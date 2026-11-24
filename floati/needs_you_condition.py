"""The needs-you condition on a seat's Stop waiter (NEEDS-YOU-OBSERVED-1, waiter series).

A seat opts in by declaring its orchestrator source in one fixed per-node file
in its root, ``config/needs-observed/NODE.json`` (the same shape ``floati needs
observe --source-config`` reads, with an optional ``interval_seconds`` from 30
to 3600, default 60). With no file the condition does not exist and the waiter
runs exactly as it did before.

The Stop waiter owns the tick. On a tick the condition pulls at most once per
interval (the pull keeps its 10 s deadline and 4 MiB bound, and the waiter's
single loop never runs two), then decides only on the pull's result: a read
pull wakes the seat for observed items whose event keys this seat has never
been woken for; a refused, failed or degraded pull never wakes it and never
stops the waiter. The waiter records a successful hook flush as a ``needs_wake``
row and publishes its own terminal exit, reason ``observed_item``. The row
suppresses the confirmed keys for this seat. Another seat with the same
source can still wake for the same keys.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Callable, Dict, List, Optional

from .errors import ProtocolRefusal

SEAT_SOURCES = Path("config/needs-observed")
PULL_STATE = Path("state/needs-observed")


def seat_source_relative(node: str) -> Path:
    return SEAT_SOURCES / (node + ".json")


class NeedsYouCondition:
    """One seat's condition for the life of one Stop waiter invocation."""

    def __init__(self, root, node: str, source: Optional[Path], report: Callable[[BaseException], None]) -> None:
        from .needs_observed import DEFAULT_INTERVAL_SECONDS
        self.root = root
        self.node = node
        self.source = source
        self.report = report
        self.interval = DEFAULT_INTERVAL_SECONDS
        try:
            self.state = root.resolve_relative(PULL_STATE) / (node + ".json")
        except ProtocolRefusal as exc:
            report(exc)
            self.state = None
        self.last = self._last_pull()
        if self.last is not None:
            self._source_config()

    def _source_config(self):
        from . import needs_observed
        if self.source is None:
            return None
        try:
            config = needs_observed.load_config(str(self.source))
        except ProtocolRefusal as exc:
            self.interval = needs_observed.DEFAULT_INTERVAL_SECONDS
            self.report(exc)
            return None
        self.interval = int(config["interval_seconds"])
        return config

    @classmethod
    def for_seat(cls, root, node: str, report: Callable[[BaseException], None]) -> Optional["NeedsYouCondition"]:
        """The condition when this seat declared a source; None, and nothing read beyond one lstat, otherwise."""
        try:
            # The file itself is never resolved, so a link there is seen and refused as a link.
            source = root.resolve_relative(SEAT_SOURCES) / (node + ".json")
            os.lstat(source)
        except FileNotFoundError:
            return None
        except (OSError, ProtocolRefusal) as exc:
            # A declaration that exists but cannot be inspected is still a
            # declaration; it never pulls and never wakes the seat.
            report(exc)
            source = None
        return cls(root, node, source, report)

    # -- the interval gate, kept across invocations of this seat's waiter --

    def _last_pull(self) -> Optional[float]:
        if self.state is None:
            return None
        try:
            value = json.loads(self.state.read_text(encoding="utf-8")).get("last_pull")
        except (OSError, ValueError, AttributeError):
            return None
        return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None

    def _record_pull(self, now: float) -> None:
        self.last = now
        if self.state is None:
            return
        try:
            self.state.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.state.with_name("." + self.state.name + "." + str(os.getpid()) + ".tmp")
            temporary.write_text(json.dumps({"last_pull": now}) + "\n", encoding="utf-8")
            os.replace(temporary, self.state)
        except OSError as exc:
            # The in-memory gate still holds for this invocation.
            self.report(exc)

    def seconds_until_due(self, now: float) -> float:
        if self.last is None or now < self.last:
            return 0.0
        return max(0.0, self.last + self.interval - now)

    def wait_cap(self, timeout: float, now: float) -> float:
        """The waiter's own wait, shortened only so that the next pull is not late."""
        return min(timeout, max(0.001, self.seconds_until_due(now)))

    # -- one tick --

    def tick(self, now: float, *, authorize: Callable[[], None]) -> List[Dict[str, object]]:
        """Pull when due; the items to wake for, or nothing when the pull did not read."""
        from . import needs_observed
        if self.seconds_until_due(now) > 0:
            return []
        config = self._source_config()
        if self.seconds_until_due(now) > 0:
            return []
        self.last = now
        if config is None:
            return []
        try:
            authorize()
            self._record_pull(now)
            evidence = needs_observed._observe_config(self.root, self.node, config, authorize=authorize)
        except Exception as exc:  # A pull that fails never stops the waiter.
            self.report(exc)
            return []
        if evidence["observation"]["state"] != "read":
            return []
        return self.pending()

    def pending(self) -> List[Dict[str, object]]:
        """Open, unread observed items whose event keys never woke this seat, oldest first."""
        from .needs_inbox import _rows, _state
        return self._eligible(_state(_rows(self.root)))

    def _eligible(self, state, selected=None) -> List[Dict[str, object]]:
        from .needs_inbox import _live
        from .needs_records import MAX_ITEMS_PER_WAKE

        woken = {entry["event_key"] for row in state["wakes"] if row["actor"] == self.node for entry in row["items"]}
        return [{"item_id": ident, "event_key": item["event_key"], "coordinate": item["coordinates"][0]}
                for ident, item in state["items"].items()
                if (selected is None or ident in selected) and item["kind"] == "needs_observed"
                and _live(state, ident) and ident not in state["reads"]
                and item["event_key"] not in woken][:MAX_ITEMS_PER_WAKE]

    def prepare_wake(self, session_digest: str, invocation_id: str, pending) -> Optional[dict]:
        """Select current items under the needs lock while the caller owns the seat wake guard."""
        from .needs_inbox import _append, _base, _state

        selected = {entry["item_id"] for entry in pending}

        def decide(rows):
            items = self._eligible(_state(rows), selected)
            if not items:
                return None, None
            return {**_base("needs_wake", "needs-wake-", self.root), "actor": self.node,
                    "session_digest": session_digest, "invocation_id": invocation_id, "items": items}, None

        return _append(self.root, decide)

    def record_flushed_wake(self, wake: dict) -> bool:
        """Confirm a flush before releasing the seat guard. A failed save leaves the wake retryable."""
        from .needs_inbox import _append
        try:
            _append(self.root, lambda rows: (None, wake))
        except Exception as exc:
            self.report(exc)
            return False
        return True

    def reason(self, wake: dict) -> str:
        """The hook text: only Floati's validated identifiers, never the orchestrator's free text."""
        lines = ["[floati] " + str(len(wake["items"])) + " observed item(s) need a person for " + self.node + ":"]
        for entry in wake["items"]:
            lines.append("- " + entry["item_id"] + " event_key " + entry["event_key"])
            lines.append("  coordinate " + json.dumps(entry["coordinate"], sort_keys=True))
        lines.append("Read them with: floati needs show --root " + str(self.root.path))
        return "\n".join(lines)

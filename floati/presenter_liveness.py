"""Presenter liveness for one recipient, derived from the bus's own receipts.

BUS-DELIVERY-UNPRESENTED-1: a delivery receipt is not evidence of
presentation.  Before a delivery receipts ``delivered``, the recipient's
presenter - its Stop waiter, hook or wake daemon - must be shown alive at the
delivery instant from journals that already exist:

- the hook journal ``receipts/stop-holds/<node>.jsonl``: an unmatched hold
  start whose testimony window covers the delivery instant shows the Stop
  waiter in hold;
- the wake-daemon journal ``receipts/wake-daemon/<node>.jsonl`` and the wake
  journal ``receipts/wakes/<node>.jsonl``: a cycle or attempt receipt inside
  the newest active consent's ``max_backoff_seconds`` window shows the daemon
  loop alive.  A lifecycle row naming ``stopped``, ``paused`` or
  ``pause_unknown`` names an arrangement that will not present.

``recipient_readiness`` alone is not evidence (SEND-READINESS-UNJOURNALED-1
measured it wrong for Codex lanes), and no lease, marker or other age stands
in for these journals.  A journal that cannot be read is evidence that it
cannot be read: it never shows a presenter, and it never refuses the
delivery - the receipt then says ``delivered_unpresented`` with the journal
named in its sources.

Read-only; every failure mode resolves to "not shown".  Callers hold the
recipient guard (the delivery path already does).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .errors import FloatiError, ProtocolRefusal
from .jsonl import read_records_snapshot
from .root import FloatiRoot, validate_identifier

STOP_HOLD_RELATIVE = Path("receipts/stop-holds")
WAKE_RELATIVE = Path("receipts/wakes")
DAEMON_RELATIVE = Path("receipts/wake-daemon")

ACTIVE_STOP_HOLD = "active_stop_hold"
RECENT_DAEMON_CYCLE = "recent_wake_daemon_cycle"
NO_LIVE_PRESENTER = "no_live_presenter_evidence"

#: Lifecycle states a serving daemon records per cycle.  ``stopped`` names a
#: deliberate stop; ``paused`` and ``pause_unknown`` name an arrangement the
#: operator asked not to wake.  No other state shows a presenter.
_DAEMON_CYCLE_STATES = frozenset(
    {"running", "idle", "backpressure", "exhausted", "refused", "unknown"}
)
_DAEMON_CONSENT_KINDS = frozenset({"wake_daemon_consent_receipt"})
_DAEMON_LIFECYCLE_KINDS = frozenset({
    "wake_daemon_lifecycle_receipt", "wake_daemon_directory_ownership_record",
    "codex_binding_repair_receipt",
})
_WAKE_KINDS = frozenset({"wake_attempt_receipt"})
_HOLD_KINDS = frozenset({"stop_hold_receipt"})
_MAX_SOURCES = 16


def _time(value: Any) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("naive timestamp")
    return parsed


def _source(path: Path, record_id: Optional[str]) -> Dict[str, Any]:
    return {"path": str(path), "record_id": record_id}


def _rows(root: FloatiRoot, relative: Path, kinds: frozenset) -> Optional[list]:
    """Read one receipt journal; ``None`` means the journal cannot be read."""
    try:
        return read_records_snapshot(root, relative, allowed_kinds=set(kinds))
    except (FloatiError, OSError, UnicodeError, ValueError, TypeError, KeyError):
        return None


def _live_hold(
    root: FloatiRoot, node: str, now: datetime, sources: List[Dict[str, Any]]
) -> bool:
    """True when an unmatched hold start's own testimony window covers now."""
    relative = STOP_HOLD_RELATIVE / f"{node}.jsonl"
    rows = _rows(root, relative, _HOLD_KINDS)
    started: Dict[str, Dict[str, Any]] = {}
    closed: set[str] = set()
    if rows is not None:
        for row in rows:
            invocation = row.get("invocation_id")
            if not isinstance(invocation, str):
                continue
            if row.get("phase") == "start":
                started.setdefault(invocation, row)
            elif row.get("phase") == "exit":
                closed.add(invocation)
    deciding: Optional[str] = None
    for invocation, row in started.items():
        if invocation in closed:
            continue
        try:
            begin = _time(row["started_at"])
        except (ValueError, TypeError, KeyError):
            continue
        deadline = row.get("deadline_seconds")
        if not isinstance(deadline, (int, float)) or isinstance(deadline, bool):
            continue
        if begin <= now < begin + timedelta(seconds=deadline):
            deciding = row.get("id") if isinstance(row.get("id"), str) else None
            break
    sources.append(_source(relative, deciding))
    return deciding is not None


def _daemon_window(
    root: FloatiRoot, node: str, sources: List[Dict[str, Any]]
) -> Optional[Tuple[Path, int]]:
    """The newest active consent's heartbeat bound, or None without one."""
    consent_relative = DAEMON_RELATIVE / f"{node}.consent.jsonl"
    legacy_relative = DAEMON_RELATIVE / f"{node}.jsonl"
    for relative, kinds in (
        (consent_relative, _DAEMON_CONSENT_KINDS),
        (legacy_relative, _DAEMON_CONSENT_KINDS),
    ):
        rows = _rows(root, relative, kinds)
        if rows is None:
            return None
        if not rows:
            continue
        newest = rows[-1]
        if newest.get("state") != "active":
            return None
        bound = newest.get("max_backoff_seconds")
        if not isinstance(bound, int) or isinstance(bound, bool) or bound <= 0:
            return None
        sources.append(_source(relative, newest.get("id")))
        return relative, bound
    return None


def _fresh(
    row: Dict[str, Any], now: datetime, seconds: int
) -> Optional[datetime]:
    try:
        moment = _time(row["timestamp"])
    except (ValueError, TypeError, KeyError):
        return None
    if moment <= now and now - moment <= timedelta(seconds=seconds):
        return moment
    return None


def _daemon_cycle(
    root: FloatiRoot,
    node: str,
    now: datetime,
    window: Tuple[Path, int],
    sources: List[Dict[str, Any]],
) -> bool:
    """True when the daemon loop left a receipt inside its consented window."""
    _, seconds = window
    lifecycle_relative = DAEMON_RELATIVE / f"{node}.jsonl"
    rows = _rows(root, lifecycle_relative, _DAEMON_LIFECYCLE_KINDS)
    sources.append(_source(lifecycle_relative, None))
    if rows is None:
        # Unreadable lifecycle history can conceal a newer stop or pause.
        # Older wake activity cannot establish the current arrangement.
        return False
    # Ownership records share this journal but never report a daemon state.
    # A delayed install append must not hide either a cycle or a later stop.
    rows = [row for row in rows if row.get("kind") == "wake_daemon_lifecycle_receipt"]
    if rows:
        newest = rows[-1]
        if newest.get("state") in {"stopped", "paused", "pause_unknown"}:
            # The native daemon keeps consent when it stops or pauses. Its
            # earlier wake activity cannot overturn that newer arrangement.
            sources[-1] = _source(lifecycle_relative, newest.get("id"))
            return False
        if newest.get("state") in _DAEMON_CYCLE_STATES and _fresh(
            newest, now, seconds
        ):
            sources[-1] = _source(
                lifecycle_relative,
                newest.get("id") if isinstance(newest.get("id"), str) else None,
            )
            return True
    wakes_relative = WAKE_RELATIVE / f"{node}.jsonl"
    rows = _rows(root, wakes_relative, _WAKE_KINDS)
    sources.append(_source(wakes_relative, None))
    if rows:
        newest = rows[-1]
        if _fresh(newest, now, seconds):
            sources[-1] = _source(
                wakes_relative,
                newest.get("id") if isinstance(newest.get("id"), str) else None,
            )
            return True
    return False


def presenter_evidence(
    root: FloatiRoot, node_id: str, *, now: datetime
) -> Dict[str, Any]:
    """Classify the recipient's presenter from the bus's own receipts.

    Returns ``{"shown": bool, "reason": code, "sources": [...]}`` where the
    sources name every journal consulted and, when one decided, its receipt.
    """
    if not isinstance(root, FloatiRoot):
        raise ProtocolRefusal("root_required", "presenter evidence requires a Floati root")
    node = validate_identifier(node_id, "node")
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise ProtocolRefusal("time_invalid", "presenter evidence requires an aware instant")
    sources: List[Dict[str, Any]] = []
    if _live_hold(root, node, now, sources):
        return {"shown": True, "reason": ACTIVE_STOP_HOLD, "sources": sources[:_MAX_SOURCES]}
    window = _daemon_window(root, node, sources)
    if window is not None and _daemon_cycle(root, node, now, window, sources):
        return {
            "shown": True,
            "reason": RECENT_DAEMON_CYCLE,
            "sources": sources[:_MAX_SOURCES],
        }
    return {"shown": False, "reason": NO_LIVE_PRESENTER, "sources": sources[:_MAX_SOURCES]}

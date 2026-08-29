"""F5 WINDOW SCHEDULING — typed scheduling against MEASURED provider windows.

THE FENCE IS THE FEATURE (brief: window-scheduling-brief-2026-08-22):
respects provider windows; NEVER account rotation or limit evasion —
terms-respect constitutional. Windows are MEASURED, never modelled. An
unknown window is a typed absence and it schedules NOTHING.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

# Closed refusal causes (spec §refusals). Unknown conditions fail closed;
# new causes enter by ruling.
CAUSE_WINDOW_UNKNOWN = "window_unknown"
CAUSE_BOUNDARY_NOT_STATED = "boundary_not_stated"
CAUSE_WINDOW_EXPIRED = "window_expired"
CAUSE_NODE_PAUSED = "node_paused"
CAUSE_WINDOW_INCOHERENT = "window_incoherent"
CAUSE_TIMESTAMP_UNREADABLE = "timestamp_unreadable"

# How a window boundary was known (stamp/receipt law: every schedule names
# its basis).
SOURCE_PROVIDER_STATED = "stated_by_provider"
SOURCE_OBSERVED_IN_RECORD = "observed_in_record"


class SchedulingRefusal(Exception):
    """Typed refusal naming its cause. Never a default interval, never a
    retry, never an optimistic attempt."""

    def __init__(self, cause: str, detail: str = "") -> None:
        if cause not in _CAUSES:
            raise ValueError("unknown refusal cause: %r" % (cause,))
        self.cause = cause
        self.detail = detail
        super().__init__("%s%s" % (cause, ": %s" % detail if detail else ""))


_CAUSES = frozenset(
    {
        CAUSE_WINDOW_UNKNOWN,
        CAUSE_BOUNDARY_NOT_STATED,
        CAUSE_WINDOW_EXPIRED,
        CAUSE_NODE_PAUSED,
        CAUSE_WINDOW_INCOHERENT,
        CAUSE_TIMESTAMP_UNREADABLE,
    }
)


@dataclass(frozen=True)
class Window:
    """A MEASURED window. Every boundary states how it was known.

    A window whose boundary lacks a stated source is NOT a window — the
    constructor refuses (`boundary_not_stated`), so no extrapolated
    boundary can ever enter the scheduler.
    """

    provider: str
    opens_at: str
    closes_at: str
    opens_source: str
    closes_source: str

    def __post_init__(self):
        for name in ("opens_source", "closes_source"):
            source = getattr(self, name)
            if source not in (SOURCE_PROVIDER_STATED, SOURCE_OBSERVED_IN_RECORD):
                raise SchedulingRefusal(
                    CAUSE_BOUNDARY_NOT_STATED,
                    "%s has no stated source: %r" % (name, source),
                )
        try:
            opens = _parse(self.opens_at)
            closes = _parse(self.closes_at)
        except ValueError as error:
            raise SchedulingRefusal(
                CAUSE_TIMESTAMP_UNREADABLE, str(error)
            ) from error
        if closes <= opens:
            # An inverted window is not a window: opens-after-closes would
            # otherwise schedule WITH A FULL BASIS STAMP - wrong with a
            # receipt attached (gate binding, F5 @214ceb3).
            raise SchedulingRefusal(
                CAUSE_WINDOW_INCOHERENT,
                "opens %s is not before closes %s"
                % (self.opens_at, self.closes_at),
            )


@dataclass(frozen=True)
class Schedule:
    """One scheduled action, stating its basis (stamp law)."""

    node: str
    action: str
    run_at: str                 # ISO instant the action may begin
    window_provider: str
    window_basis: str           # how the window was known


class Scheduler:
    """Schedules actions against measured windows. Pure; no timers, no
    waking, no polling of its own (no wake oracle — pause/resume directives
    come from THE NIGHT WATCH's ruled engine)."""

    def __init__(self, windows: Optional[List[Window]] = None,
                 paused_nodes: Optional[List[str]] = None):
        self._windows: Dict[str, Window] = {}
        for window in windows or []:
            self._windows[window.provider] = window
        self._paused = set(paused_nodes or [])

    def schedule(self, node: str, action: str, provider: str,
                 now: datetime) -> Schedule:
        """Schedule `action` for `node` against `provider`'s measured window."""
        if node in self._paused:
            raise SchedulingRefusal(CAUSE_NODE_PAUSED, node)
        window = self._windows.get(provider)
        if window is None:
            raise SchedulingRefusal(
                CAUSE_WINDOW_UNKNOWN,
                "no measured window for provider %r; an unknown window "
                "schedules NOTHING" % provider,
            )
        opens = _parse(window.opens_at)
        closes = _parse(window.closes_at)
        if now < opens:
            return Schedule(
                node=node, action=action,
                run_at=window.opens_at,
                window_provider=provider,
                window_basis="opens:%s closes:%s"
                % (window.opens_source, window.closes_source),
            )
        if now > closes:
            # The window is over. We do NOT invent the next one.
            raise SchedulingRefusal(
                CAUSE_WINDOW_EXPIRED,
                "%s closed at %s; no later window is known and none will "
                "be extrapolated" % (provider, window.closes_at),
            )
        return Schedule(
            node=node, action=action, run_at=_iso(now),
            window_provider=provider,
            window_basis="opens:%s closes:%s"
            % (window.opens_source, window.closes_source),
        )


def _parse(stamp: str) -> datetime:
    return datetime.fromisoformat(stamp.replace("Z", "+00:00"))


def _iso(moment: datetime) -> str:
    return moment.isoformat().replace("+00:00", "Z")


"""Typed night-log events (closed vocabulary; unknown kinds refuse)."""

from dataclasses import dataclass
from typing import Optional

EVENT_KINDS = frozenset(
    {
        "wake_requested",
        "wake_delivered",
        "mail_landed",
        "work_completed",
        "pause_directive",
        "resume_directive",
        "quota_ceiling_hit",
        "reset_observed",
        "loop_edge",
    }
)


@dataclass(frozen=True)
class NightEvent:
    kind: str
    node: str
    moment: str                 # ISO-8601 Z instant inside the window
    to_node: Optional[str] = None   # loop_edge target only

    def __post_init__(self):
        if self.kind not in EVENT_KINDS:
            raise ValueError(
                "unknown_event_kind: %r not in %s"
                % (self.kind, sorted(EVENT_KINDS))
            )
        if self.kind == "loop_edge" and self.to_node is None:
            raise ValueError(
                "unknown_event_kind: loop_edge requires to_node"
            )

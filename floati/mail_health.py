"""Stamped, ledger-derived mail readiness and unread-age facts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Mapping, Optional, Sequence


def wake_eligible(message: Mapping[str, object]) -> bool:
    """Legacy envelopes wake normally; FYI waits for explicit presentation."""

    return message.get("message_class", "normal") != "fyi"


@dataclass(frozen=True)
class RecipientReadiness:
    """A frozen observation of the recipient's native wake arrangements."""

    state: str
    reason: str
    observed_at: str
    mechanism_evidence: tuple = ()

    @classmethod
    def observe(cls, root, node_id: str, *, observed_at: str) -> "RecipientReadiness":
        from .bus_epoch import epoch_guard
        from .wake_hold import wake_coordination_guard
        with epoch_guard(root, exclusive=False):
            with wake_coordination_guard(root, node_id):
                return cls._observe_already_guarded(root, node_id, observed_at=observed_at)

    @classmethod
    def _observe_already_guarded(cls, root, node_id: str, *, observed_at: str) -> "RecipientReadiness":
        from .recipient_mechanisms import observe_mechanisms
        facts = observe_mechanisms(root, node_id, observed_at=observed_at)
        reasons = ("active_lease", "stop_hook", "seat_board")
        for reason, fact in zip(reasons, facts):
            if fact["state"] == "arranged":
                return cls("recipient_wake_arranged", reason, observed_at, tuple(facts))
        if all(fact["state"] == "absent" for fact in facts):
            return cls("recipient_not_listening", "all_mechanisms_absent", observed_at, tuple(facts))
        return cls("unknown", "mechanism_evidence_incomplete", observed_at, tuple(facts))

    def artifact(self) -> dict[str, object]:
        result = {
            "state": self.state,
            "reason": self.reason,
            "observed_at": self.observed_at,
        }
        if self.mechanism_evidence:
            result["checked_mechanisms"] = ["lease", "stop_hook", "seat_board"]
            result["mechanism_evidence"] = [dict(fact, sources=[dict(source) for source in fact["sources"]])
                for fact in self.mechanism_evidence]
        return result


def oldest_unread_fact(
    node: str,
    pending: Sequence[Mapping[str, object]],
    *,
    now: datetime,
) -> Optional[dict[str, object]]:
    """Return the oldest unread envelope's age, stamped at this observation."""

    if not pending:
        return None
    current = now.astimezone(timezone.utc)
    oldest = min(_parse_timestamp(str(row["timestamp"])) for row in pending)
    return {
        "node": node,
        "age_minutes": int((current - oldest).total_seconds() // 60),
        "observed_at": current.isoformat(timespec="seconds").replace("+00:00", "Z"),
    }


def _parse_timestamp(stamp: str) -> datetime:
    return datetime.fromisoformat(stamp.replace("Z", "+00:00")).astimezone(timezone.utc)

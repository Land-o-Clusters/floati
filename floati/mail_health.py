"""Stamped, ledger-derived mail readiness and unread-age facts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Mapping, Optional, Sequence


@dataclass(frozen=True)
class RecipientReadiness:
    """What the registry lease says about a recipient's wake arrangement."""

    state: str
    reason: str
    observed_at: str

    @classmethod
    def from_lease(
        cls, lease: Mapping[str, object], *, observed_at: str
    ) -> "RecipientReadiness":
        if lease.get("state") == "active":
            return cls("recipient_wake_arranged", "active_lease", observed_at)
        return cls("recipient_not_listening", "no_active_lease", observed_at)

    def artifact(self) -> dict[str, str]:
        return {
            "state": self.state,
            "reason": self.reason,
            "observed_at": self.observed_at,
        }


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

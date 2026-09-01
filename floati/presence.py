"""Self-reported node liveness and its honest read-only projection."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List

from .errors import ProtocolRefusal
from .jsonl import read_records_snapshot
from .planes import LivenessPresenceStore
from .registry import Registry
from .root import FloatiRoot


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ProtocolRefusal(
            "time_invalid", "an aware UTC-compatible datetime is required"
        )
    return value.astimezone(timezone.utc)


def _parse(value: object) -> datetime:
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(
        timezone.utc
    )


class PresenceService:
    """Record only an actor's own report and project reports without inference."""

    def __init__(self, root: FloatiRoot) -> None:
        self.root = root
        self.registry = Registry(root)
        self.store = LivenessPresenceStore(root)

    def report_self(
        self, actor: str, *, ttl_seconds: int, now: datetime
    ) -> Dict[str, object]:
        reporter = str(self.registry.require_active(actor)["node_id"])
        return self.store.observe(reporter, ttl_seconds, _utc(now))

    def reports(self, now: datetime) -> List[Dict[str, object]]:
        current = _utc(now)
        reports: List[Dict[str, object]] = []
        for node_id in self.registry.active_node_ids():
            records = read_records_snapshot(
                self.root,
                Path("liveness-presence") / f"{node_id}.jsonl",
                allowed_kinds={"liveness_presence"},
            )
            if not records:
                reports.append(
                    {
                        "node_id": node_id,
                        "state": "never_reported",
                        "reported_at": None,
                        "ttl_seconds": None,
                        "expires_at": None,
                    }
                )
                continue
            latest = records[-1]
            reported_at = _parse(latest["observed_at"])
            expires_at = _parse(latest["expires_at"])
            reports.append(
                {
                    "node_id": node_id,
                    "state": (
                        "recent_report"
                        if current < expires_at
                        else "no_report_since"
                    ),
                    "reported_at": latest["observed_at"],
                    "ttl_seconds": int((expires_at - reported_at).total_seconds()),
                    "expires_at": latest["expires_at"],
                }
            )
        return reports

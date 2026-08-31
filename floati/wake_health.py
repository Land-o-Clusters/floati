"""Stamped wake-path health projected from durable local evidence."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional

from .delivery_health import DeliveryHealthAnalyzer
from .errors import IntegrityFailure
from .events import EventLog
from .jsonl import read_records_snapshot
from .registry import Registry
from .root import FloatiRoot
from .wake_control import is_session_paused


class WakeHealthProjection:
    """Join claim, waiter, breaker, pause, and unread facts for one node."""

    def __init__(self, root: FloatiRoot) -> None:
        self.root = root

    @staticmethod
    def _current(now: datetime) -> datetime:
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("wake health observation requires an aware datetime")
        return now.astimezone(timezone.utc)

    @staticmethod
    def _stamp(now: datetime) -> str:
        return now.isoformat(timespec="seconds").replace("+00:00", "Z")

    @staticmethod
    def _latest(rows: list[Dict[str, object]]) -> Optional[Dict[str, object]]:
        return rows[-1] if rows else None

    def fact(self, node_id: str, now: datetime) -> Dict[str, object]:
        current = self._current(now)
        node = Registry(self.root).resolve_node_id(node_id, field="node")
        sessions = read_records_snapshot(
            self.root,
            Path("receipts/codex-wait-session") / f"{node}.jsonl",
            allowed_kinds={"codex_wait_session_receipt"},
        )
        claim = self._latest(sessions)
        claim_session = (
            str(claim["acting_session_id"])
            if claim is not None and claim.get("state") == "armed"
            else None
        )
        attempts = read_records_snapshot(
            self.root,
            Path("receipts/wakes") / f"{node}.jsonl",
            allowed_kinds={"wake_attempt_receipt"},
        )
        last_attempt = self._latest(attempts)
        last_seen_session = (
            None if last_attempt is None else str(last_attempt["acting_session_id"])
        )
        exits = read_records_snapshot(
            self.root,
            Path("receipts/wake-waiter-exit") / f"{node}.jsonl",
            allowed_kinds={"wake_waiter_exit_receipt"},
        )
        last_exit = self._latest(exits)
        if last_exit is None:
            waiter_exit_reason = None
            waiter_receipt_age_minutes = None
        else:
            waiter_exit_reason = str(last_exit["reason_code"])
            try:
                stamp = datetime.fromisoformat(
                    str(last_exit["timestamp"]).replace("Z", "+00:00")
                ).astimezone(timezone.utc)
            except ValueError as exc:
                raise IntegrityFailure(
                    "wake_health_receipt_invalid",
                    "waiter exit receipt timestamp is unavailable",
                ) from exc
            waiter_receipt_age_minutes = max(
                0, int((current - stamp).total_seconds() // 60)
            )

        breaker_path = self.root.resolve_relative(
            Path("state/codex-wait") / node / "breaker.json"
        )
        try:
            breaker = json.loads(breaker_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            hits = []
        except (OSError, json.JSONDecodeError) as exc:
            raise IntegrityFailure(
                "wake_health_breaker_invalid", "waiter breaker evidence is malformed"
            ) from exc
        else:
            if not isinstance(breaker, dict) or not isinstance(breaker.get("hits"), list):
                raise IntegrityFailure(
                    "wake_health_breaker_invalid", "waiter breaker evidence is malformed"
                )
            hits = [
                float(value) for value in breaker["hits"]
                if isinstance(value, (int, float))
                and 0 <= current.timestamp() - float(value) < 60
            ]
        breaker_state = "open" if len(hits) > 20 else "closed"
        pause_state = (
            "unknown" if claim_session is None
            else "paused" if is_session_paused(self.root, node, claim_session)
            else "active"
        )
        events, _unrecognized_kinds, _skew = (
            EventLog(self.root)._compatible_event_records_with_skew(snapshot=True)
        )
        health = DeliveryHealthAnalyzer.analyze(
            events=events, root=self.root, nodes=[node], now=current
        ).by_node[node]
        oldest_unread = health.oldest_unread

        entrypoint = Path(__file__).resolve().parents[1] / "scripts" / "floati-codex-wait"
        if pause_state == "paused":
            state = "paused"
        elif breaker_state == "open":
            state = "breaker_open"
        elif oldest_unread is not None and (
            claim_session is None or last_seen_session != claim_session
        ):
            state = "stale_claim_with_unread_mail"
        elif oldest_unread is not None:
            state = "unread_mail"
        else:
            state = "healthy"
        remedy = (
            f"verify the claim and run {entrypoint} --root {self.root.path} "
            f"for node {node} at {self._stamp(current)}"
        )
        return {
            "schema_version": 1,
            "node_id": node,
            "state": state,
            "observed_at": self._stamp(current),
            "claim_session": claim_session,
            "last_seen_session": last_seen_session,
            "waiter_exit_reason": waiter_exit_reason,
            "waiter_receipt_age_minutes": waiter_receipt_age_minutes,
            "breaker_state": breaker_state,
            "pause_state": pause_state,
            "oldest_unread": oldest_unread,
            "documented_entrypoint": str(entrypoint),
            "documented_entrypoint_resolves": entrypoint.is_file(),
            "remedy": remedy,
        }

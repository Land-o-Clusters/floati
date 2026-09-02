"""Sender-side mail status derived only from envelope and receipt evidence."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from .errors import IntegrityFailure, ProtocolRefusal
from .events import EventLog
from .jsonl import read_records_snapshot
from .records import WAKE_HOLD_KINDS
from .registry import Registry
from .root import FloatiRoot


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _timestamp(value: object, field: str) -> datetime:
    if not isinstance(value, str):
        raise IntegrityFailure("mail_receipt_time_invalid", f"{field} is not a timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise IntegrityFailure(
            "mail_receipt_time_invalid", f"{field} is not an RFC3339 timestamp"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise IntegrityFailure("mail_receipt_time_invalid", f"{field} is not aware")
    return parsed.astimezone(timezone.utc)


def _aware_now(now: Optional[datetime]) -> datetime:
    current = _utc_now() if now is None else now
    if (
        not isinstance(current, datetime)
        or current.tzinfo is None
        or current.utcoffset() is None
    ):
        raise ProtocolRefusal("time_invalid", "sent projection requires an aware time")
    return current.astimezone(timezone.utc)


def _receipt_paths(root: FloatiRoot, directory: str, recipient: str) -> Iterable[Path]:
    base = Path("receipts") / directory
    plain = base / f"{recipient}.jsonl"
    if root.resolve_relative(plain).is_file():
        yield plain
    session_directory = root.resolve_relative(base / recipient)
    if session_directory.is_dir():
        for path in sorted(session_directory.glob("*.jsonl")):
            if path.is_file():
                yield path.relative_to(root.tenant_home)


def _receipt_times(
    root: FloatiRoot,
    recipients: Iterable[str],
    *,
    directory: str,
    kind: str,
) -> Dict[str, datetime]:
    allowed = set(WAKE_HOLD_KINDS) if kind == "delivery_receipt" else {kind}
    result: Dict[str, datetime] = {}
    for recipient in sorted(set(recipients)):
        for relative in _receipt_paths(root, directory, recipient):
            rows = read_records_snapshot(root, relative, allowed_kinds=allowed)
            for row in rows:
                if row.get("kind") != kind:
                    continue
                if row.get("recipient") != recipient:
                    raise IntegrityFailure(
                        "mail_receipt_recipient_invalid",
                        f"{kind} belongs to another recipient",
                    )
                moment = _timestamp(row.get("timestamp"), f"{kind}.timestamp")
                for item_id in row.get("item_ids", []):
                    item = str(item_id)
                    if item not in result or moment < result[item]:
                        result[item] = moment
    return result


def _seconds(later: datetime, earlier: datetime, field: str) -> int:
    delta = int((later - earlier).total_seconds())
    if delta < 0:
        raise IntegrityFailure(
            "mail_receipt_order_invalid", f"{field} precedes its source evidence"
        )
    return delta


class SentProjection:
    """Project every envelope sent by one active node without writing anything."""

    def __init__(self, root: FloatiRoot) -> None:
        if not isinstance(root, FloatiRoot):
            raise ProtocolRefusal("root_required", "sent projection requires a Floati root")
        self.root = root

    def items(
        self, sender: str, *, now: Optional[datetime] = None
    ) -> List[Dict[str, object]]:
        actor = Registry(self.root).resolve_node_id(sender, field="sender")
        current = _aware_now(now)
        envelopes = [
            row
            for row in EventLog(self.root).event_records()
            if row.get("kind") == "message_envelope" and row.get("sender") == actor
        ]
        recipients = [str(row["recipient"]) for row in envelopes]
        delivered_at = _receipt_times(
            self.root,
            recipients,
            directory="deliveries",
            kind="delivery_receipt",
        )
        acknowledged_at = _receipt_times(
            self.root,
            recipients,
            directory="acks",
            kind="ack_receipt",
        )
        items: List[Dict[str, object]] = []
        for envelope in envelopes:
            message_id = str(envelope["id"])
            sent_at = _timestamp(envelope.get("timestamp"), "message.timestamp")
            delivery = delivered_at.get(message_id)
            acknowledgment = acknowledged_at.get(message_id)
            if acknowledgment is not None and delivery is None:
                raise IntegrityFailure(
                    "mail_receipt_order_invalid",
                    "acknowledgment exists without delivery evidence",
                )
            state = (
                "acknowledged"
                if acknowledgment is not None
                else "delivered_unacknowledged"
                if delivery is not None
                else "undelivered"
            )
            items.append(
                {
                    "message_id": message_id,
                    "recipient": envelope["recipient"],
                    "sent_at": envelope["timestamp"],
                    "state": state,
                    "delivered": delivery is not None,
                    "acknowledged": acknowledgment is not None,
                    "delivered_at": None
                    if delivery is None
                    else delivery.isoformat(timespec="milliseconds").replace(
                        "+00:00", "Z"
                    ),
                    "acknowledged_at": None
                    if acknowledgment is None
                    else acknowledgment.isoformat(timespec="milliseconds").replace(
                        "+00:00", "Z"
                    ),
                    "message_age_seconds": _seconds(current, sent_at, "observation"),
                    "delivery_latency_seconds": None
                    if delivery is None
                    else _seconds(delivery, sent_at, "delivery"),
                    "acknowledgment_latency_seconds": None
                    if acknowledgment is None
                    else _seconds(acknowledgment, delivery, "acknowledgment"),
                    "attention_age_seconds": None
                    if delivery is None or acknowledgment is not None
                    else _seconds(current, delivery, "observation"),
                }
            )
        return items

    def artifact(
        self, sender: str, *, now: Optional[datetime] = None
    ) -> Dict[str, object]:
        current = _aware_now(now)
        actor = Registry(self.root).resolve_node_id(sender, field="sender")
        return {
            "sender": actor,
            "observed_at": current.isoformat(timespec="milliseconds").replace(
                "+00:00", "Z"
            ),
            "items": self.items(actor, now=current),
        }

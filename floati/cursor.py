"""Per-item acknowledgment receipts that preserve non-contiguous completion."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, FrozenSet, List, Optional, Sequence

from .bus_epoch import shared_epoch_operation
from .epoch_rotation import automatic_epoch_operation
from .errors import IntegrityFailure, ProtocolRefusal
from .ids import uuid7_hex
from .jsonl import read_records, read_records_sealed_snapshot, read_records_snapshot
from .records import WAKE_HOLD_KINDS
from .root import FloatiRoot, validate_identifier


MAX_ACK_ITEMS = 1000


class SparseCursor:
    def __init__(self, root: FloatiRoot) -> None:
        self.root = root

    @staticmethod
    def _session_component(worker_session_id: str) -> str:
        if (
            not isinstance(worker_session_id, str)
            or not worker_session_id
            or len(worker_session_id) > 512
            or any(ord(character) < 32 or ord(character) == 127 for character in worker_session_id)
        ):
            raise ProtocolRefusal(
                "worker_session_id_invalid",
                "worker session must be a bounded opaque non-control string",
            )
        return hashlib.sha256(worker_session_id.encode("utf-8")).hexdigest()

    def path_for(
        self, recipient: str, *, worker_session_id: Optional[str] = None
    ) -> Path:
        return self.root.tenant_home / self._relative_path_for(
            recipient, worker_session_id=worker_session_id
        )

    def _relative_path_for(
        self, recipient: str, *, worker_session_id: Optional[str] = None
    ) -> Path:
        node = validate_identifier(recipient, "node")
        if worker_session_id is None:
            return Path("receipts/acks") / f"{node}.jsonl"
        return (
            Path("receipts/acks")
            / node
            / f"{self._session_component(worker_session_id)}.jsonl"
        )

    def delivery_path_for(
        self, recipient: str, *, worker_session_id: Optional[str] = None
    ) -> Path:
        return self.root.tenant_home / self._delivery_relative_path_for(
            recipient, worker_session_id=worker_session_id
        )

    def _delivery_relative_path_for(
        self, recipient: str, *, worker_session_id: Optional[str] = None
    ) -> Path:
        node = validate_identifier(recipient, "node")
        if worker_session_id is None:
            return Path("receipts/deliveries") / f"{node}.jsonl"
        return (
            Path("receipts/deliveries")
            / node
            / f"{self._session_component(worker_session_id)}.jsonl"
        )

    def _event_records(self) -> List[Dict[str, object]]:
        """Read the canonical message/retraction projection before cursor use."""

        from .events import EventLog

        return EventLog(self.root).event_records()

    def acked_ids(
        self, recipient: str, *, worker_session_id: Optional[str] = None
    ) -> FrozenSet[str]:
        frames = self._event_records()
        events = [row for row in frames if row["kind"] == "message_envelope"]
        retracted_ids = {
            str(row["retracted_message_id"])
            for row in frames
            if row["kind"] == "message_retracted"
        }
        _delivered, acknowledged = self.state_for(
            recipient,
            events,
            worker_session_id=worker_session_id,
            retracted_ids=retracted_ids,
        )
        return acknowledged

    def state_for(
        self,
        recipient: str,
        events: Sequence[Dict[str, object]],
        *,
        worker_session_id: Optional[str] = None,
        retracted_ids: FrozenSet[str] | set[str] = frozenset(),
        snapshot: bool = False,
        sealed: bool = False,
    ) -> tuple[FrozenSet[str], FrozenSet[str]]:
        node = validate_identifier(recipient, "node")
        if worker_session_id is not None:
            self._session_component(worker_session_id)
        acknowledged = set()
        if sealed:
            presented, ackable = self.validate_deliveries(
                node, events, worker_session_id=worker_session_id, sealed=True)
        elif snapshot:
            presented, ackable = self.validate_deliveries(
                node, events, worker_session_id=worker_session_id, snapshot=True)
        else:
            presented, ackable = self.validate_deliveries(
                node, events, worker_session_id=worker_session_id)
        event_by_id = {str(item["id"]): item for item in events}
        reader = read_records_sealed_snapshot if sealed else (read_records_snapshot if snapshot else read_records)
        for receipt in reader(
            self.root,
            self._relative_path_for(node, worker_session_id=worker_session_id),
            allowed_kinds={"ack_receipt"},
        ):
            if receipt.get("kind") != "ack_receipt" or receipt.get("recipient") != node:
                raise IntegrityFailure("ack_evidence_invalid", "ack receipt belongs to another contract")
            item_ids = receipt.get("item_ids")
            if not isinstance(item_ids, list) or not all(isinstance(item, str) for item in item_ids):
                raise IntegrityFailure("ack_evidence_invalid", "ack receipt has invalid item ids")
            for item_id in item_ids:
                event = event_by_id.get(item_id)
                if (
                    event is None
                    or event.get("recipient") != node
                    or event.get("worker_session_id") != worker_session_id
                    or item_id not in ackable
                ):
                    raise IntegrityFailure("ack_evidence_invalid", "ack does not correspond to delivered session evidence")
            acknowledged.update(item_ids)
        return presented, frozenset(acknowledged)

    def ack_references_for(self, recipient: str, message: Dict[str, object]) -> List[Dict[str, object]]:
        """Select a current consumption ACK without replacing full projections.

        The consumption caller retains its native coordination guard and
        candidate envelope/retraction/session checks. Whole-plane validation
        on a cache miss remains a prerequisite to this selected native join.
        """
        node = validate_identifier(recipient, "node")
        if (not isinstance(message, dict) or message.get("tenant_id") != self.root.tenant_id
                or message.get("kind") != "message_envelope" or message.get("recipient") != node):
            raise IntegrityFailure("ack_evidence_invalid", "selected ACK has no matching recipient envelope")
        worker = message.get("worker_session_id")
        if worker is not None:
            self._session_component(worker)
        from .cursor_prefix import selected_ack_references
        return selected_ack_references(self.root, node, message)

    def validate_deliveries(
        self,
        recipient: str,
        events: Sequence[Dict[str, object]],
        *,
        worker_session_id: Optional[str] = None,
        snapshot: bool = False,
        sealed: bool = False,
    ) -> tuple[FrozenSet[str], FrozenSet[str]]:
        """Classify one plane read into presented and ackable item ids.

        Only a plain ``delivery_receipt`` proves presentation, so only its
        items suppress re-presentation.  A ``delivered_unpresented`` receipt
        (BUS-DELIVERY-UNPRESENTED-1) is still delivery evidence for an
        acknowledgment - the seat demonstrably saw the item to ack it - but it
        never stands in for presentation.
        """
        node = validate_identifier(recipient, "node")
        if worker_session_id is not None:
            self._session_component(worker_session_id)
        event_by_id = {str(item["id"]): item for item in events}
        presented = set()
        ackable = set()
        reader = read_records_sealed_snapshot if sealed else (read_records_snapshot if snapshot else read_records)
        for receipt in reader(
            self.root,
            self._delivery_relative_path_for(node, worker_session_id=worker_session_id),
            allowed_kinds=set(WAKE_HOLD_KINDS),
        ):
            if receipt.get("recipient") != node:
                raise IntegrityFailure("delivery_evidence_invalid", "delivery belongs to another recipient")
            for item_id in receipt["item_ids"]:
                event = event_by_id.get(str(item_id))
                if (
                    event is None
                    or event.get("recipient") != node
                    or event.get("worker_session_id") != worker_session_id
                ):
                    raise IntegrityFailure(
                        "delivery_evidence_invalid",
                        "delivery references missing or wrong-session recipient evidence",
                    )
                item_id = str(item_id)
                ackable.add(item_id)
                if receipt.get("kind") == "delivery_receipt":
                    presented.add(item_id)
        return frozenset(presented), frozenset(ackable)

    @automatic_epoch_operation("ack")
    @shared_epoch_operation(selected_writer=True)
    def ack(
        self,
        recipient: str,
        item_ids: Sequence[str],
        *,
        acting_session_id: str,
        worker_session_id: Optional[str] = None,
        now: Optional[datetime] = None,
    ) -> Dict[str, object]:
        """Acknowledge under the coordination lock without turning holds into a cursor."""
        from .registry import Registry
        node = Registry(self.root).resolve_node_id(recipient)
        from .wake_hold import wake_coordination_guard
        with wake_coordination_guard(self.root, node, worker_session_id=worker_session_id):
            return self._ack_already_guarded(
                node,
                item_ids,
                acting_session_id=acting_session_id,
                worker_session_id=worker_session_id,
                now=now,
            )

    def _ack_already_guarded(
        self,
        recipient: str,
        item_ids: Sequence[str],
        *,
        acting_session_id: str,
        worker_session_id: Optional[str] = None,
        now: Optional[datetime] = None,
    ) -> Dict[str, object]:
        from .wake_control import validate_session_id

        node = validate_identifier(recipient, "node")
        acting_session = validate_session_id(acting_session_id)
        if worker_session_id is not None:
            self._session_component(worker_session_id)
        if (
            not isinstance(item_ids, (list, tuple))
            or not 1 <= len(item_ids) <= MAX_ACK_ITEMS
            or not all(isinstance(item, str) and item for item in item_ids)
            or len(set(item_ids)) != len(item_ids)
        ):
            raise ProtocolRefusal("ack_items_invalid", "ack needs 1 to 1000 unique item ids")
        requested = list(item_ids)
        ack_relative = self._relative_path_for(
            node, worker_session_id=worker_session_id
        )
        from .send_prefix import event_references
        from .cursor_prefix import delivery_membership, transact_ack

        # Certify the authoritative prefix before consulting recipient evidence,
        # retaining the native integrity-failure precedence. Every delivery
        # reference is still validated, including unrequested historical items.
        _events, _retracted, proof = event_references(self.root, requested)
        ackable = delivery_membership(self.root, node, requested,
            worker_session_id=worker_session_id, proof=proof)
        event_records, retracted_ids, _proof = event_references(self.root, requested, previous=proof)
        events = {str(record["id"]): record for record in event_records}

        for item_id in requested:
            event = events.get(item_id)
            if event is None:
                if self._item_was_archived(item_id, node, worker_session_id=worker_session_id):
                    raise ProtocolRefusal(
                        "ack_item_archived",
                        f"message {item_id} moved to an owned epoch archive",
                    )
                raise ProtocolRefusal("ack_item_unknown", f"message {item_id} is unknown")
            if event.get("recipient") != node:
                raise ProtocolRefusal("ack_recipient_mismatch", f"message {item_id} belongs to another recipient")
            if event.get("worker_session_id") != worker_session_id:
                raise ProtocolRefusal("ack_session_mismatch", f"message {item_id} belongs to another worker session")
            if item_id in retracted_ids:
                raise ProtocolRefusal("ack_item_retracted", f"message {item_id} has been retracted")
            if item_id not in ackable:
                if self._item_was_archived(item_id, node, worker_session_id=worker_session_id):
                    raise ProtocolRefusal(
                        "ack_item_archived",
                        f"message {item_id} moved to an owned epoch archive",
                    )
                raise ProtocolRefusal("ack_item_not_delivered", f"message {item_id} has not been delivered")

        current = datetime.now(timezone.utc) if now is None else now
        if (
            not isinstance(current, datetime)
            or current.tzinfo is None
            or current.utcoffset() is None
        ):
            raise ProtocolRefusal(
                "time_invalid", "acknowledgment requires an aware datetime"
            )
        current = current.astimezone(timezone.utc)
        from .registry import Registry

        lease = Registry(self.root).node_lease_state(node, now=current)

        receipt: Dict[str, object] = {
            "schema_version": 1,
            "id": "ack-" + uuid7_hex(),
            "tenant_id": self.root.tenant_id,
            "timestamp": current.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "kind": "ack_receipt",
            "recipient": node,
            "acting_session_id": acting_session,
            "node_lease_id": lease["node_lease_id"],
            "node_lease_state_at_ack": lease["state"],
            "node_lease_expires_at": lease["expires_at"],
            "item_ids": requested,
        }

        def decide(prior: List[Dict[str, object]]) -> tuple[Dict[str, object], object]:
            for existing in reversed(prior):
                if (
                    existing.get("item_ids") == requested
                    and existing.get("acting_session_id") == acting_session
                ):
                    return existing, None
            return receipt, receipt

        return transact_ack(self.root, ack_relative, requested, acting_session, decide,
            worker_session_id=worker_session_id)

    def _item_was_archived(self, item_id: str, recipient: str, *,
                           worker_session_id: Optional[str] = None) -> bool:
        from .send_prefix import current_epoch_has_no_roll_head
        if current_epoch_has_no_roll_head(self.root):
            return False
        from .epoch_history import archived_item_membership

        return archived_item_membership(self.root, item_id, recipient, worker_session_id)

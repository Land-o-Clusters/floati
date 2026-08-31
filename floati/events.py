"""Guarded message events and distinct delivery/denial evidence."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
import hashlib
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

from .errors import IntegrityFailure, ProtocolRefusal, SnapshotRefusal
from .ids import uuid7_hex
from .jsonl import (
    append_record,
    read_records,
    read_records_compatible,
    read_records_compatible_snapshot,
    read_records_snapshot,
    transact,
    transact_records,
)
from .records import WAKE_HOLD_KINDS, validate_record
from .registry import Registry, utc_now
from .root import FloatiRoot, validate_identifier
from .runtruth import RUN_KINDS
from .snapshot import SnapshotStore, SourceSpec


MAX_PRESENTATION_ITEMS = 1000
EVENT_KINDS = frozenset(
    {"message_envelope", "delivery_claim", "message_retracted"}
)
_ATTEMPT_BINDING_FIELDS = frozenset(
    {"attempt_id", "claim_id", "lease_id", "worker_session_id"}
)


def validate_event_records(records: Sequence[Dict[str, object]]) -> None:
    """Reject a schema-valid event prefix that cannot be semantically replayed."""

    prior_by_id: Dict[str, Dict[str, object]] = {}
    retracted = set()
    for record in records:
        if record["kind"] == "message_envelope":
            reply_to = record.get("reply_to")
            if reply_to is not None:
                original = prior_by_id.get(str(reply_to))
                if original is None:
                    raise IntegrityFailure(
                        "message_order_invalid",
                        "reply evidence must follow its referenced original message",
                    )
                if (
                    original["sender"] != record["recipient"]
                    or original["recipient"] != record["sender"]
                ):
                    raise IntegrityFailure(
                        "message_order_invalid",
                        "reply evidence must reverse the original message parties",
                    )
            prior_by_id[str(record["id"])] = record
            continue
        if record["kind"] == "delivery_claim":
            envelope = prior_by_id.get(str(record["note_ref"]))
            if envelope is None:
                raise IntegrityFailure(
                    "delivery_claim_order_invalid",
                    "delivery claim evidence must follow its referenced message",
                )
            if envelope["sha"] != record["sha"]:
                raise IntegrityFailure(
                    "delivery_claim_sha_invalid",
                    "delivery claim sha must equal its referenced message sha",
                )
            continue
        original_id = str(record["retracted_message_id"])
        original = prior_by_id.get(original_id)
        if original is None:
            raise IntegrityFailure("message_retraction_order_invalid", "retraction evidence must follow its original message")
        if original.get("worker_session_id") != record["worker_session_id"]:
            raise IntegrityFailure("message_retraction_session_invalid", "retraction worker session must equal its original message session")
        if record["author"] not in {original["sender"], original["recipient"]}:
            raise IntegrityFailure("message_retraction_party_invalid", "retraction author must be an original message party")
        if original_id in retracted:
            raise IntegrityFailure("message_retraction_duplicate", "a message can have one append-only retraction")
        retracted.add(original_id)


class EventLog:
    def __init__(self, root: FloatiRoot, registry: Optional[Registry] = None) -> None:
        self.root = root
        self.registry = registry or Registry(root)
        self.relative_path = Path("events.jsonl")
        self.denial_relative_path = Path("receipts/denials.jsonl")
        self.path = root.resolve_relative(self.relative_path)
        self.denial_path = root.resolve_relative(self.denial_relative_path)

    def delivery_path_for(
        self, recipient: str, *, worker_session_id: Optional[str] = None
    ) -> Path:
        return self.root.resolve_relative(
            self._delivery_relative_path(recipient, worker_session_id=worker_session_id)
        )

    def records(self) -> List[Dict[str, object]]:
        """Return retained message envelopes, including messages later retracted."""

        return [
            record
            for record in self.event_records()
            if record["kind"] == "message_envelope"
        ]

    @staticmethod
    def _claim_semantics(record: Mapping[str, object]) -> Dict[str, object]:
        return {
            field: record[field]
            for field in (
                "kind", "schema_version", "sha", "repo_path", "bank",
                "declared", "artifacts", "deadline_seconds",
            )
        }

    def _delivery_claim(
        self,
        claim: object,
        *,
        envelope_id: str,
        envelope_sha: str,
    ) -> Dict[str, object]:
        if not isinstance(claim, Mapping):
            raise ProtocolRefusal(
                "claim_not_object", "delivery claim document must be a JSON object"
            )
        expected = {
            "kind", "schema_version", "sha", "repo_path", "bank",
            "declared", "artifacts", "deadline_seconds",
        }
        if set(claim) != expected:
            raise ProtocolRefusal(
                "claim_fields_invalid",
                "delivery claim document fields do not match the v0 intake contract",
            )
        if claim.get("sha") != envelope_sha:
            raise ProtocolRefusal(
                "claim_sha_mismatch",
                "delivery claim sha must equal the enclosing send sha",
            )
        record: Dict[str, object] = {
            "schema_version": claim.get("schema_version"),
            "id": "delivery-claim-" + uuid7_hex(),
            "tenant_id": self.root.tenant_id,
            "timestamp": utc_now(),
            "kind": claim.get("kind"),
            "sha": claim.get("sha"),
            "repo_path": claim.get("repo_path"),
            "bank": claim.get("bank"),
            "declared": claim.get("declared"),
            "artifacts": claim.get("artifacts"),
            "note_ref": envelope_id,
            "deadline_seconds": claim.get("deadline_seconds"),
        }
        return validate_record(
            record,
            self.root.tenant_id,
            frozenset({"delivery_claim"}),
            integrity=False,
        )

    def event_records(self) -> List[Dict[str, object]]:
        """Return the authoritative append-only message/retraction ledger in frame order."""

        records = read_records(self.root, self.relative_path, allowed_kinds=EVENT_KINDS)
        self._validate_event_records(records)
        return records

    def compatible_event_records(
        self, *, snapshot: bool = False
    ) -> Tuple[List[Dict[str, object]], List[Dict[str, object]]]:
        """Read known event testimony without letting future kinds darken the bus."""

        reader = read_records_compatible_snapshot if snapshot else read_records_compatible
        records, unrecognized = reader(
            self.root, self.relative_path, allowed_kinds=set(EVENT_KINDS)
        )
        self._validate_event_records(records)
        return records, unrecognized

    @staticmethod
    def _validate_event_records(records: Sequence[Dict[str, object]]) -> None:
        """Reject a schema-valid event prefix that cannot be semantically replayed."""
        validate_event_records(records)

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

    def _delivery_relative_path(
        self, recipient: str, *, worker_session_id: Optional[str] = None
    ) -> Path:
        node = validate_identifier(recipient, "recipient")
        if worker_session_id is None:
            return Path("receipts/deliveries") / f"{node}.jsonl"
        return (
            Path("receipts/deliveries")
            / node
            / f"{self._session_component(worker_session_id)}.jsonl"
        )

    @staticmethod
    def _normalized_attempt_binding(value: object) -> object:
        """Persist only the reviewer's exact full binding or literal legacy sentinel."""

        if not isinstance(value, dict) or set(value) != _ATTEMPT_BINDING_FIELDS:
            return "absent_legacy"
        return dict(value)

    def _deny(self, sender: str, recipient: str, reason_code: str) -> None:
        def safe_claim(value: object) -> str:
            if isinstance(value, str) and 1 <= len(value) <= 64:
                return value
            digest = hashlib.sha256(repr(value).encode("utf-8")).hexdigest()[:16]
            return "invalid-" + digest
        denial: Dict[str, object] = {
            "schema_version": 0,
            "id": "denial-" + uuid7_hex(),
            "tenant_id": self.root.tenant_id,
            "timestamp": utc_now(),
            "kind": "denial_receipt",
            "attempt_id": "attempt-" + uuid7_hex(),
            "claimed_sender": safe_claim(sender),
            "claimed_recipient": safe_claim(recipient),
            "reason_code": reason_code,
        }
        append_record(self.root, self.denial_relative_path, denial, allowed_kinds={"denial_receipt"})
        raise ProtocolRefusal(reason_code, f"message refused: {reason_code}")

    def send(
        self,
        sender: str,
        recipient: str,
        repo: str,
        sha: str,
        doc: str,
        note: str,
        *,
        reply_to: Optional[str] = None,
        idempotency_key: Optional[str] = None,
        worker_session_id: Optional[str] = None,
        attempt_binding: object = None,
        claim: object = None,
        now: Optional[datetime] = None,
    ) -> Dict[str, object]:
        # Node spellings are lexical input, not a claim eligible for durable
        # denial evidence.  Validate both parties before touching registry or
        # receipt state so malformed CLI values have a zero-state refusal.
        sender = self.registry.resolve_node_id(
            sender, field="sender", unknown_code="unknown_sender",
        )
        recipient = self.registry.resolve_node_id(
            recipient, field="recipient", unknown_code="unknown_recipient",
        )
        key = ("idempotency-" + uuid7_hex()) if idempotency_key is None else idempotency_key
        if not isinstance(key, str) or not key or len(key) > 128:
            raise ProtocolRefusal("idempotency_key_invalid", "idempotency key must contain 1 to 128 characters")
        binding = self._normalized_attempt_binding(attempt_binding)
        session = worker_session_id
        if isinstance(binding, dict) and session is None:
            session = binding["worker_session_id"]  # validator proves this opaque value
        prior_events = self.event_records()
        if reply_to is not None:
            original = next((record for record in prior_events if record["id"] == reply_to), None)
            if original is None:
                self._deny(sender, recipient, "reply_to_unknown")
            if original["sender"] != recipient or original["recipient"] != sender:
                self._deny(sender, recipient, "reply_to_parties_mismatch")
        envelope: Dict[str, object] = {
            "schema_version": 0,
            "id": "msg-" + uuid7_hex(),
            "tenant_id": self.root.tenant_id,
            "timestamp": utc_now(),
            "kind": "message_envelope",
            "sender": sender,
            "recipient": recipient,
            "repo": repo,
            "sha": sha,
            "doc": doc,
            "note": note,
            "idempotency_key": key,
            "attempt_binding": binding,
        }
        if reply_to is not None:
            envelope["reply_to"] = reply_to
        if session is not None:
            envelope["worker_session_id"] = session
        validate_record(envelope, self.root.tenant_id, frozenset({"message_envelope"}), integrity=False)
        delivery_claim = None
        if claim is not None:
            delivery_claim = self._delivery_claim(
                claim, envelope_id=str(envelope["id"]), envelope_sha=sha
            )
        payload_fields = (
            "sender", "recipient", "repo", "sha", "doc", "note", "reply_to",
            "worker_session_id",
        )
        for record in prior_events:
            if record.get("idempotency_key") != key:
                continue
            prior_claim = next(
                (
                    row for row in prior_events
                    if row.get("kind") == "delivery_claim"
                    and row.get("note_ref") == record.get("id")
                ),
                None,
            )
            if (
                all(record.get(field) == envelope.get(field) for field in payload_fields)
                and record.get("attempt_binding", "absent_legacy")
                == envelope["attempt_binding"]
                and (
                    (delivery_claim is None and prior_claim is None)
                    or (
                        delivery_claim is not None
                        and prior_claim is not None
                        and self._claim_semantics(prior_claim)
                        == self._claim_semantics(delivery_claim)
                    )
                )
            ):
                return (
                    record
                    if prior_claim is None
                    else {"message": record, "claim": prior_claim}
                )
            break
        self.registry.require_protocol_lease(sender, now=now, act="send by sender")
        self.registry.require_protocol_lease(recipient, now=now, act="send to recipient")

        def decide(
            records: list[Dict[str, object]],
        ) -> tuple[Dict[str, object], Sequence[Dict[str, object]]]:
            self._validate_event_records(records)
            for record in records:
                if record.get("idempotency_key") != key:
                    continue
                prior_claim = next(
                    (
                        row for row in records
                        if row.get("kind") == "delivery_claim"
                        and row.get("note_ref") == record.get("id")
                    ),
                    None,
                )
                if (
                    all(record.get(field) == envelope.get(field) for field in payload_fields)
                    and record.get("attempt_binding", "absent_legacy")
                    == envelope["attempt_binding"]
                    and (
                        (delivery_claim is None and prior_claim is None)
                        or (
                            delivery_claim is not None
                            and prior_claim is not None
                            and self._claim_semantics(prior_claim)
                            == self._claim_semantics(delivery_claim)
                        )
                    )
                ):
                    return (
                        record
                        if prior_claim is None
                        else {"message": record, "claim": prior_claim}
                    ), ()
                raise ProtocolRefusal("idempotency_conflict", "idempotency key has different content")
            if delivery_claim is None:
                return envelope, (envelope,)
            result = {"message": envelope, "claim": delivery_claim}
            return result, (envelope, delivery_claim)

        try:
            return transact_records(
                self.root,
                self.relative_path,
                decide,
                allowed_kinds=EVENT_KINDS,
            )
        except ProtocolRefusal as exc:
            if exc.code == "idempotency_conflict":
                self._deny(sender, recipient, "idempotency_conflict")
            raise

    def retract(
        self,
        retracted_message_id: str,
        *,
        worker_session_id: str,
        reason: str,
        author: str,
    ) -> Dict[str, object]:
        """Coordinate retraction with any wake decision for its exact namespace."""
        original = next(
            (row for row in self.event_records() if row["kind"] == "message_envelope" and row["id"] == retracted_message_id),
            None,
        )
        if original is None:
            return self._retract_already_guarded(retracted_message_id, worker_session_id=worker_session_id, reason=reason, author=author)
        from .wake_hold import wake_coordination_guard
        with wake_coordination_guard(self.root, str(original["recipient"]), worker_session_id=original.get("worker_session_id")):
            return self._retract_already_guarded(retracted_message_id, worker_session_id=worker_session_id, reason=reason, author=author)

    def _retract_already_guarded(
        self,
        retracted_message_id: str,
        *,
        worker_session_id: str,
        reason: str,
        author: str,
    ) -> Dict[str, object]:
        """Append the reviewer's exact retraction record without deleting its original frame."""

        retraction: Dict[str, object] = {
            "schema_version": 0,
            "id": "ret-" + uuid7_hex(),
            "tenant_id": self.root.tenant_id,
            "timestamp": utc_now(),
            "kind": "message_retracted",
            "retracted_message_id": retracted_message_id,
            "worker_session_id": worker_session_id,
            "reason": reason,
            "author": author,
        }
        validate_record(
            retraction,
            self.root.tenant_id,
            frozenset({"message_retracted"}),
            integrity=False,
        )

        def decide(
            records: list[Dict[str, object]],
        ) -> tuple[Dict[str, object], Optional[Dict[str, object]]]:
            self._validate_event_records(records)
            prior_by_id: Dict[str, Dict[str, object]] = {}
            for record in records:
                if record["kind"] == "message_envelope":
                    prior_by_id[str(record["id"])] = record
                    continue
                if record["retracted_message_id"] == retracted_message_id:
                    if (
                        record["worker_session_id"] == worker_session_id
                        and record["reason"] == reason
                        and record["author"] == author
                    ):
                        return record, None
                    raise ProtocolRefusal(
                        "message_retraction_duplicate",
                        "a message already has an append-only retraction",
                    )
            original = prior_by_id.get(retracted_message_id)
            if original is None:
                raise ProtocolRefusal(
                    "message_retraction_unknown",
                    "retraction must name an existing message",
                )
            if original.get("worker_session_id") != worker_session_id:
                raise ProtocolRefusal(
                    "message_retraction_session_invalid",
                    "retraction worker session must equal its original message session",
                )
            if author not in {original["sender"], original["recipient"]}:
                raise ProtocolRefusal(
                    "message_retraction_party_invalid",
                    "retraction author must be an original message party",
                )
            return retraction, retraction

        return transact(self.root, self.relative_path, decide, allowed_kinds=EVENT_KINDS)

    def present(
        self,
        recipient: str,
        limit: int = MAX_PRESENTATION_ITEMS,
        *,
        worker_session_id: Optional[str] = None,
        now: Optional[datetime] = None,
    ) -> Tuple[List[Dict[str, object]], Optional[Dict[str, object]]]:
        """Present under the same recipient/session guard as wake evaluation."""
        recipient = self.registry.resolve_node_id(recipient, field="recipient")
        from .wake_hold import wake_coordination_guard
        with wake_coordination_guard(self.root, recipient, worker_session_id=worker_session_id):
            return self._present_already_guarded(
                recipient,
                limit,
                worker_session_id=worker_session_id,
                now=now,
            )

    def _present_already_guarded(
        self,
        recipient: str,
        limit: int = MAX_PRESENTATION_ITEMS,
        *,
        worker_session_id: Optional[str] = None,
        now: Optional[datetime] = None,
    ) -> Tuple[List[Dict[str, object]], Optional[Dict[str, object]]]:
        recipient = validate_identifier(recipient, "recipient")
        self.registry.require_active(recipient)
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= MAX_PRESENTATION_ITEMS:
            raise ProtocolRefusal("presentation_limit_invalid", "presentation limit must be 1 through 1000")
        if worker_session_id is not None:
            self._session_component(worker_session_id)
            messages, _payload = self._present_full_scan(
                recipient, limit, worker_session_id=worker_session_id
            )
        elif any(
            record["kind"] == "message_retracted" for record in self.event_records()
        ):
            # A snapshot containing only envelope tails cannot prove a newly
            # appended retraction; use the same full projection as sessions.
            messages, _payload = self._present_full_scan(recipient, limit)
        else:
            store = None
            try:
                store = self._inbox_snapshot_store(recipient, limit)
                loaded = store.load()
                messages = self._messages_from_snapshot(
                    recipient, limit, loaded.payload, loaded.tails.get("events.jsonl", ())
                )
            except SnapshotRefusal:
                before_scan = None
                if store is not None:
                    try:
                        before_scan = store.capture()
                    except SnapshotRefusal:
                        pass
                messages, payload = self._present_full_scan(recipient, limit)
                if store is not None and before_scan is not None:
                    try:
                        store.refresh(payload, expected=before_scan)
                    except SnapshotRefusal:
                        pass
        if not messages:
            return [], None
        self.registry.require_protocol_lease(
            recipient, now=now, act="fresh delivery to recipient"
        )
        relative = self._delivery_relative_path(
            recipient, worker_session_id=worker_session_id
        )
        def decide(prior: list[Dict[str, object]]) -> tuple[Dict[str, object], Dict[str, object]]:
            receipt: Dict[str, object] = {
                "schema_version": 0,
                "id": "delivery-" + uuid7_hex(),
                "tenant_id": self.root.tenant_id,
                "timestamp": utc_now(),
                "kind": "delivery_receipt",
                "recipient": recipient,
                "item_ids": [message["id"] for message in messages],
                "presentation_count": len(prior) + 1,
            }
            return receipt, receipt
        receipt = transact(self.root, relative, decide, allowed_kinds=set(WAKE_HOLD_KINDS))
        return messages, receipt

    def stale_send_projection(self) -> List[Dict[str, object]]:
        """Project dead-holder sends from evidence only; this method never renews a lease."""

        projections: List[Dict[str, object]] = []
        for physical_frame, record in enumerate(self.event_records(), 1):
            if record["kind"] != "message_envelope":
                continue
            binding = record.get("attempt_binding")
            if not isinstance(binding, dict):
                continue
            lease_id = binding.get("lease_id")
            if not isinstance(lease_id, str):
                continue
            lease_state = self._dead_holder_state(lease_id)
            if lease_state is None:
                continue
            projections.append(
                {
                    "state": "stale_send",
                    "message_id": record["id"],
                    "holder_lease_id": lease_id,
                    "lease_state_at_projection": lease_state,
                    "physical_frame": physical_frame,
                    "renews": False,
                }
            )
        return projections

    def _dead_holder_state(self, lease_id: str) -> Optional[str]:
        """Map only durable authority/orphan evidence to the reviewer's closed stale states."""

        try:
            subject = validate_identifier(lease_id, "lease_id")
        except ProtocolRefusal:
            return None
        grants = read_records_snapshot(
            self.root,
            Path("authority-grants") / f"{subject}.jsonl",
            allowed_kinds={"authority_grant"},
        )
        if grants:
            latest_state = grants[-1].get("state")
            if latest_state == "expired":
                return "expired"
            if latest_state == "released":
                return "abandoned"
        run_records = read_records_snapshot(
            self.root,
            Path("runs/events.jsonl"),
            allowed_kinds=RUN_KINDS,
        )
        matching = [
            record
            for record in run_records
            if record["kind"] == "supervisor_orphaned"
            and record.get("lease_id") == lease_id
        ]
        if not matching:
            return None
        orphan_class = matching[-1].get("orphan_class")
        if orphan_class == "lease_abandonment":
            return "abandoned"
        if orphan_class in {"owner_loss", "unregister"}:
            return "dead_holder"
        return None

    def _inbox_snapshot_store(self, recipient: str, limit: int) -> SnapshotStore:
        return SnapshotStore(
            self.root,
            reader="inbox",
            key=f"{recipient}:{limit}",
            discover_sources=lambda: (
                SourceSpec(self.relative_path, frozenset({"message_envelope"})),
            ),
        )

    def _present_full_scan(
        self, recipient: str, limit: int, *, worker_session_id: Optional[str] = None
    ) -> tuple[List[Dict[str, object]], Dict[str, object]]:
        from .cursor import SparseCursor

        frames = self.event_records()
        events = [record for record in frames if record["kind"] == "message_envelope"]
        retracted_ids = {
            str(record["retracted_message_id"])
            for record in frames
            if record["kind"] == "message_retracted"
        }
        delivered, acknowledged = SparseCursor(self.root).state_for(
            recipient,
            events,
            worker_session_id=worker_session_id,
            retracted_ids=retracted_ids,
        )
        matching = [
            record
            for record in events
            if record.get("recipient") == recipient
            and record.get("worker_session_id") == worker_session_id
            and record["id"] not in retracted_ids
            and record["id"] not in acknowledged
        ]
        messages = matching[:limit]
        payload: Dict[str, object] = {
            "recipient": recipient,
            "limit": limit,
            "messages": messages,
            "truncated": len(matching) > limit,
            "delivered_ids": sorted(delivered),
            "acked_ids": sorted(acknowledged),
        }
        if worker_session_id is not None:
            payload["worker_session_id"] = worker_session_id
        return messages, payload

    def _messages_from_snapshot(
        self,
        recipient: str,
        limit: int,
        payload: Dict[str, object],
        event_tail: Sequence[Dict[str, object]],
    ) -> List[Dict[str, object]]:
        fields = {
            "recipient",
            "limit",
            "messages",
            "truncated",
            "delivered_ids",
            "acked_ids",
        }
        if set(payload) != fields or payload.get("recipient") != recipient or payload.get("limit") != limit:
            raise SnapshotRefusal(
                "snapshot_payload_invalid", "inbox snapshot parameters do not match"
            )
        raw_messages = payload["messages"]
        raw_delivered = payload["delivered_ids"]
        raw_acked = payload["acked_ids"]
        truncated = payload["truncated"]
        if (
            not isinstance(raw_messages, list)
            or not all(isinstance(row, dict) for row in raw_messages)
            or not isinstance(raw_delivered, list)
            or not all(isinstance(item, str) for item in raw_delivered)
            or not isinstance(raw_acked, list)
            or not all(isinstance(item, str) for item in raw_acked)
            or not isinstance(truncated, bool)
        ):
            raise SnapshotRefusal(
                "snapshot_payload_invalid", "inbox snapshot payload is malformed"
            )
        messages = [dict(row) for row in raw_messages]
        delivered = set(raw_delivered)
        acknowledged = set(raw_acked)
        known = {str(row.get("id")) for row in messages}

        delivery_records = read_records(
            self.root,
            self._delivery_relative_path(recipient),
            allowed_kinds=set(WAKE_HOLD_KINDS),
        )
        for receipt in delivery_records:
            if receipt.get("recipient") != recipient:
                raise IntegrityFailure(
                    "delivery_evidence_invalid",
                    "delivery belongs to another recipient",
                )
            for item_id in receipt["item_ids"]:
                if item_id not in delivered and item_id not in known:
                    raise SnapshotRefusal(
                        "snapshot_tail_history_required",
                        "delivery references an event outside the retained inbox window",
                    )
                delivered.add(str(item_id))

        from .cursor import SparseCursor

        ack_records = read_records(
            self.root,
            SparseCursor(self.root)._relative_path_for(recipient),
            allowed_kinds={"ack_receipt"},
        )
        for receipt in ack_records:
            if receipt.get("recipient") != recipient:
                raise IntegrityFailure(
                    "ack_evidence_invalid",
                    "ack receipt belongs to another contract",
                )
            for item_id in receipt["item_ids"]:
                if item_id not in delivered:
                    raise IntegrityFailure(
                        "ack_evidence_invalid",
                        "ack does not correspond to delivered recipient evidence",
                    )
                acknowledged.add(str(item_id))

        retained_before_ack = len(messages)
        messages = [row for row in messages if row["id"] not in acknowledged]
        if len(messages) != retained_before_ack and truncated:
            raise SnapshotRefusal(
                "snapshot_tail_history_required",
                "acknowledgment exposed a truncated inbox window",
            )

        for record in event_tail:
            if record.get("reply_to") is not None:
                raise SnapshotRefusal(
                    "snapshot_tail_history_required",
                    "reply tail needs omitted causal history",
                )
            if record.get("recipient") != recipient or record["id"] in acknowledged:
                continue
            if len(messages) < limit:
                messages.append(dict(record))
                known.add(str(record["id"]))
            else:
                truncated = True
        return messages[:limit]

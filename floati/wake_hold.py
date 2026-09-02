"""Closed wake-hold testimony and side-effect-free inbox replay."""

from __future__ import annotations

import re
import sys
import unicodedata
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterator, List, Mapping, Optional, Sequence, Tuple

from .bus_epoch import (
    LOCK_ORDER_WAKE,
    epoch_guard,
    lock_order_guard,
    shared_epoch_operation,
)
from .errors import IntegrityFailure, ProtocolRefusal
from .ids import uuid7_hex
from .jsonl import (
    VerifiedLedgerCursor,
    WAKE_HOLD_DELIVERY_DOMAIN,
    _RETIRED_NAME,
    _locked_path,
    _transact_wake_hold_records,
    read_records,
    read_records_snapshot,
    read_records_with_prefix_digests,
    transact,
)
from .records import (
    WAKE_ATTEMPT_REFUSED_REASONS,
    WAKE_HOLD_KINDS,
    validate_record,
    wake_hold_decision_digest,
)
from .registry import Registry, utc_now
from .root import FloatiRoot, validate_identifier


@dataclass(frozen=True)
class WakeItemState:
    message: Mapping[str, object]
    state: str
    presentation_count: int


def wake_hold_receipt(
    *, tenant_id: str, recipient: str, worker_session_id: Optional[str],
    idempotency_key: str, limit: int, item_ids: Sequence[str],
    event_prefix_digest: str, delivery_prefix_digest: str,
    acknowledgment_prefix_digest: str, now: str, record_id: Optional[str] = None,
) -> Dict[str, object]:
    """Build one exact closed receipt; validation remains the durable boundary."""

    row: Dict[str, object] = {
        "schema_version": 1,
        "id": record_id or "wake-hold-" + uuid7_hex(),
        "tenant_id": tenant_id,
        "timestamp": now,
        "kind": "wake_hold_receipt",
        "recipient": recipient,
        "worker_session_id": worker_session_id,
        "idempotency_key": idempotency_key,
        "limit": limit,
        "item_ids": list(item_ids),
        "event_prefix_digest": event_prefix_digest,
        "delivery_prefix_digest": delivery_prefix_digest,
        "acknowledgment_prefix_digest": acknowledgment_prefix_digest,
    }
    row["decision_digest"] = wake_hold_decision_digest(row)
    return row


class WakeHoldLedger:
    """Path helper retained for compatibility; public writes have no authority."""

    def __init__(self, root: FloatiRoot, recipient: str, *, worker_session_id: Optional[str] = None) -> None:
        self.root = root
        self.recipient = validate_identifier(recipient, "recipient")
        self.worker_session_id = worker_session_id
        from .cursor import SparseCursor
        self.relative_path = SparseCursor(root)._delivery_relative_path_for(
            self.recipient, worker_session_id=worker_session_id,
        )

    def append(self, record: Dict[str, object]) -> Dict[str, object]:
        """Refuse: only the controller's private transaction may append."""
        raise ProtocolRefusal("wake_controller_only", "wake hold testimony requires the controller transaction")


class WakeAttemptLedger:
    """Durably record the external prompt outcome without implying acknowledgment."""

    _KINDS = frozenset({"wake_attempt_receipt"})

    def __init__(self, root: FloatiRoot) -> None:
        self.root = root

    @shared_epoch_operation
    def record(
        self,
        *,
        recipient: str,
        acting_session_id: str,
        item_ids: Sequence[str],
        decision_receipt_id: Optional[str],
        message_worker_session_id: Optional[str],
        idempotency_key: str,
        outcome: str,
        reason_code: Optional[str] = None,
    ) -> Dict[str, object]:
        from .cursor import SparseCursor
        from .events import EVENT_KINDS, validate_event_records

        node = Registry(self.root).resolve_node_id(recipient, field="recipient")
        SparseCursor._session_component(acting_session_id)
        if message_worker_session_id is not None:
            SparseCursor._session_component(message_worker_session_id)
        key = _validate_wake_key(idempotency_key)
        if outcome not in {"woke", "refused"}:
            raise ProtocolRefusal("wake_outcome_invalid", "wake outcome must be woke or refused")
        if (
            not isinstance(item_ids, (list, tuple)) or not 1 <= len(item_ids) <= 1000
            or not all(isinstance(item, str) and item for item in item_ids)
            or len(set(item_ids)) != len(item_ids)
        ):
            raise ProtocolRefusal("item_ids_invalid", "wake attempt needs unique message ids")
        requested = list(item_ids)

        events = read_records(self.root, "events.jsonl", allowed_kinds=EVENT_KINDS)
        validate_event_records(events)
        envelopes = {
            str(row["id"]): row for row in events if row["kind"] == "message_envelope"
        }
        ordered = [str(row["id"]) for row in events if row["kind"] == "message_envelope"]
        recorded_outcome = outcome
        recorded_reason = reason_code
        selected = [envelopes.get(item_id) for item_id in requested]
        missing = {item_id for item_id in requested if item_id not in envelopes}
        if missing:
            from .snapshot import _owned_epoch_archives

            for archive in _owned_epoch_archives(self.root):
                archive_relative = archive.relative_to(self.root.tenant_home)
                archived_events = read_records_snapshot(
                    self.root,
                    archive_relative / "events.jsonl",
                    allowed_kinds=set(EVENT_KINDS),
                )
                if any(
                    row.get("kind") == "message_envelope"
                    and row.get("id") in missing
                    for row in archived_events
                ):
                    raise ProtocolRefusal(
                        "wake_envelope_archived",
                        "wake envelope moved to an owned epoch archive",
                    )
        owned = (
            requested == [item_id for item_id in ordered if item_id in set(requested)]
            and all(
                row is not None
                and row.get("recipient") == node
                and row.get("worker_session_id") == message_worker_session_id
                for row in selected
            )
            and (
                message_worker_session_id is None
                or message_worker_session_id == acting_session_id
            )
        )
        if not owned:
            recorded_outcome = "refused"
            recorded_reason = "wake_envelope_not_owned"

        if recorded_outcome == "woke":
            delivery_path = SparseCursor(self.root)._delivery_relative_path_for(
                node, worker_session_id=message_worker_session_id,
            )
            deliveries = read_records(
                self.root, delivery_path,
                allowed_kinds={"delivery_receipt", "wake_hold_receipt"},
            )
            matching = [
                row for row in deliveries
                if row.get("kind") == "wake_hold_receipt"
                and row.get("id") == decision_receipt_id
            ]
            if not matching:
                recorded_outcome = "refused"
                recorded_reason = "wake_decision_missing"
            elif matching[0].get("item_ids") != requested:
                recorded_outcome = "refused"
                recorded_reason = "wake_decision_mismatch"

        if recorded_outcome == "refused" and recorded_reason not in WAKE_ATTEMPT_REFUSED_REASONS:
            recorded_reason = "wake_prompt_failed"
        row: Dict[str, object] = {
            "schema_version": 1,
            "id": "wake-attempt-" + uuid7_hex(),
            "tenant_id": self.root.tenant_id,
            "timestamp": utc_now(),
            "kind": "wake_attempt_receipt",
            "node_id": node,
            "acting_session_id": acting_session_id,
            "message_worker_session_id": message_worker_session_id,
            "idempotency_key": key,
            "item_ids": requested,
            "decision_receipt_id": decision_receipt_id,
            "outcome": recorded_outcome,
            "reason_code": recorded_reason,
        }
        validate_record(row, self.root.tenant_id, self._KINDS, integrity=False)
        relative = Path("receipts/wakes") / f"{node}.jsonl"

        def decide(prior: List[Dict[str, object]]) -> Tuple[Dict[str, object], Optional[Dict[str, object]]]:
            for existing in prior:
                if existing.get("idempotency_key") != key:
                    continue
                semantic_fields = (
                    "node_id", "acting_session_id", "message_worker_session_id",
                    "item_ids", "decision_receipt_id", "outcome", "reason_code",
                )
                if all(existing.get(field) == row.get(field) for field in semantic_fields):
                    return existing, None
                raise ProtocolRefusal(
                    "wake_attempt_idempotency_conflict",
                    "wake attempt key has different evidence",
                )
            return row, row

        with wake_coordination_guard(self.root, node):
            durable = transact(
                self.root, relative, decide, allowed_kinds=self._KINDS,
            )
        if durable["outcome"] == "refused":
            raise ProtocolRefusal(str(durable["reason_code"]), "wake attempt refused")
        return durable


def _append_controller_receipt(
    controller: "WakeHoldController", record: Dict[str, object],
) -> Dict[str, object]:
    """Legacy private name is deliberately refusal-only; naming is not authority."""
    raise ProtocolRefusal("wake_controller_only", "wake hold testimony requires original evaluate provenance")


def _unavailable(detail: str) -> IntegrityFailure:
    return IntegrityFailure("consumption_state_unavailable", detail)


@contextmanager
def wake_coordination_guard(
    root: FloatiRoot, recipient: str, *, worker_session_id: Optional[str] = None,
) -> Iterator[None]:
    """Serialize every session for one registered seat without storing truth."""

    with lock_order_guard(root, LOCK_ORDER_WAKE, label="wake"):
        node = Registry(root).resolve_node_id(recipient, field="recipient")
        if worker_session_id is not None:
            from .cursor import SparseCursor
            SparseCursor._session_component(worker_session_id)
        lock_relative = Path("receipts/wake-coordination") / node / "lane.lock"
        lock = root.resolve_relative(lock_relative)
        with _locked_path(
            lock, exclusive=True, relative=lock_relative, timeout_seconds=5.0
        ):
            yield


def _validate_wake_key(key: object) -> str:
    if (
        not isinstance(key, str) or not 1 <= len(key) <= 128
        or any(unicodedata.category(char) in {"Cc", "Cs"} or unicodedata.bidirectional(char) in {"LRE", "RLE", "LRO", "RLO", "PDF", "LRI", "RLI", "FSI", "PDI", "BN"} for char in key)
    ):
        raise ProtocolRefusal("wake_idempotency_key_invalid", "wake idempotency key is terminal-unsafe or out of bounds")
    return key


def project_wake_items(
    *, events: Sequence[Mapping[str, object]], deliveries: Sequence[Mapping[str, object]],
    acknowledgments: Sequence[Mapping[str, object]], recipient: str,
    worker_session_id: Optional[str], tenant_id: str, event_prefix_digests: Sequence[str],
    delivery_prefix_digests: Sequence[str], acknowledgment_prefix_digests: Sequence[str],
) -> Tuple[WakeItemState, ...]:
    """Purely project message state from validated physical input order.

    The controller provides byte-prefix validation separately.  This function
    deliberately does not read or write a root and therefore cannot hide a
    cursor or alter acknowledgment state.
    """

    try:
        node = validate_identifier(recipient, "recipient")
        tenant = validate_identifier(tenant_id, "tenant")
    except ProtocolRefusal as exc:
        raise _unavailable("recipient is invalid") from exc
    if (
        not event_prefix_digests or not delivery_prefix_digests or not acknowledgment_prefix_digests
        or not all(isinstance(value, str) for value in event_prefix_digests)
        or not all(isinstance(value, str) for value in delivery_prefix_digests)
        or not all(isinstance(value, str) for value in acknowledgment_prefix_digests)
        or len(event_prefix_digests) != len(events) + 1
        or len(delivery_prefix_digests) != len(deliveries) + 1
        or len(acknowledgment_prefix_digests) != len(acknowledgments) + 1
    ):
        raise _unavailable("validated raw prefix testimony is unavailable")
    validated_events: List[Dict[str, object]] = []
    for event in events:
        try:
            validated_events.append(validate_record(
                dict(event) if isinstance(event, Mapping) else event,
                tenant,
                frozenset({
                    "message_envelope", "message_retracted",
                    "bus_epoch_roll_receipt",
                    "ledger_repair_receipt",
                }),
                integrity=True,
            ))
        except (IntegrityFailure, ProtocolRefusal, KeyError, TypeError, ValueError) as exc:
            raise _unavailable("event or retraction testimony is malformed") from exc
    try:
        from .events import validate_event_records
        validate_event_records(validated_events)
    except IntegrityFailure as exc:
        raise _unavailable("event or retraction testimony is semantically invalid") from exc
    event_by_id: Dict[str, Mapping[str, object]] = {}
    ordered: List[str] = []
    retracted = set()
    for validated_event in validated_events:
        kind = validated_event["kind"]
        if kind == "message_envelope":
            item_id = validated_event["id"]
            if not isinstance(item_id, str) or item_id in event_by_id:
                raise _unavailable("events have duplicate or invalid message ids")
            event_by_id[item_id] = validated_event
            ordered.append(item_id)
        elif kind == "message_retracted":
            item_id = validated_event["retracted_message_id"]
            if not isinstance(item_id, str) or item_id not in event_by_id or item_id in retracted:
                raise _unavailable("retractions do not follow one current message")
            retracted.add(item_id)
        elif kind in {"bus_epoch_roll_receipt", "ledger_repair_receipt"}:
            continue
        else:
            raise _unavailable("event prefix contains an unknown kind")
    matching = [
        item_id for item_id in ordered
        if event_by_id[item_id].get("recipient") == node
        and event_by_id[item_id].get("worker_session_id") == worker_session_id
    ]
    matching_set = set(matching)
    counts = {item_id: 0 for item_id in matching}
    presented = set()
    seen_receipts = set()
    for index, receipt in enumerate(deliveries):
        kind = receipt.get("kind")
        if kind not in WAKE_HOLD_KINDS or receipt.get("recipient") != node:
            raise _unavailable("delivery ledger crosses its recipient contract")
        if kind == "wake_hold_receipt" and receipt.get("worker_session_id") != worker_session_id:
            raise _unavailable("hold receipt crosses its worker session")
        ids = receipt.get("item_ids")
        if not isinstance(ids, list) or (kind != "delivery_receipt" and not ids) or len(set(ids)) != len(ids):
            raise _unavailable("delivery receipt item ids are invalid")
        if any(not isinstance(item_id, str) or item_id not in matching_set for item_id in ids):
            raise _unavailable("delivery receipt names a foreign or absent message")
        physical = [item_id for item_id in matching if item_id in ids]
        if list(ids) != physical:
            raise _unavailable("delivery receipt item ids are not physical event order")
        receipt_id = receipt.get("id")
        if receipt_id is not None:
            if not isinstance(receipt_id, str) or receipt_id in seen_receipts:
                raise _unavailable("delivery ledger reuses a record id")
            seen_receipts.add(receipt_id)
        if kind == "wake_hold_receipt":
            try:
                validate_record(dict(receipt), tenant, frozenset({"wake_hold_receipt"}), integrity=True)
            except (IntegrityFailure, ProtocolRefusal, KeyError, TypeError, ValueError) as exc:
                raise _unavailable("wake hold testimony is malformed") from exc
            if receipt.get("decision_digest") != wake_hold_decision_digest(receipt):
                raise _unavailable("wake hold decision digest was forged")
            if receipt.get("delivery_prefix_digest") != delivery_prefix_digests[index]:
                raise _unavailable("hold receipt does not bind its immediate delivery prefix")
            if receipt.get("event_prefix_digest") not in event_prefix_digests:
                raise _unavailable("hold event prefix is not an ancestor")
            latest_named_frame = max(
                index for index, event in enumerate(events, start=1)
                if event.get("kind") == "message_envelope" and event.get("id") in ids
            )
            covered_positions = [
                position for position, digest in enumerate(event_prefix_digests)
                if digest == receipt.get("event_prefix_digest")
            ]
            if not any(position >= latest_named_frame for position in covered_positions):
                raise _unavailable("hold event prefix does not cover every named event")
            if receipt.get("acknowledgment_prefix_digest") not in acknowledgment_prefix_digests:
                raise _unavailable("hold acknowledgment prefix is not an ancestor")
        for item_id in ids:
            counts[item_id] += 1
            presented.add(item_id)
    acknowledged = set()
    for receipt in acknowledgments:
        if receipt.get("kind") != "ack_receipt" or receipt.get("recipient") != node:
            raise _unavailable("acknowledgment ledger crosses its recipient contract")
        ids = receipt.get("item_ids")
        if not isinstance(ids, list) or not ids or len(set(ids)) != len(ids):
            raise _unavailable("acknowledgment item ids are invalid")
        for item_id in ids:
            if not isinstance(item_id, str) or item_id not in presented:
                raise _unavailable("acknowledgment names an unpresented item")
            acknowledged.add(item_id)
    result = []
    for item_id in matching:
        event = event_by_id[item_id]
        if item_id in retracted:
            state = "retracted"
        elif item_id in acknowledged:
            state = "acknowledged"
        elif item_id in presented:
            state = "held"
        else:
            state = "fresh"
        result.append(WakeItemState(event, state, counts[item_id]))
    return tuple(result)


def validate_wake_decision_artifact(
    artifact: Mapping[str, object], *, tenant_id: str,
) -> Dict[str, object]:
    """Validate the closed Task-2 decision shape without performing a decision."""

    fields = {
        "schema_version", "artifact_version", "kind", "state", "wake_required",
        "recipient", "worker_session_id", "limit", "fresh_total", "held_total",
        "fresh_truncated", "held_truncated", "fresh_messages", "held_items", "receipt",
        "event_prefix_digest", "delivery_prefix_digest", "acknowledgment_prefix_digest",
    }
    if not isinstance(artifact, dict) or set(artifact) != fields:
        raise ProtocolRefusal("wake_decision_artifact_invalid", "artifact has an open or incomplete shape")
    if artifact["schema_version"] != 1 or artifact["artifact_version"] != 1 or artifact["kind"] != "wake_decision":
        raise ProtocolRefusal("wake_decision_artifact_invalid", "artifact version or kind is invalid")
    if artifact["state"] not in {"fresh_work", "held_only", "caught_up"} or not isinstance(artifact["wake_required"], bool):
        raise ProtocolRefusal("wake_decision_artifact_invalid", "artifact state/wake fields are invalid")
    try:
        validate_identifier(artifact["recipient"], "recipient")
        tenant = validate_identifier(tenant_id, "tenant")
    except ProtocolRefusal as exc:
        raise ProtocolRefusal("wake_decision_artifact_invalid", "artifact recipient is invalid") from exc
    if artifact["worker_session_id"] is not None and (
        not isinstance(artifact["worker_session_id"], str) or not artifact["worker_session_id"]
        or len(artifact["worker_session_id"]) > 512
        or any(unicodedata.category(char) in {"Cc", "Cs"} or unicodedata.bidirectional(char) in {"LRE", "RLE", "LRO", "RLO", "PDF", "LRI", "RLI", "FSI", "PDI", "BN"} for char in artifact["worker_session_id"])
    ):
        raise ProtocolRefusal("wake_decision_artifact_invalid", "artifact session is invalid")
    if not isinstance(artifact["limit"], int) or isinstance(artifact["limit"], bool) or not 1 <= artifact["limit"] <= 1000:
        raise ProtocolRefusal("wake_decision_artifact_invalid", "artifact limit is invalid")
    for field in ("fresh_total", "held_total"):
        if not isinstance(artifact[field], int) or isinstance(artifact[field], bool) or not 0 <= artifact[field] <= 100000:
            raise ProtocolRefusal("wake_decision_artifact_invalid", "artifact totals are invalid")
    if not all(isinstance(artifact[field], bool) for field in ("fresh_truncated", "held_truncated")):
        raise ProtocolRefusal("wake_decision_artifact_invalid", "artifact truncation flags are invalid")
    if not all(isinstance(artifact[field], str) and re.fullmatch(r"[0-9a-f]{64}", artifact[field]) for field in ("event_prefix_digest", "delivery_prefix_digest", "acknowledgment_prefix_digest")):
        raise ProtocolRefusal("wake_decision_artifact_invalid", "artifact prefix testimony is invalid")
    fresh, held, receipt = artifact["fresh_messages"], artifact["held_items"], artifact["receipt"]
    if not isinstance(fresh, list) or not isinstance(held, list):
        raise ProtocolRefusal("wake_decision_artifact_invalid", "artifact item collections are invalid")
    if len(fresh) > 1000 or len(held) > 1000 or len(fresh) > artifact["limit"] or len(held) > artifact["limit"]:
        raise ProtocolRefusal("wake_decision_artifact_invalid", "artifact item collections exceed the declared limit")
    try:
        for row in fresh:
            validate_record(dict(row), tenant, frozenset({"message_envelope"}), integrity=False)
    except (ProtocolRefusal, KeyError, TypeError, ValueError) as exc:
        raise ProtocolRefusal("wake_decision_artifact_invalid", "artifact fresh message is invalid") from exc
    fresh_ids = [row.get("id") for row in fresh if isinstance(row, dict)]
    if len(fresh_ids) != len(fresh) or len(set(fresh_ids)) != len(fresh_ids):
        raise ProtocolRefusal("wake_decision_artifact_invalid", "artifact fresh message ids are invalid")
    if any(not isinstance(row, dict) or set(row) != {"item_id", "presentation_count"} or not isinstance(row["item_id"], str) or re.fullmatch(r"msg-[0-9a-f]{12}7[0-9a-f]{3}[89ab][0-9a-f]{15}", row["item_id"]) is None or not isinstance(row["presentation_count"], int) or isinstance(row["presentation_count"], bool) or row["presentation_count"] < 1 for row in held):
        raise ProtocolRefusal("wake_decision_artifact_invalid", "artifact item shape is invalid")
    held_ids = [row["item_id"] for row in held]
    if len(set(held_ids)) != len(held_ids) or set(fresh_ids) & set(held_ids):
        raise ProtocolRefusal("wake_decision_artifact_invalid", "artifact pending item ids overlap or repeat")
    if artifact["fresh_total"] < len(fresh) or artifact["held_total"] < len(held):
        raise ProtocolRefusal("wake_decision_artifact_invalid", "artifact totals are below returned slices")
    if artifact["fresh_truncated"] != (artifact["fresh_total"] > len(fresh)) or artifact["held_truncated"] != (artifact["held_total"] > len(held)):
        raise ProtocolRefusal("wake_decision_artifact_invalid", "artifact truncation flags disagree with totals")
    fresh_state = artifact["state"] == "fresh_work"
    if fresh_state != artifact["wake_required"] or fresh_state != (receipt is not None) or fresh_state != bool(fresh):
        raise ProtocolRefusal("wake_decision_artifact_invalid", "artifact state/wake/receipt fields disagree")
    if not fresh_state and receipt is not None:
        raise ProtocolRefusal("wake_decision_artifact_invalid", "silent artifact cannot expose a receipt")
    if artifact["state"] == "held_only" and (fresh or not held or artifact["fresh_total"] != 0):
        raise ProtocolRefusal("wake_decision_artifact_invalid", "held-only artifact has inconsistent message slices")
    if artifact["state"] == "caught_up" and (
        fresh or held or artifact["fresh_total"] != 0 or artifact["held_total"] != 0
        or artifact["fresh_truncated"] or artifact["held_truncated"]
    ):
        raise ProtocolRefusal("wake_decision_artifact_invalid", "caught-up artifact has pending message slices")
    if receipt is not None:
        try:
            validate_record(dict(receipt), tenant, frozenset({"wake_hold_receipt"}), integrity=False)
        except (ProtocolRefusal, KeyError, TypeError, ValueError) as exc:
            raise ProtocolRefusal("wake_decision_artifact_invalid", "artifact receipt is invalid") from exc
        if (
            receipt.get("recipient") != artifact["recipient"]
            or receipt.get("worker_session_id") != artifact["worker_session_id"]
            or receipt.get("limit") != artifact["limit"]
            or receipt.get("item_ids") != fresh_ids
            or receipt.get("event_prefix_digest") != artifact["event_prefix_digest"]
            or receipt.get("delivery_prefix_digest") != artifact["delivery_prefix_digest"]
            or receipt.get("acknowledgment_prefix_digest") != artifact["acknowledgment_prefix_digest"]
        ):
            raise ProtocolRefusal("wake_decision_artifact_invalid", "artifact receipt does not bind its fresh decision")
    return dict(artifact)


class WakeHoldController:
    """One guarded read/decision/append transaction for non-waking held work."""

    # Salts inside sha256 preimages, not copy: each one prefixes a prefix
    # digest this controller compares against one a caller read earlier, so
    # their bytes are a compatibility contract with every wake-hold ledger
    # already on disk. The delivery domain is IMPORTED rather than repeated --
    # it is also the preimage prefix in floati/jsonl.py, and it used to be two
    # literals in two files that happened to agree. The other two are built
    # here from the same hex-built name, which is retired everywhere a reader
    # can see it and kept exactly here, where only a hash can.
    # See floati/jsonl.py for why the name is built rather than spelled;
    # tests/test_retired_name_pins.py pins all three values.
    _EVENT_DOMAIN = _RETIRED_NAME + "-wake-hold-events-v1"
    _DELIVERY_DOMAIN = WAKE_HOLD_DELIVERY_DOMAIN
    _ACK_DOMAIN = _RETIRED_NAME + "-wake-hold-acknowledgments-v1"

    def __init__(self, root: FloatiRoot) -> None:
        if not isinstance(root, FloatiRoot):
            raise ProtocolRefusal("root_required", "wake controller requires a validated writable root")
        self.root = root
        self._prefix_cursors: Dict[
            Tuple[str, str, Optional[str]], VerifiedLedgerCursor
        ] = {}

    def _prefix_cursor(
        self, plane: str, recipient: str, worker_session_id: Optional[str],
    ) -> VerifiedLedgerCursor:
        key = (plane, recipient, worker_session_id)
        cursor = self._prefix_cursors.get(key)
        if cursor is None:
            cursor = VerifiedLedgerCursor()
            self._prefix_cursors[key] = cursor
        return cursor

    def _read(
        self, recipient: str, worker_session_id: Optional[str],
    ) -> Tuple[List[Dict[str, object]], Tuple[str, ...], List[Dict[str, object]], Tuple[str, ...], List[Dict[str, object]], Tuple[str, ...]]:
        from .cursor import SparseCursor
        from .events import EVENT_KINDS, validate_event_records

        cursor = SparseCursor(self.root)
        try:
            events, event_prefixes = read_records_with_prefix_digests(
                self.root, "events.jsonl", allowed_kinds=set(EVENT_KINDS), domain=self._EVENT_DOMAIN,
                cursor=self._prefix_cursor("events", "", None),
            )
            validate_event_records(events)
            deliveries, delivery_prefixes = read_records_with_prefix_digests(
                self.root, cursor._delivery_relative_path_for(recipient, worker_session_id=worker_session_id),
                allowed_kinds=set(WAKE_HOLD_KINDS), domain=self._DELIVERY_DOMAIN,
                cursor=self._prefix_cursor("deliveries", recipient, worker_session_id),
            )
            acknowledgments, acknowledgment_prefixes = read_records_with_prefix_digests(
                self.root, cursor._relative_path_for(recipient, worker_session_id=worker_session_id),
                allowed_kinds={"ack_receipt"}, domain=self._ACK_DOMAIN,
                cursor=self._prefix_cursor("acknowledgments", recipient, worker_session_id),
            )
        except (IntegrityFailure, ProtocolRefusal, KeyError, TypeError, ValueError) as exc:
            if isinstance(exc, IntegrityFailure) and exc.code == "consumption_state_unavailable":
                raise
            raise _unavailable("wake consumption prefix is invalid") from exc
        return events, event_prefixes, deliveries, delivery_prefixes, acknowledgments, acknowledgment_prefixes

    def _append_receipt_already_guarded(
        self, recipient: str, worker_session_id: Optional[str], record: Dict[str, object],
    ) -> Dict[str, object]:
        """Enter the sealed JSONL writer only from this class's original evaluate body."""

        try:
            caller = sys._getframe(1)
        except ValueError:
            caller = None
        if (
            caller is None
            or caller.f_code is not _WAKE_HOLD_EVALUATE_CODE
            or caller.f_globals is not _WAKE_HOLD_CONTROLLER_GLOBALS
            or caller.f_locals.get("self") is not self
            or type(self) is not WakeHoldController
            or self.root is not caller.f_locals.get("self").root
            or caller.f_locals.get("node") != recipient
            or caller.f_locals.get("worker_session_id") != worker_session_id
            or caller.f_locals.get("receipt") is not record
            or not isinstance(record, dict)
            or record.get("kind") != "wake_hold_receipt"
            or record.get("recipient") != recipient
            or record.get("worker_session_id") != worker_session_id
        ):
            raise ProtocolRefusal("wake_controller_only", "wake hold append lacks original evaluate provenance")
        from .cursor import SparseCursor
        relative = SparseCursor(self.root)._delivery_relative_path_for(
            recipient, worker_session_id=worker_session_id,
        )

        def decide(prior: List[Dict[str, object]]) -> Tuple[Dict[str, object], Dict[str, object]]:
            return record, record

        return _transact_wake_hold_records(
            self.root, relative, decide,
            expected_prefix_digest=str(record.get("delivery_prefix_digest")),
        )

    @staticmethod
    def _artifact(
        *, tenant_id: str, recipient: str, worker_session_id: Optional[str], limit: int,
        fresh: Sequence[WakeItemState], held: Sequence[WakeItemState], receipt: Optional[Dict[str, object]],
        event_digest: str, delivery_digest: str, acknowledgment_digest: str,
    ) -> Dict[str, object]:
        fresh_rows = [dict(item.message) for item in fresh[:limit]]
        held_rows = [{"item_id": str(item.message["id"]), "presentation_count": item.presentation_count} for item in held[:limit]]
        state = "fresh_work" if fresh_rows else ("held_only" if held_rows else "caught_up")
        artifact: Dict[str, object] = {
            "schema_version": 1, "artifact_version": 1, "kind": "wake_decision",
            "state": state, "wake_required": state == "fresh_work", "recipient": recipient,
            "worker_session_id": worker_session_id, "limit": limit,
            "fresh_total": len(fresh), "held_total": len(held),
            "fresh_truncated": len(fresh) > len(fresh_rows), "held_truncated": len(held) > len(held_rows),
            "fresh_messages": fresh_rows, "held_items": held_rows,
            "receipt": receipt if state == "fresh_work" else None,
            "event_prefix_digest": event_digest, "delivery_prefix_digest": delivery_digest,
            "acknowledgment_prefix_digest": acknowledgment_digest,
        }
        return validate_wake_decision_artifact(artifact, tenant_id=tenant_id)

    def evaluate(
        self, recipient: str, *, idempotency_key: str, worker_session_id: Optional[str] = None,
        limit: int = 1000,
    ) -> Dict[str, object]:
        if globals() is not _WAKE_HOLD_CONTROLLER_GLOBALS:
            raise ProtocolRefusal(
                "wake_controller_only",
                "wake evaluation requires its original module globals",
            )
        with epoch_guard(self.root, exclusive=False):
            return self._evaluate_already_guarded(
                recipient,
                idempotency_key=idempotency_key,
                worker_session_id=worker_session_id,
                limit=limit,
            )

    def _evaluate_already_guarded(
        self, recipient: str, *, idempotency_key: str, worker_session_id: Optional[str] = None,
        limit: int = 1000,
    ) -> Dict[str, object]:
        node = Registry(self.root).resolve_node_id(recipient, field="recipient")
        if worker_session_id is not None:
            from .cursor import SparseCursor
            SparseCursor._session_component(worker_session_id)
        key = _validate_wake_key(idempotency_key)
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 1000:
            raise ProtocolRefusal("presentation_limit_invalid", "presentation limit must be 1 through 1000")
        with wake_coordination_guard(self.root, node, worker_session_id=worker_session_id):
            events, event_prefixes, deliveries, delivery_prefixes, acknowledgments, acknowledgment_prefixes = self._read(node, worker_session_id)
            states = project_wake_items(
                events=events, deliveries=deliveries, acknowledgments=acknowledgments,
                recipient=node, worker_session_id=worker_session_id, tenant_id=self.root.tenant_id,
                event_prefix_digests=event_prefixes, delivery_prefix_digests=delivery_prefixes,
                acknowledgment_prefix_digests=acknowledgment_prefixes,
            )
            prior = [row for row in deliveries if row.get("kind") == "wake_hold_receipt" and row.get("idempotency_key") == key]
            if len(prior) > 1:
                raise _unavailable("duplicate wake idempotency receipt")
            fresh = [item for item in states if item.state == "fresh"]
            held = [item for item in states if item.state == "held"]
            if prior:
                receipt = prior[0]
                if receipt.get("recipient") != node or receipt.get("worker_session_id") != worker_session_id or receipt.get("limit") != limit:
                    raise ProtocolRefusal("wake_idempotency_conflict", "wake retry has different namespace or limit")
                ids = receipt.get("item_ids")
                selected = [item for item in states if item.message.get("id") in ids]
                if not isinstance(ids, list) or [item.message.get("id") for item in selected] != ids:
                    raise _unavailable("wake receipt item testimony is no longer present")
                if all(item.state == "held" for item in selected):
                    try:
                        event_count = event_prefixes.index(str(receipt["event_prefix_digest"]))
                        delivery_count = delivery_prefixes.index(str(receipt["delivery_prefix_digest"]))
                        acknowledgment_count = acknowledgment_prefixes.index(str(receipt["acknowledgment_prefix_digest"]))
                        original_states = project_wake_items(
                            events=events[:event_count], deliveries=deliveries[:delivery_count],
                            acknowledgments=acknowledgments[:acknowledgment_count], recipient=node,
                            worker_session_id=worker_session_id, tenant_id=self.root.tenant_id,
                            event_prefix_digests=event_prefixes[:event_count + 1],
                            delivery_prefix_digests=delivery_prefixes[:delivery_count + 1],
                            acknowledgment_prefix_digests=acknowledgment_prefixes[:acknowledgment_count + 1],
                        )
                    except (IntegrityFailure, ValueError, KeyError, TypeError) as exc:
                        raise _unavailable("wake retry ancestors cannot reconstruct the original decision") from exc
                    original_fresh = [item for item in original_states if item.state == "fresh"]
                    original_held = [item for item in original_states if item.state == "held"]
                    if [item.message.get("id") for item in original_fresh[:limit]] != ids:
                        raise _unavailable("wake retry receipt differs from its original semantic decision")
                    return self._artifact(
                        tenant_id=self.root.tenant_id, recipient=node, worker_session_id=worker_session_id, limit=limit,
                        fresh=original_fresh, held=original_held, receipt=receipt,
                        event_digest=str(receipt["event_prefix_digest"]), delivery_digest=str(receipt["delivery_prefix_digest"]),
                        acknowledgment_digest=str(receipt["acknowledgment_prefix_digest"]),
                    )
                return self._artifact(
                    tenant_id=self.root.tenant_id, recipient=node, worker_session_id=worker_session_id, limit=limit,
                    fresh=[], held=held, receipt=None, event_digest=event_prefixes[-1],
                    delivery_digest=delivery_prefixes[-1], acknowledgment_digest=acknowledgment_prefixes[-1],
                )
            chosen = fresh[:limit]
            if not chosen:
                return self._artifact(
                    tenant_id=self.root.tenant_id, recipient=node, worker_session_id=worker_session_id, limit=limit, fresh=[], held=held,
                    receipt=None, event_digest=event_prefixes[-1], delivery_digest=delivery_prefixes[-1],
                    acknowledgment_digest=acknowledgment_prefixes[-1],
                )
            receipt = wake_hold_receipt(
                tenant_id=self.root.tenant_id, recipient=node, worker_session_id=worker_session_id,
                idempotency_key=key, limit=limit, item_ids=[str(item.message["id"]) for item in chosen],
                event_prefix_digest=event_prefixes[-1], delivery_prefix_digest=delivery_prefixes[-1],
                acknowledgment_prefix_digest=acknowledgment_prefixes[-1],
                now=utc_now(),
            )
            self._append_receipt_already_guarded(node, worker_session_id, receipt)
            return self._artifact(
                tenant_id=self.root.tenant_id, recipient=node, worker_session_id=worker_session_id, limit=limit,
                fresh=fresh, held=held, receipt=receipt, event_digest=event_prefixes[-1],
                delivery_digest=delivery_prefixes[-1], acknowledgment_digest=acknowledgment_prefixes[-1],
            )


_WAKE_HOLD_EVALUATE_CODE = WakeHoldController._evaluate_already_guarded.__code__
_WAKE_HOLD_PRIVATE_APPEND_CODE = WakeHoldController._append_receipt_already_guarded.__code__
_WAKE_HOLD_CONTROLLER_GLOBALS = WakeHoldController.evaluate.__globals__

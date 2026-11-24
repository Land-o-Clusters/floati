"""Closed wake-hold testimony and side-effect-free inbox replay."""

from __future__ import annotations

import base64
import re
import sys
import unicodedata
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, Iterator, List, Mapping, Optional, Sequence, Tuple

from .bus_epoch import (
    LOCK_ORDER_WAKE,
    epoch_guard,
    lock_order_guard,
    shared_epoch_operation,
)
from .errors import IntegrityFailure, ProtocolRefusal
from .events import EVENT_KINDS as _EVENT_KINDS
from .ids import uuid7_hex
from .inbound_hold import withhold_unapproved_states
from .jsonl import (
    VerifiedLedgerCursor,
    WAKE_HOLD_DELIVERY_DOMAIN,
    _RETIRED_NAME,
    _locked_path,
    _transact_wake_hold_records,
    read_records,
    read_records_compatible,
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
    holder_released: bool = False,
    inbound_hold_mask: Optional[str] = None,
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
    if holder_released:
        row.update(schema_version=2, selection_reason="holder_released")
    if inbound_hold_mask is not None:
        row.update(schema_version=4, inbound_hold_mask=inbound_hold_mask)
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
        from .send_prefix import event_references

        node = Registry(self.root).resolve_node_id(recipient, field="recipient")
        SparseCursor._session_component(acting_session_id)
        if message_worker_session_id is not None:
            SparseCursor._session_component(message_worker_session_id)
        key = _validate_wake_key(idempotency_key)
        if outcome not in {"woke", "queued", "refused"}:
            raise ProtocolRefusal("wake_outcome_invalid", "wake outcome must be woke, queued, or refused")
        if (
            not isinstance(item_ids, (list, tuple)) or not 1 <= len(item_ids) <= 1000
            or not all(isinstance(item, str) and item for item in item_ids)
            or len(set(item_ids)) != len(item_ids)
        ):
            raise ProtocolRefusal("item_ids_invalid", "wake attempt needs unique message ids")
        requested = list(item_ids)

        events, _retracted, _proof = event_references(
            self.root, requested, physical_order=True, optimistic=True)
        envelopes = {
            str(row["id"]): row for row in events if row["kind"] == "message_envelope"
        }
        ordered = [str(row["id"]) for row in events if row["kind"] == "message_envelope"]
        recorded_outcome = outcome
        recorded_reason = reason_code
        selected = [envelopes.get(item_id) for item_id in requested]
        missing = frozenset(item_id for item_id in requested if item_id not in envelopes)
        if missing:
            from .epoch_history import archived_envelope_membership

            if archived_envelope_membership(self.root, missing):
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

        if recorded_outcome in {"woke", "queued"}:
            if isinstance(decision_receipt_id, str) and decision_receipt_id.startswith("cursor-presentation-"):
                from .cursor_presentations import CursorPresentationLedger

                # The guarded reader proves the complete native prepare/flush
                # history, epoch prefixes and exact delivery reproduction.
                matching = [row for row in CursorPresentationLedger(self.root).read(node)
                    if row["id"] == decision_receipt_id and row["phase"] == "flushed"]
                if not matching:
                    recorded_outcome = "refused"
                    recorded_reason = "wake_decision_missing"
                elif (
                    message_worker_session_id is not None
                    or matching[0]["node_id"] != node
                    or matching[0]["acting_session_id"] != acting_session_id
                    or matching[0]["item_ids"] != requested
                ):
                    recorded_outcome = "refused"
                    recorded_reason = "wake_decision_mismatch"
            else:
                delivery_path = SparseCursor(self.root)._delivery_relative_path_for(
                    node, worker_session_id=message_worker_session_id,
                )
                deliveries = read_records(
                    self.root, delivery_path,
                    allowed_kinds=set(WAKE_HOLD_KINDS),
                )
                matching = [
                    row for row in deliveries
                    if row.get("kind") == "wake_hold_receipt"
                    and row.get("id") == decision_receipt_id
                ]
                if not matching:
                    from .epoch_carry import original_carried_hold
                    for carried in deliveries:
                        if (carried.get("kind") != "wake_hold_receipt"
                                or carried.get("schema_version") != 3
                                or carried.get("recipient") != node
                                or carried.get("worker_session_id") != message_worker_session_id
                                or carried.get("item_ids") != requested):
                            continue
                        original, _before, _states = original_carried_hold(
                            self.root, carried, node, message_worker_session_id)
                        if original["id"] == decision_receipt_id:
                            matching = [original]
                            break
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


def _claim_holder_released(root: FloatiRoot, node: str) -> bool:
    """True only when the seat's recorded claim holder is proven gone.

    The witness is the waiter's own holder testimony (pid and measured
    process start); absent or malformed testimony proves nothing and keeps
    the conservative held classification.
    """

    from .codex_wait_liveness import classify_holder, read_holder_testimony

    return classify_holder(read_holder_testimony(root, node)) == "released"


def _wake_candidates(states):
    from .mail_health import wake_eligible

    fresh = [item for item in states if item.state == "fresh" and wake_eligible(item.message)]
    held = [item for item in states if item.state == "held" and wake_eligible(item.message)]
    return fresh, held


def _excluded_mask(candidates, eligible):
    """Record the hold policy's exclusions over the exact native candidate order."""
    if len(candidates) == len(eligible):
        return None
    kept = {item.message["id"] for item in eligible}
    mask = bytearray((len(candidates) + 7) // 8)
    for index, item in enumerate(candidates):
        if item.message["id"] not in kept:
            mask[index // 8] |= 1 << (index % 8)
    return base64.b64encode(mask).decode("ascii")


def original_wake_selection(states, receipt):
    """Reproduce a recorded native selection without consulting live holders.

    A v2 decision authorizes only the original held-only branch. Its exact
    ordered IDs and limit still have to reproduce; no caller gets new work.
    Version-one receipts retain the historical fresh-only interpretation.
    """
    fresh, held = _wake_candidates(states)
    if receipt.get("schema_version") == 3:
        # The roll admitted the original decision before minting this row.
        # Its new cut can include later mail and terminal siblings. Replaying
        # selection there would invent a different decision; retain the exact
        # attested IDs, in their native event order, without consulting archives.
        selected = [item for item in states if item.message['id'] in receipt['item_ids']]
        if [item.message['id'] for item in selected] != receipt['item_ids']:
            raise _unavailable("carried hold differs from its roll-attested selection")
        return selected, [item for item in held if item.message['id'] not in receipt['item_ids']]
    if receipt.get("schema_version") == 4:
        validate_record(dict(receipt), str(receipt["tenant_id"]), {"wake_hold_receipt"}, integrity=True)
        candidates = fresh + held
        mask = base64.b64decode(receipt["inbound_hold_mask"])
        count = len(candidates)
        if len(mask) != (count + 7) // 8 or not any(mask) or (
                count % 8 and mask[-1] >> (count % 8)):
            raise _unavailable("recorded inbound hold mask differs from its native candidate cut")
        eligible = [item for index, item in enumerate(candidates)
                    if not mask[index // 8] & (1 << (index % 8))]
        fresh = [item for item in eligible if item.state == "fresh"]
        held = [item for item in eligible if item.state == "held"]
    if receipt.get("selection_reason") == "holder_released":
        validate_record(dict(receipt), str(receipt["tenant_id"]), {"wake_hold_receipt"}, integrity=True)
        if fresh or [item.message["id"] for item in held[:receipt["limit"]]] != receipt["item_ids"]:
            raise _unavailable("recorded holder release differs from its original held-only selection")
        return held, []
    return fresh, held


#: The event ledger has ONE permitted vocabulary and it belongs to the writer.
#: This is deliberately the writer's own set, imported, rather than a second
#: hand-written copy: on 2026-08-29 the copy here omitted ``delivery_claim`` —
#: a kind ``floati send --claim`` legally appends — and every wake cycle for
#: every node on that root refused ``consumption_state_unavailable`` until the
#: whole bus epoch was rolled. A reader that re-states its ledger's vocabulary
#: can drift from it; one that imports it cannot.
WAKE_EVENT_KINDS = frozenset(_EVENT_KINDS)

#: Kinds the projection validates but does not itself interpret. Every event
#: kind that is neither an envelope nor a retraction lives here by derivation,
#: so a kind added to the writer is carried, never silently refused.
_UNINTERPRETED_EVENT_KINDS = WAKE_EVENT_KINDS - {"message_envelope", "message_retracted"}

_EVENT_LEDGER = "events.jsonl"
_QUARANTINE_VERB = (
    "floati repair quarantine --root ROOT --ledger events.jsonl "
    "--record-id {record_id} --idempotency-key KEY"
)
_CITABLE_RECORD_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")


def _citable_record_id(record: object) -> Optional[str]:
    """Return the record's id only when it is safe to print in a refusal.

    A malformed record's id is untrusted input on its way to a terminal, so a
    non-string, over-long, or non-lexical id is cited by position alone.
    """

    value = record.get("id") if isinstance(record, Mapping) else None
    if isinstance(value, str) and _CITABLE_RECORD_ID.fullmatch(value) is not None:
        return value
    return None


def _malformed_event_detail(index: int, record: object, code: object) -> str:
    """Name the ledger, the physical position, the record, and the one remedy.

    An anonymous "testimony is malformed" cost this project a bus epoch: the
    two offending lines were unfindable, so the whole ledger was archived
    instead of the two records being quarantined.
    """

    record_id = _citable_record_id(record)
    named = f" id {record_id}" if record_id else ""
    remedy = _QUARANTINE_VERB.format(record_id=record_id or "ID")
    return (
        f"{_EVENT_LEDGER} record {index + 1}{named} is malformed"
        f" ({code}); quarantine that one record: {remedy}"
    )


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
            lock, exclusive=True, relative=lock_relative, timeout_seconds=5.0,
            # The enclosing scope tracks this physical lock at WAKE rank.
            # Treating it as a JSONL ledger would reject the next seat's
            # wake guard when an epoch roll coordinates several recipients.
            order_tracked=False,
        ):
            yield


def _has_rolled_wake_history(root):
    from .bus_epoch import _physical_first_roll_receipt
    from .errors import FloatiError

    try:
        return _physical_first_roll_receipt(root.resolve_relative("events.jsonl"), root.tenant_id) is not None
    except (FloatiError, OSError, AttributeError, TypeError):
        # The native reader owns diagnostics for malformed or unavailable heads.
        return False


class _RolledWakeProjection:
    """Read a rolled cut under the epoch guard and commit it under its lane."""

    def __init__(self, root, node, worker_session_id, lane, extra=()):
        from .codex_wait_liveness import holder_testimony_relative
        from .cursor import SparseCursor
        from .inbound_hold import LEDGER

        cursor = SparseCursor(root)
        self.root, self.node, self.worker_session_id, self.lane = root, node, worker_session_id, lane
        self.coordinates = (
            Path("events.jsonl"),
            cursor._delivery_relative_path_for(node, worker_session_id=worker_session_id),
            cursor._relative_path_for(node, worker_session_id=worker_session_id),
            LEDGER / (node + ".jsonl"),
            holder_testimony_relative(node),
            *extra,
        )
        self.cut = self._cut()
        lane.close()
        self.lane_held = False

    def _cut(self):
        from .send_prefix import _identity

        return tuple(_identity(self.root.resolve_relative(path)) for path in self.coordinates)

    def revalidate(self, acquire_native=None):
        if self.cut is not None:
            self.lane.enter_context(wake_coordination_guard(
                self.root, self.node, worker_session_id=self.worker_session_id))
            self.lane_held = True
            if acquire_native is not None:
                acquire_native()
            if self._cut() != self.cut:
                raise _unavailable("native wake inputs changed during rolled projection")
            self.cut = None


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
    _hold_proofs=None,
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
    from .cut_prefix import CertifiedEventSource, CertifiedPrefixes
    certified = type(events) is CertifiedEventSource
    if _hold_proofs is not None:
        from .epoch_carry import verify_hold_proof_source
        verify_hold_proof_source(_hold_proofs,
            (events, event_prefix_digests, deliveries, delivery_prefix_digests,
             acknowledgments, acknowledgment_prefix_digests), node, worker_session_id, tenant)
    if (
        not event_prefix_digests or not delivery_prefix_digests or not acknowledgment_prefix_digests
        or (not certified and not all(isinstance(value, str) for value in event_prefix_digests))
        or not all(isinstance(value, str) for value in delivery_prefix_digests)
        or not all(isinstance(value, str) for value in acknowledgment_prefix_digests)
        or len(event_prefix_digests) != len(events) + 1
        or len(delivery_prefix_digests) != len(deliveries) + 1
        or len(acknowledgment_prefix_digests) != len(acknowledgments) + 1
    ):
        raise _unavailable("validated raw prefix testimony is unavailable")
    if certified:
        if (events.root.tenant_id != tenant or type(event_prefix_digests) is not CertifiedPrefixes
                or event_prefix_digests.source is not events):
            raise _unavailable("validated raw prefix testimony is unavailable")
        selected = events.matching(node, worker_session_id)
        event_by_id = {row["id"]: row for _ordinal, row, _retracted in selected}
        event_positions = {row["id"]: ordinal for ordinal, row, _retracted in selected}
        matching = [row["id"] for _ordinal, row, _retracted in selected]
        retracted = {row["id"] for _ordinal, row, removed in selected if removed}
        class CutPositions:
            def __contains__(self, digest):
                return events.cut_position(digest) is not None
            def __getitem__(self, digest):
                value = events.cut_position(digest)
                if value is None:
                    raise KeyError(digest)
                return value
        event_prefix_positions = CutPositions()
    else:
        event_by_id, event_positions, matching, retracted = _native_wake_events(
            events, node, worker_session_id, tenant)
        # Keep the last occurrence for compatible native skipped-kind sources.
        event_prefix_positions = {digest: position
            for position, digest in enumerate(event_prefix_digests)}
    matching_set = set(matching)
    acknowledgment_prefix_set = set(acknowledgment_prefix_digests)
    counts = {item_id: 0 for item_id in matching}
    delivered = set()
    presented = set()
    seen_receipts = set()
    return _project_wake_receipts(events, deliveries, acknowledgments, node, worker_session_id,
        tenant, event_prefix_digests, delivery_prefix_digests, acknowledgment_prefix_digests,
        _hold_proofs, event_by_id, event_positions, matching, retracted, matching_set,
        event_prefix_positions, acknowledgment_prefix_set, counts, delivered, presented, seen_receipts)


def _native_wake_events(events, node, worker_session_id, tenant):
    """The original whole-source validation, unchanged on every cache miss."""
    validated_events: List[Dict[str, object]] = []
    for index, event in enumerate(events):
        try:
            validated_events.append(validate_record(
                (event.copy() if type(event) is dict
                 else dict(event) if isinstance(event, Mapping) else event),
                tenant,
                WAKE_EVENT_KINDS,
                integrity=True,
            ))
        except (IntegrityFailure, ProtocolRefusal, KeyError, TypeError, ValueError) as exc:
            raise _unavailable(
                _malformed_event_detail(
                    index, event, getattr(exc, "code", type(exc).__name__)
                )
            ) from exc
    try:
        from .events import validate_event_records
        validate_event_records(validated_events)
    except IntegrityFailure as exc:
        raise _unavailable(
            f"{_EVENT_LEDGER} testimony is semantically invalid"
            f" ({getattr(exc, 'code', type(exc).__name__)})"
        ) from exc
    event_by_id: Dict[str, Mapping[str, object]] = {}
    event_positions: Dict[str, int] = {}
    matching: List[str] = []
    retracted = set()
    for index, validated_event in enumerate(validated_events):
        kind = validated_event["kind"]
        if kind == "message_envelope":
            item_id = validated_event["id"]
            if not isinstance(item_id, str) or item_id in event_by_id:
                raise _unavailable("events have duplicate or invalid message ids")
            event_by_id[item_id] = validated_event
            event_positions[item_id] = index + 1
            if (validated_event.get("recipient") == node
                    and validated_event.get("worker_session_id") == worker_session_id):
                matching.append(item_id)
        elif kind == "message_retracted":
            item_id = validated_event["retracted_message_id"]
            if not isinstance(item_id, str) or item_id not in event_by_id or item_id in retracted:
                raise _unavailable("retractions do not follow one current message")
            retracted.add(item_id)
        elif kind in _UNINTERPRETED_EVENT_KINDS:
            continue
        else:
            raise _unavailable(
                _malformed_event_detail(index, validated_event, "record_kind_invalid")
            )
    return event_by_id, event_positions, matching, retracted


def _project_wake_receipts(events, deliveries, acknowledgments, node, worker_session_id,
        tenant, event_prefix_digests, delivery_prefix_digests, acknowledgment_prefix_digests,
        _hold_proofs, event_by_id, event_positions, matching, retracted, matching_set,
        event_prefix_positions, acknowledgment_prefix_set, counts, delivered, presented, seen_receipts):
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
        # Membership and uniqueness were established above. Increasing native
        # event ordinals reproduce the physical-order filter without rescanning
        # every message for each receipt.
        if any(event_positions[left] >= event_positions[right] for left, right in zip(ids, ids[1:])):
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
            if receipt.get("schema_version") != 3 and receipt.get("decision_digest") != wake_hold_decision_digest(receipt):
                raise _unavailable("wake hold decision digest was forged")
            if receipt.get("schema_version") == 3 and (not events
                    or events[0].get('kind') != 'bus_epoch_roll_receipt'
                    or receipt['carried_from']['roll_receipt_id'] != events[0]['id']):
                raise _unavailable("carried hold does not bind its native epoch roll")
            imported = _hold_proofs is not None and _hold_proofs.validates(receipt)
            if not imported and receipt.get("delivery_prefix_digest") != delivery_prefix_digests[index]:
                raise _unavailable("hold receipt does not bind its immediate delivery prefix")
            if not imported and receipt.get("event_prefix_digest") not in event_prefix_positions:
                raise _unavailable("hold event prefix is not an ancestor")
            if not imported and event_prefix_positions[receipt["event_prefix_digest"]] < max(
                    event_positions[item_id] for item_id in ids):
                raise _unavailable("hold event prefix does not cover every named event")
            if not imported and receipt.get("acknowledgment_prefix_digest") not in acknowledgment_prefix_set:
                raise _unavailable("hold acknowledgment prefix is not an ancestor")
        for item_id in ids:
            counts[item_id] += 1
            delivered.add(item_id)
            if kind != "delivered_unpresented":
                presented.add(item_id)
    acknowledged = set()
    for receipt in acknowledgments:
        if receipt.get("kind") != "ack_receipt" or receipt.get("recipient") != node:
            raise _unavailable("acknowledgment ledger crosses its recipient contract")
        ids = receipt.get("item_ids")
        if not isinstance(ids, list) or not ids or len(set(ids)) != len(ids):
            raise _unavailable("acknowledgment item ids are invalid")
        for item_id in ids:
            if not isinstance(item_id, str) or item_id not in delivered:
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
        self._certified_sources = {}

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
        self, recipient: str, worker_session_id: Optional[str], *, _certified=False,
        _before_native_read=None,
    ) -> Tuple[List[Dict[str, object]], Tuple[str, ...], List[Dict[str, object]], Tuple[str, ...], List[Dict[str, object]], Tuple[str, ...]]:
        from .cursor import SparseCursor
        from .events import EVENT_KINDS, validate_event_records
        from . import cut_prefix, send_prefix

        cursor = SparseCursor(self.root)
        try:
            planes = self._certified_sources.setdefault(recipient, {})
            previous = planes.get(worker_session_id, next(reversed(planes.values()), None))
            certified = (cut_prefix.certified_source(self.root, recipient, worker_session_id,
                previous=previous) if _certified else None)
            if certified is None and _before_native_read is not None:
                _before_native_read()
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
            if certified is not None:
                try:
                    # Current retractions have no certified historical ordinal.
                    # A retry needing an old cut therefore remains native.
                    if (any(removed for _ordinal, _row, removed in certified.rows)
                            and any(row.get("kind") == "wake_hold_receipt" for row in deliveries)):
                        raise send_prefix.CacheMiss()
                    certified.prepare_cuts(deliveries)
                except send_prefix._CACHE_ERRORS:
                    certified = None
            if certified is not None:
                planes[worker_session_id] = certified
                events, event_prefixes = certified, cut_prefix.CertifiedPrefixes(certified)
            else:
                before = send_prefix._identity(self.root.resolve_relative("events.jsonl"))
                events, event_prefixes = read_records_with_prefix_digests(
                    self.root, "events.jsonl", allowed_kinds=set(EVENT_KINDS), domain=self._EVENT_DOMAIN,
                    cursor=self._prefix_cursor("events", "", None), skip_unknown_kinds=True)
                validate_event_records(events)
                if _certified:
                    planes.pop(worker_session_id, None)
                    # A native fallback may bank exactly the cut it read. A
                    # concurrent append makes publication decline, never guess.
                    cut_prefix.warm(self.root, expected_identity=before)
        except (IntegrityFailure, ProtocolRefusal, KeyError, TypeError, ValueError) as exc:
            from .waiter_lock_retry import current_lock_window, transient_ledger_timeout
            if current_lock_window() is not None and transient_ledger_timeout(exc):
                raise
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
        _authorize: Optional[Callable[[], bool]] = None,
        _publish: Optional[Callable[[Dict[str, object]], None]] = None,
        _conversation: Optional[Callable[[], Dict[str, object]]] = None,
        _before_read: Optional[Callable[[], Optional[Dict[str, object]]]] = None,
        _waiter_read=None,
        _before_rolled_projection=None,
    ) -> Optional[Dict[str, object]]:
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
                _authorize=_authorize,
                _publish=_publish,
                _conversation=_conversation,
                _before_read=_before_read,
                _waiter_read=_waiter_read,
                _before_rolled_projection=_before_rolled_projection,
            )

    def _evaluate_already_guarded(
        self, recipient: str, *, idempotency_key: str, worker_session_id: Optional[str] = None,
        limit: int = 1000,
        _authorize: Optional[Callable[[], bool]] = None,
        _publish: Optional[Callable[[Dict[str, object]], None]] = None,
        _conversation: Optional[Callable[[], Dict[str, object]]] = None,
        _before_read: Optional[Callable[[], Optional[Dict[str, object]]]] = None,
        _waiter_read=None,
        _before_rolled_projection=None,
    ) -> Optional[Dict[str, object]]:
        node = Registry(self.root).resolve_node_id(recipient, field="recipient")
        if worker_session_id is not None:
            from .cursor import SparseCursor
            SparseCursor._session_component(worker_session_id)
        key = _validate_wake_key(idempotency_key)
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 1000:
            raise ProtocolRefusal("presentation_limit_invalid", "presentation limit must be 1 through 1000")
        projection = None

        def finish(artifact):
            if projection is not None:
                projection.revalidate()
            if _publish is not None:
                _publish(artifact)
            return artifact

        with ExitStack() as lane:
            lane.enter_context(wake_coordination_guard(self.root, node, worker_session_id=worker_session_id))
            # The private callbacks are synchronous: classification precedes
            # every read, and publication finishes before this physical guard
            # releases. Default evaluator calls keep the same artifact path.
            if _authorize is not None and not _authorize():
                return None
            if _before_read is not None:
                # Native Stop may quarantine an unresolved epoch without
                # interpreting selected frames. Authorization and both
                # physical guards still precede this private callback.
                quarantine = _before_read()
                if quarantine is not None:
                    return finish(quarantine)
            def begin_rolled_projection():
                nonlocal projection
                if _has_rolled_wake_history(self.root):
                    extra = _before_rolled_projection() if _before_rolled_projection is not None else ()
                    projection = _RolledWakeProjection(self.root, node, worker_session_id, lane, extra)

            if _conversation is None:
                # A certified unrolled cut already proves its first frame.
                # Avoid rereading the physical head when that bank answered.
                source = self._read(node, worker_session_id, _certified=True,
                    _before_native_read=begin_rolled_projection)
            else:
                begin_rolled_projection()
                native_read = lambda: self._read(node, worker_session_id, _certified=False)
                source = _waiter_read(native_read) if _waiter_read is not None else native_read()
            events, event_prefixes, deliveries, delivery_prefixes, acknowledgments, acknowledgment_prefixes = source
            if _conversation is not None:
                # Native Stop publication has its own conversation-bound
                # prepare/flush testimony. Default daemon/evaluator decisions
                # and the sealed wake-hold append remain unchanged.
                return finish(_conversation())
            from .epoch_carry import verified_hold_proofs
            from .cut_prefix import CertifiedEventSource, CertifiedPrefixes
            try:
                hold_proofs = None if type(events) is CertifiedEventSource else verified_hold_proofs(self.root,
                    (events, event_prefixes, deliveries, delivery_prefixes, acknowledgments, acknowledgment_prefixes),
                    node, worker_session_id)
            except (IntegrityFailure, ProtocolRefusal) as exc:
                # This public controller retains its native consumption-state
                # refusal taxonomy when an apparent import has no valid origin.
                # The pure resolver/planner still exposes strict carry errors.
                raise _unavailable("wake hold origin proof is unavailable") from exc
            states = project_wake_items(
                events=events, deliveries=deliveries, acknowledgments=acknowledgments,
                recipient=node, worker_session_id=worker_session_id, tenant_id=self.root.tenant_id,
                event_prefix_digests=event_prefixes, delivery_prefix_digests=delivery_prefixes,
                acknowledgment_prefix_digests=acknowledgment_prefixes,
                _hold_proofs=hold_proofs,
            )
            prior = [row for row in deliveries if row.get("kind") == "wake_hold_receipt" and row.get("idempotency_key") == key]
            if len(prior) > 1:
                raise _unavailable("duplicate wake idempotency receipt")
            fresh, held = _wake_candidates(states)
            candidates = fresh + held
            fresh, held = withhold_unapproved_states(self.root, node, fresh, held)
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
                        if receipt.get("schema_version") == 3:
                            from .epoch_carry import original_carried_hold
                            receipt, _original_source, original_states = original_carried_hold(
                                self.root, receipt, node, worker_session_id)
                        elif hold_proofs is not None and hold_proofs.validates(receipt):
                            _original_source, original_states = hold_proofs.original(receipt)
                        else:
                            event_count = event_prefixes.index(str(receipt["event_prefix_digest"]))
                            delivery_count = delivery_prefixes.index(str(receipt["delivery_prefix_digest"]))
                            acknowledgment_count = acknowledgment_prefixes.index(str(receipt["acknowledgment_prefix_digest"]))
                            if type(events) is CertifiedEventSource:
                                original_events = events.restricted(event_count, str(receipt["event_prefix_digest"]))
                                original_prefixes = CertifiedPrefixes(original_events)
                            else:
                                original_events, original_prefixes = events[:event_count], event_prefixes[:event_count + 1]
                            original_source = (original_events, original_prefixes,
                                deliveries[:delivery_count], delivery_prefixes[:delivery_count + 1],
                                acknowledgments[:acknowledgment_count], acknowledgment_prefixes[:acknowledgment_count + 1])
                            original_proofs = (None if type(events) is CertifiedEventSource else
                                verified_hold_proofs(self.root, original_source, node, worker_session_id))
                            original_states = project_wake_items(
                                events=original_source[0], deliveries=original_source[2], acknowledgments=original_source[4],
                                recipient=node, worker_session_id=worker_session_id, tenant_id=self.root.tenant_id,
                                event_prefix_digests=original_source[1], delivery_prefix_digests=original_source[3],
                                acknowledgment_prefix_digests=original_source[5], _hold_proofs=original_proofs)
                    except (IntegrityFailure, ValueError, KeyError, TypeError) as exc:
                        raise _unavailable("wake retry ancestors cannot reconstruct the original decision") from exc
                    original_fresh, original_held = original_wake_selection(original_states, receipt)
                    if [item.message.get("id") for item in original_fresh[:limit]] != ids:
                        raise _unavailable("wake retry receipt differs from its original semantic decision")
                    return finish(self._artifact(
                        tenant_id=self.root.tenant_id, recipient=node, worker_session_id=worker_session_id, limit=limit,
                        fresh=original_fresh, held=original_held, receipt=receipt,
                        event_digest=str(receipt["event_prefix_digest"]), delivery_digest=str(receipt["delivery_prefix_digest"]),
                        acknowledgment_digest=str(receipt["acknowledgment_prefix_digest"]),
                    ))
                return finish(self._artifact(
                    tenant_id=self.root.tenant_id, recipient=node, worker_session_id=worker_session_id, limit=limit,
                    fresh=[], held=held, receipt=None, event_digest=event_prefixes[-1],
                    delivery_digest=delivery_prefixes[-1], acknowledgment_digest=acknowledgment_prefixes[-1],
                ))
            chosen = fresh[:limit]
            inbound_hold_mask = _excluded_mask(candidates, fresh + held)
            holder_released = False
            if not chosen and held and _claim_holder_released(self.root, node):
                # WD-3: the participation claim holding this work recorded a
                # holder that is proven gone (pid and process start). A claim
                # with no observed path back to released deferred the wake
                # for minutes past its holder's death (fq-6-2026-09-08); the
                # first evaluation after proven death re-presents the work.
                # Every unprovable road stays on the held side.
                fresh = fresh + held
                held = []
                chosen = fresh[:limit]
                holder_released = True
            if not chosen:
                return finish(self._artifact(
                    tenant_id=self.root.tenant_id, recipient=node, worker_session_id=worker_session_id, limit=limit, fresh=[], held=held,
                    receipt=None, event_digest=event_prefixes[-1], delivery_digest=delivery_prefixes[-1],
                    acknowledgment_digest=acknowledgment_prefixes[-1],
                ))
            receipt = wake_hold_receipt(
                tenant_id=self.root.tenant_id, recipient=node, worker_session_id=worker_session_id,
                idempotency_key=key, limit=limit, item_ids=[str(item.message["id"]) for item in chosen],
                event_prefix_digest=event_prefixes[-1], delivery_prefix_digest=delivery_prefixes[-1],
                acknowledgment_prefix_digest=acknowledgment_prefixes[-1],
                now=utc_now(),
                holder_released=holder_released,
                inbound_hold_mask=inbound_hold_mask,
            )
            if projection is not None:
                projection.revalidate()
            self._append_receipt_already_guarded(node, worker_session_id, receipt)
            return finish(self._artifact(
                tenant_id=self.root.tenant_id, recipient=node, worker_session_id=worker_session_id, limit=limit,
                fresh=fresh, held=held, receipt=receipt, event_digest=event_prefixes[-1],
                delivery_digest=delivery_prefixes[-1], acknowledgment_digest=acknowledgment_prefixes[-1],
            ))


_WAKE_HOLD_EVALUATE_CODE = WakeHoldController._evaluate_already_guarded.__code__
_WAKE_HOLD_PRIVATE_APPEND_CODE = WakeHoldController._append_receipt_already_guarded.__code__
_WAKE_HOLD_CONTROLLER_GLOBALS = WakeHoldController.evaluate.__globals__

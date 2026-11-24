"""Pure consumption joins; output is DATA, not authority.

Epoch inputs are newest-first native source six-tuples. First-ACT candidates
require actual native projection replay of the selected exact source by the
caller. Supplying a matching source dictionary/tuple is never proof that replay
occurred. Work rows supplied here are historical DATA, never a live work claim.
No filesystem, publication, Path fallback, mutation or payload removal belongs
in this module. Existing native callers retain their live validity checks.
"""

from copy import deepcopy
import hashlib
from itertools import chain

from .errors import IntegrityFailure, ProtocolRefusal
from .framing import FrameError, encode_frame


MAX_EPOCHS = 100000
MAX_TOTAL_RECORDS = 1000000
MAX_AXIS_RECORDS = 100000
MAX_CANONICAL_BYTES = 128 * 8 * 1024 * 1024


def _invalid(detail):
    return IntegrityFailure("consumption_evidence_invalid", detail)


def _tenant(tenant, rows):
    if not isinstance(tenant, str) or not tenant:
        raise _invalid("consumption tenant is absent")
    if any(not isinstance(row, dict) or row.get("tenant_id") != tenant for row in rows):
        raise _invalid("consumption source belongs to another tenant")


def _ack_index(tenant, node, worker, events, deliveries, acknowledgments):
    """Exact original native matching rules; projection validity is external."""
    _tenant(tenant, chain(events, deliveries, acknowledgments))
    by_id = {row["id"]: row for row in events if row["kind"] == "message_envelope"}
    delivered = set()
    for delivery in deliveries:
        if delivery["kind"] not in {"delivery_receipt", "delivered_unpresented"}:
            continue
        if delivery["recipient"] != node:
            raise _invalid("delivery has another recipient")
        for item_id in delivery["item_ids"]:
            event = by_id.get(item_id)
            if event is None or event["recipient"] != node or event.get("worker_session_id") != worker:
                raise _invalid("delivery has no matching envelope plane")
            delivered.add(item_id)
    matching = {}
    for ack in acknowledgments:
        if ack["recipient"] != node:
            raise _invalid("ACK has another recipient")
        for item_id in ack["item_ids"]:
            if item_id not in delivered:
                raise _invalid("ACK has no matching delivery")
            matching.setdefault(item_id, []).append(ack)
    return matching


def ack_references(tenant, node, message, events, deliveries, acknowledgments):
    """Return detached matching ACK DATA over native-shape validated input.

    This is the join loop from MessageConsumption._ack_reference; it does not
    validate hold decisions or replace current native cursor.state_for.
    """
    _tenant(tenant, [message])
    return deepcopy(_ack_index(tenant, node, message.get("worker_session_id"), events,
                               deliveries, acknowledgments).get(message["id"], []))


def validate_consumed_envelope(tenant, node, receipt, message):
    _tenant(tenant, [receipt])
    if (not isinstance(message, dict) or message.get("tenant_id") != tenant
            or receipt["recipient"] != node or message["recipient"] != node
            or message["id"] != receipt["message_id"]
            or message.get("worker_session_id") != receipt["worker_session_id"]):
        raise _invalid("consumption has no matching current or owned archived envelope")


def _matches_ack(receipt, ack):
    items = ack.get("item_ids")
    return (ack.get("id") == receipt["ack_receipt_id"]
            and ack.get("recipient") == receipt["recipient"]
            and isinstance(items, (list, tuple)) and receipt["message_id"] in items)


def validate_consumed_ack(tenant, receipt, acknowledgments):
    _tenant(tenant, [receipt, *acknowledgments])
    if not any(_matches_ack(receipt, ack) for ack in acknowledgments):
        raise _invalid("consumption has no exact prior ACK reference")


def validate_consumed_work(tenant, node, receipt, work_item):
    _tenant(tenant, [receipt])
    if receipt["source"] == "work_complete" and (not isinstance(work_item, dict)
            or work_item.get("id") != receipt["work_item_id"] or work_item.get("holder") != node):
        raise _invalid("consumption has no matching claimed work holder")


def _consumed_join(tenant, node, receipt, message, acknowledgments, work_item=None):
    validate_consumed_envelope(tenant, node, receipt, message)
    validate_consumed_ack(tenant, receipt, acknowledgments)
    validate_consumed_work(tenant, node, receipt, work_item)
    ack = next(row for row in acknowledgments if _matches_ack(receipt, row))
    return {"receipt": receipt, "envelope": message, "ack_receipt": ack,
                     "matching_ack_receipts": acknowledgments,
                     "work_item": work_item if receipt["source"] == "work_complete" else None}


def validate_consumed_join(tenant, node, receipt, message, acknowledgments, work_item=None):
    """Detached standalone join; batch replay detaches once with shared memo."""
    return deepcopy(_consumed_join(tenant, node, receipt, message, acknowledgments, work_item))


def automatic_predecessor_open(frames):
    """Exactly the native automatic-only gate, with no age cutoff."""
    head = frames[0] if frames else None
    return head is not None and head.get("kind") == "bus_epoch_roll_receipt" and "automatic" in head


def _unsupported(detail):
    return ProtocolRefusal("archive_consumption_replay_unsupported", detail,
                           remedy="retain original epoch payload; consumption DATA exceeds supported replay bounds")


def _prepare(tenant, epochs, consumed_rows=(), work_rows=()):
    """Validate explicit records/prefix DATA once, never source ownership."""
    _tenant(tenant, ())
    from .consumption import WORK_KINDS
    from .events import EVENT_KINDS
    from .jsonl import MAX_RECORD_BYTES
    from .records import WAKE_HOLD_KINDS, validate_record
    from .wake_hold import WakeHoldController
    if not isinstance(epochs, (list, tuple)) or not epochs:
        raise _invalid("ordered source six-tuples must include the current epoch")
    if len(epochs) > MAX_EPOCHS:
        raise _unsupported("too many source epochs")
    domains = (WakeHoldController._EVENT_DOMAIN, WakeHoldController._DELIVERY_DOMAIN,
               WakeHoldController._ACK_DOMAIN)
    kinds = (EVENT_KINDS, WAKE_HOLD_KINDS, {"ack_receipt"})
    identities, total, total_bytes = {}, 0, 0

    def records(rows, allowed):
        nonlocal total, total_bytes
        if not isinstance(rows, (list, tuple)):
            raise _invalid("native record input must be a finite sequence")
        if len(rows) > MAX_AXIS_RECORDS:
            raise _unsupported("native record sequence exceeds supported count")
        result, raw, seen = [], [], set()
        for row in rows:
            total += 1
            if total > MAX_TOTAL_RECORDS:
                raise _unsupported("total native record count exceeds supported replay bound")
            validate_record(row, tenant, frozenset(allowed), integrity=True)
            try:
                frame = encode_frame(row)
            except (FrameError, RecursionError) as exc:
                raise _invalid("native record is not canonical JSON data") from exc
            if len(frame) > MAX_RECORD_BYTES:
                raise _unsupported("native frame exceeds supported record bytes")
            total_bytes += len(frame)
            if total_bytes > MAX_CANONICAL_BYTES:
                raise _unsupported("total canonical source bytes exceed supported replay bound")
            identity = row["id"]
            if identity in seen:
                raise _invalid("native record ID repeats inside one source axis")
            seen.add(identity)
            if identity in identities and identities[identity] != frame:
                raise _invalid("one native ID has conflicting canonical source bytes")
            identities[identity] = frame
            result.append(row)
            raw.append(frame)
        return result, raw

    sources, messages, first = [], [], {}
    for ordinal, source in enumerate(epochs):
        if not isinstance(source, (list, tuple)) or len(source) != 6:
            raise _invalid("epoch source does not have exactly six native axes")
        copied = []
        for axis in range(3):
            rows, raw = records(source[axis * 2], kinds[axis])
            prefixes = source[axis * 2 + 1]
            if not isinstance(prefixes, (list, tuple)) or len(prefixes) != len(rows) + 1:
                raise _invalid("native source prefix count differs from its rows")
            digest = hashlib.sha256(domains[axis].encode("ascii") + b"\0")
            if prefixes[0] != digest.hexdigest():
                raise _invalid("native source prefix has a foreign digest domain")
            for index, frame in enumerate(raw):
                digest.update(frame)
                if prefixes[index + 1] != digest.hexdigest():
                    raise _invalid("native source prefix differs from its exact frames")
            copied.extend((rows, tuple(prefixes)))
        sources.append(tuple(copied))
        by_id = {row["id"]: row for row in copied[0] if row["kind"] == "message_envelope"}
        messages.append(by_id)
        for identity, row in by_id.items():
            first.setdefault(identity, (ordinal, row))
    consumed, _raw = records(consumed_rows, {"consumption_receipt"})
    work, _raw = records(work_rows, WORK_KINDS)
    return sources, messages, first, consumed, work


def _automatic(messages, epochs, message_id):
    for index, by_id in enumerate(messages):
        if index and not automatic_predecessor_open(epochs[index - 1][0]):
            break
        message = by_id.get(message_id)
        if message is not None:
            return {"source_index": index, "message": message}
    return None


def automatic_source(tenant, message_id, epochs):
    """Resolve DATA over caller-owned ordered lineage; no lineage admission."""
    sources, messages, _first, _consumed, _work = _prepare(tenant, epochs)
    return deepcopy(_automatic(messages, sources, message_id))


def _replay_history_data(tenant, node, worker, epochs, consumed_rows, work_rows):
    """Validate ALL historical receipts, returning current-only and full joins.

    Work projection uses explicit rows only. Its holder result is DATA, not a
    substitute for the native caller's current live work-holder check.
    """
    from .consumption import ConsumptionLedger
    sources, _messages, first, consumed, work = _prepare(tenant, epochs, consumed_rows, work_rows)
    # project_tail is already a pure native method: its unused receiver is None,
    # so no FloatiRoot/ledger instance or implicit records() read is constructed.
    work_items = ConsumptionLedger.project_tail(None, {}, work)
    seen, ack_indices, joins, current = set(), {}, [], []
    for receipt in consumed:
        identity = receipt["message_id"]
        if identity in seen:
            raise _invalid("duplicate consumption for one recipient envelope")
        seen.add(identity)
        if receipt["recipient"] != node or receipt["worker_session_id"] != worker:
            raise _invalid("consumption belongs to another recipient or worker plane")
        index, message = first.get(identity, (None, None))
        validate_consumed_envelope(tenant, node, receipt, message)
        if index not in ack_indices:
            source = sources[index]
            ack_indices[index] = _ack_index(tenant, node, worker, source[0], source[2], source[4])
        matching = ack_indices[index].get(identity, [])
        joined = _consumed_join(tenant, node, receipt, message, matching,
                                        work_items.get(receipt["work_item_id"]))
        joined["source_index"] = index
        joins.append(joined)
        if index == 0:
            current.append(receipt)
    return {"current_receipts": current, "joins": joins, "work_items": work_items}


def replay_history(tenant, node, worker, epochs, consumed_rows, work_rows):
    """Detached public DATA, preserving native historical validation ordering."""
    return deepcopy(_replay_history_data(tenant, node, worker, epochs, consumed_rows, work_rows))


def first_act_candidate(tenant, node, worker, message_id, acting_session, epochs, *, projection_source=None):
    """Return candidate DATA AFTER caller's actual native projection replay.

    projection_source must bind exactly to the replayed selected source. This
    equality is DATA consistency, never proof that projection/hold replay ran.
    Native registry/coordination/claims and eventual committed admission remain
    outside this API. Never use direct ACK joins as imported-hold validity.
    """
    from .wake_control import validate_session_id
    session = validate_session_id(acting_session)
    sources, messages, first, _consumed, _work = _prepare(tenant, epochs)
    selected = _automatic(messages, sources, message_id)
    if selected is None:
        code = "consume_item_archived" if message_id in first else "consume_item_unknown"
        raise ProtocolRefusal(code, "message is archived beyond a manual boundary or unknown",
                              remedy="name acknowledged mail from the current or uninterrupted automatic epochs")
    index, message = selected["source_index"], selected["message"]
    source = sources[index]
    if message["recipient"] != node:
        raise ProtocolRefusal("consume_recipient_mismatch", "message belongs to another recipient",
                              remedy="consume as the envelope's exact recipient")
    if any(row["kind"] == "message_retracted" and row["retracted_message_id"] == message_id for row in source[0]):
        raise ProtocolRefusal("consume_item_retracted", "message has been retracted",
                              remedy="do not consume retracted mail")
    plane = message.get("worker_session_id")
    if plane != worker or (plane is not None and plane != session):
        raise ProtocolRefusal("consume_session_mismatch", "message belongs to another worker session",
                              remedy="use the exact worker session bound to the envelope")
    if projection_source is None:
        raise ProtocolRefusal("archive_consumption_projection_required", "actual native projection replay is a prerequisite",
                              remedy="replay the exact source with native hold origins before requesting candidate DATA")
    if (not isinstance(projection_source, (list, tuple)) or len(projection_source) != 6
            or any(not isinstance(projection_source[axis], (list, tuple))
                   or len(projection_source[axis]) != len(source[axis]) for axis in range(6))):
        raise _invalid("projection DATA does not bind the exact selected source")
    from .jsonl import MAX_RECORD_BYTES
    try:
        for axis in (0, 2, 4):
            for expected, actual in zip(source[axis], projection_source[axis]):
                raw = encode_frame(actual)
                if len(raw) > MAX_RECORD_BYTES:
                    raise _unsupported("projection frame exceeds supported record bytes")
                if raw != encode_frame(expected):
                    raise _invalid("projection DATA does not bind the exact selected source")
        if any(tuple(source[axis]) != tuple(projection_source[axis]) for axis in (1, 3, 5)):
            raise _invalid("projection DATA does not bind the exact selected source")
    except (TypeError, KeyError, IndexError, FrameError, RecursionError) as exc:
        raise _invalid("projection DATA does not bind the exact selected source") from exc
    acks = ack_references(tenant, node, message, source[0], source[2], source[4])
    if not acks:
        raise ProtocolRefusal("consume_item_not_acknowledged", "message requires an explicit ACK first",
                              remedy="explicitly acknowledge the named envelope, then retry")
    return deepcopy({"message": message, "source_index": index, "matching_ack_receipts": acks})

"""Bounded receipt-owned history queries; results are DATA, never hold authority."""
from dataclasses import dataclass

from . import epoch_carry
from .archive_survivor_sources import _domain_digest, _QUERY_ALLOWANCE, _query_charge
from .cursor import SparseCursor
from .errors import IntegrityFailure, ProtocolRefusal
from .events import EVENT_KINDS, validate_event_records
from .framing import decode_frames
from .jsonl import _decode_path_records
from .records import WAKE_HOLD_KINDS
from .wake_hold import WakeHoldController


MAX_BATCH_IDS = 1000


def _invalid(detail):
    return IntegrityFailure("epoch_history_query_invalid", detail)


def _unsupported(detail):
    return ProtocolRefusal("epoch_history_query_unsupported", detail,
        remedy="retain original epoch payloads; joined history exceeds supported admission")


class _Work:
    def __init__(self, remaining):
        self._remaining = remaining

    @property
    def remaining(self):
        state = _QUERY_ALLOWANCE.get()
        return self._remaining if state is None else state.remaining

    def charge(self, size):
        if _QUERY_ALLOWANCE.get() is not None:
            _query_charge(size)
            return
        if size > self.remaining:
            raise _unsupported("joined source traversal exceeds combined work support")
        self._remaining -= size


@dataclass(frozen=True)
class _NativeEpoch:
    coordinate: tuple
    owner: object
    anchor: object
    payloads: tuple


def native_epochs(root, archives, live_payloads, *, work):
    """Capture actual native sources; accept no PoolData or caller proof."""
    result = []
    sources = [(None, live_payloads)]
    for archive in archives:
        from .bus_epoch import _read_epoch_archive_payloads
        # One linear bulk capture retains the original archive witness before
        # and after, rather than rescanning the whole namespace per member.
        archive._assert_bound()
        payloads, _snapshot = _read_epoch_archive_payloads(archive.legacy_path)
        archive._assert_bound()
        sources.append((archive.receipt, payloads))
    for owner, payloads in sources:
        raw = next((raw for name, _plane, raw in payloads if name == "events.jsonl"), b"")
        work.charge(len(raw))
        rows = decode_frames(raw)
        anchor = rows[0] if rows and rows[0].get("kind") == "bus_epoch_roll_receipt" else None
        coordinate = ("live",) if owner is None else (
            "archive", owner["id"], None if anchor is None else anchor["id"])
        result.append(_NativeEpoch(coordinate, owner, anchor, tuple(payloads)))
    if len({row.coordinate for row in result}) != len(result):
        raise _invalid("native logical epoch coordinates are ambiguous")
    return tuple(result)


def source(root, epoch, node, worker, *, work, max_result_bytes):
    """Reproduce a native six-tuple from bound captured bytes, without a cache."""
    paths = SparseCursor(root)
    coordinates = (("events.jsonl", EVENT_KINDS, WakeHoldController._EVENT_DOMAIN),
        (paths._delivery_relative_path_for(node, worker_session_id=worker).as_posix(),
         WAKE_HOLD_KINDS, WakeHoldController._DELIVERY_DOMAIN),
        (paths._relative_path_for(node, worker_session_id=worker).as_posix(),
         {"ack_receipt"}, WakeHoldController._ACK_DOMAIN))
    payloads = {name: raw for name, _plane, raw in epoch.payloads}
    result = []
    for relative, kinds, domain in coordinates:
        raw = payloads.get(relative, b"")
        work.charge(len(raw))
        decoded = epoch_carry._decode(root, relative, raw, kinds, domain,
                                     events=relative == "events.jsonl")
        result.extend(decoded[:2])
    validate_event_records(result[0])
    _domain_digest("joined_native_source", result, _max_bytes=max_result_bytes,
                   _max_nodes=work.remaining)
    return tuple(result)


def envelope_membership(root, native, item_id, *, work, max_result_bytes):
    """One-ID event membership DATA; caller owns native admission and guard.

    Both native payload producers sort selected paths and include events first
    (an absent LIVE member has empty bytes). Reuse that captured cell directly,
    rather than charging/searching every unrelated delivery or ACK member.
    """
    if type(item_id) is not str:
        raise _invalid("envelope membership requires an exact native ID string")
    status = "absent"
    for epoch in native:
        work.charge(1)  # epoch visit and constant first-cell observation
        if type(epoch.payloads) is not tuple or not epoch.payloads:
            raise _invalid("native history has no captured event member")
        member = epoch.payloads[0]
        if type(member) is not tuple or len(member) != 3:
            raise _invalid("captured native event member has an invalid shape")
        name, plane, raw = member
        if (type(name) is not str or type(plane) is not str or type(raw) is not bytes
                or name != "events.jsonl" or plane != "events"):
            raise _invalid("captured native event member is not first")
        work.charge(len(raw))
        live = epoch.owner is None
        rows = _decode_path_records(root.tenant_home / "events.jsonl", root.tenant_id,
            frozenset(EVENT_KINDS), raw, unrecognized={} if live else None)
        work.charge(len(rows))  # before semantic validation and ID traversal
        if live:
            validate_event_records(rows)
        _domain_digest("native_envelope_membership_rows", rows,
            _max_bytes=max_result_bytes, _max_nodes=work.remaining)
        # Native schema fixes every envelope ID's closed shape. An out-of-shape
        # exact string cannot match, but never suppresses required decoding.
        if any(row["kind"] == "message_envelope" and row["id"] == item_id for row in rows):
            status = "live" if live else "archive"
            break
    _domain_digest("native_envelope_membership_result", status,
        _max_bytes=max_result_bytes, _max_nodes=work.remaining)
    return status


def _membership_plane(root, member, plane, *, work, max_result_bytes):
    """Strict archived rows with paid raw, row and expanded-domain work."""
    if type(member) is not tuple or len(member) != 3:
        raise _invalid("captured archive membership member has an invalid shape")
    name, actual_plane, raw = member
    if (type(name) is not str or type(actual_plane) is not str or type(raw) is not bytes
            or actual_plane != plane
            or (plane == "events" and name != "events.jsonl")
            or (plane == "deliveries" and not (
                name.startswith("receipts/deliveries/") and name.endswith(".jsonl")))):
        raise _invalid("captured archive membership member has an invalid plane")
    kinds = EVENT_KINDS if plane == "events" else WAKE_HOLD_KINDS
    work.charge(len(raw))
    rows = _decode_path_records(root.tenant_home / name, root.tenant_id, frozenset(kinds), raw)
    # Pay the row scan before deriving item counts or searching IDs. Presence
    # is schema-only here, matching the legacy strict archive reader.
    work.charge(len(rows))
    _domain_digest("native_archived_membership_rows", rows,
        _max_bytes=max_result_bytes, _max_nodes=work.remaining)
    return rows


def _membership_delivery_rows(root, epoch, relative, *, work, max_result_bytes):
    """Search one captured optional plane after the caller reserves its census."""
    for member in epoch.payloads:
        if (type(member) is not tuple or len(member) != 3
                or type(member[0]) is not str or type(member[1]) is not str
                or type(member[2]) is not bytes):
            raise _invalid("captured archive membership inventory has an invalid shape")
        if member[0] == relative:
            return _membership_plane(root, member, "deliveries", work=work,
                max_result_bytes=max_result_bytes)
    # A physically absent optional member has no synthetic empty decode or
    # domain allocation. A captured empty file takes the normal path above.
    return None


def _membership_contains_item(rows, item_id):
    """Traverse nested references only after their full count is reserved."""
    return any(item_id in row["item_ids"] for row in rows)


def archived_item_membership(root, native, item_id, recipient, worker=None, *, work, max_result_bytes):
    """Archive-only envelope OR exact delivery presence; result is DATA."""
    if (type(item_id) is not str or type(recipient) is not str
            or (worker is not None and type(worker) is not str)):
        raise _invalid("archive membership requires exact native string selectors")
    # The root composition checks the admitted reader contract before this
    # path dispatch. Never accept a caller home, Path or pooled content key.
    relative = SparseCursor(root)._delivery_relative_path_for(
        recipient, worker_session_id=worker).as_posix()
    result = False
    for epoch in native:
        if epoch.owner is None:
            continue  # no LIVE lookup or uncharged tuple slice
        work.charge(1)
        if type(epoch.payloads) is not tuple or not epoch.payloads:
            raise _invalid("native archive has no captured first event member")
        rows = _membership_plane(root, epoch.payloads[0], "events", work=work,
            max_result_bytes=max_result_bytes)
        if any(row["kind"] == "message_envelope" and row["id"] == item_id for row in rows):
            result = True
            break
        work.charge(len(epoch.payloads))
        rows = _membership_delivery_rows(root, epoch, relative, work=work,
            max_result_bytes=max_result_bytes)
        if rows is not None:
            # The plane's row debit pays this count scan; reserve every nested
            # reference before the separate presence traversal can execute.
            work.charge(sum(len(row["item_ids"]) for row in rows))
            if _membership_contains_item(rows, item_id):
                result = True
                break
    _domain_digest("native_archived_membership_result", result,
        _max_bytes=max_result_bytes, _max_nodes=work.remaining)
    return result


def _membership_batch_selectors(missing_ids):
    """Validate every prepaid selector before any archived ID comparison."""
    if any(type(item) is not str or not item for item in missing_ids):
        raise _invalid("archived envelope batch requires nonempty exact native strings")


def archived_envelope_membership(root, native, missing_ids, *, work, max_result_bytes):
    """Archive-major ANY envelope presence among the missing LIVE IDs only."""
    if type(missing_ids) is not frozenset or not 1 <= len(missing_ids) <= MAX_BATCH_IDS:
        raise _invalid("archived envelope batch requires a bounded exact frozenset")
    work.charge(len(missing_ids))
    _membership_batch_selectors(missing_ids)
    result = False
    for epoch in native:
        if epoch.owner is None:
            continue
        work.charge(1)
        if type(epoch.payloads) is not tuple or not epoch.payloads:
            raise _invalid("native archive has no captured first event member")
        rows = _membership_plane(root, epoch.payloads[0], "events", work=work,
            max_result_bytes=max_result_bytes)
        if any(row["kind"] == "message_envelope" and row["id"] in missing_ids for row in rows):
            result = True
            break
    _domain_digest("native_archived_envelope_membership_result", result,
        _max_bytes=max_result_bytes, _max_nodes=work.remaining)
    return result

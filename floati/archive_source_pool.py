"""Closed, canonical source-pool DATA, never survivor admission authority.

This validates native record shapes, content identity and structural joins only.
It neither proves source discovery/coverage nor replays hold, presentation or
execution semantics. A caller can manufacture an internally consistent pool;
native collection, issuance and committed-generation verification remain separate.
No filesystem access, publication, mutation or payload retirement is performed.
"""

import hashlib
import json
import re
from types import MappingProxyType

from .errors import IntegrityFailure, ProtocolRefusal
from .framing import FrameError, decode_frames, encode_frame
from .jsonl import MAX_LEDGER_BYTES, MAX_LEDGER_RECORDS, MAX_RECORD_BYTES, VerifiedLedgerCursor


MAX_CHUNK_BYTES = 8 * 1024 * 1024
MAX_CHUNKS = 128
MAX_ENTRIES = 1000000
MAX_REFERENCES = 1000000
MAX_AXIS_RECORDS = MAX_LEDGER_RECORDS
_COMMON = {"schema_version", "tenant_id", "tag", "key"}
_FIELDS = {
    "frame": {"record"},
    "epoch": {"epoch_id", "owner", "anchor", "predecessor"},
    "axis": {"epoch", "plane", "recipient", "worker_session_id", "frames",
             "record_count", "digest_domain", "digest"},
    "cut": {"epoch", "recipient", "worker_session_id", "axes"},
}
_HEX = re.compile(r"[0-9a-f]{64}\Z")


def _invalid(detail):
    return IntegrityFailure("archive_source_pool_invalid", detail)


def _unsupported(detail):
    return ProtocolRefusal("archive_source_pool_unsupported", detail,
                           remedy="retain original epoch payload; source pool exceeds its supported bounds")


def _canonical(value):
    try:
        return encode_frame(value)
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise _invalid("entry is not bounded canonical JSON data") from exc


def _hex(value):
    return isinstance(value, str) and _HEX.fullmatch(value) is not None


def _count(value):
    return type(value) is int and value >= 0


def entry_key(entry):
    """Content address helper, not validation or proof issuance."""
    if (not isinstance(entry, dict) or not isinstance(entry.get("tag"), str)
            or entry["tag"] not in _FIELDS):
        raise _invalid("entry has no closed source-pool tag")
    body = {key: value for key, value in entry.items() if key != "key"}
    return hashlib.sha256(b"floati-archive-source-pool-v1\0" +
                          body["tag"].encode("ascii") + b"\0" +
                          _canonical(body)).hexdigest()


def keyed_entry(tag, tenant, **fields):
    """Construct caller-owned data; read_pool performs the format validation."""
    result = dict(schema_version=1, tag=tag, tenant_id=tenant, **fields)
    result["key"] = entry_key(result)
    return result


def encode_pool(entries):
    """Validate then deterministically pack complete entries, without writes."""
    rows = []
    tenant = None
    serialized_bytes = 0
    for entry in entries:
        if len(rows) >= MAX_ENTRIES:
            raise _unsupported("too many source-pool entries")
        raw = _canonical(entry)
        if len(raw) > MAX_CHUNK_BYTES:
            raise _unsupported("one source-pool entry exceeds the 8 MiB chunk bound")
        serialized_bytes += len(raw)
        if serialized_bytes > MAX_CHUNKS * MAX_CHUNK_BYTES:
            raise _unsupported("source-pool aggregate serialized byte bound exceeded")
        if not isinstance(entry, dict):
            raise _invalid("entry is not an object")
        if tenant is None:
            tenant = entry.get("tenant_id")
        rows.append((entry.get("key"), raw))
    if any(not _hex(key) for key, _raw in rows):
        raise _invalid("entry key is not a SHA-256 content address")
    rows.sort(key=lambda row: row[0])
    chunks, pending = [], bytearray()
    for _key, raw in rows:
        if pending and len(pending) + len(raw) > MAX_CHUNK_BYTES:
            chunks.append(bytes(pending))
            pending.clear()
            if len(chunks) >= MAX_CHUNKS:
                raise _unsupported("too many source-pool chunks")
        pending.extend(raw)
    if pending:
        chunks.append(bytes(pending))
    result = tuple(chunks)
    read_pool(result, tenant)
    return result


class PoolData:
    """Immutable bytes and indexes of unprivileged, format-checked data only."""

    __slots__ = ("_entries", "_frames", "_tags", "_tenant")

    def __init__(self, tenant, entries, frames, tags):
        self._tenant = tenant
        self._entries = MappingProxyType(dict(entries))
        self._frames = MappingProxyType(dict(frames))
        self._tags = MappingProxyType({tag: tuple(keys) for tag, keys in tags.items()})

    @property
    def tenant_id(self):
        return self._tenant

    @property
    def keys(self):
        return tuple(self._entries)

    @property
    def entries(self):
        return self._entries

    @property
    def frames(self):
        return self._frames

    def by_tag(self, tag):
        return self._tags.get(tag, ())

    def entry(self, key):
        from .archive_survivor_sources import _query_charge
        _query_charge(len(self._entries[key]))
        return json.loads(self._entries[key])

    def frame_bytes(self, key):
        return self._frames[key]


def read_pool(chunks, tenant):
    """Read a complete canonical collection into DATA indices, with no authority."""
    from .cursor import SparseCursor
    from .events import EVENT_KINDS
    from .records import WAKE_HOLD_KINDS, validate_record
    from .root import validate_identifier
    from .wake_hold import WakeHoldController

    if not isinstance(tenant, str) or not tenant:
        raise _invalid("tenant binding is absent")
    entries, raw_entries, frames, tags = {}, {}, {}, {tag: [] for tag in _FIELDS}
    last_key = ""
    references = 0
    for ordinal, chunk in enumerate(chunks):
        if ordinal >= MAX_CHUNKS:
            raise _unsupported("too many source-pool chunks")
        if not isinstance(chunk, bytes) or not chunk:
            raise _invalid("each source-pool chunk must be nonempty bytes")
        if len(chunk) > MAX_CHUNK_BYTES:
            raise _unsupported("source-pool chunk exceeds 8 MiB")
        canonical_chunk = bytearray()
        try:
            from .archive_survivor_sources import _query_charge
            _query_charge(len(chunk))
            for row in decode_frames(chunk):
                if len(entries) >= MAX_ENTRIES:
                    raise _unsupported("too many source-pool entries")
                if not isinstance(row, dict) or not isinstance(row.get("tag"), str) or row["tag"] not in _FIELDS:
                    raise _invalid("unknown source-pool tag")
                if set(row) != _COMMON | _FIELDS[row["tag"]]:
                    raise _invalid("entry has missing or open fields")
                if type(row["schema_version"]) is not int or row["schema_version"] != 1 or row["tenant_id"] != tenant:
                    raise _invalid("entry version or tenant binding differs")
                key = row["key"]
                if not _hex(key) or key <= last_key or key != entry_key(row):
                    raise _invalid("entry key is duplicate, unordered or content-mismatched")
                last_key = key
                entries[key] = row
                raw_entries[key] = _canonical(row)
                canonical_chunk.extend(raw_entries[key])
                tags[row["tag"]].append(key)
                if row["tag"] == "epoch":
                    references += sum(row[field] is not None for field in ("owner", "anchor", "predecessor"))
                elif row["tag"] == "axis":
                    references += 1 + (len(row["frames"]) if isinstance(row["frames"], list) else 0)
                elif row["tag"] == "cut":
                    references += 4
                if references > MAX_REFERENCES:
                    raise _unsupported("source-pool aggregate reference bound exceeded")
        except (FrameError, UnicodeError, RecursionError, ValueError) as exc:
            raise _invalid("source-pool chunk is not complete strict JSONL") from exc
        # decode_frames accepts ordinary JSON whitespace; this format does not.
        if canonical_chunk != chunk:
            raise _invalid("source-pool JSONL is not canonical")

    def ref(key, tag):
        if not _hex(key) or key not in entries or entries[key]["tag"] != tag:
            raise _invalid("dangling or wrong-tag " + tag + " reference")
        return entries[key]

    identities = {}
    for key in tags["frame"]:
        record = entries[key]["record"]
        if not isinstance(record, dict) or not isinstance(record.get("kind"), str):
            raise _invalid("frame has no native record kind")
        raw = _canonical(record)
        if len(raw) > MAX_RECORD_BYTES:
            raise _unsupported("native frame exceeds its record byte bound")
        validate_record(record, tenant, frozenset({record["kind"]}), integrity=True)
        if record["id"] in identities and identities[record["id"]] != raw:
            raise _invalid("native record ID has conflicting canonical bytes")
        identities[record["id"]] = raw
        frames[key] = raw

    epochs = {key: entries[key] for key in tags["epoch"]}
    live = []
    owners, epoch_ids = set(), set()
    for key, epoch in epochs.items():
        if epoch["owner"] is None:
            if epoch["epoch_id"] is not None:
                raise _invalid("live epoch cannot have an archived epoch ID")
            live.append(key)
        else:
            owner = ref(epoch["owner"], "frame")["record"]
            if owner["kind"] != "bus_epoch_roll_receipt" or epoch["epoch_id"] != owner["epoch_id"]:
                raise _invalid("archived epoch owner differs from its native roll receipt")
            if epoch["owner"] in owners or epoch["epoch_id"] in epoch_ids:
                raise _invalid("native roll owner or epoch ID is reused by multiple epoch headers")
            owners.add(epoch["owner"])
            epoch_ids.add(epoch["epoch_id"])
        if epoch["anchor"] is None:
            if epoch["predecessor"] is not None:
                raise _invalid("genesis has no predecessor")
        else:
            anchor = ref(epoch["anchor"], "frame")["record"]
            previous = ref(epoch["predecessor"], "epoch")
            if anchor["kind"] != "bus_epoch_roll_receipt" or previous["owner"] != epoch["anchor"]:
                raise _invalid("epoch anchor does not own its exact predecessor")
    if len(live) != 1:
        raise _invalid("collection requires exactly one live epoch")
    reached, current = set(), live[0]
    while current is not None:
        if current in reached:
            raise _invalid("epoch lineage cycles")
        reached.add(current)
        current = epochs[current]["predecessor"]
    if reached != set(epochs):
        raise _invalid("collection has off-lineage or incomplete epoch headers")

    domains = {"events": WakeHoldController._EVENT_DOMAIN,
               "deliveries": WakeHoldController._DELIVERY_DOMAIN,
               "acks": WakeHoldController._ACK_DOMAIN}
    allowed = {"events": EVENT_KINDS, "deliveries": WAKE_HOLD_KINDS, "acks": {"ack_receipt"}}
    coordinates, axis_rows, event_axes, prefixes = {}, {}, {}, {}

    def coordinate(row):
        try:
            validate_identifier(row["recipient"], "recipient")
            if row["worker_session_id"] is not None:
                SparseCursor._session_component(row["worker_session_id"])
        except (ProtocolRefusal, TypeError, ValueError) as exc:
            raise _invalid("recipient or worker axis coordinate is invalid") from exc

    for key in tags["axis"]:
        axis = entries[key]
        epoch = ref(axis["epoch"], "epoch")
        plane = axis["plane"]
        if not isinstance(plane, str) or plane not in domains or axis["digest_domain"] != domains[plane]:
            raise _invalid("axis has a foreign plane or native digest domain")
        if plane == "events":
            if axis["recipient"] is not None or axis["worker_session_id"] is not None:
                raise _invalid("event axis must use the global event coordinate")
            event_axes[axis["epoch"]] = key
        else:
            coordinate(axis)
        scope = (axis["epoch"], plane, axis["recipient"], axis["worker_session_id"])
        if scope in coordinates:
            raise _invalid("duplicate source axis coordinate")
        coordinates[scope] = key
        refs = axis["frames"]
        if not isinstance(refs, list) or not _count(axis["record_count"]) or axis["record_count"] != len(refs):
            raise _invalid("axis frame count differs from its ordered references")
        if len(refs) > MAX_AXIS_RECORDS:
            raise _unsupported("axis exceeds native record count bound")
        _query_charge(2 * len(refs) + 1)  # validated resolved rows and prefix slots
        if any(not _hex(item) for item in refs) or len(set(refs)) != len(refs):
            raise _invalid("axis has duplicate or malformed frame references")
        rows, byte_count = [], 0
        digest = VerifiedLedgerCursor._initial_digest(domains[plane])
        native_prefixes = [digest.hexdigest()]
        for item in refs:
            record = ref(item, "frame")["record"]
            if record["kind"] not in allowed[plane]:
                raise _invalid("axis borrowed a foreign native record family")
            rows.append(record)
            byte_count += len(frames[item])
            if byte_count > MAX_LEDGER_BYTES:
                raise _unsupported("axis exceeds native ledger byte bound")
            digest.update(frames[item])
            native_prefixes.append(digest.hexdigest())
        if not _hex(axis["digest"]) or digest.hexdigest() != axis["digest"]:
            raise _invalid("axis native digest differs from ordered canonical frames")
        if plane == "events":
            rolls = [i for i, row in enumerate(rows) if row["kind"] == "bus_epoch_roll_receipt"]
            if rolls not in ([], [0]) or ((refs[0] if rolls else None) != epoch["anchor"]):
                raise _invalid("event axis does not start at its exact native epoch anchor")
        axis_rows[key] = rows
        prefixes[key] = tuple(native_prefixes)
    if set(event_axes) != set(epochs):
        raise _invalid("every epoch, including empty manual epochs, needs an event axis")

    # Empty recipient/worker planes are common. A full event scan per plane
    # would multiply event-count by plane-count despite the reference quota.
    messages_by_epoch = {
        epoch: {row["id"]: row for row in axis_rows[key] if row["kind"] == "message_envelope"}
        for epoch, key in event_axes.items()
    }
    for key, rows in axis_rows.items():
        axis = entries[key]
        if axis["plane"] == "events":
            continue
        messages = messages_by_epoch[axis["epoch"]]
        for row in rows:
            if row["recipient"] != axis["recipient"] or (row["kind"] == "wake_hold_receipt" and
                    row["worker_session_id"] != axis["worker_session_id"]):
                raise _invalid("receipt belongs to a foreign recipient or worker plane")
            for item in row["item_ids"]:
                message = messages.get(item)
                if message is None or message["recipient"] != axis["recipient"] or message.get("worker_session_id") != axis["worker_session_id"]:
                    raise _invalid("receipt item has no exact same-epoch recipient/worker envelope")

    for key in tags["cut"]:
        cut = entries[key]
        ref(cut["epoch"], "epoch")
        coordinate(cut)
        if not isinstance(cut["axes"], dict) or set(cut["axes"]) != set(domains):
            raise _invalid("cut must name exactly event, delivery and ACK axes")
        for plane, prefix in cut["axes"].items():
            if not isinstance(prefix, dict) or set(prefix) != {"axis", "count", "digest"}:
                raise _invalid("cut prefix has missing or open fields")
            axis = ref(prefix["axis"], "axis")
            if axis["epoch"] != cut["epoch"] or axis["plane"] != plane or (plane != "events" and
                    (axis["recipient"], axis["worker_session_id"]) != (cut["recipient"], cut["worker_session_id"])):
                raise _invalid("cut borrowed an axis from a foreign epoch or plane")
            count = prefix["count"]
            if not _count(count) or count > axis["record_count"] or not _hex(prefix["digest"]):
                raise _invalid("cut prefix count or digest is invalid")
            if prefixes[prefix["axis"]][count] != prefix["digest"]:
                raise _invalid("cut native prefix digest differs")
    return PoolData(tenant, raw_entries, frames, tags)

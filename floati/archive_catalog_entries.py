"""Closed catalog DATA, without native publication or removal authority.

This format cross-checks metadata against source-pool native roll frames. It
does not read payloads, establish native discovery/coverage, verify an on-disk
archive, grant survivor admission, issue commits or authorize transitions.
Caller-manufactured consistent DATA remains unprivileged.
"""

import hashlib
import json
import re
from types import MappingProxyType

from .archive_container import _facts, _inventory
from .archive_source_pool import PoolData
from .errors import IntegrityFailure, ProtocolRefusal
from .framing import FrameError, decode_frames, encode_frame


MAX_CHUNK_BYTES = 8 * 1024 * 1024
MAX_CHUNKS = 128
MAX_ENTRIES = 1000000
MAX_INVENTORY_MEMBERS = 1000000
MAX_TOTAL_INVENTORY_MEMBERS = 1000000
_MAX_NATIVE_LENGTH = 2 ** 63 - 1
_HEX = re.compile(r"[0-9a-f]{64}\Z")
_FIELDS = {"schema_version", "tag", "tenant_id", "key", "epoch_id", "roll_frame",
           "source_epoch", "predecessor", "scope", "boundary", "rolled_at",
           "facts", "inventory", "payload"}
_FACTS = {"archive_sha256", "archive_file_count", "plane_counts", "span"}


def _invalid(detail):
    return IntegrityFailure("archive_catalog_entries_invalid", detail)


def _unsupported(detail):
    return ProtocolRefusal("archive_catalog_entries_unsupported", detail,
        remedy="retain original epoch payload; catalog DATA format cannot represent this source")


def _canonical(value, *, reserved=0):
    from .archive_survivor_sources import _domain_digest
    try:
        _domain_digest("catalog_entry", value, _max_bytes=MAX_CHUNK_BYTES - 1 - reserved)
    except ProtocolRefusal as exc:
        raise _unsupported("catalog entry exceeds bounded canonical metadata support") from exc
    except IntegrityFailure as exc:
        raise _invalid("catalog entry is not bounded canonical JSON data") from exc
    try:
        return encode_frame(value)
    except (FrameError, TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise _invalid("catalog entry is not canonical JSON data") from exc


def _hex(value):
    return isinstance(value, str) and _HEX.fullmatch(value) is not None


def _integer(value):
    return type(value) is int and 0 <= value <= _MAX_NATIVE_LENGTH


def entry_key(entry):
    """Versioned content identity; not validation or proof issuance."""
    if not isinstance(entry, dict) or entry.get("tag") != "catalog_epoch":
        raise _invalid("catalog entry has no closed catalog_epoch tag")
    body = {name: value for name, value in entry.items() if name != "key"}
    # The nonempty native object gains exactly 73 bytes for its SHA-256 key.
    return hashlib.sha256(b"floati-archive-catalog-epoch-v1\0" + _canonical(body, reserved=73)).hexdigest()


def catalog_entry(tenant, **fields):
    """Construct detached data; encode/read_catalog perform validation."""
    result = dict(schema_version=1, tag="catalog_epoch", tenant_id=tenant, **fields)
    result["key"] = entry_key(result)
    return result


def encode_catalog(entries, sources):
    """Validate and deterministically pack metadata, with early allocation caps."""
    if not isinstance(sources, PoolData):
        raise _invalid("catalog requires source-pool DATA indices")
    rows, total = [], 0
    for entry in entries:
        if len(rows) >= MAX_ENTRIES:
            raise _unsupported("catalog entry count exceeds its metadata bound")
        raw = _canonical(entry)
        if len(raw) > MAX_CHUNK_BYTES:
            raise _unsupported("one catalog entry exceeds the 8 MiB metadata chunk bound")
        total += len(raw)
        if total > MAX_CHUNKS * MAX_CHUNK_BYTES:
            raise _unsupported("catalog aggregate serialized byte bound exceeded")
        if not isinstance(entry, dict) or not _hex(entry.get("key")):
            raise _invalid("catalog entry has no SHA-256 content key")
        rows.append((entry["key"], raw))
    rows.sort(key=lambda row: row[0])
    chunks, pending = [], bytearray()
    for _key, raw in rows:
        if pending and len(pending) + len(raw) > MAX_CHUNK_BYTES:
            chunks.append(bytes(pending))
            pending.clear()
            if len(chunks) >= MAX_CHUNKS:
                raise _unsupported("catalog exact packing exceeds its chunk-count bound")
        pending.extend(raw)
    if pending:
        chunks.append(bytes(pending))
    chunks = tuple(chunks)
    read_catalog(chunks, sources.tenant_id, sources)
    return chunks


class CatalogData:
    """Immutable byte/index DATA, deliberately without admission methods."""

    __slots__ = ("_tenant", "_entries", "_receipts", "_epochs", "_scopes", "_retry")

    def __init__(self, tenant, entries, receipts, epochs, scopes, retry):
        self._tenant = tenant
        self._entries = MappingProxyType(dict(entries))
        self._receipts = MappingProxyType(dict(receipts))
        self._epochs = MappingProxyType({key: tuple(value) for key, value in epochs.items()})
        self._scopes = MappingProxyType({key: tuple(value) for key, value in scopes.items()})
        self._retry = MappingProxyType(dict(retry))

    @property
    def tenant_id(self):
        return self._tenant

    @property
    def keys(self):
        return tuple(self._entries)

    @property
    def entries(self):
        return self._entries

    def entry(self, key):
        from .archive_survivor_sources import _query_charge
        _query_charge(len(self._entries[key]))
        return json.loads(self._entries[key])

    def receipt_bytes(self, key):
        return self._receipts[key]

    def by_epoch(self, epoch_id):
        return self._epochs.get(epoch_id, ())

    def by_scope(self, scope):
        return self._scopes.get(scope, ())

    def retry_key(self, key):
        return self._retry.get(key)


def read_catalog(chunks, tenant, sources):
    """Cross-check complete catalog DATA against source-pool native references."""
    from .records import _json_integer, validate_record
    if not isinstance(sources, PoolData) or not isinstance(tenant, str) or tenant != sources.tenant_id:
        raise _invalid("catalog tenant differs from its source-pool DATA binding")
    owner_epochs = {}
    for key in sources.by_tag("epoch"):
        epoch = sources.entry(key)
        if epoch["owner"] is not None:
            owner_epochs[epoch["owner"]] = (key, epoch)
    entries, receipts, epochs, scopes, retry = {}, {}, {}, {"lineage": [], "off_lineage": []}, {}
    original_ids, seen_lineage = set(), set()
    previous_key, total_inventory = "", 0
    for ordinal, chunk in enumerate(chunks):
        if ordinal >= MAX_CHUNKS:
            raise _unsupported("catalog chunk count exceeds its metadata bound")
        if not isinstance(chunk, bytes) or not chunk:
            raise _invalid("catalog chunks must be nonempty bytes")
        if len(chunk) > MAX_CHUNK_BYTES:
            raise _unsupported("catalog chunk exceeds 8 MiB")
        canonical_chunk = bytearray()
        try:
            from .archive_survivor_sources import _query_charge
            _query_charge(len(chunk))
            rows = decode_frames(chunk)
        except (FrameError, UnicodeError, ValueError, RecursionError) as exc:
            raise _invalid("catalog chunk is not complete strict JSONL") from exc
        for row in rows:
            if len(entries) >= MAX_ENTRIES:
                raise _unsupported("catalog entry count exceeds its metadata bound")
            if (not isinstance(row, dict) or set(row) != _FIELDS or row["tag"] != "catalog_epoch"
                    or type(row["schema_version"]) is not int or row["schema_version"] != 1
                    or row["tenant_id"] != tenant):
                raise _invalid("catalog entry shape, version or tenant is invalid")
            key = row["key"]
            if not _hex(key) or key <= previous_key or key != entry_key(row):
                raise _invalid("catalog content key is duplicate, unordered or mismatched")
            previous_key = key
            raw = _canonical(row)
            canonical_chunk.extend(raw)
            frame = row["roll_frame"]
            if not _hex(frame) or frame not in sources.frames:
                raise _invalid("catalog roll_frame is dangling or has a foreign tag")
            source = sources.entry(frame)
            receipt = source["record"]
            if receipt.get("kind") != "bus_epoch_roll_receipt":
                raise _invalid("catalog frame is not an original native roll receipt")
            validate_record(receipt, tenant, frozenset({"bus_epoch_roll_receipt"}), integrity=True)
            if sources.frame_bytes(frame) != _canonical(receipt):
                raise _invalid("original native roll frame bytes differ")
            if row["epoch_id"] != receipt["epoch_id"] or row["rolled_at"] != receipt["timestamp"]:
                raise _invalid("catalog epoch or original UTC timestamp differs from its native roll")
            if row["boundary"] != ("automatic" if "automatic" in receipt else "manual"):
                raise _invalid("catalog manual/automatic boundary differs from its native roll")
            # Retry uniqueness precedes payload checks: changing representation
            # must never erase or duplicate an original accepted retry key.
            native_key = receipt["idempotency_key"]
            if native_key in retry:
                raise _invalid("catalog has duplicate original native retry keys")
            if receipt["id"] in original_ids:
                raise _invalid("catalog duplicates an original native roll identity")
            original_ids.add(receipt["id"])
            if row["scope"] == "lineage":
                owned = owner_epochs.get(frame)
                if (owned is None or row["source_epoch"] != owned[0]
                        or row["predecessor"] != owned[1]["predecessor"]):
                    raise _invalid("catalog lineage owner or predecessor differs from exact source epoch")
                seen_lineage.add(owned[0])
            elif row["scope"] == "off_lineage":
                if frame in owner_epochs or row["source_epoch"] is not None or row["predecessor"] is not None:
                    raise _invalid("off-lineage retry cannot replace lineage or borrow a predecessor")
            else:
                raise _invalid("catalog scope is outside lineage/off_lineage")
            inventory = row["inventory"]
            if not isinstance(inventory, list):
                raise _invalid("catalog inventory must be an ordered finite array")
            total_inventory += len(inventory)
            if len(inventory) > MAX_INVENTORY_MEMBERS or total_inventory > MAX_TOTAL_INVENTORY_MEMBERS:
                raise _unsupported("catalog inventory exceeds its metadata member-count bound")
            inventory = _inventory(inventory)
            if any(not _integer(member["byte_length"]) for member in inventory):
                raise _invalid("opaque member length exceeds the native receipt integer contract")
            derived = _facts(inventory)
            if derived["span"]["byte_end"] > _MAX_NATIVE_LENGTH:
                raise _invalid("opaque summed length exceeds the native receipt integer contract")
            facts = row["facts"]
            if not isinstance(facts, dict) or set(facts) != _FACTS or _canonical(facts) != _canonical(derived):
                raise _invalid("catalog facts differ from the exact original inventory")
            # Existing native records keep their integer-compatible spelling;
            # catalog metadata integers are lexical. Preserve native bytes above.
            native_facts = {name: receipt[name] for name in _FACTS}
            native_facts["archive_file_count"] = _json_integer(native_facts["archive_file_count"])
            native_facts["span"] = dict(native_facts["span"], byte_start=_json_integer(native_facts["span"]["byte_start"]))
            if _canonical(native_facts) != _canonical(derived):
                raise _invalid("inventory facts differ from the original native roll facts")
            payload = row["payload"]
            if not isinstance(payload, dict):
                raise _invalid("catalog payload descriptor has missing or open fields")
            legacy_fields = {"state", "representation", "archive_path", "retirement_generation"}
            if payload.get("representation") == "compressed":
                if set(payload) != legacy_fields | {"object"}:
                    raise _unsupported("compressed payload is missing its closed object identity")
                if payload["state"] not in ("retained", "expired"):
                    raise _invalid("compressed payload state is outside retained/expired")
                if payload["archive_path"] != receipt["archive_path"]:
                    raise _invalid("compressed payload path differs from its native roll")
                if payload["retirement_generation"] is not None:
                    raise _invalid("compressed payload retirement generation is not closed")
                obj = payload["object"]
                if payload["state"] == "expired" and obj is None:
                    pass
                elif (isinstance(obj, dict) and set(obj) == {"relative_path", "byte_length", "sha256"}
                        and isinstance(obj["relative_path"], str) and obj["relative_path"]
                        and "/" not in obj["relative_path"] and obj["relative_path"] not in {".", ".."}
                        and _integer(obj["byte_length"]) and _hex(obj["sha256"])):
                    pass
                else:
                    raise _invalid("compressed object identity is missing or open")
            elif set(payload) != legacy_fields:
                raise _invalid("catalog payload descriptor has missing or open fields")
            elif (payload["representation"] != "legacy" or payload["state"] != "retained"
                    or payload["archive_path"] != receipt["archive_path"]
                    or payload["retirement_generation"] is not None):
                raise _invalid("legacy payload must retain its exact native path without a retirement generation")
            entries[key] = raw
            receipts[key] = sources.frame_bytes(frame)
            epochs.setdefault(row["epoch_id"], []).append(key)
            scopes[row["scope"]].append(key)
            retry[native_key] = key
        if canonical_chunk != chunk:
            raise _invalid("catalog chunk is not canonical JSONL")
    if seen_lineage != {key for key, _epoch in owner_epochs.values()}:
        raise _invalid("catalog omits an archived source lineage epoch")
    return CatalogData(tenant, entries, receipts, epochs, scopes, retry)

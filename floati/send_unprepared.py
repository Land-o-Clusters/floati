"""Validate unprepared native history once, without constructing a historical trie.

A fixed-size sealed Bloom filter proves only negatives. A possible match goes
back to the native frames. New sends use the ordinary authenticated append trie.
An explicit preparation replaces this temporary filter with exact frame pages.
"""
import hashlib
import json
import struct
import sys
import uuid

from . import jsonl
from .errors import IntegrityFailure
from .framing import decode_frames, encode_frame
from .send_prefix import CacheMiss, _EMPTY_HEAD, _atomic, _identity, _read

FILTER_BYTES = 512 * 1024
WORDS = struct.Struct(">IIII")
DOMAINS = {"id", "send", "retraction"}


def _bits(domain, key):
    digest = hashlib.sha256(domain.encode("ascii") + b"\0" + key.encode("utf-8")).digest()
    return ((value % (FILTER_BYTES * 8)) for value in WORDS.unpack_from(digest))


def _read_exact(prefix, size):
    with prefix.path.open("rb") as source:
        data = source.read(size)
    if len(data) != size:
        raise CacheMiss()
    return data


def report_validation(code, action, **progress):
    try:
        print(json.dumps({"command": "send", "code": code, "action": action, **progress,
            "remedy": "Run floati update prepare --root ROOT until preparation is done."}),
            file=sys.stderr, flush=True)
    except (OSError, ValueError):
        pass  # Diagnostic output cannot decide whether a native send succeeds.


def certify(prefix):
    from .events import validate_event_records
    from .send_prepare import _progress, _state
    cut = _progress(prefix)
    state = "in_progress" if cut["end"] else "pending"
    report_validation("send_preparation_" + state, "validating_native_history", preparation=_state(cut, state))
    before = cut["identity"]
    data = _read_exact(prefix, before[4])
    unknown = {}
    checked = jsonl._decode_path_records(prefix.path, prefix.root.tenant_id, prefix.kinds, data, unrecognized=unknown)
    validate_event_records(checked)
    physical = decode_frames(data) if unknown else checked
    bits = bytearray(FILTER_BYTES)
    head = bytes.fromhex(_EMPTY_HEAD)
    pending = 0
    first = None
    for row, frame in zip(physical, data.splitlines(keepends=True)):
        if first is None:
            first = [len(frame), hashlib.sha256(frame).hexdigest()]
        keys = [("id", row["id"])]
        kind = row["kind"]
        if kind == "message_envelope":
            keys.append(("send", row["idempotency_key"]))
        elif kind == "message_retracted":
            keys.append(("retraction", row["retracted_message_id"]))
        for domain, key in keys:
            for bit in _bits(domain, key):
                bits[bit >> 3] |= 1 << (bit & 7)
        if pending is not None:
            if kind not in {"message_envelope", "delivery_claim", "send_readiness_observation"} or encode_frame(row) != frame:
                pending = None
            elif kind == "message_envelope":
                pending += 1
        head = hashlib.sha256(head + frame).digest()
    if _identity(prefix.path) != before:
        raise IntegrityFailure("ledger_identity_changed_during_read", "events.jsonl changed during prefix validation")
    prefix.payload = {"binding": prefix.binding, "identity": before, "offset": len(data),
        "count": len(physical), "head": head.hex(), "trie": None, "mail_base_count": len(physical),
        "generation": uuid.uuid4().hex, "pending_only": pending, "first_frame": first,
        "filter": hashlib.sha256(bits).hexdigest(), "base_offset": len(data)}
    prefix.memo = {}
    _atomic(prefix.bank, bits)
    prefix.publish()


def lookup(prefix, domain, value):
    from .events import validate_event_records
    if domain == "mail-valid-v1":
        return False
    if domain == "mail-first-v1" or domain == "id":
        first = prefix.payload["first_frame"]
        if first is not None:
            data = _read_exact(prefix, first[0])
            if hashlib.sha256(data).hexdigest() != first[1]:
                raise CacheMiss()
            row = decode_frames(data + b"\n" if data.endswith(b"\r") else data)[0]
            if domain == "mail-first-v1" or row["id"] == value:
                return row
    if domain not in DOMAINS:
        return None
    bits = prefix.memo.get("absence_filter")
    if bits is None:
        bits = _read(prefix.bank, FILTER_BYTES)
        if len(bits) != FILTER_BYTES or hashlib.sha256(bits).hexdigest() != prefix.payload["filter"]:
            raise CacheMiss()
        prefix.memo["absence_filter"] = bits
    if any(not bits[bit >> 3] & (1 << (bit & 7)) for bit in _bits(domain, value)):
        return None
    # A Bloom collision, a historical reply and an exact replay all take the
    # native positive path. A false positive can only cost time.
    if "reported_filter_hit" not in prefix.memo:
        from .send_prepare import _progress
        cut = _progress(prefix)
        state = "in_progress" if cut["end"] else "pending"
        report_validation("send_preparation_" + state, "validating_historical_match", source_bytes=prefix.payload["base_offset"])
        prefix.memo["reported_filter_hit"] = True
    data = _read_exact(prefix, prefix.payload["base_offset"])
    unknown = {}
    rows = jsonl._decode_path_records(prefix.path, prefix.root.tenant_id, prefix.kinds, data, unrecognized=unknown)
    validate_event_records(rows)
    if domain == "id":
        return next((row for row in decode_frames(data) if row["id"] == value), None)
    if domain == "retraction":
        return next((row for row in rows if row["kind"] == "message_retracted"
                     and row["retracted_message_id"] == value), None)
    envelope = next((row for row in rows if row["kind"] == "message_envelope"
                     and row["idempotency_key"] == value), None)
    if envelope is None:
        return None
    group = [envelope]
    for kind, field in (("delivery_claim", "note_ref"), ("send_readiness_observation", "envelope_id"),
                        ("seat_execution", "envelope_id")):
        companion = next((row for row in rows if row["kind"] == kind and row[field] == envelope["id"]), None)
        if companion is not None:
            group.append(companion)
    return group

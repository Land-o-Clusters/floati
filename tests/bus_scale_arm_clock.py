"""Closed relative-clock materialization of native census fixture templates.

This constructs fixture bytes outside measured children. It is not a resend,
clock replacement, or permission to rewrite production ledgers.
"""
from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import tempfile

from floati.cursor import SparseCursor
from floati.events import validate_event_records
from floati.framing import decode_frames, encode_frame
from floati.jsonl import MAX_LEDGER_BYTES, MAX_LEDGER_RECORDS, MAX_RECORD_BYTES
from floati.records import validate_record
from floati.root import FloatiRoot


CASES = frozenset({"supervise", "watch"})
_EVENT_KINDS = frozenset({"message_envelope", "send_readiness_observation"})
_STAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{3}|\.\d{6})?Z\Z")
_TEMP = Path("\x2fprivate/tmp" if os.uname().sysname == "Darwin" else "\x2ftmp")


def _require(condition, detail):
    if not condition:
        raise ValueError("attention seed clock: " + detail)


def _json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def _digest(raw):
    return hashlib.sha256(raw).hexdigest()


def _ordinary(path, *, directory=None):
    path = Path(path)
    _require(path.is_absolute() and _TEMP in path.parents and path.resolve(strict=True) == path,
             "coordinate must be canonical below native temporary storage")
    for part in (path, *path.parents):
        if part == _TEMP:
            break
        info = part.lstat()
        _require(info.st_uid == os.getuid() and not stat.S_ISLNK(info.st_mode)
                 and not info.st_mode & 0o022, "coordinate must be ordinary owned and non-writable by others")
    info = path.lstat()
    if directory is True:
        _require(stat.S_ISDIR(info.st_mode), "expected directory")
    elif directory is False:
        _require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1, "expected ordinary unshared file")
    return path


def _files(home):
    home = _ordinary(home, directory=True)
    result = {}
    for path in sorted(home.rglob("*")):
        _ordinary(path)
        info = path.lstat()
        if stat.S_ISDIR(info.st_mode):
            continue
        _ordinary(path, directory=False)
        result[path.relative_to(home).as_posix()] = path.read_bytes()
    return result


def _inventory(home, files=None):
    files = _files(home) if files is None else files
    return {relative: {"bytes": len(raw), "sha256": _digest(raw),
                       "mode": stat.S_IMODE((home / relative).lstat().st_mode)}
            for relative, raw in files.items()}


def _time(value):
    _require(type(value) is str, "clock must be text")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    _require(parsed.tzinfo is not None and parsed.utcoffset() == timedelta(0), "clock must be UTC")
    return parsed


def _shift(value, offset_ms):
    _require(type(value) is str and _STAMP.fullmatch(value), "event clock precision is unsupported")
    shifted = _time(value) + timedelta(milliseconds=offset_ms)
    precision = "microseconds" if "." in value and len(value.split(".")[1][:-1]) == 6 else (
        "milliseconds" if "." in value else "seconds")
    # A seconds-only source cannot represent a fractional-second translation.
    _require(precision != "seconds" or offset_ms % 1000 == 0, "translation would lose native clock precision")
    return shifted.isoformat(timespec=precision).replace("+00:00", "Z")


def _events(raw, tenant):
    _require(len(raw) <= MAX_LEDGER_BYTES, "event history exceeds native byte bound")
    rows = decode_frames(raw)
    _require(len(rows) <= MAX_LEDGER_RECORDS and all(len(encode_frame(row)) - 1 <= MAX_RECORD_BYTES for row in rows),
             "event history exceeds native frame bounds")
    _require(b"".join(encode_frame(row) for row in rows) == raw, "events are not exactly canonical frames")
    for row in rows:
        validate_record(row, tenant, _EVENT_KINDS, integrity=True)
    _require(len({row["id"] for row in rows}) == len(rows), "event history repeats a record identity")
    validate_event_records(rows)
    _require(any(row["kind"] == "message_envelope" for row in rows), "native message preface is absent")
    return rows


def _profile(home, files, tenant):
    """Accept only the actually prepared attention histories and inert locks."""
    events = _events(files["events.jsonl"], tenant)
    registry = decode_frames(files["registry/entries.jsonl"])
    _require(b"".join(encode_frame(row) for row in registry) == files["registry/entries.jsonl"],
             "registry is not canonical")
    for row in registry:
        validate_record(row, tenant, {"registry_entry", "provider_switch_receipt"}, integrity=True)
    _require(len({row["id"] for row in registry}) == len(registry), "registry repeats a record identity")
    hints = set()
    if registry or any(not isinstance(row.get("recipient"), str) for row in events):
        hints.add("state/wake-hints/root-generation")
    hints.update("state/wake-hints/" + row["recipient"] + "/generation"
                 for row in events if row["kind"] == "message_envelope")
    inert = {"events.jsonl.lock", "registry/entries.jsonl.lock"}
    for relative, raw in files.items():
        if relative in {"events.jsonl", "registry/entries.jsonl"}:
            continue
        if relative in hints:
            from tests.fixtures.bus_scale_census.bus_scale_wake_hint_effects import _metadata
            _require(raw == b"", "native wake hint is not empty")
            _metadata(home, relative)
            continue
        parts = Path(relative).parts
        if (len(parts) == 4 and parts[:2] == ("receipts", "wake-coordination")
                and parts[-1] == "lane.lock") or relative in inert:
            _require(raw == b"", "coordination lock is not empty")
            continue
        if relative in {"receipts/deliveries/architect.jsonl", "receipts/acks/architect.jsonl",
                        "receipts/codex-wait-exhaustion/scale-worker.jsonl"}:
            kinds = ({"delivery_receipt", "delivered_unpresented"} if "/deliveries/" in relative else
                     {"ack_receipt"} if "/acks/" in relative else {"codex_wait_exhaustion_receipt"})
            rows = decode_frames(raw)
            _require(b"".join(encode_frame(row) for row in rows) == raw, "padding sidecar is not canonical")
            for row in rows:
                validate_record(row, tenant, kinds, integrity=True)
            continue
        if relative in {"receipts/deliveries/architect.jsonl.lock", "receipts/acks/architect.jsonl.lock",
                        "receipts/codex-wait-exhaustion/scale-worker.jsonl.lock"}:
            _require(raw == b"", "padding lock is not empty")
            continue
        _require(False, "unsupported positive sidecar or fixture profile: " + relative)
    messages = [row for row in events if row["kind"] == "message_envelope"]
    _require(all(row.get("worker_session_id") is None for row in messages), "worker-plane profile is unsupported")
    root = FloatiRoot.open_direct_home(home, create=False)
    cursor = SparseCursor(root)
    for node in {row["recipient"] for row in messages}:
        cursor.state_for(node, messages)
    _require(_files(home) == files, "native profile validation changed template bytes")
    return events


def _translated(rows, offset_ms):
    changed, leaves = copy.deepcopy(rows), []
    for ordinal, row in enumerate(changed):
        pointers = (("timestamp",),) if row["kind"] == "message_envelope" else (
            ("timestamp",), ("observed_at",), ("readiness", "observed_at"))
        for parts in pointers:
            target = row
            for part in parts[:-1]:
                target = target[part]
            before = target[parts[-1]]
            after = _shift(before, offset_ms)
            target[parts[-1]] = after
            leaves.append({"path": "events.jsonl", "ordinal": ordinal, "kind": row["kind"],
                           "record_id": row["id"], "pointer": "/" + "/".join(parts),
                           "before": before, "after": after})
    validate_event_records(changed)
    return b"".join(encode_frame(row) for row in changed), leaves


def materialize_attention_pair(pair, case, arm_bank):
    _require(case in CASES and type(pair) is dict, "unsupported case or pair")
    _require({"context", "prepared_home", "preparation_bank", "1", "10", "ledger_relative"} <= set(pair)
             and "clock_materialization" not in pair, "expected an immutable unmaterialized template pair")
    context = pair["context"]
    _require(type(context) is dict and context.get("case") == case and "canonical_time" in context,
             "native attention context is absent")
    _require(set(context) == {"case", "seed_home", "workspace", "bank", "canonical_time", "path_bindings"}
             and context["path_bindings"] == [], "unsupported attention context profile")
    arm_bank = _ordinary(arm_bank, directory=True)
    _require(not any(arm_bank.iterdir()), "arm publication bank must be empty")
    prepared = _ordinary(pair["prepared_home"], directory=True)
    preparation = _ordinary(pair["preparation_bank"], directory=True)
    parent = preparation.parent
    _require(prepared != arm_bank and prepared not in arm_bank.parents and arm_bank not in prepared.parents
             and parent != arm_bank and parent not in arm_bank.parents and arm_bank not in parent.parents,
             "arm bank overlaps preparation coordinates")
    _require(context["seed_home"] == str(prepared) and context["bank"] == str(preparation),
             "context does not bind native preparation coordinates")
    _ordinary(context["workspace"], directory=True)
    templates, rows = {}, {}
    for scale in ("1", "10"):
        home = _ordinary(pair[scale], directory=True)
        _require(home.parent.name == scale and home.parent.parent == parent and home.name == prepared.name,
                 "template does not occupy its declared preparation sibling")
        _require(home != arm_bank and home not in arm_bank.parents and arm_bank not in home.parents,
                 "arm bank overlaps an immutable template")
        files = _files(home)
        root = FloatiRoot.open_direct_home(home, create=False)
        rows[scale] = _profile(home, files, root.tenant_id)
        templates[scale] = files
    relative = pair["ledger_relative"]
    _require(relative in {"events.jsonl", "registry/entries.jsonl",
                          "receipts/deliveries/architect.jsonl",
                          "receipts/codex-wait-exhaustion/scale-worker.jsonl"}, "unknown primary family")
    _require(relative in templates["1"] and relative in templates["10"]
             and templates["10"][relative].startswith(templates["1"][relative]), "original prefix proof failed")
    _require({k: v for k, v in templates["1"].items() if k != relative}
             == {k: v for k, v in templates["10"].items() if k != relative}, "original ancillary equality failed")
    counts = pair.get("record_counts", {"1": 1000, "10": 10000})
    bounded = pair.get("bounded_fixture", False)
    _require(type(counts) is dict and set(counts) == {"1", "10"}
             and all(type(value) is int for value in counts.values())
             and type(bounded) is bool and 0 < counts["1"] < counts["10"] <= 10000
             and (bounded or counts == {"1": 1000, "10": 10000}),
             "primary history lacks an exact bounded fixture or full census declaration")
    _require(all(len(decode_frames(templates[scale][relative])) == counts[scale]
                 for scale in ("1", "10")), "primary history differs from its exact declaration")
    anchor = datetime.now(timezone.utc)
    origin = _time(context["canonical_time"])
    difference = anchor - origin
    offset_ms = (difference.days * 86400000000 + difference.seconds * 1000000 + difference.microseconds) // 1000
    target_origin = origin + timedelta(milliseconds=offset_ms)
    transformed = {scale: _translated(rows[scale], offset_ms) for scale in ("1", "10")}
    # Preflight every inverse before creating detached files or a receipt.
    for scale in ("1", "10"):
        inverse, _leaves = _translated(decode_frames(transformed[scale][0]), -offset_ms)
        _require(inverse == templates[scale]["events.jsonl"], "inverse event frames differ from native template")
    detached = copy.deepcopy(pair)
    detached["context"]["canonical_time"] = target_origin.isoformat()
    destination = Path(tempfile.mkdtemp(prefix="arm-clock-", dir=parent))
    receipt = {"version": 1, "case": case, "real_anchor": anchor.isoformat(),
               "source_canonical_time": context["canonical_time"], "canonical_time": target_origin.isoformat(),
               "offset_ms": offset_ms, "original_primary": relative, "original_prefix_exact": True, "scales": {}}
    for scale in ("1", "10"):
        source = Path(pair[scale])
        home = destination / scale / prepared.name
        home.parent.mkdir(mode=0o700)
        shutil.copytree(source, home, copy_function=shutil.copy2)
        raw, leaves = transformed[scale]
        events_path = home / "events.jsonl"
        mode = stat.S_IMODE(events_path.stat().st_mode)
        events_path.write_bytes(raw)
        events_path.chmod(mode)
        actual = _files(home)
        expected = dict(templates[scale], **{"events.jsonl": raw})
        _require(actual == expected, "detached fixture changed an unreviewed byte")
        root = FloatiRoot.open_direct_home(home, create=False)
        _profile(home, actual, root.tenant_id)
        inverse, _inverse_leaves = _translated(_events(raw, root.tenant_id), -offset_ms)
        inverse_files = dict(actual, **{"events.jsonl": inverse})
        _require(inverse_files == templates[scale], "detached inverse inventory differs")
        _require(_files(source) == templates[scale], "immutable source changed during materialization")
        detached[scale] = home
        receipt["scales"][scale] = {"template": str(source), "materialized": str(home),
            "primary_records": len(decode_frames(actual[relative])), "event_records": len(rows[scale]),
            "template_inventory": _inventory(source, templates[scale]), "materialized_inventory": _inventory(home, actual),
            "inverse_inventory": _inventory(source, inverse_files), "changed_leaves": leaves,
            "native_validators": {"records": True, "event_cross_validation": True, "cursor_cross_validation": True}}
    receipt_path = arm_bank / "seed-clock.json"
    raw_receipt = (_json(receipt) + "\n").encode()
    with receipt_path.open("xb") as stream:
        stream.write(raw_receipt)
    _ordinary(receipt_path, directory=False)
    _require(receipt_path.read_bytes() == raw_receipt, "receipt readback differs")
    detached["clock_materialization"] = {"path": str(receipt_path), "sha256": _digest(raw_receipt)}
    return detached


def publish_attention_context(path, original_context, materialized_context):
    """Publish only the known context clock transition at the stable root."""
    _require(type(original_context) is dict and type(materialized_context) is dict
             and original_context.get("case") in CASES
             and set(original_context) == {"case", "seed_home", "workspace", "bank", "canonical_time", "path_bindings"}
             and original_context["path_bindings"] == []
             and set(original_context) == set(materialized_context)
             and {k: v for k, v in original_context.items() if k != "canonical_time"}
             == {k: v for k, v in materialized_context.items() if k != "canonical_time"},
             "context transition changes a non-clock field")
    _time(original_context["canonical_time"])
    _time(materialized_context["canonical_time"])
    path = Path(path)
    _ordinary(path.parent, directory=True)
    seed_home = _ordinary(original_context["seed_home"], directory=True)
    _require(path == seed_home.parent / "case-context.json", "unexpected stable context coordinate")
    desired = (_json(materialized_context) + "\n").encode()
    if os.path.lexists(path):
        _ordinary(path, directory=False)
        raw = path.read_bytes()
        current = json.loads(raw)
        _require(raw == (_json(current) + "\n").encode(), "existing context is not canonical JSON")
        _require(current in (original_context, materialized_context),
                 "existing context is not either known arm context")
        _time(current["canonical_time"])
        if raw == desired:
            return
        with path.open("r+b") as stream:
            stream.write(desired)
            stream.truncate()
    else:
        with path.open("xb") as stream:
            stream.write(desired)
    _ordinary(path, directory=False)
    _require(path.read_bytes() == desired, "published context readback differs")

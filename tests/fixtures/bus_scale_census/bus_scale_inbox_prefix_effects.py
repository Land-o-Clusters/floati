"""Read-only complete event-index testimony for the one ruled inbox artifact."""
from __future__ import annotations

import importlib.util as _fixture_proof_util
from pathlib import Path as _FixtureProofPath

_fixture_proof_specification = _fixture_proof_util.spec_from_file_location(
    "_bus_scale_fixture_proof_bootstrap", _FixtureProofPath(__file__).with_name("_proof_namespace.py"))
_fixture_proof_bootstrap = _fixture_proof_util.module_from_spec(_fixture_proof_specification)
_fixture_proof_specification.loader.exec_module(_fixture_proof_bootstrap)
_fixture_proof_bootstrap.bind()


import copy
import hashlib
import json
from pathlib import Path

from floati import records, send_prefix
from floati.events import EVENT_KINDS, validate_event_records
from floati.inbox_prefix import storage_declaration
from floati.root import FloatiRoot
from _bus_scale_fixture_proofs import bus_scale_cursor_prefix_effects as _keys

_base = _keys._base
CACHE = storage_declaration()["index"]["path"]
RELATIVE = storage_declaration()["source"]["path"]
SUMMARY = "@cache/inbox-prefix"
TAG = {"operation": "native_cache_rebuild", "kind": "complete_native_inbox_prefix"}


def require(value, detail):
    if not value:
        raise ValueError("native inbox prefix effect: " + detail)


def capture_inbox_prefix_input(home, before):
    declaration = storage_declaration()
    CACHE, RELATIVE = declaration["index"]["path"], declaration["source"]["path"]
    root = FloatiRoot.open_direct_home(Path(home))
    raw = before.get(RELATIVE, b"")
    path = root.tenant_home / RELATIVE
    require((path.read_bytes() if path.exists() else b"") == raw,
        "pre-child event source differs from retained bytes")
    cache = _base._cache(before, CACHE)
    metadata = _base._metadata(root.tenant_home, CACHE)
    for relative, data in cache.items():
        require((root.tenant_home / relative).read_bytes() == data, "pre-child bank changed")
    return {"root": str(root.tenant_home), "relative": RELATIVE, "storage_declaration": declaration,
        "ledger_identity": send_prefix._identity(path), "source_sha256": _base.sha(raw),
        "checkpoint_sha256": (_base.sha(cache[CACHE + "/checkpoint.json"])
            if CACHE + "/checkpoint.json" in cache else None), "cache_metadata": metadata}


def verify_inbox_prefix_effect(home, before, after, delta, prefix_input=None):
    """Certify every event map member without constructing or repairing a bank."""
    declaration = storage_declaration()
    CACHE, RELATIVE = declaration["index"]["path"], declaration["source"]["path"]
    root = FloatiRoot.open_direct_home(Path(home))
    require(type(prefix_input) is dict and set(prefix_input) == {
        "root", "relative", "ledger_identity", "source_sha256",
        "checkpoint_sha256", "cache_metadata", "storage_declaration"}, "missing exact pre-child testimony")
    require(prefix_input["storage_declaration"] == declaration, "writer storage declaration changed")
    require(prefix_input["root"] == str(root.tenant_home) and prefix_input["relative"] == RELATIVE,
        "foreign root or event coordinate")
    require(type(prefix_input["cache_metadata"]) is dict, "missing private input metadata")
    raw = after.get(RELATIVE, b"")
    require(raw == before.get(RELATIVE, b"") and _base.sha(raw) == prefix_input["source_sha256"],
        "read-side event index changed its authoritative source")
    path = root.tenant_home / RELATIVE
    identity = send_prefix._identity(path)
    require(identity == prefix_input["ledger_identity"]
        and (path.read_bytes() if path.exists() else b"") == raw, "event identity or bytes changed")
    old_cache, cache = _base._cache(before, CACHE), _base._cache(after, CACHE)
    checkpoint = old_cache.get(CACHE + "/checkpoint.json")
    require(prefix_input["checkpoint_sha256"] == (
        _base.sha(checkpoint) if checkpoint is not None else None), "prior checkpoint changed")
    if old_cache:
        _base._check_metadata(prefix_input["cache_metadata"], old_cache, CACHE, bucketed=True)
    else:
        require(not prefix_input["cache_metadata"], "absent bank has unexplained metadata")
    if CACHE + "/seal.key" in old_cache:
        require(cache.get(CACHE + "/seal.key") == old_cache[CACHE + "/seal.key"],
            "native rebuild changed its retained sealing key")
    metadata = _base._metadata(root.tenant_home, CACHE)
    _base._check_metadata(metadata, cache, CACHE, bucketed=True)
    for relative, data in cache.items():
        require((root.tenant_home / relative).read_bytes() == data, "post-child bank changed")
    rows = _keys._transcript(root, RELATIVE, frozenset(EVENT_KINDS), raw)
    validate_event_records(rows)
    expected, folds = _event_map(rows)
    payload, _map = _keys._checkpoint(cache, CACHE, raw, rows, identity,
        _binding(root), expected, folds=folds)
    changed = sorted(relative for relative in set(old_cache) | set(cache)
        if old_cache.get(relative) != cache.get(relative))
    candidate = _base.raw_candidate_delta(before, after)
    require({relative: delta.get(relative) for relative in changed}
        == {relative: candidate[relative] for relative in changed}, "cache delta differs from exact bytes")
    require({relative for relative in delta if relative == CACHE or relative.startswith(CACHE + "/")}
        == set(changed), "unexplained cache delta coordinate")
    require(send_prefix._identity(path) == identity
        and _base._metadata(root.tenant_home, CACHE) == metadata, "source or bank changed during proof")
    return {"kind": TAG["kind"], "event_records": len(rows), "changed_files": changed,
        "source_sha256": _base.sha(raw), "source_identity": identity,
        "head": payload["head"], "complete_map_entries": len(expected)}


def _file_identities(home, cache):
    result = {}
    for relative in cache:
        info = (Path(home) / relative).lstat()
        result[relative] = [info.st_dev, info.st_ino]
    return result


def capture_inbox_prefix_append_input(home, before):
    """Declare a warm index append with its exact pre-publication file inodes."""
    captured = capture_inbox_prefix_input(home, before)
    CACHE = captured["storage_declaration"]["index"]["path"]
    cache = _base._cache(before, CACHE)
    require(bool(cache), "append declaration requires a complete warmed inbox bank")
    verify_inbox_prefix_effect(home, before, before, {}, captured)
    identities = _file_identities(home, cache)
    require(all((Path(home) / path).read_bytes() == raw for path, raw in cache.items())
        and send_prefix._identity(Path(home) / RELATIVE) == captured["ledger_identity"],
        "append input changed during capture")
    return dict(captured, cache_identities=identities)


def _event_map(rows):
    from _bus_scale_fixture_proofs.bus_scale_cut_prefix_effects import _midstate
    id_stream = bytearray(b"floati-seat-teardown-prefix-v2\0events.jsonl\0")
    digest = hashlib.sha256(id_stream)
    partial, executions = [], 0
    expected, counts = {}, {}
    if rows:
        expected[_base._key("first", "physical")] = rows[0]
    for position, row in enumerate(rows, 1):
        identity = row["id"].encode("utf-8")
        data = len(identity).to_bytes(4, "big") + identity
        digest.update(data)
        id_stream.extend(data)
        partial.append([row["id"], digest.hexdigest()])
        if position % 32 == 0:
            expected[_base._key("teardown-ids", str(position // 32 - 1))] = partial
            partial = []
        if row["kind"] == "message_envelope":
            recipient = row["recipient"]
            ordinal = counts.get(recipient, 0)
            key = json.dumps([recipient, ordinal], ensure_ascii=True, separators=(",", ":"))
            expected[_base._key("recipient", key)] = row
            expected[_base._key("message", row["id"])] = [position, row]
            counts[recipient] = ordinal + 1
        elif row["kind"] == "message_retracted":
            expected[_base._key("retracted", row["retracted_message_id"])] = row
            expected[_base._key("retracted-at", row["retracted_message_id"])] = position
        elif row["kind"] == "seat_execution":
            expected[_base._key("execution", str(executions))] = row
            executions += 1
    state, tail = _midstate(id_stream)
    folds = {"teardown": {"state": state, "length": len(id_stream),
        "tail": tail.hex(), "partial": partial, "executions": executions}}
    return expected, folds


def _binding(root):
    binding = _keys._binding(root, RELATIVE, EVENT_KINDS, "inbox-prefix-v1")
    binding["revision"] = {"source": send_prefix._revision(), "reader_version": records.READER_VERSION}
    binding["folds"] = {"teardown": 1}
    return binding


def verify_inbox_prefix_append_effect(home, before, after, delta, prefix_input=None):
    """Admit only a complete native insertion with atomic derived-file replacements.

    The event ledger remains append-only. directory.bin and affected bucket
    pages are replaceable membership indexes; retained entry values are immutable.
    This declaration is separate from the unchanged-source read/rebuild contract.
    """
    declaration = storage_declaration()
    CACHE, RELATIVE = declaration["index"]["path"], declaration["source"]["path"]
    root = FloatiRoot.open_direct_home(Path(home))
    require(type(prefix_input) is dict and set(prefix_input) == {
        "root", "relative", "ledger_identity", "source_sha256",
        "checkpoint_sha256", "cache_metadata", "cache_identities", "storage_declaration"},
        "missing exact declared append input")
    require(prefix_input["storage_declaration"] == declaration, "writer storage declaration changed")
    require(prefix_input["root"] == str(root.tenant_home) and prefix_input["relative"] == RELATIVE,
        "foreign append root or source coordinate")
    old_raw, raw = before.get(RELATIVE, b""), after.get(RELATIVE, b"")
    require(raw.startswith(old_raw) and len(raw) > len(old_raw)
        and _base.sha(old_raw) == prefix_input["source_sha256"],
        "native append rewrote or omitted its retained event prefix")
    old_cache, cache = _base._cache(before, CACHE), _base._cache(after, CACHE)
    require(bool(old_cache), "append requires a complete warmed input bank")
    _base._check_metadata(prefix_input["cache_metadata"], old_cache, CACHE, bucketed=True)
    require(_base.sha(old_cache[CACHE + "/checkpoint.json"]) == prefix_input["checkpoint_sha256"],
        "append input checkpoint differs from retained bytes")
    identities = prefix_input["cache_identities"]
    require(type(identities) is dict and set(identities) == set(old_cache)
        and all(type(value) is list and len(value) == 2
            and all(type(number) is int and number >= 0 for number in value)
            for value in identities.values()), "append lacks exact prior cache inode testimony")
    old_rows = _keys._transcript(root, RELATIVE, frozenset(EVENT_KINDS), old_raw)
    rows = _keys._transcript(root, RELATIVE, frozenset(EVENT_KINDS), raw)
    validate_event_records(old_rows)
    validate_event_records(rows)
    old_map, old_folds = _event_map(old_rows)
    current_map, folds = _event_map(rows)
    binding = _binding(root)
    old_payload, _ = _keys._checkpoint(old_cache, CACHE, old_raw, old_rows,
        prefix_input["ledger_identity"], binding, old_map, folds=old_folds)
    source = root.tenant_home / RELATIVE
    identity = send_prefix._identity(source)
    require(identity is not None and (old_payload["identity"] is None
        or identity[:4] == old_payload["identity"][:4])
        and source.read_bytes() == raw, "native append replaced its source inode or changed live bytes")
    payload, _ = _keys._checkpoint(cache, CACHE, raw, rows, identity, binding, current_map, folds=folds)
    require(cache[CACHE + "/seal.key"] == old_cache[CACHE + "/seal.key"]
        and payload["generation"] == old_payload["generation"],
        "append changed its retained seal or immutable generation")
    require(set(old_map) <= set(current_map)
        and all(current_map[key] == value for key, value in old_map.items()),
        "append changed an immutable retained map member")
    fresh = set(current_map) - set(old_map)
    require(bool(fresh), "declared inbox append added no membership evidence")
    expected_cache = dict(old_cache)
    for key in fresh:
        path = CACHE + "/nodes/" + format(int(key, 2), "064x")
        expected_cache[path] = cache[path]
    directory, pages = _base._bucket_map(current_map, cache[CACHE + "/seal.key"], payload["generation"])
    expected_cache[CACHE + "/directory.bin"] = directory
    expected_cache.update({CACHE + "/buckets/" + index: page for index, page in pages.items()})
    expected_cache[CACHE + "/checkpoint.json"] = cache[CACHE + "/checkpoint.json"]
    require(expected_cache == cache, "bank differs from the exact native insertion delta")
    changed = sorted(path for path in set(old_cache) | set(cache) if old_cache.get(path) != cache.get(path))
    candidate = _base.raw_candidate_delta(before, after)
    require({path for path in delta if path == CACHE or path.startswith(CACHE + "/")} == set(changed)
        and all(delta[path] == candidate[path] for path in changed),
        "append cache delta differs from its complete exact bytes")
    metadata = _base._metadata(root.tenant_home, CACHE)
    _base._check_metadata(metadata, cache, CACHE, bucketed=True)
    current_identities = _file_identities(root.tenant_home, cache)
    for path in sorted(cache):
        relative = Path(path).relative_to(CACHE)
        role = declaration["index"]["files"].get(relative.as_posix())
        if role is None and len(relative.parts) == 2:
            role = declaration["index"]["members"].get(relative.parts[0])
        require(role in {"derived_atomic_replace", "derived_replaceable", "retained_immutable"},
            "undeclared native index file: " + path)
        if path not in identities:
            continue
        prior = identities[path]
        if path in changed and role == "derived_atomic_replace":
            require(current_identities[path] != prior,
                "changed derived cache file was overwritten in place: " + path)
        elif role == "retained_immutable":
            require(cache[path] == old_cache[path], "retained immutable cache bytes changed: " + path)
            require(current_identities[path] == prior, "retained immutable cache inode was replaced: " + path)
    require(all((root.tenant_home / path).read_bytes() == value for path, value in cache.items()),
        "live appended cache differs from retained bytes")
    require(send_prefix._identity(source) == identity
        and _base._metadata(root.tenant_home, CACHE) == metadata
        and _file_identities(root.tenant_home, cache) == current_identities,
        "source or append bank changed during proof")
    batch = rows[len(old_rows):]
    semantic = {"operation": "verified_inbox_prefix_append", "format": "inbox-prefix-v1",
        "before_records": len(old_rows), "after_records": len(rows), "appended_records": len(batch),
        "appended_kinds": [row["kind"] for row in batch],
        "complete_transcripts_and_maps_validated": True, "both_checkpoint_seals_validated": True,
        "exact_insertion_delta_validated": True, "atomic_directory_replacement_validated": True}
    replaced = sorted(path for path, prior in identities.items() if current_identities[path] != prior)
    return {"semantic": semantic, "changed_files": changed, "replaced_files": replaced,
        "before_input": copy.deepcopy(prefix_input),
        "after_identity": identity, "cache_before": _base._inventory(old_cache),
        "cache_after": _base._inventory(cache)}


def project_inbox_prefix_tree(before, after, proof, begin, end, *, filesystem_clock=None):
    """Bind every admitted inbox tree byte and mutation clock to its append proof."""
    from tests.bus_scale_filesystem_clock import filesystem_bounds
    lower, upper = filesystem_bounds(begin, end, filesystem_clock)

    CACHE = proof["before_input"]["storage_declaration"]["index"]["path"]
    old, new = before["entries"], after["entries"]
    for entries, inventory in ((old, proof["cache_before"]), (new, proof["cache_after"])):
        selected = {path: row for path, row in entries.items() if path == CACHE or path.startswith(CACHE + "/")}
        files = {path for path, row in selected.items() if row["kind"] == "file"}
        require(files == set(inventory), "inbox tree file population differs from raw proof")
        for path in files:
            require(selected[path]["mode"] == 0o600 and selected[path]["bytes"] == inventory[path]["bytes"]
                and selected[path]["sha256"] == inventory[path]["sha256"],
                "inbox tree file differs from raw byte proof")
        directories = {path for path, row in selected.items() if row["kind"] == "directory"}
        require(directories == {CACHE, CACHE + "/nodes", CACHE + "/staging", CACHE + "/buckets"}
            and all(selected[path]["mode"] == 0o700 for path in directories),
            "inbox tree directory population differs")
    for path in set(old) | set(new):
        if path != CACHE and not path.startswith(CACHE + "/"):
            continue
        require(path in new, "native inbox append removed a retained tree entry")
        if (path in old and path not in proof["changed_files"] and path not in proof["replaced_files"]
                and new[path]["kind"] == "file"):
            require(new[path] == old[path], "native append changed retained inbox file metadata")
        if path not in old or new[path]["mtime_ns"] != old[path]["mtime_ns"]:
            stamp = new[path]["mtime_ns"]
            require(type(stamp) is int and lower <= stamp <= upper,
                "inbox mutation clock is outside the native invocation: " + path)
    return {"kind": "complete_validated_native_inbox_prefix_cache", "effect": copy.deepcopy(proof["semantic"])}

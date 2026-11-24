"""Read-only registry rebuild testimony through the existing KeyPrefix proof."""
from __future__ import annotations

import importlib.util as _fixture_proof_util
from pathlib import Path as _FixtureProofPath

_fixture_proof_specification = _fixture_proof_util.spec_from_file_location(
    "_bus_scale_fixture_proof_bootstrap", _FixtureProofPath(__file__).with_name("_proof_namespace.py"))
_fixture_proof_bootstrap = _fixture_proof_util.module_from_spec(_fixture_proof_specification)
_fixture_proof_specification.loader.exec_module(_fixture_proof_bootstrap)
_fixture_proof_bootstrap.bind()


from datetime import datetime, timezone
from pathlib import Path
import os
import stat

from floati import records, send_prefix
from floati.registry import REGISTRY_KINDS
from floati.root import FloatiRoot
from _bus_scale_fixture_proofs import bus_scale_cursor_prefix_effects as _keys

_base = _keys._base
CACHE = ".registry-prefix"
RELATIVE = "registry/entries.jsonl"
SUMMARY = "@cache/registry-prefix"
TAG = {"operation": "native_cache_rebuild", "kind": "complete_native_registry_prefix"}


def require(value, detail):
    if not value:
        raise ValueError("native registry prefix effect: " + detail)


def capture_registry_prefix_input(home, before):
    root = FloatiRoot.open_direct_home(Path(home))
    raw = before.get(RELATIVE, b"")
    path = root.tenant_home / RELATIVE
    require((path.read_bytes() if path.exists() else b"") == raw,
        "pre-child registry bytes differ from complete retained input")
    cache = _base._cache(before, CACHE)
    metadata = _base._metadata(root.tenant_home, CACHE)
    for relative, data in cache.items():
        require((root.tenant_home / relative).read_bytes() == data, "pre-child cache bytes changed")
    return {"root": str(root.tenant_home), "relative": RELATIVE,
        "ledger_identity": send_prefix._identity(path), "source_sha256": _base.sha(raw),
        "checkpoint_sha256": (_base.sha(cache[CACHE + "/checkpoint.json"])
            if CACHE + "/checkpoint.json" in cache else None), "cache_metadata": metadata}


def verify_registry_prefix_effect(home, before, after, delta, prefix_input=None):
    """Admit only complete native read-side rebuilds of this single artifact.

    The prior copied bank may be stale. It is retained input, never a fact.
    No cache constructor, warmer, repair or new cache-format decoder runs here.
    """
    root = FloatiRoot.open_direct_home(Path(home))
    require(type(prefix_input) is dict and set(prefix_input) == {
        "root", "relative", "ledger_identity", "source_sha256",
        "checkpoint_sha256", "cache_metadata"}, "missing exact pre-child registry testimony")
    require(prefix_input["root"] == str(root.tenant_home) and prefix_input["relative"] == RELATIVE,
        "foreign registry root or coordinate")
    require(type(prefix_input["cache_metadata"]) is dict,
        "pre-child cache metadata is not a complete object")
    old_raw, raw = before.get(RELATIVE, b""), after.get(RELATIVE, b"")
    require(raw == old_raw and _base.sha(raw) == prefix_input["source_sha256"],
        "read-side rebuild changed the authoritative registry transcript")
    path = root.tenant_home / RELATIVE
    identity = send_prefix._identity(path)
    require(identity == prefix_input["ledger_identity"]
        and (path.read_bytes() if path.exists() else b"") == raw,
        "native registry identity or bytes changed across invocation")
    old_cache, cache = _base._cache(before, CACHE), _base._cache(after, CACHE)
    prior_checkpoint = old_cache.get(CACHE + "/checkpoint.json")
    require(prefix_input["checkpoint_sha256"] == (
        _base.sha(prior_checkpoint) if prior_checkpoint is not None else None),
        "retained prior checkpoint changed")
    if old_cache:
        _base._check_metadata(prefix_input["cache_metadata"], old_cache, CACHE, bucketed=True)
    else:
        require(not prefix_input["cache_metadata"], "absent prior bank has unexplained metadata")
    if CACHE + "/seal.key" in old_cache:
        require(cache.get(CACHE + "/seal.key") == old_cache[CACHE + "/seal.key"],
            "native rebuild changed an existing sealing key")
    metadata = _base._metadata(root.tenant_home, CACHE)
    _base._check_metadata(metadata, cache, CACHE, bucketed=True)
    for relative, data in cache.items():
        require((root.tenant_home / relative).read_bytes() == data,
            "post-child cache differs from retained complete bytes")
    rows = _keys._transcript(root, RELATIVE, frozenset(REGISTRY_KINDS), raw)
    expected, latest, timestamp_order = {}, {}, []
    for row in rows:
        if row["kind"] == "registry_entry":
            expected[_base._key("registration", row["node_id"])] = row
            latest[row["node_id"]] = row
        elif row["kind"] == "node_lease":
            expected[_base._key("lease", row["node_id"])] = row
        elif row["kind"] == "registry_role_record":
            expected[_base._key("role", row["node_id"])] = row
        text = row["timestamp"]
        require(type(text) is str, "native registry timestamp is not text")
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        require(parsed.tzinfo is not None and parsed.utcoffset() is not None,
            "native registry timestamp has no UTC offset")
        timestamp_order.append((parsed.astimezone(timezone.utc), text))
    expected[_base._key("catalog", "active")] = sorted(
        node for node, row in latest.items() if row["state"] == "active")
    expected[_base._key("roster", "active")] = [[node, row["role"],
        expected.get(_base._key("role", node), {}).get("template_role"),
        expected.get(_base._key("role", node), {}).get("state")]
        for node, row in sorted(latest.items()) if row["state"] == "active"]
    folds = {"max_timestamp": max(timestamp_order)[1] if timestamp_order else None}
    binding = _keys._binding(root, RELATIVE, REGISTRY_KINDS, "registry-prefix-v2")
    binding["revision"] = {"source": send_prefix._revision(), "reader_version": records.READER_VERSION}
    binding["folds"] = {"max_timestamp": "chart-time-text-v1"}
    payload, _map = _keys._checkpoint(cache, CACHE, raw, rows, identity, binding, expected, folds=folds)
    changed = sorted(relative for relative in set(old_cache) | set(cache)
        if old_cache.get(relative) != cache.get(relative))
    candidate = _base.raw_candidate_delta(before, after)
    require({relative: delta.get(relative) for relative in changed}
        == {relative: candidate[relative] for relative in changed},
        "raw registry cache delta does not bind the complete before/after bytes")
    require(send_prefix._identity(path) == identity
        and _base._metadata(root.tenant_home, CACHE) == metadata,
        "registry or bank metadata changed during read-only proof")
    return {"kind": TAG["kind"], "registry_records": len(rows),
        "changed_files": changed, "source_sha256": _base.sha(raw),
        "source_identity": identity, "head": payload["head"], "complete_map_entries": len(expected)}


def _inventory(home):
    root = Path(home)
    device = root.lstat().st_dev
    selected = []
    # Admit every physical coordinate before reading any file bytes.
    for path in (root, *root.rglob("*")):
        info = path.lstat()
        require(info.st_uid == os.getuid() and info.st_dev == device
            and (stat.S_ISDIR(info.st_mode) or (stat.S_ISREG(info.st_mode) and info.st_nlink == 1)),
            "warm clone contains a foreign, linked or nonordinary coordinate")
        if stat.S_ISREG(info.st_mode):
            selected.append(path)
    return {path.relative_to(root).as_posix(): path.read_bytes() for path in selected}


def verify_warmed_registry_clone(home, expected_seed):
    """Prove this one warm artifact and exact preservation of all other bytes."""
    root = FloatiRoot.open_direct_home(Path(home))
    require(type(expected_seed) is dict and all(type(path) is str and type(raw) is bytes
        for path, raw in expected_seed.items()), "warm seed is not a complete byte inventory")
    actual = _inventory(root.tenant_home)
    old_cache, cache = _base._cache(expected_seed, CACHE), _base._cache(actual, CACHE)
    require({path: raw for path, raw in expected_seed.items() if path not in old_cache}
        == {path: raw for path, raw in actual.items() if path not in cache},
        "warm registry clone changed frozen noncache bytes or population")
    if CACHE + "/seal.key" in old_cache:
        require(cache.get(CACHE + "/seal.key") == old_cache[CACHE + "/seal.key"],
            "warm registry clone changed its retained sealing key")
    captured = capture_registry_prefix_input(root.tenant_home, actual)
    proof = verify_registry_prefix_effect(root.tenant_home, actual, actual, {}, captured)
    require(_inventory(root.tenant_home) == actual, "read-only warm proof changed complete clone bytes")
    proof["kind"] = "complete_native_registry_warm_clone"
    proof["changed_files"] = sorted(path for path in set(old_cache) | set(cache)
        if old_cache.get(path) != cache.get(path))
    return proof

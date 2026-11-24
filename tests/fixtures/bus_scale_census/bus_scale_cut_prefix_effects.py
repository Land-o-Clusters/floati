"""Independent exact raw-domain cut map, midstate and insertion testimony.

The product SHA compressor and cache reader never participate in this proof.
Every current map is reconstructed from the complete native event bytes.
"""
from __future__ import annotations

import importlib.util as _fixture_proof_util
from pathlib import Path as _FixtureProofPath

_fixture_proof_specification = _fixture_proof_util.spec_from_file_location(
    "_bus_scale_fixture_proof_bootstrap", _FixtureProofPath(__file__).with_name("_proof_namespace.py"))
_fixture_proof_bootstrap = _fixture_proof_util.module_from_spec(_fixture_proof_specification)
_fixture_proof_specification.loader.exec_module(_fixture_proof_bootstrap)
_fixture_proof_bootstrap.bind()

import copy
from functools import wraps
import hashlib
from pathlib import Path
import struct

from floati import records, send_prefix
from floati.errors import IntegrityFailure, ProtocolRefusal
from floati.events import EVENT_KINDS, validate_event_records
from floati.root import FloatiRoot
from floati.wake_hold import WakeHoldController
from _bus_scale_fixture_proofs import bus_scale_cursor_prefix_effects as _keys
from _bus_scale_fixture_proofs import bus_scale_registry_prefix_effects as _registry

_base = _keys._base
RELATIVE = "events.jsonl"
FORMAT = "cut-prefix-v1"
CACHE_NAME_PREFIX = ".cut-prefix-"
CACHE = CACHE_NAME_PREFIX + hashlib.sha256(RELATIVE.encode("utf-8")).hexdigest()
SUMMARY = "@cache/cut-prefix"
TAG = {"operation": "native_cache_certification", "kind": "complete_native_cut_prefix"}
raw_candidate_delta = _base.raw_candidate_delta


def require(value, detail):
    if not value:
        raise ValueError("native cut prefix effect: " + detail)


def _refusal(operation):
    @wraps(operation)
    def run(*args, **kwargs):
        try:
            return operation(*args, **kwargs)
        except (OSError, RuntimeError, KeyError, TypeError, IntegrityFailure,
                ProtocolRefusal, send_prefix.CacheMiss) as error:
            raise ValueError("native cut prefix effect: unavailable or malformed testimony") from error
    return run


# FIPS 180-4 SHA-256 compression, independently carried by the witness. The
# only hashlib-derived value below is the raw prefix digest, not this state.
_INITIAL = (0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a,
    0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19)
_ROUND = (
    0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
    0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
    0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
    0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
    0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
    0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
    0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
    0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2)
_MASK = (1 << 32) - 1


def _rotate(word, bits):
    return ((word >> bits) | (word << (32 - bits))) & _MASK


def _midstate(data):
    """Return the unpadded eight words and exact unconsumed native tail."""
    state = _INITIAL
    through = len(data) // 64 * 64
    for offset in range(0, through, 64):
        words = list(struct.unpack(">16I", data[offset:offset + 64]))
        for index in range(16, 64):
            left, right = words[index - 15], words[index - 2]
            small0 = _rotate(left, 7) ^ _rotate(left, 18) ^ (left >> 3)
            small1 = _rotate(right, 17) ^ _rotate(right, 19) ^ (right >> 10)
            words.append((words[index - 16] + small0 + words[index - 7] + small1) & _MASK)
        a, b, c, d, e, f, g, h = state
        for constant, word in zip(_ROUND, words):
            large1 = _rotate(e, 6) ^ _rotate(e, 11) ^ _rotate(e, 25)
            choose = (e & f) ^ ((~e) & g)
            first = (h + large1 + choose + constant + word) & _MASK
            large0 = _rotate(a, 2) ^ _rotate(a, 13) ^ _rotate(a, 22)
            majority = (a & b) ^ (a & c) ^ (b & c)
            a, b, c, d, e, f, g, h = ((first + large0 + majority) & _MASK,
                a, b, c, (d + first) & _MASK, e, f, g)
        state = tuple((prior + current) & _MASK
            for prior, current in zip(state, (a, b, c, d, e, f, g, h)))
    return list(state), data[through:]


def _native(root, raw):
    rows = _keys._transcript(root, RELATIVE, frozenset(EVENT_KINDS), raw)
    validate_event_records(rows)
    frames = raw.splitlines(keepends=True)
    require(len(frames) == len(rows), "known physical frame population differs")
    domain = WakeHoldController._EVENT_DOMAIN.encode("ascii") + b"\0"
    digest = hashlib.sha256(domain)
    expected = {}
    for ordinal, frame in enumerate(frames, 1):
        digest.update(frame)
        expected[_base._key("cut", digest.hexdigest())] = ordinal
    require(len(expected) == len(rows), "raw event cut digest is not injective")
    state, tail = _midstate(domain + raw)
    fold = {"digest": digest.hexdigest(), "count": len(rows),
        "length": len(domain) + len(raw), "state": state, "tail": tail.hex()}
    return rows, expected, {"cut": fold}


def _binding(root):
    binding = _keys._binding(root, RELATIVE, EVENT_KINDS, FORMAT)
    binding.update(revision={"source": send_prefix._revision(),
        "reader_version": records.READER_VERSION}, domain=WakeHoldController._EVENT_DOMAIN,
        folds={"cut": 1})
    return binding


def _file_identities(home, cache):
    return {path: send_prefix._identity(Path(home) / path) for path in cache}


def _live(home, raw):
    root = FloatiRoot.open_direct_home(Path(home))
    path = root.tenant_home / RELATIVE
    identity = send_prefix._identity(path)
    require((path.read_bytes() if path.exists() else b"") == raw,
        "live native event bytes differ from retained transcript")
    require(send_prefix._identity(path) == identity, "event identity changed during proof")
    return root, identity


@_refusal
def capture_cut_prefix_input(home, before):
    root, identity = _live(home, before.get(RELATIVE, b""))
    cache = _base._cache(before, CACHE)
    metadata = _base._metadata(root.tenant_home, CACHE)
    identities = _file_identities(home, cache)
    require(all((root.tenant_home / path).read_bytes() == raw for path, raw in cache.items()),
        "pre-child cut bank differs from retained complete bytes")
    checkpoint = cache.get(CACHE + "/checkpoint.json")
    require(_file_identities(home, cache) == identities
        and _base._metadata(root.tenant_home, CACHE) == metadata
        and send_prefix._identity(root.tenant_home / RELATIVE) == identity,
        "native cut input changed during capture")
    return {"root": str(root.tenant_home), "relative": RELATIVE,
        "ledger_identity": identity, "source_sha256": _base.sha(before.get(RELATIVE, b"")),
        "checkpoint_sha256": _base.sha(checkpoint) if checkpoint is not None else None,
        "cache_metadata": metadata, "cache_identities": identities}


def _complete(home, raw):
    source = raw.get(RELATIVE, b"")
    root, identity = _live(home, source)
    cache = _base._cache(raw, CACHE)
    metadata = _base._metadata(root.tenant_home, CACHE)
    identities = _file_identities(home, cache)
    _base._check_metadata(metadata, cache, CACHE, bucketed=True)
    require(all((root.tenant_home / path).read_bytes() == value for path, value in cache.items()),
        "live cut bank differs from retained complete bytes")
    rows, expected, folds = _native(root, source)
    payload, _ = _keys._checkpoint(cache, CACHE, source, rows, identity,
        _binding(root), expected, folds=folds)
    require(send_prefix._identity(root.tenant_home / RELATIVE) == identity
        and _base._metadata(root.tenant_home, CACHE) == metadata
        and _file_identities(home, cache) == identities
        and all((root.tenant_home / path).read_bytes() == value for path, value in cache.items()),
        "native source or cut bank metadata changed during proof")
    return root, payload, rows, expected, folds, cache, metadata


@_refusal
def verify_complete_cut_bank(home, raw):
    root, payload, rows, expected, folds, _cache, _metadata = _complete(home, raw)
    return {"kind": TAG["kind"], "event_records": len(rows),
        "source_sha256": _base.sha(raw.get(RELATIVE, b"")), "source_identity": payload["identity"],
        "head": payload["head"], "complete_map_entries": len(expected),
        "cut": copy.deepcopy(folds["cut"]), "changed_files": []}


@_refusal
def verify_cut_prefix_effect(home, before, after, delta, prefix_input=None):
    """Admit a full read rebuild or one exact retained-prefix insertion."""
    if prefix_input is None:
        require(before == after, "a mutating cut effect requires captured pre-child testimony")
        prefix_input = capture_cut_prefix_input(home, before)
    require(type(prefix_input) is dict and set(prefix_input) == {
        "root", "relative", "ledger_identity", "source_sha256", "checkpoint_sha256",
        "cache_metadata", "cache_identities"}, "missing exact pre-child cut testimony")
    old_source, source = before.get(RELATIVE, b""), after.get(RELATIVE, b"")
    root, payload, rows, expected, folds, cache, metadata = _complete(home, after)
    require(prefix_input["root"] == str(root.tenant_home) and prefix_input["relative"] == RELATIVE,
        "foreign root or event coordinate")
    require(_base.sha(old_source) == prefix_input["source_sha256"], "retained native input changed")
    old_cache = _base._cache(before, CACHE)
    old_checkpoint = old_cache.get(CACHE + "/checkpoint.json")
    require(prefix_input["checkpoint_sha256"] == (
        _base.sha(old_checkpoint) if old_checkpoint is not None else None), "prior cut checkpoint differs")
    if old_cache:
        _base._check_metadata(prefix_input["cache_metadata"], old_cache, CACHE, bucketed=True)
    else:
        require(not prefix_input["cache_metadata"], "absent bank has unexplained metadata")
    if CACHE + "/seal.key" in old_cache:
        require(old_cache[CACHE + "/seal.key"] == cache[CACHE + "/seal.key"],
            "native cut rebuild changed the retained sealing key")
    prior_identity = prefix_input["ledger_identity"]
    appended = source != old_source
    old_rows = []
    retained_identities = prefix_input["cache_identities"]
    require(type(retained_identities) is dict and set(retained_identities) == set(old_cache)
        and all(type(identity) is list and len(identity) == 7
            and all(type(part) is int for part in identity) for identity in retained_identities.values()),
        "pre-child cut cache identity population differs")
    current_identities = _file_identities(home, cache)
    if appended:
        require(source.startswith(old_source) and len(source) > len(old_source),
            "native tail append changed retained source bytes")
        require(payload["identity"] is not None and (prior_identity is None
            or payload["identity"][:4] == prior_identity[:4]), "native append replaced its source")
        require(old_checkpoint is not None, "append requires a complete warmed cut bank")
        old_rows, old_expected, old_folds = _native(root, old_source)
        old_payload, _ = _keys._checkpoint(old_cache, CACHE, old_source, old_rows, prior_identity,
            _binding(root), old_expected, folds=old_folds)
        require(payload["generation"] == old_payload["generation"],
            "tail append changed its immutable cut generation")
        require(set(old_expected) <= set(expected)
            and all(expected[key] == value for key, value in old_expected.items()),
            "tail append changed a retained cut ordinal")
        inserted = set(expected) - set(old_expected)
        require(len(inserted) == len(rows) - len(old_rows), "tail append did not insert one key per new frame")
        expected_cache = dict(old_cache)
        expected_cache.update({CACHE + "/nodes/" + format(int(key, 2), "064x"):
            cache[CACHE + "/nodes/" + format(int(key, 2), "064x")] for key in inserted})
        directory, pages = _base._bucket_map(expected, cache[CACHE + "/seal.key"], payload["generation"])
        expected_cache[CACHE + "/directory.bin"] = directory
        expected_cache.update({CACHE + "/buckets/" + index: value for index, value in pages.items()})
        expected_cache[CACHE + "/checkpoint.json"] = cache[CACHE + "/checkpoint.json"]
        require(expected_cache == cache, "cut bank differs from exact native insertion delta")
        for path, identity in retained_identities.items():
            if path.startswith(CACHE + "/nodes/") or path == CACHE + "/seal.key":
                require(cache.get(path) == old_cache[path] and current_identities.get(path) == identity,
                    "tail append rewrote a retained immutable cut node or sealing key")
    else:
        require(payload["identity"] == prior_identity, "read-side rebuild changed native source identity")
    changed = sorted(path for path in set(old_cache) | set(cache) if old_cache.get(path) != cache.get(path))
    candidate = raw_candidate_delta(before, after)
    require({path for path in delta if path == CACHE or path.startswith(CACHE + "/")} == set(changed)
        and all(delta[path] == candidate[path] for path in changed),
        "cut cache delta differs from complete retained byte delta")
    if appended:
        for path in (CACHE + "/directory.bin", CACHE + "/checkpoint.json"):
            if path in changed:
                require(current_identities[path][:2] != retained_identities[path][:2],
                    "native cut append overwrote an atomic derived file in place")
    require(all((root.tenant_home / path).read_bytes() == value for path, value in cache.items())
        and send_prefix._identity(root.tenant_home / RELATIVE) == payload["identity"]
        and _base._metadata(root.tenant_home, CACHE) == metadata
        and _file_identities(home, cache) == current_identities, "cut evidence changed during proof")
    semantic = {"operation": "verified_cut_prefix_append" if appended else "verified_cut_prefix_rebuild",
        "format": FORMAT, "before_records": len(old_rows) if appended else len(rows),
        "after_records": len(rows), "appended_kinds": [row["kind"] for row in rows[len(old_rows):]] if appended else [],
        "complete_transcripts_and_maps_validated": True, "output_checkpoint_seal_validated": True,
        "exact_insertion_delta_validated": appended, "retained_nodes_unchanged": appended}
    return {"kind": TAG["kind"], "event_records": len(rows), "complete_map_entries": len(expected),
        "source_sha256": _base.sha(source), "source_identity": payload["identity"], "head": payload["head"],
        "cut": copy.deepcopy(folds["cut"]), "semantic": semantic, "changed_files": changed,
        "replaced_files": sorted(path for path, prior in retained_identities.items()
            if current_identities.get(path, [])[:2] != prior[:2]), "before_input": copy.deepcopy(prefix_input),
        "cache_before": _base._inventory(old_cache), "cache_after": _base._inventory(cache)}


@_refusal
def verify_warmed_cut_clone(home, expected_seed):
    """Warming changes only this bank and preserves every native source byte."""
    require(type(expected_seed) is dict and all(type(path) is str and type(raw) is bytes
        for path, raw in expected_seed.items()), "warm seed is not a complete byte inventory")
    actual = _registry._inventory(Path(home))
    old_cache, cache = _base._cache(expected_seed, CACHE), _base._cache(actual, CACHE)
    require({path: raw for path, raw in expected_seed.items() if path not in old_cache}
        == {path: raw for path, raw in actual.items() if path not in cache},
        "warm cut clone changed noncache bytes or population")
    if CACHE + "/seal.key" in old_cache:
        require(cache.get(CACHE + "/seal.key") == old_cache[CACHE + "/seal.key"],
            "warm cut clone changed its retained sealing key")
    proof = verify_complete_cut_bank(home, actual)
    require(_registry._inventory(Path(home)) == actual, "read-only warm proof changed complete bytes")
    proof["kind"] = "complete_native_cut_warm_clone"
    proof["changed_files"] = sorted(path for path in set(old_cache) | set(cache)
        if old_cache.get(path) != cache.get(path))
    return proof


@_refusal
def project_cut_prefix_tree(before, after, proof, begin, end, *, filesystem_clock=None):
    """Project derived entropy only after binding every tree leaf to its proof."""
    from tests.bus_scale_filesystem_clock import filesystem_bounds
    lower, upper = filesystem_bounds(begin, end, filesystem_clock)
    old, new = before["entries"], after["entries"]
    for tree, inventory in ((old, proof["cache_before"]), (new, proof["cache_after"])):
        selected = {path: row for path, row in tree.items() if path == CACHE or path.startswith(CACHE + "/")}
        files = {path for path, row in selected.items() if row["kind"] == "file"}
        require(files == set(inventory), "cut tree file population differs from complete raw proof")
        for path in files:
            require(selected[path]["mode"] == 0o600 and selected[path]["bytes"] == inventory[path]["bytes"]
                and selected[path]["sha256"] == inventory[path]["sha256"], "cut tree leaf differs from raw byte proof")
        directories = {path for path, row in selected.items() if row["kind"] == "directory"}
        require(directories == ({CACHE, CACHE + "/nodes", CACHE + "/staging", CACHE + "/buckets"} if inventory else set())
            and all(selected[path]["mode"] == 0o700 for path in directories),
            "cut tree directory population or mode differs")
    for path in set(old) | set(new):
        if path != CACHE and not path.startswith(CACHE + "/"):
            continue
        require(path in new, "native cut effect removed a retained tree entry")
        if (path in old and path not in proof["changed_files"] and path not in proof["replaced_files"]
                and new[path]["kind"] == "file"):
            require(new[path] == old[path], "native effect changed retained cut file metadata")
        if path not in old or new[path]["mtime_ns"] != old[path]["mtime_ns"]:
            stamp = new[path]["mtime_ns"]
            require(type(stamp) is int and lower <= stamp <= upper,
                "cut mutation clock is outside native invocation")
    return {"kind": "complete_validated_native_cut_prefix_cache", "effect": copy.deepcopy(proof["semantic"])}

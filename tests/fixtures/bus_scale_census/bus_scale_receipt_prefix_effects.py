"""Read-only complete proof of one native exhaustion receipt cache effect.

No native I/O or cache path is exempted. The retained byte inventories and raw
delta remain authoritative; only a proved complete insertion becomes a stable
semantic fact for comparing independently authored native invocations.
"""
from __future__ import annotations

import base64
import copy
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import stat
import zlib

from floati import jsonl, key_prefix, send_prefix
from floati.framing import decode_frames, encode_frame
from floati.root import FloatiRoot

SUMMARY = "@cache/receipt-prefix"
FORMAT = "wait-exhaustion-prefix-v3"
KINDS = frozenset({"codex_wait_exhaustion_receipt"})
_HASH = re.compile(r"[0-9a-f]{64}\Z")


def require(value, detail):
    if not value:
        raise ValueError("native receipt prefix cache effect: " + detail)


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _bytes(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")


def cache_name(node="scale-worker"):
    require(type(node) is str and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", node) is not None,
        "selected node shape differs")
    return ".receipt-prefix-" + sha(node.encode("utf-8"))


def ledger_relative(node="scale-worker"):
    cache_name(node)
    return "receipts/codex-wait-exhaustion/" + node + ".jsonl"


def _cache(raw, name):
    return {path: value for path, value in raw.items() if path == name or path.startswith(name + "/")}


def _inventory(raw):
    return {path: {"bytes": len(value), "sha256": sha(value)} for path, value in sorted(raw.items())}


def _metadata(home, name):
    root = Path(home) / name
    if not root.exists() and not root.is_symlink():
        return {}
    result = {}
    for target in (root, *root.rglob("*")):
        info = target.lstat()
        directory = stat.S_ISDIR(info.st_mode)
        require(info.st_uid == os.getuid() and (directory or stat.S_ISREG(info.st_mode)),
            "cache contains an alias, special file or foreign owner")
        require(stat.S_IMODE(info.st_mode) == (0o700 if directory else 0o600)
            and (directory or info.st_nlink == 1), "cache metadata is not private and ordinary")
        result[target.relative_to(home).as_posix()] = {"kind": "directory" if directory else "file",
            "mode": stat.S_IMODE(info.st_mode), "uid": info.st_uid, "nlink": info.st_nlink}
    return result


def _check_metadata(metadata, cache, name, *, bucketed=False):
    directories = {path for path, value in metadata.items() if value["kind"] == "directory"}
    files = {path for path, value in metadata.items() if value["kind"] == "file"}
    require(directories == {name, name + "/nodes", name + "/staging"} | ({name + "/buckets"} if bucketed else set()),
        "cache directory or staging population differs")
    require(files == set(cache), "cache file population differs from complete retained bytes")
    for path, value in metadata.items():
        require(value["uid"] == os.getuid() and value["mode"] == (0o700 if path in directories else 0o600)
            and (path in directories or value["nlink"] == 1), "retained cache metadata differs")


def capture_receipt_prefix_input(home, before, *, node="scale-worker"):
    """Capture exact pre-child identity and private metadata without writes."""
    name, relative = cache_name(node), ledger_relative(node)
    path = Path(home) / relative
    raw = before.get(relative, b"")
    require((path.read_bytes() if path.exists() else b"") == raw, "input native transcript changed")
    cache = _cache(before, name)
    metadata = _metadata(home, name)
    if cache:
        require(name + "/checkpoint.json" in cache, "warmed input checkpoint is missing")
        _check_metadata(metadata, cache, name, bucketed=True)
        for coordinate, value in cache.items():
            require((Path(home) / coordinate).read_bytes() == value, "input cache bytes changed")
    else:
        require(not metadata and raw == b"", "absent cache requires exact native empty prior")
    return {"node": node, "relative": relative, "ledger_identity": send_prefix._identity(path),
        "checkpoint_sha256": sha(cache[name + "/checkpoint.json"]) if cache else None,
        "cache_metadata": metadata}


def _object(raw):
    value = json.loads(raw)
    require(_bytes(value) == raw, "noncanonical or duplicate-key cache JSON")
    return value


def _transcript(root, relative, raw):
    rows = jsonl._decode_path_records(root.tenant_home / relative, root.tenant_id, KINDS, raw)
    require(b"".join(encode_frame(row) for row in rows) == raw, "noncanonical native receipt transcript")
    return rows


def _key(domain, value):
    digest = hashlib.sha256(domain.encode("ascii") + b"\0" + value.encode("utf-8")).digest()
    return "".join(format(byte, "08b") for byte in digest)


def _entries(rows):
    entries = {_key("id", row["id"]): row for row in rows}
    first = {}
    for row in rows:
        first.setdefault(row["idempotency_key"], row)
    entries.update({_key("receipt", key): row for key, row in first.items()})
    return entries


def _canonical_root(entries):
    """Commit every logical ID and first-key value independently of writer insertion."""
    def build(items):
        if not items:
            return None
        if len(items) == 1:
            key, value = items[0]
            return sha(_bytes({"prefix": key, "value": sha(_bytes(value))}))
        first, last = items[0][0], items[-1][0]
        length = 0
        while length < 256 and first[length] == last[length]:
            length += 1
        require(length < 256, "duplicate logical index key")
        groups = {}
        for key, value in items:
            groups.setdefault(key[length], []).append((key, value))
        require(set(groups) == {"0", "1"}, "logical index is not binary")
        return sha(_bytes({"prefix": first[:length],
            "children": {bit: build(group) for bit, group in groups.items()}}))
    return build(sorted(entries.items()))


def _head(raw):
    head = send_prefix._EMPTY_HEAD
    for frame in raw.splitlines(keepends=True):
        head = sha(bytes.fromhex(head) + frame)
    return head


def _binding(root, relative):
    return {"format": FORMAT, "index_format": key_prefix.INDEX_FORMAT, "root": str(root.tenant_home), "tenant": root.tenant_id,
        "bucket_count": key_prefix.BUCKET_COUNT, "bucket_bytes": key_prefix.BUCKET_BYTES, "bucket_capacity": key_prefix.BUCKET_CAPACITY,
        "bucket_encoding": _bucket_encoding(),
        "kinds": sorted(KINDS), "maximum": jsonl.MAX_RECORD_BYTES, "ledger_bytes": jsonl.MAX_LEDGER_BYTES,
        "ledger_records": jsonl.MAX_LEDGER_RECORDS, "revision": send_prefix._revision(), "relative": relative,
        "folds": {}}


def _trie_checkpoint(root, name, relative, cache, raw, rows, identity):
    require(set(cache) >= {name + "/checkpoint.json", name + "/seal.key"}, "incomplete cache checkpoint")
    secret = cache[name + "/seal.key"]
    require(type(secret) is bytes and len(secret) == 32, "sealing key shape differs")
    value = _object(cache[name + "/checkpoint.json"])
    require(type(value) is dict and set(value) == {"payload", "seal"}, "checkpoint shape differs")
    payload = value["payload"]
    require(type(payload) is dict and set(payload) == {"binding", "identity", "offset", "count", "head", "trie"},
        "checkpoint payload shape differs")
    seal = hmac.new(secret, _bytes(payload), hashlib.sha256).hexdigest()
    require(type(value["seal"]) is str and hmac.compare_digest(value["seal"], seal), "checkpoint authentication failed")
    require(payload["binding"] == _binding(root, relative), "checkpoint binding or reader revision differs")
    valid_identity = (identity is None and raw == b"") or (type(identity) is list and len(identity) == 7
        and all(type(value) is int for value in identity) and identity[4] == len(raw))
    require(valid_identity and payload["identity"] == identity, "checkpoint file identity differs")
    require(type(payload["offset"]) is int and payload["offset"] == len(raw)
        and type(payload["count"]) is int and payload["count"] == len(rows)
        and payload["head"] == _head(raw), "checkpoint count, offset or exact-frame chain differs")
    expected = _entries(rows)
    require(payload["trie"] == _canonical_root(expected), "checkpoint does not commit the complete native map")
    nodes = {}
    for path, raw_node in cache.items():
        if path in {name + "/checkpoint.json", name + "/seal.key"}:
            continue
        parts = Path(path).relative_to(name).parts
        require(len(parts) == 2 and parts[0] == "nodes" and _HASH.fullmatch(parts[1]) is not None,
            "unreviewed cache member")
        require(sha(raw_node) == parts[1], "cache member hash differs from its name")
        nodes[parts[1]] = _object(raw_node)
    seen = {}
    def walk(digest, parent="", bit=None):
        require(digest in nodes, "authenticated cache node or payload is missing")
        value = nodes[digest]
        require(type(value) is dict and set(value) in ({"prefix", "value"}, {"prefix", "children"}), "trie node shape differs")
        prefix = value["prefix"]
        require(type(prefix) is str and len(prefix) <= 256 and set(prefix) <= {"0", "1"}
            and prefix.startswith(parent) and (bit is None or prefix.startswith(parent + bit)), "trie path differs")
        if "value" in value:
            require(len(prefix) == 256 and prefix not in seen and value["value"] in nodes, "trie leaf differs")
            seen[prefix] = nodes[value["value"]]
        else:
            require(len(prefix) < 256 and type(value["children"]) is dict
                and set(value["children"]) == {"0", "1"}, "trie branch differs")
            for child_bit, child in value["children"].items():
                walk(child, prefix, child_bit)
    if payload["trie"] is not None:
        walk(payload["trie"])
    require(_bytes(seen) == _bytes(expected), "complete trie payload map differs")
    return payload, nodes


def _bank_identity(path):
    info = path.lstat()
    require(stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid()
        and stat.S_IMODE(info.st_mode) == 0o700, "bank namespace is not private and ordinary")
    return [getattr(info, field) for field in send_prefix._FIELDS]


def _bucket_encoding():
    return {"format": "zlib-page-v1", "level": 9, "wbits": 15, "mem_level": 8,
        "strategy": 0, "runtime_version": zlib.ZLIB_RUNTIME_VERSION}


def _storage_page(page):
    compressor = zlib.compressobj(level=9, method=zlib.DEFLATED, wbits=15, memLevel=8, strategy=zlib.Z_DEFAULT_STRATEGY)
    return compressor.compress(page) + compressor.flush(zlib.Z_FINISH)


def _storage_decode(raw):
    try:
        require(len(raw) <= 8192 + 64, "compressed bucket exceeds bounded physical storage")
        decoder = zlib.decompressobj(wbits=15)
        page = decoder.decompress(raw, 8193)
        require(len(page) == 8192 and decoder.eof and not decoder.unused_data and not decoder.unconsumed_tail,
            "compressed bucket has oversized, truncated or trailing data")
        require(_storage_page(page) == raw, "compressed bucket is not canonical")
        return page
    except zlib.error as error:
        raise ValueError("native receipt prefix cache effect: invalid compressed bucket") from error


def _bucket_map(keys, secret, generation):
    """Independently reconstruct every fixed membership page and root hash."""
    groups = {}
    for key in keys:
        digest = hmac.new(secret, b"floati-key-bucket-v1\0" + generation.encode("ascii") + b"\0"
            + int(key, 2).to_bytes(32, "big"), hashlib.sha256).digest()
        index = int.from_bytes(digest[:2], "big") >> 6
        groups.setdefault(index, []).append(int(key, 2).to_bytes(32, "big"))
    pages = {}
    empty_hash = hashlib.sha256(bytes(8192)).digest()
    directory = bytearray(empty_hash * 1024)
    for index, members in groups.items():
        members = sorted(members)
        require(len(members) <= 255 and len(set(members)) == len(members), "membership bucket overflow or duplicate")
        page = len(members).to_bytes(2, "big") + b"".join(members) + bytes(8192 - 2 - 32 * len(members))
        pages[format(index, "04x")] = _storage_page(page)
        directory[32 * index:32 * (index + 1)] = hashlib.sha256(page).digest()
    return bytes(directory), pages


def _checkpoint(root, name, relative, cache, raw, rows, identity):
    """Independently verify every direct entry and the exact complete namespace."""
    require(set(cache) >= {name + "/checkpoint.json", name + "/seal.key"}, "incomplete cache checkpoint")
    secret = cache[name + "/seal.key"]
    require(type(secret) is bytes and len(secret) == 32, "sealing key shape differs")
    value = _object(cache[name + "/checkpoint.json"])
    require(type(value) is dict and set(value) == {"payload", "seal"}, "checkpoint shape differs")
    payload = value["payload"]
    require(type(payload) is dict and set(payload) == {"binding", "identity", "offset", "count", "head", "generation", "directory_sha256", "folds"},
        "checkpoint payload shape differs")
    require(type(value["seal"]) is str and hmac.compare_digest(value["seal"],
        hmac.new(secret, _bytes(payload), hashlib.sha256).hexdigest()), "checkpoint authentication failed")
    require(payload["binding"] == _binding(root, relative), "checkpoint binding or reader revision differs")
    require(type(payload["folds"]) is dict and payload["folds"] == {},
        "checkpoint folds differ from complete native history")
    valid_identity = (identity is None and raw == b"") or (type(identity) is list and len(identity) == 7
        and all(type(number) is int for number in identity) and identity[4] == len(raw))
    require(valid_identity and payload["identity"] == identity, "checkpoint file identity differs")
    require(type(payload["generation"]) is str and re.fullmatch(r"[0-9a-f]{32}", payload["generation"]) is not None,
        "checkpoint generation shape differs")
    require(type(payload["offset"]) is int and payload["offset"] == len(raw)
        and type(payload["count"]) is int and payload["count"] == len(rows)
        and payload["head"] == _head(raw), "checkpoint count, offset or exact-frame chain differs")
    expected = _entries(rows)
    expected_paths = {name + "/nodes/" + format(int(key, 2), "064x"): key for key in expected}
    directory, pages = _bucket_map(expected, secret, payload["generation"])
    require(cache.get(name + "/directory.bin") == directory and payload["directory_sha256"] == sha(directory),
        "complete membership directory differs from native map")
    bucket_paths = {name + "/buckets/" + index: page for index, page in pages.items()}
    require(set(cache) == {name + "/checkpoint.json", name + "/seal.key", name + "/directory.bin"} | set(expected_paths) | set(bucket_paths),
        "complete native map namespace population differs")
    require(all(cache[path] == page for path, page in bucket_paths.items()), "complete membership bucket differs from native map")
    for path in bucket_paths:
        _storage_decode(cache[path])
    decoded = {}
    binding_hash = sha(_bytes(payload["binding"]))
    for path, key in expected_paths.items():
        entry = _object(cache[path])
        require(type(entry) is dict and set(entry) == {"payload", "seal"} and type(entry["payload"]) is dict,
            "direct entry shape differs")
        body = {"index_format": key_prefix.INDEX_FORMAT, "binding_sha256": binding_hash,
            "generation": payload["generation"], "key": key, "value": expected[key]}
        require(entry["payload"] == body, "complete native map entry, generation or domain/key differs")
        require(type(entry["seal"]) is str and hmac.compare_digest(entry["seal"],
            hmac.new(secret, _bytes(body), hashlib.sha256).hexdigest()), "direct entry authentication failed")
        decoded[key] = entry["payload"]["value"]
    return payload, decoded


def raw_candidate_delta(before, after):
    """Retain complete hostile or native byte deltas before effect validation."""
    result = {}
    for path in sorted(set(before) | set(after)):
        if before.get(path) == after.get(path):
            continue
        old, new = before.get(path, b""), after.get(path)
        if new is None:
            operation, payload = "delete", b""
        elif not new and path.endswith(".lock"):
            operation, payload = "create_lock", b""
        elif path.endswith(".json") or not new.startswith(old):
            operation, payload = "replace", new
        else:
            operation, payload = "append", new[len(old):]
        result[path] = {"operation": operation, "before_bytes": len(old), "after_bytes": len(new or b""),
            "before_sha256": sha(old), "after_sha256": sha(new or b""), "payload_base64": base64.b64encode(payload).decode()}
    return result


class _MemoryInsert(send_prefix.Prefix):
    """Exact writer insertion choreography with a memory-only publication seam."""
    def __init__(self, payload, nodes):
        self.payload, self.memo = copy.deepcopy(payload), copy.deepcopy(nodes)
        self.written = {}
    def _node(self, digest):
        require(digest in self.memo, "insertion lacks authenticated prior node")
        return self.memo[digest]
    def _put(self, value):
        raw = _bytes(value)
        digest = sha(raw)
        # Native _put returns without publication on an existing memo entry.
        if digest not in self.memo:
            self.written[digest] = raw
        self.memo[digest] = copy.deepcopy(value)
        return digest


def verify_receipt_prefix_effect(home, before, after, delta, prefix_input, *, node="scale-worker"):
    """Prove complete warm or exact absent/empty cold cache append effects."""
    root = FloatiRoot.open_direct_home(Path(home))
    name, relative = cache_name(node), ledger_relative(node)
    require(type(prefix_input) is dict and set(prefix_input) == {
        "node", "relative", "ledger_identity", "checkpoint_sha256", "cache_metadata"}, "missing pre-child testimony")
    require(prefix_input["node"] == node and prefix_input["relative"] == relative, "input selected ledger differs")
    old_cache, new_cache = _cache(before, name), _cache(after, name)
    old_raw, new_raw = before.get(relative, b""), after.get(relative, b"")
    require(new_raw.startswith(old_raw), "native receipt append rewrote its prior transcript")
    old_rows, new_rows = _transcript(root, relative, old_raw), _transcript(root, relative, new_raw)
    batch = decode_frames(new_raw[len(old_raw):])
    require(len(batch) == 1 and batch[0]["kind"] == "codex_wait_exhaustion_receipt"
        and batch[0]["node_id"] == node and batch[0]["outcome"] == "consent_reopened",
        "cache effect is not one native selected exhaustion append")
    require(all(row["idempotency_key"] != batch[0]["idempotency_key"] for row in old_rows), "native selected key was not fresh")
    cold = not old_cache
    if cold:
        require(old_raw == b"" and prefix_input["checkpoint_sha256"] is None
            and prefix_input["cache_metadata"] == {},
            "cold cache did not have exact absent/empty prior")
        old_payload = {"binding": _binding(root, relative), "identity": prefix_input["ledger_identity"],
            "offset": 0, "count": 0, "head": _head(b"")}
        require(old_payload["identity"] is None or (type(old_payload["identity"]) is list
            and len(old_payload["identity"]) == 7 and old_payload["identity"][4] == 0), "cold native identity differs")
    else:
        _check_metadata(prefix_input["cache_metadata"], old_cache, name, bucketed=True)
        require(prefix_input["checkpoint_sha256"] == sha(old_cache.get(name + "/checkpoint.json", b"")), "pre-child checkpoint bytes differ")
        require(old_cache.get(name + "/seal.key") == new_cache.get(name + "/seal.key"), "native append changed its retained sealing key")
        old_payload, _ = _checkpoint(root, name, relative, old_cache, old_raw, old_rows,
            prefix_input["ledger_identity"])
    current_identity = send_prefix._identity(root.tenant_home / relative)
    require((root.tenant_home / relative).read_bytes() == new_raw, "post-child transcript differs from live evidence")
    new_payload, _ = _checkpoint(root, name, relative, new_cache, new_raw, new_rows, current_identity)
    require(new_payload["identity"] is not None and (old_payload["identity"] is None
        or old_payload["identity"][:4] == new_payload["identity"][:4]), "native append replaced its selected ledger")
    require(cold or old_payload["generation"] == new_payload["generation"], "native append changed immutable bank generation")
    inserted = {name + "/nodes/" + format(int(_key(domain, value), 2), "064x")
        for domain, value in (("id", batch[0]["id"]), ("receipt", batch[0]["idempotency_key"]))}
    require(len(inserted) == 2 and not inserted.intersection(old_cache), "native append did not insert exactly two new immutable entries")
    expected_cache = dict(old_cache)
    expected_cache.update({path: new_cache[path] for path in inserted})
    directory, pages = _bucket_map(_entries(new_rows), new_cache[name + "/seal.key"], new_payload["generation"])
    expected_cache[name + "/directory.bin"] = directory
    expected_cache.update({name + "/buckets/" + index: raw for index, raw in pages.items()})
    expected_cache[name + "/checkpoint.json"] = new_cache[name + "/checkpoint.json"]
    if cold:
        expected_cache[name + "/seal.key"] = new_cache[name + "/seal.key"]
    require(expected_cache == new_cache, "extra, removed or rewritten cache node outside exact native insertion delta")
    paths = {path for path in set(old_cache) | set(new_cache) if old_cache.get(path) != new_cache.get(path)}
    require(paths == {path for path in delta if path == name or path.startswith(name + "/")}, "cache delta population differs")
    for path in paths:
        old, new = old_cache.get(path, b""), new_cache[path]
        require(delta[path] == raw_candidate_delta(old_cache, new_cache)[path], "cache delta bytes, operation or digest differs")
    _check_metadata(_metadata(root.tenant_home, name), new_cache, name, bucketed=True)
    for path, raw in new_cache.items():
        require((root.tenant_home / path).read_bytes() == raw, "post-child cache bytes differ from retained proof")
    require(send_prefix._identity(root.tenant_home / relative) == current_identity, "native receipt identity changed during proof")
    summary = {"operation": "verified_receipt_prefix_append", "format": FORMAT,
        "before_records": len(old_rows), "after_records": len(new_rows), "appended_kinds": [row["kind"] for row in batch],
        "input_cache_state": "absent_native_empty" if cold else "complete_warmed_prefix",
        "complete_transcripts_and_maps_validated": True, "input_checkpoint_seal_validated": not cold,
        "output_checkpoint_seal_validated": True, "exact_insertion_delta_validated": True,
        "complete_namespace_and_entry_seals_validated": True}
    return {"semantic": summary, "changed_files": sorted(paths), "before_input": copy.deepcopy(prefix_input),
        "after_identity": current_identity, "cache_before": _inventory(old_cache), "cache_after": _inventory(new_cache)}


def project_receipt_prefix_tree(before, after, proof, begin, end, *, filesystem_clock=None):
    """Bind every cache tree leaf and mutation clock to the complete byte proof."""
    from tests.bus_scale_filesystem_clock import filesystem_bounds
    lower, upper = filesystem_bounds(begin, end, filesystem_clock)
    name = cache_name(proof["before_input"]["node"])
    old, new = before["entries"], after["entries"]
    for entries, inventory in ((old, proof["cache_before"]), (new, proof["cache_after"])):
        selected = {path: row for path, row in entries.items() if path == name or path.startswith(name + "/")}
        files = {path for path, row in selected.items() if row["kind"] == "file"}
        require(files == set(inventory), "cache tree file population differs from raw proof")
        for path in files:
            require(selected[path]["mode"] == 0o600 and selected[path]["bytes"] == inventory[path]["bytes"]
                and selected[path]["sha256"] == inventory[path]["sha256"], "cache tree file differs from raw proof")
        directories = {path for path, row in selected.items() if row["kind"] == "directory"}
        require(directories == ({name, name + "/nodes", name + "/staging", name + "/buckets"} if inventory else set())
            and all(selected[path]["mode"] == 0o700 for path in directories), "cache tree directory population differs")
    for path in set(old) | set(new):
        if path != name and not path.startswith(name + "/"):
            continue
        require(path in new, "native cache tree removed an entry")
        if path in old and path not in proof["changed_files"] and new[path]["kind"] == "file":
            require(new[path] == old[path], "native cache changed retained file metadata")
        if path not in old or new[path]["mtime_ns"] != old[path]["mtime_ns"]:
            stamp = new[path]["mtime_ns"]
            require(type(stamp) is int and lower <= stamp <= upper,
                "cache mutation clock is outside native invocation")
    return {"kind": "complete_validated_native_receipt_prefix_cache", "effect": copy.deepcopy(proof["semantic"])}

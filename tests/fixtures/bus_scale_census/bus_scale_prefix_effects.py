"""Read-only proof of the complete cache effect of one native report send.

This is evidence validation, not a product cache reader or an I/O exemption.
Both complete event transcripts and every cache byte remain in the raw bank.
Only a proved deterministic append is projected to a comparable semantic fact.
"""
from __future__ import annotations

import base64
import copy
import hashlib
import hmac
import importlib.util
import json
import os
from pathlib import Path
import re
import stat
from datetime import datetime, timezone

from floati import jsonl, send_prefix
from floati.events import EVENT_KINDS, validate_event_records
from floati.framing import decode_frames, encode_frame
from floati.root import FloatiRoot

CACHE = ".send-prefix"
SUMMARY = "@cache/send-prefix"
_HASH = re.compile(r"[0-9a-f]{64}")


def require(value, detail):
    if not value:
        raise ValueError("native prefix cache effect: " + detail)


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _cache(raw):
    return {path: value for path, value in raw.items()
            if path == CACHE or path.startswith(CACHE + "/")}


def _inventory(raw):
    return {path: ({"bytes": len(value), "retained_secret": True} if path == CACHE + "/seal.key"
            else {"sha256": sha(value), "bytes": len(value)}) for path, value in sorted(raw.items())}


def capture_prefix_input(home, before):
    """Capture live input identity outside the native child, without writes."""
    path = Path(home) / "events.jsonl"
    checkpoint = before.get(CACHE + "/checkpoint.json")
    require(type(checkpoint) is bytes, "a warmed input checkpoint is required")
    require((path.read_bytes() if path.exists() else b"") == before.get("events.jsonl", b""), "input transcript changed")
    return {"ledger_identity": send_prefix._identity(path), "checkpoint_sha256": sha(checkpoint)}


def _object(raw):
    value = json.loads(raw)
    require(send_prefix._bytes(value) == raw, "noncanonical or duplicate-key cache JSON")
    return value


def _transcript(root, raw):
    known = jsonl._decode_path_records(root.tenant_home / "events.jsonl", root.tenant_id,
        EVENT_KINDS, raw, unrecognized={})
    validate_event_records(known)
    rows = decode_frames(raw)
    require(b"".join(encode_frame(row) for row in rows) == raw, "noncanonical native event transcript")
    return rows, known


def _mail_entries(rows, *, base_count=None, capture_after=None):
    """Derive every historical membership tree and final catalog from envelopes.

    Explicit path stacks reconstruct each canonical set without using the
    native cache reader, tree insertion implementation or summary producer.
    """
    base_count = len(rows) if base_count is None else base_count
    require(type(base_count) is int and 0 <= base_count <= len(rows), "mail baseline differs from physical history")
    entries, catalogs, senders, changes = {}, {}, {}, []

    def put(value, changed):
        digest = sha(send_prefix._bytes(value))
        key = send_prefix._key("mail-tree-v1", digest)
        if key not in entries:
            entries[key] = value
            if changed is not None:
                changed.append(("mail-tree-v1", digest, value))
        return digest

    def node(digest):
        return entries[send_prefix._key("mail-tree-v1", digest)]

    def branch(prefix, children, changed):
        values = [node(children[bit]) for bit in ("0", "1")]
        return put({"prefix": prefix, "children": children,
            "count": sum(value["count"] for value in values),
            "oldest_utc": min((value["oldest_utc"] for value in values),
                key=lambda stamp: datetime.fromisoformat(stamp.replace("Z", "+00:00")).astimezone(timezone.utc)),
            "oldest_lex": min((value["oldest_lex"] for value in values),
                key=lambda selected: (selected["timestamp"], selected["ordinal"]))}, changed)

    def leaf(key, selected, changed):
        return put({"prefix": key, "item": selected, "count": 1,
            "oldest_utc": selected["timestamp"], "oldest_lex": selected}, changed)

    def bulk(items):
        if len(items) == 1:
            return leaf(items[0][0], items[0][1], None)
        first, last = items[0][0], items[-1][0]
        length = next(index for index in range(256) if first[index] != last[index])
        return branch(first[:length], {bit: bulk([item for item in items if item[0][length] == bit])
            for bit in ("0", "1")}, None)

    def tree(root, selected, changed):
        key = send_prefix._key("mail-member-v1", selected["id"])
        path, digest = [], root
        while digest is not None:
            value = node(digest)
            prefix = value["prefix"]
            if not key.startswith(prefix):
                length = next(index for index, pair in enumerate(zip(prefix, key)) if pair[0] != pair[1])
                digest = branch(key[:length],
                    {prefix[length]: digest, key[length]: leaf(key, selected, changed)}, changed)
                break
            if key == prefix:
                require(value["item"] == selected, "mail membership repeats an inconsistent native item")
                return digest
            bit = key[len(prefix)]
            path.append((value, bit))
            digest = value["children"][bit]
        else:
            digest = leaf(key, selected, changed)
        for value, bit in reversed(path):
            children = dict(value["children"])
            children[bit] = digest
            digest = branch(value["prefix"], children, changed)
        return digest

    incoming, outgoing = {}, {}
    for index, row in enumerate(rows[:base_count]):
        if row["kind"] != "message_envelope":
            continue
        selected = {"id": row["id"], "timestamp": row["timestamp"], "ordinal": index}
        key = send_prefix._key("mail-member-v1", row["id"])
        entries[send_prefix._key("mail-ordinal-v1", row["id"])] = index
        for party in {row["sender"], row["recipient"]}:
            catalogs.setdefault(party, {"ordinary": None, "planes": {}, "last": None})["last"] = row["timestamp"]
        label = json.dumps(row.get("worker_session_id"), separators=(",", ":"))
        incoming.setdefault((row["recipient"], label), {})[key] = selected
        outgoing.setdefault(row["sender"], {})[key] = selected
    for (recipient, label), members in incoming.items():
        head = bulk(sorted(members.items()))
        catalogs[recipient]["planes"][label] = head
        if json.loads(label) is None:
            catalogs[recipient]["ordinary"] = head
    for sender, members in outgoing.items():
        senders[sender] = bulk(sorted(members.items()))
        entries[send_prefix._key("mail-sender-v1", sender)] = senders[sender]
    entries.update({send_prefix._key("mail-node-v1", party): value for party, value in catalogs.items()})

    for index, row in enumerate(rows[base_count:], start=base_count):
        if row["kind"] != "message_envelope":
            continue
        entries[send_prefix._key("mail-ordinal-v1", row["id"])] = index
        changed = [("mail-ordinal-v1", row["id"], index)]
        for party in sorted({row["sender"], row["recipient"]}):
            value = copy.deepcopy(catalogs.get(party, {"ordinary": None, "planes": {}, "last": None}))
            value["last"] = row["timestamp"]
            if party == row["recipient"]:
                label = json.dumps(row.get("worker_session_id"), separators=(",", ":"))
                selected = {"id": row["id"], "timestamp": row["timestamp"], "ordinal": index}
                value["planes"][label] = tree(value["planes"].get(label), selected, changed)
                if row.get("worker_session_id") is None:
                    value["ordinary"] = value["planes"][label]
            catalogs[party] = value
            entries[send_prefix._key("mail-node-v1", party)] = value
            changed.append(("mail-node-v1", party, value))
        selected = {"id": row["id"], "timestamp": row["timestamp"], "ordinal": index}
        sender = row["sender"]
        senders[sender] = tree(senders.get(sender), selected, changed)
        entries[send_prefix._key("mail-sender-v1", sender)] = senders[sender]
        changed.append(("mail-sender-v1", sender, senders[sender]))
        if capture_after is not None and index >= capture_after:
            changes.extend(changed)
    return entries, changes


def _entries(rows, known, *, base_count=None):
    entries = {send_prefix._key("id", row["id"]): row for row in rows}
    groups, companions = {}, {}
    for row in known:
        if row["kind"] == "message_envelope":
            groups.setdefault(row["idempotency_key"], row)
        elif row["kind"] == "message_retracted":
            entries[send_prefix._key("retraction", row["retracted_message_id"])] = row
        elif row["kind"] in {"delivery_claim", "send_readiness_observation", "seat_execution"}:
            reference = row.get("note_ref") if row["kind"] == "delivery_claim" else row.get("envelope_id")
            companions.setdefault((reference, row["kind"]), row)
    for key, envelope in groups.items():
        entries[send_prefix._key("send", key)] = [envelope] + [companions[(envelope["id"], kind)]
            for kind in ("delivery_claim", "send_readiness_observation", "seat_execution")
            if (envelope["id"], kind) in companions]
    complete = len(rows) == len(known)
    if complete:
        entries.update(_mail_entries(rows, base_count=base_count)[0])
    entries[send_prefix._key("mail-valid-v1", "complete")] = complete
    entries[send_prefix._key("mail-first-v1", "physical")] = rows[0] if rows else None
    return entries


def _canonical_root(entries):
    """Independently commit the complete logical map, not selected paths."""
    def node(value):
        return sha(send_prefix._bytes(value))
    def build(items):
        if not items:
            return None
        if len(items) == 1:
            key, value = items[0]
            return node({"prefix": key, "value": node(value)})
        first, last = items[0][0], items[-1][0]
        length = 0
        while length < 256 and first[length] == last[length]:
            length += 1
        require(length < 256, "duplicate logical index key")
        groups = {}
        for key, value in items:
            groups.setdefault(key[length], []).append((key, value))
        require(set(groups) == {"0", "1"}, "index branch is not binary")
        return node({"prefix": first[:length], "children": {bit: build(group) for bit, group in groups.items()}})
    return build(sorted(entries.items()))


def _head(raw):
    head = send_prefix._EMPTY_HEAD
    for frame in raw.splitlines(keepends=True):
        head = sha(bytes.fromhex(head) + frame)
    return head


def _bank_nodes(raw):
    """Decode every compact-bank byte independently of the selected reader."""
    magic = b"FLOATI-SEND-BANK\x00\x01"
    header = len(magic) + 8
    require(type(raw) is bytes and len(raw) >= header and raw.startswith(magic),
        "compact bank header differs")
    count = int.from_bytes(raw[len(magic):header], "big")
    require(count <= (len(raw) - header) // 44, "compact bank directory exceeds its bytes")
    offset = header + count * 44
    nodes, previous = {}, None
    for index in range(count):
        start = header + index * 44
        digest = raw[start:start + 32]
        selected_offset = int.from_bytes(raw[start + 32:start + 40], "big")
        length = int.from_bytes(raw[start + 40:start + 44], "big")
        require(previous is None or previous < digest, "compact bank digests are not strictly sorted")
        require(selected_offset == offset and 0 < length <= 5 * jsonl.MAX_RECORD_BYTES
            and length <= len(raw) - offset, "compact bank value bounds or contiguous offsets differ")
        data = raw[offset:offset + length]
        require(hashlib.sha256(data).digest() == digest, "compact bank member hash differs")
        nodes[digest.hex()] = _object(data)
        offset += length
        previous = digest
    require(offset == len(raw), "compact bank has unexplained trailing bytes")
    return nodes


def _checkpoint(root, cache, raw, rows, known, identity):
    require(set(cache) >= {CACHE + "/checkpoint.json", CACHE + "/seal.key", CACHE + "/bank.bin"}, "incomplete cache input")
    secret = cache[CACHE + "/seal.key"]
    require(type(secret) is bytes and len(secret) == 32, "sealing key shape changed")
    value = _object(cache[CACHE + "/checkpoint.json"])
    require(type(value) is dict and set(value) == {"payload", "seal"}, "checkpoint shape changed")
    payload = value["payload"]
    fields = {"binding", "identity", "offset", "count", "head", "trie", "generation", "mail_base_count"}
    require(type(payload) is dict and set(payload) in (fields, fields | {"pending_only"}),
        "checkpoint payload shape changed")
    if payload.get("pending_only") is not None:
        simple = {"message_envelope", "delivery_claim", "send_readiness_observation"}
        require(type(payload["pending_only"]) is int
            and all(row["kind"] in simple for row in rows)
            and raw == b"".join(map(encode_frame, rows))
            and payload["pending_only"] == sum(row["kind"] == "message_envelope" for row in rows),
            "pending count differs from canonical native send history")
    seal = hmac.new(secret, send_prefix._bytes(payload), hashlib.sha256).hexdigest()
    require(type(value["seal"]) is str and hmac.compare_digest(value["seal"], seal), "checkpoint authentication failed")
    expected_binding = {"format": send_prefix._FORMAT, "root": str(root.tenant_home), "tenant": root.tenant_id,
        "kinds": sorted(EVENT_KINDS), "maximum": jsonl.MAX_RECORD_BYTES, "ledger_bytes": jsonl.MAX_LEDGER_BYTES,
        "ledger_records": jsonl.MAX_LEDGER_RECORDS, "revision": send_prefix._revision()}
    require(payload["binding"] == expected_binding, "checkpoint binding or reader revision changed")
    require(type(payload["generation"]) is str and re.fullmatch(r"[0-9a-f]{32}", payload["generation"]) is not None,
        "checkpoint generation shape differs")
    valid_identity = (identity is None and raw == b"") or (type(identity) is list and len(identity) == 7
        and all(type(value) is int for value in identity) and identity[4] == len(raw))
    require(payload["identity"] == identity and valid_identity, "checkpoint file identity differs")
    require(type(payload["offset"]) is int and payload["offset"] == len(raw)
        and type(payload["count"]) is int and payload["count"] == len(rows)
        and payload["head"] == _head(raw), "checkpoint count, offset or exact-frame chain differs")
    require(type(payload["mail_base_count"]) is int and 0 <= payload["mail_base_count"] <= len(rows),
        "mail baseline differs from physical history")
    expected = _entries(rows, known, base_count=payload["mail_base_count"])
    require(payload["trie"] == _canonical_root(expected), "checkpoint does not commit the complete native map")
    nodes = _bank_nodes(cache[CACHE + "/bank.bin"])
    for path, data in cache.items():
        if path in {CACHE + "/checkpoint.json", CACHE + "/seal.key", CACHE + "/bank.bin"}:
            continue
        relative = Path(path).relative_to(CACHE)
        require(len(relative.parts) == 2 and relative.parts[0] == "nodes"
            and _HASH.fullmatch(relative.parts[1]) is not None, "unreviewed cache member")
        require(sha(data) == relative.parts[1], "cache member hash differs from its name")
        nodes[relative.parts[1]] = _object(data)
    seen = {}
    def walk(digest, parent="", bit=None):
        require(digest in nodes, "authenticated cache node or payload is missing")
        value = nodes[digest]
        require(type(value) is dict and set(value) in ({"prefix", "value"}, {"prefix", "children"}), "trie node shape changed")
        prefix = value["prefix"]
        require(type(prefix) is str and len(prefix) <= 256 and set(prefix) <= {"0", "1"}
            and prefix.startswith(parent) and (bit is None or prefix.startswith(parent + bit)), "trie path differs")
        if "value" in value:
            require(len(prefix) == 256 and prefix not in seen and value["value"] in nodes, "trie leaf differs")
            seen[prefix] = nodes[value["value"]]
        else:
            require(len(prefix) < 256 and set(value["children"]) == {"0", "1"}, "trie branch differs")
            for child_bit, child in value["children"].items():
                walk(child, prefix, child_bit)
    if payload["trie"] is not None:
        walk(payload["trie"])
    require(send_prefix._bytes(seen) == send_prefix._bytes(expected), "complete trie payload map differs")
    return payload, nodes


def _proof_module(filename):
    specification = importlib.util.spec_from_file_location(
        "send_warm_proof_" + filename.replace(".", "_"), Path(__file__).with_name(filename))
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def _complete_warm_mail(root, raw, rows, *, cursor_preflights=None):
    """Prove every additional native warm bank at its exact source coordinate."""
    from floati.cursor import SparseCursor

    # Carried authority must share the observer's canonical module identity.
    # Fresh aliases still perform full proofs, but cannot issue its capability.
    _proof_module("_proof_namespace.py").bind()
    from _bus_scale_fixture_proofs import bus_scale_cursor_prefix_effects as cursor_proof
    consumed_proof = _proof_module("bus_scale_consumed_prefix_effects.py")
    envelopes = [row for row in rows if row["kind"] == "message_envelope"]
    nodes = {row["recipient"] for row in envelopes}
    for family in ("deliveries", "acks", "consumed"):
        directory = root.resolve_relative(Path("receipts") / family)
        if directory.is_dir():
            nodes.update(path.stem if path.is_file() else path.name for path in directory.iterdir()
                if path.is_dir() or path.suffix == ".jsonl")
    cache_names, locks = set(), {"events.jsonl.lock"}
    cursor = SparseCursor(root)
    for node in sorted(nodes):
        locks.add("receipts/wake-coordination/" + node + "/lane.lock")
        planes = {None} | {row.get("worker_session_id") for row in envelopes if row["recipient"] == node}
        for plane in sorted(planes, key=lambda value: "" if value is None else value):
            testimony = cursor_proof.verify_complete_cursor_banks(root.tenant_home, raw,
                node=node, worker_session_id=plane, cursor_preflights=cursor_preflights)
            cache_names.update(testimony["cache_names"])
            ack = cursor._relative_path_for(node, worker_session_id=plane)
            delivery = cursor._delivery_relative_path_for(node, worker_session_id=plane)
            locks.update((ack.as_posix() + ".lock", delivery.as_posix() + ".lock"))
            consumed = Path("receipts/consumed").joinpath(*ack.relative_to("receipts/acks").parts)
            if consumed.as_posix() in raw:
                consumed_proof.verify_complete_consumed_bank(root.tenant_home, raw,
                    node=node, worker_session_id=plane)
                cache_names.add(consumed_proof.cache_name(consumed))
                locks.add(consumed.as_posix() + ".lock")
    return cache_names, locks


def verify_warmed_clone(home, expected_seed, *, full_mail=False, cursor_preflights=None):
    """Admit only a complete native rebuild at the copied fixture's inode.

    All noncache bytes retain the exact frozen seed. Each declared native
    lock may be added only when absent, empty and ordinary, with the native
    lock mode. All warmed banks receive complete proofs rather than path waivers.
    This function neither warms the input nor repairs missing cache members.
    """
    root = FloatiRoot.open_direct_home(Path(home))
    require(type(full_mail) is bool, "warm proof mode must be explicit")
    if cursor_preflights is not None:
        require(type(cursor_preflights) is list, "cursor preflight collector must be a list")
    issued = [] if cursor_preflights is not None else None
    require(type(expected_seed) is dict and all(type(path) is str and type(raw) is bytes
        for path, raw in expected_seed.items()), "frozen warm seed is not an exact byte inventory")
    actual = {}
    cache_directories = set()
    for target in (root.tenant_home, *root.tenant_home.rglob("*")):
        metadata = target.lstat()
        require(metadata.st_uid == os.getuid() and (stat.S_ISDIR(metadata.st_mode)
            or stat.S_ISREG(metadata.st_mode)), "warm clone contains an alias, special file or foreign owner")
        relative = target.relative_to(root.tenant_home).as_posix()
        is_cache = relative == CACHE or relative.startswith(CACHE + "/")
        if stat.S_ISDIR(metadata.st_mode):
            if is_cache:
                require(stat.S_IMODE(metadata.st_mode) == 0o700, "warm cache directory is not private")
                cache_directories.add(relative)
            continue
        require(metadata.st_nlink == 1, "warm clone contains a shared file")
        if is_cache:
            require(stat.S_IMODE(metadata.st_mode) == 0o600, "warm cache file is not private")
        actual[relative] = target.read_bytes()
    require(cache_directories == {CACHE, CACHE + "/nodes", CACHE + "/staging"}, "warm cache has an unexplained directory")

    cache = _cache(actual)
    raw = actual.get("events.jsonl", b"")
    rows, known = _transcript(root, raw)
    mail_caches, allowed_locks = (_complete_warm_mail(root, actual, rows, cursor_preflights=issued)
        if full_mail else (set(), {"events.jsonl.lock"}))
    cut_proof = None
    cut_changed = set()
    cut = _proof_module("bus_scale_cut_prefix_effects.py")
    if any(path == cut.CACHE or path.startswith(cut.CACHE + "/")
            for path in set(expected_seed) | set(actual)):
        # The other banks have their own complete proofs. Give the cut witness
        # their current bytes while retaining every frozen cut byte; the plain
        # comparison below still binds all non-cut changes independently.
        cut_seed = {path: raw for path, raw in actual.items()
            if path != cut.CACHE and not path.startswith(cut.CACHE + "/")}
        cut_seed.update({path: raw for path, raw in expected_seed.items()
            if path == cut.CACHE or path.startswith(cut.CACHE + "/")})
        cut_proof = cut.verify_warmed_cut_clone(home, cut_seed)
        cut_changed = set(cut_proof["changed_files"])
    proved_caches = {CACHE} | mail_caches
    def proved(path):
        return path in cut_changed or any(path == name or path.startswith(name + "/") for name in proved_caches)
    expected_plain = {path: raw for path, raw in expected_seed.items() if not proved(path)}
    actual_plain = {path: raw for path, raw in actual.items() if not proved(path)}
    added_locks = []
    for lock in sorted(allowed_locks):
        if lock not in expected_plain and actual_plain.get(lock) == b"":
            native_lock = root.tenant_home / "registry/entries.jsonl.lock"
            require(native_lock.is_file() and native_lock.relative_to(root.tenant_home).as_posix() in expected_plain,
                "new warm lock has no retained native mode witness")
            require(stat.S_IMODE((root.tenant_home / lock).lstat().st_mode)
                == stat.S_IMODE(native_lock.lstat().st_mode), "new warm lock mode differs from native lock creation")
            expected_plain[lock] = b""
            added_locks.append(lock)
    require(actual_plain == expected_plain, "warm clone changed noncache frozen seed bytes or population")
    for name in sorted(proved_caches):
        old_key = expected_seed.get(name + "/seal.key")
        if old_key is not None:
            require(actual.get(name + "/seal.key") == old_key, "warm clone changed its retained sealing key")
    identity = send_prefix._identity(root.tenant_home / "events.jsonl")
    payload, nodes = _checkpoint(root, cache, raw, rows, known, identity)
    try:
        old_payload = _object(expected_seed.get(CACHE + "/checkpoint.json", b""))["payload"]
    except (ValueError, TypeError, KeyError):
        old_payload = {}
    if type(old_payload) is not dict:
        old_payload = {}
    if old_payload.get("generation") == payload["generation"]:
        require(old_payload.get("mail_base_count") == payload["mail_base_count"],
            "warm clone changed its retained generation's mail baseline")
    else:
        require(payload["mail_base_count"] == len(rows),
            "cold warm clone did not certify its complete physical mail baseline")
    reachable = set()

    def visit(digest):
        reachable.add(digest)
        node = nodes[digest]
        if "value" in node:
            reachable.add(node["value"])
        else:
            for child in node["children"].values():
                visit(child)

    if payload["trie"] is not None:
        visit(payload["trie"])
    require(set(nodes) == reachable, "warm cache contains an unexplained unreachable node")
    require(send_prefix._identity(root.tenant_home / "events.jsonl") == identity,
        "warm event identity changed during admission proof")
    if cursor_preflights is not None:
        cursor_preflights.extend(issued)
    return {"kind": "complete_native_warm_clone", "event_records": len(rows),
        "ledger_identity": identity, "reachable_nodes": len(reachable), "added_empty_locks": added_locks,
        "cut_warm_proof": cut_proof}


class _MemoryInsert(send_prefix.Prefix):
    """Use the exact writer's insertion choreography with no filesystem seam."""
    def __init__(self, payload, nodes):
        self.payload = copy.deepcopy(payload)
        self.memo = copy.deepcopy(nodes)
        self.written = {}
    def _node(self, digest):
        require(digest in self.memo, "insertion proof lacks its authenticated prior node")
        return self.memo[digest]
    def _put(self, value):
        raw = send_prefix._bytes(value)
        digest = sha(raw)
        self.written[digest] = raw
        self.memo[digest] = copy.deepcopy(value)
        return digest


def verify_prefix_effect(home, before, after, delta, prefix_input):
    """Prove all cache state and exact new files for one native two-frame send."""
    root = FloatiRoot.open_direct_home(Path(home))
    require(type(prefix_input) is dict and set(prefix_input) == {"ledger_identity", "checkpoint_sha256"},
        "missing pre-child checkpoint/stat testimony")
    old_cache, new_cache = _cache(before), _cache(after)
    require(prefix_input["checkpoint_sha256"] == sha(old_cache.get(CACHE + "/checkpoint.json", b"")),
        "pre-child checkpoint bytes differ")
    require(old_cache.get(CACHE + "/seal.key") == new_cache.get(CACHE + "/seal.key"), "native send changed its sealing key")
    old_raw, new_raw = before.get("events.jsonl", b""), after.get("events.jsonl", b"")
    require(new_raw.startswith(old_raw), "native report send rewrote its event prefix")
    old_rows, old_known = _transcript(root, old_raw)
    new_rows, new_known = _transcript(root, new_raw)
    batch = decode_frames(new_raw[len(old_raw):])
    require([row["kind"] for row in batch] == ["message_envelope", "send_readiness_observation"],
        "cache effect is not the witnessed native report pair")
    old_payload, old_nodes = _checkpoint(root, old_cache, old_raw, old_rows, old_known, prefix_input["ledger_identity"])
    current_identity = send_prefix._identity(root.tenant_home / "events.jsonl")
    require((root.tenant_home / "events.jsonl").read_bytes() == new_raw, "post-child transcript differs from live evidence")
    new_payload, _nodes = _checkpoint(root, new_cache, new_raw, new_rows, new_known, current_identity)
    require(new_payload["identity"] is not None and (old_payload["identity"] is None
        or old_payload["identity"][:4] == new_payload["identity"][:4]), "native send replaced its selected ledger")
    require(old_payload["generation"] == new_payload["generation"],
        "native send replaced the certified event generation")
    require(old_payload["mail_base_count"] == new_payload["mail_base_count"],
        "native send replaced the certified mail baseline")
    replay = _MemoryInsert(old_payload, old_nodes)
    if not old_rows:
        replay.set("mail-first-v1", "physical", batch[0])
    for row in batch:
        replay.set("id", row["id"], row)
    replay.set("send", batch[0]["idempotency_key"], batch)
    if len(old_rows) == len(old_known):
        for domain, key, value in _mail_entries(new_rows,
                base_count=old_payload["mail_base_count"], capture_after=len(old_rows))[1]:
            replay.set(domain, key, value)
    require(replay.payload["trie"] == new_payload["trie"], "post-child trie differs from exact native insertions")
    expected_cache = dict(old_cache)
    expected_cache.update({CACHE + "/nodes/" + digest: raw for digest, raw in replay.written.items()})
    expected_cache[CACHE + "/checkpoint.json"] = new_cache[CACHE + "/checkpoint.json"]
    require(expected_cache == new_cache, "extra, removed or rewritten cache node outside the exact native insertion delta")
    paths = {path for path in set(old_cache) | set(new_cache) if old_cache.get(path) != new_cache.get(path)}
    require(paths == {path for path in delta if path == CACHE or path.startswith(CACHE + "/")}, "cache delta population differs")
    for path in paths:
        old, new = old_cache.get(path, b""), new_cache[path]
        operation = "replace" if path.endswith(".json") else "append"
        payload = new if operation == "replace" else new[len(old):]
        expected = {"operation": operation, "before_sha256": sha(old), "after_sha256": sha(new),
            "before_bytes": len(old), "after_bytes": len(new), "payload_base64": base64.b64encode(payload).decode()}
        require(delta[path] == expected, "cache delta bytes, operation or digest differs")
    for path, raw in new_cache.items():
        target = root.tenant_home / path
        metadata = target.lstat()
        require(stat.S_ISREG(metadata.st_mode) and metadata.st_nlink == 1 and metadata.st_uid == os.getuid()
            and stat.S_IMODE(metadata.st_mode) == 0o600 and target.read_bytes() == raw, "post-child cache file metadata differs")
    staging = root.tenant_home / CACHE / "staging"
    for directory in (root.tenant_home / CACHE, root.tenant_home / CACHE / "nodes", staging):
        metadata = directory.lstat()
        require(stat.S_ISDIR(metadata.st_mode) and metadata.st_uid == os.getuid()
            and stat.S_IMODE(metadata.st_mode) == 0o700, "post-child cache directory metadata differs")
    require(not any(staging.iterdir()), "post-child staging bank is not empty")
    summary = {"operation": "verified_prefix_append", "format": send_prefix._FORMAT,
        "before_records": len(old_rows), "after_records": len(new_rows),
        "appended_kinds": [row["kind"] for row in batch], "complete_transcripts_and_maps_validated": True,
        "both_checkpoint_seals_validated": True, "exact_insertion_delta_validated": True}
    return {"semantic": summary, "changed_files": sorted(paths), "before_input": copy.deepcopy(prefix_input),
        "after_identity": current_identity, "cache_before": _inventory(old_cache), "cache_after": _inventory(new_cache)}


def project_prefix_tree(before, after, proof, begin, end, *, filesystem_clock=None):
    """Bind every cache tree leaf to its byte proof before coalescing metadata."""
    from tests.bus_scale_filesystem_clock import filesystem_bounds
    lower, upper = filesystem_bounds(begin, end, filesystem_clock)
    old, new = before["entries"], after["entries"]
    for entries, inventory in ((old, proof["cache_before"]), (new, proof["cache_after"])):
        actual = {path for path, row in entries.items() if row["kind"] == "file"
            and (path == CACHE or path.startswith(CACHE + "/"))}
        require(actual == set(inventory), "cache tree file population differs from raw proof")
        for path in actual:
            row = entries[path]
            require(row["mode"] == 0o600 and row["bytes"] == inventory[path]["bytes"], "cache tree file differs from raw proof")
            if path == CACHE + "/seal.key":
                require(row["bytes"] == 32 and new[path]["sha256"] == old[path]["sha256"], "cache key tree binding differs")
            else:
                require(row["sha256"] == inventory[path]["sha256"], "cache tree file digest differs from raw proof")
        directories = {path for path, row in entries.items() if row["kind"] == "directory"
            and (path == CACHE or path.startswith(CACHE + "/"))}
        require(directories == {CACHE, CACHE + "/nodes", CACHE + "/staging"}
            and all(entries[path]["mode"] == 0o700 for path in directories), "cache tree directory population differs")
    for path in set(old) | set(new):
        if path != CACHE and not path.startswith(CACHE + "/"):
            continue
        require(path in new, "native cache tree removed an entry")
        if path in old and path not in proof["changed_files"] and new[path]["kind"] == "file":
            require(new[path] == old[path], "native cache changed retained file metadata")
        if path not in old or new[path]["mtime_ns"] != old[path]["mtime_ns"]:
            stamp = new[path]["mtime_ns"]
            require(type(stamp) is int and lower <= stamp <= upper,
                "cache tree mutation clock is outside the native invocation: " + path)
    return {"kind": "complete_validated_native_prefix_cache", "effect": copy.deepcopy(proof["semantic"])}

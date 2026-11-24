"""Complete read-only ACK cache effects and unchanged native source testimony.

Every event, delivery and ACK transcript remains retained. A complete warmed
delivery join is required; neither source changes nor auxiliary cache writes
are exempted from the native effect contract.
"""
from __future__ import annotations

import base64
import copy
import hashlib
import hmac
import importlib.util
import json
import weakref
from datetime import datetime, timezone
from pathlib import Path

from floati import jsonl, key_prefix, send_prefix
from floati.cursor import SparseCursor
from floati.framing import decode_frames, encode_frame
from floati.records import WAKE_HOLD_KINDS
from floati.root import FloatiRoot


def _load(filename):
    spec = importlib.util.spec_from_file_location("cursor_effect_" + filename.replace(".", "_"), Path(__file__).with_name(filename))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_base = _load("bus_scale_receipt_prefix_effects.py")
_events = _load("bus_scale_prefix_effects.py")
SUMMARY = "@cache/cursor-prefix"
SUMMARY_DELIVERY = "@cache/delivery-prefix"
FORMAT = "ack-prefix-v4"
DELIVERY_FORMAT = "delivery-join-prefix-v4"
KINDS = frozenset({"ack_receipt"})
_PREFLIGHT_ISSUER = object()
_PREFLIGHTS = weakref.WeakKeyDictionary()


class _CursorPreflight:
    """Invocation-local authority; its testimony is never caller supplied."""
    __slots__ = ("__weakref__",)

    def __init__(self, issuer):
        require(issuer is _PREFLIGHT_ISSUER, "cursor preflight has no native issuer")


def require(value, detail):
    if not value:
        raise ValueError("native cursor prefix cache effect: " + detail)


def cache_name(relative):
    relative = Path(relative)
    require(not relative.is_absolute() and len(relative.parts) >= 3
        and relative.parts[:2] in (("receipts", "acks"), ("receipts", "deliveries"))
        and ".." not in relative.parts and relative.suffix == ".jsonl", "cursor ledger coordinate differs")
    return ".cursor-prefix-" + _base.sha(relative.as_posix().encode("utf-8"))


def _coordinates(root, node, worker_session_id):
    cursor = SparseCursor(root)
    return (cursor._relative_path_for(node, worker_session_id=worker_session_id).as_posix(),
        cursor._delivery_relative_path_for(node, worker_session_id=worker_session_id).as_posix())


def _retry(session, items):
    return json.dumps([session, list(items)], ensure_ascii=True, separators=(",", ":"))


def _mail_add(entries, root, envelope):
    """Independently rebuild one persistent canonical radix-tree insertion."""
    item = {"id": envelope["id"], "timestamp": envelope["timestamp"],
        "ordinal": envelope["ordinal"]}
    bits = _base._key("mail-member-v1", item["id"])

    def node(digest):
        return entries[_base._key("mail-tree-v1", digest)]

    def store(value):
        digest = _base.sha(_base._bytes(value))
        entries[_base._key("mail-tree-v1", digest)] = value
        return digest

    def branch(prefix, children):
        ordered = [node(children[bit]) for bit in ("0", "1")]
        return store({"prefix": prefix, "children": children,
            "count": sum(value["count"] for value in ordered),
            "oldest_utc": min((value["oldest_utc"] for value in ordered),
                key=lambda stamp: datetime.fromisoformat(stamp.replace("Z", "+00:00")).astimezone(timezone.utc).isoformat()),
            "oldest_lex": min((value["oldest_lex"] for value in ordered),
                key=lambda value: (value["timestamp"], value["ordinal"]))})

    path, current = [], root
    while current is not None:
        value = node(current)
        prefix = value["prefix"]
        common = next((index for index, pair in enumerate(zip(prefix, bits))
            if pair[0] != pair[1]), min(len(prefix), len(bits)))
        if common != len(prefix):
            break
        if prefix == bits:
            require(value.get("item") == item, "tree membership changed its native envelope")
            return root
        bit = bits[len(prefix)]
        path.append((prefix, dict(value["children"]), bit))
        current = value["children"][bit]
    leaf = store({"prefix": bits, "item": item, "count": 1,
        "oldest_utc": item["timestamp"], "oldest_lex": item})
    if current is not None:
        leaf = branch(bits[:common], {prefix[common]: current, bits[common]: leaf})
    for prefix, children, bit in reversed(path):
        children[bit] = leaf
        leaf = branch(prefix, children)
    return leaf


def _mail_fold(rows, envelopes, entries):
    """Derive exact receipt counters and every intermediate tree from DATA."""
    by_id = {row["id"]: dict(row, ordinal=ordinal) for ordinal, row in enumerate(envelopes)
        if row["kind"] == "message_envelope"}
    value = {"members": None, "visible": None, "delivery_rows": 0,
        "ack_rows": 0, "unpresented_rows": 0, "latest": None, "latest_utc": None}
    for row in rows:
        value["latest"] = row["timestamp"]
        stamp = datetime.fromisoformat(row["timestamp"].replace("Z", "+00:00")).astimezone(timezone.utc)
        if (value["latest_utc"] is None or stamp > datetime.fromisoformat(
                value["latest_utc"].replace("Z", "+00:00")).astimezone(timezone.utc)):
            value["latest_utc"] = row["timestamp"]
        for identity in row["item_ids"]:
            require(identity in by_id, "mail tree references a missing native envelope")
            value["members"] = _mail_add(entries, value["members"], by_id[identity])
            if row["kind"] in {"delivery_receipt", "delivered_unpresented"}:
                value["visible"] = _mail_add(entries, value["visible"], by_id[identity])
        counter = {"delivery_receipt": "delivery_rows", "ack_receipt": "ack_rows",
            "delivered_unpresented": "unpresented_rows"}.get(row["kind"])
        if counter is not None:
            value[counter] += 1
    return {"mail": value}


def _ack_health(rows, delivery_rows):
    minima = []
    for source in (delivery_rows, rows):
        times = {}
        for row in source:
            stamp = datetime.fromisoformat(row["timestamp"].replace("Z", "+00:00"))
            for identity in row["item_ids"]:
                if identity not in times or stamp < times[identity]:
                    times[identity] = stamp
        minima.append(times)
    return all(identity in minima[0] and int((stamp-minima[0][identity]).total_seconds()) >= 0
        for identity, stamp in minima[1].items())


def _cursor_map(rows, delivered_ids, envelopes, delivery_rows):
    entries = _entries(rows, delivered_ids)
    folds = _mail_fold(rows, envelopes, entries)
    folds["mail"]["health_safe"] = _ack_health(rows, delivery_rows)
    return entries, folds


def _entries(rows, delivered_ids):
    """Derive the complete ACK map from rows and independently proved delivery."""
    delivered_ids = set(delivered_ids)
    entries = {_base._key("id", row["id"]): row for row in rows}
    for row in rows:
        entries[_base._key("retry", _retry(row.get("acting_session_id"), row["item_ids"]))] = row
        for item in row["item_ids"]:
            entries.setdefault(_base._key("message", item), row)
    entries[_base._key("consumption", "complete")] = all(
        item in delivered_ids for row in rows for item in row["item_ids"])
    return entries


def _validate_ack_rows(rows, node, ackable):
    ackable = set(ackable)
    for row in rows:
        require(row["recipient"] == node and set(row["item_ids"]) <= ackable,
            "ACK references invalid native delivery or worker plane")


def _ack_binding(root, relative, node, worker_session_id, auxiliary):
    binding = _binding(root, relative, KINDS, FORMAT)
    binding["folds"] = {"mail": 1}
    binding.update(node=node, worker_session_id=worker_session_id,
        delivery={"generation": auxiliary["delivery_generation"],
                  "binding": auxiliary["delivery_binding"]})
    return binding


def _transcript(root, relative, kinds, raw):
    rows = jsonl._decode_path_records(root.tenant_home / relative, root.tenant_id, kinds, raw)
    require(b"".join(encode_frame(row) for row in rows) == raw, "noncanonical native cursor transcript")
    return rows


def _binding(root, relative, kinds, format):
    return {"format": format, "index_format": key_prefix.INDEX_FORMAT,
        "bucket_count": key_prefix.BUCKET_COUNT, "bucket_bytes": key_prefix.BUCKET_BYTES,
        "bucket_capacity": key_prefix.BUCKET_CAPACITY, "root": str(root.tenant_home), "tenant": root.tenant_id,
        "bucket_encoding": _base._bucket_encoding(),
        "kinds": sorted(kinds), "maximum": jsonl.MAX_RECORD_BYTES, "ledger_bytes": jsonl.MAX_LEDGER_BYTES,
        "ledger_records": jsonl.MAX_LEDGER_RECORDS, "revision": send_prefix._revision(), "relative": relative,
        "folds": {}}


def _checkpoint(cache, name, raw, rows, identity, binding, expected, *, folds=None):
    require(set(cache) >= {name + "/checkpoint.json", name + "/seal.key"}, "incomplete cache checkpoint")
    key = cache[name + "/seal.key"]
    require(type(key) is bytes and len(key) == 32, "sealing key shape differs")
    value = _base._object(cache[name + "/checkpoint.json"])
    require(type(value) is dict and set(value) == {"payload", "seal"}, "checkpoint shape differs")
    payload = value["payload"]
    require(type(payload) is dict and set(payload) == {"binding", "identity", "offset", "count", "head", "generation", "directory_sha256", "folds"},
        "checkpoint payload shape differs")
    require(type(value["seal"]) is str and hmac.compare_digest(value["seal"],
        hmac.new(key, _base._bytes(payload), hashlib.sha256).hexdigest()), "checkpoint authentication failed")
    require(payload["binding"] == binding, "checkpoint binding or reader revision differs")
    expected_folds = {} if folds is None else folds
    require(type(payload["folds"]) is dict and payload["folds"] == expected_folds
        and set(payload["folds"]) == set(binding["folds"]),
        "checkpoint folds differ from complete native history")
    valid_identity = (identity is None and raw == b"") or (type(identity) is list and len(identity) == 7
        and all(type(number) is int for number in identity) and identity[4] == len(raw))
    require(valid_identity and payload["identity"] == identity, "checkpoint file identity differs")
    require(type(payload["offset"]) is int and payload["offset"] == len(raw)
        and type(payload["count"]) is int and payload["count"] == len(rows)
        and payload["head"] == _base._head(raw), "checkpoint count, offset or exact-frame chain differs")
    require(type(payload["generation"]) is str and len(payload["generation"]) == 32
        and all(value in "0123456789abcdef" for value in payload["generation"]), "checkpoint generation shape differs")
    directory, pages = _base._bucket_map(expected, key, payload["generation"])
    require(cache.get(name + "/directory.bin") == directory and payload["directory_sha256"] == _base.sha(directory),
        "complete membership directory differs from native map")
    paths = {name + "/nodes/" + format(int(lookup, 2), "064x"): lookup for lookup in expected}
    buckets = {name + "/buckets/" + index: page for index, page in pages.items()}
    require(set(cache) == {name + "/checkpoint.json", name + "/seal.key", name + "/directory.bin"} | set(paths) | set(buckets),
        "complete native map namespace population differs")
    require(all(cache[path] == page for path, page in buckets.items()), "complete membership bucket differs from native map")
    for path in buckets:
        _base._storage_decode(cache[path])
    for path, lookup in paths.items():
        entry = _base._object(cache[path])
        require(type(entry) is dict and set(entry) == {"payload", "seal"}, "direct entry shape differs")
        body = {"index_format": key_prefix.INDEX_FORMAT, "binding_sha256": _base.sha(_base._bytes(binding)),
            "generation": payload["generation"], "key": lookup, "value": expected[lookup]}
        require(entry["payload"] == body, "complete native map entry, generation or domain/key differs")
        require(type(entry["seal"]) is str and hmac.compare_digest(entry["seal"],
            hmac.new(key, _base._bytes(body), hashlib.sha256).hexdigest()), "direct entry authentication failed")
    return payload, expected


def _auxiliary(root, raw, node, worker_session_id, delivery, identities):
    """Validate every native delivery reference, including unrequested history."""
    event_raw = raw.get("events.jsonl", b"")
    physical, known = _events._transcript(root, event_raw)
    event_cache = _events._cache(raw)
    event_metadata = _base._metadata(root.tenant_home, ".send-prefix")
    _base._check_metadata(event_metadata, event_cache, ".send-prefix")
    event_payload, _ = _events._checkpoint(root, event_cache, event_raw, physical, known, identities["events.jsonl"])
    delivery_raw = raw.get(delivery, b"")
    rows = _transcript(root, delivery, WAKE_HOLD_KINDS, delivery_raw)
    by_id = {row["id"]: row for row in known if row["kind"] == "message_envelope"}
    ackable = set()
    for row in rows:
        require(row.get("recipient") == node, "delivery belongs to another recipient")
        for item in row["item_ids"]:
            event = by_id.get(item)
            require(event is not None and event.get("recipient") == node
                and event.get("worker_session_id") == worker_session_id, "delivery references invalid native event or worker plane")
            ackable.add(item)
    name = cache_name(delivery)
    cache = _base._cache(raw, name)
    metadata = _base._metadata(root.tenant_home, name)
    _base._check_metadata(metadata, cache, name, bucketed=True)
    binding = _binding(root, delivery, WAKE_HOLD_KINDS, DELIVERY_FORMAT)
    binding["folds"] = {"mail": 1}
    binding.update(node=node, worker_session_id=worker_session_id,
        events={"generation": event_payload["generation"], "binding": event_payload["binding"]})
    entries = {_base._key("id", row["id"]): row for row in rows}
    entries.update({_base._key("ackable", item): True for item in ackable})
    for row in rows:
        if row["kind"] in {"delivery_receipt", "delivered_unpresented"}:
            for item in row["item_ids"]:
                entries.setdefault(_base._key("delivered", item), row)
    folds = _mail_fold(rows, physical, entries)
    delivery_payload, _map = _checkpoint(cache, name, delivery_raw, rows, identities[delivery], binding,
        entries, folds=folds)
    for coordinate, value in {**event_cache, **cache}.items():
        require((root.tenant_home / coordinate).read_bytes() == value, "live auxiliary cache differs from retained bytes")
    return {"event_records": len(physical), "delivery_records": len(rows), "ackable_items": len(ackable),
        "ackable_ids": sorted(ackable),
        "delivered_ids": sorted({item for row in rows
            if row["kind"] in {"delivery_receipt", "delivered_unpresented"} for item in row["item_ids"]}),
        "retracted_ids": sorted(row["retracted_message_id"] for row in known if row["kind"] == "message_retracted"),
        "delivery_binding": binding, "delivery_generation": delivery_payload["generation"],
        "complete_native_join_validated": True}, {".send-prefix": event_metadata, name: metadata}


def capture_cursor_prefix_input(home, before, *, node="architect", worker_session_id=None,
        auxiliary_unavailable_temporal_certificate=False):
    require(type(auxiliary_unavailable_temporal_certificate) is bool,
        "auxiliary temporal certificate mode is not explicit")
    root = FloatiRoot.open_direct_home(Path(home))
    ack, delivery = _coordinates(root, node, worker_session_id)
    name = cache_name(ack)
    identities = {relative: send_prefix._identity(root.tenant_home / relative)
        for relative in (ack, delivery, "events.jsonl")}
    for relative in identities:
        path = root.tenant_home / relative
        require((path.read_bytes() if path.exists() else b"") == before.get(relative, b""), "pre-child native source changed")
    auxiliary, auxiliary_metadata = _auxiliary(root, before, node, worker_session_id, delivery, identities)
    cache = _base._cache(before, name)
    metadata = _base._metadata(root.tenant_home, name)
    if cache:
        _base._check_metadata(metadata, cache, name, bucketed=True)
        require(name + "/checkpoint.json" in cache, "input ACK checkpoint is missing")
        rows = _transcript(root, ack, KINDS, before.get(ack, b""))
        _validate_ack_rows(rows, node, auxiliary["ackable_ids"])
        _physical, known = _events._transcript(root, before.get("events.jsonl", b""))
        delivery_rows = _transcript(root, delivery, WAKE_HOLD_KINDS, before.get(delivery, b""))
        entries, folds = _cursor_map(rows, auxiliary["delivered_ids"], _physical, delivery_rows)
        if auxiliary_unavailable_temporal_certificate:
            # A conservative append may decline temporal certification even
            # when a later cold replay could prove it. Auxiliary consumers
            # gain no temporal truth from False; native observation must run.
            # True still has to equal the independently replayed time fact.
            checkpoint = _base._object(cache[name + "/checkpoint.json"])
            try:
                certificate = checkpoint["payload"]["folds"]["mail"]["health_safe"]
            except (KeyError, TypeError):
                certificate = None  # The complete checkpoint check refuses malformed folds.
            if certificate is False:
                folds["mail"]["health_safe"] = False
        _checkpoint(cache, name, before.get(ack, b""), rows, identities[ack],
            _ack_binding(root, ack, node, worker_session_id, auxiliary),
            entries, folds=folds)
        for coordinate, value in cache.items():
            require((root.tenant_home / coordinate).read_bytes() == value, "pre-child ACK cache changed")
    else:
        require(not metadata and before.get(ack, b"") == b"", "absent ACK cache requires exact native empty prior")
    return {"node": node, "worker_session_id": worker_session_id, "ack_relative": ack, "delivery_relative": delivery,
        "identities": identities, "checkpoint_sha256": _base.sha(cache[name + "/checkpoint.json"]) if cache else None,
        "cache_metadata": metadata, "auxiliary_metadata": auxiliary_metadata, "auxiliary_proof": auxiliary}


def _content_certificate(raw, name, relative):
    """Project only after the caller proved the complete native map and seal."""
    payload = _base._object(raw[name + "/checkpoint.json"])["payload"]
    def binding(value):
        if type(value) is dict:
            return {key: binding(item) for key, item in value.items() if key != "generation"}
        if type(value) is list:
            return [binding(item) for item in value]
        return value
    expected = {_base._object(value)["payload"]["key"]: _base._object(value)["payload"]["value"]
        for coordinate, value in raw.items() if coordinate.startswith(name + "/nodes/")}
    folds = copy.deepcopy(payload["folds"])
    if payload["binding"]["format"] == "consumed-prefix-v2":
        # The consumed verifier has proved these exact generations/identities
        # against the complete native join. Compare their content across arms;
        # retained_mail_trees still checks every within-arm byte and metadata.
        mail = folds["mail"]
        del mail["event_generation"]
        dependencies = mail["dependencies"]
        if dependencies is not None:
            for source in ("events", "delivery", "ack"):
                dependencies[source] = {"binding": binding(dependencies[source]["binding"])}
            work = dependencies["work"]
            if work is not None:
                dependencies["work"] = {"path": work["path"],
                    "source_sha256": _base.sha(raw.get(work["path"], b""))}
    return {"kind": "complete_validated_native_cursor_prefix_cache",
        "source_sha256": _base.sha(raw.get(relative, b"")),
        **{key: payload[key] for key in ("offset", "count", "head")}, "folds": folds,
        "map_sha256": _base.sha(_base._bytes(expected)), "binding": binding(payload["binding"])}


def _preflight_stamp(root, raw, node, worker_session_id):
    home = root.tenant_home
    require(home.is_absolute() and home.resolve(strict=True) == home,
        "cursor preflight root is not canonical")
    ack, delivery = _coordinates(root, node, worker_session_id)
    require(all((home / relative).resolve(strict=False) == home / relative
        for relative in (ack, delivery, "events.jsonl")), "cursor preflight source contains an alias")
    names = (".send-prefix", cache_name(delivery), cache_name(ack))
    metadata = {name: _base._metadata(home, name) for name in names}
    retained = {relative: raw.get(relative, b"") for relative in (ack, delivery, "events.jsonl")}
    for name in names:
        cache = _base._cache(raw, name)
        _base._check_metadata(metadata[name], cache, name, bucketed=name != ".send-prefix")
        retained.update(cache)
    coordinates = set(retained) | {path for captured in metadata.values() for path in captured}
    return {"root": str(home), "tenant": root.tenant_id,
        "root_identity": send_prefix._identity(home)[:2],
        "root_metadata": [home.stat().st_mode, home.stat().st_uid], "revision": send_prefix._revision(),
        "node": node, "worker_session_id": worker_session_id, "metadata": metadata,
        "identities": {path: send_prefix._identity(home / path) for path in coordinates},
        "retained": retained, "hashes": {path: _base.sha(value) for path, value in retained.items()}}


def reuse_cursor_preflight(home, raw, *, node, worker_session_id=None, cursor_preflights=()):
    """Return a certificate only while its complete native input is unchanged."""
    require(type(cursor_preflights) in (tuple, list), "cursor preflights are not an invocation-local sequence")
    for carrier in cursor_preflights:
        if type(carrier) is not _CursorPreflight or carrier not in _PREFLIGHTS:
            continue  # Serialized or forged data has no authority: verify below.
        captured, testimony = _PREFLIGHTS[carrier]
        if (captured["node"], captured["worker_session_id"]) != (node, worker_session_id):
            continue
        root = FloatiRoot.open_direct_home(Path(home))
        require(_preflight_stamp(root, raw, node, worker_session_id) == captured,
            "carried cursor preflight differs from exact live native input")
        return copy.deepcopy(testimony)
    return None


def verify_complete_cursor_banks(home, raw, *, node, worker_session_id=None, cursor_preflights=None):
    """Verify warmed banks completely, without requiring a receipt append."""
    root = FloatiRoot.open_direct_home(Path(home))
    if cursor_preflights is not None:
        require(type(cursor_preflights) is list, "cursor preflight collector must be a list")
        preflight_stamp = _preflight_stamp(root, raw, node, worker_session_id)
    ack, delivery = _coordinates(root, node, worker_session_id)
    identities = {relative: send_prefix._identity(root.tenant_home / relative)
        for relative in (ack, delivery, "events.jsonl")}
    for relative in identities:
        path = root.tenant_home / relative
        require((path.read_bytes() if path.exists() else b"") == raw.get(relative, b""),
            "complete warm proof source differs from retained native bytes")
    auxiliary, metadata = _auxiliary(root, raw, node, worker_session_id, delivery, identities)
    name = cache_name(ack)
    cache = _base._cache(raw, name)
    metadata[name] = _base._metadata(root.tenant_home, name)
    _base._check_metadata(metadata[name], cache, name, bucketed=True)
    rows = _transcript(root, ack, KINDS, raw.get(ack, b""))
    _validate_ack_rows(rows, node, auxiliary["ackable_ids"])
    _physical, known = _events._transcript(root, raw.get("events.jsonl", b""))
    delivery_rows = _transcript(root, delivery, WAKE_HOLD_KINDS, raw.get(delivery, b""))
    entries, folds = _cursor_map(rows, auxiliary["delivered_ids"], _physical, delivery_rows)
    _checkpoint(cache, name, raw.get(ack, b""), rows, identities[ack],
        _ack_binding(root, ack, node, worker_session_id, auxiliary), entries, folds=folds)
    for relative, value in cache.items():
        require((root.tenant_home / relative).read_bytes() == value,
            "complete warm proof ACK bank differs from retained bytes")
    require(all(send_prefix._identity(root.tenant_home / relative) == identity
        for relative, identity in identities.items()), "complete warm proof source changed")
    require(all(_base._metadata(root.tenant_home, cache_name) == captured
        for cache_name, captured in metadata.items()), "complete warm proof bank metadata changed")
    names = [cache_name(delivery), name]
    certificates = {name: _content_certificate(raw, name, relative)
        for name, relative in zip(names, (delivery, ack))}
    testimony = {"cache_names": names, "cache_metadata": {name: metadata[name] for name in names},
        "certificates": certificates, "native_result": {
            label: {"identity": identities[relative],
                    **{key: certificates[cache][key] for key in ("count", "head")}}
            for label, relative, cache in (("delivery", delivery, names[0]), ("ack", ack, name))}}
    if cursor_preflights is not None:
        require(_preflight_stamp(root, raw, node, worker_session_id) == preflight_stamp,
            "native cursor input changed while issuing its preflight")
        carrier = _CursorPreflight(_PREFLIGHT_ISSUER)
        _PREFLIGHTS[carrier] = (preflight_stamp, copy.deepcopy(testimony))
        cursor_preflights.append(carrier)
    return testimony


def raw_candidate_delta(before, after):
    """Retain every changed path and exact bytes, including hostile rewrites."""
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
            "before_sha256": _base.sha(old), "after_sha256": _base.sha(new or b""),
            "payload_base64": base64.b64encode(payload).decode()}
    return result


def _event_evidence(root, raw, identity):
    transcript = raw.get("events.jsonl", b"")
    rows, known = _events._transcript(root, transcript)
    cache = _events._cache(raw)
    metadata = _base._metadata(root.tenant_home, ".send-prefix")
    _base._check_metadata(metadata, cache, ".send-prefix")
    payload, _ = _events._checkpoint(root, cache, transcript, rows, known, identity)
    for path, value in cache.items():
        require((root.tenant_home / path).read_bytes() == value, "live event cache differs from retained bytes")
    return payload, rows, metadata


def _delivery_entries(root, relative, raw, known, node, worker_session_id):
    rows = _transcript(root, relative, WAKE_HOLD_KINDS, raw)
    events = {row["id"]: row for row in known if row["kind"] == "message_envelope"}
    entries = {_base._key("id", row["id"]): row for row in rows}
    for row in rows:
        require(row["recipient"] == node, "delivery belongs to another recipient")
        for item in row["item_ids"]:
            event = events.get(item)
            require(event is not None and event["recipient"] == node and event.get("worker_session_id") == worker_session_id,
                "delivery references invalid native event or worker plane")
            entries[_base._key("ackable", item)] = True
            if row["kind"] in {"delivery_receipt", "delivered_unpresented"}:
                entries.setdefault(_base._key("delivered", item), row)
    _mail_fold(rows, known, entries)
    return rows, entries


def capture_delivery_prefix_input(home, before, *, node="architect", worker_session_id=None):
    root = FloatiRoot.open_direct_home(Path(home))
    _ack, relative = _coordinates(root, node, worker_session_id)
    name = cache_name(relative)
    identities = {path: send_prefix._identity(root.tenant_home / path) for path in (relative, "events.jsonl")}
    for path in identities:
        target = root.tenant_home / path
        require((target.read_bytes() if target.exists() else b"") == before.get(path, b""), "pre-child native delivery source changed")
    event_payload, known, event_metadata = _event_evidence(root, before, identities["events.jsonl"])
    rows, entries = _delivery_entries(root, relative, before.get(relative, b""), known, node, worker_session_id)
    cache = _base._cache(before, name)
    metadata = _base._metadata(root.tenant_home, name)
    binding = _binding(root, relative, WAKE_HOLD_KINDS, DELIVERY_FORMAT)
    binding["folds"] = {"mail": 1}
    binding.update(node=node, worker_session_id=worker_session_id,
        events={"generation": event_payload["generation"], "binding": event_payload["binding"]})
    if cache:
        _base._check_metadata(metadata, cache, name, bucketed=True)
        _checkpoint(cache, name, before.get(relative, b""), rows, identities[relative], binding, entries,
            folds=_mail_fold(rows, known, entries))
        for path, value in cache.items():
            require((root.tenant_home / path).read_bytes() == value, "pre-child delivery cache changed")
    else:
        require(not rows and not metadata, "absent delivery cache requires exact native empty prior")
    return {"node": node, "worker_session_id": worker_session_id, "delivery_relative": relative,
        "identities": identities, "checkpoint_sha256": _base.sha(cache[name + "/checkpoint.json"]) if cache else None,
        "cache_metadata": metadata, "event_metadata": event_metadata, "event_generation": event_payload["generation"]}


def verify_delivery_prefix_effect(home, before, after, delta, prefix_input, *, node="architect", worker_session_id=None):
    root = FloatiRoot.open_direct_home(Path(home))
    _ack, relative = _coordinates(root, node, worker_session_id)
    name = cache_name(relative)
    require(type(prefix_input) is dict and set(prefix_input) == {"node", "worker_session_id", "delivery_relative",
        "identities", "checkpoint_sha256", "cache_metadata", "event_metadata", "event_generation"}, "delivery input testimony shape differs")
    require(prefix_input["node"] == node and prefix_input["worker_session_id"] == worker_session_id
        and prefix_input["delivery_relative"] == relative, "selected delivery plane differs")
    event_identity = prefix_input["identities"]["events.jsonl"]
    require(before.get("events.jsonl", b"") == after.get("events.jsonl", b"")
        and send_prefix._identity(root.tenant_home / "events.jsonl") == event_identity, "delivery append changed event source")
    require(_events._cache(before) == _events._cache(after), "delivery append changed event cache")
    event_payload, known, event_metadata = _event_evidence(root, after, event_identity)
    require(event_metadata == prefix_input["event_metadata"] and event_payload["generation"] == prefix_input["event_generation"],
        "delivery append changed event generation or private metadata")
    old_raw, new_raw = before.get(relative, b""), after.get(relative, b"")
    require(new_raw.startswith(old_raw), "native delivery rewrote its prefix")
    old_rows, old_entries = _delivery_entries(root, relative, old_raw, known, node, worker_session_id)
    new_rows, new_entries = _delivery_entries(root, relative, new_raw, known, node, worker_session_id)
    batch = decode_frames(new_raw[len(old_raw):])
    require(len(batch) == 1 and batch[0]["kind"] in {"delivery_receipt", "delivered_unpresented"}
        and batch[0]["presentation_count"] == len(old_rows) + 1, "effect is not one native counted delivery append")
    old_cache, new_cache = _base._cache(before, name), _base._cache(after, name)
    binding = _binding(root, relative, WAKE_HOLD_KINDS, DELIVERY_FORMAT)
    binding["folds"] = {"mail": 1}
    binding.update(node=node, worker_session_id=worker_session_id,
        events={"generation": event_payload["generation"], "binding": event_payload["binding"]})
    cold = not old_cache
    if cold:
        require(not old_rows and prefix_input["checkpoint_sha256"] is None and prefix_input["cache_metadata"] == {},
            "cold delivery cache did not have exact absent/empty prior")
        old_payload = {"identity": prefix_input["identities"][relative]}
    else:
        _base._check_metadata(prefix_input["cache_metadata"], old_cache, name, bucketed=True)
        require(_base.sha(old_cache[name + "/checkpoint.json"]) == prefix_input["checkpoint_sha256"], "delivery input checkpoint bytes differ")
        require(old_cache[name + "/seal.key"] == new_cache[name + "/seal.key"], "delivery append changed retained sealing key")
        old_payload, _ = _checkpoint(old_cache, name, old_raw, old_rows, prefix_input["identities"][relative], binding, old_entries,
            folds=_mail_fold(old_rows, known, old_entries))
    identity = send_prefix._identity(root.tenant_home / relative)
    require((root.tenant_home / relative).read_bytes() == new_raw, "live delivery transcript differs")
    new_payload, _ = _checkpoint(new_cache, name, new_raw, new_rows, identity, binding, new_entries,
        folds=_mail_fold(new_rows, known, new_entries))
    require(identity is not None and (old_payload["identity"] is None or old_payload["identity"][:4] == identity[:4]),
        "native delivery replaced selected ledger")
    require(cold or new_payload["generation"] == old_payload["generation"], "delivery append changed immutable generation")
    fresh = set(new_entries) - set(old_entries)
    require(fresh and all(new_entries[key] == old_entries[key] for key in set(new_entries) & set(old_entries)), "delivery append replaced existing immutable value")
    expected_cache = dict(old_cache)
    for key in fresh:
        path = name + "/nodes/" + format(int(key, 2), "064x")
        expected_cache[path] = new_cache[path]
    directory, pages = _base._bucket_map(new_entries, new_cache[name + "/seal.key"], new_payload["generation"])
    expected_cache[name + "/directory.bin"] = directory
    expected_cache.update({name + "/buckets/" + index: value for index, value in pages.items()})
    expected_cache[name + "/checkpoint.json"] = new_cache[name + "/checkpoint.json"]
    if cold:
        expected_cache[name + "/seal.key"] = new_cache[name + "/seal.key"]
    require(expected_cache == new_cache, "delivery cache differs from exact native insertion delta")
    paths = {path for path in set(old_cache) | set(new_cache) if old_cache.get(path) != new_cache.get(path)}
    require(paths == {path for path in delta if path == name or path.startswith(name + "/")}, "delivery cache delta population differs")
    expected_delta = raw_candidate_delta(old_cache, new_cache)
    require(all(delta[path] == expected_delta[path] for path in paths), "delivery cache delta bytes differ")
    _base._check_metadata(_base._metadata(root.tenant_home, name), new_cache, name, bucketed=True)
    for path, value in new_cache.items():
        require((root.tenant_home / path).read_bytes() == value, "live delivery cache differs from retained bytes")
    require(send_prefix._identity(root.tenant_home / relative) == identity, "delivery identity changed during proof")
    semantic = {"operation": "verified_delivery_prefix_append", "format": DELIVERY_FORMAT,
        "before_records": len(old_rows), "after_records": len(new_rows), "appended_kinds": [batch[0]["kind"]],
        "input_cache_state": "absent_native_empty" if cold else "complete_warmed_prefix",
        "complete_transcripts_and_maps_validated": True, "complete_membership_and_entry_seals_validated": True,
        "input_checkpoint_seal_validated": not cold, "output_checkpoint_seal_validated": True,
        "exact_insertion_delta_validated": True, "unchanged_complete_event_source_validated": True}
    return {"semantic": semantic, "changed_files": sorted(paths), "before_input": copy.deepcopy(prefix_input),
        "after_identity": identity, "cache_before": _base._inventory(old_cache), "cache_after": _base._inventory(new_cache),
        "auxiliary_unchanged": {"cache_names": [".send-prefix"]}}


def verify_cursor_prefix_effect(home, before, after, delta, prefix_input, *, node="architect", worker_session_id=None,
        delivery_input=None):
    root = FloatiRoot.open_direct_home(Path(home))
    ack, delivery = _coordinates(root, node, worker_session_id)
    name = cache_name(ack)
    require(type(prefix_input) is dict and set(prefix_input) == {"node", "worker_session_id", "ack_relative",
        "delivery_relative", "identities", "checkpoint_sha256", "cache_metadata", "auxiliary_metadata", "auxiliary_proof"}, "pre-child testimony shape differs")
    require(prefix_input["node"] == node and prefix_input["worker_session_id"] == worker_session_id
        and prefix_input["ack_relative"] == ack and prefix_input["delivery_relative"] == delivery, "selected ACK plane differs")
    old_cache, new_cache = _base._cache(before, name), _base._cache(after, name)
    old_raw, new_raw = before.get(ack, b""), after.get(ack, b"")
    require(new_raw.startswith(old_raw), "native ACK rewrote its prefix")
    old_rows, new_rows = _transcript(root, ack, KINDS, old_raw), _transcript(root, ack, KINDS, new_raw)
    batch = decode_frames(new_raw[len(old_raw):])
    require(len(batch) == 1 and batch[0]["recipient"] == node, "cache effect is not one selected native ACK append")
    selected = batch[0]
    require(all(_retry(row.get("acting_session_id"), row["item_ids"]) != _retry(selected.get("acting_session_id"), selected["item_ids"])
        for row in old_rows), "selected ACK retry was not fresh")
    delivery_effect = (verify_delivery_prefix_effect(home, before, after, delta, delivery_input,
        node=node, worker_session_id=worker_session_id) if delivery_input is not None else None)
    auxiliary_names = (".send-prefix", cache_name(delivery))
    for relative in ("events.jsonl", delivery):
        if relative != delivery or delivery_effect is None:
            require(before.get(relative, b"") == after.get(relative, b"")
                and send_prefix._identity(root.tenant_home / relative) == prefix_input["identities"][relative], "native ACK changed its event or delivery source")
        path = root.tenant_home / relative
        require((path.read_bytes() if path.exists() else b"") == after.get(relative, b""), "live native source differs")
    for auxiliary_name in auxiliary_names:
        if auxiliary_name == cache_name(delivery) and delivery_effect is not None:
            continue
        require(_base._cache(before, auxiliary_name) == _base._cache(after, auxiliary_name), "native ACK changed an auxiliary cache")
        require(_base._metadata(root.tenant_home, auxiliary_name) == prefix_input["auxiliary_metadata"][auxiliary_name], "native ACK changed auxiliary cache metadata")
    after_identities = dict(prefix_input["identities"])
    if delivery_effect is not None:
        after_identities[delivery] = delivery_effect["after_identity"]
    auxiliary, auxiliary_metadata = _auxiliary(root, after, node, worker_session_id, delivery, after_identities)
    if delivery_effect is None:
        require(auxiliary == prefix_input["auxiliary_proof"] and auxiliary_metadata == prefix_input["auxiliary_metadata"], "native complete source join differs")
    # The output delivery proof independently binds its complete source and
    # generation. In a combined drain the already-verified append preserves
    # that generation; do not substitute the child's ACK binding as evidence.
    require(prefix_input["auxiliary_proof"].get("delivery_binding") == auxiliary["delivery_binding"]
        and prefix_input["auxiliary_proof"].get("delivery_generation") == auxiliary["delivery_generation"],
        "ACK join changed its independently proved delivery generation")
    _physical, known = _events._transcript(root, before.get("events.jsonl", b""))
    prior_deliveries, _prior_entries = _delivery_entries(root, delivery,
        before.get(delivery, b""), known, node, worker_session_id)
    old_ackable = {item for row in prior_deliveries for item in row["item_ids"]}
    _validate_ack_rows(old_rows, node, old_ackable)
    _validate_ack_rows(new_rows, node, auxiliary["ackable_ids"])
    require(type(selected.get("acting_session_id")) is str and bool(selected["acting_session_id"])
        and set(selected["item_ids"]) <= set(auxiliary["ackable_ids"])
        and not set(selected["item_ids"]).intersection(auxiliary["retracted_ids"]),
        "selected ACK does not correspond to live delivered native evidence")
    cold = not old_cache
    binding = _ack_binding(root, ack, node, worker_session_id, auxiliary)
    prior_delivered = {item for row in prior_deliveries
        if row["kind"] in {"delivery_receipt", "delivered_unpresented"} for item in row["item_ids"]}
    old_entries, old_folds = _cursor_map(old_rows, prior_delivered, _physical, prior_deliveries)
    if cold:
        old_entries = {}
    current_deliveries = _transcript(root, delivery, WAKE_HOLD_KINDS, after.get(delivery, b""))
    new_entries, new_folds = _cursor_map(new_rows, auxiliary["delivered_ids"], _physical, current_deliveries)
    delivery_folds = _mail_fold(current_deliveries, _physical, {})
    latest = delivery_folds["mail"]["latest_utc"]
    upper = None if latest is None else datetime.fromisoformat(latest.replace("Z", "+00:00"))
    new_folds["mail"]["health_safe"] = old_folds["mail"]["health_safe"] and all(
        not row["item_ids"] or upper is not None and int((datetime.fromisoformat(
            row["timestamp"].replace("Z", "+00:00"))-upper).total_seconds()) >= 0
        for row in batch)
    if cold:
        require(old_raw == b"" and prefix_input["checkpoint_sha256"] is None
            and prefix_input["cache_metadata"] == {}, "cold ACK cache did not have exact absent/empty prior")
        old_payload = {"binding": binding, "identity": prefix_input["identities"][ack],
            "offset": 0, "count": 0, "head": _base._head(b"")}
        require(old_payload["identity"] is None or (type(old_payload["identity"]) is list
            and len(old_payload["identity"]) == 7 and old_payload["identity"][4] == 0), "cold native ACK identity differs")
    else:
        _base._check_metadata(prefix_input["cache_metadata"], old_cache, name, bucketed=True)
        require(prefix_input["checkpoint_sha256"] == _base.sha(old_cache.get(name + "/checkpoint.json", b"")), "pre-child checkpoint bytes differ")
        require(old_cache.get(name + "/seal.key") == new_cache.get(name + "/seal.key"), "native ACK changed retained sealing key")
        old_payload, _ = _checkpoint(old_cache, name, old_raw, old_rows,
            prefix_input["identities"][ack], binding, old_entries, folds=old_folds)
    identity = send_prefix._identity(root.tenant_home / ack)
    require((root.tenant_home / ack).read_bytes() == new_raw, "live ACK transcript differs")
    new_payload, _ = _checkpoint(new_cache, name, new_raw, new_rows, identity, binding, new_entries,
        folds=new_folds)
    require(identity is not None and (old_payload["identity"] is None or old_payload["identity"][:4] == identity[:4]), "native ACK replaced its selected ledger")
    require(cold or old_payload["generation"] == new_payload["generation"], "native ACK changed immutable bank generation")
    fresh = set(new_entries) - set(old_entries)
    required = {_base._key("id", selected["id"]),
        _base._key("retry", _retry(selected.get("acting_session_id"), selected["item_ids"]))}
    require(len(required) == 2 and required <= fresh
        and all(new_entries[key] == old_entries[key] for key in set(old_entries) & set(new_entries)),
        "native ACK did not preserve old immutable values and insert its new ID/retry keys")
    inserted = {name + "/nodes/" + format(int(key, 2), "064x") for key in fresh}
    require(not inserted.intersection(old_cache), "native ACK overwrote an existing immutable key")
    expected_cache = dict(old_cache)
    expected_cache.update({path: new_cache[path] for path in inserted})
    directory, pages = _base._bucket_map(new_entries, new_cache[name + "/seal.key"], new_payload["generation"])
    expected_cache[name + "/directory.bin"] = directory
    expected_cache.update({name + "/buckets/" + index: value for index, value in pages.items()})
    expected_cache[name + "/checkpoint.json"] = new_cache[name + "/checkpoint.json"]
    if cold:
        expected_cache[name + "/seal.key"] = new_cache[name + "/seal.key"]
    require(expected_cache == new_cache, "extra, removed or rewritten ACK cache node outside exact native insertion delta")
    paths = {path for path in set(old_cache) | set(new_cache) if old_cache.get(path) != new_cache.get(path)}
    require(paths == {path for path in delta if path == name or path.startswith(name + "/")}, "ACK cache delta population differs")
    expected_delta = raw_candidate_delta(old_cache, new_cache)
    require(all(delta[path] == expected_delta[path] for path in paths), "ACK cache delta bytes, operation or digest differs")
    _base._check_metadata(_base._metadata(root.tenant_home, name), new_cache, name, bucketed=True)
    for coordinate, value in new_cache.items():
        require((root.tenant_home / coordinate).read_bytes() == value, "post-child ACK cache differs from retained bytes")
    require(send_prefix._identity(root.tenant_home / ack) == identity, "native ACK identity changed during proof")
    semantic = {"operation": "verified_cursor_prefix_append", "format": FORMAT,
        "before_records": len(old_rows), "after_records": len(new_rows), "appended_kinds": ["ack_receipt"],
        "input_cache_state": "absent_native_empty" if cold else "complete_warmed_prefix",
        "complete_transcripts_and_maps_validated": True, "input_checkpoint_seal_validated": not cold,
        "output_checkpoint_seal_validated": True, "exact_insertion_delta_validated": True,
        "complete_delivery_join_and_unchanged_sources_validated": delivery_effect is None,
        "complete_delivery_join_and_native_source_effects_validated": True}
    result = {"semantic": semantic, "changed_files": sorted(paths), "before_input": copy.deepcopy(prefix_input),
        "after_identity": identity, "cache_before": _base._inventory(old_cache), "cache_after": _base._inventory(new_cache),
        "auxiliary_unchanged": {"native_join": auxiliary,
            "cache_names": list(auxiliary_names) if delivery_effect is None else [".send-prefix"]}}
    if delivery_effect is not None:
        result["combined_delivery_effect"] = delivery_effect
    return result


def project_cursor_prefix_tree(before, after, proof, begin, end, *, filesystem_clock=None):
    from tests.bus_scale_filesystem_clock import filesystem_bounds
    lower, upper = filesystem_bounds(begin, end, filesystem_clock)
    name = cache_name(proof["before_input"]["ack_relative"])
    old, new = before["entries"], after["entries"]
    for entries, inventory in ((old, proof["cache_before"]), (new, proof["cache_after"])):
        selected = {path: row for path, row in entries.items() if path == name or path.startswith(name + "/")}
        files = {path for path, row in selected.items() if row["kind"] == "file"}
        require(files == set(inventory), "ACK cache tree file population differs")
        for path in files:
            require(selected[path]["mode"] == 0o600 and selected[path]["bytes"] == inventory[path]["bytes"]
                and selected[path]["sha256"] == inventory[path]["sha256"], "ACK cache tree file differs from raw proof")
        directories = {path for path, row in selected.items() if row["kind"] == "directory"}
        require(directories == ({name, name + "/nodes", name + "/staging", name + "/buckets"} if inventory else set())
            and all(selected[path]["mode"] == 0o700 for path in directories), "ACK cache tree directory population differs")
    for name in proof["auxiliary_unchanged"]["cache_names"]:
        require({path: row for path, row in old.items() if path == name or path.startswith(name + "/")}
            == {path: row for path, row in new.items() if path == name or path.startswith(name + "/")}, "native ACK changed auxiliary cache tree metadata")
    name = cache_name(proof["before_input"]["ack_relative"])
    for path in set(old) | set(new):
        if path != name and not path.startswith(name + "/"):
            continue
        require(path in new, "native ACK cache tree removed an entry")
        if path in old and path not in proof["changed_files"] and new[path]["kind"] == "file":
            require(new[path] == old[path], "native ACK changed retained cache file metadata")
        if path not in old or new[path]["mtime_ns"] != old[path]["mtime_ns"]:
            stamp = new[path]["mtime_ns"]
            require(type(stamp) is int and lower <= stamp <= upper,
                "ACK cache mutation clock is outside native invocation")
    return {"kind": "complete_validated_native_cursor_prefix_cache", "effect": copy.deepcopy(proof["semantic"])}


def project_delivery_prefix_tree(before, after, proof, begin, end, *, filesystem_clock=None):
    adapted = copy.deepcopy(proof)
    adapted["before_input"]["ack_relative"] = adapted["before_input"]["delivery_relative"]
    result = project_cursor_prefix_tree(before, after, adapted, begin, end,
        filesystem_clock=filesystem_clock)
    result["kind"] = "complete_validated_native_delivery_prefix_cache"
    return result

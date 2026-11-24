"""Complete read-only proof of one exact consumed-ledger certification bank.

The output bank is evidence only after every source row, native current-epoch
join, membership bucket and direct entry has been checked independently. No
cache reader, constructor, warmer or repair participates in this proof.
"""
from __future__ import annotations

import importlib.util as _fixture_proof_util
from pathlib import Path as _FixtureProofPath

_fixture_proof_specification = _fixture_proof_util.spec_from_file_location(
    "_bus_scale_fixture_proof_bootstrap", _FixtureProofPath(__file__).with_name("_proof_namespace.py"))
_fixture_proof_bootstrap = _fixture_proof_util.module_from_spec(_fixture_proof_specification)
_fixture_proof_specification.loader.exec_module(_fixture_proof_bootstrap)
_fixture_proof_bootstrap.bind()


from functools import wraps
import hashlib
from pathlib import Path

from floati import jsonl, records, send_prefix
from floati.archive_consumption_replay import replay_history
from floati.consumption import WORK_KINDS
from floati.cursor import SparseCursor
from floati.epoch_carry import _decode
from floati.events import EVENT_KINDS
from floati.records import WAKE_HOLD_KINDS
from floati.root import FloatiRoot
from floati.wake_hold import WakeHoldController, project_wake_items
from _bus_scale_fixture_proofs import bus_scale_cursor_prefix_effects as _keys


_base = _keys._base
KINDS = frozenset({"consumption_receipt"})
FORMAT = "consumed-prefix-v2"
SUMMARY = "@cache/consumed-prefix"
TAG = {"operation": "native_cache_certification", "kind": "complete_native_consumed_prefix"}


def require(value, detail):
    if not value:
        raise ValueError("native consumed prefix effect: " + detail)


def _refusal(operation):
    @wraps(operation)
    def run(*args, **kwargs):
        try:
            return operation(*args, **kwargs)
        except (OSError, RuntimeError, KeyError, TypeError, send_prefix.CacheMiss) as error:
            raise ValueError("native consumed prefix effect: unavailable or malformed testimony") from error
    return run


def _coordinates(root, node, worker_session_id):
    cursor = SparseCursor(root)
    ack = cursor._relative_path_for(node, worker_session_id=worker_session_id)
    delivery = cursor._delivery_relative_path_for(node, worker_session_id=worker_session_id)
    consumed = Path("receipts/consumed").joinpath(*ack.relative_to("receipts/acks").parts)
    return consumed.as_posix(), ack.as_posix(), delivery.as_posix()


def cache_name(relative):
    path = Path(relative)
    require(not path.is_absolute() and ".." not in path.parts
        and len(path.parts) in (3, 4) and path.parts[:2] == ("receipts", "consumed")
        and path.suffix == ".jsonl" and path.as_posix() == str(relative),
        "consumed ledger coordinate is not exact and relative")
    return ".consumed-prefix-" + hashlib.sha256(path.as_posix().encode("utf-8")).hexdigest()


def _live(root, relative, expected):
    path = root.resolve_relative(relative)
    require(path == root.tenant_home / relative and not path.is_symlink(),
        "source coordinate is aliased")
    before = send_prefix._identity(path)
    raw = b"" if before is None else send_prefix._read(path, jsonl.MAX_LEDGER_BYTES)
    require(raw == expected and send_prefix._identity(path) == before,
        "live source differs from complete retained bytes or changed during read")
    return before


def _entries(rows):
    expected = {}
    for row in rows:
        expected[_base._key("id", row["id"])] = row
        expected[_base._key("message", row["message_id"])] = row
        expected.setdefault(_base._key("key", row["idempotency_key"]), row)
    return expected


def _dependencies(root, raw, node, worker_session_id, event_payload, *, work_identity=None, with_work=False):
    """Bind generations only after independently proving their native maps."""
    testimony = _keys.capture_cursor_prefix_input(root.tenant_home, raw,
        node=node, worker_session_id=worker_session_id,
        auxiliary_unavailable_temporal_certificate=True)
    auxiliary = testimony["auxiliary_proof"]
    ack_name = _keys.cache_name(testimony["ack_relative"])
    ack_payload = _base._object(raw[ack_name + "/checkpoint.json"])["payload"]
    return {"worker_session_id": worker_session_id,
        "events": {"generation": event_payload["generation"], "binding": event_payload["binding"]},
        "delivery": {"generation": auxiliary["delivery_generation"], "binding": auxiliary["delivery_binding"]},
        "ack": {"generation": ack_payload["generation"],
            "binding": _keys._ack_binding(root, testimony["ack_relative"], node, worker_session_id, auxiliary)},
        "work": {"path": "work/items.jsonl", "identity": work_identity} if with_work else None}


def _join(root, raw, node, worker_session_id, consumed, ack, delivery, rows):
    """Replay complete native current-source DATA; imported cuts stay refused."""
    source = []
    for relative, kinds, domain, events in (
        ("events.jsonl", EVENT_KINDS, WakeHoldController._EVENT_DOMAIN, True),
        (delivery, WAKE_HOLD_KINDS, WakeHoldController._DELIVERY_DOMAIN, False),
        (ack, {"ack_receipt"}, WakeHoldController._ACK_DOMAIN, False),
    ):
        decoded, prefixes, _physical = _decode(root, relative, raw.get(relative, b""),
            kinds, domain, events=events)
        source.extend((decoded, prefixes))
    # This is the original native pure projection, including physical order,
    # complete delivery/ACK membership and hold-prefix/decision validation.
    # There is no imported-hold admission: an unowned archival cut refuses.
    project_wake_items(events=source[0], event_prefix_digests=source[1],
        deliveries=source[2], delivery_prefix_digests=source[3],
        acknowledgments=source[4], acknowledgment_prefix_digests=source[5],
        recipient=node, worker_session_id=worker_session_id, tenant_id=root.tenant_id)
    work = _keys._transcript(root, "work/items.jsonl", frozenset(WORK_KINDS),
        raw.get("work/items.jsonl", b""))
    result = replay_history(root.tenant_id, node, worker_session_id,
        [tuple(source)], rows, work)
    require(len(result["joins"]) == len(rows)
        and all(join["source_index"] == 0 for join in result["joins"]),
        "consumed history requires unmatched archived provenance")
    return result, source


@_refusal
def capture_consumed_prefix_input(home, before, *, node="architect", worker_session_id=None):
    """Retain exact pre-child identities, source commitments and bank metadata."""
    root = FloatiRoot.open_direct_home(Path(home), create=False)
    consumed, ack, delivery = _coordinates(root, node, worker_session_id)
    name = cache_name(consumed)
    sources = (consumed, "events.jsonl", ack, delivery, "work/items.jsonl")
    identities = {relative: _live(root, relative, before.get(relative, b""))
                  for relative in sources}
    cache = _base._cache(before, name)
    metadata = _base._metadata(root.tenant_home, name)
    if cache:
        _base._check_metadata(metadata, cache, name, bucketed=True)
        for relative, data in cache.items():
            require((root.tenant_home / relative).read_bytes() == data,
                "pre-child bank differs from complete retained bytes")
    else:
        require(not metadata, "absent pre-child bank has unexplained metadata")
    checkpoint = cache.get(name + "/checkpoint.json")
    require({relative: _live(root, relative, before.get(relative, b"")) for relative in sources}
        == identities and _base._metadata(root.tenant_home, name) == metadata,
        "native source or bank metadata changed during input capture")
    return {"root": str(root.tenant_home), "node": node,
        "worker_session_id": worker_session_id, "relative": consumed,
        "identities": identities,
        "source_sha256": {relative: _base.sha(before.get(relative, b"")) for relative in sources},
        "checkpoint_sha256": None if checkpoint is None else _base.sha(checkpoint),
        "cache_metadata": metadata}


@_refusal
def verify_consumed_prefix_effect(home, before, after, delta, prefix_input,
        *, node="architect", worker_session_id=None):
    """Prove a full rebuild or native append under one exact consumed literal."""
    root = FloatiRoot.open_direct_home(Path(home), create=False)
    consumed, ack, delivery = _coordinates(root, node, worker_session_id)
    name = cache_name(consumed)
    require(type(prefix_input) is dict and set(prefix_input) == {
        "root", "node", "worker_session_id", "relative", "identities",
        "source_sha256", "checkpoint_sha256", "cache_metadata"},
        "pre-child testimony shape differs")
    require(prefix_input["root"] == str(root.tenant_home)
        and prefix_input["node"] == node and prefix_input["worker_session_id"] == worker_session_id
        and prefix_input["relative"] == consumed, "foreign root or consumed plane")
    require(type(prefix_input["cache_metadata"]) is dict,
        "pre-child bank metadata is not a complete object")
    sources = (consumed, "events.jsonl", ack, delivery, "work/items.jsonl")
    require(type(prefix_input["identities"]) is dict and set(prefix_input["identities"]) == set(sources)
        and type(prefix_input["source_sha256"]) is dict
        and prefix_input["source_sha256"] == {relative: _base.sha(before.get(relative, b""))
            for relative in sources}, "retained complete source commitments differ")
    identities = {relative: _live(root, relative, after.get(relative, b""))
        for relative in sources}
    # Consume may accompany work completion, whose exact native effect is
    # proved by the caller. That work append is never admitted as cache output;
    # joins use the retained pre-child work claim, as native consume did.
    for relative in ("events.jsonl", ack, delivery):
        require(before.get(relative, b"") == after.get(relative, b"")
            and identities[relative] == prefix_input["identities"][relative],
            "consumed certification changed its envelope/delivery/ACK source")
    old_raw, raw = before.get(consumed, b""), after.get(consumed, b"")
    require(raw.startswith(old_raw), "consumed append rewrote its native history")
    prior_identity, identity = prefix_input["identities"][consumed], identities[consumed]
    require((old_raw == raw and prior_identity == identity)
        or (old_raw != raw and identity is not None
            and (prior_identity is None or prior_identity[:4] == identity[:4])),
        "consumed append replaced its source identity")
    old_rows = _keys._transcript(root, consumed, KINDS, old_raw)
    rows = _keys._transcript(root, consumed, KINDS, raw)
    require(all(row["recipient"] == node and row["worker_session_id"] == worker_session_id
        for row in rows), "consumed row belongs to another recipient or worker plane")
    joined, source = _join(root, before, node, worker_session_id, consumed, ack, delivery, rows)
    retracted = {row["retracted_message_id"] for row in source[0]
        if row["kind"] == "message_retracted"}
    for row in rows[len(old_rows):]:
        require(row["message_id"] not in retracted
            and (worker_session_id is None or row["acting_session_id"] == worker_session_id),
            "fresh consumed candidate is retracted or crosses its worker session")
        if row["source"] == "work_complete":
            work = joined["work_items"].get(row["work_item_id"])
            require(work is not None and work["state"] == "claimed" and work["holder"] == node,
                "fresh consumption has no exact retained work claim")
    old_cache, cache = _base._cache(before, name), _base._cache(after, name)
    old_checkpoint = old_cache.get(name + "/checkpoint.json")
    require(prefix_input["checkpoint_sha256"] == (
        None if old_checkpoint is None else _base.sha(old_checkpoint)),
        "pre-child checkpoint bytes differ")
    if old_cache:
        _base._check_metadata(prefix_input["cache_metadata"], old_cache, name, bucketed=True)
    else:
        require(not prefix_input["cache_metadata"], "absent prior bank has unexplained metadata")
    if name + "/seal.key" in old_cache:
        require(cache.get(name + "/seal.key") == old_cache[name + "/seal.key"],
            "native certification changed its existing sealing key")
    metadata = _base._metadata(root.tenant_home, name)
    _base._check_metadata(metadata, cache, name, bucketed=True)
    for relative, data in cache.items():
        require((root.tenant_home / relative).read_bytes() == data,
            "post-child bank differs from complete retained bytes")
    expected = _entries(rows)
    event_payload, event_rows, _metadata = _keys._event_evidence(root, after, identities["events.jsonl"])
    by_id = {row["id"]: dict(row, ordinal=ordinal)
        for ordinal, row in enumerate(event_rows) if row["kind"] == "message_envelope"}
    members = None
    for row in rows:
        envelope = by_id.get(row["message_id"])
        if envelope is not None:
            members = _keys._mail_add(expected, members, envelope)
    binding = _keys._binding(root, consumed, KINDS, FORMAT)
    binding["revision"] = {"source": send_prefix._revision(), "reader_version": records.READER_VERSION}
    binding["folds"] = {"mail": 1}
    dependency = (_dependencies(root, after, node, worker_session_id, event_payload,
        with_work=any(row["source"] == "work_complete" for row in rows),
        work_identity=prefix_input["identities"]["work/items.jsonl"] if old_raw != raw
            else identities["work/items.jsonl"]) if rows else None)
    folds = {"mail": {"members": members, "event_generation": event_payload["generation"],
        "dependencies": dependency}}
    payload, _map = _keys._checkpoint(cache, name, raw, rows, identity, binding, expected, folds=folds)
    changed = sorted(relative for relative in set(old_cache) | set(cache)
        if old_cache.get(relative) != cache.get(relative))
    require(type(delta) is dict and all(type(relative) is str for relative in delta)
        and set(changed) == {relative for relative in delta
        if relative == name or relative.startswith(name + "/")},
        "raw cache delta population differs from complete before/after bytes")
    candidate = _base.raw_candidate_delta(before, after)
    require(all(delta[relative] == candidate[relative] for relative in changed),
        "raw cache delta does not bind complete bytes, operation and digest")
    require({relative: _live(root, relative, after.get(relative, b"")) for relative in sources}
        == identities and _base._metadata(root.tenant_home, name) == metadata,
        "native source or bank metadata changed during read-only proof")
    return {"kind": TAG["kind"], "consumed_records": len(rows),
        "changed_files": changed, "source_sha256": _base.sha(raw),
        "source_identity": identity, "head": payload["head"],
        "complete_map_entries": len(expected), "complete_native_join_validated": True}


def verify_complete_consumed_bank(home, raw, *, node, worker_session_id=None):
    """Prove one warmed bank from its complete retained native sources."""
    testimony = capture_consumed_prefix_input(home, raw, node=node,
        worker_session_id=worker_session_id)
    return verify_consumed_prefix_effect(home, raw, raw, {}, testimony, node=node,
        worker_session_id=worker_session_id)

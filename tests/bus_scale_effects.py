"""Read-only proof of a cold census status cache and its stdout artifact.

No CLI/cache-refresh entrypoint is called. Native snapshot loading validates
identity, checksum and source testimony; complete cold-source anchors and
native own-clock projections additionally bind the entire semantic payload.
Unknown artifact extensions survive when cache and stdout agree exactly.
"""
from __future__ import annotations

import copy
from datetime import datetime, timedelta
import hashlib
import json
import os
from pathlib import Path
import stat

from floati.command_scope import CommandScope
from floati.consumption import ConsumptionLedger
from floati.errors import FloatiError
from floati.fleet_presence import presence_status
from floati.jsonl import MAX_LEDGER_BYTES
from floati.projection import FleetProjection
from floati.root import FloatiRoot
from tests.bus_scale_durable import normalize_durable_delta as _normalize_durable_delta, normalize_timing_delta
from tests.bus_scale_wait_effects import normalize_wait_delta


def normalize_durable_delta(case, delta, artifact, **context):
    receipt_input = context.pop("receipt_prefix_input", None)
    prefix_input = context.pop("prefix_input", None)
    cut_input = context.pop("cut_input", None)
    before = context.pop("before_raw_files", None)
    proof = None
    send_proof = None
    cut_proof = None
    hint_proof = {}
    if case == "send":
        from tests.fixtures.bus_scale_census.bus_scale_wake_hint_effects import verify_wake_hint_effects
        from floati.events import EVENT_KINDS
        home = Path(context["home"])
        after = {path.relative_to(home).as_posix(): path.read_bytes()
                 for path in home.rglob("*") if path.is_file()}
        hint_proof = verify_wake_hint_effects(FloatiRoot.open_direct_home(home), before, after,
            delta, ledger_kinds={"events.jsonl": EVENT_KINDS})
        delta = {path: row for path, row in delta.items() if path not in hint_proof}
        from tests.fixtures.bus_scale_census.bus_scale_prefix_effects import CACHE, verify_prefix_effect, SUMMARY as SEND_SUMMARY
        changed = {path for path in delta if path == CACHE or path.startswith(CACHE + "/")}
        if changed or prefix_input is not None:
            if prefix_input is None or before is None:
                raise ValueError("send cache effects require complete pre-child proof")
            home = Path(context["home"])
            after = {path.relative_to(home).as_posix(): path.read_bytes()
                     for path in home.rglob("*") if path.is_file()}
            send_proof = verify_prefix_effect(home, before, after, delta, prefix_input)
            if changed != set(send_proof["changed_files"]):
                raise ValueError("send cache delta population differs from complete proof")
            delta = {path: row for path, row in delta.items() if path not in changed}
        from tests.fixtures.bus_scale_census.bus_scale_cut_prefix_effects import (
            CACHE as CUT_CACHE, verify_cut_prefix_effect, SUMMARY as CUT_SUMMARY)
        changed = {path for path in delta if path == CUT_CACHE or path.startswith(CUT_CACHE + "/")}
        native_changed = {path for path in set(before or {}) | set(after)
            if (path == CUT_CACHE or path.startswith(CUT_CACHE + "/"))
            and (before or {}).get(path) != after.get(path)}
        if changed or native_changed or cut_input is not None:
            if cut_input is None or before is None:
                raise ValueError("cut cache effects require complete pre-child proof")
            cut_proof = verify_cut_prefix_effect(home, before, after, delta, cut_input)
            if changed != set(cut_proof["changed_files"]):
                raise ValueError("cut cache delta population differs from complete proof")
            delta = {path: row for path, row in delta.items() if path not in cut_proof["changed_files"]}
    if case in {"receipt-append", "wait-start", "wait-idle"}:
        from tests.fixtures.bus_scale_census.bus_scale_receipt_prefix_effects import (
            cache_name, verify_receipt_prefix_effect, SUMMARY)
        name = cache_name()
        changed = {path for path in delta if path.startswith(name + "/")}
        if changed:
            if receipt_input is None or before is None:
                raise ValueError("receipt cache effects require complete pre-child proof")
            home = Path(context["home"])
            after = {path.relative_to(home).as_posix(): path.read_bytes()
                     for path in home.rglob("*") if path.is_file()}
            proof = verify_receipt_prefix_effect(home, before, after, delta, receipt_input)
            if changed != set(proof["changed_files"]):
                raise ValueError("receipt cache delta population differs from complete proof")
            delta = {path: row for path, row in delta.items() if path not in changed}
    decoder = normalize_wait_delta if case in {"wait-start", "wait-idle"} else _normalize_durable_delta
    if case in {"wait-start", "wait-idle"}:
        context["before_raw_files"] = before
    result = decoder(case, delta, artifact, **context)
    if proof is not None:
        result["delta"][SUMMARY] = proof["semantic"]
    if send_proof is not None:
        result["delta"][SEND_SUMMARY] = send_proof["semantic"]
    if cut_proof is not None:
        result["delta"][CUT_SUMMARY] = cut_proof["semantic"]
    result["delta"].update(hint_proof)
    return result


def _json(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False,
                      sort_keys=True, separators=(",", ":"))


def _equal(actual, expected, label):
    if _json(actual) != _json(expected):
        raise ValueError(label + " differs from native testimony")


def _native_fields(actual, expected, label):
    """Prove known native facts without deleting unrelated artifact extensions."""
    if type(expected) is dict:
        if type(actual) is not dict or not set(expected) <= set(actual):
            raise ValueError(label + " lacks native fields")
        for key, value in expected.items():
            _native_fields(actual[key], value, label + "/" + str(key))
    elif type(expected) is list:
        if type(actual) is not list or len(actual) != len(expected):
            raise ValueError(label + " has different native membership")
        for index, value in enumerate(expected):
            _native_fields(actual[index], value, label + "/" + str(index))
    else:
        _equal(actual, expected, label)


def _utc(value):
    if type(value) is not str:
        raise ValueError("status clock must be a native UTC string")
    moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if moment.tzinfo is None or moment.utcoffset() != timedelta(0):
        raise ValueError("status clock must be UTC")
    return moment


def _validate(home, stdout_artifact):
    home = Path(home)
    if (not home.is_absolute() or home.resolve(strict=True) != home
            or not stat.S_ISDIR(home.lstat().st_mode)
            or home.lstat().st_uid != os.getuid()):
        raise ValueError("status cache requires its canonical owned fixture")
    if (type(stdout_artifact) is not dict
            or type(stdout_artifact.get("artifact_version")) is not int
            or stdout_artifact["artifact_version"] != 0
            or stdout_artifact.get("command") != "status"
            or stdout_artifact.get("status") != "ok"
            or type(stdout_artifact.get("evidence")) is not dict):
        raise ValueError("status stdout is not a successful native artifact")
    _json(stdout_artifact)
    root = FloatiRoot.open_direct_home(home, create=False)
    projection = FleetProjection(root)
    store = projection._status_snapshot_store()
    metadata = store.path.lstat()
    if (not stat.S_ISREG(metadata.st_mode) or store.path.resolve(strict=True) != store.path
            or metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_size > MAX_LEDGER_BYTES):
        raise ValueError("status cache is not the native bounded private file")
    raw_cache = store.path.read_bytes()
    envelope = store._read_envelope()
    loaded = store.load()  # Read-only: never use status_artifact/load-or-refresh.
    _json(envelope)
    if any(loaded.tails.values()):
        raise ValueError("cold status cache leaves uncovered source tails")
    sources = store._source_bytes()
    anchors = []
    for source, data in sources:
        rows = store._validated_records(source, data)
        anchors.append(dict(
            path=source.relative.as_posix(), byte_offset=len(data),
            record_ordinal=len(rows), prefix_sha256=hashlib.sha256(data).hexdigest(),
            id_fingerprints="".join(sorted(
                hashlib.sha256(str(row["id"]).encode("utf-8")).hexdigest()[:16]
                for row in rows)),
        ))
    _equal(envelope["sources"], anchors, "complete cold-source anchors")
    payload = loaded.payload
    if type(payload) is not dict or set(payload) != {
            "artifact", "work_states", "built_at", "time_sensitive",
            "message_progress_observation_version"}:
        raise ValueError("status cache payload has nonnative fields")
    version = payload["message_progress_observation_version"]
    if type(version) is not int or version != FleetProjection.MESSAGE_PROGRESS_OBSERVATION_VERSION:
        raise ValueError("status cache message progress generation is not native")
    _equal(envelope["payload"], payload, "loaded cache payload")
    evidence, cached = stdout_artifact["evidence"], payload["artifact"]
    if (type(cached) is not dict or type(evidence.get("status_schema_version")) is not int
            or evidence["status_schema_version"] != 1
            or type(cached.get("status_schema_version")) is not int
            or cached["status_schema_version"] != 0
            or "scope" in cached or "installer_shadow" in cached
            or type(evidence.get("installer_shadow")) is not dict):
        raise ValueError("status cache/stdout schema-zero to schema-one relation is invalid")
    scope = CommandScope(root=str(root.path), tenant=root.tenant_id, root_source="explicit")
    _native_fields(evidence.get("scope"), scope.evidence(), "stdout scope")
    _equal(evidence.get("fleet_presence"), presence_status(root), "live fleet presence")
    nodes = evidence.get("nodes")
    if type(nodes) is not list or not nodes:
        raise ValueError("status clock has no native node testimony")
    moments = [_utc(node["wake_health"]["recent_sessions"]["window_end"]) for node in nodes]
    if any(moment != moments[0] for moment in moments):
        raise ValueError("status nodes disagree on their native observation clock")
    own = moments[0]
    _equal(evidence.get("observed_at"), projection._timestamp(own), "stdout observation")
    _equal(payload["built_at"], evidence["observed_at"], "cache build clock")
    if type(payload["time_sensitive"]) is not bool:
        raise ValueError("cache sensitivity must be a native boolean")
    _equal(payload["time_sensitive"], projection._status_time_sensitive(), "cache sensitivity")
    _equal(payload["work_states"], ConsumptionLedger(root).project(), "cache work states")
    snapshot, mode = projection._snapshot(own)  # Native cold read; no cache writes.
    expected = dict(status_schema_version=0, kind="fleet_status", root=str(root.path),
                    tenant_id=root.tenant_id, mode=mode, **snapshot)
    _native_fields(cached, expected, "cached own-clock status")
    returned = copy.deepcopy(evidence)
    del returned["scope"]
    del returned["installer_shadow"]
    del returned["fleet_presence"]
    returned["status_schema_version"] = 0
    _equal(cached, returned, "complete cached/stdout semantic artifact")
    # All reads above must refer to the same retained cache/source generation.
    if raw_cache != store.path.read_bytes():
        raise ValueError("status cache changed during validation")
    if store._capture_for(sources) != store.capture():
        raise ValueError("status cache sources changed during validation")
    return copy.deepcopy(payload)


def validate_status_cache(home, stdout_artifact):
    """Return the full validated cold-cache payload without changing any input.

Unavailable, malformed, partial or mismatched cache evidence raises ValueError.
This helper does not normalize or measure anything; callers retain raw cache
bytes and apply their separately reviewed cross-arm normalization contract.
"""
    try:
        return _validate(home, stdout_artifact)
    except (FloatiError, OSError, ValueError, TypeError, KeyError, UnicodeError,
            OverflowError) as exc:
        raise ValueError("native status cache validation failed: " + str(exc)) from exc

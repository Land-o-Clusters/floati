"""Pure post-collection waiter qualification and audited cache projection.

Callers retain raw detail files, source witnesses and native cache validation.
These functions perform no filesystem reads/writes, imports of repository code,
native calls, process launches or measurements. They change only deep copies.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import re


_FAMILIES = {"events", "registry", "wait-exhaustion", "deliveries"}
_SCALES = {"1x", "10x"}
_MODES = {"cpu", "diagnostic"}
_STARTUP_BOUNDARY = "process birth through first settled clean poll after completed authorized caught_up reconciliation"
_BYTES_BOUNDARY = "probe installation through endpoint; source/interpreter imports excluded"


def normalize_readiness(home, artifact):
    """Validate complete fixed-clock native testimony, then exact clone paths.

    The parent owns frozen private fixtures and checks their inventory after
    validation. The already-guarded native reader performs no lock creation.
    No clock, identity, reason, or unselected path is discarded.
    """
    from floati.errors import FloatiError
    from floati.mail_health import RecipientReadiness
    from floati.root import FloatiRoot
    from tests.bus_scale_durable import _directory
    try:
        home = _directory(home, "readiness fixture")
        root = FloatiRoot.open_direct_home(home, create=False)
        expected = RecipientReadiness._observe_already_guarded(root, "scale-worker",
            observed_at="2026-10-09T00:00:00.000Z").artifact()
        _equal(artifact, expected, "complete native readiness observation")
        result = {"artifact": copy.deepcopy(artifact), "normalizations": []}
        coordinates = {0: "registry/entries.jsonl", 2: "receipts/seat-board/scale-worker.jsonl"}
        for index, relative in coordinates.items():
            sources = result["artifact"]["mechanism_evidence"][index]["sources"]
            if len(sources) != 1 or sources[0]["path"] != str(home / relative):
                raise ValueError("readiness source differs from the closed native census coordinate")
            sources[0]["path"] = "<bus-scale-fixture>/" + relative
            result["normalizations"].append({"pointer": "/mechanism_evidence/" + str(index) + "/sources/0/path",
                "reason": "complete fixed-clock native observation and exact clone source validated"})
        _roundtrip(artifact, result["artifact"], result["normalizations"])
        return result
    except (FloatiError, OSError, KeyError, IndexError, TypeError, AttributeError) as exc:
        raise ValueError("native readiness equivalence could not be established") from exc


def _json(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False,
                      sort_keys=True, separators=(",", ":"))


def _equal(actual, expected, label):
    if _json(actual) != _json(expected):
        raise ValueError(label + " differs from retained testimony")


def _integer(value, label, *, positive=False):
    if type(value) is not int or value < (1 if positive else 0):
        raise ValueError(label + " must be a nonnegative exact integer")
    return value


def _seconds(value, label):
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise ValueError(label + " must be finite and nonnegative")
    return value


def _reads(value):
    if type(value) is not dict or any(type(path) is not str for path in value):
        raise ValueError("phase reads must retain an exact path/count mapping")
    return sum(_integer(count, "phase read bytes") for count in value.values())


def _phase(detail, mode):
    if (type(detail) is not dict or detail.get("mode") != mode
            or detail.get("case") != "wait-idle"
            or type(detail.get("native_exit_code")) is not int
            or detail["native_exit_code"] != 0):
        raise ValueError("independent phase is not the complete native idle entry")
    startup, idle = detail["startup"], detail["idle"]
    if type(startup) is not dict or type(idle) is not dict:
        raise ValueError("independent startup and idle details are required")
    if (startup.get("authorized_reconciliation") is not True
            or startup.get("boundary") != _STARTUP_BOUNDARY):
        raise ValueError("startup lacks completed authorized reconciliation")
    endpoint = _integer(startup["endpoint_ns"], "startup endpoint", positive=True)
    sampled = _integer(startup["endpoint_sample_after_ns"], "startup sample", positive=True)
    begin = _integer(idle["begin_ns"], "idle begin", positive=True)
    end = _integer(idle["end_ns"], "idle end", positive=True)
    if endpoint != begin or not endpoint <= sampled < end:
        raise ValueError("startup endpoint does not bind the complete idle phase")
    sleep = _seconds(idle["native_sleep_seconds"], "native sleep")
    if sleep != 1 or end - begin < 1_000_000_000:
        raise ValueError("independent idle phase lacks one full native polling interval")
    cpu_start = _integer(startup["self_cpu_ns"], "startup CPU")
    cpu_begin = _integer(idle["cpu_begin_ns"], "idle CPU begin")
    cpu_end = _integer(idle["cpu_end_ns"], "idle CPU end")
    if cpu_start != cpu_begin or cpu_end < cpu_begin:
        raise ValueError("phase CPU counters do not bind a monotonic startup/idle boundary")
    _seconds(startup["collected_child_cpu_seconds"], "startup collected-child CPU")
    startup_reads = _reads(startup["reads"])
    if ("reads_begin" in idle) != ("reads_end" in idle):
        raise ValueError("idle read-counter endpoints are incomplete")
    if "reads_begin" in idle:
        before, after = idle["reads_begin"], idle["reads_end"]
        _reads(before)
        _reads(after)
        if any(after.get(path, 0) < count for path, count in before.items()):
            raise ValueError("idle read counters moved backwards")
    if mode == "diagnostic":
        _equal(detail["coverage"], {"bytes": True, "locks": True}, "diagnostic phase coverage")
        if startup.get("fixture_bytes_boundary") != _BYTES_BOUNDARY:
            raise ValueError("startup byte coverage boundary is not the reviewed probe interval")
        _equal(_integer(startup["bytes_read"], "startup bytes"), startup_reads, "startup byte sum")
        intervals = startup["ledger_intervals"]
        if type(intervals) is not list or startup["crossing_ledger_intervals"] != []:
            raise ValueError("startup has unclosed or endpoint-crossing ledger holds")
        durations = []
        for interval in intervals:
            if type(interval) is not dict or not str(interval["path"]).endswith(".jsonl.lock"):
                raise ValueError("startup interval is not a native ledger hold")
            acquired = _integer(interval["acquire_call_after_ns"], "ledger acquisition", positive=True)
            released = _integer(interval["release_call_after_ns"], "ledger release", positive=True)
            if not acquired <= released <= endpoint:
                raise ValueError("startup ledger interval is outside the completed boundary")
            durations.append(_seconds(interval["seconds"], "ledger hold"))
        _equal(_seconds(startup["ledger_hold_seconds"], "startup longest hold"),
               max(durations, default=0), "startup longest hold summary")
        _integer(idle["bytes_read"], "idle bytes")
        _seconds(idle["ledger_hold_seconds"], "idle longest hold")
        if "reads_begin" in idle:
            _equal(idle["bytes_read"], _reads(idle["reads_end"]) - _reads(idle["reads_begin"]),
                   "idle read-counter delta")


def qualify_wait_start(primary_row, idle_row, details_by_scale):
    """Bind unobserved whole-command costs to separate same-seed phase proof.

    The returned witness is separate from the closed observation/arm schema.
    It qualifies the paired scenario, without claiming that the primary child
    itself had an observer or that the observed phase measures only evaluation.
    """
    try:
        _json((primary_row, idle_row, details_by_scale))
        for row in (primary_row, idle_row):
            if (type(row) is not dict or row.get("family") not in _FAMILIES
                    or row.get("selected_work_equal") is not True
                    or type(row.get("arms")) is not dict or set(row["arms"]) != _SCALES):
                raise ValueError("complete selected primary and independent idle observations are required")
        _equal(primary_row["family"], idle_row["family"], "independent phase family")
        if type(details_by_scale) is not dict or set(details_by_scale) != _SCALES:
            raise ValueError("phase proof must cover both exact scales")
        seeds = {}
        for scale in ("1x", "10x"):
            primary, idle = primary_row["arms"][scale], idle_row["arms"][scale]
            for arm, effect in ((primary, "natural_deadline_exhausted"), (idle, "unchanged_idle_tick")):
                if type(arm) is not dict or arm.get("child_collected") is not True:
                    raise ValueError("phase qualification requires all native children collected")
                _equal(arm["coverage"], {"cpu": True, "bytes": True, "locks": True}, "native metric coverage")
                _equal(arm["native"], {"exit_code": 0, "status": "ok",
                       "effect": {"kind": effect, "count": 1}}, "complete native waiter outcome")
                digest = arm["seed_sha256"]
                if type(digest) is not str or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
                    raise ValueError("seed binding must be SHA-256")
                _equal(arm["clone_sha256"], digest, "restored clone binding")
                _integer(arm["records"], "seed records", positive=True)
                _integer(arm["bytes"], "seed bytes", positive=True)
                if arm["bytes"] > 64 * 1024 * 1024:
                    raise ValueError("seed exceeds the native ledger bound")
            seeds[scale] = {field: primary[field] for field in (
                "seed_sha256", "clone_sha256", "records", "bytes")}
            for field, value in seeds[scale].items():
                _equal(idle[field], value, "independent " + scale + " " + field)
            details = details_by_scale[scale]
            if type(details) is not dict or set(details) != _MODES:
                raise ValueError("each independent seed requires both CPU and diagnostic phase children")
            for mode in ("cpu", "diagnostic"):
                _phase(details[mode], mode)
        return {"independent_phase_qualified": True, "family": primary_row["family"],
                "seeds": copy.deepcopy(seeds), "details": copy.deepcopy(details_by_scale)}
    except (KeyError, TypeError, AttributeError, OverflowError) as exc:
        raise ValueError("independent waiter phase proof has an unsupported or incomplete shape") from exc


def _pointer(value, coordinate):
    if type(coordinate) is not str or not coordinate.startswith("/"):
        raise ValueError("audit coordinate must be a JSON pointer")
    encoded = coordinate.split("/")[1:]
    parts = [part.replace("~1", "/").replace("~0", "~") for part in encoded]
    if [part.replace("~", "~0").replace("/", "~1") for part in parts] != encoded:
        raise ValueError("audit pointer uses a noncanonical escape")
    for part in parts[:-1]:
        if type(value) is list:
            if re.fullmatch(r"0|[1-9][0-9]*", part) is None:
                raise ValueError("audit list index is not canonical")
            value = value[int(part)]
        elif type(value) is dict:
            value = value[part]
        else:
            raise ValueError("audit pointer traverses a scalar")
    part = parts[-1]
    if type(value) is list:
        if re.fullmatch(r"0|[1-9][0-9]*", part) is None:
            raise ValueError("audit list index is not canonical")
        key = int(part)
    elif type(value) is dict:
        key = part
    else:
        raise ValueError("audit pointer has no parent container")
    leaf = value[key]
    if type(leaf) in (dict, list):
        raise ValueError("audit must name an actually changed scalar leaf")
    return value, key, leaf


def _roundtrip(original, normalized, audits):
    if type(audits) is not list:
        raise ValueError("normalization audit must be a list")
    restored, seen = copy.deepcopy(normalized), set()
    for row in audits:
        if (type(row) is not dict or set(row) != {"pointer", "reason"}
                or type(row["reason"]) is not str or not row["reason"].strip()
                or type(row["pointer"]) is not str or row["pointer"] in seen):
            raise ValueError("audit must contain unique reasoned coordinates")
        seen.add(row["pointer"])
        _, _, old = _pointer(original, row["pointer"])
        target, key, new = _pointer(restored, row["pointer"])
        if _json(old) == _json(new):
            raise ValueError("audit coordinate did not change")
        target[key] = copy.deepcopy(old)
    _equal(restored, original, "complete audit roundtrip")


def _checksum(cache):
    return hashlib.sha256(_json({key: value for key, value in cache.items()
                                if key != "checksum"}).encode("utf-8")).hexdigest()


def _cached_evidence(artifact):
    if (type(artifact) is not dict or type(artifact.get("artifact_version")) is not int
            or artifact["artifact_version"] != 0 or artifact.get("command") != "status"
            or artifact.get("status") != "ok" or type(artifact.get("evidence")) is not dict):
        raise ValueError("cache projection requires native status stdout")
    evidence = copy.deepcopy(artifact["evidence"])
    if (type(evidence.get("status_schema_version")) is not int or evidence["status_schema_version"] != 1
            or type(evidence.get("scope")) is not dict or type(evidence.get("installer_shadow")) is not dict):
        raise ValueError("status stdout lacks its schema-one-only fields")
    del evidence["scope"]
    del evidence["installer_shadow"]
    evidence.pop("fleet_presence", None)
    evidence["status_schema_version"] = 0
    return evidence


def normalize_status_cache(cache, stdout_artifact, normalized_artifact):
    """Transpose a complete audited stdout transform onto its proved cache.

    Native validate_status_cache must already have proved the actual cache and
    all cold-source anchors. This pure seam additionally checks the original
    semantic relation and checksum; a normal form cannot overwrite a mismatch.
    """
    try:
        _json((cache, stdout_artifact, normalized_artifact))
        if (type(cache) is not dict or set(cache) != {"snapshot_version", "reader", "key", "root",
                "tenant_id", "sources", "payload", "checksum"}
                or type(cache["snapshot_version"]) is not int or cache["snapshot_version"] != 0
                or cache["reader"] != "status" or cache["key"] != "default"
                or type(cache["sources"]) is not list or cache["checksum"] != _checksum(cache)):
            raise ValueError("original cache envelope identity or checksum is invalid")
        payload = cache["payload"]
        if type(payload) is not dict or set(payload) != {"artifact", "work_states", "built_at", "time_sensitive",
                                                        "message_progress_observation_version"}:
            raise ValueError("original cache payload is not the complete native shape")
        version = payload["message_progress_observation_version"]
        if type(version) is not int or version != 1:
            raise ValueError("original cache message progress generation is not native")
        original_evidence = _cached_evidence(stdout_artifact)
        _equal(payload["artifact"], original_evidence, "original cache/stdout relation")
        _equal(cache["root"], original_evidence["root"], "cache root")
        _equal(cache["tenant_id"], original_evidence["tenant_id"], "cache tenant")
        _equal(payload["built_at"], original_evidence["observed_at"], "cache build clock")
        if type(payload["time_sensitive"]) is not bool:
            raise ValueError("cache sensitivity must be a native boolean")
        if type(normalized_artifact) is not dict or set(normalized_artifact) != {"artifact", "normalizations"}:
            raise ValueError("normal form must retain its complete artifact and audit")
        normalized, audits = normalized_artifact["artifact"], normalized_artifact["normalizations"]
        _roundtrip(stdout_artifact, normalized, audits)
        projected = _cached_evidence(normalized)
        # Preserve the live reader-only fields across the normalization.
        _equal(normalized["evidence"]["installer_shadow"], stdout_artifact["evidence"]["installer_shadow"],
               "retained installer observation")
        _equal(normalized["evidence"].get("fleet_presence"), stdout_artifact["evidence"].get("fleet_presence"),
               "retained fleet presence")
        result, cache_audit = copy.deepcopy(cache), []
        result["payload"]["artifact"] = projected
        for change in audits:
            pointer = change["pointer"]
            if pointer == "/evidence/scope/root":
                continue
            if not pointer.startswith("/evidence/") or pointer.startswith("/evidence/scope/"):
                raise ValueError("unsupported stdout-only normalization")
            cache_audit.append({"pointer": "/payload/artifact/" + pointer[len("/evidence/"):],
                                "reason": change["reason"]})
        for parent, key, value, pointer, reason in (
                (result["payload"], "built_at", projected["observed_at"], "/payload/built_at",
                 "validated cache build clock follows the audited artifact clock"),
                (result, "root", projected["root"], "/root",
                 "validated cache envelope follows the audited artifact coordinate")):
            if _json(parent[key]) != _json(value):
                parent[key] = copy.deepcopy(value)
                cache_audit.append({"pointer": pointer, "reason": reason})
        checksum = _checksum(result)
        if checksum != result["checksum"]:
            result["checksum"] = checksum
            cache_audit.append({"pointer": "/checksum", "reason": "native canonical checksum of the complete audited cache projection"})
        _roundtrip(cache, result, cache_audit)
        return {"cache": result, "normalizations": cache_audit}
    except (KeyError, IndexError, TypeError, AttributeError, OverflowError, UnicodeError) as exc:
        raise ValueError("cache normalization has an unsupported or incomplete semantic/audit shape") from exc

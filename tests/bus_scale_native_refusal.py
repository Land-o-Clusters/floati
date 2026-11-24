"""Closed census testimony for the architect-ruled d66 teardown defect.

This admission is not successful work. Raw/source binding is separate from
matrix validation and does not by itself authenticate native execution.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import stat

PRODUCT = "d66a94daa357bd392b3040c55cbe21e20ac884a3"
ROW = "F2-SEAT-TEARDOWN-SCALE-1"


def is_functional_refusal(value):
    return type(value) is dict and value.get("disposition") == "functional_red"


def _artifact(value):
    from tests.bus_scale_workloads import _fields
    value = _fields(value, {"artifact_version", "command", "schema_version", "status", "evidence"},
                    "native refused artifact")
    if (type(value["artifact_version"]) is not int or value["artifact_version"] != 0
            or type(value["schema_version"]) is not int or value["schema_version"] != 1
            or value["command"] != "seat" or value["status"] != "refused"):
        raise ValueError("functional RED requires the native seat refusal artifact")
    evidence = _fields(value["evidence"], {"code", "detail", "remedy"}, "native refusal evidence")
    detail = evidence["detail"]
    match = re.fullmatch(r"record is ([0-9]+) bytes; maximum is 65536", detail) if type(detail) is str else None
    if evidence["code"] != "record_too_large" or match is None or int(match[1]) <= 65536:
        raise ValueError("native refusal is outside the ruled frame-size defect")
    remedy = _fields(evidence["remedy"], {"kind", "why"}, "native refusal remedy")
    if remedy != {"kind": "none", "why": "no action was named for this refusal"}:
        raise ValueError("native refusal remedy changed")
    return value


def validate_functional_refusal(case, family, scale, result):
    from tests.bus_scale_workloads import _fields, _digest, _integer, _number, _MAX_BYTES
    if (case, family, scale) != ("seat-teardown", "events", "10x"):
        raise ValueError("functional RED is restricted to the ruled EVENTS/10k teardown cell")
    result = _fields(result, {"disposition", "row", "native", "records", "bytes", "seed_sha256",
        "clone_sha256", "child_collected", "raw_sha256", "available_metrics", "source"}, "functional RED")
    if result["disposition"] != "functional_red" or result["row"] != ROW:
        raise ValueError("functional RED must name the exact ruled defect")
    _integer(result["records"], minimum=10000, maximum=10000, label="refused fixture records")
    _integer(result["bytes"], minimum=1, maximum=_MAX_BYTES - 1, label="refused fixture bytes")
    for field in ("seed_sha256", "clone_sha256", "raw_sha256"):
        _digest(result[field], "functional RED " + field)
    if result["seed_sha256"] != result["clone_sha256"] or result["child_collected"] is not True:
        raise ValueError("functional RED requires restored seed and collected native children")
    native = _fields(result["native"], {"exit_code", "artifact", "diagnostic_artifact"}, "refused native modes")
    if type(native["exit_code"]) is not int or native["exit_code"] != 20:
        raise ValueError("functional RED requires native refusal exit 20")
    _artifact(native["artifact"])
    _artifact(native["diagnostic_artifact"])
    metrics = _fields(result["available_metrics"], {"cpu_seconds", "bytes_read", "max_ledger_lock_seconds"},
                      "refused available costs")
    _number(metrics["cpu_seconds"], positive=True, label="refused CPU")
    _integer(metrics["bytes_read"], minimum=0, maximum=None, label="refused bytes read")
    _number(metrics["max_ledger_lock_seconds"], positive=False, label="refused lock hold")
    source = _fields(result["source"], {"product_sha", "entry_sha256", "stdout_sha256"}, "refused source")
    if source["product_sha"] != PRODUCT:
        raise ValueError("functional RED is ruled only on the pinned d66 product")
    _digest(source["entry_sha256"], "refused entry source")
    stdout = _fields(source["stdout_sha256"], {"cpu", "diagnostic"}, "refused raw stdout")
    for value in stdout.values():
        _digest(value, "refused raw stdout")
    return result


def bind_functional_refusal(case, family, scale, result, place, entry):
    """Bind retained literal artifacts and entry bytes before matrix admission."""
    validate_functional_refusal(case, family, scale, result)
    if hashlib.sha256(Path(entry).read_bytes()).hexdigest() != result["source"]["entry_sha256"]:
        raise ValueError("functional RED entry source witness changed")
    for mode, field in (("cpu", "artifact"), ("diagnostic", "diagnostic_artifact")):
        path = Path(place) / mode / "stdout.txt"
        metadata = path.lstat()
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1
                or path.resolve(strict=True) != path or metadata.st_uid != os.getuid()):
            raise ValueError("functional RED stdout must be an owned ordinary raw file")
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != result["source"]["stdout_sha256"][mode]:
            raise ValueError("functional RED stdout digest changed")
        artifact = _artifact(json.loads(raw))
        if json.dumps(artifact, sort_keys=True) != json.dumps(result["native"][field], sort_keys=True):
            raise ValueError("functional RED literal native artifact changed")
    return result

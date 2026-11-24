"""Pure categories for witnessed census artifacts; hashes are not authentication.

The observer calls the extractor after complete native witnessing, retains raw
timestamps/stdout and binds its returned maps to those bytes in the collection.
This module does no I/O, clock observation, ledger evaluation or measurement.
"""
from __future__ import annotations

import copy
from datetime import datetime
import math
import re

from floati.errors import ProtocolRefusal
from floati.root import validate_identifier


ATTENTION_CASES = frozenset({"supervise", "watch"})
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_SEAT_FIELDS = {"state", "reason", "remedy", "hold_evidence_state", "threshold_minutes",
                "threshold_basis", "detail", "nullness"}
_SEAT_NULLS = {"oldest_waiting_at", "oldest_waiting_age_seconds", "next_check_at", "last_hold_started_at"}
_WAKE_FIELDS = {"state", "breaker_state", "pause_state", "waiter_exit_reason",
                "documented_entrypoint_resolves", "remedy", "read_finding", "nullness"}
_WAKE_NULLS = {"claim_session", "last_seen_session", "oldest_unread", "waiter_receipt_age_minutes"}


class AttentionCategoryMismatch(ValueError):
    code = "bus_scale_attention_category_mismatch"


def _require(case, condition, detail):
    if not condition:
        raise ValueError(case + " attention category malformed: " + detail)


def _fields(case, value, expected, detail):
    _require(case, type(value) is dict and set(value) == expected, detail)


def _node(case, value):
    try:
        return validate_identifier(value, "node")
    except ProtocolRefusal as error:
        raise ValueError(case + " attention category malformed: invalid node identity") from error


def _text(case, value, detail, *, nullable=False):
    _require(case, type(value) is str or nullable and value is None, detail)


def _nullness(case, value, fields):
    _fields(case, value, fields, "inexact nullness fields")
    _require(case, all(type(item) is bool for item in value.values()), "nullness must be strict booleans")


def _validate_categories(case, nodes):
    _require(case, case in ATTENTION_CASES, "unreviewed case")
    _require(case, type(nodes) is dict and bool(nodes), "node map must be nonempty")
    for node, row in nodes.items():
        _node(case, node)
        _fields(case, row, {"seat_attention", "wake_health"}, "inexact node category fields")
        seat = row["seat_attention"]
        _fields(case, seat, _SEAT_FIELDS, "inexact seat category fields")
        _require(case, type(seat["state"]) is str and seat["state"] in
                 {"not_applicable", "not_overdue", "unknown"}, "invalid attention state")
        _require(case, type(seat["reason"]) is str and seat["reason"] in {
            "harness_has_no_stop_hook", "no_waiting_mail", "fresh_waiting_mail", "hold_started_since_waiting_mail",
            "overdue_without_hold_start", "hold_evidence_unreadable"}, "invalid attention reason")
        _require(case, type(seat["hold_evidence_state"]) is str and seat["hold_evidence_state"] in
                 {"not_read", "readable", "unknown"}, "invalid hold evidence category")
        _require(case, type(seat["threshold_minutes"]) is int and seat["threshold_minutes"] > 0,
                 "invalid attention threshold")
        for field in ("remedy", "threshold_basis", "detail"):
            _text(case, seat[field], "invalid attention prose")
        _nullness(case, seat["nullness"], _SEAT_NULLS)
        wake = row["wake_health"]
        if case == "supervise":
            _require(case, wake is None, "supervise has no nested wake-health category")
            continue
        _fields(case, wake, _WAKE_FIELDS, "inexact nested wake-health fields")
        _require(case, type(wake["state"]) is str and wake["state"] in {
            "unknown", "paused", "breaker_open", "stale_claim_with_unread_mail", "unread_mail", "healthy"},
            "invalid nested wake-health state")
        _require(case, type(wake["breaker_state"]) is str and wake["breaker_state"] in {"open", "closed"},
                 "invalid breaker state")
        _require(case, type(wake["pause_state"]) is str and wake["pause_state"] in {"unknown", "paused", "active"},
                 "invalid pause state")
        _text(case, wake["waiter_exit_reason"], "invalid waiter exit reason", nullable=True)
        _text(case, wake["remedy"], "invalid nested wake-health prose")
        _require(case, type(wake["documented_entrypoint_resolves"]) is bool, "invalid entrypoint observation")
        _nullness(case, wake["nullness"], _WAKE_NULLS)
        finding = wake["read_finding"]
        if finding is not None:
            _fields(case, finding, {"code", "ledger", "record_id", "source_code"}, "inexact read finding")
            for field in ("code", "ledger", "source_code"):
                _text(case, finding[field], "invalid read finding field")
            _text(case, finding["record_id"], "invalid read finding record identity", nullable=True)


def validate_attention_proof(case, proof):
    """Validate closed testimony syntax; never authenticate the supplied hashes."""
    _fields(case, proof, {"schema_version", "case", "nodes", "sources"}, "inexact proof fields")
    _require(case, type(proof["schema_version"]) is int and proof["schema_version"] == 1,
             "invalid proof version")
    _require(case, type(proof["case"]) is str and proof["case"] == case, "proof belongs to another case")
    _validate_categories(case, proof["nodes"])
    _fields(case, proof["sources"], {"cpu", "diagnostic"}, "inexact source modes")
    _require(case, all(type(value) is str and _SHA256.fullmatch(value)
                       for value in proof["sources"].values()), "invalid source artifact hash")
    return proof


def require_attention_pair(case, one, ten):
    """Require inspectable categories at a real pair consumer, preserving hashes."""
    if case not in ATTENTION_CASES:
        return
    for arm in (one, ten):
        if "attention_proof" not in arm:
            raise ValueError(case + " attention category missing")
        validate_attention_proof(case, arm["attention_proof"])
    if one["attention_proof"]["nodes"] != ten["attention_proof"]["nodes"]:
        raise AttentionCategoryMismatch(case + " attention category mismatch")


def _instant(case, value):
    if value is None:
        return
    _require(case, type(value) is str, "invalid native timestamp type")
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(case + " attention category malformed: invalid native timestamp") from error
    _require(case, stamp.tzinfo is not None and stamp.utcoffset() is not None, "naive native timestamp")


def _detail(case, native):
    text, age = native["detail"], native["oldest_waiting_age_seconds"]
    _text(case, text, "invalid native attention prose")
    if not text:
        return text
    _require(case, type(age) in (int, float) and math.isfinite(age) and age >= 0,
             "native prose lacks a valid waiting age")
    count = native["undelivered_count"]
    _require(case, type(count) is int and count >= 0, "invalid native waiting count")
    prefix = native["node_id"] + ": " + str(count) + " undelivered, oldest waiting "
    token = str(int(age // 60)) + "m"
    _require(case, text.startswith(prefix + token + "; last hold start "),
             "native prose does not bind its exact waiting-duration token")
    return prefix + "<waiting-duration>" + text[len(prefix + token):]


def attention_categories(case, artifact):
    """Extract every native node after the caller's complete native witness."""
    _require(case, case in ATTENTION_CASES, "unreviewed extraction case")
    fields = {"artifact_version", "command", "status", "evidence"}
    if case == "watch":
        fields.add("schema_version")
    _fields(case, artifact, fields, "inexact native artifact")
    if case == "watch":
        _require(case, type(artifact["schema_version"]) is int and artifact["schema_version"] == 1,
                 "wrong native watch schema")
    _require(case, type(artifact["artifact_version"]) is int and artifact["artifact_version"] == 0
             and artifact["command"] == case and artifact["status"] == "ok", "wrong native artifact")
    evidence = artifact["evidence"]
    _require(case, type(evidence) is dict, "invalid native evidence")
    if case == "watch":
        _require(case, type(evidence.get("delta")) is dict and evidence["delta"].get("kind") == "initial"
                 and type(evidence["delta"].get("snapshot")) is dict, "invalid native watch snapshot")
        evidence = evidence["delta"]["snapshot"]
    native_nodes = evidence.get("nodes")
    _require(case, type(native_nodes) is list and bool(native_nodes), "missing native node population")
    result = {}
    for row in native_nodes:
        _require(case, type(row) is dict, "invalid native node")
        node = _node(case, row.get("node_id"))
        _require(case, node not in result, "duplicate native node")
        native = row.get("seat_attention")
        _require(case, type(native) is dict and native.get("node_id") == node, "native attention belongs to another node")
        for field in ("observed_at", "oldest_waiting_at", "next_check_at", "last_hold_started_at"):
            _instant(case, native[field])
        age = native["oldest_waiting_age_seconds"]
        _require(case, age is None or type(age) in (int, float) and math.isfinite(age) and age >= 0,
                 "invalid native waiting age")
        category = {key: copy.deepcopy(native[key]) for key in _SEAT_FIELDS - {"nullness", "detail"}}
        category.update(detail=_detail(case, native), nullness={field: native[field] is None for field in _SEAT_NULLS})
        wake_category = None
        if case == "watch":
            wake = row.get("wake_health")
            _require(case, type(wake) is dict and wake.get("node_id") == node, "native wake health belongs to another node")
            wake_category = {key: copy.deepcopy(wake[key]) for key in _WAKE_FIELDS - {"nullness", "read_finding"}}
            wake_category.update(read_finding=copy.deepcopy(wake.get("read_finding")),
                                 nullness={field: wake[field] is None for field in _WAKE_NULLS})
        result[node] = {"seat_attention": category, "wake_health": wake_category}
    _validate_categories(case, result)
    return result

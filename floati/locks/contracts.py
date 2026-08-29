"""Closed record and content-witness contracts for the private Locks ledger."""

from __future__ import annotations

import re
import unicodedata
from copy import deepcopy
from datetime import datetime, timezone
from typing import Mapping

from ..errors import IntegrityFailure, ProtocolRefusal
from ..root import validate_identifier


LOCK_KINDS = frozenset({
    "lock_acquired",
    "lock_escalated",
    "lock_announcement_stopped",
    "lock_announcement_rearmed",
    "lock_announcement_delivered",
    "car_submitted",
    "car_measurement_recorded",
    "car_reviewed",
    "car_landed",
    "car_dissolved",
    "seat_provisioned",
    "review_handoff_queued",
    "review_handoff_stopped",
    "review_handoff_rearmed",
    "review_handoff_delivered",
})
FULL_REF_PATTERN = re.compile(r"refs/(?:heads|remotes)/[^\x00-\x20~^:?*\\[\\]+\Z")
HEX40_PATTERN = re.compile(r"[0-9a-f]{40}\Z")
HEX64_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
COMMON_FIELDS = frozenset({"schema_version", "id", "tenant_id", "timestamp", "kind"})
KIND_FIELDS = {
    "lock_acquired": frozenset({"lock_id", "holder", "expires_at", "escalation_holder"}),
    "lock_escalated": frozenset({
        "lock_id", "prior_holder", "prior_expires_at", "requested_by", "holder",
        "expires_at", "escalation_holder", "announcement_id",
        "announcement_recipient", "announcement_status",
        "announcement_rearm_event", "copy_key",
    }),
    "lock_announcement_stopped": frozenset({
        "lock_id", "announcement_id", "attempts", "stopped_at", "rearm_event",
    }),
    "lock_announcement_rearmed": frozenset({
        "lock_id", "announcement_id", "observed_event",
    }),
    "lock_announcement_delivered": frozenset({
        "lock_id", "announcement_id", "receipt_id", "receipt_digest",
    }),
    "car_submitted": frozenset({"car_id", "ref", "ref_oid", "witness"}),
    "car_measurement_recorded": frozenset({
        "car_id", "measured_ref", "measured_tree", "test_count",
        "failure_count", "evidence_digest",
    }),
    "car_reviewed": frozenset({
        "car_id", "verdict", "rank", "base_ref", "base_oid", "base_tree",
        "witness_holds",
    }),
    "car_landed": frozenset({
        "car_id", "target_ref", "target_oid", "target_tree", "method",
        "witness_holds",
    }),
    "car_dissolved": frozenset({
        "car_id", "product_ref", "product_oid", "product_tree", "witness_holds",
    }),
    "seat_provisioned": frozenset({"seat_id", "hook_names", "manifest_digest"}),
    "review_handoff_queued": frozenset({
        "handoff_id", "recipient", "car_id", "ref", "base_ref", "witness",
        "rearm_event", "status", "copy_key",
    }),
    "review_handoff_stopped": frozenset({
        "handoff_id", "recipient", "car_id", "ref", "base_ref", "witness",
        "rearm_event", "status", "copy_key", "attempts", "stopped_at",
    }),
    "review_handoff_rearmed": frozenset({
        "handoff_id", "recipient", "car_id", "ref", "base_ref", "witness",
        "rearm_event", "status", "copy_key", "observed_event",
    }),
    "review_handoff_delivered": frozenset({
        "handoff_id", "recipient", "car_id", "ref", "base_ref", "witness",
        "rearm_event", "status", "receipt_id", "receipt_digest",
    }),
}
KIND_ID_PREFIX = {
    "lock_acquired": "lock-acquired-",
    "lock_escalated": "lock-escalated-",
    "lock_announcement_stopped": "lock-announcement-stopped-",
    "lock_announcement_rearmed": "lock-announcement-rearmed-",
    "lock_announcement_delivered": "lock-announcement-delivered-",
    "car_submitted": "car-submitted-",
    "car_measurement_recorded": "car-measurement-",
    "car_reviewed": "car-reviewed-",
    "car_landed": "car-landed-",
    "car_dissolved": "car-dissolved-",
    "seat_provisioned": "seat-provisioned-",
    "review_handoff_queued": "review-handoff-queued-",
    "review_handoff_stopped": "review-handoff-stopped-",
    "review_handoff_rearmed": "review-handoff-rearmed-",
    "review_handoff_delivered": "review-handoff-delivered-",
}
_BIDI_CONTROLS = frozenset({"LRE", "RLE", "LRO", "RLO", "PDF", "LRI", "RLI", "FSI", "PDI", "BN"})


def _raise(integrity: bool, code: str, detail: str) -> None:
    error = IntegrityFailure if integrity else ProtocolRefusal
    raise error(code, detail)


def _safe_text(value: object, field: str, *, integrity: bool, maximum: int = 1_024) -> str:
    if type(value) is not str:
        _raise(integrity, f"{field}_invalid", f"{field} must be an exact string")
    try:
        encoded = value.encode("utf-8", "strict")
    except UnicodeEncodeError:
        _raise(integrity, f"{field}_invalid", f"{field} must be strict UTF-8")
    if not encoded or len(encoded) > maximum:
        _raise(integrity, f"{field}_invalid", f"{field} is empty or exceeds {maximum} bytes")
    if any(
        unicodedata.category(character) in {"Cc", "Cs"}
        or unicodedata.bidirectional(character) in _BIDI_CONTROLS
        for character in value
    ):
        _raise(integrity, f"{field}_invalid", f"{field} contains a terminal-unsafe character")
    return value


def _identifier(value: object, field: str, *, integrity: bool) -> str:
    try:
        return validate_identifier(value if type(value) is str else None, field)
    except ProtocolRefusal as exc:
        _raise(integrity, exc.code, exc.detail)
    raise AssertionError("unreachable")


def validate_timestamp(value: object, field: str, *, integrity: bool) -> str:
    text = _safe_text(value, field, integrity=integrity, maximum=32)
    if not text.endswith("Z"):
        _raise(integrity, f"{field}_invalid", f"{field} must be aware UTC testimony ending in Z")
    try:
        parsed = datetime.fromisoformat(text[:-1] + "+00:00")
    except ValueError:
        _raise(integrity, f"{field}_invalid", f"{field} is not an RFC 3339 timestamp")
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        _raise(integrity, f"{field}_invalid", f"{field} must be aware UTC testimony")
    return text


def timestamp_value(value: str) -> datetime:
    return datetime.fromisoformat(value[:-1] + "+00:00")


def validate_full_ref(value: object, field: str, *, integrity: bool, code: str = "car_ref_required") -> str:
    if type(value) is not str or not value:
        _raise(integrity, code, f"{field} must be one full refs/heads or refs/remotes name")
    text = _safe_text(value, field, integrity=integrity)
    parts = text.split("/")
    if (
        FULL_REF_PATTERN.fullmatch(text) is None
        or text.endswith(("/", "."))
        or "//" in text
        or ".." in text
        or "@{" in text
        or any(part.startswith(".") or part.endswith(".lock") for part in parts)
    ):
        _raise(integrity, code, f"{field} must be one full refs/heads or refs/remotes name")
    return text


def _hex(value: object, field: str, pattern: re.Pattern[str], *, integrity: bool) -> str:
    text = _safe_text(value, field, integrity=integrity, maximum=64)
    if pattern.fullmatch(text) is None:
        _raise(integrity, f"{field}_invalid", f"{field} has the wrong hexadecimal shape")
    return text


def validate_witness(value: object, *, integrity: bool) -> dict[str, object]:
    if type(value) is not dict:
        _raise(integrity, "content_witness_invalid", "content witness must be one exact mapping")
    witness = deepcopy(value)
    kind = _safe_text(witness.get("kind"), "witness_kind", integrity=integrity, maximum=64)
    if kind not in {"path_present", "path_absent", "blob_sha256", "file_contains_utf8"}:
        _raise(integrity, "content_witness_invalid", "content witness kind is unknown")
    expected = {"kind", "path"}
    if kind == "blob_sha256":
        expected.add("sha256")
    elif kind == "file_contains_utf8":
        expected.add("needle")
    if set(witness) != expected:
        _raise(integrity, "content_witness_invalid", "content witness fields do not match its kind")
    path = _safe_text(witness.get("path"), "witness_path", integrity=integrity, maximum=4_096)
    parts = path.split("/")
    if path.startswith("/") or path.startswith("-") or any(part in {"", ".", ".."} for part in parts):
        _raise(integrity, "content_witness_invalid", "content witness path is not repository-relative")
    normalized: dict[str, object] = {"kind": kind, "path": path}
    if kind == "blob_sha256":
        normalized["sha256"] = _hex(witness.get("sha256"), "witness_sha256", HEX64_PATTERN, integrity=integrity)
    elif kind == "file_contains_utf8":
        normalized["needle"] = _safe_text(witness.get("needle"), "witness_needle", integrity=integrity, maximum=4_096)
    return normalized


def validate_lock_record(record: object, tenant_id: str, *, integrity: bool) -> dict[str, object]:
    if type(record) is not dict:
        _raise(integrity, "lock_record_invalid", "lock record must be one exact mapping")
    row = deepcopy(record)
    if row.get("schema_version") != 0 or type(row.get("schema_version")) is not int:
        _raise(integrity, "schema_version_invalid", "Locks schema_version must be exact integer 0")
    if row.get("tenant_id") != tenant_id:
        _raise(integrity, "tenant_mismatch", "Locks record tenant does not match its root")
    kind = _safe_text(row.get("kind"), "kind", integrity=integrity, maximum=64)
    if kind not in LOCK_KINDS:
        _raise(integrity, "lock_kind_invalid", "Locks record kind is unknown")
    expected = COMMON_FIELDS | KIND_FIELDS[kind]
    if set(row) != expected:
        _raise(integrity, "lock_record_fields_invalid", "Locks record fields do not match its kind")
    record_id = _safe_text(row.get("id"), "id", integrity=integrity, maximum=96)
    prefix = KIND_ID_PREFIX[kind]
    if not record_id.startswith(prefix) or len(record_id) != len(prefix) + 32:
        _raise(integrity, "record_id_invalid", "Locks record id has the wrong kind prefix")
    _hex(record_id[len(prefix):], "record_uuid", re.compile(r"[0-9a-f]{32}\Z"), integrity=integrity)
    row["timestamp"] = validate_timestamp(row.get("timestamp"), "timestamp", integrity=integrity)

    if kind == "lock_acquired":
        row["lock_id"] = _identifier(row.get("lock_id"), "lock_id", integrity=integrity)
        row["holder"] = _identifier(row.get("holder"), "holder", integrity=integrity)
        row["escalation_holder"] = _identifier(row.get("escalation_holder"), "escalation_holder", integrity=integrity)
        row["expires_at"] = validate_timestamp(row.get("expires_at"), "expires_at", integrity=integrity)
        if timestamp_value(row["expires_at"]) <= timestamp_value(row["timestamp"]):
            _raise(integrity, "lock_expiry_invalid", "lock expiry must be later than acquisition testimony")
    elif kind == "lock_escalated":
        for field in (
            "lock_id", "prior_holder", "requested_by", "holder",
            "escalation_holder", "announcement_recipient",
            "announcement_rearm_event",
        ):
            row[field] = _identifier(row.get(field), field, integrity=integrity)
        row["prior_expires_at"] = validate_timestamp(
            row.get("prior_expires_at"), "prior_expires_at", integrity=integrity,
        )
        row["expires_at"] = validate_timestamp(row.get("expires_at"), "expires_at", integrity=integrity)
        if timestamp_value(row["expires_at"]) <= timestamp_value(row["timestamp"]):
            _raise(integrity, "lock_expiry_invalid", "escalated lock expiry must follow its testimony")
        announcement_id = _safe_text(
            row.get("announcement_id"), "announcement_id", integrity=integrity, maximum=96,
        )
        prefix = "lock-announcement-"
        if not announcement_id.startswith(prefix) or len(announcement_id) != len(prefix) + 32:
            _raise(integrity, "announcement_id_invalid", "announcement id has the wrong prefix")
        _hex(announcement_id[len(prefix):], "announcement_uuid", re.compile(r"[0-9a-f]{32}\Z"), integrity=integrity)
        row["announcement_id"] = announcement_id
        if row.get("announcement_status") != "pending":
            _raise(integrity, "announcement_status_invalid", "new escalation announcement must be pending")
        if row.get("copy_key") != "[[locks.escalation.action_taken_not_role]]":
            _raise(integrity, "copy_key_invalid", "escalation announcement must use its governed copy key")
    elif kind == "lock_announcement_stopped":
        row["lock_id"] = _identifier(row.get("lock_id"), "lock_id", integrity=integrity)
        row["announcement_id"] = _safe_text(
            row.get("announcement_id"), "announcement_id", integrity=integrity, maximum=96,
        )
        attempts = row.get("attempts")
        if type(attempts) is not int or attempts <= 0:
            _raise(integrity, "announcement_attempts_invalid", "stopped announcement needs positive attempts")
        row["stopped_at"] = validate_timestamp(row.get("stopped_at"), "stopped_at", integrity=integrity)
        row["rearm_event"] = _identifier(row.get("rearm_event"), "rearm_event", integrity=integrity)
    elif kind == "lock_announcement_rearmed":
        row["lock_id"] = _identifier(row.get("lock_id"), "lock_id", integrity=integrity)
        row["announcement_id"] = _safe_text(
            row.get("announcement_id"), "announcement_id", integrity=integrity, maximum=96,
        )
        row["observed_event"] = _identifier(row.get("observed_event"), "observed_event", integrity=integrity)
    elif kind == "lock_announcement_delivered":
        row["lock_id"] = _identifier(row.get("lock_id"), "lock_id", integrity=integrity)
        row["announcement_id"] = _safe_text(
            row.get("announcement_id"), "announcement_id", integrity=integrity, maximum=96,
        )
        receipt_id = _safe_text(row.get("receipt_id"), "receipt_id", integrity=integrity, maximum=128)
        if not receipt_id.startswith("delivery-"):
            _raise(integrity, "delivery_receipt_required", "announcement delivery needs one delivery receipt id")
        row["receipt_id"] = receipt_id
        row["receipt_digest"] = _hex(
            row.get("receipt_digest"), "receipt_digest", HEX64_PATTERN, integrity=integrity,
        )
    elif kind == "car_submitted":
        row["car_id"] = _identifier(row.get("car_id"), "car_id", integrity=integrity)
        row["ref"] = validate_full_ref(row.get("ref"), "ref", integrity=integrity)
        row["ref_oid"] = _hex(row.get("ref_oid"), "ref_oid", HEX40_PATTERN, integrity=integrity)
        row["witness"] = validate_witness(row.get("witness"), integrity=integrity)
    elif kind == "car_measurement_recorded":
        row["car_id"] = _identifier(row.get("car_id"), "car_id", integrity=integrity)
        row["measured_ref"] = validate_full_ref(
            row.get("measured_ref"), "measured_ref", integrity=integrity,
            code="measurement_ref_required",
        )
        row["measured_tree"] = _hex(row.get("measured_tree"), "measured_tree", HEX40_PATTERN, integrity=integrity)
        for field in ("test_count", "failure_count"):
            value = row.get(field)
            if type(value) is not int or value < 0:
                _raise(integrity, f"{field}_invalid", f"{field} must be a non-negative integer")
        if row["failure_count"] > row["test_count"]:
            _raise(integrity, "failure_count_invalid", "failure count cannot exceed test count")
        row["evidence_digest"] = _hex(row.get("evidence_digest"), "evidence_digest", HEX64_PATTERN, integrity=integrity)
    elif kind == "car_reviewed":
        row["car_id"] = _identifier(row.get("car_id"), "car_id", integrity=integrity)
        verdict = row.get("verdict")
        if verdict not in {"approved", "blocked"}:
            _raise(integrity, "review_verdict_invalid", "review verdict must be approved or blocked")
        rank = row.get("rank")
        if type(rank) is not int or not 0 <= rank <= 100:
            _raise(integrity, "review_rank_invalid", "review rank must be an integer from 0 through 100")
        row["base_ref"] = validate_full_ref(row.get("base_ref"), "base_ref", integrity=integrity)
        row["base_oid"] = _hex(row.get("base_oid"), "base_oid", HEX40_PATTERN, integrity=integrity)
        row["base_tree"] = _hex(row.get("base_tree"), "base_tree", HEX40_PATTERN, integrity=integrity)
        if type(row.get("witness_holds")) is not bool:
            _raise(integrity, "witness_result_invalid", "review witness result must be an exact boolean")
    elif kind == "car_landed":
        row["car_id"] = _identifier(row.get("car_id"), "car_id", integrity=integrity)
        row["target_ref"] = validate_full_ref(row.get("target_ref"), "target_ref", integrity=integrity)
        row["target_oid"] = _hex(row.get("target_oid"), "target_oid", HEX40_PATTERN, integrity=integrity)
        row["target_tree"] = _hex(row.get("target_tree"), "target_tree", HEX40_PATTERN, integrity=integrity)
        if row.get("method") not in {"cherry_pick", "rebase"}:
            _raise(integrity, "landing_method_invalid", "landing method must be cherry_pick or rebase")
        if row.get("witness_holds") is not True:
            _raise(integrity, "witness_result_invalid", "landed testimony requires a positive witness")
    elif kind == "car_dissolved":
        row["car_id"] = _identifier(row.get("car_id"), "car_id", integrity=integrity)
        row["product_ref"] = validate_full_ref(row.get("product_ref"), "product_ref", integrity=integrity)
        row["product_oid"] = _hex(row.get("product_oid"), "product_oid", HEX40_PATTERN, integrity=integrity)
        row["product_tree"] = _hex(row.get("product_tree"), "product_tree", HEX40_PATTERN, integrity=integrity)
        if row.get("witness_holds") is not True:
            _raise(integrity, "witness_result_invalid", "dissolution requires a positive product witness")
    elif kind == "seat_provisioned":
        row["seat_id"] = _identifier(row.get("seat_id"), "seat_id", integrity=integrity)
        hook_names = row.get("hook_names")
        if type(hook_names) is not list or not hook_names or len(hook_names) > 64:
            _raise(integrity, "provisioning_hooks_invalid", "seat needs one bounded hook-name list")
        normalized_hooks = [
            _identifier(name, "hook_name", integrity=integrity)
            for name in hook_names
        ]
        if len(set(normalized_hooks)) != len(normalized_hooks):
            _raise(integrity, "provisioning_hooks_invalid", "seat hook names must be unique")
        row["hook_names"] = normalized_hooks
        row["manifest_digest"] = _hex(
            row.get("manifest_digest"), "manifest_digest", HEX64_PATTERN, integrity=integrity,
        )
    elif kind.startswith("review_handoff_"):
        for field in ("handoff_id", "recipient", "car_id", "rearm_event"):
            row[field] = _identifier(row.get(field), field, integrity=integrity)
        row["ref"] = validate_full_ref(row.get("ref"), "ref", integrity=integrity)
        row["base_ref"] = validate_full_ref(row.get("base_ref"), "base_ref", integrity=integrity)
        row["witness"] = validate_witness(row.get("witness"), integrity=integrity)
        if kind == "review_handoff_queued":
            if row.get("status") != "pending":
                _raise(integrity, "handoff_status_invalid", "queued handoff must be pending")
            if row.get("copy_key") != "[[locks.handoff.pending]]":
                _raise(integrity, "copy_key_invalid", "queued handoff must use its governed copy key")
        elif kind == "review_handoff_stopped":
            if row.get("status") != "stopped":
                _raise(integrity, "handoff_status_invalid", "stopped handoff must be stopped")
            if row.get("copy_key") != "[[locks.handoff.stopped]]":
                _raise(integrity, "copy_key_invalid", "stopped handoff must use its governed copy key")
            attempts = row.get("attempts")
            if type(attempts) is not int or attempts <= 0:
                _raise(integrity, "handoff_attempts_invalid", "stopped handoff needs positive attempts")
            row["stopped_at"] = validate_timestamp(
                row.get("stopped_at"), "stopped_at", integrity=integrity,
            )
        elif kind == "review_handoff_rearmed":
            if row.get("status") != "pending":
                _raise(integrity, "handoff_status_invalid", "re-armed handoff must be pending")
            if row.get("copy_key") != "[[locks.handoff.pending]]":
                _raise(integrity, "copy_key_invalid", "re-armed handoff must use its governed copy key")
            row["observed_event"] = _identifier(
                row.get("observed_event"), "observed_event", integrity=integrity,
            )
        else:
            if row.get("status") != "delivered":
                _raise(integrity, "handoff_status_invalid", "delivered handoff must be delivered")
            receipt_id = _safe_text(
                row.get("receipt_id"), "receipt_id", integrity=integrity, maximum=128,
            )
            if not receipt_id.startswith("delivery-"):
                _raise(integrity, "delivery_receipt_required", "handoff delivery needs one receipt id")
            row["receipt_id"] = receipt_id
            row["receipt_digest"] = _hex(
                row.get("receipt_digest"), "receipt_digest", HEX64_PATTERN, integrity=integrity,
            )
    return row

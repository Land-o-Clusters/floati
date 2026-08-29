"""Closed, canonical request/result frames for reconciliation observation."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import secrets
import struct
import unicodedata
from dataclasses import dataclass
from typing import Optional, Union
from urllib.parse import urlsplit

from .errors import ProtocolRefusal
from .root import IDENTIFIER_PATTERN


MAX_FRAME_BYTES = 65_536
MAX_CONTAINER_ITEMS = 64
MAX_NESTING = 16
SCHEMA_VERSION = 1

REQUEST_FIELDS = frozenset({
    "schema_version", "request_id", "request_digest", "operation_id",
    "current_evidence_id", "adapter", "target", "expected_confirmation",
    "budget_claim", "local_repository_identity",
})
RESULT_FIELDS = frozenset({
    "schema_version", "request_id", "request_digest", "outcome",
    "evidence_digest", "reason_code", "observation", "confirmation",
    "spend_status", "measured_spend",
})
ADAPTERS = frozenset({
    "git_local", "git_remote_explicit", "github_explicit",
    "deployment_explicit", "none",
})
OUTCOMES = frozenset({"confirmed", "failed", "unknown"})
SPEND_STATUSES = frozenset({"complete", "unknown"})

_TARGET_KINDS = frozenset({
    "git_ref", "git_remote_ref", "github_resource", "deployment_target",
    "shell_environment", "external_api_resource",
})
_CONFIRMATION_KINDS = frozenset({
    "git_ref_equals", "git_remote_ref_equals", "github_idempotency_marker",
    "deployment_artifact_equals", "none",
})
_ADAPTER_CONTRACTS = {
    "git_local": ("git_ref", "git_ref_equals"),
    "git_remote_explicit": ("git_remote_ref", "git_remote_ref_equals"),
    "github_explicit": ("github_resource", "github_idempotency_marker"),
    "deployment_explicit": ("deployment_target", "deployment_artifact_equals"),
}
_UUID7 = r"[0-9a-f]{12}7[0-9a-f]{3}[89ab][0-9a-f]{15}"
_HEX32 = re.compile(r"[0-9a-f]{32}\Z")
_HEX64 = re.compile(r"[0-9a-f]{64}\Z")
_OPERATION_ID = re.compile(r"effect-op-" + _UUID7 + r"\Z")
_EVIDENCE_ID = re.compile(
    r"(?:effect-confirmed|effect-failed|effect-unknown|effect-reconciled)-"
    + _UUID7 + r"\Z"
)
_REMOTE_HOST = re.compile(
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
    r"(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*\Z"
)
_REMOTE_USER = re.compile(r"[A-Za-z0-9._~-]{1,64}\Z")
_REMOTE_PATH_COMPONENT = re.compile(r"[A-Za-z0-9._~+-]{1,255}\Z")
_BIDI_CONTROLS = frozenset({
    "LRE", "RLE", "LRO", "RLO", "PDF", "LRI", "RLI", "FSI", "PDI", "BN",
})

# Task-5 reconciliation dispositions plus the parent-owned failure outcomes
# introduced by the descriptor-bound observer lifecycle.
REASON_CODES = frozenset({
    "contract_invalid", "repository_fence_invalid", "repository_identity_changed",
    "confirmation_absent", "expected_object_absent", "ref_digest_mismatch",
    "remote_identity_mismatch", "remote_coordinate_unsupported",
    "destination_unqueryable", "evidence_malformed", "adapter_unavailable",
    "reconciliation_inconclusive", "budget_claim_malformed",
    "exact_ref_and_object", "exact_remote_ref",
    "git_observation_timeout", "git_observation_unavailable",
    "git_observation_malformed", "git_object_observation_timeout",
    "git_object_observation_unavailable", "git_object_observation_malformed",
    "git_remote_observation_timeout", "git_remote_observation_unavailable",
    "git_remote_observation_malformed", "observer_launch_failed",
    "observer_timeout", "observer_protocol_invalid", "observer_child_died",
    "observer_child_nonzero", "observer_result_missing", "observer_eof_missing",
    "observer_channel_invalid", "observer_result_binding_invalid",
    "observer_cleanup_failed", "observer_request_invalid", "protocol_error",
})

_PARENT_UNKNOWN_REASONS = frozenset({
    "observer_launch_failed", "observer_timeout", "observer_protocol_invalid",
    "observer_child_died", "observer_child_nonzero", "observer_result_missing",
    "observer_eof_missing", "observer_channel_invalid",
    "observer_result_binding_invalid", "observer_cleanup_failed",
    "observer_request_invalid", "protocol_error", "budget_claim_malformed",
})
_LOCAL_UNKNOWN_REASONS = frozenset({
    "contract_invalid", "repository_fence_invalid", "repository_identity_changed",
    "evidence_malformed", "git_observation_timeout",
    "git_observation_unavailable", "git_observation_malformed",
})
_LOCAL_OBJECT_UNKNOWN_REASONS = frozenset({
    "git_object_observation_timeout", "git_object_observation_unavailable",
    "git_object_observation_malformed",
})
_REMOTE_UNKNOWN_REASONS = frozenset({
    "contract_invalid", "remote_identity_mismatch", "remote_coordinate_unsupported",
    "destination_unqueryable", "evidence_malformed",
    "git_remote_observation_timeout", "git_remote_observation_unavailable",
    "git_remote_observation_malformed",
})


@dataclass(frozen=True)
class ReconciliationRequest:
    schema_version: int
    request_id: str
    request_digest: str
    operation_id: str
    current_evidence_id: str
    adapter: str
    target: dict[str, object]
    expected_confirmation: dict[str, object]
    budget_claim: dict[str, int]
    local_repository_identity: Optional[tuple[int, int]]


@dataclass(frozen=True)
class ReconciliationResult:
    schema_version: int
    request_id: str
    request_digest: str
    outcome: str
    evidence_digest: str
    reason_code: str
    observation: Optional[dict[str, object]]
    confirmation: Optional[dict[str, object]]
    spend_status: str
    measured_spend: Optional[dict[str, int]]


class _DuplicateJSONKey(ValueError):
    pass


def _refuse(code: str, detail: str) -> None:
    raise ProtocolRefusal(code, detail)


def _canonical(value: object) -> bytes:
    try:
        return json.dumps(
            value, ensure_ascii=False, allow_nan=False, sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise ProtocolRefusal(
            "reconciliation_protocol_ijson_invalid",
            "reconciliation protocol value cannot form canonical I-JSON",
        ) from exc


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _safe_string(value: object, field: str, *, maximum: int = MAX_FRAME_BYTES) -> str:
    if type(value) is not str:
        _refuse("reconciliation_protocol_field_invalid", field + " must be an exact string")
    try:
        encoded = value.encode("utf-8", "strict")
    except UnicodeEncodeError as exc:
        raise ProtocolRefusal(
            "reconciliation_protocol_unicode_invalid", field + " is not strict UTF-8",
        ) from exc
    if not encoded or len(encoded) > maximum:
        _refuse("reconciliation_protocol_field_invalid", field + " exceeds its bound")
    if any(
        unicodedata.category(character) in {"Cc", "Cs"}
        or unicodedata.bidirectional(character) in _BIDI_CONTROLS
        for character in value
    ):
        _refuse("reconciliation_protocol_unicode_invalid", field + " contains a control character")
    return value


def classify_remote_coordinate(value: object) -> Optional[str]:
    """Return the evidence scope for one exact canonical remote coordinate."""

    if type(value) is not str:
        return None
    try:
        encoded = value.encode("utf-8", "strict")
    except UnicodeEncodeError:
        return None
    if (
        not encoded or len(encoded) > 1024 or value.startswith("-")
        or any(
            unicodedata.category(character) in {"Cc", "Cs"}
            or unicodedata.bidirectional(character) in _BIDI_CONTROLS
            for character in value
        )
    ):
        return None
    if value.startswith("/"):
        normalized = os.path.normpath(value)
        if (
            normalized != value
            or os.path.realpath(value) != value
            or any(component == ".." for component in value.split("/"))
        ):
            return None
        return "filesystem_fixture"
    try:
        parts = urlsplit(value)
    except ValueError:
        return None
    if (
        parts.scheme not in {"https", "ssh"}
        or not parts.netloc
        or not parts.path.startswith("/")
        or parts.query
        or parts.fragment
        or parts.hostname is None
        or len(parts.hostname) > 253
        or _REMOTE_HOST.fullmatch(parts.hostname) is None
        or parts.password is not None
    ):
        return None
    username = parts.username
    if username is not None and (
        parts.scheme != "ssh" or _REMOTE_USER.fullmatch(username) is None
    ):
        return None
    try:
        port = parts.port
    except ValueError:
        return None
    if (
        port is not None
        and (port < 1 or port > 65_535 or port == (443 if parts.scheme == "https" else 22))
    ):
        return None
    path_components = parts.path[1:].split("/")
    if not path_components or any(
        component in {"", ".", ".."}
        or _REMOTE_PATH_COMPONENT.fullmatch(component) is None
        for component in path_components
    ):
        return None
    authority = parts.hostname
    if username is not None:
        authority = username + "@" + authority
    if port is not None:
        authority += ":" + str(port)
    canonical = parts.scheme + "://" + authority + "/" + "/".join(path_components)
    if canonical != value:
        return None
    return "explicit_remote"


def _sha256(value: object, field: str) -> str:
    if type(value) is not str or _HEX64.fullmatch(value) is None:
        _refuse("reconciliation_protocol_field_invalid", field + " must be a lowercase SHA-256 digest")
    return value


def _integer(value: object, field: str, *, minimum: int = 0, maximum: int = 1_000_000_000) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        _refuse("reconciliation_protocol_field_invalid", field + " must be a bounded exact integer")
    return value


def _exact_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJSONKey(key)
        result[key] = value
    return result


def _copy_json(value: object, field: str, *, depth: int = 0) -> object:
    """Return a detached exact-builtin I-JSON value under fixed recursion bounds."""

    if depth > MAX_NESTING:
        _refuse("reconciliation_protocol_container_invalid", field + " exceeds nesting bound")
    value_type = type(value)
    if value_type is None.__class__ or value_type is bool:
        return value
    if value_type is int:
        return _integer(value, field, minimum=-(2 ** 53 - 1), maximum=2 ** 53 - 1)
    if value_type is float:
        if not math.isfinite(value):
            _refuse("reconciliation_protocol_ijson_invalid", field + " must be finite")
        return value
    if value_type is str:
        return _safe_string(value, field)
    if value_type is list:
        if len(value) > MAX_CONTAINER_ITEMS:
            _refuse("reconciliation_protocol_container_invalid", field + " has too many items")
        return [_copy_json(member, field, depth=depth + 1) for member in value]
    if value_type is dict:
        if len(value) > MAX_CONTAINER_ITEMS:
            _refuse("reconciliation_protocol_container_invalid", field + " has too many members")
        copied: dict[str, object] = {}
        for key, member in value.items():
            copied_key = _safe_string(key, field + " key")
            copied[copied_key] = _copy_json(member, field + "." + copied_key, depth=depth + 1)
        return copied
    _refuse("reconciliation_protocol_type_invalid", field + " must use exact JSON builtins")
    raise AssertionError("unreachable")


def _target(value: object) -> dict[str, object]:
    if type(value) is not dict or set(value) != {"kind", "coordinate", "identity_digest"}:
        _refuse("reconciliation_protocol_target_invalid", "target must use its exact closed shape")
    kind = value["kind"]
    if type(kind) is not str or kind not in _TARGET_KINDS:
        _refuse("reconciliation_protocol_target_invalid", "target kind is outside the closed set")
    return {
        "kind": kind,
        "coordinate": _safe_string(value["coordinate"], "target.coordinate", maximum=1024),
        "identity_digest": _sha256(value["identity_digest"], "target.identity_digest"),
    }


def _confirmation(value: object, field: str) -> dict[str, object]:
    if type(value) is not dict or set(value) != {"kind", "locator", "expected_digest"}:
        _refuse("reconciliation_protocol_confirmation_invalid", field + " must use its exact closed shape")
    kind = value["kind"]
    if type(kind) is not str or kind not in _CONFIRMATION_KINDS:
        _refuse("reconciliation_protocol_confirmation_invalid", field + " kind is outside the closed set")
    return {
        "kind": kind,
        "locator": _safe_string(value["locator"], field + ".locator", maximum=1024),
        "expected_digest": _sha256(value["expected_digest"], field + ".expected_digest"),
    }


def _budget(value: object, field: str) -> dict[str, int]:
    if type(value) is not dict or len(value) > MAX_CONTAINER_ITEMS:
        _refuse("reconciliation_protocol_budget_invalid", field + " must be a bounded exact map")
    copied: dict[str, int] = {}
    for budget_id, amount in value.items():
        if type(budget_id) is not str or IDENTIFIER_PATTERN.fullmatch(budget_id) is None:
            _refuse("reconciliation_protocol_budget_invalid", field + " has an invalid budget id")
        copied[budget_id] = _integer(amount, field + "." + budget_id)
    return dict(sorted(copied.items()))


def _identity(value: object, *, wire: bool) -> Optional[tuple[int, int]]:
    if value is None:
        return None
    expected_type = list if wire else tuple
    if type(value) is not expected_type or len(value) != 2:
        _refuse("reconciliation_protocol_identity_invalid", "local repository identity must be one exact device/inode pair")
    device = _integer(value[0], "local_repository_identity.device", maximum=2 ** 63 - 1)
    inode = _integer(value[1], "local_repository_identity.inode", minimum=1, maximum=2 ** 63 - 1)
    return (device, inode)


def _request_payload(request: ReconciliationRequest, *, semantic: bool) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": request.schema_version,
        "operation_id": request.operation_id,
        "current_evidence_id": request.current_evidence_id,
        "adapter": request.adapter,
        "target": dict(request.target),
        "expected_confirmation": dict(request.expected_confirmation),
        "budget_claim": dict(request.budget_claim),
        "local_repository_identity": (
            None if request.local_repository_identity is None
            else list(request.local_repository_identity)
        ),
    }
    if not semantic:
        payload["request_id"] = request.request_id
        payload["request_digest"] = request.request_digest
        return {field: payload[field] for field in REQUEST_FIELDS}
    return payload


def _result_payload(result: ReconciliationResult) -> dict[str, object]:
    return {
        "schema_version": result.schema_version,
        "request_id": result.request_id,
        "request_digest": result.request_digest,
        "outcome": result.outcome,
        "evidence_digest": result.evidence_digest,
        "reason_code": result.reason_code,
        "observation": None if result.observation is None else dict(result.observation),
        "confirmation": None if result.confirmation is None else dict(result.confirmation),
        "spend_status": result.spend_status,
        "measured_spend": None if result.measured_spend is None else dict(result.measured_spend),
    }


def _request_from_fields(
    value: object, *, wire: bool, validate_digest: bool = True,
) -> ReconciliationRequest:
    if type(value) is not dict or set(value) != REQUEST_FIELDS:
        _refuse("reconciliation_protocol_request_invalid", "request must use its exact closed shape")
    if _integer(value["schema_version"], "schema_version", minimum=SCHEMA_VERSION, maximum=SCHEMA_VERSION) != SCHEMA_VERSION:
        _refuse("reconciliation_protocol_request_invalid", "request schema version is unsupported")
    request_id = value["request_id"]
    if type(request_id) is not str or _HEX32.fullmatch(request_id) is None:
        _refuse("reconciliation_protocol_request_invalid", "request_id must be 32 lowercase hexadecimal characters")
    request_digest = _sha256(value["request_digest"], "request_digest")
    operation_id = value["operation_id"]
    current_evidence_id = value["current_evidence_id"]
    adapter = value["adapter"]
    if type(operation_id) is not str or _OPERATION_ID.fullmatch(operation_id) is None:
        _refuse("reconciliation_protocol_request_invalid", "operation_id is invalid")
    if type(current_evidence_id) is not str or _EVIDENCE_ID.fullmatch(current_evidence_id) is None:
        _refuse("reconciliation_protocol_request_invalid", "current_evidence_id is invalid")
    if type(adapter) is not str or adapter not in ADAPTERS:
        _refuse("reconciliation_protocol_request_invalid", "adapter is outside the closed set")
    target = _target(value["target"])
    expected = _confirmation(value["expected_confirmation"], "expected_confirmation")
    if adapter == "none":
        if expected["kind"] != "none":
            _refuse("reconciliation_protocol_request_invalid", "none adapter requires none confirmation")
    else:
        expected_contract = _ADAPTER_CONTRACTS[adapter]
        if (target["kind"], expected["kind"]) != expected_contract:
            _refuse("reconciliation_protocol_request_invalid", "adapter target and confirmation kinds do not match")
    request = ReconciliationRequest(
        schema_version=SCHEMA_VERSION,
        request_id=request_id,
        request_digest=request_digest,
        operation_id=operation_id,
        current_evidence_id=current_evidence_id,
        adapter=adapter,
        target=target,
        expected_confirmation=expected,
        budget_claim=_budget(value["budget_claim"], "budget_claim"),
        local_repository_identity=_identity(value["local_repository_identity"], wire=wire),
    )
    if request.adapter == "git_local" and request.local_repository_identity is None:
        _refuse("reconciliation_protocol_identity_invalid", "git_local requires a repository identity")
    if request.adapter != "git_local" and request.local_repository_identity is not None:
        _refuse("reconciliation_protocol_identity_invalid", "only git_local carries a repository identity")
    if validate_digest and request.request_digest != _digest(_request_payload(request, semantic=True)):
        _refuse("reconciliation_protocol_request_digest_invalid", "request digest does not cover semantic request fields")
    return request


def build_request(
    *, operation_id: object, current_evidence_id: object, adapter: object,
    target: object, expected_confirmation: object, budget_claim: object,
    local_repository_identity: object, request_id: Optional[str] = None,
) -> ReconciliationRequest:
    """Build one detached request with a correlation-only fresh request id."""

    selected_request_id = secrets.token_hex(16) if request_id is None else request_id
    draft = {
        "schema_version": SCHEMA_VERSION,
        "request_id": selected_request_id,
        "request_digest": "0" * 64,
        "operation_id": operation_id,
        "current_evidence_id": current_evidence_id,
        "adapter": adapter,
        "target": target,
        "expected_confirmation": expected_confirmation,
        "budget_claim": budget_claim,
        "local_repository_identity": local_repository_identity,
    }
    # Builder input uses the dataclass identity tuple, unlike decoded JSON.
    prevalidated = _request_from_fields(draft, wire=False, validate_digest=False)
    semantic_digest = _digest(_request_payload(prevalidated, semantic=True))
    completed = dict(draft, request_digest=semantic_digest)
    return _request_from_fields(completed, wire=False)


def validate_request(value: object) -> ReconciliationRequest:
    """Validate and detach one request from a trusted caller or decoded frame."""

    if type(value) is ReconciliationRequest:
        payload = _request_payload(value, semantic=False)
        payload["local_repository_identity"] = value.local_repository_identity
        return _request_from_fields(payload, wire=False)
    return _request_from_fields(value, wire=True)


def _evidence_payload(
    request: ReconciliationRequest, *, outcome: str, reason_code: str,
    observation: Optional[dict[str, object]], confirmation: Optional[dict[str, object]],
    spend_status: str, measured_spend: Optional[dict[str, int]],
) -> dict[str, object]:
    return {
        "request": _request_payload(request, semantic=True),
        "outcome": outcome,
        "reason_code": reason_code,
        "observation": observation,
        "confirmation": confirmation,
        "spend_status": spend_status,
        "measured_spend": measured_spend,
    }


def _exact_observation(
    observation: Optional[dict[str, object]], fields: frozenset[str],
) -> bool:
    return type(observation) is dict and set(observation) == fields


def _local_observation(
    observation: Optional[dict[str, object]], request: ReconciliationRequest,
    *, exact: Optional[bool] = None,
) -> bool:
    if not _exact_observation(observation, frozenset({"observed_ref_digest"})):
        return False
    observed = observation["observed_ref_digest"]
    if type(observed) is not str or _HEX64.fullmatch(observed) is None:
        return False
    expected = request.expected_confirmation["expected_digest"]
    return exact is None or (observed == expected) is exact


def _remote_observation(
    observation: Optional[dict[str, object]], request: ReconciliationRequest,
    *, exact: Optional[bool] = None,
) -> bool:
    if not _exact_observation(
        observation, frozenset({"observed_ref_digest", "evidence_scope"}),
    ):
        return False
    observed = observation["observed_ref_digest"]
    scope = observation["evidence_scope"]
    expected_scope = classify_remote_coordinate(request.target["coordinate"])
    if (
        type(observed) is not str
        or _HEX64.fullmatch(observed) is None
        or type(scope) is not str
        or scope != expected_scope
    ):
        return False
    expected = request.expected_confirmation["expected_digest"]
    return exact is None or (observed == expected) is exact


def _parent_observation(
    reason_code: str, observation: Optional[dict[str, object]],
) -> bool:
    if observation is None:
        return True
    if reason_code == "observer_launch_failed":
        return (
            _exact_observation(observation, frozenset({"error_type"}))
            and type(observation["error_type"]) is str
            and 0 < len(observation["error_type"].encode("utf-8")) <= 128
        )
    if reason_code == "observer_channel_invalid":
        return (
            _exact_observation(observation, frozenset({"peer_pid", "spawned_pid"}))
            and all(
                type(observation[field]) is int and 1 < observation[field] <= 2 ** 31 - 1
                for field in ("peer_pid", "spawned_pid")
            )
        )
    if reason_code == "observer_child_died":
        return (
            _exact_observation(observation, frozenset({"signal"}))
            and type(observation["signal"]) is int
            and 0 < observation["signal"] <= 255
        )
    if reason_code == "observer_child_nonzero":
        return (
            _exact_observation(observation, frozenset({"exit_code"}))
            and type(observation["exit_code"]) is int
            and 0 < observation["exit_code"] <= 255
        )
    if reason_code == "observer_cleanup_failed":
        return (
            _exact_observation(observation, frozenset({"pending_error_type"}))
            and type(observation["pending_error_type"]) is str
            and 0 < len(observation["pending_error_type"].encode("utf-8")) <= 128
        )
    return False


def _result_contract_is_closed(
    request: ReconciliationRequest, *, outcome: str, reason_code: str,
    observation: Optional[dict[str, object]],
) -> bool:
    if reason_code in _PARENT_UNKNOWN_REASONS:
        return outcome == "unknown" and _parent_observation(reason_code, observation)
    adapter = request.adapter
    if adapter == "git_local":
        if outcome == "confirmed":
            return reason_code == "exact_ref_and_object" and _local_observation(
                observation, request, exact=True,
            )
        if outcome == "failed":
            if reason_code == "confirmation_absent":
                return observation is None
            if reason_code == "expected_object_absent":
                return _local_observation(observation, request)
            if reason_code == "ref_digest_mismatch":
                return _local_observation(observation, request, exact=False)
            return False
        if outcome == "unknown":
            if reason_code == "reconciliation_inconclusive":
                return _local_observation(observation, request, exact=True)
            if reason_code in _LOCAL_UNKNOWN_REASONS:
                return observation is None
            if reason_code in _LOCAL_OBJECT_UNKNOWN_REASONS:
                return _local_observation(observation, request)
        return False
    if adapter == "git_remote_explicit":
        if outcome == "confirmed":
            return reason_code == "exact_remote_ref" and _remote_observation(
                observation, request, exact=True,
            )
        if outcome == "failed":
            if reason_code == "confirmation_absent":
                return observation is None
            if reason_code == "ref_digest_mismatch":
                return _remote_observation(observation, request, exact=False)
            return False
        if outcome == "unknown" and reason_code == "reconciliation_inconclusive":
            return _remote_observation(observation, request, exact=True)
        return (
            outcome == "unknown"
            and reason_code in _REMOTE_UNKNOWN_REASONS
            and observation is None
        )
    if adapter in {"github_explicit", "deployment_explicit"}:
        return (
            outcome == "unknown"
            and (
                (reason_code == "contract_invalid" and observation is None)
                or (
                    reason_code == "adapter_unavailable"
                    and _exact_observation(observation, frozenset({"adapter"}))
                    and observation["adapter"] == adapter
                )
            )
        )
    return (
        outcome == "unknown"
        and (
            (reason_code == "contract_invalid" and observation is None)
            or (
                reason_code == "reconciliation_inconclusive"
                and _exact_observation(observation, frozenset({"adapter"}))
                and observation["adapter"] == "none"
            )
        )
    )


def _result_from_fields(value: object, request: ReconciliationRequest) -> ReconciliationResult:
    if type(value) is not dict or set(value) != RESULT_FIELDS:
        _refuse("reconciliation_protocol_result_invalid", "result must use its exact closed shape")
    if _integer(value["schema_version"], "schema_version", minimum=SCHEMA_VERSION, maximum=SCHEMA_VERSION) != SCHEMA_VERSION:
        _refuse("reconciliation_protocol_result_invalid", "result schema version is unsupported")
    request_id = value["request_id"]
    request_digest = value["request_digest"]
    if (
        type(request_id) is not str
        or type(request_digest) is not str
        or request_id != request.request_id
        or request_digest != request.request_digest
    ):
        _refuse("reconciliation_protocol_result_binding_invalid", "result is not bound to this exact request")
    outcome = value["outcome"]
    reason_code = value["reason_code"]
    spend_status = value["spend_status"]
    if type(outcome) is not str or outcome not in OUTCOMES:
        _refuse("reconciliation_protocol_result_invalid", "result outcome is outside the closed set")
    if type(reason_code) is not str or reason_code not in REASON_CODES:
        _refuse("reconciliation_protocol_result_invalid", "reason code is outside the closed set")
    if type(spend_status) is not str or spend_status not in SPEND_STATUSES:
        _refuse("reconciliation_protocol_result_invalid", "spend status is outside the closed set")
    raw_observation = value["observation"]
    if raw_observation is None:
        observation = None
    elif type(raw_observation) is dict:
        observation = _copy_json(raw_observation, "observation")
        if type(observation) is not dict:
            raise AssertionError("observation copy changed map type")
    else:
        _refuse("reconciliation_protocol_result_invalid", "observation must be null or an exact map")
    raw_confirmation = value["confirmation"]
    confirmation = None if raw_confirmation is None else _confirmation(raw_confirmation, "confirmation")
    raw_measured = value["measured_spend"]
    measured = None if raw_measured is None else _budget(raw_measured, "measured_spend")
    if outcome == "confirmed":
        if (
            confirmation != request.expected_confirmation
            or spend_status != "complete"
            or measured != request.budget_claim
        ):
            _refuse(
                "reconciliation_protocol_confirmed_invalid",
                "confirmed result requires exact confirmation and complete measured spend",
            )
    elif confirmation is not None or spend_status != "unknown" or measured is not None:
        _refuse(
            "reconciliation_protocol_nonconfirmed_invalid",
            "failed and unknown results cannot carry confirmation or measured spend",
        )
    if not _result_contract_is_closed(
        request, outcome=outcome, reason_code=reason_code,
        observation=observation,
    ):
        _refuse(
            "reconciliation_protocol_result_contract_invalid",
            "result adapter, outcome, reason, and observation are incompatible",
        )
    evidence_digest = _sha256(value["evidence_digest"], "evidence_digest")
    result = ReconciliationResult(
        schema_version=SCHEMA_VERSION,
        request_id=request.request_id,
        request_digest=request.request_digest,
        outcome=outcome,
        evidence_digest=evidence_digest,
        reason_code=reason_code,
        observation=observation,
        confirmation=confirmation,
        spend_status=spend_status,
        measured_spend=measured,
    )
    if evidence_digest != _digest(_evidence_payload(
        request, outcome=outcome, reason_code=reason_code, observation=observation,
        confirmation=confirmation, spend_status=spend_status, measured_spend=measured,
    )):
        _refuse("reconciliation_protocol_evidence_digest_invalid", "evidence digest does not cover result semantics")
    return result


def build_result(
    request: ReconciliationRequest, *, outcome: str, reason_code: str,
    observation: object = None, confirmation: object = None,
    spend_status: str = "unknown", measured_spend: object = None,
) -> ReconciliationResult:
    """Build one result whose evidence digest binds request and result semantics."""

    selected_request = validate_request(request)
    draft = {
        "schema_version": SCHEMA_VERSION,
        "request_id": selected_request.request_id,
        "request_digest": selected_request.request_digest,
        "outcome": outcome,
        "evidence_digest": "0" * 64,
        "reason_code": reason_code,
        "observation": observation,
        "confirmation": confirmation,
        "spend_status": spend_status,
        "measured_spend": measured_spend,
    }
    # Validate all result invariants before calculating the evidence binding.
    preliminary = _result_from_fields(dict(draft, evidence_digest=_digest(_evidence_payload(
        selected_request, outcome=outcome, reason_code=reason_code,
        observation=(None if observation is None else _copy_json(observation, "observation")),
        confirmation=(None if confirmation is None else _confirmation(confirmation, "confirmation")),
        spend_status=spend_status,
        measured_spend=(None if measured_spend is None else _budget(measured_spend, "measured_spend")),
    ))), selected_request)
    completed = _result_payload(preliminary)
    return _result_from_fields(completed, selected_request)


def validate_result(value: object, request: ReconciliationRequest) -> ReconciliationResult:
    """Validate and detach a result, bound to exactly one validated request."""

    selected_request = validate_request(request)
    if type(value) is ReconciliationResult:
        return _result_from_fields(_result_payload(value), selected_request)
    return _result_from_fields(value, selected_request)


def encode_frame(
    value: Union[ReconciliationRequest, ReconciliationResult], *,
    request: Optional[ReconciliationRequest] = None,
) -> bytes:
    """Encode exactly one canonical length-prefixed request or result object."""

    if type(value) is ReconciliationRequest:
        if request is not None:
            _refuse("reconciliation_protocol_type_invalid", "request frames cannot carry a result binding")
        payload = _request_payload(validate_request(value), semantic=False)
    elif type(value) is ReconciliationResult:
        if request is None:
            _refuse(
                "reconciliation_protocol_result_binding_required",
                "result frames require their exact request for validation",
            )
        payload = _result_payload(validate_result(value, request))
    else:
        _refuse("reconciliation_protocol_type_invalid", "frame value must be an exact request or result")
    encoded = _canonical(payload)
    if not encoded or len(encoded) > MAX_FRAME_BYTES:
        _refuse("reconciliation_protocol_frame_invalid", "encoded frame exceeds the fixed bound")
    return struct.pack(">I", len(encoded)) + encoded


def _decode(frame: bytes) -> dict[str, object]:
    if type(frame) is not bytes or len(frame) < 4:
        _refuse("reconciliation_protocol_frame_invalid", "frame must contain a four-byte length and payload")
    declared = struct.unpack(">I", frame[:4])[0]
    if declared == 0 or declared > MAX_FRAME_BYTES or len(frame) != 4 + declared:
        _refuse("reconciliation_protocol_frame_invalid", "frame length is invalid")
    payload = frame[4:]
    try:
        decoded = json.loads(payload, object_pairs_hook=_exact_object)
    except _DuplicateJSONKey as exc:
        raise ProtocolRefusal(
            "reconciliation_protocol_duplicate_key", "frame repeats a JSON member",
        ) from exc
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolRefusal(
            "reconciliation_protocol_frame_invalid", "frame is not UTF-8 JSON",
        ) from exc
    if type(decoded) is not dict:
        _refuse("reconciliation_protocol_frame_invalid", "frame payload must be one object")
    if _canonical(decoded) != payload:
        _refuse("reconciliation_protocol_noncanonical", "frame JSON is not canonical")
    return decoded


def decode_request_frame(frame: bytes) -> ReconciliationRequest:
    """Decode one exact canonical request frame."""

    return _request_from_fields(_decode(frame), wire=True)


def decode_result_frame(frame: bytes, request: ReconciliationRequest) -> ReconciliationResult:
    """Decode one exact canonical result frame bound to ``request``."""

    return validate_result(_decode(frame), request)


__all__ = [
    "ADAPTERS", "MAX_FRAME_BYTES", "OUTCOMES", "REASON_CODES", "REQUEST_FIELDS",
    "RESULT_FIELDS", "SPEND_STATUSES", "ReconciliationRequest", "ReconciliationResult",
    "build_request", "build_result", "decode_request_frame", "decode_result_frame",
    "encode_frame", "validate_request", "validate_result",
]

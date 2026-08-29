"""Optional bounded host-local sequencer for canonical run appends."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import selectors
import socket
import stat
import struct
import threading
from collections import OrderedDict, deque
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Deque, Dict, List, Optional, Tuple

from .errors import DurabilityFailure, IntegrityFailure, ProtocolRefusal
from .root import FloatiRoot, validate_identifier
from .run_segments import SegmentConfig
from .runtruth import (
    ADMISSION_BINDING_KINDS,
    ATTEMPT_KINDS,
    CANCELLATION_KINDS,
    CAPABILITY_BINDING_KINDS,
    RUN_KINDS,
    SPAWN_GROUP_KINDS,
    SUSPENSION_KINDS,
    SUPERVISOR_KINDS,
    RunLedger,
)
from .sequencer_epoch import ManagedWriterLease


PROTOCOL_VERSION = 1
# One closed spawn-create request may carry eight maximally bounded child
# contracts plus the approved 8,192-edge table.  The measured worst-case
# canonical shape is below the next power-of-two bound of 64 MiB.
MAX_FRAME_BYTES = 64 * 1024 * 1024
SOCKET_READ_BYTES = 64 * 1024
MAX_CLIENTS = 1024
# One client may reach the full frame ceiling while every other accepted client
# holds at most one read quantum.  This keeps aggregate partial-frame storage
# below 128 MiB without reducing the approved connection or fairness bounds.
MAX_REQUEST_BUFFER_BYTES = (
    MAX_FRAME_BYTES + (MAX_CLIENTS - 1) * SOCKET_READ_BYTES
)
MAX_BATCH = 64
MAX_RESPONSE_CACHE = 4096
_APPEND_REQUEST_FIELDS = frozenset(
    {
        "protocol_version",
        "operation",
        "operation_id",
        "sequencer_epoch",
        "client_id",
        "record",
    }
)
_INTENT_REQUEST_FIELDS = frozenset(
    {
        "protocol_version",
        "operation",
        "operation_id",
        "sequencer_epoch",
        "client_id",
        "intent",
    }
)
_INTENT_OPERATIONS = {
    "scheduler_intent": ATTEMPT_KINDS,
    "cancellation_intent": CANCELLATION_KINDS,
    "supervisor_intent": SUPERVISOR_KINDS,
    "admission_binding_intent": ADMISSION_BINDING_KINDS,
    "capability_binding_intent": CAPABILITY_BINDING_KINDS,
    "capability_dispatch_intent": frozenset({"dispatch_decision"}),
}
_EVALUATED_INTENT_FIELDS = {
    "admission_binding_evaluation": frozenset(
        {"run_id", "plan", "policy"}
    ),
    "capability_binding_evaluation": frozenset(
        {
            "run_id",
            "item_id",
            "attempt_id",
            "chosen_worker",
            "worker_profile",
            "policy",
            "routing_rank",
        }
    ),
    "suspension_evaluation": frozenset(
        {
            "run_id",
            "item_id",
            "attempt_id",
            "approval_request_id",
            "adapter",
            "resume_mode",
            "provider_session_or_thread_id",
            "workspace_checkpoint",
            "execution_authority_subject",
            "execution_authority_holder",
            "execution_authority_epoch",
        }
    ),
    "approval_resume_evaluation": frozenset(
        {
            "run_id",
            "item_id",
            "attempt_id",
            "approval_decision_id",
            "workspace_checkpoint",
            "resume_authority_subject",
            "resume_authority_holder",
            "resume_authority_epoch",
        }
    ),
    "spawn_admission_enable_evaluation": frozenset(
        {"run_id", "base_plan", "policy"}
    ),
    "spawn_policy_bind_evaluation": frozenset(
        {
            "run_id", "parent_item_id", "parent_attempt_id",
            "parent_capability_set_bound_id", "adapter", "subagents_mode",
            "max_children", "max_depth", "child_capability_ceiling",
            "spawn_budget_ceiling", "workspace_policies", "policy",
        }
    ),
    "spawn_group_create_evaluation": frozenset(
        {
            "run_id", "parent_item_id", "parent_attempt_id",
            "parent_fence_token", "group_key", "children", "dependency_edges",
            "max_children", "max_depth", "child_capability_ceiling",
            "aggregate_budget", "workspace_policy", "deadline", "join_mode",
            "required_count", "on_late_result", "on_child_failure",
            "cancel_remaining_after_success", "policy",
        }
    ),
    "spawn_group_abort_evaluation": frozenset(
        {
            "run_id", "spawn_group_id", "reason_code",
            "cancel_scope_resolved_id", "operator_id", "authority_subject",
            "authority_epoch", "capability_record_id", "policy",
        }
    ),
    "spawn_child_admission_evaluation": frozenset(
        {"run_id", "spawn_group_id", "child_item_id", "outcome", "reason_code", "policy"}
    ),
    "spawn_group_close_evaluation": frozenset(
        {"run_id", "spawn_group_id", "cancel_scope_resolved_id", "outcome", "policy"}
    ),
    "spawn_late_result_disposition_evaluation": frozenset(
        {
            "run_id", "spawn_group_id", "child_item_id", "result_record_id",
            "disposition", "operator_id", "authority_subject", "authority_epoch",
            "capability_record_id", "policy",
        }
    ),
}
_SPAWN_EVALUATED_OPERATIONS = frozenset({
    "spawn_admission_enable_evaluation",
    "spawn_policy_bind_evaluation",
    "spawn_group_create_evaluation",
    "spawn_group_abort_evaluation",
    "spawn_child_admission_evaluation",
    "spawn_group_close_evaluation",
    "spawn_late_result_disposition_evaluation",
})
_EVALUATED_PREFIXES = {
    "admission_binding_evaluation": "admission-evaluation-",
    "capability_binding_evaluation": "capability-evaluation-",
    "suspension_evaluation": "suspension-evaluation-",
    "approval_resume_evaluation": "approval-resume-evaluation-",
    "spawn_admission_enable_evaluation": "spawn-admission-enable-evaluation-",
    "spawn_policy_bind_evaluation": "spawn-policy-bind-evaluation-",
    "spawn_group_create_evaluation": "spawn-group-create-evaluation-",
    "spawn_group_abort_evaluation": "spawn-group-abort-evaluation-",
    "spawn_child_admission_evaluation": "spawn-child-admission-evaluation-",
    "spawn_group_close_evaluation": "spawn-group-close-evaluation-",
    "spawn_late_result_disposition_evaluation": "spawn-late-result-disposition-evaluation-",
}
_PRIVATE_KINDS = (
    ATTEMPT_KINDS
    | CANCELLATION_KINDS
    | SUPERVISOR_KINDS
    | CAPABILITY_BINDING_KINDS
    | ADMISSION_BINDING_KINDS
    | SUSPENSION_KINDS
    | SPAWN_GROUP_KINDS
)

_UUID7_HEX = r"[0-9a-f]{12}7[0-9a-f]{3}[89ab][0-9a-f]{15}"
_IDENTIFIER_RE = re.compile(r"^[a-z0-9](?:[a-z0-9_-]{0,62}[a-z0-9])?$")
_CAPABILITY_RE = re.compile(r"^[a-z0-9](?:[a-z0-9._-]{0,126}[a-z0-9])?$")


def _intent_refuse(detail: str, runtime_detail: Optional[str] = None) -> None:
    raise ProtocolRefusal(
        "intent_fields_invalid",
        detail if runtime_detail is None else runtime_detail,
    )


def _intent_string(
    value: object, field: str, *, nullable: bool = False, maximum: int = 4096
):
    if nullable and value is None:
        return None
    if (
        not isinstance(value, str)
        or not value
        or "\x00" in value
        or "\n" in value
        or "\r" in value
        or len(value) > maximum
    ):
        _intent_refuse(field + " must be an exact bounded string")
    return value


def _intent_pattern(
    value: object, field: str, pattern: str | re.Pattern[str], *, nullable: bool = False
):
    if nullable and value is None:
        return None
    normalized = _intent_string(value, field)
    if re.fullmatch(pattern, normalized) is None:
        _intent_refuse(field + " is outside its closed lexical domain")
    return normalized


def _intent_integer(
    value: object, field: str, minimum: int, maximum: int, *, nullable: bool = False
):
    if nullable and value is None:
        return None
    if isinstance(value, bool):
        _intent_refuse(field + " must be an integer")
    if isinstance(value, int):
        normalized = value
    elif isinstance(value, float) and math.isfinite(value) and value.is_integer():
        normalized = int(value)
    else:
        _intent_refuse(field + " must be an integer")
    if not minimum <= normalized <= maximum:
        _intent_refuse(field + " is outside its closed bounds")
    return normalized


def _intent_string_list(value: object, field: str, maximum: int = 64) -> list[str]:
    if not isinstance(value, list) or len(value) > maximum:
        _intent_refuse(field + " must be a bounded array")
    normalized = [_intent_string(row, field, maximum=128) for row in value]
    if len(set(normalized)) != len(normalized):
        _intent_refuse(field + " must be unique")
    return normalized


def _intent_budget_list(value: object, field: str) -> list[Dict[str, object]]:
    if not isinstance(value, list) or len(value) > 64:
        _intent_refuse(field + " must be a bounded array")
    rows = []
    budget_ids = set()
    for raw in value:
        if not isinstance(raw, dict) or set(raw) != {"budget_id", "amount"}:
            _intent_refuse(field + " entries must be closed objects")
        budget_id = _intent_pattern(raw["budget_id"], "budget_id", _IDENTIFIER_RE)
        if budget_id in budget_ids:
            _intent_refuse(field + " budget identities must be unique")
        budget_ids.add(budget_id)
        rows.append({
            "budget_id": budget_id,
            "amount": _intent_integer(raw["amount"], "amount", 1, 1_000_000_000),
        })
    return rows


def _canonical_spawn_intent(operation: str, intent: Dict[str, object]) -> Dict[str, object]:
    from .policy import _build_policy

    normalized = deepcopy(intent)
    try:
        policy = _build_policy(normalized["policy"])
    except (ProtocolRefusal, TypeError, ValueError, KeyError) as exc:
        raise ProtocolRefusal(
            "intent_fields_invalid", "spawn policy evidence must be exact"
        ) from exc
    normalized["policy"] = _policy_evidence(policy)

    string_fields = {
        "spawn_admission_enable_evaluation": ("run_id",),
        "spawn_policy_bind_evaluation": (
            "run_id", "parent_item_id", "parent_attempt_id",
            "parent_capability_set_bound_id", "adapter", "subagents_mode",
        ),
        "spawn_group_create_evaluation": (
            "run_id", "parent_item_id", "parent_attempt_id", "parent_fence_token",
            "group_key", "workspace_policy", "deadline", "join_mode",
            "on_late_result", "on_child_failure",
        ),
        "spawn_group_abort_evaluation": ("run_id", "spawn_group_id", "reason_code"),
        "spawn_child_admission_evaluation": (
            "run_id", "spawn_group_id", "child_item_id", "outcome",
        ),
        "spawn_group_close_evaluation": ("run_id", "spawn_group_id"),
        "spawn_late_result_disposition_evaluation": (
            "run_id", "spawn_group_id", "child_item_id", "result_record_id",
            "disposition",
        ),
    }[operation]
    for field in string_fields:
        normalized[field] = _intent_string(normalized[field], field)

    optional_strings = {
        "spawn_group_abort_evaluation": (
            "cancel_scope_resolved_id", "operator_id", "authority_subject",
            "capability_record_id",
        ),
        "spawn_child_admission_evaluation": ("reason_code",),
        "spawn_group_close_evaluation": ("cancel_scope_resolved_id", "outcome"),
        "spawn_late_result_disposition_evaluation": (
            "operator_id", "authority_subject", "capability_record_id",
        ),
    }.get(operation, ())
    for field in optional_strings:
        normalized[field] = _intent_string(normalized[field], field, nullable=True)

    lexical_fields = {
        "run_id": rf"run-{_UUID7_HEX}",
        "parent_item_id": rf"work-{_UUID7_HEX}",
        "child_item_id": rf"work-{_UUID7_HEX}",
        "parent_attempt_id": rf"attempt-{_UUID7_HEX}",
        "parent_capability_set_bound_id": rf"capability-set-bound-{_UUID7_HEX}",
        "parent_fence_token": r"[0-9a-f]{64}",
        "spawn_group_id": rf"spawn-group-created-{_UUID7_HEX}",
        "cancel_scope_resolved_id": rf"cancel-scope-resolved-{_UUID7_HEX}",
        "capability_record_id": rf"capability-{_UUID7_HEX}",
        "result_record_id": rf"run-result-(?:produced|verified|accepted)-{_UUID7_HEX}",
        "adopted_item_id": rf"work-{_UUID7_HEX}",
    }
    for field, pattern in lexical_fields.items():
        if field in normalized:
            normalized[field] = _intent_pattern(
                normalized[field], field, pattern,
                nullable=field in optional_strings,
            )
    for field in ("operator_id", "authority_subject"):
        if field in normalized:
            normalized[field] = _intent_pattern(
                normalized[field], field, _IDENTIFIER_RE, nullable=True,
            )

    if operation == "spawn_admission_enable_evaluation":
        from .admission import AdmissionPlan

        try:
            normalized["base_plan"] = AdmissionPlan.from_canonical(
                normalized["base_plan"]
            ).canonical()
        except (ProtocolRefusal, TypeError, ValueError, KeyError) as exc:
            raise ProtocolRefusal(
                "intent_fields_invalid", "base plan must be a closed canonical plan"
            ) from exc
    elif operation == "spawn_policy_bind_evaluation":
        from .records import _budget_rows, _capability_set, _workspace_policies

        normalized["max_children"] = _intent_integer(
            normalized["max_children"], "max_children", 0, 8
        )
        normalized["max_depth"] = _intent_integer(
            normalized["max_depth"], "max_depth", 0, 16
        )
        normalized["child_capability_ceiling"] = _intent_string_list(
            normalized["child_capability_ceiling"], "child_capability_ceiling"
        )
        if any(
            _CAPABILITY_RE.fullmatch(value) is None
            for value in normalized["child_capability_ceiling"]
        ):
            _intent_refuse("child_capability_ceiling has an invalid capability")
        normalized["spawn_budget_ceiling"] = _intent_budget_list(
            normalized["spawn_budget_ceiling"], "spawn_budget_ceiling"
        )
        normalized["workspace_policies"] = _intent_string_list(
            normalized["workspace_policies"], "workspace_policies", maximum=2
        )
        normalized["adapter"] = _intent_pattern(
            normalized["adapter"], "adapter", _CAPABILITY_RE,
        )
        if normalized["subagents_mode"] not in {"disabled", "observed_only", "managed"}:
            _intent_refuse("subagents_mode is outside the closed set")
        if any(
            value not in {"patch_only", "isolated_worktree"}
            for value in normalized["workspace_policies"]
        ):
            _intent_refuse("workspace_policies are outside the closed set")
        if normalized["subagents_mode"] != "managed" and any((
            normalized["max_children"], normalized["max_depth"],
            normalized["child_capability_ceiling"],
            normalized["spawn_budget_ceiling"],
            normalized["workspace_policies"],
        )):
            _intent_refuse("non-managed mode requires zero spawn authority")
        _capability_set(
            normalized["child_capability_ceiling"],
            "child_capability_ceiling", _intent_refuse,
        )
        _budget_rows(
            normalized["spawn_budget_ceiling"],
            "spawn_budget_ceiling", _intent_refuse,
        )
        _workspace_policies(
            normalized["workspace_policies"],
            "workspace_policies", _intent_refuse,
        )
    elif operation == "spawn_group_create_evaluation":
        from .records import (
            _budget_rows,
            _capability_set,
            _spawn_children,
            _spawn_dependency_edges,
            _timestamp_value,
        )
        from .spawn_groups import _normalize_spawn_group_numbers

        normalized = _normalize_spawn_group_numbers(normalized)

        normalized["max_children"] = _intent_integer(
            normalized["max_children"], "max_children", 1, 8
        )
        normalized["max_depth"] = _intent_integer(
            normalized["max_depth"], "max_depth", 1, 16
        )
        normalized["required_count"] = _intent_integer(
            normalized["required_count"], "required_count", 1, 8, nullable=True
        )
        if not isinstance(normalized["cancel_remaining_after_success"], bool):
            _intent_refuse("cancel_remaining_after_success must be boolean")
        normalized["child_capability_ceiling"] = _intent_string_list(
            normalized["child_capability_ceiling"], "child_capability_ceiling"
        )
        if any(
            _CAPABILITY_RE.fullmatch(value) is None
            for value in normalized["child_capability_ceiling"]
        ):
            _intent_refuse("child_capability_ceiling has an invalid capability")
        normalized["aggregate_budget"] = _intent_budget_list(
            normalized["aggregate_budget"], "aggregate_budget"
        )
        normalized["group_key"] = _intent_pattern(
            normalized["group_key"], "group_key", _IDENTIFIER_RE,
        )
        if normalized["workspace_policy"] not in {"patch_only", "isolated_worktree"}:
            _intent_refuse("workspace_policy is outside the closed set")
        if normalized["join_mode"] not in {
            "all_accepted", "all_terminal", "quorum", "first_accepted"
        }:
            _intent_refuse("join_mode is outside the closed set")
        if normalized["join_mode"] in {"all_accepted", "all_terminal"}:
            if normalized["required_count"] is not None:
                _intent_refuse("join mode requires null required_count")
        elif normalized["join_mode"] == "first_accepted":
            if normalized["required_count"] != 1:
                _intent_refuse("first_accepted requires required_count one")
        elif (
            normalized["required_count"] is None
            or normalized["required_count"] > normalized["max_children"]
        ):
            _intent_refuse("quorum required_count exceeds max_children")
        if normalized["on_late_result"] not in {"quarantine", "operator_decision"}:
            _intent_refuse("on_late_result is outside the closed set")
        if normalized["on_child_failure"] not in {
            "fail_group", "continue_until_join_impossible"
        }:
            _intent_refuse("on_child_failure is outside the closed set")
        _timestamp_value(normalized["deadline"], "deadline", _intent_refuse)
        children = normalized["children"]
        if not isinstance(children, list) or not 1 <= len(children) <= 8:
            _intent_refuse("children must be a bounded nonempty array")
        child_fields = {
            "item_id", "task_contract_id", "task_contract", "task_contract_digest",
            "depth", "budget_allocation", "capability_ceiling", "workspace_policy",
            "workspace_key", "concurrency_key", "capability_selector",
            "requires_cancellation", "requires_callback", "retry_class",
            "effect_safety", "merge_gate",
        }
        from .contracts import TaskContract, contract_digest

        canonical_children = []
        for raw in children:
            if not isinstance(raw, dict) or set(raw) != child_fields:
                _intent_refuse("child descriptors must use exact fields")
            child = deepcopy(raw)
            for field in (
                "item_id", "task_contract_id", "task_contract_digest",
                "workspace_policy", "workspace_key", "concurrency_key",
                "capability_selector", "retry_class", "effect_safety",
            ):
                child[field] = _intent_string(child[field], field)
            child["merge_gate"] = _intent_string(
                child["merge_gate"], "merge_gate", nullable=True
            )
            child["depth"] = _intent_integer(child["depth"], "depth", 1, 16)
            for field in ("requires_cancellation", "requires_callback"):
                if not isinstance(child[field], bool):
                    _intent_refuse(field + " must be boolean")
            child["capability_ceiling"] = _intent_string_list(
                child["capability_ceiling"], "capability_ceiling"
            )
            if any(
                _CAPABILITY_RE.fullmatch(value) is None
                for value in child["capability_ceiling"]
            ):
                _intent_refuse("capability_ceiling has an invalid capability")
            child["budget_allocation"] = _intent_budget_list(
                child["budget_allocation"], "budget_allocation"
            )
            if child["workspace_policy"] not in {"patch_only", "isolated_worktree"}:
                _intent_refuse("child workspace_policy is outside the closed set")
            child["item_id"] = _intent_pattern(
                child["item_id"], "item_id", rf"work-{_UUID7_HEX}",
            )
            child["task_contract_id"] = _intent_pattern(
                child["task_contract_id"], "task_contract_id",
                rf"task-contract-{_UUID7_HEX}",
            )
            child["task_contract_digest"] = _intent_pattern(
                child["task_contract_digest"], "task_contract_digest", r"[0-9a-f]{64}",
            )
            for field in (
                "workspace_key", "concurrency_key", "capability_selector",
                "retry_class",
            ):
                child[field] = _intent_pattern(child[field], field, _IDENTIFIER_RE)
            child["merge_gate"] = _intent_pattern(
                child["merge_gate"], "merge_gate", _IDENTIFIER_RE, nullable=True,
            )
            if child["effect_safety"] not in {
                "idempotent", "non_idempotent", "unknown_effect"
            }:
                _intent_refuse("effect_safety is outside the closed set")
            try:
                child["task_contract"] = TaskContract.create(
                    **child["task_contract"]
                ).canonical()
            except (ProtocolRefusal, TypeError, ValueError, KeyError) as exc:
                raise ProtocolRefusal(
                    "intent_fields_invalid", "task contract must be canonical"
                ) from exc
            if contract_digest(child["task_contract"]) != child["task_contract_digest"]:
                _intent_refuse("task_contract_digest must cover the canonical contract")
            canonical_children.append(child)
        normalized["children"] = canonical_children
        _capability_set(
            normalized["child_capability_ceiling"],
            "child_capability_ceiling", _intent_refuse,
        )
        _budget_rows(
            normalized["aggregate_budget"],
            "aggregate_budget", _intent_refuse,
        )
        _spawn_children(normalized["children"], _intent_refuse)
        if len(canonical_children) > normalized["max_children"]:
            _intent_refuse("children exceed max_children")
        if normalized["required_count"] is not None and (
            normalized["required_count"] > len(canonical_children)
        ):
            _intent_refuse("required_count exceeds immutable membership")
        edges = normalized["dependency_edges"]
        if not isinstance(edges, list) or len(edges) > 8192:
            _intent_refuse("dependency_edges must be a bounded array")
        for edge in edges:
            if not isinstance(edge, dict) or set(edge) != {
                "source", "target", "requires", "failure_policy"
            }:
                _intent_refuse("dependency edges must be closed objects")
            for field in edge:
                edge[field] = _intent_string(edge[field], field)
            edge["source"] = _intent_pattern(
                edge["source"], "source", rf"work-{_UUID7_HEX}",
            )
            edge["target"] = _intent_pattern(
                edge["target"], "target", rf"work-{_UUID7_HEX}",
            )
            if edge["requires"] not in {"produced", "verified", "accepted"}:
                _intent_refuse("edge requires is outside the closed set")
            if edge["failure_policy"] not in {
                "fail_run", "skip_dependent", "continue"
            }:
                _intent_refuse("edge failure_policy is outside the closed set")
        _spawn_dependency_edges(edges, _intent_refuse)
    if operation in {"spawn_group_abort_evaluation", "spawn_late_result_disposition_evaluation"}:
        normalized["authority_epoch"] = _intent_integer(
            normalized["authority_epoch"], "authority_epoch", 1, 2**63 - 1,
            nullable=True,
        )
    if operation == "spawn_group_abort_evaluation":
        if normalized["reason_code"] not in {"cancellation", "operator_abandonment"}:
            _intent_refuse("abort reason is outside the closed set")
        operator = (
            normalized["operator_id"], normalized["authority_subject"],
            normalized["authority_epoch"], normalized["capability_record_id"],
        )
        if normalized["reason_code"] == "cancellation":
            if normalized["cancel_scope_resolved_id"] is None or any(
                value is not None for value in operator
            ):
                _intent_refuse("cancellation abort has conflicting authority")
        elif normalized["cancel_scope_resolved_id"] is not None or any(
            value is None for value in operator
        ):
            _intent_refuse("operator abort requires complete operator authority")
    if operation == "spawn_child_admission_evaluation" and normalized["outcome"] not in {"admit", "reject"}:
        _intent_refuse("child outcome is outside the closed set")
    if operation == "spawn_child_admission_evaluation":
        if normalized["outcome"] == "admit" and normalized["reason_code"] is not None:
            _intent_refuse("admission outcome forbids rejection reason")
        if normalized["reason_code"] is not None and normalized["reason_code"] not in {
            "item_limit", "fanout_limit", "depth_limit", "budget_refusal",
            "capability_refusal", "workspace_refusal", "deadline_expired",
            "policy_refusal", "admission_binding_refusal",
        }:
            _intent_refuse("rejection reason is outside the closed set")
    if operation == "spawn_group_close_evaluation" and normalized["outcome"] not in {
        None, "satisfied", "failed", "cancelled", "deadline", "needs_operator"
    }:
        _intent_refuse("close outcome is outside the closed set")
    if operation == "spawn_late_result_disposition_evaluation":
        if normalized["disposition"] not in {
            "quarantine", "retain_as_non_join_evidence"
        }:
            _intent_refuse("late disposition is outside the closed set")
        if any(
            normalized[field] is None
            for field in (
                "operator_id", "authority_subject", "authority_epoch",
                "capability_record_id",
            )
        ):
            _intent_refuse("late disposition requires complete operator authority")
    return normalized


def _canonical_evaluated_intent(
    operation: str, raw: object
) -> Dict[str, object]:
    expected = _EVALUATED_INTENT_FIELDS[operation]
    if not isinstance(raw, dict) or set(raw) != expected:
        raise ProtocolRefusal(
            "intent_fields_invalid", "evaluated intent fields must match exactly"
        )
    intent = dict(raw)
    if operation in _SPAWN_EVALUATED_OPERATIONS:
        return _canonical_spawn_intent(operation, intent)
    if operation not in {
        "suspension_evaluation",
        "approval_resume_evaluation",
    }:
        return intent

    string_fields = (
        (
            "run_id",
            "item_id",
            "attempt_id",
            "approval_request_id",
            "adapter",
            "resume_mode",
            "execution_authority_subject",
            "execution_authority_holder",
        )
        if operation == "suspension_evaluation"
        else (
            "run_id",
            "item_id",
            "attempt_id",
            "approval_decision_id",
            "resume_authority_subject",
            "resume_authority_holder",
        )
    )
    if any(not isinstance(intent[field], str) for field in string_fields):
        raise ProtocolRefusal(
            "intent_fields_invalid", "semantic intent strings must be exact"
        )
    if operation == "suspension_evaluation" and not (
        intent["provider_session_or_thread_id"] is None
        or isinstance(intent["provider_session_or_thread_id"], str)
    ):
        raise ProtocolRefusal(
            "intent_fields_invalid", "provider identity must be a string or null"
        )
    checkpoint = intent["workspace_checkpoint"]
    if (
        not isinstance(checkpoint, dict)
        or set(checkpoint) != {"repo", "sha", "doc"}
        or any(not isinstance(value, str) for value in checkpoint.values())
    ):
        raise ProtocolRefusal(
            "intent_fields_invalid", "workspace checkpoint must be a closed string object"
        )
    epoch_field = (
        "execution_authority_epoch"
        if operation == "suspension_evaluation"
        else "resume_authority_epoch"
    )
    epoch = intent[epoch_field]
    if isinstance(epoch, bool):
        normalized_epoch = None
    elif isinstance(epoch, int):
        normalized_epoch = epoch
    elif isinstance(epoch, float) and math.isfinite(epoch) and epoch.is_integer():
        normalized_epoch = int(epoch)
    else:
        normalized_epoch = None
    if normalized_epoch is None or not 1 <= normalized_epoch <= 2**63 - 1:
        raise ProtocolRefusal(
            "intent_fields_invalid", "authority epoch must be a positive integer"
        )
    intent[epoch_field] = normalized_epoch
    return intent


def sequencer_socket_path(root: FloatiRoot) -> Path:
    """Return one deterministic short endpoint for this exact tenant home."""

    if not isinstance(root, FloatiRoot):
        raise ProtocolRefusal("root_required", "sequencer requires a validated root")
    identity = hashlib.sha256(str(root.tenant_home).encode("utf-8")).hexdigest()[:32]
    return Path("/tmp") / ("slipway-sequencer-" + identity) / "sequencer.sock"


@dataclass(frozen=True)
class SequencerConfig:
    max_clients: int = MAX_CLIENTS
    max_request_buffer_bytes: int = MAX_REQUEST_BUFFER_BYTES
    max_batch: int = MAX_BATCH
    response_cache_size: int = MAX_RESPONSE_CACHE
    select_timeout: float = 0.1
    takeover: bool = False

    def __post_init__(self) -> None:
        _bounded_integer(self.max_clients, 1, MAX_CLIENTS, "max_clients")
        _bounded_integer(
            self.max_request_buffer_bytes,
            1,
            MAX_REQUEST_BUFFER_BYTES,
            "max_request_buffer_bytes",
        )
        _bounded_integer(self.max_batch, 1, MAX_BATCH, "max_batch")
        _bounded_integer(
            self.response_cache_size, 1, MAX_RESPONSE_CACHE, "response_cache_size"
        )
        if (
            not isinstance(self.select_timeout, (int, float))
            or isinstance(self.select_timeout, bool)
            or not 0 <= float(self.select_timeout) <= 1
        ):
            raise ProtocolRefusal(
                "sequencer_config_invalid", "select timeout is outside local service bounds"
            )
        if not isinstance(self.takeover, bool):
            raise ProtocolRefusal(
                "sequencer_config_invalid", "takeover must be an exact boolean"
            )


@dataclass
class _Connection:
    channel: socket.socket
    buffer: bytearray
    buffered_bytes: int = 0


@dataclass(frozen=True)
class _Pending:
    channel: socket.socket
    request: Dict[str, object]


def _bounded_integer(value: object, minimum: int, maximum: int, field: str) -> int:
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or not minimum <= value <= maximum
    ):
        raise ProtocolRefusal(
            "sequencer_config_invalid", field + " is outside local service bounds"
        )
    return value


def _refused(code: str, detail: str) -> Dict[str, object]:
    return {"status": "refused", "code": code, "detail": detail}


def _duplicate_pairs(pairs: List[Tuple[str, object]]) -> Dict[str, object]:
    result: Dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ProtocolRefusal(
                "duplicate_json_key", "request JSON contains a duplicate object member"
            )
        result[key] = value
    return result


def _reject_constant(_value: str) -> object:
    raise ProtocolRefusal("frame_not_ijson", "request numbers must be finite I-JSON")


def _validate_ijson(value: object) -> None:
    if isinstance(value, bool) or value is None:
        return
    if isinstance(value, int):
        if abs(value) > 9007199254740991:
            raise ProtocolRefusal(
                "frame_not_ijson", "request integer exceeds the interoperable I-JSON range"
            )
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ProtocolRefusal(
                "frame_not_ijson", "request numbers must be finite I-JSON"
            )
        return
    if isinstance(value, str):
        if any(0xD800 <= ord(character) <= 0xDFFF for character in value):
            raise ProtocolRefusal(
                "frame_not_ijson", "request text contains a Unicode surrogate"
            )
        return
    if isinstance(value, list):
        for item in value:
            _validate_ijson(item)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            _validate_ijson(key)
            _validate_ijson(item)
        return
    raise ProtocolRefusal("frame_not_ijson", "request contains a non-I-JSON value")


def _decode_request(frame: bytes) -> Dict[str, object]:
    if len(frame) > MAX_FRAME_BYTES:
        raise ProtocolRefusal(
            "frame_too_large",
            f"request frame exceeds {MAX_FRAME_BYTES} bytes",
        )
    if not frame.endswith(b"\n"):
        raise ProtocolRefusal(
            "incomplete_frame", "request must be one newline-terminated JSON object"
        )
    try:
        text = frame[:-1].decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ProtocolRefusal("frame_not_utf8", "request must be valid UTF-8") from exc
    try:
        value = json.loads(
            text,
            object_pairs_hook=_duplicate_pairs,
            parse_constant=_reject_constant,
        )
    except ProtocolRefusal:
        raise
    except (json.JSONDecodeError, RecursionError, UnicodeError) as exc:
        raise ProtocolRefusal("frame_not_json", "request must be one JSON object") from exc
    if not isinstance(value, dict):
        raise ProtocolRefusal("frame_not_object", "request must be one JSON object")
    _validate_ijson(value)
    return value


def _encode_frame(value: Dict[str, object]) -> bytes:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8") + b"\n"
    except (TypeError, ValueError, UnicodeError) as exc:
        raise ProtocolRefusal("frame_not_ijson", "request cannot form I-JSON") from exc
    if len(encoded) > MAX_FRAME_BYTES:
        raise ProtocolRefusal(
            "frame_too_large",
            f"request frame exceeds {MAX_FRAME_BYTES} bytes",
        )
    return encoded


def _policy_evidence(policy: object) -> Dict[str, object]:
    canonical = policy.canonical()
    canonical["retry_classes"] = {
        key: {"automatic": value}
        for key, value in canonical["retry_classes"].items()
    }
    canonical["approval_requirements"] = {
        key: {"required": value}
        for key, value in canonical["approval_requirements"].items()
    }
    return canonical


def _semantic_uuid(operation: str, intent: Dict[str, object]) -> str:
    payload = json.dumps(
        {"operation": operation, "intent": intent},
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    digest = hashlib.sha256(payload).hexdigest()
    return digest[:12] + "7" + digest[13:16] + "8" + digest[17:32]


def _epoch_semantic_uuid(
    epoch_record_id: str, operation: str, intent: Dict[str, object]
) -> str:
    return _semantic_uuid(
        operation,
        {"epoch_record_id": epoch_record_id, "evaluated_intent": intent},
    )


def _known_service_record_id(
    root: FloatiRoot,
    operation: str,
    intent: Dict[str, object],
    record_id: str,
) -> bool:
    return record_id in _service_record_ids(root, operation, intent)


def _service_record_ids(
    root: FloatiRoot,
    operation: str,
    intent: Dict[str, object],
) -> set[str]:
    from .sequencer_epoch import SequencerEpochLedger

    prefix = (
        "run-admission-bound-"
        if operation == "admission_binding_evaluation"
        else "capability-set-bound-"
    )
    return {
        prefix + _epoch_semantic_uuid(str(record["id"]), operation, intent)
        for record in SequencerEpochLedger(root).records()
        if record["operation"] in {"entered", "takeover"}
    }


class SequencerClient:
    """One-request-per-connection retry-safe local sequencer client."""

    def __init__(
        self, socket_path: Path, epoch: int, client_id: str, *, timeout: float = 2.0
    ) -> None:
        self.socket_path = Path(socket_path)
        self.epoch = _bounded_integer(epoch, 1, 9007199254740991, "epoch")
        self.client_id = validate_identifier(client_id, "client_id")
        if (
            not isinstance(timeout, (int, float))
            or isinstance(timeout, bool)
            or not 0 < float(timeout) <= 30
        ):
            raise ProtocolRefusal("sequencer_timeout_invalid", "client timeout is bounded")
        self.timeout = float(timeout)

    def request(self, record: Dict[str, object]) -> Dict[str, object]:
        if not isinstance(record, dict):
            raise ProtocolRefusal("record_invalid", "append requires one record object")
        operation_id = record.get("id")
        if not isinstance(operation_id, str):
            raise ProtocolRefusal("operation_id_invalid", "record id is required")
        return {
            "protocol_version": PROTOCOL_VERSION,
            "operation": "append_run_record",
            "operation_id": operation_id,
            "sequencer_epoch": self.epoch,
            "client_id": self.client_id,
            "record": record,
        }

    def frame(self, record: Dict[str, object]) -> bytes:
        return _encode_frame(self.request(record))

    def intent_request(
        self, owner: str, record: Dict[str, object], policy: object = None
    ) -> Dict[str, object]:
        operations = {
            "scheduler": "scheduler_intent",
            "cancellation": "cancellation_intent",
            "supervisor": "supervisor_intent",
            "admission_binding": "admission_binding_intent",
            "capability_binding": "capability_binding_intent",
            "capability_dispatch": "capability_dispatch_intent",
        }
        operation = operations.get(owner)
        if operation is None or not isinstance(record, dict):
            raise ProtocolRefusal("intent_invalid", "typed sequencer intent is invalid")
        core = {"schema_version", "id", "tenant_id", "timestamp", "kind"}
        if not core <= set(record):
            raise ProtocolRefusal("intent_invalid", "typed intent record envelope is incomplete")
        intent: Dict[str, object] = {
            "schema_version": record["schema_version"],
            "kind": record["kind"],
            "timestamp": record["timestamp"],
            "fields": {key: value for key, value in record.items() if key not in core},
        }
        if policy is not None:
            canonical = getattr(policy, "canonical", None)
            if not callable(canonical):
                raise ProtocolRefusal(
                    "intent_policy_invalid", "capability dispatch intent requires policy"
                )
            intent["policy"] = _policy_evidence(policy)
        return {
            "protocol_version": PROTOCOL_VERSION,
            "operation": operation,
            "operation_id": record["id"],
            "sequencer_epoch": self.epoch,
            "client_id": self.client_id,
            "intent": intent,
        }

    def append_intent(
        self, owner: str, record: Dict[str, object], policy: object = None
    ) -> Dict[str, object]:
        if owner in _EVALUATED_INTENT_FIELDS:
            if policy is not None or not isinstance(record, dict):
                raise ProtocolRefusal(
                    "intent_invalid", "evaluated operation requires one semantic intent"
                )
            intent = _canonical_evaluated_intent(owner, record)
            request = self._evaluation_request(
                owner,
                _EVALUATED_PREFIXES[owner] + _semantic_uuid(owner, intent),
                intent,
            )
            return self._exchange(_encode_frame(request))
        return self._exchange(_encode_frame(self.intent_request(owner, record, policy)))

    def append(self, record: Dict[str, object]) -> Dict[str, object]:
        return self._exchange(self.frame(record))

    def _evaluation_request(
        self, operation: str, operation_id: str, intent: Dict[str, object]
    ) -> Dict[str, object]:
        return {
            "protocol_version": PROTOCOL_VERSION,
            "operation": operation,
            "operation_id": operation_id,
            "sequencer_epoch": self.epoch,
            "client_id": self.client_id,
            "intent": intent,
        }

    def bind_admission(
        self, run_id: str, plan: object, policy: object, _timestamp: str
    ) -> Dict[str, object]:
        operation = "admission_binding_evaluation"
        intent = {
            "run_id": run_id,
            "plan": plan.canonical(),
            "policy": _policy_evidence(policy),
        }
        request = self._evaluation_request(
            operation, "admission-evaluation-" + _semantic_uuid(operation, intent), intent
        )
        return self._exchange(_encode_frame(request))

    def bind_capability(
        self,
        run_id: str,
        item_id: str,
        attempt_id: str,
        chosen_worker: str,
        worker_profile: str,
        policy: object,
        routing_rank: int,
        _timestamp: str,
    ) -> Dict[str, object]:
        operation = "capability_binding_evaluation"
        intent = {
            "run_id": run_id,
            "item_id": item_id,
            "attempt_id": attempt_id,
            "chosen_worker": chosen_worker,
            "worker_profile": worker_profile,
            "policy": _policy_evidence(policy),
            "routing_rank": routing_rank,
        }
        request = self._evaluation_request(
            operation, "capability-evaluation-" + _semantic_uuid(operation, intent), intent
        )
        return self._exchange(_encode_frame(request))

    def _exchange(self, payload: bytes) -> Dict[str, object]:
        data = bytearray()
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as channel:
                channel.settimeout(self.timeout)
                channel.connect(str(self.socket_path))
                channel.sendall(payload)
                while b"\n" not in data:
                    chunk = channel.recv(min(
                        SOCKET_READ_BYTES,
                        MAX_FRAME_BYTES + 1 - len(data),
                    ))
                    if not chunk:
                        break
                    data.extend(chunk)
                    if len(data) > MAX_FRAME_BYTES:
                        raise ProtocolRefusal(
                            "sequencer_response_invalid", "sequencer response exceeds bounds"
                        )
        except ProtocolRefusal:
            raise
        except (OSError, TimeoutError) as exc:
            raise ProtocolRefusal(
                "sequencer_unavailable", "local sequencer is unavailable"
            ) from exc
        if not data or b"\n" not in data:
            raise ProtocolRefusal(
                "sequencer_response_lost", "sequencer response was lost; exact retry is safe"
            )
        if data.find(b"\n") != len(data) - 1:
            raise ProtocolRefusal(
                "sequencer_response_invalid", "sequencer returned more than one frame"
            )
        response = _decode_response(bytes(data))
        if response["status"] == "refused":
            raise ProtocolRefusal(str(response["code"]), str(response["detail"]))
        return response


def _decode_response(frame: bytes) -> Dict[str, object]:
    try:
        value = json.loads(frame.decode("utf-8"), object_pairs_hook=_duplicate_pairs)
    except (UnicodeError, json.JSONDecodeError, ProtocolRefusal) as exc:
        raise ProtocolRefusal(
            "sequencer_response_invalid", "sequencer returned invalid JSON"
        ) from exc
    if not isinstance(value, dict) or value.get("status") not in {"ok", "refused"}:
        raise ProtocolRefusal(
            "sequencer_response_invalid", "sequencer returned an invalid response"
        )
    if value["status"] == "ok":
        expected = {"status", "record", "coordinate"}
    else:
        expected = {"status", "code", "detail"}
    if set(value) != expected:
        raise ProtocolRefusal(
            "sequencer_response_invalid", "sequencer response fields are invalid"
        )
    return value


class _ServiceOwnerSink:
    """In-process terminus for authority already proven by a service owner."""

    def __init__(self, service: "SequencerService", token: object = None) -> None:
        raise ProtocolRefusal(
            "evaluated_service_only",
            "evaluation sink is created only inside validated service evaluation",
        )

    def append(self, _record: Dict[str, object]) -> Dict[str, object]:
        raise ProtocolRefusal(
            "private_record_requires_intent",
            "service owner sink accepts only locally authorized intents",
        )

    def _append_authorized_intent(
        self,
        owner: str,
        record: Dict[str, object],
        owner_capability: object,
        policy: object,
    ) -> Dict[str, object]:
        if self.ledger is None:
            raise ProtocolRefusal(
                "sequencer_internal_error", "service owner sink is not attached"
            )
        appended = self.ledger._append_managed_owned(
            owner,
            record,
            self.service.epoch,
            self.service._lease.managed_append_capability,
            owner_capability,
            dispatch_policy=policy,
        )
        return {"record": appended}


class SequencerService:
    """Exclusive managed writer with bounded fair Unix-socket request queues."""

    def __init__(
        self,
        root: FloatiRoot,
        sequencer_id: str,
        config: SequencerConfig = SequencerConfig(),
        *,
        clock: object = None,
        segment_config: SegmentConfig = SegmentConfig(),
    ) -> None:
        if not isinstance(root, FloatiRoot):
            raise ProtocolRefusal("root_required", "sequencer requires a validated root")
        if not isinstance(config, SequencerConfig):
            raise ProtocolRefusal("sequencer_config_invalid", "sequencer config is required")
        self.root = root
        self.sequencer_id = validate_identifier(sequencer_id, "sequencer_id")
        self.config = config
        if clock is not None and not callable(clock):
            raise ProtocolRefusal("sequencer_clock_invalid", "service clock must be callable")
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self.socket_path = sequencer_socket_path(root)
        self._selector = selectors.DefaultSelector()
        self._listener: Optional[socket.socket] = None
        self._listener_registered = False
        self._connections: Dict[int, _Connection] = {}
        self._request_buffer_bytes = 0
        self._queues: Dict[str, Deque[_Pending]] = {}
        self._ready: Deque[str] = deque()
        self._ready_set = set()
        self._cache: "OrderedDict[str, Tuple[Dict[str, object], Dict[str, object]]]" = OrderedDict()
        self._closed = False
        self._state_lock = threading.RLock()
        self._close_guard = threading.Lock()
        self._ledger = RunLedger(root, segment_config=segment_config)
        self._lease = ManagedWriterLease(
            root, self.sequencer_id, takeover=config.takeover
        )
        self._lease.__enter__()
        self.epoch = self._lease.epoch
        try:
            self._open_socket()
        except BaseException:
            self._lease.__exit__(None, None, None)
            raise

    def _open_socket(self) -> None:
        runtime = self.socket_path.parent
        listener: Optional[socket.socket] = None
        try:
            runtime.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(runtime, 0o700)
            if self.socket_path.exists() or self.socket_path.is_symlink():
                metadata = os.lstat(self.socket_path)
                if not stat.S_ISSOCK(metadata.st_mode):
                    raise ProtocolRefusal(
                        "sequencer_socket_occupied",
                        "sequencer endpoint is occupied by a non-socket entry",
                    )
                self.socket_path.unlink()
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            listener.setblocking(False)
            listener.bind(str(self.socket_path))
            os.chmod(self.socket_path, 0o600)
            listener.listen(self.config.max_clients)
            self._listener = listener
            self._selector.register(listener, selectors.EVENT_READ, None)
            self._listener_registered = True
        except ProtocolRefusal:
            if listener is not None and listener is not self._listener:
                listener.close()
            raise
        except OSError as exc:
            if listener is not None and listener is not self._listener:
                listener.close()
            raise DurabilityFailure(
                "sequencer_socket_unavailable", "local sequencer endpoint is unavailable"
            ) from exc

    def serve_forever(self, stop_event: threading.Event) -> None:
        if not isinstance(stop_event, threading.Event):
            raise ProtocolRefusal("stop_event_invalid", "serve requires a threading event")
        try:
            while not stop_event.is_set() and not self._closed:
                self.serve_once()
        finally:
            self.close()

    def serve_once(self) -> int:
        with self._state_lock:
            if self._closed:
                return 0
            should_select = not self._ready
        if should_select:
            try:
                events = self._selector.select(float(self.config.select_timeout))
            except (OSError, ValueError):
                return 0
            self._handle_events(events)
            while True:
                with self._state_lock:
                    if self._closed or self._ready:
                        break
                try:
                    immediate = self._selector.select(0)
                except (OSError, ValueError):
                    break
                if not immediate:
                    break
                self._handle_events(immediate)
        batch: List[_Pending] = []
        with self._state_lock:
            while self._ready and len(batch) < self.config.max_batch:
                client_id = self._ready.popleft()
                self._ready_set.discard(client_id)
                queue = self._queues.get(client_id)
                if not queue:
                    self._queues.pop(client_id, None)
                    continue
                batch.append(queue.popleft())
                if queue:
                    self._mark_ready(client_id)
                else:
                    self._queues.pop(client_id, None)
        self._complete_batch(batch)
        return len(batch)

    def _complete_batch(self, batch: List[_Pending]) -> None:
        if len(batch) < 2 or not all(
            pending.request["operation"] == "append_run_record"
            for pending in batch
        ):
            for pending in batch:
                self._complete_pending(pending)
            return
        new: List[Tuple[_Pending, Dict[str, object]]] = []
        new_by_id: Dict[str, Dict[str, object]] = {}
        identical_retries: Dict[str, List[_Pending]] = {}
        immediate: List[Tuple[_Pending, Dict[str, object]]] = []
        for pending in batch:
            try:
                request = pending.request
                raw_record = request["record"]
                assert isinstance(raw_record, dict)
                kind = raw_record.get("kind")
                if kind in _PRIVATE_KINDS or (
                    kind == "dispatch_decision" and raw_record.get("schema_version") == 1
                ) or (
                    kind == "plan_amendment" and raw_record.get("schema_version") == 1
                ):
                    if kind in SPAWN_GROUP_KINDS or kind == "plan_amendment":
                        raise ProtocolRefusal(
                            "spawn_group_controller_only",
                            "spawn records require semantic service evaluation",
                        )
                    if kind in SUSPENSION_KINDS:
                        raise ProtocolRefusal(
                            "suspension_controller_only",
                            "approval suspension requires semantic service evaluation",
                        )
                    raise ProtocolRefusal(
                        "private_record_requires_intent",
                        "domain-owned records require a typed service intent",
                    )
                record = self._canonical_public_record(raw_record)
                operation_id = str(request["operation_id"])
                cached = self._cache.get(operation_id)
                if cached is not None:
                    prior, response = cached
                    if prior != record:
                        raise ProtocolRefusal(
                            "duplicate_record_id", "operation id has divergent payload"
                        )
                    if kind == "result_accepted":
                        response = self._canonical_public_retry_response(record)
                    immediate.append((pending, response))
                    continue
                located = self._ledger._store.lookup(operation_id)
                if located is not None:
                    if located.record != record:
                        raise ProtocolRefusal(
                            "duplicate_record_id", "operation id has divergent payload"
                        )
                    immediate.append(
                        (
                            pending,
                            self._canonical_public_retry_response(record)
                            if kind == "result_accepted"
                            else self._success(located.record, located.coordinate),
                        )
                    )
                    continue
                prior_new = new_by_id.get(operation_id)
                if prior_new is not None:
                    if prior_new == record:
                        identical_retries.setdefault(operation_id, []).append(pending)
                    else:
                        immediate.append(
                            (
                                pending,
                                _refused(
                                    "duplicate_record_id",
                                    "request refused by local sequencer",
                                ),
                            )
                        )
                    continue
                new_by_id[operation_id] = record
                new.append((pending, record))
            except (ProtocolRefusal, IntegrityFailure, DurabilityFailure) as exc:
                immediate.append(
                    (pending, _refused(exc.code, "request refused by local sequencer"))
                )
            except Exception:
                immediate.append(
                    (
                        pending,
                        _refused(
                            "sequencer_internal_error", "local sequencer refused request"
                        ),
                    )
                )
        if new:
            try:
                records = [record for _pending, record in new]
                appended = self._ledger._append_managed_batch(
                    records, self.epoch, self._lease.managed_append_capability
                )
                for (pending, _candidate), outcome in zip(new, appended):
                    if isinstance(outcome, (ProtocolRefusal, IntegrityFailure)):
                        response = _refused(
                            outcome.code, "request refused by local sequencer"
                        )
                        immediate.append((pending, response))
                        immediate.extend(
                            (retry, response)
                            for retry in identical_retries.get(
                                str(_candidate["id"]), ()
                            )
                        )
                        continue
                    record = outcome
                    assert isinstance(record, dict)
                    located = self._ledger._store.lookup(str(record["id"]))
                    if located is None:
                        raise IntegrityFailure(
                            "sequencer_coordinate_missing",
                            "committed record has no physical coordinate",
                        )
                    immediate.append(
                        (pending, self._success(record, located.coordinate))
                    )
                    response = immediate[-1][1]
                    immediate.extend(
                        (retry, response)
                        for retry in identical_retries.get(str(record["id"]), ())
                    )
            except (ProtocolRefusal, IntegrityFailure, DurabilityFailure) as exc:
                response = _refused(exc.code, "request refused by local sequencer")
                immediate.extend((pending, response) for pending, _record in new)
                immediate.extend(
                    (retry, response)
                    for _operation_id, retries in identical_retries.items()
                    for retry in retries
                )
            except Exception:
                response = _refused(
                    "sequencer_internal_error", "local sequencer refused request"
                )
                immediate.extend((pending, response) for pending, _record in new)
                immediate.extend(
                    (retry, response)
                    for _operation_id, retries in identical_retries.items()
                    for retry in retries
                )
        for pending, response in immediate:
            if response.get("status") == "ok":
                record = response["record"]
                assert isinstance(record, dict)
                self._remember(str(record["id"]), record, response)
            try:
                self._send_response(pending.channel, response)
            finally:
                self._finish(pending.channel)

    def _handle_events(self, events: List[Tuple[selectors.SelectorKey, int]]) -> None:
        for key, _mask in events:
            if key.data is None:
                self._accept_ready()
            else:
                self._read_ready(key.data)

    def _accept_ready(self) -> None:
        while True:
            with self._state_lock:
                listener = self._listener
                if listener is None or self._closed:
                    return
                if len(self._connections) >= self.config.max_clients:
                    self._suspend_listener()
                    return
            try:
                channel, _address = listener.accept()
            except BlockingIOError:
                return
            except OSError:
                return
            channel.setblocking(False)
            peer_uid = self._peer_uid(channel)
            if peer_uid is not None and peer_uid != os.geteuid():
                self._send_response(
                    channel,
                    _refused("peer_uid_mismatch", "peer must use the service owner UID"),
                )
                channel.close()
                continue
            connection = _Connection(channel, bytearray())
            accepted = False
            with self._state_lock:
                if not self._closed and self._listener is listener:
                    descriptor = channel.fileno()
                    self._connections[descriptor] = connection
                    try:
                        self._selector.register(
                            channel, selectors.EVENT_READ, connection
                        )
                    except Exception:
                        self._connections.pop(descriptor, None)
                    else:
                        accepted = True
                        if len(self._connections) >= self.config.max_clients:
                            self._suspend_listener()
            if not accepted:
                channel.close()
                with self._state_lock:
                    if self._closed:
                        return

    def _suspend_listener(self) -> None:
        with self._state_lock:
            if self._listener is None or not self._listener_registered:
                return
            try:
                self._selector.unregister(self._listener)
            except Exception:
                pass
            self._listener_registered = False

    def _resume_listener(self) -> None:
        with self._state_lock:
            if (
                self._listener is None
                or self._listener_registered
                or len(self._connections) >= self.config.max_clients
                or self._closed
            ):
                return
            try:
                self._selector.register(self._listener, selectors.EVENT_READ, None)
            except Exception:
                return
            self._listener_registered = True

    @staticmethod
    def _peer_uid(channel: socket.socket) -> Optional[int]:
        getpeereid = getattr(channel, "getpeereid", None)
        if getpeereid is not None:
            return int(getpeereid()[0])
        option = getattr(socket, "SO_PEERCRED", None)
        if option is not None:
            credentials = channel.getsockopt(socket.SOL_SOCKET, option, struct.calcsize("3i"))
            _pid, uid, _gid = struct.unpack("3i", credentials)
            return int(uid)
        return None

    def _read_ready(self, connection: _Connection) -> None:
        channel = connection.channel
        with self._state_lock:
            if self._closed or not self._connection_active(connection):
                return
            aggregate_credit = (
                self.config.max_request_buffer_bytes - self._request_buffer_bytes
            )
            read_size = min(
                SOCKET_READ_BYTES,
                MAX_FRAME_BYTES + 1 - len(connection.buffer),
                aggregate_credit,
            )
        if aggregate_credit <= 0:
            self._refuse_and_finish(
                channel,
                "request_buffer_overloaded",
                "local sequencer request buffer capacity is exhausted",
            )
            return
        try:
            chunk = channel.recv(read_size)
        except BlockingIOError:
            return
        except OSError:
            self._finish(channel)
            return
        if not chunk:
            self._finish(channel)
            return
        refusal: Optional[Tuple[str, str]] = None
        frame: Optional[bytes] = None
        with self._state_lock:
            if self._closed or not self._connection_active(connection):
                return
            connection.buffer.extend(chunk)
            connection.buffered_bytes += len(chunk)
            self._request_buffer_bytes += len(chunk)
            newline = connection.buffer.find(b"\n")
            if newline < 0:
                if len(connection.buffer) > MAX_FRAME_BYTES:
                    refusal = (
                        "frame_too_large",
                        f"request frame exceeds {MAX_FRAME_BYTES} bytes",
                    )
            elif newline + 1 != len(connection.buffer):
                refusal = (
                    "connection_queue_limit",
                    "one connection may queue exactly one request",
                )
            else:
                frame = bytes(connection.buffer)
        if refusal is not None:
            self._refuse_and_finish(channel, *refusal)
            return
        if frame is None:
            return
        try:
            request = _decode_request(frame)
            self._validate_envelope(request)
        except ProtocolRefusal as exc:
            self._refuse_and_finish(channel, exc.code, exc.detail)
            return
        del frame
        with self._state_lock:
            if self._closed or not self._connection_active(connection):
                return
            self._release_request_buffer(connection)
            try:
                self._selector.unregister(channel)
            except Exception:
                pass
            client_id = str(request["client_id"])
            self._queues.setdefault(client_id, deque()).append(
                _Pending(channel, request)
            )
            self._mark_ready(client_id)

    def _connection_active(self, connection: _Connection) -> bool:
        return any(
            candidate is connection for candidate in self._connections.values()
        )

    def _validate_envelope(self, request: Dict[str, object]) -> None:
        operation = request.get("operation")
        if operation == "append_run_record":
            if set(request) != _APPEND_REQUEST_FIELDS:
                raise ProtocolRefusal(
                    "frame_fields_invalid", "request fields must match the operation exactly"
                )
        elif operation in _INTENT_OPERATIONS:
            if set(request) != _INTENT_REQUEST_FIELDS:
                raise ProtocolRefusal(
                    "frame_fields_invalid", "request fields must match the operation exactly"
                )
        elif operation in _EVALUATED_INTENT_FIELDS:
            if set(request) != _INTENT_REQUEST_FIELDS:
                raise ProtocolRefusal(
                    "frame_fields_invalid", "request fields must match the operation exactly"
                )
        elif operation is None:
            raise ProtocolRefusal(
                "frame_fields_invalid", "request fields must match the operation exactly"
            )
        else:
            raise ProtocolRefusal("operation_invalid", "operation is not supported")
        if request["protocol_version"] != PROTOCOL_VERSION or isinstance(
            request["protocol_version"], bool
        ):
            raise ProtocolRefusal(
                "protocol_version_invalid", "protocol version must be integer one"
            )
        validate_identifier(request.get("client_id"), "client_id")  # type: ignore[arg-type]
        epoch = request["sequencer_epoch"]
        if not isinstance(epoch, int) or isinstance(epoch, bool) or epoch != self.epoch:
            raise ProtocolRefusal(
                "sequencer_epoch_mismatch", "request must name the live sequencer epoch"
            )
        if operation == "append_run_record":
            record = request["record"]
            if not isinstance(record, dict):
                raise ProtocolRefusal("record_invalid", "append requires one record object")
            candidate_id = record.get("id")
        elif operation in _INTENT_OPERATIONS:
            candidate_id = request.get("operation_id")
            self._validate_intent(str(operation), request.get("intent"))
        else:
            candidate_id = request.get("operation_id")
            request["intent"] = self._validate_evaluated_intent(
                str(operation), request.get("intent")
            )
        if request["operation_id"] != candidate_id:
            raise ProtocolRefusal(
                "operation_id_invalid", "operation id must equal the candidate record id"
            )

    @staticmethod
    def _validate_intent(operation: str, raw: object) -> None:
        if not isinstance(raw, dict):
            raise ProtocolRefusal("intent_invalid", "typed intent must be one object")
        expected = {"schema_version", "kind", "timestamp", "fields"}
        if operation == "capability_dispatch_intent":
            expected.add("policy")
        if set(raw) != expected:
            raise ProtocolRefusal(
                "intent_fields_invalid", "typed intent fields must match exactly"
            )
        if raw["kind"] in SPAWN_GROUP_KINDS or (
            raw["kind"] == "plan_amendment" and raw["schema_version"] == 1
        ):
            raise ProtocolRefusal(
                "spawn_group_controller_only",
                "spawn records require semantic service evaluation",
            )
        if raw["kind"] in SUSPENSION_KINDS:
            raise ProtocolRefusal(
                "suspension_controller_only",
                "approval suspension requires semantic service evaluation",
            )
        if raw["kind"] not in _INTENT_OPERATIONS[operation]:
            raise ProtocolRefusal(
                "intent_kind_invalid", "typed intent kind does not match its owner"
            )
        fields = raw["fields"]
        if not isinstance(fields, dict) or {
            "schema_version",
            "id",
            "tenant_id",
            "timestamp",
            "kind",
        } & set(fields):
            raise ProtocolRefusal(
                "intent_fields_invalid", "intent cannot supply durable envelope fields"
            )
        if not isinstance(raw["schema_version"], int) or isinstance(
            raw["schema_version"], bool
        ):
            raise ProtocolRefusal("intent_invalid", "intent schema version is invalid")
        if not isinstance(raw["timestamp"], str):
            raise ProtocolRefusal("intent_invalid", "intent timestamp is invalid")

    @staticmethod
    def _validate_evaluated_intent(
        operation: str, raw: object
    ) -> Dict[str, object]:
        return _canonical_evaluated_intent(operation, raw)

    def _mark_ready(self, client_id: str) -> None:
        if client_id not in self._ready_set:
            self._ready.append(client_id)
            self._ready_set.add(client_id)

    def _complete_pending(self, pending: _Pending) -> None:
        channel = pending.channel
        try:
            response = self._process_request(pending.request)
        except (ProtocolRefusal, IntegrityFailure, DurabilityFailure) as exc:
            response = _refused(exc.code, "request refused by local sequencer")
        except Exception:
            response = _refused("sequencer_internal_error", "local sequencer refused request")
        try:
            self._send_response(channel, response)
        finally:
            self._finish(channel)

    def _process_request(self, request: Dict[str, object]) -> Dict[str, object]:
        operation = request["operation"]
        assert isinstance(operation, str)
        if operation in _EVALUATED_INTENT_FIELDS:
            return self._process_evaluated_intent(request)
        if operation == "append_run_record":
            raw_record = request["record"]
            assert isinstance(raw_record, dict)
            raw_kind = raw_record.get("kind")
            if raw_kind in _PRIVATE_KINDS or (
                raw_kind == "dispatch_decision"
                and raw_record.get("schema_version") == 1
            ) or (
                raw_kind == "plan_amendment"
                and raw_record.get("schema_version") == 1
            ):
                if raw_kind in SPAWN_GROUP_KINDS or raw_kind == "plan_amendment":
                    raise ProtocolRefusal(
                        "spawn_group_controller_only",
                        "spawn records require semantic service evaluation",
                    )
                if raw_kind in SUSPENSION_KINDS:
                    raise ProtocolRefusal(
                        "suspension_controller_only",
                        "approval suspension requires semantic service evaluation",
                    )
                raise ProtocolRefusal(
                    "private_record_requires_intent",
                    "domain-owned records require a typed service intent",
                )
            record = self._canonical_public_record(raw_record)
        else:
            intent = request["intent"]
            assert isinstance(intent, dict)
            fields = intent["fields"]
            assert isinstance(fields, dict)
            record = {
                "schema_version": intent["schema_version"],
                "id": request["operation_id"],
                "tenant_id": self.root.tenant_id,
                "timestamp": intent["timestamp"],
                "kind": intent["kind"],
                **fields,
            }
        operation_id = str(request["operation_id"])
        kind = record.get("kind")
        cached = self._cache.get(operation_id)
        if cached is not None:
            prior, response = cached
            if prior != record:
                raise ProtocolRefusal(
                    "duplicate_record_id", "operation id has divergent payload"
                )
            if kind == "result_accepted":
                response = self._canonical_public_retry_response(record)
                self._remember(operation_id, record, response)
                return response
            self._cache.move_to_end(operation_id)
            return response
        if operation == "append_run_record":
            appended = self._ledger._append_managed(
                record, self.epoch, self._lease.managed_append_capability
            )
        else:
            appended = self._append_owned(operation, record, request["intent"])
        located = self._ledger._store.lookup(operation_id)
        if located is None:
            raise IntegrityFailure(
                "sequencer_coordinate_missing", "committed record has no physical coordinate"
            )
        coordinate = {
            "segment_number": located.coordinate.segment_number,
            "frame_ordinal": located.coordinate.frame_ordinal,
            "global_ordinal": located.coordinate.global_ordinal,
        }
        response = {"status": "ok", "record": appended, "coordinate": coordinate}
        self._remember(operation_id, appended, response)
        return response

    def _canonical_public_record(
        self, raw_record: Dict[str, object]
    ) -> Dict[str, object]:
        record = raw_record
        if record.get("kind") == "run_created" and isinstance(
            record.get("dependency_edges"), list
        ):
            record = dict(record)
            record["dependency_edges"] = [
                dict(
                    edge,
                    requires=edge.get("requires", "accepted"),
                    failure_policy=edge.get("failure_policy", "fail_run"),
                )
                if isinstance(edge, dict)
                else edge
                for edge in record["dependency_edges"]
            ]
        from .records import validate_record

        validate_record(record, self.root.tenant_id, RUN_KINDS, integrity=False)
        return record

    def _process_evaluated_intent(
        self, request: Dict[str, object]
    ) -> Dict[str, object]:
        operation_id = str(request["operation_id"])
        intent = request["intent"]
        assert isinstance(intent, dict)
        operation = str(request["operation"])
        semantic_uuid = _semantic_uuid(operation, intent)
        prefix = _EVALUATED_PREFIXES[operation]
        if operation_id != prefix + semantic_uuid:
            raise ProtocolRefusal(
                "operation_id_invalid", "evaluated operation id must cover its evidence"
            )
        cached = self._cache.get(operation_id)
        if cached is not None:
            prior, response = cached
            if prior != intent:
                raise ProtocolRefusal(
                    "duplicate_record_id", "operation id has divergent payload"
                )
            self._cache.move_to_end(operation_id)
            return response
        epoch_record = self._lease.record
        if not isinstance(epoch_record, dict):
            raise ProtocolRefusal(
                "sequencer_lease_inactive",
                "evaluated operation requires a live managed epoch",
            )
        binding_operation = operation in {
            "admission_binding_evaluation",
            "capability_binding_evaluation",
        }
        record_id = None
        policy = None
        if binding_operation:
            service_uuid = _epoch_semantic_uuid(
                str(epoch_record["id"]), operation, intent
            )
            record_id = (
                "run-admission-bound-" + service_uuid
                if operation == "admission_binding_evaluation"
                else "capability-set-bound-" + service_uuid
            )
            located = self._find_evaluated_record(operation, intent)
            if located is not None:
                response = self._success(located.record, located.coordinate)
                self._remember(operation_id, intent, response)
                return response
            from .policy import _build_policy

            policy_raw = intent["policy"]
            if not isinstance(policy_raw, dict):
                raise ProtocolRefusal(
                    "intent_evidence_required", "policy evidence is required"
                )
            policy = _build_policy(policy_raw)
        elif operation in _SPAWN_EVALUATED_OPERATIONS:
            from .policy import _build_policy

            policy = _build_policy(intent["policy"])
        service = self
        service_capability = object()

        class _EvaluationLedger(RunLedger):
            def _has_evaluated_service_capability(
                evaluated_ledger, capability: object
            ) -> bool:
                if (
                    capability is not service_capability
                    or evaluated_ledger.root != service.root
                    or service._closed
                    or service.epoch != service._lease.epoch
                ):
                    return False
                try:
                    return service._lease.managed_append_capability is not None
                except ProtocolRefusal:
                    return False

            def _append_suspension(
                evaluated_ledger,
                record: Dict[str, object],
                capability: object = None,
                resolve_existing: object = None,
            ) -> Dict[str, object]:
                return evaluated_ledger._append_managed_suspension(
                    record,
                    capability,
                    service.epoch,
                    service._lease.managed_append_capability,
                    service_capability,
                    resolve_existing,
                )

            def _append_scheduler(
                evaluated_ledger,
                record: Dict[str, object],
                capability: object = None,
                resolve_existing: object = None,
            ) -> Dict[str, object]:
                return evaluated_ledger._append_managed_scheduler(
                    record,
                    capability,
                    service.epoch,
                    service._lease.managed_append_capability,
                    service_capability,
                    resolve_existing,
                )

            def _append_cancellation(
                evaluated_ledger,
                record: Dict[str, object],
                capability: object = None,
                resolve_existing: object = None,
            ) -> Dict[str, object]:
                return evaluated_ledger._append_managed_cancellation(
                    record,
                    capability,
                    service.epoch,
                    service._lease.managed_append_capability,
                    service_capability,
                    resolve_existing,
                )

            def _append_spawn_group(
                evaluated_ledger,
                record: Dict[str, object],
                capability: object = None,
                resolve_existing: object = None,
            ) -> Dict[str, object]:
                return evaluated_ledger._append_managed_spawn_group(
                    record,
                    capability,
                    service.epoch,
                    service._lease.managed_append_capability,
                    service_capability,
                    resolve_existing,
                )

            def _append_spawn_admission(
                evaluated_ledger,
                record: Dict[str, object],
                capability: object = None,
                resolve_existing: object = None,
            ) -> Dict[str, object]:
                return evaluated_ledger._append_managed_spawn_admission(
                    record,
                    capability,
                    service.epoch,
                    service._lease.managed_append_capability,
                    service_capability,
                    resolve_existing,
                )

        if binding_operation:
            sink = object.__new__(_ServiceOwnerSink)
            sink.service = self
            sink.ledger = None
            ledger = _EvaluationLedger(self.root, sequencer_client=sink)
            sink.ledger = ledger
        else:
            ledger = _EvaluationLedger(self.root)
        evaluation_time = self._service_time()
        if operation == "admission_binding_evaluation":
            from .admission import AdmissionBinder, AdmissionPlan

            plan = AdmissionPlan.from_canonical(intent["plan"])
            appended = AdmissionBinder(ledger)._bind(
                str(intent["run_id"]),
                plan,
                policy,
                now=evaluation_time,
                record_id=record_id,
                _service_capability=service_capability,
            )
        elif operation == "capability_binding_evaluation":
            from .capabilities import CapabilityGrantLedger
            from .capability_binding import CapabilityBinder

            appended = CapabilityBinder(
                ledger, CapabilityGrantLedger(self.root)
            ).bind(
                str(intent["run_id"]),
                str(intent["item_id"]),
                str(intent["attempt_id"]),
                str(intent["chosen_worker"]),
                str(intent["worker_profile"]),
                policy,
                intent["routing_rank"],
                now=evaluation_time,
                _record_id=record_id,
                _service_capability=service_capability,
            )
        elif operation in {"suspension_evaluation", "approval_resume_evaluation"}:
            from .approvals import ApprovalLedger
            from .suspension import ApprovalSuspensionController

            controller = ApprovalSuspensionController(
                ledger, ApprovalLedger(self.root)
            )
            if operation == "suspension_evaluation":
                appended = controller.suspend(
                    intent["run_id"],
                    intent["item_id"],
                    intent["attempt_id"],
                    intent["approval_request_id"],
                    adapter=intent["adapter"],
                    resume_mode=intent["resume_mode"],
                    provider_session_or_thread_id=intent[
                        "provider_session_or_thread_id"
                    ],
                    workspace_checkpoint=intent["workspace_checkpoint"],
                    execution_authority_subject=intent[
                        "execution_authority_subject"
                    ],
                    execution_authority_holder=intent[
                        "execution_authority_holder"
                    ],
                    execution_authority_epoch=intent[
                        "execution_authority_epoch"
                    ],
                    now=evaluation_time,
                )
            else:
                appended = controller.consume(
                    intent["run_id"],
                    intent["item_id"],
                    intent["attempt_id"],
                    intent["approval_decision_id"],
                    workspace_checkpoint=intent["workspace_checkpoint"],
                    resume_authority_subject=intent["resume_authority_subject"],
                    resume_authority_holder=intent["resume_authority_holder"],
                    resume_authority_epoch=intent["resume_authority_epoch"],
                    now=evaluation_time,
                )
        else:
            from .admission import AdmissionBinder, AdmissionPlan
            from .spawn_groups import SpawnGroupController

            if operation == "spawn_admission_enable_evaluation":
                appended = AdmissionBinder(ledger)._enable_spawn(
                    intent["run_id"],
                    AdmissionPlan.from_canonical(intent["base_plan"]),
                    policy,
                    now=evaluation_time,
                )
            else:
                controller = SpawnGroupController(ledger, policy)
                if operation == "spawn_policy_bind_evaluation":
                    appended = controller.bind_attempt_policy(
                        intent["run_id"],
                        intent["parent_item_id"],
                        intent["parent_attempt_id"],
                        intent["parent_capability_set_bound_id"],
                        adapter=intent["adapter"],
                        subagents_mode=intent["subagents_mode"],
                        max_children=intent["max_children"],
                        max_depth=intent["max_depth"],
                        child_capability_ceiling=intent["child_capability_ceiling"],
                        spawn_budget_ceiling=intent["spawn_budget_ceiling"],
                        workspace_policies=intent["workspace_policies"],
                        now=evaluation_time,
                    )
                elif operation == "spawn_group_create_evaluation":
                    appended = controller.create_group(
                        run_id=intent["run_id"],
                        parent_item_id=intent["parent_item_id"],
                        parent_attempt_id=intent["parent_attempt_id"],
                        parent_fence_token=intent["parent_fence_token"],
                        group_key=intent["group_key"],
                        children=intent["children"],
                        dependency_edges=intent["dependency_edges"],
                        max_children=intent["max_children"],
                        max_depth=intent["max_depth"],
                        child_capability_ceiling=intent["child_capability_ceiling"],
                        aggregate_budget=intent["aggregate_budget"],
                        workspace_policy=intent["workspace_policy"],
                        deadline=intent["deadline"],
                        join_mode=intent["join_mode"],
                        required_count=intent["required_count"],
                        on_late_result=intent["on_late_result"],
                        on_child_failure=intent["on_child_failure"],
                        cancel_remaining_after_success=intent[
                            "cancel_remaining_after_success"
                        ],
                        now=evaluation_time,
                    )
                elif operation == "spawn_group_abort_evaluation":
                    appended = controller.abort_group(
                        intent["run_id"],
                        intent["spawn_group_id"],
                        reason_code=intent["reason_code"],
                        cancel_scope_resolved_id=intent["cancel_scope_resolved_id"],
                        operator_id=intent["operator_id"],
                        authority_subject=intent["authority_subject"],
                        authority_epoch=intent["authority_epoch"],
                        capability_record_id=intent["capability_record_id"],
                        now=evaluation_time,
                    )
                elif operation == "spawn_child_admission_evaluation":
                    if intent["outcome"] == "admit":
                        appended = controller.admit_child(
                            intent["run_id"], intent["spawn_group_id"],
                            intent["child_item_id"], now=evaluation_time,
                        )
                    else:
                        appended = controller.reject_child(
                            intent["run_id"], intent["spawn_group_id"],
                            intent["child_item_id"],
                            reason_code=intent["reason_code"],
                            now=evaluation_time,
                        )
                elif operation == "spawn_group_close_evaluation":
                    appended = controller.close_group(
                        intent["run_id"], intent["spawn_group_id"], adapters={},
                        cancel_scope_resolved_id=intent["cancel_scope_resolved_id"],
                        outcome=intent["outcome"], now=evaluation_time,
                    )
                else:
                    appended = controller.dispose_late_result(
                        intent["run_id"], intent["spawn_group_id"],
                        intent["child_item_id"], intent["result_record_id"],
                        intent["disposition"], operator_id=intent["operator_id"],
                        authority_subject=intent["authority_subject"],
                        authority_epoch=intent["authority_epoch"],
                        capability_record_id=intent["capability_record_id"],
                        now=evaluation_time,
                    )
        records = list(appended) if isinstance(appended, tuple) else [appended]
        primary = records[-1]
        located = self._ledger._store.lookup(str(primary["id"]))
        if located is None:
            raise IntegrityFailure(
                "sequencer_coordinate_missing", "committed record has no physical coordinate"
            )
        response = self._success(primary, located.coordinate)
        self._remember(operation_id, intent, response)
        return response

    def _find_evaluated_record(
        self, operation: str, intent: Dict[str, object]
    ) -> object:
        expected_ids = _service_record_ids(self.root, operation, intent)
        kind = (
            "run_admission_bound"
            if operation == "admission_binding_evaluation"
            else "capability_set_bound"
        )
        matches = []
        for record in self._ledger.records():
            if (
                record.get("kind") == kind
                and str(record.get("id")) in expected_ids
                and self._evaluated_record_matches_intent(operation, record, intent)
            ):
                located = self._ledger._store.lookup(str(record["id"]))
                if located is not None:
                    matches.append(located)
        if len(matches) > 1:
            raise IntegrityFailure(
                "evaluated_provenance_ambiguous",
                "canonical history contains multiple service origins for one intent",
            )
        return None if not matches else matches[0]

    @staticmethod
    def _evaluated_record_matches_intent(
        operation: str,
        record: Dict[str, object],
        intent: Dict[str, object],
    ) -> bool:
        policy = intent.get("policy")
        if not isinstance(policy, dict):
            return False
        from .policy import _build_policy

        policy_digest = _build_policy(policy).digest
        if operation == "admission_binding_evaluation":
            from .admission import AdmissionPlan

            plan = intent.get("plan")
            if not isinstance(plan, dict):
                return False
            return (
                record.get("kind") == "run_admission_bound"
                and record.get("run_id") == intent.get("run_id")
                and record.get("plan_digest") == AdmissionPlan.from_canonical(plan).digest
                and record.get("policy_digest") == policy_digest
            )
        return (
            record.get("kind") == "capability_set_bound"
            and record.get("run_id") == intent.get("run_id")
            and record.get("item_id") == intent.get("item_id")
            and record.get("attempt_id") == intent.get("attempt_id")
            and record.get("chosen_worker") == intent.get("chosen_worker")
            and record.get("policy_digest") == policy_digest
            and record.get("routing_rank") == intent.get("routing_rank")
        )

    def _service_time(self) -> datetime:
        current = self._clock()
        if (
            not isinstance(current, datetime)
            or current.tzinfo is None
            or current.utcoffset() is None
        ):
            raise ProtocolRefusal(
                "sequencer_clock_invalid", "service clock must return an aware datetime"
            )
        return current.astimezone(timezone.utc)

    @staticmethod
    def _success(record: Dict[str, object], coordinate: object) -> Dict[str, object]:
        return {
            "status": "ok",
            "record": record,
            "coordinate": {
                "segment_number": coordinate.segment_number,
                "frame_ordinal": coordinate.frame_ordinal,
                "global_ordinal": coordinate.global_ordinal,
            },
        }

    def _canonical_public_retry_response(
        self, record: Dict[str, object],
    ) -> Dict[str, object]:
        """Validate an acceptance retry through the guarded Run authority."""

        appended = self._ledger._append_managed(
            record, self.epoch, self._lease.managed_append_capability,
        )
        located = self._ledger._store.lookup(str(appended["id"]))
        if located is None:
            raise IntegrityFailure(
                "sequencer_coordinate_missing",
                "committed record has no physical coordinate",
            )
        return self._success(appended, located.coordinate)

    def _remember(
        self,
        operation_id: str,
        record: Dict[str, object],
        response: Dict[str, object],
    ) -> None:
        with self._state_lock:
            if self._closed:
                return
            self._cache[operation_id] = (dict(record), response)
            self._cache.move_to_end(operation_id)
            while len(self._cache) > self.config.response_cache_size:
                self._cache.popitem(last=False)

    def _append_owned(
        self, operation: str, record: Dict[str, object], raw_intent: object
    ) -> Dict[str, object]:
        if operation == "scheduler_intent":
            from .scheduler import RunScheduler

            owner = RunScheduler(self._ledger)
            capability = self._ledger._scheduler_capability_for(owner)
            return self._managed_owned("scheduler", record, capability)
        if operation == "cancellation_intent":
            from .cancellation import CancellationCoordinator

            owner = CancellationCoordinator(self._ledger)
            capability = self._ledger._cancellation_capability_for(owner)
            return self._managed_owned("cancellation", record, capability)
        if operation == "supervisor_intent":
            from .cancellation import FloatiSupervisor

            owner = FloatiSupervisor(self._ledger)
            capability = self._ledger._supervisor_capability_for(owner)
            return self._managed_owned("supervisor", record, capability)
        if operation == "admission_binding_intent":
            raise ProtocolRefusal(
                "intent_evidence_required",
                "admission binding requires service-evaluable evidence",
            )
        if operation == "capability_binding_intent":
            raise ProtocolRefusal(
                "intent_evidence_required",
                "capability binding requires service-evaluable evidence",
            )
        if operation == "capability_dispatch_intent":
            from .capabilities import CapabilityGrantLedger
            from .capability_binding import CapabilityBinder

            owner = CapabilityBinder(self._ledger, CapabilityGrantLedger(self.root))
            capability = self._ledger._capability_binding_capability_for(owner)
            assert isinstance(raw_intent, dict)
            from .policy import _build_policy

            policy = _build_policy(raw_intent["policy"])
            return self._managed_owned(
                "capability_dispatch",
                record,
                capability,
                dispatch_policy=policy,
            )
        raise ProtocolRefusal("operation_invalid", "typed intent is not supported")

    def _managed_owned(
        self,
        owner: str,
        record: Dict[str, object],
        owner_capability: object,
        *,
        dispatch_policy: object = None,
    ) -> Dict[str, object]:
        return self._ledger._append_managed_owned(
            owner,
            record,
            self.epoch,
            self._lease.managed_append_capability,
            owner_capability,
            dispatch_policy=dispatch_policy,
        )

    def _send_response(self, channel: socket.socket, response: Dict[str, object]) -> None:
        try:
            channel.setblocking(True)
            channel.settimeout(1.0)
            channel.sendall(_encode_response(response))
        except OSError:
            return

    def _refuse_and_finish(self, channel: socket.socket, code: str, detail: str) -> None:
        self._send_response(channel, _refused(code, detail))
        self._finish(channel)

    def _release_request_buffer(self, connection: _Connection) -> None:
        amount = connection.buffered_bytes
        assert amount == len(connection.buffer)
        assert 0 <= amount <= self._request_buffer_bytes
        self._request_buffer_bytes -= amount
        connection.buffered_bytes = 0
        connection.buffer.clear()

    def _finish(self, channel: socket.socket) -> None:
        try:
            with self._state_lock:
                try:
                    self._selector.unregister(channel)
                except Exception:
                    pass
                descriptor = next(
                    (
                        key
                        for key, connection in self._connections.items()
                        if connection.channel is channel
                    ),
                    None,
                )
                connection = (
                    self._connections.pop(descriptor)
                    if descriptor is not None
                    else None
                )
                if connection is not None:
                    self._release_request_buffer(connection)
        finally:
            try:
                channel.close()
            except OSError:
                pass
            self._resume_listener()

    def close(self) -> None:
        with self._close_guard:
            listener: Optional[socket.socket] = None
            connections: List[_Connection] = []
            with self._state_lock:
                if self._closed:
                    return
                self._closed = True
            try:
                with self._state_lock:
                    listener = self._listener
                    self._listener = None
                    self._listener_registered = False
                    connections = list(self._connections.values())
                    self._connections.clear()
                    try:
                        for connection in connections:
                            self._release_request_buffer(connection)
                    finally:
                        for connection in connections:
                            connection.buffered_bytes = 0
                            connection.buffer.clear()
                        self._request_buffer_bytes = 0
                        self._queues.clear()
                        self._ready.clear()
                        self._ready_set.clear()
                        self._cache.clear()
            finally:
                try:
                    channels = [connection.channel for connection in connections]
                    if listener is not None:
                        channels.insert(0, listener)
                    for channel in channels:
                        try:
                            channel.close()
                        except OSError:
                            pass
                finally:
                    try:
                        self._selector.close()
                    except Exception:
                        pass
                    finally:
                        try:
                            if (
                                self.socket_path.exists()
                                or self.socket_path.is_symlink()
                            ):
                                metadata = os.lstat(self.socket_path)
                                if stat.S_ISSOCK(metadata.st_mode):
                                    self.socket_path.unlink()
                        finally:
                            self._lease.__exit__(None, None, None)


def _encode_response(response: Dict[str, object]) -> bytes:
    try:
        encoded = json.dumps(
            response,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8") + b"\n"
    except (TypeError, ValueError, UnicodeError) as exc:
        raise ProtocolRefusal(
            "sequencer_response_invalid", "sequencer response cannot form I-JSON"
        ) from exc
    if len(encoded) > MAX_FRAME_BYTES:
        raise ProtocolRefusal(
            "sequencer_response_invalid", "sequencer response exceeds bounds"
        )
    return encoded


__all__ = [
    "SequencerClient",
    "SequencerConfig",
    "SequencerService",
    "sequencer_socket_path",
]

"""Dependency-free exact validators for durable v0 record kinds."""

from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, FrozenSet, Mapping, Optional

from .errors import IntegrityFailure, ProtocolRefusal
from .ids import uuid7_hex
from .root import IDENTIFIER_PATTERN


_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{3})?Z$")
_UUID7 = r"[0-9a-f]{12}7[0-9a-f]{3}[89ab][0-9a-f]{15}"
_PROVIDER_UUID7 = (
    r"[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12}"
)
_REPOSITORY = re.compile(r"^[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)?$")
_DECISION_REPOSITORY_SEGMENT = re.compile(
    r"^[A-Za-z0-9_-](?:[A-Za-z0-9._-]{0,61}[A-Za-z0-9_-])?$"
)
_CAPABILITY = re.compile(r"^[a-z0-9](?:[a-z0-9._-]{0,126}[a-z0-9])?$")
_GIT_SHA = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_DECISION_SOURCE_PATTERNS = (
    re.compile(r"^run:run-" + _UUID7 + r"$"),
    re.compile(r"^attempt:attempt-" + _UUID7 + r"$"),
    re.compile(r"^contract:task-contract-" + _UUID7 + r"$"),
    re.compile(r"^receipt:(?:worker-receipt|acceptance-receipt)-" + _UUID7 + r"$"),
    re.compile(r"^decision:decision-" + _UUID7 + r"$"),
)
_DECISION_DOC_SOURCE = re.compile(r"^doc:(.+)@(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_DECISION_AUTHOR_AUTHORITIES = frozenset({"operator", "architect", "worker"})
_BIDI_CONTROLS = frozenset(
    {"LRE", "RLE", "LRO", "RLO", "PDF", "LRI", "RLI", "FSI", "PDI", "BN"}
)
READER_VERSION = "0"
_COMMON = frozenset(("schema_version", "id", "tenant_id", "timestamp", "kind"))
WAKE_HOLD_KINDS = frozenset({"delivery_receipt", "wake_hold_receipt"})
WAKE_ATTEMPT_REFUSED_REASONS = frozenset(
    {
        "wake_envelope_not_owned",
        "wake_decision_missing",
        "wake_decision_mismatch",
        "wake_prompt_failed",
        "wake_daemon_adapter_timeout",
        "wake_daemon_adapter_unavailable",
        "wake_daemon_adapter_nonzero",
        "wake_daemon_adapter_unknown",
        "wake_daemon_cursor_output_empty",
        "wake_daemon_cursor_output_invalid",
        "wake_daemon_cursor_result_invalid",
        "wake_daemon_grok_output_empty",
        "wake_daemon_grok_output_invalid",
        "wake_daemon_grok_result_invalid",
        "wake_daemon_zcode_output_empty",
        "wake_daemon_zcode_output_invalid",
        "wake_daemon_zcode_result_invalid",
    }
)
EFFECT_KINDS = frozenset({
    "effect_intent", "effect_dispatched", "effect_acknowledged",
    "effect_confirmed", "effect_failed", "effect_unknown",
    "effect_reconciled", "compensation_proposed", "compensation_executed",
})
EFFECT_TYPES = frozenset({
    "git_ref_update", "git_remote_ref_update", "github_mutation",
    "deployment", "shell_command", "external_api",
})
EFFECT_TARGET_KINDS = frozenset({
    "git_ref", "git_remote_ref", "github_resource", "deployment_target",
    "shell_environment", "external_api_resource",
})
EFFECT_DISPATCH_ADAPTERS = frozenset({
    "git_local", "git_remote_explicit", "github_explicit", "deployment_explicit",
    "shell_explicit", "external_api_explicit",
})
EFFECT_FAILURE_REASONS = frozenset({
    "destination_rejected", "destination_conflict", "request_invalid",
    "authorization_denied", "effect_not_applied", "budget_exceeded",
})
EFFECT_UNKNOWN_REASONS = frozenset({
    "adapter_unavailable", "confirmation_absent", "process_lost",
    "destination_unqueryable", "evidence_malformed", "reconciliation_inconclusive",
})
EFFECT_COMPENSATION_REASONS = frozenset({
    "operator_requested", "effect_failed", "effect_superseded", "policy_required",
})
EFFECT_SPEND_STATUSES = frozenset({"complete", "partial", "unknown"})
EFFECT_BINDING_FIELDS = frozenset({
    "operation_id", "run_id", "item_id", "attempt_id", "attempt_started_id",
    "fence_token", "effect_type", "target", "request_digest", "idempotency_key",
    "expected_confirmation", "reconciliation_adapter", "risk_class", "budget_claim",
})
_EFFECT_TARGET_BY_TYPE = {
    "git_ref_update": "git_ref", "git_remote_ref_update": "git_remote_ref",
    "github_mutation": "github_resource", "deployment": "deployment_target",
    "shell_command": "shell_environment", "external_api": "external_api_resource",
}
_EFFECT_RECONCILIATION_ADAPTERS = frozenset({
    "git_local", "git_remote_explicit", "github_explicit", "deployment_explicit", "none",
})
_EFFECT_SPECS: Mapping[str, tuple[str, FrozenSet[str]]] = {
    "effect_intent": ("effect-intent-", _COMMON | EFFECT_BINDING_FIELDS | {"requested_by", "approval_request_id", "approval_decision_id", "approval_consumption_id", "intended_at_testimony"}),
    "effect_dispatched": ("effect-dispatched-", _COMMON | EFFECT_BINDING_FIELDS | {"effect_intent_id", "dispatch_adapter", "dispatch_evidence_digest", "dispatched_at_testimony"}),
    "effect_acknowledged": ("effect-acknowledged-", _COMMON | EFFECT_BINDING_FIELDS | {"effect_intent_id", "effect_dispatched_id", "acknowledgement_digest", "acknowledged_at_testimony"}),
    "effect_confirmed": ("effect-confirmed-", _COMMON | EFFECT_BINDING_FIELDS | {"effect_intent_id", "effect_dispatched_id", "effect_acknowledged_id", "confirmation", "confirmation_evidence_digest", "measured_spend", "confirmed_at_testimony"}),
    "effect_failed": ("effect-failed-", _COMMON | EFFECT_BINDING_FIELDS | {"effect_intent_id", "effect_dispatched_id", "reason_code", "failure_evidence_digest", "spend_status", "measured_spend", "failed_at_testimony"}),
    "effect_unknown": ("effect-unknown-", _COMMON | EFFECT_BINDING_FIELDS | {"effect_intent_id", "effect_dispatched_id", "reason_code", "unknown_evidence_digest", "spend_status", "measured_spend", "unknown_at_testimony"}),
    "effect_reconciled": ("effect-reconciled-", _COMMON | EFFECT_BINDING_FIELDS | {"effect_intent_id", "prior_effect_evidence_id", "reconciled_outcome", "reconciliation_evidence_digest", "confirmation", "spend_status", "measured_spend", "reconciled_at_testimony"}),
    "compensation_proposed": ("compensation-proposed-", _COMMON | EFFECT_BINDING_FIELDS | {"effect_intent_id", "source_effect_evidence_id", "reason_code", "compensation_plan_digest", "compensation_request_digest", "compensation_operation_id", "compensation_risk_class", "approval_request_id", "approval_decision_id", "approval_consumption_id", "proposed_at_testimony"}),
    "compensation_executed": ("compensation-executed-", _COMMON | EFFECT_BINDING_FIELDS | {"compensation_proposal_id", "compensation_operation_id", "compensation_terminal_evidence_id", "executed_at_testimony"}),
}


def _kind_prefix(kind: str) -> str:
    return kind.replace("_", "-") + "-"


_SPECS: Mapping[str, tuple[str, FrozenSet[str]]] = {
    "quota_receipt_record": (
        "quota-receipt-",
        _COMMON | {
            "provider", "endpoint_id", "facts", "idempotency_key",
            "receipt_digest",
        },
    ),
    "tide_policy_record": (
        "tide-policy-",
        _COMMON | {
            "node_id", "harness", "metric", "threshold", "action",
            "access_class", "stamp", "formula", "receipt_path", "state",
            "predecessor_policy_id", "idempotency_key",
        },
    ),
    "tide_receipt": (
        "tide-receipt-",
        _COMMON | {
            "node_id", "harness", "policy_id", "metric", "value",
            "threshold", "stamp", "access_class", "formula", "sources",
            "evaluation_state", "action", "crossing_epoch",
            "crossing_receipt_id", "state_flush_receipt_id", "envelope_id",
            "idempotency_key",
        },
    ),
    "tide_testimony_record": (
        "tide-testimony-",
        _COMMON | {
            "node_id", "harness", "metric", "value", "command",
            "access_class", "stamp", "formula", "receipt_path",
            "idempotency_key",
        },
    ),
    "ack_receipt": ("ack-", _COMMON | {"recipient", "item_ids"}),
    "authority_grant": ("authority-", _COMMON | {"subject_id", "holder", "epoch", "claimed_at", "renewed_at", "expires_at", "released_at", "ttl_seconds", "deadline_seconds", "state"}),
    "delivery_receipt": ("delivery-", _COMMON | {"recipient", "item_ids", "presentation_count"}),
    "wake_hold_receipt": ("wake-hold-", _COMMON | {"recipient", "worker_session_id", "idempotency_key", "limit", "item_ids", "event_prefix_digest", "delivery_prefix_digest", "acknowledgment_prefix_digest", "decision_digest"}),
    "wake_attempt_receipt": ("wake-attempt-", _COMMON | {"node_id", "acting_session_id", "message_worker_session_id", "idempotency_key", "item_ids", "decision_receipt_id", "outcome", "reason_code"}),
    "bus_epoch_roll_receipt": (
        "bus-epoch-roll-receipt-",
        _COMMON | {
            "archive_path", "actor", "idempotency_key",
            "invalidated_followers", "epoch_id", "span",
            "archive_sha256", "archive_file_count", "plane_counts",
        },
    ),
    "codex_wait_consent_receipt": ("codex-wait-consent-", _COMMON | {"node_id", "workspace", "workspace_map_digest", "hook_timeout_seconds", "wait_deadline_seconds", "state", "idempotency_key"}),
    "codex_wait_session_receipt": ("codex-wait-session-", _COMMON | {"node_id", "workspace", "workspace_map_digest", "acting_session_id", "operation", "state", "predecessor_receipt_id", "consent_receipt_id", "idempotency_key"}),
    "codex_wait_exhaustion_receipt": ("codex-wait-exhaustion-", _COMMON | {"node_id", "session_digest", "waited_seconds", "outcome", "idempotency_key"}),
    "wake_waiter_exit_receipt": ("wake-waiter-exit-", _COMMON | {"node_id", "session_digest", "reason_code", "waited_seconds", "idempotency_key"}),
    "wake_control_receipt": ("wake-control-", _COMMON | {"node_id", "session_digest", "operation", "state", "predecessor_receipt_id", "idempotency_key"}),
    "wake_daemon_consent_receipt": ("wake-daemon-consent-", _COMMON | {"node_id", "harness", "coordinate_digest", "adapter_version", "adapter_digest", "min_poll_seconds", "max_poll_seconds", "max_backoff_seconds", "activation_epoch", "operation", "state", "predecessor_receipt_id", "idempotency_key"}),
    "wake_daemon_lifecycle_receipt": ("wake-daemon-lifecycle-", _COMMON | {"node_id", "harness", "coordinate_digest", "daemon_instance_id", "activation_epoch", "event", "state", "reason_code", "adapter_digest", "plist_digest", "session_digest", "predecessor_receipt_id", "idempotency_key"}),
    "denial_receipt": ("denial-", _COMMON | {"attempt_id", "claimed_sender", "claimed_recipient", "reason_code"}),
    "liveness_presence": ("presence-", _COMMON | {"node_id", "observed_at", "expires_at", "state"}),
    "message_envelope": ("msg-", _COMMON | {"sender", "recipient", "repo", "sha", "doc", "note", "idempotency_key"}),
    "delivery_claim": (
        "delivery-claim-",
        _COMMON
        | {
            "sha", "repo_path", "bank", "declared", "artifacts",
            "note_ref", "deadline_seconds",
        },
    ),
    "verification_receipt": (
        "verification-receipt-",
        _COMMON
        | {
            "claim_id", "verifier", "outcome", "reason_code", "remedy",
            "runner_argv", "python_version", "repo_path", "wall_time_seconds",
            "unchecked_scope", "output_sha256", "claim", "measurement",
            "scratch",
        },
    ),
    "journal_checkpoint_state": (
        "journal-checkpoint-state-",
        _COMMON
        | {
            "journal_id", "through_seq", "head_sha256", "byte_length",
            "checkpoint_sha256",
        },
    ),
    "ledger_repair_receipt": (
        "ledger-repair-receipt-",
        _COMMON
        | {
            "ledger", "record_id", "idempotency_key", "original_digest",
            "repaired_digest", "quarantine_path", "quarantine_digest",
            "replaced_inode", "invalidated_followers",
        },
    ),
    "message_retracted": ("ret-", _COMMON | {"retracted_message_id", "worker_session_id", "reason", "author"}),
    "mutual_exclusion_hold": ("hold-", _COMMON | {"resource_id", "holder", "epoch", "acquired_at", "renewed_at", "expires_at", "released_at", "ttl_seconds", "deadline_seconds", "state"}),
    "registry_entry": ("registry-", _COMMON | {"node_id", "role", "state"}),
    "node_lease": ("lease-", _COMMON | {"node_id", "workspace", "state"}),
    "provider_switch_receipt": (
        "provider-switch-receipt-",
        _COMMON
        | {
            "node_id", "previous_registry_entry_id", "previous_harness",
            "previous_model", "harness", "model", "registry_entry_id",
        },
    ),
    "fleet_update_started": (
        "fleet-update-started-",
        _COMMON | {"plan_digest", "actor", "consent_receipt_id", "operation", "step_kind", "pre_digest", "post_digest", "step_ordinal", "step_coordinate", "commit_disposition", "step_evidence", "state", "predecessor_receipt_id", "idempotency_key", "owner_review_batch_digest", "reader_consequences", "seat_binding_consequences", "seat_exclusions", "recovery_witness"},
    ),
    "fleet_update_step": (
        "fleet-update-step-",
        _COMMON | {"plan_digest", "actor", "consent_receipt_id", "operation", "step_kind", "pre_digest", "post_digest", "step_ordinal", "step_coordinate", "commit_disposition", "step_evidence", "state", "predecessor_receipt_id", "idempotency_key", "owner_review_batch_digest", "reader_consequences", "seat_binding_consequences", "seat_exclusions"},
    ),
    "fleet_update_completed": (
        "fleet-update-completed-",
        _COMMON | {"plan_digest", "actor", "consent_receipt_id", "operation", "step_kind", "pre_digest", "post_digest", "step_ordinal", "step_coordinate", "commit_disposition", "step_evidence", "state", "predecessor_receipt_id", "idempotency_key", "owner_review_batch_digest", "reader_consequences", "seat_binding_consequences", "seat_exclusions", "step_receipt_ids", "moves", "unchanged", "previous_source_sha", "target_source_sha", "epoch_roll_state", "registry_before_sha256", "registry_after_sha256"},
    ),
    "registry_role_record": (
        "registry-role-",
        _COMMON
        | {
            "node_id", "template_role", "template_version", "template_sha256",
            "answers", "state", "predecessor_role_record_id",
        },
    ),
    "lane_spawn_receipt": (
        _kind_prefix("lane_spawn_receipt"),
        _COMMON
        | {
            "profile", "ordinal", "node_id", "actor", "state",
            "failing_step", "artifacts", "compensated",
        },
    ),
    "lane_teardown_receipt": (
        _kind_prefix("lane_teardown_receipt"),
        _COMMON
        | {
            "profile", "ordinal", "node_id", "actor", "state",
            "removed", "retained",
        },
    ),
    "wake_cause": ("wake-", _COMMON | {"node_id", "cause", "context_bytes", "wake_count"}),
    "work_item": ("work-", _COMMON | {"title", "owner", "artifact_bindings"}),
    "work_transition": ("transition-", _COMMON | {"work_item_id", "action", "actor", "authority_subject", "authority_epoch", "artifact_bindings"}),
    "capability": ("capability-", _COMMON | {"node_id", "capability", "mode", "scope", "observed_at", "expires_at"}),
    "capability_grant": ("capability-grant-", _COMMON | {"worker_id", "capability_name", "policy_digest", "approval_request_id", "approval_decision_id", "authority_subject", "authority_epoch", "expires_at", "grant_digest"}),
    "capability_revoked": ("capability-revoked-", _COMMON | {"grant_id", "reason_code", "replacement_policy_digest"}),
    "confluence_grant": ("confluence-grant-", _COMMON | {"consumer", "state", "predecessor_receipt_id", "idempotency_key"}),
    "approval_request": ("approval-request-", _COMMON | {"requester", "capability", "scope", "requested_ttl_seconds", "requested_at", "expires_at", "authority_subject", "authority_epoch"}),
    "approval_decision": ("approval-decision-", _COMMON | {"request_id", "decider", "decision", "granted_scope", "granted_ttl_seconds", "reason_code", "decided_at", "expires_at", "authority_subject", "authority_epoch"}),
    "worker_receipt": ("worker-receipt-", _COMMON | {"session_id", "work_item_id", "node_id", "adapter", "transition", "outcome_code", "authority_subject", "authority_epoch", "artifact_bindings"}),
    "worker_refusal": ("worker-refusal-", _COMMON | {"node_id", "adapter", "work_item_id", "reason_code"}),
    "session_adoption": ("adoption-", _COMMON | {"session_id", "mode", "manager_node_id", "lease_subject", "lease_epoch", "lease_expires_at"}),
    "session_release": ("release-", _COMMON | {"session_id", "adoption_id", "manager_node_id", "lease_subject", "lease_epoch"}),
    "bridge_consent": ("bridge-consent-", _COMMON | {"bridge_id", "peer_tenant_id", "actor", "direction", "state", "scope"}),
    "bridge_record": ("bridge-record-", _COMMON | {"bridge_id", "left_tenant_id", "right_tenant_id", "left_consent_id", "right_consent_id", "transport", "scope", "state"}),
    "bridge_forward": ("bridge-forward-", _COMMON | {"bridge_id", "forward_id", "direction", "source_tenant_id", "destination_tenant_id", "sender", "recipient", "repo", "sha", "doc", "note", "source_consent_id", "destination_consent_id", "stamp"}),
    "bridge_denial": ("bridge-denial-", _COMMON | {"bridge_id", "source_tenant_id", "destination_tenant_id", "direction", "reason_code", "stamp"}),
    "gateway_session_ingress": ("gateway-ingress-", _COMMON | {"gateway_version", "session_id", "actor", "workspace", "transport"}),
    "gateway_capability_declaration": ("gateway-capability-", _COMMON | {"gateway_version", "session_id", "capabilities"}),
    "gateway_approval_forward": ("gateway-approval-", _COMMON | {"gateway_version", "session_id", "request_id", "capability", "scope", "state"}),
    "decision_record": ("decision-record-", _COMMON | {"repository", "decision_id", "scope", "statement", "status", "author_authority", "source_artifact_ids", "task_contract_id", "decided_by", "supersedes", "decision_digest"}),
    "segment_opened": ("run-segment-opened-", _COMMON | {"segment_number", "first_global_ordinal", "previous_seal_digest", "max_records", "max_bytes"}),
    "segment_sealed": ("run-segment-sealed-", _COMMON | {"segment_number", "opening_record_id", "last_global_ordinal", "record_count", "byte_length", "segment_sha256", "seal_digest"}),
    "task_contract": ("task-contract-", _COMMON | {"run_id", "item_id", "objective", "non_goals", "areas_to_avoid", "input_hashes", "acceptance_checks", "constraints", "risk_class", "retry_policy", "dependencies", "contract_digest"}),
    "plan_amendment": ("plan-amendment-", _COMMON | {"run_id", "item_id", "task_contract_id", "previous_digest", "replacement_fields", "contract_digest"}),
    "run_spawn_admission_enabled": ("run-spawn-admission-enabled-", _COMMON | {"run_id", "run_admission_binding_id", "admission_digest", "policy_digest", "base_plan", "base_plan_digest", "enabled_at_testimony"}),
    "attempt_spawn_policy_bound": ("attempt-spawn-policy-bound-", _COMMON | {"run_id", "parent_item_id", "parent_attempt_id", "parent_fence_token", "parent_capability_set_bound_id", "adapter", "subagents_mode", "max_children", "max_depth", "child_capability_ceiling", "spawn_budget_ceiling", "workspace_policies", "bound_at_testimony"}),
    "spawn_group_created": ("spawn-group-created-", _COMMON | {"run_id", "parent_item_id", "parent_attempt_id", "parent_fence_token", "parent_spawn_policy_id", "group_key", "max_children", "max_depth", "child_capability_ceiling", "aggregate_budget", "workspace_policy", "deadline", "join_mode", "required_count", "on_late_result", "on_child_failure", "cancel_remaining_after_success"}),
    "spawn_group_aborted": ("spawn-group-aborted-", _COMMON | {"run_id", "spawn_group_id", "parent_attempt_id", "parent_fence_token", "reason_code", "cancel_scope_resolved_id", "operator_id", "authority_subject", "authority_epoch", "capability_record_id", "aborted_at_testimony"}),
    "child_admitted": ("child-admitted-", _COMMON | {"run_id", "spawn_group_id", "plan_amendment_id", "parent_attempt_id", "child_item_id", "child_depth", "task_contract_id", "task_contract_digest", "admission_digest", "capability_ceiling", "budget_allocation", "workspace_policy", "workspace", "admitted_at_testimony"}),
    "child_rejected": ("child-rejected-", _COMMON | {"run_id", "spawn_group_id", "plan_amendment_id", "parent_attempt_id", "child_item_id", "reason_code", "evaluated_at_testimony"}),
    "spawn_group_closed": ("spawn-group-closed-", _COMMON | {"run_id", "spawn_group_id", "plan_amendment_id", "parent_attempt_id", "member_item_ids", "accepted_item_ids", "terminal_item_ids", "rejected_item_ids", "join_mode", "required_count", "outcome", "close_reason", "cancel_scope_resolved_id", "closed_at_testimony"}),
    "untracked_descendant": ("untracked-descendant-", _COMMON | {"run_id", "parent_item_id", "parent_attempt_id", "adapter", "provider_descendant_id", "state", "adopted_item_id", "reason_code", "observed_at_testimony"}),
    "descendant_observation_closed": ("descendant-observation-closed-", _COMMON | {"run_id", "parent_item_id", "parent_attempt_id", "parent_fence_token", "attempt_spawn_policy_id", "adapter", "observed_descendant_ids", "closed_at_testimony"}),
    "spawn_late_result_disposition": ("spawn-late-result-disposition-", _COMMON | {"run_id", "spawn_group_id", "child_item_id", "result_record_id", "disposition", "operator_id", "authority_subject", "authority_epoch", "capability_record_id", "decided_at_testimony"}),
    "acceptance_receipt": ("acceptance-receipt-", _COMMON | {"run_id", "item_id", "attempt_id", "contract_digest", "check_ids", "reviewer", "evidence_bindings", "deviations", "result"}),
    "run_created": ("run-created-", _COMMON | {"run_id", "plan_digest", "item_ids", "dependency_edges"}),
    "run_admission_bound": ("run-admission-bound-", _COMMON | {"run_id", "plan_digest", "policy_digest", "max_active_attempts", "workers", "budget_reservations", "items", "admission_digest"}),
    "run_policy_bound": ("run-policy-bound-", _COMMON | {"run_id", "policy_digest"}),
    "worker_pool_bound": ("run-worker-pool-bound-", _COMMON | {"run_id", "worker_ids"}),
    "capability_set_bound": ("capability-set-bound-", _COMMON | {"run_id", "item_id", "attempt_id", "fence_token", "chosen_worker", "policy_digest", "routing_rank", "evaluated_at_testimony", "grant_ledger_high_watermark", "effective_grants", "capability_digest"}),
    "sequencer_epoch": ("sequencer-epoch-", _COMMON | {"epoch", "operation", "sequencer_id", "previous_epoch_record_id", "absence_reason"}),
    "dispatch_decision": ("run-dispatch-decision-", _COMMON | {"run_id", "item_id", "attempt_id", "eligible_workers", "chosen_worker", "capability_digest", "reason_code", "policy_digest", "routing_rank", "scheduler_epoch"}),
    "result_produced": ("run-result-produced-", _COMMON | {"run_id", "item_id", "attempt_id", "dispatch_decision_id", "worker_receipt_ids"}),
    "result_verified": ("run-result-verified-", _COMMON | {"run_id", "item_id", "attempt_id", "result_produced_id", "worker_receipt_ids"}),
    "result_accepted": ("run-result-accepted-", _COMMON | {"run_id", "item_id", "attempt_id", "predecessor_result_id", "acceptance_mode", "acceptance_receipt_id", "worker_receipt_ids"}),
    "run_terminal": ("run-terminal-", _COMMON | {"run_id", "outcome"}),
    "attempt_opened": ("attempt-opened-", _COMMON | {"run_id", "item_id", "attempt_id", "ordinal", "scheduler_epoch", "fence_token", "max_attempts", "backoff"}),
    "attempt_started": ("attempt-started-", _COMMON | {"run_id", "item_id", "attempt_id", "ordinal", "attempt_opened_id", "dispatch_decision_id", "fence_token"}),
    "attempt_terminal": ("attempt-terminal-", _COMMON | {"run_id", "item_id", "attempt_id", "ordinal", "attempt_started_id", "fence_token", "terminal_state", "policy_class", "reason_code", "effect_safety", "retry_disposition", "retry_record_id", "next_attempt_id", "next_ordinal", "retry_delay_ms", "next_scheduler_epoch", "next_fence_token"}),
    "retry_scheduled": ("retry-scheduled-", _COMMON | {"run_id", "item_id", "previous_attempt_id", "attempt_terminal_id", "next_attempt_id", "next_ordinal", "delay_ms", "scheduler_epoch", "next_fence_token"}),
    "retry_exhausted": ("retry-exhausted-", _COMMON | {"run_id", "item_id", "attempt_id", "ordinal", "attempt_terminal_id", "max_attempts", "reason_code"}),
    "cancel_requested": ("cancel-requested-", _COMMON | {"run_id", "scope", "item_id", "requested_by"}),
    "cancel_scope_resolved": ("cancel-scope-resolved-", _COMMON | {"run_id", "cancel_request_id", "scope", "item_id", "item_ids", "attempt_ids"}),
    "attempt_cancelled_before_start": ("attempt-cancelled-before-start-", _COMMON | {"run_id", "item_id", "attempt_id", "attempt_opened_id", "retry_scheduled_id", "fence_token", "cancel_scope_resolved_id", "capability_set_bound_id", "dispatch_decision_id", "reason_code", "cancelled_at_testimony"}),
    "spawn_child_cancelled_without_attempt": ("spawn-child-cancelled-without-attempt-", _COMMON | {"run_id", "spawn_group_id", "plan_amendment_id", "child_item_id", "child_admitted_id", "cancel_scope_resolved_id", "reason_code", "cancelled_at_testimony"}),
    "cancel_observed": ("cancel-observed-", _COMMON | {"run_id", "cancel_scope_resolved_id", "item_id", "attempt_id", "fence_token", "adapter", "cancel_mode"}),
    "cancel_signal_sent": ("cancel-signal-sent-", _COMMON | {"run_id", "cancel_scope_resolved_id", "item_id", "attempt_id", "fence_token", "adapter", "cancel_mode"}),
    "cancel_terminal": ("cancel-terminal-", _COMMON | {"run_id", "cancel_scope_resolved_id", "item_id", "attempt_id", "fence_token", "adapter", "cancel_mode"}),
    "cancel_unconfirmed": ("cancel-unconfirmed-", _COMMON | {"run_id", "cancel_scope_resolved_id", "item_id", "attempt_id", "fence_token", "adapter", "cancel_mode"}),
    "stale_attempt_evidence": ("stale-attempt-evidence-", _COMMON | {"run_id", "item_id", "attempt_id", "worker_receipt_ids", "presented_fence_token", "current_attempt_id", "current_fence_token"}),
    "stale_evidence_adopted": ("stale-evidence-adopted-", _COMMON | {"run_id", "item_id", "stale_evidence_id", "current_attempt_id", "current_fence_token", "operator_id", "authority_subject", "authority_epoch", "capability_record_id"}),
    "attempt_harness_session_bound": ("attempt-harness-session-bound-", _COMMON | {"run_id", "item_id", "attempt_id", "fence_token", "claim_id", "lease_id", "worker_session_id", "harness_segments"}),
    "supervisor_orphaned": ("supervisor-orphaned-", _COMMON | {"run_id", "item_id", "attempt_id", "claim_id", "lease_id", "worker_session_id", "supervisor_id", "orphan_class", "authority_subject", "authority_epoch", "capability_record_id"}),
    "attempt_suspended_for_approval": ("attempt-suspended-approval-", _COMMON | {"run_id", "item_id", "attempt_id", "attempt_started_id", "fence_token", "adapter", "approval_request_id", "exact_action_digest", "requested_scope", "resume_mode", "provider_session_or_thread_id", "workspace", "workspace_checkpoint", "execution_authority_subject", "execution_authority_holder", "authority_epoch_at_request", "approval_expiry"}),
    "approval_consumed_for_resume": ("approval-consumed-resume-", _COMMON | {"run_id", "item_id", "attempt_id", "fence_token", "attempt_suspended_id", "approval_request_id", "approval_decision_id", "exact_action_digest", "requested_scope", "resume_mode", "provider_session_or_thread_id", "workspace", "workspace_checkpoint", "resume_authority_subject", "resume_authority_holder", "resume_authority_epoch", "consumed_at_testimony"}),
    "thread_attachment_registered": ("thread-attachment-", _COMMON | {"provider", "provider_thread_id", "subject_kind", "work_item_id", "registered_by", "registered_at_testimony"}),
    "thread_observation_recorded": ("thread-observation-", _COMMON | {"attachment_id", "provider", "provider_thread_id", "provider_status", "active_flags", "provider_updated_at", "attention", "observation_outcome", "observation_reason", "observation_digest", "observed_at_testimony"}),
    "thread_attachment_detached": ("thread-attachment-detached-", _COMMON | {"attachment_id", "provider", "provider_thread_id", "detached_by", "detached_at_testimony"}),
    **_EFFECT_SPECS,
}
_V1_FIELDS: Mapping[str, FrozenSet[str]] = {
    "bus_epoch_roll_receipt": _SPECS["bus_epoch_roll_receipt"][1],
    "tide_policy_record": _SPECS["tide_policy_record"][1],
    "tide_receipt": _SPECS["tide_receipt"][1],
    "ack_receipt": _SPECS["ack_receipt"][1]
    | {
        "acting_session_id",
        "node_lease_id",
        "node_lease_state_at_ack",
        "node_lease_expires_at",
    },
    "approval_request": _SPECS["approval_request"][1] | {"exact_action_digest"},
    "approval_decision": _SPECS["approval_decision"][1] | {"exact_action_digest"},
    "plan_amendment": _COMMON | {"run_id", "spawn_group_id", "parent_item_id", "parent_attempt_id", "parent_spawn_policy_id", "previous_plan_digest", "previous_admission_digest", "policy_digest", "children", "dependency_edges", "plan_digest", "admission_digest"},
    "cancel_requested": _COMMON | {"run_id", "scope", "item_id", "item_ids", "spawn_group_id", "requested_by"},
    "cancel_scope_resolved": _COMMON | {"run_id", "cancel_request_id", "scope", "item_id", "item_ids", "attempt_ids"},
    "result_accepted": _SPECS["result_accepted"][1] | {
        "effect_operation_ids", "effect_ledger_high_watermark",
        "effect_evidence_digest",
    },
    "thread_attachment_registered": _SPECS["thread_attachment_registered"][1],
    "thread_observation_recorded": _SPECS["thread_observation_recorded"][1],
    "thread_attachment_detached": _SPECS["thread_attachment_detached"][1],
    **{kind: fields for kind, (_, fields) in _EFFECT_SPECS.items()},
}

TASK3_CANCELLATION_KINDS = frozenset({
    "attempt_cancelled_before_start",
    "spawn_child_cancelled_without_attempt",
})

SPAWN_GROUP_KINDS = frozenset({
    "run_spawn_admission_enabled",
    "attempt_spawn_policy_bound",
    "spawn_group_created",
    "spawn_group_aborted",
    "child_admitted",
    "child_rejected",
    "spawn_group_closed",
    "untracked_descendant",
    "descendant_observation_closed",
    "spawn_late_result_disposition",
})

THREAD_OBSERVATION_KINDS = frozenset({
    "thread_attachment_registered",
    "thread_observation_recorded",
    "thread_attachment_detached",
})


def _json_integer(value: object) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and math.isfinite(value) and value.is_integer():
        return int(value)
    return None

WORKER_OUTCOME_CODES = frozenset({
    "adapter_error",
    "adapter_malformed_output",
    "approval_required_unattended",
    "artifact_ambiguous",
    "artifact_missing",
    "authority_expired_mid_claim",
    "authority_deadline_unavailable",
    "authority_state_unavailable",
    "credential_network_boundary_unruled",
    "effect_context_hook_missing",
    "effect_worker_isolation_unavailable",
    "git_finalize_failed",
    "process_cancelled",
    "process_died",
    "process_start_failed",
    "process_timeout",
    "spawn_context_hook_missing",
    "spawn_observation_controller_missing",
    "protocol_error",
    "turn_failed",
    "worker_authority_changed",
    "workspace_invalid",
    "workspace_mapping_missing",
})
WORKER_REFUSAL_CODES = frozenset({
    "authority_state_unavailable",
    "consumption_state_unavailable",
    "worker_adapter_absent",
    "worker_authority_changed",
    "worker_authority_ambiguous",
    "worker_authority_missing",
    "worker_claim_lost",
    "worker_node_inactive",
    "worker_work_blocked",
    "worker_work_absent",
    "worker_workspace_missing",
})


def validate_role(value: object, *, integrity: bool = False) -> str:
    """Validate the one durable registry-role lexical boundary."""

    error = IntegrityFailure if integrity else ProtocolRefusal

    def refuse(code: str, detail: str) -> None:
        raise error(code, detail)

    _bounded_string(value, 1, 64, "role", refuse)
    return value


def wake_hold_decision_digest(record: Mapping[str, object]) -> str:
    """Digest normalized wake-hold semantics, excluding physical id and time."""

    fields = (
        "recipient", "worker_session_id", "idempotency_key", "limit", "item_ids",
        "event_prefix_digest", "delivery_prefix_digest", "acknowledgment_prefix_digest",
    )
    payload = {field: record[field] for field in fields}
    limit = _json_integer(payload["limit"])
    if limit is not None:
        payload["limit"] = limit
    encoded = json.dumps(
        payload, ensure_ascii=False, allow_nan=False, sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(b"slipway-wake-hold-decision-v1\0" + encoded).hexdigest()


def validate_record(record: Any, expected_tenant: str, allowed_kinds: FrozenSet[str], *, integrity: bool) -> Dict[str, Any]:
    error = IntegrityFailure if integrity else ProtocolRefusal

    def refuse(code: str, detail: str) -> None:
        raise error(code, detail)

    if not isinstance(record, dict):
        refuse("record_not_object", "each durable record must be an object")
    chain_fields = frozenset({"seq", "prev"})
    present_chain_fields = frozenset(record) & chain_fields
    chain: Optional[Dict[str, object]] = None
    if present_chain_fields:
        if present_chain_fields != chain_fields:
            refuse(
                "journal_chain_fields_invalid",
                "chained records must carry seq and prev together",
            )
        seq = record.get("seq")
        prev = record.get("prev")
        if not isinstance(seq, int) or isinstance(seq, bool) or not 1 <= seq <= 2**63 - 1:
            refuse("journal_seq_invalid", "journal seq is outside its v-next bounds")
        if not isinstance(prev, str) or re.fullmatch(r"[0-9a-f]{64}", prev) is None:
            refuse("journal_prev_invalid", "journal prev must be a lowercase SHA-256 digest")
        chain = {"seq": seq, "prev": prev}
        record = {
            key: value for key, value in record.items() if key not in chain_fields
        }
    kind = record.get("kind")
    if not isinstance(kind, str) or kind not in allowed_kinds or kind not in _SPECS:
        refuse("record_kind_invalid", "record kind is not permitted by this ledger")
    prefix, fields = _SPECS[kind]
    normalized_version = _json_integer(record.get("schema_version"))
    if kind in _V1_FIELDS and normalized_version == 1:
        fields = _V1_FIELDS[kind]
    actual_fields = frozenset(record)
    valid_fields = actual_fields == fields
    if kind == "message_envelope":
        valid_fields = actual_fields in (
            fields,
            fields | {"reply_to"},
            fields | {"attempt_binding"},
            fields | {"worker_session_id"},
            fields | {"reply_to", "attempt_binding"},
            fields | {"reply_to", "worker_session_id"},
            fields | {"attempt_binding", "worker_session_id"},
            fields | {"reply_to", "attempt_binding", "worker_session_id"},
        )
    if kind == "ack_receipt" and normalized_version == 1:
        valid_fields = actual_fields in (
            _SPECS["ack_receipt"][1] | {"acting_session_id"},
            _V1_FIELDS["ack_receipt"],
        )
    if kind == "run_created":
        valid_fields = actual_fields in (fields, fields | {"policy_digest"})
    if kind == "task_contract":
        valid_fields = actual_fields in (fields, fields | {"repository"})
    if kind == "work_item":
        valid_fields = fields <= actual_fields <= fields | {"workspace", "needs"}
    if kind == "node_lease":
        valid_fields = actual_fields in (
            fields | {"expires_at"},
            fields | {"predecessor_lease_id"},
        )
    if kind == "dispatch_decision" and record.get("schema_version") == 1:
        valid_fields = actual_fields in (
            fields | {"capability_set_bound_id"},
            fields | {"capability_set_bound_id", "adapter", "attempt_spawn_policy_id"},
        )
    if (
        kind == "thread_attachment_registered"
        and record.get("subject_kind") == "attempt"
    ):
        valid_fields = actual_fields == fields | {"run_id", "attempt_id"}
    if not valid_fields:
        refuse("record_fields_invalid", f"{kind} fields do not match the exact record contract")
    if kind in _V1_FIELDS and normalized_version == 1:
        normalized = dict(record, schema_version=1)
        integer_fields = []
        if kind in {"approval_request", "approval_decision"}:
            integer_fields.append("authority_epoch")
        if kind == "approval_request":
            integer_fields.append("requested_ttl_seconds")
        elif kind == "approval_decision" and record["granted_ttl_seconds"] is not None:
            integer_fields.append("granted_ttl_seconds")
        elif kind == "result_accepted":
            integer_fields.append("effect_ledger_high_watermark")
        for field in integer_fields:
            integer_value = _json_integer(record[field])
            if integer_value is not None:
                normalized[field] = integer_value
        record = normalized
    if kind in SPAWN_GROUP_KINDS and normalized_version == 1:
        normalized = deepcopy(dict(record, schema_version=1))
        integer_fields = {
            "attempt_spawn_policy_bound": ("max_children", "max_depth"),
            "spawn_group_created": ("max_children", "max_depth", "required_count"),
            "spawn_group_aborted": ("authority_epoch",),
            "child_admitted": ("child_depth",),
            "spawn_late_result_disposition": ("authority_epoch",),
        }.get(kind, ())
        for field in integer_fields:
            if normalized[field] is None:
                continue
            integer_value = _json_integer(normalized[field])
            if integer_value is not None:
                normalized[field] = integer_value
        for field in ("aggregate_budget", "spawn_budget_ceiling", "budget_allocation"):
            if field in normalized and isinstance(normalized[field], list):
                normalized[field] = _normalized_budget_rows(normalized[field])
        record = normalized
    if kind == "plan_amendment" and normalized_version == 1:
        normalized = deepcopy(dict(record, schema_version=1))
        if isinstance(normalized.get("children"), list):
            for child in normalized["children"]:
                if not isinstance(child, dict):
                    continue
                depth = _json_integer(child.get("depth"))
                if depth is not None:
                    child["depth"] = depth
                if isinstance(child.get("budget_allocation"), list):
                    child["budget_allocation"] = _normalized_budget_rows(child["budget_allocation"])
        record = normalized
    if kind in {"attempt_suspended_for_approval", "approval_consumed_for_resume"} and normalized_version == 1:
        epoch_field = (
            "authority_epoch_at_request"
            if kind == "attempt_suspended_for_approval"
            else "resume_authority_epoch"
        )
        normalized_epoch = _json_integer(record[epoch_field])
        record = dict(record, schema_version=1)
        if normalized_epoch is not None:
            record[epoch_field] = normalized_epoch
    if kind in EFFECT_KINDS and normalized_version == 1:
        normalized = deepcopy(dict(record, schema_version=1))
        for field in ("budget_claim", "measured_spend"):
            if isinstance(normalized.get(field), list):
                normalized[field] = _normalized_effect_budget_rows(normalized[field])
        record = normalized
    if kind == "sequencer_epoch":
        normalized_version = _json_integer(record["schema_version"])
        if normalized_version != 1:
            refuse("schema_version_invalid", "sequencer_epoch must use integer version one")
        normalized_epoch = _json_integer(record["epoch"])
        if normalized_epoch is None or not 1 <= normalized_epoch <= 2**63 - 1:
            refuse("epoch_invalid", "epoch is outside its v0 integer bounds")
        record = dict(
            record,
            schema_version=normalized_version,
            epoch=normalized_epoch,
        )
    is_v1_record = (
        kind in ({"ack_receipt", "approval_request", "approval_decision", "approval_consumed_for_resume", "attempt_harness_session_bound", "attempt_suspended_for_approval", "bus_epoch_roll_receipt", "capability_grant", "capability_revoked", "capability_set_bound", "confluence_grant", "dispatch_decision", "result_accepted", "run_admission_bound", "segment_opened", "segment_sealed", "sequencer_epoch", "plan_amendment", "cancel_requested", "cancel_scope_resolved", "wake_hold_receipt", "wake_attempt_receipt", "codex_wait_consent_receipt", "codex_wait_session_receipt", "codex_wait_exhaustion_receipt", "wake_waiter_exit_receipt", "tide_policy_record", "tide_receipt", "wake_daemon_consent_receipt", "wake_daemon_lifecycle_receipt", "ledger_repair_receipt", "fleet_update_started", "fleet_update_step", "fleet_update_completed"} | SPAWN_GROUP_KINDS | TASK3_CANCELLATION_KINDS | EFFECT_KINDS | THREAD_OBSERVATION_KINDS)
        and isinstance(record["schema_version"], int)
        and not isinstance(record["schema_version"], bool)
        and record["schema_version"] == 1
    )
    if kind in {"segment_opened", "segment_sealed"} and (
        record["schema_version"] != 1 or isinstance(record["schema_version"], bool)
    ):
        refuse("schema_version_invalid", "schema_version must be integer one")
    if kind in {"run_admission_bound", "sequencer_epoch"} and (
        record["schema_version"] != 1 or isinstance(record["schema_version"], bool)
    ):
        refuse("schema_version_invalid", f"{kind} must use integer version one")
    if kind in {"attempt_suspended_for_approval", "approval_consumed_for_resume"} and normalized_version != 1:
        refuse("schema_version_invalid", f"{kind} must use integer version one")
    if kind in SPAWN_GROUP_KINDS and normalized_version != 1:
        refuse("schema_version_invalid", f"{kind} must use integer version one")
    if kind in TASK3_CANCELLATION_KINDS and normalized_version != 1:
        refuse("schema_version_invalid", f"{kind} must use integer version one")
    if kind in EFFECT_KINDS and normalized_version != 1:
        refuse("schema_version_invalid", "effect records require schema version 1")
    if kind in THREAD_OBSERVATION_KINDS and normalized_version != 1:
        refuse("schema_version_invalid", "thread observation records require schema version 1")
    if kind == "wake_hold_receipt" and normalized_version != 1:
        refuse("schema_version_invalid", "wake hold receipts require schema version 1")
    if kind == "wake_attempt_receipt" and normalized_version != 1:
        refuse("schema_version_invalid", "wake attempt receipts require schema version 1")
    if kind in {"codex_wait_consent_receipt", "codex_wait_session_receipt", "codex_wait_exhaustion_receipt", "wake_waiter_exit_receipt"} and normalized_version != 1:
        refuse("schema_version_invalid", "Codex wait receipts require schema version 1")
    if kind in {"fleet_update_started", "fleet_update_step", "fleet_update_completed"} and normalized_version != 1:
        refuse("schema_version_invalid", "fleet update receipts require schema version 1")
    if kind == "ledger_repair_receipt" and normalized_version != 1:
        refuse("schema_version_invalid", "ledger repair receipts require schema version 1")
    if kind == "bus_epoch_roll_receipt" and normalized_version != 1:
        refuse("schema_version_invalid", "bus epoch roll receipts require schema version 1")
    if not is_v1_record and kind not in {"segment_opened", "segment_sealed"} and (
        record["schema_version"] != 0 or isinstance(record["schema_version"], bool)
    ):
        refuse("schema_version_invalid", "schema_version must be integer zero")
    if record["tenant_id"] != expected_tenant:
        refuse("tenant_mismatch", "record tenant does not match the selected root")
    if not _identifier(record["tenant_id"]):
        refuse("tenant_invalid", "tenant identifier is invalid")
    if not isinstance(record["id"], str) or re.fullmatch(re.escape(prefix) + _UUID7, record["id"]) is None:
        refuse("record_id_invalid", "record id must use the kind's UUIDv7 prefix")
    _timestamp_value(record["timestamp"], "timestamp", refuse)
    # Effect rows are post-v1 and may bypass legacy run-record branches below.
    # Validate their shared closed binding at the durable boundary regardless.
    if kind in EFFECT_KINDS:
        _effect_binding(record, refuse)
        if kind == "effect_intent":
            ident = lambda field: _identifier(record[field]) or refuse(f"{field}_invalid", f"{field} must be an identifier")
            ident("requested_by"); _effect_approval_refs(record, refuse); _timestamp_value(record["intended_at_testimony"], "intended_at_testimony", refuse)
        elif kind == "effect_dispatched":
            _record_ref(record["effect_intent_id"], "effect-intent-", "effect_intent_id", refuse); _enum(record["dispatch_adapter"], EFFECT_DISPATCH_ADAPTERS, "dispatch_adapter", refuse); _sha256(record["dispatch_evidence_digest"], "dispatch_evidence_digest", refuse); _timestamp_value(record["dispatched_at_testimony"], "dispatched_at_testimony", refuse)
        elif kind == "effect_acknowledged":
            _record_ref(record["effect_intent_id"], "effect-intent-", "effect_intent_id", refuse); _record_ref(record["effect_dispatched_id"], "effect-dispatched-", "effect_dispatched_id", refuse); _sha256(record["acknowledgement_digest"], "acknowledgement_digest", refuse); _timestamp_value(record["acknowledged_at_testimony"], "acknowledged_at_testimony", refuse)
        elif kind == "effect_confirmed":
            _effect_outcome_refs(record, refuse, acknowledgement=True); _effect_confirmation(record["confirmation"], "confirmation", refuse); _sha256(record["confirmation_evidence_digest"], "confirmation_evidence_digest", refuse); _effect_budget_rows(record["measured_spend"], "measured_spend", refuse); _timestamp_value(record["confirmed_at_testimony"], "confirmed_at_testimony", refuse)
        elif kind in {"effect_failed", "effect_unknown"}:
            _effect_outcome_refs(record, refuse, acknowledgement=False); _enum(record["reason_code"], EFFECT_FAILURE_REASONS if kind == "effect_failed" else EFFECT_UNKNOWN_REASONS, "reason_code", refuse); _sha256(record["failure_evidence_digest" if kind == "effect_failed" else "unknown_evidence_digest"], "evidence_digest", refuse); _enum(record["spend_status"], EFFECT_SPEND_STATUSES, "spend_status", refuse); _effect_nullable_budget_rows(record["measured_spend"], "measured_spend", refuse); _timestamp_value(record["failed_at_testimony" if kind == "effect_failed" else "unknown_at_testimony"], "outcome_at_testimony", refuse)
        elif kind == "effect_reconciled":
            _record_ref(record["effect_intent_id"], "effect-intent-", "effect_intent_id", refuse); _effect_evidence_ref(record["prior_effect_evidence_id"], "prior_effect_evidence_id", refuse); _enum(record["reconciled_outcome"], {"confirmed", "failed", "unknown"}, "reconciled_outcome", refuse); _sha256(record["reconciliation_evidence_digest"], "reconciliation_evidence_digest", refuse); _effect_nullable_confirmation(record["confirmation"], refuse); _enum(record["spend_status"], EFFECT_SPEND_STATUSES, "spend_status", refuse); _effect_nullable_budget_rows(record["measured_spend"], "measured_spend", refuse); _timestamp_value(record["reconciled_at_testimony"], "reconciled_at_testimony", refuse)
        elif kind == "compensation_proposed":
            _record_ref(record["effect_intent_id"], "effect-intent-", "effect_intent_id", refuse); _effect_evidence_ref(record["source_effect_evidence_id"], "source_effect_evidence_id", refuse); _enum(record["reason_code"], EFFECT_COMPENSATION_REASONS, "reason_code", refuse); _sha256(record["compensation_plan_digest"], "compensation_plan_digest", refuse); _sha256(record["compensation_request_digest"], "compensation_request_digest", refuse); _effect_operation_id(record["compensation_operation_id"], "compensation_operation_id", refuse); _enum(record["compensation_risk_class"], {"low", "medium", "high", "critical"}, "compensation_risk_class", refuse); _effect_approval_refs(record, refuse); _timestamp_value(record["proposed_at_testimony"], "proposed_at_testimony", refuse)
        else:
            _record_ref(record["compensation_proposal_id"], "compensation-proposed-", "compensation_proposal_id", refuse); _effect_operation_id(record["compensation_operation_id"], "compensation_operation_id", refuse); _effect_evidence_ref(record["compensation_terminal_evidence_id"], "compensation_terminal_evidence_id", refuse); _timestamp_value(record["executed_at_testimony"], "executed_at_testimony", refuse)

    def ident(field: str) -> None:
        if not _identifier(record[field]):
            refuse(f"{field}_invalid", f"{field} must be a bounded identifier")

    def integer(field: str, minimum: int, maximum: int) -> None:
        value = record[field]
        if not isinstance(value, int) or isinstance(value, bool) or not minimum <= value <= maximum:
            refuse(f"{field}_invalid", f"{field} is outside its v0 integer bounds")

    if kind == "sequencer_epoch":
        integer("epoch", 1, 2**63 - 1)
        ident("sequencer_id")
        _enum(record["operation"], {"entered", "released", "takeover"}, "operation", refuse)
        _enum(
            record["absence_reason"],
            {"initial", "graceful_release", "host_local_owner_absent"},
            "absence_reason",
            refuse,
        )
        predecessor = record["previous_epoch_record_id"]
        if predecessor is not None:
            _record_ref(
                predecessor,
                "sequencer-epoch-",
                "previous_epoch_record_id",
                refuse,
            )
    elif kind == "segment_opened":
        integer("segment_number", 0, 2**63 - 1)
        integer("first_global_ordinal", 1, 2**63 - 1)
        integer("max_records", 1, 100000)
        integer("max_bytes", 65536, 64 * 1024 * 1024)
        predecessor = record["previous_seal_digest"]
        if record["segment_number"] == 0:
            if predecessor is not None:
                refuse("previous_seal_digest_invalid", "segment zero must have a null previous seal digest")
        elif predecessor is None:
            refuse("previous_seal_digest_invalid", "later segments must name the previous seal digest")
        else:
            _sha256(predecessor, "previous_seal_digest", refuse)
    elif kind == "segment_sealed":
        integer("segment_number", 0, 2**63 - 1)
        _record_ref(record["opening_record_id"], "run-segment-opened-", "opening_record_id", refuse)
        integer("last_global_ordinal", 1, 2**63 - 1)
        integer("record_count", 1, 100000)
        integer("byte_length", 1, 64 * 1024 * 1024)
        _sha256(record["segment_sha256"], "segment_sha256", refuse)
        _sha256(record["seal_digest"], "seal_digest", refuse)
        if record["seal_digest"] != segment_seal_digest(record):
            refuse("seal_digest_invalid", "seal_digest must cover the governed segment fields")
    elif kind == "registry_entry":
        ident("node_id")
        validate_role(record["role"], integrity=integrity)
        _enum(record["state"], {"active", "retired"}, "state", refuse)
    elif kind == "node_lease":
        ident("node_id")
        if not isinstance(record["workspace"], str) or not Path(record["workspace"]).is_absolute():
            refuse("node_workspace_invalid", "node lease workspace must be absolute")
        _enum(record["state"], {"active", "retired"}, "state", refuse)
        if record["state"] == "active":
            if "expires_at" not in record or "predecessor_lease_id" in record:
                refuse("node_lease_fields_invalid", "active lease requires expires_at only")
            expires = _timestamp_value(record["expires_at"], "expires_at", refuse)
            observed = _timestamp_value(record["timestamp"], "timestamp", refuse)
            if expires <= observed:
                refuse("node_lease_time_invalid", "active lease expiry must follow its timestamp")
        else:
            if "predecessor_lease_id" not in record or "expires_at" in record:
                refuse("node_lease_fields_invalid", "retired lease requires its predecessor only")
            _record_ref(
                record["predecessor_lease_id"], "lease-", "predecessor_lease_id", refuse
            )
    elif kind == "provider_switch_receipt":
        ident("node_id")
        _record_ref(
            record["previous_registry_entry_id"], "registry-",
            "previous_registry_entry_id", refuse,
        )
        _record_ref(record["registry_entry_id"], "registry-", "registry_entry_id", refuse)
        validate_role(record["previous_harness"], integrity=integrity)
        validate_role(record["harness"], integrity=integrity)
        model_pattern = re.compile(
            r"^[A-Za-z0-9](?:[A-Za-z0-9._:/-]{0,126}[A-Za-z0-9])?$"
        )
        for field in ("previous_model", "model"):
            value = record[field]
            if value is None and field == "previous_model":
                continue
            if not isinstance(value, str) or model_pattern.fullmatch(value) is None:
                refuse("model_invalid", f"{field} is not a bounded model identifier")
    elif kind in {"fleet_update_started", "fleet_update_step", "fleet_update_completed"}:
        _sha256(record["plan_digest"], "plan_digest", refuse)
        ident("actor")
        _record_ref(record["consent_receipt_id"], "update-consent-", "consent_receipt_id", refuse)
        _enum(record["operation"], {"start", "step", "complete"}, "operation", refuse)
        expected = {"fleet_update_started": "start", "fleet_update_step": "step", "fleet_update_completed": "complete"}[kind]
        if record["operation"] != expected:
            refuse("fleet_update_operation_invalid", "fleet update receipt kind and operation disagree")
        if kind == "fleet_update_started":
            witness = record["recovery_witness"]
            witness_fields = {
                "schema_version", "kind", "inputs", "current_source_sha",
                "target_source_sha", "current_manifest_sha256",
                "target_manifest_sha256", "binding_inventory_sha256",
                "transport_registry_sha256", "target_transport_registry_sha256",
                "waiter_bindings", "target_waiter_digest", "current_encoder_sha256",
                "target_encoder_sha256", "current_transport_pins",
                "target_transport_pins", "current_managed_paths",
                "shared_install_intents", "reader_consequences",
                "seat_binding_consequences", "seat_exclusions",
                "owner_review_batch_digest", "requires_epoch_roll", "stale_pins",
                "moves", "unchanged",
            }
            if not isinstance(witness, dict) or set(witness) != witness_fields:
                refuse("fleet_update_receipt_invalid", "fleet_update_receipt_invalid")
            try:
                witness_digest = hashlib.sha256(json.dumps(
                    witness, ensure_ascii=True, allow_nan=False, sort_keys=True,
                    separators=(",", ":"),
                ).encode("ascii")).hexdigest()
            except (TypeError, ValueError):
                refuse("fleet_update_receipt_invalid", "fleet_update_receipt_invalid")
            if witness_digest != record["plan_digest"]:
                refuse("fleet_update_receipt_invalid", "fleet_update_receipt_invalid")
            try:
                # Import locally: receipt code imports this generic record validator,
                # while start-row validation must also enforce plan derivation.
                from .fleet_update_receipts import authenticate_plan

                authenticate_plan(
                    {**witness, "plan_digest": record["plan_digest"], "apply_argv": []},
                    record["actor"],
                )
            except ProtocolRefusal:
                refuse("fleet_update_receipt_invalid", "fleet_update_receipt_invalid")
        if record["step_kind"] is not None and record["step_kind"] not in {"shared_install", "waiter_binding", "transport_pins", "epoch_roll"}:
            refuse("fleet_update_step_invalid", "fleet update step kind is invalid")
        if kind == "fleet_update_step" and record["step_kind"] is None:
            refuse("fleet_update_step_invalid", "fleet update step requires its kind")
        if kind != "fleet_update_step" and record["step_kind"] is not None:
            refuse("fleet_update_step_invalid", "fleet update terminal rows cannot name a step")
        phase_fields = ("step_ordinal", "step_coordinate", "commit_disposition", "step_evidence")
        if kind != "fleet_update_step":
            if any(record[field] is not None for field in phase_fields):
                refuse("fleet_update_step_invalid", "terminal receipt has step evidence")
        else:
            if type(record["step_ordinal"]) is not int or record["step_ordinal"] < 1 or not isinstance(record["step_coordinate"], dict) or not isinstance(record["step_evidence"], dict) or record["commit_disposition"] not in {"applied", "recovered_post_state", "unchanged"}:
                refuse("fleet_update_step_invalid", "step receipt phase evidence is invalid")
            if (
                record["step_kind"] == "shared_install"
                and record["commit_disposition"] == "unchanged"
            ):
                refuse(
                    "fleet_update_step_invalid",
                    "shared install cannot have an unchanged disposition",
                )
            if record["step_coordinate"].get("kind") != record["step_kind"] or record["step_evidence"].get("kind") != record["step_kind"]:
                refuse("fleet_update_step_invalid", "step receipt evidence discriminator is invalid")
            coordinate = record["step_coordinate"]
            if record["step_kind"] == "shared_install":
                if (
                    set(coordinate) != {"kind", "destination", "metadata_relative"}
                    or not isinstance(coordinate.get("destination"), str)
                    or not coordinate["destination"].startswith("/")
                    or coordinate.get("metadata_relative")
                    != ".floati-install/manifest.v0.json"
                ):
                    refuse("fleet_update_step_invalid", "shared install coordinate is invalid")
            elif record["step_kind"] == "waiter_binding":
                if (
                    set(coordinate) != {"kind", "index", "configuration", "store", "trust_key"}
                    or type(coordinate.get("index")) is not int
                    or coordinate["index"] < 0
                    or not isinstance(coordinate.get("configuration"), str)
                    or not coordinate["configuration"].startswith("/")
                    or not isinstance(coordinate.get("store"), str)
                    or not coordinate["store"].startswith("/")
                    or coordinate.get("trust_key") is not None and not isinstance(coordinate.get("trust_key"), str)
                ):
                    refuse("fleet_update_step_invalid", "waiter binding coordinate is invalid")
            elif record["step_kind"] == "transport_pins":
                if (
                    set(coordinate) != {"kind", "registry", "transport"}
                    or not isinstance(coordinate.get("registry"), str)
                    or not coordinate["registry"].startswith("/")
                    or not _identifier(coordinate.get("transport"))
                ):
                    refuse("fleet_update_step_invalid", "transport pin coordinate is invalid")
            elif set(coordinate) != {"kind"}:
                refuse("fleet_update_step_invalid", "non-G2 step coordinate is invalid")
            if record["step_kind"] == "shared_install":
                evidence = record["step_evidence"]
                required_evidence = {
                    "kind", "journal_path", "join_id", "predecessor_ordinal",
                    "predecessor_entry_hash", "first_ordinal", "last_ordinal",
                    "entry_hashes",
                }
                if set(evidence) != required_evidence:
                    refuse("fleet_update_step_invalid", "shared install journal evidence has an invalid shape")
                if (
                    not isinstance(evidence["journal_path"], str)
                    or not evidence["journal_path"].startswith("/")
                    or not isinstance(evidence["join_id"], str)
                    or re.fullmatch(r"[0-9a-f]{64}", evidence["join_id"]) is None
                    or type(evidence["first_ordinal"]) is not int
                    or evidence["first_ordinal"] < 1
                    or type(evidence["last_ordinal"]) is not int
                    or evidence["last_ordinal"] < evidence["first_ordinal"]
                    or not isinstance(evidence["entry_hashes"], list)
                    or len(evidence["entry_hashes"]) != evidence["last_ordinal"] - evidence["first_ordinal"] + 1
                    or any(not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None for value in evidence["entry_hashes"])
                ):
                    refuse("fleet_update_step_invalid", "shared install journal evidence is invalid")
                previous_ordinal = evidence["predecessor_ordinal"]
                previous_hash = evidence["predecessor_entry_hash"]
                if previous_ordinal is None:
                    if previous_hash is not None or evidence["first_ordinal"] != 1:
                        refuse("fleet_update_step_invalid", "shared install journal predecessor is invalid")
                elif (
                    type(previous_ordinal) is not int
                    or previous_ordinal < 1
                    or previous_ordinal + 1 != evidence["first_ordinal"]
                    or not isinstance(previous_hash, str)
                    or re.fullmatch(r"[0-9a-f]{64}", previous_hash) is None
                ):
                    refuse("fleet_update_step_invalid", "shared install journal predecessor is invalid")
            if record["step_kind"] == "waiter_binding":
                evidence = record["step_evidence"]
                if set(evidence) != {"kind", "hook_post_observation"} or not isinstance(evidence.get("hook_post_observation"), dict):
                    refuse("fleet_update_step_invalid", "waiter post-trust evidence has an invalid shape")
                observation = evidence["hook_post_observation"]
                if set(observation) != {"hook_trust_key", "current_hook_hash", "observed_trusted_hash", "observed_enabled"}:
                    refuse("fleet_update_step_invalid", "waiter post-trust observation has an invalid shape")
                if (
                    not isinstance(observation["hook_trust_key"], str)
                    or not isinstance(observation["current_hook_hash"], str)
                    or re.fullmatch(r"[0-9a-f]{64}", observation["current_hook_hash"]) is None
                    or observation["observed_trusted_hash"] is not None and (not isinstance(observation["observed_trusted_hash"], str) or re.fullmatch(r"[0-9a-f]{64}", observation["observed_trusted_hash"]) is None)
                    or observation["observed_enabled"] is not None and type(observation["observed_enabled"]) is not bool
                ):
                    refuse("fleet_update_step_invalid", "waiter post-trust observation is invalid")
            if record["step_kind"] == "transport_pins":
                evidence = record["step_evidence"]
                if set(evidence) != {
                    "kind", "registry", "transport", "registry_before_sha256",
                    "registry_after_sha256", "previous_source_sha",
                    "target_source_sha", "epoch_roll_state",
                }:
                    refuse("fleet_update_step_invalid", "transport pin evidence has an invalid shape")
                if (
                    not isinstance(evidence["registry"], str)
                    or evidence["registry"] != coordinate["registry"]
                    or not isinstance(evidence["transport"], str)
                    or evidence["transport"] != coordinate["transport"]
                    or not isinstance(evidence["registry_before_sha256"], str)
                    or not isinstance(evidence["registry_after_sha256"], str)
                    or not isinstance(evidence["previous_source_sha"], str)
                    or not isinstance(evidence["target_source_sha"], str)
                    or not isinstance(evidence["epoch_roll_state"], str)
                    or re.fullmatch(r"[0-9a-f]{64}", evidence["registry_before_sha256"]) is None
                    or re.fullmatch(r"[0-9a-f]{64}", evidence["registry_after_sha256"]) is None
                    or re.fullmatch(r"[0-9a-f]{40}", evidence["previous_source_sha"]) is None
                    or re.fullmatch(r"[0-9a-f]{40}", evidence["target_source_sha"]) is None
                    or evidence["epoch_roll_state"] not in {"not_required", "completed"}
                ):
                    refuse("fleet_update_step_invalid", "transport pin evidence is invalid")
        for field in ("pre_digest", "post_digest"):
            value = record[field]
            if (kind == "fleet_update_step" and (not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None)) or (kind != "fleet_update_step" and value is not None):
                refuse("fleet_update_digest_invalid", "fleet update pre/post digest fields are invalid")
        _enum(record["state"], {"started", "completed"}, "state", refuse)
        if (kind == "fleet_update_started") != (record["state"] == "started"):
            refuse("fleet_update_state_invalid", "fleet update receipt state is invalid")
        predecessor = record["predecessor_receipt_id"]
        if predecessor is not None:
            if not isinstance(predecessor, str) or re.fullmatch(r"fleet-update-(?:started|step|completed)-" + _UUID7, predecessor) is None:
                refuse("predecessor_receipt_id_invalid", "fleet update predecessor receipt id is invalid")
        if not isinstance(record["idempotency_key"], str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", record["idempotency_key"]):
            refuse("idempotency_key_invalid", "fleet update idempotency key is invalid")
        _sha256(record["owner_review_batch_digest"], "owner_review_batch_digest", refuse)
        if not isinstance(record["reader_consequences"], list) or not isinstance(record["seat_binding_consequences"], list) or not isinstance(record["seat_exclusions"], list):
            refuse("fleet_update_owner_review_invalid", "fleet update owner review arrays are invalid")
        readers = record["reader_consequences"]
        consequences = record["seat_binding_consequences"]
        exclusions = record["seat_exclusions"]
        try:
            batch_payload = json.dumps({"reader_consequences": readers, "seat_binding_consequences": consequences, "seat_exclusions": exclusions}, ensure_ascii=True, allow_nan=False, sort_keys=True, separators=(",", ":")).encode("ascii")
        except (TypeError, ValueError):
            refuse(
                "fleet_update_owner_review_invalid",
                "fleet update owner review evidence is not canonical JSON",
            )
        if record["owner_review_batch_digest"] != hashlib.sha256(batch_payload).hexdigest():
            refuse("fleet_update_owner_review_invalid", "fleet update owner review digest is not canonical")
        reader_fields = {"reader", "surface", "registry", "transport", "manifest_path", "current_schema_version", "target_schema_version", "added_fields", "removed_fields", "change", "compatibility_after_update", "remedy"}
        if len(readers) > 1:
            refuse("fleet_update_owner_review_invalid", "fleet update repeats a reader consequence")
        for row in readers:
            if not isinstance(row, dict) or set(row) != reader_fields:
                refuse("fleet_update_owner_review_invalid", "fleet update reader consequence has an invalid shape")
            if (
                row["reader"] != "codex_fleet_bus_gateway"
                or row["surface"] != "install_manifest"
                or type(row["current_schema_version"]) is not int
                or type(row["target_schema_version"]) is not int
                or row["current_schema_version"] != 0
                or row["target_schema_version"] != 1
                or row["added_fields"] != ["ownership"]
                or row["removed_fields"] != []
                or row["change"] != "additive_widened"
                or row["compatibility_after_update"] != "not_observed"
                or row["remedy"] != "review the Codex fleet gateway reader before applying the widened manifest vocabulary"
            ):
                refuse("fleet_update_owner_review_invalid", "fleet update reader consequence formula is invalid")
            for field in ("registry", "manifest_path"):
                value = row[field]
                if not isinstance(value, str) or not Path(value).is_absolute() or str(Path(value)) != value:
                    refuse("fleet_update_owner_review_invalid", "fleet update reader consequence coordinate is invalid")
            if not _identifier(row["transport"]):
                refuse("fleet_update_owner_review_invalid", "fleet update reader consequence coordinate is invalid")
        consequence_fields = {"node_id", "workspace", "harness", "configuration", "store", "association_basis", "hook_trust_key", "current_hook_hash", "target_hook_hash", "observed_trusted_hash", "observed_enabled", "current_waiter_digest", "target_waiter_digest", "trust_rotated_by_update", "review_required_after_update", "enable_required_after_update", "relaunch_required_after_update", "reachability_after_update", "remedy"}
        exclusion_fields = {"node_id", "workspace", "authoritative_state", "harness", "reason"}
        consequence_keys = []
        for row in consequences:
            if not isinstance(row, dict) or set(row) != consequence_fields:
                refuse("fleet_update_owner_review_invalid", "fleet update consequence has an invalid shape")
            if not _identifier(row["node_id"]) or not all(isinstance(row[field], str) and row[field].startswith("/") for field in ("workspace", "configuration", "store")):
                refuse("fleet_update_owner_review_invalid", "fleet update consequence coordinate is invalid")
            validate_role(row["harness"], integrity=integrity)
            if row["association_basis"] != "conservative_root_scope" or not isinstance(row["hook_trust_key"], str) or not row["hook_trust_key"]:
                refuse("fleet_update_owner_review_invalid", "fleet update consequence association is invalid")
            for field in ("current_hook_hash", "target_hook_hash", "current_waiter_digest", "target_waiter_digest"):
                _sha256(row[field], field, refuse)
            if row["observed_trusted_hash"] is not None:
                _sha256(row["observed_trusted_hash"], "observed_trusted_hash", refuse)
            for field in ("observed_enabled", "trust_rotated_by_update", "review_required_after_update", "enable_required_after_update", "relaunch_required_after_update"):
                if not isinstance(row[field], bool):
                    refuse("fleet_update_owner_review_invalid", "fleet update consequence boolean is invalid")
            rotated = row["current_hook_hash"] != row["target_hook_hash"]
            review = row["target_hook_hash"] != row["observed_trusted_hash"]
            enable = row["observed_enabled"] is not True
            relaunch = rotated or review or enable
            remedies = []
            if enable:
                remedies.append("enable the exact Stop hook in Codex settings")
            if review:
                remedies.append("review and trust the exact Stop hook in Codex settings")
            if relaunch:
                remedies.append("relaunch the affected session")
            if (row["trust_rotated_by_update"], row["review_required_after_update"], row["enable_required_after_update"], row["relaunch_required_after_update"]) != (rotated, review, enable, relaunch):
                refuse("fleet_update_owner_review_invalid", "fleet update consequence flags are not derived from hook observation")
            if row["reachability_after_update"] != ("unknown_until_review_and_relaunch" if relaunch else "not_observed") or row["remedy"] != (";".join(remedies) if remedies else None):
                refuse("fleet_update_owner_review_invalid", "fleet update consequence disposition is invalid")
            consequence_keys.append((row["node_id"], row["workspace"], row["configuration"]))
        if consequence_keys != sorted(consequence_keys) or len(set(consequence_keys)) != len(consequence_keys):
            refuse("fleet_update_owner_review_invalid", "fleet update consequences are not sorted and unique")
        exclusion_keys = []
        for row in exclusions:
            if not isinstance(row, dict) or set(row) != exclusion_fields:
                refuse("fleet_update_owner_review_invalid", "fleet update exclusion has an invalid shape")
            if not _identifier(row["node_id"]) or not isinstance(row["workspace"], str) or not row["workspace"].startswith("/"):
                refuse("fleet_update_owner_review_invalid", "fleet update exclusion coordinate is invalid")
            validate_role(row["harness"], integrity=integrity)
            allowed_exclusions = {("retired", "node_retired"), ("lease_expired", "lease_expired"), ("active", "harness_not_codex")}
            if (row["authoritative_state"], row["reason"]) not in allowed_exclusions:
                refuse("fleet_update_owner_review_invalid", "fleet update exclusion disposition is invalid")
            exclusion_keys.append((row["node_id"], row["workspace"], row["reason"]))
        if exclusion_keys != sorted(exclusion_keys) or len(set(exclusion_keys)) != len(exclusion_keys):
            refuse("fleet_update_owner_review_invalid", "fleet update exclusions are not sorted and unique")
        if kind == "fleet_update_completed":
            if (
                not isinstance(record["previous_source_sha"], str)
                or re.fullmatch(r"[0-9a-f]{40}", record["previous_source_sha"]) is None
                or not isinstance(record["target_source_sha"], str)
                or re.fullmatch(r"[0-9a-f]{40}", record["target_source_sha"]) is None
                or not isinstance(record["epoch_roll_state"], str)
                or record["epoch_roll_state"] not in {"not_required", "completed"}
                or any(not isinstance(record[field], str) or re.fullmatch(r"[0-9a-f]{64}", record[field]) is None for field in ("registry_before_sha256", "registry_after_sha256"))
            ):
                refuse("fleet_update_completion_invalid", "fleet update terminal projection is invalid")
            if not isinstance(record["step_receipt_ids"], list) or not isinstance(record["moves"], list) or not isinstance(record["unchanged"], list):
                refuse("fleet_update_completion_invalid", "fleet update completion fields are invalid")
            steps = record["step_receipt_ids"]
            if len(steps) != len(set(steps)):
                refuse("fleet_update_completion_invalid", "fleet update completion repeats a step receipt")
            for value in steps:
                _record_ref(value, "fleet-update-step-", "step_receipt_ids", refuse)
            def absolute(value: object) -> bool:
                return (
                    isinstance(value, str)
                    and Path(value).is_absolute()
                    and str(Path(value)) == value
                )

            def pins(value: object) -> bool:
                return (
                    isinstance(value, dict)
                    and set(value) == {"manifest_sha256", "source_sha"}
                    and isinstance(value["manifest_sha256"], str)
                    and re.fullmatch(r"[0-9a-f]{64}", value["manifest_sha256"]) is not None
                    and isinstance(value["source_sha"], str)
                    and re.fullmatch(r"[0-9a-f]{40}", value["source_sha"]) is not None
                )

            def action_row(row: object, *, allow_generation: bool) -> tuple[str, str]:
                if not isinstance(row, dict) or not isinstance(row.get("kind"), str):
                    refuse("fleet_update_completion_invalid", "fleet update completion fields are invalid")
                kind = row["kind"]
                path = row.get("path")
                if not absolute(path):
                    refuse("fleet_update_completion_invalid", "fleet update completion fields are invalid")
                if kind == "shared_install":
                    if set(row) != {"kind", "path", "from", "to"}:
                        refuse("fleet_update_completion_invalid", "fleet update completion fields are invalid")
                    _sha256(row["from"], "moves.from", refuse)
                    _sha256(row["to"], "moves.to", refuse)
                elif kind == "transport_pins":
                    if set(row) != {"kind", "path", "from", "to"} or not pins(row["from"]) or not pins(row["to"]):
                        refuse("fleet_update_completion_invalid", "fleet update completion fields are invalid")
                elif kind == "waiter_binding":
                    if set(row) != {
                        "kind", "path", "store", "configuration_from_sha256",
                        "configuration_to_sha256", "current_tree_digest",
                        "target_tree_digest",
                    } or not absolute(row.get("store")):
                        refuse("fleet_update_completion_invalid", "fleet update completion fields are invalid")
                    for field in (
                        "configuration_from_sha256", "configuration_to_sha256",
                        "current_tree_digest", "target_tree_digest",
                    ):
                        _sha256(row[field], "moves." + field, refuse)
                elif kind == "waiter_generation" and allow_generation:
                    if set(row) != {
                        "kind", "path", "named_tree_digest", "current_tree_digest",
                        "retained",
                    } or row["retained"] is not True:
                        refuse("fleet_update_completion_invalid", "fleet update completion fields are invalid")
                    _sha256(row["named_tree_digest"], "unchanged.named_tree_digest", refuse)
                    _sha256(row["current_tree_digest"], "unchanged.current_tree_digest", refuse)
                else:
                    refuse("fleet_update_completion_invalid", "fleet update completion fields are invalid")
                return kind, str(path)

            move_keys = [action_row(row, allow_generation=False) for row in record["moves"]]
            unchanged_keys = [action_row(row, allow_generation=True) for row in record["unchanged"]]
            if (
                move_keys != sorted(move_keys)
                or unchanged_keys != sorted(unchanged_keys)
                or len(move_keys) != len(set(move_keys))
                or len(unchanged_keys) != len(set(unchanged_keys))
                or set(move_keys) & set(unchanged_keys)
            ):
                refuse("fleet_update_completion_invalid", "fleet update completion fields are invalid")
    elif kind == "registry_role_record":
        ident("node_id")
        ident("template_role")
        integer("template_version", 1, 1000000)
        _sha256(record["template_sha256"], "template_sha256", refuse)
        if not isinstance(record["answers"], dict) or not 1 <= len(record["answers"]) <= 32:
            refuse("role_answers_invalid", "role answers must be a non-empty bounded object")
        for key, value in record["answers"].items():
            if not _identifier(key) or not isinstance(value, str) or not 1 <= len(value) <= 500:
                refuse("role_answers_invalid", "role answers are malformed")
        _enum(record["state"], {"active"}, "state", refuse)
        predecessor = record["predecessor_role_record_id"]
        if predecessor is not None:
            _record_ref(
                predecessor, "registry-role-", "predecessor_role_record_id", refuse
            )
    elif kind == "lane_spawn_receipt":
        ident("profile")
        ident("node_id")
        ident("actor")
        integer("ordinal", 1, 1000000)
        _enum(record["state"], {"complete", "spawn_incomplete"}, "state", refuse)
        if record["failing_step"] is not None:
            _enum(
                record["failing_step"],
                {"workspace", "registry"},
                "failing_step",
                refuse,
            )
        if record["state"] == "complete" and record["failing_step"] is not None:
            refuse("lane_spawn_receipt_invalid", "complete spawn cannot name a failing step")
        artifacts = record["artifacts"]
        if not isinstance(artifacts, dict) or set(artifacts) != {
            "workspace", "registry_entry_id", "role_record_id", "lease_id",
            "committer_name", "committer_email",
        }:
            refuse("lane_spawn_artifacts_invalid", "spawn artifacts are malformed")
        for field, value in artifacts.items():
            if field == "lease_id" and value is None:
                continue
            _bounded_string(value, 1, 4096, "spawn artifact", refuse)
        compensated = record["compensated"]
        if (
            not isinstance(compensated, list)
            or len(compensated) > 8
            or any(value not in {"workspace"} for value in compensated)
        ):
            refuse("lane_spawn_compensation_invalid", "spawn compensation is malformed")
    elif kind == "lane_teardown_receipt":
        ident("profile")
        ident("node_id")
        ident("actor")
        integer("ordinal", 1, 1000000)
        _enum(record["state"], {"complete"}, "state", refuse)
        for field in ("removed", "retained"):
            values = record[field]
            if (
                not isinstance(values, list)
                or not 1 <= len(values) <= 32
                or any(not isinstance(value, str) or not 1 <= len(value) <= 4096 for value in values)
            ):
                refuse("lane_teardown_receipt_invalid", f"{field} testimony is malformed")
    elif kind == "quota_receipt_record":
        ident("provider")
        _bounded_string(record["endpoint_id"], 1, 256, "endpoint_id", refuse)
        facts = record["facts"]
        if not isinstance(facts, list) or not 1 <= len(facts) <= 16:
            refuse("quota_facts_invalid", "quota facts must be a bounded nonempty list")
        fact_keys = []
        fact_identities = []
        for fact in facts:
            if not isinstance(fact, dict) or set(fact) != {
                "provider", "surface", "state", "stamp", "source",
                "evidence_digest", "observed_at", "resets_at",
            }:
                refuse("quota_fact_invalid", "quota fact fields are malformed")
            if fact["provider"] != record["provider"]:
                refuse("quota_fact_invalid", "quota fact provider does not match its receipt")
            _bounded_string(fact["surface"], 1, 256, "quota surface", refuse)
            state = fact["state"]
            if not isinstance(state, dict) or set(state) != {"kind", "value"}:
                refuse("quota_state_invalid", "quota state is malformed")
            _enum(state["kind"], {"consumed_fraction", "session_tokens", "unknown"}, "quota state kind", refuse)
            value = state["value"]
            if state["kind"] == "unknown":
                if value is not None:
                    refuse("quota_state_invalid", "unknown quota state cannot carry a value")
            elif state["kind"] == "consumed_fraction":
                if not isinstance(value, str) or re.fullmatch(r"(?:0\.[0-9]{6}|1\.000000)", value) is None:
                    refuse("quota_state_invalid", "quota fraction is not canonical")
            elif not isinstance(value, str) or re.fullmatch(r"(?:0|[1-9][0-9]*)", value) is None:
                refuse("quota_state_invalid", "quota token count is not canonical")
            _enum(fact["stamp"], {"MEASURED", "DERIVED", "ESTIMATE"}, "quota stamp", refuse)
            _bounded_string(fact["source"], 1, 4096, "quota source", refuse)
            _sha256(fact["evidence_digest"], "evidence_digest", refuse)
            observed = _timestamp_value(fact["observed_at"], "observed_at", refuse)
            reset = fact["resets_at"]
            if reset is not None and _timestamp_value(reset, "resets_at", refuse) < observed:
                refuse("resets_at_invalid", "quota reset precedes its observation")
            fact_keys.append((fact["surface"], fact["observed_at"], fact["evidence_digest"]))
            fact_identities.append((fact["surface"], fact["observed_at"]))
        if fact_keys != sorted(fact_keys) or len(set(fact_identities)) != len(fact_identities):
            refuse("quota_facts_invalid", "quota facts must be ordered and unique")
        _bounded_string(record["idempotency_key"], 1, 128, "idempotency_key", refuse)
        _sha256(record["receipt_digest"], "receipt_digest", refuse)
    elif kind == "tide_policy_record":
        ident("node_id")
        _bounded_string(record["harness"], 1, 64, "harness", refuse)
        ident("metric")
        if not isinstance(record["threshold"], str) or re.fullmatch(r"(?:0\.[0-9]{6}|[1-9][0-9]*)", record["threshold"]) is None:
            refuse("tide_threshold_invalid", "tide threshold is not canonical numeric text")
        _enum(record["action"], {"recommend", "direct"}, "action", refuse)
        _enum(record["access_class"], {"A", "B"}, "access_class", refuse)
        _enum(
            record["stamp"],
            {"DERIVED", "SELF_REPORTED", "MEASURED_OR_DERIVED"}
            if normalized_version == 1
            else {"DERIVED", "SELF_REPORTED"},
            "stamp",
            refuse,
        )
        _bounded_string(record["formula"], 1, 500, "formula", refuse)
        _repository_document(record["receipt_path"], refuse)
        _enum(record["state"], {"active", "cleared"}, "state", refuse)
        predecessor = record["predecessor_policy_id"]
        if predecessor is not None:
            _record_ref(predecessor, "tide-policy-", "predecessor_policy_id", refuse)
        if record["state"] == "cleared" and predecessor is None:
            refuse("tide_policy_predecessor_invalid", "cleared tide policy needs its predecessor")
        _bounded_string(record["idempotency_key"], 1, 128, "idempotency_key", refuse)
    elif kind == "tide_receipt":
        ident("node_id")
        _bounded_string(record["harness"], 1, 64, "harness", refuse)
        _record_ref(record["policy_id"], "tide-policy-", "policy_id", refuse)
        ident("metric")
        for field in ("value", "threshold"):
            if not isinstance(record[field], str) or re.fullmatch(r"(?:0|0\.[0-9]+|[1-9][0-9]*(?:\.[0-9]+)?)", record[field]) is None:
                refuse(f"tide_{field}_invalid", f"tide {field} is not canonical numeric text")
        _enum(
            record["stamp"],
            {"MEASURED", "DERIVED", "SELF_REPORTED"}
            if normalized_version == 1
            else {"DERIVED", "SELF_REPORTED"},
            "stamp",
            refuse,
        )
        _enum(record["access_class"], {"A", "B"}, "access_class", refuse)
        _bounded_string(record["formula"], 1, 500, "formula", refuse)
        sources = record["sources"]
        if not isinstance(sources, list) or not 1 <= len(sources) <= 16:
            refuse("tide_sources_invalid", "tide sources must be a bounded nonempty list")
        for source in sources:
            _bounded_string(source, 1, 4096, "source", refuse)
        _enum(record["evaluation_state"], {"crossed", "recommended", "directed", "rearmed", "state_flushed"}, "evaluation_state", refuse)
        _enum(record["action"], {"recommend", "direct"}, "action", refuse)
        integer("crossing_epoch", 1, 2**63 - 1)
        for field, prefix in (
            ("crossing_receipt_id", "tide-receipt-"),
            ("state_flush_receipt_id", "node-state-flush-"),
            ("envelope_id", "msg-"),
        ):
            value = record[field]
            if value is not None:
                _record_ref(value, prefix, field, refuse)
        _bounded_string(record["idempotency_key"], 1, 128, "idempotency_key", refuse)
    elif kind == "tide_testimony_record":
        ident("node_id")
        _bounded_string(record["harness"], 1, 64, "harness", refuse)
        ident("metric")
        if not isinstance(record["value"], str) or re.fullmatch(r"0\.[0-9]{6}", record["value"]) is None:
            refuse("tide_value_invalid", "testimony value must be a canonical fraction")
        _enum(record["command"], {"/context", "/status", "/usage", "/cost"}, "command", refuse)
        _enum(record["access_class"], {"B"}, "access_class", refuse)
        _enum(record["stamp"], {"SELF_REPORTED"}, "stamp", refuse)
        _bounded_string(record["formula"], 1, 500, "formula", refuse)
        _repository_document(record["receipt_path"], refuse)
        _bounded_string(record["idempotency_key"], 1, 128, "idempotency_key", refuse)
    elif kind == "message_envelope":
        ident("sender"); ident("recipient")
        _repository(record["repo"], refuse)
        _git_sha(record["sha"], refuse)
        _repository_document(record["doc"], refuse)
        _bounded_note(record["note"], refuse)
        _bounded_string(record["idempotency_key"], 1, 128, "idempotency_key", refuse)
        if "reply_to" in record and (
            not isinstance(record["reply_to"], str)
            or re.fullmatch("msg-" + _UUID7, record["reply_to"]) is None
        ):
            refuse("reply_to_invalid", "reply_to must use the message UUIDv7 prefix")
        if "worker_session_id" in record:
            _opaque_identifier(record["worker_session_id"], "worker_session_id", refuse)
        if "attempt_binding" in record:
            _attempt_binding(
                record["attempt_binding"], record.get("worker_session_id"), refuse
            )
    elif kind == "delivery_claim":
        if not isinstance(record["sha"], str) or re.fullmatch(r"[0-9a-f]{40}", record["sha"]) is None:
            refuse("claim_sha_invalid", "delivery claim sha must be exactly 40 lowercase hex characters")
        repo_path = record["repo_path"]
        if (
            not isinstance(repo_path, str)
            or not 1 <= len(repo_path) <= 4096
            or _terminal_unsafe(repo_path)
            or not Path(repo_path).is_absolute()
            or any(part in {"", ".", ".."} for part in Path(repo_path).parts)
        ):
            refuse("claim_repo_path_invalid", "delivery claim repo_path must be one canonical absolute path")
        bank = record["bank"]
        if bank != "discover":
            if not isinstance(bank, list) or not 1 <= len(bank) <= 128:
                refuse("claim_bank_invalid", "delivery claim bank must be discover or a bounded module list")
            module_pattern = re.compile(
                r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*\Z"
            )
            if any(
                not isinstance(module, str)
                or len(module) > 256
                or module_pattern.fullmatch(module) is None
                for module in bank
            ) or bank != list(dict.fromkeys(bank)):
                refuse("claim_bank_invalid", "delivery claim bank modules must be unique import names")
        declared = record["declared"]
        if not isinstance(declared, dict) or set(declared) != {"ran", "result"}:
            refuse("claim_declared_invalid", "delivery claim declared result must name ran and result")
        ran = _json_integer(declared.get("ran"))
        if ran is None or not 0 <= ran <= 10_000_000:
            refuse("claim_declared_invalid", "delivery claim ran count is outside its v0 bounds")
        _enum(declared.get("result"), {"OK", "FAILED"}, "claim_declared_result", refuse)
        artifacts = record["artifacts"]
        if not isinstance(artifacts, list) or len(artifacts) > 256:
            refuse("claim_artifacts_invalid", "delivery claim artifacts must be a bounded list")
        artifact_paths: list[str] = []
        for artifact in artifacts:
            if not isinstance(artifact, dict) or set(artifact) != {"path", "sha256"}:
                refuse("claim_artifacts_invalid", "each claimed artifact must name path and sha256")
            path = artifact["path"]
            if (
                not isinstance(path, str)
                or not 1 <= len(path) <= 1024
                or _terminal_unsafe(path)
                or path.startswith("/")
                or any(part in {"", ".", ".."} for part in path.split("/"))
            ):
                refuse("claim_artifact_path_invalid", "claimed artifact path must stay repository-relative")
            _sha256(artifact["sha256"], "claim_artifact_sha256", refuse)
            artifact_paths.append(path)
        if artifact_paths != list(dict.fromkeys(artifact_paths)):
            refuse("claim_artifacts_invalid", "claimed artifact paths must be unique")
        _record_ref(record["note_ref"], "msg-", "note_ref", refuse)
        integer("deadline_seconds", 1, 3600)
    elif kind == "verification_receipt":
        _record_ref(record["claim_id"], "delivery-claim-", "claim_id", refuse)
        ident("verifier")
        _enum(
            record["outcome"],
            {"verified_match", "verified_mismatch", "verification_unrunnable"},
            "verification_outcome",
            refuse,
        )
        reason = record["reason_code"]
        if reason is not None:
            _enum(
                reason,
                {
                    "sha_absent", "sha_unbanked", "repository_invalid",
                    "scratch_reuse_refused", "checkout_failure",
                    "bank_module_unresolved", "deadline_exceeded",
                    "test_output_unparseable", "checkout_dirty",
                    "cleanup_failed",
                },
                "verification_reason_code",
                refuse,
            )
        _bounded_string(record["remedy"], 0, 2048, "verification_remedy", refuse)
        runner_argv = record["runner_argv"]
        if not isinstance(runner_argv, list) or len(runner_argv) > 260:
            refuse("verification_runner_invalid", "verification runner argv is out of bounds")
        for argument in runner_argv:
            _bounded_string(argument, 1, 4096, "verification_runner_argument", refuse)
        if not isinstance(record["python_version"], str) or re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", record["python_version"]) is None:
            refuse("verification_python_version_invalid", "verification python version is not canonical")
        repo_path = record["repo_path"]
        if not isinstance(repo_path, str) or not Path(repo_path).is_absolute() or _terminal_unsafe(repo_path):
            refuse("verification_repo_path_invalid", "verification repo path must be absolute")
        if not isinstance(record["wall_time_seconds"], str) or re.fullmatch(r"[0-9]+\.[0-9]{6}", record["wall_time_seconds"]) is None:
            refuse("verification_wall_time_invalid", "verification wall time must be canonical seconds")
        _enum(record["unchecked_scope"], {"none", "claimed_bank_only"}, "unchecked_scope", refuse)
        output_sha256 = record["output_sha256"]
        if output_sha256 is not None:
            _sha256(output_sha256, "output_sha256", refuse)
        claim = record["claim"]
        normalized_claim = validate_record(
            claim, expected_tenant, frozenset({"delivery_claim"}), integrity=integrity
        )
        if normalized_claim["id"] != record["claim_id"] or normalized_claim["repo_path"] != repo_path:
            refuse("verification_claim_binding_invalid", "verification receipt does not bind its exact claim")
        measurement = record["measurement"]
        if measurement is not None:
            if not isinstance(measurement, dict) or set(measurement) != {
                "declared", "artifacts", "returncode", "output_sha256"
            }:
                refuse("verification_measurement_invalid", "verification measurement fields are malformed")
            measured_declared = measurement["declared"]
            if not isinstance(measured_declared, dict) or set(measured_declared) != {"ran", "result"}:
                refuse("verification_measurement_invalid", "measured test result is malformed")
            measured_ran = _json_integer(measured_declared.get("ran"))
            if measured_ran is None or not 0 <= measured_ran <= 10_000_000:
                refuse("verification_measurement_invalid", "measured test count is out of bounds")
            _enum(measured_declared.get("result"), {"OK", "FAILED"}, "verification_measured_result", refuse)
            returncode = _json_integer(measurement["returncode"])
            if returncode is None or not -255 <= returncode <= 255:
                refuse("verification_measurement_invalid", "runner return code is out of bounds")
            _sha256(measurement["output_sha256"], "output_sha256", refuse)
            measured_artifacts = measurement["artifacts"]
            if not isinstance(measured_artifacts, list) or len(measured_artifacts) > 256:
                refuse("verification_measurement_invalid", "measured artifacts are out of bounds")
            for artifact in measured_artifacts:
                if not isinstance(artifact, dict) or set(artifact) != {"path", "present", "sha256"}:
                    refuse("verification_measurement_invalid", "measured artifact fields are malformed")
                _bounded_string(artifact["path"], 1, 1024, "measured_artifact_path", refuse)
                if type(artifact["present"]) is not bool:
                    refuse("verification_measurement_invalid", "measured artifact presence must be boolean")
                if artifact["present"]:
                    _sha256(artifact["sha256"], "measured_artifact_sha256", refuse)
                elif artifact["sha256"] is not None:
                    refuse("verification_measurement_invalid", "absent measured artifact cannot have a digest")
        scratch = record["scratch"]
        if not isinstance(scratch, dict) or set(scratch) != {"path", "created", "destroyed"}:
            refuse("verification_scratch_invalid", "verification scratch testimony is malformed")
        if not isinstance(scratch["path"], str) or not Path(scratch["path"]).is_absolute():
            refuse("verification_scratch_invalid", "verification scratch path must be absolute")
        if type(scratch["created"]) is not bool or type(scratch["destroyed"]) is not bool:
            refuse("verification_scratch_invalid", "verification scratch states must be boolean")
        if record["outcome"] == "verification_unrunnable":
            if reason is None:
                refuse("verification_reason_code_invalid", "unrunnable verification requires a reason")
        elif reason is not None or measurement is None:
            refuse("verification_outcome_invalid", "completed verification cannot carry an unrunnable reason")
    elif kind == "journal_checkpoint_state":
        ident("journal_id")
        integer("through_seq", 1, 2**63 - 1)
        integer("byte_length", 1, 64 * 1024 * 1024)
        _sha256(record["head_sha256"], "head_sha256", refuse)
        _sha256(record["checkpoint_sha256"], "checkpoint_sha256", refuse)
    elif kind == "bus_epoch_roll_receipt":
        archive_path = record["archive_path"]
        if (
            not isinstance(archive_path, str)
            or not 1 <= len(archive_path) <= 4096
            or not Path(archive_path).is_absolute()
            or _terminal_unsafe(archive_path)
        ):
            refuse(
                "archive_path_invalid",
                "archive path must be bounded absolute terminal-safe text",
            )
        ident("actor")
        _bounded_string(
            record["idempotency_key"], 1, 128, "idempotency_key", refuse
        )
        if record["invalidated_followers"] != [
            "tail_followers", "waiters", "monitors"
        ]:
            refuse(
                "invalidated_followers_invalid",
                "epoch roll receipt must name the complete invalidated follower classes",
            )
        ident("epoch_id")
        _sha256(record["archive_sha256"], "archive_sha256", refuse)
        integer("archive_file_count", 1, 2**63 - 1)
        span = record["span"]
        if not isinstance(span, dict) or set(span) != {"byte_start", "byte_end"}:
            refuse("span_invalid", "epoch byte span fields are invalid")
        if span.get("byte_start") != 0 or isinstance(span.get("byte_start"), bool):
            refuse("span_invalid", "epoch byte span must start at zero")
        byte_end = span.get("byte_end")
        if (
            not isinstance(byte_end, int)
            or isinstance(byte_end, bool)
            or not 0 <= byte_end <= 2**63 - 1
        ):
            refuse("span_invalid", "epoch byte span end is outside its bounds")
        plane_counts = record["plane_counts"]
        if not isinstance(plane_counts, dict) or set(plane_counts) != {
            "events", "deliveries", "acks"
        }:
            refuse("plane_counts_invalid", "epoch plane count fields are invalid")
        if any(
            not isinstance(plane_counts[plane], int)
            or isinstance(plane_counts[plane], bool)
            or not 0 <= plane_counts[plane] <= 2**63 - 1
            for plane in ("events", "deliveries", "acks")
        ):
            refuse("plane_counts_invalid", "epoch plane counts are outside their bounds")
        if plane_counts["events"] != 1:
            refuse(
                "plane_counts_invalid",
                "epoch roll archives exactly one events ledger",
            )
        if sum(plane_counts.values()) != record["archive_file_count"]:
            refuse(
                "plane_counts_invalid",
                "epoch plane counts must equal the archive file count",
            )
    elif kind == "ledger_repair_receipt":
        if record["ledger"] != "events.jsonl":
            refuse("repair_ledger_invalid", "repair receipt must select events.jsonl")
        _bounded_string(record["record_id"], 1, 256, "record_id", refuse)
        _bounded_string(record["idempotency_key"], 1, 128, "idempotency_key", refuse)
        for field in (
            "original_digest", "repaired_digest", "quarantine_digest",
        ):
            _sha256(record[field], field, refuse)
        quarantine_path = record["quarantine_path"]
        if (
            not isinstance(quarantine_path, str)
            or not 1 <= len(quarantine_path) <= 4096
            or not Path(quarantine_path).is_absolute()
            or _terminal_unsafe(quarantine_path)
        ):
            refuse(
                "quarantine_path_invalid",
                "quarantine path must be bounded absolute terminal-safe text",
            )
        replaced = record["replaced_inode"]
        if not isinstance(replaced, dict) or set(replaced) != {"before", "after", "changed"}:
            refuse("replaced_inode_invalid", "replaced inode fact has invalid fields")
        coordinates = []
        for position in ("before", "after"):
            coordinate = replaced[position]
            if not isinstance(coordinate, dict) or set(coordinate) != {"device", "inode"}:
                refuse("replaced_inode_invalid", "inode coordinate has invalid fields")
            if any(
                not isinstance(coordinate[field], int)
                or isinstance(coordinate[field], bool)
                or coordinate[field] < 0
                for field in ("device", "inode")
            ):
                refuse("replaced_inode_invalid", "inode coordinates must be nonnegative integers")
            coordinates.append((coordinate["device"], coordinate["inode"]))
        if replaced["changed"] is not True or coordinates[0] == coordinates[1]:
            refuse("replaced_inode_invalid", "repair receipt must prove an inode replacement")
        if record["invalidated_followers"] != ["tail_followers", "waiters", "monitors"]:
            refuse(
                "invalidated_followers_invalid",
                "repair receipt must name the complete invalidated follower classes",
            )
    elif kind == "message_retracted":
        _record_ref(record["retracted_message_id"], "msg-", "retracted_message_id", refuse)
        _opaque_identifier(record["worker_session_id"], "worker_session_id", refuse)
        _enum(
            record["reason"],
            {"sent_in_error", "superseded_by_correction", "stale_recipient", "security_scrub"},
            "reason",
            refuse,
        )
        _opaque_identifier(record["author"], "author", refuse)
    elif kind == "denial_receipt":
        if not isinstance(record["attempt_id"], str) or re.fullmatch("attempt-" + _UUID7, record["attempt_id"]) is None:
            refuse("attempt_id_invalid", "attempt id must be UUIDv7")
        _bounded_string(record["claimed_sender"], 1, 64, "claimed_sender", refuse)
        _bounded_string(record["claimed_recipient"], 1, 64, "claimed_recipient", refuse)
        _enum(record["reason_code"], {"unknown_sender", "unknown_recipient", "idempotency_conflict", "reply_to_unknown", "reply_to_parties_mismatch"}, "reason_code", refuse)
    elif kind in ("ack_receipt", "delivery_receipt"):
        ident("recipient")
        if kind == "ack_receipt" and normalized_version == 1:
            _opaque_identifier(record["acting_session_id"], "acting_session_id", refuse)
            if _terminal_unsafe(record["acting_session_id"]):
                refuse("acting_session_id_invalid", "acting session is terminal-unsafe")
            if "node_lease_state_at_ack" in record:
                lease_id = record["node_lease_id"]
                lease_expiry = record["node_lease_expires_at"]
                lease_state = record["node_lease_state_at_ack"]
                _enum(
                    lease_state,
                    {"not_leased", "active", "expired", "retired"},
                    "node_lease_state_at_ack",
                    refuse,
                )
                if lease_state == "not_leased":
                    if lease_id is not None or lease_expiry is not None:
                        refuse(
                            "ack_lease_state_invalid",
                            "not-leased acknowledgment cannot name lease coordinates",
                        )
                else:
                    _record_ref(lease_id, "lease-", "node_lease_id", refuse)
                    if lease_state in {"active", "expired"}:
                        _timestamp_value(lease_expiry, "node_lease_expires_at", refuse)
                    elif lease_expiry is not None:
                        refuse(
                            "ack_lease_state_invalid",
                            "retired lease acknowledgment cannot name an expiry",
                        )
        items = record["item_ids"]
        minimum = 1 if kind == "ack_receipt" else 0
        if not isinstance(items, list) or not minimum <= len(items) <= 1000 or any(not isinstance(item, str) or re.fullmatch("msg-" + _UUID7, item) is None for item in items) or len(set(items)) != len(items):
            refuse("item_ids_invalid", "item_ids violate the v0 message-id set contract")
        if kind == "delivery_receipt":
            integer("presentation_count", 1, 2**63 - 1)
    elif kind == "wake_hold_receipt":
        ident("recipient")
        session = record["worker_session_id"]
        if session is not None:
            _opaque_identifier(session, "worker_session_id", refuse)
            if _terminal_unsafe(session):
                refuse("worker_session_id_invalid", "worker session is terminal-unsafe")
        _bounded_string(record["idempotency_key"], 1, 128, "idempotency_key", refuse)
        if _terminal_unsafe(record["idempotency_key"]):
            refuse("idempotency_key_invalid", "idempotency key is terminal-unsafe")
        integer("limit", 1, 1000)
        items = record["item_ids"]
        if (
            not isinstance(items, list) or not 1 <= len(items) <= 1000
            or any(not isinstance(item, str) or re.fullmatch("msg-" + _UUID7, item) is None for item in items)
            or len(set(items)) != len(items)
        ):
            refuse("item_ids_invalid", "wake hold item_ids must be a nonempty unique message-id list")
        for field in (
            "event_prefix_digest", "delivery_prefix_digest", "acknowledgment_prefix_digest",
            "decision_digest",
        ):
            _sha256(record[field], field, refuse)
        if record["decision_digest"] != wake_hold_decision_digest(record):
            refuse("wake_hold_decision_digest_invalid", "decision_digest must cover wake hold semantics")
    elif kind == "wake_attempt_receipt":
        ident("node_id")
        _opaque_identifier(record["acting_session_id"], "acting_session_id", refuse)
        if _terminal_unsafe(record["acting_session_id"]):
            refuse("acting_session_id_invalid", "acting session is terminal-unsafe")
        message_session = record["message_worker_session_id"]
        if message_session is not None:
            _opaque_identifier(message_session, "message_worker_session_id", refuse)
            if _terminal_unsafe(message_session):
                refuse("message_worker_session_id_invalid", "message worker session is terminal-unsafe")
        _bounded_string(record["idempotency_key"], 1, 128, "idempotency_key", refuse)
        if _terminal_unsafe(record["idempotency_key"]):
            refuse("idempotency_key_invalid", "idempotency key is terminal-unsafe")
        items = record["item_ids"]
        if (
            not isinstance(items, list) or not 1 <= len(items) <= 1000
            or any(not isinstance(item, str) or re.fullmatch("msg-" + _UUID7, item) is None for item in items)
            or len(set(items)) != len(items)
        ):
            refuse("item_ids_invalid", "wake attempt item_ids must be a nonempty unique message-id list")
        _enum(record["outcome"], {"woke", "refused"}, "outcome", refuse)
        decision_id = record["decision_receipt_id"]
        reason_code = record["reason_code"]
        if record["outcome"] == "woke":
            _record_ref(decision_id, "wake-hold-", "decision_receipt_id", refuse)
            if reason_code is not None:
                refuse("reason_code_invalid", "a successful wake has no refusal reason")
        else:
            if decision_id is not None:
                _record_ref(decision_id, "wake-hold-", "decision_receipt_id", refuse)
            _enum(
                reason_code,
                WAKE_ATTEMPT_REFUSED_REASONS,
                "reason_code",
                refuse,
            )
    elif kind == "codex_wait_consent_receipt":
        ident("node_id")
        _bounded_string(record["workspace"], 1, 4096, "workspace", refuse)
        if _terminal_unsafe(record["workspace"]) or not str(record["workspace"]).startswith("/"):
            refuse("workspace_invalid", "workspace must be an absolute terminal-safe path")
        _sha256(record["workspace_map_digest"], "workspace_map_digest", refuse)
        integer("hook_timeout_seconds", 2, 86400)
        integer("wait_deadline_seconds", 1, 86399)
        if record["wait_deadline_seconds"] >= record["hook_timeout_seconds"]:
            refuse("wait_deadline_invalid", "wait deadline must be below hook timeout")
        _enum(record["state"], {"armed", "disarmed"}, "state", refuse)
        _bounded_string(record["idempotency_key"], 1, 128, "idempotency_key", refuse)
        if _terminal_unsafe(record["idempotency_key"]):
            refuse("idempotency_key_invalid", "idempotency key is terminal-unsafe")
    elif kind == "codex_wait_session_receipt":
        ident("node_id")
        _bounded_string(record["workspace"], 1, 4096, "workspace", refuse)
        if _terminal_unsafe(record["workspace"]) or not str(record["workspace"]).startswith("/"):
            refuse("workspace_invalid", "workspace must be an absolute terminal-safe path")
        _sha256(record["workspace_map_digest"], "workspace_map_digest", refuse)
        _opaque_identifier(record["acting_session_id"], "acting_session_id", refuse)
        if _terminal_unsafe(record["acting_session_id"]):
            refuse("acting_session_id_invalid", "acting session is terminal-unsafe")
        _enum(record["operation"], {"claim", "arm", "takeover"}, "operation", refuse)
        _enum(record["state"], {"armed"}, "state", refuse)
        predecessor = record["predecessor_receipt_id"]
        if predecessor is not None:
            _record_ref(
                predecessor,
                "codex-wait-session-",
                "predecessor_receipt_id",
                refuse,
            )
        if record["operation"] == "takeover" and predecessor is None:
            refuse("codex_wait_session_predecessor_invalid", "takeover requires its predecessor")
        if record["operation"] != "takeover" and predecessor is not None:
            refuse("codex_wait_session_predecessor_invalid", "initial arm or claim has no predecessor")
        _record_ref(
            record["consent_receipt_id"],
            "codex-wait-consent-",
            "consent_receipt_id",
            refuse,
        )
        _bounded_string(record["idempotency_key"], 1, 128, "idempotency_key", refuse)
        if _terminal_unsafe(record["idempotency_key"]):
            refuse("idempotency_key_invalid", "idempotency key is terminal-unsafe")
    elif kind == "codex_wait_exhaustion_receipt":
        ident("node_id")
        _sha256(record["session_digest"], "session_digest", refuse)
        integer("waited_seconds", 0, 86399)
        _enum(record["outcome"], {"rearmed"}, "outcome", refuse)
        _bounded_string(record["idempotency_key"], 1, 128, "idempotency_key", refuse)
        if _terminal_unsafe(record["idempotency_key"]):
            refuse("idempotency_key_invalid", "idempotency key is terminal-unsafe")
    elif kind == "wake_waiter_exit_receipt":
        ident("node_id")
        _sha256(record["session_digest"], "session_digest", refuse)
        _enum(record["reason_code"], {"exhausted", "paused", "not_claimant", "breaker", "integrity_failure"}, "reason_code", refuse)
        integer("waited_seconds", 0, 86399)
        _bounded_string(record["idempotency_key"], 1, 128, "idempotency_key", refuse)
        if _terminal_unsafe(record["idempotency_key"]):
            refuse("idempotency_key_invalid", "idempotency key is terminal-unsafe")
    elif kind == "confluence_grant":
        _bounded_string(record["consumer"], 1, 64, "consumer", refuse)
        _enum(record["state"], {"granted", "revoked"}, "state", refuse)
        predecessor = record["predecessor_receipt_id"]
        if record["state"] == "granted":
            if predecessor is not None:
                refuse("confluence_grant_predecessor_invalid", "grant has no predecessor receipt")
        else:
            _record_ref(predecessor, "confluence-grant-", "predecessor_receipt_id", refuse)
        _bounded_string(record["idempotency_key"], 1, 128, "idempotency_key", refuse)
        if _terminal_unsafe(record["idempotency_key"]):
            refuse("idempotency_key_invalid", "idempotency key is terminal-unsafe")
    elif kind == "wake_control_receipt":
        ident("node_id")
        _sha256(record["session_digest"], "session_digest", refuse)
        _enum(record["operation"], {"pause", "resume"}, "operation", refuse)
        _enum(record["state"], {"paused", "resume_requested"}, "state", refuse)
        if (record["operation"], record["state"]) not in {
            ("pause", "paused"), ("resume", "resume_requested"),
        }:
            refuse("wake_control_state_invalid", "wake control operation and state disagree")
        predecessor = record["predecessor_receipt_id"]
        if record["operation"] == "pause":
            if predecessor is not None:
                refuse("wake_control_predecessor_invalid", "pause has no predecessor receipt")
        else:
            _record_ref(predecessor, "wake-control-", "predecessor_receipt_id", refuse)
        _bounded_string(record["idempotency_key"], 1, 128, "idempotency_key", refuse)
        if _terminal_unsafe(record["idempotency_key"]):
            refuse("idempotency_key_invalid", "idempotency key is terminal-unsafe")
    elif kind == "wake_daemon_consent_receipt":
        ident("node_id")
        _enum(
            record["harness"],
            {"codex", "cursor", "grok-build", "zcode"}
            if normalized_version == 1
            else {"codex", "cursor"},
            "harness",
            refuse,
        )
        _sha256(record["coordinate_digest"], "coordinate_digest", refuse)
        _bounded_string(record["adapter_version"], 1, 64, "adapter_version", refuse)
        _sha256(record["adapter_digest"], "adapter_digest", refuse)
        integer("min_poll_seconds", 1, 86400)
        integer("max_poll_seconds", 1, 86400)
        integer("max_backoff_seconds", 1, 86400)
        integer("activation_epoch", 1, 2**63 - 1)
        if record["max_poll_seconds"] < record["min_poll_seconds"]:
            refuse("wake_daemon_poll_bounds_invalid", "maximum poll is below minimum")
        if record["max_backoff_seconds"] < record["max_poll_seconds"]:
            refuse("wake_daemon_backoff_bounds_invalid", "maximum backoff is below maximum poll")
        _enum(record["operation"], {"consent", "revoke"}, "operation", refuse)
        _enum(record["state"], {"active", "revoked"}, "state", refuse)
        if (record["operation"], record["state"]) not in {("consent", "active"), ("revoke", "revoked")}:
            refuse("wake_daemon_consent_state_invalid", "consent operation and state disagree")
        predecessor = record["predecessor_receipt_id"]
        if predecessor is not None:
            _record_ref(predecessor, "wake-daemon-consent-", "predecessor_receipt_id", refuse)
        if record["operation"] == "revoke" and predecessor is None:
            refuse("wake_daemon_consent_predecessor_invalid", "revoke requires its active predecessor")
        _bounded_string(record["idempotency_key"], 1, 128, "idempotency_key", refuse)
    elif kind == "wake_daemon_lifecycle_receipt":
        ident("node_id")
        _enum(
            record["harness"],
            {"codex", "cursor", "grok-build", "zcode"}
            if normalized_version == 1
            else {"codex", "cursor"},
            "harness",
            refuse,
        )
        _sha256(record["coordinate_digest"], "coordinate_digest", refuse)
        _bounded_string(record["daemon_instance_id"], 1, 64, "daemon_instance_id", refuse)
        integer("activation_epoch", 1, 2**63 - 1)
        _enum(record["event"], {"installed", "started", "stopped", "removed", "revoked", "idle", "paused", "pause_unknown", "wake_attempt", "backpressure", "exhausted", "owner_unknown", "adapter_unknown", "wake_evidence_unknown", "relaunch_required", "refused"}, "event", refuse)
        _enum(record["state"], {"inactive", "installed", "running", "stopped", "removed", "revoked", "idle", "paused", "pause_unknown", "backpressure", "exhausted", "unknown", "refused", "relaunch_required"}, "state", refuse)
        reason = record["reason_code"]
        if reason is not None:
            _bounded_string(reason, 1, 128, "reason_code", refuse)
        _sha256(record["adapter_digest"], "adapter_digest", refuse)
        for field in ("plist_digest", "session_digest"):
            if record[field] is not None:
                _sha256(record[field], field, refuse)
        predecessor = record["predecessor_receipt_id"]
        if predecessor is not None:
            _record_ref(predecessor, "wake-daemon-lifecycle-", "predecessor_receipt_id", refuse)
        _bounded_string(record["idempotency_key"], 1, 128, "idempotency_key", refuse)
    elif kind == "liveness_presence":
        ident("node_id")
        observed = _timestamp_value(record["observed_at"], "observed_at", refuse)
        expires = _timestamp_value(record["expires_at"], "expires_at", refuse)
        if expires <= observed:
            refuse("time_order_invalid", "expires_at must follow observed_at")
        _enum(record["state"], {"present", "silent", "expired"}, "state", refuse)
    elif kind in ("authority_grant", "mutual_exclusion_hold"):
        ident("subject_id" if kind == "authority_grant" else "resource_id"); ident("holder")
        integer("epoch", 1, 2**63 - 1); integer("ttl_seconds", 1, 86400); integer("deadline_seconds", 1, 86400)
        if record["deadline_seconds"] > record["ttl_seconds"]:
            refuse("deadline_exceeds_ttl", "deadline must not exceed TTL")
        first_field = "claimed_at" if kind == "authority_grant" else "acquired_at"
        first = _timestamp_value(record[first_field], first_field, refuse)
        renewed = _timestamp_value(record["renewed_at"], "renewed_at", refuse)
        expires = _timestamp_value(record["expires_at"], "expires_at", refuse)
        released = record["released_at"]
        released_time = None if released is None else _timestamp_value(released, "released_at", refuse)
        if renewed < first or expires < renewed or (released_time is not None and released_time < renewed):
            refuse("time_order_invalid", "interval timestamps move backward")
        state = record["state"]
        _enum(state, {"active", "released", "expired"}, "state", refuse)
        if (state == "released") != (released_time is not None):
            refuse("release_state_invalid", "released_at and state must agree")
    elif kind == "wake_cause":
        ident("node_id")
        _enum(record["cause"], {"self_wake", "external_injection", "resurrection"}, "cause", refuse)
        integer("context_bytes", 0, 65536); integer("wake_count", 1, 1000000)
    elif kind == "work_item":
        _bounded_string(record["title"], 1, 256, "title", refuse)
        ident("owner")
        _artifact_bindings(record["artifact_bindings"], refuse)
        needs = record.get("needs", [])
        if not isinstance(needs, list) or len(needs) > 64:
            refuse("needs_invalid", "needs must contain at most 64 work ids")
        seen_needs = set()
        for dependency in needs:
            if (
                not isinstance(dependency, str)
                or re.fullmatch("work-" + _UUID7, dependency) is None
                or dependency in seen_needs
            ):
                refuse("needs_invalid", "needs must contain unique work UUIDv7 ids")
            seen_needs.add(dependency)
        if "workspace" in record:
            workspace = record["workspace"]
            expected = f"\x2fprivate/tmp/floati-work/{record['id']}"
            if workspace is not None and workspace != expected:
                refuse(
                    "workspace_invalid",
                    "workspace must be the ruled absolute path derived from the work id",
                )
    elif kind == "work_transition":
        if not isinstance(record["work_item_id"], str) or re.fullmatch("work-" + _UUID7, record["work_item_id"]) is None:
            refuse("work_item_id_invalid", "work_item_id must use the work UUIDv7 prefix")
        _enum(record["action"], {"claim", "complete"}, "action", refuse)
        ident("actor"); ident("authority_subject")
        integer("authority_epoch", 1, 2**63 - 1)
        _artifact_bindings(record["artifact_bindings"], refuse)
    elif kind == "capability":
        ident("node_id")
        _capability(record["capability"], refuse)
        _enum(record["mode"], {"unavailable", "read_only", "read_write"}, "capability_mode", refuse)
        _bounded_scope(record["scope"], refuse)
        observed = _timestamp_value(record["observed_at"], "observed_at", refuse)
        expires = _timestamp_value(record["expires_at"], "expires_at", refuse)
        if expires <= observed:
            refuse("time_order_invalid", "capability expiry must follow observation")
    elif kind == "capability_grant":
        ident("worker_id"); ident("authority_subject")
        _capability(record["capability_name"], refuse)
        _sha256(record["policy_digest"], "policy_digest", refuse)
        _record_ref(record["approval_request_id"], "approval-request-", "approval_request_id", refuse)
        _record_ref(record["approval_decision_id"], "approval-decision-", "approval_decision_id", refuse)
        integer("authority_epoch", 1, 2**63 - 1)
        _timestamp_value(record["expires_at"], "expires_at", refuse)
        _sha256(record["grant_digest"], "grant_digest", refuse)
        if record["grant_digest"] != capability_grant_digest(record):
            refuse("capability_grant_digest_invalid", "grant_digest must cover the exact governed grant fields")
    elif kind == "capability_revoked":
        _record_ref(record["grant_id"], "capability-grant-", "grant_id", refuse)
        _enum(record["reason_code"], {"operator_revoked", "authority_revoked", "worker_unregistered", "policy_replaced"}, "reason_code", refuse)
        replacement = record["replacement_policy_digest"]
        if record["reason_code"] == "policy_replaced":
            _sha256(replacement, "replacement_policy_digest", refuse)
        elif replacement is not None:
            refuse("replacement_policy_digest_invalid", "only policy_replaced names a replacement policy digest")
    elif kind == "approval_request":
        ident("requester"); ident("authority_subject")
        _capability(record["capability"], refuse)
        _bounded_scope(record["scope"], refuse)
        integer("requested_ttl_seconds", 1, 86400)
        integer("authority_epoch", 1, 2**63 - 1)
        requested = _timestamp_value(record["requested_at"], "requested_at", refuse)
        expires = _timestamp_value(record["expires_at"], "expires_at", refuse)
        if expires <= requested:
            refuse("time_order_invalid", "approval request expiry must follow request")
        if record["schema_version"] == 1:
            _sha256(record["exact_action_digest"], "exact_action_digest", refuse)
    elif kind == "approval_decision":
        if not isinstance(record["request_id"], str) or re.fullmatch("approval-request-" + _UUID7, record["request_id"]) is None:
            refuse("request_id_invalid", "request_id must use the approval request UUIDv7 prefix")
        ident("decider"); ident("authority_subject")
        integer("authority_epoch", 1, 2**63 - 1)
        _enum(record["decision"], {"approved", "denied"}, "decision", refuse)
        decided = _timestamp_value(record["decided_at"], "decided_at", refuse)
        if record["decision"] == "approved":
            _bounded_scope(record["granted_scope"], refuse, field="granted_scope")
            integer("granted_ttl_seconds", 1, 86400)
            if record["reason_code"] is not None:
                refuse("reason_code_invalid", "approved decisions cannot carry a denial reason")
            expires = _timestamp_value(record["expires_at"], "expires_at", refuse)
            if expires <= decided:
                refuse("time_order_invalid", "approved grant expiry must follow decision")
        else:
            if record["granted_scope"] is not None or record["granted_ttl_seconds"] is not None or record["expires_at"] is not None:
                refuse("denial_grant_invalid", "denied decisions cannot carry grant fields")
            _bounded_string(record["reason_code"], 1, 128, "reason_code", refuse)
        if record["schema_version"] == 1:
            _sha256(record["exact_action_digest"], "exact_action_digest", refuse)
    elif kind == "worker_receipt":
        if not isinstance(record["session_id"], str) or re.fullmatch("worker-" + _UUID7, record["session_id"]) is None:
            refuse("session_id_invalid", "session_id must use the worker UUIDv7 prefix")
        if not isinstance(record["work_item_id"], str) or re.fullmatch("work-" + _UUID7, record["work_item_id"]) is None:
            refuse("work_item_id_invalid", "work_item_id must use the work UUIDv7 prefix")
        ident("node_id"); ident("authority_subject")
        _capability(record["adapter"], refuse)
        _enum(record["transition"], {"claim", "spawn", "drive", "bind_artifact", "complete", "degrade"}, "transition", refuse)
        integer("authority_epoch", 1, 2**63 - 1)
        outcome = record["outcome_code"]
        if record["transition"] == "degrade":
            _enum(outcome, WORKER_OUTCOME_CODES, "outcome_code", refuse)
        elif outcome is not None:
            refuse("outcome_code_invalid", "successful worker transitions cannot carry an outcome code")
        _artifact_bindings(record["artifact_bindings"], refuse)
    elif kind == "worker_refusal":
        ident("node_id")
        _capability(record["adapter"], refuse)
        work_item_id = record["work_item_id"]
        if work_item_id is not None and (
            not isinstance(work_item_id, str)
            or re.fullmatch("work-" + _UUID7, work_item_id) is None
        ):
            refuse("work_item_id_invalid", "work_item_id must be null or use the work UUIDv7 prefix")
        _enum(record["reason_code"], WORKER_REFUSAL_CODES, "reason_code", refuse)
    elif kind == "session_adoption":
        ident("session_id"); ident("manager_node_id"); ident("lease_subject")
        _enum(record["mode"], {"MANAGED"}, "mode", refuse)
        integer("lease_epoch", 1, 2**63 - 1)
        expires = _timestamp_value(record["lease_expires_at"], "lease_expires_at", refuse)
        if expires <= _timestamp_value(record["timestamp"], "timestamp", refuse):
            refuse("time_order_invalid", "managed lease expiry must follow adoption")
    elif kind == "session_release":
        ident("session_id"); ident("manager_node_id"); ident("lease_subject")
        integer("lease_epoch", 1, 2**63 - 1)
        if not isinstance(record["adoption_id"], str) or re.fullmatch("adoption-" + _UUID7, record["adoption_id"]) is None:
            refuse("adoption_id_invalid", "adoption_id must use the adoption UUIDv7 prefix")
    elif kind == "bridge_consent":
        ident("peer_tenant_id"); ident("actor")
        if record["peer_tenant_id"] == record["tenant_id"]:
            refuse("bridge_same_root", "bridge consent peer must be a different tenant")
        if not isinstance(record["bridge_id"], str) or re.fullmatch("bridge-" + _UUID7, record["bridge_id"]) is None:
            refuse("bridge_id_invalid", "bridge_id must use the bridge UUIDv7 prefix")
        _enum(record["direction"], {"bidirectional"}, "direction", refuse)
        _enum(record["state"], {"granted", "revoked"}, "state", refuse)
        _enum(record["scope"], {"advisory_not_consumption"}, "scope", refuse)
    elif kind == "bridge_record":
        ident("left_tenant_id"); ident("right_tenant_id")
        if record["left_tenant_id"] == record["right_tenant_id"]:
            refuse("bridge_same_root", "bridge record tenants must differ")
        if record["tenant_id"] not in {record["left_tenant_id"], record["right_tenant_id"]}:
            refuse("bridge_tenant_mismatch", "bridge record must be stored by one named tenant")
        if not isinstance(record["bridge_id"], str) or re.fullmatch("bridge-" + _UUID7, record["bridge_id"]) is None:
            refuse("bridge_id_invalid", "bridge_id must use the bridge UUIDv7 prefix")
        for field in ("left_consent_id", "right_consent_id"):
            if not isinstance(record[field], str) or re.fullmatch("bridge-consent-" + _UUID7, record[field]) is None:
                refuse(f"{field}_invalid", f"{field} must use the bridge consent UUIDv7 prefix")
        _enum(record["transport"], {"local_filesystem"}, "transport", refuse)
        _enum(record["scope"], {"advisory_not_consumption"}, "scope", refuse)
        _enum(record["state"], {"active", "revoked"}, "state", refuse)
    elif kind == "bridge_forward":
        ident("source_tenant_id"); ident("destination_tenant_id"); ident("sender"); ident("recipient")
        if record["source_tenant_id"] == record["destination_tenant_id"]:
            refuse("bridge_same_root", "bridge forward tenants must differ")
        if record["tenant_id"] not in {record["source_tenant_id"], record["destination_tenant_id"]}:
            refuse("bridge_tenant_mismatch", "bridge forward must be stored by one named tenant")
        if not isinstance(record["bridge_id"], str) or re.fullmatch("bridge-" + _UUID7, record["bridge_id"]) is None:
            refuse("bridge_id_invalid", "bridge_id must use the bridge UUIDv7 prefix")
        if not isinstance(record["forward_id"], str) or re.fullmatch("forward-" + _UUID7, record["forward_id"]) is None:
            refuse("forward_id_invalid", "forward_id must use the forward UUIDv7 prefix")
        for field in ("source_consent_id", "destination_consent_id"):
            if not isinstance(record[field], str) or re.fullmatch("bridge-consent-" + _UUID7, record[field]) is None:
                refuse(f"{field}_invalid", f"{field} must use the bridge consent UUIDv7 prefix")
        _enum(record["direction"], {"outbound", "inbound"}, "direction", refuse)
        _repository(record["repo"], refuse); _git_sha(record["sha"], refuse)
        _repository_document(record["doc"], refuse); _bounded_note(record["note"], refuse)
        _enum(record["stamp"], {"advisory_not_consumption"}, "stamp", refuse)
    elif kind == "bridge_denial":
        ident("source_tenant_id"); ident("destination_tenant_id")
        if not isinstance(record["bridge_id"], str) or re.fullmatch("bridge-" + _UUID7, record["bridge_id"]) is None:
            refuse("bridge_id_invalid", "bridge_id must use the bridge UUIDv7 prefix")
        _enum(record["direction"], {"left_to_right", "right_to_left", "invalid"}, "direction", refuse)
        _enum(record["reason_code"], {
            "bridge_same_root", "bridge_consent_missing", "bridge_consent_revoked",
            "bridge_consent_mismatch", "bridge_not_active", "bridge_direction_invalid",
            "bridge_sender_inactive", "bridge_recipient_inactive",
            "bridge_transport_forbidden", "bridge_root_unknown",
        }, "reason_code", refuse)
        _enum(record["stamp"], {"advisory_not_consumption"}, "stamp", refuse)
    elif kind in {
        "gateway_session_ingress",
        "gateway_capability_declaration",
        "gateway_approval_forward",
    }:
        integer("gateway_version", 0, 0)
        if not isinstance(record["session_id"], str) or re.fullmatch("session-" + _UUID7, record["session_id"]) is None:
            refuse("session_id_invalid", "gateway session_id must use the session UUIDv7 prefix")
        if kind == "gateway_session_ingress":
            ident("actor")
            workspace = record["workspace"]
            if (
                not isinstance(workspace, str)
                or not 2 <= len(workspace) <= 1024
                or not workspace.startswith("/")
                or any(part in {"", ".", ".."} for part in Path(workspace).parts[1:])
            ):
                refuse("workspace_invalid", "gateway workspace must be an absolute lexical path")
            _enum(record["transport"], {"stdio"}, "transport", refuse)
        elif kind == "gateway_capability_declaration":
            capabilities = record["capabilities"]
            if (
                not isinstance(capabilities, list)
                or not 1 <= len(capabilities) <= 64
                or capabilities != sorted(set(capabilities))
            ):
                refuse("capabilities_invalid", "gateway capabilities must be a sorted nonempty unique list")
            for capability in capabilities:
                _capability(capability, refuse)
        else:
            if not isinstance(record["request_id"], str) or re.fullmatch("approval-request-" + _UUID7, record["request_id"]) is None:
                refuse("request_id_invalid", "gateway request_id must use the approval request UUIDv7 prefix")
            _capability(record["capability"], refuse)
            scope = record["scope"]
            if not isinstance(scope, list) or not 1 <= len(scope) <= 32 or scope != sorted(set(scope)):
                refuse("scope_invalid", "gateway approval scope must be a sorted nonempty unique list")
            for item in scope:
                _repository_document(item, refuse)
            _enum(record["state"], {"forwarded_unresolved"}, "state", refuse)
    elif kind == "task_contract":
        _run_record_binding(record, refuse)
        _bounded_string(record["objective"], 1, 4096, "objective", refuse)
        _contract_string_list(record["non_goals"], "non_goals", 1, 64, refuse); _avoid_areas(record["areas_to_avoid"], refuse)
        _contract_hashes(record["input_hashes"], "input_hashes", refuse); _contract_strings(record["acceptance_checks"], "acceptance_checks", refuse); _contract_strings(record["constraints"], "constraints", refuse)
        _enum(record["risk_class"], {"low", "medium", "high", "critical"}, "risk_class", refuse); _contract_retry_policy(record["retry_policy"], refuse)
        _contract_string_list(record["dependencies"], "dependencies", 0, 64, refuse); _sha256(record["contract_digest"], "contract_digest", refuse)
        if "repository" in record:
            _decision_repository(record["repository"], refuse)
    elif kind == "run_spawn_admission_enabled":
        _run_id(record["run_id"], refuse)
        _record_ref(record["run_admission_binding_id"], "run-admission-bound-", "run_admission_binding_id", refuse)
        _sha256(record["admission_digest"], "admission_digest", refuse)
        _sha256(record["policy_digest"], "policy_digest", refuse)
        try:
            from .admission import AdmissionPlan

            plan = AdmissionPlan.from_canonical(record["base_plan"])
        except (ProtocolRefusal, TypeError, ValueError):
            refuse("spawn_base_plan_invalid", "base_plan must be a complete canonical AdmissionPlan")
        _sha256(record["base_plan_digest"], "base_plan_digest", refuse)
        if record["base_plan_digest"] != plan.digest:
            refuse("spawn_base_plan_digest_invalid", "base_plan_digest must cover the full base plan")
        _timestamp_value(record["enabled_at_testimony"], "enabled_at_testimony", refuse)
    elif kind == "attempt_spawn_policy_bound":
        _run_id(record["run_id"], refuse)
        _run_item_id(record["parent_item_id"], "parent_item_id", refuse)
        _attempt_id(record["parent_attempt_id"], "parent_attempt_id", refuse)
        _sha256(record["parent_fence_token"], "parent_fence_token", refuse)
        _record_ref(record["parent_capability_set_bound_id"], "capability-set-bound-", "parent_capability_set_bound_id", refuse)
        _capability(record["adapter"], refuse)
        _enum(record["subagents_mode"], {"disabled", "observed_only", "managed"}, "subagents_mode", refuse)
        integer("max_children", 0, 8); integer("max_depth", 0, 16)
        _capability_set(record["child_capability_ceiling"], "child_capability_ceiling", refuse)
        _budget_rows(record["spawn_budget_ceiling"], "spawn_budget_ceiling", refuse)
        _workspace_policies(record["workspace_policies"], "workspace_policies", refuse)
        if record["subagents_mode"] != "managed" and any((record["max_children"], record["max_depth"], record["child_capability_ceiling"], record["spawn_budget_ceiling"], record["workspace_policies"])):
            refuse("spawn_policy_limits_invalid", "only managed mode carries child ceilings")
        _timestamp_value(record["bound_at_testimony"], "bound_at_testimony", refuse)
    elif kind == "plan_amendment":
        if record["schema_version"] == 1:
            _run_id(record["run_id"], refuse)
            _record_ref(record["spawn_group_id"], "spawn-group-created-", "spawn_group_id", refuse)
            _run_item_id(record["parent_item_id"], "parent_item_id", refuse)
            _attempt_id(record["parent_attempt_id"], "parent_attempt_id", refuse)
            _record_ref(record["parent_spawn_policy_id"], "attempt-spawn-policy-bound-", "parent_spawn_policy_id", refuse)
            _sha256(record["previous_plan_digest"], "previous_plan_digest", refuse)
            _sha256(record["previous_admission_digest"], "previous_admission_digest", refuse)
            _sha256(record["policy_digest"], "policy_digest", refuse)
            _spawn_children(record["children"], refuse)
            _spawn_dependency_edges(record["dependency_edges"], refuse)
            _sha256(record["plan_digest"], "plan_digest", refuse)
            _sha256(record["admission_digest"], "admission_digest", refuse)
        else:
            _run_record_binding(record, refuse)
            _record_ref(record["task_contract_id"], "task-contract-", "task_contract_id", refuse); _sha256(record["previous_digest"], "previous_digest", refuse)
            _contract_replacement_fields(record["replacement_fields"], refuse)
            _sha256(record["contract_digest"], "contract_digest", refuse)
    elif kind == "spawn_group_created":
        _run_id(record["run_id"], refuse)
        _run_item_id(record["parent_item_id"], "parent_item_id", refuse)
        _attempt_id(record["parent_attempt_id"], "parent_attempt_id", refuse)
        _sha256(record["parent_fence_token"], "parent_fence_token", refuse)
        _record_ref(record["parent_spawn_policy_id"], "attempt-spawn-policy-bound-", "parent_spawn_policy_id", refuse)
        if not _identifier(record["group_key"]):
            refuse("group_key_invalid", "group_key must be a bounded identifier")
        integer("max_children", 1, 8); integer("max_depth", 1, 16)
        _capability_set(record["child_capability_ceiling"], "child_capability_ceiling", refuse)
        _budget_rows(record["aggregate_budget"], "aggregate_budget", refuse)
        _enum(record["workspace_policy"], {"patch_only", "isolated_worktree"}, "workspace_policy", refuse)
        _timestamp_value(record["deadline"], "deadline", refuse)
        _enum(record["join_mode"], {"all_accepted", "all_terminal", "quorum", "first_accepted"}, "join_mode", refuse)
        required = record["required_count"]
        if record["join_mode"] in {"all_accepted", "all_terminal"}:
            if required is not None:
                refuse("required_count_invalid", "all-member joins require null required_count")
        else:
            if not isinstance(required, int) or isinstance(required, bool) or not 1 <= required <= record["max_children"]:
                refuse("required_count_invalid", "threshold joins require a bounded positive count")
            if record["join_mode"] == "first_accepted" and required != 1:
                refuse("required_count_invalid", "first_accepted requires one")
        _enum(record["on_late_result"], {"quarantine", "operator_decision"}, "on_late_result", refuse)
        _enum(record["on_child_failure"], {"fail_group", "continue_until_join_impossible"}, "on_child_failure", refuse)
        if not isinstance(record["cancel_remaining_after_success"], bool):
            refuse("cancel_remaining_after_success_invalid", "cancellation choice must be boolean")
    elif kind == "spawn_group_aborted":
        _run_id(record["run_id"], refuse)
        _record_ref(record["spawn_group_id"], "spawn-group-created-", "spawn_group_id", refuse)
        _attempt_id(record["parent_attempt_id"], "parent_attempt_id", refuse)
        _sha256(record["parent_fence_token"], "parent_fence_token", refuse)
        _enum(record["reason_code"], {"cancellation", "operator_abandonment"}, "reason_code", refuse)
        if record["reason_code"] == "cancellation":
            _record_ref(record["cancel_scope_resolved_id"], "cancel-scope-resolved-", "cancel_scope_resolved_id", refuse)
            if any(record[field] is not None for field in ("operator_id", "authority_subject", "authority_epoch", "capability_record_id")):
                refuse("spawn_abort_authority_invalid", "cancellation abort requires null operator fields")
        else:
            if record["cancel_scope_resolved_id"] is not None:
                refuse("spawn_abort_authority_invalid", "operator abort cannot bind cancellation")
            for field in ("operator_id", "authority_subject"):
                if not _identifier(record[field]):
                    refuse(f"{field}_invalid", f"{field} must be a bounded identifier")
            integer("authority_epoch", 1, 2**63 - 1)
            _record_ref(record["capability_record_id"], "capability-", "capability_record_id", refuse)
        _timestamp_value(record["aborted_at_testimony"], "aborted_at_testimony", refuse)
    elif kind == "child_admitted":
        _run_id(record["run_id"], refuse)
        _record_ref(record["spawn_group_id"], "spawn-group-created-", "spawn_group_id", refuse)
        _record_ref(record["plan_amendment_id"], "plan-amendment-", "plan_amendment_id", refuse)
        _attempt_id(record["parent_attempt_id"], "parent_attempt_id", refuse)
        _run_item_id(record["child_item_id"], "child_item_id", refuse)
        integer("child_depth", 1, 16)
        _record_ref(record["task_contract_id"], "task-contract-", "task_contract_id", refuse)
        _sha256(record["task_contract_digest"], "task_contract_digest", refuse)
        _sha256(record["admission_digest"], "admission_digest", refuse)
        _capability_set(record["capability_ceiling"], "capability_ceiling", refuse)
        _budget_rows(record["budget_allocation"], "budget_allocation", refuse)
        _enum(record["workspace_policy"], {"patch_only", "isolated_worktree"}, "workspace_policy", refuse)
        if not isinstance(record["workspace"], str) or re.fullmatch(r"\x2fprivate/tmp/floati-work/work-" + _UUID7, record["workspace"]) is None:
            refuse("workspace_invalid", "child workspace must use the closed reservation path")
        _timestamp_value(record["admitted_at_testimony"], "admitted_at_testimony", refuse)
    elif kind == "child_rejected":
        _run_id(record["run_id"], refuse)
        _record_ref(record["spawn_group_id"], "spawn-group-created-", "spawn_group_id", refuse)
        _record_ref(record["plan_amendment_id"], "plan-amendment-", "plan_amendment_id", refuse)
        _attempt_id(record["parent_attempt_id"], "parent_attempt_id", refuse)
        _run_item_id(record["child_item_id"], "child_item_id", refuse)
        _enum(record["reason_code"], {"item_limit", "fanout_limit", "depth_limit", "budget_refusal", "capability_refusal", "workspace_refusal", "deadline_expired", "policy_refusal", "admission_binding_refusal"}, "reason_code", refuse)
        _timestamp_value(record["evaluated_at_testimony"], "evaluated_at_testimony", refuse)
    elif kind == "spawn_group_closed":
        _run_id(record["run_id"], refuse)
        _record_ref(record["spawn_group_id"], "spawn-group-created-", "spawn_group_id", refuse)
        _record_ref(record["plan_amendment_id"], "plan-amendment-", "plan_amendment_id", refuse)
        _attempt_id(record["parent_attempt_id"], "parent_attempt_id", refuse)
        for field in ("member_item_ids", "accepted_item_ids", "terminal_item_ids", "rejected_item_ids"):
            _run_item_ids(record[field], field, 0, 8, refuse)
        members = set(record["member_item_ids"])
        if any(not set(record[field]) <= members for field in ("accepted_item_ids", "terminal_item_ids", "rejected_item_ids")):
            refuse("spawn_group_close_sets_invalid", "closure subsets must name immutable members")
        _enum(record["join_mode"], {"all_accepted", "all_terminal", "quorum", "first_accepted"}, "join_mode", refuse)
        required = record["required_count"]
        if required is not None and (not isinstance(required, int) or isinstance(required, bool) or not 1 <= required <= 8):
            refuse("required_count_invalid", "required_count must be null or bounded")
        _enum(record["outcome"], {"satisfied", "failed", "cancelled", "deadline", "needs_operator"}, "outcome", refuse)
        _enum(record["close_reason"], {"all_members_accepted", "all_members_terminal", "quorum_reached", "first_accepted", "join_impossible", "deadline_expired", "untracked_descendant_unknown", "member_needs_operator", "child_failure", "parent_cancelled"}, "close_reason", refuse)
        allowed_reasons = {
            "satisfied": {"all_members_accepted", "all_members_terminal", "quorum_reached", "first_accepted"},
            "failed": {"child_failure", "join_impossible"},
            "cancelled": {"parent_cancelled"},
            "deadline": {"deadline_expired"},
            "needs_operator": {"untracked_descendant_unknown", "member_needs_operator"},
        }
        if record["close_reason"] not in allowed_reasons[record["outcome"]]:
            refuse("spawn_group_close_invalid", "close outcome and reason must use the exact matrix")
        cancel_scope = record["cancel_scope_resolved_id"]
        if cancel_scope is not None:
            _record_ref(record["cancel_scope_resolved_id"], "cancel-scope-resolved-", "cancel_scope_resolved_id", refuse)
        if record["outcome"] == "cancelled" and cancel_scope is None:
            refuse("spawn_group_close_invalid", "cancelled close requires resolved parent cancellation")
        if record["outcome"] not in {"cancelled", "satisfied"} and cancel_scope is not None:
            refuse("spawn_group_close_invalid", "only cancelled or satisfied sibling cleanup can bind cancellation")
        _timestamp_value(record["closed_at_testimony"], "closed_at_testimony", refuse)
    elif kind == "untracked_descendant":
        _run_id(record["run_id"], refuse)
        _run_item_id(record["parent_item_id"], "parent_item_id", refuse)
        _attempt_id(record["parent_attempt_id"], "parent_attempt_id", refuse)
        _capability(record["adapter"], refuse)
        _bounded_string(record["provider_descendant_id"], 1, 512, "provider_descendant_id", refuse)
        _enum(record["state"], {"observed", "terminated", "adopted", "unknown"}, "descendant_state", refuse)
        if record["adopted_item_id"] is not None:
            _run_item_id(record["adopted_item_id"], "adopted_item_id", refuse)
        if (record["state"] == "adopted") != (record["adopted_item_id"] is not None):
            refuse("adopted_item_id_invalid", "only adopted descendants name one managed item")
        _enum(record["reason_code"], {"native_descendant_observed", "adapter_terminated", "adopted_managed", "observation_uncertain"}, "reason_code", refuse)
        _timestamp_value(record["observed_at_testimony"], "observed_at_testimony", refuse)
    elif kind == "descendant_observation_closed":
        _run_id(record["run_id"], refuse)
        _run_item_id(record["parent_item_id"], "parent_item_id", refuse)
        _attempt_id(record["parent_attempt_id"], "parent_attempt_id", refuse)
        _sha256(record["parent_fence_token"], "parent_fence_token", refuse)
        _record_ref(record["attempt_spawn_policy_id"], "attempt-spawn-policy-bound-", "attempt_spawn_policy_id", refuse)
        _capability(record["adapter"], refuse)
        if not isinstance(record["observed_descendant_ids"], list) or len(record["observed_descendant_ids"]) > 64:
            refuse("observed_descendant_ids_invalid", "observed descendants must be sorted unique and bounded")
        for item in record["observed_descendant_ids"]:
            _bounded_string(item, 1, 512, "provider_descendant_id", refuse)
        if record["observed_descendant_ids"] != sorted(record["observed_descendant_ids"]) or len(record["observed_descendant_ids"]) != len(set(record["observed_descendant_ids"])):
            refuse("observed_descendant_ids_invalid", "observed descendants must be sorted unique and bounded")
        _timestamp_value(record["closed_at_testimony"], "closed_at_testimony", refuse)
    elif kind == "spawn_late_result_disposition":
        _run_id(record["run_id"], refuse)
        _record_ref(record["spawn_group_id"], "spawn-group-created-", "spawn_group_id", refuse)
        _run_item_id(record["child_item_id"], "child_item_id", refuse)
        if not isinstance(record["result_record_id"], str) or not any(re.fullmatch(prefix + _UUID7, record["result_record_id"]) for prefix in ("run-result-produced-", "run-result-verified-", "run-result-accepted-")):
            refuse("result_record_id_invalid", "late disposition must name one result record")
        _enum(record["disposition"], {"quarantine", "retain_as_non_join_evidence"}, "disposition", refuse)
        for field in ("operator_id", "authority_subject"):
            if not _identifier(record[field]):
                refuse(f"{field}_invalid", f"{field} must be a bounded identifier")
        integer("authority_epoch", 1, 2**63 - 1)
        _record_ref(record["capability_record_id"], "capability-", "capability_record_id", refuse)
        _timestamp_value(record["decided_at_testimony"], "decided_at_testimony", refuse)
    elif kind in THREAD_OBSERVATION_KINDS:
        _thread_observation_record(record, refuse)
    elif kind == "decision_record":
        _decision_repository(record["repository"], refuse)
        _record_ref(record["decision_id"], "decision-", "decision_id", refuse)
        if record["id"].removeprefix("decision-record-") == record["decision_id"].removeprefix("decision-"):
            refuse(
                "decision_id_not_independent",
                "decision record and logical decision identities must use independent UUIDv7 components",
            )
        _decision_scope(record["scope"], record["task_contract_id"], refuse)
        _bounded_string(record["statement"], 1, 4096, "statement", refuse)
        _enum(record["status"], {"proposed", "accepted", "rejected"}, "decision_status", refuse)
        _enum(record["author_authority"], _DECISION_AUTHOR_AUTHORITIES, "author_authority", refuse)
        if record["status"] in {"accepted", "rejected"} and record["author_authority"] == "worker":
            refuse("decision_terminal_authority_invalid", "workers may not author terminal decision records")
        _decision_source_artifact_ids(record["source_artifact_ids"], refuse)
        task_contract_id = record["task_contract_id"]
        if task_contract_id is not None:
            _record_ref(task_contract_id, "task-contract-", "task_contract_id", refuse)
        _bounded_string(record["decided_by"], 1, 64, "decided_by", refuse)
        if record["supersedes"] is not None:
            _record_ref(record["supersedes"], "decision-", "supersedes", refuse)
        _decision_digest(record, refuse)
    elif kind in ({"acceptance_receipt", "approval_consumed_for_resume", "attempt_suspended_for_approval", "run_created", "run_admission_bound", "run_policy_bound", "worker_pool_bound", "capability_set_bound", "dispatch_decision", "result_produced", "result_verified", "result_accepted", "run_terminal", "attempt_opened", "attempt_started", "attempt_terminal", "retry_scheduled", "retry_exhausted", "cancel_requested", "cancel_scope_resolved", "cancel_observed", "cancel_signal_sent", "cancel_terminal", "cancel_unconfirmed", "stale_attempt_evidence", "stale_evidence_adopted", "attempt_harness_session_bound", "supervisor_orphaned"} | TASK3_CANCELLATION_KINDS):
        if not isinstance(record["run_id"], str) or re.fullmatch("run-" + _UUID7, record["run_id"]) is None:
            refuse("run_id_invalid", "run_id must use the run UUIDv7 prefix")
        if kind == "attempt_suspended_for_approval":
            _run_item_id(record["item_id"], "item_id", refuse)
            _attempt_id(record["attempt_id"], "attempt_id", refuse)
            _record_ref(record["attempt_started_id"], "attempt-started-", "attempt_started_id", refuse)
            _sha256(record["fence_token"], "fence_token", refuse)
            _capability(record["adapter"], refuse)
            _record_ref(record["approval_request_id"], "approval-request-", "approval_request_id", refuse)
            _sha256(record["exact_action_digest"], "exact_action_digest", refuse)
            _bounded_scope(record["requested_scope"], refuse, field="requested_scope")
            _resume_binding(
                record["resume_mode"],
                record["provider_session_or_thread_id"],
                refuse,
                adapter=record["adapter"],
            )
            _suspension_workspace(record["workspace"], refuse)
            _artifact_bindings([record["workspace_checkpoint"]], refuse)
            ident("execution_authority_subject"); ident("execution_authority_holder")
            integer("authority_epoch_at_request", 1, 2**63 - 1)
            _timestamp_value(record["approval_expiry"], "approval_expiry", refuse)
        elif kind == "approval_consumed_for_resume":
            _run_item_id(record["item_id"], "item_id", refuse)
            _attempt_id(record["attempt_id"], "attempt_id", refuse)
            _sha256(record["fence_token"], "fence_token", refuse)
            _record_ref(record["attempt_suspended_id"], "attempt-suspended-approval-", "attempt_suspended_id", refuse)
            _record_ref(record["approval_request_id"], "approval-request-", "approval_request_id", refuse)
            _record_ref(record["approval_decision_id"], "approval-decision-", "approval_decision_id", refuse)
            _sha256(record["exact_action_digest"], "exact_action_digest", refuse)
            _bounded_scope(record["requested_scope"], refuse, field="requested_scope")
            _resume_binding(
                record["resume_mode"],
                record["provider_session_or_thread_id"],
                refuse,
            )
            _suspension_workspace(record["workspace"], refuse)
            _artifact_bindings([record["workspace_checkpoint"]], refuse)
            ident("resume_authority_subject"); ident("resume_authority_holder")
            integer("resume_authority_epoch", 1, 2**63 - 1)
            _timestamp_value(record["consumed_at_testimony"], "consumed_at_testimony", refuse)
        elif kind == "acceptance_receipt":
            _run_item_id(record["item_id"], "item_id", refuse); _attempt_id(record["attempt_id"], "attempt_id", refuse)
            _sha256(record["contract_digest"], "contract_digest", refuse); _contract_string_list(record["check_ids"], "check_ids", 1, 64, refuse)
            ident("reviewer"); _identifier_set(record["evidence_bindings"], "evidence_bindings", 1, 32, refuse, prefix="worker-receipt-")
            _contract_string_list(record["deviations"], "deviations", 0, 64, refuse); _enum(record["result"], {"accepted", "rejected"}, "receipt_result", refuse)
        elif kind == "run_created":
            _sha256(record["plan_digest"], "plan_digest", refuse)
            if "policy_digest" in record:
                _sha256(record["policy_digest"], "policy_digest", refuse)
            _run_item_ids(record["item_ids"], "item_ids", 1, 128, refuse)
            _dependency_edges(record["dependency_edges"], record["item_ids"], refuse)
        elif kind == "run_admission_bound":
            _sha256(record["plan_digest"], "plan_digest", refuse)
            _sha256(record["policy_digest"], "policy_digest", refuse)
            integer("max_active_attempts", 1, 64)
            workers = record["workers"]
            if not isinstance(workers, list) or not 1 <= len(workers) <= 64:
                refuse("run_admission_workers_invalid", "workers must be one bounded lexical table")
            worker_keys = []
            for worker in workers:
                if not isinstance(worker, dict) or set(worker) != {"node_id", "worker_profile"}:
                    refuse("run_admission_workers_invalid", "worker rows must contain exact fields")
                if not _identifier(worker["node_id"]) or not _identifier(worker["worker_profile"]):
                    refuse("run_admission_workers_invalid", "worker rows must contain bounded identifiers")
                worker_keys.append(worker["node_id"])
            if worker_keys != sorted(worker_keys) or len(set(worker_keys)) != len(worker_keys):
                refuse("run_admission_workers_invalid", "workers must be lexical-sorted and unique by node_id")
            reservations = record["budget_reservations"]
            if not isinstance(reservations, list) or len(reservations) > 64:
                refuse("run_admission_budgets_invalid", "budget reservations must be a bounded lexical table")
            budget_keys = []
            for reservation in reservations:
                if not isinstance(reservation, dict) or set(reservation) != {"budget_id", "amount"}:
                    refuse("run_admission_budgets_invalid", "budget rows must contain exact fields")
                amount = reservation["amount"]
                if (
                    not _identifier(reservation["budget_id"])
                    or not isinstance(amount, int)
                    or isinstance(amount, bool)
                    or not 1 <= amount <= 1_000_000_000
                ):
                    refuse("run_admission_budgets_invalid", "budget rows violate their bounded semantics")
                budget_keys.append(reservation["budget_id"])
            if budget_keys != sorted(budget_keys) or len(set(budget_keys)) != len(budget_keys):
                refuse("run_admission_budgets_invalid", "budget reservations must be lexical-sorted and unique")
            items = record["items"]
            if not isinstance(items, list) or not 1 <= len(items) <= 64:
                refuse("run_admission_items_invalid", "items must be one bounded lexical table")
            item_keys = []
            for item in items:
                if not isinstance(item, dict) or set(item) != {
                    "item_id", "workspace_key", "concurrency_key", "capability_selector"
                }:
                    refuse("run_admission_items_invalid", "item rows must contain exact fields")
                _run_item_id(item["item_id"], "item_id", refuse)
                if any(
                    not _identifier(item[field])
                    for field in ("workspace_key", "concurrency_key", "capability_selector")
                ):
                    refuse("run_admission_items_invalid", "item rows must contain bounded semantic keys")
                item_keys.append(item["item_id"])
            if item_keys != sorted(item_keys) or len(set(item_keys)) != len(item_keys):
                refuse("run_admission_items_invalid", "items must be lexical-sorted and unique by item_id")
            _sha256(record["admission_digest"], "admission_digest", refuse)
            if record["admission_digest"] != run_admission_digest(
                workers, record["max_active_attempts"], reservations, items
            ):
                refuse("run_admission_digest_invalid", "admission_digest must cover all canonical admission tables")
        elif kind == "run_policy_bound":
            _sha256(record["policy_digest"], "policy_digest", refuse)
        elif kind == "worker_pool_bound":
            _identifier_set(record["worker_ids"], "worker_ids", 1, 16, refuse)
        elif kind == "capability_set_bound":
            _run_item_id(record["item_id"], "item_id", refuse)
            _attempt_id(record["attempt_id"], "attempt_id", refuse)
            _sha256(record["fence_token"], "fence_token", refuse)
            ident("chosen_worker")
            _sha256(record["policy_digest"], "policy_digest", refuse)
            integer("routing_rank", 0, 2**31 - 1)
            _timestamp_value(record["evaluated_at_testimony"], "evaluated_at_testimony", refuse)
            integer("grant_ledger_high_watermark", 1, 100000)
            _effective_grants(record["effective_grants"], record["grant_ledger_high_watermark"], refuse)
            _sha256(record["capability_digest"], "capability_digest", refuse)
            if record["capability_digest"] != capability_set_digest(record["effective_grants"]):
                refuse("capability_set_digest_invalid", "capability_digest must cover sorted effective grant triples")
        elif kind == "dispatch_decision":
            _run_item_id(record["item_id"], "item_id", refuse)
            if not isinstance(record["attempt_id"], str) or re.fullmatch("attempt-" + _UUID7, record["attempt_id"]) is None:
                refuse("attempt_id_invalid", "attempt_id must use the attempt UUIDv7 prefix")
            _identifier_set(record["eligible_workers"], "eligible_workers", 1, 16, refuse)
            ident("chosen_worker")
            _sha256(record["capability_digest"], "capability_digest", refuse)
            _capability(record["reason_code"], refuse)
            _sha256(record["policy_digest"], "policy_digest", refuse)
            integer("routing_rank", 0, 2**31 - 1); integer("scheduler_epoch", 1, 2**63 - 1)
            if record["schema_version"] == 1:
                _record_ref(record["capability_set_bound_id"], "capability-set-bound-", "capability_set_bound_id", refuse)
                if "adapter" in record:
                    _capability(record["adapter"], refuse)
                    _record_ref(record["attempt_spawn_policy_id"], "attempt-spawn-policy-bound-", "attempt_spawn_policy_id", refuse)
        elif kind in {"result_produced", "result_verified", "result_accepted"}:
            _run_item_id(record["item_id"], "item_id", refuse)
            if not isinstance(record["attempt_id"], str) or re.fullmatch("attempt-" + _UUID7, record["attempt_id"]) is None:
                refuse("attempt_id_invalid", "attempt_id must use the attempt UUIDv7 prefix")
            _identifier_set(record["worker_receipt_ids"], "worker_receipt_ids", 1, 32, refuse, prefix="worker-receipt-")
            reference = "dispatch_decision_id" if kind == "result_produced" else ("result_produced_id" if kind == "result_verified" else "predecessor_result_id")
            if not isinstance(record[reference], str) or re.fullmatch("run-(?:dispatch-decision|result-produced|result-verified)-" + _UUID7, record[reference]) is None:
                refuse(reference + "_invalid", "result reference must be a canonical run record id")
            if kind == "result_accepted":
                _enum(record["acceptance_mode"], {"verified", "accepted_unverified"}, "acceptance_mode", refuse)
                if record["acceptance_mode"] == "verified":
                    _record_ref(record["acceptance_receipt_id"], "acceptance-receipt-", "acceptance_receipt_id", refuse)
                elif record["acceptance_receipt_id"] is not None:
                    refuse("acceptance_receipt_invalid", "accepted_unverified must not name an acceptance receipt")
                if record["schema_version"] == 1:
                    _effect_operation_ids(
                        record["effect_operation_ids"], "effect_operation_ids", refuse
                    )
                    integer("effect_ledger_high_watermark", 1, 2**63 - 1)
                    _sha256(
                        record["effect_evidence_digest"],
                        "effect_evidence_digest", refuse,
                    )
        elif kind == "run_terminal":
            _enum(record["outcome"], {"succeeded", "failed", "cancelled", "skipped", "needs_operator", "uncertain", "partially_succeeded"}, "outcome", refuse)
        elif kind == "cancel_requested":
            _enum(record["scope"], {"run", "item", "exact_items"}, "scope", refuse)
            if record["scope"] == "exact_items":
                if normalized_version != 1 or record["item_id"] is not None:
                    refuse("cancel_scope_invalid", "exact-items cancellation is schema v1 with null item_id")
                _run_item_ids(record["item_ids"], "item_ids", 1, 64, refuse)
                _record_ref(record["spawn_group_id"], "spawn-group-created-", "spawn_group_id", refuse)
            if record["scope"] == "run":
                if record["item_id"] is not None:
                    refuse("cancel_scope_invalid", "run cancellation must use a null item_id")
            elif record["scope"] == "item":
                _run_item_id(record["item_id"], "item_id", refuse)
            ident("requested_by")
        elif kind == "cancel_scope_resolved":
            _record_ref(record["cancel_request_id"], "cancel-requested-", "cancel_request_id", refuse)
            _enum(record["scope"], {"run", "item", "exact_items"}, "scope", refuse)
            if record["scope"] == "run":
                if record["item_id"] is not None:
                    refuse("cancel_scope_invalid", "run cancellation must use a null item_id")
            elif record["scope"] == "item":
                _run_item_id(record["item_id"], "item_id", refuse)
            elif normalized_version != 1 or record["item_id"] is not None:
                refuse("cancel_scope_invalid", "exact-items resolution is schema v1 with null item_id")
            _run_item_ids(record["item_ids"], "item_ids", 1, 128, refuse)
            _attempt_ids(record["attempt_ids"], "attempt_ids", 0, 128, refuse)
        elif kind == "attempt_cancelled_before_start":
            _run_item_id(record["item_id"], "item_id", refuse)
            _attempt_id(record["attempt_id"], "attempt_id", refuse)
            if record["attempt_opened_id"] is not None:
                _record_ref(record["attempt_opened_id"], "attempt-opened-", "attempt_opened_id", refuse)
            if record["retry_scheduled_id"] is not None:
                _record_ref(record["retry_scheduled_id"], "retry-scheduled-", "retry_scheduled_id", refuse)
            if (record["attempt_opened_id"] is None) == (record["retry_scheduled_id"] is None):
                refuse("cancel_transition_invalid", "pre-start cancellation must consume one opened attempt or scheduled retry")
            _sha256(record["fence_token"], "fence_token", refuse)
            _record_ref(record["cancel_scope_resolved_id"], "cancel-scope-resolved-", "cancel_scope_resolved_id", refuse)
            if record["capability_set_bound_id"] is not None:
                _record_ref(record["capability_set_bound_id"], "capability-set-bound-", "capability_set_bound_id", refuse)
            if record["dispatch_decision_id"] is not None:
                _record_ref(record["dispatch_decision_id"], "run-dispatch-decision-", "dispatch_decision_id", refuse)
            _enum(record["reason_code"], {"cancelled_before_start"}, "reason_code", refuse)
            _timestamp_value(record["cancelled_at_testimony"], "cancelled_at_testimony", refuse)
        elif kind == "spawn_child_cancelled_without_attempt":
            _record_ref(record["spawn_group_id"], "spawn-group-created-", "spawn_group_id", refuse)
            _record_ref(record["plan_amendment_id"], "plan-amendment-", "plan_amendment_id", refuse)
            _run_item_id(record["child_item_id"], "child_item_id", refuse)
            if record["child_admitted_id"] is not None:
                _record_ref(record["child_admitted_id"], "child-admitted-", "child_admitted_id", refuse)
            _record_ref(record["cancel_scope_resolved_id"], "cancel-scope-resolved-", "cancel_scope_resolved_id", refuse)
            _enum(record["reason_code"], {"cancelled_without_attempt"}, "reason_code", refuse)
            _timestamp_value(record["cancelled_at_testimony"], "cancelled_at_testimony", refuse)
        elif kind in {"cancel_observed", "cancel_signal_sent", "cancel_terminal", "cancel_unconfirmed"}:
            _record_ref(record["cancel_scope_resolved_id"], "cancel-scope-resolved-", "cancel_scope_resolved_id", refuse)
            _run_item_id(record["item_id"], "item_id", refuse); _attempt_id(record["attempt_id"], "attempt_id", refuse)
            _sha256(record["fence_token"], "fence_token", refuse); _capability(record["adapter"], refuse)
            _enum(record["cancel_mode"], {"native", "local_process_only", "unavailable"}, "cancel_mode", refuse)
        elif kind == "stale_attempt_evidence":
            _run_item_id(record["item_id"], "item_id", refuse); _attempt_id(record["attempt_id"], "attempt_id", refuse)
            _identifier_set(record["worker_receipt_ids"], "worker_receipt_ids", 1, 32, refuse, prefix="worker-receipt-")
            _sha256(record["presented_fence_token"], "presented_fence_token", refuse)
            _attempt_id(record["current_attempt_id"], "current_attempt_id", refuse)
            _sha256(record["current_fence_token"], "current_fence_token", refuse)
        elif kind == "stale_evidence_adopted":
            _run_item_id(record["item_id"], "item_id", refuse)
            _record_ref(record["stale_evidence_id"], "stale-attempt-evidence-", "stale_evidence_id", refuse)
            _attempt_id(record["current_attempt_id"], "current_attempt_id", refuse)
            _sha256(record["current_fence_token"], "current_fence_token", refuse); ident("operator_id")
            ident("authority_subject"); integer("authority_epoch", 1, 2**63 - 1)
            _record_ref(record["capability_record_id"], "capability-", "capability_record_id", refuse)
        elif kind == "attempt_harness_session_bound":
            _run_item_id(record["item_id"], "item_id", refuse); _attempt_id(record["attempt_id"], "attempt_id", refuse)
            _sha256(record["fence_token"], "fence_token", refuse)
            _record_ref(record["claim_id"], "claim-", "claim_id", refuse)
            _record_ref(record["lease_id"], "lease-", "lease_id", refuse)
            _record_ref(record["worker_session_id"], "worker-", "worker_session_id", refuse)
            if record["schema_version"] == 1:
                _harness_segments_v1(record["harness_segments"], refuse)
            else:
                _harness_segments(record["harness_segments"], refuse)
        elif kind == "supervisor_orphaned":
            _run_item_id(record["item_id"], "item_id", refuse); _attempt_id(record["attempt_id"], "attempt_id", refuse)
            _record_ref(record["claim_id"], "claim-", "claim_id", refuse)
            _record_ref(record["lease_id"], "lease-", "lease_id", refuse)
            _record_ref(record["worker_session_id"], "worker-", "worker_session_id", refuse)
            ident("supervisor_id")
            _enum(record["orphan_class"], {"owner_loss", "unregister", "lease_abandonment"}, "orphan_class", refuse)
            ident("authority_subject"); integer("authority_epoch", 1, 2**63 - 1)
            _record_ref(record["capability_record_id"], "capability-", "capability_record_id", refuse)
        elif kind in EFFECT_KINDS:
            _effect_binding(record, refuse)
            if kind == "effect_intent":
                ident("requested_by")
                _effect_approval_refs(record, refuse)
                _timestamp_value(record["intended_at_testimony"], "intended_at_testimony", refuse)
            elif kind == "effect_dispatched":
                _record_ref(record["effect_intent_id"], "effect-intent-", "effect_intent_id", refuse)
                _enum(record["dispatch_adapter"], EFFECT_DISPATCH_ADAPTERS, "dispatch_adapter", refuse)
                _sha256(record["dispatch_evidence_digest"], "dispatch_evidence_digest", refuse)
                _timestamp_value(record["dispatched_at_testimony"], "dispatched_at_testimony", refuse)
            elif kind == "effect_acknowledged":
                _record_ref(record["effect_intent_id"], "effect-intent-", "effect_intent_id", refuse)
                _record_ref(record["effect_dispatched_id"], "effect-dispatched-", "effect_dispatched_id", refuse)
                _sha256(record["acknowledgement_digest"], "acknowledgement_digest", refuse)
                _timestamp_value(record["acknowledged_at_testimony"], "acknowledged_at_testimony", refuse)
            elif kind == "effect_confirmed":
                _effect_outcome_refs(record, refuse, acknowledgement=True)
                _effect_confirmation(record["confirmation"], "confirmation", refuse)
                _sha256(record["confirmation_evidence_digest"], "confirmation_evidence_digest", refuse)
                _effect_budget_rows(record["measured_spend"], "measured_spend", refuse)
                _timestamp_value(record["confirmed_at_testimony"], "confirmed_at_testimony", refuse)
            elif kind in {"effect_failed", "effect_unknown"}:
                _effect_outcome_refs(record, refuse, acknowledgement=False)
                reasons = EFFECT_FAILURE_REASONS if kind == "effect_failed" else EFFECT_UNKNOWN_REASONS
                _enum(record["reason_code"], reasons, "reason_code", refuse)
                _sha256(record["failure_evidence_digest" if kind == "effect_failed" else "unknown_evidence_digest"], "failure_evidence_digest" if kind == "effect_failed" else "unknown_evidence_digest", refuse)
                _enum(record["spend_status"], EFFECT_SPEND_STATUSES, "spend_status", refuse)
                _effect_nullable_budget_rows(record["measured_spend"], "measured_spend", refuse)
                _timestamp_value(record["failed_at_testimony" if kind == "effect_failed" else "unknown_at_testimony"], "failed_at_testimony" if kind == "effect_failed" else "unknown_at_testimony", refuse)
            elif kind == "effect_reconciled":
                _record_ref(record["effect_intent_id"], "effect-intent-", "effect_intent_id", refuse)
                _effect_evidence_ref(record["prior_effect_evidence_id"], "prior_effect_evidence_id", refuse)
                _enum(record["reconciled_outcome"], {"confirmed", "failed", "unknown"}, "reconciled_outcome", refuse)
                _sha256(record["reconciliation_evidence_digest"], "reconciliation_evidence_digest", refuse)
                _effect_nullable_confirmation(record["confirmation"], refuse)
                _enum(record["spend_status"], EFFECT_SPEND_STATUSES, "spend_status", refuse)
                _effect_nullable_budget_rows(record["measured_spend"], "measured_spend", refuse)
                _timestamp_value(record["reconciled_at_testimony"], "reconciled_at_testimony", refuse)
            elif kind == "compensation_proposed":
                _record_ref(record["effect_intent_id"], "effect-intent-", "effect_intent_id", refuse)
                _effect_evidence_ref(record["source_effect_evidence_id"], "source_effect_evidence_id", refuse)
                _enum(record["reason_code"], EFFECT_COMPENSATION_REASONS, "reason_code", refuse)
                _sha256(record["compensation_plan_digest"], "compensation_plan_digest", refuse)
                _sha256(record["compensation_request_digest"], "compensation_request_digest", refuse)
                _effect_operation_id(record["compensation_operation_id"], "compensation_operation_id", refuse)
                _enum(record["compensation_risk_class"], {"low", "medium", "high", "critical"}, "compensation_risk_class", refuse)
                _effect_approval_refs(record, refuse)
                _timestamp_value(record["proposed_at_testimony"], "proposed_at_testimony", refuse)
            else:
                _record_ref(record["compensation_proposal_id"], "compensation-proposed-", "compensation_proposal_id", refuse)
                _effect_operation_id(record["compensation_operation_id"], "compensation_operation_id", refuse)
                _effect_evidence_ref(record["compensation_terminal_evidence_id"], "compensation_terminal_evidence_id", refuse)
                _timestamp_value(record["executed_at_testimony"], "executed_at_testimony", refuse)
    if kind in {"attempt_opened", "attempt_started", "attempt_terminal", "retry_scheduled", "retry_exhausted"}:
        _run_item_id(record["item_id"], "item_id", refuse)
        if kind == "attempt_opened":
            _attempt_id(record["attempt_id"], "attempt_id", refuse)
            integer("ordinal", 1, 32); integer("max_attempts", 1, 32)
            if record["ordinal"] > record["max_attempts"]:
                refuse("ordinal_invalid", "ordinal must not exceed max_attempts")
            integer("scheduler_epoch", 1, 2**63 - 1); _sha256(record["fence_token"], "fence_token", refuse)
            _backoff(record["backoff"], refuse)
        elif kind == "attempt_started":
            _attempt_id(record["attempt_id"], "attempt_id", refuse); integer("ordinal", 1, 32)
            _record_ref(record["attempt_opened_id"], "attempt-opened-", "attempt_opened_id", refuse)
            _record_ref(record["dispatch_decision_id"], "run-dispatch-decision-", "dispatch_decision_id", refuse)
            _sha256(record["fence_token"], "fence_token", refuse)
        elif kind == "attempt_terminal":
            _attempt_id(record["attempt_id"], "attempt_id", refuse); integer("ordinal", 1, 32)
            _record_ref(record["attempt_started_id"], "attempt-started-", "attempt_started_id", refuse)
            _sha256(record["fence_token"], "fence_token", refuse)
            _enum(record["terminal_state"], {"completed", "failed", "cancelled", "uncertain"}, "terminal_state", refuse)
            policy_class = record["policy_class"]
            if record["terminal_state"] == "completed":
                if policy_class is not None or record["reason_code"] != "completed":
                    refuse("terminal_policy_invalid", "completed terminals require null policy and completed reason")
            else:
                _enum(policy_class, {"transient", "permanent", "operator_required", "policy_refusal", "cancelled", "unknown_effect"}, "policy_class", refuse)
                _capability(record["reason_code"], refuse)
            _enum(record["effect_safety"], {"idempotent", "non_idempotent", "unknown_effect"}, "effect_safety", refuse)
            _enum(record["retry_disposition"], {"none", "scheduled", "exhausted"}, "retry_disposition", refuse)
            disposition = record["retry_disposition"]
            closure = ("retry_record_id", "next_attempt_id", "next_ordinal", "retry_delay_ms", "next_scheduler_epoch", "next_fence_token")
            if disposition == "none":
                if any(record[field] is not None for field in closure):
                    refuse("retry_closure_invalid", "none retry disposition requires null closure fields")
            elif disposition == "scheduled":
                _record_ref(record["retry_record_id"], "retry-scheduled-", "retry_record_id", refuse)
                _attempt_id(record["next_attempt_id"], "next_attempt_id", refuse); integer("next_ordinal", 1, 32)
                integer("retry_delay_ms", 0, 86400000); integer("next_scheduler_epoch", 1, 2**63 - 1)
                _sha256(record["next_fence_token"], "next_fence_token", refuse)
            else:
                _record_ref(record["retry_record_id"], "retry-exhausted-", "retry_record_id", refuse)
                if any(record[field] is not None for field in closure[1:]):
                    refuse("retry_closure_invalid", "exhausted retry disposition requires null next-attempt fields")
        elif kind == "retry_scheduled":
            _attempt_id(record["previous_attempt_id"], "previous_attempt_id", refuse)
            _record_ref(record["attempt_terminal_id"], "attempt-terminal-", "attempt_terminal_id", refuse)
            _attempt_id(record["next_attempt_id"], "next_attempt_id", refuse); integer("next_ordinal", 1, 32)
            integer("delay_ms", 0, 86400000); integer("scheduler_epoch", 1, 2**63 - 1)
            _sha256(record["next_fence_token"], "next_fence_token", refuse)
        else:
            _attempt_id(record["attempt_id"], "attempt_id", refuse); integer("ordinal", 1, 32)
            _record_ref(record["attempt_terminal_id"], "attempt-terminal-", "attempt_terminal_id", refuse)
            integer("max_attempts", 1, 32)
            _enum(record["reason_code"], {"max_attempts"}, "reason_code", refuse)
    if chain is not None:
        record = dict(record, **chain)
    return record


def is_known_record_kind(kind: object) -> bool:
    """Return whether this reader ships the exact contract for one kind."""

    return isinstance(kind, str) and kind in _SPECS


def validate_unknown_record(record: Any, expected_tenant: str) -> Dict[str, Any]:
    """Validate only the stable envelope shared with a future record kind."""

    def refuse(code: str, detail: str) -> None:
        raise IntegrityFailure(code, detail)

    if not isinstance(record, dict):
        raise IntegrityFailure("record_not_object", "each durable record must be an object")
    if not _COMMON <= frozenset(record):
        raise IntegrityFailure(
            "record_fields_invalid", "unknown record lacks the stable envelope fields"
        )
    kind = record.get("kind")
    if (
        not isinstance(kind, str)
        or re.fullmatch(r"[a-z][a-z0-9_]{0,127}", kind) is None
    ):
        raise IntegrityFailure("record_kind_invalid", "unknown record kind is invalid")
    version = record.get("schema_version")
    if (
        not isinstance(version, int)
        or isinstance(version, bool)
        or not 0 <= version <= 2**31 - 1
    ):
        raise IntegrityFailure(
            "schema_version_invalid", "unknown record schema version is invalid"
        )
    if record.get("tenant_id") != expected_tenant:
        raise IntegrityFailure(
            "tenant_mismatch", "record tenant does not match the selected root"
        )
    if not _identifier(record["tenant_id"]):
        raise IntegrityFailure("tenant_invalid", "tenant identifier is invalid")
    record_id = record.get("id")
    if (
        not isinstance(record_id, str)
        or not 1 <= len(record_id) <= 256
        or any(ord(character) < 32 or ord(character) == 127 for character in record_id)
    ):
        raise IntegrityFailure("record_id_invalid", "unknown record id is invalid")
    _timestamp_value(record.get("timestamp"), "timestamp", refuse)
    return dict(record)


def validate_artifact_bindings(value: object) -> list[Dict[str, str]]:
    """Validate an adapter boundary value with the durable binding contract."""

    def refuse(code: str, detail: str) -> None:
        raise ProtocolRefusal(code, detail)

    _artifact_bindings(value, refuse)
    return [dict(binding) for binding in value]  # type: ignore[union-attr]


def validate_repository_coordinate(value: object) -> str:
    """Validate an explicit repository ledger coordinate without normalizing it."""

    def refuse(code: str, detail: str) -> None:
        raise ProtocolRefusal(code, detail)

    _decision_repository(value, refuse)
    return value  # type: ignore[return-value]


def decision_record_digest(record: Mapping[str, object]) -> str:
    """Return the ruled full-record digest excluding only its own field."""

    try:
        canonical = dict(record)
        canonical.pop("decision_digest", None)
        encoded = json.dumps(
            canonical,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise ProtocolRefusal(
            "decision_not_ijson",
            "decision cannot form canonical I-JSON",
        ) from exc
    return hashlib.sha256(encoded).hexdigest()


def thread_observation_digest(record: Mapping[str, object]) -> str:
    """Bind the exact normalized testimony without IDs or local timestamps."""

    fields = (
        "attachment_id",
        "provider",
        "provider_thread_id",
        "provider_status",
        "active_flags",
        "provider_updated_at",
        "attention",
        "observation_outcome",
        "observation_reason",
    )
    try:
        canonical = deepcopy({field: record[field] for field in fields})
        updated = canonical["provider_updated_at"]
        if isinstance(updated, dict):
            normalized = _json_integer(updated.get("value"))
            if normalized is not None:
                updated["value"] = normalized
        encoded = json.dumps(
            canonical,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (KeyError, TypeError, ValueError, UnicodeEncodeError) as exc:
        raise ProtocolRefusal(
            "thread_observation_not_ijson",
            "thread observation cannot form canonical I-JSON",
        ) from exc
    return hashlib.sha256(encoded).hexdigest()


def segment_seal_digest(record: Mapping[str, object]) -> str:
    """Return the digest binding a sealed segment's governed metadata."""

    fields = (
        "tenant_id", "segment_number", "opening_record_id", "last_global_ordinal",
        "record_count", "byte_length", "segment_sha256",
    )
    try:
        canonical = {field: record[field] for field in fields}
        encoded = json.dumps(
            canonical,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (KeyError, TypeError, ValueError, UnicodeEncodeError) as exc:
        raise ProtocolRefusal(
            "segment_seal_not_ijson",
            "segment seal cannot form canonical I-JSON",
        ) from exc
    return hashlib.sha256(encoded).hexdigest()


def capability_grant_digest(record: Mapping[str, object]) -> str:
    """Digest the closed grant authority domain without timestamp testimony or record identity."""

    fields = (
        "tenant_id", "worker_id", "capability_name", "policy_digest",
        "approval_request_id", "approval_decision_id", "authority_subject",
        "authority_epoch", "expires_at",
    )
    try:
        canonical = {field: record[field] for field in fields}
        encoded = json.dumps(
            canonical,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (KeyError, TypeError, ValueError, UnicodeEncodeError) as exc:
        raise ProtocolRefusal(
            "capability_grant_not_ijson",
            "capability grant cannot form canonical I-JSON",
        ) from exc
    return hashlib.sha256(encoded).hexdigest()


def capability_set_digest(effective_grants: object) -> str:
    """Digest canonical sorted ``(name, grant id, physical position)`` triples."""

    try:
        triples = [
            [row["capability_name"], row["grant_id"], row["physical_position"]]
            for row in effective_grants  # type: ignore[union-attr]
        ]
        encoded = json.dumps(
            triples,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except (KeyError, TypeError, ValueError, UnicodeEncodeError) as exc:
        raise ProtocolRefusal(
            "capability_set_not_ijson",
            "effective grants cannot form canonical I-JSON triples",
        ) from exc
    return hashlib.sha256(encoded).hexdigest()


def run_admission_digest(
    workers: object,
    max_active_attempts: object,
    budget_reservations: object,
    items: object,
) -> str:
    """Digest the complete canonical runtime-admission semantic tables."""

    try:
        canonical = {
            "workers": workers,
            "max_active_attempts": max_active_attempts,
            "budget_reservations": budget_reservations,
            "items": items,
        }
        encoded = json.dumps(
            canonical,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise ProtocolRefusal(
            "run_admission_not_ijson",
            "run admission semantics cannot form canonical I-JSON",
        ) from exc
    return hashlib.sha256(encoded).hexdigest()


def _decision_digest(record: Mapping[str, object], refuse: Any) -> None:
    value = record["decision_digest"]
    _sha256(value, "decision_digest", refuse)
    try:
        expected = decision_record_digest(record)
    except ProtocolRefusal as exc:
        refuse(exc.code, exc.detail)
    if value != expected:
        refuse("decision_digest_invalid", "decision_digest must cover the full record excluding itself")


def _decision_relative_prefix(value: object, field: str, refuse: Any) -> None:
    if (
        type(value) is not str
        or not 1 <= len(value) <= 1024
        or value.startswith("/")
        or "\\" in value
        or _terminal_unsafe(value)
        or unicodedata.normalize("NFC", value) != value
        or any(component in {"", ".", ".."} for component in value.split("/"))
    ):
        refuse(field + "_invalid", field + " must be one normalized repository-relative lexical prefix")


def _decision_scope(scope: object, task_contract_id: object, refuse: Any) -> None:
    if type(scope) is not dict or type(scope.get("kind")) is not str:
        refuse("decision_scope_invalid", "scope must use one closed tagged v0 object")
    kind = scope["kind"]
    if kind == "repository":
        if set(scope) != {"kind"}:
            refuse("decision_scope_invalid", "repository scope must have only its kind")
        return
    if kind == "path_prefix":
        if set(scope) != {"kind", "path_prefix"}:
            refuse("decision_scope_invalid", "path-prefix scope must name one prefix")
        _decision_relative_prefix(scope["path_prefix"], "path_prefix", refuse)
        return
    if kind == "contract":
        if set(scope) != {"kind"}:
            refuse("decision_scope_invalid", "contract scope must have only its kind")
        _record_ref(task_contract_id, "task-contract-", "task_contract_id", refuse)
        return
    refuse("decision_scope_invalid", "scope kind is outside the closed v0 vocabulary")


def _decision_source_artifact_ids(value: object, refuse: Any) -> None:
    if type(value) is not list or not 1 <= len(value) <= 64:
        refuse("source_artifact_ids_invalid", "source artifact ids must be a nonempty bounded list")
    sources = []
    for source in value:
        if type(source) is not str or not 1 <= len(source) <= 2048 or _terminal_unsafe(source):
            refuse("source_artifact_id_invalid", "source artifact id is not a closed durable identity")
        if any(pattern.fullmatch(source) is not None for pattern in _DECISION_SOURCE_PATTERNS):
            sources.append(source)
            continue
        document = _DECISION_DOC_SOURCE.fullmatch(source)
        if document is None:
            refuse("source_artifact_id_invalid", "source artifact id is not in the closed v0 taxonomy")
        _decision_relative_prefix(document.group(1), "source_artifact_id", refuse)
        sources.append(source)
    if len(set(sources)) != len(sources):
        refuse("source_artifact_ids_invalid", "source artifact ids must be unique")


def _identifier(value: object) -> bool:
    return isinstance(value, str) and IDENTIFIER_PATTERN.fullmatch(value) is not None


def _repository(value: object, refuse: Any) -> None:
    if not isinstance(value, str) or not 1 <= len(value) <= 128 or _REPOSITORY.fullmatch(value) is None:
        refuse("repo_invalid", "repo must name one repository or owner/repository")


def _decision_repository(value: object, refuse: Any) -> None:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 128
        or "\\" in value
        or _terminal_unsafe(value)
    ):
        refuse("repository_invalid", "repository must be one or two bounded lexical segments")
    segments = value.split("/")
    if not 1 <= len(segments) <= 2 or any(
        segment in {"", ".", ".."}
        or _DECISION_REPOSITORY_SEGMENT.fullmatch(segment) is None
        for segment in segments
    ):
        refuse("repository_invalid", "repository must be one or two bounded lexical segments")


def _git_sha(value: object, refuse: Any) -> None:
    if not isinstance(value, str) or _GIT_SHA.fullmatch(value) is None:
        refuse("sha_invalid", "sha must be a 40- or 64-character lowercase Git object id")


def _sha256(value: object, field: str, refuse: Any) -> None:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        refuse(f"{field}_invalid", f"{field} must be a lowercase SHA-256 digest")


def _run_id(value: object, refuse: Any) -> None:
    if not isinstance(value, str) or re.fullmatch("run-" + _UUID7, value) is None:
        refuse("run_id_invalid", "run_id must use the run UUIDv7 prefix")


def _run_item_id(value: object, field: str, refuse: Any) -> None:
    if not isinstance(value, str) or re.fullmatch("work-" + _UUID7, value) is None:
        refuse(f"{field}_invalid", f"{field} must use the work UUIDv7 prefix")


def _effect_operation_id(value: object, field: str, refuse: Any) -> None:
    if not isinstance(value, str) or re.fullmatch("effect-op-" + _UUID7, value) is None:
        refuse(f"{field}_invalid", f"{field} must use the effect operation UUIDv7 prefix")


def _effect_operation_ids(
    value: object, field: str, refuse: Any,
) -> None:
    if not isinstance(value, list) or not 1 <= len(value) <= 1024:
        refuse(f"{field}_invalid", f"{field} must be a sorted unique operation set")
    for operation_id in value:
        _effect_operation_id(operation_id, field, refuse)
    if value != sorted(set(value)):
        refuse(f"{field}_invalid", f"{field} must be a sorted unique operation set")


def _effect_target(value: object, refuse: Any) -> str:
    if not isinstance(value, dict) or set(value) != {"kind", "coordinate", "identity_digest"}:
        refuse("target_invalid", "target must use the exact non-secret target shape")
    _enum(value["kind"], EFFECT_TARGET_KINDS, "target_kind", refuse)
    _bounded_string(value["coordinate"], 1, 1024, "target_coordinate", refuse)
    _sha256(value["identity_digest"], "target_identity_digest", refuse)
    return value["kind"]


def _effect_confirmation(value: object, field: str, refuse: Any) -> None:
    if not isinstance(value, dict) or set(value) != {"kind", "locator", "expected_digest"}:
        refuse(f"{field}_invalid", f"{field} must use the exact non-secret confirmation shape")
    _enum(
        value["kind"],
        {"git_ref_equals", "git_remote_ref_equals", "github_idempotency_marker", "deployment_artifact_equals", "none"},
        f"{field}_kind",
        refuse,
    )
    _bounded_string(value["locator"], 1, 1024, f"{field}_locator", refuse)
    _sha256(value["expected_digest"], f"{field}_expected_digest", refuse)


def _effect_nullable_confirmation(value: object, refuse: Any) -> None:
    if value is not None:
        _effect_confirmation(value, "confirmation", refuse)


def _effect_budget_rows(value: object, field: str, refuse: Any) -> None:
    if not isinstance(value, list) or len(value) > 64:
        refuse(f"{field}_invalid", f"{field} must be a bounded budget table")
    keys = []
    for row in value:
        if not isinstance(row, dict) or set(row) != {"budget_id", "amount"}:
            refuse(f"{field}_invalid", f"{field} rows must use exact fields")
        amount = _json_integer(row["amount"])
        if not _identifier(row["budget_id"]) or amount is None or not 0 <= amount <= 1_000_000_000:
            refuse(f"{field}_invalid", f"{field} rows violate bounded semantics")
        keys.append(row["budget_id"])
    if keys != sorted(set(keys)):
        refuse(f"{field}_invalid", f"{field} rows must be sorted and unique")


def _normalized_effect_budget_rows(value: list[object]) -> list[object]:
    rows = deepcopy(value)
    for row in rows:
        if isinstance(row, dict):
            amount = _json_integer(row.get("amount"))
            if amount is not None:
                row["amount"] = amount
    return rows


def _effect_nullable_budget_rows(value: object, field: str, refuse: Any) -> None:
    if value is not None:
        _effect_budget_rows(value, field, refuse)


def _effect_binding(record: Mapping[str, object], refuse: Any) -> None:
    _effect_operation_id(record["operation_id"], "operation_id", refuse)
    _run_id(record["run_id"], refuse)
    _run_item_id(record["item_id"], "item_id", refuse)
    _attempt_id(record["attempt_id"], "attempt_id", refuse)
    _record_ref(record["attempt_started_id"], "attempt-started-", "attempt_started_id", refuse)
    _sha256(record["fence_token"], "fence_token", refuse)
    _enum(record["effect_type"], EFFECT_TYPES, "effect_type", refuse)
    target_kind = _effect_target(record["target"], refuse)
    if target_kind != _EFFECT_TARGET_BY_TYPE[record["effect_type"]]:
        refuse("target_kind_invalid", "target kind must match the closed effect type pairing")
    _sha256(record["request_digest"], "request_digest", refuse)
    _bounded_string(record["idempotency_key"], 1, 128, "idempotency_key", refuse)
    _effect_confirmation(record["expected_confirmation"], "expected_confirmation", refuse)
    _enum(record["reconciliation_adapter"], _EFFECT_RECONCILIATION_ADAPTERS, "reconciliation_adapter", refuse)
    _enum(record["risk_class"], {"low", "medium", "high", "critical"}, "risk_class", refuse)
    _effect_budget_rows(record["budget_claim"], "budget_claim", refuse)


def _effect_approval_refs(record: Mapping[str, object], refuse: Any) -> None:
    for field, prefix in (
        ("approval_request_id", "approval-request-"),
        ("approval_decision_id", "approval-decision-"),
        ("approval_consumption_id", "approval-consumed-resume-"),
    ):
        if record[field] is not None:
            _record_ref(record[field], prefix, field, refuse)


def _effect_outcome_refs(record: Mapping[str, object], refuse: Any, *, acknowledgement: bool) -> None:
    _record_ref(record["effect_intent_id"], "effect-intent-", "effect_intent_id", refuse)
    _record_ref(record["effect_dispatched_id"], "effect-dispatched-", "effect_dispatched_id", refuse)
    if acknowledgement and record["effect_acknowledged_id"] is not None:
        _record_ref(record["effect_acknowledged_id"], "effect-acknowledged-", "effect_acknowledged_id", refuse)


def _effect_evidence_ref(value: object, field: str, refuse: Any) -> None:
    if not isinstance(value, str) or re.fullmatch(
        r"(?:effect-confirmed|effect-failed|effect-unknown|effect-reconciled)-" + _UUID7,
        value,
    ) is None:
        refuse(f"{field}_invalid", f"{field} must use a terminal effect evidence UUIDv7 prefix")


def _capability_set(value: object, field: str, refuse: Any) -> None:
    if not isinstance(value, list) or len(value) > 64:
        refuse(f"{field}_invalid", f"{field} must be a sorted unique bounded capability set")
    for item in value:
        _capability(item, refuse)
    if value != sorted(value) or len(value) != len(set(value)):
        refuse(f"{field}_invalid", f"{field} must be a sorted unique bounded capability set")


def _budget_rows(value: object, field: str, refuse: Any) -> None:
    if not isinstance(value, list) or len(value) > 64:
        refuse(f"{field}_invalid", f"{field} must be a bounded budget table")
    keys = []
    for row in value:
        if not isinstance(row, dict) or set(row) != {"budget_id", "amount"}:
            refuse(f"{field}_invalid", f"{field} rows must use exact fields")
        amount = _json_integer(row["amount"])
        if not _identifier(row["budget_id"]) or amount is None or not 1 <= amount <= 1_000_000_000:
            refuse(f"{field}_invalid", f"{field} rows violate bounded semantics")
        keys.append(row["budget_id"])
    if keys != sorted(set(keys)):
        refuse(f"{field}_invalid", f"{field} rows must be sorted and unique")


def _normalized_budget_rows(value: list[object]) -> list[object]:
    rows = deepcopy(value)
    for row in rows:
        if isinstance(row, dict):
            amount = _json_integer(row.get("amount"))
            if amount is not None:
                row["amount"] = amount
    return rows


def _workspace_policies(value: object, field: str, refuse: Any) -> None:
    allowed = {"patch_only", "isolated_worktree"}
    if not isinstance(value, list) or any(not isinstance(item, str) or item not in allowed for item in value):
        refuse(f"{field}_invalid", f"{field} must be a sorted closed workspace-policy set")
    if value != sorted(value) or len(value) != len(set(value)):
        refuse(f"{field}_invalid", f"{field} must be a sorted closed workspace-policy set")


def _spawn_children(value: object, refuse: Any) -> None:
    expected = {
        "item_id", "task_contract_id", "task_contract", "task_contract_digest",
        "depth", "budget_allocation", "capability_ceiling", "workspace_policy",
        "workspace_key", "concurrency_key", "capability_selector",
        "requires_cancellation", "requires_callback", "retry_class",
        "effect_safety", "merge_gate",
    }
    if not isinstance(value, list) or not 1 <= len(value) <= 8:
        refuse("spawn_children_invalid", "children must be a bounded nonempty list")
    keys = []
    contract_ids = []
    for child in value:
        if not isinstance(child, dict) or set(child) != expected:
            refuse("spawn_children_invalid", "children must use exact fields")
        _run_item_id(child["item_id"], "child_item_id", refuse)
        _record_ref(child["task_contract_id"], "task-contract-", "task_contract_id", refuse)
        contract_ids.append(child["task_contract_id"])
        contract = child["task_contract"]
        if not isinstance(contract, dict) or set(contract) != {
            "objective", "non_goals", "areas_to_avoid", "input_hashes",
            "acceptance_checks", "constraints", "risk_class", "retry_policy", "dependencies",
        }:
            refuse("child_task_contract_invalid", "embedded task contract must use exact governed fields")
        try:
            from .contracts import TaskContract, contract_digest

            typed = TaskContract.create(**contract)
        except (ProtocolRefusal, TypeError, ValueError) as exc:
            if isinstance(exc, ProtocolRefusal):
                raise
            refuse("child_task_contract_invalid", "embedded task contract is invalid")
        _sha256(child["task_contract_digest"], "task_contract_digest", refuse)
        if child["task_contract_digest"] != contract_digest(typed):
            refuse("child_task_contract_digest_invalid", "embedded contract digest must match")
        depth = _json_integer(child["depth"])
        if depth is None or not 1 <= depth <= 16:
            refuse("child_depth_invalid", "child depth must be bounded")
        _budget_rows(child["budget_allocation"], "budget_allocation", refuse)
        _capability_set(child["capability_ceiling"], "capability_ceiling", refuse)
        _enum(child["workspace_policy"], {"patch_only", "isolated_worktree"}, "workspace_policy", refuse)
        for field in ("workspace_key", "concurrency_key", "capability_selector", "retry_class"):
            if not _identifier(child[field]):
                refuse(f"{field}_invalid", f"{field} must be a bounded identifier")
        for field in ("requires_cancellation", "requires_callback"):
            if not isinstance(child[field], bool):
                refuse(f"{field}_invalid", f"{field} must be boolean")
        _enum(child["effect_safety"], {"idempotent", "non_idempotent", "unknown_effect"}, "effect_safety", refuse)
        if child["merge_gate"] is not None and not _identifier(child["merge_gate"]):
            refuse("merge_gate_invalid", "merge_gate must be null or a bounded identifier")
        keys.append(child["item_id"])
    if keys != sorted(set(keys)):
        refuse("spawn_children_invalid", "children must be sorted and unique by item_id")
    if len(contract_ids) != len(set(contract_ids)):
        refuse("spawn_contract_id_duplicate", "spawn child contract IDs must be unique")


def _spawn_dependency_edges(value: object, refuse: Any) -> None:
    if not isinstance(value, list) or len(value) > 8192:
        refuse("dependency_edges_invalid", "spawn dependency edges must be bounded")
    normalized = []
    for edge in value:
        if not isinstance(edge, dict) or set(edge) != {"source", "target", "requires", "failure_policy"}:
            refuse("dependency_edges_invalid", "spawn edges must use exact fields")
        _run_item_id(edge["source"], "dependency_source", refuse)
        _run_item_id(edge["target"], "dependency_target", refuse)
        _enum(edge["requires"], {"produced", "verified", "accepted"}, "requires", refuse)
        _enum(edge["failure_policy"], {"fail_run", "skip_dependent", "continue"}, "failure_policy", refuse)
        if edge["source"] == edge["target"]:
            refuse("dependency_edges_invalid", "spawn edges cannot self-cycle")
        normalized.append((edge["source"], edge["target"], edge["requires"], edge["failure_policy"]))
    if normalized != sorted(set(normalized)):
        refuse("dependency_edges_invalid", "spawn edges must be sorted and unique")


def _attempt_id(value: object, field: str, refuse: Any) -> None:
    if not isinstance(value, str) or re.fullmatch("attempt-" + _UUID7, value) is None:
        refuse(f"{field}_invalid", f"{field} must use the attempt UUIDv7 prefix")


def _thread_evidence_object(
    value: object,
    field: str,
    measured_values: FrozenSet[object],
    refuse: Any,
    *,
    measured_class: str,
    unknown_value: object,
) -> object:
    if not isinstance(value, dict) or set(value) != {"value", "evidence_class"}:
        refuse(
            f"{field}_invalid",
            f"{field} must use the exact value and evidence_class object",
        )
    item = value["value"]
    evidence_class = value["evidence_class"]
    if evidence_class == "unknown":
        if item != unknown_value:
            refuse(
                f"{field}_invalid",
                f"unknown {field} evidence must use its exact unknown value",
            )
        return item
    if (
        evidence_class != measured_class
        or not isinstance(item, str)
        or item not in measured_values
    ):
        refuse(
            f"{field}_invalid",
            f"{field} violates its closed evidence vocabulary",
        )
    return item


def _thread_observation_record(record: Mapping[str, object], refuse: Any) -> None:
    kind = record["kind"]
    _enum(record["provider"], {"codex_local"}, "provider", refuse)
    if (
        not isinstance(record["provider_thread_id"], str)
        or re.fullmatch(_PROVIDER_UUID7, record["provider_thread_id"]) is None
    ):
        refuse(
            "provider_thread_id_invalid",
            "provider_thread_id must be one canonical lowercase hyphenated UUIDv7",
        )

    if kind == "thread_attachment_registered":
        _enum(record["subject_kind"], {"work_item", "attempt"}, "subject_kind", refuse)
        _run_item_id(record["work_item_id"], "work_item_id", refuse)
        if record["subject_kind"] == "attempt":
            _run_id(record["run_id"], refuse)
            _attempt_id(record["attempt_id"], "attempt_id", refuse)
        if not _identifier(record["registered_by"]):
            refuse("registered_by_invalid", "registered_by must be a bounded identifier")
        _timestamp_value(
            record["registered_at_testimony"],
            "registered_at_testimony",
            refuse,
        )
        return

    _record_ref(record["attachment_id"], "thread-attachment-", "attachment_id", refuse)
    if kind == "thread_attachment_detached":
        if not _identifier(record["detached_by"]):
            refuse("detached_by_invalid", "detached_by must be a bounded identifier")
        _timestamp_value(
            record["detached_at_testimony"],
            "detached_at_testimony",
            refuse,
        )
        return

    provider_status = _thread_evidence_object(
        record["provider_status"],
        "provider_status",
        frozenset({"not_loaded", "idle", "system_error", "active"}),
        refuse,
        measured_class="measured",
        unknown_value="unknown",
    )
    active_flags = record["active_flags"]
    if not isinstance(active_flags, dict) or set(active_flags) != {
        "value",
        "evidence_class",
    }:
        refuse(
            "active_flags_invalid",
            "active_flags must use the exact value and evidence_class object",
        )
    flags_value = active_flags["value"]
    if active_flags["evidence_class"] == "unknown":
        if flags_value is not None:
            refuse("active_flags_invalid", "unknown active_flags must be null")
    elif active_flags["evidence_class"] == "measured":
        allowed_flags = {"waiting_on_approval", "waiting_on_user_input"}
        if (
            not isinstance(flags_value, list)
            or any(not isinstance(item, str) for item in flags_value)
            or flags_value != sorted(set(flags_value))
            or any(item not in allowed_flags for item in flags_value)
            or provider_status != "active" and flags_value
        ):
            refuse(
                "active_flags_invalid",
                "measured active_flags must be a sorted unique closed flag list",
            )
    else:
        refuse(
            "active_flags_invalid",
            "active_flags evidence must be measured or unknown",
        )

    provider_updated_at = record["provider_updated_at"]
    if not isinstance(provider_updated_at, dict) or set(provider_updated_at) != {
        "value",
        "evidence_class",
    }:
        refuse(
            "provider_updated_at_invalid",
            "provider_updated_at must use the exact value and evidence_class object",
        )
    updated_value = provider_updated_at["value"]
    normalized_updated = _json_integer(updated_value)
    if provider_updated_at["evidence_class"] == "unknown":
        if updated_value is not None:
            refuse(
                "provider_updated_at_invalid",
                "unknown provider_updated_at must be null",
            )
    elif (
        provider_updated_at["evidence_class"] != "measured"
        or normalized_updated is None
        or not 0 <= normalized_updated <= 253402300799
    ):
        refuse(
            "provider_updated_at_invalid",
            "measured provider_updated_at must be a bounded Unix-second integer",
        )

    attention = _thread_evidence_object(
        record["attention"],
        "attention",
        frozenset(
            {
                "none",
                "waiting_on_approval",
                "waiting_on_user_input",
                "multiple",
            }
        ),
        refuse,
        measured_class="derived",
        unknown_value="unknown",
    )
    if flags_value is None:
        expected_attention = "unknown"
    elif len(flags_value) == 0:
        expected_attention = "none"
    elif len(flags_value) == 2:
        expected_attention = "multiple"
    else:
        expected_attention = flags_value[0]
    if attention != expected_attention:
        refuse(
            "attention_invalid",
            "attention must be the exact mechanical derivative of active_flags",
        )
    _enum(
        record["observation_outcome"],
        {"observed", "unknown"},
        "observation_outcome",
        refuse,
    )
    _enum(
        record["observation_reason"],
        {
            "exact_thread_read",
            "provider_unavailable",
            "provider_timeout",
            "thread_missing",
            "protocol_invalid",
            "cleanup_failed",
        },
        "observation_reason",
        refuse,
    )
    if record["observation_outcome"] == "observed":
        if (
            record["observation_reason"] != "exact_thread_read"
            or record["provider_status"]["evidence_class"] != "measured"
            or active_flags["evidence_class"] != "measured"
            or provider_updated_at["evidence_class"] != "measured"
            or record["attention"]["evidence_class"] != "derived"
            or provider_status == "unknown"
            or attention == "unknown"
        ):
            refuse(
                "observation_evidence_invalid",
                "observed testimony requires exact measured and derived evidence",
            )
    elif (
        record["observation_reason"] == "exact_thread_read"
        or record["provider_status"] != {
            "value": "unknown",
            "evidence_class": "unknown",
        }
        or active_flags != {"value": None, "evidence_class": "unknown"}
        or provider_updated_at != {"value": None, "evidence_class": "unknown"}
        or record["attention"] != {
            "value": "unknown",
            "evidence_class": "unknown",
        }
    ):
        refuse(
            "observation_evidence_invalid",
            "unknown testimony must preserve unknown evidence for every field",
        )
    _sha256(record["observation_digest"], "observation_digest", refuse)
    if record["observation_digest"] != thread_observation_digest(record):
        refuse(
            "observation_digest_invalid",
            "observation_digest must bind the exact normalized testimony",
        )
    _timestamp_value(
        record["observed_at_testimony"],
        "observed_at_testimony",
        refuse,
    )


def _opaque_identifier(value: object, field: str, refuse: Any) -> None:
    """Accept a bounded opaque binding without imposing a new identifier grammar."""

    _bounded_string(value, 1, 512, field, refuse)


def _resume_binding(
    resume_mode: object,
    provider_id: object,
    refuse: Any,
    *,
    adapter: object = None,
) -> None:
    _enum(resume_mode, {"native", "checkpoint_restart", "unsupported"}, "resume_mode", refuse)
    if provider_id is not None:
        refuse(
            "provider_session_or_thread_id_invalid",
            "current resume modes require a null provider session or thread id",
        )
    if adapter is None:
        if resume_mode == "native":
            refuse("resume_mode_invalid", "native resume remains reserved")
        return
    expected_mode = "checkpoint_restart" if adapter == "codex" else "unsupported"
    if resume_mode != expected_mode:
        refuse(
            "resume_mode_invalid",
            "resume mode must match the current closed adapter matrix",
        )


def _suspension_workspace(value: object, refuse: Any) -> None:
    if (
        not isinstance(value, str)
        or re.fullmatch(r"\x2fprivate/tmp/floati-work/work-" + _UUID7, value) is None
    ):
        refuse(
            "workspace_invalid",
            "workspace must use the closed orchestrator reservation path",
        )


def _attempt_binding(value: object, worker_session_id: object, refuse: Any) -> None:
    """Validate the reviewer's exact legacy literal or complete opaque binding object."""

    if value == "absent_legacy":
        return
    expected = {"attempt_id", "claim_id", "lease_id", "worker_session_id"}
    if not isinstance(value, dict) or set(value) != expected:
        refuse(
            "attempt_binding_invalid",
            "attempt binding must be absent_legacy or the exact complete binding object",
        )
    for field in sorted(expected):
        _opaque_identifier(value[field], field, refuse)
    if worker_session_id != value["worker_session_id"]:
        refuse(
            "attempt_binding_session_mismatch",
            "attempt binding worker session must equal the envelope worker session",
        )


def _attempt_ids(value: object, field: str, minimum: int, maximum: int, refuse: Any) -> None:
    if not isinstance(value, list) or not minimum <= len(value) <= maximum or value != sorted(set(value)):
        refuse(f"{field}_invalid", f"{field} must be a sorted unique bounded attempt id list")
    for item in value:
        _attempt_id(item, field, refuse)


def _effective_grants(value: object, high_watermark: object, refuse: Any) -> None:
    if not isinstance(value, list) or not 1 <= len(value) <= 64:
        refuse("effective_grants_invalid", "effective_grants must be a nonempty bounded list")
    ordering = []
    for row in value:
        if not isinstance(row, dict) or set(row) != {"capability_name", "grant_id", "physical_position"}:
            refuse("effective_grants_invalid", "each effective grant must use the exact v1 triple object")
        _capability(row["capability_name"], refuse)
        _record_ref(row["grant_id"], "capability-grant-", "grant_id", refuse)
        position = row["physical_position"]
        if not isinstance(position, int) or isinstance(position, bool) or not 1 <= position <= high_watermark:
            refuse("grant_position_invalid", "grant physical position must be within the captured high-watermark")
        ordering.append((row["capability_name"], row["grant_id"], position))
    if ordering != sorted(ordering) or len(set(ordering)) != len(ordering):
        refuse("effective_grants_order_invalid", "effective grant triples must be sorted and unique")


def _harness_segments(value: object, refuse: Any) -> None:
    if not isinstance(value, list) or not 1 <= len(value) <= 32:
        refuse("harness_segments_invalid", "harness segments must be a nonempty bounded list")
    expected = list(range(1, len(value) + 1))
    for ordinal, segment in enumerate(value, 1):
        if not isinstance(segment, dict) or set(segment) != {"ordinal", "harness_session_id"} or segment["ordinal"] != ordinal:
            refuse("harness_segments_invalid", "harness segments must use contiguous explicit ordinals")
        _record_ref(segment["harness_session_id"], "worker-", "harness_session_id", refuse)


def _harness_segments_v1(value: object, refuse: Any) -> None:
    if not isinstance(value, list) or not 1 <= len(value) <= 32:
        refuse("harness_segments_invalid", "harness segments must be a nonempty bounded list")
    segment_ids = set()
    for ordinal, segment in enumerate(value, 1):
        if (
            not isinstance(segment, dict)
            or not {"ordinal", "harness_session_id", "segment_id", "segment_kind"} <= set(segment)
            or set(segment) - {"ordinal", "harness_session_id", "segment_id", "segment_kind", "predecessor_segment_id"}
            or type(segment.get("ordinal")) is not int
            or segment["ordinal"] != ordinal
        ):
            refuse("harness_segments_invalid", "harness segments must use contiguous explicit ordinals")
        _record_ref(segment["harness_session_id"], "worker-", "harness_session_id", refuse)
        _record_ref(segment["segment_id"], "seg-", "segment_id", refuse)
        if segment["segment_id"] in segment_ids:
            refuse("harness_segment_id_duplicate", "segment_id must be unique within one attempt lineage")
        segment_ids.add(segment["segment_id"])
        _enum(segment["segment_kind"], {"initial", "resume", "fork", "handoff"}, "segment_kind", refuse)
        if segment["segment_kind"] == "initial":
            if "predecessor_segment_id" in segment:
                refuse("harness_segments_invalid", "initial harness segments must omit predecessor_segment_id")
        else:
            if "predecessor_segment_id" not in segment:
                refuse("harness_segments_invalid", "transition harness segments require predecessor_segment_id")
            _record_ref(segment["predecessor_segment_id"], "seg-", "predecessor_segment_id", refuse)


def _record_ref(value: object, prefix: str, field: str, refuse: Any) -> None:
    if not isinstance(value, str) or re.fullmatch(re.escape(prefix) + _UUID7, value) is None:
        refuse(f"{field}_invalid", f"{field} must use the governed record UUIDv7 prefix")


def _backoff(value: object, refuse: Any) -> None:
    if not isinstance(value, dict) or set(value) != {"strategy", "base_delay_ms", "cap_delay_ms", "jitter"}:
        refuse("backoff_invalid", "backoff must use the exact v0 object")
    _enum(value["strategy"], {"fixed", "exponential"}, "backoff_strategy", refuse)
    for field in ("base_delay_ms", "cap_delay_ms"):
        delay = value[field]
        if not isinstance(delay, int) or isinstance(delay, bool) or not 0 <= delay <= 86400000:
            refuse("backoff_invalid", "backoff delays must be bounded integers")
    if value["base_delay_ms"] > value["cap_delay_ms"]:
        refuse("backoff_invalid", "backoff base delay must not exceed its cap")
    _enum(value["jitter"], {"sha256_25pct"}, "backoff_jitter", refuse)


def _run_item_ids(value: object, field: str, minimum: int, maximum: int, refuse: Any) -> None:
    if not isinstance(value, list) or not minimum <= len(value) <= maximum:
        refuse(f"{field}_invalid", f"{field} must be a sorted unique work-id set")
    seen = set()
    for item in value:
        _run_item_id(item, field, refuse)
        if item in seen:
            refuse(f"{field}_invalid", f"{field} must be a sorted unique work-id set")
        seen.add(item)
    if value != sorted(value):
        refuse(f"{field}_invalid", f"{field} must be a sorted unique work-id set")


def _identifier_set(value: object, field: str, minimum: int, maximum: int, refuse: Any, prefix: str = "") -> None:
    if not isinstance(value, list) or not minimum <= len(value) <= maximum:
        refuse(f"{field}_invalid", f"{field} must be a sorted unique identifier set")
    seen = set()
    for item in value:
        if prefix:
            if not isinstance(item, str) or re.fullmatch(prefix + _UUID7, item) is None:
                refuse(f"{field}_invalid", f"{field} has an invalid prefixed id")
        elif not _identifier(item):
            refuse(f"{field}_invalid", f"{field} has an invalid identifier")
        if item in seen:
            refuse(f"{field}_invalid", f"{field} must be a sorted unique identifier set")
        seen.add(item)
    if value != sorted(value):
        refuse(f"{field}_invalid", f"{field} must be a sorted unique identifier set")


def _contract_string_list(value: object, field: str, minimum: int, maximum: int, refuse: Any) -> None:
    if not isinstance(value, list) or not minimum <= len(value) <= maximum:
        refuse(field + "_invalid", field + " must be a bounded unique list")
    seen = set()
    for item in value:
        _bounded_string(item, 1, 4096, field, refuse)
        if item in seen:
            refuse(field + "_invalid", field + " must be a bounded unique list")
        seen.add(item)


def _contract_hashes(value: object, field: str, refuse: Any) -> None:
    if not isinstance(value, dict) or not 1 <= len(value) <= 64:
        refuse(field + "_invalid", field + " must be a bounded nonempty object")
    for key, digest in value.items():
        _bounded_string(key, 1, 128, field, refuse); _sha256(digest, field, refuse)


def _contract_strings(value: object, field: str, refuse: Any) -> None:
    if not isinstance(value, dict) or not 1 <= len(value) <= 64:
        refuse(field + "_invalid", field + " must be a bounded nonempty object")
    for key, item in value.items():
        _bounded_string(key, 1, 128, field, refuse); _bounded_string(item, 1, 4096, field, refuse)


def _avoid_areas(value: object, refuse: Any) -> None:
    if not isinstance(value, list) or not 1 <= len(value) <= 64:
        refuse("areas_to_avoid_invalid", "areas to avoid must be a bounded nonempty list")
    pairs = []
    for area in value:
        if not isinstance(area, dict) or set(area) != {"path", "region"}:
            refuse("areas_to_avoid_invalid", "each avoid area must exactly name path and region")
        _repository_document(area["path"], refuse); _bounded_string(area["region"], 1, 1024, "area_region", refuse)
        pairs.append((area["path"], area["region"]))
    if len(set(pairs)) != len(pairs):
        refuse("areas_to_avoid_invalid", "areas to avoid must not repeat")


def _contract_retry_policy(value: object, refuse: Any) -> None:
    if not isinstance(value, dict) or set(value) != {"max_attempts", "backoff"}:
        refuse("retry_policy_invalid", "retry policy must use exact v0 fields")
    attempts = value["max_attempts"]
    if not isinstance(attempts, int) or isinstance(attempts, bool) or not 1 <= attempts <= 32:
        refuse("retry_policy_invalid", "max attempts must be bounded")
    backoff = value["backoff"]
    if not isinstance(backoff, dict) or set(backoff) != {"base_delay_ms", "cap_delay_ms", "strategy"}:
        refuse("retry_policy_invalid", "contract backoff must use exact v0 fields")
    base, cap = backoff["base_delay_ms"], backoff["cap_delay_ms"]
    if any(not isinstance(item, int) or isinstance(item, bool) for item in (base, cap)) or not 0 <= base <= cap <= 86400000:
        refuse("retry_policy_invalid", "contract backoff delays must be bounded and ordered")
    _enum(backoff["strategy"], {"fixed", "exponential"}, "backoff_strategy", refuse)


def _run_record_binding(record: Mapping[str, object], refuse: Any) -> None:
    if not isinstance(record["run_id"], str) or re.fullmatch("run-" + _UUID7, record["run_id"]) is None:
        refuse("run_id_invalid", "run_id must use the run UUIDv7 prefix")
    _run_item_id(record["item_id"], "item_id", refuse)


def _contract_replacement_fields(value: object, refuse: Any) -> None:
    allowed = {"objective", "non_goals", "areas_to_avoid", "input_hashes", "acceptance_checks", "constraints", "risk_class", "retry_policy", "dependencies"}
    if not isinstance(value, dict) or not 1 <= len(value) <= len(allowed) or not set(value) <= allowed:
        refuse("replacement_fields_invalid", "amendments may replace only a bounded nonempty task-contract field set")
    for field, replacement in value.items():
        if field == "objective":
            _bounded_string(replacement, 1, 4096, field, refuse)
        elif field in {"non_goals", "dependencies"}:
            _contract_string_list(replacement, field, 0 if field == "dependencies" else 1, 64, refuse)
        elif field == "areas_to_avoid":
            _avoid_areas(replacement, refuse)
        elif field == "input_hashes":
            _contract_hashes(replacement, field, refuse)
        elif field in {"acceptance_checks", "constraints"}:
            _contract_strings(replacement, field, refuse)
        elif field == "risk_class":
            _enum(replacement, {"low", "medium", "high", "critical"}, field, refuse)
        else:
            _contract_retry_policy(replacement, refuse)


def _dependency_edges(value: object, item_ids: object, refuse: Any) -> None:
    if not isinstance(value, list) or len(value) > 8192:
        refuse("dependency_edges_invalid", "dependency_edges must contain at most 8192 edges")
    if not isinstance(item_ids, list):
        refuse("dependency_edges_invalid", "dependency edges require item ids")
    order = {item: index for index, item in enumerate(item_ids)}
    normalized = []
    for edge in value:
        if not isinstance(edge, dict) or not set(edge) <= {"source", "target", "requires", "failure_policy"} or not {"source", "target"} <= set(edge):
            refuse("dependency_edges_invalid", "each dependency edge has source, target, optional requires, and optional failure policy")
        source, target = edge["source"], edge["target"]
        _run_item_id(source, "dependency_source", refuse); _run_item_id(target, "dependency_target", refuse)
        requires = edge.get("requires", "accepted")
        _enum(requires, {"produced", "verified", "accepted"}, "requires", refuse)
        failure_policy = edge.get("failure_policy", "fail_run")
        _enum(failure_policy, {"fail_run", "skip_dependent", "continue"}, "failure_policy", refuse)
        if source == target or source not in order or target not in order or order[source] >= order[target]:
            refuse("dependency_edges_invalid", "dependency endpoints must be prior distinct run items")
        normalized.append((source, target, requires, failure_policy))
    if normalized != sorted(set(normalized)):
        refuse("dependency_edges_invalid", "dependency edges must be sorted and unique")


def _repository_document(value: object, refuse: Any) -> None:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 1024
        or _terminal_unsafe(value)
        or value.startswith("/")
        or any(component in ("", ".", "..") for component in value.split("/"))
    ):
        refuse("doc_invalid", "doc must be a contained repository-relative path")


def _bounded_note(value: object, refuse: Any) -> None:
    if not isinstance(value, str) or len(value) > 1024 or _terminal_unsafe(value):
        refuse("note_invalid", "note must be a string of at most 1024 characters")


def _bounded_string(value: object, minimum: int, maximum: int, field: str, refuse: Any) -> None:
    if (
        not isinstance(value, str)
        or not minimum <= len(value) <= maximum
        or _terminal_unsafe(value)
    ):
        refuse(f"{field}_invalid", f"{field} violates its v0 string bounds")


def _terminal_unsafe(value: str) -> bool:
    return any(
        unicodedata.category(character) == "Cc"
        or unicodedata.category(character) == "Cs"
        or unicodedata.bidirectional(character) in _BIDI_CONTROLS
        for character in value
    )


def _artifact_bindings(value: object, refuse: Any) -> None:
    if not isinstance(value, list) or len(value) > 32:
        refuse("artifact_bindings_invalid", "artifact_bindings must contain at most 32 bindings")
    seen = set()
    for binding in value:
        if not isinstance(binding, dict) or set(binding) != {"repo", "sha", "doc"}:
            refuse("artifact_binding_invalid", "each artifact binding must contain repo, sha, and doc")
        _repository(binding["repo"], refuse)
        _git_sha(binding["sha"], refuse)
        _repository_document(binding["doc"], refuse)
        key = (binding["repo"], binding["sha"], binding["doc"])
        if key in seen:
            refuse("artifact_binding_duplicate", "artifact bindings must be unique")
        seen.add(key)


def _capability(value: object, refuse: Any) -> None:
    if not isinstance(value, str) or _CAPABILITY.fullmatch(value) is None:
        refuse("capability_invalid", "capability must be a bounded dotted identifier")


def _bounded_scope(value: object, refuse: Any, field: str = "scope") -> None:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 512
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        refuse(f"{field}_invalid", f"{field} must be 1 to 512 visible characters")


def _enum(value: object, values: set[str], field: str, refuse: Any) -> None:
    if not isinstance(value, str) or value not in values:
        refuse(f"{field}_invalid", f"{field} is not a v0 value")


def _timestamp_value(value: object, field: str, refuse: Any) -> datetime:
    if not isinstance(value, str) or _TIMESTAMP.fullmatch(value) is None:
        refuse(f"{field}_invalid", f"{field} must be a UTC RFC3339 timestamp")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        refuse(f"{field}_invalid", f"{field} is not a real timestamp")
        raise AssertionError("unreachable")

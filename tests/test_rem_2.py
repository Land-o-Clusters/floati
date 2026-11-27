"""REM-2: ProtocolRefusal codes classified by inline remedy=.

A code is BOUND when every ProtocolRefusal site passes a non-empty remedy
or the code is in DRILL_REMEDIES, MIXED when some sites do, UNBOUND
otherwise. The unbound set is a ratchet: it may only shrink.
Send-map codes are not repo-wide bound; only DRILL_REMEDIES are.
"""

from __future__ import annotations

import ast
import hashlib
import tempfile
import unittest
from pathlib import Path

from floati.errors import DRILL_REMEDIES


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
CONSTRUCTORS = frozenset({"ProtocolRefusal", "IntegrityFailure", "DurabilityFailure"})
ROLE_VERB_FILES = frozenset(
    {
        "floati/role_assignment.py",
        "floati/role_templates.py",
        "floati/admin_cli.py",
    }
)

PINNED_PROTOCOL_COUNT = 1516

PINNED_KIND_NONE_COUNT = 1159

PINNED_BOUND_COUNT = 357

PINNED_UNBOUND_COUNT = 1132

PINNED_MIXED_COUNT = 27

PINNED_UNRESOLVED_PROTOCOL = 140

PINNED_INTEGRITY_COUNT = 168

PINNED_DURABILITY_COUNT = 26

PINNED_ROOT_SHARED = frozenset({
    'root_not_absolute',
    'root_required',
    'root_symlinked_entry',
    'root_unavailable',
})

PINNED_INTEGRITY_DURABILITY = frozenset({
    'adhoc_dispatch_authentication_failed',
    'deployment_manifest_invalid',
    'duplicate_record_id',
    'effect_evidence_invalid',
    'epoch_selected_member_changed',
    'fleet_update_completion_invalid',
    'fleet_update_install_metadata_invalid',
    'fleet_update_install_readback_invalid',
    'fleet_update_owner_review_invalid',
    'fleet_update_receipt_invalid',
    'fleet_update_receipt_order_invalid',
    'fleet_update_step_invalid',
    'fleet_update_waiter_binding_readback_invalid',
    'journal_chain_interrupted',
    'ledger_record_limit',
    'ledger_too_large',
    'message_retraction_duplicate',
    'message_retraction_party_invalid',
    'message_retraction_session_invalid',
    'quota_receipt_invalid',
    'reason_code_invalid',
    'record_kind_invalid',
    'record_not_object',
    'record_too_large',
    'schema_version_invalid',
    't3_child_link_unbound',
    'thread_attachment_detached',
    'thread_attachment_missing',
    'tide_reading_invalid',
    'update_index_storage_invalid',
    'update_ledger_record_invalid',
    'wake_daemon_adapter_unknown',
    'work_dependencies_blocked',
    'work_owner_mismatch',
    'worker_claim_already_bound',
    'worker_session_mismatch',
    'worker_transition_invalid',
    'workspace_declaration_invalid',
    '{dyn}_invalid',
    '{dyn}_unavailable',
})

PINNED_ROLE_INVALID_OUTSIDE = frozenset({
    'context_role_invalid',
    'node_projection_role_invalid',
    'operator_role_invalid',
})

PINNED_OTHER_COUNT = 1112

PINNED_PROTOCOL_DIGEST = '27451b5d6137677cf415b66b2767ce75dda6788b36ef0521106d485ddc35e088'

PINNED_KIND_NONE_DIGEST = '12c08184ab109d628fdc89c6151a0b484c3d6cc9338a3cca3f8468283123483e'

PINNED_OTHER_DIGEST = 'e500f21ff83041ebef0686840d4a5fe21e438098b6872716c4694151a9e7dcc5'

PINNED_INTEGRITY_DIGEST = '2a7a3f183d04a965cdbf833d9e7cd135d9138080232b55cb0dc8f935809cd640'

PINNED_DURABILITY_DIGEST = 'bdf41d0bc0206ba1e3221dfd7f56ad1410ae25bc6a4d357d55eec4649eafd0f5'

PINNED_BOUND = frozenset({
    'ack_item_unknown',
    'address_invalid',
    'address_preview_required',
    'address_reply_invalid',
    'adhoc_dispatch_busy',
    'adhoc_result_unbanked',
    'adhoc_teardown_blocked',
    'archive_backup_refused',
    'archive_catalog_entries_unsupported',
    'archive_checkpoint_unsupported',
    'archive_consumption_checkpoint_blocked',
    'archive_consumption_domain_unsupported',
    'archive_consumption_projection_required',
    'archive_consumption_replay_unsupported',
    'archive_execution_checkpoint_blocked',
    'archive_execution_domain_unsupported',
    'archive_policy_invalid',
    'archive_presentation_domain_unsupported',
    'archive_projection_replay_unsupported',
    'archive_retention_clock_invalid',
    'archive_source_pool_unsupported',
    'archive_survivor_admission_unsupported',
    'archive_survivor_bundle_unsupported',
    'archive_survivor_sources_unsupported',
    'arguments_invalid',
    'ask_already_resolved',
    'ask_parties_mismatch',
    'ask_question_id_invalid',
    'ask_question_unknown',
    'authority_holder_mismatch',
    'bridge_publication_invalid',
    'car_acceptance_fence_changed',
    'car_acceptance_fence_invalid',
    'claude_hook_options_invalid',
    'claude_inbox_state_invalid',
    'codex_hook_install_coordinates_missing',
    'codex_host_hook_conflict',
    'codex_host_hooks_invalid',
    'codex_wait_holder_testimony_short',
    'codex_wait_hook_conflict',
    'codex_wait_start_time_unmeasurable',
    'consume_ack_binding_mismatch',
    'consume_idempotency_conflict',
    'consume_item_archived',
    'consume_item_not_acknowledged',
    'consume_item_retracted',
    'consume_item_unknown',
    'consume_items_invalid',
    'consume_recipient_mismatch',
    'consume_session_mismatch',
    'consume_session_required',
    'consumption_snapshot_contention',
    'cursor_hook_install_codex_options',
    'cursor_wait_deadline_invalid',
    'cursor_wait_harness_unsupported',
    'cursor_wait_inbox_unreadable',
    'cursor_wait_poll_invalid',
    'cursor_wait_workspace_mismatch',
    'cursor_wait_workspace_unbound',
    'deployment_currency_unavailable',
    'dispatch_brief_invalid',
    'doctor_home_invalid',
    'door_cancelled',
    'door_terminal_io_failed',
    'epoch_history_controller_only',
    'epoch_history_query_unsupported',
    'epoch_lock_required',
    'epoch_rotation_clock_invalid',
    'epoch_selected_writer_scope_invalid',
    'gh_authentication_absent',
    'hold_already_released',
    'hold_ambiguous',
    'hold_expected_release_invalid',
    'hold_history_uncertain',
    'hold_key_conflict',
    'hold_node_unknown',
    'hold_none_open',
    'hold_unknown',
    'hook_burn_path_shape_hostful',
    'hook_burn_release_stale',
    'hook_confinement_leaked',
    'hook_confinement_release_stale',
    'hook_confinement_result_invalid',
    'hook_install_failure_hook_invalid',
    'hook_install_hooks_invalid',
    'hook_install_root_invalid',
    'hook_install_runtime_invalid',
    'hook_install_workspace_invalid',
    'hook_precondition_release_unreadable',
    'hook_trust_config_writable',
    'hook_uninstall_options_invalid',
    'inbound_hold_already_decided',
    'inbound_hold_already_open',
    'inbound_hold_changed',
    'inbound_hold_decision_seat_context',
    'inbound_hold_expected_release_invalid',
    'inbound_hold_history_uncertain',
    'inbound_hold_human_act_unavailable',
    'inbound_hold_human_act_unconfirmed',
    'inbound_hold_item_not_held',
    'inbound_hold_item_unknown',
    'inbound_hold_key_conflict',
    'inbound_hold_node_unknown',
    'inbound_hold_none_open',
    'inbound_hold_unapproved',
    'installed_reader_identity_invalid',
    'interactive_terminal_required',
    'lane_base_unreachable',
    'lane_declaration_invalid',
    'lane_declaration_missing',
    'lane_repo_undeclared',
    'lane_retirement_preflight_failed',
    'lane_row_invalid',
    'lane_workspace_exists',
    'lane_workspace_fence_isolation_unavailable',
    'lane_workspace_force_reason_required',
    'lane_workspace_in_use',
    'lane_workspace_inspection_unavailable',
    'lane_workspace_ledger_bytes_limit',
    'lane_workspace_ledger_record_limit',
    'lane_workspace_node_unknown',
    'lane_workspace_record_id_conflict',
    'lane_workspace_record_invalid',
    'lane_workspace_unpushed',
    'lane_workspace_unrecorded',
    'ledger_lock_timeout',
    'ledger_policy_invalid',
    'ledger_policy_symlink',
    'lifecycle_prompts_harness_mismatch',
    'lifecycle_prompts_harness_unsupported',
    'lifecycle_prompts_name_reserved',
    'lifecycle_prompts_target_foreign',
    'lifecycle_prompts_target_invalid',
    'lifecycle_prompts_workspace_invalid',
    'message_class_invalid',
    'native_protocol_limit',
    'native_session_unmaterialized',
    'native_status_unsupported',
    'needs_budget_invalid',
    'needs_coordinate_invalid',
    'needs_hold_not_held',
    'needs_hold_unknown',
    'needs_human_act_seat_context',
    'needs_human_act_unavailable',
    'needs_human_act_unconfirmed',
    'needs_item_closed',
    'needs_item_kind_invalid',
    'needs_item_unknown',
    'needs_key_conflict',
    'needs_key_invalid',
    'needs_node_unknown',
    'needs_not_filer',
    'needs_observed_not_correctable',
    'needs_read_invalid',
    'needs_source_config_invalid',
    'needs_source_unconfigured',
    'needs_text_invalid',
    'node_add_plan_invalid',
    'node_add_plan_path_invalid',
    'node_add_plan_path_not_absolute',
    'node_boot_first_hold_incomplete',
    'node_boot_first_hold_unproved',
    'node_boot_hook_setup_required',
    'node_boot_session_ended',
    'node_boot_workspace_mismatch',
    'node_boot_workspace_unrooted',
    'node_teardown_marker_invalid',
    'node_teardown_session_superseded',
    'note_empty',
    'prep_clear_actor_mismatch',
    'prep_clear_architect_unresolved',
    'prep_clear_claim_not_held',
    'prep_clear_complement_invalid',
    'prep_clear_idempotency_conflict',
    'prep_clear_participant_unresolved',
    'prep_clear_pushed_tip_absent',
    'prep_clear_stop_incomplete',
    'prep_clear_workspace_invalid',
    'prep_clear_workspace_unreadable',
    'presentation_authority_changed',
    'presentation_batch_invalid',
    'presentation_ledger_limit',
    'presentation_output_too_large',
    'process_pid_invalid',
    'profile_operations_unsupported',
    'profile_update_binding_invalid',
    'quiet_actor_not_coordinator',
    'quiet_exempt_unknown',
    'quiet_history_uncertain',
    'quiet_key_conflict',
    'quiet_node_unknown',
    'quiet_parameter_invalid',
    'quiet_pressure_unavailable',
    'quiet_process_table_unknown',
    'quiet_release_file_invalid',
    'quiet_settle_cap_reached',
    'quiet_target_changed',
    'quiet_target_unknown',
    'quiet_target_unresolved',
    'quiet_window_interrupted',
    'quiet_window_not_orphaned',
    'quiet_window_open',
    'quiet_window_orphaned',
    'quiet_window_released',
    'quiet_window_unknown',
    'reader_older_than_ledger',
    'recipient_readiness_invalid',
    'retired_home_disarm_key_conflict',
    'role_architect_invalid',
    'role_assignment_missing',
    'role_invalid',
    'role_template_edit_invalid',
    'role_template_exists',
    'role_template_idempotency_conflict',
    'role_template_invalid',
    'role_template_name_mismatch',
    'role_template_path_invalid',
    'role_template_reserved',
    'role_template_write_conflict',
    'role_template_write_pending',
    'rollback_host_artifacts_differ',
    'rollback_host_photograph_invalid',
    'route_already_declared',
    'route_endpoints_invalid',
    'route_not_declared',
    'run_id_invalid',
    'seat_board_claim_contested',
    'seat_board_drain_unknown',
    'seat_board_idempotency_conflict',
    'seat_board_resume_stale',
    'seat_board_workspace_unbound',
    'seat_board_workspace_undeclared',
    'seat_declaration_deaf',
    'seat_execution_callback_invalid',
    'seat_execution_callback_unsupported',
    'seat_execution_car_absent',
    'seat_execution_car_ambiguous',
    'seat_execution_claim_stale',
    'seat_execution_claim_unsupported',
    'seat_execution_controller_only',
    'seat_execution_coordinator_absent',
    'seat_execution_epoch_unknown',
    'seat_execution_invocation_closed',
    'seat_execution_running_unclosed',
    'seat_execution_source_invalid',
    'seat_execution_workspace_invalid',
    'seat_harness_config_invalid',
    'seat_migration_key_conflict',
    'seat_onboard_hook_unavailable',
    'seat_onboard_invalid',
    'seat_onboard_io_failed',
    'seat_teardown_activity_in_flight',
    'seat_teardown_activity_unknown',
    'seat_teardown_authority_superseded',
    'seat_teardown_capacity',
    'seat_teardown_coordinator_invalid',
    'seat_teardown_declaration_invalid',
    'seat_teardown_home_invalid',
    'seat_teardown_idempotency_conflict',
    'seat_teardown_mail_pending',
    'seat_teardown_pause_superseded',
    'seat_teardown_primary_invalid',
    'seat_teardown_record_too_large',
    'seat_teardown_report_invalid',
    'seat_teardown_work_pending',
    'seat_teardown_workspace_unbound',
    'seat_teardown_zcode_config_unlocated',
    'seat_transport_preflight_invalid',
    'seat_waiter_lifecycle_invalid',
    'send_preparation_unavailable',
    'setup_codex_hook_present',
    'setup_confirmation_changed',
    'setup_confirmation_invalid',
    'setup_executable_invalid',
    'setup_file_exists',
    'setup_file_invalid',
    'setup_hook_seat_mismatch',
    'setup_path_invalid',
    'setup_preimage_changed',
    'setup_receipt_invalid',
    'setup_receipt_missing',
    'setup_root_not_fresh',
    'setup_seat_required',
    'setup_teardown_arguments',
    'setup_teardown_changed',
    'sha_absent',
    'sha_unbanked',
    'sha_unchecked',
    'signature_tool_absent',
    'snapshot_identity_fence_failed',
    'solo_identity_ambiguous',
    'stop_hold_close_conflict',
    'stop_hold_closure_unproven',
    'stop_hold_identity_invalid',
    'stop_hold_invalid',
    'stop_hook_definition_change_unacknowledged',
    'supersedes_not_head',
    'uninstall_receipt_dir_absolute_required',
    'uninstall_receipt_dir_dry_run_conflict',
    'uninstall_receipt_dir_symlinked',
    'uninstall_receipt_dir_unwritable',
    'uninstall_wake_supervisor_remove_failed',
    'uninstall_wake_supervisor_unidentified',
    'uninstall_wake_sweep_directory_required',
    'update_check_missing',
    'update_consent_changed',
    'update_consent_epoch_stale',
    'update_consent_missing',
    'update_consent_revoked',
    'update_envelope_too_large',
    'update_http_status',
    'update_install_metadata_missing',
    'update_package_manager_owned',
    'update_redirect_refused',
    'update_rollback_prior_missing',
    'update_rollback_target_mismatch',
    'update_rollback_to_invalid',
    'update_signature_unverified',
    'update_transport_failed',
    'update_trust_changed',
    'update_trust_unprovisioned',
    'update_version_unavailable',
    'version_skew',
    'wait_payload_absent',
    'wait_payload_invalid',
    'waiter_arm_identity_invalid',
    'waiter_harness_mismatch',
    'waiter_invocation_unproven',
    'wake_bind_provider_config_missing',
    'wake_daemon_codex_executable_absent',
    'wake_daemon_exception_detail_invalid',
    'wake_daemon_executable_digest_mismatch',
    'wake_daemon_lifecycle_idempotency_conflict',
    'wake_daemon_log_prune_marker_unwritable',
    'wake_daemon_log_prune_receipt_unwritable',
    'wake_daemon_log_receipt_unwritable',
    'wake_daemon_log_rotation_exists',
    'wake_daemon_log_symlink',
    'wake_daemon_receipts_not_regular',
    'wake_daemon_stop_unproven',
    'wake_daemon_supervisor_digest_mismatch',
    'wake_daemon_zcode_entry_absent',
    'wake_daemon_zcode_node_absent',
    'wake_idempotency_key_invalid',
    'wake_pause_intent_conflict',
    'wake_pause_intent_invalid',
    'work_already_claimed',
    'work_duplicate_open',
    'work_unknown',
    'zcode_entrypoint_unavailable',
    'zcode_entrypoint_unverified',
    'zcode_hook_uninstall_absent',
    'zcode_hook_uninstall_harness_unsupported',
    'zcode_hook_uninstall_mismatch',
    'zcode_wait_harness_mismatch',
    'zcode_wait_workspace_mismatch',
    'zcode_wait_workspace_unbound',
})

PINNED_MIXED = frozenset({
    'adhoc_dispatch_authentication_failed',
    'adhoc_write_confinement_unsupported',
    'capability_selector_unsatisfied',
    'effect_launch_binding_invalid',
    'epoch_selected_member_changed',
    'idempotency_conflict',
    'idempotency_key_invalid',
    'lane_workspace_dirty',
    'lane_workspace_identity_mismatch',
    'ledger_byte_limit',
    'ledger_record_limit',
    'ledger_too_large',
    'purge_trash_unavailable',
    'recipient_unregistered',
    'record_too_large',
    'repository_invalid',
    'role_answer_invalid',
    'role_template_unknown',
    'root_required',
    'unknown_node',
    'wake_daemon_codex_binding_source_invalid',
    'wake_daemon_executable_invalid',
    'wake_daemon_receipts_symlink',
    'wizard_input_invalid',
    'work_item_id_invalid',
    'work_owner_mismatch',
    '{dyn}_invalid',
})

PINNED_UNBOUND = frozenset(
    Path(__file__).with_name("rem_2_unbound_pin.txt").read_text(encoding="utf-8").splitlines()
)


def _set_digest(codes: frozenset[str]) -> str:
    return hashlib.sha256("\n".join(sorted(codes)).encode("utf-8")).hexdigest()


def _call_name(func: ast.AST) -> str:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


def _const_str(node: ast.AST | None) -> str | None:
    if node is None:
        return None
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        parts: list[str] = []
        for value in node.values:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                parts.append(value.value)
            else:
                parts.append("{dyn}")
        return "".join(parts)
    return None


def _code_arg(call: ast.Call) -> ast.AST | None:
    for keyword in call.keywords:
        if keyword.arg == "code":
            return keyword.value
    if call.args:
        return call.args[0]
    return None


def constructor_sites(root: Path) -> list[tuple[str, str, str | None, bool]]:
    """Return `(file, constructor, code, nonempty_remedy)` for each constructor."""

    found: list[tuple[str, str, str | None, bool]] = []
    for path in sorted((root / "floati").rglob("*.py")):
        rel = path.relative_to(root).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = _call_name(node.func)
            if name not in CONSTRUCTORS:
                continue
            found.append(
                (rel, name, _const_str(_code_arg(node)), _nonempty_remedy(_remedy_node(node)))
            )
    return found


def _remedy_node(call: ast.Call) -> ast.AST | None:
    for keyword in call.keywords:
        if keyword.arg == "remedy":
            return keyword.value
    if len(call.args) >= 3:
        return call.args[2]
    return None


def _nonempty_remedy(node: ast.AST | None) -> bool:
    if node is None:
        return False
    if isinstance(node, ast.Constant):
        if node.value is None:
            return False
        if isinstance(node.value, str):
            return bool(node.value.strip())
        return False
    if isinstance(node, ast.Name) and node.id == "None":
        return False
    return True


def bounded_string_role_invalid(root: Path) -> bool:
    """True when validate_role can serve role_invalid through _bounded_string."""

    path = root / "floati" / "records.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename="floati/records.py")
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if _call_name(node.func) != "_bounded_string":
            continue
        if len(node.args) < 4:
            continue
        field = node.args[3]
        if isinstance(field, ast.Constant) and field.value == "role":
            return True
    return False


def census(root: Path) -> dict[str, object]:
    protocol: set[str] = set()
    integrity: set[str] = set()
    durability: set[str] = set()
    unresolved_protocol = 0
    role_literals: set[str] = set()
    site_bounds: dict[str, list[bool]] = {}
    for rel, kind, code, site_bound in constructor_sites(root):
        if code is None:
            if kind == "ProtocolRefusal":
                unresolved_protocol += 1
            continue
        if kind == "ProtocolRefusal":
            protocol.add(code)
            site_bounds.setdefault(code, []).append(site_bound)
            if code == "role_invalid" or code.endswith("role_invalid"):
                if rel not in ROLE_VERB_FILES:
                    role_literals.add(code)
        elif kind == "IntegrityFailure":
            integrity.add(code)
        else:
            durability.add(code)
    if bounded_string_role_invalid(root):
        protocol.add("role_invalid")
        role_literals.add("role_invalid")
        site_bounds.setdefault("role_invalid", []).append(True)
    bound: set[str] = set()
    mixed: set[str] = set()
    unbound: set[str] = set()
    for code in protocol:
        if code in DRILL_REMEDIES:
            bound.add(code)
            continue
        flags = site_bounds.get(code, [])
        if flags and all(flags):
            bound.add(code)
        elif flags and any(flags):
            mixed.add(code)
        else:
            unbound.add(code)
    kind_none = frozenset(unbound | mixed)
    root_shared = frozenset(code for code in kind_none if code.startswith("root_"))
    integrity_durability = frozenset(
        (kind_none & integrity) | (kind_none & durability)
    )
    role_outside = frozenset(role_literals & kind_none)
    other = kind_none - root_shared - integrity_durability - role_outside
    return {
        "protocol": frozenset(protocol),
        "kind_none": kind_none,
        "bound": frozenset(bound),
        "mixed": frozenset(mixed),
        "unbound": frozenset(unbound),
        "unresolved_protocol": unresolved_protocol,
        "integrity": frozenset(integrity),
        "durability": frozenset(durability),
        "root_shared": root_shared,
        "integrity_durability": integrity_durability,
        "role_invalid_outside": role_outside,
        "other": frozenset(other),
    }


class Rem2KindNoneCensusTests(unittest.TestCase):
    def test_constructed_fixture_classes_kind_none_codes(self) -> None:
        """Catches a walker that cannot see constructor literals or role_invalid."""

        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        package = root / "floati"
        package.mkdir()
        (package / "__init__.py").write_text("", encoding="utf-8")
        (package / "sample.py").write_text(
            "from .errors import ProtocolRefusal, IntegrityFailure, DurabilityFailure\n"
            "raise ProtocolRefusal('root_required', 'x')\n"
            "raise ProtocolRefusal('operator_role_invalid', 'x')\n"
            "raise IntegrityFailure('record_not_object', 'x')\n"
            "raise DurabilityFailure('root_deleted', 'x')\n",
            encoding="utf-8",
        )
        (package / "records.py").write_text(
            "def _bounded_string(value, minimum, maximum, field, refuse):\n"
            "    refuse(f'{field}_invalid', 'x')\n"
            "def validate_role(value):\n"
            "    _bounded_string(value, 1, 64, 'role', None)\n",
            encoding="utf-8",
        )
        derived = census(root)
        self.assertIn("root_required", derived["kind_none"])
        self.assertIn("root_required", derived["root_shared"])
        self.assertIn("record_not_object", derived["integrity"])
        self.assertIn("operator_role_invalid", derived["role_invalid_outside"])
        self.assertIn("role_invalid", derived["bound"])
        self.assertIn("root_deleted", derived["durability"])

    def test_constructed_fixture_bound_site_is_not_kind_none(self) -> None:
        """Catches bind_refusal_remedy(code, None) ignoring inline remedy=."""

        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        package = root / "floati"
        package.mkdir()
        (package / "__init__.py").write_text("", encoding="utf-8")
        (package / "records.py").write_text("", encoding="utf-8")
        (package / "sample.py").write_text(
            "from .errors import ProtocolRefusal\n"
            "raise ProtocolRefusal('always_bound', 'x', remedy='retry with --ref')\n"
            "raise ProtocolRefusal('sometimes_bound', 'x', remedy='retry with --ref')\n"
            "raise ProtocolRefusal('sometimes_bound', 'y')\n"
            "raise ProtocolRefusal('never_bound', 'x')\n",
            encoding="utf-8",
        )
        derived = census(root)
        self.assertNotIn("always_bound", derived["kind_none"])
        self.assertIn("always_bound", derived["bound"])
        self.assertIn("sometimes_bound", derived["mixed"])
        self.assertIn("never_bound", derived["unbound"])

    def test_send_map_does_not_bind_codes_outside_the_send_path(self) -> None:
        """R3 RED: test_rem_2.py:437 counted SEND_REFUSAL_REMEDIES repo-wide."""

        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        package = root / "floati"
        package.mkdir()
        (package / "__init__.py").write_text("", encoding="utf-8")
        (package / "records.py").write_text("", encoding="utf-8")
        (package / "sample.py").write_text(
            "from .errors import ProtocolRefusal\n"
            "raise ProtocolRefusal('cannot_speak', 'x')\n",
            encoding="utf-8",
        )
        derived = census(root)
        self.assertIn("cannot_speak", derived["unbound"])
        self.assertNotIn("cannot_speak", derived["bound"])

    def test_live_tree_kind_none_census_is_pinned(self) -> None:
        """Catches a new unbound code, or a moved class, without a pin change."""

        derived = census(REPOSITORY_ROOT)
        protocol = derived["protocol"]
        kind_none = derived["kind_none"]
        self.assertEqual(PINNED_PROTOCOL_COUNT, len(protocol), len(protocol))
        self.assertEqual(PINNED_KIND_NONE_COUNT, len(kind_none), len(kind_none))
        self.assertEqual(
            PINNED_UNRESOLVED_PROTOCOL,
            derived["unresolved_protocol"],
            derived["unresolved_protocol"],
        )
        self.assertEqual(
            PINNED_INTEGRITY_COUNT,
            len(derived["integrity"]),
            len(derived["integrity"]),
        )
        self.assertEqual(
            PINNED_DURABILITY_COUNT,
            len(derived["durability"]),
            len(derived["durability"]),
        )
        self.assertEqual(PINNED_ROOT_SHARED, derived["root_shared"])
        self.assertEqual(PINNED_INTEGRITY_DURABILITY, derived["integrity_durability"])
        self.assertEqual(PINNED_ROLE_INVALID_OUTSIDE, derived["role_invalid_outside"])
        self.assertEqual(PINNED_OTHER_COUNT, len(derived["other"]), len(derived["other"]))
        self.assertEqual(PINNED_PROTOCOL_DIGEST, _set_digest(protocol), _set_digest(protocol))
        self.assertEqual(
            PINNED_KIND_NONE_DIGEST, _set_digest(kind_none), _set_digest(kind_none)
        )
        self.assertEqual(
            PINNED_OTHER_DIGEST,
            _set_digest(derived["other"]),
            _set_digest(derived["other"]),
        )
        self.assertEqual(
            PINNED_INTEGRITY_DIGEST,
            _set_digest(derived["integrity"]),
            _set_digest(derived["integrity"]),
        )
        self.assertEqual(
            PINNED_DURABILITY_DIGEST,
            _set_digest(derived["durability"]),
            _set_digest(derived["durability"]),
        )
        self.assertTrue(set(DRILL_REMEDIES) <= protocol)
        self.assertTrue(set(DRILL_REMEDIES) <= derived["bound"])
        self.assertEqual(PINNED_BOUND, derived["bound"])
        self.assertEqual(PINNED_MIXED, derived["mixed"])
        self.assertEqual(PINNED_BOUND_COUNT, len(derived["bound"]), len(derived["bound"]))
        self.assertEqual(PINNED_MIXED_COUNT, len(derived["mixed"]), len(derived["mixed"]))
        self.assertLessEqual(
            len(derived["unbound"]),
            PINNED_UNBOUND_COUNT,
            len(derived["unbound"]),
        )
        arrived_unbound = derived["unbound"] - PINNED_UNBOUND
        self.assertEqual(
            frozenset(),
            arrived_unbound,
            "new unbound codes: " + ", ".join(sorted(arrived_unbound)),
        )


if __name__ == "__main__":
    unittest.main()

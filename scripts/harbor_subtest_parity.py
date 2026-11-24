"""Opt-in affected-method parity evidence; native identities stay untouched.

This is a focused verification route, never a full-suite qualification.
No tests or product modules are imported before record() enters its private HOME.
"""
import hashlib
import json
import os
from pathlib import Path
import platform
import stat
import subprocess
import sys

from parallel_isolation import _ordinary_owned_directory

# Original measured mismatch inventory; closures are separately parent-ruled.
ORIGINAL_IDS = (
    "tests.test_admin_cli.AdminCliTests.test_partial_and_non_tty_node_add_refuse_before_mutation_with_full_remedy",
    "tests.test_approval_suspension.ApprovalSuspensionProjectionTests.test_suspension_runtime_and_schema_contracts_match",
    "tests.test_cli.WakeEvaluateCliTests.test_wake_decision_schema_and_runtime_close_cross_field_and_boundary_variants",
    "tests.test_codex_live_adapter.CodexAppServerAdapterTests.test_workspace_and_local_evidence_are_owner_private",
    "tests.test_codex_wait.CodexWaitParticipationTests.test_waiter_never_opens_foreign_bus_root",
    "tests.test_cursor_session_pause.CursorSessionPauseTests.test_symlink_release_cannot_hide_an_exact_session_pause",
    "tests.harbor_demo_capture_assets.DemoCaptureAssetTests.test_capture_temporary_parent_accepts_only_public_safe_storage",
    "tests.harbor_demo_capture_assets.DemoCaptureAssetTests.test_source_sha_and_capture_text_fail_closed",
    "tests.test_deploy.DeploymentWriterTests.test_update_refuses_observed_shadow_even_when_scan_is_incomplete",
    "tests.test_doctor.LauncherInterpreterProvenanceTests.test_an_undeclarable_interpreter_is_refused_and_never_falls_back_to_path",
    "tests.test_doctor_remedy_schema.DoctorRemedySchemaTests.test_old_shape_enum_and_required_field_fences_remain",
    "tests.test_effect_controller.EffectControllerAuthorityTests.test_controller_policy_seal_preserves_method_metadata",
    "tests.test_effect_controller.WorkerEffectAuthorityTests.test_child_cannot_report_confirmation_or_raw_effect_record",
    "tests.test_effect_reconciliation.CompensationTests.test_compensation_confirm_requires_exact_preview_digest_and_approval",
    "tests.test_effect_reconciliation_exec.EffectReconciliationExecTests.test_exec_trusts_only_frozen_root_owned_canonical_interpreter",
    "tests.test_effect_reconciliation_observer.ReconciliationObserverTests.test_local_git_observes_only_exact_full_ref_and_object_through_fd",
    "tests.test_effect_reconciliation_observer.ReconciliationObserverTests.test_local_git_wrong_ref_wrong_sha_and_missing_object_fail_closed",
    "tests.test_effect_reconciliation_observer.ReconciliationObserverTests.test_remote_fixture_observes_exact_explicit_full_ref_without_local_fallback",
    "tests.test_effect_reconciliation_protocol.ReconciliationProtocolTests.test_result_refuses_wrong_request_binding_and_child_selected_types",
    "tests.test_effects.EffectCompensationProjectionTests.test_changed_or_cross_operation_compensation_references_refuse",
    "tests.test_export_public.ExportPublicTests.test_abbreviated_or_non_main_source_sha_is_refused",
    "tests.test_export_public.ReleaseBranchExportTests.test_release_source_ref_still_requires_exact_full_commit_sha",
    "tests.test_f2_board_projection.F2NativeBoardProjectionTests.test_replay_refuses_foreign_or_mismatched_predecessor_before_reporting",
    "tests.test_f2_readiness_schema_parity.ReadinessSchemaParityTests.test_hold_ids_and_terminal_safe_identity_bounds_agree",
    "tests.test_fu1_g1.FU1G1Tests.test_g1_fix3_bindings_validate_before_empty_or_excluded_workspace_maps",
    "tests.test_fu1_g1.FU1G1Tests.test_g1_reader_schema_and_runtime_reject_fabricated_rows",
    "tests.test_fu1_g2.FU1G2Tests.test_g1_fix3_bindings_validate_before_empty_or_excluded_workspace_maps",
    "tests.test_fu1_g2.FU1G2Tests.test_g1_reader_schema_and_runtime_reject_fabricated_rows",
    "tests.test_fu1_g3.FU1G3CompletionTests.test_g1_fix3_bindings_validate_before_empty_or_excluded_workspace_maps",
    "tests.test_fu1_g3.FU1G3CompletionTests.test_g1_reader_schema_and_runtime_reject_fabricated_rows",
    "tests.test_fu1_g3.FU1G3SagaTests.test_g1_fix3_bindings_validate_before_empty_or_excluded_workspace_maps",
    "tests.test_fu1_g3.FU1G3SagaTests.test_g1_reader_schema_and_runtime_reject_fabricated_rows",
    "tests.test_fu1_g4.FU1G4Tests.test_g1_fix3_bindings_validate_before_empty_or_excluded_workspace_maps",
    "tests.test_fu1_g4.FU1G4Tests.test_g1_reader_schema_and_runtime_reject_fabricated_rows",
    "tests.test_gauntlet_concurrency.ConcurrentWriterGauntletTests.test_twelve_owner_built_runs_keep_per_run_projection_and_ids_distinct_under_contention",
    "tests.test_gauntlet_recovery.RecoveryGauntletTests.test_hm3i_all_literal_run_traces_reopen_with_stable_physical_ids_and_observation",
    "tests.test_gauntlet_time.TimeHostilityGauntletTests.test_hm3i_run_projection_is_timestamp_invariant_for_all_literal_run_traces",
    "tests.test_in_guard_scope.InGuardScopeTests.test_status_default_and_json_name_validated_explicit_scope",
    "tests.test_installer_shadow.InstallerShadowEnumeratorTests.test_existing_invalid_entries_are_never_promoted_to_affirmative_none",
    "tests.test_ledger_repair.GovernedLedgerRepairTests.test_quarantine_refuses_nonselected_ledger_coordinates_before_mutation",
    "tests.test_message_consumption_lifecycle.MessageConsumptionLifecycleTests.test_extra_fields_versions_and_source_work_binding_refuse_in_both_validators",
    "tests.test_message_consumption_lifecycle.MessageConsumptionLifecycleTests.test_schema_valid_wrong_ack_recipient_and_work_references_fail_history",
    "tests.test_name_sweep.NameSweepLivingDocumentationTests.test_private_capture_corpus_does_not_expose_an_operator_account",
    "tests.test_name_sweep.NameSweepLivingDocumentationTests.test_public_capture_evidence_does_not_expose_an_operator_account",
    "tests.test_one_shot_wake.OneShotWakePathBindingTests.test_preview_refuses_unsafe_callback_paths_before_creating_local_testimony",
    "tests.test_orchestrate.FleetFaultDrillTests.test_three_fault_modes_are_distinct_and_leave_no_processes",
    "tests.harbor_parallel_isolation.ParallelIsolationTests.test_invalid_override_refuses_without_fallback",
    "tests.harbor_parallel_isolation.ParallelIsolationTests.test_worker_refuses_escaped_symlink_and_nonordinary_roots",
    "tests.harbor_parallel_qualification.ParallelQualificationTests.test_registry_duplicate_keys_aliases_modes_and_unknown_tools_refuse",
    "tests.test_record_validation.RecordValidationTests.test_v1_harness_binding_requires_explicit_segment_lineage_without_widening_v0",
    "tests.test_remote_control_coexist.RemoteControlCensusUnitTests.test_schema_binds_worker_metadata_to_exact_code_and_subject",
    "tests.test_remote_control_coexist.RemoteControlCensusUnitTests.test_schema_rejects_counted_only_severity_and_remediation_mismatches",
    "tests.test_remote_control_coexist.RemoteControlCensusUnitTests.test_schema_rejects_in_seat_severity_and_remediation_mismatches",
    "tests.test_remote_control_coexist.RemoteControlCensusUnitTests.test_schema_rejects_invalid_in_seat_counts_and_seat_coordinates",
    "tests.test_remote_control_coexist.RemoteControlCensusUnitTests.test_schema_rejects_missing_or_open_worker_metadata",
    "tests.test_role_library.RoleLibraryTests.test_local_source_symlink_and_url_refuse",
    "tests.test_run_segments.SegmentedRunStoreTests.test_invalid_constructor_inputs_refuse",
    "tests.test_seat_execution_host_tool_controls.HostToolCallbackControls.test_cli_overrides_cannot_replace_host_workspace_or_session",
    "tests.test_seat_execution_worker_binding.WorkerBindingTests.test_partial_governed_selector_never_falls_back_to_legacy",
    "tests.test_seat_harness_config.SeatHarnessConfigTests.test_entire_existing_profile_registry_is_validated_before_mutation",
    "tests.test_seat_harness_config.SeatHarnessConfigTests.test_entire_transport_registry_is_validated_before_mutation",
    "tests.test_seat_harness_config.SeatHarnessConfigTests.test_existing_profile_workspace_and_identity_each_refuse",
    "tests.test_seat_teardown.SeatTeardownTests.test_home_symlink_and_relative_boundary_refuse",
    "tests.test_sequencer_epoch.SequencerEpochTests.test_raw_epoch_mutations_require_a_live_exclusive_owner_proof",
    "tests.test_spawn_groups.SpawnGroupJoinTests.test_parent_cancellation_consumes_scheduled_retry_into_cancelled_truth",
    "tests.test_spawn_groups.SpawnGroupManagedTests.test_managed_intents_reject_extra_time_boolean_fraction_and_malformed_nested_values",
    "tests.test_spawn_groups.SpawnGroupManagedTests.test_spawn_intents_match_runtime_canonical_value_boundaries_before_ids",
    "tests.test_spawn_groups.SpawnGroupRecordTests.test_child_admitted_accepts_only_floati_workspace_in_runtime_and_schema",
    "tests.test_wait_command.WaitCommandTests.test_wait_never_resolves_its_root_from_the_ambient_environment",
    "tests.test_wake_daemon_adapters.WakeDaemonAdapterTests.test_zcode_interpreter_declaration_must_be_one_canonical_executable",
    "tests.test_wake_hold.WakeHoldRecordTests.test_generic_and_lower_writer_refuse_hold_rows_without_bytes",
    "tests.test_wake_hold.WakeHoldRecordTests.test_public_or_recovered_hold_ledger_writer_never_has_controller_authority",
    "tests.test_wake_receipt_legacy_read.DoctorLegacyFindingFactoryTests.test_named_subject_uses_existing_home_redaction_and_preserves_controls",
    "tests.test_worker_workspace_portability.WorkerWorkspacePortabilityTests.test_runtime_workspace_validators_accept_portable_shape_and_keep_refusals",
    "tests.test_worker_workspace_portability.WorkerWorkspacePortabilityTests.test_workspace_schemas_match_the_portable_runtime_shape",
    "tests.test_workers.WorkerEffectPipeTests.test_exec_bootstrap_path_must_be_absolute_owned_regular_and_nonsymlink",
    "tests.test_zcode_bridge_delivery.ZCodeBridgeDeliveryTests.test_registered_command_refuses_writable_fixed_store_and_banks_parents",
    "tests.test_zcode_bridge_doctor.AdoptedDoctorBridgeReaderTests.test_writable_store_or_banks_parent_cannot_testify_current",
    "tests.test_zcode_bridge_runtime.ZCodeBridgeRuntimeTests.test_invalid_injected_runtime_refuses_before_bus_or_firing",
    "tests.test_zcode_bridge_runtime.ZCodeBridgeRuntimeTests.test_verified_child_isolated_from_seat_bootstrap_and_python_environment",
)
CLOSURE_IDS = (
    "tests.test_gauntlet_concurrency.ConcurrentWriterGauntletTests."
    "test_twelve_process_receipt_result_race_never_refuses_an_unrelated_run_created",
    "tests.test_remote_control_coexist.RemoteControlCensusUnitTests."
    "test_schema_rejects_invalid_classification_and_worker_counts",
)
AFFECTED_IDS = tuple(sorted((*ORIGINAL_IDS, *CLOSURE_IDS)))
_FILES = ("census.json", "plan.json", "native-result.json", "aggregate.json",
          "events.jsonl", "unittest.log")
_LIMIT = 32 * 1024 * 1024


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate evidence field")
        result[key] = value
    return result


def _write(path, value):
    with path.open("x", encoding="utf-8") as stream:
        stream.write(_json(value) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def _bank(path):
    path = Path(path)
    _ordinary_owned_directory(path, Path("\x2fprivate/tmp"))
    info = path.stat()
    if stat.S_IMODE(info.st_mode) != 0o700:
        raise ValueError("parity bank must be owner-private")
    return path


def _bytes(path):
    _bank(path.parent)
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_nlink != 1 or info.st_mode & 0o022 or info.st_size > _LIMIT):
            raise ValueError("parity artifact is not bounded ordinary owned data")
        data = stream.read(_LIMIT + 1)
        if len(data) != info.st_size or len(data) > _LIMIT:
            raise ValueError("parity artifact changed while reading")
        return data


def _binding(data):
    return {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}


def source_inventory(root):
    """Full tracked bytes plus visible untracked files and any ignored Python."""
    def git(*args):
        return subprocess.check_output(["/usr/bin/git", *args], cwd=root,
                                       stderr=subprocess.PIPE)
    if Path(git("rev-parse", "--show-toplevel").decode().strip()).resolve() != root:
        raise ValueError("parity source must be the repository root")
    names = set(git("ls-files", "--cached", "--others", "--exclude-standard", "-z").split(b"\0"))
    names.update(name for name in git("ls-files", "--others", "-z").split(b"\0")
                 if name.endswith(b".py"))
    rows = []
    for name in sorted(names - {b""}):
        relative = os.fsdecode(name)
        path = root / relative
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or path.resolve() != path:
            raise ValueError("nonordinary parity source: " + relative)
        data = path.read_bytes()
        rows.append(dict(path=relative, mode=stat.S_IMODE(info.st_mode), **_binding(data)))
    return {"root": str(root), "head": git("rev-parse", "HEAD").decode().strip(),
            "index_sha256": hashlib.sha256(git("ls-files", "--stage", "-z")).hexdigest(),
            "rows": rows, "digest": hashlib.sha256(_json(rows).encode()).hexdigest()}


def _interpreter():
    return {"executable": str(Path(sys.executable).resolve()), "version": sys.version,
            "implementation": sys.implementation.name, "platform": platform.platform()}


def _selection():
    if len(set(AFFECTED_IDS)) != len(AFFECTED_IDS):
        raise ValueError("duplicate fixed affected selection")
    return {"original_ids": list(ORIGINAL_IDS), "closure_ids": list(CLOSURE_IDS),
            "ids": list(AFFECTED_IDS)}


def _successful(aggregate):
    return (aggregate["successful"] is True
            and aggregate["fixture_blocked_ids"] == []
            and aggregate["selected_ids"] == list(AFFECTED_IDS)
            and aggregate["started_ids"] == list(AFFECTED_IDS)
            and aggregate["stopped_ids"] == list(AFFECTED_IDS)
            and aggregate["tests_run"] == len(AFFECTED_IDS)
            and all(event["kind"] in ("success", "subtest_success")
                    for event in aggregate["events"]))


def record(root, output):
    """Run one fresh process's fixed original methods with their native fixtures."""
    import unittest
    import parallel_unittest as native
    import parallel_plan as planner

    root, output = Path(root).resolve(strict=True), _bank(output)
    before = source_inventory(root)  # Before importing any test module.
    fixtures = Path(os.environ["FLOATI_TEST_TEMP_ROOT"])
    _ordinary_owned_directory(fixtures, Path("\x2fprivate/tmp"))
    if fixtures != output / "worker" / "fixtures":
        raise ValueError("parity record lost its exact private worker binding")
    initial_home = Path(os.environ["HOME"])
    if fixtures not in initial_home.parents:
        raise ValueError("parity record HOME escaped its private fixture root")
    receipt = {"scope": "focused_native_methods_not_qualification", "qualified": False,
               "status": "invalid", "successful": False, "selection": _selection(),
               "source_before": before, "interpreter": _interpreter(),
               "pid": os.getpid(), "fixtures": str(fixtures), "initial_home": str(initial_home)}
    original_out, original_err = sys.stdout, sys.stderr
    try:
        with (output / "unittest.log").open("x", encoding="utf-8") as log:
            sys.stdout = sys.stderr = log
            loader = unittest.TestLoader()
            suite = loader.loadTestsFromNames(AFFECTED_IDS)
            if loader.errors:
                raise ValueError("affected loader errors: " + "\n".join(loader.errors))
            census = native._census(suite)
            if census["ids"] != list(AFFECTED_IDS):
                raise ValueError("affected loader changed fixed original method identities")
            # A narrowed census is explicit; no partial full-census unit selection.
            for unit in census["units"]:
                unit["serial_reason"] = "focused affected-method reference"
            plan = planner.make_plan(census, 1)
            if len(plan["assignments"]) != 1:
                raise ValueError("focused serial plan must have one assignment")
            _write(output / "census.json", census)
            _write(output / "plan.json", plan)
            with (output / "events.jsonl").open("x", encoding="utf-8") as journal:
                sequence = 0
                def emit(event):
                    nonlocal sequence
                    journal.write(_json({"sequence": sequence, "record": event}) + "\n")
                    journal.flush()
                    sequence += 1
                emit({"kind": "header", "selected_ids": census["ids"],
                      "population_digest": census["population_digest"]})
                result = native.run_suite(suite, plan["assignments"][0],
                                          census["population_digest"], log, event_sink=emit)
                emit({"kind": "footer", "result": result})
                os.fsync(journal.fileno())
            _write(output / "native-result.json", result)
            aggregate = planner.reconcile(census, plan, [result])
            _write(output / "aggregate.json", aggregate)
            receipt.update(status="complete", successful=_successful(aggregate))
    except Exception as error:
        receipt["error"] = type(error).__name__ + ": " + str(error)
    finally:
        sys.stdout, sys.stderr = original_out, original_err
        try:
            receipt["source_after"] = source_inventory(root)
            if receipt["source_after"] != before:
                raise ValueError("source changed during focused recording")
            final_home = Path(os.environ["HOME"])
            if fixtures not in final_home.parents:
                raise ValueError("final test HOME escaped its private fixture root")
            receipt["final_home"] = str(final_home)
        except Exception as error:
            receipt.update(status="invalid", successful=False,
                           error=type(error).__name__ + ": " + str(error))
        receipt["artifacts"] = {name: _binding(_bytes(output / name))
                                for name in _FILES if (output / name).exists()}
        _write(output / "record.json", receipt)
    return receipt


def _admit(bank, current):
    import parallel_plan as planner
    bank = _bank(bank)
    receipt_bytes = _bytes(bank / "record.json")
    receipt = json.loads(receipt_bytes, object_pairs_hook=_unique)
    if (receipt.get("scope") != "focused_native_methods_not_qualification"
            or receipt.get("status") != "complete" or receipt.get("successful") is not True
            or receipt.get("qualified") is not False
            or receipt.get("selection") != _selection()
            or receipt.get("source_before") != current or receipt.get("source_after") != current
            or receipt.get("interpreter") != _interpreter()
            or type(receipt.get("pid")) is not int or receipt["pid"] <= 0
            or set(receipt.get("artifacts", {})) != set(_FILES)):
        raise ValueError("focused record status/source/selection cannot be admitted")
    fixtures = bank / "worker" / "fixtures"
    if receipt.get("fixtures") != str(fixtures):
        raise ValueError("focused record fixture binding differs from its bank")
    _ordinary_owned_directory(fixtures, Path("\x2fprivate/tmp"))
    for field in ("initial_home", "final_home"):
        path = Path(receipt[field])
        _ordinary_owned_directory(path, Path("\x2fprivate/tmp"))
        if fixtures not in path.parents:
            raise ValueError("focused record HOME is not in its exact fixture root")
    raw = {name: _bytes(bank / name) for name in _FILES}
    if any(_binding(data) != receipt["artifacts"][name] for name, data in raw.items()):
        raise ValueError("focused record artifact binding mismatch")
    data = {name: json.loads(raw[name], object_pairs_hook=_unique)
            for name in _FILES if name.endswith(".json")}
    census, plan, result = data["census.json"], data["plan.json"], data["native-result.json"]
    if census["ids"] != list(AFFECTED_IDS) or len(plan["assignments"]) != 1:
        raise ValueError("focused census/plan differs from fixed method selection")
    frames = [json.loads(line, object_pairs_hook=_unique)
              for line in raw["events.jsonl"].splitlines()]
    if ([frame["sequence"] for frame in frames] != list(range(len(frames)))
            or frames[0]["record"] != {"kind": "header", "selected_ids": census["ids"],
                                      "population_digest": census["population_digest"]}
            or frames[-1]["record"] != {"kind": "footer", "result": result}):
        raise ValueError("focused journal sequence/header/footer mismatch")
    events = [frame["record"] for frame in frames[1:-1]]
    for kind, field in (("start", "started_ids"), ("stop", "stopped_ids")):
        if [e["test_id"] for e in events if e["kind"] == kind] != result[field]:
            raise ValueError("focused journal physical callback mismatch")
    if [e for e in events if e["kind"] not in ("start", "stop")] != result["events"]:
        raise ValueError("focused journal outcome mismatch")
    aggregate = planner.reconcile(census, plan, [result])
    if aggregate != data["aggregate.json"] or not _successful(aggregate):
        raise ValueError("focused aggregate cannot be admitted")
    return receipt, census, aggregate, _binding(receipt_bytes)


def compare(root, reference, candidate, output):
    """Admit two fixed record banks, then preserve the exact native comparison."""
    import parallel_plan as planner
    root, output = Path(root).resolve(strict=True), _bank(output)
    report = {"scope": "focused_native_methods_not_qualification", "qualified": False,
              "status": "invalid", "successful": False, "selection": _selection()}
    try:
        current = source_inventory(root)
        left, left_census, left_aggregate, left_binding = _admit(reference, current)
        right, right_census, right_aggregate, right_binding = _admit(candidate, current)
        if (Path(reference) == Path(candidate) or left["pid"] == right["pid"]
                or left["fixtures"] == right["fixtures"]
                or left["initial_home"] == right["initial_home"]
                or left["final_home"] == right["final_home"]):
            raise ValueError("focused parity requires distinct processes and private fixture roots")
        if left_census != right_census:
            raise ValueError("focused census differs between realizations")
        comparison = planner.compare_results(left_aggregate, right_aggregate)
        report.update(source_before=current, interpreter=_interpreter(),
                      reference={"path": str(reference), **left_binding},
                      candidate={"path": str(candidate), **right_binding},
                      comparison=comparison, status="complete",
                      successful=comparison["equivalent"])
        report["source_after"] = source_inventory(root)
        if report["source_after"] != current:
            raise ValueError("source changed during focused comparison")
    except Exception as error:
        report.update(status="invalid", successful=False,
                      error=type(error).__name__ + ": " + str(error))
    _write(output / "comparison.json", report)
    return report

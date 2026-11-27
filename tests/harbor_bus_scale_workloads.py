"""Closed workload and evidence controls; these data fixtures prove no speedup.

The helper validates coverage and observable witnesses, not the 1.5x budget.
Native execution and metric comparison belong to the owned runner and comparator.
"""

from __future__ import annotations

import argparse
import copy
from tests.bus_scale_test_profile import clone_fixture
from functools import lru_cache
import importlib
import unittest

from floati.cli import _parser
from floati.command_contract import describe_parser
from tests.bus_scale_attention_samples import attention_sample


try:
    _workloads = importlib.import_module("tests.bus_scale_workloads")
except ModuleNotFoundError as exc:
    if exc.name != "tests.bus_scale_workloads":
        raise
    _workloads = None


_COMMANDS = {
    "describe": "describe",
    "overlap-report": "overlap report",
    "lane-open": "lane open",
    "lane-close": "lane close",
    "sweep": "sweep",
    "reclaim": "reclaim",
    "send": "send",
    "verify": "verify",
    "inbox": "inbox",
    "wake-evaluate": "wake-evaluate",
    "wake-record": "wake-record",
    "ack": "ack",
    "sent": "sent",
    "log-history": "log",
    "status": "status",
    "effects": "effects",
    "effect-show": "effect show",
    "graph": "graph",
    "doctor": "doctor",
    "watch": "watch",
    "wait-start": "wait",
    "receipts-history": "receipts",
    "consume": "consume",
    "supervise": "supervise",
    "presence-report": "presence report",
    "presence-show": "presence show",
    "sequencer-status": "sequencer status",
    "work-add": "work add",
    "work-claim": "work claim",
    "work-complete": "work complete",
    "work-show": "work show",
    "node-drain": "node drain",
    "node-boot": "node boot",
    "node-teardown": "node teardown",
    "node-explain": "node explain",
    "node-prep-clear": "node prep-clear",
    "node-state-flush": "node state-flush",
    "node-prompts": "node prompts",
    "net-route-list": "net route list",
    "role-list": "role list",
    "role-show": "role show",
    "role-validate": "role validate",
    "quota-show": "quota show",
    "chart": "chart",
    "chart-timings": "chart timings",
    "seat-board": "seat board",
    "seat-teardown": "seat teardown",
    "wake-pause": "wake pause",
    "wake-resume": "wake resume",
    "wake-status": "wake status",
    "wake-arm": "wake arm",
    "wake-wait": "wake wait",
    "wake-daemon-status": "wake daemon status",
    "context-status": "context status",
    "context-turnover": "context turnover",
    "context-policy-show": "context policy show",
    "context-reading-record": "context reading record",
    "hold-start": "hold start",
    "hold-release": "hold release",
    "wait-idle": "seam:floati.codex_wait.run_stop_waiter:unchanged_idle_tick",
    "readiness-read": "seam:floati.mail_health.RecipientReadiness.observe",
    "receipt-append": "seam:floati.codex_wait_contract.CodexWaitReceiptLedger.record_exhaustion",
    "wake-callback": "wake-callback",
    "confluence-grant": "confluence grant",
    "confluence-revoke": "confluence revoke",
    "confluence-status": "confluence status",
    "confluence-bundle": "confluence bundle",
    "intake-scan": "intake scan",
    "intake-show": "intake show",
    "intake-preview": "intake preview",
    "wake-daemon-serve": "seam:floati.wake_daemon.WakeDaemon.serve:one_iteration"
}
_NATIVE = {
    "describe": [
        0,
        "ok",
        "command_contract_returned"
    ],
    "overlap-report": [
        0,
        "ok",
        "overlap_report_returned"
    ],
    "lane-open": [
        0,
        "ok",
        "lane_workspace_opened"
    ],
    "lane-close": [
        0,
        "ok",
        "lane_workspace_closed"
    ],
    "sweep": [
        0,
        "ok",
        "lane_sweep_returned"
    ],
    "reclaim": [
        0,
        "ok",
        "reclaim_plan_returned"
    ],
    "send": [
        0,
        "ok",
        "message_envelope_appended"
    ],
    "verify": [
        0,
        "ok",
        "delivery_claim_verified"
    ],
    "inbox": [
        0,
        "ok",
        "messages_presented"
    ],
    "wake-evaluate": [
        0,
        "ok",
        "wake_decision_returned"
    ],
    "wake-record": [
        0,
        "ok",
        "wake_attempt_recorded"
    ],
    "ack": [
        0,
        "ok",
        "acknowledgment_appended"
    ],
    "sent": [
        0,
        "ok",
        "sender_receipts_returned"
    ],
    "log-history": [
        0,
        "ok",
        "history_returned"
    ],
    "status": [
        0,
        "ok",
        "status_returned"
    ],
    "effects": [
        0,
        "ok",
        "effects_returned"
    ],
    "effect-show": [
        0,
        "ok",
        "effect_returned"
    ],
    "graph": [
        0,
        "ok",
        "graph_returned"
    ],
    "doctor": [
        35,
        "degraded",
        "doctor_returned"
    ],
    "watch": [
        0,
        "ok",
        "bounded_watch_returned"
    ],
    "wait-start": [
        0,
        "ok",
        "natural_deadline_exhausted"
    ],
    "receipts-history": [
        0,
        "ok",
        "history_returned"
    ],
    "consume": [
        0,
        "ok",
        "consumption_receipt_appended"
    ],
    "supervise": [
        0,
        "ok",
        "supervision_returned"
    ],
    "presence-report": [
        0,
        "ok",
        "presence_report_appended"
    ],
    "presence-show": [
        0,
        "ok",
        "presence_reports_returned"
    ],
    "sequencer-status": [
        0,
        "ok",
        "sequencer_status_returned"
    ],
    "work-add": [
        0,
        "ok",
        "work_item_added"
    ],
    "work-claim": [
        0,
        "ok",
        "work_item_claimed"
    ],
    "work-complete": [
        0,
        "ok",
        "consumed_work_completed"
    ],
    "work-show": [
        0,
        "ok",
        "work_items_returned"
    ],
    "node-drain": [
        0,
        "ok",
        "node_mail_drained"
    ],
    "node-boot": [
        0,
        "ok",
        "node_boot_returned"
    ],
    "node-teardown": [
        0,
        "ok",
        "node_teardown_returned"
    ],
    "node-explain": [
        0,
        "ok",
        "node_explanation_returned"
    ],
    "node-prep-clear": [
        0,
        "ok",
        "prep_clear_receipted"
    ],
    "node-state-flush": [
        0,
        "ok",
        "state_flush_receipted"
    ],
    "node-prompts": [
        0,
        "ok",
        "lifecycle_prompts_written"
    ],
    "net-route-list": [
        0,
        "ok",
        "declared_routes_returned"
    ],
    "role-list": [
        0,
        "ok",
        "roles_returned"
    ],
    "role-show": [
        0,
        "ok",
        "role_returned"
    ],
    "role-validate": [
        0,
        "ok",
        "role_validated"
    ],
    "quota-show": [
        0,
        "ok",
        "quota_testimony_returned"
    ],
    "chart": [
        0,
        "ok",
        "declared_chart_returned"
    ],
    "chart-timings": [
        0,
        "ok",
        "chart_timings_returned"
    ],
    "seat-board": [
        0,
        "ok",
        "seat_board_receipted"
    ],
    "seat-teardown": [
        0,
        "ok",
        "seat_teardown_completed"
    ],
    "wake-pause": [
        0,
        "ok",
        "wake_paused"
    ],
    "wake-resume": [
        0,
        "ok",
        "wake_resumed"
    ],
    "wake-status": [
        0,
        "ok",
        "wake_status_returned"
    ],
    "wake-arm": [
        0,
        "ok",
        "wake_armed"
    ],
    "wake-wait": [
        0,
        "ok",
        "cursor_followup_returned"
    ],
    "wake-daemon-status": [
        0,
        "ok",
        "wake_daemon_status_returned"
    ],
    "context-status": [
        0,
        "ok",
        "context_status_returned"
    ],
    "context-turnover": [
        0,
        "ok",
        "context_turnover_returned"
    ],
    "context-policy-show": [
        0,
        "ok",
        "context_policy_returned"
    ],
    "context-reading-record": [
        0,
        "ok",
        "context_reading_appended"
    ],
    "hold-start": [
        0,
        "ok",
        "hold_started"
    ],
    "hold-release": [
        0,
        "ok",
        "hold_released"
    ],
    "wait-idle": [
        0,
        "ok",
        "unchanged_idle_tick"
    ],
    "readiness-read": [
        0,
        "unknown",
        "readiness_observed"
    ],
    "receipt-append": [
        0,
        "consent_reopened",
        "exhaustion_receipt_appended"
    ],
    "wake-callback": [
        0,
        "ok",
        "wake_callback_receipted"
    ],
    "confluence-grant": [
        0,
        "ok",
        "confluence_grant_appended"
    ],
    "confluence-revoke": [
        0,
        "ok",
        "confluence_revocation_appended"
    ],
    "confluence-status": [
        0,
        "ok",
        "confluence_grants_returned"
    ],
    "confluence-bundle": [
        0,
        "ok",
        "confluence_bundle_written"
    ],
    "intake-scan": [
        0,
        "ok",
        "intake_scan_returned"
    ],
    "intake-show": [
        0,
        "ok",
        "intake_snapshots_returned"
    ],
    "intake-preview": [
        0,
        "ok",
        "intake_preview_returned"
    ],
    "wake-daemon-serve": [
        0,
        "ok",
        "daemon_poll_completed"
    ]
}
_HISTORY = frozenset([
    "sent",
    "log-history",
    "effects",
    "receipts-history",
    "work-show",
    "intake-show",
    "confluence-bundle"
])


def _must_not_execute(*args, **kwargs):
    raise AssertionError("workload scope must not execute a native handler")


def _subcommands(parser):
    actions = [a for a in parser._actions if isinstance(a, argparse._SubParsersAction)]
    if len(actions) != 1:
        raise AssertionError("expected one native subcommand registry")
    return actions[0]


@lru_cache(maxsize=1)
def _scope_template():
    return _workloads.scope_contract(_parser())


@lru_cache(maxsize=1)
def _observation_template():
    """Synthetic validator inputs, never native performance evidence."""
    rows = {}
    for index, case in enumerate(_COMMANDS, 1):
        exit_code, status, effect = _NATIVE[case]
        arms = {}
        for multiplier, arm_name in ((1, "1x"), (10, "10x")):
            digest = f"{index * 10 + multiplier:064x}"
            native = {
                "exit_code": exit_code,
                "status": status,
                "effect": {"kind": effect, "count": 1},
            }
            if case == "doctor":
                native["findings_sha256"] = "d" * 64
            arms[arm_name] = {
                "records": 100 * multiplier,
                "bytes": 102400 * multiplier,
                "seed_sha256": digest,
                "clone_sha256": digest,
                "child_collected": True,
                "raw_sha256": "a" * 64,
                "native": native,
                "coverage": {"cpu": True, "bytes": True, "locks": True},
                "metrics": {
                    "cpu_seconds": 0.01 * multiplier,
                    "bytes_read": 1024 * multiplier,
                    "max_ledger_lock_seconds": 0.001 * multiplier,
                },
            }
            if case in {"supervise", "watch"}:
                arms[arm_name]["attention_proof"] = attention_sample(case, multiplier)
        rows[case] = {
            "family": _scope_template()["cases"][case]["family"],
            "prefix_exact": True,
            "selected_work_equal": True,
            "arms": arms,
        }
    return rows


class BusScaleWorkloadTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(
            _workloads,
            "missing tests.bus_scale_workloads: implement scope_contract and "
            "require_observations before qualifying workload coverage",
        )
        for name in ("scope_contract", "require_observations"):
            self.assertTrue(callable(getattr(_workloads, name, None)), name)
        self._fixture_parser = None
        self.scope = clone_fixture(_scope_template())

    @property
    def parser(self):
        # Parser mutation controls always receive their own fresh parser.
        if self._fixture_parser is None:
            self._fixture_parser = _parser()
        return self._fixture_parser

    def observations(self):
        """Return an independent mutable copy of the shared literal population."""
        rows = clone_fixture(_observation_template())
        for case, row in rows.items():
            row["family"] = self.scope["cases"][case]["family"]
        return rows

    def reject(self, observations):
        with self.assertRaises(ValueError):
            _workloads.require_observations(self.scope, observations)

    def test_scope_retains_all_full_native_executable_rows(self):
        expected = {
            " ".join(row["path"]): row
            for row in describe_parser(self.parser)["commands"]
            if row["executable"]
        }
        self.assertEqual(186, len(expected))
        self.assertEqual(expected, self.scope["inventory"])
        self.assertIn("wake daemon serve", self.scope["inventory"])
        self.assertFalse(self.scope["inventory"]["wake daemon serve"]["public"])
        self.assertIn("update", self.scope["inventory"])

    def test_exact_70_cases_keep_native_start_and_diagnostic_idle_distinct(self):
        self.assertEqual(set(_COMMANDS), set(self.scope["cases"]))
        for case, command in _COMMANDS.items():
            with self.subTest(case=case):
                row = self.scope["cases"][case]
                self.assertEqual(command, row["command"])
                self.assertIs(row["history_output"], case in _HISTORY)
                self.assertIsInstance(row["family"], str)
                self.assertTrue(row["family"].strip())

    def test_every_native_path_is_measured_or_explicitly_excluded_once(self):
        measured = {command for command in _COMMANDS.values() if not command.startswith("seam:")}
        measured.add("wake daemon serve")  # Am7 permits exactly its native one-iteration seam.
        excluded = self.scope["excluded"]
        self.assertFalse(measured & set(excluded))
        self.assertEqual(set(self.scope["inventory"]), measured | set(excluded))
        self.assertEqual(118, len(excluded))
        self.assertIn("setup", excluded)
        self.assertIn("replay", excluded)
        self.assertIn("who", excluded)
        for path, reason in excluded.items():
            with self.subTest(path=path):
                self.assertIsInstance(reason, str)
                self.assertTrue(reason.strip())

    def test_new_command_under_administration_prefix_cannot_be_excluded_implicitly(self):
        update = _subcommands(self.parser).choices["update"]
        child = _subcommands(update).add_parser("scale-probe")
        child.set_defaults(handler=_must_not_execute)
        with self.assertRaises(ValueError):
            _workloads.scope_contract(self.parser)

    def test_new_hidden_direct_handler_cannot_disappear_from_scope(self):
        child = _subcommands(self.parser).add_parser("scale-probe", floati_public=False)
        child.set_defaults(direct_handler=_must_not_execute)
        with self.assertRaises(ValueError):
            _workloads.scope_contract(self.parser)

    def test_changed_excluded_command_contract_still_requires_review(self):
        init = _subcommands(self.parser).choices["init"]
        init.add_argument("--scale-probe")
        with self.assertRaises(ValueError):
            _workloads.scope_contract(self.parser)

    def test_changed_measured_command_contract_cannot_reuse_old_scope(self):
        send = _subcommands(self.parser).choices["send"]
        send.add_argument("--scale-probe")
        with self.assertRaises(ValueError):
            _workloads.scope_contract(self.parser)

    def test_complete_observable_pairs_accept_valid_measured_red(self):
        # All three metrics deliberately grow 10x. Coverage is not a green verdict.
        _workloads.require_observations(self.scope, self.observations())

    def test_missing_or_invented_case_is_not_complete_workload_coverage(self):
        for extra in (False, True):
            with self.subTest(extra=extra):
                rows = self.observations()
                if extra:
                    rows["invented-case"] = clone_fixture(rows["send"])
                else:
                    del rows["send"]
                self.reject(rows)

    def test_full_parser_rows_alone_are_not_native_observations(self):
        rows = {
            case: clone_fixture(self.scope["inventory"].get(command, {"command": command}))
            for case, command in _COMMANDS.items()
        }
        self.reject(rows)

    def test_missing_or_wrong_primary_family_cannot_qualify_another_ledger(self):
        for replacement in (None, "unreviewed-family"):
            with self.subTest(replacement=replacement):
                rows = self.observations()
                if replacement is None:
                    del rows["receipt-append"]["family"]
                else:
                    rows["receipt-append"]["family"] = replacement
                self.reject(rows)

    def test_missing_extra_or_mislabeled_scale_arm_fails(self):
        for operation in ("missing", "extra", "renamed"):
            with self.subTest(operation=operation):
                rows = self.observations()
                arms = rows["send"]["arms"]
                if operation == "missing":
                    del arms["10x"]
                elif operation == "extra":
                    arms["100x"] = clone_fixture(arms["10x"])
                else:
                    arms["ten"] = arms.pop("10x")
                self.reject(rows)

    def test_early_native_refusal_is_not_a_fast_success_measurement(self):
        for case in _COMMANDS:
            with self.subTest(case=case):
                rows = self.observations()
                native = rows[case]["arms"]["10x"]["native"]
                native["exit_code"] = 20
                native["status"] = "refused"
                self.reject(rows)

    def test_expected_status_and_exit_are_both_required(self):
        for field, value in (("exit_code", 31), ("status", "intentional_silence")):
            with self.subTest(field=field):
                rows = self.observations()
                rows["send"]["arms"]["1x"]["native"][field] = value
                self.reject(rows)

    def test_wait_idle_cannot_replace_entire_native_deadline(self):
        rows = self.observations()
        rows["wait-start"]["arms"]["10x"]["native"]["effect"]["kind"] = "unchanged_idle_tick"
        self.reject(rows)

    def test_append_requires_an_observed_effect_not_an_idempotent_noop(self):
        for case in ("send", "receipt-append"):
            for value in (0, -1, True, 0.5):
                with self.subTest(case=case, value=value):
                    rows = self.observations()
                    rows[case]["arms"]["1x"]["native"]["effect"]["count"] = value
                    self.reject(rows)

    def test_wrong_or_missing_native_effect_is_not_success(self):
        for replacement in (None, {"kind": "argument_parse_only", "count": 1}):
            with self.subTest(replacement=replacement):
                rows = self.observations()
                native = rows["status"]["arms"]["1x"]["native"]
                if replacement is None:
                    del native["effect"]
                else:
                    native["effect"] = replacement
                self.reject(rows)

    def test_expected_doctor_degradation_requires_raw_findings_witness(self):
        rows = self.observations()
        del rows["doctor"]["arms"]["1x"]["native"]["findings_sha256"]
        self.reject(rows)

    def test_missing_or_false_exact_prefix_and_selected_work_witnesses_fail(self):
        for field in ("prefix_exact", "selected_work_equal"):
            for value in (None, False, 1):
                with self.subTest(field=field, value=value):
                    rows = self.observations()
                    if value is None:
                        del rows["send"][field]
                    else:
                        rows["send"][field] = value
                    self.reject(rows)

    def test_nonpositive_noninteger_or_illegal_fixture_lengths_fail(self):
        for field, values in (
            ("records", (0, -1, True, 100.5, 100001)),
            ("bytes", (0, -1, True, 1024.5, 64 * 1024 * 1024 + 1)),
        ):
            for value in values:
                with self.subTest(field=field, value=value):
                    rows = self.observations()
                    rows["send"]["arms"]["10x"][field] = value
                    self.reject(rows)

    def test_record_lengths_must_be_exactly_tenfold(self):
        rows = self.observations()
        rows["send"]["arms"]["10x"]["records"] += 1
        self.reject(rows)

    def test_actual_byte_growth_need_not_be_exactly_tenfold(self):
        # Native registry prelude and provider-switch transactions need not be
        # fixed width. The exact-prefix witness and actual legal lengths bind it.
        rows = self.observations()
        rows["readiness-read"]["arms"]["10x"]["bytes"] += 97
        _workloads.require_observations(self.scope, rows)

    def test_tenfold_records_require_increasing_actual_byte_length(self):
        for adjustment in (0, -1):
            with self.subTest(adjustment=adjustment):
                rows = self.observations()
                arms = rows["send"]["arms"]
                arms["10x"]["bytes"] = arms["1x"]["bytes"] + adjustment
                self.reject(rows)

    def test_each_arm_must_start_from_its_exact_restored_seed(self):
        for arm_name in ("1x", "10x"):
            with self.subTest(arm=arm_name):
                rows = self.observations()
                rows["send"]["arms"][arm_name]["clone_sha256"] = "f" * 64
                self.reject(rows)

    def test_missing_or_malformed_raw_and_seed_digest_witnesses_fail(self):
        for field in ("raw_sha256", "seed_sha256", "clone_sha256"):
            for value in (None, "", "g" * 64, "a" * 63):
                with self.subTest(field=field, value=value):
                    rows = self.observations()
                    arm = rows["send"]["arms"]["1x"]
                    if value is None:
                        del arm[field]
                    else:
                        arm[field] = value
                    self.reject(rows)

    def test_uncollected_or_unknown_child_cannot_qualify_complete_metrics(self):
        for value in (None, False, 1):
            with self.subTest(value=value):
                rows = self.observations()
                arm = rows["send"]["arms"]["10x"]
                if value is None:
                    del arm["child_collected"]
                else:
                    arm["child_collected"] = value
                self.reject(rows)

    def test_every_case_including_history_requires_all_probe_coverage(self):
        for case in _COMMANDS:
            for metric in ("cpu", "bytes", "locks"):
                with self.subTest(case=case, metric=metric):
                    rows = self.observations()
                    rows[case]["arms"]["10x"]["coverage"][metric] = False
                    self.reject(rows)

    def test_true_coverage_flags_cannot_replace_missing_metrics(self):
        for field in ("cpu_seconds", "bytes_read", "max_ledger_lock_seconds"):
            with self.subTest(field=field):
                rows = self.observations()
                del rows["send"]["arms"]["1x"]["metrics"][field]
                self.reject(rows)

    def test_unresolved_cpu_or_nonfinite_negative_metrics_are_unavailable(self):
        for field, values in (
            ("cpu_seconds", (0, -1, True, float("nan"), float("inf"))),
            ("bytes_read", (-1, True, 1.5, float("inf"))),
            ("max_ledger_lock_seconds", (-1, True, float("nan"), float("inf"))),
        ):
            for value in values:
                with self.subTest(field=field, value=value):
                    rows = self.observations()
                    rows["send"]["arms"]["1x"]["metrics"][field] = value
                    self.reject(rows)

    def test_observed_zero_bytes_and_no_holds_are_not_missing_coverage(self):
        rows = self.observations()
        for arm in rows["wait-idle"]["arms"].values():
            arm["metrics"]["bytes_read"] = 0
            arm["metrics"]["max_ledger_lock_seconds"] = 0
        _workloads.require_observations(self.scope, rows)


if __name__ == "__main__":
    unittest.main()

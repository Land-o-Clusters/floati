"""Issue 16: public support claims must agree with the measured entry point."""

from pathlib import Path
from contextlib import redirect_stdout
from datetime import datetime, timedelta
import io
import json
import unittest
from unittest import mock

from floati.adapters.opencode import OpenCodeAdapter
from floati.copy import README_OPENCODE_LIMIT, README_OPENCODE_MATRIX_NOTE
from scripts import measure_opencode_issue16 as measurement
from scripts import measure_opencode_issue16_worker as worker_measurement
from scripts.measure_opencode_issue16 import classify_receipt


ROOT = Path(__file__).resolve().parents[1]


class OpenCodeIssue16Tests(unittest.TestCase):
    def setUp(self):
        self.receipt = json.loads((ROOT / "tests/fixtures/opencode-issue16-2026-10-25.json").read_text())

    def test_readme_support_statement_agrees_with_shipped_adapter(self):
        """An existing headless adapter cannot be described as absent."""
        self.assertEqual(("run",), OpenCodeAdapter._default_profile().headless_arguments)
        self.assertFalse("OpenCode has no adapter yet" in (ROOT / "README.md").read_text(),
                         "README denies an adapter that ships")
        self.assertIn(README_OPENCODE_LIMIT, " ".join((ROOT / "README.md").read_text().split()))
        dataset = json.loads((ROOT / "docs/capability-matrix.v0.json").read_text())
        self.assertEqual(README_OPENCODE_MATRIX_NOTE, dataset["row_notes"]["opencode/cli"])

    def test_fixture_replays_every_recorded_classification_without_a_live_cli(self):
        self.assertEqual(self.receipt["classification"], classify_receipt(self.receipt))
        self.assertTrue(classify_receipt(self.receipt)["native_resume_after_lease_expiry"])

    def test_default_provider_failure_does_not_verify_the_shipped_surface(self):
        self.assertEqual("cannot-measure:default_provider_error",
                         classify_receipt(self.receipt)["default_entrypoint"])
        self.assertFalse(OpenCodeAdapter.surface_verified)

    def test_nonce_in_the_prompt_alone_cannot_prove_conversation_recovery(self):
        self.receipt["run_invocations"][-1]["stdout"] = ""
        self.assertEqual("cannot-measure:conversation_recovery_unproved",
                         classify_receipt(self.receipt)["restart"])

    def test_a_different_resume_session_cannot_prove_recovery(self):
        argv = self.receipt["run_invocations"][-1]["argv"]
        argv[argv.index("--session") + 1] = "ses_other"
        self.assertEqual("cannot-measure:conversation_recovery_unproved",
                         classify_receipt(self.receipt)["restart"])

    def test_terminating_a_different_session_cannot_prove_recovery(self):
        argv = self.receipt["run_invocations"][3]["argv"]
        argv[argv.index("--session") + 1] = "ses_other"
        self.assertEqual("cannot-measure:conversation_recovery_unproved",
                         classify_receipt(self.receipt)["restart"])

    def test_an_already_finished_client_cannot_prove_mid_session_interruption(self):
        self.receipt["run_invocations"][3]["running_before_interrupt"] = False
        self.assertEqual("cannot-measure:conversation_recovery_unproved",
                         classify_receipt(self.receipt)["restart"])

    def test_worker_failure_is_bound_to_the_leased_node_and_not_a_native_binding(self):
        details = self.receipt["dynamics"]
        rows = details["worker_receipts_after_restart"]
        self.assertEqual(["claim", "spawn", "drive", "degrade"], [row["transition"] for row in rows])
        self.assertEqual({"oc-temp"}, {row["node_id"] for row in rows})
        self.assertEqual({"opencode"}, {row["adapter"] for row in rows})
        self.assertEqual({details["work_item"]["id"]}, {row["work_item_id"] for row in rows})
        self.assertEqual("cannot-measure:worker_adapter_error",
                         classify_receipt(self.receipt)["governed_worker_lease"])
        self.assertTrue(classify_receipt(self.receipt)["worker_receipts_unchanged_by_native_resume"])
        # Replay the captured failure; current protocol behavior belongs to
        # test_headless_worker_protocol and may advance beyond this photograph.
        self.assertEqual([], details["worker_process_audit"]["alive_after_cleanup"])

    def test_live_intake_without_the_named_binary_emits_a_typed_skip(self):
        output = io.StringIO()
        with mock.patch("sys.argv", ["measure_opencode_issue16.py", "surface"]),\
             mock.patch.object(Path, "is_file", return_value=False), redirect_stdout(output):
            measurement.main()
        self.assertEqual({"status": "skipped", "reason": "opencode_binary_unavailable",
                          "binary": "/opt/homebrew/bin/opencode"}, json.loads(output.getvalue()))

    def test_quota_budget_and_scratch_cleanup_are_receipted(self):
        self.assertLessEqual(classify_receipt(self.receipt)["run_invocations_including_help"], 6)
        self.assertEqual({"owner"}, {row["home"] for row in self.receipt["run_invocations"]})
        self.assertTrue(self.receipt["dynamics"]["scratch_removed"])
        self.assertTrue(self.receipt["dynamics"]["work_workspace_removed"])


class OpenCodeWorkerRemeasurementTests(unittest.TestCase):
    def setUp(self):
        self.receipt = json.loads((ROOT / "tests/fixtures/opencode-issue16-worker-2026-10-25.json").read_text())

    def test_worker_measurement_replays_a_completed_round_trip_and_expired_admission(self):
        self.assertEqual("complete_with_bound_artifact",
                         worker_measurement.classify_worker_receipt(self.receipt)["round_trip"])
        self.assertEqual("expired_start_refused_before_spawn",
                         worker_measurement.classify_worker_receipt(self.receipt)["expired_start"])

    def test_completion_without_the_captured_artifact_cannot_prove_a_round_trip(self):
        self.receipt["run_invocations"][0]["artifacts"] = []
        self.assertEqual("cannot-measure:worker_round_trip_unproved",
                         worker_measurement.classify_worker_receipt(self.receipt)["round_trip"])

    def test_a_refusal_after_spawn_cannot_prove_expired_admission_was_fenced(self):
        self.receipt["expired_start"]["process_audit"]["adapter_pid"] = 123
        self.assertEqual("cannot-measure:expired_admission_unproved",
                         worker_measurement.classify_worker_receipt(self.receipt)["expired_start"])

    def test_admitted_worker_completed_after_the_same_lease_expired(self):
        self.assertEqual("admitted_worker_completed_after_lease_expiry",
                         worker_measurement.classify_worker_receipt(self.receipt)["expiry_boundary"])
        self.assertEqual("opencode/glm-5.3-flash-free", self.receipt["model"])
        self.assertLessEqual(len(self.receipt["run_invocations"]), 3)
        self.assertTrue(self.receipt["scratch_removed"])
        self.assertTrue(self.receipt["work_workspaces_removed"])

    def test_a_different_lease_at_completion_cannot_prove_the_boundary(self):
        self.receipt["run_invocations"][1]["lease_at_end"]["node_lease_id"] = "lease-other"
        self.assertEqual("cannot-measure:worker_expiry_boundary_unproved",
                         worker_measurement.classify_worker_receipt(self.receipt)["expiry_boundary"])

    def test_helper_start_before_expiry_cannot_substitute_for_admission_before_expiry(self):
        boundary = self.receipt["run_invocations"][1]
        expiry = datetime.fromisoformat(boundary["lease_at_start"]["expires_at"].replace("Z", "+00:00"))
        boundary["receipts"][0]["timestamp"] = (expiry + timedelta(seconds=1)).isoformat()
        self.assertEqual("cannot-measure:worker_expiry_boundary_unproved",
                         worker_measurement.classify_worker_receipt(self.receipt)["expiry_boundary"])

    def test_live_worker_without_the_named_binary_emits_a_typed_skip(self):
        output = io.StringIO()
        with mock.patch.object(worker_measurement, "RECEIPT", Path("absent-capture.json")),\
             mock.patch.object(Path, "is_file", return_value=False), redirect_stdout(output):
            worker_measurement.capture()
        self.assertEqual({"status": "skipped", "reason": "opencode_binary_unavailable",
                          "binary": "/opt/homebrew/bin/opencode"}, json.loads(output.getvalue()))

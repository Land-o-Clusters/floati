"""Conformance is a bound bus/work round trip, never a successful CLI alone.

The bank launches only its own fake through the landed ZcodeAdapter. The owner
live capture is an explicit separate command and is never reached by discovery.
"""

import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from tests.temp_roots import REAL_TEMP_ROOT


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/measure_zcode_conformance.py"
FIXTURE = ROOT / "tests/fixtures/zcode-conformance-fake-2026-10-26.json"
NATIVE = ["/opt/homebrew/bin/node",
          "/Applications/ZCode.app/Contents/Resources/glm/zcode.cjs"]


class ZcodeConformanceReceiptTests(unittest.TestCase):
    def instrument(self):
        self.assertTrue(SCRIPT.is_file(),
                        "missing owner instrument: bus/work conformance has no replayable measurement")
        spec = importlib.util.spec_from_file_location("measure_zcode_conformance", SCRIPT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def receipt(self):
        self.assertTrue(FIXTURE.is_file(), "a fake conformance capture must be committed for replay")
        return json.loads(FIXTURE.read_text())

    def classify(self, receipt):
        return self.instrument().classify_receipt(receipt)

    def test_committed_fixture_replays_without_any_process_or_provider(self):
        receipt = self.receipt()
        with mock.patch("subprocess.Popen", side_effect=AssertionError("replay launched a process")):
            result = self.classify(receipt)
        self.assertEqual(receipt["classification"], result)
        self.assertEqual("send_delivery_ack_verified", result["bus"])
        self.assertEqual("complete_with_bound_artifact", result["work"])
        self.assertFalse(result["live_conformance"], "a fake cannot close a live matrix cell")

    def test_relabeling_fake_capture_cannot_promote_it_to_live_conformance(self):
        receipt = self.receipt()
        receipt["mode"] = "owner-live"
        receipt["invocation"]["argv"][:2] = NATIVE
        # Keep every valid bus/work coordinate: the argv still contains the
        # Python fixture entry script and fixture-harness mode after its prefix.
        result = self.classify(receipt)
        self.assertEqual("send_delivery_ack_verified", result["bus"])
        self.assertEqual("complete_with_bound_artifact", result["work"])
        self.assertFalse(result["live_conformance"],
                         "a native-looking prefix cannot authenticate the launched argv")

    def test_nonzero_provider_failure_cannot_be_overruled_by_success_json(self):
        receipt = self.receipt()
        receipt["invocation"].update(exit_code=1, stderr="Provider returned error")
        result = self.classify(receipt)
        self.assertEqual("cannot-measure:provider_or_entrypoint_failed", result["surface"])
        self.assertFalse(result["live_conformance"])

    def test_empty_or_partial_native_response_is_incomplete_even_with_exit_zero(self):
        for stdout in ("", "{}", '{"response":"OK"}', '{"sessionId":"s","usage":{}}'):
            with self.subTest(stdout=stdout):
                receipt = self.receipt()
                receipt["invocation"]["stdout"] = stdout
                self.assertEqual("cannot-measure:native_result_incomplete", self.classify(receipt)["surface"])

    def test_send_is_not_delivery_or_acknowledgment(self):
        for mode, field in (("fake", "delivery"), ("fake", "ack"),
                            ("owner-live", "ack")):
            with self.subTest(mode=mode, missing=field):
                receipt = self.receipt()
                if mode == "owner-live":
                    receipt["mode"] = mode
                    receipt["invocation"]["argv"][:2] = NATIVE
                receipt["bus"][field] = None
                result = self.classify(receipt)
                self.assertEqual("cannot-measure:bus_round_trip_unproved", result["bus"])
                if mode == "owner-live":
                    self.assertFalse(result["live_conformance"])
                    self.assertEqual("cannot-measure", result["status"])

    def test_delivery_and_ack_must_bind_the_exact_sent_envelope(self):
        for field in ("delivery", "ack"):
            with self.subTest(changed=field):
                receipt = self.receipt()
                receipt["bus"][field]["item_ids"] = ["message-other"]
                self.assertEqual("cannot-measure:bus_round_trip_unproved", self.classify(receipt)["bus"])

    def test_bus_receipt_cannot_substitute_for_independently_read_ledger_records(self):
        receipt = self.receipt()
        receipt["bus"]["ledger_records"] = []
        self.assertEqual("cannot-measure:bus_round_trip_unproved", self.classify(receipt)["bus"])

    def test_cli_stdout_is_not_a_completed_governed_work_item(self):
        receipt = self.receipt()
        receipt["work"]["work_after"]["state"] = "claimed"
        self.assertEqual("cannot-measure:worker_round_trip_unproved", self.classify(receipt)["work"])

    def test_artifact_bytes_and_commit_binding_are_both_required(self):
        for mutation in ("missing", "content", "binding"):
            with self.subTest(mutation=mutation):
                receipt = self.receipt()
                if mutation == "missing":
                    receipt["work"]["artifacts"] = []
                elif mutation == "content":
                    receipt["work"]["artifacts"][0]["content"] = "wrong nonce"
                else:
                    receipt["work"]["artifacts"][0]["binding"]["sha"] = "b" * 40
                self.assertEqual("cannot-measure:worker_round_trip_unproved", self.classify(receipt)["work"])

    def test_worker_receipts_must_share_the_result_session_item_node_and_adapter(self):
        for field in ("session_id", "work_item_id", "node_id", "adapter"):
            with self.subTest(field=field):
                receipt = self.receipt()
                receipt["work"]["receipts"][-1][field] = "other"
                self.assertEqual("cannot-measure:worker_round_trip_unproved", self.classify(receipt)["work"])

    def test_worker_authority_and_every_success_outcome_are_consistent(self):
        mutations = (("authority_subject", "other-grant"), ("authority_epoch", 99),
                     ("outcome_code", "adapter_error"))
        for field, value in mutations:
            with self.subTest(field=field):
                receipt = self.receipt()
                receipt["work"]["receipts"][0][field] = value
                self.assertEqual("cannot-measure:worker_round_trip_unproved", self.classify(receipt)["work"])
        for field, value in (("holder", "other-node"), ("authority_subject", "other-grant"),
                             ("authority_epoch", 99)):
            with self.subTest(work_after=field):
                receipt = self.receipt()
                receipt["work"]["work_after"][field] = value
                self.assertEqual("cannot-measure:worker_round_trip_unproved", self.classify(receipt)["work"])

    def test_unconfirmed_child_or_scratch_cleanup_prevents_success(self):
        for field in ("scratch_removed", "workspaces_removed"):
            with self.subTest(field=field):
                receipt = self.receipt()
                receipt["cleanup"][field] = False
                self.assertEqual("cannot-measure", self.classify(receipt)["status"])
        receipt = self.receipt()
        receipt["work"]["process_audit"]["alive_after_cleanup"] = [123]
        self.assertEqual("cannot-measure", self.classify(receipt)["status"])

    def test_incomplete_and_malformed_captures_have_a_typed_result(self):
        for receipt in ({}, {"artifact_version": 0}, {"bus": None, "work": []}):
            with self.subTest(receipt=receipt):
                result = self.classify(receipt)
                self.assertEqual("cannot-measure", result["status"])
                self.assertFalse(result["live_conformance"])

    def test_native_pid_and_workspace_must_match_the_owned_worker_audit(self):
        for field, value in (("pid", 999999), ("workspace", "/other/workspace"),
                             ("home", "/other/home")):
            with self.subTest(field=field):
                receipt = self.receipt()
                receipt["invocation"][field] = value
                self.assertEqual("cannot-measure", self.classify(receipt)["status"])

    def fake(self, directory, *, fail=False):
        fake = directory / "fake-zcode.py"
        fake.write_text(
            "import json, os, pathlib, subprocess, sys\n"
            "prompt = sys.argv[sys.argv.index('--prompt') + 1]\n"
            "request = json.loads(prompt.split('FLOATI_CONFORMANCE_REQUEST=', 1)[1])\n"
            "if " + repr(fail) + ":\n"
            "    sys.stderr.write('Provider returned error\\n'); sys.exit(1)\n"
            "subprocess.run(request['bus_argv'], check=True, stdout=subprocess.DEVNULL)\n"
            "pathlib.Path('answer.txt').write_text(request['nonce'] + '\\n')\n"
            "print(json.dumps({'sessionId':'fake-session','traceId':'fake-trace',"
            "'turnId':'fake-turn','response':'done','usage':{'source':'provider',"
            "'inputTokens':1,'outputTokens':1},'observed_home':os.environ['HOME'],"
            "'secret_inherited':'FLOATI_TEST_SECRET' in os.environ}))\n"
        )
        return ["/usr/bin/python3", "-B", str(fake)]

    def test_fake_capture_exercises_landed_adapter_and_receipts_under_fresh_home(self):
        module = self.instrument()
        with tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT) as temporary:
            directory = Path(temporary)
            output = directory / "capture.json"
            original_home = os.environ.get("HOME")
            with mock.patch.dict(os.environ, {"FLOATI_TEST_SECRET": "never-inherit"}):
                receipt = module.capture(output, command=self.fake(directory), timeout=20)
            self.assertEqual(original_home, os.environ.get("HOME"))
            self.assertEqual(receipt, json.loads(output.read_text()))
            result = module.classify_receipt(receipt)
            self.assertEqual("ok", result["status"])
            self.assertFalse(result["live_conformance"])
            payload = json.loads(receipt["invocation"]["stdout"])
            self.assertNotEqual(original_home, payload["observed_home"])
            self.assertFalse(payload["secret_inherited"])
            self.assertFalse(Path(payload["observed_home"]).exists())
            self.assertEqual("scratch", receipt["home"])

    def test_fake_provider_failure_is_saved_and_cleanup_proved_without_success(self):
        module = self.instrument()
        with tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT) as temporary:
            directory = Path(temporary)
            output = directory / "failure.json"
            receipt = module.capture(output, command=self.fake(directory, fail=True), timeout=20)
            self.assertEqual(receipt, json.loads(output.read_text()))
            self.assertEqual("cannot-measure", module.classify_receipt(receipt)["status"])
            self.assertTrue(receipt["cleanup"]["scratch_removed"])
            self.assertTrue(receipt["cleanup"]["workspaces_removed"])

    def test_existing_output_refuses_before_any_model_capable_process(self):
        module = self.instrument()
        with tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT) as temporary:
            output = Path(temporary) / "existing.json"
            output.write_text('{"untouched":true}\n')
            with mock.patch("subprocess.Popen", side_effect=AssertionError("must refuse before launch")):
                with self.assertRaises(FileExistsError):
                    module.capture(output, timeout=20)
            self.assertEqual('{"untouched":true}\n', output.read_text())

    def test_deadline_is_bounded_and_owned_child_cleanup_is_retained(self):
        module = self.instrument()
        with tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT) as temporary:
            directory = Path(temporary)
            fake = directory / "slow-zcode.py"
            fake.write_text("import time\ntime.sleep(60)\n")
            receipt = module.capture(directory / "timeout.json",
                                     command=["/usr/bin/python3", "-B", str(fake)], timeout=0.5)
            self.assertEqual("cannot-measure", module.classify_receipt(receipt)["status"])
            self.assertEqual([], receipt["work"]["process_audit"]["alive_after_cleanup"])
            self.assertTrue(receipt["cleanup"]["scratch_removed"])
            self.assertTrue(receipt["cleanup"]["workspaces_removed"])

"""Human launch approval precedes real effect-owner preparation; no provider runs."""
import contextlib
from datetime import datetime, timezone
import io
import json
import shlex
import unittest

from floati.adhoc_journal import AdhocJournal, AdhocJournalController
from floati.approvals import ApprovalLedger
from floati.capabilities import CapabilityGrantLedger
from floati.effects import EffectController
from floati.jsonl import read_records
from floati.runtruth import RunLedger
from floati.workers import WorkerRunner
from tests.fixtures.adhoc_stage1 import AdhocFixture


class AdhocExecutionTests(AdhocFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.profile_record = self.profile()
        self.requested = self.work.add("fresh public dispatch", "temporary", [],
            adhoc_profile_id=self.profile_record["id"], now=self.now)
        self.dispatch_record = self.dispatch(self.profile_record, work_item_id=self.requested["id"])
        self.envelope_record = self.envelope(self.dispatch_record)

    def invoke(self, *arguments):
        from floati.cli import main
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = main(["worker", "adhoc", *arguments])
        return code, json.loads(output.getvalue())

    def profile_arguments(self, grant=False):
        arguments = ["profile", "--root", str(self.root.path), "--as", "temporary",
            "--adapter", "codex", "--lane-opening", self.opened["record"]["id"],
            "--repository", "fixture", "--to", "coordinator", "--idempotency-key",
            self.profile_record["idempotency_key"], "--authority-subject",
            self.dispatch_record["authority_subject"], "--authority-epoch",
            str(self.dispatch_record["authority_epoch"])]
        return arguments + (["--grant-launch"] if grant else [])

    def test_public_run_without_grant_refuses_exact_remedy_and_writes_nothing(self):
        before = self.tree_bytes()
        code, artifact = self.invoke("run", "--root", str(self.root.path), "--as", "temporary",
            "--session", "public-parent", "--profile", self.profile_record["id"],
            "--envelope", self.envelope_record["id"], "--idempotency-key", "ungranted-run")
        self.assertEqual(20, code, artifact)
        self.assertIn(artifact["evidence"]["code"], {"policy_missing", "effect_approval_missing",
            "capability_selector_unsatisfied"})
        remedy_text = artifact["evidence"].get("remedy")
        self.assertIsInstance(remedy_text, str, "missing exact human grant command")
        remedy = shlex.split(remedy_text)
        self.assertEqual(["floati", "worker", "adhoc", *self.profile_arguments(True)], remedy)
        self.assertEqual(before, self.tree_bytes())
        self.assert_no_provider_effects()

    def test_profile_without_human_flag_previews_but_grants_nothing(self):
        before = self.tree_bytes()
        code, artifact = self.invoke(*self.profile_arguments())
        self.assertEqual(0, code, artifact)
        preview = artifact["evidence"]["launch_preview"]
        self.assertEqual(self.profile_record["adapter"], preview["harness"])
        self.assertEqual(self.profile_record["workspace"], preview["workspace"])
        self.assertEqual(self.profile_record["node_lease_id"], preview["node_lease_id"])
        self.assertEqual("native_launch", preview["capability"])
        self.assertRegex(preview["exact_action_digest"], r"^[0-9a-f]{64}$")
        approvals = ApprovalLedger(self.root)
        self.assertEqual([], read_records(self.root, approvals.request_path, allowed_kinds={"approval_request"}))
        self.assertEqual([], read_records(self.root, approvals.decision_path, allowed_kinds={"approval_decision"}))
        self.assertEqual([], CapabilityGrantLedger(self.root).records())
        self.assertFalse((self.root.path / "FLOATI.toml").exists())
        self.assertEqual(before, self.tree_bytes())

    def test_human_launch_grant_cannot_bind_an_unrelated_run(self):
        from copy import deepcopy
        from floati.adhoc_execution import _prepare_binding
        from floati.adhoc_launch import require_adhoc_launch
        from floati.errors import ProtocolRefusal
        from floati.ids import uuid7_hex
        self.assertEqual(0, self.invoke(*self.profile_arguments(True))[0])
        self.now = datetime.now(timezone.utc)
        admitted = AdhocJournalController(AdhocJournal(self.root)).admit(
            self.profile_record["id"], self.envelope_record["id"], self.dispatch_record,
            actor="temporary", acting_session_id="public-parent", idempotency_key="fresh-run", now=self.now)
        launch = require_adhoc_launch(self.root, self.profile_record, admitted, now=self.now)
        unrelated = deepcopy(admitted)
        unrelated["dispatch_id"] = "adhoc-dispatch-" + uuid7_hex()
        unrelated["dispatch_digest"] = "0" * 64
        with self.assertRaises(ProtocolRefusal) as refused:
            _prepare_binding(self.root, self.profile_record, unrelated,
                launch["policy"], launch["launch_grant"], self.now)
        self.assertIn(refused.exception.code, {"capability_selector_unsatisfied", "effect_launch_binding_invalid"})
        run_id = "run-" + unrelated["dispatch_id"].removeprefix("adhoc-dispatch-")
        records = [r for r in RunLedger(self.root).records() if r["run_id"] == run_id]
        self.assertFalse(any(r["kind"] in {"capability_set_bound", "dispatch_decision", "attempt_started"}
            for r in records), "unrelated admission must never acquire the human launch capability")
        self.assert_no_provider_effects()

    def test_human_flagged_profile_grant_prepares_fresh_real_effect_owner(self):
        from floati.adhoc_execution import prepare_adhoc_execution
        self.assertEqual([], RunLedger(self.root).records())
        code, artifact = self.invoke(*self.profile_arguments(True))
        self.assertEqual(0, code, artifact)
        granted = self.tree_bytes()
        self.assertEqual(0, self.invoke(*self.profile_arguments(True))[0])
        self.assertEqual(granted, self.tree_bytes(), "replay must reuse the human grant")
        self.now = datetime.now(timezone.utc)
        admitted = AdhocJournalController(AdhocJournal(self.root)).admit(
            self.profile_record["id"], self.envelope_record["id"], self.dispatch_record,
            actor="temporary", acting_session_id="public-parent", idempotency_key="fresh-run", now=self.now)
        runner, binding = prepare_adhoc_execution(self.root, self.profile_record, admitted, lambda: self.now)
        self.assertIs(type(runner), WorkerRunner)
        self.assertIs(type(runner.effect_controller), EffectController)
        run = runner.effect_controller.run_ledger.project().run(binding["run_id"])
        attempt = run["attempts"][binding["attempt_id"]]
        self.assertEqual(self.requested["id"], binding["item_id"])
        self.assertEqual("bound", run["admission_binding"]["status"])
        self.assertEqual("temporary", run["dispatches"][binding["attempt_id"]]["chosen_worker"])
        self.assertEqual(self.profile_record["adapter"], run["dispatches"][binding["attempt_id"]]["adapter"])
        self.assertIsNotNone(attempt["started"])
        proof = runner.effect_controller.run_ledger.project().effect_intent_context(
            binding["run_id"], binding["item_id"], binding["attempt_id"], attempt["opened"]["fence_token"])
        self.assertEqual(runner.effect_controller.policy.digest, proof["policy_digest"])
        grants = CapabilityGrantLedger(self.root).records()
        self.assertTrue(grants, "the real capability ledger must hold the human grant")
        approvals = ApprovalLedger(self.root)
        requests = {r["id"]: r for r in read_records(self.root, approvals.request_path, allowed_kinds={"approval_request"})}
        decisions = {r["id"]: r for r in read_records(self.root, approvals.decision_path, allowed_kinds={"approval_decision"})}
        for grant in grants:
            request, decision = requests[grant["approval_request_id"]], decisions[grant["approval_decision_id"]]
            self.assertEqual("approved", decision["decision"])
            self.assertEqual(request["id"], decision["request_id"])
            self.assertEqual(artifact["evidence"]["launch_preview"]["exact_action_digest"], request["exact_action_digest"])
        prepared = self.tree_bytes()
        self.assertEqual(binding, prepare_adhoc_execution(self.root, self.profile_record, admitted, lambda: self.now)[1])
        self.assertEqual(prepared, self.tree_bytes(), "replay must reuse the real attempt")
        self.assert_no_provider_effects()

"""Controller transactions against real scratch ledgers and a fake native child.

These controls do not qualify provider confinement: the shared worker tests own
that property. The injected execution owner runs an independent Python child;
all presentation, native identity, work, ACK, consumption and send are real.
"""
from copy import deepcopy
from datetime import timedelta
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest import mock

from floati.adhoc_journal import AdhocJournal
from floati.adhoc_threads import AdhocThreadController
from floati.cursor import SparseCursor
from floati.errors import ProtocolRefusal
from floati.ids import uuid7_hex
from floati.message_consumption import MessageConsumption
from floati.workers import WorkerReceipts, WorkerRunner
import tests.test_native_thread_protocol as native_fixtures


class _FixtureExecution:
    """Independent fake executable owner; never accepted as a public factory."""
    def __init__(self, case):
        self.case = case
        self.effect_controller = case.effects
        self.launches = 0
        self.mode = "normal"
        self.summary = "fixture result"
        self.acted = None
        self.after_child = None
        self.last_native_events = []
        self.last_native_transport = None
        self.last_process_audit = {"adapter_pid": None,
            "registered_process_groups": [], "alive_after_cleanup": [],
            "isolation_backend": "fixture-process"}

    def run_exact(self, node, adapter, work, *, native_thread_context,
                  on_native_event, startup_prompt="", now=None):
        case = self.case
        self.launches += 1
        self.prompt = json.loads(startup_prompt)
        # The real worker path claims before native allocation. Its worker
        # receipt identity stays distinct from the authenticated provider ID.
        started_at = case.now if now is None else now
        case.work.claim(work, node, "borrowed-work", 1, now=started_at)
        receipts = WorkerReceipts(case.root)
        session = "worker-" + uuid7_hex()
        receipts.append(session, work, node, adapter, "claim", None, [], now=started_at)
        receipts.append(session, work, node, adapter, "spawn", None, [], now=started_at)
        # The provider identity is supplied through the actual parent protocol
        # before the independent child sees the frozen prompt.
        on_native_event(case.event("opened"))
        self.last_native_events.append(case.event("opened"))
        messages = self.prompt["messages"]
        selected = {row["id"] for row in messages} if self.acted is None else set(self.acted)
        result = {"schema_version": 1, "dispatch_digest": native_thread_context["dispatch_digest"],
            "provider_coordinate": deepcopy(case.coordinate), "summary": self.summary,
            "inbox_dispositions": [{"message_id": row["id"],
                "disposition": "acted" if row["id"] in selected else "unacted",
                "reason": "fixture explicitly records its action", "result_paths": []}
                for row in messages]}
        if self.mode == "foreign-disposition":
            result["inbox_dispositions"].append({"message_id": "msg-019a17f0000070008000000000000009",
                "disposition": "acted", "reason": "foreign", "result_paths": []})
        payload = {"path": case.dispatch_record["result_document"], "result": result,
                   "fail": self.mode == "failed", "prepare_parent": self.mode != "parent-required"}
        script = ("import json,pathlib,sys\n"
                  "v=json.load(sys.stdin)\n"
                  "if v['fail']: raise SystemExit(7)\n"
                  "p=pathlib.Path(v['path'])\n"
                  "if v['prepare_parent']: p.parent.mkdir(parents=True,exist_ok=True)\n"
                  "p.write_text(json.dumps(v['result'],sort_keys=True)+'\\n')\n")
        child = subprocess.Popen([str(Path(sys.executable).resolve()), "-B", "-c", script],
            cwd=case.profile_record["workspace"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, start_new_session=True,
            env={**os.environ, "HOME": str(case.base / "private-home")})
        stdout, stderr = child.communicate(json.dumps(payload), timeout=5)
        self.last_native_transport = {"stdout_bytes": len(stdout.encode()), "stderr_bytes": len(stderr.encode()),
            "combined_bytes": len(stdout.encode()) + len(stderr.encode()), "protocol_records": 0,
            "stdout_sha256": hashlib.sha256(stdout.encode()).hexdigest(),
            "stderr_sha256": hashlib.sha256(stderr.encode()).hexdigest(), "exit_code": child.returncode}
        self.last_process_audit.update(adapter_pid=child.pid, registered_process_groups=[child.pid])
        if self.after_child is not None:
            self.after_child()
        if child.returncode:
            return receipts.append(session, work, node, adapter, "degrade", "process_died", [], now=case.now)
        on_native_event(case.event("terminal"))
        self.last_native_events.append(case.event("terminal"))
        receipts.append(session, work, node, adapter, "drive", None, [], now=case.now)
        return receipts.append(session, work, node, adapter, "bind_artifact", None, [], now=case.now)


class ControllerFixture:
    def setUp(self):
        self.case = native_fixtures.SynchronousNativeThreadProtocolTests("test_opened_record_preserves_admission_and_exclusive_owner")
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        c = self.case
        self.bank_remote = c.base / "controller-bank.git"
        c.git(c.base, "init", "--bare", "--quiet", str(self.bank_remote))
        c.git(c.repository, "remote", "add", "origin", str(self.bank_remote))
        # Simulate the explicit human profile command in the scratch fleet.
        from floati.adhoc_launch import grant_adhoc_launch
        grant_adhoc_launch(c.root, c.profile_record, authority_subject="borrowed-work", authority_epoch=1)
        from datetime import datetime, timezone
        c.now = datetime.now(timezone.utc)
        self.execution = _FixtureExecution(self.case)
        self.controller = AdhocThreadController(self.case.root, clock=lambda: self.case.now)
        observations = self.case.base / "empty-launch-agents"
        observations.mkdir()
        lsof = self.case.base / "fixture-lsof"
        lsof.write_text("#!/bin/sh\nexit 1\n")
        lsof.chmod(0o700)
        self.controller.profiles.lanes.launch_agents = observations
        self.controller.profiles.lanes.lsof_executable = lsof
        # The controller's proof owner is the actual production worker type.
        # Only its execution method is replaced by this independent fixture;
        # no fixture flag or alternate owner is accepted by product code.
        self.proof_owner = WorkerRunner(self.case.root, {}, effect_controller=self.case.effects,
            clock=lambda: self.case.now)
        def execute(*args, **kwargs):
            try:
                return self.execution.run_exact(*args, **kwargs)
            finally:
                self.proof_owner.last_process_audit = deepcopy(self.execution.last_process_audit)
                self.proof_owner.last_native_events = deepcopy(self.execution.last_native_events)
                self.proof_owner.last_native_transport = deepcopy(self.execution.last_native_transport)
        self.proof_owner.run_exact = execute
        patch = mock.patch.object(self.controller, "_prepare_execution",
            return_value=(self.proof_owner, deepcopy(self.case.context["effect_binding"])))
        patch.start()
        self.addCleanup(patch.stop)

    def run_dispatch(self):
        c = self.case
        return self.controller.run(c.profile_record["id"], c.envelope_record["id"],
            actor="node-a", acting_session_id="fixture-parent", idempotency_key="node-a-dispatch")

    def capture_run(self):
        try:
            return None, self.run_dispatch()
        except ProtocolRefusal as exc:
            return exc.code, None

    def bank_result(self):
        c = self.case
        sha = c.git(Path(c.profile_record["workspace"]), "rev-parse", "HEAD")
        c.git(c.repository, "update-ref", "refs/remotes/origin/result", sha)
        c.git(c.repository, "push", "--quiet", "origin", sha + ":refs/heads/" + c.profile_record["branch"])

    def acked(self):
        return SparseCursor(self.case.root).acked_ids("node-a")

    def consumed(self):
        return MessageConsumption(self.case.root).records("node-a")

    def saved(self, phase, *, stage=None):
        rows = [r for r in AdhocJournal(self.case.root).records() if r.get("phase") == phase]
        return rows if stage is None else [r for r in rows if r["evidence"].get("stage") == stage]


class AdhocThreadControllerTests(ControllerFixture, unittest.TestCase):
    def test_startup_freezes_only_node_mail_without_ack(self):
        c = self.case
        worker_mail = c.events.send("coordinator", "node-a", "fixture", c.base_sha,
            "tracked.txt", "session addressed", worker_session_id="other-session", now=c.now)
        self.execution.mode = "failed"
        self.capture_run()
        frozen = self.saved("inbox_presented")
        self.assertTrue(frozen, "admission alone must not stand for startup presentation")
        ids = frozen[0]["evidence"]["message_ids"]
        self.assertIn(c.envelope_record["id"], ids)
        self.assertNotIn(worker_mail["id"], ids)
        self.assertEqual(set(), set(self.acked()))
        self.assertEqual("claimed", c.work.show(c.requested["id"])[0]["state"])
        self.assertTrue(self.saved("failed"), "a failed native process needs retained terminal disposition")

    def test_unbanked_result_replay_never_relaunches_provider(self):
        c = self.case
        parent = Path(c.profile_record["workspace"]) / Path(c.dispatch_record["result_document"]).parent
        self.assertFalse(parent.exists())
        self.execution.mode = "parent-required"
        self.assertEqual({"status": "unsupported", "reason": "native_usage_not_reported"},
            self.controller.show(c.admitted["dispatch_id"])["usage"])
        usage = {"status": "measured", "scope": "thread_cumulative",
            "source": "thread/tokenUsage/updated.tokenUsage.total",
            "tokens": {"input_tokens": 140, "output_tokens": 20}}
        original = c.event
        def event(phase):
            value = original(phase)
            if phase == "terminal":
                value["usage"] = deepcopy(usage)
            return value
        c.event = event
        self.execution.summary = "x" * 1100
        # The measured baseline launches over 2,000 Git children here. Keep a
        # process budget for success, remote banking and exact replay together;
        # batching directory testimony leaves room below 1,400 without timing.
        with mock.patch("subprocess.Popen", wraps=subprocess.Popen) as launches:
            code, _ = self.capture_run()
            self.assertEqual("adhoc_result_unbanked", code)
            self.assertTrue((parent / "result.json").is_file())
            self.assertEqual(set(), set(self.acked()))

        saved = self.saved("provider_terminal")
        self.assertEqual(1, len(saved))
        self.assertEqual(usage, saved[0]["evidence"]["event"].get("usage"))
        workspace = Path(c.profile_record["workspace"])
        index = Path(c.profile_record["git_admin_dir"]) / "index"
        other_index = Path(c.git(Path(c.foreign_opened["workspace"]), "rev-parse", "--git-path", "index"))
        sha = c.git(workspace, "rev-parse", "HEAD")
        before, other = index.read_bytes(), other_index.read_bytes()
        def files():
            return {str(p.relative_to(workspace)): p.read_bytes()
                for p in workspace.rglob("*") if p.is_file() and not p.is_symlink()}
        working_files = files()
        c.git(c.repository, "update-ref", "refs/remotes/origin/result", sha)
        code, result = self.capture_run()
        self.assertEqual("adhoc_result_unbanked", code,
            "a planted local tracking ref must never authorize settlement")
        self.assertIsNone(result)
        self.assertEqual(before, index.read_bytes())
        self.assertEqual(working_files, files())
        self.assertEqual([], self.saved("process_closed", stage="index_settled"))

        # Keep the original three-run subprocess budget; the additional
        # planted-ref refusal above is a separate scenario merged into this ID.
        with mock.patch("subprocess.Popen", launches):
            self.bank_result()
            c.git(c.repository, "push", "--quiet", "origin", "HEAD:refs/heads/main")
            code, first = self.capture_run()
            self.assertIsNone(code)
            self.assertEqual("result_published", first["phase"])
            self.assertEqual(usage, first["usage"])
            self.assertEqual("", c.git(workspace, "diff", "--cached", "--name-only", "HEAD"),
                "banked row index must match its exact published HEAD")
            self.assertEqual(working_files, files(), "settlement must never rewrite working files")
            self.assertEqual(other, other_index.read_bytes())
            receipt = self.saved("process_closed", stage="index_settled")[0]["evidence"]
            self.assertEqual(sha, receipt["head_sha"])
            self.assertEqual(sha, receipt["remote_sha"])
            self.assertEqual("refs/heads/" + c.profile_record["branch"], receipt["remote_ref"])
            self.assertEqual(hashlib.sha256(before).hexdigest(), receipt["index_sha256_before"])
            self.assertEqual(hashlib.sha256(index.read_bytes()).hexdigest(), receipt["index_sha256_after"])
            settled = index.read_bytes()
            code, replay = self.capture_run()
            self.assertIsNone(code)
            self.assertEqual(settled, index.read_bytes())
            self.assertEqual(1, self.execution.launches)
            self.assertEqual(first, replay)
            self.assertEqual(usage, replay["usage"])
            published = self.saved("result_published")
            self.assertEqual(1, len(published))
            envelope = next(r for r in self.case.events.event_records()
                if r["id"] == published[0]["evidence"]["envelope_id"])
            self.assertEqual("message_envelope", envelope["kind"])
            self.assertEqual(self.case.envelope_record["id"], envelope["reply_to"])
        git_launches = sum("/usr/bin/git" in call.args[0] for call in launches.call_args_list)
        self.assertLessEqual(git_launches, 1400,
            "success, banking and replay must batch repeated Git directory testimony")

        # A fresh reader uses durable terminal testimony, never private history.
        # Keep this read and its replay outside the original Git-process budget.
        slot = c.root.tenant_home / c.profile_record["provider_state"]
        history = slot / "private-history.jsonl"
        history.write_text("PRIVATE-TRANSCRIPT-CANARY\n")
        real_open = Path.open
        def guarded_open(path, *args, **kwargs):
            if path == history:
                raise AssertionError("usage must never read provider history")
            return real_open(path, *args, **kwargs)
        reader = AdhocThreadController(c.root, clock=lambda: c.now)
        with mock.patch.object(Path, "open", guarded_open):
            observed = reader.show(c.admitted["dispatch_id"])
        self.assertEqual(usage, observed["usage"])
        self.assertNotIn("PRIVATE-TRANSCRIPT-CANARY", repr(observed))
        code, replay = self.capture_run()
        self.assertIsNone(code)
        self.assertEqual(usage, replay["usage"])
        self.assertEqual(1, self.execution.launches)

        # Reuse the completed fleet: a fresh conversation must reach the real
        # reservation boundary without inheriting the old provider coordinate.
        prior_history = self.controller._history(c.admitted["dispatch_id"])
        requested = c.work.add("fresh native conversation", "node-a", [],
            adhoc_profile_id=c.profile_record["id"], now=c.now)
        dispatch = dict(c.dispatch_record, work_item_id=requested["id"],
            operation="start", result_document="docs/evidence/fresh-result.json")
        envelope = c.envelope(dispatch, name="fresh-start", recipient="node-a")
        fresh = AdhocThreadController(c.root, clock=lambda: c.now)
        fresh.profiles.lanes.launch_agents = self.controller.profiles.lanes.launch_agents
        fresh.profiles.lanes.lsof_executable = self.controller.profiles.lanes.lsof_executable
        prepare = fresh._prepare_execution
        entries = []

        class ProviderBoundaryReached(Exception):
            pass

        def prepare_without_another_child(*args):
            runner, effect = prepare(*args)

            def at_provider(*args, native_thread_context, **kwargs):
                self.assertEqual("start", native_thread_context["operation"])
                self.assertIsNone(native_thread_context["provider_coordinate"])
                self.assertEqual(requested["id"], native_thread_context["work_item_id"])
                entries.append(native_thread_context)
                raise ProviderBoundaryReached()

            runner.run_exact = at_provider
            return runner, effect

        with mock.patch.object(fresh, "_prepare_execution", side_effect=prepare_without_another_child):
            with self.assertRaises(ProviderBoundaryReached):
                fresh.run(c.profile_record["id"], envelope["id"], actor="node-a",
                    acting_session_id="fixture-parent", idempotency_key="fresh-start")
        self.assertEqual(1, len(entries))
        reservation = fresh._phase(fresh._history(entries[0]["dispatch_id"]), "spawn_reserved")
        self.assertEqual(entries[0], reservation["evidence"]["native_context"])
        self.assertEqual(prior_history, fresh._history(c.admitted["dispatch_id"]))
        self.assertEqual(1, self.execution.launches)

    def test_cleanup_crash_recovery_registers_exact_native_thread_without_relaunch(self):
        from floati.adhoc_threads import AdhocThreadController
        from floati.thread_observations import ThreadObserver, ThreadObservationLedger
        with mock.patch.object(ThreadObserver, "register_adhoc", side_effect=RuntimeError("after cleanup")):
            with self.assertRaisesRegex(RuntimeError, "after cleanup"):
                self.run_dispatch()
        self.assertTrue(self.saved("process_closed", stage="cleanup"))
        self.assertEqual([], ThreadObservationLedger(self.case.root).records())
        previous = self.controller
        self.controller = AdhocThreadController(self.case.root, clock=lambda: self.case.now)
        self.controller.profiles.lanes.launch_agents = previous.profiles.lanes.launch_agents
        self.controller.profiles.lanes.lsof_executable = previous.profiles.lanes.lsof_executable
        with mock.patch.object(self.controller, "_prepare_execution",
                side_effect=AssertionError("durable cleanup must never relaunch")):
            self.assertEqual("adhoc_result_unbanked", self.capture_run()[0])
        attachments = [r for r in ThreadObservationLedger(self.case.root).records()
            if r["kind"] == "thread_attachment_registered"]
        self.assertEqual(1, len(attachments), "cleanup recovery must register the saved native identity")
        attachment = attachments[0]
        self.assertEqual(2, attachment["schema_version"])
        self.assertEqual(self.case.admitted["dispatch_id"], attachment["adhoc_dispatch_id"])
        testimony = {r["id"]: r for r in AdhocJournal(self.case.root).records()}
        self.assertIn(attachment["native_receipt_id"], testimony)
        self.assertEqual(1, self.execution.launches)
        saved = ThreadObservationLedger(self.case.root).records()
        self.assertEqual("adhoc_result_unbanked", self.capture_run()[0])
        self.assertEqual(saved, ThreadObservationLedger(self.case.root).records())

    def test_ack_crash_recovery_consumes_only_saved_acted_subset(self):
        c = self.case
        unacted = c.events.send("coordinator", "node-a", "fixture", c.base_sha,
            "tracked.txt", "explicitly unacted", now=c.now)
        self.execution.acted = [c.envelope_record["id"]]
        self.assertEqual("adhoc_result_unbanked", self.capture_run()[0])
        self.bank_result()
        real = MessageConsumption.consume_many
        with mock.patch.object(MessageConsumption, "consume_many", side_effect=RuntimeError("after ACK")):
            with self.assertRaisesRegex(RuntimeError, "after ACK"):
                self.run_dispatch()
        late = c.events.send("coordinator", "node-a", "fixture", c.base_sha,
            "tracked.txt", "arrived after frozen result", now=c.now)
        self.assertIsNone(self.capture_run()[0])
        self.assertEqual({c.envelope_record["id"]}, set(self.acked()))
        self.assertEqual([c.envelope_record["id"]], [r["message_id"] for r in self.consumed()])
        self.assertIn(unacted["id"], c.events.unacked_ids("node-a"))
        self.assertIn(late["id"], c.events.unacked_ids("node-a"))
        self.assertEqual(1, self.execution.launches)

    def test_foreign_result_disposition_refuses_without_ack(self):
        self.execution.mode = "foreign-disposition"
        code, _ = self.capture_run()
        self.assertEqual("adhoc_result_disposition_invalid", code)
        self.assertEqual(set(), set(self.acked()))
        self.assertNotEqual("completed", self.case.work.show(self.case.requested["id"])[0]["state"])

    def test_lease_expiry_during_drive_reaps_without_completion(self):
        self.execution.after_child = lambda: setattr(self.case, "now", self.case.now + timedelta(hours=2))
        code, _ = self.capture_run()
        self.assertEqual("adhoc_lease_ended", code)
        self.assertNotEqual("completed", self.case.work.show(self.case.requested["id"])[0]["state"])
        self.assertEqual(set(), set(self.acked()))
        shown = self.controller.show(self.case.admitted["dispatch_id"])
        self.assertEqual("adhoc_lease_ended", shown["reason_code"])
        self.assertEqual([], shown["cleanup"]["alive_after_cleanup"])

    def test_spawn_reservation_crash_never_relaunches(self):
        with mock.patch.object(self.execution, "run_exact", side_effect=RuntimeError("spawn uncertainty")):
            with self.assertRaisesRegex(RuntimeError, "spawn uncertainty"):
                self.run_dispatch()
        code, _ = self.capture_run()
        self.assertEqual("adhoc_spawn_unknown", code)
        self.assertEqual(0, self.execution.launches)

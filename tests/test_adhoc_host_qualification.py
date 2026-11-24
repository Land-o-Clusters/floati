"""Native-host qualification through the real bootstrap, using fake providers."""
from copy import deepcopy
import errno
import json
import os
from pathlib import Path
import stat
import sys
import unittest

from floati.adhoc_execution import prepare_adhoc_execution
from floati.adhoc_journal import AdhocJournal, AdhocJournalController
from floati.adhoc_launch import grant_adhoc_launch
from floati.adhoc_records import CLAUDE_PERMISSION, CODEX_PERMISSION
from floati.errors import ProtocolRefusal
from floati.native_thread_protocol import NativeThreadProtocol
from floati.worker_bootstrap_protocol import BuiltInAdapterSpec, NATIVE_THREAD_BOUNDS
from floati.workers import WorkerRunner
from tests.fixtures.adhoc_stage1 import AdhocFixture


class AdhocHostQualificationTests(AdhocFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()

    def qualify(self, adapter, *, usage=False):
        # The provider's work edits paths its row already tracks (the fakes
        # write PROOF.txt); on Linux the row root's own entry set is fixed.
        self.commit_row_entries(self.workspace, {"PROOF.txt": "row placeholder\n",
                                                 "notes/keep.txt": "row note\n"})
        # A successful native-host probe requires a safe owned borrowed
        # tree independently of the process umask, as the runtime fixture does.
        for path in [self.workspace, *self.workspace.rglob("*")]:
            self.assertFalse(path.is_symlink())
            path.chmod(stat.S_IMODE(path.stat().st_mode) & ~0o022)
        profile = self.profiles().create("temporary", adapter,
            self.opened["record"]["id"], "fixture", "coordinator",
            idempotency_key="host-profile", now=self.now)
        older = self.work.add("older owned work remains open", "temporary", [], now=self.now)
        requested = self.work.add("native host fixture proof", "temporary", [],
            adhoc_profile_id=profile["id"], now=self.now)
        permission = {"codex": CODEX_PERMISSION, "claude": CLAUDE_PERMISSION,
                      "pi": {"mode": "allowlisted_tools", "tools": ["read", "write", "edit"]}}[adapter]
        dispatch = self.dispatch(profile, work_item_id=requested["id"],
                                 permission=deepcopy(permission))
        envelope = self.envelope(dispatch)
        journal = AdhocJournal(self.root)
        admission = self.admit(journal, profile, envelope, dispatch)
        grant_adhoc_launch(self.root, profile, authority_subject="adhoc-work",
                          authority_epoch=1, now=self.now)
        prepared, effect_binding = prepare_adhoc_execution(
            self.root, profile, admission, lambda: self.now)
        slot = self.root.tenant_home / profile["provider_state"]
        if adapter == "claude":
            credential = slot / "claude-oauth-token"
            credential.touch(mode=0o600)
            credential.write_text("fixture-private-slot-oauth-token\n")
        binding = {"profile_id": profile["id"], **{key: profile[key] for key in
            ("lane_opening_id", "node_lease_id", "workspace")}}
        context = {"schema_version": 1, "profile_id": profile["id"],
            "profile_digest": profile["digest"], "dispatch_id": admission["dispatch_id"],
            "dispatch_digest": admission["dispatch_digest"], "node_id": "temporary",
            "node_lease_id": profile["node_lease_id"], "work_item_id": requested["id"],
            "operation": "start", "provider_coordinate": None, "workspace_binding": binding,
            "provider_state_binding": {"path": str(slot),
                **profile["filesystem_bindings"]["provider_state"]},
            "permission": deepcopy(permission), "bounds": dict(NATIVE_THREAD_BOUNDS),
            "effect_binding": effect_binding}
        # All provider witnesses are in the exact leased private slot. No live
        # credential-bearing HOME or independent writable fixture store is used.
        (slot / "opened-ack").touch(mode=0o600)
        (slot / "opened-ack").write_text("fixture startup marker")
        fixtures = Path(__file__).resolve().parent / "fixtures/adhoc-native"
        interpreter = str(Path(sys.executable).resolve())
        forbidden = {
            "sibling": Path(self.foreign_opened["workspace"]) / "tracked.txt",
            "bus": self.root.tenant_home / "state/host-forbidden.txt",
            "outer_home": Path(os.environ["HOME"]) / "host-forbidden.txt",
            "provider_parent": slot.parent / "host-forbidden.txt",
            "git_pointer": self.workspace / ".git",
            "common_config": self.repository / ".git/config",
            "common_hook": self.repository / ".git/hooks/host-forbidden",
            "common_ref": self.repository / ".git/refs/heads/main",
        }
        for path in forbidden.values():
            if not path.exists():
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"retained foreign bytes\n")
        before = {name: path.read_bytes() for name, path in forbidden.items()}
        allowed = {"workspace": self.workspace / "notes/HOST-ALLOWED.txt",
                   "provider": slot / "host-allowed.txt"}
        plan_path = slot / "host-probe-plan.json"
        plan_path.touch(mode=0o600)
        plan_path.write_text(json.dumps({"forbidden": {name: str(path) for name, path in forbidden.items()},
            "allowed": {name: str(path) for name, path in allowed.items()},
            "report": str(slot / "host-write-report.json")}))
        commands = {
            "codex": (interpreter, "-B", str(fixtures / "codex_harness.py"), str(slot), "usage" if usage else "normal"),
            "claude": (interpreter, "-B", str(fixtures / "claude_provider.py"),
                       "--fixture-mode", "usage" if usage else "complete"),
            "pi": (interpreter, "-B", str(fixtures / "pi_provider.py"),
                   "--fixture-mode", "usage" if usage else "complete", "--fixture-store", str(slot)),
        }
        command = commands[adapter]
        wrapped = (*command[:2], str(fixtures / "native_host_provider.py"),
                   command[2], str(plan_path), *command[3:])
        runner = WorkerRunner(self.root, {}, call_timeout=0.1,
            effect_controller=prepared.effect_controller,
            effect_adapter_specs={adapter: BuiltInAdapterSpec(adapter, wrapped, "borrowed")})
        protocol = NativeThreadProtocol(context, adapter_kind=adapter,
            journal_controller=AdhocJournalController(journal),
            effect_controller=prepared.effect_controller, now=self.now)
        head_before = self.git(self.workspace, "rev-parse", "HEAD")
        if sys.platform != "darwin" and not sys.platform.startswith("linux"):
            with self.assertRaises(ProtocolRefusal) as caught:
                runner.run_exact("temporary", adapter, requested["id"],
                    native_thread_context=context, on_native_event=protocol.accept, now=self.now)
            self.assertEqual("adhoc_write_confinement_unsupported", caught.exception.code)
            self.assertIn(adapter, caught.exception.detail)
            self.assertFalse((slot / "launches.jsonl").exists())
            self.assertFalse((slot / "host-write-report.json").exists())
            self.assertEqual("open", self.work.show(requested["id"])[0]["state"])
            self.assertFalse(protocol.terminal_seen)
            self.assertEqual(before, {name: path.read_bytes() for name, path in forbidden.items()})
            return
        result = runner.run_exact("temporary", adapter, requested["id"],
            native_thread_context=context, on_native_event=protocol.accept, now=self.now)
        self.assertEqual("bind_artifact", result["transition"], (result, runner.last_process_audit))
        self.assertTrue(protocol.terminal_seen)
        if adapter == "claude":
            self.assertIsNone(protocol.opened_coordinate)
        else:
            self.assertIsNotNone(protocol.opened_coordinate)
        terminal = [event for event in runner.last_native_events if event["event"] == "terminal"]
        self.assertEqual(1, len(terminal))
        self.assertEqual(adapter + "_local", terminal[0]["provider_coordinate"]["provider"])
        if usage:
            measured = terminal[0].get("usage")
            self.assertIsNotNone(measured, "actual confined bootstrap must retain provider token testimony")
            self.assertEqual("measured", measured["status"])
            self.assertEqual(140, measured["tokens"]["input_tokens"])
            self.assertEqual(20, measured["tokens"]["output_tokens"])
        self.assertEqual("open", self.work.show(older["id"])[0]["state"])
        self.assertEqual("claimed", self.work.show(requested["id"])[0]["state"])
        self.assertEqual(head_before, self.git(self.workspace, "rev-parse", "HEAD"))
        self.assertTrue((self.workspace / "PROOF.txt").is_file())
        report = json.loads((slot / "host-write-report.json").read_text())
        self.assertEqual(set(forbidden), set(report["forbidden"]))
        for name, observation in report["forbidden"].items():
            with self.subTest(target=name):
                self.assertFalse(observation["written"])
                self.assertIn(observation["errno"], {errno.EPERM, errno.EACCES})
        self.assertEqual(before, {name: path.read_bytes() for name, path in forbidden.items()})
        self.assertEqual(set(allowed), set(report["allowed"]))
        for name, path in allowed.items():
            self.assertEqual({"errno": None, "written": True}, report["allowed"][name])
            self.assertEqual("allowed native provider write\n", path.read_text())
        self.assertEqual(str(slot), report["home"])
        self.assertIsInstance(report["tmpdir"], str)
        self.assertEqual(slot, Path(report["tmpdir"]).resolve())
        launches = [json.loads(line) for line in (slot / "launches.jsonl").read_text().splitlines()]
        self.assertEqual(1, len(launches))
        self.assertEqual(str(slot), launches[0]["home"])
        audit = runner.last_process_audit
        self.assertTrue(audit["isolation_backend"].startswith(
            "macos-sandbox" if sys.platform == "darwin" else "linux-landlock-v"), audit["isolation_backend"])
        self.assertEqual([], audit["alive_after_cleanup"])
        self.assertIsInstance(audit["adapter_pid"], int)
        for pid in {audit["adapter_pid"], launches[0]["pid"]}:
            with self.assertRaises(ProcessLookupError):
                os.kill(pid, 0)
        self.assertEqual(0, runner.last_native_transport["exit_code"])
        self.assertGreater(runner.last_native_deadline, runner.call_timeout)
        self.assertEqual(runner.last_native_transport["combined_bytes"],
            runner.last_native_transport["stdout_bytes"] + runner.last_native_transport["stderr_bytes"])
        self.assertGreater(runner.last_native_transport["stdout_bytes"], 0)
        self.assertEqual(64, len(runner.last_native_transport["stdout_sha256"]))
        if adapter == "claude":
            self.assertEqual(1, json.loads((slot / "saved-session.json").read_text())["turns"])
        else:
            self.assertEqual(1, json.loads((slot / "state.json").read_text())["turns"])

    def test_codex_fake_runs_inside_native_host_boundary_and_is_reaped(self):
        for index, adapter in enumerate(("codex", "claude", "pi")):
            with self.subTest(adapter=adapter):
                if index:
                    # Each adapter needs its own lease, workspace, and private HOME.
                    self.doCleanups()
                    self.setUp()
                self.qualify(adapter, usage=True)

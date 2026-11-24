"""Shared borrowed bootstrap/process ownership, using a fake provider only."""
from pathlib import Path
import os
import stat
import sys
import unittest
from unittest import mock

from floati.adhoc_journal import AdhocJournal, AdhocJournalController
from floati.errors import ProtocolRefusal
from floati.native_thread_protocol import NativeThreadProtocol
from floati.worker_bootstrap_protocol import BuiltInAdapterSpec
from floati.workers import WorkerRunner
from tests import test_adhoc_worker as worker_fixtures


class AdhocRuntimeTests(unittest.TestCase):
    def fixture(self, *, grandchild=False, human_launch=True):
        case = worker_fixtures.BorrowedReadinessTests("test_foreign_effect_binding_refuses_before_claim_or_spawn")
        self.addCleanup(case.doCleanups)
        case.setUp()
        # The fake provider edits PROOF.txt, which its row already tracks; on
        # Linux the row root's own entry set is fixed for the dispatch.
        case.commit_row_entries(case.node_opened["workspace"], {"PROOF.txt": "row placeholder\n"})
        older, requested, context, initial = case.prepared(human_launch=human_launch)
        # This success fixture owns its borrowed tree. Keep its intended-safe
        # precondition explicit under umask 002; the kernel fence stays strict.
        workspace = Path(context["workspace_binding"]["workspace"])
        for path in [workspace, *workspace.rglob("*")]:
            self.assertFalse(path.is_symlink())
            path.chmod(stat.S_IMODE(path.stat().st_mode) & ~0o022)
        store = Path(context["provider_state_binding"]["path"])
        (store / "opened-ack").touch(mode=0o600)
        (store / "opened-ack").write_text("fixture parent ACK marker")
        harness = Path(__file__).parent / "fixtures/adhoc-native/codex_harness.py"
        if grandchild:
            wrapper = case.base / "provider-with-detached-pipes.py"
            wrapper.write_text("import os, subprocess, sys, runpy\nfrom pathlib import Path\n"
                "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(4)'], "
                "stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
                "Path(sys.argv[1], 'grandchild.pid').write_text(str(child.pid))\n"
                "runpy.run_path(" + repr(str(harness.resolve())) + ", run_name='__main__')\n")
            harness = wrapper
        runner = WorkerRunner(case.root, {}, call_timeout=0.1,
            effect_controller=initial.effect_controller,
            effect_adapter_specs={"codex": BuiltInAdapterSpec("codex",
                (str(Path(sys.executable).resolve()), str(harness.resolve()), str(store), "normal"), "borrowed")})
        protocol = NativeThreadProtocol(context, adapter_kind="codex",
            journal_controller=AdhocJournalController(AdhocJournal(case.root)),
            effect_controller=initial.effect_controller, now=case.now)
        return case, older, requested, context, runner, protocol, store

    def test_closed_pipe_grandchild_is_gone_before_success_receipt_returns(self):
        case, older, requested, context, runner, protocol, store = self.fixture(grandchild=True)
        if sys.platform != "darwin" and not sys.platform.startswith("linux"):
            with self.assertRaises(ProtocolRefusal):
                runner.run_exact("node-a", "codex", requested["id"],
                    native_thread_context=context, on_native_event=protocol.accept, now=case.now)
            return
        result = runner.run_exact("node-a", "codex", requested["id"],
            native_thread_context=context, on_native_event=protocol.accept, now=case.now)
        pid = int((store / "grandchild.pid").read_text())
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)
        self.assertEqual(result["transition"], "bind_artifact")
        self.assertEqual(runner.last_process_audit["alive_after_cleanup"], [])

    def test_bootstrap_isolation_failure_is_a_named_confinement_refusal(self):
        from floati.worker_errors import WorkerAdapterFailure
        owner, older, requested, context, runner, protocol, slot = self.fixture()
        with mock.patch.object(runner, "_receive_isolation_ready",
                               side_effect=WorkerAdapterFailure("effect_worker_isolation_unavailable")):
            with self.assertRaises(ProtocolRefusal) as caught:
                runner.run_exact("node-a", "codex", requested["id"],
                    native_thread_context=context, on_native_event=protocol.accept, now=owner.now)
        self.assertEqual("adhoc_write_confinement_unsupported", caught.exception.code)
        self.assertIn("codex", caught.exception.detail)
        self.assertFalse((slot / "launches.jsonl").exists())
        self.assertFalse(protocol.terminal_seen)
        self.assertEqual([], runner.last_process_audit["alive_after_cleanup"])

"""Readiness is an observed event, not a two-second startup promise."""
from __future__ import annotations

import importlib.util
import json
import multiprocessing
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from multiprocessing.process import BaseProcess
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tests.readiness import (
    Readiness, communicate, wait_barrier, wait_event, wait_join, wait_poll,
    wait_process, wait_readable, wait_until,
)


class _ForkExitGate:
    def __init__(self):
        self.reader, self.writer = multiprocessing.Pipe(duplex=False)
        self.released = False

    def wait(self):
        self.writer.close()
        try:
            wait_poll(self.reader, "release the delayed fork fixture")
            self.reader.recv()
        finally:
            self.reader.close()

    def set(self):
        if not self.released:
            self.writer.send(None)
            self.released = True

    def close(self):
        self.reader.close()
        self.writer.close()


def _hold_spawned_fixture_exit(target, arguments, keywords, gate):
    try:
        target(*arguments, **keywords)
    finally:
        gate.wait()


class LoadedReadinessTests(unittest.TestCase):

    @staticmethod
    def app_server_fixture():
        from tests.test_codex_live_adapter import HARNESS

        spec = importlib.util.spec_from_file_location("app_server_readiness_fixture", HARNESS)
        harness = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(harness)
        return harness

    def test_app_server_fixture_retains_exit_evidence_after_late_waitpid(self):
        from tests.temp_roots import REAL_TEMP_ROOT

        harness = self.app_server_fixture()
        with tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT) as temporary:
            workspace = Path(temporary).resolve()
            evidence = workspace / ".floati"
            evidence.mkdir()
            child = harness.start_observed_descendant(workspace)
            waitpid = child._try_wait
            delayed = False

            def observe(flags):
                nonlocal delayed
                if not delayed and flags == os.WNOHANG:
                    delayed = True
                    return (0, 0)
                return waitpid(flags)

            try:
                with mock.patch.object(child, "_try_wait", observe),\
                        mock.patch.object(subprocess, "_time",
                                          lambda: time.monotonic() + (9 if delayed else 0)):
                    harness.collect_observed_descendant(child, workspace)
            finally:
                if child.poll() is None:
                    child.terminate()
                    wait_process(child, "collect the owned app-server witness")
                if child.stdout is not None:
                    child.stdout.close()
            receipt = json.loads((evidence / "descendant-collected.json").read_text())
            self.assertTrue(receipt["collected"])
            self.assertEqual(child.pid, receipt["pid"])
            self.assertFalse(receipt.get("collection_errors"), receipt)

    def test_app_server_fixture_returns_thread_evidence_after_late_exit(self):
        from tests.temp_roots import REAL_TEMP_ROOT

        harness = self.app_server_fixture()

        def exercise():
            with tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT) as temporary:
                workspace = Path(temporary).resolve()
                owned = workspace / "owned-fixture-file"
                owned.touch()
                arguments = SimpleNamespace(boundary_mode="main-thread", tenant_target=str(owned))
                harness.write_boundary_evidence(arguments, workspace)
                result = json.loads((workspace / "isolation-evidence.json").read_text())
                self.assertEqual({"main": None, "thread": None, "thread_alive": False}, result)
                self.assertEqual(b"changedchanged", owned.read_bytes())

        variants = ((unittest.FunctionTestCase(exercise), {"<lambda>"}),)
        self.check_delayed_fixture_exits(variants, threading.Thread, threading.Event)

    def test_async_fixture_reads_survive_late_response_frames(self):
        from floati.sequencer import SequencerService
        from tests.test_approval_suspension import ApprovalSuspensionManagedTests, _ManagedSuspensionContext

        alias_delivery = "test_identical_semantic_aliases_each_receive_one_response_and_close"
        variants = (
            (ApprovalSuspensionManagedTests(
                "test_raw_private_suspension_frames_and_generic_typed_records_refuse"),
             _ManagedSuspensionContext.raw_append, ("refused", "suspension_controller_only")),
            (ApprovalSuspensionManagedTests(alias_delivery),
             getattr(ApprovalSuspensionManagedTests, alias_delivery), ("ok", None)),
        )
        send = SequencerService._send_response
        recv = socket.socket.recv

        for case, read_owner, wanted in variants:
            with self.subTest(case=case.id()):
                response_ready = threading.Event()
                permit = threading.Event()

                def held_response(service, channel, response):
                    if (response.get("status"), response.get("code")) == wanted and not permit.is_set():
                        response_ready.set()
                        wait_event(permit, "release the pending response frame")
                    return send(service, channel, response)

                def receive(channel, *args, **kwargs):
                    if sys._getframe(1).f_code is not read_owner.__code__ or permit.is_set():
                        return recv(channel, *args, **kwargs)
                    try:
                        wait_event(response_ready, "service has the exact response ready to send")
                        lifetime = channel.gettimeout()
                        if lifetime is not None and lifetime <= 9:
                            # At the old deadline the producer is still held.
                            # Observe the real empty socket without sleeping.
                            channel.setblocking(False)
                            try:
                                return recv(channel, *args, **kwargs)
                            except BlockingIOError as error:
                                raise TimeoutError("fixture read expired before response delivery") from error
                            finally:
                                channel.settimeout(lifetime)
                    finally:
                        permit.set()
                    return recv(channel, *args, **kwargs)

                result = unittest.TestResult()
                try:
                    with mock.patch.object(SequencerService, "_send_response", held_response),\
                            mock.patch.object(socket.socket, "recv", receive):
                        unittest.TestSuite([case]).run(result)
                finally:
                    permit.set()
                self.assertTrue(result.wasSuccessful(), result.failures + result.errors)
                self.assertTrue(response_ready.is_set())

    def test_host_fixtures_collect_threads_after_late_exit(self):
        from tests.test_waiter_lock_retry_bridge import WaiterLockRetryBridgeTests

        case = WaiterLockRetryBridgeTests(
            "test_committed_native_wake_is_not_erased_by_later_corrupt_observation"
        )
        missing = self.check_delayed_fixture_exits(
            ((case, {"publish_when_waiting"}),), threading.Thread, threading.Event,
            private_artifact="hooks/stop-hook-bridge.py",
        )
        if missing is not None:
            self.skipTest(
                "not in this tree (private to the harbor repository by export "
                "policy): " + missing
            )

    def test_missing_stop_bridge_reports_the_nested_fixture_skip(self):
        from tests import private_artifacts

        case = LoadedReadinessTests("test_host_fixtures_collect_threads_after_late_exit")
        result = unittest.TestResult()
        with tempfile.TemporaryDirectory() as empty_root:
            with mock.patch.object(private_artifacts, "REPOSITORY_ROOT", Path(empty_root)):
                case.run(result)

        self.assertTrue(result.wasSuccessful(), result.failures + result.errors)
        self.assertEqual(
            [(case, "not in this tree (private to the harbor repository by export "
                    "policy): hooks/stop-hook-bridge.py")],
            result.skipped,
        )

    def test_host_lifecycle_collector_preserves_late_lawful_exit(self):
        from tests.test_seat_execution_host_lifecycle import HostLifecycleModelTests

        fork = multiprocessing.get_context("fork")

        def terminal_fixture():
            pass

        def exercise():
            process = fork.Process(target=terminal_fixture)
            process.start()
            actual_close = process.close
            statuses = []

            def capture_terminal_status():
                statuses.append(process.exitcode)
                actual_close()

            with mock.patch.object(process, "close", capture_terminal_status):
                HostLifecycleModelTests.reap_fixture(process)
            self.assertEqual([0], statuses)

        self.check_delayed_fixture_exits(
            ((unittest.FunctionTestCase(exercise), {"terminal_fixture"}),),
            BaseProcess, _ForkExitGate,
        )

    def test_crash_fixtures_collect_the_real_exit_after_host_delay(self):
        from tests.test_sequencer_epoch import SequencerEpochTests

        case = SequencerEpochTests("test_abandoned_host_owner_lock_allows_explicit_takeover")
        self.check_delayed_fixture_exits(
            ((case, {"_abandon_managed_owner"}),), BaseProcess, _ForkExitGate, fork_exit=True,
        )

    def test_spawned_effect_fixtures_collect_after_host_delay(self):
        from tests.test_runtruth import EffectAcceptanceTests

        case = EffectAcceptanceTests("test_no_new_intent_can_race_after_effect_snapshot_and_acceptance_append")
        self.check_delayed_fixture_exits(
            ((case, {"_task6_acceptance_process", "_task6_intent_process"}),),
            BaseProcess, _ForkExitGate, delay=21,
        )

    def test_ask_cleanup_collects_the_original_owned_group_after_late_eof(self):
        from tests.test_net_ask_1 import NetAsk1Tests

        test_case = NetAsk1Tests("test_ask_returns_the_correlated_answer_with_its_receipt")
        child = subprocess.Popen(
            [sys.executable, "-c", "import sys; sys.stdin.buffer.read()"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            start_new_session=True,
        )
        original = child._communicate

        def delayed_output(input_data, deadline, timeout):
            if deadline is None:
                return original(input_data, deadline, timeout)
            with mock.patch.object(subprocess, "_time", lambda: time.monotonic() + 9):
                return original(input_data, deadline, timeout)

        try:
            with mock.patch.object(child, "_communicate", delayed_output),\
                    mock.patch.object(child, "kill", side_effect=AssertionError("duplicate signal")):
                test_case.reap(child)
            self.assertIsNotNone(child.returncode)
        finally:
            if child.poll() is None:
                child.kill()
            wait_process(child, "collect the ask cleanup witness")
            for stream in (child.stdin, child.stdout, child.stderr):
                stream.close()

    def test_forensics_cleanup_observes_owned_child_reap_after_host_delay(self):
        from tests import test_wake_timeout_forensics as fixture

        case = fixture.WakeTimeoutForensicsTests("test_pipe_blocked_child_sidecar_names_the_blocked_call")
        child = subprocess.Popen(
            [sys.executable, "-c", "import sys; sys.stdin.buffer.read()"],
            stdin=subprocess.PIPE,
        )
        real_kill = os.kill
        calls = 0

        def monotonic():
            nonlocal calls
            calls += 1
            return time.monotonic() + (9 if calls > 1 else 0)

        def observed_kill(pid, signum):
            if pid == child.pid and signum == 0:
                wait_process(child, "observe the owned forensic witness exit")
            return real_kill(pid, signum)

        try:
            with mock.patch.object(fixture, "time", SimpleNamespace(monotonic=monotonic, sleep=time.sleep), create=True),\
                    mock.patch.object(os, "kill", observed_kill):
                case._terminate_for_cleanup(child.pid)
            self.assertIsNotNone(child.returncode)
        finally:
            if child.poll() is None:
                child.kill()
            wait_process(child, "collect the forensic cleanup witness")
            child.stdin.close()

    def test_observed_success_survives_a_late_parent_after_the_operation_returns(self):
        from floati import cli, codex_wait, host_deadline
        from tests import test_codex_wait, test_host_permission_deadline
        from tests import test_seat_execution_host_tool_controls as hook

        variants = (
            (test_codex_wait, codex_wait, "run_stop_waiter",
             test_codex_wait.CodexWaitParticipationTests(
                 "test_unbound_workspace_is_silent_instant_exit_without_node_state")),
            (hook, cli, "_wait_payload", hook.HostToolCallbackControls(
                 "test_both_options_preserve_empty_legacy_stop_and_reject_partial_open_stdin")),
            (hook, cli, "_wait_payload", hook.HostToolCallbackControls(
                 "test_both_options_preserve_delayed_complete_input_without_waiting_for_eof")),
            (test_host_permission_deadline, host_deadline, "start_deadline",
             test_host_permission_deadline.HostPermissionDeadlineTests(
                 "test_owned_child_starts_promptly_and_exits_after_one_due_publication")),
        )
        for fixture, owner, name, case in variants:
            with self.subTest(case=case.id()):
                actual = getattr(owner, name)
                delay = False

                def settle(*args, **kwargs):
                    nonlocal delay
                    reply = actual(*args, **kwargs)
                    delay = True
                    return reply

                clock = SimpleNamespace(
                    monotonic=lambda: time.monotonic() + (9 if delay else 0),
                    sleep=time.sleep,
                )
                result = unittest.TestResult()
                with mock.patch.object(fixture, "time", clock, create=True),\
                        mock.patch.object(owner, name, settle):
                    unittest.TestSuite([case]).run(result)
                self.assertTrue(result.wasSuccessful(), result.failures + result.errors)
                self.assertTrue(delay)

    def check_delayed_fixture_exits(
        self, variants, worker_type, event_factory, *, fork_exit=False, delay=9,
        private_artifact=None,
    ):
        start_worker = worker_type.start
        join_worker = worker_type.join

        for case, target_names in variants:
            with self.subTest(case=case.id()):
                exits = {}

                def start(worker):
                    target_name = "function" if isinstance(worker, threading.Timer) else "_target"
                    target = getattr(worker, target_name)
                    if getattr(target, "__name__", None) in target_names:
                        ready_to_exit = exits[worker] = event_factory()
                        if getattr(worker, "_start_method", None) == "spawn":
                            worker._target = _hold_spawned_fixture_exit
                            worker._args = (target, worker._args, worker._kwargs, ready_to_exit)
                            worker._kwargs = {}
                            return start_worker(worker)

                        def run(*args, **kwargs):
                            if fork_exit:
                                actual_exit = os._exit
                                owner_pid = os.getpid()

                                def exit_after_gate(status):
                                    if os.getpid() == owner_pid:
                                        ready_to_exit.wait()
                                    actual_exit(status)

                                with mock.patch.object(os, "_exit", exit_after_gate):
                                    _hold_spawned_fixture_exit(target, args, kwargs, ready_to_exit)
                            else:
                                _hold_spawned_fixture_exit(target, args, kwargs, ready_to_exit)

                        setattr(worker, target_name, run)
                    return start_worker(worker)

                def join(worker, timeout=None):
                    if worker not in exits:
                        return join_worker(worker, timeout)
                    # Model scheduling delay between the
                    # worker's last operation and its real exit.
                    if timeout is not None and timeout <= delay:
                        return join_worker(worker, 0)
                    exits[worker].set()
                    return join_worker(worker, None if timeout is None else timeout - delay)

                result = unittest.TestResult()
                try:
                    with mock.patch.object(worker_type, "start", start),\
                            mock.patch.object(worker_type, "join", join):
                        unittest.TestSuite([case]).run(result)
                        unfinished = [worker.name for worker in exits
                                      if not getattr(worker, "_closed", False) and worker.is_alive()]
                finally:
                    for ready_to_exit in exits.values():
                        ready_to_exit.set()
                    for worker, ready_to_exit in exits.items():
                        try:
                            if not getattr(worker, "_closed", False):
                                wait_join(worker, "release held fixture worker exit")
                        finally:
                            if isinstance(ready_to_exit, _ForkExitGate):
                                ready_to_exit.close()
                self.assertTrue(result.wasSuccessful(), result.failures + result.errors)
                if private_artifact is not None and result.skipped == [
                    (case, "not in this tree (private to the harbor repository by export "
                           "policy): " + private_artifact)
                ]:
                    return private_artifact
                self.assertTrue(exits)
                self.assertEqual([], unfinished)

    def test_native_fixture_collects_real_terminal_process_after_host_delay(self):
        from tests.test_native_io import OrdinaryNativeIOTests

        method = "test_collected_single_process_fixture_is_not_signalled_again"
        case = OrdinaryNativeIOTests(method)
        original_wait = subprocess.Popen._wait
        observed = []

        def delayed_wait(process, timeout):
            owner = getattr(case, method).__code__
            caller = sys._getframe(2)
            if caller.f_code is not owner and caller.f_back.f_code is not owner:
                return original_wait(process, timeout)
            observed.append(process)
            original_waitpid = process._try_wait
            deferred = False

            def waitpid(flags):
                nonlocal deferred
                if not deferred and flags == os.WNOHANG:
                    deferred = True
                    return (0, 0)
                return original_waitpid(flags)

            with mock.patch.object(process, "_try_wait", waitpid),\
                    mock.patch.object(subprocess, "_time",
                                      lambda: time.monotonic() + (9 if deferred else 0)):
                return original_wait(process, timeout)

        result = unittest.TestResult()
        with mock.patch.object(subprocess.Popen, "_wait", delayed_wait):
            unittest.TestSuite([case]).run(result)
        self.assertTrue(result.wasSuccessful(), result.failures + result.errors)
        self.assertEqual(1, len(observed))
        self.assertEqual(0, observed[0].returncode)

    def test_fixture_output_survives_delayed_parent_collection(self):
        from tests.test_adhoc_claude import AdhocClaudeTests

        case = AdhocClaudeTests("test_second_process_resumes_recorded_session_only")
        start_process = subprocess.Popen.__init__
        communicate_output = subprocess.Popen._communicate
        selected = set()
        observed = False

        def start(process, *options, **kwargs):
            start_process(process, *options, **kwargs)
            if (isinstance(process.args, (list, tuple)) and
                    any(os.fsdecode(arg).endswith("/claude_driver.py") for arg in process.args)):
                selected.add(process)

        def delayed_collection(process, data, deadline, timeout):
            nonlocal observed
            if process not in selected:
                return communicate_output(process, data, deadline, timeout)
            observed = True
            # The child still writes and exits normally. Only the
            # parent's time before pipe collection advances.
            with mock.patch.object(subprocess, "_time", lambda: time.monotonic() + 9):
                return communicate_output(process, data, deadline, timeout)

        result = unittest.TestResult()
        try:
            with mock.patch.object(subprocess.Popen, "__init__", start),\
                    mock.patch.object(subprocess.Popen, "_communicate", delayed_collection):
                unittest.TestSuite([case]).run(result)
        finally:
            for process in selected:
                if process.poll() is None:
                    process.kill()
                wait_process(process, "collect the delayed output witness")
                for stream in (process.stdin, process.stdout, process.stderr):
                    if stream is not None:
                        stream.close()
        self.assertTrue(result.wasSuccessful(), result.failures + result.errors)
        self.assertTrue(observed)

    def test_watch_recovery_survives_a_slow_but_correct_start(self):
        from tests.test_gauntlet_recovery import RecoveryGauntletTests
        original_popen = subprocess.Popen

        def slow_start(argv, **kwargs):
            self.assertEqual(["-m", "floati"], argv[1:3])
            delayed = [argv[0], "-B", "-c", (
                "import runpy, sys, time; time.sleep(2.25); "
                "sys.argv = ['floati', *sys.argv[1:]]; "
                "runpy.run_module('floati', run_name='__main__')"
            ), *argv[3:]]
            return original_popen(delayed, **kwargs)

        result = unittest.TestResult()
        case = RecoveryGauntletTests(
            "test_deleted_root_under_live_watch_exits_typed_without_traceback"
        )
        with mock.patch("tests.test_gauntlet_recovery.subprocess.Popen", slow_start):
            case.run(result)
        self.assertTrue(result.wasSuccessful(), str(result.failures + result.errors))

    def test_timeout_names_the_budget_elapsed_and_load_for_each_gate(self):
        event = SimpleNamespace(wait=lambda _seconds: False)
        pipe = SimpleNamespace(poll=lambda _seconds: False)
        thread = SimpleNamespace(join=lambda _seconds: None, is_alive=lambda: True)
        barrier = mock.Mock()
        barrier.wait.side_effect = threading.BrokenBarrierError
        process = mock.Mock()
        process.wait.side_effect = subprocess.TimeoutExpired("owned child", 1)
        process.communicate.side_effect = subprocess.TimeoutExpired("owned child", 1)
        gates = [
            lambda: wait_until(lambda: False, "predicate", timeout=0),
            lambda: wait_event(event, "event", timeout=1),
            lambda: wait_poll(pipe, "pipe", timeout=1),
            lambda: wait_join(thread, "thread", timeout=1),
            lambda: wait_barrier(barrier, "barrier", timeout=1),
            lambda: wait_process(process, "exit", timeout=1),
            lambda: communicate(process, "output", timeout=1),
        ]
        for gate in gates:
            with self.subTest(gate=gate), self.assertRaisesRegex(
                AssertionError, r"readiness bound=\d+\.\d{3}s elapsed=\d+\.\d{3}s host_load="
            ):
                gate()

    def test_native_readiness_and_one_shared_frame_budget(self):
        reader, writer = os.pipe()
        try:
            os.write(writer, b"ready\n")
            self.assertEqual([reader], wait_readable(reader, "native first frame"))
            self.assertEqual(b"ready\n", os.read(reader, 6))
            readiness = Readiness("partial frame", timeout=0.01)
            with self.assertRaisesRegex(AssertionError, "partial frame: readiness bound="):
                readiness.readable(reader)
        finally:
            os.close(reader)
            os.close(writer)

    @staticmethod
    def _scanner():
        import importlib.util
        from pathlib import Path
        path = Path(__file__).resolve().parents[1] / "scripts" / "audit-readiness-waits.py"
        spec = importlib.util.spec_from_file_location("readiness_source_audit", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_source_audit_rejects_aliased_and_positional_short_setup_overrides(self):
        import ast
        from pathlib import Path
        module = self._scanner()
        for positional in (False, True):
            with self.subTest(positional=positional):
                arguments = [ast.Name(id="event", ctx=ast.Load()), ast.Constant("setup")]
                if positional:
                    arguments.append(ast.Constant(1))
                tree = ast.Module(body=[
                    ast.ImportFrom(module="tests.readiness", level=0,
                                   names=[ast.alias(name="wait_event", asname="gate")]),
                    ast.Expr(value=ast.Call(func=ast.Name(id="gate", ctx=ast.Load()),
                        args=arguments, keywords=[] if positional else [
                            ast.keyword(arg="timeout", value=ast.Constant(1))])),
                ], type_ignores=[])
                source = ast.unparse(ast.fix_missing_locations(tree))
                scan = module.Scan(Path("tests/synthetic_setup.py"), source)
                scan.visit(ast.parse(source))
                self.assertEqual(["readiness wait fixed"],
                                 [row["classification"] for row in scan.rows])
                self.assertEqual("1", scan.rows[0]["bound_expression"])

    def test_source_audit_requires_review_for_a_changed_exempt_fragment(self):
        import ast
        from pathlib import Path
        module = self._scanner()
        # Assemble the planted snippet as data so the real source census sees
        # the control's AST construction rather than a native readiness wait.
        fragment = chr(10).join(["if broken:", "    target" + ".wait" + "(1)", "else"])
        tree = ast.Module(body=[ast.Expr(value=ast.Constant(fragment))], type_ignores=[])
        source = ast.unparse(ast.fix_missing_locations(tree))
        scan = module.Scan(Path("tests/test_gauntlet_crash.py"), source,
            prefix=("EffectWorkerCrashTests",
                    "test_exec_child_timeout_and_crash_reap_provider_process_groups"))
        scan.visit(ast.parse(source))
        self.assertEqual(["embedded fragment / consumer review"],
                         [row["classification"] for row in scan.rows])

"""Readiness is an observed event, not a two-second startup promise."""
from __future__ import annotations

import subprocess
import os
import threading
import unittest
from types import SimpleNamespace
from unittest import mock

from tests.readiness import (
    Readiness, communicate, wait_barrier, wait_event, wait_join, wait_poll,
    wait_process, wait_readable, wait_until,
)


class LoadedReadinessTests(unittest.TestCase):
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

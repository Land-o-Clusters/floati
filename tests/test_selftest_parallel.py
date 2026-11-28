"""Parallel selftest mode keeps the suite and manifest exit contract.

The fixture repository is a tiny committed tree. These tests must not discover
the harbor suite.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

import floati.selftest as selftest
from tests.readiness import run_process
from tests.temp_roots import REAL_TEMP_ROOT


_RUNNER_SCRIPTS = (
    "test-parallel.py",
    "parallel_plan.py",
    "parallel_unittest.py",
    "parallel_isolation.py",
    "parallel_processes.py",
    "parallel_profile.py",
    "fixture_finalizer.py",
)


class SerialSelftestTests(unittest.TestCase):
    def test_serial_discovery_pins_test_environment_before_module_imports(self):
        with tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT) as temporary:
            root = Path(temporary)
            suite = root / "tests"
            suite.mkdir()
            (suite / "test_first.py").write_text(
                "import os\n"
                "import unittest\n"
                "from floati.cli import main\n"
                "host_temp_at_import = os.environ.get('FLOATI_HOST_TEMP_ROOT')\n"
                "home_at_import = os.environ['HOME']\n"
                "import tests\n"
                "class FirstDiscoveredTest(unittest.TestCase):\n"
                "    def test_import_environment(self):\n"
                "        self.assertEqual(tests.HOST_TEMP_ROOT, host_temp_at_import)\n"
                "        self.assertEqual(tests.HOME_PIN_ROOT, home_at_import)\n",
                encoding="utf-8",
            )
            home = root / "home"
            home.mkdir()
            environment = dict(os.environ, HOME=str(home), TMPDIR=str(root),
                               PYTHONPATH=str(selftest.repository_root()))
            for name in ("FLOATI_HOST_TEMP_ROOT", "FLOATI_TEST_TEMP_ROOT", selftest.WORKERS_ENV):
                environment.pop(name, None)
            # The fixture exercises discovery without installing a second bundle.
            run = run_process(
                [sys.executable, "-B", "-c",
                 "from floati import selftest; "
                 "selftest.finish = lambda success, root: selftest.outcome_for(success, []); "
                 "raise SystemExit(selftest.main([]))"],
                "serial fixture discovery finishes",
                cwd=root, env=environment, capture_output=True, text=True,
            )
            self.assertEqual(0, run.returncode, run.stderr)


class ParallelSelftestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.scratch = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
        self.addCleanup(self.scratch.cleanup)
        self.bank = Path(self.scratch.name)
        self.root = self.bank / "repo"
        self.root.mkdir()

    def test_interruption_collects_child_and_removes_fixtures_during_launch_and_wait(self) -> None:
        import os
        import signal
        import sys
        from contextlib import nullcontext
        from unittest import mock
        from scripts.fixture_finalizer import owned_child, run_child
        from tests.readiness import wait_readable

        original_mask = signal.pthread_sigmask(signal.SIG_UNBLOCK, (signal.SIGINT, signal.SIGTERM))
        self.addCleanup(signal.pthread_sigmask, signal.SIG_SETMASK, original_mask)
        scripts = self.root / "scripts"
        scripts.mkdir()
        runner = scripts / "test-parallel.py"
        runner.write_text(
            "import json, signal, sys, tempfile\n"
            "from pathlib import Path\n"
            "sys.path.insert(0, " + repr(str(Path(__file__).resolve().parents[1])) + ")\n"
            "import tests\n"
            "from tests.temp_roots import REAL_TEMP_ROOT\n"
            "fixture = tempfile.mkdtemp(dir=REAL_TEMP_ROOT)\n"
            "(Path(fixture) / 'fleet').mkdir()\n"
            "print(json.dumps({'paths': [tests.HOME_PIN_ROOT, fixture], "
            "'mask': [int(number) for number in signal.pthread_sigmask(signal.SIG_BLOCK, ())]}), flush=True)\n"
            "signal.pause()\n",
            encoding="utf-8",
        )
        constructor = subprocess.Popen.__init__
        inherited_mask = {int(number) for number in signal.pthread_sigmask(signal.SIG_BLOCK, ())}
        for stage in ("constructor", "constructor_failure", "active", "timeout", "timeout_result"):
            signals = (signal.SIGINT,) if stage.startswith("timeout") else (signal.SIGINT, signal.SIGTERM)
            for number in signals:
                with self.subTest(stage=stage, signal=signal.Signals(number).name):
                    observed = {}
                    spawned = []

                    def ready(process):
                        wait_readable(process.stdout, "owned child fixture readiness")
                        line = process.stdout.readline()
                        self.assertTrue(line, "child exited before fixture readiness")
                        observed.update(json.loads(line))

                    def launch(process, *arguments, **options):
                        constructor(process, *arguments, **options)
                        spawned.append(process)
                        if stage != "active":
                            ready(process)
                            if not stage.startswith("timeout"):
                                os.kill(os.getpid(), number)
                            if stage == "constructor_failure":
                                raise OSError("constructor failed after cancellation")

                    prior = signal.signal(number, signal.SIG_DFL)
                    try:
                        exception = (subprocess.TimeoutExpired if stage == "timeout" else
                                     KeyboardInterrupt if number == signal.SIGINT else SystemExit)
                        expectation = (nullcontext() if stage == "timeout_result" else
                                       self.assertRaises(exception))
                        with mock.patch.object(subprocess.Popen, "__init__", launch),\
                                expectation as caught:
                            if stage.startswith("timeout"):
                                completed = run_child(
                                    [sys.executable, "-B", str(runner)], capture_output=True,
                                    timeout=0, timeout_result=stage == "timeout_result",
                                )
                                self.assertNotEqual(0, completed.returncode)
                            elif stage != "active":
                                selftest.parallel_mode_exit(
                                    1, root=self.root, manifest_root=self.root,
                                    output=self.bank / "unreached-output",
                                )
                            else:
                                with owned_child([sys.executable, "-B", str(runner)],
                                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE) as child:
                                    ready(child)
                                    os.kill(os.getpid(), number)
                        if number == signal.SIGTERM:
                            self.assertEqual(143, caught.exception.code)
                        self.assertEqual(signal.SIG_DFL, signal.getsignal(number))
                        self.assertEqual(inherited_mask, set(observed["mask"]))
                        self.assertEqual(1, len(spawned))
                        self.assertIsNotNone(spawned[0].poll(), "interruption returned before child collection")
                        self.assertEqual([], [path for path in observed["paths"] if Path(path).exists()])
                    finally:
                        signal.signal(number, prior)
                        for process in spawned:
                            if process.poll() is None:
                                process.terminate()
                            process.wait()
                            for stream in (process.stdin, process.stdout, process.stderr):
                                if stream is not None:
                                    stream.close()

    def test_worker_request_comes_from_the_argument_or_environment(self) -> None:
        requested = selftest.requested_workers
        self.assertIsNone(requested([], {}))
        self.assertIsNone(requested([], {"FLOATI_SELFTEST_WORKERS": ""}))
        self.assertEqual(8, requested(["--workers", "8"], {}))
        self.assertEqual(3, requested([], {"FLOATI_SELFTEST_WORKERS": "3"}))
        self.assertEqual(
            8, requested(["--workers", "8"], {"FLOATI_SELFTEST_WORKERS": "3"})
        )

    def test_parallel_mode_fails_when_the_manifest_check_fails(self) -> None:
        code, out = self.run_mode(failing=False, manifest_ok=False)
        self.assertNotEqual(selftest.outcome_for(True, []), code)
        self.assertEqual(selftest.outcome_for(True, ["digest_mismatch"]), code)
        self.assertNotIn("bundle_verified", out)

    def test_hosted_shards_run_each_discovered_id_once_and_refuse_a_red_shard(self) -> None:
        self.write_suite(failing=True)
        self.write_bundle(manifest_ok=True)
        self.commit()
        receipts = []
        for shard in (1, 2):
            output = self.bank / f"shard-{shard}"
            run = run_process(
                [sys.executable, "-B", "scripts/test-parallel.py", "run",
                 "--workers", "1", "--output", str(output),
                 "--shard-count", "2", "--shard-index", str(shard)],
                "isolated shard finishes", cwd=self.root, capture_output=True, text=True,
            )
            self.assertIn(run.returncode, (0, 1), run.stderr)
            receipts.append(json.loads((output / "final.json").read_text()))
        ids = ["test_alpha.Alpha.test_alpha", "test_beta.Beta.test_beta"]
        for field in ("started_ids", "stopped_ids"):
            self.assertCountEqual(ids, [item for row in receipts for item in row["aggregate"][field]])
        self.assertCountEqual([True, False], [row["successful"] for row in receipts])

        from scripts.parallel_plan import reconcile_shards

        head = receipts[0]["source"]["head"]
        self.assertFalse(reconcile_shards(receipts, 2, head)["successful"])
        with self.assertRaises(ValueError):
            reconcile_shards([receipts[0], receipts[0]], 2, head)
        with self.assertRaises(ValueError):
            reconcile_shards(receipts[:1], 2, head)

    def test_single_runner_verdict_checks_the_full_native_population(self) -> None:
        from scripts.parallel_plan import reconcile_shards

        code, _ = self.run_mode(failing=False, manifest_ok=True)
        self.assertEqual(0, code)
        receipt = json.loads((self.bank / "run" / "final.json").read_text())
        head = receipt["source"]["head"]
        verdict = reconcile_shards([receipt], 1, head)
        self.assertTrue(verdict["successful"])
        self.assertEqual(2, verdict["population_count"])

        # Losing a native callback must fail even when the run reports success.
        receipt["results"][0]["started_ids"].clear()
        with self.assertRaises(ValueError):
            reconcile_shards([receipt], 1, head)

    def test_parallel_mode_succeeds_when_the_suite_and_manifest_pass(self) -> None:
        loader = unittest.TestLoader()
        control = loader.loadTestsFromName(
            "tests.harbor_parallel_unittest.NativeDiscoveryTests."
            "test_synthetic_adapter_is_atomic_and_runs_actual_nested_objects"
        )
        self.assertEqual([], loader.errors)
        result = control.run(unittest.TestResult())
        self.assertEqual(1, result.testsRun)
        self.assertTrue(result.wasSuccessful(), (result.errors, result.failures))
        profile = "test_id,seconds\ntest_alpha.Alpha.test_alpha,30\ntest_beta.Beta.test_beta,1\n"
        alternate = "test_id,seconds\ntest_alpha.Alpha.test_alpha,1\ntest_beta.Beta.test_beta,30\n"
        platform = "macos" if sys.platform == "darwin" else "linux"
        other_platform = "linux" if platform == "macos" else "macos"
        code, out = self.run_mode(
            failing=False, manifest_ok=True,
            profiles={platform: profile, other_platform: alternate},
        )
        self.assertEqual(selftest.outcome_for(True, []), code)
        payload = json.loads(out.strip().splitlines()[-1])
        self.assertEqual("bundle_verified", payload["status"])
        report = json.loads((self.bank / "run" / "final.json").read_text(encoding="utf-8"))
        self.assertEqual("profile seconds", report["weights"])
        self.assertEqual(hashlib.sha256(profile.encode()).hexdigest(), report["profile"]["sha256"])
        self.assertEqual(
            ["test_alpha.Alpha.test_alpha", "test_beta.Beta.test_beta"], report["census"]["ids"],
        )
        self.assertEqual(2, report["aggregate"]["tests_run"])

    def test_parallel_mode_prints_the_failing_test_and_the_erroring_subtest(self) -> None:
        code, out = self.run_mode(failing=False, manifest_ok=True, reported=True)
        self.assertNotEqual(selftest.outcome_for(True, []), code)
        self.assertEqual(selftest.outcome_for(False, []), code)
        self.assertNotIn("bundle_verified", out)
        self.assertIn("test_fail.FailCase.test_marked", out)
        self.assertIn("test_sub.SubCase.test_marked [marked-subtest]", out)
        self.assertIn("marked-failure", out)
        self.assertIn("marked-subtest-error", out)
        summary_at = out.find('"counts"')
        self.assertGreater(summary_at, out.find("test_fail.FailCase.test_marked"))
        self.assertGreater(summary_at, out.find("test_sub.SubCase.test_marked [marked-subtest]"))
        self.assertGreater(summary_at, out.find("marked-failure"))
        self.assertGreater(summary_at, out.find("marked-subtest-error"))
        self.assertGreater(summary_at, out.find("2 non-success"))
        self.assertGreater(out.find("2 non-success"), out.find("test_fail.FailCase.test_marked"))
        self.assertGreater(out.find("2 non-success"), out.find("test_sub.SubCase.test_marked [marked-subtest]"))

    def test_parallel_nonsuccess_report_stops_after_two_hundred_lines(self) -> None:
        emit = selftest.emit_parallel_nonsuccess
        output = self.bank / "capped"
        output.mkdir()
        events = [
            {"kind": "failure", "test_id": f"case_cap.Cap.test_{index:03d}"}
            for index in range(201)
        ]
        events.append({"kind": "success", "test_id": "case_cap.Cap.test_quiet"})
        (output / "final.json").write_text(
            json.dumps({"aggregate": {"events": events}}),
            encoding="utf-8",
        )
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            emit(output)
        text = stdout.getvalue()
        self.assertIn("case_cap.Cap.test_000", text)
        self.assertIn("case_cap.Cap.test_199", text)
        self.assertNotIn("case_cap.Cap.test_200", text)
        self.assertNotIn("test_quiet", text)
        self.assertLess(text.find("case_cap.Cap.test_199"), text.find("+1 more"))
        self.assertLess(text.find("+1 more"), text.find("201 non-success"))

    def test_parallel_selftest_reports_a_missing_tool_on_a_successful_run(self) -> None:
        from tests import managed_test_tools, test_opencode_host_bridge

        case_type = test_opencode_host_bridge.OpenCodeHostBridgeTests
        method = "test_unrelated_events_never_launch_observer"
        case = case_type(method)
        with mock.patch.dict(os.environ), mock.patch.object(
            managed_test_tools.shutil, "which", return_value=None
        ):
            os.environ.pop("FLOATI_TEST_NODE_EXECUTABLE", None)
            result = case.run()
            self.assertTrue(result.wasSuccessful(), result.failures + result.errors)
            self.assertEqual([(case, "test_tool_absent: node")], result.skipped)
            with mock.patch.object(
                test_opencode_host_bridge, "BRIDGE", self.bank / "missing.mjs"
            ):
                missing_bridge = case_type(method).run()
            self.assertFalse(missing_bridge.wasSuccessful())
            self.assertEqual([], missing_bridge.skipped)

        output = self.bank / "missing-tool"
        output.mkdir()
        minisign_test = "test_signing.SigningTests.test_real_signature"
        (output / "final.json").write_text(
            json.dumps({"aggregate": {"events": [
                {"kind": "skip", "test_id": minisign_test, "reason": "test_tool_absent: minisign"},
                {"kind": "skip", "test_id": case.id(), "reason": result.skipped[0][1]},
            ]}}),
            encoding="utf-8",
        )
        stdout = io.StringIO()
        selftest.emit_parallel_nonsuccess(output, stream=stdout)
        self.assertIn(f"skip {minisign_test} test_tool_absent: minisign", stdout.getvalue())
        self.assertIn(f"skip {case.id()} test_tool_absent: node", stdout.getvalue())
        self.assertNotIn("non-success", stdout.getvalue())

    def test_parallel_nonsuccess_report_keeps_the_retained_line_and_fixture_errors(self) -> None:
        emit = selftest.emit_parallel_nonsuccess
        output = self.bank / "kinds"
        output.mkdir()
        events = [
            {"kind": "success", "test_id": "quiet.Quiet.test_ok"},
            {"kind": "skip", "test_id": "quiet.Quiet.test_skip", "reason": "nope"},
            {
                "kind": "fixture_skip",
                "test_id": "setUpClass (quiet.Quiet)",
                "fixture": "setUpClass (quiet.Quiet)",
                "reason": "nope",
                "blocked_ids": [],
            },
            {
                "kind": "fixture_error",
                "test_id": "setUpClass (boom.Boom)",
                "fixture": "setUpClass (boom.Boom)",
                "blocked_ids": ["boom.Boom.test_a"],
            },
            {"kind": "unexpected_success", "test_id": "odd.Odd.test_unexpected"},
            {
                "kind": "subtest_failure",
                "test_id": "sub.Sub.test_one",
                "subtest": "sub.Sub.test_one (n=2)",
            },
        ]
        (output / "final.json").write_text(
            json.dumps({"aggregate": {"events": events}}),
            encoding="utf-8",
        )
        (output / "worker-1.log").write_text(
            "======================================================================\n"
            "ERROR: setUpClass (boom.Boom)\n"
            "----------------------------------------------------------------------\n"
            "Traceback (most recent call last):\n"
            "  File \"x.py\", line 1, in setUpClass\n"
            "RuntimeError: fixture-blew\n"
            "\n"
            "======================================================================\n"
            "FAIL: test_one (sub.Sub) (n=2)\n"
            "----------------------------------------------------------------------\n"
            "Traceback (most recent call last):\n"
            "  File \"x.py\", line 2, in test_one\n"
            "AssertionError: sub-broke\n"
            "\n"
            "----------------------------------------------------------------------\n"
            "Ran 1 test in 0.001s\n"
            "\n"
            "FAILED (failures=1)\n",
            encoding="utf-8",
        )
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            emit(output)
        text = stdout.getvalue()
        self.assertIn("fixture_error setUpClass (boom.Boom)", text)
        self.assertIn("fixture-blew", text)
        self.assertIn("unexpected_success odd.Odd.test_unexpected", text)
        self.assertIn("subtest_failure sub.Sub.test_one (n=2)", text)
        self.assertIn("sub-broke", text)
        self.assertNotIn("quiet.Quiet.test_ok", text)
        self.assertNotIn("quiet.Quiet.test_skip", text)
        self.assertNotIn("setUpClass (quiet.Quiet)", text)
        self.assertNotIn("FAILED (failures=1)", text)
        self.assertNotIn("boom.Boom.test_a", text)

    def run_mode(
        self, *, failing: bool, manifest_ok: bool, reported: bool = False,
        profiles: dict[str, str] | None = None,
    ) -> tuple[int, str]:
        parallel_mode_exit = selftest.parallel_mode_exit
        self.write_suite(failing=failing, reported=reported)
        if profiles:
            directory = self.root / "scripts" / "profiles"
            directory.mkdir()
            for platform, content in profiles.items():
                (directory / f"harbor-{platform}.csv").write_text(content, encoding="utf-8")
        self.write_bundle(manifest_ok=manifest_ok)
        self.commit()
        output = self.bank / "run"
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = parallel_mode_exit(
                2, root=self.root, manifest_root=self.root, output=output
            )
        return code, stdout.getvalue()

    def write_suite(self, *, failing: bool, reported: bool = False) -> None:
        if reported:
            self.write_reported_suite()
            self.copy_runner_scripts()
            return
        beta = (
            "        self.fail('fixture red')\n"
            if failing
            else "        self.assertTrue(True)\n"
        )
        (self.root / "test_alpha.py").write_text(
            "import unittest\n"
            "class Alpha(unittest.TestCase):\n"
            "    def test_alpha(self):\n"
            "        self.assertTrue(True)\n",
            encoding="utf-8",
        )
        (self.root / "test_beta.py").write_text(
            "import unittest\n"
            "class Beta(unittest.TestCase):\n"
            "    def test_beta(self):\n" + beta,
            encoding="utf-8",
        )
        self.copy_runner_scripts()

    def write_reported_suite(self) -> None:
        (self.root / "test_fail.py").write_text(
            "import unittest\n"
            "class FailCase(unittest.TestCase):\n"
            "    def test_marked(self):\n"
            "        self.fail('marked-failure')\n",
            encoding="utf-8",
        )
        (self.root / "test_sub.py").write_text(
            "import unittest\n"
            "class SubCase(unittest.TestCase):\n"
            "    def test_marked(self):\n"
            "        with self.subTest('marked-subtest'):\n"
            "            raise RuntimeError('marked-subtest-error')\n",
            encoding="utf-8",
        )

    def copy_runner_scripts(self) -> None:
        scripts = self.root / "scripts"
        scripts.mkdir()
        original = Path(__file__).resolve().parents[1] / "scripts"
        for name in _RUNNER_SCRIPTS:
            shutil.copyfile(original / name, scripts / name)
        (scripts / "parallel_serial_classes.json").write_text("{}\n", encoding="utf-8")

    def write_bundle(self, *, manifest_ok: bool) -> None:
        package = self.root / "floati"
        package.mkdir()
        (package / "core.py").write_bytes(b"CORE\n")
        (self.root / "schemas" / "v0").mkdir(parents=True)
        (self.root / "schemas" / "v0" / "record.json").write_bytes(b"{}\n")
        launcher = self.root / "scripts" / "floati"
        launcher.write_bytes(b"#!/bin/sh\nexit 0\n")
        launcher.chmod(0o755)
        files = [
            "floati/core.py",
            "schemas/v0/record.json",
            "scripts/floati",
        ]
        entries = []
        for relative in files:
            digest = hashlib.sha256((self.root / relative).read_bytes()).hexdigest()
            if not manifest_ok and relative == "floati/core.py":
                digest = "0" * 64
            entries.append({"path": relative, "sha256": digest})
        payload = {
            "schema_version": 0,
            "protocol_version": "0",
            "canonical_ref": "refs/heads/lane/hm0",
            "files": entries,
        }
        (self.root / "bundle-manifest.v0.json").write_text(
            json.dumps(payload, sort_keys=True), encoding="utf-8"
        )

    def commit(self) -> None:
        for args in (
            ("init", "-q"),
            ("add", "."),
            (
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.invalid",
                "commit",
                "-qm",
                "fixture",
            ),
        ):
            subprocess.run(
                ["/usr/bin/git", *args],
                cwd=self.root,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )


if __name__ == "__main__":
    unittest.main()

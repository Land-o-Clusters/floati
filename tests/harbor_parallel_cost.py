"""Observer diagnostics must measure work without changing ownership decisions."""
from __future__ import annotations

from collections import Counter
import errno
import hashlib
import json
import os
from pathlib import Path
import signal
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from scripts import parallel_processes
from tests.temp_roots import REAL_TEMP_ROOT


class ParallelProcessCostTests(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
        self.addCleanup(scratch.cleanup)
        self.output = Path(scratch.name)
        self.tree = parallel_processes.ProcessTree(self.output)
        self.addCleanup(self.tree.journal.close)

    def cost(self):
        return self.tree.cost()

    def identity(self, pid=100, parent=1):
        return {"pid": pid, "birth": [pid, 1], "ppid": parent,
                "uid": os.getuid(), "zombie": False}

    @patch.object(parallel_processes, "OWNED_LIVE_PROCESS_CAP", 32)
    def test_completed_identity_turnover_beyond_65536_keeps_valid_native_testimony(self):
        """Lifetime history is evidence, never the live ownership budget."""
        parent = self.identity()
        live = {100: parent}
        self.tree.query = SimpleNamespace(
            identity=live.get,
            children=lambda pid: list(live.keys() - {100}) if pid == 100 else [],
        )
        self.tree.retain(parent, "worker-1")
        # Lifetime turnover exceeds the patched cap; each live cohort fits it.
        for start in range(101, 101 + 66 * 16, 16):
            live.update((pid, self.identity(pid, 100)) for pid in range(start, start + 16))
            self.tree.observe()
            self.assertEqual([], self.tree.errors)
            live.clear()
            live[100] = parent
            self.assertEqual([100], [row["pid"] for row in self.tree.alive()])
        live.clear()
        self.assertEqual([], self.tree.alive())
        receipt = self.tree.receipt()
        self.assertEqual(1 + 66 * 16, len(receipt["observed"]))
        self.assertEqual(len(receipt["observed"]), len(receipt["departed"]))
        self.assertEqual(len(receipt["observed"]), len(
            (self.output / "processes.jsonl").read_text().splitlines()))
        parallel_processes.validate_ownership_receipt(receipt, [
            {"key": "worker-1", "pid": 100, "returncode": 0, "log": "worker-1.log"},
        ], "darwin")

    @patch.object(parallel_processes, "OWNED_LIVE_PROCESS_CAP", 32)
    def test_live_identity_overflow_still_refuses_with_named_reason(self):
        live = {pid: self.identity(pid) for pid in range(100, 100 + 32)}
        self.tree.query = SimpleNamespace(identity=live.get, children=lambda pid: [])
        for item in live.values():
            self.tree.retain(item, "worker-1")
        with self.assertRaisesRegex(OSError, "owned live process observation exceeded bound"):
            self.tree.retain(self.identity(100 + 32), "worker-1")
        self.assertEqual(32, len(self.tree.active))
        self.assertEqual(32, len(self.tree.receipt()["observed"]))

    @patch.object(parallel_processes, "OWNED_LIVE_PROCESS_CAP", 32)
    def test_capacity_rechecks_departures_before_refusing_new_ownership(self):
        live = {pid: self.identity(pid) for pid in range(100, 100 + 32)}
        self.tree.query = SimpleNamespace(identity=live.get, children=lambda pid: [])
        for item in live.values():
            self.tree.retain(item, "worker-1")
        # No observe/alive call intervenes: admission must refresh stale liveness.
        live.clear()
        newcomer = self.identity(100 + 32)
        self.tree.retain(newcomer, "worker-1")
        self.assertEqual(1, len(self.tree.active))
        self.assertEqual(32, len(self.tree.receipt()["departed"]))
        self.assertEqual(33, len(self.tree.receipt()["observed"]))

    def test_capacity_keeps_unreadable_and_uncertain_live_identities_counted(self):
        for code in (errno.EPERM, errno.EIO):
            with self.subTest(errno=code):
                item = self.identity()
                # The same retained identity is already present after the first case.
                self.tree.retain(item, "worker-1")

                def unreadable(pid):
                    raise OSError(code, "controlled unreadable identity", pid)

                self.tree.query = SimpleNamespace(identity=unreadable, witness=lambda pid: item)
                with patch.object(parallel_processes, "OWNED_LIVE_PROCESS_CAP", 1):
                    with self.assertRaisesRegex(OSError, "owned live process observation exceeded bound"):
                        self.tree.retain(self.identity(101), "worker-1")
                self.assertEqual(1, len(self.tree.active))
                self.assertEqual([], self.tree.receipt()["departed"])

    def test_shared_validator_preserves_native_unestablished_exit_testimony(self):
        """Actual producer rows, controlled kernel queries; no process is signaled."""
        parent = self.identity()
        self.tree.retain(parent, "worker-1")
        self.tree.query = SimpleNamespace(
            identity=lambda pid: parent if pid == 100 else None,
            children=lambda pid: [101] if pid == 100 else [],
            witness=lambda pid: None,
        )
        self.tree.observe()  # An enumerated descendant exits before identity read.
        self.tree.query.identity = lambda pid: None
        self.assertEqual([], self.tree.alive())  # The known parent has now departed.
        self.tree.root(SimpleNamespace(pid=200, returncode=0, _child_created=True), "worker-2")
        receipt = self.tree.receipt()  # Second leader exited before the first read.
        before = json.dumps(receipt, sort_keys=True)
        children = [{"key": key, "pid": pid, "returncode": 0, "log": key + ".log"}
                    for key, pid in (("worker-1", 100), ("worker-2", 200))]
        parallel_processes.validate_ownership_receipt(receipt, children, "darwin")
        self.assertEqual(before, json.dumps(receipt, sort_keys=True))
        self.assertFalse(receipt["exhaustive"])
        unknown = [row for row in receipt["departed"] if row.get("ownership") == "unestablished"]
        self.assertEqual({101, 200}, {row["pid"] for row in unknown})
        self.assertTrue(all(row["birth"] is None for row in unknown))

    def test_query_attempts_include_none_and_errors_and_preserve_signal_decisions(self):
        trace = []
        parent, child = self.identity(), self.identity(101, 100)

        def identity(pid):
            trace.append(("identity", pid))
            if pid in (100, 101):
                return parent if pid == 100 else child
            if pid == 200:
                return None
            raise OSError({201: errno.ESRCH, 202: errno.EPERM, 203: errno.EIO}[pid],
                          "controlled query result", pid)

        def children(pid):
            trace.append(("children", pid))
            return [101] if pid == 100 else []

        def witness(pid):
            trace.append(("witness", pid))
            return None

        self.tree.query = SimpleNamespace(identity=identity, children=children, witness=witness)
        self.assertEqual((None, None), self.tree.read(200))
        self.assertEqual((None, None), self.tree.read(201))
        self.assertIsNone(self.tree.read(202)[0])
        with self.assertRaises(OSError) as caught:
            self.tree.read(203)
        self.assertEqual(errno.EIO, caught.exception.errno)
        self.tree.retain(parent, "worker-1")
        self.tree.observe()
        self.assertEqual({100, 101}, {row["pid"] for row in self.tree.alive()})
        signaled = []
        with patch.object(parallel_processes.os, "kill", lambda pid, number: signaled.append((pid, number))):
            self.tree.signal(signal.SIGINT)
        self.assertEqual({(100, signal.SIGINT), (101, signal.SIGINT)}, set(signaled))
        self.assertEqual([], self.tree.errors)
        attempts = Counter(kind for kind, _ in trace)
        observed = self.cost()
        for kind in ("identity", "children", "witness"):
            self.assertEqual(attempts[kind], observed[kind + "_attempts"], kind)

    def test_observe_totals_include_empty_cap_and_base_exception_returns(self):
        with patch.object(parallel_processes.time, "perf_counter_ns", side_effect=[100, 140, 200, 270]),\
                patch.object(parallel_processes.time, "process_time_ns", side_effect=[10, 15, 30, 37]):
            self.tree.observe()
            self.tree.observe()
        observed = self.cost()
        self.assertEqual((2, 110, 12), (observed["observe_calls"], observed["observe_wall_ns"],
                                       observed["observe_process_cpu_ns"]))
        parent = self.identity()
        self.tree.retain(parent, "worker-1")
        with patch.object(parallel_processes.time, "monotonic", side_effect=[0, 5]),\
                patch.object(parallel_processes.time, "perf_counter_ns", side_effect=[1000, 1200]),\
                patch.object(parallel_processes.time, "process_time_ns", side_effect=[300, 340]):
            self.tree.observe()
        self.assertEqual("owned observation time cap reached",
                         self.tree.receipt()["limited_observation"]["reason"])

        def interrupted(pid):
            raise KeyboardInterrupt("controlled observation interruption")

        self.tree.query = SimpleNamespace(identity=lambda pid: parent, children=interrupted)
        with patch.object(parallel_processes.time, "perf_counter_ns", side_effect=[1300, 1700]),\
                patch.object(parallel_processes.time, "process_time_ns", side_effect=[350, 410]):
            with self.assertRaisesRegex(KeyboardInterrupt, "controlled observation interruption"):
                self.tree.observe()
        observed = self.cost()
        self.assertEqual((4, 710, 112), (observed["observe_calls"], observed["observe_wall_ns"],
                                        observed["observe_process_cpu_ns"]))

    def test_wall_measurement_contains_blocking_and_stays_within_external_brackets(self):
        parent = self.identity()
        self.tree.retain(parent, "worker-1")

        def children(pid):
            time.sleep(.02)
            return []

        self.tree.query = SimpleNamespace(identity=lambda pid: parent, children=children)
        wall_before, cpu_before = time.perf_counter_ns(), time.process_time_ns()
        self.tree.observe()
        cpu_after, wall_after = time.process_time_ns(), time.perf_counter_ns()
        observed = self.cost()
        self.assertGreaterEqual(observed["observe_wall_ns"], 20_000_000)
        self.assertLessEqual(observed["observe_wall_ns"], wall_after - wall_before)
        self.assertGreaterEqual(observed["observe_process_cpu_ns"], 0)
        self.assertLessEqual(observed["observe_process_cpu_ns"], cpu_after - cpu_before)

    def test_submitted_journal_bytes_match_actual_complete_lines_without_duplicate_retention(self):
        self.tree.retain(self.identity(), "worker-\N{GREEK SMALL LETTER ALPHA}")
        self.tree.retain(self.identity(101, 100), "worker-\N{GREEK SMALL LETTER ALPHA}")
        self.tree.retain(self.identity(), "worker-\N{GREEK SMALL LETTER ALPHA}")
        data = (self.output / "processes.jsonl").read_bytes()
        self.assertEqual(2, len([json.loads(line) for line in data.splitlines()]))
        observed = self.cost()
        self.assertEqual(2, observed["journal_write_attempts"])
        self.assertEqual(len(data), observed["journal_bytes_submitted"])

    def test_failed_journal_write_counts_submitted_bytes_without_claiming_acceptance(self):
        submitted = []

        def refused(text):
            submitted.append(text)
            raise OSError("controlled journal refusal")

        self.tree.journal.close()
        self.tree.journal = SimpleNamespace(write=refused, flush=lambda: None, close=lambda: None)
        with self.assertRaisesRegex(OSError, "controlled journal refusal"):
            self.tree.retain(self.identity(), "worker-1")
        self.assertEqual(b"", (self.output / "processes.jsonl").read_bytes())
        observed = self.cost()
        self.assertEqual(1, observed["journal_write_attempts"])
        self.assertEqual(len(submitted[0].encode("utf-8")), observed["journal_bytes_submitted"])
        self.assertGreater(observed["journal_bytes_submitted"], 0)

    def test_snapshot_mutation_cannot_change_totals_or_existing_ownership_receipt(self):
        original = self.tree.receipt()
        self.tree.observe()
        observed = self.cost()
        self.assertEqual(1, observed["observe_calls"])
        observed["observe_calls"] = 1000
        self.assertEqual(1, self.cost()["observe_calls"])
        self.assertEqual(original, self.tree.receipt())


class ParallelProcessCostArtifactTests(unittest.TestCase):
    def test_cost_publication_is_inside_total_wall_and_budget_decision(self):
        for target_wall, expected in ((600.0, True), (601.0, False)):
            with self.subTest(wall=target_wall):
                from tests import harbor_parallel_launcher
                fixture = harbor_parallel_launcher.ParallelLauncherIntegrationTests("runTest")
                self.addCleanup(fixture.doCleanups)
                fixture.setUp()
                fixture.fixture("import unittest\nclass Case(unittest.TestCase):\n def test_one(self): pass\n")
                launcher = fixture.launcher()
                reference = launcher.launch("serial", fixture.bank / "serial", 1)
                self.assertTrue(reference["successful"])
                self.assertTrue(reference["cleanup"]["successful"])
                original_open, original_clock = Path.open, time.monotonic
                clock_origin = [None]
                published = []

                class PublicationBoundary:
                    def __init__(self, stream):
                        self.stream = stream

                    def __enter__(self):
                        self.stream.__enter__()
                        return self

                    def __exit__(self, *arguments):
                        result = self.stream.__exit__(*arguments)
                        # Advance only the parent's deadline clock after actual publication.
                        # Children and all ownership queries ran on the native clock.
                        published.append(True)
                        return result

                    def __getattr__(self, name):
                        return getattr(self.stream, name)

                def open_at_boundary(path, *arguments, **keywords):
                    stream = original_open(path, *arguments, **keywords)
                    if path == fixture.bank / "out" / "process-cost.json":
                        return PublicationBoundary(stream)
                    return stream

                def publication_clock():
                    now = original_clock()
                    if clock_origin[0] is None:
                        clock_origin[0] = now
                    # Preserve native elapsed time before the actual publication.
                    return target_wall if published else now - clock_origin[0]

                with patch.object(Path, "open", open_at_boundary),\
                        patch.object(launcher.time, "monotonic", publication_clock):
                    result = launcher.launch("run", fixture.bank / "out", 1,
                                             reference=fixture.bank / "serial" / "final.json")
                self.assertTrue(published, "no cost publication reached the measured boundary")
                self.assertEqual("complete", result["status"])
                self.assertTrue(result["successful"])
                self.assertTrue(result["comparison"]["equivalent"])
                self.assertEqual(target_wall, result["wall_seconds"])
                self.assertTrue(result["cleanup"]["successful"])
                self.assertIs(expected, result["qualified"], "publication boundary must use the inclusive 600-second gate")


    def test_completed_native_launcher_writes_separate_cost_bound_to_its_run(self):
        from tests import harbor_parallel_launcher
        fixture = harbor_parallel_launcher.ParallelLauncherIntegrationTests("runTest")
        self.addCleanup(fixture.doCleanups)
        fixture.setUp()
        fixture.fixture("import unittest\nclass Case(unittest.TestCase):\n def test_one(self): pass\n")
        process, result = fixture.run_cli()
        self.assertEqual(0, process.returncode, process.stderr)
        self.assertTrue(result["successful"])
        path = fixture.bank / "out" / "process-cost.json"
        self.assertTrue(path.is_file(), "native launcher omitted its separate process-cost artifact")
        cost = json.loads(path.read_text())
        self.assertEqual(result["source"], cost["source"])
        self.assertEqual(result["interpreter"], cost["interpreter"])
        self.assertEqual(result["final_path"], cost["final_path"])
        self.assertEqual(hashlib.sha256((fixture.bank / "out" / "request.json").read_bytes()).hexdigest(),
                         cost["request_sha256"])
        self.assertGreater(cost["cost"]["observe_calls"], 0)
        self.assertGreater(cost["cost"]["journal_bytes_submitted"], 0)
        self.assertNotIn("cost", result["cleanup"]["ownership"])

    def test_cost_persistence_refusal_warns_without_changing_native_success(self):
        from tests import harbor_parallel_launcher
        fixture = harbor_parallel_launcher.ParallelLauncherIntegrationTests("runTest")
        self.addCleanup(fixture.doCleanups)
        fixture.setUp()
        fixture.fixture("import unittest\nfrom pathlib import Path\nclass Case(unittest.TestCase):\n"
                        " def test_one(self):\n"
                        "  (Path(__file__).resolve().parents[1] / 'out' / 'process-cost.json').mkdir()\n")
        process, result = fixture.run_cli()
        self.assertEqual(0, process.returncode, process.stderr)
        self.assertEqual("complete", result["status"])
        self.assertTrue(result["successful"])
        self.assertTrue(result["cleanup"]["successful"])
        self.assertIn("process-cost artifact unavailable", process.stderr)

    def test_cost_and_warning_refusal_still_preserve_native_final_receipt(self):
        from tests import harbor_parallel_launcher
        fixture = harbor_parallel_launcher.ParallelLauncherIntegrationTests("runTest")
        self.addCleanup(fixture.doCleanups)
        fixture.setUp()
        fixture.fixture("import unittest\nfrom pathlib import Path\nclass Case(unittest.TestCase):\n"
                        " def test_one(self):\n"
                        "  (Path(__file__).resolve().parents[1] / 'out' / 'process-cost.json').mkdir()\n")

        def broken_warning(text):
            raise OSError("controlled stderr refusal")

        launcher = fixture.launcher()
        with patch.object(launcher.sys, "stderr", SimpleNamespace(write=broken_warning, flush=lambda: None)):
            try:
                result = launcher.launch("run", fixture.bank / "out", 1)
            except OSError as exc:
                self.fail("informational cost warning replaced the native receipt: " + str(exc))
        self.assertEqual("complete", result["status"])
        self.assertTrue(result["successful"])
        self.assertTrue(result["cleanup"]["successful"])
        self.assertEqual(result, json.loads((fixture.bank / "out" / "final.json").read_text()))

    def test_output_admission_refusal_preserves_every_preexisting_artifact(self):
        from tests import harbor_parallel_launcher
        fixture = harbor_parallel_launcher.ParallelLauncherIntegrationTests("runTest")
        self.addCleanup(fixture.doCleanups)
        fixture.setUp()
        fixture.fixture("import unittest\nclass Case(unittest.TestCase):\n def test_one(self): pass\n")
        output = fixture.bank / "out"
        output.mkdir()
        (output / "process-cost.json").write_bytes(b"operator sentinel\n")
        _, result = fixture.run_cli()
        self.assertEqual("invalid", result["status"])
        self.assertEqual({"process-cost.json": b"operator sentinel\n"},
                         {path.name: path.read_bytes() for path in output.iterdir()})


if __name__ == "__main__":
    unittest.main()

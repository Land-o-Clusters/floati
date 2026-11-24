"""Runner ownership controls; no host processes, product handlers or live roots.

Required APIs live in tests/fixtures/bus_scale_census/run-bus-scale-census-2026-10-09.py:
content_witness(root, relative_paths), CONTENT_PATHS, launch_owned_child,
collect_owned_child(child, *, deadline_seconds, collection_seconds, clock,
cancelled), cleanup_owned_scratch(scratch, *, child_collected), and
attempt_exit_code(record). Collection returns status, exit_code,
child_collected and interrupted. Status is complete/cancelled/deadline/uncollected.
"""
from __future__ import annotations

import hashlib
import importlib.util
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import unittest
from unittest import mock

from tests.temp_roots import REAL_TEMP_ROOT


ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "tests/fixtures/bus_scale_census/run-bus-scale-census-2026-10-09.py"
spec = importlib.util.spec_from_file_location("bus_scale_runner_controls_subject", RUNNER)
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


class FakeClock:
    def __init__(self):
        self.now = 0.0
        self.calls = 0

    def __call__(self):
        self.calls += 1
        if self.calls > 1000:
            raise AssertionError("collection spun without a bounded wait")
        return self.now


class FakeChild:
    """A deterministic child whose waits consume only its injected clock."""
    pid = 12345

    def __init__(self, clock, *, exit_at=None, exit_code=0,
                 interrupt_exit_delay=0.25, keyboard_interrupts=0):
        self.clock = clock
        self.exit_at = exit_at
        self.exit_code = exit_code
        self.interrupt_exit_delay = interrupt_exit_delay
        self.keyboard_interrupts = keyboard_interrupts
        self.returncode = None
        self.signals = []
        self.waits = []
        self.polls = 0

    def poll(self):
        self.polls += 1
        if self.polls > 1000:
            raise AssertionError("collection polled forever")
        if self.exit_at is not None and self.clock.now >= self.exit_at:
            self.returncode = self.exit_code
        return self.returncode

    def wait(self, timeout=None):
        if timeout is None or timeout < 0:
            raise AssertionError("owned child waits must be bounded")
        self.waits.append(timeout)
        if len(self.waits) > 1000:
            raise AssertionError("collection never reached its deadline")
        if self.poll() is not None:
            return self.returncode
        if self.keyboard_interrupts:
            self.keyboard_interrupts -= 1
            self.clock.now += min(timeout, 0.01)
            raise KeyboardInterrupt()
        end = self.clock.now + timeout
        if self.exit_at is not None and self.exit_at <= end:
            self.clock.now = self.exit_at
            self.returncode = self.exit_code
            return self.returncode
        self.clock.now = end
        raise subprocess.TimeoutExpired("owned-fake-child", timeout)

    def send_signal(self, number):
        self.signals.append(number)
        if self.interrupt_exit_delay is not None and self.poll() is None:
            self.exit_at = self.clock.now + self.interrupt_exit_delay
            self.exit_code = -signal.SIGINT


class BusScaleRunnerTests(unittest.TestCase):
    def api(self, name):
        value = getattr(runner, name, None)
        self.assertIsNotNone(value, "runner must implement " + name)
        return value

    def directory(self):
        temporary = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
        self.addCleanup(temporary.cleanup)
        return Path(temporary.name).resolve()

    def collect(self, child, *, cancelled=lambda: False):
        return self.api("collect_owned_child")(
            child, deadline_seconds=2.0, collection_seconds=1.0,
            clock=child.clock, cancelled=cancelled)

    def complete_record(self):
        return {"status": "complete", "exit_code": 0, "child_collected": True,
                "interrupted": False, "scratch_removed": True,
                "source_unchanged": True, "content_unchanged": True,
                "helper_sha256": "same", "helper_final_sha256": "same",
                "native": {"native_home_removed": True}}

    def test_content_population_includes_untracked_probe_and_all_control_sources(self):
        """Catches hashing only the runner or only Git-tracked contents."""
        declared = set(self.api("CONTENT_PATHS"))
        self.assertTrue({
            "tests/fixtures/bus_scale_census/run-bus-scale-census-2026-10-09.py",
            "tests/fixtures/bus_scale_census/probe-bus-scale-waiter-2026-10-09.py",
            "tests/harbor_bus_scale_commands.py", "tests/harbor_bus_scale_io.py",
            "tests/harbor_bus_scale_runner.py",
        } <= declared)

    def test_content_witness_reads_untracked_sources_without_git(self):
        """An ordinary new source file must be witnessed before its first commit."""
        root = self.directory()
        (root / "probe.py").write_bytes(b"probe v1\n")
        (root / "control.py").write_bytes(b"control v1\n")
        capture = self.api("content_witness")
        with mock.patch.object(runner.subprocess, "run", side_effect=AssertionError("no Git census")):
            actual = capture(root, ("probe.py", "control.py"))
        self.assertEqual({name: {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
                          for name, data in (("probe.py", b"probe v1\n"),
                                             ("control.py", b"control v1\n"))}, actual)

    def test_same_size_source_rewrite_with_restored_mtime_changes_witness(self):
        """Catches a metadata/status-only witness silently accepting changed code."""
        root = self.directory()
        path = root / "probe.py"
        path.write_bytes(b"old")
        capture = self.api("content_witness")
        before = capture(root, ("probe.py",))
        metadata = path.stat()
        path.write_bytes(b"new")
        os.utime(path, ns=(metadata.st_atime_ns, metadata.st_mtime_ns))
        after = capture(root, ("probe.py",))
        self.assertEqual(before["probe.py"]["bytes"], after["probe.py"]["bytes"])
        self.assertNotEqual(before["probe.py"]["sha256"], after["probe.py"]["sha256"])

    def test_missing_declared_source_is_not_silently_omitted(self):
        """Catches a deleted or never-captured helper shrinking the witness set."""
        capture = self.api("content_witness")
        with self.assertRaises((FileNotFoundError, RuntimeError, ValueError)):
            capture(self.directory(), ("missing-probe.py",))

    def test_owned_child_is_isolated_from_parent_tty_interrupt_group(self):
        """Prevents terminal Ctrl-C plus explicit forwarding hitting one child twice."""
        launch = self.api("launch_owned_child")
        fake = FakeChild(FakeClock())
        with mock.patch.object(runner.subprocess, "Popen", return_value=fake) as popen:
            actual = launch(["fixed-python", "fixed-helper.py"], cwd=ROOT,
                            env={"HOME": "private-fixture"}, stdout=object(), stderr=object())
        self.assertIs(fake, actual)
        self.assertEqual(1, popen.call_count)
        self.assertIs(True, popen.call_args.kwargs.get("start_new_session"))

    def test_natural_completion_preserves_exit_without_interrupt(self):
        """Collection must not turn a completed native RED exit into success."""
        clock = FakeClock()
        child = FakeChild(clock, exit_at=0.5, exit_code=1)
        result = self.collect(child)
        self.assertEqual("complete", result["status"])
        self.assertEqual(1, result["exit_code"])
        self.assertTrue(result["child_collected"])
        self.assertFalse(result["interrupted"])
        self.assertEqual([], child.signals)

    def test_deadline_interrupts_once_then_collects_same_child(self):
        """Catches a run budget merely printing progress forever."""
        clock = FakeClock()
        child = FakeChild(clock)
        result = self.collect(child)
        self.assertEqual("deadline", result["status"])
        self.assertTrue(result["child_collected"])
        self.assertTrue(result["interrupted"])
        self.assertEqual(-signal.SIGINT, result["exit_code"])
        self.assertEqual([signal.SIGINT], child.signals)
        self.assertLessEqual(clock.now, 3.0)

    def test_unresponsive_child_exhausts_collection_without_claiming_exit(self):
        """Catches unbounded communicate after the one owned interrupt."""
        clock = FakeClock()
        child = FakeChild(clock, interrupt_exit_delay=None)
        result = self.collect(child)
        self.assertEqual("uncollected", result["status"])
        self.assertFalse(result["child_collected"])
        self.assertIsNone(result["exit_code"])
        self.assertEqual([signal.SIGINT], child.signals)
        self.assertLessEqual(clock.now, 3.0)
        self.assertGreaterEqual(clock.now, 2.0)

    def test_cancel_requested_before_wait_forwards_once(self):
        """Repeated true cancellation requests must not repeatedly signal."""
        child = FakeChild(FakeClock())
        result = self.collect(child, cancelled=lambda: True)
        self.assertEqual("cancelled", result["status"])
        self.assertTrue(result["child_collected"])
        self.assertEqual([signal.SIGINT], child.signals)

    def test_keyboard_interrupt_during_wait_is_collected_not_escaped(self):
        """The outer terminal cancellation still owns collection and its receipt."""
        child = FakeChild(FakeClock(), keyboard_interrupts=1)
        result = self.collect(child)
        self.assertEqual("cancelled", result["status"])
        self.assertTrue(result["child_collected"])
        self.assertEqual([signal.SIGINT], child.signals)

    def test_repeated_interrupts_do_not_restart_collection_or_forward_twice(self):
        """Catches a second Ctrl-C discarding the original cleanup deadline."""
        clock = FakeClock()
        child = FakeChild(clock, keyboard_interrupts=2, interrupt_exit_delay=None)
        result = self.collect(child)
        self.assertEqual("uncollected", result["status"])
        self.assertFalse(result["child_collected"])
        self.assertEqual([signal.SIGINT], child.signals)
        self.assertLessEqual(clock.now, 1.1)

    def test_uncollected_child_preserves_scratch_and_reports_false(self):
        """Parent cleanup must not remove inputs still writable by its child."""
        scratch = self.directory() / "owned-scratch"
        scratch.mkdir()
        (scratch / "retained-input").write_bytes(b"owned")
        removed = self.api("cleanup_owned_scratch")(scratch, child_collected=False)
        self.assertFalse(removed)
        self.assertEqual(b"owned", (scratch / "retained-input").read_bytes())

    def test_collected_child_cleanup_reports_actual_removal(self):
        """A cleanup witness follows removal, rather than mere collection."""
        scratch = self.directory() / "owned-scratch"
        scratch.mkdir()
        self.assertTrue(self.api("cleanup_owned_scratch")(scratch, child_collected=True))
        self.assertFalse(scratch.exists())

    def test_cleanup_failure_cannot_return_removed_true(self):
        """Catches optimistic cleanup evidence when rmtree is refused."""
        scratch = self.directory() / "owned-scratch"
        scratch.mkdir()
        with mock.patch.object(runner.shutil, "rmtree", side_effect=OSError("fixture refusal")):
            try:
                removed = self.api("cleanup_owned_scratch")(scratch, child_collected=True)
            except OSError:
                removed = False
        self.assertFalse(removed)
        self.assertTrue(scratch.exists())

    def test_attempt_success_requires_independent_native_home_witness(self):
        """Removing parent scratch cannot stand in for the suite's extra HOME."""
        verdict = self.api("attempt_exit_code")
        for native in ({}, {"native_home_removed": False}, {"native_home_removed": None}):
            with self.subTest(native=native):
                record = self.complete_record()
                record["native"] = native
                self.assertNotEqual(0, verdict(record))

    def test_deadline_or_uncollected_status_never_qualifies_even_with_exit_zero(self):
        """A cancellation/deadline is not a successful native completion."""
        verdict = self.api("attempt_exit_code")
        for status in ("cancelled", "deadline", "uncollected"):
            with self.subTest(status=status):
                record = self.complete_record()
                record["status"] = status
                self.assertNotEqual(0, verdict(record))

    def test_changed_content_or_missing_collection_blocks_success(self):
        """Complete-looking native output cannot override ownership/source gaps."""
        verdict = self.api("attempt_exit_code")
        for field in ("child_collected", "scratch_removed", "source_unchanged", "content_unchanged"):
            with self.subTest(field=field):
                record = self.complete_record()
                record[field] = False
                self.assertNotEqual(0, verdict(record))

    def test_complete_attempt_preserves_native_red_exit(self):
        """A bankable failing control remains failing after cleanup succeeds."""
        verdict = self.api("attempt_exit_code")
        record = self.complete_record()
        self.assertEqual(0, verdict(record))
        record["exit_code"] = 1
        self.assertNotEqual(0, verdict(record))


if __name__ == "__main__":
    unittest.main()

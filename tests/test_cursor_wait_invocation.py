"""Live hook invocations end with their hold and never queue a duplicate."""
from __future__ import annotations

from tests.readiness import wait_event
import os
import io
from pathlib import Path
import signal
import threading
import time
import unittest
from unittest import mock

from floati.codex_wait_liveness import process_start_epoch
from floati.events import EventLog
from floati.cursor_wait import run_cursor_stop_wait
from tests.cursor_invocation_fixture import CursorInvocationFixture


class CursorWaitInvocationTests(unittest.TestCase):
    def setUp(self):
        self.f = CursorInvocationFixture(self)

    def test_cursor_hold_attributes_holder_pid_and_process_birth(self):
        job = self.f.start()
        self.f.wait_started()
        start = self.f.rows()[0]
        self.assertEqual(job["process"].pid, start.get("holder_pid"),
            "Cursor's hold must attribute the actual holder, not just its session")
        self.assertEqual(process_start_epoch(job["process"].pid), start.get("process_start_epoch"))

    def test_concurrent_duplicates_exit_while_the_original_keeps_holding(self):
        for harness, duplicate_count in (("Cursor", 2), ("Codex", 1)):
            with self.subTest(harness=harness):
                f = self.f if harness == "Cursor" else CursorInvocationFixture(self,
                    harness=harness, node="codex-seat", session="codex-live-chat")
                try:
                    original = f.start()
                    f.wait_started()
                    duplicates = [f.start() for _ in range(duplicate_count)]
                    f.wait_until(lambda: all(job["process"].poll() is not None for job in duplicates),
                        "duplicate hooks queued as concurrent holders", seconds=2)
                    self.assertIsNone(original["process"].poll())
                    self.assertEqual(["start"], [row["phase"] for row in f.rows()])
                    for job in duplicates:
                        self.assertEqual(0, job["process"].returncode)
                        job["stdout"].seek(0)
                        self.assertEqual("{}\n", job["stdout"].read())
                finally:
                    f.close()

    def test_three_sequential_mail_releases_leave_no_live_hook(self):
        for index in range(2):
            with self.subTest(turn=index):
                job = self.f.start()
                self.f.wait_started(index + 1)
                self.f.send("release-turn-" + str(index))
                self.f.wait_until(lambda: job["process"].poll() is not None,
                    "mail released the hold but left the hook process alive", seconds=3)
                self.assertEqual(0, job["process"].returncode)
                self.assertEqual(2 * (index + 1), len(self.f.rows()))
                self.assertEqual(0, sum(item["process"].poll() is None for item in self.f.jobs))
                EventLog(self.f.root).drain(self.f.node, acting_session_id=self.f.session)

    def test_duplicate_exits_without_queueing_while_original_flush_is_blocked(self):
        entered, release, duplicate_done = threading.Event(), threading.Event(), threading.Event()
        errors = []
        case = self
        class FlushGate(io.StringIO):
            def flush(self):
                entered.set()
                case.assertTrue(wait_event(release, 'cursor_wait_invocation: release synchronization', timeout=120), "test did not release original hook output")
                return super().flush()
        first_output, duplicate_output = FlushGate(), io.StringIO()
        def run(output, done=None):
            try:
                run_cursor_stop_wait(root=self.f.root.path, node=self.f.node, runtime=self.f.runtime,
                    deadline_seconds=30, poll_seconds=.05, hook_timeout_seconds=60,
                    payload={"conversation_id": self.f.session, "status": "error"},
                    stdout=output, stderr=io.StringIO())
            except BaseException as exc:
                errors.append(exc)
            finally:
                if done is not None:
                    done.set()
        original = threading.Thread(target=run, args=(first_output,))
        duplicate = threading.Thread(target=run, args=(duplicate_output, duplicate_done))
        with mock.patch("floati.cursor_wait.Path.cwd", return_value=self.f.workspace):
            try:
                original.start()
                self.assertTrue(wait_event(entered, 'cursor_wait_invocation: entered synchronization'), "original hook never reached its output flush")
                duplicate.start()
                self.assertTrue(wait_event(duplicate_done, 'cursor_wait_invocation: duplicate_done synchronization'), "duplicate queued behind the original's publication guard")
                self.assertEqual("{}\n", duplicate_output.getvalue())
            finally:
                release.set()
                original.join(5)
                if duplicate.ident is not None:
                    duplicate.join(5)
        self.assertFalse(original.is_alive())
        self.assertFalse(duplicate.is_alive())
        self.assertEqual([], errors)

    def test_host_exit_retires_its_orphaned_hook_with_native_exit_receipt(self):
        host = self.f.start(host=True)
        self.f.wait_started()
        child_pid = int(Path(str(host["control"]) + ".pid").read_text())
        child_birth = process_start_epoch(child_pid)
        def cleanup_child():
            if process_start_epoch(child_pid) == child_birth:
                try:
                    os.kill(child_pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
        self.addCleanup(cleanup_child)
        host["control"].touch()
        host["process"].wait(timeout=3)
        self.f.wait_until(lambda: len(self.f.rows()) == 2,
            "host exit left an orphan holding the seat", seconds=3)
        self.f.wait_until(lambda: process_start_epoch(child_pid) != child_birth,
            "host-exit receipt was written but hook process remained alive", seconds=3)
        self.assertEqual(["start", "exit"], [row["phase"] for row in self.f.rows()])


if __name__ == "__main__":
    unittest.main()

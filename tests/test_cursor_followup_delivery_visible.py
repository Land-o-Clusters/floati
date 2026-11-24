"""STALL-VISIBLE second RED: actual closed Cursor stdout at native re-arm."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import fcntl
import io
import json
import os
import unittest
from unittest import mock

import tests  # Pin HOME before importing any product default.

from floati import cursor_wait
from floati.codex_wait_contract import CodexWaitConsentLedger, CodexWaitSessionLedger
from floati.registry import Registry
from floati.waiter_consent import arm_waiter_consent
from tests import test_f2_readiness_hold as readiness
from tests.waiter_timing import scripted_readiness


DEADLINE = 1500
CONSENT_DEADLINE = 1700
HOOK_TIMEOUT = 1800


class _ObservedPipe:
    """Observe genuine OS write/flush failures; never manufacture one."""
    def __init__(self, stream):
        self.stream = stream
        self.writes = 0
        self.flushes = 0
        self.failure = None
        self.failure_phase = None

    def write(self, value):
        self.writes += 1
        try:
            return self.stream.write(value)
        except BrokenPipeError as error:
            self.failure, self.failure_phase = error, "write"
            raise

    def flush(self):
        self.flushes += 1
        try:
            return self.stream.flush()
        except BrokenPipeError as error:
            self.failure, self.failure_phase = error, "flush"
            raise

    def close(self):
        try:
            self.stream.close()
        except BrokenPipeError:
            pass


class CursorFollowupDeliveryVisibleTests(unittest.TestCase):
    def setUp(self):
        self.f = readiness.NativeStopHoldReadinessTests("runTest")
        self.addCleanup(self.f.doCleanups)
        self.f.setUp()
        self.f.fixture("Cursor")
        self.root, self.node = self.f.root, self.f.node
        arm_waiter_consent(self.root.path, self.f.workspace, self.node,
            "Cursor", HOOK_TIMEOUT, CONSENT_DEADLINE)
        self.consent = CodexWaitConsentLedger(self.root).require_armed(self.f.binding)
        self.assertEqual(CONSENT_DEADLINE, self.consent["wait_deadline_seconds"])
        self.assertEqual(HOOK_TIMEOUT, self.consent["hook_timeout_seconds"])
        self.assertEqual("cursor", Registry(self.root).require_active(self.node)["role"].casefold())
        CodexWaitSessionLedger(self.root).arm(self.f.binding, self.consent,
            self.f.session, idempotency_key="closed-stdout-native-claim")
        self.elapsed, self.sleeps = 0.0, []
        self.epoch = datetime.now(timezone.utc)
        self.errors = io.StringIO()
        self.at_sleep = None
        self.journal = cursor_wait._journal_path(self.root.path, self.node)

    def journal_rows(self):
        if not self.journal.exists():
            return []
        return [json.loads(line) for line in self.journal.read_text().splitlines()]

    def run_deadline(self, output, *, close_reader=None):
        def sleep(seconds):
            self.sleeps.append(seconds)
            self.at_sleep = self.f.hold_rows()
            self.assertEqual(["start"], [row["phase"] for row in self.at_sleep])
            self.assertEqual(DEADLINE, self.at_sleep[0]["deadline_seconds"])
            self.assertEqual(self.consent["id"], self.at_sleep[0]["consent_receipt_id"])
            if close_reader is not None:
                self.assertEqual(0, output.writes, "pipe closed after publication had already begun")
                close_reader()
            self.elapsed = float(DEADLINE)

        with mock.patch("floati.cursor_wait.Path.cwd", return_value=self.f.workspace):
            return cursor_wait.run_cursor_stop_wait(root=self.root.path, node=self.node,
                runtime=self.f.runtime, deadline_seconds=DEADLINE, poll_seconds=1,
                hook_timeout_seconds=HOOK_TIMEOUT,
                payload={"status": "completed", "conversation_id": self.f.session,
                         "loop_count": 0, "cwd": str(self.f.workspace)},
                stdout=output, stderr=self.errors, clock=lambda: self.elapsed,
                sleep=sleep, wait_readable=scripted_readiness(sleep, interval=1),
                now_utc=lambda: (self.epoch + timedelta(seconds=self.elapsed)).isoformat().replace("+00:00", "Z"),
                last_seen_root=self.f.workspace)

    def assert_native_deadline(self):
        self.assertEqual([1.0], self.sleeps)
        self.assertEqual(float(DEADLINE), self.elapsed)
        rows = self.f.hold_rows()
        self.assertEqual(["start", "exit"], [row["phase"] for row in rows])
        self.assertEqual(self.at_sleep[0]["invocation_id"], rows[-1]["invocation_id"])
        self.assertEqual(self.f.session, rows[0]["acting_session_id"])
        lock = self.root.path / "receipts/wake-coordination" / self.node / "lane.lock"
        with lock.open("r+b") as stream:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

    def closed_pipe_at_deadline(self, phase):
        reader, writer = os.pipe()
        reader_open = [True]
        def close_reader():
            os.close(reader)
            reader_open[0] = False
        self.addCleanup(lambda: close_reader() if reader_open[0] else None)
        if phase == "write":
            stream = io.TextIOWrapper(os.fdopen(writer, "wb", buffering=0),
                encoding="utf-8", write_through=True)
        else:
            stream = os.fdopen(writer, "w", buffering=8192, encoding="utf-8")
        output = _ObservedPipe(stream)
        self.addCleanup(output.close)
        try:
            self.run_deadline(output, close_reader=close_reader)
        except BrokenPipeError:
            pass  # The passing reproduction separately verifies the real error.
        self.assertFalse(reader_open[0])
        self.assertIsInstance(output.failure, BrokenPipeError)
        self.assertEqual(phase, output.failure_phase)
        self.assertEqual(1, output.writes)
        self.assertEqual(0 if phase == "write" else 1, output.flushes)
        self.assert_native_deadline()
        self.assertFalse(any(row.get("publication") == "flushed" for row in self.journal_rows()))
        return output

    @staticmethod
    def machine_code(value):
        """Existing generic code fields, without guessing a future code name."""
        if isinstance(value, dict):
            if any(isinstance(value.get(key), str) and value[key]
                   for key in ("code", "reason_code")):
                return True
            return any(CursorFollowupDeliveryVisibleTests.machine_code(child) for child in value.values())
        if isinstance(value, list):
            return any(CursorFollowupDeliveryVisibleTests.machine_code(child) for child in value)
        return False

    def durable_failure(self, phase):
        self.closed_pipe_at_deadline(phase)
        # Read the existing native durable surfaces, not a proposed product
        # API or a guessed future receipt kind/error-code spelling.
        candidates = self.journal_rows() + self.f.hold_rows()
        measured = [row for row in candidates if self.machine_code(row)
                    and "BrokenPipeError" in json.dumps(row, sort_keys=True)]
        self.assertTrue(measured,
            "the real re-arm delivery failure has no typed durable evidence in the native Cursor journal/StopHold surfaces")

    def test_actual_closed_pipe_fails_during_rearm_write(self):
        self.closed_pipe_at_deadline("write")

    def test_actual_closed_pipe_fails_during_buffered_rearm_flush(self):
        self.closed_pipe_at_deadline("flush")

    def test_writable_stdout_flushes_one_native_deadline_rearm(self):
        output = io.StringIO()
        self.assertEqual(0, self.run_deadline(output))
        self.assert_native_deadline()
        self.assertEqual(1, len(output.getvalue().splitlines()))
        self.assertIn("25 min", json.loads(output.getvalue())["followup_message"])
        row = self.journal_rows()[-1]
        self.assertEqual("followup_rearm", row["event"])
        self.assertEqual("flushed", row["publication"])
        self.assertEqual(DEADLINE, row["waited_seconds"])

    def test_closed_rearm_write_failure_has_typed_durable_evidence(self):
        self.durable_failure("write")

    def test_closed_rearm_flush_failure_has_typed_durable_evidence(self):
        self.durable_failure("flush")

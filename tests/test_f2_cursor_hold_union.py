"""Real Cursor hold testimony shares claimant authority and publication guards."""

from __future__ import annotations

import fcntl
import io
import json
import os
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from floati import cursor_wait, jsonl
from floati.codex_wait_contract import CodexWaitSessionLedger
from floati.errors import ProtocolRefusal
from floati.events import EventLog
from floati.registry import Registry
from floati.wake_control import WakeController
from floati.records import WAKE_HOLD_KINDS
from tests import test_f2_readiness_hold as readiness
from tests.waiter_timing import scripted_readiness


class CursorHoldUnionTests(unittest.TestCase):
    def setUp(self):
        self.f = readiness.NativeStopHoldReadinessTests("runTest")
        self.addCleanup(self.f.doCleanups)
        self.f.setUp()
        self.f.fixture("Cursor")
        self.elapsed = 0.0
        self.epoch = datetime.now(timezone.utc)
        self.exit_locks = []
        self.output_rows = []
        self.journal = self.f.root.path / "state/cursor-wait" / self.f.node / "journal.jsonl"
        case = self
        class ObservedOutput(io.StringIO):
            def write(stream, value):
                case.output_rows.append(case.f.hold_rows())
                return super().write(value)
        self.output = ObservedOutput()

    def rows(self):
        return [json.loads(line) for line in self.journal.read_text().splitlines()] if self.journal.exists() else []

    def arm(self, session):
        return CodexWaitSessionLedger(self.f.root).arm(self.f.binding, self.f.consent,
            session, idempotency_key="union-arm-" + session, take_over=True)

    def run_waiter(self, *, on_sleep=None, stdout=None, workspace=None, **overrides):
        called = False
        def sleep(seconds):
            nonlocal called
            if not called and on_sleep is not None:
                called = True
                on_sleep()
            self.elapsed += seconds
        encode = jsonl._encode_record
        def observe_exit(row, *args, **kwargs):
            if row.get("kind") == "stop_hold_receipt" and row.get("phase") == "exit":
                path = self.f.root.path / "receipts/wake-coordination" / self.f.node / "lane.lock"
                descriptor = os.open(path, os.O_RDWR)
                try:
                    try:
                        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        self.exit_locks.append("held")
                    else:
                        self.exit_locks.append("free")
                        fcntl.flock(descriptor, fcntl.LOCK_UN)
                finally:
                    os.close(descriptor)
            return encode(row, *args, **kwargs)
        arguments = dict(root=self.f.root.path, node=self.f.node, runtime=self.f.runtime,
            deadline_seconds=2, poll_seconds=1, hook_timeout_seconds=1800,
            payload={"status": "completed", "conversation_id": self.f.session,
                     "cwd": str(self.f.base / "payload-is-not-authority")},
            last_seen_root=self.f.base / "diagnostic-is-not-authority",
            stdout=self.output if stdout is None else stdout, stderr=self.f.stderr,
            clock=lambda: self.elapsed, sleep=sleep,
            wait_readable=scripted_readiness(sleep, interval=1),
            now_utc=lambda: (self.epoch + timedelta(seconds=self.elapsed)).isoformat().replace("+00:00", "Z"),
            ledger_mtime=lambda: self.f.events.path.stat().st_size if self.f.events.path.exists() else 0)
        arguments.update(overrides)
        with mock.patch("floati.cursor_wait.Path.cwd", return_value=workspace or self.f.workspace),\
             mock.patch("floati.jsonl._encode_record", side_effect=observe_exit):
            self.assertEqual(0, cursor_wait.run_cursor_stop_wait(**arguments))
        return json.loads(self.output.getvalue())

    def assert_closed(self):
        rows = self.f.hold_rows()
        self.assertEqual(["start", "exit"], [row["phase"] for row in rows])
        self.assertEqual(rows[0]["invocation_id"], rows[1]["invocation_id"])
        self.assertEqual(["held"], self.exit_locks,
                         "exit evidence must use a genuinely held node guard")
        self.assertTrue(self.output_rows)
        self.assertTrue(all([row["phase"] for row in captured] == ["start", "exit"]
                            for captured in self.output_rows))
        self.assertNotIn("stop_hold_", self.f.stderr.getvalue())

    def test_actual_workspace_hold_is_ready_then_closed_before_guarded_mail_output(self):
        def send_during_hold():
            rows = self.f.hold_rows()
            self.assertEqual(["start"], [row["phase"] for row in rows])
            self.assertEqual(str(self.f.workspace), rows[0]["workspace"])
            self.assertEqual(self.f.session, rows[0]["acting_session_id"])
            self.assertEqual("", self.output.getvalue())
            self.f.assert_stop_hook_ready(self.f.send("union-during-hold"))
        body = self.run_waiter(on_sleep=send_during_hold)
        self.assertIn("unread mail", body["followup_message"])
        self.assert_closed()
        self.assertEqual("flushed", self.rows()[-1]["publication"])

    def test_takeover_closes_original_hold_and_preserves_empty_supersession(self):
        def takeover():
            self.assertEqual(["start"], [row["phase"] for row in self.f.hold_rows()])
            self.arm("cursor-successor")
            self.f.assert_no_stop_hook_witness(self.f.send("union-after-takeover"))
        self.assertEqual({}, self.run_waiter(on_sleep=takeover))
        self.assert_closed()
        row = self.rows()[-1]
        self.assertEqual("superseded_session", row["claim_state"])
        self.assertEqual("cursor-successor", row["current_session_id"])
        self.assertNotIn("publication", row)
        deliveries = jsonl.read_records_snapshot(self.f.root,
            Path("receipts/deliveries") / (self.f.node + ".jsonl"),
            allowed_kinds=set(WAKE_HOLD_KINDS))
        self.assertEqual([], deliveries)

    def test_partial_guard_failure_closes_under_real_node_lock_before_diagnostic(self):
        active = Registry.require_active
        fault = [False]
        def fail_once(registry, node):
            if self.elapsed and not fault[0]:
                fault[0] = True
                raise ProtocolRefusal("union_registry_unavailable", "scratch transient registry read",
                    remedy="restore the scratch registry")
            return active(registry, node)
        with mock.patch.object(Registry, "require_active", fail_once):
            body = self.run_waiter()
        self.assertIn("union_registry_unavailable", body["followup_message"])
        self.assert_closed()

    def test_withdrawal_after_hold_closes_before_configuration_diagnostic(self):
        from floati.seat_waiter_lifecycle import retire_waiter
        body = self.run_waiter(on_sleep=lambda: retire_waiter(self.f.root, self.f.node,
            self.f.workspace, "union-withdraw"))
        self.assertIn("cursor_wait_workspace_unbound", body["followup_message"])
        self.assert_closed()
        self.assertEqual("followup_participation_refused", self.rows()[-1]["event"])

    def test_unmapped_actual_workspace_cannot_record_hold_from_payload_coordinates(self):
        other = self.f.base / "unmapped"
        other.mkdir()
        body = self.run_waiter(workspace=other,
            payload={"status": "completed", "conversation_id": self.f.session, "cwd": str(self.f.workspace)},
            last_seen_root=self.f.workspace)
        self.assertIn("cursor_wait_workspace_unbound", body["followup_message"])
        self.assertEqual([], self.f.hold_rows())

    def test_paused_former_claimant_has_no_new_hold(self):
        self.arm(self.f.session)
        WakeController(self.f.root).pause(self.f.node, self.f.session, idempotency_key="union-paused")
        self.arm("cursor-successor")
        self.assertEqual({}, self.run_waiter())
        self.assertEqual("superseded_session", self.rows()[-1]["claim_state"])
        self.assertEqual([], self.f.hold_rows())

    def test_immediate_error_and_abort_continuations_have_no_empty_hold(self):
        for status in ("error", "aborted"):
            with self.subTest(status=status):
                self.output.seek(0)
                self.output.truncate()
                body = self.run_waiter(payload={"status": status, "conversation_id": self.f.session})
                self.assertIn("followup_message", body)
                self.assertEqual([], self.f.hold_rows())

    def test_immediate_mail_has_no_empty_hold(self):
        self.f.send("union-mail-at-entry")
        self.assertIn("unread mail", self.run_waiter()["followup_message"])
        self.assertEqual([], self.f.hold_rows())

    def test_hold_budget_excludes_time_already_spent_checking_initial_inbox(self):
        from floati.cursor_presentations import CursorPresentationLedger
        select = CursorPresentationLedger._select_waiter
        reads = 0
        def slow_read(plane, *args, **kwargs):
            nonlocal reads
            result = select(plane, *args, **kwargs)
            reads += 1
            self.elapsed += 0.75
            return result
        with mock.patch.object(CursorPresentationLedger, "_select_waiter", slow_read):
            self.run_waiter()
        self.assertGreater(reads, 0, "initial inbox selection never exercised the delay observer")
        self.assert_closed()
        self.assertEqual(1.25, self.f.hold_rows()[0]["deadline_seconds"])

    def test_failed_flush_closes_the_original_hold_without_publication_marker(self):
        class FailedFlush(io.StringIO):
            def flush(stream):
                raise OSError("scratch failed flush")
        with self.assertRaises(OSError):
            self.run_waiter(stdout=FailedFlush())
        self.assertEqual(["start", "exit"], [row["phase"] for row in self.f.hold_rows()])
        self.assertEqual(["held"], self.exit_locks)
        rows = self.rows()
        self.assertEqual(1, len(rows))
        self.assertEqual("failed", rows[0]["publication"])
        self.assertEqual("flush", rows[0]["failure_phase"])
        self.assertFalse(any(row.get("publication") == "flushed" for row in rows))
        self.arm("cursor-successor")  # A failed output must release the old guard.

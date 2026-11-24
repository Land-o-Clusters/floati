"""Cursor claimant fixture controls; real claims, delivery and node flocks."""
from __future__ import annotations

import io
import json
from pathlib import Path
import unittest
from unittest import mock

from floati import cursor_wait
from floati.codex_wait_contract import CodexWaitConsentLedger
from floati.events import EventLog
from floati.jsonl import read_records_snapshot
from floati.wake_control import WakeController
from floati.records import WAKE_HOLD_KINDS
from tests import test_codex_wait as baseline
from tests import test_f2_superseded_waiter as races
from tests.waiter_timing import scripted_readiness


A = races.A
B = races.B


class CursorClaimantTests(unittest.TestCase):
    def setUp(self):
        # Module imports and composition avoid discovering the borrowed cases.
        self.f = baseline.CodexWaitRuntimeTests()
        self.addCleanup(self.f.doCleanups)
        self.f.setUp()
        self.node = self.f.participant.binding.node_id
        self.journal = self.f.root.path / "state/cursor-wait" / self.node / "journal.jsonl"

    def rows(self):
        if not self.journal.exists():
            return []
        return [json.loads(line) for line in self.journal.read_text().splitlines()]

    def deliveries(self):
        return read_records_snapshot(self.f.root,
            Path("receipts/deliveries") / (self.node + ".jsonl"),
            allowed_kinds=set(WAKE_HOLD_KINDS))

    def run_waiter(self, *, session=A, status="completed", stdout=None,
                   sleep=None, workspace=None, **overrides):
        output = io.StringIO() if stdout is None else stdout
        errors = io.StringIO()
        clock = [0.0]
        def tick(seconds):
            clock[0] += seconds
            if sleep is not None and clock[0] == seconds:
                sleep()
        arguments = dict(root=self.f.root.path, node=self.node, runtime=self.f.base,
            deadline_seconds=2, poll_seconds=1, hook_timeout_seconds=10,
            payload={"status": status, "conversation_id": session, "loop_count": 0},
            stdout=output, stderr=errors, last_seen_root=self.f.workspace,
            clock=lambda: clock[0], sleep=tick, ledger_mtime=lambda: 0,
            wait_readable=scripted_readiness(tick, interval=1))
        arguments.update(overrides)
        with mock.patch("floati.cursor_wait.Path.cwd", return_value=workspace or self.f.workspace):
            self.assertEqual(0, cursor_wait.run_cursor_stop_wait(**arguments))
        return json.loads(output.getvalue()), errors.getvalue()

    def assert_declined(self, body, state):
        self.assertEqual({}, body)
        row = self.rows()[-1]
        self.assertEqual("superseded_session", row["reason_code"])
        self.assertEqual(state, row["claim_state"])
        self.assertEqual(A, row["calling_session_id"])
        self.assertEqual(B, row["current_session_id"])
        self.assertNotIn("publication", row)
        self.assertEqual([], self.deliveries())

    def assert_diagnostic(self, body, code):
        message = body.get("followup_message", "")
        self.assertIn(code, message)
        self.assertIn("remedy", message.lower())
        self.assertNotIn("inbox --root", message)
        self.assertNotIn("Drain it now", message)
        row = self.rows()[-1]
        self.assertEqual("followup_participation_refused", row["event"])
        self.assertEqual(code, row["reason_code"])
        self.assertTrue(row["remedy"])
        self.assertEqual([], self.deliveries())

    def test_former_positive_claimant_declines_before_delivery_for_every_exit(self):
        self.f.arm_session(A)
        self.f.arm_session(B)
        self.f.send("cursor-stale-mail")
        for status in ("completed", "error", "aborted"):
            with self.subTest(status=status):
                body, _ = self.run_waiter(status=status)
                self.assert_declined(body, "superseded_session")

    def test_never_claimant_preserves_classifier_and_names_both_sessions(self):
        self.f.arm_session(B)
        self.f.send("cursor-never-mail")
        body, _ = self.run_waiter()
        self.assert_declined(body, "not_claimant")

    def test_paused_former_claimant_preserves_supersession_and_both_ids(self):
        self.f.arm_session(A)
        self.f.arm_session(B)
        WakeController(self.f.root).pause(self.node, A, idempotency_key="cursor-paused-former")
        self.f.send("cursor-paused-former-mail")
        body, _ = self.run_waiter()
        self.assert_declined(body, "superseded_session")

    def test_paused_never_claimant_preserves_classifier_and_both_ids(self):
        self.f.arm_session(B)
        WakeController(self.f.root).pause(self.node, A, idempotency_key="cursor-paused-never")
        self.f.send("cursor-paused-never-mail")
        body, _ = self.run_waiter()
        self.assert_declined(body, "not_claimant")

    def test_two_hook_conversations_only_initial_claimant_publishes(self):
        body, _ = self.run_waiter(session=B)
        self.assertIn("armed", body["followup_message"])
        self.f.send("cursor-second-hook")
        body, _ = self.run_waiter()
        self.assert_declined(body, "not_claimant")

    def test_takeover_during_unchanged_ledger_sleep_stops_rearm(self):
        self.f.arm_session(A)
        body, _ = self.run_waiter(sleep=lambda: self.f.arm_session(B))
        self.assert_declined(body, "superseded_session")

    def test_same_session_remains_authorized_across_hook_calls(self):
        self.f.arm_session(A)
        self.run_waiter()
        self.f.send("cursor-same-session")
        body, _ = self.run_waiter()
        self.assertIn("--session " + A, body["followup_message"])
        self.assertEqual(1, len(self.deliveries()))
        self.assertEqual("flushed", self.rows()[-1].get("publication"))

    def test_actual_b_arm_blocks_until_mail_flush_and_later_a_is_empty(self):
        self.f.arm_session(A)
        self.f.send("cursor-flush-race")
        race = races._TakeoverRace(self)
        self.addCleanup(race.collect)
        output = races._FlushGate(race)
        with mock.patch("floati.jsonl.fcntl.flock", side_effect=race.observed_flock):
            body, _ = self.run_waiter(stdout=output)
            race.collect()
        self.assertIn("Drain it now", body["followup_message"])
        self.assertEqual("blocked", race.first_outcome)
        self.assertLess(race.order.index("a_flushed"), race.order.index("b_committed"))
        before = self.deliveries()
        body, _ = self.run_waiter()
        self.assertEqual({}, body)
        self.assertEqual(before, self.deliveries())
        self.assertEqual("superseded_session", self.rows()[-1]["reason_code"])

    def test_delivery_peek_and_flush_share_the_same_guard(self):
        self.f.arm_session(A)
        self.f.send("cursor-peek-race")
        race = races._TakeoverRace(self)
        self.addCleanup(race.collect)
        real_present = EventLog._write_delivery_receipt
        def observed(log, *args, **kwargs):
            race.launch()
            return real_present(log, *args, **kwargs)
        with mock.patch("floati.jsonl.fcntl.flock", side_effect=race.observed_flock),\
             mock.patch.object(EventLog, "_write_delivery_receipt", observed):
            body, _ = self.run_waiter(stdout=races._FlushGate(race))
            race.collect()
        self.assertIn("Drain it now", body["followup_message"])
        self.assertEqual("blocked", race.first_outcome)
        self.assertLess(race.order.index("a_flushed"), race.order.index("b_committed"))
        self.assertEqual(1, len(self.deliveries()))

    def assert_status_flush_guard(self, status):
        self.f.arm_session(A)
        race = races._TakeoverRace(self)
        self.addCleanup(race.collect)
        with mock.patch("floati.jsonl.fcntl.flock", side_effect=race.observed_flock):
            body, _ = self.run_waiter(status=status, stdout=races._FlushGate(race))
            race.collect()
        self.assertIn("followup_message", body)
        self.assertEqual("blocked", race.first_outcome)
        self.assertLess(race.order.index("a_flushed"), race.order.index("b_committed"))

    def test_actual_b_arm_blocks_through_error_flush(self):
        cases = ("error", "aborted", "completed")
        for index, status in enumerate(cases):
            with self.subTest(status=status):
                if index:
                    self.doCleanups()
                    self.setUp()
                self.assert_status_flush_guard(status)

    def test_unmapped_workspace_diagnostic_without_delivery(self):
        self.f.write_map([])
        body, _ = self.run_waiter()
        self.assert_diagnostic(body, "cursor_wait_workspace_unbound")

    def test_missing_consent_diagnostic_without_delivery(self):
        self.f.root.resolve_relative(CodexWaitConsentLedger._relative(self.node)).unlink()
        body, _ = self.run_waiter()
        self.assert_diagnostic(body, "codex_wait_consent_missing")

    def test_missing_and_invalid_native_ids_diagnose_without_fallback_claim(self):
        for identity in (None, 17, "bad\nidentity", "global"):
            with self.subTest(identity=identity):
                body, _ = self.run_waiter(session=identity)
                self.assert_diagnostic(body, "wake_session_invalid")
                self.assertNotIn("publication", self.rows()[-1])
        self.assertFalse(self.f.root.resolve_relative(
            Path("receipts/codex-wait-session") / (self.node + ".jsonl")).exists())

    def test_map_withdrawal_is_rechecked_without_event_change(self):
        body, _ = self.run_waiter(sleep=lambda: self.f.write_map([]))
        self.assert_diagnostic(body, "cursor_wait_workspace_unbound")

    def test_consent_withdrawal_is_rechecked_without_event_change(self):
        from floati.seat_waiter_lifecycle import retire_waiter
        def withdraw():
            retire_waiter(self.f.root, self.node, self.f.workspace, "cursor-withdraw")
            self.f.write_map([{"workspace": str(self.f.workspace), "node_id": self.node}])
        body, _ = self.run_waiter(sleep=withdraw)
        self.assert_diagnostic(body, "codex_wait_consent_missing")

    def test_pause_blocks_mail_and_is_refreshed_during_unchanged_ledger_sleep(self):
        def pause():
            WakeController(self.f.root).pause(self.node, A, idempotency_key="cursor-pause")
        body, _ = self.run_waiter(sleep=pause)
        self.assertEqual({}, body)
        self.assertEqual("paused", self.rows()[-1]["reason_code"])
        self.assertNotIn("publication", self.rows()[-1])
        self.f.send("cursor-paused-mail")
        body, _ = self.run_waiter()
        self.assertEqual({}, body)
        self.assertEqual([], self.deliveries())

    def test_actual_workspace_is_used_instead_of_payload_or_runtime(self):
        other = self.f.base / "unmapped"
        other.mkdir()
        body, _ = self.run_waiter(workspace=other, last_seen_root=self.f.workspace,
            payload={"status": "error", "conversation_id": A, "cwd": str(self.f.workspace)})
        self.assert_diagnostic(body, "cursor_wait_workspace_unbound")

    def test_diagnostic_last_seen_root_does_not_redirect_actual_workspace(self):
        body, _ = self.run_waiter(last_seen_root=self.f.base / "diagnostic-only")
        self.assertIn("Reply exactly: armed", body["followup_message"])
        self.assertEqual(str(self.f.base / "diagnostic-only"), self.rows()[-1]["last_seen_root"])

    def test_mapping_to_another_node_does_not_rederive_authority(self):
        from floati.registry import Registry
        Registry(self.f.root).register("other-cursor", "cursor")
        self.f.write_map([{"workspace": str(self.f.workspace), "node_id": "other-cursor"}])
        body, _ = self.run_waiter()
        self.assert_diagnostic(body, "cursor_wait_workspace_mismatch")

    def test_changed_binding_workspace_cannot_implicitly_claim_again(self):
        nested = self.f.workspace / "nested"
        def remap():
            self.f.write_map([{"workspace": str(nested), "node_id": self.node}])
        body, _ = self.run_waiter(workspace=nested, sleep=remap)
        self.assert_diagnostic(body, "cursor_wait_workspace_mismatch")
        claims = read_records_snapshot(self.f.root,
            Path("receipts/codex-wait-session") / (self.node + ".jsonl"),
            allowed_kinds={"codex_wait_session_receipt"})
        self.assertEqual([str(self.f.workspace)], [row["workspace"] for row in claims])

    def test_unreadable_event_ledger_retains_its_typed_reason_and_remedy(self):
        (self.f.root.path / "events.jsonl").write_text("malformed\n")
        body, _ = self.run_waiter()
        self.assertIn("malformed_json", body["followup_message"])
        row = self.rows()[-1]
        self.assertEqual("followup_inbox_unreadable", row["event"])
        self.assertEqual("malformed_json", row["reason_code"])
        self.assertIsInstance(row["remedy"], str)
        self.assertEqual([], self.deliveries())

    def test_publication_stamp_is_after_flush_and_no_journal_row_precedes_it(self):
        seen = []
        stamps = iter(("2026-10-03T00:00:00Z", "2026-10-03T00:00:01Z"))
        class ObservedFlush(io.StringIO):
            def flush(stream):
                seen.extend(self.rows())
                return super().flush()
        self.run_waiter(status="error", stdout=ObservedFlush(), now_utc=lambda: next(stamps))
        self.assertEqual([], seen)
        row = self.rows()[-1]
        self.assertEqual("flushed", row["publication"])
        self.assertEqual("2026-10-03T00:00:01Z", row["published_at"])
        self.assertEqual("2026-10-03T00:00:00Z", row["ts"])

    def test_failed_write_or_flush_cannot_journal_publication_and_releases_guard(self):
        for operation in ("write", "flush"):
            with self.subTest(operation=operation):
                class FailedOutput(io.StringIO):
                    def write(stream, value):
                        if operation == "write":
                            raise OSError("fixture write failed")
                        return super().write(value)
                    def flush(stream):
                        if operation == "flush":
                            raise OSError("fixture flush failed")
                        return super().flush()
                with self.assertRaises(OSError):
                    self.run_waiter(status="error", stdout=FailedOutput())
                self.assertEqual([], self.rows())
        self.f.arm_session(B)

    def test_journal_failure_after_flush_keeps_body_and_stderr_diagnostic(self):
        (self.f.root.path / "state").mkdir(exist_ok=True)
        (self.f.root.path / "state/cursor-wait").write_text("blocker")
        body, errors = self.run_waiter(status="error")
        self.assertIn("Drain it now", body["followup_message"])
        self.assertIn("could not write", errors)

    def test_missing_root_diagnostic_has_typed_stderr_and_never_creates_root(self):
        missing = self.f.base / "absent"
        body, errors = self.run_waiter(root=missing)
        self.assertIn("direct_home_missing", body["followup_message"])
        self.assertIn("direct_home_missing", errors)
        self.assertIn("reason_code", errors)
        self.assertIn("no durable receipt", errors)
        self.assertFalse(missing.exists())


if __name__ == "__main__":
    unittest.main()

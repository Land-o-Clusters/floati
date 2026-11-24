"""Native conversational wake controls; real scratch envelopes, never a count stub."""
from __future__ import annotations

import io
import json
from pathlib import Path
import unittest
from unittest import mock

from tests import test_f2_cursor_claimant as claimant


class CursorUnackedLoopTests(unittest.TestCase):
    def setUp(self):
        self.c = claimant.CursorClaimantTests("runTest")
        self.addCleanup(self.c.doCleanups)
        self.c.setUp()
        self.c.f.arm_session(claimant.A)

    def assert_no_ack(self):
        path = self.c.f.root.resolve_relative(
            Path("receipts/acks") / (self.c.node + ".jsonl"))
        self.assertFalse(path.exists(), "holding mail must not forge an acknowledgment")

    def first_publication(self):
        old = self.c.f.send("unacked-loop-old")
        body, error = self.c.run_waiter()
        self.assertIn("Drain it now", body["followup_message"], error)
        deliveries = self.c.deliveries()
        self.assertEqual(1, len(deliveries))
        self.assertEqual([old["id"]], deliveries[0]["item_ids"])
        self.assert_no_ack()
        return old, deliveries

    def test_already_flushed_unacked_mail_holds_without_an_immediate_repeat(self):
        old, before = self.first_publication()
        output, slept = io.StringIO(), []

        def during_hold():
            slept.append(True)
            self.assertEqual("", output.getvalue())
            self.assertEqual(before, self.c.deliveries())
            self.assert_no_ack()

        body, error = self.c.run_waiter(stdout=output, sleep=during_hold)
        self.assertTrue(slept, "the same unACKed envelope immediately woke this conversation again")
        self.assertNotIn("Drain it now", body["followup_message"], error)
        self.assertIn("held_unacked", body["followup_message"])
        self.assertIn(old["id"], body["followup_message"])
        self.assertEqual("followup_rearm", self.c.rows()[-1]["event"])
        self.assertEqual(2, self.c.rows()[-1]["waited_seconds"])
        self.assertEqual(before, self.c.deliveries())
        self.assert_no_ack()

    def test_new_mail_during_that_hold_is_selected_by_the_next_poll(self):
        old, before = self.first_publication()
        output, fresh = io.StringIO(), []

        def append_during_hold():
            self.assertEqual("", output.getvalue())
            self.assertEqual(before, self.c.deliveries())
            self.assert_no_ack()
            fresh.append(self.c.f.send("unacked-loop-new"))

        body, error = self.c.run_waiter(stdout=output, sleep=append_during_hold,
            ledger_mtime=lambda: self.c.f.root.resolve_relative("events.jsonl").stat().st_size)
        self.assertEqual(1, len(fresh), "old mail prevented the native waiter from holding for new mail")
        self.assertIn(fresh[0]["id"], body["followup_message"], error)
        self.assertNotIn(old["id"], body["followup_message"])
        self.assertEqual("followup_mail", self.c.rows()[-1]["event"])
        self.assertEqual(1, self.c.rows()[-1]["waited_seconds"])
        self.assertEqual(1, self.c.rows()[-1]["unread"])
        after = self.c.deliveries()
        self.assertEqual(before, after[:-1])
        self.assertEqual([fresh[0]["id"]], after[-1]["item_ids"])
        self.assert_no_ack()

    def test_new_authorized_conversation_can_receive_globally_held_mail(self):
        old, before = self.first_publication()
        self.c.f.arm_session(claimant.B)
        body, _ = self.c.run_waiter(session=claimant.B)
        self.assertIn(old["id"], body["followup_message"])
        self.assertEqual(before, self.c.deliveries()[:-1])
        self.assert_no_ack()

    def test_same_mtime_append_is_seen_on_the_next_native_poll(self):
        old, before = self.first_publication()
        fresh = []
        def append():
            fresh.append(self.c.f.send("same-mtime-fresh"))
        body, _ = self.c.run_waiter(sleep=append)
        self.assertEqual(1, len(fresh))
        self.assertIn(fresh[0]["id"], body["followup_message"])
        self.assertNotIn(old["id"], body["followup_message"])
        self.assertEqual(before, self.c.deliveries()[:-1])

    def test_failed_flush_is_uncertain_and_never_automatically_repeated(self):
        old = self.c.f.send("failed-flush")
        class FailedFlush(io.StringIO):
            def flush(self):
                raise OSError("scratch flush failed")
        with self.assertRaises(OSError):
            self.c.run_waiter(stdout=FailedFlush())
        before = self.c.deliveries()
        slept = []
        body, _ = self.c.run_waiter(sleep=lambda: slept.append(True))
        self.assertTrue(slept)
        self.assertIn("presentation_uncertain", body["followup_message"])
        self.assertIn(old["id"], body["followup_message"])
        self.assertEqual(before, self.c.deliveries())
        self.assert_no_ack()

    def test_initialized_presentation_history_loss_is_a_typed_refusal(self):
        self.first_publication()
        path = self.c.f.root.path / "receipts/cursor-presentations" / (self.c.node + ".jsonl")
        self.assertTrue(path.exists(), "native flush has no durable conversational testimony")
        path.unlink()
        before = self.c.deliveries()
        body, _ = self.c.run_waiter()
        self.assertIn("presentation_history_unavailable", body["followup_message"])
        self.assertEqual(before, self.c.deliveries())

"""Composed native scratch history controls; no fabricated unread counts."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import io
import json
import unittest
from unittest import mock

from floati.bus_epoch import ROLL_AUTHORITY_SUBJECT, epoch_guard, roll_bus_epoch
from floati.codex_wait_contract import CodexWaitConsentLedger, CodexWaitSessionLedger
from floati.cursor import SparseCursor
from floati.cursor_presentations import CursorPresentationLedger, OUTPUT_BYTES, limit_selection, notification
from floati.errors import IntegrityFailure
from floati.events import EVENT_KINDS, EventLog
from floati.framing import encode_frame
from floati.ids import uuid7_hex
from floati.planes import AuthorityGrantStore
from floati.records import validate_record
from floati.wake_hold import wake_coordination_guard
from tests import test_f2_cursor_claimant as claimant


class CursorPresentationHistoryTests(unittest.TestCase):
    def setUp(self):
        self.c = claimant.CursorClaimantTests("runTest")
        self.addCleanup(self.c.doCleanups)
        self.c.setUp()
        self.c.f.arm_session(claimant.A)
        self.root, self.node = self.c.f.root, self.c.node
        self.plane = CursorPresentationLedger(self.root)
        self.history = self.root.resolve_relative(self.plane.relative(self.node))
        self.witness = self.root.resolve_relative(self.plane.witness_relative(self.node))

    @contextmanager
    def selected(self, *, session=claimant.A, plane=None):
        plane = self.plane if plane is None else plane
        with epoch_guard(self.root, exclusive=False), wake_coordination_guard(self.root, self.node):
            binding = self.c.f.participant.binding
            consent = CodexWaitConsentLedger(self.root).require_armed(binding)
            classification = CodexWaitSessionLedger(self.root)._participate_already_guarded(
                binding, consent, session, classified=True)
            selection = plane.select(binding, consent, classification, harness="cursor")
            yield plane, selection

    def envelope_body(self, body):
        line = body["followup_message"].split("Exact native envelopes:\n", 1)[1].splitlines()[0]
        return json.loads(line)

    def publish(self, key):
        message = self.c.f.send(key)
        body, errors = self.c.run_waiter()
        self.assertEqual([message], self.envelope_body(body), errors)
        rows = self.plane.read(self.node)
        self.assertEqual(["prepared", "flushed"], [row["phase"] for row in rows[-2:]])
        return message, rows

    def assert_history_refused(self, detail=None):
        before = self.c.deliveries()
        with self.assertRaises(IntegrityFailure) as caught:
            CursorPresentationLedger(self.root).read(self.node)
        self.assertEqual("presentation_history_unavailable", caught.exception.code)
        if detail is not None:
            self.assertIn(detail, caught.exception.detail)
        body, _ = self.c.run_waiter()
        self.assertIn("presentation_history_unavailable", body["followup_message"])
        self.assertNotIn("Drain it now", body["followup_message"])
        self.assertEqual(before, self.c.deliveries())

    def test_write_ahead_crash_before_prepare_append_recovers_exact_native_reservation(self):
        old = self.c.f.send("write-ahead-before-append")
        with self.selected() as (plane, selection):
            bounded, body = limit_selection(selection, lambda messages: notification(
                self.root.path, self.node, self.c.f.base, claimant.A, messages))
            with mock.patch.object(plane, "_append_reserved", side_effect=OSError("scratch append stopped")):
                with self.assertRaises(OSError):
                    plane.prepare(bounded, body)
        reserved = json.loads(self.witness.read_bytes())["reserved_record"]
        self.assertEqual([old["id"]], reserved["item_ids"])
        self.assertEqual(hashlib.sha256((json.dumps(body) + "\n").encode()).hexdigest(), reserved["output_digest"])
        self.assertEqual(b"", self.history.read_bytes())
        recovered = CursorPresentationLedger(self.root).read(self.node)
        self.assertEqual([reserved], recovered)
        self.assertEqual([], self.c.deliveries())
        with self.selected(plane=CursorPresentationLedger(self.root)) as (_, selection):
            self.assertEqual([], selection["fresh_messages"])
            self.assertEqual([old["id"]], [item["item_id"] for item in selection["held_items"]])
            self.assertEqual("prepared", selection["held_items"][0]["phase"])
            self.assertEqual(1, selection["held_items"][0]["attempt_count"])
        before = self.history.read_bytes()
        slept = []
        body, _ = self.c.run_waiter(sleep=lambda: slept.append(True))
        self.assertTrue(slept)
        self.assertIn("presentation_uncertain", body["followup_message"])
        self.assertEqual(before, self.history.read_bytes())
        self.assertEqual([], self.c.deliveries())

    def test_actual_terminal_append_failure_holds_old_attempt_and_new_mail_is_independent(self):
        old = self.c.f.send("failed-terminal-append")
        original = CursorPresentationLedger._append_reserved
        def fail_terminal(plane, node, data, row):
            if row["phase"] == "flushed":
                raise OSError("scratch terminal append stopped")
            return original(plane, node, data, row)
        output = io.StringIO()
        with mock.patch.object(CursorPresentationLedger, "_append_reserved", fail_terminal):
            body, errors = self.c.run_waiter(stdout=output)
        self.assertEqual([old], self.envelope_body(body))
        self.assertEqual(1, len(output.getvalue().splitlines()), "terminal-save failure printed twice")
        self.assertIn("presentation_uncertain", errors)
        self.assertEqual(["prepared"], [row["phase"] for row in self.plane.read(self.node)])
        before = self.c.deliveries()
        slept = []
        body, _ = self.c.run_waiter(sleep=lambda: slept.append(True))
        self.assertTrue(slept)
        self.assertIn("presentation_uncertain", body["followup_message"])
        self.assertEqual(before, self.c.deliveries())
        fresh = self.c.f.send("after-uncertain-terminal")
        body, _ = self.c.run_waiter()
        self.assertEqual([fresh], self.envelope_body(body))
        self.assertEqual(before, self.c.deliveries()[:-1])
        self.assertEqual(["prepared", "prepared", "flushed"], [row["phase"] for row in self.plane.read(self.node)])

    def test_exact_terminal_replay_returns_same_receipt_without_an_append(self):
        self.c.f.send("exact-terminal-replay")
        with self.selected() as (plane, selection):
            bounded, body = limit_selection(selection, lambda messages: notification(
                self.root.path, self.node, self.c.f.base, claimant.A, messages))
            prepared = plane.prepare(bounded, body)
            delivery = plane.deliver(prepared, bounded)
            # Exercise an actual completed output flush before terminal proof.
            output = io.StringIO()
            output.write(json.dumps(body) + "\n")
            output.flush()
            terminal = plane.flushed(prepared, delivery)
            before = self.history.read_bytes()
            self.assertEqual(terminal, plane.flushed(prepared, delivery))
            self.assertEqual(before, self.history.read_bytes())
        self.assertEqual(2, len(self.plane.read(self.node)))

    def test_duplicate_native_terminal_is_refused(self):
        _, rows = self.publish("duplicate-terminal")
        with self.history.open("ab") as handle:
            handle.write(encode_frame(dict(rows[-1], id="cursor-presentation-" + uuid7_hex())))
        # Either the closed witness or batch grouping may reject this extra
        # native terminal first; neither may reset or replay the conversation.
        self.assert_history_refused()

    def test_forged_native_prefix_is_refused_even_with_matching_witness_bytes(self):
        _, rows = self.publish("prefix-forgery")
        changed = [dict(row, event_prefix_digest="f" * 64) for row in rows]
        prefix = encode_frame(changed[0])
        self.history.write_bytes(prefix + encode_frame(changed[1]))
        witness = json.loads(self.witness.read_bytes())
        witness.update(before_bytes=len(prefix), before_digest=hashlib.sha256(prefix).hexdigest(),
            reserved_record=changed[1])
        self.witness.write_text(json.dumps(witness) + "\n")
        self.assert_history_refused("input prefix")

    def test_witnessed_truncation_and_history_loss_do_not_reset_conversation(self):
        self.publish("witnessed-truncation")
        intact = self.history.read_bytes()
        for operation in ("truncate", "remove"):
            with self.subTest(operation=operation):
                if operation == "truncate":
                    self.history.write_bytes(b"")
                else:
                    self.history.unlink()
                self.assert_history_refused("missing, truncated or changed")
                self.history.write_bytes(intact)

    def test_missing_initialization_witness_is_refused_without_changing_history(self):
        self.publish("missing-initialization-witness")
        before = self.history.read_bytes()
        self.witness.unlink()
        self.assert_history_refused("initialization witness")
        self.assertEqual(before, self.history.read_bytes())

    def test_new_native_conversation_can_present_old_globally_held_envelope(self):
        old, before = self.publish("new-conversation-held")
        self.c.f.arm_session(claimant.B)
        body, _ = self.c.run_waiter(session=claimant.B)
        self.assertEqual([old], self.envelope_body(body))
        rows = self.plane.read(self.node)
        self.assertEqual(before, rows[:2])
        self.assertEqual(claimant.B, rows[-1]["acting_session_id"])

    def test_native_ack_removes_held_without_erasing_historical_presentation(self):
        old, before = self.publish("ack-removes-held")
        SparseCursor(self.root).ack(self.node, [old["id"]], acting_session_id=claimant.A)
        with self.selected() as (_, selection):
            self.assertEqual([], selection["fresh_messages"])
            self.assertEqual([], selection["held_items"])
        self.assertEqual(before, self.plane.read(self.node))
        body, _ = self.c.run_waiter()
        self.assertNotIn("held_unacked", body["followup_message"])
        self.assertNotIn(old["id"], body["followup_message"])

    def test_native_worker_route_retraction_does_not_change_ordinary_held_namespace(self):
        old, _ = self.publish("ordinary-held-alongside-worker-route")
        log = EventLog(self.root)
        worker = log.send("architect", self.node, "floati", "a" * 40,
            "docs/evidence/ping.md", "worker-routed mail", worker_session_id=claimant.A,
            idempotency_key="worker-retraction")
        retracted = log.retract(worker["id"], worker_session_id=claimant.A,
            reason="sent_in_error", author="architect")
        self.assertEqual(worker["id"], retracted["retracted_message_id"])
        with self.selected() as (_, selection):
            self.assertEqual([], selection["fresh_messages"])
            self.assertEqual([old["id"]], [item["item_id"] for item in selection["held_items"]])

    def test_fresh_tail_is_reachable_after_more_than_1000_unacked_held_envelopes(self):
        # Exercise native send at both ends; the large historical fixture is
        # built linearly from its actual envelope/readiness pair. Re-sending
        # every historical row repeatedly validates all earlier history and
        # measures quadratic setup rather than presentation reachability.
        self.c.f.send("held-prefix-0")
        events = EventLog(self.root)
        initial = events.strict_event_records()
        self.assertEqual(["message_envelope", "send_readiness_observation"],
                         [row["kind"] for row in initial])
        messages = [initial[0]]
        expected_history = list(initial)
        with self.root.resolve_relative(events.relative_path).open("ab") as stream:
            for index in range(1, 1001):
                key = "held-prefix-" + str(index)
                envelope = dict(initial[0], id="msg-" + uuid7_hex(), idempotency_key=key)
                readiness = dict(initial[1], id="send-readiness-" + uuid7_hex(),
                                 envelope_id=envelope["id"], idempotency_key=key)
                for row in (envelope, readiness):
                    stream.write(encode_frame(validate_record(row, self.root.tenant_id,
                                                               EVENT_KINDS, integrity=False)))
                messages.append(envelope)
                expected_history.extend((envelope, readiness))
        self.assertEqual(expected_history, events.strict_event_records())
        self.assertEqual(2002, len({row["id"] for row in expected_history}))
        pending = [message["id"] for message in messages]
        batches = []
        while pending:
            output = io.StringIO()
            body, errors = self.c.run_waiter(stdout=output)
            selected = self.envelope_body(body)
            ids = [message["id"] for message in selected]
            self.assertTrue(ids, errors)
            self.assertLessEqual(len(ids), 1000)
            self.assertLessEqual(len(output.getvalue().encode()), OUTPUT_BYTES)
            self.assertEqual(pending[:len(ids)], ids)
            pending = pending[len(ids):]
            batches.append(ids)
        self.assertGreater(len(batches), 1, "the whole-envelope body cap was not exercised")
        with self.selected() as (plane, selection):
            self.assertEqual(1001, selection["held_total"])
            evidence = plane.held_evidence(selection)
            self.assertTrue(evidence["truncated"])
            self.assertEqual(1000, len(evidence["held_items"]))
        before = self.c.deliveries()
        fresh = self.c.f.send("fresh-after-1001-held")
        body, _ = self.c.run_waiter()
        self.assertEqual([fresh], self.envelope_body(body))
        self.assertEqual(before, self.c.deliveries()[:-1])
        self.assertEqual([fresh["id"]], self.c.deliveries()[-1]["item_ids"])
        self.assertFalse(self.root.resolve_relative("receipts/acks/" + self.node + ".jsonl").exists())

    def test_actual_native_epoch_roll_keeps_old_history_and_allows_new_epoch_mail(self):
        old, before = self.publish("before-native-epoch-roll")
        AuthorityGrantStore(self.root).grant_exact(ROLL_AUTHORITY_SUBJECT, "architect", 1,
            datetime.now(timezone.utc))
        receipt = roll_bus_epoch(self.root, actor="architect", idempotency_key="presentation-history-roll")
        fresh = self.c.f.send("after-native-epoch-roll")
        body, errors = self.c.run_waiter()
        self.assertEqual([fresh], self.envelope_body(body), errors)
        self.assertNotIn(old["id"], body["followup_message"])
        rows = self.plane.read(self.node)
        self.assertEqual(before, rows[:2])
        self.assertNotEqual(rows[0]["epoch_anchor"], rows[-1]["epoch_anchor"])
        self.assertEqual(receipt["receipt"]["id"], rows[-1]["epoch_anchor"])
        self.assertEqual([fresh["id"]], self.c.deliveries()[-1]["item_ids"])


if __name__ == "__main__":
    unittest.main()

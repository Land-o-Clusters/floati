"""Native first-publication compatibility and held-prefix tail recovery."""
import json
import hashlib
import unittest

from tests import test_f2_codex_unacked_loop as native


class CodexPublicationSurfaceTests(unittest.TestCase):
    def setUp(self):
        self.f = native.CodexSeparateStopUnackedLoopTests("runTest")
        self.addCleanup(self.f.doCleanups)
        self.f.setUp()

    def collect(self, child):
        from floati.cursor_presentations import CursorPresentationLedger
        output, errors = self.f.collect(child)
        prepared, flushed = CursorPresentationLedger(self.f.f.root).read(self.f.node)[-2:]
        self.assertEqual(["prepared", "flushed"], [prepared["phase"], flushed["phase"]])
        digest = hashlib.sha256(output.encode("utf-8")).hexdigest()
        self.assertEqual(digest, prepared["output_digest"])
        self.assertEqual(digest, flushed["output_digest"])
        self.assertEqual(prepared["batch_id"], flushed["batch_id"])
        delivery = self.f.deliveries()[-1]
        self.assertEqual(delivery["item_ids"], prepared["item_ids"])
        self.assertEqual(delivery["item_ids"], flushed["item_ids"])
        self.assertEqual(delivery["id"], flushed["delivery_receipt_id"])
        return output, errors

    def test_first_fresh_batch_preserves_the_native_id_only_artifact(self):
        message = self.f.send("surface-first")
        output, errors = self.collect(self.f.launch())
        parsed = json.loads(output)
        self.assertEqual("block", parsed["decision"], errors)
        self.assertTrue(parsed["reason"].startswith(
            "[floati] 1 new message(s) for " + self.f.node + ": " + message["id"]), errors)
        self.assertNotIn("Exact native envelopes", parsed["reason"])
        self.assertEqual([message["id"]], self.f.deliveries()[-1]["item_ids"])
        self.f.assert_no_ack()

    def test_held_prefix_tail_embeds_only_exact_fresh_native_envelopes(self):
        old, _child, _holder, before, _attempts = self.f.first_publication()
        child = self.f.launch()
        self.f.await_second_start(child)
        fresh = self.f.send("surface-new-tail")
        output, errors = self.collect(child)
        reason = json.loads(output)["reason"]
        self.assertNotIn(old["id"], reason, errors)
        native = json.loads(reason.split("Exact native envelopes:\n", 1)[1].splitlines()[0])
        self.assertEqual([dict(fresh["message"])], native)
        self.assertEqual(before, self.f.deliveries()[:-1])
        self.assertEqual([fresh["id"]], self.f.deliveries()[-1]["item_ids"])
        self.f.assert_no_ack()

    def test_prepared_uncertain_prefix_also_publishes_exact_fresh_tail(self):
        from floati.bus_epoch import epoch_guard
        from floati.codex_wait_contract import CodexWaitConsentLedger, CodexWaitSessionLedger
        from floati.cursor_presentations import CursorPresentationLedger
        from floati.wake_hold import wake_coordination_guard
        old = self.f.send("surface-uncertain-old")
        binding = self.f.f.participant.binding
        consent = CodexWaitConsentLedger(self.f.f.root).require_armed(binding)
        with epoch_guard(self.f.f.root), wake_coordination_guard(self.f.f.root, self.f.node):
            classification = CodexWaitSessionLedger(self.f.f.root)._participate_already_guarded(
                binding, consent, native.SESSION, classified=True)
            plane = CursorPresentationLedger(self.f.f.root)
            selected = plane.select(binding, consent, classification, harness="codex")
            plane.prepare(selected, {"decision": "block", "reason": "reserved, never flushed"})
        self.assertEqual([], self.f.deliveries())
        fresh = self.f.send("surface-uncertain-new")
        output, errors = self.collect(self.f.launch())
        reason = json.loads(output)["reason"]
        self.assertNotIn(old["id"], reason, errors)
        envelopes = json.loads(reason.split("Exact native envelopes:\n", 1)[1].splitlines()[0])
        self.assertEqual([dict(fresh["message"])], envelopes)
        self.f.assert_no_ack()

    def test_ack_clearing_last_held_item_restores_id_only_artifact(self):
        from floati.cursor import SparseCursor
        old, _child, _holder, _deliveries, _attempts = self.f.first_publication()
        SparseCursor(self.f.f.root).ack(self.f.node, [old["id"]], acting_session_id=native.SESSION)
        fresh = self.f.send("surface-after-ack")
        output, errors = self.collect(self.f.launch())
        parsed = json.loads(output)
        self.assertEqual("block", parsed["decision"], errors)
        self.assertTrue(parsed["reason"].startswith(
            "[floati] 1 new message(s) for " + self.f.node + ": " + fresh["id"]), errors)
        self.assertNotIn("Exact native envelopes", parsed["reason"])
        self.assertEqual([old["id"]], self.f.rows("acks", {"ack_receipt"})[-1]["item_ids"])

    def test_large_exact_tail_is_bounded_and_remaining_whole_envelopes_stay_fresh(self):
        from floati.events import EventLog
        from floati.cursor_presentations import OUTPUT_BYTES
        old, _child, _holder, _deliveries, _attempts = self.f.first_publication()
        envelopes = [dict(EventLog(self.f.f.root).send("architect", self.f.node,
            "floati", "a" * 40, "docs/evidence/tail.md", "x" * 1024,
            idempotency_key="surface-large-" + str(index))["message"]) for index in range(180)]
        received = []
        for _ in range(2):
            output, errors = self.collect(self.f.launch())
            self.assertLessEqual(len(output.encode("utf-8")), OUTPUT_BYTES)
            reason = json.loads(output)["reason"]
            self.assertNotIn(old["id"], reason, errors)
            batch = json.loads(reason.split("Exact native envelopes:\n", 1)[1].splitlines()[0])
            self.assertTrue(batch)
            received.extend(batch)
            if len(received) == len(envelopes):
                break
        self.assertEqual(envelopes, received)
        self.f.assert_no_ack()

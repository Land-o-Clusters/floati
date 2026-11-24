"""Wake actions require real flushed conversation publication, never just a row ID."""
from __future__ import annotations

from pathlib import Path
import json
import unittest

from floati.codex_wait_contract import CodexWaitConsentLedger, CodexWaitSessionLedger
from floati.bus_epoch import epoch_guard
from floati.cursor import SparseCursor
from floati.cursor_presentations import CursorPresentationLedger
from floati.errors import IntegrityFailure, ProtocolRefusal
from floati.events import EventLog
from floati.jsonl import read_records_snapshot
from floati.records import validate_record
from floati.wake_hold import WakeAttemptLedger, wake_coordination_guard
from tests import test_codex_wait as baseline
from tests.schema_validation import SchemaValidationError, validate_json_schema


class CursorPresentationAttemptTests(unittest.TestCase):
    def setUp(self):
        self.f = baseline.CodexWaitRuntimeTests("runTest")
        self.addCleanup(self.f.doCleanups)
        self.f.setUp()
        self.session = "native-conversation-A"
        self.node = self.f.participant.binding.node_id
        self.f.arm_session(self.session)

    def publish(self, *, count=1, flushed=True):
        messages = [self.f.send("presentation-action-mail-" + str(index)) for index in range(count)]
        binding = self.f.participant.binding
        consent = CodexWaitConsentLedger(self.f.root).require_armed(binding)
        classification = CodexWaitSessionLedger(self.f.root).participate_classified(binding, consent, self.session)
        plane = CursorPresentationLedger(self.f.root)
        with epoch_guard(self.f.root, exclusive=False), wake_coordination_guard(self.f.root, self.node):
            selection = plane.select(binding, consent, classification, harness="codex")
            prepared = plane.prepare(selection, {"followup_message": "exact native scratch envelopes"})
            if not flushed:
                return messages, prepared
            delivery = plane.deliver(prepared, selection)
            terminal = plane.flushed(prepared, delivery)
        return messages, terminal

    def arguments(self, messages, terminal, **changes):
        result = dict(recipient=self.node, acting_session_id=self.session,
            item_ids=[row["id"] for row in messages], decision_receipt_id=terminal["id"],
            message_worker_session_id=None, idempotency_key="presentation-wake-action", outcome="woke")
        result.update(changes)
        return result

    def attempts(self):
        return read_records_snapshot(self.f.root, Path("receipts/wakes") / (self.node + ".jsonl"),
            allowed_kinds={"wake_attempt_receipt"})

    def test_closed_attempt_contract_accepts_only_the_two_native_decision_prefixes(self):
        repository = Path(__file__).parents[1]
        row = json.loads((repository / "tests/fixtures/wd-r4-f1/old-woke.json").read_text())
        schema = repository / "schemas/v1/wake-attempt-record.schema.json"
        for prefix in ("wake-hold-", "cursor-presentation-"):
            with self.subTest(prefix=prefix):
                candidate = dict(row, decision_receipt_id=prefix + "018f7e9b3c117abc8def0123456789ab")
                try:
                    validate_record(candidate, row["tenant_id"], {"wake_attempt_receipt"}, integrity=False)
                except ProtocolRefusal as exc:
                    self.fail("native decision reference refused: " + exc.code)
                validate_json_schema(candidate, schema)
        for prefix in ("delivery-", "cursor-presentation-invalid-", "wake-attempt-"):
            with self.subTest(prefix=prefix):
                candidate = dict(row, decision_receipt_id=prefix + "018f7e9b3c117abc8def0123456789ab")
                with self.assertRaises(ProtocolRefusal):
                    validate_record(candidate, row["tenant_id"], {"wake_attempt_receipt"}, integrity=False)
                with self.assertRaises(SchemaValidationError):
                    validate_json_schema(candidate, schema)

    def assert_refused(self, arguments, reason):
        with self.assertRaises(ProtocolRefusal) as caught:
            WakeAttemptLedger(self.f.root).record(**arguments)
        self.assertEqual(reason, caught.exception.code)
        self.assertEqual("refused", self.attempts()[-1]["outcome"])
        self.assertEqual(reason, self.attempts()[-1]["reason_code"])

    def test_flushed_native_delivery_supports_woke_queued_and_exact_replay(self):
        messages, terminal = self.publish()
        ledger = WakeAttemptLedger(self.f.root)
        for outcome in ("woke", "queued"):
            with self.subTest(outcome=outcome):
                args = self.arguments(messages, terminal, outcome=outcome, idempotency_key=outcome)
                try:
                    receipt = ledger.record(**args)
                except ProtocolRefusal as exc:
                    self.fail("native flushed presentation was refused: " + exc.code)
                self.assertEqual(outcome, receipt["outcome"])
                self.assertEqual(terminal["id"], receipt["decision_receipt_id"])
                validate_json_schema(receipt, Path(__file__).parents[1] / "schemas/v1/wake-attempt-record.schema.json")
                self.assertEqual(receipt, ledger.record(**args))
        self.assertEqual(2, len(self.attempts()))
        self.assertEqual(frozenset(), SparseCursor(self.f.root).acked_ids(self.node))

    def test_prepared_reservation_cannot_testify_to_a_wake(self):
        messages, prepared = self.publish(flushed=False)
        self.assert_refused(self.arguments(messages, prepared), "wake_decision_missing")

    def test_other_conversation_cannot_reuse_a_flushed_decision(self):
        messages, terminal = self.publish()
        self.assert_refused(self.arguments(messages, terminal, acting_session_id="native-conversation-B"),
            "wake_decision_mismatch")

    def test_exact_item_batch_is_required_without_subsets_or_reordering(self):
        messages, terminal = self.publish(count=2)
        self.assert_refused(self.arguments(messages[:1], terminal), "wake_decision_mismatch")
        self.assert_refused(self.arguments(list(reversed(messages)), terminal, idempotency_key="reversed"),
            "wake_envelope_not_owned")

    def test_unknown_presentation_id_is_a_durable_missing_decision(self):
        messages, terminal = self.publish()
        self.assert_refused(self.arguments(messages, terminal,
            decision_receipt_id="cursor-presentation-018f7e9b3c117abc8def0123456789ab"), "wake_decision_missing")

    def test_worker_routing_cannot_borrow_the_conversation_plane(self):
        messages, terminal = self.publish()
        routed = EventLog(self.f.root).send("architect", self.node, "floati", "a" * 40,
            "docs/evidence/routed.md", "routed mail", worker_session_id=self.session,
            idempotency_key="routed-presentation-attempt")
        self.assert_refused(self.arguments([routed], terminal, message_worker_session_id=self.session),
            "wake_decision_mismatch")

    def test_other_node_cannot_borrow_the_flushed_receipt(self):
        from floati.registry import Registry
        messages, terminal = self.publish()
        Registry(self.f.root).register("other-seat", "codex")
        other = EventLog(self.f.root).send("architect", "other-seat", "floati", "a" * 40,
            "docs/evidence/other.md", "other node mail", idempotency_key="other-node-presentation-attempt")
        with self.assertRaises(ProtocolRefusal) as caught:
            WakeAttemptLedger(self.f.root).record(**self.arguments([other], terminal, recipient="other-seat"))
        self.assertEqual("wake_decision_missing", caught.exception.code)
        rows = read_records_snapshot(self.f.root, "receipts/wakes/other-seat.jsonl",
            allowed_kinds={"wake_attempt_receipt"})
        self.assertEqual(1, len(rows))
        self.assertEqual("refused", rows[0]["outcome"])
        self.assertEqual([], self.attempts())

    def test_missing_native_delivery_is_integrity_failure_before_attempt_append(self):
        messages, terminal = self.publish()
        delivery = self.f.root.tenant_home / SparseCursor(self.f.root)._delivery_relative_path_for(self.node)
        delivery.write_bytes(b"")
        with self.assertRaises(IntegrityFailure) as caught:
            WakeAttemptLedger(self.f.root).record(**self.arguments(messages, terminal))
        self.assertEqual("presentation_history_unavailable", caught.exception.code)
        self.assertEqual([], self.attempts())

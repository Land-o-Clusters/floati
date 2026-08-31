from __future__ import annotations

from floati import fixture_ids as public_ids

import tempfile
import unittest
from pathlib import Path

from floati.errors import IntegrityFailure, ProtocolRefusal
from floati.ids import uuid7_hex
from floati.jsonl import append_record, read_records
from floati.root import FloatiRoot
from tests.schema_validation import validate_json_schema

try:
    from floati.cursor import SparseCursor
    from floati.events import EventLog
    from floati.registry import Registry
except ModuleNotFoundError:
    SparseCursor = None
    EventLog = None
    Registry = None


class SparseCursorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.assertIsNotNone(SparseCursor, "floati.cursor must implement sparse acknowledgment")
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = FloatiRoot.open(Path(self.temp.name), "alpha")
        self.registry = Registry(self.root)
        for node in (public_ids.worker('alpha'), "bob", "charlie"):
            self.registry.register(node, "worker")
        self.events = EventLog(self.root, self.registry)
        self.cursor = SparseCursor(self.root)
        self.messages = [
            self.events.send(
                public_ids.worker('alpha'), "bob", "slipway", character.lower() * 40,
                f"docs/evidence/{character}.md", f"notification {character}",
                idempotency_key=f"key-{character}",
            )
            for character in ("A", "B", "C")
        ]

    def test_acknowledgment_can_leave_a_hole(self) -> None:
        shown, delivery = self.events.present("bob")
        self.assertEqual([item["id"] for item in self.messages], [item["id"] for item in shown])
        self.assertEqual("delivery_receipt", delivery["kind"])
        self.cursor.ack("bob", [self.messages[1]["id"]], acting_session_id="test-session")
        pending, _ = self.events.present("bob")
        self.assertEqual([self.messages[0]["id"], self.messages[2]["id"]], [item["id"] for item in pending])

    def test_unregistered_ack_spelling_refuses_before_minting_coordination_identity(self) -> None:
        """Catches acknowledgment creating a lock namespace for an unregistered alias."""
        alias_path = self.root.resolve_relative("receipts/wake-coordination/bob_alias")

        with self.assertRaises(ProtocolRefusal) as caught:
            self.cursor.ack("bob_alias", [self.messages[0]["id"]], acting_session_id="test-session")

        self.assertEqual("unknown_node", caught.exception.code)
        self.assertFalse(alias_path.exists())

    def test_ack_union_is_sparse_and_duplicate_ack_is_idempotent(self) -> None:
        self.events.present("bob")
        self.cursor.ack("bob", [self.messages[1]["id"]], acting_session_id="test-session")
        self.cursor.ack("bob", [self.messages[0]["id"]], acting_session_id="test-session")
        before = self.cursor.path_for("bob").read_bytes()
        replay = self.cursor.ack("bob", [self.messages[1]["id"]], acting_session_id="test-session")
        self.assertEqual(before, self.cursor.path_for("bob").read_bytes())
        self.assertEqual([self.messages[1]["id"]], replay["item_ids"])
        self.assertEqual(
            frozenset((self.messages[0]["id"], self.messages[1]["id"])),
            self.cursor.acked_ids("bob"),
        )
        pending, _ = self.events.present("bob")
        self.assertEqual([self.messages[2]["id"]], [item["id"] for item in pending])

    def test_unknown_foreign_or_unseen_item_refuses_without_ack_mutation(self) -> None:
        self.events.present("bob")
        unseen = self.events.send(
            public_ids.worker('alpha'), "bob", "slipway", "d" * 40,
            "docs/evidence/D.md", "notification D", idempotency_key="key-D",
        )
        foreign = self.events.send(
            public_ids.worker('alpha'), "charlie", "slipway", "e" * 40,
            "docs/evidence/E.md", "notification E", idempotency_key="key-E",
        )
        self.events.present("charlie")
        cases = (
            ("msg-" + "0" * 32, "ack_item_unknown"),
            (foreign["id"], "ack_recipient_mismatch"),
            (unseen["id"], "ack_item_not_delivered"),
        )
        for item_id, reason in cases:
            with self.subTest(reason=reason):
                before = self.cursor.path_for("bob").read_bytes() if self.cursor.path_for("bob").exists() else b""
                with self.assertRaises(ProtocolRefusal) as caught:
                    self.cursor.ack("bob", [item_id], acting_session_id="test-session")
                self.assertEqual(reason, caught.exception.code)
                after = self.cursor.path_for("bob").read_bytes() if self.cursor.path_for("bob").exists() else b""
                self.assertEqual(before, after)

    def test_ack_receipt_names_explicit_item_ids(self) -> None:
        self.events.present("bob")
        receipt = self.cursor.ack(
            "bob",
            [self.messages[2]["id"], self.messages[0]["id"]],
            acting_session_id="codex-session-1",
        )
        self.assertEqual("ack_receipt", receipt["kind"])
        self.assertEqual(1, receipt["schema_version"])
        self.assertEqual("codex-session-1", receipt["acting_session_id"])
        self.assertIsNone(receipt["node_lease_id"])
        self.assertEqual("not_leased", receipt["node_lease_state_at_ack"])
        self.assertIsNone(receipt["node_lease_expires_at"])
        self.assertEqual([self.messages[2]["id"], self.messages[0]["id"]], receipt["item_ids"])
        durable = read_records(self.root, "receipts/acks/bob.jsonl", allowed_kinds={"ack_receipt"})
        self.assertEqual(receipt, durable[0])
        validate_json_schema(receipt, Path("schemas/v1/ack-receipt.schema.json"))

    def test_historical_v0_ack_remains_readable_after_actor_bound_v1_writes(self) -> None:
        self.events.present("bob")
        historical = {
            "schema_version": 0,
            "id": "ack-" + uuid7_hex(),
            "tenant_id": "alpha",
            "timestamp": "2026-07-31T12:00:00.000Z",
            "kind": "ack_receipt",
            "recipient": "bob",
            "item_ids": [self.messages[0]["id"]],
        }
        append_record(
            self.root,
            "receipts/acks/bob.jsonl",
            historical,
            allowed_kinds={"ack_receipt"},
        )

        receipt = self.cursor.ack(
            "bob", [self.messages[1]["id"]], acting_session_id="codex-session-2"
        )

        self.assertEqual(1, receipt["schema_version"])
        self.assertEqual(
            frozenset((self.messages[0]["id"], self.messages[1]["id"])),
            self.cursor.acked_ids("bob"),
        )

    def test_historical_actor_bound_v1_ack_without_lease_fields_remains_readable(self) -> None:
        self.events.present("bob")
        historical = {
            "schema_version": 1,
            "id": "ack-" + uuid7_hex(),
            "tenant_id": "alpha",
            "timestamp": "2026-07-31T12:00:00.000Z",
            "kind": "ack_receipt",
            "recipient": "bob",
            "acting_session_id": "historical-session",
            "item_ids": [self.messages[0]["id"]],
        }
        append_record(
            self.root,
            "receipts/acks/bob.jsonl",
            historical,
            allowed_kinds={"ack_receipt"},
        )

        self.assertEqual(
            frozenset({self.messages[0]["id"]}),
            self.cursor.acked_ids("bob"),
        )
        validate_json_schema(
            historical, Path("schemas/v1/ack-receipt.schema.json")
        )

    def test_truncated_message_history_cannot_make_deliveries_disappear(self) -> None:
        self.events.present("bob")
        self.events.path.write_bytes(b"")
        with self.assertRaises(IntegrityFailure) as caught:
            self.events.present("bob")
        self.assertEqual("delivery_evidence_invalid", caught.exception.code)

    def test_forged_durable_ack_is_rejected_as_integrity_failure(self) -> None:
        self.events.present("bob")
        forged = {
            "schema_version": 0,
            "id": "ack-" + uuid7_hex(),
            "tenant_id": "alpha",
            "timestamp": "2026-07-31T12:00:00.000Z",
            "kind": "ack_receipt",
            "recipient": "bob",
            "item_ids": ["msg-" + uuid7_hex()],
        }
        append_record(self.root, "receipts/acks/bob.jsonl", forged, allowed_kinds={"ack_receipt"})
        with self.assertRaises(IntegrityFailure) as caught:
            self.cursor.acked_ids("bob")
        self.assertEqual("ack_evidence_invalid", caught.exception.code)

    def test_forged_nonparty_retraction_fails_closed_before_ack_mutation(self) -> None:
        """Cursor reads and writes must consume EventLog's semantic retraction projection."""
        session = "worker-" + uuid7_hex()
        message = self.events.send(
            public_ids.worker('alpha'), "bob", "slipway", "d" * 40,
            "docs/evidence/retraction.md", "session message",
            idempotency_key="forged-retraction", worker_session_id=session,
        )
        shown, _delivery = self.events.present("bob", worker_session_id=session)
        self.assertEqual([message["id"]], [item["id"] for item in shown])
        forged = {
            "schema_version": 0,
            "id": "ret-" + uuid7_hex(),
            "tenant_id": "alpha",
            "timestamp": "2026-08-08T12:00:00.000Z",
            "kind": "message_retracted",
            "retracted_message_id": message["id"],
            "worker_session_id": session,
            "reason": "stale_recipient",
            "author": "attacker",
        }
        append_record(
            self.root,
            "events.jsonl",
            forged,
            allowed_kinds={"message_envelope", "message_retracted"},
        )
        with self.assertRaises(IntegrityFailure) as event_error:
            self.events.event_records()
        self.assertEqual("message_retraction_party_invalid", event_error.exception.code)

        ack_path = self.cursor.path_for("bob", worker_session_id=session)
        before = ack_path.read_bytes() if ack_path.exists() else b""
        with self.assertRaises(IntegrityFailure) as read_error:
            self.cursor.acked_ids("bob", worker_session_id=session)
        self.assertEqual("message_retraction_party_invalid", read_error.exception.code)
        with self.assertRaises(IntegrityFailure) as ack_error:
            self.cursor.ack(
                "bob", [message["id"]], acting_session_id="test-session",
                worker_session_id=session,
            )
        self.assertEqual("message_retraction_party_invalid", ack_error.exception.code)
        after = ack_path.read_bytes() if ack_path.exists() else b""
        self.assertEqual(before, after)

    def test_acknowledgment_of_a_hold_receipt_is_lawful_and_removes_held_work(self) -> None:
        """Catches acknowledgments treating controller-held work as undelivered."""
        from floati.wake_hold import WakeHoldController

        held = WakeHoldController(self.root).evaluate("bob", idempotency_key="cursor-hold")
        item_ids = [row["id"] for row in held["fresh_messages"]]
        self.cursor.ack("bob", item_ids, acting_session_id="test-session")
        after = WakeHoldController(self.root).evaluate("bob", idempotency_key="cursor-after")
        self.assertEqual("caught_up", after["state"])
        self.assertEqual([], after["held_items"])

    def test_presented_acknowledgment_remains_valid_history_after_later_retraction(self) -> None:
        """Catches ordinary cursor replay invalidating an ack when a later retraction wins pending state."""
        from floati.wake_hold import WakeHoldController

        session = "worker-" + uuid7_hex()
        message = self.events.send(
            public_ids.worker('alpha'), "bob", "slipway", "f" * 40,
            "docs/evidence/ack-then-retract.md", "historical ack",
            idempotency_key="ack-then-retract-cursor", worker_session_id=session,
        )
        shown, delivery = self.events.present("bob", worker_session_id=session)
        self.assertEqual([message], shown)
        self.assertIsNotNone(delivery)
        self.cursor.ack(
            "bob", [message["id"]], acting_session_id="test-session",
            worker_session_id=session,
        )
        self.events.retract(
            message["id"], worker_session_id=session,
            reason="sent_in_error", author=public_ids.worker('alpha'),
        )

        self.assertEqual(
            frozenset({message["id"]}),
            self.cursor.acked_ids("bob", worker_session_id=session),
        )
        pending, repeated = self.events.present("bob", worker_session_id=session)
        self.assertEqual([], pending)
        self.assertIsNone(repeated)
        decision = WakeHoldController(self.root).evaluate(
            "bob", idempotency_key="after-historical-ack", worker_session_id=session,
        )
        self.assertEqual("caught_up", decision["state"])
        self.assertFalse(decision["wake_required"])
        self.assertIsNone(decision["receipt"])

        unseen = self.events.send(
            public_ids.worker('alpha'), "bob", "slipway", "1" * 40,
            "docs/evidence/unpresented-after-retraction.md", "still unseen",
            idempotency_key="unpresented-after-retraction", worker_session_id=session,
        )
        with self.assertRaises(ProtocolRefusal) as caught:
            self.cursor.ack(
                "bob", [unseen["id"]], acting_session_id="test-session",
                worker_session_id=session,
            )
        self.assertEqual("ack_item_not_delivered", caught.exception.code)


if __name__ == "__main__":
    unittest.main()

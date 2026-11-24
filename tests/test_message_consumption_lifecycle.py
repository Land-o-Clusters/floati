"""Consumption evidence survives real epoch and storage boundaries."""

from __future__ import annotations

import copy
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import tests  # Pin HOME before native producers resolve defaults.

from floati import jsonl
from floati.bus_epoch import ROLL_AUTHORITY_SUBJECT, roll_bus_epoch
from floati.cursor import SparseCursor
from floati.errors import DurabilityFailure, IntegrityFailure, ProtocolRefusal
from floati.events import EventLog
from floati.framing import decode_frames, encode_frame
from floati.ids import uuid7_hex
from floati.message_consumption import CONSUMPTION_KINDS, MessageConsumption
from floati.planes import AuthorityGrantStore
from floati.records import validate_record
from floati.registry import Registry
from floati.root import FloatiRoot
from floati.work import WorkLog
from tests.schema_validation import SchemaValidationError, validate_json_schema


SCHEMA = Path("schemas/v1/consumption-receipt.schema.json")


class MessageConsumptionLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="floati-consumption-lifecycle-")
        self.addCleanup(temporary.cleanup)
        self.root = FloatiRoot.open_direct_home(Path(temporary.name) / "fleet", create=True)
        registry = Registry(self.root)
        for node in ("sender", "recipient", "other"):
            registry.register(node, "codex")
        self.events = EventLog(self.root)
        self.cursor = SparseCursor(self.root)
        self.consumption = MessageConsumption(self.root)
        self.work = WorkLog(self.root)

    def _acked_message(self, *, worker_session_id=None):
        message = self.events.send(
            "sender", "recipient", "floati", "a" * 40,
            "docs/dispatch.md", "bounded dispatch", idempotency_key=uuid7_hex(),
            worker_session_id=worker_session_id,
        )
        self.events.present("recipient", worker_session_id=worker_session_id)
        acknowledgment = self.cursor.ack(
            "recipient", [str(message["id"])], acting_session_id="seen-session",
            worker_session_id=worker_session_id,
        )
        return message, acknowledgment

    def _explicit_receipt(self):
        message, acknowledgment = self._acked_message()
        receipt = self.consumption.consume("recipient", str(message["id"]), "acting-session")
        return message, acknowledgment, receipt

    def _consumed_path(self):
        return self.root.resolve_relative("receipts/consumed/recipient.jsonl")

    def _claimed(self):
        now = datetime.now(timezone.utc)
        grant = AuthorityGrantStore(self.root).claim("work-claims", "recipient", 60, 60, now)
        item = self.work.add("dispatch work", "recipient", [])
        self.work.claim(
            str(item["id"]), "recipient", "work-claims", int(grant["epoch"]), now=now,
        )
        return item

    def _roll(self):
        AuthorityGrantStore(self.root).grant_exact(
            ROLL_AUTHORITY_SUBJECT, "recipient", 1, datetime.now(timezone.utc),
        )
        return roll_bus_epoch(self.root, actor="recipient", idempotency_key="consumption-epoch")

    def _assert_syntax_refuses_both(self, receipt):
        with self.assertRaises(ProtocolRefusal):
            validate_record(receipt, self.root.tenant_id, CONSUMPTION_KINDS, integrity=False)
        with self.assertRaises(SchemaValidationError):
            validate_json_schema(receipt, SCHEMA)

    def test_native_authored_explicit_and_work_receipts_match_shipped_schema(self) -> None:
        _message, _acknowledgment, explicit = self._explicit_receipt()
        item = self._claimed()
        message, _acknowledgment = self._acked_message()
        self.work.complete(
            str(item["id"]), "recipient", [], consume_ids=(str(message["id"]),),
            session_id="acting-session",
        )
        rows = self.consumption.records("recipient")
        self.assertEqual(2, len(rows))
        self.assertEqual({"explicit", "work_complete"}, {row["source"] for row in rows})
        for receipt in rows:
            with self.subTest(source=receipt["source"]):
                validate_record(receipt, self.root.tenant_id, CONSUMPTION_KINDS, integrity=False)
                validate_json_schema(receipt, SCHEMA)
        self.assertIn(explicit, rows)

    def test_extra_fields_versions_and_source_work_binding_refuse_in_both_validators(self) -> None:
        _message, _acknowledgment, receipt = self._explicit_receipt()
        mutants = (
            {"unrecognized": True},
            {"schema_version": 0},
            {"schema_version": 2},
            {"ack_receipt_id": "delivery-" + uuid7_hex()},
            {"source": "inferred"},
            {"work_item_id": "work-" + uuid7_hex()},
            {"source": "work_complete", "work_item_id": None},
        )
        for case_index, changes in enumerate(mutants):
            with self.subTest(case=(
                "extra-field",
                "schema-version-0",
                "schema-version-2",
                "delivery-id-as-ack-receipt",
                "inferred-source",
                "work-id-with-explicit-source",
                "work-complete-with-null-work-id",
            )[case_index]):
                self._assert_syntax_refuses_both({**receipt, **changes})

    def test_invalid_recipient_syntax_has_native_schema_parity(self) -> None:
        _message, _acknowledgment, receipt = self._explicit_receipt()
        self._assert_syntax_refuses_both({**receipt, "recipient": "Recipient/escape"})

    def test_reserved_mixed_case_session_has_native_schema_parity(self) -> None:
        _message, _acknowledgment, receipt = self._explicit_receipt()
        for field in ("acting_session_id", "worker_session_id"):
            with self.subTest(field=field):
                self._assert_syntax_refuses_both({**receipt, field: "All"})

    def test_terminal_unsafe_idempotency_key_has_native_schema_parity(self) -> None:
        _message, _acknowledgment, receipt = self._explicit_receipt()
        self._assert_syntax_refuses_both({**receipt, "idempotency_key": "unsafe\x1bkey"})

    def test_unicode_terminal_controls_have_native_schema_parity(self) -> None:
        _message, _acknowledgment, receipt = self._explicit_receipt()
        for unsafe in ("\u202e", "\U0001d173", "\U000e0020"):
            with self.subTest(codepoint=ord(unsafe)):
                self._assert_syntax_refuses_both({**receipt, "idempotency_key": "key" + unsafe})

    def test_schema_valid_wrong_ack_recipient_and_work_references_fail_history(self) -> None:
        _message, _acknowledgment, receipt = self._explicit_receipt()
        path = self._consumed_path()
        original = path.read_bytes()
        mutants = (
            {"ack_receipt_id": "ack-" + uuid7_hex()},
            {"recipient": "other"},
            {"source": "work_complete", "work_item_id": "work-" + uuid7_hex()},
        )
        try:
            for case_index, changes in enumerate(mutants):
                with self.subTest(case=(
                    "foreign-ack-receipt",
                    "wrong-recipient-other",
                    "foreign-work-complete-item",
                )[case_index]):
                    forged = {**receipt, **changes}
                    validate_json_schema(forged, SCHEMA)
                    path.write_bytes(encode_frame(forged))
                    with self.assertRaises(IntegrityFailure) as caught:
                        self.consumption.records("recipient")
                    self.assertEqual("consumption_evidence_invalid", caught.exception.code)
        finally:
            path.write_bytes(original)

    def test_explicit_key_conflict_leaves_original_history_bytes_unchanged(self) -> None:
        first, _acknowledgment = self._acked_message()
        second, _acknowledgment = self._acked_message()
        receipt = self.consumption.consume(
            "recipient", str(first["id"]), "acting-session", idempotency_key="same-key",
        )
        before = self._consumed_path().read_bytes()
        with self.assertRaises(ProtocolRefusal) as caught:
            self.consumption.consume(
                "recipient", str(second["id"]), "acting-session", idempotency_key="same-key",
            )
        self.assertEqual("consume_idempotency_conflict", caught.exception.code)
        self.assertEqual(before, self._consumed_path().read_bytes())
        self.assertEqual([receipt], self.consumption.records("recipient"))

    def test_semantically_invalid_event_prefix_cannot_authorize_consumption(self) -> None:
        message, _acknowledgment = self._acked_message()
        # Native send/delivery/ACK establish the full valid history first.
        # Corrupt one known envelope's causal reference without changing its
        # recipient, message ID, delivery, or acknowledgment membership.
        path = self.root.resolve_relative("events.jsonl")
        frames = decode_frames(path.read_bytes())
        for row in frames:
            if row.get("id") == message["id"]:
                row["reply_to"] = "msg-" + uuid7_hex()
        path.write_bytes(b"".join(encode_frame(row) for row in frames))
        with self.assertRaises(IntegrityFailure) as native:
            self.events.event_records()
        self.assertEqual("message_order_invalid", native.exception.code)
        with self.assertRaises(IntegrityFailure):
            self.consumption.consume("recipient", str(message["id"]), "acting-session")
        self.assertFalse(self._consumed_path().exists())

    def test_batch_cannot_bind_same_explicit_key_to_two_new_envelopes(self) -> None:
        first, _acknowledgment = self._acked_message()
        second, _acknowledgment = self._acked_message()
        with self.assertRaises(ProtocolRefusal) as caught:
            self.consumption.consume_many(
                "recipient", [str(first["id"]), str(second["id"])], "acting-session",
                idempotency_key="ambiguous-batch-key",
            )
        self.assertEqual("consume_idempotency_conflict", caught.exception.code)
        self.assertFalse(self._consumed_path().exists())

    def test_matching_worker_session_consumes_only_its_native_plane(self) -> None:
        session = "worker-" + uuid7_hex()
        message, acknowledgment = self._acked_message(worker_session_id=session)
        receipt = self.consumption.consume("recipient", str(message["id"]), session)
        self.assertEqual(session, receipt["worker_session_id"])
        self.assertEqual(session, receipt["acting_session_id"])
        self.assertEqual(acknowledgment["id"], receipt["ack_receipt_id"])
        self.assertEqual([receipt], self.consumption.records("recipient"))
        self.assertFalse(self._consumed_path().exists())
        session_files = list(self.root.resolve_relative("receipts/consumed/recipient").glob("*.jsonl"))
        self.assertEqual(1, len(session_files))
        validate_json_schema(receipt, SCHEMA)

    def test_epoch_roll_retains_durable_receipts_but_projects_only_current_consumption(self) -> None:
        old_message, _acknowledgment, _receipt = self._explicit_receipt()
        before = self._consumed_path().read_bytes()
        self._roll()
        self.assertEqual(before, self._consumed_path().read_bytes())
        self.assertEqual([], self.consumption.records("recipient"))
        with self.assertRaises(ProtocolRefusal) as caught:
            self.consumption.consume("recipient", str(old_message["id"]), "acting-session")
        self.assertEqual("consume_item_archived", caught.exception.code)
        self.assertEqual(before, self._consumed_path().read_bytes())
        current_message, _acknowledgment = self._acked_message()
        current = self.consumption.consume("recipient", str(current_message["id"]), "acting-session")
        self.assertEqual([current], self.consumption.records("recipient"))
        self.assertEqual(2, len(decode_frames(self._consumed_path().read_bytes())))

    def test_epoch_roll_validates_persisted_work_link_without_requiring_new_completion(self) -> None:
        item = self._claimed()
        message, _acknowledgment = self._acked_message()
        self.work.complete(
            str(item["id"]), "recipient", [], consume_ids=(str(message["id"]),),
            session_id="acting-session",
        )
        before = self._consumed_path().read_bytes()
        self._roll()
        self.assertEqual(before, self._consumed_path().read_bytes())
        self.assertEqual([], self.consumption.records("recipient"))
        self.assertEqual("completed", self.work.show(str(item["id"]))[0]["state"])

    def test_corrupted_owned_archive_cannot_make_historical_consumption_disappear(self) -> None:
        self._explicit_receipt()
        roll = self._roll()
        archive = Path(str(roll["receipt"]["archive_path"]))
        archived_events = archive / "events.jsonl"
        archived_events.write_bytes(archived_events.read_bytes() + b"not-a-frame\n")
        with self.assertRaises(IntegrityFailure):
            self.consumption.records("recipient")

    def test_second_consumption_storage_failure_leaves_work_claimed_and_retry_dedupes(self) -> None:
        item = self._claimed()
        first, _acknowledgment = self._acked_message()
        second, _acknowledgment = self._acked_message()
        work_path = self.root.resolve_relative("work/items.jsonl")
        before_work = work_path.read_bytes()
        append = jsonl._append_frame
        writes = 0

        def fail_second_consumption(path, encoded, **kwargs):
            nonlocal writes
            if any(row.get("kind") == "consumption_receipt" for row in decode_frames(encoded)):
                writes += 1
                if writes == 2:
                    raise DurabilityFailure("storage_unavailable", "injected second consumption write failure")
            return append(path, encoded, **kwargs)

        with mock.patch("floati.jsonl._append_frame", side_effect=fail_second_consumption):
            with self.assertRaises(DurabilityFailure):
                self.work.complete(
                    str(item["id"]), "recipient", [],
                    consume_ids=(str(first["id"]), str(second["id"])), session_id="acting-session",
                )
        self.assertEqual(before_work, work_path.read_bytes())
        partial = copy.deepcopy(self.consumption.records("recipient"))
        self.assertEqual([first["id"]], [row["message_id"] for row in partial])
        self.assertEqual("claimed", self.work.show(str(item["id"]))[0]["state"])
        self.work.complete(
            str(item["id"]), "recipient", [],
            consume_ids=(str(first["id"]), str(second["id"])), session_id="successor-session",
        )
        rows = self.consumption.records("recipient")
        self.assertEqual(2, len(rows))
        self.assertEqual(partial[0], next(row for row in rows if row["message_id"] == first["id"]))
        self.assertEqual("completed", self.work.show(str(item["id"]))[0]["state"])


if __name__ == "__main__":
    unittest.main()

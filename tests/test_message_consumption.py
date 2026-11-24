"""BUS-CONSUMED-RECEIPT-1: ACK is seen; consumption is an authored act."""

from __future__ import annotations

import importlib
import importlib.util
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import tests  # Pin HOME before a native producer resolves defaults.

from floati import jsonl
from floati.cursor import SparseCursor
from floati.errors import DurabilityFailure, ProtocolRefusal
from floati.events import EventLog
from floati.framing import decode_frames
from floati.ids import uuid7_hex
from floati.planes import AuthorityGrantStore
from floati.projection import FleetProjection
from floati.registry import Registry
from floati.root import FloatiRoot
from floati.work import WorkLog


class MessageConsumptionTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="floati-message-consumption-")
        self.addCleanup(temporary.cleanup)
        self.root = FloatiRoot.open_direct_home(Path(temporary.name) / "fleet", create=True)
        registry = Registry(self.root)
        for node in ("sender", "recipient", "other"):
            registry.register(node, "codex")
        self.events = EventLog(self.root)
        self.cursor = SparseCursor(self.root)
        self.work = WorkLog(self.root)

    def _consumption(self):
        specification = importlib.util.find_spec("floati.message_consumption")
        self.assertIsNotNone(
            specification,
            "native message-consumption history/API is absent; ACK cannot replace it",
        )
        module = importlib.import_module("floati.message_consumption")
        self.assertTrue(hasattr(module, "MessageConsumption"))
        return module.MessageConsumption(self.root)

    def _send(self, *, recipient="recipient", worker_session_id=None):
        return self.events.send(
            "sender", recipient, "floati", "a" * 40, "docs/dispatch.md",
            "bounded dispatch", idempotency_key="dispatch-" + uuid7_hex(),
            worker_session_id=worker_session_id,
        )

    def _ack(self, message, *, worker_session_id=None):
        recipient = str(message["recipient"])
        self.events.present(recipient, worker_session_id=worker_session_id)
        return self.cursor.ack(
            recipient, [str(message["id"])], acting_session_id="seen-session",
            worker_session_id=worker_session_id,
        )

    def _claimed(self):
        now = datetime.now(timezone.utc)
        grant = AuthorityGrantStore(self.root).claim(
            "work-claims", "recipient", 60, 60, now,
        )
        item = self.work.add("act on dispatch", "recipient", [])
        self.work.claim(
            str(item["id"]), "recipient", "work-claims", int(grant["epoch"]), now=now,
        )
        return item

    def _work_bytes(self):
        return self.root.resolve_relative("work/items.jsonl").read_bytes()

    def test_ack_alone_has_explicit_empty_consumption_history(self) -> None:
        message = self._send()
        acknowledgment = self._ack(message)
        history = FleetProjection(self.root).receipts("recipient")
        self.assertEqual([acknowledgment], history["acks"])
        self.assertIn(
            "consumptions", history,
            "receipt history must expose consumption separately from acknowledgment",
        )
        self.assertEqual([], history["consumptions"])
        self.assertFalse(self.root.resolve_relative("receipts/consumed/recipient.jsonl").exists())

    def test_explicit_consume_authors_native_per_envelope_history(self) -> None:
        message = self._send()
        acknowledgment = self._ack(message)
        consumption = self._consumption()
        receipt = consumption.consume("recipient", str(message["id"]), "acting-session")
        self.assertEqual("consumption_receipt", receipt["kind"])
        self.assertEqual(message["id"], receipt["message_id"])
        self.assertEqual("recipient", receipt["recipient"])
        self.assertEqual("acting-session", receipt["acting_session_id"])
        self.assertEqual(acknowledgment["id"], receipt["ack_receipt_id"])
        self.assertEqual("explicit", receipt["source"])
        self.assertIsNone(receipt["work_item_id"])
        self.assertEqual([receipt], consumption.records("recipient"))
        self.assertEqual([receipt], FleetProjection(self.root).receipts("recipient")["consumptions"])
        self.assertTrue(self.root.resolve_relative("receipts/consumed/recipient.jsonl").is_file())

    def test_missing_ack_refuses_without_consumption_or_synthetic_ack(self) -> None:
        message = self._send()
        self.events.present("recipient")
        consumption = self._consumption()
        with self.assertRaises(ProtocolRefusal) as caught:
            consumption.consume("recipient", str(message["id"]), "acting-session")
        self.assertEqual("consume_item_not_acknowledged", caught.exception.code)
        self.assertEqual([], consumption.records("recipient"))
        self.assertFalse(self.cursor.path_for("recipient").exists())

    def test_unknown_and_foreign_envelopes_refuse_without_consumption(self) -> None:
        consumption = self._consumption()
        foreign = self._send(recipient="other")
        self._ack(foreign)
        for message_id, code in (
            ("msg-" + uuid7_hex(), "consume_item_unknown"),
            (str(foreign["id"]), "consume_recipient_mismatch"),
        ):
            with self.subTest(code=code):
                with self.assertRaises(ProtocolRefusal) as caught:
                    consumption.consume("recipient", message_id, "acting-session")
                self.assertEqual(code, caught.exception.code)
                self.assertEqual([], consumption.records("recipient"))

    def test_worker_session_ack_cannot_authorize_seat_plane_consumption(self) -> None:
        worker_session = "worker-" + uuid7_hex()
        message = self._send(worker_session_id=worker_session)
        self._ack(message, worker_session_id=worker_session)
        consumption = self._consumption()
        with self.assertRaises(ProtocolRefusal) as caught:
            consumption.consume("recipient", str(message["id"]), "acting-session")
        self.assertEqual("consume_session_mismatch", caught.exception.code)
        self.assertEqual([], consumption.records("recipient"))

    def test_native_retracted_envelope_never_becomes_consumed(self) -> None:
        # Retraction's native contract names a worker session. Either the
        # retraction or the disjoint plane may be the first refusal, but no
        # validation order may create consumption for this envelope.
        worker_session = "worker-" + uuid7_hex()
        message = self._send(worker_session_id=worker_session)
        self._ack(message, worker_session_id=worker_session)
        self.events.retract(
            str(message["id"]), worker_session_id=worker_session,
            reason="superseded_by_correction", author="sender",
        )
        consumption = self._consumption()
        with self.assertRaises(ProtocolRefusal) as caught:
            consumption.consume("recipient", str(message["id"]), "acting-session")
        self.assertIn(caught.exception.code, {"consume_item_retracted", "consume_session_mismatch"})
        self.assertEqual([], consumption.records("recipient"))

    def test_node_envelope_dedupe_survives_different_acting_session(self) -> None:
        message = self._send()
        self._ack(message)
        consumption = self._consumption()
        first = consumption.consume(
            "recipient", str(message["id"]), "first-session", idempotency_key="first-key",
        )
        path = self.root.resolve_relative("receipts/consumed/recipient.jsonl")
        before = path.read_bytes()
        replay = consumption.consume(
            "recipient", str(message["id"]), "successor-session", idempotency_key="different-key",
        )
        self.assertEqual(first, replay)
        self.assertEqual(before, path.read_bytes())
        self.assertEqual([first], consumption.records("recipient"))

    def test_sparse_ack_batch_does_not_consume_neighbor_envelope(self) -> None:
        first, second = self._send(), self._send()
        self.events.present("recipient")
        self.cursor.ack(
            "recipient", [str(first["id"]), str(second["id"])],
            acting_session_id="seen-session",
        )
        consumption = self._consumption()
        consumption.consume("recipient", str(second["id"]), "acting-session")
        self.assertEqual([second["id"]], [row["message_id"] for row in consumption.records("recipient")])

    def test_legacy_completion_without_dispatches_still_completes(self) -> None:
        item = self._claimed()
        transition = self.work.complete(str(item["id"]), "recipient", [])
        self.assertEqual("complete", transition["action"])
        self.assertEqual("completed", self.work.show(str(item["id"]))[0]["state"])
        self.assertFalse(self.root.resolve_relative("receipts/consumed/recipient.jsonl").exists())

    def test_completion_authors_each_named_dispatch_consumption(self) -> None:
        item = self._claimed()
        first, second = self._send(), self._send()
        for message in (first, second):
            self._ack(message)
        consumption = self._consumption()
        self.work.complete(
            str(item["id"]), "recipient", [],
            consume_ids=(str(first["id"]), str(second["id"])), session_id="acting-session",
        )
        self.assertEqual("completed", self.work.show(str(item["id"]))[0]["state"])
        rows = consumption.records("recipient")
        self.assertEqual({first["id"], second["id"]}, {row["message_id"] for row in rows})
        self.assertEqual(2, len(rows))
        for row in rows:
            self.assertEqual("work_complete", row["source"])
            self.assertEqual(item["id"], row["work_item_id"])

    def test_mixed_invalid_dispatch_batch_leaves_no_completion_or_consumption(self) -> None:
        item = self._claimed()
        acknowledged, unseen = self._send(), self._send()
        self._ack(acknowledged)
        consumption = self._consumption()
        before = self._work_bytes()
        with self.assertRaises(ProtocolRefusal) as caught:
            self.work.complete(
                str(item["id"]), "recipient", [],
                consume_ids=(str(acknowledged["id"]), str(unseen["id"])),
                session_id="acting-session",
            )
        self.assertEqual("consume_item_not_acknowledged", caught.exception.code)
        self.assertEqual(before, self._work_bytes())
        self.assertEqual([], consumption.records("recipient"))
        self.assertEqual("claimed", self.work.show(str(item["id"]))[0]["state"])

    def test_completion_write_failure_leaves_consumed_first_and_retry_dedupes(self) -> None:
        item = self._claimed()
        message = self._send()
        self._ack(message)
        consumption = self._consumption()
        original_append = jsonl._append_frame

        def fail_completion(path, encoded, **kwargs):
            if Path(path).name == "items.jsonl" and any(
                row.get("kind") == "work_transition" and row.get("action") == "complete"
                for row in decode_frames(encoded)
            ):
                raise DurabilityFailure("storage_unavailable", "injected completion append failure")
            return original_append(path, encoded, **kwargs)

        before = self._work_bytes()
        with mock.patch("floati.jsonl._append_frame", side_effect=fail_completion):
            with self.assertRaises(DurabilityFailure):
                self.work.complete(
                    str(item["id"]), "recipient", [],
                    consume_ids=(str(message["id"]),), session_id="acting-session",
                )
        self.assertEqual(before, self._work_bytes())
        self.assertEqual("claimed", self.work.show(str(item["id"]))[0]["state"])
        first_rows = consumption.records("recipient")
        self.assertEqual(1, len(first_rows))
        self.assertEqual(message["id"], first_rows[0]["message_id"])
        self.work.complete(
            str(item["id"]), "recipient", [],
            consume_ids=(str(message["id"]),), session_id="successor-session",
        )
        self.assertEqual(first_rows, consumption.records("recipient"))
        self.assertEqual("completed", self.work.show(str(item["id"]))[0]["state"])

    def test_wrong_work_holder_cannot_consume_named_dispatch(self) -> None:
        item = self._claimed()
        foreign = self._send(recipient="other")
        self._ack(foreign)
        consumption = self._consumption()
        before = self._work_bytes()
        with self.assertRaises(ProtocolRefusal) as caught:
            self.work.complete(
                str(item["id"]), "other", [],
                consume_ids=(str(foreign["id"]),), session_id="acting-session",
            )
        self.assertEqual("work_holder_mismatch", caught.exception.code)
        self.assertEqual(before, self._work_bytes())
        self.assertEqual([], consumption.records("other"))


if __name__ == "__main__":
    unittest.main()

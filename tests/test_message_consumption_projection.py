"""BUS-CONSUMED-RECEIPT-1: ACTED testimony stays distinct in readers."""

from __future__ import annotations

import importlib
import importlib.util
import copy
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from floati import fixture_ids as ids
from floati.cursor import SparseCursor
from floati.events import EventLog
from floati.errors import SnapshotRefusal
from floati.projection import FleetProjection
from floati.registry import Registry
from floati.root import FloatiRoot
from tests import test_seat_board as board_fixture


NOW = datetime(2026, 10, 14, 12, tzinfo=timezone.utc)


def consumption_ledger(case, root):
    """A missing future API is a named contract failure, never an import error."""
    name = "floati.message_consumption"
    case.assertIsNotNone(
        importlib.util.find_spec(name),
        "BUS-CONSUMED-RECEIPT-1 requires the separate message-consumption ledger",
    )
    module = importlib.import_module(name)
    implementation = getattr(module, "MessageConsumption", None)
    case.assertTrue(callable(implementation), "MessageConsumption must be available")
    return implementation(root)


class MessageConsumptionProjectionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = FloatiRoot.open_direct_home(Path(temporary.name) / "fleet", create=True)
        for node in ("architect", "recipient"):
            Registry(self.root).register(node, "Codex")
        self.events = EventLog(self.root)

    def send(self, label, *, worker_session_id=None):
        return self.events.send(
            "architect", "recipient", "floati", "a" * 40,
            "docs/evidence/checkpoint.md", label, idempotency_key=label,
            worker_session_id=worker_session_id,
        )

    def ack(self, message):
        return SparseCursor(self.root).ack(
            "recipient", [message["id"]], acting_session_id="fixture-session",
        )

    def node_progress(self, artifact):
        row = next(node for node in artifact["nodes"] if node["node_id"] == "recipient")
        self.assertIn("message_progress", row, "status must expose all three receipt axes")
        return row["message_progress"]

    def test_ack_does_not_author_consumption_history(self):
        message = self.send("ack-is-seen")
        self.events.present("recipient")
        self.ack(message)

        history = FleetProjection(self.root).receipts("recipient")
        self.assertIn("consumptions", history, "receipts must expose separate ACTED history")
        self.assertEqual([], history["consumptions"])
        self.assertEqual(1, len(history["deliveries"]))
        self.assertEqual(1, len(history["acks"]))
        self.assertFalse(self.root.resolve_relative("receipts/consumed/recipient.jsonl").exists())

    def test_explicit_consumption_is_returned_as_its_authored_receipt(self):
        message = self.send("explicit-acted")
        self.events.present("recipient")
        self.ack(message)
        ledger = consumption_ledger(self, self.root)
        receipt = ledger.consume("recipient", message["id"], session_id="fixture-session")

        history = FleetProjection(self.root).receipts("recipient")
        self.assertEqual([receipt], history["consumptions"])
        self.assertEqual([receipt], ledger.records("recipient"))
        self.assertEqual(1, len(history["deliveries"]))
        self.assertEqual(1, len(history["acks"]))

    def test_progress_counts_unique_envelopes_across_repeated_delivery_and_consume(self):
        first, second = self.send("first"), self.send("second")
        self.events.present("recipient")
        self.events.present("recipient")
        self.ack(first)
        ledger = consumption_ledger(self, self.root)
        receipt = ledger.consume("recipient", first["id"], session_id="fixture-session")
        replay = ledger.consume("recipient", first["id"], session_id="fixture-session")

        self.assertEqual(receipt, replay)
        self.assertEqual(1, len(ledger.records("recipient")))
        self.assertEqual({"delivered": 2, "acknowledged": 1, "consumed": 1}, ledger.progress("recipient"))
        self.assertEqual(
            {"delivered": 1, "acknowledged": 0, "consumed": 0},
            ledger.progress("recipient", item_ids=[second["id"]]),
        )
        for sealed in (False, True):
            self.assertEqual({"delivered": 0, "acknowledged": 0, "consumed": 0},
                ledger.progress("recipient", item_ids=[], sealed=sealed))

    def test_cold_and_cached_status_reobserve_a_new_consumption_plane(self):
        first, second = self.send("cache-first"), self.send("cache-second")
        self.events.present("recipient")
        self.ack(first)
        projection = FleetProjection(self.root)
        before = projection.status_artifact(NOW)
        expected = {"delivered": 2, "acknowledged": 1, "consumed": 0}
        self.assertEqual(expected, self.node_progress(before))
        self.assertEqual(expected, self.node_progress(projection.status_artifact(NOW)))

        ledger = consumption_ledger(self, self.root)
        ledger.consume("recipient", first["id"], session_id="fixture-session")
        expected["consumed"] = 1
        self.assertEqual(expected, self.node_progress(projection.status_artifact(NOW)))
        self.assertEqual(expected, self.node_progress(FleetProjection(self.root).snapshot(NOW)))
        self.assertEqual(1, next(row for row in before["nodes"] if row["node_id"] == "recipient")["inbox_depth"])
        self.assertNotEqual(first["id"], second["id"])

    def test_old_or_malformed_cached_progress_requires_reprojection(self):
        projection = FleetProjection(self.root)
        projection.status_artifact(NOW)
        payload = projection._status_snapshot_store().load().payload
        for progress in (None, {"delivered": True, "acknowledged": 0, "consumed": 0},
                         {"delivered": 0, "acknowledged": 0, "consumed": 0, "inferred": 0}):
            with self.subTest(progress=progress):
                candidate = copy.deepcopy(payload)
                for node in candidate["artifact"]["nodes"]:
                    if progress is None:
                        node.pop("message_progress", None)
                    else:
                        node["message_progress"] = progress
                with self.assertRaises(SnapshotRefusal):
                    projection._status_from_snapshot(NOW, candidate, {})

    def test_live_harbor_model_refreshes_consumption_and_renders_three_axes(self):
        from floati.tui import model_from_root
        from floati.tui_render import render_plain_dump

        message = self.send("harbor-acted")
        self.events.present("recipient")
        self.ack(message)
        before = model_from_root(self.root, NOW)
        row = next(node for node in before.nodes if node["node_id"] == "recipient")
        self.assertEqual({"delivered": 1, "acknowledged": 1, "consumed": 0}, row["message_progress"])
        ledger = consumption_ledger(self, self.root)
        ledger.consume("recipient", message["id"], session_id="fixture-session")
        after = model_from_root(self.root, NOW)
        row = next(node for node in after.nodes if node["node_id"] == "recipient")
        self.assertEqual({"delivered": 1, "acknowledged": 1, "consumed": 1}, row["message_progress"])
        self.assertIn("D/A/C 1/1/1", render_plain_dump(after, width=160))

    def worker_message(self, label, *, acknowledge=False):
        session = "worker-projection-session"
        message = self.send(label, worker_session_id=session)
        self.events.present("recipient", worker_session_id=session)
        if acknowledge:
            SparseCursor(self.root).ack(
                "recipient", [message["id"]], acting_session_id=session,
                worker_session_id=session,
            )
        return message, session

    def test_cached_status_reobserves_nested_worker_consumption_at_fixed_time(self):
        message, session = self.worker_message("worker-cache-consume", acknowledge=True)
        projection = FleetProjection(self.root)
        self.assertEqual(
            {"delivered": 1, "acknowledged": 1, "consumed": 0},
            self.node_progress(projection.status_artifact(NOW)),
        )
        consumption_ledger(self, self.root).consume("recipient", message["id"], session_id=session)
        expected = {"delivered": 1, "acknowledged": 1, "consumed": 1}
        self.assertEqual(expected, self.node_progress(projection.status_artifact(NOW)))
        self.assertEqual(expected, self.node_progress(projection.snapshot(NOW)))

    def test_cached_status_reobserves_nested_worker_ack_at_fixed_time(self):
        message, session = self.worker_message("worker-cache-ack")
        projection = FleetProjection(self.root)
        self.assertEqual(
            {"delivered": 1, "acknowledged": 0, "consumed": 0},
            self.node_progress(projection.status_artifact(NOW)),
        )
        SparseCursor(self.root).ack(
            "recipient", [message["id"]], acting_session_id=session,
            worker_session_id=session,
        )
        expected = {"delivered": 1, "acknowledged": 1, "consumed": 0}
        self.assertEqual(expected, self.node_progress(projection.status_artifact(NOW)))
        self.assertEqual(expected, self.node_progress(projection.snapshot(NOW)))

    def test_cached_status_reobserves_nested_worker_delivery_at_fixed_time(self):
        session = "worker-delivery-session"
        self.send("worker-cache-delivery", worker_session_id=session)
        projection = FleetProjection(self.root)
        self.assertEqual(
            {"delivered": 0, "acknowledged": 0, "consumed": 0},
            self.node_progress(projection.status_artifact(NOW)),
        )
        self.events.present("recipient", worker_session_id=session)
        expected = {"delivered": 1, "acknowledged": 0, "consumed": 0}
        self.assertEqual(expected, self.node_progress(projection.status_artifact(NOW)))
        self.assertEqual(expected, self.node_progress(projection.snapshot(NOW)))

    def test_cached_harbor_reobserves_nested_worker_ack_and_consumption(self):
        from floati.tui import model_from_root

        message, session = self.worker_message("worker-harbor-cache")
        def progress():
            model = model_from_root(self.root, NOW)
            return next(node for node in model.nodes if node["node_id"] == "recipient")["message_progress"]
        self.assertEqual({"delivered": 1, "acknowledged": 0, "consumed": 0}, progress())
        SparseCursor(self.root).ack(
            "recipient", [message["id"]], acting_session_id=session,
            worker_session_id=session,
        )
        self.assertEqual({"delivered": 1, "acknowledged": 1, "consumed": 0}, progress())
        consumption_ledger(self, self.root).consume("recipient", message["id"], session_id=session)
        self.assertEqual({"delivered": 1, "acknowledged": 1, "consumed": 1}, progress())


class MessageConsumptionBoardProjectionTests(unittest.TestCase):
    write_map = board_fixture.SeatBoardTests.write_map
    send = board_fixture.SeatBoardTests.send
    arm_session = board_fixture.SeatBoardTests.arm_session
    board = board_fixture.SeatBoardTests.board

    def setUp(self):
        board_fixture.SeatBoardTests.setUp(self)

    def test_board_freezes_batch_progress_without_authoring_consumption(self):
        first = self.send("board-first")
        result = self.board()
        self.assertIn("message_progress", result, "boarding must receipt its frozen batch progress")
        self.assertEqual(
            {"delivered": 1, "acknowledged": 1, "consumed": 0}, result["message_progress"],
        )
        node = ids.builder("floati")
        ledger = consumption_ledger(self, self.root)
        self.assertEqual([], ledger.records(node))
        ledger.consume(node, first["id"], session_id="new-session")
        later = self.send("board-later")
        EventLog(self.root).present(node)
        SparseCursor(self.root).ack(node, [later["id"]], acting_session_id="new-session")
        ledger.consume(node, later["id"], session_id="new-session")

        self.assertEqual({"delivered": 2, "acknowledged": 2, "consumed": 2}, ledger.progress(node))
        self.assertEqual(result, self.board())
        self.assertEqual([first["id"]], result["item_ids"])

    def test_empty_board_records_zero_progress_and_no_consumption(self):
        result = self.board()
        self.assertIn("message_progress", result, "empty boarding still exposes the three receipt axes")
        self.assertEqual({"delivered": 0, "acknowledged": 0, "consumed": 0}, result["message_progress"])
        self.assertEqual([], result["item_ids"])
        self.assertFalse(self.root.resolve_relative("receipts/consumed").exists())

    def test_historical_board_rows_without_progress_replay_without_inference(self):
        from floati.records import validate_record
        from tests.schema_validation import validate_json_schema

        self.send("historical-board")
        result = self.board()
        path = self.root.resolve_relative("receipts/seat-board/" + ids.builder("floati") + ".jsonl")
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        for row in rows:
            row.pop("message_progress", None)
            validate_record(row, self.root.tenant_id, {"seat_board_receipt"}, integrity=False)
            validate_json_schema(row, Path("schemas/v1/seat-board-record.schema.json"))
        path.write_text("".join(json.dumps(row) + "\n" for row in rows))
        result.pop("message_progress", None)
        self.assertEqual(result, self.board())
        self.assertFalse(self.root.resolve_relative("receipts/consumed").exists())

    def test_board_record_cannot_claim_acted_without_authoring_consumption(self):
        from floati.errors import ProtocolRefusal
        from floati.records import validate_record
        from tests.schema_validation import SchemaValidationError, validate_json_schema

        self.send("board-no-inferred-acted")
        self.board()
        path = self.root.resolve_relative("receipts/seat-board/" + ids.builder("floati") + ".jsonl")
        record = json.loads(path.read_text().splitlines()[-1])
        self.assertEqual("completed", record["step"])
        record["message_progress"]["consumed"] = 1
        with self.assertRaises(ProtocolRefusal):
            validate_record(record, self.root.tenant_id, {"seat_board_receipt"}, integrity=False)
        with self.assertRaises(SchemaValidationError):
            validate_json_schema(record, Path("schemas/v1/seat-board-record.schema.json"))


if __name__ == "__main__":
    unittest.main()

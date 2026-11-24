"""Observational unknown is typed and never rendered as a measured zero."""
import copy
import json
from pathlib import Path
import unittest

from floati import fixture_ids as ids
from floati.errors import ProtocolRefusal
from floati.projection import FleetProjection
from floati.records import validate_record
from floati.tui_render import _message_progress_flag
from tests import test_seat_board as board_fixture
from tests.schema_validation import SchemaValidationError, validate_json_schema


UNKNOWN = {"state": "unknown", "reason_code": "ack_evidence_invalid",
           "delivered": None, "acknowledged": None, "consumed": None}


class UnknownProgressShapeTests(unittest.TestCase):
    def test_cache_accepts_unknown_but_never_a_partial_or_unsafe_unknown(self):
        self.assertTrue(FleetProjection._valid_message_progress(UNKNOWN))
        for change in ({"consumed": 0}, {"reason_code": "bad\ncode"},
                       {"state": "known"}, {"extra": True}):
            with self.subTest(change=change):
                self.assertFalse(FleetProjection._valid_message_progress(dict(UNKNOWN, **change)))

    def test_render_names_unknown_reason_and_never_prints_null_counts(self):
        rendered = _message_progress_flag({"message_progress": UNKNOWN})
        self.assertIn("UNKNOWN", rendered)
        self.assertIn("ack_evidence_invalid", rendered)
        self.assertNotIn("None", rendered)
        self.assertNotIn("0/0/0", rendered)


class UnknownBoardRecordTests(unittest.TestCase):
    write_map = board_fixture.SeatBoardTests.write_map
    send = board_fixture.SeatBoardTests.send
    arm_session = board_fixture.SeatBoardTests.arm_session
    board = board_fixture.SeatBoardTests.board
    setUp = board_fixture.SeatBoardTests.setUp

    def record(self):
        self.send("unknown-board-contract")
        self.board()
        path = self.root.resolve_relative("receipts/seat-board/" + ids.builder("floati") + ".jsonl")
        result = json.loads(path.read_text().splitlines()[-1])
        result["message_progress"] = copy.deepcopy(UNKNOWN)
        return result

    def test_completed_board_accepts_explicit_unknown_in_native_and_schema(self):
        row = self.record()
        validate_record(row, self.root.tenant_id, {"seat_board_receipt"}, integrity=False)
        validate_json_schema(row, Path("schemas/v1/seat-board-record.schema.json"))

    def test_unknown_board_cannot_claim_any_numeric_action_or_escape_completion(self):
        row = self.record()
        # Establish the valid arm before testing refusals; an always-refusing
        # validator cannot satisfy these controls.
        validate_record(row, self.root.tenant_id, {"seat_board_receipt"}, integrity=False)
        for change in ({"consumed": 1}, {"delivered": 0}, {"reason_code": "bad\ncode"}):
            invalid = copy.deepcopy(row)
            invalid["message_progress"].update(change)
            with self.subTest(change=change):
                with self.assertRaises(ProtocolRefusal):
                    validate_record(invalid, self.root.tenant_id, {"seat_board_receipt"}, integrity=False)
                with self.assertRaises(SchemaValidationError):
                    validate_json_schema(invalid, Path("schemas/v1/seat-board-record.schema.json"))
        row["step"] = "drain_started"
        with self.assertRaises(ProtocolRefusal):
            validate_record(row, self.root.tenant_id, {"seat_board_receipt"}, integrity=False)


if __name__ == "__main__":
    unittest.main()

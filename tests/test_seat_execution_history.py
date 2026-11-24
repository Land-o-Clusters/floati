"""Native execution histories refuse missing or inconsistent invocation proof."""
from copy import deepcopy
from datetime import datetime, timezone
import unittest

import tests
from floati.bus_epoch import ROLL_AUTHORITY_SUBJECT, roll_bus_epoch
from floati.errors import FloatiError
from floati.events import EventLog
from floati.framing import encode_frame
from floati.ids import uuid7_hex
from floati.planes import AuthorityGrantStore
from tests import test_seat_execution as fixture


PAYLOAD = {"event": "ended", "code": "process_died", "detail": "native cleanup completed"}


class SeatExecutionHistoryTests(unittest.TestCase):
    setUp = fixture.SeatExecutionTests.setUp
    car = fixture.SeatExecutionTests.car
    claim = fixture.SeatExecutionTests.claim
    controller = fixture.SeatExecutionTests.controller
    invocation = fixture.SeatExecutionTests.invocation

    def prepare(self, *, terminal=True):
        item = self.car()
        self.claim(item)
        controller = self.controller()
        invocation = self.invocation(controller, item)
        if terminal:
            controller.callback(invocation, PAYLOAD)
        return controller, invocation, EventLog(self.root).event_records()

    def replace_native_frames(self, rows):
        # These are deliberately corrupt, schema-valid native scratch frames,
        # not a public producer minting execution evidence. The authoritative
        # projection must refuse their inconsistent transcript semantics.
        (self.root.tenant_home / "events.jsonl").write_bytes(
            b"".join(encode_frame(row) for row in rows))

    def assert_unknown(self, rows):
        self.replace_native_frames(rows)
        state = self.controller().project("builder")
        self.assertEqual("UNKNOWN", state["state"])

    def test_terminal_without_its_native_running_start_is_unknown(self):
        unused, unused_invocation, rows = self.prepare()
        self.assert_unknown([row for row in rows if not (
            row["kind"] == "seat_execution" and row["event"] == "running")])

    def test_missing_start_cannot_return_a_successful_exact_replay_receipt(self):
        controller, invocation, rows = self.prepare()
        self.replace_native_frames([row for row in rows if not (
            row["kind"] == "seat_execution" and row["event"] == "running")])
        with self.assertRaises(FloatiError):
            controller.callback(invocation, PAYLOAD)

    def test_duplicate_native_close_is_unknown(self):
        unused, unused_invocation, rows = self.prepare()
        duplicate = deepcopy(rows[-1])
        duplicate["id"] = "seat-execution-" + uuid7_hex()
        self.assert_unknown(rows + [duplicate])

    def test_second_native_close_with_different_payload_is_unknown(self):
        unused, unused_invocation, rows = self.prepare()
        duplicate = deepcopy(rows[-1])
        duplicate.update(id="seat-execution-" + uuid7_hex(),
            code="process_timeout", detail="different terminal BODY")
        self.assert_unknown(rows + [duplicate])

    def test_duplicate_native_start_is_unknown(self):
        unused, unused_invocation, rows = self.prepare()
        start = deepcopy(next(row for row in rows if row["kind"] == "seat_execution"
                              and row["event"] == "running"))
        start["id"] = "seat-execution-" + uuid7_hex()
        self.assert_unknown([start] + rows)

    def test_native_start_after_a_closed_invocation_is_unknown(self):
        unused, unused_invocation, rows = self.prepare()
        start = deepcopy(next(row for row in rows if row["kind"] == "seat_execution"
                              and row["event"] == "running"))
        start["id"] = "seat-execution-" + uuid7_hex()
        self.assert_unknown(rows + [start])

    def test_one_invocation_cannot_change_native_identity_across_start_and_close(self):
        unused, unused_invocation, original = self.prepare()
        native_start = next(row for row in original if row["kind"] == "seat_execution"
                            and row["event"] == "running")
        changes = {
            "node_id": "other",
            "acting_session_id": "worker-replaced",
            "work_item_id": "work-" + uuid7_hex(),
            "work_claim_id": "transition-" + uuid7_hex(),
            "source_kind": "cursor",
            "source_receipt_id": "replaced-source",
            "source_pin": "c" * 64,
            "workspace": str(self.workspace / "changed-workspace"),
            "command": "floati worker run pi",
            "source_binding": {"run_id": "changed-run", "attempt_id": "changed-attempt",
                "binding_record_id": "changed-binding", "attempt_binding": {
                    "attempt_id": "changed-attempt", "claim_id": "changed-claim",
                    "lease_id": "changed-lease", "worker_session_id": native_start["acting_session_id"]}},
        }
        for field, replacement in changes.items():
            with self.subTest(field=field):
                rows = deepcopy(original)
                start = next(row for row in rows if row["kind"] == "seat_execution"
                             and row["event"] == "running")
                start[field] = replacement
                self.assert_unknown(rows)

    def test_running_invocation_closes_across_a_real_verified_epoch_boundary(self):
        controller, invocation, unused = self.prepare(terminal=False)
        AuthorityGrantStore(self.root).grant_exact(ROLL_AUTHORITY_SUBJECT,
            "architect", 1, datetime.now(timezone.utc))
        rolled = roll_bus_epoch(self.root, actor="architect",
            idempotency_key="running-native-car-epoch-roll")["receipt"]
        self.assertTrue((self.root.tenant_home / rolled["archive_path"]).is_dir())
        accepted = controller.callback(invocation, PAYLOAD)
        self.assertEqual("STALLED", self.controller().project("builder")["state"])
        current = EventLog(self.root).event_records()
        self.assertEqual(["ended"], [row["event"] for row in current if row["kind"] == "seat_execution"])
        self.assertEqual(accepted, controller.callback(invocation, PAYLOAD))
        self.assertEqual(current, EventLog(self.root).event_records())

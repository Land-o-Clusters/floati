"""The rehome history cutoff never acknowledges later scratch mail.

Unrun while fleet BUILDS HOLD is active. Only the large-batch receipt
boundaries are substituted; the race test uses native scratch ledgers.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest import mock

from floati.cursor import SparseCursor
from floati.events import EventLog
from floati.ids import uuid7_hex
from floati.records import validate_record
from floati.registry import Registry
from floati.waiter_consent import arm_waiter_consent
from tests import test_seat_onboard as fixtures


class SeatOnboardingHistoryTests(unittest.TestCase):
    setUp = fixtures.SeatOnboardingTests.setUp
    git = fixtures.SeatOnboardingTests.git
    cli = fixtures.SeatOnboardingTests.cli
    add = fixtures.SeatOnboardingTests.add

    def test_rehome_archived_cut_excludes_mail_arriving_after_snapshot(self):
        from floati import seat_waiter_lifecycle
        Registry(self.root).register("legacy", "Codex")
        old = self.base / "old-seat-home"
        old.mkdir()
        arm_waiter_consent(self.root.path, old, "legacy", "Codex", 30, 25)
        events = EventLog(self.root)
        original = events.send("architect", "legacy", "fixture", "a" * 40,
                               "README.md", "original prior-life dispatch",
                               idempotency_key="original-history")
        native_snapshot = seat_waiter_lifecycle.prior_life_mail
        fresh = []
        def snapshot_then_arrival(root, node):
            cut = native_snapshot(root, node)
            fresh.append(EventLog(root).send("architect", node, "fixture", "b" * 40,
                "README.md", "new dispatch after history snapshot", idempotency_key="fresh-arrival"))
            return cut
        with mock.patch.object(seat_waiter_lifecycle, "prior_life_mail", snapshot_then_arrival):
            code, output, errors = self.add("--node", "legacy", "--profile", "legacy-profile", "--rehome")
        self.assertEqual(0, code, errors or output)
        result = json.loads(output)["evidence"]
        self.assertEqual([original], result["prior_life_messages"])
        self.assertEqual("historical_acknowledged", result["prior_life_disposition"])
        self.assertEqual(1, len(fresh))
        self.assertEqual([fresh[0]["id"]], native_snapshot(self.root, "legacy"))
        self.assertEqual(frozenset({original["id"]}), SparseCursor(self.root).acked_ids("legacy"))
        record = json.loads(Path(result["ownership_record"]).read_bytes())
        self.assertEqual([original], record["prior_life_messages"])
        acknowledged = [item for receipt in record["historical_receipts"]
                        for item in receipt["acknowledgment"]["item_ids"]]
        delivered = [item for receipt in record["historical_receipts"]
                     for item in receipt["delivery"]["item_ids"]]
        self.assertEqual([original["id"]], acknowledged)
        self.assertEqual([original["id"]], delivered)
        self.assertNotIn(fresh[0]["id"], acknowledged)
        self.assertTrue(old.is_dir(), "rehome retains its unowned old home")

    def test_history_receipts_chunk_each_worker_session_without_loss_or_fresh_scan(self):
        from floati.seat_onboarding import _ack_history
        Registry(self.root).register("legacy", "Codex")
        template = EventLog(self.root).send("architect", "legacy", "fixture", "a" * 40,
            "README.md", "historical fixture template", idempotency_key="history-template")
        default_messages = [dict(template, id="msg-" + f"{index:012x}70008000{index:012x}", idempotency_key="default-" + str(index))
                            for index in range(1, 1006)]
        worker_messages = [dict(template, id="msg-" + f"{index:012x}70008000{index:012x}", worker_session_id="prior-worker",
                                idempotency_key="worker-" + str(index))
                           for index in range(2001, 3004)]
        messages = default_messages + worker_messages
        delivered_batches = []
        acknowledged_batches = []
        def receipt_delivery(log, recipient, batch, *, worker_session_id=None):
            self.assertIs(self.root, log.root)
            self.assertEqual("legacy", recipient)
            self.assertTrue(1 <= len(batch) <= 1000)
            self.assertTrue(all(row.get("worker_session_id") == worker_session_id for row in batch))
            ids = [row["id"] for row in batch]
            delivered_batches.append((worker_session_id, ids))
            receipt = {"schema_version": 0, "id": "delivery-" + uuid7_hex(),
                "tenant_id": self.root.tenant_id, "timestamp": "2026-10-01T00:00:00.000Z",
                "kind": "delivery_receipt", "recipient": recipient, "item_ids": ids,
                "presentation_count": len(delivered_batches)}
            validate_record(receipt, self.root.tenant_id, {"delivery_receipt"}, integrity=False)
            return receipt
        def receipt_ack(cursor, recipient, ids, *, acting_session_id, worker_session_id=None):
            self.assertIs(self.root, cursor.root)
            self.assertEqual("legacy", recipient)
            self.assertEqual("maintenance-history", acting_session_id)
            self.assertEqual((worker_session_id, ids), delivered_batches[-1])
            acknowledged_batches.append((worker_session_id, list(ids)))
            receipt = {"schema_version": 1, "id": "ack-" + uuid7_hex(),
                "tenant_id": self.root.tenant_id, "timestamp": "2026-10-01T00:00:00.000Z",
                "kind": "ack_receipt", "recipient": recipient, "item_ids": list(ids),
                "acting_session_id": acting_session_id, "node_lease_id": None,
                "node_lease_state_at_ack": "not_leased", "node_lease_expires_at": None}
            validate_record(receipt, self.root.tenant_id, {"ack_receipt"}, integrity=False)
            return receipt
        with mock.patch.object(EventLog, "_write_delivery_receipt", receipt_delivery),\
                mock.patch.object(SparseCursor, "_ack_already_guarded", receipt_ack),\
                mock.patch.object(EventLog, "present", side_effect=AssertionError("history must not rescan inbox")):
            receipts = _ack_history(self.root, "legacy", messages, "maintenance-history")
        self.assertEqual([1000, 5, 1000, 3], [len(ids) for _session, ids in delivered_batches])
        self.assertEqual([None, None, "prior-worker", "prior-worker"],
                         [session for session, _ids in delivered_batches])
        self.assertEqual(delivered_batches, acknowledged_batches)
        self.assertEqual(4, len(receipts))
        acknowledged = [item for receipt in receipts for item in receipt["acknowledgment"]["item_ids"]]
        self.assertEqual([row["id"] for row in messages], acknowledged)
        self.assertEqual(2008, len(set(acknowledged)))


if __name__ == "__main__":
    unittest.main()

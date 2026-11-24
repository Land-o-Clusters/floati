"""Exact native result ACK references against real delivery/consumption ledgers."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest

from floati.cursor import SparseCursor
from floati.errors import ProtocolRefusal
from floati.events import EventLog
from floati.ids import uuid7_hex
from floati.jsonl import append_record
from floati.message_consumption import MessageConsumption
from floati.planes import AuthorityGrantStore
from floati.registry import REGISTRY_KINDS, Registry
from floati.root import FloatiRoot
from floati.work import WorkLog
from tests.temp_roots import REAL_TEMP_ROOT


class AdhocConsumptionTests(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory(prefix="floati-exact-consumption-", dir=REAL_TEMP_ROOT)
        self.addCleanup(scratch.cleanup)
        self.root = FloatiRoot.open_direct_home(Path(scratch.name) / "fleet", create=True)
        self.now = datetime.now(timezone.utc)
        self.registry = Registry(self.root)
        for node in ("sender", "recipient"):
            self.registry.register(node, "codex")
        self.lease = {"schema_version": 0, "kind": "node_lease", "id": "lease-" + uuid7_hex(),
            "tenant_id": self.root.tenant_id, "timestamp": self.now.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "node_id": "recipient", "workspace": str(self.root.path / "nodes/recipient"),
            "expires_at": (self.now + timedelta(hours=1)).isoformat(timespec="milliseconds").replace("+00:00", "Z"), "state": "active"}
        append_record(self.root, self.registry.relative_path, self.lease, allowed_kinds=REGISTRY_KINDS)
        self.events, self.cursor = EventLog(self.root), SparseCursor(self.root)
        self.consumption = MessageConsumption(self.root)
        self.work = WorkLog(self.root)
        AuthorityGrantStore(self.root).claim("work-claims", "recipient", 60, 60, self.now)
        self.item = self.work.add("act on exact result mail", "recipient", [], now=self.now)
        self.work.claim(self.item["id"], "recipient", "work-claims", 1, now=self.now)

    def send(self):
        return self.events.send("sender", "recipient", "floati", "a" * 40,
            "docs/result.md", "result fixture", idempotency_key=uuid7_hex(), now=self.now)

    def ack(self, messages, session):
        self.events.present("recipient", now=self.now)
        return self.cursor.ack("recipient", [row["id"] for row in messages],
            acting_session_id=session, now=self.now)

    def exact(self, messages, acknowledgment, *, lease=None):
        return self.consumption.consume_many("recipient", [row["id"] for row in messages],
            "native-provider-session", work_item_id=self.item["id"], now=self.now,
            ack_receipt_ids={row["id"]: acknowledgment["id"] for row in messages},
            node_lease_id=self.lease["id"] if lease is None else lease)

    def ledger_bytes(self):
        path = self.root.resolve_relative("receipts/consumed/recipient.jsonl")
        return path.read_bytes() if path.exists() else None

    def refuses_without_write(self, function, code):
        before = self.ledger_bytes()
        failure = None
        try:
            function()
        except ProtocolRefusal as exc:
            failure = exc.code
        self.assertEqual(failure, code)
        self.assertEqual(self.ledger_bytes(), before)

    def test_native_result_selects_saved_ack_after_older_foreign_session_ack(self):
        message = self.send()
        older = self.ack([message], "older-foreign-session")
        native = self.ack([message], "native-provider-session")
        self.assertNotEqual(older["id"], native["id"])
        receipt = self.exact([message], native)[0]
        self.assertEqual(receipt["ack_receipt_id"], native["id"])
        self.assertEqual(receipt["acting_session_id"], native["acting_session_id"])
        self.assertEqual(receipt["work_item_id"], self.item["id"])

    def test_exact_ack_from_foreign_session_refuses_without_consumption(self):
        for session, lease in (("older-foreign-session", self.lease["id"]),
                               ("native-provider-session", "lease-" + uuid7_hex())):
            with self.subTest(session=session):
                message = self.send()
                acknowledgment = self.ack([message], session)
                self.refuses_without_write(
                    lambda: self.exact([message], acknowledgment, lease=lease),
                    "consume_ack_binding_mismatch")

    def test_conflicting_prior_consumption_refuses_before_neighbor_append(self):
        existing, neighbor = self.send(), self.send()
        older = self.ack([existing], "older-foreign-session")
        prior = self.consumption.consume("recipient", existing["id"], "older-foreign-session", now=self.now)
        self.assertEqual(prior["ack_receipt_id"], older["id"])
        native = self.ack([existing, neighbor], "native-provider-session")
        self.refuses_without_write(lambda: self.exact([neighbor, existing], native),
                                   "consume_idempotency_conflict")
        self.assertEqual(self.consumption.records("recipient"), [prior])

    def test_same_exact_consumption_replays_without_append(self):
        message = self.send()
        native = self.ack([message], "native-provider-session")
        first = self.exact([message], native)
        before = self.ledger_bytes()
        self.assertEqual(self.exact([message], native), first)
        self.assertEqual(self.ledger_bytes(), before)

    def test_historical_unspecified_ack_selection_keeps_oldest_receipt(self):
        message = self.send()
        older = self.ack([message], "older-foreign-session")
        self.ack([message], "native-provider-session")
        receipt = self.consumption.consume("recipient", message["id"], "another-session", now=self.now)
        self.assertEqual(receipt["ack_receipt_id"], older["id"])

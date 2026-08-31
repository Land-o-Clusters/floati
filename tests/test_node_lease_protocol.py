from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from floati.cursor import SparseCursor
from floati.errors import ProtocolRefusal
from floati.events import EventLog
from floati.ids import uuid7_hex
from floati.jsonl import append_record
from floati.registry import REGISTRY_KINDS, Registry
from floati.root import FloatiRoot
from floati.work import WorkLog
from tests.temp_roots import REAL_TEMP_ROOT


NOW = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)
SHA = "a" * 40


class NodeLeaseProtocolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
        self.addCleanup(self.temporary.cleanup)
        self.root = FloatiRoot.open_direct_home(
            Path(self.temporary.name) / "fleet", create=True
        )
        self.registry = Registry(self.root)
        self.registry.register("permanent", "Codex")
        self.registry.register("temporary", "Codex")
        self.lease_id = "lease-" + uuid7_hex()
        self.expires_at = NOW + timedelta(seconds=5)
        append_record(
            self.root,
            self.registry.relative_path,
            {
                "schema_version": 0,
                "id": self.lease_id,
                "tenant_id": self.root.tenant_id,
                "timestamp": NOW.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
                "kind": "node_lease",
                "node_id": "temporary",
                "workspace": str(self.root.path / "nodes" / "temporary"),
                "expires_at": self.expires_at.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
                "state": "active",
            },
            allowed_kinds=REGISTRY_KINDS,
        )
        self.events = EventLog(self.root)

    def send(self, sender: str, recipient: str, *, now: datetime, key: str) -> dict:
        return self.events.send(
            sender,
            recipient,
            "floati",
            SHA,
            "docs/evidence/lease.md",
            "lease boundary",
            idempotency_key=key,
            now=now,
        )

    def assert_expired(self, action) -> None:
        with self.assertRaises(ProtocolRefusal) as raised:
            action()
        self.assertEqual("node_lease_expired", raised.exception.code)
        self.assertFalse(raised.exception.detail.startswith("DRAFT - "))
        self.assertIn(self.lease_id, raised.exception.detail)

    def test_new_send_by_or_to_expired_node_refuses_but_exact_retry_survives(self) -> None:
        original = self.send(
            "permanent", "temporary", now=NOW + timedelta(seconds=1), key="before-expiry"
        )

        replay = self.send(
            "permanent", "temporary", now=NOW + timedelta(seconds=6), key="before-expiry"
        )
        self.assertEqual(original, replay)
        self.assert_expired(
            lambda: self.send(
                "permanent", "temporary", now=NOW + timedelta(seconds=6), key="to-expired"
            )
        )
        self.assert_expired(
            lambda: self.send(
                "temporary", "permanent", now=NOW + timedelta(seconds=6), key="by-expired"
            )
        )
        self.assertEqual([original], self.events.records())

    def test_expired_node_gets_no_fresh_delivery_but_late_ack_keeps_testimony(self) -> None:
        message = self.send(
            "permanent", "temporary", now=NOW + timedelta(seconds=1), key="delivery"
        )
        delivered, receipt = self.events.present(
            "temporary", now=NOW + timedelta(seconds=4)
        )
        self.assertEqual([message], delivered)
        self.assertIsNotNone(receipt)

        ack = SparseCursor(self.root).ack(
            "temporary",
            [message["id"]],
            acting_session_id="late-session",
            now=NOW + timedelta(seconds=6),
        )

        self.assertEqual(self.lease_id, ack["node_lease_id"])
        self.assertEqual("expired", ack["node_lease_state_at_ack"])
        self.assertEqual(
            self.expires_at.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            ack["node_lease_expires_at"],
        )

        later = self.send(
            "permanent", "temporary", now=NOW + timedelta(seconds=1), key="fresh-after"
        )
        self.assert_expired(
            lambda: self.events.present("temporary", now=NOW + timedelta(seconds=6))
        )
        self.assertNotEqual(message["id"], later["id"])

    def test_expired_node_cannot_claim_new_work_and_refusal_names_lease(self) -> None:
        work = WorkLog(self.root)
        item = work.add(
            "leased work", "temporary", [], now=NOW + timedelta(seconds=1)
        )

        self.assert_expired(
            lambda: work.claim(
                item["id"],
                "temporary",
                "temporary",
                1,
                now=NOW + timedelta(seconds=6),
            )
        )

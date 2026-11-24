"""DENIAL-RECEIPT-LEGACY-READ-1 RED: one legacy denial fails the timeline closed.

The live fleet carries a receipts/denials.jsonl record written before the
exact-record contract existed; `floati log --replay` refuses malformed
evidence (record_fields_invalid, exit 33) and no timeline is produced —
the reader fails closed on history. The property: the legacy record reads
as typed unknown/unavailable history (a read finding naming the ledger and
the governed record id, the other sources' events proceeding), every OTHER
validation failure still refuses, and the writer-side validator stays
strict. Every fixture here is a clone of the record shape on a scratch
root; the live root is never touched. Pattern: harbor commit 45440c95
(wake-receipt-legacy-read).
"""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from floati.errors import IntegrityFailure, ProtocolRefusal
from floati.ids import uuid7_hex
from floati.replay import ReplayTimeline
from floati.records import validate_record
from floati.root import FloatiRoot
from floati.work import WorkLog

NOW = datetime(2026, 10, 11, 12, 0, 0, tzinfo=timezone.utc)


def _stamp(minutes_before_now: float) -> str:
    moment = NOW - timedelta(minutes=minutes_before_now)
    return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")


class DenialReceiptLegacyReadTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name) / "root"
        self.root = FloatiRoot.open_direct_home(self.home, create=True)
        self.legacy_id = "denial-" + uuid7_hex()
        self.legacy_row = {
            "schema_version": 1,
            "id": self.legacy_id,
            "tenant_id": self.root.tenant_id,
            "timestamp": _stamp(90),
            "kind": "denial_receipt",
            # A field set from before the current exact-record contract:
            # the attempt reference did not exist yet.
            "claimed_sender": "sender-a",
            "claimed_recipient": "recipient-b",
            "reason_code": "unknown_sender",
        }

    def seed_denials(self, *rows: dict) -> Path:
        path = self.root.tenant_home / "receipts" / "denials.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row, sort_keys=True) + "\n")
        return path

    def seed_work_item(self) -> str:
        from floati.planes import AuthorityGrantStore

        self.registry_setup()
        grant = AuthorityGrantStore(self.root).claim(
            "work-claims", "sender-a", 60, 60, NOW)
        item = WorkLog(self.root).add(
            "legacy-read control", "sender-a", [], now=NOW
        )
        WorkLog(self.root).claim(
            item["id"], "sender-a", "work-claims", grant["epoch"], now=NOW)
        return str(item["id"])

    def registry_setup(self) -> None:
        from floati.registry import Registry

        registry = Registry(self.root)
        for node in ("sender-a", "recipient-b"):
            try:
                registry.register(node, "opencode")
            except ProtocolRefusal:
                pass

    def test_legacy_denial_reads_as_typed_unavailable_history(self):
        """THE RED: the timeline must not fail closed on the legacy record."""

        self.seed_work_item()
        self.seed_denials(self.legacy_row)
        artifact = ReplayTimeline.from_root(self.root).artifact()
        findings = artifact.get("read_findings")
        self.assertIsInstance(findings, list)
        self.assertEqual(1, len(findings))
        finding = findings[0]
        self.assertEqual("denial_receipt_legacy_unreadable", finding["code"])
        self.assertEqual("receipts/denials.jsonl", finding["ledger"])
        self.assertEqual(self.legacy_id, finding["record_id"])
        self.assertEqual("record_fields_invalid", finding["source_code"])
        # The unreadable ledger contributes no events: history is unavailable,
        # typed as such - and the other sources' events still proceed.
        self.assertEqual(0, artifact["counts"]["denial"])
        classes = {event["event_class"] for event in artifact["events"]}
        self.assertIn("claim", classes)

    def test_other_validation_failures_still_refuse(self):
        """Only the named legacy refusal converts; everything else refuses."""

        self.seed_work_item()
        path = self.seed_denials(self.legacy_row)
        with path.open("a", encoding="utf-8") as handle:
            handle.write("malformed\n")
        with self.assertRaises(IntegrityFailure):
            ReplayTimeline.from_root(self.root).artifact()

    def test_record_fields_invalid_on_another_source_still_refuses(self):
        """The conversion is scoped to the denials ledger, not to the code."""

        self.seed_work_item()
        self.seed_denials(self.legacy_row)
        refusals = self.root.tenant_home / "receipts" / "worker-refusals.jsonl"
        refusals.parent.mkdir(parents=True, exist_ok=True)
        stray = {
            "schema_version": 1,
            "id": "worker-refusal-" + uuid7_hex(),
            "tenant_id": self.root.tenant_id,
            "timestamp": _stamp(30),
            "kind": "worker_refusal",
            "not_a_known_field": True,
        }
        with refusals.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(stray, sort_keys=True) + "\n")
        with self.assertRaises(IntegrityFailure):
            ReplayTimeline.from_root(self.root).artifact()

    def test_writers_stay_strict(self):
        """The writer-side gate still refuses the cloned legacy shape."""

        # The writer gate raises the plain refusal; only the reader wraps it
        # as qualified IntegrityFailure.
        with self.assertRaises(ProtocolRefusal) as caught:
            validate_record(
                dict(self.legacy_row),
                self.root.tenant_id,
                {"denial_receipt"},
                integrity=False,
            )
        self.assertEqual("record_fields_invalid", caught.exception.code)


if __name__ == "__main__":
    unittest.main()

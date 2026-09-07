"""SKEW-2: unknown kinds in events.jsonl and receipts/ are typed skips.

MEASURED on tip 663293a3: an appended future event kind kills
`floati inbox --peek` and `floati send` with exit 33 malformed_evidence.
RED until those surfaces skip with a receipt naming kind,
reader_schema_version, first_id, and ledger.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from floati import fixture_ids as public_ids
from floati.cursor import SparseCursor
from floati.events import EventLog
from floati.framing import encode_frame
from floati.records import READER_VERSION
from floati.registry import Registry
from floati.root import FloatiRoot
from tests.temp_roots import REAL_TEMP_ROOT


FUTURE_EVENT_KIND = "future_event_kind_v99"
FUTURE_EVENT_ID = "msg-future-kind-000000000000000000000001"
FUTURE_RECEIPT_KIND = "future_receipt_kind_v99"
FUTURE_RECEIPT_ID = "receipt-future-kind-00000000000000000001"


class Skew2UnknownEventAndReceiptKindTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.root = FloatiRoot.open_direct_home(self.base / "fleet", create=True)
        Registry(self.root).register("sender", "Codex")
        Registry(self.root).register(public_ids.builder("a"), "Codex")
        self.log = EventLog(self.root)
        self.known = self.log.send(
            "sender",
            public_ids.builder("a"),
            "floati",
            "a" * 40,
            "docs/evidence/x.md",
            "skew2 known",
            idempotency_key="skew2-known",
        )

    def append_unknown_event(self) -> None:
        path = self.root.resolve_relative("events.jsonl")
        with path.open("ab") as handle:
            handle.write(
                encode_frame(
                    {
                        "schema_version": 0,
                        "id": FUTURE_EVENT_ID,
                        "tenant_id": self.root.tenant_id,
                        "timestamp": "2026-09-05T23:00:00.000Z",
                        "kind": FUTURE_EVENT_KIND,
                        "sender": "sender",
                        "recipient": public_ids.builder("a"),
                        "repo": "floati",
                        "sha": "a" * 40,
                        "doc": "docs/evidence/x.md",
                        "note": "unknown kind probe",
                        "idempotency_key": "skew2-unknown-event",
                    }
                )
            )

    def append_unknown_receipt(self) -> Path:
        relative = Path("receipts") / "foreign-future.jsonl"
        path = self.root.resolve_relative(relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("ab") as handle:
            handle.write(
                encode_frame(
                    {
                        "schema_version": 0,
                        "id": FUTURE_RECEIPT_ID,
                        "tenant_id": self.root.tenant_id,
                        "timestamp": "2026-09-05T23:00:01.000Z",
                        "kind": FUTURE_RECEIPT_KIND,
                        "node_id": public_ids.builder("a"),
                    }
                )
            )
        return relative

    def run_cli(self, *argv: str) -> tuple[int, dict]:
        completed = subprocess.run(
            [sys.executable, "-m", "floati", *argv],
            cwd=str(Path.cwd()),
            capture_output=True,
            text=True,
            check=False,
        )
        artifact: dict = {}
        if completed.stdout.strip():
            artifact = json.loads(completed.stdout.strip().splitlines()[-1])
        return completed.returncode, artifact

    def test_inbox_peek_survives_unknown_event_kind(self) -> None:
        """RED today: inbox --peek exits 33 malformed_evidence."""

        self.append_unknown_event()
        code, artifact = self.run_cli(
            "inbox",
            "--root",
            str(self.root.path),
            "--as",
            public_ids.builder("a"),
            "--peek",
        )
        self.assertNotEqual(33, code, artifact)
        self.assertNotEqual("malformed_evidence", artifact.get("status"), artifact)
        self.assertEqual("ok", artifact.get("status"), artifact)

    def test_send_survives_unknown_event_kind(self) -> None:
        """RED today: send exits 33 malformed_evidence after a future kind."""

        self.append_unknown_event()
        code, artifact = self.run_cli(
            "send",
            "--root",
            str(self.root.path),
            "--from",
            "sender",
            "--to",
            public_ids.builder("a"),
            "--repo",
            "floati",
            "--sha",
            "b" * 40,
            "--doc",
            "docs/evidence/x.md",
            "--note",
            "after unknown",
            "--idempotency-key",
            "skew2-after-unknown",
        )
        self.assertNotEqual(33, code, artifact)
        self.assertNotEqual("malformed_evidence", artifact.get("status"), artifact)
        self.assertEqual("ok", artifact.get("status"), artifact)

    def test_event_skip_receipt_names_kind_reader_first_id_ledger(self) -> None:
        """RED today: no skip receipt surface for events.jsonl."""

        self.append_unknown_event()
        receipt = self.log.skip_receipt()
        self.assertIsInstance(receipt, dict)
        assert isinstance(receipt, dict)
        self.assertEqual(FUTURE_EVENT_KIND, receipt["kind"])
        self.assertEqual(READER_VERSION, receipt["reader_schema_version"])
        self.assertEqual(FUTURE_EVENT_ID, receipt["first_id"])
        self.assertEqual("events.jsonl", receipt["ledger"])

    def test_operational_event_records_skip_unknown_kinds(self) -> None:
        """Operational reads keep known frames and skip the future kind."""

        self.append_unknown_event()
        rows = self.log.event_records()
        ids = [row["id"] for row in rows]
        self.assertIn(self.known["id"], ids)
        self.assertNotIn(FUTURE_EVENT_ID, ids)

    def test_receipt_ledger_compatible_read_skips_unknown_kind(self) -> None:
        """Unknown kinds under receipts/ must not raise record_kind_invalid."""

        relative = self.append_unknown_receipt()
        from floati.jsonl import read_records_compatible_snapshot

        rows, unrecognized = read_records_compatible_snapshot(
            self.root,
            relative,
            allowed_kinds={"ack_receipt", "delivery_receipt", "wake_hold_receipt"},
        )
        self.assertEqual([], rows)
        self.assertEqual(1, len(unrecognized))
        self.assertEqual(FUTURE_RECEIPT_KIND, unrecognized[0]["kind"])
        self.assertEqual(FUTURE_RECEIPT_ID, unrecognized[0]["first_id"])

    def test_doctor_older_readers_covers_events_vocabulary(self) -> None:
        """Doctor must name older_readers when events carry a future kind."""

        self.append_unknown_event()
        from floati.doctor import Doctor

        artifact, _return_code = Doctor(
            Path.cwd(), self.root.path, ref="HEAD", no_sandbox=True
        ).artifact()
        unrecognized = artifact.get("unrecognized_kinds") or []
        self.assertTrue(
            any(row.get("kind") == FUTURE_EVENT_KIND for row in unrecognized),
            artifact,
        )
        findings = artifact.get("findings") or []
        skew_findings = [
            row for row in findings if row.get("code") == "version_skew"
        ]
        self.assertEqual(1, len(skew_findings), artifact)
        older = skew_findings[0].get("older_readers") or []
        self.assertEqual(1, len(older), skew_findings[0])
        self.assertEqual(READER_VERSION, older[0]["reader_schema_version"])
        self.assertEqual(FUTURE_EVENT_KIND, older[0]["ledger_newest_kind"])


if __name__ == "__main__":
    unittest.main()

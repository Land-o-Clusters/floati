from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from floati.errors import ProtocolRefusal
from floati.root import FloatiRoot
from floati.state_receipts import (
    StateFileFlushReceipt,
    record_state_flush,
    render_state_receipt,
)
from tests.schema_validation import validate_json_schema


RECEIPT_ID = "018f7e9b3c137abc8def0123456789ab"
NOW = datetime(2026, 8, 27, 23, 45, 0, 123000, tzinfo=timezone.utc)


class StateFileFlushReceiptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = FloatiRoot.open_direct_home(
            Path(self.temporary.name) / "fleet", create=True
        )
        self.state_file = self.root.path / "nodes" / "builder-a" / "STATE.md"
        self.state_file.parent.mkdir(parents=True)

    def _write_state(self, payload: bytes = b"opaque seat state\n", mtime_ns: int = 2_000_000_000) -> None:
        self.state_file.write_bytes(payload)
        os.utime(self.state_file, ns=(mtime_ns, mtime_ns))

    def _record(self, *, prior_mtime_ns: int | None = None) -> dict[str, object]:
        return record_state_flush(
            self.root,
            "builder-a",
            prior_mtime_ns=prior_mtime_ns,
            id_factory=lambda: RECEIPT_ID,
            now=lambda: NOW,
        )

    def test_metadata_receipt_is_schema_shaped_and_deterministic(self) -> None:
        self._write_state()

        receipt = self._record(prior_mtime_ns=1_000_000_000)

        self.assertEqual(
            {
                "schema_version",
                "id",
                "tenant_id",
                "timestamp",
                "kind",
                "node_id",
                "state_file",
                "operation",
                "observed_mtime_ns",
                "observed_size_bytes",
                "prior_mtime_ns",
            },
            set(receipt),
        )
        self.assertEqual("node-state-flush-" + RECEIPT_ID, receipt["id"])
        self.assertEqual("2026-08-27T23:45:00.123Z", receipt["timestamp"])
        self.assertEqual(str(self.state_file), receipt["state_file"])
        self.assertEqual(2_000_000_000, receipt["observed_mtime_ns"])
        self.assertEqual(18, receipt["observed_size_bytes"])
        self.assertEqual(1_000_000_000, receipt["prior_mtime_ns"])

        schema = Path(__file__).parents[1] / "schemas" / "v0" / "node-state-file-receipt.schema.json"
        validate_json_schema(receipt, schema)

    def test_receipt_requires_observed_mtime_to_advance_past_prior_flush(self) -> None:
        self._write_state(mtime_ns=2_000_000_000)

        with self.assertRaises(ProtocolRefusal) as raised:
            self._record(prior_mtime_ns=2_000_000_000)
        self.assertEqual("state_receipt_mtime_not_newer", raised.exception.code)

    def test_missing_symlinked_nonregular_and_escaping_vessels_refuse(self) -> None:
        with self.assertRaises(ProtocolRefusal) as missing:
            self._record()
        self.assertEqual("state_receipt_missing", missing.exception.code)

        outside = Path(self.temporary.name) / "outside-state.md"
        outside.write_bytes(b"outside")
        self.state_file.symlink_to(outside)
        with self.assertRaises(ProtocolRefusal) as symlinked:
            self._record()
        self.assertEqual("state_receipt_symlink", symlinked.exception.code)

        self.state_file.unlink()
        self.state_file.mkdir()
        with self.assertRaises(ProtocolRefusal) as nonregular:
            self._record()
        self.assertEqual("state_receipt_not_regular", nonregular.exception.code)

        self.state_file.rmdir()
        self.state_file.parent.rmdir()
        outside_workspace = Path(self.temporary.name) / "outside-workspace"
        outside_workspace.mkdir()
        (self.root.path / "nodes" / "builder-a").symlink_to(
            outside_workspace, target_is_directory=True
        )
        with self.assertRaises(ProtocolRefusal) as parent_symlinked:
            self._record()
        self.assertEqual("state_receipt_symlink", parent_symlinked.exception.code)

        with self.assertRaises(ProtocolRefusal) as escaping:
            StateFileFlushReceipt(self.root, "../outside")
        self.assertEqual("node_invalid", escaping.exception.code)

    def test_opaque_state_bytes_are_not_read_or_changed_by_receipt_observation(self) -> None:
        payload = b"private\xff\x00bytes\n"
        self._write_state(payload, mtime_ns=3_000_000_000)
        before = self.state_file.read_bytes()

        with mock.patch("floati.state_receipts.os.read", side_effect=AssertionError("content read")):
            receipt = self._record()

        self.assertEqual(payload, before)
        self.assertEqual(payload, self.state_file.read_bytes())
        self.assertEqual(len(payload), receipt["observed_size_bytes"])

    def test_json_and_draft_rendering_expose_metadata_without_state_content(self) -> None:
        payload = b"do not echo this state\n"
        self._write_state(payload, mtime_ns=4_000_000_000)
        recorder = StateFileFlushReceipt(
            self.root,
            "builder-a",
            id_factory=lambda: RECEIPT_ID,
            now=lambda: NOW,
        )
        receipt = recorder.record()

        self.assertEqual(receipt, json.loads(recorder.to_json(receipt)))
        rendered = render_state_receipt(receipt)
        rendered.encode("ascii")
        self.assertIn("NODE STATE FLUSH RECEIPT", rendered)
        self.assertIn("OPERATION: flush", rendered)
        self.assertIn("OBSERVED SIZE BYTES: 23", rendered)
        self.assertNotIn("do not echo this state", rendered)


if __name__ == "__main__":
    unittest.main()

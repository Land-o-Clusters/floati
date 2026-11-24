"""F2 receipts producer REDs on a disposable bus and HOME."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from floati.confluence import ConfluenceGrantLedger, materialize_bundle
from floati.errors import ProtocolRefusal
from floati.framing import encode_frame
from floati.jsonl import append_record
from floati.registry import Registry
from floati.root import FloatiRoot
from floati.wake_hold import wake_hold_receipt
from floati.records import WAKE_HOLD_KINDS
from tests.schema_validation import validate_json_schema
from tests.temp_roots import REAL_TEMP_ROOT
from tests.test_confluence import observe_bundle_ledger_byte_reads


REPOSITORY_ROOT = Path(__file__).parents[1]
MESSAGE_ID = "msg-018f7e9b3c117abc8def0123456789ab"
RECIPIENT = "builder-b"
STAMP = "2026-09-28T12:00:00.000Z"


class ReceiptsReadV2Tests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        home = self.base / "home"
        home.mkdir()
        patcher = mock.patch.dict(os.environ, {"HOME": str(home)})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.root = FloatiRoot.open_direct_home(self.base / "fleet", create=True)
        self.grant = ConfluenceGrantLedger(self.root).grant("puddle", "f2-grant")

    def _seed_message(self, *, hold: bool = False, ack: bool = False,
                      retract: bool = False) -> None:
        envelope = {
            "schema_version": 0, "id": MESSAGE_ID,
            "tenant_id": self.root.tenant_id, "timestamp": STAMP,
            "kind": "message_envelope", "sender": "builder-a",
            "recipient": RECIPIENT, "repo": "floati", "sha": "a" * 40,
            "doc": "docs/CONFLUENCE-v0.md", "note": "F2 scratch",
            "idempotency_key": "f2-send", "attempt_binding": "absent_legacy",
        }
        if retract:
            envelope["worker_session_id"] = "f2-scratch-session"
        append_record(self.root, "events.jsonl", envelope,
                      allowed_kinds={"message_envelope"})
        if retract:
            retraction = {
                "schema_version": 0,
                "id": "ret-018f7e9b3c157abc8def0123456789ab",
                "tenant_id": self.root.tenant_id, "timestamp": STAMP,
                "kind": "message_retracted",
                "retracted_message_id": MESSAGE_ID,
                "worker_session_id": "f2-scratch-session",
                "reason": "superseded_by_correction", "author": "builder-a",
            }
            append_record(self.root, "events.jsonl", retraction,
                          allowed_kinds={"message_envelope", "message_retracted"})
            # This worker-bound message is retracted before delivery. Do not
            # fabricate an unrelated ordinary-inbox delivery for it.
            return
        delivery = {
            "schema_version": 0, "id": "delivery-018f7e9b3c127abc8def0123456789ab",
            "tenant_id": self.root.tenant_id, "timestamp": STAMP,
            "kind": "delivery_receipt", "recipient": RECIPIENT,
            "item_ids": [MESSAGE_ID], "presentation_count": 1,
        }
        relative = f"receipts/deliveries/{RECIPIENT}.jsonl"
        append_record(self.root, relative, delivery,
                      allowed_kinds=set(WAKE_HOLD_KINDS))
        if hold:
            receipt = wake_hold_receipt(
                tenant_id=self.root.tenant_id, recipient=RECIPIENT,
                worker_session_id=None, idempotency_key="f2-hold",
                limit=1, item_ids=[MESSAGE_ID],
                event_prefix_digest="a" * 64,
                delivery_prefix_digest="b" * 64,
                acknowledgment_prefix_digest="c" * 64,
                now=STAMP,
                record_id="wake-hold-018f7e9b3c137abc8def0123456789ab",
            )
            with self.root.resolve_relative(relative).open("ab") as handle:
                handle.write(encode_frame(receipt))
        if ack:
            acknowledgment = {
                "schema_version": 1,
                "id": "ack-018f7e9b3c147abc8def0123456789ab",
                "tenant_id": self.root.tenant_id, "timestamp": STAMP,
                "kind": "ack_receipt", "recipient": RECIPIENT,
                "acting_session_id": "f2-scratch-session",
                "item_ids": [MESSAGE_ID],
            }
            append_record(self.root, f"receipts/acks/{RECIPIENT}.jsonl",
                          acknowledgment, allowed_kinds={"ack_receipt"})

    def _bundle(self) -> dict:
        output = self.base / "receipts.json"
        materialize_bundle(self.root, consumer="puddle", out=output, version=2)
        document = json.loads(output.read_text(encoding="utf-8"))
        return document

    def test_hold_is_excluded_and_counted(self) -> None:
        self._seed_message(hold=True)
        document = self._bundle()
        self.assertEqual(2, document["schema_version"])
        self.assertEqual(1, document["excluded_wake_hold_receipt_count"])
        self.assertEqual(0, sum(entry["record"]["kind"] == "wake_hold_receipt"
                                for entry in document["entries"]))
        validate_json_schema(document, REPOSITORY_ROOT /
                             "schemas/v0/receipts-read-bundle-v2.schema.json")

    def test_complete_v2_bundle_reads_release_ledger_lock_but_preserve_epoch_and_bytes(self) -> None:
        self._seed_message(hold=True, ack=True)
        baseline = materialize_bundle(self.root, consumer="puddle", out=self.base / "baseline.json", version=2).read_bytes()
        with observe_bundle_ledger_byte_reads(self.root) as reads:
            output = materialize_bundle(self.root, consumer="puddle", out=self.base / "observed.json", version=2)
        prior, current = json.loads(baseline), json.loads(output.read_bytes())
        # The optional fleet projection has an export observation clock;
        # the pre-existing durable fields retain their exact bytes.
        for document in (prior, current):
            document["fleet_state"].pop("observed_at")
        self.assertEqual(json.dumps(prior).encode(), json.dumps(current).encode(),
                         "v2 event/receipt ordering and hold exclusions must remain exact")
        self.assertEqual({"confluence/grants.jsonl", "events.jsonl", "receipts/deliveries/builder-b.jsonl",
                          "receipts/acks/builder-b.jsonl"}, {path for path, _released, _protected in reads})
        self.assertTrue(all(released for _path, released, _protected in reads),
                        "complete v2 ledger reads must release their ledger lock before payload IO: " + repr(reads))
        self.assertTrue(all(protected for _path, _released, protected in reads),
                        "native epoch replacement must remain excluded during v2 snapshot IO: " + repr(reads))

    def test_envelope_carries_sender_and_zero_exclusions(self) -> None:
        self._seed_message()
        document = self._bundle()
        self.assertEqual(0, document["excluded_wake_hold_receipt_count"])
        envelopes = [entry["record"] for entry in document["entries"]
                     if entry["record"]["kind"] == "message_envelope"]
        self.assertEqual(1, len(envelopes))
        self.assertEqual((MESSAGE_ID, "builder-a", RECIPIENT, STAMP),
                         tuple(envelopes[0][key] for key in
                               ("id", "sender", "recipient", "timestamp")))
        self.assertEqual(1, sum(entry["record"]["kind"] == "delivery_receipt"
                                for entry in document["entries"]))
        validate_json_schema(document, REPOSITORY_ROOT /
                             "schemas/v0/receipts-read-bundle-v2.schema.json")

    def test_actor_bound_ack_validates_in_v2(self) -> None:
        self._seed_message(ack=True)
        document = self._bundle()
        self.assertEqual(1, sum(entry["record"]["kind"] == "ack_receipt"
                                for entry in document["entries"]))
        validate_json_schema(document, REPOSITORY_ROOT /
                             "schemas/v0/receipts-read-bundle-v2.schema.json")

    def test_retraction_is_carried_with_its_envelope(self) -> None:
        self._seed_message(retract=True)
        document = self._bundle()
        event_entries = [entry for entry in document["entries"]
                         if entry["source"] == "events.jsonl"]
        self.assertEqual(["message_envelope", "message_retracted"],
                         [entry["record"]["kind"] for entry in event_entries])
        self.assertEqual(MESSAGE_ID,
                         event_entries[1]["record"]["retracted_message_id"])
        validate_json_schema(document, REPOSITORY_ROOT /
                             "schemas/v0/receipts-read-bundle-v2.schema.json")

    def test_cli_produces_v2_on_scratch_bus(self) -> None:
        self._seed_message(hold=True, ack=True)
        registry = Registry(self.root)
        registry.register("builder-a", "Codex")
        registry.register("builder-b", "Claude")
        registry.register("retired-builder", "Codex")
        registry.retire("retired-builder")

        def snapshot():
            return {path.relative_to(self.root.path): path.read_bytes()
                    for path in self.root.path.rglob("*") if path.is_file()}

        before = snapshot()
        output = self.base / "cli-v2.json"
        completed = subprocess.run(
            [sys.executable, "-m", "floati", "confluence", "bundle",
             "--root", str(self.root.path), "--consumer", "puddle",
             "--schema-version", "2", "--out", str(output)],
            cwd=REPOSITORY_ROOT, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            text=True, capture_output=True, timeout=30,
        )
        self.assertEqual(0, completed.returncode, completed.stderr)
        document = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(2, document["schema_version"])
        self.assertEqual(1, document["excluded_wake_hold_receipt_count"])
        self.assertEqual(3, len(document["entries"]))
        self.assertEqual(["builder-a", "builder-b"],
                         [row["node_id"] for row in document["roster"]])
        validate_json_schema(document, REPOSITORY_ROOT /
                             "schemas/v0/receipts-read-bundle-v2.schema.json")
        self.assertEqual(before, snapshot())

    def test_no_grant_and_inside_root_refuse_before_write(self) -> None:
        output = self.base / "refused.json"
        ConfluenceGrantLedger(self.root).revoke("puddle", "f2-revoke")
        with self.assertRaises(ProtocolRefusal) as caught:
            materialize_bundle(self.root, consumer="puddle", out=output, version=2)
        self.assertEqual("confluence_grant_required", caught.exception.code)
        self.assertFalse(output.exists())
        ConfluenceGrantLedger(self.root).grant("puddle", "f2-regrant")
        inside = self.root.path / "bundle.json"
        with self.assertRaises(ProtocolRefusal) as caught:
            materialize_bundle(self.root, consumer="puddle", out=inside, version=2)
        self.assertEqual("confluence_out_invalid", caught.exception.code)
        self.assertFalse(inside.exists())


if __name__ == "__main__":
    unittest.main()

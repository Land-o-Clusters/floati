from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from floati import fixture_ids as public_ids
from floati.doctor import Doctor
from floati.errors import IntegrityFailure, ProtocolRefusal
from floati.events import EVENT_KINDS, EventLog
from floati.framing import encode_frame
from floati.jsonl import append_record, read_records, transact, transact_records
from floati.planes import LivenessPresenceStore
from floati.registry import Registry
from floati.root import FloatiRoot
from floati.verification import DeliveryVerifier
from tests.schema_validation import SchemaValidationError, validate_json_schema


REMEDY = "this ledger contains records from a newer floati; update the reading installation"
NOW = datetime(2026, 8, 29, 12, 0, tzinfo=timezone.utc)


class VersionSkewGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = FloatiRoot.open_direct_home(
            Path(self.temporary.name) / "version-skew", create=True
        )
        registry = Registry(self.root)
        registry.register(public_ids.builder("coordinator"), "architect")
        registry.register(public_ids.worker("target"), "worker")
        LivenessPresenceStore(self.root).observe(public_ids.builder("coordinator"), 3600, NOW)
        LivenessPresenceStore(self.root).observe(public_ids.worker("target"), 3600, NOW)
        self.log = EventLog(self.root)
        sent = self.log.send(
            public_ids.builder("coordinator"),
            public_ids.worker("target"),
            "floati",
            "a" * 40,
            "docs/evidence/known-mail.md",
            "known mail survives reader skew",
            idempotency_key="version-skew-known-mail",
        )
        self.known_message = dict(sent["message"])

    def future_record(self) -> dict[str, object]:
        return {
            "schema_version": 2,
            "id": "future-receipt-01a04900000070008000000000000000",
            "tenant_id": self.root.tenant_id,
            "timestamp": "2026-08-29T12:00:00.000Z",
            "kind": "future_receipt",
            "payload": {"newer": True},
        }

    def append_raw_event(self, record: object) -> None:
        self.log.path.parent.mkdir(parents=True, exist_ok=True)
        with self.log.path.open("ab") as handle:
            handle.write(encode_frame(record))

    def assert_skew_fact(self, fact: object) -> None:
        self.assertIsInstance(fact, dict)
        assert isinstance(fact, dict)
        self.assertEqual(
            {
                "state",
                "reader_version",
                "ledger_version",
                "unknown_kinds",
                "remedy",
                "observed_at",
            },
            set(fact),
        )
        self.assertEqual("version_skew", fact["state"])
        self.assertEqual("0", fact["reader_version"])
        self.assertEqual("2", fact["ledger_version"])
        self.assertEqual(["future_receipt"], fact["unknown_kinds"])
        self.assertEqual(REMEDY, fact["remedy"])
        observed_at = fact["observed_at"]
        self.assertIsInstance(observed_at, str)
        assert isinstance(observed_at, str)
        self.assertTrue(observed_at.endswith("Z"))
        self.assertEqual(
            timezone.utc,
            datetime.fromisoformat(observed_at.replace("Z", "+00:00")).tzinfo,
        )

    def test_compatible_inbox_delivers_known_mail_with_stamped_skew_fact(self) -> None:
        """Catches reader skew darkening known deliverable mail."""
        self.append_raw_event(self.future_record())

        messages, receipt, skew = self.log.present_compatible(public_ids.worker("target"))

        self.assertEqual([self.known_message], messages)
        self.assertIsNotNone(receipt)
        assert receipt is not None
        self.assertEqual("delivery_receipt", receipt["kind"])
        self.assertEqual([self.known_message["id"]], receipt["item_ids"])
        self.assertEqual(
            receipt,
            read_records(
                self.root,
                public_ids.compose("receipts/deliveries/", public_ids.ledger(public_ids.worker("target"))),
                allowed_kinds={"delivery_receipt", "wake_hold_receipt"},
            )[-1],
        )
        self.assert_skew_fact(skew)

    def test_strict_event_records_rejects_foreign_kind(self) -> None:
        """Catches an authoritative event read silently tolerating a future kind."""
        self.append_raw_event(self.future_record())

        with self.assertRaises(IntegrityFailure) as raised:
            self.log.event_records()

        self.assertEqual("record_kind_invalid", raised.exception.code)
        self.assertIn("future_receipt", raised.exception.detail)

    def test_explicit_verification_rejects_foreign_kind_before_claim_lookup(self) -> None:
        """Catches explicit verification bypassing strict event-ledger validation."""
        self.append_raw_event(self.future_record())

        with self.assertRaises(IntegrityFailure) as raised:
            DeliveryVerifier(self.root).verify(
                public_ids.builder("coordinator"),
                "delivery-claim-01a04900000370008000000000000000",
            )

        self.assertEqual("record_kind_invalid", raised.exception.code)
        self.assertIn("future_receipt", raised.exception.detail)

    def test_compatible_reader_keeps_malformed_known_record_fatal(self) -> None:
        """Catches malformed current vocabulary being mislabeled as version skew."""
        self.append_raw_event(
            {
                "schema_version": 0,
                "id": "msg-01a04900000170008000000000000000",
                "tenant_id": self.root.tenant_id,
                "timestamp": "2026-08-29T12:00:00.000Z",
                "kind": "message_envelope",
            }
        )

        with self.assertRaises(IntegrityFailure) as raised:
            self.log.compatible_event_records()

        self.assertEqual("record_fields_invalid", raised.exception.code)
        self.assertIn("message_envelope", raised.exception.detail)

    def test_writers_refuse_disallowed_candidates_before_appending_bytes(self) -> None:
        """Catches one writer path appending a foreign record before refusing it."""
        foreign = self.future_record()
        valid = dict(self.known_message)
        valid["id"] = "msg-01a04900000270008000000000000000"
        valid["idempotency_key"] = "version-skew-batch-valid"

        operations = (
            ("append_record", lambda: append_record(
                self.root,
                "events.jsonl",
                foreign,
                allowed_kinds=set(EVENT_KINDS),
            )),
            ("transact", lambda: transact(
                self.root,
                "events.jsonl",
                lambda _rows: (None, foreign),
                allowed_kinds=set(EVENT_KINDS),
            )),
            ("transact_records", lambda: transact_records(
                self.root,
                "events.jsonl",
                lambda _rows: (None, (valid, foreign)),
                allowed_kinds=set(EVENT_KINDS),
            )),
        )
        for name, operation in operations:
            with self.subTest(operation=name):
                before = self.log.path.read_bytes()
                with self.assertRaises(ProtocolRefusal) as raised:
                    operation()
                self.assertEqual("record_kind_invalid", raised.exception.code)
                self.assertEqual(
                    "record kind is not permitted by this ledger",
                    raised.exception.detail,
                )
                self.assertEqual(before, self.log.path.read_bytes())

    def test_doctor_retains_known_findings_and_reports_version_skew(self) -> None:
        """Catches doctor hiding a future ledger vocabulary or discarding known health."""
        messages, receipt = self.log.present(public_ids.worker("target"))
        self.assertEqual([self.known_message], messages)
        self.assertIsNotNone(receipt)
        self.append_raw_event(self.future_record())

        artifact, _return_code = Doctor(Path.cwd(), self.root.path, ref="HEAD").artifact()

        self.assertEqual(str(self.root.path), artifact["root"])
        root_valid = next(row for row in artifact["findings"] if row["code"] == "root_valid")
        self.assertEqual(str(self.root.path), root_valid["subject"])
        self.assertIn(
            "registry_live_dirs_match",
            [row["code"] for row in artifact["findings"]],
        )
        finding = next(row for row in artifact["findings"] if row["code"] == "version_skew")
        self.assertEqual("warning", finding["severity"])
        self.assertEqual(str(self.root.path), finding["subject"])
        self.assertEqual("installation predates records in this ledger", finding["detail"])
        self.assertEqual(REMEDY, finding["remediation"])
        self.assert_skew_fact(finding["vocabulary_skew"])

    def test_doctor_schema_requires_a_nonempty_remedy_without_pinning_its_words(self) -> None:
        """Catches the v1 schema freezing prose or allowing absent repair guidance."""
        self.append_raw_event(self.future_record())
        artifact, _return_code = Doctor(Path.cwd(), self.root.path, ref="HEAD").artifact()
        finding = next(row for row in artifact["findings"] if row["code"] == "version_skew")
        schema = Path("schemas/v1/doctor-artifact.schema.json")

        alternate = dict(artifact)
        alternate_finding = dict(finding)
        alternate_skew = dict(finding["vocabulary_skew"])
        alternate_skew["remedy"] = "install a compatible reader"
        alternate_finding["vocabulary_skew"] = alternate_skew
        alternate["findings"] = [
            alternate_finding if row is finding else row
            for row in artifact["findings"]
        ]
        validate_json_schema(alternate, schema)

        for remedy in ("", None):
            with self.subTest(remedy=remedy):
                refused = dict(alternate)
                refused_finding = dict(alternate_finding)
                refused_skew = dict(alternate_skew)
                if remedy is None:
                    refused_skew.pop("remedy")
                else:
                    refused_skew["remedy"] = remedy
                refused_finding["vocabulary_skew"] = refused_skew
                refused["findings"] = [
                    refused_finding if row is alternate_finding else row
                    for row in alternate["findings"]
                ]
                with self.assertRaises(SchemaValidationError):
                    validate_json_schema(refused, schema)

        for schema_path in Path("schemas/v1").iterdir():
            if schema_path.is_file():
                self.assertNotIn(REMEDY.encode("utf-8"), schema_path.read_bytes())


if __name__ == "__main__":
    unittest.main()

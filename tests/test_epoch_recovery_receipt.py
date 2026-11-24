"""Closed v1 rollback receipt contract; no bus or ledger operations."""

from __future__ import annotations

import unittest
from pathlib import Path

import tests  # Pin HOME before product imports; every path below is fixture data.

from floati.errors import IntegrityFailure, ProtocolRefusal
from floati.records import validate_record
from tests.schema_validation import SchemaValidationError, validate_json_schema


KIND = "bus_epoch_recovery_receipt"
TENANT = "fixture-fleet"
KINDS = frozenset({KIND})
SCHEMA = (
    Path(__file__).resolve().parents[1]
    / "schemas/v1/bus-epoch-recovery-receipt.schema.json"
)
FIELDS = frozenset({
    "schema_version", "id", "tenant_id", "timestamp", "kind", "actor",
    "idempotency_key", "request_id", "roll_receipt_id", "direction",
    "reason_code", "archive_path", "archive_sha256", "observed_live_sha256",
})
REQUEST_ID = "018f7e9b3c127abc8def0123456789ac"


def valid_receipt():
    return {
        "schema_version": 1,
        "id": "bus-epoch-recovery-018f7e9b3c117abc8def0123456789ab",
        "tenant_id": TENANT,
        "timestamp": "2026-10-11T18:52:00.000Z",
        "kind": KIND,
        "actor": "architect-a",
        "idempotency_key": "original-roll-key-1",
        "request_id": REQUEST_ID,
        "roll_receipt_id": "bus-epoch-roll-receipt-018f7e9b3c137abc8def0123456789ad",
        "direction": "rollback",
        "reason_code": "live_epoch_extended",
        "archive_path": "\x2ftmp/fleet-home/archive-request-" + REQUEST_ID,
        "archive_sha256": "a" * 64,
        "observed_live_sha256": "b" * 64,
    }


class EpochRecoveryReceiptContractTests(unittest.TestCase):
    def assert_refused(self, row):
        for integrity, error in ((False, ProtocolRefusal), (True, IntegrityFailure)):
            with self.subTest(integrity=integrity):
                with self.assertRaises(error):
                    validate_record(dict(row), TENANT, KINDS, integrity=integrity)
        with self.assertRaises(SchemaValidationError):
            validate_json_schema(row, SCHEMA)

    def test_native_accepts_exact_v1_recovery_receipt(self):
        row = valid_receipt()
        self.assertEqual(FIELDS, frozenset(row))
        for integrity in (False, True):
            with self.subTest(integrity=integrity):
                self.assertEqual(
                    row,
                    validate_record(dict(row), TENANT, KINDS, integrity=integrity),
                )

    def test_schema_accepts_exact_v1_recovery_receipt(self):
        validate_json_schema(valid_receipt(), SCHEMA)

    def test_schema_zero_has_typed_native_refusal(self):
        row = dict(valid_receipt(), schema_version=0)
        for integrity, error in ((False, ProtocolRefusal), (True, IntegrityFailure)):
            with self.subTest(integrity=integrity):
                with self.assertRaises(error) as caught:
                    validate_record(dict(row), TENANT, KINDS, integrity=integrity)
                self.assertEqual("schema_version_invalid", caught.exception.code)
        with self.assertRaises(SchemaValidationError):
            validate_json_schema(row, SCHEMA)

    def test_every_field_is_required_and_unknown_fields_are_refused(self):
        row = valid_receipt()
        for field in sorted(FIELDS):
            with self.subTest(missing=field):
                self.assert_refused({key: value for key, value in row.items() if key != field})
        for field in ("unexpected", "epoch_id", "outcome", "live_payload"):
            with self.subTest(extra=field):
                self.assert_refused(dict(row, **{field: "uncontracted"}))

    def test_receipt_and_original_roll_ids_require_exact_uuid7_prefixes(self):
        for field, bad_values in (
            ("id", (
                "bus-epoch-roll-receipt-" + REQUEST_ID,
                "bus-epoch-recovery-receipt-" + REQUEST_ID,
                "bus-epoch-recovery-" + REQUEST_ID.upper(),
                "bus-epoch-recovery-018f7e9b3c126abc8def0123456789ac",
                "bus-epoch-recovery-018f7e9b3c127abc7def0123456789ac",
                "bus-epoch-recovery-" + REQUEST_ID[:-1],
            )),
            ("roll_receipt_id", (
                "bus-epoch-recovery-" + REQUEST_ID,
                "bus-epoch-roll-" + REQUEST_ID,
                "bus-epoch-roll-receipt-" + REQUEST_ID.upper(),
                "bus-epoch-roll-receipt-018f7e9b3c126abc8def0123456789ac",
                "bus-epoch-roll-receipt-018f7e9b3c127abc7def0123456789ac",
            )),
            ("request_id", (
                "bus-epoch-roll-" + REQUEST_ID,
                "018f7e9b-3c12-7abc-8def-0123456789ac",
                REQUEST_ID.upper(), REQUEST_ID[:-1],
                "018f7e9b3c126abc8def0123456789ac",
                "018f7e9b3c127abc7def0123456789ac",
            )),
        ):
            for value in bad_values:
                with self.subTest(field=field, value=value):
                    self.assert_refused(dict(valid_receipt(), **{field: value}))

    def test_only_rollback_for_live_epoch_extended_is_a_recovery_receipt(self):
        for field, bad_values in (
            ("direction", ("roll_forward", "COMMITTED", "", "ROLLBACK")),
            ("reason_code", ("archive_mismatch", "", "live_epoch_changed")),
            ("kind", ("bus_epoch_roll_receipt", "bus_epoch_recovery")),
        ):
            for value in bad_values:
                with self.subTest(field=field, value=value):
                    self.assert_refused(dict(valid_receipt(), **{field: value}))

    def test_both_fact_digests_are_lowercase_sha256(self):
        for field in ("archive_sha256", "observed_live_sha256"):
            for value in ("", "a" * 63, "a" * 65, "A" * 64, "g" * 64):
                with self.subTest(field=field, value=value):
                    self.assert_refused(dict(valid_receipt(), **{field: value}))

    def test_strings_and_schema_version_reject_wrong_json_types(self):
        for field in sorted(FIELDS - {"schema_version"}):
            for value in (None, True, 1, [], {}):
                with self.subTest(field=field, wrong_type=type(value).__name__):
                    self.assert_refused(dict(valid_receipt(), **{field: value}))
        for value in (None, True, "1", -1, 2, [], {}):
            with self.subTest(schema_version=value):
                self.assert_refused(dict(valid_receipt(), schema_version=value))

    def test_archive_path_is_absolute_bounded_and_terminal_safe(self):
        for value in ("", "archive-request-1", "/" + "a" * 4096):
            with self.subTest(archive_path=value[:40]):
                self.assert_refused(dict(valid_receipt(), archive_path=value))
        # Do not add canonical-path or filesystem-existence claims to a receipt.
        # These are the native absolute, bounded, terminal-safe text limits.
        row = dict(valid_receipt(), archive_path="/" + "a" * 4095)
        validate_json_schema(row, SCHEMA)
        validate_record(row, TENANT, KINDS, integrity=True)

    def test_actor_and_original_roll_key_keep_native_bounds(self):
        for value in ("", "Actor-A", "architect/a", "a" * 65):
            with self.subTest(actor=value):
                self.assert_refused(dict(valid_receipt(), actor=value))
        for value in ("", "k" * 129):
            with self.subTest(idempotency_key=value):
                self.assert_refused(dict(valid_receipt(), idempotency_key=value))
        row = dict(valid_receipt(), idempotency_key="k" * 128)
        validate_json_schema(row, SCHEMA)
        validate_record(row, TENANT, KINDS, integrity=True)

    def test_terminal_unsafe_text_is_forbidden_in_path_and_original_key(self):
        for field in ("archive_path", "idempotency_key"):
            for unsafe in ("\x00", "\n", "\r", "\x1b", "\u202e", "\u2066"):
                value = ("\x2ftmp/fleet-home/" if field == "archive_path" else "roll-") + unsafe
                with self.subTest(field=field, unsafe=repr(unsafe)):
                    self.assert_refused(dict(valid_receipt(), **{field: value}))

    def test_native_reader_binds_selected_tenant_and_allowed_kind(self):
        row = valid_receipt()
        for tenant, kinds, code in (
            ("other-fleet", KINDS, "tenant_mismatch"),
            (TENANT, frozenset({"bus_epoch_roll_receipt"}), "record_kind_invalid"),
        ):
            with self.subTest(tenant=tenant, kinds=kinds):
                with self.assertRaises(IntegrityFailure) as caught:
                    validate_record(dict(row), tenant, kinds, integrity=True)
                self.assertEqual(code, caught.exception.code)


if __name__ == "__main__":
    unittest.main()

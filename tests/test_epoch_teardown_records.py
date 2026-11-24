"""Closed native records for roll-time teardown testimony.

Regression sources are authored before the validator; execution waits for
the managed full battery after the architect's quiet-window RESUME.
"""
from copy import deepcopy
import unittest

from floati.errors import ProtocolRefusal
from floati.records import validate_record


KIND = "seat_teardown_epoch_verification"
SUFFIX = "00000000000070008000000000000001"


def verification_record():
    return {
        "schema_version": 0, "kind": KIND, "tenant_id": "fleet",
        "id": "seat-teardown-epoch-verification-" + SUFFIX,
        "timestamp": "2026-10-16T05:00:00.000Z",
        "teardown_receipt_id": "seat-teardown-" + SUFFIX,
        "teardown_context_sha256": "a" * 64,
        "outgoing_epoch_id": "epoch-" + "b" * 32,
        "roll_receipt_id": "bus-epoch-roll-receipt-" + SUFFIX,
        "roll_receipt_sha256": "c" * 64,
        "report_envelope_id": None, "report_envelope_sha256": None,
        "verification": {"state": "verified", "failure_code": None, "remedy": None},
    }


class TeardownEpochRecordTests(unittest.TestCase):
    def validate(self, record):
        return validate_record(record, "fleet", frozenset({KIND}), integrity=False)

    def test_verified_and_failed_testimony_have_distinct_closed_shapes(self):
        valid = verification_record()
        self.assertEqual(valid, self.validate(valid))
        failed = deepcopy(valid)
        failed["verification"] = {
            "state": "failed", "failure_code": "seat_teardown_native_invalid",
            "remedy": "preserve the teardown receipts and inspect the exact saved prefix",
        }
        self.assertEqual(failed, self.validate(failed))
        for result in (
            {"state": "verified", "failure_code": "seat_teardown_native_invalid", "remedy": None},
            {"state": "verified", "failure_code": None, "remedy": "pretend repair"},
            {"state": "failed", "failure_code": None, "remedy": "inspect"},
            {"state": "failed", "failure_code": "seat_teardown_native_invalid", "remedy": None},
            {"state": "failed", "failure_code": "seat_teardown_native_invalid", "remedy": ""},
        ):
            with self.subTest(result=result), self.assertRaises(ProtocolRefusal):
                self.validate({**valid, "verification": result})

    def test_testimony_cannot_add_an_unreviewed_authority_field(self):
        row = verification_record()
        row["grant"] = True
        with self.assertRaises(ProtocolRefusal):
            self.validate(row)
        row = verification_record()
        row["verification"]["accepted_without_check"] = True
        with self.assertRaises(ProtocolRefusal):
            self.validate(row)

    def test_report_identity_and_digest_must_be_a_complete_native_pair(self):
        row = verification_record()
        row["report_envelope_id"] = "msg-" + SUFFIX
        with self.assertRaises(ProtocolRefusal):
            self.validate(row)
        row["report_envelope_sha256"] = "d" * 64
        self.assertEqual(row, self.validate(row))
        row["report_envelope_id"] = None
        with self.assertRaises(ProtocolRefusal):
            self.validate(row)

    def test_roll_context_and_report_digests_are_exact_sha256(self):
        for field in ("teardown_context_sha256", "roll_receipt_sha256"):
            row = verification_record()
            row[field] = "A" * 64
            with self.subTest(field=field), self.assertRaises(ProtocolRefusal):
                self.validate(row)
        row = verification_record()
        row["report_envelope_id"] = "msg-" + SUFFIX
        row["report_envelope_sha256"] = "short"
        with self.assertRaises(ProtocolRefusal):
            self.validate(row)

    def test_ids_cannot_substitute_another_native_receipt_family(self):
        for field, value in (
            ("teardown_receipt_id", "ack-" + SUFFIX),
            ("roll_receipt_id", "seat-teardown-" + SUFFIX),
            ("outgoing_epoch_id", "../epoch"),
        ):
            row = verification_record()
            row[field] = value
            with self.subTest(field=field), self.assertRaises(ProtocolRefusal):
                self.validate(row)

    def test_failed_testimony_refuses_terminal_control_in_its_remedy(self):
        row = verification_record()
        row["verification"] = {
            "state": "failed", "failure_code": "seat_teardown_native_invalid",
            "remedy": "inspect\u001b[31m",
        }
        with self.assertRaises(ProtocolRefusal):
            self.validate(row)

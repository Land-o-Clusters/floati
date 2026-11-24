from __future__ import annotations

from copy import deepcopy
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from floati.errors import ProtocolRefusal
from floati.jsonl import append_record, read_records_snapshot
from floati.records import validate_record
from floati.root import FloatiRoot
from tests.schema_validation import SchemaValidationError, validate_json_schema
from tests.temp_roots import REAL_TEMP_ROOT


SCHEMA = Path("schemas/v0/denial-receipt.schema.json")
LEGACY_REASONS = (
    "unknown_sender", "unknown_recipient", "idempotency_conflict",
    "reply_to_unknown", "reply_to_parties_mismatch",
)


class SendDenialDetailTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
        self.addCleanup(temporary.cleanup)
        scratch = Path(temporary.name)
        self.home = scratch / "home"
        self.home.mkdir()
        environment = mock.patch.dict(os.environ, {"HOME": str(self.home)})
        environment.start()
        self.addCleanup(environment.stop)
        self.root = FloatiRoot.open_direct_home(scratch / "denial-fleet", create=True)
        self.legacy = {
            "schema_version": 0,
            "id": "denial-00000000000070008000000000000000",
            "tenant_id": self.root.tenant_id,
            "timestamp": "2026-10-02T04:00:00.000Z",
            "kind": "denial_receipt",
            "attempt_id": "attempt-00000000000070008000000000000000",
            "claimed_sender": "builder",
            "claimed_recipient": "architect",
            "reason_code": "reply_to_parties_mismatch",
        }
        self.detail = {
            "case": "own_outgoing",
            "requested_reply_to": "msg-00000000000070008000000000000000",
            "remedy": "Drop --reply-to and send a standalone checkpoint.",
        }

    def runtime(self, row: dict) -> dict:
        return validate_record(
            row, self.root.tenant_id, frozenset({"denial_receipt"}), integrity=False
        )

    def assert_refused_by_both(self, row: dict) -> None:
        with self.assertRaises(ProtocolRefusal):
            self.runtime(row)
        with self.assertRaises(SchemaValidationError):
            validate_json_schema(row, SCHEMA)

    def test_legacy_denials_keep_all_five_reasons_and_durable_readability(self) -> None:
        for index, reason in enumerate(LEGACY_REASONS):
            with self.subTest(reason=reason):
                row = dict(self.legacy, reason_code=reason)
                row["id"] = "denial-0000000000007000800000000000000" + str(index)
                self.assertEqual(row, self.runtime(row))
                validate_json_schema(row, SCHEMA)
                append_record(
                    self.root, "denials.jsonl", row, allowed_kinds={"denial_receipt"}
                )
        rows = read_records_snapshot(
            self.root, "denials.jsonl", allowed_kinds={"denial_receipt"}
        )
        self.assertEqual(list(LEGACY_REASONS), [row["reason_code"] for row in rows])
        self.assertTrue(all("detail" not in row for row in rows))

    def test_three_closed_reply_cases_are_accepted_by_runtime_schema_and_ledger(self) -> None:
        cases = (
            ("own_outgoing", "reply_to_parties_mismatch"),
            ("other_parties", "reply_to_parties_mismatch"),
            ("unknown_reply", "reply_to_unknown"),
        )
        for index, (case, reason) in enumerate(cases):
            with self.subTest(case=case):
                detail = dict(self.detail, case=case)
                row = dict(self.legacy, reason_code=reason, detail=detail)
                row["id"] = "denial-0000000000007000800000000000000" + str(index)
                self.assertEqual(detail, self.runtime(row)["detail"])
                validate_json_schema(row, SCHEMA)
                append_record(
                    self.root, "denials.jsonl", row, allowed_kinds={"denial_receipt"}
                )
        rows = read_records_snapshot(
            self.root, "denials.jsonl", allowed_kinds={"denial_receipt"}
        )
        self.assertEqual(
            ["own_outgoing", "other_parties", "unknown_reply"],
            [row["detail"]["case"] for row in rows],
        )

    def test_three_closed_reply_cases_are_accepted_by_exported_schema(self) -> None:
        for case, reason in (
            ("own_outgoing", "reply_to_parties_mismatch"),
            ("other_parties", "reply_to_parties_mismatch"),
            ("unknown_reply", "reply_to_unknown"),
        ):
            with self.subTest(case=case):
                validate_json_schema(
                    dict(self.legacy, reason_code=reason, detail=dict(self.detail, case=case)),
                    SCHEMA,
                )

    def test_case_reason_cross_mismatches_and_unrelated_reasons_are_refused(self) -> None:
        valid = {
            ("own_outgoing", "reply_to_parties_mismatch"),
            ("other_parties", "reply_to_parties_mismatch"),
            ("unknown_reply", "reply_to_unknown"),
        }
        for case in ("own_outgoing", "other_parties", "unknown_reply"):
            for reason in LEGACY_REASONS:
                if (case, reason) in valid:
                    continue
                with self.subTest(case=case, reason=reason):
                    self.assert_refused_by_both(dict(
                        self.legacy, reason_code=reason,
                        detail=dict(self.detail, case=case),
                    ))

    def test_detail_rejects_unknown_fields_unprintable_and_out_of_bounds_values(self) -> None:
        invalid = [None, [], "detail"]
        for field in self.detail:
            detail = deepcopy(self.detail)
            del detail[field]
            invalid.append(detail)
        for field, value in (
            ("case", "self_reply"),
            ("case", "own_outgoing\n"),
            ("case", []),
            ("case", {}),
            ("case", None),
            ("requested_reply_to", ""),
            ("requested_reply_to", "x" * 129),
            ("requested_reply_to", "msg with spaces"),
            ("requested_reply_to", "msg\n"),
            ("requested_reply_to", "msg-\u00e9"),
            ("requested_reply_to", 3),
            ("remedy", ""),
            ("remedy", "x" * 1025),
            ("remedy", "new\nline"),
            ("remedy", "trailing\n"),
            ("remedy", "control\x00"),
            ("remedy", "delete\x7f"),
            ("remedy", "invisible\u200b"),
            ("remedy", "bidi\u202e"),
            ("remedy", 3),
            ("body", "private message body"),
        ):
            detail = deepcopy(self.detail)
            detail[field] = value
            invalid.append(detail)
        for detail in invalid:
            with self.subTest(detail=detail):
                self.assert_refused_by_both(dict(self.legacy, detail=detail))

    def test_valid_opaque_and_printable_boundaries_are_retained(self) -> None:
        for reply, remedy in (
            ("x", "x"),
            ("A" * 128, "x" * 1024),
            ("invalid-0123456789abcdef", "omit --reply-to to start a new thread"),
            ("a.b:c_d-e", "retry send with --reply-to msg-00000000000070008000000000000000"),
        ):
            with self.subTest(reply=reply):
                detail = dict(self.detail, requested_reply_to=reply, remedy=remedy)
                row = dict(self.legacy, detail=detail)
                self.assertEqual(detail, self.runtime(row)["detail"])
                validate_json_schema(row, SCHEMA)

    def test_extended_denials_keep_original_top_level_field_and_reason_fences(self) -> None:
        base = dict(self.legacy, detail=self.detail)
        self.assert_refused_by_both(dict(base, secret="private body"))
        self.assert_refused_by_both(dict(base, reason_code="new_reason"))
        self.assert_refused_by_both(dict(base, schema_version=1))
        for field in self.legacy:
            row = dict(base)
            del row[field]
            with self.subTest(missing=field):
                self.assert_refused_by_both(row)


if __name__ == "__main__":
    unittest.main()

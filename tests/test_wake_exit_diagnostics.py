from __future__ import annotations

from copy import deepcopy
import hashlib
from pathlib import Path
import tempfile
import unittest

from floati.errors import ProtocolRefusal
from floati.jsonl import read_records_snapshot
from floati.records import validate_record
from floati.registry import Registry
from floati.root import FloatiRoot
from floati.wake_exit import WakeExitLedger
from tests.schema_validation import SchemaValidationError, validate_json_schema
from tests.temp_roots import REAL_TEMP_ROOT


class WakeExitDiagnosticTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
        self.addCleanup(temporary.cleanup)
        self.root = FloatiRoot.open_direct_home(
            Path(temporary.name) / "diagnostic-test", create=True
        )
        Registry(self.root).register("builder", "Codex")
        self.arguments = {
            "node_id": "builder",
            "session_digest": hashlib.sha256(b"diagnostic-session").hexdigest(),
            "reason_code": "integrity_failure",
            "waited_seconds": 1,
            "idempotency_key": "diagnostic-exit",
        }
        self.diagnostic = {
            "check_id": "record_json_invalid",
            "exception_class": "IntegrityFailure",
            "input_identity": {"path": "events.jsonl", "byte_offset": 17},
            "occurrence_count": 1,
        }

    def rows(self) -> list[dict]:
        return read_records_snapshot(
            self.root, Path("receipts/wake-waiter-exit/builder.jsonl"),
            allowed_kinds={"wake_waiter_exit_receipt"},
        )

    def validate(self, row: dict) -> None:
        validate_record(
            row, self.root.tenant_id, frozenset({"wake_waiter_exit_receipt"}),
            integrity=False,
        )
        validate_json_schema(
            row, Path("schemas/v1/wake-waiter-exit-receipt.schema.json")
        )

    def test_integrity_exit_retains_check_and_replays_without_second_append(self) -> None:
        ledger = WakeExitLedger(self.root)
        first = ledger.record(**self.arguments, integrity_failure=self.diagnostic)
        replay = ledger.record(**self.arguments, integrity_failure=self.diagnostic)
        self.assertEqual(first, replay)
        self.assertEqual(self.diagnostic, first["integrity_failure"])
        self.assertEqual([first], self.rows())
        self.validate(first)
        altered = deepcopy(self.diagnostic)
        altered["input_identity"]["byte_offset"] = 18
        with self.assertRaises(ProtocolRefusal) as caught:
            ledger.record(**self.arguments, integrity_failure=altered)
        self.assertEqual("wake_waiter_exit_idempotency_conflict", caught.exception.code)
        self.assertEqual([first], self.rows())
        altered = dict(self.diagnostic, occurrence_count=2)
        with self.assertRaises(ProtocolRefusal):
            ledger.record(**self.arguments, integrity_failure=altered)
        with self.assertRaises(ProtocolRefusal):
            ledger.record(**self.arguments)

    def test_legacy_exit_replays_and_does_not_grow_diagnostic_fields(self) -> None:
        ledger = WakeExitLedger(self.root)
        row = ledger.record(**self.arguments)
        self.assertEqual(row, ledger.record(**self.arguments))
        self.assertNotIn("integrity_failure", row)
        self.validate(row)
        with self.assertRaises(ProtocolRefusal):
            ledger.record(**self.arguments, integrity_failure=self.diagnostic)

    def test_proven_hold_window_is_bounded_and_old_diagnostics_still_validate(self) -> None:
        row = WakeExitLedger(self.root).record(
            **self.arguments, integrity_failure=dict(self.diagnostic, hold_window_seconds=2),
        )
        self.validate(row)
        for window in (-1, True, 86400):
            with self.subTest(window=window):
                malformed = deepcopy(row)
                malformed["integrity_failure"]["hold_window_seconds"] = window
                with self.assertRaises(ProtocolRefusal):
                    validate_record(malformed, self.root.tenant_id, {"wake_waiter_exit_receipt"}, integrity=False)
                with self.assertRaises(SchemaValidationError):
                    validate_json_schema(malformed, Path("schemas/v1/wake-waiter-exit-receipt.schema.json"))

    def test_operation_only_and_envelope_identity_are_valid_bounded_evidence(self) -> None:
        diagnostic = deepcopy(self.diagnostic)
        diagnostic["input_identity"] = {
            "operation": "wake_hold.evaluate",
            "envelope_id": "msg-00000000000070008000000000000000",
        }
        row = WakeExitLedger(self.root).record(
            **self.arguments, integrity_failure=diagnostic
        )
        self.validate(row)
        self.assertEqual(diagnostic, row["integrity_failure"])

    def test_closed_diagnostics_refuse_body_private_path_and_invalid_count(self) -> None:
        base = WakeExitLedger(self.root).record(**self.arguments)
        invalid = []
        for identity in (
            {"path": "\x2fUsers/private/events.jsonl"},
            {"path": "../events.jsonl"},
            {"path": "a/../events.jsonl"},
            {"path": "a/./events.jsonl"},
            {"path": "a//events.jsonl"},
            {"path": "events.jsonl\nsecret"},
            {"path": "events.jsonl\n"},
            {"path": "events.jsonl", "body": "private message"},
            {"byte_offset": 17},
            {"operation": "evaluate", "byte_offset": 17},
            {"path": "events.jsonl", "byte_offset": -1},
            {"path": "events.jsonl", "byte_offset": True},
            {"path": "events.jsonl", "byte_offset": 2**63},
            {"operation": "private message content"},
            {"operation": "evaluate\n"},
            {"path": "events.jsonl", "envelope_id": "private message"},
        ):
            diagnostic = deepcopy(self.diagnostic)
            diagnostic["input_identity"] = identity
            invalid.append(diagnostic)
        for count in (0, 100001, True, "1"):
            diagnostic = deepcopy(self.diagnostic)
            diagnostic["occurrence_count"] = count
            invalid.append(diagnostic)
        for field, value in (
            ("check_id", "failed check including private content"),
            ("check_id", "record_json_invalid\n"),
            ("exception_class", "private class\ncontent"),
            ("exception_class", "IntegrityFailure\n"),
            ("detail", "private exception message"),
        ):
            diagnostic = deepcopy(self.diagnostic)
            diagnostic[field] = value
            invalid.append(diagnostic)
        for diagnostic in invalid:
            with self.subTest(diagnostic=diagnostic):
                row = dict(base, integrity_failure=diagnostic)
                with self.assertRaises(ProtocolRefusal):
                    validate_record(
                        row, self.root.tenant_id,
                        frozenset({"wake_waiter_exit_receipt"}), integrity=False,
                    )
                with self.assertRaises(SchemaValidationError):
                    validate_json_schema(
                        row, Path("schemas/v1/wake-waiter-exit-receipt.schema.json")
                    )

    def test_diagnostics_are_refused_on_other_exit_reasons_before_append(self) -> None:
        # Validate both reader and exported schema, using a legacy row to
        # avoid a writer rejection hiding an overly permissive schema.
        base = WakeExitLedger(self.root).record(**self.arguments)
        for reason in ("paused", "exhausted", "not_claimant", "breaker", "consent_withdrawn"):
            with self.subTest(reason=reason):
                arguments = dict(self.arguments, reason_code=reason)
                with self.assertRaises(ProtocolRefusal):
                    WakeExitLedger(self.root).record(
                        **arguments, integrity_failure=self.diagnostic
                    )
                row = dict(base, reason_code=reason, integrity_failure=self.diagnostic)
                with self.assertRaises(ProtocolRefusal):
                    validate_record(
                        row, self.root.tenant_id,
                        frozenset({"wake_waiter_exit_receipt"}), integrity=False,
                    )
                with self.assertRaises(SchemaValidationError):
                    validate_json_schema(
                        row, Path("schemas/v1/wake-waiter-exit-receipt.schema.json")
                    )
        self.assertEqual([base], self.rows())


if __name__ == "__main__":
    unittest.main()

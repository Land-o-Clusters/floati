"""Additive readiness schemas agree with authentic native testimony.

Every record starts as a real send or native Stop invocation. Rejected cases
corrupt that testimony; no readiness answer or hold receipt is fabricated.
Sibling timestamp equality/order remains a native semantic check because
standard JSON Schema cannot compare arbitrary instance fields.
"""
from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from floati.errors import IntegrityFailure, ProtocolRefusal
from floati.events import EventLog
from floati.framing import encode_frame
from floati.records import validate_record
from floati.stop_hold import StopHoldLedger
from tests import test_f2_readiness_hold as native_holds
from tests import test_f2_readiness_journal as native_journal
from tests import test_f2_readiness_mechanisms as native_mechanisms
from tests.schema_validation import SchemaValidationError, validate_json_schema
from tests.temp_roots import REAL_TEMP_ROOT


SCHEMAS = Path(__file__).resolve().parents[1] / "schemas/v1"
SEND_SCHEMA = SCHEMAS / "send-readiness-observation.schema.json"
HOLD_SCHEMA = SCHEMAS / "stop-hold-receipt.schema.json"


class ReadinessSchemaParityTests(unittest.TestCase):
    def fixture(self, cls, method):
        fixture = cls(method)
        self.addCleanup(fixture.doCleanups)
        fixture.setUp()
        return fixture

    def native_send(self, mechanism=None):
        if mechanism in {"seat_board", "absent"}:
            fixture = self.fixture(native_mechanisms.RecipientHookMechanismTests,
                "test_readable_exact_empty_config_checks_all_mechanisms_before_negative")
            fixture.empty_hooks()
            if mechanism == "seat_board":
                fixture.board()
            fixture.readiness("schema-native")
        elif mechanism == "stop_hook":
            fixture = self.fixture(native_holds.NativeStopHoldReadinessTests,
                "test_codex_active_empty_hold_qualifies_send_without_lease_or_daemon")
            fixture.fixture("Codex")
            fixture.run_hold("Codex", lambda: fixture.send("schema-native"))
        elif mechanism is None:
            # Worker/smoke hooks are structurally absent under the F2 ruling;
            # Claude preserves this real unreadable-hook UNKNOWN witness.
            fixture = self.fixture(native_mechanisms.RecipientHarnessHookCapabilityTests,
                "test_claude_can_carry_an_unreadable_hook_so_readiness_is_unknown")
            fixture.observe_native_send("Claude", "unknown", "unknown")
        else:
            fixture = self.fixture(native_journal.ReadinessJournalTests,
                "test_send_journals_exact_readiness_beside_an_unchanged_envelope")
            if mechanism == "active_lease":
                fixture.lease()
            fixture.send("schema-native")
        rows = EventLog(fixture.root).strict_event_records()
        observations = [row for row in rows if row["kind"] == "send_readiness_observation"]
        self.assertEqual(1, len(observations))
        return observations[0]

    def native_holds(self):
        fixture = self.fixture(native_holds.NativeStopHoldReadinessTests,
            "test_codex_deadline_closes_empty_hold_instead_of_leaving_ready_start")
        fixture.fixture("Codex")
        fixture.run_hold("Codex")
        self.hold_fixture = fixture
        rows = fixture.hold_rows()
        self.assertEqual(["start", "exit"], [row["phase"] for row in rows])
        return rows

    def assert_native_valid(self, record):
        validate_record(record, record["tenant_id"], {record["kind"]}, integrity=True)

    def assert_native_invalid(self, record):
        with self.assertRaises((IntegrityFailure, ProtocolRefusal)):
            self.assert_native_valid(record)

    def assert_both_invalid(self, record, schema):
        self.assert_native_invalid(record)
        with self.assertRaises(SchemaValidationError):
            validate_json_schema(record, schema)

    def named_positive(self, mechanism):
        row = self.native_send(mechanism)
        self.assertEqual("recipient_wake_arranged", row["readiness"]["state"])
        self.assertEqual(mechanism, row["readiness"]["reason"])
        self.assert_native_valid(row)
        validate_json_schema(row, SEND_SCHEMA)
        for wrong in {"active_lease", "stop_hook", "seat_board"} - {mechanism}:
            with self.subTest(wrong_reason=wrong):
                damaged = copy.deepcopy(row)
                damaged["readiness"]["reason"] = wrong
                self.assert_both_invalid(damaged, SEND_SCHEMA)

    def test_native_lease_requires_its_named_arrangement(self):
        self.named_positive("active_lease")

    def test_native_stop_hold_requires_its_named_arrangement(self):
        self.named_positive("stop_hook")

    def test_native_board_requires_its_named_arrangement(self):
        self.named_positive("seat_board")

    def test_native_affirmative_absence_cannot_hide_an_unknown_mechanism(self):
        row = self.native_send("absent")
        self.assertEqual("recipient_not_listening", row["readiness"]["state"])
        self.assert_native_valid(row)
        validate_json_schema(row, SEND_SCHEMA)
        for changed in ("unknown", "arranged"):
            with self.subTest(changed=changed):
                damaged = copy.deepcopy(row)
                damaged["readiness"]["mechanism_evidence"][0]["state"] = changed
                self.assert_both_invalid(damaged, SEND_SCHEMA)

    def test_native_unknown_requires_uncertainty_and_no_arranged_mechanism(self):
        row = self.native_send()
        self.assertEqual("unknown", row["readiness"]["state"])
        self.assert_native_valid(row)
        validate_json_schema(row, SEND_SCHEMA)
        arranged = copy.deepcopy(row)
        arranged["readiness"]["mechanism_evidence"][0]["state"] = "arranged"
        self.assert_both_invalid(arranged, SEND_SCHEMA)
        absent = copy.deepcopy(row)
        for fact in absent["readiness"]["mechanism_evidence"]:
            fact["state"] = "absent"
        self.assert_both_invalid(absent, SEND_SCHEMA)

    def test_native_send_preserves_legacy_utc_seconds_precision(self):
        fixture = self.fixture(native_journal.ReadinessJournalTests,
            "test_send_journals_exact_readiness_beside_an_unchanged_envelope")
        seconds = "2026-10-02T12:00:00Z"
        with mock.patch("floati.events.utc_now", return_value=seconds):
            result = fixture.events.send("sender", "recipient", "floati",
                native_journal.SHA, "docs/evidence/schema.md", "native seconds clock",
                idempotency_key="schema-seconds-clock")
        row = fixture.observation(result["message"])
        self.assertEqual(seconds, row["timestamp"])
        self.assert_native_valid(row)
        validate_json_schema(row, SEND_SCHEMA)

    def test_real_hold_start_and_exit_validate_without_message_content(self):
        for row in self.native_holds():
            with self.subTest(phase=row["phase"]):
                self.assert_native_valid(row)
                validate_json_schema(row, HOLD_SCHEMA)
                self.assertFalse({"body", "note", "messages"} & set(row))

    def test_hold_ids_and_terminal_safe_identity_bounds_agree(self):
        start = self.native_holds()[0]
        for case_index, (field, value) in enumerate((("id", "stop-hold-not-a-uuid"),
                             ("consent_receipt_id", start["id"]),
                             ("invocation_id", ""), ("invocation_id", "x" * 129),
                             ("acting_session_id", "x" * 513),
                             ("workspace", "relative"), ("harness", "unbound"),
                             ("acting_session_id", "unsafe\nidentity"))):
            with self.subTest(case=(
                "malformed-hold-id",
                "hold-id-as-consent-receipt-id",
                "empty-invocation-id",
                "invocation-id-length-129",
                "acting-session-length-513",
                "relative-workspace",
                "unbound-harness",
                "acting-session-newline",
            )[case_index], field=field):
                damaged = dict(start, **{field: value})
                self.assert_both_invalid(damaged, HOLD_SCHEMA)

    def test_hold_budget_zero_boolean_nonfinite_and_upper_bound_are_refused(self):
        start = self.native_holds()[0]
        for value in (0, -1, True, float("inf"), float("nan"), 86400.001):
            with self.subTest(budget=value):
                self.assert_both_invalid(dict(start, deadline_seconds=value), HOLD_SCHEMA)
        for value in (0.001, 86400):
            with self.subTest(valid_budget=value):
                row = dict(start, deadline_seconds=value)
                self.assert_native_valid(row)
                validate_json_schema(row, HOLD_SCHEMA)

    def test_hold_times_reject_offset_naive_invalid_calendar_and_precision(self):
        start = self.native_holds()[0]
        for value in ("2026-10-02T12:00:00+00:00", "2026-10-02T12:00:00",
                      "2026-02-30T12:00:00.000Z", "2026-10-02T12:00:00.000001Z"):
            with self.subTest(time=value):
                damaged = dict(start, started_at=value, timestamp=value)
                self.assert_both_invalid(damaged, HOLD_SCHEMA)

    def test_huge_integer_hold_budget_has_a_typed_refusal(self):
        damaged = dict(self.native_holds()[0], deadline_seconds=10 ** 400)
        failure = None
        try:
            self.assert_native_valid(damaged)
        except Exception as exc:
            failure = exc
        self.assertIsInstance(failure, (IntegrityFailure, ProtocolRefusal),
                              "budget bounds must refuse before float conversion can overflow")
        with self.assertRaises(SchemaValidationError):
            validate_json_schema(damaged, HOLD_SCHEMA)

    def test_native_checks_sibling_time_relations_beyond_standard_schema(self):
        start, exit_row = self.native_holds()
        damaged = dict(start, timestamp="2099-01-01T00:00:00.000Z")
        self.assert_native_invalid(damaged)
        validate_json_schema(damaged, HOLD_SCHEMA)
        damaged = dict(exit_row, timestamp="2000-01-01T00:00:00.000Z")
        self.assert_native_invalid(damaged)
        validate_json_schema(damaged, HOLD_SCHEMA)

    def test_seconds_precision_cannot_pass_contract_and_fail_its_native_ledger(self):
        rows = copy.deepcopy(self.native_holds())
        seconds = rows[0]["started_at"].split(".")[0] + "Z"
        for row in rows:
            row["started_at"] = seconds
            if row["phase"] == "start":
                row["timestamp"] = seconds
        root = self.hold_fixture.root
        path = root.resolve_relative(StopHoldLedger.relative(rows[0]["node_id"]))
        path.write_bytes(b"".join(encode_frame(row) for row in rows))
        with self.assertRaises(IntegrityFailure):
            StopHoldLedger(root).read(rows[0]["node_id"])
        with self.subTest(consumer="native_record"):
            self.assert_native_invalid(rows[0])
        with self.subTest(consumer="schema"), self.assertRaises(SchemaValidationError):
            validate_json_schema(rows[0], HOLD_SCHEMA)


class SchemaExclusiveNumberVocabularyTests(unittest.TestCase):
    def schema(self, document):
        temporary = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
        self.addCleanup(temporary.cleanup)
        path = Path(temporary.name) / "numeric.schema.json"
        path.write_text(json.dumps(document), encoding="utf-8")
        return path

    def test_exclusive_minimum_rejects_its_exact_boundary(self):
        schema = self.schema({"type": "number", "exclusiveMinimum": 0})
        validate_json_schema(0.001, schema)
        with self.assertRaises(SchemaValidationError):
            validate_json_schema(0, schema)

    def test_exclusive_maximum_rejects_its_exact_boundary(self):
        schema = self.schema({"type": "number", "exclusiveMaximum": 10})
        validate_json_schema(9.999, schema)
        with self.assertRaises(SchemaValidationError):
            validate_json_schema(10, schema)

    def test_numeric_exclusive_constraints_keep_boolean_and_finite_type_fences(self):
        schema = self.schema({"type": "number", "exclusiveMinimum": -1,
                              "exclusiveMaximum": 2})
        for value in (True, False, float("nan"), float("inf"), float("-inf")):
            with self.subTest(value=value), self.assertRaises(SchemaValidationError):
                validate_json_schema(value, schema)
        # A numeric keyword alone does not constrain a JSON boolean.
        schema = self.schema({"exclusiveMinimum": 0, "exclusiveMaximum": 0})
        validate_json_schema(True, schema)
        validate_json_schema(False, schema)

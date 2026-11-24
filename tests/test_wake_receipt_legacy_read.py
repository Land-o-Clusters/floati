"""WAKE-RECEIPT-LEGACY-READ-1 observation-only compatibility boundary.

The defective decision reference below is SYNTHETIC and is not evidence of
any historically lawful legacy shape. Native valid wake receipts surround
it; observations must mark the entire attempt history unavailable, while
strict readers/writers and all unrelated integrity failures still refuse.
All root data, HOME and TMPDIR are independent scratch fixtures.
"""
from __future__ import annotations

import hashlib
import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from floati import fixture_ids
from floati.doctor import Doctor, _finding
from floati.doctor_wake_receipt_legacy_read import project_wake_receipt_legacy_read
from floati.errors import IntegrityFailure, ProtocolRefusal
from floati.framing import encode_frame
from floati.jsonl import append_record, read_records_snapshot
from floati.projection import FleetProjection
from floati.records import validate_record
from floati.registry import Registry
from floati.wake_exit import WakeExitLedger
from floati.wake_health import WakeHealthProjection
from floati.wake_hold import WakeAttemptLedger, WakeHoldController
from tests import test_wake_health_surface as fixtures
from tests.schema_validation import SchemaValidationError, validate_json_schema


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SYNTHETIC_BAD_REFERENCE = "synthetic-unproven-legacy-decision-PRIVATE"


class DoctorLegacyFindingFactoryTests(unittest.TestCase):
    def fact(self, ledger):
        return {"read_finding": {
            "code": "wake_receipt_legacy_unreadable", "ledger": ledger,
            "record_id": "wake-attempt-018f7e9b3c137abc8def0123456789ab",
            "source_code": "decision_receipt_id_invalid",
        }}

    def project(self, fact, finding, factory=_finding):
        try:
            return project_wake_receipt_legacy_read(
                fact, "warning", finding, finding_factory=factory,
            )
        except TypeError as exc:
            self.fail("Doctor projector must accept its required finding factory: " + str(exc))

    def test_named_subject_uses_existing_home_redaction_and_preserves_controls(self):
        # Absolute paths challenge the write boundary only; they are not
        # schema-qualified native ledger observations or legacy validity claims.
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / "operator-home"
            home.mkdir()
            relative = "receipts/wakes/reader.jsonl"
            cases = (
                (str(home / relative), "~/" + relative),
                (str(Path(temporary) / "outside" / "wakes.jsonl"),
                 str(Path(temporary) / "outside" / "wakes.jsonl")),
                (relative, relative),
            )
            with mock.patch.dict(os.environ, {"HOME": str(home)}):
                for case_index, (ledger, expected) in enumerate(cases):
                    with self.subTest(case=(
                        "inside-HOME-redacted-ledger",
                        "outside-HOME-absolute-ledger",
                        "relative-ledger",
                    )[case_index]):
                        fact = self.fact(ledger)
                        original = copy.deepcopy(fact)
                        severity, finding = self.project(fact, {"extra": "retained"})
                        self.assertEqual(("error", expected), (severity, finding["subject"]))
                        self.assertEqual(original["read_finding"], finding["read_finding"])
                        self.assertEqual(original, fact)
                        self.assertEqual("retained", finding["extra"])

    def test_relative_named_finding_bytes_and_metadata_are_preserved(self):
        ledger = "receipts/wakes/reader.jsonl"
        fact = self.fact(ledger)
        existing = {"code": "wake_health_unknown", "severity": "warning", "extra": [1]}
        before = copy.deepcopy(existing)
        calls = []

        def factory(*arguments):
            calls.append(arguments)
            return _finding(*arguments)

        severity, finding = self.project(fact, existing, factory)
        expected = {
            "code": "wake_receipt_legacy_unreadable", "severity": "error",
            "subject": ledger, "extra": [1],
            "detail": (
                "Wake-attempt testimony is unavailable because a decision reference "
                "failed strict validation. No receipts from this ledger were used "
                "as wake evidence."
            ),
            "remediation": (
                "Retain this ledger unchanged. Inspect an owned clone and rehearse "
                "any quarantine with the actual Stop waiter before live repair."
            ),
            "read_finding": fact["read_finding"],
        }
        self.assertEqual("error", severity)
        self.assertEqual(json.dumps(expected, sort_keys=True), json.dumps(finding, sort_keys=True))
        self.assertEqual(before, existing)
        self.assertIsNot(existing, finding)
        self.assertEqual(1, len(calls))
        self.assertEqual(ledger, calls[0][2])

    def test_normal_branch_returns_original_object_without_calling_factory(self):
        existing = {"code": "wake_health_ok", "severity": "info", "extra": [1]}

        def forbidden_factory(*arguments):
            self.fail("Normal observation must not call the named-finding factory")

        severity, finding = self.project({}, existing, forbidden_factory)
        self.assertEqual("warning", severity)
        self.assertIs(existing, finding)


class WakeReceiptLegacyReadTests(unittest.TestCase):
    def setUp(self):
        fixtures.WakeHealthSurfaceTests.setUp(self)
        self.home = self.base / "home"
        self.temporary_home = self.base / "tmp"
        self.home.mkdir()
        self.temporary_home.mkdir()
        environment = mock.patch.dict(os.environ, {
            "HOME": str(self.home), "TMPDIR": str(self.temporary_home),
            "PATH": os.environ.get("PATH", os.defpath),
        }, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        self.node = fixture_ids.builder("health")
        self.other = fixture_ids.builder("legacy-healthy")
        Registry(self.root).register(self.other, "Codex")
        self.relative = "receipts/wakes/" + self.node + ".jsonl"
        self.path = self.root.resolve_relative(self.relative)
        controller = WakeHoldController(self.root)
        attempts = WakeAttemptLedger(self.root)
        valid = []
        with mock.patch("floati.wake_hold.utc_now", return_value="2026-08-30T12:05:00.000Z"):
            decision = controller.evaluate(self.node, idempotency_key="legacy-valid-decision")
        for suffix in ("before", "after"):
            with mock.patch("floati.wake_hold.utc_now", return_value="2026-08-30T12:05:00.000Z"):
                valid.append(attempts.record(
                    recipient=self.node, acting_session_id="seat-valid-" + suffix,
                    item_ids=[self.message["id"]],
                    decision_receipt_id=decision["receipt"]["id"],
                    message_worker_session_id=None,
                    idempotency_key="legacy-attempt-" + suffix, outcome="woke",
                ))
        self.valid = valid
        # The native hold receipt consumes its original item IDs in delivery
        # health. Add later mail so this fixture actually has unread evidence.
        with mock.patch("floati.events.utc_now", return_value="2026-08-30T12:06:00.000Z"):
            self.unread_message = fixtures.EventLog(self.root).send(
                "architect", self.node, "floati", "a" * 40,
                "docs/evidence/wake-health.md", "later unread observation",
                idempotency_key="legacy-later-unread-message",
            )
        self.bad = dict(valid[0],
            id="wake-attempt-018f7e9b3c137abc8def0123456789ab",
            idempotency_key="synthetic-unproven-legacy-attempt",
            decision_receipt_id=SYNTHETIC_BAD_REFERENCE,
            acting_session_id="synthetic-private-legacy-session",
        )
        with mock.patch("floati.wake_exit.utc_now", return_value="2026-08-30T12:08:00.000Z"):
            WakeExitLedger(self.root).record(
                node_id=self.node, session_digest=hashlib.sha256(b"seat-claim").hexdigest(),
                reason_code="exhausted", waited_seconds=2, idempotency_key="legacy-exit-observation",
            )
        breaker = self.root.resolve_relative("state/codex-wait/" + self.node + "/breaker.json")
        breaker.parent.mkdir(parents=True, exist_ok=True)
        breaker.write_text(json.dumps({"hits": [self.observation.timestamp()] * 21}) + "\n")
        self.expected_other = WakeHealthProjection(self.root).fact(self.other, self.observation)
        self.healthy_observations = WakeHealthProjection(self.root).fact(self.node, self.observation)
        self.install_synthetic_history()

    def install_synthetic_history(self):
        # Intentional fixture corruption only; never a native write or migration.
        self.path.write_bytes(b"".join(encode_frame(row) for row in
                                      (self.valid[0], self.bad, self.valid[1])))

    def finding(self):
        return {
            "code": "wake_receipt_legacy_unreadable", "ledger": self.relative,
            "record_id": self.bad["id"], "source_code": "decision_receipt_id_invalid",
        }

    def ledger_bytes(self):
        return {str(path.relative_to(self.root.tenant_home)): path.read_bytes()
                for path in self.root.tenant_home.rglob("*.jsonl") if path.is_file()}

    def observe(self, operation):
        try:
            return operation()
        except IntegrityFailure as failure:
            self.fail("synthetic legacy wake receipt aborted observation instead of "
                      "producing source-bound unavailable history: " + failure.code)

    def assert_unknown_attempt_history(self, fact):
        self.assertEqual("unknown", fact["state"])
        self.assertIsNone(fact["last_seen_session"], "neither valid surrounding row may be salvaged")
        self.assertEqual(self.finding(), fact["read_finding"])
        native = fact["recent_sessions"]["native_woke"]
        self.assertEqual("unavailable", native["coverage"])
        self.assertEqual(self.relative, native["source_path"])
        self.assertEqual([], native["sessions"], "entire attempt source is unavailable")
        for key in ("claim_session", "waiter_exit_reason", "waiter_receipt_age_minutes",
                    "breaker_state", "pause_state", "oldest_unread"):
            self.assertEqual(self.healthy_observations[key], fact[key], key)
        self.assertEqual("seat-claim", fact["claim_session"])
        self.assertEqual("open", fact["breaker_state"])
        self.assertEqual("exhausted", fact["waiter_exit_reason"])
        self.assertIsNotNone(fact["oldest_unread"])
        self.assertEqual(4, fact["oldest_unread"]["age_minutes"])
        encoded = json.dumps(fact, sort_keys=True)
        self.assertNotIn(SYNTHETIC_BAD_REFERENCE, encoded)
        self.assertNotIn(self.bad["acting_session_id"], encoded)
        validate_json_schema(fact, Path("schemas/v1/wake-health-fact.schema.json"))

    def test_fact_marks_entire_attempt_source_unavailable_without_losing_other_observations(self):
        before = self.ledger_bytes()
        fact = self.observe(lambda: WakeHealthProjection(self.root).fact(self.node, self.observation))
        self.assert_unknown_attempt_history(fact)
        self.assertEqual(before, self.ledger_bytes(), "observational compatibility never repairs a ledger")

    def test_recent_native_woke_is_unavailable_and_never_salvages_surrounding_valid_rows(self):
        before = self.ledger_bytes()
        recent = self.observe(lambda: WakeHealthProjection(self.root).recent_sessions(self.node, self.observation))
        self.assertEqual("unavailable", recent["native_woke"]["coverage"])
        self.assertEqual(self.relative, recent["native_woke"]["source_path"])
        self.assertEqual([], recent["native_woke"]["sessions"])
        self.assertEqual(self.healthy_observations["recent_sessions"]["cursor_local_publication"],
                         recent["cursor_local_publication"])
        self.assertEqual(before, self.ledger_bytes())

    def test_status_continues_and_preserves_healthy_other_node(self):
        before = self.ledger_bytes()
        status = self.observe(lambda: FleetProjection(self.root).status_artifact(self.observation))
        by_node = {row["node_id"]: row for row in status["nodes"]}
        self.assert_unknown_attempt_history(by_node[self.node]["wake_health"])
        self.assertEqual(self.expected_other, by_node[self.other]["wake_health"])
        self.assertEqual("healthy", by_node[self.other]["wake_health"]["state"])
        self.assertEqual(before, self.ledger_bytes())

    def test_cached_status_invalidates_bad_history_and_recovers_after_exact_valid_restore(self):
        synthetic = self.path.read_bytes()
        valid = b"".join(encode_frame(row) for row in self.valid)
        others = {path: payload for path, payload in self.ledger_bytes().items()
                  if path != self.relative}
        projection = FleetProjection(self.root)

        def facts():
            artifact = self.observe(lambda: projection.status_artifact(self.observation))
            return {row["node_id"]: row["wake_health"] for row in artifact["nodes"]}

        self.path.write_bytes(valid)
        first = facts()
        self.assertEqual(self.healthy_observations, first[self.node])
        self.assertEqual(first, facts(), "warm valid status must preserve native observations")
        self.assertTrue(list((self.root.tenant_home / ".floati-snapshots/v0").glob("*.json")),
                        "must establish an actual derived status snapshot before corruption")
        self.assertEqual(valid, self.path.read_bytes())

        self.path.write_bytes(synthetic)
        unavailable = facts()
        self.assert_unknown_attempt_history(unavailable[self.node])
        self.assertEqual(self.expected_other, unavailable[self.other])
        self.assertEqual(synthetic, self.path.read_bytes())

        self.path.write_bytes(valid)
        restored = facts()
        self.assertEqual(self.healthy_observations, restored[self.node])
        self.assertNotIn("read_finding", restored[self.node])
        self.assertEqual("seat-valid-after", restored[self.node]["last_seen_session"])
        self.assertEqual("recorded_receipts_only",
                         restored[self.node]["recent_sessions"]["native_woke"]["coverage"])
        self.assertEqual(self.expected_other, restored[self.other])
        self.assertEqual(valid, self.path.read_bytes())
        self.assertEqual(others, {path: payload for path, payload in self.ledger_bytes().items()
                                  if path != self.relative}, "only explicit fixture input changes")

    def test_doctor_emits_one_exact_legacy_finding_and_continues_later_checks(self):
        before = self.ledger_bytes()
        with mock.patch("floati.doctor._utc_now", return_value=self.observation):
            artifact, status = Doctor(REPOSITORY_ROOT, self.root.path,
                                      ref="HEAD", no_sandbox=True).artifact()
        self.assertEqual(35, status, "legacy observation is degraded, not generic malformed evidence: "
                         + json.dumps(artifact, sort_keys=True))
        findings = artifact["findings"]
        legacy = [row for row in findings if row["code"] == "wake_receipt_legacy_unreadable"]
        self.assertEqual(1, len(legacy), "one named diagnostic per affected node")
        self.assertEqual(self.relative, legacy[0]["subject"])
        self.assertEqual("error", legacy[0]["severity"])
        self.assert_unknown_attempt_history(legacy[0]["wake_health"])
        self.assertFalse(any(row["code"] == "delivery_health_unavailable" for row in findings))
        other = [row for row in findings if row["code"] == "wake_health"
                 and row["subject"] == self.other]
        self.assertEqual(1, len(other))
        self.assertEqual(self.expected_other, other[0]["wake_health"])
        self.assertEqual("ok", other[0]["severity"])
        self.assertTrue(any(row["code"] in {"manifest_exact_set", "manifest_invalid"}
                            for row in findings), "later manifest check must still execute")
        self.assertTrue(any(row["code"] == "symlink_identity_valid" for row in findings),
                        "later root-identity check must still execute")
        self.assertNotIn(SYNTHETIC_BAD_REFERENCE, json.dumps(artifact))
        self.assertEqual(before, self.ledger_bytes())
        validate_json_schema(artifact, Path("schemas/v1/doctor-artifact.schema.json"))
        for mutation in ("healthy_fact", "generic_code", "non_error"):
            malformed = copy.deepcopy(artifact)
            finding = next(row for row in malformed["findings"]
                           if row["code"] == "wake_receipt_legacy_unreadable")
            if mutation == "healthy_fact":
                finding["wake_health"] = self.expected_other
            elif mutation == "generic_code":
                finding["code"] = "wake_health"
                finding.pop("read_finding")
            else:
                finding["severity"] = "ok"
            with self.subTest(mutation=mutation), self.assertRaises(SchemaValidationError):
                validate_json_schema(malformed, Path("schemas/v1/doctor-artifact.schema.json"))

    def test_strict_attempt_reader_and_all_write_validators_still_reject_bad_reference(self):
        before = self.ledger_bytes()
        for operation in (
            lambda: WakeHealthProjection(self.root)._attempts(self.node),
            lambda: read_records_snapshot(self.root, self.relative,
                                          allowed_kinds={"wake_attempt_receipt"}),
        ):
            with self.assertRaises(IntegrityFailure) as observed:
                operation()
            self.assertEqual("decision_receipt_id_invalid", observed.exception.code)
        for operation in (
            lambda: validate_record(self.bad, self.root.tenant_id,
                                    frozenset({"wake_attempt_receipt"}), integrity=False),
            lambda: append_record(self.root, self.relative, self.bad,
                                  allowed_kinds={"wake_attempt_receipt"}),
            lambda: WakeAttemptLedger(self.root).record(
                recipient=self.node, acting_session_id="seat-write-rejected",
                item_ids=[self.message["id"]], decision_receipt_id=SYNTHETIC_BAD_REFERENCE,
                message_worker_session_id=None, idempotency_key="legacy-write-must-refuse",
                outcome="woke",
            ),
        ):
            with self.assertRaises(ProtocolRefusal) as observed:
                operation()
            self.assertEqual("decision_receipt_id_invalid", observed.exception.code)
        self.assertEqual(before, self.ledger_bytes())

    def test_other_framing_duplicate_tenant_and_node_failures_remain_strict(self):
        valid = self.valid[0]
        cases = {
            "incomplete_jsonl_line": encode_frame(valid) + b"{",
            "duplicate_record_id": encode_frame(valid) * 2,
            "tenant_mismatch": encode_frame(dict(valid, tenant_id="foreign")),
            "wake_health_receipt_invalid": encode_frame(dict(valid, node_id=self.other)),
        }
        for code, payload in cases.items():
            with self.subTest(code=code):
                self.path.write_bytes(payload)
                for operation in (
                    lambda: WakeHealthProjection(self.root).fact(self.node, self.observation),
                    lambda: WakeHealthProjection(self.root).recent_sessions(self.node, self.observation),
                    lambda: FleetProjection(self.root).status_artifact(self.observation),
                ):
                    with self.assertRaises(IntegrityFailure) as observed:
                        operation()
                    self.assertEqual(code, observed.exception.code)
                self.assertEqual(payload, self.path.read_bytes())

    def test_unavailable_fact_schema_closes_finding_and_forbids_salvaged_sessions(self):
        # A separate schema contract test, built from the lawful native fact.
        # This does not grant validity to the synthetic defective wake row.
        fact = json.loads(json.dumps(self.healthy_observations))
        fact.update(state="unknown", last_seen_session=None, read_finding=self.finding())
        native = fact["recent_sessions"]["native_woke"]
        native.update(coverage="unavailable", sessions=[])
        schema = Path("schemas/v1/wake-health-fact.schema.json")
        validate_json_schema(fact, schema)
        unidentified = json.loads(json.dumps(fact))
        unidentified["read_finding"]["record_id"] = None
        validate_json_schema(unidentified, schema)
        changes = [
            ("missing_finding", lambda value: value.pop("read_finding")),
            ("invented_last_seen", lambda value: value.update(last_seen_session="invented-session")),
            ("salvaged_sessions", lambda value: value["recent_sessions"]["native_woke"].update(
                sessions=self.healthy_observations["recent_sessions"]["native_woke"]["sessions"])),
            ("private_detail", lambda value: value["read_finding"].update(
                private_detail=SYNTHETIC_BAD_REFERENCE)),
            ("wrong_code", lambda value: value["read_finding"].update(code="other_failure")),
            ("wrong_source_code", lambda value: value["read_finding"].update(source_code="tenant_mismatch")),
            ("unsafe_ledger", lambda value: value["read_finding"].update(ledger="../foreign.jsonl")),
            ("unvalidated_record_id", lambda value: value["read_finding"].update(record_id="unsafe-id")),
        ]
        for label, change in changes:
            with self.subTest(case=label):
                altered = json.loads(json.dumps(fact))
                change(altered)
                with self.assertRaises(SchemaValidationError):
                    validate_json_schema(altered, schema)


if __name__ == "__main__":
    unittest.main()

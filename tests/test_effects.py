from __future__ import annotations

import json
import hashlib
import tempfile
import unicodedata
import unittest
from copy import deepcopy
from pathlib import Path
from types import MappingProxyType

from floati.effects import EffectLedger, EffectProjection
from floati.errors import IntegrityFailure, ProtocolRefusal
from floati.ids import uuid7_hex
from floati.records import (
    EFFECT_BINDING_FIELDS,
    EFFECT_KINDS,
    EFFECT_TYPES,
    validate_record,
)
from floati.root import FloatiRoot
from tests.schema_validation import SchemaValidationError, validate_json_schema


V1_SCHEMA_DIR = Path("schemas/v1")
_TIMESTAMP = "2026-08-11T12:00:00.000Z"


class EffectRecordFixture:
    """Hand-derived lawful v1 records; no production builders are used here."""

    def __init__(self) -> None:
        self.run_id = "run-" + uuid7_hex()
        self.item_id = "work-" + uuid7_hex()
        self.attempt_id = "attempt-" + uuid7_hex()
        self.attempt_started_id = "attempt-started-" + uuid7_hex()
        self.intent_id = "effect-intent-" + uuid7_hex()
        self.dispatched_id = "effect-dispatched-" + uuid7_hex()
        self.acknowledged_id = "effect-acknowledged-" + uuid7_hex()
        self.confirmed_id = "effect-confirmed-" + uuid7_hex()
        self.failed_id = "effect-failed-" + uuid7_hex()
        self.unknown_id = "effect-unknown-" + uuid7_hex()
        self.reconciled_id = "effect-reconciled-" + uuid7_hex()
        self.proposal_id = "compensation-proposed-" + uuid7_hex()
        self.compensation_operation_id = "effect-op-" + uuid7_hex()
        self.compensation_terminal_id = "effect-confirmed-" + uuid7_hex()
        self._binding = {
            "operation_id": "effect-op-" + uuid7_hex(),
            "run_id": self.run_id,
            "item_id": self.item_id,
            "attempt_id": self.attempt_id,
            "attempt_started_id": self.attempt_started_id,
            "fence_token": "a" * 64,
            "effect_type": "git_ref_update",
            "target": {
                "kind": "git_ref",
                "coordinate": "owner/slipway:refs/heads/main",
                "identity_digest": "b" * 64,
            },
            "request_digest": "c" * 64,
            "idempotency_key": "effect-intent-1",
            "expected_confirmation": {
                "kind": "git_ref_equals",
                "locator": "refs/heads/main",
                "expected_digest": "d" * 64,
            },
            "reconciliation_adapter": "git_local",
            "risk_class": "low",
            "budget_claim": [{"budget_id": "git", "amount": 1}],
        }

    def binding(self) -> dict[str, object]:
        return deepcopy(self._binding)

    def _row(self, kind: str, prefix: str, **fields: object) -> dict[str, object]:
        return {
            "schema_version": 1,
            "id": prefix + uuid7_hex(),
            "tenant_id": "alpha",
            "timestamp": _TIMESTAMP,
            "kind": kind,
            **self.binding(),
            **fields,
        }

    def rows(self) -> dict[str, dict[str, object]]:
        intent = self._row(
            "effect_intent", "effect-intent-", id=self.intent_id,
            requested_by="operator-a", approval_request_id=None,
            approval_decision_id=None, approval_consumption_id=None,
            intended_at_testimony=_TIMESTAMP,
        )
        dispatched = self._row(
            "effect_dispatched", "effect-dispatched-", id=self.dispatched_id,
            effect_intent_id=self.intent_id, dispatch_adapter="git_local",
            dispatch_evidence_digest="e" * 64, dispatched_at_testimony=_TIMESTAMP,
        )
        acknowledged = self._row(
            "effect_acknowledged", "effect-acknowledged-", id=self.acknowledged_id,
            effect_intent_id=self.intent_id, effect_dispatched_id=self.dispatched_id,
            acknowledgement_digest="f" * 64, acknowledged_at_testimony=_TIMESTAMP,
        )
        confirmed = self._row(
            "effect_confirmed", "effect-confirmed-", id=self.confirmed_id,
            effect_intent_id=self.intent_id, effect_dispatched_id=self.dispatched_id,
            effect_acknowledged_id=self.acknowledged_id,
            confirmation={
                "kind": "git_ref_equals", "locator": "refs/heads/main",
                "expected_digest": "d" * 64,
            },
            confirmation_evidence_digest="1" * 64,
            measured_spend=[{"budget_id": "git", "amount": 1}],
            confirmed_at_testimony=_TIMESTAMP,
        )
        failed = self._row(
            "effect_failed", "effect-failed-", id=self.failed_id,
            effect_intent_id=self.intent_id, effect_dispatched_id=self.dispatched_id,
            reason_code="destination_rejected", failure_evidence_digest="2" * 64,
            spend_status="complete", measured_spend=[{"budget_id": "git", "amount": 1}],
            failed_at_testimony=_TIMESTAMP,
        )
        unknown = self._row(
            "effect_unknown", "effect-unknown-", id=self.unknown_id,
            effect_intent_id=self.intent_id, effect_dispatched_id=self.dispatched_id,
            reason_code="confirmation_absent", unknown_evidence_digest="3" * 64,
            spend_status="unknown", measured_spend=None,
            unknown_at_testimony=_TIMESTAMP,
        )
        reconciled = self._row(
            "effect_reconciled", "effect-reconciled-", id=self.reconciled_id,
            effect_intent_id=self.intent_id, prior_effect_evidence_id=self.unknown_id,
            reconciled_outcome="confirmed", reconciliation_evidence_digest="4" * 64,
            confirmation={
                "kind": "git_ref_equals", "locator": "refs/heads/main",
                "expected_digest": "d" * 64,
            },
            spend_status="complete", measured_spend=[{"budget_id": "git", "amount": 1}],
            reconciled_at_testimony=_TIMESTAMP,
        )
        proposed = self._row(
            "compensation_proposed", "compensation-proposed-", id=self.proposal_id,
            effect_intent_id=self.intent_id, source_effect_evidence_id=self.confirmed_id,
            reason_code="operator_requested", compensation_plan_digest="6" * 64,
            compensation_request_digest="5" * 64,
            compensation_operation_id=self.compensation_operation_id,
            compensation_risk_class="medium", approval_request_id=None,
            approval_decision_id=None, approval_consumption_id=None,
            proposed_at_testimony=_TIMESTAMP,
        )
        executed = self._row(
            "compensation_executed", "compensation-executed-",
            compensation_proposal_id=self.proposal_id,
            compensation_operation_id=self.compensation_operation_id,
            compensation_terminal_evidence_id=self.compensation_terminal_id,
            executed_at_testimony=_TIMESTAMP,
        )
        return {
            row["kind"]: row
            for row in (intent, dispatched, acknowledged, confirmed, failed, unknown, reconciled, proposed, executed)
        }


class EffectRecordContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = EffectRecordFixture()
        self.rows = self.fixture.rows()

    @staticmethod
    def schema_path(kind: str) -> Path:
        return V1_SCHEMA_DIR / (kind.replace("_", "-") + "-record.schema.json")

    @staticmethod
    def accepts_runtime(record: dict[str, object]) -> bool:
        try:
            validate_record(deepcopy(record), "alpha", frozenset({record["kind"]}), integrity=False)
        except ProtocolRefusal:
            return False
        return True

    def accepts_schema(self, record: dict[str, object]) -> bool:
        try:
            validate_json_schema(record, self.schema_path(str(record["kind"])))
        except (SchemaValidationError, FileNotFoundError):
            return False
        return True

    def assert_parity(self, record: dict[str, object], expected: bool) -> None:
        self.assertEqual((expected, expected), (self.accepts_runtime(record), self.accepts_schema(record)))

    def test_all_nine_effect_record_shapes_have_runtime_and_schema_positive_controls(self) -> None:
        """Catches a missing kind registration or absent/open v1 lifecycle schema."""
        self.assertEqual(set(self.rows), EFFECT_KINDS)
        for kind, row in self.rows.items():
            with self.subTest(kind=kind):
                self.assert_parity(row, True)
                schema = json.loads(self.schema_path(kind).read_text(encoding="utf-8"))
                self.assertFalse(schema["additionalProperties"])
                self.assertEqual(set(row), set(schema["required"]))
                self.assert_parity(dict(row, unexpected=True), False)

    def test_every_effect_row_repeats_the_exact_immutable_binding(self) -> None:
        """Catches a lifecycle row that loses a binding field needed for replay identity."""
        for kind, row in self.rows.items():
            with self.subTest(kind=kind):
                self.assertTrue(EFFECT_BINDING_FIELDS <= set(row))
                self.assertEqual(self.fixture.binding(), {field: row[field] for field in EFFECT_BINDING_FIELDS})
                for field in EFFECT_BINDING_FIELDS:
                    missing = dict(row)
                    missing.pop(field)
                    self.assert_parity(missing, False)

    def test_target_and_confirmation_shapes_are_closed_and_non_secret(self) -> None:
        """Catches a target or confirmation contract admitting secret-bearing shape expansion."""
        for kind, row in self.rows.items():
            with self.subTest(kind=kind, field="target"):
                target = dict(row["target"])
                target["token"] = "secret"
                self.assert_parity(dict(row, target=target), False)
            with self.subTest(kind=kind, field="expected_confirmation"):
                expected = dict(row["expected_confirmation"])
                expected["request_body"] = "secret"
                self.assert_parity(dict(row, expected_confirmation=expected), False)
        for kind in ("effect_confirmed", "effect_reconciled"):
            confirmation = dict(self.rows[kind]["confirmation"])
            confirmation["cookie"] = "secret"
            self.assert_parity(dict(self.rows[kind], confirmation=confirmation), False)

    def test_effect_type_and_target_kind_pairing_has_runtime_schema_parity(self) -> None:
        """Catches a schema accepting a target kind for the wrong closed effect type."""
        pairs = (
            ("git_ref_update", "git_ref"),
            ("git_remote_ref_update", "git_remote_ref"),
            ("github_mutation", "github_resource"),
            ("deployment", "deployment_target"),
            ("shell_command", "shell_environment"),
            ("external_api", "external_api_resource"),
        )
        row = self.rows["effect_intent"]
        for effect_type, expected_target_kind in pairs:
            with self.subTest(effect_type=effect_type, positive=True):
                target = dict(row["target"], kind=expected_target_kind)
                self.assert_parity(dict(row, effect_type=effect_type, target=target), True)
            for _, wrong_target_kind in pairs:
                if wrong_target_kind == expected_target_kind:
                    continue
                with self.subTest(effect_type=effect_type, target_kind=wrong_target_kind):
                    target = dict(row["target"], kind=wrong_target_kind)
                    self.assert_parity(dict(row, effect_type=effect_type, target=target), False)

    def test_budget_claim_and_measured_spend_are_sorted_bounded_maps(self) -> None:
        """Catches budget tables admitting duplicate, reordered, negative, or extra values."""
        for kind, row in self.rows.items():
            with self.subTest(kind=kind, field="budget_claim"):
                self.assert_parity(dict(row, budget_claim=[{"budget_id": "z", "amount": 1}, {"budget_id": "a", "amount": 1}]), False)
                self.assert_parity(dict(row, budget_claim=[{"budget_id": "git", "amount": -1}]), False)
        for kind in ("effect_confirmed", "effect_failed", "effect_reconciled"):
            with self.subTest(kind=kind, field="measured_spend"):
                self.assert_parity(dict(self.rows[kind], measured_spend=[{"budget_id": "git", "amount": 1, "secret": "no"}]), False)

    def test_effect_string_fields_have_complete_runtime_schema_unicode_parity(self) -> None:
        """Catches either layer omitting any Cc, Cs, or terminal bidi control."""
        from floati.records import _terminal_unsafe

        unsafe = tuple(
            chr(codepoint)
            for codepoint in range(0x110000)
            if unicodedata.category(chr(codepoint)) in {"Cc", "Cs"}
            or unicodedata.bidirectional(chr(codepoint))
            in {"LRE", "RLE", "LRO", "RLO", "PDF", "LRI", "RLI", "FSI", "PDI", "BN"}
        )
        self.assertEqual(2248, len(unsafe))
        self.assertTrue(all(_terminal_unsafe(value) for value in unsafe))
        controls = ("\u061c", "\u200e", "\u200f", "\u2065", "\ufff9", "\ufffa", "\ufffb")
        self.assertTrue(all(not _terminal_unsafe(value) for value in controls))
        for kind, row in self.rows.items():
            with self.subTest(kind=kind, safe_control="target.coordinate"):
                self.assert_parity(dict(row, target=dict(row["target"], coordinate="coordinate" + controls[0])), True)
            for character in unsafe:
                target = dict(row["target"], coordinate="coordinate" + character)
                with self.subTest(kind=kind, codepoint=f"U+{ord(character):04X}"):
                    self.assert_parity(dict(row, target=target), False)

    def test_effect_numeric_and_nullable_fields_have_runtime_schema_parity(self) -> None:
        """Catches numeric/nullable disagreements around maps and optional references."""
        for kind, row in self.rows.items():
            with self.subTest(kind=kind, schema_version_float=True):
                self.assert_parity(dict(row, schema_version=1.0), True)
            with self.subTest(kind=kind, schema_version_boolean=True):
                self.assert_parity(dict(row, schema_version=True), False)
        for kind in ("effect_intent", "compensation_proposed"):
            for field in ("approval_request_id", "approval_decision_id", "approval_consumption_id"):
                with self.subTest(kind=kind, field=field):
                    record = dict(self.rows[kind], **{field: "approval-request-" + uuid7_hex()})
                    self.assert_parity(record, False if field != "approval_request_id" else True)
        for kind in ("effect_failed", "effect_unknown", "effect_reconciled"):
            with self.subTest(kind=kind, nullable_spend=True):
                self.assert_parity(dict(self.rows[kind], measured_spend=None), True)
        self.assert_parity(dict(self.rows["effect_confirmed"], measured_spend=None), False)

    def test_effect_status_artifact_is_closed_and_versioned(self) -> None:
        """Catches a read-only status artifact without a versioned closed envelope."""
        path = V1_SCHEMA_DIR / "effect-status-artifact.schema.json"
        artifact = {
            "schema_version": 1,
            "artifact_version": 0,
            "command": "effects",
            "status": "ok",
            "evidence": {
                "status_schema_version": 1,
                "kind": "effect_status",
                "observed_at": _TIMESTAMP,
                "operations": [{
                    "operation_id": self.rows["effect_intent"]["operation_id"],
                    "run_id": self.rows["effect_intent"]["run_id"],
                    "item_id": self.rows["effect_intent"]["item_id"],
                    "attempt_id": self.rows["effect_intent"]["attempt_id"],
                    "effect_type": "git_ref_update",
                    "risk_class": "low",
                    "state": "confirmed",
                    "current_evidence_id": self.rows["effect_confirmed"]["id"],
                    "reconciliation_adapter": "git_local",
                    "spend_status": "complete",
                    "budget_claim": [{"budget_id": "git", "amount": 1}],
                    "measured_spend": [{"budget_id": "git", "amount": 1}],
                    "compensation_state": "none",
                }],
            },
        }
        validate_json_schema(artifact, path)
        with self.assertRaises(SchemaValidationError):
            validate_json_schema(dict(artifact, raw_record={}), path)


class EffectProjectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = EffectRecordFixture()
        self.rows = self.fixture.rows()

    def project(self, *kinds: str) -> EffectProjection:
        return EffectProjection.from_records([deepcopy(self.rows[kind]) for kind in kinds])

    def test_intent_dispatch_ack_confirm_projects_confirmed(self) -> None:
        """Catches replay losing a lawful acknowledgement or confirmed terminal state."""
        operation = self.project(
            "effect_intent", "effect_dispatched", "effect_acknowledged", "effect_confirmed"
        ).operation(self.fixture.binding()["operation_id"])
        self.assertEqual("confirmed", operation["state"])
        self.assertEqual(self.rows["effect_confirmed"]["id"], operation["current_evidence_id"])
        self.assertEqual("complete", operation["spend_status"])
        self.assertEqual((("git", 1),), operation["measured_spend"])
        self.assertIsInstance(operation, MappingProxyType)
        with self.assertRaises(TypeError):
            operation["state"] = "unknown"

    def test_confirmed_requires_exact_expected_confirmation(self) -> None:
        """Catches direct confirmation accepting a different kind, locator, or digest."""
        confirmed = deepcopy(self.rows["effect_confirmed"])
        confirmed["effect_acknowledged_id"] = None
        lawful = EffectProjection.from_records([
            self.rows["effect_intent"], self.rows["effect_dispatched"], confirmed,
        ])
        self.assertEqual(
            "confirmed", lawful.operation(self.fixture.binding()["operation_id"])["state"]
        )
        mutations = (
            ("kind", "git_remote_ref_equals"),
            ("locator", "refs/heads/other"),
            ("expected_digest", "9" * 64),
        )
        for field, value in mutations:
            with self.subTest(field=field):
                mismatched = deepcopy(confirmed)
                mismatched["confirmation"][field] = value
                with self.assertRaises(IntegrityFailure) as caught:
                    EffectProjection.from_records([
                        self.rows["effect_intent"], self.rows["effect_dispatched"], mismatched,
                    ])
                self.assertEqual("effect_confirmation_mismatch", caught.exception.code)

    def test_reconciled_confirmed_requires_exact_expected_confirmation(self) -> None:
        """Catches reconciliation confirmation accepting a different expected state."""
        reconciled = deepcopy(self.rows["effect_reconciled"])
        reconciled["prior_effect_evidence_id"] = self.rows["effect_unknown"]["id"]
        lawful_prefix = [
            self.rows["effect_intent"], self.rows["effect_dispatched"],
            self.rows["effect_unknown"],
        ]
        lawful = EffectProjection.from_records(lawful_prefix + [reconciled])
        self.assertEqual(
            "reconciled_confirmed",
            lawful.operation(self.fixture.binding()["operation_id"])["state"],
        )
        mutations = (
            ("kind", "deployment_artifact_equals"),
            ("locator", "deployments/other"),
            ("expected_digest", "8" * 64),
        )
        for field, value in mutations:
            with self.subTest(field=field):
                mismatched = deepcopy(reconciled)
                mismatched["confirmation"][field] = value
                with self.assertRaises(IntegrityFailure) as caught:
                    EffectProjection.from_records(lawful_prefix + [mismatched])
                self.assertEqual("effect_confirmation_mismatch", caught.exception.code)

    def test_confirmed_states_require_exact_claimed_budget_key_coverage(self) -> None:
        """Catches confirmed truth omitting a claimed budget while projecting complete."""
        for terminal_kind, prefix_kinds in (
            (
                "effect_confirmed",
                ("effect_intent", "effect_dispatched"),
            ),
            (
                "effect_reconciled",
                ("effect_intent", "effect_dispatched", "effect_unknown"),
            ),
        ):
            with self.subTest(terminal_kind=terminal_kind, coverage="exact"):
                rows = EffectRecordFixture().rows()
                for row in rows.values():
                    row["budget_claim"] = [
                        {"budget_id": "git", "amount": 1},
                        {"budget_id": "network", "amount": 2},
                    ]
                terminal = deepcopy(rows[terminal_kind])
                terminal["measured_spend"] = [
                    {"budget_id": "git", "amount": 1},
                    {"budget_id": "network", "amount": 2},
                ]
                if terminal_kind == "effect_confirmed":
                    terminal["effect_acknowledged_id"] = None
                projection = EffectProjection.from_records(
                    [rows[kind] for kind in prefix_kinds] + [terminal]
                )
                operation_id = str(rows["effect_intent"]["operation_id"])
                self.assertIn(
                    projection.operation(operation_id)["state"],
                    {"confirmed", "reconciled_confirmed"},
                )

            for measured_spend in (
                [],
                [{"budget_id": "git", "amount": 1}],
            ):
                with self.subTest(
                    terminal_kind=terminal_kind,
                    measured_spend=measured_spend,
                ):
                    rows = EffectRecordFixture().rows()
                    for row in rows.values():
                        row["budget_claim"] = [
                            {"budget_id": "git", "amount": 1},
                            {"budget_id": "network", "amount": 2},
                        ]
                    terminal = deepcopy(rows[terminal_kind])
                    terminal["measured_spend"] = measured_spend
                    if terminal_kind == "effect_confirmed":
                        terminal["effect_acknowledged_id"] = None
                    with self.assertRaises(IntegrityFailure) as caught:
                        EffectProjection.from_records(
                            [rows[kind] for kind in prefix_kinds] + [terminal]
                        )
                    self.assertEqual("effect_evidence_invalid", caught.exception.code)

    def test_intent_dispatch_failed_and_unknown_remain_distinct(self) -> None:
        """Catches known failure and uncertainty collapsing into one projected state."""
        failed = self.project("effect_intent", "effect_dispatched", "effect_failed")
        self.assertEqual("failed", failed.operation(self.fixture.binding()["operation_id"])["state"])
        fixture = EffectRecordFixture()
        rows = fixture.rows()
        unknown = EffectProjection.from_records(
            [rows["effect_intent"], rows["effect_dispatched"], rows["effect_unknown"]]
        )
        self.assertEqual("unknown", unknown.operation(fixture.binding()["operation_id"])["state"])

    def test_failed_or_unknown_can_reconcile_but_confirmed_cannot_regress(self) -> None:
        """Catches reconciliation rejecting lawful uncertainty or regressing confirmation."""
        for primary in ("effect_failed", "effect_unknown"):
            rows = [deepcopy(self.rows[kind]) for kind in ("effect_intent", "effect_dispatched", primary)]
            reconciled = deepcopy(self.rows["effect_reconciled"])
            reconciled["prior_effect_evidence_id"] = rows[-1]["id"]
            projection = EffectProjection.from_records(rows + [reconciled])
            self.assertEqual(
                "reconciled_confirmed",
                projection.operation(self.fixture.binding()["operation_id"])["state"],
            )
        regressed = deepcopy(self.rows["effect_reconciled"])
        regressed["prior_effect_evidence_id"] = self.rows["effect_confirmed"]["id"]
        with self.assertRaises(IntegrityFailure) as caught:
            EffectProjection.from_records([
                self.rows["effect_intent"], self.rows["effect_dispatched"],
                self.rows["effect_acknowledged"], self.rows["effect_confirmed"], regressed,
            ])
        self.assertEqual("effect_transition_invalid", caught.exception.code)

    def test_later_reconciliation_requires_new_evidence_digest(self) -> None:
        """Catches a later reconciliation recycling evidence already used for the operation."""
        first = deepcopy(self.rows["effect_reconciled"])
        first.update({
            "prior_effect_evidence_id": self.rows["effect_unknown"]["id"],
            "reconciled_outcome": "unknown",
            "confirmation": None,
            "spend_status": "unknown",
            "measured_spend": None,
        })
        second = deepcopy(self.rows["effect_reconciled"])
        second["id"] = "effect-reconciled-" + uuid7_hex()
        second["prior_effect_evidence_id"] = first["id"]
        lawful = deepcopy(second)
        lawful["reconciliation_evidence_digest"] = "6" * 64
        prefix = [
            self.rows["effect_intent"], self.rows["effect_dispatched"],
            self.rows["effect_unknown"], first,
        ]
        projection = EffectProjection.from_records(prefix + [lawful])
        self.assertEqual(
            "reconciled_confirmed",
            projection.operation(self.fixture.binding()["operation_id"])["state"],
        )
        with self.assertRaises(IntegrityFailure) as caught:
            EffectProjection.from_records(prefix + [second])
        self.assertEqual("effect_evidence_invalid", caught.exception.code)

    def test_compensation_execution_requires_separate_post_proposal_matching_operation(self) -> None:
        """Catches self, cross-digest, or pre-proposal operations proving compensation execution."""
        source_confirmed = deepcopy(self.rows["effect_confirmed"])
        source_confirmed["effect_acknowledged_id"] = None
        source_prefix = [
            self.rows["effect_intent"], self.rows["effect_dispatched"], source_confirmed,
        ]

        compensation = EffectRecordFixture()
        compensation._binding.update({
            "run_id": self.fixture.run_id,
            "item_id": self.fixture.item_id,
            "attempt_id": self.fixture.attempt_id,
            "attempt_started_id": self.fixture.attempt_started_id,
            "fence_token": self.fixture.binding()["fence_token"],
            "request_digest": self.rows["compensation_proposed"]["compensation_request_digest"],
            "idempotency_key": "effect-compensation-1",
        })
        compensation_rows = compensation.rows()
        compensation_confirmed = deepcopy(compensation_rows["effect_confirmed"])
        compensation_confirmed["effect_acknowledged_id"] = None

        proposed = deepcopy(self.rows["compensation_proposed"])
        proposed["source_effect_evidence_id"] = source_confirmed["id"]
        proposed["compensation_operation_id"] = compensation.binding()["operation_id"]
        executed = deepcopy(self.rows["compensation_executed"])
        executed["compensation_operation_id"] = compensation.binding()["operation_id"]
        executed["compensation_terminal_evidence_id"] = compensation_confirmed["id"]
        lawful_compensation = [
            compensation_rows["effect_intent"], compensation_rows["effect_dispatched"],
            compensation_confirmed,
        ]
        projection = EffectProjection.from_records(
            source_prefix + [proposed] + lawful_compensation + [executed]
        )
        self.assertEqual(
            "executed",
            projection.operation(self.fixture.binding()["operation_id"])["compensation_state"],
        )

        self_proposed = deepcopy(proposed)
        self_proposed["compensation_operation_id"] = self.fixture.binding()["operation_id"]
        self_executed = deepcopy(executed)
        self_executed["compensation_operation_id"] = self.fixture.binding()["operation_id"]
        self_executed["compensation_terminal_evidence_id"] = source_confirmed["id"]
        with self.assertRaises(IntegrityFailure) as self_refusal:
            EffectProjection.from_records(source_prefix + [self_proposed, self_executed])
        self.assertEqual("effect_transition_invalid", self_refusal.exception.code)

        mismatched_intent = deepcopy(compensation_rows["effect_intent"])
        mismatched_intent["request_digest"] = "7" * 64
        mismatched_binding = {
            field: deepcopy(mismatched_intent[field]) for field in EFFECT_BINDING_FIELDS
        }
        mismatched_lifecycle = []
        for row in (compensation_rows["effect_dispatched"], compensation_confirmed):
            changed = deepcopy(row)
            changed.update(deepcopy(mismatched_binding))
            mismatched_lifecycle.append(changed)
        with self.assertRaises(IntegrityFailure) as digest_refusal:
            EffectProjection.from_records(
                source_prefix + [proposed, mismatched_intent] + mismatched_lifecycle + [executed]
            )
        self.assertEqual("effect_evidence_invalid", digest_refusal.exception.code)

        with self.assertRaises(IntegrityFailure) as order_refusal:
            EffectProjection.from_records(
                source_prefix + lawful_compensation + [proposed, executed]
            )
        self.assertEqual("effect_transition_invalid", order_refusal.exception.code)


    def test_exactly_one_primary_outcome_wins_by_physical_order(self) -> None:
        """Catches a second terminal row overwriting the first physical outcome."""
        for first, second in (("effect_failed", "effect_unknown"), ("effect_unknown", "effect_confirmed")):
            with self.subTest(first=first, second=second):
                with self.assertRaises(IntegrityFailure) as caught:
                    self.project("effect_intent", "effect_dispatched", first, second)
                self.assertEqual("effect_transition_invalid", caught.exception.code)

    def test_forward_cross_run_cross_attempt_and_changed_binding_references_refuse(self) -> None:
        """Catches accepting lifecycle rows that do not belong to the preceding physical intent."""
        with self.assertRaises(IntegrityFailure) as malformed:
            EffectProjection.from_records([None])  # type: ignore[list-item]
        self.assertEqual("record_not_object", malformed.exception.code)
        with self.assertRaises(IntegrityFailure) as forward:
            self.project("effect_dispatched", "effect_intent")
        self.assertEqual("effect_transition_invalid", forward.exception.code)

        mutations = (
            ("run_id", "run-" + uuid7_hex()),
            ("attempt_id", "attempt-" + uuid7_hex()),
            ("request_digest", "9" * 64),
            ("effect_intent_id", "effect-intent-" + uuid7_hex()),
        )
        for field, value in mutations:
            with self.subTest(field=field):
                dispatched = deepcopy(self.rows["effect_dispatched"])
                dispatched[field] = value
                with self.assertRaises(IntegrityFailure) as caught:
                    EffectProjection.from_records([self.rows["effect_intent"], dispatched])
                self.assertEqual("effect_evidence_invalid", caught.exception.code)

    def test_physical_order_not_timestamp_selects_current_state(self) -> None:
        """Catches testimony timestamps overriding append position during reconciliation."""
        failed = deepcopy(self.rows["effect_failed"])
        failed["timestamp"] = "2026-08-11T23:00:00.000Z"
        reconciled = deepcopy(self.rows["effect_reconciled"])
        reconciled["prior_effect_evidence_id"] = failed["id"]
        reconciled["timestamp"] = "2026-08-11T01:00:00.000Z"
        projection = EffectProjection.from_records([
            self.rows["effect_intent"], self.rows["effect_dispatched"], failed, reconciled,
        ])
        self.assertEqual(
            "reconciled_confirmed",
            projection.operation(self.fixture.binding()["operation_id"])["state"],
        )

    def test_acceptance_evidence_sorts_operations_and_hashes_exact_terminal_rows(self) -> None:
        """Catches unsorted operation binding or a digest that omits physical watermark/terminal rows."""
        other = EffectRecordFixture()
        other._binding.update({
            "run_id": self.fixture.run_id,
            "item_id": self.fixture.item_id,
            "attempt_id": self.fixture.attempt_id,
            "attempt_started_id": self.fixture.attempt_started_id,
            "fence_token": self.fixture.binding()["fence_token"],
            "idempotency_key": "effect-intent-2",
        })
        other_rows = other.rows()
        other_rows["effect_confirmed"]["effect_acknowledged_id"] = None
        own_confirmed = deepcopy(self.rows["effect_confirmed"])
        own_confirmed["effect_acknowledged_id"] = None
        records = [
            other_rows["effect_intent"], other_rows["effect_dispatched"], other_rows["effect_confirmed"],
            self.rows["effect_intent"], self.rows["effect_dispatched"], own_confirmed,
        ]
        projection = EffectProjection.from_records(records)
        evidence = projection.acceptance_evidence(self.fixture.run_id, self.fixture.attempt_id)
        operation_ids = tuple(sorted((
            str(self.fixture.binding()["operation_id"]), str(other.binding()["operation_id"]),
        )))
        terminal_by_operation = {
            str(other.binding()["operation_id"]): other_rows["effect_confirmed"],
            str(self.fixture.binding()["operation_id"]): own_confirmed,
        }
        payload = {
            "effect_operation_ids": list(operation_ids),
            "effect_ledger_high_watermark": 6,
            "terminal_effect_rows": [terminal_by_operation[value] for value in operation_ids],
        }
        expected = hashlib.sha256(json.dumps(
            payload, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")).hexdigest()
        self.assertEqual(operation_ids, evidence.operation_ids)
        self.assertEqual(6, evidence.high_watermark)
        self.assertEqual(expected, evidence.evidence_digest)
        self.assertEqual((), evidence.blockers)

    def test_spend_counts_only_confirmed_or_reconciled_confirmed(self) -> None:
        """Catches failed/unknown testimony being charged as accepted measured spend."""
        fixtures = [EffectRecordFixture() for _ in range(4)]
        for index, fixture in enumerate(fixtures[1:], start=2):
            fixture._binding.update({
                "run_id": fixtures[0].run_id,
                "item_id": fixtures[0].item_id,
                "attempt_id": fixtures[0].attempt_id,
                "attempt_started_id": fixtures[0].attempt_started_id,
                "fence_token": fixtures[0].binding()["fence_token"],
                "idempotency_key": f"effect-intent-{index}",
            })
        records: list[dict[str, object]] = []
        terminal_kinds = ("effect_confirmed", "effect_failed", "effect_unknown", "effect_reconciled")
        for fixture, terminal in zip(fixtures, terminal_kinds):
            rows = fixture.rows()
            records.extend((rows["effect_intent"], rows["effect_dispatched"]))
            if terminal == "effect_reconciled":
                records.append(rows["effect_unknown"])
                reconciled = deepcopy(rows[terminal])
                reconciled["prior_effect_evidence_id"] = rows["effect_unknown"]["id"]
                records.append(reconciled)
            else:
                terminal_row = deepcopy(rows[terminal])
                if terminal == "effect_confirmed":
                    terminal_row["effect_acknowledged_id"] = None
                records.append(terminal_row)
        evidence = EffectProjection.from_records(records).acceptance_evidence(
            fixtures[0].run_id, fixtures[0].attempt_id
        )
        self.assertEqual((("git", 2),), evidence.measured_spend)
        self.assertEqual(2, len(evidence.blockers))

    def test_projected_collections_are_copy_safe(self) -> None:
        """Catches callers mutating input rows or nested projected collections after replay."""
        records = [deepcopy(self.rows[kind]) for kind in (
            "effect_intent", "effect_dispatched", "effect_confirmed"
        )]
        records[-1]["effect_acknowledged_id"] = None
        projection = EffectProjection.from_records(records)
        records[0]["target"]["coordinate"] = "mutated"
        operation = projection.operation(self.fixture.binding()["operation_id"])
        self.assertEqual("owner/slipway:refs/heads/main", operation["target"]["coordinate"])
        with self.assertRaises(TypeError):
            operation["target"]["coordinate"] = "mutated"
        self.assertIsInstance(projection.for_attempt(self.fixture.run_id, self.fixture.attempt_id), tuple)


class EffectCompensationProjectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.source = EffectRecordFixture()
        self.rows = self.source.rows()
        self.source_confirmed = deepcopy(self.rows["effect_confirmed"])
        self.source_confirmed["effect_acknowledged_id"] = None
        self.source_prefix = [
            self.rows["effect_intent"], self.rows["effect_dispatched"],
            self.source_confirmed,
        ]
        self.compensation = EffectRecordFixture()
        self.compensation._binding.update({
            "run_id": self.source.run_id,
            "item_id": self.source.item_id,
            "attempt_id": self.source.attempt_id,
            "attempt_started_id": self.source.attempt_started_id,
            "fence_token": self.source.binding()["fence_token"],
            "request_digest": self.rows["compensation_proposed"]["compensation_request_digest"],
            "idempotency_key": "effect-compensation-projection",
        })
        self.compensation_rows = self.compensation.rows()
        self.compensation_confirmed = deepcopy(self.compensation_rows["effect_confirmed"])
        self.compensation_confirmed["effect_acknowledged_id"] = None
        self.proposal = deepcopy(self.rows["compensation_proposed"])
        self.proposal["source_effect_evidence_id"] = self.source_confirmed["id"]
        self.proposal["compensation_operation_id"] = self.compensation.binding()["operation_id"]
        self.executed = deepcopy(self.rows["compensation_executed"])
        self.executed["compensation_operation_id"] = self.compensation.binding()["operation_id"]
        self.executed["compensation_terminal_evidence_id"] = self.compensation_confirmed["id"]

    def test_compensation_executed_requires_separate_confirmed_terminal_evidence(self) -> None:
        """Catches intent, dispatch, or a proposal being accepted as compensation execution."""
        for compensation_prefix in (
            [self.compensation_rows["effect_intent"]],
            [self.compensation_rows["effect_intent"], self.compensation_rows["effect_dispatched"]],
        ):
            with self.subTest(length=len(compensation_prefix)), self.assertRaises(IntegrityFailure) as caught:
                EffectProjection.from_records(
                    self.source_prefix + [self.proposal] + compensation_prefix + [self.executed]
                )
            self.assertEqual("effect_transition_invalid", caught.exception.code)
        projection = EffectProjection.from_records(
            self.source_prefix + [self.proposal, self.compensation_rows["effect_intent"],
                                  self.compensation_rows["effect_dispatched"],
                                  self.compensation_confirmed, self.executed]
        )
        self.assertEqual(
            "executed",
            projection.operation(self.source.binding()["operation_id"])["compensation_state"],
        )

    def test_changed_or_cross_operation_compensation_references_refuse(self) -> None:
        """Catches changed proposal bindings or another operation's terminal evidence."""
        other = EffectRecordFixture()
        other_rows = other.rows()
        changed = deepcopy(self.executed)
        changed["compensation_operation_id"] = other.binding()["operation_id"]
        cross = deepcopy(self.executed)
        cross["compensation_terminal_evidence_id"] = other_rows["effect_confirmed"]["id"]
        prefix = self.source_prefix + [
            self.proposal, self.compensation_rows["effect_intent"],
            self.compensation_rows["effect_dispatched"], self.compensation_confirmed,
        ]
        for candidate in (changed, cross):
            with self.subTest(candidate=candidate), self.assertRaises(IntegrityFailure) as caught:
                EffectProjection.from_records(prefix + [candidate])
            self.assertEqual("effect_evidence_invalid", caught.exception.code)


class EffectLedgerAuthorityTests(unittest.TestCase):
    def test_public_raw_append_and_retained_capability_attempts_refuse(self) -> None:
        """Catches EffectLedger exposing raw append or trusting copied private state."""
        fixture = EffectRecordFixture()
        intent = fixture.rows()["effect_intent"]
        with tempfile.TemporaryDirectory() as directory:
            ledger = EffectLedger(FloatiRoot.open_direct_home(Path(directory) / "alpha", create=True))
            self.assertFalse(hasattr(ledger, "append"))
            capability = object()
            ledger._EffectLedger__controller_capability = capability
            retained = ledger._append_controller
            with self.assertRaises(ProtocolRefusal) as caught:
                retained(intent, capability)
            self.assertEqual("effect_controller_only", caught.exception.code)
            self.assertEqual([], ledger.records())

    def test_forged_controller_owner_and_capability_cannot_enter_transaction(self) -> None:
        """Catches copied owner/token attributes bypassing exact controller call frames."""
        fixture = EffectRecordFixture()
        intent = fixture.rows()["effect_intent"]
        with tempfile.TemporaryDirectory() as directory:
            ledger = EffectLedger(FloatiRoot.open_direct_home(Path(directory) / "alpha", create=True))
            capability = object()
            ledger._EffectLedger__controller_capability = capability
            ledger._EffectLedger__controller_owner = object()
            with self.assertRaises(ProtocolRefusal) as caught:
                ledger._append_controller(intent, capability)
            self.assertEqual("effect_controller_only", caught.exception.code)
            self.assertEqual([], ledger.records())


if __name__ == "__main__":
    unittest.main()

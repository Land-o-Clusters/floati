from __future__ import annotations

from floati import fixture_ids as public_ids

import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from floati.errors import IntegrityFailure, ProtocolRefusal
from floati.decisions import decision_digest
from floati.ids import uuid7_hex
from floati.jsonl import append_record, read_records
from floati.records import run_admission_digest, validate_record
from floati.root import FloatiRoot


class RecordValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = FloatiRoot.open(Path(self.temp.name), "alpha")
        self.path = Path("registry/entries.jsonl")

    def valid(self) -> dict:
        return {
            "schema_version": 0,
            "id": "registry-" + uuid7_hex(),
            "tenant_id": "alpha",
            "timestamp": "2026-07-31T12:00:00.000Z",
            "kind": "registry_entry",
            "node_id": "worker-1",
            "role": "worker",
            "state": "active",
        }

    def test_thread_observation_record_contract(self) -> None:
        """Catches any private thread testimony row falling outside strict v1 validation."""
        from tests.test_thread_observations import (
            THREAD_OBSERVATION_KINDS,
            thread_record_rows,
        )

        for row in thread_record_rows("alpha"):
            with self.subTest(kind=row["kind"]):
                try:
                    validated = validate_record(
                        dict(row), "alpha", THREAD_OBSERVATION_KINDS, integrity=False
                    )
                except ProtocolRefusal as exc:
                    self.fail(f"runtime rejected lawful {row['kind']}: {exc.code}")
                self.assertEqual(row, validated)

    def test_effect_records_require_v1_and_preserve_refusal_integrity_split(self) -> None:
        """Catches a post-v1 effect row silently entering a legacy record contract."""
        from tests.test_effects import EffectRecordFixture

        record = EffectRecordFixture().rows()["effect_intent"]
        validate_record(record, "alpha", frozenset({"effect_intent"}), integrity=False)
        with self.assertRaises(ProtocolRefusal) as rejected:
            validate_record(dict(record, schema_version=0), "alpha", frozenset({"effect_intent"}), integrity=False)
        self.assertEqual("schema_version_invalid", rejected.exception.code)
        with self.assertRaises(IntegrityFailure) as corrupt:
            validate_record(dict(record, schema_version=0), "alpha", frozenset({"effect_intent"}), integrity=True)
        self.assertEqual("schema_version_invalid", corrupt.exception.code)

    def valid_message(self) -> dict:
        return {
            "schema_version": 0,
            "id": "msg-" + uuid7_hex(),
            "tenant_id": "alpha",
            "timestamp": "2026-07-31T12:00:00.000Z",
            "kind": "message_envelope",
            "sender": public_ids.worker('alpha'),
            "recipient": "bob",
            "repo": "owner/floati",
            "sha": "a" * 40,
            "doc": "docs/evidence/checkpoint.md",
            "note": "HM-0.5 delivered",
            "idempotency_key": "checkpoint-a",
        }

    def valid_decision(self) -> dict:
        record = {
            "schema_version": 0,
            "id": "decision-record-" + uuid7_hex(),
            "tenant_id": "alpha",
            "timestamp": "2026-08-08T12:00:00.000Z",
            "kind": "decision_record",
            "repository": "Owner/Repo",
            "decision_id": "decision-" + uuid7_hex(),
            "scope": {"kind": "repository"},
            "statement": "Keep physical order authoritative.",
            "status": "proposed",
            "author_authority": "worker",
            "source_artifact_ids": ["run:run-" + uuid7_hex()],
            "task_contract_id": None,
            "decided_by": public_ids.reviewer(),
            "supersedes": None,
        }
        record["decision_digest"] = decision_digest(record)
        return record

    def valid_harness_binding(
        self, schema_version: int, harness_segments: list[dict[str, object]]
    ) -> dict:
        return {
            "schema_version": schema_version,
            "id": "attempt-harness-session-bound-" + uuid7_hex(),
            "tenant_id": "alpha",
            "timestamp": "2026-08-08T12:00:00.000Z",
            "kind": "attempt_harness_session_bound",
            "run_id": "run-" + uuid7_hex(),
            "item_id": "work-" + uuid7_hex(),
            "attempt_id": "attempt-" + uuid7_hex(),
            "fence_token": "a" * 64,
            "claim_id": "claim-" + uuid7_hex(),
            "lease_id": "lease-" + uuid7_hex(),
            "worker_session_id": "worker-" + uuid7_hex(),
            "harness_segments": harness_segments,
        }

    def valid_run_admission(self) -> dict:
        workers = [{"node_id": public_ids.worker('alpha'), "worker_profile": "codex"}]
        reservations = [{"budget_id": "build", "amount": 1}]
        items = [{
            "item_id": "work-018f7e9b3c117abc8def0123456789ab",
            "workspace_key": "workspace-a",
            "concurrency_key": "concurrency-a",
            "capability_selector": "review-write",
        }]
        return {
            "schema_version": 1,
            "id": "run-admission-bound-" + uuid7_hex(),
            "tenant_id": "alpha",
            "timestamp": "2026-08-09T12:00:00.000Z",
            "kind": "run_admission_bound",
            "run_id": "run-018f7e9b3c127abc8def0123456789ab",
            "plan_digest": "a" * 64,
            "policy_digest": "b" * 64,
            "max_active_attempts": 1,
            "workers": workers,
            "budget_reservations": reservations,
            "items": items,
            "admission_digest": run_admission_digest(
                workers, 1, reservations, items
            ),
        }

    def valid_v1_approval_pair(self, decision: str = "approved") -> tuple[dict, dict]:
        request = {
            "schema_version": 1,
            "id": "approval-request-018f7e9b3c117abc8def0123456789ab",
            "tenant_id": "alpha",
            "timestamp": "2026-08-09T12:00:00.000Z",
            "kind": "approval_request",
            "requester": public_ids.worker('alpha'),
            "capability": "workspace.patch",
            "scope": "repo:floati",
            "requested_ttl_seconds": 60,
            "requested_at": "2026-08-09T12:00:00.000Z",
            "expires_at": "2026-08-09T12:01:00.000Z",
            "authority_subject": "approve-build",
            "authority_epoch": 7,
            "exact_action_digest": "a" * 64,
        }
        approved = decision == "approved"
        receipt = {
            "schema_version": 1,
            "id": "approval-decision-018f7e9b3c127abc8def0123456789ab",
            "tenant_id": "alpha",
            "timestamp": "2026-08-09T12:00:01.000Z",
            "kind": "approval_decision",
            "request_id": request["id"],
            "decider": public_ids.reviewer(),
            "decision": decision,
            "granted_scope": "repo:floati" if approved else None,
            "granted_ttl_seconds": 30 if approved else None,
            "reason_code": None if approved else "operator_denied",
            "decided_at": "2026-08-09T12:00:01.000Z",
            "expires_at": "2026-08-09T12:00:31.000Z" if approved else None,
            "authority_subject": "approve-build",
            "authority_epoch": 7,
            "exact_action_digest": request["exact_action_digest"],
        }
        return request, receipt

    def test_v1_approval_action_binding_contract(self) -> None:
        """Catches action digest loss, open fields, or decision-branch drift at runtime."""
        request, approved = self.valid_v1_approval_pair()
        _, denied = self.valid_v1_approval_pair("denied")
        for record in (request, approved, denied):
            with self.subTest(kind=record["kind"], decision=record.get("decision")):
                self.assertEqual(
                    record,
                    validate_record(
                        dict(record),
                        "alpha",
                        frozenset({str(record["kind"])}),
                        integrity=False,
                    ),
                )

        for source, field, value, code in (
            (request, "exact_action_digest", "0" * 63, "exact_action_digest_invalid"),
            (approved, "exact_action_digest", "A" * 64, "exact_action_digest_invalid"),
            (approved, "reason_code", "operator_denied", "reason_code_invalid"),
            (denied, "granted_scope", "repo:floati", "denial_grant_invalid"),
            (request, "caller_authority", True, "record_fields_invalid"),
        ):
            malformed = dict(source, **{field: value})
            with self.subTest(field=field, code=code), self.assertRaises(
                ProtocolRefusal
            ) as candidate:
                validate_record(
                    malformed,
                    "alpha",
                    frozenset({str(source["kind"])}),
                    integrity=False,
                )
            self.assertEqual(code, candidate.exception.code)

    def test_v1_approval_suspension_inventory_and_runtime_contracts_are_closed(self) -> None:
        """Catches private run kinds missing from inventory or accepting open record fields."""
        from floati.runtruth import LEGACY_RUN_KINDS, RUN_KINDS, SUSPENSION_KINDS
        from tests.test_approval_suspension import ApprovalSuspensionProjectionTests

        fixtures = ApprovalSuspensionProjectionTests()
        _, state = fixtures.started_attempt()
        suspension = fixtures.suspension_record(state)
        consumed = fixtures.consumption_record(state, suspension)
        self.assertEqual(
            frozenset({
                "attempt_suspended_for_approval",
                "approval_consumed_for_resume",
            }),
            SUSPENSION_KINDS,
        )
        self.assertTrue(SUSPENSION_KINDS <= RUN_KINDS)
        self.assertTrue(SUSPENSION_KINDS.isdisjoint(LEGACY_RUN_KINDS))
        for record in (suspension, consumed):
            with self.subTest(kind=record["kind"]):
                self.assertEqual(
                    record,
                    validate_record(
                        dict(record),
                        "alpha",
                        frozenset({record["kind"]}),
                        integrity=False,
                    ),
                )
                with self.assertRaises(ProtocolRefusal) as open_shape:
                    validate_record(
                        dict(record, caller_authority=True),
                        "alpha",
                        frozenset({record["kind"]}),
                        integrity=False,
                    )
                self.assertEqual("record_fields_invalid", open_shape.exception.code)

    def test_run_admission_binding_requires_version_one_for_candidate_and_replay(self) -> None:
        """Catches a v0 binding being accepted as durable admission authority."""
        valid = self.valid_run_admission()
        self.assertEqual(
            valid,
            validate_record(
                valid, "alpha", frozenset({"run_admission_bound"}), integrity=False
            ),
        )
        downgraded = dict(valid, schema_version=0)
        with self.assertRaises(ProtocolRefusal) as candidate:
            validate_record(
                downgraded, "alpha", frozenset({"run_admission_bound"}), integrity=False
            )
        self.assertEqual("schema_version_invalid", candidate.exception.code)
        with self.assertRaises(IntegrityFailure) as replay:
            validate_record(
                downgraded, "alpha", frozenset({"run_admission_bound"}), integrity=True
            )
        self.assertEqual("schema_version_invalid", replay.exception.code)

    def test_v1_harness_binding_requires_explicit_segment_lineage_without_widening_v0(self) -> None:
        """Catches v1 segment fields leaking into legacy bindings or losing root/transition shape rules."""
        root_segment_id = "seg-" + uuid7_hex()
        v1 = self.valid_harness_binding(
            1,
            [
                {
                    "ordinal": 1,
                    "harness_session_id": "worker-" + uuid7_hex(),
                    "segment_id": root_segment_id,
                    "segment_kind": "initial",
                },
                {
                    "ordinal": 2,
                    "harness_session_id": "worker-" + uuid7_hex(),
                    "segment_id": "seg-" + uuid7_hex(),
                    "segment_kind": "resume",
                    "predecessor_segment_id": root_segment_id,
                },
            ],
        )
        self.assertEqual(
            v1,
            validate_record(
                v1,
                "alpha",
                frozenset({"attempt_harness_session_bound"}),
                integrity=False,
            ),
        )

        legacy = self.valid_harness_binding(
            0,
            [{"ordinal": 1, "harness_session_id": "worker-" + uuid7_hex()}],
        )
        self.assertEqual(
            legacy,
            validate_record(
                legacy,
                "alpha",
                frozenset({"attempt_harness_session_bound"}),
                integrity=False,
            ),
        )

        v0_with_v1_shape = deepcopy(v1)
        v0_with_v1_shape["schema_version"] = 0
        with self.assertRaises(ProtocolRefusal) as widened_v0:
            validate_record(
                v0_with_v1_shape,
                "alpha",
                frozenset({"attempt_harness_session_bound"}),
                integrity=False,
            )
        self.assertEqual("harness_segments_invalid", widened_v0.exception.code)

        duplicate_segment_id = deepcopy(v1)
        duplicate_segment_id["harness_segments"][1]["segment_id"] = root_segment_id
        with self.assertRaises(ProtocolRefusal) as duplicate:
            validate_record(
                duplicate_segment_id,
                "alpha",
                frozenset({"attempt_harness_session_bound"}),
                integrity=False,
            )
        self.assertEqual("harness_segment_id_duplicate", duplicate.exception.code)

        for mutate in (
            lambda record: record["harness_segments"][0].update(
                predecessor_segment_id="seg-" + uuid7_hex()
            ),
            lambda record: record["harness_segments"][1].pop(
                "predecessor_segment_id"
            ),
        ):
            invalid = deepcopy(v1)
            mutate(invalid)
            with self.subTest(invalid=invalid["harness_segments"]), self.assertRaises(
                ProtocolRefusal
            ) as candidate:
                validate_record(
                    invalid,
                    "alpha",
                    frozenset({"attempt_harness_session_bound"}),
                    integrity=False,
                )
            self.assertEqual("harness_segments_invalid", candidate.exception.code)
            with self.assertRaises(IntegrityFailure) as replay:
                validate_record(
                    invalid,
                    "alpha",
                    frozenset({"attempt_harness_session_bound"}),
                    integrity=True,
                )
            self.assertEqual("harness_segments_invalid", replay.exception.code)

    def test_schema_version_one_remains_invalid_for_nonbinding_records(self) -> None:
        """Catches the v1 exception widening any pre-existing record kind."""
        record = self.valid()
        record["schema_version"] = 1
        with self.assertRaises(ProtocolRefusal) as candidate:
            validate_record(record, "alpha", frozenset({"registry_entry"}), integrity=False)
        self.assertEqual("schema_version_invalid", candidate.exception.code)

    def test_append_enforces_exact_v0_contract(self) -> None:
        for key, value, code in (
            ("schema_version", 1, "schema_version_invalid"),
            ("id", "registry-not-a-uuid7", "record_id_invalid"),
            ("timestamp", "yesterday", "timestamp_invalid"),
            ("state", "maybe", "state_invalid"),
            ("extra", True, "record_fields_invalid"),
        ):
            with self.subTest(key=key):
                record = self.valid()
                record[key] = value
                with self.assertRaises(ProtocolRefusal) as caught:
                    append_record(self.root, self.path, record, allowed_kinds={"registry_entry"})
                self.assertEqual(code, caught.exception.code)

    def test_registry_role_terminal_unsafe_values_refuse_as_candidate_and_replay(self) -> None:
        """Role controls and Bidi spellings are invalid in the shared durable validator."""
        for role in ("bad\x1brole", "bad\u202erole"):
            with self.subTest(role=repr(role)):
                record = self.valid()
                record["role"] = role
                with self.assertRaises(ProtocolRefusal) as candidate:
                    validate_record(
                        record,
                        "alpha",
                        frozenset({"registry_entry"}),
                        integrity=False,
                    )
                self.assertEqual("role_invalid", candidate.exception.code)
                with self.assertRaises(IntegrityFailure) as replay:
                    validate_record(
                        record,
                        "alpha",
                        frozenset({"registry_entry"}),
                        integrity=True,
                    )
                self.assertEqual("role_invalid", replay.exception.code)

    def test_ledger_kind_is_mandatory_and_exact(self) -> None:
        with self.assertRaises(ProtocolRefusal) as missing:
            append_record(self.root, self.path, self.valid())
        self.assertEqual("ledger_kind_required", missing.exception.code)
        with self.assertRaises(ProtocolRefusal) as wrong:
            append_record(self.root, self.path, self.valid(), allowed_kinds={"message_envelope"})
        self.assertEqual("record_kind_invalid", wrong.exception.code)

    def test_persisted_schema_drift_is_integrity_failure(self) -> None:
        record = self.valid()
        record.pop("schema_version")
        absolute = self.root.resolve_relative(self.path)
        absolute.parent.mkdir(parents=True)
        absolute.write_text(json.dumps(record, separators=(",", ":")) + "\n", encoding="utf-8")
        with self.assertRaises(IntegrityFailure) as caught:
            read_records(self.root, self.path, allowed_kinds={"registry_entry"})
        self.assertEqual("record_fields_invalid", caught.exception.code)

    def test_unhashable_enum_and_item_values_refuse_without_type_escape(self) -> None:
        registry = self.valid()
        registry["state"] = []
        with self.assertRaises(ProtocolRefusal) as state:
            append_record(self.root, self.path, registry, allowed_kinds={"registry_entry"})
        self.assertEqual("state_invalid", state.exception.code)

    def test_message_envelope_enforces_exact_git_notification_fields(self) -> None:
        first = self.valid_message()
        append_record(
            self.root, "events.jsonl", first,
            allowed_kinds={"message_envelope"},
        )
        second = self.valid_message()
        second.update({
            "repo": "floati",
            "sha": "b" * 64,
            "doc": "README.md",
            "note": "",
            "idempotency_key": "checkpoint-b",
        })
        append_record(
            self.root, "events.jsonl", second,
            allowed_kinds={"message_envelope"},
        )
        durable = read_records(self.root, "events.jsonl", allowed_kinds={"message_envelope"})
        self.assertEqual([first, second], durable)
        self.assertNotIn("body", durable[0])
        self.assertNotIn("wake_cause", durable[0])

    def test_message_envelope_accepts_optional_reply_binding_without_breaking_legacy_rows(self) -> None:
        legacy = self.valid_message()
        reply = self.valid_message()
        reply["idempotency_key"] = "checkpoint-reply"
        reply["reply_to"] = legacy["id"]
        append_record(self.root, "events.jsonl", legacy, allowed_kinds={"message_envelope"})
        append_record(self.root, "events.jsonl", reply, allowed_kinds={"message_envelope"})

        durable = read_records(self.root, "events.jsonl", allowed_kinds={"message_envelope"})
        self.assertNotIn("reply_to", durable[0])
        self.assertEqual(legacy["id"], durable[1]["reply_to"])

    def test_message_session_binding_and_retraction_use_only_architect_shapes(self) -> None:
        'reviewer TD3/TD4 keeps legacy binding literal and retraction vocabulary closed.'
        legacy = self.valid_message()
        legacy["attempt_binding"] = "absent_legacy"
        append_record(
            self.root,
            "events.jsonl",
            legacy,
            allowed_kinds={"message_envelope", "message_retracted"},
        )
        binding = {
            "attempt_id": "attempt-" + uuid7_hex(),
            "claim_id": "claim-" + uuid7_hex(),
            "lease_id": "lease-" + uuid7_hex(),
            "worker_session_id": "worker-" + uuid7_hex(),
        }
        bound = self.valid_message()
        bound["id"] = "msg-" + uuid7_hex()
        bound["idempotency_key"] = "session-bound"
        bound["worker_session_id"] = binding["worker_session_id"]
        bound["attempt_binding"] = binding
        append_record(
            self.root,
            "events.jsonl",
            bound,
            allowed_kinds={"message_envelope", "message_retracted"},
        )
        retraction = {
            "schema_version": 0,
            "id": "ret-" + uuid7_hex(),
            "tenant_id": "alpha",
            "timestamp": "2026-08-08T12:00:00.000Z",
            "kind": "message_retracted",
            "retracted_message_id": bound["id"],
            "worker_session_id": binding["worker_session_id"],
            "reason": "security_scrub",
            "author": public_ids.worker('alpha'),
        }
        append_record(
            self.root,
            "events.jsonl",
            retraction,
            allowed_kinds={"message_envelope", "message_retracted"},
        )
        partial = self.valid_message()
        partial["attempt_binding"] = {"attempt_id": binding["attempt_id"]}
        with self.assertRaises(ProtocolRefusal) as partial_error:
            append_record(
                self.root,
                "events.jsonl",
                partial,
                allowed_kinds={"message_envelope", "message_retracted"},
            )
        self.assertEqual("attempt_binding_invalid", partial_error.exception.code)
        mismatch = dict(bound)
        mismatch["id"] = "msg-" + uuid7_hex()
        mismatch["idempotency_key"] = "session-mismatch"
        mismatch["worker_session_id"] = "worker-" + uuid7_hex()
        with self.assertRaises(ProtocolRefusal) as mismatch_error:
            append_record(
                self.root,
                "events.jsonl",
                mismatch,
                allowed_kinds={"message_envelope", "message_retracted"},
            )
        self.assertEqual(
            "attempt_binding_session_mismatch", mismatch_error.exception.code
        )
        bad_reason = dict(retraction)
        bad_reason["id"] = "ret-" + uuid7_hex()
        bad_reason["reason"] = "withdrawn"
        with self.assertRaises(ProtocolRefusal) as reason_error:
            append_record(
                self.root,
                "events.jsonl",
                bad_reason,
                allowed_kinds={"message_envelope", "message_retracted"},
            )
        self.assertEqual("reason_invalid", reason_error.exception.code)

    def test_message_envelope_refuses_malformed_git_notification_values(self) -> None:
        cases = (
            ("repo", "", "repo_invalid"),
            ("repo", "owner/repo/extra", "repo_invalid"),
            ("repo", "x" * 129, "repo_invalid"),
            ("sha", "a" * 39, "sha_invalid"),
            ("sha", "A" * 40, "sha_invalid"),
            ("sha", "g" * 64, "sha_invalid"),
            ("doc", "", "doc_invalid"),
            ("doc", "/README.md", "doc_invalid"),
            ("doc", "docs//README.md", "doc_invalid"),
            ("doc", "docs/./README.md", "doc_invalid"),
            ("doc", "docs/../README.md", "doc_invalid"),
            ("doc", "x" * 1025, "doc_invalid"),
            ("note", None, "note_invalid"),
            ("note", "x" * 1025, "note_invalid"),
        )
        for field, value, code in cases:
            with self.subTest(field=field, value=value):
                message = self.valid_message()
                message[field] = value
                with self.assertRaises(ProtocolRefusal) as caught:
                    append_record(
                        self.root, "events.jsonl", message,
                        allowed_kinds={"message_envelope"},
                    )
                self.assertEqual(code, caught.exception.code)

    def test_note_refusal_names_a_newline_not_length(self) -> None:
        """RED-first: a short note with \\n used to claim the 1024 cap."""
        message = self.valid_message()
        message["note"] = "short\nline"
        with self.assertRaises(ProtocolRefusal) as caught:
            append_record(
                self.root, "events.jsonl", message,
                allowed_kinds={"message_envelope"},
            )
        self.assertEqual("note_invalid", caught.exception.code)
        self.assertIn("control", caught.exception.detail)
        self.assertIn("offset 5", caught.exception.detail)
        self.assertNotIn("1024", caught.exception.detail)
        self.assertIsInstance(caught.exception.remedy, str)
        self.assertTrue(caught.exception.remedy.strip())

    def test_note_length_refusal_names_the_measured_length(self) -> None:
        message = self.valid_message()
        message["note"] = "x" * 1025
        with self.assertRaises(ProtocolRefusal) as caught:
            append_record(
                self.root, "events.jsonl", message,
                allowed_kinds={"message_envelope"},
            )
        self.assertEqual("note_invalid", caught.exception.code)
        self.assertIn("1025", caught.exception.detail)
        self.assertIn("1024", caught.exception.detail)
        self.assertNotIn("control", caught.exception.detail)

    def test_title_refusal_names_a_newline_not_bounds_prose(self) -> None:
        work = {
            "schema_version": 0,
            "id": "work-" + uuid7_hex(),
            "tenant_id": "alpha",
            "timestamp": "2026-07-31T12:00:00.000Z",
            "kind": "work_item",
            "title": "line\nbreak",
            "owner": "worker-1",
            "artifact_bindings": [],
        }
        with self.assertRaises(ProtocolRefusal) as caught:
            append_record(
                self.root, "work/items.jsonl", work,
                allowed_kinds={"work_item"},
            )
        self.assertEqual("title_invalid", caught.exception.code)
        self.assertIn("control", caught.exception.detail)
        self.assertNotIn("v0 string bounds", caught.exception.detail)

    def test_doc_refusal_names_a_newline_not_containment(self) -> None:
        message = self.valid_message()
        message["doc"] = "docs/a\nb.md"
        with self.assertRaises(ProtocolRefusal) as caught:
            append_record(
                self.root, "events.jsonl", message,
                allowed_kinds={"message_envelope"},
            )
        self.assertEqual("doc_invalid", caught.exception.code)
        self.assertIn("control", caught.exception.detail)
        self.assertNotIn("contained", caught.exception.detail)

    def test_protocol_refusal_evidence_surfaces_note_remedy(self) -> None:
        from floati.cli import _protocol_refusal_evidence

        message = self.valid_message()
        message["note"] = "short\nline"
        with self.assertRaises(ProtocolRefusal) as caught:
            append_record(
                self.root, "events.jsonl", message,
                allowed_kinds={"message_envelope"},
            )
        evidence = _protocol_refusal_evidence(caught.exception)
        self.assertEqual("note_invalid", evidence["code"])
        self.assertEqual(caught.exception.remedy, evidence["remedy"])
        self.assertNotEqual({"kind": "none", "why": "no action was named for this refusal"}, evidence["remedy"])

    def test_work_item_refuses_an_arbitrary_workspace_mapping(self) -> None:
        work = {
            "schema_version": 0,
            "id": "work-" + uuid7_hex(),
            "tenant_id": "alpha",
            "timestamp": "2026-07-31T12:00:00.000Z",
            "kind": "work_item",
            "title": "escape ruled workspace",
            "owner": "worker-1",
            "artifact_bindings": [],
            "workspace": "\x2ftmp/inferred",
        }

        with self.assertRaises(ProtocolRefusal) as caught:
            append_record(
                self.root,
                "work/items.jsonl",
                work,
                allowed_kinds={"work_item"},
            )

        self.assertEqual("workspace_invalid", caught.exception.code)

    def test_acceptance_receipt_refuses_semantic_score_and_unknown_fields(self) -> None:
        """Catches a provenance receipt that accepts an LLM score as acceptance authority."""
        receipt = {
            "schema_version": 0, "id": "acceptance-receipt-" + uuid7_hex(),
            "tenant_id": "alpha", "timestamp": "2026-08-08T12:00:00.000Z",
            "kind": "acceptance_receipt", "run_id": "run-" + uuid7_hex(),
            "item_id": "work-" + uuid7_hex(), "attempt_id": "attempt-" + uuid7_hex(),
            "contract_digest": "a" * 64, "check_ids": ["tests.unit"],
            "reviewer": "reviewer-a", "evidence_bindings": [], "deviations": [],
            "result": "accepted", "semantic_score": 0.99,
        }
        with self.assertRaises(ProtocolRefusal) as caught:
            append_record(self.root, "runs/events.jsonl", receipt, allowed_kinds={"acceptance_receipt"})
        self.assertEqual("record_fields_invalid", caught.exception.code)

    def test_task_contract_nested_unhashable_values_refuse_with_protocol_errors(self) -> None:
        """Catches raw TypeError from nested collections in task-contract, amendment, or receipt validation."""
        base = {
            "schema_version": 0, "id": "task-contract-" + uuid7_hex(), "tenant_id": "alpha",
            "timestamp": "2026-08-08T12:00:00.000Z", "kind": "task_contract",
            "run_id": "run-" + uuid7_hex(), "item_id": "work-" + uuid7_hex(), "objective": "bounded",
            "non_goals": [[]], "areas_to_avoid": [{"path": "floati/graph.py", "region": "all"}],
            "input_hashes": {"brief": "a" * 64}, "acceptance_checks": {"tests.unit": "python3 -m unittest"},
            "constraints": {"network": "dark"}, "risk_class": "high",
            "retry_policy": {"max_attempts": 1, "backoff": {"base_delay_ms": 0, "cap_delay_ms": 0, "strategy": "fixed"}},
            "dependencies": [], "contract_digest": "a" * 64,
        }
        with self.assertRaises(ProtocolRefusal) as caught:
            append_record(self.root, "runs/events.jsonl", base, allowed_kinds={"task_contract"})
        self.assertEqual("non_goals_invalid", caught.exception.code)

    def test_decision_record_is_strict_and_refuses_unruled_authority_sources_or_digest_drift(self) -> None:
        """Catches a generic validator that reopens the ruled decision binding before register lookup."""
        record = self.valid_decision()
        try:
            append_record(
                self.root,
                "repositories/Owner/Repo/decisions.jsonl",
                record,
                allowed_kinds={"decision_record"},
            )
        except ProtocolRefusal as exc:
            self.fail("valid provisional decision record refused: " + exc.code)
        self.assertEqual(
            [record],
            read_records(
                self.root,
                "repositories/Owner/Repo/decisions.jsonl",
                allowed_kinds={"decision_record"},
            ),
        )
        cases = (
            ("repository", ".", "repository_invalid"),
            ("decision_id", "decision-not-a-uuid7", "decision_id_invalid"),
            ("status", "superseded", "decision_status_invalid"),
            ("author_authority", "reviewer", "author_authority_invalid"),
            ("source_artifact_ids", ["artifact-unruled"], "source_artifact_id_invalid"),
            ("scope", "repository", "decision_scope_invalid"),
            ("decision_digest", "0" * 64, "decision_digest_invalid"),
            ("decided_by", [], "decided_by_invalid"),
            ("extra", True, "record_fields_invalid"),
        )
        for field, value, code in cases:
            with self.subTest(field=field):
                malformed = self.valid_decision()
                malformed[field] = value
                with self.assertRaises(ProtocolRefusal) as caught:
                    append_record(
                        self.root,
                        "repositories/Owner/Repo/decisions.jsonl",
                        malformed,
                        allowed_kinds={"decision_record"},
                    )
                self.assertEqual(code, caught.exception.code)

        worker_terminal = self.valid_decision()
        worker_terminal["status"] = "accepted"
        worker_terminal["decision_digest"] = decision_digest(worker_terminal)
        with self.assertRaises(ProtocolRefusal) as caught:
            append_record(
                self.root,
                "repositories/Owner/Repo/decisions.jsonl",
                worker_terminal,
                allowed_kinds={"decision_record"},
            )
        self.assertEqual("decision_terminal_authority_invalid", caught.exception.code)

    def test_task_contract_repository_is_optional_immutable_record_binding(self) -> None:
        """Catches a contract repository field that is missing, malformed, or amendable as governed content."""
        contract = {
            "schema_version": 0, "id": "task-contract-" + uuid7_hex(), "tenant_id": "alpha",
            "timestamp": "2026-08-08T12:00:00.000Z", "kind": "task_contract",
            "run_id": "run-" + uuid7_hex(), "item_id": "work-" + uuid7_hex(), "objective": "bounded",
            "non_goals": ["no inference"], "areas_to_avoid": [{"path": "floati/graph.py", "region": "all"}],
            "input_hashes": {"brief": "a" * 64}, "acceptance_checks": {"tests.unit": "python3 -m unittest"},
            "constraints": {"network": "dark"}, "risk_class": "high",
            "retry_policy": {"max_attempts": 1, "backoff": {"base_delay_ms": 0, "cap_delay_ms": 0, "strategy": "fixed"}},
            "dependencies": [], "contract_digest": "a" * 64, "repository": "Owner/Repo",
        }
        append_record(self.root, "runs/events.jsonl", contract, allowed_kinds={"task_contract"})
        legacy = dict(contract)
        legacy["id"] = "task-contract-" + uuid7_hex()
        legacy.pop("repository")
        append_record(self.root, "runs/legacy-contracts.jsonl", legacy, allowed_kinds={"task_contract"})

        malformed = dict(contract)
        malformed["id"] = "task-contract-" + uuid7_hex()
        malformed["repository"] = "../other"
        with self.assertRaises(ProtocolRefusal) as bad_repository:
            append_record(self.root, "runs/malformed-contracts.jsonl", malformed, allowed_kinds={"task_contract"})
        self.assertEqual("repository_invalid", bad_repository.exception.code)

        amendment = {
            "schema_version": 0, "id": "plan-amendment-" + uuid7_hex(), "tenant_id": "alpha",
            "timestamp": "2026-08-08T12:00:00.000Z", "kind": "plan_amendment",
            "run_id": contract["run_id"], "item_id": contract["item_id"], "task_contract_id": contract["id"],
            "previous_digest": "a" * 64, "replacement_fields": {"repository": "Other/Repo"},
            "contract_digest": "b" * 64,
        }
        with self.assertRaises(ProtocolRefusal) as amendment_refusal:
            append_record(self.root, "runs/events.jsonl", amendment, allowed_kinds={"plan_amendment"})
        self.assertEqual("replacement_fields_invalid", amendment_refusal.exception.code)

    def test_v1_spawn_group_contracts(self) -> None:
        """Catches runtime version/field drift across every Task 1 spawn contract."""
        from tests.test_spawn_groups import SpawnGroupFixtures

        fixture = SpawnGroupFixtures()
        started = fixture.started_parent()
        group = fixture.group()
        amendment = fixture.amendment(group)
        rows = [
            next(row for row in started if row["kind"] == "run_spawn_admission_enabled"),
            next(row for row in started if row["kind"] == "attempt_spawn_policy_bound"),
            group,
            amendment,
            fixture.admitted(group, amendment),
        ]
        for row in rows:
            with self.subTest(kind=row["kind"]):
                allowed = frozenset({str(row["kind"])})
                self.assertEqual(row, validate_record(row, "alpha", allowed, integrity=False))
                with self.assertRaises(ProtocolRefusal):
                    validate_record(dict(row, schema_version=0), "alpha", allowed, integrity=False)
                with self.assertRaises(ProtocolRefusal):
                    validate_record(dict(row, unexpected=True), "alpha", allowed, integrity=False)


if __name__ == "__main__":
    unittest.main()

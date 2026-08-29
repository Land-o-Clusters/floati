from __future__ import annotations

import copy
import hashlib
import json
import unittest
from pathlib import Path
from typing import Any, Dict

from tests.schema_validation import SchemaValidationError, validate_json_schema


ROOT = Path(__file__).resolve().parents[1]
C7 = ROOT / "bundle" / "c7.1"
INDEX_PATH = C7 / "bundle-index.json"
CATALOG_PATH = C7 / "schema-catalog.json"
INDEX_SCHEMA_PATH = C7 / "schemas" / "c7-read-bundle.schema.json"
PROJECTION_SCHEMA_PATH = C7 / "schemas" / "canonical-projection.schema.json"
PROJECTION_SCHEMA_ID = "https://landoclusters.com/floati/schemas/c7.1/canonical-projection.schema.json"
PROJECTION_POINTERS = {
    "/families/logical_outcomes/value/<run_id:item_id>/outcome",
    "/families/logical_outcomes/value/<run_id:item_id>/raw_fallback",
    "/families/run_outcomes/value/<run_id>/outcome",
    "/families/run_outcomes/value/<run_id>/raw_fallback",
}


# These are the Fable-ratified Q1 pointers, copied as literals from the
# authoritative Item 11 brief and architecture receipt rather than derived
# from the bundle under test.  A catalog omission or a schema-file retargeting
# must therefore make this test fail.
EXPECTED_SOURCES = {
    "https://landoclusters.com/floati/schemas/v0/run-created-record.schema.json": (
        "schemas/v0/run-created-record.schema.json",
        {
            "/run_id", "/plan_digest", "/item_ids", "/dependency_edges",
            "/dependency_edges/n/source", "/dependency_edges/n/target",
            "/dependency_edges/n/requires", "/dependency_edges/n/failure_policy",
        },
    ),
    "https://landoclusters.com/floati/schemas/v0/run-policy-bound-record.schema.json": (
        "schemas/v0/run-policy-bound-record.schema.json", {"/run_id", "/policy_digest"}
    ),
    "https://landoclusters.com/floati/schemas/v0/run-worker-pool-bound-record.schema.json": (
        "schemas/v0/run-worker-pool-bound-record.schema.json", {"/run_id", "/worker_ids"}
    ),
    "https://landoclusters.com/floati/schemas/v0/run-dispatch-decision-record.schema.json": (
        "schemas/v0/run-dispatch-decision-record.schema.json",
        {
            "/run_id", "/item_id", "/attempt_id", "/eligible_workers",
            "/chosen_worker", "/capability_digest", "/reason_code",
            "/policy_digest", "/routing_rank", "/scheduler_epoch",
        },
    ),
    "https://landoclusters.com/floati/schemas/v0/run-result-produced-record.schema.json": (
        "schemas/v0/run-result-produced-record.schema.json",
        {"/run_id", "/item_id", "/attempt_id", "/dispatch_decision_id", "/worker_receipt_ids"},
    ),
    "https://landoclusters.com/floati/schemas/v0/run-result-verified-record.schema.json": (
        "schemas/v0/run-result-verified-record.schema.json",
        {"/run_id", "/item_id", "/attempt_id", "/result_produced_id", "/worker_receipt_ids"},
    ),
    "https://landoclusters.com/floati/schemas/v0/run-result-accepted-record.schema.json": (
        "schemas/v0/run-result-accepted-record.schema.json",
        {
            "/run_id", "/item_id", "/attempt_id", "/predecessor_result_id",
            "/acceptance_mode", "/acceptance_receipt_id", "/worker_receipt_ids",
        },
    ),
    "https://landoclusters.com/floati/schemas/v0/run-terminal-record.schema.json": (
        "schemas/v0/run-terminal-record.schema.json", {"/run_id", "/outcome"}
    ),
    "https://landoclusters.com/floati/schemas/v0/task-contract-record.schema.json": (
        "schemas/v0/task-contract-record.schema.json",
        {
            "/run_id", "/item_id", "/objective", "/non_goals", "/areas_to_avoid",
            "/input_hashes", "/acceptance_checks", "/constraints", "/risk_class",
            "/retry_policy", "/dependencies", "/contract_digest",
        },
    ),
    "https://landoclusters.com/floati/schemas/v0/plan-amendment-record.schema.json": (
        "schemas/v0/plan-amendment-record.schema.json",
        {
            "/run_id", "/item_id", "/task_contract_id", "/previous_digest",
            "/replacement_fields", "/contract_digest",
        },
    ),
    "https://landoclusters.com/floati/schemas/v0/acceptance-receipt-record.schema.json": (
        "schemas/v0/acceptance-receipt-record.schema.json",
        {
            "/run_id", "/item_id", "/attempt_id", "/contract_digest", "/check_ids",
            "/reviewer", "/evidence_bindings", "/deviations", "/result",
        },
    ),
    "https://landoclusters.com/floati/schemas/v0/attempt-opened-record.schema.json": (
        "schemas/v0/attempt-opened-record.schema.json",
        {
            "/run_id", "/item_id", "/attempt_id", "/ordinal", "/scheduler_epoch",
            "/fence_token", "/max_attempts", "/backoff",
        },
    ),
    "https://landoclusters.com/floati/schemas/v0/attempt-started-record.schema.json": (
        "schemas/v0/attempt-started-record.schema.json",
        {
            "/run_id", "/item_id", "/attempt_id", "/ordinal", "/attempt_opened_id",
            "/dispatch_decision_id", "/fence_token",
        },
    ),
    "https://landoclusters.com/floati/schemas/v0/attempt-terminal-record.schema.json": (
        "schemas/v0/attempt-terminal-record.schema.json",
        {
            "/run_id", "/item_id", "/attempt_id", "/ordinal", "/attempt_started_id",
            "/fence_token", "/terminal_state", "/policy_class", "/reason_code",
            "/effect_safety", "/retry_disposition", "/retry_record_id", "/next_attempt_id",
            "/next_ordinal", "/retry_delay_ms", "/next_scheduler_epoch", "/next_fence_token",
        },
    ),
    "https://landoclusters.com/floati/schemas/v0/retry-scheduled-record.schema.json": (
        "schemas/v0/retry-scheduled-record.schema.json",
        {
            "/run_id", "/item_id", "/previous_attempt_id", "/attempt_terminal_id",
            "/next_attempt_id", "/next_ordinal", "/delay_ms", "/scheduler_epoch",
            "/next_fence_token",
        },
    ),
    "https://landoclusters.com/floati/schemas/v0/retry-exhausted-record.schema.json": (
        "schemas/v0/retry-exhausted-record.schema.json",
        {
            "/run_id", "/item_id", "/attempt_id", "/ordinal", "/attempt_terminal_id",
            "/max_attempts", "/reason_code",
        },
    ),
    "https://landoclusters.com/floati/schemas/v0/cancel-requested-record.schema.json": (
        "schemas/v0/cancel-requested-record.schema.json",
        {"/run_id", "/scope", "/item_id", "/requested_by"},
    ),
    "https://landoclusters.com/floati/schemas/v0/cancel-scope-resolved-record.schema.json": (
        "schemas/v0/cancel-scope-resolved-record.schema.json",
        {"/run_id", "/cancel_request_id", "/scope", "/item_id", "/item_ids", "/attempt_ids"},
    ),
    "https://landoclusters.com/floati/schemas/v0/cancel-observed-record.schema.json": (
        "schemas/v0/cancel-observed-record.schema.json",
        {
            "/run_id", "/cancel_scope_resolved_id", "/item_id", "/attempt_id",
            "/fence_token", "/adapter", "/cancel_mode",
        },
    ),
    "https://landoclusters.com/floati/schemas/v0/cancel-signal-sent-record.schema.json": (
        "schemas/v0/cancel-signal-sent-record.schema.json",
        {
            "/run_id", "/cancel_scope_resolved_id", "/item_id", "/attempt_id",
            "/fence_token", "/adapter", "/cancel_mode",
        },
    ),
    "https://landoclusters.com/floati/schemas/v0/cancel-terminal-record.schema.json": (
        "schemas/v0/cancel-terminal-record.schema.json",
        {
            "/run_id", "/cancel_scope_resolved_id", "/item_id", "/attempt_id",
            "/fence_token", "/adapter", "/cancel_mode",
        },
    ),
    "https://landoclusters.com/floati/schemas/v0/cancel-unconfirmed-record.schema.json": (
        "schemas/v0/cancel-unconfirmed-record.schema.json",
        {
            "/run_id", "/cancel_scope_resolved_id", "/item_id", "/attempt_id",
            "/fence_token", "/adapter", "/cancel_mode",
        },
    ),
    "https://landoclusters.com/floati/schemas/v0/stale-attempt-evidence-record.schema.json": (
        "schemas/v0/stale-attempt-evidence-record.schema.json",
        {
            "/run_id", "/item_id", "/attempt_id", "/worker_receipt_ids",
            "/presented_fence_token", "/current_attempt_id", "/current_fence_token",
        },
    ),
    "https://landoclusters.com/floati/schemas/v0/stale-evidence-adopted-record.schema.json": (
        "schemas/v0/stale-evidence-adopted-record.schema.json",
        {
            "/run_id", "/item_id", "/stale_evidence_id", "/current_attempt_id",
            "/current_fence_token", "/operator_id", "/authority_subject", "/authority_epoch",
            "/capability_record_id",
        },
    ),
    "https://landoclusters.com/floati/schemas/v0/attempt-harness-session-bound-record.schema.json": (
        "schemas/v0/attempt-harness-session-bound-record.schema.json",
        {
            "/run_id", "/item_id", "/attempt_id", "/fence_token", "/claim_id", "/lease_id",
            "/worker_session_id", "/harness_segments", "/harness_segments/n/ordinal",
            "/harness_segments/n/harness_session_id",
        },
    ),
    "https://landoclusters.com/floati/schemas/v0/supervisor-orphaned-record.schema.json": (
        "schemas/v0/supervisor-orphaned-record.schema.json",
        {
            "/run_id", "/item_id", "/attempt_id", "/claim_id", "/lease_id",
            "/worker_session_id", "/supervisor_id", "/orphan_class", "/authority_subject",
            "/authority_epoch", "/capability_record_id",
        },
    ),
    "https://landoclusters.com/floati/schemas/v0/decision-record.schema.json": (
        "schemas/v0/decision-record.schema.json",
        {
            "/repository", "/decision_id", "/scope", "/statement", "/status",
            "/author_authority", "/source_artifact_ids", "/task_contract_id", "/decided_by",
            "/supersedes", "/decision_digest",
        },
    ),
    "https://landoclusters.com/floati/schemas/v0/work-item-record.schema.json": (
        "schemas/v0/work-item-record.schema.json",
        {"/id", "/title", "/owner", "/artifact_bindings"},
    ),
    "https://landoclusters.com/floati/schemas/v0/registry-entry.schema.json": (
        "schemas/v0/registry-entry.schema.json",
        {"/id", "/node_id", "/role", "/state"},
    ),
    "https://landoclusters.com/floati/schemas/v0/worker-receipt-record.schema.json": (
        "schemas/v0/worker-receipt-record.schema.json",
        {
            "/id", "/session_id", "/work_item_id", "/node_id", "/adapter",
            "/transition", "/outcome_code", "/artifact_bindings",
        },
    ),
}
C7_1_EXPECTED_CATALOG_SOURCES_BY_FILE = {
    source_file: (source_id, 0, expected_pointers)
    for source_id, (source_file, expected_pointers) in EXPECTED_SOURCES.items()
}
C7_1_EXPECTED_CATALOG_SOURCES_BY_FILE[
    "schemas/canonical-projection.schema.json"
] = (
    PROJECTION_SCHEMA_ID,
    "c7.1-candidate",
    PROJECTION_POINTERS,
)


def load_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def catalog_sources(catalog: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    flattened: Dict[str, Dict[str, Any]] = {}
    for entry in catalog["entries"]:
        for source in entry.get("sources", []):
            source_id = source["id"]
            accumulated = flattened.setdefault(
                source_id,
                {
                    "file": source["file"],
                    "version": source["version"],
                    "sha256": source["sha256"],
                    "pointers": set(),
                },
            )
            if accumulated["file"] != source["file"]:
                raise AssertionError(f"catalog maps {source_id} to more than one file")
            if accumulated["version"] != source["version"]:
                raise AssertionError(f"catalog maps {source_id} to more than one version")
            if accumulated["sha256"] != source["sha256"]:
                raise AssertionError(f"catalog maps {source_id} to more than one digest")
            accumulated["pointers"].update(source["pointers"])
    return flattened


def catalog_sources_by_file(catalog: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    by_file: Dict[str, Dict[str, Any]] = {}
    for source_id, declared in catalog_sources(catalog).items():
        source_file = declared["file"]
        if source_file in by_file:
            raise AssertionError(
                f"catalog maps {source_file} from more than one identity"
            )
        by_file[source_file] = {"id": source_id, **declared}
    return by_file


def frame_range() -> Dict[str, object]:
    return {"ledger": "raw/runs/events.jsonl", "first_frame": 1, "last_frame": 1}


def absent(reason: str) -> Dict[str, object]:
    return {"state": "absent", "reason": reason, "raw_fallback": frame_range()}


def auxiliary_absent(ledger: str) -> Dict[str, object]:
    return {
        "ledger": ledger,
        "raw_source_digest": "0" * 64,
        "source_frames": [],
        "state": "absent",
        "reason": "source_absent",
        "raw_fallback": {"ledger": ledger, "first_frame": 0, "last_frame": 0},
    }


def projection_fixture() -> Dict[str, object]:
    return {
        "schema_version": "c7.1-candidate",
        "kind": "c7_canonical_projection",
        "tenant_id": "alpha",
        "repository": "owner/repo",
        "raw_source": "raw/runs/events.jsonl",
        "raw_source_digest": "0" * 64,
        "source_frames": [{"ordinal": 1, "record_id": "run-created-example", "kind": "run_created"}],
        "families": {
            "runs": absent("no_run_frames"),
            "work_items": absent("no_run_items"),
            "attempts": absent("no_attempt_frames"),
            "claims": absent("no_opaque_claim_references"),
            "leases": absent("no_opaque_lease_references"),
            "retries": absent("no_retry_frames"),
            "cancellations": absent("no_cancellation_frames"),
            "result_phases": absent("no_result_frames"),
            "logical_outcomes": absent("no_logical_outcomes"),
            "run_outcomes": absent("no_run_outcomes"),
            "task_contracts": absent("no_task_contract_frames"),
            "session_bindings": {
                **absent("no_session_binding_frames"),
                "segment_relation_vocabulary": ["resume", "fork", "handoff"],
            },
            "supervisor_orphans": absent("no_supervisor_orphan_frames"),
            "decisions": absent("decision_register_absent"),
        },
        "auxiliary_sources": {
            "worker_receipts": auxiliary_absent("raw/receipts/workers.jsonl"),
            "registry": auxiliary_absent("raw/registry/entries.jsonl"),
            "decisions": auxiliary_absent("raw/repositories/owner/repo/decisions.jsonl"),
            "work_items": auxiliary_absent("raw/work/items.jsonl"),
        },
        "cross_ledger_rule": "no_timestamp_merge",
        "semantic_digest": "1" * 64,
        "self_digest": "2" * 64,
    }


class C7StaticContractTests(unittest.TestCase):
    def test_index_is_self_describing_and_fail_closed(self) -> None:
        """Catches an index that lets an unknown C7 version be selected or silently mutates v0."""
        index = load_json(INDEX_PATH)
        validate_json_schema(index, INDEX_SCHEMA_PATH)
        self.assertEqual("c7.1-candidate", index["schema_version"])
        self.assertEqual("excluded-c7.1", index["approvals"])
        self.assertEqual(
            {"highest_understood": True, "unknown": "fail_closed"},
            index["reader_upgrade"],
        )
        self.assertEqual(
            {
                "id": "https://landoclusters.com/floati/schemas/c7.1/c7-read-bundle.schema.json",
                "version": "c7.1-candidate",
                "file": "schemas/c7-read-bundle.schema.json",
            },
            index["index_schema"],
        )
        self.assertEqual(
            {"path": "docs/CONFLUENCE-v0.md", "version": 0, "mutation": "forbidden"},
            index["predecessor"],
        )
        self.assertTrue((ROOT / index["predecessor"]["path"]).is_file())
        self.assertIn(
            {
                "name": "work_item_context",
                "ledger": "raw/work/items.jsonl",
                "projection": "raw_fallback_only",
                "causal_order": "physical_frame_independent",
            },
            index["families"],
        )

        unknown = copy.deepcopy(index)
        unknown["reader_upgrade"]["unknown"] = "best_effort"
        with self.assertRaises(SchemaValidationError):
            validate_json_schema(unknown, INDEX_SCHEMA_PATH)

    def test_catalog_exhaustively_maps_ruled_q1_source_authority(self) -> None:
        """Catches a Q1 catalog that omits a ruled ledger family, pointer, schema version, or source file."""
        catalog = load_json(CATALOG_PATH)
        self.assertEqual("c7.1-candidate", catalog["schema_version"])
        for coordinate, expected in (
            ("id", PROJECTION_SCHEMA_ID),
            ("version", "c7.1-candidate"),
            ("file", "schemas/canonical-projection.schema.json"),
        ):
            with self.subTest(schema="projection_schema", coordinate=coordinate):
                self.assertEqual(expected, catalog["projection_schema"][coordinate])
        self.assertEqual(
            hashlib.sha256(PROJECTION_SCHEMA_PATH.read_bytes()).hexdigest(),
            catalog["projection_schema"]["sha256"],
        )
        sources_by_file = catalog_sources_by_file(catalog)
        self.assertEqual(
            set(C7_1_EXPECTED_CATALOG_SOURCES_BY_FILE),
            set(sources_by_file),
        )
        for source_file, (source_id, version, expected_pointers) in (
            C7_1_EXPECTED_CATALOG_SOURCES_BY_FILE.items()
        ):
            declared = sources_by_file[source_file]
            for coordinate, expected in (
                ("id", source_id),
                ("version", version),
                ("pointers", expected_pointers),
            ):
                with self.subTest(schema=source_file, coordinate=coordinate):
                    self.assertEqual(expected, declared[coordinate])
            source_path = (
                C7 / source_file
                if version == "c7.1-candidate"
                else ROOT / source_file
            )
            source = load_json(source_path)
            with self.subTest(schema=source_file, coordinate="schema_id"):
                self.assertEqual(source_id, source["$id"])
            with self.subTest(schema=source_file, coordinate="schema_version"):
                self.assertEqual(
                    version,
                    source["properties"]["schema_version"]["const"],
                )
            with self.subTest(schema=source_file, coordinate="sha256"):
                self.assertEqual(
                    hashlib.sha256(source_path.read_bytes()).hexdigest(),
                    declared["sha256"],
                )

    def test_catalog_keeps_approval_excluded_and_decisions_read_only(self) -> None:
        """Catches an approval inference or a decision register that becomes a writable consolidation surface."""
        catalog = load_json(CATALOG_PATH)
        entries = {entry["family"]: entry for entry in catalog["entries"]}
        self.assertEqual("excluded-c7.1", entries["approval"]["representation"])
        self.assertEqual([], entries["approval"]["sources"])
        self.assertEqual("raw_read_only_no_consolidation", entries["decision_record"]["exposure"])
        self.assertEqual(
            "raw/repositories/<repository-coordinate>/decisions.jsonl",
            entries["decision_record"]["ledger_template"],
        )

    def test_projection_schema_requires_normative_digest_and_typed_state_shapes(self) -> None:
        """Catches null-as-unknown, unbounded family errors, or a projection that loses either digest domain."""
        valid = projection_fixture()
        validate_json_schema(valid, PROJECTION_SCHEMA_PATH)

        no_raw_fallback = projection_fixture()
        del no_raw_fallback["families"]["runs"]["raw_fallback"]
        with self.assertRaises(SchemaValidationError):
            validate_json_schema(no_raw_fallback, PROJECTION_SCHEMA_PATH)

        null_unknown = projection_fixture()
        null_unknown["families"]["runs"]["state"] = None
        with self.assertRaises(SchemaValidationError):
            validate_json_schema(null_unknown, PROJECTION_SCHEMA_PATH)

        no_error_range = projection_fixture()
        no_error_range["families"]["runs"] = {
            "state": {"kind": "error", "code": "bad_frame"},
            "raw_fallback": frame_range(),
        }
        with self.assertRaises(SchemaValidationError):
            validate_json_schema(no_error_range, PROJECTION_SCHEMA_PATH)

        no_semantic_digest = projection_fixture()
        del no_semantic_digest["semantic_digest"]
        with self.assertRaises(SchemaValidationError):
            validate_json_schema(no_semantic_digest, PROJECTION_SCHEMA_PATH)

    def test_projection_schema_closes_segments_conflicts_and_orphan_evidence(self) -> None:
        """Catches a Q5 winner-selection escape hatch or a Q6 capability-shaped orphan projection."""
        valid = projection_fixture()
        valid["families"]["session_bindings"] = {
            "state": "present",
            "segment_relation_vocabulary": ["resume", "fork", "handoff"],
            "value": {
                "attempt-example": {
                    "binding_record_id": "attempt-harness-session-bound-example",
                    "frame": 1,
                    "run_id": "run-example",
                    "item_id": "work-example",
                    "claim_id": "claim-example",
                    "lease_id": "lease-example",
                    "worker_session_id": "worker-example",
                    "segments": [{
                        "source_ref": {
                            "binding_record_id": "attempt-harness-session-bound-example",
                            "ordinal": 1,
                        },
                        "harness_session_id": "harness-example",
                        "segment_kind": absent("not_durable_c7_1"),
                        "predecessor_segment_id": absent("not_durable_c7_1"),
                    }],
                }
            },
        }
        valid["families"]["supervisor_orphans"] = {
            "state": "present",
            "value": {
                "supervisor-orphaned-example": {
                    "record_id": "supervisor-orphaned-example",
                    "frame": 1,
                    "run_id": "run-example",
                    "item_id": "work-example",
                    "attempt_id": "attempt-example",
                    "claim_id": "claim-example",
                    "lease_id": "lease-example",
                    "worker_session_id": "worker-example",
                    "orphan_class": "owner_loss",
                    "supervisor_id": "floati-supervisor",
                    "registration_lineage": {
                        "ledger": "raw/registry/entries.jsonl",
                        "frame": 1,
                        "record_id": "registry-entry-example",
                        "node_id": "floati-supervisor",
                        "role": "supervisor",
                        "state": "active",
                    },
                }
            },
        }
        validate_json_schema(valid, PROJECTION_SCHEMA_PATH)

        wrong_vocabulary = copy.deepcopy(valid)
        wrong_vocabulary["families"]["session_bindings"]["segment_relation_vocabulary"] = [
            "resume", "fork", "guess"
        ]
        with self.assertRaises(SchemaValidationError):
            validate_json_schema(wrong_vocabulary, PROJECTION_SCHEMA_PATH)

        winnerless_conflict = copy.deepcopy(valid)
        winnerless_conflict["families"]["session_bindings"]["value"] = {
            "attempt-example": {
                "state": {
                    "kind": "error",
                    "code": "conflicting_binding",
                    "offending_frame_range": frame_range(),
                },
                "raw_fallback": frame_range(),
            }
        }
        with self.assertRaises(SchemaValidationError):
            validate_json_schema(winnerless_conflict, PROJECTION_SCHEMA_PATH)

        capability_orphan = copy.deepcopy(valid)
        capability_orphan["families"]["supervisor_orphans"]["value"][
            "supervisor-orphaned-example"
        ]["capability"] = {"invented": True}
        with self.assertRaises(SchemaValidationError):
            validate_json_schema(capability_orphan, PROJECTION_SCHEMA_PATH)

    def test_static_copy_is_floati_voiced_and_v0_is_fenced(self) -> None:
        """Catches an upstream-product reference, unbranded C7 copy, or a C7.1 document that reopens v0."""
        readme = (C7 / "README.md").read_text(encoding="utf-8")
        self.assertTrue(readme.startswith("# Floati C7.1 Read Bundle"))
        self.assertIn("physical file order", readme)
        self.assertIn("cross-ledger causal merge", readme)
        self.assertIn("self_digest", readme)
        self.assertIn("conflicting_binding", readme)
        self.assertIn("read-only", readme)
        for path in C7.rglob("*"):
            if path.is_file():
                with self.subTest(path=path.relative_to(ROOT)):
                    self.assertNotIn("puddle", path.read_text(encoding="utf-8").lower())


if __name__ == "__main__":
    unittest.main()

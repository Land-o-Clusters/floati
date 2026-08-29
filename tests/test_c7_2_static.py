from __future__ import annotations

import hashlib
import json
import unittest
from copy import deepcopy
from pathlib import Path

from floati.errors import ProtocolRefusal
from tests.schema_validation import validate_json_schema
from tests.test_c7_static import EXPECTED_SOURCES, PROJECTION_POINTERS, catalog_sources


ROOT = Path(__file__).resolve().parents[1]
C7 = ROOT / "bundle" / "c7.2"
FLOATI_SCHEMA_ORIGIN = "https://landoclusters.com/floati/schemas/"
C7_2_PROJECTION_SCHEMA_ID = (
    FLOATI_SCHEMA_ORIGIN + "c7.2/canonical-projection.schema.json"
)
C7_2_V1_BINDING_POINTERS = (
    "/run_id",
    "/item_id",
    "/attempt_id",
    "/fence_token",
    "/claim_id",
    "/lease_id",
    "/worker_session_id",
    "/harness_segments",
    "/harness_segments/n/ordinal",
    "/harness_segments/n/harness_session_id",
    "/harness_segments/n/segment_id",
    "/harness_segments/n/segment_kind",
    "/harness_segments/n/predecessor_segment_id",
)
C7_2_EXPECTED_CATALOG_SOURCES_BY_FILE = {
    source_file: (source_id, 0, expected_pointers)
    for source_id, (source_file, expected_pointers) in EXPECTED_SOURCES.items()
}
C7_2_EXPECTED_CATALOG_SOURCES_BY_FILE.update(
    {
        "schemas/canonical-projection.schema.json": (
            C7_2_PROJECTION_SCHEMA_ID,
            "c7.2-candidate",
            PROJECTION_POINTERS,
        ),
        "schemas/v1/attempt-harness-session-bound-record.schema.json": (
            FLOATI_SCHEMA_ORIGIN
            + "v1/attempt-harness-session-bound-record.schema.json",
            1,
            set(C7_2_V1_BINDING_POINTERS),
        ),
    }
)


def c7_2_catalog_sources_by_file(catalog: dict[str, object]) -> dict[str, dict[str, object]]:
    by_file: dict[str, dict[str, object]] = {}
    for source_id, declared in catalog_sources(catalog).items():
        source_file = declared["file"]
        if source_file in by_file:
            raise AssertionError(
                f"C7.2 catalog maps {source_file} from more than one identity"
            )
        by_file[source_file] = {"id": source_id, **declared}
    return by_file


def _pointer(ledger: str = "raw/runs/events.jsonl") -> dict[str, object]:
    return {"ledger": ledger, "first_frame": 0, "last_frame": 0}


def _absent(reason: str, ledger: str = "raw/runs/events.jsonl") -> dict[str, object]:
    return {"state": "absent", "reason": reason, "raw_fallback": _pointer(ledger)}


def _projection_fixture() -> dict[str, object]:
    """A fully ruled empty projection, rather than an unruled `{}` family map."""

    families = {
        "runs": _absent("no_run_frames"),
        "work_items": _absent("no_run_items"),
        "attempts": _absent("no_attempt_frames"),
        "claims": _absent("no_opaque_claim_references"),
        "leases": _absent("no_opaque_lease_references"),
        "retries": _absent("no_retry_frames"),
        "cancellations": _absent("no_cancellation_frames"),
        "result_phases": _absent("no_result_frames"),
        "logical_outcomes": _absent("no_logical_outcomes"),
        "run_outcomes": _absent("no_run_outcomes"),
        "task_contracts": _absent("no_task_contract_frames"),
        "session_bindings": {
            **_absent("no_session_binding_frames"),
            "segment_kind_vocabulary": ["initial", "resume", "fork", "handoff"],
        },
        "supervisor_orphans": _absent("no_supervisor_orphan_frames"),
        "decisions": _absent("decision_register_absent"),
    }
    auxiliary_sources = {
        "worker_receipts": {
            "ledger": "raw/receipts/workers.jsonl",
            "raw_source_digest": "a" * 64,
            "source_frames": [],
            **_absent("source_absent", "raw/receipts/workers.jsonl"),
        },
        "registry": {
            "ledger": "raw/registry/entries.jsonl",
            "raw_source_digest": "a" * 64,
            "source_frames": [],
            **_absent("source_absent", "raw/registry/entries.jsonl"),
        },
        "decisions": {
            "ledger": "raw/repositories/owner/repo/decisions.jsonl",
            "raw_source_digest": "a" * 64,
            "source_frames": [],
            **_absent("source_absent", "raw/repositories/owner/repo/decisions.jsonl"),
        },
        "work_items": {
            "ledger": "raw/work/items.jsonl",
            "raw_source_digest": "a" * 64,
            "source_frames": [],
            **_absent("source_absent", "raw/work/items.jsonl"),
        },
    }
    return {
        "schema_version": "c7.2-candidate",
        "kind": "c7_canonical_projection",
        "tenant_id": "alpha",
        "repository": "owner/repo",
        "raw_source": "raw/runs/events.jsonl",
        "raw_source_digest": "a" * 64,
        "source_frames": [],
        "families": families,
        "auxiliary_sources": auxiliary_sources,
        "cross_ledger_rule": "no_timestamp_merge",
        "semantic_digest": "a" * 64,
        "self_digest": "a" * 64,
    }


class C7_2StaticContractTests(unittest.TestCase):
    def test_c7_2_package_has_exact_candidate_literals_and_predecessor(self) -> None:
        from floati.c7_2_bundle import validate_c7_2_index

        index = json.loads((C7 / "bundle-index.json").read_text(encoding="utf-8"))
        self.assertEqual("c7.2-candidate", index["schema_version"])
        self.assertEqual("excluded-c7.2", index["approvals"])
        self.assertEqual(
            {
                "path": "bundle/c7.1/bundle-index.json",
                "version": "c7.1-candidate",
                "mutation": "forbidden",
            },
            index["predecessor"],
        )
        self.assertEqual(
            {"highest_understood": True, "unknown": "fail_closed"},
            index["reader_upgrade"],
        )
        self.assertEqual(
            {
                "id": FLOATI_SCHEMA_ORIGIN + "c7.2/c7-read-bundle.schema.json",
                "version": "c7.2-candidate",
                "file": "schemas/c7-read-bundle.schema.json",
            },
            index["index_schema"],
        )
        for hostile in (1, 1.0):
            mutated = deepcopy(index)
            mutated["reader_upgrade"]["highest_understood"] = hostile
            with self.subTest(hostile=hostile):
                with self.assertRaises(ProtocolRefusal) as raised:
                    validate_c7_2_index(mutated)
                self.assertEqual("c7_upgrade_rule_invalid", raised.exception.code)

    def test_c7_2_index_and_projection_schemas_validate_their_literals(self) -> None:
        index_path = C7 / "schemas/c7-read-bundle.schema.json"
        projection_path = C7 / "schemas/canonical-projection.schema.json"
        index = json.loads((C7 / "bundle-index.json").read_text(encoding="utf-8"))
        projection = _projection_fixture()
        validate_json_schema(index, index_path)
        validate_json_schema(projection, projection_path)

        missing_family = _projection_fixture()
        del missing_family["families"]["session_bindings"]
        with self.assertRaises(AssertionError):
            validate_json_schema(missing_family, projection_path)

    def test_c7_2_source_schema_is_v1_and_names_no_relation(self) -> None:
        schema = json.loads(
            (ROOT / "schemas/v1/attempt-harness-session-bound-record.schema.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(1, schema["properties"]["schema_version"]["const"])
        self.assertIn("segment_kind", json.dumps(schema, sort_keys=True))
        self.assertNotIn('"relation"', json.dumps(schema, sort_keys=True))

    def test_c7_2_catalog_keeps_the_v0_inventory_and_adds_the_v1_binding_source(self) -> None:
        from floati.c7_2_bundle import validate_c7_2_catalog

        catalog = json.loads((C7 / "schema-catalog.json").read_text(encoding="utf-8"))
        self.assertEqual("c7.2-candidate", catalog["schema_version"])
        approval = next(
            entry for entry in catalog["entries"] if entry["family"] == "approval"
        )
        self.assertEqual("excluded-c7.2", approval["representation"])
        for coordinate, expected in (
            ("id", C7_2_PROJECTION_SCHEMA_ID),
            ("version", "c7.2-candidate"),
            ("file", "schemas/canonical-projection.schema.json"),
        ):
            with self.subTest(schema="projection_schema", coordinate=coordinate):
                self.assertEqual(expected, catalog["projection_schema"][coordinate])
        sources_by_file = c7_2_catalog_sources_by_file(catalog)
        self.assertEqual(
            set(C7_2_EXPECTED_CATALOG_SOURCES_BY_FILE),
            set(sources_by_file),
        )
        for source_file, (source_id, version, pointers) in (
            C7_2_EXPECTED_CATALOG_SOURCES_BY_FILE.items()
        ):
            declared = sources_by_file[source_file]
            with self.subTest(schema=source_file, coordinate="id"):
                self.assertEqual(source_id, declared["id"])
            with self.subTest(schema=source_file, coordinate="version"):
                self.assertEqual(version, declared["version"])
            with self.subTest(schema=source_file, coordinate="pointers"):
                self.assertEqual(pointers, declared["pointers"])
        sources = [
            source
            for entry in catalog["entries"]
            for source in entry["sources"]
        ]
        binding_sources = [
            source
            for source in sources
            if source["id"].endswith("/attempt-harness-session-bound-record.schema.json")
        ]
        self.assertEqual({0, 1}, {source["version"] for source in binding_sources})
        version_one = next(source for source in binding_sources if source["version"] == 1)
        self.assertEqual(
            "schemas/v1/attempt-harness-session-bound-record.schema.json",
            version_one["file"],
        )
        self.assertEqual(
            FLOATI_SCHEMA_ORIGIN + "v1/attempt-harness-session-bound-record.schema.json",
            version_one["id"],
        )
        self.assertEqual(
            list(C7_2_V1_BINDING_POINTERS),
            version_one["pointers"],
        )

        incomplete = deepcopy(catalog)
        incomplete["entries"] = [
            entry for entry in incomplete["entries"] if entry["family"] != "run"
        ]
        with self.assertRaises(ProtocolRefusal) as raised:
            validate_c7_2_catalog(incomplete)
        self.assertEqual("c7_catalog_shape_invalid", raised.exception.code)

    def test_c7_2_package_files_are_digestable(self) -> None:
        files = sorted(path for path in C7.rglob("*") if path.is_file())
        self.assertGreaterEqual(len(files), 5)
        self.assertTrue(all(len(hashlib.sha256(path.read_bytes()).hexdigest()) == 64 for path in files))


if __name__ == "__main__":
    unittest.main()

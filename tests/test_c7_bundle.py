from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from floati.cancellation import CancellationCoordinator
from floati.decisions import decision_digest
from floati.errors import ProtocolRefusal
from floati.framing import encode_frame
from floati.ids import uuid7_hex
from floati.root import FloatiRoot
from tests.hm3i_gauntlet_fixtures import (
    NOW,
    build_decision_case,
    build_foc_orphan_trace,
    build_success_trace,
)
from tests.schema_validation import validate_json_schema
from tests.test_c7_static import (
    C7_1_EXPECTED_CATALOG_SOURCES_BY_FILE,
    catalog_sources_by_file,
)


class C7ReadBundleTests(unittest.TestCase):
    @staticmethod
    def _tree_bytes(root: Path) -> dict[str, bytes]:
        return {
            path.relative_to(root).as_posix(): path.read_bytes()
            for path in sorted(root.rglob("*"))
            if path.is_file()
        }

    def test_c7_1_materializes_exact_raw_digest_and_physical_projection(self) -> None:
        """Catches a bundle writer that normalizes raw frames or selects state by timestamp."""
        from floati.c7_bundle import build_c7_1_bundle

        with tempfile.TemporaryDirectory() as directory:
            root = FloatiRoot.open_direct_home(Path(directory) / "alpha", create=True)
            trace = build_foc_orphan_trace(root)
            destination = Path(directory) / "c7.1"

            index = build_c7_1_bundle(
                root,
                destination,
                repository="owner/repo",
            )

            raw = (root.tenant_home / "runs/events.jsonl").read_bytes()
            projection = json.loads(
                (destination / "families/run-projection.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual("c7.1-candidate", index["schema_version"])
            self.assertEqual("excluded-c7.1", index["approvals"])
            self.assertEqual(hashlib.sha256(raw).hexdigest(), projection["raw_source_digest"])
            self.assertEqual(
                list(range(1, len(trace.ledger.records()) + 1)),
                [frame["ordinal"] for frame in projection["source_frames"]],
            )
            self.assertEqual(
                "absent",
                projection["families"]["session_bindings"]["value"]
                [trace.attempt_ids[0]]["segments"][0]["segment_kind"]["state"],
            )
            self.assertIn(trace.lease_id, projection["families"]["leases"]["value"])

    def test_c7_1_names_the_closed_future_relation_vocabulary_without_inference(self) -> None:
        """Catches a typed-absent Q5 projection that loses the ruled relation vocabulary."""
        from floati.c7_bundle import build_c7_1_bundle

        with tempfile.TemporaryDirectory() as directory:
            root = FloatiRoot.open_direct_home(Path(directory) / "alpha", create=True)
            build_foc_orphan_trace(root)
            destination = Path(directory) / "c7.1"
            build_c7_1_bundle(root, destination, repository="owner/repo")
            projection = json.loads(
                (destination / "families/run-projection.json").read_text(
                    encoding="utf-8"
                )
            )

            self.assertEqual(
                ["resume", "fork", "handoff"],
                projection["families"]["session_bindings"]["segment_relation_vocabulary"],
            )

    def test_c7_1_refuses_a_destination_inside_the_source_tenant_before_mutation(self) -> None:
        """Catches a read bundle materializer creating its snapshot inside its source root."""
        from floati.c7_bundle import build_c7_1_bundle

        with tempfile.TemporaryDirectory() as directory:
            root = FloatiRoot.open_direct_home(Path(directory) / "alpha", create=True)
            build_foc_orphan_trace(root)
            before = self._tree_bytes(root.tenant_home)

            with self.assertRaises(ProtocolRefusal) as raised:
                build_c7_1_bundle(
                    root,
                    root.tenant_home / "c7.1",
                    repository="owner/repo",
                )

            self.assertEqual("c7_destination_inside_source", raised.exception.code)
            self.assertEqual(before, self._tree_bytes(root.tenant_home))

    def test_c7_1_refuses_a_non_path_destination_with_a_typed_boundary_error(self) -> None:
        """Catches an ambient TypeError leaking through the public destination boundary."""
        from floati.c7_bundle import build_c7_1_bundle

        with tempfile.TemporaryDirectory() as directory:
            root = FloatiRoot.open_direct_home(Path(directory) / "alpha", create=True)
            before = self._tree_bytes(root.tenant_home)

            with self.assertRaises(ProtocolRefusal) as raised:
                build_c7_1_bundle(root, object(), repository="owner/repo")

            self.assertEqual("c7_destination_invalid", raised.exception.code)
            self.assertEqual(before, self._tree_bytes(root.tenant_home))

    def test_c7_1_external_snapshot_leaves_every_source_byte_unchanged(self) -> None:
        """Catches a supposedly read-only materializer changing source files or creating source artifacts."""
        from floati.c7_bundle import build_c7_1_bundle

        with tempfile.TemporaryDirectory() as directory:
            root = FloatiRoot.open_direct_home(Path(directory) / "alpha", create=True)
            build_foc_orphan_trace(root)
            before = self._tree_bytes(root.tenant_home)

            build_c7_1_bundle(
                root,
                Path(directory) / "external-c7.1",
                repository="owner/repo",
            )

            self.assertEqual(before, self._tree_bytes(root.tenant_home))

    def test_c7_1_refuses_a_destination_beneath_the_checked_in_contract_package(self) -> None:
        """Catches a hostile path that would turn the checked-in reader contract into output storage."""
        from floati import c7_bundle

        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = FloatiRoot.open_direct_home(base / "alpha", create=True)
            build_foc_orphan_trace(root)
            package = base / "contract"
            shutil.copytree(Path("bundle/c7.1"), package)
            before = self._tree_bytes(package)

            with patch.object(c7_bundle, "_PACKAGE_ROOT", package):
                with self.assertRaises(ProtocolRefusal) as raised:
                    c7_bundle.build_c7_1_bundle(
                        root,
                        package / "nested-output",
                        repository="owner/repo",
                    )

            self.assertEqual("c7_destination_contract_package", raised.exception.code)
            self.assertEqual(before, self._tree_bytes(package))

    def test_reader_fails_closed_at_an_unknown_bundle_index_version(self) -> None:
        """Catches a reader silently treating an unknown C7 index as a compatible snapshot."""
        from floati.c7_bundle import read_c7_1_bundle

        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "c7.9"
            destination.mkdir()
            (destination / "bundle-index.json").write_text(
                json.dumps(
                    {
                        "schema_version": "c7.9-unknown",
                        "kind": "c7_read_bundle_index",
                        "approvals": "excluded-c7.1",
                        "reader_upgrade": {"highest_understood": True, "unknown": "fail_closed"},
                        "families": [{"name": "run_truth"}],
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaises(ProtocolRefusal) as raised:
                read_c7_1_bundle(destination)

            self.assertEqual("c7_version_unsupported", raised.exception.code)

    def test_reader_fails_closed_when_required_index_schema_identity_is_removed(self) -> None:
        """Catches a reader accepting C7.1 metadata without the schema identity it claims to use."""
        from floati.c7_bundle import build_c7_1_bundle, read_c7_1_bundle

        with tempfile.TemporaryDirectory() as directory:
            root = FloatiRoot.open_direct_home(Path(directory) / "alpha", create=True)
            build_foc_orphan_trace(root)
            destination = Path(directory) / "c7.1"
            build_c7_1_bundle(root, destination, repository="owner/repo")
            index_path = destination / "bundle-index.json"
            index = json.loads(index_path.read_text(encoding="utf-8"))
            del index["index_schema"]
            index_path.write_text(json.dumps(index), encoding="utf-8")

            with self.assertRaises(ProtocolRefusal) as raised:
                read_c7_1_bundle(destination)

            self.assertEqual("c7_index_schema_invalid", raised.exception.code)

    def test_reader_refuses_a_digest_consistent_projection_with_a_null_family(self) -> None:
        """Catches a reader trusting digests while accepting the forbidden null-as-unknown shape."""
        from floati.c7_bundle import (
            build_c7_1_bundle,
            read_c7_1_bundle,
            self_digest,
            semantic_digest,
        )

        with tempfile.TemporaryDirectory() as directory:
            root = FloatiRoot.open_direct_home(Path(directory) / "alpha", create=True)
            build_foc_orphan_trace(root)
            destination = Path(directory) / "c7.1"
            build_c7_1_bundle(root, destination, repository="owner/repo")
            projection_path = destination / "families/run-projection.json"
            projection = json.loads(projection_path.read_text(encoding="utf-8"))
            projection["families"]["runs"] = None
            projection["semantic_digest"] = semantic_digest(projection)
            projection["self_digest"] = self_digest(projection)
            projection_path.write_text(json.dumps(projection), encoding="utf-8")

            with self.assertRaises(ProtocolRefusal) as raised:
                read_c7_1_bundle(destination)

            self.assertEqual("c7_projection_shape_invalid", raised.exception.code)

    def test_reader_refuses_a_digest_consistent_empty_present_run_map(self) -> None:
        """Catches a present current-state map that proves no state from a nonempty run ledger."""
        from floati.c7_bundle import (
            build_c7_1_bundle,
            read_c7_1_bundle,
            self_digest,
            semantic_digest,
        )

        with tempfile.TemporaryDirectory() as directory:
            root = FloatiRoot.open_direct_home(Path(directory) / "alpha", create=True)
            build_foc_orphan_trace(root)
            destination = Path(directory) / "c7.1"
            build_c7_1_bundle(root, destination, repository="owner/repo")
            projection_path = destination / "families/run-projection.json"
            projection = json.loads(projection_path.read_text(encoding="utf-8"))
            projection["families"]["runs"] = {"state": "present", "value": {}}
            projection["semantic_digest"] = semantic_digest(projection)
            projection["self_digest"] = self_digest(projection)
            projection_path.write_text(json.dumps(projection), encoding="utf-8")

            with self.assertRaises(ProtocolRefusal) as raised:
                read_c7_1_bundle(destination)

            self.assertEqual("c7_projection_shape_invalid", raised.exception.code)

    def test_reader_refuses_an_index_with_an_extra_or_malformed_family_shape(self) -> None:
        """Catches a same-version index that bypasses its own closed schema by adding arbitrary fields."""
        from floati.c7_bundle import validate_c7_1_index

        index = json.loads(Path("bundle/c7.1/bundle-index.json").read_text(encoding="utf-8"))
        index["unexpected"] = True
        with self.assertRaises(ProtocolRefusal) as raised:
            validate_c7_1_index(index)
        self.assertEqual("c7_index_shape_invalid", raised.exception.code)

    def test_malformed_unreferenced_worker_source_is_isolated_from_run_state(self) -> None:
        """Catches an auxiliary worker decoder failure being falsely blamed on physical run frames."""
        from floati.c7_bundle import build_c7_1_bundle

        with tempfile.TemporaryDirectory() as directory:
            root = FloatiRoot.open_direct_home(Path(directory) / "alpha", create=True)
            build_foc_orphan_trace(root)
            worker_path = root.tenant_home / "receipts/workers.jsonl"
            worker_path.parent.mkdir(parents=True, exist_ok=True)
            worker_path.write_bytes(b"{\n")
            destination = Path(directory) / "c7.1"
            build_c7_1_bundle(root, destination, repository="owner/repo")
            projection = json.loads(
                (destination / "families/run-projection.json").read_text(encoding="utf-8")
            )

            worker = projection["auxiliary_sources"]["worker_receipts"]
            self.assertEqual("error", worker["state"]["kind"])
            self.assertEqual("raw/receipts/workers.jsonl", worker["raw_fallback"]["ledger"])
            self.assertEqual("present", projection["families"]["runs"]["state"])
            self.assertEqual("present", projection["families"]["attempts"]["state"])

    def test_malformed_referenced_worker_source_errors_only_dependent_families(self) -> None:
        """Catches accepted/result conclusions surviving invalid evidence or poisoning independent run maps."""
        from floati.c7_bundle import build_c7_1_bundle

        with tempfile.TemporaryDirectory() as directory:
            root = FloatiRoot.open_direct_home(Path(directory) / "alpha", create=True)
            build_success_trace(root)
            worker_path = root.tenant_home / "receipts/workers.jsonl"
            worker_path.write_bytes(b"{\n")
            destination = Path(directory) / "c7.1"
            build_c7_1_bundle(root, destination, repository="owner/repo")
            projection = json.loads(
                (destination / "families/run-projection.json").read_text(encoding="utf-8")
            )

            self.assertEqual("present", projection["families"]["attempts"]["state"])
            for family in ("result_phases", "logical_outcomes", "run_outcomes"):
                with self.subTest(family=family):
                    error = projection["families"][family]
                    self.assertEqual("error", error["state"]["kind"])
                    self.assertEqual("raw/receipts/workers.jsonl", error["raw_fallback"]["ledger"])

    def test_c7_1_preflights_every_existing_output_symlink_before_any_write(self) -> None:
        """Catches raw/schema/family/index output links redirecting a snapshot write into its source."""
        from floati.c7_bundle import build_c7_1_bundle

        for child in ("raw", "schemas", "families", "bundle-index.json"):
            with self.subTest(child=child), tempfile.TemporaryDirectory() as directory:
                root = FloatiRoot.open_direct_home(Path(directory) / "alpha", create=True)
                build_foc_orphan_trace(root)
                destination = Path(directory) / "output"
                destination.mkdir()
                link = destination / child
                if child == "bundle-index.json":
                    link.symlink_to(root.tenant_home / "runs/events.jsonl")
                else:
                    link.symlink_to(root.tenant_home, target_is_directory=True)
                source_before = self._tree_bytes(root.tenant_home)
                output_before = os.readlink(link)

                with self.assertRaises(ProtocolRefusal) as raised:
                    build_c7_1_bundle(root, destination, repository="owner/repo")

                self.assertEqual("c7_destination_symlink", raised.exception.code)
                self.assertEqual(source_before, self._tree_bytes(root.tenant_home))
                self.assertTrue(link.is_symlink())
                self.assertEqual(output_before, os.readlink(link))

    def test_c7_1_refuses_a_destination_with_a_caller_controlled_symlink_ancestor(self) -> None:
        """Catches a lexical ancestor link redirecting a fresh-looking output outside the caller path."""
        from floati.c7_bundle import build_c7_1_bundle

        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = FloatiRoot.open_direct_home(base / "alpha", create=True)
            build_foc_orphan_trace(root)
            target = base / "outside"
            target.mkdir()
            redirect = base / "redirect"
            redirect.symlink_to(target, target_is_directory=True)
            before = self._tree_bytes(root.tenant_home)

            with self.assertRaises(ProtocolRefusal) as raised:
                build_c7_1_bundle(root, redirect / "snapshot", repository="owner/repo")

            self.assertEqual("c7_destination_symlink", raised.exception.code)
            self.assertEqual(before, self._tree_bytes(root.tenant_home))
            self.assertFalse((target / "snapshot").exists())

    def test_duplicate_harness_session_inside_one_binding_is_conflicting_without_winner(self) -> None:
        """Catches set-collapse treating a duplicate session in one binding as a selected relationship."""
        from floati.c7_bundle import build_c7_1_bundle

        with tempfile.TemporaryDirectory() as directory:
            root = FloatiRoot.open_direct_home(Path(directory) / "alpha", create=True)
            trace = build_foc_orphan_trace(root)
            duplicate = "worker-" + uuid7_hex()
            self._add_binding(
                trace,
                claim_id=str(trace.claim_id),
                lease_id=str(trace.lease_id),
                worker_session_id=str(trace.worker_session_id),
                sessions=[duplicate, duplicate],
            )
            destination = Path(directory) / "c7.1"
            build_c7_1_bundle(root, destination, repository="owner/repo")
            projection = json.loads(
                (destination / "families/run-projection.json").read_text(encoding="utf-8")
            )
            binding = projection["families"]["session_bindings"]["value"][trace.attempt_ids[0]]

            self.assertEqual("conflicting_binding", binding["state"]["code"])
            self.assertNotIn("selected", binding)

    def test_segment_typed_absence_fallbacks_are_bundle_relative_raw_paths(self) -> None:
        """Catches Q5 absence pointers naming a source-relative path that does not exist in a snapshot."""
        from floati.c7_bundle import build_c7_1_bundle

        with tempfile.TemporaryDirectory() as directory:
            root = FloatiRoot.open_direct_home(Path(directory) / "alpha", create=True)
            trace = build_foc_orphan_trace(root)
            destination = Path(directory) / "c7.1"
            build_c7_1_bundle(root, destination, repository="owner/repo")
            projection = json.loads(
                (destination / "families/run-projection.json").read_text(encoding="utf-8")
            )
            segment = projection["families"]["session_bindings"]["value"][trace.attempt_ids[0]]["segments"][0]

            for field in ("segment_kind", "predecessor_segment_id"):
                with self.subTest(field=field):
                    self.assertEqual("raw/runs/events.jsonl", segment[field]["raw_fallback"]["ledger"])

    def test_malformed_run_projection_keeps_the_closed_q5_vocabulary_schema_valid(self) -> None:
        """Catches a generic run-decoder error dropping the fixed Q5 vocabulary from its family."""
        from floati.c7_bundle import build_c7_1_bundle

        with tempfile.TemporaryDirectory() as directory:
            root = FloatiRoot.open_direct_home(Path(directory) / "alpha", create=True)
            run_path = root.tenant_home / "runs/events.jsonl"
            run_path.parent.mkdir(parents=True, exist_ok=True)
            run_path.write_bytes(b"{\n")
            destination = Path(directory) / "c7.1"
            build_c7_1_bundle(root, destination, repository="owner/repo")
            projection = json.loads(
                (destination / "families/run-projection.json").read_text(encoding="utf-8")
            )

            validate_json_schema(
                projection,
                destination / "schemas/canonical-projection.schema.json",
            )
            self.assertEqual(
                ["resume", "fork", "handoff"],
                projection["families"]["session_bindings"]["segment_relation_vocabulary"],
            )

    def test_public_projector_refuses_non_bytes_raw_run_override(self) -> None:
        """Catches a hostile direct API caller receiving a raw hashlib TypeError at this boundary."""
        from floati.c7_bundle import project_c7_1

        with tempfile.TemporaryDirectory() as directory:
            root = FloatiRoot.open_direct_home(Path(directory) / "alpha", create=True)
            with self.assertRaises(ProtocolRefusal) as raised:
                project_c7_1(root, repository="owner/repo", raw_run_bytes=object())
            self.assertEqual("c7_raw_run_bytes_invalid", raised.exception.code)

    def test_materialized_snapshot_contains_hashed_catalog_schemas_and_advertised_raw_ledgers(self) -> None:
        """Catches a catalog that points outside its snapshot or advertises evidence that was not copied."""
        from floati.c7_bundle import build_c7_1_bundle

        with tempfile.TemporaryDirectory() as directory:
            root = FloatiRoot.open_direct_home(Path(directory) / "alpha", create=True)
            build_foc_orphan_trace(root)
            destination = Path(directory) / "c7.1"
            build_c7_1_bundle(root, destination, repository="owner/repo")
            catalog = self._catalog(destination)

            actual_by_file = catalog_sources_by_file(catalog)
            self.assertEqual(
                set(C7_1_EXPECTED_CATALOG_SOURCES_BY_FILE),
                set(actual_by_file),
            )
            for source_file, (source_id, version, pointers) in (
                C7_1_EXPECTED_CATALOG_SOURCES_BY_FILE.items()
            ):
                declared = actual_by_file[source_file]
                with self.subTest(schema=source_file, coordinate="id"):
                    self.assertEqual(source_id, declared["id"])
                with self.subTest(schema=source_file, coordinate="version"):
                    self.assertEqual(version, declared["version"])
                with self.subTest(schema=source_file, coordinate="pointers"):
                    self.assertEqual(pointers, declared["pointers"])

            for entry in catalog["entries"]:
                for source in entry.get("sources", []):
                    with self.subTest(schema=source["id"]):
                        copied = destination / source["file"]
                        self.assertTrue(copied.is_file())
                        self.assertEqual(
                            source["sha256"],
                            hashlib.sha256(copied.read_bytes()).hexdigest(),
                        )
            self.assertTrue((destination / "raw/work/items.jsonl").is_file())

    def test_reader_verifies_copied_catalog_schemas_and_auxiliary_raw_digests(self) -> None:
        """Catches a self-consistent projection whose copied schema or auxiliary testimony changed."""
        from floati.c7_bundle import build_c7_1_bundle, read_c7_1_bundle

        with tempfile.TemporaryDirectory() as directory:
            root = FloatiRoot.open_direct_home(Path(directory) / "alpha", create=True)
            build_foc_orphan_trace(root)
            destination = Path(directory) / "c7.1"
            build_c7_1_bundle(root, destination, repository="owner/repo")

            (destination / "schemas/v0/run-created-record.schema.json").write_bytes(b"{}\n")
            with self.assertRaises(ProtocolRefusal) as raised:
                read_c7_1_bundle(destination)
            self.assertEqual("c7_catalog_schema_digest_invalid", raised.exception.code)

            build_c7_1_bundle(root, Path(directory) / "fresh", repository="owner/repo")
            fresh = Path(directory) / "fresh"
            (fresh / "raw/registry/entries.jsonl").write_bytes(b"tampered\n")
            with self.assertRaises(ProtocolRefusal) as raised:
                read_c7_1_bundle(fresh)
            self.assertEqual("c7_auxiliary_source_digest_invalid", raised.exception.code)

    def test_reader_reprojects_captured_bytes_before_accepting_state_or_outcome_claims(self) -> None:
        """Catches digest-consistent state/outcome text that is not derivable from physical frames."""
        from floati.c7_bundle import (
            build_c7_1_bundle,
            read_c7_1_bundle,
            self_digest,
            semantic_digest,
        )

        for target in ("state", "outcome"):
            with self.subTest(target=target), tempfile.TemporaryDirectory() as directory:
                root = FloatiRoot.open_direct_home(Path(directory) / "alpha", create=True)
                build_success_trace(root)
                destination = Path(directory) / "c7.1"
                build_c7_1_bundle(root, destination, repository="owner/repo")
                projection_path = destination / "families/run-projection.json"
                projection = json.loads(projection_path.read_text(encoding="utf-8"))
                if target == "state":
                    run = next(iter(projection["families"]["runs"]["value"].values()))
                    run["current_state"] = "open"
                else:
                    outcome = next(iter(projection["families"]["run_outcomes"]["value"].values()))
                    outcome["outcome"] = "failed"
                projection["semantic_digest"] = semantic_digest(projection)
                projection["self_digest"] = self_digest(projection)
                projection_path.write_text(json.dumps(projection), encoding="utf-8")

                with self.assertRaises(ProtocolRefusal) as raised:
                    read_c7_1_bundle(destination)

                self.assertEqual("c7_projection_reprojection_invalid", raised.exception.code)

    def test_reader_binds_claim_frames_and_decision_path_to_captured_identities(self) -> None:
        """Catches a digest-consistent physical reference or repository path detached from its source."""
        from floati.c7_bundle import (
            build_c7_1_bundle,
            read_c7_1_bundle,
            self_digest,
            semantic_digest,
        )

        with tempfile.TemporaryDirectory() as directory:
            root = FloatiRoot.open_direct_home(Path(directory) / "alpha", create=True)
            build_foc_orphan_trace(root)
            destination = Path(directory) / "c7.1"
            build_c7_1_bundle(root, destination, repository="owner/repo")
            projection_path = destination / "families/run-projection.json"
            projection = json.loads(projection_path.read_text(encoding="utf-8"))
            claim = next(iter(projection["families"]["claims"]["value"].values()))
            claim["frames"] = [999]
            projection["semantic_digest"] = semantic_digest(projection)
            projection["self_digest"] = self_digest(projection)
            projection_path.write_text(json.dumps(projection), encoding="utf-8")

            with self.assertRaises(ProtocolRefusal) as raised:
                read_c7_1_bundle(destination)
            self.assertEqual("c7_projection_shape_invalid", raised.exception.code)

            clean = Path(directory) / "clean"
            build_c7_1_bundle(root, clean, repository="owner/repo")
            clean_projection_path = clean / "families/run-projection.json"
            clean_projection = json.loads(clean_projection_path.read_text(encoding="utf-8"))
            clean_projection["repository"] = "other/repo"
            clean_projection["semantic_digest"] = semantic_digest(clean_projection)
            clean_projection["self_digest"] = self_digest(clean_projection)
            clean_projection_path.write_text(json.dumps(clean_projection), encoding="utf-8")

            with self.assertRaises(ProtocolRefusal) as raised:
                read_c7_1_bundle(clean)
            self.assertEqual("c7_projection_shape_invalid", raised.exception.code)

    def test_manifest_installed_layout_materializes_with_its_c7_static_package(self) -> None:
        """Catches deployment inventory omitting the static C7 package required by the installed module."""
        manifest = json.loads(Path("bundle-manifest.v0.json").read_text(encoding="utf-8"))

        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            installed = base / "installed"
            source_paths = [Path(entry["path"]) for entry in manifest["files"]]
            self.assertEqual(
                [],
                [path.as_posix() for path in source_paths if not path.is_file()],
                "the installed manifest must name materializable Floati files",
            )
            for relative in source_paths:
                target = installed / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(relative, target)
            script = """
from pathlib import Path
from floati.c7_bundle import build_c7_1_bundle
from floati.root import FloatiRoot
base = Path.cwd()
root = FloatiRoot.open_direct_home(base / 'tenant', create=True)
index = build_c7_1_bundle(root, base / 'snapshot', repository='owner/repo')
assert index['schema_version'] == 'c7.1-candidate'
"""
            environment = dict(os.environ)
            environment["PYTHONPATH"] = str(installed)
            environment["PYTHONPYCACHEPREFIX"] = str(base / "pycache")
            result = subprocess.run(
                [sys.executable, "-c", script],
                cwd=base,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(0, result.returncode, result.stderr or result.stdout)

    def test_auxiliary_timestamp_testimony_does_not_change_semantic_digest(self) -> None:
        """Catches exact auxiliary-byte testimony leaking into the timestamp-free semantic digest domain."""
        from floati.c7_bundle import build_c7_1_bundle

        with tempfile.TemporaryDirectory() as directory:
            root = FloatiRoot.open_direct_home(Path(directory) / "alpha", create=True)
            build_foc_orphan_trace(root)
            build_decision_case(root)
            before_destination = Path(directory) / "before"
            build_c7_1_bundle(root, before_destination, repository="owner/repo")
            before = json.loads(
                (before_destination / "families/run-projection.json").read_text(encoding="utf-8")
            )

            for relative in (
                "receipts/workers.jsonl",
                "registry/entries.jsonl",
                "repositories/owner/repo/decisions.jsonl",
            ):
                path = root.tenant_home / relative
                rows = [json.loads(line) for line in path.read_bytes().splitlines()]
                for row in rows:
                    row["timestamp"] = "2099-01-01T00:00:00.000Z"
                    if "decision_digest" in row:
                        row["decision_digest"] = decision_digest(row)
                path.write_bytes(b"".join(encode_frame(row) for row in rows))

            after_destination = Path(directory) / "after"
            build_c7_1_bundle(root, after_destination, repository="owner/repo")
            after = json.loads(
                (after_destination / "families/run-projection.json").read_text(encoding="utf-8")
            )

            self.assertEqual(before["semantic_digest"], after["semantic_digest"])
            for name in ("worker_receipts", "registry", "decisions"):
                with self.subTest(source=name):
                    self.assertNotEqual(
                        before["auxiliary_sources"][name]["raw_source_digest"],
                        after["auxiliary_sources"][name]["raw_source_digest"],
                    )

    def _materialized_projection(self, root: FloatiRoot, destination: Path) -> tuple[dict, dict]:
        from floati.c7_bundle import build_c7_1_bundle

        index = build_c7_1_bundle(root, destination, repository="owner/repo")
        projection = json.loads(
            (destination / "families/run-projection.json").read_text(encoding="utf-8")
        )
        return index, projection

    @staticmethod
    def _catalog(destination: Path) -> dict:
        return json.loads((destination / "schema-catalog.json").read_text(encoding="utf-8"))

    @staticmethod
    def _rewrite_timestamps(path: Path, timestamp: str) -> None:
        records = [json.loads(line) for line in path.read_bytes().splitlines()]
        for record in records:
            record["timestamp"] = timestamp
        path.write_bytes(b"".join(encode_frame(record) for record in records))

    @staticmethod
    def _add_binding(trace, *, claim_id: str, lease_id: str, worker_session_id: str, sessions: list[str]) -> None:
        CancellationCoordinator(trace.ledger).bind_harness_session(
            trace.run_id,
            trace.item_ids[0],
            trace.attempt_ids[0],
            claim_id=claim_id,
            lease_id=lease_id,
            worker_session_id=worker_session_id,
            harness_segments=[
                {"ordinal": ordinal, "harness_session_id": session}
                for ordinal, session in enumerate(sessions, start=1)
            ],
            now=NOW,
        )

    def test_c7_1_foc_bank_has_thirty_nine_executable_reader_vectors(self) -> None:
        """Catches a C7.1 contract that lists FOC cases without exercising its real reader path."""
        from floati.c7_bundle import (
            canonical_json_bytes,
            read_c7_1_bundle,
            semantic_digest,
            self_digest,
        )

        bank = json.loads(Path("tests/fixtures/c7.1/foc-bank.json").read_text(encoding="utf-8"))
        fixtures = bank["fixtures"]
        self.assertEqual(39, len(fixtures))
        self.assertEqual(39, len({fixture["id"] for fixture in fixtures}))
        reader_calls = 0

        def assert_reader_snapshot(
            snapshot: Path,
            expected_projection: dict | None = None,
            *,
            refusal_code: str | None = None,
        ) -> None:
            """Exercise the real reader once for the vector's produced artifact."""

            nonlocal reader_calls
            reader_calls += 1
            if refusal_code is not None:
                with self.assertRaises(ProtocolRefusal) as raised:
                    read_c7_1_bundle(snapshot)
                self.assertEqual(refusal_code, raised.exception.code)
                return
            self.assertIsNotNone(expected_projection)
            returned = read_c7_1_bundle(snapshot)
            self.assertEqual(
                canonical_json_bytes(expected_projection),
                canonical_json_bytes(returned["projection"]),
            )

        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            for fixture in fixtures:
                with self.subTest(fixture=fixture["id"]):
                    operation = fixture["operation"]
                    root = FloatiRoot.open_direct_home(base / fixture["id"] / "alpha", create=True)
                    trace = build_foc_orphan_trace(root)
                    destination = base / fixture["id"] / "snapshot"
                    index, projection = self._materialized_projection(root, destination)
                    catalog = self._catalog(destination)
                    catalog_entries = {entry["family"]: entry for entry in catalog["entries"]}
                    binding = projection["families"]["session_bindings"]["value"][trace.attempt_ids[0]]
                    reader_snapshot = destination
                    reader_projection: dict | None = projection
                    reader_refusal_code: str | None = None

                    if operation == "index_version":
                        self.assertEqual("c7.1-candidate", index["schema_version"])
                    elif operation == "approval_exclusion":
                        self.assertEqual("excluded-c7.1", index["approvals"])
                    elif operation == "unknown_index":
                        unknown = base / fixture["id"] / "unknown"
                        unknown.mkdir()
                        (unknown / "bundle-index.json").write_text(
                            json.dumps({**index, "schema_version": "c7.8-unknown"}),
                            encoding="utf-8",
                        )
                        reader_snapshot = unknown
                        reader_projection = None
                        reader_refusal_code = "c7_version_unsupported"
                    elif operation == "static_copy":
                        validate_json_schema(index, destination / "schemas/c7-read-bundle.schema.json")
                        validate_json_schema(projection, destination / "schemas/canonical-projection.schema.json")
                    elif operation == "catalog_run":
                        self.assertEqual("run", catalog_entries["run"]["family"])
                        self.assertIn(
                            "https://landoclusters.com/floati/schemas/v0/run-dispatch-decision-record.schema.json",
                            {source["id"] for source in catalog_entries["run"]["sources"]},
                        )
                    elif operation == "catalog_work":
                        self.assertIn("/item_ids", catalog_entries["work_item"]["sources"][0]["pointers"])
                    elif operation == "catalog_attempt":
                        self.assertEqual(3, len(catalog_entries["attempt"]["sources"]))
                    elif operation == "catalog_claim":
                        self.assertEqual("opaque_reference", catalog_entries["claim"]["representation"])
                    elif operation == "catalog_lease":
                        self.assertEqual("opaque_reference", catalog_entries["lease"]["representation"])
                    elif operation == "catalog_retry":
                        self.assertEqual(2, len(catalog_entries["retry"]["sources"]))
                    elif operation == "catalog_cancellation":
                        self.assertIn(
                            "https://landoclusters.com/floati/schemas/v0/stale-evidence-adopted-record.schema.json",
                            {source["id"] for source in catalog_entries["cancellation"]["sources"]},
                        )
                    elif operation == "catalog_result":
                        self.assertEqual(3, len(catalog_entries["result_phase"]["sources"]))
                    elif operation == "raw_digest":
                        raw = (root.tenant_home / "runs/events.jsonl").read_bytes()
                        self.assertEqual(hashlib.sha256(raw).hexdigest(), projection["raw_source_digest"])
                    elif operation == "semantic_domain":
                        mutated = json.loads(json.dumps(projection))
                        mutated["raw_source_digest"] = "0" * 64
                        mutated["self_digest"] = "f" * 64
                        self.assertEqual(projection["semantic_digest"], semantic_digest(mutated))
                    elif operation == "self_domain":
                        mutated = json.loads(json.dumps(projection))
                        mutated["raw_source_digest"] = "0" * 64
                        self.assertNotEqual(projection["self_digest"], self_digest(mutated))
                    elif operation == "typed_absent":
                        self.assertEqual("absent", binding["segments"][0]["segment_kind"]["state"])
                    elif operation == "malformed_frame":
                        (root.tenant_home / "runs/events.jsonl").write_bytes(b"{\n")
                        _index, malformed = self._materialized_projection(
                            root, base / fixture["id"] / "malformed"
                        )
                        self.assertEqual("error", malformed["families"]["runs"]["state"]["kind"])
                        reader_snapshot = base / fixture["id"] / "malformed"
                        reader_projection = malformed
                    elif operation == "raw_fallback":
                        (root.tenant_home / "runs/events.jsonl").write_bytes(b"{\n")
                        _index, malformed = self._materialized_projection(
                            root, base / fixture["id"] / "fallback"
                        )
                        fallback = malformed["families"]["attempts"]["raw_fallback"]
                        self.assertEqual("raw/runs/events.jsonl", fallback["ledger"])
                        self.assertGreaterEqual(fallback["last_frame"], fallback["first_frame"])
                        reader_snapshot = base / fixture["id"] / "fallback"
                        reader_projection = malformed
                    elif operation == "timestamp_invariance":
                        self._rewrite_timestamps(root.tenant_home / "runs/events.jsonl", "2099-01-01T00:00:00.000Z")
                        _index, rewritten = self._materialized_projection(
                            root, base / fixture["id"] / "rewritten"
                        )
                        self.assertEqual(projection["semantic_digest"], rewritten["semantic_digest"])
                        self.assertNotEqual(projection["raw_source_digest"], rewritten["raw_source_digest"])
                        reader_snapshot = base / fixture["id"] / "rewritten"
                        reader_projection = rewritten
                    elif operation == "physical_reorder":
                        source = root.tenant_home / "runs/events.jsonl"
                        lines = source.read_bytes().splitlines(keepends=True)
                        lines[-3:] = reversed(lines[-3:])
                        source.write_bytes(b"".join(lines))
                        _index, reordered = self._materialized_projection(
                            root, base / fixture["id"] / "reordered"
                        )
                        self.assertNotEqual(projection["source_frames"], reordered["source_frames"])
                        reader_snapshot = base / fixture["id"] / "reordered"
                        reader_projection = reordered
                    elif operation == "cross_ledger_no_merge":
                        decision_root = FloatiRoot.open_direct_home(base / fixture["id"] / "decision", create=True)
                        build_decision_case(decision_root)
                        _index, before = self._materialized_projection(
                            decision_root, base / fixture["id"] / "decision-before"
                        )
                        decision_path = decision_root.tenant_home / "repositories/owner/repo/decisions.jsonl"
                        rows = [json.loads(line) for line in decision_path.read_bytes().splitlines()]
                        for row in rows:
                            row["timestamp"] = "2099-01-01T00:00:00.000Z"
                            row["decision_digest"] = decision_digest(row)
                        decision_path.write_bytes(b"".join(encode_frame(row) for row in rows))
                        _index, after = self._materialized_projection(
                            decision_root, base / fixture["id"] / "decision-after"
                        )
                        self.assertEqual(before["semantic_digest"], after["semantic_digest"])
                        reader_snapshot = base / fixture["id"] / "decision-after"
                        reader_projection = after
                    elif operation == "binding_attempt":
                        self.assertEqual(trace.run_id, binding["run_id"])
                        self.assertIn(
                            trace.attempt_ids[0],
                            projection["families"]["session_bindings"]["value"],
                        )
                    elif operation == "binding_opaque_ids":
                        self.assertEqual(trace.claim_id, binding["claim_id"])
                        self.assertEqual(trace.lease_id, binding["lease_id"])
                    elif operation == "binding_source_ref":
                        self.assertEqual(binding["binding_record_id"], binding["segments"][0]["source_ref"]["binding_record_id"])
                    elif operation == "binding_no_inference":
                        self.assertNotIn("relation", binding["segments"][0])
                    elif operation == "q5_vocabulary":
                        self.assertEqual(["resume", "fork", "handoff"], projection["families"]["session_bindings"]["segment_relation_vocabulary"])
                    elif operation == "q5_predecessor_absent":
                        self.assertEqual("absent", binding["segments"][0]["predecessor_segment_id"]["state"])
                    elif operation == "q5_supersession":
                        self._add_binding(
                            trace,
                            claim_id=str(trace.claim_id),
                            lease_id=str(trace.lease_id),
                            worker_session_id=str(trace.worker_session_id),
                            sessions=["worker-" + uuid7_hex()],
                        )
                        _index, superseded = self._materialized_projection(
                            root, base / fixture["id"] / "superseded"
                        )
                        entry = superseded["families"]["session_bindings"]["value"][trace.attempt_ids[0]]
                        self.assertEqual("physical_binding_frame_order", entry["supersession"]["rule"])
                        reader_snapshot = base / fixture["id"] / "superseded"
                        reader_projection = superseded
                    elif operation == "q5_overlap_conflict":
                        self._add_binding(
                            trace,
                            claim_id=str(trace.claim_id),
                            lease_id=str(trace.lease_id),
                            worker_session_id=str(trace.worker_session_id),
                            sessions=[str(trace.worker_session_id)],
                        )
                        _index, conflicted = self._materialized_projection(
                            root, base / fixture["id"] / "overlap"
                        )
                        entry = conflicted["families"]["session_bindings"]["value"][trace.attempt_ids[0]]
                        self.assertEqual("conflicting_binding", entry["state"]["code"])
                        self.assertNotIn("selected", entry)
                        reader_snapshot = base / fixture["id"] / "overlap"
                        reader_projection = conflicted
                    elif operation == "q5_key_conflict":
                        self._add_binding(
                            trace,
                            claim_id="claim-" + uuid7_hex(),
                            lease_id="lease-" + uuid7_hex(),
                            worker_session_id="worker-" + uuid7_hex(),
                            sessions=["worker-" + uuid7_hex()],
                        )
                        _index, conflicted = self._materialized_projection(
                            root, base / fixture["id"] / "keys"
                        )
                        entry = conflicted["families"]["session_bindings"]["value"][trace.attempt_ids[0]]
                        self.assertEqual("conflicting_binding", entry["state"]["code"])
                        reader_snapshot = base / fixture["id"] / "keys"
                        reader_projection = conflicted
                    elif operation in {"q6_owner_loss", "q6_unregister", "q6_lease_abandonment"}:
                        classes = {
                            item["orphan_class"]
                            for item in projection["families"]["supervisor_orphans"]["value"].values()
                        }
                        self.assertIn(operation.removeprefix("q6_").replace("_", "_"), classes)
                    elif operation == "q6_lineage":
                        orphan = next(iter(projection["families"]["supervisor_orphans"]["value"].values()))
                        lineage = orphan["registration_lineage"]
                        self.assertEqual("floati-supervisor", lineage["node_id"])
                        self.assertIsInstance(lineage["frame"], int)
                        self.assertNotIn("capability_record_id", orphan)
                    elif operation == "decision_absent":
                        self.assertEqual("absent", projection["families"]["decisions"]["state"])
                    elif operation in {"decision_raw_only", "decision_physical_frames"}:
                        decision_root = FloatiRoot.open_direct_home(base / fixture["id"] / "decision", create=True)
                        build_decision_case(decision_root)
                        _index, decision_projection = self._materialized_projection(
                            decision_root, base / fixture["id"] / "decision-snapshot"
                        )
                        decisions = decision_projection["families"]["decisions"]["value"]
                        if operation == "decision_raw_only":
                            self.assertEqual("excluded-c7.1", decisions["consolidation"])
                            self.assertNotIn("capsule", decisions)
                        else:
                            self.assertEqual([1, 2], [frame["frame"] for frame in decisions["frames"]])
                        reader_snapshot = base / fixture["id"] / "decision-snapshot"
                        reader_projection = decision_projection
                    elif operation == "reader_raw_digest":
                        (destination / "raw/runs/events.jsonl").write_bytes(b"tampered\n")
                        reader_projection = None
                        reader_refusal_code = "c7_raw_source_digest_invalid"
                    elif operation == "source_root_fence":
                        from floati.c7_bundle import build_c7_1_bundle

                        with self.assertRaises(ProtocolRefusal) as raised:
                            build_c7_1_bundle(root, root.tenant_home / "nested", repository="owner/repo")
                        self.assertEqual("c7_destination_inside_source", raised.exception.code)
                    else:
                        self.fail(f"unimplemented C7 fixture operation: {operation}")

                    self.assertIsInstance(canonical_json_bytes(projection), bytes)
                    assert_reader_snapshot(
                        reader_snapshot,
                        reader_projection,
                        refusal_code=reader_refusal_code,
                    )

            self.assertEqual(39, reader_calls)


if __name__ == "__main__":
    unittest.main()

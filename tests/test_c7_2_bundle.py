from __future__ import annotations

import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from threading import Event, Thread
from unittest.mock import patch

from floati.cancellation import CancellationCoordinator
from floati.errors import ProtocolRefusal
from floati.framing import decode_frames, encode_frame
from floati.ids import uuid7_hex
from floati.records import validate_record
from floati.root import FloatiRoot
from tests.hm3i_gauntlet_fixtures import (
    NOW,
    build_foc_orphan_trace,
    build_retry_stale_trace,
)
from tests.test_c7_2_static import (
    C7_2_EXPECTED_CATALOG_SOURCES_BY_FILE,
    C7_2_PROJECTION_SCHEMA_ID,
    c7_2_catalog_sources_by_file,
)


FLOATI_SCHEMA_ORIGIN = "https://landoclusters.com/floati/schemas/"


class C7_2SegmentAmendmentTests(unittest.TestCase):
    @staticmethod
    def _segments() -> list[dict[str, object]]:
        initial = "seg-" + uuid7_hex()
        resumed = "seg-" + uuid7_hex()
        return [
            {
                "ordinal": 1,
                "harness_session_id": "worker-" + uuid7_hex(),
                "segment_id": initial,
                "segment_kind": "initial",
            },
            {
                "ordinal": 2,
                "harness_session_id": "worker-" + uuid7_hex(),
                "segment_id": resumed,
                "segment_kind": "resume",
                "predecessor_segment_id": initial,
            },
            {
                "ordinal": 3,
                "harness_session_id": "worker-" + uuid7_hex(),
                "segment_id": "seg-" + uuid7_hex(),
                "segment_kind": "fork",
                "predecessor_segment_id": initial,
            },
            {
                "ordinal": 4,
                "harness_session_id": "worker-" + uuid7_hex(),
                "segment_id": "seg-" + uuid7_hex(),
                "segment_kind": "handoff",
                "predecessor_segment_id": resumed,
            },
        ]

    @staticmethod
    def _bind(
        trace: object,
        segments: list[dict[str, object]],
        *,
        attempt_id: str | None = None,
    ) -> dict[str, object]:
        """Add a v1 candidate without manufacturing a C7.1 key conflict."""

        claim_id = getattr(trace, "claim_id", None) or "claim-" + uuid7_hex()
        lease_id = getattr(trace, "lease_id", None) or "lease-" + uuid7_hex()
        worker_session_id = (
            getattr(trace, "worker_session_id", None) or "worker-" + uuid7_hex()
        )
        return CancellationCoordinator(trace.ledger).bind_harness_session(
            trace.run_id,
            trace.item_ids[0],
            attempt_id or trace.attempt_ids[0],
            claim_id=claim_id,
            lease_id=lease_id,
            worker_session_id=worker_session_id,
            harness_segments=segments,
            schema_version=1,
            now=NOW,
        )

    def test_binding_v1_accepts_segment_kind_and_uuid7_segment_ids(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = FloatiRoot.open_direct_home(Path(directory) / "alpha", create=True)
            trace = build_foc_orphan_trace(root)
            record = self._bind(trace, self._segments())
            self.assertEqual(1, record["schema_version"])
            self.assertEqual("initial", record["harness_segments"][0]["segment_kind"])

    def test_non_binding_v1_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = FloatiRoot.open_direct_home(Path(directory) / "alpha", create=True)
            trace = build_foc_orphan_trace(root)
            record = dict(trace.records_by_kind["run_created"][0])
            record["schema_version"] = 1
            with self.assertRaises(ProtocolRefusal) as raised:
                validate_record(record, root.tenant_id, frozenset({"run_created"}), integrity=False)
            self.assertEqual("schema_version_invalid", raised.exception.code)

    def test_c7_1_remains_v0_only_when_a_v1_binding_is_present(self) -> None:
        """Catches C7.1 silently accepting the additive source version after dispatch widens."""

        from floati.c7_bundle import project_c7_1

        with tempfile.TemporaryDirectory() as directory:
            root = FloatiRoot.open_direct_home(Path(directory) / "alpha", create=True)
            trace = build_foc_orphan_trace(root)
            self._bind(trace, self._segments())
            projection = project_c7_1(root, repository="owner/repo")
            self.assertEqual(
                "c7_source_schema_version_unsupported",
                projection["families"]["session_bindings"]["state"]["code"],
            )

    def test_c7_2_preserves_legacy_absence_and_materializes_explicit_lineage(self) -> None:
        from floati.c7_2_bundle import build_c7_2_bundle, read_c7_2_bundle

        with tempfile.TemporaryDirectory() as directory:
            root = FloatiRoot.open_direct_home(Path(directory) / "alpha", create=True)
            trace = build_foc_orphan_trace(root)
            legacy_destination = Path(directory) / "legacy-c7.2"
            build_c7_2_bundle(root, legacy_destination, repository="owner/repo")
            legacy_projection = json.loads(
                (legacy_destination / "families/run-projection.json").read_text(
                    encoding="utf-8"
                )
            )
            legacy_segments = legacy_projection["families"]["session_bindings"]["value"][
                trace.attempt_ids[0]
            ]["segments"]
            self.assertTrue(
                all(item["segment_id"]["state"] == "absent" for item in legacy_segments)
            )
            self.assertTrue(
                all(item["segment_kind"]["state"] == "absent" for item in legacy_segments)
            )
            self.assertTrue(
                all(
                    item["predecessor_segment_id"]["state"] == "absent"
                    for item in legacy_segments
                )
            )
            self._bind(trace, self._segments())
            destination = Path(directory) / "c7.2"
            index = build_c7_2_bundle(root, destination, repository="owner/repo")
            projection = json.loads(
                (destination / "families/run-projection.json").read_text(encoding="utf-8")
            )
            family = projection["families"]["session_bindings"]
            self.assertEqual("c7.2-candidate", index["schema_version"])
            self.assertEqual("excluded-c7.2", index["approvals"])
            self.assertEqual(
                {"highest_understood": True, "unknown": "fail_closed"},
                index["reader_upgrade"],
            )
            self.assertEqual(
                ["initial", "resume", "fork", "handoff"],
                family["segment_kind_vocabulary"],
            )
            self.assertNotIn("segment_relation_vocabulary", family)
            segments = family["value"][trace.attempt_ids[0]]["segments"]
            self.assertTrue(all(item["segment_id"]["state"] == "present" for item in segments))
            self.assertEqual(
                {"initial", "resume", "fork", "handoff"},
                {
                    item["segment_kind"]["value"]
                    for item in segments
                    if item["segment_kind"]["state"] == "present"
                },
            )
            self.assertTrue(any(item["predecessor_segment_id"]["state"] == "present" for item in segments))
            self.assertEqual(
                hashlib.sha256((root.tenant_home / "runs/events.jsonl").read_bytes()).hexdigest(),
                projection["raw_source_digest"],
            )
            self.assertEqual(projection, read_c7_2_bundle(destination)["projection"])

    def test_c7_2_runtime_bundle_materializes_owned_schema_identities(self) -> None:
        """Catches a materialized C7.2 bundle retaining an old source or static-schema ID."""

        from floati.c7_2_bundle import build_c7_2_bundle

        with tempfile.TemporaryDirectory() as directory:
            root = FloatiRoot.open_direct_home(Path(directory) / "alpha", create=True)
            build_foc_orphan_trace(root)
            destination = Path(directory) / "c7.2"
            index = build_c7_2_bundle(root, destination, repository="owner/repo")
            catalog = json.loads((destination / "schema-catalog.json").read_text(encoding="utf-8"))

        with self.subTest(surface="index_schema"):
            self.assertEqual(
                {
                    "id": FLOATI_SCHEMA_ORIGIN
                    + "c7.2/c7-read-bundle.schema.json",
                    "version": "c7.2-candidate",
                    "file": "schemas/c7-read-bundle.schema.json",
                },
                index["index_schema"],
            )
        with self.subTest(surface="projection_schema"):
            self.assertEqual(
                {
                    "id": C7_2_PROJECTION_SCHEMA_ID,
                    "version": "c7.2-candidate",
                    "file": "schemas/canonical-projection.schema.json",
                },
                {
                    key: catalog["projection_schema"][key]
                    for key in ("id", "version", "file")
                },
            )

        sources_by_file = c7_2_catalog_sources_by_file(catalog)
        with self.subTest(surface="source_files"):
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

    def test_same_record_lower_ordinal_and_prior_frame_are_legal(self) -> None:
        from floati.c7_2_bundle import build_c7_2_bundle

        with tempfile.TemporaryDirectory() as directory:
            root = FloatiRoot.open_direct_home(Path(directory) / "alpha", create=True)
            trace = build_foc_orphan_trace(root)
            first = self._bind(trace, self._segments())
            predecessor = first["harness_segments"][0]["segment_id"]
            second = self._bind(
                trace,
                [
                    {
                        "ordinal": 1,
                        "harness_session_id": "worker-" + uuid7_hex(),
                        "segment_id": "seg-" + uuid7_hex(),
                        "segment_kind": "fork",
                        "predecessor_segment_id": predecessor,
                    }
                ],
            )
            build_c7_2_bundle(root, Path(directory) / "c7.2", repository="owner/repo")
            self.assertEqual(1, second["schema_version"])

    def test_missing_forward_and_duplicate_predecessor_shapes_refuse_without_inference(self) -> None:
        from floati.c7_2_bundle import project_c7_2

        with tempfile.TemporaryDirectory() as directory:
            root = FloatiRoot.open_direct_home(Path(directory) / "alpha", create=True)
            trace = build_foc_orphan_trace(root)
            self._bind(trace, self._segments())
            frames = decode_frames((root.tenant_home / "runs/events.jsonl").read_bytes())
            binding = next(item for item in frames if item["schema_version"] == 1)
            binding["harness_segments"][1]["predecessor_segment_id"] = "seg-" + uuid7_hex()
            raw = b"".join(encode_frame(frame) for frame in frames)
            with self.assertRaises(ProtocolRefusal) as raised:
                project_c7_2(root, repository="owner/repo", raw_run_bytes=raw)
            self.assertEqual("harness_predecessor_missing", raised.exception.code)

            binding["harness_segments"][1]["predecessor_segment_id"] = binding[
                "harness_segments"
            ][3]["segment_id"]
            raw = b"".join(encode_frame(frame) for frame in frames)
            with self.assertRaises(ProtocolRefusal) as raised:
                project_c7_2(root, repository="owner/repo", raw_run_bytes=raw)
            self.assertEqual("harness_predecessor_not_prior", raised.exception.code)

            binding["harness_segments"][1]["predecessor_segment_id"] = binding[
                "harness_segments"
            ][0]["segment_id"]
            binding["harness_segments"][2]["segment_id"] = binding["harness_segments"][0][
                "segment_id"
            ]
            raw = b"".join(encode_frame(frame) for frame in frames)
            with self.assertRaises(ProtocolRefusal) as raised:
                project_c7_2(root, repository="owner/repo", raw_run_bytes=raw)
            self.assertEqual("harness_segment_id_duplicate", raised.exception.code)

    def test_cross_attempt_predecessor_refuses(self) -> None:
        from floati.c7_2_bundle import project_c7_2

        with tempfile.TemporaryDirectory() as directory:
            root = FloatiRoot.open_direct_home(Path(directory) / "alpha", create=True)
            trace = build_retry_stale_trace(root)
            first = self._bind(trace, self._segments(), attempt_id=trace.attempt_ids[0])
            second_segments = self._segments()
            self._bind(trace, second_segments, attempt_id=trace.attempt_ids[1])
            frames = decode_frames((root.tenant_home / "runs/events.jsonl").read_bytes())
            second = next(
                frame
                for frame in frames
                if frame.get("schema_version") == 1
                and frame.get("attempt_id") == trace.attempt_ids[1]
            )
            second["harness_segments"][1]["predecessor_segment_id"] = first[
                "harness_segments"
            ][0]["segment_id"]
            raw = b"".join(encode_frame(frame) for frame in frames)
            with self.assertRaises(ProtocolRefusal) as raised:
                project_c7_2(root, repository="owner/repo", raw_run_bytes=raw)
            self.assertEqual("harness_predecessor_attempt_mismatch", raised.exception.code)

    def test_timestamp_testimony_changes_raw_digest_but_not_semantic_lineage(self) -> None:
        from floati.c7_2_bundle import project_c7_2

        with tempfile.TemporaryDirectory() as directory:
            root = FloatiRoot.open_direct_home(Path(directory) / "alpha", create=True)
            trace = build_foc_orphan_trace(root)
            self._bind(trace, self._segments())
            before = project_c7_2(root, repository="owner/repo")
            frames = decode_frames((root.tenant_home / "runs/events.jsonl").read_bytes())
            for frame in frames:
                frame["timestamp"] = "2099-01-01T00:00:00.000Z"
            raw = b"".join(encode_frame(frame) for frame in frames)
            after = project_c7_2(root, repository="owner/repo", raw_run_bytes=raw)
            self.assertEqual(before["semantic_digest"], after["semantic_digest"])
            self.assertNotEqual(before["raw_source_digest"], after["raw_source_digest"])

    def test_crash_like_truncated_and_non_utf8_physical_input_refuse_typed(self) -> None:
        """Catches a decoder leak that raises a raw parse exception for hostile run bytes."""

        from floati.c7_2_bundle import project_c7_2

        with tempfile.TemporaryDirectory() as directory:
            root = FloatiRoot.open_direct_home(Path(directory) / "alpha", create=True)
            trace = build_foc_orphan_trace(root)
            self._bind(trace, self._segments())
            raw = (root.tenant_home / "runs/events.jsonl").read_bytes()

            for hostile, expected_code in (
                (raw[:-1], "incomplete_frame"),
                (raw + b"\xff\n", "malformed_json"),
            ):
                with self.subTest(expected_code=expected_code):
                    with self.assertRaises(ProtocolRefusal) as raised:
                        project_c7_2(root, repository="owner/repo", raw_run_bytes=hostile)
                    self.assertEqual(expected_code, raised.exception.code)

    def test_hostile_v1_segment_field_vectors_refuse_typed_without_crashing(self) -> None:
        """Catches a v1 segment parser that leaks a type error or accepts hostile fields."""

        from floati.c7_2_bundle import project_c7_2

        with tempfile.TemporaryDirectory() as directory:
            root = FloatiRoot.open_direct_home(Path(directory) / "alpha", create=True)
            trace = build_foc_orphan_trace(root)
            self._bind(trace, self._segments())
            frames = decode_frames((root.tenant_home / "runs/events.jsonl").read_bytes())

            vectors = (
                ("segment_id", None, "segment_id_invalid"),
                ("segment_id", "seg-\x00", "segment_id_invalid"),
                ("segment_kind", ["resume"], "segment_kind_invalid"),
                ("segment_kind", "\u202eresume", "segment_kind_invalid"),
                (
                    "predecessor_segment_id",
                    {"unexpected": "object"},
                    "predecessor_segment_id_invalid",
                ),
                ("ordinal", True, "harness_segments_invalid"),
                ("harness_session_id", "worker-\x00", "harness_session_id_invalid"),
            )
            for field, value, expected_code in vectors:
                with self.subTest(field=field, value=repr(value)):
                    hostile_frames = copy.deepcopy(frames)
                    binding = next(
                        frame for frame in hostile_frames if frame.get("schema_version") == 1
                    )
                    binding["harness_segments"][1][field] = value
                    hostile = b"".join(encode_frame(frame) for frame in hostile_frames)
                    with self.assertRaises(ProtocolRefusal) as raised:
                        project_c7_2(root, repository="owner/repo", raw_run_bytes=hostile)
                    self.assertEqual(expected_code, raised.exception.code)

    def test_physical_segment_order_overrides_timestamp_testimony(self) -> None:
        """Catches a lineage selector that sorts bindings by timestamp instead of frame order."""

        from floati.c7_2_bundle import project_c7_2

        with tempfile.TemporaryDirectory() as directory:
            root = FloatiRoot.open_direct_home(Path(directory) / "alpha", create=True)
            trace = build_foc_orphan_trace(root)
            initial_id = "seg-" + uuid7_hex()
            first = self._bind(
                trace,
                [
                    {
                        "ordinal": 1,
                        "harness_session_id": "worker-" + uuid7_hex(),
                        "segment_id": initial_id,
                        "segment_kind": "initial",
                    }
                ],
            )
            second = self._bind(
                trace,
                [
                    {
                        "ordinal": 1,
                        "harness_session_id": "worker-" + uuid7_hex(),
                        "segment_id": "seg-" + uuid7_hex(),
                        "segment_kind": "resume",
                        "predecessor_segment_id": initial_id,
                    }
                ],
            )
            frames = decode_frames((root.tenant_home / "runs/events.jsonl").read_bytes())
            binding_positions = [
                position
                for position, frame in enumerate(frames)
                if frame.get("schema_version") == 1
            ]
            self.assertEqual(2, len(binding_positions))
            frames[binding_positions[0]]["timestamp"] = "2099-01-01T00:00:00.000Z"
            frames[binding_positions[1]]["timestamp"] = "2000-01-01T00:00:00.000Z"
            timestamp_inverted = b"".join(encode_frame(frame) for frame in frames)

            projection = project_c7_2(
                root,
                repository="owner/repo",
                raw_run_bytes=timestamp_inverted,
            )
            binding = projection["families"]["session_bindings"]["value"][
                trace.attempt_ids[0]
            ]
            self.assertEqual(second["id"], binding["binding_record_id"])
            self.assertEqual(
                first["id"],
                binding["supersession"]["superseded_binding_record_ids"][-1],
            )

            frames[binding_positions[0]], frames[binding_positions[1]] = (
                frames[binding_positions[1]],
                frames[binding_positions[0]],
            )
            physically_reordered = b"".join(encode_frame(frame) for frame in frames)
            with self.assertRaises(ProtocolRefusal) as raised:
                project_c7_2(
                    root,
                    repository="owner/repo",
                    raw_run_bytes=physically_reordered,
                )
            self.assertEqual("harness_predecessor_not_prior", raised.exception.code)

    def test_bounded_capture_survives_a_concurrent_post_read_segment_append(self) -> None:
        """Catches a materializer rereading a changing run ledger after its first capture."""

        from floati import c7_bundle
        from floati.c7_2_bundle import build_c7_2_bundle, read_c7_2_bundle

        with tempfile.TemporaryDirectory() as directory:
            root = FloatiRoot.open_direct_home(Path(directory) / "alpha", create=True)
            trace = build_foc_orphan_trace(root)
            initial_id = "seg-" + uuid7_hex()
            self._bind(
                trace,
                [
                    {
                        "ordinal": 1,
                        "harness_session_id": "worker-" + uuid7_hex(),
                        "segment_id": initial_id,
                        "segment_kind": "initial",
                    }
                ],
            )
            before_append = (root.tenant_home / "runs/events.jsonl").read_bytes()
            release_writer = Event()
            writer_finished = Event()
            writer_errors: list[BaseException] = []

            def append_after_capture_starts() -> None:
                if not release_writer.wait(timeout=5):
                    writer_errors.append(TimeoutError("capture never released concurrent writer"))
                    writer_finished.set()
                    return
                try:
                    self._bind(
                        trace,
                        [
                            {
                                "ordinal": 1,
                                "harness_session_id": "worker-" + uuid7_hex(),
                                "segment_id": "seg-" + uuid7_hex(),
                                "segment_kind": "resume",
                                "predecessor_segment_id": initial_id,
                            }
                        ],
                    )
                except Exception as exc:
                    writer_errors.append(exc)
                finally:
                    writer_finished.set()

            writer = Thread(target=append_after_capture_starts)
            original_read_source = c7_bundle._read_source
            run_capture_started = False

            def capture_runs_then_release_writer(read_root: FloatiRoot, relative: str) -> bytes:
                nonlocal run_capture_started
                captured = original_read_source(read_root, relative)
                if relative == c7_bundle.RUN_LEDGER and not run_capture_started:
                    run_capture_started = True
                    release_writer.set()
                    self.assertTrue(writer_finished.wait(timeout=5))
                return captured

            destination = Path(directory) / "c7.2"
            writer.start()
            try:
                with patch.object(
                    c7_bundle,
                    "_read_source",
                    side_effect=capture_runs_then_release_writer,
                ):
                    build_c7_2_bundle(root, destination, repository="owner/repo")
            finally:
                release_writer.set()
                writer.join(timeout=5)

            self.assertTrue(run_capture_started)
            self.assertFalse(writer.is_alive())
            self.assertEqual([], writer_errors)
            self.assertNotEqual(
                before_append,
                (root.tenant_home / "runs/events.jsonl").read_bytes(),
            )
            captured = (destination / "raw/runs/events.jsonl").read_bytes()
            self.assertEqual(before_append, captured)
            self.assertEqual(
                hashlib.sha256(captured).hexdigest(),
                read_c7_2_bundle(destination)["projection"]["raw_source_digest"],
            )

    def test_reader_refuses_raw_tamper_reprojection_drift_and_invalid_package(self) -> None:
        from floati.c7_2_bundle import (
            build_c7_2_bundle,
            read_c7_2_bundle,
            semantic_digest,
            self_digest,
        )

        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = FloatiRoot.open_direct_home(base / "alpha", create=True)
            trace = build_foc_orphan_trace(root)
            self._bind(trace, self._segments())

            raw_digest_snapshot = base / "raw-digest"
            build_c7_2_bundle(root, raw_digest_snapshot, repository="owner/repo")
            raw_path = raw_digest_snapshot / "raw/runs/events.jsonl"
            raw_path.write_bytes(raw_path.read_bytes().replace(b'"timestamp":"2026', b'"timestamp":"2099', 1))
            with self.assertRaises(ProtocolRefusal) as raised:
                read_c7_2_bundle(raw_digest_snapshot)
            self.assertEqual("c7_raw_source_digest_invalid", raised.exception.code)

            reprojection_snapshot = base / "reprojection"
            build_c7_2_bundle(root, reprojection_snapshot, repository="owner/repo")
            projection_path = reprojection_snapshot / "families/run-projection.json"
            projection = json.loads(projection_path.read_text(encoding="utf-8"))
            binding = projection["families"]["session_bindings"]["value"][trace.attempt_ids[0]]
            binding["segments"][-1]["segment_kind"]["value"] = "fork"
            projection["semantic_digest"] = semantic_digest(projection)
            projection["self_digest"] = self_digest(projection)
            projection_path.write_bytes(
                json.dumps(projection, sort_keys=True, separators=(",", ":")).encode("utf-8")
                + b"\n"
            )
            with self.assertRaises(ProtocolRefusal) as raised:
                read_c7_2_bundle(reprojection_snapshot)
            self.assertEqual("c7_projection_reprojection_invalid", raised.exception.code)

            package_snapshot = base / "package"
            build_c7_2_bundle(root, package_snapshot, repository="owner/repo")
            index_path = package_snapshot / "bundle-index.json"
            index = json.loads(index_path.read_text(encoding="utf-8"))
            index["reader_upgrade"]["unknown"] = "best_effort"
            index_path.write_text(json.dumps(index), encoding="utf-8")
            with self.assertRaises(ProtocolRefusal) as raised:
                read_c7_2_bundle(package_snapshot)
            self.assertEqual("c7_upgrade_rule_invalid", raised.exception.code)


if __name__ == "__main__":
    unittest.main()

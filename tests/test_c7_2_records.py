from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from floati.cancellation import CancellationCoordinator
from floati.errors import IntegrityFailure, ProtocolRefusal
from floati.framing import decode_frames, encode_frame
from floati.ids import uuid7_hex
from floati.root import FloatiRoot
from tests.hm3i_gauntlet_fixtures import (
    NOW,
    build_foc_orphan_trace,
    build_retry_stale_trace,
)


class C7_2RecordSeamTests(unittest.TestCase):
    @staticmethod
    def _segments() -> list[dict[str, object]]:
        root_segment_id = "seg-" + uuid7_hex()
        return [
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
        ]

    @staticmethod
    def _bind(trace: object, attempt_id: str, segments: list[dict[str, object]], **kwargs: object) -> dict[str, object]:
        return CancellationCoordinator(trace.ledger).bind_harness_session(
            trace.run_id,
            trace.item_ids[0],
            attempt_id,
            claim_id="claim-" + uuid7_hex(),
            lease_id="lease-" + uuid7_hex(),
            worker_session_id="worker-" + uuid7_hex(),
            harness_segments=segments,
            now=NOW,
            **kwargs,
        )

    def test_writer_defaults_to_v0_and_emits_v1_only_when_requested(self) -> None:
        """Catches legacy writer calls gaining v1 fields or explicit v1 requests emitting a v0 row."""
        with tempfile.TemporaryDirectory() as directory:
            root = FloatiRoot.open_direct_home(Path(directory) / "alpha", create=True)
            trace = build_foc_orphan_trace(root)
            legacy = self._bind(
                trace,
                trace.attempt_ids[0],
                [{"ordinal": 1, "harness_session_id": "worker-" + uuid7_hex()}],
            )
            v1 = self._bind(trace, trace.attempt_ids[0], self._segments(), schema_version=1)

            self.assertEqual(0, legacy["schema_version"])
            self.assertEqual(
                {"ordinal", "harness_session_id"}, set(legacy["harness_segments"][0])
            )
            self.assertEqual(1, v1["schema_version"])
            self.assertEqual("initial", v1["harness_segments"][0]["segment_kind"])
            self.assertNotIn("predecessor_segment_id", v1["harness_segments"][0])

    def test_replay_uses_physical_segment_position_not_timestamps_or_identifiers(self) -> None:
        """Catches a forward same-record predecessor being accepted or position inferred from metadata."""
        with tempfile.TemporaryDirectory() as directory:
            root = FloatiRoot.open_direct_home(Path(directory) / "alpha", create=True)
            trace = build_foc_orphan_trace(root)
            bound = self._bind(trace, trace.attempt_ids[0], self._segments(), schema_version=1)
            segments = bound["harness_segments"]
            path = root.resolve_relative(trace.ledger.relative_path)
            frames = decode_frames(path.read_bytes())
            persisted = next(frame for frame in frames if frame["id"] == bound["id"])
            persisted["harness_segments"][0].update(
                segment_kind="fork",
                predecessor_segment_id=segments[1]["segment_id"],
            )
            path.write_bytes(b"".join(encode_frame(frame) for frame in frames))

            with self.assertRaises(IntegrityFailure) as replay:
                trace.ledger.project()
            self.assertEqual("harness_predecessor_not_prior", replay.exception.code)

    def test_v1_ordinals_require_exact_non_boolean_integers_on_append_and_replay(self) -> None:
        """Catches Python equality admitting bool or float ordinals outside the v1 schema."""

        for hostile in (True, 1.0):
            with self.subTest(hostile=hostile), tempfile.TemporaryDirectory() as directory:
                root = FloatiRoot.open_direct_home(Path(directory) / "alpha", create=True)
                trace = build_foc_orphan_trace(root)
                path = root.resolve_relative(trace.ledger.relative_path)
                before = path.read_bytes()
                segments = self._segments()
                segments[0]["ordinal"] = hostile
                with self.assertRaises(ProtocolRefusal) as candidate:
                    self._bind(
                        trace,
                        trace.attempt_ids[0],
                        segments,
                        schema_version=1,
                    )
                self.assertEqual("harness_segments_invalid", candidate.exception.code)
                self.assertEqual(before, path.read_bytes())

        with tempfile.TemporaryDirectory() as directory:
            root = FloatiRoot.open_direct_home(Path(directory) / "alpha", create=True)
            trace = build_foc_orphan_trace(root)
            bound = self._bind(
                trace,
                trace.attempt_ids[0],
                self._segments(),
                schema_version=1,
            )
            path = root.resolve_relative(trace.ledger.relative_path)
            frames = decode_frames(path.read_bytes())
            persisted = next(frame for frame in frames if frame["id"] == bound["id"])
            persisted["harness_segments"][0]["ordinal"] = True
            path.write_bytes(b"".join(encode_frame(frame) for frame in frames))

            with self.assertRaises(IntegrityFailure) as replay:
                trace.ledger.project()
            self.assertEqual("harness_segments_invalid", replay.exception.code)

    def test_lineage_rejects_duplicate_segment_ids_and_other_attempt_predecessors(self) -> None:
        """Catches a segment ID reused in one attempt lineage or a transition borrowing another attempt's root."""
        with tempfile.TemporaryDirectory() as directory:
            root = FloatiRoot.open_direct_home(Path(directory) / "alpha", create=True)
            trace = build_foc_orphan_trace(root)
            first = self._bind(trace, trace.attempt_ids[0], self._segments(), schema_version=1)
            duplicate_id = first["harness_segments"][0]["segment_id"]
            with self.assertRaises(ProtocolRefusal) as duplicate:
                self._bind(
                    trace,
                    trace.attempt_ids[0],
                    [
                        {
                            "ordinal": 1,
                            "harness_session_id": "worker-" + uuid7_hex(),
                            "segment_id": duplicate_id,
                            "segment_kind": "initial",
                        }
                    ],
                    schema_version=1,
                )
            self.assertEqual("harness_segment_id_duplicate", duplicate.exception.code)

        with tempfile.TemporaryDirectory() as directory:
            root = FloatiRoot.open_direct_home(Path(directory) / "alpha", create=True)
            trace = build_retry_stale_trace(root)
            first_attempt, second_attempt = trace.attempt_ids
            first = self._bind(
                trace,
                first_attempt,
                [
                    {
                        "ordinal": 1,
                        "harness_session_id": "worker-" + uuid7_hex(),
                        "segment_id": "seg-" + uuid7_hex(),
                        "segment_kind": "initial",
                    }
                ],
                schema_version=1,
            )
            with self.assertRaises(ProtocolRefusal) as cross_attempt:
                self._bind(
                    trace,
                    second_attempt,
                    [
                        {
                            "ordinal": 1,
                            "harness_session_id": "worker-" + uuid7_hex(),
                            "segment_id": "seg-" + uuid7_hex(),
                            "segment_kind": "handoff",
                            "predecessor_segment_id": first["harness_segments"][0]["segment_id"],
                        }
                    ],
                    schema_version=1,
                )
            self.assertEqual(
                "harness_predecessor_attempt_mismatch", cross_attempt.exception.code
            )


if __name__ == "__main__":
    unittest.main()

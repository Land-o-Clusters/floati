from __future__ import annotations

from copy import deepcopy
import tempfile
import unittest
from pathlib import Path

from floati.cancellation import CancellationCoordinator
from floati.errors import IntegrityFailure, ProtocolRefusal
from floati.ids import uuid7_hex
from floati.root import FloatiRoot
from floati.runtruth import RunProjection
from floati.workers import WorkerReceipts
from tests.hm3i_gauntlet_fixtures import NOW, build_foc_orphan_trace, build_success_trace


class RunProjectionIncrementalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = FloatiRoot.open_direct_home(Path(self.temp.name) / "alpha", create=True)
        self.trace = build_foc_orphan_trace(self.root)
        self.records = [dict(record) for record in self.trace.records]
        self.receipts = WorkerReceipts(self.root).records()

    def _incremental(self, *, retain_record: bool = True, track_record_ids: bool = True) -> RunProjection:
        projection = RunProjection.empty(self.receipts, track_record_ids=track_record_ids)
        for position, record in enumerate(self.records, start=1):
            projection.apply(record, physical_position=position, integrity=True, retain_record=retain_record)
        return projection

    def test_incremental_replay_matches_compatibility_projection(self) -> None:
        """Catches lifecycle state being lost or changed while moving replay into apply."""
        expected = RunProjection.from_records(self.records, self.receipts)
        actual = self._incremental()
        self.assertEqual(expected.run(self.trace.run_id), actual.run(self.trace.run_id))
        self.assertEqual(expected.edges(self.trace.run_id), actual.edges(self.trace.run_id))
        self.assertEqual(expected.item_outcomes(self.trace.run_id), actual.item_outcomes(self.trace.run_id))

    def test_compatibility_replay_projects_a_terminal_run(self) -> None:
        """Catches apply retaining the old classmethod receiver at run-terminal replay."""
        terminal_root = FloatiRoot.open_direct_home(Path(self.temp.name) / "terminal", create=True)
        trace = build_success_trace(terminal_root)
        projection = RunProjection.from_records(trace.records, WorkerReceipts(terminal_root).records())
        self.assertEqual("succeeded", projection.run_outcome(trace.run_id))
        self.assertEqual("succeeded", projection.run(trace.run_id)["terminal"]["outcome"])

    def test_bounded_history_preserves_semantics_and_joins(self) -> None:
        """Catches bounded replay dropping lifecycle joins or including raw history in its digest."""
        retained = self._incremental()
        bounded = self._incremental(retain_record=False)
        self.assertEqual(retained.semantic_digest(), bounded.semantic_digest())
        self.assertEqual([], bounded.run(self.trace.run_id)["records"])
        self.assertEqual(retained.item_outcomes(self.trace.run_id), bounded.item_outcomes(self.trace.run_id))
        self.assertEqual(retained.run(self.trace.run_id)["harness_sessions"], bounded.run(self.trace.run_id)["harness_sessions"])

    def test_default_duplicate_screening_and_opt_out_state(self) -> None:
        """Catches duplicate-ID replay becoming accepted or opt-out retaining unbounded global IDs."""
        projection = RunProjection.empty()
        projection.apply(self.records[0], physical_position=1, integrity=False)
        with self.assertRaises(ProtocolRefusal) as duplicate:
            projection.apply(self.records[0], physical_position=2, integrity=False)
        self.assertEqual("duplicate_record_id", duplicate.exception.code)

        untracked = RunProjection.empty(track_record_ids=False)
        untracked.apply(self.records[0], physical_position=1, integrity=False)
        self.assertEqual(set(), untracked._seen_ids)

    def test_positions_refuse_with_the_governed_class_and_code(self) -> None:
        """Catches zero, repeated, backward, or gapped physical positions bypassing replay ordering."""
        for integrity, error in ((True, IntegrityFailure), (False, ProtocolRefusal)):
            for position in (0, 2):
                with self.subTest(integrity=integrity, position=position):
                    projection = RunProjection.empty()
                    with self.assertRaises(error) as raised:
                        projection.apply({}, physical_position=position, integrity=integrity)
                    self.assertEqual("projection_position_invalid", raised.exception.code)
            projection = RunProjection.empty()
            projection.apply(self.records[0], physical_position=1, integrity=integrity)
            for position in (1, 0):
                with self.subTest(integrity=integrity, position=position):
                    with self.assertRaises(error) as raised:
                        projection.apply({}, physical_position=position, integrity=integrity)
                    self.assertEqual("projection_position_invalid", raised.exception.code)

    def test_harness_segments_use_supplied_physical_positions(self) -> None:
        """Catches harness predecessor ordering being inferred from a local loop instead of apply positions."""
        attempt_id = self.trace.attempt_ids[0]
        coordinator = CancellationCoordinator(self.trace.ledger)
        first_id, second_id = "seg-" + uuid7_hex(), "seg-" + uuid7_hex()
        coordinator.bind_harness_session(
            self.trace.run_id, self.trace.item_ids[0], attempt_id,
            claim_id="claim-" + uuid7_hex(), lease_id="lease-" + uuid7_hex(),
            worker_session_id="worker-" + uuid7_hex(), schema_version=1,
            harness_segments=[{"ordinal": 1, "harness_session_id": "worker-" + uuid7_hex(), "segment_id": first_id, "segment_kind": "initial"}], now=NOW,
        )
        coordinator.bind_harness_session(
            self.trace.run_id, self.trace.item_ids[0], attempt_id,
            claim_id="claim-" + uuid7_hex(), lease_id="lease-" + uuid7_hex(),
            worker_session_id="worker-" + uuid7_hex(), schema_version=1,
            harness_segments=[{"ordinal": 1, "harness_session_id": "worker-" + uuid7_hex(), "segment_id": second_id, "segment_kind": "resume", "predecessor_segment_id": first_id}], now=NOW,
        )
        records = [dict(record) for record in self.trace.ledger.records()]
        projection = RunProjection.empty(WorkerReceipts(self.root).records())
        for position, record in enumerate(records, start=1):
            projection.apply(record, physical_position=position, integrity=True)

        invalid = deepcopy(records)
        first = next(row for row in invalid if row["id"] == records[-2]["id"])
        first["harness_segments"][0].update(segment_kind="resume", predecessor_segment_id=second_id)
        projection = RunProjection.empty(WorkerReceipts(self.root).records())
        with self.assertRaises(IntegrityFailure) as raised:
            for position, record in enumerate(invalid, start=1):
                projection.apply(record, physical_position=position, integrity=True)
        self.assertEqual("harness_predecessor_missing", raised.exception.code)


if __name__ == "__main__":
    unittest.main()

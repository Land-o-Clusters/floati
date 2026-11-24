"""Recorded native detachment remains proof when every process sample misses.

This is artifact replay, not a second measurement of live Git availability.
The separate G1 gate retains the fresh native fixture and its owned cleanup.
"""
import hashlib
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from tests import g1_trace_fixture
from tests import test_gate_z2_of_fl_1 as gate
from tests.projected_fixture_pins import expected_fixture_sha256


class PositiveWitnessRaceRegression(unittest.TestCase):
    def test_g1_still_proves_the_mechanism_when_every_process_poll_misses(self):
        path = Path(__file__).resolve().parent / "fixtures" / "g1-witness-artifact" / "native-completed.json"
        raw = path.read_bytes()
        self.assertLess(len(raw), 128 * 1024)
        self.assertEqual(expected_fixture_sha256(path),
                         hashlib.sha256(raw).hexdigest())
        artifact = json.loads(raw)
        self.assertEqual("recorded_native_trace_replay", artifact["provenance"]["kind"])
        rows = artifact["trace_rows"]
        witness = g1_trace_fixture.detached_witness(rows)
        self.assertTrue(g1_trace_fixture._finished(rows))
        self.assertTrue(witness["completed_repack_sids"])
        case = gate.GateForcedMaintenanceLeavesADetachedChild(
            "test_g1_pre_fix_commit_leaves_a_detached_git_child_after_return")
        # Supply the parsed native artifact at the existing fixture boundary.
        # An old sampled G1 still fails under this always-empty observer.
        with patch.object(gate, "_maintenance_children_under", return_value=[]) as observer,\
                patch.object(g1_trace_fixture, "run_positive_fixture", return_value=witness) as fixture:
            case.debug()
        fixture.assert_called_once_with()
        observer.assert_not_called()

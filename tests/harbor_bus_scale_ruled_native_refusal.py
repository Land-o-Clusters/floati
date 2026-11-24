"""Actual-main routing controls for the one ruled native functional RED.

Design: a collected native refusal is a separate closed result, never a
successful arm or a ratio. Only the witnessed EVENTS/10k seat-teardown
record_too_large outcome on d66 may name F2-SEAT-TEARDOWN-SCALE-1 and allow
the remaining cells to run. Both balanced scale calls still occur, the
positive 1x and refused 10x raw evidence survive, and every other case is
classified. Unruled or incomplete testimony stops the ordinary main path.

These tests replace expensive preparation/observation boundaries with
explicit synthetic admission data. They execute actual main, admission,
matrix classification, archiving and cleanup; they measure no performance
and authenticate no native execution. The literal refusal artifact below
is copied from the banked genuine host boundary, whose provenance remains
separate. Native collector witnessing is an independent integration gate.
"""
from __future__ import annotations

import copy
from tests.bus_scale_test_profile import clone_fixture
from contextlib import redirect_stdout
import hashlib
import io
import json
from pathlib import Path
import shutil
import tarfile
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from tests import bus_scale_seeds
from tests.temp_roots import REAL_TEMP_ROOT
from tests.harbor_bus_scale_attention_retry import (
    COORDINATOR, ROOT, SyntheticObservationBoundaries, objects, subject,
)


PRODUCT = "d66a94daa357bd392b3040c55cbe21e20ac884a3"
DEFECT = "F2-SEAT-TEARDOWN-SCALE-1"
ENTRY = ROOT / "tests/fixtures/bus_scale_census/bus-scale-native-entry-2026-10-09.py"
# Retained actual host artifact: .../bus-scale-seat-teardown-boundary-qeibc3vr/artifact.json
# SHA256 1c22f1399cdc9aed1f07489a73609ed12dcac0b3840aff55b4aeb36b8927928e.
NATIVE_ARTIFACT = {
    "artifact_version": 0, "command": "seat", "schema_version": 1, "status": "refused",
    "evidence": {"code": "record_too_large", "detail": "record is 392728 bytes; maximum is 65536",
                 "remedy": {"kind": "none", "why": "no action was named for this refusal"}},
}
SOURCE_PATHS = (
    "tests/harbor_bus_scale_ruled_native_refusal.py", "tests/bus_scale_workloads.py",
    "tests/bus_scale_commands.py", "tests/fixtures/bus_scale_census/census-bus-scale-2026-10-09.py",
    "tests/fixtures/bus_scale_census/bus-scale-expanded-observation-2026-10-15.py",
    "tests/fixtures/bus_scale_census/bus-scale-native-entry-2026-10-09.py",
)


def source_hashes():
    return {relative: hashlib.sha256((ROOT / relative).read_bytes()).hexdigest()
            for relative in SOURCE_PATHS}


class RuledRefusalBoundaries(SyntheticObservationBoundaries):
    def __init__(self, test, mutation=None):
        super().__init__(test, persistent=False)
        self.mutation = mutation

    def observe(self, case, scale, home, relative, place):
        scale = int(scale)
        family = place.relative_to(self.base / "runs").parts[0]
        raw = (Path(home) / relative).read_bytes()
        seed_digest = hashlib.sha256(raw).hexdigest()
        arm = clone_fixture(self.observations[case]["arms"][str(scale) + "x"])
        arm.update(records=1000 * scale, bytes=len(raw), seed_sha256=seed_digest,
                   clone_sha256=seed_digest)
        target_family = "registry" if self.mutation == "wrong-family" else "events"
        target_case = "presence-report" if self.mutation == "wrong-case" else "seat-teardown"
        failed = family == target_family and case == target_case and scale == 10
        if failed:
            native_bytes = (json.dumps(NATIVE_ARTIFACT, sort_keys=True, indent=2) + "\n").encode()
            self.test.assertEqual("1c22f1399cdc9aed1f07489a73609ed12dcac0b3840aff55b4aeb36b8927928e",
                                  hashlib.sha256(native_bytes).hexdigest())
            result = {
                "disposition": "functional_red", "row": DEFECT,
                "native": {"exit_code": 20, "artifact": clone_fixture(NATIVE_ARTIFACT),
                           "diagnostic_artifact": clone_fixture(NATIVE_ARTIFACT)},
                "records": 10000, "bytes": len(raw), "seed_sha256": seed_digest,
                "clone_sha256": seed_digest, "child_collected": True, "raw_sha256": "a" * 64,
                "available_metrics": {"cpu_seconds": 8.0, "bytes_read": 800,
                                      "max_ledger_lock_seconds": 0.008},
                "source": {"product_sha": PRODUCT, "entry_sha256": hashlib.sha256(ENTRY.read_bytes()).hexdigest(),
                           "stdout_sha256": {mode: hashlib.sha256(native_bytes).hexdigest()
                                             for mode in ("cpu", "diagnostic")}},
            }
            if self.mutation == "wrong-row": result["row"] = "UNRULED-DEFECT"
            elif self.mutation == "wrong-code":
                for field in ("artifact", "diagnostic_artifact"):
                    result["native"][field]["evidence"]["code"] = "seat_teardown_activity_unknown"
            elif self.mutation == "wrong-product": result["source"]["product_sha"] = "0" * 40
            elif self.mutation == "malformed-source": result["source"]["entry_sha256"] = "not-a-digest"
            elif self.mutation == "uncollected": result["child_collected"] = False
            elif self.mutation == "extra-field": result["unreviewed"] = True
            elif self.mutation == "missing-artifact": del result["native"]["diagnostic_artifact"]
            elif self.mutation == "negative-metric": result["available_metrics"]["cpu_seconds"] = -1.0
            elif self.mutation == "generic": raise ValueError("unruled ordinary observation failure")
            for mode, field in (("cpu", "artifact"), ("diagnostic", "diagnostic_artifact")):
                mode_bank = place / mode
                mode_bank.mkdir(mode=0o700)
                mode_bytes = (json.dumps(result["native"].get(field, NATIVE_ARTIFACT), sort_keys=True, indent=2) + "\n").encode()
                (mode_bank / "stdout.txt").write_bytes(mode_bytes)
                result["source"]["stdout_sha256"][mode] = hashlib.sha256(mode_bytes).hexdigest()
        else:
            result = arm
        if case == "wait-idle":
            for mode in ("cpu", "diagnostic"):
                detail = place / mode / "detail.json"
                detail.parent.mkdir(mode=0o700)
                detail.write_text(json.dumps(self.phase_details[str(scale) + "x"][mode], sort_keys=True) + "\n")
        payload = {"synthetic_admission_only": True, "family": family, "case": case,
                   "scale": scale, "result": clone_fixture(result)}
        raw_path = place / "synthetic-observation.json"
        raw_path.write_text(json.dumps(payload, sort_keys=True) + "\n")
        result["raw_sha256"] = hashlib.sha256(raw_path.read_bytes()).hexdigest()
        self.calls.append({"family": family, "case": case, "scale": scale, "failed": failed,
                           "raw_coordinate": place.relative_to(self.base).as_posix(),
                           "result": clone_fixture(result)})
        return result


class RuledNativeRefusalContinuationTests(unittest.TestCase):
    def run_firstpass(self, mutation=None, *, mode="first-pass"):
        coordinator = subject()
        # Route retained diagnostic evidence into the runner's declared bank.
        # The recorded coordinator keeps its original platform default.
        coordinator.TEMP = Path(REAL_TEMP_ROOT)
        temporary = tempfile.TemporaryDirectory(prefix="bus-scale-census-2026-10-09-ruled-refusal-",
                                                 dir=coordinator.TEMP)
        self.addCleanup(temporary.cleanup)
        output = Path(temporary.name).resolve()
        boundaries = RuledRefusalBoundaries(self, mutation)
        self.assertEqual(70, len(boundaries.scope["cases"]), "real d66 parser population must be restored first")
        initial = source_hashes()
        code = failure = None
        with mock.patch.object(bus_scale_seeds, "make_seed_pair", boundaries.seed_pair),\
                mock.patch("tests.bus_scale_workloads.mcp_tool_bindings", return_value={}),\
                mock.patch.object(coordinator, "prepare", return_value=None),\
                mock.patch.object(coordinator, "workload_seed", boundaries.workload_seed),\
                mock.patch.object(coordinator, "observation", boundaries.original),\
                mock.patch.object(coordinator, "expanded_adapter", return_value=SimpleNamespace(expanded_observation=boundaries.expanded)),\
                mock.patch("sys.argv", [str(COORDINATOR), mode, str(output)]),\
                redirect_stdout(io.StringIO()):
            try:
                code = coordinator.main()
            except (ValueError, RuntimeError) as error:
                failure = error
        record = json.loads((output / "census.json").read_bytes())
        self.assertTrue(record["scratch_removed"])
        self.assertFalse(boundaries.base.exists())
        with tarfile.open(output / "census-raw.tar.gz", "r:gz") as archive:
            raw = {member.name: archive.extractfile(member).read() for member in archive if member.isfile()}
        for call in boundaries.calls:
            coordinate = call["raw_coordinate"] + "/synthetic-observation.json"
            self.assertEqual(call["result"]["raw_sha256"], hashlib.sha256(raw[coordinate]).hexdigest())
        retained = Path(tempfile.mkdtemp(prefix="bus-scale-ruled-refusal-routing-", dir=coordinator.TEMP))
        shutil.copytree(output, retained / "output", copy_function=shutil.copy2)
        (retained / "routing-control.json").write_text(json.dumps({
            "synthetic_admission_only": True, "mode": mode, "mutation": mutation, "product_source": PRODUCT,
            "code": code, "failure_type": None if failure is None else type(failure).__name__,
            "failure_detail": None if failure is None else str(failure), "calls": boundaries.calls,
            "source_before": initial, "source_after": source_hashes(), "original_output": str(output),
        }, sort_keys=True) + "\n")
        print("synthetic ruled-native-refusal routing retained: " + str(retained), flush=True)
        self.assertEqual(initial, source_hashes())
        return coordinator, boundaries, record, code, failure, raw

    def test_actual_main_continues_every_other_cell_and_preserves_ruled_refusal_without_ratio(self):
        coordinator, boundaries, record, code, failure, raw = self.run_firstpass()
        self.assertIsNone(failure, "ruled collected refusal must remain terminal functional RED and continue")
        self.assertEqual(0, code)
        self.assertEqual("first_pass_complete_with_functional_reds", record["status"])
        self.assertEqual(1120, len(boundaries.calls), "four families, two balanced pairs, seventy cases, both scales")
        teardown = [call for call in boundaries.calls if call["case"] == "seat-teardown"]
        for family in ("events", "registry", "wait-exhaustion", "deliveries"):
            calls = [call for call in teardown if call["family"] == family]
            self.assertEqual([1, 10, 10, 1], [call["scale"] for call in calls], "no skip/retry/reorder for native refusal")
        failed = [call for call in teardown if call["failed"]]
        self.assertEqual(2, len(failed), "only actual EVENTS/10k cells carry this native refusal")
        functional = record["functional_reds"]
        self.assertEqual(1, len(functional), "one named functional RED must cover the two refused cells")
        self.assertEqual(("red", DEFECT, "events", "seat-teardown"),
                         tuple(functional[0][key] for key in ("classification", "row", "family", "case")))
        self.assertNotIn("metrics", functional[0], "a functional refusal has no ratio classification")
        self.assertEqual([{ "pair_index": index, "scale": "10x", "raw_sha256": call["result"]["raw_sha256"]}
                          for index, call in enumerate(failed)], functional[0]["failed_cells"])
        retained_objects = list(objects(record))
        for call in failed:
            self.assertIn(call["result"], retained_objects, "terminal functional RED/source/available costs disappeared")
            for mode in ("cpu", "diagnostic"):
                stdout = raw[call["raw_coordinate"] + "/" + mode + "/stdout.txt"]
                self.assertEqual(call["result"]["source"]["stdout_sha256"][mode], hashlib.sha256(stdout).hexdigest())
        for call in teardown:
            if call["family"] == "events" and call["scale"] == 1:
                self.assertIn(call["result"], retained_objects, "positive 1x evidence must accompany functional RED")
        for node in retained_objects:
            if node.get("selected_work_equal") is True:
                self.assertFalse(any(child.get("disposition") == "functional_red" for child in objects(node)))
        comparisons = record["comparisons"]
        coordinates = {(row["family"], row["case"]) for row in comparisons}
        expected = {(family, case) for family in ("events", "registry", "wait-exhaustion", "deliveries")
                    for case in boundaries.scope["cases"]} - {("events", "seat-teardown")}
        self.assertEqual(279, len(comparisons))
        self.assertEqual(expected, coordinates, "normal cells must all be classified; failed pair has no synthetic ratio")
        for row in comparisons:
            if row["case"] == "seat-teardown":
                self.assertNotEqual("events", row["family"])
                self.assertEqual([1.0, 1.0], row["metrics"]["cpu_seconds"]["pair_ratios"])

    def test_smoke_refuses_functional_failure_and_retains_returned_arms_without_calibration(self):
        _coordinator, boundaries, record, _code, failure, raw = self.run_firstpass(mode="smoke")
        self.assertIsNotNone(failure, "smoke calibration requires completed native work, not a ruled refusal")
        self.assertEqual("incomplete", record["status"])
        self.assertNotIn("comparisons", record)
        failed = [call for call in boundaries.calls if call["failed"]]
        self.assertEqual(1, len(failed), "smoke must stop at its first ruled refused native cell")
        retained_objects = list(objects(record))
        self.assertIn(failed[0]["result"], retained_objects, "smoke discarded the returned refused cell")
        positive = next(call for call in boundaries.calls if call["family"] == "events"
                        and call["case"] == "seat-teardown" and call["scale"] == 1)
        self.assertIn(positive["result"], retained_objects, "smoke discarded its completed 1x arm")
        for mode in ("cpu", "diagnostic"):
            stdout = raw[failed[0]["raw_coordinate"] + "/" + mode + "/stdout.txt"]
            self.assertEqual(failed[0]["result"]["source"]["stdout_sha256"][mode],
                             hashlib.sha256(stdout).hexdigest())

    def test_unruled_malformed_policy_source_and_uncollected_results_stop_without_continuation(self):
        for mutation in ("wrong-row", "wrong-code", "wrong-product", "malformed-source", "uncollected",
                         "extra-field", "missing-artifact", "negative-metric", "wrong-family", "wrong-case", "generic"):
            with self.subTest(mutation=mutation):
                _coordinator, boundaries, record, _code, failure, _raw = self.run_firstpass(mutation)
                self.assertIsNotNone(failure, "only the exact ruled collected native outcome may continue")
                self.assertEqual("incomplete", record["status"])
                self.assertNotIn("comparisons", record)
                self.assertLess(len(boundaries.calls), 1120)


if __name__ == "__main__":
    unittest.main()

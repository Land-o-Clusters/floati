"""Native correctness controls for teardown admission and mutation boundaries.

The empty directory below is an explicitly planted post-collection corruption,
not native work. The registry-byte control measures only argument preparation,
after a genuine owned native entry has already completed on the same seed.
Neither control is performance evidence.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from tests.bus_scale_test_profile import StableSourceFixture

from floati.root import FloatiRoot
from floati.seat_teardown import completed_teardown_for_session
from tests.bus_scale_test_profile import banked_send_repository
from tests.bus_scale_case_seeds import inventory
from tests.bus_scale_test_profile import seed_counts
from tests.bus_scale_io import IOProbe
from tests.harbor_bus_scale_expanded_observation import ADAPTER, ENTRY, RecordingOwner, load
from tests.test_bus_scale_seat_teardown_boundary import ACTOR, SESSION, SOURCE, _inventory, _json
from tests.temp_roots import REAL_TEMP_ROOT, _PLATFORM_TEMP_ROOT


CASE = "seat-teardown"
CENSUS = SOURCE / "tests/fixtures/bus_scale_census/census-bus-scale-2026-10-09.py"
HELPER = SOURCE / "tests/fixtures/bus_scale_census/bus-scale-routine-teardown-2026-10-15.py"
SOURCE_FILES = (Path(__file__).resolve(), CENSUS, HELPER, ENTRY, ADAPTER,
                SOURCE / "tests/bus_scale_case_seeds.py", SOURCE / "tests/bus_scale_io.py",
                SOURCE / "floati/seat_teardown.py", SOURCE / "floati/seat_teardown_activity.py")


class EmptyDirectoryOwner(RecordingOwner):
    """Delegate the real child and collect it before planting private state."""

    def __init__(self, delegate, seed, helper, context, retained):
        super().__init__(delegate, seed)
        self.helper, self.context, self.retained = helper, context, retained
        self.raw_before = {}
        self.injections = []

    def launch_owned_child(self, argv, **kwargs):
        child = super().launch_owned_child(argv, **kwargs)
        self.raw_before[child.pid] = copy.deepcopy(self.expected_seed)
        return child

    def collect_owned_child(self, child, **kwargs):
        result = super().collect_owned_child(child, **kwargs)
        if result.get("child_collected") is not True or result.get("exit_code") != 0:
            raise AssertionError("mutation control requires an actually collected successful native child")
        argv, pid, home = next(row for row in self.launches if row[1] == child.pid)
        mode = argv[3]
        retained = self.retained / ("post-collected-" + mode)
        retained.mkdir(mode=0o700)
        output = home.parent / ("expanded-" + mode + "-stdout.txt")
        detail = home.parent / ("expanded-" + mode + "-detail.json")
        artifact = json.loads(output.read_bytes())
        native_detail = json.loads(detail.read_bytes())
        witnessed = self.helper.witness(CASE, home, self.raw_before[pid], artifact,
                                        self.context, native_detail)
        if witnessed != {"exit_code": 0, "status": "ok", "effect": {
                "kind": "seat_teardown_completed", "count": 1}}:
            raise AssertionError("post-collection corruption lacks its genuine native completed witness")
        _json(retained / "native-witness.json", witnessed)
        shutil.copy2(output, retained / "native-stdout.txt")
        shutil.copy2(detail, retained / "native-detail.json")
        shutil.copytree(home, retained / "native-root-before-injection")
        before = inventory(home)
        planted = home / "unreviewed-empty-directory"
        planted.mkdir(mode=0o700)
        if list(planted.iterdir()) or inventory(home) != before:
            raise AssertionError("empty-directory injection unexpectedly changed native file bytes")
        provenance = {"kind": "test_planted_post_collection_empty_directory",
                      "native_execution": False, "performance_evidence": False,
                      "child_pid": pid, "mode": mode, "collection": result,
                      "path": str(planted), "relative": planted.relative_to(home).as_posix(),
                      "empty": True, "native_file_bytes_unchanged": True}
        self.injections.append(provenance)
        _json(retained / "injection.json", provenance)
        shutil.copytree(home, retained / "root-after-injection")
        return result


class TeardownInstrumentBoundaryTests(StableSourceFixture, unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="bus-scale-census-owned-teardown-instrument-",
                                                     dir=REAL_TEMP_ROOT)
        self.base = Path(self.temporary.name).resolve()
        self.retained = Path(tempfile.mkdtemp(prefix="bus-scale-teardown-instrument-boundary-",
                                             dir=_PLATFORM_TEMP_ROOT)).resolve()
        self.record = {"test": self.id(), "performance_evidence": False,
                       "source_before": self.source_hashes()}
        self.addCleanup(self.retain_and_remove)
        self.source_workspace, _sha = banked_send_repository(self.base)
        self.entry = load(ENTRY, "teardown_instrument_entry")
        self.adapter = load(ADAPTER, "teardown_instrument_adapter")
        self.helper = load(HELPER, "teardown_instrument_helper")
        self.owner = load(SOURCE / "tests/fixtures/bus_scale_census/run-bus-scale-census-2026-10-09.py",
                          "teardown_instrument_owner")
        self.census = load(CENSUS, "teardown_instrument_census")

    def source_hashes(self):
        return {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in SOURCE_FILES}

    def retain_and_remove(self):
        self.record["source_after"] = self.source_hashes()
        try:
            for path in SOURCE_FILES:
                destination = self.retained / "source" / path.relative_to(SOURCE)
                destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                shutil.copyfile(path, destination)
            shutil.copytree(self.base, self.retained / "fixture")
            self.record["fixture_inventory"] = _inventory(self.retained / "fixture")
        finally:
            self.temporary.cleanup()
            self.record["scratch_removed"] = not self.base.exists()
            _json(self.retained / "result.json", self.record)
            print("teardown instrument boundary evidence: " + str(self.retained), flush=True)

    def prepare(self, family):
        pair, workspace = self.census.workload_seed({}, family, CASE, self.source_workspace,
                                                   self.base, self.entry, {}, counts=seed_counts())
        self.record["family"] = family
        self.record["context"] = pair["context"]
        self.record["immutable_seed_inventories"] = {
            scale: _inventory(pair[scale]) for scale in ("1", "10")}
        return pair, workspace

    def assert_seeds_unchanged(self, pair):
        for scale in ("1", "10"):
            self.assertEqual(self.record["immutable_seed_inventories"][scale], _inventory(pair[scale]))

    def test_collected_native_teardown_rejects_unreviewed_empty_bus_directory(self):
        pair, workspace = self.prepare("events")
        seed = inventory(pair["1"])
        self.assertEqual(seed_counts()[0], seed["events.jsonl"].count(b"\n"))
        place = self.base / "empty-directory-observation"
        place.mkdir(mode=0o700)
        owner = EmptyDirectoryOwner(self.owner, seed, self.helper, pair["context"], self.retained)
        refusal, result = None, None
        try:
            result = self.adapter.expanded_observation(owner, CASE, pair, "1", workspace, place, ENTRY)
        except (self.adapter.UnresolvedObservation, self.helper.Unresolved) as error:
            refusal = {"code": error.code, "detail": str(error)}
        finally:
            self.record.update(adapter_refusal=refusal, adapter_result=result,
                               native_launches=[row[0] for row in owner.launches],
                               native_collections=owner.collections, injections=owner.injections)
        self.assertTrue(owner.injections, "negative control must complete a native child before planting state")
        self.assertTrue(all(row["child_collected"] and row["exit_code"] == 0 for row in owner.collections))
        self.assert_seeds_unchanged(pair)
        self.assertIsNotNone(refusal,
            "collector admitted genuine native work with an unreviewed empty bus directory; final restore hides the mutation")
        self.assertIsNone(result, "unreviewed native post-state cannot produce a successful arm")

    def test_argument_preparation_reads_zero_scaled_registry_bytes_after_native_positive(self):
        pair, workspace = self.prepare("registry")
        home, context = pair["prepared_home"], pair["context"]
        seed = inventory(pair["10"])
        self.assertEqual(seed_counts()[1], seed["registry/entries.jsonl"].count(b"\n"))
        shutil.rmtree(home)
        shutil.copytree(pair["10"], home)
        _json(home.parent / "case-context.json", context)
        before = inventory(home)
        stdout, stderr, detail = (home.parent / name for name in
                                  ("positive-stdout.txt", "positive-stderr.txt", "positive-detail.json"))
        environment = {key: value for key, value in os.environ.items()
                       if not key.startswith(("GIT_", "FLOATI_", "PYTHON", "LD_", "DYLD_"))}
        environment.update(PATH="/usr/bin:/bin", PYTHONNOUSERSITE="1", PYTHONDONTWRITEBYTECODE="1")
        with stdout.open("xb") as output, stderr.open("xb") as error:
            child = self.owner.launch_owned_child(
                ["/usr/bin/python3", "-B", str(ENTRY), "cpu", CASE, str(home), str(workspace),
                 str(detail), "owned-census-v1"], cwd=workspace, env=environment,
                stdout=output, stderr=error)
            collected = self.owner.collect_owned_child(child, deadline_seconds=120, collection_seconds=10)
        self.record["native_positive_collection"] = collected
        self.assertTrue(collected["child_collected"])
        self.assertEqual(0, collected["exit_code"], stderr.read_text())
        artifact, native_detail = json.loads(stdout.read_bytes()), json.loads(detail.read_bytes())
        self.assertEqual("completed", artifact["evidence"]["step"])
        self.assertEqual(artifact["evidence"], completed_teardown_for_session(
            FloatiRoot.open_direct_home(home), ACTOR, SESSION))
        witness = self.helper.witness(CASE, home, before, artifact, context, native_detail)
        _json(self.retained / "native-positive-witness.json", witness)
        shutil.copytree(home, self.retained / "native-positive-root")
        shutil.rmtree(home)
        shutil.copytree(pair["10"], home)
        self.assertEqual(seed, inventory(home))
        probe = IOProbe(home)
        with probe:
            argv = self.helper.arguments(CASE, home, workspace, context)
        probe.assert_complete()
        self.record["arguments_io"] = {"read_bytes_by_path": dict(probe.read_bytes_by_path),
                                       "external_processes": probe.external_processes,
                                       "argv": argv, "native_execution": False}
        self.assertEqual([], probe.external_processes, "argument preparation must not launch a substitute native proof")
        self.assertEqual(seed, inventory(home), "argument preparation changed its native restored input")
        self.assert_seeds_unchanged(pair)
        self.assertEqual(0, probe.read_bytes_by_path.get("registry/entries.jsonl", 0),
            "harness argument preparation replays the scaled registry before authoritative native CLI work")

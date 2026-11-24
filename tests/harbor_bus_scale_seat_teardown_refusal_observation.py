"""Current native teardown completion at both exact census boundaries.

These correctness controls use real Git, native activity and owned entry
children. No census timing ratio is asserted. Schema-2 commitments allow the
10k operation to complete; historical d66 refusal admission and actual-byte
proof controls remain in the ruled-refusal and teardown-product-proof modules.
"""
from __future__ import annotations

import base64
import copy
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from tests.bus_scale_test_profile import StableSourceFixture

from floati.events import EventLog
from floati.root import FloatiRoot
from floati.seat_teardown_prefix import prefix_proof
from tests.bus_scale_case_seeds import inventory
from tests.bus_scale_test_profile import banked_send_repository, full_scale_enabled, seed_counts
from tests.harbor_bus_scale_expanded_observation import ADAPTER, ENTRY, RecordingOwner, load
from tests.test_bus_scale_seat_teardown_boundary import ACTOR, SESSION, SOURCE, _inventory, _json
from tests.temp_roots import REAL_TEMP_ROOT, _PLATFORM_TEMP_ROOT


CASE = "seat-teardown"
CENSUS = SOURCE / "tests/fixtures/bus_scale_census/census-bus-scale-2026-10-09.py"
HELPER = SOURCE / "tests/fixtures/bus_scale_census/bus-scale-routine-teardown-2026-10-15.py"
SOURCE_FILES = (Path(__file__).resolve(), CENSUS, HELPER, ENTRY, ADAPTER,
                SOURCE / "tests/bus_scale_native_refusal.py", SOURCE / "tests/bus_scale_case_seeds.py",
                SOURCE / "floati/seat_teardown.py", SOURCE / "floati/seat_teardown_activity.py")


class SeatTeardownRefusalObservationTests(StableSourceFixture, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._shared_preparation = None
        cls._shared_receipts = []
        cls.addClassCleanup(cls._close_shared_preparation)

    @classmethod
    def _close_shared_preparation(cls):
        fixture = cls._shared_preparation
        cls._shared_preparation = None
        if fixture is None:
            return
        fixture.doCleanups()
        for receipt in cls._shared_receipts:
            record = json.loads(receipt.read_bytes())
            record["scratch_removed"] = not Path(record["original_coordinate"]).exists()
            record["shared_fixture_cleanup_pending"] = False
            _json(receipt, record)

    def setUp(self):
        if full_scale_enabled():
            return self._prepare_native_pair()
        cls = type(self)
        fixture = cls._shared_preparation
        reusable = True
        if fixture is not None:
            try:
                reusable = fixture._immutable_identities() == fixture._seed_identities
            except (OSError, ValueError):
                reusable = False
        if not reusable:
            fixture.doCleanups()
            cls._shared_preparation = fixture = None
        if fixture is None:
            fixture = cls()
            try:
                fixture._prepare_native_pair()
                fixture._seed_identities = fixture._immutable_identities()
            except BaseException:
                fixture.doCleanups()
                raise
            cls._shared_preparation = fixture
        for name in ("temporary", "base", "source_workspace", "entry", "adapter", "owner",
                     "workspace", "home"):
            setattr(self, name, getattr(fixture, name))
        self.pair = copy.deepcopy(fixture.pair)
        self.context = self.pair["context"]
        self.seed_inventories = copy.deepcopy(fixture.seed_inventories)
        self.entry._TEARDOWN_GUARD = None
        self.retained = Path(tempfile.mkdtemp(prefix="bus-scale-seat-teardown-refusal-observation-",
                                             dir=_PLATFORM_TEMP_ROOT)).resolve()
        self.record = copy.deepcopy(fixture.record)
        self.record.update(test=self.id(), shared_preparation=str(fixture.retained / "fixture"))
        self._shared_fixture_user = True
        self._owned_places = []
        self.addCleanup(self.retain_and_remove)

    def _immutable_identities(self):
        result = {}
        for scale in ("1", "10"):
            home = self.pair[scale]
            values = {}
            for path in (home, *home.rglob("*")):
                info = path.lstat()
                values[path.relative_to(home).as_posix()] = (info.st_dev, info.st_ino,
                    info.st_mode, info.st_uid, info.st_nlink, info.st_size,
                    info.st_mtime_ns, info.st_ctime_ns)
            result[scale] = values
        return result

    def _prepare_native_pair(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="bus-scale-census-owned-teardown-refusal-",
                                                     dir=REAL_TEMP_ROOT)
        self.base = Path(self.temporary.name).resolve()
        self.retained = Path(tempfile.mkdtemp(prefix="bus-scale-seat-teardown-refusal-observation-",
                                             dir=_PLATFORM_TEMP_ROOT)).resolve()
        self.record = {"test": self.id(), "performance_evidence": False,
            "transport_profile": "full_native" if full_scale_enabled() else "bounded_in_process",
            "original_coordinate": str(self.base),
            "source_before": {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in SOURCE_FILES}}
        self.addCleanup(self.retain_and_remove)
        self.source_workspace, _sha = banked_send_repository(self.base)
        self.entry = load(ENTRY, "native_teardown_refusal_entry")
        self.adapter = load(ADAPTER, "native_teardown_refusal_adapter")
        self.owner = load(SOURCE / "tests/fixtures/bus_scale_census/run-bus-scale-census-2026-10-09.py",
                          "native_teardown_refusal_owner")
        census = load(CENSUS, "native_teardown_refusal_census")
        # Exercise the actual coordinator layout and adapter.prepare route:
        # this source repository deliberately has no configured origin remote.
        self.pair, self.workspace = census.workload_seed({}, "events", CASE,
            self.source_workspace, self.base, self.entry, {}, counts=seed_counts())
        self.home = self.pair["prepared_home"]
        self.context = self.pair["context"]
        self.assertEqual(self.workspace.parent / "repo", self.workspace)
        self.assertEqual("seat-home", self.workspace.parent.name)
        self.assertEqual(str(self.workspace.parent), self.context["home"])
        self.assertTrue((Path(self.context["origin"]) / "config").is_file())
        self.assertEqual(str(self.workspace), self.context["workspace"])
        self.assertEqual(str(self.home), self.context["seed_home"])
        self.assertEqual(SESSION, self.context["session"])
        self.record["native_context"] = self.context
        self.seed_inventories = {scale: _inventory(self.pair[scale]) for scale in ("1", "10")}
        self.record["immutable_seed_inventories"] = self.seed_inventories

    def retain_and_remove(self):
        self.record["source_after"] = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in SOURCE_FILES}
        try:
            for path in SOURCE_FILES:
                destination = self.retained / "source" / path.relative_to(SOURCE)
                destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                shutil.copyfile(path, destination)
            if getattr(self, "_shared_fixture_user", False):
                for path in self._owned_places:
                    shutil.copytree(path, self.retained / "observations" / path.name)
                self.record["observation_inventory"] = _inventory(self.retained / "observations")
            else:
                shutil.copytree(self.base, self.retained / "fixture")
                self.record["fixture_inventory"] = _inventory(self.retained / "fixture")
        finally:
            if getattr(self, "_shared_fixture_user", False):
                self.record["shared_fixture_cleanup_pending"] = True
                type(self)._shared_receipts.append(self.retained / "result.json")
                # Collection and final restoration are asserted in each body;
                # retain its complete bank before removing only that owned bank.
                for path in self._owned_places:
                    shutil.rmtree(path)
            else:
                self.temporary.cleanup()
            self.record["scratch_removed"] = not self.base.exists()
            _json(self.retained / "result.json", self.record)
            print("native teardown refusal observation evidence: " + str(self.retained), flush=True)

    def place(self, name):
        path = self.base / name
        path.mkdir(mode=0o700)
        if getattr(self, "_shared_fixture_user", False):
            self._owned_places.append(path)
        return path

    def immutable_unchanged(self):
        for scale in ("1", "10"):
            self.assertEqual(self.seed_inventories[scale], _inventory(self.pair[scale]))

    def test_actual_coordinator_prepare_completes_1k_native_teardown_in_both_modes(self):
        seed = inventory(self.pair["1"])
        self.assertEqual(seed_counts()[0], seed["events.jsonl"].count(b"\n"))
        place = self.place("positive-1k")
        owner = RecordingOwner(self.owner, seed)
        result = self.adapter.expanded_observation(owner, CASE, self.pair, "1", self.workspace, place, ENTRY)
        self.assertEqual({"exit_code": 0, "status": "ok", "effect": {
            "kind": "seat_teardown_completed", "count": 1}}, result["native"])
        self.assertEqual(seed_counts()[0], result["records"])
        self.assertTrue(result["child_collected"])
        self.assertEqual(["cpu", "diagnostic"], [row[0][3] for row in owner.launches])
        for mode in ("cpu", "diagnostic"):
            artifact = json.loads((place / mode / "stdout.txt").read_bytes())
            self.assertEqual("completed", artifact["evidence"]["step"])
            initial_events = EventLog(FloatiRoot.open_direct_home(self.pair["1"])).event_records()
            self.assertEqual(2, artifact["evidence"]["schema_version"])
            self.assertEqual(prefix_proof(initial_events, "events.jsonl"),
                             artifact["evidence"]["event_cutoff"])
            self.assertEqual(self.context["claim_id"], artifact["evidence"]["claim_receipt_id"])
            self.assertEqual(self.context["consent_id"], artifact["evidence"]["consent_receipt_id"])
            self.assertEqual(self.context["sha"], artifact["evidence"]["pushed_tip"])
        self.assertEqual(seed, inventory(self.home))
        self.immutable_unchanged()
        self.record["actual_prepare_positive_verified"] = True

    def test_10k_current_native_completion_is_collected_without_historical_refusal_admission(self):
        seed = inventory(self.pair["10"])
        self.assertEqual(seed_counts()[1], seed["events.jsonl"].count(b"\n"))
        initial_root = FloatiRoot.open_direct_home(self.pair["10"])
        initial_events = EventLog(initial_root).event_records()
        self.assertEqual(seed_counts()[1], len(initial_events))
        self.assertFalse(EventLog(initial_root).unacked_ids(ACTOR))
        place = self.place("completed-10k")
        owner = RecordingOwner(self.owner, seed)
        result = self.adapter.expanded_observation(owner, CASE, self.pair, "10", self.workspace, place, ENTRY)
        self.assertEqual({"exit_code": 0, "status": "ok", "effect": {
            "kind": "seat_teardown_completed", "count": 1}}, result["native"])
        self.assertNotIn("disposition", result)
        self.assertNotIn("source", result)
        self.assertEqual({"cpu": True, "bytes": True, "locks": True}, result["coverage"])
        self.assertEqual(seed_counts()[1], result["records"])
        self.assertEqual(len(seed["events.jsonl"]), result["bytes"])
        self.assertEqual(hashlib.sha256(seed["events.jsonl"]).hexdigest(), result["seed_sha256"])
        self.assertEqual(result["seed_sha256"], result["clone_sha256"])
        collection = json.loads((place / "collection.json").read_bytes())
        self.assertIsNone(collection["product_proof"], "completed current work must not claim the d66 refusal")
        self.assertEqual(hashlib.sha256((place / "collection.json").read_bytes()).hexdigest(), result["raw_sha256"])
        self.assertEqual(["cpu", "diagnostic"], [row[0][3] for row in owner.launches])
        self.assertEqual(2, len(owner.admitted_inputs))
        for index, mode in enumerate(("cpu", "diagnostic")):
            bank = place / mode
            artifact = json.loads((bank / "stdout.txt").read_bytes())
            self.assertEqual("ok", artifact["status"])
            self.assertEqual("completed", artifact["evidence"]["step"])
            self.assertEqual(2, artifact["evidence"]["schema_version"])
            self.assertEqual(prefix_proof(initial_events, "events.jsonl"),
                             artifact["evidence"]["event_cutoff"])
            mode_collection = json.loads((bank / "collection.json").read_bytes())
            self.assertTrue(mode_collection["collection"]["child_collected"])
            self.assertEqual(0, mode_collection["collection"]["exit_code"])
            self.assertEqual("completed", mode_collection["disposition"])
            self.assertGreater(mode_collection["cpu_seconds"], 0)
            before = {path: base64.b64decode(raw) for path, raw in
                      json.loads((bank / "before-raw.json").read_bytes()).items()}
            # The admitted warm input is independently bound to the immutable
            # seed by RecordingOwner before each measured child starts.
            self.assertEqual(owner.admitted_inputs[index], before)
            archived = inventory(bank / "scale-fleet")
            self.assertTrue(archived["events.jsonl"].startswith(seed["events.jsonl"]))
            receipts = archived["receipts/seat-teardown/scale-worker.jsonl"].splitlines()
            self.assertEqual(["started", "paused", "reported", "completed"],
                             [json.loads(row)["step"] for row in receipts])
            for name in ("detail.json", "native-delta.json", "equivalence.json", "auxiliary-proof.json"):
                self.assertTrue((bank / name).is_file(), "complete native proof must precede next restore")
        self.assertEqual(seed, inventory(self.home), "final restore must follow complete native archiving")
        self.immutable_unchanged()
        self.record["actual_prepare_10k_positive_verified" if full_scale_enabled()
                    else "bounded_current_completion_verified"] = True

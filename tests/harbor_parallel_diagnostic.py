"""Native controls for a fixed-profile, always-unqualified diagnostic.

The initial absent-implementation RED is retained separately. Fixtures reuse the tiny
qualification repository and private import/tool stand-ins; they do not measure
host performance. Corruption happens only after a genuine child is collected.
These controls neither inspect nor recover any retained live-run coordinate.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from tests import harbor_parallel_qualification as qualification_fixtures


class ParallelDiagnosticTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        case = cls("runTest")
        cls.addClassCleanup(case.doCleanups)
        cls.native_owner, cls.native_helper, cls.native_costs, cls.native_output = case.fixture()
        cls.native_before = cls.native_costs.read_bytes(), cls.native_owner.registry.read_bytes()
        cls.native_receipt = case.invoke(cls.native_owner, cls.native_helper,
                                         cls.native_costs, cls.native_output)
        case.collected(cls.native_output, cls.native_receipt)
        case.assertEqual("complete", cls.native_receipt["status"])
        case.assertTrue(cls.native_receipt["successful"])
        case.assertTrue(cls.native_receipt["cleanup"]["successful"])

    def fixture(self):
        owner = qualification_fixtures.ParallelQualificationTests("runTest")
        helper = owner.helper()
        self.addCleanup(owner.doCleanups)
        owner.fixture()
        costs = owner.bank / "historical.csv"
        costs.write_text("test_id,seconds\ntest_cases.Case0.test_observe,0.125\n"
                         "old_population.Removed.test_old,99\n")
        return owner, helper, costs, owner.bank / "diagnostic"

    def invoke(self, owner, helper, costs, output):
        owner.diagnostic_scratch = []
        allocate = helper.tempfile.mkdtemp

        def owned_allocation(*arguments, **keywords):
            if keywords.get("prefix") != "qualification-diagnostic-":
                return allocate(*arguments, **keywords)
            self.assertEqual((), arguments)
            path = Path(allocate(prefix="diagnostic-control-", dir=owner.bank)).resolve()
            owner.diagnostic_scratch.append((path, self.scratch_identity(path)))
            return str(path)

        with owner.registry_patch(helper), mock.patch.object(helper.tempfile, "mkdtemp", owned_allocation):
            return helper.run_diagnostic(owner.root, output, costs)

    def collected(self, output, receipt):
        self.assertEqual(1, len(receipt["launches"]))
        launch = receipt["launches"][0]
        self.assertEqual("parallel", launch["phase"])
        self.assertTrue(launch["collected"])
        self.assertEqual(["-E", "-s", "-B"], launch["argv"][1:4])
        self.assertNotIn("--reference", launch["argv"])
        native = json.loads((output / "parallel/final.json").read_bytes())
        self.assertEqual(8, native["workers"])
        self.assertFalse(native["qualified"])
        self.assertFalse(receipt["qualified"])
        self.assertFalse((output / "serial").exists())
        self.assertFalse((output / "qualification.json").exists())
        self.assertEqual(receipt, json.loads((output / "diagnostic.json").read_bytes()))
        return launch, native

    def test_real_workers_and_grandchildren_use_profile_but_never_qualify(self):
        owner, costs, output = self.native_owner, self.native_costs, self.native_output
        before, receipt = self.native_before, self.native_receipt
        launch, native = self.collected(output, receipt)
        self.assertEqual("complete", receipt["status"])
        self.assertTrue(receipt["successful"])
        self.assertTrue(receipt["cleanup"]["successful"])
        self.assertEqual(0, launch["exit_code"])
        self.assertEqual(8, native["aggregate"]["tests_run"])
        self.assertGreaterEqual(receipt["parallel_wall_seconds"], 0)
        self.assertEqual(hashlib.sha256(before[0]).hexdigest(), receipt["costs"]["sha256"])
        self.assertEqual(receipt["costs"]["sha256"], native["profile"]["sha256"])
        self.assertEqual(1, native["profile"]["matched_ids"])
        self.assertEqual(7, len(native["profile"]["unmatched_population_ids"]))
        self.assertEqual(["old_population.Removed.test_old"], native["profile"]["unused_profile_ids"])
        self.assertEqual(before, (costs.read_bytes(), owner.registry.read_bytes()))
        self.assertEqual({"controller", "discovery", "worker", "grandchild"},
                         set(receipt["preflight"]["stages"]))
        rows = [json.loads(line) for path in (owner.root / "observations").glob("*.jsonl")
                for line in path.read_text().splitlines()]
        self.assertEqual(8, len(rows))
        self.assertEqual(8, len({row["pid"] for row in rows}))
        for row in rows:
            self.assertEqual(row["home"], row["grandchild"]["home"])
            self.assertEqual(row["imports"], row["grandchild"]["imports"])
            self.assertEqual(row["tools"], row["grandchild"]["tools"])
            self.assertNotEqual(str(Path.home()), row["home"])
            for package in ("PIL", "jsonschema"):
                self.assertEqual(str(owner.imports / package / "__init__.py"), row["imports"][package])
            self.assertEqual(owner.tools["minisign"], row["tools"]["FLOATI_TEST_MINISIGN_EXECUTABLE"])
            self.assertEqual(owner.tools["node"], row["tools"]["FLOATI_TEST_NODE_EXECUTABLE"])

    def test_complete_native_failure_retains_outcomes_and_timing_without_qualification(self):
        owner, helper, costs, output = self.fixture()
        path = owner.root / "test_cases.py"
        path.write_text(path.read_text().replace(
            "class Case0(Base, unittest.TestCase): pass",
            "class Case0(Base, unittest.TestCase):\n def test_observe(self): self.fail('native diagnostic failure')"))
        owner.commit()
        receipt = self.invoke(owner, helper, costs, output)
        launch, native = self.collected(output, receipt)
        self.assertEqual("complete", receipt["status"])
        self.assertFalse(receipt["successful"])
        self.assertFalse(native["successful"])
        self.assertEqual(8, native["aggregate"]["tests_run"])
        self.assertNotEqual(0, launch["exit_code"])
        self.assertGreaterEqual(receipt["parallel_wall_seconds"], 0)
        self.assertTrue(receipt["cleanup"]["successful"])
        self.assertIn("process_cost", launch)

    def test_missing_dependency_refuses_before_suite_methods(self):
        owner, helper, costs, output = self.fixture()
        shutil.rmtree(owner.imports / "PIL")
        with self.assertRaises(ValueError):
            self.invoke(owner, helper, costs, output)
        self.assertFalse((owner.root / "observations").exists())

    def test_dirty_source_refuses_before_suite_methods(self):
        owner, helper, costs, output = self.fixture()
        (owner.root / "subject.txt").write_text("dirty source\n")
        with self.assertRaises(ValueError):
            self.invoke(owner, helper, costs, output)
        self.assertFalse((owner.root / "observations").exists())

    def intercept_collected(self, owner, helper, output, change):
        capture = helper._capture
        observed = []

        def collect_then_change(argv, **keywords):
            result = capture(argv, **keywords)
            launch = keywords.get("record")
            if launch is not None and launch.get("phase") == "parallel":
                native = json.loads((output / "parallel/final.json").read_bytes())
                self.assertTrue(launch["collected"])
                self.assertTrue(native["cleanup"]["successful"])
                self.assertEqual("complete", native["status"])
                observed.append(native)
                change(native, keywords)
            return result

        return mock.patch.object(helper, "_capture", collect_then_change), observed

    @staticmethod
    def scratch_identity(path):
        info = path.lstat()
        if path.resolve(strict=True) != path or not stat.S_ISDIR(info.st_mode):
            raise ValueError("fixture scratch is not an ordinary canonical directory")
        return (info.st_dev, info.st_ino, info.st_uid, stat.S_IMODE(info.st_mode))

    def register_synthetic_scratch_cleanup(self, owner, receipt, observed):
        # A corruption/interruption seam only runs AFTER real child collection
        # and native cleanup success. That independent pre-corruption evidence
        # authorizes cleanup of this fixture's outer scratch only.
        if len(observed) != 1:
            return
        if len(owner.diagnostic_scratch) != 1:
            raise ValueError("fixture did not independently record exactly one scratch allocation")
        path, identity = owner.diagnostic_scratch[0]
        if receipt.get("scratch_root") != str(path):
            raise ValueError("returned scratch does not match independent fixture allocation")
        if os.path.lexists(path):
            if self.scratch_identity(path) != identity:
                raise ValueError("fixture scratch identity changed before cleanup")

            def remove_owned():
                if self.scratch_identity(path) != identity:
                    raise ValueError("fixture scratch identity changed at cleanup")
                shutil.rmtree(path)

            self.addCleanup(remove_owned)

    def test_foreign_receipt_scratch_is_rejected_and_sentinel_is_retained(self):
        owner, helper, costs, output = self.fixture()
        owned, foreign = owner.bank / "owned-scratch", owner.bank / "foreign"
        owned.mkdir(mode=0o700)
        foreign.mkdir(mode=0o700)
        sentinel = foreign / "sentinel"
        sentinel.write_bytes(b"foreign contents stay unchanged\n")
        owner.diagnostic_scratch = [(owned, self.scratch_identity(owned))]
        before = len(self._cleanups)
        with self.assertRaisesRegex(ValueError, "independent fixture allocation"):
            self.register_synthetic_scratch_cleanup(owner, {"scratch_root": str(foreign)}, [{}])
        self.assertEqual(before, len(self._cleanups), "mismatch registered a destructive cleanup")
        self.assertEqual(b"foreign contents stay unchanged\n", sentinel.read_bytes())
        self.assertTrue(owned.is_dir())

    def test_source_registry_and_cost_drift_after_collection_remain_invalid(self):
        for field in ("source", "registry", "costs"):
            with self.subTest(field=field):
                owner, helper, costs, output = self.fixture()

                def change(native, keywords):
                    if field == "source":
                        (owner.root / "subject.txt").write_text("drift\n")
                    elif field == "registry":
                        owner.profile["jobs"]["future-job"] = {"kind": "fixed-python"}
                        owner.write_registry()
                    else:
                        costs.write_text(costs.read_text().replace("0.125", "0.25"))

                patch, observed = self.intercept_collected(owner, helper, output, change)
                with patch:
                    receipt = self.invoke(owner, helper, costs, output)
                self.register_synthetic_scratch_cleanup(owner, receipt, observed)
                self.assertEqual(1, len(observed))
                self.collected(output, receipt)
                self.assertEqual("invalid", receipt["status"])
                self.assertFalse(receipt["successful"])

    def test_native_journal_cost_and_aggregate_corruption_cannot_be_admitted(self):
        for field in ("journal", "process-cost", "aggregate"):
            with self.subTest(case=field):
                helper, path, native, launch = self.copied_native()
                # Prove the copied genuine artifacts are admissible before the
                # single corruption; a relocation mistake cannot supply the RED.
                self.admit_copy(helper, path, native, launch)
                phase = path.parent
                if field == "journal":
                    key = native["results"][0]["key"]
                    journal = phase / (key + ".events.jsonl")
                    journal.write_text("\n".join(journal.read_text().splitlines()[:-1]) + "\n")
                elif field == "process-cost":
                    (phase / "process-cost.json").unlink()
                else:
                    native["aggregate"]["tests_run"] += 1
                    path.write_text(json.dumps(native) + "\n")
                # Missing artifacts retain the native filesystem absence;
                # malformed present evidence is a validation refusal.
                expected = FileNotFoundError if field == "process-cost" else ValueError
                with self.assertRaises(expected):
                    self.admit_copy(helper, path, native, launch)

    def copied_native(self):
        temporary = tempfile.TemporaryDirectory(prefix="admission-copy-", dir=self.native_owner.bank)
        self.addCleanup(temporary.cleanup)
        phase = Path(temporary.name) / "parallel"
        shutil.copytree(self.native_output / "parallel", phase)
        path = phase / "final.json"
        native = json.loads(path.read_bytes())
        native["final_path"] = str(path)
        path.write_text(json.dumps(native) + "\n")
        cost_path = phase / "process-cost.json"
        cost = json.loads(cost_path.read_bytes())
        cost["final_path"] = str(path)
        cost_path.write_text(json.dumps(cost) + "\n")
        return self.native_helper, path, native, copy.deepcopy(self.native_receipt["launches"][0])

    def admit_copy(self, helper, path, native, launch):
        return helper._admit_diagnostic(helper._launcher(self.native_owner.root), path,
            native, launch, self.native_receipt["source"], self.native_receipt["costs"],
            self.native_costs.read_bytes(),
            self.native_receipt["preflight"]["stages"]["controller"]["interpreter"])

    def test_unproved_native_cleanup_retains_outer_scratch_and_never_becomes_green(self):
        owner, helper, costs, output = self.fixture()

        def change(native, keywords):
            native["cleanup"]["successful"] = False
            (output / "parallel/final.json").write_text(json.dumps(native) + "\n")

        patch, observed = self.intercept_collected(owner, helper, output, change)
        with patch:
            receipt = self.invoke(owner, helper, costs, output)
        # Only this synthetic control may clean its own outer scratch: before
        # corruption it independently observed genuine collected native proof.
        # No cleanup helper is run against an interrupted or unknown real run.
        self.register_synthetic_scratch_cleanup(owner, receipt, observed)
        self.assertEqual(1, len(observed))
        self.collected(output, receipt)
        self.assertFalse(receipt["successful"])
        self.assertFalse(receipt["cleanup"]["successful"])
        self.assertTrue(receipt["cleanup"]["scratch_retained"])
        self.assertTrue(Path(receipt["scratch_root"]).is_dir())

    def corrupted_ownership(self, mutation):
        owner, helper, costs, output = self.fixture()

        def change(native, keywords):
            self.assertTrue(native["cleanup"]["ownership"]["observed"],
                            "corruption control requires genuine observed process identities")
            mutation(native)
            (output / "parallel/final.json").write_text(json.dumps(native) + "\n")

        patch, observed = self.intercept_collected(owner, helper, output, change)
        with patch:
            receipt = self.invoke(owner, helper, costs, output)
        self.register_synthetic_scratch_cleanup(owner, receipt, observed)
        self.assertEqual(1, len(observed))
        self.collected(output, receipt)
        self.assertEqual("invalid", receipt["status"], "contradictory ownership was admitted")
        self.assertFalse(receipt["successful"])
        self.assertFalse(receipt["cleanup"]["native_cleanup_proved"])
        self.assertFalse(receipt["cleanup"]["successful"])
        self.assertTrue(receipt["cleanup"]["scratch_retained"])
        self.assertTrue(Path(receipt["scratch_root"]).is_dir())

    def test_observed_processes_without_departures_cannot_authorize_cleanup(self):
        def empty_departures(native):
            native["cleanup"]["ownership"]["departed"] = []
        self.corrupted_ownership(empty_departures)

    def test_malformed_process_identity_cannot_authorize_cleanup(self):
        def malformed_identity(native):
            native["cleanup"]["ownership"]["observed"][0]["birth"] = [True]
        self.corrupted_ownership(malformed_identity)

    def test_wrong_collected_leader_cannot_authorize_cleanup(self):
        def wrong_leader(native):
            native["children"][0]["pid"] += 1_000_000
        self.corrupted_ownership(wrong_leader)

    def test_cancellation_at_collected_boundary_prevents_success_and_receipt_upgrade(self):
        owner, helper, costs, output = self.fixture()

        def change(native, keywords):
            # A controlled delivery at a real collected-child boundary; no
            # process is signaled and no descendant is deliberately orphaned.
            keywords["cancellation"].number = signal.SIGINT

        patch, observed = self.intercept_collected(owner, helper, output, change)
        with patch:
            receipt = self.invoke(owner, helper, costs, output)
        self.register_synthetic_scratch_cleanup(owner, receipt, observed)
        self.assertEqual(1, len(observed))
        self.collected(output, receipt)
        self.assertEqual("interrupted", receipt["status"])
        self.assertEqual(130, receipt["exit_code"])
        self.assertFalse(receipt["successful"])

    def test_cli_refuses_authority_overrides_before_artifact_mutation(self):
        root = qualification_fixtures.SOURCE
        script = root / "scripts/diagnose-parallel.py"
        self.assertTrue(script.is_file(), "closed fixed-profile diagnostic CLI is absent")
        owner, helper, costs, output = self.fixture()
        output.mkdir()
        (output / "sentinel").write_bytes(b"retain exactly\n")
        for flag in ("--profile", "--registry", "--root", "--workers", "--reference", "--sha"):
            with self.subTest(flag=flag):
                result = subprocess.run([sys.executable, "-B", str(script), "--output", str(output),
                    "--costs", str(costs), flag, "/caller-override"], cwd=root,
                    capture_output=True, text=True, timeout=10)
                self.assertEqual(2, result.returncode, result.stderr)
                self.assertIn("unrecognized arguments", result.stderr)
                self.assertIn(flag, result.stderr)
                self.assertEqual({"sentinel": b"retain exactly\n"},
                                 {path.name: path.read_bytes() for path in output.iterdir()})


if __name__ == "__main__":
    unittest.main()

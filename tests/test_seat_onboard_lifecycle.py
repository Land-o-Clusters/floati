"""Scratch regressions for repeated seat incarnations and owned recovery cuts."""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest import mock

from floati.codex_wait_contract import WORKSPACE_MAP_RELATIVE, consent_ledger_relative
from floati.jsonl import read_records_snapshot
from floati.registry import Registry
from floati.seat_declaration import SeatDeclaration
from tests import test_seat_onboard as fixtures


class SeatOnboardLifecycleTests(unittest.TestCase):
    setUp = fixtures.SeatOnboardingTests.setUp
    git = fixtures.SeatOnboardingTests.git
    cli = fixtures.SeatOnboardingTests.cli
    add = fixtures.SeatOnboardingTests.add

    def prepared(self, *extra):
        code, output, errors = self.add(*extra)
        self.assertEqual(0, code, errors or output)
        return json.loads(output)["evidence"]

    def remove(self):
        return self.cli("seat", "remove", "--root", str(self.root.path),
                        "--home", str(self.home))

    def removed(self):
        code, output, errors = self.remove()
        self.assertEqual(0, code, errors or output)
        self.assertFalse(self.home.exists())
        return json.loads(output)["evidence"]

    def assert_io_failure(self, response):
        code, output, errors = response
        self.assertEqual(20, code, errors or output)
        artifact = json.loads(output)
        self.assertEqual("refused", artifact["status"])
        self.assertEqual("seat_onboard_io_failed", artifact["evidence"]["code"])

    def consent(self, node):
        return read_records_snapshot(self.root, consent_ledger_relative(node),
                                     allowed_kinds={"codex_wait_consent_receipt"})

    def test_same_node_same_home_rehome_removal_disarms_latest_native_consent(self):
        baseline = (self.config.read_bytes(), self.profiles.read_bytes())
        first = self.prepared("--node", "repeat-seat", "--profile", "repeat-profile")
        self.removed()
        first_disarm = self.consent(first["node_id"])[-1]
        self.assertEqual("disarmed", first_disarm["state"])
        second = self.prepared("--node", first["node_id"], "--profile", first["profile"], "--rehome")
        rearmed = self.consent(first["node_id"])[-1]
        self.assertEqual("armed", rearmed["state"])
        self.assertNotEqual(first_disarm["id"], rearmed["id"])
        self.assertEqual(first["node_id"], second["node_id"])
        self.removed()
        latest = self.consent(first["node_id"])[-1]
        self.assertEqual("disarmed", latest["state"])
        self.assertNotEqual(first_disarm["id"], latest["id"])
        self.assertEqual("retired", Registry(self.root)._latest(first["node_id"])["state"])
        self.assertFalse(self.root.path.joinpath(WORKSPACE_MAP_RELATIVE).exists())
        self.assertEqual(baseline, (self.config.read_bytes(), self.profiles.read_bytes()))

    def test_third_same_node_same_home_incarnation_preserves_distinct_lifecycle_history(self):
        baseline = (self.config.read_bytes(), self.profiles.read_bytes())
        first = self.prepared("--node", "repeat-seat", "--profile", "repeat-profile")
        self.removed()
        arguments = ("--node", first["node_id"], "--profile", first["profile"], "--rehome")
        self.prepared(*arguments)
        self.removed()
        archives = {path: path.read_bytes() for path in self.root.path.joinpath(
            "state/seat-onboarding-history").glob("*.json")}
        self.assertTrue(archives)
        third = self.prepared(*arguments)
        self.assertEqual(first["node_id"], third["node_id"])
        for path, encoded in archives.items():
            self.assertEqual(encoded, path.read_bytes())
        self.removed()
        current_archives = list(self.root.path.joinpath("state/seat-onboarding-history").glob("*.json"))
        self.assertGreater(len(current_archives), len(archives))
        self.assertEqual("disarmed", self.consent(first["node_id"])[-1]["state"])
        self.assertEqual(baseline, (self.config.read_bytes(), self.profiles.read_bytes()))

    def test_published_declaration_then_failure_remains_owned_and_reversible(self):
        baseline = (self.config.read_bytes(), self.profiles.read_bytes())
        publish = SeatDeclaration.publish_declared_workspace
        markers = []
        def publish_then_fail(*arguments, **keywords):
            result = publish(*arguments, **keywords)
            marker = Path(arguments[0]) / "SEAT.json"
            markers.append(marker.read_bytes())
            raise OSError("injected failure after owned declaration publication")
        with mock.patch.object(SeatDeclaration, "publish_declared_workspace", publish_then_fail):
            self.assert_io_failure(self.add("--node", "declaration-seat", "--profile", "declaration-profile"))
        self.assertEqual(1, len(markers))
        self.assertEqual(markers[0], self.home.joinpath("repo/SEAT.json").read_bytes())
        self.removed()
        self.assertFalse(self.root.path.joinpath(WORKSPACE_MAP_RELATIVE).exists())
        self.assertEqual("disarmed", self.consent("declaration-seat")[-1]["state"])
        self.assertEqual("retired", Registry(self.root)._latest("declaration-seat")["state"])
        self.assertEqual(baseline, (self.config.read_bytes(), self.profiles.read_bytes()))

    def test_interrupted_owned_stage_deletion_retries_matching_inventory_subset(self):
        from floati import seat_onboarding
        baseline = (self.config.read_bytes(), self.profiles.read_bytes())
        publish = seat_onboarding._rename_generation_noreplace
        def fail_home_publish(source, target):
            if target == self.home:
                raise OSError("injected failure before owned stage publication")
            return publish(source, target)
        with mock.patch.object(seat_onboarding, "_rename_generation_noreplace", fail_home_publish):
            self.assert_io_failure(self.add())
        records = list(self.root.path.joinpath("state/seat-onboarding").glob("*.json"))
        self.assertEqual(1, len(records))
        record = json.loads(records[0].read_bytes())
        stage = Path(record["stage"])
        self.assertIn("repo/README.md", record["inventory"])
        self.assertFalse(self.home.exists())
        native_rmtree = seat_onboarding.shutil.rmtree
        def partial_stage_delete(path, *arguments, **keywords):
            if Path(path) == stage:
                stage.joinpath("repo/README.md").unlink()
                raise OSError("injected interruption after deleting an owned stage file")
            return native_rmtree(path, *arguments, **keywords)
        with mock.patch.object(seat_onboarding.shutil, "rmtree", partial_stage_delete):
            self.assert_io_failure(self.remove())
        self.assertTrue(stage.is_dir())
        self.assertFalse(stage.joinpath("repo/README.md").exists())
        self.removed()
        self.assertFalse(stage.exists())
        self.assertIsNone(Registry(self.root)._latest(record["node_id"]))
        self.assertEqual(baseline, (self.config.read_bytes(), self.profiles.read_bytes()))


if __name__ == "__main__":
    unittest.main()

"""Exercise every retirement entry point against real owned lane directories."""
from dataclasses import replace
from pathlib import Path
import unittest
from unittest.mock import patch

from floati.admin_registry import RegistryAdminBackend
from floati.errors import DurabilityFailure, ProtocolRefusal
from floati.ids import uuid7_hex
from floati.node_wizard import NodeRetirePlan
from floati.registry import Registry
from tests import test_lane_workspaces, test_sc1_lane_scaling


class LaneWorkspaceRetirementTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_lane_workspaces.LaneWorkspaceTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def plan(self):
        root = self.fixture.root
        record = dict(Registry(root).require_active("builder"),
                      id="registry-" + uuid7_hex(), state="retired")
        return NodeRetirePlan("builder", str(root.path / "nodes/builder"), (record,))

    def test_admin_retirement_checks_all_lanes_before_removing_any(self):
        service = self.fixture._service()
        clean = Path(service.open(actor="builder", row="a-clean", repo="fixture")["workspace"])
        dirty = Path(service.open(actor="builder", row="z-dirty", repo="fixture")["workspace"])
        (dirty / "tracked.txt").write_text("uncommitted")
        backend = RegistryAdminBackend(self.fixture.root)
        with patch.object(type(service), "_runtime_references", return_value=[]):
            with self.assertRaises(ProtocolRefusal) as caught:
                backend.commit_retire(self.plan())
            self.assertEqual("lane_workspace_dirty", caught.exception.code)
            self.assertTrue(clean.is_dir())
            self.assertTrue(dirty.is_dir())
            self.fixture._git(dirty, "restore", "tracked.txt")
            backend.commit_retire(self.plan())
        self.assertFalse(clean.exists())
        self.assertFalse(dirty.exists())

    def test_invalid_retirement_preview_cannot_delete_a_clean_lane(self):
        service = self.fixture._service()
        workspace = Path(service.open(actor="builder", row="clean", repo="fixture")["workspace"])
        plan = self.plan()
        broken = replace(plan, records=(dict(plan.records[0], role=""),))
        with patch.object(type(service), "_runtime_references", return_value=[]):
            with self.assertRaises(ProtocolRefusal):
                RegistryAdminBackend(self.fixture.root).commit_retire(broken)
        self.assertTrue(workspace.is_dir())
        Registry(self.fixture.root).require_active("builder")

    def test_registry_capacity_refusal_precedes_lane_removal(self):
        service = self.fixture._service()
        workspace = Path(service.open(actor="builder", row="clean", repo="fixture")["workspace"])
        with patch("floati.lane_retirement.MAX_LEDGER_RECORDS", 1):
            with self.assertRaises(ProtocolRefusal) as caught:
                Registry(self.fixture.root).retire("builder")
        self.assertEqual("lane_retirement_preflight_failed", caught.exception.code)
        self.assertTrue(workspace.is_dir())

    def test_late_registry_failure_reports_completed_removal_as_incomplete_progress(self):
        import floati.jsonl as ledger
        service = self.fixture._service()
        workspace = Path(service.open(actor="builder", row="clean", repo="fixture")["workspace"])
        original = ledger._append_frame
        registry_path = self.fixture.root.path / "registry/entries.jsonl"
        def fail_registry(path, *args, **kwargs):
            if path == registry_path:
                raise ProtocolRefusal("fixture_registry_unavailable", "injected after lane close")
            return original(path, *args, **kwargs)
        with patch.object(type(service), "_runtime_references", return_value=[]), patch.object(ledger, "_append_frame", fail_registry):
            with self.assertRaises(DurabilityFailure) as caught:
                Registry(self.fixture.root).retire("builder")
        self.assertEqual("lane_retirement_incomplete", caught.exception.code)
        self.assertIn(str(workspace), caught.exception.detail)
        self.assertFalse(workspace.exists())
        Registry(self.fixture.root).require_active("builder")
        self.assertEqual("closed", self.fixture._records()[-1]["state"])

    def test_numbered_retirement_preserves_lane_history_and_legacy_state(self):
        numbered = test_sc1_lane_scaling.Sc1LaneScalingTests()
        numbered.setUp()
        self.addCleanup(numbered.doCleanups)
        service = numbered.service()
        node = service.spawn(actor="architect-a", profile_name="sre")["node_id"]
        for name in ("lanes-root.json", "lane-repositories.json", "lane-board.json"):
            target = numbered.root.path / "state" / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes((self.fixture.root.path / "state" / name).read_bytes())
        from floati.lane_workspaces import LaneWorkspaces
        lanes = LaneWorkspaces(numbered.root, launch_agents=self.fixture.launch_agents,
                               lsof_executable=self.fixture.lsof)
        workspace = Path(lanes.open(actor=node, row="owned", repo="fixture")["workspace"])
        (workspace / "tracked.txt").write_text("dirty")
        home = numbered.root.path / "nodes" / node
        foreign = home / "keep.bin"
        foreign.write_bytes(b"retained legacy state")
        with patch.object(LaneWorkspaces, "_runtime_references", return_value=[]):
            with self.assertRaises(ProtocolRefusal) as caught:
                service.retire(actor="architect-a", instance=node, drain=True)
            self.assertEqual("lane_workspace_dirty", caught.exception.code)
            self.assertTrue(workspace.is_dir())
            self.fixture._git(workspace, "restore", "tracked.txt")
            receipt = service.retire(actor="architect-a", instance=node, drain=True)
        self.assertFalse(workspace.exists())
        self.assertTrue((home / "lanes.jsonl").is_file())
        self.assertEqual(b"retained legacy state", foreign.read_bytes())
        self.assertIn(str(home), receipt["retained"])
        self.assertNotIn(str(home), receipt["removed"])

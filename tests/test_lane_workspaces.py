"""The six ordered LANES-1 REDs use only declared local fixture coordinates."""
from __future__ import annotations

import hashlib
import importlib
import json
import plistlib
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from floati.errors import ProtocolRefusal
from floati.framing import decode_frames
from floati.git_process import fixed_git_command, fixed_git_environment
from floati.registry import Registry
from floati.root import FloatiRoot


class LaneWorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name).resolve()
        self.root = FloatiRoot.open_direct_home(self.base / "fleet", create=True)
        Registry(self.root).register("builder", "Codex")
        self.repository = self.base / "repository"
        self.repository.mkdir()
        self._git(self.repository, "init", "--quiet", "--initial-branch=main")
        self._git(self.repository, "config", "user.name", "Fixture Builder")
        self._git(self.repository, "config", "user.email", "fixture@example.invalid")
        self._git(self.repository, "config", "extensions.worktreeConfig", "true")
        (self.repository / "tracked.txt").write_text("base\n", encoding="utf-8")
        self._git(self.repository, "add", "tracked.txt")
        self._git(self.repository, "commit", "--quiet", "-m", "fixture base")
        self.base_sha = self._git(self.repository, "rev-parse", "HEAD")
        self._git(self.repository, "update-ref", "refs/remotes/origin/main", self.base_sha)
        self.lanes_root = self.base / "lanes"
        self.launch_agents = self.base / "LaunchAgents"
        self.launch_agents.mkdir()
        self.lsof = self.base / "fixture-lsof"
        self.lsof.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        self.lsof.chmod(0o755)
        self._state("lanes-root.json", {"schema_version": 0, "path": str(self.lanes_root)})
        self._state("lane-repositories.json", {
            "schema_version": 0,
            "repositories": {"fixture": {"path": str(self.repository),
                                           "default_base": "refs/remotes/origin/main"}},
        })
        self._state("lane-board.json", {"schema_version": 0, "rows": {}})

    def _git(self, repository, *arguments):
        result = subprocess.run(
            fixed_git_command("/usr/bin/git", repository, arguments),
            env=fixed_git_environment("/usr/bin/git"),
            capture_output=True, text=True, timeout=15, check=True,
        )
        return result.stdout.strip()

    def _state(self, name, value):
        path = self.root.path / "state" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value) + "\n", encoding="utf-8")

    def _service(self):
        try:
            module = importlib.import_module("floati.lane_workspaces")
        except ModuleNotFoundError as exc:
            if exc.name != "floati.lane_workspaces":
                raise
            self.fail("LANES-1 is missing floati.lane_workspaces.LaneWorkspaces")
        self.assertTrue(hasattr(module, "LaneWorkspaces"), "LANES-1 service is missing")
        return module.LaneWorkspaces(self.root, launch_agents=self.launch_agents,
                                     lsof_executable=self.lsof)

    def _records(self):
        path = self.root.path / "nodes" / "builder" / "lanes.jsonl"
        self.assertTrue(path.is_file(), "lane ownership must have a durable ledger")
        return decode_frames(path.read_bytes())

    def _refuses(self, code, operation):
        with self.assertRaises(ProtocolRefusal) as caught:
            operation()
        self.assertEqual(code, caught.exception.code)
        self.assertIsInstance(caught.exception.remedy, str)
        self.assertTrue(caught.exception.remedy.strip())
        return caught.exception

    def test_01_open_records_owned_worktree_and_refuses_duplicate(self):
        """Catches creating an unrecorded lane or overwriting an existing row."""
        service = self._service()
        result = service.open(actor="builder", row="row-one", repo="fixture")
        workspace = self.lanes_root / "builder" / "work" / "row-one"
        self.assertEqual(str(workspace), result["workspace"])
        self.assertTrue(workspace.is_dir())
        self.assertEqual(self.base_sha, self._git(workspace, "rev-parse", "HEAD"))
        self.assertEqual(self.repository / ".git", Path(self._git(
            workspace, "rev-parse", "--path-format=absolute", "--git-common-dir")))
        record = result["record"]
        self.assertEqual("lane_workspace_record", record["kind"])
        self.assertEqual("builder/work/row-one", record["path"])
        self.assertEqual("open", record["state"])
        self.assertEqual(self.base_sha, record["base_sha"])
        self.assertIn(record, self._records())
        before = (self.root.path / "nodes/builder/lanes.jsonl").read_bytes()
        self._refuses("lane_workspace_exists", lambda: service.open(
            actor="builder", row="row-one", repo="fixture"))
        self.assertEqual(before, (self.root.path / "nodes/builder/lanes.jsonl").read_bytes())
        self.assertEqual("base\n", (workspace / "tracked.txt").read_text())

    def test_02_close_refuses_dirty_unpushed_and_receipts_force_reason(self):
        """Catches deleting unbanked work or losing an explicit force explanation."""
        service = self._service()
        workspace = Path(service.open(actor="builder", row="row-two", repo="fixture")["workspace"])
        (workspace / "tracked.txt").write_text("changed\n", encoding="utf-8")
        self._refuses("lane_workspace_dirty", lambda: service.close(actor="builder", row="row-two"))
        self.assertEqual("changed\n", (workspace / "tracked.txt").read_text())
        self._git(workspace, "add", "tracked.txt")
        self._git(workspace, "commit", "--quiet", "-m", "fixture unpushed work")
        self._refuses("lane_workspace_unpushed", lambda: service.close(actor="builder", row="row-two"))
        self.assertTrue(workspace.is_dir())
        result = service.close(actor="builder", row="row-two", force=True,
                               why="Discard the explicit fixture experiment")
        self.assertEqual(str(workspace), result["removed"])
        self.assertFalse(workspace.exists())
        self.assertNotIn(str(workspace), self._git(self.repository, "worktree", "list", "--porcelain"))
        self.assertEqual("closed", result["record"]["state"])
        self.assertEqual("builder", result["record"]["closed_by"])
        self.assertEqual("Discard the explicit fixture experiment", result["record"]["why"])
        self.assertEqual(["open", "closed"], [row["state"] for row in self._records()])

    def test_03_close_preserves_workspace_referenced_by_launchagent(self):
        """Catches removal of a checkout still named by an installed launcher."""
        service = self._service()
        workspace = Path(service.open(actor="builder", row="row-three", repo="fixture")["workspace"])
        plist = self.launch_agents / "fixture.plist"
        plist.write_bytes(plistlib.dumps({"Label": "fixture", "ProgramArguments": [
            "/usr/bin/python3", str(workspace / "launcher.py")]}))
        refused = self._refuses("lane_workspace_in_use", lambda: service.close(
            actor="builder", row="row-three"))
        self.assertIn(str(plist), refused.detail)
        self.assertTrue(workspace.is_dir())
        self.assertEqual(["open"], [row["state"] for row in self._records()])

    def test_04_retire_preflights_all_lanes_and_retains_close_history(self):
        """Catches retirement bypassing dirty checks or deleting earlier clean lanes."""
        service = self._service()
        clean = Path(service.open(actor="builder", row="a-clean", repo="fixture")["workspace"])
        dirty = Path(service.open(actor="builder", row="z-dirty", repo="fixture")["workspace"])
        (dirty / "tracked.txt").write_text("retained work\n", encoding="utf-8")
        with patch.object(type(service), "_runtime_references", return_value=[]):
            refused = self._refuses("lane_workspace_dirty", lambda: Registry(self.root).retire("builder"))
            self.assertIn("z-dirty", refused.detail)
            self.assertTrue(clean.is_dir(), "preflight must precede the first removal")
            self.assertTrue(dirty.is_dir())
            Registry(self.root).require_active("builder")
            self._git(dirty, "restore", "tracked.txt")
            retired = Registry(self.root).retire("builder")
        self.assertEqual("retired", retired["state"])
        self.assertFalse(clean.exists())
        self.assertFalse(dirty.exists())
        closed = [row for row in self._records() if row["state"] == "closed"]
        self.assertEqual({"a-clean", "z-dirty"}, {row["row"] for row in closed})

    def test_05_sweep_closes_landed_owned_lane_and_preserves_unmanaged_bytes(self):
        """Catches sweeping foreign directories or removing rows without board evidence."""
        service = self._service()
        landed = Path(service.open(actor="builder", row="row-five", repo="fixture")["workspace"])
        retained = Path(service.open(actor="builder", row="still-open", repo="fixture")["workspace"])
        foreign = self.lanes_root / "foreign"
        foreign.mkdir()
        payload = foreign / "keep.bin"
        payload.write_bytes(b"unmanaged fixture\x00\xff")
        digest = hashlib.sha256(payload.read_bytes()).hexdigest()
        self._state("lane-board.json", {"schema_version": 0, "rows": {"row-five": "landed"}})
        preview = service.sweep()
        self.assertEqual(1, len(preview["eligible"]))
        self.assertEqual([], preview["closed"])
        self.assertTrue(preview["unmanaged"])
        self.assertTrue(landed.exists())
        applied = service.sweep(apply=True)
        self.assertEqual("degraded", applied["status"])
        self.assertEqual(1, len(applied["closed"]))
        self.assertEqual("operation:sweep", applied["closed"][0]["record"]["closed_by"])
        self.assertFalse(landed.exists())
        self.assertTrue(retained.exists())
        self.assertEqual(digest, hashlib.sha256(payload.read_bytes()).hexdigest())

    def test_06_doctor_measures_open_lane_age_and_unmanaged_bytes(self):
        """Catches missing lane monitoring or silently inventing zero unmanaged bytes."""
        service = self._service()
        service.open(actor="builder", row="row-six", repo="fixture")
        foreign = self.lanes_root / "foreign"
        foreign.mkdir()
        (foreign / "keep.bin").write_bytes(b"unmanaged")
        observed = service.doctor()
        node = next(row for row in observed["nodes"] if row["node_id"] == "builder")
        self.assertEqual(1, node["open_lanes"])
        self.assertIsInstance(node["oldest_open_age_seconds"], (int, float))
        self.assertGreaterEqual(node["oldest_open_age_seconds"], 0)
        self.assertEqual(9, observed["unmanaged_bytes"])


if __name__ == "__main__":
    unittest.main()

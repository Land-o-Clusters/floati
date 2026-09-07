"""Removal boundaries for declared lane workspaces; no live host observers."""
from __future__ import annotations

import json
import shlex
import unittest
from pathlib import Path
from unittest.mock import patch

from floati.errors import ProtocolRefusal
from tests import test_lane_workspaces as fixtures


class LaneWorkspaceSafetyTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.LaneWorkspaceTests()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()

    def _open(self, row="safety-row"):
        service = self.fixture._service()
        result = service.open(actor="builder", row=row, repo="fixture")
        return service, Path(result["workspace"])

    def _refused(self, operation, code=None):
        with self.assertRaises(ProtocolRefusal) as caught:
            operation()
        if code is not None:
            self.assertEqual(code, caught.exception.code)
        self.assertIsInstance(caught.exception.remedy, str)
        self.assertTrue(caught.exception.remedy.strip())
        return caught.exception

    def _force(self, service, row="safety-row"):
        return service.close(actor="builder", row=row, force=True,
                             why="Explicit fixture cleanup request")

    def test_unrecorded_directory_survives_forced_close(self):
        """Catches treating a matching layout path as deletion authority."""
        service = self.fixture._service()
        workspace = self.fixture.lanes_root / "builder/work/safety-row"
        workspace.mkdir(parents=True)
        foreign = workspace / "keep.bin"
        foreign.write_bytes(b"foreign work\x00\xff")
        self._refused(lambda: self._force(service))
        self.assertEqual(b"foreign work\x00\xff", foreign.read_bytes())

    def test_changed_owner_marker_cannot_be_forced(self):
        """Catches trusting an old lane record after workspace ownership changes."""
        service, workspace = self._open()
        git_dir = Path(self.fixture._git(workspace, "rev-parse", "--absolute-git-dir"))
        marker = git_dir / "floati-lane-owner.json"
        self.assertTrue(marker.is_file())
        identity = json.loads(marker.read_text())
        identity["opened_record_id"] = "lane-workspace-00000000000070008000000000000000"
        marker.write_text(json.dumps(identity) + "\n")
        self._refused(lambda: self._force(service))
        self.assertTrue(workspace.is_dir())
        self.assertEqual(["open"], [record["state"] for record in self.fixture._records()])

    def test_new_lane_overrides_fence_without_changing_shared_parent(self):
        """Catches either inherited seat fences or clearing another checkout's fence."""
        fixture = self.fixture
        fixture._git(fixture.repository, "config", "--local", "floati.seatFenceRoot", "/fixture/parent-fleet")
        fixture._git(fixture.repository, "config", "--local", "floati.seatFenceNode", "parent-seat")
        shared_config = fixture.repository / ".git/config"
        before = shared_config.read_bytes()
        _service, workspace = self._open()
        self.assertEqual(before, shared_config.read_bytes())
        for key, parent in (("floati.seatFenceRoot", "/fixture/parent-fleet"),
                            ("floati.seatFenceNode", "parent-seat")):
            self.assertEqual(parent, fixture._git(fixture.repository, "config", "--get", key))
            self.assertEqual("", fixture._git(workspace, "config", "--get", key))
            self.assertEqual("", fixture._git(workspace, "config", "--worktree", "--get", key))

    def test_binding_reference_cannot_be_forced(self):
        """Catches runtime binding references being weaker than dirty-work overrides."""
        service, workspace = self._open()
        binding = self.fixture.root.path / "state/wake-daemon/adapters/codex/fixture.json"
        binding.parent.mkdir(parents=True)
        binding.write_text(json.dumps({"schema_version": 0, "workspace": str(workspace)}) + "\n")
        refusal = self._refused(lambda: self._force(service), "lane_workspace_in_use")
        self.assertIn(str(binding), refusal.detail)
        self.assertTrue(workspace.exists())

    def test_lsof_reference_cannot_be_forced(self):
        """Catches deleting a clean checkout while a process holds its files."""
        service, workspace = self._open()
        for returncode, warning in ((1, ""), (0, ""),
                                    (1, "lsof: WARNING: incomplete scan")):
            with self.subTest(returncode=returncode, warning=warning):
                self.fixture.lsof.write_text(
                    "#!/bin/sh\nprintf '%s\\n' " + shlex.quote("p123") + " "
                    + shlex.quote("n" + str(workspace / "tracked.txt"))
                    + "\nprintf '%s' " + shlex.quote(warning) + " >&2\nexit "
                    + str(returncode) + "\n")
                refusal = self._refused(lambda: self._force(service), "lane_workspace_in_use")
                self.assertIn("p123", refusal.detail)
                self.assertIn(str(workspace / "tracked.txt"), refusal.detail)
                self.assertTrue(workspace.exists())

    def test_lsof_opendir_warning_preserves_lane_and_names_diagnostic(self):
        """Catches mistaking an incomplete recursive scan for proof of no users."""
        service, workspace = self._open()
        warning = "lsof: WARNING: can't opendir(" + str(workspace / "vendor") + ")"
        self.fixture.lsof.write_text(
            "#!/bin/sh\nprintf '%s\\n' " + shlex.quote(warning) + " >&2\nexit 1\n")
        refusal = self._refused(lambda: self._force(service),
                                "lane_workspace_inspection_unavailable")
        self.assertIn(warning, refusal.detail)
        self.assertTrue(workspace.exists())
        self.assertEqual(["open"], [record["state"] for record in self.fixture._records()])

    def test_failed_runtime_observer_cannot_be_forced(self):
        """Catches treating an observer failure as proof that no process uses a lane."""
        service, workspace = self._open()
        self.fixture.lsof.write_text("#!/bin/sh\nprintf 'fixture observer failed\\n' >&2\nexit 2\n")
        self._refused(lambda: self._force(service))
        self.assertTrue(workspace.exists())
        self.assertEqual(["open"], [record["state"] for record in self.fixture._records()])

    def test_sweep_preflights_dirty_last_lane_before_removing_clean_first(self):
        """Catches partial deletion followed by a pre-mutation dirty refusal."""
        service, clean = self._open("a-clean")
        dirty = Path(service.open(actor="builder", row="z-dirty", repo="fixture")["workspace"])
        (dirty / "tracked.txt").write_text("uncommitted fixture work\n")
        self.fixture._state("lane-board.json", {
            "schema_version": 0, "rows": {"a-clean": "landed", "z-dirty": "struck"},
        })
        self._refused(lambda: service.sweep(apply=True), "lane_workspace_dirty")
        self.assertTrue(clean.exists())
        self.assertEqual("uncommitted fixture work\n", (dirty / "tracked.txt").read_text())
        self.assertEqual(["open", "open"], [record["state"] for record in self.fixture._records()])

    def test_local_tag_does_not_prove_remote_reachability(self):
        """Catches using all refs to authorize removal of a never-pushed commit."""
        service, workspace = self._open()
        (workspace / "tracked.txt").write_text("local-only commit\n")
        self.fixture._git(workspace, "add", "tracked.txt")
        self.fixture._git(workspace, "commit", "--quiet", "-m", "fixture local only")
        self.fixture._git(workspace, "tag", "fixture-local-tag")
        self._refused(lambda: service.close(actor="builder", row="safety-row"),
                      "lane_workspace_unpushed")
        self.assertTrue(workspace.exists())

    def test_missing_board_never_infers_row_eligibility(self):
        """Catches treating missing row testimony as a landed board row."""
        service, workspace = self._open()
        (self.fixture.root.path / "state/lane-board.json").unlink()
        result = service.sweep(apply=True)
        self.assertEqual([], result["eligible"])
        self.assertEqual([], result["closed"])
        self.assertTrue(workspace.exists())

    def test_malformed_board_refuses_before_removal(self):
        """Catches accepting conflicting terminal states through duplicate JSON keys."""
        service, workspace = self._open()
        (self.fixture.root.path / "state/lane-board.json").write_text(
            '{"schema_version":0,"rows":{"safety-row":"open","safety-row":"landed"}}\n')
        self._refused(lambda: service.sweep(apply=True))
        self.assertTrue(workspace.exists())

    def test_symlinked_lanes_root_cannot_authorize_removal(self):
        """Catches following a replacement root symlink to an owned worktree."""
        service, workspace = self._open()
        original_root = self.fixture.lanes_root
        moved_root = self.fixture.base / "moved-lanes"
        original_root.rename(moved_root)
        original_root.symlink_to(moved_root, target_is_directory=True)
        self._refused(lambda: self._force(service))
        self.assertTrue((moved_root / "builder/work/safety-row/tracked.txt").is_file())

    def test_changed_root_declaration_does_not_adopt_same_relative_path(self):
        """Catches configuration replacement redirecting an old ownership record."""
        service, workspace = self._open()
        replacement = self.fixture.base / "replacement-lanes"
        foreign = replacement / "builder/work/safety-row"
        foreign.mkdir(parents=True)
        (foreign / "keep.bin").write_bytes(b"replacement is not owned")
        self.fixture._state("lanes-root.json", {"schema_version": 0, "path": str(replacement)})
        self._refused(lambda: self._force(service))
        self.assertTrue(workspace.exists())
        self.assertEqual(b"replacement is not owned", (foreign / "keep.bin").read_bytes())

    def test_doctor_counts_loose_files_at_root_and_structural_ancestors(self):
        """Catches omitting unmanaged bytes outside foreign subdirectories."""
        service, _workspace = self._open()
        (self.fixture.lanes_root / "dump.bin").write_bytes(b"root!")
        (self.fixture.lanes_root / "builder/work/dump.bin").write_bytes(b"parent")
        observed = service.doctor()
        self.assertEqual(11, observed["unmanaged_bytes"])

    def test_doctor_includes_registered_node_without_lanes(self):
        """Catches a monitoring row disappearing merely because no lane exists."""
        observed = self.fixture._service().doctor()
        row = next(row for row in observed["nodes"] if row["node_id"] == "builder")
        self.assertEqual(0, row["open_lanes"])
        self.assertIsNone(row["oldest_open_age_seconds"])

    def test_closed_lane_structural_directories_are_not_unmanaged_bytes(self):
        """Catches completed lane cleanup reporting its empty layout as foreign work."""
        service, _workspace = self._open()
        service.close(actor="builder", row="safety-row")
        observed = service.doctor()
        self.assertEqual(0, observed["unmanaged_bytes"])
        self.assertEqual(0, next(row for row in observed["nodes"] if row["node_id"] == "builder")["open_lanes"])

    def test_invalid_force_reason_refuses_before_removal(self):
        """Catches receipt validation happening after forced deletion."""
        service, workspace = self._open()
        for why in ("unsafe\u202ereason", "unsafe\x7freason"):
            with self.subTest(why=repr(why)):
                self._refused(lambda: service.close(actor="builder", row="safety-row",
                                                     force=True, why=why))
                self.assertTrue(workspace.exists())
        self.assertEqual(["open"], [record["state"] for record in self.fixture._records()])

    def test_sweep_receipt_capacity_is_checked_for_entire_batch(self):
        """Catches removing the first lane before discovering later receipts cannot fit."""
        service, first = self._open("a-first")
        second = Path(service.open(actor="builder", row="b-second", repo="fixture")["workspace"])
        self.fixture._state("lane-board.json", {
            "schema_version": 0, "rows": {"a-first": "landed", "b-second": "landed"},
        })
        with patch("floati.jsonl.MAX_LEDGER_RECORDS", 3):
            self._refused(lambda: service.sweep(apply=True))
        self.assertTrue(first.exists())
        self.assertTrue(second.exists())
        self.assertEqual(["open", "open"], [record["state"] for record in self.fixture._records()])

    def test_ignored_local_bytes_require_explicit_force(self):
        """Catches deleting ignored local work that Git omits from ordinary status."""
        fixture = self.fixture
        (fixture.repository / ".gitignore").write_text("local-notes/\n")
        fixture._git(fixture.repository, "add", ".gitignore")
        fixture._git(fixture.repository, "commit", "--quiet", "-m", "fixture ignore rule")
        fixture._git(fixture.repository, "update-ref", "refs/remotes/origin/main", "HEAD")
        service, workspace = self._open()
        local = workspace / "local-notes/keep.txt"
        local.parent.mkdir()
        local.write_text("uncommitted private work\n")
        self._refused(lambda: service.close(actor="builder", row="safety-row"),
                      "lane_workspace_dirty")
        self.assertEqual("uncommitted private work\n", local.read_text())


if __name__ == "__main__":
    unittest.main()

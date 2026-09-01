from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from floati.errors import ProtocolRefusal
from floati.root import FloatiRoot


NOW = datetime(2026, 8, 31, 0, 30, 0, tzinfo=timezone.utc)


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


class SandboxProbeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = FloatiRoot.open_direct_home(self.base / "alpha", create=True)
        for relative in ("cursors", "receipts/deliveries", "receipts"):
            self.root.resolve_relative(relative).mkdir(parents=True, exist_ok=True)
        self.repo = self.base / "repo"
        self.repo.mkdir()
        _git(self.repo, "init", "--quiet")

    def test_s1_all_five_coordinates_complete_write_cycle_without_residue(self) -> None:
        from floati.sandbox_probe import probe_write_set

        before = sorted(str(path.relative_to(self.base)) for path in self.base.rglob("*"))
        facts = probe_write_set(self.root, "builder-a", repository=self.repo, now=NOW)
        after = sorted(str(path.relative_to(self.base)) for path in self.base.rglob("*"))

        self.assertEqual(
            ["bus_cursors", "bus_receipts", "bus_ledger", "git_common_dir", "git_worktree_admin_dir"],
            [fact["coordinate"] for fact in facts],
        )
        self.assertTrue(all(fact["verdict"] == "writable" for fact in facts))
        self.assertTrue(all(fact["residue_path"] is None for fact in facts))
        self.assertEqual(before, after)
        self.assertTrue(all(fact["observed_at"] == "2026-08-31T00:30:00.000Z" for fact in facts))

    def test_s4_absent_and_underivable_coordinates_are_unknown_not_writable(self) -> None:
        from floati.sandbox_probe import probe_write_set

        self.root.resolve_relative("cursors").rmdir()
        absent = probe_write_set(self.root, "builder-a", repository=self.repo, now=NOW)
        absent_cursor = next(fact for fact in absent if fact["coordinate"] == "bus_cursors")
        self.assertEqual(("unknown", "path_absent"), (absent_cursor["verdict"], absent_cursor["reason_code"]))

        underivable = probe_write_set(self.root, "builder-a", now=NOW)
        git_facts = [fact for fact in underivable if fact["coordinate"].startswith("git_")]
        self.assertEqual({"unknown"}, {fact["verdict"] for fact in git_facts})
        self.assertEqual({"coordinate_underivable"}, {fact["reason_code"] for fact in git_facts})

    def test_s6_naive_clock_refuses(self) -> None:
        from floati.sandbox_probe import probe_write_set

        with self.assertRaises(ProtocolRefusal) as caught:
            probe_write_set(self.root, "builder-a", repository=self.repo, now=datetime(2026, 8, 31))
        self.assertEqual("time_invalid", caught.exception.code)

    def test_s3_linked_worktree_keeps_common_and_admin_coordinates_distinct(self) -> None:
        from floati.sandbox_probe import probe_write_set

        _git(self.repo, "commit", "--allow-empty", "--quiet", "-m", "root")
        linked = self.base / "linked"
        _git(self.repo, "worktree", "add", "--quiet", str(linked))
        common = Path(_git(linked, "rev-parse", "--git-common-dir")).resolve()
        admin = Path(_git(linked, "rev-parse", "--git-dir")).resolve()
        self.assertNotEqual(common, admin)

        original_open = os.open

        def refuse_common(path, flags, mode=0o777, *, dir_fd=None):
            candidate = Path(path)
            if candidate.parent == common:
                raise PermissionError(1, "operation not permitted", str(path))
            return original_open(path, flags, mode, dir_fd=dir_fd) if dir_fd is not None else original_open(path, flags, mode)

        with patch("floati.sandbox_probe.os.open", side_effect=refuse_common):
            facts = probe_write_set(self.root, "builder-a", repository=linked, now=NOW)
        by_coordinate = {fact["coordinate"]: fact for fact in facts}
        self.assertEqual("refused", by_coordinate["git_common_dir"]["verdict"])
        self.assertEqual("permission_denied", by_coordinate["git_common_dir"]["reason_code"])
        self.assertEqual("writable", by_coordinate["git_worktree_admin_dir"]["verdict"])
        self.assertEqual(str(common), by_coordinate["git_common_dir"]["path"])
        self.assertEqual(str(admin), by_coordinate["git_worktree_admin_dir"]["path"])

    def test_s5_unlink_failure_keeps_writable_fact_and_names_residue(self) -> None:
        from floati.sandbox_probe import probe_write_set

        real_unlink = os.unlink

        def refuse_probe(path, *, dir_fd=None):
            if Path(path).name.startswith(".floati-write-probe-"):
                raise PermissionError(1, "operation not permitted", str(path))
            return real_unlink(path, dir_fd=dir_fd) if dir_fd is not None else real_unlink(path)

        with patch("floati.sandbox_probe.os.unlink", side_effect=refuse_probe):
            facts = probe_write_set(self.root, "builder-a", now=NOW)
        bus = next(fact for fact in facts if fact["coordinate"] == "bus_cursors")
        self.assertEqual("writable", bus["verdict"])
        self.assertIsNotNone(bus["residue_path"])

    def test_s9_concurrent_probes_have_unique_clean_results(self) -> None:
        from floati.sandbox_probe import probe_write_set

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: probe_write_set(self.root, "builder-a", now=NOW), range(2)))
        self.assertTrue(all(fact["verdict"] == "writable" for facts in results for fact in facts[:3]))


class SandboxRemedyTests(unittest.TestCase):
    def test_codex_remedy_names_paths_and_writable_roots(self) -> None:
        from floati.sandbox_remedy import remedy_for

        text = remedy_for("Codex", [Path("\x2ftmp/bus"), Path("\x2ftmp/git")])
        self.assertIn(".codex/config.toml", text)
        self.assertIn("writable_roots", text)
        self.assertIn("\x2ftmp/bus", text)
        self.assertIn("\x2ftmp/git", text)

    def test_unknown_harness_has_typed_absence_not_codex_fallback(self) -> None:
        from floati.sandbox_remedy import remedy_for

        text = remedy_for("unknown-harness", [Path("\x2ftmp/bus")])
        self.assertEqual("no verified remedy is recorded for this harness", text)
        self.assertNotIn("writable_roots", text)


class DoctorSandboxFlagTests(unittest.TestCase):
    def test_sandbox_checks_default_and_only_no_sandbox_disables(self) -> None:
        from floati.cli import _parser

        parser = _parser()
        parsed = parser.parse_args(["doctor", "--source", "\x2ftmp/source"])
        self.assertFalse(parsed.no_sandbox)
        parsed = parser.parse_args(["doctor", "--source", "\x2ftmp/source", "--no-sandbox"])
        self.assertTrue(parsed.no_sandbox)
        with self.assertRaises(ProtocolRefusal):
            parser.parse_args(["doctor", "--source", "\x2ftmp/source", "--sandbox"])


if __name__ == "__main__":
    unittest.main()

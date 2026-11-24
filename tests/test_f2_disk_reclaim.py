"""F2-DISK-RECLAIM-1: reclaim deletes only idle declared build output.

Am.7 / msg-01a12ce9eca67abc9a7d35a30e52f0f6: dry run by default; a probe
error means KEEP; a non-declared path or a claimed tree is kept; the
summary reports what df moved. Never run --apply on the live box.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from floati import fixture_ids as ids
from floati.reclaim import reclaim
from floati.registry import Registry
from floati.root import FloatiRoot
from tests.temp_roots import REAL_TEMP_ROOT
from tests.readiness import wait_process, wait_readable

IDLE_AGO = 48 * 3600
FRESH_AGO = 60
NOW = 1_700_000_000.0


def _write_home(onboarding: Path, name: str, home: Path) -> Path:
    onboarding.mkdir(parents=True, exist_ok=True)
    record = onboarding / (name + ".json")
    record.write_text(json.dumps({"home": str(home)}), encoding="utf-8")
    home.mkdir(parents=True, exist_ok=True)
    return home


def _plant_dir(parent: Path, name: str, *, age: float) -> Path:
    target = parent / name
    target.mkdir()
    artifact = target / "out.bin"
    artifact.write_bytes(b"\0" * 4096)
    stamped = NOW - age
    os.utime(artifact, (stamped, stamped))
    os.utime(target, (stamped, stamped))
    return target


class DiskReclaimTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.home = self.base / "fleet"
        self.root = FloatiRoot.open_direct_home(self.home, create=True)
        onboarding = self.home / "state" / "seat-onboarding"
        self.idle_tree = _write_home(onboarding, "idle", self.base / "idle-tree")
        self.active_tree = _write_home(onboarding, "active", self.base / "active-tree")
        self.undeclared_tree = _write_home(
            onboarding, "undeclared", self.base / "undeclared-tree"
        )
        self.claimed_tree = _write_home(onboarding, "claimed", self.base / "claimed-tree")
        self.probe_tree = _write_home(onboarding, "probe", self.base / "probe-tree")
        self.idle_build = _plant_dir(self.idle_tree, ".build", age=IDLE_AGO)
        self.active_build = _plant_dir(self.active_tree, ".build", age=FRESH_AGO)
        self.undeclared = _plant_dir(self.undeclared_tree, "secret-cache", age=IDLE_AGO)
        self.claimed_build = _plant_dir(self.claimed_tree, ".build", age=IDLE_AGO)
        self.probe_build = _plant_dir(self.probe_tree, ".build", age=IDLE_AGO)
        (self.home / "state" / "reclaim-declarations.v0.json").write_text(
            json.dumps({"schema_version": 0, "paths": [".build"]}),
            encoding="utf-8",
        )

    def _kwargs(self) -> dict:
        def newest_mtime(path: Path) -> float:
            if path.resolve() == self.probe_build.resolve():
                raise OSError("bfs -newermt refused")
            newest = 0.0
            for dirpath, _dirnames, filenames in os.walk(path):
                for name in filenames:
                    newest = max(newest, os.path.getmtime(Path(dirpath) / name))
            return newest

        return {
            "now": NOW,
            "process_cwds": (),
            "claimed_trees": (self.claimed_tree.resolve(),),
            "newest_mtime": newest_mtime,
            "statvfs": lambda _path: type("vfs", (), {"f_bavail": 100, "f_frsize": 1})(),
        }

    def test_dry_run_lists_only_the_idle_declared_dir_and_deletes_nothing(self) -> None:
        result = reclaim(self.root, apply=False, **self._kwargs())
        listed = [Path(row["path"]).resolve() for row in result["listed"]]
        self.assertEqual([self.idle_build.resolve()], listed)
        self.assertTrue(result["dry_run"])
        self.assertEqual(0, result["df_moved_bytes"])
        for path in (
            self.idle_build,
            self.active_build,
            self.undeclared,
            self.claimed_build,
            self.probe_build,
        ):
            self.assertTrue(path.is_dir(), path)

    def test_apply_removes_only_the_idle_declared_dir(self) -> None:
        result = reclaim(self.root, apply=True, **self._kwargs())
        deleted = [Path(row["path"]).resolve() for row in result["deleted"]]
        self.assertEqual([self.idle_build.resolve()], deleted)
        self.assertFalse(self.idle_build.exists())
        self.assertTrue(self.active_build.is_dir())
        self.assertTrue(self.undeclared.is_dir())
        self.assertTrue(self.claimed_build.is_dir())
        self.assertTrue(self.probe_build.is_dir())
        kept_reasons = {row["reason"]: Path(row["path"]).resolve() for row in result["kept"]}
        self.assertEqual(self.active_build.resolve(), kept_reasons["recent_write"])
        self.assertEqual(self.undeclared.resolve(), kept_reasons["undeclared"])
        self.assertEqual(self.claimed_build.resolve(), kept_reasons["claimed"])
        self.assertEqual(self.probe_build.resolve(), kept_reasons["probe_error"])
        self.assertIn("utc", result["deleted"][0])
        self.assertIn("bytes", result["deleted"][0])
        self.assertIn("rule", result["deleted"][0])

    def test_probe_error_keeps_by_name(self) -> None:
        result = reclaim(self.root, apply=True, **self._kwargs())
        kept = [row for row in result["kept"] if row["reason"] == "probe_error"]
        self.assertEqual(1, len(kept))
        self.assertEqual(self.probe_build.resolve(), Path(kept[0]["path"]).resolve())
        self.assertTrue(self.probe_build.is_dir())

    def _plant_live_board_claim(self, workspace: Path) -> None:
        from floati.codex_wait_contract import (
            CodexWaitConsentLedger,
            CodexWaitSessionLedger,
            resolve_participant,
        )

        node = ids.builder("floati")
        Registry(self.root).register(node, "worker")
        map_path = self.home / "codex-wait" / "workspaces.v0.json"
        map_path.parent.mkdir(parents=True, exist_ok=True)
        map_path.write_text(
            json.dumps(
                {
                    "schema_version": 0,
                    "tenant_id": self.home.name,
                    "mappings": [
                        {"workspace": str(workspace.resolve()), "node_id": node}
                    ],
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n",
            encoding="utf-8",
        )
        participant = resolve_participant(self.home, workspace)
        self.assertIsNotNone(participant)
        assert participant is not None
        consent = CodexWaitConsentLedger(self.root).arm(
            participant.binding,
            hook_timeout_seconds=10,
            wait_deadline_seconds=2,
            idempotency_key="reclaim-live-claim-consent",
        )
        CodexWaitSessionLedger(self.root).arm(
            participant.binding,
            consent,
            "reclaim-live-session",
            idempotency_key="reclaim-live-claim-arm",
        )

    def test_production_apply_keeps_idle_pycache_under_a_live_board_claim(self) -> None:
        import contextlib
        import io
        import time

        from floati.cli import main

        pycache = _plant_dir(self.claimed_tree, "__pycache__", age=IDLE_AGO)
        stamped = time.time() - IDLE_AGO
        for path in (pycache, pycache / "out.bin", self.claimed_build, self.claimed_build / "out.bin"):
            os.utime(path, (stamped, stamped))
        (self.home / "state" / "reclaim-declarations.v0.json").write_text(
            json.dumps({"schema_version": 0, "paths": [".build", "__pycache__"]}),
            encoding="utf-8",
        )
        self._plant_live_board_claim(self.claimed_tree)

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            rc = main(["reclaim", "--root", str(self.root.path), "--apply"])
        artifact = json.loads(output.getvalue())
        self.assertEqual(0, rc, artifact)
        self.assertTrue(pycache.is_dir(), artifact)
        self.assertTrue(self.claimed_build.is_dir(), artifact)
        kept = {
            Path(row["path"]).resolve()
            for row in artifact["evidence"]["kept"]
            if row.get("reason") == "claimed"
        }
        self.assertIn(pycache.resolve(), kept)

    def test_production_apply_keeps_a_tree_with_a_live_process_cwd(self) -> None:
        import contextlib
        import io
        import subprocess
        import sys
        import time

        from floati.cli import main

        stamped = time.time() - IDLE_AGO
        for path in (self.idle_build, self.idle_build / "out.bin"):
            os.utime(path, (stamped, stamped))
        child = subprocess.Popen(
            [sys.executable, "-B", "-c",
             "import os,time; os.write(1,b'ready\\n'); time.sleep(120)"],
            cwd=str(self.idle_tree), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
        )

        def _stop_child() -> None:
            child.kill()
            wait_process(child, "live cwd fixture collection")
            child.stdout.close()

        self.addCleanup(_stop_child)
        wait_readable(child.stdout, "live cwd fixture first frame")
        self.assertEqual(b"ready\n", child.stdout.readline())
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            rc = main(["reclaim", "--root", str(self.root.path), "--apply"])
        artifact = json.loads(output.getvalue())
        self.assertEqual(0, rc, artifact)
        self.assertTrue(self.idle_build.is_dir(), artifact)
        kept = {
            Path(row["path"]).resolve()
            for row in artifact["evidence"]["kept"]
            if row.get("reason") == "process_cwd"
        }
        self.assertIn(self.idle_build.resolve(), kept)

    def test_reclaim_verb_is_dry_run_by_default(self) -> None:
        import contextlib
        import io

        from floati.cli import main

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            rc = main(["reclaim", "--root", str(self.root.path)])
        artifact = json.loads(output.getvalue())
        self.assertEqual(0, rc, artifact)
        self.assertTrue(artifact["evidence"]["dry_run"])
        self.assertEqual([], artifact["evidence"]["deleted"])
        self.assertTrue(self.idle_build.is_dir())


if __name__ == "__main__":
    unittest.main()

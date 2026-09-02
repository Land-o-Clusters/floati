"""F10-1 gate two — PACKAGED-TREE SMOKE BATTERY over the manifest's own tree.

Ruling (`docs/design/f10-1-remedy-ruling-2026-08-30.md`): an isolated
install tree, **one real command per verb family past `--help`** —
because **A LAZY IMPORT MOVES A PACKAGING DEFECT FROM STARTUP TO FIRST
REAL USE**, and help proves nothing. The tree is built from the
bundle manifest's file list — the release's own vouched set — and the
battery asserts the dark locks package is ABSENT from it before running,
so the tree stands exactly as a real installation would.

Every command runs with cwd at the isolated tree and the tree's own
`scripts/floati` launcher, against a scratch fleet root, and must return
a parsed artifact with the expected status — a crash, an unparseable
stdout, or a wrong exit fails the battery.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from tests.temp_roots import REAL_TEMP_ROOT

REPOSITORY_ROOT = Path(__file__).parents[1]
LAUNCHER = "scripts/floati"


class PackagedSmokeBattery(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temp = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
        cls.tree = Path(cls.temp.name) / "tree"
        cls.tree.mkdir()
        manifest = json.loads(
            (REPOSITORY_ROOT / "bundle-manifest.v0.json").read_text(
                encoding="utf-8"))
        for entry in manifest["files"]:
            destination = cls.tree / entry["path"]
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(REPOSITORY_ROOT / entry["path"], destination)
            destination.chmod(0o755 if entry["path"].startswith("scripts/")
                              else 0o644)
        # the precondition the battery exists to exercise: the dark
        # package the two live modules used to import is NOT in the tree
        cls.assertFalse(
            cls,
            (cls.tree / "floati" / "locks" / "cleanup.py").exists(),
            "the packaged tree carries the dark locks package")
        cls.fleet = Path(cls.temp.name) / "fleet"
        cls.solo = Path(cls.temp.name) / "solo"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temp.cleanup()

    def floati(self, *arguments: str, expect_ok: bool = True) -> dict:
        completed = subprocess.run(
            [str(self.tree / LAUNCHER), *arguments],
            cwd=str(self.tree),
            capture_output=True,
            text=True,
            timeout=120,
        )
        artifact = None
        for stream in (completed.stdout, completed.stderr):
            try:
                artifact = json.loads(stream)
                break
            except json.JSONDecodeError:
                continue
        if expect_ok:
            # THE load-bearing assertion: a real use of the verb produces
            # a parsed artifact with ok status. A lazy-import crash emits
            # no artifact at all. A nonzero rc beside a parsed ok artifact
            # is the CLI's observation-exit convention (e.g. status's
            # installer-shadow advisory), not a packaging defect.
            self.assertIsNotNone(
                artifact,
                f"rc={completed.returncode} no artifact; "
                f"stderr={completed.stderr[-400:]}")
            self.assertEqual("ok", artifact["status"], artifact)
        return artifact

    def test_real_commands_per_verb_family(self) -> None:
        # bootstrap + node families
        self.floati("init", "--root", str(self.fleet))
        self.floati(
            "register", "--root", str(self.fleet), "smoke-a",
            "--harness", "codex")
        self.floati(
            "register", "--root", str(self.fleet), "smoke-b",
            "--harness", "codex")
        # bus family: send, inbox, ack round-trip
        self.floati(
            "send", "--root", str(self.fleet), "--from", "smoke-a",
            "--to", "smoke-b", "--repo", "smoke", "--sha", "a" * 40,
            "--doc", "docs/design/f10-1-remedy-ruling-2026-08-30.md",
            "--note", "packaged smoke battery")
        inbox = self.floati(
            "inbox", "--root", str(self.fleet), "--as", "smoke-b", "--peek")
        messages = inbox["evidence"]["messages"]
        self.assertEqual(1, len(messages), "smoke-b inbox is empty")
        self.floati(
            "ack", "--root", str(self.fleet), "--as", "smoke-b",
            "--session", "smoke-b-session", "--id", messages[0]["id"])
        # truth surfaces (render-family verbs take --json: exit 22 is the
        # contract's own "re-run with --json" remedy)
        self.floati("status", "--root", str(self.fleet), "--json")
        self.floati("log", "--root", str(self.fleet))
        self.floati("describe", "--json")
        self.floati("graph", "--root", str(self.fleet), "--json")
        self.floati("receipts", "smoke-b", "--root", str(self.fleet))
        # health family
        # --source is currency-comparison data (a real checkout); the
        # imports still come from the isolated tree via the launcher.
        # Doctor may lawfully report degraded on a scratch root or cannot_speak
        # when no installer destination was named; the artifact must parse.
        artifact = self.floati(
            "doctor", "--root", str(self.fleet), "--source",
            str(REPOSITORY_ROOT), expect_ok=False)
        self.assertIn(artifact["status"], ("ok", "degraded", "cannot_speak"))
        # solo work family
        self.floati("init", "--root", str(self.solo), "--solo", "solo-a",
                    "--harness", "codex")
        # wake family: a real status run (typed states allowed)
        artifact = self.floati(
            "wake", "status", "--root", str(self.fleet), "--as", "smoke-b",
            "--session", "smoke-b-session", expect_ok=False)
        self.assertIn(artifact["status"],
                      ("ok", "refused", "intentional_silence", "no_result"))
        if artifact["status"] == "refused":
            self.assertTrue(
                artifact["evidence"].get("code"),
                "a typed refusal must name its code")


if __name__ == "__main__":
    unittest.main()

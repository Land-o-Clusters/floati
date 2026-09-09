"""UNINSTALL-LX-1: a scratch install must not leave files the uninstaller will not own."""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests.temp_roots import REAL_TEMP_ROOT


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = REPOSITORY_ROOT / "scripts" / "floati"
CAPTURE_SUBPROCESS_ENV = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin"}


class UninstallLinuxForeignFilesTests(unittest.TestCase):
    def test_launcher_does_not_write_bytecode_into_its_installed_tree(self) -> None:
        """The installed entrypoint must not compile into the tree it owns."""
        with tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT) as raw:
            installed = Path(raw) / "installed"
            shutil.copytree(REPOSITORY_ROOT / "floati", installed / "floati",
                            ignore=shutil.ignore_patterns("__pycache__"))
            (installed / "scripts").mkdir()
            shutil.copy2(LAUNCHER, installed / "scripts" / "floati")
            result = subprocess.run(
                [str(installed / "scripts" / "floati"), "--help"],
                cwd=raw, env=CAPTURE_SUBPROCESS_ENV, capture_output=True, text=True,
            )
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertEqual([], list(installed.rglob("*.pyc")))

    def test_committed_tree_install_dry_run_preserves_only_the_planted_foreign_file(
        self,
    ) -> None:
        """Catches PATH-only install/dry-run subprocesses writing __pycache__ into dest.

        SHOT-1-F1 bounded the frame; this asserts the population. Green on macOS.
        A box-leg RED with a printed list is the Linux measurement.
        """

        with tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT) as raw:
            base = Path(raw)
            clone = base / "source"
            destination = base / "installed"
            cloned = subprocess.run(
                ["/usr/bin/git", "clone", "--local", str(REPOSITORY_ROOT), str(clone)],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(0, cloned.returncode, cloned.stderr)
            install = subprocess.run(
                [
                    str(clone / "scripts" / "floati"),
                    "install",
                    "--source",
                    str(clone),
                    "--destination",
                    str(destination),
                    "--committed-tree",
                ],
                cwd=clone,
                env=CAPTURE_SUBPROCESS_ENV,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(0, install.returncode, install.stderr or install.stdout)
            planted = destination / "foreign.keep"
            planted.write_text("not owned by Floati\n", encoding="utf-8")
            dry_run = subprocess.run(
                [
                    str(destination / "scripts" / "floati"),
                    "uninstall",
                    "--destination",
                    str(destination),
                    "--dry-run",
                ],
                cwd=clone,
                env=CAPTURE_SUBPROCESS_ENV,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(0, dry_run.returncode, dry_run.stderr or dry_run.stdout)
            artifact = json.loads(dry_run.stdout)
            foreign = artifact["evidence"]["foreign_preserved"]
            self.assertEqual(["foreign.keep"], foreign, foreign)


if __name__ == "__main__":
    unittest.main()

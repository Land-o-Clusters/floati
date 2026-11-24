"""MK-D1 must photograph the same real handoff after an unrelated commit."""

import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests.temp_roots import REAL_TEMP_ROOT


ROOT = Path(__file__).resolve().parents[1]


class MkD1RepeatabilityTests(unittest.TestCase):
    @unittest.skipUnless(importlib.util.find_spec("PIL"), "Pillow is not installed")
    def test_capture_resolves_in_an_independent_export_history(self):
        """Catches a harbor-only object ID leaking into exported capture readers."""
        with tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT) as temporary:
            base = Path(temporary).resolve()
            checkout = base / "checkout"
            checkout.mkdir()
            shutil.copytree(ROOT / "floati", checkout / "floati",
                            ignore=shutil.ignore_patterns("__pycache__"))
            relatives = (
                "scripts/capture-mk-d1.py", "scripts/capture-readme-real-ledgers.py",
                "scripts/capture-tui-moments.py", "docs/demo/recipes/mk-d1.md",
                "tests/fixtures/floati-capture-mono.ttf",
            )
            for relative in relatives:
                destination = checkout / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(ROOT / relative, destination)
            environment = {
                **os.environ,
                "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null",
                "GIT_AUTHOR_NAME": "Export Fixture",
                "GIT_AUTHOR_EMAIL": "export@example.invalid",
                "GIT_COMMITTER_NAME": "Export Fixture",
                "GIT_COMMITTER_EMAIL": "export@example.invalid",
            }

            def git(*arguments):
                return subprocess.run(
                    ["/usr/bin/git", "-c", "core.hooksPath=/dev/null", *arguments],
                    cwd=checkout, env=environment, capture_output=True, text=True,
                    check=True, timeout=60,
                ).stdout.strip()

            git("init", "--quiet")
            git("add", "--", "floati", *relatives)
            git("commit", "--quiet", "-m", "independent export fixture")
            source = git("rev-parse", "HEAD")
            git("update-ref", "refs/remotes/origin/main", source)
            output = base / "output"
            completed = subprocess.run(
                [sys.executable, "-B", str(ROOT / "scripts/capture-mk-d1.py"),
                 "--handoff-repository", str(checkout),
                 "--font", str(checkout / "tests/fixtures/floati-capture-mono.ttf"),
                 "--captured-at", "2026-10-02T23:27:00.000Z",
                 "--source-sha", source, "--scratch", str(base / "scratch"),
                 "--output", str(output)],
                cwd=ROOT, env=environment, capture_output=True, text=True, timeout=120,
            )
            self.assertEqual(0, completed.returncode, completed.stderr)
            self.assertIn(source, (output / "mk-d1-standard.txt").read_text())

    @unittest.skipUnless(importlib.util.find_spec("PIL"), "Pillow is not installed")
    def test_capture_bytes_survive_a_different_banked_head(self):
        """Catches HEAD in the photograph and undeclared checkout-path exposure."""
        with tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT) as temporary:
            base = Path(temporary).resolve()
            checkout = base / "checkout"
            environment = {
                **os.environ,
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": "/dev/null",
                "GIT_AUTHOR_NAME": "Capture Fixture",
                "GIT_AUTHOR_EMAIL": "capture@example.invalid",
                "GIT_COMMITTER_NAME": "Capture Fixture",
                "GIT_COMMITTER_EMAIL": "capture@example.invalid",
            }

            def git(*arguments, cwd=checkout):
                return subprocess.run(
                    ["/usr/bin/git", "-c", "core.hooksPath=/dev/null", *arguments],
                    cwd=cwd, env=environment, capture_output=True, text=True,
                    check=True, timeout=60,
                ).stdout.strip()

            git("clone", "--shared", "--quiet", str(ROOT), str(checkout), cwd=base)
            # Exercise the working copy of the generator, including an uncommitted fix.
            shutil.copyfile(ROOT / "scripts/capture-mk-d1.py",
                            checkout / "scripts/capture-mk-d1.py")
            source = git("rev-parse", "HEAD")
            git("update-ref", "refs/remotes/origin/fixture-base", source)
            snapshots = []
            heads = []
            for index in range(2):
                if index:
                    git("commit", "--allow-empty", "--quiet", "-m", "unrelated fixture change")
                    git("update-ref", "refs/remotes/origin/fixture-next", "HEAD")
                heads.append(git("rev-parse", "HEAD"))
                output = base / f"output-{index}"
                completed = subprocess.run(
                    [sys.executable, "-B", "scripts/capture-mk-d1.py",
                     "--font", str(checkout / "tests/fixtures/floati-capture-mono.ttf"),
                     "--captured-at", "2026-10-02T23:27:00.000Z",
                     "--source-sha", source,
                     "--scratch", str(base / f"scratch-{index}"),
                     "--output", str(output)],
                    cwd=checkout, env=environment, capture_output=True, text=True,
                    timeout=120,
                )
                self.assertEqual(0, completed.returncode, completed.stderr)
                snapshots.append({path.name: path.read_bytes() for path in output.iterdir()})
            self.assertNotEqual(heads[0], heads[1])
            self.assertEqual({"manifest.json", "mk-d1-standard.txt", "mk-d1-dark.gif",
                              "mk-d1-light.gif"}, set(snapshots[0]))
            for name in snapshots[0]:
                self.assertTrue(snapshots[0][name] == snapshots[1][name], name)


if __name__ == "__main__":
    unittest.main()

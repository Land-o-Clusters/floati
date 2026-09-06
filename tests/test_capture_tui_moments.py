from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests.temp_roots import REAL_TEMP_ROOT

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_FONT = REPOSITORY_ROOT / "tests" / "fixtures" / "floati-capture-mono.ttf"


class CaptureTuiMomentsTests(unittest.TestCase):
    """CAP-4: the TUI moment generator runs real moments and exposes them safe."""

    def setUp(self) -> None:
        self.temporary = Path(tempfile.mkdtemp(dir=REAL_TEMP_ROOT))
        self.addCleanup(shutil.rmtree, self.temporary, True)
        self.scratch = self.temporary / "scratch"
        self.output = self.temporary / "output"

    def _capture_env(self) -> dict[str, str]:
        return {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "PYTHONDONTWRITEBYTECODE": "1",
            "HOME": os.environ.get("HOME", "/var/empty"),
            "FLOATI_CAPTURE_FONT": str(FIXTURE_FONT),
        }

    def _capture(self, *moments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable,
                "scripts/capture-tui-moments.py",
                "--scratch", str(self.scratch),
                "--output", str(self.output),
                "--source-sha", "d" * 40,
                *(
                    argument
                    for moment in moments
                    for argument in ("--capture", moment)
                ),
            ],
            cwd=str(REPOSITORY_ROOT),
            env=self._capture_env(),
            capture_output=True,
            text=True,
            timeout=600,
        )

    def test_fixture_declares_the_repo_shipped_font(self) -> None:
        """CAP-4-F1: this capture family never depends on host fonts.

        The declared face is the repo-shipped fixture font, so the family
        runs identically on a Mac and on the Linux box.
        """

        self.assertTrue(
            FIXTURE_FONT.is_file(),
            f"repo-shipped fixture font is missing: {FIXTURE_FONT}",
        )
        self.assertEqual(
            str(FIXTURE_FONT), self._capture_env()["FLOATI_CAPTURE_FONT"]
        )

    def test_font_absence_without_candidates_is_typed(self) -> None:
        """CAP-4-F1, Linux semantics: no declaration, no readable candidate.

        The tool refuses with the typed absence reason naming the two
        declaration channels; the pin runs everywhere and is never skipped.
        """

        import importlib.util

        script = REPOSITORY_ROOT / "scripts" / "capture-readme-real-ledgers.py"
        spec = importlib.util.spec_from_file_location("capture_readme_engine", script)
        self.assertIsNotNone(spec and spec.loader)
        engine = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(engine)

        with self.assertRaises(engine.ProtocolRefusal) as raised:
            engine.resolve_capture_font(
                environ={},
                candidates=[REPOSITORY_ROOT / "docs" / "no-such-font.ttf"],
            )
        self.assertEqual(engine.FONT_ABSENT_CODE, raised.exception.code)
        self.assertIn(engine.CAPTURE_FONT_FLAG, raised.exception.remedy)
        self.assertIn(engine.CAPTURE_FONT_VARIABLE, raised.exception.remedy)
        self.assertEqual(3, engine.FONT_ABSENT_EXIT_CODE)

    def test_board_idle_static_moment_carries_txt_and_both_themes(self) -> None:
        completed = self._capture("board-idle")

        self.assertEqual(0, completed.returncode, completed.stderr[-400:])
        names = sorted(path.name for path in self.output.iterdir())
        self.assertEqual(
            [
                "board-idle-dark.png",
                "board-idle-light.png",
                "board-idle-standard.txt",
                "manifest.json",
            ],
            names,
        )
        manifest = json.loads((self.output / "manifest.json").read_text())
        self.assertEqual(False, manifest["synthetic"])
        self.assertEqual(1, len(manifest["captures"]))
        record = manifest["captures"][0]
        self.assertEqual("board-idle", record["name"])
        self.assertEqual(False, record["animated"])
        self.assertIn("fixture fleet, real run", record["caption"])
        for entry in record["files"]:
            path = self.output / entry["path"]
            self.assertTrue(path.is_file(), entry["path"])
            self.assertEqual(64, len(entry["sha256"]))

    def test_replay_moment_is_animated_and_names_frames(self) -> None:
        completed = self._capture("replay-in-flight")

        self.assertEqual(0, completed.returncode, completed.stderr[-400:])
        manifest = json.loads((self.output / "manifest.json").read_text())
        record = manifest["captures"][0]
        self.assertEqual(True, record["animated"])
        self.assertGreater(record["frame_count"], 1)
        self.assertEqual(
            {"replay-in-flight-dark.gif", "replay-in-flight-light.gif",
             "replay-in-flight-standard.txt"},
            {entry["path"] for entry in record["files"]},
        )

    def test_existing_output_directory_is_refused(self) -> None:
        self.output.mkdir()
        (self.output / "occupied.txt").write_text("occupied\n")

        completed = self._capture("board-idle")

        self.assertNotEqual(0, completed.returncode)
        self.assertIn("output is not empty", completed.stderr)


if __name__ == "__main__":
    unittest.main()

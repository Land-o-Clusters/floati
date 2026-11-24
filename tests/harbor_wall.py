from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
STATES = ("idle", "live", "degraded", "replay", "graph", "install", "selftest")
MODES = ("standard", "plain")
THEMES = ("dark", "light")


class WallCaptureTests(unittest.TestCase):
    @staticmethod
    def _luminance(color: str) -> float:
        channels = [int(color[index : index + 2], 16) / 255 for index in (1, 3, 5)]
        linear = [
            value / 12.92
            if value <= 0.04045
            else ((value + 0.055) / 1.055) ** 2.4
            for value in channels
        ]
        return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]

    @classmethod
    def _contrast(cls, first: str, second: str) -> float:
        high, low = sorted(
            (cls._luminance(first), cls._luminance(second)), reverse=True
        )
        return (high + 0.05) / (low + 0.05)

    def _generate(self, destination: Path) -> None:
        result = subprocess.run(
            ["python3", "scripts/capture-wall.py", "--output", str(destination)],
            cwd=REPOSITORY_ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("", result.stderr)

    def test_generator_emits_complete_valid_deterministic_wall(self) -> None:
        with tempfile.TemporaryDirectory() as first_temp, tempfile.TemporaryDirectory() as second_temp:
            first = Path(first_temp)
            second = Path(second_temp)
            self._generate(first)
            self._generate(second)

            expected_stems = {
                f"{state}-{mode}-{theme}"
                for state in STATES
                for mode in MODES
                for theme in THEMES
            }
            self.assertEqual(expected_stems, {path.stem for path in first.glob("*.svg")})
            self.assertEqual(expected_stems, {path.stem for path in first.glob("*.txt")})
            for path in sorted(first.iterdir()):
                peer = second / path.name
                self.assertTrue(peer.is_file(), path.name)
                self.assertEqual(path.read_bytes(), peer.read_bytes(), path.name)
            for path in first.glob("*.svg"):
                ET.parse(path)
                self.assertNotIn("\x1b", path.read_text(encoding="utf-8"))

    def test_wall_semantics_modes_themes_and_manifest_agree(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            wall = Path(temporary)
            self._generate(wall)
            required = {
                "idle": "NO NODES",
                "live": "DRIVING",
                "degraded": "DEGRADED",
                "replay": "FLIGHT RECORDER",
                "graph": "HARBOR CHART",
                "install": '"status":"installed"',
                "selftest": '"status":"bundle_verified"',
            }
            for state, marker in required.items():
                standard = (wall / f"{state}-standard-dark.txt").read_text(encoding="utf-8")
                self.assertIn(marker, standard)
            graph = (wall / "graph-standard-dark.txt").read_text(encoding="utf-8")
            self.assertIn("demo-architect", graph)
            for state in STATES:
                for mode in MODES:
                    dark = (wall / f"{state}-{mode}-dark.txt").read_bytes()
                    light = (wall / f"{state}-{mode}-light.txt").read_bytes()
                    self.assertEqual(dark, light)
                    self.assertNotEqual(
                        (wall / f"{state}-{mode}-dark.svg").read_bytes(),
                        (wall / f"{state}-{mode}-light.svg").read_bytes(),
                    )
            manifest = json.loads((wall / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(0, manifest["schema_version"])
            self.assertEqual(28, len(manifest["captures"]))
            for capture in manifest["captures"]:
                svg = wall / capture["svg"]
                text = wall / capture["text"]
                self.assertEqual(hashlib.sha256(svg.read_bytes()).hexdigest(), capture["sha256_svg"])
                self.assertEqual(hashlib.sha256(text.read_bytes()).hexdigest(), capture["sha256_text"])

    def test_semantic_accents_have_contrast_and_plain_frames_stay_unadorned(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            wall = Path(temporary)
            self._generate(wall)
            namespace = {"svg": "http://www.w3.org/2000/svg"}
            palettes = {
                "dark": ("#12161c", "#d8dee9", "#ff9f43"),
                "light": ("#f7f3eb", "#20252c", "#853d07"),
            }
            for theme, (expected_background, expected_foreground, expected_accent) in palettes.items():
                root = ET.parse(wall / f"degraded-standard-{theme}.svg").getroot()
                background = root.find("svg:rect", namespace).attrib["fill"]
                header = root.find("svg:text/svg:tspan", namespace)
                accents = [
                    element
                    for element in root.findall(".//svg:tspan", namespace)
                    if element.attrib.get("fill") == expected_accent
                ]

                self.assertEqual(expected_background, background)
                self.assertEqual(expected_foreground, header.attrib["fill"])
                self.assertTrue(accents)
                self.assertTrue(any("DENIAL" in "".join(element.itertext()) for element in accents))
                self.assertGreaterEqual(self._contrast(expected_accent, background), 4.5)

            plain = (wall / "degraded-plain-light.svg").read_text(encoding="utf-8")
            self.assertNotIn('fill="#853d07"', plain)
            self.assertEqual(
                (wall / "live-standard-dark.txt").read_bytes(),
                (wall / "live-standard-light.txt").read_bytes(),
            )


if __name__ == "__main__":
    unittest.main()

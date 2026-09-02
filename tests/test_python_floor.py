"""PY-1: one canonical Python floor and an explicit zero-dependency promise."""

from __future__ import annotations

import re
import unittest
from pathlib import Path

from floati import __version__


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


class PythonFloorContractTests(unittest.TestCase):
    def test_project_metadata_declares_python_floor_and_zero_dependencies(self) -> None:
        """Catches a release that leaves packaging frontends to guess compatibility."""
        metadata = REPOSITORY_ROOT / "pyproject.toml"
        self.assertTrue(metadata.is_file(), "pyproject.toml must declare the Python floor")
        text = metadata.read_text(encoding="utf-8")

        self.assertEqual(
            [
                "[project]",
                'name = "floati"',
                f'version = "{__version__}"',
                'requires-python = ">=3.9"',
                "dependencies = []",
            ],
            [line for line in text.splitlines() if line],
        )

    def test_operator_documents_name_pyproject_as_the_floor_authority(self) -> None:
        """Catches prose drifting back to an unbound tested-on claim."""
        for relative in ("README.md", "AGENTS.md"):
            with self.subTest(path=relative):
                text = (REPOSITORY_ROOT / relative).read_text(encoding="utf-8")
                self.assertRegex(
                    text,
                    re.compile(
                        r"pyproject\.toml[^\n]*Python 3\.9 or newer",
                        re.IGNORECASE,
                    ),
                )


if __name__ == "__main__":
    unittest.main()

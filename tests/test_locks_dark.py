from __future__ import annotations

from tests.test_cli import LAUNCHER

import json
import re
import subprocess
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
LOCKS_EXPECTED_WIRED = False
_LOCKS_COMMAND = re.compile(
    r"(?m)(?:^|\s)(?:python3\s+-m\s+floati|(?:\./)?scripts/floati|floati)\s+locks(?:\s|$)"
)


class LocksDarkFenceTests(unittest.TestCase):
    def cli_resolves_locks(self) -> bool:
        completed = subprocess.run(
            [str(LAUNCHER), "locks"],
            cwd=REPOSITORY_ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        if completed.returncode == 0:
            return True
        try:
            artifact = json.loads(completed.stdout)
        except (json.JSONDecodeError, TypeError):
            return True
        return not (
            completed.returncode == 20
            and artifact.get("status") == "refused"
            and artifact.get("evidence", {}).get("code") == "arguments_invalid"
        )

    def readme_resolves_locks_command(self) -> bool:
        readme = (REPOSITORY_ROOT / "README.md").read_text(encoding="utf-8")
        fenced = "\n".join(re.findall(r"```[^\n]*\n(.*?)```", readme, re.DOTALL))
        return _LOCKS_COMMAND.search(fenced) is not None

    def bundle_contains_locks_package(self) -> bool:
        manifest = json.loads(
            (REPOSITORY_ROOT / "bundle-manifest.v0.json").read_text(encoding="utf-8")
        )
        return any(
            entry.get("path", "").startswith("floati/locks/")
            for entry in manifest.get("files", [])
        )

    def public_script_resolves_locks(self) -> bool:
        scripts = REPOSITORY_ROOT / "scripts"
        for path in scripts.iterdir():
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8")
            if "floati.locks" in text or _LOCKS_COMMAND.search(text) is not None:
                return True

        public_entry_points = (
            scripts / "floati",
            scripts / "capture-demo-assets.py",
        )
        for path in public_entry_points:
            completed = subprocess.run(
                [str(path), "--help"],
                cwd=REPOSITORY_ROOT,
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )
            if completed.returncode != 0:
                return True
            if _LOCKS_COMMAND.search(completed.stdout + completed.stderr) is not None:
                return True
        return False

    def test_locks_public_surface_matches_permanent_wiring_expectation(self) -> None:
        """Catches any partial public activation or accidental removal of the DARK fence."""

        observed = {
            "cli": self.cli_resolves_locks(),
            "readme": self.readme_resolves_locks_command(),
            "bundle": self.bundle_contains_locks_package(),
            "scripts": self.public_script_resolves_locks(),
        }
        self.assertEqual({key: LOCKS_EXPECTED_WIRED for key in observed}, observed)


if __name__ == "__main__":
    unittest.main()

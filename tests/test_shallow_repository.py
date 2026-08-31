from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from floati.deploy import DeploymentWriter
from floati.doctor import Doctor
from floati.errors import ProtocolRefusal


class ShallowRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.seed = self.base / "seed"
        self.seed.mkdir()
        self._git(self.seed, "init", "--quiet", "--initial-branch=main")
        self._git(self.seed, "config", "user.name", "Floati Test")
        self._git(self.seed, "config", "user.email", "floati-test@example.invalid")
        (self.seed / "one.txt").write_text("one\n", encoding="utf-8")
        self._git(self.seed, "add", "one.txt")
        self._git(self.seed, "commit", "--quiet", "-m", "one")
        (self.seed / "two.txt").write_text("two\n", encoding="utf-8")
        self._git(self.seed, "add", "two.txt")
        self._git(self.seed, "commit", "--quiet", "-m", "two")
        self.shallow = self.base / "shallow"
        subprocess.run(
            ["git", "clone", "--quiet", "--depth", "1", f"file://{self.seed}", str(self.shallow)],
            check=True,
            capture_output=True,
            text=True,
        )

    @staticmethod
    def _git(cwd: Path, *args: str) -> str:
        return subprocess.run(
            ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
        ).stdout.strip()

    def test_real_depth_one_clone_is_detected(self) -> None:
        from floati.git_process import is_shallow_repository

        self.assertTrue(is_shallow_repository(self.shallow))
        self.assertFalse(is_shallow_repository(self.seed))

    def test_committed_tree_refuses_before_any_deployment_write(self) -> None:
        writer = DeploymentWriter(self.shallow, self.base / "destination", "install", committed_tree=True)
        with self.assertRaises(ProtocolRefusal) as caught:
            writer._check_currency(self.shallow)
        self.assertEqual("deployment_shallow_repository", caught.exception.code)

    def test_doctor_reports_shallow_source_as_typed_finding(self) -> None:
        root = self.base / "fleet"
        root.mkdir()
        artifact, return_code = Doctor(self.shallow, root, ref="HEAD").artifact()
        finding = next(row for row in artifact["findings"] if row["code"] == "shallow_repository")
        self.assertEqual("warning", finding["severity"])
        self.assertIn(return_code, {33, 35})


if __name__ == "__main__":
    unittest.main()

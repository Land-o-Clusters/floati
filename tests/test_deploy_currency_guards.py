from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from floati.deploy import DeploymentWriter, _git
from floati.errors import DEPLOY_CURRENCY_REMEDY, ProtocolRefusal, UNNAMED_REMEDY
from tests.temp_roots import REAL_TEMP_ROOT


class DeploymentCurrencyGuardTests(unittest.TestCase):
    """Deployment currency refusals must identify the exact inspected ref."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.source = self.base / "source"
        self.destination = self.base / "destination"
        self.source.mkdir()
        self._git("init", "--quiet", "--initial-branch=lane/hm0")
        self._git("config", "user.name", "Floati Currency Test")
        self._git("config", "user.email", "floati-currency@example.invalid")
        self._write("floati/__init__.py", "VERSION = 'currency-test'\n")
        self._write("scripts/floati", "#!/bin/sh\nexit 0\n")
        (self.source / "scripts/floati").chmod(0o755)
        self._write_manifest()
        self._git("add", ".")
        self._git("commit", "--quiet", "-m", "currency source")
        git_directory = Path(shutil.which("git") or "").parent
        self.assertTrue((git_directory / "git").is_file())
        self.path = os.pathsep.join(
            (str(self.source / "scripts"), str(git_directory))
        )

    def _git(self, *args: str) -> str:
        completed = subprocess.run(
            ["git", *args],
            cwd=self.source,
            capture_output=True,
            text=True,
            check=True,
        )
        return completed.stdout.strip()

    def _write(self, relative: str, content: str) -> None:
        path = self.source / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def _write_manifest(self) -> None:
        files = []
        for relative in ("floati/__init__.py", "scripts/floati"):
            files.append(
                {
                    "path": relative,
                    "sha256": hashlib.sha256(
                        (self.source / relative).read_bytes()
                    ).hexdigest(),
                }
            )
        (self.source / "bundle-manifest.v0.json").write_text(
            json.dumps(
                {
                    "schema_version": 0,
                    "protocol_version": "0",
                    "canonical_ref": "refs/heads/lane/hm0",
                    "files": files,
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

    def test_named_ref_failure_identifies_the_exact_ref(self) -> None:
        """Catches a missing target ref collapsing to anonymous Git stderr."""

        with patch.dict(os.environ, {"PATH": self.path}, clear=False):
            with self.assertRaises(ProtocolRefusal) as raised:
                DeploymentWriter(
                    self.source,
                    self.destination,
                    "install",
                    ref="origin/main",
                ).run()

        self.assertEqual("deployment_currency_unavailable", raised.exception.code)
        self.assertIn("origin/main", raised.exception.detail)
        self._assert_ins2_currency_remedy(raised.exception.remedy)
        self.assertFalse(self.destination.exists())

    def test_committed_tree_mode_never_resolves_target_ref(self) -> None:
        """Catches committed-tree installs consulting an unavailable named ref."""

        with patch.dict(os.environ, {"PATH": self.path}, clear=False):
            result = DeploymentWriter(
                self.source,
                self.destination,
                "install",
                ref="origin/main",
                committed_tree=True,
            ).run()

        self.assertEqual("committed-tree-ci", result["currency_mode"])
        self.assertEqual("origin/main", result["ref"])
        self.assertEqual(self._git("rev-parse", "HEAD"), result["source_sha"])

    def _assert_ins2_currency_remedy(self, remedy: object) -> None:
        self.assertEqual(DEPLOY_CURRENCY_REMEDY, remedy)
        self.assertNotEqual(UNNAMED_REMEDY, remedy)
        self.assertIn("--ref", remedy)

    def _run_install(self, *, ref: str) -> None:
        with patch.dict(os.environ, {"PATH": self.path}, clear=False):
            DeploymentWriter(
                self.source,
                self.destination,
                "install",
                ref=ref,
            ).run()

    def test_dirty_tree_currency_refusal_names_the_ref_action(self) -> None:
        """Catches a dirty-tree currency refusal still carrying the placeholder remedy."""

        (self.source / "floati/__init__.py").write_text("VERSION = 'dirty'\n", encoding="utf-8")
        with self.assertRaises(ProtocolRefusal) as raised:
            self._run_install(ref="HEAD")
        self.assertEqual("deployment_currency_unavailable", raised.exception.code)
        self.assertIn("not clean", raised.exception.detail)
        self._assert_ins2_currency_remedy(raised.exception.remedy)
        self.assertFalse(self.destination.exists())

    def test_head_not_named_ref_currency_refusal_names_the_ref_action(self) -> None:
        """Catches HEAD-is-not-ref still omitting the tag-checkout --ref action."""

        self._git("tag", "v0.1.0")
        (self.source / "floati/__init__.py").write_text("VERSION = 'moved'\n", encoding="utf-8")
        self._write_manifest()
        self._git("add", ".")
        self._git("commit", "--quiet", "-m", "move HEAD off the tag")
        with self.assertRaises(ProtocolRefusal) as raised:
            self._run_install(ref="v0.1.0")
        self.assertEqual("deployment_currency_unavailable", raised.exception.code)
        self.assertIn("v0.1.0", raised.exception.detail)
        self._assert_ins2_currency_remedy(raised.exception.remedy)
        self.assertFalse(self.destination.exists())

    def test_git_inspect_oserror_currency_refusal_names_the_ref_action(self) -> None:
        """Catches a git OSError currency refusal still carrying the placeholder remedy."""

        with patch("floati.deploy.subprocess.run", side_effect=OSError("git missing")):
            with self.assertRaises(ProtocolRefusal) as raised:
                _git(self.source, ("status", "--porcelain=v1"), executable="/usr/bin/git")
        self.assertEqual("deployment_currency_unavailable", raised.exception.code)
        self.assertIn("git could not inspect", raised.exception.detail)
        self._assert_ins2_currency_remedy(raised.exception.remedy)


if __name__ == "__main__":
    unittest.main()

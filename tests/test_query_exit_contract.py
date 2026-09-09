"""Completed queries retain diagnostic absences without becoming failed commands."""
from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from floati.doctor import _fold_shadow_exit
from floati.mcp import run_cli_artifact
from floati.root import FloatiRoot


class QueryExitContractTests(unittest.TestCase):
    def test_status_and_watch_keep_missing_install_observation_as_evidence(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "fleet"
            FloatiRoot.open_direct_home(root, create=True)
            with patch.dict(os.environ, {"PATH": "/usr/bin:/bin"}, clear=True):
                for command, extra in (("status", ["--json"]),
                                       ("watch", ["--iterations", "1", "--interval", "0.05"])):
                    with self.subTest(command=command):
                        code, artifact = run_cli_artifact([command, "--root", str(root), *extra])
                        self.assertEqual(0, code, artifact)
                        self.assertEqual("ok", artifact["status"])
                        evidence = artifact["evidence"]
                        if command == "watch":
                            evidence = evidence["delta"]["snapshot"]
                        self.assertEqual("cannot_speak", evidence["installer_shadow"]["outcome"])

    def test_clear_install_observation_does_not_refuse_status(self):
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            root = base / "fleet"
            FloatiRoot.open_direct_home(root, create=True)
            install = base / "installed"
            scripts = install / "scripts"
            scripts.mkdir(parents=True)
            (scripts / "floati").write_text("fixture launcher\n")
            with patch.dict(os.environ, {"PATH": str(scripts)}, clear=True):
                code, artifact = run_cli_artifact([
                    "status", "--root", str(root), "--destination", str(install), "--json"])
            self.assertEqual("affirmative_none", artifact["evidence"]["installer_shadow"]["outcome"])
            self.assertEqual(0, code, artifact)

    def test_doctor_reports_incomplete_or_shadowed_install_as_degraded(self):
        for observation_exit in (0, 21, 22):
            with self.subTest(observation_exit=observation_exit):
                self.assertEqual(35, _fold_shadow_exit(0, observation_exit))
                self.assertEqual(33, _fold_shadow_exit(33, observation_exit))
        self.assertEqual(0, _fold_shadow_exit(0, 20))

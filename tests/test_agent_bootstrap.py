"""Worktree bootstrap and gateway pin diagnostics."""

import json
import tempfile
import unittest
from pathlib import Path

from scripts import agent_bootstrap


class AgentBootstrapTests(unittest.TestCase):
    def test_foreign_venv_is_refused_before_use(self):
        with tempfile.TemporaryDirectory() as bank:
            root = Path(bank).resolve()
            foreign = root / "foreign"
            foreign.mkdir()
            (root / ".venv").symlink_to(foreign, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, "Move .*bootstrapping"):
                agent_bootstrap.owned_venv(root)
            self.assertTrue((root / ".venv").is_symlink())

    def test_registry_reports_stale_executables_in_both_profiles(self):
        class ApprovalRequired(RuntimeError):
            pass

        def validate(path, label):
            path = Path(path)
            if not path.is_file() or path != path.resolve(strict=True):
                raise ApprovalRequired(f"{label} is missing")
            return path

        with tempfile.TemporaryDirectory() as bank:
            brew = Path(bank).resolve()
            (brew / "opt").mkdir()
            profiles = {}
            replacements = {}
            for profile, field, tool in (
                ("floati-harbor", "node_executable", "node"),
                ("peer-project", "gh_executable", "gh"),
            ):
                old = brew / f"Cellar/{tool}/old/bin/{tool}"
                new = brew / f"Cellar/{tool}/new/bin/{tool}"
                new.parent.mkdir(parents=True)
                new.write_text("#!/bin/sh\nprintf '%s\\n' v26.12.1\n")
                new.chmod(0o755)
                (brew / "opt" / tool).symlink_to(new.parents[1])
                profiles[profile] = {"tools": {field: str(old)}, "git_common_dirs": []}
                replacements[f"profiles.{profile}.tools.{field}"] = new
            gateway = {
                "PROFILE_REGISTRY": brew / "registry.json",
                "_load_registry_bytes": lambda: json.dumps({"profiles": profiles}).encode(),
                "_unique_object": dict,
                "_fixed_executable": validate,
                "ApprovalRequired": ApprovalRequired,
            }
            with self.assertRaises(ValueError) as blocked:
                agent_bootstrap.gateway_pins(gateway)
            for field, new in replacements.items():
                self.assertIn(field, str(blocked.exception))
                self.assertIn(str(new), str(blocked.exception))

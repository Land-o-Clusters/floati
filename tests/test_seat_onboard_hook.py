"""Hook prerequisites refuse scratch onboarding before any capability writes.

Unrun while the fleet BUILDS HOLD remains active.
"""

from __future__ import annotations

import json
import shlex
import unittest
from pathlib import Path

from floati.codex_hook_install import format_waiter_stop_command
from floati.codex_hook_trust import codex_hook_current_hash
from tests import test_seat_onboard as fixtures


class SeatOnboardingHookTests(unittest.TestCase):
    setUp = fixtures.SeatOnboardingTests.setUp
    git = fixtures.SeatOnboardingTests.git
    cli = fixtures.SeatOnboardingTests.cli
    add = fixtures.SeatOnboardingTests.add

    def assert_prerequisite_refuses_without_writes(self):
        root_before = {str(path.relative_to(self.root.path)): path.read_bytes()
                       for path in self.root.path.rglob("*") if path.is_file()}
        configuration_before = (self.config.read_bytes(), self.profiles.read_bytes())
        hooks_before = self.hooks.read_bytes() if self.hooks.exists() else None
        launcher_before = self.waiter_launcher.read_bytes()
        code, output, errors = self.add()
        self.assertEqual(20, code, errors or output)
        artifact = json.loads(output)
        self.assertEqual("refused", artifact["status"])
        self.assertIsInstance(artifact["evidence"]["code"], str)
        self.assertTrue(artifact["evidence"]["code"])
        self.assertEqual(configuration_before, (self.config.read_bytes(), self.profiles.read_bytes()))
        self.assertEqual(hooks_before, self.hooks.read_bytes() if self.hooks.exists() else None)
        self.assertEqual(launcher_before, self.waiter_launcher.read_bytes())
        root_after = {str(path.relative_to(self.root.path)): path.read_bytes()
                      for path in self.root.path.rglob("*") if path.is_file()}
        self.assertEqual(root_before, root_after)
        self.assertFalse(self.home.exists())
        self.assertEqual([], list(self.base.glob(".seat-add-*")))
        self.assertFalse(self.root.path.joinpath("state/seat-onboarding").exists())

    def test_absent_stop_hooks_file_refuses_without_configuration_or_registry_write(self):
        self.hooks.unlink()
        self.assert_prerequisite_refuses_without_writes()

    def test_matching_waiter_without_codex_trust_refuses_without_arm_or_clone(self):
        self.config.write_text('model = "fixture"\n')
        self.assert_prerequisite_refuses_without_writes()

    def test_trusted_waiter_for_another_root_refuses_without_adoption(self):
        other_root = self.base / "other-fleet"
        other_root.mkdir()
        block = {"hooks": [{"type": "command",
            "command": format_waiter_stop_command(self.waiter_launcher, other_root),
            "timeout": 300, "statusMessage": "Watching Floati bus"}]}
        self.hooks.write_text(json.dumps({"hooks": {"Stop": [block]}}) + "\n")
        self.config.write_text('model = "fixture"\n\n[hooks.state.'
            + json.dumps(f"{self.hooks}:stop:0:0") + "]\n"
            + "enabled = true\ntrusted_hash = "
            + json.dumps(codex_hook_current_hash(block)) + "\n")
        self.assert_prerequisite_refuses_without_writes()
        self.assertEqual([], list(other_root.iterdir()))

    def test_missing_hook_remedy_prepares_only_global_hook_then_names_host_trust(self):
        """A new seat must not need a dummy seat to prepare its global hook."""
        self.hooks.unlink()
        code, output, errors = self.add()
        self.assertEqual(20, code, errors or output)
        refusal = json.loads(output)["evidence"]
        self.assertEqual("seat_onboard_hook_unavailable", refusal["code"])
        self.assertTrue(refusal["remedy"].startswith("floati hook install"), refusal)
        command = shlex.split(refusal["remedy"])
        self.assertEqual(["floati", "hook", "install"], command[:3])
        self.assertIn("--prepare-only", command)
        self.assertIn("--acknowledge-stop-hook-change", command)
        self.assertNotIn("--workspace", command)
        self.assertNotIn("--as", command)
        self.assertEqual(str(Path.home() / ".local/share/floati/waiter"),
            command[command.index("--destination") + 1])
        registry = (self.root.path / "registry/entries.jsonl").read_bytes()
        config = self.config.read_bytes()
        code, output, errors = self.cli(*command[1:])
        self.assertEqual(0, code, errors or output)

        prepared = json.loads(output)["evidence"]
        self.assertFalse(prepared["hook_armed"])
        self.assertEqual(config, self.config.read_bytes())
        self.assertEqual(registry, (self.root.path / "registry/entries.jsonl").read_bytes())
        self.assertFalse(self.home.exists())
        self.assertFalse((self.root.path / "codex-wait/workspaces.v0.json").exists())
        self.assertEqual([], list((self.root.path / "receipts/codex-wait-consent").glob("*.jsonl")))
        code, output, errors = self.add()
        self.assertEqual(20, code, errors or output)
        refusal = json.loads(output)["evidence"]
        self.assertEqual("seat_onboard_hook_unavailable", refusal["code"])
        self.assertIn("host hook trust", refusal["detail"])
        self.assertIn("Settings", refusal["remedy"])
        # Fixture testimony models the owner's host action, never live trust.
        self.config.write_text('model = "fixture"\n[hooks.state.' +
            json.dumps(prepared["hook_trust_key"]) + ']\nenabled = true\ntrusted_hash = ' +
            json.dumps(prepared["hook_trust_current_hash"]) + '\n')
        code, output, errors = self.add()
        self.assertEqual(0, code, errors or output)

    def test_foreign_event_without_stop_gets_additive_preparation_remedy(self):
        foreign = b'{ "hooks" : {"SessionStart" : [{"hooks":[{"type":"command","command":"foreign"}]}]} }\n'
        self.hooks.write_bytes(foreign)
        code, output, errors = self.add()
        self.assertEqual(20, code, errors or output)
        refusal = json.loads(output)["evidence"]
        self.assertTrue(refusal["remedy"].startswith("floati hook install"), refusal)
        command = shlex.split(refusal["remedy"])
        code, output, errors = self.cli(*command[1:])
        self.assertEqual(0, code, errors or output)
        self.assertIn(b'"SessionStart" : [{"hooks":[{"type":"command","command":"foreign"}]}]',
            self.hooks.read_bytes())


if __name__ == "__main__":
    unittest.main()

"""H1-F1 migration RED: new install writes floati wait; the old script is a shim."""

from __future__ import annotations

import json
import shlex
import tempfile
import unittest
from pathlib import Path

from floati import fixture_ids as public_ids
from floati.registry import Registry
from floati.root import FloatiRoot
from tests.temp_roots import REAL_TEMP_ROOT
from tests.test_h1_f1 import NEEDLE, REPOSITORY_ROOT


class H1F1MigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.source = REPOSITORY_ROOT
        self.bus_home = self.base / "demo-fleet"
        root = FloatiRoot.open_direct_home(self.bus_home, create=True)
        Registry(root).register(public_ids.builder("floati"), "worker")
        self.workspace = self.base / "workspace"
        self.workspace.mkdir()
        self.hooks_path = self.base / ".codex" / "hooks.json"
        self.hooks_path.parent.mkdir()
        self.hooks_path.write_text(
            json.dumps({"hooks": {"Stop": []}}, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        self.destination = self.base / ".codex" / "floati-wake"

    def installer(self):
        from floati.codex_hook_install import CodexHookInstaller

        return CodexHookInstaller(
            source_root=self.source,
            bus_home=self.bus_home,
            hooks_path=self.hooks_path,
            destination=self.destination,
        )

    def test_new_install_writes_the_floati_wait_launcher(self) -> None:
        self.installer().install(
            self.workspace,
            public_ids.builder("floati"),
            hook_timeout_seconds=10,
            wait_deadline_seconds=2,
        )
        command = json.loads(self.hooks_path.read_text(encoding="utf-8"))[
            "hooks"
        ]["Stop"][0]["hooks"][0]["command"]
        words = shlex.split(command)
        self.assertIn("wait", words)
        self.assertIn("--for", words)
        self.assertIn("fresh-work", words)

    def test_old_script_is_a_shim_that_execs_floati_wait(self) -> None:
        text = (REPOSITORY_ROOT / "scripts" / NEEDLE).read_text(encoding="utf-8")
        self.assertNotIn("from floati.codex_wait import main", text)
        self.assertIn('"wait"', text)
        self.assertIn("fresh-work", text)

    def test_hook_naming_the_old_basename_is_still_observed(self) -> None:
        from floati.codex_hook_trust import observe_codex_waiter_hooks

        document = json.loads(self.hooks_path.read_text(encoding="utf-8"))
        document["hooks"]["Stop"].append(
            {
                "hooks": [
                    {
                        "type": "command",
                        "command": (
                            "/usr/bin/python3 /opt/store/"
                            + "0" * 64
                            + "/scripts/"
                            + NEEDLE
                            + " --root "
                            + str(self.bus_home)
                        ),
                        "timeout": 10,
                        "statusMessage": "Watching Floati bus",
                    }
                ]
            }
        )
        self.hooks_path.write_text(
            json.dumps(document, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        rows = observe_codex_waiter_hooks(self.hooks_path)
        self.assertEqual(1, len(rows))

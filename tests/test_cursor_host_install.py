"""Native installer controls, including runtime-migration boundary REDs."""
import json
import shlex
import unittest

from floati.cursor_wait import (CURSOR_LOOP_LIMIT, DEFAULT_DEADLINE_SECONDS,
    HOOK_TIMEOUT_SECONDS, format_cursor_stop_command, install_cursor_hook)
from floati.errors import ProtocolRefusal
from tests import test_codex_hook_install as fixture


class CursorHostInstallTests(unittest.TestCase):
    def test_native_workspace_install_registers_post_tool_failure_alongside_stop(self):
        f = fixture.CodexHookInstallerTests("runTest")
        f.setUp()
        self.addCleanup(f.doCleanups)
        hooks_dir = f.workspace / ".cursor"
        hooks_dir.mkdir()
        foreign = {"command": "foreign-failure-handler", "timeout": 5}
        (hooks_dir / "hooks.json").write_text(json.dumps({"version": 1,
            "hooks": {"postToolUseFailure": [foreign]}}))
        install_cursor_hook(workspace=f.workspace, root=f.bus_home,
            node=fixture.public_ids.builder("floati"), runtime=f.source)
        hooks = json.loads((hooks_dir / "hooks.json").read_text())["hooks"]
        self.assertEqual(foreign, hooks["postToolUseFailure"][0])
        self.assertEqual(2, len(hooks["postToolUseFailure"]),
            "supported denial callbacks must enter through the governed native installer")
        self.assertIn("floati", hooks["postToolUseFailure"][1]["command"])
        self.assertLessEqual(hooks["postToolUseFailure"][1]["timeout"], 10)
        self.assertEqual(1, len(hooks["stop"]))


class CursorFailureHookMigrationTests(unittest.TestCase):
    def setUp(self):
        self.f = fixture.CodexHookInstallerTests("runTest")
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.node = fixture.public_ids.builder("floati")
        self.old_runtime = self.f.base / "previous runtime"
        self.new_runtime = self.f.base / "current runtime"
        for runtime in (self.old_runtime, self.new_runtime):
            (runtime / "scripts").mkdir(parents=True)
            (runtime / "scripts" / "floati").write_text("#!/bin/sh\n")
        self.old_command = self.command(self.old_runtime)
        self.new_command = self.command(self.new_runtime)
        directory = self.f.workspace / ".cursor"
        directory.mkdir()
        self.hooks_path = directory / "hooks.json"
        self.provenance = self.f.bus_home / "state" / "cursor-wait" / self.node / "installed-hook.json"
        self.foreign = {"command": "/opt/foreign/bin/failure-audit", "timeout": 5}

    def command(self, runtime):
        # This is the actual native command producer, including all runtime,
        # root, node, deadline, timeout and loop-limit coordinates.
        return format_cursor_stop_command(runtime=runtime, root=self.f.bus_home,
            node=self.node, deadline_seconds=DEFAULT_DEADLINE_SECONDS)

    def seed(self, failures):
        self.hooks_path.write_text(json.dumps({"version": 1, "hooks": {
            "stop": [{"command": self.old_command, "timeout": int(HOOK_TIMEOUT_SECONDS),
                "loop_limit": CURSOR_LOOP_LIMIT}],
            "postToolUseFailure": failures}}, indent=3) + "\n")

    def install_current(self):
        return install_cursor_hook(workspace=self.f.workspace, root=self.f.bus_home,
            node=self.node, runtime=self.new_runtime)

    def assert_single_current_callback(self, foreign):
        failures = json.loads(self.hooks_path.read_bytes())["hooks"]["postToolUseFailure"]
        self.assertEqual(len(foreign) + 1, len(failures),
            "a previous runtime must not survive alongside the current owned callback")
        for handler in foreign:
            self.assertEqual(1, failures.count(handler), "genuine foreign handlers must survive")
        owned = [row for row in failures if row not in foreign]
        self.assertEqual([{"command": self.new_command, "timeout": 10}], owned)

    def migrate_or_refuse_without_writes(self):
        before = self.hooks_path.read_bytes()
        try:
            self.install_current()
        except ProtocolRefusal:
            # Missing ownership evidence is allowed to refuse, never to keep
            # the uncertain old launcher and silently add a second callback.
            self.assertEqual(before, self.hooks_path.read_bytes())
            self.assertFalse(self.provenance.exists())
            return False
        self.assert_single_current_callback([self.foreign])
        return True

    def test_previous_runtime_migrates_to_one_current_callback_or_explicitly_refuses(self):
        for launcher_present in (True, False):
            with self.subTest(launcher_present=launcher_present):
                if not launcher_present:
                    (self.old_runtime / "scripts" / "floati").unlink()
                self.seed([self.foreign, {"command": self.old_command, "timeout": 10}])
                self.assertTrue(self.migrate_or_refuse_without_writes())
                before_repeat = self.hooks_path.read_bytes()
                self.install_current()
                self.assert_single_current_callback([self.foreign])
                self.assertEqual(before_repeat, self.hooks_path.read_bytes(),
                    "repeat installation must preserve the accepted state")

    def test_old_and_current_governed_callbacks_are_ambiguous_and_refuse_without_writes(self):
        self.seed([self.foreign, {"command": self.old_command, "timeout": 10},
            {"command": self.new_command, "timeout": 10}])
        before = self.hooks_path.read_bytes()
        with self.assertRaises(ProtocolRefusal):
            self.install_current()
        self.assertEqual(before, self.hooks_path.read_bytes())
        self.assertFalse(self.provenance.exists())

    def test_malformed_governed_callback_refuses_instead_of_becoming_foreign(self):
        old_argv = shlex.split(self.old_command)
        mismatched = old_argv.copy()
        mismatched[mismatched.index("--runtime") + 1] = str(self.new_runtime)
        no_node = old_argv.copy()
        index = no_node.index("--as")
        del no_node[index:index + 2]
        for command in (self.old_command + " '", shlex.join(mismatched),
                shlex.join(no_node), self.new_command + " --root /foreign",
                "'" + old_argv[0] + " " + shlex.join(old_argv[1:])):
            with self.subTest(command=command):
                self.seed([self.foreign, {"command": command, "timeout": 10}])
                before = self.hooks_path.read_bytes()
                with self.assertRaises(ProtocolRefusal):
                    self.install_current()
                self.assertEqual(before, self.hooks_path.read_bytes())
                self.assertFalse(self.provenance.exists())

    def test_foreign_audit_arguments_and_status_markers_remain_foreign(self):
        audit = {"command": shlex.join(["/opt/foreign/bin/audit-hook", "--command", self.old_command]),
            "timeout": 5, "statusMessage": "Audit " + str(self.old_runtime / "scripts" / "floati")}
        filename = {"command": "/opt/foreign/bin/audit-hook --output \x2fprivate/tmp/floati-codex-wait-audit.json",
            "timeout": 5}
        self.seed([audit, filename])
        self.install_current()
        self.assert_single_current_callback([audit, filename])
        before_repeat = self.hooks_path.read_bytes()
        self.install_current()
        self.assert_single_current_callback([audit, filename])
        self.assertEqual(before_repeat, self.hooks_path.read_bytes())


if __name__ == "__main__":
    unittest.main()

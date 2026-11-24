"""Claude Stop installation uses only explicitly named scratch settings."""

import hashlib
import io
import json
import os
import shlex
import tempfile
from types import SimpleNamespace
import unittest
from pathlib import Path
from unittest import mock

from floati import fixture_ids
from floati.mcp import run_cli_artifact
from floati.registry import Registry
from floati.root import FloatiRoot
from tests.temp_roots import REAL_TEMP_ROOT


class ClaudeHookInstallTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        home = mock.patch.dict(os.environ, {"HOME": str(self.base)})
        home.start()
        self.addCleanup(home.stop)
        self.root = FloatiRoot.open_direct_home(self.base / "fleet", create=True)
        self.node = fixture_ids.builder("a")
        Registry(self.root).register(self.node, "claude")
        self.workspace = self.base / "workspace with spaces"
        self.workspace.mkdir()
        self.runtime = self.base / "runtime with spaces"
        (self.runtime / "scripts").mkdir(parents=True)
        (self.runtime / "scripts/floati").write_text("#!/bin/sh\n")
        self.settings = self.base / "settings.json"

    def argv(self, verb="install"):
        return ["hook", verb, "--harness", "claude", "--root", str(self.root.path),
                "--as", self.node, "--workspace", str(self.workspace),
                "--runtime", str(self.runtime), "--hooks", str(self.settings)]

    def install(self):
        code, artifact = run_cli_artifact(self.argv())
        self.assertEqual(0, code, artifact)
        return artifact["evidence"]

    def test_settings_spelling_installs_identity_free_commands_for_a_fresh_session(self):
        argv = self.argv()
        argv[argv.index('--hooks')] = '--settings'
        code, artifact = run_cli_artifact(argv)
        self.assertEqual(0, code, artifact)
        commands = self.workspace / '.claude/commands'
        for verb in ('boot', 'teardown'):
            content = (commands / (verb + '-seat.md')).read_text()
            self.assertIn('floati node ' + verb, content)
            self.assertIn('$ARGUMENTS', content)
            if verb == 'boot':
                self.assertIn('--settings', content)
            self.assertNotIn(self.node, content)
            self.assertNotIn(str(self.root.path), content)

    def test_replay_restores_missing_commands_without_rewriting_settings(self):
        first = self.install()
        settings = self.settings.read_bytes()
        command = self.workspace / '.claude/commands/boot-seat.md'
        original = command.read_bytes()
        command.unlink()
        self.assertEqual(first, self.install())
        self.assertEqual(original, command.read_bytes())
        self.assertEqual(settings, self.settings.read_bytes())

    def test_foreign_command_preserves_settings_and_activation_before_install(self):
        commands = self.workspace / '.claude/commands'
        commands.mkdir(parents=True)
        foreign = commands / 'teardown-seat.md'
        foreign.write_bytes(b'owner command\n')
        self.settings.write_bytes(b'{"foreign":true}\n')
        code, artifact = run_cli_artifact(self.argv())
        self.assertEqual(20, code, artifact)
        self.assertEqual('lifecycle_prompts_target_foreign', artifact['evidence']['code'])
        self.assertEqual(b'{"foreign":true}\n', self.settings.read_bytes())
        self.assertEqual(b'owner command\n', foreign.read_bytes())
        self.assertFalse((commands / 'boot-seat.md').exists())
        self.assertFalse((self.root.path / 'state/claude-wait').exists())

    def test_foreign_bytes_survive_install_reinstall_and_exact_restore(self):
        foreign = b'{ "matcher" : "*", "hooks" : [{"type":"command", "command":"/usr/bin/true", "timeout":3}] }'
        unrelated = b'"preferences" : { "unicode":"\\u00e9", "number":1e2 }'
        prior = (b'{\r\n  ' + unrelated + b', "hooks" : {"Stop" : [ ' + foreign
                 + b' ], "SessionStart":[' + foreign + b'], "PostToolUse":['
                 + foreign + b'], "UserPromptSubmit":[' + foreign + b']}\r\n}\r\n')
        self.settings.write_bytes(prior)
        self.install()
        installed = self.settings.read_bytes()
        self.assertIn(foreign, installed)
        self.assertIn(unrelated, installed)
        hooks = json.loads(installed)["hooks"]
        for event in ("Stop", "SessionStart", "PostToolUse", "UserPromptSubmit"):
            with self.subTest(event=event):
                self.assertEqual(json.loads(foreign), hooks[event][0])
                self.assertEqual(2, len(hooks[event]))
        self.install()
        self.assertEqual(installed, self.settings.read_bytes())
        code, artifact = run_cli_artifact(self.argv("uninstall"))
        self.assertEqual(0, code, artifact)
        self.assertEqual(prior, self.settings.read_bytes())

    def test_relay_hooks_present_at_startup_and_tool_boundaries_with_stop_unchanged(self):
        from floati.claude_hook_install import format_claude_stop_command
        from floati.zcode_hook_install import ZCODE_HOLD_SECONDS

        evidence = self.install()
        installed = self.settings.read_bytes()
        hooks = json.loads(installed)["hooks"]
        timeout, deadline = ZCODE_HOLD_SECONDS
        stop = hooks["Stop"][-1]["hooks"][-1]
        self.assertEqual({"type": "command", "command": format_claude_stop_command(
            runtime=self.runtime, root=self.root.path, node=self.node,
            hook_timeout_seconds=timeout, deadline_seconds=deadline),
            "timeout": timeout}, stop)
        expected = [str(self.runtime / "scripts/floati"), "hook", "relay", "--harness", "claude",
                    "--root", str(self.root.path), "--as", self.node,
                    "--runtime", str(self.runtime)]
        for event in ("SessionStart", "PostToolUse", "UserPromptSubmit"):
            with self.subTest(event=event):
                self.assertEqual(1, len(hooks[event]))
                relay = hooks[event][0]["hooks"]
                self.assertEqual(1, len(relay))
                self.assertEqual("command", relay[0]["type"])
                self.assertEqual(expected, shlex.split(relay[0]["command"]))
                self.assertNotIn("async", relay[0])
                self.assertNotIn("MESSAGING_TOKEN", relay[0]["command"])
        self.assertEqual(evidence, self.install())
        self.assertEqual(installed, self.settings.read_bytes())
        code, artifact = run_cli_artifact(self.argv("uninstall"))
        self.assertEqual(0, code, artifact)
        self.assertFalse(self.settings.exists())

    def test_recorded_stop_only_install_gains_relays_and_restores_its_original_settings(self):
        from floati import claude_hook_install as installer
        from floati.claude_hook_install import format_claude_stop_command
        from floati.waiter_consent import arm_waiter_consent
        from floati.zcode_hook_install import ZCODE_HOLD_SECONDS

        prior = b'{ "foreign" : true }\n'
        timeout, deadline = ZCODE_HOLD_SECONDS
        command = format_claude_stop_command(runtime=self.runtime, root=self.root.path,
            node=self.node, hook_timeout_seconds=timeout, deadline_seconds=deadline)
        legacy_settings = json.dumps({"foreign": True, "hooks": {"Stop": [{"hooks": [
            {"type": "command", "command": command, "timeout": timeout}]}]}}).encode()
        consent = arm_waiter_consent(self.root.path, self.workspace, self.node,
            "claude", timeout, deadline)
        directory = self.root.path / "state/claude-wait" / self.node
        directory.mkdir(parents=True, exist_ok=True)
        record = {"schema_version": 0, "harness": "claude", "hooks_path": str(self.settings),
                  "workspace": str(self.workspace), "runtime": str(self.runtime),
                  "root": str(self.root.path), "node": self.node, "command": command,
                  "existed": True, "preinstall_sha256": hashlib.sha256(prior).hexdigest(),
                  "installed_sha256": hashlib.sha256(legacy_settings).hexdigest(),
                  "consent": consent}
        (directory / "pre-install-settings.bin").write_bytes(prior)
        (directory / "installed-hook.json").write_text(json.dumps(record))
        self.settings.write_bytes(legacy_settings)
        write = installer._write_atomic

        def fail_settings(path, data):
            if path == self.settings:
                raise OSError("constructed upgrade publication failure")
            return write(path, data)

        with mock.patch.object(installer, "_write_atomic", side_effect=fail_settings):
            code, artifact = run_cli_artifact(self.argv())
        self.assertNotEqual(0, code, artifact)
        self.assertEqual(legacy_settings, self.settings.read_bytes())
        upgraded = self.install()
        installed = self.settings.read_bytes()
        hooks = json.loads(installed)["hooks"]
        self.assertEqual(json.loads(legacy_settings)["hooks"]["Stop"], hooks["Stop"])
        self.assertEqual(1, len(hooks["SessionStart"]))
        self.assertEqual(1, len(hooks["PostToolUse"]))
        self.assertEqual(1, len(hooks["UserPromptSubmit"]))
        self.assertEqual(record["consent"], upgraded["consent"])
        self.assertEqual(upgraded, self.install())
        self.assertEqual(installed, self.settings.read_bytes())
        code, artifact = run_cli_artifact(self.argv("uninstall"))
        self.assertEqual(0, code, artifact)
        self.assertEqual(prior, self.settings.read_bytes())

    def test_recorded_relay_install_gains_prompt_hook_without_duplicating_other_handlers(self):
        from floati import claude_hook_install as installer

        prior = b'{ "foreign" : true }\n'
        self.settings.write_bytes(prior)
        self.install()
        directory = self.root.path / "state/claude-wait" / self.node
        receipt_path = directory / "installed-hook.json"
        record = json.loads(receipt_path.read_bytes())
        legacy = json.loads(self.settings.read_bytes())
        legacy["hooks"].pop("UserPromptSubmit", None)
        legacy_bytes = json.dumps(legacy).encode()
        record.pop("relay_events", None)
        record["installed_sha256"] = hashlib.sha256(legacy_bytes).hexdigest()
        receipt_path.write_text(json.dumps(record))
        self.settings.write_bytes(legacy_bytes)
        write = installer._write_atomic

        def fail_settings(path, data):
            if path == self.settings:
                raise OSError("constructed prompt-hook upgrade failure")
            return write(path, data)

        with mock.patch.object(installer, "_write_atomic", side_effect=fail_settings):
            code, _ = run_cli_artifact(self.argv())
        self.assertNotEqual(0, code)
        self.assertEqual(legacy_bytes, self.settings.read_bytes())
        upgraded = self.install()
        installed = self.settings.read_bytes()
        hooks = json.loads(installed)["hooks"]
        for event in ("Stop", "SessionStart", "PostToolUse"):
            self.assertEqual(legacy["hooks"][event], hooks[event])
        self.assertEqual(1, len(hooks["UserPromptSubmit"]))
        self.assertEqual(upgraded, self.install())
        self.assertEqual(installed, self.settings.read_bytes())
        code, artifact = run_cli_artifact(self.argv("uninstall"))
        self.assertEqual(0, code, artifact)
        self.assertEqual(prior, self.settings.read_bytes())

    def test_absent_file_is_removed_on_uninstall(self):
        self.install()
        code, artifact = run_cli_artifact(self.argv("uninstall"))
        self.assertEqual(0, code, artifact)
        self.assertFalse(self.settings.exists())

    def test_missing_hooks_and_stop_keep_unrelated_bytes(self):
        for prior in (b'{ "other" : [1, 2] }\n', b'{ "hooks" : { "SessionStart" : [] }, "other":true }'):
            with self.subTest(prior=prior):
                self.settings.write_bytes(prior)
                self.install()
                code, artifact = run_cli_artifact(self.argv("uninstall"))
                self.assertEqual(0, code, artifact)
                self.assertEqual(prior, self.settings.read_bytes())

    def test_malformed_settings_refuse_without_mutation(self):
        for prior in (b'{broken', b'[]', b'{"hooks":[]}', b'{"hooks":{"Stop":{}}}',
                      b'{"hooks":{"SessionStart":{}}}', b'{"hooks":{"PostToolUse":{}}}',
                      b'{"hooks":{"UserPromptSubmit":{}}}',
                      b'{"hooks":{},"hooks":{}}'):
            with self.subTest(prior=prior):
                self.settings.write_bytes(prior)
                code, artifact = run_cli_artifact(self.argv())
                self.assertEqual(20, code, artifact)
                self.assertEqual("claude_hook_settings_invalid", artifact["evidence"]["code"])
                self.assertEqual(prior, self.settings.read_bytes())
                self.assertFalse((self.root.path / "state/claude-wait").exists())

    def test_bare_library_and_cli_calls_refuse_without_home_discovery(self):
        from floati.claude_hook_install import install_claude_hook, uninstall_claude_hook
        from floati.errors import ProtocolRefusal

        with mock.patch.object(Path, "home", side_effect=AssertionError("home discovery")):
            for operation in (install_claude_hook, uninstall_claude_hook):
                with self.assertRaises(ProtocolRefusal) as caught:
                    operation(workspace=self.workspace, root=self.root.path,
                              node=self.node, runtime=self.runtime)
                self.assertEqual("claude_hook_settings_required", caught.exception.code)
            code, artifact = run_cli_artifact(self.argv()[:-2])
        self.assertEqual(20, code, artifact)
        self.assertEqual("claude_hook_settings_required", artifact["evidence"]["code"])

    def test_symlink_settings_refuse_without_touching_target(self):
        target = self.base / "target.json"
        target.write_bytes(b'{}')
        self.settings.symlink_to(target)
        code, artifact = run_cli_artifact(self.argv())
        self.assertEqual(20, code, artifact)
        self.assertEqual(b'{}', target.read_bytes())

    def test_changed_settings_are_preserved_on_reinstall_and_uninstall(self):
        self.install()
        changed = self.settings.read_bytes() + b'\n'
        self.settings.write_bytes(changed)
        for verb in ("install", "uninstall"):
            code, artifact = run_cli_artifact(self.argv(verb))
            self.assertEqual(20, code, artifact)
            self.assertEqual("claude_hook_settings_changed", artifact["evidence"]["code"])
            self.assertEqual(changed, self.settings.read_bytes())

    def test_installed_command_parses_and_reaches_consented_waiter(self):
        from floati.codex_wait_contract import CodexWaitConsentLedger, resolve_participant
        from floati.zcode_hook_install import ZCODE_HOLD_SECONDS

        evidence = self.install()
        hook = json.loads(self.settings.read_bytes())["hooks"]["Stop"][-1]["hooks"][-1]
        argv = shlex.split(hook["command"])
        self.assertEqual(["wake", "wait", "--harness", "claude"], argv[1:5])
        self.assertEqual(str(self.runtime / "scripts/floati"), argv[0])
        participant = resolve_participant(self.root.path, self.workspace)
        self.assertIsNotNone(participant)
        consent = CodexWaitConsentLedger(self.root).require_armed(participant.binding)
        self.assertEqual(ZCODE_HOLD_SECONDS, (hook["timeout"], consent["wait_deadline_seconds"]))
        self.assertEqual(consent["id"], evidence["consent"]["consent_receipt_id"])
        payload = {"cwd": str(self.workspace), "session_id": "claude-scratch-session", "hook_event_name": "Stop"}
        from floati.cli import main
        with mock.patch("sys.stdin", io.StringIO(json.dumps(payload))),\
             mock.patch("floati.codex_wait.run_stop_waiter", return_value=0) as waiter:
            self.assertEqual(0, main(argv[1:]))
        self.assertEqual(self.root.path, waiter.call_args.kwargs["bus_home"])
        self.assertEqual(payload, waiter.call_args.kwargs["hook_payload"])

    def test_configured_doorbell_skips_stop_and_fallback_restores_native_wait(self):
        from floati.cli import main

        evidence = self.install()
        payload = {"cwd": str(self.workspace), "session_id": "claude-scratch-session",
                   "hook_event_name": "Stop"}
        for usable in (True, False):
            with self.subTest(doorbell_usable=usable):
                output = io.StringIO()

                def native_waiter(**kwargs):
                    kwargs["stdout"].write('{"decision":"block","reason":"native fallback"}')
                    return 0

                from floati import claude_inbox
                with mock.patch.object(claude_inbox, "stop_path_required", return_value=not usable),\
                     mock.patch("sys.stdin", io.StringIO(json.dumps(payload))),\
                     mock.patch("sys.stdout", output),\
                     mock.patch("floati.codex_wait.run_stop_waiter", side_effect=native_waiter):
                    self.assertEqual(0, main(shlex.split(evidence["command"])[1:]))
                if usable:
                    self.assertEqual("", output.getvalue())
                else:
                    self.assertEqual("block", json.loads(output.getvalue())["decision"])

    def test_doctor_reports_installed_then_absent(self):
        from floati.doctor import Doctor
        from floati.claude_hook_install import project_claude_installed_hook_findings

        def findings():
            artifact, _ = Doctor(Path(__file__).resolve().parents[1], str(self.root.path),
                                 ref="HEAD", no_sandbox=True).artifact()
            from tests.schema_validation import validate_json_schema
            validate_json_schema(artifact, Path("schemas/v1/doctor-artifact.schema.json"))
            return [row for row in artifact["findings"] if row["code"].startswith("claude_hook_")]

        self.assertEqual(["claude_hook_absent"], [row["code"] for row in findings()])
        self.install()
        rows = project_claude_installed_hook_findings(self.root)
        self.assertEqual(["claude_hook_installed"], [row["code"] for row in rows])
        self.assertIn(str(self.settings), rows[0]["detail"])
        self.settings.unlink()
        self.assertEqual(["claude_hook_absent"],
            [row["code"] for row in project_claude_installed_hook_findings(self.root)])

    def test_installed_stop_delivers_real_scratch_mail(self):
        from floati.cli import main
        from floati.events import EventLog

        evidence = self.install()
        sender = fixture_ids.builder("b")
        Registry(self.root).register(sender, "claude")
        message = EventLog(self.root).send(sender, self.node, "floati", "a" * 40,
            "docs/CONFLUENCE-v0.md", "scratch hook mail", idempotency_key="claude-hook-mail")
        payload = {"cwd": str(self.workspace), "session_id": "claude-scratch-session", "hook_event_name": "Stop"}
        output = io.StringIO()
        with mock.patch("sys.stdin", io.StringIO(json.dumps(payload))),\
             mock.patch("sys.stdout", output):
            self.assertEqual(0, main(shlex.split(evidence["command"])[1:]))
        decision = json.loads(output.getvalue())
        self.assertEqual("block", decision["decision"])
        self.assertIn(message["id"], decision["reason"])

    def test_failed_settings_publication_retries_only_the_exact_preimage(self):
        from floati import claude_hook_install as installer

        self.settings.write_bytes(b'{ "original" : true }\n')
        write = installer._write_atomic

        def fail_settings(path, data):
            if path == self.settings:
                raise OSError("constructed publication failure")
            return write(path, data)

        with mock.patch.object(installer, "_write_atomic", side_effect=fail_settings):
            code, _ = run_cli_artifact(self.argv())
        self.assertNotEqual(0, code)
        self.assertEqual(b'{ "original" : true }\n', self.settings.read_bytes())
        self.install()
        code, artifact = run_cli_artifact(self.argv("uninstall"))
        self.assertEqual(0, code, artifact)
        self.assertEqual(b'{ "original" : true }\n', self.settings.read_bytes())

    def test_doctor_refuses_redirected_state_ancestry_without_reading_receipts(self):
        from floati.claude_hook_install import project_claude_installed_hook_findings

        self.install()
        state = self.root.path / "state"
        redirected = self.base / "redirected-state"
        state.rename(redirected)
        state.symlink_to(redirected, target_is_directory=True)
        with mock.patch("floati.claude_hook_install._record") as reader:
            rows = project_claude_installed_hook_findings(self.root)
        reader.assert_not_called()
        self.assertEqual(["claude_hook_invalid"], [row["code"] for row in rows])

    def test_stop_payload_for_another_node_never_reaches_waiter(self):
        from floati.cli import main

        evidence = self.install()
        argv = shlex.split(evidence["command"])[1:]
        argv[argv.index("--as") + 1] = fixture_ids.builder("b")
        payload = {"cwd": str(self.workspace), "session_id": "claude-scratch-session"}
        output = io.StringIO()
        with mock.patch("sys.stdin", io.StringIO(json.dumps(payload))),\
             mock.patch("sys.stdout", output),\
             mock.patch("floati.codex_wait.run_stop_waiter") as waiter:
            self.assertEqual(20, main(argv))
        waiter.assert_not_called()
        self.assertEqual("claude_hook_workspace_mismatch", json.loads(output.getvalue())["evidence"]["code"])


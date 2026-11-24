"""Governed installation adds observers without deciding permissions."""
import contextlib
import io
import json
import shlex
import unittest

from floati.errors import ProtocolRefusal
from tests import test_codex_hook_install as fixture


EVENTS = ("PermissionRequest", "PreToolUse", "PostToolUse", "UserPromptSubmit",
          "SessionStart", "SessionEnd", "SubagentStart", "SubagentStop",
          "PreCompact", "PostCompact", "Interrupt")


class HostObserverInstallTests(unittest.TestCase):
    def setUp(self):
        self.f = fixture.CodexHookInstallerTests("runTest")
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)

    def install(self):
        return self.f.installer().install(self.f.workspace,
            fixture.public_ids.builder("floati"), hook_timeout_seconds=10,
            wait_deadline_seconds=2, acknowledge_stop_hook_change=self.f.fresh_pair,
            include_host_observers=True)

    def test_native_install_registers_fast_observers_and_preserves_foreign_handlers(self):
        before = json.loads(self.f.hooks_path.read_text())["hooks"]
        self.install()
        hooks = json.loads(self.f.hooks_path.read_text())["hooks"]
        self.assertEqual(self.f.original_stop, hooks["Stop"][:2])
        self.assertEqual(3, len(hooks["Stop"]), "Stop must remain one owned waiter")
        for event in EVENTS:
            self.assertEqual(before.get(event, []), hooks[event][:-1], event)
            handler = hooks[event][-1]["hooks"][0]
            self.assertEqual(hooks["Stop"][-1]["hooks"][0]["command"], handler["command"])
            self.assertIn("floati-codex-wait", handler["command"])
            self.assertNotIn("--workspace", handler["command"])
            self.assertNotIn("--session", handler["command"])
            self.assertFalse(handler.get("async", False),
                "local observation must commit before its deadline process starts")
            self.assertLessEqual(handler["timeout"], 10)
            if event == "Interrupt":
                self.assertLessEqual(handler["timeout"], 3)

    def test_foreign_handler_bytes_survive_stop_and_observer_insertion(self):
        foreign = '{ "hooks" : [ { "command":"foreign\\u0020handler", "type" : "command" } ] }'
        self.f.hooks_path.write_text('{"hooks":{"Stop":[' + foreign
            + '],"PermissionRequest":[' + foreign + ']},"foreign": { "keep" : true }}\n')
        receipt = self.install()
        after = self.f.hooks_path.read_text()
        self.assertEqual(2, after.count(foreign))
        self.assertIn('"foreign": { "keep" : true }', after)
        self.assertEqual(set(EVENTS), set(receipt["host_observer_definitions"]))
        for definition in receipt["host_observer_definitions"].values():
            self.assertEqual(64, len(definition["definition_hash"]))
            self.assertEqual("unmeasured", definition["trust"])
            self.assertNotIn("hook_trust_key", definition)
            self.assertIn("not a native trust hash", definition["hash_provenance"])
        self.assertEqual(len(EVENTS), len({row["definition_hash"]
            for row in receipt["host_observer_definitions"].values()}))

    def assert_foreign_marker_groups_coexist(self, foreign):
        # Exercise Stop as well as every observer: a marker in metadata or an
        # argument filename does not make the foreign executable Floati-owned.
        events = ("Stop", *EVENTS)
        raw = json.dumps(foreign, ensure_ascii=True, separators=(", ", " : "))
        document = '{"hooks":{' + ",".join(json.dumps(event) + ":[" + raw + "]"
            for event in events) + '}}\n'
        self.f.hooks_path.write_text(document)
        self.install()
        after = self.f.hooks_path.read_bytes()
        self.assertEqual(len(events), after.decode().count(raw),
            "foreign group bytes must survive the additive install")
        hooks = json.loads(after)["hooks"]
        for event in events:
            with self.subTest(event=event):
                self.assertEqual(foreign, hooks[event][0])
                self.assertEqual(2, len(hooks[event]),
                    "foreign marker text must coexist with exactly one owned group")
        self.install()
        self.assertEqual(after, self.f.hooks_path.read_bytes())

    def test_foreign_status_message_marker_is_not_owned_launcher_authority(self):
        for handler in ({"type": "command",
                "command": "/opt/foreign/bin/audit-hook --source codex", "timeout": 5,
                "statusMessage": "Auditing floati-codex-wait adoption"},
                {"type": "command",
                "command": "/opt/foreign/bin/audit-hook --output \x2fprivate/tmp/floati-codex-wait-audit.json",
                "timeout": 5}):
            with self.subTest(handler=handler):
                self.assert_foreign_marker_groups_coexist({"hooks": [handler]})

    def assert_malformed_launcher_positions_refuse_without_writes(self, command):
        from floati.codex_hook_trust import board_guard_observation

        original = json.loads(self.f.hooks_path.read_bytes())
        trust_path = self.f.hooks_path.with_name("config.toml")
        trust_before = trust_path.read_bytes() if trust_path.exists() else None
        for event in ("Stop", "PermissionRequest"):
            document = json.loads(json.dumps(original))
            document["hooks"][event][-1]["hooks"][0]["command"] = command
            before = json.dumps(document, indent=3).encode() + b"\n"
            self.f.hooks_path.write_bytes(before)
            if event == "Stop":
                with self.subTest(event=event, stage="trust must not skip candidate"):
                    self.assertTrue(board_guard_observation(self.f.hooks_path)["refused"],
                        "an unsupported actual launcher must not disappear from the trust gate")
                    self.assertEqual(before, self.f.hooks_path.read_bytes())
            with self.subTest(event=event, stage="explicit refusal"):
                with self.assertRaises(ProtocolRefusal):
                    self.install()
            with self.subTest(event=event, stage="unchanged bytes"):
                self.assertEqual(before, self.f.hooks_path.read_bytes(),
                    "malformed launcher ownership must refuse before rewriting hooks")
                self.assertEqual(trust_before,
                    trust_path.read_bytes() if trust_path.exists() else None)

    def native_stop_argv(self):
        block = fixture.expected_stop_block(self.f.destination, self.f.bus_home, 10)
        return shlex.split(block["hooks"][0]["command"])

    def test_opening_unclosed_quote_in_actual_launcher_token_refuses_without_writes(self):
        self.install()
        argv = self.native_stop_argv()
        command = shlex.quote(argv[0]) + " '" + argv[1] + " " + shlex.join(argv[2:])
        with self.assertRaises(ValueError):
            shlex.split(command)
        original = self.f.hooks_path.read_bytes()
        commands = (command, shlex.join([argv[0], "-B", *argv[1:]]),
            *(shlex.join([interpreter, *argv[1:]]) for interpreter in
                ("/opt/floati-test-python/bin/python3", "/venv/bin/python")))
        for command in commands:
            with self.subTest(command=command):
                self.f.hooks_path.write_bytes(original)
                self.assert_malformed_launcher_positions_refuse_without_writes(command)

    def test_duplicate_or_malformed_owned_observers_refuse_without_writes(self):
        self.install()
        original = json.loads(self.f.hooks_path.read_text())
        for damage in ("duplicate", "async", "mixed", "wrong-root", "float-timeout"):
            with self.subTest(damage=damage):
                document = json.loads(json.dumps(original))
                groups = document["hooks"]["PermissionRequest"]
                handler = groups[-1]["hooks"][0]
                if damage == "duplicate":
                    groups.append(groups[-1])
                elif damage == "async":
                    handler["async"] = True
                elif damage == "mixed":
                    groups[-1]["hooks"].append({"type": "command", "command": "foreign"})
                elif damage == "wrong-root":
                    handler["command"] += " --root /foreign"
                else:
                    handler["timeout"] = 10.0
                before = json.dumps(document).encode()
                self.f.hooks_path.write_bytes(before)
                with self.assertRaises(ProtocolRefusal):
                    self.install()
                self.assertEqual(before, self.f.hooks_path.read_bytes())

    def test_duplicate_json_keys_refuse_without_writes(self):
        before = b'{"hooks":{"Stop":[],"PermissionRequest":[],"PermissionRequest":[]}}'
        self.f.hooks_path.write_bytes(before)
        with self.assertRaises(ProtocolRefusal):
            self.install()
        self.assertEqual(before, self.f.hooks_path.read_bytes())

    def test_default_installer_keeps_observer_installation_opt_in(self):
        self.f.installer().install(self.f.workspace,
            fixture.public_ids.builder("floati"), hook_timeout_seconds=10,
            wait_deadline_seconds=2, acknowledge_stop_hook_change=self.f.fresh_pair)
        hooks = json.loads(self.f.hooks_path.read_text())["hooks"]
        self.assertNotIn("PermissionRequest", hooks)

    def test_codex_cli_requires_every_codex_coordinate_before_any_write(self):
        from floati.cli import main
        coordinates = {"--hooks": str(self.f.hooks_path),
            "--destination": str(self.f.destination), "--hook-timeout-seconds": "10",
            "--wait-deadline-seconds": "2", "--acknowledge-stop-hook-change": self.f.fresh_pair}
        before = self.f.hooks_path.read_bytes()
        for absent in coordinates:
            with self.subTest(absent=absent):
                arguments = ["hook", "install", "--harness", "codex", "--root", str(self.f.bus_home),
                    "--as", fixture.public_ids.builder("floati"), "--workspace", str(self.f.workspace),
                    "--runtime", str(self.f.source)]
                for flag, value in coordinates.items():
                    if flag != absent:
                        arguments.extend([flag, value])
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(20, main(arguments))
                self.assertEqual(before, self.f.hooks_path.read_bytes())
                self.assertFalse(self.f.destination.exists())

    def test_cursor_cli_refuses_codex_only_flags_without_installation(self):
        from floati.cli import main
        for flag, value in (("--hooks", str(self.f.hooks_path)),
                ("--destination", str(self.f.destination)), ("--hook-timeout-seconds", "10"),
                ("--wait-deadline-seconds", "2"), ("--acknowledge-stop-hook-change", self.f.fresh_pair)):
            with self.subTest(flag=flag):
                arguments = ["hook", "install", "--harness", "cursor", "--root", str(self.f.bus_home),
                    "--as", fixture.public_ids.builder("floati"), "--workspace", str(self.f.workspace),
                    "--runtime", str(self.f.source), flag, value]
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(20, main(arguments))
                self.assertFalse((self.f.workspace / ".cursor").exists())

    def test_native_cli_exposes_codex_observers_only_with_explicit_install_coordinates(self):
        from floati.cli import main
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            try:
                status = main(["hook", "install", "--harness", "codex",
                    "--root", str(self.f.bus_home), "--as", fixture.public_ids.builder("floati"),
                    "--workspace", str(self.f.workspace), "--runtime", str(self.f.source),
                    "--hooks", str(self.f.hooks_path), "--destination", str(self.f.destination),
                    "--hook-timeout-seconds", "10", "--wait-deadline-seconds", "2",
                    "--acknowledge-stop-hook-change", self.f.fresh_pair])
            except SystemExit as exc:
                status = exc.code
        self.assertEqual(0, status, errors.getvalue() + output.getvalue())
        hooks = json.loads(self.f.hooks_path.read_text())["hooks"]
        self.assertIn("PermissionRequest", hooks)
        self.assertIn("Interrupt", hooks)


class HostObserverInstallHelpTests(unittest.TestCase):
    codex_options = ("--hooks", "--destination", "--hook-timeout-seconds",
        "--wait-deadline-seconds", "--acknowledge-stop-hook-change")

    def test_authored_help_documents_codex_coordinates_and_adoption_boundary(self):
        from floati.help_copy import HELP_COPY

        copy = HELP_COPY["hook install"]
        harness_doc = copy["option_docs"]["--harness"].casefold()
        self.assertIn("codex", harness_doc)
        self.assertIn("cursor", harness_doc)
        for option in self.codex_options:
            with self.subTest(option=option):
                explanation = copy["option_docs"].get(option, "").casefold()
                self.assertIn("codex", explanation)
                self.assertIn("required", explanation,
                    "the conditional Codex requirement must be explicit")
        prose = " ".join((copy["description"], copy["body"], *copy["notes"])).casefold()
        self.assertNotIn("does not edit a live codex hooks.json", prose)
        self.assertIn("trust", prose)
        self.assertIn("relaunch", prose)
        self.assertIn("--harness codex", copy["examples"])
        self.assertIn("--harness cursor", copy["examples"])

if __name__ == "__main__":
    unittest.main()

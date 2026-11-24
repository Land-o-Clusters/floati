"""Contracts for the additive, digest-receipted Codex waiter installer."""

from __future__ import annotations

from floati import fixture_ids as public_ids

import hashlib
import json
import shlex
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from floati.registry import Registry
from floati.root import FloatiRoot
from tests.temp_roots import REAL_TEMP_ROOT


def expected_stop_block(destination: Path, bus_home: Path, timeout: int) -> dict:
    """The Stop group this fixture expects install to write.

    Built from the fixture's own coordinates, never read back from the
    planner: if the product's planned definition drifts, the operator pair
    a test derives from this stops matching and the install refuses.
    """

    command = " ".join(
        shlex.quote(part)
        for part in (
            "/usr/bin/python3",
            str(destination / "current" / "scripts" / "floati-codex-wait"),
            "wait",
            "--for",
            "fresh-work",
            "--root",
            str(bus_home),
        )
    )
    return {
        "hooks": [
            {
                "type": "command",
                "command": command,
                "timeout": timeout,
                "statusMessage": "Watching Floati bus",
            }
        ]
    }


def operator_pair(preimage_block, target_block) -> str:
    """What an operator types: `<installed>:<planned>` Stop-group hashes.

    `preimage_block is None` states that no Floati Stop group is installed,
    whose preimage is sha256(b"").
    """

    from floati.codex_hook_trust import codex_hook_current_hash

    preimage = (
        hashlib.sha256(b"").hexdigest()
        if preimage_block is None
        else codex_hook_current_hash(preimage_block)
    )
    return f"{preimage}:{codex_hook_current_hash(target_block)}"


class CodexHookInstallerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.source = Path(__file__).resolve().parents[1]
        self.bus_home = self.base / "demo-fleet"
        root = FloatiRoot.open_direct_home(self.bus_home, create=True)
        Registry(root).register(public_ids.builder('floati'), "worker")
        self.workspace = self.base / "workspace"
        self.workspace.mkdir()
        self.hooks_path = self.base / ".codex" / "hooks.json"
        self.hooks_path.parent.mkdir()
        self.original_stop = [
            {
                "hooks": [
                    {
                        "type": "command",
                        "command": "'/opt/example/bin/bridge' --source codex",
                        "timeout": 5,
                    }
                ]
            },
            {
                "matcher": "*",
                "hooks": [
                    {
                        "type": "command",
                        "command": "/usr/bin/python3 /opt/example/bin/existing_wait.py",
                        "timeout": 1800,
                    }
                ],
            },
        ]
        self.hooks_path.write_text(
            json.dumps(
                {
                    "hooks": {
                        "Stop": self.original_stop,
                        "SubagentStop": [
                            {"hooks": [{"type": "command", "command": "subagent-only"}]}
                        ],
                        "SessionStart": [{"hooks": [{"type": "command", "command": "true"}]}],
                    }
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        self.destination = self.base / ".codex" / "floati-wake"

        # This fixture installs into a Stop list with no Floati group, so its
        # first install CHANGES the definition and names this operator pair.
        self.fresh_pair = operator_pair(
            None, expected_stop_block(self.destination, self.bus_home, 10)
        )

    def installer(self):
        from floati.codex_hook_install import CodexHookInstaller

        return CodexHookInstaller(
            source_root=self.source,
            bus_home=self.bus_home,
            hooks_path=self.hooks_path,
            destination=self.destination,
        )

    def test_global_preparation_refuses_invalid_trust_before_any_write(self):
        from floati.errors import ProtocolRefusal
        from floati.codex_hook_trust import codex_hook_current_hash
        before = self.hooks_path.read_bytes()
        config = self.hooks_path.with_name("config.toml")
        foreign = self.base / "foreign.toml"
        foreign.write_text('model = "fixture"\n')
        for case in ("symlink", "malformed", "stale-ack"):
            with self.subTest(case=case):
                self.hooks_path.write_bytes(before)
                self.destination = self.base / ("prepare-" + case)
                pair = operator_pair(None, expected_stop_block(self.destination, self.bus_home, 10))
                config.unlink(missing_ok=True)
                if case == "symlink":
                    config.symlink_to(foreign)
                elif case == "malformed":
                    config.write_text('[hooks.state.' + json.dumps(f"{self.hooks_path}:stop:2:0") +
                        ']\ntrusted_hash = "invalid"\n')
                else:
                    config.write_text('model = "fixture"\n')
                with self.assertRaises(ProtocolRefusal):
                    self.installer().prepare(hook_timeout_seconds=10,
                        acknowledge_stop_hook_change=("0" * 64 + ":" +
                            codex_hook_current_hash(expected_stop_block(self.destination, self.bus_home, 10))
                            if case == "stale-ack" else pair))
                self.assertEqual(before, self.hooks_path.read_bytes())
                self.assertFalse(self.destination.exists(), case)

    def test_install_adds_one_block_and_preserves_every_existing_block(self) -> None:
        receipt = self.installer().install(
            self.workspace,
            public_ids.builder('floati'),
            hook_timeout_seconds=10,
            wait_deadline_seconds=2,
            acknowledge_stop_hook_change=self.fresh_pair,
        )

        installed = json.loads(self.hooks_path.read_text(encoding="utf-8"))
        self.assertEqual(self.original_stop, installed["hooks"]["Stop"][:2])
        self.assertEqual(3, len(installed["hooks"]["Stop"]))
        block = installed["hooks"]["Stop"][2]
        self.assertEqual("Watching Floati bus", block["hooks"][0]["statusMessage"])
        self.assertEqual(10, block["hooks"][0]["timeout"])
        self.assertIn(str(self.bus_home), block["hooks"][0]["command"])
        # BL-11: the registered command names the stable current launcher and
        # never the bundle digest, so updates cannot consume Codex trust.
        self.assertIn(
            str(self.destination / "current" / "scripts" / "floati-codex-wait"),
            block["hooks"][0]["command"])
        self.assertNotIn(receipt["bundle_digest"], block["hooks"][0]["command"])
        for field in (
            "bundle_digest",
            "installed_bundle_digest",
            "hooks_before_digest",
            "hooks_after_digest",
            "command_digest",
            "workspace_map_digest",
        ):
            self.assertRegex(receipt[field], r"^[0-9a-f]{64}$")
        self.assertEqual("installed", receipt["state"])

    def test_shared_commands_are_installed_in_the_native_codex_home(self) -> None:
        receipt = self.installer().install(self.workspace, public_ids.builder('floati'),
            hook_timeout_seconds=10, wait_deadline_seconds=2,
            acknowledge_stop_hook_change=self.fresh_pair)
        commands = self.hooks_path.parent / 'skills'
        self.assertEqual(str(commands), receipt['seat_commands']['out'])
        for verb in ('boot', 'teardown'):
            content = (commands / f'{verb}-seat/SKILL.md').read_text()
            self.assertTrue(content.startswith(f'---\nname: {verb}-seat\n'))
            self.assertIn('\ndescription: ', content)
            self.assertNotIn('$ARGUMENTS', content)
            self.assertNotIn(str(self.bus_home), content)
            self.assertNotIn(public_ids.builder('floati'), content)
        self.assertFalse((self.workspace / '.codex/prompts').exists())

    def test_installed_commands_use_the_recorded_runtime_without_path_lookup(self) -> None:
        """A fresh shell must reach this install even with no Floati on PATH."""
        import re
        import shlex

        receipt = self.installer().install(self.workspace, public_ids.builder('floati'),
            hook_timeout_seconds=10, wait_deadline_seconds=2,
            acknowledge_stop_hook_change=self.fresh_pair)
        commands = Path(receipt['seat_commands']['out'])
        for verb in ('boot', 'teardown'):
            with self.subTest(verb=verb):
                path = commands / f'{verb}-seat/SKILL.md'
                # Also read the old output so the RED names the runtime defect
                # independently of the command-surface defect.
                if not path.exists():
                    path = commands / f'{verb}-seat.md'
                content = path.read_text()
                invocation = re.search(r'Run `([^`]+)` with', content).group(1)
                argv = shlex.split(invocation)
                self.assertEqual(str(self.source / 'scripts/floati'), argv[0])
                self.assertEqual(['node', verb], argv[1:3])

    def test_installed_waiter_launcher_declares_the_fixed_stop_hook_interpreter(self) -> None:
        receipt = self.installer().install(
            self.workspace,
            public_ids.builder('floati'),
            hook_timeout_seconds=10,
            wait_deadline_seconds=2,
            acknowledge_stop_hook_change=self.fresh_pair,
        )

        installed = json.loads(self.hooks_path.read_text(encoding="utf-8"))
        command = installed["hooks"]["Stop"][2]["hooks"][0]["command"]
        launcher = self.destination / receipt["bundle_digest"] / "scripts" / "floati-codex-wait"
        self.assertEqual("/usr/bin/python3", shlex.split(command)[0])
        with launcher.open("rb") as stream:
            self.assertEqual(b"#!/usr/bin/python3\n", stream.readline())

    def test_install_names_the_absent_fixed_host_interpreter_before_writing(self) -> None:
        from floati.errors import ProtocolRefusal

        missing = self.base / "missing-usr-bin-python3"
        hooks_before = self.hooks_path.read_bytes()
        with mock.patch("floati.codex_hook_install._CODEX_WAIT_INTERPRETER", missing):
            with self.assertRaises(ProtocolRefusal) as caught:
                self.installer().install(
                    self.workspace,
                    public_ids.builder('floati'),
                    hook_timeout_seconds=10,
                    wait_deadline_seconds=2,
                    acknowledge_stop_hook_change=self.fresh_pair,
                )

        self.assertEqual("codex_wait_host_interpreter_absent", caught.exception.code)
        self.assertIn(str(missing), caught.exception.detail)
        self.assertEqual(hooks_before, self.hooks_path.read_bytes())
        self.assertFalse(self.destination.exists())

    def test_rebind_names_the_absent_fixed_host_interpreter_before_shape_validation(self) -> None:
        from floati.codex_hook_install import plan_waiter_rebind
        from floati.errors import ProtocolRefusal

        receipt = self.installer().install(
            self.workspace,
            public_ids.builder('floati'),
            hook_timeout_seconds=10,
            wait_deadline_seconds=2,
            acknowledge_stop_hook_change=self.fresh_pair,
        )
        self.assertRegex(receipt["bundle_digest"], r"^[0-9a-f]{64}$")
        malformed = json.loads(self.hooks_path.read_text(encoding="utf-8"))
        malformed["hooks"]["Stop"][2]["hooks"][0]["command"] = "echo floati-codex-wait"
        self.hooks_path.write_text(json.dumps(malformed), encoding="utf-8")
        missing = self.base / "missing-usr-bin-python3"

        with mock.patch("floati.codex_hook_install._CODEX_WAIT_INTERPRETER", missing):
            with self.assertRaises(ProtocolRefusal) as caught:
                plan_waiter_rebind(self.hooks_path, self.destination, "1" * 64)

        self.assertEqual("codex_wait_host_interpreter_absent", caught.exception.code)
        self.assertIn(str(missing), caught.exception.detail)

    def test_exact_retry_never_duplicates_the_floati_block(self) -> None:
        first = self.installer().install(
            self.workspace,
            public_ids.builder('floati'),
            hook_timeout_seconds=10,
            wait_deadline_seconds=2,
            acknowledge_stop_hook_change=self.fresh_pair,
        )
        second = self.installer().install(
            self.workspace,
            public_ids.builder('floati'),
            hook_timeout_seconds=10,
            wait_deadline_seconds=2,
        )

        installed = json.loads(self.hooks_path.read_text(encoding="utf-8"))
        matching = [
            block
            for block in installed["hooks"]["Stop"]
            if "floati-codex-wait" in block.get("hooks", [{}])[0].get("command", "")
        ]
        self.assertEqual(1, len(matching))
        self.assertEqual(first["bundle_digest"], second["bundle_digest"])
        self.assertEqual(first["command_digest"], second["command_digest"])
        self.assertEqual("already_installed", second["state"])

    def test_map_growth_keeps_prior_consent_and_uses_coordinate_bound_keys(self) -> None:
        from floati.codex_wait_contract import CodexWaitConsentLedger, resolve_participant

        second_workspace = self.base / "second-workspace"
        second_workspace.mkdir()
        first = self.installer().install(
            self.workspace,
            public_ids.builder('floati'),
            hook_timeout_seconds=10,
            wait_deadline_seconds=2,
            acknowledge_stop_hook_change=self.fresh_pair,
        )
        second = self.installer().install(
            second_workspace,
            public_ids.builder('floati'),
            hook_timeout_seconds=10,
            wait_deadline_seconds=2,
        )
        retried = self.installer().install(
            self.workspace,
            public_ids.builder('floati'),
            hook_timeout_seconds=10,
            wait_deadline_seconds=2,
        )

        participant = resolve_participant(self.bus_home, self.workspace)
        assert participant is not None
        consent = CodexWaitConsentLedger(participant.root).require_armed(
            participant.binding
        )
        self.assertEqual(first["consent_receipt_id"], consent["id"])
        self.assertEqual(first["consent_receipt_id"], retried["consent_receipt_id"])
        self.assertNotEqual(first["consent_receipt_id"], second["consent_receipt_id"])

    def test_install_reports_trust_and_never_writes_codex_trust_state(self) -> None:
        config_path = self.hooks_path.with_name("config.toml")
        first = self.installer().install(
            self.workspace,
            public_ids.builder('floati'),
            hook_timeout_seconds=10,
            wait_deadline_seconds=2,
            acknowledge_stop_hook_change=self.fresh_pair,
        )

        self.assertEqual("untrusted_pending_user", first["hook_trust"])
        self.assertFalse(first["hook_armed"])
        self.assertTrue(first["hook_trust_remediation"])
        self.assertNotIn("DRAFT - ", first["hook_trust_remediation"])
        self.assertFalse(config_path.exists())

        config_path.write_text(
            "[hooks.state." + json.dumps(first["hook_trust_key"]) + "]\n"
            "trusted_hash = " + json.dumps(first["hook_trust_current_hash"]) + "\n",
            encoding="utf-8",
        )
        before = config_path.read_bytes()
        second = self.installer().install(
            self.workspace,
            public_ids.builder('floati'),
            hook_timeout_seconds=10,
            wait_deadline_seconds=2,
        )

        self.assertEqual("trusted", second["hook_trust"])
        self.assertTrue(second["hook_armed"])
        self.assertEqual(before, config_path.read_bytes())

    def test_install_replaces_one_prior_floati_stop_block_and_never_touches_subagent_stop(self) -> None:
        from floati.codex_hook_trust import codex_hook_current_hash

        document = json.loads(self.hooks_path.read_text(encoding="utf-8"))
        old_block = {
            "hooks": [{
                "type": "command",
                "command": (
                    f"/usr/bin/python3 {self.destination / ('0' * 64) / 'scripts/floati-codex-wait'} "
                    f"--root {self.bus_home}"
                ),
                "timeout": 9,
                "statusMessage": "Watching Floati bus",
            }]
        }
        document["hooks"]["Stop"].insert(1, old_block)
        subagent_before = document["hooks"]["SubagentStop"]
        self.hooks_path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        trust_key = f"{self.hooks_path}:stop:1:0"
        self.hooks_path.with_name("config.toml").write_text(
            "[hooks.state." + json.dumps(trust_key) + "]\n"
            "trusted_hash = " + json.dumps(codex_hook_current_hash(old_block)) + "\n",
            encoding="utf-8",
        )

        receipt = self.installer().install(
            self.workspace,
            public_ids.builder('floati'),
            hook_timeout_seconds=10,
            wait_deadline_seconds=2,
            acknowledge_stop_hook_change=operator_pair(old_block, expected_stop_block(self.destination, self.bus_home, 10)),
            session_id="installer-session",
        )

        installed = json.loads(self.hooks_path.read_text(encoding="utf-8"))
        self.assertEqual(self.original_stop[0], installed["hooks"]["Stop"][0])
        self.assertEqual(self.original_stop[1], installed["hooks"]["Stop"][2])
        self.assertEqual(subagent_before, installed["hooks"]["SubagentStop"])
        replacement = installed["hooks"]["Stop"][1]
        # BL-11: the replacement migrates the legacy digest-named command to
        # the stable current launcher.
        self.assertIn(
            str(self.destination / "current" / "scripts" / "floati-codex-wait"),
            replacement["hooks"][0]["command"])
        self.assertNotIn(receipt["bundle_digest"], replacement["hooks"][0]["command"])
        self.assertEqual("replaced", receipt["state"])
        self.assertEqual("modified", receipt["hook_trust_status"])
        self.assertIs(True, receipt.get("hook_trust_invalidated_by_install"))
        self.assertTrue(receipt["hook_trust_remediation"])
        self.assertEqual("installer-session", receipt["acting_session_id"])
        self.assertRegex(receipt["session_receipt_id"], r"^codex-wait-session-")

    def test_install_refuses_multiple_prior_floati_stop_blocks_without_writing_hooks(self) -> None:
        from floati.errors import ProtocolRefusal

        document = json.loads(self.hooks_path.read_text(encoding="utf-8"))
        old_block = {
            "hooks": [{
                "type": "command",
                "command": (
                    f"/usr/bin/python3 {self.destination / ('0' * 64) / 'scripts/floati-codex-wait'} "
                    f"--root {self.bus_home}"
                ),
                "timeout": 9,
                "statusMessage": "Watching Floati bus",
            }]
        }
        document["hooks"]["Stop"].extend([old_block, old_block])
        before = (json.dumps(document, indent=2, sort_keys=True) + "\n").encode()
        self.hooks_path.write_bytes(before)

        with self.assertRaisesRegex(ProtocolRefusal, "codex_wait_hook_conflict"):
            self.installer().install(
                self.workspace,
                public_ids.builder('floati'),
                hook_timeout_seconds=10,
                wait_deadline_seconds=2,
            )
        self.assertEqual(before, self.hooks_path.read_bytes())

    def test_install_can_explicitly_take_over_same_bundle_for_a_new_session(self) -> None:
        first = self.installer().install(
            self.workspace,
            public_ids.builder('floati'),
            hook_timeout_seconds=10,
            wait_deadline_seconds=2,
            acknowledge_stop_hook_change=self.fresh_pair,
            session_id="installer-session-one",
        )
        second = self.installer().install(
            self.workspace,
            public_ids.builder('floati'),
            hook_timeout_seconds=10,
            wait_deadline_seconds=2,
            session_id="installer-session-two",
        )

        self.assertEqual(first["bundle_digest"], second["bundle_digest"])
        self.assertEqual("installer-session-two", second["acting_session_id"])
        self.assertNotEqual(first["session_receipt_id"], second["session_receipt_id"])

    def test_malformed_single_floati_match_refuses_before_any_installer_write(self) -> None:
        from floati.codex_wait_contract import WORKSPACE_MAP_RELATIVE
        from floati.errors import ProtocolRefusal

        document = json.loads(self.hooks_path.read_text(encoding="utf-8"))
        document["hooks"]["Stop"].append(
            {"hooks": [{"type": "command", "command": "echo floati-codex-wait"}]}
        )
        before = (json.dumps(document, indent=2, sort_keys=True) + "\n").encode()
        self.hooks_path.write_bytes(before)

        with self.assertRaisesRegex(ProtocolRefusal, "codex_wait_hook_conflict"):
            self.installer().install(
                self.workspace,
                public_ids.builder('floati'),
                hook_timeout_seconds=10,
                wait_deadline_seconds=2,
                session_id="installer-session",
            )

        self.assertEqual(before, self.hooks_path.read_bytes())
        self.assertFalse(self.destination.exists())
        self.assertFalse((self.bus_home / WORKSPACE_MAP_RELATIVE).exists())
        self.assertFalse((self.bus_home / "receipts/codex-wait-consent").exists())
        self.assertFalse((self.bus_home / "receipts/codex-wait-session").exists())

    def test_rewrite_preserves_noncanonical_subagent_stop_bytes(self) -> None:
        marker = '"SubagentStop" : [ { "hooks" : [ { "type" : "command", "command" : "subagent-only" } ] } ]'
        self.hooks_path.write_text(
            '{\n  "hooks" : {\n    "Stop" : [],\n    '
            + marker
            + ',\n    "SessionStart" : []\n  }\n}\n',
            encoding="utf-8",
        )

        self.installer().install(
            self.workspace,
            public_ids.builder('floati'),
            hook_timeout_seconds=10,
            wait_deadline_seconds=2,
            acknowledge_stop_hook_change=self.fresh_pair,
        )

        self.assertIn(marker.encode(), self.hooks_path.read_bytes())


if __name__ == "__main__":
    unittest.main()

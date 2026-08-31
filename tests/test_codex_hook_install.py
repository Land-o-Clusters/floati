"""Contracts for the additive, digest-receipted Codex waiter installer."""

from __future__ import annotations

from floati import fixture_ids as public_ids

import json
import tempfile
import unittest
from pathlib import Path

from floati.registry import Registry
from floati.root import FloatiRoot
from tests.temp_roots import REAL_TEMP_ROOT


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

    def installer(self):
        from floati.codex_hook_install import CodexHookInstaller

        return CodexHookInstaller(
            source_root=self.source,
            bus_home=self.bus_home,
            hooks_path=self.hooks_path,
            destination=self.destination,
        )

    def test_install_adds_one_block_and_preserves_every_existing_block(self) -> None:
        receipt = self.installer().install(
            self.workspace,
            public_ids.builder('floati'),
            hook_timeout_seconds=10,
            wait_deadline_seconds=2,
        )

        installed = json.loads(self.hooks_path.read_text(encoding="utf-8"))
        self.assertEqual(self.original_stop, installed["hooks"]["Stop"][:2])
        self.assertEqual(3, len(installed["hooks"]["Stop"]))
        block = installed["hooks"]["Stop"][2]
        self.assertEqual("Watching Floati bus", block["hooks"][0]["statusMessage"])
        self.assertEqual(10, block["hooks"][0]["timeout"])
        self.assertIn(str(self.bus_home), block["hooks"][0]["command"])
        self.assertIn(receipt["bundle_digest"], block["hooks"][0]["command"])
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

    def test_exact_retry_never_duplicates_the_floati_block(self) -> None:
        first = self.installer().install(
            self.workspace,
            public_ids.builder('floati'),
            hook_timeout_seconds=10,
            wait_deadline_seconds=2,
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

        receipt = self.installer().install(
            self.workspace,
            public_ids.builder('floati'),
            hook_timeout_seconds=10,
            wait_deadline_seconds=2,
            session_id="installer-session",
        )

        installed = json.loads(self.hooks_path.read_text(encoding="utf-8"))
        self.assertEqual(self.original_stop[0], installed["hooks"]["Stop"][0])
        self.assertEqual(self.original_stop[1], installed["hooks"]["Stop"][2])
        self.assertEqual(subagent_before, installed["hooks"]["SubagentStop"])
        replacement = installed["hooks"]["Stop"][1]
        self.assertIn(receipt["bundle_digest"], replacement["hooks"][0]["command"])
        self.assertEqual("replaced", receipt["state"])
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
        )

        self.assertIn(marker.encode(), self.hooks_path.read_bytes())


if __name__ == "__main__":
    unittest.main()

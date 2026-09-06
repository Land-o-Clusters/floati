"""WAKE-CLAIM-1: two live sessions of one seat cannot silently arm over each other."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from floati import fixture_ids as public_ids
from floati.codex_wait_contract import (
    WORKSPACE_MAP_RELATIVE,
    CodexWaitConsentLedger,
    resolve_participant,
)
from tests.test_cli import LAUNCHER


REPOSITORY_ROOT = Path(__file__).parents[1]


class WakeClaim1Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.root = self.base / "fleet"
        initialized = self.run_cli("init", "--root", str(self.root))
        self.assertEqual(0, initialized.returncode, initialized.stderr)

    def run_cli(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(LAUNCHER), *arguments],
            cwd=REPOSITORY_ROOT,
            env=dict(os.environ),
            text=True,
            capture_output=True,
            check=False,
        )

    def artifact(self, result: subprocess.CompletedProcess[str]) -> dict:
        self.assertEqual("", result.stderr, result.stderr)
        return json.loads(result.stdout)

    def _arm_workspace(self) -> Path:
        registered = self.run_cli(
            "register",
            "--root",
            str(self.root),
            public_ids.builder("floati"),
            "--harness",
            "Codex",
        )
        self.assertEqual(0, registered.returncode, registered.stderr)
        workspace = self.base / "workspace"
        workspace.mkdir()
        map_path = self.root / WORKSPACE_MAP_RELATIVE
        map_path.parent.mkdir(parents=True)
        map_path.write_text(
            json.dumps(
                {
                    "schema_version": 0,
                    "tenant_id": self.root.name,
                    "mappings": [
                        {
                            "workspace": str(workspace),
                            "node_id": public_ids.builder("floati"),
                        }
                    ],
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n",
            encoding="utf-8",
        )
        participant = resolve_participant(self.root, workspace)
        self.assertIsNotNone(participant)
        CodexWaitConsentLedger(participant.root).arm(
            participant.binding,
            hook_timeout_seconds=10,
            wait_deadline_seconds=2,
            idempotency_key="wake-claim-1-consent",
        )
        return workspace

    def test_live_second_session_wake_arm_refuses_without_takeover(self) -> None:
        """Today's 18:22Z instance: two sessions of one seat armed over each other."""

        workspace = self._arm_workspace()
        first = self.run_cli(
            "wake",
            "arm",
            "--root",
            str(self.root),
            "--as",
            public_ids.builder("floati"),
            "--session",
            "session-one",
            "--workspace",
            str(workspace),
            "--idempotency-key",
            "arm-one",
        )
        second = self.run_cli(
            "wake",
            "arm",
            "--root",
            str(self.root),
            "--as",
            public_ids.builder("floati"),
            "--session",
            "session-two",
            "--workspace",
            str(workspace),
            "--idempotency-key",
            "arm-two",
        )

        self.assertEqual(0, first.returncode, first.stderr)
        self.assertEqual("arm", self.artifact(first)["evidence"]["operation"])
        self.assertEqual(20, second.returncode, second.stdout)
        refused = self.artifact(second)
        self.assertEqual("refused", refused["status"])
        self.assertEqual("seat_board_claim_contested", refused["evidence"]["code"])
        self.assertIn("session-one", refused["evidence"]["detail"])
        self.assertIn("--take-over", refused["evidence"]["detail"])
        self.assertNotEqual(
            {"kind": "none", "why": "no action was named for this refusal"},
            refused["evidence"].get("remedy"),
        )

    def test_live_second_session_wake_arm_with_takeover_is_predecessor_bound(self) -> None:
        workspace = self._arm_workspace()
        first = self.run_cli(
            "wake",
            "arm",
            "--root",
            str(self.root),
            "--as",
            public_ids.builder("floati"),
            "--session",
            "session-one",
            "--workspace",
            str(workspace),
            "--idempotency-key",
            "arm-one",
        )
        second = self.run_cli(
            "wake",
            "arm",
            "--root",
            str(self.root),
            "--as",
            public_ids.builder("floati"),
            "--session",
            "session-two",
            "--workspace",
            str(workspace),
            "--idempotency-key",
            "arm-two",
            "--take-over",
        )

        self.assertEqual(0, first.returncode, first.stderr)
        self.assertEqual(0, second.returncode, second.stderr)
        first_row = self.artifact(first)["evidence"]
        second_row = self.artifact(second)["evidence"]
        self.assertEqual("takeover", second_row["operation"])
        self.assertEqual("session-two", second_row["acting_session_id"])
        self.assertEqual(first_row["id"], second_row["predecessor_receipt_id"])

    def test_paused_predecessor_is_adoptable_without_takeover(self) -> None:
        workspace = self._arm_workspace()
        first = self.run_cli(
            "wake",
            "arm",
            "--root",
            str(self.root),
            "--as",
            public_ids.builder("floati"),
            "--session",
            "session-one",
            "--workspace",
            str(workspace),
            "--idempotency-key",
            "arm-one",
        )
        self.assertEqual(0, first.returncode, first.stderr)
        paused = self.run_cli(
            "wake",
            "pause",
            "--root",
            str(self.root),
            "--as",
            public_ids.builder("floati"),
            "--session",
            "session-one",
            "--idempotency-key",
            "pause-one",
        )
        self.assertEqual(0, paused.returncode, paused.stderr)
        second = self.run_cli(
            "wake",
            "arm",
            "--root",
            str(self.root),
            "--as",
            public_ids.builder("floati"),
            "--session",
            "session-two",
            "--workspace",
            str(workspace),
            "--idempotency-key",
            "arm-two",
        )

        self.assertEqual(0, second.returncode, second.stderr)
        first_row = self.artifact(first)["evidence"]
        second_row = self.artifact(second)["evidence"]
        self.assertEqual("takeover", second_row["operation"])
        self.assertEqual("session-two", second_row["acting_session_id"])
        self.assertEqual(first_row["id"], second_row["predecessor_receipt_id"])

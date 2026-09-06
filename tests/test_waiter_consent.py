"""WAKE-IDLE-1-F1: `floati waiter arm` — the public, harness-typed consent verb.

The only code path that armed waiter consent was the Codex hook installer,
so every other harness armed it through a Python heredoc on the consent
ledger. This row gives the runbook one verb: it checks the seat's registered
harness, maps the workspace, and arms the same consent ledger under the
same derived-key idempotency. It writes nothing until every check has
passed — a refused arm leaves no mutation.
"""

from __future__ import annotations

from floati import fixture_ids as public_ids

import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from floati.jsonl import read_records_snapshot
from floati.registry import Registry
from floati.root import FloatiRoot
from tests.temp_roots import REAL_TEMP_ROOT

ZCODE_SEAT = public_ids.worker("zcode")
CODEX_SEAT = public_ids.worker("codex")


def consent_ledger_rows(root: FloatiRoot, node: str) -> list[dict]:
    return read_records_snapshot(
        root,
        Path("receipts/codex-wait-consent") / public_ids.ledger(node),
        allowed_kinds={"codex_wait_consent_receipt"},
    )


class WaiterConsentFixture:
    """One registered seat on one fresh bus home; the map starts absent."""

    def __init__(self, base: Path, name: str, node: str, role: str) -> None:
        self.bus_home = base / name
        self.root = FloatiRoot.open_direct_home(self.bus_home, create=True)
        Registry(self.root).register(node, role)
        self.node = node
        self.workspace = base / (name + "-workspace")
        self.workspace.mkdir()

    def run_arm(
        self,
        *,
        node: str,
        harness: str,
        hook_timeout_seconds: int = 30,
        wait_deadline_seconds: int = 25,
        workspace: Path | None = None,
    ) -> tuple[int, dict]:
        from floati import cli

        argv = [
            "waiter",
            "arm",
            "--root",
            str(self.bus_home),
            "--node",
            node,
            "--workspace",
            str(self.workspace if workspace is None else workspace),
            "--harness",
            harness,
            "--hook-timeout-seconds",
            str(hook_timeout_seconds),
            "--wait-deadline-seconds",
            str(wait_deadline_seconds),
        ]
        stdout = io.StringIO()
        with mock.patch("sys.stdout", stdout), mock.patch("sys.stdin", io.StringIO("")):
            status = cli.main(argv)
        return status, json.loads(stdout.getvalue())


class WaiterArmRegistrationTests(unittest.TestCase):
    def test_waiter_arm_is_a_registered_public_verb(self) -> None:
        """RED today: the verb does not exist, so the parser has no row."""

        from floati.cli import _parser
        from floati.command_contract import describe_parser

        rows = describe_parser(_parser())["commands"]
        row = next((r for r in rows if r["path"] == ["waiter", "arm"]), None)

        self.assertIsNotNone(row, "floati waiter arm is not registered on the parser")
        assert row is not None
        self.assertTrue(row["public"], "floati waiter arm must be an operator surface")
        declared = {
            name for argument in row["arguments"] for name in argument["option_strings"]
        }
        for required in (
            "--root",
            "--node",
            "--workspace",
            "--harness",
            "--hook-timeout-seconds",
            "--wait-deadline-seconds",
        ):
            self.assertIn(required, declared)


class WaiterArmTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.zcode = WaiterConsentFixture(self.base, "zcode-fleet", ZCODE_SEAT, "zcode")

    def test_waiter_arm_maps_the_workspace_and_arms_consent(self) -> None:
        """The runbook's step 3, as one verb: map, resolve, arm."""

        from floati.codex_wait_contract import resolve_participant

        status, artifact = self.zcode.run_arm(
            node=ZCODE_SEAT, harness="zcode"
        )

        self.assertEqual(0, status)
        self.assertEqual("ok", artifact["status"])
        evidence = artifact["evidence"]
        self.assertEqual(ZCODE_SEAT, evidence["node_id"])
        self.assertEqual("zcode", evidence["harness"])
        self.assertEqual(30, evidence["hook_timeout_seconds"])
        self.assertEqual(25, evidence["wait_deadline_seconds"])
        self.assertEqual("armed", evidence["state"])
        self.assertTrue(evidence["consent_receipt_id"].startswith("codex-wait-consent-"))
        self.assertTrue(evidence["idempotency_key"].startswith("waiter-consent-arm-"))
        participant = resolve_participant(self.zcode.bus_home, self.zcode.workspace)
        self.assertIsNotNone(participant, "armed workspace did not resolve")
        assert participant is not None
        self.assertEqual(ZCODE_SEAT, participant.binding.node_id)
        rows = consent_ledger_rows(self.zcode.root, ZCODE_SEAT)
        self.assertEqual(1, len(rows))
        self.assertEqual("armed", rows[-1]["state"])
        self.assertEqual(evidence["consent_receipt_id"], rows[-1]["id"])
        self.assertEqual(30, rows[-1]["hook_timeout_seconds"])
        self.assertEqual(25, rows[-1]["wait_deadline_seconds"])

    def test_waiter_arm_is_idempotent_under_the_derived_key(self) -> None:
        """The same numbers name the same key, so the receipt is reused."""

        first_status, first = self.zcode.run_arm(node=ZCODE_SEAT, harness="zcode")
        map_bytes = (self.zcode.bus_home / "codex-wait" / "workspaces.v0.json").read_bytes()

        second_status, second = self.zcode.run_arm(node=ZCODE_SEAT, harness="zcode")

        self.assertEqual(0, first_status)
        self.assertEqual(0, second_status)
        self.assertEqual(
            first["evidence"]["consent_receipt_id"],
            second["evidence"]["consent_receipt_id"],
        )
        self.assertEqual(1, len(consent_ledger_rows(self.zcode.root, ZCODE_SEAT)))
        self.assertEqual(
            map_bytes,
            (self.zcode.bus_home / "codex-wait" / "workspaces.v0.json").read_bytes(),
        )

    def test_waiter_arm_re_arms_when_a_number_changes(self) -> None:
        """A changed deadline changes the key and appends a new armed receipt."""

        _, first = self.zcode.run_arm(node=ZCODE_SEAT, harness="zcode")
        _, second = self.zcode.run_arm(
            node=ZCODE_SEAT, harness="zcode", wait_deadline_seconds=20
        )

        self.assertNotEqual(
            first["evidence"]["consent_receipt_id"],
            second["evidence"]["consent_receipt_id"],
        )
        rows = consent_ledger_rows(self.zcode.root, ZCODE_SEAT)
        self.assertEqual(2, len(rows))
        self.assertEqual("armed", rows[-1]["state"])
        self.assertEqual(20, rows[-1]["wait_deadline_seconds"])

    def test_waiter_arm_arms_a_codex_harness_too(self) -> None:
        """The verb is harness-typed, not zcode-only: Codex seats arm through it."""

        fixture = WaiterConsentFixture(self.base, "codex-fleet", CODEX_SEAT, "codex")

        status, artifact = fixture.run_arm(node=CODEX_SEAT, harness="codex")

        self.assertEqual(0, status)
        self.assertEqual("ok", artifact["status"])
        self.assertEqual(CODEX_SEAT, artifact["evidence"]["node_id"])
        self.assertEqual("codex", artifact["evidence"]["harness"])

    def test_waiter_arm_refuses_a_harness_the_node_is_not(self) -> None:
        """Arming is deliberate and typed: the registry role must agree."""

        status, artifact = self.zcode.run_arm(node=ZCODE_SEAT, harness="codex")

        self.assertEqual(20, status)
        self.assertEqual("refused", artifact["status"])
        self.assertEqual("waiter_harness_mismatch", artifact["evidence"]["code"])
        self.assertIn("zcode", artifact["evidence"]["detail"])
        self.assertFalse(
            (self.zcode.bus_home / "codex-wait" / "workspaces.v0.json").exists(),
            "a refused arm mapped the workspace anyway",
        )
        self.assertFalse((self.zcode.bus_home / "receipts" / "codex-wait-consent").exists())

    def test_waiter_arm_refuses_a_deadline_at_the_hook_timeout(self) -> None:
        """The ledger's own bound is pre-checked, so a refusal writes nothing."""

        status, artifact = self.zcode.run_arm(
            node=ZCODE_SEAT, harness="zcode", wait_deadline_seconds=30
        )

        self.assertEqual(20, status)
        self.assertEqual("refused", artifact["status"])
        self.assertEqual("wait_deadline_invalid", artifact["evidence"]["code"])
        self.assertFalse(
            (self.zcode.bus_home / "codex-wait" / "workspaces.v0.json").exists(),
            "a refused arm mapped the workspace anyway",
        )
        self.assertFalse((self.zcode.bus_home / "receipts" / "codex-wait-consent").exists())

    def test_waiter_arm_refuses_an_unregistered_node(self) -> None:
        status, artifact = self.zcode.run_arm(node="stranger", harness="zcode")

        self.assertEqual(20, status)
        self.assertEqual("refused", artifact["status"])
        self.assertEqual("unknown_node", artifact["evidence"]["code"])
        self.assertFalse((self.zcode.bus_home / "codex-wait" / "workspaces.v0.json").exists())

    def test_waiter_arm_refuses_a_missing_workspace(self) -> None:
        status, artifact = self.zcode.run_arm(
            node=ZCODE_SEAT, harness="zcode",
            workspace=self.base / "nowhere",
        )

        self.assertEqual(20, status)
        self.assertEqual("refused", artifact["status"])
        self.assertFalse((self.zcode.bus_home / "codex-wait" / "workspaces.v0.json").exists())


if __name__ == "__main__":
    unittest.main()

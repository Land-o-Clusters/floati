from __future__ import annotations

from floati import fixture_ids as public_ids

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from floati.codex_wait_contract import (
    WORKSPACE_MAP_RELATIVE,
    CodexWaitConsentLedger,
    resolve_participant,
)


REPOSITORY_ROOT = Path(__file__).parents[1]


class AdminCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.root = self.base / "fleet"
        initialized = self.run_cli("init", "--root", str(self.root))
        self.assertEqual(0, initialized.returncode, initialized.stderr)

    def run_cli(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["python3", "-m", "floati", *arguments],
            cwd=REPOSITORY_ROOT,
            env=dict(os.environ),
            text=True,
            capture_output=True,
            check=False,
        )

    def artifact(self, result: subprocess.CompletedProcess[str]) -> dict:
        self.assertEqual("", result.stderr, result.stderr)
        return json.loads(result.stdout)

    def test_register_and_retire_compose_the_nested_workspace_contract(self) -> None:
        """Catches CLI registration bypassing B2 workspace creation or retention."""
        registered = self.run_cli(
            "register", "--root", str(self.root), public_ids.builder('a'), "--harness", "Codex",
            "--create-workspace",
        )
        self.assertEqual(0, registered.returncode, registered.stderr)
        registered_artifact = self.artifact(registered)
        workspace = self.root / "nodes" / public_ids.builder('a')
        self.assertTrue(workspace.is_dir())
        self.assertEqual("created", registered_artifact["evidence"]["workspace"]["state"])

        retired = self.run_cli("retire", "--root", str(self.root), public_ids.builder('a'))

        self.assertEqual(0, retired.returncode, retired.stderr)
        self.assertEqual("retained", self.artifact(retired)["evidence"]["workspace"]["state"])
        self.assertTrue(workspace.is_dir())

    def test_node_add_and_switch_emit_exact_preview_rows_before_receipted_commits(self) -> None:
        """Catches the activated wizard writing guessed rows or losing model testimony."""
        added = self.run_cli(
            "node", "add", "--root", str(self.root), "--node", public_ids.verifier(),
            "--harness", public_ids.compose('opencode-', public_ids.verifier()), "--lifetime", "temporary",
            "--lease-minutes", "60",
        )
        self.assertEqual(0, added.returncode, added.stderr)
        added_evidence = self.artifact(added)["evidence"]
        self.assertEqual(2, len(added_evidence["records"]))
        self.assertEqual(2, len(added_evidence["preview_rows"]))
        self.assertTrue((self.root / "nodes" / public_ids.verifier()).is_dir())

        switched = self.run_cli(
            "node", "switch", "--root", str(self.root), "--node", public_ids.verifier(),
            "--harness", "Cursor", "--model", "gpt-5.6",
        )

        self.assertEqual(0, switched.returncode, switched.stderr)
        switched_evidence = self.artifact(switched)["evidence"]
        self.assertEqual("provider_switch_receipt", switched_evidence["records"][1]["kind"])
        self.assertEqual(2, len(switched_evidence["preview_rows"]))

    def test_role_list_show_and_assignment_use_the_shipped_typed_library(self) -> None:
        """Catches CLI role handling inventing prose or bypassing the typed record."""
        listed = self.run_cli("role", "list", "--root", str(self.root))
        self.assertEqual(0, listed.returncode, listed.stderr)
        self.assertEqual(
            [
                "architect",
                "builder",
                "github-manager",
                "researcher",
                "reviewer",
                "sre",
            ],
            self.artifact(listed)["evidence"]["roles"],
        )
        shown = self.run_cli("role", "show", "--root", str(self.root), "architect")
        self.assertEqual(0, shown.returncode, shown.stderr)
        self.assertEqual("architect", self.artifact(shown)["evidence"]["template"]["role"])

        registered = self.run_cli(
            "register", "--root", str(self.root), "architect-a", "--harness", "Codex",
            "--create-workspace",
        )
        self.assertEqual(0, registered.returncode, registered.stderr)
        assigned = self.run_cli(
            "node", "role", "--root", str(self.root), "--node", "architect-a",
            "--template", "architect", "--answer", "repo=floati",
            "--answer", "never_touch=foreign-bus", "--answer", "owner_stops=flip",
        )

        self.assertEqual(0, assigned.returncode, assigned.stderr)
        evidence = self.artifact(assigned)["evidence"]
        self.assertEqual("registry_role_record", evidence["records"][0]["kind"])
        self.assertEqual(1, len(evidence["preview_rows"]))

    def test_chart_and_survey_are_explicit_declared_root_reads(self) -> None:
        """Catches chart or survey discovering roots or mutating a surveyed candidate."""
        registered = self.run_cli(
            "register", "--root", str(self.root), "architect-a", "--harness", "architect",
        )
        self.assertEqual(0, registered.returncode, registered.stderr)
        declarations = self.base / "declared.json"
        declarations.write_text(
            json.dumps(
                {
                    "schema_version": 0,
                    "roots": [{
                        "bus_id": "fleet", "root": str(self.root),
                        "architect_node": "architect-a", "downstream": [],
                    }],
                }
            ) + "\n",
            encoding="utf-8",
        )
        candidate = self.base / "foreign"
        candidate.mkdir()
        ledger = candidate / "events.jsonl"
        ledger.write_text("foreign bytes\n", encoding="utf-8")
        before = ledger.stat()

        chart = self.run_cli("chart", "--declared-roots", str(declarations), "--json")
        survey = self.run_cli(
            "survey", "--declared-roots", str(declarations),
            "--search-path", str(self.base), "--json",
        )

        self.assertEqual(0, chart.returncode, chart.stderr)
        self.assertEqual("declared_roots_and_ledgers", self.artifact(chart)["evidence"]["source"])
        self.assertEqual(0, survey.returncode, survey.stderr)
        self.assertEqual("explicit_user_request", self.artifact(survey)["evidence"]["invocation"])
        after = ledger.stat()
        self.assertEqual((before.st_dev, before.st_ino, before.st_size), (after.st_dev, after.st_ino, after.st_size))

    def test_admin_help_is_static_and_restamped(self) -> None:
        'Catches argparse-generated copy, or a DRAFT stamp surviving the reviewer voice pass.'
        for arguments, phrase in (
            (("node", "--help"), "floati node"),
            (("role", "--help"), "floati role"),
            (("chart", "--help"), "floati chart"),
            (("survey", "--help"), "floati survey"),
        ):
            with self.subTest(arguments=arguments):
                result = self.run_cli(*arguments)
                self.assertEqual(0, result.returncode, result.stderr)
                self.assertIn(phrase, result.stdout)
                self.assertNotIn("DRAFT -", result.stdout)

    def test_wake_arm_explicitly_transfers_one_workspace_to_one_session(self) -> None:
        registered = self.run_cli(
            "register", "--root", str(self.root), public_ids.builder('floati'), "--harness", "Codex",
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
                    "mappings": [{"workspace": str(workspace), "node_id": public_ids.builder('floati')}],
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n",
            encoding="utf-8",
        )
        participant = resolve_participant(self.root, workspace)
        self.assertIsNotNone(participant)
        consent = CodexWaitConsentLedger(participant.root).arm(
            participant.binding,
            hook_timeout_seconds=10,
            wait_deadline_seconds=2,
            idempotency_key="admin-cli-consent",
        )

        first = self.run_cli(
            "wake", "arm", "--root", str(self.root), "--as", public_ids.builder('floati'),
            "--session", "session-one", "--workspace", str(workspace),
            "--idempotency-key", "arm-one",
        )
        second = self.run_cli(
            "wake", "arm", "--root", str(self.root), "--as", public_ids.builder('floati'),
            "--session", "session-two", "--workspace", str(workspace),
            "--idempotency-key", "arm-two",
        )

        self.assertEqual(0, first.returncode, first.stderr)
        self.assertEqual(0, second.returncode, second.stderr)
        first_row = self.artifact(first)["evidence"]
        second_row = self.artifact(second)["evidence"]
        self.assertEqual("arm", first_row["operation"])
        self.assertEqual("takeover", second_row["operation"])
        self.assertEqual("session-two", second_row["acting_session_id"])
        self.assertEqual(first_row["id"], second_row["predecessor_receipt_id"])
        self.assertEqual(consent["id"], second_row["consent_receipt_id"])

    def test_boot_teardown_explain_and_state_flush_use_live_typed_sources(self) -> None:
        """Catches D3-D5 registration using cached topology or reading STATE.md content."""
        registered = self.run_cli(
            "register", "--root", str(self.root), "architect-a", "--harness", "Codex",
            "--create-workspace",
        )
        self.assertEqual(0, registered.returncode, registered.stderr)
        assigned = self.run_cli(
            "node", "role", "--root", str(self.root), "--node", "architect-a",
            "--template", "architect", "--answer", "repo=floati",
            "--answer", "never_touch=foreign-bus", "--answer", "owner_stops=flip",
        )
        self.assertEqual(0, assigned.returncode, assigned.stderr)
        declarations = self.base / "declared-projection.json"
        declarations.write_text(
            json.dumps(
                {
                    "schema_version": 0,
                    "roots": [{
                        "bus_id": "fleet", "root": str(self.root),
                        "architect_node": "architect-a", "downstream": [],
                    }],
                }
            ) + "\n",
            encoding="utf-8",
        )
        common = (
            "--root", str(self.root), "--node", "architect-a",
            "--declared-roots", str(declarations),
            "--managed-executable", "/usr/local/bin/floati-fleet",
            "--profile", public_ids.compose('puddle-floati-', public_ids.builder('floati')), "--json",
        )

        boot = self.run_cli("node", "boot", *common)
        teardown = self.run_cli("node", "teardown", *common)
        explained = self.run_cli("node", "explain", *common)

        self.assertEqual(0, boot.returncode, boot.stderr)
        self.assertEqual("node_boot_projection", self.artifact(boot)["evidence"]["kind"])
        self.assertEqual(0, teardown.returncode, teardown.stderr)
        self.assertEqual("node_teardown_projection", self.artifact(teardown)["evidence"]["kind"])
        self.assertEqual(0, explained.returncode, explained.stderr)
        self.assertEqual("node_boot_projection", self.artifact(explained)["evidence"]["kind"])

        state_file = self.root / "nodes" / "architect-a" / "STATE.md"
        state_file.write_text("private seat content\n", encoding="utf-8")
        flushed = self.run_cli(
            "node", "state-flush", "--root", str(self.root), "--node", "architect-a",
        )
        self.assertEqual(0, flushed.returncode, flushed.stderr)
        receipt = self.artifact(flushed)["evidence"]
        self.assertEqual("node_state_flush_receipt", receipt["kind"])
        self.assertNotIn("private seat content", flushed.stdout)


if __name__ == "__main__":
    unittest.main()

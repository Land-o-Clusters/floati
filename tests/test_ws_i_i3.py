"""WS-I I3: `node add --plan file.json` is an argument of add."""

from __future__ import annotations

from tests.test_cli import LAUNCHER

import argparse
import io
import json
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from floati.cli import _parser, main


REPOSITORY_ROOT = Path(__file__).parents[1]


def _node_add_parser() -> argparse.ArgumentParser:
    parser = _parser()
    commands = next(
        action
        for action in parser._actions
        if isinstance(action, argparse._SubParsersAction)
    )
    node_commands = next(
        action
        for action in commands.choices["node"]._actions
        if isinstance(action, argparse._SubParsersAction)
    )
    return node_commands.choices["add"]


class NodeAddPlanArgumentTests(unittest.TestCase):
    def test_plan_is_an_argument_of_add(self) -> None:
        """RED: --plan is not an argument of add."""

        options = [
            name
            for action in _node_add_parser()._actions
            for name in action.option_strings
        ]
        self.assertIn("--plan", options, "--plan is not an argument of add")


class NodeAddPlanMutationTests(unittest.TestCase):
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

    def test_plan_add_performs_the_same_mutation_as_flagged_add(self) -> None:
        plan = self.base / "node.json"
        plan.write_text(
            json.dumps(
                {
                    "node": "builder-plan",
                    "harness": "Codex",
                    "lifetime": "permanent",
                }
            ),
            encoding="utf-8",
        )
        added = self.run_cli(
            "node", "add", "--root", str(self.root), "--plan", str(plan)
        )
        self.assertEqual(0, added.returncode, added.stderr)
        evidence = self.artifact(added)["evidence"]
        self.assertEqual("builder-plan", evidence["records"][0]["node_id"])
        self.assertEqual("Codex", evidence["records"][0]["role"])
        self.assertEqual("active", evidence["records"][0]["state"])
        self.assertEqual(1, len(evidence["preview_rows"]))
        self.assertTrue((self.root / "nodes" / "builder-plan").is_dir())

        flagged = self.run_cli(
            "node",
            "add",
            "--root",
            str(self.root),
            "--node",
            "builder-flagged",
            "--harness",
            "Codex",
            "--lifetime",
            "permanent",
        )
        self.assertEqual(0, flagged.returncode, flagged.stderr)
        flagged_evidence = self.artifact(flagged)["evidence"]
        self.assertEqual(
            evidence["records"][0]["kind"],
            flagged_evidence["records"][0]["kind"],
        )
        self.assertEqual(
            evidence["records"][0]["role"],
            flagged_evidence["records"][0]["role"],
        )

    def test_plan_add_bypasses_the_door(self) -> None:
        plan = self.base / "door.json"
        plan.write_text(
            json.dumps(
                {
                    "node": "builder-plan-door",
                    "harness": "Codex",
                    "lifetime": "permanent",
                }
            ),
            encoding="utf-8",
        )
        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            patch(
                "floati.tui_doors.run_node_add_door",
                side_effect=AssertionError("plan add must bypass the door"),
            ),
            redirect_stdout(stdout),
            redirect_stderr(stderr),
        ):
            code = main(
                [
                    "node",
                    "add",
                    "--root",
                    str(self.root),
                    "--plan",
                    str(plan),
                ]
            )
        self.assertEqual(0, code, stderr.getvalue())
        self.assertEqual(
            "builder-plan-door",
            json.loads(stdout.getvalue())["evidence"]["records"][0]["node_id"],
        )

    def test_plan_mixed_with_identity_flags_refuses(self) -> None:
        plan = self.base / "mix.json"
        plan.write_text(
            json.dumps(
                {
                    "node": "builder-mix",
                    "harness": "Codex",
                    "lifetime": "permanent",
                }
            ),
            encoding="utf-8",
        )
        mixed = self.run_cli(
            "node",
            "add",
            "--root",
            str(self.root),
            "--plan",
            str(plan),
            "--node",
            "builder-mix",
        )
        self.assertEqual(20, mixed.returncode, mixed.stdout)
        artifact = json.loads(mixed.stdout)
        self.assertEqual("refused", artifact["status"])
        self.assertEqual("arguments_invalid", artifact["evidence"]["code"])

    def test_relative_plan_path_refuses(self) -> None:
        relative = self.run_cli(
            "node",
            "add",
            "--root",
            str(self.root),
            "--plan",
            "node.json",
        )
        self.assertEqual(20, relative.returncode, relative.stdout)
        artifact = json.loads(relative.stdout)
        self.assertEqual("node_add_plan_path_not_absolute", artifact["evidence"]["code"])

    def test_plan_adopt_false_refuses_before_commit(self) -> None:
        sibling = self.base / "other-bus"
        sibling.mkdir()
        (sibling / "events.jsonl").write_bytes(b"")
        before = (sibling / "events.jsonl").read_bytes()
        plan = self.base / "no-adopt.json"
        plan.write_text(
            json.dumps(
                {
                    "node": "builder-plan-adopt",
                    "harness": "Codex",
                    "lifetime": "permanent",
                    "survey": True,
                    "adopt": False,
                }
            ),
            encoding="utf-8",
        )
        refused = self.run_cli(
            "node", "add", "--root", str(self.root), "--plan", str(plan)
        )
        self.assertEqual(20, refused.returncode, refused.stdout)
        artifact = json.loads(refused.stdout)
        self.assertEqual("refused", artifact["status"])
        self.assertEqual(
            "wizard_undeclared_bus_not_adopted", artifact["evidence"]["code"]
        )
        self.assertFalse((self.root / "nodes" / "builder-plan-adopt").exists())
        self.assertEqual(before, (sibling / "events.jsonl").read_bytes())


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

from floati.cli import _parser
from floati.helptext import HELP


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def _registered_paths(parser: argparse.ArgumentParser) -> set[tuple[str, ...]]:
    paths: set[tuple[str, ...]] = set()

    def visit(current: argparse.ArgumentParser, prefix: tuple[str, ...]) -> None:
        for action in current._actions:
            if not isinstance(action, argparse._SubParsersAction):
                continue
            for name, child in action.choices.items():
                path = prefix + (name,)
                paths.add(path)
                visit(child, path)

    visit(parser, ())
    return paths


class DescribeContractTests(unittest.TestCase):
    def run_cli(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        environment = dict(os.environ)
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        return subprocess.run(
            [sys.executable, "-m", "floati", *arguments],
            cwd=REPOSITORY_ROOT,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
        )

    def test_describe_cli_emits_the_schema_versioned_machine_contract(self) -> None:
        """Catches the machine contract disappearing behind prose or argparse help."""

        completed = self.run_cli("describe", "--json")

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertEqual("", completed.stderr)
        artifact = json.loads(completed.stdout)
        self.assertEqual("describe", artifact["command"])
        self.assertEqual("ok", artifact["status"])
        self.assertEqual(0, artifact["evidence"]["schema_version"])
        self.assertEqual("command_contract", artifact["evidence"]["kind"])

    def test_describe_is_a_count_pinned_bijection_with_the_live_parser(self) -> None:
        """Catches either a described-only command or an argparse-only command."""

        try:
            from floati.command_contract import describe_parser
        except ImportError as exc:
            self.fail(f"live parser projection is missing: {exc}")

        parser = _parser()
        contract = describe_parser(parser)
        described = {tuple(row["path"]) for row in contract["commands"]}
        public = {
            tuple(row["path"])
            for row in contract["commands"]
            if row["public"]
        }
        hidden = described - public

        self.assertEqual(_registered_paths(parser), described)
        self.assertEqual(119, contract["command_count"])
        self.assertEqual(len(described), contract["command_count"])
        self.assertEqual(110, len(public))
        self.assertEqual({
            ("wake-evaluate",),
            ("wake-record",),
            ("wake-callback",),
            ("wake", "daemon", "serve"),
            ("update", "consent"),
            ("update", "revoke"),
            ("update", "status"),
            ("update", "check"),
            ("update", "apply"),
        }, hidden)
        self.assertEqual(
            [0, 20, 22, 31, 32, 33, 34, 35],
            [row["code"] for row in contract["exit_codes"]],
        )
        for row in contract["commands"]:
            with self.subTest(path=row["path"]):
                parsed = parser.parse_args(row["example_argv"])
                self.assertIsNotNone(parsed)

    def test_every_static_help_topic_resolves_to_a_described_command(self) -> None:
        """Catches either side of public parser/static-help drift."""

        try:
            from floati.command_contract import describe_parser
        except ImportError as exc:
            self.fail(f"live parser projection is missing: {exc}")

        rows = describe_parser(_parser())["commands"]
        self.assertTrue(all("public" in row for row in rows))
        described = {tuple(row["path"]) for row in rows if row["public"]}
        help_topics = {tuple(topic.split()) for topic in HELP if topic}
        self.assertEqual(help_topics, described)

    def test_agents_verb_table_names_every_public_root_command(self) -> None:
        """Catches an agent-facing root verb disappearing from the operator manual."""

        try:
            from floati.command_contract import describe_parser
        except ImportError as exc:
            self.fail(f"live parser projection is missing: {exc}")

        rows = describe_parser(_parser())["commands"]
        self.assertTrue(all("public" in row for row in rows))
        public_roots = {
            row["path"][0]
            for row in rows
            if row["public"] and len(row["path"]) == 1
        }
        agents = (REPOSITORY_ROOT / "AGENTS.md").read_text(encoding="utf-8")
        verbs = agents.split("## Verbs", 1)[1].split("## Standard workflows", 1)[0]
        missing = {
            command
            for command in public_roots
            if f"`{command}`" not in verbs and f"`{command} " not in verbs
        }
        self.assertEqual(set(), missing)

    def test_schema_version_projection_preserves_top_level_error_artifacts(self) -> None:
        """Catches incomplete nested commands silently losing their v1 envelope."""

        from floati.command_contract import schema_version_for_arguments

        parser = _parser()
        for command in (
            "effects",
            "effect",
            "threads",
            "thread",
            "wake-evaluate",
            "wake-record",
        ):
            with self.subTest(command=command):
                self.assertEqual(
                    1,
                    schema_version_for_arguments(parser, [command]),
                )

        self.assertIsNone(schema_version_for_arguments(parser, []))
        self.assertIsNone(schema_version_for_arguments(parser, ["status"]))


if __name__ == "__main__":
    unittest.main()

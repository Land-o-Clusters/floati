from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import unittest
from pathlib import Path

from floati.cli import _parser
from floati.helptext import HELP, _RAW, help_for


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def _agents_exit_code_rows(text: str) -> dict[int, str]:
    rows: dict[int, str] = {}
    inside = False
    for line in text.splitlines():
        if line.startswith("## Exit codes"):
            inside = True
            continue
        if inside and line.startswith("## "):
            break
        if inside and line.startswith("|"):
            cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
            if cells and cells[0].isdigit():
                rows[int(cells[0])] = cells[1]
    return rows


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
        self.assertEqual(131, contract["command_count"])
        self.assertEqual(len(described), contract["command_count"])
        self.assertEqual(122, len(public))
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

    def test_every_described_option_appears_in_the_rendered_help(self) -> None:
        """DOC-2: AGENTS.md promises `COMMAND --help` prints the full contract."""

        completed = self.run_cli("describe", "--json")
        self.assertEqual(0, completed.returncode, completed.stderr)
        commands = json.loads(completed.stdout)["evidence"]["commands"]
        missing: list[str] = []
        unrouted: list[str] = []
        for command in commands:
            if not command.get("public") or not command.get("executable"):
                continue
            topic = " ".join(command["path"])
            page = help_for([*command["path"], "--help"])
            if page is None:
                missing.append(f"{topic}: --help renders nothing")
                continue
            for argument in command["arguments"]:
                for option in argument["option_strings"]:
                    if option not in page:
                        missing.append(f"{topic}: {option}")
            dedicated = _RAW.get(topic)
            if dedicated is not None and page.splitlines()[1] != dedicated.splitlines()[1]:
                unrouted.append(
                    f"{topic}: --help serves '{dedicated.splitlines()[1].strip()}'"
                )
        self.assertEqual(
            [],
            missing,
            "options describe lists that the verb's --help never prints:\n"
            + "\n".join(missing),
        )
        self.assertEqual(
            [],
            unrouted,
            "dedicated help pages help_for never routes to:\n" + "\n".join(unrouted),
        )

    def test_agents_md_exit_code_table_agrees_with_the_describe_vocabulary(self) -> None:
        """DOC-2: the manual's exit codes are pinned to the projected vocabulary."""

        completed = self.run_cli("describe", "--json")
        self.assertEqual(0, completed.returncode, completed.stderr)
        vocabulary = json.loads(completed.stdout)["evidence"]["exit_codes"]
        text = (REPOSITORY_ROOT / "AGENTS.md").read_text(encoding="utf-8")
        rows = _agents_exit_code_rows(text)
        expected = {int(row["code"]): row["status"] for row in vocabulary}
        self.assertEqual(
            sorted(expected),
            sorted(rows),
            "AGENTS.md exit-code table codes disagree with describe --json",
        )
        for code, status in expected.items():
            self.assertIn(status, rows[code], f"AGENTS.md exit {code} row must name its status")
        status_list = re.search(r"`status` is (.*?)\.", text, re.DOTALL)
        self.assertIsNotNone(status_list, "AGENTS.md must enumerate the status vocabulary")
        for status in sorted({row["status"] for row in vocabulary}):
            self.assertIn(status, status_list.group(1), f"AGENTS.md status list omits {status}")

    def test_agents_md_documents_the_install_destination_variable_beside_destination(
        self,
    ) -> None:
        """DOC-2: FLOATI_INSTALL_DESTINATION is documented where --destination is."""

        text = (REPOSITORY_ROOT / "AGENTS.md").read_text(encoding="utf-8")
        section = text.split("## Install", 1)[1].split("\n## ", 1)[0]
        self.assertIn("--destination", section)
        self.assertIn(
            "FLOATI_INSTALL_DESTINATION",
            section,
            "the installer-shadow destination variable is undocumented where --destination is",
        )


if __name__ == "__main__":
    unittest.main()

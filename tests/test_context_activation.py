from __future__ import annotations

from floati import fixture_ids as public_ids

import io
import json
import os
import shlex
import shutil
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from floati import context
from floati.cli import _parser, main
from floati.helptext import help_for
from floati.manifest import _deployable_paths
from floati.registry import Registry
from floati.role_templates import SHIPPED_ROLE_NAMES
from floati.root import FloatiRoot


REPOSITORY_ROOT = Path(__file__).parents[1]


class ContextActivationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(dir="\x2fprivate/tmp")
        self.addCleanup(self.temporary.cleanup)
        self.root_path = Path(self.temporary.name) / "fleet"
        self.root = FloatiRoot.open_direct_home(self.root_path, create=True)
        Registry(self.root).register("builder-a", "codex")

    def run_cli(self, *arguments: str) -> tuple[int, dict[str, object], str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            status = main(list(arguments))
        rendered = stdout.getvalue() or stderr.getvalue()
        return status, json.loads(rendered), stdout.getvalue(), stderr.getvalue()

    def test_real_parser_and_handler_activate_the_dark_context_seam(self) -> None:
        """Catches E2 remaining importable but unreachable from the product CLI."""

        parser = _parser()
        for subcommand in ("status", "turnover"):
            with self.subTest(subcommand=subcommand):
                parsed = parser.parse_args(
                    [
                        "context",
                        subcommand,
                        "--root",
                        str(self.root_path),
                        "--as",
                        "builder-a",
                        "--json",
                    ]
                )
                self.assertEqual("context", parsed.command)
                self.assertEqual(subcommand, parsed.context_command)
                self.assertTrue(callable(parsed.handler))

        before = {
            path.relative_to(self.root_path): path.read_bytes()
            for path in self.root_path.rglob("*")
            if path.is_file()
        }
        status, artifact, stdout, stderr = self.run_cli(
            "context",
            "status",
            "--root",
            str(self.root_path),
            "--as",
            "builder-a",
            "--json",
        )
        after = {
            path.relative_to(self.root_path): path.read_bytes()
            for path in self.root_path.rglob("*")
            if path.is_file()
        }

        self.assertEqual(0, status, stderr)
        self.assertEqual("context_status_projection", artifact["evidence"]["kind"])
        self.assertEqual("", stderr)
        self.assertNotEqual("", stdout)
        self.assertEqual(before, after)

        refused, artifact, _, _ = self.run_cli(
            "context",
            "turnover",
            "--root",
            str(self.root_path),
            "--as",
            "builder-a",
            "--profile",
            "caller-selected",
        )
        self.assertEqual(20, refused)
        self.assertEqual("arguments_invalid", artifact["evidence"]["code"])

    def test_context_is_registered_once_at_the_canonical_cli_seam(self) -> None:
        """Catches stale integration work registering over Tide's live parser."""

        with patch("floati.context.register_cli", wraps=context.register_cli) as register:
            parser = _parser()

        self.assertEqual(1, register.call_count)
        self.assertEqual("context", parser.parse_args([
            "context", "status", "--root", str(self.root_path),
            "--as", "builder-a", "--json",
        ]).command)

    def test_context_help_is_static_voice_passed_copy_for_every_live_path(self) -> None:
        """Catches argparse fallback, hidden Tide verbs, or premature voice pass."""

        for arguments, synopsis in (
            (("context", "--help"), "floati context {status|turnover|policy|reading}"),
            (("context", "status", "--help"), "floati context status --root ROOT --as NODE [--json]"),
            (("context", "turnover", "--help"), "floati context turnover --root ROOT --as NODE [--json]"),
            (("context", "policy", "--help"), "floati context policy {set|show|clear}"),
            (("context", "policy", "set", "--help"), "floati context policy set --root ROOT --node NODE --metric METRIC"),
            (("context", "policy", "show", "--help"), "floati context policy show --root ROOT --node NODE [--json]"),
            (("context", "policy", "clear", "--help"), "floati context policy clear --root ROOT --node NODE --idempotency-key KEY [--json]"),
            (("context", "reading", "--help"), "floati context reading {record}"),
            (("context", "reading", "record", "--help"), "floati context reading record --root ROOT --as NODE --metric METRIC"),
        ):
            with self.subTest(arguments=arguments):
                stdout = io.StringIO()
                stderr = io.StringIO()
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    status = main(list(arguments))
                self.assertEqual(0, status, stderr.getvalue())
                self.assertTrue(stdout.getvalue().startswith("NAME\n"))
                self.assertIn(synopsis, stdout.getvalue())
                self.assertNotIn("DRAFT -", stdout.getvalue())
                self.assertEqual("", stderr.getvalue())

    def test_mutating_context_help_examples_execute_against_the_closed_catalog(self) -> None:
        """Catches provisional examples teaching refused Tide metric/value pairs."""

        for topic in (
            ("context", "policy", "set"),
            ("context", "reading", "record"),
        ):
            with self.subTest(topic=topic):
                page = help_for((*topic, "--help"))
                self.assertIsNotNone(page)
                examples = page.split("EXAMPLES\n", 1)[1]
                example = next(
                    line.strip()
                    for line in examples.splitlines()
                    if line.strip().startswith("floati ")
                )
                arguments = shlex.split(example)[1:]
                arguments = [
                    str(self.root_path) if value == "/var/tmp/fleet"
                    else "builder-a" if value == public_ids.builder('a')
                    else value
                    for value in arguments
                ]

                status, artifact, _, stderr = self.run_cli(*arguments)

                self.assertEqual(0, status, artifact)
                self.assertEqual("", stderr)

    def test_bundle_carries_context_and_exact_shipped_role_dependencies(self) -> None:
        """Catches activation installing code without the D1 provenance it consumes."""

        deployable = set(_deployable_paths(REPOSITORY_ROOT))
        manifest = json.loads(
            (REPOSITORY_ROOT / "bundle-manifest.v0.json").read_text(encoding="utf-8")
        )
        manifested = {entry["path"] for entry in manifest["files"]}
        required_context = {
            "floati/context.py",
            "floati/context_absences.py",
            "floati/context_absences_v0.py",
            "schemas/v0/context-absence-dataset.schema.json",
            "schemas/v0/context-projection.schema.json",
        }
        required_roles = {
            f"roles/shipped/{role}.json" for role in SHIPPED_ROLE_NAMES
        }

        self.assertEqual(required_roles, {
            path for path in deployable if path.startswith("roles/shipped/")
        })
        self.assertTrue(required_context | required_roles <= deployable)
        self.assertTrue(required_context | required_roles <= manifested)

    def test_manifest_only_installed_runtime_loads_shipped_roles(self) -> None:
        """Catches packaged role bytes that cannot be resolved by the installed CLI."""

        manifest = json.loads(
            (REPOSITORY_ROOT / "bundle-manifest.v0.json").read_text(encoding="utf-8")
        )
        destination = Path(self.temporary.name) / "installed"
        for entry in manifest["files"]:
            relative = Path(entry["path"])
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(REPOSITORY_ROOT / relative, target)

        environment = dict(os.environ)
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        environment.pop("PYTHONPATH", None)
        command = [
            str(destination / "scripts" / "floati"),
            "role",
            "list",
            "--root",
            str(self.root_path),
        ]

        result = subprocess.run(
            command,
            cwd=self.temporary.name,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(
            list(SHIPPED_ROLE_NAMES),
            json.loads(result.stdout)["evidence"]["roles"],
        )

        (destination / "roles" / "shipped" / "reviewer.json").unlink()
        missing = subprocess.run(
            command,
            cwd=self.temporary.name,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(20, missing.returncode, missing.stderr)
        self.assertEqual(
            "role_template_path_invalid",
            json.loads(missing.stderr)["evidence"]["code"],
        )


if __name__ == "__main__":
    unittest.main()

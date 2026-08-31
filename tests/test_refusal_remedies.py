from __future__ import annotations

import argparse
import io
import json
import os
import subprocess
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from floati.cli import main
from floati.errors import ProtocolRefusal


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


class RefusalRemedyTests(unittest.TestCase):
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

    def artifact_for(self, refusal: ProtocolRefusal) -> tuple[int, dict[str, object]]:
        parser = argparse.ArgumentParser(add_help=False)
        commands = parser.add_subparsers(dest="command", required=True)
        probe = commands.add_parser("remedy-probe", add_help=False)

        def refuse(_arguments: argparse.Namespace) -> tuple[str, dict[str, object], int]:
            raise refusal

        probe.set_defaults(handler=refuse)
        stdout = io.StringIO()
        stderr = io.StringIO()
        with mock.patch("floati.cli._parser", return_value=parser):
            with redirect_stdout(stdout), redirect_stderr(stderr):
                exit_code = main(["remedy-probe"])
        self.assertEqual("", stdout.getvalue())
        return exit_code, json.loads(stderr.getvalue())

    def test_supplied_remedy_survives_both_protocol_refusal_artifacts(self) -> None:
        """Catches refusal serialization dropping the caller's bounded remedy."""

        for code, expected_status, expected_exit in (
            ("example_refusal", "refused", 20),
            ("cannot_speak", "cannot_speak", 22),
        ):
            with self.subTest(code=code):
                try:
                    refusal = ProtocolRefusal(
                        code,
                        "example detail",
                        remedy="DRAFT - retry with --root",
                    )
                except TypeError as exc:
                    self.fail(f"ProtocolRefusal rejected its optional remedy: {exc}")

                exit_code, artifact = self.artifact_for(refusal)

                self.assertEqual(expected_exit, exit_code)
                self.assertEqual(expected_status, artifact["status"])
                self.assertEqual(
                    "DRAFT - retry with --root",
                    artifact["evidence"]["remedy"],
                )

    def test_absent_remedy_is_explicit_json_null_on_real_cli_refusals(self) -> None:
        """Catches omission being confused with typed absence at the CLI boundary."""

        for arguments, expected_exit, expected_status, expected_evidence in (
            (
                ("status",),
                22,
                "cannot_speak",
                {
                    "code": "cannot_speak",
                    "detail": "no command root was resolved from --root or FLOATI_BUS_ROOT",
                    "remedy": None,
                },
            ),
            (
                ("init", "--root", "relative"),
                20,
                "refused",
                {
                    "code": "root_not_absolute",
                    "detail": "the root path must be absolute",
                    "remedy": None,
                },
            ),
        ):
            with self.subTest(arguments=arguments):
                completed = self.run_cli(*arguments)
                artifact = json.loads(completed.stderr)

                self.assertEqual(expected_exit, completed.returncode)
                self.assertEqual(expected_status, artifact["status"])
                self.assertEqual(expected_evidence, artifact["evidence"])

    def test_empty_remedy_serializes_as_null_never_an_empty_claim(self) -> None:
        """Catches an empty remedy escaping as user-visible guidance."""

        try:
            refusal = ProtocolRefusal("example_refusal", "detail", remedy="")
        except TypeError as exc:
            self.fail(f"ProtocolRefusal rejected its optional remedy: {exc}")

        _, artifact = self.artifact_for(refusal)

        self.assertIn("remedy", artifact["evidence"])
        self.assertIsNone(artifact["evidence"]["remedy"])


if __name__ == "__main__":
    unittest.main()

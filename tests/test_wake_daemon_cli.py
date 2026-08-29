from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from floati.cli import main
from floati.registry import Registry
from floati.root import FloatiRoot


class WakeDaemonCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(dir="/private/tmp")
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.home = self.base / "home"
        self.home.mkdir()
        self.root_path = self.base / "fleet-alpha"
        self.root = FloatiRoot.open_direct_home(self.root_path, create=True)
        Registry(self.root).register("lane-a", "worker")
        self.workspace = self.base / "workspace"
        self.workspace.mkdir()
        self.executable = self.base / "cursor-agent"
        self.executable.write_bytes(b"#!/bin/sh\nexit 0\n")
        self.executable.chmod(0o700)

    def run_cli(self, *arguments: str) -> tuple[int, dict]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with mock.patch.dict(os.environ, {"HOME": str(self.home)}, clear=False):
            with redirect_stdout(stdout), redirect_stderr(stderr):
                status = main(list(arguments))
        payload = stdout.getvalue() or stderr.getvalue()
        return status, json.loads(payload)

    def identity(self, operation: str, harness: str = "cursor") -> list[str]:
        return [
            "wake", "daemon", operation,
            "--root", str(self.root_path),
            "--as", "lane-a",
            "--harness", harness,
        ]

    def bind(self) -> tuple[int, dict]:
        return self.run_cli(
            *self.identity("bind"),
            "--session", "cursor-session-1",
            "--workspace", str(self.workspace),
            "--executable", str(self.executable),
            "--binding-epoch", "1",
        )

    def consent(self) -> tuple[int, dict]:
        return self.run_cli(
            *self.identity("consent"),
            "--min-poll-seconds", "1",
            "--max-poll-seconds", "30",
            "--max-backoff-seconds", "120",
            "--activation-epoch", "1",
        )

    def test_status_before_consent_is_inactive_and_creates_no_daemon_state(self) -> None:
        before = {path.relative_to(self.root_path) for path in self.root_path.rglob("*")}
        status, artifact = self.run_cli(*self.identity("status"))
        after = {path.relative_to(self.root_path) for path in self.root_path.rglob("*")}

        self.assertEqual(0, status)
        self.assertEqual("inactive", artifact["evidence"]["state"])
        self.assertFalse(artifact["evidence"]["display"].startswith("DRAFT -"))
        self.assertEqual(before, after)

    def test_cursor_bind_is_exact_and_codex_bind_requires_the_waiter(self) -> None:
        status, artifact = self.bind()
        self.assertEqual(0, status)
        self.assertEqual("cursor-session-1", artifact["evidence"]["session_id"])
        self.assertFalse(artifact["evidence"]["display"].startswith("DRAFT -"))

        refused, payload = self.run_cli(
            *self.identity("bind", "codex"),
            "--session", "codex-session-1",
            "--workspace", str(self.workspace),
            "--executable", str(self.executable),
            "--binding-epoch", "1",
        )
        self.assertEqual(20, refused)
        self.assertEqual("wake_daemon_codex_binding_source_invalid", payload["evidence"]["code"])

    def test_consent_and_install_do_not_invoke_launchctl(self) -> None:
        self.bind()
        with mock.patch("subprocess.run") as run:
            consent_status, consent = self.consent()
            install_status, installed = self.run_cli(*self.identity("install"))
        self.assertEqual(0, consent_status)
        self.assertEqual("active", consent["evidence"]["state"])
        self.assertEqual(0, install_status)
        self.assertEqual("installed", installed["evidence"]["state"])
        self.assertEqual([], run.call_args_list)

    def test_bindingless_consent_can_be_revoked_without_launchctl(self) -> None:
        """Catches leaving a daemon consent requiring a binding that entry did not."""

        consent_status, _consent = self.consent()
        with mock.patch("subprocess.run") as run:
            revoke_status, revoked = self.run_cli(*self.identity("revoke"))
            status, inactive = self.run_cli(*self.identity("status"))

        self.assertEqual(0, consent_status)
        self.assertEqual(0, revoke_status)
        self.assertEqual("revoked", revoked["evidence"]["state"])
        self.assertEqual(
            "wake_daemon_lifecycle_receipt",
            revoked["evidence"]["receipt"]["kind"],
        )
        self.assertIsNone(revoked["evidence"]["receipt"]["session_digest"])
        self.assertIsNone(revoked["evidence"]["receipt"]["plist_digest"])
        self.assertEqual(0, status)
        self.assertEqual("inactive", inactive["evidence"]["state"])
        self.assertEqual([], run.call_args_list)

    def test_start_before_install_is_a_typed_refusal(self) -> None:
        self.bind()
        self.consent()
        status, artifact = self.run_cli(*self.identity("start"))
        self.assertEqual(20, status)
        self.assertEqual("wake_daemon_supervisor_missing", artifact["evidence"]["code"])

    def test_unsupported_harness_refuses_and_all_public_help_is_restamped(self) -> None:
        status, artifact = self.run_cli(*self.identity("status", "claude"))
        self.assertEqual(20, status)
        self.assertEqual("arguments_invalid", artifact["evidence"]["code"])

        for operation in (
            None, "consent", "bind", "install", "start", "status", "stop", "remove", "revoke"
        ):
            with self.subTest(operation=operation):
                args = ["wake", "daemon"]
                if operation is not None:
                    args.append(operation)
                args.append("--help")
                stdout = io.StringIO()
                with redirect_stdout(stdout):
                    self.assertEqual(0, main(args))
                self.assertNotIn("DRAFT -", stdout.getvalue())
                self.assertNotIn(" daemon serve ", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()

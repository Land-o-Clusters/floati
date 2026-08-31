from __future__ import annotations

from floati import fixture_ids as public_ids

import io
import json
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from floati.cli import main
from floati.copy import WAKE_DAEMON_GROK_BOUND_DISPLAY
from floati.errors import ProtocolRefusal
from floati.registry import Registry
from floati.root import FloatiRoot
from floati.wake_daemon_contract import AdapterBindingStore, DaemonCoordinate
from tests.temp_roots import REAL_TEMP_ROOT


class WakeDaemonCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.home = self.base / "home"
        self.home.mkdir()
        self.root_path = self.base / "fleet-alpha"
        self.root = FloatiRoot.open_direct_home(self.root_path, create=True)
        Registry(self.root).register("builder-a", "worker")
        self.workspace = self.base / "workspace"
        self.workspace.mkdir()
        self.executable = self.base / "cursor-agent"
        self.executable.write_bytes(b"#!/bin/sh\nexit 0\n")
        self.executable.chmod(0o700)
        # A fake cursor that answers a resume with a valid success result for
        # the bound session - the shape probe_resume validates.
        self.proof_executable = self.base / "cursor-proof"
        self.proof_executable.write_bytes(
            b"#!/bin/sh\n"
            b"printf '%s' '{\"type\":\"result\",\"subtype\":\"success\","
            b"\"is_error\":false,\"session_id\":\"cursor-session-1\"}'\n"
        )
        self.proof_executable.chmod(0o700)
        # A fake that leaves a marker whenever it RUNS - proves a declined
        # probe never spent the turn.
        self.marker = self.base / "probe-ran"
        self.spy_executable = self.base / "cursor-spy"
        self.spy_executable.write_bytes(
            f"#!/bin/sh\ntouch {self.marker}\nexit 0\n".encode()
        )
        self.spy_executable.chmod(0o700)

    def run_cli(self, *arguments: str, stdin: str = "") -> tuple[int, dict]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with mock.patch.dict(os.environ, {"HOME": str(self.home)}, clear=False):
            with redirect_stdout(stdout), redirect_stderr(stderr),\
                    mock.patch("sys.stdin", io.StringIO(stdin)):
                status = main(list(arguments))
        payload = stdout.getvalue() or stderr.getvalue()
        self._stderr = stderr.getvalue()
        return status, json.loads(payload)

    def bind_with(self, executable: Path, *, yes: bool = False, stdin: str = "") -> tuple[int, dict]:
        arguments = [
            *self.identity("bind"),
            "--session", "cursor-session-1",
            "--workspace", str(self.workspace),
            "--executable", str(executable),
            "--binding-epoch", "1",
        ]
        if yes:
            arguments.append("--yes")
        return self.run_cli(*arguments, stdin=stdin)

    def identity(self, operation: str, harness: str = "cursor") -> list[str]:
        return [
            "wake", "daemon", operation,
            "--root", str(self.root_path),
            "--as", "builder-a",
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

    def test_bind_refuses_an_adapter_that_declares_no_probe_class(self) -> None:
        """WD-R5a at the bind surface (R5d scope): an undeclared adapter cannot bind."""
        from floati import wake_daemon_adapters as module

        with mock.patch.object(module, "_ADAPTER_RESUME_PROBES", {"codex": "costs_one_turn"}):
            status, artifact = self.bind()

        self.assertEqual(20, status)
        self.assertEqual("wake_daemon_resume_probe_undeclared", artifact["evidence"]["code"])

    def test_bind_offers_the_probe_and_a_decline_records_resume_unproven(self) -> None:
        """WD-R5b: consent is a surface - the offer shows what will run and
        what it costs; a decline binds with a RECORDED absence, not silence."""
        status, artifact = self.bind_with(
            self.spy_executable, stdin="n\n",
        )

        self.assertEqual(0, status)
        self.assertIn("ONE resume", self._stderr)
        self.assertIn("costs one turn", self._stderr)
        self.assertIn("cursor-session-1", self._stderr)
        self.assertEqual("resume_unproven", artifact["evidence"]["resume_state"])
        self.assertFalse(self.marker.exists(), "a declined probe must not run")

    def test_bind_with_yes_runs_the_probe_and_records_resume_proven(self) -> None:
        """--yes is the exception: consent without the interactive ask."""
        status, artifact = self.bind_with(self.proof_executable, yes=True)

        self.assertEqual(0, status)
        self.assertEqual("resume_proven", artifact["evidence"]["resume_state"])

    def test_bind_eof_declines_without_spending_the_turn(self) -> None:
        """A non-interactive bind never spends a turn by default."""
        status, artifact = self.bind_with(self.spy_executable, stdin="")

        self.assertEqual(0, status)
        self.assertEqual("resume_unproven", artifact["evidence"]["resume_state"])
        self.assertFalse(self.marker.exists(), "EOF must not authorize the probe")

    def test_bind_probe_failure_refuses_wake_bind_target_unresumable(self) -> None:
        """On probe failure the bind is refused, carrying the observed
        duration and the remedy - and no binding is written."""
        status, artifact = self.bind_with(self.executable, yes=True)

        self.assertEqual(20, status)
        evidence = artifact["evidence"]
        self.assertEqual("wake_bind_target_unresumable", evidence["code"])
        self.assertIn("probe failed after", evidence["detail"])
        self.assertIn("headless", evidence["detail"])
        with self.assertRaises(ProtocolRefusal):
            AdapterBindingStore(self.root).read(
                DaemonCoordinate(self.root, "builder-a", "cursor")
            )

    def test_bind_probe_success_is_judged_by_the_result_not_the_exit_code(self) -> None:
        """An exit-0 resume that answers from the WRONG session is not a
        proven resume - it fails the bind like any other failure."""
        lying = self.base / "cursor-lying"
        lying.write_bytes(
            b"#!/bin/sh\n"
            b"printf '%s' '{\"type\":\"result\",\"subtype\":\"success\","
            b"\"is_error\":false,\"session_id\":\"someone-else\"}'\n"
        )
        lying.chmod(0o700)

        status, artifact = self.bind_with(lying, yes=True)

        self.assertEqual(20, status)
        self.assertEqual("wake_bind_target_unresumable", artifact["evidence"]["code"])

    def test_cursor_bind_is_exact_and_codex_bind_requires_the_waiter(self) -> None:
        status, artifact = self.bind()
        self.assertEqual(0, status)
        self.assertEqual("cursor-session-1", artifact["evidence"]["session_id"])
        self.assertEqual(
            "exact Cursor session binding recorded.",
            artifact["evidence"]["display"],
        )

        refused, payload = self.run_cli(
            *self.identity("bind", "codex"),
            "--session", "codex-session-1",
            "--workspace", str(self.workspace),
            "--executable", str(self.executable),
            "--binding-epoch", "1",
        )
        self.assertEqual(20, refused)
        self.assertEqual("wake_daemon_codex_binding_source_invalid", payload["evidence"]["code"])

    def test_grok_build_bind_is_exact(self) -> None:
        status, artifact = self.run_cli(
            *self.identity("bind", "grok-build"),
            "--session", public_ids.compose(public_ids.verifier(), '-session-1'),
            "--workspace", str(self.workspace),
            "--executable", str(self.executable),
            "--binding-epoch", "1",
        )

        self.assertEqual(0, status, artifact)
        self.assertEqual("grok-build", artifact["evidence"]["harness"])
        self.assertEqual(public_ids.compose(public_ids.verifier(), '-session-1'), artifact["evidence"]["session_id"])
        self.assertEqual(str(self.workspace), artifact["evidence"]["workspace"])
        self.assertEqual(str(self.executable), artifact["evidence"]["executable"])
        self.assertEqual(
            WAKE_DAEMON_GROK_BOUND_DISPLAY,
            artifact["evidence"]["display"],
        )

    def test_grok_build_consent_and_install_preserve_the_closed_coordinate(self) -> None:
        bind_status, _binding = self.run_cli(
            *self.identity("bind", "grok-build"),
            "--session", public_ids.compose(public_ids.verifier(), '-session-1'),
            "--workspace", str(self.workspace),
            "--executable", str(self.executable),
            "--binding-epoch", "1",
        )
        with mock.patch("subprocess.run") as run:
            consent_status, consent = self.run_cli(
                *self.identity("consent", "grok-build"),
                "--min-poll-seconds", "1",
                "--max-poll-seconds", "30",
                "--max-backoff-seconds", "120",
                "--activation-epoch", "7",
            )
            install_status, installed = self.run_cli(
                *self.identity("install", "grok-build")
            )

        self.assertEqual(0, bind_status)
        self.assertEqual(0, consent_status, consent)
        self.assertEqual("grok-build", consent["evidence"]["harness"])
        self.assertEqual(7, consent["evidence"]["activation_epoch"])
        self.assertEqual(0, install_status, installed)
        self.assertEqual("installed", installed["evidence"]["state"])
        receipt = installed["evidence"]["receipt"]
        self.assertEqual(
            "wake_daemon_lifecycle_receipt",
            receipt["kind"],
        )
        self.assertEqual("grok-build", receipt["harness"])
        self.assertEqual(consent["evidence"]["coordinate_digest"], receipt["coordinate_digest"])
        self.assertEqual(7, receipt["activation_epoch"])
        self.assertEqual([], run.call_args_list)

    def test_grok_build_lifecycle_and_exit_door_emit_exact_receipts(self) -> None:
        self.run_cli(
            *self.identity("bind", "grok-build"),
            "--session", public_ids.compose(public_ids.verifier(), '-session-1'),
            "--workspace", str(self.workspace),
            "--executable", str(self.executable),
            "--binding-epoch", "1",
        )
        self.run_cli(
            *self.identity("consent", "grok-build"),
            "--min-poll-seconds", "1",
            "--max-poll-seconds", "30",
            "--max-backoff-seconds", "120",
            "--activation-epoch", "7",
        )
        install_status, installed = self.run_cli(
            *self.identity("install", "grok-build")
        )
        launchctl_calls: list[tuple[str, ...]] = []

        def launchctl(argv, **_kwargs):
            call = tuple(argv)
            launchctl_calls.append(call)
            returncode = 113 if call[1] == "print" else 0
            return subprocess.CompletedProcess(call, returncode, "", "")

        with mock.patch("subprocess.run", side_effect=launchctl):
            start_status, started = self.run_cli(
                *self.identity("start", "grok-build")
            )
            status_status, status = self.run_cli(
                *self.identity("status", "grok-build")
            )
            stop_status, stopped = self.run_cli(
                *self.identity("stop", "grok-build")
            )
            remove_status, removed = self.run_cli(
                *self.identity("remove", "grok-build")
            )
            revoke_status, revoked = self.run_cli(
                *self.identity("revoke", "grok-build")
            )

        for exit_status, artifact in (
            (install_status, installed),
            (start_status, started),
            (stop_status, stopped),
            (remove_status, removed),
            (revoke_status, revoked),
        ):
            self.assertEqual(0, exit_status, artifact)
            receipt = artifact["evidence"]["receipt"]
            self.assertEqual("wake_daemon_lifecycle_receipt", receipt["kind"])
            self.assertEqual("grok-build", receipt["harness"])
            self.assertEqual(7, receipt["activation_epoch"])

        self.assertEqual(0, status_status, status)
        self.assertEqual("stopped", status["evidence"]["state"])
        self.assertEqual("unknown", revoked["evidence"]["state"])
        final_status, inactive = self.run_cli(
            *self.identity("status", "grok-build")
        )
        self.assertEqual(0, final_status)
        self.assertEqual("inactive", inactive["evidence"]["state"])
        self.assertEqual(
            ["bootstrap", "kickstart", "print", "bootout", "print"],
            [call[1] for call in launchctl_calls],
        )

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

    def test_unsupported_harness_refuses_and_top_level_help_is_restamped(self) -> None:
        status, artifact = self.run_cli(*self.identity("status", "claude"))
        self.assertEqual(20, status)
        self.assertEqual("arguments_invalid", artifact["evidence"]["code"])

        stdout = io.StringIO()
        with redirect_stdout(stdout):
            self.assertEqual(0, main(["wake", "daemon", "--help"]))
        self.assertNotIn("DRAFT -", stdout.getvalue())
        self.assertNotIn(" daemon serve ", stdout.getvalue())

    def test_grok_build_public_help_is_draft_stamped(self) -> None:
        approved_legacy = {
            "consent": (
                "floati wake daemon consent - record exact activation consent",
                "Record bounded polling consent for one exact adapter contract.",
                "--harness cursor --min-poll-seconds",
            ),
            "bind": (
                "floati wake daemon bind - bind one exact session",
                "Record one exact session, workspace, and executable digest.",
                "--harness cursor --session SESSION",
            ),
            "install": (
                "floati wake daemon install - install the exact LaunchAgent",
                "Install deterministic digest-bound plist bytes without starting them.",
                "wake daemon install --root ~/fleet --as builder-a --harness cursor",
            ),
            "start": (
                "floati wake daemon start - start the exact LaunchAgent",
                "Bootstrap and kickstart only the deterministic user-domain LaunchAgent.",
                "wake daemon start --root ~/fleet --as builder-a --harness cursor",
            ),
            "status": (
                "floati wake daemon status - inspect one daemon coordinate",
                "Report inactive before consent and otherwise inspect only the exact installed supervisor coordinate.",
                "wake daemon status --root ~/fleet --as builder-a --harness cursor",
            ),
            "stop": (
                "floati wake daemon stop - stop the exact LaunchAgent",
                "Request bootout and prove process absence for one exact user-domain label.",
                "wake daemon stop --root ~/fleet --as builder-a --harness cursor",
            ),
            "remove": (
                "floati wake daemon remove - remove the exact LaunchAgent",
                "Quarantine and remove only matching deterministic plist bytes.",
                "wake daemon remove --root ~/fleet --as builder-a --harness cursor",
            ),
            "revoke": (
                "floati wake daemon revoke - revoke exact daemon consent",
                "Stop and remove matching supervisor bytes, then append exact consent revocation.",
                "wake daemon revoke --root ~/fleet --as builder-a --harness cursor",
            ),
        }
        for operation in (
            "consent", "bind", "install", "start", "status", "stop", "remove", "revoke"
        ):
            with self.subTest(operation=operation):
                args = ["wake", "daemon", operation, "--help"]
                stdout = io.StringIO()
                with redirect_stdout(stdout):
                    self.assertEqual(0, main(args))
                help_text = stdout.getvalue()
                self.assertIn("grok-build", help_text)
                for legacy_fragment in approved_legacy[operation]:
                    self.assertIn(legacy_fragment, help_text)
                self.assertNotIn(" daemon serve ", help_text)


if __name__ == "__main__":
    unittest.main()

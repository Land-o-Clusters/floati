"""WAKE-BIND-1: a nonzero adapter refusal carries a bounded stderr excerpt.

The bind that named ``wake_daemon_adapter_nonzero`` discarded the process's
only diagnostic stream. Issue #17 is why the bound is mandatory: Homebrew
``cursor-agent --version`` dumped 7,336,599 bytes. First and last 256 bytes
travel; the elided count is named; the whole stream never lands in a result.
"""

from __future__ import annotations

from floati import fixture_ids as public_ids

import hashlib
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
from floati.registry import Registry
from floati.root import FloatiRoot
from floati.wake_daemon_contract import AdapterBindingStore, DaemonCoordinate
from tests.temp_roots import REAL_TEMP_ROOT


# Contracted bound: each of head and tail. Named here so the RED can fail
# before the production constant exists.
EXCERPT_BYTES = 256


class _Runner:
    def __init__(
        self,
        *,
        returncode: int = 1,
        stdout: str = "",
        stderr: str = "stderr\n",
    ) -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.calls: list[tuple[tuple[str, ...], Path, int]] = []

    def __call__(
        self, argv: tuple[str, ...], cwd: Path, timeout: int
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append((argv, cwd, timeout))
        return subprocess.CompletedProcess(
            argv, self.returncode, self.stdout, self.stderr
        )


class WakeBind1Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.root = FloatiRoot.open_direct_home(self.base / "fleet-alpha", create=True)
        Registry(self.root).register(public_ids.builder("a"), "worker")
        self.workspace = self.base / "workspace"
        self.workspace.mkdir()
        self.target = self.base / "agent-target"
        self.target.write_bytes(b"#!/bin/sh\nexit 0\n")
        self.target.chmod(0o700)

    def binding(self, harness: str = "grok-build"):
        from floati.wake_daemon_adapters import adapter_contract_digest

        coordinate = DaemonCoordinate(self.root, public_ids.builder("a"), harness)
        return AdapterBindingStore(self.root).write(
            coordinate,
            session_id="session-1",
            workspace=self.workspace,
            executable=self.target,
            adapter_version="1",
            adapter_digest=adapter_contract_digest(harness),
            binding_epoch=1,
        )

    def refuse_nonzero(self, stderr: str, stdout: str = ""):
        from floati.wake_daemon_adapters import GrokBuildResumeWakeAdapter

        binding = self.binding()
        coordinate = DaemonCoordinate(self.root, public_ids.builder("a"), "grok-build")
        runner = _Runner(returncode=7, stdout=stdout, stderr=stderr)
        return GrokBuildResumeWakeAdapter(
            coordinate, runner=runner
        ).request_wake(binding, "wake", 30)

    def test_nonzero_carries_the_captured_stderr_excerpt(self) -> None:
        diagnostic = "cursor-agent: resume failed: session not found\n"
        refused = self.refuse_nonzero(diagnostic)

        self.assertEqual("refused", refused.outcome)
        self.assertEqual("wake_daemon_adapter_nonzero", refused.reason_code)
        excerpt = getattr(refused, "stderr_excerpt", None)
        self.assertIsInstance(excerpt, str)
        self.assertIn("session not found", excerpt)

    def test_stderr_over_the_bound_elides_the_middle_and_names_the_count(self) -> None:
        head = "H" * EXCERPT_BYTES
        tail = "T" * EXCERPT_BYTES
        middle = "M" * 97
        stderr = head + middle + tail
        refused = self.refuse_nonzero(stderr)
        excerpt = getattr(refused, "stderr_excerpt", None)

        self.assertIsInstance(excerpt, str)
        self.assertTrue(excerpt.startswith(head), excerpt[:40])
        self.assertTrue(excerpt.endswith(tail), excerpt[-40:])
        self.assertIn("97 bytes elided", excerpt)
        self.assertNotIn(middle, excerpt)
        self.assertNotEqual(stderr, excerpt)
        self.assertLess(len(excerpt.encode("utf-8")), len(stderr.encode("utf-8")))

    def test_stdout_digest_is_unchanged_on_nonzero(self) -> None:
        stdout = '{"sessionId":"session-1","stopReason":"end_turn"}\n'
        refused = self.refuse_nonzero("why\n", stdout=stdout)
        self.assertEqual(
            hashlib.sha256(stdout.encode("utf-8")).hexdigest(),
            refused.output_digest,
        )

    def test_default_runner_survives_non_utf8_adapter_stderr(self) -> None:
        """Production path must not UnicodeDecodeError before the excerpt."""

        from floati.wake_daemon_adapters import GrokBuildResumeWakeAdapter

        # The bytes are written by Python and the script only MOVES them: an
        # earlier version asked /bin/sh's printf for a hex escape, which bash
        # honours and dash does not, so on Linux the adapter emitted that
        # escape as LITERAL TEXT and the test failed for the fixture's reason
        # rather than the product's.
        payload = self.base / "noisy-adapter-stderr.bin"
        payload.write_bytes(b"head\xffmiddle\xfetail\n")
        executable = self.base / "noisy-adapter"
        executable.write_bytes(
            b"#!/bin/sh\n"
            b"cat " + str(payload).encode() + b" >&2\n"
            b"exit 7\n"
        )
        executable.chmod(0o700)
        from floati.wake_daemon_adapters import adapter_contract_digest

        coordinate = DaemonCoordinate(self.root, public_ids.builder("a"), "grok-build")
        binding = AdapterBindingStore(self.root).write(
            coordinate,
            session_id="session-1",
            workspace=self.workspace,
            executable=executable,
            adapter_version="1",
            adapter_digest=adapter_contract_digest("grok-build"),
            binding_epoch=1,
        )
        refused = GrokBuildResumeWakeAdapter(coordinate).request_wake(
            binding, "wake", 30
        )
        self.assertEqual("refused", refused.outcome)
        self.assertEqual("wake_daemon_adapter_nonzero", refused.reason_code)
        excerpt = getattr(refused, "stderr_excerpt", None)
        self.assertIsInstance(excerpt, str)
        self.assertIn("head", excerpt)
        self.assertIn("tail", excerpt)
        # If this fires, read the excerpt before reading the product: the
        # fixture must actually have put non-UTF-8 BYTES on stderr, and a
        # shell that renders them as text makes this test construct nothing.
        self.assertIn(
            "\ufffd",
            excerpt,
            "the fixture did not emit raw non-UTF-8 bytes; excerpt was "
            + repr(excerpt),
        )


class WakeBind1CliTests(unittest.TestCase):
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

    def bind_yes(self, executable: Path) -> tuple[int, dict]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        arguments = [
            "wake", "daemon", "bind",
            "--root", str(self.root_path),
            "--as", "builder-a",
            "--harness", "cursor",
            "--session", "cursor-session-1",
            "--workspace", str(self.workspace),
            "--executable", str(executable),
            "--binding-epoch", "1",
            "--yes",
        ]
        with mock.patch.dict(os.environ, {"HOME": str(self.home)}, clear=False):
            with redirect_stdout(stdout), redirect_stderr(stderr):
                status = main(arguments)
        payload = stdout.getvalue() or stderr.getvalue()
        return status, json.loads(payload)

    def test_bind_unresumable_refusal_includes_the_stderr_excerpt(self) -> None:
        executable = self.base / "cursor-agent"
        executable.write_bytes(
            b"#!/bin/sh\n"
            b"printf '%s' 'cursor-agent: resume failed: session not found' >&2\n"
            b"exit 7\n"
        )
        executable.chmod(0o700)

        status, artifact = self.bind_yes(executable)

        self.assertEqual(20, status)
        self.assertEqual("wake_bind_target_unresumable", artifact["evidence"]["code"])
        detail = artifact["evidence"]["detail"]
        self.assertIn("wake_daemon_adapter_nonzero", detail)
        self.assertIn("session not found", detail)

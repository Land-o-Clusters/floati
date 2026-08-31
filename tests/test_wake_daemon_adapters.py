from __future__ import annotations

from floati import fixture_ids as public_ids

import hashlib
import subprocess
import tempfile
import unittest
from pathlib import Path

from floati.errors import ProtocolRefusal
from floati.registry import Registry
from floati.root import FloatiRoot
from floati.wake_daemon_contract import AdapterBindingStore, DaemonCoordinate


class _Runner:
    def __init__(self, *, returncode: int = 0, stdout: str = "ok\n") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.calls: list[tuple[tuple[str, ...], Path, int]] = []
        self.raise_timeout = False
        self.raise_oserror = False

    def __call__(
        self, argv: tuple[str, ...], cwd: Path, timeout: int
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append((argv, cwd, timeout))
        if self.raise_timeout:
            raise subprocess.TimeoutExpired(argv, timeout)
        if self.raise_oserror:
            raise OSError("unavailable")
        return subprocess.CompletedProcess(argv, self.returncode, self.stdout, "stderr\n")


class WakeDaemonAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(dir="\x2fprivate/tmp")
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.root = FloatiRoot.open_direct_home(self.base / "fleet-alpha", create=True)
        Registry(self.root).register(public_ids.builder('a'), "worker")
        self.workspace = self.base / "workspace"
        self.workspace.mkdir()
        self.target = self.base / "agent-target"
        self.target.write_bytes(b"#!/bin/sh\nexit 0\n")
        self.target.chmod(0o700)
        self.link = self.base / "agent-link"
        self.link.symlink_to(self.target)

    def binding(self, harness: str, *, session_id: str = "session-1"):
        from floati.wake_daemon_adapters import adapter_contract_digest

        coordinate = DaemonCoordinate(self.root, public_ids.builder('a'), harness)
        return AdapterBindingStore(self.root).write(
            coordinate,
            session_id=session_id,
            workspace=self.workspace,
            executable=self.target,
            adapter_version="1",
            adapter_digest=adapter_contract_digest(harness),
            binding_epoch=1,
        )

    def test_codex_uses_only_the_fixed_queue_vector(self) -> None:
        from floati import wake_daemon_adapters as adapters

        binding = self.binding("codex")
        runner = _Runner(stdout="")
        coordinate = DaemonCoordinate(self.root, public_ids.builder('a'), "codex")
        prior = adapters.CODEX_EXECUTABLE
        adapters.CODEX_EXECUTABLE = self.link
        self.addCleanup(setattr, adapters, "CODEX_EXECUTABLE", prior)

        adapter = adapters.CodexQueueWakeAdapter(coordinate, runner=runner)
        result = adapter.request_wake(binding, "[floati] 1 new message: msg-1", 30)

        self.assertEqual("woke", result.outcome)
        self.assertEqual(
            (
                str(self.link),
                "queue",
                "--thread",
                "session-1",
                "--message",
                "[floati] 1 new message: msg-1",
            ),
            runner.calls[0][0],
        )
        self.assertEqual(self.workspace, runner.calls[0][1])
        self.assertEqual(30, runner.calls[0][2])

    def test_cursor_uses_only_the_bound_resume_vector(self) -> None:
        from floati.wake_daemon_adapters import CursorResumeWakeAdapter

        binding = self.binding("cursor")
        runner = _Runner(
            stdout=(
                '{"type":"result","subtype":"success","is_error":false,'
                '"session_id":"session-1","result":"ok"}\n'
            )
        )
        adapter = CursorResumeWakeAdapter(
            DaemonCoordinate(self.root, public_ids.builder('a'), "cursor"), runner=runner
        )

        result = adapter.request_wake(binding, "wake exact cursor session", 35)

        self.assertEqual("woke", result.outcome)
        self.assertEqual(
            (
                str(self.target),
                "--print",
                "--output-format",
                "json",
                "--single-turn",
                "--resume",
                "session-1",
                "wake exact cursor session",
            ),
            runner.calls[0][0],
        )
        self.assertEqual(self.workspace, runner.calls[0][1])

    def test_cursor_result_must_be_success_shaped_for_the_bound_session(self) -> None:
        from floati.wake_daemon_adapters import CursorResumeWakeAdapter

        binding = self.binding("cursor")
        coordinate = DaemonCoordinate(self.root, public_ids.builder('a'), "cursor")
        invalid_results = (
            '{"type":"result","subtype":"success","is_error":false,'
            '"session_id":"another-session","result":"ok"}\n',
            '{"type":"result","subtype":"error","is_error":true,'
            '"session_id":"session-1","result":"failed"}\n',
            '{"result":"ok"}\n',
        )

        for stdout in invalid_results:
            with self.subTest(stdout=stdout):
                result = CursorResumeWakeAdapter(
                    coordinate, runner=_Runner(stdout=stdout)
                ).request_wake(binding, "wake", 30)
                self.assertEqual("unknown", result.outcome)
                self.assertEqual("wake_daemon_cursor_result_invalid", result.reason_code)

    def test_grok_build_uses_only_bound_headless_resume_vector(self) -> None:
        from floati.wake_daemon_adapters import wake_adapter_for

        try:
            binding = self.binding("grok-build")
            stdout = (
                '{"text":"done","stopReason":"end_turn",'
                '"sessionId":"session-1","requestId":"request-1"}\n'
            )
            runner = _Runner(stdout=stdout)
            adapter = wake_adapter_for(
                self.root, public_ids.builder('a'), "grok-build", runner=runner
            )
        except ProtocolRefusal as exc:
            self.fail(f"grok-build adapter was refused: {exc}")

        result = adapter.request_wake(binding, "drain the declared root", 40)

        self.assertEqual("woke", result.outcome)
        self.assertIsNone(result.reason_code)
        self.assertEqual(
            hashlib.sha256(stdout.encode("utf-8")).hexdigest(),
            result.output_digest,
        )
        self.assertEqual(
            (
                str(self.target),
                "-p",
                "drain the declared root",
                "--output-format",
                "json",
                "--resume",
                "session-1",
            ),
            runner.calls[0][0],
        )
        self.assertEqual(self.workspace, runner.calls[0][1])
        self.assertEqual(40, runner.calls[0][2])

    def test_grok_build_result_requires_exact_session_and_end_turn(self) -> None:
        from floati.wake_daemon_adapters import GrokBuildResumeWakeAdapter

        binding = self.binding("grok-build")
        coordinate = DaemonCoordinate(self.root, public_ids.builder('a'), "grok-build")
        cases = (
            ("", "wake_daemon_grok_output_empty"),
            ("{\n", "wake_daemon_grok_output_invalid"),
            (
                '{"sessionId":"another-session","stopReason":"end_turn"}\n',
                "wake_daemon_grok_result_invalid",
            ),
            (
                '{"sessionId":"session-1","stopReason":"max_tokens"}\n',
                "wake_daemon_grok_result_invalid",
            ),
            (
                '[{"sessionId":"session-1","stopReason":"end_turn"}]\n',
                "wake_daemon_grok_result_invalid",
            ),
        )

        for stdout, reason_code in cases:
            with self.subTest(stdout=stdout):
                result = GrokBuildResumeWakeAdapter(
                    coordinate, runner=_Runner(stdout=stdout)
                ).request_wake(binding, "wake", 30)
                self.assertEqual("unknown", result.outcome)
                self.assertEqual(reason_code, result.reason_code)

    def test_grok_build_timeout_and_nonzero_never_claim_woke(self) -> None:
        from floati.wake_daemon_adapters import GrokBuildResumeWakeAdapter

        binding = self.binding("grok-build")
        coordinate = DaemonCoordinate(self.root, public_ids.builder('a'), "grok-build")
        timeout = _Runner()
        timeout.raise_timeout = True
        unavailable = _Runner()
        unavailable.raise_oserror = True
        nonzero = _Runner(
            returncode=7,
            stdout='{"sessionId":"session-1","stopReason":"end_turn"}\n',
        )

        timed_out = GrokBuildResumeWakeAdapter(
            coordinate, runner=timeout
        ).request_wake(binding, "wake", 30)
        missing = GrokBuildResumeWakeAdapter(
            coordinate, runner=unavailable
        ).request_wake(binding, "wake", 30)
        refused = GrokBuildResumeWakeAdapter(
            coordinate, runner=nonzero
        ).request_wake(binding, "wake", 30)

        self.assertEqual("unknown", timed_out.outcome)
        self.assertEqual("wake_daemon_adapter_timeout", timed_out.reason_code)
        self.assertEqual("unknown", missing.outcome)
        self.assertEqual("wake_daemon_adapter_unavailable", missing.reason_code)
        self.assertEqual("refused", refused.outcome)
        self.assertEqual("wake_daemon_adapter_nonzero", refused.reason_code)
        self.assertEqual(
            hashlib.sha256(nonzero.stdout.encode("utf-8")).hexdigest(),
            refused.output_digest,
        )

    def test_adapter_refuses_stale_session_or_executable_digest(self) -> None:
        from floati.wake_daemon_adapters import (
            CursorResumeWakeAdapter,
            adapter_contract_digest,
        )

        stale = self.binding("cursor", session_id="session-old")
        current = self.binding("cursor", session_id="session-new")
        coordinate = DaemonCoordinate(self.root, public_ids.builder('a'), "cursor")
        adapter = CursorResumeWakeAdapter(
            coordinate, runner=_Runner()
        )
        with self.assertRaisesRegex(ProtocolRefusal, "session"):
            adapter.request_wake(stale, "wake", 30)

        newer_epoch = AdapterBindingStore(self.root).write(
            coordinate,
            session_id="session-new",
            workspace=self.workspace,
            executable=self.target,
            adapter_version="1",
            adapter_digest=adapter_contract_digest("cursor"),
            binding_epoch=2,
        )
        self.assertNotEqual(current["binding_epoch"], newer_epoch["binding_epoch"])
        with self.assertRaisesRegex(ProtocolRefusal, "session"):
            adapter.request_wake(current, "wake", 30)

        AdapterBindingStore(self.root).write(
            coordinate,
            session_id="session-new",
            workspace=self.workspace,
            executable=self.target,
            adapter_version="1",
            adapter_digest="f" * 64,
            binding_epoch=3,
        )
        with self.assertRaisesRegex(ProtocolRefusal, "adapter"):
            adapter.exact_binding()

        current = self.binding("cursor", session_id="session-new")
        self.target.write_bytes(b"changed\n")
        with self.assertRaisesRegex(ProtocolRefusal, "digest"):
            adapter.request_wake(current, "wake", 30)

    def test_timeout_nonzero_and_empty_cursor_output_never_claim_woke(self) -> None:
        from floati.wake_daemon_adapters import CursorResumeWakeAdapter

        binding = self.binding("cursor")
        coordinate = DaemonCoordinate(self.root, public_ids.builder('a'), "cursor")
        timeout = _Runner()
        timeout.raise_timeout = True
        refused = _Runner(returncode=7)
        empty = _Runner(stdout="")

        self.assertEqual(
            "unknown",
            CursorResumeWakeAdapter(coordinate, runner=timeout)
            .request_wake(binding, "wake", 30)
            .outcome,
        )
        self.assertEqual(
            "refused",
            CursorResumeWakeAdapter(coordinate, runner=refused)
            .request_wake(binding, "wake", 30)
            .outcome,
        )
        self.assertEqual(
            "unknown",
            CursorResumeWakeAdapter(coordinate, runner=empty)
            .request_wake(binding, "wake", 30)
            .outcome,
        )

    def test_unsupported_harness_and_caller_argument_surface_are_absent(self) -> None:
        from floati.wake_daemon_adapters import (
            CursorResumeWakeAdapter,
            GrokBuildResumeWakeAdapter,
            wake_adapter_for,
        )

        with self.assertRaisesRegex(ProtocolRefusal, "unsupported"):
            wake_adapter_for(self.root, public_ids.builder('a'), "claude", runner=_Runner())

        adapter = CursorResumeWakeAdapter(
            DaemonCoordinate(self.root, public_ids.builder('a'), "cursor"), runner=_Runner()
        )
        self.assertFalse(hasattr(adapter, "argv"))
        with self.assertRaises(TypeError):
            adapter.request_wake(self.binding("cursor"), "wake", 30, argv=("sh",))

        grok = GrokBuildResumeWakeAdapter(
            DaemonCoordinate(self.root, public_ids.builder('a'), "grok-build"), runner=_Runner()
        )
        self.assertFalse(hasattr(grok, "argv"))
        with self.assertRaises(TypeError):
            grok.request_wake(
                self.binding("grok-build"), "wake", 30, argv=("sh",)
            )


if __name__ == "__main__":
    unittest.main()

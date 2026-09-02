from __future__ import annotations

from floati import fixture_ids as public_ids

import hashlib
import json
import subprocess
import tempfile
import unittest
from unittest import mock
from pathlib import Path

from floati.errors import ProtocolRefusal
from floati.registry import Registry
from floati.root import FloatiRoot
from floati.wake_daemon_contract import AdapterBindingStore, DaemonCoordinate
from tests.temp_roots import REAL_TEMP_ROOT


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
        self.temporary = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
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

    def test_zcode_uses_only_the_bound_resume_vector(self) -> None:
        """WD-R2: the zcode wake vector is the measured headless resume
        shape — K4's live receipt proved `--json --no-color … --prompt`
        parses and returns the typed artifact; am1 proved `--resume`
        parses. Success requires the artifact's sessionId to name the
        bound session (the only machine-checkable session fact zcode's
        artifact carries — it has no stopReason)."""
        from floati import wake_daemon_adapters as adapters

        binding = self.binding("zcode")
        stdout = (
            '{"sessionId":"session-1","traceId":"t-1","turnId":"turn-1",'
            '"response":"WD-R2 woke","usage":{"source":"provider"}}\n'
        )
        runner = _Runner(stdout=stdout)
        coordinate = DaemonCoordinate(self.root, "lane-a", "zcode")
        prior = adapters.ZCODE_ENTRY_SCRIPT
        adapters.ZCODE_ENTRY_SCRIPT = self.link
        self.addCleanup(setattr, adapters, "ZCODE_ENTRY_SCRIPT", prior)

        adapter = adapters.ZcodeResumeWakeAdapter(coordinate, runner=runner)
        result = adapter.request_wake(binding, "[floati] 1 new message: msg-1", 45)

        self.assertEqual("woke", result.outcome)
        self.assertIsNone(result.reason_code)
        self.assertEqual(
            hashlib.sha256(stdout.encode("utf-8")).hexdigest(),
            result.output_digest,
        )
        self.assertEqual(
            (
                "/opt/homebrew/bin/node",
                str(self.target),
                "--json",
                "--no-color",
                "--resume",
                "session-1",
                "--prompt",
                "[floati] 1 new message: msg-1",
            ),
            runner.calls[0][0],
        )
        self.assertEqual(self.workspace, runner.calls[0][1])
        self.assertEqual(45, runner.calls[0][2])

    def test_zcode_probe_resume_uses_the_measured_vector(self) -> None:
        from floati import wake_daemon_adapters as adapters
        from floati.wake_daemon_adapters import ZcodeResumeWakeAdapter

        stdout = '{"sessionId":"session-1","response":"probe woke"}\n'
        runner = _Runner(stdout=stdout)
        adapter = ZcodeResumeWakeAdapter(
            DaemonCoordinate(self.root, public_ids.builder("a"), "zcode"),
            runner=runner,
        )

        result = adapter.probe_resume(
            self.target,
            self.workspace,
            "session-1",
            "[floati] probe",
            45,
        )

        self.assertEqual("woke", result.outcome)
        self.assertEqual(
            (
                str(adapters.ZCODE_NODE.resolve(strict=True)),
                str(self.target),
                "--json",
                "--no-color",
                "--resume",
                "session-1",
                "--prompt",
                "[floati] probe",
            ),
            runner.calls[0][0],
        )

    def test_zcode_result_requires_exact_session_and_a_response(self) -> None:
        from floati import wake_daemon_adapters as adapters
        from floati.wake_daemon_adapters import ZcodeResumeWakeAdapter

        prior = adapters.ZCODE_ENTRY_SCRIPT
        adapters.ZCODE_ENTRY_SCRIPT = self.link
        self.addCleanup(setattr, adapters, "ZCODE_ENTRY_SCRIPT", prior)

        binding = self.binding("zcode")
        coordinate = DaemonCoordinate(self.root, "lane-a", "zcode")
        cases = (
            ("", "wake_daemon_zcode_output_empty"),
            ("{\n", "wake_daemon_zcode_output_invalid"),
            (
                '{"sessionId":"another-session","response":"ok"}\n',
                "wake_daemon_zcode_result_invalid",
            ),
            (
                '{"sessionId":"session-1"}\n',
                "wake_daemon_zcode_result_invalid",
            ),
            (
                '{"sessionId":"session-1","response":""}\n',
                "wake_daemon_zcode_result_invalid",
            ),
            (
                '[{"sessionId":"session-1","response":"ok"}]\n',
                "wake_daemon_zcode_result_invalid",
            ),
        )

        for stdout, reason_code in cases:
            with self.subTest(stdout=stdout):
                result = ZcodeResumeWakeAdapter(
                    coordinate, runner=_Runner(stdout=stdout)
                ).request_wake(binding, "wake", 30)
                self.assertEqual("unknown", result.outcome)
                self.assertEqual(reason_code, result.reason_code)

    def test_zcode_timeout_and_nonzero_never_claim_woke(self) -> None:
        from floati import wake_daemon_adapters as adapters
        from floati.wake_daemon_adapters import ZcodeResumeWakeAdapter

        prior = adapters.ZCODE_ENTRY_SCRIPT
        adapters.ZCODE_ENTRY_SCRIPT = self.link
        self.addCleanup(setattr, adapters, "ZCODE_ENTRY_SCRIPT", prior)

        binding = self.binding("zcode")
        coordinate = DaemonCoordinate(self.root, "lane-a", "zcode")
        timeout = _Runner()
        timeout.raise_timeout = True
        unavailable = _Runner()
        unavailable.raise_oserror = True
        nonzero = _Runner(
            returncode=7,
            stdout='{"sessionId":"session-1","response":"ok"}\n',
        )

        timed_out = ZcodeResumeWakeAdapter(
            coordinate, runner=timeout
        ).request_wake(binding, "wake", 30)
        missing = ZcodeResumeWakeAdapter(
            coordinate, runner=unavailable
        ).request_wake(binding, "wake", 30)
        refused = ZcodeResumeWakeAdapter(
            coordinate, runner=nonzero
        ).request_wake(binding, "wake", 30)

        self.assertEqual("unknown", timed_out.outcome)
        self.assertEqual("wake_daemon_adapter_timeout", timed_out.reason_code)
        self.assertEqual("unknown", missing.outcome)
        self.assertEqual("wake_daemon_adapter_unavailable", missing.reason_code)
        self.assertEqual("refused", refused.outcome)
        self.assertEqual("wake_daemon_adapter_nonzero", refused.reason_code)

    def test_zcode_wake_vector_is_derivable_and_entry_is_digest_bound(self) -> None:
        from floati import wake_daemon_adapters as adapters

        # The adapter is reachable through the factory, and the contract
        # digest pins the argv shape the adapter must build.
        binding = self.binding("zcode")
        prior = adapters.ZCODE_ENTRY_SCRIPT
        adapters.ZCODE_ENTRY_SCRIPT = self.link
        self.addCleanup(setattr, adapters, "ZCODE_ENTRY_SCRIPT", prior)
        adapter = adapters.wake_adapter_for(
            self.root, "lane-a", "zcode", runner=_Runner()
        )
        self.assertIsInstance(adapter, adapters.ZcodeResumeWakeAdapter)

        # A binding naming a DIFFERENT entry script is refused: the wake
        # vector runs one exact measured interpreter + entry pair.
        other = self.base / "other-entry.cjs"
        other.write_bytes(b"// not the pinned entry\n")
        other.chmod(0o700)
        mismatched = AdapterBindingStore(self.root).write(
            DaemonCoordinate(self.root, "lane-a", "zcode"),
            session_id="session-1",
            workspace=self.workspace,
            executable=other,
            adapter_version="1",
            adapter_digest=adapters.adapter_contract_digest("zcode"),
            binding_epoch=2,
        )
        with self.assertRaisesRegex(ProtocolRefusal, "entry"):
            adapter.request_wake(mismatched, "wake", 30)

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

    def test_zcode_uses_only_the_bound_resume_vector(self) -> None:
        from floati import wake_daemon_adapters as adapters

        binding = self.binding("zcode")
        stdout = (
            '{"sessionId":"session-1","traceId":"t-1","turnId":"turn-1",'
            '"response":"WD-R2 woke","usage":{"source":"provider"}}\n'
        )
        runner = _Runner(stdout=stdout)
        coordinate = DaemonCoordinate(self.root, public_ids.builder('a'), "zcode")
        prior = adapters.ZCODE_ENTRY_SCRIPT
        adapters.ZCODE_ENTRY_SCRIPT = self.link
        self.addCleanup(setattr, adapters, "ZCODE_ENTRY_SCRIPT", prior)

        adapter = adapters.ZcodeResumeWakeAdapter(coordinate, runner=runner)
        result = adapter.request_wake(binding, "[floati] 1 new message: msg-1", 45)

        self.assertEqual("woke", result.outcome)
        self.assertIsNone(result.reason_code)
        self.assertEqual(
            hashlib.sha256(stdout.encode("utf-8")).hexdigest(),
            result.output_digest,
        )
        self.assertEqual(
            (
                str(adapters.ZCODE_NODE.resolve(strict=True)),
                str(self.target),
                "--json",
                "--no-color",
                "--resume",
                "session-1",
                "--prompt",
                "[floati] 1 new message: msg-1",
            ),
            runner.calls[0][0],
        )
        self.assertEqual(self.workspace, runner.calls[0][1])
        self.assertEqual(45, runner.calls[0][2])

    def test_zcode_result_requires_exact_session_and_a_response(self) -> None:
        from floati import wake_daemon_adapters as adapters
        from floati.wake_daemon_adapters import ZcodeResumeWakeAdapter

        prior = adapters.ZCODE_ENTRY_SCRIPT
        adapters.ZCODE_ENTRY_SCRIPT = self.link
        self.addCleanup(setattr, adapters, "ZCODE_ENTRY_SCRIPT", prior)

        binding = self.binding("zcode")
        coordinate = DaemonCoordinate(self.root, public_ids.builder('a'), "zcode")
        cases = (
            ("", "wake_daemon_zcode_output_empty"),
            ("{\n", "wake_daemon_zcode_output_invalid"),
            (
                '{"sessionId":"another-session","response":"ok"}\n',
                "wake_daemon_zcode_result_invalid",
            ),
            (
                '{"sessionId":"session-1"}\n',
                "wake_daemon_zcode_result_invalid",
            ),
            (
                '{"sessionId":"session-1","response":""}\n',
                "wake_daemon_zcode_result_invalid",
            ),
            (
                '[{"sessionId":"session-1","response":"ok"}]\n',
                "wake_daemon_zcode_result_invalid",
            ),
        )

        for stdout, reason_code in cases:
            with self.subTest(stdout=stdout):
                result = ZcodeResumeWakeAdapter(
                    coordinate, runner=_Runner(stdout=stdout)
                ).request_wake(binding, "wake", 30)
                self.assertEqual("unknown", result.outcome)
                self.assertEqual(reason_code, result.reason_code)

    def test_zcode_timeout_and_nonzero_never_claim_woke(self) -> None:
        from floati import wake_daemon_adapters as adapters
        from floati.wake_daemon_adapters import ZcodeResumeWakeAdapter

        prior = adapters.ZCODE_ENTRY_SCRIPT
        adapters.ZCODE_ENTRY_SCRIPT = self.link
        self.addCleanup(setattr, adapters, "ZCODE_ENTRY_SCRIPT", prior)

        binding = self.binding("zcode")
        coordinate = DaemonCoordinate(self.root, public_ids.builder('a'), "zcode")
        timeout = _Runner()
        timeout.raise_timeout = True
        unavailable = _Runner()
        unavailable.raise_oserror = True
        nonzero = _Runner(
            returncode=7,
            stdout='{"sessionId":"session-1","response":"ok"}\n',
        )

        timed_out = ZcodeResumeWakeAdapter(
            coordinate, runner=timeout
        ).request_wake(binding, "wake", 30)
        missing = ZcodeResumeWakeAdapter(
            coordinate, runner=unavailable
        ).request_wake(binding, "wake", 30)
        refused = ZcodeResumeWakeAdapter(
            coordinate, runner=nonzero
        ).request_wake(binding, "wake", 30)

        self.assertEqual("unknown", timed_out.outcome)
        self.assertEqual("wake_daemon_adapter_timeout", timed_out.reason_code)
        self.assertEqual("unknown", missing.outcome)
        self.assertEqual("wake_daemon_adapter_unavailable", missing.reason_code)
        self.assertEqual("refused", refused.outcome)
        self.assertEqual("wake_daemon_adapter_nonzero", refused.reason_code)

    def test_zcode_wake_vector_is_derivable_and_entry_is_digest_bound(self) -> None:
        from floati import wake_daemon_adapters as adapters

        prior = adapters.ZCODE_ENTRY_SCRIPT
        adapters.ZCODE_ENTRY_SCRIPT = self.link
        self.addCleanup(setattr, adapters, "ZCODE_ENTRY_SCRIPT", prior)
        adapter = adapters.wake_adapter_for(
            self.root, public_ids.builder('a'), "zcode", runner=_Runner()
        )
        self.assertIsInstance(adapter, adapters.ZcodeResumeWakeAdapter)

        other = self.base / "other-entry.cjs"
        other.write_bytes(b"// not the pinned entry\n")
        other.chmod(0o700)
        mismatched = AdapterBindingStore(self.root).write(
            DaemonCoordinate(self.root, public_ids.builder('a'), "zcode"),
            session_id="session-1",
            workspace=self.workspace,
            executable=other,
            adapter_version="1",
            adapter_digest=adapters.adapter_contract_digest("zcode"),
            binding_epoch=2,
        )
        with self.assertRaisesRegex(ProtocolRefusal, "entry"):
            adapter.request_wake(mismatched, "wake", 30)


def _emitted_wake_adapter_reasons() -> set[str]:
    """Derive refused reasons from WakeAdapterResult literals in the adapter module."""
    import ast

    source = Path(__file__).parents[1] / "floati" / "wake_daemon_adapters.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    emitted: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
        if name != "WakeAdapterResult" or len(node.args) < 2:
            continue
        reason = node.args[1]
        if isinstance(reason, ast.Constant) and isinstance(reason.value, str):
            emitted.add(reason.value)
    return emitted


class WakeAdapterEmittedReasonPinTests(unittest.TestCase):
    def test_refused_reason_set_covers_adapter_emissions_and_matches_schema(self) -> None:
        """Row 20: the ledger set is derived from adapter emissions, not hand-copied."""
        import json

        from floati.records import WAKE_ATTEMPT_REFUSED_REASONS

        emitted = _emitted_wake_adapter_reasons()
        self.assertTrue(
            {code for code in emitted if "zcode" in code},
            "derived pin must see zcode emissions; walking only the cursor adapter stays green through erasure",
        )
        self.assertTrue(
            emitted <= set(WAKE_ATTEMPT_REFUSED_REASONS),
            emitted - set(WAKE_ATTEMPT_REFUSED_REASONS),
        )
        schema = json.loads(
            (Path(__file__).parents[1] / "schemas/v1/wake-attempt-record.schema.json").read_text(
                encoding="utf-8"
            )
        )
        enum = set(schema["properties"]["reason_code"]["enum"])
        self.assertEqual(enum, {None} | set(WAKE_ATTEMPT_REFUSED_REASONS))


class ResumeProbeTests(unittest.TestCase):
    """WD-R5b: the probe runs the adapter's OWN resume shape without a
    persisted binding - one bounded resume, judged by the adapter's own
    result validation. It exists only for costs_one_turn adapters."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.root = FloatiRoot.open_direct_home(self.base / "fleet-alpha", create=True)
        Registry(self.root).register(public_ids.builder("a"), "worker")
        self.workspace = self.base / "workspace"
        self.workspace.mkdir()
        self.executable = self.base / "cursor-proof"
        self.executable.write_bytes(b"#!/bin/sh\nexit 0\n")
        self.executable.chmod(0o700)

    def test_probe_resume_judges_by_the_adapters_own_result_shape(self) -> None:
        from floati.wake_daemon_adapters import PROBE_REASON, CursorResumeWakeAdapter

        stdout = json.dumps({
            "type": "result", "subtype": "success",
            "is_error": False, "session_id": "cursor-session-1",
        })
        runner = _Runner(stdout=stdout)
        coordinate = DaemonCoordinate(self.root, public_ids.builder("a"), "cursor")
        adapter = CursorResumeWakeAdapter(coordinate, runner=runner)

        result = adapter.probe_resume(
            self.executable, self.workspace, "cursor-session-1", PROBE_REASON, 300,
        )

        self.assertEqual("woke", result.outcome)
        self.assertEqual(1, len(runner.calls))
        argv, cwd, deadline = runner.calls[0]
        self.assertEqual(str(self.executable), argv[0])
        self.assertEqual("--resume", argv[5])
        self.assertEqual("cursor-session-1", argv[6])
        self.assertEqual(PROBE_REASON, argv[7])
        self.assertEqual(self.workspace, cwd)
        self.assertEqual(300, deadline)

    def test_probe_resume_rejects_a_result_from_another_session(self) -> None:
        from floati.wake_daemon_adapters import PROBE_REASON, CursorResumeWakeAdapter

        stdout = json.dumps({
            "type": "result", "subtype": "success",
            "is_error": False, "session_id": "someone-else",
        })
        runner = _Runner(stdout=stdout)
        coordinate = DaemonCoordinate(self.root, public_ids.builder("a"), "cursor")
        adapter = CursorResumeWakeAdapter(coordinate, runner=runner)

        result = adapter.probe_resume(
            self.executable, self.workspace, "cursor-session-1", PROBE_REASON, 300,
        )

        self.assertEqual("unknown", result.outcome)
        self.assertEqual("wake_daemon_cursor_result_invalid", result.reason_code)

    def test_probe_resume_fails_closed_where_no_resume_shape_exists(self) -> None:
        from floati.wake_daemon_adapters import CodexQueueWakeAdapter

        coordinate = DaemonCoordinate(self.root, public_ids.builder("a"), "codex")
        adapter = CodexQueueWakeAdapter(coordinate, runner=_Runner())

        with self.assertRaises(ProtocolRefusal) as ctx:
            adapter.probe_resume(
                self.executable, self.workspace, "session-1", "probe", 300,
            )
        self.assertEqual("wake_daemon_probe_unavailable", ctx.exception.code)


class ResumeProbeDeclarationTests(unittest.TestCase):
    """WD-R5a: the adapter DECLARES its probe class; absent is UNDECLARED and refuses.

    Am.1 (5fc3f7d) re-cut WD-R5 after the lane measured that no turn-free
    resume probe exists for any adapter. The declaration is where the next
    adapter keeps the guard honest instead of assuming the primitive.
    """

    def test_every_declared_wake_adapter_declares_costs_one_turn(self) -> None:
        from floati.wake_daemon_adapters import _ADAPTER_VERSIONS, resume_probe_class

        for harness in _ADAPTER_VERSIONS:
            self.assertEqual("costs_one_turn", resume_probe_class(harness))

    def test_resume_probe_declaration_refuses_when_absent(self) -> None:
        from floati import wake_daemon_adapters as module

        with mock.patch.object(module, "_ADAPTER_RESUME_PROBES", {"codex": "costs_one_turn"}):
            with self.assertRaises(ProtocolRefusal) as ctx:
                module.resume_probe_class("cursor")
        self.assertEqual("wake_daemon_resume_probe_undeclared", ctx.exception.code)


if __name__ == "__main__":
    unittest.main()

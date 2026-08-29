from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import List, Tuple
from unittest import mock

from floati.errors import ProtocolRefusal


HARNESS = (
    Path(__file__).parent
    / "fixtures"
    / "codex-thread-observer"
    / "reference_harness.py"
).resolve()
THREAD_ID = "018f3a2b-4c5d-7e8f-9a0b-1c2d3e4f5678"


class ThreadSourceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)

    def command(self, mode: str) -> Tuple[List[str], Path, Path, Path]:
        methods = self.base / (mode + "-methods")
        params = self.base / (mode + "-params.json")
        diagnostic = self.base / (mode + "-diagnostic.json")
        return (
            [sys.executable, str(HARNESS), mode, str(methods), str(params), str(diagnostic)],
            methods,
            params,
            diagnostic,
        )

    def source(self, mode: str):
        from floati.thread_source import CodexLocalThreadSource

        command, methods, params, diagnostic = self.command(mode)
        return CodexLocalThreadSource._for_test(command), methods, params, diagnostic

    def read_from_harness(self, mode: str, *, deadline: float = 1.0):
        source, methods, params, diagnostic = self.source(mode)
        result = source.read(THREAD_ID, deadline_seconds=deadline)
        method_rows = methods.read_text(encoding="utf-8").splitlines()
        parameter_row = (
            json.loads(params.read_text(encoding="utf-8")) if params.exists() else None
        )
        return result, method_rows, parameter_row, diagnostic

    def test_reference_harness_standalone_lawful_round_trip(self) -> None:
        command, methods, _, _ = self.command("idle")
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.addCleanup(lambda: process.poll() is None and process.kill())
        assert process.stdin is not None and process.stdout is not None
        for message in (
            {"id": 1, "method": "initialize", "params": {"clientInfo": {"name": "control", "version": "0"}}},
            {"method": "initialized"},
            {"id": 2, "method": "thread/read", "params": {"threadId": THREAD_ID, "includeTurns": False}},
        ):
            process.stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
            process.stdin.flush()
            if "id" in message:
                response = json.loads(process.stdout.readline())
        self.assertEqual("idle", response["result"]["thread"]["status"]["type"])
        process.stdin.close()
        process.wait(timeout=1)
        process.stdout.close()
        process.stderr.close()
        self.assertEqual(["initialize", "initialized", "thread/read"], methods.read_text().splitlines())

    def test_source_sends_only_initialize_initialized_and_exact_thread_read(self) -> None:
        result, methods, params, _ = self.read_from_harness("idle")
        self.assertEqual("idle", result.provider_status)
        self.assertEqual(["initialize", "initialized", "thread/read"], methods)
        self.assertEqual({"threadId": THREAD_ID, "includeTurns": False}, params)

    def test_source_announces_floati_client_identity(self) -> None:
        """Catches a shipped thread-provider initialize payload using the retired name."""
        result, _, _, diagnostic_path = self.read_from_harness("idle")
        self.assertEqual("observed", result.observation_outcome)
        diagnostic = json.loads(diagnostic_path.read_text(encoding="utf-8"))

        self.assertEqual(
            {"name": "floati-thread-observer", "version": "0"},
            diagnostic["initialize"]["params"]["clientInfo"],
        )

    def test_source_normalizes_only_closed_status_and_attention_inputs(self) -> None:
        expected = {
            "idle": ("idle", ()),
            "not-loaded": ("not_loaded", ()),
            "system-error": ("system_error", ()),
            "active-approval": ("active", ("waiting_on_approval",)),
            "active-input": ("active", ("waiting_on_user_input",)),
            "active-both": ("active", ("waiting_on_approval", "waiting_on_user_input")),
        }
        for mode, values in expected.items():
            with self.subTest(mode=mode):
                result, _, _, _ = self.read_from_harness(mode)
                self.assertEqual(values, (result.provider_status, result.active_flags))
                self.assertEqual(1786622400, result.provider_updated_at)
                self.assertEqual(("observed", "exact_thread_read"), (result.observation_outcome, result.observation_reason))

    def test_source_never_returns_thread_content_fields(self) -> None:
        result, _, _, _ = self.read_from_harness("idle")
        encoded = repr(result)
        for secret in (
            "HOSTILE_TITLE",
            "HOSTILE_PREVIEW",
            "HOSTILE_TURN",
            "/private/hostile",
            "session-secret",
            "model-secret",
        ):
            self.assertNotIn(secret, encoded)

    def test_source_ignores_only_one_closed_recognized_notification(self) -> None:
        result, methods, _, _ = self.read_from_harness("notification")
        self.assertEqual("observed", result.observation_outcome)
        self.assertEqual(["initialize", "initialized", "thread/read"], methods)

    def test_source_maps_missing_and_protocol_failures_to_bounded_unknown(self) -> None:
        expected = {
            "missing": "thread_missing",
            "malformed": "protocol_invalid",
            "oversized": "protocol_invalid",
            "partial": "protocol_invalid",
            "duplicate": "protocol_invalid",
            "trailing": "protocol_invalid",
            "nonempty-turns": "protocol_invalid",
            "wrong-thread": "protocol_invalid",
            "server-request": "protocol_invalid",
            "malformed-method": "protocol_invalid",
            "unknown-notification": "protocol_invalid",
            "wrong-response-id": "protocol_invalid",
            "response-extra": "protocol_invalid",
            "duplicate-root-key": "protocol_invalid",
            "updated-float": "protocol_invalid",
            "extra-status": "protocol_invalid",
            "extra-flag": "protocol_invalid",
            "float-response-id": "protocol_invalid",
            "null-error": "protocol_invalid",
            "crash": "provider_unavailable",
            "hang": "provider_timeout",
        }
        for mode, reason in expected.items():
            with self.subTest(mode=mode):
                lawful, _, _, _ = self.read_from_harness("idle")
                self.assertEqual("observed", lawful.observation_outcome)
                result, _, _, _ = self.read_from_harness(mode, deadline=0.25)
                self.assertEqual(("unknown", reason), (result.observation_outcome, result.observation_reason))
                self.assertEqual(("unknown", None, None), (result.provider_status, result.active_flags, result.provider_updated_at))

    def test_source_accepts_standard_optional_error_data_without_exposing_it(self) -> None:
        lawful, _, _, _ = self.read_from_harness("idle")
        self.assertEqual("observed", lawful.observation_outcome)
        for mode, reason in (
            ("provider-error-data", "provider_unavailable"),
            ("missing-data", "thread_missing"),
        ):
            with self.subTest(mode=mode):
                result, _, _, _ = self.read_from_harness(mode)
                self.assertEqual(
                    ("unknown", reason),
                    (result.observation_outcome, result.observation_reason),
                )
                self.assertNotIn("HOSTILE_ERROR_DATA", repr(result))

    def test_source_normalizes_flag_order_without_inventing_state(self) -> None:
        result, _, _, _ = self.read_from_harness("active-reversed")
        self.assertEqual(
            ("active", ("waiting_on_approval", "waiting_on_user_input")),
            (result.provider_status, result.active_flags),
        )

    def test_source_uses_minimal_environment_cwd_and_descriptor_set(self) -> None:
        hostile_fd = os.open(self.base / "hostile-fd", os.O_CREAT | os.O_RDWR, 0o600)
        self.addCleanup(os.close, hostile_fd)
        os.set_inheritable(hostile_fd, True)
        cwd_before = Path.cwd()
        environment_before = dict(os.environ)
        with mock.patch.dict(os.environ, {"THREAD_SOURCE_SECRET": "hostile-secret"}):
            _, _, _, diagnostic_path = self.read_from_harness("idle")
        diagnostic = json.loads(diagnostic_path.read_text(encoding="utf-8"))
        self.assertEqual("/", diagnostic["cwd"])
        expected_environment = {"HOME", "PATH", "LANG", "LC_ALL"} | (
            {"CODEX_HOME"} if "CODEX_HOME" in os.environ else set()
        )
        child_environment = set(diagnostic["environment"])
        self.assertTrue(expected_environment.issubset(child_environment))
        self.assertLessEqual(
            child_environment - expected_environment,
            {"__CF_USER_TEXT_ENCODING"},
            "only the macOS runtime-injected text encoding may appear",
        )
        self.assertNotIn(str(self.base / "hostile-fd"), diagnostic["fds"].values())
        self.assertNotIn("THREAD_SOURCE_SECRET", diagnostic["environment"])
        self.assertEqual(cwd_before, Path.cwd())
        self.assertEqual(environment_before, dict(os.environ))

    def test_source_refuses_invalid_inputs_before_launch(self) -> None:
        source, methods, _, _ = self.source("idle")
        for thread_id, deadline in (
            ("not-a-thread", 1.0),
            (THREAD_ID.replace("-", ""), 1.0),
            (THREAD_ID, 0),
            (THREAD_ID, 61.0),
            (THREAD_ID, float("inf")),
            (THREAD_ID, True),
        ):
            with self.subTest(thread_id=thread_id, deadline=deadline):
                with self.assertRaises(ProtocolRefusal):
                    source.read(thread_id, deadline_seconds=deadline)
        self.assertFalse(methods.exists())

    def test_extreme_finite_deadline_refuses_without_leaking_child(self) -> None:
        import floati.thread_source as thread_source

        source, methods, _, _ = self.source("idle")
        actual_popen = thread_source.subprocess.Popen
        processes = []

        def capture_process(*args, **kwargs):
            process = actual_popen(*args, **kwargs)
            processes.append(process)
            return process

        try:
            with mock.patch("floati.thread_source.subprocess.Popen", side_effect=capture_process):
                with self.assertRaises(ProtocolRefusal):
                    source.read(THREAD_ID, deadline_seconds=1e308)
        finally:
            for process in processes:
                if process.poll() is None:
                    thread_source._cleanup(process)
        self.assertEqual([], processes)
        self.assertFalse(methods.exists())

    def test_source_finally_cleans_child_after_unexpected_reader_failure(self) -> None:
        import floati.thread_source as thread_source

        source, _, _, _ = self.source("idle")
        actual_cleanup = thread_source._cleanup
        cleaned = []
        processes = []
        actual_popen = thread_source.subprocess.Popen

        def capture_process(*args, **kwargs):
            process = actual_popen(*args, **kwargs)
            processes.append(process)
            return process

        def record_cleanup(process):
            cleaned.append(process)
            return actual_cleanup(process)

        try:
            with mock.patch(
                "floati.thread_source._response",
                side_effect=OverflowError("unexpected reader failure"),
            ), mock.patch(
                "floati.thread_source._cleanup", side_effect=record_cleanup
            ), mock.patch(
                "floati.thread_source.subprocess.Popen", side_effect=capture_process
            ):
                with self.assertRaises(OverflowError):
                    source.read(THREAD_ID, deadline_seconds=1)
        finally:
            for process in processes:
                if process.poll() is None:
                    actual_cleanup(process)
        self.assertEqual(1, len(cleaned))
        self.assertIsNotNone(cleaned[0])
        self.assertIsNotNone(cleaned[0].poll())

    def test_source_has_one_fixed_production_command_and_rejects_symlinked_home(self) -> None:
        from floati.thread_source import CodexLocalThreadSource

        self.assertEqual(
            ("/opt/homebrew/bin/codex", "app-server", "--stdio"),
            tuple(CodexLocalThreadSource()._command),
        )
        real_home = self.base / "real-home"
        real_home.mkdir()
        real_home = real_home.resolve()
        lawful, lawful_methods, _, _ = self.source("idle")
        with mock.patch.dict(os.environ, {"HOME": str(real_home)}, clear=True):
            lawful_result = lawful.read(THREAD_ID, deadline_seconds=1)
        self.assertEqual(
            "observed", lawful_result.observation_outcome, lawful_result,
        )
        self.assertTrue(lawful_methods.exists())
        lawful_methods.unlink()

        linked_home = real_home.parent / "linked-home"
        linked_home.symlink_to(real_home, target_is_directory=True)
        source, methods, _, _ = self.source("idle")
        with mock.patch.dict(os.environ, {"HOME": str(linked_home)}, clear=True):
            result = source.read(THREAD_ID, deadline_seconds=1)
        self.assertEqual(("unknown", "provider_unavailable"), (result.observation_outcome, result.observation_reason))
        self.assertFalse(methods.exists())

        loop_home = real_home.parent / "loop-home"
        loop_home.symlink_to(loop_home)
        loop_source, loop_methods, _, _ = self.source("idle")
        with mock.patch.dict(os.environ, {"HOME": str(loop_home)}, clear=True):
            loop_result = loop_source.read(THREAD_ID, deadline_seconds=1)
        self.assertEqual(
            ("unknown", "provider_unavailable"),
            (loop_result.observation_outcome, loop_result.observation_reason),
        )
        self.assertFalse(loop_methods.exists())

    def test_cleanup_failure_overrides_observed_values(self) -> None:
        import floati.thread_source as thread_source

        source, _, _, _ = self.source("idle")
        actual_cleanup = thread_source._cleanup
        with mock.patch(
            "floati.thread_source._cleanup",
            side_effect=lambda process: (actual_cleanup(process), False)[1],
        ):
            result = source.read(THREAD_ID, deadline_seconds=1)
        self.assertEqual(
            ("unknown", None, None, "unknown", "cleanup_failed"),
            (
                result.provider_status,
                result.active_flags,
                result.provider_updated_at,
                result.observation_outcome,
                result.observation_reason,
            ),
        )

    def test_cleanup_reports_pipe_close_failures(self) -> None:
        import floati.thread_source as thread_source

        process = mock.Mock()
        process.pid = 987654321
        process.stdin.close.side_effect = OSError("stdin close failed")
        process.stdout.close.side_effect = OSError("stdout close failed")
        process.wait.return_value = 0
        with mock.patch(
            "floati.thread_source.os.killpg", side_effect=ProcessLookupError
        ), mock.patch("floati.thread_source._group_exists", return_value=False):
            self.assertFalse(thread_source._cleanup(process))

    def test_source_reaps_sigterm_ignoring_descendant(self) -> None:
        result, _, _, diagnostic = self.read_from_harness("ignore-term-child")
        self.assertEqual("observed", result.observation_outcome)
        descendant = int(Path(str(diagnostic) + ".descendant").read_text(encoding="ascii"))
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            try:
                os.kill(descendant, 0)
            except ProcessLookupError:
                break
            time.sleep(0.01)
        else:
            self.fail("SIGTERM-ignoring harness descendant survived source cleanup")

    def test_production_source_has_no_forbidden_method_or_content_surface(self) -> None:
        source = Path("floati/thread_source.py").read_text(encoding="utf-8")
        self.assertEqual(1, source.count('"thread/read"'))
        for forbidden in (
            "thread/list",
            "turn/start",
            "turn/steer",
            "turn/interrupt",
            "thread/archive",
            "thread/resume",
            "thread/fork",
            "thread/name",
            "send_message",
            "preview",
            "prompt",
        ):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()

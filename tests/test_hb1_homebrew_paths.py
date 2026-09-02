from __future__ import annotations

import argparse
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from floati import fixture_ids as public_ids
from floati.errors import ProtocolRefusal
from floati.registry import Registry
from floati.root import FloatiRoot
from floati.wake_daemon_contract import AdapterBindingStore, DaemonCoordinate
from tests.temp_roots import REAL_TEMP_ROOT


THREAD_ID = "018f3a2b-4c5d-7e8f-9a0b-1c2d3e4f5678"


class HB1HomebrewPathTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)

    @staticmethod
    def worker_args(root: Path, **overrides: object) -> argparse.Namespace:
        values: dict[str, object] = {
            "root": str(root),
            "actor": public_ids.builder("a"),
            "adapter": "claude",
            "claude_executable": None,
            "codex_executable": None,
            "pi_executable": None,
        }
        values.update(overrides)
        return argparse.Namespace(**values)

    def test_worker_linux_undeclared_is_a_host_condition_before_root_creation(self) -> None:
        """Catches worker setup touching durable state before naming the missing flag."""
        import floati.cli as cli

        root = self.base / "never-created-fleet"
        with mock.patch.object(cli.sys, "platform", "linux"):
            try:
                status, evidence, exit_code = cli._worker_run(self.worker_args(root))
            except ProtocolRefusal as exc:
                self.fail(f"worker reached the fleet root before host classification: {exc.code}")

        self.assertEqual(("degraded", 35), (status, exit_code))
        self.assertEqual("host_condition", evidence["status"])
        self.assertEqual(
            "fcd20_claude_executable_undeclared",
            evidence["evidence"]["code"],
        )
        self.assertIn("--claude-executable", evidence["evidence"]["remedy"])
        self.assertFalse(root.exists())

    def test_worker_selected_declaration_reaches_the_real_adapter_command(self) -> None:
        """Catches worker run accepting a flag while still constructing the old default."""
        import floati.cli as cli

        root_path = self.base / "fleet"
        FloatiRoot.open_direct_home(root_path, create=True)
        executable = str(Path(sys.executable).resolve())
        captured: dict[str, object] = {}

        class CapturingRunner:
            def __init__(self, root: FloatiRoot, adapters: dict[str, object]) -> None:
                captured["root"] = root
                captured["adapters"] = adapters

            def run(self, actor: str, adapter: str) -> dict[str, object]:
                captured["actor"] = actor
                captured["adapter"] = adapter
                return {"transition": "degrade", "reason": "no_work"}

        args = self.worker_args(root_path, claude_executable=executable)
        with mock.patch.object(cli.sys, "platform", "linux"), mock.patch.object(
            cli, "WorkerRunner", CapturingRunner
        ):
            cli._worker_run(args)

        adapters = captured["adapters"]
        self.assertEqual({"claude"}, set(adapters))  # type: ignore[arg-type]
        self.assertEqual(
            (executable,),
            tuple(adapters["claude"].command),  # type: ignore[index,union-attr]
        )

    def test_thread_source_absent_declaration_is_not_provider_unavailable(self) -> None:
        """Catches a missing operator sentence being mislabeled as provider health."""
        from floati.thread_source import CodexLocalThreadSource

        try:
            source = CodexLocalThreadSource(None)
        except TypeError as exc:
            self.fail(f"thread source has no operator declaration seam: {exc}")
        result = source.read(THREAD_ID)
        self.assertEqual(
            ("unknown", "codex_executable_absent"),
            (result.observation_outcome, result.observation_reason),
        )

    def test_thread_source_uses_one_explicit_canonical_executable(self) -> None:
        """Catches the observer ignoring its declaration and retaining the fixed prefix."""
        from floati.thread_source import CodexLocalThreadSource

        executable = str(Path(sys.executable).resolve())
        try:
            source = CodexLocalThreadSource(executable)
        except TypeError as exc:
            self.fail(f"thread source has no operator declaration seam: {exc}")
        self.assertEqual(
            (executable, "app-server", "--stdio"),
            tuple(source._command),
        )

    def test_zcode_interpreter_is_strictly_resolved_before_argv(self) -> None:
        """Catches the unchecked interpreter reaching subprocess argv."""
        from floati import wake_daemon_adapters as adapters

        missing = self.base / "missing-node"
        prior = adapters.ZCODE_NODE
        adapters.ZCODE_NODE = missing
        self.addCleanup(setattr, adapters, "ZCODE_NODE", prior)

        with self.assertRaises(ProtocolRefusal) as raised:
            adapters.ZcodeResumeWakeAdapter.resume_argv(
                self.base / "entry.cjs", "session-1", "wake"
            )
        self.assertEqual("wake_daemon_zcode_node_absent", raised.exception.code)
        self.assertIn(str(missing), raised.exception.detail)

    def test_codex_wake_pin_absence_names_path_and_remedy(self) -> None:
        """Catches the trust pin refusing without enough coordinates to repair it."""
        from floati import wake_daemon_adapters as adapters

        root = FloatiRoot.open_direct_home(self.base / "wake-fleet", create=True)
        Registry(root).register(public_ids.builder("a"), "worker")
        workspace = self.base / "workspace"
        workspace.mkdir()
        executable = self.base / "bound-codex"
        executable.write_bytes(b"#!/bin/sh\nexit 0\n")
        executable.chmod(0o700)
        coordinate = DaemonCoordinate(root, public_ids.builder("a"), "codex")
        binding = AdapterBindingStore(root).write(
            coordinate,
            session_id="session-1",
            workspace=workspace,
            executable=executable,
            adapter_version="1",
            adapter_digest=adapters.adapter_contract_digest("codex"),
            binding_epoch=1,
        )
        missing = self.base / "missing-codex"
        prior = adapters.CODEX_EXECUTABLE
        adapters.CODEX_EXECUTABLE = missing
        self.addCleanup(setattr, adapters, "CODEX_EXECUTABLE", prior)

        adapter = adapters.CodexQueueWakeAdapter(coordinate)
        with self.assertRaises(ProtocolRefusal) as raised:
            adapter.request_wake(binding, "wake", 30)
        self.assertEqual("wake_daemon_codex_executable_absent", raised.exception.code)
        self.assertIn(str(missing), raised.exception.detail)
        self.assertIsInstance(raised.exception.remedy, str)
        self.assertIn(str(missing), raised.exception.remedy)


if __name__ == "__main__":
    unittest.main()

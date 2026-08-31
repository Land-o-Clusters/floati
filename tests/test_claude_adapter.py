from __future__ import annotations

from floati import fixture_ids as public_ids

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from floati.errors import ProtocolRefusal
from floati.workers import WorkerAdapterFailure

try:
    import floati.adapters.claude as claude
    from floati.adapters.claude import ClaudeHeadlessAdapter
except (ImportError, ModuleNotFoundError):
    claude = None
    ClaudeHeadlessAdapter = None


FIXTURE = Path("tests/fixtures/claude-headless/reference_harness.py").resolve()


class ClaudeHeadlessAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.assertIsNotNone(claude, "Claude headless adapter module must exist")
        self.assertIsNotNone(ClaudeHeadlessAdapter, "Claude headless adapter must exist")
        self.temp = tempfile.TemporaryDirectory(dir="\x2fprivate/tmp")
        self.addCleanup(self.temp.cleanup)
        self.parent = Path(self.temp.name) / "slipway-work"
        codex_patcher = mock.patch("floati.adapters.codex_live._WORKSPACE_PARENT", self.parent)
        claude_patcher = mock.patch("floati.adapters.claude._WORKSPACE_PARENT", self.parent)
        codex_patcher.start()
        claude_patcher.start()
        self.addCleanup(codex_patcher.stop)
        self.addCleanup(claude_patcher.stop)
        self.item = {
            "id": "work-019fbb00000070008000000000000001",
            "workspace": str(self.parent / "work-019fbb00000070008000000000000001"),
            "title": "Create PROOF.txt with the fixture proof line",
        }

    def adapter(self, mode: str = "complete"):
        return ClaudeHeadlessAdapter(
            (sys.executable, str(FIXTURE), "--fixture-mode", mode)
        )

    def test_fixture_round_trip_uses_exact_fail_closed_headless_contract(self) -> None:
        adapter = self.adapter()

        handle = adapter.spawn(self.item, deadline_seconds=5)
        bindings = adapter.drive(handle, self.item, deadline_seconds=5)

        workspace = Path(str(self.item["workspace"]))
        fixture = workspace / ".floati" / "claude-fixture.json"
        self.assertTrue(fixture.is_file(), "Floati evidence must include the fixture input")
        invocation = json.loads(
            fixture.read_text(encoding="utf-8")
        )
        self.assertEqual(
            {
                "cwd": str(workspace.resolve()),
                "input_format": "text",
                "output_format": "json",
                "permission_mode": "dontAsk",
                "no_session_persistence": True,
                "print": True,
                "prompt": self.item["title"],
                "tools": ["Read,Write,Edit"],
            },
            invocation,
        )
        self.assertEqual("PROOF.txt", bindings[0]["doc"])
        self.assertEqual("FLOATI Claude fixture proof\n", (workspace / "PROOF.txt").read_text())
        self.assertFalse((workspace / ".floati" / "claude-output.json").is_symlink())
        self.assertFalse(os.path.lexists(workspace / ".slipway"))

    def test_prepared_workspace_refuses_legacy_before_git_or_evidence_creation(self) -> None:
        self.assertIsNotNone(
            getattr(claude, "refuse_legacy_workspace_artifacts", None),
            "Claude adapter must preflight legacy workspace artifacts",
        )
        workspace = Path(str(self.item["workspace"]))
        workspace.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        workspace.mkdir(mode=0o700)
        legacy = workspace / ".slipway-claude"
        contents = b"Claude legacy workspace sentinel\n"
        legacy.write_bytes(contents)
        metadata = workspace.lstat()
        legacy_metadata = legacy.lstat()
        adapter = self.adapter()
        adapter.set_prepared_workspace(
            str(workspace), metadata.st_dev, metadata.st_ino,
        )

        with (
            mock.patch.object(
                claude.CodexAppServerAdapter,
                "_initialize_repository",
                side_effect=AssertionError("legacy refusal must precede Git initialization"),
            ),
            mock.patch.object(
                claude,
                "_open_private_file",
                side_effect=AssertionError("legacy refusal must precede evidence creation"),
            ),
            mock.patch.object(
                claude.subprocess,
                "Popen",
                side_effect=AssertionError("legacy refusal must precede provider launch"),
            ),
            self.assertRaises(ProtocolRefusal) as raised,
        ):
            adapter.spawn(self.item, deadline_seconds=5)

        self.assertEqual("legacy_workspace_artifacts", raised.exception.code)
        self.assertEqual(
            "workspace refused: legacy artifact '.slipway-claude' predates the Floati rename; nothing was read, migrated, or deleted; start a fresh root, or archive the legacy artifacts yourself and run again",
            raised.exception.detail,
        )
        current = legacy.lstat()
        self.assertTrue(os.path.lexists(legacy))
        self.assertEqual(
            (legacy_metadata.st_dev, legacy_metadata.st_ino),
            (current.st_dev, current.st_ino),
        )
        self.assertEqual(contents, legacy.read_bytes())
        self.assertFalse(os.path.lexists(workspace / ".floati"))
        self.assertFalse(os.path.lexists(workspace / ".git"))

    def test_exec_adapter_provider_inherits_the_current_process_group(self) -> None:
        """Catches exec-owned Claude sessions creating a separate provider group."""
        observed_groups: list[int] = []
        real_popen = subprocess.Popen

        def capture_provider_group(*args, **kwargs):
            process = real_popen(*args, **kwargs)
            command = args[0] if args else kwargs.get("args", ())
            if str(FIXTURE) in command:
                observed_groups.append(os.getpgid(process.pid))
            return process

        adapter = ClaudeHeadlessAdapter(
            (sys.executable, str(FIXTURE), "--fixture-mode", "complete"),
            isolate_process_group=False,
        )
        with mock.patch.object(claude.subprocess, "Popen", capture_provider_group):
            handle = adapter.spawn(self.item, deadline_seconds=5)
            adapter.drive(handle, self.item, deadline_seconds=5)

        self.assertEqual([os.getpgrp()], observed_groups)

    def test_prepared_workspace_round_trip_uses_exact_filesystem_identity(self) -> None:
        """Catches Claude bypassing the inherited prepared-workspace contract."""
        workspace = Path(str(self.item["workspace"]))
        workspace.parent.mkdir(parents=True, mode=0o700)
        workspace.mkdir(mode=0o700)
        metadata = workspace.lstat()
        adapter = self.adapter()
        adapter.set_prepared_workspace(
            str(workspace), metadata.st_dev, metadata.st_ino,
        )

        handle = adapter.spawn(self.item, deadline_seconds=5)
        bindings = adapter.drive(handle, self.item, deadline_seconds=5)

        current = workspace.lstat()
        self.assertEqual((metadata.st_dev, metadata.st_ino), (current.st_dev, current.st_ino))
        self.assertEqual(os.getuid(), current.st_uid)
        self.assertEqual(0o700, stat.S_IMODE(current.st_mode))
        self.assertEqual(["PROOF.txt"], [binding["doc"] for binding in bindings])

    def test_prepared_workspace_replacements_cannot_receive_evidence_or_provider(self) -> None:
        """Catches Claude reopening the ruled pathname for evidence or Popen."""
        for boundary in ("evidence", "popen"):
            with self.subTest(boundary=boundary):
                lawful = dict(self.item)
                lawful["id"] = lawful["id"][:-2] + ("11" if boundary == "evidence" else "12")
                lawful["workspace"] = str(self.parent / str(lawful["id"]))
                lawful_workspace = Path(str(lawful["workspace"]))
                lawful_workspace.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                lawful_workspace.mkdir(mode=0o700)
                lawful_metadata = lawful_workspace.lstat()
                lawful_adapter = self.adapter()
                lawful_adapter.set_prepared_workspace(str(lawful_workspace), lawful_metadata.st_dev, lawful_metadata.st_ino)
                lawful_handle = lawful_adapter.spawn(lawful, deadline_seconds=5)
                lawful_adapter.drive(lawful_handle, lawful, deadline_seconds=5)

                item = dict(self.item)
                item["id"] = item["id"][:-2] + ("21" if boundary == "evidence" else "22")
                item["workspace"] = str(self.parent / str(item["id"]))
                workspace = Path(str(item["workspace"]))
                original = workspace.with_name(workspace.name + "-original")
                workspace.mkdir(mode=0o700)
                metadata = workspace.lstat()
                adapter = self.adapter()
                adapter.set_prepared_workspace(str(workspace), metadata.st_dev, metadata.st_ino)
                replaced = False
                real_secure = claude._secure_directory
                real_popen = subprocess.Popen

                def replace() -> None:
                    nonlocal replaced
                    if not replaced:
                        workspace.rename(original)
                        workspace.mkdir(mode=0o700)
                        replaced = True

                def secure(path: Path, *, create: bool) -> None:
                    if boundary == "evidence" and create and path.name == ".floati":
                        replace()
                    real_secure(path, create=create)

                def popen(*args, **kwargs):
                    command = args[0] if args else kwargs.get("args", ())
                    if boundary == "popen" and str(FIXTURE) in command:
                        replace()
                    return real_popen(*args, **kwargs)

                with mock.patch.object(claude, "_secure_directory", secure), mock.patch.object(claude.subprocess, "Popen", popen):
                    try:
                        handle = adapter.spawn(item, deadline_seconds=5)
                        adapter.drive(handle, item, deadline_seconds=5)
                    except WorkerAdapterFailure as exc:
                        self.fail(f"prepared Claude lifecycle lost accepted inode: {exc.code}")

                self.assertTrue(replaced)
                self.assertFalse((workspace / ".git").exists())
                self.assertFalse((workspace / ".floati").exists())

    def test_permission_result_is_typed_unattended_degradation(self) -> None:
        adapter = self.adapter("approval")
        handle = adapter.spawn(self.item, deadline_seconds=5)

        with self.assertRaisesRegex(WorkerAdapterFailure, "approval_required_unattended"):
            adapter.drive(handle, self.item, deadline_seconds=5)

    def test_malformed_oversized_and_failed_results_are_typed(self) -> None:
        for mode, code in (
            ("malformed", "protocol_error"),
            ("oversized", "protocol_error"),
            ("failed", "turn_failed"),
        ):
            with self.subTest(mode=mode):
                item = dict(self.item)
                item["id"] = item["id"][:-2] + {"malformed": "02", "oversized": "03", "failed": "04"}[mode]
                item["workspace"] = str(self.parent / str(item["id"]))
                adapter = self.adapter(mode)
                handle = adapter.spawn(item, deadline_seconds=5)
                with self.assertRaisesRegex(WorkerAdapterFailure, code):
                    adapter.drive(handle, item, deadline_seconds=5)

    def test_cli_parser_accepts_claude_worker_without_expanding_orchestrate(self) -> None:
        from floati.cli import _parser

        worker = _parser().parse_args(
            ["worker", "run", "--root", "\x2fprivate/tmp/fleet", "--as", public_ids.builder('a'), "--adapter", "claude"]
        )
        self.assertEqual("claude", worker.adapter)
        with self.assertRaisesRegex(ProtocolRefusal, "invalid choice: 'claude'"):
            _parser().parse_args(
                ["orchestrate", "--root", "\x2fprivate/tmp/fleet", "--plan", "\x2fprivate/tmp/plan.json", "--adapter", "claude", "--deadline", "5"]
            )


if __name__ == "__main__":
    unittest.main()

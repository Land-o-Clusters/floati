"""Governed zcode Stop-hook install, at the cursor install's contract.

Scratch workspace and scratch fleet root only. The test never resolves
the operator home's ``.zcode`` or this repository's project ``.zcode``.
"""

from __future__ import annotations

import io
import json
import shlex
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from floati import cli, fixture_ids as public_ids
from floati.codex_wait_contract import (
    CodexWaitConsentLedger, CodexWaitSessionLedger, resolve_participant,
)
from floati.mcp import run_cli_artifact
from floati.jsonl import read_records_snapshot

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
from floati.registry import Registry
from floati.root import FloatiRoot
from floati.waiter_consent import arm_waiter_consent
from floati.wake_control import WakeController
from tests.temp_roots import REAL_TEMP_ROOT


NODE = public_ids.builder("z")


class ZCodeHookInstallTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.root = FloatiRoot.open_direct_home(self.base / "fleet", create=True)
        Registry(self.root).register(NODE, "zcode")
        self.workspace = self.base / "workspace"
        self.workspace.mkdir()
        self.runtime = self.base / "runtime"
        (self.runtime / "scripts").mkdir(parents=True)
        (self.runtime / "scripts" / "floati").write_text("#!/bin/sh\n", encoding="utf-8")
        self.hooks_path = self.workspace / ".zcode" / "hooks.json"

    def install_argv(self) -> list[str]:
        return [
            "hook", "install",
            "--harness", "zcode",
            "--root", str(self.root.path),
            "--as", NODE,
            "--workspace", str(self.workspace),
            "--runtime", str(self.runtime),
        ]

    def uninstall_argv(self) -> list[str]:
        argv = self.install_argv()
        argv[1] = "uninstall"
        return argv

    def test_install_arms_the_registered_zcode_workspace_through_waiter_consent(self) -> None:
        code, artifact = run_cli_artifact(self.install_argv())
        self.assertEqual(0, code, artifact)
        participant = resolve_participant(self.root.path, self.workspace)
        self.assertIsNotNone(participant, "installed zcode hook has no waiter workspace binding")
        consent = CodexWaitConsentLedger(self.root).require_armed(participant.binding)
        self.assertEqual(NODE, consent["node_id"])
        self.assertEqual(3700, consent["hook_timeout_seconds"])
        self.assertEqual(3600, consent["wait_deadline_seconds"])

    def test_installed_command_reaches_the_native_waiter_with_zcode_payload(self) -> None:
        # The real native waiter records its paused exit. No harness process
        # or model is launched, and no synthetic map writer is involved.
        arm_waiter_consent(self.root.path, self.workspace, NODE, "zcode", 3700, 3600)
        participant = resolve_participant(self.root.path, self.workspace)
        consent = CodexWaitConsentLedger(self.root).require_armed(participant.binding)
        session = "zcode-hook-fixture-session"
        CodexWaitSessionLedger(self.root).arm(
            participant.binding, consent, session, idempotency_key="zcode-hook-fixture",
        )
        WakeController(self.root).pause(NODE, session, idempotency_key="zcode-hook-pause")
        code, installed = run_cli_artifact(self.install_argv())
        self.assertEqual(0, code, installed)
        hook = json.loads(self.hooks_path.read_text())["hooks"]["Stop"][-1]["hooks"][-1]
        stdout, stderr = io.StringIO(), io.StringIO()
        payload = {"hook_event_name": "Stop", "session_id": session, "cwd": str(self.workspace)}
        with mock.patch("pathlib.Path.cwd", return_value=self.workspace),\
                mock.patch("sys.stdin", io.StringIO(json.dumps(payload))),\
                redirect_stdout(stdout), redirect_stderr(stderr):
            code = cli.main(shlex.split(hook["command"])[1:])
        self.assertEqual(0, code, stderr.getvalue() + stdout.getvalue())
        self.assertEqual("", stdout.getvalue())
        exits = read_records_snapshot(self.root,
            Path("receipts/wake-waiter-exit") / f"{NODE}.jsonl",
            allowed_kinds={"wake_waiter_exit_receipt"})
        self.assertEqual(1, len(exits))
        self.assertEqual("paused", exits[0]["reason_code"])

    def test_installed_command_cannot_borrow_the_payload_workspace(self) -> None:
        code, installed = run_cli_artifact(self.install_argv())
        self.assertEqual(0, code, installed)
        hook = json.loads(self.hooks_path.read_text())["hooks"]["Stop"][-1]["hooks"][-1]
        payload = {"hook_event_name": "Stop", "session_id": "zcode-hook-fixture-session",
            "cwd": str(self.workspace)}
        with mock.patch("pathlib.Path.cwd", return_value=self.base),\
                mock.patch("sys.stdin", io.StringIO(json.dumps(payload))):
            code, refused = run_cli_artifact(shlex.split(hook["command"])[1:])
        self.assertEqual(20, code, refused)
        self.assertEqual("zcode_wait_workspace_unbound", refused["evidence"]["code"])

    def test_install_refuses_a_different_registered_harness_before_hook_writes(self) -> None:
        other = public_ids.worker("codex")
        Registry(self.root).register(other, "codex")
        argv = self.install_argv()
        argv[argv.index("--as") + 1] = other
        code, refused = run_cli_artifact(argv)
        self.assertEqual(20, code, refused)
        self.assertEqual("waiter_harness_mismatch", refused["evidence"]["code"])
        self.assertFalse(self.hooks_path.exists())
        self.assertFalse((self.root.path / "codex-wait/workspaces.v0.json").exists())

    def test_install_records_explicit_root_and_node_and_a_root_receipt(self) -> None:
        code, artifact = run_cli_artifact(self.install_argv())
        self.assertEqual(0, code, artifact)
        self.assertEqual("ok", artifact["status"])
        evidence = artifact["evidence"]
        self.assertEqual("zcode", evidence["harness"])
        self.assertEqual(str(self.root.path), evidence["root"])
        self.assertEqual(NODE, evidence["node"])
        self.assertEqual(str(self.hooks_path), evidence["hooks_path"])
        self.assertEqual(str(self.workspace), evidence["workspace"])

        document = json.loads(self.hooks_path.read_text(encoding="utf-8"))
        hook = document["hooks"]["Stop"][0]["hooks"][0]
        self.assertEqual("command", hook["type"])
        argv = shlex.split(hook["command"])
        self.assertEqual(str(self.runtime / "scripts" / "floati"), argv[0])
        self.assertEqual(["wake", "wait", "--harness", "zcode"], argv[1:5])
        flags = dict(zip(argv[5::2], argv[6::2]))
        self.assertEqual(str(self.root.path), flags["--root"])
        self.assertEqual(NODE, flags["--as"])
        self.assertEqual(str(self.runtime), flags["--runtime"])

        receipt_path = (
            self.root.path / "state" / "zcode-wait" / NODE / "installed-hook.json"
        )
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        self.assertEqual(str(self.hooks_path), receipt["hooks_path"])
        self.assertEqual(NODE, receipt["node"])
        self.assertEqual(str(self.workspace), receipt["workspace"])
        self.assertEqual(str(self.root.path), receipt["root"])
        self.assertFalse((Path.home() / ".zcode").exists())

    def test_install_keeps_a_foreign_stop_command(self) -> None:
        foreign = {
            "type": "command",
            "command": "/usr/bin/true",
            "timeout": 3,
        }
        self.hooks_path.parent.mkdir(parents=True)
        self.hooks_path.write_text(json.dumps({
            "hooks": {"Stop": [{"matcher": "*", "hooks": [foreign]}]},
        }), encoding="utf-8")
        code, artifact = run_cli_artifact(self.install_argv())
        self.assertEqual(0, code, artifact)
        document = json.loads(self.hooks_path.read_text(encoding="utf-8"))
        commands = [
            hook["command"]
            for entry in document["hooks"]["Stop"]
            for hook in entry["hooks"]
        ]
        self.assertIn("/usr/bin/true", commands)
        self.assertTrue(any("wake wait" in command and "--harness zcode" in command
                            for command in commands))

    def test_install_writes_the_hold_constant_into_the_hook(self) -> None:
        from floati import zcode_hook_install as install

        with mock.patch.object(install, "ZCODE_HOLD_SECONDS", (11, 7), create=True):
            code, artifact = run_cli_artifact(self.install_argv())
        self.assertEqual(0, code, artifact)
        hook = json.loads(self.hooks_path.read_text(encoding="utf-8"))["hooks"]["Stop"][-1]["hooks"][-1]
        argv = shlex.split(hook["command"])
        flags = dict(zip(argv[5::2], argv[6::2]))
        self.assertEqual(11, hook.get("timeout"))
        self.assertEqual("7", flags.get("--deadline-seconds"))
        self.assertEqual("11", flags.get("--hook-timeout-seconds"))

    def test_doctor_reports_the_installed_hook_and_its_hold(self) -> None:
        code, artifact = run_cli_artifact(self.install_argv())
        self.assertEqual(0, code, artifact)
        document = json.loads(self.hooks_path.read_text(encoding="utf-8"))
        hook = document["hooks"]["Stop"][-1]["hooks"][-1]
        flags = dict(zip(shlex.split(hook["command"])[5::2], shlex.split(hook["command"])[6::2]))
        from floati.doctor import Doctor

        doctor, _status = Doctor(
            REPOSITORY_ROOT, str(self.root.path), ref="HEAD", no_sandbox=True,
        ).artifact()
        rows = [row for row in doctor["findings"] if row.get("code") == "zcode_hook_installed"]
        self.assertTrue(rows)
        for row in rows:
            self.assertEqual(hook["timeout"], row["hook_timeout_seconds"])
            self.assertEqual(int(flags["--deadline-seconds"]), row["wait_deadline_seconds"])
            self.assertEqual(str(self.hooks_path), row["hooks_path"])
        self.assertFalse((Path.home() / ".zcode").exists())

    def test_uninstall_restores_the_exact_preinstall_bytes(self) -> None:
        prior = b'{"kept":true,"hooks":{"Stop":[{"matcher":"*","hooks":[{"command":"/usr/bin/true","timeout":3,"type":"command"}]}]}}\n'
        self.hooks_path.parent.mkdir(parents=True)
        self.hooks_path.write_bytes(prior)
        code, installed = run_cli_artifact(self.install_argv())
        self.assertEqual(0, code, installed)
        hook_bytes = self.hooks_path.read_bytes()
        self.assertNotEqual(prior, hook_bytes)
        receipt_path = self.root.path / "state" / "zcode-wait" / NODE / "installed-hook.json"
        receipt = receipt_path.read_bytes()
        code, again = run_cli_artifact(self.install_argv())
        self.assertEqual(0, code, again)
        self.assertEqual(hook_bytes, self.hooks_path.read_bytes())
        self.assertEqual(receipt, receipt_path.read_bytes())
        code, removed = run_cli_artifact(self.uninstall_argv())
        self.assertEqual(0, code, removed)
        self.assertEqual(prior, self.hooks_path.read_bytes())

    def test_uninstall_removes_a_hook_file_that_did_not_exist(self) -> None:
        self.assertFalse(self.hooks_path.exists())
        code, installed = run_cli_artifact(self.install_argv())
        self.assertEqual(0, code, installed)
        self.assertTrue(self.hooks_path.is_file())
        hook_bytes = self.hooks_path.read_bytes()
        code, again = run_cli_artifact(self.install_argv())
        self.assertEqual(0, code, again)
        self.assertEqual(hook_bytes, self.hooks_path.read_bytes())
        code, removed = run_cli_artifact(self.uninstall_argv())
        self.assertEqual(0, code, removed)
        self.assertFalse(self.hooks_path.exists())

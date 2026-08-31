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
from floati.wake_daemon_adapters import adapter_contract_digest
from floati.wake_daemon_contract import (
    AdapterBindingStore,
    DaemonConsentLedger,
    DaemonCoordinate,
)
from tests.temp_roots import REAL_TEMP_ROOT


class _Launchctl:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.print_returncode = 113

    def __call__(self, argv: tuple[str, ...]) -> subprocess.CompletedProcess[str]:
        self.calls.append(argv)
        returncode = self.print_returncode if argv[1] == "print" else 0
        return subprocess.CompletedProcess(argv, returncode, "", "")


class WakeDaemonLaunchAgentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.root = FloatiRoot.open_direct_home(self.base / "fleet-alpha", create=True)
        Registry(self.root).register(public_ids.builder('a'), "worker")
        self.coordinate = DaemonCoordinate(self.root, public_ids.builder('a'), "cursor")
        self.workspace = self.base / "workspace"
        self.workspace.mkdir()
        self.executable = self.base / "cursor-agent"
        self.executable.write_bytes(b"#!/bin/sh\nexit 0\n")
        self.executable.chmod(0o700)
        AdapterBindingStore(self.root).write(
            self.coordinate,
            session_id="cursor-session-1",
            workspace=self.workspace,
            executable=self.executable,
            adapter_version="1",
            adapter_digest=adapter_contract_digest("cursor"),
            binding_epoch=1,
        )
        DaemonConsentLedger(self.root).consent(
            self.coordinate,
            adapter_version="1",
            adapter_digest=adapter_contract_digest("cursor"),
            min_poll_seconds=1,
            max_poll_seconds=30,
            max_backoff_seconds=120,
            activation_epoch=1,
            idempotency_key="launchd-consent",
        )
        self.launcher = self.base / "installed" / "scripts" / "floati"
        self.launcher.parent.mkdir(parents=True)
        self.launcher.write_bytes(b"#!/bin/sh\nexit 0\n")
        self.launcher.chmod(0o700)
        self.launch_agents = self.base / "Library" / "LaunchAgents"
        self.runner = _Launchctl()

    def manager(self):
        from floati.wake_daemon_launchd import LaunchAgentManager

        return LaunchAgentManager(
            self.coordinate,
            installed_launcher=self.launcher,
            launch_agents_directory=self.launch_agents,
            uid=501,
            runner=self.runner,
        )

    def test_preview_is_deterministic_closed_and_contains_no_listener(self) -> None:
        preview = self.manager().preview()
        self.assertEqual(
            {
                "Label",
                "ProgramArguments",
                "RunAtLoad",
                "KeepAlive",
                "ProcessType",
                "ThrottleInterval",
                "StandardOutPath",
                "StandardErrorPath",
            },
            set(preview["plist"]),
        )
        self.assertEqual(
            [
                str(self.launcher),
                "wake",
                "daemon",
                "serve",
                "--root",
                str(self.root.path),
                "--as",
                public_ids.builder('a'),
                "--harness",
                "cursor",
                "--activation-epoch",
                "1",
            ],
            preview["plist"]["ProgramArguments"],
        )
        self.assertEqual(
            hashlib.sha256(preview["encoded"]).hexdigest(),
            preview["plist_digest"],
        )
        self.assertNotIn(b"Sockets", preview["encoded"])
        self.assertEqual([], self.runner.calls)

    def test_grok_build_preview_is_bound_to_the_exact_closed_coordinate(self) -> None:
        from floati.wake_daemon_launchd import LaunchAgentManager

        coordinate = DaemonCoordinate(self.root, public_ids.builder('a'), "grok-build")
        AdapterBindingStore(self.root).write(
            coordinate,
            session_id=public_ids.compose(public_ids.verifier(), '-session-1'),
            workspace=self.workspace,
            executable=self.executable,
            adapter_version="1",
            adapter_digest=adapter_contract_digest("grok-build"),
            binding_epoch=1,
        )
        DaemonConsentLedger(self.root).consent(
            coordinate,
            adapter_version="1",
            adapter_digest=adapter_contract_digest("grok-build"),
            min_poll_seconds=1,
            max_poll_seconds=30,
            max_backoff_seconds=120,
            activation_epoch=7,
            idempotency_key=public_ids.compose(public_ids.verifier(), '-launchd-consent'),
        )
        manager = LaunchAgentManager(
            coordinate,
            installed_launcher=self.launcher,
            launch_agents_directory=self.launch_agents,
            uid=501,
            runner=self.runner,
        )

        preview = manager.preview()

        self.assertEqual(
            [
                str(self.launcher),
                "wake",
                "daemon",
                "serve",
                "--root",
                str(self.root.path),
                "--as",
                public_ids.builder('a'),
                "--harness",
                "grok-build",
                "--activation-epoch",
                "7",
            ],
            preview["plist"]["ProgramArguments"],
        )
        self.assertEqual(
            hashlib.sha256(preview["encoded"]).hexdigest(),
            preview["plist_digest"],
        )
        self.assertEqual([], self.runner.calls)

    def test_program_arguments_execute_installed_shell_launcher_directly(self) -> None:
        preview = self.manager().preview()

        completed = subprocess.run(
            preview["plist"]["ProgramArguments"],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )

        self.assertEqual(0, completed.returncode, completed.stderr)

    def test_install_is_digest_exact_and_never_calls_launchctl(self) -> None:
        installed = self.manager().install()
        path = Path(installed["plist_path"])
        self.assertEqual(installed["plist_digest"], hashlib.sha256(path.read_bytes()).hexdigest())
        self.assertEqual(0o600, path.stat().st_mode & 0o777)
        self.assertEqual([], self.runner.calls)

    def test_start_refuses_missing_or_mismatched_plist_then_uses_fixed_user_vectors(self) -> None:
        manager = self.manager()
        with self.assertRaisesRegex(ProtocolRefusal, "supervisor_missing"):
            manager.start()
        self.assertEqual([], self.runner.calls)

        manager.install()
        manager.plist_path.write_bytes(b"changed\n")
        with self.assertRaisesRegex(ProtocolRefusal, "digest_mismatch"):
            manager.start()
        self.assertEqual([], self.runner.calls)

        manager.plist_path.write_bytes(manager.preview()["encoded"])
        started = manager.start()
        label = manager.label
        self.assertEqual("running", started["state"])
        self.assertEqual(
            [
                ("/bin/launchctl", "bootstrap", "gui/501", str(manager.plist_path)),
                ("/bin/launchctl", "kickstart", f"gui/501/{label}"),
            ],
            self.runner.calls,
        )

    def test_stop_proves_absence_or_reports_unknown(self) -> None:
        manager = self.manager()
        manager.install()
        stopped = manager.stop()
        self.assertEqual("stopped", stopped["state"])
        self.assertEqual("bootout", self.runner.calls[-2][1])
        self.assertEqual("print", self.runner.calls[-1][1])

        self.runner.print_returncode = 0
        unknown = manager.stop()
        self.assertEqual("unknown", unknown["state"])

    def test_remove_refuses_digest_drift_without_deleting_then_removes_exact_plist(self) -> None:
        manager = self.manager()
        installed = manager.install()
        with self.assertRaisesRegex(ProtocolRefusal, "digest_mismatch"):
            manager.remove(expected_plist_digest="0" * 64)
        self.assertTrue(manager.plist_path.exists())

        manager.plist_path.write_bytes(b"changed\n")
        with self.assertRaisesRegex(ProtocolRefusal, "digest_mismatch"):
            manager.remove(expected_plist_digest=installed["plist_digest"])
        self.assertTrue(manager.plist_path.exists())

        manager.plist_path.write_bytes(manager.preview()["encoded"])
        removed = manager.remove(expected_plist_digest=installed["plist_digest"])
        self.assertEqual("removed", removed["state"])
        self.assertFalse(manager.plist_path.exists())

    def test_revoke_refuses_a_symlinked_plist_without_closing_consent(self) -> None:
        manager = self.manager()
        target = self.base / "foreign.plist"
        target.write_bytes(manager.preview()["encoded"])
        manager.launch_agents_directory.mkdir(parents=True)
        manager.plist_path.symlink_to(target)

        with self.assertRaisesRegex(ProtocolRefusal, "digest_mismatch"):
            manager.revoke(idempotency_key="symlink-revoke")
        self.assertTrue(manager.plist_path.is_symlink())
        self.assertEqual(
            "active",
            DaemonConsentLedger(self.root).require_active(self.coordinate)["state"],
        )

    def test_revoke_deletes_the_exact_plist_and_does_not_overclaim_process_absence(self) -> None:
        manager = self.manager()
        manager.install()
        self.runner.print_returncode = 0
        revoked = manager.revoke(idempotency_key="launchd-revoke")
        self.assertEqual("unknown", revoked["state"])
        self.assertFalse(manager.plist_path.exists())
        with self.assertRaisesRegex(ProtocolRefusal, "consent_absent"):
            DaemonConsentLedger(self.root).require_active(self.coordinate)


if __name__ == "__main__":
    unittest.main()

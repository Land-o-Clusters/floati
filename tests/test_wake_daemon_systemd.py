from __future__ import annotations

from floati import fixture_ids as public_ids

import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from floati.errors import ProtocolRefusal
from floati.registry import Registry
from floati.root import FloatiRoot
from floati.wake_daemon_adapters import adapter_contract_digest
from floati.wake_daemon_contract import (
    AdapterBindingStore,
    DaemonConsentLedger,
    DaemonCoordinate,
)


class _Systemctl:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.is_active_returncode = 3

    def __call__(self, argv: tuple[str, ...]) -> subprocess.CompletedProcess[str]:
        self.calls.append(argv)
        returncode = self.is_active_returncode if argv[2] == "is-active" else 0
        return subprocess.CompletedProcess(argv, returncode, "", "")


class WakeDaemonSystemdUserUnitTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = "\x2fprivate/tmp" if Path("\x2fprivate/tmp").is_dir() else None
        self.temporary = tempfile.TemporaryDirectory(dir=tmp)
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.root = FloatiRoot.open_direct_home(self.base / "fleet-alpha", create=True)
        Registry(self.root).register(public_ids.builder("a"), "worker")
        self.coordinate = DaemonCoordinate(self.root, public_ids.builder("a"), "cursor")
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
            idempotency_key="systemd-consent",
        )
        self.launcher = self.base / "installed" / "scripts" / "floati"
        self.launcher.parent.mkdir(parents=True)
        self.launcher.write_bytes(b"#!/bin/sh\nexit 0\n")
        self.launcher.chmod(0o700)
        self.user_units = self.base / "systemd" / "user"
        self.systemctl = self.base / "host-bin" / "systemctl"
        self.systemctl.parent.mkdir()
        self.systemctl.write_bytes(b"#!/bin/sh\nexit 0\n")
        self.systemctl.chmod(0o700)
        self.runner = _Systemctl()

    def manager(self, **overrides):
        from floati.wake_daemon_systemd import SystemdUserUnitManager

        kwargs = {
            "installed_launcher": self.launcher,
            "user_units_directory": self.user_units,
            "runner": self.runner,
            "systemctl_locator": lambda: str(self.systemctl),
        }
        kwargs.update(overrides)
        return SystemdUserUnitManager(self.coordinate, **kwargs)

    def test_module_imports_and_exposes_the_launchd_twin(self) -> None:
        from floati.wake_daemon_systemd import SystemdUserUnitManager

        self.assertTrue(callable(SystemdUserUnitManager.preview))
        self.assertTrue(callable(SystemdUserUnitManager.install))
        self.assertTrue(callable(SystemdUserUnitManager.start))
        self.assertTrue(callable(SystemdUserUnitManager.status))
        self.assertTrue(callable(SystemdUserUnitManager.stop))
        self.assertTrue(callable(SystemdUserUnitManager.remove))
        self.assertTrue(callable(SystemdUserUnitManager.revoke))

    def test_shipped_source_has_no_owner_home_literal(self) -> None:
        source = Path("floati/wake_daemon_systemd.py").read_text(encoding="utf-8")
        self.assertNotIn("\x2fUsers/", source)
        self.assertNotIn("penguinspecz", source.casefold())

    def test_default_user_units_directory_is_path_home(self) -> None:
        manager = self.manager()
        from floati.wake_daemon_systemd import SystemdUserUnitManager

        defaulted = SystemdUserUnitManager(
            self.coordinate,
            installed_launcher=self.launcher,
            runner=self.runner,
        )
        self.assertEqual(
            Path.home() / ".config" / "systemd" / "user",
            defaulted.user_units_directory,
        )
        self.assertTrue(defaulted.daemon_instance_id.startswith("systemd-"))
        self.assertNotEqual(manager.user_units_directory, defaulted.user_units_directory)

    def test_preview_is_deterministic_closed_user_unit_and_contains_no_listener(self) -> None:
        preview = self.manager().preview()
        encoded = preview["encoded"]
        self.assertIsInstance(encoded, bytes)
        text = encoded.decode("utf-8")
        self.assertIn("[Unit]\n", text)
        self.assertIn("[Service]\n", text)
        self.assertIn("[Install]\n", text)
        self.assertIn("Type=simple\n", text)
        self.assertIn("WantedBy=default.target\n", text)
        self.assertEqual(
            [
                str(self.launcher),
                "wake",
                "daemon",
                "serve",
                "--root",
                str(self.root.path),
                "--as",
                public_ids.builder("a"),
                "--harness",
                "cursor",
                "--activation-epoch",
                "1",
            ],
            preview["program_arguments"],
        )
        for argument in preview["program_arguments"]:
            self.assertIn(argument, text)
        self.assertEqual(
            hashlib.sha256(encoded).hexdigest(),
            preview["plist_digest"],
        )
        lowered = encoded.lower()
        self.assertNotIn(b"listenstream", lowered)
        self.assertNotIn(b"sockets", lowered)
        self.assertNotIn(b"socket=", lowered)
        self.assertEqual([], self.runner.calls)

    def test_preview_refuses_consent_and_binding_digest_mismatch(self) -> None:
        AdapterBindingStore(self.root).write(
            self.coordinate,
            session_id="cursor-session-1",
            workspace=self.workspace,
            executable=self.executable,
            adapter_version="1",
            adapter_digest="0" * 64,
            binding_epoch=2,
        )
        with self.assertRaisesRegex(ProtocolRefusal, "adapter_digest_mismatch"):
            self.manager().preview()

    def test_program_arguments_execute_installed_shell_launcher_directly(self) -> None:
        preview = self.manager().preview()
        completed = subprocess.run(
            preview["program_arguments"],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
        self.assertEqual(0, completed.returncode, completed.stderr)

    def test_install_is_digest_exact_and_never_calls_systemctl(self) -> None:
        installed = self.manager().install()
        path = Path(installed["plist_path"])
        self.assertTrue(path.name.endswith(".service"))
        self.assertEqual(installed["plist_digest"], hashlib.sha256(path.read_bytes()).hexdigest())
        self.assertEqual(0o600, path.stat().st_mode & 0o777)
        self.assertEqual([], self.runner.calls)

    def test_start_refuses_missing_or_mismatched_unit_then_uses_fixed_user_vectors(self) -> None:
        manager = self.manager()
        with self.assertRaisesRegex(ProtocolRefusal, "supervisor_missing"):
            manager.start()
        self.assertEqual([], self.runner.calls)

        manager.install()
        manager.unit_path.write_bytes(b"changed\n")
        with self.assertRaisesRegex(ProtocolRefusal, "digest_mismatch"):
            manager.start()
        self.assertEqual([], self.runner.calls)

        manager.unit_path.write_bytes(manager.preview()["encoded"])
        started = manager.start()
        unit = manager.unit_name
        self.assertEqual("running", started["state"])
        self.assertEqual(
            [
                (str(self.systemctl), "--user", "daemon-reload"),
                (str(self.systemctl), "--user", "start", unit),
            ],
            self.runner.calls,
        )

    def test_linux_control_ignores_path_decoy_and_keeps_fixed_user_vectors(self) -> None:
        decoy = self.base / "decoy-bin" / "systemctl"
        decoy.parent.mkdir()
        decoy.write_bytes(b"#!/bin/sh\nexit 0\n")
        decoy.chmod(0o700)
        manager = self.manager(systemctl_locator=None)
        manager.install()

        with patch("floati.wake_daemon_systemd.sys.platform", "linux"), patch(
            "floati.wake_daemon_systemd.SYSTEMCTL_CANDIDATES",
            (str(self.systemctl),),
            create=True,
        ), patch.dict(os.environ, {"PATH": str(decoy.parent)}, clear=True):
            started = manager.start()

        self.assertEqual("running", started["state"])
        self.assertEqual(
            [
                (str(self.systemctl), "--user", "daemon-reload"),
                (str(self.systemctl), "--user", "start", manager.unit_name),
            ],
            self.runner.calls,
        )

    def test_systemctl_allowlist_uses_the_same_derived_executable(self) -> None:
        manager = self.manager()
        valid = (
            str(self.systemctl),
            "--user",
            "is-active",
            manager.unit_name,
        )
        manager._systemctl(valid)
        self.assertEqual([valid], self.runner.calls)

        with self.assertRaisesRegex(ProtocolRefusal, "systemctl_vector_invalid"):
            manager._systemctl(
                (
                    str(self.systemctl),
                    "--system",
                    "is-active",
                    manager.unit_name,
                )
            )
        self.assertEqual([valid], self.runner.calls)

    def test_linux_control_names_absent_fixed_systemctl_candidates(self) -> None:
        candidates = ("/missing/usr/bin/systemctl", "/missing/bin/systemctl")
        manager = self.manager(runner=None, systemctl_locator=None)
        manager.install()

        with patch("floati.wake_daemon_systemd.sys.platform", "linux"), patch(
            "floati.wake_daemon_systemd.SYSTEMCTL_CANDIDATES",
            candidates,
            create=True,
        ):
            with self.assertRaises(ProtocolRefusal) as caught:
                manager.start()

        self.assertEqual("wake_daemon_systemctl_unavailable", caught.exception.code)
        self.assertEqual(
            "systemctl executable is absent from fixed candidates: "
            "/missing/usr/bin/systemctl, /missing/bin/systemctl",
            caught.exception.detail,
        )

    def test_status_maps_is_active_exit_codes(self) -> None:
        manager = self.manager()
        manager.install()
        self.runner.is_active_returncode = 0
        self.assertEqual("running", manager.status()["state"])
        self.runner.is_active_returncode = 3
        stopped = manager.status()
        self.assertEqual("stopped", stopped["state"])
        self.assertEqual("wake_daemon_process_absent", stopped["reason_code"])
        self.runner.is_active_returncode = 4
        unknown = manager.status()
        self.assertEqual("unknown", unknown["state"])
        self.assertEqual("wake_daemon_process_unknown", unknown["reason_code"])

    def test_stop_proves_absence_or_reports_unknown(self) -> None:
        manager = self.manager()
        manager.install()
        stopped = manager.stop()
        self.assertEqual("stopped", stopped["state"])
        self.assertEqual("stop", self.runner.calls[-2][2])
        self.assertEqual("is-active", self.runner.calls[-1][2])

        self.runner.is_active_returncode = 0
        unknown = manager.stop()
        self.assertEqual("unknown", unknown["state"])

    def test_remove_refuses_digest_drift_without_deleting_then_removes_exact_unit(self) -> None:
        manager = self.manager()
        installed = manager.install()
        with self.assertRaisesRegex(ProtocolRefusal, "digest_mismatch"):
            manager.remove(expected_plist_digest="0" * 64)
        self.assertTrue(manager.unit_path.exists())

        manager.unit_path.write_bytes(b"changed\n")
        with self.assertRaisesRegex(ProtocolRefusal, "digest_mismatch"):
            manager.remove(expected_plist_digest=installed["plist_digest"])
        self.assertTrue(manager.unit_path.exists())

        manager.unit_path.write_bytes(manager.preview()["encoded"])
        removed = manager.remove(expected_plist_digest=installed["plist_digest"])
        self.assertEqual("removed", removed["state"])
        self.assertFalse(manager.unit_path.exists())

    def test_revoke_refuses_a_symlinked_unit_without_closing_consent(self) -> None:
        manager = self.manager()
        target = self.base / "foreign.service"
        target.write_bytes(manager.preview()["encoded"])
        manager.user_units_directory.mkdir(parents=True)
        manager.unit_path.symlink_to(target)

        with self.assertRaisesRegex(ProtocolRefusal, "digest_mismatch"):
            manager.revoke(idempotency_key="symlink-revoke")
        self.assertTrue(manager.unit_path.is_symlink())
        self.assertEqual(
            "active",
            DaemonConsentLedger(self.root).require_active(self.coordinate)["state"],
        )

    def test_revoke_deletes_the_exact_unit_and_does_not_overclaim_process_absence(self) -> None:
        manager = self.manager()
        manager.install()
        self.runner.is_active_returncode = 0
        revoked = manager.revoke(idempotency_key="systemd-revoke")
        self.assertEqual("unknown", revoked["state"])
        self.assertFalse(manager.unit_path.exists())
        with self.assertRaisesRegex(ProtocolRefusal, "consent_absent"):
            DaemonConsentLedger(self.root).require_active(self.coordinate)

    def test_default_runner_on_non_linux_is_typed_absence(self) -> None:
        if sys.platform.startswith("linux"):
            self.skipTest("Linux live systemd activate is row 20, not this sitting")
        manager = self.manager(runner=None)
        manager.install()
        with self.assertRaisesRegex(ProtocolRefusal, "systemd_unmeasurable"):
            manager.start()
        with self.assertRaisesRegex(ProtocolRefusal, "systemd_unmeasurable"):
            manager.status()
        with self.assertRaisesRegex(ProtocolRefusal, "systemd_unmeasurable"):
            manager.stop()

    def test_live_systemctl_user_is_absent_or_unmeasurable_on_this_darwin_seat(self) -> None:
        """Filesystem fact only. Never treat Darwin systemctl as a Linux pass."""
        if sys.platform.startswith("linux"):
            self.skipTest("Linux live systemd is the LX-phase conformance row")
        which = shutil.which("systemctl")
        if which is not None:
            self.fail(
                f"systemctl is on PATH at {which}; Darwin still must not claim a "
                "Linux user-unit pass — default-runner start is typed unmeasurable"
            )

    def test_linux_cli_selects_systemd_and_darwin_keeps_launchd(self) -> None:
        from floati.admin_cli import _wake_daemon_manager
        from floati.wake_daemon_launchd import LaunchAgentManager
        from floati.wake_daemon_systemd import SystemdUserUnitManager

        with patch("floati.admin_cli.sys.platform", "linux"):
            selected = _wake_daemon_manager(self.coordinate)
            self.assertIsInstance(selected, SystemdUserUnitManager)
        with patch("floati.admin_cli.sys.platform", "darwin"):
            selected = _wake_daemon_manager(self.coordinate)
            self.assertIsInstance(selected, LaunchAgentManager)


if __name__ == "__main__":
    unittest.main()

"""WD-2 (P4): terminal refusals exit 0 and final, stop proves, uninstall removes.

The deciding questions, constructed RED-first on the pre-fix tree:

(a) consent revoked mid-serve: today the recovery path re-raises the same
    refusal out of serve() - a non-zero exit the LaunchAgent's
    ``KeepAlive: {SuccessfulExit: false}`` answers by relaunching every
    ThrottleInterval seconds, forever. The contract: exactly one typed
    lifecycle receipt and serve() returns cleanly.
(b) registry entry retired/inactive: today the daemon writes a
    cycle_exception receipt every cycle and polls forever. The contract:
    one retire receipt, exit clean.
(c) activation epoch moved: mid-serve today the daemon silently adopts the
    new epoch and keeps running; at startup the serve command refuses
    non-zero and launchd relaunches it every ten seconds (the measured
    36,722-refusal incident). The contract: one retire receipt, exit clean,
    both mid-serve and at startup.
(d) stop proves: today a bootout whose observation says anything but the
    one expected return code reports state ``unknown`` under ``ok`` - the
    measured night it could not tell two still-running daemons from two it
    had just stopped. The contract: ``stopped`` with the observed pid gone,
    or ``stop_unproven`` with what it saw - never ``unknown`` - and a stop
    of a daemon that is not running says so by name.
(e) uninstall removes: today floati uninstall has zero wake-daemon
    handling. The contract: every installed floati wake supervisor
    (plist/unit) is proven stopped and removed with a receipt; with no
    daemon installed the receipt says none.

Receipt events sit inside the lifecycle enum records.py already ships;
the dispatch's `event: retired` was RULED down to reuse (bus
msg-01a08d33c626728e988647ea4a2ee6c5): consent-revoke -> event/state
revoked; registry-inactive, binding-gone, epoch-moved -> event/state
stopped, with the ruled reason names carrying the typed reason. This
file asserts the ruled vocabulary.
"""

from __future__ import annotations

import argparse
import json
import plistlib
import subprocess
import tempfile
import unittest
from pathlib import Path

from floati import fixture_ids as public_ids
from floati.errors import ProtocolRefusal
from floati.jsonl import read_records_snapshot
from floati.registry import Registry
from floati.root import FloatiRoot
from floati.wake_daemon_adapters import adapter_contract_digest
from floati.wake_daemon_contract import (
    DAEMON_KINDS,
    AdapterBindingStore,
    DaemonConsentLedger,
    DaemonCoordinate,
)
from tests.temp_roots import REAL_TEMP_ROOT

_CYCLE_CAP = 60
_RETIRE_EVENT_CONSENT_REVOKED = "revoked"
_RETIRE_EVENT_DEFAULT = "stopped"


def _lifecycle_rows(root: FloatiRoot, node_id: str) -> list[dict]:
    return [
        row
        for row in read_records_snapshot(
            root,
            Path("receipts/wake-daemon") / f"{node_id}.jsonl",
            allowed_kinds=DAEMON_KINDS,
        )
        if row.get("kind") == "wake_daemon_lifecycle_receipt"
    ]


def _retire_receipts(root: FloatiRoot, node_id: str, reason_code: str) -> list[dict]:
    return [
        row
        for row in _lifecycle_rows(root, node_id)
        if row.get("event") in (_RETIRE_EVENT_CONSENT_REVOKED, _RETIRE_EVENT_DEFAULT)
        and row.get("reason_code") == reason_code
    ]


class _NoMailAdapter:
    """A bound adapter for a seat with no mail: cycles resolve idle."""

    def __init__(self, root: FloatiRoot, coordinate: DaemonCoordinate) -> None:
        self.coordinate = coordinate
        self.store = AdapterBindingStore(root)

    def exact_binding(self):
        from floati.wake_daemon_adapters import AdapterBinding

        return AdapterBinding.from_record(self.store.read(self.coordinate))

    def observe_session(self, binding: object) -> str:
        return "unknown"

    def request_wake(
        self,
        binding: object,
        reason: str,
        deadline_seconds: int,
        envelopes: object = None,
    ):
        from floati.wake_daemon_adapters import WakeAdapterResult

        return WakeAdapterResult("woke", None, 0, "e" * 64)


class _ServeFixture(unittest.TestCase):
    """One consented, bound, mail-less coordinate under a scratch root."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.root = FloatiRoot.open_direct_home(self.base / "fleet", create=True)
        self.node = public_ids.builder("a")
        Registry(self.root).register(self.node, "worker")
        self.coordinate = DaemonCoordinate(self.root, self.node, "cursor")
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
        self.consent = DaemonConsentLedger(self.root)
        self.consent.consent(
            self.coordinate,
            adapter_version="1",
            adapter_digest=adapter_contract_digest("cursor"),
            min_poll_seconds=1,
            max_poll_seconds=30,
            max_backoff_seconds=120,
            activation_epoch=1,
            idempotency_key="wd2-consent-1",
        )

    def daemon(self):
        from floati.wake_daemon import WakeDaemon

        return WakeDaemon(self.coordinate, _NoMailAdapter(self.root, self.coordinate))

    def serve_bounded(self, daemon, hook) -> list[float]:
        """Run serve() with a bounded loop; `hook` fires between cycles.

        The cap exists so a pre-fix daemon that polls forever on a terminal
        condition fails the assertion instead of hanging the run.
        """

        sleeps: list[float] = []
        state = {"cycles": 0}

        def stop_requested() -> bool:
            return state["cycles"] >= _CYCLE_CAP

        def sleep(delay: float) -> None:
            state["cycles"] += 1
            hook(state)
            sleeps.append(delay)

        daemon.serve(stop_requested, clock=lambda: 100.0, sleep=sleep)
        return sleeps


class ConsentRevokedMidServeRetiresTests(_ServeFixture):
    def test_a_consent_revoked_mid_serve_writes_one_receipt_and_returns(
        self,
    ) -> None:
        """RED (pre-fix): the recovery path re-raises consent_absent and serve
        exits non-zero - relaunch bait under KeepAlive.SuccessfulExit=false."""

        def hook(_state: dict) -> None:
            if _state["cycles"] == 1:
                self.consent.revoke(
                    self.coordinate, idempotency_key="wd2-revoke-mid-serve"
                )

        sleeps = self.serve_bounded(self.daemon(), hook)

        self.assertLessEqual(
            len(sleeps), 3, "daemon kept polling after its consent was revoked"
        )
        retired = _retire_receipts(self.root, self.node, "wake_daemon_consent_revoked")
        self.assertEqual(
            1,
            len(retired),
            f"expected exactly one retire receipt, saw {retired}",
        )
        self.assertEqual("wake_daemon_consent_revoked", retired[0]["reason_code"])

    def test_a_control_transient_cycle_fault_still_keeps_polling(self) -> None:
        """A refusal the daemon CAN clear is not terminal: no retire receipt,
        serve keeps cycling to the cap. Controls the terminal classification."""

        from unittest import mock

        daemon = self.daemon()
        real_cycle = daemon.run_cycle
        calls = {"count": 0}

        def flaky_cycle(now: float) -> dict:
            calls["count"] += 1
            if calls["count"] == 1:
                raise ProtocolRefusal("wake_probe_transient", "synthetic transient")
            return real_cycle(now)

        with mock.patch.object(daemon, "run_cycle", side_effect=flaky_cycle):
            self.serve_bounded(daemon, lambda _state: None)

        self.assertEqual(_CYCLE_CAP, calls["count"])
        self.assertEqual(
            [], _retire_receipts(self.root, self.node, "wake_probe_transient")
        )
        transient = [
            row
            for row in _lifecycle_rows(self.root, self.node)
            if row.get("reason_code") == "wake_probe_transient"
        ]
        self.assertGreaterEqual(len(transient), 1)


class RegistryRetiredMidServeRetiresTests(_ServeFixture):
    def test_b_registry_entry_retired_mid_serve_writes_one_receipt_and_returns(
        self,
    ) -> None:
        """RED (pre-fix): the daemon writes cycle_exception receipts forever
        and never exits on an inactive registry entry."""
        def hook(_state: dict) -> None:
            if _state["cycles"] == 1:
                Registry(self.root).retire(str(self.node))

        sleeps = self.serve_bounded(self.daemon(), hook)

        self.assertLessEqual(
            len(sleeps), 3, "daemon kept polling after its node left the registry"
        )
        retired = _retire_receipts(self.root, self.node, "wake_daemon_registry_inactive")
        self.assertEqual(
            1, len(retired), f"expected exactly one retire receipt, saw {retired}"
        )
        self.assertEqual("wake_daemon_registry_inactive", retired[0]["reason_code"])


class BindingGoneMidServeRetiresTests(_ServeFixture):
    def test_b2_binding_removed_mid_serve_writes_one_receipt_and_returns(
        self,
    ) -> None:
        """RED (pre-fix): cycle_exception receipts forever on a gone binding.

        The fourth ruled terminal kind: P4 lists binding gone among the
        refusals no cycle can clear."""

        def hook(_state: dict) -> None:
            if _state["cycles"] == 1:
                AdapterBindingStore(self.root).remove(self.coordinate)

        sleeps = self.serve_bounded(self.daemon(), hook)

        self.assertLessEqual(
            len(sleeps), 3, "daemon kept polling after its binding was removed"
        )
        retired = _retire_receipts(self.root, self.node, "wake_daemon_binding_gone")
        self.assertEqual(
            1, len(retired), f"expected exactly one retire receipt, saw {retired}"
        )
        self.assertEqual("wake_daemon_binding_gone", retired[0]["reason_code"])


class ActivationEpochMovedMidServeRetiresTests(_ServeFixture):
    def test_c_activation_epoch_moved_mid_serve_writes_one_receipt_and_returns(
        self,
    ) -> None:
        """RED (pre-fix): the daemon silently adopts the moved epoch and keeps
        running as an unconsented-for process."""

        def hook(_state: dict) -> None:
            if _state["cycles"] == 1:
                self.consent.consent(
                    self.coordinate,
                    adapter_version="1",
                    adapter_digest=adapter_contract_digest("cursor"),
                    min_poll_seconds=1,
                    max_poll_seconds=30,
                    max_backoff_seconds=120,
                    activation_epoch=2,
                    idempotency_key="wd2-consent-2",
                )

        sleeps = self.serve_bounded(self.daemon(), hook)

        self.assertLessEqual(
            len(sleeps), 3, "daemon kept running under another daemon's epoch"
        )
        retired = _retire_receipts(self.root, self.node, "wake_daemon_epoch_moved")
        self.assertEqual(
            1, len(retired), f"expected exactly one retire receipt, saw {retired}"
        )


class ServeStartupEpochMismatchTests(_ServeFixture):
    def test_c_startup_epoch_mismatch_returns_ok_with_one_receipt(self) -> None:
        """RED (pre-fix): the serve command refuses non-zero and launchd
        relaunches it every ThrottleInterval seconds (36,722 refusals over
        four days on one seat)."""

        from floati.admin_cli import _wake_daemon_serve

        self.consent.consent(
            self.coordinate,
            adapter_version="1",
            adapter_digest=adapter_contract_digest("cursor"),
            min_poll_seconds=1,
            max_poll_seconds=30,
            max_backoff_seconds=120,
            activation_epoch=2,
            idempotency_key="wd2-consent-2",
        )
        args = argparse.Namespace(
            root=str(self.root.path),
            actor=str(self.node),
            harness="cursor",
            activation_epoch=1,
            zcode_node_executable=None,
            zcode_entry_executable=None,
        )

        status, artifact, exit_code = _wake_daemon_serve(args)

        self.assertEqual("ok", status)
        self.assertEqual(0, exit_code)
        self.assertEqual("wake_daemon_epoch_moved", artifact["reason_code"])
        retired = _retire_receipts(self.root, self.node, "wake_daemon_epoch_moved")
        self.assertEqual(1, len(retired))


class _LaunchctlScript:
    """launchctl runner with a per-print script of (returncode, stdout)."""

    def __init__(self, prints: list[tuple[int, str]]) -> None:
        self.prints = prints
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, argv: tuple[str, ...]) -> subprocess.CompletedProcess[str]:
        self.calls.append(argv)
        if argv[1] == "print":
            returncode, stdout = (
                self.prints.pop(0) if self.prints else (113, "")
            )
            return subprocess.CompletedProcess(argv, returncode, stdout, "")
        return subprocess.CompletedProcess(argv, 0, "", "")


class StopProvesLaunchdTests(_ServeFixture):
    def manager(self, runner, *, pid_alive=None):
        from floati.wake_daemon_launchd import LaunchAgentManager

        launcher = self.base / "installed" / "scripts" / "floati"
        launcher.parent.mkdir(parents=True, exist_ok=True)
        launcher.write_bytes(b"#!/bin/sh\nexit 0\n")
        launcher.chmod(0o700)
        kwargs = {
            "installed_launcher": launcher,
            "launch_agents_directory": self.base / "Library" / "LaunchAgents",
            "uid": 501,
            "runner": runner,
        }
        if pid_alive is not None:
            kwargs["pid_alive"] = pid_alive
        return LaunchAgentManager(self.coordinate, **kwargs)

    def test_d_running_daemon_stop_proves_the_observed_pid_gone(self) -> None:
        manager = self.manager(
            _LaunchctlScript([(0, "PID = 4242\n"), (0, "PID = 4242\n")]),
            pid_alive=lambda pid: False,
        )
        manager.install()

        artifact = manager.stop()

        self.assertEqual("stopped", artifact["state"])
        self.assertIsNone(artifact["reason_code"])
        self.assertEqual(4242, artifact["observation"]["observed_pid"])
        self.assertIs(False, artifact["observation"]["pid_alive"])

    def test_d_stop_unproven_names_what_it_saw_never_unknown(self) -> None:
        manager = self.manager(
            _LaunchctlScript([(0, "PID = 4242\n"), (0, "PID = 4242\n")]),
            pid_alive=lambda pid: True,
        )
        manager.install()

        artifact = manager.stop()

        self.assertEqual("stop_unproven", artifact["state"])
        self.assertEqual("wake_daemon_stop_unproven", artifact["reason_code"])
        self.assertEqual(4242, artifact["observation"]["observed_pid"])
        self.assertIs(True, artifact["observation"]["pid_alive"])
        self.assertNotEqual("unknown", artifact["state"])

    def test_d_control_stop_of_a_daemon_not_running_says_so_by_name(self) -> None:
        manager = self.manager(_LaunchctlScript([(113, "")]))
        manager.install()

        artifact = manager.stop()

        self.assertEqual("stopped", artifact["state"])
        self.assertEqual("wake_daemon_process_absent", artifact["reason_code"])
        self.assertEqual(113, artifact["observation"]["print_returncode"])

    def test_d_unreadable_print_with_dead_pid_still_proves_stopped(self) -> None:
        """The measured 20:45Z incident shape: the observation channel says
        something the old verb could not classify and the daemon was in fact
        gone - the pid proof, not the return-code convention, decides."""

        manager = self.manager(
            _LaunchctlScript([(0, "PID = 4242\n"), (1, "launchctl: error\n")]),
            pid_alive=lambda pid: False,
        )
        manager.install()

        artifact = manager.stop()

        self.assertEqual("stopped", artifact["state"])
        self.assertNotEqual("unknown", artifact["state"])
        self.assertEqual(4242, artifact["observation"]["observed_pid"])


class _SystemctlScript:
    """systemctl runner: scripted is-active results, MainPID for show."""

    def __init__(self, is_active: list[int], main_pid: str = "") -> None:
        self.is_active = list(is_active)
        self.main_pid = main_pid
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, argv: tuple[str, ...]) -> subprocess.CompletedProcess[str]:
        self.calls.append(argv)
        if argv[2] == "is-active":
            returncode = self.is_active.pop(0) if self.is_active else 3
            return subprocess.CompletedProcess(argv, returncode, "", "")
        if argv[2] == "show":
            return subprocess.CompletedProcess(argv, 0, self.main_pid, "")
        return subprocess.CompletedProcess(argv, 0, "", "")


class StopProvesSystemdTests(_ServeFixture):
    def manager(self, runner, *, pid_alive=None):
        from floati.wake_daemon_systemd import SystemdUserUnitManager

        launcher = self.base / "installed" / "scripts" / "floati"
        launcher.parent.mkdir(parents=True, exist_ok=True)
        launcher.write_bytes(b"#!/bin/sh\nexit 0\n")
        launcher.chmod(0o700)
        systemctl = self.base / "host-bin" / "systemctl"
        systemctl.parent.mkdir(parents=True, exist_ok=True)
        systemctl.write_bytes(b"#!/bin/sh\nexit 0\n")
        systemctl.chmod(0o700)
        kwargs = {
            "installed_launcher": launcher,
            "user_units_directory": self.base / "systemd" / "user",
            "runner": runner,
            "systemctl_locator": lambda: str(systemctl),
        }
        if pid_alive is not None:
            kwargs["pid_alive"] = pid_alive
        return SystemdUserUnitManager(self.coordinate, **kwargs)

    def test_d_running_daemon_stop_proves_the_observed_pid_gone_systemd(self) -> None:
        manager = self.manager(
            _SystemctlScript([0, 3], main_pid="4242\n"),
            pid_alive=lambda pid: False,
        )
        manager.install()

        artifact = manager.stop()

        self.assertEqual("stopped", artifact["state"])
        self.assertIsNone(artifact["reason_code"])
        self.assertEqual(4242, artifact["observation"]["observed_pid"])

    def test_d_stop_unproven_names_what_it_saw_never_unknown_systemd(self) -> None:
        manager = self.manager(
            _SystemctlScript([0, 0], main_pid="4242\n"),
            pid_alive=lambda pid: True,
        )
        manager.install()

        artifact = manager.stop()

        self.assertEqual("stop_unproven", artifact["state"])
        self.assertEqual("wake_daemon_stop_unproven", artifact["reason_code"])
        self.assertEqual(4242, artifact["observation"]["observed_pid"])

    def test_d_control_stop_of_a_daemon_not_running_says_so_by_name_systemd(self) -> None:
        manager = self.manager(_SystemctlScript([3]))
        manager.install()

        artifact = manager.stop()

        self.assertEqual("stopped", artifact["state"])
        self.assertEqual("wake_daemon_process_absent", artifact["reason_code"])
        self.assertNotIn("stop", [call[2] for call in runner_calls(manager)])

    def test_d_unreadable_is_active_with_dead_pid_still_proves_stopped(self) -> None:
        manager = self.manager(
            _SystemctlScript([0, 1], main_pid="4242\n"),
            pid_alive=lambda pid: False,
        )
        manager.install()

        artifact = manager.stop()

        self.assertEqual("stopped", artifact["state"])
        self.assertNotEqual("unknown", artifact["state"])
        self.assertEqual(4242, artifact["observation"]["observed_pid"])


def runner_calls(manager):
    return manager._runner.calls  # noqa: SLF001 - test introspects the injected runner


class _SupervisorScript:
    """One fake runner for both supervisors: launchctl prints + systemctl."""

    def __init__(
        self,
        prints: list[tuple[int, str]],
        *,
        is_active: list[int] | None = None,
        main_pid: str = "",
    ) -> None:
        self.prints = prints
        self.is_active = is_active if is_active is not None else [3]
        self.main_pid = main_pid
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, argv: tuple[str, ...]) -> subprocess.CompletedProcess[str]:
        self.calls.append(argv)
        if str(argv[0]).endswith("systemctl"):
            verb = argv[2]
            if verb == "is-active":
                returncode = self.is_active.pop(0) if self.is_active else 3
                return subprocess.CompletedProcess(argv, returncode, "", "")
            if verb == "show":
                return subprocess.CompletedProcess(argv, 0, self.main_pid, "")
            return subprocess.CompletedProcess(argv, 0, "", "")
        if argv[1] == "print":
            returncode, stdout = self.prints.pop(0) if self.prints else (113, "")
            return subprocess.CompletedProcess(argv, returncode, stdout, "")
        return subprocess.CompletedProcess(argv, 0, "", "")


class UninstallRemovesWakeDaemonsTests(unittest.TestCase):
    _DIGEST = "a" * 64

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.destination = self.base / "install"
        self.destination.mkdir()
        self.launch_agents = self.base / "Library" / "LaunchAgents"
        self.user_units = self.base / "systemd" / "user"
        self.launch_agents.mkdir(parents=True)
        self.user_units.mkdir(parents=True)
        self.prints: list[tuple[int, str]] = []
        self.runner = _SupervisorScript(self.prints)
        self.systemctl = self.base / "host-bin" / "systemctl"
        self.systemctl.parent.mkdir(parents=True, exist_ok=True)
        self.systemctl.write_bytes(b"#!/bin/sh\nexit 0\n")
        self.systemctl.chmod(0o700)
        from floati.root import FloatiRoot as _FloatiRoot

        self.root = _FloatiRoot.open_direct_home(self.base / "fleet", create=True)

    def owned_file(self, relative: str, payload: bytes) -> dict[str, str]:
        import hashlib
        import json

        path = self.destination / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        metadata = self.destination / ".floati-install" / "manifest.v0.json"
        entry = {"path": relative, "sha256": hashlib.sha256(payload).hexdigest()}
        payload_manifest = {
            "schema_version": 0,
            "source_ref": "refs/heads/main",
            "source_sha": "a" * 40,
            "files": [entry],
        }
        if metadata.exists():
            seen = json.loads(metadata.read_text(encoding="utf-8"))
            seen["files"].append(entry)
            payload_manifest = seen
        metadata.parent.mkdir(parents=True, exist_ok=True)
        metadata.write_text(
            json.dumps(payload_manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return entry

    def launcher(self) -> Path:
        path = self.base / "host" / "floati"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"#!/bin/sh\nexit 0\n")
        path.chmod(0o700)
        return path

    def install_floati_plist(self) -> Path:
        label = f"com.landoclusters.floati.wake.{self._DIGEST}"
        plist = {
            "Label": label,
            "ProgramArguments": [
                str(self.launcher()),
                "wake",
                "daemon",
                "serve",
                "--root",
                str(self.root.path),
                "--as",
                "worker-a",
                "--harness",
                "cursor",
                "--activation-epoch",
                "1",
            ],
            "KeepAlive": {"SuccessfulExit": False},
        }
        path = self.launch_agents / f"{label}.plist"
        path.write_bytes(plistlib.dumps(plist))
        return path

    def install_foreign_plist(self) -> Path:
        path = self.launch_agents / "com.other.widget.plist"
        path.write_bytes(plistlib.dumps({"Label": "com.other.widget"}))
        return path

    def install_floati_unit(self) -> Path:
        quoted = " ".join(
            f'"{part}"'
            for part in (
                str(self.launcher()),
                "wake",
                "daemon",
                "serve",
                "--root",
                str(self.root.path),
                "--as",
                "worker-a",
                "--harness",
                "cursor",
            )
        )
        unit = (
            "[Unit]\nDescription=floati wake daemon\n\n"
            "[Service]\nExecStart=" + quoted + "\n"
        )
        path = self.user_units / f"floati-wake-{self._DIGEST}.service"
        path.write_text(unit, encoding="utf-8")
        return path

    def install_foreign_unit(self) -> Path:
        path = self.user_units / "operator thing.service"
        path.write_text("[Unit]\nDescription=not floati\n", encoding="utf-8")
        return path

    def writer(self, **overrides):
        from floati.uninstall import UninstallWriter

        kwargs = {
            "wake_sweep": True,
            "launch_agents_directory": self.launch_agents,
            "user_units_directory": self.user_units,
            "supervisor_runner": self.runner,
            "systemctl_locator": lambda: str(self.systemctl),
            "pid_alive": lambda pid: False,
        }
        kwargs.update(overrides)
        return UninstallWriter(self.destination, **kwargs)

    def test_e_uninstall_removes_installed_wake_daemons_with_a_receipt(self) -> None:
        plist = self.install_floati_plist()
        foreign_plist = self.install_foreign_plist()
        unit = self.install_floati_unit()
        self.owned_file("scripts/floati", b"#!/bin/sh\n")

        result = self.writer().run()

        self.assertFalse(plist.exists(), "floati LaunchAgent plist survived uninstall")
        self.assertFalse(unit.exists(), "floati systemd unit survived uninstall")
        self.assertTrue(foreign_plist.exists(), "foreign plist was removed")
        self.assertIn(str(plist), [row["path"] for row in result["wake_daemons_removed"]])
        self.assertIn(str(unit), [row["path"] for row in result["wake_daemons_removed"]])
        self.assertIn(
            str(plist),
            [row["path"] for row in result["wake_daemons_found"]],
            "receipt must name what was seen",
        )

    def test_e_control_no_daemon_says_none(self) -> None:
        self.owned_file("scripts/floati", b"#!/bin/sh\n")

        result = self.writer().run()

        self.assertEqual([], result["wake_daemons_removed"])
        self.assertEqual([], result["wake_daemons_found"])
        self.assertEqual(2, result["removed_count"])

    def test_e_bare_sweep_construction_refuses_typed_never_reaches_home(
        self,
    ) -> None:
        """RED (Am.2): a library construction asking for the sweep without
        naming the directories refuses typed - it never enumerates the
        operator's real LaunchAgents, never reaches Path.home()."""

        self.owned_file("scripts/floati", b"#!/bin/sh\n")

        from floati.uninstall import UninstallWriter

        with self.assertRaisesRegex(
            ProtocolRefusal, "uninstall_wake_sweep_directory_required"
        ):
            UninstallWriter(self.destination, wake_sweep=True)

    def test_e_removal_writes_a_receipt_per_supervisor_without_receipt_dir(
        self,
    ) -> None:
        """RED (Am.2): a deletion with no receipt is the defect. The CLI
        verb always writes one removal receipt per supervisor into the
        daemon's own root state plane, --receipt-dir or not."""

        plist = self.install_floati_plist()
        self.owned_file("scripts/floati", b"#!/bin/sh\n")

        result = self.writer().run()

        self.assertFalse(plist.exists())
        receipt_dir = self.root.resolve_relative(
            Path("state/wake-daemon/uninstall-removed")
        )
        receipts = sorted(receipt_dir.glob("*.json"))
        self.assertEqual(1, len(receipts), receipts)
        payload = json.loads(receipts[0].read_text(encoding="utf-8"))
        self.assertEqual(str(plist), payload["path"])
        self.assertIn("sha256", payload)

    def test_e_uninstall_refuses_a_daemon_it_cannot_prove_stopped(self) -> None:
        plist = self.install_floati_plist()
        self.owned_file("scripts/floati", b"#!/bin/sh\n")
        self.prints.extend(
            [(0, "PID = 4242\n"), (0, "PID = 4242\n"), (0, "PID = 4242\n")]
        )

        with self.assertRaisesRegex(ProtocolRefusal, "stop_unproven"):
            self.writer(pid_alive=lambda pid: True).run()

        self.assertTrue(plist.exists(), "plist of an unproven daemon was removed")
        self.assertTrue(
            (self.destination / "scripts/floati").exists(),
            "destination files were removed though the sweep refused",
        )


if __name__ == "__main__":
    unittest.main()

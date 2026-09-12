"""Digest-bound macOS user LaunchAgent lifecycle for one wake daemon."""

from __future__ import annotations

import hashlib
import os
import plistlib
import stat
import subprocess
from pathlib import Path
from typing import Callable, Dict, Mapping, Optional

from .errors import ProtocolRefusal
from .ids import uuid7_hex
from .jsonl import read_records_snapshot
from .wake_daemon_contract import (
    DAEMON_KINDS,
    AdapterBindingStore,
    DaemonConsentLedger,
    DaemonCoordinate,
    DaemonLifecycleLedger,
)


LaunchctlRunner = Callable[[tuple[str, ...]], subprocess.CompletedProcess[str]]

# AGENTS.md: every refusal names a remedy, and this one shipped with none - a
# reader got "installed LaunchAgent plist does not match the deterministic
# preview" and no action. The cause is almost never drift: the plist's
# ProgramArguments[0] is the ABSOLUTE PATH of the executable that installed it,
# so invoking from a different checkout composes a different plist and a
# different digest. That is a measured incident (MX1-M14), not a hypothesis -
# the reporter nearly filed it as a defect, then re-ran from the installing
# executable and the installed plist's sha256 was byte-identical to the digest
# recorded at install.
#
# The remedy names the plist path itself because the verbs that would print it
# - status, stop, remove - ALL validate first and refuse with this same code.
# Sending the reader to a verb that refuses is not a remedy.
SUPERVISOR_DIGEST_MISMATCH_REMEDY = (
    "the installed LaunchAgent names the absolute path of the executable that "
    "installed it: read ProgramArguments[0] in {path} and run this verb from "
    "that checkout, or install the one you want with 'floati wake daemon "
    "install --root {root} --as {node} --harness {harness}'"
)


def _default_runner(argv: tuple[str, ...]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(argv), capture_output=True, text=True, check=False, timeout=10
    )


def _default_pid_alive(pid: int) -> bool:
    """True while the process exists; ESRCH is the only proven absence."""

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # The process exists; this user merely cannot signal it.
        return True
    return True


def _launchctl_print_pid(stdout: str) -> Optional[int]:
    """The one process fact `launchctl print` states: `PID = <n>`."""

    import re

    found = re.search(r"^\s*PID = (\d+)\s*$", stdout, re.MULTILINE)
    return None if found is None else int(found.group(1))


class LaunchAgentManager:
    """Install and control one exact user-domain plist without implicit activation."""

    def __init__(
        self,
        coordinate: DaemonCoordinate,
        *,
        installed_launcher: Path,
        launch_agents_directory: Optional[Path] = None,
        uid: Optional[int] = None,
        runner: Optional[LaunchctlRunner] = None,
        pid_alive: Optional[Callable[[int], bool]] = None,
    ) -> None:
        if not isinstance(coordinate, DaemonCoordinate):
            raise ProtocolRefusal(
                "wake_daemon_coordinate_invalid", "LaunchAgent requires one coordinate"
            )
        self.coordinate = coordinate
        self.root = coordinate.root
        self.launcher = self._launcher(installed_launcher)
        directory = (
            Path.home() / "Library" / "LaunchAgents"
            if launch_agents_directory is None
            else Path(launch_agents_directory)
        )
        if (
            not directory.is_absolute()
            or directory.is_symlink()
            or directory.resolve(strict=False) != directory
        ):
            raise ProtocolRefusal(
                "wake_daemon_launch_agents_invalid",
                "LaunchAgents directory must be an absolute nonsymlink path",
            )
        self.launch_agents_directory = directory
        selected_uid = os.getuid() if uid is None else uid
        if (
            not isinstance(selected_uid, int)
            or isinstance(selected_uid, bool)
            or selected_uid < 0
        ):
            raise ProtocolRefusal(
                "wake_daemon_uid_invalid", "LaunchAgent uid is invalid"
            )
        self.uid = selected_uid
        self.label = "com.landoclusters.floati.wake." + coordinate.digest
        self.plist_path = directory / f"{self.label}.plist"
        self._runner = _default_runner if runner is None else runner
        self._pid_alive = _default_pid_alive if pid_alive is None else pid_alive
        self.consent = DaemonConsentLedger(self.root)
        self.lifecycle = DaemonLifecycleLedger(self.root)
        self.daemon_instance_id = "launchd-" + coordinate.digest[:32]

    def digest_mismatch_remedy(self) -> str:
        """Name an action for every wake_daemon_supervisor_digest_mismatch."""

        return SUPERVISOR_DIGEST_MISMATCH_REMEDY.format(
            path=self.plist_path,
            root=self.root.path,
            node=self.coordinate.node_id,
            harness=self.coordinate.harness,
        )

    def preview(self) -> Dict[str, object]:
        consent = self.consent.require_active(self.coordinate)
        binding = AdapterBindingStore(self.root).read(self.coordinate)
        if (
            binding["adapter_version"] != consent["adapter_version"]
            or binding["adapter_digest"] != consent["adapter_digest"]
        ):
            raise ProtocolRefusal(
                "wake_daemon_adapter_digest_mismatch",
                "LaunchAgent consent and adapter binding disagree",
            )
        logs = self.root.resolve_relative(
            Path("state/wake-daemon/logs") / self.coordinate.digest
        )
        plist: Dict[str, object] = {
            "Label": self.label,
            "ProgramArguments": [
                str(self.launcher),
                "wake",
                "daemon",
                "serve",
                "--root",
                str(self.root.path),
                "--as",
                self.coordinate.node_id,
                "--harness",
                self.coordinate.harness,
                "--activation-epoch",
                str(consent["activation_epoch"]),
            ],
            "RunAtLoad": False,
            "KeepAlive": {"SuccessfulExit": False},
            "ProcessType": "Background",
            "ThrottleInterval": 10,
            "StandardOutPath": str(logs.with_suffix(".stdout.log")),
            "StandardErrorPath": str(logs.with_suffix(".stderr.log")),
        }
        encoded = plistlib.dumps(plist, fmt=plistlib.FMT_XML, sort_keys=True)
        return {
            "schema_version": 0,
            "state": "preview",
            "label": self.label,
            "plist_path": str(self.plist_path),
            "plist": plist,
            "encoded": encoded,
            "plist_digest": hashlib.sha256(encoded).hexdigest(),
            "consent": consent,
            "binding": binding,
        }

    def install(self) -> Dict[str, object]:
        preview = self.preview()
        encoded = preview["encoded"]
        if not isinstance(encoded, bytes):
            raise ProtocolRefusal(
                "wake_daemon_supervisor_preview_invalid",
                "deterministic LaunchAgent bytes are unavailable",
            )
        if self.plist_path.exists() or self.plist_path.is_symlink():
            self._validate_installed(preview)
        else:
            self.launch_agents_directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            log_directory = self.root.resolve_relative("state/wake-daemon/logs")
            log_directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            temporary = self.plist_path.with_name(
                f".{self.plist_path.name}.{os.getpid()}.{uuid7_hex()}.tmp"
            )
            descriptor = -1
            try:
                descriptor = os.open(
                    temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
                )
                if os.write(descriptor, encoded) != len(encoded):
                    raise OSError("short plist write")
                os.fsync(descriptor)
                os.close(descriptor)
                descriptor = -1
                os.link(temporary, self.plist_path)
                temporary.unlink()
            except OSError as exc:
                if descriptor >= 0:
                    os.close(descriptor)
                temporary.unlink(missing_ok=True)
                raise ProtocolRefusal(
                    "wake_daemon_supervisor_install_failed",
                    "LaunchAgent plist could not be installed",
                ) from exc
            self._validate_installed(preview)
        receipt = self._record(
            preview,
            event="installed",
            state="installed",
            reason_code=None,
        )
        return self._artifact(preview, "installed", None, receipt)

    def start(self) -> Dict[str, object]:
        preview = self.preview()
        self._validate_installed(preview)
        bootstrap = self._launchctl(
            ("/bin/launchctl", "bootstrap", self.domain, str(self.plist_path))
        )
        if bootstrap.returncode != 0:
            raise ProtocolRefusal(
                "wake_daemon_supervisor_start_failed",
                "launchctl bootstrap refused the exact user LaunchAgent",
            )
        kickstart = self._launchctl(
            ("/bin/launchctl", "kickstart", f"{self.domain}/{self.label}")
        )
        if kickstart.returncode != 0:
            raise ProtocolRefusal(
                "wake_daemon_supervisor_start_failed",
                "launchctl kickstart refused the exact user LaunchAgent",
            )
        receipt = self._record(
            preview, event="started", state="running", reason_code=None
        )
        return self._artifact(preview, "running", None, receipt)

    def status(self) -> Dict[str, object]:
        preview = self.preview()
        self._validate_installed(preview)
        observed = self._launchctl(
            ("/bin/launchctl", "print", f"{self.domain}/{self.label}")
        )
        if observed.returncode == 0:
            return self._artifact(preview, "running", None, None)
        if observed.returncode == 113:
            return self._artifact(
                preview, "stopped", "wake_daemon_process_absent", None
            )
        return self._artifact(
            preview, "unknown", "wake_daemon_process_unknown", None
        )

    def stop(self) -> Dict[str, object]:
        # WD-2 (d): a stop verb proves its work. The 2026-09-10 dispatch
        # measured this verb reporting `unknown` under `ok` for four daemons,
        # two of which it had just stopped and two of which were still
        # running - return-code conventions cannot tell those apart, an
        # observed pid can. The contract: `stopped` with the observed pid
        # gone, `stop_unproven` naming exactly what it saw, never `unknown`
        # under a clean exit; and a stop of a daemon that is not running
        # says so by name.
        preview = self.preview()
        self._validate_installed(preview)
        observed = self._launchctl(
            ("/bin/launchctl", "print", f"{self.domain}/{self.label}")
        )
        if observed.returncode != 0:
            receipt = self._record(
                preview,
                event="stopped",
                state="stopped",
                reason_code="wake_daemon_process_absent",
            )
            return self._artifact(
                preview,
                "stopped",
                "wake_daemon_process_absent",
                receipt,
                observation={"print_returncode": observed.returncode},
            )
        observed_pid = _launchctl_print_pid(str(observed.stdout))
        self._launchctl(
            ("/bin/launchctl", "bootout", f"{self.domain}/{self.label}")
        )
        after = self._launchctl(
            ("/bin/launchctl", "print", f"{self.domain}/{self.label}")
        )
        after_pid = (
            _launchctl_print_pid(str(after.stdout))
            if after.returncode == 0
            else None
        )
        post_alive = (
            after_pid is not None and bool(self._pid_alive(int(after_pid)))
        )
        observation = {
            "observed_pid": observed_pid,
            "post_print_returncode": after.returncode,
            "post_observed_pid": after_pid,
            "pid_alive": None if after_pid is None else bool(post_alive),
        }
        if post_alive:
            state = "stop_unproven"
            reason: Optional[str] = "wake_daemon_stop_unproven"
            receipt = self._record(
                preview, event="owner_unknown", state="unknown", reason_code=reason
            )
        else:
            state = "stopped"
            reason = None if observed_pid is not None else "wake_daemon_process_absent"
            receipt = self._record(
                preview, event="stopped", state="stopped", reason_code=reason
            )
        return self._artifact(preview, state, reason, receipt, observation=observation)

    def remove(self, *, expected_plist_digest: Optional[str] = None) -> Dict[str, object]:
        preview = self.preview()
        expected = (
            str(preview["plist_digest"])
            if expected_plist_digest is None
            else expected_plist_digest
        )
        return self._remove_preview(preview, expected)

    def revoke(self, *, idempotency_key: str) -> Dict[str, object]:
        consent = self.consent.require_active(self.coordinate)
        try:
            AdapterBindingStore(self.root).read(self.coordinate)
        except ProtocolRefusal as exc:
            if exc.code != "wake_daemon_binding_absent":
                raise
            reason = (
                "wake_daemon_unbound_plist_preserved"
                if self.plist_path.exists() or self.plist_path.is_symlink()
                else None
            )
            revoked = self.consent.revoke(
                self.coordinate, idempotency_key=idempotency_key
            )
            prior = self._last_lifecycle()
            receipt = self.lifecycle.record(
                self.coordinate,
                daemon_instance_id=self.daemon_instance_id,
                activation_epoch=int(consent["activation_epoch"]),
                event="revoked",
                state="revoked",
                reason_code=reason,
                adapter_digest=str(consent["adapter_digest"]),
                plist_digest=None,
                session_digest=None,
                predecessor_receipt_id=None if prior is None else str(prior["id"]),
                idempotency_key="launchd-revoked-" + uuid7_hex(),
            )
            return {
                "schema_version": 0,
                "state": "revoked",
                "reason_code": reason,
                "label": self.label,
                "plist_path": str(self.plist_path),
                "plist_digest": None,
                "receipt": receipt,
                "consent_receipt": revoked,
            }
        preview = self.preview()
        process_state = "unknown"
        process_reason: Optional[str] = "wake_daemon_process_unknown"
        if self.plist_path.exists() or self.plist_path.is_symlink():
            stopped = self.stop()
            process_state = str(stopped["state"])
            process_reason = stopped["reason_code"]
            self._remove_preview(preview, str(preview["plist_digest"]))
        self.consent.revoke(self.coordinate, idempotency_key=idempotency_key)
        final_state = "revoked" if process_state == "stopped" else "unknown"
        receipt = self._record(
            preview,
            event="revoked",
            state=final_state,
            reason_code=None if final_state == "revoked" else process_reason,
        )
        return self._artifact(
            preview,
            final_state,
            None if final_state == "revoked" else process_reason,
            receipt,
        )

    @property
    def domain(self) -> str:
        return f"gui/{self.uid}"

    def _remove_preview(
        self, preview: Mapping[str, object], expected_digest: str
    ) -> Dict[str, object]:
        if expected_digest != preview["plist_digest"]:
            raise ProtocolRefusal(
                "wake_daemon_supervisor_digest_mismatch",
                "requested removal digest differs from the deterministic plist",
                remedy=self.digest_mismatch_remedy(),
            )
        encoded, identity = self._read_installed()
        if hashlib.sha256(encoded).hexdigest() != expected_digest:
            raise ProtocolRefusal(
                "wake_daemon_supervisor_digest_mismatch",
                "installed LaunchAgent plist differs from the expected digest",
                remedy=self.digest_mismatch_remedy(),
            )
        quarantine = self.plist_path.with_name(
            f".{self.plist_path.name}.{uuid7_hex()}.remove"
        )
        try:
            os.replace(self.plist_path, quarantine)
            moved = os.lstat(quarantine)
            if (moved.st_dev, moved.st_ino) != identity:
                raise OSError("plist identity changed before quarantine")
            if hashlib.sha256(quarantine.read_bytes()).hexdigest() != expected_digest:
                raise OSError("plist digest changed in quarantine")
            quarantine.unlink()
        except OSError as exc:
            if quarantine.exists() and not self.plist_path.exists():
                try:
                    os.replace(quarantine, self.plist_path)
                except OSError:
                    pass
            raise ProtocolRefusal(
                "wake_daemon_supervisor_remove_failed",
                "exact LaunchAgent plist could not be safely removed",
            ) from exc
        receipt = self._record(
            preview, event="removed", state="removed", reason_code=None
        )
        return self._artifact(preview, "removed", None, receipt)

    def _validate_installed(self, preview: Mapping[str, object]) -> None:
        if not self.plist_path.exists() and not self.plist_path.is_symlink():
            raise ProtocolRefusal(
                "wake_daemon_supervisor_missing",
                "the exact user LaunchAgent plist is not installed",
            )
        encoded, _identity = self._read_installed()
        if (
            encoded != preview["encoded"]
            or hashlib.sha256(encoded).hexdigest() != preview["plist_digest"]
        ):
            raise ProtocolRefusal(
                "wake_daemon_supervisor_digest_mismatch",
                "installed LaunchAgent plist does not match the deterministic preview",
                remedy=self.digest_mismatch_remedy(),
            )

    def _read_installed(self) -> tuple[bytes, tuple[int, int]]:
        if self.plist_path.is_symlink() or not self.plist_path.is_file():
            raise ProtocolRefusal(
                "wake_daemon_supervisor_digest_mismatch",
                "LaunchAgent plist is absent, symlinked, or not a regular file",
                remedy=self.digest_mismatch_remedy(),
            )
        descriptor = -1
        try:
            descriptor = os.open(
                self.plist_path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            )
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode):
                raise OSError("not regular")
            encoded = os.read(descriptor, 65537)
            if len(encoded) > 65536:
                raise OSError("oversized")
        except OSError as exc:
            raise ProtocolRefusal(
                "wake_daemon_supervisor_digest_mismatch",
                "LaunchAgent plist could not be read exactly",
                remedy=self.digest_mismatch_remedy(),
            ) from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        return encoded, (info.st_dev, info.st_ino)

    def _launchctl(self, argv: tuple[str, ...]) -> subprocess.CompletedProcess[str]:
        target = f"{self.domain}/{self.label}"
        valid = (
            argv == ("/bin/launchctl", "bootstrap", self.domain, str(self.plist_path))
            or argv == ("/bin/launchctl", "kickstart", target)
            or argv == ("/bin/launchctl", "bootout", target)
            or argv == ("/bin/launchctl", "print", target)
        )
        if not valid:
            raise ProtocolRefusal(
                "wake_daemon_launchctl_vector_invalid",
                "launchctl invocation is outside the fixed user-domain surface",
            )
        try:
            return self._runner(argv)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ProtocolRefusal(
                "wake_daemon_launchctl_unavailable", "launchctl result is unknown"
            ) from exc

    def _record(
        self,
        preview: Mapping[str, object],
        *,
        event: str,
        state: str,
        reason_code: Optional[str],
    ) -> Dict[str, object]:
        binding = preview["binding"]
        consent = preview["consent"]
        if not isinstance(binding, dict) or not isinstance(consent, dict):
            raise ProtocolRefusal(
                "wake_daemon_supervisor_preview_invalid",
                "LaunchAgent preview lacks binding or consent testimony",
            )
        prior = self._last_lifecycle()
        return self.lifecycle.record(
            self.coordinate,
            daemon_instance_id=self.daemon_instance_id,
            activation_epoch=int(consent["activation_epoch"]),
            event=event,
            state=state,
            reason_code=reason_code,
            adapter_digest=str(binding["adapter_digest"]),
            plist_digest=str(preview["plist_digest"]),
            session_digest=str(binding["session_digest"]),
            predecessor_receipt_id=None if prior is None else str(prior["id"]),
            idempotency_key=f"launchd-{event}-{uuid7_hex()}",
        )

    def _last_lifecycle(self) -> Optional[Dict[str, object]]:
        rows = read_records_snapshot(
            self.root,
            Path("receipts/wake-daemon") / f"{self.coordinate.node_id}.jsonl",
            allowed_kinds=DAEMON_KINDS,
        )
        matching = [
            row
            for row in rows
            if row.get("kind") == "wake_daemon_lifecycle_receipt"
            and row.get("coordinate_digest") == self.coordinate.digest
        ]
        return None if not matching else matching[-1]

    @staticmethod
    def _artifact(
        preview: Mapping[str, object],
        state: str,
        reason_code: Optional[str],
        receipt: Optional[Mapping[str, object]],
        observation: Optional[Mapping[str, object]] = None,
    ) -> Dict[str, object]:
        artifact = {
            "schema_version": 0,
            "state": state,
            "reason_code": reason_code,
            "label": preview["label"],
            "plist_path": preview["plist_path"],
            "plist_digest": preview["plist_digest"],
            "receipt": None if receipt is None else dict(receipt),
        }
        if observation is not None:
            artifact["observation"] = dict(observation)
        return artifact

    @staticmethod
    def _launcher(value: Path) -> Path:
        path = Path(value).expanduser()
        if (
            not path.is_absolute()
            or path.is_symlink()
            or not path.is_file()
            or path.stat().st_mode & 0o111 == 0
        ):
            raise ProtocolRefusal(
                "wake_daemon_launcher_invalid",
                "installed launcher must be an absolute ordinary executable",
            )
        return path.resolve(strict=True)

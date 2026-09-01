"""Digest-bound systemd user-unit lifecycle for one wake daemon."""

from __future__ import annotations

import hashlib
import os
import stat
import subprocess
import sys
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


SystemctlRunner = Callable[[tuple[str, ...]], subprocess.CompletedProcess[str]]
SystemctlLocator = Callable[[], Optional[str]]
SYSTEMCTL_CANDIDATES = ("/usr/bin/systemctl", "/bin/systemctl")


def _default_runner(argv: tuple[str, ...]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(argv), capture_output=True, text=True, check=False, timeout=10
    )


def _default_systemctl_locator() -> Optional[str]:
    for candidate in SYSTEMCTL_CANDIDATES:
        path = Path(candidate)
        if path.exists() or path.is_symlink():
            return candidate
    return None


def _quote_exec_start(parts: list[str]) -> str:
    quoted = []
    for part in parts:
        escaped = (
            part.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$")
        )
        quoted.append(f'"{escaped}"')
    return " ".join(quoted)


class SystemdUserUnitManager:
    """Install and control one exact systemd user unit without implicit activation."""

    def __init__(
        self,
        coordinate: DaemonCoordinate,
        *,
        installed_launcher: Path,
        user_units_directory: Optional[Path] = None,
        runner: Optional[SystemctlRunner] = None,
        systemctl_locator: Optional[SystemctlLocator] = None,
    ) -> None:
        if not isinstance(coordinate, DaemonCoordinate):
            raise ProtocolRefusal(
                "wake_daemon_coordinate_invalid",
                "systemd user unit requires one coordinate",
            )
        self.coordinate = coordinate
        self.root = coordinate.root
        self.launcher = self._launcher(installed_launcher)
        directory = (
            Path.home() / ".config" / "systemd" / "user"
            if user_units_directory is None
            else Path(user_units_directory)
        )
        if (
            not directory.is_absolute()
            or directory.is_symlink()
            or directory.resolve(strict=False) != directory
        ):
            raise ProtocolRefusal(
                "wake_daemon_user_units_invalid",
                "systemd user units directory must be an absolute nonsymlink path",
            )
        self.user_units_directory = directory
        self.unit_name = f"floati-wake-{coordinate.digest}.service"
        self.unit_path = directory / self.unit_name
        self.label = self.unit_name
        self.plist_path = self.unit_path
        self._injected_runner = runner is not None
        self._runner = _default_runner if runner is None else runner
        self._systemctl_locator = (
            _default_systemctl_locator
            if systemctl_locator is None
            else systemctl_locator
        )
        self._systemctl_executable: Optional[str] = None
        self.consent = DaemonConsentLedger(self.root)
        self.lifecycle = DaemonLifecycleLedger(self.root)
        self.daemon_instance_id = "systemd-" + coordinate.digest[:32]

    def preview(self) -> Dict[str, object]:
        consent = self.consent.require_active(self.coordinate)
        binding = AdapterBindingStore(self.root).read(self.coordinate)
        if (
            binding["adapter_version"] != consent["adapter_version"]
            or binding["adapter_digest"] != consent["adapter_digest"]
        ):
            raise ProtocolRefusal(
                "wake_daemon_adapter_digest_mismatch",
                "systemd user unit consent and adapter binding disagree",
            )
        logs = self.root.resolve_relative(
            Path("state/wake-daemon/logs") / self.coordinate.digest
        )
        program_arguments = [
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
        ]
        stdout = str(logs.with_suffix(".stdout.log"))
        stderr = str(logs.with_suffix(".stderr.log"))
        encoded = "\n".join(
            (
                "[Unit]",
                f"Description=floati wake daemon {self.coordinate.digest}",
                "",
                "[Service]",
                "Type=simple",
                f"ExecStart={_quote_exec_start(program_arguments)}",
                "Restart=on-failure",
                "RestartSec=10",
                f"StandardOutput=append:{stdout}",
                f"StandardError=append:{stderr}",
                "",
                "[Install]",
                "WantedBy=default.target",
                "",
            )
        ).encode("utf-8")
        return {
            "schema_version": 0,
            "state": "preview",
            "label": self.label,
            "plist_path": str(self.unit_path),
            "plist_digest": hashlib.sha256(encoded).hexdigest(),
            "unit_name": self.unit_name,
            "unit_path": str(self.unit_path),
            "program_arguments": program_arguments,
            "encoded": encoded,
            "consent": consent,
            "binding": binding,
        }

    def install(self) -> Dict[str, object]:
        preview = self.preview()
        encoded = preview["encoded"]
        if not isinstance(encoded, bytes):
            raise ProtocolRefusal(
                "wake_daemon_supervisor_preview_invalid",
                "deterministic systemd user unit bytes are unavailable",
            )
        if self.unit_path.exists() or self.unit_path.is_symlink():
            self._validate_installed(preview)
        else:
            self.user_units_directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            log_directory = self.root.resolve_relative("state/wake-daemon/logs")
            log_directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            temporary = self.unit_path.with_name(
                f".{self.unit_path.name}.{os.getpid()}.{uuid7_hex()}.tmp"
            )
            descriptor = -1
            try:
                descriptor = os.open(
                    temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
                )
                if os.write(descriptor, encoded) != len(encoded):
                    raise OSError("short unit write")
                os.fsync(descriptor)
                os.close(descriptor)
                descriptor = -1
                os.link(temporary, self.unit_path)
                temporary.unlink()
            except OSError as exc:
                if descriptor >= 0:
                    os.close(descriptor)
                temporary.unlink(missing_ok=True)
                raise ProtocolRefusal(
                    "wake_daemon_supervisor_install_failed",
                    "systemd user unit could not be installed",
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
        executable = self._resolve_systemctl_executable()
        reload = self._systemctl((executable, "--user", "daemon-reload"))
        if reload.returncode != 0:
            raise ProtocolRefusal(
                "wake_daemon_supervisor_start_failed",
                "systemctl daemon-reload refused the exact user unit",
            )
        started = self._systemctl(
            (executable, "--user", "start", self.unit_name)
        )
        if started.returncode != 0:
            raise ProtocolRefusal(
                "wake_daemon_supervisor_start_failed",
                "systemctl start refused the exact user unit",
            )
        receipt = self._record(
            preview, event="started", state="running", reason_code=None
        )
        return self._artifact(preview, "running", None, receipt)

    def status(self) -> Dict[str, object]:
        preview = self.preview()
        self._validate_installed(preview)
        executable = self._resolve_systemctl_executable()
        observed = self._systemctl(
            (executable, "--user", "is-active", self.unit_name)
        )
        if observed.returncode == 0:
            return self._artifact(preview, "running", None, None)
        if observed.returncode == 3:
            return self._artifact(
                preview, "stopped", "wake_daemon_process_absent", None
            )
        return self._artifact(
            preview, "unknown", "wake_daemon_process_unknown", None
        )

    def stop(self) -> Dict[str, object]:
        preview = self.preview()
        self._validate_installed(preview)
        executable = self._resolve_systemctl_executable()
        self._systemctl((executable, "--user", "stop", self.unit_name))
        observed = self._systemctl(
            (executable, "--user", "is-active", self.unit_name)
        )
        proven = observed.returncode == 3
        state = "stopped" if proven else "unknown"
        reason = None if proven else "wake_daemon_process_unknown"
        receipt = self._record(
            preview,
            event="stopped" if proven else "owner_unknown",
            state=state,
            reason_code=reason,
        )
        return self._artifact(preview, state, reason, receipt)

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
                if self.unit_path.exists() or self.unit_path.is_symlink()
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
                idempotency_key="systemd-revoked-" + uuid7_hex(),
            )
            return {
                "schema_version": 0,
                "state": "revoked",
                "reason_code": reason,
                "label": self.label,
                "plist_path": str(self.unit_path),
                "plist_digest": None,
                "receipt": receipt,
                "consent_receipt": revoked,
            }
        preview = self.preview()
        process_state = "unknown"
        process_reason: Optional[str] = "wake_daemon_process_unknown"
        if self.unit_path.exists() or self.unit_path.is_symlink():
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

    def _remove_preview(
        self, preview: Mapping[str, object], expected_digest: str
    ) -> Dict[str, object]:
        if expected_digest != preview["plist_digest"]:
            raise ProtocolRefusal(
                "wake_daemon_supervisor_digest_mismatch",
                "requested removal digest differs from the deterministic unit",
            )
        encoded, identity = self._read_installed()
        if hashlib.sha256(encoded).hexdigest() != expected_digest:
            raise ProtocolRefusal(
                "wake_daemon_supervisor_digest_mismatch",
                "installed systemd user unit differs from the expected digest",
            )
        quarantine = self.unit_path.with_name(
            f".{self.unit_path.name}.{uuid7_hex()}.remove"
        )
        try:
            os.replace(self.unit_path, quarantine)
            moved = os.lstat(quarantine)
            if (moved.st_dev, moved.st_ino) != identity:
                raise OSError("unit identity changed before quarantine")
            if hashlib.sha256(quarantine.read_bytes()).hexdigest() != expected_digest:
                raise OSError("unit digest changed in quarantine")
            quarantine.unlink()
        except OSError as exc:
            if quarantine.exists() and not self.unit_path.exists():
                try:
                    os.replace(quarantine, self.unit_path)
                except OSError:
                    pass
            raise ProtocolRefusal(
                "wake_daemon_supervisor_remove_failed",
                "exact systemd user unit could not be safely removed",
            ) from exc
        receipt = self._record(
            preview, event="removed", state="removed", reason_code=None
        )
        return self._artifact(preview, "removed", None, receipt)

    def _validate_installed(self, preview: Mapping[str, object]) -> None:
        if not self.unit_path.exists() and not self.unit_path.is_symlink():
            raise ProtocolRefusal(
                "wake_daemon_supervisor_missing",
                "the exact systemd user unit is not installed",
            )
        encoded, _identity = self._read_installed()
        if (
            encoded != preview["encoded"]
            or hashlib.sha256(encoded).hexdigest() != preview["plist_digest"]
        ):
            raise ProtocolRefusal(
                "wake_daemon_supervisor_digest_mismatch",
                "installed systemd user unit does not match the deterministic preview",
            )

    def _read_installed(self) -> tuple[bytes, tuple[int, int]]:
        if self.unit_path.is_symlink() or not self.unit_path.is_file():
            raise ProtocolRefusal(
                "wake_daemon_supervisor_digest_mismatch",
                "systemd user unit is absent, symlinked, or not a regular file",
            )
        descriptor = -1
        try:
            descriptor = os.open(
                self.unit_path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
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
                "systemd user unit could not be read exactly",
            ) from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        return encoded, (info.st_dev, info.st_ino)

    def _systemctl(self, argv: tuple[str, ...]) -> subprocess.CompletedProcess[str]:
        executable = self._resolve_systemctl_executable()
        unit = self.unit_name
        valid = (
            argv == (executable, "--user", "daemon-reload")
            or argv == (executable, "--user", "start", unit)
            or argv == (executable, "--user", "stop", unit)
            or argv == (executable, "--user", "is-active", unit)
        )
        if not valid:
            raise ProtocolRefusal(
                "wake_daemon_systemctl_vector_invalid",
                "systemctl invocation is outside the fixed user-unit surface",
            )
        try:
            return self._runner(argv)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ProtocolRefusal(
                "wake_daemon_systemctl_unavailable", "systemctl result is unknown"
            ) from exc

    def _resolve_systemctl_executable(self) -> str:
        if not self._injected_runner and not sys.platform.startswith("linux"):
            raise ProtocolRefusal(
                "wake_daemon_systemd_unmeasurable",
                "systemd user units are not measurable on this platform",
            )
        if self._systemctl_executable is not None:
            return self._systemctl_executable
        try:
            candidate = self._systemctl_locator()
        except OSError as exc:
            raise ProtocolRefusal(
                "wake_daemon_systemctl_unavailable",
                "systemctl executable could not be resolved from PATH",
            ) from exc
        if candidate is None:
            detail = (
                "systemctl executable is absent from fixed candidates: "
                + ", ".join(SYSTEMCTL_CANDIDATES)
                if self._systemctl_locator is _default_systemctl_locator
                else "operator-declared systemctl executable is absent"
            )
            raise ProtocolRefusal(
                "wake_daemon_systemctl_unavailable",
                detail,
            )
        path = Path(candidate).expanduser()
        try:
            if not path.is_absolute():
                raise OSError("systemctl path is not absolute")
            resolved = path.resolve(strict=True)
            metadata = resolved.stat()
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o111 == 0:
                raise OSError("systemctl path is not an ordinary executable")
        except (OSError, RuntimeError) as exc:
            raise ProtocolRefusal(
                "wake_daemon_systemctl_unavailable",
                "resolved systemctl path is not an absolute ordinary executable",
            ) from exc
        self._systemctl_executable = str(resolved)
        return self._systemctl_executable

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
                "systemd user unit preview lacks binding or consent testimony",
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
            idempotency_key=f"systemd-{event}-{uuid7_hex()}",
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
    ) -> Dict[str, object]:
        return {
            "schema_version": 0,
            "state": state,
            "reason_code": reason_code,
            "label": preview["label"],
            "plist_path": preview["plist_path"],
            "plist_digest": preview["plist_digest"],
            "receipt": None if receipt is None else dict(receipt),
        }

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

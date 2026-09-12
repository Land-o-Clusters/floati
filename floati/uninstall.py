"""Manifest-exact, data-retaining removal of an installed Floati bundle.

HOME-1: a removal is a durable action and leaves a durable receipt —
but only where the operator declares one (``--receipt-dir DIR``,
absolute). The default writes no file anywhere, least of all bare
``$HOME``: the U2 tombstone writer (``Path.home() /
"floati-uninstalled-<ts>.json"``, commit 9cb87558) is the measured
incident this contract never repeats.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import plistlib
import re
import secrets
import stat
import subprocess
import time
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from .errors import DurabilityFailure, ProtocolRefusal
from .storage_identity import INSTALL_METADATA_DIRECTORY
from .update_ownership import validate_install_ownership
from .wake_daemon_launchd import _default_pid_alive, _launchctl_print_pid
from .wake_daemon_systemd import SYSTEMCTL_CANDIDATES


_METADATA_RELATIVE = PurePosixPath(INSTALL_METADATA_DIRECTORY, "manifest.v0.json")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SOURCE_SHA = re.compile(r"^[0-9a-f]{40}$")
_DATA_NOTICE = (
    "Bus roots and ledgers are retained; uninstall removes tool files only."
)

# WD-2 (e): uninstall owns the wake daemons it installed. The 2026-09-10
# dispatch measured `floati uninstall` with zero wake-daemon handling: a
# tool that leaves background processes behind after its own removal breaks
# the promise a daemon is. The sweep removes only files that identify as
# floati wake supervisors by NAME and by CONTENT (label/unit pattern plus
# the wake-serve command line), proves the process gone first, and retains
# every bus root, ledger, and state file - only the supervisor bytes go.
_WAKE_LAUNCHD_PLIST = re.compile(
    r"^com\.landoclusters\.floati\.wake\.[0-9a-f]{64}\.plist$"
)
_WAKE_SYSTEMD_UNIT = re.compile(r"^floati-wake-[0-9a-f]{64}\.service$")
_WAKE_SERVE_TOKENS = ("wake", "daemon", "serve")
def _default_supervisor_runner(
    argv: tuple[str, ...]
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(argv), capture_output=True, text=True, check=False, timeout=10
    )


def _default_systemctl_locator() -> Optional[str]:
    for candidate in SYSTEMCTL_CANDIDATES:
        path = Path(candidate)
        if path.exists() or path.is_symlink():
            return candidate
    return None


def _names_wake_serve(tokens: Sequence[str]) -> bool:
    for start in range(0, max(0, len(tokens) - 2)):
        if tuple(tokens[start : start + 3]) == _WAKE_SERVE_TOKENS:
            return True
    return False


def _discover_wake_supervisors(
    launch_agents_directory: Path, user_units_directory: Path
) -> List[Dict[str, Any]]:
    """Name floati wake supervisor files by exact identity, nothing else."""

    found: List[Dict[str, Any]] = []
    if launch_agents_directory.is_dir():
        for path in sorted(launch_agents_directory.iterdir()):
            if path.is_symlink() or not path.is_file():
                continue
            if _WAKE_LAUNCHD_PLIST.fullmatch(path.name) is None:
                continue
            try:
                with path.open("rb") as handle:
                    plist = plistlib.load(handle)
            except (OSError, plistlib.InvalidFileException, ValueError):
                found.append(
                    {
                        "supervisor": "launchd",
                        "path": str(path),
                        "label": None,
                        "identified": False,
                    }
                )
                continue
            arguments = (
                plist.get("ProgramArguments") if isinstance(plist, dict) else None
            )
            label_value = plist.get("Label") if isinstance(plist, dict) else None
            tokens = [
                str(token) for token in arguments
            ] if isinstance(arguments, list) else []
            if label_value != path.stem or not _names_wake_serve(tokens):
                continue
            facts = _serve_facts_from_tokens(tokens)
            if facts is None:
                found.append(
                    {
                        "supervisor": "launchd",
                        "path": str(path),
                        "label": None,
                        "identified": False,
                    }
                )
                continue
            found.append(
                {
                    "supervisor": "launchd",
                    "path": str(path),
                    "label": str(path.stem),
                    "identified": True,
                    **facts,
                }
            )
    if user_units_directory.is_dir():
        for path in sorted(user_units_directory.iterdir()):
            if path.is_symlink() or not path.is_file():
                continue
            if _WAKE_SYSTEMD_UNIT.fullmatch(path.name) is None:
                continue
            try:
                text = path.read_text(encoding="utf-8")[:65536]
            except (OSError, UnicodeDecodeError):
                found.append(
                    {
                        "supervisor": "systemd",
                        "path": str(path),
                        "label": None,
                        "identified": False,
                    }
                )
                continue
            identified = False
            facts = None
            for line in text.splitlines():
                if line.startswith("ExecStart="):
                    tokens = line[len("ExecStart=") :].replace('"', " ").split()
                    identified = _names_wake_serve(tokens)
                    if identified:
                        facts = _serve_facts_from_tokens(tokens)
                    break
            if not identified:
                found.append(
                    {
                        "supervisor": "systemd",
                        "path": str(path),
                        "label": None,
                        "identified": False,
                    }
                )
                continue
            found.append(
                {
                    "supervisor": "systemd",
                    "path": str(path),
                    "label": str(path.name),
                    "identified": True,
                    **facts,
                }
            )
    return found


def _digest_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _read_regular(path: Path, code: str, detail: str) -> Tuple[bytes, os.stat_result]:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as exc:
        raise ProtocolRefusal(code, detail) from exc
    try:
        identity = os.fstat(descriptor)
        if not stat.S_ISREG(identity.st_mode):
            raise ProtocolRefusal(code, detail)
        chunks: List[bytes] = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks), identity
    finally:
        os.close(descriptor)


def _owned_tool_path(relative: PurePosixPath) -> bool:
    value = relative.as_posix()
    return (
        value == "LICENSE"
        or value in {
            "scripts/floati",
            "scripts/floati-codex-wait",
            "scripts/floati-quota-statusline",
        }
        or value.startswith("floati/")
        or value.startswith("schemas/")
        or value.startswith("bundle/")
        or value.startswith("roles/shipped/")
    )


def _validate_relative(value: object) -> PurePosixPath:
    if not isinstance(value, str):
        raise ProtocolRefusal("uninstall_manifest_invalid", "owned path must be text")
    relative = PurePosixPath(value)
    if (
        relative.is_absolute()
        or not relative.parts
        or any(part in ("", ".", "..") for part in relative.parts)
        or not _owned_tool_path(relative)
    ):
        raise ProtocolRefusal(
            "uninstall_manifest_invalid",
            f"manifest path is outside the Floati tool bundle: {value}",
        )
    return relative


def _path_has_symlink(destination: Path, relative: PurePosixPath) -> bool:
    current = destination
    for part in relative.parts:
        current = current / part
        try:
            if current.is_symlink():
                return True
        except OSError:
            return True
    return False


def _serve_facts_from_tokens(tokens: list[str]) -> Optional[dict]:
    """Pull --root/--as/--harness facts out of a serve command line.

    The removal receipt is written into the daemon's OWN root state plane,
    so the root the daemon served must be known before anything is deleted;
    a supervisor line without the three facts is not identified.
    """

    wanted = {"--root": "root", "--as": "node_id", "--harness": "harness"}
    facts: dict = {}
    for index, token in enumerate(tokens):
        if token in wanted and index + 1 < len(tokens):
            facts[wanted[token]] = tokens[index + 1]
    if len(facts) != len(wanted):
        return None
    return facts


def _prove_wake_supervisor(
    item: Dict[str, Any],
    *,
    uid: int,
    supervisor_runner: Callable[[tuple], subprocess.CompletedProcess[str]],
    systemctl_locator: Callable[[], Optional[str]],
    pid_alive: Callable[[int], bool],
) -> Dict[str, Any]:
    """Prove the daemon behind one supervisor file is gone, or say what it saw.

    The same contract the stop verb carries (WD-2 d): `stopped` with the
    observed pid gone, never a guess. A supervisor command that cannot run
    or an observation that still shows a live pid is `unproven`.
    """

    path = str(item["path"])
    try:
        if item["supervisor"] == "launchd":
            target = f"gui/{uid}/{item['label']}"
            observed = supervisor_runner(("/bin/launchctl", "print", target))
            if observed.returncode != 0:
                return {**item, "stopped": True, "observed_pid": None,
                        "proof": "launchctl_print_absent"}
            observed_pid = _launchctl_print_pid(str(observed.stdout))
            stop_result = supervisor_runner(("/bin/launchctl", "bootout", target))
            after = supervisor_runner(("/bin/launchctl", "print", target))
            after_pid = (
                _launchctl_print_pid(str(after.stdout))
                if after.returncode == 0
                else None
            )
        else:
            executable = systemctl_locator()
            if executable is None:
                return {**item, "stopped": False, "observed_pid": None,
                        "proof": "systemctl_absent"}
            unit = item["label"]
            observed = supervisor_runner(
                (str(executable), "--user", "is-active", unit)
            )
            if observed.returncode != 0:
                return {**item, "stopped": True, "observed_pid": None,
                        "proof": "is_active_absent"}
            shown = supervisor_runner(
                (str(executable), "--user", "show", "-p", "MainPID", "--value", unit)
            )
            try:
                observed_pid = int(str(shown.stdout).strip())
            except ValueError:
                observed_pid = 0
            observed_pid = observed_pid or None
            supervisor_runner((str(executable), "--user", "stop", unit))
            after = supervisor_runner(
                (str(executable), "--user", "is-active", unit)
            )
            after_pid = None
            if after.returncode == 0:
                shown_after = supervisor_runner(
                    (
                        str(executable),
                        "--user",
                        "show",
                        "-p",
                        "MainPID",
                        "--value",
                        unit,
                    )
                )
                try:
                    after_pid = int(str(shown_after.stdout).strip()) or None
                except ValueError:
                    after_pid = None
            stop_result = after
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {**item, "stopped": False, "observed_pid": None,
                "proof": f"supervisor_unavailable:{type(exc).__name__}"}
    alive = after_pid is not None and bool(pid_alive(int(after_pid)))
    if alive:
        return {**item, "stopped": False, "observed_pid": observed_pid,
                "proof": "process_still_alive"}
    return {
        **item,
        "stopped": True,
        "observed_pid": observed_pid,
        "proof": f"stop_observed_returncode_{getattr(stop_result, 'returncode', 'unknown')}",
    }


def _remove_proven_supervisor(item: Dict[str, Any]) -> Dict[str, Any]:
    """Quarantine-and-unlink one proven-stopped supervisor file, then write
    the per-supervisor removal receipt into the daemon's own root state
    plane - Am.2: a deletion with no receipt is the defect, --receipt-dir
    or not."""

    path = Path(str(item["path"]))
    payload_digest = hashlib.sha256(path.read_bytes()).hexdigest()
    quarantine = path.with_name(f".{path.name}.{secrets.token_hex(8)}.remove")
    try:
        os.replace(path, quarantine)
        identity = os.lstat(quarantine)
        if not stat.S_ISREG(identity.st_mode):
            raise OSError("supervisor file is not a regular file")
        quarantine.unlink()
    except OSError as exc:
        if quarantine.exists() and not path.exists():
            try:
                os.replace(quarantine, path)
            except OSError:
                pass
        raise ProtocolRefusal(
            "uninstall_wake_supervisor_remove_failed",
            f"proven-stopped wake supervisor could not be removed safely: {path}",
            remedy="check the file's permissions, remove it by hand if it is "
            "yours, and re-run uninstall",
        ) from exc

    receipt_directory = (
        Path(str(item["root"])) / "state" / "wake-daemon" / "uninstall-removed"
    )
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    receipt_path = receipt_directory / f"{stamp}-{item['label']}.json"
    receipt = {
        "schema_version": 0,
        "kind": "wake_daemon_uninstall_removed",
        "supervisor": item["supervisor"],
        "path": str(path),
        "label": item["label"],
        "node_id": item.get("node_id"),
        "harness": item.get("harness"),
        "observed_pid": item.get("observed_pid"),
        "proof": item.get("proof"),
        "sha256": payload_digest,
        "removed_at": stamp,
    }
    try:
        receipt_directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        receipt_path.write_text(
            json.dumps(receipt, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    except OSError as exc:
        raise DurabilityFailure(
            "uninstall_wake_receipt_write_failed",
            "wake supervisor removed, but its removal receipt could not be "
            f"written: {receipt_path}",
        ) from exc
    return {**item, "removed": True, "receipt": str(receipt_path)}


class UninstallWriter:
    """Remove only unchanged files named by exact install ownership metadata."""

    def __init__(
        self,
        destination: os.PathLike[str] | str,
        *,
        dry_run: bool = False,
        wake_sweep: bool = False,
        launch_agents_directory: os.PathLike[str] | str | None = None,
        user_units_directory: os.PathLike[str] | str | None = None,
        supervisor_runner: Optional[Callable[[tuple], subprocess.CompletedProcess[str]]] = None,
        systemctl_locator: Optional[Callable[[], Optional[str]]] = None,
        pid_alive: Optional[Callable[[int], bool]] = None,
    ) -> None:
        self.destination_arg = destination
        self.dry_run = dry_run
        # WD-2 (e) + Am.2: the wake sweep names real supervisor directories
        # and speaks to the real host supervisor, so it runs ONLY when the
        # caller asks for it AND names the directories explicitly -
        # Path.home() is never a default reachable from library code. The
        # `floati uninstall` verb is the operator surface: it supplies the
        # user's real directories and takes the home reach itself.
        self.wake_sweep = wake_sweep
        if wake_sweep and (
            launch_agents_directory is None or user_units_directory is None
        ):
            raise ProtocolRefusal(
                "uninstall_wake_sweep_directory_required",
                "the wake supervisor sweep requires the caller to name the "
                "LaunchAgents and systemd user-unit directories explicitly",
                remedy="let the floati uninstall CLI supply them (it always "
                "does), or pass launch_agents_directory and "
                "user_units_directory explicitly",
            )
        self.launch_agents_directory = (
            None if launch_agents_directory is None
            else Path(launch_agents_directory)
        )
        self.user_units_directory = (
            None if user_units_directory is None else Path(user_units_directory)
        )
        self.supervisor_runner = (
            _default_supervisor_runner if supervisor_runner is None else supervisor_runner
        )
        self.systemctl_locator = (
            _default_systemctl_locator if systemctl_locator is None else systemctl_locator
        )
        self.pid_alive = _default_pid_alive if pid_alive is None else pid_alive

    def _destination(self) -> Path:
        destination = Path(self.destination_arg).expanduser()
        if not destination.is_absolute():
            raise ProtocolRefusal(
                "uninstall_destination_absolute_required",
                "destination must be absolute",
            )
        if destination.is_symlink():
            raise ProtocolRefusal(
                "uninstall_destination_symlinked",
                "destination must not be a symlink",
            )
        if not destination.is_dir():
            raise ProtocolRefusal(
                "uninstall_destination_missing",
                "destination must be an existing installation directory",
            )
        if not isinstance(self.dry_run, bool):
            raise ProtocolRefusal("uninstall_mode_invalid", "dry_run must be boolean")
        return destination.resolve()

    def _load_manifest(
        self, destination: Path
    ) -> Tuple[List[Dict[str, str]], Dict[str, os.stat_result], Dict[str, Any]]:
        metadata_directory = destination / INSTALL_METADATA_DIRECTORY
        metadata = destination.joinpath(*_METADATA_RELATIVE.parts)
        if metadata_directory.is_symlink() or metadata.is_symlink():
            raise ProtocolRefusal(
                "uninstall_manifest_invalid",
                "install ownership metadata must not be a symlink",
            )
        if not metadata.is_file():
            raise ProtocolRefusal(
                "uninstall_manifest_missing",
                "exact install ownership metadata is required; nothing was removed",
            )
        raw, metadata_identity = _read_regular(
            metadata,
            "uninstall_manifest_invalid",
            "install ownership metadata is not a readable regular file",
        )
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProtocolRefusal(
                "uninstall_manifest_invalid",
                "install ownership metadata is unreadable",
            ) from exc
        if not isinstance(payload, dict):
            raise ProtocolRefusal(
                "uninstall_manifest_invalid",
                "install ownership metadata has an unexpected shape",
            )
        schema_version = payload.get("schema_version")
        if isinstance(schema_version, bool) or schema_version not in {0, 1}:
            raise ProtocolRefusal(
                "uninstall_manifest_invalid", "install ownership schema is unsupported"
            )
        expected_fields = {"schema_version", "source_ref", "source_sha", "files"}
        ownership = None
        if schema_version == 1:
            expected_fields.add("ownership")
        if set(payload) != expected_fields:
            raise ProtocolRefusal(
                "uninstall_manifest_invalid",
                "install ownership metadata has an unexpected shape",
            )
        if schema_version == 1:
            try:
                ownership = validate_install_ownership(payload["ownership"])
            except ProtocolRefusal as exc:
                raise ProtocolRefusal(
                    "uninstall_manifest_invalid", exc.detail
                ) from exc
        if not isinstance(payload["source_ref"], str) or not payload["source_ref"]:
            raise ProtocolRefusal(
                "uninstall_manifest_invalid", "install source ref is invalid"
            )
        if not isinstance(payload["source_sha"], str) or not _SOURCE_SHA.fullmatch(
            payload["source_sha"]
        ):
            raise ProtocolRefusal(
                "uninstall_manifest_invalid", "install source SHA is invalid"
            )
        if not isinstance(payload["files"], list) or not payload["files"]:
            raise ProtocolRefusal(
                "uninstall_manifest_invalid", "install ownership file set is empty"
            )

        entries: List[Dict[str, str]] = []
        identities: Dict[str, os.stat_result] = {}
        seen = set()
        for item in payload["files"]:
            if not isinstance(item, dict) or set(item) != {"path", "sha256"}:
                raise ProtocolRefusal(
                    "uninstall_manifest_invalid", "install ownership entry is invalid"
                )
            relative = _validate_relative(item["path"])
            relative_text = relative.as_posix()
            digest = item["sha256"]
            if relative_text in seen:
                raise ProtocolRefusal(
                    "uninstall_manifest_invalid", f"duplicate owned path: {relative_text}"
                )
            if not isinstance(digest, str) or not _SHA256.fullmatch(digest):
                raise ProtocolRefusal(
                    "uninstall_manifest_invalid", f"invalid owned digest: {relative_text}"
                )
            if _path_has_symlink(destination, relative):
                raise ProtocolRefusal(
                    "uninstall_manifest_mismatch",
                    f"owned path traverses or is a symlink: {relative_text}",
                )
            path = destination.joinpath(*relative.parts)
            current, identity = _read_regular(
                path,
                "uninstall_manifest_mismatch",
                f"owned file is missing or not regular: {relative_text}",
            )
            if _digest_bytes(current) != digest:
                raise ProtocolRefusal(
                    "uninstall_manifest_mismatch",
                    f"owned file digest changed: {relative_text}",
                )
            seen.add(relative_text)
            entries.append({"path": relative_text, "sha256": digest})
            identities[relative_text] = identity

        if [entry["path"] for entry in entries] != sorted(seen):
            raise ProtocolRefusal(
                "uninstall_manifest_invalid", "install ownership paths are not ordered"
            )
        if ownership is not None:
            entrypoint_digest = next(
                (
                    entry["sha256"]
                    for entry in entries
                    if entry["path"] == ownership["entrypoint"]
                ),
                None,
            )
            if (
                ownership["destination"] != str(destination)
                or entrypoint_digest != ownership["entrypoint_sha256"]
            ):
                raise ProtocolRefusal(
                    "uninstall_manifest_invalid",
                    "install ownership binding does not match the removable file set",
                )
        metadata_digest = _digest_bytes(raw)
        metadata_receipt = {
            "path": _METADATA_RELATIVE.as_posix(),
            "sha256": metadata_digest,
        }
        identities[_METADATA_RELATIVE.as_posix()] = metadata_identity
        return entries, identities, metadata_receipt

    @staticmethod
    def _same_identity(
        path: Path, expected: os.stat_result, expected_digest: str
    ) -> bool:
        try:
            payload, current = _read_regular(
                path,
                "uninstall_manifest_mismatch",
                "owned file changed before removal",
            )
        except ProtocolRefusal:
            return False
        return (
            stat.S_ISREG(current.st_mode)
            and (current.st_dev, current.st_ino, current.st_size)
            == (expected.st_dev, expected.st_ino, expected.st_size)
            and _digest_bytes(payload) == expected_digest
        )

    @staticmethod
    def _foreign_files(destination: Path, owned: Sequence[str]) -> List[str]:
        owned_set = set(owned)
        foreign: List[str] = []
        for path in destination.rglob("*"):
            relative = path.relative_to(destination).as_posix()
            if relative.startswith(INSTALL_METADATA_DIRECTORY + "/"):
                continue
            if relative in owned_set:
                continue
            if path.is_file() or path.is_symlink():
                foreign.append(relative)
        return sorted(foreign)

    @staticmethod
    def _retained_records(destination: Path, removal_paths: Sequence[str]) -> List[str]:
        removal = set(removal_paths)
        metadata = destination / INSTALL_METADATA_DIRECTORY
        if not metadata.exists():
            return []
        retained: List[str] = []
        for path in metadata.rglob("*"):
            if not (path.is_file() or path.is_symlink()):
                continue
            relative = path.relative_to(destination).as_posix()
            if relative in removal:
                continue
            retained.append(relative)
        return sorted(retained)

    @staticmethod
    def _remove_empty_owned_directories(destination: Path, entries: Sequence[str]) -> None:
        candidates = set()
        for value in entries:
            current = PurePosixPath(value).parent
            while current.parts and current != PurePosixPath("."):
                candidates.add(current)
                current = current.parent
        for relative in sorted(candidates, key=lambda value: len(value.parts), reverse=True):
            path = destination.joinpath(*relative.parts)
            try:
                path.rmdir()
            except OSError:
                continue

    def run(self) -> Dict[str, Any]:
        destination = self._destination()
        entries, identities, metadata_receipt = self._load_manifest(destination)
        owned_paths = [entry["path"] for entry in entries]
        receipts = [*entries, metadata_receipt]
        foreign = self._foreign_files(destination, owned_paths)
        retained = self._retained_records(
            destination, [receipt["path"] for receipt in receipts]
        )
        wake_found = (
            _discover_wake_supervisors(
                self.launch_agents_directory, self.user_units_directory
            )
            if self.wake_sweep
            else []
        )  # no sweep, no enumeration: bare library construction is inert
        wake_found_rows = [
            {
                "supervisor": item["supervisor"],
                "path": item["path"],
                "label": item["label"],
            }
            for item in wake_found
        ]
        if self.dry_run:
            return {
                "destination": str(destination),
                "dry_run": True,
                "removal_receipts": receipts,
                "removed_count": 0,
                "foreign_preserved": foreign,
                "retained_records": retained,
                "data_retention_notice": _DATA_NOTICE,
                "wake_daemons_found": wake_found_rows,
                "wake_daemons_removed": [],
            }

        # WD-2 (e): the wake sweep is fail-closed and it runs first. Every
        # supervisor is proven stopped before anything is removed; one
        # unproven daemon refuses the whole uninstall with nothing touched.
        proven: List[Dict[str, Any]] = []
        for item in wake_found:
            if not item.get("identified", False):
                raise ProtocolRefusal(
                    "uninstall_wake_supervisor_unidentified",
                    "a file carries the floati wake supervisor name but not "
                    f"its content: {item['path']}; inspect it and remove it "
                    "by hand if it is yours",
                    remedy="inspect the named file and remove it by hand if "
                    "it is yours; uninstall never deletes a file it cannot "
                    "identify",
                )
            proof = _prove_wake_supervisor(
                item,
                uid=os.getuid(),
                supervisor_runner=self.supervisor_runner,
                systemctl_locator=self.systemctl_locator,
                pid_alive=self.pid_alive,
            )
            if not proof["stopped"]:
                raise ProtocolRefusal(
                    "wake_daemon_stop_unproven",
                    "wake daemon could not be proven stopped before "
                    f"uninstall: {item['path']} ({proof['proof']}); "
                    "stop it and re-run uninstall",
                    remedy="stop the daemon - floati wake daemon stop - or "
                    "bootout/stop its supervisor unit by label, then re-run "
                    "uninstall",
                )
            proven.append(proof)
        wake_removed = [_remove_proven_supervisor(item) for item in proven]

        for entry in entries:
            relative = PurePosixPath(entry["path"])
            path = destination.joinpath(*relative.parts)
            if not self._same_identity(
                path, identities[entry["path"]], entry["sha256"]
            ):
                raise ProtocolRefusal(
                    "uninstall_manifest_mismatch",
                    f"owned file identity changed before removal: {entry['path']}",
                )
            path.unlink()

        metadata = destination.joinpath(*_METADATA_RELATIVE.parts)
        if not self._same_identity(
            metadata,
            identities[_METADATA_RELATIVE.as_posix()],
            metadata_receipt["sha256"],
        ):
            raise ProtocolRefusal(
                "uninstall_manifest_mismatch",
                "install ownership metadata changed before removal",
            )
        metadata.unlink()
        try:
            (destination / INSTALL_METADATA_DIRECTORY).rmdir()
        except OSError:
            pass
        self._remove_empty_owned_directories(destination, owned_paths)
        return {
            "destination": str(destination),
            "dry_run": False,
            "removal_receipts": receipts,
            "removed_count": len(receipts),
            "foreign_preserved": foreign,
            "retained_records": self._retained_records(
                destination, [receipt["path"] for receipt in receipts]
            ),
            "data_retention_notice": _DATA_NOTICE,
            "wake_daemons_found": wake_found_rows,
            "wake_daemons_removed": [
                {
                    "supervisor": item["supervisor"],
                    "path": item["path"],
                    "label": item["label"],
                    "observed_pid": item["observed_pid"],
                    "proof": item["proof"],
                    "receipt": item["receipt"],
                }
                for item in wake_removed
            ],
        }


RECEIPT_PREFIX = "floati-uninstalled-"


def _write_receipt(
    receipt_dir: Path, directory_fd: int, evidence: Dict[str, Any]
) -> Path:
    """Create only a new receipt in the directory held since preflight."""

    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    counter = 0
    try:
        while True:
            suffix = f"-{counter}" if counter else ""
            name = f"{RECEIPT_PREFIX}{stamp}{suffix}.json"
            try:
                descriptor = os.open(
                    name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o600, dir_fd=directory_fd,
                )
                break
            except FileExistsError:
                counter += 1
        try:
            receipt_identity = os.fstat(descriptor)
            payload = {"schema_version": 1, "command": "uninstall", **evidence}
            remaining = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
            while remaining:
                written = os.write(descriptor, remaining)
                if written <= 0:
                    raise OSError("receipt write made no progress")
                remaining = remaining[written:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.fsync(directory_fd)
        # The descriptor prevents redirection; do not claim the old pathname
        # still identifies the receipt when its directory has moved.
        expected = os.fstat(directory_fd)
        current = receipt_dir.lstat()
        if (current.st_dev, current.st_ino) != (expected.st_dev, expected.st_ino):
            raise OSError("declared receipt directory changed after preflight")
        current_receipt = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if (current_receipt.st_dev, current_receipt.st_ino) != (
            receipt_identity.st_dev, receipt_identity.st_ino
        ):
            raise OSError("receipt identity changed during publication")
    except OSError as exc:
        raise DurabilityFailure(
            "uninstall_receipt_write_failed",
            "tool removal completed, but receipt durability or its declared "
            f"location is unproved: {receipt_dir}; {exc}",
        ) from exc
    return receipt_dir / name


def _validate_receipt_dir(receipt_dir_arg: str) -> Tuple[Path, int]:
    """Validate writability before removal and retain the directory identity.

    Only a freshly created probe is eligible for cleanup. Existing files,
    including links, are never opened for writing or removed.
    """

    receipt_dir = Path(receipt_dir_arg).expanduser()
    if not receipt_dir.is_absolute():
        raise ProtocolRefusal(
            "uninstall_receipt_dir_absolute_required",
            "--receipt-dir must be an absolute path; a receipt is never "
            "written relative to an ambient working directory",
            remedy="pass one absolute directory, e.g. "
            "--receipt-dir /absolute/path/to/receipts",
        )
    if receipt_dir.is_symlink():
        raise ProtocolRefusal(
            "uninstall_receipt_dir_symlinked",
            "--receipt-dir must not be a symlink",
            remedy="pass the real directory itself, never a symlink to it",
        )
    directory_fd = None
    try:
        receipt_dir.mkdir(parents=True, exist_ok=True)
        directory_fd = os.open(receipt_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        probe = f".floati-receipt-probe-{os.getpid()}-{secrets.token_hex(16)}"
        descriptor = os.open(
            probe, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600, dir_fd=directory_fd,
        )
        try:
            expected = os.fstat(descriptor)
            current = os.stat(probe, dir_fd=directory_fd, follow_symlinks=False)
            if (current.st_dev, current.st_ino) != (expected.st_dev, expected.st_ino):
                raise OSError("receipt probe identity changed")
            os.unlink(probe, dir_fd=directory_fd)
        finally:
            os.close(descriptor)
        return receipt_dir, directory_fd
    except OSError as exc:
        if directory_fd is not None:
            os.close(directory_fd)
        raise ProtocolRefusal(
            "uninstall_receipt_dir_unwritable",
            "--receipt-dir must be writable; a receipt that cannot be "
            "written must refuse before the removal starts",
            remedy="choose a writable directory for --receipt-dir",
        ) from exc


def _handle(args: argparse.Namespace) -> Tuple[str, Dict[str, Any], int]:
    if args.receipt_dir is not None:
        if args.dry_run:
            raise ProtocolRefusal(
                "uninstall_receipt_dir_dry_run_conflict",
                "--receipt-dir records a real removal; a dry run writes "
                "no receipt",
                remedy="drop --receipt-dir to keep planning, or drop "
                "--dry-run to perform the real removal with its receipt",
            )
        receipt_dir, directory_fd = _validate_receipt_dir(args.receipt_dir)
        try:
            evidence = UninstallWriter(
                args.destination,
                dry_run=args.dry_run,
                wake_sweep=True,
                launch_agents_directory=(
                    Path.home() / "Library" / "LaunchAgents"
                ),
                user_units_directory=(
                    Path.home() / ".config" / "systemd" / "user"
                ),
            ).run()
            evidence = {
                **evidence,
                "receipt_written": str(_write_receipt(receipt_dir, directory_fd, evidence)),
            }
        finally:
            os.close(directory_fd)
    else:
        evidence = UninstallWriter(
            args.destination,
            dry_run=args.dry_run,
            wake_sweep=True,
            # Am.2: the CLI is the operator surface - it - and only it -
            # reaches Path.home() to name the real supervisor directories.
            launch_agents_directory=(
                Path.home() / "Library" / "LaunchAgents"
            ),
            user_units_directory=(
                Path.home() / ".config" / "systemd" / "user"
            ),
        ).run()
    return "ok", evidence, 0


def register_cli(commands: argparse._SubParsersAction) -> None:
    """Register the dark command; the integration train calls this once from cli.py."""
    uninstall = commands.add_parser("uninstall")
    uninstall.add_argument("--destination", required=True)
    uninstall.add_argument("--dry-run", action="store_true")
    uninstall.add_argument("--receipt-dir", default=None, metavar="DIR")
    uninstall.add_argument("--json", action="store_true")
    uninstall.set_defaults(handler=_handle)

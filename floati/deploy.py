"""Guarded installation and update of the exact Floati bundle."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from .errors import IntegrityFailure, ProtocolRefusal
from .git_process import fixed_git_command, fixed_git_environment, is_shallow_repository
from .installer_shadow import enumerate_installer_shadow
from .manifest import MANIFEST_NAME, verify_manifest
from .storage_identity import (
    INSTALL_METADATA_DIRECTORY,
    refuse_legacy_workspace_artifacts,
)
from .update_ownership import validate_install_ownership
from . import wiring_journal


_METADATA_DIR = INSTALL_METADATA_DIRECTORY
_METADATA_NAME = "manifest.v0.json"


def _entry_path(value: os.PathLike[str] | str, label: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ProtocolRefusal("deployment_path_absolute_required", f"{label} must be absolute")
    if path.is_symlink():
        raise ProtocolRefusal("deployment_symlinked_entry", f"{label} is a symlink")
    return path


def _has_symlink_component(root: Path, relative: PurePosixPath) -> bool:
    current = root
    for part in relative.parts:
        current = current / part
        try:
            if current.is_symlink():
                return True
        except OSError:
            return True
    return False


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _journal_timestamp() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _git(
    source: Path,
    args: Sequence[str],
    *,
    executable: str = "git",
    inspected_ref: Optional[str] = None,
) -> str:
    try:
        result = subprocess.run(
            fixed_git_command(executable, source, args),
            env=fixed_git_environment(executable),
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        subject = f" ref {inspected_ref}" if inspected_ref is not None else " source"
        raise ProtocolRefusal(
            "deployment_currency_unavailable",
            f"git could not inspect{subject}: {exc}",
        ) from exc
    if result.returncode != 0:
        detail = result.stderr.strip() or "git inspection failed"
        if inspected_ref is not None:
            detail = f"git could not inspect ref {inspected_ref}: {detail}"
        raise ProtocolRefusal("deployment_currency_unavailable", detail)
    return result.stdout.strip()


def _manifest_entries(
    source: Path, *, git_executable: str = "/usr/bin/git"
) -> List[Dict[str, str]]:
    errors = verify_manifest(source, git_executable=git_executable)
    if errors:
        raise IntegrityFailure(
            "deployment_manifest_invalid",
            "; ".join(errors),
        )
    try:
        raw = json.loads((source / MANIFEST_NAME).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise IntegrityFailure("deployment_manifest_invalid", "manifest unreadable") from exc
    entries = raw.get("files") if isinstance(raw, dict) else None
    if not isinstance(entries, list):
        raise IntegrityFailure("deployment_manifest_invalid", "manifest files are invalid")

    validated: List[Dict[str, str]] = []
    for entry in entries:
        relative = entry["path"]
        pure = PurePosixPath(relative)
        if _has_symlink_component(source, pure):
            raise IntegrityFailure(
                "deployment_manifest_invalid",
                f"manifest path traverses a symlink: {relative}",
            )
        path = source.joinpath(*pure.parts)
        if path.is_symlink() or not path.is_file():
            raise IntegrityFailure(
                "deployment_manifest_invalid",
                f"manifest path is not a regular file: {relative}",
            )
        if _digest(path) != entry["sha256"]:
            raise IntegrityFailure(
                "deployment_manifest_invalid",
                f"manifest digest mismatch: {relative}",
            )
        validated.append({"path": relative, "sha256": entry["sha256"]})
    return validated


def _metadata_path(destination: Path) -> Path:
    return destination / _METADATA_DIR / _METADATA_NAME


def render_install_metadata(
    *,
    destination: Path,
    source_ref: str,
    source_sha: str,
    entries: Sequence[Dict[str, str]],
    entrypoint_sha256: str,
    previous_ownership: Optional[Dict[str, object]],
) -> bytes:
    """Purely render the exact schema-1 install metadata persisted by the writer."""

    if previous_ownership is None:
        owner = {
            "kind": "unknown",
            "manager": None,
            "remedy": "reinstall with the governed standalone installer",
        }
    else:
        validated = validate_install_ownership(previous_ownership)
        owner = {
            "kind": validated["kind"],
            "manager": validated["manager"],
            "remedy": validated["remedy"],
        }
    ownership = validate_install_ownership(
        {
            "kind": owner["kind"],
            "destination": str(destination),
            "entrypoint": "scripts/floati",
            "entrypoint_sha256": entrypoint_sha256,
            "manager": owner["manager"],
            "remedy": owner["remedy"],
        }
    )
    payload = {
        "schema_version": 1,
        "source_ref": source_ref,
        "source_sha": source_sha,
        "files": list(entries),
        "ownership": ownership,
    }
    return (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _load_previous(
    destination: Path,
) -> Optional[Tuple[Dict[str, str], Optional[Dict[str, object]]]]:
    metadata = _metadata_path(destination)
    metadata_dir = metadata.parent
    if metadata_dir.exists() and metadata_dir.is_symlink():
        raise ProtocolRefusal("deployment_metadata_symlinked", "install metadata directory is a symlink")
    if metadata_dir.exists() and not metadata_dir.is_dir():
        raise ProtocolRefusal("deployment_metadata_invalid", "install metadata path is not a directory")
    if metadata.exists() and metadata.is_symlink():
        raise ProtocolRefusal("deployment_metadata_symlinked", "install metadata is a symlink")
    if not metadata.exists():
        if metadata_dir.exists():
            raise ProtocolRefusal("deployment_metadata_invalid", "install metadata manifest is missing")
        return None
    if not metadata.is_file():
        raise ProtocolRefusal("deployment_metadata_invalid", "install metadata is not a file")
    try:
        raw = json.loads(metadata.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolRefusal("deployment_metadata_invalid", "install metadata is unreadable") from exc
    if not isinstance(raw, dict):
        raise ProtocolRefusal("deployment_metadata_invalid", "install metadata is not one object")
    schema_version = raw.get("schema_version")
    if isinstance(schema_version, bool) or schema_version not in {0, 1}:
        raise ProtocolRefusal("deployment_metadata_invalid", "install metadata schema is unsupported")
    expected_fields = {"schema_version", "source_ref", "source_sha", "files"}
    if schema_version == 1:
        expected_fields.add("ownership")
    if set(raw) != expected_fields:
        raise ProtocolRefusal("deployment_metadata_invalid", "install metadata has an unexpected shape")
    ownership = None
    if schema_version == 1:
        try:
            ownership = validate_install_ownership(raw["ownership"])
        except ProtocolRefusal as exc:
            raise ProtocolRefusal("deployment_metadata_invalid", exc.detail) from exc
    files = raw.get("files")
    if not isinstance(files, list):
        raise ProtocolRefusal("deployment_metadata_invalid", "install metadata files are invalid")
    previous: Dict[str, str] = {}
    for entry in files:
        if not isinstance(entry, dict) or set(entry) != {"path", "sha256"}:
            raise ProtocolRefusal("deployment_metadata_invalid", "install metadata entry is invalid")
        relative = entry.get("path")
        digest = entry.get("sha256")
        if not isinstance(relative, str) or not isinstance(digest, str):
            raise ProtocolRefusal("deployment_metadata_invalid", "install metadata entry is invalid")
        pure = PurePosixPath(relative)
        if pure.is_absolute() or not pure.parts or any(part in ("", ".", "..") for part in pure.parts):
            raise ProtocolRefusal("deployment_metadata_invalid", f"invalid owned path: {relative}")
        if _METADATA_DIR in pure.parts:
            raise ProtocolRefusal("deployment_metadata_invalid", f"metadata owns reserved path: {relative}")
        previous[relative] = digest
    return previous, ownership


def _destination_path(destination: Path, relative: str) -> Path:
    pure = PurePosixPath(relative)
    if pure.is_absolute() or not pure.parts or any(part in ("", ".", "..") for part in pure.parts):
        raise ProtocolRefusal("deployment_manifest_invalid", f"invalid managed path: {relative}")
    if _has_symlink_component(destination, pure):
        raise ProtocolRefusal("deployment_foreign_collision", f"managed path traverses a symlink: {relative}")
    return destination.joinpath(*pure.parts)


def _destination_files(destination: Path) -> Iterable[Tuple[str, Path]]:
    if not destination.exists():
        return ()
    found: List[Tuple[str, Path]] = []
    for path in destination.rglob("*"):
        try:
            relative = path.relative_to(destination)
        except ValueError:
            continue
        if _METADATA_DIR in relative.parts:
            continue
        if path.is_file() or path.is_symlink():
            found.append((relative.as_posix(), path))
    return found


def _ensure_parent(destination: Path, relative: str) -> Path:
    pure = PurePosixPath(relative)
    parent = destination
    for part in pure.parts[:-1]:
        containing = parent
        parent = containing / part
        if parent.exists() and parent.is_symlink():
            raise ProtocolRefusal("deployment_foreign_collision", f"managed parent is a symlink: {relative}")
        if parent.exists() and not parent.is_dir():
            raise ProtocolRefusal("deployment_foreign_collision", f"managed parent is not a directory: {relative}")
        if not parent.exists():
            parent.mkdir()
        # Persist both the directory inode and its link from the ancestor
        # before publishing beneath it.  Replay this even for an exact
        # existing directory: visibility cannot prove that a predecessor's
        # containing-directory fsync returned before its response was lost.
        for directory in (parent, containing):
            descriptor = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    return destination.joinpath(*pure.parts)


class DeploymentWriter:
    """Install or update only the source manifest's owned file set."""

    def __init__(
        self,
        source: os.PathLike[str] | str,
        destination: os.PathLike[str] | str,
        operation: str,
        *,
        ref: str = "origin/main",
        committed_tree: bool = False,
        installation_owner: Optional[Dict[str, object]] = None,
        installer_path: Optional[str] = None,
        git_executable: str = "/usr/bin/git",
        join_id: Optional[str] = None,
        planned_intents: Optional[Sequence[Dict[str, str]]] = None,
        fault_hook: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.source_arg = source
        self.destination_arg = destination
        self.operation = operation
        self.ref = ref
        self.committed_tree = committed_tree
        self.installation_owner = installation_owner
        self.installer_path = installer_path
        self.git_executable = git_executable
        self.join_id = join_id
        self.planned_intents = planned_intents
        self._fault_hook = fault_hook

    def _fault(self, event: str) -> None:
        """Narrow test seam for a crash at one durable writer boundary."""

        if self._fault_hook is not None:
            self._fault_hook(event)

    def _atomic_write_bytes(
        self,
        target: Path,
        payload: bytes,
        *,
        mode: int,
        phase: str,
    ) -> None:
        """Commit exact bytes through an owned same-directory durable boundary."""

        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{target.name}.floati-", dir=target.parent
        )
        temporary = Path(temporary_name)
        pending_temporary = True
        try:
            os.fchmod(descriptor, mode)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            self._fault(f"after_{phase}_file_fsync")
            os.replace(temporary, target)
            pending_temporary = False
            self._fault(f"after_{phase}_replace")
            self._durable_readback(target, payload, phase=phase)
        finally:
            if pending_temporary:
                try:
                    temporary.unlink()
                except FileNotFoundError:
                    pass

    def _atomic_copy(self, source: Path, target: Path) -> None:
        self._atomic_write_bytes(
            target, source.read_bytes(), mode=source.stat().st_mode & 0o777,
            phase="file",
        )

    def _durable_readback(self, target: Path, payload: bytes, *, phase: str) -> None:
        """Establish the parent-directory barrier and exact bytes on retries too."""

        directory_fd = os.open(target.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        self._fault(f"after_{phase}_directory_fsync")
        if target.is_symlink() or not target.is_file() or target.read_bytes() != payload:
            raise IntegrityFailure("deployment_write_invalid", f"atomic {phase} readback diverged")
        self._fault(f"after_{phase}_readback")

    def _validate_join_id(self) -> None:
        if self.join_id is not None and (
            not isinstance(self.join_id, str)
            or re.fullmatch(r"[0-9a-f]{64}", self.join_id) is None
        ):
            raise ProtocolRefusal(
                "deployment_join_invalid",
                "join_id must be one lowercase SHA-256 digest when supplied",
            )

    def _validated_writer_intents(
        self,
        destination: Path,
        entries: List[Dict[str, str]],
        previous: Optional[Dict[str, str]],
        metadata_sha256: str,
        *,
        check_ops: bool,
    ) -> List[Dict[str, str]]:
        """Return the one ordered vector used for recovery and emission.

        Ordinary install/update callers derive the historical behavior.  G2
        supplies an authenticated vector, whose manifest order, absolute
        paths, target digests, and (while the pre inventory is available)
        create/replace operations must equal that same derivation.
        """

        derived = [
            {
                "kind": "file",
                "op": "replace" if entry["path"] in (previous or {}) else "create",
                "path": str(_destination_path(destination, entry["path"])),
                "sha256": entry["sha256"],
            }
            for entry in entries
        ]
        derived.append(
            {
                "kind": "file",
                "op": "replace" if previous is not None else "create",
                "path": str(_metadata_path(destination)),
                "sha256": metadata_sha256,
            }
        )
        supplied = self.planned_intents
        if supplied is None:
            return derived
        if not isinstance(supplied, list) or len(supplied) != len(derived):
            raise ProtocolRefusal(
                "deployment_join_invalid",
                "planned writer intents do not equal the target manifest inventory",
            )
        validated: List[Dict[str, str]] = []
        for raw, expected in zip(supplied, derived):
            if (
                not isinstance(raw, dict)
                or set(raw) != {"kind", "op", "path", "sha256"}
                or raw.get("kind") != "file"
                or raw.get("op") not in {"create", "replace"}
                or raw.get("path") != expected["path"]
                or raw.get("sha256") != expected["sha256"]
                or (check_ops and raw.get("op") != expected["op"])
            ):
                raise ProtocolRefusal(
                    "deployment_join_invalid",
                    "planned writer intent diverges from the exact pre/target inventory",
                )
            validated.append(
                {
                    "kind": "file",
                    "op": str(raw["op"]),
                    "path": str(raw["path"]),
                    "sha256": str(raw["sha256"]),
                }
            )
        return validated

    def _check_currency(self, source: Path) -> str:
        if is_shallow_repository(source, git_executable=self.git_executable):
            raise ProtocolRefusal(
                "deployment_shallow_repository",
                "source repository is shallow; fetch complete history before deploying",
            )
        status = _git(
            source,
            ("status", "--porcelain=v1", "--untracked-files=all"),
            executable=self.git_executable,
        )
        if status:
            raise ProtocolRefusal(
                "deployment_currency_unavailable",
                "source tree is not clean",
            )
        head = _git(
            source,
            ("rev-parse", "--verify", "HEAD^{commit}"),
            executable=self.git_executable,
            inspected_ref="HEAD",
        )
        if self.committed_tree:
            return head
        target = _git(
            source,
            ("rev-parse", "--verify", f"{self.ref}^{{commit}}"),
            executable=self.git_executable,
            inspected_ref=self.ref,
        )
        if head != target:
            raise ProtocolRefusal(
                "deployment_currency_unavailable",
                f"HEAD {head} is not {self.ref} ({target})",
            )
        return head

    def _preflight_collisions(
        self,
        destination: Path,
        entries: List[Dict[str, str]],
        previous: Optional[Dict[str, str]],
    ) -> None:
        if not destination.exists():
            return
        for entry in entries:
            relative = entry["path"]
            path = _destination_path(destination, relative)
            if not path.exists() and not path.is_symlink():
                continue
            if previous is None or relative not in previous:
                raise ProtocolRefusal(
                    "deployment_foreign_collision",
                    f"managed path already exists without Floati ownership: {relative}",
                )
            if path.is_symlink() or not path.is_file() or _digest(path) != previous[relative]:
                raise ProtocolRefusal(
                    "deployment_foreign_collision",
                    f"managed path was changed by a foreign writer: {relative}",
                )

    def _joined_prefix(
        self,
        destination: Path,
        entries: List[Dict[str, str]],
        previous: Optional[Dict[str, str]],
        intents: List[Dict[str, str]],
    ) -> tuple[list[wiring_journal.JournalEntry], list[bool]]:
        """Authenticate and classify one terminal deterministic writer prefix.

        A journal row is intentionally committed before its file write.  The
        terminal joined row may therefore still hold the exact planned pre
        bytes, but no earlier joined row may.  Future rows have no evidence
        and must remain exact pre-state.  This is deliberately performed
        before creating parents, deleting stale files, or appending a row.
        """

        if self.join_id is None:
            return [], []
        journal = wiring_journal.journal_path(destination)
        try:
            all_rows = wiring_journal.read_entries(journal) if journal.exists() else []
        except wiring_journal.WiringJournalCorrupt as exc:
            raise ProtocolRefusal("deployment_join_invalid", "joined wiring journal is corrupt") from exc
        joined = [row for row in all_rows if row.payload.get("join_id") == self.join_id]
        if not joined:
            return [], []
        if (
            [row.ordinal for row in joined] != list(range(joined[0].ordinal, joined[0].ordinal + len(joined)))
            or joined[-1].ordinal != len(all_rows)
            or len(joined) > len(intents)
        ):
            raise ProtocolRefusal("deployment_join_invalid", "joined writer entries are not one terminal contiguous prefix")

        post_state: list[bool] = []
        for index, entry in enumerate(entries):
            relative = entry["path"]
            path = _destination_path(destination, relative)
            if path.is_symlink():
                raise ProtocolRefusal("deployment_foreign_collision", f"managed path is a symlink: {relative}")
            exists = path.exists()
            digest = _digest(path) if exists and path.is_file() else None
            pre_digest = previous.get(relative) if previous is not None else None
            pre_ok = (not exists) if pre_digest is None else (digest == pre_digest)
            post_ok = digest == entry["sha256"]
            if exists and not path.is_file():
                pre_ok = post_ok = False
            if index < len(joined):
                intent = intents[index]
                joined_row = joined[index]
                if (
                    joined_row.payload.get("action") != self.operation
                    or joined_row.payload.get("kind") != intent["kind"]
                    or joined_row.payload.get("path") != intent["path"]
                    or joined_row.payload.get("op") != intent["op"]
                    or joined_row.payload.get("sha256") != intent["sha256"]
                ):
                    raise ProtocolRefusal("deployment_join_invalid", "joined writer prefix diverges from the planned manifest")
                if index < len(joined) - 1:
                    allowed = post_ok
                else:
                    allowed = pre_ok or post_ok
            else:
                # A later post byte has no prior journal evidence.  `pre ==
                # post` remains harmless, but every other target byte is an
                # ambiguous external write and must not be overwritten.
                allowed = pre_ok
            if not allowed:
                raise ProtocolRefusal("deployment_join_invalid", f"managed path diverges from deterministic join state: {relative}")
            post_state.append(post_ok)
        return joined, post_state

    def _ownership_for_entrypoint(
        self,
        destination: Path,
        previous: Optional[Dict[str, object]],
        entrypoint_sha256: str,
    ) -> Dict[str, object]:
        """Render ownership from a known entrypoint digest without reading bytes."""

        supplied = self.installation_owner
        if supplied is not None:
            if not isinstance(supplied, dict) or set(supplied) != {
                "kind", "manager", "remedy"
            }:
                raise ProtocolRefusal(
                    "deployment_owner_invalid",
                    "installation_owner must contain exact kind/manager/remedy fields",
                )
            owner = supplied
        elif previous is not None:
            owner = {
                "kind": previous["kind"],
                "manager": previous["manager"],
                "remedy": previous["remedy"],
            }
        elif self.operation == "update":
            owner = {
                "kind": "unknown",
                "manager": None,
                "remedy": "reinstall with the governed standalone installer",
            }
        else:
            owner = {"kind": "floati_standalone", "manager": None, "remedy": None}
        try:
            return validate_install_ownership({
                "kind": owner["kind"], "destination": str(destination.resolve()),
                "entrypoint": "scripts/floati", "entrypoint_sha256": entrypoint_sha256,
                "manager": owner["manager"], "remedy": owner["remedy"],
            })
        except ProtocolRefusal as exc:
            raise ProtocolRefusal("deployment_owner_invalid", exc.detail) from exc

    def _remove_stale(
        self,
        destination: Path,
        previous: Optional[Dict[str, str]],
        current_paths: set[str],
    ) -> List[str]:
        if not previous:
            return []
        preserved: List[str] = []
        for relative, old_digest in previous.items():
            if relative in current_paths:
                continue
            pure = PurePosixPath(relative)
            path = destination.joinpath(*pure.parts)
            if _has_symlink_component(destination, pure):
                preserved.append(relative)
                continue
            if path.is_symlink() or not path.is_file() or _digest(path) != old_digest:
                preserved.append(relative)
                continue
            path.unlink()
        return preserved

    def _write_metadata(
        self,
        destination: Path,
        entries: List[Dict[str, str]],
        source_sha: str,
        ownership: Dict[str, object],
    ) -> None:
        metadata_dir = destination / _METADATA_DIR
        if metadata_dir.exists() and metadata_dir.is_symlink():
            raise ProtocolRefusal("deployment_metadata_symlinked", "install metadata directory is a symlink")
        metadata_dir.mkdir(mode=0o700, exist_ok=True)
        metadata = _metadata_path(destination)
        if metadata.exists() and metadata.is_symlink():
            raise ProtocolRefusal("deployment_metadata_symlinked", "install metadata is a symlink")
        temporary = metadata.with_name(f".{metadata.name}.tmp")
        temporary.write_bytes(
            render_install_metadata(
                destination=destination.resolve(strict=True),
                source_ref=self.ref,
                source_sha=source_sha,
                entries=entries,
                entrypoint_sha256=str(ownership["entrypoint_sha256"]),
                previous_ownership=ownership,
            )
        )
        os.replace(temporary, metadata)

    def _ownership(
        self,
        destination: Path,
        previous: Optional[Dict[str, object]],
    ) -> Dict[str, object]:
        supplied = self.installation_owner
        if supplied is not None:
            if not isinstance(supplied, dict) or set(supplied) != {
                "kind", "manager", "remedy"
            }:
                raise ProtocolRefusal(
                    "deployment_owner_invalid",
                    "installation_owner must contain exact kind/manager/remedy fields",
                )
            owner = supplied
        elif previous is not None:
            owner = {
                "kind": previous["kind"],
                "manager": previous["manager"],
                "remedy": previous["remedy"],
            }
        elif self.operation == "update":
            owner = {
                "kind": "unknown",
                "manager": None,
                "remedy": "reinstall with the governed standalone installer",
            }
        else:
            owner = {"kind": "floati_standalone", "manager": None, "remedy": None}
        entrypoint = destination / "scripts" / "floati"
        if entrypoint.is_symlink() or not entrypoint.is_file():
            raise IntegrityFailure(
                "deployment_write_invalid",
                "installed entrypoint is unavailable for ownership measurement",
            )
        record = {
            "kind": owner["kind"],
            "destination": str(destination.resolve()),
            "entrypoint": "scripts/floati",
            "entrypoint_sha256": _digest(entrypoint),
            "manager": owner["manager"],
            "remedy": owner["remedy"],
        }
        try:
            return validate_install_ownership(record)
        except ProtocolRefusal as exc:
            raise ProtocolRefusal("deployment_owner_invalid", exc.detail) from exc

    def _validate_owner_intent(self, destination: Path) -> None:
        supplied = self.installation_owner
        if supplied is None:
            return
        if set(supplied) != {"kind", "manager", "remedy"}:
            raise ProtocolRefusal(
                "deployment_owner_invalid",
                "installation_owner must contain exact kind/manager/remedy fields",
            )
        record = {
            "kind": supplied["kind"],
            "destination": str(destination.resolve()),
            "entrypoint": "scripts/floati",
            "entrypoint_sha256": "0" * 64,
            "manager": supplied["manager"],
            "remedy": supplied["remedy"],
        }
        try:
            validate_install_ownership(record)
        except ProtocolRefusal as exc:
            raise ProtocolRefusal("deployment_owner_invalid", exc.detail) from exc

    def stage_source(self) -> Dict[str, object]:
        """Verify exact source/Git/manifest identity without target mutation."""

        if self.operation not in {"install", "update"}:
            raise ProtocolRefusal("deployment_operation_invalid", "operation must be install or update")
        self._validate_join_id()
        if not isinstance(self.ref, str) or not self.ref or any(char.isspace() for char in self.ref):
            raise ProtocolRefusal("deployment_ref_invalid", "ref must be one non-empty Git ref")
        if not isinstance(self.committed_tree, bool):
            raise ProtocolRefusal("deployment_mode_invalid", "committed_tree must be boolean")
        source = _entry_path(self.source_arg, "source")
        destination = _entry_path(self.destination_arg, "destination")
        if not source.is_dir() or (destination.exists() and not destination.is_dir()):
            raise ProtocolRefusal("deployment_source_invalid", "source and destination must be directories")
        if destination.exists() and source.resolve() == destination.resolve():
            raise ProtocolRefusal("deployment_source_destination_same", "source and destination must differ")
        self._validate_owner_intent(destination)
        refuse_legacy_workspace_artifacts(destination)
        source_sha = self._check_currency(source)
        entries = _manifest_entries(source, git_executable=self.git_executable)
        return {
            "source": str(source),
            "destination": str(destination),
            "source_sha": source_sha,
            "manifest_entries": entries,
        }

    def stage(self) -> Dict[str, object]:
        """Verify an update source and destination without creating or replacing bytes.

        Fleet-update orchestration uses this before its first durable receipt so
        its shared-install target is checked alongside every waiter target.
        """

        staged = self.stage_source()
        destination = Path(str(staged["destination"]))
        entries = list(staged["manifest_entries"])
        previous_install = (
            _load_previous(destination)
            if destination.exists() and _metadata_path(destination).exists()
            else None
        )
        self._preflight_collisions(
            destination, entries, previous_install[0] if previous_install is not None else None
        )
        previous = previous_install[0] if previous_install is not None else None
        previous_ownership = previous_install[1] if previous_install is not None else None
        entrypoint_digest = next(
            entry["sha256"] for entry in entries if entry["path"] == "scripts/floati"
        )
        ownership = self._ownership_for_entrypoint(
            destination, previous_ownership, entrypoint_digest
        )
        planned_metadata = render_install_metadata(
            destination=destination.resolve(), source_ref=self.ref,
            source_sha=str(staged["source_sha"]), entries=entries,
            entrypoint_sha256=entrypoint_digest, previous_ownership=ownership,
        )
        self._validated_writer_intents(
            destination, entries, previous,
            hashlib.sha256(planned_metadata).hexdigest(), check_ops=True,
        )
        return staged

    def verify_durable_post(self) -> Dict[str, object]:
        """Replay exact post-state durability barriers without replacing bytes.

        An authenticated saga may observe every joined target byte after a
        predecessor crashed before its response.  Byte equality alone cannot
        prove the parent-directory fsync completed, so this path rechecks the
        explicit source/metadata identity, fsyncs each parent, and reads every
        byte back without appending a journal row or invoking ``os.replace``.
        """

        self._validate_join_id()
        source = _entry_path(self.source_arg, "source")
        destination = _entry_path(self.destination_arg, "destination")
        if not source.is_dir() or not destination.is_dir():
            raise ProtocolRefusal(
                "deployment_source_invalid",
                "durable post verification requires source and destination directories",
            )
        source_sha = self._check_currency(source)
        entries = _manifest_entries(source, git_executable=self.git_executable)
        previous_install = _load_previous(destination)
        if previous_install is None:
            raise ProtocolRefusal(
                "deployment_metadata_invalid", "durable post verification requires install metadata"
            )
        previous, previous_ownership = previous_install
        expected_files = {entry["path"]: entry["sha256"] for entry in entries}
        if previous != expected_files:
            raise ProtocolRefusal(
                "deployment_join_invalid",
                "durable post metadata does not name the exact target inventory",
            )
        entrypoint_digest = next(
            entry["sha256"] for entry in entries if entry["path"] == "scripts/floati"
        )
        ownership = self._ownership_for_entrypoint(
            destination, previous_ownership, entrypoint_digest
        )
        planned_metadata = render_install_metadata(
            destination=destination.resolve(strict=True),
            source_ref=self.ref,
            source_sha=source_sha,
            entries=entries,
            entrypoint_sha256=entrypoint_digest,
            previous_ownership=ownership,
        )
        self._validated_writer_intents(
            destination, entries, previous,
            hashlib.sha256(planned_metadata).hexdigest(), check_ops=False,
        )
        for entry in entries:
            source_path = source.joinpath(*PurePosixPath(entry["path"]).parts)
            target = _destination_path(destination, entry["path"])
            self._durable_readback(target, source_path.read_bytes(), phase="file")
        self._durable_readback(
            _metadata_path(destination), planned_metadata, phase="metadata"
        )
        if self.join_id is not None:
            wiring_journal.replay_durability(destination)
        return {
            "source_sha": source_sha,
            "manifest_entries": entries,
            "metadata_sha256": hashlib.sha256(planned_metadata).hexdigest(),
        }

    def run(self) -> Dict[str, Any]:
        if self.operation not in {"install", "update"}:
            raise ProtocolRefusal("deployment_operation_invalid", "operation must be install or update")
        self._validate_join_id()
        if not isinstance(self.ref, str) or not self.ref or any(char.isspace() for char in self.ref):
            raise ProtocolRefusal("deployment_ref_invalid", "ref must be one non-empty Git ref")
        if not isinstance(self.committed_tree, bool):
            raise ProtocolRefusal("deployment_mode_invalid", "committed_tree must be boolean")
        if self.installation_owner is not None and not isinstance(
            self.installation_owner, dict
        ):
            raise ProtocolRefusal(
                "deployment_owner_invalid", "installation_owner must be one mapping"
            )

        source = _entry_path(self.source_arg, "source")
        destination = _entry_path(self.destination_arg, "destination")
        if source.exists() and not source.is_dir():
            raise ProtocolRefusal("deployment_source_invalid", "source is not a directory")
        if destination.exists() and not destination.is_dir():
            raise ProtocolRefusal("deployment_destination_invalid", "destination is not a directory")
        if source.exists() and destination.exists() and source.resolve() == destination.resolve():
            raise ProtocolRefusal("deployment_source_destination_same", "source and destination must differ")

        self._validate_owner_intent(destination)

        refuse_legacy_workspace_artifacts(destination)

        source_sha = self._check_currency(source)
        entries = _manifest_entries(source, git_executable=self.git_executable)
        managed_paths = [entry["path"] for entry in entries]
        current_paths = set(managed_paths)

        installer_shadow = enumerate_installer_shadow(
            destination,
            path=self.installer_path,
            source_script=source / "scripts" / "floati",
        )
        outcome = installer_shadow["outcome"]
        if outcome == "found":
            raise ProtocolRefusal(
                "deployment_shadow_found",
                str(installer_shadow["reason"]),
            )
        if outcome != "affirmative_none":
            raise ProtocolRefusal(
                "deployment_shadow_unknown",
                (
                    f'{installer_shadow["reason"]} Blocked PATH entry: '
                    f'{installer_shadow["blocked_entry"]}.'
                    if "remedy" in installer_shadow
                    else str(installer_shadow["reason"])
                ),
                (
                    str(installer_shadow["remedy"])
                    if "remedy" in installer_shadow
                    else None
                ),
            )

        journal_candidate = wiring_journal.journal_path(destination)
        try:
            existing_journal = (
                wiring_journal.read_entries(journal_candidate)
                if journal_candidate.exists() else []
            )
        except wiring_journal.WiringJournalCorrupt as exc:
            raise ProtocolRefusal("deployment_join_invalid", "joined wiring journal is corrupt") from exc
        has_join = self.join_id is not None and any(
            row.payload.get("join_id") == self.join_id for row in existing_journal
        )
        if has_join:
            # A prior append may have reached the file fsync but lost its
            # response before the parent-directory barrier.  Re-establish
            # both barriers before resuming any target mutation.
            try:
                existing_journal = wiring_journal.replay_durability(destination)
            except wiring_journal.WiringJournalCorrupt as exc:
                raise ProtocolRefusal(
                    "deployment_join_invalid", "joined wiring journal is corrupt"
                ) from exc
        previous_install = (
            _load_previous(destination)
            if destination.exists() and (
                not has_join
                or (self.operation != "install" and _metadata_path(destination).exists())
            )
            else None
        )
        previous = previous_install[0] if previous_install is not None else None
        previous_ownership = (
            previous_install[1] if previous_install is not None else None
        )
        entrypoint_digest = next(
            entry["sha256"] for entry in entries if entry["path"] == "scripts/floati"
        )
        ownership = self._ownership_for_entrypoint(
            destination, previous_ownership, entrypoint_digest
        )
        metadata = _metadata_path(destination)
        planned_metadata = render_install_metadata(
            destination=destination.resolve(), source_ref=self.ref,
            source_sha=source_sha, entries=entries,
            entrypoint_sha256=entrypoint_digest, previous_ownership=ownership,
        )
        writer_intents = self._validated_writer_intents(
            destination, entries, previous,
            hashlib.sha256(planned_metadata).hexdigest(), check_ops=True,
        )
        joined_prefix, joined_post_state = self._joined_prefix(
            destination, entries, previous, writer_intents
        )
        if not joined_prefix:
            self._preflight_collisions(destination, entries, previous)

        if not destination.exists():
            destination.mkdir(mode=0o700)
        elif destination.is_symlink():
            raise ProtocolRefusal("deployment_symlinked_entry", "destination is a symlink")

        if joined_prefix and previous is not None and set(previous) - current_paths:
            raise ProtocolRefusal(
                "deployment_join_invalid",
                "joined recovery cannot prove the pre-journal stale-file deletion boundary",
            )
        preserved = self._remove_stale(destination, previous, current_paths)

        metadata_post = (
            metadata.exists() and not metadata.is_symlink()
            and metadata.is_file() and metadata.read_bytes() == planned_metadata
        )
        metadata_pre = not metadata.exists() or previous_install is not None
        metadata_joined = len(joined_prefix) == len(entries) + 1
        if joined_prefix:
            if metadata_joined and not (metadata_pre or metadata_post):
                raise ProtocolRefusal("deployment_join_invalid", "joined metadata entry requires exact planned pre or post bytes")
            if len(joined_prefix) < len(entries) and not metadata_pre:
                raise ProtocolRefusal("deployment_join_invalid", "partial joined writer metadata is not exact pre-state")
            if len(joined_prefix) == len(entries) and not (metadata_pre or metadata_post):
                raise ProtocolRefusal("deployment_join_invalid", "metadata is neither exact pre nor post state")

        # U2 manifest-before-meaning (E3.1): the wiring journal entry for
        # each file is APPENDED before the file is written. A crash between
        # append and write leaves an honest extra entry whose target is
        # absent — uninstall replay reports that as already-done, never as
        # an untracked artifact.
        journal = wiring_journal.journal_path(destination)
        for index, entry in enumerate(entries):
            relative = entry["path"]
            source_path = source.joinpath(*PurePosixPath(relative).parts)
            target = _ensure_parent(destination, relative)
            intent = writer_intents[index]
            journal_payload = {
                "v": wiring_journal.JOURNAL_SCHEMA_VERSION,
                "ts": _journal_timestamp(),
                "actor": {"command": self.operation,
                          "floatiVersion": source_sha[:12]},
                "action": self.operation,
                "kind": intent["kind"],
                "path": intent["path"],
                "op": intent["op"],
                "sha256": intent["sha256"],
            }
            if self.join_id is not None:
                journal_payload["join_id"] = self.join_id
            already_journaled = index < len(joined_prefix)
            if not already_journaled:
                wiring_journal.append_entry(destination, journal_payload)
                self._fault("after_file_journal")
            if target.exists() and target.is_symlink():
                raise ProtocolRefusal("deployment_foreign_collision", f"managed path is a symlink: {relative}")
            post_state = joined_post_state[index] if joined_prefix else False
            if not post_state:
                self._atomic_copy(source_path, target)
            else:
                self._durable_readback(target, source_path.read_bytes(), phase="file")

        if metadata_joined:
            joined = joined_prefix[-1]
            metadata_intent = writer_intents[-1]
            if (
                joined.payload.get("path") != metadata_intent["path"]
                or joined.payload.get("action") != self.operation
                or joined.payload.get("kind") != metadata_intent["kind"]
                or joined.payload.get("op") != metadata_intent["op"]
                or joined.payload.get("sha256") != metadata_intent["sha256"]
                or not (metadata_pre or metadata_post)
            ):
                raise ProtocolRefusal("deployment_join_invalid", "joined metadata entry diverges from the planned install")
            if metadata_post:
                self._durable_readback(
                    metadata, planned_metadata, phase="metadata"
                )
            else:
                self._atomic_write_bytes(
                    metadata, planned_metadata, mode=0o600, phase="metadata"
                )
        else:
            if metadata.exists() and metadata.is_symlink():
                raise ProtocolRefusal("deployment_metadata_symlinked", "install metadata is a symlink")
            metadata_intent = writer_intents[-1]
            metadata_journal_payload = {
                "v": wiring_journal.JOURNAL_SCHEMA_VERSION,
                "ts": _journal_timestamp(),
                "actor": {"command": self.operation, "floatiVersion": source_sha[:12]},
                "action": self.operation, "kind": metadata_intent["kind"],
                "path": metadata_intent["path"], "op": metadata_intent["op"],
                "sha256": metadata_intent["sha256"],
            }
            if self.join_id is not None:
                metadata_journal_payload["join_id"] = self.join_id
            wiring_journal.append_entry(destination, metadata_journal_payload)
            self._fault("after_metadata_journal")
            if not metadata_post:
                self._atomic_write_bytes(
                    metadata, planned_metadata, mode=0o600, phase="metadata"
                )
            else:
                self._durable_readback(
                    metadata, planned_metadata, phase="metadata"
                )
        for relative in managed_paths:
            target = destination.joinpath(*PurePosixPath(relative).parts)
            if not target.is_file() or target.is_symlink() or _digest(target) != next(
                entry["sha256"] for entry in entries if entry["path"] == relative
            ):
                raise IntegrityFailure("deployment_write_invalid", f"installed digest mismatch: {relative}")

        for relative, _path in _destination_files(destination):
            if relative not in current_paths and relative not in preserved:
                preserved.append(relative)

        return {
            "operation": self.operation,
            "currency_mode": "committed-tree-ci" if self.committed_tree else "named-ref",
            "ref": self.ref,
            "source_sha": source_sha,
            "managed_paths": managed_paths,
            "foreign_preserved": sorted(set(preserved)),
            "wiring_journal": str(journal),
            "wiring_join_id": self.join_id,
            "status": "installed" if self.operation == "install" else "updated",
            "installer_shadow": installer_shadow,
        }

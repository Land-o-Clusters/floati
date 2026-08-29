"""Guarded installation and update of the exact Floati bundle."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path, PurePosixPath
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .errors import IntegrityFailure, ProtocolRefusal
from .installer_shadow import enumerate_installer_shadow
from .manifest import MANIFEST_NAME, verify_manifest
from .storage_identity import (
    INSTALL_METADATA_DIRECTORY,
    refuse_legacy_workspace_artifacts,
)
from . import wiring_journal


_GIT_ENV = {"GIT_ATTR_NOSYSTEM": "1"}
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


def _git(source: Path, args: Sequence[str]) -> str:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=source,
            env={**os.environ, **_GIT_ENV},
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ProtocolRefusal(
            "deployment_currency_unavailable",
            f"git could not inspect source: {exc}",
        ) from exc
    if result.returncode != 0:
        detail = result.stderr.strip() or "git inspection failed"
        raise ProtocolRefusal("deployment_currency_unavailable", detail)
    return result.stdout.strip()


def _manifest_entries(source: Path) -> List[Dict[str, str]]:
    errors = verify_manifest(source)
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


def _load_previous(destination: Path) -> Optional[Dict[str, str]]:
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
    files = raw.get("files") if isinstance(raw, dict) else None
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
    return previous


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
        parent = parent / part
        if parent.exists() and parent.is_symlink():
            raise ProtocolRefusal("deployment_foreign_collision", f"managed parent is a symlink: {relative}")
        if parent.exists() and not parent.is_dir():
            raise ProtocolRefusal("deployment_foreign_collision", f"managed parent is not a directory: {relative}")
        if not parent.exists():
            parent.mkdir()
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
    ) -> None:
        self.source_arg = source
        self.destination_arg = destination
        self.operation = operation
        self.ref = ref
        self.committed_tree = committed_tree

    def _check_currency(self, source: Path) -> str:
        status = _git(source, ("status", "--porcelain=v1", "--untracked-files=all"))
        if status:
            raise ProtocolRefusal(
                "deployment_currency_unavailable",
                "source tree is not clean",
            )
        head = _git(source, ("rev-parse", "--verify", "HEAD^{commit}"))
        target = _git(source, ("rev-parse", "--verify", f"{self.ref}^{{commit}}"))
        if not self.committed_tree and head != target:
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
    ) -> None:
        metadata_dir = destination / _METADATA_DIR
        if metadata_dir.exists() and metadata_dir.is_symlink():
            raise ProtocolRefusal("deployment_metadata_symlinked", "install metadata directory is a symlink")
        metadata_dir.mkdir(mode=0o700, exist_ok=True)
        metadata = _metadata_path(destination)
        if metadata.exists() and metadata.is_symlink():
            raise ProtocolRefusal("deployment_metadata_symlinked", "install metadata is a symlink")
        payload = {
            "schema_version": 0,
            "source_ref": self.ref,
            "source_sha": source_sha,
            "files": entries,
        }
        temporary = metadata.with_name(f".{metadata.name}.tmp")
        temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(temporary, metadata)

    def run(self) -> Dict[str, Any]:
        if self.operation not in {"install", "update"}:
            raise ProtocolRefusal("deployment_operation_invalid", "operation must be install or update")
        if not isinstance(self.ref, str) or not self.ref or any(char.isspace() for char in self.ref):
            raise ProtocolRefusal("deployment_ref_invalid", "ref must be one non-empty Git ref")
        if not isinstance(self.committed_tree, bool):
            raise ProtocolRefusal("deployment_mode_invalid", "committed_tree must be boolean")

        source = _entry_path(self.source_arg, "source")
        destination = _entry_path(self.destination_arg, "destination")
        if source.exists() and not source.is_dir():
            raise ProtocolRefusal("deployment_source_invalid", "source is not a directory")
        if destination.exists() and not destination.is_dir():
            raise ProtocolRefusal("deployment_destination_invalid", "destination is not a directory")
        if source.exists() and destination.exists() and source.resolve() == destination.resolve():
            raise ProtocolRefusal("deployment_source_destination_same", "source and destination must differ")

        refuse_legacy_workspace_artifacts(destination)

        source_sha = self._check_currency(source)
        entries = _manifest_entries(source)
        managed_paths = [entry["path"] for entry in entries]
        current_paths = set(managed_paths)

        installer_shadow = enumerate_installer_shadow(
            destination,
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
                str(installer_shadow["reason"]),
            )

        previous = _load_previous(destination) if destination.exists() else None
        self._preflight_collisions(destination, entries, previous)

        if not destination.exists():
            destination.mkdir(mode=0o700)
        elif destination.is_symlink():
            raise ProtocolRefusal("deployment_symlinked_entry", "destination is a symlink")

        preserved = self._remove_stale(destination, previous, current_paths)

        # U2 manifest-before-meaning (E3.1): the wiring journal entry for
        # each file is APPENDED before the file is written. A crash between
        # append and write leaves an honest extra entry whose target is
        # absent — uninstall replay reports that as already-done, never as
        # an untracked artifact.
        journal = wiring_journal.journal_path(destination)
        for entry in entries:
            relative = entry["path"]
            source_path = source.joinpath(*PurePosixPath(relative).parts)
            target = _ensure_parent(destination, relative)
            op = "replace" if relative in (previous or {}) else "create"
            wiring_journal.append_entry(destination, {
                "v": wiring_journal.JOURNAL_SCHEMA_VERSION,
                "ts": _journal_timestamp(),
                "actor": {"command": self.operation,
                          "floatiVersion": source_sha[:12]},
                "action": self.operation,
                "kind": "file",
                "path": str(target),
                "op": op,
                "sha256": entry["sha256"],
            })
            if target.exists() and target.is_symlink():
                raise ProtocolRefusal("deployment_foreign_collision", f"managed path is a symlink: {relative}")
            shutil.copy2(source_path, target)

        self._write_metadata(destination, entries, source_sha)
        wiring_journal.append_entry(destination, {
            "v": wiring_journal.JOURNAL_SCHEMA_VERSION,
            "ts": _journal_timestamp(),
            "actor": {"command": self.operation,
                      "floatiVersion": source_sha[:12]},
            "action": self.operation,
            "kind": "file",
            "path": str(_metadata_path(destination)),
            "op": "replace" if _metadata_path(destination).exists() else "create",
            "sha256": _digest(_metadata_path(destination)),
        })
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
            "status": "installed" if self.operation == "install" else "updated",
            "installer_shadow": installer_shadow,
        }

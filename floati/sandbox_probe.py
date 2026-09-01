"""Bounded write probes for the coordinates a sandboxed seat must reach."""

from __future__ import annotations

import errno
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .errors import ProtocolRefusal
from .git_process import fixed_git_command, fixed_git_environment
from .ids import uuid7_hex
from .registry import utc_now
from .root import FloatiRoot, validate_identifier


_COORDINATES = (
    "bus_cursors",
    "bus_receipts",
    "bus_ledger",
    "git_common_dir",
    "git_worktree_admin_dir",
)


def _observed_at(value: Optional[datetime]) -> str:
    if value is None:
        return utc_now()
    if value.tzinfo is None or value.utcoffset() is None:
        raise ProtocolRefusal("time_invalid", "probe clock must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _fact(
    coordinate: str,
    path: Optional[Path],
    verdict: str,
    reason_code: str,
    observed_at: str,
    *,
    errno_name: Optional[str] = None,
    residue_path: Optional[Path] = None,
) -> dict:
    return {
        "schema_version": 0,
        "coordinate": coordinate,
        "path": None if path is None else str(path),
        "verdict": verdict,
        "reason_code": reason_code,
        "errno_name": errno_name,
        "observed_at": observed_at,
        "residue_path": None if residue_path is None else str(residue_path),
    }


def _probe_directory(path: Optional[Path], coordinate: str, observed_at: str) -> dict:
    if path is None:
        return _fact(coordinate, None, "unknown", "coordinate_underivable", observed_at)
    path = Path(path)
    if path.is_symlink():
        return _fact(coordinate, path, "unknown", "path_not_directory", observed_at)
    if not path.exists():
        return _fact(coordinate, path, "unknown", "path_absent", observed_at)
    if not path.is_dir():
        return _fact(coordinate, path, "unknown", "path_not_directory", observed_at)

    probe_path = path / f".floati-write-probe-{uuid7_hex()}"
    fd: Optional[int] = None
    created = False
    result: Optional[dict] = None
    try:
        try:
            fd = os.open(os.fspath(probe_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            created = True
            os.write(fd, b"x")
            os.fsync(fd)
            result = _fact(coordinate, path, "writable", "probe_succeeded", observed_at)
        except OSError as exc:
            if exc.errno in (errno.EACCES, errno.EPERM):
                result = _fact(
                    coordinate, path, "refused", "permission_denied", observed_at,
                    errno_name=errno.errorcode.get(exc.errno),
                )
            elif exc.errno == errno.EROFS:
                result = _fact(
                    coordinate, path, "refused", "read_only_filesystem", observed_at,
                    errno_name=errno.errorcode.get(exc.errno),
                )
            else:
                result = _fact(
                    coordinate, path, "unknown", "probe_errno_unmapped", observed_at,
                    errno_name=errno.errorcode.get(exc.errno) if exc.errno is not None else None,
                )
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        if created:
            try:
                os.unlink(os.fspath(probe_path))
            except OSError:
                if result is not None and result["verdict"] == "writable":
                    result["residue_path"] = str(probe_path)
    assert result is not None
    return result


def _git_coordinate(repository: Optional[Path], executable: str, argument: str) -> Optional[Path]:
    if repository is None:
        return None
    repo = Path(repository).expanduser().resolve()
    try:
        completed = subprocess.run(
            fixed_git_command(executable, repo, ("rev-parse", argument)),
            env=fixed_git_environment(executable),
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0 or not completed.stdout.strip():
        return None
    raw = Path(completed.stdout.strip())
    return (repo / raw).resolve() if not raw.is_absolute() else raw.resolve()


def probe_write_set(
    root: FloatiRoot,
    node_id: str,
    *,
    repository: Optional[Path] = None,
    git_executable: str = "/usr/bin/git",
    now: Optional[datetime] = None,
) -> tuple[dict, ...]:
    """Probe all five sandbox coordinates in their canonical order."""

    validate_identifier(node_id, "node")
    observed_at = _observed_at(now)
    coordinates = {
        "bus_cursors": root.resolve_relative("cursors"),
        "bus_receipts": root.resolve_relative("receipts/deliveries"),
        "bus_ledger": root.resolve_relative("events.jsonl").parent,
        "git_common_dir": _git_coordinate(repository, git_executable, "--git-common-dir"),
        "git_worktree_admin_dir": _git_coordinate(repository, git_executable, "--git-dir"),
    }
    return tuple(
        _probe_directory(coordinates[coordinate], coordinate, observed_at)
        for coordinate in _COORDINATES
    )

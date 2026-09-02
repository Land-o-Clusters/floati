"""Explicit preserved-root sanitation through the host's Trash only.

This module is deliberately separate from :mod:`floati.uninstall`.  Uninstall
owns manifest-exact tool bytes; purge owns only the user roots named by the
caller.  The writer inventories every root before moving anything and emits a
receipt for every regular file it finds.  Symlinks and non-regular entries are
foreign to this contract and refuse the complete operation.
"""

from __future__ import annotations

import argparse
import ctypes
import errno
import hashlib
import os
import pwd
import re
import stat
import sys
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from .errors import DurabilityFailure, ProtocolRefusal


_STAMP_FORMAT = "%Y%m%dT%H%M%SZ"
_TRASH_PREFIX = "floati-"
_SAFE_STAMP = re.compile(r"^[A-Za-z0-9._-]+$")

# The exclusive-rename primitive is a PAIRED (function, dirfd sentinel, flag)
# tuple bound per platform and never a reused constant: the same small integers
# name different operations on each host.  0x4 is RENAME_EXCL on macOS and
# RENAME_WHITEOUT on Linux; 0x1 is RENAME_NOREPLACE on Linux and RENAME_SECLUDE
# on macOS.  A mis-paired constant is not an error, it is a different syscall
# that succeeds.  (docs/evidence/ci-rt-1b-gate-2026-08-30.md section 2.)
_DARWIN_AT_FDCWD = -2  # MacOSX.sdk/usr/include/sys/fcntl.h
_LINUX_AT_FDCWD = -100  # Linux include/uapi/linux/fcntl.h, glibc io/fcntl.h
_RENAME_EXCL = 0x00000004  # MacOSX.sdk/usr/include/sys/stdio.h
_RENAME_NOREPLACE = 0x00000001  # Linux include/uapi/linux/fs.h

_DARWIN_TRASH_NAME = ".Trash"
_XDG_DEFAULT_DATA_HOME = (".local", "share")
_XDG_TRASH_NAME = "Trash"
_XDG_TRASH_FILES = "files"
_XDG_TRASH_INFO = "info"
_TRASHINFO_SUFFIX = ".trashinfo"
_TRASHINFO_HEADER = "[Trash Info]"
_TRASHINFO_PATH_FIELD = "Path"
_TRASHINFO_DATE_FIELD = "DeletionDate"
_TRASHINFO_DATE_FORMAT = "%Y-%m-%dT%H:%M:%S"


class _PurgeRecoveryFailure(DurabilityFailure):
    """A post-mutation purge failure with closed recovery evidence."""

    def __init__(self, code: str, detail: str, evidence: Dict[str, Any]) -> None:
        super().__init__(code, detail)
        self.evidence = evidence


_LIBC = ctypes.CDLL(None, use_errno=True)
_RENAMEATX_NP = getattr(_LIBC, "renameatx_np", None)
if _RENAMEATX_NP is not None:
    _RENAMEATX_NP.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    _RENAMEATX_NP.restype = ctypes.c_int
_RENAMEAT2 = getattr(_LIBC, "renameat2", None)
if _RENAMEAT2 is not None:
    _RENAMEAT2.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    _RENAMEAT2.restype = ctypes.c_int


def _exclusive_rename_primitive() -> Tuple[Any, int, int]:
    """Bind the exclusive-rename primitive for the DECLARED host platform.

    Selection is by ``sys.platform`` and never by probing which symbol happens
    to resolve: a host that exports the other platform's symbol must not be
    handed the other platform's flag.  The three values are returned as one
    tuple so a call site cannot mix a function from one platform with a flag
    from the other.
    """

    if sys.platform == "darwin":
        function, directory, flag = _RENAMEATX_NP, _DARWIN_AT_FDCWD, _RENAME_EXCL
    else:
        function, directory, flag = _RENAMEAT2, _LINUX_AT_FDCWD, _RENAME_NOREPLACE
    if function is None:
        raise OSError(
            errno.ENOTSUP,
            "the host has no exclusive rename primitive",
        )
    return function, directory, flag


def _timestamp() -> str:
    return datetime.now(timezone.utc).strftime(_STAMP_FORMAT)


def _lexists(path: Path) -> bool:
    return os.path.lexists(str(path))


def _rename_exclusive(source: os.PathLike[str], destination: os.PathLike[str]) -> None:
    """Atomically rename without replacing a filesystem-equivalent target."""

    function, directory, flag = _exclusive_rename_primitive()
    ctypes.set_errno(0)
    result = function(
        directory,
        os.fsencode(source),
        directory,
        os.fsencode(destination),
        flag,
    )
    if result != 0:
        error = ctypes.get_errno() or errno.EIO
        raise OSError(error, os.strerror(error), str(destination))


def _rename_exclusive_at(
    source_dir_fd: int,
    source_name: str,
    destination_dir_fd: int,
    destination_name: str,
) -> None:
    """Atomically rename relative to held source and destination directories."""

    function, _, flag = _exclusive_rename_primitive()
    ctypes.set_errno(0)
    result = function(
        source_dir_fd,
        os.fsencode(source_name),
        destination_dir_fd,
        os.fsencode(destination_name),
        flag,
    )
    if result != 0:
        error = ctypes.get_errno() or errno.EIO
        raise OSError(error, os.strerror(error), destination_name)


def _digest_regular(path: Path) -> Tuple[str, os.stat_result]:
    flags = os.O_RDONLY
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ProtocolRefusal(
            "purge_foreign_entry",
            f"could not safely open regular file: {path}",
        ) from exc
    try:
        identity = os.fstat(descriptor)
        if not stat.S_ISREG(identity.st_mode):
            raise ProtocolRefusal(
                "purge_foreign_entry",
                f"purge accepts regular files only: {path}",
            )
        digest = hashlib.sha256()
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
        return digest.hexdigest(), identity
    except OSError as exc:
        raise ProtocolRefusal(
            "purge_foreign_entry",
            f"could not read regular file: {path}",
        ) from exc
    finally:
        os.close(descriptor)


@dataclass(frozen=True)
class _ObservedFile:
    root: Path
    path: Path
    relative: str
    sha256: str
    size: int
    device: int
    inode: int


@dataclass(frozen=True)
class _ObservedRoot:
    path: Path
    device: int
    inode: int
    files: Tuple[_ObservedFile, ...]


@dataclass(frozen=True)
class _PlannedFile:
    root: Path
    path: Path
    relative: str
    sha256: str
    size: int
    device: int
    inode: int
    trash: Path


@dataclass(frozen=True)
class _PlannedRoot:
    path: Path
    trash: Path
    device: int
    inode: int


@dataclass
class _MoveBinding:
    root: _PlannedRoot
    root_fd: int
    parent_fd: int
    parent_device: int
    parent_inode: int

    def close(self) -> None:
        os.close(self.root_fd)
        os.close(self.parent_fd)


@dataclass(frozen=True)
class PurgePlan:
    """An immutable, digest-bound preview of one explicit purge request."""

    roots: Tuple[_PlannedRoot, ...]
    files: Tuple[_PlannedFile, ...]
    trash_dir: Path
    trash_device: int
    trash_inode: int
    timestamp: str

    def _root_receipts(self, status: str) -> List[Dict[str, str]]:
        return [
            {
                "original": str(root.path),
                "trash": str(root.trash),
                "status": status,
            }
            for root in self.roots
        ]

    def _file_receipts(self, status: str) -> List[Dict[str, Any]]:
        observation = "post-move-verified" if status == "trashed" else "plan-scan"
        return [
            {
                "root": str(file.root),
                "original": str(file.path),
                "relative": file.relative,
                "trash": str(file.trash),
                "sha256": file.sha256,
                "sha256_observation": observation,
                "size": file.size,
                "status": status,
            }
            for file in self.files
        ]

    def render(self) -> str:
        lines = [
            "PURGE PREVIEW - Trash only; deletes nothing",
            f"Trash: {self.trash_dir}",
            "Roots:",
        ]
        for root in self.roots:
            lines.append(f"  - {root.path} -> {root.trash}")
        lines.append("Files:")
        for file in self.files:
            lines.append(
                f"  - {file.path} -> {file.trash} "
                f"(sha256={file.sha256}, size={file.size})"
            )
        if not self.files:
            lines.append("  - (no regular files)")
        lines.append("Foreign entries: refuse before any root moves")
        lines.append("Hard deletion: never")
        return "\n".join(lines)

    def evidence(self, *, dry_run: bool, status: str) -> Dict[str, Any]:
        files = self._file_receipts(status)
        return {
            "operation": "purge",
            "dry_run": dry_run,
            "trash_only": True,
            "trash_dir": str(self.trash_dir),
            "roots": [str(root.path) for root in self.roots],
            "root_receipts": self._root_receipts(status),
            "file_receipts": files,
            "preview": self.render(),
            "root_count": len(self.roots),
            "file_count": len(files),
            "moved_root_count": 0 if dry_run else len(self.roots),
            "trashed_count": 0 if dry_run else len(files),
            "foreign_files": [],
        }


def _account_home() -> Path:
    try:
        return Path(pwd.getpwuid(os.getuid()).pw_dir).resolve(strict=True)
    except (KeyError, OSError) as exc:
        raise ProtocolRefusal(
            "purge_home_unavailable",
            "the current account home directory cannot be resolved",
        ) from exc


def _xdg_data_home(home: Path) -> Path:
    """The freedesktop data home, bounded by the account home.

    ``$XDG_DATA_HOME`` is honoured only when it is absolute and lies inside the
    account home.  The fixed-Trash authority is the reason: purge's destination
    may not be steered outside the account by the caller's environment, and an
    unbounded read of this variable would reintroduce exactly the redirection
    the fixed authority exists to refuse.  Anything else falls back to the
    spec's own default, ``$HOME/.local/share``.
    """

    default = home.joinpath(*_XDG_DEFAULT_DATA_HOME)
    declared = os.environ.get("XDG_DATA_HOME")
    if not declared:
        return default
    candidate = Path(declared)
    if not candidate.is_absolute() or ".." in candidate.parts:
        return default
    if candidate != home and not candidate.is_relative_to(home):
        return default
    return candidate


def _trash_dir() -> Path:
    """The directory Trashed roots are moved into, bound by declared platform.

    macOS keeps ``~/.Trash``.  Every other host is the freedesktop Trash:
    ``$XDG_DATA_HOME/Trash`` with its ``files/`` and ``info/`` halves, default
    ``~/.local/share/Trash``.  An absent Trash is the typed refusal
    ``purge_trash_unavailable`` carrying the exact directory to create — never
    a delete in place, never a silent no-op.
    """

    home = _account_home()
    if sys.platform == "darwin":
        trash = home / _DARWIN_TRASH_NAME
        if trash.is_symlink() or not trash.is_dir():
            raise ProtocolRefusal(
                "purge_trash_unavailable",
                f"fixed Trash directory is unavailable: {trash}",
                f"create the Trash directory: {trash}",
            )
        return trash.resolve()

    root = _xdg_data_home(home) / _XDG_TRASH_NAME
    files = root / _XDG_TRASH_FILES
    info = root / _XDG_TRASH_INFO
    missing = [
        half
        for half in (files, info)
        if half.is_symlink() or not half.is_dir()
    ]
    if missing:
        raise ProtocolRefusal(
            "purge_trash_unavailable",
            "fixed Trash directory is unavailable: "
            + ", ".join(str(half) for half in missing),
            "create the freedesktop Trash directories: "
            + " ".join(str(half) for half in (files, info)),
        )
    return files.resolve()


def _trash_info_dir(files: Path) -> Optional[Path]:
    """The freedesktop ``info/`` half for a resolved ``files/`` half.

    ``None`` on macOS, whose Trash has no info half.  ``None`` also when the
    resolved destination is not a freedesktop ``files/`` directory — a state
    :func:`_trash_dir` cannot produce, since it validates both halves together,
    and which therefore only arises when a caller has replaced the resolver.
    """

    if sys.platform == "darwin" or files.name != _XDG_TRASH_FILES:
        return None
    info = files.parent / _XDG_TRASH_INFO
    if info.is_symlink() or not info.is_dir():
        return None
    return info.resolve()


def _trashinfo_payload(original: Path, deleted_at: datetime) -> bytes:
    """One freedesktop trashinfo record: origin path and deletion time."""

    quoted = urllib.parse.quote(str(original), safe="/")
    stamp = deleted_at.strftime(_TRASHINFO_DATE_FORMAT)
    return (
        f"{_TRASHINFO_HEADER}\n"
        f"{_TRASHINFO_PATH_FIELD}={quoted}\n"
        f"{_TRASHINFO_DATE_FIELD}={stamp}\n"
    ).encode("utf-8")


def _write_trashinfo(
    info_dir_fd: int,
    name: str,
    original: Path,
    deleted_at: datetime,
) -> None:
    """Record one trashinfo beside the moved root, exclusively by name."""

    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    handle = os.open(name, flags, 0o600, dir_fd=info_dir_fd)
    try:
        os.write(handle, _trashinfo_payload(original, deleted_at))
    finally:
        os.close(handle)


def _absolute_path(value: os.PathLike[str] | str, *, label: str) -> Path:
    try:
        path = Path(value)
    except (TypeError, ValueError, OSError) as exc:
        raise ProtocolRefusal("purge_path_invalid", f"{label} is not a path") from exc
    if not path.is_absolute():
        raise ProtocolRefusal(
            "purge_root_absolute_required",
            f"{label} must be absolute",
        )
    if ".." in path.parts:
        raise ProtocolRefusal(
            "purge_path_invalid",
            f"{label} must not contain parent traversal",
        )
    return path


def _root_paths(
    roots: Sequence[os.PathLike[str] | str] | os.PathLike[str] | str,
) -> List[Path]:
    if isinstance(roots, (str, os.PathLike)):
        values: Iterable[os.PathLike[str] | str] = [roots]
    else:
        values = roots
    try:
        raw_values = list(values)
    except TypeError as exc:
        raise ProtocolRefusal("purge_roots_required", "roots must be a sequence") from exc
    if not raw_values:
        raise ProtocolRefusal(
            "purge_roots_required",
            "at least one explicit preserved user root is required",
        )

    normalized: List[Path] = []
    for value in raw_values:
        path = _absolute_path(value, label="preserved root")
        if path.is_symlink():
            raise ProtocolRefusal(
                "purge_root_symlink",
                f"preserved root must not be a symlink: {path}",
            )
        if not path.exists():
            raise ProtocolRefusal(
                "purge_root_missing",
                f"preserved root does not exist: {path}",
            )
        if not path.is_dir():
            raise ProtocolRefusal(
                "purge_foreign_file",
                f"a regular file is not a preserved root: {path}",
            )
        try:
            canonical = path.resolve(strict=True)
        except OSError as exc:
            raise ProtocolRefusal(
                "purge_root_invalid",
                f"preserved root cannot be resolved: {path}",
            ) from exc
        if canonical != path:
            raise ProtocolRefusal(
                "purge_root_symlink_ancestor",
                f"preserved root {path} resolves through an alias to {canonical}",
            )
        if canonical == Path(canonical.anchor):
            raise ProtocolRefusal(
                "purge_root_invalid",
                "the filesystem root is not a lawful preserved user root",
            )
        if canonical == _account_home():
            raise ProtocolRefusal(
                "purge_root_invalid",
                "the home directory is not a lawful preserved user root",
            )
        normalized.append(canonical)

    seen = set()
    for path in normalized:
        if path in seen:
            raise ProtocolRefusal(
                "purge_root_overlap",
                f"the same preserved root was supplied more than once: {path}",
            )
        seen.add(path)
    for index, path in enumerate(normalized):
        for other in normalized[index + 1 :]:
            if path.is_relative_to(other) or other.is_relative_to(path):
                raise ProtocolRefusal(
                    "purge_root_overlap",
                    f"preserved roots overlap: {path} and {other}",
                )
    return normalized


def _validate_trash(path: Path) -> Path:
    if not path.is_absolute():
        raise ProtocolRefusal("purge_trash_invalid", "Trash directory must be absolute")
    if path.is_symlink() or not path.is_dir():
        raise ProtocolRefusal(
            "purge_trash_unavailable",
            f"fixed Trash directory is unavailable: {path}",
        )
    try:
        return path.resolve(strict=True)
    except OSError as exc:
        raise ProtocolRefusal(
            "purge_trash_unavailable",
            f"fixed Trash directory cannot be resolved: {path}",
        ) from exc


def _trash_device(path: Path) -> int:
    try:
        identity = os.stat(path, follow_symlinks=False)
    except OSError as exc:
        raise ProtocolRefusal(
            "purge_trash_unavailable",
            f"fixed Trash directory cannot be inspected: {path}",
        ) from exc
    if not stat.S_ISDIR(identity.st_mode):
        raise ProtocolRefusal(
            "purge_trash_unavailable",
            f"fixed Trash directory is not a directory: {path}",
        )
    return identity.st_dev


def _directory_flags() -> int:
    return os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)


def _stat_at(directory_fd: int, name: str) -> Optional[os.stat_result]:
    try:
        return os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None


def _identity_matches(identity: Optional[os.stat_result], device: int, inode: int) -> bool:
    return bool(
        identity is not None
        and stat.S_ISDIR(identity.st_mode)
        and (identity.st_dev, identity.st_ino) == (device, inode)
    )


def _digest_regular_at(
    directory_fd: int,
    name: str,
    display_path: Path,
) -> Tuple[str, os.stat_result]:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(name, flags, dir_fd=directory_fd)
    except OSError as exc:
        raise ProtocolRefusal(
            "purge_foreign_entry",
            f"could not safely open regular file: {display_path}",
        ) from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise ProtocolRefusal(
                "purge_foreign_entry",
                f"purge accepts regular files only: {display_path}",
            )
        digest = hashlib.sha256()
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
        after = os.fstat(descriptor)
        before_key = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
        after_key = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
        if before_key != after_key:
            raise ProtocolRefusal(
                "purge_identity_changed",
                f"file changed while it was inventoried: {display_path}",
            )
        return digest.hexdigest(), after
    except OSError as exc:
        raise ProtocolRefusal(
            "purge_foreign_entry",
            f"could not read regular file: {display_path}",
        ) from exc
    finally:
        os.close(descriptor)


def _scan_open_root(root_fd: int, root: Path) -> _ObservedRoot:
    """Inventory a root through held descriptors, never through a mutable ancestor."""

    root_identity = os.fstat(root_fd)
    if not stat.S_ISDIR(root_identity.st_mode):
        raise ProtocolRefusal(
            "purge_root_invalid",
            f"preserved root is not a regular directory: {root}",
        )

    observations: List[_ObservedFile] = []
    pending: List[Tuple[int, Path, str]] = [(os.dup(root_fd), root, "")]
    try:
        while pending:
            directory_fd, display_directory, relative_directory = pending.pop()
            try:
                try:
                    with os.scandir(directory_fd) as scanned:
                        entries = sorted(list(scanned), key=lambda entry: entry.name)
                except OSError as exc:
                    raise ProtocolRefusal(
                        "purge_root_unreadable",
                        "preserved root subtree cannot be completely inventoried: "
                        f"{display_directory}",
                    ) from exc
                for entry in entries:
                    display_path = display_directory / entry.name
                    relative = (
                        f"{relative_directory}/{entry.name}"
                        if relative_directory
                        else entry.name
                    )
                    try:
                        identity = entry.stat(follow_symlinks=False)
                    except OSError as exc:
                        raise ProtocolRefusal(
                            "purge_root_unreadable",
                            f"preserved root entry cannot be inspected: {display_path}",
                        ) from exc
                    if stat.S_ISLNK(identity.st_mode):
                        raise ProtocolRefusal(
                            "purge_foreign_entry",
                            "symlinked entry is foreign to the purge contract: "
                            f"{display_path}",
                        )
                    if stat.S_ISDIR(identity.st_mode):
                        try:
                            child_fd = os.open(
                                entry.name,
                                _directory_flags(),
                                dir_fd=directory_fd,
                            )
                        except OSError as exc:
                            raise ProtocolRefusal(
                                "purge_root_unreadable",
                                "preserved root subtree cannot be completely inventoried: "
                                f"{display_path}",
                            ) from exc
                        opened = os.fstat(child_fd)
                        if (opened.st_dev, opened.st_ino) != (
                            identity.st_dev,
                            identity.st_ino,
                        ):
                            os.close(child_fd)
                            raise ProtocolRefusal(
                                "purge_identity_changed",
                                f"directory changed while it was inventoried: {display_path}",
                            )
                        pending.append((child_fd, display_path, relative))
                        continue
                    if not stat.S_ISREG(identity.st_mode):
                        raise ProtocolRefusal(
                            "purge_foreign_entry",
                            "non-regular entry is foreign to the purge contract: "
                            f"{display_path}",
                        )
                    digest, reopened = _digest_regular_at(
                        directory_fd,
                        entry.name,
                        display_path,
                    )
                    if (reopened.st_dev, reopened.st_ino, reopened.st_size) != (
                        identity.st_dev,
                        identity.st_ino,
                        identity.st_size,
                    ):
                        raise ProtocolRefusal(
                            "purge_identity_changed",
                            f"file changed while it was inventoried: {display_path}",
                        )
                    observations.append(
                        _ObservedFile(
                            root=root,
                            path=root / relative,
                            relative=relative,
                            sha256=digest,
                            size=identity.st_size,
                            device=identity.st_dev,
                            inode=identity.st_ino,
                        )
                    )
            finally:
                os.close(directory_fd)
    finally:
        for directory_fd, _, _ in pending:
            os.close(directory_fd)
    observations.sort(key=lambda item: item.relative)
    return _ObservedRoot(
        path=root,
        device=root_identity.st_dev,
        inode=root_identity.st_ino,
        files=tuple(observations),
    )


def _scan_root(root: Path) -> _ObservedRoot:
    try:
        root_identity = os.lstat(root)
    except OSError as exc:
        raise ProtocolRefusal(
            "purge_root_missing",
            f"preserved root could not be opened: {root}",
        ) from exc
    if stat.S_ISLNK(root_identity.st_mode) or not stat.S_ISDIR(root_identity.st_mode):
        raise ProtocolRefusal(
            "purge_root_invalid",
            f"preserved root is not a regular directory: {root}",
        )

    observations: List[_ObservedFile] = []
    pending = [root]
    while pending:
        directory = pending.pop()
        try:
            with os.scandir(directory) as scanned:
                entries = sorted(list(scanned), key=lambda entry: entry.name)
        except OSError as exc:
            raise ProtocolRefusal(
                "purge_root_unreadable",
                f"preserved root subtree cannot be completely inventoried: {directory}",
            ) from exc
        child_directories: List[Path] = []
        for entry in entries:
            path = Path(entry.path)
            try:
                identity = entry.stat(follow_symlinks=False)
            except OSError as exc:
                raise ProtocolRefusal(
                    "purge_root_unreadable",
                    f"preserved root entry cannot be inspected: {path}",
                ) from exc
            if stat.S_ISLNK(identity.st_mode):
                raise ProtocolRefusal(
                    "purge_foreign_entry",
                    f"symlinked entry is foreign to the purge contract: {path}",
                )
            if stat.S_ISDIR(identity.st_mode):
                child_directories.append(path)
                continue
            if not stat.S_ISREG(identity.st_mode):
                raise ProtocolRefusal(
                    "purge_foreign_entry",
                    f"non-regular entry is foreign to the purge contract: {path}",
                )
            digest, reopened = _digest_regular(path)
            if (reopened.st_dev, reopened.st_ino, reopened.st_size) != (
                identity.st_dev,
                identity.st_ino,
                identity.st_size,
            ):
                raise ProtocolRefusal(
                    "purge_identity_changed",
                    f"file changed while it was inventoried: {path}",
                )
            observations.append(
                _ObservedFile(
                    root=root,
                    path=path,
                    relative=path.relative_to(root).as_posix(),
                    sha256=digest,
                    size=identity.st_size,
                    device=identity.st_dev,
                    inode=identity.st_ino,
                )
            )
        pending.extend(reversed(child_directories))
    observations.sort(key=lambda item: item.relative)
    return _ObservedRoot(
        path=root,
        device=root_identity.st_dev,
        inode=root_identity.st_ino,
        files=tuple(observations),
    )


def _target_for(
    root: Path,
    trash: Path,
    timestamp: str,
    reserved: set[Path],
) -> Path:
    info = _trash_info_dir(trash)
    base = trash / f"{_TRASH_PREFIX}{root.name}-{timestamp}"
    target = base
    counter = 1
    while (
        _lexists(target)
        or target in reserved
        or (
            info is not None
            and _lexists(info / f"{target.name}{_TRASHINFO_SUFFIX}")
        )
    ):
        target = trash / f"{base.name}-{counter}"
        counter += 1
    reserved.add(target)
    return target


class PurgeWriter:
    """Preview and move only the explicitly named preserved user roots."""

    def __init__(
        self,
        roots: Sequence[os.PathLike[str] | str] | os.PathLike[str] | str,
        *,
        dry_run: bool = False,
        timestamp: Optional[str] = None,
        timestamp_factory: Callable[[], str] = _timestamp,
    ) -> None:
        if not isinstance(dry_run, bool):
            raise ProtocolRefusal("purge_mode_invalid", "dry_run must be boolean")
        self.roots_arg = roots
        self.dry_run = dry_run
        self.timestamp = timestamp
        self.timestamp_factory = timestamp_factory

    def _trash(self) -> Path:
        return _validate_trash(_trash_dir())

    def plan(self) -> PurgePlan:
        roots = _root_paths(self.roots_arg)
        trash = self._trash()
        canonical_home = _account_home()
        for root in roots:
            if root == trash or root.is_relative_to(trash):
                raise ProtocolRefusal(
                    "purge_trash_root",
                    f"a path inside Trash is foreign to the purge contract: {root}",
                )
            if trash.is_relative_to(root):
                raise ProtocolRefusal(
                    "purge_trash_root",
                    f"Trash cannot be inside a preserved root: {root}",
                )
            if root == canonical_home:
                raise ProtocolRefusal(
                    "purge_root_invalid",
                    "the home directory is not a lawful preserved user root",
                )

        stamp = self.timestamp or self.timestamp_factory()
        if not isinstance(stamp, str) or not _SAFE_STAMP.fullmatch(stamp):
            raise ProtocolRefusal("purge_timestamp_invalid", "purge timestamp must be text")
        observed = [_scan_root(root) for root in roots]
        trash_identity = os.stat(trash, follow_symlinks=False)
        reserved: set[Path] = set()
        planned_roots: List[_PlannedRoot] = []
        planned_files: List[_PlannedFile] = []
        for snapshot in observed:
            target = _target_for(snapshot.path, trash, stamp, reserved)
            planned_roots.append(
                _PlannedRoot(
                    path=snapshot.path,
                    trash=target,
                    device=snapshot.device,
                    inode=snapshot.inode,
                )
            )
            for file in snapshot.files:
                planned_files.append(
                    _PlannedFile(
                        root=file.root,
                        path=file.path,
                        relative=file.relative,
                        sha256=file.sha256,
                        size=file.size,
                        device=file.device,
                        inode=file.inode,
                        trash=target / file.relative,
                    )
                )
        return PurgePlan(
            roots=tuple(planned_roots),
            files=tuple(planned_files),
            trash_dir=trash,
            trash_device=trash_identity.st_dev,
            trash_inode=trash_identity.st_ino,
            timestamp=stamp,
        )

    @staticmethod
    def _same_files(expected: Sequence[_PlannedFile], observed: Sequence[_ObservedFile]) -> bool:
        if len(expected) != len(observed):
            return False
        return all(
            (
                expected_file.path == observed_file.path
                and expected_file.relative == observed_file.relative
                and expected_file.sha256 == observed_file.sha256
                and expected_file.size == observed_file.size
                and expected_file.device == observed_file.device
                and expected_file.inode == observed_file.inode
            )
            for expected_file, observed_file in zip(expected, observed)
        )

    def _validate_plan_layout(self, plan: PurgePlan) -> None:
        if not isinstance(plan, PurgePlan):
            raise ProtocolRefusal("purge_plan_invalid", "purge plan has the wrong type")
        if not isinstance(plan.timestamp, str) or not _SAFE_STAMP.fullmatch(plan.timestamp):
            raise ProtocolRefusal("purge_plan_invalid", "purge plan timestamp is invalid")
        fixed_trash = self._trash()
        if plan.trash_dir != fixed_trash:
            raise ProtocolRefusal(
                "purge_plan_invalid",
                "purge plan does not name the fixed Trash directory",
            )
        requested_roots = tuple(_root_paths(self.roots_arg))
        if tuple(root.path for root in plan.roots) != requested_roots:
            raise ProtocolRefusal(
                "purge_plan_invalid",
                "purge plan roots do not match the writer request",
            )

        planned_by_path: Dict[Path, _PlannedRoot] = {}
        targets: set[Path] = set()
        for root in plan.roots:
            base_name = f"{_TRASH_PREFIX}{root.path.name}-{plan.timestamp}"
            target_name = root.trash.name
            suffix = target_name.removeprefix(f"{base_name}-")
            valid_name = target_name == base_name or (
                target_name.startswith(f"{base_name}-")
                and suffix.isdigit()
                and not suffix.startswith("0")
            )
            if root.trash.parent != plan.trash_dir or not valid_name:
                raise ProtocolRefusal(
                    "purge_plan_invalid",
                    f"purge plan has an invalid Trash destination for {root.path}",
                )
            if root.path in planned_by_path or root.trash in targets:
                raise ProtocolRefusal(
                    "purge_plan_invalid",
                    "purge plan contains duplicate roots or Trash destinations",
                )
            planned_by_path[root.path] = root
            targets.add(root.trash)

        seen_files: set[Tuple[Path, str]] = set()
        for file in plan.files:
            root = planned_by_path.get(file.root)
            relative = Path(file.relative)
            if (
                root is None
                or not file.relative
                or relative.is_absolute()
                or file.relative != relative.as_posix()
                or any(part in ("", ".", "..") for part in relative.parts)
                or file.path != root.path / relative
                or file.trash != root.trash / relative
                or (file.root, file.relative) in seen_files
            ):
                raise ProtocolRefusal(
                    "purge_plan_invalid",
                    "purge plan contains a file outside its bound root and Trash target",
                )
            seen_files.add((file.root, file.relative))

    def _verify(self, plan: PurgePlan) -> None:
        self._validate_plan_layout(plan)
        trash = self._trash()
        if trash != plan.trash_dir:
            raise ProtocolRefusal(
                "purge_trash_changed",
                "the fixed Trash directory changed after preview",
            )
        trash_identity = os.stat(trash, follow_symlinks=False)
        if (trash_identity.st_dev, trash_identity.st_ino) != (
            plan.trash_device,
            plan.trash_inode,
        ):
            raise ProtocolRefusal(
                "purge_trash_changed",
                "the fixed Trash directory identity changed after preview",
            )
        trash_device = trash_identity.st_dev
        cross_device = [root.path for root in plan.roots if root.device != trash_device]
        if cross_device:
            raise ProtocolRefusal(
                "purge_cross_device",
                "every preserved root must share the fixed Trash filesystem before any move: "
                + ", ".join(str(path) for path in cross_device),
            )
        expected_by_root: Dict[Path, List[_PlannedFile]] = {
            root.path: [] for root in plan.roots
        }
        for file in plan.files:
            expected_by_root[file.root].append(file)
        for root in plan.roots:
            try:
                identity = os.lstat(root.path)
            except OSError as exc:
                raise ProtocolRefusal(
                    "purge_identity_changed",
                    f"preserved root disappeared after preview: {root.path}",
                ) from exc
            if (
                stat.S_ISLNK(identity.st_mode)
                or not stat.S_ISDIR(identity.st_mode)
                or (identity.st_dev, identity.st_ino) != (root.device, root.inode)
            ):
                raise ProtocolRefusal(
                    "purge_identity_changed",
                    f"preserved root identity changed after preview: {root.path}",
                )
            current = _scan_root(root.path)
            if not self._same_files(expected_by_root[root.path], current.files):
                raise ProtocolRefusal(
                    "purge_identity_changed",
                    f"a preserved file changed after preview: {root.path}",
                )
        for root in plan.roots:
            if _lexists(root.trash):
                raise ProtocolRefusal(
                    "purge_trash_collision",
                    f"planned Trash destination became occupied: {root.trash}",
                )

    def _open_verified_root_at_move(
        self,
        root: _PlannedRoot,
        expected: Sequence[_PlannedFile],
    ) -> _MoveBinding:
        parent_fd: Optional[int] = None
        root_fd: Optional[int] = None
        try:
            parent_fd = os.open(root.path.parent, _directory_flags())
            parent_identity = os.fstat(parent_fd)
            root_fd = os.open(root.path.name, _directory_flags(), dir_fd=parent_fd)
        except OSError as exc:
            if root_fd is not None:
                os.close(root_fd)
            if parent_fd is not None:
                os.close(parent_fd)
            raise ProtocolRefusal(
                "purge_identity_changed",
                f"preserved root could not be opened at the move boundary: {root.path}",
            ) from exc
        try:
            opened = os.fstat(root_fd)
            if (
                not stat.S_ISDIR(opened.st_mode)
                or (opened.st_dev, opened.st_ino) != (root.device, root.inode)
            ):
                raise ProtocolRefusal(
                    "purge_identity_changed",
                    f"preserved root identity changed at the move boundary: {root.path}",
                )
            current = _scan_open_root(root_fd, root.path)
            if not self._same_files(expected, current.files):
                raise ProtocolRefusal(
                    "purge_identity_changed",
                    f"a preserved file changed at the move boundary: {root.path}",
                )
            named = _stat_at(parent_fd, root.path.name)
            if not _identity_matches(named, opened.st_dev, opened.st_ino):
                raise ProtocolRefusal(
                    "purge_identity_changed",
                    f"preserved root path changed at the move boundary: {root.path}",
                )
            return _MoveBinding(
                root=root,
                root_fd=root_fd,
                parent_fd=parent_fd,
                parent_device=parent_identity.st_dev,
                parent_inode=parent_identity.st_ino,
            )
        except BaseException:
            os.close(root_fd)
            os.close(parent_fd)
            raise

    @staticmethod
    def _trash_path_matches(plan: PurgePlan) -> bool:
        try:
            identity = os.lstat(plan.trash_dir)
        except OSError:
            return False
        return _identity_matches(identity, plan.trash_device, plan.trash_inode)

    @staticmethod
    def _source_path_matches(binding: _MoveBinding) -> bool:
        named = _stat_at(binding.parent_fd, binding.root.path.name)
        if not _identity_matches(named, binding.root.device, binding.root.inode):
            return False
        if not PurgeWriter._parent_path_matches(binding):
            return False
        try:
            absolute = os.lstat(binding.root.path)
        except OSError:
            return False
        return _identity_matches(absolute, binding.root.device, binding.root.inode)

    @staticmethod
    def _parent_path_matches(binding: _MoveBinding) -> bool:
        try:
            parent = os.lstat(binding.root.path.parent)
        except OSError:
            return False
        return (
            stat.S_ISDIR(parent.st_mode)
            and (parent.st_dev, parent.st_ino)
            == (binding.parent_device, binding.parent_inode)
        )

    @classmethod
    def _rollback(
        cls,
        moved: Sequence[_MoveBinding],
        trash_fd: int,
    ) -> Tuple[List[Dict[str, str]], List[Dict[str, str]]]:
        restored: List[Dict[str, str]] = []
        stranded: List[Dict[str, str]] = []
        for binding in reversed(moved):
            root = binding.root
            inspection_failed = False
            try:
                target_identity = _stat_at(trash_fd, root.trash.name)
                source_identity = _stat_at(binding.parent_fd, root.path.name)
            except OSError:
                inspection_failed = True
                target_identity = None
                source_identity = None
            if (
                not inspection_failed
                and target_identity is not None
                and source_identity is None
                and _identity_matches(target_identity, root.device, root.inode)
            ):
                try:
                    _rename_exclusive_at(
                        trash_fd,
                        root.trash.name,
                        binding.parent_fd,
                        root.path.name,
                    )
                except OSError:
                    pass
            try:
                target_identity = _stat_at(trash_fd, root.trash.name)
                restored_at_source = cls._source_path_matches(binding)
            except OSError:
                inspection_failed = True
                target_identity = None
                restored_at_source = False
            if (
                not inspection_failed
                and restored_at_source
                and target_identity is None
            ):
                restored.append(
                    {
                        "kind": "restored-root-receipt",
                        "original": str(root.path),
                        "trash": str(root.trash),
                        "status": "restored",
                    }
                )
            else:
                stranded.append(
                    {
                        "kind": "stranded-root-receipt",
                        "original": str(root.path),
                        "trash": str(root.trash),
                        "status": "stranded",
                    }
                )
        restored.reverse()
        stranded.reverse()
        return restored, stranded

    def execute(self, plan: PurgePlan) -> Dict[str, Any]:
        trash_fd: Optional[int] = None
        info_fd: Optional[int] = None
        bindings: List[_MoveBinding] = []
        moved: List[_MoveBinding] = []
        try:
            self._verify(plan)
            try:
                trash_fd = os.open(plan.trash_dir, _directory_flags())
            except OSError as exc:
                raise ProtocolRefusal(
                    "purge_trash_changed",
                    "the fixed Trash directory could not be held for the transaction",
                ) from exc
            held_trash = os.fstat(trash_fd)
            if (held_trash.st_dev, held_trash.st_ino) != (
                plan.trash_device,
                plan.trash_inode,
            ):
                raise ProtocolRefusal(
                    "purge_trash_changed",
                    "the fixed Trash directory changed before the transaction",
                )

            info_dir = _trash_info_dir(plan.trash_dir)
            if info_dir is not None:
                try:
                    info_fd = os.open(info_dir, _directory_flags())
                except OSError as exc:
                    raise ProtocolRefusal(
                        "purge_trash_unavailable",
                        f"Trash info directory could not be held: {info_dir}",
                        f"create the freedesktop Trash info directory: {info_dir}",
                    ) from exc

            expected_by_root: Dict[Path, List[_PlannedFile]] = {
                root.path: [] for root in plan.roots
            }
            for file in plan.files:
                expected_by_root[file.root].append(file)
            for root in plan.roots:
                binding = self._open_verified_root_at_move(
                    root,
                    expected_by_root[root.path],
                )
                bindings.append(binding)
                if not self._source_path_matches(binding):
                    raise ProtocolRefusal(
                        "purge_identity_changed",
                        f"preserved root path changed at the rename boundary: {root.path}",
                    )
                if not self._trash_path_matches(plan):
                    raise ProtocolRefusal(
                        "purge_trash_changed",
                        "the fixed Trash directory changed at the rename boundary",
                    )
                _rename_exclusive_at(
                    binding.parent_fd,
                    root.path.name,
                    trash_fd,
                    root.trash.name,
                )
                moved.append(binding)
                target_identity = _stat_at(trash_fd, root.trash.name)
                if not _identity_matches(target_identity, root.device, root.inode):
                    raise ProtocolRefusal(
                        "purge_identity_changed",
                        f"moved root identity did not match the preview: {root.path}",
                    )
                if not self._parent_path_matches(binding):
                    raise ProtocolRefusal(
                        "purge_identity_changed",
                        f"preserved root parent changed during the move: {root.path.parent}",
                    )
                post_move = _scan_open_root(binding.root_fd, root.path)
                if not self._same_files(expected_by_root[root.path], post_move.files):
                    raise ProtocolRefusal(
                        "purge_identity_changed",
                        f"a preserved file changed across the move boundary: {root.path}",
                    )
                if not self._trash_path_matches(plan):
                    raise ProtocolRefusal(
                        "purge_trash_changed",
                        "the fixed Trash directory changed during the transaction",
                    )
                if info_fd is not None:
                    _write_trashinfo(
                        info_fd,
                        f"{root.trash.name}{_TRASHINFO_SUFFIX}",
                        root.path,
                        datetime.now(),
                    )
        except (OSError, ProtocolRefusal) as exc:
            if moved and trash_fd is not None:
                restored, stranded = self._rollback(moved, trash_fd)
                evidence: Dict[str, Any] = {
                    "operation": "purge",
                    "status": "degraded",
                    "reason_code": (
                        "purge_rollback_failed" if stranded else "purge_move_failed"
                    ),
                    "restored_root_receipts": restored,
                    "stranded_root_receipts": stranded,
                }
                code = evidence["reason_code"]
                detail = (
                    "Trash move failed and one or more roots could not be restored"
                    if stranded
                    else "Trash move failed after mutation; every moved root was restored"
                )
                raise _PurgeRecoveryFailure(
                    code,
                    detail,
                    evidence,
                ) from exc
            if isinstance(exc, ProtocolRefusal):
                raise
            if exc.errno == errno.EXDEV:
                detail = "Trash is on another filesystem; no copy-and-remove fallback is allowed"
            elif exc.errno == errno.EEXIST:
                detail = "an exclusive Trash destination became occupied; prior moves were restored"
            else:
                detail = "Trash move failed; every completed move was restored"
            raise ProtocolRefusal(
                "purge_move_failed",
                detail,
            ) from exc
        finally:
            for binding in bindings:
                binding.close()
            if trash_fd is not None:
                os.close(trash_fd)
            if info_fd is not None:
                os.close(info_fd)
        return plan.evidence(dry_run=False, status="trashed")

    def run(self) -> Dict[str, Any]:
        plan = self.plan()
        if self.dry_run:
            return plan.evidence(dry_run=True, status="planned")
        return self.execute(plan)


def plan(
    roots: Sequence[os.PathLike[str] | str] | os.PathLike[str] | str,
    *,
    timestamp: Optional[str] = None,
) -> PurgePlan:
    """Build a read-only plan for callers that need to inspect it first."""

    return PurgeWriter(roots, timestamp=timestamp).plan()


def execute(purge_plan: PurgePlan) -> Dict[str, Any]:
    """Execute one previously generated plan through the same Trash boundary."""

    writer = PurgeWriter(
        [root.path for root in purge_plan.roots],
        timestamp=purge_plan.timestamp,
    )
    return writer.execute(purge_plan)


def _handle(args: argparse.Namespace) -> Tuple[str, Dict[str, Any], int]:
    try:
        evidence = PurgeWriter(args.roots, dry_run=args.dry_run).run()
    except _PurgeRecoveryFailure as exc:
        evidence = dict(exc.evidence)
        evidence["code"] = exc.code
        evidence["detail"] = exc.detail
        return "degraded", evidence, 35
    return "ok", evidence, 0


def register_cli(commands: argparse._SubParsersAction) -> None:
    """Register the dark purge command for integration activation."""

    purge = commands.add_parser(
        "purge",
        help="move the exact roots you list into the account Trash; never deletes",
    )
    purge.add_argument(
        "--root",
        "--preserved-root",
        action="append",
        dest="roots",
        required=True,
        help="one absolute preserved root to move; repeat for more",
    )
    purge.add_argument(
        "--dry-run",
        action="store_true",
        help="list every file that would move; move nothing",
    )
    purge.set_defaults(handler=_handle)

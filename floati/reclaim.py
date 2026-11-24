"""Delete only declared build output in idle fleet-created trees. Never a guess."""

from __future__ import annotations

import ctypes
import errno
import json
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from .doctor_disk_watch import _fleet_trees
from .registry import utc_now
from .root import FloatiRoot

IDLE_SECONDS = 24 * 3600
DECLARATIONS_RELATIVE = Path("state") / "reclaim-declarations.v0.json"


def _load_declared(root: FloatiRoot) -> Set[str]:
    path = root.path / DECLARATIONS_RELATIVE
    if path.is_symlink() or not path.is_file():
        return set()
    try:
        raw = json.loads(path.read_bytes())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return set()
    if not isinstance(raw, dict):
        return set()
    names = raw.get("paths")
    if not isinstance(names, list):
        return set()
    return {name for name in names if isinstance(name, str) and name and "/" not in name}


def _directory_bytes(path: Path) -> int:
    total = 0
    for dirpath, _dirnames, filenames in os.walk(path, followlinks=False):
        for name in filenames:
            try:
                total += int((Path(dirpath) / name).stat().st_size)
            except OSError:
                continue
    return total


def _default_newest_mtime(path: Path) -> float:
    newest = path.stat().st_mtime
    for dirpath, _dirnames, filenames in os.walk(path, followlinks=False):
        current = Path(dirpath)
        try:
            newest = max(newest, current.stat().st_mtime)
        except OSError as exc:
            raise OSError("mtime walk failed") from exc
        for name in filenames:
            try:
                newest = max(newest, (current / name).stat().st_mtime)
            except OSError as exc:
                raise OSError("mtime walk failed") from exc
    return newest


def _inside(path: Path, tree: Path) -> bool:
    try:
        path.relative_to(tree)
    except ValueError:
        return False
    return True


def _measure_free(path: Path, statvfs: Callable[[str], object]) -> Optional[int]:
    try:
        vfs = statvfs(str(path))
        free = int(getattr(vfs, "f_bavail")) * int(getattr(vfs, "f_frsize"))
    except (OSError, AttributeError, TypeError, ValueError):
        return None
    if free < 0:
        return None
    return free


def _row(path: Path, *, reason: str, bytes_count: int, rule: str) -> Dict[str, object]:
    return {
        "path": str(path),
        "reason": reason,
        "bytes": bytes_count,
        "rule": rule,
        "utc": utc_now(),
    }


def _judge(
    tree: Path,
    candidate: Path,
    *,
    declared: Set[str],
    now: float,
    process_cwds: Sequence[Path],
    claimed_trees: Sequence[Path],
    newest_mtime: Callable[[Path], float],
) -> Tuple[str, str]:
    if candidate.name not in declared:
        return "undeclared", "keep_undeclared"
    resolved_tree = tree.resolve()
    try:
        if any(
            resolved_tree == claimed.resolve() or _inside(resolved_tree, claimed.resolve())
            for claimed in claimed_trees
        ):
            return "claimed", "keep_claimed"
        if any(_inside(cwd.resolve(), resolved_tree) for cwd in process_cwds):
            return "process_cwd", "keep_process_cwd"
    except OSError:
        return "probe_error", "keep_probe_error"
    try:
        newest = float(newest_mtime(candidate))
    except OSError:
        return "probe_error", "keep_probe_error"
    if now - newest < IDLE_SECONDS:
        return "recent_write", "keep_recent_write"
    return "idle", "delete_idle_declared"


def host_process_cwds(*, on_unreadable: Optional[Callable[[str], None]] = None) -> Tuple[Path, ...]:
    """Resolve visible process cwds and optionally report gaps in coverage."""

    if sys.platform.startswith("linux"):
        return _linux_process_cwds(on_unreadable)
    if sys.platform == "darwin":
        return _darwin_process_cwds(on_unreadable)
    raise OSError("process cwd probe unsupported on " + sys.platform)


def _linux_process_cwds(on_unreadable=None) -> Tuple[Path, ...]:
    try:
        names = os.listdir("/proc")
    except OSError as exc:
        raise OSError("process cwd probe failed") from exc
    cwds: List[Path] = []
    for name in names:
        if not name.isdigit():
            continue
        process = Path("/proc", name)
        try:
            cwd = Path(os.readlink(process / "cwd"))
            if not cwd.is_absolute():
                raise OSError("process cwd is not absolute")
            cwds.append(cwd.resolve())
        except FileNotFoundError:
            if on_unreadable is None:
                continue
            try:
                # A vanished PID, kernel thread or zombie has no useful cwd.
                fields = (process / "stat").read_bytes().rpartition(b") ")[2].split()
                if fields[0] not in (b"Z", b"X") and not (int(fields[6]) & 0x00200000):
                    on_unreadable("A live process cwd is unavailable")
            except FileNotFoundError:
                continue
            except (OSError, IndexError, ValueError):
                on_unreadable("A process with no readable cwd could not be classified")
        except (OSError, RuntimeError):
            if on_unreadable is not None:
                on_unreadable("A process cwd could not be read")
    if not cwds:
        raise OSError("process cwd probe returned no cwds")
    return tuple(cwds)


def _darwin_process_cwds(on_unreadable=None) -> Tuple[Path, ...]:
    # Measured on Darwin: PROC_PIDVNODEPATHINFO needs 2352 bytes; pvi_cdir.vip_path
    # starts at offset 152 (vnode_info then MAXPATHLEN).
    proc_pidvnodepathinfo = 9
    proc_all_pids = 1
    vnodepathinfo_size = 2352
    cdir_path_offset = 152
    maxpathlen = 1024
    lib = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
    lib.proc_listpids.argtypes = [
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_int,
    ]
    lib.proc_listpids.restype = ctypes.c_int
    lib.proc_pidinfo.argtypes = [
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_uint64,
        ctypes.c_void_p,
        ctypes.c_int,
    ]
    lib.proc_pidinfo.restype = ctypes.c_int
    needed = lib.proc_listpids(proc_all_pids, 0, None, 0)
    if needed <= 0:
        raise OSError("process cwd probe failed")
    buf = (ctypes.c_int * (needed // 4 + 64))()
    got = lib.proc_listpids(proc_all_pids, 0, buf, ctypes.sizeof(buf))
    if got <= 0:
        raise OSError("process cwd probe failed")
    if on_unreadable is not None and got >= ctypes.sizeof(buf):
        on_unreadable("The process inventory filled its buffer")
    cwds: List[Path] = []
    info = ctypes.create_string_buffer(vnodepathinfo_size)
    for pid in buf[: got // 4]:
        if pid <= 0:
            continue
        ctypes.set_errno(0)
        received = lib.proc_pidinfo(
            int(pid), proc_pidvnodepathinfo, 0, info, vnodepathinfo_size
        )
        if received != vnodepathinfo_size:
            if on_unreadable is not None and ctypes.get_errno() != errno.ESRCH:
                on_unreadable("A process cwd could not be read")
            continue
        raw = info.raw[cdir_path_offset : cdir_path_offset + maxpathlen]
        text = raw.split(b"\0", 1)[0].decode("utf-8", "surrogateescape")
        if not text.startswith("/"):
            if text and on_unreadable is not None:
                on_unreadable("A process cwd is not absolute")
            continue
        try:
            cwds.append(Path(text).resolve())
        except (OSError, RuntimeError):
            if on_unreadable is not None:
                on_unreadable("A process cwd could not be resolved")
    if not cwds:
        raise OSError("process cwd probe returned no cwds")
    return tuple(cwds)


def reclaim(
    root: FloatiRoot,
    *,
    apply: bool = False,
    now: Optional[float] = None,
    process_cwds: Iterable[Path] = (),
    claimed_trees: Iterable[Path] = (),
    newest_mtime: Optional[Callable[[Path], float]] = None,
    statvfs: Optional[Callable[[str], object]] = None,
    derivation_failed: bool = False,
) -> Dict[str, object]:
    """Dry-run by default. --apply deletes only idle declared build output."""

    if derivation_failed:
        apply = False
    if now is None:
        now = time.time()
    if newest_mtime is None:
        newest_mtime = _default_newest_mtime
    if statvfs is None:
        statvfs = os.statvfs
    declared = _load_declared(root)
    claimed = tuple(Path(item) for item in claimed_trees)
    cwds = tuple(Path(item) for item in process_cwds)
    listed: List[Dict[str, object]] = []
    kept: List[Dict[str, object]] = []
    deleted: List[Dict[str, object]] = []
    before = _measure_free(root.path, statvfs)
    for tree, files_only in _fleet_trees(root):
        if files_only:
            continue
        try:
            children = list(tree.iterdir())
        except OSError:
            continue
        for child in children:
            if child.is_symlink() or not child.is_dir():
                continue
            if derivation_failed:
                reason, rule = "probe_error", "keep_probe_error"
            else:
                reason, rule = _judge(
                    tree,
                    child,
                    declared=declared,
                    now=now,
                    process_cwds=cwds,
                    claimed_trees=claimed,
                    newest_mtime=newest_mtime,
                )
            size = _directory_bytes(child)
            if reason != "idle":
                kept.append(_row(child, reason=reason, bytes_count=size, rule=rule))
                continue
            listed.append(_row(child, reason=reason, bytes_count=size, rule=rule))
            if not apply:
                continue
            retry_reason, retry_rule = _judge(
                tree,
                child,
                declared=declared,
                now=now,
                process_cwds=cwds,
                claimed_trees=claimed,
                newest_mtime=newest_mtime,
            )
            if retry_reason != "idle":
                kept.append(_row(child, reason=retry_reason, bytes_count=size, rule=retry_rule))
                continue
            shutil.rmtree(child)
            deleted.append(_row(child, reason=reason, bytes_count=size, rule=rule))
    after = _measure_free(root.path, statvfs)
    df_moved = 0
    if apply and before is not None and after is not None:
        df_moved = after - before
    return {
        "listed": listed,
        "kept": kept,
        "deleted": deleted,
        "dry_run": not apply,
        "df_moved_bytes": df_moved,
    }

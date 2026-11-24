"""Doctor finding: host free space against the fleet floor. Never deletes."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Callable, Dict, List, Mapping, Optional, Set, Tuple

from .copy import DOCTOR_DISK_WATCH_REMEDIATION, DOCTOR_DISK_WATCH_UNMEASURED
from .root import FloatiRoot

FLEET_DISK_FLOOR_BYTES = 1 << 30
TREE_WALK_BUDGET_SECONDS = 0.5

BUILD_DIRECTORY_NAMES = frozenset(
    {
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".tox",
        "htmlcov",
        "build",
        "dist",
        ".eggs",
    }
)
BUILD_DIRECTORY_SUFFIXES = (".egg-info",)
BUILD_FILE_NAMES = frozenset({".coverage", "coverage.xml"})
BUILD_FILE_SUFFIXES = frozenset({".pyc", ".pyo"})


def _is_build_directory(name: str) -> bool:
    return name in BUILD_DIRECTORY_NAMES or name.endswith(BUILD_DIRECTORY_SUFFIXES)


def _is_build_file(name: str) -> bool:
    if name in BUILD_FILE_NAMES:
        return True
    return os.path.splitext(name)[1] in BUILD_FILE_SUFFIXES


def _classified_file_name(name: str) -> str:
    if name in BUILD_FILE_NAMES:
        return name
    suffix = os.path.splitext(name)[1]
    return suffix if suffix in BUILD_FILE_SUFFIXES else name


def _usable_tree(path: Path) -> Optional[Path]:
    if not path.is_absolute():
        return None
    try:
        if path.is_symlink() or not path.is_dir():
            return None
        return path.resolve()
    except OSError:
        return None


def _ownership_homes(root: FloatiRoot) -> List[Path]:
    directory = root.path / "state" / "seat-onboarding"
    if directory.is_symlink() or not directory.is_dir():
        return []
    homes: List[Path] = []
    try:
        entries = list(directory.iterdir())
    except OSError:
        return []
    for entry in entries:
        if entry.is_symlink() or not entry.is_file() or entry.suffix != ".json":
            continue
        try:
            raw = json.loads(entry.read_bytes())
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
            continue
        if not isinstance(raw, dict):
            continue
        for key in ("home",):
            value = raw.get(key)
            if isinstance(value, str):
                usable = _usable_tree(Path(value))
                if usable is not None:
                    homes.append(usable)
        profile = raw.get("profile")
        if isinstance(profile, dict):
            workspace = profile.get("workspace_root")
            if isinstance(workspace, str):
                usable = _usable_tree(Path(workspace))
                if usable is not None:
                    homes.append(usable)
    return homes


def _mapped_workspaces(root: FloatiRoot) -> List[Path]:
    from .codex_wait_contract import mapped_workspaces

    found: List[Path] = []
    for workspace, _node in mapped_workspaces(root.path):
        usable = _usable_tree(workspace)
        if usable is not None:
            found.append(usable)
    return found


def _lane_workspaces(root: FloatiRoot) -> List[Path]:
    try:
        from .lane_workspaces import LaneWorkspaces

        service = LaneWorkspaces(root)
        lanes, _repositories = service._declarations()
        found: List[Path] = []
        for node in service._nodes():
            for record in service._open_records(node):
                relative = record.get("path")
                if not isinstance(relative, str) or not relative:
                    continue
                usable = _usable_tree(lanes / relative)
                if usable is not None:
                    found.append(usable)
        return found
    except Exception:
        return []


def _fleet_trees(root: FloatiRoot) -> List[Tuple[Path, bool]]:
    """Budgeted trees the fleet created: ownership homes and registered workspaces."""

    trees: List[Tuple[Path, bool]] = [(root.path, True)]
    seen: Set[Path] = {root.path}
    for path in (*_ownership_homes(root), *_mapped_workspaces(root), *_lane_workspaces(root)):
        if path in seen:
            continue
        seen.add(path)
        trees.append((path, False))
    return trees


def _walk(
    path: Path,
    *,
    files_only: bool,
    deadline: float,
    monotonic: Callable[[], float],
) -> Tuple[int, Set[str], bool, Optional[str]]:
    build_bytes = 0
    names: Set[str] = set()
    stack: List[Tuple[Path, bool]] = [(path, False)]
    while stack:
        if monotonic() >= deadline:
            return build_bytes, names, True, "walk_capped"
        current, inside_build = stack.pop()
        try:
            with os.scandir(current) as iterator:
                children = list(iterator)
        except OSError:
            if current == path and not inside_build:
                return 0, set(), True, "tree_unreadable"
            continue
        for entry in children:
            if monotonic() >= deadline:
                return build_bytes, names, True, "walk_capped"
            try:
                if entry.is_symlink():
                    continue
                name = entry.name
                if entry.is_dir(follow_symlinks=False):
                    if files_only and current == path:
                        continue
                    if _is_build_directory(name):
                        names.add(name)
                    stack.append(
                        (Path(entry.path), inside_build or _is_build_directory(name))
                    )
                elif entry.is_file(follow_symlinks=False):
                    classified = _is_build_file(name)
                    if classified:
                        names.add(_classified_file_name(name))
                    if inside_build or classified:
                        build_bytes += int(entry.stat(follow_symlinks=False).st_size)
            except (OSError, ValueError):
                continue
    return build_bytes, names, False, None


def _measure_free(
    path: Path, statvfs: Callable[[str], object]
) -> Tuple[Optional[int], Optional[str]]:
    try:
        vfs = statvfs(str(path))
        free_bytes = int(getattr(vfs, "f_bavail")) * int(getattr(vfs, "f_frsize"))
        if free_bytes < 0:
            raise ValueError("negative free space")
    except (OSError, AttributeError, TypeError, ValueError):
        return None, "statvfs_unavailable"
    return free_bytes, None


def _exit_code(
    *,
    free_bytes: Optional[int],
    floor_bytes: int,
    build_bytes: int,
    partial: bool,
) -> int:
    del build_bytes
    if free_bytes is None or partial:
        return 2
    if free_bytes >= floor_bytes:
        return 0
    return 1


def observe_disk_watch(
    root: FloatiRoot,
    *,
    statvfs: Optional[Callable[[str], object]] = None,
    monotonic: Optional[Callable[[], float]] = None,
    tree_budget_seconds: float = TREE_WALK_BUDGET_SECONDS,
    floor_bytes: int = FLEET_DISK_FLOOR_BYTES,
) -> Dict[str, object]:
    """Report host free space and derived build output. This function never deletes."""

    if statvfs is None:
        statvfs = os.statvfs
    if monotonic is None:
        monotonic = time.monotonic
    free_bytes, stat_reason = _measure_free(root.path, statvfs)
    trees: List[Dict[str, object]] = []
    build_bytes = 0
    build_names: Set[str] = set()
    partial = False
    reasons: Set[str] = set()
    if stat_reason is not None:
        partial = True
        reasons.add(stat_reason)
    for tree_path, files_only in _fleet_trees(root):
        started = monotonic()
        deadline = started + tree_budget_seconds
        tree_bytes, names, tree_partial, tree_reason = _walk(
            tree_path,
            files_only=files_only,
            deadline=deadline,
            monotonic=monotonic,
        )
        trees.append(
            {
                "path": str(tree_path),
                "build_bytes": tree_bytes,
                "partial": tree_partial,
                "reason": tree_reason,
            }
        )
        build_bytes += tree_bytes
        build_names.update(names)
        if tree_partial:
            partial = True
            if tree_reason is not None:
                reasons.add(tree_reason)
    if stat_reason is not None:
        reason: Optional[str] = stat_reason
    elif "walk_capped" in reasons:
        reason = "walk_capped"
    elif "tree_unreadable" in reasons:
        reason = "tree_unreadable"
    else:
        reason = None
    return {
        "exit": _exit_code(
            free_bytes=free_bytes,
            floor_bytes=floor_bytes,
            build_bytes=build_bytes,
            partial=partial,
        ),
        "floor_bytes": floor_bytes,
        "free_bytes": free_bytes,
        "build_bytes": build_bytes,
        "trees": trees,
        "partial": partial,
        "reason": reason,
        "build_names": sorted(build_names),
    }


def _disk_watch_finding(observation: Dict[str, object], subject: str) -> Dict[str, object]:
    from .doctor import _finding

    exit_code = int(observation["exit"])
    severity = {0: "ok", 1: "warning", 2: "error"}[exit_code]
    free = observation["free_bytes"]
    if exit_code == 2:
        reason = observation.get("reason")
        detail = "host disk cannot be measured"
        if reason:
            detail += f" ({reason})"
        detail += f"; build output {observation['build_bytes']} bytes"
        remediation = DOCTOR_DISK_WATCH_UNMEASURED
    else:
        free_text = "unavailable" if free is None else str(free)
        detail = (
            f"host free {free_text} bytes against fleet floor "
            f"{observation['floor_bytes']} bytes; build output "
            f"{observation['build_bytes']} bytes"
        )
        if observation["partial"]:
            detail += f"; partial ({observation['reason']})"
        remediation = None if exit_code == 0 else DOCTOR_DISK_WATCH_REMEDIATION
    finding = _finding(
        "host_disk_watch",
        severity,
        subject,
        detail,
        remediation,
    )
    finding["disk_watch"] = observation
    return finding


def _fold_disk_watch_exit(current: int, disk: int) -> int:
    if disk == 0:
        return current
    if current in {20, 22, 33, 36}:
        return current
    if current == 0:
        return disk
    return current


def project_disk_watch(root, findings: List[Dict[str, object]], rc: int, **kwargs) -> int:
    """Register the host disk finding once and fold its exit into doctor rc."""

    observation = observe_disk_watch(root, **kwargs)
    findings.append(_disk_watch_finding(observation, str(root.path)))
    return _fold_disk_watch_exit(rc, int(observation["exit"]))


def _format_free_gib_or_mib(free_bytes: int) -> str:
    """Render binary units to one decimal without overstating measured space."""

    unit_bytes, unit = (1024 ** 3, "GiB") if free_bytes >= 1024 ** 3 else (1024 ** 2, "MiB")
    tenths = free_bytes * 10 // unit_bytes
    return f"{tenths // 10}.{tenths % 10} {unit}"


def disk_watch_line(observation: Mapping[str, object]) -> Optional[str]:
    """Summarize the measured detail shared by board and wake reports."""

    from .tui_render import (
        DISK_WATCH_ALERT,
        DISK_WATCH_CAPPED_TREES,
        DISK_WATCH_ONE_CAPPED_TREE,
        DISK_WATCH_PARTIAL_ALERT,
        DISK_WATCH_UNMEASURED_ALERT,
    )

    exit_code = int(observation["exit"])
    if exit_code == 1:
        return DISK_WATCH_ALERT
    if exit_code == 2:
        free_bytes = observation.get("free_bytes")
        if free_bytes is None:
            return DISK_WATCH_UNMEASURED_ALERT
        line = DISK_WATCH_PARTIAL_ALERT.format(free=_format_free_gib_or_mib(free_bytes))
        capped_trees = sum(
            tree.get("reason") == "walk_capped"
            for tree in observation.get("trees", [])
        )
        if capped_trees == 1:
            line += DISK_WATCH_ONE_CAPPED_TREE
        elif capped_trees > 1:
            line += DISK_WATCH_CAPPED_TREES.format(count=capped_trees)
        return line
    return None


def attach_disk_watch_report(root, report: Dict[str, object], **kwargs) -> Dict[str, object]:
    """Attach the typed disk-watch line to a seat-board or wake report."""

    observation = observe_disk_watch(root, **kwargs)
    line = disk_watch_line(observation)
    if line is None:
        return report
    attached = dict(report)
    attached["disk_watch"] = observation
    attached["disk_watch_line"] = line
    return attached

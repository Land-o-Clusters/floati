"""Bounded local lane inventory; foreign coordinates are observed, never adopted."""
from __future__ import annotations

import os
from pathlib import Path
import stat
import time

MAX_ENTRIES = 100000
MAX_DEPTH = 64


def measure_directory(path: Path, *, budget: list[int] | None = None) -> dict:
    """Count ordinary bytes without following symlinks; absence remains unavailable."""
    total = 0
    remaining = [MAX_ENTRIES] if budget is None else budget
    try:
        remaining[0] -= 1
        if remaining[0] < 0:
            raise ValueError('inventory_entry_bound_exceeded')
        initial = path.lstat()
        if stat.S_ISREG(initial.st_mode):
            return {'path': str(path), 'age_seconds': max(0.0, time.time() - initial.st_mtime),
                    'bytes': initial.st_size, 'reason': None}
        if not stat.S_ISDIR(initial.st_mode):
            return {'path': str(path), 'age_seconds': None, 'bytes': None,
                    'reason': 'directory_identity_unavailable'}
        pending = [(path, 0)]
        while pending:
            current, depth = pending.pop()
            if depth > MAX_DEPTH:
                raise ValueError('inventory_depth_exceeded')
            with os.scandir(current) as entries:
                for entry in entries:
                    remaining[0] -= 1
                    if remaining[0] < 0:
                        raise ValueError('inventory_entry_bound_exceeded')
                    metadata = entry.stat(follow_symlinks=False)
                    if stat.S_ISDIR(metadata.st_mode):
                        pending.append((Path(entry.path), depth + 1))
                    elif stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
                        total += metadata.st_size
        return {'path': str(path), 'age_seconds': max(0.0, time.time() - initial.st_mtime),
                'bytes': total, 'reason': None}
    except (OSError, ValueError):
        return {'path': str(path), 'age_seconds': None, 'bytes': None,
                'reason': 'inventory_unavailable'}


def inventory(lanes_root: Path, recorded: list[Path], git_worktrees: list[Path],
              structural_ancestors: set[Path] | None = None) -> list[dict]:
    """Return nonoverlapping topmost unmanaged directories at declared coordinates."""
    managed = set(recorded)
    structural = set() if structural_ancestors is None else structural_ancestors
    candidates = set()
    unavailable = []
    pending = [(lanes_root, 0)]
    scanned = 0
    while pending:
        parent, depth = pending.pop()
        if parent in managed:
            continue
        try:
            if depth > MAX_DEPTH:
                raise ValueError('inventory_depth_exceeded')
            if parent.is_symlink():
                unavailable.append({'path': str(parent), 'bytes': None, 'age_seconds': None,
                                    'reason': 'inventory_symlink'})
                continue
            with os.scandir(parent) as entries:
                for entry in entries:
                    scanned += 1
                    if scanned > MAX_ENTRIES:
                        raise ValueError('inventory_entry_bound_exceeded')
                    path = Path(entry.path)
                    if path in managed:
                        continue
                    metadata = entry.stat(follow_symlinks=False)
                    if stat.S_ISLNK(metadata.st_mode):
                        unavailable.append({'path': str(path), 'bytes': None, 'age_seconds': None,
                                            'reason': 'inventory_symlink'})
                    elif stat.S_ISREG(metadata.st_mode):
                        candidates.add(path)
                    elif stat.S_ISDIR(metadata.st_mode):
                        if path in structural or any(path in lane.parents for lane in managed):
                            pending.append((path, depth + 1))
                        else:
                            candidates.add(path)
        except FileNotFoundError:
            unavailable.append({'path': str(parent), 'bytes': None, 'age_seconds': None,
                                'reason': 'inventory_absent' if parent == lanes_root else 'inventory_disappeared'})
        except (OSError, ValueError):
            unavailable.append({'path': str(parent), 'bytes': None, 'age_seconds': None,
                                'reason': 'inventory_unavailable'})
    for path in git_worktrees:
        if path not in managed and not any(path in lane.parents for lane in managed):
            candidates.add(path)
    topmost = [path for path in sorted(candidates) if not any(parent in candidates for parent in path.parents)]
    budget = [max(0, MAX_ENTRIES - scanned)]
    rows = [measure_directory(path, budget=budget) for path in topmost]
    rows.extend(row for row in unavailable if not any(Path(row['path']) == p or p in Path(row['path']).parents for p in topmost))
    return sorted(rows, key=lambda row: row['path'])

"""Generated-artifact source-name scrub used by the full selftest."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Iterable, List


_FORBIDDEN = bytes.fromhex("5369676e616c4372616674")
_EXCLUDED_NAMES = frozenset((".git", "__pycache__", "HM0_BRIEF.md"))
_MAX_HISTORY_BYTES = 16 * 1024 * 1024


def scan_generated_tree(root: Path) -> List[str]:
    """Return repository-relative generated files containing the private name."""
    base = Path(root).resolve()
    hits: List[str] = []
    for path in _files(base):
        try:
            data = path.read_bytes()
        except OSError:
            continue
        if _FORBIDDEN.lower() in data.lower():
            hits.append(path.relative_to(base).as_posix())
    return sorted(hits)


def scan_git_history_notes(root: Path) -> List[str]:
    """Return commit-message and Git-note coordinates containing the private name."""

    base = Path(root).resolve()
    messages = _git(
        base,
        "log",
        "--all",
        "--max-count=10000",
        "--format=format:%H%x00%B%x00",
    )
    fields = messages.split(b"\0")
    hits: List[str] = []
    for index in range(0, len(fields) - 1, 2):
        sha = fields[index].decode("ascii", errors="replace").strip()
        body = fields[index + 1]
        if sha and _FORBIDDEN.lower() in body.lower():
            hits.append(f"{sha}:commit-message")

    refs = _git(base, "for-each-ref", "--format=%(refname)", "refs/notes")
    for raw_ref in refs.splitlines():
        ref = raw_ref.decode("utf-8", errors="replace").strip()
        if not ref:
            continue
        listing = _git(base, "notes", f"--ref={ref}", "list")
        for line in listing.splitlines():
            parts = line.decode("ascii", errors="replace").split()
            if len(parts) != 2:
                continue
            object_sha = parts[1]
            note = _git(base, "notes", f"--ref={ref}", "show", object_sha)
            if _FORBIDDEN.lower() in note.lower():
                hits.append(f"{ref}:{object_sha}:note")
    return sorted(hits)


def _git(root: Path, *arguments: str) -> bytes:
    try:
        result = subprocess.run(
            ["git", "--no-replace-objects", *arguments],
            cwd=root,
            env=_git_environment(),
            check=False,
            capture_output=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError("history-note scrub unavailable") from exc
    if result.returncode != 0:
        raise RuntimeError("history-note scrub unavailable")
    if len(result.stdout) > _MAX_HISTORY_BYTES:
        raise RuntimeError("history-note scrub exceeds bounded output")
    return result.stdout


def _files(root: Path) -> Iterable[Path]:
    tracked = _tracked_files(root)
    if tracked is not None:
        yield from tracked
        return
    for path in root.rglob("*"):
        if any(part in _EXCLUDED_NAMES for part in path.relative_to(root).parts):
            continue
        if path.is_file() and not path.is_symlink():
            yield path


def _tracked_files(root: Path) -> List[Path] | None:
    """Return the Git publication inventory, or None for a non-repository fixture."""

    try:
        probe = subprocess.run(
            ["git", "--no-replace-objects", "rev-parse", "--is-inside-work-tree"],
            cwd=root,
            env=_git_environment(),
            check=False,
            capture_output=True,
            timeout=30,
        )
        if probe.returncode != 0:
            if (root / ".git").exists():
                raise RuntimeError("tracked source scrub unavailable")
            return None
        if probe.stdout.strip() != b"true":
            raise RuntimeError("tracked source scrub requires a work tree")
        result = subprocess.run(
            ["git", "--no-replace-objects", "ls-files", "-z", "--cached"],
            cwd=root,
            env=_git_environment(),
            check=False,
            capture_output=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError("tracked source scrub unavailable") from exc
    if result.returncode != 0:
        raise RuntimeError("tracked source scrub unavailable")
    if len(result.stdout) > _MAX_HISTORY_BYTES:
        raise RuntimeError("tracked source scrub exceeds bounded output")
    paths: List[Path] = []
    for raw in result.stdout.split(b"\0"):
        if not raw:
            continue
        relative = Path(os.fsdecode(raw))
        if relative.is_absolute() or ".." in relative.parts:
            raise RuntimeError("tracked source scrub path is invalid")
        if any(part in _EXCLUDED_NAMES for part in relative.parts):
            continue
        path = root / relative
        if path.is_file() and not path.is_symlink():
            paths.append(path)
    return paths


def _git_environment() -> dict[str, str]:
    """Use the addressed checkout, never caller-injected Git coordinates."""

    environment = {
        key: value for key, value in os.environ.items() if not key.startswith("GIT_")
    }
    environment.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_PAGER": "cat"})
    return environment

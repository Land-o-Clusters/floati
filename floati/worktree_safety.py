"""Worktree-removal safety — the live successor of the dark cleanup capability.

F10-1 remedy (b): `lane_scaling.py` and `verification.py` need ONE
capability from the dark `floati/locks` package — refuse removing a
worktree whose HEAD holds commits that no ref reaches (the L-4 law:
"workspace removal ONLY under L-4; refusal is the feature"). Measured at
the call sites, the capability is git-reachability arithmetic plus path
hygiene — not locks machinery (no ledger, queue, or handoff) — so it is
REIMPLEMENTED HERE. The dark boundary is restored, not legitimized; the
refusal code `cleanup_unreferenced_commits` is preserved because it is
the typed contract the call sites and their banks pin.

The git environment is sandboxed the way the dark GitObserver sandboxed
it: fixed PATH, empty HOME, no system/global config, no prompts, no
optional locks, C locale.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Iterable, Optional

from .errors import ProtocolRefusal

_MAX_REFS = 4096
_SANDBOX_ENV = {
    "PATH": "/usr/bin:/bin",
    "HOME": "/var/empty",
    "LANG": "C",
    "LC_ALL": "C",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_OPTIONAL_LOCKS": "0",
    "GIT_NO_REPLACE_OBJECTS": "1",
    "GIT_PAGER": "cat",
    "PAGER": "cat",
}


def _canonical_directory(value: Path, field: str) -> Path:
    if (
        not isinstance(value, Path)
        or not value.is_absolute()
        or value.is_symlink()
    ):
        raise ProtocolRefusal(
            f"{field}_invalid",
            "worktree safety requires one canonical absolute non-symlink directory",
        )
    resolved = value.resolve()
    if resolved != value or not resolved.is_dir():
        raise ProtocolRefusal(
            f"{field}_invalid",
            "worktree safety path is not a canonical directory",
        )
    return resolved


def _run(
    cwd: Path,
    *arguments: str,
    stdin: Optional[bytes] = None,
    allow_status: Iterable[int] = (0,),
) -> subprocess.CompletedProcess[bytes]:
    try:
        completed = subprocess.run(
            ["/usr/bin/git", *arguments],
            cwd=str(cwd),
            input=stdin,
            env=_SANDBOX_ENV,
            timeout=5.0,
            capture_output=True,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ProtocolRefusal(
            "git_observation_failed",
            "worktree safety could not observe the repository",
        ) from exc
    if completed.returncode not in tuple(allow_status):
        raise ProtocolRefusal(
            "git_observation_malformed",
            "worktree safety observed an unexpected git outcome",
        )
    return completed


def _text_output(completed: subprocess.CompletedProcess[bytes], what: str) -> str:
    try:
        return completed.stdout.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ProtocolRefusal(
            "git_observation_malformed", f"{what} is not valid UTF-8"
        ) from exc


def _sha_lines(text: str, what: str) -> set[str]:
    lines = text.splitlines()
    if any(len(line) != 40 or not all(c in "0123456789abcdef" for c in line)
           for line in lines):
        raise ProtocolRefusal(
            "git_observation_malformed", f"{what} returned a non-SHA line"
        )
    return set(lines)


def _common_git_dir(repository: Path) -> Path:
    output = _text_output(
        _run(
            repository,
            "rev-parse",
            "--path-format=absolute",
            "--git-common-dir",
        ),
        "git common directory",
    ).strip()
    if not output:
        raise ProtocolRefusal(
            "git_observation_malformed", "git common directory is empty"
        )
    return Path(output)


def require_worktree_commits_referenced(
    worktree: Path, *, repository: Optional[Path] = None
) -> None:
    """Refuse while `worktree` HEAD holds commits no ref in the common
    repository reaches — removing the worktree would orphan them."""
    worktree = _canonical_directory(worktree, "worktree")
    repository = (
        _canonical_directory(repository, "repository")
        if repository is not None
        else worktree
    )
    if _common_git_dir(repository) != _common_git_dir(worktree):
        raise ProtocolRefusal(
            "worktree_repository_mismatch",
            "worktree belongs to another Git common directory",
        )
    head = _text_output(
        _run(worktree, "rev-parse", "--verify", "HEAD^{commit}"),
        "worktree head",
    ).strip()
    if len(head) != 40 or not all(c in "0123456789abcdef" for c in head):
        raise ProtocolRefusal(
            "git_observation_malformed", "worktree head is not a SHA"
        )
    reachable = _sha_lines(
        _text_output(_run(worktree, "rev-list", head), "worktree reachability"),
        "worktree reachability",
    )
    ref_text = _text_output(
        _run(repository, "for-each-ref", "--format=%(refname)"),
        "repository refs",
    )
    refs = tuple(line for line in ref_text.splitlines() if line)
    if len(refs) > _MAX_REFS or any(
        not line.startswith("refs/") or line.startswith("-") for line in refs
    ):
        raise ProtocolRefusal(
            "git_observation_malformed", "repository ref inventory is out of bounds"
        )
    referenced: set[str] = set()
    if refs:
        payload = ("\n".join(refs) + "\n").encode("utf-8")
        referenced = _sha_lines(
            _text_output(
                _run(repository, "rev-list", "--stdin", stdin=payload),
                "referenced reachability",
            ),
            "referenced reachability",
        )
    unreferenced = sorted(reachable - referenced)
    if unreferenced:
        raise ProtocolRefusal(
            "cleanup_unreferenced_commits",
            "cleanup refused; commits reachable only from the worktree: "
            + ",".join(unreferenced),
        )

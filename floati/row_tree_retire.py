"""Offer git worktree remove for finished row trees. Never --force."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from .git_process import fixed_git_command, fixed_git_environment
from .registry import utc_now
from .root import FloatiRoot

DECLARATIONS_RELATIVE = Path("state") / "row-tree-repos.v0.json"
RULE = "git_worktree_remove"
GitRun = Callable[[Path, Sequence[str]], subprocess.CompletedProcess]


def _default_git_run(repository: Path, arguments: Sequence[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        fixed_git_command("/usr/bin/git", repository, arguments),
        env=fixed_git_environment("/usr/bin/git"),
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )


def _load_repos(root: FloatiRoot) -> List[Path]:
    path = root.path / DECLARATIONS_RELATIVE
    if path.is_symlink() or not path.is_file():
        return []
    try:
        raw = json.loads(path.read_bytes())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return []
    if not isinstance(raw, dict):
        return []
    names = raw.get("repos")
    if not isinstance(names, list):
        return []
    repos: List[Path] = []
    for item in names:
        if not isinstance(item, str) or not item.startswith("/") or "/" == item:
            continue
        candidate = Path(item)
        if candidate.is_dir():
            repos.append(candidate)
    return repos


def _parse_worktrees(text: str) -> List[Dict[str, Any]]:
    entries: List[Dict[str, Any]] = []
    current: Dict[str, Any] = {}
    for line in text.splitlines():
        if not line:
            if current:
                entries.append(current)
                current = {}
            continue
        if line.startswith("worktree "):
            if current:
                entries.append(current)
            current = {
                "path": line[len("worktree "):],
                "head": "",
                "detached": False,
            }
        elif line.startswith("HEAD "):
            current["head"] = line[len("HEAD "):]
        elif line == "detached":
            current["detached"] = True
    if current:
        entries.append(current)
    return entries


def _receipt(now: str, path: Path, reason: str) -> Dict[str, str]:
    return {"utc": now, "path": str(path), "rule": RULE, "reason": reason}


def _resolve_claimed(claimed_trees: Iterable[Path]) -> Set[Path]:
    claimed: Set[Path] = set()
    for tree in claimed_trees:
        try:
            claimed.add(Path(tree).resolve())
        except OSError:
            continue
    return claimed


def _default_refs(git_run: GitRun, repo: Path) -> Tuple[Optional[str], Optional[str]]:
    origin_ref = None
    local_ref = None
    origin_head = git_run(repo, ("symbolic-ref", "--quiet", "refs/remotes/origin/HEAD"))
    if origin_head.returncode == 0 and origin_head.stdout.strip():
        origin_ref = origin_head.stdout.strip()
        local_ref = "refs/heads/" + origin_ref.rsplit("/", 1)[-1]
    if origin_ref is None:
        for name in ("main", "master"):
            remote = git_run(repo, ("rev-parse", "--verify", "refs/remotes/origin/" + name))
            if remote.returncode == 0:
                origin_ref = "refs/remotes/origin/" + name
                local_ref = "refs/heads/" + name
                break
    if local_ref is None:
        for name in ("main", "master"):
            probe = git_run(repo, ("rev-parse", "--verify", "refs/heads/" + name))
            if probe.returncode == 0:
                local_ref = "refs/heads/" + name
                break
    return local_ref, origin_ref


def _is_ancestor(git_run: GitRun, repo: Path, commit: str, ref: str) -> Optional[bool]:
    result = git_run(repo, ("merge-base", "--is-ancestor", commit, ref))
    if result.returncode == 0:
        return True
    if result.returncode == 1:
        return False
    return None


def _classify(
    git_run: GitRun,
    repo: Path,
    tree: Dict[str, Any],
    claimed: Set[Path],
    local_ref: Optional[str],
    origin_ref: Optional[str],
) -> Tuple[str, str]:
    try:
        path = Path(tree["path"]).resolve()
    except OSError:
        return "keep", "probe_error"
    if path in claimed:
        return "keep", "claimed"
    if not local_ref:
        return "keep", "probe_error"
    head = str(tree.get("head") or "")
    if not head:
        return "keep", "probe_error"
    try:
        status = git_run(path, ("status", "--porcelain"))
    except OSError:
        return "keep", "probe_error"
    if status.returncode != 0:
        return "keep", "probe_error"
    landed = _is_ancestor(git_run, path, head, local_ref)
    if landed is None:
        return "keep", "probe_error"
    if tree.get("detached") and not landed:
        return "keep", "detached_unreferenced"
    if status.stdout.strip():
        return "keep", "dirty"
    if not landed:
        return "keep", "not_landed"
    if origin_ref:
        pushed = _is_ancestor(git_run, path, head, origin_ref)
        if pushed is None:
            return "keep", "probe_error"
        if not pushed:
            return "keep", "unpushed"
    return "offer", "landed_clean_unclaimed"


def offer_row_trees(
    root: FloatiRoot,
    *,
    apply: bool = False,
    claimed_trees: Iterable[Path] = (),
    git_run: Optional[GitRun] = None,
    now: Optional[str] = None,
    derivation_failed: bool = False,
) -> Dict[str, Any]:
    runner = git_run or _default_git_run
    stamp = now or utc_now()
    if derivation_failed:
        apply = False
    claimed = _resolve_claimed(claimed_trees)
    offered: List[Dict[str, str]] = []
    retired: List[Dict[str, str]] = []
    kept: List[Dict[str, str]] = []

    for repo in _load_repos(root):
        try:
            listed = runner(repo, ("worktree", "list", "--porcelain"))
        except OSError:
            kept.append(_receipt(stamp, repo, "probe_error"))
            continue
        if listed.returncode != 0:
            kept.append(_receipt(stamp, repo, "probe_error"))
            continue
        entries = _parse_worktrees(listed.stdout)
        if not entries:
            continue
        try:
            primary = Path(entries[0]["path"]).resolve()
        except (OSError, KeyError):
            primary = repo.resolve()
        local_ref, origin_ref = _default_refs(runner, repo)
        for tree in entries[1:]:
            try:
                path = Path(tree["path"]).resolve()
            except OSError:
                kept.append(_receipt(stamp, Path(str(tree.get("path") or repo)), "probe_error"))
                continue
            if path == primary:
                continue
            if derivation_failed:
                decision, reason = "keep", "probe_error"
            else:
                decision, reason = _classify(runner, repo, tree, claimed, local_ref, origin_ref)
            if decision != "offer":
                kept.append(_receipt(stamp, path, reason))
                continue
            offered.append(_receipt(stamp, path, reason))
            if not apply:
                continue
            again, again_reason = _classify(runner, repo, tree, claimed, local_ref, origin_ref)
            if again != "offer":
                kept.append(_receipt(stamp, path, again_reason))
                offered.pop()
                continue
            try:
                removed = runner(repo, ("worktree", "remove", os.fspath(path)))
            except OSError:
                kept.append(_receipt(stamp, path, "probe_error"))
                offered.pop()
                continue
            if removed.returncode != 0:
                kept.append(_receipt(stamp, path, "probe_error"))
                offered.pop()
                continue
            retired.append(_receipt(stamp, path, reason))

    return {
        "dry_run": not apply,
        "offered": offered,
        "retired": retired,
        "kept": kept,
    }

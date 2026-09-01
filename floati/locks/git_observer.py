"""Bounded local Git observations used by the DARK Locks package."""

from __future__ import annotations

import os
import re
import subprocess
import hashlib
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Iterable, Mapping, Optional

from ..errors import ProtocolRefusal
from ..git_process import require_complete_history_for_reachability
from .contracts import validate_full_ref, validate_witness


_HEX40 = re.compile(r"[0-9a-f]{40}\Z")
_MAX_OUTPUT_BYTES = 1_048_576
_MAX_REFS = 4_096


@dataclass(frozen=True)
class RepositoryIdentity:
    path: Path
    common_git_dir: Path
    device: int
    inode: int


@dataclass(frozen=True)
class GitRef:
    ref: str
    oid: str
    tree_oid: str


@dataclass(frozen=True)
class WitnessResult:
    ref: GitRef
    witness: Mapping[str, object]
    holds: bool


class GitObserver:
    def __init__(self, repository: Path, *, timeout_seconds: float = 5.0) -> None:
        if not isinstance(repository, Path) or not repository.is_absolute() or repository.is_symlink():
            raise ProtocolRefusal(
                "repository_invalid",
                "Git observation requires one canonical absolute non-symlink directory",
            )
        resolved = repository.resolve()
        if resolved != repository or not resolved.is_dir():
            raise ProtocolRefusal("repository_invalid", "Git repository path is not canonical")
        if type(timeout_seconds) not in {int, float} or isinstance(timeout_seconds, bool) or not 0 < timeout_seconds <= 30:
            raise ProtocolRefusal("git_timeout_invalid", "Git observation timeout is out of bounds")
        self.repository = resolved
        self.timeout_seconds = float(timeout_seconds)
        common = self._path_output(self._run(
            self.repository,
            "rev-parse",
            "--path-format=absolute",
            "--git-common-dir",
        ), "git common directory")
        stat = common.stat()
        self.identity = RepositoryIdentity(
            path=self.repository,
            common_git_dir=common,
            device=stat.st_dev,
            inode=stat.st_ino,
        )

    @staticmethod
    def _environment() -> dict[str, str]:
        return {
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

    def _run(
        self,
        cwd: Path,
        *arguments: str,
        stdin: Optional[bytes] = None,
        allow_status: Iterable[int] = (0,),
    ) -> subprocess.CompletedProcess[bytes]:
        if not isinstance(cwd, Path) or not cwd.is_absolute() or cwd.is_symlink():
            raise ProtocolRefusal("repository_invalid", "Git working directory is not canonical")
        argv = [
            "/usr/bin/git",
            "--no-optional-locks",
            "--no-replace-objects",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "core.fsmonitor=false",
            "-C",
            os.fspath(cwd),
            *arguments,
        ]
        try:
            completed = subprocess.run(
                argv,
                input=stdin,
                capture_output=True,
                check=False,
                timeout=self.timeout_seconds,
                env=self._environment(),
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ProtocolRefusal("git_observation_unavailable", "bounded local Git observation failed") from exc
        if len(completed.stdout) > _MAX_OUTPUT_BYTES or len(completed.stderr) > _MAX_OUTPUT_BYTES:
            raise ProtocolRefusal("git_observation_overflow", "Git observation exceeded its output bound")
        allowed = frozenset(allow_status)
        if completed.returncode not in allowed:
            detail = completed.stderr[:512].decode("utf-8", "replace").strip()
            raise ProtocolRefusal(
                "git_observation_failed",
                "Git observation returned nonzero" + ((": " + detail) if detail else ""),
            )
        return completed

    @staticmethod
    def _text_output(completed: subprocess.CompletedProcess[bytes], field: str) -> str:
        try:
            return completed.stdout.decode("utf-8", "strict").strip()
        except UnicodeDecodeError as exc:
            raise ProtocolRefusal("git_observation_malformed", f"{field} is not strict UTF-8") from exc

    def _path_output(self, completed: subprocess.CompletedProcess[bytes], field: str) -> Path:
        text = self._text_output(completed, field)
        candidate = Path(text)
        if not candidate.is_absolute() or candidate.is_symlink():
            raise ProtocolRefusal("git_observation_malformed", f"{field} is not a canonical absolute path")
        resolved = candidate.resolve()
        if resolved != candidate or not resolved.exists():
            raise ProtocolRefusal("git_observation_malformed", f"{field} did not resolve canonically")
        return resolved

    def common_git_dir(self, worktree: Path) -> Path:
        return self._path_output(self._run(
            worktree,
            "rev-parse",
            "--path-format=absolute",
            "--git-common-dir",
        ), "worktree common directory")

    def unique_reachable_commits(self, worktree: Path) -> tuple[str, ...]:
        if not isinstance(worktree, Path) or not worktree.is_absolute() or worktree.is_symlink():
            raise ProtocolRefusal("worktree_invalid", "cleanup assessment requires one canonical absolute worktree")
        resolved = worktree.resolve()
        if resolved != worktree or not resolved.is_dir():
            raise ProtocolRefusal("worktree_invalid", "cleanup assessment requires one canonical absolute worktree")
        worktree = resolved
        if self.common_git_dir(worktree) != self.identity.common_git_dir:
            raise ProtocolRefusal("worktree_repository_mismatch", "worktree belongs to another Git common directory")
        require_complete_history_for_reachability(worktree)
        head = self._text_output(
            self._run(worktree, "rev-parse", "--verify", "HEAD^{commit}"),
            "worktree head",
        )
        self._require_sha(head, "worktree head")
        candidate = self._sha_lines(
            self._text_output(self._run(worktree, "rev-list", head), "worktree reachability"),
            "worktree reachability",
        )
        ref_text = self._text_output(
            self._run(self.repository, "for-each-ref", "--format=%(refname)"),
            "repository refs",
        )
        refs = tuple(line for line in ref_text.splitlines() if line)
        if len(refs) > _MAX_REFS or any(not line.startswith("refs/") or line.startswith("-") for line in refs):
            raise ProtocolRefusal("git_observation_malformed", "repository ref inventory is out of bounds")
        referenced: set[str] = set()
        if refs:
            payload = ("\n".join(refs) + "\n").encode("utf-8")
            referenced = self._sha_lines(
                self._text_output(
                    self._run(self.repository, "rev-list", "--stdin", stdin=payload),
                    "ref reachability",
                ),
                "ref reachability",
            )
        return tuple(sorted(candidate - referenced))

    def resolve_ref(self, full_ref: object) -> GitRef:
        ref = validate_full_ref(full_ref, "ref", integrity=False)
        oid = self._text_output(
            self._run(self.repository, "rev-parse", "--verify", ref + "^{commit}"),
            "ref object",
        )
        tree_oid = self._text_output(
            self._run(self.repository, "rev-parse", "--verify", ref + "^{tree}"),
            "ref tree",
        )
        return GitRef(
            ref=ref,
            oid=self._require_sha(oid, "ref object"),
            tree_oid=self._require_sha(tree_oid, "ref tree"),
        )

    def verify_witness(self, full_ref: object, witness: object) -> WitnessResult:
        ref = self.resolve_ref(full_ref)
        normalized = validate_witness(
            dict(witness) if isinstance(witness, Mapping) else witness,
            integrity=False,
        )
        coordinate = ref.ref + ":" + str(normalized["path"])
        kind = str(normalized["kind"])
        if kind in {"path_present", "path_absent"}:
            result = self._run(
                self.repository,
                "cat-file",
                "-e",
                coordinate,
                allow_status=(0, 128),
            )
            present = result.returncode == 0
            holds = present if kind == "path_present" else not present
        else:
            result = self._run(
                self.repository,
                "cat-file",
                "blob",
                coordinate,
                allow_status=(0, 128),
            )
            if result.returncode != 0:
                holds = False
            elif kind == "blob_sha256":
                holds = hashlib.sha256(result.stdout).hexdigest() == normalized["sha256"]
            else:
                try:
                    text = result.stdout.decode("utf-8", "strict")
                except UnicodeDecodeError as exc:
                    raise ProtocolRefusal(
                        "content_witness_unreadable",
                        "file_contains_utf8 witness selected a non-UTF-8 blob",
                    ) from exc
                holds = str(normalized["needle"]) in text
        return WitnessResult(
            ref=ref,
            witness=MappingProxyType(normalized),
            holds=holds,
        )

    def trees_agree(self, left_ref: object, right_ref: object) -> bool:
        left = self.resolve_ref(left_ref)
        right = self.resolve_ref(right_ref)
        result = self._run(
            self.repository,
            "diff",
            "--quiet",
            "--no-ext-diff",
            left.tree_oid,
            right.tree_oid,
            "--",
            allow_status=(0, 1),
        )
        return result.returncode == 0

    @staticmethod
    def _require_sha(value: str, field: str) -> str:
        if _HEX40.fullmatch(value) is None:
            raise ProtocolRefusal("git_observation_malformed", f"{field} contains a non-SHA value")
        return value

    @classmethod
    def _sha_lines(cls, value: str, field: str) -> set[str]:
        if not value:
            return set()
        lines = value.splitlines()
        if any(_HEX40.fullmatch(line) is None for line in lines):
            raise ProtocolRefusal("git_observation_malformed", f"{field} contains malformed output")
        return set(lines)

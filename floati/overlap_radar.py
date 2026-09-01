"""Local, advisory overlap evidence for parallel Git work."""

from __future__ import annotations

import ast
import re
import subprocess
from pathlib import Path
from typing import Mapping, Sequence

from .errors import ProtocolRefusal
from .git_process import fixed_git_command, fixed_git_environment


_SIGNAL_STAMPS = frozenset({"MEASURED", "DERIVED", "HEURISTIC", "UNKNOWN"})
_ATTEMPT_BINDING_FIELDS = frozenset(
    {"attempt_id", "claim_id", "lease_id", "worker_session_id"}
)
_HUNK_HEADER = re.compile(
    r"^@@ -\d+(?:,\d+)? \+(?P<start>\d+)(?:,(?P<count>\d+))? @@"
)


def _git(repository: Path, *arguments: str) -> str:
    try:
        result = subprocess.run(
            fixed_git_command("/usr/bin/git", repository, arguments),
            env=fixed_git_environment("/usr/bin/git"),
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ProtocolRefusal(
            "overlap_git_unavailable", f"git could not derive overlap evidence: {exc}"
        ) from exc
    if result.returncode != 0:
        raise ProtocolRefusal(
            "overlap_git_unavailable",
            result.stderr.strip() or "git overlap inspection failed",
        )
    return result.stdout


def _resolve_commit(repository: Path, ref: str) -> str:
    return _git(repository, "rev-parse", "--verify", f"{ref}^{{commit}}").strip()


def _changed_paths(repository: Path, base_sha: str, branch_sha: str) -> tuple[str, ...]:
    output = _git(
        repository,
        "diff",
        "--name-only",
        "-z",
        "--no-renames",
        base_sha,
        branch_sha,
        "--",
    )
    return tuple(path for path in output.split("\0") if path)


def _changed_lines(
    repository: Path, base_sha: str, branch_sha: str, path: str
) -> frozenset[int]:
    output = _git(
        repository,
        "diff",
        "--unified=0",
        "--no-renames",
        base_sha,
        branch_sha,
        "--",
        path,
    )
    lines: set[int] = set()
    for raw_line in output.splitlines():
        matched = _HUNK_HEADER.match(raw_line)
        if matched is None:
            continue
        start = int(matched.group("start"))
        count = int(matched.group("count") or "1")
        lines.update(range(start, start + count))
    return frozenset(lines)


def _changed_python_symbols(
    repository: Path,
    base_sha: str,
    branch_sha: str,
    path: str,
) -> frozenset[str]:
    changed_lines = _changed_lines(repository, base_sha, branch_sha, path)
    if not changed_lines:
        return frozenset()
    source = _git(repository, "show", f"{branch_sha}:{path}")
    try:
        tree = ast.parse(source, filename=path)
    except SyntaxError:
        return frozenset()
    symbols = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        end_line = getattr(node, "end_lineno", node.lineno)
        if any(node.lineno <= line <= end_line for line in changed_lines):
            symbols.add(node.name)
    return frozenset(symbols)


def _is_schema(path: str) -> bool:
    return path.endswith(".schema.json") or (
        path.startswith("schemas/") and path.endswith(".json")
    )


def validate_signal(signal: Mapping[str, object]) -> dict[str, object]:
    stamp = signal.get("stamp")
    if stamp not in _SIGNAL_STAMPS:
        raise ProtocolRefusal(
            "overlap_signal_stamp_invalid",
            f"signal stamp must be one of {sorted(_SIGNAL_STAMPS)}, got {stamp!r}",
        )
    return dict(signal)


def _validate_attempt_binding(value: object) -> object:
    if value == "absent_predispatch":
        return value
    if not isinstance(value, Mapping) or set(value) != _ATTEMPT_BINDING_FIELDS:
        raise ProtocolRefusal(
            "overlap_attempt_binding_invalid",
            "attempt binding must be absent_predispatch or the exact complete binding object",
        )
    for field in sorted(_ATTEMPT_BINDING_FIELDS):
        member = value[field]
        if not isinstance(member, str) or not member or len(member) > 512:
            raise ProtocolRefusal(
                "overlap_attempt_binding_invalid",
                f"attempt binding {field} must be a nonempty bounded string",
            )
    return dict(value)


def hard_concurrency_keys(signals: Sequence[Mapping[str, object]]) -> tuple[str, ...]:
    keys = []
    for raw_signal in signals:
        signal = validate_signal(raw_signal)
        if signal.get("hard_lock") is not True:
            continue
        if signal["stamp"] == "HEURISTIC":
            raise ProtocolRefusal(
                "overlap_heuristic_hard_lock_refused",
                "a HEURISTIC overlap signal cannot drive a hard concurrency lock",
            )
        keys.append(str(signal["coordinate"]))
    return tuple(keys)


def derive_overlap_report(
    repository_root: Path,
    base_ref: str,
    left_ref: str,
    right_ref: str,
    *,
    attempt_binding: object = "absent_predispatch",
) -> dict[str, object]:
    root = Path(repository_root).expanduser().resolve()
    actual_root = Path(
        _git(root, "rev-parse", "--show-toplevel").strip()
    ).resolve()
    if actual_root != root:
        raise ProtocolRefusal(
            "overlap_repository_root_invalid",
            f"repository root must be {actual_root}, got {root}",
        )

    base_sha = _resolve_commit(root, base_ref)
    left_sha = _resolve_commit(root, left_ref)
    right_sha = _resolve_commit(root, right_ref)
    shared_paths = sorted(
        set(_changed_paths(root, base_sha, left_sha))
        & set(_changed_paths(root, base_sha, right_sha))
    )

    signals: list[dict[str, object]] = []
    for path in shared_paths:
        if _is_schema(path):
            signals.append(
                validate_signal(
                    {
                        "kind": "same_schema",
                        "coordinate": path,
                        "stamp": "MEASURED",
                        "hard_lock": True,
                        "detail": "both refs changed the same schema path",
                    }
                )
            )
        if not path.endswith(".py"):
            continue
        left_symbols = _changed_python_symbols(root, base_sha, left_sha, path)
        right_symbols = _changed_python_symbols(root, base_sha, right_sha, path)
        for symbol in sorted(left_symbols & right_symbols):
            signals.append(
                validate_signal(
                    {
                        "kind": "same_symbol",
                        "coordinate": f"{path}:{symbol}",
                        "stamp": "MEASURED",
                        "hard_lock": True,
                        "detail": "both refs changed lines within the same Python symbol",
                    }
                )
            )

    return {
        "schema_version": 1,
        "attempt_binding": _validate_attempt_binding(attempt_binding),
        "inputs": {
            "repository_root": str(root),
            "base_ref": base_ref,
            "base_sha": base_sha,
            "left_ref": left_ref,
            "left_sha": left_sha,
            "right_ref": right_ref,
            "right_sha": right_sha,
        },
        "signals": signals,
        "warnings": [],
    }

#!/usr/bin/env python3
"""Refuse a projected public tree that carries a secret-shaped token.

This is a rehearsal step, not a product dependency. The four shapes are the
B7 list: a GitHub PAT, an AWS access key, a private-key block, and a 40-hex
beside the word token. Needles are hex-built so this file never carries a
consecutive secret shape of its own.

Gitleaks is operator-declared only. An undeclared or missing executable is a
typed absence, never a skip and never a PATH probe.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Iterable

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT))

from floati import fleet_update  # noqa: E402
from floati.errors import ProtocolRefusal  # noqa: E402


COMMAND = "export-secret-fence"
REFUSAL_CODE = "public_export_secret_fence_failed"
EXCLUDED_PARTS = frozenset((".git", "__pycache__"))
MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_GIT_OUTPUT = 16 * 1024 * 1024
TOKEN_NEAR_BYTES = 32

# Stage names from export-rehearsal receipt banks, plus this fence.
REHEARSAL_STAGES = (
    "projection",
    "name_fence",
    "workflow_runner_fence",
    "secret_fence",
    "fence3_negative_control",
    "projected_suite",
    "baseline",
)

_GHP_PREFIX = bytes.fromhex("6768705f").decode("ascii")
_GITHUB_PAT_PREFIX = bytes.fromhex("6769746875625f7061745f").decode("ascii")
_AKIA_PREFIX = bytes.fromhex("414b4941").decode("ascii")
_PEM_BEGIN = bytes.fromhex("2d2d2d2d2d424547494e20").decode("ascii")
_PEM_TAIL = bytes.fromhex("50524956415445204b45592d2d2d2d2d").decode("ascii")

_GITHUB_CLASSIC = re.compile(re.escape(_GHP_PREFIX) + r"[A-Za-z0-9]{36}")
_GITHUB_FINE = re.compile(re.escape(_GITHUB_PAT_PREFIX) + r"[A-Za-z0-9_]{22,}")
_AWS_KEY = re.compile(re.escape(_AKIA_PREFIX) + r"[A-Z0-9]{16}")
_PEM_BLOCK = re.compile(
    re.escape(_PEM_BEGIN) + r"[A-Z ]{0,32}" + re.escape(_PEM_TAIL)
)
_HEX40 = re.compile(r"(?<![0-9A-Fa-f])[0-9A-Fa-f]{40}(?![0-9A-Fa-f])")
_TOKEN_WORD = re.compile(r"(?<![A-Za-z0-9_])token(?![A-Za-z0-9_])", re.IGNORECASE)


def observe_gitleaks(declared: str | None) -> dict[str, object]:
    """Record one operator-declared scanner, or a typed absence.

    Never searches PATH. The canonical-executable predicate is
    ``fleet_update._explicit_executable`` (imported, not copied). A missing or
    non-canonical path is absence, not a skip.
    """

    if declared is None or declared == "":
        return {
            "reason": "operator_executable_absent",
            "state": "undeclared",
        }
    try:
        path = fleet_update._explicit_executable(
            declared, "gitleaks_executable_invalid"
        )
    except ProtocolRefusal:
        return {
            "reason": "operator_executable_absent",
            "state": "absent",
        }
    return {
        "path": path,
        "reason": "operator_declared",
        "state": "declared",
    }


def _entries(root: Path) -> Iterable[Path]:
    tracked = _tracked_paths(root)
    if tracked is not None:
        for relative in tracked:
            path = root / relative
            if path.is_symlink() or path.is_file():
                yield path
        return
    for path in sorted(root.rglob("*"), key=lambda candidate: os.fsencode(candidate)):
        relative = path.relative_to(root)
        if any(part in EXCLUDED_PARTS for part in relative.parts):
            continue
        if path.is_symlink() or path.is_file():
            yield path


def _tracked_paths(root: Path) -> list[Path] | None:
    environment = {
        key: value for key, value in os.environ.items() if not key.startswith("GIT_")
    }
    environment.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_PAGER": "cat"})
    try:
        probe = subprocess.run(
            ["/usr/bin/git", "rev-parse", "--is-inside-work-tree"],
            cwd=root,
            env=environment,
            check=False,
            capture_output=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if probe.returncode != 0 or probe.stdout.strip() != b"true":
        return None
    try:
        listing = subprocess.run(
            ["/usr/bin/git", "ls-files", "-z", "--cached"],
            cwd=root,
            env=environment,
            check=False,
            capture_output=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if listing.returncode != 0 or len(listing.stdout) > MAX_GIT_OUTPUT:
        return None
    paths: list[Path] = []
    for raw in listing.stdout.split(b"\0"):
        if not raw:
            continue
        relative = Path(os.fsdecode(raw))
        if relative.is_absolute() or ".." in relative.parts:
            continue
        if any(part in EXCLUDED_PARTS for part in relative.parts):
            continue
        paths.append(relative)
    return sorted(paths, key=lambda path: os.fsencode(path))


def _line_number(data: str, index: int) -> int:
    return data.count("\n", 0, index) + 1


def _near_token(line: str, start: int, end: int) -> bool:
    for match in _TOKEN_WORD.finditer(line):
        token_start, token_end = match.span()
        distance = min(abs(start - token_end), abs(end - token_start))
        if distance <= TOKEN_NEAR_BYTES:
            return True
    return False


def scan_tree(
    root: Path, census: dict[str, int] | None = None
) -> list[dict[str, object]]:
    """Return stable path/code findings for secret-shaped tokens."""

    base = Path(root).resolve()
    counts = census if census is not None else {}
    counts["files_examined"] = 0
    counts["candidates_examined"] = 0
    findings: list[dict[str, object]] = []

    for path in _entries(base):
        relative = path.relative_to(base).as_posix()
        if path.is_symlink() or not path.is_file():
            continue
        counts["files_examined"] += 1
        try:
            size = path.stat().st_size
        except OSError:
            continue
        if size > MAX_FILE_BYTES:
            continue
        try:
            data = path.read_bytes()
        except OSError:
            continue
        if b"\x00" in data[:8192]:
            continue
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            text = data.decode("latin-1")

        def add(code: str, index: int) -> None:
            findings.append(
                {
                    "code": code,
                    "line": _line_number(text, index),
                    "path": relative,
                }
            )

        for match in _GITHUB_CLASSIC.finditer(text):
            counts["candidates_examined"] += 1
            add("github_pat", match.start())
        for match in _GITHUB_FINE.finditer(text):
            suffix = match.group(0)[len(_GITHUB_PAT_PREFIX) :]
            if not any(character.isdigit() for character in suffix):
                continue
            counts["candidates_examined"] += 1
            add("github_pat", match.start())
        for match in _AWS_KEY.finditer(text):
            counts["candidates_examined"] += 1
            add("aws_access_key", match.start())
        for match in _PEM_BLOCK.finditer(text):
            counts["candidates_examined"] += 1
            add("private_key_block", match.start())
        position = 0
        for line in text.split("\n"):
            hexes = list(_HEX40.finditer(line))
            tokens = list(_TOKEN_WORD.finditer(line))
            counts["candidates_examined"] += len(hexes) + len(tokens)
            for hex_match in hexes:
                if _near_token(line, hex_match.start(), hex_match.end()):
                    add("token_adjacent_hex40", position + hex_match.start())
            position += len(line) + 1

    return sorted(
        findings,
        key=lambda finding: (
            str(finding["path"]),
            str(finding["code"]),
            int(finding.get("line", 0)),
        ),
    )


def _artifact(status: str, evidence: dict[str, object]) -> str:
    return json.dumps(
        {
            "artifact_version": 0,
            "command": COMMAND,
            "evidence": evidence,
            "status": status,
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("root")
    parser.add_argument("--gitleaks-executable", default=None)
    args = parser.parse_args(argv)
    supplied = Path(args.root)
    if supplied.is_symlink() or not supplied.is_dir():
        print(
            _artifact(
                "refused",
                {
                    "code": "public_export_secret_fence_root_invalid",
                    "detail": "secret fence requires one existing non-symlink directory",
                    "findings": [],
                    "gitleaks": observe_gitleaks(args.gitleaks_executable),
                },
            )
        )
        return 20

    root = supplied.resolve()
    census: dict[str, int] = {}
    findings = scan_tree(root, census=census)
    gitleaks = observe_gitleaks(args.gitleaks_executable)
    if findings:
        print(
            _artifact(
                "refused",
                {
                    "code": REFUSAL_CODE,
                    "detail": "projected tree contains a secret-shaped token",
                    "findings": findings,
                    "files_examined": census["files_examined"],
                    "candidates_examined": census["candidates_examined"],
                    "gitleaks": gitleaks,
                    "root": str(root),
                },
            )
        )
        return 20
    print(
        _artifact(
            "ok",
            {
                "findings": [],
                "files_examined": census["files_examined"],
                "candidates_examined": census["candidates_examined"],
                "gitleaks": gitleaks,
                "root": str(root),
            },
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

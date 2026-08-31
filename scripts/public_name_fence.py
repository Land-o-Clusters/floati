#!/usr/bin/env python3
"""Encoding-agnostic byte fence for a staged public Floati tree."""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import subprocess
import sys
import tokenize
from pathlib import Path
from typing import Iterable


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT))

from floati.scrub import scan_generated_tree  # noqa: E402


COMMAND = "public-name-fence"
ENCODINGS = ("utf-8", "utf-16-le", "utf-16-be", "utf-32-le", "utf-32-be")
OWNER_USERNAME = bytes.fromhex("63687269736d656e656e64657a").decode("ascii")
FENCE_TOKENS = (
    ("operator_home_path", bytes.fromhex("2f55736572732f").decode("ascii")),
    ("private_tmp_path", bytes.fromhex("2f707269766174652f746d70").decode("ascii")),
    ("owner_username", OWNER_USERNAME),
)
EXCLUDED_PARTS = frozenset((".git", "__pycache__"))
MAX_GIT_OUTPUT = 16 * 1024 * 1024
PATH_LITERAL_CONTRACT_PREFIXES = ("bundle/c7.1/", "bundle/c7.2/", "schemas/")
SEAT_NAME_EXEMPT_PATHS = frozenset(
    ("docs/capability-matrix.md", "docs/capability-matrix.v0.json")
)
_VERIFICATION_SEAT = bytes.fromhex("67726f6b").decode("ascii")
_VERIFICATION_SEAT_EXPLICIT = bytes.fromhex(
    "67726f6b2d7468652d73656174"
).decode("ascii")
_ARCHITECT_SEAT = bytes.fromhex("6661626c65").decode("ascii")
_BUILD_SEAT_PREFIX = bytes.fromhex("616c696365").decode("ascii")
_LANE_SEAT_PREFIX = bytes.fromhex("6c616e652d").decode("ascii")
_SHORT_BUILD_SEAT = bytes.fromhex("736f6c").decode("ascii")
_SEAT_PATTERN = re.compile(
    rf"(?<![A-Za-z0-9_])(?:"
    rf"{re.escape(_VERIFICATION_SEAT_EXPLICIT)}|"
    rf"{re.escape(_VERIFICATION_SEAT)}(?![-_]build)|"
    rf"{re.escape(_ARCHITECT_SEAT)}|"
    rf"{re.escape(_BUILD_SEAT_PREFIX)}[A-Za-z0-9._-]*|"
    rf"(?<!verification )(?<!build ){re.escape(_LANE_SEAT_PREFIX)}[A-Za-z0-9._-]+|"
    rf"{re.escape(_SHORT_BUILD_SEAT)}"
    rf")(?![A-Za-z0-9_])",
    re.IGNORECASE,
)
SEAT_NAME_SITE_ALLOWLIST = (
    (
        _VERIFICATION_SEAT,
        "docs/COPY-LEDGER.md",
        "third-party product named by the reviewed wake-daemon copy row",
    ),
    (
        _VERIFICATION_SEAT,
        "docs/assets/floati-multifleet-dark.svg",
        "third-party product label in the governed dark architecture asset",
    ),
    (
        _VERIFICATION_SEAT,
        "docs/assets/floati-multifleet-light.svg",
        "third-party product label in the governed light architecture asset",
    ),
)
SEAT_NAME_FILE_LINE_COUNT_ALLOWLIST = (
    (
        _VERIFICATION_SEAT,
        "docs/research/regatta/grok-build-tui-mechanisms-dr.md",
        21,
        "third-party product research record with a consultant-pinned line count",
    ),
)
SEAT_NAME_CONTENT_LINE_COUNT_ALLOWLIST = (
    (
        _VERIFICATION_SEAT,
        "README.md",
        f"| {_VERIFICATION_SEAT} |",
        1,
        "generated harness-product row with a fence-derived line count",
    ),
    (
        _VERIFICATION_SEAT,
        "README.md",
        f"| {_VERIFICATION_SEAT} / desktop |",
        1,
        "desktop harness-product row with a fence-derived line count",
    ),
)
_DATED_DESIGN_RECORD = re.compile(
    r"^docs/design/(?:.*/)?[^/]*-20\d{2}-\d{2}-\d{2}[^/]*\.md$"
)


def encoded_variants(token: str) -> tuple[bytes, ...]:
    """Return deterministic byte spellings for an ASCII fence token."""

    variants = {
        spelling.encode(encoding)
        for spelling in (token, token.lower(), token.upper())
        for encoding in ENCODINGS
    }
    return tuple(sorted(variants))


def is_record_class_path(relative: str) -> bool:
    """Return whether a path is a derived historical/evidence record."""

    return (
        relative.startswith(("docs/evidence/", "docs/rulings/"))
        or _DATED_DESIGN_RECORD.fullmatch(relative) is not None
    )


def _seat_name_exempt(relative: str) -> bool:
    return (
        relative in SEAT_NAME_EXEMPT_PATHS
        or is_record_class_path(relative)
    )


def _seat_name_hits(path: Path, data: bytes) -> list[tuple[int, str]]:
    if path.suffix == ".py":
        try:
            text = data.decode("utf-8")
            tokens = tokenize.generate_tokens(io.StringIO(text).readline)
            return sorted(
                (
                    (token.start[0], match.group(0))
                    for token in tokens
                    if token.type in (tokenize.STRING, tokenize.COMMENT)
                    for match in _SEAT_PATTERN.finditer(token.string)
                ),
                key=lambda hit: (hit[0], hit[1].casefold()),
            )
        except (UnicodeDecodeError, tokenize.TokenError, IndentationError):
            pass
    for encoding in ENCODINGS:
        try:
            text = data.decode(encoding)
        except UnicodeDecodeError:
            continue
        hits = [
            (text.count("\n", 0, match.start()) + 1, match.group(0))
            for match in _SEAT_PATTERN.finditer(text)
        ]
        if hits:
            return hits
    return []


def _seat_name_lines(path: Path, data: bytes) -> list[int]:
    return sorted({line for line, _token in _seat_name_hits(path, data)})


def _unallowlisted_seat_name_lines(
    relative: str, path: Path, data: bytes
) -> list[int]:
    hits = _seat_name_hits(path, data)
    used: set[int] = set()

    for token, allowed_path, expected_lines, _reason in (
        SEAT_NAME_FILE_LINE_COUNT_ALLOWLIST
    ):
        if allowed_path != relative:
            continue
        matching = [
            index
            for index, (_line, found) in enumerate(hits)
            if found.casefold() == token.casefold()
        ]
        if len({hits[index][0] for index in matching}) == expected_lines:
            used.update(matching)

    try:
        content_lines = data.decode("utf-8").splitlines()
    except UnicodeDecodeError:
        content_lines = []
    for token, allowed_path, content_anchor, expected_lines, _reason in (
        SEAT_NAME_CONTENT_LINE_COUNT_ALLOWLIST
    ):
        if allowed_path != relative:
            continue
        matching = [
            index
            for index, (line, found) in enumerate(hits)
            if found.casefold() == token.casefold()
            and line <= len(content_lines)
            and content_anchor.casefold() in content_lines[line - 1].casefold()
        ]
        if len({hits[index][0] for index in matching}) == expected_lines:
            used.update(matching)

    available = [
        index
        for index, (_token, allowed_path, _reason) in enumerate(
            SEAT_NAME_SITE_ALLOWLIST
        )
        if allowed_path == relative
    ]
    used_sites: set[int] = set()
    findings: set[int] = set()
    for hit_index, (line, token) in enumerate(hits):
        if hit_index in used:
            continue
        allowance = next(
            (
                index
                for index in available
                if index not in used_sites
                and SEAT_NAME_SITE_ALLOWLIST[index][0].casefold()
                == token.casefold()
            ),
            None,
        )
        if allowance is None:
            findings.add(line)
        else:
            used_sites.add(allowance)
    return sorted(findings)


def allowlist_measurements(root: Path) -> dict[str, int]:
    """Measure the four public-name allowances in the fence's own line/site units."""

    base = Path(root).resolve()

    def data_for(relative: str) -> bytes:
        path = base / relative
        if path.is_symlink() or not path.is_file():
            return b""
        try:
            return path.read_bytes()
        except OSError:
            return b""

    content_line_counts: list[int] = []
    for token, relative, anchor, _expected, _reason in (
        SEAT_NAME_CONTENT_LINE_COUNT_ALLOWLIST
    ):
        data = data_for(relative)
        try:
            lines = data.decode("utf-8").splitlines()
        except UnicodeDecodeError:
            lines = []
        matching_lines = {
            line
            for line, found in _seat_name_hits(base / relative, data)
            if found.casefold() == token.casefold()
            and line <= len(lines)
            and anchor.casefold() in lines[line - 1].casefold()
        }
        content_line_counts.append(len(matching_lines))

    research_token, research_path, _expected, _reason = (
        SEAT_NAME_FILE_LINE_COUNT_ALLOWLIST[0]
    )
    research_lines = {
        line
        for line, found in _seat_name_hits(
            base / research_path, data_for(research_path)
        )
        if found.casefold() == research_token.casefold()
    }
    vendor_sites = sum(
        any(
            found.casefold() == token.casefold()
            for _line, found in _seat_name_hits(base / relative, data_for(relative))
        )
        for token, relative, _reason in SEAT_NAME_SITE_ALLOWLIST
    )
    return {
        "readme_desktop_product_lines": content_line_counts[1],
        "readme_harness_product_lines": content_line_counts[0],
        "research_product_lines": len(research_lines),
        "vendor_product_sites": vendor_sites,
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
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError("public name fence tracked inventory unavailable") from exc
    if listing.returncode != 0 or len(listing.stdout) > MAX_GIT_OUTPUT:
        raise RuntimeError("public name fence tracked inventory unavailable")
    paths: list[Path] = []
    for raw in listing.stdout.split(b"\0"):
        if not raw:
            continue
        relative = Path(os.fsdecode(raw))
        if relative.is_absolute() or ".." in relative.parts:
            raise RuntimeError("public name fence tracked path is invalid")
        if any(part in EXCLUDED_PARTS for part in relative.parts):
            continue
        paths.append(relative)
    return sorted(paths, key=lambda path: os.fsencode(path))


def _scannable_data(path: Path, data: bytes) -> bytes:
    if path.suffix != ".py":
        return data
    try:
        text = data.decode("utf-8")
        tokens = tokenize.generate_tokens(io.StringIO(text).readline)
        surfaces = [
            token.string
            for token in tokens
            if token.type in (tokenize.STRING, tokenize.COMMENT)
        ]
    except (UnicodeDecodeError, tokenize.TokenError, IndentationError):
        return data
    return "\n".join(surfaces).encode("utf-8")


def scan_tree(root: Path) -> list[dict[str, object]]:
    """Return stable path/code findings without decoding staged files."""

    base = Path(root).resolve()
    findings: list[dict[str, object]] = []
    private_project_paths = set(scan_generated_tree(base))
    token_variants = {
        code: encoded_variants(token) for code, token in FENCE_TOKENS
    }

    for path in _entries(base):
        relative = path.relative_to(base).as_posix()
        seat_exempt = _seat_name_exempt(relative)
        if not seat_exempt and _SEAT_PATTERN.search(relative):
            findings.append({"code": "seat_name_path", "path": relative})
        if path.is_symlink():
            findings.append({"code": "symlink_path", "path": relative})
            continue
        try:
            data = path.read_bytes()
        except OSError:
            findings.append({"code": "file_unreadable", "path": relative})
            continue
        scannable = _scannable_data(path, data)
        for code, variants in token_variants.items():
            if relative.startswith(PATH_LITERAL_CONTRACT_PREFIXES):
                continue
            if any(variant in scannable for variant in variants):
                findings.append({"code": code, "path": relative})
        if not seat_exempt:
            for line in _unallowlisted_seat_name_lines(relative, path, data):
                findings.append({"code": "seat_name", "line": line, "path": relative})
        if relative in private_project_paths:
            findings.append({"code": "private_project_name", "path": relative})

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
    args = parser.parse_args(argv)
    supplied = Path(args.root)
    if supplied.is_symlink() or not supplied.is_dir():
        print(
            _artifact(
                "refused",
                {
                    "code": "public_name_fence_root_invalid",
                    "detail": "public name fence requires one existing non-symlink directory",
                    "findings": [],
                },
            )
        )
        return 20

    root = supplied.resolve()
    findings = scan_tree(root)
    measurements = allowlist_measurements(root)
    if findings:
        print(
            _artifact(
                "refused",
                {
                    "code": "public_name_fence_failed",
                    "detail": "staged public tree contains a forbidden byte sequence or path type",
                    "findings": findings,
                    "allowlist_measurements": measurements,
                    "root": str(root),
                },
            )
        )
        return 20
    print(
        _artifact(
            "ok",
            {
                "allowlist_measurements": measurements,
                "findings": [],
                "root": str(root),
            },
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

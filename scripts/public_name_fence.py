#!/usr/bin/env python3
"""Encoding-agnostic byte fence for a staged public Floati tree."""

from __future__ import annotations

import argparse
import codecs
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

from floati.identity_fence import (  # noqa: E402
    GOVERNED_TEMP_FENCES,
    HOME_PATTERN,
    HOME_PREFIX,
    OWNER_USERNAME,
)
from floati.scrub import scan_generated_tree  # noqa: E402


COMMAND = "public-name-fence"
ENCODINGS = ("utf-8", "utf-16-le", "utf-16-be", "utf-32-le", "utf-32-be")
FENCE_TOKENS = (
    ("operator_home_path", HOME_PREFIX),
    *GOVERNED_TEMP_FENCES,
    ("owner_username", OWNER_USERNAME),
)
TEMP_FENCE_CODES = frozenset(code for code, _prefix in GOVERNED_TEMP_FENCES)
EXCLUDED_PARTS = frozenset((".git", "__pycache__"))
MAX_GIT_OUTPUT = 16 * 1024 * 1024
PATH_LITERAL_CONTRACT_PREFIXES = ("bundle/c7.1/", "bundle/c7.2/", "schemas/")
_VERIFICATION_SEAT = bytes.fromhex("67726f6b").decode("ascii")
_VERIFICATION_SEAT_EXPLICIT = bytes.fromhex(
    "67726f6b2d7468652d73656174"
).decode("ascii")
_ARCHITECT_SEAT = bytes.fromhex("6661626c65").decode("ascii")
FLEET_REGISTRY_NODE_ROLE_LABELS = {
    bytes.fromhex("616c696365").decode("ascii"): "build lane",
    bytes.fromhex("616c6963652d63697479").decode("ascii"): "build lane",
    bytes.fromhex("616c6963652d6e6563726f").decode("ascii"): "build lane",
    _ARCHITECT_SEAT: "the architect",
    bytes.fromhex("6c616e652d617070").decode("ascii"): "build lane",
    bytes.fromhex("6c616e652d666c6f617469").decode("ascii"): "build lane",
    bytes.fromhex("6c616e652d707564646c65").decode("ascii"): "build lane",
    bytes.fromhex("6c616e652d736c6970776179").decode("ascii"): "build lane",
    bytes.fromhex("6c616e652d736f6c").decode("ascii"): "build lane",
    bytes.fromhex("6c616e652d7a636f6465").decode("ascii"): "build lane",
}
REVIEWED_FLEET_IDENTITY_ROLE_LABELS = {
    bytes.fromhex("707564646c652d666c656574").decode("ascii"): "the fleet",
    bytes.fromhex("707564646c652d666c6f6174692d617263686974656374").decode("ascii"): "the architect",
}
SEAT_ROLE_LABELS = {
    **FLEET_REGISTRY_NODE_ROLE_LABELS,
    **REVIEWED_FLEET_IDENTITY_ROLE_LABELS,
}
_EXACT_SEAT_IDS = "|".join(
    re.escape(token)
    for token in sorted(SEAT_ROLE_LABELS, key=lambda value: (-len(value), value))
)
_SEAT_PATTERN = re.compile(
    rf"(?<![A-Za-z0-9_-])(?:"
    rf"{re.escape(_VERIFICATION_SEAT_EXPLICIT)}|"
    rf"{_EXACT_SEAT_IDS}"
    rf")(?![A-Za-z0-9_-])",
    re.IGNORECASE,
)
_SEAT_PATH_PATTERN = re.compile(
    rf"(?<![A-Za-z0-9_])(?:{re.escape(_VERIFICATION_SEAT_EXPLICIT)}|{_EXACT_SEAT_IDS})"
    rf"(?![A-Za-z0-9_])",
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


def _decode_json_text(data: bytes) -> tuple[str, str, bytes]:
    for bom, encoding in (
        (codecs.BOM_UTF32_LE, "utf-32-le"),
        (codecs.BOM_UTF32_BE, "utf-32-be"),
        (codecs.BOM_UTF16_LE, "utf-16-le"),
        (codecs.BOM_UTF16_BE, "utf-16-be"),
        (codecs.BOM_UTF8, "utf-8"),
    ):
        if data.startswith(bom):
            return data[len(bom) :].decode(encoding), encoding, bom
    if len(data) >= 4:
        if data[:3] == b"\x00\x00\x00":
            encoding = "utf-32-be"
        elif data[1:4] == b"\x00\x00\x00":
            encoding = "utf-32-le"
        elif data[0] == 0 and data[2] == 0:
            encoding = "utf-16-be"
        elif data[1] == 0 and data[3] == 0:
            encoding = "utf-16-le"
        else:
            encoding = "utf-8"
    else:
        encoding = "utf-8"
    return data.decode(encoding), encoding, b""


def _encode_json_text(text: str, encoding: str, bom: bytes) -> bytes:
    return bom + text.encode(encoding)


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
    if path.suffix in (".json", ".jsonl"):
        documents: list[object] = []
        try:
            text, _encoding, _bom = _decode_json_text(data)
            if path.suffix == ".jsonl":
                documents = [json.loads(line) for line in text.splitlines() if line]
            else:
                documents = [json.loads(text)]
        except (UnicodeDecodeError, json.JSONDecodeError):
            documents = []

        capability_matrix = path.as_posix().endswith(
            "/docs/capability-matrix.v0.json"
        )

        def semantic_token(value: object, field: str | None = None) -> str | None:
            if isinstance(value, str):
                match = _SEAT_PATTERN.search(value)
                if match:
                    return match.group(0)
                if (
                    capability_matrix
                    and field is not None
                    and (
                        field.casefold() in {"seeded_by", "seat"}
                        or field.casefold().endswith("_seat")
                    )
                    and value.casefold() == _VERIFICATION_SEAT
                ):
                    return value
                return None
            if isinstance(value, list):
                return next(
                    (
                        token
                        for item in value
                        if (token := semantic_token(item, field))
                    ),
                    None,
                )
            if isinstance(value, dict):
                for key, item in value.items():
                    token = semantic_token(key) or semantic_token(
                        item, key if isinstance(key, str) else None
                    )
                    if token:
                        return token
            return None

        token = semantic_token(documents)
        if token:
            return [(1, token)]
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
        if _SEAT_PATH_PATTERN.search(relative):
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
        temp_found = False
        for code, variants in token_variants.items():
            if relative.startswith(PATH_LITERAL_CONTRACT_PREFIXES):
                continue
            if code in TEMP_FENCE_CODES and temp_found:
                continue
            if any(variant in scannable for variant in variants):
                findings.append({"code": code, "path": relative})
                if code in TEMP_FENCE_CODES:
                    temp_found = True
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

"""Verify the exact versioned deployable Floati bundle."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path, PurePosixPath
from typing import Dict, List, Set

from .role_templates import SHIPPED_ROLE_NAMES


EXPECTED_PROTOCOL_VERSION = "0"
EXPECTED_CANONICAL_REF = "refs/heads/lane/hm0"
MANIFEST_NAME = "bundle-manifest.v0.json"
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_DARK_DEPLOYMENT_PREFIXES = ("floati/locks/",)


def _deployable_paths(repo_root: Path) -> List[str]:
    paths = []
    for pattern in (
        "LICENSE",
        "bundle/c7.1/**/*",
        "bundle/c7.1/LICENSE",
        "bundle/c7.2/**/*",
        "bundle/c7.2/LICENSE",
        "schemas/LICENSE",
        "floati/**/*.py",
        "schemas/v[0-9]*/*.json",
        "scripts/floati",
        "scripts/floati-codex-wait",
    ):
        for path in repo_root.glob(pattern):
            if path.is_file() and "__pycache__" not in path.parts:
                relative = path.relative_to(repo_root).as_posix()
                if not relative.startswith(_DARK_DEPLOYMENT_PREFIXES):
                    paths.append(relative)
    for role in SHIPPED_ROLE_NAMES:
        path = repo_root / "roles" / "shipped" / f"{role}.json"
        if path.is_file():
            paths.append(path.relative_to(repo_root).as_posix())
    return sorted(set(paths))


def _has_symlink_component(root: Path, relative: str) -> bool:
    current = root
    for part in PurePosixPath(relative).parts:
        current = current / part
        try:
            if current.is_symlink():
                return True
        except OSError:
            return True
    return False


def verify_manifest(repo_root: Path) -> List[str]:
    root = Path(repo_root).resolve()
    manifest_path = root / MANIFEST_NAME
    if not manifest_path.is_file():
        return ["manifest_missing"]
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return ["manifest_unreadable"]
    if not isinstance(manifest, dict):
        return ["manifest_not_object"]

    errors: List[str] = []
    if manifest.get("schema_version") != 0 or isinstance(manifest.get("schema_version"), bool):
        errors.append("manifest_schema_version_mismatch")
    if manifest.get("protocol_version") != EXPECTED_PROTOCOL_VERSION:
        errors.append("protocol_version_mismatch")
    if manifest.get("canonical_ref") != EXPECTED_CANONICAL_REF:
        errors.append("canonical_ref_mismatch")

    entries = manifest.get("files")
    if not isinstance(entries, list):
        errors.append("manifest_files_invalid")
        return errors

    listed: List[str] = []
    valid_entries: Dict[str, str] = {}
    seen: Set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {"path", "sha256"}:
            errors.append("manifest_entry_invalid")
            continue
        relative = entry.get("path")
        digest = entry.get("sha256")
        if not isinstance(relative, str):
            errors.append("manifest_path_invalid")
            continue
        pure = PurePosixPath(relative)
        if pure.is_absolute() or not pure.parts or any(part in ("", ".", "..") for part in pure.parts):
            errors.append(f"manifest_path_invalid:{relative}")
            continue
        listed.append(relative)
        if relative in seen:
            errors.append(f"duplicate_manifest_path:{relative}")
            continue
        seen.add(relative)
        if not isinstance(digest, str) or not SHA256_PATTERN.fullmatch(digest):
            errors.append(f"manifest_digest_invalid:{relative}")
            continue
        valid_entries[relative] = digest

    if listed != sorted(listed):
        errors.append("manifest_order_invalid")
    expected = _deployable_paths(root)
    if set(listed) != set(expected):
        errors.append("tracked_set_mismatch")

    for relative, expected_digest in valid_entries.items():
        path = root / PurePosixPath(relative)
        if _has_symlink_component(root, relative):
            errors.append(f"file_symlink:{relative}")
            continue
        if not path.is_file():
            errors.append(f"file_missing:{relative}")
            continue
        actual_digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual_digest != expected_digest:
            errors.append(f"digest_mismatch:{relative}")
    return errors

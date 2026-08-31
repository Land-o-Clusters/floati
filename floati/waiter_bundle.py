"""One exact, shared inventory identity for the shipped Codex waiter runtime."""

from __future__ import annotations

import hashlib
from pathlib import Path

from .errors import ProtocolRefusal


def waiter_runtime_files(root: Path) -> tuple[Path, ...]:
    """Return the complete ordinary-file inventory in deterministic path order."""

    source = Path(root)
    if not source.is_absolute() or source.is_symlink() or not source.is_dir():
        raise ProtocolRefusal("fleet_update_target_invalid", "waiter source must be one canonical directory")
    paths = [source / "LICENSE", source / "scripts" / "floati-codex-wait"]
    paths.extend((source / "floati").glob("**/*.py"))
    paths.extend((source / "schemas").glob("v[0-9]*/*.json"))
    selected = tuple(sorted(paths, key=lambda path: path.relative_to(source).as_posix()))
    if not selected:
        raise ProtocolRefusal("fleet_update_target_invalid", "target has no waiter runtime files")
    for path in selected:
        if path.is_symlink() or not path.is_file() or path.resolve(strict=True) != path:
            raise ProtocolRefusal("fleet_update_target_invalid", f"target runtime path is not an ordinary file: {path.relative_to(source)}")
    return selected


def waiter_runtime_digest(root: Path) -> str:
    """Hash relative UTF-8 paths plus raw SHA-256 digests, matching installer framing."""

    source = Path(root)
    digest = hashlib.sha256()
    for path in waiter_runtime_files(source):
        digest.update(path.relative_to(source).as_posix().encode("utf-8") + b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()

"""One exact, shared inventory identity for the shipped Codex waiter runtime."""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

from .errors import ProtocolRefusal


def waiter_source_provenance(root: Path) -> dict[str, object]:
    """Carry installed testimony or a measured clean source commit, never paths."""
    from .installed_reader import installed_reader_identity
    from .git_process import fixed_git_command, fixed_git_environment

    source = Path(root)
    identity = installed_reader_identity(source)
    sha = identity.get('source_sha') if identity is not None else None
    if identity is None:
        try:
            def git(*arguments: str):
                return subprocess.run(
                    fixed_git_command('/usr/bin/git', source, arguments),
                    env=fixed_git_environment('/usr/bin/git'),
                    capture_output=True, text=True, timeout=5, check=False,
                )
            top = git('rev-parse', '--show-toplevel')
            if top.returncode == 0 and Path(top.stdout.strip()) == source:
                head = git('rev-parse', '--verify', 'HEAD^{commit}')
                paths = [path.relative_to(source).as_posix() for path in waiter_runtime_files(source)]
                status = git('status', '--porcelain=v1', '--untracked-files=all', '--', *paths)
                if head.returncode == 0 and status.returncode == 0 and not status.stdout:
                    import re
                    candidate = head.stdout.strip()
                    if re.fullmatch(r'[0-9a-f]{40}', candidate):
                        sha = candidate
        except (OSError, subprocess.TimeoutExpired, UnicodeDecodeError):
            pass
    return {'schema_version': 0, 'source_sha': sha,
            'source_state': 'measured' if sha is not None else 'absent'}


def write_waiter_provenance(source: Path, staging: Path) -> None:
    """Publish provenance only inside the still-private waiter staging directory."""
    from .installed_reader import PROVENANCE_NAME

    (staging / PROVENANCE_NAME).write_text(
        json.dumps(waiter_source_provenance(source), sort_keys=True) + '\n',
        encoding='utf-8',
    )


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

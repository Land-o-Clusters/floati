"""Fixed local Git subprocess coordinates shared by production observers."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Sequence

from .errors import ProtocolRefusal


_FIXED_GIT_ENVIRONMENT = {
    "GIT_ASKPASS": "/usr/bin/false",
    "GIT_ATTR_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_SYSTEM": "/dev/null",
    "GIT_NO_REPLACE_OBJECTS": "1",
    "GIT_OPTIONAL_LOCKS": "0",
    "GIT_PAGER": "cat",
    "GIT_TERMINAL_PROMPT": "0",
    "HOME": "/var/empty",
    "LANG": "C",
    "LC_ALL": "C",
    "PAGER": "cat",
    "SSH_ASKPASS": "/usr/bin/false",
    "XDG_CONFIG_HOME": "/var/empty",
}


def fixed_git_environment(executable: str) -> dict[str, str]:
    """Return a fixed environment without inheriting ambient Git coordinates."""

    executable_path = Path(executable)
    search_directories = ["/usr/bin", "/bin"]
    if executable_path.is_absolute():
        search_directories.insert(0, str(executable_path.parent))
    environment = dict(_FIXED_GIT_ENVIRONMENT)
    environment["PATH"] = os.pathsep.join(dict.fromkeys(search_directories))
    return environment


def fixed_git_command(
    executable: str, repository: Path, arguments: Sequence[str]
) -> list[str]:
    """Bind one explicit Git executable to one explicit repository path."""

    return [
        executable,
        "--no-optional-locks",
        "--no-replace-objects",
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        "core.fsmonitor=false",
        "-C",
        os.fspath(repository),
        *arguments,
    ]


def is_shallow_repository(
    repository: Path, *, git_executable: str = "/usr/bin/git"
) -> bool:
    """Return whether one explicit repository has truncated commit history."""

    repo = Path(repository).expanduser().resolve()
    try:
        result = subprocess.run(
            fixed_git_command(git_executable, repo, ("rev-parse", "--is-shallow-repository")),
            env=fixed_git_environment(git_executable),
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ProtocolRefusal("git_shallow_state_unavailable", f"git could not inspect shallow state: {exc}") from exc
    if result.returncode != 0:
        raise ProtocolRefusal(
            "git_shallow_state_unavailable",
            result.stderr.strip() or "git shallow-state inspection failed",
        )
    value = result.stdout.strip().lower()
    if value not in {"true", "false"}:
        raise ProtocolRefusal("git_shallow_state_unavailable", "git returned an invalid shallow-state value")
    return value == "true"


def require_complete_history_for_reachability(
    repository: Path, *, git_executable: str = "/usr/bin/git"
) -> None:
    """Refuse reachability arithmetic when commit history is truncated."""

    if is_shallow_repository(repository, git_executable=git_executable):
        raise ProtocolRefusal(
            "git_reachability_shallow_repository",
            "reachability cannot be determined from a shallow repository",
        )

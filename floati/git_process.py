"""Fixed local Git subprocess coordinates shared by production observers."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Sequence


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

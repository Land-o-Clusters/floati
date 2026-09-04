"""Resolve tools for host tests without weakening managed gateway declarations."""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Optional

from floati.fleet_update import _explicit_executable


def executable(declaration_variable: str, fallback_name: str) -> Optional[Path]:
    """Return a managed declaration, or retain PATH fallback for host-only runs.

    The presence of ``declaration_variable`` selects the managed contract. An
    invalid declaration therefore refuses and never degrades into discovery.
    """

    if declaration_variable in os.environ:
        return Path(
            _explicit_executable(
                os.environ[declaration_variable], "managed_test_tool_invalid"
            )
        )
    selected = shutil.which(fallback_name)
    return None if selected is None else Path(selected).resolve(strict=True)

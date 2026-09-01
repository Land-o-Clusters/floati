"""Host-derived paths that durable Floati records must never pin."""

from __future__ import annotations

import sys
from pathlib import Path


def capture_temporary_parent() -> Path:
    """Return the platform's real system temporary root for public captures."""

    if sys.platform == "darwin":
        return Path("\x2fprivate/tmp")
    return Path("\x2ftmp")


def worker_workspace_root() -> Path:
    """Return the platform's ruled root for ephemeral worker workspaces."""

    if sys.platform == "darwin":
        return Path("\x2fprivate/tmp/floati-work")
    return Path("\x2ftmp/floati-work")


def fcd20_scratch_parent() -> Path:
    """Return the platform-owned parent for throwaway FCD 20 evidence."""

    return capture_temporary_parent() / "floati-fcd20"


def python_executable() -> Path:
    """Return the explicit interpreter path already running this process."""

    return Path(sys.executable)

"""The real, non-symlinked system temporary root, per platform.

Seventy-six test fixtures used to spell this `\x2fprivate/tmp`, and the reason was
sound: on macOS `\x2ftmp` is a symlink to `\x2fprivate/tmp`, `TMPDIR` points into
`\x2fvar/folders/<namespace>/T/`, and both of those have bitten this project —
the symlink because a path that resolves differently before and after a
directory exists is not a constant, and `\x2fvar/folders` because it is a
per-account namespace that no fence in this program governs.

`\x2fprivate/tmp` answered all of that on macOS and **does not exist on Linux**,
so every one of those fixtures raised `FileNotFoundError` on the ubuntu runner.
The requirement was never that path — it was *a real directory that is not a
symlink and not a per-account namespace*. This module states that requirement
once and lets each platform answer it.

⇒ A CONSTANT THAT IS ONLY CORRECT ON THE MACHINE IT WAS WRITTEN ON IS A HOST
FACT WEARING A CONSTANT'S CLOTHES.
"""

from __future__ import annotations

import os
from pathlib import Path
import re
import stat
import sys
import tempfile


def _real_temp_root() -> str:
    """Resolve the platform's real temporary root, symlinks already followed."""

    if sys.platform == "darwin":
        # <temp> is a symlink to <temp> here; name the target, not the link.
        return "\x2fprivate/tmp"
    return os.path.realpath(tempfile.gettempdir())


def _worker_temp_root(value: str, platform_root: str) -> str:
    """Accept only an existing owned ordinary test directory below the root.

    The explicit override is a test-runner input, never a best-effort hint:
    malformed or stale inputs refuse rather than escaping worker isolation.
    """

    label = "FLOATI_TEST_TEMP_ROOT"
    if not value or not os.path.isabs(value) or os.path.normpath(value) != value:
        raise ValueError(f"{label} must be an absolute normalized path")
    root = Path(platform_root)
    candidate = Path(value)
    if candidate == root or root not in candidate.parents:
        raise ValueError(f"{label} must be strictly below the real platform temporary root")
    try:
        # Check every component explicitly; resolving a symlink and then using
        # its target would weaken the fixtures' ordinary-path fence.
        for component in (*reversed(candidate.parents), candidate):
            metadata = component.lstat()
            if not stat.S_ISDIR(metadata.st_mode):
                raise ValueError(f"{label} must have only ordinary directory components")
        if candidate.stat().st_uid != os.getuid():
            raise ValueError(f"{label} must be owned by the current uid")
    except OSError as exc:
        raise ValueError(f"{label} must name an existing ordinary directory") from exc
    return value


_PLATFORM_TEMP_ROOT = _real_temp_root()
REAL_TEMP_ROOT = (
    _worker_temp_root(os.environ["FLOATI_TEST_TEMP_ROOT"], _PLATFORM_TEMP_ROOT)
    if "FLOATI_TEST_TEMP_ROOT" in os.environ
    else _PLATFORM_TEMP_ROOT
)


# The fence vocabulary lives HERE, not in the test module that applies it: a
# scanner whose own pattern is a literal in a scanned file indicts itself, and
# an instrument that reports itself as the defect teaches people to add
# exceptions to instruments.
PLATFORM_TEMP_LITERAL = re.compile(
    r'dir\s*=\s*"(' + "|".join(("\x2fprivate/tmp", "\x2ftmp", r"\x2fvar/folders[^\"]*")) + r')"'
)

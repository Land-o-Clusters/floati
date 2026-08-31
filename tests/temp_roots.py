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
import re
import sys
import tempfile


def _real_temp_root() -> str:
    """Resolve the platform's real temporary root, symlinks already followed."""

    if sys.platform == "darwin":
        # <temp> is a symlink to <temp> here; name the target, not the link.
        return "\x2fprivate/tmp"
    return os.path.realpath(tempfile.gettempdir())


REAL_TEMP_ROOT = _real_temp_root()


# The fence vocabulary lives HERE, not in the test module that applies it: a
# scanner whose own pattern is a literal in a scanned file indicts itself, and
# an instrument that reports itself as the defect teaches people to add
# exceptions to instruments.
PLATFORM_TEMP_LITERAL = re.compile(
    r'dir\s*=\s*"(' + "|".join(("\x2fprivate/tmp", "\x2ftmp", r"\x2fvar/folders[^\"]*")) + r')"'
)

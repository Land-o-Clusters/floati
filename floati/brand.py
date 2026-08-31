"""Owner-ratified Floati identity marks for interactive milestones."""

from __future__ import annotations

from .copy import register


BUOY_ORANGE = "\x1b[38;5;208m"
HARBOR_SLATE = "\x1b[38;5;245m"
RESET = "\x1b[0m"

BUOY_MARK = register(
    "brand.buoy_mark",
    "      ⊙\n"
    "      │\n"
    "     ╱ ╲\n"
    "    ╱───╲\n"
    "   ╱     ╲\n"
    " ~~~~~~~~~~~",
    "TTY milestone mark",
)


def render_buoy_mark(*, color: bool) -> str:
    """Render the exact mark; callers own the ruled interactive-only boundary."""

    if not color:
        return BUOY_MARK
    lines = BUOY_MARK.splitlines()
    return "\n".join(
        (
            BUOY_ORANGE + lines[0] + RESET,
            BUOY_ORANGE + lines[1] + RESET,
            BUOY_ORANGE + lines[2] + RESET,
            BUOY_ORANGE
            + "    ╱"
            + RESET
            + HARBOR_SLATE
            + "───"
            + RESET
            + BUOY_ORANGE + "╲" + RESET,
            BUOY_ORANGE + lines[4] + RESET,
            HARBOR_SLATE + lines[5] + RESET,
        )
    )

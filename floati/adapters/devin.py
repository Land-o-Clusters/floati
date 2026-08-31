"""Devin worker adapter — AD-1 work-column for the Devin CLI surface.

RE-DERIVED from floati/adapters/headless_template.py. Declares ONLY identity
and profile. Every wiring discipline is inherited.

SURFACE STATUS: UNVERIFIED pending live spawn intake (charter override 5).
headless_arguments is HONESTLY EMPTY — live `--help` names `-p/--print`, but
the spawn argv spelling is not claimed until a live turn cites it.
Live C11 stamp: /opt/homebrew/bin/devin (Homebrew cask). A second copy may
exist under ~/.local/bin; cells must name which binary.
"""

from __future__ import annotations

from .headless_template import HarnessProfile, HeadlessProfileAdapter


_DEFAULT_COMMAND = ("/opt/homebrew/bin/devin",)
_PROFILE = HarnessProfile(
    name="devin",
    command=_DEFAULT_COMMAND,
    headless_arguments=(),  # HONESTLY EMPTY until live intake cites spellings
    stderr_name="devin.stderr",
)


class DevinAdapter(HeadlessProfileAdapter):
    """Devin CLI worker with explicit workspace and fail-closed tools."""

    name = "devin"

    def __init__(self, source=None, *, isolate_process_group: bool = True) -> None:
        if isinstance(source, HarnessProfile):
            profile = source
        elif source is not None:
            profile = HarnessProfile(
                name=_PROFILE.name,
                command=tuple(source),
                headless_arguments=_PROFILE.headless_arguments,
                stderr_name=_PROFILE.stderr_name,
            )
        else:
            profile = _PROFILE
        super().__init__(profile, isolate_process_group=isolate_process_group)

    @classmethod
    def availability(cls, command=None):
        cmd = tuple(command) if command else _PROFILE.command
        return super().availability(cmd, profile=_PROFILE)

    @classmethod
    def _default_profile(cls) -> HarnessProfile:
        return _PROFILE

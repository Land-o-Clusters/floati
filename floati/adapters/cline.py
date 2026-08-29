"""Cline worker adapter — roster 4 of the full-parity roster.

RE-DERIVED from floati/adapters/headless_template.py (the repaired
template) per the architect's ruling: this module declares ONLY its
identity and profile. Every wiring discipline is inherited.

SURFACE STATUS: UNVERIFIED pending live invocation intake (charter override
5). The declared entry point is sourced from the vendor CLI documentation;
surface_verified remains false until the matching binary is exercised.
"""

from __future__ import annotations

from .headless_template import HarnessProfile, HeadlessProfileAdapter


_DEFAULT_COMMAND = ('/opt/homebrew/bin/cline',)
_PROFILE = HarnessProfile(
    name="cline",
    command=_DEFAULT_COMMAND,
    # Citation: https://docs.cline.bot/usage/cli-overview — `cline --json "task"`.
    headless_arguments=("--json",),
    stderr_name="cline.stderr",
    cited_source="https://docs.cline.bot/usage/cli-overview",
)


class ClineAdapter(HeadlessProfileAdapter):
    """Cline worker with explicit workspace and fail-closed tools."""

    name = "cline"

    def __init__(self, source=None, *,
                 isolate_process_group: bool = True) -> None:
        """Accept a HarnessProfile or an absolute command sequence."""
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
        super().__init__(profile,
                         isolate_process_group=isolate_process_group)

    @classmethod
    def availability(cls, command=None):
        cmd = tuple(command) if command else _PROFILE.command
        return super().availability(cmd, profile=_PROFILE)

    @classmethod
    def _default_profile(cls) -> HarnessProfile:
        return _PROFILE

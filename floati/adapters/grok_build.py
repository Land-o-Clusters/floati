"""`grok-build` worker adapter — roster 1 of the full-parity roster.

RE-DERIVED from floati/adapters/headless_template.py (the repaired
template) per the architect's ruling: this module declares ONLY its
identity and profile. Every wiring discipline is inherited.

SURFACE STATUS: UNVERIFIED pending live invocation intake (charter override
5). The declared entry point is sourced from the vendor CLI documentation;
surface_verified remains false until the matching binary is exercised.
"""

from __future__ import annotations

from pathlib import Path

from .headless_template import HarnessProfile, HeadlessProfileAdapter


# Live vendor binary is `grok`. Fixed prefixes; never PATH, never grok-build.
_GROK_CANDIDATES = (
    "/opt/homebrew/bin/grok",
    "/usr/local/bin/grok",
)
_HEADLESS_ARGUMENTS = ("-p",)
_STDERR_NAME = "grok-build.stderr"
_CITED_SOURCE = "https://docs.x.ai/build/cli/headless-scripting"


def _select_default_command() -> tuple[str, ...]:
    for candidate in _GROK_CANDIDATES:
        if Path(candidate).is_file():
            return (candidate,)
    return (_GROK_CANDIDATES[0],)


def _profile(command: tuple[str, ...] | None = None) -> HarnessProfile:
    return HarnessProfile(
        name="grok-build",
        command=command if command is not None else _select_default_command(),
        # Citation: https://docs.x.ai/build/cli/headless-scripting — vendor `-p` flag.
        headless_arguments=_HEADLESS_ARGUMENTS,
        stderr_name=_STDERR_NAME,
        cited_source=_CITED_SOURCE,
    )


class GrokBuildAdapter(HeadlessProfileAdapter):
    """`grok-build` worker with explicit workspace and fail-closed tools."""

    name = "grok-build"

    def __init__(self, source=None, *,
                 isolate_process_group: bool = True) -> None:
        """Accept a HarnessProfile or an absolute command sequence."""
        if isinstance(source, HarnessProfile):
            profile = source
        elif source is not None:
            profile = _profile(tuple(source))
        else:
            profile = _profile()
        super().__init__(profile,
                         isolate_process_group=isolate_process_group)

    @classmethod
    def availability(cls, command=None):
        cmd = tuple(command) if command else _select_default_command()
        return super().availability(cmd, profile=_profile(cmd))

    @classmethod
    def _default_profile(cls) -> HarnessProfile:
        return _profile()

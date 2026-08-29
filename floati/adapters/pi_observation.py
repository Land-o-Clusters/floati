"""Pi Observation worker adapter — roster 5 of the full-parity roster.

RE-DERIVED from floati/adapters/headless_template.py (the repaired
template) per the architect's ruling: this module declares ONLY its
identity and profile. Every wiring discipline is inherited.

SURFACE STATUS: UNVERIFIED pending live intake (charter override 5).
headless_arguments is HONESTLY EMPTY — no argument spelling is claimed
until cited from live verification.
"""

from __future__ import annotations

from .headless_template import HarnessProfile, HeadlessProfileAdapter


_DEFAULT_COMMAND = ('/opt/homebrew/bin/pi-observation',)
_PROFILE = HarnessProfile(
    name="pi-observation",
    command=_DEFAULT_COMMAND,
    headless_arguments=(),  # HONESTLY EMPTY until live intake cites spellings
    stderr_name="pi-observation.stderr",
)


class PiObservationAdapter(HeadlessProfileAdapter):
    """Pi Observation worker with explicit workspace and fail-closed tools."""

    name = "pi-observation"

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

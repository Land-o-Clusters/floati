"""Antigravity worker adapter — AD-1 work-column for the Antigravity CLI.

RE-DERIVED from floati/adapters/headless_template.py. Declares ONLY identity
and profile. Every wiring discipline is inherited.

CLI product name is ``agy`` (PATH name ``antigravity`` ABSENT). Two copies
can exist: Homebrew cask ``/opt/homebrew/bin/agy`` (1.1.5) and a user-local
``~/.local/bin/agy`` (1.1.22). This adapter binds the **user-local** copy
via ``Path.home() / ".local" / "bin" / "agy"`` — not the cask, and not a
machine-literal home path.

surface_verified is True from the reviewer gate measurement at
``864eff86f66c133d361085d7ba0fa2a2936bd92f``: the user-local binary answered
``agy --version`` as ``1.1.22`` in 48 ms. headless_arguments stay honestly
empty until a live spawn cites argv spellings.
"""

from __future__ import annotations

from pathlib import Path

from .headless_template import HarnessProfile, HeadlessProfileAdapter


def _bound_agy_command() -> tuple[str, ...]:
    """User-local agy (1.1.22), never the Homebrew cask (1.1.5)."""
    return (str(Path.home() / ".local" / "bin" / "agy"),)


def _profile() -> HarnessProfile:
    return HarnessProfile(
        name="antigravity",
        command=_bound_agy_command(),
        headless_arguments=(),  # HONESTLY EMPTY until live intake cites spellings
        stderr_name="antigravity.stderr",
    )


class AntigravityAdapter(HeadlessProfileAdapter):
    """Antigravity CLI worker with explicit workspace and fail-closed tools."""

    name = "antigravity"
    surface_verified = True

    def __init__(self, source=None, *, isolate_process_group: bool = True) -> None:
        if isinstance(source, HarnessProfile):
            profile = source
        elif source is not None:
            profile = HarnessProfile(
                name="antigravity",
                command=tuple(source),
                headless_arguments=(),
                stderr_name="antigravity.stderr",
            )
        else:
            profile = _profile()
        super().__init__(profile, isolate_process_group=isolate_process_group)

    @classmethod
    def availability(cls, command=None):
        cmd = tuple(command) if command else _bound_agy_command()
        return super().availability(cmd, profile=_profile())

    @classmethod
    def _default_profile(cls) -> HarnessProfile:
        return _profile()

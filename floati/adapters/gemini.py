"""Gemini CLI headless profile awaiting live conformance and wake evidence."""

from __future__ import annotations

from dataclasses import replace

from .headless_template import HarnessProfile, HeadlessProfileAdapter


_PROFILE = HarnessProfile(
    name="gemini",
    command=("/opt/homebrew/bin/gemini",),
    headless_arguments=("--output-format", "json"),
    stderr_name="gemini.stderr",
    cited_source="https://geminicli.com/docs/cli/headless/",
    prompt_form=("--prompt",),
)


class GeminiAdapter(HeadlessProfileAdapter):
    name = "gemini"

    def __init__(self, source=None, *, isolate_process_group: bool = True) -> None:
        profile = _PROFILE
        if isinstance(source, HarnessProfile):
            profile = source
        elif source is not None:
            profile = replace(profile, command=tuple(source))
        super().__init__(profile, isolate_process_group=isolate_process_group)

    @classmethod
    def _default_profile(cls) -> HarnessProfile:
        return _PROFILE

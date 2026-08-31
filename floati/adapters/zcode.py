"""Zcode worker adapter — K1 of the zcode kit (ZC-1), roster member.

RE-DERIVED from floati/adapters/headless_template.py (the repaired
template) per the architect's ruling: this module declares ONLY its
identity and profile. Every wiring discipline is inherited.

SURFACE STATUS: VERIFIED BY MEASUREMENT — the strongest citation class
in the roster. The argv below was exercised live on this machine
(`WAKE-PROBE-OK`, rc=0, glm-4.7-flash), not read off --help, which on
this harness is CLAIMED at best: finding ZC1-F1 measured 5 of 19
advertised options refusing at the parser
(docs/evidence/gauntlet/ZC1-zcode-scoping-photograph-am2.md).

REFUSED AT THE PARSER DESPITE --help ADVERTISING THEM — never declare:
--allowed-tools · --max-turns · --settings · --permission-mode ·
--allow-main-worktree-yolo.

CONFINEMENT CONSTRAINT (a design input, not a caveat): --disallowed-tools
parses and --allowed-tools does not, so a zcode seat can be denylisted
and NEVER confined to an allowlist. Denylists fail open, including over
tools a future version adds. Confine at the workspace/OS layer and treat
the harness as unconfined.

FAILURE IS NOT MACHINE-READABLE: --json yields a typed artifact on
SUCCESS (sessionId, traceId, turnId, response, provider-sourced usage)
and 0 bytes of stdout plus a raw Node stack on FAILURE. The inherited
_validate_result keys on return_code and stderr — keep it that way;
never parse stdout to decide failure.
"""

from __future__ import annotations

from .headless_template import HarnessProfile, HeadlessProfileAdapter


_DEFAULT_COMMAND = (
    '/opt/homebrew/bin/node',
    '/Applications/ZCode.app/Contents/Resources/glm/zcode.cjs',
)
_PROFILE = HarnessProfile(
    name="zcode",
    command=_DEFAULT_COMMAND,
    headless_arguments=("--json", "--no-color"),
    stderr_name="zcode.stderr",
    cited_source=(
        "docs/evidence/gauntlet/ZC1-zcode-scoping-photograph-am2.md"
    ),
    # K4 live finding: the template default `-- <title>` is REFUSED by
    # zcode (Unknown command + help dump; capture attempt1-stderr-helpdump
    # .txt in the K4 captures). The measured prompt spelling is
    # `--prompt <text>` — exercised live in am1's parse sweep and am2's
    # WAKE-PROBE-OK turn, and re-exercised by the K4 receipt turn.
    prompt_form=("--prompt",),
)


class ZcodeAdapter(HeadlessProfileAdapter):
    """Zcode CLI worker with explicit workspace and fail-closed tools."""

    name = "zcode"
    surface_verified = True

    def __init__(self, source=None, *,
                 isolate_process_group: bool = True) -> None:
        """Accept a HarnessProfile or an absolute command sequence."""
        if isinstance(source, HarnessProfile):
            profile = source
        elif source is not None:
            # A custom command keeps the measured argv, prompt form, AND
            # citation: the spellings belong to the harness, not to one
            # binary path.
            profile = HarnessProfile(
                name=_PROFILE.name,
                command=tuple(source),
                headless_arguments=_PROFILE.headless_arguments,
                stderr_name=_PROFILE.stderr_name,
                cited_source=_PROFILE.cited_source,
                prompt_form=_PROFILE.prompt_form,
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

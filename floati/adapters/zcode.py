"""Zcode worker adapter — K1 of the zcode kit (ZC-1), roster member.

RE-DERIVED from floati/adapters/headless_template.py (the repaired
template) per the architect's ruling: this module declares its identity,
profile and packaged provider environment. Process wiring is inherited.

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

import json
import os
from pathlib import Path
import plistlib
import re
import subprocess

from .headless_template import HarnessProfile, HeadlessProfileAdapter
from ..errors import ProtocolRefusal


def _packaged_resources(entry: Path):
    resources = entry.parent.parent
    if (entry.name != "zcode.cjs" or entry.parent.name != "glm"
            or resources.name != "Resources" or resources.parent.name != "Contents"
            or resources.parent.parent.suffix != ".app"):
        return None
    return resources


def resolve_zcode_command(command=None):
    """Resolve only the declared Node/entry pair when it is used, never at import.

    In particular the Homebrew Node link may point at a new version after an
    upgrade. No filesystem search or previously observed digest selects it.
    Other explicit fixture/custom argv shapes retain their existing contract.
    """
    declared = tuple(command) if command is not None else _PROFILE.command
    if len(declared) == 2 and all(Path(part).is_absolute() for part in declared):
        return tuple(str(Path(part).resolve()) for part in declared)
    return declared


def zcode_entry_testimony(command=None):
    """Read the current declared bundle's version as testimony, never a gate."""
    declared = tuple(command) if command is not None else _PROFILE.command
    resolved = resolve_zcode_command(declared)
    resources = _packaged_resources(Path(resolved[1])) if len(resolved) == 2 else None
    version = None
    config = None
    if resources is not None:
        config = str(resources / "config/provider/zcode-builtin.json")
        try:
            metadata = plistlib.loads((resources.parent / "Info.plist").read_bytes())
            observed = metadata.get("CFBundleShortVersionString")
            if isinstance(observed, str) and observed.strip():
                version = observed
        except (OSError, ValueError, TypeError, AttributeError, plistlib.InvalidFileException):
            pass
    return {"declared_command": list(declared), "resolved_command": list(resolved),
            "version": version, "builtin_provider_config": config}


def _entrypoint_refusal(command, missing_piece):
    native = zcode_entry_testimony(command)
    version = native["version"] or "version unavailable"
    return ProtocolRefusal(
        "zcode_entrypoint_unavailable",
        f"ZCode {version} cannot start the declared entry "
        f"{native['resolved_command'][-1]}: missing {missing_piece}",
        remedy=("supply the complete terminal runtime documented for this ZCode release; "
                "use its explicit entry point; the desktop Agent bundle alone "
                "does not supply the interactive TUI"),
    )


def zcode_startup_refusal(entry, stderr):
    """Translate Node's actual missing-dependency diagnostic, never a stack mention."""
    if isinstance(stderr, str):
        missing = re.search(r"Cannot find (?:package|module) ['\"]([^'\"\r\n]{1,256})['\"]", stderr)
        if missing is not None:
            return _entrypoint_refusal((_DEFAULT_COMMAND[0], str(entry)), missing.group(1))
    return None


# Resolve with ESM import conditions and the entry's own parent URL. This
# checks metadata only: it never imports the TUI or starts a provider/session.
_TUI_RESOLVER = """import { existsSync } from 'node:fs';
import { pathToFileURL, fileURLToPath } from 'node:url';
try {
  const url = import.meta.resolve('@zcode/tui', pathToFileURL(process.argv[1]).href);
  console.log(JSON.stringify({code: existsSync(fileURLToPath(url)) ? null : 'ERR_MODULE_NOT_FOUND'}));
} catch (error) {
  console.log(JSON.stringify({code: error.code || 'resolution_failed'}));
}
"""


def require_zcode_terminal_runtime(command=None):
    """Refuse a desktop Agent script whose external interactive runtime is absent.

    v3.17.1's build externalizes @zcode/tui; its documented standalone
    distribution supplies it. Headless --prompt does not load that package.
    Custom commands and older entries keep their existing launch contract.
    """
    command = resolve_zcode_command(_DEFAULT_COMMAND if command is None else command)
    if len(command) != 2 or _packaged_resources(Path(command[1])) is None:
        return
    entry = Path(command[1])
    native = zcode_entry_testimony(command)
    detail = "entry unreadable"
    try:
        source = entry.read_text(encoding="utf-8")
        if re.search(r"import\s*\(\s*(['\"])@zcode/tui\1\s*\)", source) is None:
            return
        detail = "TUI dependency resolution unavailable"
        # Do not inherit Node preloads, custom module paths or loader controls.
        environment = {key: os.environ[key] for key in ("HOME", "LANG") if key in os.environ}
        environment["PATH"] = os.defpath
        result = subprocess.run(
            [command[0], "--no-warnings", "--experimental-import-meta-resolve",
             "--input-type=module", "--eval", _TUI_RESOLVER, command[1]],
            cwd=entry.parent, env=environment, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=15,
        )
        if result.returncode == 0:
            observation = json.loads(result.stdout)
            if isinstance(observation, dict) and observation.get("code") == "ERR_MODULE_NOT_FOUND":
                raise _entrypoint_refusal(command, "@zcode/tui")
            if isinstance(observation, dict) and "code" in observation and observation["code"] is None:
                return
    except (OSError, ValueError, subprocess.TimeoutExpired):
        pass
    raise ProtocolRefusal(
        "zcode_entrypoint_unverified",
        f"ZCode {native['version'] or 'version unavailable'}: {detail} for {entry}",
        remedy="verify the declared Node interpreter and the complete terminal runtime for this ZCode release",
    )


def zcode_launch_environment(entry: Path):
    """Use ZCode's provider env seam for its packaged glm entry.

    ZCode 3.16.3: zcode.cjs:104-106 reads this override; app.asar's
    out/main/index.js:1095 resolves this packaged resource. See the source
    identities and app spawn citations in the F2-SEAT-BIND-1 report.
    Keep HOME, personal config and data-base coordinates inherited: setting
    only the builtin override retains the CLI's normal cache initialization.
    """
    resources = _packaged_resources(entry.resolve())
    if resources is None:
        return None
    config = resources / "config/provider/zcode-builtin.json"
    environment = os.environ.copy()
    # A packaged entry's provider data belongs to that same resource tree,
    # never to an unrelated inherited builtin override. If absent, leave the
    # CLI to emit its measured missing-provider diagnostic.
    environment.pop("ZCODE_BUILTIN_PROVIDER_CONFIG_FILE", None)
    if config.is_file() and config.resolve() == config:
        environment["ZCODE_BUILTIN_PROVIDER_CONFIG_FILE"] = str(config)
    return environment


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
        self.declared_command = tuple(profile.command)

    def spawn(self, item, *, deadline_seconds):
        self.command = resolve_zcode_command(self.declared_command)
        return super().spawn(item, deadline_seconds=deadline_seconds)

    def _launch_environment(self):
        if len(self.command) == 2:
            return zcode_launch_environment(Path(self.command[1]))
        return None

    @classmethod
    def availability(cls, command=None):
        cmd = tuple(command) if command else _PROFILE.command
        return super().availability(cmd, profile=_PROFILE)

    @classmethod
    def _default_profile(cls) -> HarnessProfile:
        return _PROFILE

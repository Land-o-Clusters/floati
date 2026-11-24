"""LC-GEN: project lifecycle commands from the LC-1 taxonomy.

Ruling ``docs/rulings/2026-09-05-lc-1-lifecycle-taxonomy-ruling.md`` §LC-1d:
one generator verb (``node prompts``) projects, from the role template, the
harness adapter table and the LC-1 parity table, the per-seat command files.
Every projection opens with a header naming the generator, the harbor SHA
and "hand edits are overwritten"; regeneration is byte-stable; the LC-R3
reserved-name fence refuses product verbs and other bus families' prompt
names for every file name the adapter's pattern yields (LC-GEN-R1 a),
and a file already under ``--out`` that does not carry the generator
header is refused by path — only generator-headed files are overwritten
(LC-GEN-R1 b); and the generator writes ONLY under the caller's
``--out``. The separate governed installer helper installs only the
identity-free seat command pair in a harness's fixed workspace directory.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import shlex
import subprocess
from pathlib import Path

from .admin_registry import RegistryAdminBackend
from .errors import ProtocolRefusal
from .git_process import GIT_MAINTENANCE_OPTIONS
from .role_library import RoleTemplateLibrary
from .role_templates import RoleTemplate
from .root import FloatiRoot, validate_identifier

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]

#: The LC-1a closed verb set.
LIFECYCLE_VERBS: tuple[str, ...] = (
    "boot", "board", "drain", "pause", "resume", "prep-clear", "turnover",
)

#: Other bus families' prompt names (LC-R3); the product's own verbs are
#: DERIVED from describe's live registry rather than hand-typed.
BUS_FAMILY_NAMES: frozenset[str] = frozenset({"pausebus", "resumebus"})


def reserved_names() -> frozenset[str]:
    """Product verb names (from describe's live registry) + bus families."""

    from .cli import _parser
    from .command_contract import describe_parser

    names: set[str] = set(BUS_FAMILY_NAMES)
    for row in describe_parser(_parser())["commands"]:
        path = [str(part) for part in row["path"]]
        names.add(path[-1])
        if len(path) > 1:
            names.add("-".join(path))
    return frozenset(names)

#: The harness adapter table: where each harness keeps its seat prompts and
#: which lifecycle verbs are realized as files there (LC-1 §1, LC-1d). The
#: file-name stem is adapter data (`file_pattern`): a pattern that can
#: produce a bare reserved word is exactly the shape the reserved-name
#: fence inside ``project_prompts`` refuses (LC-GEN-R1 a).
HARNESS_ADAPTERS: dict[str, dict] = {
    "codex": {
        "prompts_dir": ".codex/prompts",
        "verbs": ("board", "drain", "pause", "resume", "prep-clear"),
        "file_pattern": "{verb}-{node}",
    },
    "claude": {
        "prompts_dir": ".claude/commands",
        "verbs": ("boot", "prep-clear", "board"),
        "file_pattern": "{verb}-{node}",
    },
    "zcode": {
        "prompts_dir": ".",  # BOOT.md references the shared command pair
        "verbs": ("prep-clear",),
        "file_pattern": "{verb}-{node}",
    },
    "cursor": {
        "prompts_dir": ".cursor/commands",
        "verbs": ("board", "drain"),
        "file_pattern": "{verb}-{node}",
    },
}

SEAT_COMMAND_HARNESSES = frozenset({"cursor", "codex", "zcode", "claude"})

#: Honest dashes with their receipts (LC-1b): a cell is a dash, never a
#: gap, when the harness cannot realize the verb today.
DASH_RECEIPTS: dict[tuple[str, str], str] = {
    ("codex", "turnover"): (
        "dash: WIND-DOWN then BOARD; the receipts link by the wake claim's "
        "predecessor id (LC-1 §1 TURNOVER)"
    ),
    ("claude", "drain"): (
        "dash: the product inbox/ack verbs run by hand; no file wrapper (LC-1 §1 DRAIN)"
    ),
    ("claude", "pause"): (
        "dash: no daemon binds Claude seats today (LC-1 §1 PAUSE/RESUME)"
    ),
    ("claude", "resume"): (
        "dash: no daemon binds Claude seats today (LC-1 §1 PAUSE/RESUME)"
    ),
    ("claude", "turnover"): (
        "dash: WIND-DOWN then BOARD; the receipts link by the wake claim's "
        "predecessor id (LC-1 §1 TURNOVER)"
    ),
    ("zcode", "board"): (
        "dash: the Stop-hook bridge injects at a turn end; seat board by hand "
        "(LC-1 §1 BOARD)"
    ),
    ("zcode", "drain"): "dash: the bridge drains at turn end (LC-1 §1 DRAIN)",
    ("zcode", "pause"): (
        "dash: wake pause/resume verbs by hand; no file wrapper (LC-1 §1 PAUSE/RESUME)"
    ),
    ("zcode", "resume"): (
        "dash: wake pause/resume verbs by hand; no file wrapper (LC-1 §1 PAUSE/RESUME)"
    ),
    ("zcode", "turnover"): (
        "dash: WIND-DOWN then BOARD; the receipts link by the wake claim's "
        "predecessor id (LC-1 §1 TURNOVER)"
    ),
    ("cursor", "pause"): (
        "dash: wake pause/resume verbs by hand; no file wrapper (LC-1 §1 PAUSE/RESUME)"
    ),
    ("cursor", "resume"): (
        "dash: wake pause/resume verbs by hand; no file wrapper (LC-1 §1 PAUSE/RESUME)"
    ),
    ("cursor", "prep-clear"): (
        "dash: the product node prep-clear verb runs by hand; no file wrapper "
        "(LC-1 §1 PREP-CLEAR)"
    ),
    ("cursor", "turnover"): (
        "dash: WIND-DOWN then BOARD; the receipts link by the wake claim's "
        "predecessor id (LC-1 §1 TURNOVER)"
    ),
}

_GIT_CANDIDATES = ("/usr/bin/git", "/bin/git")

#: Every file this generator writes opens with this marker; a file under
#: ``--out`` without it was not written here and is never overwritten.
GENERATED_MARKER = "<!-- generated by floati node prompts"

#: The LC-1 §1 product realization of each projected verb, as a command path
#: into the live parser. The rendered lines DERIVE from the parser's own
#: required options — never from prose.
VERB_COMMANDS: dict[str, tuple[tuple[str, ...], ...]] = {
    "boot": (("node", "spawn"),),
    "board": (("seat", "board"),),
    "drain": (("inbox",), ("ack",)),
    "pause": (("wake", "pause"),),
    "resume": (("wake", "resume"),),
    "prep-clear": (("node", "prep-clear"),),
    "teardown": (("node", "teardown"),),
}

_PROVIDED = ("--root", "--as")


def _subparser(path: tuple[str, ...]):
    from .cli import _parser

    parser = _parser()
    for name in path:
        for action in parser._actions:
            if isinstance(action, argparse._SubParsersAction):
                parser = action.choices[name]
                break
    return parser


def _command_line(path: tuple[str, ...], root: str, node: str) -> str:
    """Render one invocation from the parser's own required options."""

    parser = _subparser(path)
    parts = ["floati", *path, "--root", root, "--as", node]
    for action in parser._actions:
        if not action.required or not action.option_strings:
            continue
        option = action.option_strings[0]
        if option in _PROVIDED:
            continue
        placeholder = (action.metavar or action.dest).upper().replace("-", "_")
        parts.extend([option, placeholder])
    if path == ("seat", "board"):
        parts.append("--take-over")
    return " ".join(parts)


def _verb_lines(verb: str, root: str, node: str) -> list[str]:
    lines = []
    for path in VERB_COMMANDS[verb]:
        lines.append(f"- `{_command_line(path, root, node)}`")
    return lines


def harbor_sha() -> str:
    """The running tree's HEAD SHA, git by the fixed candidate list."""

    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    for candidate in _GIT_CANDIDATES:
        if not os.access(candidate, os.X_OK):
            continue
        result = subprocess.run(
            [candidate, *GIT_MAINTENANCE_OPTIONS, "rev-parse", "HEAD"],
            cwd=str(REPOSITORY_ROOT),
            env=env,
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    raise ProtocolRefusal(
        "lifecycle_prompts_sha_absent",
        "the harbor SHA cannot be read: none of the fixed git candidates answered",
    )


def assert_no_collision(name: str) -> None:
    """LC-R3: a projected name may not claim a reserved name.

    Called by ``project_prompts`` for EVERY file name it is about to
    write, so the fence fires on whatever shape an adapter's pattern
    produces — a bare reserved word included (LC-GEN-R1 a). The reserved
    set is derived from describe's live registry plus the bus families
    and pinned by count in the contract test, so the fence tracks the
    product as its verb vocabulary moves.
    """

    if name in reserved_names():
        raise ProtocolRefusal(
            "lifecycle_prompts_name_reserved",
            f"{name!r} is claimed by the product's own verbs or another bus "
            "family's prompt set",
            remedy="project the file under a seat-suffixed name instead",
        )


def _file_stem(adapter: dict, verb: str, node: str) -> str:
    """The file-name stem one verb projects to, by the adapter's pattern."""

    return adapter["file_pattern"].format(verb=verb, node=node)


def _header(source: str, sha: str) -> str:
    return (
        f"{GENERATED_MARKER} ({source}) at harbor {sha}\n"
        "     hand edits are overwritten -->"
    )


def _check_projection_target(target: Path) -> None:
    """LC-GEN-R1 (b): only generator-headed files under --out are ours.

    A file already under the output directory without the marker is
    someone else's command file and is refused by path; a file WITH the
    marker is this generator's and is overwritten, as the header says.
    """

    if target.is_symlink() or any(parent.is_symlink() for parent in target.parents):
        raise ProtocolRefusal(
            "lifecycle_prompts_target_invalid", f"{target} is symlinked",
            remedy="use ordinary command files inside the declared seat workspace",
        )
    if target.exists():
        if not target.is_file():
            raise ProtocolRefusal(
                "lifecycle_prompts_target_invalid", f"{target} is not a regular file",
                remedy="use an ordinary command file inside the declared seat workspace",
            )
        existing = target.read_text(encoding="utf-8", errors="replace")
        if target.name == "SKILL.md" and existing.startswith("---\n"):
            # Native skills require front matter before the ownership marker.
            _, separator, body = existing[4:].partition("\n---\n")
            if separator:
                existing = body.lstrip("\n")
        if not existing.startswith(GENERATED_MARKER):
            raise ProtocolRefusal(
                "lifecycle_prompts_target_foreign",
                f"{target} exists under the output directory and does not "
                "carry the generator header; it is not this generator's file",
                remedy="project into an empty directory, or move the foreign file",
            )


def _write_projection(target: Path, content: str) -> None:
    _check_projection_target(target)
    target.write_text(content, encoding="utf-8")


def _digest(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _seat_command_contents(
    harness: str, sha: str | None = None, *, runtime: Path | None = None,
) -> dict[str, str]:
    """The shared pair contains no fleet, node, role, or session identity."""

    def header(source: str) -> str:
        if sha is not None:
            return _header(source, sha)
        # Installed bundles need no Git checkout to project these commands.
        return f"{GENERATED_MARKER} ({source})\n     hand edits are overwritten -->"

    common = [
        "Run from the current seat workspace containing its governed `SEAT.json`.",
        "The product resolves the root and node from that declaration and the registry.",
        "An optional node argument is only an assertion; never choose another root or node.",
        "If this chat is rooted elsewhere, stop and present the product's rooting remedy; "
        "open the declared seat workspace before retrying.",
        "Read the harness's actual current session id from its native runtime evidence. "
        "Pass it explicitly as `--session SESSION`; never invent or omit a session id.",
        "Pass an optional asserted node as `--node NODE`; omit that option when absent.",
    ]
    if harness == 'claude':
        common.append('Command arguments supplied by the owner: $ARGUMENTS')
    elif harness == 'codex':
        common.append('Read command arguments from the current owner message.')
    result = {}
    for verb in ("boot", "teardown"):
        executable = str(runtime / 'scripts/floati') if runtime is not None else 'floati'
        invocation = shlex.join([executable, 'node', verb])
        extra = []
        if harness == 'claude' and verb == 'boot':
            invocation += ' --settings SETTINGS'
            extra = [
                'Use the explicit absolute Claude settings path supplied in the command '
                'arguments as SETTINGS. Never discover or default to account settings. '
                'If no path was supplied, present the settings-required refusal and ask '
                'for the explicit path before retrying.',
            ]
        if verb == "teardown":
            invocation += " --repo REPO --doc DOC"
            extra = [
                "Resolve REPO and the existing committed retention report DOC from the "
                "seat/session evidence or the command arguments; never invent or omit them.",
            ]
        result[f"{verb}-seat.md"] = "\n".join([
            header(f"{harness} shared {verb} prompt"), "",
            f"# {verb}-seat", "", *common, *extra, "",
            f"Run `{invocation}` with the options resolved above.",
            "Read the single JSON artifact and its receipts. Present a typed refusal "
            "and its remedy unchanged; report success only when the artifact proves it.",
            "",
        ])
    if harness == "zcode":
        result["BOOT.md"] = "\n".join([
            header("zcode shared lifecycle entrypoint"), "",
            "# Seat lifecycle", "",
            "To boot this seat, read `boot-seat.md` and perform its command.",
            "To end this session, read `teardown-seat.md` and perform its command.",
            "Both commands resolve identity from the current workspace's `SEAT.json`.",
            "",
        ])
    return result


def plan_seat_commands(
    workspace: Path, harness: str, *, codex_home: Path | None = None,
    runtime: Path | None = None,
) -> tuple[Path, dict[str, str]]:
    """Render and validate the shared commands without touching their targets."""

    workspace = Path(workspace)
    if harness not in SEAT_COMMAND_HARNESSES:
        raise ProtocolRefusal(
            "lifecycle_prompts_harness_unsupported",
            f"shared seat commands are not implemented for {harness!r}",
            remedy="use Cursor, Codex, zcode or Claude",
        )
    if not workspace.is_absolute() or workspace.is_symlink():
        raise ProtocolRefusal(
            "lifecycle_prompts_workspace_invalid",
            "workspace must be an existing absolute nonsymlink directory",
            remedy="run the governed hook installer for the declared seat workspace",
        )
    if codex_home is not None:
        codex_home = Path(codex_home)
        if harness != 'codex' or not codex_home.is_absolute() or codex_home.resolve() != codex_home:
            raise ProtocolRefusal('lifecycle_prompts_target_invalid',
                'native Codex commands require the explicit canonical Codex configuration home',
                remedy='use the same ordinary Codex home as the governed hooks file')
        # Codex 0.164 retains CODEX_HOME/skills; prompts are no longer loaded.
        out_dir = codex_home / 'skills'
    elif harness == 'codex':
        out_dir = workspace / '.agents/skills'
    else:
        out_dir = workspace / HARNESS_ADAPTERS[harness]["prompts_dir"]
    if harness == 'codex':
        runtime = Path(runtime) if runtime is not None else REPOSITORY_ROOT
        if not runtime.is_absolute() or runtime.resolve() != runtime or not runtime.is_dir():
            raise ProtocolRefusal('lifecycle_prompts_target_invalid',
                'native Codex commands require the canonical installed runtime directory',
                remedy='use the runtime recorded by the governed hook installer')
    contents = _seat_command_contents(harness, runtime=runtime)
    if harness == 'codex':
        contents = {
            f'{Path(name).stem}/SKILL.md': (
                f'---\nname: {Path(name).stem}\n'
                f'description: {Path(name).stem.split("-")[0].capitalize()} the current '
                'declared Floati seat only on an explicit owner request.\n---\n\n' + content
            )
            for name, content in contents.items()
        }
    for name in contents:
        _check_projection_target(out_dir / name)
    return out_dir, contents


def install_seat_commands(
    workspace: Path, harness: str, *, codex_home: Path | None = None,
    runtime: Path | None = None,
) -> dict:
    """Install the shared pair through a governed hook installer, without a role."""

    if not Path(workspace).is_dir():
        raise ProtocolRefusal(
            "lifecycle_prompts_workspace_invalid",
            "workspace must be an existing absolute nonsymlink directory",
            remedy="run the governed hook installer for the declared seat workspace",
        )
    out_dir, contents = plan_seat_commands(workspace, harness, codex_home=codex_home,
                                            runtime=runtime)
    out_dir.mkdir(parents=True, exist_ok=True)
    files = []
    for name, content in sorted(contents.items()):
        (out_dir / name).parent.mkdir(parents=True, exist_ok=True)
        _write_projection(out_dir / name, content)
        files.append({"path": name, "sha256": _digest(content)})
    return {"harness": harness, "out": str(out_dir), "files": files}


def _verb_body(
    verb: str, node: str, role: dict, template: RoleTemplate, root_path: str
) -> str:
    duties = ", ".join(template.duties[:2])
    lines = [
        f"You are **{node}**, role `{role['template_role']}` "
        f"(template v{role['template_version']}, sha {role['template_sha256'][:12]}; "
        f"duties: {duties}).",
        "",
        f"**{verb.upper()}.**",
    ]
    lines.extend(_verb_lines(verb, root_path, node))
    if verb == "board":
        lines.extend([
            "",
            "On success, present the artifact's verified `takeover_report` verbatim: "
            "`TOOK OVER <node> from <newly displaced session or none>`.",
            "Replay preserves historical displacement and reports no new displacement.",
        ])
    lines.extend([
        "",
        "Fill the uppercase placeholders from your seat declaration, never "
        "from memory.",
    ])
    return "\n".join(lines)


def _zcode_section(node: str, sha: str, parity: dict[str, str], root_path: str) -> str:
    lines = [
        _header("BOOT.md verb section", sha),
        "",
        f"## Lifecycle verbs for {node}",
        "",
    ]
    for verb in LIFECYCLE_VERBS:
        cell = parity[verb]
        if cell.startswith("file:"):
            lines.append(f"- **{verb}**: realized by `{cell[5:]}` in this directory.")
        else:
            lines.append(f"- **{verb}**: {cell}")
    lines.append("")
    for verb in ("boot", "prep-clear"):
        lines.extend(_verb_lines(verb, root_path, node))
    return "\n".join(lines) + "\n"


def project_prompts(root: FloatiRoot, node: str, harness: str, out_dir: Path) -> dict:
    """Write the seat's lifecycle projections under ``out_dir`` and report."""

    adapter = HARNESS_ADAPTERS.get(harness)
    if adapter is None:
        raise ProtocolRefusal(
            "lifecycle_prompts_harness_unknown",
            f"harness {harness!r} has no adapter; known: "
            f"{', '.join(sorted(HARNESS_ADAPTERS))}",
        )
    node = validate_identifier(node, "node")
    backend = RegistryAdminBackend(root)
    with backend.read_once(node_id=node):
        entry = backend.active_node(node)
        if str(entry.get("role", "")).casefold() != harness.casefold():
            registered_harness = str(entry.get("role", "")).casefold()
            detail = (
                f"node {node!r} is registered on harness {entry.get('role')!r}, "
                f"not {harness!r}"
            )
            if registered_harness in HARNESS_ADAPTERS:
                remedy = shlex.join([
                    "floati", "node", "prompts", "--root", str(root.tenant_home),
                    "--as", node, "--harness", registered_harness,
                    "--out", str(out_dir),
                ])
            else:
                detail += (
                    "; the registered harness has no lifecycle adapter; the operator "
                    "can correct the assignment with node switch or re-register "
                    "the node using its intended harness and model"
                )
                remedy = "floati node switch --help"
            raise ProtocolRefusal(
                "lifecycle_prompts_harness_mismatch",
                detail,
                remedy=remedy,
            )
        role = backend.role_record(node)
    templates = RoleTemplateLibrary(root).templates()
    template = templates.get(str(role["template_role"]))
    if template is None:
        raise ProtocolRefusal(
            "role_template_unknown", "the node's role template is not shipped"
        )
    sha = harbor_sha()
    root_path = str(root.tenant_home)

    parity: dict[str, str] = {}
    for verb in LIFECYCLE_VERBS:
        if harness in SEAT_COMMAND_HARNESSES and verb == "boot":
            parity[verb] = "file:boot-seat.md"
        elif verb in adapter["verbs"]:
            stem = _file_stem(
                adapter, verb, node
            )
            parity[verb] = f"file:{stem}.md"
        elif (harness, verb) in DASH_RECEIPTS:
            parity[verb] = DASH_RECEIPTS[(harness, verb)]
        else:
            raise ProtocolRefusal(
                "lifecycle_prompts_parity_gap",
                f"verb {verb!r} on harness {harness!r} is neither realized nor "
                "an honest dash (LC-1 §1)",
            )
    for verb in adapter["verbs"]:
        if verb in parity:
            continue
        stem = _file_stem(
            adapter, verb, node
        )
        parity[verb] = f"file:{stem}.md"

    contents = {}
    for verb in adapter["verbs"]:
        if verb == 'boot' and harness in SEAT_COMMAND_HARNESSES:
            continue
        stem = _file_stem(adapter, verb, node)
        assert_no_collision(stem)
        contents[f"{stem}.md"] = "\n".join([
            _header(f"{harness} {verb} prompt", sha), "",
            _verb_body(verb, node, role, template, root_path), "",
        ]) + "\n"
    if harness in SEAT_COMMAND_HARNESSES:
        contents.update(_seat_command_contents(harness, sha))
        parity["teardown"] = "file:teardown-seat.md"
    for name in contents:
        assert_no_collision(Path(name).stem)
        _check_projection_target(out_dir / name)
    out_dir.mkdir(parents=True, exist_ok=True)
    files: list[dict[str, str]] = []
    for name, content in contents.items():
        target = out_dir / name
        _write_projection(target, content)
        files.append({"path": name, "sha256": _digest(content)})

    files.sort(key=lambda file_entry: file_entry["path"])
    return {
        "node": node,
        "harness": harness,
        "out": str(out_dir),
        "files": files,
        "parity": {harness: parity},
    }

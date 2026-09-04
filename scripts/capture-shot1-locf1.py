#!/usr/bin/env python3
"""Generate deterministic SHOT-1 and LOC-F1 terminal photographs."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import math
import os
import re
import subprocess
import sys
import textwrap
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Mapping, NamedTuple, Sequence


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT))

from floati.identity_fence import (  # noqa: E402
    HOME_PATTERN,
    OWNER_USERNAME,
    redact_governed_temp_prefixes,
)
from floati.scrub import FORBIDDEN_NAMES  # noqa: E402


SOURCE_SHA = re.compile(r"^[0-9a-f]{40}$")
UTC_STAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")
CAPTURE_REFUSAL_EXIT = 20
FONT_SIZE = 18
TERMINAL_COLUMNS = 120
TERMINAL_ROWS = 40
FONT_CANDIDATES = (
    Path("/System/Library/Fonts/Menlo.ttc"),
    Path("/System/Library/Fonts/SFNSMono.ttf"),
    Path("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"),
)
FONT_ABSENT_CODE = "capture_font_absent"
THEMES = {
    "dark": {"background": "#12161c", "foreground": "#d8dee9"},
    "light": {"background": "#f7f3eb", "foreground": "#20252c"},
}
DERIVED_DEMO_DOCUMENTS = ("CAPTURE-INVENTORY.md", "manifest.json")
ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
VOLATILE_ID = re.compile(
    r"\b(?:msg|ack|delivery|denial|work|worker-receipt|wake-attempt|wake-hold)-[0-9a-f]{32}\b"
)
EXPOSED_TEMP_CHILD = re.compile(r"(?<=<temp>/)[A-Za-z0-9._-]+")


class CaptureRefusal(RuntimeError):
    def __init__(self, code: str, detail: str, remedy: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.remedy = remedy


class DeclaredFont(NamedTuple):
    path: Path
    sha256: str
    face: str
    cell_width: int
    cell_height: int
    data: bytes


class CaptureMoment(NamedTuple):
    name: str
    text: str
    surface: str
    data_class: str
    source: str


def terminal_grid(
    text: str,
    columns: int = TERMINAL_COLUMNS,
    rows: int = TERMINAL_ROWS,
) -> str:
    """Return one exact terminal cell grid; refuse rather than crop testimony."""

    if columns <= 0 or rows <= 0:
        raise ValueError("terminal dimensions must be positive")
    lines = text.expandtabs(4).splitlines()
    if len(lines) > rows or any(len(line) > columns for line in lines):
        raise CaptureRefusal(
            "capture_terminal_overflow",
            f"capture testimony does not fit {columns}x{rows} without cropping",
            "shorten the product testimony at its source and regenerate the whole frame",
        )
    padded = [line.ljust(columns) for line in lines]
    padded.extend(" " * columns for _ in range(rows - len(padded)))
    return "\n".join(padded) + "\n"


def terminal_transcript(text: str) -> str:
    """Serialize visible testimony without terminal-cell padding."""

    terminal_grid(text)
    lines = [line.rstrip() for line in text.expandtabs(4).splitlines()]
    return "\n".join(lines).rstrip() + "\n"


def resolve_capture_font(
    path: Path | None,
    *,
    candidates: Sequence[Path] | None = None,
) -> DeclaredFont:
    """Resolve one declared face, or one member of the fixed host set."""

    remedy = "declare one absolute readable regular file with --font PATH"
    candidates = FONT_CANDIDATES if candidates is None else tuple(candidates)
    if path is None:
        path = next(
            (
                candidate
                for candidate in candidates
                if candidate.is_file() and os.access(candidate, os.R_OK)
            ),
            None,
        )
        if path is None:
            raise CaptureRefusal(
                FONT_ABSENT_CODE,
                (
                    "no capture font was declared and none of the fixed candidates "
                    f"is readable: {', '.join(str(candidate) for candidate in candidates)}"
                ),
                remedy,
            )
    if not path.is_absolute() or not path.is_file() or not os.access(path, os.R_OK):
        raise CaptureRefusal(
            "capture_font_declaration_invalid",
            f"--font must name an absolute readable regular file: {path}",
            remedy,
        )
    data = path.read_bytes()
    try:
        from PIL import ImageFont

        face = ImageFont.truetype(io.BytesIO(data), FONT_SIZE)
    except ModuleNotFoundError as exc:
        raise CaptureRefusal(
            "capture_renderer_absent",
            "Pillow is unavailable to the declared capture host python",
            "run with the declared /usr/bin/python3 host that provides Pillow",
        ) from exc
    except OSError as exc:
        raise CaptureRefusal(
            "capture_font_declaration_invalid",
            f"declared font bytes are not a loadable face: {path}",
            remedy,
        ) from exc
    bbox = face.getbbox("Mg")
    cell_width = max(1, math.ceil(face.getlength("M")))
    cell_height = max(1, bbox[3] - min(0, bbox[1]) + 4)
    family, style = face.getname()
    return DeclaredFont(
        path=path,
        sha256=hashlib.sha256(data).hexdigest(),
        face=f"{family} {style}".strip(),
        cell_width=cell_width,
        cell_height=cell_height,
        data=data,
    )


def render_terminal_png(
    text: str,
    theme: str,
    font: DeclaredFont,
    *,
    scale: int = 2,
):
    """Raster one exact terminal grid directly at the declared source scale."""

    if theme not in THEMES:
        raise ValueError(f"unknown terminal theme: {theme}")
    if scale <= 0:
        raise ValueError("source scale must be positive")
    lines = text.splitlines()
    if len(lines) != TERMINAL_ROWS or any(len(line) != TERMINAL_COLUMNS for line in lines):
        raise ValueError("terminal raster input must already be an exact 120x40 grid")

    from PIL import Image, ImageDraw, ImageFont

    palette = THEMES[theme]
    image = Image.new(
        "RGB",
        (
            TERMINAL_COLUMNS * font.cell_width * scale,
            TERMINAL_ROWS * font.cell_height * scale,
        ),
        palette["background"],
    )
    draw = ImageDraw.Draw(image)
    face = ImageFont.truetype(io.BytesIO(font.data), FONT_SIZE * scale)
    for row, line in enumerate(lines):
        draw.text(
            (0, row * font.cell_height * scale),
            line,
            font=face,
            fill=palette["foreground"],
            spacing=0,
        )
    return image


def _wrapped_lines(text: str, width: int = TERMINAL_COLUMNS) -> list[str]:
    lines: list[str] = []
    for line in text.splitlines():
        if len(line) <= width:
            lines.append(line)
            continue
        pieces = textwrap.wrap(
            line,
            width=width,
            subsequent_indent="    ",
            break_long_words=True,
            break_on_hyphens=False,
            replace_whitespace=False,
            drop_whitespace=False,
        )
        lines.extend(piece.rstrip() for piece in pieces)
    return lines


def expose_capture_text(text: str, *, scratch_roots: Iterable[Path]) -> str:
    """Normalize instrument coordinates once, then enforce publication fences."""

    exposed = ANSI.sub("", text).replace("\r\n", "\n").replace("\r", "\n")
    for root in sorted((path.resolve() for path in scratch_roots), key=lambda p: -len(str(p))):
        exposed = exposed.replace(str(root), "<scratch>")
    exposed = redact_governed_temp_prefixes(exposed)
    exposed = EXPOSED_TEMP_CHILD.sub("capture", exposed)
    exposed = HOME_PATTERN.sub("~/", exposed)
    exposed = exposed.replace(OWNER_USERNAME, "operator")
    exposed = VOLATILE_ID.sub("<receipt-id>", exposed)
    for code, token in FORBIDDEN_NAMES:
        decoded = token.decode("ascii")
        if decoded.casefold() in exposed.casefold():
            raise CaptureRefusal(
                "capture_text_forbidden",
                f"capture testimony contains governed name class {code}",
                "regenerate from a public-safe source ledger; never edit the photograph",
            )
    lines = _wrapped_lines(exposed.strip(), TERMINAL_COLUMNS)
    if len(lines) > TERMINAL_ROWS:
        raise CaptureRefusal(
            "capture_terminal_overflow",
            f"exposed testimony has {len(lines)} rows; terminal contract permits {TERMINAL_ROWS}",
            "select a narrower product surface and regenerate the whole frame",
        )
    return "\n".join(lines).rstrip() + "\n"


def _moment(
    name: str,
    text: str,
    surface: str,
    data_class: str,
    source: str,
    *,
    scratch_roots: Iterable[Path],
) -> CaptureMoment:
    return CaptureMoment(
        name,
        expose_capture_text(text, scratch_roots=scratch_roots),
        surface,
        data_class,
        source,
    )


def _captured_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def _board_moment() -> CaptureMoment:
    path = REPOSITORY_ROOT / "docs/evidence/captures/hm1-tui-monochrome.txt"
    return _moment(
        "board",
        path.read_text(encoding="utf-8"),
        "floati board --demo --no-animation",
        "fixture",
        path.relative_to(REPOSITORY_ROOT).as_posix(),
        scratch_roots=(),
    )


def _handoff_moment(scratch: Path, source_sha: str, now: datetime) -> CaptureMoment:
    from floati.cursor import SparseCursor
    from floati.errors import ProtocolRefusal
    from floati.events import EventLog
    from floati.registry import Registry
    from floati.root import FloatiRoot

    root = FloatiRoot.open_direct_home(scratch / "handoff", create=True)
    registry = Registry(root)
    registry.register("sender", "Codex")
    registry.register("receiver", "Claude")
    events = EventLog(root)
    send = events.send(
        "sender",
        "receiver",
        "floati",
        source_sha,
        "docs/status/QUEUE-2026-09-01.md",
        "public capture handoff",
        idempotency_key="capture-handoff",
        now=now,
    )
    message = send["message"]
    presented, delivery = events.present("receiver", now=now)
    acknowledgment = SparseCursor(root).ack(
        "receiver",
        [str(message["id"])],
        acting_session_id="capture-session",
        now=now,
    )
    denied_code = "absent"
    try:
        events.send(
            "sender",
            "receiver",
            "floati",
            source_sha,
            "docs/status/QUEUE-2026-09-01.md",
            "different payload",
            idempotency_key="capture-handoff",
            now=now,
        )
    except ProtocolRefusal as refusal:
        denied_code = refusal.code
    text = "\n".join(
        (
            "FLOATI // HANDOFF RECEIPTS",
            "$ floati receipts receiver --root <scratch>",
            "",
            f"DELIVERED      {delivery['kind']}  items={len(presented)}",
            f"ACKNOWLEDGED   {acknowledgment['kind']}  items={len(acknowledgment['item_ids'])}",
            f"DENIED         denial_receipt  code={denied_code}",
            "",
            "Append is not delivery. Delivery is not acknowledgment.",
            "A refusal is recorded separately from both.",
        )
    )
    return _moment(
        "handoff",
        text,
        "floati receipts NODE --root ROOT",
        "real",
        "fresh scratch ledger: send -> inbox -> ack -> refused send",
        scratch_roots=(scratch,),
    )


def _lease_moment(scratch: Path, now: datetime) -> CaptureMoment:
    from floati.presence import PresenceService
    from floati.registry import Registry
    from floati.root import FloatiRoot

    root = FloatiRoot.open_direct_home(scratch / "lease", create=True)
    Registry(root).register("receiver", "Codex")
    service = PresenceService(root)
    service.report_self("receiver", ttl_seconds=30, now=now)
    before = service.reports(now)[0]
    after = service.reports(now + timedelta(seconds=31))[0]
    text = "\n".join(
        (
            "FLOATI // LEASE EXPIRY",
            "$ floati presence show --root <scratch>",
            "",
            f"BEFORE  receiver  {before['state']}  ttl={before['ttl_seconds']}s",
            f"AFTER   receiver  {after['state']}  expires_at={after['expires_at']}",
            "",
            "AUTHORITY EXPIRED — work remains recorded and available.",
            "Expiry means no report since the named time; it never means down.",
        )
    )
    return _moment(
        "lease",
        text,
        "floati presence show plus expired-authority board testimony",
        "real",
        "fresh scratch liveness ledger",
        scratch_roots=(scratch,),
    )


def _declared_utf8_file(path: Path, flag: str) -> Path:
    if not path.is_absolute() or not path.is_file() or not os.access(path, os.R_OK):
        raise CaptureRefusal(
            "capture_input_declaration_invalid",
            f"{flag} must name an absolute readable regular file: {path}",
            f"declare one absolute readable UTF-8 file with {flag} PATH",
        )
    return path


def _declared_source(path: Path) -> str:
    resolved = path.resolve()
    try:
        return resolved.relative_to(REPOSITORY_ROOT.resolve()).as_posix()
    except ValueError:
        return resolved.name


def _dead_receiver_moment(path: Path) -> CaptureMoment:
    declared = _declared_utf8_file(path, "--dead-receiver-transcript")
    return _moment(
        "dead-receiver",
        declared.read_text(encoding="utf-8"),
        "floati doctor --probe --probe-budget 1",
        "real",
        _declared_source(declared),
        scratch_roots=(),
    )


def _identity_moment(scratch: Path, now: datetime) -> CaptureMoment:
    from floati.planes import AuthorityGrantStore
    from floati.registry import Registry
    from floati.root import FloatiRoot

    root = FloatiRoot.open_direct_home(scratch / "identity", create=True)
    registry = Registry(root)
    registry.register("architect", "Codex")
    registry.register("builder", "Claude")
    grant = AuthorityGrantStore(root).claim("site-capture", "builder", 300, 300, now)
    text = "\n".join(
        (
            "FLOATI // IDENTITY AND AUTHORITY",
            "$ floati grant --as architect --holder builder --subject site-capture --epoch 1",
            "",
            f"holder: {grant['holder']}",
            f"subject: {grant['subject_id']}",
            f"epoch: {grant['epoch']}",
            f"expires_at: {grant['expires_at']}",
            "",
            "Detached signatures verify exact artifact bytes and their binding.",
            "No certificate view exists: do not claim this is a session certificate.",
        )
    )
    return _moment(
        "identity",
        text,
        "floati grant plus signature verify",
        "real",
        "fresh scratch authority ledger",
        scratch_roots=(scratch,),
    )


def _adapter_moment(path: Path) -> CaptureMoment:
    declared = _declared_utf8_file(path, "--adapter-transcript")
    text = (
        declared.read_text(encoding="utf-8")
        + "\nONE BUS, MANY HARNESSES — adapters keep their dialects at the boundary.\n"
    )
    return _moment(
        "adapter-boundary",
        text,
        "floati chart --declared-roots FILE",
        "real",
        _declared_source(declared),
        scratch_roots=(),
    )


def _failure_moment() -> CaptureMoment:
    path = REPOSITORY_ROOT / "docs/evidence/captures/floati-replay-drill.txt"
    source = path.read_text(encoding="utf-8")
    selected = source.split("\n{", 1)[0].rstrip()
    text = (
        "FLOATI // FAILURE INJECTION\n"
        "$ python3 -m unittest -v tests.test_gauntlet_crash\n\n"
        "INJECTED: worker process failure and authority change\n"
        "CAUGHT: typed replay evidence preserved below\n\n"
        + selected
    )
    return _moment(
        "failure-injection",
        text,
        "gauntlet crash injection replay",
        "real",
        path.relative_to(REPOSITORY_ROOT).as_posix(),
        scratch_roots=(),
    )


def _help_moment() -> CaptureMoment:
    result = subprocess.run(
        [sys.executable, "-m", "floati", "--help"],
        cwd=REPOSITORY_ROOT,
        env={"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "PYTHONPATH": str(REPOSITORY_ROOT)},
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise CaptureRefusal(
            "capture_help_failed",
            f"root help exited {result.returncode}: {result.stderr.strip()}",
            "repair the root help surface before capturing it",
        )
    return _moment(
        "help",
        "$ floati --help\n\n" + result.stdout,
        "floati --help",
        "real",
        "live argparse command surface",
        scratch_roots=(),
    )


def build_loc_moments(
    scratch: Path,
    source_sha: str,
    captured_at: str,
    *,
    dead_receiver_transcript: Path,
    adapter_transcript: Path,
) -> tuple[CaptureMoment, ...]:
    scratch.mkdir(parents=True, exist_ok=True)
    now = _captured_time(captured_at)
    return (
        _board_moment(),
        _handoff_moment(scratch, source_sha, now),
        _lease_moment(scratch, now),
        _dead_receiver_moment(dead_receiver_transcript),
        _identity_moment(scratch, now),
        _adapter_moment(adapter_transcript),
        _failure_moment(),
        _help_moment(),
    )


def _survey_moment(scratch: Path) -> CaptureMoment:
    from floati.foreign_bus_survey import ForeignBusSurvey
    from floati.registry import Registry
    from floati.root import FloatiRoot

    declared = FloatiRoot.open_direct_home(scratch / "survey" / "declared", create=True)
    Registry(declared).register("builder", "Codex")
    foreign = scratch / "survey" / "foreign-bus"
    foreign.mkdir(parents=True)
    (foreign / "events.jsonl").write_text("", encoding="utf-8")
    declarations = scratch / "survey" / "declared-roots.json"
    declarations.write_text(
        json.dumps(
            {
                "schema_version": 0,
                "roots": [
                    {
                        "bus_id": "floati-declared",
                        "root": str(declared.path),
                        "architect_node": "builder",
                        "downstream": [],
                    }
                ],
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    hooks = scratch / "survey" / "hooks.json"
    hooks.write_text(json.dumps({"hooks": [{"command": f"wait --root {foreign}"}]}), encoding="utf-8")
    targets = scratch / "survey" / "targets.json"
    targets.write_text(json.dumps({"workspaces": [str(declared.path)]}), encoding="utf-8")
    artifact = ForeignBusSurvey(
        declarations,
        search_paths=(foreign.parent,),
        hooks_path=hooks,
        targets_paths=(targets,),
    ).run()
    text = (
        "FLOATI // FOREIGN-BUS SURVEY\n"
        "$ floati survey --declared-roots <scratch>/declared-roots.json --json\n\n"
        + json.dumps(artifact, indent=2, sort_keys=True)
    )
    return _moment(
        "survey",
        text,
        "floati survey --declared-roots FILE --json",
        "fixture",
        "fresh explicit bounded survey fixture",
        scratch_roots=(scratch,),
    )


def render_wake_ping_summary(source_text: str) -> str:
    """Verify the real gate facts and project them without fleet coordinates."""

    required = (
        "`wake_attempt_receipt`s",
        "`outcome: woke`",
        "no human in the mail path",
        "trusted hook",
        "armed waiter",
        "session wakes",
        "work begins",
        "organic wake receipt from a real harness Stop",
    )
    missing = [needle for needle in required if needle not in source_text]
    if missing:
        raise CaptureRefusal(
            "capture_wake_evidence_invalid",
            "organic wake evidence is missing: " + ", ".join(missing),
            "restore the ruled gate evidence before regenerating the wake frame",
        )
    return (
        "REAL WAKE ATTEMPT RECEIPT — outcome: woke\n"
        "NO HUMAN IN THE MAIL PATH\n"
        "TRUSTED HOOK → ARMED WAITER → ENVELOPE LANDS\n"
        "SESSION WAKES → WORK BEGINS\n"
        "ACCEPTANCE: ORGANIC HARNESS STOP WAKE SATISFIED"
    )


def _wake_ping_moment(path: Path) -> CaptureMoment:
    declared = _declared_utf8_file(path, "--wake-evidence")
    summary = render_wake_ping_summary(declared.read_text(encoding="utf-8"))
    text = "\n".join(
        (
            "FLOATI // WAKE PING",
            "$ floati receipts NODE --root ROOT",
            "",
            "ENVELOPE APPENDED",
            "WAITER OBSERVED UNREAD MAIL",
            "WAKE ATTEMPT RECEIPT — outcome: woke",
            "SESSION RESUMED; WORK BEGAN",
            "",
            summary,
        )
    )
    return _moment(
        "wake-ping",
        text,
        "organic Codex Stop wake receipt",
        "real",
        _declared_source(declared),
        scratch_roots=(),
    )


def render_uninstall_summary(artifact: dict[str, object]) -> str:
    """Project a successful dry-run artifact without expanding per-file receipts."""

    evidence = artifact.get("evidence")
    if artifact.get("status") != "ok" or not isinstance(evidence, dict):
        raise CaptureRefusal(
            "capture_uninstall_artifact_invalid",
            "uninstall dry run did not emit a successful evidence object",
            "repair the dry-run artifact before capturing uninstall",
        )
    receipts = evidence.get("removal_receipts")
    foreign = evidence.get("foreign_preserved")
    removed_count = evidence.get("removed_count")
    retention = evidence.get("data_retention_notice")
    if (
        evidence.get("dry_run") is not True
        or not isinstance(receipts, list)
        or not isinstance(foreign, list)
        or not all(isinstance(path, str) for path in foreign)
        or not isinstance(removed_count, int)
        or not isinstance(retention, str)
    ):
        raise CaptureRefusal(
            "capture_uninstall_artifact_invalid",
            "uninstall dry-run evidence is missing its stable summary fields",
            "repair the dry-run artifact before capturing uninstall",
        )
    retained_names = ", ".join(foreign)
    retained = (
        retained_names
        if len(retained_names) <= 80
        else f"{len(foreign)} (full paths retained in the machine artifact)"
    )
    if not foreign:
        retained = "none"
    return "\n".join(
        (
            "FLOATI // UNINSTALL DRY RUN",
            "$ floati uninstall --destination <scratch>/installed --dry-run",
            "",
            "STATUS: ok",
            "DRY RUN: true",
            f"OWNED TOOL FILES PLANNED: {len(receipts)}",
            f"FILES REMOVED: {removed_count}",
            f"FOREIGN FILES RETAINED: {retained}",
            "",
            retention,
            "NO FILE REMOVED — dry run is a plan, not a mutation.",
        )
    )


def _uninstall_moment(scratch: Path) -> CaptureMoment:
    base = scratch / "uninstall"
    destination = base / "installed"
    base.mkdir(parents=True, exist_ok=True)
    install = subprocess.run(
        [
            str(REPOSITORY_ROOT / "scripts/floati"),
            "install",
            "--source",
            str(REPOSITORY_ROOT),
            "--destination",
            str(destination),
            "--committed-tree",
        ],
        cwd=REPOSITORY_ROOT,
        env={
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            "PYTHONDONTWRITEBYTECODE": "1",
        },
        check=False,
        capture_output=True,
        text=True,
    )
    if install.returncode != 0:
        raise CaptureRefusal(
            "capture_install_failed",
            f"scratch committed-tree install exited {install.returncode}: {install.stderr.strip()}",
            "repair the committed-tree install before capturing uninstall",
        )
    foreign = destination / "foreign.keep"
    foreign.write_text("not owned by Floati\n", encoding="utf-8")
    dry_run = subprocess.run(
        [
            str(destination / "scripts/floati"),
            "uninstall",
            "--destination",
            str(destination),
            "--dry-run",
        ],
        cwd=REPOSITORY_ROOT,
        env={
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            "PYTHONDONTWRITEBYTECODE": "1",
        },
        check=False,
        capture_output=True,
        text=True,
    )
    if dry_run.returncode != 0 or not foreign.is_file():
        raise CaptureRefusal(
            "capture_uninstall_dry_run_failed",
            f"uninstall dry run exited {dry_run.returncode} or changed a foreign file",
            "repair dry-run non-mutation before capturing uninstall",
        )
    try:
        artifact = json.loads(dry_run.stdout)
    except json.JSONDecodeError as exc:
        raise CaptureRefusal(
            "capture_uninstall_artifact_invalid",
            "uninstall dry run did not emit one JSON artifact",
            "repair the dry-run artifact before capturing uninstall",
        ) from exc
    text = render_uninstall_summary(artifact)
    return _moment(
        "uninstall-dry-run",
        text,
        "floati uninstall --dry-run",
        "real",
        "fresh committed-tree scratch installation",
        scratch_roots=(scratch,),
    )


def build_shot_moments(
    scratch: Path,
    source_sha: str,
    captured_at: str,
    *,
    wake_evidence: Path,
) -> tuple[CaptureMoment, ...]:
    del source_sha, captured_at
    scratch.mkdir(parents=True, exist_ok=True)
    return (
        _survey_moment(scratch),
        _wake_ping_moment(wake_evidence),
        _uninstall_moment(scratch),
    )


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _file_receipt(path: Path, relative_path: str) -> dict[str, object]:
    return {
        "path": relative_path,
        "bytes": path.stat().st_size,
        "sha256": _digest(path),
    }


def derive_demo_inventory(demo_root: Path) -> dict[str, object]:
    """Derive one non-circular directory-truth model from demo bytes."""

    if not demo_root.is_dir():
        raise CaptureRefusal(
            "capture_demo_root_invalid",
            f"demo root is not a directory: {demo_root}",
            "declare the existing docs/demo directory after capture generation",
        )
    direct_files = [
        _file_receipt(path, path.name)
        for path in sorted(demo_root.iterdir(), key=lambda candidate: candidate.name)
        if path.is_file() and path.name not in DERIVED_DEMO_DOCUMENTS
    ]
    nested_manifests = [
        _file_receipt(path, path.relative_to(demo_root).as_posix())
        for path in sorted(demo_root.rglob("manifest.json"))
        if path.parent != demo_root
    ]
    return {
        "schema_version": 0,
        "generator": "scripts/capture-shot1-locf1.py",
        "root": "docs/demo",
        "excluded_generated_files": list(DERIVED_DEMO_DOCUMENTS),
        "direct_files": direct_files,
        "nested_manifests": nested_manifests,
    }


def _count_label(count: int, singular: str) -> str:
    return f"{count} {singular}" + ("" if count == 1 else "s")


def render_demo_inventory(model: Mapping[str, object]) -> str:
    """Render the human inventory from the exact manifest model."""

    direct_files = model["direct_files"]
    nested_manifests = model["nested_manifests"]
    if not isinstance(direct_files, list) or not isinstance(nested_manifests, list):
        raise ValueError("demo inventory lists are required")
    lines = [
        "# Demo capture inventory",
        "",
        "Status: generated from the committed `docs/demo` directory truth.",
        "The two generated truth documents are excluded from their own receipt set.",
        "",
        f"## Direct capture files — {_count_label(len(direct_files), 'direct capture file')}",
        "",
    ]
    lines.extend(
        f"- `docs/demo/{row['path']}` — {row['bytes']} bytes — SHA-256 `{row['sha256']}`"
        for row in direct_files
    )
    if not direct_files:
        lines.append("- None.")
    lines.extend(
        (
            "",
            f"## Nested capture families — {_count_label(len(nested_manifests), 'nested capture manifest')}",
            "",
        )
    )
    lines.extend(
        f"- `docs/demo/{row['path']}` — {row['bytes']} bytes — SHA-256 `{row['sha256']}`"
        for row in nested_manifests
    )
    if not nested_manifests:
        lines.append("- None.")
    lines.extend(
        (
            "",
            "## Generated truth documents",
            "",
            "- `docs/demo/manifest.json`",
            "- `docs/demo/CAPTURE-INVENTORY.md`",
            "",
            "Nested PNGs and transcripts are receipted by each nested manifest and are not duplicated here.",
        )
    )
    return "\n".join(lines) + "\n"


def write_demo_inventory(demo_root: Path) -> dict[str, object]:
    model = derive_demo_inventory(demo_root)
    (demo_root / "manifest.json").write_text(
        json.dumps(model, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (demo_root / "CAPTURE-INVENTORY.md").write_text(
        render_demo_inventory(model),
        encoding="utf-8",
    )
    return model


def write_capture_directory(
    output: Path,
    moments: Sequence[CaptureMoment],
    font: DeclaredFont,
    source_sha: str,
    captured_at: str,
    *,
    site_names: bool,
) -> dict[str, object]:
    if output.exists() and any(output.iterdir()):
        raise CaptureRefusal(
            "capture_output_not_empty",
            f"capture output is not empty: {output}",
            "declare a new empty output directory",
        )
    output.mkdir(parents=True, exist_ok=True)
    artifacts: list[dict[str, object]] = []
    for moment in moments:
        grid = terminal_grid(moment.text)
        transcript = terminal_transcript(moment.text)
        for theme in ("dark", "light"):
            stem = (
                f"floati-{moment.name}-{theme}-source"
                if site_names
                else f"{moment.name}-{theme}"
            )
            text_path = output / f"{stem}.txt"
            text_path.write_text(transcript, encoding="utf-8")
            image_path = output / f"{stem}.png"
            render_terminal_png(grid, theme, font, scale=2).save(
                image_path,
                format="PNG",
                optimize=False,
                compress_level=9,
            )
            for kind, path in (("transcript", text_path), ("png", image_path)):
                artifacts.append(
                    {
                        "path": path.name,
                        "kind": kind,
                        "moment": moment.name,
                        "theme": theme,
                        "sha256": _digest(path),
                        "bytes": path.stat().st_size,
                    }
                )
    manifest: dict[str, object] = {
        "schema_version": 0,
        "generator": "scripts/capture-shot1-locf1.py",
        "source_sha": source_sha,
        "captured_at": captured_at,
        "terminal": {"columns": 120, "rows": 40, "source_scale": 2},
        "font": {
            "path": str(font.path),
            "sha256": font.sha256,
            "face": font.face,
            "cell_width": font.cell_width,
            "cell_height": font.cell_height,
        },
        "themes": THEMES,
        "moments": [
            {
                "name": moment.name,
                "surface": moment.surface,
                "data_class": moment.data_class,
                "source": moment.source,
            }
            for moment in moments
        ],
        "artifacts": sorted(artifacts, key=lambda row: str(row["path"])),
    }
    (output / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return manifest


def _refusal_artifact(command: str, refusal: CaptureRefusal) -> dict[str, object]:
    return {
        "artifact_version": 0,
        "command": command,
        "status": "refused",
        "evidence": {
            "code": refusal.code,
            "detail": refusal.detail,
            "remedy": refusal.remedy,
        },
    }


def _validate_inputs(args: argparse.Namespace) -> DeclaredFont:
    if SOURCE_SHA.fullmatch(args.source_sha) is None:
        raise CaptureRefusal(
            "capture_source_sha_invalid",
            "--source-sha must be lowercase 40-hex",
            "pass the exact committed source SHA",
        )
    if UTC_STAMP.fullmatch(args.captured_at) is None:
        raise CaptureRefusal(
            "capture_timestamp_invalid",
            "--captured-at must be an explicit millisecond UTC timestamp",
            "pass YYYY-MM-DDTHH:MM:SS.mmmZ",
        )
    try:
        datetime.fromisoformat(args.captured_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CaptureRefusal(
            "capture_timestamp_invalid",
            "--captured-at is not a calendar-valid UTC timestamp",
            "pass YYYY-MM-DDTHH:MM:SS.mmmZ",
        ) from exc
    for name in ("scratch", "site_output", "shot_output"):
        value = getattr(args, name)
        if not value.is_absolute():
            raise CaptureRefusal(
                "capture_path_invalid",
                f"--{name.replace('_', '-')} must be absolute: {value}",
                "declare absolute empty output and scratch paths",
            )
    if args.demo_root is not None and not args.demo_root.is_absolute():
        raise CaptureRefusal(
            "capture_path_invalid",
            f"--demo-root must be absolute: {args.demo_root}",
            "declare the absolute docs/demo directory",
        )
    _declared_utf8_file(args.dead_receiver_transcript, "--dead-receiver-transcript")
    _declared_utf8_file(args.adapter_transcript, "--adapter-transcript")
    _declared_utf8_file(args.wake_evidence, "--wake-evidence")
    return resolve_capture_font(args.font)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--font", type=Path, required=True)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--captured-at", required=True)
    parser.add_argument("--scratch", type=Path, required=True)
    parser.add_argument("--site-output", type=Path, required=True)
    parser.add_argument("--shot-output", type=Path, required=True)
    parser.add_argument("--dead-receiver-transcript", type=Path, required=True)
    parser.add_argument("--adapter-transcript", type=Path, required=True)
    parser.add_argument("--wake-evidence", type=Path, required=True)
    parser.add_argument("--demo-root", type=Path)
    args = parser.parse_args(argv)
    try:
        font = _validate_inputs(args)
        for path in (args.scratch, args.site_output, args.shot_output):
            if path.exists() and any(path.iterdir()):
                raise CaptureRefusal(
                    "capture_output_not_empty",
                    f"capture path is not empty: {path}",
                    "declare new empty scratch and output paths",
                )
        loc_moments = build_loc_moments(
            args.scratch / "loc",
            args.source_sha,
            args.captured_at,
            dead_receiver_transcript=args.dead_receiver_transcript,
            adapter_transcript=args.adapter_transcript,
        )
        shot_moments = build_shot_moments(
            args.scratch / "shot",
            args.source_sha,
            args.captured_at,
            wake_evidence=args.wake_evidence,
        )
        site_manifest = write_capture_directory(
            args.site_output,
            loc_moments,
            font,
            args.source_sha,
            args.captured_at,
            site_names=True,
        )
        shot_manifest = write_capture_directory(
            args.shot_output,
            shot_moments,
            font,
            args.source_sha,
            args.captured_at,
            site_names=False,
        )
        demo_inventory = (
            write_demo_inventory(args.demo_root) if args.demo_root is not None else None
        )
        print(
            json.dumps(
                {
                    "artifact_version": 0,
                    "command": "capture-shot1-locf1",
                    "status": "ok",
                    "evidence": {
                        "site_output": str(args.site_output),
                        "site_artifacts": len(site_manifest["artifacts"]),
                        "shot_output": str(args.shot_output),
                        "shot_artifacts": len(shot_manifest["artifacts"]),
                        "source_sha": args.source_sha,
                        "captured_at": args.captured_at,
                        "demo_direct_files": (
                            len(demo_inventory["direct_files"])
                            if demo_inventory is not None
                            else None
                        ),
                        "demo_nested_manifests": (
                            len(demo_inventory["nested_manifests"])
                            if demo_inventory is not None
                            else None
                        ),
                    },
                },
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        return 0
    except CaptureRefusal as refusal:
        print(
            json.dumps(
                _refusal_artifact("capture-shot1-locf1", refusal),
                sort_keys=True,
                separators=(",", ":"),
            ),
            file=sys.stderr,
        )
        return CAPTURE_REFUSAL_EXIT


if __name__ == "__main__":
    raise SystemExit(main())

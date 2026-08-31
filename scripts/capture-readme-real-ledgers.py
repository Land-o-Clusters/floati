#!/usr/bin/env python3
"""Capture README candidates from fresh, real Floati scratch ledgers."""

from __future__ import annotations

import argparse
import hashlib
import html
import io
import json
import re
import sys
import textwrap
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT))

from floati.admin_registry import RegistryAdminBackend  # noqa: E402
from floati.doctor import Doctor  # noqa: E402
from floati.events import EventLog  # noqa: E402
from floati import fixture_ids  # noqa: E402
from floati.ids import uuid7_hex  # noqa: E402
from floati.multi_bus_chart import MultiBusHarborChart, render_multi_bus_chart  # noqa: E402
from floati.node_wizard import NodeWizard  # noqa: E402
from floati.planes import (  # noqa: E402
    AuthorityGrantStore,
    LivenessPresenceStore,
    MutualExclusionHoldStore,
)
from floati.registry import Registry  # noqa: E402
from floati.replay import ReplayTimeline  # noqa: E402
from floati.replay_render import render_replay_frame, render_replay_plain  # noqa: E402
from floati.root import FloatiRoot  # noqa: E402
from floati.tui import model_from_root  # noqa: E402
from floati.tui_render import render_frame, render_plain_dump  # noqa: E402
from floati.work import WorkLog  # noqa: E402
from floati.workers import WorkerReceipts  # noqa: E402


_FLOATI_BUILDER = fixture_ids.builder("floati")
_REVIEWER = fixture_ids.reviewer()
ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
ANSI_COLOR = re.compile(r"\x1b\[([0-9;]*)m")
FONT = Path("/System/Library/Fonts/SFNSMono.ttf")
PALETTES = {
    "dark": {
        "background": "#12161c",
        "foreground": "#d8dee9",
        "dim": "#9aa6b2",
        "accent": "#ff9f43",
    },
    "light": {
        "background": "#f7f3eb",
        "foreground": "#20252c",
        "dim": "#66717c",
        "accent": "#853d07",
    },
}


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _stamp(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _svg(testimony: str, title: str, theme: str) -> str:
    palette = PALETTES[theme]
    wrap_width = 78 if title == "doctor-delivery-health" else 96
    lines = [
        wrapped
        for line in ANSI.sub("", testimony).rstrip("\n").splitlines()
        for wrapped in (
            textwrap.wrap(
                line,
                width=wrap_width,
                subsequent_indent="    ",
                break_long_words=False,
                break_on_hyphens=False,
            )
            or [""]
        )
    ]
    height = max(140, 52 + 19 * len(lines))
    tspans = []
    for index, line in enumerate(lines):
        escaped = html.escape(line)
        color = palette["accent"] if line.startswith(("!", ">", "+")) else palette["foreground"]
        tspans.append(
            f'    <tspan x="28" dy="{0 if index == 0 else 19}" fill="{color}">{escaped}</tspan>'
        )
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="1920" height="{height}" '
        f'viewBox="0 0 1920 {height}" role="img">\n'
        f'  <title>{html.escape(title)} — {theme}</title>\n'
        "  <desc>Real Floati scratch-ledger terminal capture.</desc>\n"
        f'  <rect width="1920" height="{height}" fill="{palette["background"]}"/>\n'
        '  <text x="28" y="32" font-family="ui-monospace, SFMono-Regular, Menlo, monospace" '
        'font-size="14" xml:space="preserve">\n'
        + "\n".join(tspans)
        + "\n  </text>\n</svg>\n"
    )


def _write_capture(
    output: Path,
    name: str,
    standard: str,
    *,
    plain: str | None = None,
) -> dict[str, object]:
    standard_path = output / f"{name}-standard.txt"
    standard_path.write_text(standard.rstrip() + "\n", encoding="utf-8")
    files = [standard_path]
    if plain is not None:
        plain_path = output / f"{name}-plain.txt"
        plain_path.write_text(plain.rstrip() + "\n", encoding="utf-8")
        files.append(plain_path)
    for theme in ("dark", "light"):
        svg_path = output / f"{name}-{theme}.svg"
        svg_path.write_text(_svg(standard, name, theme), encoding="utf-8")
        files.append(svg_path)
    return {
        "name": name,
        "real_ledger": True,
        "files": [
            {"path": path.name, "sha256": _digest(path), "bytes": path.stat().st_size}
            for path in files
        ],
    }


def _line_runs(line: str, theme: str) -> list[tuple[str, str]]:
    palette = PALETTES[theme]
    runs: list[tuple[str, str]] = []
    color = palette["foreground"]
    cursor = 0
    for match in ANSI_COLOR.finditer(line):
        if match.start() > cursor:
            runs.append((line[cursor : match.start()], color))
        code = match.group(1)
        if code in {"38;5;208", "93"}:
            color = palette["accent"]
        elif code == "38;5;245":
            color = palette["dim"]
        elif code in {"", "0"}:
            color = palette["foreground"]
        cursor = match.end()
    if cursor < len(line):
        runs.append((line[cursor:], color))
    return runs or [("", color)]


def _terminal_frame(testimony: str, theme: str) -> Image.Image:
    palette = PALETTES[theme]
    image = Image.new("RGB", (1600, 900), palette["background"])
    draw = ImageDraw.Draw(image)
    font = ImageFont.truetype(str(FONT), 20)
    x_margin = 30
    y = 26
    line_height = 25
    for line in testimony.rstrip().splitlines():
        if y + line_height > image.height - 26:
            break
        x = x_margin
        for piece, color in _line_runs(line, theme):
            draw.text((x, y), piece, font=font, fill=color)
            x += round(draw.textlength(piece, font=font))
        y += line_height
    return image


def _write_gif(path: Path, frames: list[Image.Image], durations: list[int]) -> None:
    if len(frames) < 2 or len(frames) != len(durations):
        raise ValueError("animated capture requires matching multi-frame durations")
    converted = [frame.convert("P", palette=Image.Palette.ADAPTIVE) for frame in frames]
    converted[0].save(
        path,
        format="GIF",
        save_all=True,
        append_images=converted[1:],
        duration=durations,
        loop=0,
        disposal=2,
        optimize=False,
    )


def _write_animated_capture(
    output: Path,
    name: str,
    standard: str,
    animation_frames: list[str],
    *,
    plain: str | None = None,
    duration_ms: int,
) -> dict[str, object]:
    standard_path = output / f"{name}-standard.txt"
    standard_path.write_text(standard.rstrip() + "\n", encoding="utf-8")
    files = [standard_path]
    if plain is not None:
        plain_path = output / f"{name}-plain.txt"
        plain_path.write_text(plain.rstrip() + "\n", encoding="utf-8")
        files.append(plain_path)
    for theme in ("dark", "light"):
        gif_path = output / f"{name}-{theme}.gif"
        rendered = [_terminal_frame(frame, theme) for frame in animation_frames]
        durations = [duration_ms] * len(rendered)
        durations[-1] = max(1200, duration_ms)
        _write_gif(gif_path, rendered, durations)
        files.append(gif_path)
    return {
        "name": name,
        "real_ledger": True,
        "animated": True,
        "frame_count": len(animation_frames),
        "files": [
            {"path": path.name, "sha256": _digest(path), "bytes": path.stat().st_size}
            for path in files
        ],
    }


def _new_root(path: Path) -> FloatiRoot:
    if path.exists() and any(path.iterdir()):
        raise SystemExit(f"scratch root is not empty: {path}")
    return FloatiRoot.open_direct_home(path, create=True)


def _board_capture(scratch: Path, output: Path) -> dict[str, object]:
    root = _new_root(scratch / "harbor-board")
    registry = Registry(root)
    for node, harness in (
        ("architect-codex", "Architect"),
        ("builder-claude", "Claude"),
        ("reviewer-opencode", "OpenCode"),
    ):
        registry.register(node, harness)

    now = datetime.now(timezone.utc)
    liveness = LivenessPresenceStore(root)
    liveness.observe("architect-codex", 3600, now - timedelta(seconds=2))
    liveness.observe("builder-claude", 3600, now - timedelta(seconds=5))
    liveness.observe("reviewer-opencode", 60, now - timedelta(minutes=2))
    authority = AuthorityGrantStore(root)
    grant = authority.claim("capture-work", "architect-codex", 3600, 3600, now - timedelta(seconds=4))
    authority.claim("review-work", "reviewer-opencode", 3600, 3600, now - timedelta(seconds=4))
    MutualExclusionHoldStore(root).acquire(
        "capture-workspace", "architect-codex", 3600, 3600, now - timedelta(seconds=3)
    )
    work = WorkLog(root)
    claimed = work.add("capture the three-plane harbor", "architect-codex", [], now=now - timedelta(seconds=2))
    work.claim(claimed["id"], "architect-codex", "capture-work", grant["epoch"], now=now - timedelta(seconds=1))
    work.add(
        "review the frozen frame",
        "reviewer-opencode",
        [],
        needs=[claimed["id"]],
        now=now,
    )
    EventLog(root).send(
        "architect-codex",
        "builder-claude",
        "floati",
        "a" * 40,
        "docs/evidence/POST-CAMPAIGN-CAPTURE-SET.md",
        "DRAFT frame review requested",
        idempotency_key="capture-board-mail",
    )
    model = model_from_root(root, now)
    standard = (
        f"ROOT={root.path}\n$ floati board --root $ROOT --session capture-session --no-animation\n\n"
        + render_frame(model, 108, 30, selected=0, color=True)
    )
    plain = (
        f"ROOT={root.path}\n$ floati board --root $ROOT --session capture-session --no-animation\n\n"
        + render_plain_dump(model, width=108)
    )
    result = _write_capture(output, "harbor-board", standard, plain=plain)
    result["root"] = str(root.path)
    return result


def _replay_capture(scratch: Path, output: Path) -> dict[str, object]:
    root = _new_root(scratch / "flight-recorder")
    registry = Registry(root)
    registry.register("architect-codex", "Architect")
    registry.register("builder-claude", "Claude")
    now = datetime.now(timezone.utc)
    grant = AuthorityGrantStore(root).claim(
        "orchestration", "builder-claude", 3600, 3600, now - timedelta(seconds=12)
    )
    work = WorkLog(root)
    receipts = WorkerReceipts(root)
    first_session = "worker-" + uuid7_hex()
    second_session = "worker-" + uuid7_hex()
    first = work.add("assemble capture ledger", "builder-claude", [], now=now - timedelta(seconds=11))
    work.claim(first["id"], "builder-claude", "orchestration", grant["epoch"], now=now - timedelta(seconds=10))
    for transition, offset in (("claim", 9), ("spawn", 8), ("drive", 7), ("bind_artifact", 5)):
        receipts.append(
            first_session, first["id"], "builder-claude", "claude",
            transition, None, [], now=now - timedelta(seconds=offset),
        )
    work.complete(first["id"], "builder-claude", [], now=now - timedelta(seconds=4))
    receipts.append(
        first_session, first["id"], "builder-claude", "claude",
        "complete", None, [], now=now - timedelta(seconds=4),
    )
    second = work.add(
        "verify replay receipts",
        "builder-claude",
        [],
        needs=[first["id"]],
        now=now - timedelta(seconds=3),
    )
    work.claim(second["id"], "builder-claude", "orchestration", grant["epoch"], now=now - timedelta(seconds=2))
    for transition in ("claim", "spawn", "drive", "bind_artifact"):
        receipts.append(
            second_session, second["id"], "builder-claude", "claude",
            transition, None, [], now=now - timedelta(seconds=1),
        )
    work.complete(second["id"], "builder-claude", [], now=now)
    receipts.append(
        second_session, second["id"], "builder-claude", "claude",
        "complete", None, [], now=now,
    )
    artifact = ReplayTimeline.from_root(root).artifact()
    prefix = f"ROOT={root.path}\n$ floati log --root $ROOT --replay --speed 4\n\n"
    animation_frames = [
        prefix + render_replay_frame(artifact, count, width=118, height=30)
        for count in range(1, len(artifact["events"]) + 1)
    ]
    standard = animation_frames[-1]
    plain = (
        f"ROOT={root.path}\n$ floati log --root $ROOT --replay --plain\n\n"
        + render_replay_plain(artifact, width=118)
    )
    result = _write_animated_capture(
        output,
        "flight-recorder-replay",
        standard,
        animation_frames,
        plain=plain,
        duration_ms=250,
    )
    result["root"] = str(root.path)
    result["event_count"] = len(artifact["events"])
    return result


def _onboard_capture(scratch: Path, output: Path) -> dict[str, object]:
    root = _new_root(scratch / "onboard-wizard")
    preview = io.StringIO()
    result = NodeWizard(
        root, RegistryAdminBackend(root), id_factory=uuid7_hex
    ).add_from_keys(["architect-codex", "Codex", "permanent"], preview)
    record = result["records"][0]
    preview_payload = json.loads(preview.getvalue().split("ledger preview: ", 1)[1])
    command = (
        f"ROOT={root.path}\n"
        "$ floati node add --root $ROOT --node architect-codex "
        "--harness Codex --lifetime permanent"
    )
    preview_frame = "\n".join(
        (
            command,
            "",
            "RECORDS PREVIEW — BEFORE WRITE",
            json.dumps(preview_payload, indent=2, sort_keys=True),
        )
    )
    testimony = "\n".join(
        (
            preview_frame,
            "",
            "COMMIT RECEIPT",
            json.dumps(
                {
                    "status": "ok",
                    "node_id": record["node_id"],
                    "state": record["state"],
                    "record_id": record["id"],
                    "workspace": result["workspace"],
                },
                indent=2,
                sort_keys=True,
            ),
        )
    )
    captured = _write_animated_capture(
        output,
        "onboard-wizard",
        testimony,
        [command, preview_frame, testimony],
        duration_ms=1100,
    )
    captured["root"] = str(root.path)
    return captured


def _doctor_capture(
    doctor_root: Path,
    doctor_source: Path,
    output: Path,
) -> dict[str, object]:
    root = FloatiRoot.open_direct_home(doctor_root, create=False)
    doctor = Doctor(doctor_source, root.path, ref="origin/main")
    artifact, _ = doctor.artifact()
    delivery_findings = [
        finding
        for finding in artifact["findings"]
        if finding.get("code") == "delivery_health"
    ]

    stop = threading.Event()
    event_log = EventLog(root)

    def drain_architect() -> None:
        while not stop.wait(0.05):
            event_log.present("architect-codex")

    drainer = threading.Thread(target=drain_architect, daemon=True)
    drainer.start()
    try:
        probe_artifact, _ = doctor.probe(1.0)
    finally:
        stop.set()
        drainer.join(timeout=2.0)

    lines = [
        f"ROOT={root.path}",
        f"SOURCE={doctor_source}",
        "$ floati doctor --root $ROOT --source $SOURCE --probe --probe-budget 1",
        "",
        "DELIVERY HEALTH",
    ]
    for finding in delivery_findings:
        prefix = "! RED" if finding["severity"] == "error" else "+ OK "
        lines.append(f"{prefix}  {finding['detail']}")
    lines.extend(("", "LOOPBACK PROBE"))
    for finding in probe_artifact["findings"]:
        prefix = "+ PASS" if finding["severity"] == "ok" else "! DEAF"
        lines.append(f"{prefix}  {finding['subject']}: {finding['detail']}")
    lines.extend(("", "STATE DEGRADED — one or more delivery paths require attention"))
    captured = _write_capture(output, "doctor-delivery-health", "\n".join(lines))
    captured["root"] = str(root.path)
    captured["probe_budget_seconds"] = 1
    return captured


def _chart_capture(scratch: Path, output: Path) -> dict[str, object]:
    upstream = _new_root(scratch / "harbor-upstream")
    downstream = _new_root(scratch / "harbor-downstream")
    for root, rows in (
        (upstream, (("architect-codex", "Architect"), (_FLOATI_BUILDER, "Codex"))),
        (downstream, (("architect-puddle", "Architect"), (_REVIEWER, "Claude"))),
    ):
        registry = Registry(root)
        for node, harness in rows:
            registry.register(node, harness)
    EventLog(upstream).send(
        "architect-codex", _FLOATI_BUILDER, "floati", "b" * 40,
        "docs/evidence/POST-CAMPAIGN-CAPTURE-SET.md", "DRAFT downstream handoff",
        idempotency_key="capture-chart-upstream",
    )
    EventLog(downstream).send(
        "architect-puddle", _REVIEWER, "puddle", "c" * 40,
        "docs/evidence/POST-CAMPAIGN-CAPTURE-SET.md", "DRAFT frame review",
        idempotency_key="capture-chart-downstream",
    )
    declarations = scratch / "declared-roots.json"
    declarations.write_text(
        json.dumps(
            {
                "schema_version": 0,
                "roots": [
                    {
                        "bus_id": "floati-upstream",
                        "root": str(upstream.path),
                        "architect_node": "architect-codex",
                        "downstream": ["puddle-downstream"],
                    },
                    {
                        "bus_id": "puddle-downstream",
                        "root": str(downstream.path),
                        "architect_node": "architect-puddle",
                        "downstream": [],
                    },
                ],
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    artifact = MultiBusHarborChart(declarations).artifact()
    testimony = (
        f"DECLARED_ROOTS={declarations}\n$ floati chart --declared-roots $DECLARED_ROOTS\n\n"
        + render_multi_bus_chart(artifact)
    )
    captured = _write_capture(output, "harbor-chart-multibus", testimony)
    captured["roots"] = [str(upstream.path), str(downstream.path)]
    captured["declared_roots"] = str(declarations)
    return captured


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scratch", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--doctor-root", type=Path, required=True)
    parser.add_argument("--doctor-source", type=Path, required=True)
    args = parser.parse_args()
    scratch = args.scratch.expanduser().resolve()
    output = args.output.expanduser().resolve()
    scratch.mkdir(parents=True, exist_ok=True)
    output.mkdir(parents=True, exist_ok=True)
    if any(output.iterdir()):
        raise SystemExit(f"output is not empty: {output}")
    captures = [
        _board_capture(scratch, output),
        _replay_capture(scratch, output),
        _onboard_capture(scratch, output),
        _chart_capture(scratch, output),
        _doctor_capture(
            args.doctor_root.expanduser().resolve(),
            args.doctor_source.expanduser().resolve(),
            output,
        ),
    ]
    manifest = {
        "schema_version": 0,
        "generator": "scripts/capture-readme-real-ledgers.py",
        "source_sha": args.source_sha,
        "captured_at": _stamp(datetime.now(timezone.utc)),
        "synthetic": False,
        "scratch": str(scratch),
        "captures": captures,
    }
    manifest_path = output / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({"status": "ok", "manifest": str(manifest_path), "captures": len(captures)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

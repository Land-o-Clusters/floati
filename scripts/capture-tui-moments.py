#!/usr/bin/env python3
"""CAP-4: capture real TUI runs, in the TUI's own palette, into docs/demo/tui/.

Every moment is a real run: real stores on a real scratch root, real commands
under a real terminal where the surface is a command, the real installer and
the real selftest. The palette is the TUI's own (render_frame color=True, or a
real tty for subprocesses). Nothing here is the synthetic wall kit: where the
fleet is a fixture, the caption says fixture; where the command ran, it says
real.
"""

from __future__ import annotations

import argparse
import importlib.util
import io
import json
import os
import pty
import subprocess
import sys
import textwrap
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from PIL import Image

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT))

from floati import fixture_ids  # noqa: E402
from floati.brand import render_buoy_mark  # noqa: E402
from floati.events import EventLog  # noqa: E402
from floati.ids import uuid7_hex  # noqa: E402
from floati.planes import (  # noqa: E402
    AuthorityGrantStore,
    LivenessPresenceStore,
)
from floati.registry import Registry  # noqa: E402
from floati.replay import ReplayTimeline  # noqa: E402
from floati.replay_render import render_replay_frame, render_replay_plain  # noqa: E402
from floati.root import FloatiRoot  # noqa: E402
from floati.tui import model_from_root  # noqa: E402
from floati.tui_render import render_frame, render_plain_dump  # noqa: E402
from floati.work import WorkLog  # noqa: E402
from floati.workers import WorkerReceipts  # noqa: E402

spec = importlib.util.spec_from_file_location(
    "capture_readme_real_ledgers",
    REPOSITORY_ROOT / "scripts/capture-readme-real-ledgers.py",
)
engine = importlib.util.module_from_spec(spec)
spec.loader.exec_module(engine)

BOARD_COMMAND = "$ floati board --root $ROOT --session capture-session --no-animation\n\n"
GRAPH_COMMAND = "$ floati graph --root $ROOT\n\n"
REPLAY_COMMAND = "$ floati log --root $ROOT --replay --speed 4\n\n"

IDLE_CAPTION = "fixture fleet, real run - three registered nodes, an empty bus, nothing in flight"
LIVE_CAPTION = (
    "fixture fleet, real run - each frame re-renders the ledger after real mail, "
    "claims and receipts land; no independent agents are live"
)
DEGRADED_CAPTION = (
    "fixture fleet, real run - one presence lapsed and one lease ran out, and the "
    "board says so"
)
GRAPH_CAPTION = "real run of floati graph on a fixture tenant"
REPLAY_CAPTION = (
    "fixture fleet, real run - the flight recorder replays a real ledger event by event"
)
INSTALL_CAPTION = (
    "real run - the installer executed from the committed tree into a scratch destination"
)
SELFTEST_CAPTION = "real run - python3 -m floati.selftest under a real terminal"


def _pty_run(command: list[str], cwd: Path, timeout: int = 300) -> str:
    """Run one real command under a real terminal and return its colored output.

    The master is drained while the child runs, or a full pty buffer would
    block the child on write while this side blocks on the child.
    """

    master, slave = pty.openpty()
    collected: list[bytes] = []
    import threading

    def drain() -> None:
        while True:
            try:
                chunk = os.read(master, 65536)
            except OSError:
                return
            if not chunk:
                return
            collected.append(chunk)

    reader = threading.Thread(target=drain, daemon=True)
    reader.start()
    try:
        completed = subprocess.run(
            command,
            cwd=str(cwd),
            stdin=slave,
            stdout=slave,
            stderr=slave,
            timeout=timeout,
            env={
                "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
                "PYTHONDONTWRITEBYTECODE": "1",
                "TERM": "xterm-256color",
                "HOME": os.environ.get("HOME", "/var/empty"),
            },
        )
        if completed.returncode != 0:
            raise RuntimeError(f"{command[0]} exited {completed.returncode}")
    finally:
        os.close(slave)
        reader.join(timeout=10)
        os.close(master)
    return b"".join(collected).decode("utf-8", errors="replace")


# The TUI's own palette: every SGR code the board, graph, replay and receipts
# emit, mapped to a distinct colour per theme (xterm-256 values on dark,
# darkened for the light background). The readme engine maps only accent and
# dim; a frame that flattens the TUI's colours is not the TUI's own palette.
TUI_CODE_COLOURS = {
    "38;5;208": ("#ff9f43", "#853d07"),  # buoy accent
    "93": ("#ff9f43", "#853d07"),
    "38;5;42": ("#00d787", "#00875a"),   # live
    "38;5;45": ("#5fd7ff", "#007695"),   # info
    "38;5;214": ("#ffaf00", "#9c6f00"),  # attention
    "38;5;37": ("#00afd7", "#007695"),   # water
    "38;5;240": ("#585858", "#8b929a"),  # frame
    "38;5;245": ("#8a8a8a", "#66717c"),  # dim
    "38;5;252": ("#d0d0d0", "#3b4046"),  # bright text
}


def tui_line_runs(line: str, theme: str) -> list[tuple[str, str]]:
    palette = engine.PALETTES[theme]
    index = 0 if theme == "dark" else 1
    runs: list[tuple[str, str]] = []
    colour = palette["foreground"]
    cursor = 0
    for match in engine.ANSI_COLOR.finditer(line):
        if match.start() > cursor:
            runs.append((line[cursor: match.start()], colour))
        code = match.group(1)
        if code in {"", "0"}:
            colour = palette["foreground"]
        else:
            colour = TUI_CODE_COLOURS.get(
                code, (palette["foreground"], palette["foreground"])
            )[index]
        cursor = match.end()
    if cursor < len(line):
        runs.append((line[cursor:], colour))
    return runs or [("", colour)]


def tui_terminal_frame(
    testimony: str,
    theme: str,
    *,
    font_path: Path,
) -> Image.Image:
    from PIL import ImageDraw, ImageFont

    palette = engine.PALETTES[theme]
    image = Image.new("RGB", (1600, 900), palette["background"])
    draw = ImageDraw.Draw(image)
    font = ImageFont.truetype(io.BytesIO(font_path.read_bytes()), 20)
    x_margin = 30
    y = 26
    line_height = 25
    for line in testimony.rstrip().splitlines():
        if y + line_height > image.height - 26:
            break
        x = x_margin
        for piece, colour in tui_line_runs(line, theme):
            draw.text((x, y), piece, font=font, fill=colour)
            x += round(draw.textlength(piece, font=font))
        y += line_height
    return image


def _static_capture(
    output: Path,
    name: str,
    standard: str,
    *,
    caption: str,
    font_path: Path,
    instrument_roots: tuple[Path, ...],
) -> dict[str, object]:
    exposed = engine.expose_readme_text(standard, instrument_roots=instrument_roots)
    engine._assert_rendered_identity_safe(name, [exposed])
    text_path = output / f"{name}-standard.txt"
    text_path.write_text(exposed.rstrip() + "\n", encoding="utf-8")
    files = [{"path": text_path.name, "sha256": engine._digest(text_path),
              "bytes": text_path.stat().st_size}]
    for theme in ("dark", "light"):
        frame = tui_terminal_frame(exposed, theme, font_path=font_path)
        png_path = output / f"{name}-{theme}.png"
        frame.save(png_path, format="PNG")
        files.append({"path": png_path.name, "sha256": engine._digest(png_path),
                      "bytes": png_path.stat().st_size})
    return {
        "name": name,
        "real_ledger": True,
        "animated": False,
        "frame_count": 1,
        "caption": caption,
        "files": files,
    }


def _animated_capture(
    output: Path,
    name: str,
    standard: str,
    animation_frames: list[str],
    *,
    caption: str,
    duration_ms: int,
    font_path: Path,
    instrument_roots: tuple[Path, ...],
) -> dict[str, object]:
    exposed = engine.expose_readme_text(standard, instrument_roots=instrument_roots)
    exposed_frames = [
        engine.expose_readme_text(frame, instrument_roots=instrument_roots)
        for frame in animation_frames
    ]
    engine._assert_rendered_identity_safe(name, exposed_frames)
    text_path = output / f"{name}-standard.txt"
    text_path.write_text(exposed.rstrip() + "\n", encoding="utf-8")
    files = [{"path": text_path.name, "sha256": engine._digest(text_path),
              "bytes": text_path.stat().st_size}]
    for theme in ("dark", "light"):
        rendered = [
            tui_terminal_frame(frame, theme, font_path=font_path)
            for frame in exposed_frames
        ]
        durations = [duration_ms] * len(rendered)
        durations[-1] = max(1200, duration_ms)
        gif_path = output / f"{name}-{theme}.gif"
        engine._write_gif(gif_path, rendered, durations)
        files.append({"path": gif_path.name, "sha256": engine._digest(gif_path),
                      "bytes": gif_path.stat().st_size})
    return {
        "name": name,
        "real_ledger": True,
        "animated": True,
        "frame_count": len(animation_frames),
        "caption": caption,
        "files": files,
    }


def _seed_nodes(root: FloatiRoot, moment: datetime) -> None:
    registry = Registry(root)
    for node, harness in (
        ("architect-codex", "Architect"),
        ("builder-claude", "Claude"),
        ("reviewer-opencode", "OpenCode"),
    ):
        registry.register(node, harness)


def _board_idle_capture(
    scratch: Path, output: Path, moment: datetime, font_path: Path
) -> dict[str, object]:
    root = engine._new_root(scratch / "board-idle")
    _seed_nodes(root, moment)
    model = model_from_root(root, moment)
    standard = (
        f"ROOT={root.path}\n{BOARD_COMMAND}"
        + render_frame(model, 108, 30, selected=0, color=True)
    )
    return _static_capture(
        output, "board-idle", standard,
        caption=IDLE_CAPTION, font_path=font_path, instrument_roots=(scratch,),
    )


def _board_live_capture(
    scratch: Path, output: Path, moment: datetime, font_path: Path,
    id_factory: Callable[[], str],
) -> dict[str, object]:
    root = engine._new_root(scratch / "board-live")
    _seed_nodes(root, moment)
    work = WorkLog(root)
    frames = []
    step = timedelta(seconds=3)
    authority = AuthorityGrantStore(root).claim(
        "capture-live", "architect-codex", 3600, 3600, moment
    )
    pending = work.add(
        "carry the live capture", "architect-codex", [], now=moment
    )
    for index in range(8):
        now = moment + index * step
        if index == 2:
            work.claim(
                pending["id"], "architect-codex", "capture-live",
                authority["epoch"], now=now,
            )
        if index == 4:
            EventLog(root).send(
                "architect-codex", "builder-claude", "floati", "a" * 40,
                "docs/demo/tui/manifest.json", "live frame: take the next slice",
                idempotency_key=f"capture-live-{index}", now=now,
            )
        if index == 5:
            receipts = WorkerReceipts(root)
            session = "worker-" + id_factory()
            for transition, offset in (
                ("claim", 1), ("spawn", 1), ("drive", 0),
            ):
                receipts.append(
                    session, pending["id"], "architect-codex", "codex",
                    transition, None, [], now=now - timedelta(seconds=offset),
                )
        if index == 6:
            work.complete(pending["id"], "architect-codex", [], now=now)
            EventLog(root).send(
                "builder-claude", "reviewer-opencode", "floati", "a" * 40,
                "docs/demo/tui/manifest.json", "live frame: ready for review",
                idempotency_key=f"capture-live-{index}", now=now,
            )
        model = model_from_root(root, now)
        frames.append(
            f"ROOT={root.path}\n{BOARD_COMMAND}"
            + render_frame(model, 108, 30, selected=0, color=True)
        )
    return _animated_capture(
        output, "board-live", frames[-1], frames,
        caption=LIVE_CAPTION, duration_ms=420, font_path=font_path,
        instrument_roots=(scratch,),
    )


def _board_degraded_capture(
    scratch: Path, output: Path, moment: datetime, font_path: Path
) -> dict[str, object]:
    root = engine._new_root(scratch / "board-degraded")
    _seed_nodes(root, moment)
    liveness = LivenessPresenceStore(root)
    liveness.observe("architect-codex", 3600, moment - timedelta(seconds=2))
    liveness.observe("builder-claude", 60, moment - timedelta(minutes=9))
    liveness.observe("reviewer-opencode", 3600, moment - timedelta(seconds=6))
    authority = AuthorityGrantStore(root).claim(
        "capture-degraded", "architect-codex", 60, 60, moment - timedelta(minutes=30)
    )
    work = WorkLog(root)
    stalled = work.add("stalled without a witness", "architect-codex", [], now=moment - timedelta(minutes=31))
    work.claim(
        stalled["id"], "architect-codex", "capture-degraded",
        authority["epoch"], now=moment - timedelta(minutes=30),
    )
    model = model_from_root(root, moment)
    standard = (
        f"ROOT={root.path}\n{BOARD_COMMAND}"
        + render_frame(model, 108, 30, selected=0, color=True)
    )
    return _static_capture(
        output, "board-degraded", standard,
        caption=DEGRADED_CAPTION, font_path=font_path, instrument_roots=(scratch,),
    )


def _graph_capture(
    scratch: Path, output: Path, moment: datetime, font_path: Path
) -> dict[str, object]:
    root = engine._new_root(scratch / "graph")
    _seed_nodes(root, moment)
    standard = _pty_run(
        ["python3", "-m", "floati", "graph", "--root", str(root.path)], REPOSITORY_ROOT
    )
    standard = f"ROOT={root.path}\n{GRAPH_COMMAND}" + standard
    return _static_capture(
        output, "graph", standard,
        caption=GRAPH_CAPTION, font_path=font_path, instrument_roots=(scratch,),
    )


def _replay_capture(
    scratch: Path, output: Path, moment: datetime, font_path: Path,
    id_factory: Callable[[], str],
) -> dict[str, object]:
    root = engine._new_root(scratch / "replay")
    _seed_nodes(root, moment)
    work = WorkLog(root)
    receipts = WorkerReceipts(root)
    grant = AuthorityGrantStore(root).claim(
        "capture-replay", "builder-claude", 3600, 3600, moment - timedelta(seconds=14)
    )
    session = "worker-" + id_factory()
    item = work.add("rebuild the ledger out loud", "builder-claude", [], now=moment - timedelta(seconds=13))
    work.claim(item["id"], "builder-claude", "capture-replay", grant["epoch"], now=moment - timedelta(seconds=12))
    for transition, offset in (
        ("claim", 11), ("spawn", 10), ("drive", 9), ("bind_artifact", 7),
    ):
        receipts.append(
            session, item["id"], "builder-claude", "claude",
            transition, None, [], now=moment - timedelta(seconds=offset),
        )
    work.complete(item["id"], "builder-claude", [], now=moment - timedelta(seconds=5))
    receipts.append(
        session, item["id"], "builder-claude", "claude",
        "complete", None, [], now=moment - timedelta(seconds=5),
    )
    for offset in (4, 3):
        EventLog(root).send(
            "builder-claude", "reviewer-opencode", "floati", "a" * 40,
            "docs/demo/tui/manifest.json", f"replay frame at minus {offset}s",
            idempotency_key=f"capture-replay-{offset}", now=moment - timedelta(seconds=offset),
        )
    artifact = ReplayTimeline.from_root(root).artifact()
    animation_frames = [
        f"ROOT={root.path}\n{REPLAY_COMMAND}"
        + render_replay_frame(artifact, count, width=118, height=30)
        for count in range(1, len(artifact["events"]) + 1)
    ]
    standard = animation_frames[-1]
    return _animated_capture(
        output, "replay-in-flight", standard, animation_frames,
        caption=REPLAY_CAPTION, duration_ms=280, font_path=font_path,
        instrument_roots=(scratch,),
    )


def _install_capture(
    scratch: Path, output: Path, moment: datetime, font_path: Path
) -> dict[str, object]:
    base = scratch / "install-run"
    base.mkdir(parents=True, exist_ok=False)
    destination = base / "installed"
    source = base / "source"
    head = subprocess.run(
        ["/usr/bin/git", "rev-parse", "HEAD"],
        cwd=str(REPOSITORY_ROOT), check=True, capture_output=True, text=True,
    ).stdout.strip()
    subprocess.run(
        ["/usr/bin/git", "clone", "--shared", "--no-checkout",
         str(REPOSITORY_ROOT), str(source)],
        check=True, capture_output=True, text=True,
    )
    subprocess.run(
        ["/usr/bin/git", "checkout", "--detach", head],
        cwd=str(source), check=True, capture_output=True, text=True,
    )
    completed = subprocess.run(
        [
            str(source / "scripts" / "floati"),
            "install", "--source", str(source), "--destination", str(destination),
            "--committed-tree",
        ],
        cwd=str(REPOSITORY_ROOT),
        env={
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            "PYTHONDONTWRITEBYTECODE": "1",
            "HOME": os.environ.get("HOME", "/var/empty"),
        },
        capture_output=True, text=True,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"install failed: {completed.stderr.strip()}")
    receipt = completed.stdout.strip()
    logical = receipt.replace(',"', ',\n"').splitlines()
    wrapped = [
        piece
        for line in logical
        for piece in textwrap.wrap(
            line, width=108, break_long_words=True, break_on_hyphens=False
        )
    ]
    receipt_view = "\n".join([*wrapped[:3], "  [...]", *wrapped[-10:]]) + "\n"
    standard = (
        "$ floati install --source <repo> --destination <scratch>\n\n"
        + render_buoy_mark(color=True)
        + "\n"
        + receipt_view
    )
    return _static_capture(
        output, "install-moment", standard,
        caption=INSTALL_CAPTION, font_path=font_path, instrument_roots=(scratch,),
    )


def _selftest_capture(
    scratch: Path, output: Path, moment: datetime, font_path: Path
) -> dict[str, object]:
    base = scratch / "selftest-run"
    base.mkdir(parents=True, exist_ok=False)
    source = base / "source"
    head = subprocess.run(
        ["/usr/bin/git", "rev-parse", "HEAD"],
        cwd=str(REPOSITORY_ROOT), check=True, capture_output=True, text=True,
    ).stdout.strip()
    subprocess.run(
        ["/usr/bin/git", "clone", "--shared", "--no-checkout",
         str(REPOSITORY_ROOT), str(source)],
        check=True, capture_output=True, text=True,
    )
    subprocess.run(
        ["/usr/bin/git", "checkout", "--detach", head],
        cwd=str(source), check=True, capture_output=True, text=True,
    )
    completed = subprocess.run(
        [sys.executable, "-m", "floati.selftest"],
        cwd=str(source),
        env={
            **os.environ,
            "PYTHONDONTWRITEBYTECODE": "1",
        },
        capture_output=True, text=True, timeout=2400,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"selftest exited {completed.returncode}: {completed.stderr[-400:]}"
        )
    verified = next(
        line for line in completed.stdout.splitlines() if '"status"' in line
    )
    standard = (
        "$ python3 -m floati.selftest\n\n"
        "  [the full suite runs; its last line is the receipt]\n\n"
        + render_buoy_mark(color=True)
        + "\n"
        + verified
        + "\n"
    )
    return _static_capture(
        output, "selftest", standard,
        caption=SELFTEST_CAPTION, font_path=font_path, instrument_roots=(scratch,),
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--font", type=Path)
    parser.add_argument("--captured-at", type=engine._capture_moment)
    parser.add_argument("--scratch", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument(
        "--capture",
        action="append",
        choices=(
            "board-idle", "board-live", "board-degraded", "graph",
            "replay-in-flight", "install-moment", "selftest",
        ),
        help="capture only this named moment; repeat to select multiple",
    )
    args = parser.parse_args()
    try:
        font_path = engine.resolve_capture_font(args.font)
    except engine.ProtocolRefusal as refusal:
        if refusal.code not in (
            engine.FONT_ABSENT_CODE, engine.FONT_DECLARATION_INVALID_CODE
        ):
            raise
        print(json.dumps(engine.font_absence_report(refusal), sort_keys=True), file=sys.stderr)
        return engine.FONT_ABSENT_EXIT_CODE
    captured_at = args.captured_at or datetime.now(timezone.utc)
    scratch = args.scratch.expanduser().resolve()
    output = args.output.expanduser().resolve()
    scratch.mkdir(parents=True, exist_ok=True)
    output.mkdir(parents=True, exist_ok=True)
    if any(output.iterdir()):
        raise SystemExit(f"output is not empty: {output}")
    requested = args.capture or [
        "board-idle", "board-live", "board-degraded", "graph",
        "replay-in-flight", "install-moment", "selftest",
    ]
    captures = []
    for name in requested:
        with engine._capture_id_scope(name, captured_at) as id_factory:
            if name == "board-idle":
                captures.append(
                    _board_idle_capture(scratch, output, captured_at, font_path)
                )
            elif name == "board-live":
                captures.append(
                    _board_live_capture(
                        scratch, output, captured_at, font_path, id_factory
                    )
                )
            elif name == "board-degraded":
                captures.append(
                    _board_degraded_capture(scratch, output, captured_at, font_path)
                )
            elif name == "graph":
                captures.append(
                    _graph_capture(scratch, output, captured_at, font_path)
                )
            elif name == "replay-in-flight":
                captures.append(
                    _replay_capture(
                        scratch, output, captured_at, font_path, id_factory
                    )
                )
            elif name == "install-moment":
                captures.append(
                    _install_capture(scratch, output, captured_at, font_path)
                )
            else:
                captures.append(
                    _selftest_capture(scratch, output, captured_at, font_path)
                )
    manifest = {
        "schema_version": 0,
        "generator": "scripts/capture-tui-moments.py",
        "source_sha": args.source_sha,
        "captured_at": engine._stamp(captured_at),
        "synthetic": False,
        "scratch": str(scratch),
        "captures": captures,
    }
    manifest_path = output / "manifest.json"
    manifest_path.write_text(
        engine.expose_readme_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            instrument_roots=(scratch,),
        ),
        encoding="utf-8",
    )
    print(json.dumps({
        "status": "ok", "manifest": str(manifest_path), "captures": len(captures),
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

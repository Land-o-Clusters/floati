#!/usr/bin/env python3
"""MK-D2: the failure demonstration, captured the way every README capture is.

One real run on a real scratch root with a real ledger: a worker seat dies
mid-run (its presence lapses with the claim still held and mail still
undelivered), `floati board` names the stalled claim and its holder, and a
real `floati doctor` names the undelivered count and its age. Dark and light,
one outcome, no sound; frames carry no baked text and host paths are redacted
at exposure. The limit line rides in the caption verbatim:

    The doctor reports what is missing and how old it is. It does not say
    why the seat died.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import pty
import shutil
import subprocess
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT))


def _load(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, REPOSITORY_ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


engine = _load("capture_readme_real_ledgers", "scripts/capture-readme-real-ledgers.py")
moments = _load("capture_tui_moments", "scripts/capture-tui-moments.py")

from floati.events import EventLog  # noqa: E402
from floati.planes import AuthorityGrantStore, LivenessPresenceStore  # noqa: E402
from floati.registry import Registry  # noqa: E402
from floati.root import FloatiRoot  # noqa: E402
from floati.tui import model_from_root  # noqa: E402
from floati.tui_render import render_frame  # noqa: E402
from floati.work import WorkLog  # noqa: E402
from floati.workers import WorkerReceipts  # noqa: E402

BOARD_COMMAND = "$ floati board --root $ROOT --session capture-session --no-animation\n\n"
DOCTOR_COMMAND_PREFIX = "$ floati doctor --root $ROOT --source <repository>\n\n"
LIMIT_LINE = (
    "The doctor reports what is missing and how old it is. "
    "It does not say why the seat died."
)
CAPTION = (
    "fixture fleet, real run - a worker dies mid-run with a claimed row and "
    "undelivered mail; " + LIMIT_LINE
)
BOARD_FRAMES = 6
DOCTOR_FRAMES = 2
FRAME_MS = 1500


def _pty_text(command: list[str], cwd: Path, tolerated: set[int]) -> str:
    """One real command under a real terminal; tolerated exits keep the text.

    `doctor` exits 35 (degraded) precisely when it has findings to name -
    that exit IS the demonstration, so the frame keeps what it said.
    """

    master, slave = pty.openpty()
    collected: list[bytes] = []

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
    completed = subprocess.run(
        command,
        cwd=cwd,
        stdout=slave,
        stderr=slave,
        timeout=300,
        env={
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            "PYTHONDONTWRITEBYTECODE": "1",
            "TERM": "xterm-256color",
            "HOME": os.environ.get("HOME", "/var/empty"),
        },
    )
    os.close(slave)
    reader.join(timeout=10)
    os.close(master)
    if completed.returncode not in tolerated:
        raise RuntimeError(
            f"{command[0]} exited {completed.returncode}, tolerated {sorted(tolerated)}"
        )
    return b"".join(collected).decode("utf-8", errors="replace")


def _seed_die_mid_run(root: FloatiRoot, moment: datetime, id_factory) -> None:
    """The real ledger of a worker dying mid-run, stamped for real ages."""

    registry = Registry(root)
    for node, harness in (
        ("architect-codex", "Architect"),
        ("builder-claude", "Claude"),
        ("reviewer-opencode", "OpenCode"),
    ):
        registry.register(node, harness)
    liveness = LivenessPresenceStore(root)
    liveness.observe("architect-codex", 3600, moment - timedelta(seconds=2))
    liveness.observe("builder-claude", 60, moment - timedelta(minutes=10))
    liveness.observe("reviewer-opencode", 3600, moment - timedelta(seconds=6))
    authority = AuthorityGrantStore(root).claim(
        "capture-mk-d2", "builder-claude", 3600, 3600, moment - timedelta(minutes=32)
    )
    work = WorkLog(root)
    row = work.add(
        "carry the stalled slice", "architect-codex", [],
        now=moment - timedelta(minutes=32),
    )
    work.claim(
        row["id"], "builder-claude", "capture-mk-d2",
        authority["epoch"], now=moment - timedelta(minutes=31),
    )
    receipts = WorkerReceipts(root)
    session = "worker-" + id_factory()
    for transition, offset in (("claim", 2), ("spawn", 1), ("drive", 0)):
        receipts.append(
            session, row["id"], "builder-claude", "claude",
            transition, None, [], now=moment - timedelta(minutes=31, seconds=offset),
        )
    # The architect hands the worker its next slice; the worker is already
    # gone, so nothing ever drains it - this is the undelivered mail the
    # doctor counts and ages.
    EventLog(root).send(
        "architect-codex", "builder-claude", "floati", "a" * 40,
        "docs/demo/mk-d2/manifest.json", "next slice: the queue moved",
        idempotency_key="capture-mk-d2-undelivered",
        now=moment - timedelta(minutes=31),
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--font", type=Path)
    parser.add_argument("--captured-at", type=engine._capture_moment)
    parser.add_argument("--scratch", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--source-sha", required=True)
    args = parser.parse_args()
    try:
        font_path = engine.resolve_capture_font(args.font)
    except engine.ProtocolRefusal as refusal:
        if refusal.code not in (
            engine.FONT_ABSENT_CODE, engine.FONT_DECLARATION_INVALID_CODE
        ):
            raise
        print(
            json.dumps(engine.font_absence_report(refusal), sort_keys=True),
            file=sys.stderr,
        )
        return engine.FONT_ABSENT_EXIT_CODE
    captured_at = args.captured_at or datetime.now(timezone.utc)
    scratch = args.scratch.expanduser().resolve()
    output = args.output.expanduser().resolve()
    scratch.mkdir(parents=True, exist_ok=True)
    output.mkdir(parents=True, exist_ok=True)
    if any(output.iterdir()):
        raise SystemExit(f"output is not empty: {output}")

    root = engine._new_root(scratch / "mk-d2")
    # The doctor reads its source bundle from a COPY of the tree under the
    # scratch, so every path the real command prints is an instrument root
    # and is redacted at exposure - the host checkout path never reaches a
    # frame.
    source_tree = scratch / "floati-source"
    shutil.copytree(
        REPOSITORY_ROOT,
        source_tree,
        ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc", ".zcode"),
    )
    with engine._capture_id_scope("mk-d2", captured_at) as id_factory:
        _seed_die_mid_run(root, captured_at, id_factory)

        frames: list[str] = []
        step = timedelta(seconds=90)
        for index in range(BOARD_FRAMES):
            now = captured_at + index * step
            model = model_from_root(root, now)
            frames.append(
                f"ROOT={root.path}\n{BOARD_COMMAND}"
                + render_frame(model, 108, 30, selected=0, color=True)
            )
        doctor_output = _pty_text(
            [
                sys.executable, "-m", "floati", "doctor",
                "--root", str(root.path),
                "--source", str(source_tree),
            ],
            cwd=REPOSITORY_ROOT,
            tolerated={0, 35},
        )
        for _ in range(DOCTOR_FRAMES):
            frames.append(
                f"ROOT={root.path}\n{DOCTOR_COMMAND_PREFIX}" + doctor_output
            )

        exposed = [
            engine.expose_readme_text(frame, instrument_roots=(scratch,))
            for frame in frames
        ]
        engine._assert_rendered_identity_safe("mk-d2", exposed)
        text_path = output / "mk-d2-standard.txt"
        text_path.write_text(exposed[-1].rstrip() + "\n", encoding="utf-8")
        files = [
            {
                "path": text_path.name,
                "sha256": engine._digest(text_path),
                "bytes": text_path.stat().st_size,
            }
        ]
        for theme in ("dark", "light"):
            rendered = [
                moments.tui_terminal_frame(frame, theme, font_path=font_path)
                for frame in exposed
            ]
            durations = [FRAME_MS] * len(rendered)
            durations[-1] = max(3000, FRAME_MS)
            gif_path = output / f"mk-d2-{theme}.gif"
            engine._write_gif(gif_path, rendered, durations)
            files.append(
                {
                    "path": gif_path.name,
                    "sha256": engine._digest(gif_path),
                    "bytes": gif_path.stat().st_size,
                }
            )

    manifest = {
        "schema_version": 0,
        "generator": "scripts/capture-mk-d2.py",
        "source_sha": args.source_sha,
        "captured_at": engine._stamp(captured_at),
        "synthetic": False,
        "scratch": str(scratch),
        "captures": [
            {
                "name": "mk-d2",
                "real_ledger": True,
                "animated": True,
                "frame_count": len(frames),
                "caption": CAPTION,
                "limit_line": LIMIT_LINE,
                "files": files,
            }
        ],
    }
    manifest_path = output / "manifest.json"
    manifest_path.write_text(
        engine.expose_readme_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            instrument_roots=(scratch,),
        ),
        encoding="utf-8",
    )
    seconds = len(frames) * FRAME_MS / 1000
    print(
        json.dumps(
            {
                "status": "ok",
                "manifest": str(manifest_path),
                "frames": len(frames),
                "seconds": seconds,
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

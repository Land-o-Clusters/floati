#!/usr/bin/env python3
"""MK-D3: the transition demonstration, captured the way every README
capture is.

One real run on a real scratch root with a real ledger: a seat's session
claims a slice, spawns, drives, and ENDS mid-slice - a real degrade
receipt, the work still claimed for the next session. `floati log
--replay` then plays the recorded events back in order to `REPLAY
COMPLETE`, finishing nothing. Dark and light, one outcome, no sound;
frames carry no baked text and host paths are redacted at exposure. The
limit line rides in the caption verbatim:

    Replay reconstructs what was recorded, in order. It does not finish
    work that was interrupted.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import subprocess
import sys
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

from floati.replay import ReplayTimeline  # noqa: E402
from floati.replay_render import render_replay_frame  # noqa: E402
from floati.work import WorkLog  # noqa: E402
from floati.workers import WorkerReceipts  # noqa: E402
from floati.planes import AuthorityGrantStore  # noqa: E402
from floati.registry import Registry  # noqa: E402
from floati.root import FloatiRoot  # noqa: E402

REPLAY_COMMAND = "$ floati log --root $ROOT --replay\n\n"
PLAIN_COMMAND = "$ floati log --root $ROOT --replay --plain\n\n"
LIMIT_LINE = (
    "Replay reconstructs what was recorded, in order. "
    "It does not finish work that was interrupted."
)
CAPTION = (
    "fixture fleet, real run - a seat's session ends mid-slice and the "
    "flight recorder plays every recorded event back in order, finishing "
    "nothing; " + LIMIT_LINE
)
FRAME_MS = 1500
PLAIN_FRAMES = 2
FRAME_WIDTH = 112
FRAME_HEIGHT = 30


def _seed_session_end(root: FloatiRoot, moment: datetime, id_factory) -> None:
    """The real ledger of one seat's session, from claim to its end.

    Every row is written by the shipped writer a real session uses, with
    real offsets so the replay's elapsed timeline reads naturally.
    """

    registry = Registry(root)
    for node, harness in (
        ("architect-codex", "Architect"),
        ("builder-claude", "Claude"),
    ):
        registry.register(node, harness)
    base = moment - timedelta(minutes=6)
    authority = AuthorityGrantStore(root).claim(
        "capture-mk-d3", "builder-claude", 3600, 3600, base
    )
    work = WorkLog(root)
    row = work.add(
        "carry the handover slice", "architect-codex", [],
        now=base,
    )
    work.claim(
        row["id"], "builder-claude", "capture-mk-d3",
        authority["epoch"], now=base + timedelta(seconds=30),
    )
    receipts = WorkerReceipts(root)
    session = "worker-" + id_factory()

    def receipt(transition, offset, bindings=()):
        receipts.append(
            session, row["id"], "builder-claude", "claude",
            transition, None, list(bindings), now=base + timedelta(seconds=offset),
        )

    receipt("claim", 31)
    receipt("spawn", 35)
    receipt("drive", 50)
    # the session ENDS here: a real degrade receipt, the work still
    # claimed for the next session - the replay reconstructs all of it
    # and finishes none of it
    receipts.append(
        session, row["id"], "builder-claude", "claude",
        "degrade", "process_cancelled", [], now=base + timedelta(seconds=62),
    )


def _plain_transcript(root: FloatiRoot) -> str:
    """The real `log --replay --plain` stderr: the copyable transcript."""

    completed = subprocess.run(
        [
            sys.executable, "-m", "floati", "log",
            "--root", str(root.path), "--replay", "--plain",
        ],
        cwd=REPOSITORY_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=300,
        env={
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            "PYTHONDONTWRITEBYTECODE": "1",
            "TERM": "xterm-256color",
            "HOME": os.environ.get("HOME", "/var/empty"),
        },
    )
    if completed.returncode != 0:
        raise RuntimeError(f"log --replay --plain exited {completed.returncode}")
    return completed.stderr.decode("utf-8", errors="replace")


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

    root = engine._new_root(scratch / "mk-d3")
    with engine._capture_id_scope("mk-d3", captured_at) as id_factory:
        _seed_session_end(root, captured_at, id_factory)

        artifact = ReplayTimeline.from_root(root).artifact()
        events = list(artifact["events"])
        if not events:
            raise SystemExit("the seeded ledger produced no replay events")

        frames: list[str] = []
        # the shipped replay surface, played forward one event per frame,
        # the same idiom as the hero capture
        for count in range(1, len(events) + 1):
            frames.append(
                f"ROOT={root.path}\n{REPLAY_COMMAND}"
                + render_replay_frame(
                    artifact, count, width=FRAME_WIDTH, height=FRAME_HEIGHT
                )
            )
        plain = _plain_transcript(root)
        if "REPLAY COMPLETE" not in plain:
            raise SystemExit("the plain replay never reached REPLAY COMPLETE")
        for _ in range(PLAIN_FRAMES):
            frames.append(f"ROOT={root.path}\n{PLAIN_COMMAND}" + plain)

        exposed = [
            engine.expose_readme_text(frame, instrument_roots=(scratch,))
            for frame in frames
        ]
        engine._assert_rendered_identity_safe("mk-d3", exposed)
        text_path = output / "mk-d3-standard.txt"
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
            gif_path = output / f"mk-d3-{theme}.gif"
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
        "generator": "scripts/capture-mk-d3.py",
        "source_sha": args.source_sha,
        "captured_at": engine._stamp(captured_at),
        "synthetic": False,
        "scratch": str(scratch),
        "captures": [
            {
                "name": "mk-d3",
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

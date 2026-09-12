#!/usr/bin/env python3
"""MK-D1: the handoff demonstration, captured the way every README capture is.

One real run on a real scratch root with a real ledger: a Claude Code seat
sends a row to a Codex seat, the Codex seat drains and acks in one command
(the drain artifact carries the delivery receipt and the acknowledgment
receipt), `floati receipts` prints the receiver's delivered and acknowledged
history, and then one malformed envelope is refused with its typed code. Dark
and light, one outcome, no sound; frames carry no baked text and host paths
are redacted at exposure. The limit line rides in the caption verbatim:

    The receiver acknowledged when it next drained. This recording does not
    show a seat waking on its own.
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

import floati.cli as cli  # noqa: E402
import floati.cursor as cursor_module  # noqa: E402
import floati.events as events_module  # noqa: E402
import floati.registry as registry_module  # noqa: E402
import argparse as argparse_module  # noqa: E402
from floati.registry import Registry  # noqa: E402

LIMIT_LINE = (
    "The receiver acknowledged when it next drained. "
    "This recording does not show a seat waking on its own."
)
CAPTION = (
    "fixture fleet, real run - a Claude Code seat sends a row to a Codex seat, "
    "the Codex seat drains and acks, receipts names the delivery and the "
    "acknowledgment, and one malformed envelope is refused; " + LIMIT_LINE
)
SENDER = "claude-sender"
RECEIVER = "codex-receiver"
RECEIVER_SESSION = "receiver-session"
HANDOFF_DOC = "docs/demo/recipes/mk-d1.md"
HANDOFF_NOTE = "row one: the handoff slice"
BANKED_SHA = "0" * 40
FRAME_MS = 3000
FINAL_MS = 4000
WRAP_COLUMNS = 110

SEND_COMMAND = (
    f"$ floati send --root $ROOT --from {SENDER} --to {RECEIVER} --repo floati \\\n"
    f"      --sha {BANKED_SHA} --doc {HANDOFF_DOC} --note \"{HANDOFF_NOTE}\"\n\n"
)
INBOX_COMMAND = (
    f"$ floati inbox --root $ROOT --as {RECEIVER} --session {RECEIVER_SESSION}\n\n"
)
RECEIPTS_COMMAND_PREFIX = f"$ floati receipts {RECEIVER} --root $ROOT\n\n"
REFUSED_SEND_COMMAND = (
    f"$ floati send --root $ROOT --from {SENDER} --to {RECEIVER} --repo floati \\\n"
    f"      --sha 00000 --doc {HANDOFF_DOC} --note \"{HANDOFF_NOTE}\"\n\n"
)


_PINNED_ATTRIBUTES: list = []


def _pin_capture_clock(moment: datetime, id_factory) -> None:
    """Pin the wall clock and the id factory the mail plane reads, so every
    envelope and receipt is an exact function of --captured-at.

    The frozen datetime is a real subclass returning real subclass instances:
    the cursor validates `isinstance(current, datetime)` in its own module
    namespace, so a plain datetime from a foreign class would be refused.
    The cursor mints acknowledgment ids through its own module binding, which
    the shared capture id scope does not list, so it is pinned here too.
    """

    stamp = moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            if tz is not None:
                return cls.fromtimestamp(moment.timestamp(), tz)
            return cls.fromtimestamp(moment.timestamp())

    pins = (
        (events_module, "utc_now", lambda: stamp),
        (registry_module, "utc_now", lambda: stamp),
        (cursor_module, "datetime", FrozenDatetime),
        (cursor_module, "uuid7_hex", id_factory),
    )
    for module, name, replacement in pins:
        _PINNED_ATTRIBUTES.append((module, name, getattr(module, name)))
        setattr(module, name, replacement)


def _restore_capture_clock() -> None:
    while _PINNED_ATTRIBUTES:
        module, name, original = _PINNED_ATTRIBUTES.pop()
        setattr(module, name, original)


def _pty_text(command: list[str], cwd: Path, tolerated: set[int]) -> str:
    """One real command under a real terminal; tolerated exits keep the text.

    The refused send exits 20 precisely because the envelope is malformed -
    that typed refusal IS the demonstration, so the frame keeps what it said.
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


def _artifact_text(command: str, status: str, evidence: dict) -> str:
    """Serialize one handler result exactly the way the CLI prints it."""

    artifact = {
        "artifact_version": 0,
        "command": command,
        "status": status,
        "evidence": evidence,
    }
    return json.dumps(
        artifact, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def _wrap_line(line: str, width: int) -> list[str]:
    """Greedy hard wrap, the way a terminal of this width would break it."""

    if len(line) <= width:
        return [line]
    wrapped = []
    while len(line) > width:
        wrapped.append(line[:width])
        line = line[width:]
    if line:
        wrapped.append(line)
    return wrapped


def _wrap(text: str, width: int = WRAP_COLUMNS) -> str:
    return "\n".join(
        "\n".join(_wrap_line(line, width)) for line in text.splitlines()
    )


def _frame(root_path: Path, command: str, output: str) -> str:
    """The raw frame: real path, real one-line artifacts, no wrapping yet.

    Exposure runs BEFORE any wrapping - the redactor matches one contiguous
    scratch spelling, and a path broken across lines could not be replaced.
    """

    return f"ROOT={root_path}\n\n{command}{output}\n"


def _seed_handoff(root, moment: datetime) -> tuple[str, str]:
    """The real handoff, performed by the CLI's own handlers at the captured
    moment: one send, one drain that presents and acknowledges."""

    with engine._capture_id_scope("mk-d1", moment) as id_factory:
        _pin_capture_clock(moment, id_factory)
        try:
            Registry(root).register(SENDER, "claude")
            Registry(root).register(RECEIVER, "codex")

            status, evidence, _ = cli._send(
                argparse_module.Namespace(
                    root=str(root.path),
                    sender=SENDER,
                    recipient=RECEIVER,
                    repo="floati",
                    sha=BANKED_SHA,
                    doc=HANDOFF_DOC,
                    note=HANDOFF_NOTE,
                    reply_to=None,
                    idempotency_key=None,
                    claim=None,
                )
            )
            if status != "ok":
                raise RuntimeError(f"handoff send did not clear: {status}")
            send_artifact = _artifact_text("send", status, evidence)

            status, evidence, _ = cli._inbox(
                argparse_module.Namespace(
                    root=str(root.path),
                    recipient=RECEIVER,
                    session=RECEIVER_SESSION,
                    peek=False,
                )
            )
            if status != "ok" or not evidence.get("acknowledgment"):
                raise RuntimeError(f"handoff drain did not acknowledge: {status}")
            inbox_artifact = _artifact_text("inbox", status, evidence)
            return send_artifact, inbox_artifact
        finally:
            _restore_capture_clock()


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

    root = engine._new_root(scratch / "fleet")
    send_artifact, inbox_artifact = _seed_handoff(root, captured_at)

    receipts_output = _pty_text(
        [
            sys.executable, "-m", "floati", "receipts", RECEIVER,
            "--root", str(root.path),
        ],
        cwd=REPOSITORY_ROOT,
        tolerated={0},
    )
    refused_output = _pty_text(
        [
            sys.executable, "-m", "floati", "send",
            "--root", str(root.path),
            "--from", SENDER, "--to", RECEIVER,
            "--repo", "floati", "--sha", "00000",
            "--doc", HANDOFF_DOC, "--note", HANDOFF_NOTE,
        ],
        cwd=REPOSITORY_ROOT,
        tolerated={20},
    )

    frames = [
        _frame(root.path, SEND_COMMAND, send_artifact),
        _frame(root.path, INBOX_COMMAND, inbox_artifact),
        _frame(root.path, RECEIPTS_COMMAND_PREFIX, receipts_output),
        _frame(root.path, REFUSED_SEND_COMMAND, refused_output),
    ]
    exposed = [
        engine.expose_readme_text(frame, instrument_roots=(scratch,))
        for frame in frames
    ]
    engine._assert_rendered_identity_safe("mk-d1", exposed)
    exposed = [_wrap(frame) for frame in exposed]
    transcript = "\n".join(block.rstrip() + "\n" for block in exposed)
    text_path = output / "mk-d1-standard.txt"
    text_path.write_text(transcript, encoding="utf-8")
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
        durations[-1] = max(FINAL_MS, FRAME_MS)
        gif_path = output / f"mk-d1-{theme}.gif"
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
        "generator": "scripts/capture-mk-d1.py",
        "source_sha": args.source_sha,
        "captured_at": engine._stamp(captured_at),
        "synthetic": False,
        "scratch": str(scratch),
        "captures": [
            {
                "name": "mk-d1",
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
    seconds = (sum(durations)) / 1000
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

#!/usr/bin/env python3
"""Wake posture matrix: photograph each claimed harness's wake anatomy."""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Sequence

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from run_gauntlet import (  # noqa: E402
    CAPTURE_DIR,
    CLONE,
    capture,
    write_json,
)


WAKE_TERMS = re.compile(
    r"hook|plugin|event|stop|wait|session|daemon|subscribe|webhook",
    re.IGNORECASE,
)


def which(name: str) -> str:
    env = dict(os.environ)
    result = capture(["/usr/bin/command", "-v", name], env=env, timeout_s=5.0)
    return (result.get("stdout") or "").strip()


def summarize_help(text: str) -> List[str]:
    hits = []
    for line in text.splitlines():
        if WAKE_TERMS.search(line):
            hits.append(line.strip())
    return hits[:40]


def probe_argv(argv: Sequence[str], *, timeout_s: float = 8.0) -> Dict[str, Any]:
    executable = Path(argv[0])
    if not executable.exists():
        return {
            "argv": list(argv),
            "exit": None,
            "timed_out": False,
            "absent": True,
            "stdout": "",
            "stderr": "",
            "wake_term_lines": [],
        }
    result = capture(list(argv), cwd=CLONE, timeout_s=timeout_s)
    combined = (result.get("stdout") or "") + "\n" + (result.get("stderr") or "")
    return {
        "argv": list(argv),
        "exit": result["exit"],
        "timed_out": result["timed_out"],
        "stdout_bytes": result["stdout_bytes"],
        "stderr_bytes": result["stderr_bytes"],
        "stdout_sha256": result["stdout_sha256"],
        "stderr_sha256": result["stderr_sha256"],
        "stdout": result["stdout"][:4000],
        "stderr": result["stderr"][:2000],
        "wake_term_lines": summarize_help(combined),
    }


def file_identity(path: str) -> Dict[str, Any]:
    if not path:
        return {"present": False, "path": path}
    resolved = Path(path)
    if not resolved.exists():
        return {"present": False, "path": path}
    real = Path(os.path.realpath(path))
    return {
        "present": True,
        "path": path,
        "realpath": str(real),
        "size": real.stat().st_size if real.is_file() else None,
    }


def main() -> int:
    control = probe_argv(["/usr/bin/python3", "--version"], timeout_s=3.0)
    probes: Dict[str, Any] = {
        "codex": {
            "binaries": ["/opt/homebrew/bin/codex"],
            "commands": [
                ["/opt/homebrew/bin/codex", "--version"],
                ["/opt/homebrew/bin/codex", "--help"],
                ["/opt/homebrew/bin/codex", "help", "hooks"],
            ],
        },
        "claude": {
            "binaries": ["/opt/homebrew/bin/claude", "\x2fUsers/penguinspecz/.local/bin/claude"],
            "commands": [
                ["/opt/homebrew/bin/claude", "--version"],
                ["/opt/homebrew/bin/claude", "--help"],
                ["/opt/homebrew/bin/claude", "hooks", "--help"],
            ],
        },
        "opencode": {
            "binaries": ["/opt/homebrew/bin/opencode"],
            "commands": [
                ["/opt/homebrew/bin/opencode", "--version"],
                ["/opt/homebrew/bin/opencode", "--help"],
                ["/opt/homebrew/bin/opencode", "serve", "--help"],
            ],
        },
        "cursor": {
            "binaries": [
                "/opt/homebrew/bin/cursor-agent",
                "\x2fUsers/penguinspecz/.local/bin/cursor-agent",
                "/Applications/Cursor.app/Contents/Resources/app/bin/cursor",
            ],
            "commands": [
                ["\x2fUsers/penguinspecz/.local/bin/cursor-agent", "--version"],
                ["/opt/homebrew/bin/cursor-agent", "--help"],
                [
                    "/Applications/Cursor.app/Contents/Resources/app/bin/cursor",
                    "--version",
                ],
            ],
        },
        "cline": {
            "binaries": ["/opt/homebrew/bin/cline"],
            "commands": [
                ["/opt/homebrew/bin/cline", "--version"],
                ["/opt/homebrew/bin/cline", "--help"],
            ],
        },
        "grok-build": {
            "binaries": ["/opt/homebrew/bin/grok-build", "/opt/homebrew/bin/grok"],
            "commands": [
                ["/opt/homebrew/bin/grok-build", "--version"],
                ["/opt/homebrew/bin/grok", "--version"],
                ["/opt/homebrew/bin/grok", "--help"],
            ],
        },
        "pi": {
            "binaries": ["/opt/homebrew/bin/pi"],
            "commands": [
                ["/opt/homebrew/bin/pi", "--version"],
                ["/opt/homebrew/bin/pi", "--help"],
            ],
        },
        "herdr": {
            "binaries": ["/opt/homebrew/bin/herdr"],
            "commands": [
                ["/opt/homebrew/bin/herdr", "--version"],
                ["/opt/homebrew/bin/herdr", "--help"],
            ],
        },
        "t3": {
            "binaries": ["/opt/homebrew/bin/t3"],
            "commands": [
                ["/opt/homebrew/bin/t3", "--version"],
                ["/opt/homebrew/bin/t3", "--help"],
            ],
        },
    }
    payload: Dict[str, Any] = {
        "started_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "control": control,
        "harnesses": {},
    }
    for name, spec in probes.items():
        identities = {path: file_identity(path) for path in spec["binaries"]}
        which_hits = {Path(path).name: which(Path(path).name) for path in spec["binaries"]}
        commands = [probe_argv(argv) for argv in spec["commands"]]
        payload["harnesses"][name] = {
            "identities": identities,
            "which": which_hits,
            "commands": commands,
        }
    artifact = write_json(CAPTURE_DIR / "H-wake-posture-probe.json", payload)
    print(json.dumps({"status": "ok", "artifact": artifact}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Dual-surface inventory: cli / desktop app / IDE extension, C0 discipline."""

from __future__ import annotations

import json
import os
import plistlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import sys

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from run_gauntlet import CAPTURE_DIR, CLONE, capture, write_json  # noqa: E402


TOKENISH = ("token", "credential", "secret", "oauth", "apk_", "cog_")


def which(name: str) -> str:
    result = capture(["/usr/bin/command", "-v", name], timeout_s=5.0)
    return (result.get("stdout") or "").strip()


def probe(argv: Sequence[str], *, timeout_s: float = 8.0) -> Dict[str, Any]:
    exe = Path(argv[0])
    if not exe.exists():
        return {"argv": list(argv), "absent": True}
    result = capture(list(argv), cwd=CLONE, timeout_s=timeout_s)
    text = (result.get("stdout") or "") + "\n" + (result.get("stderr") or "")
    lowered = text.lower()
    auth_ask = any(word in lowered for word in TOKENISH) and any(
        word in lowered for word in ("paste", "login", "sign in", "signin", "api key", "enter your")
    )
    return {
        "argv": list(argv),
        "absent": False,
        "exit": result["exit"],
        "timed_out": result["timed_out"],
        "stdout_bytes": result["stdout_bytes"],
        "stderr_bytes": result["stderr_bytes"],
        "stdout_sha256": result["stdout_sha256"],
        "stderr_sha256": result["stderr_sha256"],
        "stdout": (result.get("stdout") or "")[:2500],
        "stderr": (result.get("stderr") or "")[:1500],
        "auth_prompt_suspected": auth_ask,
    }


def identity(path: str) -> Dict[str, Any]:
    p = Path(path)
    if not p.exists():
        return {"path": path, "present": False}
    real = Path(os.path.realpath(path))
    return {
        "path": path,
        "present": True,
        "realpath": str(real),
        "is_dir": p.is_dir(),
        "size": real.stat().st_size if real.is_file() else None,
    }


def app_plist(app: Path) -> Dict[str, Any]:
    info = app / "Contents" / "Info.plist"
    row: Dict[str, Any] = {
        "app": str(app),
        "present": app.is_dir(),
        "plist_present": info.is_file(),
    }
    if not info.is_file():
        return row
    try:
        data = plistlib.loads(info.read_bytes())
    except Exception as exc:
        row["plist_error"] = type(exc).__name__
        return row
    for key in (
        "CFBundleIdentifier",
        "CFBundleName",
        "CFBundleExecutable",
        "CFBundleShortVersionString",
        "CFBundleVersion",
    ):
        row[key] = data.get(key)
    macos = app / "Contents" / "MacOS"
    if macos.is_dir():
        row["macos_names"] = sorted(p.name for p in macos.iterdir())[:20]
    return row


def path_presence(path: Path) -> Dict[str, Any]:
    return {
        "path": str(path),
        "exists": path.exists(),
        "is_file": path.is_file(),
        "is_dir": path.is_dir(),
        "is_symlink": path.is_symlink(),
    }


def list_dir_names(path: Path, *, limit: int = 40) -> Dict[str, Any]:
    if not path.is_dir():
        return {"path": str(path), "present": False, "names": []}
    names = sorted(p.name for p in path.iterdir())
    return {
        "path": str(path),
        "present": True,
        "count": len(names),
        "names": names[:limit],
        "truncated": len(names) > limit,
    }


def main() -> int:
    applications = Path("/Applications")
    app_names = []
    if applications.is_dir():
        app_names = sorted(p.name for p in applications.iterdir() if p.suffix == ".app")
    cursor_ext = Path("~/.cursor/extensions")
    vscode_ext = Path("~/.vscode/extensions")
    cli_names = [
        "codex",
        "claude",
        "opencode",
        "cursor",
        "cursor-agent",
        "cline",
        "grok-build",
        "grok",
        "pi",
        "herdr",
        "t3",
        "agy",
        "antigravity",
        "devin",
        "devin-cli",
    ]
    which_map = {name: which(name) for name in cli_names}
    versions = {}
    for name, path in which_map.items():
        if path:
            versions[name] = probe([path, "--version"], timeout_s=8.0)
        else:
            versions[name] = {"argv": [name, "--version"], "absent": True}

    interesting_apps = [
        Path("/Applications/Claude.app"),
        Path("/Applications/OpenCode.app"),
        Path("/Applications/Cursor.app"),
        Path("/Applications/Grok Bot.app"),
        Path("/Applications/T3 Code (Nightly).app"),
        Path("/Applications/Antigravity.app"),
        Path("/Applications/Codex.app"),
        Path("/Applications/ChatGPT.app"),
        Path("/Applications/Devin.app"),
        Path("/Applications/Cline.app"),
        Path("/Applications/Pi.app"),
        Path("/Applications/Herdr.app"),
        Path("/Applications/Windsurf.app"),
        Path("/Applications/Visual Studio Code.app"),
    ]
    apps = [app_plist(app) for app in interesting_apps]
    matched_apps = [
        name
        for name in app_names
        if any(
            needle in name.lower()
            for needle in (
                "claude",
                "codex",
                "openai",
                "chatgpt",
                "opencode",
                "cursor",
                "t3",
                "grok",
                "antigravity",
                "devin",
                "cline",
                "pi",
                "herdr",
                "gemini",
            )
        )
    ]

    hook_paths = [
        Path("~/.codex/hooks.json"),
        Path("~/.claude/settings.json"),
        Path("~/.cursor/hooks.json"),
        Path("~/.config/opencode"),
        Path("~/.cline/hooks"),
        Path("~/.config/herdr"),
        Path("~/.devin"),
        Path("~/.config/devin"),
        Path("~/.antigravity"),
        Path("~/.config/antigravity"),
        Path("~/Library/Application Support/Claude"),
        Path("~/Library/Application Support/Codex"),
        Path("~/Library/Application Support/OpenCode"),
        Path("~/Library/Application Support/Cursor"),
        Path("~/Library/Application Support/Antigravity"),
        Path("~/Library/Application Support/Devin"),
        Path("~/Library/Application Support/T3 Code"),
        Path("~/Library/Application Support/t3code"),
        Path("~/Library/Application Support/Grok"),
    ]

    payload: Dict[str, Any] = {
        "started_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "control": probe(["/usr/bin/python3", "--version"], timeout_s=3.0),
        "brew_cask": probe(["/opt/homebrew/bin/brew", "list", "--cask"], timeout_s=20.0),
        "which": which_map,
        "versions": versions,
        "applications_all": app_names,
        "applications_name_hits": matched_apps,
        "apps": apps,
        "cursor_extensions": list_dir_names(cursor_ext, limit=80),
        "vscode_extensions": list_dir_names(vscode_ext, limit=40),
        "config_presence": [path_presence(path) for path in hook_paths],
        "cli_help": {},
    }
    help_targets = {
        "agy": which_map.get("agy") or "~/.local/bin/agy",
        "devin": which_map.get("devin") or "/opt/homebrew/bin/devin",
        "antigravity": which_map.get("antigravity") or "",
    }
    for key, path in help_targets.items():
        if path and Path(path).exists():
            payload["cli_help"][key] = probe([path, "--help"], timeout_s=8.0)
        else:
            payload["cli_help"][key] = {"path": path, "absent": True}

    artifact = write_json(CAPTURE_DIR / "H-surface-sweep.json", payload)
    print(json.dumps({"status": "ok", "artifact": artifact}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

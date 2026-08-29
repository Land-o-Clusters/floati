#!/usr/bin/env python3
"""Wake-family close drills: per-surface honesty, event-driven push paths, herdr N/A.

Fixtures / local probes only. Never the live fleet root. Never open GUI apps.
Never synthetic wake rows.
"""

from __future__ import annotations

import json
import os
import plistlib
import signal
import socket
import subprocess
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import sys

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from run_gauntlet import CAPTURE_DIR, CLONE, SCRATCH_PARENT, capture, write_json  # noqa: E402


HOME = Path("~")


def utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def path_row(path: Path) -> Dict[str, Any]:
    return {
        "path": str(path),
        "exists": path.exists(),
        "is_file": path.is_file(),
        "is_dir": path.is_dir(),
        "is_symlink": path.is_symlink(),
        "realpath": str(path.resolve()) if path.exists() else None,
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
    except Exception as exc:  # noqa: BLE001 — photograph the failure
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
    return row


def grep_bundle_hooks(app: Path, *, limit: int = 40) -> Dict[str, Any]:
    """Search app Contents for wake-ish filenames only (no GUI launch)."""
    root = app / "Contents"
    if not root.is_dir():
        return {"app": str(app), "present": False, "hits": []}
    hits: List[str] = []
    terms = ("hook", "hooks.json", "stop", "plugin", "webhook")
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        name = path.name.lower()
        if any(term in name for term in terms):
            try:
                rel = str(path.relative_to(app))
            except ValueError:
                rel = str(path)
            hits.append(rel)
            if len(hits) >= limit:
                break
    return {
        "app": str(app),
        "present": True,
        "hit_count": len(hits),
        "hits": hits,
        "truncated": len(hits) >= limit,
    }


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def http_get(url: str, *, timeout_s: float = 2.0) -> Dict[str, Any]:
    started = time.time()
    try:
        with urllib.request.urlopen(url, timeout=timeout_s) as resp:
            body = resp.read()
            return {
                "url": url,
                "http_status": int(resp.status),
                "bytes": len(body),
                "elapsed_s": round(time.time() - started, 3),
                "error": None,
            }
    except Exception as exc:  # noqa: BLE001
        return {
            "url": url,
            "http_status": None,
            "bytes": 0,
            "elapsed_s": round(time.time() - started, 3),
            "error": "{0}: {1}".format(type(exc).__name__, exc),
        }


def run_push_server(
    argv: Sequence[str],
    *,
    port: int,
    ready_substr: str,
    probe_url: str,
    cycles: int = 3,
    boot_timeout_s: float = 12.0,
    env: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    started_utc = utc()
    proc = subprocess.Popen(
        list(argv),
        cwd=str(CLONE),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=dict(os.environ, **(env or {})),
        text=True,
        start_new_session=True,
    )
    deadline = time.time() + boot_timeout_s
    buf = ""
    ready = False
    while time.time() < deadline:
        if proc.poll() is not None:
            break
        assert proc.stdout is not None
        line = proc.stdout.readline()
        if line:
            buf += line
            if ready_substr in line:
                ready = True
                break
        else:
            time.sleep(0.05)
    cycle_rows: List[Dict[str, Any]] = []
    if ready:
        for i in range(1, cycles + 1):
            cycle_rows.append({"i": i, **http_get(probe_url)})
            time.sleep(0.05)
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        pass
    try:
        rest, _ = proc.communicate(timeout=3)
        if rest:
            buf += rest
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass
        rest, _ = proc.communicate(timeout=2)
        if rest:
            buf += rest
    return {
        "argv": list(argv),
        "port": port,
        "started_utc": started_utc,
        "ready": ready,
        "ready_substr": ready_substr,
        "returncode": proc.returncode,
        "server_output": buf[-4000:],
        "cycles": cycle_rows,
    }


def desktop_surface_probe() -> Dict[str, Any]:
    apps = {
        "codex_desktop": Path("/Applications/ChatGPT.app"),
        "claude_desktop_chat": Path("/Applications/Claude.app"),
        "opencode_desktop": Path("/Applications/OpenCode.app"),
        "cursor_desktop": Path("/Applications/Cursor.app"),
        "t3_desktop": Path("/Applications/T3 Code (Nightly).app"),
        "grok_desktop": Path("/Applications/Grok Bot.app"),
        "antigravity_desktop": Path("/Applications/Antigravity.app"),
    }
    configs = {
        "codex_hooks": HOME / ".codex" / "hooks.json",
        "cursor_hooks": HOME / ".cursor" / "hooks.json",
        "opencode_plugins": HOME / ".config" / "opencode" / "plugins",
        "claude_settings": HOME / ".claude" / "settings.json",
        "herdr_config": HOME / ".config" / "herdr",
    }
    cursor_ext = HOME / ".cursor" / "extensions"
    extension_names: List[str] = []
    if cursor_ext.is_dir():
        extension_names = sorted(
            p.name for p in cursor_ext.iterdir() if "claude-code" in p.name.lower()
        )
    return {
        "apps": {name: app_plist(path) for name, path in apps.items()},
        "bundle_hook_filenames": {
            name: grep_bundle_hooks(path) for name, path in apps.items()
        },
        "configs": {name: path_row(path) for name, path in configs.items()},
        "claude_code_cursor_extensions": extension_names,
        "gui_open_invoked": False,
        "sign_in_invoked": False,
    }


def herdr_not_applicable() -> Dict[str, Any]:
    version = capture(["/opt/homebrew/bin/herdr", "--version"], timeout_s=8.0)
    help_ = capture(["/opt/homebrew/bin/herdr", "--help"], timeout_s=8.0)
    status = capture(["/opt/homebrew/bin/herdr", "status"], timeout_s=8.0)
    text = (help_.get("stdout") or "") + "\n" + (help_.get("stderr") or "")
    llmish = any(
        token in text.lower()
        for token in ("openai", "anthropic", "completion", "prompt", "llm", "model ")
    )
    return {
        "version": version,
        "help": help_,
        "status": status,
        "help_mentions_llm_turn": llmish,
        "help_names_workspace_manager": "terminal workspace manager" in text.lower(),
        "server_running": "status: running" in ((status.get("stdout") or "").lower()),
    }


def pi_push_path() -> Dict[str, Any]:
    version = capture(["/opt/homebrew/bin/pi", "--version"], timeout_s=8.0)
    help_ = capture(["/opt/homebrew/bin/pi", "--help"], timeout_s=8.0)
    listed = capture(["/opt/homebrew/bin/pi", "list"], timeout_s=12.0)
    text = (help_.get("stdout") or "") + "\n" + (help_.get("stderr") or "")
    return {
        "version": version,
        "help": {
            "exit": help_["exit"],
            "stdout_sha256": help_["stdout_sha256"],
            "stderr_sha256": help_["stderr_sha256"],
            "has_mode_rpc": "--mode" in text and "rpc" in text,
            "has_extension_flags": "--extension" in text or "-e <path>" in text,
            "has_no_session": "--no-session" in text,
            "has_session_flags": "--session" in text and "--resume" in text,
        },
        "list_extensions": listed,
    }


def main() -> int:
    SCRATCH_PARENT.mkdir(parents=True, exist_ok=True)
    t3_home = SCRATCH_PARENT / "wake-family-t3-home"
    t3_home.mkdir(parents=True, exist_ok=True)

    control = capture(["/usr/bin/python3", "--version"], timeout_s=3.0)

    oc_port = free_port()
    opencode_push = run_push_server(
        ["/opt/homebrew/bin/opencode", "serve", "--port", str(oc_port)],
        port=oc_port,
        ready_substr="listening on http://127.0.0.1:{0}".format(oc_port),
        probe_url="http://127.0.0.1:{0}/".format(oc_port),
        cycles=3,
    )

    t3_port = free_port()
    t3_push = run_push_server(
        [
            "/opt/homebrew/bin/t3",
            "serve",
            "--no-browser",
            "--port",
            str(t3_port),
            "--base-dir",
            str(t3_home),
        ],
        port=t3_port,
        ready_substr="Listening on http://127.0.0.1:{0}".format(t3_port),
        probe_url="http://127.0.0.1:{0}/".format(t3_port),
        cycles=3,
        boot_timeout_s=20.0,
        env={"HOME": str(t3_home)},
    )

    payload: Dict[str, Any] = {
        "started_utc": utc(),
        "control": control,
        "branch_tip_note": "lane/grok-gauntlet off main; fixtures under .gauntlet-scratch",
        "live_fleet_root_used": False,
        "gui_apps_opened": False,
        "desktop_surfaces": desktop_surface_probe(),
        "event_driven_push": {
            "opencode_cli_serve_3cycle": opencode_push,
            "t3_cli_serve_3cycle": t3_push,
            "pi_cli_extension_rpc_surface": pi_push_path(),
        },
        "herdr_not_applicable": herdr_not_applicable(),
        "organic_cursor_desktop": {
            "note": (
                "This grok seat is a live Cursor desktop agent session; "
                "daemon acceptance already witnessed production wakes on this "
                "coordinate. This row does not re-open ChatGPT.app / Claude.app / "
                "OpenCode.app / T3.app / Antigravity.app / Grok Bot.app."
            ),
            "seat": "grok",
            "harness_surface": "cursor/desktop",
            "hooks_json": path_row(HOME / ".cursor" / "hooks.json"),
        },
    }
    payload["ended_utc"] = utc()
    artifact = write_json(CAPTURE_DIR / "H-wake-family-close.json", payload)
    print(json.dumps({"status": "ok", "artifact": artifact}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

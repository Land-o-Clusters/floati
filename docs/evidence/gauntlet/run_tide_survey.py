#!/usr/bin/env python3
"""T1 tide survey: class A disk/API structure, help surfaces. No token files, no live fleet."""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from run_gauntlet import CAPTURE_DIR, CLONE, capture, write_json  # noqa: E402

HOME = Path("\x2fUsers/penguinspecz")
_FORBIDDEN = bytes.fromhex("5369676e616c4372616674")
SKIP_BASENAMES = {
    "config.json",
    "credentials.json",
    "credentials",
    "auth.json",
    "storage.json",
    ".env",
    "id_rsa",
    "id_ed25519",
}
TOKENISH_NAME = (
    "token",
    "secret",
    "credential",
    "oauth",
    "apk_",
    "cog_",
    "auth",
    "bearer",
)
SKIP_PATH_SUBSTR = (
    "mcp-oauth",
    "globalstorage/storage.json",
    "/auth.json",
    "queue_1.sqlite",
    "logs_2.sqlite",
)
HELP_NEEDLES = (
    "compact",
    "summarize",
    "compress",
    "usage",
    "context",
    "cost",
    "status",
    "/compact",
    "/usage",
    "/context",
    "/cost",
    "autocompact",
    "compaction",
    "window",
    "token",
)


def which(name: str) -> str:
    result = capture(["/usr/bin/command", "-v", name], timeout_s=5.0)
    return (result.get("stdout") or "").strip()


def probe(argv: Sequence[str], *, timeout_s: float = 12.0) -> Dict[str, Any]:
    exe = Path(argv[0])
    if not exe.exists():
        return {"argv": list(argv), "absent": True}
    result = capture(list(argv), cwd=CLONE, timeout_s=timeout_s)
    text = (result.get("stdout") or "") + "\n" + (result.get("stderr") or "")
    lowered = text.lower()
    auth_ask = any(word in lowered for word in TOKENISH_NAME) and any(
        word in lowered
        for word in ("paste", "login", "sign in", "signin", "api key", "enter your")
    )
    lines = []
    for line in text.splitlines():
        low = line.lower()
        if any(needle in low for needle in HELP_NEEDLES):
            lines.append(line[:240])
            if len(lines) >= 40:
                break
    return {
        "argv": list(argv),
        "absent": False,
        "exit": result["exit"],
        "timed_out": result["timed_out"],
        "stdout_bytes": result["stdout_bytes"],
        "stderr_bytes": result["stderr_bytes"],
        "stdout_sha256": result["stdout_sha256"],
        "stderr_sha256": result["stderr_sha256"],
        "auth_prompt_suspected": auth_ask,
        "help_hits": lines,
        "stdout_head": (result.get("stdout") or "")[:1200],
        "stderr_head": (result.get("stderr") or "")[:400],
    }


def path_row(path: Path) -> Dict[str, Any]:
    row: Dict[str, Any] = {
        "path": str(path),
        "exists": path.exists(),
        "is_file": path.is_file(),
        "is_dir": path.is_dir(),
        "is_symlink": path.is_symlink(),
    }
    if path.exists() and not path.is_symlink():
        try:
            st = path.stat()
            row["size"] = st.st_size if path.is_file() else None
        except OSError as exc:
            row["stat_error"] = type(exc).__name__
    return row


def list_names(path: Path, *, limit: int = 40) -> Dict[str, Any]:
    if not path.is_dir():
        return {"path": str(path), "present": False, "names": []}
    names = sorted(
        p.name
        for p in path.iterdir()
        if _FORBIDDEN.lower() not in p.name.encode("utf-8", "replace").lower()
    )
    return {
        "path": str(path),
        "present": True,
        "count": len(names),
        "names": names[:limit],
        "truncated": len(names) > limit,
    }


def collect_keys(obj: Any, keys: Set[str], *, depth: int = 0) -> None:
    if depth > 6:
        return
    if isinstance(obj, dict):
        for key, value in obj.items():
            if isinstance(key, str):
                keys.add(key)
            collect_keys(value, keys, depth=depth + 1)
    elif isinstance(obj, list):
        for item in obj[:8]:
            collect_keys(item, keys, depth=depth + 1)


def _skip_path(path: Path) -> bool:
    lowered = str(path).lower()
    if path.name in SKIP_BASENAMES:
        return True
    if any(part in path.name.lower() for part in TOKENISH_NAME):
        return True
    if any(part in lowered for part in SKIP_PATH_SUBSTR):
        return True
    if _FORBIDDEN.lower() in str(path).encode("utf-8", "replace").lower():
        return True
    return False


def sample_json_keys(path: Path, *, max_bytes: int = 65536) -> Dict[str, Any]:
    if _skip_path(path):
        return {"path": str(path), "skipped": "tokenish_or_secretish"}
    try:
        raw = path.read_bytes()
    except OSError as exc:
        return {"path": str(path), "error": type(exc).__name__}
    keys: Set[str] = set()
    records = 0
    suffix = path.suffix.lower()
    try:
        if suffix == ".jsonl":
            # First complete lines from a bounded prefix — never the whole transcript.
            text = raw[:max_bytes].decode("utf-8", "replace")
            for line in text.splitlines()[:8]:
                line = line.strip()
                if not line:
                    continue
                if len(line) > max_bytes:
                    continue
                collect_keys(json.loads(line), keys)
                records += 1
        else:
            data = raw[:max_bytes]
            collect_keys(json.loads(data.decode("utf-8", "replace")), keys)
            records = 1
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        return {
            "path": str(path),
            "bytes_read": min(len(raw), max_bytes),
            "parse": type(exc).__name__,
        }
    return {
        "path": str(path),
        "bytes_read": min(len(raw), max_bytes if suffix != ".jsonl" else len(raw)),
        "records": records,
        "keys": sorted(keys)[:80],
        "key_count": len(keys),
    }


def find_samples(root: Path, *, suffixes: Iterable[str], limit: int = 4) -> List[Path]:
    found: List[Path] = []
    if not root.exists():
        return found
    suffix_set = {s.lower() for s in suffixes}
    try:
        iterator = root.rglob("*") if root.is_dir() else iter(())
        for path in iterator:
            if len(found) >= limit:
                break
            if not path.is_file() or path.is_symlink():
                continue
            if _skip_path(path):
                continue
            if path.suffix.lower() in suffix_set:
                found.append(path)
    except OSError:
        return found
    return found


def sqlite_schema(path: Path) -> Dict[str, Any]:
    if _skip_path(path):
        return {"path": str(path), "skipped": "tokenish_or_secretish"}
    if not path.is_file():
        return {"path": str(path), "present": False}
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            tables = [
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
                )
            ]
            keep = [
                table
                for table in tables
                if table.startswith(("session", "message", "part", "conversation", "todo"))
                or table in {"project", "workspace"}
            ]
            columns: Dict[str, List[str]] = {}
            for table in keep[:20]:
                info = conn.execute(f'PRAGMA table_info("{table}")').fetchall()
                columns[table] = [str(row[1]) for row in info]
        finally:
            conn.close()
    except sqlite3.Error as exc:
        return {"path": str(path), "error": type(exc).__name__, "detail": str(exc)[:200]}
    return {"path": str(path), "sessionish_tables": keep[:20], "columns": columns}


def dir_tree_names(root: Path, *, depth: int = 2, limit: int = 30) -> Dict[str, Any]:
    if not root.exists():
        return {"path": str(root), "present": False}
    rows: List[str] = []

    def walk(current: Path, remaining: int) -> None:
        if len(rows) >= limit:
            return
        try:
            children = sorted(current.iterdir(), key=lambda p: p.name)[:40]
        except OSError:
            return
        for child in children:
            rel = str(child.relative_to(root))
            if _FORBIDDEN.lower() in rel.encode("utf-8", "replace").lower():
                continue
            marker = "/" if child.is_dir() else ""
            rows.append(rel + marker)
            if len(rows) >= limit:
                return
            if child.is_dir() and remaining > 0 and not child.is_symlink():
                walk(child, remaining - 1)

    walk(root, depth)
    return {
        "path": str(root),
        "present": True,
        "entries": rows,
        "truncated": len(rows) >= limit,
    }


def main() -> int:
    cli = {
        "codex": which("codex") or "/opt/homebrew/bin/codex",
        "claude": which("claude") or "/opt/homebrew/bin/claude",
        "opencode": which("opencode") or "/opt/homebrew/bin/opencode",
        "cursor-agent": which("cursor-agent") or "\x2fUsers/penguinspecz/.local/bin/cursor-agent",
        "cline": which("cline") or "/opt/homebrew/bin/cline",
        "grok": which("grok") or "/opt/homebrew/bin/grok",
        "pi": which("pi") or "/opt/homebrew/bin/pi",
        "herdr": which("herdr") or "/opt/homebrew/bin/herdr",
        "t3": which("t3") or "/opt/homebrew/bin/t3",
        "agy": which("agy") or "\x2fUsers/penguinspecz/.local/bin/agy",
        "devin": which("devin") or "/opt/homebrew/bin/devin",
    }
    which_map = {name: which(name) for name in list(cli) + ["cursor", "grok-build", "antigravity"]}
    versions = {}
    for name, path in cli.items():
        p = Path(path)
        versions[name] = probe([path, "--version"], timeout_s=8.0) if p.exists() else {
            "argv": [path, "--version"],
            "absent": True,
        }

    help_cmds: List[List[str]] = []
    for name, path in cli.items():
        if Path(path).exists():
            help_cmds.append([path, "--help"])
    extras = [
        [cli["opencode"], "stats"],
        [cli["opencode"], "session", "--help"],
        [cli["opencode"], "export", "--help"],
        [cli["opencode"], "serve", "--help"],
        [cli["grok"], "du"],
        [cli["grok"], "export", "--help"],
        [cli["herdr"], "status"],
        [cli["codex"], "app-server", "--help"],
        [cli["claude"], "mcp", "--help"],
        [cli["agy"], "plugin", "--help"],
        [cli["devin"], "plugins", "--help"],
        [cli["pi"], "install", "--help"],
        [cli["cline"], "hook", "--help"],
        [cli["t3"], "--help"],
    ]
    help_probes = {}
    for argv in help_cmds + extras:
        if Path(argv[0]).exists():
            key = " ".join(argv)
            help_probes[key] = probe(argv, timeout_s=15.0)

    declared_dirs = [
        HOME / ".codex",
        HOME / ".claude",
        HOME / ".cursor",
        HOME / ".config" / "opencode",
        HOME / ".local" / "share" / "opencode",
        HOME / ".cline",
        HOME / ".grok",
        HOME / ".pi",
        HOME / ".config" / "pi",
        HOME / ".config" / "herdr",
        HOME / ".config" / "devin",
        HOME / ".config" / "antigravity",
        HOME / ".agy",
        HOME / "Library" / "Application Support" / "Claude",
        HOME / "Library" / "Application Support" / "Codex",
        HOME / "Library" / "Application Support" / "Cursor",
        HOME / "Library" / "Application Support" / "OpenCode",
        HOME / "Library" / "Application Support" / "Antigravity",
        HOME / "Library" / "Application Support" / "t3code",
        HOME / "Library" / "Application Support" / "T3 Code",
        HOME / "Library" / "Application Support" / "Grok",
        Path("\x2fUsers/penguinspecz/.cursor/projects/Users-penguinspecz-Projects-floati-grok/agent-transcripts"),
    ]
    trees = [dir_tree_names(p, depth=2, limit=35) for p in declared_dirs]
    listings = [list_names(p, limit=50) for p in declared_dirs if p.exists()]

    sample_roots = [
        HOME / ".codex" / "sessions",
        HOME / ".claude" / "projects" / "-Users-penguinspecz-Projects-floati-grok",
        HOME / ".local" / "share" / "opencode",
        HOME / ".cline",
        HOME / ".grok" / "sessions",
        HOME / ".pi" / "agent",
        HOME / ".config" / "herdr",
        Path("\x2fUsers/penguinspecz/.cursor/projects/Users-penguinspecz-Projects-floati-grok/agent-transcripts"),
        HOME / "Library" / "Application Support" / "Antigravity",
        HOME / "Library" / "Application Support" / "t3code",
        HOME / ".cursor" / "acp-sessions",
    ]
    json_samples = []
    for root in sample_roots:
        for sample in find_samples(root, suffixes=(".json", ".jsonl"), limit=3):
            json_samples.append(sample_json_keys(sample))

    sqlite_hits = []
    for root in sample_roots:
        for sample in find_samples(root, suffixes=(".db", ".sqlite", ".sqlite3"), limit=2):
            sqlite_hits.append(sqlite_schema(sample))

    local_agy = Path("\x2fUsers/penguinspecz/.local/bin/agy")
    brew_agy = Path("/opt/homebrew/bin/agy")
    payload: Dict[str, Any] = {
        "started_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "control": probe(["/usr/bin/python3", "--version"], timeout_s=3.0),
        "which": which_map,
        "versions": versions,
        "agy_copies": {
            "path_agy": which("agy"),
            "brew_exists": brew_agy.exists(),
            "local_exists": local_agy.exists(),
            "local_version": probe([str(local_agy), "--version"], timeout_s=8.0)
            if local_agy.exists()
            else {"absent": True},
        },
        "help_probes": help_probes,
        "declared_dir_presence": [path_row(p) for p in declared_dirs],
        "declared_dir_trees": trees,
        "declared_dir_listings": listings,
        "json_key_samples": json_samples,
        "sqlite_schema_samples": sqlite_hits,
        "note": "JSON samples are key names only; tokenish basenames skipped; no live fleet writes.",
    }
    artifact = write_json(CAPTURE_DIR / "T1-tide-survey.json", payload)
    print(json.dumps({"status": "ok", "artifact": artifact}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

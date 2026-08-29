#!/usr/bin/env python3
"""T1 depth-2+ key-path probe. Key paths only; no values. No live fleet."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Set

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from run_gauntlet import CAPTURE_DIR, write_json  # noqa: E402

_FORBIDDEN = bytes.fromhex("5369676e616c4372616674")
MAX_LINE = 65536
MAX_LINES = 2000
MAX_DEPTH = 8


def skip_path(path: Path) -> bool:
    lowered = str(path).encode("utf-8", "replace").lower()
    if _FORBIDDEN.lower() in lowered:
        return True
    name = path.name.lower()
    if any(part in name for part in ("auth", "oauth", "credential", "secret", "token")):
        return True
    return False


def collect_paths(obj: Any, prefix: str, depth: int, acc: Set[str]) -> None:
    if depth > MAX_DEPTH:
        return
    if isinstance(obj, dict):
        for key, value in obj.items():
            if not isinstance(key, str):
                continue
            path = f"{prefix}.{key}" if prefix else key
            acc.add(path)
            collect_paths(value, path, depth + 1, acc)
    elif isinstance(obj, list):
        child = f"{prefix}[]" if prefix else "[]"
        acc.add(child)
        for item in obj[:16]:
            collect_paths(item, child, depth + 1, acc)


def probe_jsonl(path: Path) -> Dict[str, Any]:
    if skip_path(path) or not path.is_file():
        return {"path": str(path), "skipped": True}
    paths: Set[str] = set()
    records = 0
    parse_errors = 0
    max_depth_seen = 0
    with path.open("rb") as handle:
        for raw in handle:
            if records >= MAX_LINES:
                break
            raw = raw.strip()
            if not raw:
                continue
            if len(raw) > MAX_LINE:
                parse_errors += 1
                continue
            try:
                obj = json.loads(raw)
            except json.JSONDecodeError:
                parse_errors += 1
                continue
            before = len(paths)
            collect_paths(obj, "", 0, paths)
            records += 1
            if len(paths) > before:
                max_depth_seen = max(max_depth_seen, max((p.count(".") + p.count("[]") // 2) for p in paths) if paths else 0)
    usageish = sorted(p for p in paths if any(n in p.lower() for n in ("usage", "token", "cost", "window", "compact")))
    return {
        "path": str(path),
        "file_bytes": path.stat().st_size,
        "records": records,
        "parse_errors": parse_errors,
        "key_path_count": len(paths),
        "key_paths": sorted(paths)[:250],
        "usageish_paths": usageish[:80],
        "truncated_paths": len(paths) > 250,
    }


def probe_json(path: Path) -> Dict[str, Any]:
    if skip_path(path) or not path.is_file():
        return {"path": str(path), "skipped": True}
    try:
        obj = json.loads(path.read_bytes()[:262144].decode("utf-8", "replace"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        return {"path": str(path), "error": type(exc).__name__}
    paths: Set[str] = set()
    collect_paths(obj, "", 0, paths)
    usageish = sorted(p for p in paths if any(n in p.lower() for n in ("usage", "token", "cost", "window", "compact")))
    return {
        "path": str(path),
        "file_bytes": path.stat().st_size,
        "key_path_count": len(paths),
        "key_paths": sorted(paths)[:250],
        "usageish_paths": usageish[:80],
    }


def sibling_files(directory: Path) -> List[str]:
    if not directory.is_dir():
        return []
    names = []
    for child in sorted(directory.iterdir()):
        if skip_path(child):
            continue
        names.append(child.name + ("/" if child.is_dir() else ""))
        if len(names) >= 40:
            break
    return names


def main() -> int:
    home = Path("~")
    cursor_jsonl = Path(
        "~/.cursor/projects/Users-operator-Projects-floati-grok/agent-transcripts/44b70300-ddc4-4869-a4ce-a0e15d2e12f5/44b70300-ddc4-4869-a4ce-a0e15d2e12f5.jsonl"
    )
    # Prefer a small Codex jsonl so line-one truncation is not the whole story.
    codex_small = Path(
        "~/.codex/sessions/2026/08/09/rollout-2026-08-09T01-17-48-019fe4f4-aeca-7e33-b884-db3a7671b317.jsonl"
    )
    codex_mid = Path(
        "~/.codex/sessions/2026/07/14/rollout-2026-07-14T22-06-41-019f6386-ba54-7c82-8091-d3d490cf24d4.jsonl"
    )
    grok_session = Path(
        "~/.grok/sessions/%2FUsers%2Foperator%2FProjects%2Ffloati-luna/01a04646-85fe-7e13-9530-29d015132df3"
    )
    payload = {
        "started_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "discipline": "key_paths_only_no_values_depth_8",
        "cursor_agent_transcripts": probe_jsonl(cursor_jsonl),
        "codex_jsonl_all_lines_small_file": probe_jsonl(codex_small),
        "codex_jsonl_midsize_beyond_line_one": probe_jsonl(codex_mid),
        "grok_session_dir": str(grok_session),
        "grok_session_siblings": sibling_files(grok_session),
        "grok_summary": probe_json(grok_session / "summary.json") if grok_session.is_dir() else {"absent": True},
        "grok_updates": probe_jsonl(grok_session / "updates.jsonl") if grok_session.is_dir() else {"absent": True},
        "claude_control_depth2": probe_jsonl(
            home / ".claude/projects/-Users-operator-Projects-floati-grok/f2dba0d4-977b-461a-bfcf-6f34e8c3b18a.jsonl"
        ),
    }
    # One more grok sibling if present.
    if grok_session.is_dir():
        extras = {}
        for child in sorted(grok_session.iterdir()):
            if child.suffix.lower() in {".json", ".jsonl"} and child.name not in {"summary.json", "updates.jsonl"}:
                extras[child.name] = probe_jsonl(child) if child.suffix == ".jsonl" else probe_json(child)
        payload["grok_other_json"] = extras
    artifact = write_json(CAPTURE_DIR / "T1-depth2-keypaths.json", payload)
    print(json.dumps({"status": "ok", "artifact": artifact}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

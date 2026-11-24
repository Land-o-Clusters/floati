"""User-level Cursor stop hook.

The hook is one file under the account Cursor directory. It does not name a
seat. Each ``hook install`` writes a marker in that workspace, and this hook
admits the seat only when exactly one marker matches a workspace root from
the stop payload. Anything else is a journal line and no waiter.

This module stays importable as a copied script: the process Cursor starts
has no package path until a marker names one, and a missing marker must
still be able to journal.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path


# Project stop-hook bounds. The launched waiter must use the same numbers
# the project hook already publishes: timeout 1800, deadline 1500, loop 1000.
HOOK_TIMEOUT_SECONDS = 1800
DEADLINE_SECONDS = 1500
LOOP_LIMIT = 1000

MARKER_NAME = "floati-seat.json"
SCRIPT_NAME = "floati-cursor-user-stop.py"
JOURNAL_NAME = "floati-seat-journal.jsonl"
_IDENTIFIER = re.compile(r"[a-z0-9][a-z0-9_-]*")


def marker_path(workspace: Path) -> Path:
    return Path(workspace) / ".cursor" / MARKER_NAME


def _journal_path() -> Path:
    return Path.home() / ".cursor" / JOURNAL_NAME


def _user_script_path() -> Path:
    return Path.home() / ".cursor" / SCRIPT_NAME


def _user_hooks_path() -> Path:
    return Path.home() / ".cursor" / "hooks.json"


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        if os.write(descriptor, payload) != len(payload):
            raise OSError("short write")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.replace(temporary, path)


def _append_journal(record: dict) -> None:
    path = _journal_path()
    if path.is_symlink() or path.parent.is_symlink():
        return
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    line = (json.dumps(record, sort_keys=True) + "\n").encode("utf-8")
    descriptor = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
    try:
        if os.write(descriptor, line) != len(line):
            raise OSError("short journal write")
    finally:
        os.close(descriptor)


def _refuse(code: str, **fields: object) -> dict:
    record = {"event": "seat_unresolved", "code": code, **fields}
    _append_journal(record)
    return {"status": "refused", "code": code}


def marker_bytes(*, workspace: Path, root: Path, node: str, runtime: Path) -> bytes:
    body = {
        "harness": "cursor",
        "node": node,
        "root": str(root),
        "runtime": str(runtime),
        "schema_version": 0,
        "workspace": str(workspace),
    }
    return (json.dumps(body, sort_keys=True) + "\n").encode("utf-8")


def _seat_from_marker(payload: object, workspace: str) -> dict | None:
    if not isinstance(payload, dict):
        return None
    if payload.get("schema_version") != 0 or payload.get("harness") != "cursor":
        return None
    if payload.get("workspace") != workspace:
        return None
    node = payload.get("node")
    root = payload.get("root")
    runtime = payload.get("runtime")
    if not isinstance(node, str) or _IDENTIFIER.fullmatch(node) is None:
        return None
    if not isinstance(root, str) or not Path(root).is_absolute():
        return None
    if not isinstance(runtime, str) or not Path(runtime).is_absolute():
        return None
    return {
        "status": "resolved",
        "node": node,
        "root": root,
        "workspace": workspace,
        "runtime": runtime,
    }


def _read_marker(workspace: str) -> tuple[str, Path, dict | None]:
    """Return ``(kind, path, seat)``.

    ``kind`` is ``resolved``, ``absent``, or ``unreadable``.
    """

    path = marker_path(workspace)
    if path.is_symlink() or path.parent.is_symlink():
        return "unreadable", path, None
    if not path.exists():
        return "absent", path, None
    if not path.is_file():
        return "unreadable", path, None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return "unreadable", path, None
    seat = _seat_from_marker(payload, workspace)
    if seat is None:
        return "unreadable", path, None
    return "resolved", path, seat


def resolve_cursor_seat(workspace_roots: object) -> dict:
    """Admit one seat, or journal why the payload's markers cannot name one."""

    roots = workspace_roots if isinstance(workspace_roots, list) else []
    found: list[tuple[Path, dict]] = []
    missing: list[Path] = []
    broken: list[Path] = []
    for root in roots:
        if not isinstance(root, str) or not root or not Path(root).is_absolute():
            continue
        kind, path, seat = _read_marker(root)
        if kind == "resolved" and seat is not None:
            found.append((path, seat))
        elif kind == "absent":
            missing.append(path)
        else:
            broken.append(path)
    if len(found) > 1:
        return _refuse(
            "cursor_seat_marker_ambiguous",
            markers=[str(path) for path, _seat in found],
        )
    if broken:
        if len(broken) == 1 and not found:
            return _refuse("cursor_seat_marker_unreadable", marker=str(broken[0]))
        return _refuse(
            "cursor_seat_marker_unreadable",
            markers=[str(path) for path in broken],
        )
    if len(found) == 1:
        return found[0][1]
    if len(missing) == 1:
        return _refuse("cursor_seat_marker_absent", marker=str(missing[0]))
    return _refuse(
        "cursor_seat_marker_absent",
        markers=[str(path) for path in missing],
    )


def waiter_argv(outcome: dict) -> list[str]:
    launcher = str(Path(outcome["runtime"]) / "scripts" / "floati")
    return [
        launcher,
        "wake",
        "wait",
        "--harness",
        "cursor",
        "--root",
        outcome["root"],
        "--as",
        outcome["node"],
        "--runtime",
        outcome["runtime"],
        "--deadline-seconds",
        str(int(DEADLINE_SECONDS)),
        "--hook-timeout-seconds",
        str(int(HOOK_TIMEOUT_SECONDS)),
        "--loop-limit",
        str(int(LOOP_LIMIT)),
    ]


def write_cursor_seat_marker(*, workspace: Path, root: Path, node: str, runtime: Path) -> Path:
    path = marker_path(workspace)
    if path.is_symlink() or path.parent.is_symlink():
        _refuse_hooks("cursor seat marker path is a symlink")
    _atomic_write(path, marker_bytes(workspace=workspace, root=root, node=node, runtime=runtime))
    return path


def _refuse_hooks(detail: str) -> None:
    from .errors import ProtocolRefusal

    raise ProtocolRefusal(
        "hook_install_hooks_invalid",
        detail,
        remedy="fix or remove the user Cursor hooks file and re-run install",
    )


def _owned_user_command(command: object, script_path: Path) -> bool:
    if not isinstance(command, str):
        return False
    try:
        argv = shlex.split(command)
    except ValueError:
        return False
    return str(script_path) in argv


def plan_cursor_user_hook(*, timeout: int, loop_limit: int, read=None) -> dict[Path, bytes]:
    """Render the seat-free account hook and its executable source."""

    directory = Path.home() / ".cursor"
    script_path = _user_script_path()
    hooks_path = _user_hooks_path()
    if directory.is_symlink() or script_path.is_symlink() or hooks_path.is_symlink():
        _refuse_hooks("user Cursor hook path is a symlink")
    source = Path(__file__).read_bytes()
    command = shlex.join([sys.executable, str(script_path)])
    entry = {"command": command, "loop_limit": int(loop_limit), "timeout": int(timeout)}
    payload: dict = {"version": 1, "hooks": {"stop": []}}
    try:
        before = read(hooks_path) if read is not None else (
            hooks_path.read_bytes() if hooks_path.is_file() else None)
        if before is not None:
            existing = json.loads(before.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        _refuse_hooks("existing user Cursor hooks.json is not JSON")
    if before is not None:
        if not isinstance(existing, dict):
            _refuse_hooks("existing user Cursor hooks.json is not an object")
        payload = existing
    hooks = payload.get("hooks")
    if hooks is None:
        payload["hooks"] = {}
    elif not isinstance(hooks, dict):
        _refuse_hooks("existing user Cursor hooks object is invalid")
    stops = payload["hooks"].get("stop", [])
    if not isinstance(stops, list):
        _refuse_hooks("existing user Cursor stop hooks are not a list")
    if read is not None and any(isinstance(row, dict) and
            _owned_user_command(row.get("command"), script_path) for row in stops):
        _refuse_hooks("the account already has a Floati Cursor stop hook")
    payload["hooks"]["stop"] = [
        row for row in stops
        if not (isinstance(row, dict) and _owned_user_command(row.get("command"), script_path))
    ]
    payload["hooks"]["stop"].append(entry)
    payload["version"] = 1
    encoded = (json.dumps(payload, indent=2) + "\n").encode("utf-8")
    return {script_path: source, hooks_path: encoded}


def install_cursor_user_hook(*, timeout: int, loop_limit: int) -> Path:
    """Install one seat-free stop hook under the account Cursor directory."""
    for path, content in plan_cursor_user_hook(timeout=timeout, loop_limit=loop_limit).items():
        _atomic_write(path, content)
    return _user_hooks_path()


def uninstall_cursor_seat_marker(*, workspace: Path, root: Path, node: str, runtime: Path) -> dict:
    """Remove the workspace seat marker. The user-level hook stays."""

    from .errors import ProtocolRefusal
    from .root import validate_identifier

    workspace = Path(workspace)
    root = Path(root)
    runtime = Path(runtime)
    if not workspace.is_absolute() or workspace.is_symlink() or not workspace.is_dir():
        raise ProtocolRefusal(
            "hook_install_workspace_invalid",
            "workspace must be an absolute nonsymlink directory",
            remedy="pass --workspace as an absolute project directory",
        )
    if not root.is_absolute():
        raise ProtocolRefusal(
            "hook_install_root_invalid",
            "root must be an absolute path",
            remedy="pass --root as an absolute fleet directory",
        )
    if not runtime.is_absolute():
        raise ProtocolRefusal(
            "hook_install_runtime_invalid",
            "runtime must be an absolute path",
            remedy="pass --runtime as the absolute floati checkout or install",
        )
    validate_identifier(node, field="node")
    path = marker_path(workspace)
    if path.is_symlink() or path.parent.is_symlink():
        _refuse_hooks("cursor seat marker path is a symlink")
    removed = path.is_file()
    if removed:
        path.unlink()
    return {
        "schema_version": 0,
        "harness": "cursor",
        "workspace": str(workspace),
        "marker_path": str(path),
        "root": str(root),
        "node": node,
        "removed": removed,
    }


def main() -> int:
    raw = sys.stdin.buffer.read()
    try:
        payload = json.loads(raw.decode("utf-8") if raw else "{}")
    except (UnicodeError, json.JSONDecodeError):
        payload = None
    roots: object = []
    if isinstance(payload, dict):
        roots = payload.get("workspace_roots", [])
    outcome = resolve_cursor_seat(roots)
    if outcome.get("status") != "resolved":
        sys.stdout.write("{}\n")
        return 0
    # Cursor launches user hooks from ~/.cursor, outside the declared seat.
    # The marker-selected workspace is the child's actual authority coordinate;
    # the waiter still verifies its mapping and consent strictly.
    completed = subprocess.run(waiter_argv(outcome), input=raw, cwd=outcome["workspace"])
    return int(completed.returncode)


if __name__ == "__main__":
    raise SystemExit(main())

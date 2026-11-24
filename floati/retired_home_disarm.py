"""F2-RETIRED-HOME-DISARM-1: a seat's old homes never keep armed hooks.

Board Am.5 measured the re-home that left a retired home's
``.cursor/hooks.json`` armed as the node for four days: the zombie woke on
every envelope while the live seat kept building. Seat migration and node
retirement sweep every hook config the bus's own workspace map still
records for the node outside the home it keeps, remove only the entries
that name this node on this root, and receipt the act. The config file
itself and every foreign entry are never deleted, and the sweep never
scans the filesystem: its only inputs are the workspace map and the
harness-known config paths inside the homes that map records.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Callable, Optional, Union

from .codex_wait_contract import mapped_workspaces
from .errors import ProtocolRefusal
from .ids import uuid7_hex
from .jsonl import transact
from .registry import utc_now
from .root import FloatiRoot

KIND = "retired_home_disarm_receipt"
CURSOR_HOOK_RELATIVE = os.path.join(".cursor", "hooks.json")
ZCODE_HOOK_RELATIVES = (
    os.path.join(".zcode", "hooks.json"),
    os.path.join(".zcode", "settings.json"),
    os.path.join("hooks", "hooks.json"),
)
CONFIG_RELATIVES = (CURSOR_HOOK_RELATIVE, *ZCODE_HOOK_RELATIVES)


def _names_coordinate(command: str, root: str, node: str) -> bool:
    return root in command and (
        f"--as {node}" in command or f"--node {node}" in command
    )


def _disarm_cursor_document(
    document: object, root: str, node: str
) -> tuple[int, list[str]]:
    hooks = document.get("hooks") if isinstance(document, dict) else None
    stops = hooks.get("stop") if isinstance(hooks, dict) else None
    if not isinstance(stops, list):
        return 0, []

    def names(entry: object) -> bool:
        return (
            isinstance(entry, dict)
            and isinstance(entry.get("command"), str)
            and _names_coordinate(entry["command"], root, node)
        )

    removed = [str(row["command"]) for row in stops if names(row)]
    if isinstance(hooks, dict):
        hooks["stop"] = [row for row in stops if not names(row)]
    return len(removed), removed


def _disarm_zcode_document(
    document: object, root: str, node: str
) -> tuple[int, list[str]]:
    hooks = document.get("hooks") if isinstance(document, dict) else None
    stop_groups = hooks.get("Stop") if isinstance(hooks, dict) else None
    if not isinstance(stop_groups, list):
        return 0, []

    def names(entry: object) -> bool:
        if not isinstance(entry, dict):
            return False
        command = entry.get("command")
        if isinstance(command, str) and _names_coordinate(command, root, node):
            return True
        env = entry.get("env")
        return (
            isinstance(env, dict)
            and env.get("HOOK_NODE") == node
            and env.get("HOOK_ROOT") == root
        )

    removed: list[str] = []
    for group in stop_groups:
        members = group.get("hooks") if isinstance(group, dict) else None
        if not isinstance(members, list):
            continue
        removed.extend(
            str(row.get("command", "")) for row in members if names(row)
        )
        group["hooks"] = [row for row in members if not names(row)]
    return len(removed), removed


def _disarmer_for(relative: str) -> Callable[[object, str, str], tuple[int, list[str]]]:
    return (
        _disarm_cursor_document if relative == CURSOR_HOOK_RELATIVE
        else _disarm_zcode_document
    )


def _rewrite_json(path: Path, document: object) -> tuple[str, str]:
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    encoded = (json.dumps(document, indent=2) + "\n").encode("utf-8")
    mode = path.stat().st_mode & 0o777
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.floati-disarm-", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, mode)
        offset = 0
        while offset < len(encoded):
            written = os.write(descriptor, encoded[offset:])
            if written <= 0:
                raise OSError("short disarm write")
            offset += written
        os.fsync(descriptor)
        os.close(descriptor)
        os.replace(temporary, path)
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise
    after = hashlib.sha256(path.read_bytes()).hexdigest()
    return before, after


def _sweep_one(path: Path, root: str, node: str) -> dict[str, object]:
    """Disarm one config file, or report it typed-unreadable, never fatal."""

    relative = os.path.join(*path.parts[-2:])
    entry: dict[str, object] = {"config": str(path), "commands": []}
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        entry["outcome"] = "unreadable"
        entry["removed"] = 0
        return entry
    removed, commands = _disarmer_for(relative)(document, root, node)
    if removed:
        entry["before_sha256"], entry["after_sha256"] = _rewrite_json(
            path, document
        )
    entry["removed"] = removed
    entry["commands"] = commands
    return entry


def recorded_old_homes(
    bus_home: Path, node_id: str, new_home: Optional[Path]
) -> list[Path]:
    """The homes the bus's own workspace map still binds to the node.

    ``new_home`` (the migration destination) is excluded; at retirement
    there is none, so every recorded home is old. Never a filesystem scan.
    """

    keep = None if new_home is None else new_home.resolve()
    homes: list[Path] = []
    for workspace, bound_node in mapped_workspaces(bus_home):
        if bound_node != node_id:
            continue
        if keep is not None and workspace.resolve() == keep:
            continue
        homes.append(workspace)
    return homes


def disarm_retired_homes(
    root: FloatiRoot,
    node_id: str,
    *,
    new_home: Optional[Path],
    idempotency_key: str,
) -> dict[str, object]:
    """Disarm every hook config the node left behind, with one receipt."""

    node = node_id
    root_str = str(root.path)
    homes = recorded_old_homes(root.tenant_home, node, new_home)
    disarmed: list[dict[str, object]] = []
    for home in homes:
        for relative in CONFIG_RELATIVES:
            candidate = home / relative
            if candidate.is_symlink() or not candidate.is_file():
                continue
            disarmed.append(_sweep_one(candidate, root_str, node))
    row = {
        "schema_version": 1,
        "kind": KIND,
        "tenant_id": root.tenant_id,
        "id": "retired-home-disarm-" + uuid7_hex(),
        "timestamp": utc_now(),
        "node_id": node,
        "new_home": None if new_home is None else str(new_home.resolve()),
        "homes_examined": [str(home) for home in homes],
        "disarmed": disarmed,
        "idempotency_key": idempotency_key,
    }
    relative_ledger = Path("receipts/retired-home-disarm") / (node + ".jsonl")

    def decide(prior):
        matching = [r for r in prior if r["idempotency_key"] == idempotency_key]
        if matching:
            if matching[-1]["node_id"] != node:
                raise ProtocolRefusal(
                    "retired_home_disarm_key_conflict",
                    "disarm key has different coordinates",
                    remedy="use a new key for different disarm coordinates",
                )
            return matching[-1], None
        return row, row

    return transact(root, relative_ledger, decide, allowed_kinds={KIND})

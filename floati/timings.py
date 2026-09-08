"""SN-R1 timing receipts: one post-bind ledger row per instrumented verb.

Am.4: the gauntlet wins. A hostile node spelling must refuse BEFORE any
durable write, so a timing row exists only AFTER the invocation's root and
every named node have resolved through the governed primitives (post-bind).
Am.5: bind is not entitlement. A declared seat that disagrees with the
requested root or node is bound and still not entitled; that refusal is
before-drain and must leave the root byte-identical. Pre-bind refusals -
argparse, an unknown verb, an unresolved root, a node spelling that names
nobody - write nothing anywhere. Entitled post-bind refusals emit a row
with outcome ``refused`` and the refusal's own code.

There is no ``--record-timing`` flag and no artifact field: timing lives in
the ledger it describes, and the instrumented set is the design's six verbs
(``send``, ``inbox``, ``ack``, ``doctor``, ``wake resume``, ``seat board``).
"""

from __future__ import annotations

import hashlib
import json
import math
import resource
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence

from .errors import DurabilityFailure, IntegrityFailure, ProtocolRefusal
from .ids import uuid7_hex
from .jsonl import append_record, read_records
from .registry import Registry, utc_now
from .root import FloatiRoot, resolve_command_root

INSTRUMENTED_VERBS = frozenset(
    {"send", "inbox", "ack", "doctor", "wake resume", "seat board"}
)
_TWO_WORD_VERBS = {
    ("wake", "resume"): "wake resume",
    ("seat", "board"): "seat board",
}
#: The parser dests that name nodes, per instrumented verb. Every name must
#: resolve through the registry before the invocation is bound.
_NODE_DESTS: Mapping[str, tuple[str, ...]] = {
    "send": ("sender", "recipient"),
    "inbox": ("recipient",),
    "ack": ("recipient",),
    "doctor": (),
    "wake resume": ("actor",),
    "seat board": ("actor",),
}
_OUTCOME = {
    "ok": "ok",
    "refused": "refused",
    "degraded": "degraded",
    "no_result": "no_result",
    "error": "error",
    "intentional_silence": "no_result",
    "cannot_speak": "error",
    "malformed_evidence": "error",
    "orchestration_deadline": "error",
}
_OUTCOME_FOR_EXIT = {
    0: "ok",
    20: "refused",
    22: "error",
    31: "no_result",
    32: "no_result",
    33: "error",
    34: "error",
    35: "degraded",
}
_NULL_REFUSAL_OUTCOMES = frozenset({"ok", "no_result"})
_PERCENTILE_FLOOR = 20

_CONTEXT = threading.local()


def _cpu_seconds() -> float:
    usage = resource.getrusage(resource.RUSAGE_SELF)
    return usage.ru_utime + usage.ru_stime


def verb_path(arguments: Sequence[str]) -> str:
    if not arguments:
        return ""
    if len(arguments) >= 2:
        pair = _TWO_WORD_VERBS.get((arguments[0], arguments[1]))
        if pair is not None:
            return pair
    return arguments[0]


def command_slug(command: str) -> str:
    return command.replace(" ", "-")


def argv_digest(arguments: Sequence[str], resolved_root: Optional[Path]) -> str:
    resolved = list(arguments)
    if resolved_root is not None:
        canonical = str(resolved_root)
        index = 0
        while index < len(resolved):
            token = resolved[index]
            if token == "--root" and index + 1 < len(resolved):
                resolved[index + 1] = canonical
                index += 2
                continue
            if token.startswith("--root="):
                resolved[index] = "--root=" + canonical
            index += 1
    encoded = json.dumps(resolved, ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    )
    return hashlib.sha256(encoded).hexdigest()


def _round_seconds(value: float) -> float:
    return round(max(0.0, value), 3)


def outcome_for_status(status: str) -> str:
    return _OUTCOME.get(status, "error")


def refusal_code_for(outcome: str, evidence: Mapping[str, Any]) -> Optional[str]:
    if outcome in _NULL_REFUSAL_OUTCOMES:
        return None
    code = evidence.get("code")
    if isinstance(code, str) and code:
        return code
    return "error"


def node_id_for(command: str, parsed: Any) -> Optional[str]:
    """The invocation's --as/--from actor, for the verbs that name one."""

    if parsed is None:
        return None
    for dest in _NODE_DESTS.get(command, ()):
        value = getattr(parsed, dest, None)
        if isinstance(value, str) and value:
            return value
    return None


def root_flag_value(arguments: Sequence[str]) -> Optional[str]:
    """Read --root from argv without parsing the rest of the command."""

    found = None
    index = 0
    while index < len(arguments):
        token = arguments[index]
        if token == "--root":
            if index + 1 < len(arguments) and not str(arguments[index + 1]).startswith(
                "-"
            ):
                found = arguments[index + 1]
                index += 2
                continue
            index += 1
            continue
        if token.startswith("--root="):
            value = token.split("=", 1)[1]
            found = value or None
        index += 1
    return found


def begin(arguments: Sequence[str]) -> None:
    _CONTEXT.started_at = utc_now()
    _CONTEXT.mono = time.monotonic()
    _CONTEXT.cpu = _cpu_seconds()
    _CONTEXT.arguments = list(arguments)
    _CONTEXT.parsed = None
    _CONTEXT.argv_root = root_flag_value(arguments)
    _CONTEXT.finished = False


def bind_parsed(parsed: Any) -> None:
    _CONTEXT.parsed = parsed


def clear() -> None:
    for attr in (
        "started_at",
        "mono",
        "cpu",
        "arguments",
        "parsed",
        "argv_root",
        "finished",
    ):
        if hasattr(_CONTEXT, attr):
            delattr(_CONTEXT, attr)


def _entitled_to_write(root: FloatiRoot, node_id: Optional[str]) -> bool:
    """True only when this invocation may leave a durable row on ``root``.

    Bind is not entitlement. A declared seat whose marker disagrees with the
    requested root or node has already refused; the command never became
    entitled to touch that root. Pre-bind still writes nothing via
    ``_bind_state``. This is the later line: before-drain, after bind.
    """

    from .seat_declaration import SeatDeclaration, require_declared_coordinate

    try:
        cwd = Path.cwd()
    except OSError:
        return True
    declaration = SeatDeclaration.load(cwd)
    if declaration is None:
        return True
    if node_id is None:
        return (
            declaration.root == str(root.path)
            and declaration.tenant_id == root.tenant_id
        )
    try:
        require_declared_coordinate(cwd, node_id, root)
    except ProtocolRefusal:
        return False
    return True


def _bind_state() -> tuple[Optional[FloatiRoot], Optional[str], bool]:
    """Resolve the root and every named node through the governed primitives.

    Returns ``(root, node_id, bound)``. ``bound`` is False whenever anything
    refuses - an unresolved root or a node spelling that names nobody is a
    pre-bind invocation, and a pre-bind invocation writes nothing.
    """

    parsed = getattr(_CONTEXT, "parsed", None)
    arguments = getattr(_CONTEXT, "arguments", None)
    command = verb_path(arguments or ())
    if parsed is None or command not in INSTRUMENTED_VERBS:
        return None, None, False
    raw_root = getattr(parsed, "root", None)
    if not (isinstance(raw_root, str) and raw_root):
        raw_root = getattr(_CONTEXT, "argv_root", None)
    if not (isinstance(raw_root, str) and raw_root):
        return None, None, False
    try:
        root = resolve_command_root(raw_root, create=False)
    except (ProtocolRefusal, IntegrityFailure, DurabilityFailure, OSError, ValueError):
        return None, None, False
    node_id: Optional[str] = None
    registry = Registry(root)
    for dest in _NODE_DESTS.get(command, ()):
        value = getattr(parsed, dest, None)
        if not (isinstance(value, str) and value):
            return None, None, False
        try:
            resolved = registry.resolve_node_id(value, field=dest)
        except (ProtocolRefusal, IntegrityFailure, ValueError):
            return None, None, False
        if node_id is None:
            node_id = resolved
    return root, node_id, True


def record_timing_receipt(
    root: FloatiRoot,
    *,
    command: str,
    arguments: Sequence[str],
    node_id: Optional[str],
    started_at: str,
    wall_seconds: float,
    cpu_seconds: float,
    outcome: str,
    refusal_code: Optional[str],
) -> Dict[str, Any]:
    record: Dict[str, Any] = {
        "schema_version": 1,
        "id": "timing-" + uuid7_hex(),
        "tenant_id": root.tenant_id,
        "timestamp": utc_now(),
        "kind": "timing_receipt",
        "command": command,
        "argv_digest": argv_digest(arguments, root.path),
        "node_id": node_id,
        "started_at": started_at,
        "wall_seconds": wall_seconds,
        "cpu_seconds": cpu_seconds,
        "outcome": outcome,
        "refusal_code": refusal_code,
    }
    relative = Path("receipts/timings") / (command_slug(command) + ".jsonl")
    append_record(root, relative, record, allowed_kinds={"timing_receipt"})
    return record


def finish(
    status: Optional[str] = None,
    evidence: Optional[Mapping[str, Any]] = None,
    *,
    exit_code: Optional[int] = None,
) -> None:
    """Write the one timing row for this invocation, if it earned one.

    Idempotent: the first call wins, so a direct handler that knows its own
    status (doctor) reports it and the CLI's generic tail adds nothing.
    """

    if getattr(_CONTEXT, "finished", True):
        return
    _CONTEXT.finished = True
    if not hasattr(_CONTEXT, "mono"):
        return
    command = verb_path(getattr(_CONTEXT, "arguments", ()))
    if command not in INSTRUMENTED_VERBS:
        return
    root, node_id, bound = _bind_state()
    if not bound or root is None:
        return
    if not _entitled_to_write(root, node_id):
        return
    if status is not None:
        outcome = outcome_for_status(status)
        code = refusal_code_for(outcome, evidence or {})
    else:
        outcome = _OUTCOME_FOR_EXIT.get(exit_code if exit_code is not None else -1, "error")
        code = None if outcome in _NULL_REFUSAL_OUTCOMES else "error"
    wall_seconds = _round_seconds(time.monotonic() - _CONTEXT.mono)
    cpu_seconds = _round_seconds(_cpu_seconds() - _CONTEXT.cpu)
    record_timing_receipt(
        root,
        command=command,
        arguments=list(_CONTEXT.arguments),
        node_id=node_id,
        started_at=_CONTEXT.started_at,
        wall_seconds=wall_seconds,
        cpu_seconds=cpu_seconds,
        outcome=outcome,
        refusal_code=code,
    )


def _percentile(sorted_values: Sequence[float], percent: float) -> float:
    rank = max(1, math.ceil(percent / 100.0 * len(sorted_values)))
    return _round_seconds(float(sorted_values[rank - 1]))


def _parse_timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ProtocolRefusal(
            "arguments_invalid",
            "since must be a UTC RFC3339 timestamp",
        ) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def derive_chart(
    root: FloatiRoot,
    *,
    command: Optional[str] = None,
    since: Optional[str] = None,
) -> Dict[str, Any]:
    since_at = None if since is None else _parse_timestamp(since)
    rows: list[Dict[str, Any]] = []
    timings_dir = root.path / "receipts" / "timings"
    if timings_dir.is_dir() and not timings_dir.is_symlink():
        for path in sorted(timings_dir.glob("*.jsonl")):
            if not path.is_file() or path.is_symlink():
                continue
            relative = Path("receipts/timings") / path.name
            rows.extend(
                read_records(root, relative, allowed_kinds={"timing_receipt"})
            )
    if command is not None:
        rows = [row for row in rows if row.get("command") == command]
    if since_at is not None:
        rows = [
            row
            for row in rows
            if _parse_timestamp(str(row["timestamp"])) >= since_at
        ]
    grouped: Dict[str, list[Dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(str(row["command"]), []).append(row)
    commands: Dict[str, Any] = {}
    for name, items in sorted(grouped.items()):
        walls = sorted(float(item["wall_seconds"]) for item in items)
        refused = sum(1 for item in items if item.get("outcome") == "refused")
        slowest = max(items, key=lambda item: float(item["wall_seconds"]))
        entry: Dict[str, Any] = {
            "count": len(items),
            "max_seconds": _round_seconds(walls[-1]),
            "slowest_receipt_id": slowest["id"],
            "slowest_outcome": slowest["outcome"],
            "refused_share": round(refused / len(items), 3),
        }
        if len(items) < _PERCENTILE_FLOOR:
            entry["insufficient"] = True
        else:
            entry["insufficient"] = False
            entry["p50_seconds"] = _percentile(walls, 50)
            entry["p95_seconds"] = _percentile(walls, 95)
        commands[name] = entry
    return {"stamp": "derived", "commands": commands}

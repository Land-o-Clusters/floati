"""Read-only exact native waiter lease/hold binding for census observations."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import uuid

from floati.framing import decode_frames, encode_frame
from floati.records import validate_record
from floati.root import FloatiRoot, validate_identifier
from floati.stop_hold import StopHoldLedger
from floati.wake_control import validate_session_id


def _require(value, detail):
    if not value:
        raise ValueError("native waiter invocation effect: " + detail)


def _pid(value):
    return type(value) is int and 0 < value < 2**31


def _birth(value, wall_end):
    return type(value) in (int, float) and math.isfinite(value) and 0 <= value <= wall_end.timestamp()


def _object(raw):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            _require(key not in result, "repeated state field")
            result[key] = value
        return result
    result = json.loads(raw, object_pairs_hook=unique)
    _require(type(result) is dict and len(raw) <= 8192
             and (json.dumps(result, sort_keys=True, separators=(",", ":")) + "\n").encode() == raw,
             "state is not canonical bounded native JSON")
    return result


def verify_waiter_invocation(root, node, session, workspace, before, after, *,
                             child_pid, wall_begin, wall_end, phase="exited", allow_unheld=False):
    """Prove one selected lease from its complete current bytes and hold pair."""
    _require(isinstance(root, FloatiRoot), "root is not validated")
    node, session = validate_identifier(node, "node"), validate_session_id(session)
    _require(type(allow_unheld) is bool and phase == "exited" and _pid(child_pid)
             and isinstance(wall_begin, datetime) and isinstance(wall_end, datetime)
             and wall_begin.tzinfo is not None and wall_end.tzinfo is not None and wall_begin <= wall_end,
             "terminal invocation requires exact collected process and wall bounds")
    _require(type(before) is dict and type(after) is dict, "immediate raw inventories are required")
    digest = hashlib.sha256(session.encode()).hexdigest()
    relative = f"state/waiter-invocations/{node}/{digest}.json"
    prefix = "state/waiter-invocations"
    changed = {path for path in set(before) | set(after)
               if (path == prefix or path.startswith(prefix + "/")) and before.get(path) != after.get(path)}
    _require(relative in changed and changed <= {relative, relative[:-5] + ".lock"},
             "complete changed invocation population differs from the selected session")
    _require(relative not in before and type(after.get(relative)) is bytes,
             "only one new selected invocation is reviewed")
    from floati.waiter_invocation import _ordinary, _read
    path = root.tenant_home / relative
    _ordinary(path)
    info = path.lstat()
    _require(stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid()
             and stat.S_IMODE(info.st_mode) == 0o600 and info.st_nlink == 1,
             "state is not a private owned ordinary file")
    lock_relative = relative[:-5] + ".lock"
    lock = root.tenant_home / lock_relative
    _ordinary(lock)
    lock_info = lock.lstat()
    _require(after.get(lock_relative) == b"" and lock.read_bytes() == b""
             and stat.S_ISREG(lock_info.st_mode) and lock_info.st_uid == os.geteuid()
             and stat.S_IMODE(lock_info.st_mode) == 0o600 and lock_info.st_nlink == 1,
             "selected invocation lock is not the exact private empty native lease")
    for index,parent in enumerate((path.parent, path.parent.parent)):
        metadata = parent.lstat()
        _require(stat.S_ISDIR(metadata.st_mode) and metadata.st_uid == os.geteuid()
                 and not metadata.st_mode & (0o077 if index == 0 else 0o022),
                 "invocation parent permissions differ from the native producer")
    raw = after[relative]
    _require(path.read_bytes() == raw, "state does not bind retained current bytes")
    row = _object(raw)
    _require(_read(path) == row, "native state reader differs from retained bytes")
    _require(set(row) == {"node_id", "session_id", "workspace", "pid", "process_start_epoch",
             "parent_pid", "parent_process_start_epoch", "host_ancestry", "token", "invocation_id", "phase"},
             "state fields differ from the native schema")
    _require(row["node_id"] == node and row["session_id"] == session
             and row["workspace"] == str(workspace) and row["pid"] == child_pid
             and row["phase"] == phase and _birth(row["process_start_epoch"], wall_end),
             "state has a foreign root/node/session/workspace/process/phase")
    token = row["token"]
    _require(type(token) is str and re.fullmatch(r"[0-9a-f]{32}", token), "token encoding differs")
    parsed = uuid.UUID(hex=token)
    born = datetime.fromtimestamp((parsed.int >> 80) / 1000, timezone.utc)
    _require(parsed.version == 7 and parsed.variant == uuid.RFC_4122
             and wall_begin - timedelta(milliseconds=1) <= born <= wall_end,
             "token is not a native identity from this invocation")
    ancestry = row["host_ancestry"]
    _require(type(ancestry) is list and 0 < len(ancestry) <= 64, "host ancestry is unbounded or absent")
    seen = {child_pid}
    from floati.codex_wait_liveness import process_start_epoch
    from floati.waiter_invocation import _native_parent
    for index, ancestor in enumerate(ancestry):
        _require(type(ancestor) is dict and set(ancestor) == {"pid", "process_start_epoch", "parent_pid"}
                 and _pid(ancestor["pid"]) and ancestor["pid"] not in seen
                 and _pid(ancestor["parent_pid"]) and _birth(ancestor["process_start_epoch"], wall_end),
                 "host ancestry is malformed or cyclic")
        seen.add(ancestor["pid"])
        _require(process_start_epoch(ancestor["pid"]) == ancestor["process_start_epoch"]
                 and _native_parent(ancestor["pid"]) == ancestor["parent_pid"],
                 "host ancestry differs from retained native process identities")
        _require(ancestor["process_start_epoch"] <= row["process_start_epoch"],
                 "host ancestor birth follows the selected waiter birth")
        if index:
            _require(ancestry[index - 1]["parent_pid"] == ancestor["pid"], "host ancestry edge differs")
    _require(ancestry[-1]["parent_pid"] == 1
             and row["parent_pid"] == ancestry[0]["pid"]
             and row["parent_process_start_epoch"] == ancestry[0]["process_start_epoch"],
             "native parent does not match the complete ancestry")
    _require(ancestry[0]["process_start_epoch"] <= row["process_start_epoch"] <= born.timestamp(),
             "waiter birth lies outside its native parent/token chronology")
    ledger = StopHoldLedger.relative(node).as_posix()
    old, current = before.get(ledger, b""), after.get(ledger, b"")
    _require(type(old) is bytes and type(current) is bytes and current.startswith(old),
             "hold ledger rewrote its retained prefix")
    rows = decode_frames(current[len(old):])
    _require(b"".join(encode_frame(item) for item in rows) == current[len(old):],
             "hold append is not canonical")
    for item in rows:
        validate_record(item, root.tenant_id, {"stop_hold_receipt"}, integrity=True)
    StopHoldLedger._progress(rows, node)
    if not rows and allow_unheld:
        _require(row["invocation_id"] is None and current == old,
                 "unheld mail return must have no invocation binding or new hold rows")
        _require(row["process_start_epoch"] > 0 and (child_pid == os.getpid()
                 or row["process_start_epoch"] >= wall_begin.timestamp() - 1),
                 "unheld waiter birth exceeds the collected native launch bounds")
    else:
        _require(len(rows) == 2 and [item["phase"] for item in rows] == ["start", "exit"],
                 "terminal invocation lacks its exact new start/exit pair")
    for item in rows:
        _require(item["invocation_id"] == row["invocation_id"] and item["node_id"] == node
                 and item["acting_session_id"] == session and item["workspace"] == str(workspace)
                 and item.get("holder_pid") == child_pid
                 and item.get("process_start_epoch") == row["process_start_epoch"],
                 "hold pair differs from the exact native invocation")
    _require(StopHoldLedger(root).read(node) == decode_frames(current),
             "native hold reader differs from the complete retained transcript")
    _require(path.read_bytes() == raw, "state changed during read-only proof")
    return {"path": relative, "value": row}

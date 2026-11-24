"""One nonqueueing kernel lease through a native waiter's final output flush.

The holder publishes its measured PID/birth and accepts an exact-token retire
request. Teardown never selects or signals processes by their command line.
"""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import errno
import hashlib
import json
import math
import os
from pathlib import Path
import stat
import sys
import time

from .codex_wait_liveness import process_alive, process_start_epoch
from .errors import DurabilityFailure, IntegrityFailure, ProtocolRefusal
from .ids import uuid7_hex
from .root import validate_identifier
from .wake_control import validate_session_id


def _refuse(detail):
    return ProtocolRefusal("waiter_invocation_unproven", detail,
        remedy="preserve the exact waiter invocation evidence and restore its native exit proof before teardown")


def _paths(root, node, session):
    node = validate_identifier(node, "node")
    session = validate_session_id(session)
    digest = hashlib.sha256(session.encode("utf-8")).hexdigest()
    # Keep the lexical coordinate: resolving first would erase a symlinked
    # parent before the ordinary-path check has a chance to reject it.
    base = root.tenant_home / "state/waiter-invocations" / node / digest
    paths = base.with_suffix(".lock"), base.with_suffix(".json"), base.with_suffix(".retire.json")
    for path in paths:
        _ordinary(path)
    return paths


def _ordinary(path):
    for coordinate in (*reversed(path.parents), path):
        try:
            metadata = coordinate.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(metadata.st_mode) or (
                coordinate == path and not stat.S_ISREG(metadata.st_mode)) or (
                coordinate != path and not stat.S_ISDIR(metadata.st_mode)):
            raise _refuse("nonordinary waiter invocation coordinate")


def _open_lock(path):
    _ordinary(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    _ordinary(path)
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    metadata = os.fstat(descriptor)
    named = path.lstat()
    if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid()
            or metadata.st_nlink != 1 or (metadata.st_dev, metadata.st_ino) != (named.st_dev, named.st_ino)):
        os.close(descriptor)
        raise _refuse("waiter invocation lease identity changed")
    return descriptor


def _read(path):
    _ordinary(path)
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise _refuse("waiter invocation state is not ordinary")
        data = os.read(descriptor, 8193)
        if len(data) > 8192:
            raise _refuse("waiter invocation state exceeds its bound")
    finally:
        os.close(descriptor)
    try:
        return json.loads(data)
    except (ValueError, UnicodeError) as exc:
        raise _refuse("waiter invocation state is malformed") from exc


def _write(path, row):
    _ordinary(path)
    temporary = path.with_name("." + path.name + "." + uuid7_hex())
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        encoded = (json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
        if len(encoded) > 8192:
            raise _refuse("waiter invocation testimony exceeds its bounded state")
        if os.write(descriptor, encoded) != len(encoded):
            raise _refuse("waiter invocation testimony write was truncated")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.replace(temporary, path)


def _native_parent(pid):
    """Return a live process's native parent; only proved exit returns None."""
    if sys.platform == "darwin":
        from .codex_wait_liveness import _darwin_process_info
        try:
            info = _darwin_process_info(pid)
        except ProtocolRefusal as exc:
            raise _refuse(exc.detail) from exc
        if info is None or info["status"] == 5:
            return None
        return info["parent_pid"]
    elif sys.platform.startswith("linux"):
        try:
            cells = Path("/proc", str(pid), "stat").read_text().rpartition(") ")[2].split()
        except (FileNotFoundError, ProcessLookupError):
            return None
        except OSError as exc:
            raise _refuse("live waiter process metadata is unreadable") from exc
        if len(cells) < 2 or not cells[1].isdigit():
            raise _refuse("native waiter process ancestry is malformed")
        if cells[0] in {"Z", "X"}:
            return None
        return int(cells[1])
    raise _refuse("native waiter process ancestry is unsupported on " + sys.platform)


def _same_process(pid, birth):
    measured = process_start_epoch(pid)
    if measured is None:
        # A failed read may be followed by PID reuse. Recheck birth, not
        # signal-zero on the numeric PID, before calling the holder unreadable.
        from .seat_host_worker import _identity
        current = _identity(pid)
        if current is None:
            if _native_parent(pid) is None:
                return False
        elif current['process_start_epoch'] != birth:
            return False
        raise _refuse("live waiter process birth is unreadable; actual exit is unproved")
    if measured != birth:
        return False
    # A terminal, unreaped zombie has exited; it cannot retain cwd activity.
    return _native_parent(pid) is not None


def _host_ancestry():
    """Retain native parent edges, including a shell between hook and host."""
    pid, result, seen = os.getppid(), [], set()
    while pid > 1:
        if pid in seen or len(result) >= 64:
            raise _refuse("native waiter host ancestry is cyclic or exceeds its bound")
        seen.add(pid)
        birth = process_start_epoch(pid)
        parent = _native_parent(pid)
        if birth is None or parent is None:
            raise _refuse("native waiter host exited before its ancestry could be verified")
        result.append({"pid": pid, "process_start_epoch": birth, "parent_pid": parent})
        pid = parent
    if not result:
        raise _refuse("native waiter was already orphaned before host attribution")
    return result


class WaiterInvocation:
    def __init__(self, root, node, workspace, session):
        self.root, self.node, self.workspace, self.session = root, node, Path(workspace), session
        self.lock, self.state, self.request = _paths(root, node, session)
        self.descriptor = _open_lock(self.lock)
        self.owned = False
        try:
            fcntl.flock(self.descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(self.descriptor)
            self.descriptor = None
            return
        self.owned = True
        try:
            birth = process_start_epoch(os.getpid())
            ancestry = _host_ancestry()
            if birth is None:
                raise _refuse("native waiter process birth cannot be measured")
            self.row = {"node_id": node, "session_id": session, "workspace": str(workspace),
                "pid": os.getpid(), "process_start_epoch": birth, "parent_pid": ancestry[0]["pid"],
                "parent_process_start_epoch": ancestry[0]["process_start_epoch"],
                "host_ancestry": ancestry, "token": uuid7_hex(), "invocation_id": None, "phase": "running"}
            _write(self.state, self.row)
        except BaseException:
            os.close(self.descriptor)
            self.descriptor = None
            raise

    def bind_hold(self, start):
        self.row["invocation_id"] = start["invocation_id"]
        _write(self.state, self.row)

    def stop_reason(self):
        if os.getppid() != self.row["parent_pid"]:
            return "host_exited"
        try:
            for ancestor in self.row["host_ancestry"]:
                if not _same_process(ancestor["pid"], ancestor["process_start_epoch"]):
                    return "host_exited"
                if _native_parent(ancestor["pid"]) != ancestor["parent_pid"]:
                    return "host_exited"
        except ProtocolRefusal:
            return "host_liveness_unproven"
        request = _read(self.request)
        if request == {"token": self.row["token"], "pid": self.row["pid"],
                "process_start_epoch": self.row["process_start_epoch"]}:
            return "teardown_requested"
        return None

    def close(self):
        if self.descriptor is None:
            return
        try:
            if self.owned and hasattr(self, "row"):
                self.row["phase"] = "exited"
                _write(self.state, self.row)
        finally:
            os.close(self.descriptor)
            self.descriptor = None


def verified_invocation(root_path, node, workspace, session, *, harnesses=("codex", "cursor", "zcode", "claude")):
    """Read-only binding/consent validation, before taking any publication guard."""
    from .codex_wait_contract import CodexWaitConsentLedger, CodexWaitSessionLedger, resolve_participant
    from .jsonl import read_records_snapshot
    from .registry import Registry
    from .wake_control import is_session_paused
    try:
        participant = resolve_participant(Path(root_path), Path(workspace), _strict=True)
        if participant is None or (node is not None and participant.binding.node_id != node):
            return None
        node = participant.binding.node_id
        harness = Registry(participant.root).require_active(node)["role"].casefold()
        if harness not in harnesses:
            return None
        CodexWaitConsentLedger(participant.root).require_armed(participant.binding)
        validate_session_id(session)
        ledger = CodexWaitSessionLedger(participant.root)
        matching = ledger._matching(read_records_snapshot(participant.root, ledger._relative(node),
            allowed_kinds=ledger._KINDS), participant.binding)
        if matching and matching[-1]["acting_session_id"] != session:
            return None
        if is_session_paused(participant.root, node, session):
            return None
    except (ProtocolRefusal, IntegrityFailure, DurabilityFailure, OSError, ValueError):
        return None
    return WaiterInvocation(participant.root, node, workspace, session)


@contextmanager
def retire_waiter_invocations(root, node, workspace, session, *, preflight=None):
    """Request exact cooperative exit, prove process death, retain the lease.

    This is deliberately outside the node guard: the waiter needs that guard
    to append its native exit receipt. A successor cannot enter until the
    caller has completed teardown and released this context.
    """
    from .codex_wait_contract import CodexWaitConsentLedger, CodexWaitSessionLedger, resolve_participant
    from .jsonl import read_records_snapshot
    from .stop_hold import StopHoldLedger
    participant = resolve_participant(root.path, Path(workspace), _strict=True)
    if participant is None or participant.binding.node_id != node:
        yield []
        return
    ledger = CodexWaitSessionLedger(root)
    claims = ledger._matching(read_records_snapshot(root, ledger._relative(node),
        allowed_kinds=ledger._KINDS), participant.binding)
    if not claims or claims[-1]["acting_session_id"] != session:
        yield []
        return
    try:
        consent = CodexWaitConsentLedger(root).require_armed(participant.binding)
    except ProtocolRefusal as exc:
        if exc.code != "codex_wait_consent_missing":
            raise
        yield []
        return
    ledger._identity(participant.binding, consent, session)
    lock, state, request = _paths(root, node, session)
    descriptor = _open_lock(lock)
    retired = []
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            row = _read(state)
            if (not isinstance(row, dict) or row.get("node_id") != node or row.get("session_id") != session
                    or row.get("workspace") != str(workspace) or type(row.get("pid")) is not int
                    or not 0 < row["pid"] < 2**31 or type(row.get("process_start_epoch")) not in {int, float}
                    or not 0 <= row["process_start_epoch"] <= 253402300799
                    or not math.isfinite(row["process_start_epoch"]) or not isinstance(row.get("token"), str)):
                raise _refuse("held waiter invocation lacks exact process testimony")
            if row["pid"] == os.getpid():
                raise _refuse("teardown cannot synchronously retire its own process")
            invocation = row.get("invocation_id")
            starts = [item for item in StopHoldLedger(root).read(node)
                if item["invocation_id"] == invocation and item["phase"] == "start"]
            if (len(starts) != 1 or starts[0]["node_id"] != node
                    or starts[0]["acting_session_id"] != session or starts[0]["workspace"] != str(workspace)
                    or starts[0].get("holder_pid") != row["pid"]
                    or starts[0].get("process_start_epoch") != row["process_start_epoch"]):
                raise _refuse("held waiter lacks an exact native start for its root/node/session/process")
            if not _same_process(row["pid"], row["process_start_epoch"]):
                raise _refuse("kernel lease holder no longer matches its native process testimony")
            if preflight is not None:
                preflight(row)
            current = _read(state)
            if not isinstance(current, dict) or current.get("token") != row["token"]:
                raise _refuse("waiter invocation changed during read-only teardown preflight")
            _write(request, {"token": row["token"], "pid": row["pid"],
                "process_start_epoch": row["process_start_epoch"]})
            deadline = time.monotonic() + 10
            while True:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise _refuse("waiter did not release its invocation lease after retirement")
                    time.sleep(.02)
                    continue
                break
            while _same_process(row["pid"], row["process_start_epoch"]):
                if time.monotonic() >= deadline:
                    raise _refuse("waiter released its lease but its process has not exited")
                time.sleep(.02)
            current = _read(state)
            if current is None or current.get("token") != row["token"] or current.get("phase") != "exited":
                raise _refuse("waiter did not publish its exact terminal invocation testimony")
            invocation = current.get("invocation_id")
            if invocation is not None:
                rows = [item for item in StopHoldLedger(root).read(node) if item["invocation_id"] == invocation]
                if len(rows) != 2 or [item["phase"] for item in rows] != ["start", "exit"]:
                    raise _refuse("waiter exit has no exact native start/exit receipt pair")
                start = rows[0]
                if (start["node_id"] != node or start["acting_session_id"] != session
                        or start["workspace"] != str(workspace)
                        or start.get("holder_pid") != row["pid"]
                        or start.get("process_start_epoch") != row["process_start_epoch"]):
                    raise _refuse("waiter exit receipts differ from its exact root/node/session/process")
                retired.append({"pid": row["pid"], "process_start_epoch": row["process_start_epoch"],
                    "invocation_id": invocation, "start_receipt_id": rows[0]["id"],
                    "exit_receipt_id": rows[1]["id"], "reason": "teardown_requested"})
        else:
            # A normal mail return can release its invocation lease a few
            # instructions before the CLI process exits. Keep the newly
            # acquired lease and wait out that exact finalization race too.
            row = _read(state)
            if (isinstance(row, dict) and row.get("phase") == "exited"
                    and row.get("node_id") == node and row.get("session_id") == session
                    and row.get("workspace") == str(workspace)
                    and type(row.get("pid")) is int and 0 < row["pid"] < 2**31
                    and type(row.get("process_start_epoch")) in {int, float}
                    and row["pid"] != os.getpid()):
                deadline = time.monotonic() + 10
                while _same_process(row["pid"], row["process_start_epoch"]):
                    if time.monotonic() >= deadline:
                        raise _refuse("completed waiter invocation still has a live CLI process")
                    time.sleep(.02)
        yield retired
    finally:
        os.close(descriptor)

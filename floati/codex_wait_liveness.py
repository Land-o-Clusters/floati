"""Holder liveness testimony for participation claims: pid AND process start.

A waiter participation claim outlives its holder: nothing appends a release
row when the holder process dies, so the work it presented stays classified
held and the wake daemon defers on it forever (measured on 2026-09-08,
``docs/evidence/fq-6-2026-09-08.md``).  This module is the liveness witness
for that claim: the waiter records ``pid`` and its measured process start
time at participation, and any consumer can later classify the claim as
``live`` or ``released``.  Liveness is judged by pid AND process start, never
by name and never by pid alone -- a reused pid is a dead holder wearing it.
"""

from __future__ import annotations

import ctypes
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

from .errors import ProtocolRefusal
from .registry import utc_now
from .root import FloatiRoot, validate_identifier
from .wake_control import validate_session_id


HOLDER_TESTIMONY_KIND = "codex_wait_holder_testimony"
_TESTIMONY_SCHEMA_VERSION = 1
_TESTIMONY_MAX_BYTES = 4096
_PID_BOUND = 2**31 - 1
#: Both verdicts agree only to the whole second that `/bin/ps -o lstart=`
#: reports, so the reuse comparison is judged at one second: death is decided
#: by signal zero alone, and a pid reused inside the same second is not a
#: host behavior this tolerance can confuse with a live holder.
_START_TOLERANCE_SECONDS = 1.0

_LIVE = "live"
_RELEASED = "released"
_UNPROVEN = "unproven"


def holder_testimony_relative(node_id: str) -> Path:
    """One derived coordinate for a seat's holder testimony."""

    return Path("state/codex-wait") / validate_identifier(node_id, "node") / "holder.json"


def holder_testimony_path(root: FloatiRoot, node_id: str) -> Path:
    return root.resolve_relative(holder_testimony_relative(node_id))


@dataclass(frozen=True)
class HolderTestimony:
    session_id: str
    pid: int
    process_start_epoch: float
    timestamp: str


def process_alive(pid: int) -> bool:
    """Signal-zero liveness: absent is dead; unreadable is alive."""

    if not isinstance(pid, int) or isinstance(pid, bool) or not 1 <= pid <= _PID_BOUND:
        raise ProtocolRefusal(
            "process_pid_invalid",
            "pid is outside its bounds",
            remedy="pass the numeric process id of a real process, 1 through 2147483647",
        )
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


if sys.platform == "darwin":

    class _ProcBsdInfo(ctypes.Structure):
        # Field offsets measured on this host (darwin 25.6.0) against
        # `/bin/ps -o lstart=` ground truth on 2026-09-10: the pid field
        # reads back the queried pid at offset 12 and the start time lands
        # at 120/128, one 136-byte record. A layout drift fails the pid
        # self-check and classifies unproven instead of misclassifying.
        _fields_ = [
            ("pbi_flags", ctypes.c_uint32),
            ("pbi_status", ctypes.c_uint32),
            ("pbi_reserved", ctypes.c_uint32),
            ("pbi_pid", ctypes.c_uint32),
            ("pbi_ppid", ctypes.c_uint32),
            ("pbi_uid", ctypes.c_uint32),
            ("pbi_gid", ctypes.c_uint32),
            ("pbi_ruid", ctypes.c_uint32),
            ("pbi_rgid", ctypes.c_uint32),
            ("pbi_svuid", ctypes.c_uint32),
            ("pbi_svgid", ctypes.c_uint32),
            ("rfu_1", ctypes.c_uint32),
            ("pbi_comm", ctypes.c_char * 16),
            ("pbi_name", ctypes.c_char * 48),
            ("pbi_nfiles", ctypes.c_uint32),
            ("pbi_pgid", ctypes.c_uint32),
            ("pbi_start_tvsec", ctypes.c_uint64),
            ("pbi_start_tvusec", ctypes.c_uint64),
        ]

    _PROC_PIDTBSDINFO = 3
    _LIBPROC = ctypes.CDLL("/usr/lib/libproc.dylib")
    _LIBPROC.proc_pidinfo.restype = ctypes.c_int

    def _start_epoch(pid: int) -> Optional[float]:
        info = _ProcBsdInfo()
        received = _LIBPROC.proc_pidinfo(
            ctypes.c_int(pid), ctypes.c_int(_PROC_PIDTBSDINFO),
            ctypes.c_ulonglong(0), ctypes.byref(info),
            ctypes.c_int(ctypes.sizeof(info)),
        )
        if received <= 0:
            return None
        if info.pbi_pid != pid:
            raise ProtocolRefusal(
                "codex_wait_start_time_unmeasurable",
                "the process start layout did not read back the queried pid",
                remedy=(
                    "re-measure the proc_pidinfo proc_bsdinfo layout against "
                    "/bin/ps -o lstart= and update _ProcBsdInfo; until then the "
                    "holder classifies unproven and work stays held"
                ),
            )
        if info.pbi_start_tvusec >= 1_000_000:
            raise ProtocolRefusal(
                "codex_wait_start_time_unmeasurable",
                "the process start microseconds are outside a timeval",
                remedy=(
                    "re-measure the proc_pidinfo proc_bsdinfo layout against "
                    "/bin/ps -o lstart= and update _ProcBsdInfo; until then the "
                    "holder classifies unproven and work stays held"
                ),
            )
        return info.pbi_start_tvsec + info.pbi_start_tvusec / 1e6

else:

    def _start_epoch(pid: int) -> Optional[float]:
        try:
            stat_raw = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
            boot_raw = Path("/proc/stat").read_text(encoding="utf-8")
        except OSError:
            return None
        after_comm = stat_raw.rpartition(")")[2].split()
        if len(after_comm) < 22:
            return None
        boot = None
        for line in boot_raw.splitlines():
            if line.startswith("btime "):
                boot = int(line.split()[1])
                break
        ticks_per_second = os.sysconf("SC_CLK_TCK")
        if boot is None or ticks_per_second <= 0:
            return None
        return boot + int(after_comm[19]) / ticks_per_second


def process_start_epoch(pid: int) -> Optional[float]:
    """Measure one process's start as seconds since the epoch, or None.

    ``None`` is absent: a dead pid, or a live one this host cannot measure.
    """

    if not isinstance(pid, int) or isinstance(pid, bool) or not 1 <= pid <= _PID_BOUND:
        raise ProtocolRefusal(
            "process_pid_invalid",
            "pid is outside its bounds",
            remedy="pass the numeric process id of a real process, 1 through 2147483647",
        )
    return _start_epoch(pid)


def write_holder_testimony(
    root: FloatiRoot, node_id: str, session_id: str,
) -> Dict[str, Any]:
    """Record who holds the claim: this process's pid and measured start.

    Defaults are the calling process itself, which is the only honest
    default: the waiter writes this from its own participation path.
    """

    node = validate_identifier(node_id, "node")
    session = validate_session_id(session_id)
    pid = os.getpid()
    measured = process_start_epoch(pid)
    if measured is None:
        raise ProtocolRefusal(
            "codex_wait_start_time_unmeasurable",
            "holder testimony requires a measurable process start time",
            remedy=(
                "none-with-why: hold without testimony; the claim classifies "
                "unproven and the waiter keeps holding the turn regardless"
            ),
        )
    row: Dict[str, Any] = {
        "schema_version": _TESTIMONY_SCHEMA_VERSION,
        "kind": HOLDER_TESTIMONY_KIND,
        "tenant_id": root.tenant_id,
        "node_id": node,
        "session_id": session,
        "pid": pid,
        "process_start_epoch": measured,
        "timestamp": utc_now(),
    }
    encoded = (json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    path = holder_testimony_path(root, node)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        if os.write(descriptor, encoded) != len(encoded):
            raise ProtocolRefusal(
                "codex_wait_holder_testimony_short",
                "holder testimony write was truncated",
                remedy=(
                    "let the waiter's next participation rewrite the testimony; "
                    "repeated short writes mean the state disk is failing and "
                    "the claim stays unproven, which holds conservatively"
                ),
            )
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.replace(temporary, path)
    return row


def read_holder_testimony(root: FloatiRoot, node_id: str) -> Optional[HolderTestimony]:
    """Read one seat's holder testimony; anything unprovable reads as absent.

    This is waiter state, not a ledger: per the breaker precedent a
    malformed, foreign, or overgrown file is absence, never an error.
    """

    path = holder_testimony_path(root, node_id)
    try:
        data = path.read_bytes()
    except OSError:
        return None
    if not data or len(data) > _TESTIMONY_MAX_BYTES:
        return None
    try:
        row = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(row, dict) or set(row) != {
        "schema_version", "kind", "tenant_id", "node_id", "session_id",
        "pid", "process_start_epoch", "timestamp",
    }:
        return None
    if row["schema_version"] != _TESTIMONY_SCHEMA_VERSION or row["kind"] != HOLDER_TESTIMONY_KIND:
        return None
    if row["tenant_id"] != root.tenant_id or row["node_id"] != node_id:
        return None
    pid = row["pid"]
    if not isinstance(pid, int) or isinstance(pid, bool) or not 1 <= pid <= _PID_BOUND:
        return None
    start = row["process_start_epoch"]
    if isinstance(start, bool) or not isinstance(start, (int, float)) or start < 0:
        return None
    try:
        session = validate_session_id(row["session_id"])
    except ProtocolRefusal:
        return None
    timestamp = row["timestamp"]
    if not isinstance(timestamp, str) or not 1 <= len(timestamp) <= 64:
        return None
    return HolderTestimony(
        session_id=session, pid=pid, process_start_epoch=float(start), timestamp=timestamp,
    )


def classify_holder(testimony: Optional[HolderTestimony]) -> str:
    """Classify one holder as ``live``, ``released``, or ``unproven``.

    Every unmeasurable road leads to ``unproven``, the conservative verdict:
    only proven death -- or proven pid reuse -- releases a claim.  The
    instrument's own typed failures are absorbed here so a layout drift can
    never turn a waiter's hold loop into a crash loop.
    """

    if testimony is None:
        return _UNPROVEN
    if not process_alive(testimony.pid):
        return _RELEASED
    try:
        measured = process_start_epoch(testimony.pid)
    except ProtocolRefusal:
        return _UNPROVEN
    if measured is None:
        return _UNPROVEN
    if abs(measured - testimony.process_start_epoch) <= _START_TOLERANCE_SECONDS:
        return _LIVE
    return _RELEASED

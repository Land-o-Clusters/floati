"""NET-QUIET-1: one fleet-wide quiet window, run as a product verb.

A window settles BEFORE it holds: it waits for the 15-minute load average to
fall to a bound, and gives up without holding anything when the settle cap
passes first. Once open it holds every seat: each rescan pauses (SIGSTOP) the
process subtree of any seat that is building or testing; a seat that is only
writing keeps running and is observed. The window never pauses the operator's
own seat, an exempt seat, its own process tree, a process of another account,
or a process someone else had already stopped.

The window releases itself on a touch of its release file, on its cap, or on
SIGINT/SIGTERM/SIGHUP, and its exit path resumes (SIGCONT) exactly the
processes it paused itself, each checked against the start identity recorded
before the pause. Recovery resumes confirmed holds. An interrupted signal with
no durable outcome requires the exact operator command printed by quiet status.

At release every active seat gets one exposure receipt: what ran in its
workspace inside the window and the CPU it used, measured as the delta of
cumulative CPU time over the timed interval, never a sampled percentage. A
delta the window could not observe whole (a process first seen after the
window opened although it started before, a process that exited between
rescans, a failed rescan) says it is a lower bound.

Seats map to workspaces only through the root's own workspace map (the map
``waiter arm`` writes); a seat with no mapped workspace is reported
``no_workspace``, never guessed. No home scan, no network, no telemetry.
"""
from __future__ import annotations

import ctypes
import errno
import math
import os
import re
import select
import shlex
import signal
import struct
import subprocess
import sys
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from .errors import ProtocolRefusal
from .framing import FrameError, decode_frames
from .ids import uuid7_hex
from .jsonl import (MAX_LEDGER_BYTES, MAX_LEDGER_RECORDS, MAX_RECORD_BYTES,
                    _append_frame, _encode_record, _locked_path)
from .registry import Registry
from .root import FloatiRoot, validate_identifier

LEDGER_RELATIVE = Path("receipts/quiet/windows.jsonl")
WINDOW_KIND = "quiet_window_receipt"
EXPOSURE_KIND = "quiet_exposure_receipt"
QUIET_KINDS = frozenset({WINDOW_KIND, EXPOSURE_KIND})
TERMINAL_PHASES = frozenset({"released", "not_opened"})
CPU_METHOD = "cumulative_cpu_time_delta"
MAX_EXPOSED_PROCESSES = 64
MAX_PAUSES_PER_RECORD = 256
MAX_PROCESSES = 100000
_STOPPED_STATES = frozenset({"T", "t"})
_TERMINAL_STATES = frozenset({"Z", "X", "x"})

# Build and test executables, matched on the executable's own name.
_BUILD_EXECUTABLES = frozenset({
    "swift-frontend", "swift-build", "swift-test", "swift-driver", "swiftc",
    "swift-autolink-extract", "xctest", "xcodebuild", "clang", "clang++", "gcc",
    "g++", "cc", "c++", "ld", "ld64", "ld.lld", "lld", "rustc", "make", "gmake",
    "ninja", "cmake", "gradle", "gradlew", "javac", "mvn", "tsc", "esbuild",
    "webpack", "pytest", "py.test", "jest", "vitest", "tox", "nox",
})
# Tool front ends that build or test only with a named subcommand.
_SUBCOMMAND_TOOLS = {
    "cargo": frozenset({"build", "test", "bench", "check", "clippy", "run"}),
    "go": frozenset({"build", "test", "vet", "install"}),
    "swift": frozenset({"build", "test"}),
    "npm": frozenset({"test", "build", "t"}),
    "pnpm": frozenset({"test", "build", "t"}),
    "yarn": frozenset({"test", "build"}),
    "bun": frozenset({"test", "build"}),
    "dotnet": frozenset({"build", "test"}),
}
# Interpreters and shells build or test when an argument names a runner.
_INTERPRETERS = re.compile(r"(?:python(?:[0-9.]*)|pypy[0-9.]*|bash|zsh|sh|dash|node|uv|perl|ruby)\Z")
RUNNER_WORDS = (
    "unittest", "pytest", "test-parallel", "xctest", "tox", "nox", "jest", "vitest",
    "mocha", "webpack", "tsc", "swift build", "swift test", "cargo build", "cargo test",
    "go build", "go test", "npm test", "npm run test", "npm build", "npm run build",
    "make", "xcodebuild", "build-app", "package-release", "release-audit", "dry-run.sh",
)
_RUNNER_WORDS = re.compile(
    r"(?:^|[^a-z0-9_])(?:" + "|".join(re.escape(word) for word in RUNNER_WORDS) + r")(?:$|[^a-z0-9_])")
PRESSURE_METRICS = {
    "psi_memory_some_avg10": "Linux PSI /proc/pressure/memory 'some' avg10, percent of time stalled",
    "psi_memory_full_avg10": "Linux PSI /proc/pressure/memory 'full' avg10, percent of time stalled",
    "vm_pressure_level": "macOS kern.memorystatus_vm_pressure_level: 1 normal, 2 warning, 4 critical",
}


def covered_commands() -> dict:
    """The command patterns a quiet window holds, as data a reader can apply."""
    return {
        "executables": sorted(_BUILD_EXECUTABLES),
        "tool_subcommands": {tool: sorted(words) for tool, words in sorted(_SUBCOMMAND_TOOLS.items())},
        "interpreters": "python, pypy, bash, zsh, sh, dash, node, uv, perl, ruby",
        "interpreter_runner_words": list(RUNNER_WORDS),
    }


def builds_or_tests(executable: str, argv: Sequence[str]) -> bool:
    """Whether one process is building or testing, from its own argv only."""
    name = Path(str(executable)).name.casefold()
    if name in _BUILD_EXECUTABLES:
        return True
    words = [str(token) for token in argv]
    if name in _SUBCOMMAND_TOOLS:
        subcommands = [w for w in words[1:4] if not w.startswith("-")]
        return any(w in _SUBCOMMAND_TOOLS[name] for w in subcommands[:2])
    if _INTERPRETERS.fullmatch(name):
        text = " ".join(words[1:]).casefold()
        return _RUNNER_WORDS.search(text) is not None
    return False


@dataclass(frozen=True)
class ProcessRow:
    pid: int
    ppid: int
    state: str
    cpu_seconds: Optional[float]
    started_at: Optional[float]
    start_resolution: float
    start_token: str
    cwd: Optional[str]
    executable: str
    argv: Tuple[str, ...]
    uid: Optional[int] = None


@dataclass(frozen=True)
class QuietParameters:
    load_bound: float
    settle_cap_seconds: float
    cap_seconds: float
    interval_seconds: float
    release_file: Optional[str]
    exempt: Tuple[str, ...]
    trigger: str = "operator"
    pressure_metric: Optional[str] = None
    pressure_threshold: Optional[float] = None
    pressure_release_below: Optional[float] = None

    def validated(self) -> "QuietParameters":
        def number(value, name, minimum, maximum, *, allow_zero=False):
            try:
                value = float(value)
            except (TypeError, ValueError):
                value = math.nan
            low_ok = value >= minimum if allow_zero else value > minimum
            if not math.isfinite(value) or not low_ok or value > maximum:
                raise ProtocolRefusal("quiet_parameter_invalid", name + " is outside its bounds",
                                      remedy="Pass " + name + " as a number above " + str(minimum)
                                      + " and at most " + str(maximum) + ".")
            return value
        release = self.release_file
        if release is not None:
            path = Path(release).expanduser()
            if not path.is_absolute() or not path.parent.is_dir():
                raise ProtocolRefusal("quiet_release_file_invalid",
                                      "the release file must be an absolute path in an existing directory",
                                      remedy="Pass --release-file as an absolute path whose directory exists.")
            release = str(path)
        exempt = tuple(sorted({validate_identifier(node, "node") for node in self.exempt}))
        load_bound = threshold = below = None
        metric = self.pressure_metric
        if self.trigger == "operator":
            load_bound = number(self.load_bound, "--load-bound", 0.0, 1e9)
            if metric is not None or self.pressure_threshold is not None:
                raise ProtocolRefusal("quiet_parameter_invalid", "pressure options need --trigger memory_pressure",
                                      remedy="Pass --trigger memory_pressure with the pressure options, or drop them.")
        elif self.trigger == "memory_pressure":
            if metric not in PRESSURE_METRICS:
                raise ProtocolRefusal("quiet_parameter_invalid", "--pressure-metric names no supported metric",
                                      remedy="Pass --pressure-metric as one of: " + ", ".join(sorted(PRESSURE_METRICS)) + ".")
            threshold = number(self.pressure_threshold, "--pressure-threshold", 0.0, 1e9)
            below = threshold if self.pressure_release_below is None else number(
                self.pressure_release_below, "--pressure-release-below", 0.0, 1e9)
            if below > threshold:
                raise ProtocolRefusal("quiet_parameter_invalid", "--pressure-release-below is above the threshold",
                                      remedy="Pass --pressure-release-below at or under --pressure-threshold.")
            if self.load_bound is not None:
                load_bound = number(self.load_bound, "--load-bound", 0.0, 1e9)
        else:
            raise ProtocolRefusal("quiet_parameter_invalid", "--trigger names no supported trigger",
                                  remedy="Pass --trigger operator or --trigger memory_pressure.")
        return QuietParameters(
            load_bound=load_bound,
            settle_cap_seconds=number(self.settle_cap_seconds, "--settle-cap-seconds", 0.0, 86400.0, allow_zero=True),
            cap_seconds=number(self.cap_seconds, "--cap-seconds", 0.0, 86400.0),
            interval_seconds=number(self.interval_seconds, "--interval-seconds", 0.0, 3600.0),
            release_file=release, exempt=exempt, trigger=self.trigger, pressure_metric=metric,
            pressure_threshold=threshold, pressure_release_below=below)


def _now_text(wall: float) -> str:
    moment = datetime.fromtimestamp(wall, timezone.utc)
    return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _round(value: Optional[float]) -> Optional[float]:
    return None if value is None else round(float(value), 6)


# ---------------------------------------------------------------------------
# Native process tables.


def parse_darwin_cpu_time(cell: str) -> Optional[float]:
    """Parse ps(1) ``time`` ([[dd-]hh:]mm:ss[.ss]) into seconds, or None."""
    match = re.fullmatch(r"(?:([0-9]+)-)?(?:([0-9]+):)?([0-9]+):([0-9]+(?:\.[0-9]+)?)", str(cell).strip())
    if match is None:
        return None
    days, hours, minutes, seconds = match.groups()
    if float(seconds) >= 60:
        return None
    return (int(days or 0) * 86400 + int(hours or 0) * 3600 + int(minutes) * 60 + float(seconds))


def _unknown(detail: str) -> ProtocolRefusal:
    return ProtocolRefusal("quiet_process_table_unknown", detail,
                           remedy="Restore a readable native process table for this account, then retry.")


def _linux_boot_id() -> str:
    boot_id = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    if not re.fullmatch(r"[0-9a-f-]{36}", boot_id):
        raise OSError(errno.EIO, "unreadable kernel boot identity")
    return boot_id


def _linux_scope():
    status = Path("/proc/self/status").read_bytes().splitlines()
    nspid = next((line.split()[1:] for line in status if line.startswith(b"NSpid:")), [])
    if nspid != [str(os.getpid()).encode("ascii")]:
        raise OSError(errno.ESTALE, "procfs and the caller do not share a PID namespace")
    return f"linux:{_linux_boot_id()}:{os.stat('/proc/self/ns/pid').st_ino}"


def _linux_in_scope(token, scope):
    if token.isdecimal():
        # Historical tick-only receipts can prove a mismatch but cannot authorize a signal.
        return True
    parts = token.split(":")
    if len(parts) != 4 or parts[0] != "linux":
        raise OSError(errno.ESTALE, "the recorded process identity cannot be authenticated")
    if parts[1] != scope.split(":")[1]:
        return False
    if token.rsplit(":", 1)[0] != scope:
        raise OSError(errno.ESTALE, "the target belongs to another PID namespace")
    return True


def _linux_table() -> List[ProcessRow]:
    uid = os.getuid()
    ticks = os.sysconf("SC_CLK_TCK")
    try:
        scope = _linux_scope()
        boot = next(int(line.split()[1]) for line in Path("/proc/stat").read_text().splitlines()
                    if line.startswith("btime "))
        with os.scandir("/proc") as entries:
            pids = sorted(int(entry.name) for entry in entries if entry.name.isdigit())
    except (OSError, StopIteration, ValueError, IndexError) as exc:
        raise _unknown("the native /proc table is unreadable") from exc
    if len(pids) > MAX_PROCESSES:
        raise _unknown("the native process table exceeds its bound")
    rows = []
    for pid in pids:
        directory = Path("/proc") / str(pid)
        try:
            # Keep stat, argv and cwd on the same /proc directory. A dead
            # directory descriptor never follows a replacement numeric PID.
            descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        except OSError:
            continue
        try:
            directory = Path("/proc/self/fd") / str(descriptor)
            owner = next(int(line.split()[2]) for line in (directory / "status").read_text().splitlines()
                         if line.startswith("Uid:"))
            text = (directory / "stat").read_text()
            head, _separator, tail = text.rpartition(") ")
            cells = tail.split()
            name = head.partition(" (")[2]
            state, ppid = cells[0], int(cells[1])
            cpu = (int(cells[11]) + int(cells[12])) / ticks
            start_ticks = int(cells[19])
            argv = tuple(part for part in (directory / "cmdline").read_bytes()[:65536]
                         .decode("utf-8", "replace").split("\0") if part)
            cwd = None
            if owner == uid:
                try:
                    cwd = os.readlink(directory / "cwd")
                except OSError:
                    cwd = None
        except (FileNotFoundError, ProcessLookupError, StopIteration):
            continue
        except (OSError, ValueError, IndexError):
            continue
        finally:
            os.close(descriptor)
        executable = Path(argv[0]).name if argv else name
        rows.append(ProcessRow(pid, ppid, state, cpu, boot + start_ticks / ticks, 1.0 + 1.0 / ticks,
                               f"{scope}:{start_ticks}", cwd, executable or name, argv or (name,), owner))
    return rows


def _linux_pidfd_call(name, *arguments):
    # Some supported Python builds omit pidfd wrappers even on a host
    # whose libc exposes them. Both interfaces act on kernel process handles.
    operation = getattr(ctypes.CDLL(None, use_errno=True), name, None)
    if operation is None:
        raise OSError(errno.ENOTSUP, "identity-bound signals are unavailable")
    result = operation(*arguments)
    if result < 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))
    return result


def _linux_pidfd_open(pid):
    if hasattr(os, "pidfd_open"):
        return os.pidfd_open(pid)
    return _linux_pidfd_call("pidfd_open", pid, 0)


def _linux_status(pid: int, scope: str):
    try:
        text = Path("/proc/%d/stat" % pid).read_bytes()
    except (FileNotFoundError, ProcessLookupError):
        # A missing stat entry can be a visibility restriction. The kernel
        # handle proves absence or exit independently of procfs visibility.
        try:
            descriptor = _linux_pidfd_open(pid)
        except ProcessLookupError:
            return None
        try:
            poller = select.poll()
            poller.register(descriptor, select.POLLIN)
            if any(events & (select.POLLIN | select.POLLHUP) for _, events in poller.poll(0)):
                return None
            raise OSError(errno.EACCES, "the live process identity is unreadable")
        finally:
            os.close(descriptor)
    try:
        cells = text.rpartition(b") ")[2].split()
        state = cells[0].decode("ascii")
        ticks = int(cells[19])
        if state in _TERMINAL_STATES:
            return None
        return f"{scope}:{ticks}", state
    except (ValueError, IndexError) as exc:
        raise OSError(errno.EIO, "invalid native process identity") from exc


def _ps(arguments: Sequence[str]) -> str:
    try:
        result = subprocess.run(["/bin/ps", *arguments], capture_output=True, timeout=30,
                                env={"LC_ALL": "C", "PATH": "/usr/bin:/bin"})
    except (OSError, subprocess.SubprocessError) as exc:
        raise _unknown("ps(1) is unavailable") from exc
    if result.returncode not in (0, 1):
        raise _unknown("ps(1) failed")
    return result.stdout.decode("utf-8", "replace")


def _darwin_start(cells: Sequence[str]) -> Optional[float]:
    try:
        return time.mktime(time.strptime(" ".join(cells), "%a %b %d %H:%M:%S %Y"))
    except (ValueError, OverflowError):
        return None


def parse_darwin_table(text: str, uid: int, cwds: Dict[int, str]) -> List[ProcessRow]:
    """Parse ``ps -A -ww -o pid=,ppid=,uid=,state=,time=,lstart=,args=`` rows."""
    rows = []
    for line in text.splitlines():
        cells = line.split()
        if len(cells) < 11 or not all(c.lstrip("-").isdigit() for c in cells[:3]):
            continue
        pid, ppid, owner = int(cells[0]), int(cells[1]), int(cells[2])
        if pid <= 0:
            continue
        cpu = parse_darwin_cpu_time(cells[4])
        started = _darwin_start(cells[5:10])
        argv = tuple(cells[10:])
        rows.append(ProcessRow(pid, ppid, cells[3][:1], cpu, started, 1.0,
                               " ".join(cells[5:10]), cwds.get(pid) if owner == uid else None,
                               Path(argv[0]).name, argv, owner))
    return rows


def _darwin_status(pid: int):
    # XNU PROC_PIDT_BSDINFOWITHUNIQID contains proc_bsdinfo (136 bytes)
    # and proc_uniqidentifierinfo (56 bytes) from the same process reference.
    from .codex_wait_liveness import _ProcBsdInfo, _LIBPROC
    size = ctypes.sizeof(_ProcBsdInfo)
    raw = ctypes.create_string_buffer(size + 56)
    ctypes.set_errno(0)
    got = _LIBPROC.proc_pidinfo(ctypes.c_int(pid), ctypes.c_int(18), ctypes.c_uint64(0),
                                ctypes.byref(raw), ctypes.c_int(len(raw)))
    if got == 0 and ctypes.get_errno() in (errno.ESRCH, errno.ENOENT):
        return None
    if size != 136 or got != len(raw):
        raise OSError(ctypes.get_errno() or errno.EIO, "unreadable native process identity")
    info = _ProcBsdInfo.from_buffer(raw)
    unique, = struct.unpack_from("=Q", raw, size + 16)
    version, = struct.unpack_from("=I", raw, size + 32)
    if (info.pbi_pid != pid or not unique or not info.pbi_start_tvsec
            or info.pbi_start_tvusec >= 1_000_000 or info.pbi_status not in (1, 2, 3, 4, 5)):
        raise OSError(errno.EIO, "invalid native process identity")
    if info.pbi_status == 5:
        return None
    token = f"darwin:{info.pbi_start_tvsec}:{info.pbi_start_tvusec}:{unique}:{version}"
    return token, "T" if info.pbi_status == 4 else "R"


def _darwin_table() -> List[ProcessRow]:
    from .seat_teardown_activity import _darwin_snapshot
    uid = os.getuid()
    before = {}
    for line in _ps(["-A", "-o", "pid=,uid="]).splitlines():
        cells = line.split()
        if len(cells) == 2 and cells[0].isdigit() and cells[1] == str(uid):
            pid = int(cells[0])
            try:
                observed = _darwin_status(pid)
            except OSError:
                continue
            if observed:
                before[pid] = observed[0]
    try:
        located, _ancestry, _uncertain = _darwin_snapshot()
    except ProtocolRefusal as exc:
        raise _unknown("native cwd observation failed: " + exc.detail) from exc
    cwds = {row["pid"]: row["cwd"] for row in located}
    rows = parse_darwin_table(_ps(["-A", "-ww", "-o", "pid=,ppid=,uid=,state=,time=,lstart=,args="]), uid, cwds)
    bound = []
    for row in rows:
        if row.uid != uid:
            bound.append(row)
            continue
        if row.pid not in before:
            continue
        try:
            after = _darwin_status(row.pid)
        except OSError:
            continue
        if after and before[row.pid] == after[0]:
            bound.append(replace(row, start_token=after[0]))
    return bound


def read_memory_pressure(metric: str) -> Optional[float]:
    """One measured memory-pressure reading, or None when this host has none."""
    if metric in ("psi_memory_some_avg10", "psi_memory_full_avg10"):
        if not sys.platform.startswith("linux"):
            return None
        wanted = "some" if metric.endswith("some_avg10") else "full"
        try:
            for line in Path("/proc/pressure/memory").read_text().splitlines():
                cells = line.split()
                if cells and cells[0] == wanted:
                    fields = dict(cell.split("=", 1) for cell in cells[1:] if "=" in cell)
                    value = float(fields["avg10"])
                    return value if math.isfinite(value) else None
        except (OSError, KeyError, ValueError):
            return None
        return None
    if metric == "vm_pressure_level":
        if sys.platform != "darwin":
            return None
        try:
            result = subprocess.run(["/usr/sbin/sysctl", "-n", "kern.memorystatus_vm_pressure_level"],
                                    capture_output=True, timeout=10, env={"LC_ALL": "C"})
            if result.returncode:
                return None
            return float(int(result.stdout.decode("ascii").strip()))
        except (OSError, subprocess.SubprocessError, ValueError, UnicodeDecodeError):
            return None
    return None


class NativeHost:
    """This host: its load, clocks, process table and signals."""

    def __init__(self) -> None:
        self._stop: Optional[str] = None
        self._wake: Optional[Tuple[int, int]] = None

    def loadavg(self) -> Tuple[float, float, float]:
        return os.getloadavg()

    def monotonic(self) -> float:
        return time.monotonic()

    def wall(self) -> float:
        return time.time()

    def self_pid(self) -> int:
        return os.getpid()

    def snapshot(self) -> List[ProcessRow]:
        if sys.platform.startswith("linux"):
            return _linux_table()
        if sys.platform == "darwin":
            return _darwin_table()
        raise _unknown("no native process table on " + sys.platform)

    def identity(self, pid: int) -> Optional[str]:
        if sys.platform.startswith("linux"):
            status = _linux_status(pid, _linux_scope())
        elif sys.platform == "darwin":
            status = _darwin_status(pid)
        else:
            raise OSError(errno.ENOTSUP, "process identity is unavailable")
        return None if status is None else status[0]

    def matches(self, pid: int, token: str) -> bool:
        if sys.platform.startswith("linux"):
            scope = _linux_scope()
            if not _linux_in_scope(token, scope):
                return False
            status = _linux_status(pid, scope)
            current = status[0] if status else None
        else:
            current = self.identity(pid)
        if current is None:
            return False
        if token.startswith(("linux:", "darwin:")):
            return current == token
        # Legacy frames recorded only Linux start ticks or Darwin lstart
        # (whole seconds). A mismatch can prove departure. A match cannot
        # authenticate the present occupant for a signal.
        if current.startswith("linux:") and token.isdecimal():
            if current.rsplit(":", 1)[1] != token:
                return False
        raise OSError(errno.ESTALE, "the recorded process identity cannot be authenticated")

    def signal(self, pid: int, token: str, number: int) -> bool:
        if pid <= 0:
            raise OSError(errno.EINVAL, "invalid process coordinate")
        if sys.platform.startswith("linux"):
            scope = _linux_scope()
            if not _linux_in_scope(token, scope):
                return False
            try:
                descriptor = _linux_pidfd_open(pid)
                try:
                    current = _linux_status(pid, scope)
                    if current is None:
                        return False
                    if token.isdecimal():
                        if current[0].rsplit(":", 1)[1] == token:
                            raise OSError(errno.ESTALE, "the recorded process identity cannot be authenticated")
                        return False
                    if current[0] != token:
                        return False
                    if number == signal.SIGSTOP and current[1] in _STOPPED_STATES:
                        raise OSError(errno.EALREADY, "the process is already stopped")
                    if hasattr(signal, "pidfd_send_signal"):
                        signal.pidfd_send_signal(descriptor, number)
                    else:
                        _linux_pidfd_call("pidfd_send_signal", descriptor, number, None, 0)
                finally:
                    os.close(descriptor)
            except ProcessLookupError:
                return False
            return True
        if sys.platform == "darwin":
            from .codex_wait_liveness import _LIBPROC
            if not self.matches(pid, token):
                return False
            if number == signal.SIGSTOP:
                current = _darwin_status(pid)
                if current is None or current[0] != token:
                    return False
                if current[1] in _STOPPED_STATES:
                    raise OSError(errno.EALREADY, "the process is already stopped")
            # XNU proc_find_audit_token resolves words 5 and 7 (PID and
            # PID version). psignal_by_audit_token retains that process ref
            # and checks the caller's signal privileges before delivery.
            target = (ctypes.c_uint32 * 8)()
            target[5], target[7] = pid, int(token.rsplit(":", 1)[1])
            send = getattr(_LIBPROC, "proc_signal_with_audittoken", None)
            if send is None:
                raise OSError(errno.ENOTSUP, "identity-bound signals are unavailable")
            error = send(ctypes.byref(target), ctypes.c_int(number))
            if error == errno.ESRCH:
                return False
            if error:
                raise OSError(error, os.strerror(error))
            return True
        raise OSError(errno.ENOTSUP, "identity-bound signals are unavailable")

    def pressure(self, metric: str) -> Optional[float]:
        return read_memory_pressure(metric)

    def stop_requested(self) -> Optional[str]:
        return self._stop

    def wait(self, seconds: float) -> bool:
        """Sleep up to ``seconds``; True when a release signal arrived."""
        if self._stop is not None:
            return True
        if self._wake is None:
            time.sleep(max(0.0, seconds))
            return self._stop is not None
        read, _write = self._wake
        deadline = time.monotonic() + max(0.0, seconds)
        while self._stop is None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                ready, _, _ = select.select([read], [], [], remaining)
            except InterruptedError:
                continue
            if ready:
                try:
                    os.read(read, 4096)
                except OSError:
                    pass
        return self._stop is not None

    @contextmanager
    def signal_guard(self):
        read, write = os.pipe()
        os.set_blocking(read, False)
        os.set_blocking(write, False)
        self._wake = (read, write)
        previous_fd = signal.set_wakeup_fd(write)
        numbers = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
        previous = {number: signal.getsignal(number) for number in numbers}

        def handler(number, _frame):
            self._stop = signal.Signals(number).name

        try:
            for number in numbers:
                signal.signal(number, handler)
            yield
        finally:
            for number, old in previous.items():
                signal.signal(number, old)
            signal.set_wakeup_fd(previous_fd)
            self._wake = None
            os.close(read)
            os.close(write)


# ---------------------------------------------------------------------------
# The window ledger.


def _ledger_path(root: FloatiRoot) -> Path:
    return root.resolve_relative(LEDGER_RELATIVE)


def _read(root: FloatiRoot) -> Tuple[List[dict], int]:
    """Tolerant read: no lock creation, no history rewrite."""
    try:
        with _ledger_path(root).open("rb") as stream:
            data = stream.read(MAX_LEDGER_BYTES + 1)
    except FileNotFoundError:
        return [], 0
    if len(data) > MAX_LEDGER_BYTES:
        return [], 1
    rows, partial = [], 0
    for index, raw in enumerate(data.splitlines(keepends=True)):
        if index >= MAX_LEDGER_RECORDS:
            partial += 1
            break
        try:
            row, = decode_frames(raw)
            if (not isinstance(row, dict) or row.get("kind") not in QUIET_KINDS
                    or row.get("tenant_id") != root.tenant_id or not isinstance(row.get("window_id"), str)):
                raise ValueError("unattributable quiet row")
            rows.append(row)
        except (FrameError, ValueError, TypeError):
            partial += 1
    return rows, partial


@dataclass
class _LedgerBudget:
    path: Path
    records: Optional[int] = None


@contextmanager
def _locked(root: FloatiRoot):
    path = _ledger_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _locked_path(path.with_name(path.name + ".lock"), exclusive=True,
                      relative=LEDGER_RELATIVE.as_posix() + ".lock"):
        yield _LedgerBudget(path)


def _append(root: FloatiRoot, ledger: _LedgerBudget, record: dict, *, reserve: int = 0) -> dict:
    """Admit the append and its remaining recovery frames under the writer lock."""
    encoded = _encode_record(record, root.tenant_id, QUIET_KINDS, max_bytes=MAX_RECORD_BYTES)
    path = ledger.path
    size = path.stat().st_size if path.exists() else 0
    remedy = "Preserve this full ledger and obtain an explicit retention plan before opening another window."
    if size + len(encoded) + reserve * MAX_RECORD_BYTES > MAX_LEDGER_BYTES:
        raise ProtocolRefusal("ledger_byte_limit", "quiet window ledger byte bound reached", remedy=remedy)
    if ledger.records is None:
        ledger.records = 0
        if size:
            with path.open("rb") as stream:
                ledger.records = sum(chunk.count(b"\n") for chunk in iter(lambda: stream.read(1 << 20), b""))
    if ledger.records + 1 + reserve > MAX_LEDGER_RECORDS:
        raise ProtocolRefusal("ledger_record_limit", "quiet window ledger record bound reached", remedy=remedy)
    _append_frame(path, encoded)
    ledger.records += 1
    return record


def _window_record(root, window_id, actor, phase, key, load, facts, wall):
    return {"schema_version": 1, "kind": WINDOW_KIND,
            "id": window_id if phase == "settling" else "quiet-window-" + uuid7_hex(),
            "tenant_id": root.tenant_id, "timestamp": _now_text(wall), "window_id": window_id,
            "node_id": actor, "phase": phase, "idempotency_key": key,
            "load": None if load is None else [_round(value) for value in load], "facts": facts}


def _windows(rows: Iterable[dict]) -> Dict[str, dict]:
    windows: Dict[str, dict] = {}
    for row in rows:
        window = windows.setdefault(row["window_id"], {"rows": [], "exposures": []})
        if row.get("kind") == EXPOSURE_KIND:
            window["exposures"].append(row)
        else:
            window["rows"].append(row)
    return windows


def _phase_rows(window: dict, phase: str) -> List[dict]:
    return [row for row in window["rows"] if row.get("phase") == phase]


def _driver_alive(window: dict, host) -> Optional[bool]:
    settling = _phase_rows(window, "settling")
    driver = settling[0].get("facts", {}).get("driver") if settling else None
    if not isinstance(driver, dict) or not isinstance(driver.get("pid"), int):
        return None
    token = driver.get("start_token")
    if not isinstance(token, str):
        return None
    try:
        return host.matches(driver["pid"], token)
    except OSError:
        return None


def _legacy_failed_release(row):
    facts = row.get("facts", {})
    return (row.get("phase") == "released" and not facts.get("recovery_version")
            and bool(facts.get("not_resumed_count") or facts.get("not_resumed")))


def _state(window: dict, host) -> str:
    phases = {row.get("phase") for row in window["rows"]}
    if "released" in phases:
        return "release_pending" if _legacy_failed_release(_phase_rows(window, "released")[-1]) else "released"
    if "not_opened" in phases:
        return "not_opened"
    if "release_pending" in phases:
        return "release_pending"
    if not phases & {"settling"}:
        return "unknown"
    alive = _driver_alive(window, host)
    if alive is False:
        return "orphaned"
    if alive is None:
        return "unknown"
    return "open" if "opened" in phases else "settling"


def _recorded_pauses(window: dict) -> List[dict]:
    pauses = []
    for row in _phase_rows(window, "paused"):
        for pause in row.get("facts", {}).get("pauses", []):
            if isinstance(pause, dict) and isinstance(pause.get("pid"), int) and isinstance(pause.get("start_token"), str):
                pauses.append(pause)
    return pauses


def _outstanding(window):
    """Only a stop confirmed after its intent grants automatic continue authority."""
    ambiguous, confirmed = {}, {}
    for row in window["rows"]:
        phase = row.get("phase")
        if _legacy_failed_release(row):
            # The old terminal writer conflated probe failure with departure.
            # If its detail was truncated keep every possible unsuccessful target.
            facts = row["facts"]
            missed = facts.get("not_resumed", [])
            if len(missed) == facts.get("not_resumed_count", len(missed)):
                pids = {entry["pid"] for entry in missed}
                ambiguous = {key: pause for key, pause in ambiguous.items() if key[0] in pids}
        for pause in row.get("facts", {}).get("pauses", []):
            key = (pause["pid"], pause["start_token"])
            if phase == "pause_intent":
                ambiguous[key] = pause
            elif phase == "paused":
                if (pause.get("intent_id") and key in ambiguous
                        and pause["intent_id"] == ambiguous[key].get("intent_id")):
                    confirmed[key] = ambiguous.pop(key)
                else:
                    # Original writers used "paused" as a batch write-ahead record.
                    ambiguous[key] = pause
            elif phase == "resume_intent" and key in confirmed:
                ambiguous[key] = confirmed.pop(key)
            elif phase == "resume_refused" and key in ambiguous:
                confirmed[key] = ambiguous.pop(key)
            elif phase in ("pause_cancelled", "pause_resolved"):
                confirmed.pop(key, None)
                ambiguous.pop(key, None)
    return list(confirmed.values()), list(ambiguous.values())


def _resolve_pause(root, path, window_id, actor, key, pause, outcome, host):
    _append(root, path, _window_record(root, window_id, actor, "pause_resolved", key, None,
                                      {"pauses": [pause], "outcome": outcome}, host.wall()))


def _projection(root: FloatiRoot, window_id: str, window: dict, host, *, detail: bool) -> dict:
    settling = _phase_rows(window, "settling")
    opened = _phase_rows(window, "opened")
    terminal = [row for row in window["rows"] if row.get("phase") in TERMINAL_PHASES]
    facts = terminal[-1].get("facts", {}) if terminal else {}
    result = {
        "window_id": window_id,
        "state": _state(window, host),
        "node_id": settling[0].get("node_id") if settling else None,
        "idempotency_key": settling[0].get("idempotency_key") if settling else None,
        "requested_at": settling[0].get("timestamp") if settling else None,
        "opened_at": opened[0].get("facts", {}).get("opened_at") if opened else None,
        "released_at": facts.get("released_at"),
        "cause": facts.get("cause"),
        "paused_processes": len(_recorded_pauses(window)),
        "exposure_receipts": len(window["exposures"]),
    }
    if result["state"] not in TERMINAL_PHASES and settling:
        result["released_at"] = None
        result["ambiguous_targets"] = _ambiguous_targets(
            root, window_id, settling[0]["node_id"], _outstanding(window)[1])
    if detail:
        result["parameters"] = settling[0].get("facts", {}).get("parameters") if settling else None
        result["seats"] = opened[0].get("facts", {}).get("seats") if opened else None
        result["resumed"] = facts.get("resumed", [])
        result["not_resumed"] = facts.get("not_resumed", [])
        result["exposures"] = [_exposure_view(row) for row in window["exposures"]]
    return result


def _exposure_view(row: dict) -> dict:
    return {key: row.get(key) for key in (
        "id", "node_id", "scope", "observation", "held", "hold_reason", "interval", "cpu",
        "processes", "processes_truncated")}


def show_windows(root: FloatiRoot, window_id: Optional[str] = None, *, host=None) -> dict:
    host = NativeHost() if host is None else host
    rows, partial = _read(root)
    windows = _windows(rows)
    if window_id is not None:
        if window_id not in windows:
            raise ProtocolRefusal("quiet_window_unknown", "no quiet window has this ID in this root",
                                  remedy="Run floati quiet show --root ROOT to list this root's windows.")
        return {"window": _projection(root, window_id, windows[window_id], host, detail=True),
                "partial_records": partial}
    return {"windows": [_projection(root, ident, window, host, detail=False) for ident, window in windows.items()],
            "partial_records": partial}


def _command_answer(command: Optional[str], active: Optional[bool]) -> Optional[dict]:
    if command is None:
        return None
    try:
        argv = shlex.split(command)
    except ValueError:
        argv = command.split()
    covered = bool(argv) and builds_or_tests(Path(argv[0]).name, argv)
    return {"covered": covered, "waits": bool(active) and covered}


def quiet_status(root: FloatiRoot, *, host=None, command: Optional[str] = None) -> dict:
    """The pull read any agent can make before heavy work, with no hook.

    Floati holds only its own enrolled seats; everyone else is told here.
    """
    host = NativeHost() if host is None else host
    rows, partial = _read(root)
    current = None
    ambiguous = []
    for window_id, window in _windows(rows).items():
        state = _state(window, host)
        if state in ("settling", "open", "orphaned", "unknown", "release_pending"):
            current = (window_id, window, state)
            settling = _phase_rows(window, "settling")
            if settling:
                ambiguous.extend(_ambiguous_targets(root, window_id, settling[0]["node_id"],
                                                    _outstanding(window)[1]))
    if current is None:
        quiet = {"active": None if partial else False, "state": "unknown" if partial else "none",
                 "absent": "history_partial" if partial else None}
        return {"quiet": quiet, "command": _command_answer(command, quiet["active"]),
                "partial_records": partial}
    window_id, window, state = current
    settling = _phase_rows(window, "settling")
    opened = _phase_rows(window, "opened")
    parameters = settling[0].get("facts", {}).get("parameters") or {} if settling else {}
    opened_at = opened[0].get("facts", {}).get("opened_at") if opened else None
    cap_at = None
    if opened_at is not None and isinstance(parameters.get("cap_seconds"), (int, float)):
        try:
            moment = datetime.fromisoformat(opened_at.replace("Z", "+00:00"))
            cap_at = _now_text(moment.timestamp() + float(parameters["cap_seconds"]))
        except (ValueError, OverflowError):
            cap_at = None
    seats = opened[0].get("facts", {}).get("seats", {}) if opened else {}
    quiet = {
        "active": True, "state": state, "window_id": window_id,
        "node_id": settling[0].get("node_id") if settling else None,
        "requested_at": settling[0].get("timestamp") if settling else None,
        "opened_at": opened_at, "cap_at": cap_at,
        "release_file": parameters.get("release_file"),
        "held": "enrolled_seats_building_or_testing",
        "outside_reach": "observed_not_held",
        "holdable_seats": seats.get("holdable", []),
        "trigger": (opened[0].get("facts", {}).get("trigger") if opened else None)
        or {"kind": parameters.get("trigger", "operator")},
        "covered_commands": covered_commands(),
        "guidance": "defer_builds_and_tests_until_released",
        "absent": None,
    }
    quiet["ambiguous_targets"] = ambiguous
    if state in ("orphaned", "release_pending"):
        quiet["remedy"] = _release_command(root, quiet["node_id"] or "NODE", window_id)
    return {"quiet": quiet, "command": _command_answer(command, True), "partial_records": partial}


def _release_command(root: FloatiRoot, actor: str, window_id: str) -> str:
    return shlex.join(["floati", "quiet", "release", "--root", str(root.path), "--as", actor,
                       "--window", window_id, "--idempotency-key", "quiet-release-" + uuid7_hex()])


def _ambiguous_targets(root, window_id, actor, pauses):
    """Project exact coordinates and a shell-quoted command for each unconfirmed hold."""
    return [
        dict(pause, started_at=pause.get("started_at"), window_id=window_id,
             resolve_command=_release_command(root, actor, window_id) + " " + shlex.join(
                 ["--resolve-pid", str(pause["pid"]), "--start-token", pause["start_token"]]))
        for pause in pauses
    ]


def _target_result(root, window_id, actor, pause, outcome, remaining, *, replayed):
    result = {"window_id": window_id, "pid": pause["pid"], "start_token": pause["start_token"],
              "outcome": outcome, "replayed": replayed,
              "ambiguous_targets": _ambiguous_targets(root, window_id, actor, remaining),
              "release_command": _release_command(root, actor, window_id)}
    if outcome == "resolved_by_exit":
        error = ProtocolRefusal("quiet_target_changed",
                                "The recorded process exited or its identity changed.",
                                remedy=result["release_command"])
        error.refusal_detail = result
        raise error
    return result


def _resolution_replay(root, window_id, window, actor, key, pid, token):
    for row in window["rows"]:
        if row.get("idempotency_key") != key:
            continue
        facts = row.get("facts", {})
        pauses = facts.get("pauses", [])
        if (row.get("phase") == "pause_resolved" and row.get("node_id") == actor
                and facts.get("outcome") in ("operator_resumed", "resolved_by_exit")
                and len(pauses) == 1 and pauses[0].get("operator_selected") is True
                and (pauses[0]["pid"], pauses[0]["start_token"]) == (pid, token)):
            return _target_result(root, window_id, actor, pauses[0], facts["outcome"],
                                  _outstanding(window)[1], replayed=True)
        raise ProtocolRefusal("quiet_key_conflict", "This key already names another quiet window action.",
                              remedy="Use the resolution command from floati quiet status.")
    return None


def _resolve_target(root, path, window_id, window, actor, key, pid, token, host):
    """Resolve one explicitly selected unconfirmed hold while the ledger lock is held."""
    _, ambiguous = _outstanding(window)
    pause = next((target for target in ambiguous
                  if (target["pid"], target["start_token"]) == (pid, token)), None)
    if pause is None:
        raise ProtocolRefusal("quiet_target_unknown", "No unconfirmed pause matches this process identity.",
                              remedy="Use the resolution command from floati quiet status.")
    try:
        delivered = host.signal(pid, token, signal.SIGCONT)
    except OSError:
        error = ProtocolRefusal("quiet_target_unresolved", "The host could not continue this process.",
                                remedy="Check process access and retry this resolution.")
        error.refusal_detail = {"ambiguous_targets": _ambiguous_targets(root, window_id, actor, ambiguous)}
        raise error from None
    outcome = "operator_resumed" if delivered else "resolved_by_exit"
    _resolve_pause(root, path, window_id, actor, key, dict(pause, operator_selected=True), outcome, host)
    remaining = [other for other in ambiguous if other is not pause]
    return _target_result(root, window_id, actor, pause, outcome, remaining, replayed=False)


# ---------------------------------------------------------------------------
# One window.


class _Tracked:
    __slots__ = ("node", "pid", "token", "executable", "heavy", "baseline", "exact",
                 "last_cpu", "paused", "seen_last_scan", "own_account")

    def __init__(self, node, row: ProcessRow, baseline, exact):
        self.node, self.pid, self.token = node, row.pid, row.start_token
        self.own_account = row.uid in (None, os.getuid())
        self.executable, self.heavy = row.executable, False
        self.baseline, self.exact = baseline, exact
        self.last_cpu, self.paused, self.seen_last_scan = row.cpu_seconds, False, True


class QuietWindow:
    def __init__(self, root: FloatiRoot, actor: str, idempotency_key: str,
                 parameters: QuietParameters, *, host=None) -> None:
        self.root = root
        self.registry = Registry(root)
        self.actor = validate_identifier(actor, "node")
        self.key = validate_identifier(idempotency_key, "idempotency_key")
        self.parameters = parameters.validated()
        self.host = NativeHost() if host is None else host

    # -- admission ----------------------------------------------------------

    def _admit(self) -> Tuple[Optional[dict], Dict[str, List[Path]], Tuple[str, ...]]:
        try:
            self.registry.require_active(self.actor)
        except ProtocolRefusal:
            raise ProtocolRefusal("quiet_node_unknown", "a quiet window needs a registered active operator node",
                                  remedy="Pass --as a registered active node of this root.") from None
        governance = self.registry.governance()
        if governance is not None and governance.coordinator != self.actor:
            raise ProtocolRefusal("quiet_actor_not_coordinator",
                                  "this fleet declares a coordinator; only it may hold every seat",
                                  remedy="Run the window as the declared coordinator " + governance.coordinator + ".")
        active = self.registry.active_node_ids()
        unknown = [node for node in self.parameters.exempt if node not in active]
        if unknown:
            raise ProtocolRefusal("quiet_exempt_unknown", "an exempt node is not an active node of this root",
                                  remedy="Pass --exempt only for active nodes: " + ", ".join(unknown) + ".")
        from .codex_wait_contract import mapped_workspaces
        workspaces: Dict[str, List[Path]] = {}
        for workspace, node in mapped_workspaces(self.root.path):
            if node not in active:
                continue
            try:
                canonical = Path(workspace).resolve(strict=True)
            except (OSError, RuntimeError):
                continue
            if canonical.is_dir():
                workspaces.setdefault(node, []).append(canonical)
        return None, workspaces, active

    def _parameters_fact(self) -> dict:
        return {key: (list(value) if isinstance(value, tuple) else value)
                for key, value in asdict(self.parameters).items()}

    # -- process attribution --------------------------------------------------

    def _attribute(self, rows: Sequence[ProcessRow]) -> Tuple[Dict[int, ProcessRow], Dict[int, str], set]:
        table = {row.pid: row for row in rows}
        mapping = sorted(((path, node) for node, paths in self.workspaces.items() for path in paths),
                         key=lambda item: len(item[0].parts), reverse=True)
        seat_by_cwd: Dict[int, Optional[str]] = {}
        uid = os.getuid()
        for row in rows:
            seat = None
            if row.uid not in (None, uid):
                # Another account's process is outside the window's reach.
                seat_by_cwd[row.pid] = None
                continue
            if row.cwd:
                try:
                    cwd = Path(row.cwd).resolve()
                except (OSError, RuntimeError):
                    cwd = Path(row.cwd)
                for path, node in mapping:
                    if cwd == path or path in cwd.parents:
                        seat = node
                        break
            seat_by_cwd[row.pid] = seat
        seats: Dict[int, str] = {}

        def seat_of(pid, depth=0):
            if pid in seats:
                return seats[pid]
            if pid not in table or depth > 512 or table[pid].uid not in (None, uid):
                return None
            seat = seat_by_cwd.get(pid)
            if seat is None:
                parent = table[pid].ppid
                seat = seat_of(parent, depth + 1) if parent != pid else None
            if seat is not None:
                seats[pid] = seat
            return seat

        for row in rows:
            seat_of(row.pid)
        # The window's own tree: its ancestry and every descendant.
        own = set()
        pid, steps = self.host.self_pid(), 0
        while pid in table and pid not in own and steps < 512:
            own.add(pid)
            pid, steps = table[pid].ppid, steps + 1
        own.add(self.host.self_pid())
        children: Dict[int, List[int]] = {}
        for row in rows:
            children.setdefault(row.ppid, []).append(row.pid)
        stack = [self.host.self_pid()]
        while stack:
            current = stack.pop()
            for child in children.get(current, ()):
                if child not in own:
                    own.add(child)
                    stack.append(child)
        self._children = children
        return table, seats, own

    def _hold_reason(self, node: str) -> str:
        if node == self.actor:
            return "actor"
        if node in self.parameters.exempt:
            return "exempt"
        if node not in self.workspaces:
            return "no_workspace"
        return "writing_only"

    # -- scans ----------------------------------------------------------------

    def _scan(self, *, pause: bool) -> None:
        try:
            rows = self.host.snapshot()
        except ProtocolRefusal:
            self.scans_failed += 1
            return
        self.scans += 1
        table, seats, own = self._attribute(rows)
        for tracked in self.tracked.values():
            tracked.seen_last_scan = False
        for row in rows:
            # node None: work outside every enrolled seat, observed, never held.
            node = seats.get(row.pid)
            if row.pid in own or row.state[:1] in _TERMINAL_STATES:
                continue
            identity = (row.pid, row.start_token)
            tracked = self.tracked.get(identity)
            if tracked is None:
                if self.opening_scan:
                    baseline, exact = row.cpu_seconds, row.cpu_seconds is not None
                elif row.started_at is not None and row.started_at - row.start_resolution >= self.opened_wall:
                    baseline, exact = 0.0, row.cpu_seconds is not None
                else:
                    baseline, exact = row.cpu_seconds, False
                tracked = self.tracked[identity] = _Tracked(node, row, baseline, exact)
            if row.cpu_seconds is not None:
                tracked.last_cpu = row.cpu_seconds
            tracked.seen_last_scan = True
            if builds_or_tests(row.executable, row.argv):
                tracked.heavy = True
        if pause:
            self._pause(table, seats, own)

    def _pause(self, table, seats, own) -> None:
        ours = {(t.pid, t.token) for t in self.tracked.values() if t.paused}
        # A descendant can change into a different mapped workspace. Keep
        # the protected ancestry even when direct cwd attribution changes.
        protected = set()
        pending = [pid for pid, node in seats.items()
                   if node == self.actor or node in self.parameters.exempt]
        while pending:
            pid = pending.pop()
            if pid not in protected:
                protected.add(pid)
                pending.extend(self._children.get(pid, ()))
        roots: List[int] = []
        for pid, row in table.items():
            node = seats.get(pid)
            if (node is None or pid in own or pid in protected or self._hold_reason(node) != "writing_only"
                    or row.state[:1] in _STOPPED_STATES | _TERMINAL_STATES):
                continue
            parent = table.get(row.ppid)
            under_ours = False
            probe, steps = row, 0
            while probe is not None and steps < 512:
                if (probe.pid, probe.start_token) in ours:
                    under_ours = True
                    break
                probe, steps = table.get(probe.ppid), steps + 1
            if under_ours or builds_or_tests(row.executable, row.argv):
                top = row
                while True:
                    parent = table.get(top.ppid)
                    if (parent is None or parent.pid in own or seats.get(parent.pid) != node
                            or parent.state[:1] in _TERMINAL_STATES):
                        break
                    if parent.state[:1] in _STOPPED_STATES and (parent.pid, parent.start_token) not in ours:
                        break
                    top = parent
                roots.append(top.pid)
        selected: List[ProcessRow] = []
        chosen = set()
        for start in roots:
            queue = [start]
            while queue:
                pid = queue.pop(0)
                row = table.get(pid)
                if (row is None or pid in chosen or pid in own or pid in protected
                        or seats.get(pid) != seats[start]):
                    continue
                chosen.add(pid)
                if (row.state[:1] not in _STOPPED_STATES | _TERMINAL_STATES
                        and (row.pid, row.start_token) not in ours):
                    selected.append(row)
                queue.extend(self._children.get(pid, ()))
        if not selected:
            return
        with _locked(self.root) as path:
            for row in selected:
                if self._release_boundary():
                    break
                node = seats[row.pid]
                pause = {"node_id": node, "pid": row.pid, "start_token": row.start_token,
                         "started_at": row.started_at, "executable": row.executable[:128],
                         "intent_id": uuid7_hex()}
                facts = {"pauses": [pause]}
                _append(self.root, path, _window_record(
                    self.root, self.window_id, self.actor, "pause_intent", self.key,
                    None, facts, self.host.wall()), reserve=2 * len(ours) + 3 + self.close_budget)
                delivered = False
                if not self._release_boundary():
                    try:
                        delivered = self.host.signal(row.pid, row.start_token, signal.SIGSTOP)
                    except OSError as exc:
                        facts = dict(facts, reason="signal_refused", errno=exc.errno)
                        # The host refused delivery. A process death before this
                        # cancellation is durable leaves only the original intent.
                        pass
                phase = "paused" if delivered else "pause_cancelled"
                _append(self.root, path, _window_record(
                    self.root, self.window_id, self.actor, phase, self.key, None, facts, self.host.wall()))
                if delivered:
                    identity = (row.pid, row.start_token)
                    self.tracked[identity].paused = True
                    ours.add(identity)
                    self.held_seats.add(node)

    # -- the run --------------------------------------------------------------

    def _replay(self, rows: List[dict]) -> Optional[dict]:
        windows = _windows(rows)
        for window_id, window in windows.items():
            settling = _phase_rows(window, "settling")
            if not settling or settling[0].get("idempotency_key") != self.key:
                continue
            prior = settling[0].get("facts", {}).get("parameters")
            if prior != self._parameters_fact() or settling[0].get("node_id") != self.actor:
                raise ProtocolRefusal("quiet_key_conflict", "this idempotency key already names a different window",
                                      remedy="Replay the original request with this key, or choose a fresh key.")
            state = _state(window, self.host)
            if state == "not_opened":
                cause = _phase_rows(window, "not_opened")[-1].get("facts", {}).get("cause")
                raise ProtocolRefusal(
                    "quiet_window_interrupted" if cause == "signal" else "quiet_settle_cap_reached",
                    "the window with this key never opened; nothing was held (window %s)" % window_id,
                    remedy="Retry later with a fresh idempotency key.")
            if state in TERMINAL_PHASES:
                return dict(_projection(self.root, window_id, window, self.host, detail=True), replayed=True)
            raise ProtocolRefusal("quiet_window_open", "the window with this key is still in progress",
                                  remedy="Touch its release file or wait for its cap; read it with floati quiet show.")
        return None

    def _refuse_live_windows(self, rows: List[dict]) -> None:
        for window_id, window in _windows(rows).items():
            state = _state(window, self.host)
            if state in ("open", "settling"):
                raise ProtocolRefusal("quiet_window_open", "another quiet window is in progress in this root: " + window_id,
                                      remedy="Wait for it to release, or touch its release file.")
            if state in ("orphaned", "unknown", "release_pending"):
                actor = _phase_rows(window, "settling")[0].get("node_id") or self.actor
                raise ProtocolRefusal("quiet_window_orphaned",
                                      "a quiet window's driver died, or cannot be shown alive, before it released: "
                                      + window_id,
                                      remedy=_release_command(self.root, actor, window_id))

    def run(self) -> dict:
        _none, self.workspaces, self.active = self._admit()
        guard = getattr(self.host, "signal_guard", None)
        with (guard() if guard is not None else _null()):
            return self._run()

    def _stop(self) -> Optional[str]:
        probe = getattr(self.host, "stop_requested", None)
        return probe() if probe is not None else None

    def _run(self) -> dict:
        host, parameters = self.host, self.parameters
        self.window_id = "quiet-window-" + uuid7_hex()
        driver_pid = host.self_pid()
        pressure = parameters.trigger == "memory_pressure"
        self.trigger = {"kind": parameters.trigger}
        if pressure:
            self.trigger.update({"metric": parameters.pressure_metric, "threshold": parameters.pressure_threshold,
                                 "release_below": parameters.pressure_release_below, "set_reading": None,
                                 "release_reading": None, "reads_failed": 0})
        with _locked(self.root) as path:
            rows, partial = _read(self.root)
            if partial:
                raise ProtocolRefusal("quiet_history_uncertain",
                                      "opening a window requires complete window history; reads remain available",
                                      remedy="Inspect the quiet window ledger before opening another window; preserve its history.")
            replay = self._replay(rows)
            if replay is not None:
                return replay
            self._refuse_live_windows(rows)
            if pressure and self._pressure() is None:
                raise ProtocolRefusal("quiet_pressure_unavailable",
                                      "this host gives no reading for " + str(parameters.pressure_metric) + "; nothing was held",
                                      remedy="Choose a --pressure-metric this host measures, or pass --trigger operator.")
            # One exposure per seat and one outside, the terminal frame, and
            # one nonterminal release marker if any resume needs a retry.
            self.close_budget = len(self.active) + 3
            try:
                driver_token = host.identity(driver_pid)
            except OSError as exc:
                raise _unknown("cannot identify the quiet window driver") from exc
            if driver_token is None:
                raise _unknown("cannot identify the quiet window driver")
            load = host.loadavg()
            _append(self.root, path, _window_record(
                self.root, self.window_id, self.actor, "settling", self.key, load,
                {"parameters": self._parameters_fact(),
                 "driver": {"pid": driver_pid, "start_token": driver_token}}, host.wall()),
                reserve=1 + self.close_budget)
        # Settle BEFORE holding: nothing is paused until the 15-minute load
        # is at or under the bound (operator trigger), or until measured
        # memory pressure reaches the declared threshold (pressure trigger);
        # the window gives up at its cap either way.
        started = host.monotonic()
        while True:
            load = host.loadavg()
            if pressure:
                reading = self._pressure()
                if reading is not None and reading >= parameters.pressure_threshold:
                    self.trigger["set_reading"] = _round(reading)
                    break
            elif load[2] <= parameters.load_bound:
                break
            elapsed = host.monotonic() - started
            cause = "signal" if self._stop() else ("settle_cap" if elapsed >= parameters.settle_cap_seconds else None)
            if cause is None and host.wait(min(parameters.interval_seconds, parameters.settle_cap_seconds - elapsed)):
                cause = "signal"
            if cause is not None:
                with _locked(self.root) as path:
                    _append(self.root, path, _window_record(
                        self.root, self.window_id, self.actor, "not_opened", self.key, load,
                        {"cause": cause, "settle_seconds": _round(host.monotonic() - started),
                         "load_bound": parameters.load_bound, "trigger": self.trigger}, host.wall()))
                if cause == "signal":
                    raise ProtocolRefusal("quiet_window_interrupted",
                                          "the window was interrupted while settling; nothing was held",
                                          remedy="Retry with a fresh idempotency key when the fleet is ready.")
                if pressure:
                    raise ProtocolRefusal("quiet_settle_cap_reached",
                                          "memory pressure stayed under %s for the settle cap; nothing was held (window %s)"
                                          % (parameters.pressure_threshold, self.window_id),
                                          remedy="Run the pressure window again with a fresh idempotency key to keep guarding.")
                raise ProtocolRefusal("quiet_settle_cap_reached",
                                      "the 15-minute load stayed above %s for the settle cap; nothing was held (window %s)"
                                      % (parameters.load_bound, self.window_id),
                                      remedy="Retry later with a fresh idempotency key, or raise --settle-cap-seconds or --load-bound.")
        # Open and hold.
        self.tracked: Dict[Tuple[int, str], _Tracked] = {}
        self.held_seats: set = set()
        self.scans = self.scans_failed = 0
        self.opened_mono, self.opened_wall = host.monotonic(), host.wall()
        seats = {node: self._hold_reason(node) for node in self.active}
        with _locked(self.root) as path:
            _append(self.root, path, _window_record(
                self.root, self.window_id, self.actor, "opened", self.key, load,
                {"opened_at": _now_text(self.opened_wall), "trigger": dict(self.trigger),
                 "covered_commands": covered_commands(),
                 "reach": {"held": "enrolled_seats_building_or_testing",
                           "outside_reach": "observed_not_held"},
                 "seats": {"holdable": sorted(n for n, r in seats.items() if r == "writing_only"),
                           "actor": self.actor, "exempt": list(parameters.exempt),
                           "no_workspace": sorted(n for n, r in seats.items() if r == "no_workspace")}},
                self.opened_wall), reserve=self.close_budget)
        self.release_requested = None
        cause = None
        try:
            self.opening_scan = True
            while cause is None:
                cause = self._release_cause()
                if cause is not None:
                    break
                self._scan(pause=True)
                self.opening_scan = False
                cause = self._release_boundary()
                if cause is not None:
                    break
                remaining = parameters.cap_seconds - (host.monotonic() - self.opened_mono)
                if host.wait(max(0.0, min(parameters.interval_seconds, remaining))):
                    cause = "signal"
        finally:
            released_mono, released_wall = host.monotonic(), host.wall()
            resumed, not_resumed, ambiguous = self._resume()
            self._scan(pause=False)
            result = self._close(cause or "error", released_mono, released_wall, resumed, not_resumed, ambiguous)
        return result

    def _release_boundary(self) -> Optional[str]:
        """Latch a signal, touch or cap observed before taking another hold."""
        if self.release_requested is None:
            self.release_requested = self._boundary_cause()
        return self.release_requested

    def _boundary_cause(self) -> Optional[str]:
        if self._stop():
            return "signal"
        release = self.parameters.release_file
        if release is not None:
            try:
                if os.stat(release).st_mtime_ns >= int(self.opened_wall * 1e9) - 1_000_000:
                    return "touch"
            except OSError:
                pass
        if self.host.monotonic() - self.opened_mono >= self.parameters.cap_seconds:
            return "cap"
        return None

    def _release_cause(self) -> Optional[str]:
        boundary = self._release_boundary()
        if boundary is not None:
            return boundary
        if self.parameters.trigger == "memory_pressure":
            reading = self._pressure()
            # An unreadable reading keeps the hold; only a measured fall releases it.
            if reading is not None and reading < self.parameters.pressure_release_below:
                self.trigger["release_reading"] = _round(reading)
                return "pressure_fell"
        return None

    def _pressure(self) -> Optional[float]:
        probe = getattr(self.host, "pressure", None)
        reading = probe(self.parameters.pressure_metric) if probe is not None else None
        if reading is None or isinstance(reading, bool) or not isinstance(reading, (int, float))\
                or not math.isfinite(reading):
            if hasattr(self, "trigger") and "reads_failed" in self.trigger:
                self.trigger["reads_failed"] += 1
            return None
        return float(reading)

    def _resume(self):
        with _locked(self.root) as path:
            rows, partial = _read(self.root)
            if partial:
                raise _partial_release()
            window = _windows(rows)[self.window_id]
            return _recover_targets(self.root, path, self.window_id, window, self.actor, self.key, self.host,
                                    close_budget=self.close_budget)

    def _close(self, cause, released_mono, released_wall, resumed, not_resumed, ambiguous) -> dict:
        seconds = _round(released_mono - self.opened_mono)
        interval = {"opened_at": _now_text(self.opened_wall), "released_at": _now_text(released_wall),
                    "seconds": seconds}
        exposures = [self._exposure(node, interval) for node in self.active]
        exposures.append(self._exposure(None, interval))
        load = None
        try:
            load = self.host.loadavg()
        except OSError:
            pass
        pending = ambiguous or any(row["reason"] == "signal_refused" for row in not_resumed)
        state = "release_pending" if pending else "released"
        with _locked(self.root) as path:
            _append(self.root, path, _window_record(
                self.root, self.window_id, self.actor, state, self.key, load,
                {"recovery_version": 2, "cause": cause, "released_at": None if pending else interval["released_at"], "interval_seconds": seconds,
                 **_bounded_resumes(resumed, not_resumed), "trigger": dict(self.trigger),
                 "scans": self.scans, "scans_failed": self.scans_failed}, released_wall))
            written = [_append(self.root, path, exposure) for exposure in exposures]
        return {"window_id": self.window_id, "state": state, "cause": cause,
                "ambiguous_targets": _ambiguous_targets(self.root, self.window_id, self.actor, ambiguous),
                "opened_at": interval["opened_at"], "released_at": None if pending else interval["released_at"],
                "interval_seconds": seconds, "resumed": resumed, "not_resumed": not_resumed,
                "scans": self.scans, "scans_failed": self.scans_failed, "trigger": dict(self.trigger),
                "exposures": [_exposure_view(row) for row in written]}

    def _exposure(self, node: Optional[str], interval: dict) -> dict:
        """One seat's exposure, or (node None) the work outside every seat."""
        processes = [t for t in self.tracked.values() if t.node == node]
        reason = "not_enrolled" if node is None else self._hold_reason(node)
        if reason == "no_workspace":
            observation, cpu = "no_workspace", {"method": CPU_METHOD, "seconds": None, "bound": None,
                                                "absent": "no_workspace"}
        else:
            observation = "observed"
            total, exact = 0.0, self.scans_failed == 0
            for tracked in processes:
                if tracked.last_cpu is None or tracked.baseline is None:
                    exact = False
                    continue
                total += max(0.0, tracked.last_cpu - tracked.baseline)
                exact = exact and tracked.exact and tracked.seen_last_scan
            cpu = {"method": CPU_METHOD, "seconds": _round(total), "bound": "exact" if exact else "lower",
                   "absent": None}
        if node is not None and node in self.held_seats:
            reason = "building_or_testing"

        def delta(t):
            return None if t.last_cpu is None or t.baseline is None else max(0.0, t.last_cpu - t.baseline)

        if node is None:
            # Outside the window's reach, list only the work that ran.
            processes = [t for t in processes if t.heavy or (delta(t) or 0.0) > 0.0]
        rows = sorted(processes, key=lambda t: (-(delta(t) or 0.0), t.pid))
        listed = [{"pid": t.pid, "executable": t.executable[:128], "build_or_test": t.heavy,
                   "paused": t.paused, "own_account": t.own_account, "cpu_seconds": _round(delta(t)),
                   "exited_before_release": not t.seen_last_scan}
                  for t in rows[:MAX_EXPOSED_PROCESSES]]
        return {"schema_version": 1, "kind": EXPOSURE_KIND, "id": "quiet-exposure-" + uuid7_hex(),
                "tenant_id": self.root.tenant_id, "timestamp": interval["released_at"],
                "window_id": self.window_id, "node_id": node,
                "scope": "outside_reach" if node is None else "seat", "observation": observation,
                "held": node is not None and node in self.held_seats, "hold_reason": reason,
                "interval": interval, "cpu": cpu,
                "processes": listed, "processes_truncated": max(0, len(rows) - MAX_EXPOSED_PROCESSES)}


@contextmanager
def _null():
    yield


def _bounded_resumes(resumed: List[dict], not_resumed: List[dict]) -> dict:
    """Keep one terminal record inside its byte bound; counts stay whole."""
    return {"resumed": resumed[:MAX_PAUSES_PER_RECORD], "resumed_count": len(resumed),
            "not_resumed": not_resumed[:MAX_PAUSES_PER_RECORD], "not_resumed_count": len(not_resumed)}


def _partial_release():
    return ProtocolRefusal("quiet_history_uncertain", "release requires complete quiet window history",
                           remedy="Inspect and preserve the quiet window ledger before retrying release.")


def _recover_targets(root, path, window_id, window, actor, key, host, *, close_budget=1):
    def resolved(pause, outcome):
        _resolve_pause(root, path, window_id, actor, key, pause, outcome, host)

    confirmed, ambiguous = _outstanding(window)
    resumed, not_resumed, pending = [], [], []
    # Each remaining confirmed target needs a continue intent plus a result.
    # An ambiguous target needs just the result of the operator's decision.
    reserve = 2 * len(confirmed) + len(ambiguous) + close_budget
    for pause in reversed(confirmed):
        facts = {"pauses": [pause]}
        entry = {"pid": pause["pid"], "node_id": pause["node_id"]}
        reserve -= 1
        _append(root, path, _window_record(root, window_id, actor, "resume_intent",
                                           key, None, facts, host.wall()), reserve=reserve)
        try:
            delivered = host.signal(pause["pid"], pause["start_token"], signal.SIGCONT)
        except OSError:
            not_resumed.append(dict(entry, reason="signal_refused"))
            # Re-arm automatic recovery after known non-delivery only when a
            # subsequent attempt fits. Otherwise the explicit command can
            # still consume the reserved final resolution frame.
            try:
                _append(root, path, _window_record(root, window_id, actor, "resume_refused",
                                                   key, None, facts, host.wall()), reserve=reserve + 1)
            except ProtocolRefusal as exc:
                if exc.code not in ("ledger_byte_limit", "ledger_record_limit"):
                    raise
                pending.append(pause)
            else:
                reserve += 1
            continue
        resolved(pause, "resumed" if delivered else "resolved_by_exit")
        reserve -= 1
        if delivered:
            resumed.append(entry)
        else:
            not_resumed.append(dict(entry, reason="exited_or_reused"))
    for pause in ambiguous:
        try:
            departed = not host.matches(pause["pid"], pause["start_token"])
        except OSError:
            departed = False
        if departed:
            resolved(pause, "resolved_by_exit")
            not_resumed.append({"node_id": pause["node_id"], "pid": pause["pid"], "reason": "exited_or_reused"})
        else:
            pending.append(pause)
    return resumed, not_resumed, pending


def release_orphan(root: FloatiRoot, actor: str, window_id: str, idempotency_key: str, *, host=None,
                   resolve_pid=None, start_token=None) -> dict:
    """Release a window whose driver died: resume its recorded pauses."""
    host = NativeHost() if host is None else host
    actor = validate_identifier(actor, "node")
    key = validate_identifier(idempotency_key, "idempotency_key")
    try:
        Registry(root).require_active(actor)
    except ProtocolRefusal:
        raise ProtocolRefusal("quiet_node_unknown", "releasing a window needs a registered active node",
                              remedy="Pass --as a registered active node of this root.") from None
    with _locked(root) as path:
        rows, partial = _read(root)
        if partial:
            raise _partial_release()
        windows = _windows(rows)
        window = windows.get(window_id)
        if window is None:
            raise ProtocolRefusal("quiet_window_unknown", "no quiet window has this ID in this root",
                                  remedy="Run floati quiet show --root ROOT to list this root's windows.")
        if (resolve_pid is None) != (start_token is None):
            raise ProtocolRefusal("quiet_parameter_invalid", "Pass the target PID and its start token together.",
                                  remedy="Use the resolution command from floati quiet status.")
        if resolve_pid is not None:
            replay = _resolution_replay(root, window_id, window, actor, key, resolve_pid, start_token)
            if replay is not None:
                return replay
        released = _phase_rows(window, "released")
        if released and not _legacy_failed_release(released[-1]):
            facts = released[-1].get("facts", {})
            if released[-1].get("idempotency_key") == key:
                return {"window_id": window_id, "state": "released", "cause": facts.get("cause"),
                        "resumed": facts.get("resumed", []), "not_resumed": facts.get("not_resumed", []),
                        "replayed": True}
            raise ProtocolRefusal("quiet_window_released", "this window is already released",
                                  remedy="Read it with floati quiet show --root ROOT --window " + window_id + ".")
        state = _state(window, host)
        if state not in ("orphaned", "release_pending"):
            refusal = ProtocolRefusal("quiet_window_not_orphaned",
                                  "manual recovery needs a lost driver or an unfinished release; this window is " + state,
                                  remedy="Touch the window's release file, or wait for its cap.")
            refusal.refusal_detail = {"state": state, "ambiguous_targets": _ambiguous_targets(
                root, window_id, actor, _outstanding(window)[1])}
            raise refusal
        if resolve_pid is not None:
            return _resolve_target(root, path, window_id, window, actor, key, resolve_pid, start_token, host)
        resumed, not_resumed, ambiguous = _recover_targets(root, path, window_id, window, actor, key, host)
        if ambiguous or any(row["reason"] == "signal_refused" for row in not_resumed):
            return {"window_id": window_id, "state": state, "resumed": resumed, "not_resumed": not_resumed,
                    "ambiguous_targets": _ambiguous_targets(root, window_id, actor, ambiguous),
                    "release_command": _release_command(root, actor, window_id)}
        wall = host.wall()
        _append(root, path, _window_record(
            root, window_id, actor, "released", key, None,
            {"recovery_version": 2, "cause": "operator_release_orphan", "released_at": _now_text(wall), "interval_seconds": None,
             **_bounded_resumes(resumed, not_resumed), "scans": None, "scans_failed": None}, wall))
        return {"window_id": window_id, "state": "released", "cause": "operator_release_orphan",
                "resumed": resumed, "not_resumed": not_resumed, "replayed": False,
                "exposure": {"absent": "driver_lost"}}


# ---------------------------------------------------------------------------
# CLI.


def register_cli(commands) -> None:
    quiet = commands.add_parser("quiet", floati_mcp_exposure="never")
    actions = quiet.add_subparsers(dest="quiet_action", required=True)
    run = actions.add_parser("run", floati_mcp_exposure="never")
    run.add_argument("--root", required=True, metavar="ROOT")
    run.add_argument("--as", dest="actor", required=True, metavar="NODE")
    run.add_argument("--idempotency-key", required=True, metavar="KEY")
    run.add_argument("--trigger", choices=("operator", "memory_pressure"), default="operator")
    run.add_argument("--load-bound", type=float, metavar="LOAD")
    run.add_argument("--pressure-metric", choices=tuple(sorted(PRESSURE_METRICS)))
    run.add_argument("--pressure-threshold", type=float, metavar="VALUE")
    run.add_argument("--pressure-release-below", type=float, metavar="VALUE")
    run.add_argument("--settle-cap-seconds", required=True, type=float, metavar="N")
    run.add_argument("--cap-seconds", required=True, type=float, metavar="N")
    run.add_argument("--interval-seconds", type=float, default=15.0, metavar="N")
    run.add_argument("--release-file", metavar="PATH")
    run.add_argument("--exempt", action="append", default=[], metavar="NODE")
    run.set_defaults(handler=_handle)
    status = actions.add_parser("status", floati_mcp_exposure="read")
    status.add_argument("--root", required=True, metavar="ROOT")
    status.add_argument("--json", action="store_true")
    status.add_argument("--command", metavar="TEXT")
    status.set_defaults(handler=_handle)
    show = actions.add_parser("show", floati_mcp_exposure="read")
    show.add_argument("--root", required=True, metavar="ROOT")
    show.add_argument("--window", metavar="ID")
    show.set_defaults(handler=_handle)
    release = actions.add_parser("release", floati_mcp_exposure="never")
    release.add_argument("--root", required=True, metavar="ROOT")
    release.add_argument("--as", dest="actor", required=True, metavar="NODE")
    release.add_argument("--window", required=True, metavar="ID")
    release.add_argument("--idempotency-key", required=True, metavar="KEY")
    release.add_argument("--resolve-pid", type=int, metavar="PID")
    release.add_argument("--start-token", metavar="TOKEN")
    release.set_defaults(handler=_handle)


def _handle(args):
    from .cli import _root
    root = _root(args.root)
    if args.quiet_action == "status":
        evidence = quiet_status(root, command=args.command)
        if args.json:
            evidence["quiet_status_schema_version"] = 1
        return "ok", evidence, 0
    if args.quiet_action == "show":
        return "ok", show_windows(root, args.window), 0
    if args.quiet_action == "release":
        return "ok", release_orphan(root, args.actor, args.window, args.idempotency_key,
                                    resolve_pid=args.resolve_pid, start_token=args.start_token), 0
    parameters = QuietParameters(args.load_bound, args.settle_cap_seconds, args.cap_seconds,
                                 args.interval_seconds, args.release_file, tuple(args.exempt),
                                 args.trigger, args.pressure_metric, args.pressure_threshold,
                                 args.pressure_release_below)
    return "ok", QuietWindow(root, args.actor, args.idempotency_key, parameters).run(), 0

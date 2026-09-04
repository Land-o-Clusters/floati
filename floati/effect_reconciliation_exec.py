"""Descriptor- and digest-bound launcher for reconciliation observation."""

from __future__ import annotations

import fcntl
import hashlib
import math
import os
import select
import signal
import socket
import stat
import struct
import sys
import threading
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence, Tuple

from . import effect_reconciliation_observer as observer_source
from . import effect_reconciliation_protocol as protocol_source
from .effect_reconciliation_protocol import (
    MAX_FRAME_BYTES,
    ReconciliationRequest,
    ReconciliationResult,
    build_result,
    decode_result_frame,
    encode_frame,
    validate_request,
)
from .errors import ProtocolRefusal


_CHANNEL_TARGET = 3
_PROTOCOL_TARGET = 4
_OBSERVER_TARGET = 5
_REPOSITORY_TARGET = 6
_FIRST_SOURCE_DESCRIPTOR = 7
_MAX_SOURCE_BYTES = 262_144
_MAX_INTERPRETER_BYTES = 67_108_864
_PID_PREAMBLE_BYTES = 8
_REMOTE_ENVIRONMENT_ALLOWLIST = (
    "SSH_AUTH_SOCK", "SSL_CERT_FILE", "SSL_CERT_DIR", "NO_PROXY",
    "HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY",
)
_MAX_REMOTE_ENVIRONMENT_BYTES = 4096
_BIDI_CONTROLS = frozenset({
    "LRE", "RLE", "LRO", "RLO", "PDF", "LRI", "RLI", "FSI", "PDI", "BN",
})

_LOADER = (
    "import hashlib,importlib.machinery,os,re,struct,sys,types\n"
    "if len(sys.argv)!=5: raise SystemExit(126)\n"
    "pfd,pdigest,ofd,odigest=sys.argv[1:5]\n"
    "if pfd!='4' or ofd!='5': raise SystemExit(126)\n"
    "if any(len(d)!=64 or any(c not in '0123456789abcdef' for c in d) for d in (pdigest,odigest)): raise SystemExit(126)\n"
    "def read_source(fd):\n"
    " chunks=[]; total=0\n"
    " while True:\n"
    "  chunk=os.read(fd,65536)\n"
    "  if not chunk: break\n"
    "  total+=len(chunk)\n"
    "  if total>262144: raise ValueError('oversized observer source')\n"
    "  chunks.append(chunk)\n"
    " if total==0: raise ValueError('empty observer source')\n"
    " return b''.join(chunks)\n"
    "try:\n"
    " protocol_source,observer_source=read_source(4),read_source(5)\n"
    "except BaseException:\n"
    " for fd in (4,5):\n"
    "  try: os.close(fd)\n"
    "  except OSError: pass\n"
    " raise SystemExit(126)\n"
    "try:\n"
    " os.close(4); os.close(5)\n"
    "except OSError: raise SystemExit(126)\n"
    "if hashlib.sha256(protocol_source).hexdigest()!=pdigest or hashlib.sha256(observer_source).hexdigest()!=odigest: raise SystemExit(126)\n"
    "try:\n"
    " protocol_code=compile(protocol_source,'effect_reconciliation_protocol.py','exec')\n"
    " observer_code=compile(observer_source,'effect_reconciliation_observer.py','exec')\n"
    "except BaseException: raise SystemExit(126)\n"
    "pkg=types.ModuleType('floati'); pkg.__package__='floati'; pkg.__file__='floati/__init__.py'; pkg.__path__=[]; pkg.__spec__=importlib.machinery.ModuleSpec('floati',loader=None,is_package=True)\n"
    "errors=types.ModuleType('floati.errors')\n"
    "class FloatiError(RuntimeError):\n"
    " def __init__(self,code,detail): super().__init__(code+': '+detail); self.code=code; self.detail=detail\n"
    "class ProtocolRefusal(FloatiError): pass\n"
    "errors.FloatiError=FloatiError; errors.ProtocolRefusal=ProtocolRefusal\n"
    "root=types.ModuleType('floati.root'); root.IDENTIFIER_PATTERN=re.compile(r'^[a-z0-9](?:[a-z0-9_-]{0,62}[a-z0-9])?$')\n"
    "protocol=types.ModuleType('floati.effect_reconciliation_protocol'); protocol.__package__='floati'; protocol.__file__='effect_reconciliation_protocol.py'; protocol.__spec__=importlib.machinery.ModuleSpec('floati.effect_reconciliation_protocol',loader=None,origin=protocol.__file__)\n"
    "sys.modules['floati']=pkg; sys.modules['floati.errors']=errors; sys.modules['floati.root']=root; sys.modules['floati.effect_reconciliation_protocol']=protocol\n"
    "try: exec(protocol_code,protocol.__dict__,protocol.__dict__)\n"
    "except BaseException: raise SystemExit(126)\n"
    "preamble=struct.pack('>Q',os.getpid()); offset=0\n"
    "try:\n"
    " while offset<len(preamble):\n"
    "  written=os.write(3,preamble[offset:])\n"
    "  if written<=0: raise OSError('PID preamble made no progress')\n"
    "  offset+=written\n"
    "except BaseException: raise SystemExit(126)\n"
    "sys.argv=['effect_reconciliation_observer.py']\n"
    "spec=importlib.machinery.ModuleSpec('floati.__main__',loader=None,origin='effect_reconciliation_observer.py')\n"
    "namespace={'__name__':'__main__','__file__':'effect_reconciliation_observer.py','__package__':'floati','__spec__':spec}\n"
    "exec(observer_code,namespace,namespace)\n"
)


@dataclass(frozen=True)
class _OpenedSource:
    path: Path
    descriptor: int
    device: int
    inode: int
    digest: str


@dataclass(frozen=True)
class _TrustedInterpreter:
    path: Path
    device: int
    inode: int
    uid: int
    mode: int
    size: int
    digest: str


_TRUSTED_INTERPRETER_LOCK = threading.Lock()


class _ChannelTimeout(TimeoutError):
    pass


class _ResultMissing(EOFError):
    pass


class _ProtocolInvalid(ValueError):
    pass


class _ResultBindingInvalid(_ProtocolInvalid):
    pass


class _EOFMissing(_ChannelTimeout):
    pass


class _ObserverCleanupFailure(RuntimeError):
    pass


class _InterpreterTrustFailure(OSError):
    def __init__(self, observation: dict[str, object]) -> None:
        super().__init__("reconciliation interpreter component is not root-trusted")
        self.observation = observation


class _InterpreterUntrusted(RuntimeError):
    def __init__(self, observation: Optional[dict[str, object]] = None) -> None:
        super().__init__("reconciliation interpreter is not root-trusted")
        self.observation = observation


def _source_paths() -> Tuple[Path, Path]:
    """Return the two installed sources that the fixed loader accepts."""

    protocol_file = getattr(protocol_source, "__file__", None)
    observer_file = getattr(observer_source, "__file__", None)
    if type(protocol_file) is not str or type(observer_file) is not str:
        raise OSError("reconciliation observer source path is unavailable")
    protocol_path = Path(protocol_file)
    observer_path = Path(observer_file)
    if not protocol_path.is_absolute() or not observer_path.is_absolute():
        raise OSError("reconciliation observer source path is not absolute")
    return protocol_path, observer_path


def _read_source(descriptor: int) -> str:
    digest = hashlib.sha256()
    total = 0
    while True:
        chunk = os.read(descriptor, 65_536)
        if not chunk:
            break
        total += len(chunk)
        if total > _MAX_SOURCE_BYTES:
            raise OSError("reconciliation observer source exceeds fixed bound")
        digest.update(chunk)
    if total == 0:
        raise OSError("reconciliation observer source is empty")
    os.lseek(descriptor, 0, os.SEEK_SET)
    return digest.hexdigest()


def _snapshot_interpreter(path: Path) -> _TrustedInterpreter:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_size <= 0
            or metadata.st_size > _MAX_INTERPRETER_BYTES
            or metadata.st_mode & 0o111 == 0
        ):
            raise OSError("reconciliation interpreter is not a bounded executable")
        digest = hashlib.sha256()
        total = 0
        while True:
            chunk = os.read(descriptor, 65_536)
            if not chunk:
                break
            total += len(chunk)
            if total > _MAX_INTERPRETER_BYTES:
                raise OSError("reconciliation interpreter exceeds fixed bound")
            digest.update(chunk)
        final = os.fstat(descriptor)
        path_metadata = os.lstat(path)
        if (
            total != metadata.st_size
            or (final.st_dev, final.st_ino, final.st_uid, final.st_mode, final.st_size)
            != (
                metadata.st_dev, metadata.st_ino, metadata.st_uid,
                metadata.st_mode, metadata.st_size,
            )
            or stat.S_ISLNK(path_metadata.st_mode)
            or (
                path_metadata.st_dev, path_metadata.st_ino,
                path_metadata.st_uid, path_metadata.st_mode, path_metadata.st_size,
            ) != (
                metadata.st_dev, metadata.st_ino, metadata.st_uid,
                metadata.st_mode, metadata.st_size,
            )
        ):
            raise OSError("reconciliation interpreter changed while hashing")
        return _TrustedInterpreter(
            path=path,
            device=metadata.st_dev,
            inode=metadata.st_ino,
            uid=metadata.st_uid,
            mode=metadata.st_mode,
            size=metadata.st_size,
            digest=digest.hexdigest(),
        )
    finally:
        os.close(descriptor)


def _interpreter_trust_observation(
    interpreter_path: Path, component: Path, metadata: os.stat_result,
) -> dict[str, object]:
    return {
        "interpreter_path": str(interpreter_path),
        "failing_component": str(component),
        "component_uid": metadata.st_uid,
        "component_mode": metadata.st_mode,
    }


def _root_trusted_interpreter_path(path: Path) -> None:
    current = path.parent
    while True:
        metadata = os.lstat(current)
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or metadata.st_uid != 0
            or metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        ):
            raise _InterpreterTrustFailure(
                _interpreter_trust_observation(path, current, metadata),
            )
        if current.parent == current:
            return
        current = current.parent


def _freeze_trusted_interpreter(value: object) -> _TrustedInterpreter:
    if type(value) is not str:
        raise TypeError("reconciliation interpreter path must be an exact string")
    if not value or not os.path.isabs(value) or os.path.realpath(value) != value:
        raise ValueError("reconciliation interpreter path must be canonical and absolute")
    path = Path(value)
    _root_trusted_interpreter_path(path)
    trusted = _snapshot_interpreter(path)
    if (
        trusted.uid != 0
        or trusted.mode & (stat.S_IWGRP | stat.S_IWOTH)
    ):
        raise _InterpreterTrustFailure(
            _interpreter_trust_observation(path, path, os.lstat(path)),
        )
    return trusted


def _revalidate_trusted_interpreter(trusted: _TrustedInterpreter) -> Path:
    if type(trusted) is not _TrustedInterpreter:
        raise TypeError("reconciliation interpreter trust record is invalid")
    current = _snapshot_interpreter(trusted.path)
    if current != trusted:
        raise OSError("reconciliation interpreter identity changed")
    _root_trusted_interpreter_path(trusted.path)
    if current.uid != 0 or current.mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise _InterpreterTrustFailure(
            _interpreter_trust_observation(
                trusted.path, trusted.path, os.lstat(trusted.path),
            ),
        )
    return trusted.path


_TRUSTED_INTERPRETER: Optional[_TrustedInterpreter] = None


def _trusted_interpreter() -> _TrustedInterpreter:
    global _TRUSTED_INTERPRETER
    if _TRUSTED_INTERPRETER is None:
        with _TRUSTED_INTERPRETER_LOCK:
            if _TRUSTED_INTERPRETER is None:
                try:
                    _TRUSTED_INTERPRETER = _freeze_trusted_interpreter(
                        os.path.realpath(sys.executable),
                    )
                except _InterpreterTrustFailure as exc:
                    raise _InterpreterUntrusted(exc.observation) from exc
                except OSError as exc:
                    raise _InterpreterUntrusted from exc
    assert _TRUSTED_INTERPRETER is not None
    return _TRUSTED_INTERPRETER


def _close_descriptors(descriptors: Sequence[int]) -> None:
    failure: Optional[OSError] = None
    for descriptor in descriptors:
        try:
            os.close(descriptor)
        except OSError as exc:
            if failure is None:
                failure = exc
    if failure is not None:
        raise failure


def _open_bound_sources(paths: Tuple[Path, Path]) -> Tuple[_OpenedSource, _OpenedSource]:
    expected_names = (
        "effect_reconciliation_protocol.py", "effect_reconciliation_observer.py",
    )
    if (
        type(paths) is not tuple
        or len(paths) != 2
        or any(not isinstance(path, Path) or not path.is_absolute() for path in paths)
        or tuple(path.name for path in paths) != expected_names
        or paths[0].parent != paths[1].parent
    ):
        raise OSError("reconciliation observer source selection is invalid")
    package = paths[0].parent
    package_descriptor: Optional[int] = None
    opened: list[_OpenedSource] = []
    try:
        package_metadata = os.lstat(package)
        if (
            package.resolve(strict=True) != package
            or stat.S_ISLNK(package_metadata.st_mode)
            or not stat.S_ISDIR(package_metadata.st_mode)
            or package_metadata.st_uid != os.geteuid()
            or package_metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        ):
            raise OSError("reconciliation observer package is not trusted")
        directory_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        directory_flags |= getattr(os, "O_DIRECTORY", 0)
        directory_flags |= getattr(os, "O_NOFOLLOW", 0)
        package_descriptor = os.open(package, directory_flags)
        held_package = os.fstat(package_descriptor)
        if (
            not stat.S_ISDIR(held_package.st_mode)
            or (held_package.st_dev, held_package.st_ino)
            != (package_metadata.st_dev, package_metadata.st_ino)
        ):
            raise OSError("reconciliation observer package identity changed")
        source_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        source_flags |= getattr(os, "O_NOFOLLOW", 0)
        for path in paths:
            descriptor = os.open(path.name, source_flags, dir_fd=package_descriptor)
            try:
                metadata = os.fstat(descriptor)
                if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid():
                    raise OSError("reconciliation observer source is not trusted")
                opened.append(_OpenedSource(
                    path=path,
                    descriptor=descriptor,
                    device=metadata.st_dev,
                    inode=metadata.st_ino,
                    digest=_read_source(descriptor),
                ))
            except BaseException:
                os.close(descriptor)
                raise
        final_package = os.lstat(package)
        if (final_package.st_dev, final_package.st_ino) != (
            held_package.st_dev, held_package.st_ino,
        ):
            raise OSError("reconciliation observer package identity changed")
        for record in opened:
            metadata = os.lstat(record.path)
            if (
                stat.S_ISLNK(metadata.st_mode)
                or not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != os.geteuid()
                or (metadata.st_dev, metadata.st_ino) != (record.device, record.inode)
            ):
                raise OSError("reconciliation observer source identity changed")
    except BaseException:
        cleanup = [record.descriptor for record in opened]
        if package_descriptor is not None:
            cleanup.append(package_descriptor)
        if cleanup:
            _close_descriptors(cleanup)
        raise
    if package_descriptor is None or len(opened) != 2:
        raise OSError("reconciliation observer sources are incomplete")
    try:
        os.close(package_descriptor)
    except OSError:
        _close_descriptors([record.descriptor for record in opened])
        raise
    return opened[0], opened[1]


def _open_repository(request: ReconciliationRequest) -> Optional[int]:
    if request.adapter != "git_local":
        return None
    coordinate = request.target["coordinate"]
    if (
        type(coordinate) is not str
        or not os.path.isabs(coordinate)
        or os.path.normpath(coordinate) != coordinate
        or os.path.realpath(coordinate) != coordinate
    ):
        return None
    components = coordinate.split("/")[1:]
    if any(not component or component in {".", ".."} for component in components):
        return None
    directory_flag = getattr(os, "O_DIRECTORY", None)
    nofollow_flag = getattr(os, "O_NOFOLLOW", None)
    if type(directory_flag) is not int or type(nofollow_flag) is not int:
        return None
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | directory_flag | nofollow_flag
    descriptor: Optional[int] = None
    try:
        descriptor = os.open("/", flags)
        for component in components:
            child = os.open(component, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or request.local_repository_identity != (metadata.st_dev, metadata.st_ino)
        ):
            return None
        selected = descriptor
        descriptor = None
        return selected
    except OSError:
        return None
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass


def _relocate_launch_descriptors(descriptors: list[int]) -> list[int]:
    """Move every source above all fixed child targets before file actions."""

    if (
        type(descriptors) is not list
        or any(type(descriptor) is not int or descriptor < 0 for descriptor in descriptors)
        or len(descriptors) != len(set(descriptors))
    ):
        raise ValueError("launch descriptors must be distinct exact integers")
    relocated: list[int] = []
    try:
        for descriptor in descriptors:
            relocated.append(fcntl.fcntl(
                descriptor, fcntl.F_DUPFD_CLOEXEC, _FIRST_SOURCE_DESCRIPTOR,
            ))
    except BaseException:
        _close_descriptors([*relocated, *descriptors])
        raise
    try:
        _close_descriptors(descriptors)
    except BaseException:
        _close_descriptors(relocated)
        raise
    return relocated


def _descriptor_directory() -> str:
    return "/proc/self/fd" if sys.platform.startswith("linux") else "/dev/fd"


def _open_descriptors() -> set[int]:
    try:
        entries = os.listdir(_descriptor_directory())
    except OSError as exc:
        raise RuntimeError("descriptor enumeration unavailable") from exc
    result = set()
    for entry in entries:
        if type(entry) is not str or not entry.isascii() or not entry.isdigit():
            continue
        descriptor = int(entry)
        try:
            os.fstat(descriptor)
        except OSError:
            continue
        result.add(descriptor)
    return result


def _safe_remote_environment_value(value: object) -> Optional[str]:
    if type(value) is not str:
        return None
    try:
        encoded = value.encode("utf-8", "strict")
    except UnicodeEncodeError:
        return None
    if not encoded or len(encoded) > _MAX_REMOTE_ENVIRONMENT_BYTES or "\x00" in value:
        return None
    if any(
        unicodedata.category(character) in {"Cc", "Cs"}
        or unicodedata.bidirectional(character) in _BIDI_CONTROLS
        for character in value
    ):
        return None
    return value


def _child_environment(request: ReconciliationRequest) -> dict[str, str]:
    if request.adapter != "git_remote_explicit":
        return {}
    environment: dict[str, str] = {}
    for name in _REMOTE_ENVIRONMENT_ALLOWLIST:
        value = _safe_remote_environment_value(os.environ.get(name))
        if value is not None:
            environment[name] = value
    return environment


class ObserverChannel:
    """One parent-owned private stream with a single absolute deadline."""

    def __init__(self, channel_socket: socket.socket) -> None:
        if type(channel_socket) is not socket.socket:
            raise TypeError("observer channel requires an exact socket")
        self._socket: Optional[socket.socket] = channel_socket

    def close(self) -> None:
        channel_socket = self._socket
        self._socket = None
        if channel_socket is not None:
            channel_socket.close()

    def _selected_socket(self) -> socket.socket:
        if self._socket is None:
            raise OSError("observer channel is closed")
        return self._socket

    def _read_exact(self, size: int, deadline: float, *, missing: bool = False) -> bytes:
        chunks = bytearray()
        selected = self._selected_socket()
        while len(chunks) < size:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise _ChannelTimeout("observer channel read timed out")
            readable, _, _ = select.select((selected,), (), (), remaining)
            if not readable:
                raise _ChannelTimeout("observer channel read timed out")
            chunk = selected.recv(size - len(chunks))
            if not chunk:
                if missing and not chunks:
                    raise _ResultMissing("observer returned EOF without a result")
                raise _ProtocolInvalid("observer frame ended before its declared boundary")
            chunks.extend(chunk)
        return bytes(chunks)

    def read_pid(self, deadline: float) -> int:
        return struct.unpack(">Q", self._read_exact(_PID_PREAMBLE_BYTES, deadline))[0]

    def send_request(self, request: ReconciliationRequest, deadline: float) -> None:
        payload = encode_frame(request)
        selected = self._selected_socket()
        offset = 0
        while offset < len(payload):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise _ChannelTimeout("observer channel write timed out")
            _, writable, _ = select.select((), (selected,), (), remaining)
            if not writable:
                raise _ChannelTimeout("observer channel write timed out")
            written = selected.send(payload[offset:])
            if written <= 0:
                raise OSError("observer channel write made no progress")
            offset += written

    def read_result(
        self, request: ReconciliationRequest, deadline: float,
    ) -> ReconciliationResult:
        header = self._read_exact(4, deadline, missing=True)
        length = struct.unpack(">I", header)[0]
        if length == 0 or length > MAX_FRAME_BYTES:
            raise _ProtocolInvalid("observer result length is outside the fixed bound")
        frame = header + self._read_exact(length, deadline)
        try:
            return decode_result_frame(frame, request)
        except ProtocolRefusal as exc:
            if exc.code in {
                "reconciliation_protocol_result_binding_invalid",
                "reconciliation_protocol_evidence_digest_invalid",
            }:
                raise _ResultBindingInvalid(str(exc)) from exc
            raise _ProtocolInvalid(str(exc)) from exc

    def require_eof(self, deadline: float) -> None:
        selected = self._selected_socket()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise _EOFMissing("observer did not close after its result")
        readable, _, _ = select.select((selected,), (), (), remaining)
        if not readable:
            raise _EOFMissing("observer did not close after its result")
        if selected.recv(1):
            raise _ProtocolInvalid("observer returned duplicate or trailing bytes")


class SpawnedReconciliationObserver:
    """Serialized wait/signal ownership for exactly one posix_spawn PID."""

    def __init__(self, pid: int) -> None:
        if type(pid) is not int or pid <= 1:
            raise ValueError("invalid reconciliation observer PID")
        self.pid = pid
        self.process_group = _owned_observer_process_group(pid)
        if self.process_group is None:
            raise OSError("reconciliation observer process group is not exact")
        self.status: Optional[int] = None
        self._waitable = True
        self._lock = threading.Lock()

    def _poll_locked(self) -> None:
        if not self._waitable:
            return
        try:
            waited, status = os.waitpid(self.pid, os.WNOHANG)
        except ChildProcessError:
            self._waitable = False
            return
        if waited == self.pid:
            self.status = status
            self._waitable = False

    def wait_until(self, deadline: float) -> bool:
        while True:
            with self._lock:
                self._poll_locked()
                if not self._waitable:
                    return self.status is not None
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            time.sleep(min(0.005, remaining))

    def _signal(self, signum: int) -> None:
        with self._lock:
            self._poll_locked()
            if not self._waitable:
                return
            try:
                os.kill(self.pid, signum)
            except ProcessLookupError:
                self._poll_locked()

    def terminate_and_reap(self) -> bool:
        group_owned = False
        with self._lock:
            if self._waitable:
                group_owned = (
                    _owned_observer_process_group(self.pid) == self.process_group
                )
        if not group_owned:
            self._signal(signal.SIGTERM)
            time.sleep(0.1)
            self._signal(signal.SIGKILL)
            self.wait_until(time.monotonic() + 1.0)
            return False
        try:
            os.killpg(self.process_group, signal.SIGTERM)
        except ProcessLookupError:
            pass
        except (OSError, ValueError):
            group_owned = False
        time.sleep(0.1)
        if group_owned and _process_group_exists(self.process_group):
            try:
                os.killpg(self.process_group, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except (OSError, ValueError):
                group_owned = False
        child_reaped = self.wait_until(time.monotonic() + 1.0)
        group_gone = group_owned and _wait_process_group_gone(
            self.process_group, time.monotonic() + 1.0,
        )
        return child_reaped and group_gone

    def exit_code(self) -> Optional[int]:
        status = self.status
        if status is None:
            return None
        if os.WIFEXITED(status):
            return os.WEXITSTATUS(status)
        if os.WIFSIGNALED(status):
            return -os.WTERMSIG(status)
        return None


def _owned_observer_process_group(pid: int) -> Optional[int]:
    """Return pid only while it names the exact session/process-group leader."""

    if type(pid) is not int or pid <= 1:
        return None
    try:
        process_group = os.getpgid(pid)
        session = os.getsid(pid)
    except (OSError, ProcessLookupError, ValueError):
        return None
    if process_group != pid or session != pid:
        return None
    return process_group


def _process_group_exists(process_group: int) -> bool:
    try:
        os.killpg(process_group, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Darwin reports EPERM for a session containing only a zombie leader.
        # Callers treat that as no remaining signalable member and never issue
        # another group signal from this classification.
        return False
    except (OSError, ValueError):
        return True
    return True


def _wait_process_group_gone(process_group: int, deadline: float) -> bool:
    while _process_group_exists(process_group):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(0.005, remaining))
    return True


def _terminate_and_reap_raw_pid(pid: int) -> bool:
    """Definitively dispose of a child before wrapper ownership exists."""

    if type(pid) is not int or pid <= 1:
        return False

    def poll_until(deadline: float) -> bool:
        while True:
            try:
                waited, _status = os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                return False
            if waited == pid:
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            time.sleep(min(0.005, remaining))

    process_group = _owned_observer_process_group(pid)
    if process_group is None:
        for signum, grace in ((signal.SIGTERM, 0.1), (signal.SIGKILL, 1.0)):
            try:
                os.kill(pid, signum)
            except ProcessLookupError:
                pass
            except (OSError, ValueError):
                return False
            if poll_until(time.monotonic() + grace):
                return False
        return False
    try:
        os.killpg(process_group, signal.SIGTERM)
    except ProcessLookupError:
        pass
    except (OSError, ValueError):
        return False
    time.sleep(0.1)
    if _process_group_exists(process_group):
        try:
            os.killpg(process_group, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except (OSError, ValueError):
            return False
    child_reaped = poll_until(time.monotonic() + 1.0)
    return child_reaped and _wait_process_group_gone(
        process_group, time.monotonic() + 1.0,
    )


def _failure(
    request: ReconciliationRequest, reason_code: str,
    observation: Optional[dict[str, object]] = None,
) -> ReconciliationResult:
    return build_result(
        request,
        outcome="unknown",
        reason_code=reason_code,
        observation=observation,
    )


def _build_cleanup_failure(
    request: ReconciliationRequest,
    cause: Optional[BaseException],
) -> Tuple[ReconciliationResult, _ObserverCleanupFailure]:
    """Build the closed cleanup result while preserving its pending cause."""

    try:
        if cause is None:
            raise _ObserverCleanupFailure(
                "spawned reconciliation observer cleanup failed",
            )
        raise _ObserverCleanupFailure(
            "spawned reconciliation observer cleanup failed",
        ) from cause
    except _ObserverCleanupFailure as cleanup_error:
        observation = None
        if cleanup_error.__cause__ is not None:
            observation = {
                "pending_error_type": type(cleanup_error.__cause__).__name__,
            }
        return (
            _failure(
                request, "observer_cleanup_failed", observation,
            ),
            cleanup_error,
        )


def _spawn_observer(
    request: ReconciliationRequest,
) -> Tuple[SpawnedReconciliationObserver, ObserverChannel]:
    opened = _open_bound_sources(_source_paths())
    repository_descriptor = _open_repository(request)
    parent_socket: Optional[socket.socket] = None
    child_socket: Optional[socket.socket] = None
    source_descriptors: list[int] = [record.descriptor for record in opened]
    if repository_descriptor is not None:
        source_descriptors.append(repository_descriptor)
    relocated: list[int] = []
    raw_pid: Optional[int] = None
    process: Optional[SpawnedReconciliationObserver] = None
    channel: Optional[ObserverChannel] = None
    try:
        parent_socket, child_socket = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        parent_socket.set_inheritable(False)
        child_socket.set_inheritable(False)
        source_descriptors.insert(0, child_socket.detach())
        child_socket = None
        relocated = _relocate_launch_descriptors(source_descriptors)
        source_descriptors = []
        targets = [_CHANNEL_TARGET, _PROTOCOL_TARGET, _OBSERVER_TARGET]
        if repository_descriptor is not None:
            targets.append(_REPOSITORY_TARGET)
        actions = [
            (os.POSIX_SPAWN_DUP2, descriptor, target)
            for descriptor, target in zip(relocated, targets)
        ]
        actual = _open_descriptors()
        final_targets = set(targets)
        actions.extend(
            (os.POSIX_SPAWN_CLOSE, descriptor)
            for descriptor in sorted(actual)
            if descriptor >= 3 and descriptor not in final_targets
        )
        try:
            interpreter_path = _revalidate_trusted_interpreter(_trusted_interpreter())
        except _InterpreterTrustFailure as exc:
            raise _InterpreterUntrusted(exc.observation) from exc
        except OSError as exc:
            raise _InterpreterUntrusted from exc
        interpreter = str(interpreter_path)
        raw_pid = os.posix_spawn(
            interpreter,
            [
                interpreter, "-I", "-S", "-B", "-c", _LOADER,
                "4", opened[0].digest, "5", opened[1].digest,
            ],
            _child_environment(request),
            file_actions=actions,
            setsid=True,
        )
        process = SpawnedReconciliationObserver(raw_pid)
        raw_pid = None
        parent_sources = relocated
        relocated = []
        _close_descriptors(parent_sources)
        channel = ObserverChannel(parent_socket)
        parent_socket = None
        return process, channel
    except BaseException as exc:
        cleanup_failed = False
        if channel is not None:
            try:
                channel.close()
            except BaseException:
                cleanup_failed = True
        if process is not None:
            try:
                if not process.terminate_and_reap():
                    cleanup_failed = True
            except BaseException:
                cleanup_failed = True
        elif raw_pid is not None:
            try:
                if not _terminate_and_reap_raw_pid(raw_pid):
                    cleanup_failed = True
            except BaseException:
                cleanup_failed = True
        if cleanup_failed:
            raise _ObserverCleanupFailure(
                "spawned reconciliation observer cleanup failed",
            ) from exc
        raise
    finally:
        try:
            if source_descriptors:
                _close_descriptors(source_descriptors)
            if relocated:
                _close_descriptors(relocated)
        finally:
            if child_socket is not None:
                child_socket.close()
            if parent_socket is not None:
                parent_socket.close()


def observe_effect_reconciliation(
    request: ReconciliationRequest,
    *,
    timeout_seconds: float = 5.0,
) -> ReconciliationResult:
    """Run one fresh observer and accept only its fully bound terminal frame."""

    selected_request = validate_request(request)
    if (
        not isinstance(timeout_seconds, (int, float))
        or isinstance(timeout_seconds, bool)
        or not math.isfinite(float(timeout_seconds))
        or timeout_seconds <= 0
    ):
        raise ValueError("observer timeout must be a finite positive number")
    deadline = time.monotonic() + float(timeout_seconds)
    try:
        process, channel = _spawn_observer(selected_request)
    except _InterpreterUntrusted as exc:
        return _failure(
            selected_request,
            "effect_reconciliation_interpreter_untrusted",
            exc.observation,
        )
    except _ObserverCleanupFailure:
        return _failure(selected_request, "observer_cleanup_failed")
    except Exception as exc:
        return _failure(
            selected_request, "observer_launch_failed",
            {"error_type": type(exc).__name__},
        )

    reason: Optional[str] = None
    observation: Optional[dict[str, object]] = None
    result: Optional[ReconciliationResult] = None
    parent_timeout = False
    cleanup_failed = False
    pending_base_error: Optional[BaseException] = None
    try:
        try:
            peer_pid = channel.read_pid(deadline)
            if peer_pid != process.pid:
                reason = "observer_channel_invalid"
                observation = {"peer_pid": peer_pid, "spawned_pid": process.pid}
            else:
                channel.send_request(selected_request, deadline)
                result = channel.read_result(selected_request, deadline)
                channel.require_eof(deadline)
        except _ResultBindingInvalid:
            reason = "observer_result_binding_invalid"
        except _ResultMissing:
            reason = "observer_result_missing"
        except _EOFMissing:
            reason = "observer_eof_missing"
            parent_timeout = True
        except _ProtocolInvalid:
            reason = "observer_protocol_invalid"
        except _ChannelTimeout:
            reason = "observer_timeout"
            parent_timeout = True
        except (BrokenPipeError, ConnectionError, OSError):
            reason = "observer_channel_invalid"
        except Exception:
            cleanup_failed = True
        except BaseException as exc:
            pending_base_error = exc
    finally:
        try:
            channel.close()
        except Exception:
            cleanup_failed = True
        except BaseException as exc:
            cleanup_failed = True
            if pending_base_error is None:
                pending_base_error = exc
        reaped = False
        if not parent_timeout:
            try:
                reaped = process.wait_until(deadline)
            except BaseException:
                cleanup_failed = True
        if not reaped:
            try:
                reaped = process.terminate_and_reap()
            except BaseException:
                cleanup_failed = True
        if not reaped:
            cleanup_failed = True
        if parent_timeout and reason is None:
            reason = "observer_timeout"

    if cleanup_failed:
        cleanup_result, _cleanup_error = _build_cleanup_failure(
            selected_request, pending_base_error,
        )
        return cleanup_result
    if pending_base_error is not None:
        raise pending_base_error
    exit_code = process.exit_code()
    if exit_code is None:
        return _failure(selected_request, "observer_cleanup_failed")
    if not parent_timeout:
        if exit_code < 0:
            reason = "observer_child_died"
            observation = {"signal": -exit_code}
            result = None
        elif exit_code != 0:
            reason = "observer_child_nonzero"
            observation = {"exit_code": exit_code}
            result = None
    if reason is not None:
        return _failure(selected_request, reason, observation)
    if result is None:
        return _failure(selected_request, "observer_result_missing")
    return result


__all__ = ["observe_effect_reconciliation"]

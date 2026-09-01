"""Descriptor-bound, read-only Effect reconciliation observer."""

from __future__ import annotations

import ctypes
import errno
import hashlib
import json
import os
import re
import resource
import selectors
import signal
import stat
import struct
import subprocess
import sys
import time
import unicodedata
from typing import Optional

from .effect_reconciliation_protocol import (
    MAX_FRAME_BYTES,
    build_result,
    classify_remote_coordinate,
    decode_request_frame,
    encode_frame,
)


_GIT = "/usr/bin/git"
_GIT_TIMEOUT_SECONDS = 5.0
_MAX_GIT_OUTPUT_BYTES = 4096
_MAX_GIT_OUTPUT_LINES = 64
_MAX_REMOTE_ENV_BYTES = 4096
_IO_TIMEOUT_SECONDS = 5.0
_DESCRIPTOR_CLOSE_FLOOR = 256
_FULL_OBJECT_ID = re.compile(r"[0-9a-f]{64}\Z")
_REF_TEXT = re.compile(r"refs/(?:heads|tags)/[A-Za-z0-9][A-Za-z0-9._/-]{0,510}\Z")
_BIDI_CONTROLS = frozenset({
    "LRE", "RLE", "LRO", "RLO", "PDF", "LRI", "RLI", "FSI", "PDI", "BN",
})

REMOTE_ENV_ALLOWLIST = (
    "SSH_AUTH_SOCK", "SSL_CERT_FILE", "SSL_CERT_DIR", "NO_PROXY",
    "HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY",
)

_FIXED_GIT_ENVIRONMENT = {
    "GIT_ASKPASS": "/usr/bin/false",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_SYSTEM": "/dev/null",
    "GIT_NO_LAZY_FETCH": "1",
    "GIT_PAGER": "cat",
    "GIT_TERMINAL_PROMPT": "0",
    "HOME": "/var/empty",
    "LANG": "C",
    "LC_ALL": "C",
    "PATH": "/usr/bin:/bin",
    "SSH_ASKPASS": "/usr/bin/false",
}
_GIT_PREFIX = (
    _GIT,
    "-c", "core.hooksPath=/dev/null",
    "--no-lazy-fetch",
    "--no-optional-locks",
    "--no-replace-objects",
)


def _descriptor_close_end() -> int:
    soft_limit = resource.getrlimit(resource.RLIMIT_NOFILE)[0]
    if soft_limit == resource.RLIM_INFINITY:
        soft_limit = os.sysconf("SC_OPEN_MAX")
    close_end = max(_DESCRIPTOR_CLOSE_FLOOR, int(soft_limit))
    if sys.platform == "darwin":
        close_end = min(close_end, _darwin_maxfilesperproc())
    return close_end


def _darwin_maxfilesperproc() -> int:
    """Read Darwin's process descriptor ceiling without spawning a child."""

    try:
        libc = ctypes.CDLL(None, use_errno=True)
        sysctlbyname = libc.sysctlbyname
        sysctlbyname.argtypes = (
            ctypes.c_char_p,
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_size_t),
            ctypes.c_void_p,
            ctypes.c_size_t,
        )
        sysctlbyname.restype = ctypes.c_int
        value = ctypes.c_int()
        value_size = ctypes.c_size_t(ctypes.sizeof(value))
        result = sysctlbyname(
            b"kern.maxfilesperproc",
            ctypes.byref(value),
            ctypes.byref(value_size),
            None,
            0,
        )
    except (AttributeError, OSError, TypeError, ValueError) as exc:
        raise RuntimeError("darwin descriptor ceiling unavailable") from exc
    if (
        result != 0
        or value_size.value != ctypes.sizeof(value)
        or value.value <= 0
    ):
        raise RuntimeError("darwin descriptor ceiling unavailable")
    return value.value


def _descriptor_directory() -> str:
    return "/proc/self/fd" if sys.platform.startswith("linux") else "/dev/fd"


def _open_descriptors() -> set[int]:
    """Return the verified actual open descriptor set."""

    try:
        entries = os.listdir(_descriptor_directory())
    except OSError as exc:
        raise RuntimeError("descriptor enumeration unavailable") from exc
    descriptors: set[int] = set()
    for entry in entries:
        if type(entry) is not str or not entry.isascii() or not entry.isdigit():
            continue
        descriptor = int(entry)
        try:
            os.fstat(descriptor)
        except OSError as exc:
            if exc.errno == errno.EBADF:
                continue
            raise RuntimeError("descriptor verification unavailable") from exc
        descriptors.add(descriptor)
    return descriptors


def _close_unruled_descriptors(allowed: set[int]) -> None:
    if type(allowed) is not set or any(type(fd) is not int or fd < 0 for fd in allowed):
        raise ValueError("invalid descriptor allowlist")
    if sys.platform == "darwin":
        # Hosted Darwin can omit real inherited descriptors from /dev/fd.
        close_end = _descriptor_close_end()
        start = 0
        for descriptor in sorted(fd for fd in allowed if fd < close_end):
            os.closerange(start, descriptor)
            start = descriptor + 1
        os.closerange(start, close_end)
    unruled = _open_descriptors() - allowed
    for _round in range(8):
        for descriptor in sorted(unruled):
            try:
                os.close(descriptor)
            except OSError as exc:
                if exc.errno != errno.EBADF:
                    raise RuntimeError("descriptor closure unavailable") from exc
        unruled = _open_descriptors() - allowed
        if not unruled:
            return
    raise RuntimeError("descriptor closure could not be verified")


def _scrub_environment() -> None:
    retained = {}
    for name in REMOTE_ENV_ALLOWLIST:
        value = _safe_inherited_value(os.environ.get(name))
        if value is not None:
            retained[name] = value
    os.environ.clear()
    os.environ.update(retained)


def _safe_inherited_value(value: object) -> Optional[str]:
    if type(value) is not str:
        return None
    try:
        encoded = value.encode("utf-8", "strict")
    except UnicodeEncodeError:
        return None
    if not encoded or len(encoded) > _MAX_REMOTE_ENV_BYTES or "\x00" in value:
        return None
    if any(
        unicodedata.category(character) in {"Cc", "Cs"}
        or unicodedata.bidirectional(character) in _BIDI_CONTROLS
        for character in value
    ):
        return None
    return value


def _git_environment(*, remote: bool) -> dict[str, str]:
    environment = dict(_FIXED_GIT_ENVIRONMENT)
    if remote:
        for name in REMOTE_ENV_ALLOWLIST:
            inherited = _safe_inherited_value(os.environ.get(name))
            if inherited is not None:
                environment[name] = inherited
    return environment


def _valid_full_ref(value: object) -> bool:
    if type(value) is not str or _REF_TEXT.fullmatch(value) is None:
        return False
    if ".." in value or "//" in value or "@{" in value or value.endswith(("/", ".")):
        return False
    return all(
        component and not component.startswith(".") and not component.endswith(".lock")
        for component in value.split("/")[2:]
    )


def _kill_git_process(process: subprocess.Popen[bytes]) -> None:
    try:
        process.kill()
    except (OSError, ProcessLookupError):
        try:
            os.kill(process.pid, signal.SIGKILL)
        except OSError:
            pass


def _run_git(arguments: list[str], environment: dict[str, str]) -> tuple[str, bytes, bytes]:
    """Run fixed Git under bounded time and output without shell or inherited fds."""

    if type(arguments) is not list or not arguments or arguments[0] != _GIT:
        return "unavailable", b"", b""
    try:
        process = subprocess.Popen(
            arguments,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
            close_fds=True,
        )
    except (OSError, ValueError, TypeError):
        return "unavailable", b"", b""
    if process.stdout is None or process.stderr is None:
        _kill_git_process(process)
        process.wait()
        return "unavailable", b"", b""
    streams = {process.stdout.fileno(): bytearray(), process.stderr.fileno(): bytearray()}
    selector = selectors.DefaultSelector()
    try:
        for descriptor in streams:
            os.set_blocking(descriptor, False)
            selector.register(descriptor, selectors.EVENT_READ)
        deadline = time.monotonic() + _GIT_TIMEOUT_SECONDS
        malformed = False
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _kill_git_process(process)
                process.wait()
                return "timeout", b"", b""
            for key, _events in selector.select(min(remaining, 0.1)):
                try:
                    chunk = os.read(key.fd, 1024)
                except BlockingIOError:
                    continue
                if not chunk:
                    selector.unregister(key.fd)
                    continue
                streams[key.fd].extend(chunk)
                if (
                    len(streams[key.fd]) > _MAX_GIT_OUTPUT_BYTES
                    or streams[key.fd].count(b"\n") > _MAX_GIT_OUTPUT_LINES
                ):
                    malformed = True
                    _kill_git_process(process)
                    break
            if malformed:
                break
        if malformed:
            process.wait()
            return "malformed", b"", b""
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            _kill_git_process(process)
            process.wait()
            return "timeout", b"", b""
        try:
            returncode = process.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            _kill_git_process(process)
            process.wait()
            return "timeout", b"", b""
        stdout = bytes(streams[process.stdout.fileno()])
        stderr = bytes(streams[process.stderr.fileno()])
        return ("ok" if returncode == 0 else "failed"), stdout, stderr
    finally:
        if process.poll() is None:
            _kill_git_process(process)
            process.wait()
        selector.close()
        process.stdout.close()
        process.stderr.close()


def _local_result(request: object, repository_fd: Optional[int]):
    expected = request.expected_confirmation
    full_ref = expected["locator"]
    if not _valid_full_ref(full_ref):
        return build_result(request, outcome="unknown", reason_code="contract_invalid")
    if repository_fd is None:
        return build_result(request, outcome="unknown", reason_code="repository_identity_changed")
    try:
        metadata = os.fstat(repository_fd)
    except OSError:
        return build_result(request, outcome="unknown", reason_code="repository_identity_changed")
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or request.local_repository_identity != (metadata.st_dev, metadata.st_ino)
    ):
        return build_result(request, outcome="unknown", reason_code="repository_identity_changed")
    coordinate = request.target["coordinate"]
    if (
        not coordinate.startswith("/")
        or os.path.normpath(coordinate) != coordinate
        or any(component == ".." for component in coordinate.split("/"))
    ):
        return build_result(request, outcome="unknown", reason_code="repository_fence_invalid")
    identity_payload = {
        "device": metadata.st_dev,
        "inode": metadata.st_ino,
        "path": coordinate,
    }
    identity_digest = hashlib.sha256(json.dumps(
        identity_payload, ensure_ascii=False, allow_nan=False, sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")).hexdigest()
    if identity_digest != request.target["identity_digest"]:
        return build_result(request, outcome="unknown", reason_code="repository_fence_invalid")
    common = list(_GIT_PREFIX)
    directory_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    directory_flags |= getattr(os, "O_DIRECTORY", 0)
    previous_directory = os.open(".", directory_flags)
    try:
        os.fchdir(repository_fd)
        ref_status, ref_stdout, _ = _run_git(
            [*common, "rev-parse", "--verify", full_ref + "^{object}"],
            _git_environment(remote=False),
        )
        if ref_status == "failed":
            return build_result(request, outcome="failed", reason_code="confirmation_absent")
        if ref_status != "ok":
            return build_result(
                request, outcome="unknown", reason_code="git_observation_" + ref_status,
            )
        try:
            ref_value = ref_stdout[:-1].decode("ascii")
        except UnicodeDecodeError:
            ref_value = ""
        if ref_stdout != (ref_value + "\n").encode("ascii") or _FULL_OBJECT_ID.fullmatch(ref_value) is None:
            return build_result(request, outcome="unknown", reason_code="evidence_malformed")
        expected_digest = expected["expected_digest"]
        object_status, object_stdout, _ = _run_git(
            [*common, "cat-file", "-e", expected_digest + "^{object}"],
            _git_environment(remote=False),
        )
    finally:
        try:
            os.fchdir(previous_directory)
        finally:
            os.close(previous_directory)
    observation = {"observed_ref_digest": ref_value}
    if object_status == "failed":
        return build_result(
            request, outcome="failed", reason_code="expected_object_absent",
            observation=observation,
        )
    if object_status != "ok" or object_stdout != b"":
        return build_result(
            request, outcome="unknown",
            reason_code="git_object_observation_" + (
                object_status if object_status != "ok" else "malformed"
            ),
            observation=observation,
        )
    if ref_value != expected_digest:
        return build_result(
            request, outcome="failed", reason_code="ref_digest_mismatch",
            observation=observation,
        )
    return build_result(
        request, outcome="unknown", reason_code="reconciliation_inconclusive",
        observation=observation,
    )


def _remote_result(request: object, repository_fd: Optional[int]):
    if repository_fd is not None:
        return build_result(request, outcome="unknown", reason_code="contract_invalid")
    expected = request.expected_confirmation
    full_ref = expected["locator"]
    if not _valid_full_ref(full_ref):
        return build_result(request, outcome="unknown", reason_code="contract_invalid")
    coordinate = request.target["coordinate"]
    evidence_scope = classify_remote_coordinate(coordinate)
    if evidence_scope is None:
        return build_result(
            request, outcome="unknown", reason_code="remote_coordinate_unsupported",
        )
    remote = coordinate
    if hashlib.sha256(remote.encode("utf-8")).hexdigest() != request.target["identity_digest"]:
        return build_result(request, outcome="unknown", reason_code="remote_identity_mismatch")
    status, stdout, _ = _run_git(
        [*_GIT_PREFIX, "ls-remote", "--refs", remote, full_ref],
        _git_environment(remote=True),
    )
    if status == "failed":
        return build_result(request, outcome="unknown", reason_code="destination_unqueryable")
    if status != "ok":
        return build_result(
            request, outcome="unknown", reason_code="git_remote_observation_" + status,
        )
    if stdout == b"":
        return build_result(request, outcome="failed", reason_code="confirmation_absent")
    try:
        line = stdout[:-1].decode("ascii")
    except UnicodeDecodeError:
        line = ""
    fields = line.split("\t")
    if (
        stdout != (line + "\n").encode("ascii")
        or len(fields) != 2
        or _FULL_OBJECT_ID.fullmatch(fields[0]) is None
        or fields[1] != full_ref
    ):
        return build_result(request, outcome="unknown", reason_code="evidence_malformed")
    observation = {
        "observed_ref_digest": fields[0],
        "evidence_scope": evidence_scope,
    }
    if fields[0] != expected["expected_digest"]:
        return build_result(
            request, outcome="failed", reason_code="ref_digest_mismatch",
            observation=observation,
        )
    return build_result(
        request, outcome="unknown", reason_code="reconciliation_inconclusive",
        observation=observation,
    )


def _observe_request(request: object, repository_fd: Optional[int]):
    adapter = request.adapter
    if adapter == "git_local":
        return _local_result(request, repository_fd)
    if adapter == "git_remote_explicit":
        return _remote_result(request, repository_fd)
    if repository_fd is not None:
        return build_result(request, outcome="unknown", reason_code="contract_invalid")
    if adapter in {"github_explicit", "deployment_explicit"}:
        return build_result(
            request, outcome="unknown", reason_code="adapter_unavailable",
            observation={"adapter": adapter},
        )
    return build_result(
        request, outcome="unknown", reason_code="reconciliation_inconclusive",
        observation={"adapter": "none"},
    )


def _read_exact(descriptor: int, size: int, deadline: float) -> bytes:
    chunks = bytearray()
    while len(chunks) < size:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("observer channel read timed out")
        readable, _, _ = select_select((descriptor,), (), (), remaining)
        if not readable:
            raise TimeoutError("observer channel read timed out")
        chunk = os.read(descriptor, size - len(chunks))
        if not chunk:
            raise EOFError("observer channel closed before frame completed")
        chunks.extend(chunk)
    return bytes(chunks)


def _write_all(descriptor: int, value: bytes, deadline: float) -> None:
    offset = 0
    while offset < len(value):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("observer channel write timed out")
        _, writable, _ = select_select((), (descriptor,), (), remaining)
        if not writable:
            raise TimeoutError("observer channel write timed out")
        written = os.write(descriptor, value[offset:])
        if written <= 0:
            raise OSError("observer channel write made no progress")
        offset += written


# Bound once so ordinary same-process reassignment cannot redirect the primitive
# used by the descriptor-loaded observer body.
from select import select as select_select


def run_observer(*, channel_fd: int = 3, repository_fd: Optional[int] = None) -> int:
    """Run one closed reconciliation request/result exchange."""

    if type(channel_fd) is not int or channel_fd != 3:
        raise ValueError("observer channel must be descriptor 3")
    if repository_fd is not None and (type(repository_fd) is not int or repository_fd != 6):
        raise ValueError("observer repository must be absent or descriptor 6")
    allowed = {0, 1, 2, channel_fd}
    if repository_fd is not None:
        allowed.add(repository_fd)
    try:
        if os.getsid(0) != os.getpid():
            os.setsid()
        os.chdir("/")
        _close_unruled_descriptors(allowed)
        _scrub_environment()
        deadline = time.monotonic() + _IO_TIMEOUT_SECONDS
        header = _read_exact(channel_fd, 4, deadline)
        length = struct.unpack(">I", header)[0]
        if length == 0 or length > MAX_FRAME_BYTES:
            return 1
        request = decode_request_frame(header + _read_exact(channel_fd, length, deadline))
        result = _observe_request(request, repository_fd)
        _write_all(
            channel_fd, encode_frame(result, request=request),
            time.monotonic() + _IO_TIMEOUT_SECONDS,
        )
        return 0
    except BaseException:
        return 1
    finally:
        for descriptor in (channel_fd, repository_fd):
            if descriptor is None:
                continue
            try:
                os.close(descriptor)
            except OSError:
                pass


def _repository_descriptor_if_open() -> Optional[int]:
    try:
        os.fstat(6)
    except OSError:
        return None
    return 6


if __name__ == "__main__":
    raise SystemExit(run_observer(repository_fd=_repository_descriptor_if_open()))


__all__ = ["REMOTE_ENV_ALLOWLIST", "run_observer"]

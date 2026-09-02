"""Bounded pull-only local Codex thread observation source."""

from __future__ import annotations

import json
import math
import os
import re
import select
import signal
import stat
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Mapping, Optional, Sequence, Tuple

from .errors import ProtocolRefusal
from .fleet_update import _explicit_executable


_MAX_FRAME_BYTES = 1_048_576
_MAX_TOTAL_BYTES = 2_097_152
_DEFAULT_DEADLINE_SECONDS = 5.0
_MAX_DEADLINE_SECONDS = 60.0
_PRODUCTION_ARGUMENTS = ("app-server", "--stdio")
_THREAD_ID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
_STATUS = {
    "notLoaded": "not_loaded",
    "idle": "idle",
    "systemError": "system_error",
    "active": "active",
}
_FLAGS = {
    "waitingOnApproval": "waiting_on_approval",
    "waitingOnUserInput": "waiting_on_user_input",
}
_IGNORED_NOTIFICATIONS = frozenset({"thread/status/changed"})


@dataclass(frozen=True)
class ThreadReadResult:
    provider_status: str
    active_flags: Optional[Tuple[str, ...]]
    provider_updated_at: Optional[int]
    observation_outcome: str
    observation_reason: str


class _SourceFailure(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class _LineReader:
    def __init__(self, stream: object) -> None:
        self._fd = stream.fileno()  # type: ignore[union-attr]
        self._buffer = bytearray()
        self._total = 0

    def message(self, deadline: float) -> Dict[str, object]:
        while b"\n" not in self._buffer:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise _SourceFailure("provider_timeout")
            ready, _, _ = select.select([self._fd], [], [], remaining)
            if not ready:
                raise _SourceFailure("provider_timeout")
            try:
                block = os.read(self._fd, 65536)
            except OSError as exc:
                raise _SourceFailure("provider_unavailable") from exc
            if not block:
                if self._buffer:
                    raise _SourceFailure("protocol_invalid")
                raise _SourceFailure("provider_unavailable")
            self._buffer.extend(block)
            self._total += len(block)
            if self._total > _MAX_TOTAL_BYTES or (
                b"\n" not in self._buffer and len(self._buffer) > _MAX_FRAME_BYTES
            ):
                raise _SourceFailure("protocol_invalid")

        raw, _, remainder = self._buffer.partition(b"\n")
        self._buffer = bytearray(remainder)
        if not raw or len(raw) > _MAX_FRAME_BYTES:
            raise _SourceFailure("protocol_invalid")
        try:
            value = json.loads(
                raw.decode("utf-8"),
                parse_constant=lambda _: (_ for _ in ()).throw(ValueError()),
                object_pairs_hook=_unique_object,
            )
        except (UnicodeDecodeError, ValueError, RecursionError) as exc:
            raise _SourceFailure("protocol_invalid") from exc
        if not isinstance(value, dict):
            raise _SourceFailure("protocol_invalid")
        return value

    def has_trailing_bytes(self) -> bool:
        if self._buffer:
            return True
        ready, _, _ = select.select([self._fd], [], [], 0)
        if not ready:
            return False
        try:
            return bool(os.read(self._fd, 1))
        except OSError:
            return True


def _unique_object(pairs: Sequence[Tuple[str, object]]) -> Dict[str, object]:
    value: Dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON object member")
        value[key] = item
    return value


def _unknown(reason: str) -> ThreadReadResult:
    return ThreadReadResult(
        provider_status="unknown",
        active_flags=None,
        provider_updated_at=None,
        observation_outcome="unknown",
        observation_reason=reason,
    )


def _canonical_directory(raw: str) -> str:
    path = Path(raw)
    if not path.is_absolute():
        raise _SourceFailure("provider_unavailable")
    try:
        metadata = path.lstat()
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise _SourceFailure("provider_unavailable") from exc
    if stat.S_ISLNK(metadata.st_mode) or resolved != path or not stat.S_ISDIR(metadata.st_mode):
        raise _SourceFailure("provider_unavailable")
    return str(path)


def _minimal_environment() -> Dict[str, str]:
    home = os.environ.get("HOME")
    if not isinstance(home, str) or not home:
        raise _SourceFailure("provider_unavailable")
    environment = {
        "HOME": _canonical_directory(home),
        "PATH": "/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
    }
    codex_home = os.environ.get("CODEX_HOME")
    if codex_home is not None:
        if not codex_home:
            raise _SourceFailure("provider_unavailable")
        environment["CODEX_HOME"] = _canonical_directory(codex_home)
    return environment


def _canonical_executable(path: Path) -> Tuple[str, Tuple[int, int]]:
    if not path.is_absolute():
        raise _SourceFailure("provider_unavailable")
    try:
        resolved = path.resolve(strict=True)
        metadata = resolved.lstat()
    except (OSError, RuntimeError) as exc:
        raise _SourceFailure("provider_unavailable") from exc
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid not in {0, os.getuid()}
        or metadata.st_mode & 0o111 == 0
    ):
        raise _SourceFailure("provider_unavailable")
    return str(resolved), (metadata.st_dev, metadata.st_ino)


def _revalidate_executable(path: str, identity: Tuple[int, int]) -> None:
    try:
        metadata = os.stat(path, follow_symlinks=False)
    except OSError as exc:
        raise _SourceFailure("provider_unavailable") from exc
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid not in {0, os.getuid()}
        or metadata.st_mode & 0o111 == 0
        or (metadata.st_dev, metadata.st_ino) != identity
    ):
        raise _SourceFailure("provider_unavailable")


def _send(stream: object, message: Mapping[str, object]) -> None:
    try:
        payload = json.dumps(
            message,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8") + b"\n"
        stream.write(payload)  # type: ignore[union-attr]
        stream.flush()  # type: ignore[union-attr]
    except (BrokenPipeError, OSError, TypeError, ValueError, UnicodeEncodeError) as exc:
        raise _SourceFailure("provider_unavailable") from exc


def _response(reader: _LineReader, expected_id: int, deadline: float) -> object:
    while True:
        message = reader.message(deadline)
        if "method" not in message:
            break
        method = message["method"]
        if (
            set(message) == {"method", "params"}
            and isinstance(method, str)
            and method in _IGNORED_NOTIFICATIONS
            and isinstance(message["params"], dict)
        ):
            continue
        raise _SourceFailure("protocol_invalid")
    if set(message) not in ({"id", "result"}, {"id", "error"}):
        raise _SourceFailure("protocol_invalid")
    response_id = message.get("id")
    if (
        not isinstance(response_id, int)
        or isinstance(response_id, bool)
        or response_id != expected_id
    ):
        raise _SourceFailure("protocol_invalid")
    if "error" in message:
        error = message["error"]
        if (
            not isinstance(error, dict)
            or set(error) not in ({"code", "message"}, {"code", "message", "data"})
            or not isinstance(error.get("code"), int)
            or isinstance(error.get("code"), bool)
            or not isinstance(error.get("message"), str)
        ):
            raise _SourceFailure("protocol_invalid")
        if (
            expected_id == 2
            and error.get("code") == -32602
            and error.get("message") == "thread not found"
        ):
            raise _SourceFailure("thread_missing")
        raise _SourceFailure("provider_unavailable")
    return message["result"]


def _normalize(result: object, expected_thread_id: str) -> ThreadReadResult:
    if not isinstance(result, dict) or set(result) != {"thread"}:
        raise _SourceFailure("protocol_invalid")
    thread = result["thread"]
    if not isinstance(thread, dict):
        raise _SourceFailure("protocol_invalid")
    try:
        thread_id = thread["id"]
        status = thread["status"]
        updated_at = thread["updatedAt"]
        turns = thread["turns"]
    except KeyError as exc:
        raise _SourceFailure("protocol_invalid") from exc
    if thread_id != expected_thread_id or turns != []:
        raise _SourceFailure("protocol_invalid")
    if (
        not isinstance(updated_at, int)
        or isinstance(updated_at, bool)
        or not 0 <= updated_at <= 253402300799
        or not isinstance(status, dict)
    ):
        raise _SourceFailure("protocol_invalid")
    status_type = status.get("type")
    provider_status = _STATUS.get(status_type) if isinstance(status_type, str) else None
    if provider_status is None:
        raise _SourceFailure("protocol_invalid")
    if status_type == "active":
        if set(status) != {"type", "activeFlags"}:
            raise _SourceFailure("protocol_invalid")
        raw_flags = status["activeFlags"]
        if (
            not isinstance(raw_flags, list)
            or any(not isinstance(flag, str) or flag not in _FLAGS for flag in raw_flags)
            or len(raw_flags) != len(set(raw_flags))
        ):
            raise _SourceFailure("protocol_invalid")
        flags = tuple(sorted(_FLAGS[flag] for flag in raw_flags))
    else:
        if set(status) != {"type"}:
            raise _SourceFailure("protocol_invalid")
        flags = ()
    return ThreadReadResult(
        provider_status=provider_status,
        active_flags=flags,
        provider_updated_at=updated_at,
        observation_outcome="observed",
        observation_reason="exact_thread_read",
    )


def _group_exists(process_group: int) -> bool:
    try:
        os.killpg(process_group, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _cleanup(process: Optional[subprocess.Popen[bytes]]) -> bool:
    if process is None:
        return True
    ok = True
    if process.stdin is not None:
        try:
            process.stdin.close()
        except OSError:
            ok = False
    process_group = process.pid
    try:
        os.killpg(process_group, signal.SIGTERM)
    except ProcessLookupError:
        pass
    except PermissionError:
        pass
    deadline = time.monotonic() + 0.1
    while _group_exists(process_group) and time.monotonic() < deadline:
        time.sleep(0.005)
    if _group_exists(process_group):
        try:
            os.killpg(process_group, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except PermissionError:
            pass
    try:
        process.wait(timeout=0.5)
    except (subprocess.TimeoutExpired, OSError):
        ok = False
    group_deadline = time.monotonic() + 0.5
    while _group_exists(process_group) and time.monotonic() < group_deadline:
        time.sleep(0.005)
    if _group_exists(process_group):
        ok = False
    if process.stdout is not None:
        try:
            process.stdout.close()
        except OSError:
            ok = False
    return ok


class CodexLocalThreadSource:
    """Read exactly one explicitly registered local Codex thread."""

    def __init__(self, executable: object = None) -> None:
        self._command: Sequence[str] = (
            "/opt/homebrew/bin/codex",
            *_PRODUCTION_ARGUMENTS,
        )
        self._executable_declared = executable is not None
        if executable is None:
            return
        try:
            selected = _explicit_executable(
                executable, "thread_source_codex_executable_invalid"
            )
        except ProtocolRefusal as exc:
            raise ProtocolRefusal(
                exc.code,
                exc.detail,
                remedy=(
                    "pass --codex-executable with one absolute canonical "
                    "executable path"
                ),
            ) from exc
        self._command = (selected, *_PRODUCTION_ARGUMENTS)

    @classmethod
    def _for_test(cls, command: Sequence[str]) -> "CodexLocalThreadSource":
        if (
            not isinstance(command, (list, tuple))
            or not command
            or any(not isinstance(item, str) or not item for item in command)
        ):
            raise ProtocolRefusal("thread_source_invalid", "test source command is invalid")
        source = cls.__new__(cls)
        source._command = tuple(command)
        source._executable_declared = True
        return source

    def read(
        self,
        provider_thread_id: str,
        *,
        deadline_seconds: float = _DEFAULT_DEADLINE_SECONDS,
    ) -> ThreadReadResult:
        if not isinstance(provider_thread_id, str) or _THREAD_ID.fullmatch(provider_thread_id) is None:
            raise ProtocolRefusal(
                "provider_thread_id_invalid",
                "provider thread ID must be one canonical lowercase hyphenated UUIDv7",
            )
        if (
            not isinstance(deadline_seconds, (int, float))
            or isinstance(deadline_seconds, bool)
            or not math.isfinite(float(deadline_seconds))
            or deadline_seconds <= 0
            or deadline_seconds > _MAX_DEADLINE_SECONDS
        ):
            raise ProtocolRefusal(
                "thread_source_deadline_invalid",
                "deadline must be finite, positive, and at most 60 seconds",
            )
        if not self._executable_declared:
            return _unknown("codex_executable_absent")

        process: Optional[subprocess.Popen[bytes]] = None
        result = _unknown("provider_unavailable")
        cleanup_ok = True
        try:
            executable, identity = _canonical_executable(Path(self._command[0]))
            command = (executable, *self._command[1:])
            environment = _minimal_environment()
            _revalidate_executable(executable, identity)
            deadline = time.monotonic() + float(deadline_seconds)
            process = subprocess.Popen(
                command,
                cwd="/",
                env=environment,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=False,
                bufsize=0,
                close_fds=True,
                start_new_session=True,
            )
            if process.stdin is None or process.stdout is None:
                raise _SourceFailure("provider_unavailable")
            reader = _LineReader(process.stdout)
            _send(
                process.stdin,
                {
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "clientInfo": {
                            "name": "floati-thread-observer",
                            "version": "0",
                        }
                    },
                },
            )
            initialize = _response(reader, 1, deadline)
            if not isinstance(initialize, dict):
                raise _SourceFailure("protocol_invalid")
            _send(process.stdin, {"method": "initialized"})
            _send(
                process.stdin,
                {
                    "id": 2,
                    "method": "thread/read",
                    "params": {
                        "includeTurns": False,
                        "threadId": provider_thread_id,
                    },
                },
            )
            observed = _response(reader, 2, deadline)
            result = _normalize(observed, provider_thread_id)
            if reader.has_trailing_bytes():
                raise _SourceFailure("protocol_invalid")
        except _SourceFailure as failure:
            result = _unknown(failure.reason)
        except (OSError, ValueError, TypeError):
            result = _unknown("provider_unavailable")
        finally:
            cleanup_ok = _cleanup(process)
        if not cleanup_ok:
            return _unknown("cleanup_failed")
        return result

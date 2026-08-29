"""Closed bootstrap configuration and canonical Worker socket framing."""

from __future__ import annotations

import json
import math
import os
import select
import socket
import struct
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .worker_errors import WorkerAdapterFailure
from .worker_isolation import WorkerIsolationPolicy


MAX_FRAME_BYTES = 1_048_576
MAX_COMMAND_PARTS = 64
MAX_COMMAND_PART_BYTES = 4_096
MAX_COMMAND_BYTES = 65_536

_FAILURE_CODE = "effect_worker_isolation_unavailable"
_BUILT_IN_ADAPTER_KINDS = frozenset({"codex", "claude", "pi"})
_BIDI_CONTROL_CODEPOINTS = frozenset(
    {
        0x061C,
        0x200E,
        0x200F,
        0x202A,
        0x202B,
        0x202C,
        0x202D,
        0x202E,
        0x2066,
        0x2067,
        0x2068,
        0x2069,
    }
)


@dataclass(frozen=True)
class BuiltInAdapterSpec:
    kind: str
    command: tuple[str, ...]


def _failure() -> WorkerAdapterFailure:
    return WorkerAdapterFailure(_FAILURE_CODE)


def _is_safe_command_part(value: str) -> bool:
    try:
        encoded = value.encode("utf-8", "strict")
    except UnicodeEncodeError:
        return False
    if not encoded or len(encoded) > MAX_COMMAND_PART_BYTES:
        return False
    for character in value:
        if (
            unicodedata.category(character) in {"Cc", "Cs", "Cf"}
            or ord(character) in _BIDI_CONTROL_CODEPOINTS
            or character in {"\u2028", "\u2029"}
        ):
            return False
    return True


def validate_builtin_adapter_spec(value: object) -> BuiltInAdapterSpec:
    """Validate and detach one closed built-in adapter selection."""

    if type(value) is not BuiltInAdapterSpec:
        raise _failure()
    if type(value.kind) is not str or value.kind not in _BUILT_IN_ADAPTER_KINDS:
        raise _failure()
    if type(value.command) is not tuple or not value.command:
        raise _failure()
    if len(value.command) > MAX_COMMAND_PARTS:
        raise _failure()
    command = []
    total_bytes = 0
    for part in value.command:
        if type(part) is not str or not _is_safe_command_part(part):
            raise _failure()
        part_bytes = len(part.encode("utf-8", "strict"))
        total_bytes += part_bytes
        if total_bytes > MAX_COMMAND_BYTES:
            raise _failure()
        command.append(part)
    if not os.path.isabs(command[0]):
        raise _failure()
    return BuiltInAdapterSpec(value.kind, tuple(command))


def builtin_adapter_spec_to_payload(value: BuiltInAdapterSpec) -> dict[str, object]:
    spec = validate_builtin_adapter_spec(value)
    return {"kind": spec.kind, "command": list(spec.command)}


def builtin_adapter_spec_from_payload(value: object) -> BuiltInAdapterSpec:
    if type(value) is not dict or set(value.keys()) != {"kind", "command"}:
        raise _failure()
    kind = value["kind"]
    command = value["command"]
    if type(kind) is not str or type(command) is not list:
        raise _failure()
    parts = []
    for part in command:
        if type(part) is not str:
            raise _failure()
        parts.append(part)
    return validate_builtin_adapter_spec(BuiltInAdapterSpec(kind, tuple(parts)))


def validate_isolation_backend(value: object) -> str:
    """Return one canonical backend name or the shared typed refusal."""

    if type(value) is not str:
        raise _failure()
    if value == "macos-sandbox":
        return value
    prefix = "linux-landlock-v"
    suffix = value[len(prefix):]
    if (
        value.startswith(prefix)
        and 1 <= len(suffix) <= 3
        and suffix[0] in "123456789"
        and all(character in "0123456789" for character in suffix)
        and 3 <= int(suffix) <= 999
    ):
        return value
    raise _failure()


def _canonical_path(value: object) -> Path:
    if type(value) is not str or not value:
        raise _failure()
    candidate = Path(value)
    if not candidate.is_absolute() or str(candidate) != value:
        raise _failure()
    try:
        if candidate.resolve(strict=False) != candidate:
            raise _failure()
    except (OSError, RuntimeError):
        raise _failure()
    return candidate


def _identity_to_payload(value: object, *, optional: bool) -> Optional[list[int]]:
    if value is None and optional:
        return None
    if type(value) is not tuple or len(value) != 2:
        raise _failure()
    device, inode = value
    if (
        type(device) is not int
        or type(inode) is not int
        or device < 0
        or inode <= 0
    ):
        raise _failure()
    return [device, inode]


def _identity_from_payload(value: object, *, optional: bool) -> Optional[tuple[int, int]]:
    if value is None and optional:
        return None
    if type(value) is not list or len(value) != 2:
        raise _failure()
    device, inode = value
    if (
        type(device) is not int
        or type(inode) is not int
        or device < 0
        or inode <= 0
    ):
        raise _failure()
    return device, inode


def isolation_policy_to_payload(policy: WorkerIsolationPolicy) -> dict[str, object]:
    if type(policy) is not WorkerIsolationPolicy:
        raise _failure()
    tenant_home = _canonical_path(str(policy.tenant_home))
    scratch = _canonical_path(str(policy.scratch))
    write_probe = _canonical_path(str(policy.write_probe))
    workspace: Optional[Path]
    if policy.workspace is None:
        workspace = None
        if policy.workspace_identity is not None:
            raise _failure()
    else:
        workspace = _canonical_path(str(policy.workspace))
        if policy.workspace_identity is None:
            raise _failure()
    return {
        "tenant_home": str(tenant_home),
        "workspace": None if workspace is None else str(workspace),
        "scratch": str(scratch),
        "write_probe": str(write_probe),
        "workspace_identity": _identity_to_payload(
            policy.workspace_identity, optional=True
        ),
        "scratch_identity": _identity_to_payload(
            policy.scratch_identity, optional=False
        ),
        "probe_identity": _identity_to_payload(
            policy.probe_identity, optional=False
        ),
    }


def isolation_policy_from_payload(value: object) -> WorkerIsolationPolicy:
    expected_keys = {
        "tenant_home",
        "workspace",
        "scratch",
        "write_probe",
        "workspace_identity",
        "scratch_identity",
        "probe_identity",
    }
    if type(value) is not dict or set(value.keys()) != expected_keys:
        raise _failure()
    tenant_home = _canonical_path(value["tenant_home"])
    scratch = _canonical_path(value["scratch"])
    write_probe = _canonical_path(value["write_probe"])
    workspace_value = value["workspace"]
    workspace_identity = _identity_from_payload(
        value["workspace_identity"], optional=True
    )
    if workspace_value is None:
        if workspace_identity is not None:
            raise _failure()
        workspace = None
    else:
        workspace = _canonical_path(workspace_value)
        if workspace_identity is None:
            raise _failure()
    scratch_identity = _identity_from_payload(value["scratch_identity"], optional=False)
    probe_identity = _identity_from_payload(value["probe_identity"], optional=False)
    if scratch_identity is None or probe_identity is None:
        raise _failure()
    return WorkerIsolationPolicy(
        tenant_home=tenant_home,
        workspace=workspace,
        scratch=scratch,
        write_probe=write_probe,
        workspace_identity=workspace_identity,
        scratch_identity=scratch_identity,
        probe_identity=probe_identity,
    )


def _normalise_json(value: object) -> object:
    if value is None or type(value) is bool or type(value) is int:
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise _failure()
        return value
    if type(value) is str:
        try:
            value.encode("utf-8", "strict")
        except UnicodeEncodeError:
            raise _failure()
        return value
    if type(value) is list:
        return [_normalise_json(item) for item in value]
    if type(value) is dict:
        normalised: dict[str, object] = {}
        for key, item in value.items():
            if type(key) is not str:
                raise _failure()
            if key in normalised:
                raise _failure()
            normalised[key] = _normalise_json(item)
        return normalised
    raise _failure()


def _encode_envelope(frame: tuple[str, object]) -> bytes:
    if type(frame) is not tuple or len(frame) != 2:
        raise _failure()
    verb, payload = frame
    if type(verb) is not str or not verb:
        raise _failure()
    try:
        envelope = {"payload": _normalise_json(payload), "verb": verb}
        encoded = json.dumps(
            envelope,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
    except (RecursionError, TypeError, UnicodeError, ValueError):
        raise _failure()
    if not 1 <= len(encoded) <= MAX_FRAME_BYTES:
        raise _failure()
    return encoded


def _no_duplicate_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _decode_envelope(encoded: bytes) -> tuple[str, object]:
    try:
        decoded = json.loads(encoded.decode("ascii"), object_pairs_hook=_no_duplicate_object)
        if type(decoded) is not dict or set(decoded.keys()) != {"payload", "verb"}:
            raise ValueError("invalid bootstrap envelope")
        verb = decoded["verb"]
        if type(verb) is not str or not verb:
            raise ValueError("invalid bootstrap verb")
        payload = _normalise_json(decoded["payload"])
        canonical = _encode_envelope((verb, payload))
    except (RecursionError, TypeError, UnicodeError, ValueError, WorkerAdapterFailure):
        raise _failure()
    if canonical != encoded:
        raise _failure()
    return verb, payload


class BootstrapChannel:
    """Bounded canonical frames over one descriptor-owned stream socket."""

    def __init__(self, descriptor: int) -> None:
        if type(descriptor) is not int or descriptor < 0:
            raise _failure()
        try:
            self._socket = socket.socket(fileno=descriptor)
        except OSError:
            raise _failure()

    def _read_exact(self, size: int, deadline: Optional[float]) -> bytes:
        chunks = []
        remaining = size
        while remaining:
            if deadline is not None:
                wait = deadline - time.monotonic()
                if wait <= 0:
                    raise TimeoutError("bootstrap channel receive deadline exceeded")
                try:
                    readable, _, _ = select.select([self._socket], [], [], wait)
                except (OSError, OverflowError, ValueError):
                    raise _failure()
                if not readable:
                    raise TimeoutError("bootstrap channel receive deadline exceeded")
            try:
                chunk = self._socket.recv(remaining)
            except OSError:
                raise _failure()
            if not chunk:
                raise _failure()
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    def send(self, frame: tuple[str, object]) -> None:
        encoded = _encode_envelope(frame)
        try:
            self._socket.sendall(struct.pack(">I", len(encoded)) + encoded)
        except OSError:
            raise _failure()

    def recv(self, timeout: Optional[float] = None) -> tuple[str, object]:
        if timeout is None:
            deadline = None
        elif (
            type(timeout) not in {int, float}
            or not math.isfinite(float(timeout))
            or timeout < 0
        ):
            raise _failure()
        else:
            deadline = time.monotonic() + float(timeout)
        header = self._read_exact(4, deadline)
        size = struct.unpack(">I", header)[0]
        if not 1 <= size <= MAX_FRAME_BYTES:
            raise _failure()
        return _decode_envelope(self._read_exact(size, deadline))

    def poll(self, timeout: float) -> bool:
        if type(timeout) not in {int, float}:
            raise _failure()
        try:
            if not math.isfinite(float(timeout)) or timeout < 0:
                raise _failure()
            readable, _, _ = select.select([self._socket], [], [], timeout)
        except (OSError, OverflowError, ValueError):
            raise _failure()
        return bool(readable)

    def close(self) -> None:
        self._socket.close()

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
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .worker_errors import WorkerAdapterFailure
from .worker_isolation import WorkerIsolationPolicy


MAX_FRAME_BYTES = 1_048_576
MAX_COMMAND_PARTS = 64
MAX_COMMAND_PART_BYTES = 4_096
MAX_COMMAND_BYTES = 65_536

# A versioned internal handoff to Task3's transport and Task4's native adapters.
# Dispatch duration is authenticated in AdhocJournal, never a caller override here.
NATIVE_THREAD_BOUNDS = {"stdout_bytes": 1_048_576, "stderr_bytes": 1_048_576,
    "combined_bytes": 2_097_152, "jsonl_frame_bytes": 65_536,
    "json_depth": 64, "protocol_records": 4096}
NATIVE_THREAD_FIELDS = {"schema_version", "profile_id", "profile_digest", "dispatch_id", "dispatch_digest",
    "node_id", "node_lease_id", "work_item_id", "operation", "provider_coordinate",
    "provider_state_binding", "workspace_binding", "permission", "bounds", "effect_binding"}

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
    workspace_mode: str = "fresh"


def _failure() -> WorkerAdapterFailure:
    return WorkerAdapterFailure(_FAILURE_CODE)


def validate_native_thread_context(value):
    """Closed detached data only; parent must authenticate all native references."""
    from .adhoc_records import UUID, _path, validate_native_permission
    from .adhoc_workspace import validate_borrowed_binding
    from .errors import ProtocolRefusal

    def refuse(code, detail):
        raise ProtocolRefusal(code, detail,
            remedy="use the exact admitted dispatch and its parent-owned native references")

    if type(value) is not dict or set(value) != NATIVE_THREAD_FIELDS:
        refuse("adhoc_dispatch_authentication_failed", "native context must repeat its closed version-one fields")
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        refuse("adhoc_dispatch_authentication_failed", "native context requires integer version one")
    for field, prefix in (("profile_id", "adhoc-profile-"), ("dispatch_id", "adhoc-dispatch-"),
                          ("node_lease_id", "lease-"), ("work_item_id", "work-")):
        if type(value[field]) is not str or re.fullmatch(re.escape(prefix) + UUID, value[field]) is None:
            refuse("adhoc_dispatch_authentication_failed", "native context has an invalid native reference")
    for field in ("profile_digest", "dispatch_digest"):
        if type(value[field]) is not str or re.fullmatch(r"[0-9a-f]{64}", value[field]) is None:
            refuse("adhoc_dispatch_authentication_failed", "native context requires exact SHA256 digests")
    if type(value["node_id"]) is not str or re.fullmatch(r"[a-z0-9](?:[a-z0-9_-]{0,62}[a-z0-9])?", value["node_id"]) is None:
        refuse("adhoc_dispatch_authentication_failed", "native context requires one exact node")
    if type(value["operation"]) is not str or value["operation"] not in {"start", "resume"}:
        refuse("adhoc_dispatch_authentication_failed", "native context requires explicit start or resume")
    validate_native_permission(value["permission"], error=refuse)
    if (type(value["bounds"]) is not dict or value["bounds"] != NATIVE_THREAD_BOUNDS
        or any(type(bound) is not int for bound in value["bounds"].values())):
        refuse("adhoc_dispatch_authentication_failed", "native context bounds differ from the versioned transport contract")
    validate_borrowed_binding(value["workspace_binding"])
    slot = value["provider_state_binding"]
    if type(slot) is not dict or set(slot) != {"path", "dev", "ino"}:
        refuse("adhoc_binding_changed", "provider-state binding must repeat path and descriptor identity")
    _path(slot["path"], refuse)
    if type(slot["dev"]) is not int or slot["dev"] < 0 or type(slot["ino"]) is not int or slot["ino"] <= 0:
        refuse("adhoc_binding_changed", "provider slot requires nonboolean descriptor integers")
    effect = value["effect_binding"]
    if type(effect) is not dict or set(effect) != {"run_id", "item_id", "attempt_id"}:
        refuse("effect_launch_coordinates_invalid", "native context requires exact parent run coordinates")
    for field, prefix in (("run_id", "run-"), ("item_id", "work-"), ("attempt_id", "attempt-")):
        if type(effect[field]) is not str or re.fullmatch(re.escape(prefix) + UUID, effect[field]) is None:
            refuse("effect_launch_coordinates_invalid", "effect context reference is invalid")
    if effect["item_id"] != value["work_item_id"]:
        refuse("effect_launch_item_mismatch", "effect context must name the requested work item")
    coordinate = value["provider_coordinate"]
    if coordinate is not None:
        if type(coordinate) is not dict or set(coordinate) != {"provider", "session_id", "session_file"}:
            refuse("native_session_mismatch", "provider coordinate must have its closed fields")
        if type(coordinate["provider"]) is not str or coordinate["provider"] not in {"codex_local", "claude_local", "pi_local"}:
            refuse("native_session_mismatch", "provider coordinate is outside the native family")
        validate_native_permission(value["permission"], coordinate["provider"].removesuffix("_local"), error=refuse)
        session = coordinate["session_id"]
        if type(session) is not str or not session or any(unicodedata.category(c).startswith("C") for c in session) or len(session.encode()) > 512:
            refuse("native_session_mismatch", "provider identifier must be bounded native testimony")
        if coordinate["provider"] == "codex_local":
            from .records import _PROVIDER_UUID7
            if re.fullmatch(_PROVIDER_UUID7, session) is None or coordinate["session_file"] is not None:
                refuse("native_session_mismatch", "Codex requires its canonical UUIDv7 coordinate without a session file")
        if coordinate["session_file"] is not None:
            _path(coordinate["session_file"], refuse)
    if value["operation"] == "start" and coordinate is not None:
        refuse("native_session_mismatch", "start cannot accept a caller provider coordinate")
    if value["operation"] == "resume" and coordinate is None:
        refuse("native_session_unmaterialized", "resume requires durable native provider testimony")
    return json.loads(json.dumps(value, allow_nan=False))


def require_borrowed_native_readiness(spec):
    """Implementation-owned readiness; no caller flag can enable legacy adapters."""
    spec = validate_builtin_adapter_spec(spec)
    if spec.workspace_mode != "borrowed":
        raise _failure()
    from .errors import ProtocolRefusal
    import sys
    if sys.platform != "darwin" and not sys.platform.startswith("linux"):
        raise ProtocolRefusal("adhoc_write_confinement_unsupported",
            f"W1 UNSUPPORTED for {spec.kind}: borrowed write confinement requires the macOS sandbox or Linux Landlock",
            remedy="retain the exact open work and use the supported borrowed isolation backend")
    return spec


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
    if type(value.workspace_mode) is not str or value.workspace_mode not in {"fresh", "borrowed"}:
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
    return BuiltInAdapterSpec(value.kind, tuple(command), value.workspace_mode)


def builtin_adapter_spec_to_payload(value: BuiltInAdapterSpec) -> dict[str, object]:
    spec = validate_builtin_adapter_spec(value)
    payload = {"kind": spec.kind, "command": list(spec.command)}
    if spec.workspace_mode == "borrowed":
        payload["workspace_mode"] = "borrowed"
    return payload


def builtin_adapter_spec_from_payload(value: object) -> BuiltInAdapterSpec:
    if type(value) is not dict or set(value.keys()) not in ({"kind", "command"}, {"kind", "command", "workspace_mode"}):
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
    mode = value.get("workspace_mode", "fresh")
    if "workspace_mode" in value and mode != "borrowed":
        raise _failure()
    return validate_builtin_adapter_spec(BuiltInAdapterSpec(kind, tuple(parts), mode))


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
    payload = {
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
    if policy.workspace_mode == "borrowed":
        if policy.provider_state is None or policy.provider_state_identity is None or policy.gitfile_identity is None:
            raise _failure()
        payload.update(workspace_mode="borrowed", provider_state=str(_canonical_path(str(policy.provider_state))),
            provider_state_identity=_identity_to_payload(policy.provider_state_identity, optional=False),
            gitfile_identity=_identity_to_payload(policy.gitfile_identity, optional=False))
    elif policy.workspace_mode != "fresh" or any(value is not None for value in
            (policy.provider_state, policy.provider_state_identity, policy.gitfile_identity)):
        raise _failure()
    return payload


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
    borrowed_keys = {"workspace_mode", "provider_state", "provider_state_identity", "gitfile_identity"}
    if type(value) is not dict or set(value.keys()) not in (expected_keys, expected_keys | borrowed_keys):
        raise _failure()
    mode = value.get("workspace_mode", "fresh")
    if set(value.keys()) != expected_keys and mode != "borrowed":
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
        workspace_mode=mode,
        provider_state=_canonical_path(value["provider_state"]) if mode == "borrowed" else None,
        provider_state_identity=_identity_from_payload(value["provider_state_identity"], optional=False) if mode == "borrowed" else None,
        gitfile_identity=_identity_from_payload(value["gitfile_identity"], optional=False) if mode == "borrowed" else None,
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

    def send(self, frame: tuple[str, object], *, deadline: Optional[float] = None) -> None:
        if deadline is not None:
            try:
                valid_deadline = type(deadline) in {int, float} and math.isfinite(float(deadline))
            except (OverflowError, ValueError):
                valid_deadline = False
            if not valid_deadline:
                raise _failure()
            deadline = float(deadline)
            if deadline <= time.monotonic():
                raise TimeoutError("bootstrap channel send deadline exceeded")
        encoded = _encode_envelope(frame)
        payload = struct.pack(">I", len(encoded)) + encoded
        restore_timeout = False
        prior_timeout = None
        try:
            if deadline is None:
                self._socket.sendall(payload)
                return
            # Set descriptor nonblocking mode explicitly. A per-call flag is
            # insufficient on supported hosts for a partially writable stream.
            prior_timeout = self._socket.gettimeout()
            self._socket.setblocking(False)
            restore_timeout = True
            pending = memoryview(payload)
            while pending:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("bootstrap channel send deadline exceeded")
                _, writable, _ = select.select([], [self._socket], [], remaining)
                if not writable:
                    raise TimeoutError("bootstrap channel send deadline exceeded")
                try:
                    count = self._socket.send(pending)
                except (BlockingIOError, InterruptedError):
                    continue
                if count <= 0:
                    raise _failure()
                pending = pending[count:]
        except TimeoutError:
            raise
        except OSError:
            raise _failure()
        finally:
            if restore_timeout:
                self._socket.settimeout(prior_timeout)

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

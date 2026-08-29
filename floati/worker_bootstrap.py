"""Trusted fresh-exec bootstrap for effect-enabled Worker adapters."""

from __future__ import annotations

import argparse
import errno
import json
import importlib.machinery
import os
import socket
import struct
import sys
import types
from typing import Optional


_PYTHON_INJECTION_VARIABLES = (
    "PYTHONPATH",
    "PYTHONHOME",
    "PYTHONSTARTUP",
    "PYTHONINSPECT",
    "PYTHONUSERBASE",
    "PYTHONBREAKPOINT",
)
_LAUNCH_KEYS = {
    "schema_version",
    "session_id",
    "adapter",
    "item",
    "deadline_millis",
    "spawn_context",
    "effect_context",
    "isolation_policy",
}


def _descriptor_directory() -> str:
    if sys.platform.startswith("linux"):
        return "/proc/self/fd"
    return "/dev/fd"


def _open_descriptors() -> set[int]:
    try:
        entries = os.listdir(_descriptor_directory())
    except OSError as exc:
        raise RuntimeError("descriptor enumeration unavailable") from exc
    descriptors = set()
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


def close_all_descriptors_except(allowed: set[int]) -> None:
    """Enumerate, close, repeat, and verify every unruled open descriptor."""

    if type(allowed) is not set or any(
        type(descriptor) is not int or descriptor < 0 for descriptor in allowed
    ):
        raise ValueError("invalid descriptor allowlist")
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
    if unruled:
        raise RuntimeError("descriptor closure could not be verified")


def _send_typed_unavailable(descriptor: int) -> None:
    """Best-effort closed failure before the trusted protocol can be imported."""

    _validate_descriptor(descriptor)
    encoded = json.dumps(
        {
            "payload": "effect_worker_isolation_unavailable",
            "verb": "failure",
        },
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
    transport = socket.socket(fileno=os.dup(descriptor))
    try:
        transport.sendall(struct.pack(">I", len(encoded)) + encoded)
    finally:
        transport.close()


def _trusted_imports() -> tuple[object, ...]:
    failure = "effect_worker_isolation_unavailable"
    failure_type = getattr(
        sys.modules.get("floati.worker_errors"),
        "WorkerAdapterFailure",
        RuntimeError,
    )
    preloaded = globals().get("_FLOATI_PRELOADED_MODULES")
    if type(preloaded) is not tuple or len(preloaded) != 4:
        raise failure_type(failure)
    package_directory = os.path.dirname(__file__)
    expected = (
        ("floati", "__init__.py", "floati"),
        ("floati.worker_errors", "worker_errors.py", "floati"),
        ("floati.worker_isolation", "worker_isolation.py", "floati"),
        (
            "floati.worker_bootstrap_protocol",
            "worker_bootstrap_protocol.py",
            "floati",
        ),
    )
    for index, (module, metadata) in enumerate(zip(preloaded, expected)):
        name, basename, package_name = metadata
        spec = getattr(module, "__spec__", None)
        if (
            type(module) is not types.ModuleType
            or module.__name__ != name
            or module.__package__ != package_name
            or module.__file__ != os.path.join(package_directory, basename)
            or type(spec) is not importlib.machinery.ModuleSpec
            or spec.name != name
            or spec.origin != os.path.join(package_directory, basename)
            or sys.modules.get(name) is not module
        ):
            raise failure_type(failure)
        if index == 0:
            if (
                getattr(module, "__path__", None) != [package_directory]
                or spec.submodule_search_locations is None
            ):
                raise failure_type(failure)
        elif spec.submodule_search_locations is not None:
            raise failure_type(failure)

    package, errors, isolation, protocol = preloaded
    del package
    try:
        BootstrapChannel = protocol.BootstrapChannel
        builtin_adapter_spec_from_payload = protocol.builtin_adapter_spec_from_payload
        isolation_policy_from_payload = protocol.isolation_policy_from_payload
        validate_isolation_backend = protocol.validate_isolation_backend
        WorkerAdapterFailure = errors.WorkerAdapterFailure
        apply_worker_isolation = isolation.apply_worker_isolation
    except (AttributeError, TypeError) as exc:
        raise failure_type(failure) from exc

    return (
        BootstrapChannel,
        builtin_adapter_spec_from_payload,
        isolation_policy_from_payload,
        validate_isolation_backend,
        WorkerAdapterFailure,
        apply_worker_isolation,
    )


def _validate_descriptor(descriptor: int) -> None:
    if type(descriptor) is not int or descriptor < 3:
        raise ValueError("invalid bootstrap descriptor")
    probe = socket.socket(fileno=os.dup(descriptor))
    try:
        if probe.family != socket.AF_UNIX:
            raise ValueError("bootstrap descriptor is not AF_UNIX")
        if probe.getsockopt(socket.SOL_SOCKET, socket.SO_TYPE) != socket.SOCK_STREAM:
            raise ValueError("bootstrap descriptor is not SOCK_STREAM")
        probe.getpeername()
    finally:
        probe.close()


def _validate_session_id(value: object) -> str:
    if type(value) is not str or not value.startswith("worker-"):
        raise ValueError("invalid session id")
    suffix = value[len("worker-"):]
    if len(suffix) != 32 or any(character not in "0123456789abcdef" for character in suffix):
        raise ValueError("invalid session id")
    return value


def _validate_detached_object(value: object, *, optional: bool) -> Optional[dict[str, object]]:
    if value is None and optional:
        return None
    if type(value) is not dict:
        raise ValueError("invalid launch object")
    return dict(value)


def _construct_adapter(spec: object) -> object:
    kind = spec.kind  # type: ignore[attr-defined]
    command = spec.command  # type: ignore[attr-defined]
    if kind == "codex":
        from floati.adapters.codex_live import CodexAppServerAdapter

        return CodexAppServerAdapter(command, isolate_process_group=False)
    if kind == "claude":
        from floati.adapters.claude import ClaudeHeadlessAdapter

        return ClaudeHeadlessAdapter(command, isolate_process_group=False)
    if kind == "pi":
        from floati.adapters.pi import PiRpcAdapter

        return PiRpcAdapter(command, isolate_process_group=False)
    from floati.worker_errors import WorkerAdapterFailure

    raise WorkerAdapterFailure("effect_worker_isolation_unavailable")


def bootstrap_main(descriptor: int = 3) -> int:
    """Validate, isolate, acknowledge readiness, then run one built-in adapter."""

    channel: Optional[object] = None
    ready = False
    try:
        os.setsid()
        os.chdir("/")
        close_all_descriptors_except({0, 1, 2, descriptor})
        _validate_descriptor(descriptor)
        for name in _PYTHON_INJECTION_VARIABLES:
            os.environ.pop(name, None)
        (
            BootstrapChannel,
            builtin_adapter_spec_from_payload,
            isolation_policy_from_payload,
            validate_isolation_backend,
            WorkerAdapterFailure,
            apply_worker_isolation,
        ) = _trusted_imports()
        channel = BootstrapChannel(descriptor)
        verb, payload = channel.recv()
        if verb != "launch" or type(payload) is not dict or set(payload) != _LAUNCH_KEYS:
            raise WorkerAdapterFailure("effect_worker_isolation_unavailable")
        if channel.poll(0.0):
            raise WorkerAdapterFailure("effect_worker_isolation_unavailable")
        if type(payload["schema_version"]) is not int or payload["schema_version"] != 1:
            raise WorkerAdapterFailure("effect_worker_isolation_unavailable")
        _validate_session_id(payload["session_id"])
        spec = builtin_adapter_spec_from_payload(payload["adapter"])
        item = _validate_detached_object(payload["item"], optional=False)
        spawn_context = _validate_detached_object(
            payload["spawn_context"], optional=True
        )
        effect_context = _validate_detached_object(
            payload["effect_context"], optional=True
        )
        deadline_millis = payload["deadline_millis"]
        if (
            type(deadline_millis) is not int
            or not 10 <= deadline_millis <= 60_000
        ):
            raise WorkerAdapterFailure("effect_worker_isolation_unavailable")
        policy = isolation_policy_from_payload(payload["isolation_policy"])
        backend = validate_isolation_backend(apply_worker_isolation(policy))
        channel.send(("isolation_ready", {"backend": backend}))
        ready = True
        from floati.worker_adapter_runtime import run_adapter_session

        adapter = _construct_adapter(spec)
        run_adapter_session(
            channel,
            adapter,
            item,
            deadline_millis / 1_000.0,
            spawn_context,
            effect_context,
            policy,
            process_group_mode="inherited",
        )
        channel = None
        return 0
    except BaseException as exc:
        if channel is not None:
            try:
                code = getattr(exc, "code", None)
                if not ready:
                    code = "effect_worker_isolation_unavailable"
                elif type(code) is not str:
                    code = "adapter_error"
                channel.send(("failure", code))
            except BaseException:
                pass
            try:
                channel.close()
            except BaseException:
                pass
        else:
            try:
                _send_typed_unavailable(descriptor)
            except BaseException:
                pass
        if isinstance(exc, SystemExit) and ready and type(exc.code) is int:
            return exc.code
        return 1


def _parse_descriptor(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--fd", type=int, default=3)
    arguments = parser.parse_args(argv)
    return arguments.fd


if __name__ == "__main__":
    raise SystemExit(bootstrap_main(_parse_descriptor(sys.argv[1:])))

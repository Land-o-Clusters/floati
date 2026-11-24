"""Shared post-isolation callback runtime for Worker adapters."""

from __future__ import annotations

import signal
import time
from copy import deepcopy
from typing import Optional

from .worker_bootstrap_protocol import BootstrapChannel
from .worker_errors import WorkerAdapterFailure
from .worker_isolation import WorkerIsolationPolicy


def run_adapter_session(
    channel: BootstrapChannel,
    adapter: object,
    item: dict[str, object],
    deadline_seconds: float,
    spawn_context: Optional[dict[str, object]],
    effect_context: Optional[dict[str, object]],
    isolation_policy: Optional[WorkerIsolationPolicy],
    *,
    process_group_mode: str = "legacy",
    native_thread_context: Optional[dict] = None,
) -> None:
    """Run the existing adapter callback protocol after isolation is ready."""

    try:
        borrowed = isolation_policy is not None and isolation_policy.workspace_mode == "borrowed"
        if borrowed != (native_thread_context is not None):
            raise WorkerAdapterFailure("effect_worker_isolation_unavailable")
        if type(process_group_mode) is not str or process_group_mode not in {
            "legacy", "inherited",
        }:
            raise WorkerAdapterFailure("adapter_error")
        if not borrowed and isolation_policy is not None and getattr(
            adapter, "requires_workspace", False
        ):
            set_prepared_workspace = getattr(
                adapter, "set_prepared_workspace", None
            )
            if (
                isolation_policy.workspace is None
                or isolation_policy.workspace_identity is None
                or not callable(set_prepared_workspace)
            ):
                raise WorkerAdapterFailure("adapter_error")
            set_prepared_workspace(
                str(isolation_policy.workspace),
                isolation_policy.workspace_identity[0],
                isolation_policy.workspace_identity[1],
            )

        def terminate(_signum: int, _frame: object) -> None:
            cancel = getattr(adapter, "cancel", None)
            if callable(cancel):
                cancel()
            raise SystemExit(143)

        signal.signal(signal.SIGTERM, terminate)
        if process_group_mode == "legacy":
            register_process_group = getattr(
                adapter, "set_process_group_registrar", None
            )
            if callable(register_process_group):
                register_process_group(
                    lambda process_group: channel.send(
                        ("process_group", process_group)
                    )
                )
        deadline = time.monotonic() + deadline_seconds
        if borrowed:
            from .native_thread_protocol import NativeThreadEmitter
            from .worker_bootstrap_protocol import validate_native_thread_context
            native_context = validate_native_thread_context(native_thread_context)
            slot = native_context["provider_state_binding"]
            if (process_group_mode != "inherited"
                or item.get("adhoc_binding") != native_context["workspace_binding"]
                or item.get("workspace") != str(isolation_policy.workspace)
                or slot["path"] != str(isolation_policy.provider_state)
                or (slot["dev"], slot["ino"]) != isolation_policy.provider_state_identity):
                raise WorkerAdapterFailure("effect_worker_isolation_unavailable")
            setter = getattr(adapter, "set_native_thread_context", None)
            if not callable(setter):
                raise WorkerAdapterFailure("effect_worker_isolation_unavailable")
            emitter = NativeThreadEmitter(native_context, channel=channel, deadline=deadline)
            setter(native_context, emitter.emit)
        if spawn_context is not None:
            set_spawn_context = getattr(adapter, "set_spawn_context", None)
            if not callable(set_spawn_context):
                raise WorkerAdapterFailure("spawn_context_hook_missing")
            set_spawn_context(
                deepcopy(spawn_context),
                lambda event: channel.send(("descendant", deepcopy(event))),
            )
        if effect_context is not None:
            set_effect_context = getattr(adapter, "set_effect_context", None)
            if not callable(set_effect_context):
                raise WorkerAdapterFailure("effect_context_hook_missing")
            set_effect_context(
                deepcopy(effect_context),
                lambda event: channel.send(("effect", deepcopy(event))),
            )
        handle = adapter.spawn(  # type: ignore[attr-defined]
            item, deadline_seconds=max(0.001, deadline - time.monotonic())
        )
        channel.send(("spawned", None))
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise WorkerAdapterFailure("process_timeout")
        bindings = adapter.drive(  # type: ignore[attr-defined]
            handle, item, deadline_seconds=remaining
        )
        if borrowed:
            channel.send(("native_transport", getattr(adapter, "last_native_transport", None)))
        channel.send(("result", bindings))
        if effect_context is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not channel.poll(remaining):
                raise WorkerAdapterFailure("process_timeout")
            if channel.recv() != ("effect_reporting_closed", None):
                raise WorkerAdapterFailure("adapter_error")
            channel.send(("effect_reporting_closed_ack", None))
        if (
            spawn_context is not None
            and spawn_context.get("subagents_mode") in {"observed_only", "managed"}
        ):
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not channel.poll(remaining):
                raise WorkerAdapterFailure("process_timeout")
            if channel.recv() != ("observation_closed", None):
                raise WorkerAdapterFailure("adapter_error")
            channel.send(("observation_closed_ack", None))
    except WorkerAdapterFailure as failure:
        if native_thread_context is not None:
            from .native_thread_protocol import _FAILURES
            if failure.code in _FAILURES:
                channel.send(("native_failure", {"reason_code": failure.code,
                    "worker_failure": _FAILURES[failure.code]}))
            else:
                channel.send(("failure", failure.code))
        else:
            channel.send(("failure", failure.code))
    except Exception:
        channel.send(("failure", "adapter_error"))
    finally:
        channel.close()

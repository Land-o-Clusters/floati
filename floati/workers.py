"""Authority-first worker execution and receipt-only worker state."""

from __future__ import annotations

import multiprocessing
import hashlib
import json
import os
import signal
import socket
import stat
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, Mapping, Optional, Protocol, Sequence

from .errors import IntegrityFailure, ProtocolRefusal
from .ids import uuid7_hex
from .jsonl import append_record, read_records_snapshot, transact
from .records import WORKER_OUTCOME_CODES, validate_artifact_bindings, validate_record
from .registry import Registry
from .root import FloatiRoot
from .work import WORK_KINDS, WorkLog
from .worker_isolation import (
    WorkerIsolationPolicy,
    apply_worker_isolation,
    cleanup_worker_isolation,
    prepare_worker_isolation,
)
from .worker_errors import WorkerAdapterFailure
from .worker_bootstrap_protocol import (
    BootstrapChannel,
    BuiltInAdapterSpec,
    builtin_adapter_spec_to_payload,
    isolation_policy_to_payload,
    validate_builtin_adapter_spec,
    validate_isolation_backend,
)
from .worker_adapter_runtime import run_adapter_session
from .worker_exec import (
    SpawnedWorkerProcess,
    _validated_bootstrap_path,
    spawn_effect_worker,
)


WORKER_KINDS = {"worker_receipt"}
WORKER_REFUSAL_KINDS = {"worker_refusal"}

try:
    from .effects import (
        EffectController as _EffectController,
        _worker_effect_operation,
        _worker_uncertain_operations,
    )
except ModuleNotFoundError as exc:
    if exc.name != f"{__package__}.effects":
        raise
    _EFFECT_CONTROLLER_TYPE = None
    _EFFECT_WORKER_OPERATIONS = ()
else:
    _EFFECT_CONTROLLER_TYPE = _EffectController
    _EFFECT_WORKER_OPERATIONS = (
        _EffectController.intent,
        _EffectController.dispatched,
        _EffectController.acknowledged,
        _EffectController.failed,
        _EffectController.unknown,
        _worker_effect_operation,
        _worker_uncertain_operations,
        _EffectController._require_worker_pipe_receive,
    )

_EFFECT_EVENT_FIELDS = {
    "intent": frozenset({
        "verb", "effect_type", "target", "request_digest",
        "idempotency_key", "expected_confirmation",
        "reconciliation_adapter", "risk_class", "budget_claim",
        "requested_by", "approval_request_id", "approval_decision_id",
        "approval_consumption_id",
    }),
    "dispatch": frozenset({
        "verb", "idempotency_key", "dispatch_adapter",
        "dispatch_evidence_digest",
    }),
    "acknowledgement": frozenset({
        "verb", "idempotency_key", "acknowledgement_digest",
    }),
    "failure": frozenset({
        "verb", "idempotency_key", "reason_code", "evidence_digest",
        "spend_status", "measured_spend",
    }),
    "unknown": frozenset({
        "verb", "idempotency_key", "reason_code", "evidence_digest",
        "spend_status", "measured_spend",
    }),
}

_WORKER_BOOTSTRAP_PATH = Path(__file__).resolve().with_name("worker_bootstrap.py")


def _cleanup_unused_prepared_workspace(policy: WorkerIsolationPolicy) -> None:
    """Remove only the still-identical empty workspace from a pre-ready failure."""

    if policy.workspace is None or policy.workspace_identity is None:
        return
    path = policy.workspace
    try:
        metadata = os.lstat(path)
        resolved = path.resolve(strict=True)
        parent = path.parent.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise WorkerAdapterFailure("adapter_error") from exc
    if (
        resolved != path
        or resolved.parent != parent
        or stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or (metadata.st_dev, metadata.st_ino) != policy.workspace_identity
    ):
        raise WorkerAdapterFailure("adapter_error")
    try:
        os.rmdir(path)
    except OSError as exc:
        raise WorkerAdapterFailure("adapter_error") from exc


def _bootstrap_channel_at_eof(connection: object) -> bool:
    """Distinguish a clean stream boundary from a truncated canonical frame."""

    if type(connection) is not BootstrapChannel:
        return False
    try:
        return connection._socket.recv(1, socket.MSG_PEEK) == b""
    except (BlockingIOError, InterruptedError):
        return False
    except OSError as exc:
        raise WorkerAdapterFailure("process_died") from exc


def _receive_worker_frame(connection: object, deadline: float) -> object:
    """Receive one frame without widening legacy multiprocessing semantics."""

    if type(connection) is BootstrapChannel:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Worker receive deadline exceeded")
        return connection.recv(remaining)
    return connection.recv()


def _now(value: Optional[datetime]) -> datetime:
    current = datetime.now(timezone.utc) if value is None else value
    if not isinstance(current, datetime) or current.tzinfo is None or current.utcoffset() is None:
        raise ProtocolRefusal("time_invalid", "an aware UTC-compatible datetime is required")
    return current.astimezone(timezone.utc)


def _timestamp(value: datetime) -> str:
    return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _parse_time(value: object) -> datetime:
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)


class WorkerAdapter(Protocol):
    name: str
    cancel_mode: str

    def spawn(self, item: Dict[str, object], *, deadline_seconds: float) -> object: ...

    def drive(
        self, handle: object, item: Dict[str, object], *, deadline_seconds: float
    ) -> list[Dict[str, str]]: ...


class CodexBoundaryAdapter:
    """Fail closed before the credential-owning, network-backed live route."""

    name = "codex"
    cancel_mode = "unavailable"

    def spawn(self, item: Dict[str, object], *, deadline_seconds: float) -> object:
        raise WorkerAdapterFailure("credential_network_boundary_unruled")

    def drive(
        self, handle: object, item: Dict[str, object], *, deadline_seconds: float
    ) -> list[Dict[str, str]]:
        raise WorkerAdapterFailure("credential_network_boundary_unruled")


def _adapter_process(
    connection: object,
    adapter: WorkerAdapter,
    item: Dict[str, object],
    deadline_seconds: float,
    spawn_context: Optional[Dict[str, object]] = None,
    effect_context: Optional[Dict[str, object]] = None,
    isolation_policy: Optional[WorkerIsolationPolicy] = None,
) -> None:
    try:
        os.setsid()
        if isolation_policy is not None:
            backend = validate_isolation_backend(
                apply_worker_isolation(isolation_policy)
            )
            connection.send(("isolation_ready", {"backend": backend}))
        run_adapter_session(
            connection,
            adapter,
            item,
            deadline_seconds,
            spawn_context,
            effect_context,
            isolation_policy,
        )
    except WorkerAdapterFailure as failure:
        connection.send(("failure", failure.code))
    except Exception:
        connection.send(("failure", "adapter_error"))
    finally:
        connection.close()


class WorkerReceipts:
    _NEXT = {
        "claim": {"spawn", "degrade"},
        "spawn": {"drive", "degrade"},
        "drive": {"bind_artifact", "degrade"},
        "bind_artifact": {"complete", "degrade"},
    }
    _STATES = {
        "claim": "claim",
        "spawn": "driving",
        "drive": "driving",
        "bind_artifact": "driving",
        "complete": "complete",
        "degrade": "degraded",
    }

    def __init__(self, root: FloatiRoot) -> None:
        self.root = root
        self.relative_path = Path("receipts/workers.jsonl")

    def records(self) -> list[Dict[str, object]]:
        return read_records_snapshot(self.root, self.relative_path, allowed_kinds=WORKER_KINDS)

    def append(
        self,
        session_id: str,
        work_item_id: str,
        node_id: str,
        adapter: str,
        transition: str,
        outcome_code: Optional[str],
        artifact_bindings: Sequence[Dict[str, str]],
        *,
        now: Optional[datetime] = None,
    ) -> Dict[str, object]:
        current = _now(now)
        item = WorkLog(self.root).show(work_item_id)[0]
        if item["holder"] != node_id:
            raise ProtocolRefusal("worker_claim_missing", "worker receipt requires the exact work claim")
        if transition == "complete":
            if item["state"] != "completed":
                raise ProtocolRefusal("worker_completion_missing", "complete receipt requires work completion")
            completion = self._completion_record(work_item_id)
            if completion is None or completion["artifact_bindings"] != list(artifact_bindings):
                raise ProtocolRefusal(
                    "worker_completion_mismatch",
                    "complete receipt bindings must match the work completion",
                )
        elif item["state"] != "claimed":
            raise ProtocolRefusal("worker_claim_missing", "worker receipt requires an active work claim")
        record: Dict[str, object] = {
            "schema_version": 0,
            "id": "worker-receipt-" + uuid7_hex(),
            "tenant_id": self.root.tenant_id,
            "timestamp": _timestamp(current),
            "kind": "worker_receipt",
            "session_id": session_id,
            "work_item_id": work_item_id,
            "node_id": node_id,
            "adapter": adapter,
            "transition": transition,
            "outcome_code": outcome_code,
            "authority_subject": item["authority_subject"],
            "authority_epoch": item["authority_epoch"],
            "artifact_bindings": list(artifact_bindings),
        }
        validate_record(record, self.root.tenant_id, frozenset(WORKER_KINDS), integrity=False)

        def decide(records: list[Dict[str, object]]) -> tuple[Dict[str, object], Dict[str, object]]:
            projected = self._project(records)
            prior = [existing for existing in records if existing["session_id"] == session_id]
            if not prior:
                if transition != "claim":
                    raise ProtocolRefusal("worker_transition_invalid", "worker session must begin with claim")
                if any(row["work_item_id"] == work_item_id for row in projected):
                    raise ProtocolRefusal(
                        "worker_claim_already_bound", "work claim already has a worker session"
                    )
            else:
                latest = prior[-1]
                if (
                    latest["work_item_id"] != work_item_id
                    or latest["node_id"] != node_id
                    or latest["adapter"] != adapter
                    or latest["authority_subject"] != item["authority_subject"]
                    or latest["authority_epoch"] != item["authority_epoch"]
                ):
                    raise ProtocolRefusal("worker_session_mismatch", "worker session identity cannot change")
                if transition not in self._NEXT.get(str(latest["transition"]), set()):
                    raise ProtocolRefusal("worker_transition_invalid", "worker receipt transition is out of order")
                if transition == "complete" and latest["artifact_bindings"] != record["artifact_bindings"]:
                    raise ProtocolRefusal(
                        "worker_completion_mismatch",
                        "bound artifacts must match the work completion",
                    )
            return record, record

        return transact(
            self.root, self.relative_path, decide, allowed_kinds=WORKER_KINDS
        )

    def _completion_record(self, work_item_id: str) -> Optional[Dict[str, object]]:
        records = read_records_snapshot(
            self.root, Path("work/items.jsonl"), allowed_kinds=WORK_KINDS
        )
        for record in reversed(records):
            if (
                record["kind"] == "work_transition"
                and record["action"] == "complete"
                and record["work_item_id"] == work_item_id
            ):
                return record
        return None

    def sessions(self) -> list[Dict[str, object]]:
        return self._project(self.records())

    def _project(self, records: Sequence[Dict[str, object]]) -> list[Dict[str, object]]:
        projected: Dict[str, Dict[str, object]] = {}
        work_sessions: Dict[str, str] = {}
        for record in records:
            session_id = str(record["session_id"])
            row = projected.get(session_id)
            if row is None:
                if record["transition"] != "claim":
                    raise IntegrityFailure(
                        "worker_transition_invalid", "durable worker session does not begin with claim"
                    )
                work_item_id = str(record["work_item_id"])
                if work_item_id in work_sessions:
                    raise IntegrityFailure(
                        "worker_claim_already_bound", "durable work claim has multiple worker sessions"
                    )
                work_sessions[work_item_id] = session_id
                row = {
                    "session_id": session_id,
                    "work_item_id": record["work_item_id"],
                    "node_id": record["node_id"],
                    "adapter": record["adapter"],
                    "authority_subject": record["authority_subject"],
                    "authority_epoch": record["authority_epoch"],
                    "artifact_bindings": [],
                }
                projected[session_id] = row
            else:
                if (
                    row["work_item_id"] != record["work_item_id"]
                    or row["node_id"] != record["node_id"]
                    or row["adapter"] != record["adapter"]
                    or row["authority_subject"] != record["authority_subject"]
                    or row["authority_epoch"] != record["authority_epoch"]
                ):
                    raise IntegrityFailure(
                        "worker_session_mismatch", "durable worker session identity changed"
                    )
                if record["transition"] not in self._NEXT.get(str(row["transition"]), set()):
                    raise IntegrityFailure(
                        "worker_transition_invalid", "durable worker transition is out of order"
                    )
            row["transition"] = record["transition"]
            row["state"] = self._STATES[str(record["transition"])]
            row["outcome_code"] = record["outcome_code"]
            row["last_activity"] = record["timestamp"]
            for binding in record["artifact_bindings"]:
                if binding not in row["artifact_bindings"]:
                    row["artifact_bindings"].append(binding)
        return list(projected.values())


class WorkerRefusals:
    def __init__(self, root: FloatiRoot) -> None:
        self.root = root
        self.relative_path = Path("receipts/worker-refusals.jsonl")

    def records(self) -> list[Dict[str, object]]:
        return read_records_snapshot(
            self.root, self.relative_path, allowed_kinds=WORKER_REFUSAL_KINDS
        )

    def append(
        self,
        node_id: str,
        adapter: str,
        work_item_id: Optional[str],
        reason_code: str,
        *,
        now: Optional[datetime] = None,
    ) -> Dict[str, object]:
        record: Dict[str, object] = {
            "schema_version": 0,
            "id": "worker-refusal-" + uuid7_hex(),
            "tenant_id": self.root.tenant_id,
            "timestamp": _timestamp(_now(now)),
            "kind": "worker_refusal",
            "node_id": node_id,
            "adapter": adapter,
            "work_item_id": work_item_id,
            "reason_code": reason_code,
        }
        validate_record(
            record, self.root.tenant_id, frozenset(WORKER_REFUSAL_KINDS), integrity=False
        )
        append_record(
            self.root, self.relative_path, record, allowed_kinds=WORKER_REFUSAL_KINDS
        )
        return record


class WorkerRunner:
    def __init__(
        self,
        root: FloatiRoot,
        adapters: Mapping[str, WorkerAdapter],
        *,
        call_timeout: float = 60.0,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        spawn_controller: object = None,
        effect_controller: object = None,
        effect_adapter_specs: Optional[Mapping[str, BuiltInAdapterSpec]] = None,
    ) -> None:
        if (
            not isinstance(call_timeout, (int, float))
            or isinstance(call_timeout, bool)
            or not 0.01 <= float(call_timeout) <= 60
        ):
            raise ProtocolRefusal(
                "worker_timeout_invalid", "worker call timeout must be 0.01 through 60 seconds"
            )
        self.root = root
        self.adapters = dict(adapters)
        self.call_timeout = float(call_timeout)
        self.clock = clock
        self.spawn_controller = spawn_controller
        self.effect_controller = effect_controller
        if effect_adapter_specs is None:
            detached_specs: dict[str, BuiltInAdapterSpec] = {}
        elif type(effect_adapter_specs) is not dict:
            raise WorkerAdapterFailure("effect_worker_isolation_unavailable")
        else:
            detached_specs = {}
            kinds: set[str] = set()
            for name, candidate in effect_adapter_specs.items():
                if type(name) is not str:
                    raise WorkerAdapterFailure(
                        "effect_worker_isolation_unavailable"
                    )
                spec = validate_builtin_adapter_spec(candidate)
                if spec.kind in kinds:
                    raise WorkerAdapterFailure(
                        "effect_worker_isolation_unavailable"
                    )
                kinds.add(spec.kind)
                detached_specs[name] = spec
        self._effect_adapter_specs = detached_specs
        self.work = WorkLog(root)
        self.receipts = WorkerReceipts(root)
        self.refusals = WorkerRefusals(root)
        self.last_process_audit: Dict[str, object] = {
            "adapter_pid": None,
            "registered_process_groups": [],
            "alive_after_cleanup": [],
            "isolation_backend": None,
        }

    def run(
        self,
        node_id: str,
        adapter_name: str,
        *,
        now: Optional[datetime] = None,
        on_drive: Optional[Callable[[], None]] = None,
        run_id: Optional[str] = None,
        item_id: Optional[str] = None,
        attempt_id: Optional[str] = None,
    ) -> Dict[str, object]:
        self.last_process_audit = {
            "adapter_pid": None,
            "registered_process_groups": [],
            "alive_after_cleanup": [],
            "isolation_backend": None,
        }
        current = self._observe(now)
        try:
            Registry(self.root).require_active(node_id)
        except ProtocolRefusal:
            try:
                self.refusals.append(
                    node_id, adapter_name, None, "worker_node_inactive", now=current
                )
            except ProtocolRefusal:
                # Structurally invalid identifiers cannot form a valid refusal record.
                pass
            raise
        effect_enabled = self.effect_controller is not None
        adapter_spec = (
            self._effect_adapter_specs.get(adapter_name)
            if effect_enabled
            else None
        )
        adapter = None if effect_enabled else self.adapters.get(adapter_name)
        if not effect_enabled and adapter is None:
            self.refusals.append(node_id, adapter_name, None, "worker_adapter_absent", now=current)
            raise ProtocolRefusal("worker_adapter_absent", "requested worker adapter is unavailable")
        try:
            grant = self._active_authority(node_id, current)
        except ProtocolRefusal as refusal:
            self.refusals.append(
                node_id, adapter_name, None, refusal.code, now=current
            )
            raise
        except IntegrityFailure as failure:
            self.refusals.append(
                node_id,
                adapter_name,
                None,
                "authority_state_unavailable",
                now=current,
            )
            raise IntegrityFailure(
                "authority_state_unavailable",
                "validated authority state is unavailable",
            ) from failure
        try:
            item = self.work.claim_owned_oldest(
                node_id,
                str(grant["subject_id"]),
                int(grant["epoch"]),
                now=current,
            )
        except IntegrityFailure as failure:
            reason = (
                "consumption_state_unavailable"
                if failure.code == "consumption_state_unavailable"
                else "authority_state_unavailable"
            )
            self.refusals.append(node_id, adapter_name, None, reason, now=current)
            raise
        except ProtocolRefusal as refusal:
            if refusal.code == "work_dependencies_blocked":
                self.refusals.append(
                    node_id, adapter_name, None, "worker_work_blocked", now=current
                )
                raise ProtocolRefusal(
                    "worker_work_blocked",
                    "owned work exists but its dependencies are incomplete",
                ) from refusal
            if refusal.code == "work_owned_open_absent":
                self.refusals.append(
                    node_id, adapter_name, None, "worker_work_absent", now=current
                )
                raise ProtocolRefusal(
                    "worker_work_absent", "no owned open work item is available"
                ) from refusal
            if refusal.code.startswith("authority_"):
                self.refusals.append(
                    node_id,
                    adapter_name,
                    None,
                    "worker_authority_changed",
                    now=current,
                )
                raise ProtocolRefusal(
                    "worker_authority_changed",
                    "authority changed before the atomic work claim",
                ) from refusal
            self.refusals.append(
                node_id, adapter_name, None, "worker_claim_lost", now=current
            )
            raise
        session_id = "worker-" + uuid7_hex()
        last = self.receipts.append(
            session_id, str(item["id"]), node_id, adapter_name, "claim", None, [], now=self._observe(now)
        )
        if effect_enabled and (
            adapter_spec is None or adapter_spec.kind != adapter_name
        ):
            return self.receipts.append(
                session_id,
                str(item["id"]),
                node_id,
                adapter_name,
                "degrade",
                "effect_worker_isolation_unavailable",
                [],
                now=self._observe(now),
            )
        requires_workspace = (
            effect_enabled or getattr(adapter, "requires_workspace", False)
        )
        if requires_workspace and item.get("workspace") is None:
            self.refusals.append(
                node_id,
                adapter_name,
                str(item["id"]),
                "worker_workspace_missing",
                now=self._observe(now),
            )
            return self.receipts.append(
                session_id,
                str(item["id"]),
                node_id,
                adapter_name,
                "degrade",
                "workspace_mapping_missing",
                [],
                now=self._observe(now),
            )
        launch_observed = self._observe(now)
        try:
            authority_status = self._authority_status(
                grant, node_id, launch_observed
            )
        except IntegrityFailure:
            self.refusals.append(
                node_id,
                adapter_name,
                str(item["id"]),
                "authority_state_unavailable",
                now=launch_observed,
            )
            return self.receipts.append(
                session_id,
                str(item["id"]),
                node_id,
                adapter_name,
                "degrade",
                "authority_state_unavailable",
                [],
                now=launch_observed,
            )
        if authority_status != "current":
            self.refusals.append(
                node_id,
                adapter_name,
                str(item["id"]),
                "worker_authority_changed",
                now=launch_observed,
            )
            return self.receipts.append(
                session_id,
                str(item["id"]),
                node_id,
                adapter_name,
                "degrade",
                (
                    "authority_expired_mid_claim"
                    if authority_status == "expired"
                    else "worker_authority_changed"
                ),
                [],
                now=launch_observed,
            )
        ttl_remaining = (
            _parse_time(grant["expires_at"]) - launch_observed
        ).total_seconds() - 1.0
        effective_deadline = min(
            float(grant["deadline_seconds"]), ttl_remaining, self.call_timeout
        )
        if effective_deadline <= 0:
            return self.receipts.append(
                session_id,
                str(item["id"]),
                node_id,
                adapter_name,
                "degrade",
                "authority_deadline_unavailable",
                [],
                now=launch_observed,
            )
        process_deadline = time.monotonic() + effective_deadline
        process: Optional[object] = None
        parent: Optional[object] = None
        child: Optional[object] = None
        process_started = False
        child_process_groups: set[int] = set()
        effect_context = self._governed_effect_context(
            adapter_name, adapter=adapter, adapter_spec=adapter_spec,
            run_id=run_id, item_id=item_id,
            attempt_id=attempt_id, claimed_work_item_id=str(item["id"]),
        )
        effect_operations = _EFFECT_WORKER_OPERATIONS if effect_context is not None else ()
        effect_application: Optional[tuple[object, ...]] = None
        effect_application_identity: object = None
        end_effect_application: Optional[Callable[[], None]] = None
        effect_boundary_closed = False
        spawn_context = self._governed_spawn_context(
            adapter_name, adapter=adapter, adapter_spec=adapter_spec,
            run_id=run_id, item_id=item_id,
            attempt_id=attempt_id, claimed_work_item_id=str(item["id"]),
        )
        spawn_descendant_application: Optional[
            tuple[object, str, str, object, object, object]
        ] = None
        end_spawn_launch: Optional[Callable[[], None]] = None
        spawn_result_handler: Optional[
            Callable[[Optional[datetime]], Dict[str, object]]
        ] = None
        isolation_policy: Optional[WorkerIsolationPolicy] = None
        isolation_cleanup_attempted = False
        isolation_backend: Optional[str] = None
        terminal_failure: Optional[str] = None

        def clean_isolation_after_exit() -> None:
            nonlocal isolation_cleanup_attempted
            if isolation_policy is None or isolation_cleanup_attempted:
                return
            if process is not None:
                process.join(0)
                if process.is_alive():
                    raise WorkerAdapterFailure("adapter_error")
            isolation_cleanup_attempted = True
            try:
                cleanup_worker_isolation(isolation_policy)
                if isolation_backend is None:
                    _cleanup_unused_prepared_workspace(isolation_policy)
            except Exception as exc:
                raise WorkerAdapterFailure("adapter_error") from exc

        try:
            if effect_context is not None:
                bootstrap_path = _validated_bootstrap_path(
                    _WORKER_BOOTSTRAP_PATH
                )
                try:
                    self.root.resolve_relative(Path("effects")).mkdir(
                        mode=0o700, exist_ok=True,
                    )
                except OSError as exc:
                    raise WorkerAdapterFailure("adapter_error") from exc
                workspace = Path(str(item["workspace"]))
                isolation_policy = prepare_worker_isolation(
                    self.root.tenant_home, workspace, session_id,
                )
            try:
                if effect_context is not None:
                    if adapter_spec is None or isolation_policy is None:
                        raise WorkerAdapterFailure(
                            "effect_worker_isolation_unavailable"
                        )
                    launch_payload = {
                        "schema_version": 1,
                        "session_id": session_id,
                        "adapter": builtin_adapter_spec_to_payload(adapter_spec),
                        "item": dict(item),
                        "deadline_millis": max(
                            10, min(60_000, int(effective_deadline * 1_000))
                        ),
                        "spawn_context": spawn_context,
                        "effect_context": effect_context,
                        "isolation_policy": isolation_policy_to_payload(
                            isolation_policy
                        ),
                    }
                    process, parent = spawn_effect_worker(
                        bootstrap_path, launch_payload,
                    )
                else:
                    context = multiprocessing.get_context("fork")
                    parent, child = context.Pipe()
                    process_type = context.Process
                    process = process_type(
                        target=_adapter_process,
                        args=(
                            child, adapter, item, effective_deadline,
                            spawn_context, effect_context, isolation_policy,
                        ),
                        name="floati-worker-adapter",
                    )
                    process.start()
                process_started = True
                if child is not None:
                    child.close()
                    child = None
                if effect_context is not None:
                    if (
                        self.effect_controller is None
                        or type(getattr(process, "pid", None)) is not int
                        or parent is None
                    ):
                        raise ProtocolRefusal(
                            "effect_application_capability_invalid",
                            "one started WorkerRunner process and live parent pipe must own effect application",
                        )
                    effect_application_identity = object()
                    effect_application_active = [True]
                    effect_application_owner_pid = os.getpid()
                    effect_application_frame = sys._getframe()
                    effect_application_controller = self.effect_controller
                    effect_application_process = process
                    effect_application_connection = parent
                    effect_application_context = effect_context

                    def authorize_effect_application(
                        candidate: object,
                        controller: object,
                        candidate_context: object,
                    ) -> object:
                        if (
                            not effect_application_active[0]
                            or os.getpid() != effect_application_owner_pid
                            or candidate is not effect_application_identity
                            or controller is not effect_application_controller
                            or candidate_context is not effect_application_context
                            or effect_application_process is not process
                            or effect_application_connection is not parent
                        ):
                            return None
                        return (
                            effect_application_process,
                            effect_application_connection,
                            effect_application_owner_pid,
                            effect_application_frame,
                        )

                    def release_effect_application() -> None:
                        effect_application_active[0] = False

                    (
                        intent, dispatched, acknowledged, failed, unknown,
                        resolve_operation, uncertain_operations,
                        require_pipe_receive,
                    ) = effect_operations
                    effect_application = (
                        effect_application_controller, effect_application_context,
                        intent, dispatched, acknowledged, failed, unknown,
                        resolve_operation, uncertain_operations,
                        require_pipe_receive, effect_application_identity,
                        authorize_effect_application,
                    )
                    end_effect_application = release_effect_application
                if (
                    spawn_context is not None
                    and spawn_context["subagents_mode"] in {"observed_only", "managed"}
                ):
                    if (
                        self.spawn_controller is None
                        or type(getattr(process, "pid", None)) is not int
                        or parent is None
                    ):
                        raise ProtocolRefusal(
                            "spawn_launch_capability_invalid",
                            "one started WorkerRunner process and live parent pipe must own descendant authority",
                        )
                    launch_identity = object()
                    launch_active = [True]
                    launch_owner_pid = os.getpid()
                    launch_frame = sys._getframe()
                    launch_controller = self.spawn_controller
                    launch_process = process
                    launch_connection = parent
                    launch_run_id = str(run_id)
                    launch_attempt_id = str(attempt_id)
                    launch_adapter = adapter_name

                    def authorize_launch(
                        candidate: object,
                        controller: object,
                        candidate_run_id: str,
                        candidate_attempt_id: str,
                        candidate_adapter: str,
                    ) -> object:
                        if (
                            not launch_active[0]
                            or os.getpid() != launch_owner_pid
                            or candidate is not launch_identity
                            or controller is not launch_controller
                            or (
                                candidate_run_id,
                                candidate_attempt_id,
                                candidate_adapter,
                            ) != (
                                launch_run_id,
                                launch_attempt_id,
                                launch_adapter,
                            )
                            or launch_process is not process
                            or launch_connection is not parent
                        ):
                            return None
                        return (
                            launch_process,
                            launch_connection,
                            launch_owner_pid,
                            launch_frame,
                        )

                    def close_observation(
                        observed_at: Optional[datetime],
                    ) -> Dict[str, object]:
                        return launch_controller.close_descendant_observation(
                            launch_run_id,
                            launch_attempt_id,
                            _launch_capability=launch_identity,
                            _launch_authorizer=authorize_launch,
                            now=observed_at,
                        )

                    def release_launch() -> None:
                        launch_active[0] = False

                    from .spawn_groups import SpawnGroupController

                    if not isinstance(launch_controller, SpawnGroupController):
                        raise ProtocolRefusal(
                            "spawn_observation_controller_missing",
                            "observed launch requires the parent-owned controller before fork",
                        )
                    descendant_application = (
                        SpawnGroupController.record_untracked_descendant
                    )
                    spawn_descendant_application = (
                        launch_controller,
                        launch_run_id,
                        launch_attempt_id,
                        launch_identity,
                        authorize_launch,
                        descendant_application,
                    )
                    spawn_result_handler = close_observation
                    end_spawn_launch = release_launch
            except (OSError, RuntimeError, ValueError) as exc:
                raise WorkerAdapterFailure("process_start_failed") from exc
            if effect_context is not None:
                isolation_backend = self._receive_isolation_ready(
                    parent, process, process_deadline,
                )
            status, value = self._receive(
                parent, process, process_deadline, child_process_groups,
                spawn_descendant_application,
                effect_application=effect_application,
            )
            if status == "failure":
                code = str(value)
                raise WorkerAdapterFailure(code if code in WORKER_OUTCOME_CODES else "adapter_error")
            if status != "spawned":
                raise WorkerAdapterFailure("adapter_error")
            last = self.receipts.append(
                session_id, str(item["id"]), node_id, adapter_name, "spawn", None, [], now=self._observe(now)
            )
            last = self.receipts.append(
                session_id, str(item["id"]), node_id, adapter_name, "drive", None, [], now=self._observe(now)
            )
            if on_drive is not None:
                on_drive()
            status, value = self._receive(
                parent, process, process_deadline, child_process_groups,
                spawn_descendant_application, spawn_result_handler,
                effect_application=effect_application,
            )
            if status == "failure":
                code = str(value)
                raise WorkerAdapterFailure(code if code in WORKER_OUTCOME_CODES else "adapter_error")
            if status != "result":
                raise WorkerAdapterFailure("adapter_error")
            result_bindings = value
            if effect_application is not None:
                try:
                    parent.send(("effect_reporting_closed", None))
                except (BrokenPipeError, EOFError, OSError) as exc:
                    raise WorkerAdapterFailure("process_died") from exc
                effect_status, effect_value = self._receive(
                    parent, process, process_deadline, child_process_groups,
                    effect_application=effect_application,
                    effect_events_allowed=False,
                )
                if (
                    effect_status != "effect_reporting_closed_ack"
                    or effect_value is not None
                ):
                    raise WorkerAdapterFailure("adapter_error")
                if not (
                    spawn_context is not None
                    and spawn_context["subagents_mode"] in {"observed_only", "managed"}
                ):
                    self._finish_effect_process(
                        parent, process, process_deadline,
                    )
                    effect_boundary_closed = True
            if spawn_context is not None and spawn_context["subagents_mode"] in {"observed_only", "managed"}:
                if spawn_result_handler is None:
                    raise WorkerAdapterFailure("spawn_observation_controller_missing")
                try:
                    parent.send(("observation_closed", None))
                except (BrokenPipeError, EOFError, OSError) as exc:
                    raise WorkerAdapterFailure("process_died") from exc
                final_status, final_value = self._receive(
                    parent, process, process_deadline, child_process_groups,
                    effect_application=effect_application,
                    effect_events_allowed=False,
                )
                if final_status == "failure":
                    code = str(final_value)
                    raise WorkerAdapterFailure(
                        code if code in WORKER_OUTCOME_CODES else "adapter_error"
                    )
                if final_status != "observation_closed_ack" or final_value is not None:
                    raise WorkerAdapterFailure("adapter_error")
                if effect_application is not None:
                    self._finish_effect_process(
                        parent, process, process_deadline,
                    )
                    effect_boundary_closed = True
            clean_isolation_after_exit()
            try:
                bindings = validate_artifact_bindings(result_bindings)
            except ProtocolRefusal as exc:
                raise WorkerAdapterFailure("adapter_malformed_output") from exc
            observed = self._observe(now)
            try:
                authority_status = self._authority_status(
                    grant, node_id, observed
                )
            except IntegrityFailure as exc:
                self.refusals.append(
                    node_id,
                    adapter_name,
                    str(item["id"]),
                    "authority_state_unavailable",
                    now=observed,
                )
                raise WorkerAdapterFailure("authority_state_unavailable") from exc
            if authority_status != "current":
                self.refusals.append(
                    node_id,
                    adapter_name,
                    str(item["id"]),
                    "worker_authority_changed",
                    now=observed,
                )
                raise WorkerAdapterFailure(
                    "authority_expired_mid_claim"
                    if authority_status == "expired"
                    else "worker_authority_changed"
                )
            last = self.receipts.append(
                session_id, str(item["id"]), node_id, adapter_name, "bind_artifact", None, bindings, now=self._observe(now)
            )
            self.work.complete(str(item["id"]), node_id, bindings, now=self._observe(now))
            last = self.receipts.append(
                session_id, str(item["id"]), node_id, adapter_name, "complete", None, bindings, now=self._observe(now)
            )
        except WorkerAdapterFailure as failure:
            if (
                effect_application is not None
                and not effect_boundary_closed
                and failure.code in {"process_died", "process_timeout"}
            ):
                self._record_worker_effect_uncertainty(
                    effect_application, now=self._observe(now),
                )
            terminal_failure = (
                failure.code
                if failure.code in WORKER_OUTCOME_CODES
                else "adapter_error"
            )
        except KeyboardInterrupt:
            if effect_application is not None and not effect_boundary_closed:
                self._record_worker_effect_uncertainty(
                    effect_application, now=self._observe(now),
                )
            terminal_failure = "process_cancelled"
        finally:
            if end_effect_application is not None:
                end_effect_application()
            if end_spawn_launch is not None:
                end_spawn_launch()
            if process is not None and process_started:
                for process_group in sorted(child_process_groups):
                    self._terminate_process_group(process_group)
                if type(process) is SpawnedWorkerProcess:
                    # A validated readiness frame binds this exact still-
                    # waitable group. Escalate while its unreaped leader
                    # prevents numeric reuse; only then may join observe exit.
                    if not process.shutdown_process_group():
                        process.terminate()
                else:
                    process.join(0.1)
                    self._terminate_process_group(process.pid)
                process.join(1)
                if process.is_alive():
                    if type(process) is SpawnedWorkerProcess:
                        process.kill()
                    else:
                        try:
                            os.killpg(process.pid, signal.SIGKILL)
                        except (ProcessLookupError, PermissionError):
                            pass
                    process.join(1)
            if parent is not None:
                parent.close()
            if child is not None:
                child.close()
            adapter_pid = None if process is None else process.pid
            if type(process) is SpawnedWorkerProcess:
                candidates = list(sorted(child_process_groups))
                if (
                    process_started
                    and process.is_alive()
                    and isinstance(adapter_pid, int)
                ):
                    candidates.append(adapter_pid)
            else:
                candidates = [
                    value
                    for value in [adapter_pid, *sorted(child_process_groups)]
                    if isinstance(value, int)
                ]
            cleanup_deadline = time.monotonic() + 0.5
            alive = [value for value in candidates if self._process_group_alive(value)]
            while alive and time.monotonic() < cleanup_deadline:
                time.sleep(0.01)
                alive = [value for value in candidates if self._process_group_alive(value)]
            self.last_process_audit = {
                "adapter_pid": adapter_pid,
                "registered_process_groups": sorted(child_process_groups),
                "alive_after_cleanup": alive,
                "isolation_backend": isolation_backend,
            }
            try:
                clean_isolation_after_exit()
            except WorkerAdapterFailure:
                terminal_failure = "adapter_error"
        if terminal_failure is not None:
            last = self.receipts.append(
                session_id,
                str(item["id"]),
                node_id,
                adapter_name,
                "degrade",
                terminal_failure,
                [],
                now=self._observe(now),
            )
        return last

    def _observe(self, fixed: Optional[datetime]) -> datetime:
        return _now(fixed if fixed is not None else self.clock())

    def _receive(
        self,
        connection: object,
        process: object,
        deadline: float,
        child_process_groups: set[int],
        spawn_descendant_application: Optional[
            tuple[object, str, str, object, object, object]
        ] = None,
        spawn_result_handler: Optional[Callable[[Optional[datetime]], Dict[str, object]]] = None,
        *,
        effect_application: Optional[tuple[object, ...]] = None,
        effect_events_allowed: bool = True,
    ) -> tuple[str, object]:
        descendant_application = None
        if spawn_descendant_application is not None:
            descendant_application = spawn_descendant_application[5]
            if not callable(descendant_application):
                raise WorkerAdapterFailure("adapter_error")
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not connection.poll(remaining):
                if process.is_alive():
                    raise WorkerAdapterFailure("process_timeout")
                raise WorkerAdapterFailure("process_died")
            if _bootstrap_channel_at_eof(connection):
                raise WorkerAdapterFailure("process_died")
            received_at = (
                self._observe(None)
                if (
                    descendant_application is not None
                    or spawn_result_handler is not None
                    or effect_application is not None
                )
                else None
            )
            try:
                result = _receive_worker_frame(connection, deadline)
            except TimeoutError as exc:
                code = "process_timeout" if process.is_alive() else "process_died"
                raise WorkerAdapterFailure(code) from exc
            except (EOFError, OSError) as exc:
                raise WorkerAdapterFailure("process_died") from exc
            except WorkerAdapterFailure as exc:
                raise WorkerAdapterFailure("adapter_error") from exc
            if not isinstance(result, tuple) or len(result) != 2:
                raise WorkerAdapterFailure("adapter_error")
            status, value = result
            if (
                status == "process_group"
                and type(process) is SpawnedWorkerProcess
            ):
                raise WorkerAdapterFailure("adapter_error")
            if status == "effect":
                if effect_application is None or not effect_events_allowed:
                    raise WorkerAdapterFailure("protocol_error")
                try:
                    effect_event = self._effect_event_snapshot(value)
                    self._apply_effect_event(
                        effect_application, effect_event, received_at,
                    )
                except (IntegrityFailure, ProtocolRefusal) as exc:
                    raise WorkerAdapterFailure("protocol_error") from exc
                continue
            if status == "descendant":
                if (
                    spawn_descendant_application is None
                    or descendant_application is None
                ):
                    raise WorkerAdapterFailure("adapter_error")
                descendant_snapshot = self._descendant_snapshot(value)
                (
                    controller,
                    run_id,
                    attempt_id,
                    launch_identity,
                    launch_authorizer,
                    descendant_application,
                ) = spawn_descendant_application
                descendant_application(
                    controller,
                    run_id,
                    attempt_id,
                    descendant_snapshot[0],
                    descendant_snapshot[1],
                    adopted_item_id=descendant_snapshot[2],
                    _launch_capability=launch_identity,
                    _launch_authorizer=launch_authorizer,
                    now=received_at,
                )
                continue
            if status == "result" and spawn_result_handler is not None:
                spawn_result_handler(received_at)
            if status != "process_group":
                if (
                    effect_application is not None
                    and not effect_events_allowed
                    and connection.poll(min(0.02, max(0.0, deadline - time.monotonic())))
                ):
                    if _bootstrap_channel_at_eof(connection):
                        return status, value
                    try:
                        trailing = _receive_worker_frame(connection, deadline)
                    except TimeoutError as exc:
                        code = (
                            "process_timeout" if process.is_alive()
                            else "process_died"
                        )
                        raise WorkerAdapterFailure(code) from exc
                    except EOFError:
                        return status, value
                    except OSError as exc:
                        raise WorkerAdapterFailure("process_died") from exc
                    except WorkerAdapterFailure as exc:
                        raise WorkerAdapterFailure("adapter_error") from exc
                    if (
                        isinstance(trailing, tuple)
                        and len(trailing) == 2
                        and trailing[0] == "effect"
                    ):
                        raise WorkerAdapterFailure("protocol_error")
                    raise WorkerAdapterFailure("adapter_error")
                return status, value
            if (
                not isinstance(value, int)
                or isinstance(value, bool)
                or value <= 1
                or value == process.pid
            ):
                raise WorkerAdapterFailure("adapter_error")
            try:
                if os.getpgid(value) != value:
                    raise WorkerAdapterFailure("adapter_error")
            except ProcessLookupError:
                continue
            except PermissionError as exc:
                raise WorkerAdapterFailure("adapter_error") from exc
            child_process_groups.add(value)

    @staticmethod
    def _receive_isolation_ready(
        connection: object,
        process: object,
        deadline: float,
    ) -> str:
        """Consume the one required effect-Worker handshake before callbacks."""

        remaining = deadline - time.monotonic()
        if remaining <= 0 or not connection.poll(remaining):
            if process.is_alive():
                raise WorkerAdapterFailure("process_timeout")
            raise WorkerAdapterFailure("process_died")
        if _bootstrap_channel_at_eof(connection):
            raise WorkerAdapterFailure("process_died")
        try:
            frame = _receive_worker_frame(connection, deadline)
        except TimeoutError as exc:
            code = "process_timeout" if process.is_alive() else "process_died"
            raise WorkerAdapterFailure(code) from exc
        except (EOFError, OSError) as exc:
            raise WorkerAdapterFailure("process_died") from exc
        except WorkerAdapterFailure:
            raise
        if (
            isinstance(frame, tuple)
            and len(frame) == 2
            and frame[0] == "failure"
            and frame[1] == "effect_worker_isolation_unavailable"
        ):
            raise WorkerAdapterFailure("effect_worker_isolation_unavailable")
        if not isinstance(frame, tuple) or len(frame) != 2:
            raise WorkerAdapterFailure("effect_worker_isolation_unavailable")
        status, value = frame
        if (
            status != "isolation_ready"
            or type(value) is not dict
            or set(value) != {"backend"}
            or type(value["backend"]) is not str
        ):
            raise WorkerAdapterFailure("effect_worker_isolation_unavailable")
        backend = validate_isolation_backend(value["backend"])
        if (
            type(process) is SpawnedWorkerProcess
            and not process.confirm_process_group()
        ):
            raise WorkerAdapterFailure("effect_worker_isolation_unavailable")
        return backend

    @staticmethod
    def _finish_effect_process(
        connection: object,
        process: object,
        deadline: float,
    ) -> None:
        """Drain the closed reporting pipe through child exit or fail closed."""

        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                if process.is_alive():
                    raise WorkerAdapterFailure("process_timeout")
                raise WorkerAdapterFailure("protocol_error")
            if not connection.poll(remaining):
                if process.is_alive():
                    raise WorkerAdapterFailure("process_timeout")
                raise WorkerAdapterFailure("protocol_error")
            if _bootstrap_channel_at_eof(connection):
                process.join(min(0.05, max(0.0, deadline - time.monotonic())))
                if process.is_alive():
                    continue
                return
            try:
                trailing = _receive_worker_frame(connection, deadline)
            except TimeoutError as exc:
                if process.is_alive():
                    raise WorkerAdapterFailure("process_timeout") from exc
                raise WorkerAdapterFailure("protocol_error") from exc
            except EOFError:
                process.join(min(0.05, max(0.0, deadline - time.monotonic())))
                if process.is_alive():
                    continue
                return
            except OSError as exc:
                raise WorkerAdapterFailure("process_died") from exc
            except WorkerAdapterFailure as exc:
                raise WorkerAdapterFailure("adapter_error") from exc
            if (
                isinstance(trailing, tuple)
                and len(trailing) == 2
                and trailing[0] == "effect"
            ):
                raise WorkerAdapterFailure("protocol_error")
            raise WorkerAdapterFailure("adapter_error")

    @staticmethod
    def _effect_event_snapshot(event: object) -> dict[str, object]:
        def detach(value: object, depth: int = 0) -> object:
            if depth > 16:
                raise ProtocolRefusal(
                    "effect_pipe_event_invalid", "worker effect event is too deep"
                )
            if value is None or type(value) in {str, int, bool}:
                return value
            if type(value) is list:
                if len(value) > 256:
                    raise ProtocolRefusal(
                        "effect_pipe_event_invalid", "worker effect event list is too large"
                    )
                return [detach(item, depth + 1) for item in value]
            if type(value) is dict:
                if len(value) > 64:
                    raise ProtocolRefusal(
                        "effect_pipe_event_invalid", "worker effect event object is too large"
                    )
                detached: dict[str, object] = {}
                for key, item in value.items():
                    if type(key) is not str:
                        raise ProtocolRefusal(
                            "effect_pipe_event_invalid",
                            "worker effect event keys must be exact text",
                        )
                    detached[key] = detach(item, depth + 1)
                return detached
            raise ProtocolRefusal(
                "effect_pipe_event_invalid",
                "worker effect event must contain exact serializable primitives",
            )

        snapshot = detach(event)
        if type(snapshot) is not dict:
            raise ProtocolRefusal(
                "effect_pipe_event_invalid", "worker effect event must be one exact object"
            )
        verb = snapshot.get("verb")
        fields = _EFFECT_EVENT_FIELDS.get(verb)
        if fields is None or set(snapshot) != fields:
            raise ProtocolRefusal(
                "effect_pipe_event_invalid",
                "worker effect event verb and fields must match exactly",
            )
        return snapshot

    @staticmethod
    def _apply_effect_event(
        application: tuple[object, ...],
        event: dict[str, object],
        observed_at: Optional[datetime],
    ) -> dict[str, object]:
        (
            controller, context, intent, dispatched, acknowledged, failed,
            unknown, resolve_operation, _uncertain_operations,
            require_pipe_receive, application_identity, application_authorizer,
        ) = application
        require_pipe_receive(
            controller, application_identity, context,
            application_authorizer, event,
        )
        verb = str(event["verb"])
        if verb == "intent":
            return intent(
                controller,
                run_id=context["run_id"], item_id=context["item_id"],
                attempt_id=context["attempt_id"],
                fence_token=context["fence_token"],
                effect_type=event["effect_type"], target=event["target"],
                request_digest=event["request_digest"],
                idempotency_key=event["idempotency_key"],
                expected_confirmation=event["expected_confirmation"],
                reconciliation_adapter=event["reconciliation_adapter"],
                risk_class=event["risk_class"],
                budget_claim=event["budget_claim"],
                requested_by=event["requested_by"],
                approval_request_id=event["approval_request_id"],
                approval_decision_id=event["approval_decision_id"],
                approval_consumption_id=event["approval_consumption_id"],
                now=observed_at,
            )
        operation = resolve_operation(
            controller, context, event["idempotency_key"],
        )
        operation_id = operation["operation_id"]
        if verb == "dispatch":
            return dispatched(
                controller, operation_id,
                dispatch_adapter=event["dispatch_adapter"],
                dispatch_evidence_digest=event["dispatch_evidence_digest"],
                now=observed_at,
            )
        if verb == "acknowledgement":
            return acknowledged(
                controller, operation_id,
                acknowledgement_digest=event["acknowledgement_digest"],
                now=observed_at,
            )
        outcome = failed if verb == "failure" else unknown
        return outcome(
            controller, operation_id,
            reason_code=event["reason_code"],
            evidence_digest=event["evidence_digest"],
            spend_status=event["spend_status"],
            measured_spend=event["measured_spend"],
            now=observed_at,
        )

    @staticmethod
    def _record_worker_effect_uncertainty(
        application: tuple[object, ...], *, now: datetime,
    ) -> None:
        controller, context = application[:2]
        unknown = application[6]
        resolve_operation = application[7]
        uncertain_operations = application[8]
        for operation in uncertain_operations(controller, context):
            evidence_digest = hashlib.sha256(json.dumps(
                {
                    "domain": "slipway-worker-effect-process-loss-v1",
                    "operation_id": operation["operation_id"],
                    "run_id": context["run_id"],
                    "attempt_id": context["attempt_id"],
                    "reason_code": "process_lost",
                },
                sort_keys=True, separators=(",", ":"), ensure_ascii=False,
            ).encode("utf-8")).hexdigest()
            try:
                unknown(
                    controller, operation["operation_id"],
                    reason_code="process_lost", evidence_digest=evidence_digest,
                    spend_status="unknown", measured_spend=None, now=now,
                )
            except ProtocolRefusal as exc:
                if exc.code != "effect_transition_invalid":
                    raise
                current = resolve_operation(
                    controller, context, operation["idempotency_key"],
                )
                if (
                    current["operation_id"] != operation["operation_id"]
                    or current["primary_outcome_id"] is None
                ):
                    raise

    @staticmethod
    def _descendant_snapshot(event: object) -> tuple[str, str, Optional[str]]:
        if not isinstance(event, dict):
            raise WorkerAdapterFailure("adapter_error")
        allowed = {"provider_descendant_id", "state", "adopted_item_id"}
        if set(event) - allowed:
            raise WorkerAdapterFailure("adapter_error")
        provider_descendant_id = event.get("provider_descendant_id")
        state = event.get("state")
        adopted_item_id = event.get("adopted_item_id")
        if (
            not isinstance(provider_descendant_id, str)
            or not isinstance(state, str)
            or (adopted_item_id is not None and not isinstance(adopted_item_id, str))
        ):
            raise WorkerAdapterFailure("adapter_error")
        return provider_descendant_id, state, adopted_item_id

    def _governed_spawn_context(
        self, adapter_name: str, *, adapter: object,
        adapter_spec: Optional[BuiltInAdapterSpec] = None,
        run_id: Optional[str], item_id: Optional[str],
        attempt_id: Optional[str], claimed_work_item_id: str,
    ) -> Optional[Dict[str, object]]:
        coordinates = (run_id, item_id, attempt_id)
        if all(value is None for value in coordinates):
            return None
        if any(value is None for value in coordinates):
            raise ProtocolRefusal("spawn_launch_coordinates_invalid", "governed launch requires all run coordinates")
        if adapter_spec is None:
            if not callable(getattr(adapter, "set_spawn_context", None)):
                raise ProtocolRefusal("spawn_context_hook_missing", "governed launch adapter lacks the spawn-context hook")
            if getattr(adapter, "name", None) != adapter_name:
                raise ProtocolRefusal(
                    "spawn_adapter_identity_invalid",
                    "governed launch requires the actual adapter object's durable name",
                )
        elif adapter_spec.kind != adapter_name:
            raise ProtocolRefusal(
                "spawn_adapter_identity_invalid",
                "governed launch requires one matching built-in adapter spec",
            )
        if item_id != claimed_work_item_id:
            raise ProtocolRefusal("spawn_launch_item_mismatch", "governed run item must equal the claimed work item")
        from .runtruth import RunLedger

        run = RunLedger(self.root).project().run(str(run_id))
        state = run["attempts"].get(str(attempt_id))
        dispatch = run["dispatches"].get(str(attempt_id))
        policy = run["attempt_spawn_policy"].get(str(attempt_id))
        if (
            state is None or dispatch is None or policy is None
            or state["opened"]["item_id"] != item_id
            or state["started"] is None or state["terminal"] is not None
            or dispatch.get("adapter") != adapter_name
            or policy.get("adapter") != adapter_name
            or dispatch.get("attempt_spawn_policy_id") != policy.get("id")
        ):
            raise ProtocolRefusal("spawn_launch_binding_invalid", "governed launch must repeat current adapter and attempt policy")
        if policy["subagents_mode"] in {"observed_only", "managed"} and self.spawn_controller is None:
            raise ProtocolRefusal("spawn_observation_controller_missing", "observed launch requires the parent-owned controller before fork")
        return {
            "schema_version": 1,
            "run_id": run_id, "item_id": item_id, "attempt_id": attempt_id,
            "fence_token": state["opened"]["fence_token"],
            "attempt_spawn_policy_id": policy["id"],
            "adapter": adapter_name, "subagents_mode": policy["subagents_mode"],
        }

    def _governed_effect_context(
        self, adapter_name: str, *, adapter: object,
        adapter_spec: Optional[BuiltInAdapterSpec] = None,
        run_id: Optional[str], item_id: Optional[str],
        attempt_id: Optional[str], claimed_work_item_id: str,
    ) -> Optional[Dict[str, object]]:
        if self.effect_controller is None:
            return None
        coordinates = (run_id, item_id, attempt_id)
        if any(value is None for value in coordinates):
            raise ProtocolRefusal(
                "effect_launch_coordinates_invalid",
                "effect-enabled launch requires all durable run coordinates",
            )
        if type(self.effect_controller) is not _EFFECT_CONTROLLER_TYPE:
            raise ProtocolRefusal(
                "effect_controller_invalid",
                "worker effect reporting requires the exact EffectController",
            )
        if self.effect_controller.ledger.root.tenant_home != self.root.tenant_home:
            raise ProtocolRefusal(
                "effect_root_mismatch",
                "worker and effect controller must share one tenant root",
            )
        if adapter_spec is None or adapter_spec.kind != adapter_name:
            raise ProtocolRefusal(
                "effect_adapter_identity_invalid",
                "effect-enabled launch requires one matching built-in adapter spec",
            )
        if item_id != claimed_work_item_id:
            raise ProtocolRefusal(
                "effect_launch_item_mismatch",
                "effect-enabled launch item must equal the claimed work item",
            )
        projection = self.effect_controller.run_ledger.project()
        run = projection.run(str(run_id))
        state = run["attempts"].get(str(attempt_id))
        dispatch = run["dispatches"].get(str(attempt_id))
        if (
            state is None
            or state["opened"].get("item_id") != item_id
            or dispatch is None
            or dispatch.get("adapter") != adapter_name
        ):
            raise ProtocolRefusal(
                "effect_launch_binding_invalid",
                "effect-enabled launch must repeat the current durable dispatch",
            )
        context = {
            "run_id": str(run_id), "item_id": str(item_id),
            "attempt_id": str(attempt_id),
            "fence_token": state["opened"]["fence_token"],
        }
        projection.effect_intent_context(
            context["run_id"], context["item_id"], context["attempt_id"],
            context["fence_token"],
        )
        if len(_EFFECT_WORKER_OPERATIONS) != 8:
            raise ProtocolRefusal(
                "effect_application_binding_invalid",
                "worker effect application requires captured controller operations",
            )
        return context

    def _begin_spawn_launch(
        self, run_id: str, attempt_id: str, adapter_name: str,
    ) -> object:
        raise ProtocolRefusal(
            "spawn_launch_capability_invalid",
            "free-standing launch minting cannot prove a live worker process and pipe",
        )

    def _end_spawn_launch(self, capability: object) -> None:
        raise ProtocolRefusal(
            "spawn_launch_capability_invalid",
            "launch authority is closure-local to WorkerRunner.run",
        )

    def _close_spawn_observation(
        self, run_id: str, attempt_id: str, *, now: Optional[datetime] = None,
    ) -> Dict[str, object]:
        raise ProtocolRefusal(
            "spawn_launch_capability_required",
            "observation close requires the closure-local WorkerRunner launch",
        )

    def _handle_spawn_event(
        self, run_id: object, attempt_id: object, event: object,
    ) -> None:
        if self.spawn_controller is not None:
            try:
                closed = self.spawn_controller.ledger.project().run(str(run_id))[
                    "descendant_observation_close"
                ].get(str(attempt_id))
            except (IntegrityFailure, KeyError, ProtocolRefusal):
                closed = None
            if closed is not None:
                raise ProtocolRefusal(
                    "descendant_observation_closed",
                    "descendant testimony cannot follow observation closure",
                )
        raise ProtocolRefusal(
            "spawn_launch_capability_required",
            "spawn events require the closure-local WorkerRunner launch",
        )

    @staticmethod
    def _terminate_process_group(process_group: int) -> None:
        try:
            os.killpg(process_group, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            return
        deadline = time.monotonic() + 0.5
        while time.monotonic() < deadline:
            try:
                os.killpg(process_group, 0)
            except ProcessLookupError:
                return
            except PermissionError:
                break
            time.sleep(0.01)
        try:
            os.killpg(process_group, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass

    @staticmethod
    def _process_group_alive(process_group: int) -> bool:
        try:
            os.killpg(process_group, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True

    def _active_authority(self, node_id: str, now: datetime) -> Dict[str, object]:
        directory = self.root.resolve_relative("authority-grants")
        active: list[Dict[str, object]] = []
        if directory.is_dir():
            for path in sorted(directory.glob("*.jsonl")):
                records = read_records_snapshot(
                    self.root,
                    path.relative_to(self.root.tenant_home),
                    allowed_kinds={"authority_grant"},
                )
                if not records:
                    continue
                grant = records[-1]
                if (
                    grant["state"] == "active"
                    and grant["holder"] == node_id
                    and now < _parse_time(grant["expires_at"])
                ):
                    active.append(dict(grant))
        if not active:
            raise ProtocolRefusal("worker_authority_missing", "worker requires one exact active authority grant")
        if len(active) != 1:
            raise ProtocolRefusal("worker_authority_ambiguous", "worker requires exactly one active authority grant")
        return active[0]

    def _authority_status(
        self, grant: Dict[str, object], node_id: str, now: datetime
    ) -> str:
        subject = str(grant["subject_id"])
        records = read_records_snapshot(
            self.root,
            Path("authority-grants") / f"{subject}.jsonl",
            allowed_kinds={"authority_grant"},
        )
        if not records:
            return "changed"
        latest = records[-1]
        same_grant = (
            latest["holder"] == node_id and latest["epoch"] == grant["epoch"]
        )
        if same_grant and (
            latest["state"] == "expired"
            or (
                latest["state"] == "active"
                and now >= _parse_time(latest["expires_at"])
            )
        ):
            return "expired"
        if (
            same_grant
            and latest["state"] == "active"
            and latest["expires_at"] == grant["expires_at"]
            and now < _parse_time(latest["expires_at"])
        ):
            return "current"
        return "changed"

    def _authority_is_current(
        self, grant: Dict[str, object], node_id: str, now: datetime
    ) -> bool:
        return self._authority_status(grant, node_id, now) == "current"

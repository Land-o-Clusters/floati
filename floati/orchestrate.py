"""Bounded dependency-aware orchestration over the canonical work ledger."""

from __future__ import annotations

import json
import multiprocessing
import os
import queue
import signal
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, Mapping, Optional, Sequence, Tuple

from .errors import IntegrityFailure, ProtocolRefusal
from .registry import Registry
from .root import FloatiRoot, validate_identifier
from .work import WorkLog
from .workers import WorkerAdapter, WorkerReceipts, WorkerRunner


ORCHESTRATE_DRAINED = 0
ORCHESTRATE_DEADLINE = 34
ORCHESTRATE_DEGRADED = 35
MAX_PLAN_BYTES = 65536
MAX_WORKERS = 16
MAX_ITEMS = 128


def _reject_duplicate_keys(pairs: Sequence[Tuple[str, object]]) -> Dict[str, object]:
    result: Dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ProtocolRefusal(
                "orchestrate_plan_invalid", "plan objects must not repeat keys"
            )
        result[key] = value
    return result


@dataclass(frozen=True)
class PlanItem:
    key: str
    title: str
    owner: str
    needs: Tuple[str, ...]


@dataclass(frozen=True)
class DrillAction:
    mode: str
    node: str

    def __post_init__(self) -> None:
        if self.mode not in {"kill_worker", "expire_authority", "hang_child"}:
            raise ProtocolRefusal(
                "orchestrate_drill_invalid", "drill mode is not a version-zero action"
            )
        validate_identifier(self.node, "drill_node")


@dataclass(frozen=True)
class OrchestrationPlan:
    workers: Tuple[str, ...]
    items: Tuple[PlanItem, ...]

    @classmethod
    def load(cls, path: Path) -> "OrchestrationPlan":
        candidate = Path(path)
        if not candidate.is_absolute():
            raise ProtocolRefusal(
                "orchestrate_plan_path_invalid", "plan path must be absolute"
            )
        try:
            if candidate.stat().st_size > MAX_PLAN_BYTES:
                raise ProtocolRefusal(
                    "orchestrate_plan_too_large",
                    f"plan exceeds {MAX_PLAN_BYTES} bytes",
                )
            raw = json.loads(
                candidate.read_text(encoding="utf-8"),
                object_pairs_hook=_reject_duplicate_keys,
            )
        except ProtocolRefusal:
            raise
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ProtocolRefusal(
                "orchestrate_plan_invalid", "plan must be one readable JSON object"
            ) from exc
        if not isinstance(raw, dict) or set(raw) != {
            "schema_version",
            "workers",
            "items",
        }:
            raise ProtocolRefusal(
                "orchestrate_plan_invalid", "plan fields do not match version zero"
            )
        if raw["schema_version"] != 0 or isinstance(raw["schema_version"], bool):
            raise ProtocolRefusal(
                "orchestrate_plan_invalid", "plan schema_version must be integer zero"
            )
        workers_value = raw["workers"]
        if (
            not isinstance(workers_value, list)
            or not 3 <= len(workers_value) <= MAX_WORKERS
        ):
            raise ProtocolRefusal(
                "orchestrate_worker_count_invalid",
                f"plan requires 3 through {MAX_WORKERS} workers",
            )
        try:
            workers = tuple(validate_identifier(value, "worker") for value in workers_value)
        except ProtocolRefusal as exc:
            raise ProtocolRefusal(
                "orchestrate_workers_invalid", "each worker must be a bounded node id"
            ) from exc
        if len(set(workers)) != len(workers):
            raise ProtocolRefusal(
                "orchestrate_workers_invalid", "workers must be unique"
            )
        items_value = raw["items"]
        if (
            not isinstance(items_value, list)
            or not len(workers) < len(items_value) <= MAX_ITEMS
        ):
            raise ProtocolRefusal(
                "orchestrate_item_count_invalid",
                f"plan requires more items than workers and at most {MAX_ITEMS} items",
            )
        items = []
        seen_keys = set()
        owned = set()
        for value in items_value:
            if not isinstance(value, dict) or set(value) != {
                "key",
                "title",
                "owner",
                "needs",
            }:
                raise ProtocolRefusal(
                    "orchestrate_item_invalid", "each plan item has exact version-zero fields"
                )
            try:
                key = validate_identifier(value["key"], "item_key")
                owner = validate_identifier(value["owner"], "owner")
            except ProtocolRefusal as exc:
                raise ProtocolRefusal(
                    "orchestrate_item_invalid", "item key and owner must be bounded identifiers"
                ) from exc
            title = value["title"]
            needs_value = value["needs"]
            if (
                key in seen_keys
                or owner not in workers
                or not isinstance(title, str)
                or not 1 <= len(title) <= 256
                or not isinstance(needs_value, list)
                or len(needs_value) > 64
                or any(not isinstance(dependency, str) for dependency in needs_value)
                or len(set(needs_value)) != len(needs_value)
            ):
                raise ProtocolRefusal(
                    "orchestrate_item_invalid", "plan item fields violate their bounds"
                )
            if any(dependency not in seen_keys for dependency in needs_value):
                raise ProtocolRefusal(
                    "orchestrate_dependency_invalid",
                    "dependencies must name unique earlier item keys",
                )
            seen_keys.add(key)
            owned.add(owner)
            items.append(PlanItem(key, title, owner, tuple(needs_value)))
        if owned != set(workers):
            raise ProtocolRefusal(
                "orchestrate_workers_invalid", "every worker must own at least one item"
            )
        return cls(workers, tuple(items))


def append_admitted_run(
    ledger: object,
    plan: object,
    policy: object,
    artifact: object,
    *,
    run_id: str,
    timestamp: str,
) -> Tuple[Dict[str, object], Dict[str, object]]:
    """Append the existing initial run frames behind one current in-process gate.

    This is intentionally an internal invocation seam, not a durable admission
    receipt or a replacement for the still-unruled universal ledger boundary.
    Legacy ``floati orchestrate`` plans do not call it.
    """

    from .admission import AdmissionArtifact, AdmissionEvaluator, AdmissionPlan
    from .ids import uuid7_hex
    from .policy import RepositoryPolicy
    from .runtruth import RunLedger

    if not isinstance(ledger, RunLedger):
        raise ProtocolRefusal("run_ledger_required", "admitted run creation requires the canonical RunLedger")
    if not isinstance(plan, AdmissionPlan) or not isinstance(policy, RepositoryPolicy):
        raise ProtocolRefusal("admission_input_required", "admitted run creation requires a loaded plan and policy")
    if not isinstance(artifact, AdmissionArtifact):
        raise ProtocolRefusal("admission_artifact_required", "admitted run creation requires an AdmissionArtifact")

    # This must remain immediately before the first run_created append.  It
    # re-evaluates the immutable pair and rejects stale, forged, or non-admitted
    # artifacts without manufacturing a capability or durable record family.
    AdmissionEvaluator.require_current_admission(plan, policy, artifact)
    created = ledger.append(
        {
            "schema_version": 0,
            "id": "run-created-" + uuid7_hex(),
            "tenant_id": ledger.root.tenant_id,
            "timestamp": timestamp,
            "kind": "run_created",
            "run_id": run_id,
            "plan_digest": artifact.plan_digest,
            "policy_digest": artifact.policy_digest,
            "item_ids": [item.item_id for item in plan.items],
            "dependency_edges": [edge.canonical() for edge in plan.dependency_edges],
        }
    )
    bound = ledger.append(
        {
            "schema_version": 0,
            "id": "run-policy-bound-" + uuid7_hex(),
            "tenant_id": ledger.root.tenant_id,
            "timestamp": timestamp,
            "kind": "run_policy_bound",
            "run_id": run_id,
            "policy_digest": artifact.policy_digest,
        }
    )
    return created, bound


def _controller(
    root: FloatiRoot,
    adapters: Mapping[str, WorkerAdapter],
    adapter_name: str,
    node_id: str,
    start: object,
    call_timeout: float,
    poll_interval: float,
    audit_queue: object,
    drive_reached: Optional[object],
    drive_release: Optional[object],
) -> None:
    def terminate(_signum: int, _frame: object) -> None:
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, terminate)
    start.wait()
    try:
        while True:
            owned = [item for item in WorkLog(root).show() if item["owner"] == node_id]
            if any(item["readiness"] == "ready" for item in owned):
                runner = WorkerRunner(root, adapters, call_timeout=call_timeout)
                result: Optional[Dict[str, object]] = None
                try:
                    on_drive = None
                    if drive_reached is not None and drive_release is not None:
                        def wait_for_drill() -> None:
                            drive_reached.set()
                            drive_release.wait()

                        on_drive = wait_for_drill
                    result = runner.run(
                        node_id, adapter_name, on_drive=on_drive
                    )
                finally:
                    audit_queue.put(
                        {
                            "node_id": node_id,
                            "work_item_id": (
                                None if result is None else result.get("work_item_id")
                            ),
                            **runner.last_process_audit,
                        }
                    )
                if result["transition"] == "degrade":
                    return
                continue
            if any(item["readiness"] == "blocked" for item in owned):
                time.sleep(poll_interval)
                continue
            return
    except KeyboardInterrupt:
        return


class FleetOrchestrator:
    def __init__(
        self,
        root: FloatiRoot,
        adapters: Mapping[str, WorkerAdapter],
        *,
        adapter_name: str,
        redraw_interval: float = 0.25,
        worker_timeout: float = 60.0,
    ) -> None:
        if adapter_name not in adapters:
            raise ProtocolRefusal(
                "worker_adapter_absent", "requested worker adapter is unavailable"
            )
        if (
            not isinstance(redraw_interval, (int, float))
            or isinstance(redraw_interval, bool)
            or not 0.01 <= float(redraw_interval) <= 0.25
        ):
            raise ProtocolRefusal(
                "orchestrate_interval_invalid",
                "orchestration redraw interval must be 0.01 through 0.25 seconds",
            )
        self.root = root
        self.adapters = dict(adapters)
        self.adapter_name = adapter_name
        self.redraw_interval = float(redraw_interval)
        if (
            not isinstance(worker_timeout, (int, float))
            or isinstance(worker_timeout, bool)
            or not 0.01 <= float(worker_timeout) <= 60
        ):
            raise ProtocolRefusal(
                "worker_timeout_invalid", "worker timeout must be 0.01 through 60 seconds"
            )
        self.worker_timeout = float(worker_timeout)

    def run(
        self,
        plan: OrchestrationPlan,
        deadline_seconds: float,
        *,
        on_frame: Optional[Callable[[Dict[str, object]], None]] = None,
        drills: Sequence[DrillAction] = (),
    ) -> Dict[str, object]:
        if (
            not isinstance(deadline_seconds, (int, float))
            or isinstance(deadline_seconds, bool)
            or not 0.1 <= float(deadline_seconds) <= 3600
        ):
            raise ProtocolRefusal(
                "orchestrate_deadline_invalid",
                "orchestration deadline must be 0.1 through 3600 seconds",
            )
        work = WorkLog(self.root)
        if work.show():
            raise ProtocolRefusal(
                "orchestrate_root_not_empty",
                "orchestration v0 requires an empty work ledger",
            )
        self._preflight(plan)
        if any(action.node not in plan.workers for action in drills):
            raise ProtocolRefusal(
                "orchestrate_drill_invalid", "each drill node must belong to the plan"
            )
        if len({(action.mode, action.node) for action in drills}) != len(drills):
            raise ProtocolRefusal(
                "orchestrate_drill_invalid", "drill actions must be unique"
            )
        if len({action.node for action in drills}) != len(drills):
            raise ProtocolRefusal(
                "orchestrate_drill_invalid",
                "orchestration v0 permits at most one drill per node",
            )
        seeded: Dict[str, str] = {}
        for item in plan.items:
            record = work.add(
                item.title,
                item.owner,
                [],
                needs=[seeded[dependency] for dependency in item.needs],
                provision_workspace=True,
            )
            seeded[item.key] = str(record["id"])

        context = multiprocessing.get_context("fork")
        start = context.Event()
        audit_queue = context.Queue()
        drive_gates = {
            action.node: (context.Event(), context.Event()) for action in drills
        }
        hang_actions = [action for action in drills if action.mode == "hang_child"]
        if len(hang_actions) > 1:
            raise ProtocolRefusal(
                "orchestrate_drill_unsupported",
                "orchestration v0 supports one causative child-hang drill",
            )
        hang_events: Dict[str, object] = {}
        for action in hang_actions:
            setter = getattr(self.adapters[self.adapter_name], "set_hang_event", None)
            if not callable(setter):
                raise ProtocolRefusal(
                    "orchestrate_drill_unsupported",
                    "selected adapter cannot enact a child-hang drill",
                )
            event = context.Event()
            setter(action.node, event)
            hang_events[action.node] = event
        processes = [
            context.Process(
                target=_controller,
                args=(
                    self.root,
                    self.adapters,
                    self.adapter_name,
                    node,
                    start,
                    self.worker_timeout,
                    min(0.05, self.redraw_interval),
                    audit_queue,
                    drive_gates.get(node, (None, None))[0],
                    drive_gates.get(node, (None, None))[1],
                ),
                name=f"floati-orchestrator-{node}",
            )
            for node in plan.workers
        ]
        started = time.monotonic()
        deadline = started + float(deadline_seconds)
        for process in processes:
            process.start()
        controller_pids = [int(process.pid) for process in processes if process.pid]
        state = "degraded"
        triggered = [False for _action in drills]
        start.set()
        self._emit_frame(on_frame)
        try:
            while True:
                now = time.monotonic()
                if now >= deadline:
                    state = "deadline"
                    break
                if not any(process.is_alive() for process in processes):
                    state = "degraded"
                    break
                time.sleep(min(self.redraw_interval, max(0.001, deadline - now)))
                self._emit_frame(on_frame)
                self._apply_drills(
                    drills,
                    triggered,
                    processes,
                    plan.workers,
                    hang_events,
                    drive_gates,
                )
                sessions = self._stable_sessions()
                latest_by_node = {
                    str(row["node_id"]): row for row in sessions
                }
                drills_terminal = bool(drills) and all(triggered) and all(
                    latest_by_node.get(action.node, {}).get("state") == "degraded"
                    for action in drills
                )
                if drills_terminal or (
                    not drills
                    and any(row["state"] == "degraded" for row in sessions)
                ):
                    state = "degraded"
                    break
        finally:
            process_by_node = dict(zip(plan.workers, processes))
            if state == "degraded":
                grace_nodes = {
                    str(row["node_id"])
                    for row in self._stable_sessions()
                    if row["state"] == "degraded"
                }
            elif state == "deadline":
                grace_nodes = set()
            else:
                grace_nodes = set(plan.workers)
            cleanup_deadline = time.monotonic() + (
                min(2.0, self.worker_timeout + 1.0) if grace_nodes else 0.0
            )
            for node in grace_nodes:
                process = process_by_node[node]
                process.join(max(0.0, cleanup_deadline - time.monotonic()))
            for process in processes:
                if process.is_alive():
                    process.terminate()
            for process in processes:
                process.join(1)
            for process in processes:
                if process.is_alive():
                    process.kill()
                    process.join(1)
        process_audits: list[Dict[str, object]] = []
        while True:
            try:
                process_audits.append(audit_queue.get(timeout=0.05))
            except queue.Empty:
                break
        audit_queue.close()
        audit_queue.join_thread()
        self._emit_frame(on_frame)
        controller_alive = [
            int(process.pid)
            for process in processes
            if process.pid is not None and process.is_alive()
        ]
        audit_alive = [
            int(pid)
            for audit in process_audits
            for pid in audit["alive_after_cleanup"]
        ]
        alive_after = sorted(set([*controller_alive, *audit_alive]))
        records = WorkerReceipts(self.root).records()
        chains = {
            str(session["session_id"]): [
                str(row["id"])
                for row in records
                if row["session_id"] == session["session_id"]
            ]
            for session in WorkerReceipts(self.root).sessions()
        }
        final_sessions = WorkerReceipts(self.root).sessions()
        final_work = work.show()
        controller_exit_codes = [process.exitcode for process in processes]
        if state != "deadline":
            state = (
                "drained"
                if self._drained(
                    seeded,
                    final_work,
                    final_sessions,
                    controller_exit_codes,
                    process_audits,
                    alive_after,
                    triggered,
                )
                else "degraded"
            )
        drill_rows = []
        for index, action in enumerate(drills):
            session = next(
                (
                    row
                    for row in reversed(final_sessions)
                    if row["node_id"] == action.node
                ),
                None,
            )
            drill_rows.append(
                {
                    "mode": action.mode,
                    "node": action.node,
                    "triggered": triggered[index],
                    "outcome": None if session is None else session["outcome_code"],
                }
            )
        return {
            "state": state,
            "return_code": {
                "drained": ORCHESTRATE_DRAINED,
                "deadline": ORCHESTRATE_DEADLINE,
                "degraded": ORCHESTRATE_DEGRADED,
            }[state],
            "worker_count": len(plan.workers),
            "item_count": len(plan.items),
            "seeded_items": seeded,
            "controller_pids": controller_pids,
            "controller_exit_codes": controller_exit_codes,
            "alive_after_cleanup": alive_after,
            "process_audits": process_audits,
            "final_work": final_work,
            "terminal_sessions": final_sessions,
            "receipt_chains": chains,
            "drills": drill_rows,
            "elapsed_seconds": round(time.monotonic() - started, 6),
        }

    def _preflight(self, plan: OrchestrationPlan) -> None:
        runner = WorkerRunner(self.root, self.adapters)
        current = datetime.now(timezone.utc)
        for node in plan.workers:
            Registry(self.root).require_active(node)
            runner._active_authority(node, current)

    def _emit_frame(
        self, callback: Optional[Callable[[Dict[str, object]], None]]
    ) -> None:
        if callback is None:
            return
        for _ in range(3):
            try:
                callback(
                    {
                        "work": WorkLog(self.root).show(),
                        "workers": WorkerReceipts(self.root).sessions(),
                        "receipts": WorkerReceipts(self.root).records(),
                    }
                )
                return
            except IntegrityFailure:
                time.sleep(0.002)
        raise IntegrityFailure(
            "orchestration_projection_unavailable",
            "could not read a stable orchestration frame",
        )

    def _apply_drills(
        self,
        drills: Sequence[DrillAction],
        triggered: list[bool],
        processes: Sequence[multiprocessing.Process],
        workers: Sequence[str],
        hang_events: Mapping[str, object],
        drive_gates: Mapping[str, Tuple[object, object]],
    ) -> None:
        sessions = self._stable_sessions()
        process_by_node = dict(zip(workers, processes))
        for index, action in enumerate(drills):
            if triggered[index]:
                continue
            drive_reached, drive_release = drive_gates[action.node]
            if not drive_reached.is_set():
                continue
            session = next(
                (
                    row
                    for row in reversed(sessions)
                    if row["node_id"] == action.node and row["transition"] == "drive"
                ),
                None,
            )
            if session is None:
                continue
            if action.mode == "kill_worker":
                process = process_by_node[action.node]
                if process.is_alive():
                    process.terminate()
            elif action.mode == "expire_authority":
                from .planes import AuthorityGrantStore

                AuthorityGrantStore(self.root).expire(
                    str(session["authority_subject"]),
                    action.node,
                    int(session["authority_epoch"]),
                    datetime.now(timezone.utc),
                )
            elif action.mode == "hang_child":
                hang_events[action.node].set()
            if action.mode != "kill_worker":
                drive_release.set()
            triggered[index] = True

    @staticmethod
    def _drained(
        seeded: Mapping[str, str],
        final_work: Sequence[Mapping[str, object]],
        final_sessions: Sequence[Mapping[str, object]],
        controller_exit_codes: Sequence[Optional[int]],
        process_audits: Sequence[Mapping[str, object]],
        alive_after_cleanup: Sequence[int],
        drills_triggered: Sequence[bool] = (),
    ) -> bool:
        expected = set(seeded.values())
        completed_work = {
            str(item["id"])
            for item in final_work
            if item.get("readiness") == "done"
        }
        terminal = {
            str(session["work_item_id"])
            for session in final_sessions
            if session.get("state") == "complete"
            and session.get("transition") == "complete"
            and session.get("outcome_code") is None
        }
        clean_audits = {
            str(audit["work_item_id"])
            for audit in process_audits
            if audit.get("work_item_id") is not None
            and not audit.get("alive_after_cleanup")
        }
        return (
            completed_work == expected
            and terminal == expected
            and len(final_sessions) == len(expected)
            and all(code == 0 for code in controller_exit_codes)
            and len(process_audits) == len(expected)
            and clean_audits == expected
            and not alive_after_cleanup
            and all(drills_triggered)
        )

    def _stable_sessions(self) -> list[Dict[str, object]]:
        for _ in range(3):
            try:
                return WorkerReceipts(self.root).sessions()
            except IntegrityFailure:
                time.sleep(0.002)
        raise IntegrityFailure(
            "orchestration_projection_unavailable",
            "could not read stable worker sessions",
        )

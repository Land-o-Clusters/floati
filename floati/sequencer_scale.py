"""Deterministic hostile-scale fixture for the optional local sequencer."""

from __future__ import annotations

import hashlib
import json
import resource
import socket
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Sequence, Tuple

from .admission import AdmissionEvaluator, AdmissionPlan
from .contracts import PlanAmendment, TaskContract, contract_digest
from .errors import IntegrityFailure, ProtocolRefusal
from .policy import RepositoryPolicy
from .records import run_admission_digest
from .root import FloatiRoot
from .run_segments import SegmentConfig
from .runtruth import RunLedger, RunProjection
from .sequencer import SequencerClient, SequencerConfig, SequencerService


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 8, 9, 12, 0, tzinfo=timezone.utc)
NOW_TEXT = "2026-08-09T12:00:00.000Z"


@dataclass(frozen=True)
class ScaleConfig:
    max_records: int
    batch_size: int
    client_count: int
    item_count: int
    lifecycle_record_count: int
    restart_batch_ordinals: Tuple[int, ...]

    def __post_init__(self) -> None:
        values = (
            (self.max_records, 1, 100_000, "max_records"),
            (self.batch_size, 1, 64, "batch_size"),
            (self.client_count, 1, 100, "client_count"),
            (self.item_count, 1, 10_000, "item_count"),
            (self.lifecycle_record_count, 1, 1_000_000, "lifecycle_record_count"),
        )
        for value, lower, upper, field in values:
            if (
                not isinstance(value, int)
                or isinstance(value, bool)
                or not lower <= value <= upper
            ):
                raise ProtocolRefusal("scale_config_invalid", field + " is outside bounds")
        total_batches = (self.lifecycle_record_count + self.batch_size - 1) // self.batch_size
        if (
            not isinstance(self.restart_batch_ordinals, tuple)
            or tuple(sorted(set(self.restart_batch_ordinals)))
            != self.restart_batch_ordinals
            or any(
                not isinstance(value, int)
                or isinstance(value, bool)
                or not 1 <= value < total_batches
                for value in self.restart_batch_ordinals
            )
        ):
            raise ProtocolRefusal(
                "scale_config_invalid", "restart batches must be sorted interior ordinals"
            )


class _IdentitySource:
    def __init__(self) -> None:
        self.value = 1

    def next(self, prefix: str) -> str:
        value = self.value
        self.value += 1
        digits = list(f"{value:032x}")
        digits[12] = "7"
        digits[16] = "8"
        return prefix + "".join(digits)


@dataclass
class _ItemState:
    item_id: str
    task_contract_id: str
    contract: TaskContract


@dataclass
class _RunState:
    run_id: str
    plan: AdmissionPlan
    items: List[_ItemState]


def _contract() -> Dict[str, object]:
    return {
        "objective": "prove deterministic hostile sequencer scale",
        "non_goals": ["no live fleet claim"],
        "areas_to_avoid": [{"path": "bundle/c7.1", "region": "all"}],
        "input_hashes": {"brief": "a" * 64},
        "acceptance_checks": {"tests.unit": "python3 -m unittest"},
        "constraints": {"network": "dark"},
        "risk_class": "low",
        "retry_policy": {
            "max_attempts": 1,
            "backoff": {
                "base_delay_ms": 0,
                "cap_delay_ms": 0,
                "strategy": "fixed",
            },
        },
        "dependencies": [],
    }


def _plan_value(item_ids: Sequence[str]) -> Dict[str, object]:
    return {
        "schema_version": 0,
        "workers": [{"node_id": "scale-worker", "worker_profile": "codex"}],
        "max_active_attempts": 1,
        "budget_reservations": [{"budget_id": "build", "amount": 1}],
        "items": [
            {
                "item_id": item_id,
                "contract": _contract(),
                "capability_selector": "review_write",
                "requires_cancellation": True,
                "requires_callback": True,
                "workspace_key": f"workspace-{ordinal:05d}",
                "concurrency_key": f"concurrency-{ordinal:05d}",
                "retry_class": "transient",
                "effect_safety": "idempotent",
                "merge_gate": None,
            }
            for ordinal, item_id in enumerate(item_ids)
        ],
        "dependency_edges": [],
    }


def _append_batch(
    ledger: RunLedger,
    projection: RunProjection,
    records: Sequence[Dict[str, object]],
) -> None:
    if not records:
        return

    def decide(snapshot):
        if snapshot.total_records != projection._last_position:
            raise IntegrityFailure(
                "scale_projection_stale", "scale projection does not cover exact durable prefix"
            )
        for offset, record in enumerate(records, start=1):
            projection.apply(
                record,
                physical_position=snapshot.total_records + offset,
                integrity=False,
                retain_record=False,
            )
        return list(records), list(records)

    ledger._store.transact_batch(decide)


def _replay(ledger: RunLedger) -> RunProjection:
    projection = RunProjection.empty()
    for position, record in enumerate(ledger._store.iter_records(), start=1):
        projection.apply(
            record,
            physical_position=position,
            integrity=True,
            retain_record=False,
        )
    return projection


def _service(
    root: FloatiRoot,
    config: ScaleConfig,
    ordinal: int,
    *,
    start: bool = True,
) -> Tuple[SequencerService, threading.Event, threading.Thread]:
    service = SequencerService(
        root,
        f"scale-{ordinal:03d}",
        config=SequencerConfig(
            max_clients=min(1024, config.client_count * 3 + 1),
            max_batch=config.batch_size,
            select_timeout=0.01,
        ),
        segment_config=SegmentConfig(max_records=config.max_records),
    )
    stop = threading.Event()
    thread = threading.Thread(target=service.serve_forever, args=(stop,), daemon=True)
    if start:
        thread.start()
    return service, stop, thread


def _stop(
    service: SequencerService, stop: threading.Event, thread: threading.Thread
) -> None:
    stop.set()
    thread.join(5)
    service.close()
    if thread.is_alive():
        raise ProtocolRefusal("scale_service_stop_timeout", "sequencer did not stop")


def _peak_rss_bytes() -> int:
    measured = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    return measured if sys.platform == "darwin" else measured * 1024


def _socket_run_created(
    ids: _IdentitySource, tenant_id: str
) -> Dict[str, object]:
    return {
        "schema_version": 0,
        "id": ids.next("run-created-"),
        "tenant_id": tenant_id,
        "timestamp": NOW_TEXT,
        "kind": "run_created",
        "run_id": ids.next("run-"),
        "plan_digest": "a" * 64,
        "item_ids": [ids.next("work-")],
        "dependency_edges": [],
    }


def _queue_socket_request(
    service: SequencerService,
    client_id: str,
    record: Dict[str, object],
) -> socket.socket:
    channel = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        channel.settimeout(20)
        channel.connect(str(service.socket_path))
        channel.sendall(
            SequencerClient(service.socket_path, service.epoch, client_id).frame(
                record
            )
        )
    except BaseException:
        channel.close()
        raise
    return channel


def _read_socket_response(channel: socket.socket) -> Dict[str, object]:
    data = bytearray()
    try:
        while b"\n" not in data:
            chunk = channel.recv(65536 - len(data))
            if not chunk:
                break
            data.extend(chunk)
    finally:
        channel.close()
    if not data or data.find(b"\n") != len(data) - 1:
        raise ProtocolRefusal(
            "scale_socket_response_invalid",
            "real socket phase requires one complete response frame",
        )
    response = json.loads(bytes(data))
    if not isinstance(response, dict) or response.get("status") != "ok":
        raise ProtocolRefusal(
            "scale_socket_response_invalid",
            "real socket phase request did not commit",
        )
    return response


def _run_real_socket_phase(
    service: SequencerService,
    thread: threading.Thread,
    config: ScaleConfig,
    ids: _IdentitySource,
) -> Dict[str, object]:
    noisy_client = "client-000"
    queued: List[Tuple[str, Dict[str, object], socket.socket]] = []
    # Put the response-loss request and all quiet peers in the initial bounded
    # accept window.  Once service starts, the noisy principal continuously
    # submits the remaining requests while those quiet peers receive turns.
    lost_record = _socket_run_created(ids, service.root.tenant_id)
    lost_channel = _queue_socket_request(service, noisy_client, lost_record)
    lost_channel.close()
    for ordinal in range(1, config.client_count):
        client_id = f"client-{ordinal:03d}"
        record = _socket_run_created(ids, service.root.tenant_id)
        queued.append(
            (client_id, record, _queue_socket_request(service, client_id, record))
        )
    thread.start()
    for _ordinal in range(config.client_count + 1):
        record = _socket_run_created(ids, service.root.tenant_id)
        queued.append(
            (
                noisy_client,
                record,
                _queue_socket_request(service, noisy_client, record),
            )
        )
    observed: List[Tuple[int, str]] = []
    for client_id, _record, channel in queued:
        response = _read_socket_response(channel)
        coordinate = response.get("coordinate")
        if not isinstance(coordinate, dict) or not isinstance(
            coordinate.get("global_ordinal"), int
        ):
            raise ProtocolRefusal(
                "scale_socket_response_invalid",
                "real socket response requires a physical coordinate",
            )
        observed.append((int(coordinate["global_ordinal"]), client_id))

    retry = SequencerClient(
        service.socket_path, service.epoch, noisy_client, timeout=20
    ).append(lost_record)
    retry_coordinate = retry.get("coordinate")
    if retry.get("record") != lost_record or not isinstance(retry_coordinate, dict):
        raise ProtocolRefusal(
            "scale_response_loss_unresolved",
            "real lost response did not resolve to its durable record",
        )
    observed.append((int(retry_coordinate["global_ordinal"]), noisy_client))
    observed.sort()
    positions: Dict[str, List[int]] = {}
    for turn, (_ordinal, client_id) in enumerate(observed, start=1):
        positions.setdefault(client_id, []).append(turn)
    gaps = [
        right - left
        for turns in positions.values()
        for left, right in zip(turns, turns[1:])
    ]
    quiet_first_turns = [
        turns[0] for client_id, turns in positions.items() if client_id != noisy_client
    ]
    return {
        "committed_records": len(observed),
        "max_service_turns": max(gaps + quiet_first_turns, default=0),
        "noisy_client": noisy_client,
        "real_socket_turns": len(observed),
        "response_losses": 1,
        "response_loss_resolutions": 1,
    }


def run_scale_fixture(config: ScaleConfig) -> Dict[str, object]:
    """Run one temporary, policy-valid deterministic scale fixture."""

    if not isinstance(config, ScaleConfig):
        raise ProtocolRefusal("scale_config_invalid", "ScaleConfig is required")
    started = time.perf_counter()
    ids = _IdentitySource()
    timeout_codes: List[str] = []
    unknown_responses = 0
    retry_resolutions = 0
    acknowledged_records = 0
    restart_offsets: List[int] = []
    with tempfile.TemporaryDirectory(prefix="slipway-sequencer-scale-") as directory:
        # Admission deliberately rejects any symlinked lexical parent.  macOS
        # spells its temporary root through /var -> /private/var, so bind the
        # fixture to the canonical path before writing governed plan input.
        base = Path(directory).resolve()
        root = FloatiRoot.open_direct_home(base / "alpha", create=True)
        policy = RepositoryPolicy.load(REPOSITORY_ROOT / "FLOATI.toml")
        segment_config = SegmentConfig(max_records=config.max_records)
        service, stop, thread = _service(root, config, 0, start=False)
        service._ledger._store.activate(now=NOW)
        socket_phase = _run_real_socket_phase(service, thread, config, ids)
        projection = _replay(service._ledger)
        acknowledged_records = int(socket_phase["committed_records"]) - int(
            socket_phase["response_losses"]
        )
        unknown_responses = int(socket_phase["response_losses"])
        retry_resolutions = int(socket_phase["response_loss_resolutions"])
        injected_restart_unknowns = 0
        run_states: List[_RunState] = []
        setup_records: List[Dict[str, object]] = []
        plans = base / "plans"
        plans.mkdir()
        remaining = config.item_count
        global_item = 0
        run_ordinal = 0
        while remaining:
            width = min(64, remaining)
            item_ids = [ids.next("work-") for _ in range(width)]
            plan_path = plans / f"plan-{run_ordinal:04d}.json"
            plan_path.write_text(
                json.dumps(_plan_value(item_ids), sort_keys=True, separators=(",", ":")),
                encoding="utf-8",
            )
            plan = AdmissionPlan.load(plan_path)
            admission = AdmissionEvaluator.evaluate(plan, policy)
            if admission.outcome != "admitted":
                raise ProtocolRefusal(
                    "scale_plan_not_admitted", "scale plan must satisfy unchanged FLOATI.toml"
                )
            run_id = ids.next("run-")
            setup_records.append(
                {
                    "schema_version": 0,
                    "id": ids.next("run-created-"),
                    "tenant_id": root.tenant_id,
                    "timestamp": NOW_TEXT,
                    "kind": "run_created",
                    "run_id": run_id,
                    "plan_digest": plan.digest,
                    "policy_digest": policy.digest,
                    "item_ids": item_ids,
                    "dependency_edges": [],
                }
            )
            item_states: List[_ItemState] = []
            for item in plan.items:
                task_id = ids.next("task-contract-")
                setup_records.append(
                    {
                        "schema_version": 0,
                        "id": task_id,
                        "tenant_id": root.tenant_id,
                        "timestamp": NOW_TEXT,
                        "kind": "task_contract",
                        "run_id": run_id,
                        "item_id": item.item_id,
                        **item.contract.canonical(),
                        "contract_digest": contract_digest(item.contract),
                    }
                )
                item_states.append(_ItemState(item.item_id, task_id, item.contract))
                global_item += 1
            setup_records.extend(
                [
                    {
                        "schema_version": 0,
                        "id": ids.next("run-policy-bound-"),
                        "tenant_id": root.tenant_id,
                        "timestamp": NOW_TEXT,
                        "kind": "run_policy_bound",
                        "run_id": run_id,
                        "policy_digest": policy.digest,
                    },
                    {
                        "schema_version": 0,
                        "id": ids.next("run-worker-pool-bound-"),
                        "tenant_id": root.tenant_id,
                        "timestamp": NOW_TEXT,
                        "kind": "worker_pool_bound",
                        "run_id": run_id,
                        "worker_ids": ["scale-worker"],
                    },
                ]
            )
            run_states.append(_RunState(run_id, plan, item_states))
            remaining -= width
            run_ordinal += 1

        for offset in range(0, len(setup_records), config.batch_size):
            batch = setup_records[offset : offset + config.batch_size]
            _append_batch(service._ledger, projection, batch)
            acknowledged_records += len(batch)

        flat_items = [
            (run_state, item)
            for run_state in run_states
            for item in run_state.items
        ]
        restart_set = set(config.restart_batch_ordinals)
        lifecycle_index = 0
        batch_ordinal = 0
        service_ordinal = 0
        while lifecycle_index < config.lifecycle_record_count:
            batch: List[Dict[str, object]] = []
            while (
                len(batch) < config.batch_size
                and lifecycle_index < config.lifecycle_record_count
            ):
                run_state, item = flat_items[lifecycle_index % len(flat_items)]
                cycle = lifecycle_index // len(flat_items) + 1
                replacement = item.contract.replaced(
                    objective=f"hostile-scale-{cycle:04d}-{lifecycle_index % len(flat_items):05d}"
                )
                amendment = PlanAmendment.between(item.contract, replacement)
                batch.append(
                    {
                        "schema_version": 0,
                        "id": ids.next("plan-amendment-"),
                        "tenant_id": root.tenant_id,
                        "timestamp": NOW_TEXT,
                        "kind": "plan_amendment",
                        "run_id": run_state.run_id,
                        "item_id": item.item_id,
                        "task_contract_id": item.task_contract_id,
                        "previous_digest": amendment.previous_digest,
                        "replacement_fields": dict(amendment.replacement_fields),
                        "contract_digest": amendment.contract_digest,
                    }
                )
                item.contract = replacement
                lifecycle_index += 1
            batch_ordinal += 1
            try:
                _append_batch(service._ledger, projection, batch)
            except ProtocolRefusal as exc:
                timeout_codes.append(exc.code)
                raise
            acknowledged_records += len(batch)
            if batch_ordinal in restart_set:
                unknown_responses += 1
                injected_restart_unknowns += 1
                unknown = batch[-1]
                restart_offsets.append(lifecycle_index)
                _stop(service, stop, thread)
                service_ordinal += 1
                service, stop, thread = _service(root, config, service_ordinal)
                projection = _replay(service._ledger)
                retry = SequencerClient(
                    service.socket_path,
                    service.epoch,
                    f"retry-{service_ordinal:03d}",
                    timeout=10,
                ).append(unknown)
                if retry.get("record") == unknown:
                    retry_resolutions += 1

        admission_records: List[Dict[str, object]] = []
        for state in run_states:
            workers = [worker.canonical() for worker in state.plan.workers]
            reservations = [
                reservation.canonical() for reservation in state.plan.budget_reservations
            ]
            items = [
                {
                    "item_id": item.item_id,
                    "workspace_key": item.workspace_key,
                    "concurrency_key": item.concurrency_key,
                    "capability_selector": item.capability_selector,
                }
                for item in state.plan.items
            ]
            admission_records.append(
                {
                    "schema_version": 1,
                    "id": ids.next("run-admission-bound-"),
                    "tenant_id": root.tenant_id,
                    "timestamp": NOW_TEXT,
                    "kind": "run_admission_bound",
                    "run_id": state.run_id,
                    "plan_digest": state.plan.digest,
                    "policy_digest": policy.digest,
                    "max_active_attempts": state.plan.max_active_attempts,
                    "workers": workers,
                    "budget_reservations": reservations,
                    "items": items,
                    "admission_digest": run_admission_digest(
                        workers, state.plan.max_active_attempts, reservations, items
                    ),
                }
            )
        for offset in range(0, len(admission_records), config.batch_size):
            batch = admission_records[offset : offset + config.batch_size]
            _append_batch(service._ledger, projection, batch)
            acknowledged_records += len(batch)

        sequencer_digest = projection.semantic_digest()
        total_records = projection._last_position
        segment_dir = root.resolve_relative("runs/segments")
        segment_files = sorted(segment_dir.glob("[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9].jsonl"))
        segment_digests = [
            {
                "segment": path.name,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "bytes": path.stat().st_size,
            }
            for path in segment_files
        ]
        validated_count = sum(1 for _record in service._ledger._store.iter_records())
        sealed_digest_failures = 0 if validated_count == total_records else 1
        unique_ids = len(projection._seen_ids)
        _stop(service, stop, thread)
        daemonless = RunLedger(root, segment_config=segment_config)
        daemonless_projection = _replay(daemonless)
        daemonless_digest = daemonless_projection.semantic_digest()
        max_turns = int(socket_phase["max_service_turns"])
        duplicate_record_ids = total_records - unique_ids
        lost_acknowledged = max(0, acknowledged_records - total_records)
        ledger_lock_timeouts = sum(code == "ledger_lock_timeout" for code in timeout_codes)
        passed = (
            duplicate_record_ids == 0
            and lost_acknowledged == 0
            and unknown_responses == retry_resolutions
            and ledger_lock_timeouts == 0
            and max_turns <= config.client_count + 1
            and len(segment_files) >= max(1, total_records // config.max_records)
            and sealed_digest_failures == 0
            and sequencer_digest == daemonless_digest
        )
        return {
            "artifact_version": 1,
            "command": "sequencer-scale-gate",
            "status": "passed" if passed else "failed",
            "config": {
                "max_records": config.max_records,
                "batch_size": config.batch_size,
            },
            "counts": {
                "requested_lifecycle_records": config.lifecycle_record_count,
                "valid_run_records": total_records,
                "admitted_items": config.item_count,
                "client_principals": config.client_count,
            },
            "restart_points": {
                "batch_ordinals": list(config.restart_batch_ordinals),
                "record_offsets": restart_offsets,
            },
            "duplicates": {"record_ids": duplicate_record_ids},
            "acknowledgements": {
                "lost": lost_acknowledged,
                "unknown_responses": unknown_responses,
                "retry_resolutions": retry_resolutions,
                "real_response_losses": socket_phase["response_losses"],
                "real_response_loss_resolutions": socket_phase[
                    "response_loss_resolutions"
                ],
                "injected_restart_unknowns": injected_restart_unknowns,
            },
            "timeouts": {
                "ledger_lock": ledger_lock_timeouts,
                "codes": timeout_codes,
            },
            "fairness": {
                "source": "real_socket_service",
                "noisy_client": socket_phase["noisy_client"],
                "real_socket_turns": socket_phase["real_socket_turns"],
                "max_service_turns": max_turns,
                "bound": config.client_count + 1,
            },
            "memory": {"peak_sequencer_rss_bytes": _peak_rss_bytes()},
            "segments": {
                "count": len(segment_files),
                "sealed_digest_failures": sealed_digest_failures,
                "digests": segment_digests,
            },
            "replay": {
                "sequencer_projection_digest": sequencer_digest,
                "daemonless_projection_digest": daemonless_digest,
                "streaming_direct_equal": sequencer_digest == daemonless_digest,
            },
            "elapsed_seconds": round(time.perf_counter() - started, 3),
        }


__all__ = ["ScaleConfig", "run_scale_fixture"]

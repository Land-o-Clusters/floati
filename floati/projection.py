"""Shared read projection for CLI, watch, and the harbor board."""

from __future__ import annotations

import copy
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, Iterator, List, Mapping, Optional, Sequence

from .command_scope import CommandScope
from .errors import ProtocolRefusal, SnapshotRefusal
from .consumption import ConsumptionLedger, WORK_KINDS
from .effects import EffectLedger, EffectProjection
from .events import EVENT_KINDS
from .jsonl import read_records_snapshot
from .records import EFFECT_KINDS, THREAD_OBSERVATION_KINDS
from .registry import REGISTRY_KINDS, Registry
from .root import FloatiRoot
from .snapshot import SnapshotStore, SourceSpec
from .supervisor import Supervisor
from .thread_observations import ThreadObservationLedger, ThreadObservationProjection
from .workers import (
    WORKER_KINDS,
    WORKER_REFUSAL_KINDS,
    WorkerReceipts,
    WorkerRefusals,
)


_EFFECT_STATUS_FIELDS = (
    "operation_id", "run_id", "item_id", "attempt_id", "effect_type",
    "risk_class", "state", "current_evidence_id", "reconciliation_adapter",
    "spend_status", "budget_claim", "measured_spend", "compensation_state",
)
_EFFECT_ATTENTION_ORDER = ("unknown", "incomplete", "failed", "confirmed")
_EFFECT_ATTENTION_STATES = {
    "unknown": frozenset({"unknown", "reconciled_unknown"}),
    "incomplete": frozenset({"intent", "dispatched", "acknowledged"}),
    "failed": frozenset({"failed", "reconciled_failed"}),
    "confirmed": frozenset({"confirmed", "reconciled_confirmed"}),
}
_EFFECT_ATTENTION_RANK = {
    state: rank for rank, state in enumerate(_EFFECT_ATTENTION_ORDER)
}


def _effect_budget(value: object) -> object:
    if value is None:
        return None
    return [
        {"budget_id": str(row["budget_id"]), "amount": int(row["amount"])}
        if isinstance(row, Mapping)
        else {"budget_id": str(row[0]), "amount": int(row[1])}
        for row in value
    ]


def _effect_attention_state(operation: Mapping[str, object]) -> str:
    if operation["compensation_state"] == "proposed":
        return "incomplete"
    state = operation["state"]
    return next(
        attention
        for attention, members in _EFFECT_ATTENTION_STATES.items()
        if state in members
    )


def _effect_attention_key(operation: Mapping[str, object]) -> tuple[int, str]:
    attention = _effect_attention_state(operation)
    return _EFFECT_ATTENTION_RANK[attention], str(operation["operation_id"])


class EffectStatusProjection:
    """Physically read-only effect status shared by CLI and observers."""

    def __init__(self, root: FloatiRoot) -> None:
        self.root = root

    def _operations(self) -> List[Dict[str, object]]:
        records = read_records_snapshot(
            self.root, EffectLedger.relative_path, allowed_kinds=set(EFFECT_KINDS)
        )
        projection = EffectProjection.from_records(records)
        operation_ids = sorted(
            str(record["operation_id"])
            for record in records
            if record["kind"] == "effect_intent"
        )
        operations = []
        for operation_id in operation_ids:
            source = projection.operation(operation_id)
            row = {field: source[field] for field in _EFFECT_STATUS_FIELDS}
            row["budget_claim"] = _effect_budget(row["budget_claim"])
            row["measured_spend"] = _effect_budget(row["measured_spend"])
            operations.append(row)
        operations.sort(key=_effect_attention_key)
        return operations

    def artifact(
        self,
        now: datetime,
        *,
        run_id: Optional[str] = None,
        attempt_id: Optional[str] = None,
        operation_id: Optional[str] = None,
    ) -> Dict[str, object]:
        current = FleetProjection._current(now)
        operations = [
            row for row in self._operations()
            if (run_id is None or row["run_id"] == run_id)
            and (attempt_id is None or row["attempt_id"] == attempt_id)
            and (operation_id is None or row["operation_id"] == operation_id)
        ]
        return {
            "status_schema_version": 1,
            "kind": "effect_status",
            "observed_at": FleetProjection._timestamp(current),
            "operations": operations,
        }

    def summary(self) -> Dict[str, object]:
        operations = self._operations()
        counts = {
            label: sum(row["state"] in _EFFECT_ATTENTION_STATES[label] for row in operations)
            for label in _EFFECT_ATTENTION_ORDER
        }
        attention_counts = {
            label: sum(_effect_attention_state(row) == label for row in operations)
            for label in _EFFECT_ATTENTION_ORDER
        }
        return {
            "attention": [
                {"state": label, "count": attention_counts[label]}
                for label in _EFFECT_ATTENTION_ORDER
            ],
            "counts": counts,
            "compensation_counts": {
                state: sum(row["compensation_state"] == state for row in operations)
                for state in ("none", "proposed", "executed")
            },
        }


_THREAD_ATTENTION_ORDER = (
    "waiting_on_approval",
    "waiting_on_user_input",
    "multiple",
    "unknown",
    "none",
)
_THREAD_ATTENTION_RANK = {
    value: rank for rank, value in enumerate(_THREAD_ATTENTION_ORDER)
}


class ThreadObservationStatusProjection:
    """Physically read-only registered-thread testimony for operators."""

    def __init__(self, root: FloatiRoot) -> None:
        self.root = root

    def _rows(self, *, reveal_coordinate: bool) -> List[Dict[str, object]]:
        records = read_records_snapshot(
            self.root,
            ThreadObservationLedger.relative_path,
            allowed_kinds=set(THREAD_OBSERVATION_KINDS),
        )
        projected = ThreadObservationProjection.from_records(records)
        rows = []
        for state in projected.attachments():
            attachment = state["attachment"]
            observation = state["latest_observation"]
            assert isinstance(attachment, dict)
            if isinstance(observation, dict):
                status = copy.deepcopy(observation["provider_status"])
                flags = copy.deepcopy(observation["active_flags"])
                updated_at = copy.deepcopy(observation["provider_updated_at"])
                attention = copy.deepcopy(observation["attention"])
                outcome = observation["observation_outcome"]
                reason = observation["observation_reason"]
            else:
                status = {"value": "unknown", "evidence_class": "unknown"}
                flags = {"value": None, "evidence_class": "unknown"}
                updated_at = {"value": None, "evidence_class": "unknown"}
                attention = {"value": "unknown", "evidence_class": "unknown"}
                outcome = "unknown"
                reason = "not_observed"
            row: Dict[str, object] = {
                "attachment_id": attachment["id"],
                "subject_kind": attachment["subject_kind"],
                "work_item_id": attachment["work_item_id"],
                "run_id": attachment.get("run_id"),
                "attempt_id": attachment.get("attempt_id"),
                "provider_status": status,
                "active_flags": flags,
                "provider_updated_at": updated_at,
                "attention": attention,
                "observation_outcome": outcome,
                "observation_reason": reason,
                "detached": state["detachment"] is not None,
            }
            if reveal_coordinate:
                row["provider"] = attachment["provider"]
                row["provider_thread_id"] = attachment["provider_thread_id"]
            rows.append(row)
        rows.sort(
            key=lambda row: (
                _THREAD_ATTENTION_RANK[str(row["attention"]["value"])],
                str(row["attachment_id"]),
            )
        )
        return rows

    def artifact(
        self, now: datetime, *, attachment_id: Optional[str] = None
    ) -> Dict[str, object]:
        current = FleetProjection._current(now)
        rows = self._rows(reveal_coordinate=attachment_id is not None)
        if attachment_id is not None:
            rows = [row for row in rows if row["attachment_id"] == attachment_id]
        return {
            "status_schema_version": 1,
            "kind": "thread_observation_status",
            "observed_at": FleetProjection._timestamp(current),
            "attachments": rows,
        }

    def summary(self) -> Dict[str, object]:
        rows = self._rows(reveal_coordinate=False)
        return {
            "attention": [
                {
                    "state": label,
                    "count": sum(
                        row["attention"]["value"] == label for row in rows
                    ),
                }
                for label in _THREAD_ATTENTION_ORDER
            ],
            "observation_counts": {
                outcome: sum(
                    row["observation_outcome"] == outcome for row in rows
                )
                for outcome in ("observed", "unknown")
            },
            "registered_total": len(rows),
            "detached_total": sum(bool(row["detached"]) for row in rows),
        }

class FleetProjection:
    def __init__(self, root: FloatiRoot) -> None:
        self.root = root

    def snapshot(
        self,
        now: datetime,
        *,
        scope: Optional[CommandScope] = None,
    ) -> Dict[str, object]:
        snapshot, _ = self._snapshot(now)
        if scope is not None:
            snapshot["scope"] = scope.evidence()
        return snapshot

    def status_artifact(
        self,
        now: datetime,
        *,
        scope: Optional[CommandScope] = None,
    ) -> Dict[str, object]:
        current = self._current(now)
        store = None
        try:
            store = self._status_snapshot_store()
            loaded = store.load()
            artifact = self._status_from_snapshot(current, loaded.payload, loaded.tails)
            if scope is not None:
                artifact["scope"] = scope.evidence()
            return artifact
        except SnapshotRefusal:
            before_scan = None
            if store is not None:
                try:
                    before_scan = store.capture()
                except SnapshotRefusal:
                    pass
            snapshot, mode = self._snapshot(current)
            artifact = {
                "status_schema_version": 0,
                "kind": "fleet_status",
                "root": str(self.root.path),
                "tenant_id": self.root.tenant_id,
                "mode": mode,
                **snapshot,
            }
            payload = {
                "artifact": artifact,
                "work_states": ConsumptionLedger(self.root).project(),
                "built_at": self._timestamp(current),
                "time_sensitive": self._status_time_sensitive(),
            }
            if store is not None and before_scan is not None:
                try:
                    store.refresh(payload, expected=before_scan)
                except SnapshotRefusal:
                    pass
            if scope is not None:
                artifact["scope"] = scope.evidence()
            return artifact

    @staticmethod
    def _current(now: datetime) -> datetime:
        if (
            not isinstance(now, datetime)
            or now.tzinfo is None
            or now.utcoffset() is None
        ):
            raise ProtocolRefusal(
                "time_invalid", "an aware UTC-compatible datetime is required"
            )
        return now.astimezone(timezone.utc)

    @staticmethod
    def _timestamp(now: datetime) -> str:
        return now.isoformat(timespec="milliseconds").replace("+00:00", "Z")

    def _status_snapshot_store(self) -> SnapshotStore:
        return SnapshotStore(
            self.root,
            reader="status",
            key="default",
            discover_sources=self._status_sources,
        )

    def _status_sources(self) -> Sequence[SourceSpec]:
        sources = [
            SourceSpec(Path("registry/entries.jsonl"), frozenset(REGISTRY_KINDS)),
            SourceSpec(Path("events.jsonl"), frozenset(EVENT_KINDS)),
            SourceSpec(ConsumptionLedger.relative_path, frozenset(WORK_KINDS)),
            SourceSpec(Path("receipts/denials.jsonl"), frozenset({"denial_receipt"})),
            SourceSpec(Path("receipts/workers.jsonl"), frozenset(WORKER_KINDS)),
            SourceSpec(
                Path("receipts/worker-refusals.jsonl"),
                frozenset(WORKER_REFUSAL_KINDS),
            ),
            SourceSpec(EffectLedger.relative_path, frozenset(EFFECT_KINDS)),
            SourceSpec(
                ThreadObservationLedger.relative_path,
                frozenset(THREAD_OBSERVATION_KINDS),
            ),
        ]
        for directory, kinds in (
            ("receipts/deliveries", frozenset({"delivery_receipt", "wake_hold_receipt"})),
            ("receipts/acks", frozenset({"ack_receipt"})),
            ("authority-grants", frozenset({"authority_grant"})),
            ("mutual-exclusion-holds", frozenset({"mutual_exclusion_hold"})),
            ("liveness-presence", frozenset({"liveness_presence"})),
            ("receipts/tide-policy", frozenset({"tide_policy_record"})),
            ("receipts/tide", frozenset({"tide_receipt"})),
        ):
            absolute = self.root.resolve_relative(directory)
            if absolute.is_dir():
                sources.extend(
                    SourceSpec(path.relative_to(self.root.tenant_home), kinds)
                    for path in sorted(absolute.glob("*.jsonl"))
                )
        return tuple(sources)

    def _status_time_sensitive(self) -> bool:
        prefixes = {
            "authority-grants",
            "mutual-exclusion-holds",
            "liveness-presence",
            "events.jsonl",
            "receipts",
        }
        for source in self._status_sources():
            if source.relative.parts[0] not in prefixes:
                continue
            path = self.root.resolve_relative(source.relative)
            if path.is_file() and path.stat().st_size:
                return True
        return False

    def _status_from_snapshot(
        self,
        now: datetime,
        payload: Dict[str, object],
        tails: Dict[str, Sequence[Dict[str, object]]],
    ) -> Dict[str, object]:
        if set(payload) != {
            "artifact",
            "work_states",
            "built_at",
            "time_sensitive",
        }:
            raise SnapshotRefusal(
                "snapshot_payload_invalid", "status snapshot fields are invalid"
            )
        artifact = payload["artifact"]
        work_states = payload["work_states"]
        built_at = payload["built_at"]
        time_sensitive = payload["time_sensitive"]
        if (
            not isinstance(artifact, dict)
            or not isinstance(work_states, dict)
            or not isinstance(built_at, str)
            or not isinstance(time_sensitive, bool)
        ):
            raise SnapshotRefusal(
                "snapshot_payload_invalid", "status snapshot payload is malformed"
            )
        if time_sensitive and self._timestamp(now) != built_at:
            raise SnapshotRefusal(
                "snapshot_clock_boundary", "time-sensitive status must be reprojected"
            )
        result = copy.deepcopy(artifact)
        if "threads" not in result or "unrecognized_kinds" not in result:
            raise SnapshotRefusal(
                "snapshot_payload_invalid",
                "status snapshot predates registered thread testimony",
            )
        nodes = result.get("nodes")
        if not isinstance(nodes, list) or any(
            not isinstance(node, dict) or "wake_health" not in node
            for node in nodes
        ):
            raise SnapshotRefusal(
                "snapshot_payload_invalid",
                "status snapshot predates wake-health testimony",
            )
        result["observed_at"] = self._timestamp(now)

        unsupported = set()
        work_tail: Sequence[Dict[str, object]] = ()
        for path, records in tails.items():
            if not records:
                continue
            if path == "events.jsonl":
                self._apply_status_event_tail(result, records, now=now)
            elif path == ConsumptionLedger.relative_path.as_posix():
                work_tail = records
            elif path == "receipts/denials.jsonl":
                result["receipt_counts"]["denial"] += len(records)
            elif path == "receipts/worker-refusals.jsonl":
                result["worker_refusals"].extend(copy.deepcopy(records))
                if any(row["reason_code"] == "worker_work_absent" for row in records):
                    result["consumption"]["wake_state"] = "unsatisfied_wake"
            else:
                unsupported.add(path)
        if unsupported:
            raise SnapshotRefusal(
                "snapshot_tail_history_required",
                "status tail needs omitted projection history",
            )
        if work_tail:
            projected = ConsumptionLedger(self.root).project_tail(
                work_states, work_tail
            )
            counts = {
                state: sum(1 for item in projected.values() if item["state"] == state)
                for state in ("open", "claimed", "completed")
            }
            result["work_counts"] = counts
            result["consumption"]["counts"] = dict(counts)
            result["consumption"]["state"] = (
                "work_available" if counts["open"] else "caught_up"
            )
        return result

    @staticmethod
    def _apply_status_event_tail(
        artifact: Dict[str, object], records: Sequence[Dict[str, object]], *, now: datetime
    ) -> None:
        from .mail_health import oldest_unread_fact

        nodes = {str(row["node_id"]): row for row in artifact["nodes"]}
        if records and any("wake_health" in node for node in nodes.values()):
            raise SnapshotRefusal(
                "snapshot_tail_history_required",
                "wake-health event tail needs full projection history",
            )
        pending_by_recipient: Dict[str, list[Dict[str, object]]] = {}
        for record in records:
            if record.get("kind") != "message_envelope":
                raise SnapshotRefusal(
                    "snapshot_tail_history_required",
                    "event tail needs full ledger history",
                )
            if record.get("reply_to") is not None:
                raise SnapshotRefusal(
                    "snapshot_tail_history_required",
                    "reply tail needs omitted causal history",
                )
            sender = nodes.get(str(record["sender"]))
            recipient = nodes.get(str(record["recipient"]))
            if sender is not None:
                sender["last_activity"] = record["timestamp"]
            if recipient is not None:
                if recipient.get("oldest_unread") is not None:
                    raise SnapshotRefusal(
                        "snapshot_tail_history_required",
                        "unread event tail needs omitted oldest-message history",
                    )
                recipient["last_activity"] = record["timestamp"]
                recipient["inbox_depth"] += 1
                pending_by_recipient.setdefault(str(record["recipient"]), []).append(record)
        for recipient_id, pending in pending_by_recipient.items():
            recipient = nodes.get(recipient_id)
            if recipient is not None:
                recipient["oldest_unread"] = oldest_unread_fact(
                    recipient_id, pending, now=now
                )

    def _snapshot(self, now: datetime) -> tuple[Dict[str, object], str]:
        supervised = Supervisor(self.root).snapshot(now)
        from .wake_health import WakeHealthProjection

        wake_health = WakeHealthProjection(self.root)
        for node in supervised["nodes"]:
            node["wake_health"] = wake_health.fact(str(node["node_id"]), now)
        consumption = ConsumptionLedger(self.root).summary()
        work_counts = dict(consumption["counts"])
        worker_refusals = WorkerRefusals(self.root).records()
        if any(row["reason_code"] == "worker_work_absent" for row in worker_refusals):
            consumption["wake_state"] = "unsatisfied_wake"
        else:
            consumption["wake_state"] = "none"
        receipt_counts = {
            "delivery": self._count_directory("receipts/deliveries", "delivery_receipt"),
            "ack": self._count_directory("receipts/acks", "ack_receipt"),
            "denial": len(read_records_snapshot(self.root, "receipts/denials.jsonl", allowed_kinds={"denial_receipt"})),
        }
        snapshot = {
            "observed_at": supervised["observed_at"],
            "nodes": supervised["nodes"],
            "stale_leases": supervised["stale_leases"],
            "stale_lease_count": len(supervised["stale_leases"]),
            "work_counts": work_counts,
            "receipt_counts": receipt_counts,
            "consumption": consumption,
            "worker_refusals": worker_refusals,
            "workers": WorkerReceipts(self.root).sessions(),
            "threads": ThreadObservationStatusProjection(self.root).summary(),
            "unrecognized_kinds": supervised["unrecognized_kinds"],
        }
        return snapshot, str(supervised["mode"])

    def receipts(self, node_id: str) -> Dict[str, object]:
        Registry(self.root).require_active(node_id)
        deliveries = read_records_snapshot(
            self.root, Path("receipts/deliveries") / f"{node_id}.jsonl",
            allowed_kinds={"delivery_receipt", "wake_hold_receipt"},
        )
        acknowledgments = read_records_snapshot(
            self.root, Path("receipts/acks") / f"{node_id}.jsonl",
            allowed_kinds={"ack_receipt"},
        )
        denials = [
            record for record in read_records_snapshot(
                self.root, "receipts/denials.jsonl", allowed_kinds={"denial_receipt"}
            )
            if node_id in (record["claimed_sender"], record["claimed_recipient"])
        ]
        return {"node_id": node_id, "deliveries": [row for row in deliveries if row["kind"] == "delivery_receipt"], "acks": acknowledgments, "denials": denials}

    def _count_directory(self, directory: str, kind: str) -> int:
        absolute = self.root.resolve_relative(directory)
        if not absolute.is_dir():
            return 0
        return sum(
            sum(1 for row in read_records_snapshot(self.root, path.relative_to(self.root.tenant_home), allowed_kinds={kind, "wake_hold_receipt"}) if row["kind"] == kind)
            for path in sorted(absolute.glob("*.jsonl"))
        )


def collect_deltas(
    projection: FleetProjection,
    interval: float = 0.25,
    iterations: Optional[int] = None,
    *,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    sleeper: Callable[[float], None] = time.sleep,
) -> List[Dict[str, object]]:
    return list(iter_deltas(projection, interval, iterations, now=now, sleeper=sleeper))


def iter_deltas(
    projection: FleetProjection,
    interval: float = 0.25,
    iterations: Optional[int] = None,
    *,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    sleeper: Callable[[float], None] = time.sleep,
) -> Iterator[Dict[str, object]]:
    if not isinstance(interval, (int, float)) or isinstance(interval, bool) or not 0.05 <= float(interval) <= 60:
        raise ProtocolRefusal("watch_interval_invalid", "watch interval must be 0.05 through 60 seconds")
    if iterations is not None and (
        not isinstance(iterations, int) or isinstance(iterations, bool) or not 1 <= iterations <= 10000
    ):
        raise ProtocolRefusal("watch_iterations_invalid", "watch iterations must be 1 through 10000")
    count = 0
    previous = None
    while iterations is None or count < iterations:
        snapshot = projection.snapshot(now())
        comparable = dict(snapshot)
        comparable.pop("observed_at", None)
        encoded = json.dumps(comparable, sort_keys=True, separators=(",", ":"))
        if previous is None:
            yield {"kind": "initial", "snapshot": snapshot}
        elif encoded != previous:
            yield {"kind": "change", "snapshot": snapshot}
        previous = encoded
        count += 1
        if iterations is None or count < iterations:
            sleeper(float(interval))

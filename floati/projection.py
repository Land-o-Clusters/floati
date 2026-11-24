"""Shared read projection for CLI, watch, and the harbor board."""

from __future__ import annotations

import copy
import json
import os
import re
import stat
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, Iterator, List, Mapping, Optional, Sequence

from .command_scope import CommandScope
from .bus_epoch import epoch_guard
from .errors import IntegrityFailure, ProtocolRefusal, SnapshotRefusal
from .consumption import ConsumptionLedger, WORK_KINDS
from .message_consumption import MessageConsumption
from .effects import EffectLedger, EffectProjection
from .events import EVENT_KINDS
from .jsonl import read_records_sealed_snapshot, read_records_snapshot
from .records import EFFECT_KINDS, THREAD_OBSERVATION_KINDS, WAKE_HOLD_KINDS
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


def provider_state_size(root: FloatiRoot, profile: Mapping[str, object]) -> Dict[str, object]:
    """Count only bounded descriptor-bound metadata, never conversation content."""
    try:
        coordinates = {"root": root.tenant_home,
            "provider_state": root.tenant_home / str(profile["provider_state"])}
        with root.bind_adhoc_coordinates(coordinates) as (identities, descriptors):
            if any(identities[key] != profile["filesystem_bindings"][key]
                   for key in coordinates):
                raise ValueError("replaced provider directory")
            remaining, total = 4096, 0
            def count(directory, depth):
                nonlocal remaining, total
                metadata = os.fstat(directory)
                if depth > 64 or metadata.st_uid != os.geteuid():
                    raise ValueError("unsafe directory")
                with os.scandir(directory) as entries:
                    for entry in entries:
                        remaining -= 1
                        if remaining < 0:
                            raise ValueError("metadata enumeration bound")
                        info = entry.stat(follow_symlinks=False)
                        if info.st_uid != os.geteuid():
                            raise ValueError("foreign provider entry")
                        if stat.S_ISDIR(info.st_mode):
                            child = os.open(entry.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                dir_fd=directory)
                            try:
                                actual = os.fstat(child)
                                if (actual.st_dev, actual.st_ino) != (info.st_dev, info.st_ino):
                                    raise ValueError("changed directory")
                                count(child, depth + 1)
                            finally:
                                os.close(child)
                        elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                            total += info.st_size
                            if total > 2 ** 63 - 1:
                                raise ValueError("metadata size bound")
                        else:
                            raise ValueError("nonordinary provider entry")
            slot = descriptors["provider_state"]
            if stat.S_IMODE(os.fstat(slot).st_mode) != 0o700:
                raise ValueError("provider slot is not private")
            count(slot, 0)
            return {"value": total, "evidence_class": "measured"}
    except (OSError, ProtocolRefusal, ValueError, KeyError, TypeError):
        return {"value": None, "evidence_class": "unknown", "reason": "provider_state_unsafe"}


class AdhocThreadStatusProjection:
    """Join durable execution references without asking a provider for status."""

    def __init__(self, root: FloatiRoot) -> None:
        self.root = root

    def artifact(self, now: datetime) -> Dict[str, object]:
        from .adhoc_profiles import AdhocProfiles
        from .adhoc_journal import AdhocJournal
        from .work import WorkLog

        profiles = AdhocProfiles(self.root).records()
        if not profiles:
            return {"profiles": []}
        history = AdhocJournal(self.root).records()
        registry = Registry(self.root).known_records()
        active_nodes = set(Registry(self.root).active_node_ids())
        leases = {row["id"]: row for row in registry
            if row["kind"] == "node_lease"}
        workers = WorkerReceipts(self.root).records()
        rows = []
        for profile in profiles:
            admissions = [row for row in history if row["kind"] == "adhoc_dispatch_record"
                and row["profile_id"] == profile["id"]]
            latest = admissions[-1] if admissions else None
            phases = [row for row in history if latest is not None
                and row.get("dispatch_id") == latest["dispatch_id"]
                and row["kind"] != "adhoc_dispatch_record"]
            coordinate, native_id = None, None
            for phase in phases:
                evidence = phase["evidence"]
                if phase["kind"] == "adhoc_native_thread_opened_record":
                    coordinate = evidence.get("provider_coordinate")
                    native_id = phase["id"]
                elif phase.get("phase") == "provider_terminal":
                    event = evidence.get("event", {})
                    if event.get("provider_coordinate") is not None:
                        coordinate, native_id = event["provider_coordinate"], phase["id"]
            work_id = latest["dispatch"]["work_item_id"] if latest else None
            work_state = None
            if work_id is not None:
                work_state = WorkLog(self.root).show(work_id)[0]["state"]
            lease = leases.get(profile["node_lease_id"])
            current_lease = next((row for row in reversed(registry)
                if row.get("kind") == "node_lease" and row.get("node_id") == profile["node_id"]), None)
            lease_active = (profile["node_id"] in active_nodes and lease is not None
                and current_lease == lease and lease["state"] == "active"
                and now < datetime.fromisoformat(str(lease["expires_at"]).replace("Z", "+00:00")))
            phase = phases[-1].get("phase", "provider_opened") if phases else "admitted" if latest else "profiled"
            rows.append({"profile_id": profile["id"], "node_id": profile["node_id"],
                "node_lease_id": profile["node_lease_id"], "lane_opening_id": profile["lane_opening_id"],
                "adapter": profile["adapter"], "dispatch_id": latest["dispatch_id"] if latest else None,
                "phase": phase,
                "work_item_id": work_id, "work_state": work_state,
                "worker_receipt_ids": [row["id"] for row in workers if row["work_item_id"] == work_id],
                "native_receipt_id": native_id, "provider_coordinate": copy.deepcopy(coordinate),
                "provider_status": {"value": "unknown", "evidence_class": "unknown"},
                "observation_reason": "not_observed", "lease_active": lease_active,
                "history_state": "retained" if lease_active and phase != "teardown_completed" else "inactive_non_resumable",
                "provider_state_bytes": provider_state_size(self.root, profile)})
        return {"profiles": rows}


class FleetProjection:
    MESSAGE_PROGRESS_OBSERVATION_VERSION = 1

    def __init__(self, root: FloatiRoot) -> None:
        self.root = root

    def snapshot(
        self,
        now: datetime,
        *,
        scope: Optional[CommandScope] = None,
    ) -> Dict[str, object]:
        from .mail_summary import observe
        from .send_prefix import _CACHE_ERRORS

        mail = observe(self.root)
        if mail is not None:
            try:
                snapshot, _ = self._summary_snapshot(now, mail)
            except _CACHE_ERRORS:
                snapshot, _ = self._snapshot(now)
        else:
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
            from .status_snapshot import StatusSnapshotStore
            from .send_prefix import _CACHE_ERRORS

            mail = store.observation if isinstance(store, StatusSnapshotStore) else None
            if mail is not None:
                try:
                    snapshot, mode = self._summary_snapshot(current, mail)
                except _CACHE_ERRORS:
                    # Disposable proof failure must retain the complete native
                    # reader and its original authoritative-source anchors.
                    store = SnapshotStore(self.root, reader="status", key="default",
                                          discover_sources=self._status_sources)
                    try:
                        before_scan = store.capture()
                    except SnapshotRefusal:
                        before_scan = None
                    snapshot, mode = self._snapshot(current)
            else:
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
                "message_progress_observation_version": self.MESSAGE_PROGRESS_OBSERVATION_VERSION,
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
        from .mail_summary import observe
        from .status_snapshot import StatusSnapshotStore
        from .send_prefix import _CACHE_ERRORS

        mail = observe(self.root)
        if mail is not None:
            try:
                return StatusSnapshotStore(self.root, observation=mail,
                                           discover_sources=self._status_sources)
            except (SnapshotRefusal, *_CACHE_ERRORS):
                pass
        return SnapshotStore(
            self.root,
            reader="status",
            key="default",
            discover_sources=self._status_sources,
        )

    def _status_sources(self) -> Sequence[SourceSpec]:
        from .adhoc_records import PROFILE_RELATIVE, JOURNAL_RELATIVE
        from .adhoc_profiles import PROFILE_KINDS
        from .adhoc_journal import JOURNAL_KINDS
        sources = [
            SourceSpec(PROFILE_RELATIVE, frozenset(PROFILE_KINDS)),
            SourceSpec(JOURNAL_RELATIVE, frozenset(JOURNAL_KINDS)),
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
            ("receipts/stop-holds", frozenset({"stop_hold_receipt"})),
            ("receipts/deliveries", frozenset({"delivery_receipt", "delivered_unpresented", "wake_hold_receipt"})),
            ("receipts/acks", frozenset({"ack_receipt"})),
            ("receipts/consumed", frozenset({"consumption_receipt"})),
            ("receipts/wakes", frozenset({"wake_attempt_receipt"})),
            ("authority-grants", frozenset({"authority_grant"})),
            ("mutual-exclusion-holds", frozenset({"mutual_exclusion_hold"})),
            ("liveness-presence", frozenset({"liveness_presence"})),
            ("receipts/tide-policy", frozenset({"tide_policy_record"})),
            ("receipts/tide", frozenset({"tide_receipt"})),
        ):
            absolute = self.root.resolve_relative(directory)
            if absolute.is_dir():
                paths = list(absolute.glob("*.jsonl"))
                if directory in {"receipts/deliveries", "receipts/acks", "receipts/consumed"}:
                    # Worker-session ledgers live at node/session-hash.jsonl.
                    # All three planes affect message_progress, so a warm
                    # status or harbor snapshot must observe their appends.
                    paths.extend(absolute.glob("*/*.jsonl"))
                sources.extend(
                    SourceSpec(path.relative_to(self.root.tenant_home), kinds)
                    for path in sorted(paths)
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
        return self._registry_has_active_node_lease()

    def _registry_has_active_node_lease(self) -> bool:
        """An active node lease expires with the clock, so a warm status must rebuild."""

        from .registry import read_registry_compatible

        records, _unrecognized, _versions = read_registry_compatible(self.root)
        return any(
            record.get("kind") == "node_lease" and record.get("state") == "active"
            for record in records
        )

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
            "message_progress_observation_version",
        }:
            raise SnapshotRefusal(
                "snapshot_payload_invalid", "status snapshot fields are invalid"
            )
        artifact = payload["artifact"]
        work_states = payload["work_states"]
        built_at = payload["built_at"]
        time_sensitive = payload["time_sensitive"]
        if (
            type(payload["message_progress_observation_version"]) is not int
            or payload["message_progress_observation_version"] != self.MESSAGE_PROGRESS_OBSERVATION_VERSION
            or not isinstance(artifact, dict)
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
            not isinstance(node, dict) or not isinstance(node.get("wake_health"), dict)
            or "recent_sessions" not in node["wake_health"]
            or "seat_attention" not in node
            or node.get("lifetime") not in {"permanent", "temporary"}
            or not self._valid_message_progress(node.get("message_progress"))
            for node in nodes
        ):
            raise SnapshotRefusal(
                "snapshot_payload_invalid",
                "status snapshot predates recent-session testimony",
            )
        result["observed_at"] = self._timestamp(now)
        adhoc = AdhocThreadStatusProjection(self.root).artifact(now)
        if adhoc["profiles"]:
            result["adhoc_threads"] = adhoc
        else:
            result.pop("adhoc_threads", None)

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
        # Cursor journals are best-effort state, not governed SnapshotStore
        # records. Reobserve only this bounded hour on a cache hit, including
        # when the clock advances without a ledger write.
        from .wake_health import WakeHealthProjection

        wake_health = WakeHealthProjection(self.root)
        for node in result["nodes"]:
            node["wake_health"]["recent_sessions"] = wake_health.recent_sessions(
                str(node["node_id"]), now,
            )
        return result

    @staticmethod
    def _valid_message_progress(value: object) -> bool:
        if isinstance(value, dict) and value.get("state") == "unknown":
            return (set(value) == {"state", "reason_code", "delivered", "acknowledged", "consumed"}
                    and isinstance(value.get("reason_code"), str)
                    and re.fullmatch(r"[a-z][a-z0-9_]{0,127}", value["reason_code"]) is not None
                    and all(value[key] is None for key in ("delivered", "acknowledged", "consumed")))
        return (
            isinstance(value, dict)
            and set(value) == {"delivered", "acknowledged", "consumed"}
            and all(type(count) is int and count >= 0 for count in value.values())
        )

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
        # Retain the independent native projection for cold recovery and
        # census equivalence. Only the status producer supplies a certified
        # mail observation to the shared projection below.
        return self._project_snapshot(now)

    def _summary_snapshot(self, now: datetime, mail) -> tuple[Dict[str, object], str]:
        from .send_prefix import CacheMiss

        if not mail.current():
            raise CacheMiss()
        result = self._project_snapshot(now, mail=mail)
        if not mail.current():
            raise CacheMiss()
        return result

    def _project_snapshot(self, now: datetime, *, mail=None) -> tuple[Dict[str, object], str]:
        supervisor = Supervisor(self.root)
        supervised = supervisor.snapshot(now, mail=False if mail is None else mail)
        from .wake_health import WakeHealthProjection

        wake_health = WakeHealthProjection(self.root)
        for node in supervised["nodes"]:
            node["wake_health"] = (wake_health.fact(str(node["node_id"]), now, mail=False) if mail is None
                                   else wake_health.fact(str(node["node_id"]), now, mail=mail))
        consumption = ConsumptionLedger(self.root).summary()
        work_counts = dict(consumption["counts"])
        worker_refusals = WorkerRefusals(self.root).records()
        if any(row["reason_code"] == "worker_work_absent" for row in worker_refusals):
            consumption["wake_state"] = "unsatisfied_wake"
        else:
            consumption["wake_state"] = "none"
        receipt_counts = {
            "delivery": (mail.receipt_counts["delivery"] if mail is not None else
                         self._count_directory("receipts/deliveries", "delivery_receipt", allowed=set(WAKE_HOLD_KINDS))),
            "ack": (mail.receipt_counts["ack"] if mail is not None else
                    self._count_directory("receipts/acks", "ack_receipt")),
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
        adhoc = AdhocThreadStatusProjection(self.root).artifact(now)
        if adhoc["profiles"]:
            snapshot["adhoc_threads"] = adhoc
        return snapshot, str(supervised["mode"])

    def receipts(self, node_id: str) -> Dict[str, object]:
        with epoch_guard(self.root):
            Registry(self.root).require_active(node_id)
            deliveries = read_records_sealed_snapshot(
                self.root, Path("receipts/deliveries") / f"{node_id}.jsonl",
                allowed_kinds={"delivery_receipt", "delivered_unpresented", "wake_hold_receipt"},
            )
            acknowledgments = read_records_sealed_snapshot(
                self.root, Path("receipts/acks") / f"{node_id}.jsonl",
                allowed_kinds={"ack_receipt"},
            )
            denials = [
                record for record in read_records_sealed_snapshot(
                    self.root, "receipts/denials.jsonl", allowed_kinds={"denial_receipt"}
                )
                if node_id in (record["claimed_sender"], record["claimed_recipient"])
            ]
            ledger = MessageConsumption(self.root)
            progress = ledger.progress(node_id, sealed=True)
            consumptions = []
            if progress.get("state") != "unknown":
                try:
                    consumptions = ledger.records(node_id, sealed=True)
                except IntegrityFailure as error:
                    progress = ledger.unknown_progress(error)
            result = {"node_id": node_id, "deliveries": [row for row in deliveries if row["kind"] in ("delivery_receipt", "delivered_unpresented")], "acks": acknowledgments, "consumptions": consumptions, "denials": denials}
            if progress.get("state") == "unknown":
                result["message_progress"] = progress
            return result

    def _count_directory(self, directory: str, kind: str, *, allowed: Optional[frozenset] = None) -> int:
        absolute = self.root.resolve_relative(directory)
        if not absolute.is_dir():
            return 0
        return sum(
            sum(1 for row in read_records_snapshot(self.root, path.relative_to(self.root.tenant_home), allowed_kinds=set(allowed) if allowed is not None else {kind, "wake_hold_receipt"}) if row["kind"] == kind)
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

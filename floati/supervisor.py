"""Read-only fleet supervision projection; it never performs plane actions."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Sequence

from .errors import ProtocolRefusal
from .consumption import ConsumptionLedger
from .delivery_health import DeliveryHealthAnalyzer
from .events import EVENT_KINDS, validate_event_records
from .jsonl import read_records_compatible_snapshot, read_records_snapshot
from .registry import REGISTRY_KINDS
from .root import FloatiRoot
from .workers import WorkerReceipts, WorkerRefusals


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ProtocolRefusal("time_invalid", "an aware UTC-compatible datetime is required")
    return value.astimezone(timezone.utc)


def _parse(value: object) -> datetime:
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)


def _latest_files(root: FloatiRoot, directory: str, kind: str) -> List[Dict[str, object]]:
    absolute = root.resolve_relative(directory)
    if not absolute.is_dir():
        return []
    latest = []
    for path in sorted(absolute.glob("*.jsonl")):
        relative = path.relative_to(root.tenant_home)
        records = read_records_snapshot(root, relative, allowed_kinds={kind})
        if records:
            latest.append(records[-1])
    return latest


class Supervisor:
    def __init__(self, root: FloatiRoot) -> None:
        self.root = root

    def snapshot(self, now: datetime) -> Dict[str, object]:
        from .projection import EffectStatusProjection
        from .tide import TideEvaluator
        from .tide_policy import TidePolicyLedger

        current = _utc(now)
        registry = read_records_snapshot(
            self.root, "registry/entries.jsonl", allowed_kinds=set(REGISTRY_KINDS)
        )
        active_nodes = []
        seen = set()
        for record in reversed(registry):
            if record["kind"] != "registry_entry":
                continue
            node = str(record["node_id"])
            if node in seen:
                continue
            seen.add(node)
            if record["state"] == "active":
                active_nodes.append(record)
        active_nodes.reverse()

        authority = _latest_files(self.root, "authority-grants", "authority_grant")
        mutex = _latest_files(self.root, "mutual-exclusion-holds", "mutual_exclusion_hold")
        events, unrecognized = read_records_compatible_snapshot(
            self.root, "events.jsonl", allowed_kinds=set(EVENT_KINDS)
        )
        validate_event_records(events)
        events = [
            record for record in events
            if record.get("kind") == "message_envelope"
        ]
        stale = self._stale(authority, "authority", "subject_id", current)
        stale.extend(self._stale(mutex, "mutex", "resource_id", current))
        stale.sort(key=lambda item: (str(item["plane"]), str(item["subject_id"])))

        nodes = []
        tide_policies = TidePolicyLedger(self.root)
        tide_status = TideEvaluator(
            self.root,
            source_sha="f2b587634cfc6d6a52cc24bd02bfd978919c359b",
        )
        for entry in active_nodes:
            node_id = str(entry["node_id"])
            acked = self._acked(node_id)
            inbox_depth = sum(
                1 for event in events
                if event["recipient"] == node_id and event["id"] not in acked
            )
            liveness_records = read_records_snapshot(
                self.root,
                Path("liveness-presence") / f"{node_id}.jsonl",
                allowed_kinds={"liveness_presence"},
            )
            node_times = [str(entry["timestamp"])]
            node_times.extend(str(record["timestamp"]) for record in events if node_id in (record["sender"], record["recipient"]))
            node_times.extend(str(record["timestamp"]) for record in authority if record["holder"] == node_id)
            node_times.extend(str(record["timestamp"]) for record in mutex if record["holder"] == node_id)
            if liveness_records:
                node_times.append(str(liveness_records[-1]["timestamp"]))
            nodes.append({
                "node_id": node_id,
                "role": entry["role"],
                "liveness": self._liveness(liveness_records, current),
                "authority": self._plane_state(authority, node_id, current),
                "mutex": self._plane_state(mutex, node_id, current),
                "inbox_depth": inbox_depth,
                "last_activity": node_times[-1],
                "tide": {
                    "policy": "active" if tide_policies.show(node_id) is not None else "off",
                    "turnover_state": tide_status.status(node_id)["turnover_state"],
                },
            })

        health = DeliveryHealthAnalyzer.analyze(
            events=events,
            root=self.root,
            nodes=[str(row["node_id"]) for row in nodes],
            now=current,
        )
        for row in nodes:
            row["oldest_unread"] = health.by_node[str(row["node_id"])].oldest_unread

        return {
            "mode": "report_only",
            "observed_at": current.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "nodes": nodes,
            "stale_leases": stale,
            "consumption": ConsumptionLedger(self.root).summary(),
            "worker_refusals": WorkerRefusals(self.root).records(),
            "workers": WorkerReceipts(self.root).sessions(),
            "effects": EffectStatusProjection(self.root).summary(),
            "unrecognized_kinds": unrecognized,
        }

    def _acked(self, node_id: str) -> set[str]:
        records = read_records_snapshot(
            self.root,
            Path("receipts/acks") / f"{node_id}.jsonl",
            allowed_kinds={"ack_receipt"},
        )
        return {str(item_id) for record in records for item_id in record["item_ids"]}

    @staticmethod
    def _liveness(records: Sequence[Dict[str, object]], current: datetime) -> str:
        if not records:
            return "unknown"
        latest = records[-1]
        observed = _parse(latest["observed_at"])
        expires = _parse(latest["expires_at"])
        if current >= expires:
            return "expired"
        if current >= observed + (expires - observed) / 2:
            return "silent"
        return "present"

    @staticmethod
    def _plane_state(records: Iterable[Dict[str, object]], node_id: str, current: datetime) -> str:
        states = []
        for record in records:
            if record["holder"] != node_id or record["state"] == "released":
                continue
            states.append("expired" if current >= _parse(record["expires_at"]) else "active")
        if "active" in states:
            return "active"
        if "expired" in states:
            return "expired"
        return "none"

    @staticmethod
    def _stale(records: Iterable[Dict[str, object]], plane: str, subject_field: str, current: datetime) -> List[Dict[str, object]]:
        return [
            {
                "plane": plane,
                "subject_id": record[subject_field],
                "holder": record["holder"],
                "epoch": record["epoch"],
                "expires_at": record["expires_at"],
            }
            for record in records
            if record["state"] == "active" and current >= _parse(record["expires_at"])
        ]

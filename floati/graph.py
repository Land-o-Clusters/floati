"""Deterministic Harbor Chart topology projected only from durable ledgers."""

from __future__ import annotations

from pathlib import Path
from typing import Dict

from .events import EventLog
from .consumption import ConsumptionLedger
from .jsonl import read_records_snapshot
from .root import FloatiRoot
from .workers import WorkerReceipts
from .runtruth import RunLedger


class HarborGraph:
    def __init__(self, root: FloatiRoot) -> None:
        self.root = root

    def artifact(self) -> Dict[str, object]:
        _events, unrecognized = EventLog(self.root).compatible_event_records(
            snapshot=True
        )
        registry = read_records_snapshot(
            self.root,
            Path("registry/entries.jsonl"),
            allowed_kinds={"registry_entry"},
        )
        latest_nodes: Dict[str, Dict[str, object]] = {}
        for record in registry:
            latest_nodes[str(record["node_id"])] = record
        nodes = [
            {
                "id": node_id,
                "kind": "node",
                "role": str(record["role"]),
                "state": str(record["state"]),
            }
            for node_id, record in sorted(latest_nodes.items())
        ]

        work = ConsumptionLedger(self.root).project()
        run_ledger = RunLedger(self.root)
        run_projection = run_ledger.project()
        canonical_edges = [
            {"kind": "run_dependency", "source": edge.source, "target": edge.target,
             "requires": edge.requires, "failure_policy": edge.failure_policy}
            for run_id in {str(record["run_id"]) for record in run_ledger.records() if record["kind"] == "run_created"}
            for edge in run_projection.edges(run_id)
        ]
        canonical_pairs = {(row["source"], row["target"]) for row in canonical_edges}
        legacy_edges = [
            {
                "kind": "work_dependency",
                "source": str(dependency),
                "target": str(item_id),
                "requires": "accepted",
                "failure_policy": "fail_run",
            }
            for item_id, item in work.items()
            for dependency in item["needs"]
            if (str(dependency), str(item_id)) not in canonical_pairs
        ]
        edges = sorted(canonical_edges + legacy_edges, key=lambda row: (row["source"], row["target"], row["kind"]))

        workers = sorted(
            (
                {
                    "id": str(row["session_id"]),
                    "kind": "worker",
                    "node_id": str(row["node_id"]),
                    "work_item_id": str(row["work_item_id"]),
                    "adapter": str(row["adapter"]),
                    "state": str(row["state"]),
                    "outcome_code": row["outcome_code"],
                }
                for row in WorkerReceipts(self.root).sessions()
            ),
            key=lambda row: row["id"],
        )

        bridge_records = read_records_snapshot(
            self.root,
            Path("bridges/records.jsonl"),
            allowed_kinds={"bridge_record"},
        )
        bridge_stubs = sorted(
            (
                {
                    "kind": "bridge_stub",
                    "bridge_id": str(record["bridge_id"]),
                    "peer_tenant_id": str(
                        record["right_tenant_id"]
                        if record["left_tenant_id"] == self.root.tenant_id
                        else record["left_tenant_id"]
                    ),
                    "state": str(record["state"]),
                }
                for record in bridge_records
            ),
            key=lambda row: (row["bridge_id"], row["peer_tenant_id"]),
        )
        return {
            "schema_version": 0,
            "topology_version": "0",
            "tenant_id": self.root.tenant_id,
            "nodes": nodes,
            "workers": workers,
            "edges": edges,
            "bridge_stubs": bridge_stubs,
            "unrecognized_kinds": unrecognized,
        }


class HarborTraffic:
    """Counts-only directed fleet traffic projected from durable ledgers."""

    def __init__(self, root: FloatiRoot) -> None:
        self.root = root

    def artifact(self) -> Dict[str, object]:
        counts: Dict[tuple[str, str], Dict[str, int]] = {}

        def counter(sender: object, recipient: object) -> Dict[str, int]:
            key = (str(sender), str(recipient))
            if key not in counts:
                counts[key] = {"envelope_count": 0, "denial_count": 0}
            return counts[key]

        events, unrecognized = EventLog(self.root).compatible_event_records()
        for record in events:
            if record["kind"] == "message_envelope":
                counter(record["sender"], record["recipient"])["envelope_count"] += 1

        denials = read_records_snapshot(
            self.root,
            Path("receipts/denials.jsonl"),
            allowed_kinds={"denial_receipt"},
        )
        for record in denials:
            counter(
                record["claimed_sender"], record["claimed_recipient"]
            )["denial_count"] += 1

        pairs = [
            {
                "sender": sender,
                "recipient": recipient,
                "envelope_count": values["envelope_count"],
                "denial_count": values["denial_count"],
            }
            for (sender, recipient), values in sorted(counts.items())
        ]
        return {
            "schema_version": 0,
            "tenant_id": self.root.tenant_id,
            "pairs": pairs,
            "unrecognized_kinds": unrecognized,
        }

"""The single validated work-ledger coordinate consumed by operators and workers."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Dict, Optional, Sequence

from .errors import IntegrityFailure
from .jsonl import read_records_snapshot
from .root import FloatiRoot


WORK_KINDS = {"work_item", "work_transition"}


class ConsumptionLedger:
    """Read and project the authoritative work consumption ledger."""

    relative_path = Path("work/items.jsonl")

    def __init__(self, root: FloatiRoot) -> None:
        self.root = root

    def records(self) -> list[Dict[str, object]]:
        try:
            return read_records_snapshot(
                self.root, self.relative_path, allowed_kinds=WORK_KINDS
            )
        except IntegrityFailure as failure:
            raise IntegrityFailure(
                "consumption_state_unavailable",
                "validated work-ledger consumption state is unavailable",
            ) from failure

    def project(
        self, records: Optional[Sequence[Dict[str, object]]] = None
    ) -> Dict[str, Dict[str, object]]:
        source = self.records() if records is None else records
        return self.project_tail({}, source)

    def project_tail(
        self,
        initial: Dict[str, Dict[str, object]],
        records: Sequence[Dict[str, object]],
    ) -> Dict[str, Dict[str, object]]:
        try:
            states: Dict[str, Dict[str, object]] = copy.deepcopy(initial)
            for record in records:
                if record["kind"] == "work_item":
                    if any(
                        str(dependency) not in states
                        for dependency in record.get("needs", [])
                    ):
                        raise IntegrityFailure(
                            "work_dependency_orphan",
                            "work item dependency must name an earlier item",
                        )
                    states[str(record["id"])] = {
                        "id": record["id"],
                        "title": record["title"],
                        "owner": record["owner"],
                        "state": "open",
                        "holder": None,
                        "authority_subject": None,
                        "authority_epoch": None,
                        "workspace": record.get("workspace"),
                        "needs": list(record.get("needs", [])),
                        "artifact_bindings": list(record["artifact_bindings"]),
                        "last_activity": record["timestamp"],
                    }
                    continue
                item = states.get(str(record["work_item_id"]))
                if item is None:
                    raise IntegrityFailure(
                        "work_transition_orphan",
                        "work transition names an unknown item",
                    )
                action = record["action"]
                if action == "claim":
                    if item["state"] != "open":
                        raise IntegrityFailure(
                            "work_transition_invalid",
                            "durable claim does not follow an open item",
                        )
                    if any(
                        states.get(str(dependency), {}).get("state") != "completed"
                        for dependency in item["needs"]
                    ):
                        raise IntegrityFailure(
                            "work_dependencies_blocked",
                            "durable claim precedes dependency completion",
                        )
                    item["state"] = "claimed"
                    item["holder"] = record["actor"]
                    item["authority_subject"] = record["authority_subject"]
                    item["authority_epoch"] = record["authority_epoch"]
                elif action == "complete":
                    if item["state"] != "claimed" or item["holder"] != record["actor"]:
                        raise IntegrityFailure(
                            "work_transition_invalid",
                            "durable completion does not match its claim",
                        )
                    item["state"] = "completed"
                    for binding in record["artifact_bindings"]:
                        if binding not in item["artifact_bindings"]:
                            item["artifact_bindings"].append(binding)
                item["last_activity"] = record["timestamp"]
            for item in states.values():
                if item["state"] == "claimed":
                    item["readiness"] = "claimed"
                elif item["state"] == "completed":
                    item["readiness"] = "done"
                elif all(
                    states.get(str(dependency), {}).get("state") == "completed"
                    for dependency in item["needs"]
                ):
                    item["readiness"] = "ready"
                else:
                    item["readiness"] = "blocked"
            return states
        except IntegrityFailure as failure:
            if failure.code == "consumption_state_unavailable":
                raise
            raise IntegrityFailure(
                "consumption_state_unavailable",
                "validated work-ledger consumption state is unavailable",
            ) from failure

    def summary(self, node_id: Optional[str] = None) -> Dict[str, object]:
        states = self.project()
        selected = list(states.values())
        if node_id is not None:
            selected = [
                item
                for item in selected
                if (item["state"] == "open" and item["owner"] == node_id)
                or (item["state"] != "open" and item["holder"] == node_id)
            ]
        counts = {
            state: sum(1 for item in selected if item["state"] == state)
            for state in ("open", "claimed", "completed")
        }
        return {
            "coordinate": self.relative_path.as_posix(),
            "state": "work_available" if counts["open"] else "caught_up",
            "counts": counts,
        }

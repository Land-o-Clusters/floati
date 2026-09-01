"""Append-only orchestration work log, distinct from the mail ledger."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from .errors import IntegrityFailure, ProtocolRefusal
from .host_paths import worker_workspace_root
from .consumption import ConsumptionLedger
from .ids import uuid7_hex
from .jsonl import read_records, read_records_snapshot, transact
from .records import validate_record
from .registry import Registry
from .root import FloatiRoot, validate_identifier


WORK_KINDS = {"work_item", "work_transition"}


def _now(value: Optional[datetime]) -> datetime:
    current = datetime.now(timezone.utc) if value is None else value
    if not isinstance(current, datetime) or current.tzinfo is None or current.utcoffset() is None:
        raise ProtocolRefusal("time_invalid", "an aware UTC-compatible datetime is required")
    return current.astimezone(timezone.utc)


def _timestamp(value: datetime) -> str:
    return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


class WorkLog:
    def __init__(self, root: FloatiRoot, registry: Optional[Registry] = None) -> None:
        self.root = root
        self.registry = registry or Registry(root)
        self.consumption = ConsumptionLedger(root)
        self.relative_path = self.consumption.relative_path
        self.path = root.resolve_relative(self.relative_path)

    def add(
        self,
        title: str,
        owner: str,
        artifact_bindings: Sequence[Dict[str, str]],
        *,
        needs: Sequence[str] = (),
        provision_workspace: bool = False,
        now: Optional[datetime] = None,
    ) -> Dict[str, object]:
        self.registry.require_active(owner)
        if not isinstance(provision_workspace, bool):
            raise ProtocolRefusal(
                "workspace_invalid", "workspace provisioning must be explicitly enabled"
            )
        current = _now(now)
        item_id = "work-" + uuid7_hex()
        record: Dict[str, object] = {
            "schema_version": 0,
            "id": item_id,
            "tenant_id": self.root.tenant_id,
            "timestamp": _timestamp(current),
            "kind": "work_item",
            "title": title,
            "owner": owner,
            "artifact_bindings": list(artifact_bindings),
        }
        if needs:
            record["needs"] = list(needs)
        if provision_workspace:
            record["workspace"] = str(worker_workspace_root() / item_id)
        validate_record(record, self.root.tenant_id, frozenset({"work_item"}), integrity=False)

        def decide(records: List[Dict[str, object]]) -> tuple[Dict[str, object], Dict[str, object]]:
            states = self.consumption.project(records)
            for dependency in record.get("needs", []):
                if dependency not in states:
                    raise ProtocolRefusal(
                        "work_dependency_unknown",
                        "each dependency must name an existing earlier work item",
                    )
            return record, record

        return transact(self.root, self.relative_path, decide, allowed_kinds=WORK_KINDS)

    def claim(
        self,
        item_id: str,
        actor: str,
        authority_subject: str,
        authority_epoch: int,
        *,
        now: Optional[datetime] = None,
    ) -> Dict[str, object]:
        self.registry.require_active(actor)
        current = _now(now)
        self.registry.require_protocol_lease(
            actor, now=current, act="work claim by actor"
        )
        subject = validate_identifier(authority_subject, "authority_subject")
        if not isinstance(authority_epoch, int) or isinstance(authority_epoch, bool) or authority_epoch < 1:
            raise ProtocolRefusal("authority_epoch_invalid", "authority epoch must be a positive integer")
        self._require_authority(subject, actor, authority_epoch, current)
        return self._transition(
            item_id,
            "claim",
            actor,
            subject,
            authority_epoch,
            [],
            current,
        )

    def claim_owned_oldest(
        self,
        actor: str,
        authority_subject: str,
        authority_epoch: int,
        *,
        now: Optional[datetime] = None,
    ) -> Dict[str, object]:
        """Atomically select and claim the oldest owned open work item."""

        self.registry.require_active(actor)
        current = _now(now)
        self.registry.require_protocol_lease(
            actor, now=current, act="work claim by actor"
        )
        subject = validate_identifier(authority_subject, "authority_subject")
        if (
            not isinstance(authority_epoch, int)
            or isinstance(authority_epoch, bool)
            or authority_epoch < 1
        ):
            raise ProtocolRefusal(
                "authority_epoch_invalid", "authority epoch must be a positive integer"
            )
        try:
            self._require_authority(subject, actor, authority_epoch, current)
        except IntegrityFailure as failure:
            raise IntegrityFailure(
                "authority_state_unavailable",
                "validated authority state is unavailable",
            ) from failure

        def decide(records: List[Dict[str, object]]) -> tuple[Dict[str, object], Dict[str, object]]:
            states = self.consumption.project(records)
            item = next(
                (
                    candidate
                    for candidate in states.values()
                    if candidate["readiness"] == "ready" and candidate["owner"] == actor
                ),
                None,
            )
            if item is None:
                if any(
                    candidate["readiness"] == "blocked"
                    and candidate["owner"] == actor
                    for candidate in states.values()
                ):
                    raise ProtocolRefusal(
                        "work_dependencies_blocked",
                        "owned work exists but its dependencies are incomplete",
                    )
                raise ProtocolRefusal(
                    "work_owned_open_absent", "no owned open work item is available"
                )
            record = self._transition_record(
                str(item["id"]),
                "claim",
                actor,
                subject,
                authority_epoch,
                [],
                current,
            )
            claimed = dict(item)
            claimed.update(
                {
                    "state": "claimed",
                    "holder": actor,
                    "authority_subject": subject,
                    "authority_epoch": authority_epoch,
                    "last_activity": record["timestamp"],
                }
            )
            return claimed, record

        try:
            return transact(
                self.root, self.relative_path, decide, allowed_kinds=WORK_KINDS
            )
        except IntegrityFailure as failure:
            raise IntegrityFailure(
                "consumption_state_unavailable",
                "validated work-ledger consumption state is unavailable",
            ) from failure

    def complete(
        self,
        item_id: str,
        actor: str,
        artifact_bindings: Sequence[Dict[str, str]],
        *,
        now: Optional[datetime] = None,
    ) -> Dict[str, object]:
        self.registry.require_active(actor)
        current = _now(now)

        def decide(records: List[Dict[str, object]]) -> tuple[Dict[str, object], Dict[str, object]]:
            states = self.consumption.project(records)
            item = states.get(item_id)
            if item is None:
                raise ProtocolRefusal("work_unknown", "work item does not exist")
            if item["state"] != "claimed":
                raise ProtocolRefusal("work_not_claimed", "work item must be claimed before completion")
            if item["holder"] != actor:
                raise ProtocolRefusal("work_holder_mismatch", "only the exact claim holder may complete work")
            record = self._transition_record(
                item_id,
                "complete",
                actor,
                str(item["authority_subject"]),
                int(item["authority_epoch"]),
                artifact_bindings,
                current,
            )
            return record, record

        return transact(self.root, self.relative_path, decide, allowed_kinds=WORK_KINDS)

    def show(self, item_id: Optional[str] = None) -> List[Dict[str, object]]:
        states = self.consumption.project()
        if item_id is None:
            return list(states.values())
        item = states.get(item_id)
        if item is None:
            raise ProtocolRefusal("work_unknown", "work item does not exist")
        return [item]

    def _transition(
        self,
        item_id: str,
        action: str,
        actor: str,
        authority_subject: str,
        authority_epoch: int,
        artifact_bindings: Sequence[Dict[str, str]],
        current: datetime,
    ) -> Dict[str, object]:
        def decide(records: List[Dict[str, object]]) -> tuple[Dict[str, object], Dict[str, object]]:
            states = self.consumption.project(records)
            item = states.get(item_id)
            if item is None:
                raise ProtocolRefusal("work_unknown", "work item does not exist")
            if item["state"] != "open":
                raise ProtocolRefusal("work_not_open", "work item is not open")
            if item["readiness"] != "ready":
                raise ProtocolRefusal(
                    "work_dependencies_blocked",
                    "work item dependencies are incomplete",
                )
            record = self._transition_record(
                item_id,
                action,
                actor,
                authority_subject,
                authority_epoch,
                artifact_bindings,
                current,
            )
            return record, record

        return transact(self.root, self.relative_path, decide, allowed_kinds=WORK_KINDS)

    def _transition_record(
        self,
        item_id: str,
        action: str,
        actor: str,
        authority_subject: str,
        authority_epoch: int,
        artifact_bindings: Sequence[Dict[str, str]],
        current: datetime,
    ) -> Dict[str, object]:
        record: Dict[str, object] = {
            "schema_version": 0,
            "id": "transition-" + uuid7_hex(),
            "tenant_id": self.root.tenant_id,
            "timestamp": _timestamp(current),
            "kind": "work_transition",
            "work_item_id": item_id,
            "action": action,
            "actor": actor,
            "authority_subject": authority_subject,
            "authority_epoch": authority_epoch,
            "artifact_bindings": list(artifact_bindings),
        }
        return validate_record(record, self.root.tenant_id, frozenset({"work_transition"}), integrity=False)

    def _require_authority(
        self,
        subject: str,
        actor: str,
        epoch: int,
        current: datetime,
    ) -> None:
        records = read_records(
            self.root,
            Path("authority-grants") / f"{subject}.jsonl",
            allowed_kinds={"authority_grant"},
        )
        coordinate = f"(holder={actor}, subject={subject}, epoch={epoch})"
        if not records:
            raise ProtocolRefusal(
                "authority_missing",
                f"no authority grant exists for {coordinate}",
            )
        grant = records[-1]
        if grant["state"] != "active":
            raise ProtocolRefusal(
                "authority_inactive",
                f"authority grant {grant['id']} is {grant['state']} for {coordinate}",
            )
        if current >= _parse_time(str(grant["expires_at"])):
            raise ProtocolRefusal(
                "authority_inactive",
                f"authority grant {grant['id']} expired for {coordinate}",
            )
        if grant["holder"] != actor:
            raise ProtocolRefusal(
                "authority_holder_mismatch",
                f"authority grant {grant['id']} does not match {coordinate}",
            )
        if grant["epoch"] != epoch:
            raise ProtocolRefusal(
                "authority_epoch_mismatch",
                f"authority grant {grant['id']} does not match {coordinate}",
            )

    def _project(self, records: Sequence[Dict[str, object]]) -> Dict[str, Dict[str, object]]:
        """Compatibility wrapper for callers that used the old projection helper."""

        return self.consumption.project(records)

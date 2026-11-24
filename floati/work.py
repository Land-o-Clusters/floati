"""Append-only orchestration work log, distinct from the mail ledger."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from .errors import AUTHORITY_SUPERSEDED, IntegrityFailure, ProtocolRefusal
from .host_paths import worker_workspace_root
from .consumption import ConsumptionLedger
from .car_fence import car_acceptance_guard
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
        dispatcher: Optional[str] = None,
        refuse_duplicates: bool = True,
        needs: Sequence[str] = (),
        provision_workspace: bool = False,
        car_binding: Optional[Dict[str, str]] = None,
        adhoc_profile_id: Optional[str] = None,
        now: Optional[datetime] = None,
        notification_findings: Optional[List[Dict[str, object]]] = None,
    ) -> Dict[str, object]:
        """Return the stored item; collect command findings separately if requested."""
        self.registry.require_active(owner)
        if dispatcher is not None:
            self.registry.require_active(dispatcher)
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
        if car_binding is not None:
            record["schema_version"] = 1
            record["car_binding"] = dict(car_binding)
        if adhoc_profile_id is not None:
            if car_binding is not None or provision_workspace:
                raise ProtocolRefusal("adhoc_binding_changed", "borrowed work cannot also own a fresh workspace or car binding")
            from .adhoc_profiles import AdhocProfiles
            profile = AdhocProfiles(self.root).get(adhoc_profile_id)
            if profile["node_id"] != owner:
                raise ProtocolRefusal("work_owner_mismatch", "borrowed work belongs to the exact profile node")
            record.update(schema_version=2, workspace=profile["workspace"],
                adhoc_binding={"profile_id": profile["id"], "lane_opening_id": profile["lane_opening_id"],
                    "node_lease_id": profile["node_lease_id"], "workspace": profile["workspace"]})
        if provision_workspace:
            record["workspace"] = str(worker_workspace_root() / item_id)
        validate_record(record, self.root.tenant_id, frozenset({"work_item"}), integrity=False)

        def decide(records: List[Dict[str, object]]) -> tuple[Dict[str, object], Dict[str, object]]:
            states = self.consumption.project(records)
            if refuse_duplicates:
                for prior in records:
                    if prior["kind"] != "work_item" or states[str(prior["id"])]["state"] != "open":
                        continue
                    keys = ("title", "owner", "artifact_bindings", "needs", "car_binding", "adhoc_binding")
                    matches = all(
                        prior.get(key, [] if key == "needs" else None)
                        == record.get(key, [] if key == "needs" else None)
                        for key in keys
                    )
                    if matches and ("workspace" in prior) == ("workspace" in record):
                        raise ProtocolRefusal(
                            "work_duplicate_open", "an identical open work item already exists"
                        )
            for dependency in record.get("needs", []):
                if dependency not in states:
                    raise ProtocolRefusal(
                        "work_dependency_unknown",
                        "each dependency must name an existing earlier work item",
                    )
            return record, record

        with car_acceptance_guard(self.root):
            # Authoring input precedes the item; a raced duplicate leaves only
            # an inert intent. Ordinary duplicates do not create journals.
            decide(read_records_snapshot(self.root, self.relative_path, allowed_kinds=WORK_KINDS))
            from .work_notification import WorkNotifications, _absence
            journal = WorkNotifications(self.root)
            if dispatcher is not None:
                journal.intent(item_id, dispatcher)
            result = transact(self.root, self.relative_path, decide, allowed_kinds=WORK_KINDS)
        findings = _absence("add", dispatcher, artifact_bindings)
        if dispatcher is not None and artifact_bindings:
            findings.extend(journal.replay(self, item_id, dispatcher, now=current)["findings"])
        if notification_findings is not None:
            notification_findings.extend(findings)
        return result

    def require_claimable(
        self, item_id: str, actor: str, *, now: Optional[datetime] = None,
    ) -> None:
        """Preflight work state before resolving CLI authority defaults.

        Claim still rechecks work state and authority inside its transaction.
        """
        self.registry.require_active(actor)
        self.registry.require_protocol_lease(actor, now=_now(now), act="work claim by actor")
        item = self.show(item_id)[0]
        self._require_claimable(item, actor)

    def claim(
        self,
        item_id: str,
        actor: str,
        authority_subject: str,
        authority_epoch: int,
        *,
        now: Optional[datetime] = None,
        notification_findings: Optional[List[Dict[str, object]]] = None,
    ) -> Dict[str, object]:
        """Return the claim record, keeping notification findings out of its fields."""
        self.registry.require_active(actor)
        current = _now(now)
        self.registry.require_protocol_lease(
            actor, now=current, act="work claim by actor"
        )
        subject = validate_identifier(authority_subject, "authority_subject")
        if not isinstance(authority_epoch, int) or isinstance(authority_epoch, bool) or authority_epoch < 1:
            raise ProtocolRefusal("authority_epoch_invalid", "authority epoch must be a positive integer")
        pending = self._replay(item_id, actor, current)
        result = self._transition(
            item_id,
            "claim",
            actor,
            subject,
            authority_epoch,
            [],
            current,
        )
        if notification_findings is not None:
            notification_findings.extend(pending["findings"])
        return result

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
            if item.get("adhoc_binding") is not None:
                raise ProtocolRefusal("workspace_invalid", "borrowed work requires the exact native readiness path")
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
            if item.get("car_binding") is not None:
                claimed["claim_id"] = record["id"]
            return claimed, record

        try:
            with car_acceptance_guard(self.root):
                snapshot = self.consumption.project(read_records_snapshot(
                    self.root, self.relative_path, allowed_kinds=WORK_KINDS))
                candidate = next((item for item in snapshot.values()
                                  if item["readiness"] == "ready" and item["owner"] == actor), None)
                pending = ({"findings": []} if candidate is None else
                           self._replay(str(candidate["id"]), actor, current, already_guarded=True))
                result = transact(
                    self.root, self.relative_path, decide, allowed_kinds=WORK_KINDS
                )
            return {**result, "findings": pending["findings"]}
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
        consume_ids: Sequence[str] = (),
        session_id: Optional[str] = None,
        notification_findings: Optional[List[Dict[str, object]]] = None,
    ) -> Dict[str, object]:
        """Return the completion record and collect optional command findings beside it."""
        self.registry.require_active(actor)
        current = _now(now)

        def decide(records: List[Dict[str, object]]) -> tuple[Dict[str, object], Dict[str, object]]:
            states = self.consumption.project(records)
            item = states.get(item_id)
            if item is None:
                raise ProtocolRefusal("work_unknown", "work item does not exist")
            if item["state"] == "completed" and item["holder"] == actor:
                from .work_notification import WorkNotifications
                prior = next(r for r in reversed(records) if r["kind"] == "work_transition"
                             and r["work_item_id"] == item_id and r["action"] == "complete")
                if (prior["artifact_bindings"] == list(artifact_bindings)
                        and WorkNotifications(self.root).records(item_id)):
                    return prior, None
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

        with car_acceptance_guard(self.root):
            if consume_ids:
                if session_id is None:
                    raise ProtocolRefusal("consume_session_required", "work complete --consume requires --session", remedy="pass --session naming the chat that acted on these envelopes")
                # Validate the holder and completion payload before authoring
                # any ACTED receipt. The car guard serializes work writers.
                decide(read_records_snapshot(self.root, self.relative_path, allowed_kinds=WORK_KINDS))
                from .message_consumption import MessageConsumption
                MessageConsumption(self.root).consume_many(
                    actor, consume_ids, session_id, work_item_id=item_id, now=current,
                )
            result = transact(self.root, self.relative_path, decide, allowed_kinds=WORK_KINDS)
        from .work_notification import WorkNotifications, _absence
        journal = WorkNotifications(self.root)
        intent = next((r for r in journal.records(item_id) if r["phase"] == "intent"), None)
        findings = _absence("complete", None if intent is None else intent["dispatcher"], artifact_bindings)
        findings.extend(journal.replay(self, item_id, actor, now=current)["findings"])
        if notification_findings is not None:
            notification_findings.extend(findings)
        return result

    def _replay(self, item_id, actor, current, *, already_guarded=False):
        from .work_notification import WorkNotifications
        return WorkNotifications(self.root).replay(self, item_id, actor, now=current, already_guarded=already_guarded)

    def notify(self, item_id, actor, *, now=None):
        self.registry.require_active(actor)
        current = _now(now)
        self.registry.require_protocol_lease(actor, now=current, act="work notify by sender")
        items = self.consumption.project()
        if item_id is not None and item_id not in items:
            raise ProtocolRefusal("work_unknown", "work item does not exist")
        notifications, findings = [], []
        for selected in ([item_id] if item_id is not None else items):
            if item_id is None:
                from .work_notification import WorkNotifications
                pending = WorkNotifications(self.root).plan(self, selected, now=current)[3]
                if not any(n["sender"] == actor for n in pending):
                    continue
            result = self._replay(selected, actor, current)
            notifications.extend(result["notifications"])
            findings.extend(result["findings"])
        return {"notifications": notifications, "findings": findings}

    def show(self, item_id: Optional[str] = None, *, actor: Optional[str] = None,
             now: Optional[datetime] = None) -> List[Dict[str, object]]:
        states = self.consumption.project()
        if item_id is not None and item_id not in states:
            raise ProtocolRefusal("work_unknown", "work item does not exist")
        items = list(states.values()) if item_id is None else [states[item_id]]
        from .work_notification import WorkNotifications
        journal = WorkNotifications(self.root)
        for item in items:
            item["notifications"] = journal.records(str(item["id"]))
        if actor is None:
            return items
        self.registry.require_active(actor)
        current = _now(now)
        try:
            self.registry.require_protocol_lease(actor, now=current, act="work claim by actor")
            lease_code = None
        except ProtocolRefusal as refusal:
            lease_code = refusal.code
        directory = self.root.resolve_relative(Path("authority-grants"))
        grants = []
        if directory.exists():
            for path in sorted(directory.glob("*.jsonl")):
                rows = read_records_snapshot(self.root, Path("authority-grants") / path.name,
                                    allowed_kinds={"authority_grant"})
                if rows:
                    grants.append(rows)
        authorities = [rows[-1] for rows in grants if rows[-1]["holder"] == actor
                       and rows[-1]["state"] == "active"
                       and current < _parse_time(str(rows[-1]["expires_at"]))]
        superseded = []
        for rows in grants:
            earlier = next((row for row in reversed(rows[:-1]) if row["holder"] == actor), None)
            if (earlier is not None and rows[-1]["holder"] != actor
                    and earlier["state"] == "active"
                    and current < _parse_time(str(earlier["expires_at"]))):
                superseded.append(rows[-1])
        for item in items:
            code = lease_code
            if code is None:
                try:
                    self._require_claimable(item, actor)
                except ProtocolRefusal as refusal:
                    code = refusal.code
            if code is None and not authorities:
                code = AUTHORITY_SUPERSEDED if superseded else "authority_missing"
            item["eligibility"] = {
                "eligible": code is None, "code": code,
                "authorities": [{"subject": g["subject_id"], "epoch": g["epoch"]} for g in authorities],
                "findings": [{"code": AUTHORITY_SUPERSEDED, "subject": g["subject_id"],
                              "holder": g["holder"], "epoch": g["epoch"]} for g in superseded],
            }
        return items

    @staticmethod
    def _require_claimable(item: Dict[str, object], actor: str) -> None:
        if item["state"] == "claimed":
            raise ProtocolRefusal("work_already_claimed", "work item already has a claimant")
        if item["state"] != "open":
            raise ProtocolRefusal("work_not_open", "work item is not open")
        if item["readiness"] != "ready":
            raise ProtocolRefusal("work_dependencies_blocked", "work item dependencies are incomplete")
        if (item.get("car_binding") is not None or item.get("adhoc_binding") is not None) and item["owner"] != actor:
            raise ProtocolRefusal("work_owner_mismatch", "a car must be claimed by its exact owner",
                remedy="claim as the recorded owner or request an explicit coordinator reassignment")

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
            self._require_claimable(item, actor)
            self._require_authority(authority_subject, actor, authority_epoch, current)
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

        with car_acceptance_guard(self.root):
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

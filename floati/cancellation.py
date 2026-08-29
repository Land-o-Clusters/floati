"""Scheduler-owned durable cancellation and late-result fencing."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Dict, Mapping, Optional, Sequence

from .errors import ProtocolRefusal
from .approvals import ApprovalLedger, CapabilityLedger
from .ids import uuid7_hex
from .runtruth import RunLedger, RunProjection, _cancel_scope_items
from .scheduler import RunScheduler
from .registry import Registry


class CancelMode(str, Enum):
    native = "native"
    local_process_only = "local_process_only"
    unavailable = "unavailable"


class CancellationCoordinator:
    """Own the cancellation record families and preserve their causal order."""

    def __init__(self, ledger: RunLedger) -> None:
        if not isinstance(ledger, RunLedger):
            raise ProtocolRefusal("run_ledger_required", "cancellation requires the canonical RunLedger")
        self.ledger = ledger
        self.scheduler = RunScheduler(ledger)
        self.__cancellation_capability = ledger._cancellation_capability_for(self)

    @staticmethod
    def _timestamp(now: Optional[object]) -> str:
        if now is None:
            value = datetime.now(timezone.utc)
            return value.strftime("%Y-%m-%dT%H:%M:%S.") + f"{value.microsecond // 1000:03d}Z"
        if isinstance(now, str):
            return now
        if isinstance(now, datetime):
            value = now.astimezone(timezone.utc)
            return value.strftime("%Y-%m-%dT%H:%M:%S.") + f"{value.microsecond // 1000:03d}Z"
        raise ProtocolRefusal("timestamp_invalid", "cancellation now must be UTC timestamp text or datetime")

    def _record(self, kind: str, prefix: str, now: Optional[object], **fields: object) -> Dict[str, object]:
        return {"schema_version": 0, "id": prefix + uuid7_hex(), "tenant_id": self.ledger.root.tenant_id,
                "timestamp": self._timestamp(now), "kind": kind, **fields}

    def _append(self, record: Dict[str, object]) -> Dict[str, object]:
        return self.ledger._append_cancellation(
            record,
            self.__cancellation_capability,
            self._resolve_existing,
        )

    @staticmethod
    def _resolve_existing(
        projection: RunProjection,
        candidate: Dict[str, object],
    ) -> Optional[Dict[str, object]]:
        """Resolve cancellation semantic aliases under the writer lock."""

        if (
            candidate.get("kind") == "cancel_requested"
            and candidate.get("scope") == "exact_items"
        ):
            run = projection.run(str(candidate["run_id"]))
            existing = next((
                row["requested"]
                for row in run["cancellations"].values()
                if row["requested"].get("scope") == "exact_items"
                and row["requested"].get("spawn_group_id")
                == candidate.get("spawn_group_id")
            ), None)
            if existing is None:
                return None
            semantic_fields = (
                "run_id", "scope", "item_id", "item_ids",
                "spawn_group_id", "requested_by",
            )
            if any(
                existing[field] != candidate[field]
                for field in semantic_fields
            ):
                raise ProtocolRefusal(
                    "cancel_scope_divergent",
                    "exact cancellation retry changed its immutable coordinates",
                )
            return existing

        if candidate.get("kind") == "cancel_scope_resolved":
            run = projection.run(str(candidate["run_id"]))
            cancellation = run["cancellations"].get(
                candidate.get("cancel_request_id")
            )
            existing = (
                None if cancellation is None else cancellation.get("resolved")
            )
            if existing is None:
                return None
            semantic_fields = (
                "run_id", "cancel_request_id", "scope", "item_id",
                "item_ids", "attempt_ids",
            )
            if any(
                existing[field] != candidate[field]
                for field in semantic_fields
            ):
                raise ProtocolRefusal(
                    "cancel_scope_divergent",
                    "cancellation resolution retry changed its immutable closure",
                )
            return existing

        if candidate.get("kind") == "spawn_child_cancelled_without_attempt":
            run = projection.run(str(candidate["run_id"]))
            existing = run["spawn_item_outcome_records"].get(
                candidate.get("child_item_id")
            )
            if (
                not isinstance(existing, dict)
                or existing.get("kind")
                != "spawn_child_cancelled_without_attempt"
            ):
                return None
            semantic_fields = set(candidate) - {
                "id", "timestamp", "cancelled_at_testimony",
            }
            if any(
                existing[field] != candidate[field]
                for field in semantic_fields
            ):
                raise ProtocolRefusal(
                    "cancel_transition_invalid",
                    "zero-attempt cancellation changed its semantic coordinates",
                )
            return existing

        if candidate.get("kind") in {
            "cancel_observed", "cancel_signal_sent", "cancel_terminal",
            "cancel_unconfirmed",
        }:
            run = projection.run(str(candidate["run_id"]))
            cancellation = next((
                row for row in run["cancellations"].values()
                if row["resolved"] is not None
                and row["resolved"]["id"]
                == candidate.get("cancel_scope_resolved_id")
            ), None)
            prior = (
                None if cancellation is None
                else cancellation["attempts"].get(candidate.get("attempt_id"))
            )
            if not isinstance(prior, dict):
                return None
            kind = candidate["kind"]
            existing = (
                prior if kind == "cancel_observed"
                else prior.get({
                    "cancel_signal_sent": "signal",
                    "cancel_terminal": "terminal",
                    "cancel_unconfirmed": "unconfirmed",
                }[str(kind)])
            )
            if not isinstance(existing, dict):
                return None
            semantic_fields = set(candidate) - {"id", "timestamp"}
            if any(
                existing[field] != candidate[field]
                for field in semantic_fields
            ):
                raise ProtocolRefusal(
                    "cancel_transition_invalid",
                    "cancellation transition changed its semantic coordinates",
                )
            return existing

        if (
            candidate.get("kind") != "attempt_cancelled_before_start"
        ):
            return None
        run = projection.run(str(candidate["run_id"]))
        state = run["attempts"].get(candidate["attempt_id"])
        if state is None:
            return None
        existing = state.get("terminal")
        if (
            not isinstance(existing, dict)
            or existing.get("kind") != "attempt_cancelled_before_start"
        ):
            return None
        semantic_fields = (
            "run_id", "item_id", "attempt_id", "attempt_opened_id",
            "retry_scheduled_id", "fence_token", "cancel_scope_resolved_id",
            "capability_set_bound_id", "dispatch_decision_id", "reason_code",
        )
        if any(existing[field] != candidate[field] for field in semantic_fields):
            raise ProtocolRefusal(
                "cancel_transition_invalid",
                "scheduled retry cancellation changed its semantic coordinates",
            )
        return existing

    def _v1_record(self, kind: str, prefix: str, now: Optional[object], **fields: object) -> Dict[str, object]:
        record = self._record(kind, prefix, now, **fields)
        record["schema_version"] = 1
        return record

    def request(
        self,
        run_id: str,
        adapters: Mapping[str, object],
        *,
        item_id: Optional[str] = None,
        requested_by: str = "operator",
        now: Optional[object] = None,
    ) -> Dict[str, object]:
        """Resolve and persist a cancellation scope before invoking any adapter action."""
        scope = "run" if item_id is None else "item"
        projection = self.ledger.project()
        run = projection.run(run_id)
        existing = next((
            row for row in run["cancellations"].values()
            if row["requested"]["scope"] == scope
            and row["requested"]["item_id"] == item_id
            and row["requested"]["requested_by"] == requested_by
        ), None)
        if existing is None:
            requested = self._append(self._record("cancel_requested", "cancel-requested-", now,
                run_id=run_id, scope=scope, item_id=item_id, requested_by=requested_by))
            projection = self.ledger.project()
            run = projection.run(run_id)
        else:
            requested = existing["requested"]
            if existing["resolved"] is not None:
                resolved = existing["resolved"]
                self._complete_resolution(run_id, resolved, adapters, now=now)
                self._complete_spawn_cancellation(run_id, resolved, adapters, now=now)
                self._terminalize_confirmed_attempts(run_id, resolved, now=now)
                return resolved
        item_ids = _cancel_scope_items(run, projection.edges(run_id), item_id)
        attempt_ids = sorted(
            attempt_id for attempt_id, state in run["attempts"].items()
            if state["opened"]["item_id"] in item_ids and state["terminal"] is None
        )
        resolved = self._append(self._record("cancel_scope_resolved", "cancel-scope-resolved-", now,
            run_id=run_id, cancel_request_id=requested["id"], scope=scope, item_id=item_id,
            item_ids=item_ids, attempt_ids=attempt_ids))
        self._complete_resolution(run_id, resolved, adapters, now=now)
        self._complete_spawn_cancellation(run_id, resolved, adapters, now=now)
        self._terminalize_confirmed_attempts(run_id, resolved, now=now)
        return resolved

    def request_exact_items(
        self, run_id: str, item_ids: Sequence[str], adapters: Mapping[str, object], *,
        spawn_group_id: str, requested_by: str = "spawn_join",
        now: Optional[object] = None,
    ) -> Dict[str, object]:
        """Persist an exact immutable member set without dependency expansion."""

        exact = sorted(item_ids)
        if not exact or len(exact) != len(set(exact)):
            raise ProtocolRefusal("cancel_scope_invalid", "exact item cancellation must be nonempty and unique")
        run = self.ledger.project().run(run_id)
        for cancellation in run["cancellations"].values():
            request = cancellation["requested"]
            if (
                request.get("scope") == "exact_items"
                and request.get("spawn_group_id") == spawn_group_id
                and request.get("item_ids") == exact
                and request.get("requested_by") == requested_by
            ):
                resolved = cancellation["resolved"]
                if resolved is None:
                    break
                self._complete_resolution(run_id, resolved, adapters, now=now)
                self._terminalize_confirmed_attempts(run_id, resolved, now=now)
                return resolved
        else:
            requested = self._append(self._v1_record(
                "cancel_requested", "cancel-requested-", now,
                run_id=run_id, scope="exact_items", item_id=None,
                item_ids=exact, spawn_group_id=spawn_group_id,
                requested_by=requested_by,
            ))
            run = self.ledger.project().run(run_id)
            attempt_ids = sorted(
                attempt_id for attempt_id, state in run["attempts"].items()
                if state["opened"]["item_id"] in exact and state["terminal"] is None
            )
            resolved = self._append(self._v1_record(
                "cancel_scope_resolved", "cancel-scope-resolved-", now,
                run_id=run_id, cancel_request_id=requested["id"],
                scope="exact_items", item_id=None, item_ids=exact,
                attempt_ids=attempt_ids,
            ))
            self._complete_resolution(run_id, resolved, adapters, now=now)
            self._terminalize_confirmed_attempts(run_id, resolved, now=now)
            return resolved
        requested = cancellation["requested"]
        run = self.ledger.project().run(run_id)
        attempt_ids = sorted(
            attempt_id for attempt_id, state in run["attempts"].items()
            if state["opened"]["item_id"] in exact and state["terminal"] is None
        )
        resolved = self._append(self._v1_record(
            "cancel_scope_resolved", "cancel-scope-resolved-", now,
            run_id=run_id, cancel_request_id=requested["id"],
            scope="exact_items", item_id=None, item_ids=exact,
            attempt_ids=attempt_ids,
        ))
        self._complete_resolution(run_id, resolved, adapters, now=now)
        self._terminalize_confirmed_attempts(run_id, resolved, now=now)
        return resolved

    def _complete_resolution(
        self, run_id: str, resolved: Dict[str, object],
        adapters: Mapping[str, object], *, now: Optional[object],
    ) -> None:
        run = self.ledger.project().run(run_id)
        cancellation = next(
            row for row in run["cancellations"].values()
            if row["resolved"] is not None and row["resolved"]["id"] == resolved["id"]
        )
        for attempt_id in resolved["attempt_ids"]:
            if attempt_id in cancellation["attempts"]:
                continue
            state = run["attempts"][attempt_id]
            opened = state["opened"]
            if state["terminal"] is not None:
                continue
            dispatch = run["dispatches"].get(attempt_id)
            if state["started"] is None:
                self._append(self._v1_record(
                    "attempt_cancelled_before_start", "attempt-cancelled-before-start-", now,
                    run_id=run_id, item_id=opened["item_id"], attempt_id=attempt_id,
                    attempt_opened_id=opened["id"], fence_token=opened["fence_token"],
                    retry_scheduled_id=None,
                    cancel_scope_resolved_id=resolved["id"],
                    capability_set_bound_id=(
                        None if run["capability_sets"].get(attempt_id) is None
                        else run["capability_sets"][attempt_id]["id"]
                    ),
                    dispatch_decision_id=None if dispatch is None else dispatch["id"],
                    reason_code="cancelled_before_start",
                    cancelled_at_testimony=self._timestamp(now),
                ))
                continue
            if dispatch is None:
                raise ProtocolRefusal("attempt_missing", "cancellation requires a dispatched attempt")
            adapter = adapters.get(str(dispatch["chosen_worker"]))
            if adapter is None:
                mode = CancelMode.unavailable
            else:
                mode = self._cancel_mode(adapter)
            common = {"run_id": run_id, "cancel_scope_resolved_id": resolved["id"],
                "item_id": opened["item_id"], "attempt_id": attempt_id,
                "fence_token": opened["fence_token"], "adapter": str(dispatch["chosen_worker"]),
                "cancel_mode": mode.value}
            self._append(self._record("cancel_observed", "cancel-observed-", now, **common))
            if mode is CancelMode.unavailable:
                self._append(self._record("cancel_unconfirmed", "cancel-unconfirmed-", now, **common))
                continue
            try:
                assert adapter is not None
                self._invoke(adapter, mode)
            except Exception:
                self._append(self._record("cancel_unconfirmed", "cancel-unconfirmed-", now, **common))
                raise
            self._append(self._record("cancel_signal_sent", "cancel-signal-sent-", now, **common))
            self._append(self._record("cancel_terminal", "cancel-terminal-", now, **common))

        self._terminalize_confirmed_attempts(run_id, resolved, now=now)

        run = self.ledger.project().run(run_id)
        for item_id in resolved["item_ids"]:
            attempt_ids = run["item_attempt_ids"].get(item_id, [])
            if not attempt_ids:
                continue
            prior = run["attempts"][attempt_ids[-1]]
            terminal = prior.get("terminal")
            schedule = prior.get("schedule")
            if (
                terminal is None
                or terminal.get("retry_disposition") != "scheduled"
            ):
                continue
            reservation = (
                schedule
                if schedule is not None
                else {
                    "id": terminal["retry_record_id"],
                    "next_attempt_id": terminal["next_attempt_id"],
                    "next_ordinal": terminal["next_ordinal"],
                    "scheduler_epoch": terminal["next_scheduler_epoch"],
                    "next_fence_token": terminal["next_fence_token"],
                }
            )
            if reservation["next_attempt_id"] in run["attempts"]:
                continue
            self._append(self._v1_record(
                "attempt_cancelled_before_start", "attempt-cancelled-before-start-", now,
                run_id=run_id, item_id=item_id,
                attempt_id=reservation["next_attempt_id"], attempt_opened_id=None,
                retry_scheduled_id=reservation["id"],
                fence_token=reservation["next_fence_token"],
                cancel_scope_resolved_id=resolved["id"],
                capability_set_bound_id=None, dispatch_decision_id=None,
                reason_code="cancelled_before_start",
                cancelled_at_testimony=self._timestamp(now),
            ))
            run = self.ledger.project().run(run_id)

        run = self.ledger.project().run(run_id)
        for group in run["spawn_groups"].values():
            if group["state"] != "activated":
                continue
            for child_item_id in group["member_item_ids"]:
                if (
                    child_item_id not in resolved["item_ids"]
                    or run["item_attempt_ids"].get(child_item_id)
                    or child_item_id in group["rejections"]
                    or child_item_id in run["spawn_item_outcomes"]
                ):
                    continue
                admission = group["admissions"].get(child_item_id)
                self._append(self._v1_record(
                    "spawn_child_cancelled_without_attempt",
                    "spawn-child-cancelled-without-attempt-", now,
                    run_id=run_id, spawn_group_id=group["created"]["id"],
                    plan_amendment_id=group["amendment"]["id"],
                    child_item_id=child_item_id,
                    child_admitted_id=None if admission is None else admission["id"],
                    cancel_scope_resolved_id=resolved["id"],
                    reason_code="cancelled_without_attempt",
                    cancelled_at_testimony=self._timestamp(now),
                ))

    def _terminalize_confirmed_attempts(
        self, run_id: str, resolved: Dict[str, object], *, now: Optional[object],
    ) -> None:
        """Append ordinary cancelled attempt truth once provider cancellation confirms."""

        run = self.ledger.project().run(run_id)
        cancellation = next((
            row for row in run["cancellations"].values()
            if row["resolved"] is not None
            and row["resolved"]["id"] == resolved["id"]
        ), None)
        if cancellation is None:
            return
        for attempt_id in resolved["attempt_ids"]:
            state = run["attempts"].get(attempt_id)
            transition = cancellation["attempts"].get(attempt_id)
            if (
                state is None or state["started"] is None or state["terminal"] is not None
                or not isinstance(transition, dict)
                or transition.get("terminal") is None
            ):
                continue
            if any(
                group["created"]["parent_attempt_id"] == attempt_id
                and group["state"] in {"pending", "activated"}
                for group in run["spawn_groups"].values()
            ):
                continue
            self.scheduler.terminal_attempt(
                run_id, state["opened"]["item_id"], attempt_id,
                "cancelled", "cancelled", "operator_cancellation", "idempotent",
                now=now,
            )
            run = self.ledger.project().run(run_id)

    def _complete_spawn_cancellation(
        self, run_id: str, resolved: Dict[str, object],
        adapters: Mapping[str, object], *, now: Optional[object],
    ) -> None:
        controller = getattr(self.ledger, "_spawn_group_controller", None)
        if controller is None:
            return
        run = self.ledger.project().run(run_id)
        covered = set(resolved["item_ids"])
        for group_id, group in list(run["spawn_groups"].items()):
            if group["created"]["parent_item_id"] not in covered:
                continue
            if group["state"] == "pending":
                controller.abort_group(
                    run_id, group_id, reason_code="cancellation",
                    cancel_scope_resolved_id=resolved["id"], now=now,
                )
        run = self.ledger.project().run(run_id)
        while True:
            self._terminalize_confirmed_attempts(run_id, resolved, now=now)
            run = self.ledger.project().run(run_id)
            candidates = [
                (group_id, group)
                for group_id, group in run["spawn_groups"].items()
                if group["state"] == "activated"
                and group["created"]["parent_item_id"] in covered
                and set(group["member_item_ids"]) <= covered
            ]
            if not candidates:
                break
            closable = []
            for group_id, group in candidates:
                complete = all(
                    item_id in run["spawn_item_outcomes"]
                    or item_id in run["accepted"]
                    or item_id in group["rejections"]
                    or (
                        run["item_attempt_ids"].get(item_id)
                        and run["attempts"][run["item_attempt_ids"][item_id][-1]]["terminal"]
                        is not None
                    )
                    for item_id in group["member_item_ids"]
                )
                if complete:
                    closable.append(group_id)
            if not closable:
                controller.close_group(
                    run_id, candidates[0][0],
                    cancel_scope_resolved_id=resolved["id"],
                    outcome="cancelled", adapters=dict(adapters), now=now,
                )
            for group_id in closable:
                controller.close_group(
                    run_id, group_id, cancel_scope_resolved_id=resolved["id"],
                    outcome="cancelled", adapters=dict(adapters), now=now,
                )
        self._terminalize_confirmed_attempts(run_id, resolved, now=now)

    @staticmethod
    def _cancel_mode(adapter: object) -> CancelMode:
        mode = getattr(adapter, "cancel_mode", None)
        try:
            return mode if isinstance(mode, CancelMode) else CancelMode(mode)
        except (TypeError, ValueError) as exc:
            raise ProtocolRefusal("cancel_mode_invalid", "adapter must declare one governed cancel mode") from exc

    @staticmethod
    def _invoke(adapter: object, mode: CancelMode) -> None:
        name = "cancel" if mode is CancelMode.native else "cancel_local_process"
        action = getattr(adapter, name, None)
        if not callable(action):
            raise ProtocolRefusal("cancel_action_missing", "adapter lacks its declared cancellation action")
        action()

    def retain_late_receipt(
        self,
        run_id: str,
        item_id: str,
        attempt_id: str,
        worker_receipt_ids: Sequence[str],
        presented_fence_token: str,
        *,
        now: Optional[object] = None,
    ) -> Dict[str, object]:
        """Retain a superseded receipt without allowing a canonical result transition."""
        run = self.ledger.project().run(run_id)
        current = self.scheduler.current_attempt_fence(run_id, item_id, now=now)
        current_attempt_id = current["attempt_id"]
        if current_attempt_id == attempt_id:
            raise ProtocolRefusal("attempt_current", "current attempt receipts use canonical result transitions")
        return self._append(self._record("stale_attempt_evidence", "stale-attempt-evidence-", now,
            run_id=run_id, item_id=item_id, attempt_id=attempt_id,
            worker_receipt_ids=list(worker_receipt_ids), presented_fence_token=presented_fence_token,
            current_attempt_id=current_attempt_id, current_fence_token=current["fence_token"]))

    def adopt_stale_evidence(
        self,
        run_id: str,
        stale_evidence_id: str,
        *,
        operator_id: str,
        authority_subject: object = None,
        authority_epoch: object = None,
        capability_record_id: object = None,
        now: Optional[object] = None,
    ) -> Dict[str, object]:
        """Record the explicit operator decision that admits retained stale evidence."""
        run = self.ledger.project().run(run_id)
        evidence = run["stale_evidence"].get(stale_evidence_id)
        if evidence is None:
            raise ProtocolRefusal("stale_evidence_missing", "operator adoption requires retained stale evidence")
        capability = _authorize_actor(self.ledger, operator_id, "operator", "stale_evidence.adopt",
            authority_subject, authority_epoch, capability_record_id, now)
        current = self.scheduler.current_attempt_fence(run_id, evidence["item_id"], now=now)
        current_attempt_id = current["attempt_id"]
        return self._append(self._record("stale_evidence_adopted", "stale-evidence-adopted-", now,
            run_id=run_id, item_id=evidence["item_id"], stale_evidence_id=stale_evidence_id,
            current_attempt_id=current_attempt_id, current_fence_token=current["fence_token"],
            operator_id=operator_id, authority_subject=authority_subject,
            authority_epoch=authority_epoch, capability_record_id=capability["id"]))

    def bind_harness_session(
        self,
        run_id: str,
        item_id: str,
        attempt_id: str,
        *,
        claim_id: str,
        lease_id: str,
        worker_session_id: str,
        harness_segments: Sequence[Dict[str, object]],
        schema_version: int = 0,
        now: Optional[object] = None,
    ) -> Dict[str, object]:
        """Persist FOC's explicit attempt-to-harness-session join without inference."""
        run = self.ledger.project().run(run_id)
        state = run["attempts"].get(attempt_id)
        if state is None or state["opened"]["item_id"] != item_id:
            raise ProtocolRefusal("attempt_missing", "harness binding requires a matching attempt")
        record = self._record("attempt_harness_session_bound", "attempt-harness-session-bound-", now,
            run_id=run_id, item_id=item_id, attempt_id=attempt_id,
            fence_token=state["opened"]["fence_token"], claim_id=claim_id, lease_id=lease_id,
            worker_session_id=worker_session_id, harness_segments=list(harness_segments))
        record["schema_version"] = schema_version
        return self._append(record)


class FloatiSupervisor:
    """The sole authenticated lifecycle emitter for typed worker orphaning."""

    def __init__(self, ledger: RunLedger) -> None:
        if not isinstance(ledger, RunLedger):
            raise ProtocolRefusal("run_ledger_required", "Floati supervisor requires the canonical RunLedger")
        self.ledger = ledger
        self.__supervisor_capability = ledger._supervisor_capability_for(self)

    def owner_loss(self, *args: object, **kwargs: object) -> Dict[str, object]:
        return self._emit("owner_loss", *args, **kwargs)

    def unregister(self, *args: object, **kwargs: object) -> Dict[str, object]:
        return self._emit("unregister", *args, **kwargs)

    def lease_abandonment(self, *args: object, **kwargs: object) -> Dict[str, object]:
        return self._emit("lease_abandonment", *args, **kwargs)

    def _emit(self, orphan_class: str, run_id: str, item_id: str, attempt_id: str, *,
              claim_id: str, lease_id: str, worker_session_id: str,
              authority_subject: object = None, authority_epoch: object = None,
              capability_record_id: object = None, now: Optional[object] = None) -> Dict[str, object]:
        capability = _authorize_actor(self.ledger, "floati-supervisor", "floatisupervisor", "orphan.emit",
            authority_subject, authority_epoch, capability_record_id, now)
        record = {"schema_version": 0, "id": "supervisor-orphaned-" + uuid7_hex(),
            "tenant_id": self.ledger.root.tenant_id, "timestamp": _timestamp(now), "kind": "supervisor_orphaned",
            "run_id": run_id, "item_id": item_id, "attempt_id": attempt_id,
            "claim_id": claim_id, "lease_id": lease_id, "worker_session_id": worker_session_id,
            "supervisor_id": "floati-supervisor", "orphan_class": orphan_class,
            "authority_subject": authority_subject, "authority_epoch": authority_epoch,
            "capability_record_id": capability["id"]}
        return self.ledger._append_supervisor(record, self.__supervisor_capability)


def _timestamp(now: Optional[object]) -> str:
    return CancellationCoordinator._timestamp(now)


def _authority_time(now: Optional[object]) -> datetime:
    if isinstance(now, datetime):
        if now.tzinfo is None or now.utcoffset() is None:
            raise ProtocolRefusal("time_invalid", "authority requires an aware UTC-compatible datetime")
        return now.astimezone(timezone.utc)
    if isinstance(now, str):
        try:
            value = datetime.fromisoformat(now.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ProtocolRefusal("timestamp_invalid", "authority requires a UTC timestamp") from exc
        if value.tzinfo is None:
            raise ProtocolRefusal("time_invalid", "authority requires an aware UTC-compatible datetime")
        return value.astimezone(timezone.utc)
    raise ProtocolRefusal("operator_authority_required", "operator action requires a supplied authority timestamp")


def _authorize_actor(ledger: RunLedger, actor: str, role: str, capability_name: str,
                     authority_subject: object, authority_epoch: object,
                     capability_record_id: object, now: Optional[object]) -> Dict[str, object]:
    if not isinstance(authority_subject, str) or not isinstance(authority_epoch, int) or isinstance(authority_epoch, bool) or not isinstance(capability_record_id, str):
        raise ProtocolRefusal("operator_authority_required", "operator action requires exact authority and capability evidence")
    current = _authority_time(now)
    entry = Registry(ledger.root).require_active(actor)
    if str(entry["role"]).casefold() != role:
        raise ProtocolRefusal("operator_role_invalid", "actor does not hold the required lifecycle role")
    capability = CapabilityLedger(ledger.root).current(actor, capability_name, now=current)
    if capability["id"] != capability_record_id or capability["status"] != "current" or capability["mode"] != "read_write":
        raise ProtocolRefusal("operator_capability_invalid", "actor lacks the exact active write capability")
    grant = ApprovalLedger(ledger.root)._authority(authority_subject, authority_epoch, current)
    if grant["holder"] != actor:
        raise ProtocolRefusal("authority_holder_mismatch", "authority holder does not match lifecycle actor")
    return capability

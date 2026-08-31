"""Direct approval suspension evaluation over canonical durable evidence."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from typing import Dict, Optional

from .approvals import ApprovalLedger
from .errors import ProtocolRefusal
from .ids import uuid7_hex
from .jsonl import read_records
from .planes import AuthorityGrantStore
from .records import validate_record
from .runtruth import RunLedger, RunProjection, SUSPENSION_KINDS


def _now(value: Optional[datetime]) -> datetime:
    current = datetime.now(timezone.utc) if value is None else value
    if (
        not isinstance(current, datetime)
        or current.tzinfo is None
        or current.utcoffset() is None
    ):
        raise ProtocolRefusal(
            "time_invalid", "an aware UTC-compatible datetime is required"
        )
    return current.astimezone(timezone.utc)


def _timestamp(value: datetime) -> str:
    return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _parse_time(value: object) -> datetime:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(
            timezone.utc
        )
    except (TypeError, ValueError) as exc:
        raise ProtocolRefusal("timestamp_invalid", "durable timestamp is invalid") from exc


class ApprovalSuspensionController:
    """Evaluate suspend/resume requests directly against same-root ledgers."""

    def __init__(
        self, ledger: RunLedger, approvals: ApprovalLedger | None = None
    ) -> None:
        if not isinstance(ledger, RunLedger):
            raise ProtocolRefusal(
                "run_ledger_required", "suspension requires the canonical RunLedger"
            )
        selected = approvals or ApprovalLedger(ledger.root)
        if not isinstance(selected, ApprovalLedger):
            raise ProtocolRefusal(
                "approval_ledger_required",
                "suspension requires the canonical ApprovalLedger",
            )
        if selected.root.tenant_home != ledger.root.tenant_home:
            raise ProtocolRefusal(
                "approval_root_mismatch",
                "approval and run ledgers must share one tenant root",
            )
        self.ledger = ledger
        self.approvals = selected
        self.authorities = AuthorityGrantStore(ledger.root)
        self.__capability = ledger._suspension_capability_for(self)

    def suspend(
        self,
        run_id: str,
        item_id: str,
        attempt_id: str,
        approval_request_id: str,
        *,
        adapter: str,
        resume_mode: str,
        provider_session_or_thread_id: str | None,
        workspace_checkpoint: dict[str, str],
        execution_authority_subject: str,
        execution_authority_holder: str,
        execution_authority_epoch: int,
        now: datetime | None = None,
    ) -> dict[str, object]:
        if self.ledger._sequencer_client is not None:
            return self.ledger._evaluate_suspension_intent(
                "suspension_evaluation",
                {
                    "run_id": run_id,
                    "item_id": item_id,
                    "attempt_id": attempt_id,
                    "approval_request_id": approval_request_id,
                    "adapter": adapter,
                    "resume_mode": resume_mode,
                    "provider_session_or_thread_id": provider_session_or_thread_id,
                    "workspace_checkpoint": deepcopy(workspace_checkpoint),
                    "execution_authority_subject": execution_authority_subject,
                    "execution_authority_holder": execution_authority_holder,
                    "execution_authority_epoch": execution_authority_epoch,
                },
            )
        current = _now(now)
        checkpoint = deepcopy(workspace_checkpoint)
        request = self._approval_request(approval_request_id)
        projection = self.ledger.project()
        run = projection.run(run_id)
        state = run["attempts"].get(attempt_id)
        if state is None or state["opened"]["item_id"] != item_id:
            raise ProtocolRefusal(
                "attempt_missing", "suspension requires the exact run item attempt"
            )

        existing = state["suspension"]
        if existing is not None:
            self._validate_suspend_retry(
                existing,
                request,
                adapter,
                resume_mode,
                provider_session_or_thread_id,
                checkpoint,
                execution_authority_subject,
                execution_authority_holder,
                execution_authority_epoch,
            )
            self._ensure_old_authority_inactive(existing, current)
            return deepcopy(existing)

        if (
            state["started"] is None
            or state["terminal"] is not None
            or state["state"] != "running"
        ):
            raise ProtocolRefusal(
                "attempt_suspension_invalid",
                "suspension requires an exact started running attempt",
            )
        if request["schema_version"] != 1:
            raise ProtocolRefusal(
                "approval_action_binding_required",
                "suspension requires a v1 action-bound approval request",
            )
        if current >= _parse_time(request["expires_at"]):
            raise ProtocolRefusal(
                "approval_request_expired", "approval request has expired"
            )
        self._validate_adapter(adapter, resume_mode, provider_session_or_thread_id)
        authority = self.authorities.exact_tail(execution_authority_subject)
        self._require_active_authority(
            authority,
            execution_authority_holder,
            execution_authority_epoch,
            current,
            prefix="execution_authority",
        )
        opened = state["opened"]
        started = state["started"]
        record: Dict[str, object] = {
            "schema_version": 1,
            "id": "attempt-suspended-approval-" + uuid7_hex(),
            "tenant_id": self.ledger.root.tenant_id,
            "timestamp": _timestamp(current),
            "kind": "attempt_suspended_for_approval",
            "run_id": run_id,
            "item_id": item_id,
            "attempt_id": attempt_id,
            "attempt_started_id": started["id"],
            "fence_token": opened["fence_token"],
            "adapter": adapter,
            "approval_request_id": request["id"],
            "exact_action_digest": request["exact_action_digest"],
            "requested_scope": request["scope"],
            "resume_mode": resume_mode,
            "provider_session_or_thread_id": provider_session_or_thread_id,
            "workspace": f"\x2fprivate/tmp/floati-work/{item_id}",
            "workspace_checkpoint": checkpoint,
            "execution_authority_subject": execution_authority_subject,
            "execution_authority_holder": execution_authority_holder,
            "authority_epoch_at_request": execution_authority_epoch,
            "approval_expiry": request["expires_at"],
        }
        validate_record(
            record,
            self.ledger.root.tenant_id,
            frozenset({"attempt_suspended_for_approval"}),
            integrity=False,
        )
        durable = self.ledger._append_suspension(
            record,
            self.__capability,
            self._resolve_existing,
        )
        self._ensure_old_authority_inactive(durable, current)
        return deepcopy(durable)

    def consume(
        self,
        run_id: str,
        item_id: str,
        attempt_id: str,
        approval_decision_id: str,
        *,
        workspace_checkpoint: dict[str, str],
        resume_authority_subject: str,
        resume_authority_holder: str,
        resume_authority_epoch: int,
        now: datetime | None = None,
    ) -> dict[str, object]:
        if self.ledger._sequencer_client is not None:
            return self.ledger._evaluate_suspension_intent(
                "approval_resume_evaluation",
                {
                    "run_id": run_id,
                    "item_id": item_id,
                    "attempt_id": attempt_id,
                    "approval_decision_id": approval_decision_id,
                    "workspace_checkpoint": deepcopy(workspace_checkpoint),
                    "resume_authority_subject": resume_authority_subject,
                    "resume_authority_holder": resume_authority_holder,
                    "resume_authority_epoch": resume_authority_epoch,
                },
            )
        current = _now(now)
        checkpoint = deepcopy(workspace_checkpoint)
        projection = self.ledger.project()
        run = projection.run(run_id)
        state = run["attempts"].get(attempt_id)
        if state is None or state["opened"]["item_id"] != item_id:
            raise ProtocolRefusal(
                "attempt_missing", "approval consumption requires the exact attempt"
            )
        suspension = state["suspension"]
        if suspension is None:
            raise ProtocolRefusal(
                "approval_suspension_missing", "attempt has no durable suspension"
            )
        existing = state["approval_consumption"]
        if existing is not None:
            self._validate_consume_retry(
                existing,
                approval_decision_id,
                checkpoint,
                resume_authority_subject,
                resume_authority_holder,
                resume_authority_epoch,
            )
            return deepcopy(existing)
        if state["terminal"] is not None or state["state"] != "suspended":
            raise ProtocolRefusal(
                "approval_consumption_terminal",
                "only a live suspended attempt can consume approval",
            )
        if suspension["resume_mode"] == "unsupported":
            raise ProtocolRefusal(
                "approval_resume_unsupported",
                "the suspended adapter does not support continuation",
            )
        if checkpoint != suspension["workspace_checkpoint"]:
            raise ProtocolRefusal(
                "resume_checkpoint_divergent",
                "resume must preserve the exact suspended checkpoint",
            )
        decision = self._approval_decision(approval_decision_id)
        if decision["request_id"] != suspension["approval_request_id"]:
            raise ProtocolRefusal(
                "approval_decision_divergent",
                "decision must name the suspended approval request",
            )
        if _parse_time(decision["decided_at"]) < _parse_time(
            suspension["timestamp"]
        ):
            raise ProtocolRefusal(
                "approval_decision_reordered",
                "decision must follow the durable suspension it authorizes",
            )
        if decision["schema_version"] != 1 or decision.get(
            "exact_action_digest"
        ) != suspension["exact_action_digest"]:
            raise ProtocolRefusal(
                "approval_action_divergent",
                "decision must repeat the suspended exact action",
            )
        if decision["decision"] != "approved":
            raise ProtocolRefusal(
                "approval_not_approved", "resume requires an approved decision"
            )
        if decision.get("granted_scope") != suspension["requested_scope"]:
            raise ProtocolRefusal(
                "approval_scope_divergent",
                "decision must preserve the suspended requested scope",
            )
        expires_at = decision.get("expires_at")
        if expires_at is None or current >= _parse_time(expires_at):
            raise ProtocolRefusal(
                "approval_decision_expired", "approved decision has expired"
            )
        if resume_authority_subject != suspension["execution_authority_subject"]:
            raise ProtocolRefusal(
                "resume_authority_subject_mismatch",
                "resume authority must preserve the suspended subject",
            )
        if (
            not isinstance(resume_authority_epoch, int)
            or isinstance(resume_authority_epoch, bool)
            or resume_authority_epoch <= suspension["authority_epoch_at_request"]
        ):
            raise ProtocolRefusal(
                "resume_authority_not_newer",
                "resume authority epoch must be strictly newer",
            )
        authority = self.authorities.exact_tail(resume_authority_subject)
        self._require_active_authority(
            authority,
            resume_authority_holder,
            resume_authority_epoch,
            current,
            prefix="resume_authority",
        )
        record: Dict[str, object] = {
            "schema_version": 1,
            "id": "approval-consumed-resume-" + uuid7_hex(),
            "tenant_id": self.ledger.root.tenant_id,
            "timestamp": _timestamp(current),
            "kind": "approval_consumed_for_resume",
            "run_id": run_id,
            "item_id": item_id,
            "attempt_id": attempt_id,
            "fence_token": suspension["fence_token"],
            "attempt_suspended_id": suspension["id"],
            "approval_request_id": suspension["approval_request_id"],
            "approval_decision_id": decision["id"],
            "exact_action_digest": suspension["exact_action_digest"],
            "requested_scope": suspension["requested_scope"],
            "resume_mode": suspension["resume_mode"],
            "provider_session_or_thread_id": suspension[
                "provider_session_or_thread_id"
            ],
            "workspace": suspension["workspace"],
            "workspace_checkpoint": checkpoint,
            "resume_authority_subject": resume_authority_subject,
            "resume_authority_holder": resume_authority_holder,
            "resume_authority_epoch": resume_authority_epoch,
            "consumed_at_testimony": _timestamp(current),
        }
        validate_record(
            record,
            self.ledger.root.tenant_id,
            frozenset({"approval_consumed_for_resume"}),
            integrity=False,
        )
        return deepcopy(
            self.ledger._append_suspension(
                record,
                self.__capability,
                self._resolve_existing,
            )
        )

    def _resolve_existing(
        self,
        projection: RunProjection,
        candidate: Dict[str, object],
    ) -> Optional[Dict[str, object]]:
        """Resolve exact semantic retries while the run-writer lock is held."""
        run = projection.run(str(candidate["run_id"]))
        state = run["attempts"].get(candidate["attempt_id"])
        if state is None or state["opened"]["item_id"] != candidate["item_id"]:
            return None
        if candidate["kind"] == "attempt_suspended_for_approval":
            existing = state["suspension"]
            if existing is None:
                return None
            self._validate_suspend_retry(
                existing,
                {
                    "id": candidate["approval_request_id"],
                    "exact_action_digest": candidate["exact_action_digest"],
                    "scope": candidate["requested_scope"],
                },
                str(candidate["adapter"]),
                str(candidate["resume_mode"]),
                candidate["provider_session_or_thread_id"],
                candidate["workspace_checkpoint"],
                str(candidate["execution_authority_subject"]),
                str(candidate["execution_authority_holder"]),
                int(candidate["authority_epoch_at_request"]),
            )
            return deepcopy(existing)
        existing = state["approval_consumption"]
        if existing is None:
            return None
        self._validate_consume_retry(
            existing,
            str(candidate["approval_decision_id"]),
            candidate["workspace_checkpoint"],
            str(candidate["resume_authority_subject"]),
            str(candidate["resume_authority_holder"]),
            int(candidate["resume_authority_epoch"]),
        )
        return deepcopy(existing)

    def _approval_request(self, request_id: str) -> Dict[str, object]:
        requests = read_records(
            self.ledger.root,
            self.approvals.request_path,
            allowed_kinds={"approval_request"},
        )
        request = next((row for row in requests if row["id"] == request_id), None)
        if request is None:
            raise ProtocolRefusal(
                "approval_request_unknown", "approval request does not exist"
            )
        return request

    def _approval_decision(self, decision_id: str) -> Dict[str, object]:
        decisions = read_records(
            self.ledger.root,
            self.approvals.decision_path,
            allowed_kinds={"approval_decision"},
        )
        decision = next((row for row in decisions if row["id"] == decision_id), None)
        if decision is None:
            raise ProtocolRefusal(
                "approval_decision_unknown", "approval decision does not exist"
            )
        return decision

    @staticmethod
    def _validate_adapter(
        adapter: str, resume_mode: str, provider_id: str | None
    ) -> None:
        lawful = (
            adapter == "codex"
            and resume_mode == "checkpoint_restart"
            and provider_id is None
        ) or (
            adapter != "codex"
            and resume_mode == "unsupported"
            and provider_id is None
        )
        if not lawful:
            raise ProtocolRefusal(
                "adapter_resume_mode_invalid",
                "adapter, resume mode, and provider identity are incompatible",
            )

    @staticmethod
    def _require_active_authority(
        authority: Dict[str, object],
        holder: str,
        epoch: int,
        current: datetime,
        *,
        prefix: str,
    ) -> None:
        if authority["epoch"] != epoch:
            raise ProtocolRefusal(
                prefix + "_epoch_mismatch", "authority epoch does not match exact tail"
            )
        if authority["holder"] != holder:
            raise ProtocolRefusal(
                prefix + "_holder_mismatch", "authority holder does not match exact tail"
            )
        if authority["state"] != "active" or current >= _parse_time(
            authority["expires_at"]
        ):
            raise ProtocolRefusal(
                prefix + "_inactive", "authority is not currently active"
            )

    def _validate_suspend_retry(
        self,
        existing: Dict[str, object],
        request: Dict[str, object],
        adapter: str,
        resume_mode: str,
        provider_id: str | None,
        checkpoint: Dict[str, str],
        subject: str,
        holder: str,
        epoch: int,
    ) -> None:
        if request.get("exact_action_digest") != existing["exact_action_digest"]:
            raise ProtocolRefusal(
                "suspension_action_divergent", "retry changed the exact action"
            )
        if request.get("scope") != existing["requested_scope"]:
            raise ProtocolRefusal(
                "suspension_scope_divergent", "retry changed the requested scope"
            )
        if request["id"] != existing["approval_request_id"]:
            raise ProtocolRefusal(
                "suspension_request_divergent", "retry changed approval request"
            )
        if checkpoint != existing["workspace_checkpoint"]:
            raise ProtocolRefusal(
                "suspension_checkpoint_divergent", "retry changed checkpoint"
            )
        if (
            adapter != existing["adapter"]
            or resume_mode != existing["resume_mode"]
            or provider_id != existing["provider_session_or_thread_id"]
        ):
            raise ProtocolRefusal(
                "suspension_adapter_divergent", "retry changed adapter coordinates"
            )
        if (
            subject != existing["execution_authority_subject"]
            or holder != existing["execution_authority_holder"]
            or epoch != existing["authority_epoch_at_request"]
        ):
            raise ProtocolRefusal(
                "suspension_authority_divergent", "retry changed old authority"
            )

    @staticmethod
    def _validate_consume_retry(
        existing: Dict[str, object],
        decision_id: str,
        checkpoint: Dict[str, str],
        subject: str,
        holder: str,
        epoch: int,
    ) -> None:
        if decision_id != existing["approval_decision_id"]:
            raise ProtocolRefusal(
                "approval_consumption_divergent", "retry changed approval decision"
            )
        if checkpoint != existing["workspace_checkpoint"]:
            raise ProtocolRefusal(
                "resume_checkpoint_divergent", "retry changed checkpoint"
            )
        if (
            subject != existing["resume_authority_subject"]
            or holder != existing["resume_authority_holder"]
            or epoch != existing["resume_authority_epoch"]
        ):
            raise ProtocolRefusal(
                "resume_authority_divergent", "retry changed resume authority"
            )

    def _ensure_old_authority_inactive(
        self, suspension: Dict[str, object], current: datetime
    ) -> None:
        subject = str(suspension["execution_authority_subject"])
        holder = str(suspension["execution_authority_holder"])
        epoch = int(suspension["authority_epoch_at_request"])
        latest = self.authorities.exact_tail(subject)
        latest_epoch = int(latest["epoch"])
        if latest_epoch > epoch:
            return
        if latest_epoch != epoch or latest["holder"] != holder:
            raise ProtocolRefusal(
                "authority_history_mismatch",
                "old authority is not the exact current or superseded epoch",
            )
        if latest["state"] in {"released", "expired"}:
            return
        if latest["state"] != "active":
            raise ProtocolRefusal(
                "authority_history_mismatch", "old authority state is invalid"
            )
        if current >= _parse_time(latest["expires_at"]):
            return
        try:
            self._release_authority(subject, holder, epoch, current)
        except ProtocolRefusal as exc:
            if exc.code not in {"authority_released", "authority_expired"}:
                raise
        confirmed = self.authorities.exact_tail(subject)
        if int(confirmed["epoch"]) > epoch:
            return
        if (
            confirmed["epoch"] != epoch
            or confirmed["holder"] != holder
            or confirmed["state"] not in {"released", "expired"}
        ):
            raise ProtocolRefusal(
                "authority_release_unconfirmed",
                "old execution authority remains active",
            )

    def _release_authority(
        self, subject: str, holder: str, epoch: int, current: datetime
    ) -> Dict[str, object]:
        """Narrow injection seam for the post-suspension release operation."""
        return self.authorities.release(subject, holder, epoch, current)


__all__ = ["ApprovalSuspensionController", "SUSPENSION_KINDS"]

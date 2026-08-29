"""Bounded scheduler authority for durable Floati attempts and retries."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, Optional

from .errors import ProtocolRefusal
from .ids import uuid7_hex
from .runtruth import RunLedger, attempt_fence_token, retry_delay_from_backoff


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int
    base_delay_ms: int
    cap_delay_ms: int
    strategy: str = "exponential"
    jitter: str = "sha256_25pct"

    def __post_init__(self) -> None:
        if (not isinstance(self.max_attempts, int) or isinstance(self.max_attempts, bool) or not 1 <= self.max_attempts <= 32 or
                not isinstance(self.base_delay_ms, int) or isinstance(self.base_delay_ms, bool) or not 0 <= self.base_delay_ms <= 86400000 or
                not isinstance(self.cap_delay_ms, int) or isinstance(self.cap_delay_ms, bool) or not 0 <= self.cap_delay_ms <= 86400000 or
                self.base_delay_ms > self.cap_delay_ms or not isinstance(self.strategy, str) or
                self.strategy not in {"fixed", "exponential"} or self.jitter != "sha256_25pct"):
            raise ProtocolRefusal("retry_policy_invalid", "retry policy violates the bounded v0 contract")

    def backoff(self) -> Dict[str, object]:
        return {"strategy": self.strategy, "base_delay_ms": self.base_delay_ms,
                "cap_delay_ms": self.cap_delay_ms, "jitter": self.jitter}


def retry_delay_ms(run_id: str, item_id: str, ordinal: int, policy: RetryPolicy) -> int:
    if not isinstance(ordinal, int) or isinstance(ordinal, bool) or not 1 <= ordinal <= 32:
        raise ProtocolRefusal("ordinal_invalid", "retry ordinal must be one through 32")
    return retry_delay_from_backoff(run_id, item_id, ordinal, policy.backoff())


class RunScheduler:
    def __init__(self, ledger: RunLedger) -> None:
        if not isinstance(ledger, RunLedger):
            raise ProtocolRefusal("run_ledger_required", "scheduler requires the canonical RunLedger")
        self.ledger = ledger
        self.__scheduler_capability = ledger._scheduler_capability_for(self)

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
        raise ProtocolRefusal("timestamp_invalid", "scheduler now must be UTC timestamp text or datetime")

    def _record(self, kind: str, prefix: str, now: Optional[object], **fields: object) -> Dict[str, object]:
        return {"schema_version": 0, "id": prefix + uuid7_hex(), "tenant_id": self.ledger.root.tenant_id,
                "timestamp": self._timestamp(now), "kind": kind, **fields}

    @staticmethod
    def _policy_matches(opened: Dict[str, object], policy: RetryPolicy, scheduler_epoch: int) -> bool:
        return (opened["max_attempts"] == policy.max_attempts and opened["backoff"] == policy.backoff() and
                opened["scheduler_epoch"] == scheduler_epoch)

    def open_attempt(self, run_id: str, item_id: str, policy: RetryPolicy, scheduler_epoch: int, *, now: Optional[object] = None) -> Dict[str, object]:
        if not isinstance(policy, RetryPolicy):
            raise ProtocolRefusal("retry_policy_required", "open attempt requires a validated RetryPolicy")
        self.reconcile(now=now)
        run = self.ledger.project().run(run_id)
        contract = run["contracts"].get(item_id)
        if contract is None:
            raise ProtocolRefusal("task_contract_missing", "a task contract must bind before an item attempt opens")
        governed = contract["contract"].canonical()["retry_policy"]
        backoff = governed["backoff"]
        if (policy.max_attempts != governed["max_attempts"] or policy.base_delay_ms != backoff["base_delay_ms"] or
                policy.cap_delay_ms != backoff["cap_delay_ms"] or policy.strategy != backoff["strategy"] or
                policy.jitter != "sha256_25pct"):
            raise ProtocolRefusal("task_contract_policy_mismatch", "attempt retry policy must equal the frozen task contract")
        attempt_ids = run["item_attempt_ids"].get(item_id, [])
        if not attempt_ids:
            record = self._record("attempt_opened", "attempt-opened-", now, run_id=run_id, item_id=item_id,
                attempt_id="attempt-" + uuid7_hex(), ordinal=1, scheduler_epoch=scheduler_epoch,
                fence_token=attempt_fence_token(run_id, item_id, 1, scheduler_epoch), max_attempts=policy.max_attempts,
                backoff=policy.backoff())
            return self.ledger._append_scheduler(record, self.__scheduler_capability)
        state = run["attempts"][attempt_ids[-1]]
        opened = state["opened"]
        if state["terminal"] is None:
            if self._policy_matches(opened, policy, scheduler_epoch):
                return opened
            raise ProtocolRefusal("attempt_open_input_divergent", "open replay must preserve policy and epoch")
        schedule = state["schedule"]
        if schedule is None:
            raise ProtocolRefusal("attempt_not_available", "item has no reserved retry attempt")
        if not self._policy_matches(opened, policy, scheduler_epoch):
            raise ProtocolRefusal("attempt_open_input_divergent", "retry open must preserve policy and epoch")
        record = self._record("attempt_opened", "attempt-opened-", now, run_id=run_id, item_id=item_id,
            attempt_id=schedule["next_attempt_id"], ordinal=schedule["next_ordinal"], scheduler_epoch=schedule["scheduler_epoch"],
            fence_token=schedule["next_fence_token"], max_attempts=opened["max_attempts"], backoff=opened["backoff"])
        return self.ledger._append_scheduler(record, self.__scheduler_capability)

    def start_attempt(self, run_id: str, item_id: str, attempt_id: str, dispatch_decision_id: str, *, now: Optional[object] = None) -> Dict[str, object]:
        self.reconcile(now=now)
        run = self.ledger.project().run(run_id)
        state = run["attempts"].get(attempt_id)
        if state is None or state["opened"]["item_id"] != item_id:
            raise ProtocolRefusal("attempt_missing", "start requires a matching opened attempt")
        if state["started"] is not None:
            existing = state["started"]
            if existing["dispatch_decision_id"] == dispatch_decision_id:
                return existing
            raise ProtocolRefusal("attempt_start_input_divergent", "start replay changed dispatch decision")
        opened = state["opened"]
        record = self._record("attempt_started", "attempt-started-", now, run_id=run_id, item_id=item_id,
            attempt_id=attempt_id, ordinal=opened["ordinal"], attempt_opened_id=opened["id"],
            dispatch_decision_id=dispatch_decision_id, fence_token=opened["fence_token"])
        return self.ledger._append_scheduler(record, self.__scheduler_capability)

    def terminal_attempt(self, run_id: str, item_id: str, attempt_id: str, terminal_state: str,
                         policy_class: object, reason_code: str, effect_safety: str, *, now: Optional[object] = None) -> Dict[str, object]:
        self.reconcile(now=now)
        run = self.ledger.project().run(run_id)
        state = run["attempts"].get(attempt_id)
        if state is None or state["opened"]["item_id"] != item_id or state["started"] is None:
            raise ProtocolRefusal("attempt_missing", "terminal requires a matching started attempt")
        if state["terminal"] is not None:
            existing = state["terminal"]
            if all(existing[field] == value for field, value in (("terminal_state", terminal_state), ("policy_class", policy_class), ("reason_code", reason_code), ("effect_safety", effect_safety))):
                self.reconcile(now=now)
                return existing
            raise ProtocolRefusal("attempt_terminal_input_divergent", "terminal replay changed semantic inputs")
        opened = state["opened"]
        eligible = (terminal_state, policy_class, reason_code, effect_safety) == ("failed", "transient", "transient_failure", "idempotent")
        closure: Dict[str, object] = {"retry_disposition": "none", "retry_record_id": None, "next_attempt_id": None,
            "next_ordinal": None, "retry_delay_ms": None, "next_scheduler_epoch": None, "next_fence_token": None}
        if eligible and opened["ordinal"] < opened["max_attempts"]:
            ordinal = opened["ordinal"] + 1
            closure = {"retry_disposition": "scheduled", "retry_record_id": "retry-scheduled-" + uuid7_hex(),
                "next_attempt_id": "attempt-" + uuid7_hex(), "next_ordinal": ordinal,
                "retry_delay_ms": retry_delay_from_backoff(run_id, item_id, ordinal, opened["backoff"]),
                "next_scheduler_epoch": opened["scheduler_epoch"],
                "next_fence_token": attempt_fence_token(run_id, item_id, ordinal, opened["scheduler_epoch"])}
        elif eligible:
            closure["retry_disposition"] = "exhausted"
            closure["retry_record_id"] = "retry-exhausted-" + uuid7_hex()
        record = self._record("attempt_terminal", "attempt-terminal-", now, run_id=run_id, item_id=item_id,
            attempt_id=attempt_id, ordinal=opened["ordinal"], attempt_started_id=state["started"]["id"],
            fence_token=opened["fence_token"], terminal_state=terminal_state, policy_class=policy_class,
            reason_code=reason_code, effect_safety=effect_safety, **closure)
        terminal = self.ledger._append_scheduler(record, self.__scheduler_capability)
        self.reconcile(now=now)
        return terminal

    def current_attempt_fence(self, run_id: str, item_id: str, *, now: Optional[object] = None) -> Dict[str, object]:
        """Return the physical-order current attempt and fence for one run item."""
        self.reconcile(run_id, item_id, now=now)
        run = self.ledger.project().run(run_id)
        attempt_ids = run["item_attempt_ids"].get(item_id, [])
        if not attempt_ids:
            raise ProtocolRefusal("attempt_missing", "current fence requires an opened item attempt")
        return run["attempts"][attempt_ids[-1]]["opened"]

    def reconcile(self, run_id: Optional[str] = None, item_id: Optional[str] = None, *, now: Optional[object] = None) -> list[Dict[str, object]]:
        appended = []
        projection = self.ledger.project()
        for candidate_run_id, run in list(projection._runs.items()):
            if run_id is not None and candidate_run_id != run_id:
                continue
            for state in list(run["attempts"].values()):
                opened, terminal = state["opened"], state["terminal"]
                if terminal is None or (item_id is not None and opened["item_id"] != item_id):
                    continue
                if terminal.get("kind") == "attempt_cancelled_before_start":
                    continue
                if (
                    terminal["retry_disposition"] == "scheduled"
                    and state["schedule"] is None
                    and terminal["next_attempt_id"] not in run["attempts"]
                ):
                    record = self._record("retry_scheduled", "retry-scheduled-", now, run_id=candidate_run_id,
                        item_id=opened["item_id"], previous_attempt_id=opened["attempt_id"], attempt_terminal_id=terminal["id"],
                        next_attempt_id=terminal["next_attempt_id"], next_ordinal=terminal["next_ordinal"],
                        delay_ms=terminal["retry_delay_ms"], scheduler_epoch=terminal["next_scheduler_epoch"],
                        next_fence_token=terminal["next_fence_token"])
                    record["id"] = terminal["retry_record_id"]
                    appended.append(self.ledger._append_scheduler(record, self.__scheduler_capability))
                elif terminal["retry_disposition"] == "exhausted" and state["exhaustion"] is None:
                    record = self._record("retry_exhausted", "retry-exhausted-", now, run_id=candidate_run_id,
                        item_id=opened["item_id"], attempt_id=opened["attempt_id"], ordinal=opened["ordinal"],
                        attempt_terminal_id=terminal["id"], max_attempts=opened["max_attempts"], reason_code="max_attempts")
                    record["id"] = terminal["retry_record_id"]
                    appended.append(self.ledger._append_scheduler(record, self.__scheduler_capability))
        return appended

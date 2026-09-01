from __future__ import annotations

from floati import fixture_ids as public_ids

import errno
import json
import os
import socket
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from floati.contracts import TaskContract, contract_digest
from floati.approvals import ApprovalLedger
from floati.errors import DurabilityFailure, IntegrityFailure, ProtocolRefusal
from floati.host_paths import worker_workspace_root
from floati.ids import uuid7_hex
from floati.jsonl import read_records
from floati.planes import AuthorityGrantStore
from floati.records import validate_record
from floati.registry import Registry
from floati.root import FloatiRoot
from floati.runtruth import RunLedger, RunProjection, attempt_fence_token
from floati.scheduler import RetryPolicy, RunScheduler
from floati.sequencer import (
    SequencerClient,
    SequencerConfig,
    SequencerService,
    _encode_frame,
    _semantic_uuid,
)
from tests.schema_validation import SchemaValidationError, validate_json_schema

try:
    from floati.suspension import ApprovalSuspensionController
except ModuleNotFoundError:
    ApprovalSuspensionController = None


NOW = "2026-08-09T12:00:00.000Z"
DIGEST = "a" * 64
DIRECT_NOW = datetime(2026, 8, 9, 12, 0, 0, tzinfo=timezone.utc)
EXPIRED = DIRECT_NOW + timedelta(seconds=302)


class ApprovalSuspensionProjectionTests(unittest.TestCase):
    def record(self, kind: str, prefix: str, **fields: object) -> dict:
        return {
            "schema_version": 0,
            "id": prefix + uuid7_hex(),
            "tenant_id": "alpha",
            "timestamp": NOW,
            "kind": kind,
            **fields,
        }

    def started_attempt(self, *, item_id: str | None = None) -> tuple[list[dict], dict]:
        run_id = "run-" + uuid7_hex()
        item_id = item_id or "work-" + uuid7_hex()
        attempt_id = "attempt-" + uuid7_hex()
        fence_token = attempt_fence_token(run_id, item_id, 1, 7)
        contract = TaskContract.create(
            objective="park one exact approval action",
            non_goals=["no provider relaunch"],
            areas_to_avoid=[{"path": "slip/sequencer.py", "region": "all"}],
            input_hashes={"brief": DIGEST},
            acceptance_checks={"tests.unit": "python3 -m unittest"},
            constraints={"network": "dark"},
            risk_class="high",
            retry_policy={
                "max_attempts": 1,
                "backoff": {
                    "base_delay_ms": 0,
                    "cap_delay_ms": 0,
                    "strategy": "fixed",
                },
            },
            dependencies=[],
        )
        opened = self.record(
            "attempt_opened",
            "attempt-opened-",
            run_id=run_id,
            item_id=item_id,
            attempt_id=attempt_id,
            ordinal=1,
            scheduler_epoch=7,
            fence_token=fence_token,
            max_attempts=1,
            backoff={
                "strategy": "fixed",
                "base_delay_ms": 0,
                "cap_delay_ms": 0,
                "jitter": "sha256_25pct",
            },
        )
        dispatch = self.record(
            "dispatch_decision",
            "run-dispatch-decision-",
            run_id=run_id,
            item_id=item_id,
            attempt_id=attempt_id,
            eligible_workers=["worker-a"],
            chosen_worker="worker-a",
            capability_digest=DIGEST,
            reason_code="policy.route",
            policy_digest=DIGEST,
            routing_rank=0,
            scheduler_epoch=7,
        )
        started = self.record(
            "attempt_started",
            "attempt-started-",
            run_id=run_id,
            item_id=item_id,
            attempt_id=attempt_id,
            ordinal=1,
            attempt_opened_id=opened["id"],
            dispatch_decision_id=dispatch["id"],
            fence_token=fence_token,
        )
        prefix = [
            self.record(
                "run_created",
                "run-created-",
                run_id=run_id,
                plan_digest=DIGEST,
                item_ids=[item_id],
                dependency_edges=[],
            ),
            self.record(
                "task_contract",
                "task-contract-",
                run_id=run_id,
                item_id=item_id,
                **contract.canonical(),
                contract_digest=contract_digest(contract),
            ),
            self.record(
                "run_policy_bound",
                "run-policy-bound-",
                run_id=run_id,
                policy_digest=DIGEST,
            ),
            self.record(
                "worker_pool_bound",
                "run-worker-pool-bound-",
                run_id=run_id,
                worker_ids=["worker-a"],
            ),
            opened,
            dispatch,
            started,
        ]
        return prefix, {
            "run_id": run_id,
            "item_id": item_id,
            "attempt_id": attempt_id,
            "fence_token": fence_token,
            "opened": opened,
            "dispatch": dispatch,
            "started": started,
        }

    def suspension_record(self, state: dict, **changes: object) -> dict:
        record = {
            "schema_version": 1,
            "id": "attempt-suspended-approval-" + uuid7_hex(),
            "tenant_id": "alpha",
            "timestamp": NOW,
            "kind": "attempt_suspended_for_approval",
            "run_id": state["run_id"],
            "item_id": state["item_id"],
            "attempt_id": state["attempt_id"],
            "attempt_started_id": state["started"]["id"],
            "fence_token": state["fence_token"],
            "adapter": "codex",
            "approval_request_id": "approval-request-" + uuid7_hex(),
            "exact_action_digest": "b" * 64,
            "requested_scope": "repo:slipway",
            "resume_mode": "checkpoint_restart",
            "provider_session_or_thread_id": None,
            "workspace": str(worker_workspace_root() / state["item_id"]),
            "workspace_checkpoint": {
                "repo": "owner/slipway",
                "sha": "c" * 40,
                "doc": "docs/checkpoints/approval-park.md",
            },
            "execution_authority_subject": "execute-run",
            "execution_authority_holder": "worker-a",
            "authority_epoch_at_request": 7,
            "approval_expiry": "2026-08-09T12:05:00.000Z",
        }
        return dict(record, **changes)

    def consumption_record(self, state: dict, suspension: dict, **changes: object) -> dict:
        record = {
            "schema_version": 1,
            "id": "approval-consumed-resume-" + uuid7_hex(),
            "tenant_id": "alpha",
            "timestamp": NOW,
            "kind": "approval_consumed_for_resume",
            "run_id": state["run_id"],
            "item_id": state["item_id"],
            "attempt_id": state["attempt_id"],
            "fence_token": state["fence_token"],
            "attempt_suspended_id": suspension["id"],
            "approval_request_id": suspension["approval_request_id"],
            "approval_decision_id": "approval-decision-" + uuid7_hex(),
            "exact_action_digest": suspension["exact_action_digest"],
            "requested_scope": suspension["requested_scope"],
            "resume_mode": suspension["resume_mode"],
            "provider_session_or_thread_id": suspension["provider_session_or_thread_id"],
            "workspace": suspension["workspace"],
            "workspace_checkpoint": suspension["workspace_checkpoint"],
            "resume_authority_subject": suspension["execution_authority_subject"],
            "resume_authority_holder": "worker-b",
            "resume_authority_epoch": suspension["authority_epoch_at_request"] + 1,
            "consumed_at_testimony": "2026-08-09T12:01:00.000Z",
        }
        return dict(record, **changes)

    def worker_receipt(self, state: dict, **changes: object) -> dict:
        record = {
            "schema_version": 0,
            "id": "worker-receipt-" + uuid7_hex(),
            "tenant_id": "alpha",
            "timestamp": NOW,
            "kind": "worker_receipt",
            "session_id": "worker-" + uuid7_hex(),
            "work_item_id": state["item_id"],
            "node_id": "worker-a",
            "adapter": "codex",
            "transition": "claim",
            "outcome_code": None,
            "authority_subject": "execute-run",
            "authority_epoch": 7,
            "artifact_bindings": [],
        }
        return dict(record, **changes)

    def result_record(self, state: dict, receipt: dict) -> dict:
        return self.record(
            "result_produced",
            "run-result-produced-",
            run_id=state["run_id"],
            item_id=state["item_id"],
            attempt_id=state["attempt_id"],
            dispatch_decision_id=state["dispatch"]["id"],
            worker_receipt_ids=[receipt["id"]],
        )

    def denial_terminal(self, state: dict) -> dict:
        return self.record(
            "attempt_terminal",
            "attempt-terminal-",
            run_id=state["run_id"],
            item_id=state["item_id"],
            attempt_id=state["attempt_id"],
            ordinal=1,
            attempt_started_id=state["started"]["id"],
            fence_token=state["fence_token"],
            terminal_state="failed",
            policy_class="operator_required",
            reason_code="approval_denial",
            effect_safety="idempotent",
            retry_disposition="none",
            retry_record_id=None,
            next_attempt_id=None,
            next_ordinal=None,
            retry_delay_ms=None,
            next_scheduler_epoch=None,
            next_fence_token=None,
        )

    def cancelled_attempt_terminal(self, state: dict) -> dict:
        return self.record(
            "attempt_terminal",
            "attempt-terminal-",
            run_id=state["run_id"],
            item_id=state["item_id"],
            attempt_id=state["attempt_id"],
            ordinal=1,
            attempt_started_id=state["started"]["id"],
            fence_token=state["fence_token"],
            terminal_state="cancelled",
            policy_class="cancelled",
            reason_code="operator_cancellation",
            effect_safety="idempotent",
            retry_disposition="none",
            retry_record_id=None,
            next_attempt_id=None,
            next_ordinal=None,
            retry_delay_ms=None,
            next_scheduler_epoch=None,
            next_fence_token=None,
        )

    def cancellation_records(self, state: dict) -> list[dict]:
        requested = self.record(
            "cancel_requested",
            "cancel-requested-",
            run_id=state["run_id"],
            scope="item",
            item_id=state["item_id"],
            requested_by="operator",
        )
        resolved = self.record(
            "cancel_scope_resolved",
            "cancel-scope-resolved-",
            run_id=state["run_id"],
            cancel_request_id=requested["id"],
            scope="item",
            item_id=state["item_id"],
            item_ids=[state["item_id"]],
            attempt_ids=[state["attempt_id"]],
        )
        observed = self.record(
            "cancel_observed",
            "cancel-observed-",
            run_id=state["run_id"],
            cancel_scope_resolved_id=resolved["id"],
            item_id=state["item_id"],
            attempt_id=state["attempt_id"],
            fence_token=state["fence_token"],
            adapter="codex",
            cancel_mode="local_process_only",
        )
        return [requested, resolved, observed]

    def complete_cancellation_records(self, state: dict) -> list[dict]:
        records = self.cancellation_records(state)
        resolved, observed = records[1], records[2]
        signal = self.record(
            "cancel_signal_sent",
            "cancel-signal-sent-",
            run_id=state["run_id"],
            cancel_scope_resolved_id=resolved["id"],
            item_id=state["item_id"],
            attempt_id=state["attempt_id"],
            fence_token=state["fence_token"],
            adapter=observed["adapter"],
            cancel_mode=observed["cancel_mode"],
        )
        terminal = self.record(
            "cancel_terminal",
            "cancel-terminal-",
            run_id=state["run_id"],
            cancel_scope_resolved_id=resolved["id"],
            item_id=state["item_id"],
            attempt_id=state["attempt_id"],
            fence_token=state["fence_token"],
            adapter=observed["adapter"],
            cancel_mode=observed["cancel_mode"],
        )
        return [*records, signal, terminal]

    def replay(self, records: list[dict], receipts: tuple[dict, ...] = ()) -> RunProjection:
        return RunProjection.from_records(records, receipts, integrity=True)

    def replay_floati_workspace(
        self,
        records: list[dict],
        receipts: tuple[dict, ...] = (),
    ) -> RunProjection:
        positive_workspaces = [
            record
            for record in records
            if isinstance(record.get("item_id"), str)
            and record.get("workspace")
            == str(worker_workspace_root() / record["item_id"])
        ]
        self.assertTrue(
            positive_workspaces,
            "Floati workspace RED helper requires an exact positive new-root fixture",
        )
        try:
            return self.replay(records, receipts)
        except IntegrityFailure as exc:
            if (
                exc.code == "workspace_invalid"
                and exc.detail
                == "workspace must use the closed orchestrator reservation path"
            ):
                self.fail(
                    "Floati approval-suspension workspace must be accepted before replay"
                )
            raise

    def test_started_attempt_suspends_and_reserves_exact_workspace(self) -> None:
        """Catches a suspension that does not become projected canonical state."""
        prefix, state = self.started_attempt()
        suspension = self.suspension_record(state)
        projected = self.replay_floati_workspace(prefix + [suspension]).run(state["run_id"])["attempts"][state["attempt_id"]]
        self.assertEqual("suspended", projected["state"])
        self.assertEqual(suspension, projected["suspension"])
        self.assertIsNone(projected["approval_consumption"])

    def test_suspension_refuses_result_but_keeps_cancellation_and_denial_terminal_legal(self) -> None:
        """Catches a suspended fence producing results or losing lawful closure paths."""
        positive_prefix, positive_state = self.started_attempt()
        positive_receipt = self.worker_receipt(positive_state)
        positive = self.result_record(positive_state, positive_receipt)
        self.replay(positive_prefix + [positive], (positive_receipt,))
        self.assertEqual("result_produced", positive["kind"])

        prefix, state = self.started_attempt()
        suspension = self.suspension_record(state)
        receipt = self.worker_receipt(state)
        result = self.result_record(state, receipt)
        with self.assertRaises(IntegrityFailure) as caught:
            self.replay(prefix + [suspension, result], (receipt,))
        self.assertEqual("attempt_suspended", caught.exception.code)

        cancellation = self.cancellation_records(state)
        cancelled = self.replay_floati_workspace(prefix + [suspension, *cancellation]).run(state["run_id"])
        self.assertEqual("cancel_observed", cancellation[-1]["kind"])
        self.assertEqual(cancellation[-1], cancelled["cancellations"][cancellation[0]["id"]]["attempts"][state["attempt_id"]])

        terminal = self.denial_terminal(state)
        projected = self.replay_floati_workspace(prefix + [suspension, terminal]).run(state["run_id"])["attempts"][state["attempt_id"]]
        self.assertEqual("terminal", projected["state"])
        self.assertEqual(terminal, projected["terminal"])

    def test_consumption_requires_exact_suspension_and_reopens_same_fence(self) -> None:
        """Catches consumption that opens a new attempt or changes the suspended fence."""
        prefix, state = self.started_attempt()
        suspension = self.suspension_record(state)
        consumed = self.consumption_record(state, suspension)
        projected = self.replay_floati_workspace(prefix + [suspension, consumed]).run(state["run_id"])["attempts"][state["attempt_id"]]
        self.assertEqual("resumed", projected["state"])
        self.assertEqual(consumed, projected["approval_consumption"])
        self.assertEqual(state["fence_token"], projected["opened"]["fence_token"])

    def test_resumed_result_accepts_receipt_from_exact_consumed_authority(self) -> None:
        """Proves resumed result and acceptance evidence use the consumed authority."""
        prefix, state = self.started_attempt()
        suspension = self.suspension_record(state)
        consumed = self.consumption_record(
            state, suspension, resume_authority_holder="worker-a"
        )
        receipt = self.worker_receipt(
            state,
            timestamp="2026-08-09T12:01:00.000Z",
            authority_subject=consumed["resume_authority_subject"],
            authority_epoch=consumed["resume_authority_epoch"],
        )
        produced = self.result_record(state, receipt)
        verified = self.record(
            "result_verified",
            "run-result-verified-",
            run_id=state["run_id"],
            item_id=state["item_id"],
            attempt_id=state["attempt_id"],
            result_produced_id=produced["id"],
            worker_receipt_ids=[receipt["id"]],
        )
        contract = next(row for row in prefix if row["kind"] == "task_contract")
        acceptance = self.record(
            "acceptance_receipt",
            "acceptance-receipt-",
            run_id=state["run_id"],
            item_id=state["item_id"],
            attempt_id=state["attempt_id"],
            contract_digest=contract["contract_digest"],
            check_ids=["tests.unit"],
            reviewer="reviewer-a",
            evidence_bindings=[receipt["id"]],
            deviations=[],
            result="accepted",
        )
        accepted = self.record(
            "result_accepted",
            "run-result-accepted-",
            run_id=state["run_id"],
            item_id=state["item_id"],
            attempt_id=state["attempt_id"],
            predecessor_result_id=verified["id"],
            acceptance_mode="verified",
            acceptance_receipt_id=acceptance["id"],
            worker_receipt_ids=[receipt["id"]],
        )

        projected = self.replay_floati_workspace(
            prefix + [suspension, consumed, produced, verified, acceptance, accepted],
            (receipt,),
        ).run(state["run_id"])
        self.assertEqual(accepted, projected["accepted"][state["item_id"]])

    def test_resumed_result_refuses_pre_suspension_authority_receipts(self) -> None:
        """Catches old-epoch receipts authorizing any resumed result evidence."""
        prefix, state = self.started_attempt()
        suspension = self.suspension_record(state)
        consumed = self.consumption_record(
            state, suspension, resume_authority_holder="worker-a"
        )
        current_receipt = self.worker_receipt(
            state,
            timestamp="2026-08-09T12:01:00.000Z",
            authority_subject=consumed["resume_authority_subject"],
            authority_epoch=consumed["resume_authority_epoch"],
        )
        old_receipt = self.worker_receipt(
            state,
            timestamp="2026-08-09T11:59:59.000Z",
            authority_subject=suspension["execution_authority_subject"],
            authority_epoch=suspension["authority_epoch_at_request"],
        )
        with self.assertRaises(IntegrityFailure) as result_refusal:
            self.replay(
                prefix + [suspension, consumed, self.result_record(state, old_receipt)],
                (old_receipt,),
            )
        self.assertEqual("worker_receipt_invalid", result_refusal.exception.code)

        produced = self.result_record(state, current_receipt)
        verified = self.record(
            "result_verified",
            "run-result-verified-",
            run_id=state["run_id"],
            item_id=state["item_id"],
            attempt_id=state["attempt_id"],
            result_produced_id=produced["id"],
            worker_receipt_ids=[current_receipt["id"]],
        )
        contract = next(row for row in prefix if row["kind"] == "task_contract")
        stale_acceptance = self.record(
            "acceptance_receipt",
            "acceptance-receipt-",
            run_id=state["run_id"],
            item_id=state["item_id"],
            attempt_id=state["attempt_id"],
            contract_digest=contract["contract_digest"],
            check_ids=["tests.unit"],
            reviewer="reviewer-a",
            evidence_bindings=[old_receipt["id"]],
            deviations=[],
            result="accepted",
        )
        with self.assertRaises(IntegrityFailure) as acceptance_refusal:
            self.replay(
                prefix
                + [suspension, consumed, produced, verified, stale_acceptance],
                (current_receipt, old_receipt),
            )
        self.assertEqual(
            "acceptance_receipt_invalid", acceptance_refusal.exception.code
        )

    def test_terminal_releases_workspace_reservation_for_a_later_run(self) -> None:
        """Catches terminal closure leaking the suspension's logical workspace reservation."""
        prefix, state = self.started_attempt()
        suspension = self.suspension_record(state)
        terminal = self.denial_terminal(state)
        later_prefix, later_state = self.started_attempt(item_id=state["item_id"])
        later_suspension = self.suspension_record(later_state)
        projection = self.replay_floati_workspace(
            prefix + [suspension, terminal] + later_prefix + [later_suspension]
        )
        self.assertEqual(
            "suspended",
            projection.run(later_state["run_id"])["attempts"]
            [later_state["attempt_id"]]["state"],
        )

    def test_complete_cancellation_terminalizes_suspension_and_releases_workspace(self) -> None:
        """Catches a completed cancellation chain being unable to close its suspended attempt."""
        ordinary_prefix, ordinary_state = self.started_attempt()
        ordinary_cancellation = self.complete_cancellation_records(ordinary_state)
        ordinary_terminal = self.cancelled_attempt_terminal(ordinary_state)
        ordinary = self.replay(
            ordinary_prefix + [*ordinary_cancellation, ordinary_terminal]
        ).run(ordinary_state["run_id"])["attempts"][ordinary_state["attempt_id"]]
        self.assertEqual(("terminal", "cancelled"), (ordinary["state"], ordinary["terminal"]["terminal_state"]))

        denial_prefix, denial_state = self.started_attempt()
        denial_suspension = self.suspension_record(denial_state)
        denial = self.replay_floati_workspace(
            denial_prefix + [denial_suspension, self.denial_terminal(denial_state)]
        ).run(denial_state["run_id"])["attempts"][denial_state["attempt_id"]]
        self.assertEqual(("terminal", "approval_denial"), (denial["state"], denial["terminal"]["reason_code"]))

        prefix, state = self.started_attempt()
        suspension = self.suspension_record(state)
        cancellation = self.complete_cancellation_records(state)
        cancelled_terminal = self.cancelled_attempt_terminal(state)
        cancelled_only = self.replay_floati_workspace(prefix + [suspension, *cancellation])
        suspended = cancelled_only.run(state["run_id"])["attempts"][state["attempt_id"]]
        self.assertEqual(("suspended", None), (suspended["state"], suspended["terminal"]))

        later_prefix, later_state = self.started_attempt(item_id=state["item_id"])
        later_suspension = self.suspension_record(later_state)
        with self.assertRaises(IntegrityFailure) as still_reserved:
            self.replay(
                prefix
                + [suspension, *cancellation]
                + later_prefix
                + [later_suspension]
            )
        self.assertEqual("workspace_reserved", still_reserved.exception.code)

        projection = self.replay_floati_workspace(
            prefix
            + [suspension, *cancellation, cancelled_terminal]
            + later_prefix
            + [later_suspension]
        )
        terminal = projection.run(state["run_id"])["attempts"][state["attempt_id"]]
        self.assertEqual(("terminal", "cancelled"), (terminal["state"], terminal["terminal"]["terminal_state"]))
        self.assertEqual(
            "suspended",
            projection.run(later_state["run_id"])["attempts"][later_state["attempt_id"]]["state"],
        )

    def test_public_append_refuses_private_suspension_records(self) -> None:
        """Catches the Task 2 record inventory becoming public raw append authority."""
        _, state = self.started_attempt()
        suspension = self.suspension_record(state)
        with tempfile.TemporaryDirectory() as directory:
            ledger = RunLedger(FloatiRoot.open(Path(directory), "alpha"))
            with self.assertRaises(ProtocolRefusal) as caught:
                ledger.append(suspension)
            self.assertEqual("suspension_controller_only", caught.exception.code)
            self.assertEqual([], ledger.records())

    def test_replay_rejects_duplicate_workspace_double_consumption_and_causal_reorder(self) -> None:
        """Catches hostile physical prefixes manufacturing or duplicating resume authority."""
        prefix, state = self.started_attempt()
        suspension = self.suspension_record(state)
        consumed = self.consumption_record(state, suspension)
        second_consumed = self.consumption_record(state, suspension)
        second_prefix, second_state = self.started_attempt(item_id=state["item_id"])
        second_suspension = self.suspension_record(second_state)
        duplicate_suspension = self.suspension_record(state)
        terminal = self.denial_terminal(state)
        cases = (
            (prefix + [consumed], "approval_suspension_missing"),
            (prefix + [suspension, consumed, second_consumed], "approval_consumption_duplicate"),
            (prefix + second_prefix + [suspension, second_suspension], "workspace_reserved"),
            (prefix + [suspension, duplicate_suspension], "attempt_suspension_duplicate"),
            (prefix + [suspension, terminal, consumed], "approval_consumption_terminal"),
        )
        for records, code in cases:
            with self.subTest(code=code), self.assertRaises(IntegrityFailure) as caught:
                self.replay(records)
            self.assertEqual(code, caught.exception.code)

    def test_consumption_repeats_every_suspension_coordinate_and_uses_newer_same_subject_authority(self) -> None:
        """Catches semantic drift or stale authority in a resume consumption record."""
        prefix, state = self.started_attempt()
        suspension = self.suspension_record(state)
        positive = self.consumption_record(state, suspension)
        self.replay_floati_workspace(prefix + [suspension, positive])

        mutations = (
            ("attempt_suspended_id", "attempt-suspended-approval-" + uuid7_hex()),
            ("approval_request_id", "approval-request-" + uuid7_hex()),
            ("exact_action_digest", "d" * 64),
            ("requested_scope", "repo:other"),
            ("resume_mode", "unsupported"),
            ("workspace", str(worker_workspace_root() / f"work-{uuid7_hex()}")),
            ("workspace_checkpoint", {"repo": "owner/slipway", "sha": "d" * 40, "doc": "other.md"}),
            ("resume_authority_subject", "other-subject"),
            ("resume_authority_epoch", suspension["authority_epoch_at_request"]),
        )
        for field, value in mutations:
            divergent = self.consumption_record(state, suspension, **{field: value})
            with self.subTest(field=field), self.assertRaises(IntegrityFailure) as caught:
                self.replay(prefix + [suspension, divergent])
            self.assertEqual("approval_consumption_invalid", caught.exception.code)

    def test_suspension_runtime_and_schema_contracts_match(self) -> None:
        """Catches open fields, weak scalar bounds, or schema/runtime disagreement."""
        _, state = self.started_attempt()
        suspension = self.suspension_record(state)
        consumed = self.consumption_record(state, suspension)
        paths = {
            "attempt_suspended_for_approval": "schemas/v1/attempt-suspended-for-approval-record.schema.json",
            "approval_consumed_for_resume": "schemas/v1/approval-consumed-for-resume-record.schema.json",
        }

        def accepted(record: dict) -> tuple[bool, bool]:
            try:
                validate_record(dict(record), "alpha", frozenset({record["kind"]}), integrity=False)
            except ProtocolRefusal:
                runtime = False
            else:
                runtime = True
            try:
                validate_json_schema(record, paths[record["kind"]])
            except (SchemaValidationError, FileNotFoundError):
                schema = False
            else:
                schema = True
            return runtime, schema

        for record in (suspension, consumed):
            with self.subTest(positive=record["kind"]):
                self.assertEqual((True, True), accepted(record))
            retired = dict(
                record,
                workspace=f"\x2fprivate/tmp/slipway-work/{state['item_id']}",
            )
            with self.subTest(retired_root=record["kind"]):
                self.assertEqual((False, False), accepted(retired))

        hostile = (
            dict(suspension, schema_version=0),
            dict(suspension, id="attempt-suspended-approval-bad"),
            dict(suspension, exact_action_digest="A" * 64),
            dict(suspension, requested_scope="repo:slipway\n"),
            dict(suspension, authority_epoch_at_request=True),
            dict(suspension, workspace=str(worker_workspace_root() / "not-a-work-id")),
            dict(suspension, workspace_checkpoint={"repo": "owner/slipway", "sha": "c" * 39, "doc": "x"}),
            dict(suspension, resume_mode="native", provider_session_or_thread_id=None),
            dict(suspension, extra=True),
            {key: value for key, value in suspension.items() if key != "approval_expiry"},
            dict(consumed, resume_authority_epoch=7.5),
            dict(consumed, consumed_at_testimony="2026-08-09 12:01:00Z"),
            dict(consumed, approval_decision_id="approval-decision-bad"),
            dict(consumed, extra=True),
        )
        for record in hostile:
            with self.subTest(hostile=record):
                self.assertEqual((False, False), accepted(record))

        for record, field in (
            (suspension, "schema_version"),
            (suspension, "authority_epoch_at_request"),
            (consumed, "schema_version"),
            (consumed, "resume_authority_epoch"),
        ):
            integral = dict(record, **{field: float(record[field])})
            with self.subTest(integral=field, kind=record["kind"]):
                self.assertEqual((True, True), accepted(integral))

    def test_adapter_resume_matrix_matches_runtime_and_schema(self) -> None:
        """Catches native or adapter-incompatible resume modes entering durable truth."""
        _, state = self.started_attempt()
        codex = self.suspension_record(state)
        claude = self.suspension_record(
            state,
            adapter="claude",
            resume_mode="unsupported",
            provider_session_or_thread_id=None,
        )
        unknown = self.suspension_record(
            state,
            adapter="future-adapter",
            resume_mode="unsupported",
            provider_session_or_thread_id=None,
        )
        consumed_codex = self.consumption_record(state, codex)
        consumed_unsupported = self.consumption_record(state, claude)
        paths = {
            "attempt_suspended_for_approval": "schemas/v1/attempt-suspended-for-approval-record.schema.json",
            "approval_consumed_for_resume": "schemas/v1/approval-consumed-for-resume-record.schema.json",
        }

        def accepted(record: dict) -> tuple[bool, bool]:
            try:
                validate_record(dict(record), "alpha", frozenset({record["kind"]}), integrity=False)
            except ProtocolRefusal:
                runtime = False
            else:
                runtime = True
            try:
                validate_json_schema(record, paths[record["kind"]])
            except SchemaValidationError:
                schema = False
            else:
                schema = True
            return runtime, schema

        for record in (codex, claude, unknown, consumed_codex, consumed_unsupported):
            with self.subTest(positive=(record.get("adapter"), record["resume_mode"])):
                self.assertEqual((True, True), accepted(record))

        hostile = (
            dict(codex, resume_mode="unsupported"),
            dict(codex, resume_mode="native", provider_session_or_thread_id="thread-123"),
            dict(claude, resume_mode="checkpoint_restart"),
            dict(claude, resume_mode="native", provider_session_or_thread_id="thread-123"),
            dict(unknown, resume_mode="checkpoint_restart"),
            dict(unknown, resume_mode="native", provider_session_or_thread_id="thread-123"),
            dict(codex, provider_session_or_thread_id="thread-123"),
            dict(consumed_codex, resume_mode="native", provider_session_or_thread_id="thread-123"),
            dict(consumed_codex, provider_session_or_thread_id="thread-123"),
            dict(consumed_unsupported, provider_session_or_thread_id="thread-123"),
        )
        for record in hostile:
            with self.subTest(hostile=(record.get("adapter"), record["resume_mode"], record["provider_session_or_thread_id"])):
                self.assertEqual((False, False), accepted(record))


class _DirectSuspensionContext:
    """Real-ledger fixture for controller behavior, restart, and authority proofs."""

    def __init__(
        self,
        base: Path,
        *,
        adapter: str = "codex",
        resume_mode: str = "checkpoint_restart",
        fail_release_once: bool = False,
    ) -> None:
        self.home = base / "fleet"
        self.root = FloatiRoot.open_direct_home(self.home, create=True)
        registry = Registry(self.root)
        registry.register(public_ids.worker('alpha'), "Codex")
        registry.register(public_ids.reviewer(), "Claude")
        self.authorities = AuthorityGrantStore(self.root)
        self.approval_authority = self.authorities.claim(
            "approve-build", public_ids.reviewer(), 600, 600, DIRECT_NOW
        )
        self.execution_authority = self.authorities.claim(
            "execute-run", "worker-a", 600, 600, DIRECT_NOW
        )
        self.approvals = ApprovalLedger(self.root)
        self.request = self.approvals.request_for_action(
            public_ids.worker('alpha'),
            "workspace.patch",
            "repo:slipway",
            300,
            "b" * 64,
            "approve-build",
            int(self.approval_authority["epoch"]),
            now=DIRECT_NOW + timedelta(seconds=1),
        )
        self.changed_action_request = self.approvals.request_for_action(
            public_ids.worker('alpha'),
            "workspace.patch",
            "repo:slipway",
            300,
            "c" * 64,
            "approve-build",
            int(self.approval_authority["epoch"]),
            now=DIRECT_NOW + timedelta(seconds=1),
        )
        self.changed_scope_request = self.approvals.request_for_action(
            public_ids.worker('alpha'),
            "workspace.patch",
            "repo:other",
            300,
            "b" * 64,
            "approve-build",
            int(self.approval_authority["epoch"]),
            now=DIRECT_NOW + timedelta(seconds=1),
        )
        self.ledger = RunLedger(self.root)
        self.run_id = "run-" + uuid7_hex()
        self.item_id = "work-" + uuid7_hex()
        self._seed_started_attempt()
        self.checkpoint = {
            "repo": "owner/slipway",
            "sha": "d" * 40,
            "doc": "docs/checkpoints/approval-park.md",
        }
        self.suspend_args = {
            "run_id": self.run_id,
            "item_id": self.item_id,
            "attempt_id": self.attempt_id,
            "approval_request_id": self.request["id"],
            "adapter": adapter,
            "resume_mode": resume_mode,
            "provider_session_or_thread_id": None,
            "workspace_checkpoint": dict(self.checkpoint),
            "execution_authority_subject": "execute-run",
            "execution_authority_holder": "worker-a",
            "execution_authority_epoch": int(self.execution_authority["epoch"]),
            "now": DIRECT_NOW + timedelta(seconds=2),
        }
        self.controller = ApprovalSuspensionController(self.ledger, self.approvals)
        if fail_release_once:
            calls = 0
            real_release = self.controller._release_authority

            def fail_once(*args: object, **kwargs: object) -> dict[str, object]:
                nonlocal calls
                calls += 1
                if calls == 1:
                    raise DurabilityFailure(
                        "injected_post_fsync_failure",
                        "suspension is durable before authority release",
                    )
                return real_release(*args, **kwargs)

            self.controller._release_authority = fail_once

    def _record(self, kind: str, prefix: str, **fields: object) -> dict[str, object]:
        return {
            "schema_version": 0,
            "id": prefix + uuid7_hex(),
            "tenant_id": self.root.tenant_id,
            "timestamp": NOW,
            "kind": kind,
            **fields,
        }

    def _seed_started_attempt(self) -> None:
        policy = RetryPolicy(1, 0, 0, strategy="fixed")
        contract = TaskContract.create(
            objective="park one exact approval action",
            non_goals=["no provider relaunch"],
            areas_to_avoid=[{"path": "slip/sequencer.py", "region": "all"}],
            input_hashes={"brief": DIGEST},
            acceptance_checks={"tests.unit": "python3 -m unittest"},
            constraints={"network": "dark"},
            risk_class="high",
            retry_policy={
                "max_attempts": 1,
                "backoff": {
                    "base_delay_ms": 0,
                    "cap_delay_ms": 0,
                    "strategy": "fixed",
                },
            },
            dependencies=[],
        )
        self.ledger.append(
            self._record(
                "run_created",
                "run-created-",
                run_id=self.run_id,
                plan_digest=DIGEST,
                item_ids=[self.item_id],
                dependency_edges=[],
            )
        )
        self.ledger.append(
            self._record(
                "task_contract",
                "task-contract-",
                run_id=self.run_id,
                item_id=self.item_id,
                **contract.canonical(),
                contract_digest=contract_digest(contract),
            )
        )
        self.ledger.append(
            self._record(
                "run_policy_bound",
                "run-policy-bound-",
                run_id=self.run_id,
                policy_digest=DIGEST,
            )
        )
        self.ledger.append(
            self._record(
                "worker_pool_bound",
                "run-worker-pool-bound-",
                run_id=self.run_id,
                worker_ids=["worker-a"],
            )
        )
        scheduler = RunScheduler(self.ledger)
        opened = scheduler.open_attempt(
            self.run_id, self.item_id, policy, 7, now=NOW
        )
        dispatch = self.ledger.append(
            self._record(
                "dispatch_decision",
                "run-dispatch-decision-",
                run_id=self.run_id,
                item_id=self.item_id,
                attempt_id=opened["attempt_id"],
                eligible_workers=["worker-a"],
                chosen_worker="worker-a",
                capability_digest=DIGEST,
                reason_code="policy.route",
                policy_digest=DIGEST,
                routing_rank=0,
                scheduler_epoch=7,
            )
        )
        started = scheduler.start_attempt(
            self.run_id,
            self.item_id,
            str(opened["attempt_id"]),
            str(dispatch["id"]),
            now=NOW,
        )
        self.attempt_id = str(opened["attempt_id"])
        self.attempt_started_id = str(started["id"])
        self.fence_token = str(opened["fence_token"])

    def reopen(self) -> "_DirectSuspensionContext":
        self.root = FloatiRoot.open_direct_home(self.home, create=False)
        self.ledger = RunLedger(self.root)
        self.approvals = ApprovalLedger(self.root)
        self.authorities = AuthorityGrantStore(self.root)
        self.controller = ApprovalSuspensionController(self.ledger, self.approvals)
        return self

    def suspension_records(self) -> list[dict[str, object]]:
        return [
            row
            for row in self.ledger.records()
            if row["kind"] == "attempt_suspended_for_approval"
        ]

    def consumption_records(self) -> list[dict[str, object]]:
        return [
            row
            for row in self.ledger.records()
            if row["kind"] == "approval_consumed_for_resume"
        ]

    def authority_tail(self, subject: str = "execute-run") -> dict[str, object]:
        return read_records(
            self.root,
            f"authority-grants/{subject}.jsonl",
            allowed_kinds={"authority_grant"},
        )[-1]

    def attempt_state(self) -> dict[str, object]:
        return self.ledger.project().run(self.run_id)["attempts"][self.attempt_id]

    def approve(self, *, ttl_seconds: int = 120) -> dict[str, object]:
        self.decision = self.approvals.decide(
            str(self.request["id"]),
            public_ids.reviewer(),
            "approved",
            None,
            granted_scope="repo:slipway",
            granted_ttl_seconds=ttl_seconds,
            now=DIRECT_NOW + timedelta(seconds=3),
        )
        return self.decision

    def deny(self) -> dict[str, object]:
        self.decision = self.approvals.decide(
            str(self.request["id"]),
            public_ids.reviewer(),
            "denied",
            "operator_denied",
            now=DIRECT_NOW + timedelta(seconds=3),
        )
        return self.decision

    def activate_resume(self, holder: str = "worker-b") -> dict[str, object]:
        self.resume_authority = self.authorities.claim(
            "execute-run",
            holder,
            600,
            600,
            DIRECT_NOW + timedelta(seconds=4),
        )
        return self.resume_authority

    def prepare_approved_resume(self, *, ttl_seconds: int = 120) -> None:
        self.controller.suspend(**self.suspend_args)
        self.approve(ttl_seconds=ttl_seconds)
        authority = self.activate_resume()
        self.consume_args = {
            "run_id": self.run_id,
            "item_id": self.item_id,
            "attempt_id": self.attempt_id,
            "approval_decision_id": self.decision["id"],
            "workspace_checkpoint": dict(self.checkpoint),
            "resume_authority_subject": "execute-run",
            "resume_authority_holder": "worker-b",
            "resume_authority_epoch": int(authority["epoch"]),
            "now": DIRECT_NOW + timedelta(seconds=5),
        }

    def divergent_suspend_inputs(self) -> tuple[tuple[dict[str, object], str], ...]:
        checkpoint = dict(self.checkpoint, sha="e" * 40)
        return (
            (
                dict(
                    self.suspend_args,
                    approval_request_id=self.changed_action_request["id"],
                ),
                "suspension_action_divergent",
            ),
            (
                dict(
                    self.suspend_args,
                    approval_request_id=self.changed_scope_request["id"],
                ),
                "suspension_scope_divergent",
            ),
            (
                dict(self.suspend_args, workspace_checkpoint=checkpoint),
                "suspension_checkpoint_divergent",
            ),
            (
                dict(
                    self.suspend_args,
                    execution_authority_holder="worker-z",
                ),
                "suspension_authority_divergent",
            ),
            (
                dict(
                    self.suspend_args,
                    execution_authority_epoch=int(self.execution_authority["epoch"]) + 1,
                ),
                "suspension_authority_divergent",
            ),
        )


class _ManagedSuspensionContext(_DirectSuspensionContext):
    def __init__(
        self,
        home: Path,
        *,
        service_now: datetime = DIRECT_NOW + timedelta(seconds=2),
    ) -> None:
        super().__init__(home)
        self.service_now = service_now
        self._start_service("sequencer-a")

    def _start_service(self, sequencer_id: str) -> None:
        self.service = SequencerService(
            self.root,
            sequencer_id,
            config=SequencerConfig(select_timeout=0.01),
            clock=lambda: self.service_now,
        )
        self.stop = threading.Event()
        self.thread = threading.Thread(
            target=self.service.serve_forever, args=(self.stop,), daemon=True
        )
        self.thread.start()
        self.client = SequencerClient(
            self.service.socket_path, self.service.epoch, "approval-controller"
        )
        self.ledger = RunLedger(self.root, sequencer_client=self.client)
        self.controller = ApprovalSuspensionController(self.ledger, self.approvals)

    def close(self) -> None:
        self.stop.set()
        self.thread.join(3)
        self.service.close()

    def restart_service(self) -> "_ManagedSuspensionContext":
        self.close()
        self.root = FloatiRoot.open_direct_home(self.home, create=False)
        self.approvals = ApprovalLedger(self.root)
        self.authorities = AuthorityGrantStore(self.root)
        self._start_service("sequencer-b")
        return self

    def prepare_approved_resume(self, *, ttl_seconds: int = 120) -> None:
        self.controller.suspend(**self.suspend_args)
        self.approve(ttl_seconds=ttl_seconds)
        authority = self.activate_resume()
        self.consume_args = {
            "run_id": self.run_id,
            "item_id": self.item_id,
            "attempt_id": self.attempt_id,
            "approval_decision_id": self.decision["id"],
            "workspace_checkpoint": dict(self.checkpoint),
            "resume_authority_subject": "execute-run",
            "resume_authority_holder": "worker-b",
            "resume_authority_epoch": int(authority["epoch"]),
            "now": DIRECT_NOW + timedelta(seconds=5),
        }

    def raw_append(self, record: dict[str, object]) -> dict[str, object]:
        payload = self.client.frame(record)
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as channel:
            channel.settimeout(3)
            channel.connect(str(self.service.socket_path))
            channel.sendall(payload)
            response = b""
            while not response.endswith(b"\n"):
                chunk = channel.recv(65536)
                if not chunk:
                    break
                response += chunk
        return json.loads(response)

    def private_candidate(self) -> dict[str, object]:
        records = self.suspension_records()
        if not records:
            raise AssertionError("managed positive control did not append a suspension")
        return records[0]

    def suspension_intent(self) -> dict[str, object]:
        return {
            key: value
            for key, value in self.suspend_args.items()
            if key != "now"
        }

    def suspension_payload(self) -> bytes:
        operation = "suspension_evaluation"
        intent = self.suspension_intent()
        request = self.client._evaluation_request(
            operation,
            "suspension-evaluation-" + _semantic_uuid(operation, intent),
            intent,
        )
        return _encode_frame(request)

    def approval_resume_intent(self) -> dict[str, object]:
        return {
            key: value
            for key, value in self.consume_args.items()
            if key != "now"
        }

    def forge_service_owned_append(self) -> None:
        self.service._ledger._append_suspension(self.private_candidate(), object())


class ApprovalSuspensionManagedTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)

    def managed_ready_context(
        self, *, service_now: datetime = DIRECT_NOW + timedelta(seconds=2)
    ) -> _ManagedSuspensionContext:
        managed = _ManagedSuspensionContext(
            self.base / ("managed-" + uuid7_hex()), service_now=service_now
        )
        self.addCleanup(managed.close)
        return managed

    def managed_approved_context(self) -> _ManagedSuspensionContext:
        managed = self.managed_ready_context()
        managed.prepare_approved_resume()
        managed.service_now = DIRECT_NOW + timedelta(seconds=5)
        return managed

    def test_managed_suspend_is_reconstructed_and_releases_authority_service_side(self) -> None:
        managed = self.managed_ready_context()
        record = managed.controller.suspend(**managed.suspend_args)
        self.assertEqual("attempt_suspended_for_approval", record["kind"])
        self.assertEqual("released", managed.authority_tail()["state"])

    def test_managed_resume_consumes_once_and_retry_survives_service_restart(self) -> None:
        managed = self.managed_approved_context()
        consumed = managed.controller.consume(**managed.consume_args)
        retried = managed.restart_service().controller.consume(**managed.consume_args)
        self.assertEqual(1, len(managed.consumption_records()))
        self.assertEqual(consumed["id"], retried["id"])

    def test_raw_private_suspension_frames_and_generic_typed_records_refuse(self) -> None:
        managed = self.managed_ready_context()
        self.assertEqual(
            "attempt_suspended_for_approval",
            managed.controller.suspend(**managed.suspend_args)["kind"],
        )
        response = managed.raw_append(managed.private_candidate())
        self.assertEqual(
            ("refused", "suspension_controller_only"),
            (response["status"], response["code"]),
        )
        with self.assertRaises(ProtocolRefusal) as generic:
            managed.client.append_intent("scheduler", managed.private_candidate())
        self.assertEqual("suspension_controller_only", generic.exception.code)

    def test_service_clock_and_canonical_ledgers_override_wire_testimony(self) -> None:
        managed = self.managed_ready_context(service_now=EXPIRED)
        with self.assertRaises(ProtocolRefusal) as caught:
            managed.controller.suspend(**managed.suspend_args)
        self.assertEqual("approval_request_expired", caught.exception.code)

    def test_retained_service_or_client_reference_cannot_mint_suspension_authority(self) -> None:
        managed = self.managed_ready_context()
        managed.controller.suspend(**managed.suspend_args)
        with self.assertRaises(ProtocolRefusal) as caught:
            managed.forge_service_owned_append()
        self.assertEqual("suspension_controller_only", caught.exception.code)

    def test_lost_consumption_response_recovers_from_canonical_ledger_after_restart(self) -> None:
        managed = self.managed_approved_context()
        with mock.patch.object(managed.service, "_send_response", return_value=None):
            with self.assertRaises(ProtocolRefusal):
                managed.controller.consume(**managed.consume_args)
        self.assertEqual(1, len(managed.consumption_records()))
        retried = managed.restart_service().controller.consume(**managed.consume_args)
        self.assertEqual(managed.consumption_records()[0]["id"], retried["id"])

    def test_identical_semantic_aliases_each_receive_one_response_and_close(self) -> None:
        managed = self.managed_ready_context()
        channels = []
        for _index in range(2):
            channel = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            channel.settimeout(3)
            channel.connect(str(managed.service.socket_path))
            channel.sendall(managed.suspension_payload())
            channels.append(channel)
        responses = []
        for channel in channels:
            payload = b""
            while True:
                chunk = channel.recv(65536)
                if not chunk:
                    break
                payload += chunk
            responses.append(json.loads(payload))
            channel.close()
        self.assertEqual(["ok", "ok"], [row["status"] for row in responses])
        self.assertEqual(responses[0]["record"]["id"], responses[1]["record"]["id"])
        self.assertEqual(1, len(managed.suspension_records()))

    def test_numeric_adapter_is_not_durably_coerced_by_managed_reconstruction(self) -> None:
        """Catches hostile adapter testimony becoming a valid durable string."""
        managed = self.managed_ready_context()
        arguments = dict(
            managed.suspend_args,
            adapter=123,
            resume_mode="unsupported",
        )
        try:
            record = managed.controller.suspend(**arguments)
        except ProtocolRefusal as caught:
            self.assertEqual("intent_fields_invalid", caught.code)
            self.assertEqual([], managed.suspension_records())
            return
        self.fail(
            "numeric adapter reached durable coercion as " + repr(record["adapter"])
        )

    def test_managed_evaluated_intent_values_are_exact_and_nonpoisoning(self) -> None:
        """Catches any public semantic coordinate accepting a hostile wire type."""
        valid_suspend = self.managed_ready_context()
        suspension = valid_suspend.controller.suspend(**valid_suspend.suspend_args)
        self.assertEqual("attempt_suspended_for_approval", suspension["kind"])

        suspend_string_fields = (
            "run_id",
            "item_id",
            "attempt_id",
            "approval_request_id",
            "adapter",
            "resume_mode",
            "execution_authority_subject",
            "execution_authority_holder",
        )
        suspend_mutations = [
            (field, 123) for field in suspend_string_fields
        ] + [
            ("provider_session_or_thread_id", 123),
            ("workspace_checkpoint", []),
            (
                "workspace_checkpoint",
                dict(valid_suspend.checkpoint, extra="forged"),
            ),
            (
                "workspace_checkpoint",
                dict(valid_suspend.checkpoint, repo=123),
            ),
            ("execution_authority_epoch", True),
            ("execution_authority_epoch", 1.5),
        ]
        for field, value in suspend_mutations:
            with self.subTest(operation="suspend", field=field, value=value):
                managed = self.managed_ready_context()
                intent = managed.suspension_intent()
                intent[field] = value
                with self.assertRaises(ProtocolRefusal) as caught:
                    managed.client.append_intent("suspension_evaluation", intent)
                self.assertEqual("intent_fields_invalid", caught.exception.code)
                self.assertEqual([], managed.suspension_records())

        valid_resume = self.managed_approved_context()
        consumed = valid_resume.controller.consume(**valid_resume.consume_args)
        self.assertEqual("approval_consumed_for_resume", consumed["kind"])

        resume_string_fields = (
            "run_id",
            "item_id",
            "attempt_id",
            "approval_decision_id",
            "resume_authority_subject",
            "resume_authority_holder",
        )
        resume_mutations = [
            (field, 123) for field in resume_string_fields
        ] + [
            ("workspace_checkpoint", []),
            (
                "workspace_checkpoint",
                dict(valid_resume.checkpoint, extra="forged"),
            ),
            (
                "workspace_checkpoint",
                dict(valid_resume.checkpoint, sha=123),
            ),
            ("resume_authority_epoch", False),
            ("resume_authority_epoch", 2.5),
        ]
        for field, value in resume_mutations:
            with self.subTest(operation="resume", field=field, value=value):
                managed = self.managed_approved_context()
                intent = managed.approval_resume_intent()
                intent[field] = value
                with self.assertRaises(ProtocolRefusal) as caught:
                    managed.client.append_intent("approval_resume_evaluation", intent)
                self.assertEqual("intent_fields_invalid", caught.exception.code)
                self.assertEqual([], managed.consumption_records())

    def test_integral_json_number_epochs_match_direct_runtime_normalization(self) -> None:
        """Catches managed epoch validation becoming narrower than durable v1 records."""
        managed = self.managed_ready_context()
        suspend = managed.suspension_intent()
        suspend["execution_authority_epoch"] = float(
            suspend["execution_authority_epoch"]
        )
        suspended = managed.client.append_intent("suspension_evaluation", suspend)[
            "record"
        ]
        self.assertIs(type(suspended["authority_epoch_at_request"]), int)

        managed.approve()
        authority = managed.activate_resume()
        managed.service_now = DIRECT_NOW + timedelta(seconds=5)
        resume = {
            "run_id": managed.run_id,
            "item_id": managed.item_id,
            "attempt_id": managed.attempt_id,
            "approval_decision_id": managed.decision["id"],
            "workspace_checkpoint": dict(managed.checkpoint),
            "resume_authority_subject": "execute-run",
            "resume_authority_holder": "worker-b",
            "resume_authority_epoch": float(authority["epoch"]),
        }
        consumed = managed.client.append_intent(
            "approval_resume_evaluation", resume
        )["record"]
        self.assertIs(type(consumed["resume_authority_epoch"]), int)

    def test_repeated_malformed_alias_refuses_without_append_then_valid_intent_succeeds(self) -> None:
        """Catches malformed aliases entering cache or poisoning the lawful operation."""
        managed = self.managed_ready_context()
        malformed = managed.suspension_intent()
        malformed["adapter"] = 123
        malformed["resume_mode"] = "unsupported"
        for _attempt in range(2):
            with self.assertRaises(ProtocolRefusal) as caught:
                managed.client.append_intent("suspension_evaluation", malformed)
            self.assertEqual("intent_fields_invalid", caught.exception.code)
        self.assertEqual([], managed.suspension_records())
        valid = managed.controller.suspend(**managed.suspend_args)
        self.assertEqual("attempt_suspended_for_approval", valid["kind"])
        self.assertEqual(1, len(managed.suspension_records()))


class ApprovalSuspensionControllerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.assertIsNotNone(
            ApprovalSuspensionController,
            "floati.suspension must provide ApprovalSuspensionController",
        )
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)

    def context(self, **kwargs: object) -> _DirectSuspensionContext:
        return _DirectSuspensionContext(
            self.base / ("case-" + uuid7_hex()), **kwargs
        )

    def test_direct_suspend_fences_attempt_and_durably_releases_old_authority(self) -> None:
        """Catches returning before the suspension fence and old release are durable."""
        context = self.context()
        suspension = context.controller.suspend(**context.suspend_args)
        self.assertEqual("attempt_suspended_for_approval", suspension["kind"])
        self.assertEqual("released", context.authority_tail()["state"])
        self.assertEqual("suspended", context.attempt_state()["state"])
        self.assertEqual(context.attempt_started_id, suspension["attempt_started_id"])
        self.assertEqual(context.fence_token, suspension["fence_token"])
        self.assertEqual(context.request["exact_action_digest"], suspension["exact_action_digest"])

    def test_suspend_exact_retry_finishes_release_after_post_fsync_failure(self) -> None:
        """Catches response retry duplicating suspension or abandoning active old authority."""
        context = self.context(fail_release_once=True)
        with self.assertRaises(DurabilityFailure):
            context.controller.suspend(**context.suspend_args)
        self.assertEqual("suspended", context.attempt_state()["state"])
        self.assertEqual("active", context.authority_tail()["state"])
        retried = context.reopen().controller.suspend(**context.suspend_args)
        self.assertEqual(1, len(context.suspension_records()))
        self.assertEqual("released", context.authority_tail()["state"])
        self.assertEqual(context.suspension_records()[0]["id"], retried["id"])
        self.assertEqual([], [row for row in context.ledger.records() if row["kind"] == "result_produced"])
        self.assertEqual(context.checkpoint, retried["workspace_checkpoint"])

    def test_suspend_retry_waits_for_release_fsync_or_rollback(self) -> None:
        """Catches success from a transient released frame that later rolls back."""
        context = self.context(fail_release_once=True)
        with self.assertRaises(DurabilityFailure):
            context.controller.suspend(**context.suspend_args)

        writer = context.reopen().controller
        observer_root = FloatiRoot.open_direct_home(context.home, create=False)
        observer = ApprovalSuspensionController(
            RunLedger(observer_root), ApprovalLedger(observer_root)
        )
        write_visible = threading.Event()
        permit_fsync_failure = threading.Event()
        observer_tail_returned = threading.Event()
        writer_errors: list[BaseException] = []
        observer_errors: list[BaseException] = []
        observer_results: list[dict[str, object]] = []
        real_fsync = os.fsync
        real_exact_tail = AuthorityGrantStore.exact_tail
        fsync_calls = 0
        fsync_calls_lock = threading.Lock()

        def controlled_fsync(descriptor: int) -> None:
            nonlocal fsync_calls
            with fsync_calls_lock:
                fsync_calls += 1
                first = fsync_calls == 1
            if first:
                write_visible.set()
                if not permit_fsync_failure.wait(5):
                    raise AssertionError("test did not release the blocked fsync")
                raise OSError(errno.EIO, "injected authority fsync failure")
            real_fsync(descriptor)

        def tracked_exact_tail(
            store: AuthorityGrantStore, subject_id: str
        ) -> dict[str, object]:
            result = real_exact_tail(store, subject_id)
            if threading.current_thread().name == "suspension-observer":
                observer_tail_returned.set()
            return result

        def retry_writer() -> None:
            try:
                writer.suspend(**context.suspend_args)
            except BaseException as exc:
                writer_errors.append(exc)

        def retry_observer() -> None:
            try:
                observer_results.append(observer.suspend(**context.suspend_args))
            except BaseException as exc:
                observer_errors.append(exc)

        with mock.patch("floati.jsonl.os.fsync", controlled_fsync), mock.patch.object(
            AuthorityGrantStore, "exact_tail", tracked_exact_tail
        ):
            writer_thread = threading.Thread(target=retry_writer, name="suspension-writer")
            writer_thread.start()
            self.assertTrue(write_visible.wait(5), "release frame never reached pre-fsync seam")
            observer_thread = threading.Thread(
                target=retry_observer, name="suspension-observer"
            )
            observer_thread.start()
            transient_tail_returned = observer_tail_returned.wait(0.25)
            permit_fsync_failure.set()
            writer_thread.join(5)
            observer_thread.join(5)

        self.assertFalse(writer_thread.is_alive())
        self.assertFalse(observer_thread.is_alive())
        self.assertFalse(
            transient_tail_returned,
            "exact retry observed rollback-capable released evidence",
        )
        self.assertEqual(1, len(writer_errors))
        self.assertIsInstance(writer_errors[0], DurabilityFailure)
        self.assertEqual([], observer_errors)
        self.assertEqual(1, len(observer_results))
        self.assertEqual("released", context.authority_tail()["state"])
        self.assertEqual(1, len(context.suspension_records()))

    def test_suspend_exact_retry_accepts_durably_released_positive_control(self) -> None:
        """Proves synchronization does not reject a committed released tail."""
        context = self.context()
        suspension = context.controller.suspend(**context.suspend_args)
        retried = context.reopen().controller.suspend(**context.suspend_args)
        self.assertEqual(suspension["id"], retried["id"])
        self.assertEqual("released", context.authority_tail()["state"])

    def test_suspend_retry_never_releases_a_newer_authority_epoch(self) -> None:
        """Catches recovery releasing the current epoch instead of confirming the old fence."""
        context = self.context(fail_release_once=True)
        with self.assertRaises(DurabilityFailure):
            context.controller.suspend(**context.suspend_args)
        context.authorities.expire(
            "execute-run",
            "worker-a",
            int(context.execution_authority["epoch"]),
            DIRECT_NOW + timedelta(seconds=3),
        )
        newer = context.activate_resume(holder="worker-b")
        retried = context.reopen().controller.suspend(**context.suspend_args)
        self.assertEqual(context.suspension_records()[0]["id"], retried["id"])
        self.assertEqual(newer["id"], context.authority_tail()["id"])
        self.assertEqual("active", context.authority_tail()["state"])

    def test_suspend_retry_after_old_authority_timeout_confirms_inactive(self) -> None:
        """Catches a fenced retry becoming unrecoverable after the old TTL elapses."""
        context = self.context(fail_release_once=True)
        with self.assertRaises(DurabilityFailure):
            context.controller.suspend(**context.suspend_args)
        arguments = dict(
            context.suspend_args,
            now=DIRECT_NOW + timedelta(seconds=601),
        )
        retried = context.reopen().controller.suspend(**arguments)
        self.assertEqual(context.suspension_records()[0]["id"], retried["id"])
        self.assertEqual("active", context.authority_tail()["state"])
        self.assertEqual(
            "2026-08-09T12:10:00.000Z",
            context.authority_tail()["expires_at"],
        )

    def test_suspend_refuses_changed_action_scope_checkpoint_fence_and_authority(self) -> None:
        """Catches semantic retries changing any durable suspension coordinate."""
        context = self.context()
        context.controller.suspend(**context.suspend_args)
        for mutation, code in context.divergent_suspend_inputs():
            with self.subTest(code=code), self.assertRaises(ProtocolRefusal) as caught:
                context.controller.suspend(**mutation)
            self.assertEqual(code, caught.exception.code)
        self.assertEqual(1, len(context.suspension_records()))

    def test_concurrent_exact_suspend_returns_one_durable_record_to_both_callers(self) -> None:
        """Catches pre-lock exact contenders turning idempotency into a duplicate refusal."""
        context = self.context()
        controllers = []
        for _index in range(2):
            root = FloatiRoot.open_direct_home(context.home, create=False)
            controllers.append(
                ApprovalSuspensionController(RunLedger(root), ApprovalLedger(root))
            )
        ready_to_append = threading.Barrier(2)
        results: list[dict[str, object]] = []
        errors: list[BaseException] = []

        def pause_before_append(record: dict, *args: object, **kwargs: object) -> dict:
            validated = validate_record(record, *args, **kwargs)
            if record.get("kind") == "attempt_suspended_for_approval":
                ready_to_append.wait(5)
            return validated

        def suspend(controller: ApprovalSuspensionController) -> None:
            try:
                results.append(controller.suspend(**context.suspend_args))
            except BaseException as exc:
                errors.append(exc)

        with mock.patch("floati.suspension.validate_record", pause_before_append):
            contenders = [
                threading.Thread(target=suspend, args=(controller,))
                for controller in controllers
            ]
            for contender in contenders:
                contender.start()
            for contender in contenders:
                contender.join(5)

        self.assertTrue(all(not contender.is_alive() for contender in contenders))
        self.assertEqual([], errors)
        self.assertEqual(2, len(results))
        self.assertEqual(results[0]["id"], results[1]["id"])
        self.assertEqual(1, len(context.suspension_records()))

    def test_concurrent_exact_consume_returns_one_durable_record_to_both_callers(self) -> None:
        """Catches pre-lock resume contenders turning idempotency into a duplicate refusal."""
        context = self.context()
        context.prepare_approved_resume()
        controllers = []
        for _index in range(2):
            root = FloatiRoot.open_direct_home(context.home, create=False)
            controllers.append(
                ApprovalSuspensionController(RunLedger(root), ApprovalLedger(root))
            )
        ready_to_append = threading.Barrier(2)
        results: list[dict[str, object]] = []
        errors: list[BaseException] = []

        def pause_before_append(record: dict, *args: object, **kwargs: object) -> dict:
            validated = validate_record(record, *args, **kwargs)
            if record.get("kind") == "approval_consumed_for_resume":
                ready_to_append.wait(5)
            return validated

        def consume(controller: ApprovalSuspensionController) -> None:
            try:
                results.append(controller.consume(**context.consume_args))
            except BaseException as exc:
                errors.append(exc)

        with mock.patch("floati.suspension.validate_record", pause_before_append):
            contenders = [
                threading.Thread(target=consume, args=(controller,))
                for controller in controllers
            ]
            for contender in contenders:
                contender.start()
            for contender in contenders:
                contender.join(5)

        self.assertTrue(all(not contender.is_alive() for contender in contenders))
        self.assertEqual([], errors)
        self.assertEqual(2, len(results))
        self.assertEqual(results[0]["id"], results[1]["id"])
        self.assertEqual(1, len(context.consumption_records()))

    def test_consume_approved_decision_once_under_newer_live_authority(self) -> None:
        """Catches resume without exact approval or duplicate consumption on retry."""
        context = self.context()
        context.prepare_approved_resume()
        consumed = context.controller.consume(**context.consume_args)
        retried = context.reopen().controller.consume(**context.consume_args)
        self.assertEqual(consumed["id"], retried["id"])
        self.assertEqual(1, len(context.consumption_records()))
        self.assertEqual("resumed", context.attempt_state()["state"])

    def test_consume_refuses_denied_expired_same_epoch_wrong_holder_and_unsupported(self) -> None:
        """Catches fail-open resume branches; a lawful independent control runs first."""
        positive = self.context()
        positive.prepare_approved_resume()
        self.assertEqual(
            "approval_consumed_for_resume",
            positive.controller.consume(**positive.consume_args)["kind"],
        )

        denied = self.context()
        denied.controller.suspend(**denied.suspend_args)
        denied.deny()
        denied_authority = denied.activate_resume()
        denied_args = {
            "run_id": denied.run_id,
            "item_id": denied.item_id,
            "attempt_id": denied.attempt_id,
            "approval_decision_id": denied.decision["id"],
            "workspace_checkpoint": dict(denied.checkpoint),
            "resume_authority_subject": "execute-run",
            "resume_authority_holder": "worker-b",
            "resume_authority_epoch": denied_authority["epoch"],
            "now": DIRECT_NOW + timedelta(seconds=5),
        }

        expired = self.context()
        expired.prepare_approved_resume(ttl_seconds=2)
        expired.consume_args["now"] = DIRECT_NOW + timedelta(seconds=6)

        same_epoch = self.context()
        same_epoch.controller.suspend(**same_epoch.suspend_args)
        same_epoch.approve()
        same_epoch.consume_args = {
            "run_id": same_epoch.run_id,
            "item_id": same_epoch.item_id,
            "attempt_id": same_epoch.attempt_id,
            "approval_decision_id": same_epoch.decision["id"],
            "workspace_checkpoint": dict(same_epoch.checkpoint),
            "resume_authority_subject": "execute-run",
            "resume_authority_holder": "worker-a",
            "resume_authority_epoch": same_epoch.execution_authority["epoch"],
            "now": DIRECT_NOW + timedelta(seconds=5),
        }

        wrong_holder = self.context()
        wrong_holder.prepare_approved_resume()
        wrong_holder.consume_args["resume_authority_holder"] = "worker-z"

        unsupported = self.context(adapter="claude", resume_mode="unsupported")
        unsupported.prepare_approved_resume()

        cases = (
            (denied, denied_args, "approval_not_approved"),
            (expired, expired.consume_args, "approval_decision_expired"),
            (same_epoch, same_epoch.consume_args, "resume_authority_not_newer"),
            (wrong_holder, wrong_holder.consume_args, "resume_authority_holder_mismatch"),
            (unsupported, unsupported.consume_args, "approval_resume_unsupported"),
        )
        for context, arguments, code in cases:
            with self.subTest(code=code), self.assertRaises(ProtocolRefusal) as caught:
                context.controller.consume(**arguments)
            self.assertEqual(code, caught.exception.code)
            self.assertEqual([], context.consumption_records())

    def test_public_and_forged_private_suspension_appends_refuse(self) -> None:
        """Catches a raw or forged caller acquiring controller-only append authority."""
        context = self.context()
        candidate = context.controller.suspend(**context.suspend_args)
        with self.assertRaises(ProtocolRefusal) as public:
            context.ledger.append(candidate)
        self.assertEqual("suspension_controller_only", public.exception.code)
        with self.assertRaises(ProtocolRefusal) as forged_capability:
            context.ledger._suspension_capability_for(object())
        self.assertEqual("suspension_controller_only", forged_capability.exception.code)
        with self.assertRaises(ProtocolRefusal) as forged_append:
            context.ledger._append_suspension(candidate, object())
        self.assertEqual("suspension_controller_only", forged_append.exception.code)
        self.assertEqual(1, len(context.suspension_records()))

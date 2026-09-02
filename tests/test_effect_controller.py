from __future__ import annotations

from floati import fixture_ids as public_ids

import hashlib
import gc
import inspect
import errno
import json
import multiprocessing
import multiprocessing.util
import os
import shutil
import subprocess
import tempfile
import threading
import types
import unittest
from contextlib import contextmanager
from copy import deepcopy
from datetime import timedelta
from pathlib import Path
from unittest import mock

import floati.jsonl as jsonl
import floati.effects as effects_module
import floati.policy as policy_module
import floati.runtruth as runtruth
import floati.workers as workers_module
from floati.approvals import ApprovalLedger
from floati.effects import EffectLedger
try:
    from floati.effects import EffectController
except ImportError:
    EffectController = None
from floati.errors import ProtocolRefusal
from floati.framing import encode_frame
from floati.host_paths import worker_workspace_root
from floati.ids import uuid7_hex
from floati.jsonl import append_record
from floati.planes import AuthorityGrantStore
from floati.policy import RepositoryPolicy
from floati.records import EFFECT_KINDS
from floati.root import FloatiRoot
from floati.runtruth import RunLedger
from floati.suspension import ApprovalSuspensionController
from tests.test_admission import ITEM_A, ITEM_B
from tests.test_run_limits import NOW, _RunLimitCase


REQUEST_DIGEST = hashlib.sha256(b"effect request").hexdigest()


def _recovered_controller_body(method):
    """Return the callable body exposed by the current controller wrapper."""
    wrapped = getattr(method, "__wrapped__", None)
    if callable(wrapped):
        return wrapped
    body_code = getattr(method, "_effect_controller_body_code", None)
    for cell in method.__closure__ or ():
        candidate = cell.cell_contents
        if callable(candidate) and getattr(candidate, "__code__", None) is body_code:
            return candidate
    raise AssertionError("controller wrapper must expose its callable body for this attack")


def _managed_acceptance_process(
    base: str,
    acceptance: dict[str, object],
    acquired: object,
    release: object,
    results: object,
) -> None:
    from floati.sequencer import SequencerService

    root = FloatiRoot.open_direct_home(Path(base), create=False)
    original_guard = runtruth.effect_acceptance_guard

    @contextmanager
    def observed_guard(selected_root):
        with original_guard(selected_root):
            acquired.set()
            if not release.wait(5):
                raise RuntimeError("managed acceptance release timed out")
            yield

    service = SequencerService(root, "effect-race-sequencer")
    try:
        with mock.patch.object(runtruth, "effect_acceptance_guard", observed_guard):
            outcomes = service._ledger._append_managed_batch(
                [acceptance], service.epoch,
                service._lease.managed_append_capability,
            )
        outcome = outcomes[0]
        if isinstance(outcome, ProtocolRefusal):
            results.put(("managed_refused", outcome.code))
        else:
            results.put(("managed_ok", outcome["id"]))
    except Exception as exc:
        results.put(("managed_error", f"{type(exc).__name__}:{exc}"))
    finally:
        service.close()


def _effect_intent_process(
    base: str,
    policy_path: str,
    intent_args: dict[str, object],
    started: object,
    results: object,
) -> None:
    root = FloatiRoot.open_direct_home(Path(base), create=False)
    controller = EffectController(
        EffectLedger(root), RunLedger(root),
        RepositoryPolicy.load(Path(policy_path)), ApprovalLedger(root),
    )
    started.set()
    try:
        intent = controller.intent(**intent_args)
        results.put(("effect_ok", intent["id"]))
    except ProtocolRefusal as exc:
        results.put(("effect_refused", exc.code))
    except Exception as exc:
        results.put(("effect_error", f"{type(exc).__name__}:{exc}"))


class _EffectCase:
    def __init__(self, testcase: unittest.TestCase, *, start: bool = True) -> None:
        self.run = _RunLimitCase(testcase)
        self.root = self.run.root
        self.run_ledger = self.run.ledger
        self.effect_ledger = EffectLedger(self.root)
        self.item_id = ITEM_A
        self.opened = self.run.opened[self.item_id]
        self.dispatch = None
        self.started = None
        if start:
            self.dispatch = self.run.dispatch(self.item_id, "node-a")
            self.started = self.run.scheduler.start_attempt(
                self.run.run_id,
                self.item_id,
                self.opened["attempt_id"],
                self.dispatch["id"],
                now=NOW + timedelta(seconds=21),
            )
        self.controller = None if EffectController is None else EffectController(
            self.effect_ledger,
            self.run_ledger,
            self.run.policy,
            ApprovalLedger(self.root),
        )

    def intent_args(self, **changes: object) -> dict[str, object]:
        values: dict[str, object] = {
            "run_id": self.run.run_id,
            "item_id": self.item_id,
            "attempt_id": self.opened["attempt_id"],
            "fence_token": self.opened["fence_token"],
            "effect_type": "git_ref_update",
            "target": {
                "kind": "git_ref",
                "coordinate": "owner/floati:refs/heads/main",
                "identity_digest": "a" * 64,
            },
            "request_digest": REQUEST_DIGEST,
            "idempotency_key": "effect-one",
            "expected_confirmation": {
                "kind": "git_ref_equals",
                "locator": "refs/heads/main",
                "expected_digest": "b" * 64,
            },
            "reconciliation_adapter": "git_local",
            "risk_class": "low",
            "budget_claim": [{"budget_id": "build", "amount": 1}],
            "requested_by": "node-a",
            "now": NOW + timedelta(seconds=22),
        }
        values.update(changes)
        return values

    def approve_action(self, *, digest: str = REQUEST_DIGEST):
        authorities = AuthorityGrantStore(self.root)
        grant = authorities.claim(
            "effect-approval", public_ids.reviewer(), 240, 240,
            NOW + timedelta(seconds=22),
        )
        approvals = ApprovalLedger(self.root)
        request = approvals.request_for_action(
            "node-a", "effect.execute", "repo:floati", 120, digest,
            "effect-approval", grant["epoch"],
            now=NOW + timedelta(seconds=23),
        )
        decision = approvals.decide(
            request["id"], public_ids.reviewer(), "approved", None,
            granted_scope="repo:floati", granted_ttl_seconds=90,
            now=NOW + timedelta(seconds=24),
        )
        return request, decision

    def suspend_for_action(self, *, resume: bool):
        authorities = AuthorityGrantStore(self.root)
        approval_authority = authorities.claim(
            "resume-approval", public_ids.reviewer(), 240, 240,
            NOW + timedelta(seconds=22),
        )
        execution = authorities.claim(
            "effect-execution", "node-a", 240, 240,
            NOW + timedelta(seconds=22),
        )
        approvals = ApprovalLedger(self.root)
        request = approvals.request_for_action(
            "node-a", "effect.execute", "repo:floati", 120,
            REQUEST_DIGEST, "resume-approval", approval_authority["epoch"],
            now=NOW + timedelta(seconds=23),
        )
        checkpoint = {
            "repo": "owner/floati", "sha": "d" * 40,
            "doc": "docs/checkpoints/effect.md",
        }
        suspension = ApprovalSuspensionController(
            self.run_ledger, approvals
        )
        suspended = suspension.suspend(
            self.run.run_id, self.item_id, self.opened["attempt_id"],
            request["id"], adapter="codex", resume_mode="checkpoint_restart",
            provider_session_or_thread_id=None,
            workspace_checkpoint=checkpoint,
            execution_authority_subject="effect-execution",
            execution_authority_holder="node-a",
            execution_authority_epoch=execution["epoch"],
            now=NOW + timedelta(seconds=24),
        )
        if not resume:
            return request, None, suspended, None
        decision = approvals.decide(
            request["id"], public_ids.reviewer(), "approved", None,
            granted_scope="repo:floati", granted_ttl_seconds=90,
            now=NOW + timedelta(seconds=25),
        )
        resumed_authority = authorities.claim(
            "effect-execution", "node-a", 240, 240,
            NOW + timedelta(seconds=26),
        )
        consumed = suspension.consume(
            self.run.run_id, self.item_id, self.opened["attempt_id"],
            decision["id"], workspace_checkpoint=checkpoint,
            resume_authority_subject="effect-execution",
            resume_authority_holder="node-a",
            resume_authority_epoch=resumed_authority["epoch"],
            now=NOW + timedelta(seconds=27),
        )
        return request, decision, suspended, consumed

    def accept_result(self) -> dict[str, object]:
        return self.run.ledger.append(self.result_acceptance_candidate())

    def result_acceptance_candidate(self) -> dict[str, object]:
        assert self.dispatch is not None
        receipt = {
            "schema_version": 0,
            "id": "worker-receipt-" + uuid7_hex(),
            "tenant_id": self.root.tenant_id,
            "timestamp": "2026-08-09T14:00:22.000Z",
            "kind": "worker_receipt",
            "session_id": "worker-" + uuid7_hex(),
            "work_item_id": self.item_id,
            "node_id": "node-a",
            "adapter": "codex",
            "transition": "claim",
            "outcome_code": None,
            "authority_subject": "execute-run",
            "authority_epoch": 1,
            "artifact_bindings": [],
        }
        append_record(
            self.root, "receipts/workers.jsonl", receipt,
            allowed_kinds={"worker_receipt"},
        )
        produced = self.run._append(
            "result_produced", "run-result-produced-",
            item_id=self.item_id,
            attempt_id=self.opened["attempt_id"],
            dispatch_decision_id=self.dispatch["id"],
            worker_receipt_ids=[receipt["id"]],
        )
        return {
            "schema_version": 0,
            "id": "run-result-accepted-" + uuid7_hex(),
            "tenant_id": self.root.tenant_id,
            "timestamp": "2026-08-09T14:00:00.000Z",
            "kind": "result_accepted",
            "run_id": self.run.run_id,
            "item_id": self.item_id,
            "attempt_id": self.opened["attempt_id"],
            "predecessor_result_id": produced["id"],
            "acceptance_mode": "accepted_unverified",
            "acceptance_receipt_id": None,
            "worker_receipt_ids": [receipt["id"]],
        }


class EffectControllerBindingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.assertIsNotNone(
            EffectController, "floati.effects must provide EffectController"
        )

    def test_lawful_intent_binds_current_started_attempt_and_fence(self) -> None:
        """Catches caller testimony replacing the durable start/fence identity."""
        case = _EffectCase(self)
        intent = case.controller.intent(**case.intent_args())
        self.assertEqual(case.started["id"], intent["attempt_started_id"])
        self.assertEqual(case.opened["fence_token"], intent["fence_token"])
        self.assertEqual([intent], case.effect_ledger.records())

    def test_intent_refuses_missing_unstarted_terminal_suspended_or_stale_attempt(self) -> None:
        """Catches intent escaping any current-started-attempt lifecycle fence."""
        missing = _EffectCase(self)
        unstarted = _EffectCase(self, start=False)
        terminal = _EffectCase(self)
        terminal.run.scheduler.terminal_attempt(
            terminal.run.run_id, terminal.item_id, terminal.opened["attempt_id"],
            "failed", "permanent", "permanent_failure", "idempotent",
            now=NOW + timedelta(seconds=23),
        )
        suspended = _EffectCase(self)
        suspended.suspend_for_action(resume=False)
        cases = (
            (missing, {"attempt_id": "attempt-" + uuid7_hex()}, "effect_attempt_missing"),
            (unstarted, {}, "effect_attempt_unstarted"),
            (terminal, {}, "effect_attempt_terminal"),
            (suspended, {}, "effect_attempt_suspended"),
            (missing, {"fence_token": "f" * 64}, "effect_fence_stale"),
        )
        for candidate, changes, code in cases:
            with self.subTest(code=code), self.assertRaises(ProtocolRefusal) as caught:
                candidate.controller.intent(**candidate.intent_args(**changes))
            self.assertEqual(code, caught.exception.code)
            self.assertEqual([], candidate.effect_ledger.records())

    def test_no_new_intent_after_result_acceptance(self) -> None:
        """Catches cross-ledger write skew admitting an effect after acceptance."""
        case = _EffectCase(self)
        accepted = case.accept_result()
        self.assertEqual("result_accepted", accepted["kind"])
        with self.assertRaises(ProtocolRefusal) as caught:
            case.controller.intent(**case.intent_args())
        self.assertEqual("effect_attempt_accepted", caught.exception.code)
        self.assertEqual([], case.effect_ledger.records())

    def test_risk_gate_requires_matching_request_decision_and_resume_consumption(self) -> None:
        """Catches high-risk intent using absent, denied, drifted, or unrelated approval."""
        case = _EffectCase(self)
        with self.assertRaises(ProtocolRefusal) as missing:
            case.controller.intent(**case.intent_args(risk_class="high"))
        self.assertEqual("effect_approval_required", missing.exception.code)

        request, decision = case.approve_action()
        intent = case.controller.intent(**case.intent_args(
            risk_class="high", approval_request_id=request["id"],
            approval_decision_id=decision["id"],
        ))
        self.assertEqual((request["id"], decision["id"]), (
            intent["approval_request_id"], intent["approval_decision_id"],
        ))

        drifted = _EffectCase(self)
        wrong_request, wrong_decision = drifted.approve_action(digest="c" * 64)
        with self.assertRaises(ProtocolRefusal) as wrong:
            drifted.controller.intent(**drifted.intent_args(
                risk_class="high", approval_request_id=wrong_request["id"],
                approval_decision_id=wrong_decision["id"],
            ))
        self.assertEqual("effect_approval_action_mismatch", wrong.exception.code)

        resumed = _EffectCase(self)
        resume_request, resume_decision, _suspended, consumption = (
            resumed.suspend_for_action(resume=True)
        )
        with self.assertRaises(ProtocolRefusal) as missing_consumption:
            resumed.controller.intent(**resumed.intent_args(
                risk_class="high", approval_request_id=resume_request["id"],
                approval_decision_id=resume_decision["id"],
            ))
        self.assertEqual(
            "effect_approval_consumption_mismatch",
            missing_consumption.exception.code,
        )
        other_request, other_decision = resumed.approve_action()
        with self.assertRaises(ProtocolRefusal) as unrelated_consumption:
            resumed.controller.intent(**resumed.intent_args(
                risk_class="high", approval_request_id=other_request["id"],
                approval_decision_id=other_decision["id"],
                approval_consumption_id=consumption["id"],
            ))
        self.assertEqual(
            "effect_approval_consumption_mismatch",
            unrelated_consumption.exception.code,
        )
        resumed_intent = resumed.controller.intent(**resumed.intent_args(
            risk_class="high", approval_request_id=resume_request["id"],
            approval_decision_id=resume_decision["id"],
            approval_consumption_id=consumption["id"],
        ))
        self.assertEqual(consumption["id"], resumed_intent["approval_consumption_id"])

    def test_approval_and_consumption_do_not_count_as_effect_confirmation(self) -> None:
        """Catches approval evidence satisfying independent effect confirmation."""
        case = _EffectCase(self)
        request, decision = case.approve_action()
        intent = case.controller.intent(**case.intent_args(
            risk_class="high", approval_request_id=request["id"],
            approval_decision_id=decision["id"],
        ))
        evidence = case.effect_ledger.project().acceptance_evidence(
            case.run.run_id, case.opened["attempt_id"]
        )
        self.assertEqual((intent["operation_id"] + ":intent",), evidence.blockers)
        self.assertEqual((), evidence.measured_spend)

    def test_budget_claim_must_fit_attempt_and_run_reservations(self) -> None:
        """Catches unknown, over-bound, or aggregate claims exceeding durable admission."""
        lawful = _EffectCase(self)
        row = lawful.controller.intent(**lawful.intent_args())
        self.assertEqual([{"budget_id": "build", "amount": 1}], row["budget_claim"])

        cases = (
            ([{"budget_id": "unknown", "amount": 1}], "effect_budget_unknown"),
            ([{"budget_id": "build", "amount": 2}], "effect_run_budget_exceeded"),
        )
        for claim, code in cases:
            with self.subTest(code=code):
                hostile = _EffectCase(self)
                with self.assertRaises(ProtocolRefusal) as caught:
                    hostile.controller.intent(**hostile.intent_args(budget_claim=claim))
                self.assertEqual(code, caught.exception.code)
                self.assertEqual([], hostile.effect_ledger.records())

    def test_changed_idempotency_binding_refuses_inside_effect_transaction(self) -> None:
        """Catches same-key retries changing a request after pre-lock validation."""
        case = _EffectCase(self)
        first = case.controller.intent(**case.intent_args())
        with self.assertRaises(ProtocolRefusal) as caught:
            case.controller.intent(**case.intent_args(request_digest="d" * 64))
        self.assertEqual("effect_idempotency_conflict", caught.exception.code)
        self.assertEqual([first], case.effect_ledger.records())

    def test_controller_methods_construct_records_and_reject_raw_record_inputs(self) -> None:
        """Catches typed lifecycle methods becoming caller-built durable-row appends."""
        case = _EffectCase(self)
        intent = case.controller.intent(**case.intent_args())
        dispatched = case.controller.dispatched(
            intent["operation_id"], dispatch_adapter="git_local",
            dispatch_evidence_digest="d" * 64,
            now=NOW + timedelta(seconds=23),
        )
        acknowledged = case.controller.acknowledged(
            intent["operation_id"], acknowledgement_digest="e" * 64,
            now=NOW + timedelta(seconds=24),
        )
        failed = case.controller.failed(
            intent["operation_id"], reason_code="effect_not_applied",
            evidence_digest="f" * 64, spend_status="complete",
            measured_spend=[{"budget_id": "build", "amount": 0}],
            now=NOW + timedelta(seconds=25),
        )
        self.assertEqual(
            ["effect_intent", "effect_dispatched", "effect_acknowledged", "effect_failed"],
            [row["kind"] for row in case.effect_ledger.records()],
        )
        self.assertEqual(dispatched["id"], acknowledged["effect_dispatched_id"])
        self.assertEqual("f" * 64, failed["failure_evidence_digest"])
        raw_calls = (
            lambda: case.controller.dispatched(
                deepcopy(intent), dispatch_adapter="git_local",
                dispatch_evidence_digest="1" * 64,
            ),
            lambda: case.controller.acknowledged(
                deepcopy(intent), acknowledgement_digest="2" * 64,
            ),
            lambda: case.controller.failed(
                deepcopy(intent), reason_code="effect_not_applied",
                evidence_digest="3" * 64, spend_status="unknown",
            ),
            lambda: case.controller.unknown(
                deepcopy(intent), reason_code="confirmation_absent",
                evidence_digest="4" * 64, spend_status="unknown",
            ),
        )
        for index, call in enumerate(raw_calls):
            with self.subTest(index=index), self.assertRaises(ProtocolRefusal) as caught:
                call()
            self.assertEqual("effect_operation_id_invalid", caught.exception.code)


class EffectControllerAuthorityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.assertIsNotNone(EffectController)

    def test_lawful_control_precedes_raw_retained_copied_forged_and_foreign_refusals(self) -> None:
        """Catches any ordinary object or retained private seam acquiring append authority."""
        case = _EffectCase(self)
        lawful = case.controller.intent(**case.intent_args())
        before = case.effect_ledger.records()
        copied = case.controller._EffectController__capability
        retained = case.effect_ledger._append_controller
        candidate = deepcopy(lawful)
        candidate["id"] = "effect-intent-" + uuid7_hex()
        candidate["idempotency_key"] = "forged-key"
        attempts = (
            lambda: retained(candidate, object()),
            lambda: retained(candidate, copied),
            lambda: case.effect_ledger._controller_capability_for(object()),
            lambda: case.controller._negative_outcome(
                "effect_failed", lawful["operation_id"],
                reason_code="effect_not_applied", evidence_digest="7" * 64,
                spend_status="unknown", measured_spend=None,
                now=NOW + timedelta(seconds=23),
            ),
        )
        for index, attack in enumerate(attempts):
            with self.subTest(index=index), self.assertRaises(ProtocolRefusal) as caught:
                attack()
            self.assertEqual("effect_controller_only", caught.exception.code)
            self.assertEqual(before, case.effect_ledger.records())

        unowned = EffectLedger(case.root)
        forged_controller = object.__new__(EffectController)
        forged_controller.ledger = unowned
        with self.assertRaises(ProtocolRefusal) as forged_exact_type:
            unowned._controller_capability_for(forged_controller)
        self.assertEqual(
            "effect_controller_only", forged_exact_type.exception.code
        )
        self.assertEqual(before, case.effect_ledger.records())

        foreign = _EffectCase(self)
        with self.assertRaises(ProtocolRefusal) as cross:
            case.effect_ledger._controller_capability_for(foreign.controller)
        self.assertEqual("effect_controller_only", cross.exception.code)
        self.assertEqual(before, case.effect_ledger.records())

        with self.assertRaises(ProtocolRefusal) as cross_root:
            EffectController(
                EffectLedger(case.root), foreign.run_ledger, case.run.policy,
                ApprovalLedger(case.root),
            )
        self.assertEqual("effect_root_mismatch", cross_root.exception.code)
        self.assertEqual(before, case.effect_ledger.records())

    def test_subclass_and_class_monkeypatch_cannot_append(self) -> None:
        """Catches dynamic dispatch replacing exact controller-owned construction."""
        case = _EffectCase(self)
        lawful = case.controller.intent(**case.intent_args())
        before = case.effect_ledger.records()

        class HostileController(EffectController):
            pass

        with self.assertRaises(ProtocolRefusal) as subclassed:
            HostileController(
                EffectLedger(case.root), case.run_ledger, case.run.policy,
                ApprovalLedger(case.root),
            )
        self.assertEqual("effect_controller_only", subclassed.exception.code)

        retained_append = case.effect_ledger._append_controller
        copied = case.controller._EffectController__capability

        def hostile_intent(_self, **_fields):
            return retained_append(deepcopy(lawful), copied)

        with mock.patch.object(EffectController, "intent", hostile_intent):
            with self.assertRaises(ProtocolRefusal) as monkeypatched:
                case.controller.intent(**case.intent_args())
        self.assertEqual("effect_controller_only", monkeypatched.exception.code)
        self.assertEqual(before, case.effect_ledger.records())

    def test_sequencer_client_has_no_effect_append_authority(self) -> None:
        """Catches a managed run client being mistaken for effect-ledger authority."""
        case = _EffectCase(self)
        lawful = case.controller.intent(**case.intent_args())
        before = case.effect_ledger.records()
        self.assertEqual([lawful], before)

        class Client:
            def append(self, _record):
                return {"record": _record}

        with self.assertRaises(ProtocolRefusal) as caught:
            EffectController(
                EffectLedger(case.root),
                RunLedger(case.root, sequencer_client=Client()),
                case.run.policy,
                ApprovalLedger(case.root),
            )
        self.assertEqual("effect_managed_evaluation_required", caught.exception.code)
        self.assertEqual(before, case.effect_ledger.records())

    def test_generic_jsonl_and_retained_helpers_cannot_append_effect_truth(self) -> None:
        """Catches the generic root writer bypassing EffectController authority."""
        attacks = ("append_record", "transact", "append_frame")
        for attack in attacks:
            with self.subTest(attack=attack):
                case = _EffectCase(self)
                lawful = case.controller.intent(**case.intent_args())
                candidate = deepcopy(lawful)
                candidate["id"] = "effect-intent-" + uuid7_hex()
                if attack == "append_record":
                    invoke = lambda: jsonl.append_record(
                        case.root, EffectLedger.relative_path, candidate,
                        allowed_kinds=set(EFFECT_KINDS),
                    )
                elif attack == "transact":
                    invoke = lambda: jsonl.transact(
                        case.root, EffectLedger.relative_path,
                        lambda _existing: (candidate, candidate),
                        allowed_kinds=set(EFFECT_KINDS),
                    )
                else:
                    path = case.root.resolve_relative(EffectLedger.relative_path)
                    invoke = lambda: jsonl._append_frame(
                        path, encode_frame(candidate)
                    )
                with self.assertRaises(ProtocolRefusal) as caught:
                    invoke()
                self.assertEqual("effect_controller_only", caught.exception.code)
                self.assertEqual([lawful], case.effect_ledger.records())

        case = _EffectCase(self)
        lawful = case.controller.intent(**case.intent_args())
        internal = getattr(jsonl, "_transact_effect_records", None)
        self.assertTrue(callable(internal), "Effect writes need one sealed internal transaction")
        if callable(internal):
            with self.assertRaises(ProtocolRefusal) as retained:
                internal(case.root, lambda _existing: (lawful, lawful))
            self.assertEqual("effect_controller_only", retained.exception.code)
        self.assertEqual([lawful], case.effect_ledger.records())

        case = _EffectCase(self)
        lawful = case.controller.intent(**case.intent_args())
        candidate = deepcopy(lawful)
        candidate["id"] = "effect-intent-" + uuid7_hex()

        def hostile_append(self, _raw=None, _capability=None, _resolve=None):
            return internal(
                self.root, lambda _existing: (candidate, candidate)
            )

        with mock.patch.object(EffectLedger, "_append_controller", hostile_append):
            with self.assertRaises(ProtocolRefusal) as replaced_owner:
                case.effect_ledger._append_controller(candidate)
        self.assertEqual("effect_controller_only", replaced_owner.exception.code)
        self.assertEqual([lawful], case.effect_ledger.records())

    def test_effect_path_aliases_cannot_reach_the_protected_ledger(self) -> None:
        """Catches traversal and symlink spellings reaching Effect truth."""
        for alias_kind in ("dot", "traversal", "symlink"):
            with self.subTest(alias_kind=alias_kind):
                case = _EffectCase(self)
                lawful = case.controller.intent(**case.intent_args())
                candidate = deepcopy(lawful)
                candidate["id"] = "effect-intent-" + uuid7_hex()
                protected = case.root.resolve_relative(EffectLedger.relative_path)
                if alias_kind == "dot":
                    alias = Path(str(protected.parent) + "/./records.jsonl")
                elif alias_kind == "traversal":
                    pivot = protected.parent / "pivot"
                    pivot.mkdir()
                    alias = pivot / ".." / "records.jsonl"
                else:
                    alias_parent = case.root.tenant_home / "effect-alias"
                    alias_parent.symlink_to(protected.parent, target_is_directory=True)
                    alias = alias_parent / "records.jsonl"
                before_bytes = protected.read_bytes()
                before_entries = sorted(
                    (str(path.relative_to(case.root.tenant_home)), path.is_symlink())
                    for path in case.root.tenant_home.rglob("*")
                )
                with self.assertRaises(ProtocolRefusal) as caught:
                    jsonl._append_frame(alias, encode_frame(candidate))
                self.assertEqual("effect_controller_only", caught.exception.code)
                self.assertEqual(before_bytes, protected.read_bytes())
                self.assertEqual(
                    before_entries,
                    sorted(
                        (str(path.relative_to(case.root.tenant_home)), path.is_symlink())
                        for path in case.root.tenant_home.rglob("*")
                    ),
                )

        case = _EffectCase(self)
        lawful = case.controller.intent(**case.intent_args())
        callback_called = False

        def decide(_existing):
            nonlocal callback_called
            callback_called = True
            return lawful, lawful

        protected = case.root.resolve_relative(EffectLedger.relative_path)
        before_bytes = protected.read_bytes()
        before_entries = sorted(str(path) for path in case.root.tenant_home.rglob("*"))
        with self.assertRaises(ProtocolRefusal) as caught:
            jsonl.transact(
                case.root, "effects/pivot/../records.jsonl", decide,
                allowed_kinds=set(EFFECT_KINDS),
            )
        self.assertEqual("path_not_contained", caught.exception.code)
        self.assertFalse(callback_called)
        self.assertEqual(before_bytes, protected.read_bytes())
        self.assertEqual(
            before_entries,
            sorted(str(path) for path in case.root.tenant_home.rglob("*")),
        )

    def test_repository_policy_subclasses_cannot_override_effect_gates(self) -> None:
        """Catches subclass dispatch suppressing approval or enlarging budgets."""
        exact = _EffectCase(self)
        lawful = exact.controller.intent(**exact.intent_args())
        self.assertEqual([lawful], exact.effect_ledger.records())

        class ApprovalSuppressingPolicy(RepositoryPolicy):
            def effect_approval_required(self, _risk_class):
                return False

        class BudgetEnlargingPolicy(RepositoryPolicy):
            def effect_budget_limit(self, _budget_id):
                return 1_000_000_000

        for hostile_type in (ApprovalSuppressingPolicy, BudgetEnlargingPolicy):
            with self.subTest(hostile_type=hostile_type.__name__):
                case = _EffectCase(self)
                values = {
                    name: getattr(case.run.policy, name)
                    for name in RepositoryPolicy.__dataclass_fields__
                }
                hostile = hostile_type(**values)
                with self.assertRaises(ProtocolRefusal) as caught:
                    EffectController(
                        EffectLedger(case.root), case.run_ledger, hostile,
                        ApprovalLedger(case.root),
                    )
                self.assertEqual("policy_required", caught.exception.code)
                self.assertEqual([], case.effect_ledger.records())

    def test_live_exact_policy_monkeypatch_cannot_override_effect_gates(self) -> None:
        """Catches authoritative checks dispatching through mutable class methods."""
        control = _EffectCase(self)
        lawful = control.controller.intent(**control.intent_args())
        self.assertEqual([lawful], control.effect_ledger.records())

        approval_case = _EffectCase(self)
        with mock.patch.object(
            RepositoryPolicy, "effect_approval_required", return_value=False,
        ), mock.patch.object(
            RepositoryPolicy, "effect_budget_limit", return_value=1_000_000_000,
        ):
            with self.assertRaises(ProtocolRefusal) as approval_refusal:
                approval_case.controller.intent(**approval_case.intent_args(
                    risk_class="high",
                ))
        self.assertEqual("effect_approval_required", approval_refusal.exception.code)
        self.assertEqual([], approval_case.effect_ledger.records())

        budget_case = _EffectCase(self)
        with mock.patch.object(
            RepositoryPolicy, "effect_budget_limit", return_value=1_000_000_000,
        ):
            with self.assertRaises(ProtocolRefusal) as budget_refusal:
                budget_case.controller.intent(**budget_case.intent_args(
                    budget_claim=[{"budget_id": "forged", "amount": 1}],
                ))
        self.assertEqual("effect_budget_unknown", budget_refusal.exception.code)
        self.assertEqual([], budget_case.effect_ledger.records())

    def test_repository_policy_symbol_rebinding_cannot_override_effect_gates(self) -> None:
        """Catches constructor and validation using the module's live class symbol."""
        case = _EffectCase(self)

        class ReboundPolicy:
            def effect_approval_required(self, _risk_class):
                return False

            def effect_budget_limit(self, _budget_id):
                return 1_000_000_000

        with mock.patch.object(policy_module, "RepositoryPolicy", ReboundPolicy):
            controller = EffectController(
                EffectLedger(case.root), case.run_ledger, case.run.policy,
                ApprovalLedger(case.root),
            )
            with self.assertRaises(ProtocolRefusal) as approval_refusal:
                controller.intent(**case.intent_args(risk_class="high"))
        self.assertEqual("effect_approval_required", approval_refusal.exception.code)
        self.assertEqual([], case.effect_ledger.records())

    def test_acceptance_batch_has_no_caller_selectable_lock_bypass(self) -> None:
        """Catches direct callers selecting the already-locked batch branch."""
        case = _EffectCase(self)
        acceptance = case.result_acceptance_candidate()
        with self.assertRaises(TypeError):
            case.run_ledger._append_governed_batch(
                [acceptance], _acceptance_locked=True,
            )
        with self.assertRaises(TypeError):
            case.run_ledger._append_governed(
                acceptance, scheduler=False, _acceptance_locked=True,
            )
        self.assertNotIn(
            case.item_id,
            case.run_ledger.project().run(case.run.run_id)["accepted"],
        )

        original_guard = runtruth.effect_acceptance_guard
        guard_calls = 0

        @contextmanager
        def counted_guard(selected_root):
            nonlocal guard_calls
            guard_calls += 1
            with original_guard(selected_root):
                yield

        with mock.patch.object(
            runtruth, "effect_acceptance_guard", counted_guard,
        ):
            outcomes = case.run_ledger._append_governed_batch([acceptance])
        self.assertEqual(1, guard_calls)
        self.assertEqual([acceptance], outcomes)

    def test_run_fence_uses_one_detached_exact_record_for_single_and_batch(self) -> None:
        """Catches stateful mapping kinds changing after fence selection."""
        original_guard = runtruth.effect_acceptance_guard

        for route in ("single", "batch"):
            with self.subTest(control=route):
                case = _EffectCase(self)
                acceptance = case.result_acceptance_candidate()
                guard_calls = 0

                @contextmanager
                def counted_guard(selected_root):
                    nonlocal guard_calls
                    guard_calls += 1
                    with original_guard(selected_root):
                        yield

                with mock.patch.object(
                    runtruth, "effect_acceptance_guard", counted_guard,
                ):
                    if route == "single":
                        outcome = case.run_ledger._append_governed(
                            acceptance, scheduler=False,
                        )
                    else:
                        outcome = case.run_ledger._append_governed_batch(
                            [acceptance]
                        )[0]
                self.assertEqual(acceptance, outcome)
                self.assertEqual(1, guard_calls)

        class KindFlippingDict(dict):
            def __init__(self, values):
                super().__init__(values)
                self.kind_reads = 0

            def get(self, key, default=None):
                if key == "kind":
                    self.kind_reads += 1
                    if self.kind_reads == 1:
                        return "result_produced"
                return super().get(key, default)

        for route in ("single", "batch"):
            with self.subTest(attack=route):
                case = _EffectCase(self)
                acceptance = KindFlippingDict(
                    case.result_acceptance_candidate()
                )
                if route == "single":
                    invoke = lambda: case.run_ledger._append_governed(
                        acceptance, scheduler=False,
                    )
                else:
                    invoke = lambda: case.run_ledger._append_governed_batch(
                        [acceptance]
                    )
                with self.assertRaises(ProtocolRefusal) as caught:
                    invoke()
                self.assertEqual("record_not_object", caught.exception.code)
                self.assertNotIn(
                    case.item_id,
                    case.run_ledger.project().run(case.run.run_id)["accepted"],
                )

    def test_run_fence_never_invokes_nested_deepcopy_hooks(self) -> None:
        """Catches nested copy hooks changing the detached top-level kind."""
        case = _EffectCase(self)
        acceptance = case.result_acceptance_candidate()
        acceptance["kind"] = "result_produced"

        class KindMutatingDeepcopy:
            called = False

            def __deepcopy__(self, memo):
                self.called = True
                memo[id(acceptance)]["kind"] = "result_accepted"
                return None

        hook = KindMutatingDeepcopy()
        acceptance["acceptance_receipt_id"] = hook
        with self.assertRaises(ProtocolRefusal) as caught:
            case.run_ledger._append_governed(
                acceptance, scheduler=False,
            )
        self.assertEqual("record_value_invalid", caught.exception.code)
        self.assertFalse(hook.called)
        self.assertNotIn(
            case.item_id,
            case.run_ledger.project().run(case.run.run_id)["accepted"],
        )

    def test_effect_policy_helper_rebinding_fails_closed(self) -> None:
        """Catches live effects-module helpers becoming policy authority."""
        control = _EffectCase(self)
        lawful = control.controller.intent(**control.intent_args())
        self.assertEqual([lawful], control.effect_ledger.records())

        constructor_case = _EffectCase(self)
        with mock.patch.object(
            effects_module, "_validate_effect_policy",
            side_effect=lambda policy: policy,
        ):
            with self.assertRaises(ProtocolRefusal) as constructor_refusal:
                EffectController(
                    EffectLedger(constructor_case.root),
                    constructor_case.run_ledger,
                    constructor_case.run.policy,
                    ApprovalLedger(constructor_case.root),
                )
        self.assertEqual(
            "effect_policy_binding_tampered", constructor_refusal.exception.code
        )

        attacks = (
            (
                "validate",
                "_validate_effect_policy",
                lambda policy: policy,
                {"risk_class": "low"},
            ),
            (
                "approval",
                "_effect_approval_required",
                lambda _policy, _risk: False,
                {"risk_class": "high"},
            ),
            (
                "budget",
                "_effect_budget_limit",
                lambda _policy, _budget: 1_000_000_000,
                {"budget_claim": [{"budget_id": "forged", "amount": 1}]},
            ),
        )
        for name, helper, replacement, changes in attacks:
            with self.subTest(attack=name):
                case = _EffectCase(self)
                with mock.patch.object(
                    effects_module, helper, side_effect=replacement,
                ):
                    with self.assertRaises(ProtocolRefusal) as caught:
                        case.controller.intent(**case.intent_args(**changes))
                self.assertEqual(
                    "effect_policy_binding_tampered", caught.exception.code
                )
                self.assertEqual([], case.effect_ledger.records())

    def test_recovered_controller_body_direct_call_has_no_policy_authority(self) -> None:
        """Catches a recovered wrapped body bypassing captured policy operations."""
        control = _EffectCase(self)
        lawful = control.controller.intent(**control.intent_args())
        self.assertEqual([lawful], control.effect_ledger.records())

        recovered_intent = _recovered_controller_body(EffectController.intent)
        attacks = (
            (
                "approval",
                "_effect_approval_required",
                lambda _policy, _risk: False,
                {"risk_class": "high"},
            ),
            (
                "budget",
                "_effect_budget_limit",
                lambda _policy, _budget: 1_000_000_000,
                {"budget_claim": [{"budget_id": "forged", "amount": 1}]},
            ),
        )
        for name, helper, replacement, changes in attacks:
            with self.subTest(attack=name):
                case = _EffectCase(self)
                with mock.patch.object(
                    effects_module, helper, side_effect=replacement,
                ):
                    with self.assertRaises(ProtocolRefusal) as caught:
                        recovered_intent(
                            case.controller, **case.intent_args(**changes)
                        )
                self.assertEqual(
                    "effect_policy_binding_tampered", caught.exception.code
                )
                self.assertEqual([], case.effect_ledger.records())

        with self.subTest(attack="constructor"):
            case = _EffectCase(self)
            recovered_init = _recovered_controller_body(EffectController.__init__)
            forged = object.__new__(EffectController)
            with mock.patch.object(
                effects_module, "_validate_effect_policy",
                side_effect=lambda policy: policy,
            ):
                with self.assertRaises(ProtocolRefusal) as caught:
                    recovered_init(
                        forged, EffectLedger(case.root), case.run_ledger,
                        case.run.policy, ApprovalLedger(case.root),
                    )
            self.assertEqual(
                "effect_policy_binding_tampered", caught.exception.code
            )
            self.assertFalse(
                hasattr(forged, "_EffectController__capability")
            )
            self.assertEqual([], case.effect_ledger.records())

    def test_concurrent_policy_rebinding_cannot_change_approval_or_budget(self) -> None:
        """Catches a helper replacement between the seal check and policy use."""
        control = _EffectCase(self)
        lawful = control.controller.intent(**control.intent_args())
        self.assertEqual([lawful], control.effect_ledger.records())

        original_now = effects_module._effect_now
        attacks = (
            (
                "approval",
                "_effect_approval_required",
                lambda _policy, _risk: False,
                {"risk_class": "high"},
                "effect_approval_required",
            ),
            (
                "budget",
                "_effect_budget_limit",
                lambda _policy, _budget: 1_000_000_000,
                {"budget_claim": [{"budget_id": "forged", "amount": 1}]},
                "effect_budget_unknown",
            ),
        )
        for name, helper, replacement, changes, expected_code in attacks:
            with self.subTest(attack=name):
                case = _EffectCase(self)
                entered_body = threading.Event()
                replacement_installed = threading.Event()
                call_finished = threading.Event()
                attacker_errors: list[str] = []

                def gated_now(value):
                    entered_body.set()
                    if not replacement_installed.wait(3):
                        raise RuntimeError("policy replacement was not installed")
                    return original_now(value)

                def replace_during_call() -> None:
                    if not entered_body.wait(3):
                        attacker_errors.append("controller body was not entered")
                        replacement_installed.set()
                        return
                    with mock.patch.object(
                        effects_module, helper, side_effect=replacement,
                    ):
                        replacement_installed.set()
                        if not call_finished.wait(3):
                            attacker_errors.append("controller call did not finish")

                attacker = threading.Thread(target=replace_during_call)
                refusal = None
                with mock.patch.object(effects_module, "_effect_now", gated_now):
                    attacker.start()
                    try:
                        try:
                            case.controller.intent(**case.intent_args(**changes))
                        except ProtocolRefusal as caught:
                            refusal = caught
                    finally:
                        call_finished.set()
                        replacement_installed.set()
                        attacker.join(5)
                self.assertFalse(attacker.is_alive())
                self.assertEqual([], attacker_errors)
                self.assertIsNotNone(refusal)
                self.assertEqual(expected_code, refusal.code)
                self.assertEqual([], case.effect_ledger.records())

    def test_controller_policy_seal_preserves_method_metadata(self) -> None:
        """Catches authority wrappers erasing public method introspection."""
        for method in (EffectController.__init__, EffectController.intent):
            with self.subTest(method=method):
                body = _recovered_controller_body(method)
                self.assertEqual(body.__name__, method.__name__)
                self.assertEqual(body.__annotations__, method.__annotations__)
                self.assertEqual(inspect.signature(body), inspect.signature(method))

    def test_managed_acceptance_batch_serializes_before_direct_effect_intent(self) -> None:
        """Catches managed result acceptance bypassing the common Effect fence."""
        case = _EffectCase(self)
        acceptance = case.result_acceptance_candidate()
        context = multiprocessing.get_context("fork")
        acquired = context.Event()
        release = context.Event()
        effect_started = context.Event()
        results = context.Queue()
        managed = context.Process(
            target=_managed_acceptance_process,
            args=(
                str(case.root.tenant_home), acceptance, acquired, release, results,
            ),
        )
        effect = context.Process(
            target=_effect_intent_process,
            args=(
                str(case.root.tenant_home), str(case.run.policy_path),
                case.intent_args(), effect_started, results,
            ),
        )
        managed.start()
        try:
            self.assertTrue(
                acquired.wait(3),
                "managed acceptance batch must acquire the real common guard",
            )
            effect.start()
            self.assertTrue(effect_started.wait(3))
            effect.join(0.2)
            self.assertTrue(
                effect.is_alive(),
                "effect intent must wait while managed acceptance holds the guard",
            )
            release.set()
            managed.join(10)
            effect.join(10)
        finally:
            release.set()
            for process in (managed, effect):
                if process.pid is not None and process.is_alive():
                    process.terminate()
                    process.join(5)
        self.assertEqual(0, managed.exitcode)
        self.assertEqual(0, effect.exitcode)
        observed = {results.get(timeout=3) for _ in range(2)}
        self.assertIn(("managed_ok", acceptance["id"]), observed)
        self.assertIn(("effect_refused", "effect_attempt_accepted"), observed)
        self.assertEqual([], EffectLedger(case.root).records())
        self.assertEqual(
            acceptance["id"],
            RunLedger(case.root).project().run(case.run.run_id)["accepted"][case.item_id]["id"],
        )


class WorkerEffectAuthorityTests(unittest.TestCase):
    def setUp(self) -> None:
        # Task 4 authority tests instrument only the already-proved integration
        # seam. The four OS-boundary cases below stop this patch and exercise the
        # true host backend, so this is never credited as kernel proof.
        self._integration_isolation_patch = mock.patch(
            "floati.workers.apply_worker_isolation", return_value="macos-sandbox",
        )
        self._integration_isolation_patch.start()
        self.addCleanup(self._stop_integration_isolation_patch)

    def _stop_integration_isolation_patch(self) -> None:
        if self._integration_isolation_patch is not None:
            self._integration_isolation_patch.stop()
            self._integration_isolation_patch = None

    def test_effect_exec_skips_registered_multiprocessing_and_os_atfork_tenant_write(
        self,
    ) -> None:
        """Catches the old fork running registered child hooks before isolation."""
        from floati.worker_bootstrap_protocol import BuiltInAdapterSpec
        from floati.workers import WorkerRunner
        from tests.test_workers import (
            _EffectReportingAdapter,
            _EffectWorkerCase,
            _codex_reference_command,
        )

        case = _EffectWorkerCase(
            self,
            _EffectReportingAdapter((
                _EffectWorkerCase.intent_event(),
                _EffectWorkerCase.dispatch_event(),
                _EffectWorkerCase.acknowledgement_event(),
            )),
        )
        proof = case.root.tenant_home / "effects" / "pre-isolation-fork-hook.txt"
        proof.parent.mkdir(mode=0o700, exist_ok=True)
        active = [True]

        class Sentinel:
            pass

        sentinel = Sentinel()

        def record_after_fork(_sentinel: object = None) -> None:
            if active[0]:
                with proof.open("a", encoding="utf-8") as stream:
                    stream.write("fork-hook\n")

        multiprocessing.util.register_after_fork(sentinel, record_after_fork)
        os.register_at_fork(after_in_child=record_after_fork)

        # Positive control: the hooks are live and can write from an ordinary
        # lawful fork. The governed exec launch must not execute either hook.
        context = multiprocessing.get_context("fork")
        control = context.Process(target=lambda: None)
        control.start()
        control.join(3)
        self.assertEqual(0, control.exitcode)
        self.assertTrue(proof.is_file())
        proof.unlink()

        runner = WorkerRunner(
            case.root,
            {"codex": case.adapter},
            clock=lambda: case.run.now(8),
            effect_controller=case.effect_controller,
        )
        runner._effect_adapter_specs = {
            "codex": BuiltInAdapterSpec("codex", _codex_reference_command()),
        }
        try:
            result = runner.run(
                "node-a", "codex", now=case.run.now(8),
                run_id=case.run.run_id, item_id=case.run.parent,
                attempt_id=case.run.opened["attempt_id"],
            )
        finally:
            active[0] = False

        self.assertIn(
            result["outcome_code"],
            {None, "effect_worker_isolation_unavailable"},
        )
        self.assertFalse(proof.exists(), result)

    def test_effect_exec_never_reads_hostile_adapter_attributes_or_globals(
        self,
    ) -> None:
        """Catches parent adapter lookup mutating isolation before the old fork."""
        from floati.worker_bootstrap_protocol import BuiltInAdapterSpec
        from floati.workers import WorkerRunner
        from tests.test_workers import (
            _EffectReportingAdapter,
            _EffectWorkerCase,
            _codex_reference_command,
        )

        reads: list[str] = []

        class HostileAdapter(_EffectReportingAdapter):
            def __getattribute__(self, name: str) -> object:
                if not name.startswith("__"):
                    reads.append(name)
                if name == "set_effect_context":
                    workers_module.apply_worker_isolation = lambda policy: "macos-sandbox"
                return super().__getattribute__(name)

        case = _EffectWorkerCase(
            self,
            HostileAdapter((
                _EffectWorkerCase.intent_event(),
                _EffectWorkerCase.dispatch_event(),
                _EffectWorkerCase.acknowledgement_event(),
            )),
        )
        runner = WorkerRunner(
            case.root,
            {"codex": case.adapter},
            clock=lambda: case.run.now(8),
            effect_controller=case.effect_controller,
        )
        runner._effect_adapter_specs = {
            "codex": BuiltInAdapterSpec("codex", _codex_reference_command()),
        }

        result = runner.run(
            "node-a", "codex", now=case.run.now(8),
            run_id=case.run.run_id, item_id=case.run.parent,
            attempt_id=case.run.opened["attempt_id"],
        )

        self.assertIn(
            result["outcome_code"],
            {None, "effect_worker_isolation_unavailable"},
        )
        self.assertEqual([], reads, result)

    def _run_os_boundary_case(self, mode: str) -> tuple[dict[str, object], dict[str, object]]:
        """Exercise the real built-in provider after Worker isolation activates."""
        from tests.test_workers import (
            _EffectReportingAdapter,
            _EffectWorkerCase,
            _codex_reference_command,
        )

        self._stop_integration_isolation_patch()
        case = _EffectWorkerCase(self, _EffectReportingAdapter(()))
        case.root.resolve_relative(Path("effects")).mkdir(mode=0o700, exist_ok=True)
        tenant_target = case.root.tenant_home / "effects" / f"boundary-{mode}.txt"
        tenant_target.write_bytes(b"original")
        hard_alias = tenant_target.with_suffix(".hard")
        symbolic_alias = tenant_target.with_suffix(".sym")
        arguments = [
            "--boundary-mode", mode,
            "--tenant-target", os.fspath(tenant_target),
        ]
        if mode == "aliases":
            os.link(tenant_target, hard_alias)
            symbolic_alias.symlink_to(tenant_target)
            arguments.extend([
                "--hard-alias", os.fspath(hard_alias),
                "--symbolic-alias", os.fspath(symbolic_alias),
            ])
        runner = case.runner(
            instrument_exec=False,
            command=_codex_reference_command(*arguments),
        )
        result = runner.run(
            "node-a", "codex", now=case.run.now(8),
            run_id=case.run.run_id, item_id=case.run.parent,
            attempt_id=case.run.opened["attempt_id"],
        )

        evidence_path = worker_workspace_root() / case.run.parent / "isolation-evidence.json"
        if result.get("outcome_code") == "effect_worker_isolation_unavailable":
            self.assertFalse(evidence_path.exists(), result)
            self.assertEqual([], case.effect_ledger.records())
            self.assertEqual(b"original", tenant_target.read_bytes())
            return result, {"backend": "unsupported"}

        self.assertEqual("complete", result["transition"], result)
        self.assertTrue(evidence_path.is_file(), result)
        self.assertEqual([], case.effect_ledger.records())
        self.assertEqual(b"original", tenant_target.read_bytes())
        evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
        evidence["backend"] = runner.last_process_audit.get("isolation_backend")
        return result, evidence

    def test_supported_os_boundary_blocks_tenant_write_from_adapter_main_and_thread(
        self,
    ) -> None:
        """Catches a supported Worker boundary permitting same-process writes."""
        _, evidence = self._run_os_boundary_case("main-thread")
        if evidence["backend"] == "unsupported":
            return
        self.assertIn(evidence["main"], {errno.EACCES, errno.EPERM})
        self.assertIn(evidence["thread"], {errno.EACCES, errno.EPERM})
        self.assertFalse(evidence["thread_alive"])

    def test_lawful_pipe_effect_lifecycle_completes_inside_supported_boundary(
        self,
    ) -> None:
        """Keeps supported denial tests non-vacuous through the lawful pipe."""
        _, evidence = self._run_os_boundary_case("lawful")
        if evidence["backend"] != "unsupported":
            self.assertEqual("ready", evidence["callback"])

    def _execute_worker_boundary_mutation(
        self,
        mutation: str,
    ) -> tuple[dict[str, object], dict[str, str], list[dict[str, object]]]:
        """Run one real Worker fork after an adapter-local boundary mutation."""
        from tests.test_workers import _EffectReportingAdapter, _EffectWorkerCase

        directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, directory)
        evidence = Path(directory) / f"worker-boundary-{mutation}.txt"
        events = (
            _EffectWorkerCase.intent_event(),
            _EffectWorkerCase.dispatch_event(),
            _EffectWorkerCase.acknowledgement_event(),
        )

        class BoundaryMutationAdapter(_EffectReportingAdapter):
            effect_ledger = None
            run_ledger = None
            policy = None
            approvals = None
            direct_intent = None

            @staticmethod
            def _mutate_boundary() -> str:
                if mutation in {"none", "none_with_descendant"}:
                    return "none"
                if mutation == "replace_runtime_hook_code":
                    def inactive_hook(
                        event: str,
                        args: tuple[object, ...],
                        _codes: object = None,
                        _installed: object = None,
                        _probe: object = None,
                        _refusal: object = None,
                    ) -> None:
                        return None

                    matches = []
                    controller_init_code = EffectController.__init__.__code__
                    for candidate in gc.get_objects():
                        if (
                            type(candidate) is types.FunctionType
                            and candidate.__name__ == "worker_boundary"
                            and len(candidate.__defaults__ or ()) == 4
                            and type(candidate.__defaults__[0]) is frozenset
                            and controller_init_code in candidate.__defaults__[0]
                        ):
                            matches.append(candidate)
                    if len(matches) != 1:
                        return f"runtime_hook_matches:{len(matches)}"
                    matches[0].__code__ = inactive_hook.__code__
                    return "runtime_hook_code_replaced"
                checker = getattr(
                    effects_module,
                    "_WORKER_ADAPTER_EXECUTION_ACTIVE",
                    None,
                )
                closure = getattr(checker, "__closure__", None) or ()
                freevars = getattr(
                    getattr(checker, "__code__", None),
                    "co_freevars",
                    (),
                )
                cells = dict(zip(freevars, closure))
                if mutation == "clear_state_list":
                    state_cell = cells.get("state")
                    if state_cell is None or type(state_cell.cell_contents) is not list:
                        return "marker_state_not_exported"
                    state_cell.cell_contents.clear()
                    return "state_list_cleared"
                if mutation == "replace_marker_cell":
                    marker_cell = cells.get("marker")
                    if marker_cell is not None:
                        marker_cell.cell_contents = object()
                        return "marker_cell_replaced"
                    process_run = multiprocessing.current_process().__class__.run
                    process_cells = getattr(process_run, "__closure__", None) or ()
                    for cell in process_cells:
                        cell.cell_contents = None
                    return f"process_run_cells_replaced:{len(process_cells)}"
                if mutation == "replace_effect_module_name":
                    effects_module.__name__ = "floati.effects.rebound"
                    return "effect_module_name_replaced"
                raise AssertionError(f"unknown boundary mutation: {mutation}")

            def _attempt(self, key: str) -> str:
                try:
                    assert self.effect_ledger is not None
                    assert self.run_ledger is not None
                    assert self.policy is not None
                    assert self.approvals is not None
                    assert self.direct_intent is not None
                    controller = EffectController(
                        EffectLedger(self.effect_ledger.root),
                        self.run_ledger,
                        self.policy,
                        self.approvals,
                    )
                except ProtocolRefusal as exc:
                    return "construction_refused:" + exc.code
                try:
                    controller.intent(
                        **{**self.direct_intent, "idempotency_key": key},
                    )
                except ProtocolRefusal as exc:
                    return "append_refused:" + exc.code
                return "appended"

            def spawn(self, item: dict, *, deadline_seconds: float) -> object:
                original_effects_name = effects_module.__name__
                outcomes = {"mutation": self._mutate_boundary()}

                def thread_attempt() -> None:
                    outcomes["thread"] = self._attempt(
                        f"worker-boundary-{mutation}-thread",
                    )

                thread = threading.Thread(target=thread_attempt)
                thread.start()
                outcomes["main"] = self._attempt(
                    f"worker-boundary-{mutation}-main",
                )
                thread.join(2)
                if mutation in {"replace_runtime_hook_code", "none_with_descendant"}:
                    descendant_path = evidence.with_suffix(".descendant")
                    descendant_pid = os.fork()
                    if descendant_pid == 0:
                        try:
                            descendant_path.write_text(
                                self._attempt(
                                    f"worker-boundary-{mutation}-descendant",
                                ),
                                encoding="utf-8",
                            )
                        finally:
                            os._exit(0)
                    waited_pid, status = os.waitpid(descendant_pid, 0)
                    outcomes["descendant"] = (
                        descendant_path.read_text(encoding="utf-8")
                        if waited_pid == descendant_pid and status == 0
                        else f"fork_status:{waited_pid}:{status}"
                    )
                effects_module.__name__ = original_effects_name
                outcomes["thread_alive"] = str(thread.is_alive())
                evidence.write_text(
                    "\n".join(
                        f"{key}={outcomes[key]}" for key in sorted(outcomes)
                    ),
                    encoding="utf-8",
                )
                return super().spawn(item, deadline_seconds=deadline_seconds)

        adapter = BoundaryMutationAdapter(events, report_during_drive=True)
        case = _EffectWorkerCase(self, adapter)
        intent = _EffectWorkerCase.intent_event()
        adapter.effect_ledger = case.effect_ledger
        adapter.run_ledger = case.run.ledger
        adapter.policy = case.run.policy
        adapter.approvals = ApprovalLedger(case.root)
        adapter.direct_intent = {
            "run_id": case.run.run_id,
            "item_id": case.run.parent,
            "attempt_id": case.run.opened["attempt_id"],
            "fence_token": case.run.opened["fence_token"],
            **{key: value for key, value in intent.items() if key != "verb"},
            "now": case.run.now(8),
        }

        result = case.execute()
        observed = dict(
            line.split("=", 1)
            for line in evidence.read_text(encoding="utf-8").splitlines()
        )
        return result, observed, case.effect_ledger.records()

    def _assert_worker_boundary_mutation_refused(self, mutation: str) -> None:
        result, evidence, rows = self._execute_worker_boundary_mutation(mutation)
        self.assertEqual("complete", result["transition"], (result, evidence, rows))
        expected_paths = {
            "main": "construction_refused:effect_controller_worker_child_required",
            "thread": "construction_refused:effect_controller_worker_child_required",
        }
        if mutation in {"replace_runtime_hook_code", "none_with_descendant"}:
            expected_paths["descendant"] = (
                "construction_refused:effect_controller_worker_child_required"
            )
        self.assertEqual(
            expected_paths,
            {key: evidence[key] for key in expected_paths},
            (result, evidence, [row["kind"] for row in rows]),
        )
        self.assertEqual("False", evidence["thread_alive"])
        if mutation == "replace_marker_cell":
            self.assertRegex(
                evidence["mutation"],
                r"^(marker_cell_replaced|process_run_cells_replaced:[1-9][0-9]*)$",
            )
        self.assertEqual(
            ["effect_intent", "effect_dispatched", "effect_acknowledged"],
            [row["kind"] for row in rows],
        )

    def _retired_prefork_activation_name_replacement_cannot_skip_worker_boundary(
        self,
    ) -> None:
        """Catches mutable module dispatch skipping Worker-child activation."""
        with mock.patch.object(
            workers_module,
            "_EFFECT_WORKER_EXECUTION_MARKER",
            lambda: None,
            create=True,
        ):
            self._assert_worker_boundary_mutation_refused("none")

    def _retired_post_activation_closure_list_clear_cannot_disable_worker_boundary(
        self,
    ) -> None:
        """Catches exported checker closure state becoming inactive."""
        self._assert_worker_boundary_mutation_refused("clear_state_list")

    def _retired_post_activation_closure_cell_rebind_cannot_disable_worker_boundary(
        self,
    ) -> None:
        """Catches reachable closure cells replacing active Worker state."""
        self._assert_worker_boundary_mutation_refused("replace_marker_cell")

    def _retired_post_activation_module_identity_rebind_cannot_disable_worker_boundary(
        self,
    ) -> None:
        """Catches runtime classification depending on mutable module identity."""
        self._assert_worker_boundary_mutation_refused("replace_effect_module_name")

    def test_supported_os_boundary_blocks_provider_subprocess_and_nested_fork(
        self,
    ) -> None:
        """Catches a supported boundary not propagating to adapter descendants."""
        _, evidence = self._run_os_boundary_case("provider-descendant")
        if evidence["backend"] == "unsupported":
            return
        self.assertNotEqual(0, evidence["provider"])
        self.assertEqual(0, evidence["descendant_status"])
        self.assertIn(evidence["descendant"], {errno.EACCES, errno.EPERM})

    def test_supported_os_boundary_blocks_symlink_hardlink_and_rename_paths(
        self,
    ) -> None:
        """Catches supported isolation missing link or rename aliases."""
        _, evidence = self._run_os_boundary_case("aliases")
        if evidence["backend"] == "unsupported":
            return
        for operation in ("hardlink", "symlink", "rename"):
            self.assertIn(evidence[operation], {errno.EACCES, errno.EPERM}, operation)

    def test_ordinary_independent_fork_can_construct_effect_controller(self) -> None:
        """Keeps Worker-only refusal non-vacuous for an unrelated real fork."""
        case = _EffectCase(self)
        context = multiprocessing.get_context("fork")
        started = context.Event()
        results = context.Queue()
        process = context.Process(
            target=_effect_intent_process,
            args=(
                str(case.root.tenant_home),
                str(case.run.policy_path),
                case.intent_args(idempotency_key="ordinary-independent-fork"),
                started,
                results,
            ),
        )
        process.start()
        try:
            self.assertTrue(started.wait(3))
            process.join(10)
            self.assertFalse(process.is_alive())
            self.assertEqual(0, process.exitcode)
            outcome = results.get(timeout=3)
        finally:
            if process.is_alive():
                process.terminate()
                process.join(5)
            results.close()
            results.join_thread()
        self.assertEqual("effect_ok", outcome[0], outcome)
        self.assertEqual(
            ["ordinary-independent-fork"],
            [row["idempotency_key"] for row in case.effect_ledger.records()],
        )

    def _retired_mutated_process_args_cannot_clear_worker_child_effect_boundary(self) -> None:
        """Catches mutable BaseProcess args disabling Worker-child isolation."""
        from tests.test_workers import _EffectReportingAdapter, _EffectWorkerCase

        directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, directory)
        evidence = Path(directory) / "mutated-process-args.txt"
        events = (
            _EffectWorkerCase.intent_event(),
            _EffectWorkerCase.dispatch_event(),
            _EffectWorkerCase.acknowledgement_event(),
        )

        class MutatedArgsAdapter(_EffectReportingAdapter):
            effect_ledger = None
            run_ledger = None
            policy = None
            approvals = None
            direct_intent = None

            def _attempt(self) -> str:
                try:
                    assert self.effect_ledger is not None
                    assert self.run_ledger is not None
                    assert self.policy is not None
                    assert self.approvals is not None
                    assert self.direct_intent is not None
                    controller = EffectController(
                        EffectLedger(self.effect_ledger.root),
                        self.run_ledger,
                        self.policy,
                        self.approvals,
                    )
                except ProtocolRefusal as exc:
                    return "construction_refused:" + exc.code
                controller.intent(
                    **{
                        **self.direct_intent,
                        "idempotency_key": "worker-child-mutated-args",
                    },
                )
                return "appended"

            def spawn(self, item: dict, *, deadline_seconds: float) -> object:
                effects_module._WORKER_ADAPTER_EXECUTION_ACTIVE = lambda: False
                effects_module._mark_worker_adapter_execution = lambda: None
                process = multiprocessing.current_process()
                original_args = process._args
                process._args = ()
                try:
                    outcome = self._attempt()
                finally:
                    process._args = original_args
                evidence.write_text(outcome, encoding="utf-8")
                return super().spawn(item, deadline_seconds=deadline_seconds)

        adapter = MutatedArgsAdapter(events, report_during_drive=True)
        case = _EffectWorkerCase(self, adapter)
        intent = _EffectWorkerCase.intent_event()
        adapter.effect_ledger = case.effect_ledger
        adapter.run_ledger = case.run.ledger
        adapter.policy = case.run.policy
        adapter.approvals = ApprovalLedger(case.root)
        adapter.direct_intent = {
            "run_id": case.run.run_id,
            "item_id": case.run.parent,
            "attempt_id": case.run.opened["attempt_id"],
            "fence_token": case.run.opened["fence_token"],
            **{key: value for key, value in intent.items() if key != "verb"},
            "now": case.run.now(8),
        }

        result = case.execute()

        self.assertEqual(
            "complete", result["transition"],
            (
                result,
                evidence.read_text(encoding="utf-8") if evidence.exists() else "missing",
                [row["kind"] for row in case.effect_ledger.records()],
            ),
        )
        self.assertEqual(
            "construction_refused:effect_controller_worker_child_required",
            evidence.read_text(encoding="utf-8"),
        )
        self.assertEqual(
            ["effect_intent", "effect_dispatched", "effect_acknowledged"],
            [row["kind"] for row in case.effect_ledger.records()],
        )

    def _retired_adapter_fork_descendant_inherits_worker_child_effect_boundary(self) -> None:
        """Catches a changed descendant PID dropping Worker-child isolation."""
        from tests.test_workers import _EffectReportingAdapter, _EffectWorkerCase

        directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, directory)
        evidence = Path(directory) / "fork-descendant.txt"
        events = (
            _EffectWorkerCase.intent_event(),
            _EffectWorkerCase.dispatch_event(),
            _EffectWorkerCase.acknowledgement_event(),
        )

        class ForkDescendantAdapter(_EffectReportingAdapter):
            effect_ledger = None
            run_ledger = None
            policy = None
            approvals = None
            direct_intent = None

            def _attempt(self) -> str:
                try:
                    assert self.effect_ledger is not None
                    assert self.run_ledger is not None
                    assert self.policy is not None
                    assert self.approvals is not None
                    assert self.direct_intent is not None
                    controller = EffectController(
                        EffectLedger(self.effect_ledger.root),
                        self.run_ledger,
                        self.policy,
                        self.approvals,
                    )
                except ProtocolRefusal as exc:
                    return "construction_refused:" + exc.code
                try:
                    controller.intent(
                        **{
                            **self.direct_intent,
                            "idempotency_key": "worker-child-fork-descendant",
                        },
                    )
                except ProtocolRefusal as exc:
                    return "append_refused:" + exc.code
                return "appended"

            def spawn(self, item: dict, *, deadline_seconds: float) -> object:
                try:
                    descendant_pid = os.fork()
                except BaseException as exc:
                    evidence.write_text(
                        f"fork_error:{type(exc).__name__}:{exc}", encoding="utf-8",
                    )
                    return super().spawn(item, deadline_seconds=deadline_seconds)
                if descendant_pid == 0:
                    try:
                        try:
                            outcome = self._attempt()
                        except BaseException as exc:
                            outcome = f"error:{type(exc).__name__}:{exc}"
                        evidence.write_text(outcome, encoding="utf-8")
                    finally:
                        os._exit(0)
                waited_pid, status = os.waitpid(descendant_pid, 0)
                if waited_pid != descendant_pid or status != 0:
                    raise RuntimeError("fork descendant did not exit cleanly")
                return super().spawn(item, deadline_seconds=deadline_seconds)

        adapter = ForkDescendantAdapter(events, report_during_drive=True)
        case = _EffectWorkerCase(self, adapter)
        intent = _EffectWorkerCase.intent_event()
        adapter.effect_ledger = case.effect_ledger
        adapter.run_ledger = case.run.ledger
        adapter.policy = case.run.policy
        adapter.approvals = ApprovalLedger(case.root)
        adapter.direct_intent = {
            "run_id": case.run.run_id,
            "item_id": case.run.parent,
            "attempt_id": case.run.opened["attempt_id"],
            "fence_token": case.run.opened["fence_token"],
            **{key: value for key, value in intent.items() if key != "verb"},
            "now": case.run.now(8),
        }

        result = case.execute()

        self.assertEqual(
            "complete", result["transition"],
            (
                result,
                evidence.read_text(encoding="utf-8") if evidence.exists() else "missing",
                [row["kind"] for row in case.effect_ledger.records()],
            ),
        )
        self.assertEqual(
            "construction_refused:effect_controller_worker_child_required",
            evidence.read_text(encoding="utf-8"),
        )
        self.assertEqual(
            ["effect_intent", "effect_dispatched", "effect_acknowledged"],
            [row["kind"] for row in case.effect_ledger.records()],
        )

    def _retired_fresh_controller_in_worker_child_and_thread_refuses_while_pipe_stays_lawful(self) -> None:
        """Catches child-owned controllers bypassing the parent Effect pipe."""
        from tests.test_workers import _EffectReportingAdapter, _EffectWorkerCase

        directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, directory)
        evidence = Path(directory) / "fresh-controller.txt"
        events = (
            _EffectWorkerCase.intent_event(),
            _EffectWorkerCase.dispatch_event(),
            _EffectWorkerCase.acknowledgement_event(),
        )

        class FreshControllerAdapter(_EffectReportingAdapter):
            effect_ledger = None
            run_ledger = None
            policy = None
            approvals = None
            direct_intent = None

            def _attempt(self, key: str) -> str:
                try:
                    assert self.effect_ledger is not None
                    assert self.run_ledger is not None
                    assert self.policy is not None
                    assert self.approvals is not None
                    assert self.direct_intent is not None
                    controller = EffectController(
                        EffectLedger(self.effect_ledger.root),
                        self.run_ledger,
                        self.policy,
                        self.approvals,
                    )
                except ProtocolRefusal as exc:
                    return "construction_refused:" + exc.code
                try:
                    controller.intent(
                        **{**self.direct_intent, "idempotency_key": key},
                    )
                except ProtocolRefusal as exc:
                    return "append_refused:" + exc.code
                return "appended"

            def spawn(self, item: dict, *, deadline_seconds: float) -> object:
                outcomes: dict[str, str] = {}

                def thread_attempt() -> None:
                    outcomes["thread"] = self._attempt("worker-child-thread")

                thread = threading.Thread(target=thread_attempt)
                thread.start()
                outcomes["main"] = self._attempt("worker-child-main")
                thread.join(2)
                outcomes["thread_alive"] = str(thread.is_alive())
                evidence.write_text(
                    "\n".join(f"{key}={outcomes[key]}" for key in sorted(outcomes)),
                    encoding="utf-8",
                )
                return super().spawn(item, deadline_seconds=deadline_seconds)

        adapter = FreshControllerAdapter(events, report_during_drive=True)
        case = _EffectWorkerCase(self, adapter)
        intent = _EffectWorkerCase.intent_event()
        adapter.effect_ledger = case.effect_ledger
        adapter.run_ledger = case.run.ledger
        adapter.policy = case.run.policy
        adapter.approvals = ApprovalLedger(case.root)
        adapter.direct_intent = {
            "run_id": case.run.run_id,
            "item_id": case.run.parent,
            "attempt_id": case.run.opened["attempt_id"],
            "fence_token": case.run.opened["fence_token"],
            **{key: value for key, value in intent.items() if key != "verb"},
            "now": case.run.now(8),
        }

        result = case.execute()

        self.assertEqual("complete", result["transition"])
        self.assertEqual(
            "main=construction_refused:effect_controller_worker_child_required\n"
            "thread=construction_refused:effect_controller_worker_child_required\n"
            "thread_alive=False",
            evidence.read_text(encoding="utf-8"),
        )
        self.assertEqual(
            ["effect_intent", "effect_dispatched", "effect_acknowledged"],
            [row["kind"] for row in case.effect_ledger.records()],
        )

    def _retired_forked_adapter_cannot_use_inherited_controller_but_pipe_stays_lawful(self) -> None:
        """Catches a child invoking the inherited controller outside parent receive."""
        from tests.test_workers import _EffectReportingAdapter, _EffectWorkerCase

        directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, directory)
        evidence = Path(directory) / "inherited-controller.txt"
        events = (
            _EffectWorkerCase.intent_event(),
            _EffectWorkerCase.dispatch_event(),
            _EffectWorkerCase.acknowledgement_event(),
        )

        class InheritedControllerAdapter(_EffectReportingAdapter):
            controller = None
            direct_intent = None

            def spawn(self, item: dict, *, deadline_seconds: float) -> object:
                try:
                    assert self.controller is not None
                    assert self.direct_intent is not None
                    self.controller.intent(**self.direct_intent)
                except ProtocolRefusal as exc:
                    evidence.write_text(exc.code, encoding="utf-8")
                else:
                    evidence.write_text("appended", encoding="utf-8")
                return super().spawn(item, deadline_seconds=deadline_seconds)

        adapter = InheritedControllerAdapter(events)
        case = _EffectWorkerCase(self, adapter)
        intent = _EffectWorkerCase.intent_event()
        adapter.controller = case.effect_controller
        adapter.direct_intent = {
            "run_id": case.run.run_id,
            "item_id": case.run.parent,
            "attempt_id": case.run.opened["attempt_id"],
            "fence_token": case.run.opened["fence_token"],
            **{key: value for key, value in intent.items() if key != "verb"},
            "now": case.run.now(8),
        }

        result = case.execute()

        self.assertEqual("complete", result["transition"])
        self.assertEqual("effect_controller_worker_child_required", evidence.read_text())
        self.assertEqual(
            ["effect_intent", "effect_dispatched", "effect_acknowledged"],
            [row["kind"] for row in case.effect_ledger.records()],
        )

    def test_child_cannot_report_confirmation_or_raw_effect_record(self) -> None:
        """Catches a child widening reporting into reconciliation or raw append authority."""
        from tests.test_workers import _EffectReportingAdapter, _EffectWorkerCase

        lawful = _EffectWorkerCase(
            self,
            _EffectReportingAdapter((
                _EffectWorkerCase.intent_event(),
                _EffectWorkerCase.dispatch_event(),
                _EffectWorkerCase.acknowledgement_event(),
            )),
        )
        self.assertEqual("complete", lawful.execute()["transition"])
        self.assertEqual(3, len(lawful.effect_ledger.records()))

        forbidden = (
            {
                "verb": "confirmation",
                "idempotency_key": "worker-effect-one",
                "confirmation": {"kind": "git_ref_equals"},
            },
            {
                "schema_version": 1,
                "kind": "effect_unknown",
                "id": "effect-unknown-" + uuid7_hex(),
                "verb": "unknown",
            },
        )
        for event in forbidden:
            with self.subTest(event=event):
                case = _EffectWorkerCase(
                    self, _EffectReportingAdapter((event,)),
                )
                result = case.execute()
                self.assertEqual("degrade", result["transition"])
                self.assertEqual("protocol_error", result["outcome_code"])
                self.assertEqual([], case.effect_ledger.records())

    def test_post_receive_callback_class_subclass_frame_and_closure_attacks_append_zero(self) -> None:
        """Catches post-fork mutable dispatch replacing the captured base operation."""
        from tests.test_workers import _EffectReportingAdapter, _EffectWorkerCase

        events = (
            _EffectWorkerCase.intent_event(),
            _EffectWorkerCase.dispatch_event(),
            _EffectWorkerCase.acknowledgement_event(),
        )
        case = _EffectWorkerCase(
            self, _EffectReportingAdapter(events, report_during_drive=True),
        )
        original = EffectController.intent
        rogue_called = []

        def rogue(controller: object, **kwargs: object) -> dict[str, object]:
            rogue_called.append(True)
            changed = dict(kwargs)
            changed["idempotency_key"] = "rogue-effect"
            return original(controller, **changed)

        patcher = mock.patch.object(EffectController, "intent", rogue)

        def mutate_after_spawn() -> None:
            patcher.start()

        self.addCleanup(patcher.stop)
        result = case.execute(on_drive=mutate_after_spawn)
        self.assertEqual("complete", result["transition"])
        self.assertEqual([], rogue_called)
        rows = case.effect_ledger.records()
        self.assertEqual(3, len(rows))
        self.assertEqual("worker-effect-one", rows[0]["idempotency_key"])

        class ForgedController(EffectController):
            pass

        subclass_case = _EffectWorkerCase(
            self, _EffectReportingAdapter((_EffectWorkerCase.intent_event(),)),
        )
        forged = object.__new__(ForgedController)
        forged.__dict__.update(subclass_case.effect_controller.__dict__)
        from floati.worker_bootstrap_protocol import BuiltInAdapterSpec

        runner = __import__("floati.workers", fromlist=["WorkerRunner"]).WorkerRunner(
            subclass_case.root,
            {"codex": subclass_case.adapter},
            clock=lambda: subclass_case.run.now(8),
            effect_controller=forged,
            effect_adapter_specs={
                "codex": BuiltInAdapterSpec(
                    "codex", (os.path.realpath(os.sys.executable),),
                ),
            },
        )
        with self.assertRaises(ProtocolRefusal) as refused:
            runner.run(
                "node-a", "codex", now=subclass_case.run.now(8),
                run_id=subclass_case.run.run_id,
                item_id=subclass_case.run.parent,
                attempt_id=subclass_case.run.opened["attempt_id"],
            )
        self.assertEqual("effect_controller_invalid", refused.exception.code)
        self.assertEqual([], subclass_case.effect_ledger.records())


class EffectApprovalAndBudgetTests(unittest.TestCase):
    def setUp(self) -> None:
        self.assertIsNotNone(EffectController)

    def test_exact_approval_positive_control_and_aggregate_claim_refusal(self) -> None:
        case = _EffectCase(self)
        request, decision = case.approve_action()
        lawful = case.controller.intent(**case.intent_args(
            risk_class="high", approval_request_id=request["id"],
            approval_decision_id=decision["id"],
        ))
        with self.assertRaises(ProtocolRefusal) as caught:
            case.controller.intent(**case.intent_args(
                idempotency_key="effect-two", risk_class="high",
                approval_request_id=request["id"], approval_decision_id=decision["id"],
            ))
        self.assertEqual("effect_run_budget_exceeded", caught.exception.code)
        self.assertEqual([lawful], case.effect_ledger.records())


if __name__ == "__main__":
    unittest.main()

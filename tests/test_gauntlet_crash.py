from __future__ import annotations

from floati import fixture_ids as public_ids

import multiprocessing
import os
import shutil
import signal
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from floati.errors import DurabilityFailure, IntegrityFailure, ProtocolRefusal
from floati.contracts import TaskContract, contract_digest
from floati.decisions import DecisionRegister, decision_digest
from floati.events import EventLog
from floati.approvals import ApprovalLedger
from floati.effects import EffectController, EffectLedger
from floati.ids import uuid7_hex
from floati.jsonl import append_record, read_records
from floati.planes import AuthorityGrantStore
from floati.registry import Registry
from floati.root import FloatiRoot
from floati.work import WorkLog
from floati.workers import WorkerAdapterFailure, WorkerRefusals, WorkerRunner
from floati.runtruth import RunLedger
from floati.policy import RepositoryPolicy
from tests.hm3i_gauntlet_fixtures import (
    CANONICAL_RUN_KINDS,
    axis_coverage_from_traces,
    build_cancellation_trace,
    build_foc_orphan_trace,
    build_retry_stale_trace,
    build_success_trace,
)
from tests.test_spawn_groups import (
    SpawnGroupFixtures,
    _ManagedSpawnCase,
    _Task3Case,
)
from tests.test_effect_controller import _EffectCase


CRASH_EXIT = 91
NOW = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)
THREAD_NOW = datetime(2026, 8, 13, 12, 0, tzinfo=timezone.utc)
THREAD_ID = "018f3a2b-4c5d-7e8f-9a0b-1c2d3e4f5678"
THREAD_HARNESS = (
    Path(__file__).parent
    / "fixtures"
    / "codex-thread-observer"
    / "reference_harness.py"
).resolve()


def _install_append_crash(point: str) -> None:
    import floati.jsonl as jsonl

    real_write = jsonl.os.write
    real_fsync = jsonl.os.fsync
    real_ftruncate = jsonl.os.ftruncate
    fsync_calls = 0

    def crash() -> None:
        os._exit(CRASH_EXIT)

    def write(descriptor: int, data: bytes) -> int:
        if point == "before_append":
            crash()
        if point == "mid_append":
            real_write(descriptor, data[: len(data) // 2])
            crash()
        if point == "after_append":
            real_write(descriptor, data)
            crash()
        if point.startswith("rollback_"):
            return real_write(descriptor, data[: len(data) // 2])
        return real_write(descriptor, data)

    def ftruncate(descriptor: int, size: int) -> None:
        if point == "rollback_before_ftruncate":
            crash()
        real_ftruncate(descriptor, size)
        if point == "rollback_after_ftruncate":
            crash()

    def fsync(descriptor: int) -> None:
        nonlocal fsync_calls
        fsync_calls += 1
        if point in {"before_file_fsync", "rollback_before_fsync"} and fsync_calls == 1:
            crash()
        if point == "before_parent_fsync" and fsync_calls == 2:
            crash()
        real_fsync(descriptor)
        if point in {"after_file_fsync", "rollback_after_fsync"} and fsync_calls == 1:
            crash()
        if point == "after_parent_fsync" and fsync_calls == 2:
            crash()

    jsonl.os.write = write
    jsonl.os.ftruncate = ftruncate
    jsonl.os.fsync = fsync


def _send_then_crash(base: str, point: str) -> None:
    root = FloatiRoot.open(Path(base), "alpha")
    _install_append_crash(point)
    EventLog(root).send(
        public_ids.worker('alpha'),
        "bob",
        "slipway",
        "b" * 40,
        "docs/evidence/hm3h.md",
        "crash retry",
        idempotency_key="gauntlet-crash-key",
    )


def _wake_evaluate_then_crash(base: str, point: str) -> None:
    from floati.wake_hold import WakeHoldController

    root = FloatiRoot.open(Path(base), "alpha")
    _install_append_crash(point)
    WakeHoldController(root).evaluate("bob", idempotency_key="wake-crash-key")


def _new_ledger_then_crash(base: str, point: str) -> None:
    root = FloatiRoot.open(Path(base), "alpha")
    _install_append_crash(point)
    append_record(
        root,
        "new-ledger/events.jsonl",
        {
            "schema_version": 0,
            "id": "registry-018f7e9b3c117abc8def0123456789ab",
            "tenant_id": "alpha",
            "timestamp": "2026-08-01T12:00:00.000Z",
            "kind": "registry_entry",
            "node_id": "new-node",
            "role": "worker",
            "state": "active",
        },
        allowed_kinds={"registry_entry"},
    )


def _thread_observer_for_process(base: str, mode: str):
    from floati.thread_observations import ThreadObserver
    from floati.thread_source import CodexLocalThreadSource

    root = FloatiRoot.open(Path(base), "alpha")
    prefix = Path(base) / f"thread-{mode}-{os.getpid()}"
    source = CodexLocalThreadSource._for_test(
        [
            sys.executable,
            str(THREAD_HARNESS),
            mode,
            str(prefix) + "-methods",
            str(prefix) + "-params.json",
            str(prefix) + "-diagnostic.json",
        ]
    )
    return root, ThreadObserver._for_test(root, source)


def _thread_action_process(
    base: str,
    action: str,
    work_item_id: str,
    attachment_id: str | None,
    point: str | None,
    results: object | None = None,
) -> None:
    try:
        if action == "observe":
            root, observer = _thread_observer_for_process(base, "idle")
        else:
            from floati.thread_observations import ThreadObserver

            root = FloatiRoot.open(Path(base), "alpha")
            observer = ThreadObserver(root)
        if point is not None:
            _install_append_crash(point)
        if action == "register":
            row = observer.register_work_item(
                work_item_id, THREAD_ID, "observer-node", now=THREAD_NOW
            )
        elif action == "observe":
            assert attachment_id is not None
            row = observer.observe(attachment_id, now=THREAD_NOW)
        else:
            assert action == "detach" and attachment_id is not None
            row = observer.detach(
                attachment_id, "observer-node", now=THREAD_NOW
            )
        if results is not None:
            results.put(("ok", row))
    except BaseException as exc:
        if results is not None:
            results.put(
                (
                    "error",
                    f"{type(exc).__name__}:{getattr(exc, 'code', '')}:{exc}",
                )
            )


def _effect_intent_then_crash(
    base: str, policy_path: str, intent_args: dict[str, object], point: str
) -> None:
    root = FloatiRoot.open_direct_home(Path(base), create=False)
    controller = EffectController(
        EffectLedger(root), RunLedger(root),
        RepositoryPolicy.load(Path(policy_path)), ApprovalLedger(root),
    )
    _install_append_crash(point)
    controller.intent(**intent_args)


def _retry_effect_after_process_restart(
    base: str, policy_path: str, intent_args: dict[str, object], results: object
) -> None:
    root = FloatiRoot.open_direct_home(Path(base), create=False)
    controller = EffectController(
        EffectLedger(root), RunLedger(root),
        RepositoryPolicy.load(Path(policy_path)), ApprovalLedger(root),
    )
    try:
        canonical = controller.intent(**intent_args)
        results.put(("ok", canonical))
    except Exception as exc:
        results.put(("error", f"{type(exc).__name__}:{exc}"))


def _claim_then_crash(base: str) -> None:
    root = FloatiRoot.open(Path(base), "alpha")
    WorkLog(root).claim_owned_oldest(
        public_ids.worker('alpha'), "work-claims", 1, now=NOW
    )
    os._exit(CRASH_EXIT)


def _run_created_record() -> dict:
    return {"schema_version": 0, "id": "run-created-018f7e9b3c117abc8def0123456789ab", "tenant_id": "alpha",
        "timestamp": "2026-08-02T12:00:00.000Z", "kind": "run_created", "run_id": "run-018f7e9b3c117abc8def0123456789ab",
        "plan_digest": "a" * 64, "item_ids": ["work-018f7e9b3c117abc8def0123456789ab"], "dependency_edges": []}


def _run_created_then_crash(base: str, point: str) -> None:
    root = FloatiRoot.open(Path(base), "alpha")
    _install_append_crash(point)
    RunLedger(root).append(_run_created_record())


def _decision_proposal_record() -> dict:
    record = {
        "schema_version": 0,
        "id": "decision-record-018f7e9b3c117abc8def0123456789ab",
        "tenant_id": "alpha",
        "timestamp": "2026-08-08T12:00:00.000Z",
        "kind": "decision_record",
        "repository": "owner/repo",
        "decision_id": "decision-018f7e9b3c127abc8def0123456789ab",
        "scope": {"kind": "repository"},
        "statement": "Crash recovery preserves one complete proposal.",
        "status": "proposed",
        "author_authority": "worker",
        "source_artifact_ids": ["run:run-018f7e9b3c137abc8def0123456789ab"],
        "task_contract_id": None,
        "decided_by": public_ids.reviewer(),
        "supersedes": None,
    }
    record["decision_digest"] = decision_digest(record)
    return record


def _seed_decision_source(root: FloatiRoot) -> None:
    RunLedger(root).append(
        {
            "schema_version": 0,
            "id": "run-created-018f7e9b3c147abc8def0123456789ab",
            "tenant_id": root.tenant_id,
            "timestamp": "2026-08-08T12:00:00.000Z",
            "kind": "run_created",
            "run_id": "run-018f7e9b3c137abc8def0123456789ab",
            "plan_digest": "a" * 64,
            "item_ids": ["work-018f7e9b3c157abc8def0123456789ab"],
            "dependency_edges": [],
        }
    )


def _decision_proposal_then_crash(base: str, point: str) -> None:
    root = FloatiRoot.open(Path(base), "alpha")
    _seed_decision_source(root)
    _install_append_crash(point)
    DecisionRegister(root, "owner/repo").append(_decision_proposal_record())


def _append_task_contract(ledger: RunLedger, run_id: str, item_id: str) -> dict:
    contract = TaskContract.create(
        objective="govern crash retry", non_goals=["no post-attempt amendment"],
        areas_to_avoid=[{"path": "slip/graph.py", "region": "all"}],
        input_hashes={"brief": "a" * 64}, acceptance_checks={"tests.unit": "python3 -m unittest"},
        constraints={"network": "dark"}, risk_class="high",
        retry_policy={"max_attempts": 2, "backoff": {"base_delay_ms": 10, "cap_delay_ms": 10, "strategy": "exponential"}}, dependencies=[],
    )
    return ledger.append({"schema_version": 0, "id": "task-contract-018f7e9b3c117abc8def0123456789ab", "tenant_id": "alpha",
        "timestamp": "2026-08-02T12:00:00.000Z", "kind": "task_contract", "run_id": run_id, "item_id": item_id,
        **contract.canonical(), "contract_digest": contract_digest(contract)})


def _terminal_before_retry_closure_then_crash(base: str) -> None:
    """Durably stop after terminal reservation and before its reconciliation closure."""
    from floati.scheduler import RetryPolicy, RunScheduler

    root = FloatiRoot.open(Path(base), "alpha")
    ledger = RunLedger(root)
    run = _run_created_record()
    ledger.append(run)
    ledger.append({"schema_version": 0, "id": "run-policy-bound-018f7e9b3c117abc8def0123456789ab", "tenant_id": "alpha",
        "timestamp": "2026-08-02T12:00:00.000Z", "kind": "run_policy_bound", "run_id": run["run_id"], "policy_digest": "a" * 64})
    ledger.append({"schema_version": 0, "id": "run-worker-pool-bound-018f7e9b3c117abc8def0123456789ab", "tenant_id": "alpha",
        "timestamp": "2026-08-02T12:00:00.000Z", "kind": "worker_pool_bound", "run_id": run["run_id"], "worker_ids": ["worker-a"]})
    _append_task_contract(ledger, run["run_id"], run["item_ids"][0])
    scheduler = RunScheduler(ledger)
    opened = scheduler.open_attempt(run["run_id"], run["item_ids"][0], RetryPolicy(2, 10, 10), 1,
        now="2026-08-02T12:00:00.000Z")
    decision = ledger.append({"schema_version": 0, "id": "run-dispatch-decision-018f7e9b3c117abc8def0123456789ac", "tenant_id": "alpha",
        "timestamp": "2026-08-02T12:00:00.000Z", "kind": "dispatch_decision", "run_id": run["run_id"], "item_id": run["item_ids"][0],
        "attempt_id": opened["attempt_id"], "eligible_workers": ["worker-a"], "chosen_worker": "worker-a", "capability_digest": "a" * 64,
        "reason_code": "policy.route", "policy_digest": "a" * 64, "routing_rank": 0, "scheduler_epoch": 1})
    scheduler.start_attempt(run["run_id"], run["item_ids"][0], opened["attempt_id"], decision["id"], now="2026-08-02T12:00:00.000Z")
    original = scheduler.reconcile
    calls = [0]
    def crash_after_terminal(*args: object, **kwargs: object) -> object:
        calls[0] += 1
        if calls[0] == 2:
            os._exit(CRASH_EXIT)
        return original(*args, **kwargs)
    scheduler.reconcile = crash_after_terminal
    scheduler.terminal_attempt(run["run_id"], run["item_ids"][0], opened["attempt_id"], "failed", "transient",
        "transient_failure", "idempotent", now="2026-08-02T12:00:00.000Z")


def _build_trace_and_crash_at_run_append(
    base: str, trace_name: str, target_append: int, point: str
) -> None:
    """Crash precisely at one owner-mediated run append while a trace is forming."""

    builders = {
        "success": build_success_trace,
        "retry": build_retry_stale_trace,
        "cancellation_native": lambda root: build_cancellation_trace(root, "native"),
        "cancellation_unavailable": lambda root: build_cancellation_trace(root, "unavailable"),
        "foc": build_foc_orphan_trace,
    }
    root = FloatiRoot.open_direct_home(Path(base) / "trace", create=True)
    original_append = RunLedger._append
    count = 0

    def append_with_cut(
        ledger: RunLedger,
        record: dict[str, object],
        *,
        scheduler: bool,
        **kwargs: object,
    ) -> dict[str, object]:
        nonlocal count
        count += 1
        if count == target_append:
            _install_append_crash(point)
        return original_append(ledger, record, scheduler=scheduler, **kwargs)

    RunLedger._append = append_with_cut
    builders[trace_name](root)


class CrashPointGauntletTests(unittest.TestCase):
    @staticmethod
    def bytes_under(path: Path) -> dict[str, bytes]:
        return {
            item.relative_to(path).as_posix(): item.read_bytes()
            for item in sorted(path.rglob("*"))
            if item.is_file() and not item.is_symlink()
        }

    def run_crasher(self, target: object, args: tuple[object, ...]) -> None:
        process = multiprocessing.get_context("fork").Process(target=target, args=args)
        process.start()
        process.join(5)
        self.assertEqual(CRASH_EXIT, process.exitcode)

    def seeded_mail_root(self, base: Path) -> FloatiRoot:
        root = FloatiRoot.open(base, "alpha")
        registry = Registry(root)
        registry.register(public_ids.worker('alpha'), "worker")
        registry.register("bob", "worker")
        EventLog(root).send(
            public_ids.worker('alpha'),
            "bob",
            "slipway",
            "a" * 40,
            "docs/evidence/hm3h.md",
            "baseline",
            idempotency_key="gauntlet-baseline-key",
        )
        return root


    def lawful_spawn_lifecycle(self) -> None:
        from floati.runtruth import RunProjection

        case = _Task3Case(self)
        case.activate(
            join_mode="all_terminal",
            on_child_failure="continue_until_join_impossible",
        )
        case.reject()
        closed = case.controller.close_group(
            case.run_id, case.created["id"], now=case.now(3602),
        )
        _runner, worker_result = case.run_worker()
        self.assertEqual("complete", worker_result["transition"])
        observation = case.ledger.project().run(case.run_id)[
            "descendant_observation_close"
        ][case.opened["attempt_id"]]
        self.assertEqual("satisfied", closed["outcome"])
        self.assertEqual([], observation["observed_descendant_ids"])

        complete = SpawnGroupFixtures()
        started = complete.started_parent()
        complete_group = complete.group(
            on_child_failure="continue_until_join_impossible",
        )
        complete_amendment = complete.amendment(complete_group)
        complete_rejected = complete.rejected(complete_group, complete_amendment)
        complete_close = complete.close(
            complete_group,
            complete_amendment,
            outcome="satisfied",
            close_reason="all_members_terminal",
            rejected_item_ids=[complete.child],
        )
        result_records, receipt = complete.parent_result_records(started)
        terminal = complete.parent_terminal(
            started,
            terminal_state="completed",
            policy_class=None,
            reason_code="completed",
        )
        projection = RunProjection.from_records(
            [
                *started,
                complete_group,
                complete_amendment,
                complete_rejected,
                complete_close,
                complete.observation_close(),
                *result_records,
                terminal,
            ],
            worker_receipts=[receipt],
            integrity=False,
        ).run(complete.run_id)
        self.assertEqual(
            "completed",
            projection["attempts"][complete.attempt]["terminal"]["terminal_state"],
        )

    def test_spawn_response_loss_restart_resolves_exact_pair_without_rewriting_bytes(self) -> None:
        """Catches response loss or restart duplicating the created/amendment pair."""

        self.lawful_spawn_lifecycle()
        case = _Task3Case(self)
        case.prepare_parent()
        managed = _ManagedSpawnCase(self, case)
        arguments = case.create_kwargs()
        with mock.patch.object(managed.service, "_send_response", return_value=None):
            with self.assertRaises(ProtocolRefusal) as lost:
                managed.controller.create_group(**arguments)
        self.assertEqual("sequencer_response_lost", lost.exception.code)
        projected = case.ledger.project().run(case.run_id)["spawn_groups"]
        self.assertEqual(1, len(projected))
        durable = next(iter(projected.values()))
        self.assertEqual("activated", durable["state"])
        before = self.bytes_under(case.root.resolve_relative("runs"))
        restarted = managed.restart()
        created, amendment = restarted.controller.create_group(**arguments)
        self.assertEqual(created["id"], amendment["spawn_group_id"])
        self.assertEqual(durable["created"]["id"], created["id"])
        self.assertEqual(before, self.bytes_under(case.root.resolve_relative("runs")))

    def test_spawn_short_write_rolls_back_then_exact_retry_commits_one_pair(self) -> None:
        """Catches a partial private spawn append surviving rollback."""

        self.lawful_spawn_lifecycle()
        case = _Task3Case(self)
        case.prepare_parent()
        before_records = case.ledger.records()
        before_bytes = self.bytes_under(case.root.tenant_home)
        real_write = os.write

        def short_write(descriptor: int, data: bytes) -> int:
            return real_write(descriptor, data[: max(1, len(data) // 2)])

        with mock.patch("floati.jsonl.os.write", short_write):
            with self.assertRaises(DurabilityFailure) as partial:
                case.controller.create_group(**case.create_kwargs())
        self.assertEqual("short_write", partial.exception.code)
        self.assertEqual(before_records, case.ledger.records())
        self.assertEqual(before_bytes, self.bytes_under(case.root.tenant_home))

        created, amendment = case.controller.create_group(**case.create_kwargs())
        self.assertEqual(created["id"], amendment["spawn_group_id"])
        self.assertEqual(2, len(case.ledger.records()) - len(before_records))

    def test_every_existing_ledger_append_and_rollback_crash_seam_is_typed(self) -> None:
        complete_points = {
            "after_append",
            "before_file_fsync",
            "after_file_fsync",
        }
        absent_points = {
            "before_append",
            "rollback_after_ftruncate",
            "rollback_before_fsync",
            "rollback_after_fsync",
        }
        torn_points = {"mid_append", "rollback_before_ftruncate"}

        for point in sorted(complete_points | absent_points | torn_points):
            with self.subTest(point=point), tempfile.TemporaryDirectory() as directory:
                base = Path(directory)
                root = self.seeded_mail_root(base)
                self.run_crasher(_send_then_crash, (str(base), point))
                if point in torn_points:
                    with self.assertRaises(IntegrityFailure) as read_failure:
                        read_records(root, "events.jsonl", allowed_kinds={"message_envelope"})
                    self.assertEqual("incomplete_jsonl_line", read_failure.exception.code)
                    with self.assertRaises(IntegrityFailure) as retry_failure:
                        EventLog(root).send(
                            public_ids.worker('alpha'),
                            "bob",
                            "slipway",
                            "b" * 40,
                            "docs/evidence/hm3h.md",
                            "crash retry",
                            idempotency_key="gauntlet-crash-key",
                        )
                    self.assertEqual("incomplete_jsonl_line", retry_failure.exception.code)
                    continue

                rows = read_records(root, "events.jsonl", allowed_kinds={"message_envelope"})
                expected_before_retry = 2 if point in complete_points else 1
                self.assertEqual(expected_before_retry, len(rows))
                retried = EventLog(root).send(
                    public_ids.worker('alpha'),
                    "bob",
                    "slipway",
                    "b" * 40,
                    "docs/evidence/hm3h.md",
                    "crash retry",
                    idempotency_key="gauntlet-crash-key",
                )
                rows = read_records(root, "events.jsonl", allowed_kinds={"message_envelope"})
                self.assertEqual(2, len(rows))
                self.assertEqual("gauntlet-crash-key", retried["idempotency_key"])
                self.assertEqual(1, sum(row["id"] == retried["id"] for row in rows))

    def test_new_ledger_parent_fsync_crash_seams_leave_one_complete_frame(self) -> None:
        for point in ("before_parent_fsync", "after_parent_fsync"):
            with self.subTest(point=point), tempfile.TemporaryDirectory() as directory:
                base = Path(directory)
                root = FloatiRoot.open(base, "alpha")
                self.run_crasher(_new_ledger_then_crash, (str(base), point))
                rows = read_records(
                    root,
                    "new-ledger/events.jsonl",
                    allowed_kinds={"registry_entry"},
                )
                self.assertEqual(["new-node"], [row["node_id"] for row in rows])

    def test_crash_between_work_claim_and_worker_receipt_never_double_claims(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = FloatiRoot.open(base, "alpha")
            Registry(root).register(public_ids.worker('alpha'), "worker")
            item = WorkLog(root).add("crash-gap", public_ids.worker('alpha'), [], now=NOW)
            AuthorityGrantStore(root).claim(
                "work-claims", public_ids.worker('alpha'), 60, 60, NOW
            )

            self.run_crasher(_claim_then_crash, (str(base),))

            with self.assertRaises(ProtocolRefusal) as retry:
                WorkerRunner(
                    root,
                    {"fixture": object()},
                    clock=lambda: NOW,
                ).run(public_ids.worker('alpha'), "fixture", now=NOW)
            self.assertEqual("worker_work_absent", retry.exception.code)
            rows = read_records(
                root,
                "work/items.jsonl",
                allowed_kinds={"work_item", "work_transition"},
            )
            claims = [
                row
                for row in rows
                if row["kind"] == "work_transition" and row["action"] == "claim"
            ]
            self.assertEqual([item["id"]], [row["work_item_id"] for row in claims])
            self.assertEqual(
                "worker_work_absent",
                WorkerRefusals(root).records()[-1]["reason_code"],
            )

    def test_run_created_crash_recovers_one_complete_frame_or_prior_state(self) -> None:
        for point, complete in (("before_append", False), ("after_append", True), ("after_file_fsync", True)):
            with self.subTest(point=point), tempfile.TemporaryDirectory() as directory:
                root = FloatiRoot.open(Path(directory), "alpha")
                self.run_crasher(_run_created_then_crash, (directory, point))
                ledger = RunLedger(root)
                rows = ledger.records()
                self.assertEqual(1 if complete else 0, len(rows))
                self.assertEqual(_run_created_record(), ledger.append(_run_created_record()))
                self.assertEqual(1, len(ledger.records()))

    def test_decision_proposal_crash_recovers_one_complete_frame_or_prior_state(self) -> None:
        """Catches decision recovery that duplicates a proposal or projects a torn frame as current truth."""
        for point, complete in (("before_append", False), ("after_append", True), ("after_file_fsync", True)):
            with self.subTest(point=point), tempfile.TemporaryDirectory() as directory:
                root = FloatiRoot.open(Path(directory), "alpha")
                self.run_crasher(_decision_proposal_then_crash, (directory, point))
                register = DecisionRegister(root, "owner/repo")
                self.assertEqual(1 if complete else 0, len(register.records()))
                self.assertEqual(_decision_proposal_record(), register.append(_decision_proposal_record()))
                self.assertEqual(1, len(register.records()))

    def test_terminal_reservation_crash_reconciles_one_exact_retry_on_restart(self) -> None:
        """Catches a restart that invents or loses a retry after terminal durability."""
        with tempfile.TemporaryDirectory() as directory:
            root = FloatiRoot.open(Path(directory), "alpha")
            self.run_crasher(_terminal_before_retry_closure_then_crash, (directory,))
            from floati.scheduler import RunScheduler
            ledger = RunLedger(root)
            before = ledger.records()
            self.assertEqual("attempt_terminal", before[-1]["kind"])
            RunScheduler(ledger).reconcile(now="2026-08-02T12:00:00.000Z")
            after = ledger.records()
            self.assertEqual("retry_scheduled", after[-1]["kind"])
            self.assertEqual(before[-1]["retry_record_id"], after[-1]["id"])
            RunScheduler(ledger).reconcile(now="2026-08-02T12:00:00.000Z")
            self.assertEqual(after, ledger.records())

    def test_interrupted_suspension_responses_retry_exactly_without_rewriting_run_bytes(self) -> None:
        """Catches a lost response duplicating either private frame on restart retry."""
        from datetime import timedelta
        from tests.test_approval_suspension import (
            DIRECT_NOW,
            _ManagedSuspensionContext,
        )

        for operation in ("suspend", "consume"):
            with self.subTest(operation=operation), tempfile.TemporaryDirectory() as directory:
                base = Path(directory)
                positive = _ManagedSuspensionContext(
                    base / "positive",
                    service_now=DIRECT_NOW + timedelta(seconds=2),
                )
                try:
                    if operation == "consume":
                        positive.prepare_approved_resume()
                        positive.service_now = DIRECT_NOW + timedelta(seconds=5)
                        arguments = positive.consume_args
                        first = positive.controller.consume(**arguments)
                        invoke = positive.controller.consume
                    else:
                        arguments = positive.suspend_args
                        first = positive.controller.suspend(**arguments)
                        invoke = positive.controller.suspend
                    before = self.bytes_under(positive.home / "runs")
                    retried = invoke(**arguments)
                    self.assertEqual(first["id"], retried["id"])
                    self.assertEqual(before, self.bytes_under(positive.home / "runs"))
                finally:
                    positive.close()

                interrupted = _ManagedSuspensionContext(
                    base / "interrupted",
                    service_now=DIRECT_NOW + timedelta(seconds=2),
                )
                try:
                    if operation == "consume":
                        interrupted.prepare_approved_resume()
                        interrupted.service_now = DIRECT_NOW + timedelta(seconds=5)
                        arguments = interrupted.consume_args
                        invoke = interrupted.controller.consume
                    else:
                        arguments = interrupted.suspend_args
                        invoke = interrupted.controller.suspend
                    with mock.patch.object(
                        interrupted.service, "_send_response", return_value=None
                    ):
                        with self.assertRaises(ProtocolRefusal):
                            invoke(**arguments)
                    records = (
                        interrupted.consumption_records()
                        if operation == "consume"
                        else interrupted.suspension_records()
                    )
                    self.assertEqual(1, len(records))
                    before = self.bytes_under(interrupted.home / "runs")
                    expected_id = records[0]["id"]
                    interrupted.restart_service()
                    retried = (
                        interrupted.controller.consume(**arguments)
                        if operation == "consume"
                        else interrupted.controller.suspend(**arguments)
                    )
                    self.assertEqual(expected_id, retried["id"])
                    self.assertEqual(before, self.bytes_under(interrupted.home / "runs"))
                finally:
                    interrupted.close()

    def test_suspension_frames_short_write_rolls_back_before_exact_retry(self) -> None:
        """Catches either private run frame surviving a partial physical append."""
        from tests.test_approval_suspension import _DirectSuspensionContext

        real_write = os.write

        def short_write(descriptor: int, data: bytes) -> int:
            return real_write(descriptor, data[: max(1, len(data) // 2)])

        with tempfile.TemporaryDirectory() as directory:
            suspend = _DirectSuspensionContext(Path(directory) / "suspend")
            before = suspend.ledger.records()
            with mock.patch("floati.jsonl.os.write", short_write):
                with self.assertRaises(DurabilityFailure) as caught:
                    suspend.controller.suspend(**suspend.suspend_args)
            self.assertEqual("short_write", caught.exception.code)
            self.assertEqual(before, suspend.reopen().ledger.records())
            self.assertEqual("active", suspend.authority_tail()["state"])
            self.assertEqual(
                "attempt_suspended_for_approval",
                suspend.controller.suspend(**suspend.suspend_args)["kind"],
            )

            consume = _DirectSuspensionContext(Path(directory) / "consume")
            consume.prepare_approved_resume()
            before = consume.ledger.records()
            with mock.patch("floati.jsonl.os.write", short_write):
                with self.assertRaises(DurabilityFailure) as caught:
                    consume.controller.consume(**consume.consume_args)
            self.assertEqual("short_write", caught.exception.code)
            self.assertEqual(before, consume.reopen().ledger.records())
            self.assertEqual("suspended", consume.attempt_state()["state"])
            self.assertEqual(
                "approval_consumed_for_resume",
                consume.controller.consume(**consume.consume_args)["kind"],
            )

    def test_hm3i_every_run_kind_has_a_forked_append_cut_with_only_prefix_or_typed_torn_truth(self) -> None:
        """Every literal run kind is cut at the shared durability seams, never silently repaired."""
        builders = (
            ("success", build_success_trace),
            ("retry", build_retry_stale_trace),
            ("cancellation_native", lambda root: build_cancellation_trace(root, "native")),
            ("cancellation_unavailable", lambda root: build_cancellation_trace(root, "unavailable")),
            ("foc", build_foc_orphan_trace),
        )
        selected: list[tuple[str, int, str]] = []
        seen_kinds: set[str] = set()
        traces = []
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            for trace_name, builder in builders:
                trace = builder(
                    FloatiRoot.open_direct_home(base / f"baseline-{trace_name}", create=True)
                )
                traces.append(trace)
                for ordinal, record in enumerate(trace.records, start=1):
                    kind = str(record["kind"])
                    if kind not in seen_kinds:
                        selected.append((trace_name, ordinal, kind))
                        seen_kinds.add(kind)

            self.assertTrue(all(axis_coverage_from_traces("crash", traces).values()))

        self.assertEqual(CANONICAL_RUN_KINDS, seen_kinds)
        complete_points = {"after_append", "before_file_fsync", "after_file_fsync"}
        points = (
            "before_append",
            "mid_append",
            "after_append",
            "before_file_fsync",
            "after_file_fsync",
        )
        for trace_name, target_append, kind in selected:
            for point in points:
                with self.subTest(kind=kind, point=point), tempfile.TemporaryDirectory() as directory:
                    self.run_crasher(
                        _build_trace_and_crash_at_run_append,
                        (directory, trace_name, target_append, point),
                    )
                    root = FloatiRoot.open_direct_home(Path(directory) / "trace", create=False)
                    if point == "mid_append":
                        with self.assertRaises(IntegrityFailure) as torn:
                            RunLedger(root).records()
                        self.assertEqual("incomplete_jsonl_line", torn.exception.code)
                        continue

                    records = RunLedger(root).records()
                    expected_count = target_append if point in complete_points else target_append - 1
                    self.assertEqual(expected_count, len(records))
                    if point in complete_points:
                        self.assertEqual(kind, records[-1]["kind"])


class ThreadObservationCrashTests(unittest.TestCase):
    def _seed(self, directory: str, action: str):
        from floati.thread_observations import ThreadObserver, ThreadObservationLedger

        base = Path(directory)
        root = FloatiRoot.open(base, "alpha")
        Registry(root).register("owner-node", "Codex")
        Registry(root).register("observer-node", "Codex")
        item = WorkLog(root).add("thread crash seam", "owner-node", [])
        attachment_id = None
        if action != "register":
            attachment = ThreadObserver(root).register_work_item(
                str(item["id"]), THREAD_ID, "observer-node", now=THREAD_NOW
            )
            attachment_id = str(attachment["id"])
        return root, ThreadObservationLedger, str(item["id"]), attachment_id

    def test_every_thread_append_crash_seam_is_exact_or_typed_on_restart(self) -> None:
        """Catches registration, observation, or detachment repair after real death."""

        context = multiprocessing.get_context("fork")
        points = (
            "before_append",
            "mid_append",
            "after_append",
            "before_file_fsync",
            "after_file_fsync",
            "before_parent_fsync",
            "after_parent_fsync",
            "rollback_before_ftruncate",
            "rollback_after_ftruncate",
            "rollback_before_fsync",
            "rollback_after_fsync",
        )
        torn_points = {"mid_append", "rollback_before_ftruncate"}
        complete_points = {
            "after_append",
            "before_file_fsync",
            "after_file_fsync",
            "before_parent_fsync",
            "after_parent_fsync",
        }
        for action in ("register", "observe", "detach"):
            action_points = (
                points
                if action == "register"
                else tuple(point for point in points if "parent_fsync" not in point)
            )
            for point in action_points:
                with self.subTest(action=action, point=point), tempfile.TemporaryDirectory() as directory:
                    root, ledger_type, work_item_id, attachment_id = self._seed(
                        directory, action
                    )
                    prior_count = 0 if action == "register" else 1
                    crasher = context.Process(
                        target=_thread_action_process,
                        args=(
                            directory,
                            action,
                            work_item_id,
                            attachment_id,
                            point,
                        ),
                    )
                    crasher.start()
                    crasher.join(10)
                    if crasher.is_alive():
                        crasher.terminate()
                        crasher.join(5)
                    self.assertEqual(CRASH_EXIT, crasher.exitcode)

                    path = root.resolve_relative(ledger_type.relative_path)
                    crashed_bytes = path.read_bytes() if path.exists() else b""
                    if action == "observe" and point == "before_append":
                        method_files = list(Path(directory).glob("thread-idle-*-methods"))
                        self.assertEqual(1, len(method_files))
                        self.assertIn("thread/read", method_files[0].read_text())
                    if point in torn_points:
                        with self.assertRaises(IntegrityFailure) as caught:
                            ledger_type(root).records()
                        self.assertEqual("incomplete_jsonl_line", caught.exception.code)
                    else:
                        expected = prior_count + (1 if point in complete_points else 0)
                        self.assertEqual(expected, len(ledger_type(root).records()))

                    results = context.Queue()
                    restarted = context.Process(
                        target=_thread_action_process,
                        args=(
                            directory,
                            action,
                            work_item_id,
                            attachment_id,
                            None,
                            results,
                        ),
                    )
                    restarted.start()
                    status, value = results.get(timeout=10)
                    restarted.join(10)
                    if restarted.is_alive():
                        restarted.terminate()
                        restarted.join(5)
                    self.assertEqual(0, restarted.exitcode)
                    if point in torn_points:
                        self.assertEqual("error", status, value)
                        self.assertIn("IntegrityFailure:incomplete_jsonl_line", value)
                        self.assertEqual(crashed_bytes, path.read_bytes())
                    else:
                        self.assertEqual("ok", status, value)
                        records = ledger_type(root).records()
                        self.assertEqual(prior_count + 1, len(records))
                        self.assertEqual(records[-1], value)

    def test_source_crash_timeout_partial_and_group_cleanup_remain_bounded(self) -> None:
        """Catches process start/response/TERM/KILL/reap seams escaping testimony."""

        from floati.thread_source import CodexLocalThreadSource

        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            for mode, outcome, reason in (
                ("idle", "observed", "exact_thread_read"),
                ("crash", "unknown", "provider_unavailable"),
                ("hang", "unknown", "provider_timeout"),
                ("partial", "unknown", "protocol_invalid"),
                ("ignore-term-child", "observed", "exact_thread_read"),
            ):
                with self.subTest(mode=mode):
                    prefix = base / mode
                    diagnostic = Path(str(prefix) + "-diagnostic.json")
                    source = CodexLocalThreadSource._for_test(
                        [
                            sys.executable,
                            str(THREAD_HARNESS),
                            mode,
                            str(prefix) + "-methods",
                            str(prefix) + "-params.json",
                            str(diagnostic),
                        ]
                    )
                    result = source.read(THREAD_ID, deadline_seconds=0.25)
                    self.assertEqual(
                        (outcome, reason),
                        (result.observation_outcome, result.observation_reason),
                    )
                    if mode == "ignore-term-child":
                        descendant = int(
                            Path(str(diagnostic) + ".descendant").read_text(
                                encoding="ascii"
                            )
                        )
                        deadline = time.monotonic() + 1
                        while time.monotonic() < deadline:
                            try:
                                os.kill(descendant, 0)
                            except ProcessLookupError:
                                break
                            time.sleep(0.01)
                        else:
                            self.fail("observer descendant survived exact group cleanup")


class EffectLedgerCrashTests(unittest.TestCase):
    def test_short_write_rolls_back_without_partial_effect_row(self) -> None:
        """Catches a short Effect-ledger write leaving a torn durable frame."""
        case = _EffectCase(self)
        intent_args = case.intent_args()
        real_write = os.write

        def short_write(descriptor: int, data: bytes) -> int:
            return real_write(descriptor, data[: len(data) // 2])

        with mock.patch("floati.jsonl.os.write", short_write):
            with self.assertRaises(DurabilityFailure) as caught:
                case.controller.intent(**intent_args)
        self.assertEqual("short_write", caught.exception.code)
        self.assertEqual([], EffectLedger(case.root).records())
        retried = case.controller.intent(**intent_args)
        self.assertEqual([retried], EffectLedger(case.root).records())

    def test_restart_replays_prefix_and_exact_retry_returns_canonical_row(self) -> None:
        """Catches real process death losing the physical intent selected on fresh retry."""
        context = multiprocessing.get_context("fork")
        for point in ("after_append", "before_file_fsync", "after_file_fsync"):
            with self.subTest(point=point):
                case = _EffectCase(self)
                intent_args = case.intent_args()
                crasher = context.Process(
                    target=_effect_intent_then_crash,
                    args=(
                        str(case.root.tenant_home), str(case.run.policy_path),
                        intent_args, point,
                    ),
                )
                crasher.start()
                crasher.join(10)
                if crasher.is_alive():
                    crasher.terminate()
                    crasher.join(5)
                self.assertEqual(CRASH_EXIT, crasher.exitcode)

                durable = EffectLedger(case.root).records()
                self.assertEqual(1, len(durable))
                retry = dict(intent_args)
                retry["now"] = datetime(2026, 8, 11, 13, 0, tzinfo=timezone.utc)
                results = context.Queue()
                restarted = context.Process(
                    target=_retry_effect_after_process_restart,
                    args=(
                        str(case.root.tenant_home), str(case.run.policy_path),
                        retry, results,
                    ),
                )
                restarted.start()
                status, value = results.get(timeout=10)
                restarted.join(10)
                if restarted.is_alive():
                    restarted.terminate()
                    restarted.join(5)
                self.assertEqual(0, restarted.exitcode)
                self.assertEqual("ok", status, value)
                self.assertEqual(durable[0], value)
                self.assertEqual(durable, EffectLedger(case.root).records())

    def test_every_effect_append_crash_seam_is_exact_or_typed_on_restart(self) -> None:
        """Catches an Effect crash seam being repaired, skipped, or retried ambiguously."""
        context = multiprocessing.get_context("fork")
        points = (
            "before_append",
            "mid_append",
            "after_append",
            "before_file_fsync",
            "after_file_fsync",
            "before_parent_fsync",
            "after_parent_fsync",
            "rollback_before_ftruncate",
            "rollback_after_ftruncate",
            "rollback_before_fsync",
            "rollback_after_fsync",
        )
        torn_points = {"mid_append", "rollback_before_ftruncate"}
        complete_points = {
            "after_append",
            "before_file_fsync",
            "after_file_fsync",
            "before_parent_fsync",
            "after_parent_fsync",
        }
        for point in points:
            with self.subTest(point=point):
                case = _EffectCase(self)
                intent_args = case.intent_args()
                crasher = context.Process(
                    target=_effect_intent_then_crash,
                    args=(
                        str(case.root.tenant_home), str(case.run.policy_path),
                        intent_args, point,
                    ),
                )
                crasher.start()
                crasher.join(10)
                if crasher.is_alive():
                    crasher.terminate()
                    crasher.join(5)
                self.assertEqual(CRASH_EXIT, crasher.exitcode)

                path = case.root.resolve_relative(EffectLedger.relative_path)
                crashed_bytes = path.read_bytes() if path.exists() else b""
                if point in torn_points:
                    with self.assertRaises(IntegrityFailure) as caught:
                        EffectLedger(case.root).records()
                    self.assertEqual("incomplete_jsonl_line", caught.exception.code)
                else:
                    durable = EffectLedger(case.root).records()
                    self.assertEqual(1 if point in complete_points else 0, len(durable))

                retry = dict(intent_args)
                retry["now"] = datetime(2026, 8, 11, 13, 0, tzinfo=timezone.utc)
                results = context.Queue()
                restarted = context.Process(
                    target=_retry_effect_after_process_restart,
                    args=(
                        str(case.root.tenant_home), str(case.run.policy_path),
                        retry, results,
                    ),
                )
                restarted.start()
                status, value = results.get(timeout=10)
                restarted.join(10)
                if restarted.is_alive():
                    restarted.terminate()
                    restarted.join(5)
                self.assertEqual(0, restarted.exitcode)
                if point in torn_points:
                    self.assertEqual("error", status)
                    self.assertTrue(
                        value.startswith("IntegrityFailure:incomplete_jsonl_line:"),
                        value,
                    )
                    self.assertEqual(crashed_bytes, path.read_bytes())
                    continue
                self.assertEqual("ok", status, value)
                durable = EffectLedger(case.root).records()
                self.assertEqual(1, len(durable))
                self.assertEqual(durable[0], value)

    def test_corrupt_tail_is_integrity_failure_and_is_never_skipped(self) -> None:
        """Catches restart silently ignoring a corrupt durable Effect-ledger suffix."""
        case = _EffectCase(self)
        case.controller.intent(**case.intent_args())
        path = case.root.resolve_relative(EffectLedger.relative_path)
        with path.open("ab") as handle:
            handle.write(b'{"schema_version":1')
        with self.assertRaises(IntegrityFailure) as records_failure:
            EffectLedger(case.root).records()
        self.assertEqual("incomplete_jsonl_line", records_failure.exception.code)
        with self.assertRaises(IntegrityFailure) as projection_failure:
            EffectLedger(case.root).project()
        self.assertEqual("incomplete_jsonl_line", projection_failure.exception.code)


class EffectWorkerCrashTests(unittest.TestCase):
    def setUp(self) -> None:
        # Crash/terminal tests instrument the integration seam; Task 1 owns real
        # kernel proof, and the unsupported test below replaces this patch.
        patcher = mock.patch(
            "floati.workers.apply_worker_isolation", return_value="macos-sandbox",
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_exec_child_timeout_and_crash_reap_provider_process_groups(self) -> None:
        """Catches timeout/death failing to reap an inherited exec provider."""
        from tests.test_workers import _EffectReportingAdapter, _EffectWorkerCase
        from tests.test_worker_bootstrap import WorkerBootstrapTests

        for mode in ("timeout", "crash"):
            with self.subTest(mode=mode):
                directory = Path(tempfile.mkdtemp(dir="\x2fprivate\x2ftmp"))
                self.addCleanup(shutil.rmtree, directory, True)
                pid_path = directory / "provider.pid"

                adapter_source = (
                    "import os\n"
                    "import subprocess\n"
                    "import sys\n"
                    "import time\n"
                    "from pathlib import Path\n"
                    "class Adapter:\n"
                    "    requires_workspace = False\n"
                    "    def __init__(self, command, *, isolate_process_group=True):\n"
                    "        self.isolate_process_group = isolate_process_group\n"
                    "        self.registrar = None\n"
                    "    def set_process_group_registrar(self, registrar):\n"
                    "        self.registrar = registrar\n"
                    "    def set_spawn_context(self, context, emit):\n"
                    "        return None\n"
                    "    def set_effect_context(self, context, emit):\n"
                    "        return None\n"
                    "    def spawn(self, item, *, deadline_seconds):\n"
                    "        provider = subprocess.Popen(\n"
                    "            [sys.executable, '-c',\n"
                    "             \"import os,signal,time; from pathlib import Path; \"\n"
                    "             \"signal.signal(signal.SIGTERM, signal.SIG_IGN); \"\n"
                    "             \"Path(os.environ['SLIPWAY_PROVIDER_PID']).write_text(\"\n"
                    "             \"str(os.getpid()) + ':' + str(os.getpgrp()), encoding='utf-8'); \"\n"
                    "             \"time.sleep(30)\"],\n"
                    "            start_new_session=self.isolate_process_group,\n"
                    "        )\n"
                    "        ready_deadline = time.monotonic() + 1.0\n"
                    "        while not Path(os.environ['SLIPWAY_PROVIDER_PID']).exists():\n"
                    "            if time.monotonic() >= ready_deadline:\n"
                    "                raise RuntimeError('provider did not install SIGTERM handler')\n"
                    "            time.sleep(0.005)\n"
                    "        if self.isolate_process_group and self.registrar is not None:\n"
                    "            self.registrar(provider.pid)\n"
                    "        if os.environ['SLIPWAY_PROVIDER_MODE'] == 'crash':\n"
                    "            os._exit(7)\n"
                    "        time.sleep(30)\n"
                    "        return object()\n"
                    "    def drive(self, handle, item, *, deadline_seconds):\n"
                    "        return []\n"
                    "CodexAppServerAdapter = Adapter\n"
                    "ClaudeHeadlessAdapter = Adapter\n"
                    "PiRpcAdapter = Adapter\n"
                )
                bootstrap = WorkerBootstrapTests._instrumented_package(
                    self,
                    directory / "instrumented",
                    isolation_source=WorkerBootstrapTests._stub_isolation(
                        "return 'macos-sandbox'"
                    ),
                    adapter_source=adapter_source,
                )
                case = _EffectWorkerCase(self, _EffectReportingAdapter(()))
                runner = case.runner(instrument_exec=False)
                # Keep the Worker deadline beyond the instrumented bootstrap's
                # own one-second provider-readiness bound so a loaded full bank
                # still proves descendant cleanup instead of timing out before
                # the provider PID/process group exists.
                runner.call_timeout = 2.0
                environment = {
                    "SLIPWAY_PROVIDER_PID": str(pid_path),
                    "SLIPWAY_PROVIDER_MODE": mode,
                }
                with mock.patch(
                    "floati.workers._WORKER_BOOTSTRAP_PATH", bootstrap,
                ), mock.patch.dict(os.environ, environment, clear=False):
                    result = runner.run(
                        "node-a", "codex", now=case.run.now(8),
                        run_id=case.run.run_id, item_id=case.run.parent,
                        attempt_id=case.run.opened["attempt_id"],
                    )

                self.assertEqual("degrade", result["transition"])
                self.assertEqual(
                    "process_timeout" if mode == "timeout" else "process_died",
                    result["outcome_code"],
                )
                provider_pid, provider_group = (
                    int(value)
                    for value in pid_path.read_text(encoding="utf-8").split(":")
                )
                adapter_pid = runner.last_process_audit["adapter_pid"]
                self.assertEqual(adapter_pid, provider_group)
                self.assertEqual([], runner.last_process_audit["registered_process_groups"])
                deadline = time.monotonic() + 0.5
                provider_survived = True
                while time.monotonic() < deadline:
                    try:
                        os.kill(provider_pid, 0)
                    except ProcessLookupError:
                        provider_survived = False
                        break
                    time.sleep(0.01)
                audited_survivors = runner.last_process_audit["alive_after_cleanup"]
                if provider_survived:
                    try:
                        os.killpg(provider_group, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                self.assertEqual(
                    (False, []),
                    (provider_survived, audited_survivors),
                    "TERM-ignoring provider survived while the audit reported empty",
                )

    def test_isolation_failure_does_not_record_process_lost_effect_unknown(self) -> None:
        """Catches a pre-callback isolation refusal inventing dispatched uncertainty."""
        from tests.test_workers import _EffectReportingAdapter, _EffectWorkerCase

        case = _EffectWorkerCase(
            self,
            _EffectReportingAdapter((
                _EffectWorkerCase.intent_event(),
                _EffectWorkerCase.dispatch_event(),
            )),
        )
        with mock.patch(
            "floati.workers.apply_worker_isolation",
            side_effect=WorkerAdapterFailure("effect_worker_isolation_unavailable"),
        ):
            result = case.execute()

        self.assertEqual("degrade", result["transition"])
        self.assertEqual(
            "effect_worker_isolation_unavailable", result["outcome_code"],
        )
        self.assertEqual([], case.effect_ledger.records())

    def test_concurrent_terminal_evidence_preserves_worker_degradation(self) -> None:
        """Catches a terminal race escaping before the degradation receipt is written."""
        import floati.workers as workers_module
        from tests.test_workers import _EffectReportingAdapter, _EffectWorkerCase

        case = _EffectWorkerCase(
            self,
            _EffectReportingAdapter((
                _EffectWorkerCase.intent_event(),
                _EffectWorkerCase.dispatch_event(),
            ), die_after_reports=True),
        )
        runner = case.runner()
        projected = threading.Event()
        terminal_written = threading.Event()
        writer_errors = []

        def concurrent_terminal() -> None:
            try:
                self.assertTrue(projected.wait(2))
                operation = next(
                    iter(case.effect_ledger.project()._operations.values())
                )
                case.effect_controller.failed(
                    operation["operation_id"],
                    reason_code="effect_not_applied",
                    evidence_digest="f" * 64,
                    spend_status="complete",
                    measured_spend=None,
                    now=case.run.now(8),
                )
            except BaseException as exc:
                writer_errors.append(exc)
            finally:
                terminal_written.set()

        def synchronized_projection(
            controller: object, context: object,
        ) -> tuple[dict[str, object], ...]:
            operations = original_operations[6](controller, context)
            projected.set()
            self.assertTrue(terminal_written.wait(2))
            return operations

        original_operations = workers_module._EFFECT_WORKER_OPERATIONS
        synchronized_operations = (
            *original_operations[:6], synchronized_projection,
            *original_operations[7:],
        )
        writer = threading.Thread(target=concurrent_terminal)
        writer.start()
        try:
            with mock.patch.object(
                workers_module,
                "_EFFECT_WORKER_OPERATIONS",
                synchronized_operations,
            ):
                result = runner.run(
                    "node-a", "codex", now=case.run.now(8),
                    run_id=case.run.run_id, item_id=case.run.parent,
                    attempt_id=case.run.opened["attempt_id"],
                )
        finally:
            writer.join(2)

        self.assertFalse(writer.is_alive())
        self.assertEqual([], writer_errors)
        self.assertEqual("degrade", result["transition"])
        self.assertEqual("process_died", result["outcome_code"])
        self.assertEqual(
            "degrade", runner.receipts.records()[-1]["transition"],
        )
        operation = next(iter(case.effect_ledger.project()._operations.values()))
        self.assertEqual("failed", operation["state"])
        self.assertEqual("effect_not_applied", operation["current_record"]["reason_code"])

    def test_child_process_loss_after_dispatch_records_unknown_and_degrades(self) -> None:
        """Catches worker degradation becoming durable before dispatched-effect uncertainty."""
        from tests.test_workers import _EffectReportingAdapter, _EffectWorkerCase

        adapter = _EffectReportingAdapter(
            (
                _EffectWorkerCase.intent_event(),
                _EffectWorkerCase.dispatch_event(),
            ),
            die_after_reports=True,
        )
        case = _EffectWorkerCase(self, adapter)
        runner = case.runner()
        original_append = runner.receipts.append
        observed_order = []

        def append_with_order(*args: object, **kwargs: object) -> dict[str, object]:
            transition = args[4]
            if transition == "degrade":
                operation = next(
                    iter(case.effect_ledger.project()._operations.values())
                )
                observed_order.append(operation["state"])
            return original_append(*args, **kwargs)

        runner.receipts.append = append_with_order
        result = runner.run(
            "node-a", "codex", now=case.run.now(8),
            run_id=case.run.run_id, item_id=case.run.parent,
            attempt_id=case.run.opened["attempt_id"],
        )

        self.assertEqual("degrade", result["transition"])
        self.assertEqual("process_died", result["outcome_code"])
        self.assertEqual(["unknown"], observed_order)
        rows = case.effect_ledger.records()
        self.assertEqual(
            ["effect_intent", "effect_dispatched", "effect_unknown"],
            [row["kind"] for row in rows],
        )
        self.assertEqual("process_lost", rows[-1]["reason_code"])

class WakeHoldCrashTests(unittest.TestCase):
    """A controller append crash may leave only absence, one receipt, or typed torn evidence."""

    def test_every_hold_append_crash_seam_never_projects_clean_false_idle(self) -> None:
        """Catches a torn controller receipt being silently treated as a caught-up inbox."""
        from floati.wake_hold import WakeHoldController

        complete = {
            "after_append", "before_file_fsync", "after_file_fsync",
            "before_parent_fsync", "after_parent_fsync",
        }
        torn = {"mid_append", "rollback_before_ftruncate"}
        absent = {"before_append", "rollback_after_ftruncate", "rollback_before_fsync", "rollback_after_fsync"}
        for point in sorted(complete | torn | absent):
            with self.subTest(point=point), tempfile.TemporaryDirectory() as directory:
                base = Path(directory)
                root = CrashPointGauntletTests().seeded_mail_root(base)
                process = multiprocessing.get_context("fork").Process(target=_wake_evaluate_then_crash, args=(str(base), point))
                process.start()
                process.join(5)
                self.assertEqual(CRASH_EXIT, process.exitcode)
                if point in torn:
                    with self.assertRaises(IntegrityFailure):
                        WakeHoldController(root).evaluate("bob", idempotency_key="wake-crash-key")
                else:
                    artifact = WakeHoldController(root).evaluate("bob", idempotency_key="wake-crash-key")
                    self.assertNotEqual("caught_up", artifact["state"])


if __name__ == "__main__":
    unittest.main()

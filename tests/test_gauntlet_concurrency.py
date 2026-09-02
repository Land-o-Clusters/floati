from __future__ import annotations

from floati import fixture_ids as public_ids

import fcntl
import hashlib
import json
import multiprocessing
import os
import signal
import sys
import tempfile
import threading
import time
import traceback
import unittest
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from floati.errors import ProtocolRefusal
from floati.decisions import DecisionRegister, decision_digest
from floati.events import EventLog
from floati.approvals import ApprovalLedger
from floati.effects import EffectController, EffectLedger
from floati.ids import uuid7_hex
from floati.cursor import SparseCursor
from floati.jsonl import read_records
from floati.planes import AuthorityGrantStore
from floati.registry import Registry
from floati.root import FloatiRoot
from floati.runtruth import RunLedger
from floati.work import WorkLog
from floati.worker_exec import SpawnedWorkerProcess
from floati.admission import AdmissionEvaluator, AdmissionPlan
from floati.policy import PolicyDeploymentChecker, RepositoryPolicy
from tests.hm3i_gauntlet_fixtures import (
    CANONICAL_RUN_KINDS,
    assert_physical_projection,
    build_admission_case,
    build_cancellation_trace,
    build_foc_orphan_trace,
    build_policy_case,
    build_retry_stale_trace,
    build_success_trace,
)
from tests.test_effect_controller import _EffectCase


NOW = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)
THREAD_NOW = datetime(2026, 8, 13, 12, 0, tzinfo=timezone.utc)
THREAD_ID = "018f3a2b-4c5d-7e8f-9a0b-1c2d3e4f5678"
THREAD_HARNESS = (
    Path(__file__).parent
    / "fixtures"
    / "codex-thread-observer"
    / "reference_harness.py"
).resolve()
HAMMER_PROCESSES = 12
OPERATIONS_PER_PROCESS = 10


def _append_effect_process(
    base: str, policy_path: str, operation: str, fields: dict[str, object],
    start: object, results: object,
) -> None:
    root = FloatiRoot.open_direct_home(Path(base), create=False)
    controller = EffectController(
        EffectLedger(root), RunLedger(root),
        RepositoryPolicy.load(Path(policy_path)), ApprovalLedger(root),
    )
    start.wait()
    try:
        method = getattr(controller, operation)
        canonical = method(**fields)
        results.put(("ok", canonical["id"]))
    except ProtocolRefusal as exc:
        results.put(("refused", exc.code))
    except Exception as exc:
        results.put(("error", f"{type(exc).__name__}:{exc}"))


def _run_effect_race(
    base: Path, policy_path: Path,
    operations: list[tuple[str, dict[str, object]]],
) -> list[tuple[str, str]]:
    context = multiprocessing.get_context("fork")
    start = context.Event()
    results = context.Queue()
    processes = [
        context.Process(
            target=_append_effect_process,
            args=(str(base), str(policy_path), operation, fields, start, results),
        )
        for operation, fields in operations
    ]
    for process in processes:
        process.start()
    start.set()
    observed = [results.get(timeout=10) for _ in processes]
    for process in processes:
        process.join(10)
        if process.is_alive():
            process.terminate()
            process.join(5)
        if process.exitcode != 0:
            raise AssertionError(f"effect append child exited {process.exitcode}")
    return observed


def _thread_concurrent_action(
    base: str,
    action: str,
    work_item_id: str,
    provider_thread_id: str,
    attachment_id: str | None,
    mode: str,
    start: object,
    results: object,
) -> None:
    from floati.thread_observations import ThreadObserver

    root = FloatiRoot.open(Path(base), "alpha")
    if action == "observe":
        from floati.thread_source import CodexLocalThreadSource

        prefix = Path(base) / f"concurrent-{mode}-{os.getpid()}"
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
        observer = ThreadObserver._for_test(root, source)
    else:
        observer = ThreadObserver(root)
    start.wait()
    try:
        if action == "register":
            row = observer.register_work_item(
                work_item_id,
                provider_thread_id,
                "observer-node",
                now=THREAD_NOW,
            )
        elif action == "observe":
            assert attachment_id is not None
            row = observer.observe(attachment_id, now=THREAD_NOW)
        else:
            assert action == "detach" and attachment_id is not None
            row = observer.detach(
                attachment_id, "observer-node", now=THREAD_NOW
            )
        results.put((action, "ok", str(row["id"])))
    except ProtocolRefusal as exc:
        results.put((action, "refused", exc.code))
    except BaseException as exc:
        results.put((action, "error", f"{type(exc).__name__}:{exc}"))


def _thread_ordered_action(
    base: str,
    action: str,
    work_item_id: str,
    provider_thread_id: str,
    attachment_id: str,
    mode: str,
    now_text: object,
    first: bool,
    start: object,
    first_at_transaction: object,
    first_committed: object,
    results: object,
) -> None:
    """Run both children concurrently while forcing exact transaction order."""
    import floati.jsonl as jsonl_module
    from contextlib import contextmanager
    from datetime import datetime
    from floati.thread_observations import ThreadObserver

    root = FloatiRoot.open(Path(base), "alpha")
    if action == "observe":
        from floati.thread_source import CodexLocalThreadSource

        prefix = Path(base) / f"ordered-{mode}-{os.getpid()}"
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
        observer = ThreadObserver._for_test(root, source)
    else:
        observer = ThreadObserver(root)

    original_lock = jsonl_module._locked_path

    @contextmanager
    def ordered_lock(path, *, exclusive, timeout_seconds=jsonl_module.LOCK_TIMEOUT_SECONDS):
        is_thread_write = (
            exclusive
            and path.name == "records.jsonl.lock"
            and path.parent.name == "thread-observations"
        )
        if not is_thread_write:
            with original_lock(
                path, exclusive=exclusive, timeout_seconds=timeout_seconds,
            ):
                yield
            return
        if first:
            first_at_transaction.set()
            try:
                with original_lock(
                    path, exclusive=exclusive, timeout_seconds=timeout_seconds,
                ):
                    yield
            finally:
                first_committed.set()
            return
        if not first_at_transaction.wait(10) or not first_committed.wait(10):
            raise RuntimeError("ordered thread transaction did not reach its fence")
        with original_lock(
            path, exclusive=exclusive, timeout_seconds=timeout_seconds,
        ):
            yield

    jsonl_module._locked_path = ordered_lock
    start.wait()
    now = (
        now_text
        if isinstance(now_text, datetime)
        else datetime.fromisoformat(str(now_text).replace("Z", "+00:00"))
    )
    try:
        if action == "observe":
            row = observer.observe(attachment_id, now=now)
        else:
            assert action == "detach"
            row = observer.detach(attachment_id, "observer-node", now=now)
        results.put((action, "ok", str(row["id"])))
    except ProtocolRefusal as exc:
        results.put((action, "refused", exc.code))
    except BaseException as exc:
        results.put((action, "error", f"{type(exc).__name__}:{exc}"))


def _hold_file_lock(lock_path: str, ready: object, release: object) -> None:
    path = Path(lock_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        ready.set()
        release.wait(10)


def _send_under_contention(base: str, results: object) -> None:
    root = FloatiRoot.open(Path(base), "alpha")
    try:
        EventLog(root).send(
            public_ids.worker('alpha'),
            "bob",
            "slipway",
            "a" * 40,
            "docs/evidence/hm3h.md",
            "lock contention",
            idempotency_key="gauntlet-lock-key",
        )
    except ProtocolRefusal as exc:
        results.put(("refused", exc.code))
    else:
        results.put(("ok", "sent"))


def _claim_under_contention(base: str, results: object) -> None:
    root = FloatiRoot.open(Path(base), "alpha")
    try:
        AuthorityGrantStore(root).claim(
            "work-claims", public_ids.worker('alpha'), 60, 60, NOW
        )
    except ProtocolRefusal as exc:
        results.put(("refused", exc.code))
    else:
        results.put(("ok", "claimed"))


def _retry_lock_timeouts(operation: object) -> object:
    for _ in range(100):
        try:
            return operation()
        except ProtocolRefusal as exc:
            if exc.code not in {"ledger_lock_timeout", "cas_lock_timeout"}:
                raise
            time.sleep(0.01)
    raise RuntimeError("lock contention did not clear after bounded retries")


def _register_hammer(
    base: str, index: int, start: object, results: object
) -> None:
    registry = Registry(FloatiRoot.open(Path(base), "alpha"))
    start.wait()
    try:
        ids = []
        for item in range(OPERATIONS_PER_PROCESS):
            node = f"hammer-{index:02d}-{item:02d}"
            record = _retry_lock_timeouts(
                lambda node=node: registry.register(node, "worker")
            )
            ids.append(record["id"])
        results.put(("ok", ids))
    except Exception as exc:
        results.put(("error", f"{type(exc).__name__}:{exc}"))


def _send_hammer(base: str, index: int, start: object, results: object) -> None:
    events = EventLog(FloatiRoot.open(Path(base), "alpha"))
    start.wait()
    try:
        ids = []
        for item in range(OPERATIONS_PER_PROCESS):
            key = f"hammer-send-{index:02d}-{item:02d}"
            record = _retry_lock_timeouts(
                lambda key=key: events.send(
                    public_ids.worker('alpha'),
                    "bob",
                    "slipway",
                    "a" * 40,
                    "docs/evidence/hm3h.md",
                    key,
                    idempotency_key=key,
                )
            )
            ids.append(record["id"])
        results.put(("ok", ids))
    except Exception as exc:
        results.put(("error", f"{type(exc).__name__}:{exc}"))


def _ack_hammer(
    base: str, item_ids: list[str], start: object, results: object
) -> None:
    cursor = SparseCursor(FloatiRoot.open(Path(base), "alpha"))
    start.wait()
    try:
        record = _retry_lock_timeouts(
            lambda: cursor.ack(
                "bob", item_ids, acting_session_id="gauntlet-session"
            )
        )
        results.put(("ok", record["id"]))
    except Exception as exc:
        results.put(("error", f"{type(exc).__name__}:{exc}"))


def _claim_hammer(base: str, start: object, results: object) -> None:
    work = WorkLog(FloatiRoot.open(Path(base), "alpha"))
    claimed = []
    start.wait()
    try:
        while True:
            try:
                item = _retry_lock_timeouts(
                    lambda: work.claim_owned_oldest(
                        public_ids.worker('alpha'), "work-claims", 1, now=NOW
                    )
                )
            except ProtocolRefusal as exc:
                if exc.code == "work_owned_open_absent":
                    break
                raise
            claimed.append(item["id"])
        results.put(("ok", claimed))
    except Exception as exc:
        results.put(("error", f"{type(exc).__name__}:{exc}"))


def _decision_proposal() -> dict:
    record: dict[str, object] = {
        "schema_version": 0,
        "id": "decision-record-018f7e9b3c117abc8def0123456789ab",
        "tenant_id": "alpha",
        "timestamp": "2026-08-08T12:00:00.000Z",
        "kind": "decision_record",
        "repository": "owner/repo",
        "decision_id": "decision-018f7e9b3c127abc8def0123456789ab",
        "scope": {"kind": "repository"},
        "statement": "Concurrent proposal remains one semantic effect.",
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


def _decision_hammer(base: str, start: object, results: object) -> None:
    register = DecisionRegister(FloatiRoot.open(Path(base), "alpha"), "owner/repo")
    start.wait()
    try:
        record = _retry_lock_timeouts(lambda: register.append(_decision_proposal()))
        results.put(("ok", record["id"]))
    except Exception as exc:
        results.put(("error", f"{type(exc).__name__}:{exc}"))


def _tree_digest(root: Path) -> str:
    """Fingerprint every ordinary test input byte without invoking a product cache."""

    digest = hashlib.sha256()
    for path in sorted(root.rglob("*"), key=lambda value: value.as_posix()):
        relative = path.relative_to(root).as_posix().encode("utf-8")
        if path.is_dir():
            digest.update(b"D\0" + relative + b"\0")
        elif path.is_file():
            digest.update(b"F\0" + relative + b"\0" + path.read_bytes())
        else:
            raise AssertionError(f"unexpected nonregular fixture path: {path}")
    return digest.hexdigest()


def _policy_admission_read_hammer(
    policy_path: str, plan_path: str, start: object, results: object
) -> None:
    """Read-only Item 7/8 equality worker; it must not open any durable owner."""

    start.wait()
    try:
        policy = RepositoryPolicy.load(Path(policy_path))
        deployment = PolicyDeploymentChecker.check(Path(policy_path), policy.digest)
        plan = AdmissionPlan.load(Path(plan_path))
        artifact = AdmissionEvaluator.evaluate(plan, policy)
        results.put(
            (
                "ok",
                json.dumps(
                    {
                        "policy_digest": policy.digest,
                        "policy_status": deployment.status.value,
                        "admission": artifact.machine(),
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            )
        )
    except Exception as exc:
        results.put(("error", f"{type(exc).__name__}:{exc}"))


def _run_created_candidate(tenant_id: str) -> dict[str, object]:
    return {
        "schema_version": 0,
        "id": "run-created-018f7e9b3c117abc8def0123456789ab",
        "tenant_id": tenant_id,
        "timestamp": "2026-08-08T12:00:00.000Z",
        "kind": "run_created",
        "run_id": "run-018f7e9b3c117abc8def0123456789ab",
        "plan_digest": "a" * 64,
        "item_ids": ["work-018f7e9b3c117abc8def0123456789ab"],
        "dependency_edges": [],
    }


def _forked_writer_failure(exc: BaseException) -> str:
    """Carry the caught exception out of the child, repr and traceback intact.

    A forked writer that fails in CI and nowhere else is only ever diagnosed by
    what it says on the way out. `f"{type(exc).__name__}:{exc}"` drops the
    repr's arguments and every frame, and the assertion that consumed it
    printed only the STATUS SET - so a red run named neither the exception nor
    the line. Diagnostics only: nothing here changes what is caught, what is
    put on the queue as a status, or what any assertion accepts.
    """

    trace = "".join(
        traceback.format_exception(type(exc), exc, exc.__traceback__)
    ).strip()
    return f"{repr(exc)}\n{trace}"


def _run_created_hammer(base: str, start: object, results: object) -> None:
    ledger = RunLedger(FloatiRoot.open_direct_home(Path(base), create=False))
    start.wait()
    try:
        record = _retry_lock_timeouts(
            lambda: ledger.append(_run_created_candidate(ledger.root.tenant_id))
        )
        results.put(("ok", record["id"]))
    except Exception as exc:
        results.put(("error", _forked_writer_failure(exc)))


def _run_trace_hammer(base: str, start: object, results: object) -> None:
    """Append a distinct owner-built successful trace and return its local expectation."""

    root = FloatiRoot.open_direct_home(Path(base), create=False)
    start.wait()
    try:
        trace = build_success_trace(root)
        owned_records = [
            record
            for record in trace.ledger.records()
            if record.get("run_id") == trace.run_id
        ]
        results.put(
            (
                "ok",
                (
                    trace.run_id,
                    trace.item_ids,
                    assert_physical_projection(trace),
                    [record["id"] for record in owned_records],
                ),
            )
        )
    except Exception as exc:
        results.put(("error", _forked_writer_failure(exc)))


def _run_full_trace_family_hammer(
    base: str, index: int, start: object, results: object
) -> None:
    """Build one member of the complete run-family fixture in a forked owner home."""

    root = FloatiRoot.open_direct_home(
        Path(base) / f"family-{index:02d}", create=True
    )
    builders = (
        build_success_trace,
        build_retry_stale_trace,
        lambda member_root: build_cancellation_trace(member_root, "native"),
        lambda member_root: build_cancellation_trace(member_root, "local_process_only"),
        lambda member_root: build_cancellation_trace(member_root, "unavailable"),
        build_foc_orphan_trace,
    )
    start.wait()
    try:
        trace = builders[index % len(builders)](root)
        records = trace.ledger.records()
        results.put(
            (
                "ok",
                (
                    str(root.path),
                    trace.run_id,
                    trace.item_ids,
                    assert_physical_projection(trace),
                    [record["id"] for record in records],
                ),
            )
        )
    except Exception as exc:
        results.put(("error", f"{type(exc).__name__}:{exc}"))


def _mixed_run_receipt_race_hammer(
    base: str, index: int, start: object, results: object
) -> None:
    """One unrelated create races the receipt/result-bearing traces."""

    if index == 0:
        _run_created_hammer(base, start, results)
    else:
        _run_trace_hammer(base, start, results)


def _wake_evaluate_hammer(base: str, key: str, start: object, results: object) -> None:
    """Exercise the real controller in a separate process, never a mocked lock."""
    from floati.wake_hold import WakeHoldController

    root = FloatiRoot.open(Path(base), "alpha")
    start.wait()
    try:
        artifact = WakeHoldController(root).evaluate("bob", idempotency_key=key)
        results.put(("ok", artifact))
    except BaseException as exc:
        results.put(("error", f"{type(exc).__name__}:{getattr(exc, 'code', '')}"))


def _wake_session_evaluate_hammer(
    base: str, session: str, start: object, results: object,
) -> None:
    """Evaluate one real session namespace after a common multi-process release."""
    from floati.wake_hold import WakeHoldController

    root = FloatiRoot.open(Path(base), "alpha")
    start.wait()
    try:
        artifact = WakeHoldController(root).evaluate(
            "bob", idempotency_key="same-key", worker_session_id=session,
        )
        results.put(("ok", artifact))
    except BaseException as exc:
        results.put(("error", f"{type(exc).__name__}:{getattr(exc, 'code', '')}"))


def _wake_ordered_action(
    base: str, action: str, item_id: str, session: str | None, first: bool,
    first_locked: object, release_first: object, results: object,
) -> None:
    """Run a real public operation while deterministically fencing its coordination lock."""
    from contextlib import contextmanager
    import floati.wake_hold as wake_hold_module

    root = FloatiRoot.open(Path(base), "alpha")
    real_guard = wake_hold_module.wake_coordination_guard

    @contextmanager
    def ordered_guard(root_arg: object, recipient: str, *, worker_session_id: str | None = None):
        with real_guard(root_arg, recipient, worker_session_id=worker_session_id):
            if first:
                first_locked.set()
                if not release_first.wait(10):
                    raise RuntimeError("ordered wake action was not released")
            yield

    wake_hold_module.wake_coordination_guard = ordered_guard
    try:
        if action == "evaluate":
            row = wake_hold_module.WakeHoldController(root).evaluate(
                "bob", idempotency_key="ordered-seed", worker_session_id=session,
            )
            results.put((action, "ok", row["state"]))
        elif action == "ack":
            SparseCursor(root).ack(
                "bob", [item_id], acting_session_id="gauntlet-session",
                worker_session_id=session,
            )
            results.put((action, "ok", "acknowledged"))
        else:
            EventLog(root).retract(
                item_id, worker_session_id=session, reason="sent_in_error", author=public_ids.worker('alpha'),
            )
            results.put((action, "ok", "retracted"))
    except BaseException as exc:
        results.put((action, "error", f"{type(exc).__name__}:{getattr(exc, 'code', '')}"))


class ConcurrentWriterGauntletTests(unittest.TestCase):
    @staticmethod
    def _hammer_failures(results: list[tuple[str, object]]) -> str:
        """Render every non-ok payload, so a red run names its own cause."""

        return "\n".join(
            f"[{index}] {status}: {value}"
            for index, (status, value) in enumerate(results)
            if status != "ok"
        ) or "no non-ok payload was returned"

    def run_hammer(
        self,
        target: object,
        args_for_index: object,
        *,
        timeout: float = 30,
    ) -> list[tuple[str, object]]:
        context = multiprocessing.get_context("fork")
        start = context.Event()
        results = context.Queue()
        processes = [
            context.Process(
                target=target,
                args=args_for_index(index, start, results),
            )
            for index in range(HAMMER_PROCESSES)
        ]
        for process in processes:
            process.start()
        start.set()
        for process in processes:
            process.join(timeout)
            self.assertFalse(process.is_alive(), "hammer process exceeded its bound")
            self.assertEqual(0, process.exitcode)
        return [results.get(timeout=2) for _ in processes]


    def test_ledger_lock_contention_is_typed_and_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = FloatiRoot.open(base, "alpha")
            registry = Registry(root)
            registry.register(public_ids.worker('alpha'), "worker")
            registry.register("bob", "worker")

            context = multiprocessing.get_context("fork")
            ready = context.Event()
            release = context.Event()
            results = context.Queue()
            holder = context.Process(
                target=_hold_file_lock,
                args=(str(root.resolve_relative("events.jsonl.lock")), ready, release),
            )
            contender = context.Process(
                target=_send_under_contention,
                args=(str(base), results),
            )
            holder.start()
            self.assertTrue(ready.wait(2))
            contender.start()
            contender.join(2)
            try:
                self.assertFalse(
                    contender.is_alive(),
                    "ledger lock acquisition exceeded the two-second gauntlet bound",
                )
                self.assertEqual(
                    ("refused", "ledger_lock_timeout"),
                    results.get(timeout=1),
                )
            finally:
                release.set()
                holder.join(2)
                if contender.is_alive():
                    contender.terminate()
                    contender.join(2)
                if holder.is_alive():
                    holder.terminate()
                    holder.join(2)

    def test_cas_lock_contention_is_typed_and_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = FloatiRoot.open(base, "alpha")
            lock_path = root.resolve_relative(
                "authority-grants/work-claims.jsonl.cas.lock"
            )

            context = multiprocessing.get_context("fork")
            ready = context.Event()
            release = context.Event()
            results = context.Queue()
            holder = context.Process(
                target=_hold_file_lock,
                args=(str(lock_path), ready, release),
            )
            contender = context.Process(
                target=_claim_under_contention,
                args=(str(base), results),
            )
            holder.start()
            self.assertTrue(ready.wait(2))
            contender.start()
            contender.join(2)
            try:
                self.assertFalse(
                    contender.is_alive(),
                    "CAS lock acquisition exceeded the two-second gauntlet bound",
                )
                self.assertEqual(
                    ("refused", "cas_lock_timeout"),
                    results.get(timeout=1),
                )
            finally:
                release.set()
                holder.join(2)
                if contender.is_alive():
                    contender.terminate()
                    contender.join(2)
                if holder.is_alive():
                    holder.terminate()
                    holder.join(2)

    def test_twelve_process_send_register_ack_and_claim_torture_has_no_double_effects(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = FloatiRoot.open(base, "alpha")
            registry = Registry(root)
            registry.register(public_ids.worker('alpha'), "worker")
            registry.register("bob", "worker")

            registration_results = self.run_hammer(
                _register_hammer,
                lambda index, start, results: (
                    str(base), index, start, results
                ),
            )
            self.assertEqual(
                {"ok"}, {status for status, _ in registration_results}
            )
            registry_rows = read_records(
                root,
                "registry/entries.jsonl",
                allowed_kinds={"registry_entry"},
            )
            self.assertEqual(
                2 + HAMMER_PROCESSES * OPERATIONS_PER_PROCESS,
                len(registry_rows),
            )
            self.assertEqual(
                len(registry_rows),
                len({row["node_id"] for row in registry_rows}),
            )

            send_results = self.run_hammer(
                _send_hammer,
                lambda index, start, results: (
                    str(base), index, start, results
                ),
            )
            self.assertEqual({"ok"}, {status for status, _ in send_results})
            event_rows = read_records(
                root, "events.jsonl", allowed_kinds={"message_envelope"}
            )
            expected_count = HAMMER_PROCESSES * OPERATIONS_PER_PROCESS
            self.assertEqual(expected_count, len(event_rows))
            self.assertEqual(
                expected_count,
                len({row["idempotency_key"] for row in event_rows}),
            )

            messages, _ = EventLog(root).present("bob")
            message_ids = [str(message["id"]) for message in messages]
            ack_results = self.run_hammer(
                _ack_hammer,
                lambda _index, start, results: (
                    str(base), message_ids, start, results
                ),
            )
            self.assertEqual({"ok"}, {status for status, _ in ack_results})
            self.assertEqual(1, len({value for _, value in ack_results}))
            ack_rows = read_records(
                root,
                "receipts/acks/bob.jsonl",
                allowed_kinds={"ack_receipt"},
            )
            self.assertEqual(1, len(ack_rows))
            self.assertEqual(set(message_ids), set(SparseCursor(root).acked_ids("bob")))

            work = WorkLog(root)
            expected_work_ids = {
                work.add(f"hammer-work-{index:03d}", public_ids.worker('alpha'), [], now=NOW)["id"]
                for index in range(expected_count)
            }
            AuthorityGrantStore(root).claim(
                "work-claims", public_ids.worker('alpha'), 60, 60, NOW
            )
            claim_results = self.run_hammer(
                _claim_hammer,
                lambda _index, start, results: (str(base), start, results),
            )
            self.assertEqual({"ok"}, {status for status, _ in claim_results})
            claimed_ids = [
                item_id
                for _, process_claims in claim_results
                for item_id in process_claims
            ]
            self.assertEqual(expected_work_ids, set(claimed_ids))
            self.assertEqual(len(expected_work_ids), len(claimed_ids))

    def test_twelve_same_decision_proposals_have_one_physical_and_logical_effect(self) -> None:
        """Catches concurrent decision writers that duplicate the same proposal or invent terminal decision state."""
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = FloatiRoot.open(base, "alpha")
            _seed_decision_source(root)
            results = self.run_hammer(
                _decision_hammer,
                lambda _index, start, queue: (str(base), start, queue),
            )
            self.assertEqual({"ok"}, {status for status, _ in results})
            self.assertEqual(1, len({value for _, value in results}))
            register = DecisionRegister(root, "owner/repo")
            self.assertEqual([_decision_proposal()], register.records())
            self.assertEqual("proposed", register.project().status_for(_decision_proposal()["decision_id"]))

    def test_twelve_policy_and_admission_readers_are_equal_and_leave_no_durable_side_effect(self) -> None:
        """Item 7/8 readers are concurrent pure inputs, not a hidden run/worker/cache owner."""
        with tempfile.TemporaryDirectory() as directory:
            root = FloatiRoot.open_direct_home(Path(directory) / "readers", create=True)
            policy = build_policy_case(root)
            admission = build_admission_case(root)
            before = _tree_digest(root.path)

            results = self.run_hammer(
                _policy_admission_read_hammer,
                lambda _index, start, queue: (
                    str(policy.path), str(admission.plan_path), start, queue
                ),
            )

            self.assertEqual({"ok"}, {status for status, _ in results})
            self.assertEqual(1, len({value for _, value in results}))
            payload = json.loads(results[0][1])
            self.assertEqual(policy.digest, payload["policy_digest"])
            self.assertEqual("DEPLOYED", payload["policy_status"])
            self.assertEqual("admitted", payload["admission"]["outcome"])
            self.assertEqual(before, _tree_digest(root.path))
            self.assertFalse(root.resolve_relative("runs/events.jsonl").exists())
            self.assertFalse(root.resolve_relative("receipts/workers.jsonl").exists())

    def test_twelve_same_run_created_candidates_are_one_idempotent_effect_and_divergence_is_typed(self) -> None:
        """Every contender sees the same run-created frame; changing its same-id payload never appends."""
        with tempfile.TemporaryDirectory() as directory:
            root = FloatiRoot.open_direct_home(Path(directory) / "idempotent", create=True)
            results = self.run_hammer(
                _run_created_hammer,
                lambda _index, start, queue: (str(root.path), start, queue),
            )
            self.assertEqual({"ok"}, {status for status, _ in results})
            self.assertEqual(
                {_run_created_candidate(root.tenant_id)["id"]},
                {value for _, value in results},
            )
            ledger = RunLedger(root)
            self.assertEqual([_run_created_candidate(root.tenant_id)], ledger.records())
            path = root.resolve_relative(RunLedger.relative_path)
            before = path.read_bytes()
            divergent = _run_created_candidate(root.tenant_id)
            divergent["plan_digest"] = "b" * 64
            with self.assertRaises(ProtocolRefusal) as refusal:
                ledger.append(divergent)
            self.assertEqual("duplicate_record_id", refusal.exception.code)
            self.assertEqual(before, path.read_bytes())

    def test_twelve_owner_built_runs_keep_per_run_projection_and_ids_distinct_under_contention(self) -> None:
        """Forked writers append complete governed runs without duplicate IDs or cross-run projection state."""
        with tempfile.TemporaryDirectory() as directory:
            root = FloatiRoot.open_direct_home(Path(directory) / "run-traces", create=True)
            results = self.run_hammer(
                _run_trace_hammer,
                lambda _index, start, queue: (str(root.path), start, queue),
            )
            self.assertEqual(
                {"ok"},
                {status for status, _ in results},
                self._hammer_failures(results),
            )
            payloads = [value for _, value in results]
            run_ids = [value[0] for value in payloads]
            self.assertEqual(HAMMER_PROCESSES, len(set(run_ids)))
            ledger = RunLedger(root)
            records = ledger.records()
            self.assertEqual(len(records), len({record["id"] for record in records}))
            self.assertEqual(
                set(run_ids),
                {
                    record["run_id"]
                    for record in records
                    if record["kind"] == "run_created"
                },
            )
            projection = ledger.project()
            for run_id, item_ids, expected, expected_ids in payloads:
                with self.subTest(run_id=run_id):
                    run = projection.run(run_id)
                    owned = [record for record in records if record.get("run_id") == run_id]
                    self.assertEqual(expected_ids, [record["id"] for record in owned])
                    self.assertEqual(expected.run_outcome, projection.run_outcome(run_id))
                    self.assertEqual(
                        expected.item_outcomes,
                        tuple(sorted(projection.item_outcomes(run_id).items())),
                    )
                    self.assertEqual(
                        expected.contract_history,
                        tuple(
                            sorted(
                                (
                                    item_id,
                                    tuple(contract["history_ids"]),
                                    str(contract["contract_digest"]),
                                )
                                for item_id, contract in run["contracts"].items()
                            )
                        ),
                    )
                    self.assertEqual(tuple(item_ids), tuple(run["item_ids"]))

    def test_twelve_process_trace_fixture_does_not_claim_unexecuted_run_kinds(self) -> None:
        """The contention axis must derive its vocabulary from its own process traces."""
        with tempfile.TemporaryDirectory() as directory:
            results = self.run_hammer(
                _run_full_trace_family_hammer,
                lambda index, start, queue: (directory, index, start, queue),
            )
            self.assertEqual({"ok"}, {status for status, _ in results})
            observed: set[str] = set()
            for _status, value in results:
                path, run_id, item_ids, expected, expected_ids = value
                root = FloatiRoot.open_direct_home(Path(path), create=False)
                records = RunLedger(root).records()
                self.assertEqual(expected_ids, [record["id"] for record in records])
                self.assertEqual(expected.run_outcome, RunLedger(root).project().run_outcome(run_id))
                self.assertEqual(
                    expected.item_outcomes,
                    tuple(sorted(RunLedger(root).project().item_outcomes(run_id).items())),
                )
                self.assertEqual(tuple(item_ids), tuple(RunLedger(root).project().run(run_id)["item_ids"]))
                observed.update(str(record["kind"]) for record in records)
            self.assertEqual(CANONICAL_RUN_KINDS, observed)

    def test_twelve_process_receipt_result_race_never_refuses_an_unrelated_run_created(self) -> None:
        """The receipt view is taken under the run lock, so another valid run cannot poison its projection."""
        with tempfile.TemporaryDirectory() as directory:
            root = FloatiRoot.open_direct_home(Path(directory) / "receipt-race", create=True)
            results = self.run_hammer(
                _mixed_run_receipt_race_hammer,
                lambda index, start, queue: (
                    str(root.path), index, start, queue
                ),
            )
            self.assertEqual(
                {"ok"},
                {status for status, _ in results},
                self._hammer_failures(results),
            )
            created = [value for _, value in results if isinstance(value, str)]
            traces = [value for _, value in results if not isinstance(value, str)]
            self.assertEqual(
                [_run_created_candidate(root.tenant_id)["id"]], created
            )
            self.assertEqual(HAMMER_PROCESSES - 1, len(traces))
            ledger = RunLedger(root)
            records = ledger.records()
            self.assertEqual(len(records), len({record["id"] for record in records}))
            projection = ledger.project()
            self.assertEqual(
                [_run_created_candidate(root.tenant_id)],
                [
                    record
                    for record in records
                    if record["run_id"]
                    == _run_created_candidate(root.tenant_id)["run_id"]
                ],
            )
            self.assertEqual(
                HAMMER_PROCESSES,
                len(
                    {
                        record["run_id"]
                        for record in records
                        if record["kind"] == "run_created"
                    }
                ),
            )
            self.assertIsNotNone(
                projection.run(_run_created_candidate(root.tenant_id)["run_id"])
            )


class EffectWorkerExecProcessTests(unittest.TestCase):
    def test_group_shutdown_escalates_before_first_waitpid(self) -> None:
        """Catches group escalation reaping its identity-bearing leader early."""
        process = SpawnedWorkerProcess(4242)
        events: list[tuple[str, int]] = []

        def signal_group(process_group: int, signum: int) -> None:
            self.assertEqual(4242, process_group)
            events.append(("signal", signum))

        def reap(pid: int, options: int) -> tuple[int, int]:
            self.assertEqual((4242, os.WNOHANG), (pid, options))
            events.append(("waitpid", options))
            return 4242, 0

        with (
            mock.patch("floati.worker_exec.os.getpgid", return_value=4242),
            mock.patch("floati.worker_exec.os.killpg", side_effect=signal_group),
            mock.patch("floati.worker_exec.os.waitpid", side_effect=reap),
        ):
            self.assertTrue(process.confirm_process_group())
            shutdown = getattr(process, "shutdown_process_group", None)
            self.assertTrue(callable(shutdown), "exec group shutdown is missing")
            shutdown(grace_seconds=0)
            self.assertEqual(
                [
                    ("signal", signal.SIGTERM),
                    ("signal", 0),
                    ("signal", signal.SIGKILL),
                ],
                events,
            )
            process.join(0)

        self.assertEqual(
            [
                ("signal", signal.SIGTERM),
                ("signal", 0),
                ("signal", signal.SIGKILL),
                ("waitpid", os.WNOHANG),
            ],
            events,
        )

    def test_group_shutdown_escalates_when_existence_probe_is_denied(self) -> None:
        """Catches EPERM on a present group bypassing pre-reap escalation."""
        process = SpawnedWorkerProcess(4242)
        signals: list[int] = []

        def signal_group(process_group: int, signum: int) -> None:
            self.assertEqual(4242, process_group)
            signals.append(signum)
            if signum == 0:
                raise PermissionError

        with (
            mock.patch("floati.worker_exec.os.getpgid", return_value=4242),
            mock.patch("floati.worker_exec.os.killpg", side_effect=signal_group),
            mock.patch("floati.worker_exec.os.waitpid") as waitpid,
        ):
            self.assertTrue(process.confirm_process_group())
            try:
                process.shutdown_process_group(grace_seconds=0.1)
            except PermissionError:
                self.fail("denied existence probe bypassed SIGKILL escalation")

        self.assertEqual([signal.SIGTERM, 0, signal.SIGKILL], signals)
        waitpid.assert_not_called()

    def test_observed_exec_exit_is_cached_before_any_process_group_signal(self) -> None:
        """Catches a reaped/reused PID receiving a later cleanup signal."""
        process = SpawnedWorkerProcess(4242)
        with (
            mock.patch("floati.worker_exec.os.waitpid", return_value=(4242, 0)),
            mock.patch("floati.worker_exec.os.getpgid") as getpgid,
            mock.patch("floati.worker_exec.os.killpg") as killpg,
            mock.patch("floati.worker_exec.os.kill") as kill_process,
        ):
            self.assertFalse(process.is_alive())
            process.terminate()
            process.kill()
        self.assertEqual(0, process.exitcode)
        getpgid.assert_not_called()
        killpg.assert_not_called()
        kill_process.assert_not_called()

    def test_external_reap_before_group_shutdown_never_signals_reused_group(
        self,
    ) -> None:
        """Catches shutdown signaling after another waiter consumes the child."""
        process = SpawnedWorkerProcess(4242)
        reap_locked = threading.Event()
        resume_reap = threading.Event()
        signal_errors: list[BaseException] = []

        def paused_external_reap(pid: int, options: int) -> tuple[int, int]:
            self.assertEqual((4242, os.WNOHANG), (pid, options))
            reap_locked.set()
            if not resume_reap.wait(1.0):
                raise RuntimeError("reap race did not resume")
            raise ChildProcessError

        def shutdown() -> None:
            try:
                shutdown_process_group(grace_seconds=0)
            except BaseException as exc:
                signal_errors.append(exc)

        with (
            mock.patch("floati.worker_exec.os.getpgid", return_value=4242),
            mock.patch("floati.worker_exec.os.waitpid", side_effect=paused_external_reap),
            mock.patch("floati.worker_exec.os.killpg") as killpg,
            mock.patch("floati.worker_exec.os.kill") as kill_process,
        ):
            self.assertTrue(process.confirm_process_group())
            shutdown_process_group = getattr(
                process, "shutdown_process_group", None,
            )
            self.assertTrue(
                callable(shutdown_process_group),
                "exec group shutdown is missing",
            )
            reap_thread = threading.Thread(target=process.join)
            reap_thread.start()
            self.assertTrue(reap_locked.wait(1.0))
            signal_thread = threading.Thread(target=shutdown)
            signal_thread.start()
            resume_reap.set()
            reap_thread.join(1.0)
            signal_thread.join(1.0)
            self.assertFalse(reap_thread.is_alive(), "reap decision deadlocked")
            self.assertFalse(signal_thread.is_alive(), "signal decision deadlocked")

        self.assertIsNone(process.exitcode)
        self.assertEqual([], signal_errors)
        killpg.assert_not_called()
        kill_process.assert_not_called()

    def test_timed_join_remains_bounded_during_serialized_signal_decision(
        self,
    ) -> None:
        """Catches the state lock turning bounded join into a signal deadlock."""
        process = SpawnedWorkerProcess(4242)
        signal_locked = threading.Event()
        resume_signal = threading.Event()
        join_finished = threading.Event()

        def paused_process_group(pid: int) -> int:
            self.assertEqual(4242, pid)
            signal_locked.set()
            if not resume_signal.wait(1.0):
                raise RuntimeError("signal decision did not resume")
            return pid

        def timed_join() -> None:
            process.join(0.05)
            join_finished.set()

        with (
            mock.patch("floati.worker_exec.os.waitpid", return_value=(0, 0)),
            mock.patch(
                "floati.worker_exec.os.getpgid", side_effect=paused_process_group,
            ),
            mock.patch("floati.worker_exec.os.killpg"),
        ):
            signal_thread = threading.Thread(target=process.terminate)
            signal_thread.start()
            self.assertTrue(signal_locked.wait(1.0))
            join_thread = threading.Thread(target=timed_join)
            join_thread.start()
            try:
                self.assertTrue(
                    join_finished.wait(0.15),
                    "timed join blocked behind the serialized signal decision",
                )
            finally:
                resume_signal.set()
                signal_thread.join(1.0)
                join_thread.join(1.0)
            self.assertFalse(signal_thread.is_alive())
            self.assertFalse(join_thread.is_alive())


class ThreadObservationConcurrencyTests(unittest.TestCase):
    def setUp(self) -> None:
        from floati.thread_observations import ThreadObserver, ThreadObservationLedger

        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = FloatiRoot.open(self.base, "alpha")
        Registry(self.root).register("owner-node", "Codex")
        Registry(self.root).register("observer-node", "Codex")
        self.first = WorkLog(self.root).add("thread race one", "owner-node", [])
        self.second = WorkLog(self.root).add("thread race two", "owner-node", [])
        self.Observer = ThreadObserver
        self.Ledger = ThreadObservationLedger

    def _race(
        self,
        operations: list[tuple[str, str, str, str | None, str]],
    ) -> list[tuple[str, str, str]]:
        context = multiprocessing.get_context("fork")
        start = context.Event()
        results = context.Queue()
        processes = [
            context.Process(
                target=_thread_concurrent_action,
                args=(
                    str(self.base),
                    action,
                    work_item_id,
                    thread_id,
                    attachment_id,
                    mode,
                    start,
                    results,
                ),
            )
            for action, work_item_id, thread_id, attachment_id, mode in operations
        ]
        for process in processes:
            process.start()
        start.set()
        observed = [results.get(timeout=15) for _ in processes]
        for process in processes:
            process.join(15)
            if process.is_alive():
                process.terminate()
                process.join(5)
            self.assertEqual(0, process.exitcode)
        return observed

    def _attachment(self) -> str:
        row = self.Observer(self.root).register_work_item(
            str(self.first["id"]), THREAD_ID, "observer-node", now=THREAD_NOW
        )
        return str(row["id"])

    def _ordered_race(
        self,
        first: tuple[str, str, str],
        second: tuple[str, str, str],
        attachment_id: str,
    ) -> list[tuple[str, str, str]]:
        context = multiprocessing.get_context("fork")
        start = context.Event()
        first_at_transaction = context.Event()
        first_committed = context.Event()
        results = context.Queue()
        processes = []
        for is_first, (action, mode, now_text) in (
            (True, first), (False, second),
        ):
            processes.append(
                context.Process(
                    target=_thread_ordered_action,
                    args=(
                        str(self.base), action, str(self.first["id"]), THREAD_ID,
                        attachment_id, mode, now_text, is_first, start,
                        first_at_transaction, first_committed, results,
                    ),
                )
            )
        for process in processes:
            process.start()
        start.set()
        observed = [results.get(timeout=15) for _ in processes]
        for process in processes:
            process.join(15)
            if process.is_alive():
                process.terminate()
                process.join(5)
            self.assertEqual(0, process.exitcode)
        return observed

    def test_competing_changed_registrations_have_one_physical_winner(self) -> None:
        results = self._race(
            [
                ("register", str(self.first["id"]), THREAD_ID, None, "idle"),
                ("register", str(self.second["id"]), THREAD_ID, None, "idle"),
            ]
        )
        self.assertEqual({"ok", "refused"}, {row[1] for row in results})
        self.assertEqual(
            {"thread_attachment_conflict"},
            {row[2] for row in results if row[1] == "refused"},
        )
        self.assertEqual(1, len(self.Ledger(self.root).records()))

    def test_exact_concurrent_registration_and_observation_retries_are_physical_once(self) -> None:
        registrations = self._race(
            [
                ("register", str(self.first["id"]), THREAD_ID, None, "idle")
                for _ in range(4)
            ]
        )
        self.assertEqual({"ok"}, {row[1] for row in registrations})
        self.assertEqual(1, len({row[2] for row in registrations}))
        attachment_id = registrations[0][2]

        observations = self._race(
            [
                (
                    "observe",
                    str(self.first["id"]),
                    THREAD_ID,
                    attachment_id,
                    "idle",
                )
                for _ in range(4)
            ]
        )
        self.assertEqual({"ok"}, {row[1] for row in observations})
        self.assertEqual(1, len({row[2] for row in observations}))
        self.assertEqual(2, len(self.Ledger(self.root).records()))

    def test_changed_snapshots_serialize_by_physical_transaction_order(self) -> None:
        attachment_id = self._attachment()
        results = self._ordered_race(
            ("observe", "active-input", "2026-08-13T12:00:02.000Z"),
            ("observe", "active-approval", "2026-08-13T12:00:01.000Z"),
            attachment_id,
        )
        self.assertEqual({"ok"}, {row[1] for row in results}, results)
        records = self.Ledger(self.root).records()
        observations = [
            row for row in records if row["kind"] == "thread_observation_recorded"
        ]
        self.assertEqual(2, len(observations))
        self.assertEqual(
            {"waiting_on_approval", "waiting_on_user_input"},
            {row["attention"]["value"] for row in observations},
        )
        self.assertEqual(
            ["waiting_on_user_input", "waiting_on_approval"],
            [row["attention"]["value"] for row in observations],
        )
        self.assertEqual(
            ["2026-08-13T12:00:02.000Z", "2026-08-13T12:00:01.000Z"],
            [row["observed_at_testimony"] for row in observations],
            "physical order must follow the transaction fence, not timestamps",
        )

    def test_observe_vs_detach_has_only_the_two_legal_physical_orders(self) -> None:
        for first_action, expected in (
            (
                "observe",
                [
                    "thread_attachment_registered",
                    "thread_observation_recorded",
                    "thread_attachment_detached",
                ],
            ),
            (
                "detach",
                ["thread_attachment_registered", "thread_attachment_detached"],
            ),
        ):
            with self.subTest(first_action=first_action):
                if first_action == "observe":
                    root = self.root
                    attachment_id = self._attachment()
                else:
                    directory = tempfile.TemporaryDirectory()
                    self.addCleanup(directory.cleanup)
                    self.base = Path(directory.name)
                    self.root = FloatiRoot.open(self.base, "alpha")
                    Registry(self.root).register("owner-node", "Codex")
                    Registry(self.root).register("observer-node", "Codex")
                    self.first = WorkLog(self.root).add(
                        "thread ordered detach", "owner-node", []
                    )
                    attachment_id = self._attachment()
                    root = self.root
                second_action = "detach" if first_action == "observe" else "observe"
                results = self._ordered_race(
                    (first_action, "idle", THREAD_NOW),
                    (second_action, "idle", THREAD_NOW),
                    attachment_id,
                )
                statuses = {(row[0], row[1]) for row in results}
                self.assertIn((first_action, "ok"), statuses)
                self.assertIn(
                    (second_action, "ok" if first_action == "observe" else "refused"),
                    statuses,
                )
                self.assertEqual(
                    expected,
                    [row["kind"] for row in self.Ledger(root).records()],
                )


class EffectLedgerConcurrencyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.case = _EffectCase(self)
        self.home = self.case.root.tenant_home
        self.root = self.case.root
        self.policy_path = self.case.run.policy_path

    def test_concurrent_same_idempotency_intent_has_one_physical_row(self) -> None:
        """Catches same-key retries resolving outside the real Effect-ledger writer lock."""
        candidates: list[tuple[str, dict[str, object]]] = []
        for index in range(4):
            candidate = self.case.intent_args(
                now=datetime(2026, 8, 9, 14, 0, index, tzinfo=timezone.utc)
            )
            candidates.append(("intent", candidate))
        results = _run_effect_race(self.home, self.policy_path, candidates)
        self.assertEqual({"ok"}, {status for status, _ in results})
        self.assertEqual(1, len({value for _, value in results}))
        self.assertEqual(1, len(EffectLedger(self.root).records()))

    def test_concurrent_same_outcome_retry_has_one_physical_row(self) -> None:
        """Catches exact outcome retries appending duplicate physical terminal testimony."""
        intent = self.case.controller.intent(**self.case.intent_args())
        self.case.controller.dispatched(
            intent["operation_id"], dispatch_adapter="git_local",
            dispatch_evidence_digest="d" * 64,
        )
        candidates: list[tuple[str, dict[str, object]]] = []
        for index in range(4):
            candidates.append(("failed", {
                "operation_id": intent["operation_id"],
                "reason_code": "effect_not_applied",
                "evidence_digest": "e" * 64,
                "spend_status": "complete",
                "measured_spend": [{"budget_id": "build", "amount": 0}],
                "now": datetime(2026, 8, 9, 14, 1, index, tzinfo=timezone.utc),
            }))
        results = _run_effect_race(self.home, self.policy_path, candidates)
        self.assertEqual({"ok"}, {status for status, _ in results})
        self.assertEqual(1, len({value for _, value in results}))
        self.assertEqual(3, len(EffectLedger(self.root).records()))

    def test_concurrent_conflicting_outcomes_have_one_winner_and_one_refusal(self) -> None:
        """Catches concurrent primary outcomes both becoming durable or both being refused."""
        intent = self.case.controller.intent(**self.case.intent_args())
        self.case.controller.dispatched(
            intent["operation_id"], dispatch_adapter="git_local",
            dispatch_evidence_digest="d" * 64,
        )
        common = {
            "operation_id": intent["operation_id"],
            "evidence_digest": "e" * 64,
            "spend_status": "unknown",
            "measured_spend": None,
        }
        results = _run_effect_race(self.home, self.policy_path, [
            ("failed", dict(common, reason_code="effect_not_applied")),
            ("unknown", dict(common, reason_code="confirmation_absent")),
        ])
        self.assertEqual(
            [("ok", 1), ("refused", 1)],
            sorted((status, sum(1 for item, _ in results if item == status)) for status in {item for item, _ in results}),
        )
        self.assertEqual(
            {"effect_transition_invalid"},
            {value for status, value in results if status == "refused"},
        )
        self.assertEqual(3, len(EffectLedger(self.root).records()))

    def test_effect_intent_and_run_acceptance_race_has_one_durable_winner(self) -> None:
        """Catches the truth-free acceptance fence admitting both cross-ledger writers."""
        from tests.test_runtruth import (
            _task6_acceptance_process,
            _task6_intent_process,
        )

        candidate = self.case.result_acceptance_candidate()
        context = multiprocessing.get_context("spawn")
        start = context.Event()
        outcomes = context.Queue()
        acceptance = context.Process(
            target=_task6_acceptance_process,
            args=(str(self.home), candidate, start, outcomes),
        )
        intent = context.Process(
            target=_task6_intent_process,
            args=(
                str(self.home), str(self.policy_path),
                self.case.intent_args(), start, outcomes,
            ),
        )
        acceptance.start()
        intent.start()
        start.set()
        acceptance.join(20)
        intent.join(20)
        self.assertEqual((0, 0), (acceptance.exitcode, intent.exitcode))

        observed = {outcomes.get(timeout=5)[0:2] for _ in range(2)}
        labels = {row[0] for row in observed}
        self.assertIn(
            labels,
            (
                {"acceptance_ok", "intent_refused"},
                {"intent_ok", "acceptance_refused"},
            ),
        )
        refusals = {row[1] for row in observed if row[0].endswith("refused")}
        self.assertTrue(
            refusals <= {"effect_attempt_accepted", "effect_binding_required"},
            refusals,
        )
        run_rows = RunLedger(self.root).records()
        effect_rows = EffectLedger(self.root).records()
        accepted_count = sum(row["kind"] == "result_accepted" for row in run_rows)
        self.assertIn((accepted_count, len(effect_rows)), ((1, 0), (0, 1)))
        lock_path = self.root.resolve_relative("effects/acceptance.lock")
        self.assertEqual(b"", lock_path.read_bytes())
        lock_path.unlink()
        self.assertEqual(run_rows, RunLedger(self.root).records())
        self.assertEqual(effect_rows, EffectLedger(self.root).records())

class WakeHoldConcurrencyTests(unittest.TestCase):
    """Cross-process controller controls; a per-ledger lock alone permits duplicate presentation."""

    def _root_with_message(self, base: Path) -> FloatiRoot:
        root = FloatiRoot.open(base, "alpha")
        registry = Registry(root)
        registry.register(public_ids.worker('alpha'), "worker")
        registry.register("bob", "worker")
        EventLog(root, registry).send(
            public_ids.worker('alpha'), "bob", "slipway", "a" * 40, "docs/evidence/wake-race.md",
            "wake race", idempotency_key="wake-race-message",
        )
        return root

    def _race(self, base: Path, keys: list[str]) -> list[tuple[str, object]]:
        context = multiprocessing.get_context("fork")
        start, results = context.Event(), context.Queue()
        processes = [context.Process(target=_wake_evaluate_hammer, args=(str(base), key, start, results)) for key in keys]
        for process in processes:
            process.start()
        start.set()
        observed = [results.get(timeout=10) for _ in processes]
        for process in processes:
            process.join(10)
            self.assertFalse(process.is_alive())
            self.assertEqual(0, process.exitcode)
        return observed

    def test_concurrent_same_key_creates_one_hold_row(self) -> None:
        """Catches response-loss retries appending multiple canonical hold rows."""
        with tempfile.TemporaryDirectory() as directory:
            root = self._root_with_message(Path(directory))
            observed = self._race(Path(directory), ["same-key"] * 4)
            self.assertEqual({"ok"}, {status for status, _value in observed})
            rows = read_records(root, "receipts/deliveries/bob.jsonl", allowed_kinds={"delivery_receipt", "wake_hold_receipt"})
            self.assertEqual(1, len(rows))

    def test_competing_keys_do_not_present_one_fresh_message_twice(self) -> None:
        """Catches independent decisions both classifying the same locked input as fresh."""
        with tempfile.TemporaryDirectory() as directory:
            root = self._root_with_message(Path(directory))
            observed = self._race(Path(directory), ["left-key", "right-key"])
            self.assertEqual({"ok"}, {status for status, _value in observed})
            waking = [artifact for _status, artifact in observed if artifact["wake_required"]]
            self.assertEqual(1, len(waking))
            rows = read_records(root, "receipts/deliveries/bob.jsonl", allowed_kinds={"delivery_receipt", "wake_hold_receipt"})
            self.assertEqual(1, len(rows))

    def test_identical_keys_are_independent_only_across_session_ledgers(self) -> None:
        """Catches a session lock namespace collapsing two lawful fresh inboxes together."""
        sessions = (
            "worker-018f7e9b3c137abc8def0123456789ab",
            "worker-018f7e9b3c147abc8def0123456789ab",
        )
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = FloatiRoot.open(base, "alpha")
            registry = Registry(root)
            registry.register(public_ids.worker('alpha'), "worker")
            registry.register("bob", "worker")
            for index, session in enumerate(sessions):
                EventLog(root, registry).send(
                    public_ids.worker('alpha'), "bob", "slipway", "a" * 40,
                    "docs/evidence/wake-race.md", "wake race",
                    idempotency_key=f"session-race-{index}",
                    worker_session_id=session,
                )
            context = multiprocessing.get_context("fork")
            start, results = context.Event(), context.Queue()
            processes = [
                context.Process(
                    target=_wake_session_evaluate_hammer,
                    args=(str(base), session, start, results),
                )
                for session in sessions
            ]
            for process in processes:
                process.start()
            start.set()
            observed = [results.get(timeout=10) for _ in processes]
            for process in processes:
                process.join(10)
                self.assertFalse(process.is_alive())
                self.assertEqual(0, process.exitcode)
            self.assertEqual({"ok"}, {status for status, _artifact in observed})
            self.assertEqual(
                {session for _status, artifact in observed for session in [artifact["worker_session_id"]]},
                set(sessions),
            )
            self.assertTrue(all(artifact["wake_required"] for _status, artifact in observed))

    def test_two_sessions_for_one_seat_share_one_exclusive_lane_lease(self) -> None:
        """Catches per-session locks letting two sessions for one seat act concurrently."""
        from floati.wake_hold import wake_coordination_guard

        sessions = (
            "worker-018f7e9b3c137abc8def0123456789ab",
            "worker-018f7e9b3c147abc8def0123456789ab",
        )
        with tempfile.TemporaryDirectory() as directory:
            root = FloatiRoot.open(Path(directory), "alpha")
            Registry(root).register("bob", "worker")
            first_locked = threading.Event()
            release_first = threading.Event()
            second_locked = threading.Event()

            def hold_first() -> None:
                with wake_coordination_guard(root, "bob", worker_session_id=sessions[0]):
                    first_locked.set()
                    self.assertTrue(release_first.wait(5))

            def take_second() -> None:
                self.assertTrue(first_locked.wait(5))
                with wake_coordination_guard(root, "bob", worker_session_id=sessions[1]):
                    second_locked.set()

            first = threading.Thread(target=hold_first)
            second = threading.Thread(target=take_second)
            first.start()
            second.start()
            self.assertTrue(first_locked.wait(5))
            self.assertFalse(second_locked.wait(0.1))
            release_first.set()
            first.join(5)
            second.join(5)
            self.assertFalse(first.is_alive())
            self.assertFalse(second.is_alive())
            self.assertTrue(second_locked.is_set())
            self.assertEqual(
                ["lane.lock"],
                sorted(path.name for path in self.root_paths(root, "bob")),
            )

    @staticmethod
    def root_paths(root: FloatiRoot, node: str) -> list[Path]:
        path = root.resolve_relative(Path("receipts/wake-coordination") / node)
        return list(path.iterdir()) if path.exists() else []

    def _ordered_race(self, base: Path, first_action: str, second_action: str, item_id: str, session: str | None) -> list[tuple[str, str, str]]:
        context = multiprocessing.get_context("fork")
        first_locked, release_first, results = context.Event(), context.Event(), context.Queue()
        first = context.Process(target=_wake_ordered_action, args=(str(base), first_action, item_id, session, True, first_locked, release_first, results))
        second = context.Process(target=_wake_ordered_action, args=(str(base), second_action, item_id, session, False, first_locked, release_first, results))
        first.start()
        self.assertTrue(first_locked.wait(5))
        second.start()
        time.sleep(0.05)
        release_first.set()
        first.join(10)
        second.join(10)
        self.assertEqual((0, 0), (first.exitcode, second.exitcode))
        return [results.get(timeout=2), results.get(timeout=2)]

    def test_evaluation_and_acknowledgment_are_serialized_in_both_lock_orders(self) -> None:
        """Catches ack and exact retry observing or publishing stale interleaved state."""
        from floati.wake_hold import WakeHoldController

        for first_action in ("evaluate", "ack"):
            with self.subTest(first_action=first_action), tempfile.TemporaryDirectory() as directory:
                base = Path(directory)
                root = self._root_with_message(base)
                item_id = str(EventLog(root).records()[0]["id"])
                WakeHoldController(root).evaluate("bob", idempotency_key="ordered-seed")
                other = "ack" if first_action == "evaluate" else "evaluate"
                observed = self._ordered_race(base, first_action, other, item_id, None)
                self.assertEqual({"ok"}, {status for _action, status, _state in observed})
                final = WakeHoldController(root).evaluate("bob", idempotency_key="ordered-seed")
                self.assertEqual("caught_up", final["state"])
                if first_action == "ack":
                    self.assertIn(("evaluate", "ok", "caught_up"), observed)

    def test_evaluation_and_retraction_are_serialized_in_both_lock_orders(self) -> None:
        """Catches retraction and exact retry deadlocking or returning a stale wake."""
        from floati.wake_hold import WakeHoldController

        session = "worker-018f7e9b3c137abc8def0123456789ab"
        for first_action in ("evaluate", "retract"):
            with self.subTest(first_action=first_action), tempfile.TemporaryDirectory() as directory:
                base = Path(directory)
                root = FloatiRoot.open(base, "alpha")
                registry = Registry(root)
                registry.register(public_ids.worker('alpha'), "worker")
                registry.register("bob", "worker")
                item = EventLog(root, registry).send(public_ids.worker('alpha'), "bob", "slipway", "a" * 40, "docs/evidence/wake-race.md", "wake race", idempotency_key="wake-race-message", worker_session_id=session)
                WakeHoldController(root).evaluate("bob", idempotency_key="ordered-seed", worker_session_id=session)
                other = "retract" if first_action == "evaluate" else "evaluate"
                observed = self._ordered_race(base, first_action, other, str(item["id"]), session)
                self.assertEqual({"ok"}, {status for _action, status, _state in observed})
                final = WakeHoldController(root).evaluate("bob", idempotency_key="ordered-seed", worker_session_id=session)
                self.assertEqual("caught_up", final["state"])
                if first_action == "retract":
                    self.assertIn(("evaluate", "ok", "caught_up"), observed)


if __name__ == "__main__":
    unittest.main()

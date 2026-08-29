from __future__ import annotations

import errno
import fcntl
import json
import hashlib
import multiprocessing
import os
import shutil
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
import tempfile
import types
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from floati.errors import IntegrityFailure, ProtocolRefusal
from floati.approvals import ApprovalLedger
from floati.effects import EffectController, EffectLedger
from floati.ids import uuid7_hex
from floati.planes import AuthorityGrantStore
from floati.registry import Registry
from floati.root import FloatiRoot
from floati.work import WORK_KINDS, WorkLog
from floati.jsonl import append_record
from floati.worker_bootstrap_protocol import (
    BootstrapChannel,
    BuiltInAdapterSpec,
    isolation_policy_from_payload,
    isolation_policy_to_payload,
    validate_isolation_backend,
)
from floati.worker_isolation import cleanup_worker_isolation, prepare_worker_isolation
from tests.test_spawn_groups import _Task2Case

try:
    from floati.workers import WorkerAdapterFailure, WorkerReceipts, WorkerRefusals, WorkerRunner, _adapter_process
except (ImportError, ModuleNotFoundError):
    WorkerAdapterFailure = RuntimeError
    WorkerReceipts = None
    WorkerRefusals = None
    WorkerRunner = None


NOW = datetime(2026, 7, 31, 20, 0, tzinfo=timezone.utc)

_CODEX_REFERENCE_HARNESS = (
    Path(__file__).parent
    / "fixtures"
    / "codex-app-server"
    / "reference_harness.py"
)


def _codex_reference_command(*arguments: str) -> tuple[str, ...]:
    """Return one valid built-in command for real fresh-exec Worker tests."""

    return (
        os.path.realpath(sys.executable),
        os.fspath(_CODEX_REFERENCE_HARNESS),
        "--mode",
        "complete",
        *arguments,
    )


class _CompletingAdapter:
    name = "fixture"

    def __init__(self, work: WorkLog) -> None:
        self.work = work
        self.spawned_after_claim = False

    def spawn(self, item: dict, *, deadline_seconds: float) -> object:
        self.spawned_after_claim = self.work.show(item["id"])[0]["state"] == "claimed"
        if not self.spawned_after_claim:
            raise RuntimeError("adapter observed work before claim")
        return object()

    def drive(self, handle: object, item: dict, *, deadline_seconds: float) -> list[dict[str, str]]:
        return [{"repo": "slipway-proof", "sha": "a" * 40, "doc": "README.md"}]


class _RuntimeIdentityAdapter:
    name = "fixture"

    def __init__(self, evidence: Path) -> None:
        self.evidence = evidence

    def spawn(self, item: dict, *, deadline_seconds: float) -> object:
        return object()

    def drive(
        self, handle: object, item: dict, *, deadline_seconds: float,
    ) -> list[dict[str, str]]:
        self.evidence.write_text(
            multiprocessing.current_process().name, encoding="utf-8",
        )
        return []


class _DyingAdapter:
    name = "fixture"

    def spawn(self, item: dict, *, deadline_seconds: float) -> object:
        raise WorkerAdapterFailure("process_died")

    def drive(self, handle: object, item: dict, *, deadline_seconds: float) -> list[dict[str, str]]:
        raise AssertionError("drive must not follow a dead process")


class _HangingAdapter:
    name = "fixture"

    def spawn(self, item: dict, *, deadline_seconds: float) -> object:
        time.sleep(0.2)
        return object()

    def drive(self, handle: object, item: dict, *, deadline_seconds: float) -> list[dict[str, str]]:
        return []


class _ExplodingAdapter:
    name = "fixture"

    def spawn(self, item: dict, *, deadline_seconds: float) -> object:
        raise RuntimeError("untyped adapter failure")

    def drive(self, handle: object, item: dict, *, deadline_seconds: float) -> list[dict[str, str]]:
        return []


class _ProcessDeathAdapter:
    name = "fixture"

    def spawn(self, item: dict, *, deadline_seconds: float) -> object:
        os._exit(7)

    def drive(self, handle: object, item: dict, *, deadline_seconds: float) -> list[dict[str, str]]:
        return []


class _MalformedOutputAdapter:
    name = "fixture"

    def spawn(self, item: dict, *, deadline_seconds: float) -> object:
        return object()

    def drive(self, handle: object, item: dict, *, deadline_seconds: float) -> list[dict[str, str]]:
        return [{}]


class _DeadlineAdapter:
    name = "fixture"

    def __init__(self, evidence: Path) -> None:
        self.evidence = evidence

    def _record(self, phase: str, deadline_seconds: float) -> None:
        with self.evidence.open("a", encoding="utf-8") as handle:
            handle.write(f"{phase}:{deadline_seconds:.3f}\n")

    def spawn(self, item: dict, *, deadline_seconds: float) -> object:
        self._record("spawn", deadline_seconds)
        return object()

    def drive(self, handle: object, item: dict, *, deadline_seconds: float) -> list[dict[str, str]]:
        self._record("drive", deadline_seconds)
        return [{"repo": "slipway-proof", "sha": "a" * 40, "doc": "README.md"}]


class _WorkspaceAdapter(_DeadlineAdapter):
    name = "codex"
    requires_workspace = True


class _CorruptAuthorityAfterDriveAdapter(_CompletingAdapter):
    def __init__(self, work: WorkLog, authority_path: Path) -> None:
        super().__init__(work)
        self.authority_path = authority_path

    def drive(self, handle: object, item: dict, *, deadline_seconds: float) -> list[dict[str, str]]:
        self.authority_path.write_text('{"incomplete":', encoding="utf-8")
        return super().drive(handle, item, deadline_seconds=deadline_seconds)


class _GrandchildHangAdapter:
    name = "fixture"

    def __init__(self, pid_path: Path) -> None:
        self.pid_path = pid_path

    def spawn(self, item: dict, *, deadline_seconds: float) -> object:
        child = subprocess.Popen(["sleep", "30"])
        self.pid_path.write_text(str(child.pid), encoding="utf-8")
        try:
            time.sleep(30)
        finally:
            try:
                child.wait(timeout=1)
            except subprocess.TimeoutExpired:
                child.terminate()
                child.wait(timeout=1)
        return object()

    def drive(self, handle: object, item: dict, *, deadline_seconds: float) -> list[dict[str, str]]:
        return []


class _CrashAfterCodexSpawnAdapter:
    name = "codex"
    requires_workspace = True

    def __init__(self, command: tuple[str, ...]) -> None:
        from floati.adapters.codex_live import CodexAppServerAdapter

        self.live = CodexAppServerAdapter(command)

    def set_process_group_registrar(self, registrar: object) -> None:
        self.live.set_process_group_registrar(registrar)  # type: ignore[arg-type]

    def spawn(self, item: dict, *, deadline_seconds: float) -> object:
        self.live.spawn(item, deadline_seconds=deadline_seconds)
        workspace = Path(str(item["workspace"]))
        evidence = workspace / ".floati"
        if not evidence.is_dir():
            raise AssertionError("Floati evidence must exist before adapter metadata")
        if os.path.lexists(workspace / ".slipway"):
            raise AssertionError("a Floati worker must not create legacy evidence")
        (evidence / "adapter.pgid").write_text(
            str(os.getpgrp()), encoding="utf-8"
        )
        os._exit(7)

    def drive(self, handle: object, item: dict, *, deadline_seconds: float) -> list[dict[str, str]]:
        raise AssertionError("drive must not follow an adapter crash")


class _SpawnContextAdapter:
    name = "fixture"

    def set_spawn_context(self, context: dict[str, object], emit: object) -> None:
        self.context = context
        emit({"provider_descendant_id": "native-1", "state": "observed", "adopted_item_id": None})
        emit({"provider_descendant_id": "native-1", "state": "terminated", "adopted_item_id": None})

    def spawn(self, item: dict, *, deadline_seconds: float) -> object:
        return object()

    def drive(self, handle: object, item: dict, *, deadline_seconds: float) -> list[dict[str, str]]:
        return []


class _EffectReportingAdapter:
    name = "codex"

    def __init__(
        self,
        events: tuple[dict[str, object], ...],
        *,
        report_during_drive: bool = False,
        die_after_reports: bool = False,
    ) -> None:
        self.events = events
        self.report_during_drive = report_during_drive
        self.die_after_reports = die_after_reports
        self.emit = None

    def set_spawn_context(self, context: dict[str, object], emit: object) -> None:
        return None

    def set_effect_context(self, context: dict[str, object], emit: object) -> None:
        self.effect_context = context
        self.emit = emit
        if not self.report_during_drive:
            self._report()

    def _report(self) -> None:
        assert callable(self.emit)
        for event in self.events:
            self.emit(event)
        if self.die_after_reports:
            os._exit(7)

    def spawn(self, item: dict, *, deadline_seconds: float) -> object:
        return object()

    def drive(
        self, handle: object, item: dict, *, deadline_seconds: float,
    ) -> list[dict[str, str]]:
        if self.report_during_drive:
            self._report()
        return [{"repo": "slipway-proof", "sha": "a" * 40, "doc": "README.md"}]


class _EffectWorkerCase:
    """One real Run/Work/Effect fixture for the Worker private pipe."""

    def __init__(self, testcase: unittest.TestCase, adapter: object) -> None:
        self.testcase = testcase
        self.run = _Task2Case(testcase)
        self.run.prepare_parent(mode="disabled")
        self.root = self.run.root
        Registry(self.root).register("node-a", "Codex")
        work_item = {
            "schema_version": 0,
            "id": self.run.parent,
            "tenant_id": self.root.tenant_id,
            "timestamp": "2026-08-10T12:00:07.000Z",
            "kind": "work_item",
            "title": "report one governed external effect",
            "owner": "node-a",
            "artifact_bindings": [],
        }
        workspace = Path("/private/tmp/floati-work") / self.run.parent
        shutil.rmtree(workspace, ignore_errors=True)
        testcase.addCleanup(shutil.rmtree, workspace, True)
        work_item["workspace"] = str(workspace)
        append_record(
            self.root,
            "work/items.jsonl",
            work_item,
            allowed_kinds=WORK_KINDS,
        )
        AuthorityGrantStore(self.root).claim(
            "work-claims", "node-a", 120, 120, self.run.now(7),
        )
        self.effect_ledger = EffectLedger(self.root)
        self.effect_controller = EffectController(
            self.effect_ledger,
            self.run.ledger,
            self.run.policy,
            ApprovalLedger(self.root),
        )
        self.adapter = adapter

    @staticmethod
    def intent_event(**changes: object) -> dict[str, object]:
        event: dict[str, object] = {
            "verb": "intent",
            "effect_type": "git_ref_update",
            "target": {
                "kind": "git_ref",
                "coordinate": "owner/slipway:refs/heads/main",
                "identity_digest": "a" * 64,
            },
            "request_digest": hashlib.sha256(b"worker effect request").hexdigest(),
            "idempotency_key": "worker-effect-one",
            "expected_confirmation": {
                "kind": "git_ref_equals",
                "locator": "refs/heads/main",
                "expected_digest": "b" * 64,
            },
            "reconciliation_adapter": "git_local",
            "risk_class": "low",
            "budget_claim": [{"budget_id": "build", "amount": 1}],
            "requested_by": "node-a",
            "approval_request_id": None,
            "approval_decision_id": None,
            "approval_consumption_id": None,
        }
        event.update(changes)
        return event

    @staticmethod
    def dispatch_event(**changes: object) -> dict[str, object]:
        event: dict[str, object] = {
            "verb": "dispatch",
            "idempotency_key": "worker-effect-one",
            "dispatch_adapter": "git_local",
            "dispatch_evidence_digest": "c" * 64,
        }
        event.update(changes)
        return event

    @staticmethod
    def acknowledgement_event(**changes: object) -> dict[str, object]:
        event: dict[str, object] = {
            "verb": "acknowledgement",
            "idempotency_key": "worker-effect-one",
            "acknowledgement_digest": "d" * 64,
        }
        event.update(changes)
        return event

    @staticmethod
    def unknown_event(**changes: object) -> dict[str, object]:
        event: dict[str, object] = {
            "verb": "unknown",
            "idempotency_key": "worker-effect-one",
            "reason_code": "confirmation_absent",
            "evidence_digest": "e" * 64,
            "spend_status": "unknown",
            "measured_spend": None,
        }
        event.update(changes)
        return event

    def runner(
        self,
        *,
        instrument_exec: bool = True,
        command: tuple[str, ...] | None = None,
    ) -> WorkerRunner:
        runner = WorkerRunner(
            self.root,
            {"codex": self.adapter},
            clock=lambda: self.run.now(8),
            effect_controller=self.effect_controller,
            effect_adapter_specs={
                "codex": BuiltInAdapterSpec(
                    "codex", command or _codex_reference_command(),
                ),
            },
        )
        if instrument_exec:
            patcher = mock.patch(
                "floati.workers.spawn_effect_worker",
                side_effect=self._spawn_instrumented_effect_worker,
            )
            patcher.start()
            self.testcase.addCleanup(patcher.stop)
        return runner

    def _spawn_instrumented_effect_worker(
        self, bootstrap_path: Path, launch_payload: dict[str, object],
    ) -> tuple[object, object]:
        """Exercise parent protocol semantics without claiming kernel proof."""
        del bootstrap_path
        policy = isolation_policy_from_payload(launch_payload["isolation_policy"])
        context = multiprocessing.get_context("fork")
        parent_channel, child_channel = context.Pipe()
        process = context.Process(
            target=_adapter_process,
            args=(
                child_channel,
                self.adapter,
                launch_payload["item"],
                launch_payload["deadline_millis"] / 1_000.0,
                launch_payload["spawn_context"],
                launch_payload["effect_context"],
                policy,
            ),
            name="floati-worker-adapter-instrumented",
        )
        process.start()
        child_channel.close()
        return process, parent_channel

    def execute(
        self, *, on_drive: object = None, include_context: bool = True,
    ) -> dict[str, object]:
        coordinates = (
            {
                "run_id": self.run.run_id,
                "item_id": self.run.parent,
                "attempt_id": self.run.opened["attempt_id"],
            }
            if include_context else {}
        )
        return self.runner().run(
            "node-a", "codex", now=self.run.now(8), on_drive=on_drive,
            **coordinates,
        )


def _governed_two_way_success_child(connection: object) -> None:
    """Exercise the private-pipe messages required after a governed result."""

    try:
        connection.send(("spawned", None))
        connection.send(("result", [{
            "repo": "slipway-proof", "sha": "a" * 40, "doc": "README.md",
        }]))
        if connection.recv() != ("observation_closed", None):
            connection.send(("failure", "adapter_error"))
            return
        connection.send(("observation_closed_ack", None))
    finally:
        connection.close()


class WorkerContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = FloatiRoot.open_direct_home(Path(self.temp.name) / "fleet", create=True)
        Registry(self.root).register("lane-a", "Codex")
        self.work = WorkLog(self.root)
        self.item = self.work.add("write README line", "lane-a", [], now=NOW)

    def grant(self) -> None:
        AuthorityGrantStore(self.root).claim("work-claims", "lane-a", 60, 60, NOW)

    def test_receipts_project_only_the_latest_durable_worker_state(self) -> None:
        self.assertIsNotNone(WorkerReceipts, "worker receipt contract must exist")
        self.grant()
        claim = self.work.claim(self.item["id"], "lane-a", "work-claims", 1, now=NOW)
        receipts = WorkerReceipts(self.root)
        session = "worker-" + uuid7_hex()
        receipts.append(session, self.item["id"], "lane-a", "fixture", "claim", None, [], now=NOW)
        receipts.append(session, self.item["id"], "lane-a", "fixture", "spawn", None, [], now=NOW)
        receipts.append(session, self.item["id"], "lane-a", "fixture", "drive", None, [], now=NOW)

        projected = receipts.sessions()
        self.assertEqual(1, len(projected))
        self.assertEqual("driving", projected[0]["state"])
        self.assertEqual(claim["authority_epoch"], projected[0]["authority_epoch"])
        self.assertEqual([], projected[0]["artifact_bindings"])

    def test_runner_claims_before_adapter_and_binds_completion_artifact(self) -> None:
        self.assertIsNotNone(WorkerRunner, "worker runner must exist")
        self.grant()
        adapter = _CompletingAdapter(self.work)

        result = WorkerRunner(self.root, {"fixture": adapter}).run("lane-a", "fixture", now=NOW)

        self.assertEqual("complete", result["transition"])
        item = self.work.show(self.item["id"])[0]
        self.assertEqual("completed", item["state"])
        self.assertEqual("README.md", item["artifact_bindings"][0]["doc"])
        transitions = [row["transition"] for row in WorkerReceipts(self.root).records()]
        self.assertEqual(["claim", "spawn", "drive", "bind_artifact", "complete"], transitions)

    def test_runner_worker_process_announces_floati_identity(self) -> None:
        """Catches a shipped WorkerRunner child retaining the retired process label."""
        self.assertIsNotNone(WorkerRunner, "worker runner must exist")
        self.grant()
        evidence = Path(self.temp.name) / "worker-process-name"

        result = WorkerRunner(
            self.root, {"fixture": _RuntimeIdentityAdapter(evidence)},
        ).run("lane-a", "fixture", now=NOW)

        self.assertEqual("complete", result["transition"])
        self.assertEqual("floati-worker-adapter", evidence.read_text(encoding="utf-8"))

    def test_legacy_receive_does_not_sample_descendant_observation_clock(self) -> None:
        """A launch without spawn coordinates never consults the descendant clock."""

        self.grant()
        adapter = _CompletingAdapter(self.work)

        def forbidden_clock() -> datetime:
            raise RuntimeError("legacy receive sampled descendant observation time")

        result = WorkerRunner(
            self.root, {"fixture": adapter}, clock=forbidden_clock,
        ).run("lane-a", "fixture", now=NOW)

        self.assertEqual("complete", result["transition"])
        self.assertEqual("completed", self.work.show(self.item["id"])[0]["state"])

    def test_spawn_context_and_descendant_events_cross_the_real_fork_pipe(self) -> None:
        context = {
            "schema_version": 1, "run_id": "run-a", "item_id": "work-a",
            "attempt_id": "attempt-a", "fence_token": "f" * 64,
            "attempt_spawn_policy_id": "attempt-spawn-policy-bound-a",
            "adapter": "fixture", "subagents_mode": "observed_only",
        }
        fork = multiprocessing.get_context("fork")
        parent, child = fork.Pipe()
        process = fork.Process(
            target=_adapter_process,
            args=(child, _SpawnContextAdapter(), {"id": "work-a"}, 2.0, context),
        )
        process.start()
        child.close()
        messages = [parent.recv(), parent.recv(), parent.recv(), parent.recv()]
        self.assertTrue(process.is_alive())
        parent.send(("observation_closed", None))
        self.assertEqual(("observation_closed_ack", None), parent.recv())
        process.join(2)
        parent.close()
        self.assertEqual(
            ["descendant", "descendant", "spawned", "result"],
            [row[0] for row in messages],
        )
        self.assertEqual("observed", messages[0][1]["state"])
        self.assertEqual("terminated", messages[1][1]["state"])
        self.assertEqual(0, process.exitcode)

    def test_governed_private_pipe_represents_a_two_way_observation_close(self) -> None:
        fork = multiprocessing.get_context("fork")
        parent, child = fork.Pipe()
        process = fork.Process(target=_governed_two_way_success_child, args=(child,))
        process.start()
        child.close()
        self.assertEqual(("spawned", None), parent.recv())
        self.assertEqual("result", parent.recv()[0])
        parent.send(("observation_closed", None))
        self.assertEqual(("observation_closed_ack", None), parent.recv())
        process.join(2)
        parent.close()
        self.assertEqual(0, process.exitcode)

    def test_disabled_spawn_context_needs_no_observation_close_ack(self) -> None:
        class DisabledAdapter:
            name = "fixture"

            def set_spawn_context(
                self, context: dict[str, object], emit: object,
            ) -> None:
                return None

            def spawn(self, item: dict, *, deadline_seconds: float) -> object:
                return object()

            def drive(
                self, handle: object, item: dict, *, deadline_seconds: float,
            ) -> list[dict[str, str]]:
                return []

        context = {
            "schema_version": 1, "run_id": "run-a", "item_id": "work-a",
            "attempt_id": "attempt-a", "fence_token": "f" * 64,
            "attempt_spawn_policy_id": "attempt-spawn-policy-bound-a",
            "adapter": "fixture", "subagents_mode": "disabled",
        }
        fork = multiprocessing.get_context("fork")
        parent, child = fork.Pipe()
        process = fork.Process(
            target=_adapter_process,
            args=(child, DisabledAdapter(), {"id": "work-a"}, 5.0, context),
        )
        process.start()
        child.close()
        messages = [parent.recv(), parent.recv()]
        process.join(1)
        finished_without_ack = not process.is_alive()
        if process.is_alive():
            parent.send(("observation_closed", None))
            process.join(2)
        parent.close()
        self.assertEqual(["spawned", "result"], [row[0] for row in messages])
        self.assertTrue(finished_without_ack)
        self.assertEqual(0, process.exitcode)

    def test_runner_records_typed_degradation_after_process_death(self) -> None:
        self.assertIsNotNone(WorkerRunner, "worker runner must exist")
        self.grant()

        result = WorkerRunner(self.root, {"fixture": _DyingAdapter()}).run(
            "lane-a", "fixture", now=NOW
        )

        self.assertEqual("degrade", result["transition"])
        self.assertEqual("process_died", result["outcome_code"])
        self.assertEqual("claimed", self.work.show(self.item["id"])[0]["state"])
        self.assertEqual("degraded", WorkerReceipts(self.root).sessions()[0]["state"])

    def test_runner_refuses_without_exact_live_authority_before_adapter_action(self) -> None:
        self.assertIsNotNone(WorkerRunner, "worker runner must exist")
        adapter = _CompletingAdapter(self.work)

        with self.assertRaises(ProtocolRefusal) as caught:
            WorkerRunner(self.root, {"fixture": adapter}).run("lane-a", "fixture", now=NOW)

        self.assertEqual("worker_authority_missing", caught.exception.code)
        self.assertFalse(adapter.spawned_after_claim)
        self.assertEqual([], WorkerReceipts(self.root).records())
        refusals = WorkerRefusals(self.root).records()
        self.assertEqual(1, len(refusals))
        self.assertEqual("worker_authority_missing", refusals[0]["reason_code"])
        self.assertIsNone(refusals[0]["work_item_id"])

    def test_one_work_claim_cannot_bind_two_worker_sessions(self) -> None:
        self.grant()
        self.work.claim(self.item["id"], "lane-a", "work-claims", 1, now=NOW)
        receipts = WorkerReceipts(self.root)
        receipts.append(
            "worker-018f0f23abcd71238000000000000000",
            self.item["id"], "lane-a", "fixture", "claim", None, [], now=NOW,
        )

        with self.assertRaises(ProtocolRefusal) as caught:
            receipts.append(
                "worker-018f0f23abce71238000000000000000",
                self.item["id"], "lane-a", "fixture", "claim", None, [], now=NOW,
            )

        self.assertEqual("worker_claim_already_bound", caught.exception.code)
        self.assertEqual(1, len(receipts.sessions()))

    def test_concurrent_receipt_branches_append_exactly_one_transition(self) -> None:
        self.grant()
        self.work.claim(self.item["id"], "lane-a", "work-claims", 1, now=NOW)
        receipts = WorkerReceipts(self.root)
        session = "worker-018f0f23abcd71238000000000000000"
        receipts.append(session, self.item["id"], "lane-a", "fixture", "claim", None, [], now=NOW)

        def append_spawn() -> str:
            try:
                receipts.append(
                    session, self.item["id"], "lane-a", "fixture", "spawn", None, [], now=NOW
                )
                return "ok"
            except ProtocolRefusal as exc:
                return exc.code

        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = sorted(executor.map(lambda _: append_spawn(), range(2)))

        self.assertEqual(["ok", "worker_transition_invalid"], outcomes)
        self.assertEqual(["claim", "spawn"], [row["transition"] for row in receipts.records()])

    def test_projection_rejects_schema_valid_forged_transition_order(self) -> None:
        self.grant()
        claimed = self.work.claim(self.item["id"], "lane-a", "work-claims", 1, now=NOW)
        forged = {
            "schema_version": 0,
            "id": "worker-receipt-" + uuid7_hex(),
            "tenant_id": self.root.tenant_id,
            "timestamp": "2026-07-31T20:00:00.000Z",
            "kind": "worker_receipt",
            "session_id": "worker-" + uuid7_hex(),
            "work_item_id": self.item["id"],
            "node_id": "lane-a",
            "adapter": "fixture",
            "transition": "complete",
            "outcome_code": None,
            "authority_subject": claimed["authority_subject"],
            "authority_epoch": claimed["authority_epoch"],
            "artifact_bindings": [],
        }
        path = self.root.resolve_relative("receipts/workers.jsonl")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(forged, separators=(",", ":")) + "\n", encoding="utf-8")

        with self.assertRaises(IntegrityFailure) as caught:
            WorkerReceipts(self.root).sessions()

        self.assertEqual("worker_transition_invalid", caught.exception.code)

    def test_completed_work_cannot_gain_a_retroactive_worker_session(self) -> None:
        self.grant()
        self.work.claim(self.item["id"], "lane-a", "work-claims", 1, now=NOW)
        self.work.complete(self.item["id"], "lane-a", [], now=NOW)

        with self.assertRaises(ProtocolRefusal) as caught:
            WorkerReceipts(self.root).append(
                "worker-018f0f23abcd71238000000000000000",
                self.item["id"], "lane-a", "fixture", "claim", None, [], now=NOW,
            )

        self.assertEqual("worker_claim_missing", caught.exception.code)

    def test_complete_receipt_must_match_the_work_completion_bindings(self) -> None:
        self.grant()
        self.work.claim(self.item["id"], "lane-a", "work-claims", 1, now=NOW)
        receipts = WorkerReceipts(self.root)
        session = "worker-018f0f23abcd71238000000000000000"
        binding_a = {"repo": "slipway", "sha": "a" * 40, "doc": "README.md"}
        binding_b = {"repo": "slipway", "sha": "b" * 40, "doc": "README.md"}
        for transition in ("claim", "spawn", "drive"):
            receipts.append(
                session, self.item["id"], "lane-a", "fixture",
                transition, None, [], now=NOW,
            )
        receipts.append(
            session, self.item["id"], "lane-a", "fixture",
            "bind_artifact", None, [binding_a], now=NOW,
        )
        self.work.complete(self.item["id"], "lane-a", [binding_b], now=NOW)

        with self.assertRaises(ProtocolRefusal) as caught:
            receipts.append(
                session, self.item["id"], "lane-a", "fixture",
                "complete", None, [binding_a], now=NOW,
            )

        self.assertEqual("worker_completion_mismatch", caught.exception.code)

    def test_malformed_adapter_output_becomes_typed_degradation(self) -> None:
        self.grant()

        result = WorkerRunner(self.root, {"fixture": _MalformedOutputAdapter()}).run(
            "lane-a", "fixture", now=NOW
        )

        self.assertEqual("degrade", result["transition"])
        self.assertEqual("adapter_malformed_output", result["outcome_code"])
        self.assertEqual("degraded", WorkerReceipts(self.root).sessions()[0]["state"])

    def test_unknown_node_refusal_is_durable(self) -> None:
        with self.assertRaises(ProtocolRefusal) as caught:
            WorkerRunner(self.root, {"fixture": _CompletingAdapter(self.work)}).run(
                "ghost", "fixture", now=NOW
            )

        self.assertEqual("unknown_node", caught.exception.code)
        refusals = WorkerRefusals(self.root).records()
        self.assertEqual(1, len(refusals))
        self.assertEqual("worker_node_inactive", refusals[0]["reason_code"])
        self.assertIsNone(refusals[0]["work_item_id"])

    def test_claim_race_refusal_is_durable(self) -> None:
        self.grant()
        runner = WorkerRunner(self.root, {"fixture": _CompletingAdapter(self.work)})

        def lose_claim(*args: object, **kwargs: object) -> dict:
            raise ProtocolRefusal("work_not_open", "another claimant won")

        runner.work.claim_owned_oldest = lose_claim  # type: ignore[method-assign]
        with self.assertRaises(ProtocolRefusal) as caught:
            runner.run("lane-a", "fixture", now=NOW)

        self.assertEqual("work_not_open", caught.exception.code)
        refusals = WorkerRefusals(self.root).records()
        self.assertEqual("worker_claim_lost", refusals[-1]["reason_code"])
        self.assertIsNone(refusals[-1]["work_item_id"])

    def test_claim_time_authority_turnover_stays_out_of_work_plane(self) -> None:
        self.grant()
        runner = WorkerRunner(
            self.root, {"fixture": _CompletingAdapter(self.work)}
        )

        def lose_authority(*args: object, **kwargs: object) -> dict:
            raise ProtocolRefusal("authority_inactive", "authority turned over")

        runner.work.claim_owned_oldest = lose_authority  # type: ignore[method-assign]
        with self.assertRaises(ProtocolRefusal) as caught:
            runner.run("lane-a", "fixture", now=NOW)

        self.assertEqual("worker_authority_changed", caught.exception.code)
        refusals = WorkerRefusals(self.root).records()
        self.assertEqual("worker_authority_changed", refusals[-1]["reason_code"])

    def test_claim_time_integrity_failure_is_consumption_state_unavailable(self) -> None:
        self.grant()
        adapter = _CompletingAdapter(self.work)
        runner = WorkerRunner(self.root, {"fixture": adapter})

        def corrupt_claim(*args: object, **kwargs: object) -> dict:
            raise IntegrityFailure(
                "consumption_state_unavailable", "claim revalidation failed"
            )

        runner.work.claim_owned_oldest = corrupt_claim  # type: ignore[method-assign]
        with self.assertRaises(IntegrityFailure) as caught:
            runner.run("lane-a", "fixture", now=NOW)

        self.assertEqual("consumption_state_unavailable", caught.exception.code)
        self.assertFalse(adapter.spawned_after_claim)
        refusals = WorkerRefusals(self.root).records()
        self.assertEqual("consumption_state_unavailable", refusals[-1]["reason_code"])

    def test_authority_integrity_failure_keeps_its_plane_honest(self) -> None:
        self.grant()
        self.root.resolve_relative("authority-grants/work-claims.jsonl").write_text(
            '{"incomplete":', encoding="utf-8"
        )

        with self.assertRaises(IntegrityFailure) as caught:
            WorkerRunner(
                self.root, {"fixture": _CompletingAdapter(self.work)}
            ).run("lane-a", "fixture", now=NOW)

        self.assertEqual("authority_state_unavailable", caught.exception.code)
        refusals = WorkerRefusals(self.root).records()
        self.assertEqual("authority_state_unavailable", refusals[-1]["reason_code"])

    def test_prelaunch_authority_corruption_records_terminal_plane_honest_degradation(self) -> None:
        self.grant()
        adapter = _CompletingAdapter(self.work)
        runner = WorkerRunner(self.root, {"fixture": adapter})
        claim = runner.work.claim_owned_oldest
        authority_path = self.root.resolve_relative(
            "authority-grants/work-claims.jsonl"
        )

        def corrupt_after_claim(*args: object, **kwargs: object) -> dict:
            item = claim(*args, **kwargs)
            authority_path.write_text('{"incomplete":', encoding="utf-8")
            return item

        runner.work.claim_owned_oldest = corrupt_after_claim  # type: ignore[method-assign]
        result = runner.run("lane-a", "fixture", now=NOW)

        self.assertFalse(adapter.spawned_after_claim)
        self.assertEqual("degrade", result["transition"])
        self.assertEqual("authority_state_unavailable", result["outcome_code"])
        refusal = WorkerRefusals(self.root).records()[-1]
        self.assertEqual("authority_state_unavailable", refusal["reason_code"])
        self.assertEqual(self.item["id"], refusal["work_item_id"])

    def test_postdrive_authority_corruption_records_terminal_plane_honest_degradation(self) -> None:
        self.grant()
        authority_path = self.root.resolve_relative(
            "authority-grants/work-claims.jsonl"
        )
        adapter = _CorruptAuthorityAfterDriveAdapter(self.work, authority_path)

        result = WorkerRunner(self.root, {"fixture": adapter}).run(
            "lane-a", "fixture", now=NOW
        )

        self.assertEqual("degrade", result["transition"])
        self.assertEqual("authority_state_unavailable", result["outcome_code"])
        refusal = WorkerRefusals(self.root).records()[-1]
        self.assertEqual("authority_state_unavailable", refusal["reason_code"])
        self.assertEqual(self.item["id"], refusal["work_item_id"])

    def test_invalid_work_ledger_refuses_as_consumption_state_unavailable(self) -> None:
        self.grant()
        adapter = _CompletingAdapter(self.work)
        self.root.resolve_relative("work/items.jsonl").write_text(
            '{"incomplete":', encoding="utf-8"
        )

        with self.assertRaises(IntegrityFailure) as caught:
            WorkerRunner(self.root, {"fixture": adapter}).run(
                "lane-a", "fixture", now=NOW
            )

        self.assertEqual("consumption_state_unavailable", caught.exception.code)
        self.assertFalse(adapter.spawned_after_claim)
        refusals = WorkerRefusals(self.root).records()
        self.assertEqual("consumption_state_unavailable", refusals[-1]["reason_code"])
        self.assertIsNone(refusals[-1]["work_item_id"])

    def test_worker_consumption_never_synthesizes_delivery_or_ack(self) -> None:
        self.grant()

        WorkerRunner(self.root, {"fixture": _CompletingAdapter(self.work)}).run(
            "lane-a", "fixture", now=NOW
        )

        self.assertFalse(
            self.root.resolve_relative("receipts/deliveries/lane-a.jsonl").exists()
        )
        self.assertFalse(
            self.root.resolve_relative("receipts/acks/lane-a.jsonl").exists()
        )

    def test_hang_and_untyped_exception_become_distinct_typed_degradation(self) -> None:
        for adapter, expected in (
            (_HangingAdapter(), "process_timeout"),
            (_ExplodingAdapter(), "adapter_error"),
            (_ProcessDeathAdapter(), "process_died"),
        ):
            with self.subTest(expected=expected):
                with tempfile.TemporaryDirectory() as temporary:
                    root = FloatiRoot.open_direct_home(Path(temporary) / "fleet", create=True)
                    Registry(root).register("lane-a", "Codex")
                    AuthorityGrantStore(root).claim("work-claims", "lane-a", 60, 60, NOW)
                    WorkLog(root).add("bounded adapter", "lane-a", [], now=NOW)

                    result = WorkerRunner(
                        root, {"fixture": adapter}, call_timeout=0.05
                    ).run("lane-a", "fixture", now=NOW)

                    self.assertEqual("degrade", result["transition"])
                    self.assertEqual(expected, result["outcome_code"])

    def test_runner_passes_a_deadline_clipped_to_ttl_minus_the_fixed_margin(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            evidence = Path(temporary) / "deadlines.txt"
            AuthorityGrantStore(self.root).claim("bounded", "lane-a", 10, 10, NOW)

            result = WorkerRunner(
                self.root,
                {"fixture": _DeadlineAdapter(evidence)},
                call_timeout=30,
            ).run("lane-a", "fixture", now=NOW)

            self.assertEqual("complete", result["transition"])
            entries = evidence.read_text(encoding="utf-8").splitlines()
            self.assertEqual(["spawn", "drive"], [entry.split(":")[0] for entry in entries])
            for entry in entries:
                seconds = float(entry.split(":")[1])
                self.assertGreater(seconds, 8.0)
                self.assertLessEqual(seconds, 9.0)

    def test_runner_reobserves_wall_clock_immediately_before_adapter_launch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            evidence = Path(temporary) / "fresh-deadline.txt"
            AuthorityGrantStore(self.root).claim("bounded", "lane-a", 10, 10, NOW)
            observations = iter((NOW, NOW + timedelta(seconds=2)))
            last = NOW + timedelta(seconds=2)

            def clock() -> datetime:
                return next(observations, last)

            result = WorkerRunner(
                self.root,
                {"fixture": _DeadlineAdapter(evidence)},
                call_timeout=30,
                clock=clock,
            ).run("lane-a", "fixture")

            self.assertEqual("complete", result["transition"])
            spawn_seconds = float(
                evidence.read_text(encoding="utf-8").splitlines()[0].split(":")[1]
            )
            self.assertGreater(spawn_seconds, 6.0)
            self.assertLessEqual(spawn_seconds, 7.0)

    def test_deadline_that_cannot_fit_margin_degrades_without_starting_adapter(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            evidence = Path(temporary) / "not-started.txt"
            AuthorityGrantStore(self.root).claim("bounded", "lane-a", 1, 1, NOW)

            result = WorkerRunner(
                self.root,
                {"fixture": _DeadlineAdapter(evidence)},
            ).run("lane-a", "fixture", now=NOW)

            self.assertEqual("degrade", result["transition"])
            self.assertEqual("authority_deadline_unavailable", result["outcome_code"])
            self.assertFalse(evidence.exists())
            self.assertEqual(["claim", "degrade"], [row["transition"] for row in WorkerReceipts(self.root).records()])

    def test_codex_item_without_workspace_records_refusal_and_terminal_degradation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            evidence = Path(temporary) / "not-started.txt"
            self.grant()

            result = WorkerRunner(
                self.root,
                {"codex": _WorkspaceAdapter(evidence)},
            ).run("lane-a", "codex", now=NOW)

            self.assertEqual("degrade", result["transition"])
            self.assertEqual("workspace_mapping_missing", result["outcome_code"])
            refusal = WorkerRefusals(self.root).records()[-1]
            self.assertEqual("worker_workspace_missing", refusal["reason_code"])
            self.assertEqual(self.item["id"], refusal["work_item_id"])
            self.assertFalse(evidence.exists())

    def test_timeout_terminates_the_adapter_process_group_including_grandchild(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            pid_path = Path(temporary) / "grandchild.pid"
            self.grant()

            result = WorkerRunner(
                self.root,
                {"fixture": _GrandchildHangAdapter(pid_path)},
                call_timeout=0.2,
            ).run("lane-a", "fixture", now=NOW)

            self.assertEqual("process_timeout", result["outcome_code"])
            pid = int(pid_path.read_text(encoding="utf-8"))
            for _ in range(20):
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    break
                time.sleep(0.05)
            else:
                self.fail("adapter grandchild survived process-group cancellation")

    def test_outer_runner_timeout_reaps_the_live_app_server_process(self) -> None:
        from floati.adapters.codex_live import CodexAppServerAdapter

        with tempfile.TemporaryDirectory() as temporary:
            root = FloatiRoot.open_direct_home(Path(temporary) / "fleet", create=True)
            Registry(root).register("lane-a", "Codex")
            current = datetime.now(timezone.utc)
            item = WorkLog(root).add(
                "Create PROOF.txt",
                "lane-a",
                [],
                provision_workspace=True,
                now=current,
            )
            workspace = Path(str(item["workspace"]))
            self.addCleanup(shutil.rmtree, workspace, True)
            AuthorityGrantStore(root).claim("work-claims", "lane-a", 10, 10, current)
            harness = (
                Path(__file__).parent
                / "fixtures"
                / "codex-app-server"
                / "reference_harness.py"
            )
            adapter = CodexAppServerAdapter(
                (str(Path(sys.executable).resolve()), str(harness), "--mode", "hang")
            )

            result = WorkerRunner(
                root, {"codex": adapter}, call_timeout=0.3
            ).run("lane-a", "codex", now=current)

            self.assertEqual("process_timeout", result["outcome_code"])
            pid_path = workspace / ".floati" / "harness.pid"
            self.assertTrue(pid_path.is_file(), "Floati evidence must include the harness pid")
            self.assertFalse(os.path.lexists(workspace / ".slipway"))
            pid = int(pid_path.read_text(encoding="utf-8"))
            try:
                for _ in range(20):
                    try:
                        os.kill(pid, 0)
                    except ProcessLookupError:
                        break
                    time.sleep(0.05)
                else:
                    self.fail("live app-server survived outer runner timeout")
            finally:
                try:
                    os.kill(pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass

    def test_adapter_crash_after_spawn_reaps_the_live_app_server_process(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = FloatiRoot.open_direct_home(Path(temporary) / "fleet", create=True)
            Registry(root).register("lane-a", "Codex")
            current = datetime.now(timezone.utc)
            item = WorkLog(root).add(
                "Create PROOF.txt",
                "lane-a",
                [],
                provision_workspace=True,
                now=current,
            )
            workspace = Path(str(item["workspace"]))
            self.addCleanup(shutil.rmtree, workspace, True)
            AuthorityGrantStore(root).claim("work-claims", "lane-a", 10, 10, current)
            harness = (
                Path(__file__).parent
                / "fixtures"
                / "codex-app-server"
                / "reference_harness.py"
            )
            adapter = _CrashAfterCodexSpawnAdapter(
                (str(Path(sys.executable).resolve()), str(harness), "--mode", "hang")
            )

            result = WorkerRunner(root, {"codex": adapter}, call_timeout=1).run(
                "lane-a", "codex", now=current
            )

            self.assertEqual("process_died", result["outcome_code"])
            evidence = workspace / ".floati"
            pid_path = evidence / "harness.pid"
            adapter_pgid_path = evidence / "adapter.pgid"
            harness_pgid_path = evidence / "harness.pgid"
            for path in (pid_path, adapter_pgid_path, harness_pgid_path):
                self.assertTrue(path.is_file(), f"Floati evidence is missing: {path}")
            self.assertFalse(os.path.lexists(workspace / ".slipway"))
            pid = int(pid_path.read_text(encoding="utf-8"))
            adapter_pgid = int(adapter_pgid_path.read_text(encoding="utf-8"))
            harness_pgid = int(harness_pgid_path.read_text(encoding="utf-8"))
            self.assertNotEqual(adapter_pgid, harness_pgid)
            for _ in range(20):
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    break
                time.sleep(0.05)
            else:
                try:
                    os.kill(pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                self.fail("live app-server survived abrupt adapter death")


class WorkerEffectPipeTests(unittest.TestCase):
    def setUp(self) -> None:
        # Kernel enforcement has its own real-backend bank. These tests exercise
        # the Worker fork/pipe/lifecycle contract on hosts where nesting another
        # kernel sandbox may be unavailable.
        patcher = mock.patch(
            "floati.workers.apply_worker_isolation", return_value="macos-sandbox",
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    @staticmethod
    def _marked_source(source: str, proof: Path, label: str) -> str:
        marker = (
            "from pathlib import Path as _PreludeProofPath\n"
            f"_PreludeProofPath({str(proof)!r}).open('a', encoding='utf-8').write({label!r} + '\\n')\n"
        )
        future = "from __future__ import annotations\n"
        if future in source:
            return source.replace(future, future + marker, 1)
        return marker + source

    def _prelude_package(self, root: Path) -> Path:
        package = root / "floati"
        package.mkdir(parents=True)
        source_package = Path(__file__).parents[1] / "floati"
        for name in (
            "worker_errors.py",
            "worker_isolation.py",
            "worker_bootstrap_protocol.py",
            "worker_bootstrap.py",
        ):
            shutil.copyfile(source_package / name, package / name)
        return package / "worker_bootstrap.py"

    def _exec_launch_payload(self, root: Path) -> tuple[dict[str, object], object]:
        tenant = root / "tenant"
        (tenant / "effects").mkdir(parents=True)
        session_id = "worker-018f7e9b3c117abc8def0123456789ab"
        policy = prepare_worker_isolation(tenant, root / "workspace", session_id)
        self.addCleanup(cleanup_worker_isolation, policy)
        return {
            "schema_version": 1,
            "session_id": session_id,
            "adapter": {"kind": "codex", "command": ["/bin/echo"]},
            "item": {"id": "work-a"},
            "deadline_millis": 2_000,
            "spawn_context": None,
            "effect_context": None,
            "isolation_policy": isolation_policy_to_payload(policy),
        }, policy

    def test_effect_exec_refuses_foreign_process_group_without_signaling_it(self) -> None:
        """Catches an exec child naming an unrelated same-UID process group."""
        from floati.worker_exec import SpawnedWorkerProcess

        foreign = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            start_new_session=True,
        )
        self.addCleanup(foreign.wait, 2.0)

        def stop_foreign() -> None:
            if foreign.poll() is None:
                try:
                    os.killpg(foreign.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass

        self.addCleanup(stop_foreign)
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            try:
                if os.getpgid(foreign.pid) == foreign.pid:
                    break
            except ProcessLookupError:
                self.fail("foreign positive-control process exited before the test")
            time.sleep(0.01)
        self.assertEqual(foreign.pid, os.getpgid(foreign.pid))

        parent_socket, child_socket = socket.socketpair(
            socket.AF_UNIX, socket.SOCK_STREAM,
        )
        worker_pid = os.fork()
        if worker_pid == 0:
            try:
                parent_socket.close()
                os.setsid()
                channel = BootstrapChannel(child_socket.detach())
                channel.send(("isolation_ready", {"backend": "macos-sandbox"}))
                channel.send(("process_group", foreign.pid))
                time.sleep(30)
            finally:
                os._exit(0)
        child_socket.close()
        process = SpawnedWorkerProcess(worker_pid)
        channel = BootstrapChannel(parent_socket.detach())
        self.addCleanup(channel.close)

        case = _EffectWorkerCase(self, _EffectReportingAdapter(()))
        runner = case.runner(instrument_exec=False)
        runner.call_timeout = 0.4
        with mock.patch(
            "floati.workers.spawn_effect_worker", return_value=(process, channel),
        ):
            result = runner.run(
                "node-a", "codex", now=case.run.now(8),
                run_id=case.run.run_id, item_id=case.run.parent,
                attempt_id=case.run.opened["attempt_id"],
            )

        self.assertEqual("degrade", result["transition"])
        self.assertEqual("adapter_error", result["outcome_code"])
        self.assertIsNone(foreign.poll(), "parent signaled the unrelated process group")
        self.assertEqual([], runner.last_process_audit["registered_process_groups"])

    def test_exec_uses_opened_prelude_sources_after_all_package_paths_are_replaced(
        self,
    ) -> None:
        """Catches dependency path replacement selecting unbound prelude bytes."""
        from floati.worker_exec import spawn_effect_worker

        with tempfile.TemporaryDirectory(dir="/private/tmp") as temporary:
            root = Path(temporary)
            bootstrap = self._prelude_package(root / "trusted")
            package = bootstrap.parent
            proof = root / "replacement-proof"
            replacements = root / "replacements"
            replacements.mkdir()
            for name in ("worker_isolation.py", "worker_bootstrap_protocol.py"):
                source = (package / name).read_text(encoding="utf-8")
                (replacements / name).write_text(
                    self._marked_source(source, proof, name), encoding="utf-8",
                )
            payload, _policy = self._exec_launch_payload(root / "policy")
            real_spawn = os.posix_spawn
            launched_prelude_descriptors: list[int] = []

            def replace_then_spawn(
                executable: str,
                argv: list[str],
                environment: dict[str, str],
                **kwargs: object,
            ) -> int:
                for name in ("worker_isolation.py", "worker_bootstrap_protocol.py"):
                    os.replace(replacements / name, package / name)
                for action in kwargs["file_actions"]:  # type: ignore[index]
                    if action[0] == os.POSIX_SPAWN_DUP2 and action[2] in {4, 5, 6, 7}:
                        launched_prelude_descriptors.append(action[1])
                return real_spawn(executable, argv, environment, **kwargs)

            with mock.patch(
                "floati.worker_exec.os.posix_spawn", side_effect=replace_then_spawn,
            ):
                process, channel = spawn_effect_worker(bootstrap.resolve(), payload)
            self.addCleanup(channel.close)
            self.assertTrue(channel.poll(10.0))
            first_frame = channel.recv()
            self.assertIn(
                first_frame[0], {"failure", "isolation_ready"}, first_frame,
            )
            if first_frame[0] == "failure":
                self.assertEqual(
                    ("failure", "effect_worker_isolation_unavailable"), first_frame,
                )
            else:
                self.assertEqual(
                    first_frame[1]["backend"],
                    validate_isolation_backend(first_frame[1]["backend"]),
                )
                process.terminate()
            process.join(3.0)

            self.assertFalse(proof.exists())
            self.assertIsNotNone(process.exitcode)
            self.assertEqual(4, len(launched_prelude_descriptors))
            for descriptor in launched_prelude_descriptors:
                with self.assertRaises(OSError) as caught:
                    os.fstat(descriptor)
                self.assertEqual(errno.EBADF, caught.exception.errno)

    def test_exec_refuses_mutated_open_prelude_before_any_project_module_runs(
        self,
    ) -> None:
        """Catches a held source inode changing after its parent digest is frozen."""
        from floati.worker_exec import spawn_effect_worker

        with tempfile.TemporaryDirectory(dir="/private/tmp") as temporary:
            root = Path(temporary)
            bootstrap = self._prelude_package(root / "trusted")
            package = bootstrap.parent
            proof = root / "project-module-proof"
            for name in (
                "worker_errors.py",
                "worker_isolation.py",
                "worker_bootstrap_protocol.py",
                "worker_bootstrap.py",
            ):
                path = package / name
                path.write_text(
                    self._marked_source(path.read_text(encoding="utf-8"), proof, name),
                    encoding="utf-8",
                )
            payload, _policy = self._exec_launch_payload(root / "policy")
            target = package / "worker_isolation.py"
            mutated = self._marked_source(
                (Path(__file__).parents[1] / "floati" / "worker_isolation.py").read_text(
                    encoding="utf-8"
                ),
                proof,
                "mutated-worker-isolation.py",
            ).encode("utf-8")
            real_spawn = os.posix_spawn

            def mutate_then_spawn(
                executable: str,
                argv: list[str],
                environment: dict[str, str],
                **kwargs: object,
            ) -> int:
                descriptor = os.open(target, os.O_WRONLY | os.O_TRUNC)
                try:
                    os.write(descriptor, mutated)
                finally:
                    os.close(descriptor)
                return real_spawn(executable, argv, environment, **kwargs)

            with mock.patch(
                "floati.worker_exec.os.posix_spawn", side_effect=mutate_then_spawn,
            ):
                process, channel = spawn_effect_worker(bootstrap.resolve(), payload)
            channel.close()
            process.join(3.0)

            self.assertEqual(126, process.exitcode)
            self.assertFalse(proof.exists())

    def test_exec_loader_does_not_consult_sys_path_or_meta_path_for_prelude_modules(
        self,
    ) -> None:
        """Catches path finders resolving any pre-isolation project dependency."""
        from floati.worker_exec import spawn_effect_worker

        with tempfile.TemporaryDirectory(dir="/private/tmp") as temporary:
            root = Path(temporary)
            bootstrap = self._prelude_package(root / "trusted")
            package = bootstrap.parent
            target_proof = root / "target-finder-proof"
            control_proof = root / "control-finder-proof"
            replacement = root / "replacement"
            replacement.mkdir()
            for name in ("worker_errors.py", "worker_isolation.py", "worker_bootstrap_protocol.py"):
                shutil.copyfile(package / name, replacement / name)
            positive = replacement / "prelude_positive_control.py"
            positive.write_text("VALUE = 1\n", encoding="utf-8")
            prefix = (
                "import importlib.machinery as _prelude_machinery, sys as _prelude_sys\n"
                "class _PreludeFinder:\n"
                " def find_spec(self, fullname, path=None, target=None):\n"
                "  mapping = {\n"
                + "".join(
                    f"   {('floati.' + name[:-3])!r}: {str(replacement / name)!r},\n"
                    for name in ("worker_errors.py", "worker_isolation.py", "worker_bootstrap_protocol.py")
                )
                + "  }\n"
                f"  if fullname in mapping:\n   open({str(target_proof)!r}, 'a').write(fullname + '\\n')\n"
                "  if fullname == 'prelude_positive_control':\n"
                f"   open({str(control_proof)!r}, 'a').write(fullname + '\\n')\n"
                f"   return _prelude_machinery.ModuleSpec(fullname, _prelude_machinery.SourceFileLoader(fullname, {str(positive)!r}))\n"
                "  if fullname in mapping:\n"
                "   return _prelude_machinery.ModuleSpec(fullname, _prelude_machinery.SourceFileLoader(fullname, mapping[fullname]))\n"
                "  return None\n"
                "_prelude_sys.meta_path.insert(0, _PreludeFinder())\n"
                "import prelude_positive_control\n"
            )
            source = bootstrap.read_text(encoding="utf-8")
            future = "from __future__ import annotations\n"
            bootstrap.write_text(source.replace(future, future + prefix, 1), encoding="utf-8")
            payload, _policy = self._exec_launch_payload(root / "policy")

            process, channel = spawn_effect_worker(bootstrap.resolve(), payload)
            self.addCleanup(channel.close)
            self.assertTrue(channel.poll(10.0))
            first_frame = channel.recv()
            self.assertIn(
                first_frame[0], {"failure", "isolation_ready"}, first_frame,
            )
            if first_frame[0] == "failure":
                self.assertEqual(
                    ("failure", "effect_worker_isolation_unavailable"), first_frame,
                )
            else:
                self.assertEqual(
                    first_frame[1]["backend"],
                    validate_isolation_backend(first_frame[1]["backend"]),
                )
                process.terminate()
            process.join(3.0)

            self.assertTrue(control_proof.is_file())
            self.assertFalse(target_proof.exists())

    def test_exec_prelude_mapping_names_order_descriptors_and_digests_are_closed(
        self,
    ) -> None:
        """Catches caller-controlled or reordered source bindings."""
        import floati.worker_exec as worker_exec

        expected = (
            ("floati.worker_errors", "worker_errors.py", 4),
            ("floati.worker_isolation", "worker_isolation.py", 5),
            ("floati.worker_bootstrap_protocol", "worker_bootstrap_protocol.py", 6),
            ("__main__", "worker_bootstrap.py", 7),
        )
        self.assertTrue(hasattr(worker_exec, "_PRELUDE_SOURCES"))
        self.assertEqual(expected, worker_exec._PRELUDE_SOURCES)
        with tempfile.TemporaryDirectory(dir="/private/tmp") as temporary:
            bootstrap = self._prelude_package(Path(temporary))
            records = worker_exec._open_validated_prelude(bootstrap.resolve())
            try:
                self.assertEqual(expected, tuple(
                    (record.module_name, record.basename, record.target_descriptor)
                    for record in records
                ))
                for record in records:
                    source = (bootstrap.parent / record.basename).read_bytes()
                    self.assertEqual(hashlib.sha256(source).hexdigest(), record.digest)
                    self.assertEqual(
                        (bootstrap.parent / record.basename).stat().st_ino,
                        record.inode,
                    )
            finally:
                for record in records:
                    os.close(record.descriptor)

    def test_exec_relocates_colliding_socket_and_source_descriptors_before_file_actions(
        self,
    ) -> None:
        """Catches fd 3-7 allocation cycles corrupting later spawn actions."""
        import floati.worker_exec as worker_exec

        self.assertTrue(hasattr(worker_exec, "_relocate_launch_descriptors"))
        descriptors = [3, 7, 4, 6, 5]
        with mock.patch(
            "floati.worker_exec.fcntl.fcntl", side_effect=[8, 9, 10, 11, 12],
        ) as duplicate, mock.patch("floati.worker_exec.os.close") as close:
            relocated = worker_exec._relocate_launch_descriptors(descriptors)
        self.assertEqual([8, 9, 10, 11, 12], relocated)
        self.assertEqual(
            [mock.call(descriptor, fcntl.F_DUPFD_CLOEXEC, 8) for descriptor in descriptors],
            duplicate.call_args_list,
        )
        self.assertEqual([mock.call(descriptor) for descriptor in descriptors], close.call_args_list)

    def test_exec_closes_every_parent_prelude_descriptor_after_start_or_failure(
        self,
    ) -> None:
        """Catches any held prelude source surviving either parent disposition."""
        from floati.worker_exec import spawn_effect_worker

        with tempfile.TemporaryDirectory(dir="/private/tmp") as temporary:
            bootstrap = self._prelude_package(Path(temporary))
            captured: list[int] = []

            def refuse(
                executable: str,
                argv: list[str],
                environment: dict[str, str],
                **kwargs: object,
            ) -> int:
                del executable, argv, environment
                for action in kwargs["file_actions"]:  # type: ignore[index]
                    if action[0] == os.POSIX_SPAWN_DUP2 and action[2] in {4, 5, 6, 7}:
                        captured.append(action[1])
                raise OSError("synthetic spawn refusal")

            with mock.patch("floati.worker_exec.os.posix_spawn", side_effect=refuse):
                with self.assertRaises(OSError):
                    spawn_effect_worker(bootstrap.resolve(), {})
            self.assertEqual(4, len(captured))
            for descriptor in captured:
                with self.assertRaises(OSError) as caught:
                    os.fstat(descriptor)
                self.assertEqual(errno.EBADF, caught.exception.errno)

    def test_effect_exec_clean_socket_eof_preserves_process_death_and_drain_semantics(
        self,
    ) -> None:
        """Catches canonical socket EOF collapsing into an isolation refusal."""

        class ExitedProcess:
            pid = 12345

            @staticmethod
            def is_alive() -> bool:
                return False

            @staticmethod
            def join(timeout: float) -> None:
                del timeout

        for phase in ("ready", "finish"):
            with self.subTest(phase=phase):
                parent_socket, child_socket = socket.socketpair()
                channel = BootstrapChannel(parent_socket.detach())
                self.addCleanup(channel.close)
                child_socket.close()
                deadline = time.monotonic() + 0.5
                if phase == "ready":
                    with self.assertRaises(WorkerAdapterFailure) as caught:
                        WorkerRunner._receive_isolation_ready(
                            channel, ExitedProcess(), deadline,
                        )
                    self.assertEqual("process_died", caught.exception.code)
                else:
                    WorkerRunner._finish_effect_process(
                        channel, ExitedProcess(), deadline,
                    )

    def test_effect_exec_partial_socket_frame_never_counts_as_clean_final_drain(
        self,
    ) -> None:
        """Catches a truncated final frame being accepted as ordinary EOF."""

        class ExitedProcess:
            pid = 12345

            @staticmethod
            def is_alive() -> bool:
                return False

            @staticmethod
            def join(timeout: float) -> None:
                del timeout

        parent_socket, child_socket = socket.socketpair()
        channel = BootstrapChannel(parent_socket.detach())
        self.addCleanup(channel.close)
        child_socket.sendall(b"\x00\x00")
        child_socket.close()
        with self.assertRaises(WorkerAdapterFailure) as caught:
            WorkerRunner._finish_effect_process(
                channel, ExitedProcess(), time.monotonic() + 0.5,
            )
        self.assertEqual("adapter_error", caught.exception.code)

    def test_effect_exec_partial_socket_frame_cannot_outlive_worker_deadline(
        self,
    ) -> None:
        """Catches one readable byte opening an unbounded Worker frame read."""

        class LiveProcess:
            pid = 12345

            @staticmethod
            def is_alive() -> bool:
                return True

        fragments = (
            ("header", b"\x00\x00"),
            ("body", struct.pack(">I", 24) + b'{"payload":'),
        )
        runner = WorkerRunner.__new__(WorkerRunner)
        for phase, fragment in fragments:
            with self.subTest(phase=phase):
                parent_socket, child_socket = socket.socketpair()
                channel = BootstrapChannel(parent_socket.detach())
                child_socket.sendall(fragment)
                result: list[str] = []
                finished = threading.Event()

                def receive() -> None:
                    try:
                        runner._receive(
                            channel,
                            LiveProcess(),
                            time.monotonic() + 0.05,
                            set(),
                        )
                    except WorkerAdapterFailure as exc:
                        result.append(exc.code)
                    finally:
                        finished.set()

                thread = threading.Thread(target=receive)
                thread.start()
                try:
                    self.assertTrue(
                        finished.wait(0.25),
                        "partial bootstrap frame outlived the Worker deadline",
                    )
                    self.assertEqual(["process_timeout"], result)
                finally:
                    child_socket.close()
                    thread.join(1.0)
                    channel.close()

    def test_effect_exec_rejects_stateful_spec_mapping_and_duplicate_kind_binding(
        self,
    ) -> None:
        """Catches caller-controlled mappings or aliases becoming config authority."""
        executable = os.path.realpath(sys.executable)

        class StatefulDict(dict):
            def items(self):
                raise AssertionError("stateful spec mapping was inspected")

            def __iter__(self):
                raise AssertionError("stateful spec mapping was iterated")

        stateful_directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, stateful_directory)
        with self.assertRaises(WorkerAdapterFailure) as stateful:
            WorkerRunner(
                FloatiRoot.open_direct_home(
                    Path(stateful_directory) / "tenant", create=True,
                ),
                {},
                effect_adapter_specs=StatefulDict(),
            )
        self.assertEqual("effect_worker_isolation_unavailable", stateful.exception.code)

        root_directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root_directory)
        root = FloatiRoot.open_direct_home(Path(root_directory) / "tenant", create=True)
        with self.assertRaises(WorkerAdapterFailure) as duplicate:
            WorkerRunner(
                root,
                {},
                effect_adapter_specs={
                    "codex": BuiltInAdapterSpec("codex", (executable,)),
                    "alias": BuiltInAdapterSpec("codex", (executable,)),
                },
            )
        self.assertEqual("effect_worker_isolation_unavailable", duplicate.exception.code)

    def test_effect_exec_rejects_missing_custom_or_mismatched_builtin_spec_before_prepare(
        self,
    ) -> None:
        """Catches an injected object or mismatched kind reaching preparation."""
        executable = os.path.realpath(sys.executable)
        for specs in ({}, {"codex": BuiltInAdapterSpec("pi", (executable,))}):
            with self.subTest(specs=specs):
                case = _EffectWorkerCase(self, object())
                runner = WorkerRunner(
                    case.root,
                    {"codex": case.adapter},
                    clock=lambda: case.run.now(8),
                    effect_controller=case.effect_controller,
                    effect_adapter_specs=specs,
                )
                with mock.patch(
                    "floati.workers.prepare_worker_isolation",
                    side_effect=AssertionError("invalid spec reached preparation"),
                ):
                    result = runner.run(
                        "node-a", "codex", now=case.run.now(8),
                        run_id=case.run.run_id, item_id=case.run.parent,
                        attempt_id=case.run.opened["attempt_id"],
                    )
                self.assertEqual("degrade", result["transition"])
                self.assertEqual(
                    "effect_worker_isolation_unavailable", result["outcome_code"],
                )

    def test_effect_exec_never_serializes_adapter_callable_module_class_or_object(
        self,
    ) -> None:
        """Catches the canonical launch document widening into Python authority."""
        case = _EffectWorkerCase(self, object())
        captured: list[dict[str, object]] = []

        def refuse_after_capture(
            bootstrap_path: Path, launch_payload: dict[str, object],
        ) -> tuple[object, BootstrapChannel]:
            self.assertTrue(bootstrap_path.is_absolute())
            captured.append(launch_payload)
            raise OSError("synthetic posix_spawn refusal")

        runner = WorkerRunner(
            case.root,
            {"codex": case.adapter},
            clock=lambda: case.run.now(8),
            effect_controller=case.effect_controller,
            effect_adapter_specs={
                "codex": BuiltInAdapterSpec(
                    "codex", (os.path.realpath(sys.executable),),
                ),
            },
        )
        with mock.patch(
            "floati.workers.spawn_effect_worker", side_effect=refuse_after_capture,
        ):
            result = runner.run(
                "node-a", "codex", now=case.run.now(8),
                run_id=case.run.run_id, item_id=case.run.parent,
                attempt_id=case.run.opened["attempt_id"],
            )

        self.assertEqual("process_start_failed", result["outcome_code"])
        self.assertEqual(1, len(captured))
        self.assertEqual(
            {
                "schema_version", "session_id", "adapter", "item",
                "deadline_millis", "spawn_context", "effect_context",
                "isolation_policy",
            },
            set(captured[0]),
        )
        json.dumps(captured[0], allow_nan=False)

        def assert_closed(value: object) -> None:
            self.assertIsNot(value, case.adapter)
            self.assertIsNot(value, case.adapter.__class__)
            self.assertFalse(callable(value))
            self.assertNotIsInstance(value, types.ModuleType)
            if type(value) is dict:
                for key, item in value.items():
                    self.assertIs(type(key), str)
                    assert_closed(item)
            elif type(value) is list:
                for item in value:
                    assert_closed(item)
            else:
                self.assertIn(type(value), {str, int, bool, type(None)})

        assert_closed(captured[0])

    def test_exec_bootstrap_path_must_be_absolute_owned_regular_and_nonsymlink(
        self,
    ) -> None:
        """Catches tenant, relative, symlink, or non-file bootstrap selection."""
        from floati.worker_exec import spawn_effect_worker

        with tempfile.TemporaryDirectory(dir="/private/tmp") as temporary:
            root = Path(temporary)
            regular = self._prelude_package(root / "regular")
            symbolic = root / "bootstrap-link.py"
            symbolic.symlink_to(regular)
            invalid = (Path("worker_bootstrap.py"), root, symbolic)
            for candidate in invalid:
                with self.subTest(candidate=candidate):
                    with mock.patch("floati.worker_exec.os.posix_spawn") as spawn:
                        with self.assertRaises(WorkerAdapterFailure):
                            spawn_effect_worker(candidate, {})
                        spawn.assert_not_called()
            with mock.patch(
                "floati.worker_exec.os.posix_spawn", side_effect=OSError("stop"),
            ) as spawn:
                with self.assertRaises(OSError):
                    spawn_effect_worker(regular.resolve(), {})
                spawn.assert_called_once()

    def test_effect_exec_strips_native_loader_environment_but_keeps_provider_values(
        self,
    ) -> None:
        """Catches native loader configuration reaching pre-isolation startup."""
        from floati.worker_exec import spawn_effect_worker

        with tempfile.TemporaryDirectory(dir="/private/tmp") as temporary:
            bootstrap = self._prelude_package(Path(temporary))
            hostile = {
                "DYLD_INSERT_LIBRARIES": "/hostile/dyld.dylib",
                "DYLD_LIBRARY_PATH": "/hostile/dyld",
                "LD_PRELOAD": "/hostile/preload.so",
                "LD_LIBRARY_PATH": "/hostile/ld",
                "LIBPATH": "/hostile/aix",
                "SHLIB_PATH": "/hostile/hpux",
            }
            ordinary = {
                "OPENAI_API_KEY": "provider-secret",
                "CODEX_HOME": "/provider/runtime",
                "SLIPWAY_PROVIDER_MODE": "ordinary",
            }
            environment = dict(hostile)
            environment.update(ordinary)
            with mock.patch.dict(os.environ, environment, clear=True):
                with mock.patch(
                    "floati.worker_exec.os.posix_spawn", side_effect=OSError("stop"),
                ) as spawn:
                    with self.assertRaises(OSError):
                        spawn_effect_worker(bootstrap.resolve(), {})

            child_environment = spawn.call_args.args[2]
            for name in hostile:
                self.assertNotIn(name, child_environment)
            for name, value in ordinary.items():
                self.assertEqual(value, child_environment[name])

    def test_effect_exec_runs_opened_verified_bootstrap_bytes_after_path_replacement(
        self,
    ) -> None:
        """Catches bootstrap pathname replacement changing the executed program."""
        from floati.worker_exec import spawn_effect_worker

        with tempfile.TemporaryDirectory(dir="/private/tmp") as temporary:
            root = Path(temporary)
            marker = root / "marker"
            bootstrap = self._prelude_package(root / "trusted")
            replacement = root / "replacement.py"
            bootstrap.write_text(
                "from pathlib import Path\n"
                f"Path({str(marker)!r}).write_text('opened', encoding='utf-8')\n",
                encoding="utf-8",
            )
            replacement.write_text(
                "from pathlib import Path\n"
                f"Path({str(marker)!r}).write_text('replacement', encoding='utf-8')\n",
                encoding="utf-8",
            )
            real_spawn = os.posix_spawn
            captured_argv: list[str] = []

            def replace_then_spawn(
                executable: str,
                argv: list[str],
                environment: dict[str, str],
                **kwargs: object,
            ) -> int:
                captured_argv.extend(argv)
                os.replace(replacement, bootstrap)
                return real_spawn(executable, argv, environment, **kwargs)

            with mock.patch(
                "floati.worker_exec.os.posix_spawn", side_effect=replace_then_spawn,
            ):
                process, channel = spawn_effect_worker(bootstrap.resolve(), {})
            channel.close()
            process.join(3.0)

            self.assertEqual(0, process.exitcode)
            self.assertEqual("opened", marker.read_text(encoding="utf-8"))
            self.assertEqual(["-I", "-S", "-B", "-c"], captured_argv[1:5])
            self.assertNotEqual(str(bootstrap), captured_argv[5])

    def test_effect_exec_digest_rejects_open_file_mutation_after_hash(self) -> None:
        """Catches execution when opened bootstrap bytes change after hashing."""
        from floati.worker_exec import spawn_effect_worker

        with tempfile.TemporaryDirectory(dir="/private/tmp") as temporary:
            root = Path(temporary)
            marker = root / "marker"
            bootstrap = self._prelude_package(root / "trusted")
            bootstrap.write_text("raise SystemExit(0)\n", encoding="utf-8")
            replacement_source = (
                "from pathlib import Path\n"
                f"Path({str(marker)!r}).write_text('mutated', encoding='utf-8')\n"
            )
            real_spawn = os.posix_spawn

            def mutate_then_spawn(
                executable: str,
                argv: list[str],
                environment: dict[str, str],
                **kwargs: object,
            ) -> int:
                bootstrap.write_text(replacement_source, encoding="utf-8")
                return real_spawn(executable, argv, environment, **kwargs)

            with mock.patch(
                "floati.worker_exec.os.posix_spawn", side_effect=mutate_then_spawn,
            ):
                process, channel = spawn_effect_worker(bootstrap.resolve(), {})
            channel.close()
            process.join(3.0)

            self.assertEqual(126, process.exitcode)
            self.assertFalse(marker.exists())

    def test_posix_spawn_start_failure_cleans_probe_scratch_and_unused_empty_workspace(
        self,
    ) -> None:
        """Catches prepared empty launch paths leaking after exec start refusal."""
        from floati.worker_isolation import prepare_worker_isolation as real_prepare

        case = _EffectWorkerCase(self, object())
        policies = []

        def capture_prepare(*args: object, **kwargs: object) -> object:
            policy = real_prepare(*args, **kwargs)
            policies.append(policy)
            return policy

        runner = WorkerRunner(
            case.root,
            {"codex": case.adapter},
            clock=lambda: case.run.now(8),
            effect_controller=case.effect_controller,
            effect_adapter_specs={
                "codex": BuiltInAdapterSpec(
                    "codex", (os.path.realpath(sys.executable),),
                ),
            },
        )
        with (
            mock.patch(
                "floati.workers.prepare_worker_isolation",
                side_effect=capture_prepare,
            ),
            mock.patch(
                "floati.workers.spawn_effect_worker",
                side_effect=OSError("synthetic posix_spawn refusal"),
            ),
        ):
            result = runner.run(
                "node-a", "codex", now=case.run.now(8),
                run_id=case.run.run_id, item_id=case.run.parent,
                attempt_id=case.run.opened["attempt_id"],
            )

        self.assertEqual("process_start_failed", result["outcome_code"])
        self.assertEqual(1, len(policies))
        policy = policies[0]
        self.assertFalse(policy.write_probe.exists())
        self.assertFalse(policy.scratch.exists())
        self.assertIsNotNone(policy.workspace)
        self.assertFalse(policy.workspace.exists())

    def test_posix_spawn_cleanup_never_deletes_replacement_or_artifact_workspace(
        self,
    ) -> None:
        """Catches start cleanup deleting an identity replacement or output."""
        from floati.worker_isolation import prepare_worker_isolation as real_prepare

        for mutation in ("replacement", "artifact"):
            with self.subTest(mutation=mutation):
                case = _EffectWorkerCase(self, object())
                policies = []

                def capture_prepare(*args: object, **kwargs: object) -> object:
                    policy = real_prepare(*args, **kwargs)
                    policies.append(policy)
                    return policy

                def refuse_after_mutation(*args: object, **kwargs: object) -> object:
                    del args, kwargs
                    policy = policies[0]
                    assert policy.workspace is not None
                    if mutation == "replacement":
                        policy.workspace.rmdir()
                        policy.workspace.mkdir(mode=0o700)
                    else:
                        (policy.workspace / "artifact.txt").write_text(
                            "preserve", encoding="utf-8",
                        )
                    raise OSError("synthetic posix_spawn refusal")

                runner = WorkerRunner(
                    case.root,
                    {"codex": case.adapter},
                    clock=lambda: case.run.now(8),
                    effect_controller=case.effect_controller,
                    effect_adapter_specs={
                        "codex": BuiltInAdapterSpec(
                            "codex", (os.path.realpath(sys.executable),),
                        ),
                    },
                )
                with (
                    mock.patch(
                        "floati.workers.prepare_worker_isolation",
                        side_effect=capture_prepare,
                    ),
                    mock.patch(
                        "floati.workers.spawn_effect_worker",
                        side_effect=refuse_after_mutation,
                    ),
                ):
                    result = runner.run(
                        "node-a", "codex", now=case.run.now(8),
                        run_id=case.run.run_id, item_id=case.run.parent,
                        attempt_id=case.run.opened["attempt_id"],
                    )

                self.assertEqual("adapter_error", result["outcome_code"])
                policy = policies[0]
                assert policy.workspace is not None
                self.assertTrue(policy.workspace.is_dir())
                if mutation == "artifact":
                    self.assertEqual(
                        "preserve",
                        (policy.workspace / "artifact.txt").read_text(encoding="utf-8"),
                    )

    def test_unaccepted_child_backend_refuses_before_callbacks_or_frames(self) -> None:
        """Catches a forged isolation backend reaching any adapter callback."""
        context = multiprocessing.get_context("fork")

        for backend in (
            "bogus-backend",
            "linux-landlock-v²",
            "linux-landlock-v" + ("9" * 5000),
        ):
            with self.subTest(backend=backend[:40]):
                callback_count = context.Value("i", 0)

                class CallbackCountingAdapter(_EffectReportingAdapter):
                    def _count(self) -> None:
                        with callback_count.get_lock():
                            callback_count.value += 1

                    def set_process_group_registrar(self, registrar: object) -> None:
                        self._count()

                    def set_effect_context(
                        self, effect_context: dict[str, object], emit: object,
                    ) -> None:
                        self._count()
                        emit(_EffectWorkerCase.intent_event())  # type: ignore[operator]

                    def spawn(
                        self, item: dict, *, deadline_seconds: float,
                    ) -> object:
                        self._count()
                        return object()

                    def drive(
                        self,
                        handle: object,
                        item: dict,
                        *,
                        deadline_seconds: float,
                    ) -> list[dict[str, str]]:
                        self._count()
                        return []

                parent, child = context.Pipe()
                process = context.Process(
                    target=_adapter_process,
                    args=(
                        child,
                        CallbackCountingAdapter(()),
                        {"id": "work-a"},
                        2.0,
                        None,
                        {},
                        object(),
                    ),
                )
                with mock.patch(
                    "floati.workers.apply_worker_isolation", return_value=backend,
                ):
                    process.start()
                child.close()
                messages = []
                deadline = time.monotonic() + 2.0
                try:
                    while time.monotonic() < deadline:
                        if parent.poll(0.05):
                            try:
                                message = parent.recv()
                            except EOFError:
                                break
                            messages.append(message)
                            if message[0] == "result":
                                parent.send(("effect_reporting_closed", None))
                        elif not process.is_alive():
                            break
                    process.join(0.5)
                finally:
                    if process.is_alive():
                        process.terminate()
                        process.join(2)
                    parent.close()

                self.assertEqual(0, callback_count.value)
                self.assertEqual(
                    [("failure", "effect_worker_isolation_unavailable")],
                    messages,
                )
                self.assertEqual(0, process.exitcode)

    def test_parent_rejects_noncanonical_landlock_backend_as_typed_refusal(self) -> None:
        """Catches Unicode, ambiguous, and unbounded ABI suffixes."""
        context = multiprocessing.get_context("fork")
        malformed = (
            "linux-landlock-v²",
            "linux-landlock-v",
            "linux-landlock-v+3",
            "linux-landlock-v-3",
            "linux-landlock-v2",
            "linux-landlock-v03",
            "linux-landlock-v" + ("9" * 5000),
        )

        for backend in malformed:
            with self.subTest(backend=backend[:40]):
                parent, child = context.Pipe()
                process = context.Process(
                    target=child.send,
                    args=(("isolation_ready", {"backend": backend}),),
                )
                process.start()
                child.close()
                try:
                    with self.assertRaises(WorkerAdapterFailure) as caught:
                        WorkerRunner._receive_isolation_ready(
                            parent, process, time.monotonic() + 2.0,
                        )
                    self.assertEqual(
                        "effect_worker_isolation_unavailable", caught.exception.code,
                    )
                    process.join(2)
                finally:
                    if process.is_alive():
                        process.terminate()
                        process.join(2)
                    parent.close()
                self.assertEqual(0, process.exitcode)

        for backend in ("linux-landlock-v3", "linux-landlock-v999"):
            with self.subTest(backend=backend):
                parent, child = context.Pipe()
                process = context.Process(
                    target=child.send,
                    args=(("isolation_ready", {"backend": backend}),),
                )
                process.start()
                child.close()
                try:
                    self.assertEqual(
                        backend,
                        WorkerRunner._receive_isolation_ready(
                            parent, process, time.monotonic() + 2.0,
                        ),
                    )
                    process.join(2)
                finally:
                    if process.is_alive():
                        process.terminate()
                        process.join(2)
                    parent.close()
                self.assertEqual(0, process.exitcode)

    def test_real_backend_executes_zero_parent_adapter_callbacks(self) -> None:
        """Catches fresh exec inspecting or running the parent adapter object."""
        callback_evidence = Path(tempfile.mkdtemp(dir="/private/tmp")) / "callback"
        self.addCleanup(shutil.rmtree, callback_evidence.parent, True)

        class CallbackAdapter(_EffectReportingAdapter):
            requires_workspace = True

            def _mark(self, phase: str) -> None:
                callback_evidence.write_text(phase, encoding="utf-8")

            def set_prepared_workspace(
                self, path: str, device: int, inode: int,
            ) -> None:
                self._mark("prepared_workspace")

            def set_process_group_registrar(self, registrar: object) -> None:
                self._mark("process_group")

            def set_spawn_context(self, context: dict[str, object], emit: object) -> None:
                self._mark("spawn_context")

            def set_effect_context(self, context: dict[str, object], emit: object) -> None:
                self._mark("effect_context")

            def spawn(self, item: dict, *, deadline_seconds: float) -> object:
                self._mark("spawn")
                return object()

            def drive(
                self, handle: object, item: dict, *, deadline_seconds: float,
            ) -> list[dict[str, str]]:
                self._mark("drive")
                return []

        case = _EffectWorkerCase(self, CallbackAdapter(()))
        runner = case.runner(instrument_exec=False)
        result = runner.run(
            "node-a", "codex", now=case.run.now(8),
            run_id=case.run.run_id, item_id=case.run.parent,
            attempt_id=case.run.opened["attempt_id"],
        )

        if result.get("outcome_code") == "effect_worker_isolation_unavailable":
            self.assertEqual("degrade", result["transition"])
        else:
            self.assertEqual("complete", result["transition"], result)
            self.assertIsNone(result.get("outcome_code"), result)
        self.assertFalse(callback_evidence.exists())
        self.assertEqual([], case.effect_ledger.records())
        self.assertNotEqual("process_lost", result.get("outcome_code"))

    def test_effect_exec_isolation_ready_is_first_accepted_child_frame(self) -> None:
        """Catches adapter callbacks or spawn acknowledgement preceding isolation."""
        context = multiprocessing.get_context("fork")
        parent, child = context.Pipe()
        adapter = _EffectReportingAdapter((_EffectWorkerCase.intent_event(),))
        process = context.Process(
            target=_adapter_process,
            args=(child, adapter, {"id": "work-a"}, 2.0, None, {}, object()),
        )
        process.start()
        child.close()
        messages = []
        try:
            while not messages or messages[-1][0] != "result":
                messages.append(parent.recv())
            parent.send(("effect_reporting_closed", None))
            messages.append(parent.recv())
            process.join(2)
        finally:
            if process.is_alive():
                process.terminate()
                process.join(2)
            parent.close()
        self.assertEqual(0, process.exitcode)
        self.assertEqual(
            ["isolation_ready", "effect", "spawned", "result", "effect_reporting_closed_ack"],
            [message[0] for message in messages],
        )
        self.assertEqual(
            {"backend": "macos-sandbox"}, messages[0][1],
        )

    def test_spawn_private_pipe_positive_control_remains_green(self) -> None:
        """Keeps hostile Effect-pipe refusals non-vacuous beside the prior pipe."""
        context = {
            "schema_version": 1, "run_id": "run-a", "item_id": "work-a",
            "attempt_id": "attempt-a", "fence_token": "f" * 64,
            "attempt_spawn_policy_id": "attempt-spawn-policy-bound-a",
            "adapter": "fixture", "subagents_mode": "observed_only",
        }
        fork = multiprocessing.get_context("fork")
        parent, child = fork.Pipe()
        process = fork.Process(
            target=_adapter_process,
            args=(child, _SpawnContextAdapter(), {"id": "work-a"}, 2.0, context),
        )
        process.start()
        child.close()
        messages = [parent.recv(), parent.recv(), parent.recv(), parent.recv()]
        parent.send(("observation_closed", None))
        self.assertEqual(("observation_closed_ack", None), parent.recv())
        process.join(2)
        parent.close()
        self.assertEqual(
            ["descendant", "descendant", "spawned", "result"],
            [row[0] for row in messages],
        )
        self.assertEqual(0, process.exitcode)

    def test_real_child_pipe_can_report_lawful_intent_dispatch_and_ack(self) -> None:
        """Catches effect reports being dropped or bypassing the real fork pipe."""
        adapter = _EffectReportingAdapter((
            _EffectWorkerCase.intent_event(),
            _EffectWorkerCase.dispatch_event(),
            _EffectWorkerCase.acknowledgement_event(),
        ))
        case = _EffectWorkerCase(self, adapter)

        result = case.execute()

        self.assertEqual("complete", result["transition"])
        rows = case.effect_ledger.records()
        self.assertEqual(
            ["effect_intent", "effect_dispatched", "effect_acknowledged"],
            [row["kind"] for row in rows],
        )
        operation = next(iter(case.effect_ledger.project()._operations.values()))
        self.assertEqual("acknowledged", operation["state"])
        self.assertEqual(case.run.run_id, operation["run_id"])
        self.assertEqual(case.run.parent, operation["item_id"])
        self.assertEqual(case.run.opened["attempt_id"], operation["attempt_id"])

    def test_effect_event_without_governed_attempt_context_refuses_before_fork(self) -> None:
        """Catches an effect-enabled adapter starting without durable Run coordinates."""
        directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, directory)
        marker = Path(directory) / "adapter-started"

        class MarkingAdapter(_EffectReportingAdapter):
            def spawn(self, item: dict, *, deadline_seconds: float) -> object:
                marker.write_text("started", encoding="utf-8")
                return super().spawn(item, deadline_seconds=deadline_seconds)

        case = _EffectWorkerCase(
            self, MarkingAdapter((_EffectWorkerCase.intent_event(),)),
        )

        with self.assertRaises(ProtocolRefusal) as caught:
            case.execute(include_context=False)

        self.assertEqual("effect_launch_coordinates_invalid", caught.exception.code)
        self.assertFalse(marker.exists())
        self.assertEqual([], case.effect_ledger.records())

    def test_duplicate_or_mutated_same_pipe_event_cannot_append_twice(self) -> None:
        """Catches duplicate delivery or post-send mutation creating another intent."""
        first = _EffectWorkerCase.intent_event()
        duplicate = dict(first)
        changed = dict(first)
        changed["request_digest"] = "f" * 64
        case = _EffectWorkerCase(
            self, _EffectReportingAdapter((first, duplicate, changed)),
        )

        result = case.execute()

        self.assertEqual("degrade", result["transition"])
        self.assertEqual("protocol_error", result["outcome_code"])
        self.assertEqual(
            ["effect_intent"],
            [row["kind"] for row in case.effect_ledger.records()],
        )

    def test_effect_report_after_result_or_final_ack_is_protocol_error(self) -> None:
        """Catches effect testimony remaining writable after the worker result boundary."""
        case = _EffectWorkerCase(self, _EffectReportingAdapter(()))
        runner = case.runner()
        context = multiprocessing.get_context("fork")
        parent, child = context.Pipe()

        def late_report(connection: object) -> None:
            try:
                connection.send(("result", []))
                connection.send(("effect", _EffectWorkerCase.unknown_event()))
            finally:
                connection.close()

        process = context.Process(target=late_report, args=(child,))
        process.start()
        child.close()
        try:
            with self.assertRaises(WorkerAdapterFailure) as caught:
                runner._receive(
                    parent, process, time.monotonic() + 2, set(),
                    effect_application=(object(),),
                    effect_events_allowed=False,
                )
            self.assertEqual("protocol_error", caught.exception.code)
        finally:
            process.join(2)
            parent.close()

    def test_retained_emitter_after_close_ack_is_protocol_error_before_completion(self) -> None:
        """Catches close acknowledgement winning a race with a retained pipe emitter."""

        class RetainedEmitterAdapter(_EffectReportingAdapter):
            def set_effect_context(
                self, context: dict[str, object], emit: object,
            ) -> None:
                from multiprocessing.connection import Connection

                super().set_effect_context(context, emit)
                closure = getattr(emit, "__closure__", None) or ()
                connection = next(
                    cell.cell_contents
                    for cell in closure
                    if isinstance(cell.cell_contents, Connection)
                )
                self.retained_fd = os.dup(connection.fileno())

            def drive(
                self, handle: object, item: dict, *, deadline_seconds: float,
            ) -> list[dict[str, str]]:
                from multiprocessing.connection import Connection

                emitter_pid = os.fork()
                if emitter_pid == 0:
                    retained = Connection(self.retained_fd)
                    time.sleep(0.08)
                    try:
                        retained.send(("effect", _EffectWorkerCase.unknown_event()))
                    except (BrokenPipeError, EOFError, OSError):
                        pass
                    finally:
                        retained.close()
                    os._exit(0)
                os.close(self.retained_fd)
                return super().drive(
                    handle, item, deadline_seconds=deadline_seconds,
                )

        case = _EffectWorkerCase(self, RetainedEmitterAdapter(()))

        result = case.execute()

        self.assertEqual("degrade", result["transition"])
        self.assertEqual("protocol_error", result["outcome_code"])
        self.assertEqual([], case.effect_ledger.records())

    def test_clean_close_malformed_result_does_not_record_process_loss(self) -> None:
        """Catches parent-side validation failure becoming false process-loss testimony."""

        class MalformedEffectAdapter(_EffectReportingAdapter):
            def drive(
                self, handle: object, item: dict, *, deadline_seconds: float,
            ) -> list[dict[str, str]]:
                if self.report_during_drive:
                    self._report()
                return [{}]

        case = _EffectWorkerCase(
            self,
            MalformedEffectAdapter((
                _EffectWorkerCase.intent_event(),
                _EffectWorkerCase.dispatch_event(),
            ), report_during_drive=True),
        )

        result = case.execute()

        self.assertEqual("degrade", result["transition"])
        self.assertEqual("adapter_malformed_output", result["outcome_code"])
        rows = case.effect_ledger.records()
        self.assertEqual(
            ["effect_intent", "effect_dispatched"],
            [row["kind"] for row in rows],
        )
        operation = next(iter(case.effect_ledger.project()._operations.values()))
        self.assertEqual("dispatched", operation["state"])

    def test_effect_disabled_custom_adapter_retains_legacy_fork_without_bootstrap(self) -> None:
        """Catches the legacy path gaining isolation work, a hook, or a frame."""
        class DisabledAdapter(_CompletingAdapter):
            def set_effect_context(self, context: object, emit: object) -> None:
                raise AssertionError("disabled effect hook must remain inert")

        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = FloatiRoot.open_direct_home(Path(temp.name) / "fleet", create=True)
        Registry(root).register("lane-a", "Codex")
        work = WorkLog(root)
        work.add("legacy effect-disabled work", "lane-a", [], now=NOW)
        AuthorityGrantStore(root).claim("work-claims", "lane-a", 60, 60, NOW)

        def forbidden_clock() -> datetime:
            raise AssertionError("disabled effect context sampled its clock")

        with (
            mock.patch(
                "floati.workers.prepare_worker_isolation",
                side_effect=AssertionError("disabled effect path prepared isolation"),
            ),
            mock.patch(
                "floati.workers.apply_worker_isolation",
                side_effect=AssertionError("disabled effect path applied isolation"),
            ),
            mock.patch(
                "floati.workers.spawn_effect_worker",
                side_effect=AssertionError("disabled effect path used exec bootstrap"),
                create=True,
            ),
        ):
            result = WorkerRunner(
                root, {"fixture": DisabledAdapter(work)}, clock=forbidden_clock,
            ).run("lane-a", "fixture", now=NOW)
        self.assertEqual("complete", result["transition"])

        class DirectDisabledAdapter:
            name = "fixture"

            def spawn(self, item: dict, *, deadline_seconds: float) -> object:
                return object()

            def drive(
                self, handle: object, item: dict, *, deadline_seconds: float,
            ) -> list[dict[str, str]]:
                return []

        context = multiprocessing.get_context("fork")
        parent, child = context.Pipe()
        process = context.Process(
            target=_adapter_process,
            args=(child, DirectDisabledAdapter(), {"id": "legacy"}, 2.0),
        )
        process.start()
        child.close()
        try:
            frames = [parent.recv(), parent.recv()]
            process.join(2)
        finally:
            if process.is_alive():
                process.terminate()
                process.join(2)
            parent.close()
        self.assertEqual(0, process.exitcode)
        self.assertEqual(["spawned", "result"], [frame[0] for frame in frames])

    def test_effect_worker_parent_prepares_exact_workspace_before_fork(self) -> None:
        """Catches isolation preparation moving into the child or changing identity."""
        from floati.adapters.codex_live import CodexAppServerAdapter
        from floati.worker_isolation import prepare_worker_isolation as real_prepare

        with tempfile.TemporaryDirectory(dir="/private/tmp") as temporary:
            evidence = Path(temporary) / "prepared.jsonl"
            harness = (
                Path(__file__).parent
                / "fixtures"
                / "codex-app-server"
                / "reference_harness.py"
            )

            class ObservingCodexAdapter(CodexAppServerAdapter):
                def set_spawn_context(
                    self, context: dict[str, object], emit: object,
                ) -> None:
                    self.spawn_context = context
                    with evidence.open("a", encoding="utf-8") as stream:
                        stream.write(json.dumps({"phase": "spawn-context"}) + "\n")

                def set_effect_context(
                    self, context: dict[str, object], emit: object,
                ) -> None:
                    self.effect_context = context
                    with evidence.open("a", encoding="utf-8") as stream:
                        stream.write(json.dumps({"phase": "effect-context"}) + "\n")

                def set_prepared_workspace(
                    self, path: str, device: int, inode: int,
                ) -> None:
                    super().set_prepared_workspace(path, device, inode)
                    metadata = os.lstat(path)
                    with evidence.open("a", encoding="utf-8") as stream:
                        stream.write(json.dumps({
                            "phase": "child-hook", "pid": os.getpid(),
                            "path": path, "device": metadata.st_dev,
                            "inode": metadata.st_ino,
                        }) + "\n")

            def observing_prepare(
                tenant_home: Path, workspace: Path | None, session_id: str,
            ) -> object:
                policy = real_prepare(tenant_home, workspace, session_id)
                with evidence.open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps({
                        "phase": "parent-prepare", "pid": os.getpid(),
                        "path": str(policy.workspace),
                        "device": policy.workspace_identity[0],
                        "inode": policy.workspace_identity[1],
                    }) + "\n")
                return policy

            adapter = ObservingCodexAdapter((
                str(Path(sys.executable).resolve()), str(harness), "--mode", "complete",
            ))
            case = _EffectWorkerCase(self, adapter)

            def observing_apply(policy: object) -> str:
                with evidence.open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps({"phase": "isolation-applied"}) + "\n")
                return "macos-sandbox"

            with mock.patch(
                "floati.workers.prepare_worker_isolation", side_effect=observing_prepare,
            ), mock.patch(
                "floati.workers.apply_worker_isolation", side_effect=observing_apply,
            ):
                result = case.execute()

            rows = [json.loads(line) for line in evidence.read_text().splitlines()]
            parent_row = next(row for row in rows if row["phase"] == "parent-prepare")
            child_row = next(row for row in rows if row["phase"] == "child-hook")
            self.assertEqual("complete", result["transition"])
            self.assertEqual(os.getpid(), parent_row["pid"])
            self.assertNotEqual(parent_row["pid"], child_row["pid"])
            self.assertEqual(
                [
                    "parent-prepare", "isolation-applied", "child-hook",
                    "spawn-context", "effect-context",
                ],
                [row["phase"] for row in rows],
            )
            self.assertEqual(
                (parent_row["path"], parent_row["device"], parent_row["inode"]),
                (child_row["path"], child_row["device"], child_row["inode"]),
            )

    def test_isolation_probe_and_scratch_cleanup_preserve_workspace_artifacts(self) -> None:
        """Catches cleanup leaking ephemeral paths or deleting durable artifacts."""
        from floati.adapters.codex_live import CodexAppServerAdapter

        with tempfile.TemporaryDirectory(dir="/private/tmp") as temporary:
            evidence = Path(temporary) / "policy.json"
            harness = (
                Path(__file__).parent
                / "fixtures"
                / "codex-app-server"
                / "reference_harness.py"
            )

            def record_policy(policy: object) -> str:
                evidence.write_text(json.dumps({
                    "workspace": str(policy.workspace),
                    "scratch": str(policy.scratch),
                    "probe": str(policy.write_probe),
                }), encoding="utf-8")
                return "macos-sandbox"

            class ContextCodexAdapter(CodexAppServerAdapter):
                def set_spawn_context(
                    self, context: dict[str, object], emit: object,
                ) -> None:
                    self.spawn_context = context

                def set_effect_context(
                    self, context: dict[str, object], emit: object,
                ) -> None:
                    self.effect_context = context

            adapter = ContextCodexAdapter((
                str(Path(sys.executable).resolve()), str(harness), "--mode", "complete",
            ))
            case = _EffectWorkerCase(self, adapter)
            with mock.patch(
                "floati.workers.apply_worker_isolation", side_effect=record_policy,
            ):
                result = case.execute()

            paths = json.loads(evidence.read_text(encoding="utf-8"))
            workspace = Path(paths["workspace"])
            scratch = Path(paths["scratch"])
            probe = Path(paths["probe"])
            self.assertTrue(probe.name.startswith(".floati-effect-worker-"))
            self.assertTrue(scratch.name.startswith("floati-effect-worker-"))
            self.assertFalse(probe.name.startswith(".slipway-effect-worker-"))
            self.assertFalse(scratch.name.startswith("slipway-effect-worker-"))
            self.assertFalse(scratch.name.startswith("."))
            self.assertFalse(os.path.lexists(workspace / ".slipway"))
            self.assertEqual("complete", result["transition"])
            self.assertTrue((workspace / "PROOF.txt").is_file())
            self.assertFalse(scratch.exists())
            self.assertFalse(probe.exists())

    def test_isolation_prefix_contract_uses_floati_and_never_creates_legacy_forms(
        self,
    ) -> None:
        """Catches a partial probe/scratch storage rename before Worker execution."""
        from floati import worker_isolation

        with self.subTest("probe prefix"):
            self.assertEqual(
                ".floati-effect-worker-", worker_isolation._PROBE_PREFIX,
            )
        with self.subTest("scratch prefix"):
            self.assertEqual(
                "floati-effect-worker-", worker_isolation._SCRATCH_PREFIX,
            )

        with tempfile.TemporaryDirectory(dir="/private/tmp") as temporary:
            root = Path(temporary)
            tenant = root / "tenant"
            effects = tenant / "effects"
            effects.mkdir(parents=True)
            policy = prepare_worker_isolation(
                tenant, root / "workspace", "floati-effect-prefix-contract",
            )
            try:
                legacy_probe = policy.write_probe.with_name(
                    policy.write_probe.name.replace(
                        ".floati-effect-worker-", ".slipway-effect-worker-", 1,
                    ),
                )
                legacy_scratch = policy.scratch.with_name(
                    policy.scratch.name.replace(
                        "floati-effect-worker-", "slipway-effect-worker-", 1,
                    ),
                )
                with self.subTest("legacy hidden probe absent"):
                    self.assertFalse(os.path.lexists(legacy_probe))
                with self.subTest("legacy unhidden scratch absent"):
                    self.assertFalse(os.path.lexists(legacy_scratch))
            finally:
                cleanup_worker_isolation(policy)

    def test_isolation_cleanup_failure_degrades_without_removing_workspace(self) -> None:
        """Catches cleanup failure completing work or deleting its persistent output."""
        from floati.adapters.codex_live import CodexAppServerAdapter

        harness = (
            Path(__file__).parent
            / "fixtures"
            / "codex-app-server"
            / "reference_harness.py"
        )

        class ContextCodexAdapter(CodexAppServerAdapter):
            def set_spawn_context(
                self, context: dict[str, object], emit: object,
            ) -> None:
                self.spawn_context = context

            def set_effect_context(
                self, context: dict[str, object], emit: object,
            ) -> None:
                self.effect_context = context

        adapter = ContextCodexAdapter((
            str(Path(sys.executable).resolve()), str(harness), "--mode", "complete",
        ))
        case = _EffectWorkerCase(self, adapter)
        from floati.worker_isolation import (
            cleanup_worker_isolation as real_cleanup,
            prepare_worker_isolation as real_prepare,
        )
        policies: list[object] = []
        real_unlink = os.unlink
        failed = False

        def capture_prepare(*args: object, **kwargs: object) -> object:
            policy = real_prepare(*args, **kwargs)
            policies.append(policy)
            return policy

        def fail_probe_once(
            path: object, *args: object, **kwargs: object,
        ) -> None:
            nonlocal failed
            candidate = Path(os.fsdecode(path))
            if candidate.name.startswith(".floati-effect-worker-") and not failed:
                failed = True
                raise OSError(errno.EACCES, "probe unlink failed")
            real_unlink(path, *args, **kwargs)

        with (
            mock.patch(
                "floati.workers.prepare_worker_isolation",
                side_effect=capture_prepare,
            ),
            mock.patch(
                "floati.worker_isolation.os.unlink",
                side_effect=fail_probe_once,
            ),
        ):
            result = case.execute()

        workspace = Path("/private/tmp/floati-work") / case.run.parent
        self.assertEqual(1, len(policies))
        policy = policies[0]
        self.assertTrue(policy.write_probe.name.startswith(".floati-effect-worker-"))
        self.assertTrue(policy.scratch.name.startswith("floati-effect-worker-"))
        self.assertFalse(policy.write_probe.name.startswith(".slipway-effect-worker-"))
        self.assertFalse(policy.scratch.name.startswith("slipway-effect-worker-"))
        self.assertFalse(policy.scratch.name.startswith("."))
        self.assertFalse(os.path.lexists(workspace / ".slipway"))
        self.assertEqual("degrade", result["transition"])
        self.assertEqual("adapter_error", result["outcome_code"])
        self.assertTrue((workspace / "PROOF.txt").is_file())
        self.assertEqual("claimed", WorkLog(case.root).show(case.run.parent)[0]["state"])
        self.assertTrue(policy.write_probe.is_file())
        real_cleanup(policy)
        self.assertFalse(policy.write_probe.exists())
        self.assertFalse(policy.scratch.exists())


class WorkerCancellationDeclarationTests(unittest.TestCase):
    def test_live_and_boundary_adapters_declare_one_frozen_cancel_mode(self) -> None:
        """Catches an adapter entering cancellation without an explicit governed mode."""
        from floati.adapters.claude import ClaudeHeadlessAdapter
        from floati.adapters.codex_live import CodexAppServerAdapter
        from floati.adapters.pi import PiRpcAdapter
        from floati.workers import CodexBoundaryAdapter

        modes = {CodexBoundaryAdapter.cancel_mode, CodexAppServerAdapter.cancel_mode,
                 ClaudeHeadlessAdapter.cancel_mode, PiRpcAdapter.cancel_mode}
        self.assertEqual({"native", "unavailable"}, modes)
        self.assertTrue(modes <= {"native", "local_process_only", "unavailable"})


if __name__ == "__main__":
    unittest.main()

"""Actual WorkerRunner publication follows the owned child cleanup boundary."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import tests  # Pin all product defaults away from the operator's HOME.
from floati.events import EventLog
from floati.planes import AuthorityGrantStore
from floati.registry import Registry
from floati.root import FloatiRoot
from floati.seat_execution import SeatExecutionController
from floati.work import WorkLog
from floati.workers import WorkerReceipts, WorkerRunner
from tests import test_workers as adapters


ARTIFACT = {"repo": "floati-proof", "sha": "a" * 40, "doc": "README.md"}
CAR = {"row": "F2-SEAT-STALL-VISIBLE-1", "repository": "floati-proof", "branch": "codex/native-car"}


class NativeWorkerExecutionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.root = FloatiRoot.open_direct_home(self.base / "fleet", create=True)
        self.workspace = self.base / "native-workspace"
        self.workspace.mkdir()
        self.node = "builder"
        registry = Registry(self.root)
        registry.register(self.node, "Codex")
        registry.register("architect", "Codex")
        registry.record_governance(topology="star", coordinator="architect",
            coordinator_authority=("dispatch_bounded_work",), owner_tier=("publishing",))
        self.now = datetime.now(timezone.utc)
        self.work = WorkLog(self.root)
        self.item = self.work.add("native producer boundary", self.node, [ARTIFACT], car_binding=CAR, now=self.now)
        AuthorityGrantStore(self.root).claim("work-claims", self.node, 60, 60, self.now)

    def execution_rows(self):
        return [row for row in EventLog(self.root).event_records() if row.get("kind") == "seat_execution"]

    def notices(self):
        return [row for row in EventLog(self.root).event_records() if row.get("kind") == "message_envelope"]

    def run_worker(self, runner, on_drive=None):
        with mock.patch("floati.workers.Path.cwd", return_value=self.workspace):
            return runner.run(self.node, "fixture", now=self.now, on_drive=on_drive)

    def assert_real_cleanup_finished(self, runner):
        pid = runner.last_process_audit["adapter_pid"]
        self.assertIsInstance(pid, int)
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)

    def test_failure_starts_at_native_spawn_and_ends_only_after_owned_cleanup(self):
        runner = WorkerRunner(self.root, {"fixture": adapters._MalformedOutputAdapter()})
        during_drive, during_cleanup = [], []
        native_cleanup = WorkerRunner._terminate_process_group
        def observe_cleanup(group):
            during_cleanup.append([row["event"] for row in self.execution_rows()])
            native_cleanup(group)
            during_cleanup.append([row["event"] for row in self.execution_rows()])
        def on_drive():
            during_drive.append(SeatExecutionController(self.root).project(self.node)["state"])
            self.assertEqual(["running"], [row["event"] for row in self.execution_rows()])
            self.assertEqual([], self.notices())
        with mock.patch.object(WorkerRunner, "_terminate_process_group", side_effect=observe_cleanup):
            result = self.run_worker(runner, on_drive)
        self.assertEqual(["RUNNING"], during_drive)
        self.assertTrue(during_cleanup)
        self.assertTrue(all(events == ["running"] for events in during_cleanup))
        self.assertEqual("degrade", result["transition"])
        self.assertEqual("adapter_malformed_output", result["outcome_code"])
        self.assertEqual([], runner.last_process_audit["alive_after_cleanup"])
        self.assert_real_cleanup_finished(runner)
        rows = self.execution_rows()
        self.assertEqual(["running", "ended"], [row["event"] for row in rows])
        self.assertEqual("adapter_malformed_output", rows[-1]["code"])
        self.assertEqual(runner.last_process_audit, json.loads(rows[-1]["detail"]))
        spawn = next(row for row in WorkerReceipts(self.root).records() if row["transition"] == "spawn")
        self.assertEqual(spawn["id"], rows[0]["source_receipt_id"])
        self.assertEqual(rows[-1]["envelope_id"], self.notices()[0]["id"])
        self.assertEqual("STALLED", SeatExecutionController(self.root).project(self.node)["state"])

    def test_unavailable_native_liveness_observation_reports_unknown_after_real_join(self):
        runner = WorkerRunner(self.root, {"fixture": adapters._MalformedOutputAdapter()})
        native_killpg = os.killpg
        def denied_observation(group, signal):
            if signal == 0:
                raise PermissionError("scratch liveness observation unavailable")
            return native_killpg(group, signal)
        with mock.patch("floati.workers.os.killpg", side_effect=denied_observation):
            result = self.run_worker(runner)
        self.assertEqual("adapter_malformed_output", result["outcome_code"])
        self.assertTrue(runner.last_process_audit["alive_after_cleanup"])
        self.assert_real_cleanup_finished(runner)
        rows = self.execution_rows()
        self.assertEqual(["running", "unknown"], [row["event"] for row in rows])
        self.assertEqual("worker_cleanup_unconfirmed", rows[-1]["code"])
        self.assertEqual(runner.last_process_audit, json.loads(rows[-1]["detail"]))
        self.assertEqual("UNKNOWN", SeatExecutionController(self.root).project(self.node)["state"])

    def test_failed_native_notice_transaction_preserves_worker_outcome_and_cleanup(self):
        runner = WorkerRunner(self.root, {"fixture": adapters._MalformedOutputAdapter()})
        native_send = EventLog.send
        publications = []
        def failed_notice(log, *arguments, **keywords):
            if keywords.get("_execution_publication") is not None:
                publications.append(True)
                with mock.patch("floati.jsonl.os.fsync", side_effect=OSError("scratch publication disk failure")):
                    return native_send(log, *arguments, **keywords)
            return native_send(log, *arguments, **keywords)
        with mock.patch.object(EventLog, "send", new=failed_notice):
            result = self.run_worker(runner)
        self.assertEqual([True], publications)
        self.assertEqual("degrade", result["transition"])
        self.assertEqual("adapter_malformed_output", result["outcome_code"])
        self.assertEqual([], runner.last_process_audit["alive_after_cleanup"])
        self.assert_real_cleanup_finished(runner)
        self.assertEqual(["running"], [row["event"] for row in self.execution_rows()])
        self.assertEqual([], self.notices())
        self.assertEqual("unknown", runner.last_execution_report["status"])
        self.assertEqual("UNKNOWN", SeatExecutionController(self.root).project(self.node)["state"])

    def test_banked_completion_has_no_stalled_notice_for_a_completed_car(self):
        runner = WorkerRunner(self.root, {"fixture": adapters._CompletingAdapter(self.work)})
        observed = []
        result = self.run_worker(runner, lambda: observed.append(
            SeatExecutionController(self.root).project(self.node)["state"]))
        self.assertEqual(["RUNNING"], observed)
        self.assertEqual("complete", result["transition"])
        self.assertEqual("completed", self.work.show(self.item["id"])[0]["state"])
        self.assertEqual([], runner.last_process_audit["alive_after_cleanup"])
        self.assert_real_cleanup_finished(runner)
        self.assertEqual([], self.notices())
        self.assertEqual("NO_CAR", SeatExecutionController(self.root).project(self.node)["state"])

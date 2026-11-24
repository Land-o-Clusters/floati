"""Native Cursor publication failures retain the car owned at hold start."""
from datetime import datetime, timezone
import io
import os
import unittest
from unittest import mock

import tests  # Keep product defaults out of the operator's HOME.
from floati.events import EventLog
from floati import cursor_wait
from floati.planes import AuthorityGrantStore
from floati.registry import Registry
from floati.seat_execution import SeatExecutionController
from floati.work import WorkLog
from tests import test_cursor_followup_delivery_visible as native
from tests.waiter_timing import scripted_readiness


BINDING = {"repo": "floati", "sha": "a" * 40, "doc": "docs/car.md"}
CAR = {"row": "F2-SEAT-STALL-VISIBLE-1", "repository": "floati", "branch": "codex/car"}


class CursorStartBindingTests(unittest.TestCase):
    def setUp(self):
        self.f = native.CursorFollowupDeliveryVisibleTests("runTest")
        self.addCleanup(self.f.doCleanups)
        self.f.setUp()
        self.root, self.node = self.f.root, self.f.node
        Registry(self.root).record_governance(topology="star", coordinator="architect",
            coordinator_authority=("dispatch_bounded_work",), owner_tier=("publishing",))
        self.now = datetime.now(timezone.utc)
        self.work = WorkLog(self.root)
        self.grant = AuthorityGrantStore(self.root).claim("work-claims", self.node, 60, 60, self.now)

    def claim_car(self):
        item = self.work.add("native Cursor implementation", self.node, [BINDING],
            car_binding=CAR, now=self.now)
        claim = self.work.claim(item["id"], self.node, "work-claims", self.grant["epoch"], now=self.now)
        return item, claim

    def rows(self):
        return EventLog(self.root).event_records()

    def fail_at_deadline(self, phase, change=None):
        reader, writer = os.pipe()
        reader_open = [True]
        def at_native_sleep():
            if change is not None:
                change()
            os.close(reader)
            reader_open[0] = False
        self.addCleanup(lambda: os.close(reader) if reader_open[0] else None)
        stream = (io.TextIOWrapper(os.fdopen(writer, "wb", buffering=0),
            encoding="utf-8", write_through=True) if phase == "write"
            else os.fdopen(writer, "w", buffering=8192, encoding="utf-8"))
        output = native._ObservedPipe(stream)
        self.addCleanup(output.close)
        # CURSOR-HOOK-REARM-DEATH-1: the closed pipe ends the hook quietly
        # (rc 0, never an escaping traceback). Every attribution assertion
        # below is unchanged - the failure stays bound to the car owned at
        # hold start, and the dispatcher still hears followup_failed.
        self.assertEqual(0, self.f.run_deadline(output, close_reader=at_native_sleep))
        self.assertEqual(phase, output.failure_phase)
        self.f.assert_native_deadline()
        return output

    def assert_no_failed_attribution(self, item):
        self.assertFalse(any(row.get("kind") == "seat_execution"
            and row.get("event") == "followup_failed"
            and row.get("work_item_id") == item["id"] for row in self.rows()))
        self.assertFalse(any(row.get("kind") == "message_envelope" for row in self.rows()))
        self.assertEqual("UNKNOWN", SeatExecutionController(self.root).project(self.node)["state"])

    def test_native_write_failure_is_bound_to_the_original_claim(self):
        self.stable_failure("write")

    def test_native_flush_failure_is_bound_to_the_original_claim(self):
        self.stable_failure("flush")

    def stable_failure(self, phase):
        item, claim = self.claim_car()
        at_start = []
        self.fail_at_deadline(phase, lambda: at_start.append(
            SeatExecutionController(self.root).project(self.node)))
        self.assertEqual(["RUNNING"], [state["state"] for state in at_start])
        self.assertEqual(claim["id"], at_start[0]["work_claim_id"])
        rows = [row for row in self.rows() if row.get("kind") == "seat_execution"]
        self.assertEqual(["running", "followup_failed"], [row["event"] for row in rows])
        self.assertTrue(all(row["work_item_id"] == item["id"]
            and row["work_claim_id"] == claim["id"] for row in rows))
        self.assertEqual(self.f.at_sleep[0]["invocation_id"], rows[0]["invocation_id"])
        self.assertIn("BrokenPipeError", rows[-1]["detail"])
        self.assertEqual("STALLED", SeatExecutionController(self.root).project(self.node)["state"])

    def replacement_failure(self, phase, owned_at_start):
        original = self.claim_car()[0] if owned_at_start else None
        replacement = []
        def replace():
            if original is not None:
                self.work.complete(original["id"], self.node, [BINDING], now=self.now)
            replacement.append(self.claim_car()[0])
        self.fail_at_deadline(phase, replace)
        self.assert_no_failed_attribution(replacement[0])

    def test_write_failure_of_historic_hold_cannot_bind_replacement_car(self):
        cases = (("write", True), ("flush", True), ("write", False), ("flush", False))
        for index, case in enumerate(cases):
            phase, owned_at_start = case
            with self.subTest(phase=phase, owned_at_start=owned_at_start):
                if index:
                    self.doCleanups()
                    self.setUp()
                self.replacement_failure(phase, owned_at_start)

    def test_successful_followup_settles_only_its_running_veto_without_notice(self):
        item, claim = self.claim_car()
        observed = []
        output = native._ObservedPipe(io.StringIO())
        self.addCleanup(output.close)
        self.assertEqual(0, self.f.run_deadline(output, close_reader=lambda: observed.append(
            SeatExecutionController(self.root).project(self.node)["state"])))
        self.assertEqual(["RUNNING"], observed)
        rows = [row for row in self.rows() if row.get("kind") == "seat_execution"]
        self.assertEqual(["running", "unknown"], [row["event"] for row in rows])
        self.assertEqual("cursor_followup_returned", rows[-1]["code"])
        self.assertEqual("UNKNOWN", SeatExecutionController(self.root).project(self.node)["state"])
        self.assertFalse(any(row.get("kind") == "message_envelope" for row in self.rows()))

    def test_optional_car_fence_failure_preserves_normal_native_hold(self):
        self.claim_car()
        with mock.patch("floati.car_fence.car_acceptance_guard",
                side_effect=OSError("scratch execution fence unavailable")):
            self.assertEqual(0, self.f.run_deadline(io.StringIO()))
        self.f.assert_native_deadline()
        self.assertEqual([], self.rows())

    def test_missing_native_start_testimony_publishes_no_failure_callback(self):
        item, unused = self.claim_car()
        elapsed = [0.0]
        reader, writer = os.pipe()
        output = native._ObservedPipe(io.TextIOWrapper(os.fdopen(writer, "wb", buffering=0),
            encoding="utf-8", write_through=True))
        self.addCleanup(output.close)
        def sleep(seconds):
            os.close(reader)
            elapsed[0] = float(native.DEADLINE)
        with mock.patch("floati.stop_hold.StopHoldLedger._start_already_guarded",
                side_effect=OSError("scratch hold history unavailable")), mock.patch(
                "floati.cursor_wait.Path.cwd", return_value=self.f.f.workspace):
            # CURSOR-HOOK-REARM-DEATH-1: quiet rc 0 with no hold testimony -
            # and still no failure callback for an invocation that never held.
            self.assertEqual(0, cursor_wait.run_cursor_stop_wait(root=self.root.path, node=self.node,
                runtime=self.f.f.runtime, deadline_seconds=native.DEADLINE,
                poll_seconds=1, hook_timeout_seconds=native.HOOK_TIMEOUT,
                payload={"status": "completed", "conversation_id": self.f.f.session},
                stdout=output, stderr=self.f.errors, clock=lambda: elapsed[0], sleep=sleep,
                wait_readable=scripted_readiness(sleep, interval=1),
                now_utc=lambda: self.now.isoformat().replace("+00:00", "Z"),
                last_seen_root=self.f.f.workspace))
        self.assertEqual("write", output.failure_phase)
        self.assertEqual([], self.rows())
        self.assertEqual("UNKNOWN", SeatExecutionController(self.root).project(self.node)["state"])

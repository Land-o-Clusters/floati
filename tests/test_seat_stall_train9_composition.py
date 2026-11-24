"""Post-composition native controls for train9 and seat execution testimony.

These controls were added after source reconciliation, not as production RED.
Only output publication and host free-space observation are fault injected.
"""
from datetime import datetime, timezone
import io
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest import mock

import tests
from floati.codex_wait_contract import CodexWaitConsentLedger, resolve_participant
from floati.cursor_presentations import CursorPresentationLedger
from floati.events import EventLog
from floati.jsonl import read_records_snapshot
from floati.planes import AuthorityGrantStore
from floati.registry import Registry
from floati.seat_execution import SeatExecutionController
from floati.stop_hold import StopHoldLedger
from floati.work import WorkLog
from tests import test_f2_cursor_claimant as claimant
from tests import test_seat_execution_surfaces as surfaces


class _ObservedOutput(io.StringIO):
    def __init__(self, failure_phase=None):
        super().__init__()
        self.failure_phase = failure_phase
        self.writes = 0
        self.flushes = 0

    def write(self, value):
        self.writes += 1
        if self.failure_phase == "write":
            raise OSError("composition stdout write unavailable")
        return super().write(value)

    def flush(self):
        self.flushes += 1
        if self.failure_phase == "flush":
            raise OSError("composition stdout flush unavailable")
        return super().flush()


class SeatStallCursorCompositionTests(unittest.TestCase):
    def setUp(self):
        # Compose fixtures without inheriting their discovered test methods.
        self.c = claimant.CursorClaimantTests("runTest")
        self.addCleanup(self.c.doCleanups)
        self.c.setUp()
        self.root = self.c.f.root
        self.node = "composition-cursor"
        registry = Registry(self.root)
        registry.register(self.node, "Cursor")
        registry.record_governance(topology="star", coordinator="architect",
            coordinator_authority=("dispatch_bounded_work",), owner_tier=("publishing",))
        self.c.f.write_map([{"workspace": str(self.c.f.workspace), "node_id": self.node}])
        self.c.f.participant = resolve_participant(self.root.path, self.c.f.workspace)
        self.assertIsNotNone(self.c.f.participant)
        CodexWaitConsentLedger(self.root).arm(self.c.f.participant.binding,
            hook_timeout_seconds=10, wait_deadline_seconds=2,
            idempotency_key="composition-cursor-consent")
        self.c.node = self.node
        self.c.journal = self.root.path / "state/cursor-wait" / self.node / "journal.jsonl"
        self.c.f.arm_session(claimant.A)
        self.now = datetime.now(timezone.utc)
        self.work = WorkLog(self.root)
        grant = AuthorityGrantStore(self.root).claim("work-claims", self.node, 60, 60, self.now)
        self.item = self.work.add("train9 native Cursor composition", self.node,
            [surfaces.fixture.BINDING], car_binding=surfaces.fixture.CAR, now=self.now)
        self.claim = self.work.claim(self.item["id"], self.node, "work-claims",
            grant["epoch"], now=self.now)

    def send(self, key):
        return EventLog(self.root).send("architect", self.node,
            **surfaces.fixture.BINDING, note="native composition envelope", idempotency_key=key)

    def execution_rows(self):
        return [row for row in EventLog(self.root).event_records()
                if row["kind"] == "seat_execution"]

    def presentations(self):
        return CursorPresentationLedger(self.root).read(self.node)

    def assert_original_invocation(self, rows, events):
        self.assertEqual(events, [row["event"] for row in rows])
        self.assertTrue(all(row["work_item_id"] == self.item["id"]
            and row["work_claim_id"] == self.claim["id"]
            and row["acting_session_id"] == claimant.A for row in rows))
        self.assertEqual(1, len({row["invocation_id"] for row in rows}))
        holds = StopHoldLedger(self.root).read(self.node)
        self.assertEqual(["start", "exit"], [row["phase"] for row in holds])
        self.assertEqual(holds[0]["invocation_id"], rows[0]["invocation_id"])

    def assert_prepared(self, message):
        rows = self.presentations()
        self.assertEqual(["prepared"], [row["phase"] for row in rows])
        self.assertEqual([message["id"]], rows[0]["item_ids"])
        self.assertEqual(claimant.A, rows[0]["acting_session_id"])
        self.assertEqual([message["id"]], self.c.deliveries()[0]["item_ids"])
        self.assertEqual(1, len(self.c.deliveries()))

    def fail_fresh_publication(self, phase):
        messages = []
        def send_during_native_hold():
            projection = SeatExecutionController(self.root).project(self.node)
            self.assertEqual("RUNNING", projection["state"])
            self.assertEqual(self.claim["id"], projection["work_claim_id"])
            messages.append(self.send("composition-fresh-" + phase))
        output = _ObservedOutput(phase)
        with self.assertRaisesRegex(OSError, "composition stdout " + phase):
            self.c.run_waiter(stdout=output, sleep=send_during_native_hold)
        self.assertEqual(1, len(messages))
        self.assertEqual(1, output.writes)
        self.assertEqual(0 if phase == "write" else 1, output.flushes)
        self.assert_prepared(messages[0])
        rows = self.execution_rows()
        self.assert_original_invocation(rows, ["running", "followup_failed"])
        self.assertEqual("cursor_followup_publication_failed", rows[-1]["code"])
        journal = self.c.rows()[-1]
        self.assertEqual("failed", journal["publication"])
        self.assertEqual(phase, journal["failure_phase"])
        self.assertEqual(rows[-1]["invocation_id"], journal["invocation_id"])
        self.assertFalse(any(row.get("publication") == "flushed" for row in self.c.rows()))
        self.assertEqual("STALLED", SeatExecutionController(self.root).project(self.node)["state"])
        notices = [row for row in EventLog(self.root).records() if row["sender"] == self.node]
        self.assertEqual([rows[-1]["envelope_id"]], [row["id"] for row in notices])

    def test_fresh_native_mail_write_failure_preserves_claim_and_uncertain_preparation(self):
        self.fail_fresh_publication("write")

    def test_fresh_native_mail_flush_failure_preserves_claim_and_uncertain_preparation(self):
        self.fail_fresh_publication("flush")

    def test_terminal_save_failure_after_stdout_flush_stays_unknown_without_output_retry(self):
        messages = []
        output = _ObservedOutput()
        with mock.patch.object(CursorPresentationLedger, "flushed",
                side_effect=OSError("composition terminal save unavailable")):
            body, errors = self.c.run_waiter(stdout=output,
                sleep=lambda: messages.append(self.send("composition-terminal-save")))
        self.assertEqual(1, len(messages))
        self.assertIn(messages[0]["id"], body["followup_message"])
        self.assertEqual((1, 1), (output.writes, output.flushes))
        self.assertIn("presentation_uncertain", errors)
        self.assertEqual("presentation_uncertain", self.c.rows()[-1]["presentation_phase"])
        self.assertEqual("flushed", self.c.rows()[-1]["publication"])
        self.assert_prepared(messages[0])
        rows = self.execution_rows()
        self.assert_original_invocation(rows, ["running", "unknown"])
        self.assertEqual("cursor_followup_returned", rows[-1]["code"])
        self.assertTrue(all(row["envelope_id"] is None for row in rows))
        self.assertEqual("UNKNOWN", SeatExecutionController(self.root).project(self.node)["state"])
        self.assertEqual([messages[0]["id"]], [row["id"] for row in EventLog(self.root).records()])
        before = self.c.deliveries()
        recovery, _ = self.c.run_waiter()
        self.assertIn("presentation_uncertain", recovery["followup_message"])
        self.assertEqual(before, self.c.deliveries())
        self.assertEqual(["prepared"], [row["phase"] for row in self.presentations()])

    def test_old_unacked_only_deadline_preserves_delivery_and_closes_only_its_invocation(self):
        old = self.send("composition-held-only")
        self.c.run_waiter()
        before_deliveries = self.c.deliveries()
        before_presentations = self.presentations()
        self.assertEqual(["prepared", "flushed"], [row["phase"] for row in before_presentations])
        self.assertEqual([], self.execution_rows(), "immediate mail cannot invent a native hold")
        output = _ObservedOutput()
        observed = []
        def observe_native_hold():
            self.assertEqual("", output.getvalue())
            self.assertEqual(before_deliveries, self.c.deliveries())
            observed.append(SeatExecutionController(self.root).project(self.node)["state"])
        body, _ = self.c.run_waiter(stdout=output, sleep=observe_native_hold)
        self.assertEqual(["RUNNING"], observed)
        self.assertIn("held_unacked", body["followup_message"])
        self.assertIn(old["id"], body["followup_message"])
        self.assertNotIn("Drain it now", body["followup_message"])
        self.assertEqual(2, self.c.rows()[-1]["waited_seconds"])
        self.assertEqual(before_deliveries, self.c.deliveries())
        self.assertEqual(before_presentations, self.presentations())
        acknowledgments = read_records_snapshot(self.root,
            Path("receipts/acks") / (self.node + ".jsonl"), allowed_kinds={"ack_receipt"})
        self.assertEqual([], acknowledgments)
        rows = self.execution_rows()
        self.assert_original_invocation(rows, ["running", "unknown"])
        self.assertTrue(all(row["envelope_id"] is None for row in rows))
        self.assertEqual("UNKNOWN", SeatExecutionController(self.root).project(self.node)["state"])
        self.assertEqual([old["id"]], [row["id"] for row in EventLog(self.root).records()])


class SeatStallBoardCompositionTests(unittest.TestCase):
    def test_fresh_and_cached_board_expose_stalled_car_and_native_disk_warning(self):
        from floati.projection import FleetProjection
        from floati.snapshot import SnapshotStore
        from floati.tui import model_from_root
        from floati.tui_render import DISK_WATCH_ALERT, _detail_lines, render_frame
        case = surfaces.SeatExecutionSurfaceTests("runTest")
        self.addCleanup(case.doCleanups)
        case.setUp()
        case.stalled()
        store = SnapshotStore(case.root, reader="board", key="full-redraw",
            discover_sources=FleetProjection(case.root)._status_sources)
        self.assertFalse(store.path.exists())
        current = datetime.now(timezone.utc)
        with mock.patch("os.statvfs", return_value=SimpleNamespace(f_bavail=4096, f_frsize=1)):
            fresh = model_from_root(case.root, now=current)
            store.load()  # Prove the native cache is usable before the next call.
            banked_snapshot = store.path.read_bytes()
            cached = model_from_root(case.root, now=current)
        self.assertEqual(banked_snapshot, store.path.read_bytes())
        for route, model in (("fresh", fresh), ("cached", cached)):
            with self.subTest(route=route):
                index = next(i for i, row in enumerate(model.nodes) if row["node_id"] == "builder")
                projection = model.nodes[index]["car_execution"]
                self.assertEqual("STALLED", projection["state"])
                self.assertEqual("process_died", projection["reason"])
                self.assertEqual(1, model.effects["disk_watch"]["exit"])
                self.assertEqual(4096, model.effects["disk_watch"]["free_bytes"])
                detail = "\n".join(_detail_lines(model, 200, index))
                self.assertIn("STALLED", detail)
                self.assertIn("process_died", detail)
                self.assertIn(DISK_WATCH_ALERT, render_frame(model, 100, 30, selected=index, color=False))
        self.assertEqual(fresh.nodes, cached.nodes)


if __name__ == "__main__":
    unittest.main()

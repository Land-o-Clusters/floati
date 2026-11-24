"""Native epoch replay and simultaneous car completion/notice orderings."""
from tests.readiness import wait_event, wait_poll
from datetime import datetime, timezone
import fcntl
import multiprocessing
import os
import unittest
from unittest import mock

import tests
from floati.bus_epoch import ROLL_AUTHORITY_SUBJECT, roll_bus_epoch
from floati.errors import ProtocolRefusal
from floati.events import EventLog
from floati.planes import AuthorityGrantStore
from tests import test_seat_execution as fixture


PAYLOAD = {"event": "ended", "code": "process_died", "detail": "native cleanup completed"}


def _complete_first(work, item, entered, release, result):
    from floati import work as work_module
    transaction = work_module.transact

    def held_transition(*args, **kwargs):
        entered.set()
        if not wait_event(release, 'seat_execution_epoch_race: release synchronization', timeout=120):
            raise RuntimeError("scratch race handoff exhausted")
        return transaction(*args, **kwargs)

    try:
        with mock.patch("floati.work.transact", side_effect=held_transition):
            row = work.complete(item["id"], "builder", [fixture.BINDING], now=fixture.NOW)
        result.send(("completed", row))
    except Exception as exc:
        result.send(("error", repr(exc)))
    finally:
        result.close()


def _notify_first(controller, invocation, entered, release, result):
    native_send = EventLog.send

    def held_send(events, *args, **kwargs):
        entered.set()
        if not wait_event(release, 'seat_execution_epoch_race: release synchronization', timeout=120):
            raise RuntimeError("scratch race handoff exhausted")
        return native_send(events, *args, **kwargs)

    try:
        with mock.patch.object(EventLog, "send", held_send):
            row = controller.callback(invocation, PAYLOAD)
        result.send(("notified", row))
    except Exception as exc:
        result.send(("error", repr(exc)))
    finally:
        result.close()


class SeatExecutionEpochRaceTests(unittest.TestCase):
    setUp = fixture.SeatExecutionTests.setUp
    car = fixture.SeatExecutionTests.car
    claim = fixture.SeatExecutionTests.claim
    controller = fixture.SeatExecutionTests.controller
    invocation = fixture.SeatExecutionTests.invocation

    def prepare(self):
        item = self.car()
        self.claim(item)
        controller = self.controller()
        invocation = self.invocation(controller, item)
        return item, controller, invocation

    def test_accepted_callback_replays_exactly_after_native_roll_and_completion(self):
        item, controller, invocation = self.prepare()
        accepted = controller.callback(invocation, PAYLOAD)
        self.work.complete(item["id"], "builder", [fixture.BINDING], now=fixture.NOW)
        AuthorityGrantStore(self.root).grant_exact(ROLL_AUTHORITY_SUBJECT,
            "architect", 1, datetime.now(timezone.utc))
        receipt = roll_bus_epoch(self.root, actor="architect",
            idempotency_key="accepted-car-callback-epoch-roll")["receipt"]
        self.assertTrue((self.root.tenant_home / receipt["archive_path"]).is_dir())
        before = EventLog(self.root).event_records()
        self.assertFalse(any(row["kind"] == "message_envelope" for row in before))
        self.assertEqual(accepted, controller.callback(invocation, PAYLOAD))
        self.assertEqual(before, EventLog(self.root).event_records())
        self.assertEqual("NO_CAR", controller.project("builder")["state"])
        with self.assertRaisesRegex(ProtocolRefusal, "idempotency_conflict"):
            controller.callback(invocation, dict(PAYLOAD, detail="changed exact body"))

    def race(self, first):
        item, controller, invocation = self.prepare()
        context = multiprocessing.get_context("fork")
        entered, release = context.Event(), context.Event()
        receive, send = context.Pipe(duplex=False)
        target = _complete_first if first == "completion" else _notify_first
        owner = self.work if first == "completion" else controller
        subject = item if first == "completion" else invocation
        child = context.Process(target=target, args=(owner, subject, entered, release, send))
        child.start()
        send.close()
        contended = []
        parent_inode = (self.root.tenant_home / "work").stat().st_ino
        native_flock = fcntl.flock

        def release_on_native_contention(descriptor, operation):
            try:
                return native_flock(descriptor, operation)
            except BlockingIOError:
                if os.fstat(descriptor).st_ino == parent_inode:
                    contended.append(True)
                    release.set()
                raise

        try:
            self.assertTrue(wait_event(entered, 'seat_execution_epoch_race: entered synchronization'))
            with mock.patch("fcntl.flock", side_effect=release_on_native_contention):
                if first == "completion":
                    with self.assertRaises(ProtocolRefusal):
                        controller.callback(invocation, PAYLOAD)
                else:
                    self.work.complete(item["id"], "builder", [fixture.BINDING], now=fixture.NOW)
            self.assertTrue(contended, "second native operation did not contend on first car acceptance")
            self.assertTrue(wait_poll(receive, 'seat_execution_epoch_race: receive synchronization'))
            outcome, result = receive.recv()
            self.assertEqual("completed" if first == "completion" else "notified", outcome, result)
            events = EventLog(self.root).event_records()
            notices = [row for row in events if row["kind"] == "message_envelope"]
            executions = [row for row in events if row["kind"] == "seat_execution"]
            self.assertEqual(0 if first == "completion" else 1, len(notices))
            self.assertEqual(["running"] if first == "completion" else ["running", "ended"],
                [row["event"] for row in executions])
            if notices:
                self.assertEqual(notices[0]["id"], executions[-1]["envelope_id"])
            self.assertEqual("completed", self.work.show(item["id"])[0]["state"])
            self.assertEqual("NO_CAR", controller.project("builder")["state"])
        finally:
            release.set()
            child.join(12)
            receive.close()
        self.assertFalse(child.is_alive())
        self.assertEqual(0, child.exitcode)

    def test_completion_wins_actual_concurrent_notice_acceptance(self):
        self.race("completion")

    def test_notice_wins_actual_concurrent_completion_and_both_are_receipted(self):
        self.race("notice")

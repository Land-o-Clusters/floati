"""Current native gateway source cannot race a replacement wake claim."""
import time
import unittest
from unittest import mock

import tests
from floati.codex_wait_contract import CodexWaitConsentLedger, CodexWaitSessionLedger, resolve_participant
from floati.errors import ProtocolRefusal
from floati.events import EventLog
from floati.waiter_consent import arm_waiter_consent
from floati.waiter_lock_retry import waiter_lock_window
from tests import test_seat_execution as fixture


class SeatExecutionGatewayRaceTests(unittest.TestCase):
    setUp = fixture.SeatExecutionTests.setUp
    car = fixture.SeatExecutionTests.car
    claim = fixture.SeatExecutionTests.claim
    controller = fixture.SeatExecutionTests.controller

    def prepare_gateway(self):
        self.claim(self.car())
        arm_waiter_consent(self.root.path, self.workspace, "builder", "Codex", 1800, 1700)
        self.binding = resolve_participant(self.root.path, self.workspace).binding
        self.consent = CodexWaitConsentLedger(self.root).require_armed(self.binding)
        self.sessions = CodexWaitSessionLedger(self.root)
        self.sessions.arm(self.binding, self.consent, "runtime-old",
            idempotency_key="gateway-old", take_over=True)

    def bind(self, controller):
        return controller._bind_gateway("builder", "runtime-old", self.workspace,
            command="codex-fleet-bus profile inbox", source_pin="b" * 64)

    def test_ordinary_current_source_issues_native_running_testimony(self):
        self.prepare_gateway()
        invocation = self.bind(self.controller())
        self.assertIsNotNone(invocation)
        rows = EventLog(self.root).event_records()
        self.assertEqual(["running"], [row["event"] for row in rows if row["kind"] == "seat_execution"])
        self.assertEqual("runtime-old", rows[-1]["acting_session_id"])

    def test_replacement_winning_before_bind_refuses_old_runtime(self):
        self.prepare_gateway()
        self.sessions.arm(self.binding, self.consent, "runtime-new",
            idempotency_key="gateway-replacement", take_over=True)
        with self.assertRaises(ProtocolRefusal):
            self.bind(self.controller())
        self.assertEqual([], EventLog(self.root).event_records())

    def test_takeover_and_old_source_issue_cannot_both_succeed_at_issue_boundary(self):
        self.prepare_gateway()
        controller = self.controller()
        original_issue = controller._issue
        replacement = []
        issued = []

        def race_at_issue(**coordinates):
            try:
                replacement.append(self.sessions.arm(self.binding, self.consent,
                    "runtime-new", idempotency_key="gateway-racing-replacement", take_over=True))
            except ProtocolRefusal:
                pass
            result = original_issue(**coordinates)
            issued.append(result)
            return result

        # A held native node fence may refuse the synchronous replacement;
        # an actual replacement winning instead must refuse the old source.
        # Both successful actions are the forbidden outcome, not one ordering.
        with waiter_lock_window(0.08, time.monotonic, time.sleep):
            with mock.patch.object(controller, "_issue", side_effect=race_at_issue):
                try:
                    self.bind(controller)
                except ProtocolRefusal:
                    pass
        self.assertFalse(replacement and issued,
            "new native session and old gateway source were both accepted")
        self.assertTrue(replacement or issued or not EventLog(self.root).event_records())

"""Native scratch car ownership, callback admission and publication controls."""
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import tests
from floati.errors import ProtocolRefusal
from floati.events import EventLog
from floati.planes import AuthorityGrantStore
from floati.registry import Registry
from floati.root import FloatiRoot
from floati.work import WorkLog
from floati.ids import uuid7_hex

NOW = datetime(2026, 10, 4, tzinfo=timezone.utc)
BINDING = {"repo": "floati", "sha": "a" * 40, "doc": "docs/car.md"}
CAR = {"row": "F2-SEAT-STALL-VISIBLE-1", "repository": "floati", "branch": "codex/car"}


class SeatExecutionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = FloatiRoot.open_direct_home(Path(self.temp.name) / "fleet", create=True)
        self.workspace = Path(self.temp.name).resolve() / "workspace"
        self.workspace.mkdir()
        self.registry = Registry(self.root)
        self.registry.register("builder", "Codex")
        self.registry.register("architect", "Codex")
        self.registry.register("other", "Codex")
        self.work = WorkLog(self.root)
        self.grant = AuthorityGrantStore(self.root).claim("work-claims", "builder", 60, 60, NOW)

    def car(self, owner="builder"):
        return self.work.add("implementation", owner, [BINDING], car_binding=CAR, now=NOW)

    def claim(self, item):
        return self.work.claim(item["id"], "builder", "work-claims", self.grant["epoch"], now=NOW)

    def controller(self):
        from floati.seat_execution import SeatExecutionController
        return SeatExecutionController(self.root, recipient="architect")

    def invocation(self, controller, item, session=None):
        # An actual native worker start receipt authenticates this binding.
        from floati.workers import WorkerReceipts
        receipts = WorkerReceipts(self.root)
        session = session or "worker-" + uuid7_hex()
        receipts.append(session, item["id"], "builder", "codex", "claim", None, [], now=NOW)
        receipt = receipts.append(session, item["id"], "builder", "codex", "spawn", None, [], now=NOW)
        return controller.bind_worker(receipt, self.workspace)

    def gateway(self, controller, session):
        from floati.waiter_consent import arm_waiter_consent
        from floati.codex_wait_contract import resolve_participant, CodexWaitConsentLedger, CodexWaitSessionLedger
        arm_waiter_consent(self.root.path, self.workspace, "builder", "Codex", 1800, 1700)
        binding = resolve_participant(self.root.path, self.workspace).binding
        consent = CodexWaitConsentLedger(self.root).require_armed(binding)
        CodexWaitSessionLedger(self.root).arm(binding, consent, session,
            idempotency_key="gateway-" + session, take_over=True)
        return controller._bind_gateway("builder", session, self.workspace,
            command="codex-fleet-bus profile inbox", source_pin="b" * 64)

    def test_structured_car_uses_native_claim_and_banked_completion(self):
        item = self.car()
        claim = self.claim(item)
        projected = self.work.show(item["id"])[0]
        self.assertEqual(CAR, projected["car_binding"])
        self.assertEqual(claim["id"], projected["claim_id"])
        self.work.complete(item["id"], "builder", [BINDING], now=NOW)
        self.assertEqual("NO_CAR", self.controller().project("builder")["state"])

    def test_foreign_owner_cannot_start_car(self):
        item = self.car("other")
        with self.assertRaisesRegex(ProtocolRefusal, "owner"):
            self.claim(item)
        self.assertEqual("open", self.work.show(item["id"])[0]["state"])

    def test_no_claim_is_no_car_even_with_incoming_mail(self):
        self.car()
        EventLog(self.root).send("architect", "builder", **BINDING, note="new mail")
        state = self.controller().project("builder")
        self.assertEqual("NO_CAR", state["state"])
        self.assertIsNone(state["last_execution_activity"])

    def test_claim_without_execution_testimony_stays_unknown(self):
        self.claim(self.car())
        self.assertEqual("UNKNOWN", self.controller().project("builder")["state"])

    def test_native_worker_end_publishes_one_atomic_notice_and_replays(self):
        item = self.car(); self.claim(item)
        controller = self.controller()
        invocation = self.invocation(controller, item)
        payload = {"event": "ended", "code": "approval_required_unattended", "detail": "cancelled and cleaned"}
        first = controller.callback(invocation, payload)
        self.assertEqual("STALLED", controller.project("builder")["state"])
        self.assertEqual(first, controller.callback(invocation, payload))
        rows = EventLog(self.root).event_records()
        notice = first["notice"]["message"]
        self.assertEqual(1, sum(r["kind"] == "message_envelope" for r in rows))
        self.assertEqual(notice["id"], first["execution"]["envelope_id"])
        self.assertEqual(1, sum(r["kind"] == "send_readiness_observation" for r in rows))
        with self.assertRaises(ProtocolRefusal):
            controller.callback(invocation, dict(payload, detail="different"))

    def test_completed_car_rejects_fresh_callback_but_historical_replay_survives(self):
        item = self.car(); self.claim(item)
        controller = self.controller(); invocation = self.invocation(controller, item)
        payload = {"event": "ended", "code": "process_died", "detail": "cleaned"}
        accepted = controller.callback(invocation, payload)
        self.work.complete(item["id"], "builder", [BINDING], now=NOW)
        self.assertEqual(accepted, controller.callback(invocation, payload))
        with self.assertRaises(ProtocolRefusal):
            controller.callback(invocation, {"event": "unknown", "code": "cleanup_unknown", "detail": "survivors"})

    def test_completion_winning_suppresses_notice(self):
        item = self.car(); self.claim(item)
        controller = self.controller(); invocation = self.invocation(controller, item)
        self.work.complete(item["id"], "builder", [BINDING], now=NOW)
        with self.assertRaises(ProtocolRefusal):
            controller.callback(invocation, {"event": "ended", "code": "process_died", "detail": "cleaned"})
        self.assertEqual([], EventLog(self.root).records())

    def test_newer_running_invocation_vetoes_older_terminal(self):
        item = self.car(); self.claim(item)
        controller = self.controller(); older = self.invocation(controller, item)
        # Independent authenticated gateway execution may overlap the worker.
        newer = self.gateway(controller, "runtime-b")
        controller.callback(older, {"event": "ended", "code": "process_died", "detail": "cleaned"})
        self.assertEqual("RUNNING", controller.project("builder")["state"])
        self.assertIsNotNone(newer)

    def test_refused_action_is_not_whole_seat_stalled(self):
        item = self.car(); self.claim(item)
        controller = self.controller()
        invocation = self.gateway(controller, "runtime-a")
        result = controller.callback(invocation, {"event": "refused_action",
            "code": "CODEX_FLEET_BUS_APPROVAL_REQUIRED", "detail": "exact refusal BODY"})
        self.assertEqual("UNKNOWN", controller.project("builder")["state"])
        self.assertIn("exact refusal BODY", result["notice"]["message"]["note"])

    def test_closed_callback_cannot_authenticate_a_payload_or_change_coordinates(self):
        item = self.car(); self.claim(item)
        controller = self.controller(); invocation = self.invocation(controller, item)
        with self.assertRaises(ProtocolRefusal):
            controller.callback({"authenticated": True, "node": "builder"},
                {"event": "ended", "code": "process_died", "detail": "forged"})
        with self.assertRaises(ProtocolRefusal):
            controller.callback(invocation, {"event": "ended", "code": "process_died",
                "detail": "alias", "work_item_id": item["id"]})
        with self.assertRaises(ProtocolRefusal):
            controller.callback(invocation, {"event": "pending_approval", "code": "pending", "detail": "not native"})
        self.assertEqual([], EventLog(self.root).records())

    def test_failed_publication_supplies_no_successful_notice_testimony(self):
        item = self.car(); self.claim(item)
        controller = self.controller(); invocation = self.invocation(controller, item)
        before = EventLog(self.root).event_records()
        with mock.patch("floati.jsonl.os.fsync", side_effect=OSError("full disk")):
            with self.assertRaises(Exception):
                controller.callback(invocation, {"event": "ended", "code": "process_died", "detail": "cleaned"})
        # The native transaction rolls back failed publication, including its event.
        self.assertEqual(before, EventLog(self.root).event_records())

    def test_forged_native_receipt_does_not_bind(self):
        item = self.car(); self.claim(item)
        controller = self.controller()
        with self.assertRaises(ProtocolRefusal):
            controller.bind_worker({"id": "invented", "node_id": "builder"}, self.workspace)
        self.assertEqual([], EventLog(self.root).event_records())

"""Native dependency carry regressions; no existing test fixture imports."""
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest

from floati import bus_epoch
from floati.codex_wait_contract import (resolve_participant,
    CodexWaitConsentLedger, CodexWaitSessionLedger)
from floati.waiter_consent import arm_waiter_consent
from floati.cursor import SparseCursor
from floati.cursor_presentations import CursorPresentationLedger
from floati.epoch_carry import build_carry_plan, carry_dependency_witness
from floati.errors import IntegrityFailure, ProtocolRefusal
from floati.events import EventLog
from floati.framing import decode_frames, encode_frame
from floati.planes import AuthorityGrantStore
from floati.registry import Registry
from floati.root import FloatiRoot
from floati.wake_hold import WakeHoldController, wake_coordination_guard
from tests.temp_roots import REAL_TEMP_ROOT


class EpochCarryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.root = FloatiRoot.open_direct_home(self.base / "fleet-home", create=True)
        self.fixture_roll_sequence = 0
        for node in ("sender", "seat-a", "seat-b"):
            Registry(self.root).register(node, "Codex")
        AuthorityGrantStore(self.root).grant_exact("bus-epoch-roll", "sender", 1,
                                                   datetime.now(timezone.utc))

    def send(self, key, node="seat-a", **kw):
        return EventLog(self.root).send("sender", node, "floati", "a" * 40,
            "docs/evidence/carry.md", "native carry fixture", idempotency_key=key, **kw)

    def payloads(self):
        with bus_epoch.epoch_guard(self.root, exclusive=True):
            selected = bus_epoch._preflight_selected_members(self.root)
            return bus_epoch._read_selected_epoch_payloads(self.root, selected)

    def plan(self):
        with bus_epoch.epoch_guard(self.root, exclusive=True):
            return build_carry_plan(self.root, self.payloads())

    def automatic_roll(self):
        def planner(root, old):
            plan = build_carry_plan(root, old)
            return plan.live_payloads, {"trigger": "ack", "policy": {"max_bytes": 1,
                "max_age_days": 7, "source": "operator_declared"},
                "observed_bytes": next(len(raw) for name, _plane, raw in old if name == "events.jsonl"),
                "observed_at": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
                "epoch_started_at": None, "due_reasons": ["bytes"]}
        return bus_epoch.roll_automatic_epoch(self.root, actor="sender", planner=planner)

    def ids(self, plan, relative="events.jsonl"):
        data = dict((name, raw) for name, _plane, raw in plan.live_payloads).get(relative, b"")
        return [row["id"] for row in decode_frames(data)]

    def install_fixture_carry(self, payloads):
        """Install genuine native frames as the future transaction's target.

        Only fixture file placement is synthetic: all receipts, IDs and cuts
        come from product APIs. Transaction/fault correctness has its own tests.
        """
        self.fixture_roll_sequence += 1
        result = bus_epoch.roll_bus_epoch(self.root, actor="sender",
            idempotency_key="fixture-roll-" + str(self.fixture_roll_sequence))
        with bus_epoch.epoch_guard(self.root, exclusive=True):
            for relative, _plane, raw in payloads:
                path = self.root.path / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes((encode_frame(result["receipt"]) if relative == "events.jsonl" else b"") + raw)
        return Path(result["receipt"]["archive_path"])

    def hold_result(self, key="after-roll", worker=None):
        try:
            return WakeHoldController(self.root).evaluate("seat-a", idempotency_key=key,
                worker_session_id=worker)
        except (IntegrityFailure, ProtocolRefusal) as exc:
            return {"state": "refused", "code": exc.code}

    def presentation(self, *, flushed=False):
        workspace = self.base / "workspace"
        workspace.mkdir(exist_ok=True)
        arm_waiter_consent(self.root.path, workspace, "seat-a", "Codex", 10, 6)
        binding = resolve_participant(self.root.path, workspace).binding
        consent = CodexWaitConsentLedger(self.root).require_armed(binding)
        ledger = CodexWaitSessionLedger(self.root)
        ledger.arm(binding, consent, "conversation-a", idempotency_key="carry-session")
        classification = ledger.participate_classified(binding, consent, "conversation-a")
        plane = CursorPresentationLedger(self.root)
        with bus_epoch.epoch_guard(self.root), wake_coordination_guard(self.root, "seat-a"):
            selection = plane.select(binding, consent, classification, harness="codex")
            prepared = plane.prepare(selection, {"followup_message": "native fixture"})
            if flushed:
                plane.flushed(prepared, plane.deliver(prepared, selection))
        return binding, consent, classification, prepared

    def test_unread_envelope_is_carried_but_fully_acked_unrelated_history_is_not(self):
        old = self.send("finished", "seat-b")
        EventLog(self.root).present("seat-b")
        SparseCursor(self.root).ack("seat-b", [old["id"]], acting_session_id="finished")
        pending = self.send("unread")
        plan = self.plan()
        self.assertIn(pending["id"], self.ids(plan))
        self.assertNotIn(old["id"], self.ids(plan))
        self.install_fixture_carry(plan.live_payloads)
        self.assertEqual([pending["id"]], EventLog(self.root).unacked_ids("seat-a"))

    def test_whole_mixed_delivery_batch_retains_terminal_sibling_and_real_ack(self):
        first, second = self.send("completed-sibling"), self.send("pending-sibling")
        _rows, delivery = EventLog(self.root).present("seat-a")
        ack = SparseCursor(self.root).ack("seat-a", [first["id"]], acting_session_id="history")
        plan = self.plan()
        self.assertIn(first["id"], self.ids(plan))
        self.assertIn(second["id"], self.ids(plan))
        self.assertIn(delivery["id"], self.ids(plan, "receipts/deliveries/seat-a.jsonl"))
        self.assertIn(ack["id"], self.ids(plan, "receipts/acks/seat-a.jsonl"))
        self.install_fixture_carry(plan.live_payloads)
        self.assertEqual([second["id"]], EventLog(self.root).unacked_ids("seat-a"))

    def test_reply_carries_original_and_its_terminal_proof_without_resurrection(self):
        first = self.send("original")
        EventLog(self.root).present("seat-a")
        SparseCursor(self.root).ack("seat-a", [first["id"]], acting_session_id="original")
        reply = EventLog(self.root).send("seat-a", "sender", "floati", "a" * 40,
            "docs/evidence/carry.md", "native reply", reply_to=first["id"], idempotency_key="reply")
        plan = self.plan()
        self.assertLess(self.ids(plan).index(first["id"]), self.ids(plan).index(reply["id"]))
        self.install_fixture_carry(plan.live_payloads)
        self.assertEqual([], EventLog(self.root).unacked_ids("seat-a"))
        self.assertEqual([reply["id"]], EventLog(self.root).unacked_ids("sender"))

    def test_imported_hold_validates_original_cut_and_exact_retry_identity(self):
        message = self.send("held")
        before = WakeHoldController(self.root).evaluate("seat-a", idempotency_key="original-hold")
        plan = self.plan()
        self.assertIn(message["id"], self.ids(plan))
        self.install_fixture_carry(plan.live_payloads)
        from floati import cut_prefix, inbox_prefix, send_prefix
        self.assertIsNotNone(send_prefix.warm(self.root))
        self.assertIsNotNone(inbox_prefix.warm(self.root))
        self.assertIsNotNone(cut_prefix.warm(self.root))
        self.assertIsNone(cut_prefix.certified_source(self.root, "seat-a"),
            "a native roll head must keep imported-hold validation on native sources")
        after = self.hold_result()
        self.assertEqual("held_only", after["state"])
        retried = self.hold_result("original-hold")
        self.assertEqual(before, retried)

    def test_archive_tamper_cannot_be_hidden_by_a_previously_verified_hold(self):
        self.send("held")
        WakeHoldController(self.root).evaluate("seat-a", idempotency_key="original-hold")
        plan = self.plan()
        archive = self.install_fixture_carry(plan.live_payloads)
        self.assertEqual("held_only", self.hold_result()["state"])
        events = archive / "events.jsonl"
        events.write_bytes(events.read_bytes().replace(b"native carry fixture", b"native carry fixturf", 1))
        self.assertEqual("refused", self.hold_result()["state"])

    def test_prepared_only_reservation_remains_uncertain_in_same_conversation(self):
        message = self.send("prepared-only")
        binding, consent, classification, prepared = self.presentation()
        plan = self.plan()
        self.assertIn(message["id"], self.ids(plan))
        self.install_fixture_carry(plan.live_payloads)
        plane = CursorPresentationLedger(self.root)
        with bus_epoch.epoch_guard(self.root), wake_coordination_guard(self.root, "seat-a"):
            selection = plane.select(binding, consent, classification, harness="codex")
        self.assertEqual([], selection["fresh_messages"])
        self.assertEqual("presentation_uncertain", plane.held_evidence(selection)["state"])
        self.assertEqual([message["id"]], [item["item_id"] for item in selection["held_items"]])
        self.assertEqual(prepared["id"], plane.read("seat-a")[0]["id"])

    def test_flushed_reservation_is_not_republished_after_roll(self):
        message = self.send("flushed")
        binding, consent, classification, _prepared = self.presentation(flushed=True)
        plan = self.plan()
        self.install_fixture_carry(plan.live_payloads)
        plane = CursorPresentationLedger(self.root)
        with bus_epoch.epoch_guard(self.root), wake_coordination_guard(self.root, "seat-a"):
            selection = plane.select(binding, consent, classification, harness="codex")
        self.assertEqual([], selection["fresh_messages"])
        self.assertEqual("held_unacked", plane.held_evidence(selection)["state"])
        self.assertEqual([message["id"]], [item["item_id"] for item in selection["held_items"]])

    def test_second_roll_preserves_imported_hold_without_rebasing_its_hashes(self):
        message = self.send("held")
        before = WakeHoldController(self.root).evaluate("seat-a", idempotency_key="original-hold")
        first = self.plan()
        self.install_fixture_carry(first.live_payloads)
        second = self.plan()
        self.install_fixture_carry(second.live_payloads)
        self.assertEqual([message["id"]], EventLog(self.root).unacked_ids("seat-a"))
        self.assertEqual(before, self.hold_result("original-hold"))

    def test_malformed_selected_source_refuses_instead_of_planning_around_it(self):
        self.send("unread")
        original = self.payloads()
        broken = tuple((relative, plane, raw + b'{"partial":') if relative == "events.jsonl"
                       else (relative, plane, raw) for relative, plane, raw in original)
        with self.assertRaises((IntegrityFailure, ProtocolRefusal)):
            with bus_epoch.epoch_guard(self.root, exclusive=True):
                build_carry_plan(self.root, broken)

    def test_exact_carried_execution_history_does_not_duplicate_native_invocation(self):
        from floati.work import WorkLog
        from floati.workers import WorkerReceipts
        from floati.seat_execution import SeatExecutionController, _history
        from floati.ids import uuid7_hex
        now = datetime.now(timezone.utc)
        work = WorkLog(self.root)
        artifact = {"repo": "floati", "sha": "a" * 40, "doc": "docs/evidence/carry.md"}
        item = work.add("carry native invocation", "seat-a", [artifact],
            car_binding={"row": "BUS-AUTO-ROTATE-1", "repository": "floati", "branch": "codex/carry"}, now=now)
        grant = AuthorityGrantStore(self.root).claim("work-claims", "seat-a", 60, 60, now)
        work.claim(item["id"], "seat-a", "work-claims", grant["epoch"], now=now)
        workers = WorkerReceipts(self.root)
        session = "worker-" + uuid7_hex()
        workers.append(session, item["id"], "seat-a", "codex", "claim", None, [], now=now)
        spawn = workers.append(session, item["id"], "seat-a", "codex", "spawn", None, [], now=now)
        workspace = self.base / "worker-workspace"
        workspace.mkdir()
        controller = SeatExecutionController(self.root, recipient="sender")
        invocation = controller.bind_worker(spawn, workspace)
        plan = self.plan()
        self.install_fixture_carry(plan.live_payloads)
        self.assertEqual(1, sum(row["kind"] == "seat_execution" for row in _history(self.root)))
        path = self.root.tenant_home / "events.jsonl"
        original = path.read_bytes()
        forged = [dict(row, timestamp="2026-10-14T00:00:00.000Z")
                  if row["kind"] == "seat_execution" else row for row in decode_frames(original)]
        try:
            path.write_bytes(b"".join(encode_frame(row) for row in forged))
            with self.assertRaises(IntegrityFailure):
                _history(self.root)
        finally:
            path.write_bytes(original)
        closed = controller.callback(invocation, {"event": "ended", "code": "process_died", "detail": "owned child collected"})
        self.assertEqual("ended", closed["execution"]["event"])

    def test_noop_witness_invalidates_on_ack_and_unselected_hold_membership(self):
        message = self.send("witness")
        EventLog(self.root).present("seat-a")
        before = carry_dependency_witness(self.root)
        self.assertIsNotNone(before)
        self.assertEqual(before, carry_dependency_witness(self.root))
        SparseCursor(self.root).ack("seat-a", [message["id"]], acting_session_id="witness")
        after_ack = carry_dependency_witness(self.root)
        self.assertNotEqual(before, after_ack)
        # The conservative witness covers namespace membership independently
        # of which typed plane the carry planner currently projects.
        hold_directory = self.root.tenant_home / "receipts" / "stop-holds"
        hold_directory.mkdir(exist_ok=True)
        self.assertNotEqual(after_ack, carry_dependency_witness(self.root))

    def test_ack_then_automatic_roll_allows_exact_consumption_and_replay(self):
        from floati.message_consumption import MessageConsumption
        message = self.send("consume-after-auto")
        EventLog(self.root).present("seat-a")
        SparseCursor(self.root).ack("seat-a", [message["id"]], acting_session_id="acting")
        self.automatic_roll()
        self.send("next-epoch", "seat-b")
        self.automatic_roll()
        self.assertNotIn(message["id"], [row["id"] for row in EventLog(self.root).records()])
        consumption = MessageConsumption(self.root)
        accepted = consumption.consume("seat-a", message["id"], "acting")
        self.assertEqual(message["id"], accepted["message_id"])
        self.assertEqual(accepted, consumption.consume("seat-a", message["id"], "acting"))

    def test_ack_then_automatic_roll_allows_work_complete_consumption(self):
        from floati.work import WorkLog
        message = self.send("work-after-auto")
        EventLog(self.root).present("seat-a")
        SparseCursor(self.root).ack("seat-a", [message["id"]], acting_session_id="acting")
        work = WorkLog(self.root)
        now = datetime.now(timezone.utc)
        artifact = {"repo": "floati", "sha": "a" * 40, "doc": "docs/evidence/carry.md"}
        item = work.add("act then complete", "seat-a", [artifact], now=now)
        grant = AuthorityGrantStore(self.root).claim("work-claims", "seat-a", 60, 60, now)
        work.claim(item["id"], "seat-a", "work-claims", grant["epoch"], now=now)
        self.automatic_roll()
        work.complete(item["id"], "seat-a", [artifact], consume_ids=[message["id"]], session_id="acting")
        self.assertEqual("completed", work.show(item["id"])[0]["state"])

    def test_manual_archive_boundary_still_refuses_new_consumption(self):
        from floati.message_consumption import MessageConsumption
        message = self.send("manual-consume-boundary")
        EventLog(self.root).present("seat-a")
        SparseCursor(self.root).ack("seat-a", [message["id"]], acting_session_id="acting")
        bus_epoch.roll_bus_epoch(self.root, actor="sender", idempotency_key="manual-boundary")
        self.send("post-manual", "seat-b")
        self.automatic_roll()
        with self.assertRaises(ProtocolRefusal) as refusal:
            MessageConsumption(self.root).consume("seat-a", message["id"], "acting")
        self.assertEqual("consume_item_archived", refusal.exception.code)

    def test_unsealed_imported_hold_mapping_cannot_authorize_projection(self):
        from floati.wake_hold import project_wake_items
        self.send("held")
        WakeHoldController(self.root).evaluate("seat-a", idempotency_key="native-hold")
        source = WakeHoldController(self.root)._read("seat-a", None)
        with self.assertRaises(IntegrityFailure):
            project_wake_items(events=source[0], deliveries=source[2], acknowledgments=source[4],
                recipient="seat-a", worker_session_id=None, tenant_id=self.root.tenant_id,
                event_prefix_digests=source[1], delivery_prefix_digests=source[3],
                acknowledgment_prefix_digests=source[5], _hold_proofs={})

    def test_all_worker_planes_keep_independent_held_eligibility(self):
        from floati.ids import uuid7_hex
        before = {}
        for _index in range(2):
            worker = "worker-" + uuid7_hex()
            self.send("mail-" + worker, worker_session_id=worker)
            before[worker] = WakeHoldController(self.root).evaluate("seat-a",
                idempotency_key="hold-" + worker, worker_session_id=worker)
        self.automatic_roll()
        for worker, original in before.items():
            controller = WakeHoldController(self.root)
            self.assertEqual("held_only", controller.evaluate("seat-a",
                idempotency_key="after-" + worker, worker_session_id=worker)["state"])
            self.assertEqual(original, controller.evaluate("seat-a",
                idempotency_key="hold-" + worker, worker_session_id=worker))

    def test_new_presentation_cut_with_imported_legacy_hold_loads_natively(self):
        self.send("legacy-held")
        WakeHoldController(self.root).evaluate("seat-a", idempotency_key="legacy-hold")
        self.automatic_roll()
        self.send("new-mail-after-carry")
        binding, consent, classification, prepared = self.presentation()
        plane = CursorPresentationLedger(self.root)
        self.assertEqual(prepared["id"], plane.read("seat-a")[0]["id"])
        with bus_epoch.epoch_guard(self.root), wake_coordination_guard(self.root, "seat-a"):
            selection = plane.select(binding, consent, classification, harness="codex")
        self.assertEqual([], selection["fresh_messages"])

    def test_new_flushed_cut_with_imported_legacy_hold_loads_natively(self):
        self.send("legacy-held")
        WakeHoldController(self.root).evaluate("seat-a", idempotency_key="legacy-hold")
        self.automatic_roll()
        self.send("new-mail-after-carry")
        _binding, _consent, _classification, prepared = self.presentation(flushed=True)
        rows = CursorPresentationLedger(self.root).read("seat-a")
        self.assertEqual(2, len(rows))
        self.assertEqual(prepared["id"], rows[0]["id"])
        self.assertEqual("flushed", rows[1]["phase"])

    def test_same_id_changed_envelope_cannot_borrow_imported_hold_cut(self):
        message = self.send("legacy-held")
        WakeHoldController(self.root).evaluate("seat-a", idempotency_key="legacy-hold")
        self.automatic_roll()
        path = self.root.tenant_home / "events.jsonl"
        frames = decode_frames(path.read_bytes())
        path.write_bytes(b"".join(encode_frame(dict(row, note="changed native payload")
            if row["id"] == message["id"] else row) for row in frames))
        self.assertEqual("refused", self.hold_result("same-id-changed")["state"])

    def test_unissued_proof_subclass_cannot_override_native_admission(self):
        from floati.epoch_carry import _VerifiedHoldProofs
        from floati.wake_hold import project_wake_items
        class UnissuedProof(_VerifiedHoldProofs):
            def __init__(self):
                pass
            def assert_source(self, *_arguments):
                pass
            def validates(self, _receipt):
                return True
        self.send("legacy-held")
        WakeHoldController(self.root).evaluate("seat-a", idempotency_key="legacy-hold")
        self.automatic_roll()
        source = WakeHoldController(self.root)._read("seat-a", None)
        with self.assertRaises(IntegrityFailure):
            project_wake_items(events=source[0], deliveries=source[2], acknowledgments=source[4],
                recipient="seat-a", worker_session_id=None, tenant_id=self.root.tenant_id,
                event_prefix_digests=source[1], delivery_prefix_digests=source[3],
                acknowledgment_prefix_digests=source[5], _hold_proofs=UnissuedProof())

    def _changed_presentation_attempt_refuses(self, *, flushed):
        message = self.send("original-presentation")
        binding, consent, classification, _prepared = self.presentation(flushed=flushed)
        self.automatic_roll()
        added = self.send("distinct-new-envelope")
        plane = CursorPresentationLedger(self.root)
        with bus_epoch.epoch_guard(self.root), wake_coordination_guard(self.root, "seat-a"):
            selection = plane.select(binding, consent, classification, harness="codex")
        self.assertEqual([added["id"]], [row["id"] for row in selection["fresh_messages"]])
        path = self.root.tenant_home / "events.jsonl"
        frames = decode_frames(path.read_bytes())
        path.write_bytes(b"".join(encode_frame(dict(row, note="changed carried presentation payload")
            if row["id"] == message["id"] else row) for row in frames))
        with self.assertRaises((IntegrityFailure, ProtocolRefusal)):
            with bus_epoch.epoch_guard(self.root), wake_coordination_guard(self.root, "seat-a"):
                plane.select(binding, consent, classification, harness="codex")

    def test_prepared_attempt_cannot_borrow_same_id_changed_carried_envelope(self):
        self._changed_presentation_attempt_refuses(flushed=False)

    def test_flushed_attempt_cannot_borrow_same_id_changed_carried_envelope(self):
        self._changed_presentation_attempt_refuses(flushed=True)

    def _malformed_native_hold_keeps_refusal(self, *, anchored=False, duplicate=False):
        from floati.ids import uuid7_hex
        from floati.records import wake_hold_decision_digest
        self.send("original-local-hold")
        receipt = WakeHoldController(self.root).evaluate("seat-a",
            idempotency_key="original-local-hold")["receipt"]
        if anchored:
            self.automatic_roll()
            self.send("fresh-after-native-roll")
            receipt = WakeHoldController(self.root).evaluate("seat-a",
                idempotency_key="new-local-hold")["receipt"]
        path = self.root.tenant_home / "receipts/deliveries/seat-a.jsonl"
        rows = decode_frames(path.read_bytes())
        hostile = dict(receipt)
        if duplicate:
            hostile["id"] = "wake-hold-" + uuid7_hex()
            hostile["decision_digest"] = wake_hold_decision_digest(hostile)
            rows.append(hostile)
        else:
            hostile["event_prefix_digest"] = "f" * 64
            hostile["decision_digest"] = wake_hold_decision_digest(hostile)
            rows = [hostile if row["id"] == receipt["id"] else row for row in rows]
        path.write_bytes(b"".join(encode_frame(row) for row in rows))
        with self.assertRaises(IntegrityFailure) as caught:
            WakeHoldController(self.root).evaluate("seat-a", idempotency_key="later-local-hold")
        self.assertEqual("consumption_state_unavailable", caught.exception.code)

    def test_unanchored_forged_native_hold_digest_keeps_native_refusal(self):
        self._malformed_native_hold_keeps_refusal()

    def test_unanchored_duplicate_native_hold_keeps_native_refusal(self):
        self._malformed_native_hold_keeps_refusal(duplicate=True)

    def test_new_local_hold_corruption_after_carry_keeps_native_refusal(self):
        self._malformed_native_hold_keeps_refusal(anchored=True)

"""Selected consumption joins preserve complete native ACK-plane validation."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from floati import cursor_prefix, jsonl, send_prefix
from floati.cursor import SparseCursor
from floati.errors import IntegrityFailure
from floati.events import EventLog
from floati.ids import uuid7_hex
from floati.key_prefix import KeyPrefix
from floati.message_consumption import MessageConsumption
from floati.registry import Registry
from floati.root import FloatiRoot
from tests.bus_scale_io import IOProbe
from tests.temp_roots import REAL_TEMP_ROOT
from tests.bus_scale_test_profile import StableSourceFixture
from tests.bus_scale_test_profile import stable_fixture_sources
from tests.bus_scale_prefix_fixture import shared_prefix_baseline


class SelectedConsumptionCursorTests(StableSourceFixture, unittest.TestCase):
    @stable_fixture_sources()
    @shared_prefix_baseline()
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
        self.addCleanup(temporary.cleanup)
        self.root = FloatiRoot.open_direct_home(Path(temporary.name) / "fleet", create=True)
        for node in ("sender", "recipient", "other"):
            Registry(self.root).register(node, "Codex")
        self.events = EventLog(self.root)
        self.cursor = SparseCursor(self.root)

    def send(self, *, worker=None):
        return self.events.send("sender", "recipient", "floati", "a" * 40,
            "docs/selected-consumption.md", "native selected ACK join",
            idempotency_key="selected-" + uuid7_hex(), worker_session_id=worker)["message"]

    def acknowledge(self, message, *, session="seen"):
        worker = message.get("worker_session_id")
        self.events.present("recipient", worker_session_id=worker)
        return self.cursor.ack("recipient", [message["id"]],
            acting_session_id=session, worker_session_id=worker)

    def selected(self, message):
        method = getattr(SparseCursor(self.root), "ack_references_for", None)
        self.assertTrue(callable(method),
            "current consumption needs a selected native ACK join within the cursor prefix")
        return method("recipient", message)

    def assert_no_history_read(self, probe):
        probe.assert_complete()
        for relative in ("events.jsonl", "receipts/acks/recipient.jsonl",
                         "receipts/deliveries/recipient.jsonl"):
            self.assertEqual(0, probe.read_bytes_by_path.get(relative, 0),
                "selected join replayed certified authoritative history: " + relative)

    def test_fresh_selected_join_reads_no_certified_event_delivery_or_ack_history(self):
        message = self.send()
        acknowledgment = self.acknowledge(message)
        self.assertEqual([acknowledgment], self.selected(message))
        with IOProbe(self.root.tenant_home) as probe:
            self.assertEqual([acknowledgment], self.selected(message))
        self.assert_no_history_read(probe)

    def test_native_source_appends_preserve_selected_join_without_history_replay(self):
        first = self.send()
        first_ack = self.acknowledge(first)
        self.assertEqual([first_ack], self.selected(first))
        second = self.send()
        second_ack = self.acknowledge(second, session="second-seen")
        with IOProbe(self.root.tenant_home) as probe:
            self.assertEqual([first_ack], self.selected(first))
            self.assertEqual([second_ack], self.selected(second))
        self.assert_no_history_read(probe)

    def test_candidate_selects_the_first_physical_ack_and_returns_detached_data(self):
        message = self.send()
        first = self.acknowledge(message, session="first-seen")
        second = self.acknowledge(message, session="second-seen")
        full = MessageConsumption(self.root)._ack_reference(
            "recipient", message, self.events.event_records())
        self.assertEqual([first, second], full)
        selected = self.selected(message)
        self.assertEqual([first], selected)
        selected[0]["item_ids"].append("unowned-mutated-data")
        self.assertEqual([first], self.selected(message))

    def test_selected_batch_keeps_all_native_item_ids_and_their_envelope_joins(self):
        first = self.send()
        self.events.present("recipient")
        second = self.send()
        self.events.present("recipient")
        acknowledgment = self.cursor.ack("recipient", [first["id"], second["id"]],
            acting_session_id="batch-seen")
        self.assertEqual([acknowledgment], self.selected(first))
        with IOProbe(self.root.tenant_home) as probe:
            for message in (first, second):
                selected = self.selected(message)
                self.assertEqual([acknowledgment], selected)
                self.assertEqual([first["id"], second["id"]], selected[0]["item_ids"])
        self.assert_no_history_read(probe)

    def test_unrequested_historical_ack_with_no_delivery_still_refuses(self):
        message = self.send()
        acknowledgment = self.acknowledge(message)
        self.selected(message)
        forged = dict(acknowledgment, id="ack-" + uuid7_hex(),
            item_ids=["msg-" + uuid7_hex()])
        jsonl.append_record(self.root, "receipts/acks/recipient.jsonl", forged,
            allowed_kinds={"ack_receipt"})
        with self.assertRaises(IntegrityFailure) as caught:
            self.selected(message)
        self.assertEqual("ack_evidence_invalid", caught.exception.code)

    def test_same_size_delivery_perturbation_revokes_the_join(self):
        message = self.send()
        self.acknowledge(message)
        self.selected(message)
        path = self.cursor.delivery_path_for("recipient")
        before = path.stat()
        raw = path.read_bytes()
        changed = raw.replace(b'"recipient":"recipient"', b'"recipient":"recipienx"', 1)
        self.assertNotEqual(raw, changed)
        self.assertEqual(len(raw), len(changed))
        path.write_bytes(changed)
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
        with self.assertRaises(IntegrityFailure) as caught:
            self.selected(message)
        self.assertEqual("delivery_evidence_invalid", caught.exception.code)

    def test_missing_checkpoint_and_selected_entry_rebuild_the_complete_native_plane(self):
        message = self.send()
        acknowledgment = self.acknowledge(message)
        self.assertEqual([acknowledgment], self.selected(message))
        relative = self.cursor._relative_path_for("recipient").as_posix()
        bank = self.root.tenant_home / (".cursor-prefix-" + hashlib.sha256(relative.encode()).hexdigest())
        lookup = send_prefix._key("message", message["id"])
        complete = send_prefix._key("consumption", "complete")
        for target in (bank / "checkpoint.json", bank / "nodes" / KeyPrefix.filename(lookup),
                       bank / "nodes" / KeyPrefix.filename(complete)):
            with self.subTest(target=target.name):
                self.assertTrue(target.is_file(), "selected ACK join lacks its certified proof node")
                target.unlink()
                with IOProbe(self.root.tenant_home) as probe:
                    self.assertEqual([acknowledgment], self.selected(message))
                probe.assert_complete()
                self.assertGreater(probe.read_bytes_by_path.get(relative, 0), 0,
                    "missing selected proof must replay native history, never prove absence")

    def test_identical_delivery_replacement_invalidates_prior_ack_join_generation(self):
        message = self.send()
        acknowledgment = self.acknowledge(message)
        self.selected(message)
        path = self.cursor.delivery_path_for("recipient")
        replacement = path.with_suffix(".replacement")
        replacement.write_bytes(path.read_bytes())
        replacement.replace(path)
        with IOProbe(self.root.tenant_home) as probe:
            self.assertEqual([acknowledgment], self.selected(message))
        probe.assert_complete()
        self.assertGreater(probe.read_bytes_by_path.get("receipts/deliveries/recipient.jsonl", 0), 0)
        self.assertGreater(probe.read_bytes_by_path.get("receipts/acks/recipient.jsonl", 0), 0,
            "a replaced delivery source must revoke old ACK-join certification")

    def test_plain_ack_never_authorizes_a_worker_plane_candidate(self):
        plain = self.send()
        self.acknowledge(plain)
        worker = self.send(worker="exact-worker")
        self.assertEqual([], self.selected(worker))
        worker_ack = self.acknowledge(worker, session="worker-seen")
        self.assertEqual([worker_ack], self.selected(worker))

    def test_hold_only_ack_does_not_become_real_delivery_consumption_evidence(self):
        from floati.wake_hold import WakeHoldController
        message = self.send()
        held = WakeHoldController(self.root).evaluate("recipient", idempotency_key="selected-hold")
        self.assertIn(message["id"], [row["id"] for row in held["fresh_messages"]])
        self.cursor.ack("recipient", [message["id"]], acting_session_id="held-seen")
        with self.assertRaises(IntegrityFailure) as native:
            MessageConsumption(self.root)._ack_reference(
                "recipient", message, self.events.event_records())
        with self.assertRaises(IntegrityFailure) as selected:
            self.selected(message)
        self.assertEqual("consumption_evidence_invalid", native.exception.code)
        self.assertEqual(native.exception.code, selected.exception.code,
            "a controller hold can be acknowledged but cannot replace an actual delivery join")

    def test_unrelated_hold_only_ack_preserves_the_complete_native_consumption_refusal(self):
        from floati.wake_hold import WakeHoldController
        held_message = self.send()
        held = WakeHoldController(self.root).evaluate(
            "recipient", idempotency_key="unrelated-selected-hold")
        self.assertEqual([held_message["id"]],
            [row["id"] for row in held["fresh_messages"]])
        held_ack = self.cursor.ack("recipient", [held_message["id"]],
            acting_session_id="unrelated-held-seen")
        self.assertEqual([held_message["id"]], held_ack["item_ids"],
            "generic ACK must continue accepting a native controller hold")
        candidate = self.send()
        self.acknowledge(candidate, session="real-delivery-seen")
        frames = self.events.event_records(sealed=True)
        _presented, acknowledged = self.cursor.state_for(
            "recipient", frames, sealed=True)
        self.assertEqual({held_message["id"], candidate["id"]}, set(acknowledged))
        with self.assertRaises(IntegrityFailure) as native:
            MessageConsumption(self.root)._ack_reference(
                "recipient", candidate, frames, sealed=True)
        self.assertEqual("consumption_evidence_invalid", native.exception.code)
        with self.assertRaises(IntegrityFailure) as selected:
            self.selected(candidate)
        self.assertEqual(native.exception.code, selected.exception.code,
            "selecting another ACK cannot erase the native whole-plane delivery refusal")

    def hold_then_candidate(self):
        from floati.wake_hold import WakeHoldController
        held_message = self.send()
        WakeHoldController(self.root).evaluate("recipient", idempotency_key="repair-selected-hold")
        self.cursor.ack("recipient", [held_message["id"]], acting_session_id="held-seen")
        candidate = self.send()
        acknowledgment = self.acknowledge(candidate, session="candidate-seen")
        with self.assertRaises(IntegrityFailure) as caught:
            self.selected(candidate)
        self.assertEqual("consumption_evidence_invalid", caught.exception.code)
        return held_message, candidate, acknowledgment

    def test_later_real_delivery_rebuilds_a_false_consumption_certificate(self):
        held, candidate, acknowledgment = self.hold_then_candidate()
        self.events._write_delivery_receipt("recipient", [held])
        with IOProbe(self.root.tenant_home) as probe:
            self.assertEqual([acknowledgment], self.selected(candidate))
        probe.assert_complete()
        self.assertGreater(probe.read_bytes_by_path.get("receipts/acks/recipient.jsonl", 0), 0,
            "a false certificate must replay the complete ACK join before becoming true")
        with IOProbe(self.root.tenant_home) as probe:
            self.assertEqual([acknowledgment], self.selected(candidate))
        self.assert_no_history_read(probe)

    def test_native_warm_refreshes_false_consumption_testimony_after_real_delivery(self):
        held, candidate, acknowledgment = self.hold_then_candidate()
        self.events._write_delivery_receipt("recipient", [held])
        self.assertIsNotNone(cursor_prefix.warm(self.root, "recipient"))
        with IOProbe(self.root.tenant_home) as probe:
            self.assertEqual([acknowledgment], self.selected(candidate))
        self.assert_no_history_read(probe)

    def test_full_state_for_keeps_native_validation_and_does_not_call_selected_api(self):
        message = self.send()
        self.acknowledge(message)
        self.selected(message)
        with patch.object(SparseCursor, "ack_references_for", side_effect=AssertionError("selected shortcut")):
            with IOProbe(self.root.tenant_home) as probe:
                _presented, acknowledged = self.cursor.state_for(
                    "recipient", self.events.event_records(), sealed=True)
        probe.assert_complete()
        self.assertIn(message["id"], acknowledged)
        self.assertGreater(probe.read_bytes_by_path.get("receipts/acks/recipient.jsonl", 0), 0)

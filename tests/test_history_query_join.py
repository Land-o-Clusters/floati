"""Native retained suffix joins; no consumer or sealed-proof migration."""
import tests

from pathlib import Path
import stat
import unittest
from unittest import mock

from floati import archive_catalog, archive_survivor_admission, bus_epoch, epoch_history
from floati.epoch_carry import ArchiveProofResolver, carry_dependency_witness
from floati.errors import IntegrityFailure, ProtocolRefusal
from floati.events import EventLog
from floati.cursor import SparseCursor
from floati.framing import decode_frames, encode_frame
from floati.wake_hold import WakeHoldController
from tests import test_epoch_carry as native_fixture


class HistoryQueryJoinTests(unittest.TestCase):
    def setUp(self):
        self.native = native_fixture.EpochCarryTests()
        self.native.setUp()
        self.native.send("retained-history-original")
        bus_epoch.roll_bus_epoch(self.native.root, actor="sender", idempotency_key="retained-history-original-roll")
        self.addCleanup(self.native.doCleanups)
        self.root = self.native.root
        self.message = self.native.send("join-live")


    def context(self):
        try:
            return epoch_history.open_history(self.root)
        except ProtocolRefusal as failure:
            self.fail("native retained generation has no joined query context: " + failure.code)

    def assert_native_sources(self, context):
        resolver = ArchiveProofResolver(self.root)
        self.assertEqual(resolver.source(self.root.tenant_home, "seat-a"),
                         context.source(context.live_coordinate, "seat-a"))
        for epoch, path in zip(context.epochs, resolver.archives()):
            self.assertEqual(resolver.source(path, "seat-a"),
                             context.source(epoch.coordinate, "seat-a"))

    def test_native_commit_joins_unchanged_retained_lineage(self):
        context = self.context()
        self.assertIsNone(context.generation)
        self.assertEqual(("live",), context.live_coordinate)
        self.assertEqual(1, len(context.epochs))
        self.assert_native_sources(context)

    def test_postcommit_send_extends_the_committed_live_prefix(self):
        added = self.native.send("join-later")
        context = self.context()
        source = context.source(context.live_coordinate, "seat-a")
        self.assertIn(added["id"], [row["id"] for row in source[0]])
        self.assertIn(self.message["id"], [row["id"] for row in source[0]])
        self.assert_native_sources(context)

    def test_empty_committed_delivery_plane_can_become_nonempty(self):
        _rows, receipt = EventLog(self.root).present("seat-a")
        context = self.context()
        source = context.source(context.live_coordinate, "seat-a")
        self.assertEqual([receipt["id"]], [row["id"] for row in source[2]])
        self.assert_native_sources(context)

    def test_empty_committed_ack_plane_can_become_nonempty(self):
        EventLog(self.root).present("seat-a")
        ack = SparseCursor(self.root).ack("seat-a", [self.message["id"]], acting_session_id="join-ack")
        context = self.context()
        self.assertEqual([ack["id"]], [row["id"] for row in
            context.source(context.live_coordinate, "seat-a")[4]])
        self.assert_native_sources(context)

    def test_automatic_roll_rehomes_former_live_by_native_owner_and_anchor(self):
        receipt = self.native.automatic_roll()["receipt"]
        context = self.context()
        self.assertEqual(2, len(context.epochs))
        self.assertEqual(receipt, context.epochs[0].receipt)
        self.assertEqual("automatic", context.epochs[0].boundary)
        self.assert_native_sources(context)

    def test_manual_roll_preserves_boundary_and_full_old_source_testimony(self):
        EventLog(self.root).present("seat-a")
        ack = SparseCursor(self.root).ack("seat-a", [self.message["id"]], acting_session_id="before-manual")
        receipt = bus_epoch.roll_bus_epoch(self.root, actor="sender", idempotency_key="join-manual")["receipt"]
        context = self.context()
        epoch = context.epochs[0]
        self.assertEqual(receipt, epoch.receipt)
        self.assertEqual("manual", epoch.boundary)
        self.assertIn(ack["id"], [row["id"] for row in context.source(epoch.coordinate, "seat-a")[4]])
        self.assert_native_sources(context)

    def test_multiple_native_rolls_join_committed_suffix_once(self):
        self.native.automatic_roll()
        self.native.automatic_roll()
        bus_epoch.roll_bus_epoch(self.root, actor="sender", idempotency_key="join-last-manual")
        context = self.context()
        coordinates = [epoch.coordinate for epoch in context.epochs]
        self.assertEqual(4, len(coordinates))
        self.assertEqual(4, len(set(coordinates)))
        self.assert_native_sources(context)

    def test_join_preserves_exact_sources_with_hold_and_completed_presentation(self):
        self.native.presentation(flushed=True)
        self.native.send("join-held")
        WakeHoldController(self.root).evaluate("seat-a", idempotency_key="join-original-hold")
        context = self.context()
        self.assert_native_sources(context)
        data = context.source(context.live_coordinate, "seat-a")
        data[0].clear()
        self.assertTrue(context.source(context.live_coordinate, "seat-a")[0])



    def test_missing_retained_member_is_integrity_failure_never_expired(self):
        context = self.context()
        path = Path(context.epochs[-1].receipt["archive_path"]) / "events.jsonl"
        path.unlink()
        with self.assertRaises(IntegrityFailure):
            epoch_history.open_history(self.root)





class HistoryQueryJoinReaderIdentityTests(unittest.TestCase):
    """Legacy source reads need their own reader seal without cold DATA."""
    def setUp(self):
        self.native = native_fixture.EpochCarryTests()
        self.native.setUp()
        self.native.send("retained-history-original")
        bus_epoch.roll_bus_epoch(self.native.root, actor="sender", idempotency_key="retained-history-original-roll")
        self.addCleanup(self.native.doCleanups)
        self.root = self.native.root
        self.native.send("source-reader-original")
        self.context = epoch_history.open_history(self.root)
        self.coordinate = self.context.live_coordinate
        self.assertIsNone(self.context.generation)
        self.assertTrue(self.context.source(self.coordinate, "seat-a")[0])

    def refuses_before_call(self, owner, name):
        calls = []
        def replacement(*args, **kwargs):
            calls.append(True)
            raise AssertionError("substituted source reader was invoked")
        with mock.patch.object(owner, name, replacement):
            with self.assertRaises(IntegrityFailure):
                self.context.source(self.coordinate, "seat-a")
        self.assertEqual([], calls)

    def test_source_cursor_alias_replacement_refuses_before_invocation(self):
        from floati import history_query_join
        self.refuses_before_call(history_query_join, "SparseCursor")

    def test_source_frame_encoder_alias_refuses_before_invocation(self):
        from floati import epoch_carry
        self.refuses_before_call(epoch_carry, "encode_frame")

    def test_source_decoder_budget_change_invalidates_old_context(self):
        from floati import epoch_carry
        with mock.patch.object(epoch_carry, "MAX_LEDGER_BYTES", epoch_carry.MAX_LEDGER_BYTES + 1):
            with self.assertRaises(IntegrityFailure):
                self.context.source(self.coordinate, "seat-a")

    def test_source_work_charger_replacement_refuses_before_invocation(self):
        from floati import history_query_join
        self.refuses_before_call(history_query_join._Work, "charge")

    def test_source_result_budget_change_invalidates_old_context(self):
        with mock.patch.object(epoch_history, "MAX_QUERY_RESULT_BYTES", epoch_history.MAX_QUERY_RESULT_BYTES + 1):
            with self.assertRaises(IntegrityFailure):
                self.context.source(self.coordinate, "seat-a")


class HistoryQueryJoinReviewTests(unittest.TestCase):
    def setUp(self):
        self.native = native_fixture.EpochCarryTests()
        self.native.setUp()
        self.native.send("retained-history-original")
        bus_epoch.roll_bus_epoch(self.native.root, actor="sender", idempotency_key="retained-history-original-roll")
        self.addCleanup(self.native.doCleanups)
        self.root = self.native.root
        self.native.send("review-source-original")

    def domain_change(self, name):
        context = epoch_history.open_history(self.root)
        self.assertIsNone(context.generation)
        coordinate, token = context.live_coordinate, context.dependency_token
        before = context.source(coordinate, "seat-a")
        # Physical identity and native inputs are unchanged. A changed source
        # domain must invalidate, rather than returning new prefix axes.
        with mock.patch.object(WakeHoldController, name, getattr(WakeHoldController, name) + "-changed"):
            with self.assertRaises(IntegrityFailure):
                context.source(coordinate, "seat-a")
        self.assertEqual(token, context.dependency_token)
        self.assertEqual(before, context.source(coordinate, "seat-a"))

    def test_legacy_event_domain_change_invalidates_source(self):
        cases = ("_EVENT_DOMAIN", "_DELIVERY_DOMAIN", "_ACK_DOMAIN")
        for domain in cases:
            with self.subTest(domain=domain):
                self.domain_change(domain)

    def test_legacy_controller_alias_refuses_before_domain_lookup(self):
        from floati import history_query_join
        context = epoch_history.open_history(self.root)
        coordinate, lookups = context.live_coordinate, []
        class Substituted(type):
            def __getattribute__(cls, name):
                if name in ("_EVENT_DOMAIN", "_DELIVERY_DOMAIN", "_ACK_DOMAIN"):
                    lookups.append(name)
                    raise AssertionError("changed controller domain semantics ran")
                return type.__getattribute__(cls, name)
        class Replacement(metaclass=Substituted):
            pass
        with mock.patch.object(history_query_join, "WakeHoldController", Replacement):
            with self.assertRaises(IntegrityFailure):
                context.source(coordinate, "seat-a")
        self.assertEqual([], lookups)

    def test_legacy_source_kind_vocabularies_are_bound_by_value(self):
        from floati import history_query_join
        context = epoch_history.open_history(self.root)
        coordinate = context.live_coordinate
        for name in ("EVENT_KINDS", "WAKE_HOLD_KINDS"):
            with self.subTest(name=name), mock.patch.object(history_query_join, name,
                    set(getattr(history_query_join, name)) | {"review_extra_kind"}):
                with self.assertRaises(IntegrityFailure):
                    context.source(coordinate, "seat-a")

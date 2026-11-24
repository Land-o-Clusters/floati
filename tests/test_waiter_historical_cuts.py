"""Historical eligibility agrees with native replay at each physical cutoff.

All records are scratch, in-memory native fixtures. The existing projector is
the independent behavioral oracle; no waiter, root, or validation is replaced.
"""
from __future__ import annotations

import itertools
import unittest
from unittest import mock

from floati import cursor_presentations
from floati.errors import IntegrityFailure
from floati.framing import encode_frame
from floati.jsonl import VerifiedLedgerCursor
from floati.records import validate_record
from floati.wake_hold import WakeHoldController, project_wake_items, wake_hold_receipt
from tests.test_wake_hold import NOW, message


TENANT = "alpha"
NODE = "bob"
WORKER = "worker-historical-cuts"


def record_id(kind, number):
    return kind + "018f7e9b3c117abc8def" + format(number, "012x")


A = record_id("msg-", 1)
B = record_id("msg-", 2)
WORKER_ITEM = record_id("msg-", 3)
FOREIGN_ITEM = record_id("msg-", 4)
MISSING_ITEM = record_id("msg-", 5)


def prefixes(rows, domain):
    digest = VerifiedLedgerCursor._initial_digest(domain)
    values = [digest.hexdigest()]
    for row in rows:
        digest.update(encode_frame(row))
        values.append(digest.hexdigest())
    return tuple(values)


class NativeSource:
    """Build closed native records without accessing any fleet or installed code."""

    def __init__(self):
        self.events = []
        self.deliveries = []
        self.acks = []

    @staticmethod
    def checked(row):
        return validate_record(row, TENANT, {row["kind"]}, integrity=True)

    def envelope(self, item_id, *, recipient=NODE, session=None):
        row = message(item_id, recipient=recipient, session=session, tenant_id=TENANT)
        row["idempotency_key"] = "historical-" + item_id
        self.events.append(self.checked(row))
        return self

    def retract_worker(self):
        self.events.append(self.checked({
            "schema_version": 0,
            "id": record_id("ret-", len(self.events) + 1),
            "tenant_id": TENANT,
            "timestamp": NOW,
            "kind": "message_retracted",
            "retracted_message_id": WORKER_ITEM,
            "worker_session_id": WORKER,
            "reason": "sent_in_error",
            "author": NODE,
        }))
        return self

    def delivery(self, *item_ids, recipient=NODE):
        self.deliveries.append(self.checked({
            "schema_version": 0,
            "id": record_id("delivery-", len(self.deliveries) + 1),
            "tenant_id": TENANT,
            "timestamp": NOW,
            "kind": "delivery_receipt",
            "recipient": recipient,
            "item_ids": list(item_ids),
            "presentation_count": len(self.deliveries) + 1,
        }))
        return self

    def ack(self, *item_ids):
        self.acks.append(self.checked({
            "schema_version": 0,
            "id": record_id("ack-", len(self.acks) + 1),
            "tenant_id": TENANT,
            "timestamp": NOW,
            "kind": "ack_receipt",
            "recipient": NODE,
            "item_ids": list(item_ids),
        }))
        return self

    def hold(self, *item_ids, event_count=None, ack_count=None):
        source = self.source()
        event_count = len(self.events) if event_count is None else event_count
        ack_count = len(self.acks) if ack_count is None else ack_count
        self.deliveries.append(self.checked(wake_hold_receipt(
            tenant_id=TENANT,
            recipient=NODE,
            worker_session_id=None,
            idempotency_key="historical-hold-" + str(len(self.deliveries)),
            limit=len(item_ids),
            item_ids=item_ids,
            event_prefix_digest=source[1][event_count],
            delivery_prefix_digest=source[3][-1],
            acknowledgment_prefix_digest=source[5][ack_count],
            now=NOW,
            record_id=record_id("wake-hold-", len(self.deliveries) + 1),
        )))
        return self

    def source(self):
        return (
            tuple(self.events), prefixes(self.events, WakeHoldController._EVENT_DOMAIN),
            tuple(self.deliveries), prefixes(self.deliveries, WakeHoldController._DELIVERY_DOMAIN),
            tuple(self.acks), prefixes(self.acks, WakeHoldController._ACK_DOMAIN),
        )

    def limits(self):
        return len(self.events), len(self.deliveries), len(self.acks)


def native_eligible(source, cut, item_ids):
    """Use the unchanged native replay and the original ordered selection rule."""
    events, ep, deliveries, dp, acks, ap = source
    event_count, delivery_count, ack_count = cut
    states = project_wake_items(
        events=events[:event_count],
        deliveries=deliveries[:delivery_count],
        acknowledgments=acks[:ack_count],
        recipient=NODE,
        worker_session_id=None,
        tenant_id=TENANT,
        event_prefix_digests=ep[:event_count + 1],
        delivery_prefix_digests=dp[:delivery_count + 1],
        acknowledgment_prefix_digests=ap[:ack_count + 1],
    )
    return [item.message["id"] for item in states
            if item.state in {"fresh", "held"} and item.message["id"] in item_ids]


class WaiterHistoricalCutsTests(unittest.TestCase):
    def test_repeated_imported_hold_cut_does_not_reslice_or_rescan_all_states(self):
        import json
        from floati import archive_presentation_replay as cold, bus_epoch
        from floati.archive_projection_replay import PoolEpochView
        from floati.codex_wait_contract import CodexWaitSessionLedger
        from floati.wake_hold import wake_coordination_guard
        from tests.test_archive_presentation_replay import ArchivePresentationReplayTests

        fixture = ArchivePresentationReplayTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        root = fixture.root
        envelope = fixture.fixture.send("imported-cut-cost")
        WakeHoldController(root).evaluate("seat-a", idempotency_key="original-cost-hold")
        fixture.fixture.install_fixture_carry(fixture.fixture.plan().live_payloads)
        binding, consent, unused_classification, first = fixture.fixture.presentation()
        plane = cursor_presentations.CursorPresentationLedger(root)
        first_bytes = (root.path / plane.relative("seat-a")).read_bytes()
        first_witness = json.loads((root.path / plane.witness_relative("seat-a")).read_text())

        # A second conversation may prepare the same still-undelivered mail.
        # Only claim/presentation planes change, so both groups name one cut.
        sessions = CodexWaitSessionLedger(root)
        sessions.arm(binding, consent, "conversation-b", idempotency_key="second-cut-session", take_over=True)
        classification = sessions.participate_classified(binding, consent, "conversation-b")
        with bus_epoch.epoch_guard(root), wake_coordination_guard(root, "seat-a"):
            selection = plane.select(binding, consent, classification, harness="codex")
            second = plane.prepare(selection, {"followup_message": "second native conversation"})
        chunks, inputs = fixture.inputs()
        one_group = dict(inputs, witness=first_witness, physical_bytes=first_bytes)

        def replay_with_work(supplied):
            visits = {"slices": 0, "states": 0, "replays": {}}
            class CountedList(list):
                def __getitem__(self, key):
                    if isinstance(key, slice):
                        visits["slices"] += len(range(*key.indices(len(self))))
                    return super().__getitem__(key)
            class CountedState:
                def __init__(self, item):
                    self.item = item
                @property
                def state(self):
                    visits["states"] += 1
                    return self.item.state
                @property
                def message(self):
                    return self.item.message
            native_source = PoolEpochView._source
            def source(view, *args):
                return tuple(CountedList(axis) for axis in native_source(view, *args))
            native_replayer = cold._projection_replayer
            def replayer(*args):
                project, original, origin, holds = native_replayer(*args)
                def replay(source, *coordinate):
                    key = tuple((len(source[axis]), source[axis + 1][-1]) for axis in (0, 2, 4))
                    visits["replays"][key] = visits["replays"].get(key, 0) + 1
                    return [CountedState(item) for item in project(source, *coordinate)]
                return replay, original, origin, holds
            # Observe actual work while the public cold API constructs and
            # validates its own historical cuts. No cache is constructed here.
            with mock.patch.object(PoolEpochView, "_source", source),\
                    mock.patch.object(cold, "_projection_replayer", replayer):
                result = fixture.replay(chunks, supplied)
            return result, visits

        original, once = replay_with_work(one_group)
        repeated, twice = replay_with_work(inputs)
        self.assertEqual([first], original["records"])
        self.assertEqual([first, second], repeated["records"])
        self.assertEqual([first["item_ids"]] * 2, [group["eligible_ids"] for group in repeated["groups"]])
        self.assertEqual([envelope["id"]], first["item_ids"])
        self.assertEqual(repeated["groups"][0]["counts"], repeated["groups"][1]["counts"])
        self.assertGreater(once["slices"], 0)
        self.assertGreater(once["states"], 0)
        self.assertTrue(once["replays"])
        self.assertTrue(all(count == 1 for count in once["replays"].values()))
        self.assertEqual(once, twice, "a repeated imported cut must not repeat source slices or state scans")

    def index(self, source, limits):
        implementation = getattr(cursor_presentations, "_HistoricalCuts", None)
        self.assertIsNotNone(implementation, "historical cut admission is not implemented")
        return implementation(source, NODE, TENANT, limits)

    def assert_eligible(self, index, source, cut, requested, expected):
        self.assertEqual(expected, native_eligible(source, cut, requested))
        self.assertEqual(expected, list(index.eligible(cut, requested)))

    def assert_cut_refused(self, index, source, cut, requested=(A,)):
        with self.assertRaises(IntegrityFailure):
            native_eligible(source, cut, requested)
        with self.assertRaises(IntegrityFailure):
            index.eligible(cut, requested)

    def test_every_small_cut_matches_native_projection_with_mixed_mail_and_repeats(self):
        """Catches cutoff dependencies omitted even when the joint source is valid."""
        fixture = NativeSource().envelope(A).envelope(WORKER_ITEM, session=WORKER)
        fixture.envelope(FOREIGN_ITEM, recipient="charlie").envelope(B).retract_worker()
        fixture.delivery().delivery(A).ack(A)
        fixture.hold(B, event_count=4, ack_count=1).delivery(A).ack(A).ack(B)
        source = fixture.source()
        index = self.index(source, fixture.limits())
        requests = ((A, B), (B, A), (WORKER_ITEM, FOREIGN_ITEM, MISSING_ITEM, A, B), ())
        admitted = refused = 0
        for cut in itertools.product(*(range(count + 1) for count in fixture.limits())):
            for requested in requests:
                with self.subTest(cut=cut, requested=requested):
                    try:
                        expected = native_eligible(source, cut, requested)
                    except IntegrityFailure:
                        refused += 1
                        with self.assertRaises(IntegrityFailure):
                            index.eligible(cut, requested)
                    else:
                        admitted += 1
                        self.assertEqual(expected, list(index.eligible(cut, requested)))
        self.assertGreater(admitted, 0)
        self.assertGreater(refused, 0)

    def test_delivery_cannot_borrow_a_message_beyond_the_historical_event_cut(self):
        """Removing delivery event coverage would admit this otherwise valid joint source."""
        fixture = NativeSource().envelope(A).envelope(B).delivery(B)
        source = fixture.source()
        index = self.index(source, fixture.limits())
        self.assert_cut_refused(index, source, (1, 1, 0))
        self.assert_eligible(index, source, (1, 0, 0), (A, B), [A])
        self.assert_eligible(index, source, (2, 1, 0), (B, A), [A, B])

    def test_hold_event_ancestry_is_required_even_when_every_named_message_exists(self):
        """Checking only named message ordinals misses the hold's later scan prefix."""
        fixture = NativeSource().envelope(A).envelope(B).hold(A, event_count=2)
        source = fixture.source()
        index = self.index(source, fixture.limits())
        self.assert_cut_refused(index, source, (1, 1, 0))
        self.assert_eligible(index, source, (2, 1, 0), (A, B), [A, B])

    def test_hold_cannot_borrow_an_ack_prefix_beyond_the_historical_ack_cut(self):
        """Dropping hold ACK ancestry incorrectly admits the A=0 cut."""
        fixture = NativeSource().envelope(A).envelope(B).delivery(A).ack(A)
        fixture.hold(B, ack_count=1)
        source = fixture.source()
        index = self.index(source, fixture.limits())
        self.assert_cut_refused(index, source, (2, 2, 0), (B,))
        self.assert_eligible(index, source, (2, 1, 0), (A, B), [A, B])
        self.assert_eligible(index, source, (2, 2, 1), (A, B), [B])

    def test_ack_cannot_borrow_a_delivery_beyond_the_historical_delivery_cut(self):
        """Dropping ACK delivery coverage lets an unpresented historical ACK suppress mail."""
        fixture = NativeSource().envelope(A).envelope(B).delivery(A).delivery(B).ack(B)
        source = fixture.source()
        index = self.index(source, fixture.limits())
        self.assert_cut_refused(index, source, (2, 1, 1))
        self.assert_eligible(index, source, (2, 1, 0), (A, B), [A, B])
        self.assert_eligible(index, source, (2, 2, 1), (A, B), [A])

    def test_first_delivery_and_first_ack_survive_later_repeat_receipts(self):
        """Last-occurrence indexes either reject an old valid ACK or revive ACKed mail."""
        fixture = NativeSource().envelope(A).envelope(B)
        fixture.delivery(A).delivery(A).delivery(B).ack(A).ack(A)
        source = fixture.source()
        index = self.index(source, fixture.limits())
        self.assert_eligible(index, source, (2, 1, 0), (A, B), [A, B])
        self.assert_eligible(index, source, (2, 1, 1), (A, B), [B])
        self.assert_eligible(index, source, (2, 3, 1), (A, B), [B])
        self.assert_eligible(index, source, (2, 3, 2), (A, B), [B])

    def test_worker_retraction_and_foreign_mail_never_join_the_ordinary_namespace(self):
        """Ignoring worker routing or event cutoffs leaks foreign envelopes into selection."""
        fixture = NativeSource().envelope(A).envelope(WORKER_ITEM, session=WORKER)
        fixture.envelope(FOREIGN_ITEM, recipient="charlie").envelope(B).retract_worker()
        source = fixture.source()
        index = self.index(source, fixture.limits())
        requested = (B, WORKER_ITEM, FOREIGN_ITEM, MISSING_ITEM, A)
        self.assert_eligible(index, source, (1, 0, 0), requested, [A])
        self.assert_eligible(index, source, (2, 0, 0), requested, [A])
        self.assert_eligible(index, source, (4, 0, 0), requested, [A, B])
        self.assert_eligible(index, source, (5, 0, 0), requested, [A, B])

    def test_empty_legacy_delivery_is_valid_even_at_an_empty_event_prefix(self):
        """An empty receipt contributes no event dependency and does not hide fresh mail."""
        fixture = NativeSource().envelope(A).delivery()
        source = fixture.source()
        index = self.index(source, fixture.limits())
        self.assert_eligible(index, source, (0, 1, 0), (A,), [])
        self.assert_eligible(index, source, (1, 1, 0), (A,), [A])

    def test_joint_max_constructor_refuses_an_invalid_referenced_prefix(self):
        """Skipping native admission in construction would accept a future-event delivery."""
        fixture = NativeSource().envelope(A).envelope(B).delivery(B)
        source = fixture.source()
        invalid_limits = (1, 1, 0)
        with self.assertRaises(IntegrityFailure):
            native_eligible(source, invalid_limits, (A, B))
        with self.assertRaises(IntegrityFailure):
            self.index(source, invalid_limits)

    def test_incomparable_valid_cuts_keep_their_own_eligibility_at_the_joint_maximum(self):
        """Using the joint maximum's final state erases earlier fresh and held mail."""
        fixture = NativeSource().envelope(A).envelope(B).delivery(A).delivery(A).ack(A)
        source = fixture.source()
        index = self.index(source, (2, 2, 1))
        self.assert_eligible(index, source, (2, 0, 0), (A, B), [A, B])
        self.assert_eligible(index, source, (1, 2, 0), (A, B), [A])
        self.assert_eligible(index, source, (1, 1, 1), (A, B), [])

    def test_joint_max_keeps_native_delivery_order_routing_and_retraction_validation(self):
        """Index construction must retain native semantics beyond mere ordinal coverage."""
        reversed_delivery = NativeSource().envelope(A).envelope(B).delivery(B, A)
        worker_delivery = NativeSource().envelope(WORKER_ITEM, session=WORKER)
        worker_delivery.delivery(WORKER_ITEM)
        foreign_retraction = NativeSource().envelope(WORKER_ITEM, session=WORKER)
        foreign_retraction.retract_worker()
        foreign_retraction.events[-1] = NativeSource.checked(dict(
            foreign_retraction.events[-1], author="charlie"))
        for label, fixture in (
            ("physical delivery order", reversed_delivery),
            ("worker delivery in ordinary ledger", worker_delivery),
            ("foreign retraction party", foreign_retraction),
        ):
            with self.subTest(contract=label):
                source = fixture.source()
                with self.assertRaises(IntegrityFailure):
                    native_eligible(source, fixture.limits(), (A, B, WORKER_ITEM))
                with self.assertRaises(IntegrityFailure):
                    self.index(source, fixture.limits())

    def test_unreferenced_future_ack_does_not_change_historical_admission(self):
        """Projecting the whole tail instead of the joint maximum rejects an older valid cut."""
        fixture = NativeSource().envelope(A).delivery(A).ack(B)
        source = fixture.source()
        with self.assertRaises(IntegrityFailure):
            native_eligible(source, fixture.limits(), (A,))
        index = self.index(source, (1, 1, 0))
        self.assert_eligible(index, source, (1, 1, 0), (A,), [A])

    def test_unreferenced_future_delivery_does_not_change_historical_admission(self):
        """Indexing unrelated delivery tails must not widen the native reader's scope."""
        fixture = NativeSource().envelope(A).delivery(A).delivery(A, recipient="charlie")
        source = fixture.source()
        with self.assertRaises(IntegrityFailure):
            native_eligible(source, fixture.limits(), (A,))
        index = self.index(source, (1, 1, 0))
        self.assert_eligible(index, source, (1, 1, 0), (A,), [A])


if __name__ == "__main__":
    unittest.main()

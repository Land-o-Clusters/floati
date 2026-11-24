"""Source-pool encoding over actual native survivor plans."""
from copy import deepcopy
import unittest
from unittest.mock import patch

from floati import archive_source_pool, archive_survivor_pool, bus_epoch, epoch_carry
from floati.archive_survivor_sources import collect_survivor_sources
from floati.cursor import SparseCursor
from floati.errors import IntegrityFailure, ProtocolRefusal
from floati.framing import encode_frame
from floati.events import EventLog
from floati.ids import uuid7_hex
from floati.message_consumption import MessageConsumption
from floati.wake_hold import WakeHoldController
from tests import test_epoch_carry as carry_fixture


class ArchiveSurvivorPoolTests(unittest.TestCase):
    def setUp(self):
        self.fixture = carry_fixture.EpochCarryTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.root

    def plan(self):
        with bus_epoch.epoch_guard(self.root, exclusive=True):
            return collect_survivor_sources(self.root)

    def encode(self, plan):
        chunks = archive_survivor_pool.encode_survivor_source_pool(plan)
        self.assertTrue(chunks)
        self.assertTrue(all(isinstance(chunk, bytes) and len(chunk) <= 8 * 1024 * 1024 for chunk in chunks))
        return archive_source_pool.read_pool(chunks, self.root.tenant_id)

    def test_complete_event_axes_and_empty_manual_boundary_encode(self):
        message = self.fixture.send("pool-before-manual")
        EventLog(self.root).present("seat-a")
        SparseCursor(self.root).ack("seat-a", [message["id"]], acting_session_id="future")
        self.fixture.automatic_roll()
        manual = bus_epoch.roll_bus_epoch(self.root, actor="sender", idempotency_key="pool-empty-manual")
        self.fixture.send("after-pool-manual")
        self.fixture.automatic_roll()
        plan = self.plan()
        projection = plan["sources"]["projection"]
        self.assertEqual(4, len(projection["epochs"]))
        headers = [entry for entry in projection["epochs"]
                   if entry["epoch_id"] == manual["receipt"]["epoch_id"]]
        self.assertEqual(1, len(headers))
        self.assertEqual(manual["receipt"], headers[0]["roll_receipt"])
        self.assertFalse(headers[0]["automatic"])
        self.assertFalse(any(cut["epoch_id"] == headers[0]["epoch_id"] for cut in projection["cuts"]))
        data = self.encode(plan)
        epochs = [data.entry(key) for key in data.by_tag("epoch")]
        self.assertEqual(4, len(epochs))
        middle = next(epoch for epoch in epochs if epoch["epoch_id"] == manual["receipt"]["epoch_id"])
        self.assertEqual(manual["receipt"], data.entry(middle["owner"])["record"])
        events = [data.entry(key) for key in data.by_tag("axis") if data.entry(key)["plane"] == "events"]
        self.assertEqual({epoch["key"] for epoch in epochs}, {axis["epoch"] for axis in events})

    def test_original_imported_hold_prefix_cuts_encode_without_rebasing(self):
        self.fixture.send("first-held")
        first = WakeHoldController(self.root).evaluate("seat-a", idempotency_key="first", limit=1)
        for index in range(3):
            self.fixture.send("extra-" + str(index))
        second = WakeHoldController(self.root).evaluate("seat-a", idempotency_key="second", limit=1)
        self.assertEqual((3, 1, True), (second["fresh_total"], second["held_total"], second["fresh_truncated"]))
        self.fixture.automatic_roll()
        self.fixture.automatic_roll()
        epoch_carry._FACT_CACHE.clear()
        epoch_carry._ORIGIN_CACHE.clear()
        plan = self.plan()
        holds = {entry["receipt"]["id"]: entry for entry in plan["sources"]["projection"]["holds"]}
        for original in (first, second):
            self.assertEqual(original, holds[original["receipt"]["id"]]["artifact"])
        self.assertGreater(len(holds[second["receipt"]["id"]]["before"][0]), 1)
        data = self.encode(plan)
        cuts = [data.entry(key) for key in data.by_tag("cut")]
        for original in (first, second):
            with self.subTest(receipt=original["receipt"]["id"]):
                receipt = original["receipt"]
                self.assertTrue(any(cut["axes"]["events"]["digest"] == receipt["event_prefix_digest"]
                    and cut["axes"]["deliveries"]["digest"] == receipt["delivery_prefix_digest"]
                    and cut["axes"]["acks"]["digest"] == receipt["acknowledgment_prefix_digest"] for cut in cuts))

    def test_completed_presentation_cut_and_native_authority_frames_encode(self):
        message = self.fixture.send("completed-presentation-pool")
        _binding, consent, _classification, prepared = self.fixture.presentation(flushed=True)
        SparseCursor(self.root).ack("seat-a", [message["id"]], acting_session_id="conversation-a")
        self.fixture.automatic_roll()
        self.fixture.automatic_roll()
        plan = self.plan()
        entries = plan["sources"]["presentation"]["entries"]
        self.assertEqual(1, len(entries))
        self.assertEqual(["prepared", "flushed"], [row["phase"] for row in entries[0]["records"]])
        group = entries[0]["groups"][0]
        self.assertEqual(prepared, group["records"][0])
        self.assertEqual(consent, group["consent"])
        self.assertEqual(prepared["claim_receipt_id"], group["claim"]["id"])
        self.assertIn(message, group["source"][0])
        self.assertEqual(prepared["item_ids"], group["eligible_ids"])
        data = self.encode(plan)
        records = {data.entry(key)["record"]["id"]: data.entry(key)["record"]
                   for key in data.by_tag("frame")}
        self.assertEqual(prepared, records[prepared["id"]])
        self.assertEqual(consent, records[consent["id"]])
        self.assertIn(prepared["claim_receipt_id"], records)
        cuts = [data.entry(key) for key in data.by_tag("cut")]
        self.assertTrue(any(cut["axes"]["events"]["digest"] == prepared["event_prefix_digest"]
            and cut["axes"]["deliveries"]["digest"] == prepared["delivery_prefix_digest"]
            and cut["axes"]["acks"]["digest"] == prepared["acknowledgment_prefix_digest"] for cut in cuts))

    def test_same_native_id_changed_in_supplied_planning_data_cannot_borrow_frame(self):
        self.fixture.send("immutable-pool-input")
        plan = self.plan()
        hostile = deepcopy(plan)
        row = hostile["sources"]["projection"]["epochs"][0]["event_source"][0][0]
        self.assertEqual("message_envelope", row["kind"])
        row["note"] = "changed outside native collector"
        with self.assertRaises(IntegrityFailure):
            archive_survivor_pool.encode_survivor_source_pool(hostile)

    def test_independent_worker_axes_keep_their_native_context(self):
        workers = ["worker-" + uuid7_hex(), "worker-" + uuid7_hex()]
        for worker in workers:
            self.fixture.send("worker-mail-" + worker, worker_session_id=worker)
            WakeHoldController(self.root).evaluate("seat-a", idempotency_key="hold-" + worker,
                                                   worker_session_id=worker)
        self.fixture.automatic_roll()
        data = self.encode(self.plan())
        delivery_axes = [data.entry(key) for key in data.by_tag("axis")
                         if data.entry(key)["plane"] == "deliveries"]
        self.assertEqual(set(workers), {axis["worker_session_id"] for axis in delivery_axes})

    def test_historical_consumption_frames_encode_across_manual_boundary(self):
        message = self.fixture.send("acted-before-pool")
        EventLog(self.root).present("seat-a")
        SparseCursor(self.root).ack("seat-a", [message["id"]], acting_session_id="acting")
        consumed = MessageConsumption(self.root).consume("seat-a", message["id"], "acting")
        bus_epoch.roll_bus_epoch(self.root, actor="sender", idempotency_key="pool-after-act")
        self.assertEqual([], MessageConsumption(self.root).records("seat-a"))
        plan = self.plan()
        self.assertEqual([consumed], plan["sources"]["consumption"]["receipts"])
        frames = {row["id"]: row for row in plan["frames"]}
        self.assertIn(message["id"], frames)
        self.assertEqual(consumed, frames[consumed["id"]])
        data = self.encode(plan)
        rows = {data.entry(key)["record"]["id"]: data.entry(key)["record"]
                for key in data.by_tag("frame")}
        self.assertEqual(consumed, rows[consumed["id"]])
        self.assertEqual(dict(message["message"]), rows[message["id"]])

    def test_isolated_original_hold_cut_rows_and_prefixes_match_complete_axis(self):
        self.fixture.send("isolated-original-occurrence")
        WakeHoldController(self.root).evaluate("seat-a", idempotency_key="original-cut-occurrence")
        original = self.plan()
        for mutation in ("row", "prefix", "prefix-last"):
            with self.subTest(mutation=mutation):
                plan = deepcopy(original)
                before = plan["sources"]["projection"]["holds"][0]["before"]
                if mutation == "row":
                    index = next(i for i, row in enumerate(before[0]) if row["kind"] == "message_envelope")
                    before[0][index] = dict(before[0][index], note="isolated false source occurrence")
                    self.assertFalse(any(row.get("note") == "isolated false source occurrence"
                        for epoch in plan["sources"]["projection"]["epochs"] for row in epoch["event_source"][0]))
                elif mutation == "prefix":
                    before[1][0] = "f" * 64
                else:
                    before[1][-1] = "f" * 64
                with self.assertRaises(IntegrityFailure) as refused:
                    archive_survivor_pool.encode_survivor_source_pool(plan)
                self.assertEqual("archive_survivor_pool_invalid", refused.exception.code)

    def test_malformed_isolated_hold_anchor_refuses_with_native_integrity_error(self):
        self.fixture.send("malformed-original-anchor")
        WakeHoldController(self.root).evaluate("seat-a", idempotency_key="malformed-cut-anchor")
        original = self.plan()
        for mutation in ("missing-kind", "missing-id", "unhashable-id"):
            with self.subTest(mutation=mutation):
                plan = deepcopy(original)
                before = plan["sources"]["projection"]["holds"][0]["before"]
                row = dict(before[0][0])
                if mutation == "missing-kind":
                    del row["kind"]
                else:
                    row["kind"] = "bus_epoch_roll_receipt"
                    if mutation == "missing-id":
                        del row["id"]
                    else:
                        row["id"] = []
                before[0][0] = row
                with self.assertRaises(IntegrityFailure):
                    archive_survivor_pool.encode_survivor_source_pool(plan)

    def _overconsumption_control(self, *, byte_limit=False):
        self.fixture.send("bounded-adapter-input")
        plan = self.plan()
        rows = plan["frames"]
        self.assertGreaterEqual(len(rows), 2)
        overconsumed = []
        def supplied():
            yield rows[0]
            yield rows[1]
            overconsumed.append(True)
        plan["frames"] = supplied()
        if byte_limit:
            limit = max(len(encode_frame(archive_source_pool.keyed_entry(
                "frame", self.root.tenant_id, record=row))) for row in rows[:2])
            controls = {"MAX_CHUNKS": 1, "MAX_CHUNK_BYTES": limit}
        else:
            controls = {"MAX_ENTRIES": 1}
        with patch.multiple(archive_source_pool, **controls):
            with self.assertRaises(ProtocolRefusal) as refused:
                archive_survivor_pool.encode_survivor_source_pool(plan)
        self.assertEqual("archive_source_pool_unsupported", refused.exception.code)
        self.assertEqual([], overconsumed, "adapter consumed beyond first provable pool overflow")

    def test_adapter_stops_frame_generator_at_unique_entry_limit(self):
        self._overconsumption_control()

    def test_adapter_stops_frame_generator_at_aggregate_byte_limit(self):
        self._overconsumption_control(byte_limit=True)

    def test_duplicate_frame_occurrences_cannot_bypass_input_work_bound(self):
        self.fixture.send("duplicate-adapter-work")
        plan = self.plan()
        row = plan["frames"][0]
        overconsumed = []
        encoded = []
        def nodes(value):
            if type(value) is dict:
                return 1 + len(value) + sum(nodes(child) for child in value.values())
            if type(value) in (list, tuple):
                return 1 + sum(nodes(child) for child in value)
            return 1
        native_encode = archive_survivor_pool.encode_frame
        def observed(value):
            if value.get("kind") == row["kind"] and value.get("id") == row["id"]:
                encoded.append(True)
            return native_encode(value)
        def supplied():
            for _ in range(3):
                yield row
            overconsumed.append(True)
        plan["frames"] = supplied()
        with patch.object(archive_survivor_pool, "MAX_INPUT_WORK", (nodes(row) * 2 + 8) * 2),\
                patch.object(archive_survivor_pool, "encode_frame", observed):
            with self.assertRaises(ProtocolRefusal) as refused:
                archive_survivor_pool.encode_survivor_source_pool(plan)
        self.assertEqual("archive_source_pool_unsupported", refused.exception.code)
        self.assertEqual([], overconsumed, "duplicate hits overconsumed beyond provable work overflow")
        self.assertEqual(2, len(encoded), "two complete duplicate frames must fit before the third refuses")

    def test_oversized_metadata_refuses_before_content_address_encoding(self):
        self.fixture.send("metadata-preflight")
        plan = self.plan()
        plan["tenant_id"] = "x" * (archive_survivor_pool.MAX_RECORD_BYTES + 1)
        with patch.object(archive_source_pool, "keyed_entry",
                side_effect=AssertionError("metadata encoded before bounded preflight")):
            with self.assertRaises(ProtocolRefusal) as refused:
                archive_survivor_pool.encode_survivor_source_pool(plan)
        self.assertEqual("archive_source_pool_unsupported", refused.exception.code)


if __name__ == "__main__":
    unittest.main()

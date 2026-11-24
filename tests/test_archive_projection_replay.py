"""Cold pure replay controls over genuine native multi-roll source pools."""
from copy import deepcopy
from pathlib import Path
import unittest
from unittest.mock import patch

from floati import archive_projection_replay, archive_source_pool, bus_epoch, epoch_carry
from floati.archive_projection_replay import PoolEpochView, replay_projection_pool
from floati.archive_survivor_pool import encode_survivor_source_pool
from floati.archive_survivor_sources import collect_survivor_sources
from floati.cursor import SparseCursor
from floati.errors import IntegrityFailure, ProtocolRefusal
from floati.events import EventLog
from floati.framing import encode_frame
from floati.jsonl import VerifiedLedgerCursor
from floati.records import wake_hold_decision_digest
from floati.wake_hold import WakeHoldController
from tests import test_epoch_carry as carry_fixture


class ArchiveProjectionReplayTests(unittest.TestCase):
    def setUp(self):
        self.fixture = carry_fixture.EpochCarryTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.root

    def plan(self):
        with bus_epoch.epoch_guard(self.root, exclusive=True):
            return collect_survivor_sources(self.root)

    def replay(self, plan):
        chunks = encode_survivor_source_pool(plan)
        # Pure replay must not resurrect a filesystem archive or borrow native
        # resolver caches. These are source DATA, not a new runtime proof object.
        epoch_carry._FACT_CACHE.clear()
        epoch_carry._ORIGIN_CACHE.clear()
        with patch.object(Path, "read_bytes", side_effect=AssertionError("payload read")):
            return replay_projection_pool(chunks, self.root.tenant_id)

    def test_cold_multi_roll_replay_preserves_entire_original_hold_artifacts(self):
        self.fixture.send("already-held")
        first = WakeHoldController(self.root).evaluate("seat-a", idempotency_key="first", limit=1)
        for index in range(3):
            self.fixture.send("fresh-" + str(index))
        second = WakeHoldController(self.root).evaluate("seat-a", idempotency_key="second", limit=1)
        self.assertEqual((3, 1, True), (second["fresh_total"], second["held_total"], second["fresh_truncated"]))
        self.fixture.automatic_roll()
        self.fixture.automatic_roll()
        replayed = self.replay(self.plan())
        artifacts = {row["receipt"]["id"]: row["artifact"] for row in replayed["holds"]}
        originals = {first["receipt"]["id"]: first, second["receipt"]["id"]: second}
        self.assertEqual(originals, {identity: artifacts[identity] for identity in originals})
        expected_ids = {row['receipt']['idempotency_key']: row['receipt']['item_ids']
                        for row in (first, second)}
        carried = [row for row in replayed['holds'] if row['receipt']['schema_version'] == 3]
        self.assertTrue(carried)
        for row in carried:
            self.assertEqual(expected_ids[row['receipt']['idempotency_key']],
                             [item['id'] for item in row['artifact']['fresh_messages']])

    def test_acknowledged_source_survives_without_any_prior_consumption(self):
        message = self.fixture.send("future-first-act")
        EventLog(self.root).present("seat-a")
        SparseCursor(self.root).ack("seat-a", [message["id"]], acting_session_id="future-actor")
        self.fixture.automatic_roll()
        self.fixture.automatic_roll()
        self.assertFalse((self.root.path / "receipts/consumed").exists())
        replayed = self.replay(self.plan())
        states = [state for cut in replayed["cuts"] for state in cut["states"]
                  if state["message"]["id"] == message["id"]]
        self.assertEqual(1, len(states))
        self.assertEqual(dict(message["message"]), states[0]["message"])
        self.assertEqual("acknowledged", states[0]["state"])
        self.assertEqual(1, states[0]["presentation_count"])

    def test_empty_manual_epoch_keeps_its_boundary_in_cold_replay(self):
        message = self.fixture.send("before-manual")
        EventLog(self.root).present("seat-a")
        SparseCursor(self.root).ack("seat-a", [message["id"]], acting_session_id="acting")
        self.fixture.automatic_roll()
        manual = bus_epoch.roll_bus_epoch(self.root, actor="sender", idempotency_key="empty-manual")
        self.fixture.send("after-manual")
        self.fixture.automatic_roll()
        replayed = self.replay(self.plan())
        self.assertEqual(4, len(replayed["epochs"]))
        middle = next(row for row in replayed["epochs"] if row["epoch_id"] == manual["receipt"]["epoch_id"])
        self.assertFalse(middle["automatic"])
        self.assertEqual(manual["receipt"], middle["roll_receipt"])
        self.assertFalse(any(row["epoch_id"] == middle["epoch_id"] for row in replayed["cuts"]))

    def test_internally_valid_pool_cannot_authorize_wrong_original_selection(self):
        self.fixture.send("first-fresh")
        second = self.fixture.send("second-fresh")
        original = WakeHoldController(self.root).evaluate("seat-a", idempotency_key="selection", limit=1)
        self.fixture.automatic_roll()
        plan = self.plan()
        def change(value):
            if isinstance(value, dict):
                if value.get("kind") == "wake_hold_receipt" and value.get("id") == original["receipt"]["id"]:
                    value["item_ids"] = [second["id"]]
                    value["decision_digest"] = wake_hold_decision_digest(value)
                for member in value.values():
                    change(member)
            elif isinstance(value, (list, tuple)):
                for member in value:
                    change(member)
        change(plan)
        def refresh(value):
            if isinstance(value, dict):
                for member in value.values():
                    refresh(member)
            elif isinstance(value, (list, tuple)):
                for member in value:
                    refresh(member)
                if (isinstance(value, list) and len(value) == 6
                        and all(isinstance(member, list) for member in value)
                        and all(isinstance(row, dict) for axis in (0, 2, 4) for row in value[axis])):
                    for axis, domain in ((0, WakeHoldController._EVENT_DOMAIN),
                                         (2, WakeHoldController._DELIVERY_DOMAIN),
                                         (4, WakeHoldController._ACK_DOMAIN)):
                        digest = VerifiedLedgerCursor._initial_digest(domain)
                        prefixes = [digest.hexdigest()]
                        for row in value[axis]:
                            digest.update(encode_frame(row))
                            prefixes.append(digest.hexdigest())
                        value[axis + 1] = prefixes
        refresh(plan)
        chunks = encode_survivor_source_pool(plan)
        # The pool is byte/shape consistent. Only native selection replay can
        # reject borrowing the second fresh item instead of the first.
        archive_source_pool.read_pool(chunks, self.root.tenant_id)
        with self.assertRaises((IntegrityFailure, ProtocolRefusal)):
            replay_projection_pool(chunks, self.root.tenant_id)

    def test_missing_context_planes_cannot_disappear_from_complete_replay(self):
        self.fixture.send("missing-context")
        pool = archive_source_pool.read_pool(encode_survivor_source_pool(self.plan()), self.root.tenant_id)
        entries = [pool.entry(key) for key in pool.keys
                   if pool.entry(key)["tag"] not in {"cut"}
                   and not (pool.entry(key)["tag"] == "axis" and pool.entry(key)["plane"] != "events")]
        chunks = archive_source_pool.encode_pool(entries)
        archive_source_pool.read_pool(chunks, self.root.tenant_id)
        with self.assertRaises((IntegrityFailure, ProtocolRefusal)):
            replay_projection_pool(chunks, self.root.tenant_id)

    def test_results_are_detached_data_and_never_live_hold_admission(self):
        self.fixture.send("detached-replay")
        WakeHoldController(self.root).evaluate("seat-a", idempotency_key="detached")
        plan = self.plan()
        first = self.replay(plan)
        def inspect(value):
            self.assertIsNot(type(value), epoch_carry._VerifiedHoldProofs)
            if isinstance(value, dict):
                for member in value.values():
                    inspect(member)
            elif isinstance(value, (list, tuple)):
                for member in value:
                    inspect(member)
        inspect(first)
        self.assertTrue(first["holds"])
        expected = deepcopy(first)
        first["holds"][0]["artifact"]["fresh_total"] = 999
        self.assertEqual(expected, self.replay(plan))

    def test_extra_context_axis_is_not_silently_omitted_from_replay(self):
        self.fixture.send("complete-context-scopes")
        pool = archive_source_pool.read_pool(encode_survivor_source_pool(self.plan()), self.root.tenant_id)
        entries = [pool.entry(key) for key in pool.keys]
        epoch = next(row for row in entries if row["tag"] == "epoch" and row["owner"] is None)
        entries.append(archive_source_pool.keyed_entry("axis", self.root.tenant_id,
            epoch=epoch["key"], plane="deliveries", recipient="sender", worker_session_id=None,
            frames=[], record_count=0, digest_domain=WakeHoldController._DELIVERY_DOMAIN,
            digest=VerifiedLedgerCursor._initial_digest(WakeHoldController._DELIVERY_DOMAIN).hexdigest()))
        chunks = archive_source_pool.encode_pool(entries)
        archive_source_pool.read_pool(chunks, self.root.tenant_id)
        with self.assertRaises((IntegrityFailure, ProtocolRefusal)):
            replay_projection_pool(chunks, self.root.tenant_id)

    def test_logical_source_queries_do_not_rescan_event_membership_per_context(self):
        self.fixture.send("fanout-event-index")
        view = PoolEpochView(encode_survivor_source_pool(self.plan()), self.root.tenant_id)
        epoch = view.epochs()[0]["key"]
        accesses = []
        class WatchedRecord(dict):
            def __getitem__(self, key):
                if key == "kind":
                    accesses.append(key)
                return super().__getitem__(key)
        axis = view._axes[(epoch, "events", None, None)]
        axis[1][:] = [WatchedRecord(row) for row in axis[1]]
        for index in range(16):
            view.source(epoch, "absent-" + str(index))
        self.assertLessEqual(len(accesses), 2 * len(axis[1]) + 2,
                             "logical view rescanned native event membership for each context")

    def test_missing_original_before_cut_cannot_borrow_the_valid_full_source(self):
        self.fixture.send("original-cut-required")
        original = WakeHoldController(self.root).evaluate("seat-a", idempotency_key="original-cut")
        pool = archive_source_pool.read_pool(encode_survivor_source_pool(self.plan()), self.root.tenant_id)
        entries = [pool.entry(key) for key in pool.keys]
        entries = [row for row in entries if not (row["tag"] == "cut"
            and row["axes"]["deliveries"]["digest"] == original["receipt"]["delivery_prefix_digest"])]
        chunks = archive_source_pool.encode_pool(entries)
        archive_source_pool.read_pool(chunks, self.root.tenant_id)
        with self.assertRaises((IntegrityFailure, ProtocolRefusal)):
            replay_projection_pool(chunks, self.root.tenant_id)

    def test_replay_work_bound_refuses_before_projecting_an_over_budget_cut(self):
        self.fixture.send("bounded-projection-work")
        WakeHoldController(self.root).evaluate("seat-a", idempotency_key="bounded")
        self.fixture.automatic_roll()
        chunks = encode_survivor_source_pool(self.plan())
        native = archive_projection_replay._project_origin_data
        calls = []
        def observed(*args):
            calls.append(True)
            return native(*args)
        with patch.object(archive_projection_replay, "MAX_REPLAY_RECORD_VISITS", 0),\
                patch.object(archive_projection_replay, "_project_origin_data", observed):
            with self.assertRaises(ProtocolRefusal) as refused:
                replay_projection_pool(chunks, self.root.tenant_id)
        self.assertEqual("archive_projection_replay_unsupported", refused.exception.code)
        self.assertEqual([], calls)

    def test_imported_origin_depth_has_a_typed_preserve_payload_bound(self):
        self.fixture.send("depth-bound")
        WakeHoldController(self.root).evaluate("seat-a", idempotency_key="depth")
        # Legacy imports retain their depth bound after the v3 writer replaces
        # new carries with native roll testimony.
        self.fixture.install_fixture_carry(self.fixture.plan().live_payloads)
        self.fixture.install_fixture_carry(self.fixture.plan().live_payloads)
        chunks = encode_survivor_source_pool(self.plan())
        with patch.object(archive_projection_replay, "MAX_ORIGIN_DEPTH", 0):
            with self.assertRaises(ProtocolRefusal) as refused:
                replay_projection_pool(chunks, self.root.tenant_id)
        self.assertEqual("archive_projection_replay_unsupported", refused.exception.code)


if __name__ == "__main__":
    unittest.main()

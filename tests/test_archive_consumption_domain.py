"""Closed consumption DATA controls from actual native ACT/ACK/roll fixtures."""
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import unittest
from unittest import mock

from floati import archive_consumption_domain as domain
from floati.archive_survivor_sources import collect_survivor_sources
from floati.archive_survivor_pool import encode_survivor_source_pool
from floati.bus_epoch import epoch_guard
from floati.errors import IntegrityFailure, ProtocolRefusal
from floati.framing import decode_frames, encode_frame
from floati.message_consumption import MessageConsumption
from floati.planes import AuthorityGrantStore
from tests import test_archive_consumption_checkpoint as fixture
from tests import test_epoch_carry as roll_fixture


class ArchiveConsumptionDomainTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixture.ArchiveConsumptionCheckpointTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.root

    def automatic_roll(self):
        AuthorityGrantStore(self.root).grant_exact("bus-epoch-roll", "sender", 1,
                                                 datetime.now(timezone.utc))
        return roll_fixture.EpochCarryTests.automatic_roll(self.fixture)

    def collected(self):
        with epoch_guard(self.root, exclusive=True):
            plan = collect_survivor_sources(self.root)
        return encode_survivor_source_pool(plan), plan["sources"]["consumption"]

    def prepared(self, *, rolls=0, manual=False, work=False, worker=None):
        native = self.fixture.prepare(work=work, worker=worker)
        for ordinal in range(rolls):
            if manual:
                self.fixture.roll("domain-manual-" + str(ordinal))
            else:
                self.automatic_roll()
        pool, plan = self.collected()
        return pool, plan, native

    def encoded(self, **options):
        pool, plan, native = self.prepared(**options)
        chunks = domain.encode_consumption_domain(pool, self.root.tenant_id, plan)
        self.assertTrue(chunks, "codec silently emitted no consumption domain")
        return pool, plan, native, chunks

    def pure(self, operation, *args):
        with mock.patch.object(Path, "read_bytes", side_effect=AssertionError("cold payload read")),\
                mock.patch.object(Path, "write_bytes", side_effect=AssertionError("cold payload write")),\
                mock.patch("builtins.open", side_effect=AssertionError("cold open")):
            return operation(*args)

    def rows(self, chunks):
        return [row for chunk in chunks for row in decode_frames(chunk)]

    def altered(self, chunks, change):
        rows = deepcopy(self.rows(chunks))
        change(rows)
        return (b"".join(encode_frame(row) for row in rows),)

    def refused(self, pool, chunks):
        with self.assertRaises(IntegrityFailure) as caught:
            self.pure(domain.verify_consumption_domain, pool, self.root.tenant_id, chunks)
        self.assertEqual("archive_consumption_domain_invalid", caught.exception.code)

    def test_native_acted_roundtrip_after_two_automatic_rolls_keeps_exact_join(self):
        worker_native = self.fixture.prepare(worker="worker-a")
        pool, plan, native, chunks = self.encoded(rolls=2)
        result = self.pure(domain.verify_consumption_domain, pool, self.root.tenant_id, chunks)
        for field in domain.SEMANTIC_FIELDS:
            self.assertEqual(json.dumps(plan[field], sort_keys=True), json.dumps(result[field], sort_keys=True))
        self.assertEqual(native[2]["id"], result["joins"][0]["receipt_id"])
        self.assertTrue(result["planes"])
        self.assertEqual({None, "worker-a"}, {row["worker_session_id"] for row in result["source_planes"]})
        self.assertEqual({native[2]["id"], worker_native[2]["id"]}, {row["id"] for row in result["receipts"]})
        self.assertTrue(all(type(chunk) is bytes and len(chunk) <= 8 * 1024 * 1024 for chunk in chunks))

    def test_old_act_survives_two_manual_boundaries_including_empty_epoch(self):
        pool, plan, native, chunks = self.encoded(rolls=2, manual=True)
        result = self.pure(domain.verify_consumption_domain, pool, self.root.tenant_id, chunks)
        self.assertEqual([native[2]], result["receipts"])
        self.assertEqual(plan["lineage"], result["lineage"])
        self.assertEqual(3, len([row for row in self.rows(chunks) if row["tag"] == "epoch"]))
        self.assertTrue(all(row["message_id"] != native[0]["id"] or not row["automatic_eligible"]
                            for row in result["readiness"]))

    def test_future_first_act_with_no_previous_consumption_has_complete_readiness(self):
        message = self.fixture._send()
        ack = self.fixture._ack(message)
        self.automatic_roll()
        self.automatic_roll()
        pool, plan = self.collected()
        self.assertEqual([], plan["receipts"])
        self.assertEqual([], plan["source_planes"], "native ACT-only subset must stay unchanged")
        chunks = domain.encode_consumption_domain(pool, self.root.tenant_id, plan)
        result = self.pure(domain.verify_consumption_domain, pool, self.root.tenant_id, chunks)
        ready = [row for row in result["readiness"] if row["message_id"] == message["id"]]
        self.assertEqual(1, len(ready), "no prior ACT must not erase future first-ACT sources")
        self.assertTrue(ready[0]["automatic_eligible"])
        self.assertEqual([ack], ready[0]["matching_ack_receipts"])
        self.assertEqual([], result["source_planes"])
        self.assertTrue(result["planes"])

    def test_manual_boundary_stops_future_first_act_but_retains_original_sources(self):
        message = self.fixture._send()
        self.fixture._ack(message)
        self.fixture.roll("future-domain-manual")
        pool, plan = self.collected()
        chunks = domain.encode_consumption_domain(pool, self.root.tenant_id, plan)
        result = self.pure(domain.verify_consumption_domain, pool, self.root.tenant_id, chunks)
        ready = [row for row in result["readiness"] if row["message_id"] == message["id"]]
        self.assertEqual(1, len(ready))
        self.assertFalse(ready[0]["automatic_eligible"])
        self.assertTrue(result["planes"])

    def test_unacknowledged_source_is_preserved_without_claiming_first_act_readiness(self):
        message = self.fixture._send()
        pool, plan = self.collected()
        chunks = domain.encode_consumption_domain(pool, self.root.tenant_id, plan)
        result = self.pure(domain.verify_consumption_domain, pool, self.root.tenant_id, chunks)
        ready = [row for row in result["readiness"] if row["message_id"] == message["id"]]
        self.assertEqual(1, len(ready))
        self.assertEqual([], ready[0]["matching_ack_receipts"])
        self.assertFalse(ready[0]["acknowledged"])


    def test_work_complete_retains_original_transitions_and_current_holder_data(self):
        pool, plan, native, chunks = self.encoded(work=True, rolls=1, manual=True)
        result = self.pure(domain.verify_consumption_domain, pool, self.root.tenant_id, chunks)
        self.assertEqual(plan["work_records"], result["work_records"])
        self.assertEqual(native[3]["id"], result["joins"][0]["work_item"]["id"])
        self.assertEqual("recipient", result["joins"][0]["work_item"]["holder"])

    def test_empty_domain_has_explicit_epoch_and_empty_work_scope(self):
        pool, plan = self.collected()
        chunks = domain.encode_consumption_domain(pool, self.root.tenant_id, plan)
        tags = [row["tag"] for row in self.rows(chunks)]
        self.assertIn("epoch", tags)
        self.assertIn("work_batch", tags)
        result = self.pure(domain.verify_consumption_domain, pool, self.root.tenant_id, chunks)
        self.assertEqual([], result["receipts"])

    def test_encoder_rejects_changed_native_receipt_join_source_or_lineage(self):
        pool, plan, native = self.prepared(rolls=1)
        edits = (lambda p: p["receipts"][0].update(acting_session_id="forged"),
                 lambda p: p["joins"][0].update(receipt_id="consume-forged"),
                 lambda p: p["source_planes"].clear(),
                 lambda p: p["lineage"].clear(),
                 lambda p: p["receipts"].clear(),
                 lambda p: p["receipts"].extend(deepcopy(p["receipts"])))
        for ordinal, change in enumerate(edits):
            with self.subTest(ordinal=ordinal):
                changed = deepcopy(plan)
                change(changed)
                with self.assertRaises(IntegrityFailure) as caught:
                    domain.encode_consumption_domain(pool, self.root.tenant_id, changed)
                self.assertEqual("archive_consumption_domain_invalid", caught.exception.code)


    def test_closed_domain_rejects_missing_extra_duplicate_and_reordered_entries(self):
        pool, plan, native, chunks = self.encoded(rolls=2)
        edits = (lambda rows: rows.pop(), lambda rows: rows.append(deepcopy(rows[-1])),
                 lambda rows: rows.reverse(), lambda rows: rows[0].update(tag="caller_authority"))
        for ordinal, change in enumerate(edits):
            with self.subTest(ordinal=ordinal):
                self.refused(pool, self.altered(chunks, change))

    def test_wrong_tenant_domain_or_open_shape_refuses(self):
        pool, plan, native, chunks = self.encoded()
        for field, value in (("tenant_id", "other"), ("domain", "execution"), ("caller_proof", True)):
            with self.subTest(field=field):
                self.refused(pool, self.altered(chunks, lambda rows: rows[0].update({field: value})))

    def test_wrong_act_frame_or_cut_cannot_borrow_valid_pool(self):
        pool, plan, native, chunks = self.encoded()
        for field in ("receipt", "source_cut"):
            with self.subTest(field=field):
                self.refused(pool, self.altered(chunks, lambda rows:
                    next(row for row in rows if row["tag"] == "acted").update({field: "f" * 64})))

    def test_complete_plane_scope_cannot_be_omitted_even_without_an_act(self):
        message = self.fixture._send()
        self.fixture._ack(message)
        pool, plan = self.collected()
        chunks = domain.encode_consumption_domain(pool, self.root.tenant_id, plan)
        rows = self.rows(chunks)
        self.assertTrue(any(row["tag"] == "plane" for row in rows))
        self.refused(pool, self.altered(chunks, lambda rows:
            rows.remove(next(row for row in rows if row["tag"] == "plane"))))

    def test_reference_bound_precedes_logical_view_copies(self):
        pool, plan, native = self.prepared()
        with mock.patch.object(domain, "MAX_REFERENCES", 1), mock.patch.object(
                domain, "PoolEpochView", side_effect=AssertionError("logical view copied before quota")):
            with self.assertRaises(ProtocolRefusal) as caught:
                domain.encode_consumption_domain(pool, self.root.tenant_id, plan)
        self.assertEqual("archive_consumption_domain_unsupported", caught.exception.code)

    def test_expanded_budget_precedes_batch_joins(self):
        pool, plan, native = self.prepared()
        with mock.patch.object(domain, "MAX_EXPANDED_BYTES", 1), mock.patch.object(
                domain, "replay_history", side_effect=AssertionError("join cloned before quota")):
            with self.assertRaises(ProtocolRefusal) as caught:
                domain.encode_consumption_domain(pool, self.root.tenant_id, plan)
        self.assertEqual("archive_consumption_domain_unsupported", caught.exception.code)

    def test_finite_plan_arrays_refuse_generators_without_consuming_them(self):
        pool, plan, native = self.prepared()
        def unbounded():
            self.fail("unbounded native plan generator was consumed")
            yield None
        plan["receipts"] = unbounded()
        with self.assertRaises(IntegrityFailure):
            domain.encode_consumption_domain(pool, self.root.tenant_id, plan)

    def test_preflight_reserves_work_before_advancing_queued_siblings(self):
        pool, plan = self.collected()
        visits = []
        class WatchedList(list):
            def __iter__(self):
                for child in super().__iter__():
                    visits.append(1)
                    yield child
        graph = 0
        for unused in range(5):
            graph = WatchedList([0] * 7 + [graph])
        plan = dict(source_witness=graph, **{key: value for key, value in plan.items() if key != "source_witness"})
        with mock.patch.object(domain, "MAX_WORK", 24):
            with self.assertRaises(ProtocolRefusal) as caught:
                domain.encode_consumption_domain(pool, self.root.tenant_id, plan)
        self.assertEqual("archive_consumption_domain_unsupported", caught.exception.code)
        self.assertTrue(visits)
        self.assertLessEqual(len(visits), 24)

    def test_preflight_refuses_depth_before_python_recursion(self):
        pool, plan = self.collected()
        value = 0
        for unused in range(10):
            value = [value]
        plan["source_witness"] = value
        with mock.patch.object(domain, "MAX_DEPTH", 8):
            with self.assertRaises(ProtocolRefusal) as caught:
                domain.encode_consumption_domain(pool, self.root.tenant_id, plan)
        self.assertEqual("archive_consumption_domain_unsupported", caught.exception.code)

    def test_actual_shared_ack_batch_stays_shared_in_detached_output(self):
        messages = [self.fixture._send() for unused in range(4)]
        self.fixture.events.present("recipient")
        ack = self.fixture.cursor.ack("recipient", [row["id"] for row in messages], acting_session_id="seen")
        MessageConsumption(self.root).consume_many("recipient", [row["id"] for row in messages], "acting")
        pool, plan = self.collected()
        with mock.patch.object(domain, "replay_history", wraps=domain.replay_history) as history:
            chunks = domain.encode_consumption_domain(pool, self.root.tenant_id, plan)
        self.assertEqual(1, history.call_count, "shared source revalidated once per ACT instead of per context")
        result = self.pure(domain.verify_consumption_domain, pool, self.root.tenant_id, chunks)
        self.assertEqual(4, len(result["joins"]))
        self.assertTrue(all(row["ack_receipt"] is result["joins"][0]["ack_receipt"] for row in result["joins"]))
        self.assertEqual(ack, result["joins"][0]["ack_receipt"])
        result["joins"][0]["ack_receipt"]["item_ids"].clear()
        self.assertEqual(4, len(plan["joins"][0]["ack_receipt"]["item_ids"]))

    def test_domain_chunks_are_bounded_and_canonical(self):
        pool, plan, native, chunks = self.encoded()
        with mock.patch.object(domain, "MAX_CHUNK_BYTES", 1):
            with self.assertRaises(ProtocolRefusal) as caught:
                domain.verify_consumption_domain(pool, self.root.tenant_id, chunks)
        self.assertEqual("archive_consumption_domain_unsupported", caught.exception.code)
        self.refused(pool, tuple(chunk.replace(b'"domain":', b'"domain" :', 1) for chunk in chunks))

    def test_empty_domain_still_validates_tenant_before_pool_construction(self):
        pool, plan = self.collected()
        for tenant in ("", None, [], "other-tenant"):
            with self.subTest(tenant=tenant), self.assertRaises((IntegrityFailure, ProtocolRefusal)):
                domain.encode_consumption_domain(pool, tenant, plan)


    def test_act_source_plane_binding_uses_direct_keys_instead_of_rescanning_planes(self):
        for worker in ("worker-a", "worker-b", "worker-c", "worker-d"):
            self.fixture.prepare(worker=worker)
        pool, plan = self.collected()
        reads = []
        class WatchedPlane(dict):
            def __getitem__(self, key):
                if key == "source_prefix":
                    reads.append(1)
                return super().__getitem__(key)
        native = domain._plane
        with mock.patch.object(domain, "_plane", side_effect=lambda *args: WatchedPlane(native(*args))):
            domain.encode_consumption_domain(pool, self.root.tenant_id, plan)
        self.assertLessEqual(len(reads), 2 * len(plan["receipts"]),
                             "each ACT scans all previously bound source planes")

    def test_serialized_domain_references_have_their_own_aggregate_charge(self):
        pool, plan, unused_native = self.prepared()
        chunks = domain.encode_consumption_domain(pool, self.root.tenant_id, plan)
        # Pool references and output-domain references share one bounded budget.
        # A quota equal to the source inventory leaves no room for the output.
        data = domain.read_pool(pool, self.root.tenant_id)
        axes = [data.entry(key) for key in data.by_tag("axis")]
        records = {key: data.entry(key)["record"] for key in data.by_tag("frame")}
        contexts = {(axis["recipient"], axis["worker_session_id"]) for axis in axes if axis["plane"] != "events"}
        event_count = sum(len(axis["frames"]) for axis in axes if axis["plane"] == "events")
        work_count = sum(row["kind"] in domain.WORK_KINDS for row in records.values())
        # Account for the input cut references before the serialized entries.
        source_refs = (len(records) + len(data.by_tag("epoch")) + sum(1 + len(axis["frames"]) for axis in axes)
                       + len(contexts) * (event_count + work_count)
                       + sum(len(records[key]["item_ids"]) for axis in axes if axis["plane"] == "acks" for key in axis["frames"])
                       + sum(sum(data.entry(key)[field] is not None
                                 for field in ("owner", "anchor", "predecessor"))
                             for key in data.by_tag("epoch"))
                       + 4 * len(data.by_tag("cut")))
        with mock.patch.object(domain, "MAX_REFERENCES", source_refs):
            with self.assertRaises(ProtocolRefusal) as caught:
                domain.encode_consumption_domain(pool, self.root.tenant_id, plan)
        self.assertEqual("archive_consumption_domain_unsupported", caught.exception.code)
        self.assertTrue(chunks)

    def test_repeated_context_event_bytes_are_charged_before_logical_view(self):
        self.fixture._send()
        self.fixture._send(recipient="other")
        pool, plan = self.collected()
        data = domain.read_pool(pool, self.root.tenant_id)
        sizes = {key: len(encode_frame(data.entry(key)["record"])) for key in data.by_tag("frame")}
        event_bytes = sum(sum(sizes[ref] for ref in data.entry(key)["frames"])
                          for key in data.by_tag("axis") if data.entry(key)["plane"] == "events")
        limit = sum(map(len, pool)) + 2 * event_bytes - 1
        error = None
        with mock.patch.object(domain, "MAX_EXPANDED_BYTES", limit), mock.patch.object(
                domain, "PoolEpochView", side_effect=AssertionError("repeated event bytes not reserved before copy")):
            try:
                domain.encode_consumption_domain(pool, self.root.tenant_id, plan)
            except Exception as caught:
                error = caught
        self.assertIsInstance(error, ProtocolRefusal)
        self.assertEqual("archive_consumption_domain_unsupported", error.code)

    def test_entry_byte_limit_precedes_whole_container_encoding(self):
        pool, plan, unused_native = self.prepared()
        native_encode = domain.encode_frame
        limit = max(len(chunk) for chunk in pool)
        # Keep the original pool below the bound, but make this supplied entry
        # larger than it with several individually supported scalar values.
        value = plan["source_planes"][0]
        value["source_prefix"] = ["x" * (limit // 2)] * 3
        self.assertGreater(len(json.dumps(value).encode("utf-8")), limit)
        def watched_encode(member):
            if member is value:
                raise AssertionError("oversized supplied entry encoded before byte refusal")
            return native_encode(member)
        with mock.patch.object(domain, "MAX_CHUNK_BYTES", limit), mock.patch.object(domain, "encode_frame", watched_encode):
            with self.assertRaises(ProtocolRefusal) as caught:
                domain.encode_consumption_domain(pool, self.root.tenant_id, plan)
        self.assertEqual("archive_consumption_domain_unsupported", caught.exception.code)

    def test_pool_cut_reference_charge_precedes_logical_view_indexing(self):
        pool, plan, unused_native = self.prepared()
        data = domain.read_pool(pool, self.root.tenant_id)
        self.assertTrue(data.by_tag("cut"), "native fixture must retain source cuts")
        axes = [data.entry(key) for key in data.by_tag("axis")]
        records = {key: data.entry(key)["record"] for key in data.by_tag("frame")}
        contexts = {(axis["recipient"], axis["worker_session_id"]) for axis in axes if axis["plane"] != "events"}
        event_count = sum(len(axis["frames"]) for axis in axes if axis["plane"] == "events")
        work_count = sum(row["kind"] in domain.WORK_KINDS for row in records.values())
        # Reserve the original frame/epoch/axis/context/ACK costs exactly,
        # leaving no room for the required neutral cut inventory.
        old_refs = (len(records) + len(data.by_tag("epoch")) + sum(1 + len(axis["frames"]) for axis in axes)
                    + len(contexts) * (event_count + work_count)
                    + sum(len(records[key]["item_ids"]) for axis in axes if axis["plane"] == "acks" for key in axis["frames"]))
        error = None
        with mock.patch.object(domain, "MAX_REFERENCES", old_refs), mock.patch.object(
                domain, "PoolEpochView", side_effect=AssertionError("neutral cuts copied before domain quota")):
            try:
                domain.encode_consumption_domain(pool, self.root.tenant_id, plan)
            except Exception as caught:
                error = caught
        self.assertIsInstance(error, ProtocolRefusal)
        self.assertEqual("archive_consumption_domain_unsupported", error.code)


if __name__ == "__main__":
    unittest.main()

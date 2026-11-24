"""Closed execution/PID domain controls over genuinely collected native pools."""
from copy import deepcopy
import json
import unittest
from unittest import mock

from floati import archive_execution_domain as domain
from floati.archive_survivor_sources import collect_survivor_sources
from floati.archive_survivor_pool import encode_survivor_source_pool
from floati.bus_epoch import epoch_guard
from floati.errors import FloatiError, IntegrityFailure, ProtocolRefusal
from floati.framing import decode_frames, encode_frame
from tests import test_archive_execution_checkpoint as fixture
from tests import test_archive_execution_replay as pure_fixture
from tests import test_seat_execution as native_fixture


class ArchiveExecutionDomainTests(unittest.TestCase):
    setUp = native_fixture.SeatExecutionTests.setUp
    car = native_fixture.SeatExecutionTests.car
    claim = native_fixture.SeatExecutionTests.claim
    controller = native_fixture.SeatExecutionTests.controller
    invocation = native_fixture.SeatExecutionTests.invocation
    prepare = fixture.ArchiveExecutionCheckpointTests.prepare
    automatic_roll = fixture.ArchiveExecutionCheckpointTests.automatic_roll
    tree = fixture.ArchiveExecutionCheckpointTests.tree
    pure = pure_fixture.ArchiveExecutionReplayTests.pure

    def collected(self, *, rolls=0, absent_worker=False):
        self.prepare({"event": "unknown", "code": "worker_cleanup_unconfirmed",
                      "detail": json.dumps(fixture.AUDIT)})
        if absent_worker:
            (self.root.tenant_home / "receipts/workers.jsonl").unlink()
            (self.root.tenant_home / "receipts/workers.jsonl.lock").unlink(missing_ok=True)
        for ordinal in range(rolls):
            self.automatic_roll("domain-" + str(ordinal))
        with epoch_guard(self.root, exclusive=True):
            plan = collect_survivor_sources(self.root)
        return encode_survivor_source_pool(plan), plan["sources"]["execution"]

    def encoded(self, **options):
        pool, plan = self.collected(**options)
        return pool, plan, domain.encode_execution_domain(pool, self.root.tenant_id, plan)

    def rows(self, chunks):
        return [row for chunk in chunks for row in decode_frames(chunk)]

    def altered(self, chunks, edit):
        rows = deepcopy(self.rows(chunks))
        edit(rows)
        return (b"".join(encode_frame(row) for row in rows),)

    def refused(self, pool, chunks):
        with self.assertRaises(IntegrityFailure) as caught:
            self.pure(domain.verify_execution_domain, pool, self.root.tenant_id, chunks)
        self.assertEqual("archive_execution_domain_invalid", caught.exception.code)

    def test_cold_roundtrip_after_two_rolls_matches_complete_native_result(self):
        pool, plan, chunks = self.encoded(rolls=2)
        for relative in ("events.jsonl.lock", "receipts/workers.jsonl.lock"):
            (self.root.tenant_home / relative).unlink(missing_ok=True)
        before = self.tree()
        result = self.pure(domain.verify_execution_domain, pool, self.root.tenant_id, chunks)
        self.assertEqual(before, self.tree())
        expected = {field: plan[field] for field in domain.SEMANTIC_FIELDS}
        expected["tenant_id"] = self.root.tenant_id
        self.assertEqual(json.dumps(expected, sort_keys=True), json.dumps(result, sort_keys=True))
        self.assertTrue(all(type(chunk) is bytes and len(chunk) <= 8 * 1024 * 1024 for chunk in chunks))

    def test_encoder_independently_checks_caller_replays_acceptance_and_pid_joins(self):
        pool, plan = self.collected()
        changes = (lambda p: p["replays"][-1]["result"]["execution"].update(detail="caller rewritten replay"),
                   lambda p: p["acceptance_keys"][-1].update(key="caller-replay-key"),
                   lambda p: p["worker_joins"][-1].update(audit_pid=987600),
                   lambda p: p["pid_receipts"][0].update(pid=987600))
        for edit in changes:
            changed = deepcopy(plan)
            edit(changed)
            with self.subTest(edit=changes.index(edit)):
                with self.assertRaises(IntegrityFailure) as caught:
                    domain.encode_execution_domain(pool, self.root.tenant_id, changed)
                self.assertEqual("archive_execution_domain_invalid", caught.exception.code)

    def test_missing_extra_and_duplicate_replay_results_refuse(self):
        pool, unused, chunks = self.encoded(rolls=2)
        def changed_result(rows):
            entry = next(row for row in rows if row["tag"] == "replay" and row["result"]["notice"] is not None)
            entry["result"]["notice"]["recipient_readiness"]["reason"] = "invented"
        def missing_notice(rows):
            next(row for row in rows if row["tag"] == "replay" and row["notice_frames"])["notice_frames"] = []
        edits = (
            ("missing replay", lambda rows: rows.remove(next(row for row in rows if row["tag"] == "replay"))),
            ("duplicate replay", lambda rows: rows.append(deepcopy(next(row for row in rows if row["tag"] == "replay")))),
            ("unknown replay", lambda rows: rows.append(dict(next(row for row in rows if row["tag"] == "replay"), tag="unknown"))),
            ("changed result", changed_result), ("missing notice", missing_notice),
            ("missing event batch", lambda rows: rows.remove(next(row for row in rows if row["tag"] == "event_batch"))),
            ("event frame order", lambda rows: next(row for row in rows if row["tag"] == "event_batch")["frames"].reverse()),
            ("event ordinal", lambda rows: next(row for row in rows if row["tag"] == "event_batch").update(ordinal=99)),
            ("missing worker frame", lambda rows: next(row for row in rows if row["tag"] == "worker_batch")["frames"].pop()),
            ("worker frame order", lambda rows: next(row for row in rows if row["tag"] == "worker_batch")["frames"].reverse()),
            ("PID group", lambda rows: next(row for row in rows if row["tag"] == "pid").update(pid=987656)),
        )
        for label, edit in edits:
            with self.subTest(case=label):
                self.refused(pool, self.altered(chunks, edit))
        for field, value in (("tenant_id", "other-tenant"), ("domain", "presentation"), ("extra", True)):
            with self.subTest(field=field):
                self.refused(pool, self.altered(chunks, lambda rows: rows[0].update({field: value})))


    def test_worker_absence_roundtrip_keeps_no_pid_ownership(self):
        pool, plan, chunks = self.encoded(absent_worker=True)
        actual = self.pure(domain.verify_execution_domain, pool, self.root.tenant_id, chunks)
        self.assertEqual([], actual["pid_receipts"])
        self.assertEqual(plan["worker_joins"], actual["worker_joins"])
        self.assertTrue(all(row["source"] is None for row in actual["worker_joins"]))


    def test_reference_budget_refuses_before_logical_view_deepcopy(self):
        pool, plan = self.collected()
        data = domain.read_pool(pool, self.root.tenant_id)
        axis_refs = sum(1 + len(data.entry(key)["frames"]) for key in data.by_tag("axis"))
        for limit in (1, axis_refs):
            with self.subTest(limit=limit), mock.patch.object(domain, "MAX_REFERENCES", limit), mock.patch.object(
                    domain, "PoolEpochView", side_effect=AssertionError("eager logical-view copy before bound")):
                with self.assertRaises(ProtocolRefusal) as caught:
                    domain.encode_execution_domain(pool, self.root.tenant_id, plan)
                self.assertEqual("archive_execution_domain_unsupported", caught.exception.code)

    def test_global_history_is_validated_once_per_encoding(self):
        pool, plan = self.collected()
        with mock.patch.object(domain, "_execution_history", wraps=domain._execution_history) as history:
            domain.encode_execution_domain(pool, self.root.tenant_id, plan)
        self.assertEqual(1, history.call_count)


    def test_oversized_or_noncanonical_chunk_refuses_before_domain_replay(self):
        pool, unused, chunks = self.encoded()
        with mock.patch.object(domain, "MAX_CHUNK_BYTES", 1):
            with self.assertRaises(ProtocolRefusal) as caught:
                domain.verify_execution_domain(pool, self.root.tenant_id, chunks)
        self.assertEqual("archive_execution_domain_unsupported", caught.exception.code)
        changed = tuple(chunk.replace(b'"domain":', b'"domain" :', 1) for chunk in chunks)
        self.refused(pool, changed)

    def test_preflight_does_not_expand_more_nodes_than_its_work_budget(self):
        expanded = []
        class WatchedList(list):
            # Instrument iteration only: payload contents are ordinary finite
            # JSON list/scalar data and the scaled graph is deliberately tiny.
            def __iter__(self):
                for value in super().__iter__():
                    expanded.append(True)
                    yield value
        graph = 0
        for unused in range(5):
            graph = WatchedList([0] * 7 + [graph])
        with mock.patch.object(domain, "MAX_WORK", 24):
            with self.assertRaises(ProtocolRefusal) as caught:
                domain._Budget().scan(graph)
        self.assertEqual("archive_execution_domain_unsupported", caught.exception.code)
        self.assertLessEqual(len(expanded), 24,
            "preflight expanded queued siblings without reserving their work")

    def test_preflight_refuses_nesting_before_the_runtime_recursion_boundary(self):
        graph = 0
        for unused in range(10):
            graph = [graph]
        with mock.patch.object(domain, "MAX_DEPTH", 8):
            with self.assertRaises(ProtocolRefusal) as caught:
                domain._Budget().scan(graph)
        self.assertEqual("archive_execution_domain_unsupported", caught.exception.code)

"""Finite shared-work hooks and detached result reservations."""
import contextvars
import threading
import unittest
from unittest import mock

from floati import archive_survivor_sources as work
from floati import archive_source_pool as pools, archive_catalog_entries as catalogs
from floati import archive_survivor_admission as admission
from floati import archive_survivor_bundle as bundle, archive_consumption_domain as consumption
from floati import archive_execution_domain as execution, archive_presentation_domain as presentation
from tests.readiness import wait_join


class QueryWorkHookTests(unittest.TestCase):
    def test_exact_boundary_sticky_exhaustion_and_fresh_scope(self):
        with self.assertRaises(work._QueryWorkExhausted):
            with work._query_work_scope(3) as state:
                work._query_charge(3)
                self.assertEqual(0, state.remaining)
                with self.assertRaises(work._QueryWorkExhausted):
                    work._query_charge(1)
                work._query_charge(0)
        self.assertIsNone(work._QUERY_ALLOWANCE.get())
        with work._query_work_scope(3) as state:
            work._query_charge(3)
            self.assertEqual(0, state.remaining)

    def test_nested_scope_cannot_reset_refund_or_enlarge(self):
        with self.assertRaises(work._QueryWorkExhausted):
            with work._query_work_scope(2) as outer:
                with work._query_work_scope(1000) as inner:
                    self.assertIs(outer, inner)
                    work._query_charge(2)
                work._query_charge(1)
        with self.assertRaises(work._QueryWorkExhausted):
            with work._query_work_scope(2):
                work._query_charge(-1)
        self.assertIsNone(work._QUERY_ALLOWANCE.get())

    def test_copied_context_cannot_debit_other_thread_and_fresh_thread_is_independent(self):
        outcomes = []
        with work._query_work_scope(5) as state:
            copied = contextvars.copy_context()
            def foreign():
                try:
                    copied.run(work._query_charge, 1)
                except work._QueryWorkExhausted:
                    outcomes.append("foreign refused")
                with work._query_work_scope(2) as own:
                    work._query_charge(2)
                    outcomes.append(own.remaining)
            thread = threading.Thread(target=foreign)
            thread.start()
            wait_join(thread, "foreign query work finishes")
            self.assertFalse(thread.is_alive())
            self.assertEqual(5, state.remaining)
            work._query_charge(5)
        self.assertEqual(["foreign refused", 0], outcomes)

    def test_cumulative_leaf_work_and_foreign_wrappers_keep_private_exhaustion(self):
        for wrapper in (execution._semantic_call, consumption._call):
            def stages():
                bundle._Budget().charge(work=3)
                execution._Budget().charge(work=3)
                consumption._Budget().charge(work=3)
                presentation._Budget().charge(work=3)
            with self.subTest(wrapper=wrapper.__module__):
                with self.assertRaises(work._QueryWorkExhausted):
                    with work._query_work_scope(11):
                        wrapper(stages)
        self.assertIsNone(work._QUERY_ALLOWANCE.get())

    def test_shared_object_coverage_charges_every_expanded_occurrence(self):
        shared = {"name": ["a", "b", "c"]}
        value = [shared] * 4
        measured = []
        work._domain_digest("budget_fixture", value,
            _on_measure=lambda size, nodes: measured.append(nodes))
        nodes = measured[0]
        with work._query_work_scope(nodes):
            work._domain_digest("budget_fixture", value)
        with self.assertRaises(work._QueryWorkExhausted):
            with work._query_work_scope(nodes - 1):
                work._domain_digest("budget_fixture", value)

    def test_coverage_refuses_before_advancing_overbound_siblings(self):
        calls = []
        def scalar(*args):
            calls.append(True)
            self.fail("scalar serialization preceded sibling admission")
        with mock.patch.object(work, "_canonical", scalar):
            with self.assertRaises(work._QueryWorkExhausted):
                with work._query_work_scope(2):
                    work._domain_digest("budget_fixture", ["x"] * 4)
        self.assertEqual([], calls)

    def test_pool_catalog_and_leaf_decode_are_reserved_before_parser(self):
        raw = b"{}\n"
        operations = (
            (pools, lambda: pools.read_pool((raw,), "tenant")),
            (catalogs, lambda: catalogs.read_catalog((raw,), "tenant",
                pools.PoolData("tenant", {}, {}, {}))),
            (bundle, lambda: bundle._manifest(raw, "tenant", bundle._Budget())),
            (consumption, lambda: consumption._read((raw,), "tenant", consumption._Budget())),
            (execution, lambda: execution._read_domain((raw,), "tenant", execution._Budget())),
            (presentation, lambda: presentation._read((raw,), "tenant", presentation._Budget())),
        )
        def parser(*args):
            self.fail("decode ran before its raw reservation")
        for module, operation in operations:
            with self.subTest(module=module.__name__), mock.patch.object(module, "decode_frames", parser):
                with self.assertRaises(work._QueryWorkExhausted):
                    with work._query_work_scope(len(raw) - 1):
                        operation()

    def test_stored_entry_decode_is_reserved_before_json_allocation(self):
        raw = b'{"name":"value"}'
        entries = (pools.PoolData("tenant", {"key": raw}, {}, {}),
                   catalogs.CatalogData("tenant", {"key": raw}, {}, {}, {}, {}))
        for entry in entries:
            with self.subTest(entry=type(entry).__name__):
                with self.assertRaises(work._QueryWorkExhausted):
                    with work._query_work_scope(len(raw) - 1):
                        entry.entry("key")

    def test_projection_expansion_refuses_before_origin_or_projector(self):
        from floati import archive_projection_replay as projection
        source = ([{"id": "closed-data-only"}], ("prefix",), [], ("prefix",), [], ("prefix",))
        project, _original, _origin, _holds = projection._projection_replayer(object(), "tenant")
        calls = []
        def expensive(*args, **kwargs):
            calls.append(True)
            self.fail("origin/projector ran before expanded source admission")
        with mock.patch.object(projection, "_imported_hold_origins", expensive),\
                mock.patch.object(projection, "_project_origin_data", expensive):
            with self.assertRaises(work._QueryWorkExhausted):
                with work._query_work_scope(4):
                    project(source, "epoch", "seat-a", None)
        self.assertEqual([], calls)

    def test_source_result_decode_reserves_raw_before_copy(self):
        # This is detached DATA only. The helper cannot register any admission.
        raw = b'{"tag":"frame","name":"' + b"x" * 1000 + b'"}\n'
        sources = pools.PoolData("tenant", {"key": raw}, {}, {})
        commit = {"generation": "detached-test-data"}
        base = {"generation": commit["generation"], "commit": commit,
                "catalog": [], "survivors": {}, "sources": []}
        measured = []
        work._domain_digest("result_fixture", base,
            _on_measure=lambda size, nodes: measured.append(nodes))
        calls = []
        def decode(*args, **kwargs):
            calls.append(True)
            self.fail("source JSON copied before raw reservation")
        with mock.patch.object(admission.json, "loads", decode):
            with self.assertRaises(work._QueryWorkExhausted):
                with work._query_work_scope(measured[0] + len(raw) - 1):
                    admission._encode_query_data(commit, [], sources, {})
        self.assertEqual([], calls)

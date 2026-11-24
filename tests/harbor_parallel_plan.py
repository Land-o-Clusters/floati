"""Exact partition and native receipt proofs for the parallel runner."""

import copy
import hashlib
import importlib
import json
import unittest
from unittest import mock

try:
    planner = importlib.import_module("scripts.parallel_plan")
except ModuleNotFoundError as exc:
    if exc.name != "scripts.parallel_plan":
        raise
    planner = None


def census():
    return {
        "ids": ["m.A.a", "m.A.b", "m.B.a", "m.C.a", "n.D.a"],
        "units": [
            {"key": "A", "ids": ["m.A.a", "m.A.b"], "classes": ["m.A"], "serial_reason": None},
            {"key": "B", "ids": ["m.B.a"], "classes": ["m.B"], "serial_reason": None},
            {"key": "C", "ids": ["m.C.a"], "classes": ["m.C"], "serial_reason": None},
            {"key": "D", "ids": ["n.D.a"], "classes": ["n.D"], "serial_reason": "shared checkout"},
        ],
        "population_digest": "e88613d0c1dbc9828e39571883eecb8f8ad647665dc26dab359c1548848bd65e",
        "membership": {
            "m.A.a": {"class": "m.A", "module": "m", "origin": "m"},
            "m.A.b": {"class": "m.A", "module": "m", "origin": "m"},
            "m.B.a": {"class": "m.B", "module": "m", "origin": "m"},
            "m.C.a": {"class": "m.C", "module": "m", "origin": "m"},
            "n.D.a": {"class": "n.D", "module": "n", "origin": "n"},
        },
    }


def receipt(assignment):
    ids = assignment["ids"][:]
    return {
        "key": assignment["key"], "population_digest": "e88613d0c1dbc9828e39571883eecb8f8ad647665dc26dab359c1548848bd65e",
        "selected_ids": ids, "started_ids": ids[:], "stopped_ids": ids[:],
        "events": [{"kind": "success", "test_id": test_id} for test_id in ids],
        "fixture_blocked_ids": [], "tests_run": len(ids), "successful": True,
        "duration_seconds": 0.1, "status": "complete",
    }


class ParallelPlanTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(planner, "scripts.parallel_plan is not implemented")
        self.census = census()

    def plan(self):
        return planner.make_plan(self.census, 2)

    def reject_plan_before_execution(self, plan, reason):
        executed = []
        with self.assertRaisesRegex(ValueError, reason):
            planner.validate_plan(self.census, plan)
            executed.append("executor invoked")
        self.assertEqual(executed, [])

    def test_count_balancing_and_native_order(self):
        self.assertEqual(self.plan()["assignments"], [
            {"key": "worker-1", "phase": "parallel", "ids": ["m.A.a", "m.A.b"], "unit_keys": ["A"]},
            {"key": "worker-2", "phase": "parallel", "ids": ["m.B.a", "m.C.a"], "unit_keys": ["B", "C"]},
            {"key": "serial", "phase": "serial", "ids": ["n.D.a"], "unit_keys": ["D"]},
        ])

    def test_duration_weights_affect_placement_only(self):
        plan = planner.make_plan(self.census, 2, {"A": 1.0, "B": 8.0, "C": 3.0, "D": 9.0})
        self.assertEqual(plan["assignments"][0]["ids"], ["m.B.a"])
        self.assertEqual(plan["assignments"][1]["ids"], ["m.A.a", "m.A.b", "m.C.a"])
        planner.validate_plan(self.census, plan)

    def test_prediction_reports_parallel_max_mean_separately_from_serial_tail(self):
        weights = {"A": 1.0, "B": 8.0, "C": 3.0, "D": 90.0}
        plan = planner.make_plan(self.census, 2, weights)
        self.assertTrue(callable(getattr(planner, 'plan_estimates', None)),
                        'planning must report predicted max/mean')
        estimates = planner.plan_estimates(plan, weights)
        self.assertEqual(8.0, estimates['max_parallel_seconds'])
        self.assertEqual(6.0, estimates['mean_parallel_seconds'])
        self.assertAlmostEqual(8 / 6, estimates['parallel_max_mean_ratio'])
        self.assertEqual(90.0, estimates['serial_tail_seconds'])
        zero = planner.plan_estimates(plan, dict.fromkeys(weights, 0.0))
        self.assertIsNone(zero['parallel_max_mean_ratio'])

    def test_weights_must_cover_all_units_with_finite_nonnegative_values(self):
        for weights in ({"A": 1}, {"A": 1, "B": 1, "C": 1, "D": float("nan")},
                        {"A": -1, "B": 1, "C": 1, "D": 1},
                        {"A": 1, "B": 1, "C": 1, "D": 1, "foreign": 1}):
            with self.subTest(weights=weights), self.assertRaisesRegex(ValueError, "weights"):
                planner.make_plan(self.census, 2, weights)

    def test_worker_count_bounds_and_empty_workers_refuse(self):
        for workers in (0, 4, True, 1.5):
            with self.subTest(workers=workers), self.assertRaisesRegex(ValueError, "workers"):
                planner.make_plan(self.census, workers)
        for cpus, limit in ((None, 1), (1, 1), (10, 10), (12, 12)):
            with self.subTest(cpus=cpus), mock.patch('os.cpu_count', return_value=cpus):
                serial = copy.deepcopy(self.census)
                for unit in serial['units']:
                    unit['serial_reason'] = 'exclusive'
                self.assertEqual('serial', planner.make_plan(serial, limit)['assignments'][0]['key'])
                with self.assertRaisesRegex(ValueError, 'parallel_workers_out_of_range'):
                    planner.make_plan(serial, limit + 1)

    def test_worker_keys_are_canonical_and_within_the_host_bound(self):
        with mock.patch('os.cpu_count', return_value=10):
            for key in ('worker-0', 'worker-01', 'worker-11', 'worker-1.0', 'worker--1'):
                with self.subTest(key=key):
                    plan = self.plan()
                    plan['assignments'][0]['key'] = key
                    self.reject_plan_before_execution(plan, 'phase/key|parallel_workers_out_of_range')

    def test_all_serial_population_has_only_serial_assignment(self):
        for unit in self.census["units"]:
            unit["serial_reason"] = "exclusive"
        plan = planner.make_plan(self.census, 1)
        self.assertEqual(plan["assignments"], [{"key": "serial", "phase": "serial",
                         "ids": self.census["ids"], "unit_keys": ["A", "B", "C", "D"]}])

    def test_empty_census_refuses(self):
        self.census.update(ids=[], units=[])
        with self.assertRaisesRegex(ValueError, "empty population"):
            planner.validate_census(self.census)

    def test_duplicate_census_id_refuses(self):
        self.census["ids"].append("m.A.a")
        with self.assertRaisesRegex(ValueError, "duplicate"):
            planner.validate_census(self.census)

    def test_census_digest_is_independently_verified(self):
        self.census["population_digest"] = "unverified"
        with self.assertRaisesRegex(ValueError, "population digest"):
            planner.validate_census(self.census)

    def test_missing_serial_reason_refuses_as_value_error(self):
        del self.census["units"][0]["serial_reason"]
        with self.assertRaisesRegex(ValueError, "serial reason"):
            planner.make_plan(self.census, 2)

    def test_zero_duration_weights_cannot_create_empty_workers(self):
        plan = planner.make_plan(self.census, 3, {"A": 0, "B": 0, "C": 0, "D": 0})
        self.assertEqual([len(a["unit_keys"]) for a in plan["assignments"]], [1, 1, 1, 1])

    def test_membership_requires_exact_population_keys(self):
        for mutation in ("missing", "foreign", "absent"):
            damaged = copy.deepcopy(self.census)
            if mutation == "missing":
                del damaged["membership"]["m.A.a"]
            elif mutation == "foreign":
                damaged["membership"]["foreign"] = {"class": "m.A", "module": "m", "origin": "m"}
            else:
                del damaged["membership"]
            with self.subTest(mutation=mutation), self.assertRaisesRegex(ValueError, "membership"):
                planner.validate_census(damaged)

    def test_membership_requires_native_class_module_and_origin_identity(self):
        for field, value in (("class", "m.Z"), ("module", "foreign"), ("origin", "")):
            damaged = copy.deepcopy(self.census)
            damaged["membership"]["m.A.a"][field] = value
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "membership"):
                planner.validate_census(damaged)

    def test_unit_classes_must_equal_member_classes(self):
        self.census["units"][0]["classes"].append("m.Z")
        with self.assertRaisesRegex(ValueError, "membership"):
            planner.validate_census(self.census)

    def test_missing_unit_member_refuses(self):
        self.census["units"][0]["ids"].pop()
        with self.assertRaisesRegex(ValueError, "missing"):
            planner.validate_census(self.census)

    def test_duplicate_unit_key_refuses(self):
        self.census["units"][1]["key"] = "A"
        with self.assertRaisesRegex(ValueError, "duplicate unit"):
            planner.validate_census(self.census)

    def test_class_cannot_be_split_across_census_units(self):
        self.census["units"][1]["classes"] = ["m.A"]
        with self.assertRaisesRegex(ValueError, "split class"):
            planner.validate_census(self.census)

    def test_dropped_id_refuses_before_execution(self):
        plan = self.plan()
        plan["assignments"][0]["ids"].pop()
        self.reject_plan_before_execution(plan, "missing")

    def test_duplicate_id_refuses_before_execution(self):
        plan = self.plan()
        plan["assignments"][0]["ids"].append("m.A.a")
        self.reject_plan_before_execution(plan, "duplicate")

    def test_foreign_id_refuses_before_execution(self):
        plan = self.plan()
        plan["assignments"][0]["ids"][0] = "foreign.test"
        self.reject_plan_before_execution(plan, "foreign")

    def test_empty_assignment_refuses_before_execution(self):
        plan = self.plan()
        plan["assignments"].append({"key": "worker-3", "phase": "parallel", "ids": [], "unit_keys": []})
        self.reject_plan_before_execution(plan, "empty assignment")

    def test_split_unit_refuses_before_execution(self):
        plan = self.plan()
        plan["assignments"][0]["ids"].remove("m.A.b")
        plan["assignments"][1]["ids"].append("m.A.b")
        self.reject_plan_before_execution(plan, "split unit")

    def test_unit_key_coverage_is_independent_of_ids(self):
        plan = self.plan()
        plan["assignments"][1]["unit_keys"].remove("B")
        self.reject_plan_before_execution(plan, "missing unit")

    def test_serial_hazard_cannot_be_placed_in_parallel_phase(self):
        plan = self.plan()
        plan["assignments"][-1].update(key="worker-3", phase="parallel")
        self.reject_plan_before_execution(plan, "serial unit")

    def test_digest_mismatch_refuses_before_execution(self):
        plan = self.plan()
        plan["population_digest"] = "another population"
        self.reject_plan_before_execution(plan, "population digest")

    def test_native_selection_order_is_verified(self):
        plan = self.plan()
        plan["assignments"][0]["ids"].reverse()
        self.reject_plan_before_execution(plan, "native order")


class ParallelReconciliationTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(planner, "scripts.parallel_plan is not implemented")
        self.census = census()
        self.plan = planner.make_plan(self.census, 2)
        self.results = [receipt(a) for a in self.plan["assignments"]]

    def reconcile(self):
        return planner.reconcile(self.census, self.plan, self.results)

    def test_complete_native_outcomes_reconcile_in_assignment_order(self):
        self.results.reverse()
        aggregate = self.reconcile()
        self.assertEqual(aggregate["selected_ids"], self.census["ids"])
        self.assertEqual(aggregate["tests_run"], 5)
        self.assertTrue(aggregate["successful"])
        self.assertEqual(aggregate["fixture_blocked_ids"], [])

    def test_missing_result_refuses(self):
        self.results.pop()
        with self.assertRaisesRegex(ValueError, "missing result"):
            self.reconcile()

    def test_duplicate_result_refuses(self):
        self.results.append(copy.deepcopy(self.results[0]))
        with self.assertRaisesRegex(ValueError, "duplicate result"):
            self.reconcile()

    def test_result_digest_and_selection_are_checked(self):
        for field, value, reason in (("population_digest", "foreign", "population digest"),
                                     ("selected_ids", ["m.A.a"], "selected")):
            with self.subTest(field=field):
                results = copy.deepcopy(self.results)
                results[0][field] = value
                with self.assertRaisesRegex(ValueError, reason):
                    planner.reconcile(self.census, self.plan, results)

    def test_missing_stop_refuses(self):
        self.results[0]["stopped_ids"].pop()
        with self.assertRaisesRegex(ValueError, "stop"):
            self.reconcile()

    def test_callback_order_must_remain_native_inside_each_worker(self):
        for field in ("started_ids", "stopped_ids"):
            with self.subTest(field=field):
                results = copy.deepcopy(self.results)
                results[0][field].reverse()
                with self.assertRaisesRegex(ValueError, "order"):
                    planner.reconcile(self.census, self.plan, results)

    def test_noncomplete_result_refuses(self):
        for status in ("interrupted", "invalid", None):
            with self.subTest(status=status):
                results = copy.deepcopy(self.results)
                results[0]["status"] = status
                with self.assertRaisesRegex(ValueError, "incomplete"):
                    planner.reconcile(self.census, self.plan, results)

    def test_absent_completion_status_refuses(self):
        del self.results[0]["status"]
        with self.assertRaisesRegex(ValueError, "incomplete"):
            self.reconcile()

    def test_missing_outcome_refuses_even_when_tests_run_matches(self):
        self.results[0]["events"].pop()
        with self.assertRaisesRegex(ValueError, "missing outcome"):
            self.reconcile()

    def test_duplicate_start_refuses(self):
        self.results[0]["started_ids"].append("m.A.a")
        with self.assertRaisesRegex(ValueError, "duplicate start"):
            self.reconcile()

    def test_native_tests_run_count_is_checked(self):
        self.results[0]["tests_run"] = 99
        with self.assertRaisesRegex(ValueError, "tests_run"):
            self.reconcile()

    def test_failure_cannot_be_hidden_by_successful_boolean(self):
        self.results[0]["events"][0]["kind"] = "failure"
        with self.assertRaisesRegex(ValueError, "successful"):
            self.reconcile()

    def test_multiple_failure_outcomes_are_preserved(self):
        self.results[0]["events"] = [
            {"kind": "subtest_failure", "test_id": "m.A.a", "subtest": "m.A.a (x=1)"},
            {"kind": "subtest_failure", "test_id": "m.A.a", "subtest": "m.A.a (x=2)"},
            {"kind": "success", "test_id": "m.A.b"},
        ]
        self.results[0]["successful"] = False
        aggregate = self.reconcile()
        self.assertFalse(aggregate["successful"])
        self.assertEqual(aggregate["events"][:2], self.results[0]["events"][:2])

    def test_native_subtest_skip_without_parent_success_is_accounted(self):
        self.results[0]["events"][0] = {"kind": "skip", "test_id": "m.A.a",
                                        "subtest": "m.A.a (x=1)", "reason": "optional"}
        self.assertTrue(self.reconcile()["successful"])

    def test_successful_subtests_still_require_native_parent_outcome(self):
        self.results[0]["events"][0] = {"kind": "subtest_success", "test_id": "m.A.a",
                                        "subtest": "m.A.a (x=1)"}
        with self.assertRaisesRegex(ValueError, "missing outcome"):
            self.reconcile()

    def test_success_cannot_duplicate_or_contradict_terminal_failure(self):
        for event in ({"kind": "success", "test_id": "m.A.a"},
                      {"kind": "failure", "test_id": "m.A.a"}):
            with self.subTest(event=event):
                results = copy.deepcopy(self.results)
                results[0]["events"].append(event)
                results[0]["successful"] = event["kind"] == "success"
                with self.assertRaisesRegex(ValueError, "outcome"):
                    planner.reconcile(self.census, self.plan, results)

    def test_expected_failure_and_unexpected_success_keep_native_meanings(self):
        self.results[0]["events"][0]["kind"] = "expected_failure"
        self.assertTrue(self.reconcile()["successful"])
        self.results[0]["events"][0]["kind"] = "unexpected_success"
        self.results[0]["successful"] = False
        self.assertFalse(self.reconcile()["successful"])

    def test_fixture_skip_accounts_for_unstarted_ids_without_invented_callbacks(self):
        result = self.results[0]
        result.update(started_ids=[], stopped_ids=[], tests_run=0,
                      fixture_blocked_ids=["m.A.a", "m.A.b"],
                      events=[{"kind": "fixture_skip", "test_id": "setUpClass (m.A)",
                               "fixture": "setUpClass (m.A)", "reason": "unavailable",
                               "blocked_ids": ["m.A.a", "m.A.b"]}])
        aggregate = self.reconcile()
        self.assertEqual(aggregate["tests_run"], 3)
        self.assertEqual(aggregate["started_ids"], ["m.B.a", "m.C.a", "n.D.a"])
        self.assertEqual(aggregate["fixture_blocked_ids"], ["m.A.a", "m.A.b"])
        self.assertTrue(aggregate["successful"])

    def test_fixture_error_is_native_failure(self):
        result = self.results[0]
        result.update(started_ids=[], stopped_ids=[], tests_run=0, successful=False,
                      fixture_blocked_ids=["m.A.a", "m.A.b"],
                      events=[{"kind": "fixture_error", "test_id": "setUpClass (m.A)",
                               "fixture": "setUpClass (m.A)", "blocked_ids": ["m.A.a", "m.A.b"]}])
        self.assertFalse(self.reconcile()["successful"])

    def test_unstarted_ids_cannot_be_hidden_as_fixture_blocked_without_fixture(self):
        self.results[0].update(started_ids=[], stopped_ids=[], tests_run=0,
                               fixture_blocked_ids=["m.A.a", "m.A.b"], events=[])
        with self.assertRaisesRegex(ValueError, "fixture"):
            self.reconcile()

    def test_fixture_blocked_id_must_belong_to_the_fixture(self):
        self.results[0].update(started_ids=[], stopped_ids=[], tests_run=0,
                               fixture_blocked_ids=["m.A.a", "m.A.b"],
                               events=[{"kind": "fixture_skip", "test_id": "setUpClass (foreign.Z)",
                                        "fixture": "setUpClass (foreign.Z)", "reason": "unavailable",
                                        "blocked_ids": ["m.A.a", "m.A.b"]}])
        with self.assertRaisesRegex(ValueError, "fixture"):
            self.reconcile()

    def test_teardown_fixture_error_preserves_executed_methods(self):
        self.results[0]["events"].append({"kind": "fixture_error",
            "test_id": "tearDownClass (m.A)", "fixture": "tearDownClass (m.A)", "blocked_ids": []})
        self.results[0]["successful"] = False
        aggregate = self.reconcile()
        self.assertFalse(aggregate["successful"])
        self.assertEqual(aggregate["tests_run"], 5)
        self.assertEqual(aggregate["fixture_blocked_ids"], [])

    def test_module_fixture_skip_blocks_exact_selected_descendants(self):
        self.results[0].update(started_ids=[], stopped_ids=[], tests_run=0,
            fixture_blocked_ids=["m.A.a", "m.A.b"], events=[{"kind": "fixture_skip",
            "test_id": "setUpModule (m)", "fixture": "setUpModule (m)",
            "reason": "module unavailable", "blocked_ids": ["m.A.a", "m.A.b"]}])
        self.assertTrue(self.reconcile()["successful"])

    def test_fixture_summary_and_event_blocked_ids_must_match(self):
        self.results[0].update(started_ids=[], stopped_ids=[], tests_run=0,
            fixture_blocked_ids=["m.A.a", "m.A.b"], events=[{"kind": "fixture_skip",
            "test_id": "setUpClass (m.A)", "fixture": "setUpClass (m.A)",
            "reason": "optional", "blocked_ids": ["m.A.a"]}])
        with self.assertRaisesRegex(ValueError, "fixture"):
            self.reconcile()

    def test_repeated_setup_and_cleanup_errors_preserve_events_and_union_blocking(self):
        result = self.results[0]
        fixture = {"kind": "fixture_error", "test_id": "setUpClass (m.A)",
                   "fixture": "setUpClass (m.A)", "blocked_ids": ["m.A.a", "m.A.b"]}
        result.update(started_ids=[], stopped_ids=[], tests_run=0, successful=False,
                      fixture_blocked_ids=["m.A.a", "m.A.b"],
                      events=[fixture, copy.deepcopy(fixture)])
        aggregate = self.reconcile()
        self.assertEqual(aggregate["events"][:2], [fixture, fixture])
        self.assertEqual(aggregate["fixture_blocked_ids"], ["m.A.a", "m.A.b"])
        self.assertFalse(aggregate["successful"])
        candidate = copy.deepcopy(aggregate)
        candidate["events"].pop(0)
        comparison = planner.compare_results(aggregate, candidate)
        self.assertFalse(comparison["equivalent"])
        self.assertEqual(comparison["differences"]["events"]["missing"], [fixture])

    def test_custom_test_ids_use_native_fixture_membership(self):
        replacements = {"m.A.a": "adapter-case-one", "m.A.b": "adapter-case-two"}
        self.census["ids"] = [replacements.get(test_id, test_id) for test_id in self.census["ids"]]
        self.census["units"][0]["ids"] = ["adapter-case-one", "adapter-case-two"]
        self.census["membership"] = {replacements.get(test_id, test_id): member
                                    for test_id, member in self.census["membership"].items()}
        self.census["population_digest"] = hashlib.sha256(json.dumps(
            self.census["ids"], ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")).hexdigest()
        self.plan = planner.make_plan(self.census, 2)
        for callback, kind, reason in (("setUpClass", "fixture_skip", "optional"),
                                      ("setUpClass", "fixture_error", None),
                                      ("tearDownClass", "fixture_error", None),
                                      ("setUpModule", "fixture_error", None)):
            self.results = [receipt(a) for a in self.plan["assignments"]]
            for result in self.results:
                result["population_digest"] = self.census["population_digest"]
            setup = callback.startswith("setUp")
            identity = "m" if callback.endswith("Module") else "m.A"
            holder = callback + " (" + identity + ")"
            fixture = {"kind": kind, "test_id": holder, "fixture": holder,
                       "blocked_ids": ["adapter-case-one", "adapter-case-two"] if setup else []}
            if reason is not None:
                fixture["reason"] = reason
            if setup:
                self.results[0].update(started_ids=[], stopped_ids=[], tests_run=0,
                    fixture_blocked_ids=["adapter-case-one", "adapter-case-two"], events=[fixture])
            else:
                self.results[0]["events"].append(fixture)
            self.results[0]["successful"] = kind == "fixture_skip"
            with self.subTest(callback=callback, kind=kind):
                aggregate = self.reconcile()
                self.assertEqual(aggregate["fixture_blocked_ids"], fixture["blocked_ids"])
                self.assertEqual(aggregate["successful"], kind == "fixture_skip")

    def test_malformed_event_kind_refuses_as_value_error(self):
        self.results[0]["events"][0]["kind"] = []
        with self.assertRaisesRegex(ValueError, "event"):
            self.reconcile()

    def test_success_cannot_hide_failed_subtest(self):
        self.results[0]["events"].append({"kind": "subtest_failure", "test_id": "m.A.a",
                                         "subtest": "m.A.a (x=1)"})
        self.results[0]["successful"] = False
        with self.assertRaisesRegex(ValueError, "outcome"):
            self.reconcile()

    def test_comparison_refuses_events_without_native_identity(self):
        reference = self.reconcile()
        candidate = copy.deepcopy(reference)
        candidate["events"][0] = {}
        with self.assertRaisesRegex(ValueError, "event"):
            planner.compare_results(reference, candidate)

    def test_foreign_event_and_unknown_kind_refuse(self):
        for event in ({"kind": "success", "test_id": "foreign"},
                      {"kind": "invented", "test_id": "m.A.a"}):
            with self.subTest(event=event):
                results = copy.deepcopy(self.results)
                results[0]["events"][0] = event
                with self.assertRaisesRegex(ValueError, "event"):
                    planner.reconcile(self.census, self.plan, results)

    def test_comparison_ignores_worker_order_but_preserves_multiplicity_and_skip_reasons(self):
        reference = self.reconcile()
        candidate = copy.deepcopy(reference)
        candidate["events"].reverse()
        candidate["started_ids"].reverse()
        candidate["stopped_ids"].reverse()
        self.assertTrue(planner.compare_results(reference, candidate)["equivalent"])
        candidate["events"].append(candidate["events"][0])
        difference = planner.compare_results(reference, candidate)
        self.assertFalse(difference["equivalent"])
        self.assertIn("events", difference["differences"])
        reference["events"][0] = {"kind": "skip", "test_id": "m.A.a", "reason": "optional library"}
        candidate = copy.deepcopy(reference)
        candidate["events"][0]["reason"] = "another library"
        self.assertFalse(planner.compare_results(reference, candidate)["equivalent"])

    def test_comparison_reports_execution_and_population_mismatch(self):
        reference = self.reconcile()
        candidate = copy.deepcopy(reference)
        candidate["started_ids"].pop()
        candidate["population_digest"] = "different"
        comparison = planner.compare_results(reference, candidate)
        self.assertFalse(comparison["equivalent"])
        self.assertIn("started_ids", comparison["differences"])
        self.assertIn("population_digest", comparison["differences"])


if __name__ == "__main__":
    unittest.main()

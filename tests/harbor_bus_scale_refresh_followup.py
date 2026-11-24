"""Am3 follow-up admission controls; supplied data is never speed evidence.

These controls invoke only pure comparison/admission helpers. They launch no
census, waiter, performance child or clock. Native measurements remain owned by
the closed runner after its independent RED/GREEN qualification.
"""
from __future__ import annotations

import copy
import importlib.util
import io
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

from tests import bus_scale_metrics as metrics


ROOT = Path(__file__).resolve().parents[1]
TARGETS = (
    ("registry", "readiness-read", "cpu_seconds"),
    ("registry", "receipt-append", "ledger_hold_seconds"),
    ("registry", "log-history", "ledger_hold_seconds"),
    ("deliveries", "wait-start", "ledger_hold_seconds"),
    ("deliveries", "status", "cpu_seconds"),
)


def load_script(name):
    path = ROOT / "tests/fixtures/bus_scale_census" / (name + "-2026-10-09.py")
    spec = importlib.util.spec_from_file_location("refresh_controls_" + name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def pairs(samples):
    """Exact numeric comparator inputs, not simulated native execution."""
    return [{"order": [1, 10] if index % 2 == 0 else [10, 1],
             "1": dict(cpu_seconds=small, read_bytes=small,
                       ledger_hold_seconds=small),
             "10": dict(cpu_seconds=large, read_bytes=large,
                        ledger_hold_seconds=large)}
            for index, (small, large) in enumerate(samples)]


class PerPairFollowupTests(unittest.TestCase):
    def verdict(self, samples, expected, *, history=False):
        supplied = pairs(samples)
        saved = copy.deepcopy(supplied)
        result = metrics.compare_pairs(supplied, history_output=history)
        self.assertEqual(saved, supplied)
        for name, row in result["metrics"].items():
            with self.subTest(metric=name):
                self.assertEqual("info" if history and name != "ledger_hold_seconds"
                                 else expected, row["classification"])
                self.assertEqual([large / small for small, large in samples],
                                 row.get("pair_ratios"),
                                 "retain every pair ratio behind the ruled verdict")
        return result

    def test_four_red_pairs_remain_red_when_one_heavy_pass_reverses_aggregate(self):
        self.verdict([(1, 2)] * 4 + [(10000, 10000)], "red")

    def test_four_threshold_passes_remain_pass_when_one_red_outlier_dominates(self):
        self.verdict([(2, 3)] * 4 + [(10000, 1000000)], "pass")

    def test_three_two_split_stays_ambiguous_even_when_aggregate_exceeds_budget(self):
        self.verdict([(1, 2)] * 3 + [(1, 1)] * 2, "ambiguous")

    def test_history_output_does_not_exempt_hold_from_pairwise_ambiguity(self):
        self.verdict([(1, 2)] * 3 + [(1, 1)] * 2, "ambiguous", history=True)

    def test_two_pair_refresh_keeps_symmetric_threshold_and_disagreement(self):
        for samples, expected in (([(2, 3), (2, 3)], "pass"),
                                  ([(1, 2), (1, 2)], "red"),
                                  ([(1, 2), (1, 1)], "ambiguous")):
            with self.subTest(samples=samples):
                result = metrics.compare_first_pass(pairs(samples))
                self.assertTrue(all(row["classification"] == expected
                                    for row in result["metrics"].values()))


class ClosedFollowupAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.coordinator = load_script("census-bus-scale")
        self.runner = load_script("run-bus-scale-census")

    def api(self, owner, name):
        value = getattr(owner, name, None)
        self.assertTrue(callable(value),
                        "missing ruled closed follow-up admission: " + name)
        return value

    def selection(self):
        return {"observations": [dict(family=family, case=case, metric=metric)
                                 for family, case, metric in TARGETS],
                "qualification_companions": [dict(family="deliveries", case="wait-idle",
                                                   for_case="wait-start")],
                "pairs": 5, "deadline_seconds": 3600}

    def test_selector_admits_exact_five_and_keeps_idle_companion_unmeasured(self):
        select = self.api(self.coordinator, "followup_selection")
        validate = self.api(self.coordinator, "validate_followup_selection")
        plan = select()
        self.assertEqual(self.selection(), plan)
        saved = copy.deepcopy(plan)
        validate(plan)
        self.assertEqual(saved, plan)
        plan["observations"].clear()
        self.assertEqual(self.selection(), select(), "caller cannot alter the closed selector")

    def test_selector_refuses_missing_extra_duplicate_or_companion_promotion(self):
        validate = self.api(self.coordinator, "validate_followup_selection")
        variants = []
        value = self.selection()
        value["observations"].pop()
        variants.append(value)
        value = self.selection()
        value["observations"].append(dict(family="events", case="doctor", metric="cpu_seconds"))
        variants.append(value)
        value = self.selection()
        value["observations"][-1] = copy.deepcopy(value["observations"][0])
        variants.append(value)
        value = self.selection()
        value["observations"].append(dict(family="deliveries", case="wait-idle", metric="cpu_seconds"))
        variants.append(value)
        for field, replacement in (("pairs", 2), ("pairs", True),
                                   ("deadline_seconds", 3601),
                                   ("qualification_companions", [])):
            value = self.selection()
            value[field] = replacement
            variants.append(value)
        for value in variants:
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate(value)

    def test_targeted_route_has_exact_3600_second_cap_and_no_unknown_default(self):
        deadline = self.api(self.runner, "action_deadline_seconds")
        self.assertEqual(3600, deadline("census-followup"))
        self.assertEqual(7200, deadline("census-first-pass"))
        with self.assertRaises(ValueError):
            deadline("census-followup-arbitrary")

    def test_actual_runner_dispatch_passes_targeted_cap_to_child_collection(self):
        with tempfile.TemporaryDirectory(dir=self.runner.TEMP,
                                         prefix="bus-scale-route-controls-") as temporary:
            launched, collected = [], []

            def launch(argv, **kwargs):
                launched.append(argv)
                Path(argv[-1]).write_text(json.dumps({"native_home_removed": True}))
                return SimpleNamespace(pid=12345)

            def collect(child, **kwargs):
                collected.append(kwargs)
                return dict(status="deadline", exit_code=1,
                            child_collected=True, interrupted=True)

            with mock.patch.object(self.runner, "TEMP", Path(temporary)),\
                    mock.patch.object(self.runner, "launch_owned_child", launch),\
                    mock.patch.object(self.runner, "collect_owned_child", collect),\
                    mock.patch.object(self.runner, "source", return_value={"head": "routing-control"}),\
                    mock.patch.object(self.runner, "source_population", return_value=()),\
                    mock.patch("sys.argv", [str(self.runner.SELF), "census-followup", "dispatch"]),\
                    redirect_stdout(io.StringIO()):
                try:
                    code = self.runner.main()
                except ValueError as error:
                    self.fail("actual runner rejects the ruled targeted route: " + str(error))
            self.assertEqual(1, code)
            self.assertEqual(1, len(launched))
            self.assertEqual(["--child", "census-followup"], launched[0][-3:-1])
            self.assertEqual(3600, collected[0]["deadline_seconds"])
            receipt = json.loads((Path(temporary) / (self.runner.PREFIX + "dispatch")
                                  / "receipt.json").read_text())
            self.assertEqual("deadline", receipt["status"])
            self.assertTrue(receipt["child_collected"])
            self.assertTrue(receipt["scratch_removed"])

    def test_actual_coordinator_routes_only_targets_and_native_idle_companions(self):
        # The routing control uses synthetic seed, MCP and observation inputs.
        # Live MCP coverage has its own control in the population module.
        from tests import bus_scale_commands as commands, bus_scale_workloads as workloads

        inventory = commands._baseline_contract()
        scope = dict(inventory=inventory, cases=copy.deepcopy(workloads._CASES),
                     excluded=workloads._exclusions(inventory, commands.classify_contract(inventory)))
        calls = []

        class RoutingComplete(Exception):
            pass

        def seed_pair(base):
            result = {}
            for family in ("events", "registry", "exhaustion", "deliveries"):
                result[family] = {"ledger_relative": "history.jsonl"}
                for scale in (1, 10):
                    home = base / family / str(scale) / "scale-fleet"
                    home.mkdir(parents=True)
                    (home / "history.jsonl").write_bytes(b"{}\n" * scale)
                    result[family][str(scale)] = home
            return result

        def observe(owner, case, seed, relative, workspace, place):
            family, scale = seed.parent.parent.name, int(seed.parent.name)
            calls.append((family, case, scale))
            if len(calls) == 60:
                raise RoutingComplete("complete closed population observed; no verdict requested")
            for mode in ("cpu", "diagnostic"):
                target = place / mode
                target.mkdir(parents=True, exist_ok=True)
                (target / "detail.json").write_text("{}")
            native_code, native_status, native_effect = workloads._NATIVE[case]
            return dict(records=100 * scale, bytes=102400 * scale,
                        seed_sha256="a" * 64, clone_sha256="a" * 64,
                        raw_sha256="b" * 64, child_collected=True,
                        native=dict(exit_code=native_code, status=native_status,
                                    effect=dict(kind=native_effect, count=1)),
                        coverage=dict(cpu=True, bytes=True, locks=True),
                        metrics=dict(cpu_seconds=1, bytes_read=1, max_ledger_lock_seconds=1))

        with tempfile.TemporaryDirectory(dir=self.coordinator.TEMP,
                                         prefix="bus-scale-census-2026-10-09-route-") as temporary:
            with mock.patch("tests.bus_scale_seeds.make_seed_pair", seed_pair),\
                    mock.patch("tests.bus_scale_workloads.scope_contract", return_value=scope),\
                    mock.patch.object(workloads, "mcp_tool_bindings", return_value={}),\
                    mock.patch.object(self.coordinator, "prepare"),\
                    mock.patch.object(self.coordinator, "owner_module", return_value=object()),\
                    mock.patch.object(self.coordinator, "observation", observe),\
                    mock.patch.object(self.coordinator, "qualify_wait_start", return_value={"qualified": True}),\
                    mock.patch("sys.argv", ["census", "followup", temporary]),\
                    redirect_stdout(io.StringIO()):
                try:
                    with self.assertRaises(RoutingComplete):
                        self.coordinator.main()
                except ValueError as error:
                    self.fail("actual coordinator rejects the closed follow-up population: " + str(error))
        expected = {(family, case) for family, case, _metric in TARGETS}
        expected.add(("deliveries", "wait-idle"))
        self.assertEqual(expected, {(family, case) for family, case, scale in calls})
        for family, case in expected:
            self.assertEqual([1, 10, 10, 1, 1, 10, 10, 1, 1, 10],
                             [scale for selected_family, selected_case, scale in calls
                              if (selected_family, selected_case) == (family, case)])

    def test_deadline_or_uncollected_attempt_is_typed_incomplete_without_verdict(self):
        admission = self.api(self.coordinator, "followup_admission")
        for status, collected, code in (("deadline", True, "followup_deadline"),
                                        ("uncollected", False, "followup_child_uncollected")):
            record = dict(status=status, child_collected=collected,
                          selection=self.selection(), observations=[],
                          qualification_companions=[])
            saved = copy.deepcopy(record)
            result = admission(record)
            self.assertEqual("incomplete", result.get("status"))
            self.assertEqual(code, result.get("code"))
            for field in ("comparisons", "red_map", "governing_observations"):
                self.assertNotIn(field, result, "unfinished follow-up cannot emit a verdict")
            self.assertEqual(saved, record)
            self.assertNotEqual(0, self.runner.attempt_exit_code(record))

    def test_incomplete_followup_preserves_finished_coordinate_and_raw_partial_pairs(self):
        admission = self.api(self.coordinator, "followup_admission")
        complete = dict(family="registry", case="readiness-read", metric="cpu_seconds",
                        pairs=pairs([(1, 2)] * 5))
        partial = dict(family="deliveries", case="status", metric="cpu_seconds",
                       pairs=pairs([(1, 2)] * 2))
        record = dict(status="deadline", child_collected=True,
                      selection=self.selection(), observations=[complete, partial],
                      qualification_companions=[])
        saved = copy.deepcopy(record)
        result = admission(record)
        self.assertEqual("incomplete", result.get("status"))
        self.assertEqual("followup_deadline", result.get("code"))
        verdicts = result.get("comparisons", [])
        self.assertEqual(1, len(verdicts))
        self.assertEqual(("registry", "readiness-read", "cpu_seconds"),
                         tuple(verdicts[0][key] for key in ("family", "case", "metric")))
        self.assertEqual("red", verdicts[0]["classification"])
        self.assertEqual(saved["observations"], result.get("observations"))
        self.assertEqual(saved, record)

    def test_full_red_map_keeps_governing_aggregate_distinct_from_max_pair_ratio(self):
        from tests import bus_scale_commands as commands, bus_scale_workloads as workloads

        inventory = commands._baseline_contract()
        scope = dict(inventory=inventory, cases=copy.deepcopy(workloads._CASES),
                     excluded=workloads._exclusions(inventory, commands.classify_contract(inventory)))
        matrix = {}
        for family in self.coordinator.FAMILIES:
            matrix[family] = []
            for index, (small, large) in enumerate([(1, 2)] * 4 + [(10000, 10000)]):
                rows = {}
                for case in scope["cases"]:
                    code, status, effect = workloads._NATIVE[case]
                    arms = {}
                    for scale, multiplier, value in (("1x", 1, small), ("10x", 10, large)):
                        native = dict(exit_code=code, status=status, effect=dict(kind=effect, count=1))
                        if case == "doctor":
                            native["findings_sha256"] = "c" * 64
                        arms[scale] = dict(records=100 * multiplier, bytes=102400 * multiplier,
                            seed_sha256="a" * 64, clone_sha256="a" * 64, raw_sha256="b" * 64,
                            child_collected=True, native=native, coverage=dict(cpu=True, bytes=True, locks=True),
                            metrics=dict(cpu_seconds=value, bytes_read=value, max_ledger_lock_seconds=value))
                    if case in {"supervise", "watch"}:
                        from tests.bus_scale_attention_samples import attention_sample
                        for scale, arm in arms.items():
                            arm["attention_proof"] = attention_sample(case, scale)
                    rows[case] = dict(family=family, prefix_exact=True, selected_work_equal=True, arms=arms)
                matrix[family].append(dict(order=[1, 10] if index % 2 == 0 else [10, 1], observations=rows))
        result = self.coordinator.validate_matrix(scope, matrix)
        self.assertTrue(result["red_map"])
        for row in result["red_map"]:
            with self.subTest(command=row["subcommand"], metric=row["metric"]):
                self.assertLessEqual(row["ratio"], 1.5,
                    "the RED-map aggregate must survive the reversed majority verdict")
                self.assertEqual(2, row["max_pair_ratio"])


if __name__ == "__main__":
    unittest.main()

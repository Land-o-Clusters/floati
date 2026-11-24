"""Controls for paired aggregation and a contract-bound nonexpanding RED map."""

from __future__ import annotations

import copy
from functools import lru_cache
import importlib
import math
import unittest

from floati.cli import _parser
from tests.bus_scale_commands import executable_contract


try:
    _metrics = importlib.import_module("tests.bus_scale_metrics")
except ModuleNotFoundError as exc:
    if exc.name != "tests.bus_scale_metrics":
        raise
    _metrics = None


_METRICS = ("cpu_seconds", "read_bytes", "ledger_hold_seconds")


def _pairs(cpu: float = 1.0, read: int = 100, hold: float = 1.0) -> list[dict]:
    return [
        {
            "order": [1, 10] if index % 2 == 0 else [10, 1],
            "1": {"cpu_seconds": 1.0, "read_bytes": 100, "ledger_hold_seconds": 1.0},
            "10": {"cpu_seconds": cpu, "read_bytes": read, "ledger_hold_seconds": hold},
        }
        for index in range(5)
    ]


@lru_cache(maxsize=1)
def _metric_contract_template():
    return executable_contract(_parser())


class BusScaleMetricTests(unittest.TestCase):
    def setUp(self) -> None:
        self.assertIsNotNone(
            _metrics,
            "missing pure metric helper tests.bus_scale_metrics: implement "
            "compare_pairs, derive_red_map and require_red_nonexpansion",
        )
        for name in ("compare_pairs", "derive_red_map", "require_red_nonexpansion"):
            self.assertTrue(
                callable(getattr(_metrics, name, None)),
                f"missing metric helper function tests.bus_scale_metrics.{name}",
            )
        self.contracts = copy.deepcopy(_metric_contract_template())

    def _row(
        self, metric: str = "cpu_seconds", ratio: float = 2.0,
        classification: str = "red", subcommand: str = "send",
    ) -> dict:
        return {
            "subcommand": subcommand,
            "metric": metric,
            "ratio": ratio,
            "classification": classification,
            "contract": copy.deepcopy(self.contracts[subcommand]),
        }

    def test_five_alternating_pairs_at_exact_threshold_pass(self) -> None:
        pairs = _pairs(cpu=1.5, read=150, hold=1.5)
        before = copy.deepcopy(pairs)
        result = _metrics.compare_pairs(pairs)
        self.assertEqual(before, pairs)
        self.assertEqual({"metrics"}, set(result))
        self.assertEqual(set(_METRICS), set(result["metrics"]))
        for metric in _METRICS:
            with self.subTest(metric=metric):
                row = result["metrics"][metric]
                self.assertEqual({"1", "10", "ratio", "classification", "aggregate_classification",
                                  "pair_ratios", "pair_classifications"}, set(row))
                self.assertEqual(1.5, row["ratio"])
                self.assertEqual("pass", row["classification"])

    def test_ruled_two_pair_first_pass_keeps_both_ratios_and_consistent_verdicts(self):
        result = _metrics.compare_first_pass(_pairs(cpu=2, read=200, hold=2)[:2])
        for row in result["metrics"].values():
            self.assertEqual("red", row["classification"])
            self.assertEqual([2.0, 2.0], row["pair_ratios"])
        flat = _metrics.compare_first_pass(_pairs(cpu=1.5, read=150, hold=1.5)[:2])
        self.assertTrue(all(row["classification"] == "pass" for row in flat["metrics"].values()))

    def test_disagreeing_first_pass_pairs_are_ambiguous_even_when_aggregate_is_red(self):
        pairs = _pairs(cpu=8, read=800, hold=8)[:2]
        pairs[1]["10"] = dict(pairs[1]["1"])
        result = _metrics.compare_first_pass(pairs)
        self.assertTrue(all(row["classification"] == "ambiguous" for row in result["metrics"].values()))

    def test_first_pass_does_not_admit_one_or_five_pairs_or_unbalanced_order(self):
        for pairs in (_pairs()[:1], _pairs(), [dict(row, order=[1, 10]) for row in _pairs()[:2]]):
            with self.subTest(count=len(pairs)), self.assertRaises(ValueError):
                _metrics.compare_first_pass(pairs)

    def test_first_pass_history_cpu_and_bytes_info_never_waives_lock_ambiguity(self):
        pairs = _pairs(cpu=8, read=800, hold=8)[:2]
        pairs[1]["10"] = dict(pairs[1]["1"])
        rows = _metrics.compare_first_pass(pairs, history_output=True)["metrics"]
        self.assertEqual("info", rows["cpu_seconds"]["classification"])
        self.assertEqual("info", rows["read_bytes"]["classification"])
        self.assertEqual("ambiguous", rows["ledger_hold_seconds"]["classification"])

    def test_cpu_and_bytes_sum_all_repeats_while_hold_uses_maximum(self) -> None:
        pairs = _pairs()
        for index, pair in enumerate(pairs):
            pair["1"] = {
                "cpu_seconds": 100.0 if index == 0 else 1.0,
                "read_bytes": 1000 if index == 0 else 10,
                "ledger_hold_seconds": 10.0 if index == 0 else 1.0,
            }
            pair["10"] = {
                "cpu_seconds": 100.0 if index == 0 else 2.0,
                "read_bytes": 1000 if index == 0 else 20,
                "ledger_hold_seconds": 14.0 if index == 0 else 2.0,
            }
        rows = _metrics.compare_pairs(pairs)["metrics"]
        self.assertEqual((104.0, 108.0), (rows["cpu_seconds"]["1"], rows["cpu_seconds"]["10"]))
        self.assertEqual((1040, 1080), (rows["read_bytes"]["1"], rows["read_bytes"]["10"]))
        self.assertEqual((10.0, 14.0), (rows["ledger_hold_seconds"]["1"], rows["ledger_hold_seconds"]["10"]))
        self.assertAlmostEqual(108.0 / 104.0, rows["cpu_seconds"]["ratio"])
        self.assertAlmostEqual(1080 / 1040, rows["read_bytes"]["ratio"])
        self.assertEqual(1.4, rows["ledger_hold_seconds"]["ratio"])
        self.assertTrue(all(row["classification"] == "red" for row in rows.values()))

    def test_any_exactly_above_threshold_metric_is_red_without_epsilon(self) -> None:
        above = math.nextafter(1.5, math.inf)
        rows = _metrics.compare_pairs(_pairs(cpu=above, read=151, hold=above))["metrics"]
        for metric in _METRICS:
            with self.subTest(metric=metric):
                self.assertGreater(rows[metric]["ratio"], 1.5)
                self.assertEqual("red", rows[metric]["classification"])

    def test_missing_or_extra_pair_is_unresolved(self) -> None:
        for pairs in (_pairs()[:4], _pairs() + _pairs()[:1], []):
            with self.subTest(count=len(pairs)):
                with self.assertRaises(ValueError):
                    _metrics.compare_pairs(pairs)

    def test_order_must_follow_the_predeclared_alternation(self) -> None:
        for index, order in ((0, [10, 1]), (1, [1, 10]), (2, [True, 10]), (4, [1, 10, 1])):
            with self.subTest(index=index, order=order):
                pairs = _pairs()
                pairs[index]["order"] = order
                with self.assertRaises(ValueError):
                    _metrics.compare_pairs(pairs)

    def test_missing_or_extra_arm_or_metric_is_unresolved(self) -> None:
        for mutation in ("missing_arm", "extra_arm", "missing_metric", "extra_metric"):
            with self.subTest(mutation=mutation):
                pairs = _pairs()
                if mutation == "missing_arm":
                    del pairs[0]["10"]
                elif mutation == "extra_arm":
                    pairs[0]["100"] = copy.deepcopy(pairs[0]["10"])
                elif mutation == "missing_metric":
                    del pairs[0]["1"]["read_bytes"]
                else:
                    pairs[0]["10"]["other_metric"] = 1.0
                with self.assertRaises(ValueError):
                    _metrics.compare_pairs(pairs)

    def test_invalid_metric_values_are_not_performance_verdicts(self) -> None:
        cases = {
            "cpu_seconds": (True, False, math.nan, math.inf, -math.inf, -1.0, 0.0, "1"),
            "read_bytes": (True, False, 0.0, 1.5, math.nan, math.inf, -1, "1"),
            "ledger_hold_seconds": (True, False, math.nan, math.inf, -math.inf, -1.0, "1"),
        }
        for metric, values in cases.items():
            for arm in ("1", "10"):
                for value in values:
                    with self.subTest(metric=metric, arm=arm, value=value):
                        pairs = _pairs()
                        pairs[3][arm][metric] = value
                        with self.assertRaises(ValueError):
                            _metrics.compare_pairs(pairs)

    def test_nonfinite_aggregate_or_ratio_is_unresolved(self) -> None:
        overflow_sum = _pairs(cpu=1e308)
        overflow_ratio = _pairs(cpu=1e300)
        for pair in overflow_ratio:
            pair["1"]["cpu_seconds"] = 1e-300
        for pairs in (overflow_sum, overflow_ratio):
            with self.subTest(pairs=pairs):
                with self.assertRaises(ValueError):
                    _metrics.compare_pairs(pairs)

    def test_verified_zero_bytes_and_zero_hold_have_unit_ratio(self) -> None:
        pairs = _pairs()
        for pair in pairs:
            for arm in ("1", "10"):
                pair[arm]["read_bytes"] = 0
                pair[arm]["ledger_hold_seconds"] = 0.0
        rows = _metrics.compare_pairs(pairs)["metrics"]
        for metric in ("read_bytes", "ledger_hold_seconds"):
            self.assertEqual(1.0, rows[metric]["ratio"])
            self.assertEqual("pass", rows[metric]["classification"])

    def test_zero_to_positive_bytes_or_hold_is_unresolved(self) -> None:
        for metric in ("read_bytes", "ledger_hold_seconds"):
            with self.subTest(metric=metric):
                pairs = _pairs()
                for pair in pairs:
                    pair["1"][metric] = 0
                with self.assertRaises(ValueError):
                    _metrics.compare_pairs(pairs)

    def test_positive_to_zero_bytes_or_hold_has_zero_ratio(self) -> None:
        rows = _metrics.compare_pairs(_pairs(read=0, hold=0.0))["metrics"]
        for metric in ("read_bytes", "ledger_hold_seconds"):
            self.assertEqual(0.0, rows[metric]["ratio"])
            self.assertEqual("pass", rows[metric]["classification"])

    def test_explicit_history_output_marks_only_cpu_and_bytes_info(self) -> None:
        rows = _metrics.compare_pairs(_pairs(cpu=3.0, read=900, hold=2.0), history_output=True)["metrics"]
        self.assertEqual("info", rows["cpu_seconds"]["classification"])
        self.assertEqual("info", rows["read_bytes"]["classification"])
        self.assertEqual("red", rows["ledger_hold_seconds"]["classification"])
        self.assertEqual(2.0, rows["ledger_hold_seconds"]["ratio"])
        pass_hold = _metrics.compare_pairs(_pairs(hold=1.5), history_output=True)["metrics"]
        self.assertEqual("pass", pass_hold["ledger_hold_seconds"]["classification"])

    def test_history_output_requires_an_actual_boolean(self) -> None:
        for value in (1, 0, "true", None, [], {}):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    _metrics.compare_pairs(_pairs(), history_output=value)

    def test_red_map_is_derived_exactly_from_red_rows(self) -> None:
        rows = [
            self._row("cpu_seconds", 2.0),
            self._row("read_bytes", 1.5, "pass"),
            self._row("read_bytes", 9.0, "info", "log"),
            self._row("ledger_hold_seconds", 1.75, "red", "log"),
        ]
        before = copy.deepcopy(rows)
        self.assertEqual(
            {("send", "cpu_seconds"): 2.0, ("log", "ledger_hold_seconds"): 1.75},
            _metrics.derive_red_map(rows, self.contracts),
        )
        self.assertEqual(before, rows)

    def test_duplicate_metric_rows_refuse_even_when_identical_or_not_red(self) -> None:
        for row in (self._row(), self._row(ratio=1.0, classification="pass")):
            with self.subTest(classification=row["classification"]):
                with self.assertRaises(ValueError):
                    _metrics.derive_red_map([row, copy.deepcopy(row)], self.contracts)

    def test_ratio_and_classification_must_agree_and_hold_cannot_be_info(self) -> None:
        for row in (
            self._row(ratio=1.5, classification="red"),
            self._row(ratio=1.5000000000000002, classification="pass"),
            self._row("ledger_hold_seconds", 2.0, "info"),
            self._row(classification="unknown"),
        ):
            with self.subTest(row=row):
                with self.assertRaises(ValueError):
                    _metrics.derive_red_map([row], self.contracts)

    def test_red_map_rejects_unknown_metrics_and_nonfinite_or_boolean_ratios(self) -> None:
        for ratio in (True, False, math.nan, math.inf, -math.inf, -1.0, "2"):
            with self.subTest(ratio=ratio):
                with self.assertRaises(ValueError):
                    _metrics.derive_red_map([self._row(ratio=ratio)], self.contracts)
        with self.assertRaises(ValueError):
            _metrics.derive_red_map([self._row(metric="unreviewed_metric")], self.contracts)

    def test_unknown_or_stale_contract_cannot_enter_red_map(self) -> None:
        unknown = self._row()
        unknown["subcommand"] = "scale-probe"
        stale = self._row()
        stale["contract"]["arguments"][0]["default"] = "changed-default"
        typed_drift = self._row()
        self.assertIs(typed_drift["contract"]["public"], True)
        typed_drift["contract"]["public"] = 1
        for row in (unknown, stale, typed_drift):
            with self.subTest(row=row):
                with self.assertRaises(ValueError):
                    _metrics.derive_red_map([row], self.contracts)

    def test_incomplete_or_extra_red_row_fields_refuse(self) -> None:
        missing = self._row()
        del missing["contract"]
        extra = self._row()
        extra["waive"] = True
        for row in (missing, extra):
            with self.subTest(row=row):
                with self.assertRaises(ValueError):
                    _metrics.derive_red_map([row], self.contracts)

    def test_red_nonexpansion_allows_removal_shrink_or_equal_ratios(self) -> None:
        baseline = {("send", "cpu_seconds"): 3.0, ("doctor", "read_bytes"): 4.0}
        for current in (
            {},
            {("send", "cpu_seconds"): 2.0},
            copy.deepcopy(baseline),
        ):
            with self.subTest(current=current):
                _metrics.require_red_nonexpansion(baseline, current)

    def test_new_command_metric_or_increased_ratio_expands_red(self) -> None:
        baseline = {("send", "cpu_seconds"): 2.0}
        for current in (
            {("doctor", "cpu_seconds"): 2.0},
            {("send", "read_bytes"): 2.0},
            {("send", "cpu_seconds"): math.nextafter(2.0, math.inf)},
        ):
            with self.subTest(current=current):
                with self.assertRaises(ValueError):
                    _metrics.require_red_nonexpansion(baseline, current)

    def test_nonexpansion_refuses_invalid_baseline_and_current_red_maps(self) -> None:
        valid = {("send", "cpu_seconds"): 2.0}
        for invalid in (
            {("send", "cpu_seconds"): True},
            {("send", "cpu_seconds"): math.nan},
            {("send", "cpu_seconds"): math.inf},
            {("send", "cpu_seconds"): 1.5},
            {("send", "unreviewed_metric"): 2.0},
            {"send": 2.0},
        ):
            for baseline, current in ((invalid, valid), (valid, invalid)):
                with self.subTest(baseline=baseline, current=current):
                    with self.assertRaises(ValueError):
                        _metrics.require_red_nonexpansion(baseline, current)


if __name__ == "__main__":
    unittest.main()

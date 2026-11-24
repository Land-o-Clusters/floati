"""Complete matrix controls; synthetic rows prove admission, not performance."""
from __future__ import annotations
import copy
from tests.bus_scale_test_profile import clone_fixture
from functools import lru_cache
import importlib.util
from pathlib import Path
import unittest
from tests import harbor_bus_scale_workloads as workload_controls

PATH = Path(__file__).resolve().parents[1] / "tests/fixtures/bus_scale_census/census-bus-scale-2026-10-09.py"


@lru_cache(maxsize=1)
def _matrix_template():
    specification = importlib.util.spec_from_file_location("bus_scale_matrix_subject", PATH)
    subject = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(subject)
    fixture = workload_controls.BusScaleWorkloadTests()
    fixture.setUp()
    matrix = {}
    for family in ("events", "registry", "wait-exhaustion", "deliveries"):
        observations = fixture.observations()
        for value in observations.values():
            value["family"] = family
        matrix[family] = [
            {"order": [1, 10] if index % 2 == 0 else [10, 1],
             "observations": clone_fixture(observations)}
            for index in range(5)
        ]
    return subject, fixture.scope, matrix


class BusScaleMatrixTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(PATH.exists(), "missing owned complete census producer")
        self.subject, scope, matrix = _matrix_template()
        self.assertTrue(callable(getattr(self.subject, "validate_matrix", None)),
                        "matrix must verify every case, family and balanced repeat")
        self.scope, self.matrix = clone_fixture((scope, matrix))

    def reject(self, matrix):
        with self.assertRaises(ValueError):
            self.subject.validate_matrix(self.scope, matrix)

    def red_keys(self):
        # Derive the expected union from the reviewed workload population.
        # Full-history CPU/bytes are INFO only for their own cases.
        return {(self.subject.NATIVE_BINDINGS[case], metric)
                for case, row in self.scope["cases"].items()
                for metric in ("cpu_seconds", "read_bytes", "ledger_hold_seconds")
                if not row["history_output"] or metric == "ledger_hold_seconds"}

    def test_complete_matrix_derives_every_case_family_metric_and_native_red(self):
        result = self.subject.validate_matrix(self.scope, self.matrix)
        self.assertEqual(len(self.matrix) * len(self.scope["cases"]), len(result["comparisons"]))
        self.assertEqual({(family, case) for family in self.matrix for case in self.scope["cases"]},
                         {(row["family"], row["case"]) for row in result["comparisons"]})
        self.assertEqual(3 * len(set(self.subject.NATIVE_BINDINGS[case]
                                    for case in self.scope["cases"])), len(result["native_metric_rows"]))
        self.assertEqual(self.red_keys(), {(row["subcommand"], row["metric"]) for row in result["red_map"]})

    def test_ruled_first_pass_requires_two_complete_balanced_pairs_in_every_family(self):
        matrix = {family: rows[:2] for family, rows in self.matrix.items()}
        result = self.subject.validate_matrix(self.scope, matrix, first_pass=True)
        self.assertEqual(len(self.matrix) * len(self.scope["cases"]), len(result["comparisons"]))
        self.assertEqual([], result["ambiguous_observations"])
        self.assertEqual(self.red_keys(), {(row["subcommand"], row["metric"]) for row in result["red_map"]})
        matrix["events"] = matrix["events"][:1]
        with self.assertRaises(ValueError):
            self.subject.validate_matrix(self.scope, matrix, first_pass=True)

    def test_missing_or_extra_family_does_not_pass_primary_only_coverage(self):
        for family in self.matrix:
            changed = dict(self.matrix)
            del changed[family]
            self.reject(changed)
        changed = dict(self.matrix)
        changed["unreviewed-family"] = changed["events"]
        self.reject(changed)

    def test_every_family_requires_exactly_five_balanced_pairs(self):
        for family in self.matrix:
            for count in (0, 1, 4, 6):
                changed = dict(self.matrix)
                changed[family] = (changed[family] * 2)[:count]
                self.reject(changed)
            changed = dict(self.matrix)
            changed[family] = list(changed[family])
            changed[family][1] = dict(changed[family][1])
            changed[family][1]["order"] = [1, 10]
            self.reject(changed)

    def test_no_case_may_be_omitted_or_substituted_in_one_repeat(self):
        changed = clone_fixture(self.matrix)
        del changed["registry"][4]["observations"]["readiness-read"]
        self.reject(changed)
        changed = clone_fixture(self.matrix)
        changed["deliveries"][0]["observations"]["invented"] = changed["deliveries"][0]["observations"]["send"]
        self.reject(changed)

    def test_uncollected_native_or_partial_diagnostic_never_enters_red_map(self):
        for field, value in (("child_collected", False), ("coverage", {"cpu": True, "bytes": True, "locks": False})):
            changed = clone_fixture(self.matrix)
            changed["wait-exhaustion"][2]["observations"]["receipt-append"]["arms"]["10x"][field] = value
            self.reject(changed)

    def test_case_family_mismatch_cannot_be_relabelled_as_coverage(self):
        changed = clone_fixture(self.matrix)
        changed["registry"][0]["observations"]["doctor"]["family"] = "events"
        self.reject(changed)


if __name__ == "__main__":
    unittest.main()

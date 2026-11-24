"""Run the real-limit teardown matrix after changes to teardown, jsonl or the
prefix banks, and before a release. Select CapacityMutationEvidence separately
to reproduce the two boundary mutations without paying for the cold send index.
"""
import inspect
import json
import os
import time
import unittest
from unittest.mock import patch

from floati import jsonl, seat_teardown_capacity
from floati.send_prefix import CACHE_NAME
from tests import test_seat_teardown_scale as scale


class _TimedCase:
    def run(self, result=None):
        start_load = os.getloadavg()
        started = time.monotonic()
        try:
            return super().run(result)
        finally:
            print(json.dumps({"test": self.id(), "wall_seconds": time.monotonic() - started,
                              "load_before": start_load, "load_after": os.getloadavg()}), flush=True)


class RealLimitBoundaryTests(_TimedCase, scale.SeatTeardownScaleTests):
    boundary_limit = None


class ColdIndexNativeTests(_TimedCase, scale.SeatTeardownNativeScaleTests):
    cold_send_index = True


def load_tests(loader, standard_tests, pattern):
    suite = unittest.TestSuite(RealLimitBoundaryTests(name) for name in scale.BOUNDARY_CASES)
    suite.addTests(loader.loadTestsFromTestCase(ColdIndexNativeTests))
    return suite


class CapacityMutationEvidence(unittest.TestCase):
    """Run each original boundary assertion with a one-expression fault."""

    def check_mutation(self, function_name, original, mutation, test_name, assertion):
        function = getattr(seat_teardown_capacity, function_name)
        source = inspect.getsource(function)
        self.assertEqual(1, source.count(original))
        # The mutated code uses the live module globals, so the scoped limit
        # patch and native capacity checks apply exactly as in the real test.
        changed = {}
        exec(compile(source.replace(original, mutation), inspect.getsourcefile(function), "exec"),
             function.__globals__, changed)
        for case_type in (scale.SeatTeardownScaleTests, RealLimitBoundaryTests):
            case = case_type(test_name)
            with self.subTest(case=case.id()):
                set_up = case.setUp
                def prepare():
                    set_up()
                    # An under-reserved report reaches its real atomic writer.
                    # Use the supported fallback to isolate this fault from
                    # the separately measured cold-index build.
                    (case.root.tenant_home / CACHE_NAME).touch(exist_ok=False)
                result = unittest.TestResult()
                with patch.object(case, "setUp", prepare), patch.object(
                        seat_teardown_capacity, function_name, changed[function_name]):
                    case.run(result)
                self.assertEqual([], result.errors)
                self.assertEqual(1, len(result.failures), result.failures)
                self.assertIn(assertion, result.failures[0][1])
                print(json.dumps({"mutant": self._testMethodName, "case": case.id(),
                    "limit": case.boundary_limit or jsonl.MAX_LEDGER_RECORDS,
                    "failing_assertion": assertion}), flush=True)

    def test_strict_equality_refuses_the_recoverable_paused_boundary(self):
        self.check_mutation("_capacity", "count + writes > MAX_LEDGER_RECORDS",
            "count + writes >= MAX_LEDGER_RECORDS",
            "test_capacity_filled_after_preflight_preserves_exact_paused_recovery",
            "self.assertEqual(['started', 'paused'], [row['step'] for row in self.f.rows()])")

    def test_one_report_slot_cannot_preserve_the_pre_effect_refusal(self):
        self.check_mutation("preflight_teardown", "physical_count, 2, 2 * MAX_RECORD_BYTES",
            "physical_count, 1, 2 * MAX_RECORD_BYTES",
            "test_initial_hundred_thousand_refuses_capacity_before_any_teardown_effect",
            "self.assertEqual([], self.f.rows())")

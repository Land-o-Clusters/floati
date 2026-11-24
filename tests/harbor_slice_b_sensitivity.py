"""Opt-in Slice B mutation evidence; excluded from ordinary test discovery."""
from __future__ import annotations

import copy
import inspect
import json
from pathlib import Path
import tempfile
import types
import unittest
from unittest import mock

from tests import harbor_parallel_qualification as qualification
from tests import harbor_parallel_qualification_boundaries as boundaries
from tests.temp_roots import REAL_TEMP_ROOT


def _replace_function(function, old, new):
    source = inspect.getsource(function)
    if source.count(old) != 1:
        raise AssertionError("controlled mutation did not identify exactly one predicate")
    namespace = dict(function.__globals__)
    exec(compile(source.replace(old, new), function.__code__.co_filename, "exec"), namespace)
    compiled = namespace[function.__name__]
    # Keep the real module globals so later owned capture/allocation wrappers
    # remain in the actual controller path; only this function's code changes.
    return types.FunctionType(compiled.__code__, function.__globals__, function.__name__,
                              function.__defaults__, function.__closure__)


class SliceBSensitivityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        assertion_case = unittest.TestCase()
        cls.seed = boundaries._OwnedPair(assertion_case, cls.addClassCleanup)
        receipt = cls.seed.run()
        assertion_case.assertTrue(receipt["qualified"])
        for phase in ("serial", "parallel"):
            cls.seed.pristine(phase)

    def existing_case(self, method):
        # Local class only: no imported/inherited test methods enter discovery.
        local = type("OwnedSensitivityBoundary", (boundaries.ParallelQualificationBoundaryTests,), {})
        local.setUpClass()
        local.seed = self.seed
        return local(method)

    def run_existing(self, case):
        result = unittest.TestResult()
        case.run(result)
        self.assertEqual([], result.errors)
        self.assertEqual([], result.skipped)
        for failed, traceback in result.failures:
            print("expected inner failure", str(failed), traceback)
        return result.failures

    def test_bad_seed_fails_positive_admission_before_corruption(self):
        case = self.existing_case("test_launcher_pid_must_be_strict_integer_matching_collected_process")
        path, snapshot = self.seed.pristine("parallel")
        damaged = json.loads(snapshot["raw"])
        damaged["availability"] = "unavailable"
        try:
            path.write_text(json.dumps(damaged) + "\n")
            failures = self.run_existing(case)
            self.assertEqual(4, len(failures))
            self.assertIn('self.case.assertEqual(snapshot["raw"], path.read_bytes())', failures[0][1])
            self.assertTrue(all("native data seed is poisoned" in text for _, text in failures[1:]))
        finally:
            path.write_bytes(snapshot["raw"])
            self.seed.pristine("parallel")

    def test_duplicate_key_acceptance_loses_exact_c04_rejection(self):
        case = self.existing_case("test_malformed_and_duplicate_key_cost_cannot_qualify")
        with mock.patch.object(self.seed.helper, "_unique", dict):
            failures = self.run_existing(case)
        self.assertEqual(1, len(failures))
        self.assertIn("variant='duplicate-key'", str(failures[0][0]))
        self.assertIn("ValueError not raised", failures[0][1])
        self.seed.pristine("parallel")

    def test_boolean_counter_acceptance_loses_all_eight_field_rejections(self):
        case = self.existing_case("test_each_cost_counter_requires_non_boolean_nonnegative_integer")
        mutant = _replace_function(self.seed.helper._validate_cost,
                                   "type(cost[key]) is not int", "not isinstance(cost[key], int)")
        with mock.patch.object(self.seed.helper, "_validate_cost", mutant):
            failures = self.run_existing(case)
        self.assertEqual(8, len(failures))
        for counter, (failed, traceback) in zip(boundaries.COUNTERS, failures):
            self.assertIn("counter=" + repr(counter), str(failed))
            self.assertIn("variant='boolean'", str(failed))
            self.assertIn("ValueError not raised", traceback)
        self.seed.pristine("parallel")

    def test_negative_counter_acceptance_loses_c22_rejection(self):
        case = self.existing_case("test_each_cost_counter_requires_non_boolean_nonnegative_integer")
        mutant = _replace_function(self.seed.helper._validate_cost, " or cost[key] < 0", "")
        with mock.patch.object(self.seed.helper, "_validate_cost", mutant):
            failures = self.run_existing(case)
        self.assertEqual(1, len(failures))
        self.assertIn("variant=-1", str(failures[0][0]))
        self.assertIn("ValueError not raised", failures[0][1])
        self.seed.pristine("parallel")

    def controller_case(self, phase, alter_helper, expected):
        case = boundaries.ParallelQualificationBoundaryTests("runTest")
        original = qualification.ParallelQualificationTests.helper

        def helper(owner):
            module = original(owner)
            alter_helper(module)
            return module

        mutation = ((lambda path, document, raw: path.unlink()) if phase == "serial"
                    else case.replace_field("request_sha256", "0" * 64))
        reason = ("FileNotFoundError" if phase == "serial"
                  else "native process-cost sidecar schema or phase binding is invalid")
        try:
            with mock.patch.object(qualification.ParallelQualificationTests, "helper", helper):
                with self.assertRaisesRegex(AssertionError, expected) as failure:
                    case.controller_rejects(phase, mutation, reason)
            print("expected controller failure", phase, str(failure.exception))
        finally:
            self.assertTrue(case.doCleanups(), "controlled controller fixture cleanup failed")

    def test_controller_cost_bypass_breaks_both_fresh_interceptions(self):
        def bypass(module):
            module._run_pair = _replace_function(module._run_pair,
                'launch["process_cost"] = _validate_cost(final_path, report, launch, source)',
                'pass  # controlled controller admission omission')

        # S must stop before parallel; P must refuse its completed native pair.
        with self.subTest(phase="serial"):
            self.controller_case("serial", bypass, "Lists differ")
        with self.subTest(phase="parallel"):
            self.controller_case("parallel", bypass, "returned cost refusal status")

    def test_persisted_qualified_override_is_detected_after_rejection(self):
        def publish_bad_qualification(module):
            publish = module._publish

            def altered(path, document):
                if path.name == "qualification.json" and document.get("status") == "invalid":
                    document = copy.deepcopy(document)
                    document["qualified"] = True
                return publish(path, document)
            module._publish = altered

        self.controller_case("parallel", publish_bad_qualification, "persisted invalid cost qualified")

    def test_foreign_replacement_cannot_authorize_fixture_removal(self):
        callbacks = []
        producer = boundaries._OwnedPair(self, callbacks.append)
        saved = producer.bank.with_name(producer.bank.name + "-saved")
        with tempfile.TemporaryDirectory(prefix="slice-b-foreign-", dir=REAL_TEMP_ROOT) as value:
            foreign = Path(value)
            sentinel = foreign / "sentinel"
            sentinel.write_bytes(b"foreign fixture must survive\n")
            producer.bank.rename(saved)
            producer.bank.symlink_to(foreign, target_is_directory=True)
            try:
                with self.assertRaisesRegex(AssertionError, "not ordinary private data"):
                    callbacks[0]()
                self.assertEqual(b"foreign fixture must survive\n", sentinel.read_bytes())
            finally:
                producer.bank.unlink()
                saved.rename(producer.bank)
                callbacks[0]()
        self.assertFalse(producer.bank.exists())

    def test_failed_no_record_capture_retains_bank_without_collection_fiction(self):
        callbacks = []
        producer = boundaries._OwnedPair(self, callbacks.append)
        sentinel = producer.bank / "retained-sentinel"
        sentinel.write_bytes(b"no unproved cleanup\n")
        # No process is created by this fault. Never relabel it collected just
        # to remove the diagnostic bank: retain it as the control's evidence.
        with mock.patch.object(producer.helper, "_capture", side_effect=RuntimeError("controlled probe failure")):
            with self.assertRaisesRegex(ValueError, "controlled probe failure"):
                producer.run()
        self.assertEqual([], producer.attempts)
        self.assertEqual([{"phase": None, "returned": False}], producer.capture_entries)
        with self.assertRaisesRegex(AssertionError, "retained unproved native fixture"):
            callbacks[0]()
        self.assertEqual(b"no unproved cleanup\n", sentinel.read_bytes())
        print("retained diagnostic bank; no child launched", str(producer.bank))

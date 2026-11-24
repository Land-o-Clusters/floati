"""Native-discovery and callback fidelity controls for the parallel runner."""

import importlib.util
import io
from pathlib import Path
import sys
import tempfile
import textwrap
import types
import unittest


_PATH = Path(__file__).resolve().parents[1] / "scripts" / "parallel_unittest.py"
if _PATH.exists():
    _SPEC = importlib.util.spec_from_file_location("parallel_unittest", _PATH)
    native = importlib.util.module_from_spec(_SPEC)
    _SPEC.loader.exec_module(native)
else:
    native = None


class NativeDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(native, "native discovery/recorder is not implemented")
        self.temp = tempfile.TemporaryDirectory(prefix="parallel-native-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.before_modules = set(sys.modules)
        self.addCleanup(self.clear_modules)
        before_path = sys.path[:]
        self.addCleanup(setattr, sys, "path", before_path)

    def clear_modules(self):
        for name in set(sys.modules) - self.before_modules:
            if name.startswith("test_native_"):
                del sys.modules[name]

    def write(self, name, source):
        (self.root / (name + ".py")).write_text(textwrap.dedent(source))

    def test_synthetic_adapter_is_atomic_and_runs_actual_nested_objects(self):
        self.write("test_native_adapter", '''
            import types, unittest
            synthetic = types.ModuleType("native_unimportable")
            exec("import unittest\\nclass A(unittest.TestCase):\\n def test_z(self): pass\\n def test_a(self): pass\\nclass B(unittest.TestCase):\\n def test_b(self): pass", synthetic.__dict__)
            def load_tests(loader, tests, pattern):
                return unittest.TestSuite([unittest.TestSuite([synthetic.A("test_z"), unittest.TestSuite([synthetic.A("test_a")])]), unittest.TestSuite([synthetic.B("test_b")])])
        ''')
        suite, census = native.discover_suite(self.root)
        ids = ["native_unimportable.A.test_z", "native_unimportable.A.test_a", "native_unimportable.B.test_b"]
        self.assertEqual(census["ids"], ids)
        self.assertNotIn("native_unimportable", sys.modules)
        self.assertEqual(len(census["units"]), 1)
        self.assertEqual(census["units"][0]["ids"], ids)
        self.assertIn("test_native_adapter", census["units"][0]["key"])
        selected = native.select_suite(suite, ids)
        self.assertEqual(len(list(list(list(selected)[0])[0])), 2)
        result = native.run_suite(selected, {"key": "worker-1", "ids": ids}, census["population_digest"], io.StringIO())
        self.assertEqual(result["started_ids"], ids)
        self.assertEqual(result["tests_run"], 3)
        self.assertTrue(result["successful"])

    def test_class_selection_retains_native_order_and_module_fixture_promotion(self):
        self.write("test_native_classes", '''
            import unittest
            class A(unittest.TestCase):
                def test_a(self): pass
                def test_b(self): pass
            class B(unittest.TestCase):
                def test_c(self): pass
        ''')
        self.write("test_native_fixture", '''
            import unittest
            def setUpModule(): pass
            class C(unittest.TestCase):
                def test_d(self): pass
            class D(unittest.TestCase):
                def test_e(self): pass
        ''')
        suite, census = native.discover_suite(self.root)
        self.assertEqual([len(u["ids"]) for u in census["units"]], [2, 1, 2])
        wanted = ["test_native_classes.A.test_b", "test_native_classes.A.test_a"]
        selected = native.select_suite(suite, wanted)
        result = native.run_suite(selected, {"key": "w", "ids": wanted}, census["population_digest"], io.StringIO())
        self.assertEqual(result["started_ids"], list(reversed(wanted)))
        for ids in ([wanted[0]], wanted + [wanted[0]], ["foreign.test"]):
            with self.subTest(ids=ids), self.assertRaises(ValueError):
                native.select_suite(suite, ids)

    def test_duplicate_ids_and_discovery_errors_refuse(self):
        self.write("test_native_duplicate", '''
            import unittest
            class A(unittest.TestCase):
                def test_a(self): pass
            def load_tests(loader, tests, pattern):
                return unittest.TestSuite([A("test_a"), A("test_a")])
        ''')
        with self.assertRaisesRegex(ValueError, "duplicate"):
            native.discover_suite(self.root)
        (self.root / "test_native_duplicate.py").unlink()
        self.write("test_native_broken", "raise ImportError('broken discovery')")
        with self.assertRaisesRegex(ValueError, "discovery"):
            native.discover_suite(self.root)

    def test_custom_suite_run_cannot_be_silently_flattened(self):
        self.write("test_native_custom", '''
            import unittest
            class A(unittest.TestCase):
                def test_a(self): pass
            class Custom(unittest.TestSuite):
                def run(self, result, debug=False): return result
            def load_tests(loader, tests, pattern):
                return Custom([A("test_a")])
        ''')
        with self.assertRaisesRegex(ValueError, "suite|discovery"):
            native.discover_suite(self.root)

    def test_discovery_keeps_native_import_path_for_runtime_imports(self):
        self.write("native_runtime_helper", "VALUE = 7")
        self.write("test_native_runtime", '''
            import unittest
            class A(unittest.TestCase):
                def test_a(self):
                    import native_runtime_helper
                    self.assertEqual(native_runtime_helper.VALUE, 7)
        ''')
        self.addCleanup(sys.modules.pop, "native_runtime_helper", None)
        suite, census = native.discover_suite(self.root)
        result = native.run_suite(suite, {"key": "w", "ids": census["ids"]}, census["population_digest"], io.StringIO())
        self.assertTrue(result["successful"], result)

    def test_repeated_class_scope_keeps_intervening_fixture_boundaries(self):
        self.write("native_class_scope", '''
            import unittest
            calls = []
            class A(unittest.TestCase):
                @classmethod
                def setUpClass(cls): calls.append("A setup")
                @classmethod
                def tearDownClass(cls): calls.append("A teardown")
                def test_first(self): calls.append("first")
                def test_last(self): calls.append("last")
            class B(unittest.TestCase):
                @classmethod
                def setUpClass(cls): calls.append("B setup")
                @classmethod
                def tearDownClass(cls): calls.append("B teardown")
                def test_middle(self): calls.append("middle")
        ''')
        self.addCleanup(sys.modules.pop, "native_class_scope", None)
        for suffix, cls, method in (("a", "A", "first"), ("b", "B", "middle"), ("c", "A", "last")):
            self.write("test_native_" + suffix, f'''
                import unittest, native_class_scope
                def load_tests(loader, tests, pattern):
                    return unittest.TestSuite([native_class_scope.{cls}("test_{method}")])
            ''')
        suite, census = native.discover_suite(self.root)
        self.assertEqual(len(census["units"]), 1)
        expected = ["A setup", "first", "A teardown", "B setup", "middle", "B teardown", "A setup", "last", "A teardown"]
        unittest.TextTestRunner(stream=io.StringIO()).run(suite)
        module = sys.modules["native_class_scope"]
        self.assertEqual(module.calls, expected)
        module.calls.clear()
        suite, census = native.discover_suite(self.root)
        with self.assertRaises(ValueError):
            native.select_suite(suite, [census["ids"][0], census["ids"][2]])
        selected = native.select_suite(suite, census["units"][0]["ids"])
        result = native.run_suite(selected, {"key": "w", "ids": census["ids"]}, census["population_digest"], io.StringIO())
        self.assertTrue(result["successful"])
        self.assertEqual(module.calls, expected)

    def test_repeated_module_scope_keeps_intervening_module_transition(self):
        self.write("native_module_scope", '''
            import unittest
            calls = []
            def setUpModule(): calls.append("setup")
            def tearDownModule(): calls.append("teardown")
            class A(unittest.TestCase):
                def test_first(self): calls.append("first")
            class C(unittest.TestCase):
                def test_last(self): calls.append("last")
        ''')
        self.addCleanup(sys.modules.pop, "native_module_scope", None)
        self.write("test_native_a", '''
            import unittest, native_module_scope
            def load_tests(loader, tests, pattern):
                return unittest.TestSuite([native_module_scope.A("test_first")])
        ''')
        self.write("test_native_b", '''
            import unittest, native_module_scope
            class B(unittest.TestCase):
                def test_middle(self): native_module_scope.calls.append("middle")
        ''')
        self.write("test_native_c", '''
            import unittest, native_module_scope
            def load_tests(loader, tests, pattern):
                return unittest.TestSuite([native_module_scope.C("test_last")])
        ''')
        suite, census = native.discover_suite(self.root)
        self.assertEqual(len(census["units"]), 1)
        expected = ["setup", "first", "teardown", "middle", "setup", "last", "teardown"]
        unittest.TextTestRunner(stream=io.StringIO()).run(suite)
        module = sys.modules["native_module_scope"]
        self.assertEqual(module.calls, expected)
        module.calls.clear()
        suite, census = native.discover_suite(self.root)
        selected = native.select_suite(suite, census["units"][0]["ids"])
        result = native.run_suite(selected, {"key": "w", "ids": census["ids"]}, census["population_digest"], io.StringIO())
        self.assertTrue(result["successful"])
        self.assertEqual(module.calls, expected)


class NativeRecordingTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(native, "native discovery/recorder is not implemented")

    def record(self, case, names=None, event_sink=None):
        names = names or unittest.defaultTestLoader.getTestCaseNames(case)
        suite = unittest.TestSuite(case(name) for name in names)
        ids = [test.id() for test in suite]
        result = native.run_suite(suite, {"key": "worker-1", "ids": ids}, "digest", io.StringIO(), event_sink=event_sink)
        return ids, result

    def semantic_aggregate(self, case):
        from scripts import parallel_plan
        suite = unittest.defaultTestLoader.loadTestsFromTestCase(case)
        census = native._census(suite)
        plan = parallel_plan.make_plan(census, 1)
        self.assertEqual(len(plan["assignments"]), 1)
        emitted = []
        result = native.run_suite(suite, plan["assignments"][0],
                                  census["population_digest"], io.StringIO(),
                                  event_sink=emitted.append)
        self.assertEqual(result["started_ids"], census["ids"])
        self.assertEqual(result["stopped_ids"], census["ids"])
        self.assertEqual([e for e in emitted if e["kind"] not in ("start", "stop")],
                         result["events"])
        return parallel_plan.reconcile(census, plan, [result])

    def test_semantic_subtest_labels_preserve_native_parity_across_distinct_inputs(self):
        # Dropping native semantic IDs or case multiplicity breaks this proof.
        import hashlib
        import uuid
        from scripts import parallel_plan
        traces, aggregates = [], []
        with tempfile.TemporaryDirectory() as temporary:
            for realization in ("first", "second"):
                path = Path(temporary) / realization
                path.write_text(realization)
                token = uuid.uuid4().hex
                digest = hashlib.sha256(realization.encode()).hexdigest()
                callback = lambda value=realization: value
                trace = []

                class Cases(unittest.TestCase):
                    def test_inputs(self):
                        cases = (("temporary-path", path), ("generated-id", token),
                                 ("content-digest", digest), ("callable", callback),
                                 ("callable", callback))
                        for label, value in cases:
                            with self.subTest(case=label):
                                with self.subTest(status="accepted"):
                                    observed = (value.read_text() if label == "temporary-path"
                                                else value() if label == "callable" else value)
                                    self.assertTrue(observed)
                                    trace.append((label, observed))

                aggregates.append(self.semantic_aggregate(Cases))
                self.assertEqual(sum(event["kind"] == "subtest_success"
                                     for event in aggregates[-1]["events"]), 10)
                self.assertEqual([label for label, _ in trace],
                                 ["temporary-path", "generated-id", "content-digest",
                                  "callable", "callable"])
                self.assertEqual([trace[0][1], trace[1][1], trace[2][1], trace[3][1], trace[4][1]],
                                 [realization, token, digest, realization, realization])
                traces.append(trace)
        self.assertNotEqual(traces[0], traces[1])
        self.assertTrue(parallel_plan.compare_results(*aggregates)["equivalent"])
        aggregates[1]["events"].reverse()
        self.assertTrue(parallel_plan.compare_results(*aggregates)["equivalent"])

    def test_equal_count_case_substitution_and_stable_dimension_changes_refuse(self):
        # An ordinal/count-only comparator would accept both hostile variants.
        from scripts import parallel_plan
        variants = ((("inside-root", "completed"), ("outside-root", "completed")),
                    (("inside-root", "completed"), ("inside-root", "completed")),
                    (("inside-root", "completed"), ("outside-root", "aborted")))
        aggregates = []
        for rows in variants:
            trace = []

            class Cases(unittest.TestCase):
                def test_targets(self):
                    for target, status in rows:
                        with self.subTest(target=target, status=status):
                            trace.append((target, status))

            aggregates.append(self.semantic_aggregate(Cases))
            self.assertEqual(trace, list(rows))
        for candidate in aggregates[1:]:
            self.assertEqual(len(candidate["events"]), len(aggregates[0]["events"]))
            difference = parallel_plan.compare_results(aggregates[0], candidate)
            self.assertFalse(difference["equivalent"])
            self.assertEqual(set(difference["differences"]), {"events"})
            mismatch = difference["differences"]["events"]
            self.assertEqual(len(mismatch["missing"]), 1)
            self.assertEqual(len(mismatch["extra"]), 1)
            self.assertIn("outside-root", mismatch["missing"][0]["subtest"])
        self.assertIn("inside-root", parallel_plan.compare_results(
            aggregates[0], aggregates[1])["differences"]["events"]["extra"][0]["subtest"])
        self.assertIn("aborted", parallel_plan.compare_results(
            aggregates[0], aggregates[2])["differences"]["events"]["extra"][0]["subtest"])

    def test_literal_semantic_labels_and_unlabelled_values_are_never_scrubbed(self):
        # Broad path/UUID/digest/address scrubbing must not erase literal cases.
        from scripts import parallel_plan
        pairs = (("\x2fprivate/tmp/left", "\x2fprivate/tmp/right"),
                 ("11111111-1111-4111-8111-111111111111", "22222222-2222-4222-8222-222222222222"),
                 ("a" * 64, "b" * 64),
                 ("<object object at 0x1234>", "<object object at 0x5678>"))
        for left, right in pairs:
            aggregates = []
            for literal in (left, right):
                class Cases(unittest.TestCase):
                    def test_literal(self):
                        with self.subTest(case=literal):
                            self.assertTrue(literal)
                aggregates.append(self.semantic_aggregate(Cases))
            self.assertFalse(parallel_plan.compare_results(*aggregates)["equivalent"])
        with tempfile.TemporaryDirectory() as temporary:
            aggregates = []
            for name in ("first", "second"):
                path = Path(temporary) / name
                class Unlabelled(unittest.TestCase):
                    def test_raw(self):
                        with self.subTest(path=path):
                            self.assertEqual(path.name, name)
                aggregates.append(self.semantic_aggregate(Unlabelled))
            self.assertFalse(parallel_plan.compare_results(*aggregates)["equivalent"])

    def test_outcome_multiplicity_subtests_and_skip_reasons_are_preserved(self):
        class Outcomes(unittest.TestCase):
            def test_success(self): pass
            def test_failure(self): self.fail("no")
            def test_error(self): raise RuntimeError("bad")
            @unittest.skip("deliberate skip")
            def test_skip(self): pass
            @unittest.expectedFailure
            def test_expected(self): self.fail("expected")
            @unittest.expectedFailure
            def test_unexpected(self): pass
            def test_subtests(self):
                with self.subTest(n=1): pass
                with self.subTest(n=2): self.fail("subfailure")
                with self.subTest(n=3): raise RuntimeError("suberror")
                with self.subTest(n=4): self.skipTest("subskip")
        emitted = []
        ids, result = self.record(Outcomes, event_sink=emitted.append)
        self.assertEqual(result["selected_ids"], ids)
        self.assertEqual(result["started_ids"], ids)
        self.assertEqual(result["stopped_ids"], ids)
        self.assertEqual(result["fixture_blocked_ids"], [])
        self.assertEqual(result["tests_run"], 7)
        self.assertFalse(result["successful"])
        self.assertCountEqual([e["kind"] for e in result["events"]], ["success", "failure", "error", "skip", "expected_failure", "unexpected_success", "subtest_success", "subtest_failure", "subtest_error", "skip"])
        self.assertCountEqual([e["reason"] for e in result["events"] if "reason" in e], ["deliberate skip", "subskip"])
        subevents = [e for e in result["events"] if "subtest" in e]
        self.assertEqual(len(subevents), 4)
        self.assertTrue(all(e["test_id"].endswith("test_subtests") for e in subevents))
        self.assertEqual([e for e in emitted if e["kind"] not in ("start", "stop")], result["events"])

    def test_class_setup_error_and_skip_block_without_inventing_execution(self):
        for error in (RuntimeError("setup error"), unittest.SkipTest("fixture skip")):
            class Blocked(unittest.TestCase):
                @classmethod
                def setUpClass(cls): raise error
                def test_a(self): self.fail("must not execute")
                def test_b(self): self.fail("must not execute")
            ids, result = self.record(Blocked)
            self.assertEqual(result["tests_run"], 0)
            self.assertEqual(result["started_ids"], [])
            self.assertEqual(result["stopped_ids"], [])
            self.assertEqual(result["fixture_blocked_ids"], ids)
            self.assertEqual(len(result["events"]), 1)
            self.assertEqual(result["events"][0]["kind"], "fixture_skip" if isinstance(error, unittest.SkipTest) else "fixture_error")

    def test_teardown_error_is_fixture_event_after_success(self):
        class Teardown(unittest.TestCase):
            @classmethod
            def tearDownClass(cls): raise RuntimeError("teardown")
            def test_a(self): pass
        ids, result = self.record(Teardown)
        self.assertEqual(result["started_ids"], ids)
        self.assertEqual(result["fixture_blocked_ids"], [])
        self.assertEqual([e["kind"] for e in result["events"]], ["success", "fixture_error"])
        self.assertIn("tearDownClass", result["events"][1]["fixture"])

    def test_method_failure_and_teardown_error_both_survive(self):
        class Multiple(unittest.TestCase):
            def test_a(self): self.fail("body")
            def tearDown(self): raise RuntimeError("teardown")
        ids, result = self.record(Multiple)
        self.assertEqual([e["kind"] for e in result["events"]], ["failure", "error"])
        self.assertEqual([e["test_id"] for e in result["events"]], ids * 2)
        self.assertEqual(result["status"], "complete")

    def test_method_setup_error_is_an_executed_method(self):
        class Setup(unittest.TestCase):
            def setUp(self): raise RuntimeError("setup")
            def test_a(self): pass
        ids, result = self.record(Setup)
        self.assertEqual(result["started_ids"], ids)
        self.assertEqual(result["fixture_blocked_ids"], [])
        self.assertEqual(result["events"], [{"kind": "error", "test_id": ids[0]}])

    def test_module_setup_error_blocks_both_classes(self):
        module = types.ModuleType("native_fixture_module")
        exec("import unittest\ndef setUpModule(): raise RuntimeError('setup')\nclass A(unittest.TestCase):\n def test_a(self): pass\nclass B(unittest.TestCase):\n def test_b(self): pass", module.__dict__)
        sys.modules[module.__name__] = module
        self.addCleanup(sys.modules.pop, module.__name__)
        suite = unittest.defaultTestLoader.loadTestsFromModule(module)
        ids = ["native_fixture_module.A.test_a", "native_fixture_module.B.test_b"]
        result = native.run_suite(suite, {"key": "w", "ids": ids}, "digest", io.StringIO())
        self.assertEqual(result["fixture_blocked_ids"], ids)
        self.assertEqual(result["tests_run"], 0)
        self.assertEqual(result["events"][0]["fixture"], "setUpModule (native_fixture_module)")

    def test_interrupt_retains_exact_callbacks_and_is_unsuccessful(self):
        class Interrupted(unittest.TestCase):
            def test_a(self): raise KeyboardInterrupt()
            def test_b(self): pass
        ids, result = self.record(Interrupted)
        self.assertEqual(result["status"], "interrupted")
        self.assertFalse(result["successful"])
        self.assertEqual(result["started_ids"], ids[:1])
        self.assertEqual(result["stopped_ids"], ids[:1])
        self.assertEqual(result["events"], [])

    def test_unknown_callback_and_mismatched_assignment_fail_closed(self):
        class Unknown(unittest.TestCase):
            def test_a(self): pass
            def run(self, result=None):
                result.addSuccess(unittest.FunctionTestCase(lambda: None))
        _, result = self.record(Unknown)
        self.assertFalse(result["successful"])
        self.assertEqual(result["status"], "invalid")
        with self.assertRaises(ValueError):
            native.run_suite(unittest.TestSuite([Unknown("test_a")]), {"key": "w", "ids": []}, "digest", io.StringIO())

    def test_test_body_cannot_swallow_an_unknown_callback_refusal(self):
        class Swallowed(unittest.TestCase):
            def test_a(self):
                try:
                    self._outcome.result.addSuccess(unittest.FunctionTestCase(lambda: None))
                except ValueError:
                    pass
        _, result = self.record(Swallowed)
        self.assertEqual(result["status"], "invalid")
        self.assertFalse(result["successful"])

    def test_module_teardown_skip_is_preserved_without_blocking_methods(self):
        module = types.ModuleType("native_teardown_module")
        exec("import unittest\ndef tearDownModule(): raise unittest.SkipTest('teardown skip')\nclass A(unittest.TestCase):\n def test_a(self): pass", module.__dict__)
        sys.modules[module.__name__] = module
        self.addCleanup(sys.modules.pop, module.__name__)
        _, result = self.record(module.A)
        self.assertEqual(result["fixture_blocked_ids"], [])
        self.assertEqual(result["events"][-1], {"kind": "fixture_skip", "test_id": "tearDownModule (native_teardown_module)", "fixture": "tearDownModule (native_teardown_module)", "blocked_ids": [], "reason": "teardown skip"})

    def test_subtest_success_without_parent_outcome_is_incomplete(self):
        class Incomplete(unittest.TestCase):
            def test_a(self): pass
            def run(self, result=None):
                result.startTest(self)
                result.addSubTest(self, unittest.case._SubTest(self, None, {"n": 1}), None)
                result.stopTest(self)
        _, result = self.record(Incomplete)
        self.assertEqual(result["events"][0]["kind"], "subtest_success")
        self.assertEqual(result["status"], "invalid")
        self.assertFalse(result["successful"])

    def test_native_failed_and_skipped_subtests_need_no_parent_success(self):
        for outcome in ("failure", "skip"):
            class Subtests(unittest.TestCase):
                def test_a(self):
                    with self.subTest(n=1):
                        if outcome == "failure":
                            self.fail("failure")
                        else:
                            self.skipTest("skip")
            _, result = self.record(Subtests)
            self.assertEqual(result["status"], "complete")
            self.assertEqual(len(result["events"]), 1)
            self.assertEqual(result["events"][0]["kind"], "subtest_failure" if outcome == "failure" else "skip")

    def test_setup_and_cleanup_errors_preserve_repeated_fixture_callbacks(self):
        class Blocked(unittest.TestCase):
            @classmethod
            def setUpClass(cls):
                cls.addClassCleanup(cls.bad_cleanup)
                raise RuntimeError("setup")
            @classmethod
            def bad_cleanup(cls): raise RuntimeError("cleanup")
            def test_a(self): pass
        emitted = []
        ids, result = self.record(Blocked, event_sink=emitted.append)
        self.assertEqual(result["status"], "complete")
        self.assertFalse(result["successful"])
        self.assertEqual(result["fixture_blocked_ids"], ids)
        self.assertEqual(result["started_ids"], [])
        self.assertEqual(len(result["events"]), 2)
        self.assertEqual([event["kind"] for event in result["events"]], ["fixture_error", "fixture_error"])
        self.assertEqual([event["blocked_ids"] for event in result["events"]], [ids, ids])
        self.assertEqual(emitted, result["events"])

    def test_module_setup_and_cleanup_callbacks_share_blocked_union(self):
        module = types.ModuleType("native_setup_cleanup")
        exec("import unittest\ndef cleanup(): raise RuntimeError('cleanup')\ndef setUpModule():\n unittest.addModuleCleanup(cleanup)\n raise RuntimeError('setup')\nclass A(unittest.TestCase):\n def test_a(self): pass", module.__dict__)
        sys.modules[module.__name__] = module
        self.addCleanup(sys.modules.pop, module.__name__)
        emitted = []
        ids, result = self.record(module.A, event_sink=emitted.append)
        self.assertEqual(result["status"], "complete")
        self.assertFalse(result["successful"])
        self.assertEqual(result["fixture_blocked_ids"], ids)
        self.assertEqual(result["tests_run"], 0)
        self.assertEqual(len(result["events"]), 2)
        self.assertEqual([event["blocked_ids"] for event in result["events"]], [ids, ids])
        self.assertEqual(emitted, result["events"])

    def test_incremental_sink_observes_start_before_body_failure_and_parity(self):
        emitted = []
        observed_in_body = []
        class Failing(unittest.TestCase):
            def test_a(self):
                observed_in_body.extend(emitted)
                raise RuntimeError("controlled failure")
        test = Failing("test_a")
        result = native.run_suite(unittest.TestSuite([test]), {"key": "w", "ids": [test.id()]}, "digest", io.StringIO(), event_sink=emitted.append)
        self.assertEqual(observed_in_body, [{"kind": "start", "test_id": test.id()}])
        self.assertEqual(emitted, [{"kind": "start", "test_id": test.id()}, *result["events"], {"kind": "stop", "test_id": test.id()}])
        self.assertEqual(result["status"], "complete")
        self.assertFalse(result["successful"])

    def test_sink_failure_cannot_be_swallowed_into_success(self):
        class Passing(unittest.TestCase):
            def test_a(self): pass
        for fail_kind in ("start", "success", "stop"):
            def sink(event):
                if event["kind"] == fail_kind:
                    raise OSError("sink write failed")
            test = Passing("test_a")
            result = native.run_suite(unittest.TestSuite([test]), {"key": "w", "ids": [test.id()]}, "digest", io.StringIO(), event_sink=sink)
            self.assertEqual(result["status"], "invalid")
            self.assertFalse(result["successful"])
            self.assertIn("sink", result["incomplete_reason"])

    def test_setup_blockers_are_scoped_before_a_repeated_class_recovers(self):
        class Repeated(unittest.TestCase):
            attempts = 0
            @classmethod
            def setUpClass(cls):
                cls.attempts += 1
                if cls.attempts == 1:
                    raise RuntimeError("first setup fails")
            def test_first(self): pass
            def test_last(self): pass
        class Between(unittest.TestCase):
            def test_middle(self): pass
        tests = [Repeated("test_first"), Between("test_middle"), Repeated("test_last")]
        ids = [test.id() for test in tests]
        emitted = []
        result = native.run_suite(unittest.TestSuite(tests), {"key": "w", "ids": ids}, "digest", io.StringIO(), event_sink=emitted.append)
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["fixture_blocked_ids"], ids[:1])
        self.assertEqual(result["started_ids"], ids[1:])
        fixture_events = [event for event in emitted if "fixture" in event]
        self.assertEqual(fixture_events, [result["events"][0]])
        self.assertEqual(fixture_events[0]["blocked_ids"], ids[:1])


if __name__ == "__main__":
    unittest.main()

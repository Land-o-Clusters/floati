"""Contracts for fresh-process scratch roots and conservative serial units."""

from __future__ import annotations

import copy
from contextlib import nullcontext
import importlib
import json
import os
import socket
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock

from scripts.parallel_isolation import _platform_temp_root as platform_boundary
from tests.temp_roots import REAL_TEMP_ROOT


REPOSITORY = Path(__file__).resolve().parents[1]


class ParallelIsolationTests(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
        self.addCleanup(self.scratch.cleanup)
        self.root = Path(self.scratch.name)

    def helper(self):
        from scripts import parallel_isolation
        return parallel_isolation

    def child(self, code, env):
        return subprocess.run(
            [sys.executable, "-c", code], cwd=REPOSITORY, env=env,
            capture_output=True, text=True, timeout=20,
        )

    def temp_root_child(self, override=None, *, supplied=True):
        env = dict(os.environ)
        if supplied:
            env["FLOATI_TEST_TEMP_ROOT"] = override
        else:
            env.pop("FLOATI_TEST_TEMP_ROOT", None)
        return self.child(
            "import json, runpy; "
            "print(json.dumps(runpy.run_path('tests/temp_roots.py')['REAL_TEMP_ROOT']))",
            env,
        )

    def test_helper_is_available_without_importing_tests(self):
        result = self.child(
            "import sys; import scripts.parallel_isolation; "
            "assert 'tests' not in sys.modules", dict(os.environ),
        )
        self.assertEqual(0, result.returncode, result.stderr)

    def test_inherited_claude_inbox_never_reaches_a_test_process(self):
        """All test entry points discard session endpoints before tests run."""
        endpoint = self.root / "inherited.sock"
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
            listener.bind(str(endpoint))
            listener.listen(4)
            listener.setblocking(False)
            contaminated = dict(os.environ,
                CLAUDE_CODE_MESSAGING_SOCKET=str(endpoint),
                CLAUDE_CODE_MESSAGING_TOKEN="scratch-test-token")
            attempt = (
                "import os, socket; "
                "endpoint = os.environ.get('CLAUDE_CODE_MESSAGING_SOCKET'); "
                "peer = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM); "
                "peer.connect(endpoint) if endpoint else None; peer.close(); "
                "assert 'CLAUDE_CODE_MESSAGING_SOCKET' not in os.environ; "
                "assert 'CLAUDE_CODE_MESSAGING_TOKEN' not in os.environ"
            )
            for route in ("package", "parallel", "selftest"):
                with self.subTest(route=route):
                    env = contaminated
                    if route == "parallel":
                        worker = self.root / "inbox-worker"
                        worker.mkdir(mode=0o700)
                        env = self.helper().make_worker_environment(worker, contaminated, "")
                        code = attempt
                    elif route == "package":
                        code = "import tests; " + attempt
                    else:
                        code = (
                            "import unittest; from unittest import mock; "
                            "from floati import selftest; "
                            "check = lambda *args, **kwargs: (exec(" + repr(attempt) + "), unittest.TestSuite())[1]; "
                            "discovery = mock.patch.object(unittest.defaultTestLoader, 'discover', side_effect=check); "
                            "finish = mock.patch.object(selftest, 'finish', return_value=0); "
                            "discovery.start(); finish.start(); "
                            "assert selftest.main([]) == 0"
                        )
                    result = self.child(code, env)
                    # A queued connection is proof of leakage; no readiness wait
                    # or timing assumption is needed after the child has exited.
                    try:
                        peer, _address = listener.accept()
                    except BlockingIOError:
                        connected = False
                    else:
                        peer.close()
                        connected = True
                    self.assertFalse(connected, route + " reached the inherited inbox")
                    self.assertEqual(0, result.returncode, result.stderr)
            self.assertIn("CLAUDE_CODE_MESSAGING_SOCKET", contaminated)
            self.assertIn("CLAUDE_CODE_MESSAGING_TOKEN", contaminated)

    def test_parallel_child_refuses_inherited_claude_coordinates_before_discovery(self):
        source = REPOSITORY / "scripts/test-parallel.py"
        code = (
            "import importlib.util; from pathlib import Path; "
            "spec = importlib.util.spec_from_file_location('inbox_isolation_child', " + repr(str(source)) + "); "
            "launcher = importlib.util.module_from_spec(spec); spec.loader.exec_module(launcher); "
            "launcher.child_main(Path(" + repr(str(self.root)) + "), Path(" + repr(str(self.root)) + "), 'discovery')"
        )
        for variable in ("CLAUDE_CODE_MESSAGING_SOCKET", "CLAUDE_CODE_MESSAGING_TOKEN"):
            with self.subTest(variable=variable):
                result = self.child(code, dict(os.environ, **{variable: "scratch-inherited-value"}))
                self.assertNotEqual(0, result.returncode)
                self.assertIn("test_environment_contains_claude_messaging", result.stderr)
                self.assertNotIn("scratch-inherited-value", result.stderr)

    def test_workers_have_distinct_roots_and_preserve_original_code_path(self):
        base = dict(os.environ, PYTHONPATH="/code/already-carried")
        before = dict(base)
        envs = []
        for name in ("worker-one", "worker-two"):
            worker = self.root / name
            worker.mkdir(mode=0o700)
            env = self.helper().make_worker_environment(worker, base, "/code/user-site")
            envs.append(env)
            for key in ("HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME",
                        "TMPDIR", "TMP", "TEMP", "FLOATI_TEST_TEMP_ROOT"):
                path = Path(env[key]).resolve()
                self.assertTrue(path.is_relative_to(worker), (key, path))
                self.assertTrue(path.is_dir(), (key, path))
            self.assertEqual(
                ["/code/already-carried", "/code/user-site"],
                env["PYTHONPATH"].split(os.pathsep),
            )
        self.assertEqual(before, base)
        self.assertNotEqual(envs[0]["HOME"], envs[1]["HOME"])
        self.assertNotEqual(envs[0]["TMPDIR"], envs[1]["TMPDIR"])

    def test_fresh_child_pins_home_and_retains_original_user_site_in_grandchild(self):
        user_site = self.root / "original-user-site"
        user_site.mkdir()
        (user_site / "parallel_code_probe.py").write_text("MARKER = 'original-code'\n")
        worker = self.root / "worker"
        worker.mkdir(mode=0o700)
        env = self.helper().make_worker_environment(worker, dict(os.environ), str(user_site))
        result = self.child(
            "import json, os, subprocess, sys; import tests; "
            "from tests.temp_roots import REAL_TEMP_ROOT; "
            "import parallel_code_probe; "
            "child = subprocess.check_output([sys.executable, '-c', "
            "'import parallel_code_probe; print(parallel_code_probe.MARKER)'], text=True); "
            "print(json.dumps({'before': tests.REAL_HOME_BEFORE_PIN, "
            "'home': os.environ['HOME'], 'fixtures': REAL_TEMP_ROOT, "
            "'marker': parallel_code_probe.MARKER, 'child': child.strip()}))",
            env,
        )
        self.assertEqual(0, result.returncode, result.stderr)
        receipt = json.loads(result.stdout)
        self.assertEqual("original-code", receipt["marker"])
        self.assertEqual("original-code", receipt["child"])
        self.assertEqual(env["HOME"], receipt["before"])
        self.assertNotEqual(receipt["before"], receipt["home"])
        self.assertEqual(Path(receipt["before"]).parent, Path(receipt["home"]).parent)
        self.assertEqual(env["FLOATI_TEST_TEMP_ROOT"], receipt["fixtures"])
        self.assertTrue(Path(receipt["home"]).is_relative_to(worker))

    def test_child_retains_home_fence_and_signing_tmpdir_probe(self):
        worker = self.root / "worker"
        worker.mkdir(mode=0o700)
        env = self.helper().make_worker_environment(worker, dict(os.environ), "")
        result = self.child(
            "import unittest; from tests.test_home_fence import HomePinTests; "
            "from tests.test_signing_ordinary import SigningOrdinaryPathTests; "
            "suite = unittest.TestSuite([HomePinTests('test_a_child_process_inherits_the_pin'), "
            "SigningOrdinaryPathTests('test_ordinary_accepts_a_regular_file_under_host_tmpdir')]); "
            "result = unittest.TextTestRunner().run(suite); "
            "assert result.wasSuccessful(); "
            "import sys; assert len(result.skipped) == (0 if sys.platform == 'darwin' else 1)",
            env,
        )
        self.assertEqual(0, result.returncode, result.stderr)

    def test_valid_nested_override_is_used(self):
        nested = self.root / "nested" / "fixtures"
        nested.mkdir(parents=True)
        result = self.temp_root_child(str(nested))
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(str(nested), json.loads(result.stdout))

    def test_full_module_runs_in_fresh_isolated_environment(self):
        if "FLOATI_TEST_TEMP_ROOT" in os.environ:
            # The full-module child runs this control too. It proves that its
            # already-active fixture override remains valid without recursing.
            self.assertEqual(os.environ["FLOATI_TEST_TEMP_ROOT"], REAL_TEMP_ROOT)
            result = self.temp_root_child(REAL_TEMP_ROOT)
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertEqual(REAL_TEMP_ROOT, json.loads(result.stdout))
            return
        worker = self.root / "outer-worker"
        worker.mkdir(mode=0o700)
        env = self.helper().make_worker_environment(worker, dict(os.environ), "")
        result = subprocess.run(
            [sys.executable, "-m", "unittest", "tests.harbor_parallel_isolation"],
            cwd=REPOSITORY, env=env, capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)

    def test_unconfigured_temp_root_retains_platform_behavior(self):
        result = self.temp_root_child(supplied=False)
        self.assertEqual(0, result.returncode, result.stderr)
        expected = "\x2fprivate/tmp" if sys.platform == "darwin" else os.path.realpath(tempfile.gettempdir())
        self.assertEqual(expected, json.loads(result.stdout))

    def test_invalid_override_refuses_without_fallback(self):
        ordinary_file = self.root / "file"
        ordinary_file.touch()
        link = self.root / "link"
        link.symlink_to(self.root, target_is_directory=True)
        for case_index, path in enumerate(("", "relative", str(platform_boundary()), "/", str(self.root / "missing"),
                     str(ordinary_file), str(link), str(link / "file"),
                     str(self.root) + "/../" + self.root.name, str(self.root) + "/")):
            with self.subTest(case=(
                "empty-root",
                "relative-root",
                "platform-boundary-root",
                "filesystem-root",
                "missing-root",
                "regular-file-root",
                "symlink-root",
                "symlink-parent-file",
                "parent-traversal-root",
                "trailing-separator-root",
            )[case_index]):
                result = self.temp_root_child(path)
                self.assertNotEqual(0, result.returncode)
                self.assertIn("FLOATI_TEST_TEMP_ROOT", result.stderr)

    def test_foreign_owned_override_refuses(self):
        result = self.child(
            "import os, runpy; uid = os.getuid(); os.getuid = lambda: uid + 1; "
            "runpy.run_path('tests/temp_roots.py')",
            dict(os.environ, FLOATI_TEST_TEMP_ROOT=str(self.root)),
        )
        self.assertNotEqual(0, result.returncode)
        self.assertIn("owned", result.stderr)

    def test_worker_refuses_escaped_symlink_and_nonordinary_roots(self):
        link = self.root / "link"
        link.symlink_to(self.root, target_is_directory=True)
        ordinary_file = self.root / "file"
        ordinary_file.touch()
        for case_index, path in enumerate((platform_boundary(), Path("relative"), self.root / ".." / self.root.name,
                     link, ordinary_file, self.root / "missing")):
            with self.subTest(case=(
                "platform-boundary-root",
                "relative-root",
                "parent-traversal-root",
                "symlink-root",
                "regular-file-root",
                "missing-root",
            )[case_index]):
                with self.assertRaises((ValueError, OSError)):
                    self.helper().make_worker_environment(path, dict(os.environ), "")

    def test_worker_boundary_uses_native_paths_and_injected_foreign_boundary_under_tmpdir(self):
        """Run the native boundary live; inject the foreign platform's boundary."""
        native_platform = sys.platform
        native_resolve = Path.resolve

        def resolve_only_on_native_platform(path, *arguments, **keywords):
            self.assertEqual(native_platform, sys.platform,
                "a simulated platform must inject its boundary, never resolve host paths")
            return native_resolve(path, *arguments, **keywords)

        for platform, spelling in (("darwin", "\x2fprivate/tmp"), ("linux", "\x2ftmp")):
            native = platform == native_platform
            injected = (nullcontext() if native else
                        mock.patch.object(self.helper(), "_platform_temp_root", return_value=self.root))
            with self.subTest(platform=platform, boundary="native" if native else "injected"),\
                 mock.patch.object(sys, "platform", platform),\
                 mock.patch.object(Path, "resolve", resolve_only_on_native_platform),\
                 mock.patch.object(tempfile, "gettempdir", return_value=str(self.root)), injected:
                boundary = platform_boundary() if native else self.root
                if native:
                    self.assertEqual(Path(spelling).resolve(strict=True), boundary,
                                     "the native arm resolves its real platform boundary")
                self.assertEqual(self.helper()._platform_temp_root(), boundary)
                with self.assertRaisesRegex(ValueError, "strictly below"):
                    self.helper().make_worker_environment(boundary, dict(os.environ), "")

    def test_linux_fixture_override_is_strictly_below_worker_tmpdir(self):
        if sys.platform == "darwin":
            self.skipTest("Linux tempfile root contract")
        worker = self.root / "worker"
        worker.mkdir(mode=0o700)
        env = self.helper().make_worker_environment(worker, dict(os.environ), "")
        self.assertTrue(Path(env["FLOATI_TEST_TEMP_ROOT"]).is_relative_to(env["TMPDIR"]))
        self.assertNotEqual(env["FLOATI_TEST_TEMP_ROOT"], env["TMPDIR"])

    def test_linux_branch_validates_fixture_below_changed_gettempdir(self):
        # Exercise Linux's gettempdir contract even on the Darwin development
        # host, in a fresh process so the platform switch cannot leak to tests.
        worker = self.root / "worker"
        worker.mkdir(mode=0o700)
        result = self.child(
            "import json, os, runpy, sys, tempfile; from pathlib import Path; "
            "sys.platform = 'linux'; tempfile.tempdir = " + repr(str(self.root)) + "; "
            "from scripts.parallel_isolation import make_worker_environment; "
            "env = make_worker_environment(Path(" + repr(str(worker)) + "), dict(os.environ), ''); "
            "os.environ.update(env); tempfile.tempdir = None; "
            "root = runpy.run_path('tests/temp_roots.py')['REAL_TEMP_ROOT']; "
            "print(json.dumps({'root': root, 'tmp': tempfile.gettempdir()}))",
            dict(os.environ),
        )
        self.assertEqual(0, result.returncode, result.stderr)
        receipt = json.loads(result.stdout)
        self.assertEqual(str(worker / "tmp"), receipt["tmp"])
        self.assertEqual(str(worker / "tmp" / "fixtures"), receipt["root"])


class ParallelSerialRulesTests(unittest.TestCase):
    def fixed_parent_consumers(self, suite, **options):
        from tests.parallel_consumers import fixed_parent_consumers
        return fixed_parent_consumers(suite, **options)

    def synthetic_consumers(self, source):
        # Native discovery has already imported this module in the full bank.
        # Standalone controls import definitions only, never run the fixture.
        import tests.test_workers

        temporary = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
        self.addCleanup(temporary.cleanup)
        path = Path(temporary.name) / "future_consumers.py"
        path.write_text(source)
        module = types.ModuleType("future_consumers")
        module.__file__ = str(path)
        sys.modules[module.__name__] = module
        self.addCleanup(sys.modules.pop, module.__name__, None)
        exec(compile(source, str(path), "exec"), module.__dict__)
        return unittest.TestSuite([module.FutureConsumer("test_effect")])

    def test_unseen_inherited_aliased_consumer_requires_its_own_serial_rule(self):
        """A fifth consumer must enter the inventory without a name-list edit."""
        from scripts.parallel_unittest import _census

        suite = self.synthetic_consumers('''import unittest
def make_fixture(test):
    from tests.test_workers import _EffectWorkerCase as Fixture
    return Fixture(test, object())
class Parent(unittest.TestCase):
    def helper(self):
        return make_fixture(self)
    def test_effect(self):
        self.helper().execute()
class FutureConsumer(Parent):
    pass
''')
        consumers = self.fixed_parent_consumers(suite)
        self.assertIn("future_consumers.FutureConsumer", consumers)
        census = _census(suite)
        classified = self.apply(census, {})
        with self.assertRaisesRegex(AssertionError, "fixed work parent"):
            for unit in classified["units"]:
                if any(identity in consumers for identity in unit["classes"]):
                    self.assertIsNotNone(unit["serial_reason"], "fixed work parent requires serial ordering")

    def test_qualified_stored_and_subclassed_fixture_references_are_consumers(self):
        variants = (
            "import tests.test_workers as workers\n        workers._EffectWorkerCase(self, object())",
            "from tests import test_workers as workers\n        self.factory = workers._EffectWorkerCase\n        self.factory(self, object())",
            "from tests.test_workers import _EffectWorkerCase as Base\n        class Fixture(Base):\n            pass\n        Fixture(self, object())",
        )
        for body in variants:
            with self.subTest(body=body):
                suite = self.synthetic_consumers(
                    "import unittest\nclass FutureConsumer(unittest.TestCase):\n"
                    "    def test_effect(self):\n        " + body + "\n"
                )
                self.assertEqual({"future_consumers.FutureConsumer"}, self.fixed_parent_consumers(suite))

    def test_wrapped_fixture_consumer_is_traced_without_running_wrapper(self):
        sources = ('''import unittest
from unittest import mock
class FutureConsumer(unittest.TestCase):
    @mock.patch('floati.workers.apply_worker_isolation')
    def test_effect(self, mocked):
        from tests.test_workers import _EffectWorkerCase as Fixture
        Fixture(self, object())
''', '''import unittest
from unittest import mock
@mock.patch('floati.workers.apply_worker_isolation')
def make(test, mocked):
    from tests.test_workers import _EffectWorkerCase as Fixture
    Fixture(test, object())
class FutureConsumer(unittest.TestCase):
    def test_effect(self):
        make(self)
''')
        for source in sources:
            with self.subTest(source=source):
                suite = self.synthetic_consumers(source)
                self.assertEqual({"future_consumers.FutureConsumer"}, self.fixed_parent_consumers(suite))

    def test_static_event_factory_alone_does_not_need_the_fixed_parent(self):
        suite = self.synthetic_consumers('''import unittest
class FutureConsumer(unittest.TestCase):
    def test_effect(self):
        from tests.test_workers import _EffectWorkerCase as Fixture
        Fixture.intent_event()
''')
        self.assertEqual(set(), self.fixed_parent_consumers(suite))

    def test_unreviewed_direct_and_stored_root_uses_fail_closed(self):
        for body in ("root()", "self.supplier = root\n        self.supplier()"):
            with self.subTest(body=body):
                suite = self.synthetic_consumers(
                    "import unittest\nclass FutureConsumer(unittest.TestCase):\n"
                    "    def test_effect(self):\n"
                    "        from floati.host_paths import worker_workspace_root as root\n"
                    "        " + body + "\n"
                )
                with self.assertRaisesRegex(ValueError, "unreviewed fixed work parent use.*FutureConsumer"):
                    self.fixed_parent_consumers(suite)

    def test_dynamic_fixture_lookup_refuses_instead_of_omitting_a_consumer(self):
        suite = self.synthetic_consumers('''import unittest
class FutureConsumer(unittest.TestCase):
    def test_effect(self):
        import tests.test_workers as workers
        name = '_EffectWorkerCase'
        getattr(workers, name)(self, object())
''')
        with self.assertRaisesRegex(ValueError, "dynamic fixed-parent lookup requires review"):
            self.fixed_parent_consumers(suite)

    def test_unloaded_aliased_and_transitive_helpers_require_source_review(self):
        """An unloaded relevant helper must refuse, not vanish from the census."""
        with tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT) as temporary:
            source_root = Path(temporary)
            (source_root / "tests").mkdir()
            (source_root / "tests" / "future_root.py").write_text(
                "from floati.host_paths import worker_workspace_root as root\n"
                "def make():\n    return root()\n"
            )
            for source in (
                "from floati.host_paths import worker_workspace_root as root\n"
                "def make():\n    return root()\n",
                "import tests.future_root as helper\n"
                "def make():\n    return helper.make()\n",
            ):
                with self.subTest(source=source):
                    (source_root / "tests" / "future_helper.py").write_text(source)
                    suite = self.synthetic_consumers('''import unittest
class FutureConsumer(unittest.TestCase):
    def test_effect(self):
        from tests.future_helper import make
        make()
''')
                    with self.assertRaisesRegex(ValueError, "unloaded test helper requires review: tests.future_helper"):
                        self.fixed_parent_consumers(suite, source_root=source_root)

    def census(self):
        return {"ids": ["tests.m.A.test_a", "tests.m.B.test_b", "tests.n.C.test_c"],
                "units": [{"key": "module:m", "ids": ["tests.m.A.test_a", "tests.m.B.test_b"],
                           "classes": ["tests.m.A", "tests.m.B"], "serial_reason": None},
                          {"key": "class:n.C", "ids": ["tests.n.C.test_c"],
                           "classes": ["tests.n.C"], "serial_reason": None}],
                "population_digest": "unchanged"}

    def apply(self, census, rules):
        from scripts.parallel_isolation import apply_serial_rules
        return apply_serial_rules(census, rules)

    def test_serial_class_promotes_whole_fixture_unit_without_reordering(self):
        census = self.census()
        before = copy.deepcopy(census)
        classified = self.apply(census, {"tests.m.B": "writes shared checkout"})
        self.assertEqual(before, census)
        self.assertEqual(census["ids"], classified["ids"])
        self.assertEqual(census["units"][0]["ids"], classified["units"][0]["ids"])
        self.assertIn("tests.m.B", classified["units"][0]["serial_reason"])
        self.assertIn("writes shared checkout", classified["units"][0]["serial_reason"])
        self.assertIsNone(classified["units"][1]["serial_reason"])
        self.assertEqual("unchanged", classified["population_digest"])

    def test_stale_or_wildcard_serial_rules_refuse(self):
        for rules in ({"tests.stale.Missing": "hazard"}, {"tests.m.*": "hazard"},
                      {"tests.m.A": ""}, {"tests.m.A": None}):
            with self.subTest(rules=rules):
                with self.assertRaises(ValueError):
                    self.apply(self.census(), rules)

    def test_existing_and_multiple_resource_reasons_survive_promotion(self):
        census = self.census()
        census["units"][0]["serial_reason"] = "native fixture restriction"
        classified = self.apply(census, {"tests.m.A": "resource A", "tests.m.B": "resource B"})
        for reason in ("native fixture restriction", "resource A", "resource B"):
            self.assertIn(reason, classified["units"][0]["serial_reason"])

    def test_static_inventory_rules_resolve_to_real_unittest_classes(self):
        rules = json.loads((REPOSITORY / "scripts/parallel_serial_classes.json").read_text())
        self.assertTrue(rules, "the serial hazard inventory must not be empty")
        for identity, reason in rules.items():
            with self.subTest(identity=identity):
                module_name, separator, class_name = identity.rpartition(".")
                self.assertTrue(separator, "a serial rule must name its test module")
                case = getattr(importlib.import_module(module_name), class_name)
                self.assertIsInstance(case, type)
                self.assertTrue(issubclass(case, unittest.TestCase))
                self.assertEqual(identity, f"{case.__module__}.{case.__qualname__}")
                self.assertIsInstance(reason, str)
                self.assertTrue(reason.strip(), "a serial rule must state its resource reason")

    def test_fixed_work_parent_consumers_remain_whole_in_exclusive_phase(self):
        """Catch an indirect effect fixture escaping its prerequisite ordering."""
        from floati.worker_errors import WorkerAdapterFailure
        from floati.worker_isolation import prepare_worker_isolation
        from scripts.parallel_plan import make_plan
        from scripts.parallel_unittest import discover_suite

        # Witness the preparation contract using only an owned absent parent.
        # Never inspect or change the host's fixed floati-work directory.
        with tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT) as directory:
            root = Path(directory)
            tenant = root / "tenant"
            (tenant / "effects").mkdir(parents=True)
            absent_parent = root / "absent-work-parent"
            with self.assertRaises(WorkerAdapterFailure) as refused:
                prepare_worker_isolation(tenant, absent_parent / "work-item", "session")
            self.assertEqual("effect_worker_isolation_unavailable", refused.exception.code)
            self.assertFalse(absent_parent.exists())

        suite, census = discover_suite(REPOSITORY)
        consumers = self.fixed_parent_consumers(suite)
        rules = json.loads((REPOSITORY / "scripts/parallel_serial_classes.json").read_text())
        classified = self.apply(census, rules)
        plan = make_plan(classified, workers=8)
        serial = next(item for item in plan["assignments"] if item["phase"] == "serial")
        self.assertEqual(census["ids"], classified["ids"])
        self.assertCountEqual(census["ids"], [
            test_id for item in plan["assignments"] for test_id in item["ids"]
        ])
        for identity in consumers:
            with self.subTest(identity=identity):
                units = [unit for unit in classified["units"] if identity in unit["classes"]]
                self.assertEqual(1, len(units), "class must remain atomic")
                unit = units[0]
                self.assertIsNotNone(unit["serial_reason"], "fixed work parent requires serial ordering")
                self.assertIn(unit["key"], serial["unit_keys"])
                self.assertEqual(unit["ids"], [
                    test_id for test_id in serial["ids"] if test_id in unit["ids"]
                ])


if __name__ == "__main__":
    unittest.main()

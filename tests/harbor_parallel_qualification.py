"""Fixed-profile qualification admission and tiny real paired-run controls.

The import packages and tool executables below are explicitly private routing
fixtures. They do not qualify Pillow, jsonschema, Minisign, Node or performance.
No installed profile is changed. Full-host qualification remains separate.
"""
from __future__ import annotations

import copy
import importlib.util
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from tests.temp_roots import REAL_TEMP_ROOT


SOURCE = Path(__file__).resolve().parents[1]
SCRIPTS = ("test-parallel.py", "parallel_plan.py", "parallel_unittest.py",
           "parallel_isolation.py", "parallel_processes.py", "parallel_profile.py",
           "fixture_finalizer.py")


class ParallelQualificationTests(unittest.TestCase):
    def helper(self):
        path = SOURCE / "scripts/parallel_qualification.py"
        self.assertTrue(path.is_file(),
            "fixed-profile paired qualification is absent; the generic runner cannot supply project-job parity")
        specification = importlib.util.spec_from_file_location("qualification_contract", path)
        module = importlib.util.module_from_spec(specification)
        specification.loader.exec_module(module)
        return module

    def fixture(self, *, bank=None):
        if bank is None:
            temporary = tempfile.TemporaryDirectory(prefix="qualification-owned-", dir=REAL_TEMP_ROOT)
            self.addCleanup(temporary.cleanup)
            self.bank = Path(temporary.name).resolve()
        else:
            # This narrowly composed caller owns removal, including retained
            # producer failures; no TemporaryDirectory finalizer owns its data.
            self.bank = Path(bank)
            info = self.bank.lstat()
            self.assertEqual(self.bank, self.bank.resolve(strict=True))
            self.assertTrue(stat.S_ISDIR(info.st_mode))
            self.assertEqual(os.getuid(), info.st_uid)
            self.assertEqual(0o700, stat.S_IMODE(info.st_mode))
            self.assertEqual([], list(self.bank.iterdir()))
        self.root = self.bank / "repo"
        self.root.mkdir(mode=0o700)
        scripts = self.root / "scripts"
        scripts.mkdir(mode=0o700)
        for name in SCRIPTS:
            shutil.copyfile(SOURCE / "scripts" / name, scripts / name)
        (scripts / "parallel_serial_classes.json").write_text("{}\n")
        (self.root / "subject.txt").write_text("original tracked input\n")
        self.imports = self.bank / "profile-imports"
        self.imports.mkdir(mode=0o700)
        for name in ("PIL", "jsonschema"):
            package = self.imports / name
            package.mkdir(mode=0o700)
            (package / "__init__.py").write_text(
                "__version__ = 'routing-fixture-only'\nROUTING_FIXTURE = True\n")
        self.tools = {}
        for name in ("minisign", "node"):
            path = self.bank / name
            path.write_text("#!/bin/sh\nprintf '%s\\n' 'routing-fixture-only'\n")
            path.chmod(0o700)
            self.tools[name] = str(path)
        self.home, self.temporary = self.bank / "home", self.bank / "tmp"
        self.home.mkdir(mode=0o700)
        self.temporary.mkdir(mode=0o700)
        self.git("init", "-q")
        self.write_cases()
        # Linux CPython writes __pycache__ beside these scripts on import.
        # Bytecode is not committed source; the scratch repository must say so
        # or qualification refuses the tree it just committed.
        (self.root / ".gitignore").write_text("__pycache__/\n*.pyc\n")
        self.commit()
        self.profile = {
            "git_common_dirs": [str(self.root / ".git")],
            "interpreter": str(Path(sys.executable).resolve(strict=True)),
            "jobs": {"unittest": {"kind": "fixed-python"}},
            "tools": {"python_import_root": str(self.imports),
                "minisign_executable": self.tools["minisign"],
                "node_executable": self.tools["node"]},
        }
        self.registry = self.bank / "project-job-profiles.json"
        self.write_registry()

    def git(self, *arguments, cwd=None):
        return subprocess.check_output(["/usr/bin/git", *arguments], cwd=cwd or self.root,
            stderr=subprocess.STDOUT, text=True).strip()

    def commit(self):
        self.git("add", ".")
        self.git("-c", "user.name=Qualification Fixture", "-c", "user.email=fixture@example.invalid",
                 "commit", "-qm", "private qualification fixture")

    def write_registry(self, document=None):
        if document is None:
            document = {"schema_version": 1, "profiles": {self.helper().PROFILE_NAME: self.profile}}
        self.registry.write_text(json.dumps(document, sort_keys=True) + "\n")
        self.registry.chmod(0o600)

    def write_cases(self, *, mutate=False):
        # Eight independent classes make eight nonempty native assignments.
        # Every method observes actual inherited imports/tools and an actual
        # grandchild; assertions below consume those retained observations.
        text = '''import json, os, subprocess, sys, unittest
from pathlib import Path
import PIL, jsonschema
ROOT = Path(__file__).resolve().parent
def observed():
 return {"pid": os.getpid(), "home": os.environ.get("HOME"),
  "xdg": [os.environ.get(k) for k in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME")],
  "imports": {"PIL": PIL.__file__, "jsonschema": jsonschema.__file__},
  "tools": {k: os.environ.get(k) for k in ("FLOATI_TEST_MINISIGN_EXECUTABLE", "FLOATI_TEST_NODE_EXECUTABLE")}}
class Base:
 def test_observe(self):
  childcode = "import json,os,PIL,jsonschema; print(json.dumps({'home':os.environ.get('HOME'),'imports':{'PIL':PIL.__file__,'jsonschema':jsonschema.__file__},'tools':{k:os.environ.get(k) for k in ('FLOATI_TEST_MINISIGN_EXECUTABLE','FLOATI_TEST_NODE_EXECUTABLE')}}))"
  child = subprocess.run([sys.executable, '-c', childcode], capture_output=True, text=True, check=True)
  row = observed(); row['grandchild'] = json.loads(child.stdout)
  out = ROOT / 'observations'; out.mkdir(exist_ok=True)
  with (out / (str(os.getpid()) + '.jsonl')).open('a') as stream: stream.write(json.dumps(row) + '\\n')
'''
        if mutate:
            text += "  if (ROOT.parent / 'pair' / 'serial' / 'final.json').exists(): (ROOT / 'subject.txt').write_text('changed during parallel\\n')\n"
        for ordinal in range(8):
            text += "class Case%d(Base, unittest.TestCase): pass\n" % ordinal
        (self.root / "test_cases.py").write_text(text)

    def registry_patch(self, helper):
        # A test-owned module constant replacement, never a public path flag.
        return mock.patch.object(helper, "PROFILE_REGISTRY", self.registry)

    def test_fixed_registry_is_read_only_and_inert_extra_job_grants_no_override(self):
        helper = self.helper()
        self.fixture()
        self.profile["jobs"]["future-owner-approved-job"] = {"kind": "fixed-python"}
        self.write_registry()
        before = self.registry.read_bytes(), self.registry.stat().st_mode
        with self.registry_patch(helper):
            profile = helper.read_profile()
        self.assertEqual(self.profile, profile)
        self.assertEqual(before, (self.registry.read_bytes(), self.registry.stat().st_mode))

    def test_registry_duplicate_keys_aliases_modes_and_unknown_tools_refuse(self):
        helper = self.helper()
        self.fixture()
        original = self.registry.read_bytes()
        broken = copy.deepcopy(self.profile)
        broken["tools"]["caller_interpreter"] = "/bin/sh"
        variants = [json.dumps({"schema_version": 1, "profiles": {helper.PROFILE_NAME: broken}}).encode(),
                    original.replace(b'"schema_version": 1', b'"schema_version": 1, "schema_version": 1')]
        for case_index, raw in enumerate(variants):
            with self.subTest(case=(
                "caller-interpreter-bin-sh",
                "duplicate-schema-version-key",
            )[case_index]):
                self.registry.write_bytes(raw)
                with self.registry_patch(helper), self.assertRaises(ValueError):
                    helper.read_profile()
                self.assertEqual(raw, self.registry.read_bytes())
        self.registry.write_bytes(original)
        self.registry.chmod(0o644)
        with self.registry_patch(helper), self.assertRaises(ValueError):
            helper.read_profile()
        self.registry.chmod(0o600)
        saved = self.bank / "saved-registry"
        self.registry.rename(saved)
        self.registry.symlink_to(saved)
        try:
            with self.registry_patch(helper), self.assertRaises(ValueError):
                helper.read_profile()
            self.assertEqual(original, saved.read_bytes())
        finally:
            self.registry.unlink()
            saved.rename(self.registry)

    def test_common_directory_admits_real_linked_worktree_and_refuses_foreign_repo(self):
        helper = self.helper()
        self.fixture()
        linked = self.bank / "linked"
        self.git("worktree", "add", "--detach", str(linked), "HEAD")
        identity = helper.validate_repository(linked, self.profile)
        self.assertEqual(str(self.root / ".git"), str(identity["git_common_dir"]))
        foreign = self.bank / "foreign"
        foreign.mkdir()
        self.git("init", "-q", cwd=foreign)
        with self.assertRaises(ValueError):
            helper.validate_repository(foreign, self.profile)

    def test_environment_uses_only_profile_code_tools_and_private_data_roots(self):
        helper = self.helper()
        self.fixture()
        inherited = {"HOME": "/untrusted-home", "PATH": "/untrusted-bin", "LANG": "C",
            "PYTHONPATH": "/untrusted-code", "PYTHONHOME": "/untrusted-python",
            "GIT_DIR": "/untrusted-git", "LD_PRELOAD": "/untrusted-library",
            "FLOATI_TEST_MINISIGN_EXECUTABLE": "/untrusted-minisign"}
        before = dict(inherited)
        env = helper.make_environment(self.profile, inherited, self.home, self.temporary)
        self.assertEqual(before, inherited)
        self.assertEqual("/usr/bin:/bin:/usr/sbin:/sbin", env["PATH"])
        self.assertEqual(str(self.imports), env["PYTHONPATH"])
        self.assertEqual(str(self.home), env["HOME"])
        for key in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME"):
            self.assertTrue(Path(env[key]).is_relative_to(self.home))
        for key in ("TMPDIR", "TMP", "TEMP"):
            self.assertEqual(self.temporary, Path(env[key]).resolve())
        self.assertEqual(self.tools["minisign"], env["FLOATI_TEST_MINISIGN_EXECUTABLE"])
        self.assertEqual(self.tools["node"], env["FLOATI_TEST_NODE_EXECUTABLE"])
        for key in ("PYTHONHOME", "GIT_DIR", "LD_PRELOAD"):
            self.assertNotIn(key, env)

    def test_real_preflight_retains_import_and_tool_parity_at_all_four_boundaries(self):
        helper = self.helper()
        self.fixture()
        env = helper.make_environment(self.profile, {}, self.home, self.temporary)
        receipt = helper.preflight(self.profile, self.root, env)
        self.assertTrue(receipt["successful"])
        self.assertEqual({"controller", "discovery", "worker", "grandchild"}, set(receipt["stages"]))
        for name, stage in receipt["stages"].items():
            with self.subTest(stage=name):
                self.assertNotEqual(os.getpid(), stage["pid"])
                self.assertTrue(Path(stage["home"]).is_relative_to(self.bank))
                for package in ("PIL", "jsonschema"):
                    self.assertEqual(self.imports / package / "__init__.py", Path(stage["imports"][package]))
                self.assertEqual(self.tools["minisign"], stage["tools"]["FLOATI_TEST_MINISIGN_EXECUTABLE"])
                self.assertEqual(self.tools["node"], stage["tools"]["FLOATI_TEST_NODE_EXECUTABLE"])
        self.assertFalse((self.root / "observations").exists(), "dependency preflight ran tests")

    def test_missing_profile_dependency_refuses_before_any_suite_method_runs(self):
        helper = self.helper()
        self.fixture()
        shutil.rmtree(self.imports / "PIL")
        with self.registry_patch(helper), self.assertRaises(ValueError):
            helper.run_pair(self.root, self.bank / "pair")
        self.assertFalse((self.root / "observations").exists())

    def test_dirty_or_untracked_discoverable_source_refuses_before_execution(self):
        helper = self.helper()
        self.fixture()
        subject = self.root / "subject.txt"
        subject.write_text("dirty tracked input\n")
        with self.registry_patch(helper), self.assertRaises(ValueError):
            helper.run_pair(self.root, self.bank / "dirty-pair")
        subject.write_text("original tracked input\n")
        (self.root / "test_untracked.py").write_text("raise RuntimeError('must never import')\n")
        with self.registry_patch(helper), self.assertRaises(ValueError):
            helper.run_pair(self.root, self.bank / "untracked-pair")
        self.assertFalse((self.root / "observations").exists())

    def test_scratch_commit_qualifies_while_host_checkout_and_bytecode_are_untracked(self):
        status_command = ["/usr/bin/git", "status", "--porcelain", "--untracked-files=all"]
        source_status_before = subprocess.check_output(status_command, cwd=SOURCE, text=True)

        def assert_source_unchanged(phase):
            self.assertEqual(source_status_before,
                subprocess.check_output(status_command, cwd=SOURCE, text=True),
                "runner self-test changed the running checkout " + phase)

        self.addCleanup(assert_source_unchanged, "after cleanup")
        # Linux CPython writes __pycache__ beside imported scratch scripts.
        # That bytecode is not committed source. An untracked file in the
        # checkout that launched the test is not the scratch repository either.
        self.fixture()
        launching_checkout = self.bank / "launching-checkout"
        self.git("clone", "--quiet", "--no-hardlinks", str(self.root), str(launching_checkout))
        shutil.copyfile(SOURCE / "scripts/parallel_qualification.py",
            launching_checkout / "scripts/parallel_qualification.py")
        self.git("add", "scripts/parallel_qualification.py", cwd=launching_checkout)
        self.git("-c", "user.name=Qualification Fixture", "-c", "user.email=fixture@example.invalid",
                 "commit", "-qm", "private launching checkout fixture", cwd=launching_checkout)
        (launching_checkout / "tests").mkdir()
        host_dirt = launching_checkout / "tests" / "_host_checkout_dirt.py"
        self.addCleanup(host_dirt.unlink, missing_ok=True)
        host_dirt.write_text("raise RuntimeError('host checkout dirt')\n")
        assert_source_unchanged("while host dirt is planted")
        cache = self.root / "scripts" / "__pycache__"
        cache.mkdir()
        (cache / "parallel_isolation.cpython-39.pyc").write_bytes(b"\x00bytecode-not-source")
        # Launch from the owned clone, so the caller's untracked Python is
        # real without ever exposing it to this suite's source-identity check.
        child = subprocess.run([sys.executable, "-B", "-c", """
import json, sys
from pathlib import Path
sys.path.insert(0, str(Path.cwd() / 'scripts'))
import parallel_qualification as helper
helper.PROFILE_REGISTRY = Path(sys.argv[1])
receipt = helper.run_pair(Path(sys.argv[2]), Path(sys.argv[3]))
print(json.dumps({'launching_checkout': str(Path.cwd()), 'receipt': receipt}))
""", str(self.registry), str(self.root), str(self.bank / "pair")],
            cwd=launching_checkout, capture_output=True, text=True)
        self.assertEqual(0, child.returncode, child.stdout + child.stderr)
        observed = json.loads(child.stdout)
        self.assertEqual(str(launching_checkout), observed["launching_checkout"])
        receipt = observed["receipt"]
        assert_source_unchanged("after qualification with host dirt still planted")
        self.assertTrue(receipt["qualified"], receipt)
        self.assertNotEqual(launching_checkout, self.root.resolve())
        self.assertNotEqual(SOURCE.resolve(), launching_checkout)
        self.assertTrue(host_dirt.is_file())
        host_status = subprocess.check_output(
            ["/usr/bin/git", "status", "--porcelain", "--untracked-files=all",
             "--", "tests/_host_checkout_dirt.py"],
            cwd=launching_checkout, text=True)
        self.assertIn("_host_checkout_dirt.py", host_status)
        self.assertNotIn("parallel_isolation.cpython-39.pyc", self.git("ls-files"))

    def test_real_pair_qualifies_same_native_population_with_eight_private_workers(self):
        helper = self.helper()
        self.fixture()
        output = self.bank / "pair"
        with self.registry_patch(helper):
            receipt = helper.run_pair(self.root, output)
        serial = json.loads((output / "serial/final.json").read_text())
        parallel = json.loads((output / "parallel/final.json").read_text())
        self.assertTrue(receipt["qualified"], receipt)
        self.assertTrue(receipt["cleanup"]["successful"])
        self.assertEqual("serial", serial["mode"])
        self.assertEqual(8, parallel["workers"])
        self.assertTrue(parallel["comparison"]["equivalent"])
        self.assertEqual(2, len(receipt["launches"]))
        for launch in receipt["launches"]:
            self.assertEqual(["-E", "-s", "-B"], launch["argv"][1:4])
            self.assertEqual("/usr/bin:/bin:/usr/sbin:/sbin", launch["environment"]["PATH"])
            self.assertEqual(str(self.imports), launch["environment"]["PYTHONPATH"])
            self.assertEqual(self.tools["minisign"], launch["environment"]["FLOATI_TEST_MINISIGN_EXECUTABLE"])
            self.assertEqual(self.tools["node"], launch["environment"]["FLOATI_TEST_NODE_EXECUTABLE"])
            self.assertNotEqual(os.environ["HOME"], launch["environment"]["HOME"])
        self.assertEqual(str(output / "serial/final.json"), parallel["profile"]["path"])
        self.assertEqual("serial reference", parallel["profile"]["source"])
        self.assertEqual(serial["source"], parallel["source"])
        self.assertEqual(serial["aggregate"]["selected_ids"], parallel["census"]["ids"])
        self.assertEqual(8, serial["aggregate"]["tests_run"])
        self.assertEqual(8, parallel["aggregate"]["tests_run"])
        rows = [json.loads(line) for path in (self.root / "observations").glob("*.jsonl")
                for line in path.read_text().splitlines()]
        self.assertEqual(16, len(rows))
        self.assertEqual(9, len({row["pid"] for row in rows}))
        private_temp = Path("\x2fprivate/tmp") if sys.platform == "darwin" else Path("\x2ftmp").resolve()
        for row in rows:
            self.assertTrue(Path(row["home"]).is_relative_to(private_temp))
            self.assertNotEqual(os.environ["HOME"], row["home"])
            self.assertTrue(all(Path(path).is_relative_to(Path(row["home"])) for path in row["xdg"]))
            self.assertEqual(row["home"], row["grandchild"]["home"])
            self.assertEqual(row["imports"], row["grandchild"]["imports"])
            self.assertEqual(row["tools"], row["grandchild"]["tools"])
            for package in ("PIL", "jsonschema"):
                self.assertEqual(str(self.imports / package / "__init__.py"), row["imports"][package])
        for result in (serial, parallel):
            self.assertTrue(result["cleanup"]["successful"])
            self.assertFalse(Path(result["scratch_root"]).exists())

    def test_parallel_source_drift_cannot_be_qualified_by_a_matching_serial_reference(self):
        helper = self.helper()
        self.fixture()
        self.write_cases(mutate=True)
        self.commit()
        with self.registry_patch(helper):
            receipt = helper.run_pair(self.root, self.bank / "pair")
        self.assertFalse(receipt["qualified"])
        self.assertNotEqual("original tracked input\n", (self.root / "subject.txt").read_text())
        self.assertTrue((self.bank / "pair/serial/final.json").is_file())

    def test_external_wall_gate_includes_actual_parallel_final_publication(self):
        """Boundary policy controls use actual publication, not timed evidence.

        Native child clocks remain unchanged. The controller sees exactly the
        acceptance limit or one second beyond it after the final file exists.
        """
        for target_wall, expected in ((600.0, True), (601.0, False)):
            with self.subTest(wall=target_wall):
                helper = self.helper()
                self.fixture()
                output = self.bank / "pair"
                def publication_clock():
                    return target_wall if (output / "parallel/final.json").is_file() else 0.0
                with self.registry_patch(helper), mock.patch.object(helper, "controller_clock", publication_clock):
                    receipt = helper.run_pair(self.root, output)
                parallel = json.loads((output / "parallel/final.json").read_text())
                self.assertTrue(parallel["qualified"], "native outcome parity must succeed before external cost refusal")
                self.assertIs(expected, receipt["qualified"], "published parity must retain the inclusive 600-second bound")
                self.assertEqual(600.0, receipt["parallel_limit_seconds"])
                if not expected:
                    self.assertIn("fixed 600-second qualification bound", receipt["error"])
                self.assertEqual(target_wall, receipt["parallel_wall_seconds"])
                self.assertTrue(receipt["cleanup"]["successful"])

    def test_unqualified_platform_refuses_before_opening_a_profile_coordinate(self):
        """A platform with no qualified profile never opens the installed registry.

        Darwin still attempts that registry and does not use this refusal.
        """
        helper = self.helper()
        touched = []
        original = helper._ordinary

        def record(value, **keywords):
            touched.append(os.fsdecode(value))
            return original(value, **keywords)

        caught = None
        try:
            with mock.patch.object(helper.sys, "platform", "linux"), mock.patch.object(helper, "_ordinary", record):
                helper.read_profile()
        except Exception as error:
            caught = error
        self.assertEqual(("ValueError", "unsupported_platform"), (
            None if caught is None else type(caught).__name__,
            None if caught is None else str(caught)))
        self.assertEqual([], touched)

        touched.clear()
        try:
            with mock.patch.object(helper.sys, "platform", "darwin"), mock.patch.object(helper, "_ordinary", record):
                helper.read_profile()
        except (OSError, ValueError) as error:
            self.assertNotEqual("unsupported_platform", str(error))
        self.assertEqual([os.fsdecode(helper.PROFILE_REGISTRY)], touched[:1])

    def test_private_registry_uses_the_running_interpreter_on_an_unqualified_platform(self):
        """Neutral admission must bind the interpreter that is actually running.

        A hardcoded host stub is absent on every other platform, so the private
        registry has to name the canonical running executable and still load.
        """
        helper = self.helper()
        self.fixture()
        running = str(Path(sys.executable).resolve(strict=True))
        self.assertEqual(running, self.profile["interpreter"])
        with mock.patch.object(helper.sys, "platform", "linux"), self.registry_patch(helper):
            loaded = helper.read_profile()
        self.assertEqual(running, loaded["interpreter"])


if __name__ == "__main__":
    unittest.main()

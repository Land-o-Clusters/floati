"""Native 1k teardown routing/admission controls, never performance evidence.

Each control first completes the actual private teardown without an observer
replacement. The immutable 1k seed is then restored at its original bound root.
Missing adapter routing, primary-seat layout admission, or exact subprocess
admission must not turn a native completed operation into an unsupported arm.
"""
from __future__ import annotations

import base64
import contextlib
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from tests.bus_scale_test_profile import StableSourceFixture
from unittest.mock import patch

from floati import cli
from floati.events import EventLog
from floati.git_process import fixed_git_command, fixed_git_environment
from floati.root import FloatiRoot
from floati.seat_teardown import completed_teardown_for_session
from floati.seat_teardown_prefix import prefix_proof
from tests.bus_scale_case_seeds import inventory, make_case_seed_pair
from tests.bus_scale_test_profile import full_scale_enabled, seed_counts
from tests.harbor_bus_scale_expanded_observation import ADAPTER, ENTRY, RecordingOwner, load
from tests.test_bus_scale_seat_teardown_boundary import ACTOR, SESSION, SOURCE, _NativePrelude, _inventory, _json
from tests.temp_roots import REAL_TEMP_ROOT, _PLATFORM_TEMP_ROOT


CASE = "seat-teardown"
SOURCE_FILES = (
    Path(__file__).resolve(), ENTRY, ADAPTER,
    SOURCE / "tests/test_bus_scale_seat_teardown_boundary.py",
    SOURCE / "tests/bus_scale_case_seeds.py",
    SOURCE / "tests/fixtures/bus_scale_census/bus-scale-routine-lifecycle-2026-10-15.py",
    SOURCE / "floati/seat_teardown.py", SOURCE / "floati/seat_teardown_activity.py",
)


class SeatTeardownObservationTests(StableSourceFixture, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._shared_native = None
        cls._shared_receipts = []
        cls.addClassCleanup(cls._close_shared_native)

    @classmethod
    def _close_shared_native(cls):
        fixture = cls._shared_native
        cls._shared_native = None
        if fixture is None:
            return
        fixture.doCleanups()
        for receipt in cls._shared_receipts:
            record = json.loads(receipt.read_bytes())
            record["scratch_removed"] = not Path(record["scratch"]).exists()
            record["shared_fixture_cleanup_pending"] = False
            _json(receipt, record)

    def setUp(self):
        if full_scale_enabled():
            return self._build_native_fixture()
        cls = type(self)
        fixture = cls._shared_native
        if fixture is not None and not fixture._restore_shared_start():
            # An authoritative source changed. Preserve its real evidence and
            # rebuild a genuine fixture; neither ctime nor inode is forged.
            fixture.doCleanups()
            cls._shared_native = fixture = None
        if fixture is None:
            fixture = cls()
            try:
                fixture._build_native_fixture()
                fixture._remember_shared_start()
            except BaseException:
                fixture.doCleanups()
                raise
            cls._shared_native = fixture
        for name in ("temporary", "base", "entry", "adapter", "delegate", "seat_home",
                     "workspace", "home", "place"):
            setattr(self, name, getattr(fixture, name))
        for name in ("pair", "context", "argv", "seed", "calls", "artifact"):
            setattr(self, name, copy.deepcopy(getattr(fixture, name)))
        # The genuine positive's Popen vectors remain testimony of that one
        # operation. Every body still performs its own route/entry/admission
        # assertion against the freshly restored, actually bound small seed.
        self.entry._TEARDOWN_GUARD = None
        self.retained = Path(tempfile.mkdtemp(prefix="bus-scale-seat-teardown-observation-",
                                             dir=_PLATFORM_TEMP_ROOT)).resolve()
        self.record = copy.deepcopy(fixture.record)
        self.record.update(test=self.id(), shared_genuine_base=str(fixture.retained))
        self._shared_fixture_user = True
        self.addCleanup(self.retain_and_remove)

    @staticmethod
    def _file_identity(path):
        info = path.lstat()
        return (info.st_dev, info.st_ino, info.st_mode, info.st_nlink,
                info.st_size, info.st_mtime_ns, info.st_ctime_ns)

    def _remember_shared_start(self):
        self._static_files, self._static_directories = {}, {}
        for path in self.base.rglob("*"):
            if path == self.home or self.home in path.parents:
                continue
            relative = path.relative_to(self.base).as_posix()
            self.assertFalse(path.is_symlink())
            if path.is_dir():
                self._static_directories[relative] = (path.stat().st_dev, path.stat().st_ino,
                                                      stat.S_IMODE(path.stat().st_mode))
            else:
                self._static_files[relative] = (self._file_identity(path), path.read_bytes())

    def _restored_auxiliary(self, path):
        # The real two-mode observer restores these exact Git auxiliaries by
        # replacement. Captured Popen vectors bind their coordinates, not old
        # inodes; each body resets the entry guard and proves current identity.
        # Immutable seed ledgers still require their complete original stat.
        return any(path == root or root in path.parents
                   for root in (self.workspace, self.base / "origin.git"))

    def _restore_shared_start(self):
        try:
            for relative, (identity, raw) in self._static_files.items():
                path = self.base / relative
                if path.lstat().st_uid != os.getuid():
                    return False
                actual = self._file_identity(path)
                if self._restored_auxiliary(path):
                    # Check actual type/mode/link count/size and bytes, without
                    # pretending the restored Git file has its former ctime.
                    same_identity = actual[0] == identity[0] and actual[2:5] == identity[2:5]
                else:
                    same_identity = actual == identity
                if not same_identity or path.read_bytes() != raw:
                    return False
            for relative, identity in self._static_directories.items():
                path = self.base / relative
                info = path.lstat()
                if info.st_uid != os.getuid():
                    return False
                actual = (info.st_dev, info.st_ino, stat.S_IMODE(info.st_mode))
                if self._restored_auxiliary(path):
                    same_identity = actual[0] == identity[0] and actual[2] == identity[2]
                else:
                    same_identity = actual == identity
                if not same_identity or not stat.S_ISDIR(info.st_mode):
                    return False
            # Body outputs are fixture-owned additions. Remove them without
            # changing any immutable seed, Git source, or retained native file.
            for path in sorted(self.base.rglob("*"), key=lambda item: len(item.parts), reverse=True):
                if path == self.home or self.home in path.parents:
                    continue
                relative = path.relative_to(self.base).as_posix()
                if relative in self._static_files or relative in self._static_directories:
                    continue
                path.rmdir() if path.is_dir() and not path.is_symlink() else path.unlink()
            self.restore_seed()
            return True
        except (OSError, ValueError):
            return False

    def _build_native_fixture(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="bus-scale-census-owned-teardown-observation-",
                                                     dir=REAL_TEMP_ROOT)
        self.base = Path(self.temporary.name).resolve()
        self.retained = Path(tempfile.mkdtemp(prefix="bus-scale-seat-teardown-observation-",
                                             dir=_PLATFORM_TEMP_ROOT)).resolve()
        self.record = {"test": self.id(), "performance_evidence": False,
                       "native_activity_unmodified": True, "scratch": str(self.base),
                       "source_before": {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in SOURCE_FILES},
                       "git_commands": []}
        self.addCleanup(self.retain_and_remove)
        self.record["source_head"] = self.git(SOURCE, "rev-parse", "HEAD").strip()
        self.entry = load(ENTRY, "teardown_observation_entry")
        self.adapter = load(ADAPTER, "teardown_observation_adapter")
        self.delegate = load(SOURCE / "tests/fixtures/bus_scale_census/run-bus-scale-census-2026-10-09.py",
                             "teardown_observation_owner")
        self.seat_home = self.base / "seat-home"
        self.seat_home.mkdir(mode=0o700)
        self.workspace = self.seat_home / "repo"
        self.workspace.mkdir(mode=0o700)
        remote = self.base / "origin.git"
        self.git(self.base, "init", "--quiet", "--bare", str(remote))
        self.git(self.workspace, "init", "--quiet", "-b", "main")
        for key, value in (("user.name", "Private Census Fixture"),
                           ("user.email", "fixture@example.invalid"), ("commit.gpgsign", "false")):
            self.git(self.workspace, "config", key, value)
        self.git(self.workspace, "remote", "add", "origin", str(remote))
        (self.workspace / "note.md").write_text("owned native teardown observation\n", encoding="utf-8")
        seeds = self.base / "seeds"
        seeds.mkdir(mode=0o700)
        self.pair = make_case_seed_pair(seeds, "events", CASE,
            _NativePrelude(self.git, self.seat_home), self.workspace, counts=seed_counts())
        self.home = self.pair["prepared_home"]
        self.context = self.pair["context"]
        self.context.update(case=CASE, actor=ACTOR, seed_home=str(self.home),
            bank=str(self.pair["preparation_bank"]), owned_scope=str(self.base),
            constructed_native_preface=True)
        self.argv = ["seat", "teardown", "--root", str(self.home), "--as", ACTOR,
            "--home", str(self.seat_home), "--session", SESSION, "--repo", "floati",
            "--doc", "note.md", "--idempotency-key", "census-private-teardown"]
        self.seed = inventory(self.pair["1"])
        self.restore_seed()
        self.assertEqual(seed_counts()[0], len(EventLog(FloatiRoot.open_direct_home(self.home)).event_records()))
        self.assertFalse(EventLog(FloatiRoot.open_direct_home(self.home)).unacked_ids(ACTOR))
        self.calls = []
        native = subprocess.Popen.__init__

        def capture(process, argv, *args, **kwargs):
            self.calls.append((list(argv), dict(kwargs)))
            return native(process, argv, *args, **kwargs)

        output, error = io.StringIO(), io.StringIO()
        with patch.object(subprocess.Popen, "__init__", capture),\
                contextlib.redirect_stdout(output), contextlib.redirect_stderr(error):
            code = cli.main(self.argv)
        (self.retained / "native-positive-stdout.txt").write_text(output.getvalue(), encoding="utf-8")
        (self.retained / "native-positive-stderr.txt").write_text(error.getvalue(), encoding="utf-8")
        self.artifact = json.loads(output.getvalue())
        _json(self.retained / "native-positive-artifact.json", self.artifact)
        self.record["native_positive_exit_code"] = code
        self.record["actual_popen"] = json.loads(json.dumps(self.calls, default=str))
        shutil.copytree(self.home, self.retained / "native-positive-root")
        self.assertEqual(0, code, "native prerequisite did not complete: " + output.getvalue())
        self.assertEqual("ok", self.artifact["status"])
        self.assertEqual("completed", self.artifact["evidence"]["step"])
        self.assertEqual(self.artifact["evidence"], completed_teardown_for_session(
            FloatiRoot.open_direct_home(self.home), ACTOR, SESSION))
        self.record["native_positive_completed_verified"] = True
        self.restore_seed()
        self.place = self.base / "observation"
        self.place.mkdir(mode=0o700)

    def git(self, workspace, *arguments):
        argv = fixed_git_command("/usr/bin/git", workspace, arguments)
        result = subprocess.run(argv, cwd=self.base, env=fixed_git_environment("/usr/bin/git"),
                                capture_output=True, text=True, timeout=30)
        self.record["git_commands"].append({"argv": argv, "exit_code": result.returncode,
                                            "stdout": result.stdout, "stderr": result.stderr})
        self.assertEqual(0, result.returncode, result.stderr)
        return result.stdout

    def restore_seed(self):
        shutil.rmtree(self.home)
        shutil.copytree(self.pair["1"], self.home)
        self.assertEqual(self.seed, inventory(self.home))

    def retain_and_remove(self):
        self.record["source_after"] = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in SOURCE_FILES}
        try:
            for path in SOURCE_FILES:
                destination = self.retained / "source" / path.relative_to(SOURCE)
                destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                shutil.copyfile(path, destination)
            shutil.copytree(self.base, self.retained / "fixture")
            self.record["fixture_inventory"] = _inventory(self.retained / "fixture")
        finally:
            if not getattr(self, "_shared_fixture_user", False):
                self.temporary.cleanup()
            self.record["scratch_removed"] = not self.base.exists()
            if getattr(self, "_shared_fixture_user", False):
                self.record["shared_fixture_cleanup_pending"] = True
                type(self)._shared_receipts.append(self.retained / "result.json")
            _json(self.retained / "result.json", self.record)
            print("native seat teardown observation evidence: " + str(self.retained), flush=True)

    def test_completed_native_teardown_is_routed_to_real_routine_arguments(self):
        # Removing this registered case must make a completed native workload
        # unsupported, rather than silently classifying refusal as an effect.
        helper = self.entry.routine_module(CASE)
        self.assertIn(CASE, helper.CASES)
        argv = self.entry.arguments(CASE, self.home, self.workspace, self.context)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = cli.main(argv)
        (self.base / "routed-stdout.txt").write_text(output.getvalue(), encoding="utf-8")
        self.assertEqual(0, code, output.getvalue())
        artifact = json.loads(output.getvalue())
        self.assertEqual("completed", artifact["evidence"]["step"])
        self.assertEqual(artifact["evidence"], completed_teardown_for_session(
            FloatiRoot.open_direct_home(self.home), ACTOR, SESSION))

    def test_actual_primary_seat_layout_is_admitted_by_owned_native_entry(self):
        # The operation requires HOME/repo; changing it to workspace breaks
        # primary-seat declaration, so the child admission must accept repo.
        _json(self.home.parent / "case-context.json", self.context)
        output, error, detail = (self.home.parent / name for name in
                                 ("entry-stdout.txt", "entry-stderr.txt", "entry-detail.json"))
        environment = {"HOME": str(Path.home()), "PATH": "/usr/bin:/bin",
                       "PYTHONNOUSERSITE": "1", "PYTHONDONTWRITEBYTECODE": "1"}
        argv = ["/usr/bin/python3", "-B", str(ENTRY), "cpu", CASE, str(self.home),
                str(self.workspace), str(detail), "owned-census-v1"]
        with output.open("xb") as out, error.open("xb") as err:
            child = self.delegate.launch_owned_child(argv, cwd=self.workspace, env=environment,
                                                     stdout=out, stderr=err)
            collected = self.delegate.collect_owned_child(child, deadline_seconds=120, collection_seconds=10)
        self.record["entry_collection"] = collected
        self.assertTrue(collected["child_collected"])
        self.assertEqual(0, collected["exit_code"], error.read_text())
        self.assertEqual("completed", json.loads(output.read_text())["evidence"]["step"])
        self.assertEqual(0, json.loads(detail.read_text())["native_exit_code"])

    def test_completed_native_teardown_has_two_archived_modes_and_exact_final_restore(self):
        # Nearest existing control: reuse this real two-mode operation for the
        # cut bank's positive admission and bounded corruption replays.
        from floati.cut_prefix import warm as warm_cut
        from tests.fixtures.bus_scale_census import bus_scale_cut_prefix_effects as cut
        native = self.adapter.load_entry(ENTRY)
        helper = native.routine_module(CASE)
        prepare = self.adapter.prepare_teardown_inbox
        normalize = helper.normalize
        project = self.adapter.teardown_bus_tree
        checked = []

        def warmed(home):
            self.assertIsNotNone(warm_cut(FloatiRoot.open_direct_home(home)))
            return prepare(home)

        def normalized(case, delta, artifact, context, **bounds):
            positive = normalize(case, delta, artifact, context, **bounds)
            self.assertIn(cut.SUMMARY, positive["delta"],
                          "a native cut append needs its own semantic certificate")
            if "unexplained-node" not in checked:
                home = Path(bounds["home"])
                retained = self.adapter.tree(home)
                directory = home / cut.CACHE / "nodes"
                info = directory.stat()
                extra = directory / ("f" * 64)
                self.assertFalse(extra.exists())
                try:
                    extra.write_bytes(b"unexplained cut node\n")
                    extra.chmod(0o600)
                    after = helper.snapshot(home)
                    changed = cut.raw_candidate_delta(bounds["before_raw_files"], after)
                    with self.assertRaises(ValueError):
                        normalize(case, changed, artifact, context, **bounds)
                    self.assertEqual(after, helper.snapshot(home),
                                     "cut refusal must preserve the complete supplied bytes")
                    checked.append("unexplained-node")
                finally:
                    extra.unlink()
                    os.utime(directory, ns=(info.st_atime_ns, info.st_mtime_ns))
                self.assertEqual(retained, self.adapter.tree(home),
                                 "corruption replay must restore collected bytes and metadata")
            return positive

        def projected(before, after, normalized, begin, end, *args, **kwargs):
            positive = project(before, after, normalized, begin, end, *args, **kwargs)
            self.assertIn(cut.CACHE, positive["entries"],
                          "teardown equivalence must carry the independent cut certificate")
            if "content" not in checked:
                retained = next(path for path in before["entries"]
                    if path.startswith(cut.CACHE + "/nodes/")
                    and before["entries"][path] == after["entries"].get(path))
                for fault in ("content", "mode", "mtime"):
                    with self.subTest(cut_fault=fault):
                        poisoned = copy.deepcopy(after)
                        row = poisoned["entries"][retained]
                        if fault == "content":
                            row["sha256"] = "0" * 64
                        elif fault == "mode":
                            row["mode"] = 0o644
                        else:
                            row["mtime_ns"] += 1
                        with self.assertRaises((ValueError, self.adapter.UnresolvedObservation)):
                            project(before, poisoned, normalized, begin, end, *args, **kwargs)
                        checked.append(fault)
            return positive

        owner = RecordingOwner(self.delegate, self.seed)
        with patch.object(self.adapter, "load_entry", return_value=native),\
                patch.object(native, "routine_module", return_value=helper),\
                patch.object(self.adapter, "prepare_teardown_inbox", warmed),\
                patch.object(helper, "normalize", normalized),\
                patch.object(self.adapter, "teardown_bus_tree", projected):
            arm = self.adapter.expanded_observation(owner, CASE, self.pair, "1",
                                                    self.workspace, self.place, ENTRY)
        self.assertEqual(["unexplained-node", "content", "mode", "mtime"], checked)
        self.assertEqual({"exit_code": 0, "status": "ok", "effect": {
            "kind": "seat_teardown_completed", "count": 1}}, arm["native"])
        self.assertEqual(seed_counts()[0], arm["records"])
        self.assertTrue(arm["child_collected"])
        self.assertEqual({"cpu": True, "bytes": True, "locks": True}, arm["coverage"])
        self.assertEqual(["cpu", "diagnostic"], [row[0][3] for row in owner.launches])
        self.assertEqual(self.seed, inventory(self.home), "final restore must preserve the actual bound seed")
        self.assertEqual(self.seed, inventory(self.pair["1"]), "immutable input must survive both native modes")
        self.assertEqual(hashlib.sha256((self.place / "collection.json").read_bytes()).hexdigest(),
                         arm["raw_sha256"])
        self.assertEqual(2, len(owner.admitted_inputs))
        for index, mode in enumerate(("cpu", "diagnostic")):
            bank = self.place / mode
            artifact = json.loads((bank / "stdout.txt").read_bytes())
            self.assertEqual("completed", artifact["evidence"]["step"])
            self.assertEqual(2, artifact["evidence"]["schema_version"])
            initial_events = EventLog(FloatiRoot.open_direct_home(self.pair["1"])).event_records()
            self.assertEqual(seed_counts()[0], len(initial_events))
            self.assertEqual(prefix_proof(initial_events, "events.jsonl"),
                             artifact["evidence"]["event_cutoff"])
            collection = json.loads((bank / "collection.json").read_bytes())
            self.assertTrue(collection["collection"]["child_collected"])
            self.assertEqual(0, collection["collection"]["exit_code"])
            self.assertGreater(collection["cpu_seconds"], 0)
            self.assertEqual(hashlib.sha256((bank / "stdout.txt").read_bytes()).hexdigest(),
                             collection["stdout_sha256"])
            restore = json.loads((self.place / (mode + "-restore.json")).read_bytes())
            self.assertTrue(restore["restored_exact"])
            before = {path: base64.b64decode(raw) for path, raw in
                      json.loads((bank / "before-raw.json").read_bytes()).items()}
            # RecordingOwner captures every complete launch input after its
            # read-only warmed-clone proof, outside the measured child.
            self.assertEqual(owner.admitted_inputs[index], before)
            receipts = (bank / "scale-fleet/receipts/seat-teardown/scale-worker.jsonl").read_text().splitlines()
            self.assertEqual(["started", "paused", "reported", "completed"],
                             [json.loads(row)["step"] for row in receipts])
            for name in ("detail.json", "native-delta.json", "equivalence.json", "auxiliary-proof.json"):
                self.assertTrue((bank / name).is_file(), "complete native testimony must precede next restore")

    def activity_calls(self):
        calls = [(argv, kwargs) for argv, kwargs in self.calls if kwargs.get("cwd") == "/"]
        self.assertTrue(any(argv[0] == "/usr/bin/git" for argv, _kwargs in calls),
                        "positive control must capture actual native Git activity")
        if sys.platform == "darwin":
            # The native observer brackets cwd observation with two PID
            # snapshots. Each snapshot permits one metadata-only retry; the
            # exact captured vectors still pass the admission checks below.
            ps_calls = sum(argv == ["/bin/ps", "-A", "-o", "pid=,ppid=,uid=,state="]
                           for argv, _kwargs in calls)
            self.assertGreaterEqual(ps_calls, 2)
            self.assertLessEqual(ps_calls, 4)
            self.assertTrue(any(argv[0] in {"/usr/sbin/lsof", "/usr/bin/lsof"}
                and argv[1:] == ["-n", "-P", "-a", "-u", str(os.getuid()), "-d", "cwd", "-F0pfn"]
                for argv, _kwargs in calls))
        return calls

    def test_actual_fixed_activity_subprocesses_are_admitted_for_exact_teardown_case(self):
        for argv, kwargs in self.activity_calls():
            with self.subTest(argv=argv):
                self.assertTrue(self.entry.routine_subprocess(CASE, argv, kwargs, self.home,
                                                               self.workspace, self.context), (argv, kwargs))

    def test_teardown_activity_policy_refuses_changed_vectors_environment_and_coordinates(self):
        foreign = self.base / "foreign-repo"
        foreign.mkdir(mode=0o700)
        for argv, kwargs in self.activity_calls():
            with self.subTest(argv=argv):
                policy = self.entry.routine_subprocess
                for changed_argv, changed_kwargs in (
                    (argv + ["--unreviewed"], kwargs),
                    (argv, dict(kwargs, cwd=str(self.workspace))),
                    (argv, dict(kwargs, shell=True)),
                    (argv, dict(kwargs, executable="/bin/sh")),
                    (argv, dict(kwargs, env=dict(kwargs["env"], GIT_DIR=str(self.home)))),
                    (argv, dict(kwargs, stdout=1)),
                    (argv, dict(kwargs, start_new_session=False)),
                    (argv, {key: value for key, value in kwargs.items() if key != "start_new_session"}),
                ):
                    self.assertFalse(policy(CASE, changed_argv, changed_kwargs,
                                             self.home, self.workspace, self.context))
                self.assertFalse(policy("node-prep-clear", argv, kwargs, self.home,
                                        self.workspace, self.context))
                self.assertFalse(policy(CASE, argv, kwargs, self.home, foreign, self.context))
        self.assertEqual(self.seed, inventory(self.home), "admission is read-only")

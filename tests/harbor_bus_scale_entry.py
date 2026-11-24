"""Native entry policy and interval controls without launching host processes."""

from __future__ import annotations

import copy
import importlib.util
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from floati.git_process import fixed_git_command, fixed_git_environment
from tests.bus_scale_io import IOProbe
from tests.temp_roots import REAL_TEMP_ROOT


_ENTRY_PATH = (
    Path(__file__).resolve().parents[1]
    / "tests/fixtures/bus_scale_census/bus-scale-native-entry-2026-10-09.py"
)
_SPEC = importlib.util.spec_from_file_location("bus_scale_native_entry_controls", _ENTRY_PATH)
_entry = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_entry)


def _hold(path, start, finish, *, mode="shared"):
    """Complete synthetic acquired/released interval, in seconds from origin."""
    acquired = int(start * 1e9)
    released = int(finish * 1e9)
    return {
        "path": path,
        "mode": mode,
        "fd": 17,
        "generation": 1,
        "device": 1,
        "inode": 2,
        "acquire_call_before_ns": acquired - 1,
        "acquire_call_after_ns": acquired,
        "release_call_before_ns": released - 1,
        "release_call_after_ns": released,
        "seconds": finish - start,
        "release": "unlock",
    }


class BusScaleNativeEntryPolicyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT, prefix="bus-scale-entry-controls-")
        self.addCleanup(temporary.cleanup)
        self.scratch = Path(temporary.name).resolve()
        self.home = self.scratch / "home"
        self.home.mkdir(mode=0o700)
        self.fixture = self.scratch / "scale-fleet"
        self.fixture.mkdir(mode=0o700)
        self.doctor_env = {
            "HOME": str(self.home), "PATH": "/usr/bin:/bin", "GIT_ATTR_NOSYSTEM": "1",
        }
        self.doctor_argvs = (
            ["git", "-c", "gc.auto=0", "-c", "gc.autoDetach=false",
             "-c", "maintenance.auto=false", "status", "--porcelain=v1", "--untracked-files=all"],
            ["git", "-c", "gc.auto=0", "-c", "gc.autoDetach=false",
             "-c", "maintenance.auto=false", "rev-parse", "--verify", "HEAD^{commit}"],
        )
        self.shallow_argv = fixed_git_command(
            "/usr/bin/git", _entry.ROOT, ("rev-parse", "--is-shallow-repository"),
        )
        self.fixed_env = fixed_git_environment("/usr/bin/git")

    def doctor_kwargs(self):
        return {"cwd": _entry.ROOT, "env": dict(self.doctor_env)}

    def test_exact_production_shallow_argv_and_fixed_environment_are_accepted(self):
        # Delegate to the real production builders, including all isolation
        # options. A stale abbreviated argv must not define this contract.
        self.assertTrue(_entry.source_only_git(self.shallow_argv, {"env": self.fixed_env}))

    def test_exact_native_manifest_source_reads_are_accepted_with_fixed_environment(self):
        for executable in ("git", "/usr/bin/git"):
            for query in (("rev-parse", "--show-toplevel"), ("ls-files", "-z")):
                with self.subTest(executable=executable, query=query):
                    command = fixed_git_command(executable, _entry.ROOT, query)
                    self.assertTrue(_entry.source_only_git(command,
                        {"env": fixed_git_environment(executable)}))
                    wrong = fixed_git_command(executable, self.fixture, query)
                    self.assertFalse(_entry.source_only_git(wrong,
                        {"env": fixed_git_environment(executable)}))

    def test_both_exact_doctor_source_reads_with_private_home_are_accepted(self):
        for argv in self.doctor_argvs:
            with self.subTest(argv=argv):
                self.assertTrue(_entry.source_only_git(argv, self.doctor_kwargs()))

    def test_doctor_read_from_alternate_cwd_or_fixture_is_refused(self):
        for cwd in (self.fixture, _entry.ROOT / "docs", Path("/")):
            for argv in self.doctor_argvs:
                with self.subTest(cwd=cwd, argv=argv):
                    kwargs = self.doctor_kwargs()
                    kwargs["cwd"] = cwd
                    self.assertFalse(_entry.source_only_git(argv, kwargs))

    def test_shallow_probe_cannot_select_the_bus_fixture_as_repository(self):
        argv = fixed_git_command(
            "/usr/bin/git", self.fixture, ("rev-parse", "--is-shallow-repository"),
        )
        self.assertFalse(_entry.source_only_git(argv, {"env": self.fixed_env}))

    def test_extra_flags_and_different_source_queries_are_refused(self):
        for argv in (
            [*self.doctor_argvs[0], "--ignored"],
            [*self.doctor_argvs[1], "--quiet"],
            ["git", "show", "HEAD:events.jsonl"],
            [*self.shallow_argv, "--git-dir", str(self.fixture)],
        ):
            with self.subTest(argv=argv):
                self.assertFalse(_entry.source_only_git(argv, self.doctor_kwargs()))

    def test_doctor_maintenance_prefix_is_exact_and_cannot_admit_other_commands(self):
        prefix = self.doctor_argvs[0][:7]
        for argv in (
            ["git", *self.doctor_argvs[0][7:]],
            [*prefix[:2], "gc.auto=1", *prefix[3:], *self.doctor_argvs[0][7:]],
            ["git", "-c", "gc.autoDetach=false", "-c", "gc.auto=0",
             "-c", "maintenance.auto=false", *self.doctor_argvs[0][7:]],
            [*prefix, "status", "--untracked-files=all", "--porcelain=v1"],
            [*prefix, "reset", "--hard", "HEAD"],
            [*prefix, "maintenance", "run"],
            [*self.doctor_argvs[0], "--ignored"],
            [*self.doctor_argvs[1], "--quiet"],
        ):
            with self.subTest(argv=argv):
                self.assertFalse(_entry.source_only_git(argv, self.doctor_kwargs()))

    def test_shell_mode_and_executable_override_cannot_change_the_allowed_program(self):
        for option in ({"shell": True}, {"executable": "/bin/sh"}):
            with self.subTest(option=option):
                kwargs = self.doctor_kwargs()
                kwargs.update(option)
                self.assertFalse(_entry.source_only_git(self.doctor_argvs[0], kwargs))

    def test_non_git_and_shell_strings_are_refused(self):
        for argv in (
            ["/bin/cat", str(self.fixture / "events.jsonl")],
            ["/bin/sh", "-c", "git status"],
            "git status --porcelain=v1 --untracked-files=all",
        ):
            with self.subTest(argv=argv):
                self.assertFalse(_entry.source_only_git(argv, self.doctor_kwargs()))

    def test_each_fixed_environment_value_and_key_set_is_bound_exactly(self):
        for key in self.fixed_env:
            with self.subTest(key=key):
                env = dict(self.fixed_env)
                env[key] += "-changed"
                self.assertFalse(_entry.source_only_git(self.shallow_argv, {"env": env}))
        for operation in ("missing", "extra"):
            with self.subTest(operation=operation):
                env = dict(self.fixed_env)
                if operation == "missing":
                    del env["LC_ALL"]
                else:
                    env["UNREVIEWED_VARIABLE"] = "1"
                self.assertFalse(_entry.source_only_git(self.shallow_argv, {"env": env}))

    def test_doctor_git_redirectors_and_extra_git_controls_are_refused(self):
        for key, value in (
            ("GIT_DIR", str(self.fixture)), ("GIT_WORK_TREE", str(self.fixture)),
            ("GIT_COMMON_DIR", str(self.fixture)), ("GIT_INDEX_FILE", str(self.fixture / "index")),
            ("GIT_CONFIG_COUNT", "1"), ("GIT_CONFIG_NOSYSTEM", "1"),
            ("GIT_TERMINAL_PROMPT", "0"), ("GIT_ATTR_NOSYSTEM", "0"),
        ):
            with self.subTest(key=key):
                kwargs = self.doctor_kwargs()
                kwargs["env"][key] = value
                self.assertFalse(_entry.source_only_git(self.doctor_argvs[0], kwargs))

    def test_doctor_loader_redirectors_and_nonfixed_path_are_refused(self):
        for key, value in (
            ("PATH", str(self.fixture)), ("LD_PRELOAD", str(self.fixture / "library.so")),
            ("DYLD_INSERT_LIBRARIES", str(self.fixture / "library.dylib")),
        ):
            with self.subTest(key=key):
                kwargs = self.doctor_kwargs()
                kwargs["env"][key] = value
                self.assertFalse(_entry.source_only_git(self.doctor_argvs[0], kwargs))

    def test_doctor_requires_private_home_and_exact_attribute_control(self):
        for key, value in (("HOME", "/"), ("GIT_ATTR_NOSYSTEM", None)):
            with self.subTest(key=key):
                kwargs = self.doctor_kwargs()
                if value is None:
                    del kwargs["env"][key]
                else:
                    kwargs["env"][key] = value
                self.assertFalse(_entry.source_only_git(self.doctor_argvs[0], kwargs))

    def _mock_constructor(self, launches):
        def construct(process, argv, **kwargs):
            launches.append((list(argv), copy.deepcopy(kwargs)))
            process.pid = 12345
            process.returncode = 0
            process._child_created = False
        return construct

    def test_io_probe_records_and_collects_an_exact_mocked_native_git_probe(self):
        launches = []
        with mock.patch.object(subprocess.Popen, "__init__", self._mock_constructor(launches)),\
                mock.patch.object(subprocess.Popen, "poll", return_value=0):
            with IOProbe(self.fixture, external_process_policy=_entry.source_only_git) as probe:
                subprocess.Popen(self.shallow_argv, env=dict(self.fixed_env))
            probe.assert_complete()
        self.assertEqual(1, len(launches))
        self.assertEqual(self.shallow_argv, launches[0][0])
        self.assertEqual(1, len(probe.external_processes))
        self.assertEqual("launched", probe.external_processes[0]["status"])
        self.assertEqual(0, probe.external_processes[0]["returncode"])
        self.assertEqual({}, dict(probe.read_bytes_by_path))

    def test_io_probe_collects_both_exact_doctor_queries_without_ledger_io(self):
        for argv in self.doctor_argvs:
            with self.subTest(argv=argv):
                launches = []
                with mock.patch.object(subprocess.Popen, "__init__", self._mock_constructor(launches)),\
                        mock.patch.object(subprocess.Popen, "poll", return_value=0):
                    with IOProbe(self.fixture, external_process_policy=_entry.source_only_git) as probe:
                        subprocess.Popen(argv, **self.doctor_kwargs())
                    probe.assert_complete()
                self.assertEqual([argv], [row[0] for row in launches])
                self.assertEqual([0], [row["returncode"] for row in probe.external_processes])
                self.assertEqual({}, dict(probe.read_bytes_by_path))

    def test_io_probe_policy_denial_precedes_even_the_mock_child_constructor(self):
        launches = []
        kwargs = self.doctor_kwargs()
        kwargs["env"]["GIT_ATTR_NOSYSTEM"] = "0"
        with mock.patch.object(subprocess.Popen, "__init__", self._mock_constructor(launches)),\
                mock.patch.object(subprocess.Popen, "poll", return_value=0):
            with IOProbe(self.fixture, external_process_policy=_entry.source_only_git) as probe:
                with self.assertRaisesRegex(RuntimeError, "policy denied"):
                    subprocess.Popen(self.doctor_argvs[0], **kwargs)
            with self.assertRaisesRegex(RuntimeError, "unsupported I/O coverage"):
                probe.assert_complete()
        self.assertEqual([], launches)
        self.assertEqual([], probe.external_processes)

    def test_send_git_probe_is_bound_to_the_owned_git_workspace_and_private_env(self):
        parent = self.scratch / "bus-scale-census-owned-control"
        parent.mkdir(mode=0o700)
        workspace = parent / "workspace"
        from tests.bus_scale_test_profile import banked_send_repository
        repository, _sha = banked_send_repository(parent)
        repository.rename(workspace)
        workspace.chmod(0o700)
        command = fixed_git_command("/usr/bin/git", workspace, ("rev-parse", "--show-toplevel"))
        env = {"HOME": str(self.home), "PATH": "/usr/bin:/bin"}
        self.assertTrue(_entry.scratch_git_probe(command, {"env": env}, workspace))
        variants = (
            {"env": dict(env, GIT_DIR=str(self.fixture))},
            {"env": dict(env, DYLD_INSERT_LIBRARIES="\x2ftmp/library")},
            {"env": dict(env, HOME="/")},
            {"env": env, "executable": "/bin/sh"},
            {"env": env, "cwd": self.fixture},
        )
        for kwargs in variants:
            with self.subTest(kwargs=kwargs):
                self.assertFalse(_entry.scratch_git_probe(command, kwargs, workspace))
        for args in ([*command, "--git-dir", str(self.fixture)],
                     fixed_git_command("/usr/bin/git", self.fixture, ("rev-parse", "--show-toplevel"))):
            self.assertFalse(_entry.scratch_git_probe(args, {"env": env}, workspace))


class BusScaleNativeEntryHoldTests(unittest.TestCase):
    def summarize(self, rows, **kwargs):
        return _entry.hold_summary(SimpleNamespace(holds=rows), **kwargs)

    def test_all_jsonl_ledger_paths_share_one_family_with_exact_maximum(self):
        paths = (
            "events.jsonl.lock", "nodes/registry.jsonl.lock",
            "receipts/codex-wait-exhaustion/scale-worker.jsonl.lock",
            "receipts/deliveries/architect.jsonl.lock",
            "receipts/wake-waiter-exit/scale-worker.jsonl.lock",
        )
        rows = [_hold(path, 0, index) for index, path in enumerate(paths, 1)]
        result = self.summarize(rows)
        self.assertEqual(len(paths), result["ledger"]["count"])
        self.assertEqual(set(paths), {r["path"] for r in result["ledger"]["intervals"]})
        self.assertEqual(5, result["ledger"]["maximum_seconds"])
        self.assertEqual(0, result["lifetime"]["count"])

    def test_native_epoch_directory_guard_is_separate_from_ledgers(self):
        result = self.summarize([_hold(".", 0, 3)])
        self.assertEqual(1, result["epoch"]["count"])
        self.assertEqual(3, result["epoch"]["maximum_seconds"])
        self.assertEqual(0, result["ledger"]["count"])
        self.assertEqual(0, result["other"]["count"])

    def test_native_wake_coordination_and_sequencer_owner_are_separate(self):
        result = self.summarize([
            _hold("receipts/wake-coordination/scale-worker/lane.lock", 0, 2),
            _hold("sequencer/owner.lock", 0, 4, mode="exclusive"),
        ])
        self.assertEqual(1, result["coordination"]["count"])
        self.assertEqual(2, result["coordination"]["maximum_seconds"])
        self.assertEqual(1, result["lifetime"]["count"])
        self.assertEqual(4, result["lifetime"]["maximum_seconds"])
        self.assertEqual(0, result["ledger"]["count"])

    def test_unknown_lock_names_do_not_inherit_epoch_coordination_or_lifetime(self):
        paths = (
            "foreign/epoch-audit.lock", "foreign/waiter-notes.lock",
            "foreign/sequencer-cache.lock", "foreign/wake-coordination.lock",
            "state/wake-control/scale-worker.lock", "work/car-acceptance.lock",
        )
        result = self.summarize([_hold(path, 0, 1) for path in paths])
        self.assertEqual(len(paths), result["other"]["count"])
        self.assertEqual(set(paths), {row["path"] for row in result["other"]["intervals"]})
        for family in ("ledger", "epoch", "coordination", "lifetime"):
            self.assertEqual(0, result[family]["count"])

    def test_idle_summary_excludes_nonoverlapping_and_touching_intervals(self):
        rows = [
            _hold("events.jsonl.lock", 0, 9),
            _hold("events.jsonl.lock", 9, 10),
            _hold("events.jsonl.lock", 20, 21),
            _hold("events.jsonl.lock", 21, 30),
        ]
        result = self.summarize(rows, begin=10_000_000_000, end=20_000_000_000)
        self.assertEqual(0, result["ledger"]["count"])
        self.assertEqual([], result["ledger"]["intervals"])
        self.assertEqual(0, result["ledger"]["maximum_seconds"])

    def test_idle_summary_clips_real_overlap_and_uses_maximum_not_sum(self):
        rows = [
            _hold("events.jsonl.lock", 8, 12),
            _hold("events.jsonl.lock", 14, 17),
            _hold("events.jsonl.lock", 19, 23),
            _hold("foreign/notes.lock", 7, 11),
        ]
        original = copy.deepcopy(rows)
        result = self.summarize(rows, begin=10_000_000_000, end=20_000_000_000)
        self.assertEqual(3, result["ledger"]["count"])
        self.assertEqual([2, 3, 1], [row["seconds"] for row in result["ledger"]["intervals"]])
        self.assertEqual(3, result["ledger"]["maximum_seconds"])
        self.assertEqual(1, result["other"]["maximum_seconds"])
        self.assertEqual(original, rows)

    def test_idle_summary_clips_enclosing_lifetime_hold_without_calling_it_ledger(self):
        result = self.summarize(
            [_hold("sequencer/owner.lock", 0, 30, mode="exclusive")],
            begin=10_000_000_000, end=20_000_000_000,
        )
        self.assertEqual(1, result["lifetime"]["count"])
        self.assertEqual(10, result["lifetime"]["maximum_seconds"])
        self.assertEqual(0, result["ledger"]["maximum_seconds"])

    def test_empty_probe_retains_explicit_zero_count_for_every_family(self):
        result = self.summarize([])
        self.assertEqual({"ledger", "epoch", "coordination", "lifetime", "other"}, set(result))
        for row in result.values():
            self.assertEqual({"count": 0, "maximum_seconds": 0, "intervals": []}, row)


class BusScaleNativeIdlePhaseTests(unittest.TestCase):
    def test_final_shortened_poll_cannot_count_as_equal_idle_work(self):
        idle, changes = {}, []
        with mock.patch.object(_entry.time, "monotonic_ns", return_value=1_000_000_000):
            _entry.observe_idle(lambda actual: (False, False), object(), changes, idle, None,
                reconciliation={"state": "caught_up", "wake_required": False, "fresh_messages": []},
                authorized=True, deadline_ns=1_250_000_000, poll_interval_seconds=1.0)
        self.assertNotIn("begin_ns", idle)

    def test_clean_hints_before_native_reconciliation_do_not_start_idle(self):
        idle, changes = {}, []
        for _ in range(3):
            _entry.observe_idle(lambda watcher: (False, False), object(), changes, idle, None)
        self.assertEqual({}, idle)

    def test_idle_requires_authorized_caught_up_reconciliation_and_a_later_clean_tick(self):
        idle, changes = {}, []
        watcher = object()
        for artifact in (None, {"state": "fresh_work", "wake_required": True},
                         {"state": "caught_up", "wake_required": False, "fresh_messages": [], "held_total": 0}):
            _entry.observe_idle(lambda actual: (False, False), watcher, changes, idle, None,
                                reconciliation=artifact, authorized=artifact is not None, deadline_ns=10**30)
        self.assertIn("begin_ns", idle)
        self.assertNotIn("end_ns", idle)
        _entry.observe_idle(lambda actual: (False, False), watcher, changes, idle, None,
            reconciliation={"state": "caught_up", "wake_required": False, "fresh_messages": [], "held_total": 0},
            authorized=True, deadline_ns=10**30, previous_current_time_ns=0)
        self.assertGreater(idle["end_ns"], idle["begin_ns"])

    def test_dirty_tick_discards_the_earlier_clean_boundary(self):
        changes, idle = [], {}
        values = iter(((False, False), (True, False), (False, False), (False, False)))
        watcher = object()
        seen = []

        def native_changed(actual):
            self.assertIs(watcher, actual)
            value = next(values)
            seen.append(value)
            return value

        with mock.patch.object(_entry.time, "monotonic_ns", side_effect=(100, 300, 400)),\
                mock.patch.object(_entry.time, "process_time_ns", side_effect=(10, 30, 40)):
            for _ in range(4):
                _entry.observe_idle(native_changed, watcher, changes, idle, None,
                    reconciliation={"state": "caught_up", "wake_required": False,
                                    "fresh_messages": [], "held_total": 0}, authorized=True,
                    deadline_ns=10**30, previous_current_time_ns=0)
        self.assertEqual([(False, False), (True, False), (False, False), (False, False)], seen)
        self.assertEqual(300, idle["begin_ns"])
        self.assertEqual(400, idle["end_ns"])
        self.assertEqual(30, idle["cpu_begin_ns"])
        self.assertEqual(40, idle["cpu_end_ns"])


if __name__ == "__main__":
    unittest.main()

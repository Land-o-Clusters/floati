"""Native external calls must be bound to the actual private case inputs."""
from contextlib import redirect_stdout
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

from floati import cli
from tests.harbor_bus_scale_routine_lifecycle import fixture, load, ENTRY, HELPER


class RoutineExternalPolicyTests(unittest.TestCase):
    def test_native_node_prompts_source_head_probe_is_exact_and_case_bound(self):
        from floati.lifecycle_prompts import harbor_sha
        entry = load(ENTRY, "routine_entry_prompt_source_policy")
        calls = []
        native = subprocess.Popen.__init__
        def record(process, argv, *args, **kwargs):
            calls.append((list(argv), dict(kwargs)))
            return native(process, argv, *args, **kwargs)
        environment = {"HOME": str(Path.home()), "PATH": "/usr/bin:/bin",
                       "PYTHONNOUSERSITE": "1", "PYTHONDONTWRITEBYTECODE": "1"}
        with patch.dict(os.environ, environment, clear=True), patch.object(subprocess.Popen, "__init__", record):
            self.assertRegex(harbor_sha(), r"^[0-9a-f]{40}$")
            self.assertEqual(1, len(calls))
            argv, kwargs = calls[0]
            self.assertTrue(entry.node_prompt_source_git("node-prompts", argv, kwargs))
            # Derive mutations from the actual native vector, including its
            # maintenance controls, rather than copying a producer literal.
            for index in range(len(argv)):
                self.assertFalse(entry.node_prompt_source_git(
                    "node-prompts", argv[:index] + argv[index + 1:], kwargs))
            self.assertFalse(entry.node_prompt_source_git("node-boot", argv, kwargs))
            self.assertFalse(entry.node_prompt_source_git("node-prompts", argv + ["--all"], kwargs))
            for changed in (dict(kwargs, cwd="\x2fprivate/tmp"), dict(kwargs, shell=True),
                            dict(kwargs, stdout=1), dict(kwargs, executable="/bin/sh"),
                            dict(kwargs, env=dict(environment, GIT_DIR="\x2fprivate/tmp"))):
                self.assertFalse(entry.node_prompt_source_git("node-prompts", argv, changed))

    def test_every_expanded_parser_case_has_exactly_one_native_adapter(self):
        from floati.cli import _parser
        from tests.bus_scale_workloads import scope_contract
        entry = load(ENTRY, "routine_entry_closed_population")
        original = {"send", "status", "doctor", "log-history", "receipts-history",
                    "readiness-read", "receipt-append", "wait-start", "wait-idle"}
        cases = set(scope_contract(_parser())["cases"]) - original
        self.assertEqual(61, len(cases))
        for case in sorted(cases):
            with self.subTest(case=case):
                helper = entry.routine_module(case)
                self.assertIn(case, helper.CASES)

    def test_native_overlap_and_lane_calls_are_admitted_only_for_their_case(self):
        entry = load(ENTRY, "routine_entry_policy_native")
        policy = getattr(entry, "routine_subprocess", None)
        self.assertTrue(callable(policy), "missing case-bound diagnostic subprocess contract")
        source = HELPER.parent / "bus-scale-routine-reads-2026-10-15.py"
        for case in ("overlap-report", "lane-open", "lane-close", "node-prep-clear"):
            with self.subTest(case=case), fixture(case) as (bank, home, workspace):
                helper = load(source if case == "overlap-report" else HELPER,
                              "routine_entry_policy_" + case.replace("-", "_"))
                context = helper.prepare(case, home, workspace, bank)
                calls = []
                native = subprocess.Popen.__init__
                def record(process, argv, *args, **kwargs):
                    calls.append((list(argv), dict(kwargs)))
                    return native(process, argv, *args, **kwargs)
                native_environment = {"HOME": str(Path.home()), "PATH": "/usr/bin:/bin",
                                      "PYTHONNOUSERSITE": "1", "PYTHONDONTWRITEBYTECODE": "1"}
                with patch.dict(os.environ, native_environment, clear=True),\
                        patch.object(subprocess.Popen, "__init__", record), redirect_stdout(io.StringIO()):
                    code = cli.main(helper.arguments(case, home, workspace, context))
                self.assertEqual(0, code)
                self.assertTrue(calls, "control must exercise native external work")
                for argv, kwargs in calls:
                    # prep-clear also invokes the existing native send SHA
                    # proofs with inherited entry environment. Keep their old
                    # closed contract, rather than widening the new one.
                    if "env" not in kwargs:
                        with patch.dict(os.environ, native_environment, clear=True):
                            self.assertTrue(entry.scratch_git_probe(argv, kwargs, workspace), argv)
                            self.assertFalse(entry.scratch_git_probe(argv + ["--unreviewed"], kwargs, workspace))
                            self.assertFalse(entry.scratch_git_probe(argv, dict(kwargs, shell=True), workspace))
                        continue
                    self.assertTrue(policy(case, argv, kwargs, home, workspace, context), (argv, kwargs, context))
                    self.assertFalse(policy("presence-show", argv, kwargs, home, workspace, context))
                    self.assertFalse(policy(case, argv + ["--unreviewed"], kwargs,
                                            home, workspace, context))
                    self.assertFalse(policy(case, argv, dict(kwargs, shell=True),
                                            home, workspace, context))
                    self.assertFalse(policy(case, argv, dict(kwargs, executable="/bin/sh"),
                                            home, workspace, context))
                    environment = dict(kwargs.get("env", {}), GIT_DIR=str(home))
                    self.assertFalse(policy(case, argv, dict(kwargs, env=environment),
                                            home, workspace, context))

    def test_unrelated_process_queries_and_foreign_workspace_are_refused(self):
        entry = load(ENTRY, "routine_entry_policy_negative")
        policy = getattr(entry, "routine_subprocess", None)
        self.assertTrue(callable(policy), "missing case-bound diagnostic subprocess contract")
        with fixture("lane-open") as (bank, home, workspace):
            helper = load(HELPER, "routine_entry_policy_negative_helper")
            context = helper.prepare("lane-open", home, workspace, bank)
            for argv in (["/bin/sh", "-c", "true"], ["/bin/cat", str(home / "events.jsonl")],
                         ["/usr/bin/git", "-C", str(home), "status"], ["/bin/ps", "aux"]):
                self.assertFalse(policy("lane-open", argv, {}, home, workspace, context))

    def test_exact_native_doctor_process_census_is_the_only_host_query(self):
        entry = load(ENTRY, "routine_entry_doctor_policy")
        with fixture("doctor") as (bank, home, workspace):
            argv = ["/bin/ps", "-eo", "pid=,ppid=,command="]
            kwargs = {"stdout": subprocess.PIPE, "stderr": subprocess.PIPE, "text": True}
            self.assertTrue(entry.routine_subprocess("doctor", argv, kwargs, home, workspace, {}))
            self.assertFalse(entry.routine_subprocess("sent", argv, kwargs, home, workspace, {}))
            self.assertFalse(entry.routine_subprocess("doctor", argv + ["-e"], kwargs, home, workspace, {}))
            self.assertFalse(entry.routine_subprocess("doctor", argv, dict(kwargs, env={}), home, workspace, {}))

    def test_prepared_cursor_stdin_drives_real_native_wake_work_and_is_restored(self):
        entry = load(ENTRY, "routine_entry_stdin_policy")
        supplied = getattr(entry, "prepared_stdin", None)
        self.assertTrue(callable(supplied), "native entry drops prepared Cursor stop input")
        with fixture("wake-wait") as (bank, home, workspace):
            helper = load(HELPER, "routine_entry_stdin_helper")
            context = helper.prepare("wake-wait", home, workspace, bank)
            before = helper.snapshot(home)
            original = sys.stdin
            output = io.StringIO()
            previous = Path.cwd()
            try:
                os.chdir(workspace)
                with supplied("wake-wait", context), redirect_stdout(output):
                    code = cli.main(helper.arguments("wake-wait", home, workspace, context))
            finally:
                os.chdir(previous)
            self.assertIs(sys.stdin, original)
            self.assertEqual(0, code)
            witness = helper.witness("wake-wait", home, before, json.loads(output.getvalue()), context, {})
            self.assertGreater(witness["effect"]["count"], 0)


if __name__ == "__main__":
    unittest.main()

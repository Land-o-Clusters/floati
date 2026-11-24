"""BL-10-F2-LINUX-GIT-1: retain native ownership on older host Git."""

from pathlib import Path
import json
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from tests import test_fl1_census_attribution as attribution


class OlderGitNativeAttributionTests(attribution.NativeAttributionTests):
    """Exercise every ownership case with the older option contract.

    The shim rejects only the unavailable option, then execs native Git.
    Successful cases therefore still observe a real maintenance process,
    its private hook, its marker and its ordinary release/reap path.
    """

    def setUp(self):
        super().setUp()
        self.bank = tempfile.TemporaryDirectory(prefix="fl1-older-git-")
        self.addCleanup(self.bank.cleanup)
        self.shim = Path(self.bank.name) / "git"
        self.shim.write_text(
            '#!/bin/sh\n'
            'for arg do\n'
            ' if [ "$arg" = "--no-detach" ]; then\n'
            '  printf "%s\\n" "git: unknown option no-detach" >&2\n'
            '  exit 129\n'
            ' fi\n'
            'done\n'
            'exec /usr/bin/git "$@"\n'
        )
        self.shim.chmod(0o700)
        self.native_popen = subprocess.Popen
        self.maintenance_calls = []

        def older_git(argv, *args, **kwargs):
            if list(argv[:3]) == ["/usr/bin/git", "maintenance", "run"]:
                self.maintenance_calls.append(list(argv))
                argv = [str(self.shim), *argv[1:]]
            return self.native_popen(argv, *args, **kwargs)

        shim_patch = patch.object(subprocess, "Popen", older_git)
        shim_patch.start()
        self.addCleanup(shim_patch.stop)

    def tearDown(self):
        self.assertTrue(self.maintenance_calls,
                        "ownership case must exercise the older Git contract")
        super().tearDown()


class OlderGitShapeControlTests(unittest.TestCase):
    def test_unavailable_option_really_exits_before_native_maintenance(self):
        case = OlderGitNativeAttributionTests(
            "test_matching_marker_native_maintenance_still_counts")
        case.setUp()
        try:
            result = subprocess.run(
                ["/usr/bin/git", "maintenance", "run", "--no-detach"],
                capture_output=True, text=True,
            )
            self.assertEqual(129, result.returncode)
            self.assertIn("unknown option no-detach", result.stderr)
            self.assertEqual(1, len(case.maintenance_calls))
        finally:
            case.doCleanups()


class ForegroundMaintenanceTests(unittest.TestCase):
    def test_native_maintenance_finishes_its_work_without_a_detached_continuation(self):
        """Native Trace2 completion survives a descendant missed by polling."""
        with tempfile.TemporaryDirectory(prefix="fl1-foreground-trace-") as temporary:
            trace = Path(temporary) / "trace.jsonl"
            native_popen = subprocess.Popen

            def traced(argv, *args, **kwargs):
                if list(argv[:3]) == ["/usr/bin/git", "maintenance", "run"]:
                    kwargs["env"] = dict(kwargs["env"], GIT_TRACE2_EVENT=str(trace))
                return native_popen(argv, *args, **kwargs)

            with patch.object(subprocess, "Popen", traced):
                with attribution.held_maintenance() as (repository, process):
                    self.assertIsNone(process.poll())
                    self.assertTrue(attribution.contains_pid(
                        attribution.gate._maintenance_children_under(repository), process.pid))
                    parent = attribution.gate.ProcessQuery().identity(process.pid)
                    hook_pid = int((repository.parent / "ready").read_text())
                    hook = attribution.gate.ProcessQuery().identity(hook_pid)
                    self.assertIsNotNone(hook, "the native pre-auto-gc hook must be live")
            self.assertEqual(0, process.returncode)
            self.assertFalse(repository.parent.exists(), "native collection must precede removal")
            raw = trace.read_bytes()
            self.assertTrue(raw.endswith(b"\n"))
            self.assertLess(len(raw), 8 * 1024 * 1024)
            rows = [json.loads(line) for line in raw.splitlines()]
            roots = [row for row in rows if row.get("event") == "start"
                     and "/" not in row.get("sid", "")]
            self.assertEqual(1, len(roots))
            self.assertEqual(["maintenance", "run"], roots[0]["argv"][1:3])
            self.assertTrue(roots[0]["sid"].endswith("-P%08x" % process.pid))
            names = {row.get("name") for row in rows if row.get("event") == "cmd_name"}
            self.assertTrue({"maintenance", "gc", "repack"} <= names,
                            "trace must contain actual native GC/repack work")
            forks = [row for row in rows if
                     row.get("event") == "exit" and row.get("file") == "setup.c"
                     or row.get("event") == "region_enter" and
                     row.get("category") == "maintenance" and row.get("label") == "detach"]
            print(json.dumps({"control": "foreground-maintenance", "pid": process.pid,
                              "native_trace": rows}, sort_keys=True))
            self.assertEqual([], forks, "native maintenance/GC must not daemonize")
            starts = {row["sid"] for row in rows if row.get("event") == "start"}
            for sid in starts:
                terminals = [row for row in rows if row.get("sid") == sid
                             and row.get("event") == "atexit"]
                self.assertEqual(1, len(terminals), "one native terminal per foreground Git")
                self.assertEqual(0, terminals[0]["code"])
            child_starts = sorted((row["sid"], row["child_id"]) for row in rows
                                  if row.get("event") == "child_start")
            child_exits = sorted((row["sid"], row["child_id"]) for row in rows
                                 if row.get("event") == "child_exit" and row.get("code") == 0)
            self.assertTrue(child_starts)
            self.assertEqual(child_starts, child_exits, "all native launched work must finish")
            for retained in (parent, hook):
                observed = attribution.gate.ProcessQuery().identity(retained["pid"])
                self.assertTrue(observed is None or observed["birth"] != retained["birth"],
                                "a witnessed native process survived fixture collection")

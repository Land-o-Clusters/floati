"""FQ-12 / BL-7: the pre-job hook fails on its own deadline.

The lock and load gates stay. A held shared hook lock plus
FLOATI_HOOK_MAX_WAIT=5 must exit non-zero with a typed
hook_wait_deadline line in about five seconds, instead of sleeping
until GitHub timeout-minutes: 40.

Am.1: the shared lock path is runner-host configuration. The shipped
hook reads FLOATI_HOOK_LOCK_PATH with a neutral default; this test
constructs its own lock path under its temp dir.

Am.3: an unreadable or unparsable load is a typed absence
(loadavg=unavailable), not a substituted 0. That reading is not quiet:
the hook waits or hits its deadline. Both-readers-broken must not
start the job. A readable load still gates as before.
"""

from __future__ import annotations

import fcntl
import os
import subprocess
import time
import unittest
from pathlib import Path

from tests.temp_roots import REAL_TEMP_ROOT


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
HOOK = REPOSITORY_ROOT / "scripts" / "hooks-job-started.sh"


class HookWaitDeadlineTests(unittest.TestCase):
    def setUp(self) -> None:
        import tempfile

        self.temporary = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.lock = self.base / "shared-hook.lock"
        self.ours = self.base / "floati-harbor-gate.lock"
        self.lock.write_text("", encoding="utf-8")
        self.ours.write_text("", encoding="utf-8")

    def _env(self, **extra: str) -> dict[str, str]:
        environ = os.environ.copy()
        environ["FLOATI_HOOK_LOCK_PATH"] = str(self.lock)
        environ["FLOATI_HARBOR_GATE_LOCK_PATH"] = str(self.ours)
        environ["FLOATI_HOOK_MAX_LOAD"] = "999"
        environ.update(extra)
        return environ

    def _bindir(self) -> Path:
        bindir = self.base / "bin"
        bindir.mkdir(exist_ok=True)
        return bindir

    def _write_sysctl(self, body: str) -> Path:
        path = self._bindir() / "sysctl"
        path.write_text("#!/bin/sh\n" + body, encoding="utf-8")
        path.chmod(0o700)
        return path

    def _with_path(self, environ: dict[str, str]) -> dict[str, str]:
        environ["PATH"] = str(self._bindir()) + os.pathsep + environ.get("PATH", "")
        return environ

    def test_held_lock_hits_typed_deadline_in_about_five_seconds(self) -> None:
        """RED on the unfixed hook: it sleeps 15 forever. After FQ-12 it
        names the deadline and exits in ~5s."""

        handle = open(self.lock, "a", encoding="utf-8")
        self.addCleanup(handle.close)
        fcntl.flock(handle, fcntl.LOCK_EX)
        began = time.monotonic()
        completed = subprocess.run(
            [str(HOOK)],
            cwd=REPOSITORY_ROOT,
            capture_output=True,
            text=True,
            env=self._env(FLOATI_HOOK_MAX_WAIT="5"),
            timeout=20,
        )
        elapsed = time.monotonic() - began
        combined = completed.stdout + completed.stderr
        self.assertNotEqual(0, completed.returncode, combined)
        self.assertRegex(
            combined,
            r"hook_wait_deadline lock_held=true loadavg=[0-9.]+",
            combined,
        )
        self.assertGreaterEqual(elapsed, 4.0)
        self.assertLess(elapsed, 8.0, combined)

    def test_free_lock_still_permits_start(self) -> None:
        completed = subprocess.run(
            [str(HOOK)],
            cwd=REPOSITORY_ROOT,
            capture_output=True,
            text=True,
            env=self._env(FLOATI_HOOK_MAX_WAIT="5"),
            timeout=10,
        )
        combined = completed.stdout + completed.stderr
        self.assertEqual(0, completed.returncode, combined)
        self.assertIn("load-gate=start-permitted", combined)
        self.assertNotIn("hook_wait_deadline", combined)

    def test_reads_one_minute_load_from_proc_file_when_present(self) -> None:
        proc = self.base / "proc-loadavg"
        proc.write_text("6.25 1.00 0.50 1/100 9\n", encoding="utf-8")
        self._write_sysctl('echo "sysctl-must-not-run" >&2; exit 1\n')
        completed = subprocess.run(
            [str(HOOK)],
            cwd=REPOSITORY_ROOT,
            capture_output=True,
            text=True,
            env=self._with_path(
                self._env(
                    FLOATI_HOOK_MAX_WAIT="5",
                    FLOATI_HOOK_PROC_LOADAVG=str(proc),
                )
            ),
            timeout=10,
        )
        combined = completed.stdout + completed.stderr
        self.assertEqual(0, completed.returncode, combined)
        self.assertIn("load1=6.25", combined)
        self.assertRegex(combined, r"loadavg=6\.25")
        self.assertNotIn("sysctl-must-not-run", completed.stderr)
        self.assertNotIn("sysctl-must-not-run", completed.stdout)

    def test_reads_one_minute_load_from_sysctl_when_proc_absent(self) -> None:
        missing = self.base / "no-such-proc-loadavg"
        self._write_sysctl('echo "{ 3.21 0.00 0.00 }"\n')
        completed = subprocess.run(
            [str(HOOK)],
            cwd=REPOSITORY_ROOT,
            capture_output=True,
            text=True,
            env=self._with_path(
                self._env(
                    FLOATI_HOOK_MAX_WAIT="5",
                    FLOATI_HOOK_PROC_LOADAVG=str(missing),
                )
            ),
            timeout=10,
        )
        combined = completed.stdout + completed.stderr
        self.assertEqual(0, completed.returncode, combined)
        self.assertIn("load1=3.21", combined)
        self.assertRegex(combined, r"loadavg=3\.21")

    def test_both_readers_broken_does_not_start_the_job(self) -> None:
        """RED on Am.2: empty load became 0 and start-permitted. Am.3:
        typed absence, wait to deadline, never start."""

        missing = self.base / "no-such-proc-loadavg"
        self._write_sysctl(
            'echo "sysctl: cannot stat /proc/sys/vm/loadavg" >&2; exit 1\n'
        )
        completed = subprocess.run(
            [str(HOOK)],
            cwd=REPOSITORY_ROOT,
            capture_output=True,
            text=True,
            env=self._with_path(
                self._env(
                    FLOATI_HOOK_MAX_WAIT="5",
                    FLOATI_HOOK_PROC_LOADAVG=str(missing),
                )
            ),
            timeout=20,
        )
        combined = completed.stdout + completed.stderr
        self.assertNotEqual(0, completed.returncode, combined)
        self.assertNotIn("load-gate=start-permitted", combined)
        self.assertRegex(
            combined,
            r"hook_wait_deadline lock_held=false loadavg=unavailable",
            combined,
        )
        self.assertNotIn("cannot stat", completed.stderr)
        self.assertNotIn("cannot stat", completed.stdout)
        self.assertNotRegex(combined, r"loadavg=0(?:[^0-9.]|$)")

    def test_held_lock_deadline_states_unavailable_when_sysctl_fails(self) -> None:
        handle = open(self.lock, "a", encoding="utf-8")
        self.addCleanup(handle.close)
        fcntl.flock(handle, fcntl.LOCK_EX)
        missing = self.base / "no-such-proc-loadavg"
        self._write_sysctl(
            'echo "sysctl: cannot stat /proc/sys/vm/loadavg" >&2; exit 1\n'
        )
        completed = subprocess.run(
            [str(HOOK)],
            cwd=REPOSITORY_ROOT,
            capture_output=True,
            text=True,
            env=self._with_path(
                self._env(
                    FLOATI_HOOK_MAX_WAIT="5",
                    FLOATI_HOOK_PROC_LOADAVG=str(missing),
                )
            ),
            timeout=20,
        )
        combined = completed.stdout + completed.stderr
        self.assertNotEqual(0, completed.returncode, combined)
        self.assertRegex(
            combined,
            r"hook_wait_deadline lock_held=true loadavg=unavailable",
            combined,
        )
        self.assertNotIn("cannot stat", completed.stderr)
        self.assertNotIn("cannot stat", completed.stdout)

    def test_lock_and_load_gates_are_not_loosened(self) -> None:
        source = HOOK.read_text(encoding="utf-8")
        self.assertIn("FLOATI_HOOK_MAX_LOAD:-8", source)
        self.assertIn("FLOATI_HOOK_LOCK_PATH:-/tmp/floati-hook.lock", source)
        self.assertIn("FLOATI_HOOK_MAX_WAIT:-1500", source)
        self.assertIn("FLOATI_HOOK_PROC_LOADAVG:-/proc/loadavg", source)


if __name__ == "__main__":
    unittest.main()

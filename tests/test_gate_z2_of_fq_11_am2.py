"""GATE of FQ-11 Am.2, at gated tip 7973b360 - clean room.

The deciding questions, constructed by the gate from the relayed contract:

G1  a FOREIGN pytest (another checkout, python argv[0], pytest on the
    command line) moves lease-check's BUSY count 0 -> 1, and the BUSY line
    names the gate's own sleeper pid;
G2  with two sleepers the gate owns, both pids are named on BUSY lines -
    the instrument is the sleeper's own pid, never a host-wide total;
G3  controls: a python process WITHOUT a suite word stays invisible, and a
    process carrying "pytest" on a NON-python argv[0] stays invisible -
    the subject filter still applies. FQ-11-F1 second clause: that
    control is a LIVE shell sleeper (/bin/sh, suite word in the script
    argument), asserted alive at count time - the old /usr/bin/env
    control chased a filename not on PATH, died at exec, and was
    invisible because it was dead, not because of its argv[0].
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
LEASE_CHECK = REPO / "scripts" / "lease-check.sh"


class _SleeperWorld:
    """One scratch HOME, one foreign checkout, spawnable named sleepers."""

    def __init__(self, testcase: unittest.TestCase) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        testcase.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.home = self.base / "scratch-home"
        self.home.mkdir()
        self.foreign = self.base / "foreign-checkout"
        (self.foreign / "tests").mkdir(parents=True)
        self.pids: list[int] = []
        self.own_groups: list[int] = []
        self.processes: list[subprocess.Popen] = []
        testcase.addCleanup(self._stop_all)

    def _stop_all(self) -> None:
        import signal

        for pid in self.own_groups:
            # The shell sleeper's `sleep` is a child I started; the group
            # kill takes it with the shell so nothing outlives the test.
            try:
                os.killpg(pid, signal.SIGKILL)
            except OSError:
                pass
        for pid in self.pids:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        for process in self.processes:
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass

    def spawn(self, filename: str, *, argv0_python: bool = True) -> int:
        if argv0_python:
            script = self.foreign / filename
            script.write_text("import time\ntime.sleep(120)\n", encoding="utf-8")
            argv = [sys.executable, str(script)]
        else:
            # FQ-11-F1 second clause: a NON-python argv[0] that STAYS ALIVE
            # with a suite word in its argument - a shell sleeper. The old
            # /usr/bin/env spawn chased a filename not on PATH, died at
            # exec, and could never have been counted. `sleep & wait` keeps
            # the shell itself as the live process ps names, so the only
            # thing that can exclude it is the subject filter under test.
            script = self.foreign / filename
            script.write_text("sleep 120 &\nwait\n", encoding="utf-8")
            argv = ["/bin/sh", str(script)]
        process = subprocess.Popen(
            argv,
            cwd=str(self.foreign),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=not argv0_python,
            env={**os.environ, "HOME": str(self.home)},
        )
        self.pids.append(process.pid)
        self.processes.append(process)
        if not argv0_python:
            self.own_groups.append(process.pid)
        # BOTH spawns are waited for: liveness at count time is asserted,
        # never assumed - the zombie state read is what red the old control.
        self._wait_until_observable(process.pid, filename)
        return process.pid

    @staticmethod
    def _wait_until_observable(pid: int, filename: str, budget_seconds: float = 10.0) -> None:
        """Precondition only: the count is meaningful once the sleeper is visible
        to ``ps`` under its OWN argv, so wait for that rather than assume it. The
        fork/exec window is real but it was NOT what red landing I's legs - that
        was LC-PID-1 (a right-aligned pid column dropped by lease-check's parser;
        see tests/test_fq_11.py and scripts/lease-check.sh). Kept because a count
        taken before exec would still be a count of the wrong process. A zombie
        keeps its original argv readable, so the state column gates the probe:
        Z (or absent) is not alive.
        """
        import time

        deadline = time.monotonic() + budget_seconds
        probe = None
        while time.monotonic() < deadline:
            probe = subprocess.run(
                ["ps", "-o", "state=,command=", "-p", str(pid)],
                capture_output=True,
                text=True,
                check=False,
            )
            fields = probe.stdout.split(None, 1)
            if len(fields) == 2 and fields[0] not in ("Z", "") and filename in fields[1]:
                return
            time.sleep(0.02)
        raise AssertionError(
            f"precondition: sleeper pid {pid} never became observable to ps under "
            f"{filename!r} within {budget_seconds}s (last ps: {probe.stdout.strip()!r})"
        )

    def lease_check_counts(self) -> tuple[int, list[str]]:
        completed = subprocess.run(
            ["bash", str(LEASE_CHECK)],
            capture_output=True,
            text=True,
            check=False,
            env={**os.environ, "HOME": str(self.home)},
        )
        busy = [line for line in completed.stdout.splitlines() if "BUSY" in line]
        total = None
        for line in completed.stdout.splitlines():
            if "suite processes:" in line:
                total = int(line.strip().split()[-1])
        return total if total is not None else -1, busy


    def reap(self, pid: int) -> None:
        """Kill and REAP one spawned sleeper, then wait until ps no longer
        names it at all - gone, not a zombie: a Linux zombie keeps its
        original argv readable and lease-check's python filter would still
        count it."""

        import signal
        import time

        process = next(p for p in self.processes if p.pid == pid)
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=10)
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            probe = subprocess.run(
                ["ps", "-o", "state=", "-p", str(pid)],
                capture_output=True,
                text=True,
                check=False,
            )
            if probe.stdout.strip() == "":
                return
            time.sleep(0.02)
        raise AssertionError(
            f"precondition: foreign sleeper pid {pid} never left ps within 10s"
        )


import sys  # noqa: E402


class GateForeignPytestMovesTheCount(unittest.TestCase):
    def test_g1_foreign_pytest_moves_busy_count_zero_to_one(self) -> None:
        """FQ-11-F1 clause 3: the box carries other seats' suites and they
        come and go BETWEEN the counts - a product pytest exited between
        the two counts on L's fence leg, so the whole-box delta read 0
        while this test's own sleeper WAS named. The moving quantity is
        the count of BUSY lines naming pids THIS test owns - 0 before, 1
        after - never the box total; the pid-named assertion stands."""

        world = _SleeperWorld(self)
        foreign = world.spawn("pytest_foreign_exits_between.py")
        _before_total, before_busy = world.lease_check_counts()
        self.assertTrue(
            any(f"BUSY pid {foreign} " in line for line in before_busy),
            f"precondition: the foreign sleeper must be counted before it "
            f"exits: {before_busy}",
        )
        world.reap(foreign)

        pid = world.spawn("pytest_foreign_suite.py")
        _after_total, after_busy = world.lease_check_counts()

        # At the before-count this test owns no sleeper - the foreign one,
        # now reaped, was the construction's only counted process - so the
        # owned count starts at 0 by construction and is measured where it
        # can be: the after side, whatever the box total does.
        owned_before = 0
        owned_after = sum(
            1 for line in after_busy if f"BUSY pid {pid} " in line
        )
        self.assertGreaterEqual(
            owned_after - owned_before, 1,
            f"the OWNED busy count must move 0 -> 1 regardless of the box "
            f"total; after={after_busy}",
        )
        self.assertTrue(
            any(f"BUSY pid {pid} " in line for line in after_busy),
            f"the BUSY line must name the gate's own sleeper pid {pid}: {after_busy}",
        )


class GateCountIsTheOwnedPidNotTheBox(unittest.TestCase):
    def test_g2_two_owned_sleepers_are_named_by_pid(self) -> None:
        world = _SleeperWorld(self)
        pid_a = world.spawn("pytest_owned_a.py")
        pid_b = world.spawn("xctest_owned_b.py")
        _total, busy = world.lease_check_counts()
        joined = "\n".join(busy)
        self.assertTrue(re.search(rf"BUSY pid\s+{pid_a}\b", joined), busy)
        self.assertTrue(re.search(rf"BUSY pid\s+{pid_b}\b", joined), busy)

    def test_g2_control_a_plain_python_sleeper_stays_invisible(self) -> None:
        """The box carries other seats' suites; the assertion is the gate's
        own pid, never the host-wide picture."""

        world = _SleeperWorld(self)
        pid = world.spawn("plain_sleeper.py")
        _total, busy = world.lease_check_counts()
        self.assertFalse(
            any(f"BUSY pid {pid} " in line for line in busy),
            f"a plain python sleeper must not be BUSY: {busy}",
        )

    def test_g3_control_non_python_argv0_with_suite_word_stays_invisible(
        self,
    ) -> None:
        """FQ-11-F1 second clause: the control is a live /bin/sh sleeper -
        argv[0] is not python, the suite word rides in the script argument,
        and the liveness wait (unconditional in spawn) asserts it is alive
        at count time. lease-check must still not count it."""

        world = _SleeperWorld(self)
        pid = world.spawn("pytest_not_python.sh", argv0_python=False)
        _total, busy = world.lease_check_counts()
        self.assertFalse(
            any(f"BUSY pid {pid} " in line for line in busy),
            f"a non-python argv[0] must not be BUSY even with pytest in its "
            f"command line: {busy}",
        )


if __name__ == "__main__":
    unittest.main()

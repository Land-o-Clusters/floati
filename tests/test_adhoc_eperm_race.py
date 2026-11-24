"""A held zombie makes the post-leader group-exit race deterministic."""

from __future__ import annotations

import errno
import itertools
import os
import select
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

from floati.adapters.headless_template import HarnessProfile, HeadlessProfileAdapter
from floati.worker_errors import WorkerAdapterFailure
from tests.readiness import Readiness, wait_readable


class AdhocEpermRaceTests(unittest.TestCase):
    def fixture(self):
        # Same session, separate group: the test owns waitpid for every member.
        # A new session would prevent our second child joining the target group.
        leader = subprocess.Popen(
            ["/usr/bin/python3", "-B", "-c", "import sys; sys.stdin.buffer.read()"],
            stdin=subprocess.PIPE, preexec_fn=os.setpgrp,
        )
        self.addCleanup(self.reap, leader)
        adapter = HeadlessProfileAdapter(HarnessProfile(
            name=HeadlessProfileAdapter.name, command=("/usr/bin/python3",),
            stderr_name="unused.stderr"))
        adapter._active_process = leader
        self.addCleanup(adapter.cancel)
        return adapter, leader

    @staticmethod
    def reap(process):
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None:
                stream.close()

    def member(self, group):
        process = subprocess.Popen(
            ["/usr/bin/python3", "-B", "-c",
             "import os,sys; os.write(1,b'ready\\n'); sys.stdin.buffer.read(1)"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            preexec_fn=lambda: os.setpgid(0, group),
        )
        self.addCleanup(self.reap, process)
        wait_readable(process.stdout, "owned group member first frame")
        self.assertEqual(b"ready\n", process.stdout.readline())
        return process

    def leave_zombie(self, process):
        # Observe kernel exit without waitpid: no sleep or scheduler race, and
        # no use of the production liveness reader to construct the expected state.
        if sys.platform == "darwin":
            with closing(select.kqueue()) as queue:
                queue.control([select.kevent(process.pid, filter=select.KQ_FILTER_PROC,
                    flags=select.KQ_EV_ADD, fflags=select.KQ_NOTE_EXIT)], 0)
                process.stdin.close()
                readiness = Readiness("owned group member kernel exit")
                events = queue.control(None, 1, readiness.remaining())
                if not events:
                    raise readiness.failure()
                self.assertEqual(1, len(events))
                self.assertTrue(events[0].fflags & select.KQ_NOTE_EXIT)
        else:
            process.stdin.close()
            observed = os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOWAIT)
            self.assertEqual(process.pid, observed.si_pid)

    def test_zombie_only_group_does_not_turn_successful_cancel_into_timeout(self):
        adapter, leader = self.fixture()
        zombie = self.member(leader.pid)
        self.leave_zombie(zombie)
        leader.stdin.close()
        leader.wait(timeout=5)
        # The parent deliberately withholds reaping until after cancel returns.
        if sys.platform == "darwin":
            with self.assertRaises(PermissionError) as caught:
                os.killpg(leader.pid, 0)
            self.assertEqual(errno.EPERM, caught.exception.errno)
        try:
            adapter.cancel()
        except WorkerAdapterFailure as exc:
            self.fail("an exited zombie-only group must be cleaned: " + exc.code)
        self.assertIsNone(adapter._active_process)
        zombie.wait(timeout=5)
        with self.assertRaises(ProcessLookupError):
            os.killpg(leader.pid, 0)

    def test_eperm_after_reap_can_be_proved_absent(self):
        adapter, leader = self.fixture()
        leader.stdin.close()
        leader.wait(timeout=5)
        real_killpg = os.killpg

        def denied_probe(group, signum):
            if group == leader.pid and signum == 0:
                raise PermissionError(errno.EPERM, "probe denied")
            return real_killpg(group, signum)

        with mock.patch("floati.adapters.headless_template.os.killpg", denied_probe):
            try:
                adapter.cancel()
            except WorkerAdapterFailure as exc:
                self.fail("a proved absent group must be cleaned: " + exc.code)
        self.assertIsNone(adapter._active_process)

    def test_eperm_with_live_member_keeps_group_owned(self):
        adapter, leader = self.fixture()
        member = self.member(leader.pid)
        leader.stdin.close()
        leader.wait(timeout=5)
        # Model a member we cannot signal (e.g. changed credentials). Keep the
        # actual member live; replacing only the signal syscall earns no verdict.
        real_killpg = os.killpg

        def denied_group(group, signum):
            if group == leader.pid:
                raise PermissionError(errno.EPERM, "group not signalable")
            return real_killpg(group, signum)

        with mock.patch("floati.adapters.headless_template.os.killpg", denied_group),\
                mock.patch("floati.adapters.headless_template.time.monotonic",
                    side_effect=itertools.count(step=1)):
            with self.assertRaises(WorkerAdapterFailure) as caught:
                adapter.cancel()
        self.assertEqual("process_timeout", caught.exception.code)
        self.assertIs(adapter._active_process, leader)
        self.assertIsNone(member.poll())

    def test_unreadable_group_evidence_keeps_ownership(self):
        adapter, leader = self.fixture()
        leader.stdin.close()
        leader.wait(timeout=5)
        real_killpg = os.killpg

        def denied_probe(group, signum):
            if group == leader.pid and signum == 0:
                raise PermissionError(errno.EPERM, "probe denied")
            return real_killpg(group, signum)

        with mock.patch("floati.adapters.headless_template.os.killpg", denied_probe),\
                mock.patch("ctypes.CDLL", side_effect=OSError("observation unavailable")),\
                mock.patch("floati.adapters.headless_template.Path", side_effect=OSError("procfs unavailable")),\
                mock.patch("floati.adapters.headless_template.time.monotonic",
                    side_effect=itertools.count(step=1)):
            with self.assertRaises(WorkerAdapterFailure) as caught:
                adapter.cancel()
        self.assertEqual("process_timeout", caught.exception.code)
        self.assertIs(adapter._active_process, leader)

    def test_linux_terminal_census_requires_visible_stable_zombies(self):
        # Exercise the Linux backend on every host using real proc-format files;
        # native Linux process/kernel qualification remains a separate claim.
        with tempfile.TemporaryDirectory() as temporary:
            proc = Path(temporary)
            (proc / "self").mkdir()
            status = proc / "self/status"
            mounts = proc / "self/mountinfo"
            member = proc / "123"
            member.mkdir()
            stat = member / "stat"

            def stat_row(state):
                return "123 (fixture ) name) " + " ".join(
                    [state, "1", "4242", *(["0"] * 16), "12345"]) + "\n"

            cases = (
                ("rw", "Z", "NStgid:\t321\n", True),
                ("rw,hidepid=2", "Z", "NStgid:\t321\n", False),
                ("rw", "S", "NStgid:\t321\n", False),
                ("rw", "Z", "NStgid:\t999 321\n", False),
                ("rw", "Z", "NStgid:\t999\n", False),
                ("rw", "Z", "Name:\tfixture\n", False),
                ("rw", "Z", "NStgid:\t321\nNStgid:\t321\n", False),
                ("rw", "Z", "NStgid:\tbroken\n", False),
            )
            for options, state, namespace, cleaned in cases:
                with self.subTest(options=options, state=state, namespace=namespace):
                    status.write_text(namespace)
                    mounts.write_text("10 1 0:1 / /proc rw - proc proc " + options + "\n")
                    stat.write_text(stat_row(state))
                    with mock.patch("floati.adapters.headless_template.sys.platform", "linux"),\
                            mock.patch("floati.adapters.headless_template.os.getpid", return_value=321),\
                            mock.patch("floati.adapters.headless_template.Path",
                                side_effect=lambda path: proc / Path(path).relative_to("/proc")),\
                            mock.patch("floati.adapters.headless_template.os.killpg",
                                side_effect=PermissionError(errno.EPERM, "probe denied")),\
                            mock.patch("floati.adapters.headless_template.time.monotonic",
                                side_effect=itertools.count(step=1)):
                        if cleaned:
                            HeadlessProfileAdapter._require_group_exit(4242)
                        else:
                            with self.assertRaises(WorkerAdapterFailure) as caught:
                                HeadlessProfileAdapter._require_group_exit(4242)
                            self.assertEqual("process_timeout", caught.exception.code)


if __name__ == "__main__":
    unittest.main()

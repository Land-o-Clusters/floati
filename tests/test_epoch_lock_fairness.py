"""EPOCH-EX-STARVATION-1: cross-process fairness of the tenant epoch barrier.

A new module: the epoch writer-exclusion and observation modules prove what the
barrier excludes, and none of them measures how long a contended exclusive
acquisition waits behind independent shared holders in other processes.
"""
from __future__ import annotations

from contextlib import redirect_stdout
import hashlib
import io
import multiprocessing
import os
from pathlib import Path
import tempfile
import time
import traceback
import unittest
from unittest import mock

from floati import bus_epoch
from floati.bus_epoch import epoch_guard
from floati.cli import main
from floati.doctor import project_root_layout_findings
from floati.errors import ProtocolRefusal
from floati.root import FloatiRoot
from tests.readiness import wait_event, wait_poll


_HOLDERS = 4
_WARM_HOLDS = 3
_WORK = b"\0" * 65536


def _shared_work(rounds: int) -> None:
    digest = b""
    for _ in range(rounds):
        digest = hashlib.sha256(_WORK + digest).digest()


def _calibrated_rounds() -> int:
    """Size one shared hold as a few milliseconds of this host's CPU work."""

    rounds, elapsed = 1, 0.0
    while elapsed < 0.002 and rounds < 1 << 16:
        rounds *= 2
        started = time.monotonic()
        _shared_work(rounds)
        elapsed = time.monotonic() - started
    return rounds


def _shared_stream(home, rounds, resume, stop, channel):
    """Hold SH back to back, each hold independent bounded work."""

    holds, refusals = [], []
    announced = resumed_from = None
    try:
        root = FloatiRoot.open_direct_home(Path(home))
        while True:
            if stop.is_set() and resumed_from is not None and any(
                    acquired >= resumed_from for _requested, acquired, _released in holds):
                break
            if resume.is_set() and resumed_from is None:
                resumed_from = time.monotonic()
            requested = time.monotonic()
            try:
                with epoch_guard(root):
                    acquired = time.monotonic()
                    _shared_work(rounds)
                    released = time.monotonic()
            except ProtocolRefusal as refusal:
                refusals.append(refusal.code)
                continue
            holds.append((requested, acquired, released))
            if announced is None and len(holds) >= _WARM_HOLDS:
                announced = True
                channel.send({"kind": "streaming", "pid": os.getpid()})
        channel.send({"kind": "done", "pid": os.getpid(), "holds": holds,
                      "refusals": refusals})
    except BaseException:
        channel.send({"kind": "error", "pid": os.getpid(), "trace": traceback.format_exc()})
        raise
    finally:
        channel.close()


def _hold_shared_until(home, held, release):
    root = FloatiRoot.open_direct_home(Path(home))
    with epoch_guard(root):
        held.set()
        release.wait(60)


def _host_load():
    try:
        return os.getloadavg()
    except (AttributeError, OSError):
        return "unavailable"


class EpochExclusiveFairnessTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.context = multiprocessing.get_context("fork")
        self.use_home(self.base / "fleet", initialized=True)

    def use_home(self, home, *, initialized):
        """Select a fresh tenant, made by ``floati init`` or without it."""

        self.home = home
        if initialized:
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(0, main(["init", "--root", str(home)]), output.getvalue())
        self.root = FloatiRoot.open_direct_home(home, create=not initialized)

    def contend_once(self):
        """One contended exclusive acquisition, which a tenant needs only once."""

        held, release = self.context.Event(), self.context.Event()
        holder = self.context.Process(target=_hold_shared_until,
                                      args=(str(self.home), held, release))
        holder.start()
        self.addCleanup(lambda: (release.set(), holder.join(10)))
        wait_event(held, "shared holder before the first contended exclusive")
        native_sleep = time.sleep

        def contended_sleep(seconds):
            # The first poll proves the exclusive attempt was contended.
            release.set()
            native_sleep(seconds)

        with mock.patch.object(bus_epoch.time, "sleep", contended_sleep):
            with epoch_guard(self.root, exclusive=True):
                pass
        holder.join(10)
        self.assertEqual(0, holder.exitcode)

    def start_stream(self, rounds):
        resume, stop = self.context.Event(), self.context.Event()
        holders = []
        for _ in range(_HOLDERS):
            incoming, outgoing = self.context.Pipe(duplex=False)
            process = self.context.Process(
                target=_shared_stream,
                args=(str(self.home), rounds, resume, stop, outgoing))
            process.start()
            outgoing.close()
            holders.append((process, incoming))

        def collect():
            resume.set()
            stop.set()
            for process, incoming in holders:
                process.join(10)
                if process.is_alive():
                    process.terminate()
                    process.join(5)
                incoming.close()
            self.assertFalse(any(process.is_alive() for process, _ in holders),
                             "a shared holder escaped cleanup")

        self.addCleanup(collect)
        for process, incoming in holders:
            wait_poll(incoming, "shared holder stream start")
            message = incoming.recv()
            self.assertEqual("streaming", message.get("kind"), message)
        return holders, resume, stop

    def finish_stream(self, holders, resume, stop):
        resume.set()
        stop.set()
        results = []
        for process, incoming in holders:
            wait_poll(incoming, "shared holder stream resumed after the exclusive hold")
            message = incoming.recv()
            self.assertEqual("done", message.get("kind"), message)
            process.join(10)
            self.assertEqual(0, process.exitcode)
            results.append(message)
        return results

    def assert_exclusive_wait_bounded_by_the_work_in_front(self):
        rounds = _calibrated_rounds()
        holders, resume, stop = self.start_stream(rounds)
        requested = time.monotonic()
        refusal = None
        try:
            with epoch_guard(self.root, exclusive=True):
                acquired = time.monotonic()
                _shared_work(rounds)
                released = time.monotonic()
        except ProtocolRefusal as caught:
            refusal = caught
            acquired = released = time.monotonic()
        results = self.finish_stream(holders, resume, stop)

        holds = [hold for result in results for hold in result["holds"]]
        longest = max(hold_released - hold_acquired
                      for _start, hold_acquired, hold_released in holds)
        in_front = [hold for hold in holds if hold[1] <= requested < hold[2]]
        arrived = [hold for hold in holds if requested < hold[1] < acquired]
        resumed = [hold for hold in holds if hold[1] >= released]
        # The work in front of the waiter is at most one hold per holder in
        # flight when it asked, plus one hold per holder already past the
        # admission point; each is bounded by the longest hold this host
        # measured. Polling adds a few product poll intervals.
        bound = 2 * longest + 20 * bus_epoch.EPOCH_LOCK_POLL_SECONDS
        waited = acquired - requested
        evidence = (
            f"waited={waited:.6f}s bound={bound:.6f}s own_work={released - acquired:.6f}s "
            f"longest_shared_hold={longest:.6f}s shared_holds={len(holds)} "
            f"in_front={len(in_front)} arrived_while_waiting={len(arrived)} "
            f"refusal={getattr(refusal, 'code', None)} holders={_HOLDERS} rounds={rounds} "
            f"host_load={_host_load()}")
        print("epoch_ex_fairness " + evidence)
        self.assertEqual([], [code for result in results for code in result["refusals"]],
                         "shared holders were refused: " + evidence)
        self.assertTrue(all(any(hold[1] >= released for hold in result["holds"])
                            for result in results), "a shared holder did not resume: " + evidence)
        self.assertTrue(resumed, evidence)
        self.assertIsNone(refusal, "the exclusive waiter was starved: " + evidence)
        self.assertLessEqual(waited, bound, "the exclusive waiter was starved: " + evidence)
        self.assertEqual([], [row for row in project_root_layout_findings(self.root)
                              if row["code"] == "root_unmanaged_bytes"],
                         "contended acquisition left unmanaged root bytes")

    def test_first_exclusive_contention_on_an_initialized_root_is_bounded(self):
        """A stream of independent SH holders cannot postpone a waiting EX.

        This is the tenant's first exclusive contention since ``floati init``.
        """

        self.assert_exclusive_wait_bounded_by_the_work_in_front()

    def test_root_made_without_init_is_bounded_after_its_first_contention(self):
        """A tenant that predates init's turnstile is fair from its second contention."""

        self.use_home(self.base / "legacy", initialized=False)
        self.contend_once()
        self.assert_exclusive_wait_bounded_by_the_work_in_front()

    def test_shared_holder_waiting_on_a_later_shared_acquirer_does_not_deadlock(self):
        """A dependency between two SH holders survives a waiting EX between them."""

        budget = 0.4
        context = self.context
        first_held, second_held = context.Event(), context.Event()
        waiter_polling = context.Event()
        outcomes = context.Queue()

        def first():
            with mock.patch.object(bus_epoch, "EPOCH_LOCK_TIMEOUT_SECONDS", budget):
                root = FloatiRoot.open_direct_home(self.home)
                with epoch_guard(root):
                    first_held.set()
                    # This holder's work depends on a later shared acquirer.
                    outcomes.put(("first_saw_second", second_held.wait(30)))

        def second():
            with mock.patch.object(bus_epoch, "EPOCH_LOCK_TIMEOUT_SECONDS", budget):
                root = FloatiRoot.open_direct_home(self.home)
                try:
                    with epoch_guard(root):
                        second_held.set()
                except ProtocolRefusal as refusal:
                    outcomes.put(("second_refused", refusal.code))
                else:
                    outcomes.put(("second_refused", None))

        def waiter():
            native_sleep = time.sleep

            def contended_sleep(seconds):
                # The first poll sleep proves the EX attempt is contended.
                waiter_polling.set()
                native_sleep(seconds)

            with mock.patch.object(bus_epoch, "EPOCH_LOCK_TIMEOUT_SECONDS", budget * 4),\
                    mock.patch.object(bus_epoch.time, "sleep", contended_sleep):
                root = FloatiRoot.open_direct_home(self.home)
                try:
                    with epoch_guard(root, exclusive=True):
                        pass
                except ProtocolRefusal as refusal:
                    outcomes.put(("waiter_refused", refusal.code))
                else:
                    outcomes.put(("waiter_refused", None))

        processes = []

        def collect():
            for process in processes:
                process.join(10)
                if process.is_alive():
                    process.terminate()
                    process.join(5)

        self.addCleanup(collect)
        for target, ready, description in (
                (first, first_held, "first shared holder"),
                (waiter, waiter_polling, "contended exclusive waiter"),
                (second, None, None)):
            processes.append(context.Process(target=target))
            processes[-1].start()
            if ready is not None:
                wait_event(ready, description)
        for process in processes:
            process.join(30)
            self.assertFalse(process.is_alive(), "a lock participant deadlocked")
            self.assertEqual(0, process.exitcode)
        seen = dict(outcomes.get(timeout=5) for _ in range(3))
        self.assertEqual({"first_saw_second": True, "second_refused": None,
                          "waiter_refused": None}, seen)

if __name__ == "__main__":
    unittest.main()

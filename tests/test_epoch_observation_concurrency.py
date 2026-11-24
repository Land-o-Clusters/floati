"""Native send contention must not turn a policy hint into false corruption."""
from __future__ import annotations

from tests.readiness import wait_event, wait_poll
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import json
import multiprocessing
import os
from pathlib import Path
import time
import traceback
import unittest
from unittest import mock

from floati import bus_epoch, epoch_rotation as rotation, jsonl
from floati.events import EventLog
from floati.errors import ProtocolRefusal
from floati.root import FloatiRoot
from tests import test_epoch_rotation as epoch_tests
from tests.test_process_atomicity import assert_send_companions


def _observe_in_owned_child(home, channel):
    """Keep a potentially blocking filesystem open out of the test runner."""
    timeline = []
    original_flock = fcntl.flock

    def traced_flock(fd, operation):
        info = os.fstat(fd)
        coordinate = {"device": info.st_dev, "inode": info.st_ino, "operation": operation}
        timeline.append({"kind": "attempt", **coordinate})
        result = original_flock(fd, operation)
        timeline.append({"kind": "returned", **coordinate})
        return result

    try:
        channel.send({"kind": "ready", "pid": os.getpid()})
        with mock.patch.object(fcntl, "flock", traced_flock):
            try:
                root = FloatiRoot.open_direct_home(Path(home), create=False)
                rotation.observe_policy(root, now=datetime.now(timezone.utc))
            except Exception as failure:
                result = {"kind": "refused", "code": getattr(failure, "code", None),
                          "type": type(failure).__name__}
            else:
                result = {"kind": "observed"}
        channel.send({**result, "pid": os.getpid(), "timeline": timeline})
    finally:
        channel.close()


def _sender_holding_native_events_lock(home, release, channel):
    """Pause inside the real writer lock, before the native append begins."""
    timeline = []
    original_lock = jsonl._locked_path
    original_flock = fcntl.flock
    held = False

    def traced_flock(fd, operation):
        info = os.fstat(fd)
        coordinate = {"device": info.st_dev, "inode": info.st_ino,
                      "operation": operation}
        timeline.append({"kind": "attempt", **coordinate})
        result = original_flock(fd, operation)
        timeline.append({"kind": "acquired_or_released", **coordinate})
        return result

    @contextmanager
    def held_lock(path, **kwargs):
        nonlocal held
        with original_lock(path, **kwargs):
            if path == Path(home) / "events.jsonl.lock" and kwargs["exclusive"] and not held:
                held = True
                info = path.stat()
                channel.send({"kind": "held", "pid": os.getpid(),
                              "device": info.st_dev, "inode": info.st_ino})
                if not wait_event(release, "test_epoch_observation_concurrency release"):
                    raise AssertionError("observer did not release the owned writer")
            yield

    try:
        root = FloatiRoot.open_direct_home(Path(home), create=False)
        with mock.patch.object(jsonl, "_locked_path", held_lock), mock.patch.object(
                fcntl, "flock", traced_flock):
            receipt = EventLog(root).send(
                "sender", "recipient", "floati", "a" * 40, "AGENTS.md",
                "fixture interfering", idempotency_key="interfering",
            )
        channel.send({"kind": "sent", "pid": os.getpid(), "receipt": receipt,
                      "timeline": timeline})
    except BaseException:
        channel.send({"kind": "error", "pid": os.getpid(), "trace": traceback.format_exc()})
        raise
    finally:
        channel.close()


def _contended_exclusive(home, polling, channel):
    """Wait for EX against a shared observer; announce the first poll."""
    native_sleep = time.sleep

    def contended_sleep(seconds):
        polling.set()
        native_sleep(seconds)

    try:
        with mock.patch.object(bus_epoch.time, "sleep", contended_sleep):
            root = FloatiRoot.open_direct_home(Path(home), create=False)
            try:
                with bus_epoch.epoch_guard(root, exclusive=True):
                    pass
            except ProtocolRefusal as refusal:
                channel.send({"kind": "refused", "code": refusal.code})
            else:
                channel.send({"kind": "acquired"})
    finally:
        channel.close()


def _shared_writer_creating_ledger_lock(home, held, observer_waiting, release, channel):
    """Hold SH like a native sender, and create the ledger lock while holding it."""
    try:
        root = FloatiRoot.open_direct_home(Path(home), create=False)
        with bus_epoch.epoch_guard(root):
            held.set()
            if not wait_event(observer_waiting, "pristine observer contended for EX"):
                raise AssertionError("observer never contended")
            with jsonl._locked_path(root.tenant_home / "events.jsonl.lock", exclusive=False,
                                    relative="events.jsonl.lock"):
                pass
            channel.send({"kind": "ledger_lock_created"})
            # Still inside this shared hold: the observer must not need it to end.
            channel.send({"kind": "released", "observer_finished": release.wait(30)})
    except BaseException:
        channel.send({"kind": "error", "trace": traceback.format_exc()})
        raise
    finally:
        channel.close()


class EpochObservationConcurrencyTests(unittest.TestCase):
    def owned_fixture(self):
        owner = epoch_tests.EpochRotationTests("runTest")
        owner.setUp()
        self.addCleanup(owner.doCleanups)
        owner.policy(size=10_000_000, days=3650)
        return owner

    def test_contended_exclusive_waiter_never_changes_a_shared_observation(self):
        """A waiting EX must not alter the tenant home an SH observation reads."""
        owner = self.owned_fixture()
        owner.send("seed")
        rotation._invalidate(owner.root)
        context = multiprocessing.get_context("fork")
        polling = context.Event()
        incoming, outgoing = context.Pipe(duplex=False)
        children = []

        def collect():
            for child in children:
                child.join(10)
                if child.is_alive():
                    child.terminate()
                    child.join(5)
            incoming.close()

        self.addCleanup(collect)
        original_start = rotation._observed_start

        def contended_start(root):
            if not children:
                # The observation holds SH here; let a real EX contend for it.
                children.append(context.Process(target=_contended_exclusive,
                                                args=(str(owner.root.path), polling, outgoing)))
                children[0].start()
                outgoing.close()
                wait_event(polling, "exclusive waiter contended during observation")
            return original_start(root)

        with mock.patch.object(rotation, "_observed_start", contended_start):
            result = rotation.observe_policy(owner.root, now=datetime.now(timezone.utc))
        self.assertFalse(result["due"])
        wait_poll(incoming, "exclusive waiter outcome")
        self.assertEqual({"kind": "acquired"}, incoming.recv())
        children[0].join(10)
        self.assertEqual(0, children[0].exitcode)

    def test_pristine_observer_shares_a_ledger_lock_created_while_it_waits(self):
        """A pristine hint waiting for EX joins the shared path once it can."""
        owner = self.owned_fixture()
        lock = owner.root.path / "events.jsonl.lock"
        self.assertFalse(lock.exists())
        context = multiprocessing.get_context("fork")
        held, waiting, release = context.Event(), context.Event(), context.Event()
        incoming, outgoing = context.Pipe(duplex=False)
        child = context.Process(target=_shared_writer_creating_ledger_lock,
                                args=(str(owner.root.path), held, waiting, release, outgoing))
        child.start()
        outgoing.close()

        def collect():
            release.set()
            waiting.set()
            child.join(10)
            if child.is_alive():
                child.terminate()
                child.join(5)
            incoming.close()

        self.addCleanup(collect)
        wait_event(held, "native shared writer")
        native_sleep = time.sleep

        def contended_sleep(seconds):
            waiting.set()
            native_sleep(seconds)

        failure = None
        with mock.patch.object(bus_epoch.time, "sleep", contended_sleep):
            try:
                result = rotation.observe_policy(owner.root, now=datetime.now(timezone.utc))
            except ProtocolRefusal as caught:
                failure = caught
        release.set()
        self.assertIsNone(failure, "the pristine observer waited out the shared writer")
        self.assertFalse(result["due"])
        wait_poll(incoming, "native shared writer report")
        self.assertEqual({"kind": "ledger_lock_created"}, incoming.recv())
        wait_poll(incoming, "native shared writer release")
        self.assertEqual({"kind": "released", "observer_finished": True}, incoming.recv())
        child.join(10)
        self.assertEqual(0, child.exitcode)

    def assert_invalid_sidecar_refuses_without_blocking(self, shape, code):
        owner = epoch_tests.EpochRotationTests("runTest")
        owner.setUp()
        processes = []

        def cleanup_fixture():
            if any(process.is_alive() for process in processes):
                owner.temporary._finalizer.detach()
                self.fail("uncollected observer; retained fixture " + str(owner.home))
            owner.doCleanups()

        self.addCleanup(cleanup_fixture)
        owner.policy(size=10_000_000, days=3650)
        owner.send("seed")
        lock = owner.root.path / "events.jsonl.lock"
        lock.unlink()
        target = owner.home / "sidecar-target"
        if shape == "fifo":
            os.mkfifo(lock)
        elif shape == "directory":
            lock.mkdir()
        elif shape == "symlink":
            target.write_bytes(b"unchanged sidecar target")
            lock.symlink_to(target)
        elif shape == "hardlink":
            target.write_bytes(b"unchanged sidecar target")
            os.link(target, lock)
        else:
            self.fail("unknown sidecar fixture")

        def snapshot():
            state = lock.lstat()
            return ((state.st_dev, state.st_ino, state.st_mode, state.st_nlink,
                     state.st_size, state.st_mtime_ns, state.st_ctime_ns),
                    (owner.root.path / "events.jsonl").read_bytes(),
                    (owner.root.path / "state/ledger-policy.json").read_bytes(),
                    target.read_bytes() if target.exists() else None)

        before = snapshot()
        context = multiprocessing.get_context("fork")
        incoming, outgoing = context.Pipe(duplex=False)
        child = context.Process(target=_observe_in_owned_child,
                                args=(str(owner.root.path), outgoing))
        child.start()
        processes.append(child)
        outgoing.close()

        def collect_child():
            child.join(2)
            if child.is_alive():
                child.terminate()
                child.join(2)
            self.assertFalse(child.is_alive(), "owned observer escaped cleanup")

        self.addCleanup(incoming.close)
        self.addCleanup(collect_child)
        self.assertTrue(wait_poll(incoming, "test_epoch_observation_concurrency incoming"), "owned observer did not start")
        self.assertEqual({"kind": "ready", "pid": child.pid}, incoming.recv())
        result = incoming.recv() if wait_poll(incoming, "test_epoch_observation_concurrency incoming") else None
        collect_child()
        self.assertEqual(before, snapshot())
        print("epoch_observation_sidecar=" + json.dumps({
            "shape": shape, "result": result, "child_pid": child.pid,
            "child_exit_code": child.exitcode, "child_collected": not child.is_alive(),
            "fixture_unchanged": True,
        }, sort_keys=True))
        self.assertIsNotNone(result, "observation blocked on an invalid " + shape + " sidecar")
        self.assertEqual(0, child.exitcode)
        self.assertEqual("refused", result["kind"], result)
        self.assertEqual(code, result["code"], result)

    def test_fifo_sidecar_refuses_without_blocking_the_observer(self):
        self.assert_invalid_sidecar_refuses_without_blocking("fifo", "epoch_rotation_path_invalid")

    def test_symlink_directory_and_hardlinked_sidecars_refuse_without_mutation(self):
        for shape, code in (("symlink", "storage_unavailable"),
                            ("directory", "epoch_rotation_path_invalid"),
                            ("hardlink", "epoch_rotation_path_invalid")):
            with self.subTest(shape=shape):
                self.assert_invalid_sidecar_refuses_without_blocking(shape, code)

    def test_pristine_observation_and_invalid_policy_leave_the_root_unchanged(self):
        for invalid in (False, True):
            with self.subTest(invalid_policy=invalid):
                owner = epoch_tests.EpochRotationTests("runTest")
                owner.setUp()
                self.addCleanup(owner.doCleanups)
                owner.policy(size=10_000_000, days=3650)
                if invalid:
                    (owner.root.path / "state/ledger-policy.json").write_text(
                        '{"schema_version":0,"max_bytes":false,"max_age_days":7}')
                lock = owner.root.path / "events.jsonl.lock"
                self.assertFalse(lock.exists())

                def snapshot():
                    return [(str(path.relative_to(owner.root.path)),
                             None if path.is_dir() else path.read_bytes())
                            for path in sorted(owner.root.path.rglob("*"))]

                before = snapshot()
                timeline = []
                original_flock = fcntl.flock

                def traced_flock(fd, operation):
                    result = original_flock(fd, operation)
                    info = os.fstat(fd)
                    root_stat = owner.root.path.stat()
                    self.assertEqual((root_stat.st_dev, root_stat.st_ino),
                                     (info.st_dev, info.st_ino))
                    timeline.append(operation)
                    return result

                with mock.patch.object(fcntl, "flock", traced_flock):
                    if invalid:
                        with self.assertRaises(ProtocolRefusal) as caught:
                            rotation.observe_policy(owner.root, now=datetime.now(timezone.utc))
                        self.assertEqual("ledger_policy_invalid", caught.exception.code)
                    else:
                        result = rotation.observe_policy(owner.root, now=datetime.now(timezone.utc))
                        self.assertEqual(0, result["observed_bytes"])
                        self.assertFalse(result["due"])
                self.assertEqual(before, snapshot())
                self.assertFalse(lock.exists())
                self.assertEqual([fcntl.LOCK_SH | fcntl.LOCK_NB, fcntl.LOCK_UN,
                                  fcntl.LOCK_EX | fcntl.LOCK_NB, fcntl.LOCK_UN], timeline)

    def test_unchanged_cached_observation_does_not_reread_policy_or_first_frame(self):
        owner = epoch_tests.EpochRotationTests("runTest")
        owner.setUp()
        self.addCleanup(owner.doCleanups)
        owner.policy(size=10_000_000, days=3650)
        owner.send("seed")
        moment = datetime.now(timezone.utc)
        expected = rotation.observe_policy(owner.root, now=moment)
        with mock.patch.object(rotation, "_observed_start",
                               side_effect=AssertionError("cached frame reread")), mock.patch(
                "floati.bus_epoch.ledger_roll_policy",
                side_effect=AssertionError("cached policy reread")):
            self.assertEqual(expected, rotation.observe_policy(owner.root, now=moment))

    def test_native_writer_contention_preserves_both_sends_without_false_integrity_failure(self):
        owner = epoch_tests.EpochRotationTests("runTest")
        owner.setUp()
        processes = []

        def cleanup_fixture():
            if any(process.is_alive() for process in processes):
                owner.temporary._finalizer.detach()
                self.fail("uncollected owned writer; retained fixture " + str(owner.home))
            owner.doCleanups()

        self.addCleanup(cleanup_fixture)
        owner.policy(size=10_000_000, days=3650)
        seed = owner.send("seed")
        context = multiprocessing.get_context("fork")
        release = context.Event()
        incoming, outgoing = context.Pipe(duplex=False)
        child = context.Process(target=_sender_holding_native_events_lock,
                                args=(str(owner.root.path), release, outgoing))
        child.start()
        processes.append(child)
        outgoing.close()

        def cleanup_child():
            release.set()
            child.join(5)
            if child.is_alive():
                child.terminate()
                child.join(5)
            incoming.close()
            self.assertFalse(child.is_alive(), "owned writer was not collected")

        self.addCleanup(cleanup_child)

        def receive(kind):
            self.assertTrue(wait_poll(incoming, "test_epoch_observation_concurrency incoming"), "owned writer produced no bounded response")
            result = incoming.recv()
            self.assertEqual(kind, result.get("kind"), result)
            self.assertEqual(child.pid, result.get("pid"), result)
            return result

        held = receive("held")
        coordinate = (held["device"], held["inode"])
        root_stat = owner.root.path.stat()
        root_coordinate = (root_stat.st_dev, root_stat.st_ino)
        timeline = []
        child_result = None

        def complete_writer(reason):
            nonlocal child_result
            self.assertIsNone(child_result, "writer must be released exactly once")
            timeline.append({"kind": "release_writer", "reason": reason})
            release.set()
            child_result = receive("sent")
            child.join(5)
            self.assertFalse(child.is_alive(), "native writer did not exit")
            self.assertEqual(0, child.exitcode)
            timeline.append({"kind": "writer_collected", "exit_code": child.exitcode})

        original_flock = fcntl.flock

        def lock_aware_flock(fd, operation):
            info = os.fstat(fd)
            entry = {"device": info.st_dev, "inode": info.st_ino, "operation": operation}
            timeline.append({"kind": "flock_attempt", **entry})
            try:
                result = original_flock(fd, operation)
            except BlockingIOError:
                timeline.append({"kind": "flock_blocked", **entry})
                if ((info.st_dev, info.st_ino) == coordinate
                        and operation == fcntl.LOCK_SH | fcntl.LOCK_NB
                        and child_result is None):
                    # The real kernel refused SH against the child's real EX.
                    # Let the writer finish, then preserve that exception for
                    # the native bounded poll/retry loop to handle unchanged.
                    complete_writer("native_shared_lock_contended")
                raise
            timeline.append({"kind": "flock_returned", **entry})
            return result

        original_start = rotation._observed_start

        def scheduled_start(root):
            timeline.append({"kind": "capture_enter"})
            started = original_start(root)
            timeline.append({"kind": "native_capture_returned"})
            if child_result is None:
                # Old code reads before taking any ledger lock. Preserve that
                # real read, then arrange a valid append before its final stat.
                complete_writer("unprotected_capture")
            return started

        rotation._invalidate(owner.root)
        observer = None
        failure = None
        with mock.patch.object(fcntl, "flock", lock_aware_flock), mock.patch.object(
                rotation, "_observed_start", scheduled_start):
            try:
                observer = owner.send("observer")
            except Exception as caught:
                failure = caught

        self.assertIsNotNone(child_result, "the intended native schedule was not reached")
        rows = EventLog(owner.root).event_records()
        envelopes = assert_send_companions(self, rows)
        expected = {seed["id"], child_result["receipt"]["id"]}
        if observer is not None:
            expected.add(observer["id"])
        self.assertEqual(expected, {row["id"] for row in envelopes})
        self.assertEqual(len(expected), len(envelopes))
        self.assertEqual([], owner.archives())
        print("epoch_observation_concurrency=" + json.dumps({
            "parent_timeline": timeline, "child_timeline": child_result["timeline"],
            "child_pid": child.pid, "child_exit_code": child.exitcode,
            "child_collected": not child.is_alive(), "events_lock": coordinate,
            "epoch_lock": root_coordinate,
            "strict_native_rows": len(rows), "native_companions_reconciled": True,
            "failure_code": getattr(failure, "code", None),
            "envelope_ids": sorted(expected),
        }, sort_keys=True))
        self.assertIsNone(failure, "concurrent native sends must both succeed: " + repr(failure))
        self.assertEqual({"seed", "observer", "interfering"},
                         {row["idempotency_key"] for row in envelopes})
        releases = [item for item in timeline if item["kind"] == "release_writer"]
        self.assertEqual(["native_shared_lock_contended"], [item["reason"] for item in releases])
        captures = [index for index, item in enumerate(timeline) if item["kind"] == "capture_enter"]
        shared = [index for index, item in enumerate(timeline)
                  if item["kind"] == "flock_returned"
                  and (item["device"], item["inode"]) == coordinate
                  and item["operation"] == fcntl.LOCK_SH | fcntl.LOCK_NB]
        self.assertEqual(1, len(captures), timeline)
        self.assertTrue(any(index < captures[0] for index in shared), timeline)
        first_shared = next(index for index in shared if index < captures[0])

        def completed_locks(identity, operation):
            return [index for index, item in enumerate(timeline)
                    if item["kind"] == "flock_returned"
                    and (item["device"], item["inode"]) == identity
                    and item["operation"] == operation]

        epoch_shared = completed_locks(root_coordinate, fcntl.LOCK_SH | fcntl.LOCK_NB)
        self.assertTrue(any(index < first_shared for index in epoch_shared), timeline)
        ledger_releases = completed_locks(coordinate, fcntl.LOCK_UN)
        epoch_releases = completed_locks(root_coordinate, fcntl.LOCK_UN)
        ledger_exclusive = completed_locks(coordinate, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.assertTrue(ledger_exclusive, timeline)
        # Observation must release ledger then epoch before the send obtains
        # native EX; retaining either would change the writer's lock order.
        ledger_release = next(index for index in ledger_releases if index > captures[0])
        epoch_release = next(index for index in epoch_releases if index > ledger_release)
        self.assertLess(ledger_release, epoch_release, timeline)
        self.assertLess(epoch_release, min(ledger_exclusive), timeline)

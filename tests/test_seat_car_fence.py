"""Native scratch confinement and serialization for the shared car fence."""
from tests.readiness import wait_event
import fcntl
import multiprocessing
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest import mock

import tests
from floati.car_fence import car_acceptance_guard
from floati.errors import ProtocolRefusal
from floati.root import FloatiRoot


def _hold_car(root_path, entered, release, finished, observe=False):
    root = FloatiRoot.open_direct_home(root_path)
    try:
        with car_acceptance_guard(root, observe=observe):
            entered.set()
            wait_event(release, 'seat_car_fence: release synchronization', timeout=120)
    finally:
        finished.set()


def _hold_replaced_car(root_path, entered, release, refused):
    root = FloatiRoot.open_direct_home(root_path)
    try:
        with car_acceptance_guard(root):
            entered.set()
            wait_event(release, 'seat_car_fence: release synchronization', timeout=120)
    except ProtocolRefusal:
        refused.set()


class SeatCarFenceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name).resolve()
        self.root = FloatiRoot.open_direct_home(self.home / "fleet", create=True)
        self.work = self.root.tenant_home / "work"
        self.lock = self.work / "car-acceptance.lock"

    def create_lock(self):
        self.work.mkdir(exist_ok=True, mode=0o700)
        self.lock.touch(mode=0o600)

    def test_guarded_operation_oserror_keeps_identity_and_releases_all_fences(self):
        for scenario, observe in (("missing-work", True), ("missing-lock", True),
                                  ("present-observed", True), ("present-exclusive", False)):
            with self.subTest(scenario=scenario):
                root = FloatiRoot.open_direct_home(self.home / scenario, create=True)
                work = root.tenant_home / "work"
                lock = work / "car-acceptance.lock"
                if scenario != "missing-work":
                    work.mkdir(mode=0o700)
                if scenario.startswith("present"):
                    lock.touch(mode=0o600)
                failure = OSError("native caller publication failed")
                try:
                    with car_acceptance_guard(root, observe=observe):
                        raise failure
                except BaseException as caught:
                    self.assertIs(caught, failure)
                else:
                    self.fail("caller failure was swallowed")
                with car_acceptance_guard(root):
                    pass

    def test_failed_operation_still_checks_changed_fence_on_exit(self):
        self.create_lock()
        failure = OSError("native caller publication failed")
        with self.assertRaises(ProtocolRefusal) as caught:
            with car_acceptance_guard(self.root):
                self.lock.unlink()
                raise failure
        self.assertEqual("car_acceptance_fence_invalid", caught.exception.code)
        self.assertIsInstance(caught.exception.__cause__, FileNotFoundError)
        with car_acceptance_guard(self.root):
            pass

    def assert_refused_before_entry(self, operation):
        entered = []
        with self.assertRaises(ProtocolRefusal):
            with operation:
                entered.append(True)
        self.assertEqual([], entered)

    def test_symlinked_work_never_creates_a_foreign_lock(self):
        foreign = self.home / "foreign"
        foreign.mkdir()
        self.work.symlink_to(foreign, target_is_directory=True)
        self.assert_refused_before_entry(car_acceptance_guard(self.root))
        self.assertEqual([], list(foreign.iterdir()))

    def test_symlinked_lock_is_refused_even_to_an_in_root_target(self):
        self.work.mkdir(mode=0o700)
        target = self.work / "unrelated.lock"
        target.touch(mode=0o600)
        self.lock.symlink_to(target)
        self.assert_refused_before_entry(car_acceptance_guard(self.root))

    def test_symlinked_work_is_refused_even_to_an_in_root_directory(self):
        target = self.root.tenant_home / "unrelated"
        target.mkdir()
        self.work.symlink_to(target, target_is_directory=True)
        self.assert_refused_before_entry(car_acceptance_guard(self.root))
        self.assertEqual([], list(target.iterdir()))

    def test_hardlinked_lock_cannot_be_a_foreign_fence(self):
        self.create_lock()
        os.link(self.lock, self.home / "foreign.lock")
        self.assert_refused_before_entry(car_acceptance_guard(self.root))

    def test_foreign_content_in_lock_is_not_overwritten_or_accepted(self):
        self.create_lock()
        self.lock.write_bytes(b"foreign controller state")
        self.assert_refused_before_entry(car_acceptance_guard(self.root))
        self.assertEqual(b"foreign controller state", self.lock.read_bytes())

    def test_other_writable_lock_cannot_split_a_car_fence(self):
        self.create_lock()
        self.lock.chmod(0o666)
        self.assert_refused_before_entry(car_acceptance_guard(self.root))

    def test_other_writable_work_directory_cannot_supply_a_lock(self):
        self.work.mkdir(mode=0o700)
        self.work.chmod(0o777)
        self.assert_refused_before_entry(car_acceptance_guard(self.root))
        self.assertEqual([], list(self.work.iterdir()))

    def test_fifo_is_refused_without_blocking(self):
        self.work.mkdir(mode=0o700)
        os.mkfifo(self.lock, 0o600)
        for observe in (False, True):
            self.assert_refused_before_entry(car_acceptance_guard(self.root, observe=observe))

    def test_foreign_file_owner_is_refused(self):
        self.create_lock()
        from types import SimpleNamespace
        real_fstat = os.fstat
        inode = self.lock.stat().st_ino

        def owner_changed(descriptor):
            info = real_fstat(descriptor)
            if info.st_ino != inode:
                return info
            return SimpleNamespace(st_dev=info.st_dev, st_ino=info.st_ino,
                st_mode=info.st_mode, st_uid=os.geteuid() + 1,
                st_nlink=info.st_nlink, st_size=info.st_size)

        with mock.patch("floati.car_fence.os.fstat", side_effect=owner_changed):
            self.assert_refused_before_entry(car_acceptance_guard(self.root))

    def _replace_at_acquisition(self, replace):
        self.create_lock()
        original = self.lock.stat()
        real_flock = fcntl.flock
        replaced = []

        def racing_flock(descriptor, operation):
            info = os.fstat(descriptor)
            if (info.st_dev, info.st_ino) == (original.st_dev, original.st_ino) and operation & fcntl.LOCK_NB and not replaced:
                replaced.append(True)
                replace()
            return real_flock(descriptor, operation)

        with mock.patch("fcntl.flock", side_effect=racing_flock):
            self.assert_refused_before_entry(car_acceptance_guard(self.root))
        self.assertEqual([True], replaced)

    def test_lock_replaced_after_open_cannot_accept_against_an_old_inode(self):
        def replace():
            self.lock.rename(self.work / "retained-old.lock")
            self.lock.touch(mode=0o600)
        self._replace_at_acquisition(replace)

    def test_work_directory_replaced_after_open_cannot_split_the_fence(self):
        def replace():
            self.work.rename(self.root.tenant_home / "retained-work")
            self.work.mkdir(mode=0o700)
            self.lock.touch(mode=0o600)
        self._replace_at_acquisition(replace)

    def test_root_replaced_after_open_cannot_use_its_old_epoch_fence(self):
        def replace():
            self.root.tenant_home.rename(self.home / "retained-fleet")
            self.root.tenant_home.mkdir(mode=0o700)
            self.work.mkdir(mode=0o700)
            self.lock.touch(mode=0o600)
        self._replace_at_acquisition(replace)

    def test_exit_detects_lost_lock_continuity(self):
        self.create_lock()
        with self.assertRaises(ProtocolRefusal):
            with car_acceptance_guard(self.root):
                self.lock.rename(self.work / "retained-old.lock")
                self.lock.touch(mode=0o600)

    def test_observation_does_not_create_work_or_lock(self):
        with car_acceptance_guard(self.root, observe=True) as present:
            self.assertFalse(present)
        self.assertFalse(self.work.exists())
        self.work.mkdir(mode=0o700)
        with car_acceptance_guard(self.root, observe=True) as present:
            self.assertFalse(present)
        self.assertEqual([], list(self.work.iterdir()))

    def test_observation_of_existing_fence_preserves_bytes_and_identity(self):
        self.create_lock()
        before = self.lock.stat()
        with car_acceptance_guard(self.root, observe=True) as present:
            self.assertTrue(present)
        after = self.lock.stat()
        self.assertEqual((before.st_ino, before.st_size, before.st_mtime_ns),
                         (after.st_ino, after.st_size, after.st_mtime_ns))

    def test_observation_refuses_changed_absence(self):
        with self.assertRaisesRegex(ProtocolRefusal, "fence_changed"):
            with car_acceptance_guard(self.root, observe=True):
                self.create_lock()
        self.lock.unlink()
        with self.assertRaisesRegex(ProtocolRefusal, "fence_changed"):
            with car_acceptance_guard(self.root, observe=True):
                self.lock.touch(mode=0o600)

    def test_contended_fence_obeys_native_waiter_deadline(self):
        from floati.waiter_lock_retry import waiter_lock_window, WaiterLockDeadline
        context = multiprocessing.get_context("fork")
        entered, release, finished = [context.Event() for _ in range(3)]
        child = context.Process(target=_hold_car,
            args=(self.root.tenant_home, entered, release, finished))
        child.start()
        try:
            self.assertTrue(wait_event(entered, 'seat_car_fence: entered synchronization'))
            with waiter_lock_window(0.05, time.monotonic, time.sleep) as window:
                with self.assertRaises(WaiterLockDeadline) as raised:
                    with car_acceptance_guard(self.root):
                        self.fail("contended fence entered after deadline")
                self.assertTrue(window.contended)
                self.assertIn("work/car-acceptance.lock", raised.exception.detail)
        finally:
            release.set()
            child.join(12)
        self.assertFalse(child.is_alive())
        self.assertEqual(0, child.exitcode)

    def test_claim_completion_and_notice_callers_serialize_across_processes(self):
        context = multiprocessing.get_context("fork")
        entered, release, finished = [context.Event() for _ in range(3)]
        child = context.Process(target=_hold_car,
            args=(self.root.tenant_home, entered, release, finished))
        child.start()
        try:
            self.assertTrue(wait_event(entered, 'seat_car_fence: entered synchronization'))
            self.assertFalse(finished.is_set())
            # Release only while this native second caller is contending.
            real_flock = fcntl.flock
            contended = []
            inodes = {self.lock.stat().st_ino, self.work.stat().st_ino}

            def releasing_flock(descriptor, operation):
                try:
                    return real_flock(descriptor, operation)
                except BlockingIOError:
                    if os.fstat(descriptor).st_ino in inodes:
                        contended.append(True)
                        release.set()
                    raise

            with mock.patch("fcntl.flock", side_effect=releasing_flock):
                with car_acceptance_guard(self.root):
                    self.assertTrue(wait_event(finished, 'seat_car_fence: finished synchronization'))
            self.assertTrue(contended)
        finally:
            release.set()
            child.join(12)
        self.assertFalse(child.is_alive())
        self.assertEqual(0, child.exitcode)

    def test_replaced_leaf_cannot_split_a_still_held_parent_fence(self):
        context = multiprocessing.get_context("fork")
        entered, release, refused = [context.Event() for _ in range(3)]
        child = context.Process(target=_hold_replaced_car,
            args=(self.root.tenant_home, entered, release, refused))
        child.start()
        try:
            self.assertTrue(wait_event(entered, 'seat_car_fence: entered synchronization'))
            self.lock.rename(self.work / "retained-old.lock")
            self.lock.touch(mode=0o600)
            real_flock = fcntl.flock
            parent_inode = self.work.stat().st_ino
            contended = []

            def releasing_parent(descriptor, operation):
                try:
                    return real_flock(descriptor, operation)
                except BlockingIOError:
                    if os.fstat(descriptor).st_ino == parent_inode:
                        contended.append(True)
                        release.set()
                    raise

            with mock.patch("fcntl.flock", side_effect=releasing_parent):
                with car_acceptance_guard(self.root):
                    self.assertTrue(contended, "replacement leaf bypassed the held parent fence")
                    self.assertTrue(wait_event(refused, 'seat_car_fence: refused synchronization'))
        finally:
            release.set()
            child.join(12)
        self.assertFalse(child.is_alive())
        self.assertEqual(0, child.exitcode)

    def test_existing_observation_serializes_with_a_native_mutator(self):
        self.create_lock()
        before = self.lock.stat()
        context = multiprocessing.get_context("fork")
        entered, release, finished = [context.Event() for _ in range(3)]
        child = context.Process(target=_hold_car,
            args=(self.root.tenant_home, entered, release, finished, True))
        child.start()
        try:
            self.assertTrue(wait_event(entered, 'seat_car_fence: entered synchronization'))
            real_flock = fcntl.flock
            parent_inode = self.work.stat().st_ino
            contended = []

            def release_reader(descriptor, operation):
                try:
                    return real_flock(descriptor, operation)
                except BlockingIOError:
                    if os.fstat(descriptor).st_ino == parent_inode:
                        contended.append(True)
                        release.set()
                    raise

            with mock.patch("fcntl.flock", side_effect=release_reader):
                with car_acceptance_guard(self.root):
                    self.assertTrue(contended)
                    self.assertTrue(wait_event(finished, 'seat_car_fence: finished synchronization'))
        finally:
            release.set()
            child.join(12)
        self.assertFalse(child.is_alive())
        self.assertEqual(0, child.exitcode)
        after = self.lock.stat()
        self.assertEqual((before.st_ino, before.st_size, before.st_mtime_ns),
                         (after.st_ino, after.st_size, after.st_mtime_ns))

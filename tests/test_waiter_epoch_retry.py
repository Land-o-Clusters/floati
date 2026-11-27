"""Native Stop lock/member races retain bounded re-arm and exact refusals."""
import fcntl
import os
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from floati import epoch_rotation
from tests.readiness import wait_event
from tests.temp_roots import REAL_TEMP_ROOT
from tests.probe_waiter_epoch_retry import run_case


class WaiterEpochRetryTests(unittest.TestCase):
    def check_native(self, case):
        with tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT) as base:
            result = run_case(case, Path(base) / case)
            for name, passed in result["checks"].items():
                with self.subTest(check=name):
                    self.assertTrue(passed, result)

    def test_late_contender_still_encounters_the_owned_epoch_holder(self):
        run_maintenance = epoch_rotation.stop_maintenance
        start_thread = threading.Thread.start
        wait = threading.Event.wait
        flock = fcntl.flock
        owner = threading.current_thread()

        for case in ("lock_release", "lock_short_map", "lock_deadline", "lock_retry_release"):
            with self.subTest(case=case):
                entered_wait = threading.Event()
                expired = threading.Event()
                exited = threading.Event()
                role = threading.local()
                coordinates = set()
                contended = set()

                def start(worker):
                    target = worker._target
                    if getattr(target, "__name__", None) == "hold_epoch":
                        def observe_exit():
                            role.holder = True
                            try:
                                target()
                            finally:
                                exited.set()
                        worker._target = observe_exit
                    return start_thread(worker)

                def delayed_wait(event, timeout=None):
                    if getattr(role, "holder", False):
                        if timeout is not None and timeout < 10:
                            expired.set()
                        entered_wait.set()
                        if expired.is_set():
                            return wait(event, 0)
                    return wait(event, timeout)

                def late_maintenance(root, **kwargs):
                    identity = root.tenant_home.stat()
                    coordinates.add((identity.st_dev, identity.st_ino))
                    wait_event(entered_wait, "epoch holder enters its release wait")
                    if expired.is_set():
                        wait_event(exited, "short fixture lease expires before native contender")
                    return run_maintenance(root, **kwargs)

                def saw_contender(descriptor, operation):
                    try:
                        return flock(descriptor, operation)
                    except BlockingIOError:
                        identity = os.fstat(descriptor)
                        coordinate = (identity.st_dev, identity.st_ino)
                        if threading.current_thread() is owner and coordinate in coordinates:
                            contended.add(coordinate)
                        raise

                with patch.object(threading.Thread, "start", start),\
                        patch.object(threading.Event, "wait", delayed_wait),\
                        patch.object(epoch_rotation, "stop_maintenance", late_maintenance),\
                        patch.object(fcntl, "flock", saw_contender):
                    self.check_native(case)
                self.assertTrue(contended, "the real contender must encounter the held epoch lock")

    def test_native_delivery_uses_original_budget_after_host_scheduling_gap(self):
        from tests import probe_waiter_epoch_retry as probe

        stop_maintenance = epoch_rotation.stop_maintenance
        for case in ("lock_release", "member_once"):
            with self.subTest(case=case):
                scheduled_gap = 0.0

                def delayed_maintenance(*args, **kwargs):
                    nonlocal scheduled_gap
                    try:
                        return stop_maintenance(*args, **kwargs)
                    finally:
                        scheduled_gap = 9.0

                host_clock = SimpleNamespace(
                    monotonic=lambda: time.monotonic() + scheduled_gap,
                    sleep=time.sleep,
                )
                with patch.object(probe, "time", host_clock),\
                        patch.object(epoch_rotation, "stop_maintenance", delayed_maintenance):
                    self.check_native(case)
                self.assertEqual(9.0, scheduled_gap, "must reach the real maintenance seam")

    def test_epoch_lock_deadline_blocks_to_rearm_without_integrity_exit(self):
        self.check_native("lock_deadline")

    def test_epoch_lock_release_delivers_exact_mail_in_original_window(self):
        self.check_native("lock_release")

    def test_epoch_acquisition_timeout_then_release_retries_in_original_window(self):
        self.check_native("lock_retry_release")

    def test_short_epoch_contention_preserves_strict_native_map_refusal(self):
        self.check_native("lock_short_map")

    def test_actual_selected_identity_change_replans_and_delivers(self):
        self.check_native("member_once")

    def test_corruption_after_selected_race_retains_native_refusal(self):
        self.check_native("member_corrupt")

    def test_member_retry_rechecks_actual_native_pause_before_delivery(self):
        self.check_native("member_pause")

    def test_same_code_planner_protocol_refusal_is_not_retried_as_race(self):
        self.check_native("planner_refusal")

    def test_same_code_integrity_failure_is_not_retried_as_race(self):
        self.check_native("member_integrity")

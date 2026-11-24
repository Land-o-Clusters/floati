"""Opt-in scheduling displacement for the existing waiter test scenarios.

Selected with `python -m unittest scripts.mac_leg_flake_schedule`, never by
discovery. Only the test observer's stopwatch and thread scheduling change;
the product clocks, native locks, receipts and assertions remain real.
This models an observer descheduled between its two stopwatch reads, a
dispatch descheduled after the observer timestamp, and a takeover thread
not scheduled within the old half-second join. It is not a timing sample.
"""
from datetime import datetime, timedelta
import threading
import time
from types import SimpleNamespace
import unittest
from unittest import mock

from tests import test_waiter_lock_retry as fixture


SCENARIOS = (
    "test_short_native_startup_guard_budget_cannot_use_five_second_acquisition",
    "test_delayed_native_start_metadata_retains_original_expiry",
    "test_cursor_takeover_commits_during_native_retry_backoff",
    "test_native_takeover_progresses_while_events_lock_remains_held",
    "test_cursor_initial_guard_expiry_never_bootstraps_terminal_claim",
    "test_cursor_deadline_cannot_publish_rearm_while_replacement_guard_is_busy",
    "test_cursor_initial_retry_uses_shorter_native_consent_window",
    "test_cursor_malformed_native_consent_keeps_typed_unreadable_without_retry",
)


class ScheduledThread(threading.Thread):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.scheduled = threading.Event()

    def run(self):
        if getattr(self._target, "__name__", "") == "replace":
            if not self.scheduled.wait(60):
                raise AssertionError("takeover scheduling event was not released")
        super().run()

    def join(self, timeout=None):
        if timeout != 0.5:
            self.scheduled.set()
            return super().join(timeout)
        # Model a thread still awaiting its first timeslice when the legacy
        # half-second join expires. Cleanup releases it through the same API.


class EarlierObservation(datetime):
    @classmethod
    def now(cls, tz=None):
        return datetime.now(tz) - timedelta(seconds=2)


class ScheduledSuite(unittest.TestSuite):
    def run(self, result, debug=False):
        reads = {}

        def observer_clock():
            identity = threading.get_ident()
            count = reads.get(identity, 0)
            reads[identity] = count + 1
            return time.monotonic() + (6 if count % 2 else 0)

        # Replace only this test module's references, never the shared time
        # or threading modules that product code uses.
        with mock.patch.object(fixture, "time", SimpleNamespace(
                monotonic=observer_clock, sleep=time.sleep)),\
                mock.patch.object(fixture, "datetime", EarlierObservation),\
                mock.patch.object(fixture, "threading", SimpleNamespace(
                    Thread=ScheduledThread, Event=threading.Event, Timer=threading.Timer)):
            return super().run(result, debug)


def load_tests(loader, tests, pattern):
    return ScheduledSuite(fixture.WaiterLockRetryTests(name) for name in SCENARIOS)

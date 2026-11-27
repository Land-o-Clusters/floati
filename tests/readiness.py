"""Event-driven test setup with a generous watchdog and attributable failures.

These bounds are test-harness watchdogs, never product deadline inputs.
Keep deadline/refusal tests on their explicitly measured product clocks.
"""
from __future__ import annotations

import os
import queue
import select
import subprocess
import threading
import time


READY_SECONDS = 60.0


class Readiness:
    def __init__(self, description, timeout=READY_SECONDS):
        self.description = description
        self.bound = timeout
        self.started = time.monotonic()

    def failure(self):
        try:
            load = os.getloadavg()
        except (AttributeError, OSError):
            load = "unavailable"
        return AssertionError(
            f"{self.description}: readiness bound={self.bound:.3f}s "
            f"elapsed={time.monotonic() - self.started:.3f}s host_load={load}"
        )

    def remaining(self):
        remaining = self.bound - (time.monotonic() - self.started)
        if remaining <= 0:
            raise self.failure()
        return remaining

    def until(self, predicate):
        while True:
            value = predicate()
            if value:
                return value
            time.sleep(min(0.01, self.remaining()))

    def event(self, event):
        if not event.wait(self.remaining()):
            raise self.failure()
        return True

    def readable(self, source):
        ready, _, _ = select.select([source], [], [], self.remaining())
        if not ready:
            raise self.failure()
        return ready


def wait_until(predicate, description, timeout=READY_SECONDS):
    return Readiness(description, timeout).until(predicate)


def wait_event(event, description, timeout=READY_SECONDS):
    return Readiness(description, timeout).event(event)


def wait_readable(source, description, timeout=READY_SECONDS):
    return Readiness(description, timeout).readable(source)


def wait_poll(connection, description, timeout=READY_SECONDS):
    readiness = Readiness(description, timeout)
    if not connection.poll(readiness.remaining()):
        raise readiness.failure()
    return True


def wait_join(thread, description, timeout=READY_SECONDS):
    readiness = Readiness(description, timeout)
    thread.join(readiness.remaining())
    if thread.is_alive():
        raise readiness.failure()


def wait_barrier(barrier, description, timeout=READY_SECONDS):
    readiness = Readiness(description, timeout)
    try:
        return barrier.wait(readiness.remaining())
    except threading.BrokenBarrierError:
        raise readiness.failure() from None


def wait_process(process, description, timeout=READY_SECONDS):
    readiness = Readiness(description, timeout)
    try:
        return process.wait(timeout=readiness.remaining())
    except subprocess.TimeoutExpired:
        raise readiness.failure() from None


def communicate(process, description, timeout=READY_SECONDS, **kwargs):
    readiness = Readiness(description, timeout)
    try:
        return process.communicate(timeout=readiness.remaining(), **kwargs)
    except subprocess.TimeoutExpired:
        raise readiness.failure() from None


def run_process(argv, description, **kwargs):
    """Collect an owned command under the shared hang ceiling."""
    readiness = Readiness(description)
    try:
        return subprocess.run(argv, timeout=readiness.remaining(), **kwargs)
    except subprocess.TimeoutExpired:
        raise readiness.failure() from None


def wait_queue(source, description, timeout=READY_SECONDS):
    readiness = Readiness(description, timeout)
    try:
        return source.get(timeout=readiness.remaining())
    except queue.Empty:
        raise readiness.failure() from None


def wait_condition(condition, predicate, description, timeout=READY_SECONDS):
    """Wait while the caller owns the condition's lock, as wait_for requires."""
    readiness = Readiness(description, timeout)
    value = condition.wait_for(predicate, timeout=readiness.remaining())
    if not value:
        raise readiness.failure()
    return value

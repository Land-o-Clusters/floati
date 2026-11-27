"""Explicit scripted readiness for existing virtual-clock/race controls."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import select
import time
from unittest.mock import patch


@contextmanager
def seeded_mail_timing():
    """Read durable fixture mail at one instant across both waiter clocks."""
    now = time.time()
    stamp = datetime.fromtimestamp(now, timezone.utc).isoformat(timespec="milliseconds")

    def unexpected_wait(*_args):
        raise AssertionError("seeded mail must be presented without waiting")

    # Delivery liveness compares the event timestamp with the hold's own
    # wall-time testimony. Keep those on the same timeline as the wait budget.
    with patch("floati.events.utc_now", return_value=stamp.replace("+00:00", "Z")):
        yield dict(monotonic=lambda: 0.0, wall_time=lambda: now,
                   sleep=unexpected_wait, wait_readable=unexpected_wait)


def scripted_readiness(sleeper, *, interval=1.0):
    """Schedule the fixture's external event without replacing native reads.

    The supplied sleeper advances its fake clock or injects a real concurrent
    event at its original schedule. An early scheduled tick is a spurious
    readiness return, not a completed timeout. The actual native source and
    all native reads still decide whether anything changed; no inbox or
    authorized decision is fabricated.
    """
    def wait(source, timeout):
        if timeout <= 0:
            raise AssertionError("waiter requested readiness outside its budget")
        step = min(interval, timeout)
        sleeper(step)
        return bool(select.select([source], [], [], 0)[0]) or step < timeout
    return wait

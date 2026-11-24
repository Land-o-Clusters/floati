"""Explicit scripted readiness for existing virtual-clock/race controls."""
from __future__ import annotations

import select


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

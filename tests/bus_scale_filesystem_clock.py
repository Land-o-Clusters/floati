"""Recorded filesystem timestamp bounds for owned census invocations.

Linux's CLOCK_REALTIME_COARSE is clock ID 5 in the Linux UAPI. Kernel
filesystem timestamp updates can use this coarse realtime clock, so a newly
written file can precede a precise userspace realtime sample within the same
tick. Record the coarse boundary before the invocation rather than inventing
a tolerance or sampling a clock later while validating retained evidence.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import sys
import time


_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_LINUX_CLOCK_REALTIME_COARSE = 5
_FIELDS = frozenset({"platform", "clock", "lower_ns", "realtime_ns", "wall_begin"})
_CLOCKS = {"linux": "CLOCK_REALTIME_COARSE", "darwin": "CLOCK_REALTIME"}


def epoch_ns(value):
    """Convert an aware datetime to epoch nanoseconds without float rounding."""
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError("filesystem clock requires a timezone-aware datetime")
    delta = value.astimezone(timezone.utc) - _EPOCH
    return ((delta.days * 86400 + delta.seconds) * 1000000 + delta.microseconds) * 1000


def _platform():
    if sys.platform not in _CLOCKS:
        raise ValueError("filesystem clock is unsupported on platform: " + sys.platform)
    return sys.platform


def capture_filesystem_clock():
    """Return the precise UTC begin and its contemporaneous lower-bound receipt."""
    platform = _platform()
    if platform == "linux":
        try:
            lower = time.clock_gettime_ns(_LINUX_CLOCK_REALTIME_COARSE)
        except (AttributeError, OSError, ValueError) as error:
            raise ValueError("Linux CLOCK_REALTIME_COARSE is unavailable") from error
        realtime = time.time_ns()
    else:
        realtime = time.time_ns()
        lower = realtime
    if type(lower) is not int or type(realtime) is not int or lower > realtime:
        raise ValueError("filesystem clock samples are inconsistent")
    begin = _EPOCH + timedelta(microseconds=realtime // 1000)
    receipt = {"platform": platform, "clock": _CLOCKS[platform], "lower_ns": lower,
               "realtime_ns": realtime, "wall_begin": begin.isoformat()}
    return begin, receipt


def filesystem_bounds(begin, end, receipt=None):
    """Validate retained clock evidence and return inclusive integer bounds.

    Without a receipt, preserve the strict datetime interval. Darwin also
    uses the begin datetime's microsecond precision; Linux uses only the
    coarse lower boundary actually recorded before that interval. The upper
    boundary always remains the precise collected end datetime.
    """
    platform = _platform()
    lower, upper = epoch_ns(begin), epoch_ns(end)
    if lower > upper:
        raise ValueError("filesystem clock interval is reversed")
    if receipt is None:
        return lower, upper
    if type(receipt) is not dict or set(receipt) != _FIELDS:
        raise ValueError("filesystem clock receipt has an invalid field population")
    if (type(receipt["platform"]) is not str or receipt["platform"] != platform
            or type(receipt["clock"]) is not str or receipt["clock"] != _CLOCKS[platform]):
        raise ValueError("filesystem clock receipt has a foreign platform or clock")
    recorded, realtime = receipt["lower_ns"], receipt["realtime_ns"]
    if type(recorded) is not int or type(realtime) is not int or recorded > realtime:
        raise ValueError("filesystem clock receipt has inconsistent sample bounds")
    if (type(receipt["wall_begin"]) is not str or receipt["wall_begin"] != begin.isoformat()
            or realtime // 1000 * 1000 != lower):
        raise ValueError("filesystem clock receipt does not bind the exact invocation begin")
    if platform == "darwin":
        if recorded != realtime:
            raise ValueError("Darwin filesystem clock must use its recorded realtime sample")
        return lower, upper
    if recorded > upper:
        raise ValueError("filesystem clock lower boundary is after invocation end")
    return recorded, upper

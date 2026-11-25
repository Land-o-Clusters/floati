"""Read the proposed Puddle quota export. The caller selects the local file."""

import json
import os
import re
import stat
from datetime import datetime
from pathlib import Path

from .addressing import _unique_object
from .session_identity import IDENTIFIER_PATTERN


SNAPSHOT_NAME = "puddle-quota-v1.json"
SOURCE = "puddle_quota_snapshot_v1"
_MAX_BYTES = 65536
_TIMESTAMP = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?(?:Z|\+00:00)")
_READING_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")


class QuotaUnavailable(ValueError):
    """A routing input cannot support a measured choice."""


def quota_utc(value):
    if not isinstance(value, str) or not _TIMESTAMP.fullmatch(value):
        raise ValueError("invalid UTC timestamp")
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def reading_interval(reading):
    fields = {"status", "reading_id", "measured_at", "expires_at", "remaining_bps"}
    if (not isinstance(reading, dict) or set(reading) != fields
            or reading["status"] != "MEASURED"
            or not isinstance(reading["reading_id"], str)
            or not _READING_ID.fullmatch(reading["reading_id"])
            or type(reading["remaining_bps"]) is not int
            or not 0 <= reading["remaining_bps"] <= 10000):
        raise ValueError("invalid measured quota reading")
    measured = quota_utc(reading["measured_at"])
    expires = quota_utc(reading["expires_at"])
    if expires <= measured:
        raise ValueError("invalid quota reading interval")
    return measured, expires


def read_puddle_quota_snapshot(path):
    if path is None:
        raise QuotaUnavailable("quota_snapshot_not_selected")
    source = Path(path)
    if not source.is_absolute() or source.name != SNAPSHOT_NAME:
        raise QuotaUnavailable("quota_snapshot_invalid")
    try:
        with os.fdopen(os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValueError("snapshot must be an ordinary file")
            raw = stream.read(_MAX_BYTES + 1)
        if len(raw) > _MAX_BYTES:
            raise ValueError("snapshot exceeds size bound")
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
        if (not isinstance(value, dict) or set(value) != {"schema_version", "readings"}
                or type(value["schema_version"]) is not int or value["schema_version"] != 1
                or not isinstance(value["readings"], dict)):
            raise ValueError("invalid quota snapshot version or shape")
        for harness, reading in value["readings"].items():
            if not IDENTIFIER_PATTERN.fullmatch(harness):
                raise ValueError("invalid harness key")
            reading_interval(reading)
    except FileNotFoundError as exc:
        raise QuotaUnavailable("quota_snapshot_missing") from exc
    except (ValueError, RecursionError) as exc:
        raise QuotaUnavailable("quota_snapshot_invalid") from exc
    except OSError as exc:
        raise QuotaUnavailable("quota_snapshot_unreadable") from exc
    return value["readings"]

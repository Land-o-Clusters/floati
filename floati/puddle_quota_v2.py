"""The closed, local Puddle quota interchange for enrolled accounts."""

import ctypes
import json
import os
import re
import stat
import sys
from datetime import timedelta
from pathlib import Path

from .addressing import _unique_object
from .puddle_quota_snapshot import QuotaUnavailable, quota_utc
from .session_identity import IDENTIFIER_PATTERN

SNAPSHOT_NAME = "puddle-quota-v2.json"
SOURCE = "puddle_quota_snapshot_v2"
ACCOUNT_REF = re.compile(r"acct-[0-9a-f]{32}")
READING_ID = re.compile(r"r-[0-9a-f]{32}")
INSTANCE_REF = re.compile(r"[0-9a-f]{64}")
FRESHNESS = timedelta(seconds=120)
_MAX_BYTES = 65536
_READING_FIELDS = {"harness", "account_ref", "reading_id", "observed_at", "value"}
_UNKNOWN_REASONS = (
    "source_unavailable", "account_unverified", "window_missing",
    "window_conflict", "denominator_unknown",
)
_LOCAL_LINUX_FILESYSTEMS = {
    "btrfs", "ext2", "ext3", "ext4", "f2fs", "overlay", "ramfs",
    "tmpfs", "xfs", "zfs",
}


def quota_stamp(value):
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValueError("invalid quota observation timestamp")
    return quota_utc(value)


def _bounded_integer(value, low, high):
    return type(value) is int and low <= value <= high


def reading_index(rows):
    """Validate every row before any account can use the generation."""
    if not isinstance(rows, list) or len(rows) > 512:
        raise ValueError("invalid quota readings")
    index, ids = {}, set()
    for row in rows:
        if not isinstance(row, dict) or set(row) not in (
            _READING_FIELDS, _READING_FIELDS | {"t3_provider_instance"},
        ):
            raise ValueError("invalid quota reading fields")
        harness, account, reading_id = row["harness"], row["account_ref"], row["reading_id"]
        if (not all(isinstance(item, str) for item in (harness, account, reading_id))
                or not IDENTIFIER_PATTERN.fullmatch(harness)
                or not ACCOUNT_REF.fullmatch(account) or not READING_ID.fullmatch(reading_id)):
            raise ValueError("invalid quota identity")
        key = (harness, account)
        if key in index or reading_id in ids:
            raise ValueError("duplicate quota identity")
        observed = quota_stamp(row["observed_at"])
        if "t3_provider_instance" in row:
            instance = row["t3_provider_instance"]
            if (not isinstance(instance, dict) or set(instance) != {"instance_ref", "verified_at"}
                    or not isinstance(instance["instance_ref"], str)
                    or not INSTANCE_REF.fullmatch(instance["instance_ref"])):
                raise ValueError("invalid provider instance binding")
            quota_stamp(instance["verified_at"])
        value = row["value"]
        if not isinstance(value, dict):
            raise ValueError("invalid quota value")
        if value.get("status") == "UNKNOWN":
            if set(value) != {"status", "reason"} or value["reason"] not in _UNKNOWN_REASONS:
                raise ValueError("invalid unknown quota value")
        else:
            if (set(value) != {"status", "window_kind", "window_seconds", "remaining_bps",
                               "resets_at", "expires_at"}
                    or value["status"] != "MEASURED"
                    or value["window_kind"] not in ("account_primary", "account_weekly")
                    or not _bounded_integer(value["window_seconds"], 1, 31536000)
                    or not _bounded_integer(value["remaining_bps"], 0, 10000)):
                raise ValueError("invalid measured quota value")
            reset, expiry = quota_stamp(value["resets_at"]), quota_stamp(value["expires_at"])
            if not observed < expiry <= reset or expiry - observed != min(reset - observed, FRESHNESS):
                raise ValueError("invalid quota interval")
        index[key] = row
        ids.add(reading_id)
    return index


class UnknownQuotaReading(QuotaUnavailable):
    def __init__(self, reason):
        super().__init__("quota_reading_unknown")
        self.reason = reason


def current_account_reading(row, at):
    if row is None:
        raise QuotaUnavailable("quota_reading_missing")
    if quota_stamp(row["observed_at"]) > at:
        raise QuotaUnavailable("quota_reading_future")
    if row["value"]["status"] == "UNKNOWN":
        raise UnknownQuotaReading(row["value"]["reason"])
    if at >= quota_stamp(row["value"]["expires_at"]):
        raise QuotaUnavailable("quota_reading_stale")
    return row


class _DarwinStatfs(ctypes.Structure):
    # Darwin 64-bit struct statfs, from sys/mount.h.
    _fields_ = [
        ("block_size", ctypes.c_uint32), ("io_size", ctypes.c_int32),
        ("counts", ctypes.c_uint64 * 5), ("fsid", ctypes.c_int32 * 2),
        ("owner", ctypes.c_uint32), ("fs_type", ctypes.c_uint32),
        ("flags", ctypes.c_uint32), ("subtype", ctypes.c_uint32),
        ("type_name", ctypes.c_char * 16), ("mountpoint", ctypes.c_char * 1024),
        ("mounted_from", ctypes.c_char * 1024), ("extended", ctypes.c_uint32 * 8),
    ]


def _local_filesystem(descriptor):
    if sys.platform == "darwin":
        value = _DarwinStatfs()
        libc = ctypes.CDLL(None)
        fstatfs = getattr(libc, "fstatfs$INODE64", libc.fstatfs)
        result = fstatfs(descriptor, ctypes.byref(value))
        return result == 0 and bool(value.flags & 0x1000)  # MNT_LOCAL
    if sys.platform.startswith("linux"):
        # Device number alone cannot distinguish separate mounts of a filesystem.
        with open(f"/proc/self/fdinfo/{descriptor}", encoding="ascii") as stream:
            mount = next((line.split()[1] for line in stream if line.startswith("mnt_id:")), None)
        with open("/proc/self/mountinfo", encoding="utf-8", errors="surrogateescape") as stream:
            for line in stream:
                fields = line.split()
                if fields[0] == mount:
                    return fields[fields.index("-") + 1] in _LOCAL_LINUX_FILESYSTEMS
    return False


def _identity(info):
    return info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_nlink


def _read_private_snapshot(path):
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    descriptors = [os.open(path.anchor, directory_flags)]
    try:
        for part in path.parts[1:-1]:
            descriptors.append(os.open(part, directory_flags, dir_fd=descriptors[-1]))
        parent = descriptors[-1]
        directory = os.fstat(parent)
        owner = os.geteuid()
        if (directory.st_uid != owner or stat.S_IMODE(directory.st_mode) != 0o700
                or not _local_filesystem(parent)):
            raise PermissionError("quota export directory is not private and local")
        file_flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
        with os.fdopen(os.open(path.name, file_flags, dir_fd=parent), "rb") as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != owner
                    or info.st_mode & 0o077 or info.st_nlink != 1
                    or not _local_filesystem(stream.fileno())):
                raise PermissionError("quota export file is not private and local")
            raw = stream.read(_MAX_BYTES + 1)
            after = os.fstat(stream.fileno())
            if (_identity(info)[:4] != _identity(after)[:4] or after.st_nlink > 1
                    or info.st_size != after.st_size or info.st_mtime_ns != after.st_mtime_ns
                    or (after.st_nlink == 1 and info.st_ctime_ns != after.st_ctime_ns)):
                raise PermissionError("quota export changed during read")
        # Allow an atomic publication to replace the original file. The held
        # file is one generation, provided it stayed private throughout.
        for index, part in enumerate(path.parts[1:-1]):
            expected = os.fstat(descriptors[index + 1])
            named = os.stat(part, dir_fd=descriptors[index], follow_symlinks=False)
            if _identity(named) != _identity(expected):
                raise PermissionError("quota export directory changed during read")
        final = os.fstat(parent)
        if (final.st_mode, final.st_uid) != (directory.st_mode, directory.st_uid):
            raise PermissionError("quota export directory permissions changed during read")
        return raw
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _decimal_integer(token):
    if len(token) > 20:
        raise ValueError("quota integer exceeds the wire bound")
    number = int(token)
    if not -(2 ** 63) <= number < 2 ** 63:
        raise ValueError("quota integer exceeds the wire bound")
    return number


def _invalid_number(_token):
    raise ValueError("quota numbers must be decimal integer tokens")


def _check_depth(document):
    pending = [(document, 1)]
    while pending:
        value, depth = pending.pop()
        if isinstance(value, (dict, list)):
            if depth > 16:
                raise ValueError("quota document exceeds the nesting bound")
            children = value.values() if isinstance(value, dict) else value
            pending.extend((child, depth + 1) for child in children)


def read_account_quota(path, tenant):
    source = Path(path)
    if (not source.is_absolute() or source.anchor != "/" or source.name != SNAPSHOT_NAME
            or os.fspath(source) != os.fspath(path) or ".." in source.parts):
        raise QuotaUnavailable("quota_snapshot_invalid")
    try:
        raw = _read_private_snapshot(source)
    except FileNotFoundError as exc:
        raise QuotaUnavailable("quota_snapshot_missing") from exc
    except OSError as exc:
        raise QuotaUnavailable("quota_snapshot_unreadable") from exc
    try:
        if len(raw) > _MAX_BYTES:
            raise ValueError("quota document exceeds the byte bound")
        document = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object,
                              parse_int=_decimal_integer, parse_float=_invalid_number,
                              parse_constant=_invalid_number)
        _check_depth(document)
        version = document.get("schema_version") if isinstance(document, dict) else None
        if type(version) is int and version not in (1, 2):
            raise QuotaUnavailable("quota_snapshot_version_unsupported")
        if (type(version) is not int or version != 2
                or set(document) != {"schema_version", "tenant_id", "readings"}
                or not isinstance(document["tenant_id"], str)
                or not IDENTIFIER_PATTERN.fullmatch(document["tenant_id"])):
            raise ValueError("invalid quota document")
        readings = reading_index(document["readings"])
    except QuotaUnavailable:
        raise
    except (ValueError, RecursionError) as exc:
        raise QuotaUnavailable("quota_snapshot_invalid") from exc
    if document["tenant_id"] != tenant:
        raise QuotaUnavailable("quota_tenant_mismatch")
    return readings

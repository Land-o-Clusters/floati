"""Bounded advisory observations of the selected waiter backend.

These cells describe an observed wait, never session authority or process
liveness. Missing, malformed and expired observations remain named UNKNOWNs.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
import stat

from .errors import FloatiError
from .ids import uuid7_hex
from .recipient_mechanisms import _json, _parent, _read, _time
from .root import validate_identifier


_LIMIT = 2048
_FIELDS = {"schema_version", "tenant_id", "node_id", "mode", "observed_at", "valid_until"}
_MAX_AGE_SECONDS = 120
_READ_ERRORS = (FloatiError, OSError, ValueError, TypeError, KeyError, OverflowError, RecursionError)


def _path(root, node):
    return root.tenant_home / "state/waiter-observations" / (validate_identifier(node, "node") + ".json")


def _utc(now):
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("waiter observation time must be aware")
    return now.astimezone(timezone.utc)


def _decode(root, node, raw):
    row = _json(raw)
    if (not isinstance(row, dict) or set(row) != _FIELDS
            or type(row["schema_version"]) is not int or row["schema_version"] != 1
            or row["tenant_id"] != root.tenant_id or row["node_id"] != node
            or row["mode"] not in {"blocking", "polling"}
            or not isinstance(row["observed_at"], str) or not isinstance(row["valid_until"], str)):
        raise ValueError("invalid waiter observation")
    observed, expires = _time(row["observed_at"]), _time(row["valid_until"])
    if not 0 < (expires - observed).total_seconds() <= _MAX_AGE_SECONDS:
        raise ValueError("unbounded waiter observation")
    return row, observed, expires


def _unknown(reason):
    return {"stamp": "UNKNOWN", "value": None, "observed_at": None,
            "age_seconds": None, "valid_until": None, "reason": reason}


def project_waiter_mode(root, node, now):
    """Read one ordinary bounded observation without creating any root state."""
    try:
        current = _utc(now)
        row, observed, expires = _decode(root, node, _read(_path(root, node), _LIMIT))
    except FileNotFoundError:
        return _unknown("waiter_observation_absent")
    except _READ_ERRORS:
        return _unknown("waiter_observation_invalid")
    if observed > current:
        return _unknown("waiter_observation_in_future")
    if current >= expires:
        return _unknown("waiter_observation_expired")
    return {"stamp": "MEASURED", "value": row["mode"],
            "observed_at": row["observed_at"], "age_seconds": int((current - observed).total_seconds()),
            "valid_until": row["valid_until"], "reason": None}


def _close_descriptors(descriptors):
    for descriptor in reversed(descriptors):
        try:
            os.close(descriptor)
        except OSError:
            pass


def _open_observation_parent(root):
    descriptors = []
    try:
        parent = _parent(root.tenant_home / "coordinate")
        descriptors.append(parent)
        for component in ("state", "waiter-observations"):
            try:
                os.mkdir(component, 0o700, dir_fd=parent)
            except FileExistsError:
                pass
            parent = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            descriptors.append(parent)
            metadata = os.fstat(parent)
            if metadata.st_uid != os.geteuid() or metadata.st_mode & 0o022:
                raise ValueError("waiter observation parent is not owned ordinary state")
        return parent, descriptors
    except BaseException:
        _close_descriptors(descriptors)
        raise


def prepare_waiter_observation(root):
    """Create only the advisory namespace before a watch seals its baseline.

    No mode is recorded here. Later atomic leaf renewals stay inside this
    dedicated directory and cannot churn the selected state parent metadata.
    """
    try:
        _parent_descriptor, descriptors = _open_observation_parent(root)
    except _READ_ERRORS:
        return False
    _close_descriptors(descriptors)
    return True


def write_waiter_observation(root, node, mode, valid_until, *, observed_at=None):
    """Atomically publish advisory state; an observer fault never stops a wait."""
    descriptors = []
    temporary = None
    parent = None
    try:
        observed = _utc(observed_at if observed_at is not None else datetime.now(timezone.utc))
        expires = _utc(valid_until)
        row = {"schema_version": 1, "tenant_id": root.tenant_id, "node_id": node,
               "mode": mode, "observed_at": observed.isoformat(), "valid_until": expires.isoformat()}
        encoded = (json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
        _decode(root, node, encoded)
        if len(encoded) > _LIMIT:
            return False
        path = _path(root, node)
        parent, descriptors = _open_observation_parent(root)
        try:
            previous = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
            if (not stat.S_ISREG(previous.st_mode) or previous.st_nlink != 1
                    or previous.st_uid != os.geteuid()):
                return False
        except FileNotFoundError:
            pass
        temporary = "." + path.name + "." + uuid7_hex() + ".tmp"
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                             0o600, dir_fd=parent)
        try:
            if os.write(descriptor, encoded) != len(encoded):
                return False
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        # Reject a replaced parent before publication, without following aliases.
        current_parent = _parent(path)
        try:
            if ((os.fstat(current_parent).st_dev, os.fstat(current_parent).st_ino)
                    != (os.fstat(parent).st_dev, os.fstat(parent).st_ino)):
                return False
        finally:
            os.close(current_parent)
        os.replace(temporary, path.name, src_dir_fd=parent, dst_dir_fd=parent)
        temporary = None
        os.fsync(parent)
        return True
    except _READ_ERRORS:
        return False
    finally:
        if temporary is not None and parent is not None:
            try:
                os.unlink(temporary, dir_fd=parent)
            except OSError:
                pass
        _close_descriptors(descriptors)

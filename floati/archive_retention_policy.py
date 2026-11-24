"""Read-only archive policy and pure retention DATA; no retirement authority."""
from datetime import datetime, timedelta
import json
import os
import stat

from .errors import ProtocolRefusal
from .records import _json_integer, validate_record

MAX_POLICY_BYTES = 65536
_NAME = "archive-policy.json"


def _invalid(detail):
    return ProtocolRefusal("archive_policy_invalid", detail,
        remedy="provide state/archive-policy.json as one unchanged regular JSON file with schema_version 1 and boolean keep_forever")


def _clock(detail):
    return ProtocolRefusal("archive_retention_clock_invalid", detail,
        remedy="supply an explicit aware UTC observation at or after the native roll timestamp; retain archive payload")


def _policy(value):
    if (not isinstance(value, dict) or set(value) != {"schema_version", "keep_forever"}
            or _json_integer(value["schema_version"]) != 1 or type(value["keep_forever"]) is not bool):
        raise _invalid("archive policy must be exactly {schema_version: 1, keep_forever: bool}")
    return {"schema_version": 1, "keep_forever": value["keep_forever"]}


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise _invalid("archive policy repeats a JSON field")
        result[key] = value
    return result


def _constant(value):
    raise _invalid("archive policy contains a nonfinite JSON constant")


def read_archive_policy(root):
    """Read the fixed policy without initializing state or changing any bytes.

    Shared EPOCH excludes cooperative writers. No-follow descriptors and exact
    seven-field identity rechecks reject observed external substitution/growth;
    this observation is DATA and grants no future mutation authority.
    """
    from .bus_epoch import epoch_guard, _require_owned_epoch_guard, _selected_member_identity
    identity = _selected_member_identity
    absent = {"schema_version": 1, "keep_forever": False}
    parent = leaf = None
    try:
        with epoch_guard(root, exclusive=False):
            held = _require_owned_epoch_guard(root)
            root_identity = identity(os.fstat(held.descriptor))
            def root_bound():
                _require_owned_epoch_guard(root)
                if identity(os.fstat(held.descriptor)) != root_identity:
                    raise _invalid("archive policy root changed during observation")
            def missing(name, descriptor):
                try:
                    os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                except FileNotFoundError:
                    return True
                return False
            try:
                parent_status = os.stat("state", dir_fd=held.descriptor, follow_symlinks=False)
            except FileNotFoundError:
                root_bound()
                if not missing("state", held.descriptor):
                    raise _invalid("archive policy parent appeared during observation")
                return absent
            if not stat.S_ISDIR(parent_status.st_mode):
                raise _invalid("archive policy parent is not an ordinary directory")
            parent_identity = identity(parent_status)
            parent = os.open("state", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                             dir_fd=held.descriptor)
            if identity(os.fstat(parent)) != parent_identity:
                raise _invalid("archive policy parent changed before opening")
            def parents_bound():
                root_bound()
                if (identity(os.fstat(parent)) != parent_identity
                        or identity(os.stat("state", dir_fd=held.descriptor, follow_symlinks=False)) != parent_identity):
                    raise _invalid("archive policy parent changed during observation")
            try:
                before = os.stat(_NAME, dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                parents_bound()
                if not missing(_NAME, parent):
                    raise _invalid("archive policy appeared during observation")
                return absent
            if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
                    or before.st_size > MAX_POLICY_BYTES):
                raise _invalid("archive policy must be a singly linked regular file within its byte bound")
            file_identity = identity(before)
            leaf = os.open(_NAME, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            if identity(os.fstat(leaf)) != file_identity:
                raise _invalid("archive policy changed before its bound read")
            blocks, size = [], 0
            while True:
                block = os.read(leaf, min(65536, before.st_size - size + 1))
                if not block:
                    break
                size += len(block)
                if size > before.st_size:
                    raise _invalid("archive policy grew beyond its captured identity")
                blocks.append(block)
            if (size != before.st_size or identity(os.fstat(leaf)) != file_identity
                    or identity(os.stat(_NAME, dir_fd=parent, follow_symlinks=False)) != file_identity):
                raise _invalid("archive policy changed during its bound read")
            parents_bound()
            value = json.loads(b"".join(blocks).decode("utf-8"), object_pairs_hook=_pairs, parse_constant=_constant)
            result = _policy(value)
            # Parsing grants no exemption from the complete observation fence.
            if (identity(os.fstat(leaf)) != file_identity
                    or identity(os.stat(_NAME, dir_fd=parent, follow_symlinks=False)) != file_identity):
                raise _invalid("archive policy changed before observation completed")
            parents_bound()
            return result
    except (OSError, UnicodeError, ValueError, RecursionError) as exc:
        raise _invalid("archive policy is unavailable or malformed") from exc
    finally:
        if leaf is not None:
            os.close(leaf)
        if parent is not None:
            os.close(parent)


def retention_decision(native_roll_receipt, policy, observed_at):
    """Derive eligibility from immutable native testimony, never file mtimes.

    The receipt is validated through the existing native closed format. This
    function performs no filesystem reads, clock acquisition or mutation, and
    its booleans neither issue retirement authority nor attest payload presence.
    """
    declared = _policy(policy)
    try:
        if (not isinstance(observed_at, datetime) or observed_at.tzinfo is None
                or observed_at.utcoffset() != timedelta(0)):
            raise _clock("archive retention observation must be an explicit aware UTC datetime")
    except (TypeError, ValueError, OverflowError) as exc:
        raise _clock("archive retention observation has an invalid UTC clock") from exc
    tenant = native_roll_receipt.get("tenant_id") if isinstance(native_roll_receipt, dict) else ""
    receipt = validate_record(native_roll_receipt, tenant, frozenset({"bus_epoch_roll_receipt"}), integrity=False)
    stamp = datetime.fromisoformat(receipt["timestamp"].replace("Z", "+00:00"))
    age = observed_at - stamp
    if age < timedelta(0):
        raise _clock("native archive roll timestamp is in the observation's future")
    expired = not declared["keep_forever"] and age >= timedelta(days=30)
    return {"retained": not expired, "expired": expired,
            "keep_forever": declared["keep_forever"], "age_seconds": age.total_seconds()}

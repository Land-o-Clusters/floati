"""Closed ask-cost-v2 snapshot and receipt-scoped attestation lease."""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import re
import stat
import sys
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

from .records import _UUID7


BASENAME = "ask-cost-v2.json"
_INVALID = "puddle_ask_cost_invalid"
_NODE = re.compile(r"[a-z0-9](?:[a-z0-9_-]{0,62}[a-z0-9])?")
_QUESTION = re.compile("ask-question-" + _UUID7)
_ANSWER = re.compile("ask-answer-" + _UUID7)
_DIGEST = re.compile(r"[0-9a-f]{64}")
_MEASUREMENT = re.compile(r"m-[0-9a-f]{32}")
_STAMP = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]{1,6})?Z")
_STAMPS = ("observed_at", "checked_at", "expires_at")
_UNKNOWN_REASONS = frozenset({
    "execution_unbound", "session_conflict", "population_incomplete", "usage_missing",
    "usage_conflict", "source_unsupported", "counter_invalid",
})
_LEASE = timedelta(hours=24)


def session_reference(tenant, receipt_id, acting_session):
    fields = ("floati.ask-session.v2", tenant, receipt_id, acting_session)
    return hashlib.sha256("\0".join(fields).encode("utf-8")).hexdigest()


def _session_ref(row):
    return session_reference(row["tenant_id"], row["id"], row["acting_session_id"])


def stamp(value):
    if not _matches(_STAMP, value):
        raise ValueError("invalid UTC timestamp")
    return datetime.fromisoformat(value[:-1] + "+00:00")


def _matches(pattern, value):
    return isinstance(value, str) and pattern.fullmatch(value) is not None


def _tokens(value):
    return type(value) is int and 0 <= value <= 2**63 - 1


def _usage_valid(usage):
    if not isinstance(usage, dict):
        return False
    basis = usage.get("basis")
    if basis == "MEASURED":
        return (usage.keys() == {"basis", "measurement_id", "total_tokens"}
                and _matches(_MEASUREMENT, usage["measurement_id"])
                and _tokens(usage["total_tokens"]))
    if basis == "DERIVED":
        return (usage.keys() == {"basis", "reason", "lower_tokens", "upper_tokens"}
                and usage["reason"] == "bounded_population"
                and _tokens(usage["lower_tokens"]) and _tokens(usage["upper_tokens"])
                and usage["lower_tokens"] <= usage["upper_tokens"])
    if usage.keys() != {"basis", "reason"} or not isinstance(usage["reason"], str):
        return False
    if basis == "ESTIMATE":
        return usage["reason"] == "assumption_required"
    return basis == "UNKNOWN" and usage["reason"] in _UNKNOWN_REASONS


def _row_times(row):
    fields = {
        "question_id": _QUESTION, "answer_id": _ANSWER, "asking_node": _NODE,
        "answering_node": _NODE, "asking_session_ref": _DIGEST, "answering_session_ref": _DIGEST,
    }
    if (not isinstance(row, dict) or row.keys() != {*fields, *_STAMPS, "usage"}
            or any(not _matches(pattern, row[key]) for key, pattern in fields.items())
            or not _usage_valid(row["usage"])):
        raise ValueError("invalid pair")
    observed, checked, expires = (stamp(row[key]) for key in _STAMPS)
    if not observed <= checked < expires or expires - checked != _LEASE:
        raise ValueError("invalid lease")
    return observed, checked, expires


def _reject_numeric(value):
    raise ValueError("invalid numeric token")


def _depth(value, level=1):
    if isinstance(value, dict):
        children = value.values()
    elif isinstance(value, list):
        children = value
    else:
        return
    if level > 16:
        raise ValueError("excessive JSON nesting")
    for child in children:
        _depth(child, level + 1)


def _integer(token):
    if len(token) > 20:
        raise ValueError("integer outside signed 64-bit range")
    value = int(token)
    if not -(2**63) <= value < 2**63:
        raise ValueError("integer outside signed 64-bit range")
    return value


def evaluate(path, question, answer, evaluated, max_bytes, unique_object):
    """Return fresh evidence or the first applicable wire-order failure."""
    try:
        raw = read_local_snapshot(path, max_bytes)
    except FileNotFoundError:
        return {"reason": "puddle_measured_cost_seam_unavailable"}
    except (OSError, ValueError):
        return {"reason": "puddle_ask_cost_unreadable"}
    try:
        if len(raw) > max_bytes:
            raise ValueError("snapshot too large")
        export = json.loads(raw.decode("utf-8"), object_pairs_hook=unique_object,
                            parse_int=_integer, parse_float=_reject_numeric, parse_constant=_reject_numeric)
        _depth(export)
        if not isinstance(export, dict) or type(export.get("schema_version")) is not int:
            return {"reason": _INVALID}
        if export["schema_version"] not in (1, 2):
            return {"reason": "puddle_ask_cost_version_unsupported"}
        if export["schema_version"] != 2 or export.keys() != {"schema_version", "tenant_id", "records"}:
            return {"reason": _INVALID}
        rows = export["records"]
        if not _matches(_NODE, export["tenant_id"]) or not isinstance(rows, list) or len(rows) > 4096:
            return {"reason": _INVALID}
        matches = []
        questions, answers, measurements = set(), set(), set()
        ambiguous = False
        same_tenant = export["tenant_id"] == question["tenant_id"]
        for row in rows:
            observed, checked, expires = _row_times(row)
            qid, aid = row["question_id"], row["answer_id"]
            mid = row["usage"].get("measurement_id")
            if qid in questions or aid in answers or mid in measurements:
                ambiguous = True
            questions.add(qid)
            answers.add(aid)
            if mid is not None:
                measurements.add(mid)
            if same_tenant and qid == question["id"] and aid == answer["id"]:
                if stamp(answer["timestamp"]) > observed:
                    return {"reason": _INVALID}
                matches.append((row, checked, expires))
    except (ValueError, RecursionError):
        return {"reason": _INVALID}
    if ambiguous:
        return {"reason": "puddle_ask_cost_ambiguous"}
    if not matches:
        return {"reason": "puddle_ask_cost_not_found"}
    row, checked, expires = matches[0]
    if (row["asking_node"] != question["sender"] or row["answering_node"] != answer["sender"]
            or row["asking_session_ref"] != _session_ref(question)
            or row["answering_session_ref"] != _session_ref(answer)):
        return {"reason": "puddle_ask_cost_binding_mismatch"}
    if evaluated < checked:
        return {"reason": "puddle_ask_cost_future"}
    if evaluated >= expires:
        return {"reason": "puddle_ask_cost_stale"}
    usage = row["usage"]
    if usage["basis"] == "MEASURED":
        return {**usage, **{key: row[key] for key in _STAMPS}}
    return dict(usage)


# Linux has no MNT_LOCAL flag. Admit known local filesystem types and leave
# unfamiliar mounts (including network and FUSE implementations) unproved.
_LINUX_LOCAL = {0xEF53, 0x58465342, 0x9123683E, 0x01021994, 0x794C7630, 0x2FC12FC1}


def local_filesystem(fd):
    if sys.platform not in ("darwin", "linux"):
        return False
    libc = ctypes.CDLL(None, use_errno=True)
    # statfs is native ABI data. Both supported ABIs fit in this aligned buffer.
    storage = (ctypes.c_uint64 * 512)()
    if sys.platform == "darwin":
        query = getattr(libc, "fstatfs$INODE64", libc.fstatfs)
        if query(fd, ctypes.byref(storage)) != 0:
            return False
        # struct statfs (64-bit inode ABI), f_flags follows f_owner and f_type.
        flags = ctypes.c_uint32.from_buffer(storage, 64).value
        return bool(flags & 0x1000) and not flags & 0x200000
    return (libc.fstatfs(fd, ctypes.byref(storage)) == 0
            and ctypes.c_ulong.from_buffer(storage).value & 0xFFFFFFFF in _LINUX_LOCAL)


@contextmanager
def private_local_directory(path: Path):
    """Hold the explicitly selected private directory without following links."""
    if path.anchor != "/" or ".." in path.parts:
        raise ValueError("noncanonical export directory")
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_DIRECTORY
    parent = os.open("/", flags)
    try:
        for part in path.parts[1:]:
            following = os.open(part, flags, dir_fd=parent)
            os.close(parent)
            parent = following
            if not local_filesystem(parent):
                raise OSError("nonlocal export path")
        directory = os.fstat(parent)
        if (directory.st_uid != os.geteuid() or stat.S_IMODE(directory.st_mode) != 0o700
                or not local_filesystem(parent)):
            raise OSError("unsafe export directory")
        yield parent
    finally:
        os.close(parent)


def read_local_snapshot(path: Path, max_bytes: int) -> bytes:
    with private_local_directory(path.parent) as parent:
        return read_private_file(parent, path.name, max_bytes)


def read_private_file(parent, name, max_bytes):
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
    fd = os.open(name, flags, dir_fd=parent)
    with os.fdopen(fd, "rb") as stream:
        first = os.fstat(fd)
        if (not stat.S_ISREG(first.st_mode) or first.st_uid != os.geteuid()
                or first.st_mode & 0o077 or first.st_nlink != 1 or not local_filesystem(fd)):
            raise OSError("unsafe export file")
        data = stream.read(max_bytes + 1)
        last = os.fstat(fd)
        if (first.st_size, first.st_mtime_ns) != (last.st_size, last.st_mtime_ns):
            raise OSError("export file modified while reading")
        return data

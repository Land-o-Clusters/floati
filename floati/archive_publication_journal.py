"""Bounded read-only observation of fixed native publication intent.

Intent is DATA, never generation or maintenance authority. This module has no
writer and accepts no caller prefix, pathname selector or decoder callback.
"""
from dataclasses import dataclass
import hashlib
import json
import os
import re
import stat

from . import archive_catalog as catalog
from .errors import IntegrityFailure

PENDING = "receipts/.epoch-history-publication.pending"
STAGE_PREFIX = "receipts/.epoch-history-publication-"
MAX_JOURNAL_BYTES = 256 * 1024
_NONCE = re.compile(r"^[0-9a-f]{32}$")
_HASH = re.compile(r"^[0-9a-f]{64}$")
_FIELDS = frozenset({"schema_version", "tenant_id", "root", "parent", "ledger", "journal",
    "prior", "frame", "nonce", "stage"})


def _invalid(detail):
    return IntegrityFailure("archive_publication_invalid", detail)


@dataclass(frozen=True)
class Observation:
    raw: bytes
    document: dict
    frame: bytes
    row: dict
    identities: object
    aliases: tuple


def _decode(root, raw):
    from .framing import decode_frames, encode_frame
    from .records import validate_record
    from .jsonl import MAX_LEDGER_BYTES, MAX_RECORD_BYTES
    if len(raw) > MAX_JOURNAL_BYTES:
        raise _invalid("publication intent exceeds its fixed byte bound")
    try:
        value = json.loads(raw)
        if (type(value) is not dict or set(value) != _FIELDS
                or catalog._canonical(value) + b"\n" != raw
                or type(value["schema_version"]) is not int or value["schema_version"] != 1
                or value["tenant_id"] != root.tenant_id
                or type(value["nonce"]) is not str or _NONCE.fullmatch(value["nonce"]) is None
                or value["stage"] != STAGE_PREFIX + value["nonce"] + ".stage"):
            raise _invalid("publication intent has a noncanonical or foreign shape")
        for key in ("root", "parent", "ledger", "journal"):
            item = value[key]
            fields = {"device", "inode", "mode"} | ({"path"} if key == "root" else set())
            if (type(item) is not dict or set(item) != fields
                    or any(type(item[name]) is not int or item[name] < 0 for name in ("device", "inode", "mode"))):
                raise _invalid("publication intent object identity is malformed")
        if value["root"]["path"] != str(root.tenant_home):
            raise _invalid("publication intent names another physical root")
        prior = value["prior"]
        if (type(prior) is not dict or set(prior) != {"byte_length", "sha256", "generation"}
                or type(prior["byte_length"]) is not int or not 0 <= prior["byte_length"] <= MAX_LEDGER_BYTES
                or type(prior["sha256"]) is not str or _HASH.fullmatch(prior["sha256"]) is None
                or (prior["generation"] is not None and
                    (type(prior["generation"]) is not str or _HASH.fullmatch(prior["generation"]) is None))
                or type(value["frame"]) is not str):
            raise _invalid("publication prior prefix or frame is malformed")
        frame = value["frame"].encode("utf-8")
        if not 0 < len(frame) <= MAX_RECORD_BYTES:
            raise _invalid("publication proposed frame exceeds its native byte bound")
        rows = decode_frames(frame)
        if len(rows) != 1 or encode_frame(rows[0]) != frame:
            raise _invalid("publication proposed frame is not exactly canonical")
        row = validate_record(rows[0], root.tenant_id, frozenset({catalog.KIND}), integrity=True)
        if row["previous_generation"] != prior["generation"]:
            raise _invalid("publication proposed frame has another predecessor")
        return value, frame, row
    except (ValueError, TypeError, KeyError, UnicodeError, RecursionError) as failure:
        raise _invalid("publication intent cannot be decoded as bounded native data") from failure


def observe(root, *, _expected_identities=None):
    """Read actual fixed pending and its one derived alias without enumeration."""
    expected = (catalog._capture_identities(root, ("receipts",)) if _expected_identities is None
        else catalog._capture_map(_expected_identities))
    identities = dict(expected)
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK
    with catalog._opened_capture(root, "receipts", expected, directory=True) as (parent, parent_status, _seen):
        try:
            status = os.stat(PENDING.split("/")[-1], dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            return None
        if (not stat.S_ISREG(status.st_mode) or status.st_nlink not in {1, 2}
                or status.st_size > MAX_JOURNAL_BYTES):
            raise _invalid("pending intent is aliased, special or over its byte bound")
        identity = catalog._identity(status)
        if PENDING in expected and expected[PENDING] != identity:
            raise _invalid("pending intent changed from its retained observation")
        descriptor = os.open(PENDING.split("/")[-1], flags, dir_fd=parent)
        try:
            if catalog._identity(os.fstat(descriptor)) != identity:
                raise _invalid("pending intent changed while opening")
            blocks, total = [], 0
            while True:
                block = os.read(descriptor, min(65536, status.st_size + 1 - total))
                if not block:
                    break
                total += len(block)
                if total > status.st_size:
                    raise _invalid("pending intent grew during bounded read")
                blocks.append(block)
            if total != status.st_size:
                raise _invalid("pending intent was truncated during read")
            raw = b"".join(blocks)
            value, frame, row = _decode(root, raw)
            if tuple(value["journal"][name] for name in ("device", "inode", "mode")) != identity[:3]:
                raise _invalid("pending intent is not its originally created native journal inode")
            root_identity, parent_identity = expected["."], catalog._identity(parent_status)
            for key, actual in (("root", root_identity), ("parent", parent_identity)):
                if tuple(value[key][name] for name in ("device", "inode", "mode")) != actual[:3]:
                    raise _invalid("publication intent directory was replaced")
            stage = value["stage"]
            try:
                stage_status = os.stat(stage.split("/")[-1], dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                stage_status = None
            if stage_status is None:
                if status.st_nlink != 1:
                    raise _invalid("publication intent has an unexplained alias")
                aliases = (PENDING,)
            else:
                if status.st_nlink != 2 or catalog._identity(stage_status) != identity:
                    raise _invalid("publication stage is not the exact second pending alias")
                if stage in expected and expected[stage] != identity:
                    raise _invalid("publication stage changed from its retained observation")
                stage_fd = os.open(stage.split("/")[-1], flags, dir_fd=parent)
                try:
                    if catalog._identity(os.fstat(stage_fd)) != identity:
                        raise _invalid("publication stage changed while opening")
                finally:
                    os.close(stage_fd)
                identities[stage] = identity
                aliases = (stage, PENDING)
            if (catalog._identity(os.fstat(descriptor)) != identity
                    or catalog._identity(os.stat(PENDING.split("/")[-1], dir_fd=parent, follow_symlinks=False)) != identity):
                raise _invalid("pending intent changed during observation")
            identities[PENDING] = identity
            return Observation(raw, value, frame, row, catalog._capture_map(identities), aliases)
        finally:
            os.close(descriptor)


def classify(root, raw, ledger_status, observation):
    """Classify exact actual ledger bytes; callers independently verify chain."""
    value = observation.document
    if (not stat.S_ISREG(ledger_status.st_mode) or ledger_status.st_nlink != 1
            or tuple(value["ledger"][name] for name in ("device", "inode", "mode")) != catalog._identity(ledger_status)[:3]):
        raise _invalid("publication ledger inode/type/link identity changed")
    length = value["prior"]["byte_length"]
    prefix, tail = raw[:length], raw[length:]
    if len(prefix) != length or hashlib.sha256(prefix).hexdigest() != value["prior"]["sha256"]:
        raise _invalid("publication original ledger prefix changed")
    if tail == observation.frame:
        return "committed", prefix
    if observation.frame.startswith(tail):
        return ("unchanged" if not tail else "rolled_back"), prefix
    raise _invalid("publication suffix is outside its exact owned frame prefix")


def assert_bound(root, observation):
    """Recheck actual immutable intent aliases; observation supplies no reader."""
    current = observe(root, _expected_identities=observation.identities)
    if (current is None or current.raw != observation.raw
            or current.aliases != observation.aliases
            or dict(current.identities) != dict(observation.identities)):
        raise _invalid("publication intent changed from its retained physical observation")

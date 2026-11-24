"""One disposable event index with authenticated per-recipient membership.

Every source byte participates in the checkpoint chain. A complete native
event replay certifies a rebuild; selected reads never turn missing cache
evidence into message absence. Native send/retract writers advance the same
artifact while they retain the event-ledger lock.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from . import jsonl, records, send_prefix
from .bus_epoch import epoch_guard
from .errors import IntegrityFailure
from .key_prefix import KeyPrefix


CACHE_NAME = ".inbox-prefix"
RELATIVE = Path("events.jsonl")
FORMAT = "inbox-prefix-v1"
FIRST = "physical"


def storage_declaration():
    """Expose the native index writer's roles without opening its bank."""
    return KeyPrefix.storage_declaration(relative=RELATIVE, cache_name=CACHE_NAME)


def recipient_key(recipient, ordinal):
    return json.dumps([recipient, ordinal], ensure_ascii=True, separators=(",", ":"))


class InboxPrefix(KeyPrefix):
    def __init__(self, root, *, read_only=False, source_revision=None):
        from .events import EVENT_KINDS
        super().__init__(root, EVENT_KINDS, relative=RELATIVE,
            cache_name=CACHE_NAME, format=FORMAT, folds={"teardown": 1},
            revision={"source": send_prefix._revision() if source_revision is None else source_revision,
                      "reader_version": records.READER_VERSION},
            read_only=read_only)
        self._next = {}

    def rebuild(self):
        from .events import validate_event_records
        before = send_prefix._identity(self.path)
        unknown = {}
        rows = jsonl._read_path_records(self.path, self.root.tenant_id,
            self.kinds, unrecognized=unknown)
        validate_event_records(rows)
        if unknown:
            # The compatible native reader still speaks about known rows;
            # this strict KeyPrefix format cannot certify future frames.
            raise send_prefix.CacheMiss()
        data = self.path.read_bytes() if before is not None else b""
        if before != send_prefix._identity(self.path):
            raise IntegrityFailure("ledger_identity_changed_during_read",
                "events changed during inbox prefix validation")
        checked = jsonl._decode_path_records(self.path, self.root.tenant_id,
            self.kinds, data)
        validate_event_records(checked)
        entries, counts = {}, {}
        if rows:
            entries[send_prefix._key("first", FIRST)] = rows[0]
        for position, row in enumerate(rows, 1):
            if row["kind"] == "message_envelope":
                node = row["recipient"]
                ordinal = counts.get(node, 0)
                entries[send_prefix._key("recipient", recipient_key(node, ordinal))] = row
                entries[send_prefix._key("message", row["id"])] = [position, row]
                counts[node] = ordinal + 1
            elif row["kind"] == "message_retracted":
                entries[send_prefix._key("retracted", row["retracted_message_id"])] = row
        from .seat_teardown_prefix import advance_event_ids
        def put(domain, key, value):
            entries[send_prefix._key(domain, key)] = value
        self._rebuilt_teardown = advance_event_ids(rows, None, 0, put)
        self.rebuild_entries(rows, entries, before, data)

    def _fold(self, rows, prior):
        if not prior:
            return {"teardown": self._rebuilt_teardown}
        return prior

    def load(self, *, previous=None):
        from .seat_teardown_prefix import validate_event_fold
        super().load(previous=previous)
        validate_event_fold(self)

    def positioned_message(self, identity):
        value = super().get("message", identity)
        if value is not None:
            if (type(value) is not list or len(value) != 2 or type(value[0]) is not int
                    or not 1 <= value[0] <= self.payload["count"] or type(value[1]) is not dict
                    or value[1].get("kind") != "message_envelope" or value[1].get("id") != identity):
                raise send_prefix.CacheMiss()
        return value

    def get(self, domain, key):
        if domain == "message":
            value = self.positioned_message(key)
            return None if value is None else value[1]
        return super().get(domain, key)

    def recipient_count(self, recipient):
        # Recipient ordinal members are immutable and contiguous. Find a
        # local absence bound before binary search so unrelated recipients'
        # append history adds no lookups. The native physical count is only
        # a safety bound; no mutable per-recipient count is cached.
        maximum = self.payload["count"]
        low, high = 0, 1
        while self.get("recipient", recipient_key(recipient, high)) is not None:
            if high >= maximum:
                raise send_prefix.CacheMiss()
            low, high = high + 1, 2 * high + 1
        while low < high:
            middle = (low + high) // 2
            if self.get("recipient", recipient_key(recipient, middle)) is None:
                high = middle
            else:
                low = middle + 1
        if self.get("recipient", recipient_key(recipient, low)) is not None:
            raise send_prefix.CacheMiss()
        return low

    def prepare(self, batch):
        self._next = {row["recipient"]: self.recipient_count(row["recipient"])
            for row in batch if row["kind"] == "message_envelope"}
        self._assert_current()

    def _advance(self, batch, encoded):
        if not self.payload["count"] and batch:
            self.set("first", FIRST, batch[0])
        from .seat_teardown_prefix import advance_event_ids
        self.payload["folds"]["teardown"] = advance_event_ids(batch,
            self.payload["folds"]["teardown"], self.payload["count"], self.set)
        for position, row in enumerate(batch, self.payload["count"] + 1):
            if row["kind"] == "message_envelope":
                node = row["recipient"]
                ordinal = self._next[node]
                self.set("recipient", recipient_key(node, ordinal), row)
                self.set("message", row["id"], [position, row])
                self._next[node] = ordinal + 1
            elif row["kind"] == "message_retracted":
                self.set("retracted", row["retracted_message_id"], row)
        for frame in encoded:
            self.payload["head"] = hashlib.sha256(bytes.fromhex(self.payload["head"]) + frame).hexdigest()
        self.payload["count"] += len(batch)


def _open(root, *, create=True, read_only=False):
    bank = root.tenant_home / CACHE_NAME
    if (not create or read_only) and not (bank.exists() or bank.is_symlink()):
        return None
    try:
        prefix = InboxPrefix(root, read_only=read_only)
        try:
            prefix.load()
        except send_prefix._CACHE_ERRORS:
            if read_only:
                return None
            prefix.rebuild()
        return prefix
    except send_prefix._CACHE_ERRORS:
        return None


def _read(root, select, *, read_only=False):
    """Run one selected read under the same source lock as native appends."""
    path, _tenant = jsonl._resolve(root, RELATIVE, write=False)
    lock, lock_relative = jsonl._lock_beside(path, RELATIVE)
    with epoch_guard(root), jsonl._locked_path(lock, exclusive=not read_only, relative=lock_relative):
        prefix = _open(root, read_only=read_only)
        for attempt in range(2):
            if prefix is None:
                break
            try:
                rows = select(prefix)
                prefix._assert_current()
                return rows
            except send_prefix._CACHE_ERRORS:
                if attempt == 0 and not read_only:
                    try:
                        prefix.rebuild()
                    except send_prefix._CACHE_ERRORS:
                        prefix = None
                else:
                    prefix = None
    # Release the ledger lock before taking the native sealed snapshot;
    # acquiring a second flock descriptor is not a reentrant read.
    # Compatible future kinds remain readable without a strict bank seal.
    from .events import EventLog
    return EventLog(root).event_records(sealed=True)


def _selected(prefix, message_ids):
    first = prefix.get("first", FIRST)
    rows = [] if first is None else [first]
    seen = {row["id"] for row in rows}
    for message_id in message_ids:
        for domain in ("message", "retracted"):
            row = prefix.get(domain, message_id)
            if row is not None and row["id"] not in seen:
                rows.append(row)
                seen.add(row["id"])
    return rows


def selected_frames(root, message_ids):
    """Exact current envelopes/retractions and physical head for consume.

    IDs are not recipient-filtered: a foreign envelope must still produce
    the native recipient mismatch, rather than becoming an unknown message.
    """
    return _read(root, lambda prefix: _selected(prefix, message_ids))


def recipient_frames(root, recipient, *, read_only=False):
    """Enumerate only this recipient's native envelope order and retractions."""
    def select(prefix):
        messages = []
        for ordinal in range(prefix.recipient_count(recipient)):
            row = prefix.get("recipient", recipient_key(recipient, ordinal))
            if row is None or row.get("kind") != "message_envelope" or row.get("recipient") != recipient:
                raise send_prefix.CacheMiss()
            messages.append(row)
        return _selected(prefix, [row["id"] for row in messages])
    return _read(root, select, read_only=read_only)


def prepare_append(root, relative, kinds, max_bytes, batch, *, prior=None):
    """Prepare only an existing bank under the native event writer lock."""
    from .events import EVENT_KINDS, validate_event_records
    if (Path(relative) != RELATIVE or kinds != frozenset(EVENT_KINDS)
            or max_bytes != jsonl.MAX_RECORD_BYTES):
        return None
    if prior is not None:
        try:
            validate_event_records([*prior, *batch])
        except IntegrityFailure:
            # Generic JSONL authoring did not previously add event-join
            # refusals. Leave its bank stale; a later read must replay and
            # report the native integrity failure instead of sealing it.
            return None
    prefix = _open(root, create=False)
    if prefix is not None:
        try:
            prefix.prepare(batch)
        except send_prefix._CACHE_ERRORS:
            return None
    return prefix


def advance(prefix, batch, encoded, identity):
    if prefix is not None:
        try:
            prefix.appended(batch, encoded, identity)
        except send_prefix._CACHE_ERRORS:
            pass  # Native fsync succeeded; the old identity forces recovery.


def warm(root):
    """Certify a copied event inode outside the measured child."""
    path, _tenant = jsonl._resolve(root, RELATIVE, write=False)
    lock, lock_relative = jsonl._lock_beside(path, RELATIVE)
    with epoch_guard(root), jsonl._locked_path(lock, exclusive=True, relative=lock_relative):
        prefix = _open(root)
        return None if prefix is None else {"identity": prefix.payload["identity"],
            "count": prefix.payload["count"], "head": prefix.payload["head"]}

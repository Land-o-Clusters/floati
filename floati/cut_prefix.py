"""Certified raw-domain event cuts, disposable and advanced by native writers."""
from __future__ import annotations

import hashlib
from pathlib import Path

from . import jsonl, records, send_prefix
from .bus_epoch import epoch_guard
from .errors import IntegrityFailure
from .key_prefix import KeyPrefix
from .sha256_midstate import SHA256

CACHE_NAME_PREFIX = ".cut-prefix-"
RELATIVE = Path("events.jsonl")
FORMAT = "cut-prefix-v1"
_UNSET = object()
_CERTIFIED = object()


def cache_name(relative=RELATIVE):
    path = Path(relative)
    if path.is_absolute() or ".." in path.parts or path.as_posix() != str(relative):
        raise send_prefix.CacheMiss()
    return CACHE_NAME_PREFIX + hashlib.sha256(path.as_posix().encode("utf-8")).hexdigest()


def _domain():
    from .wake_hold import WakeHoldController
    return WakeHoldController._EVENT_DOMAIN


class CutPrefix(KeyPrefix):
    def __init__(self, root, relative=RELATIVE, domain=None, *, read_only=False, source_revision=None):
        from .events import EVENT_KINDS
        self.domain = _domain() if domain is None else domain
        self.domain_bytes = self.domain.encode("ascii") + b"\0"
        super().__init__(root, EVENT_KINDS, relative=relative,
            cache_name=cache_name(relative), format=FORMAT, folds={"cut": 1},
            revision={"source": send_prefix._revision() if source_revision is None else source_revision,
                      "reader_version": records.READER_VERSION},
            read_only=read_only)
        self.binding["domain"] = self.domain

    def _check_folds(self, folds):
        super()._check_folds(folds)
        cut = folds["cut"]
        if (type(cut) is not dict or set(cut) != {"digest", "count", "length", "state", "tail"}
                or type(cut["count"]) is not int or not 0 <= cut["count"] <= jsonl.MAX_LEDGER_RECORDS
                or type(cut["state"]) is not list or type(cut["tail"]) is not str
                or type(cut["digest"]) is not str or send_prefix._HEX.fullmatch(cut["digest"]) is None):
            raise send_prefix.CacheMiss()
        try:
            tail = bytes.fromhex(cut["tail"])
            if tail.hex() != cut["tail"]:
                raise ValueError()
            digest = SHA256(state=cut["state"], length=cut["length"], tail=tail)
            if digest.hexdigest() != cut["digest"]:
                raise ValueError()
        except (ValueError, TypeError, OverflowError) as error:
            raise send_prefix.CacheMiss() from error

    @staticmethod
    def _state(digest, count):
        return {"digest": digest.hexdigest(), "count": count, "length": digest.length,
            "state": list(digest.state), "tail": digest.tail.hex()}

    def _fold(self, rows, prior):
        if not prior:
            return {"cut": self._rebuilt_fold}
        return prior  # _advance has already fed the exact physical frames.

    def load(self, *, previous=None):
        super().load(previous=previous)
        cut = self.payload["folds"]["cut"]
        if (cut["count"] != self.payload["count"]
                or cut["length"] != len(self.domain_bytes) + self.payload["offset"]):
            raise send_prefix.CacheMiss()
        length = cut["length"] % 64
        # Authenticate the live uncompressed tail as well as its persisted
        # midstate. No full source replay occurs in this read-only admission.
        amount = min(length, self.payload["offset"])
        tail = b""
        if amount:
            with self.path.open("rb") as stream:
                stream.seek(-amount, 2)
                tail = stream.read(amount)
        if length > amount:
            tail = self.domain_bytes[-(length - amount):] + tail
        if tail.hex() != cut["tail"]:
            raise send_prefix.CacheMiss()
        self._assert_current()

    def rebuild(self):
        from .events import validate_event_records
        from .framing import encode_frame
        before = send_prefix._identity(self.path)
        unknown = {}
        rows = jsonl._read_path_records(self.path, self.root.tenant_id, self.kinds, unrecognized=unknown)
        validate_event_records(rows)
        if unknown:
            raise send_prefix.CacheMiss()
        data = b"" if before is None else self.path.read_bytes()
        if before != send_prefix._identity(self.path):
            raise IntegrityFailure("ledger_identity_changed_during_read", "events changed during cut validation")
        if b"".join(encode_frame(row) for row in rows) != data:
            raise send_prefix.CacheMiss()
        fast, digest, entries = hashlib.sha256(self.domain_bytes), SHA256().update(self.domain_bytes), {}
        for ordinal, frame in enumerate(data.splitlines(keepends=True), 1):
            fast.update(frame)
            digest.update(frame)
            entries[send_prefix._key("cut", fast.hexdigest())] = ordinal
        self._rebuilt_fold = self._state(digest, len(rows))
        if fast.hexdigest() != self._rebuilt_fold["digest"]:
            raise send_prefix.CacheMiss()
        self.rebuild_entries(rows, entries, before, data)

    def _advance(self, batch, encoded):
        cut = self.payload["folds"]["cut"]
        digest = SHA256(state=cut["state"], length=cut["length"], tail=bytes.fromhex(cut["tail"]))
        if digest.hexdigest() != cut["digest"]:
            raise send_prefix.CacheMiss()
        for ordinal, frame in enumerate(encoded, self.payload["count"] + 1):
            digest.update(frame)
            self.set("cut", digest.hexdigest(), ordinal)
            self.payload["head"] = hashlib.sha256(bytes.fromhex(self.payload["head"]) + frame).hexdigest()
        self.payload["count"] += len(batch)
        self.payload["folds"] = {"cut": self._state(digest, self.payload["count"])}


def _open(root, *, create=False, read_only=False):
    bank = root.tenant_home / cache_name()
    if (not create or read_only) and not (bank.exists() or bank.is_symlink()):
        return None
    try:
        prefix = CutPrefix(root, read_only=read_only)
        try:
            prefix.load()
        except send_prefix._CACHE_ERRORS:
            if read_only:
                return None
            prefix.rebuild()
        return prefix
    except send_prefix._CACHE_ERRORS:
        return None


def prepare_append(root, relative, kinds, max_bytes, batch):
    from .events import EVENT_KINDS
    if (Path(relative) != RELATIVE or kinds != frozenset(EVENT_KINDS)
            or max_bytes != jsonl.MAX_RECORD_BYTES or any(row["kind"] not in EVENT_KINDS for row in batch)):
        return None
    return _open(root)


def advance(prefix, batch, encoded, identity):
    if prefix is not None:
        try:
            prefix.appended(batch, encoded, identity)
        except send_prefix._CACHE_ERRORS:
            pass  # The durable source won; old identity can never authorize it.


def warm(root, *, expected_identity=_UNSET):
    path = root.resolve_relative(RELATIVE)
    lock, coordinate = jsonl._lock_beside(path, RELATIVE)
    with epoch_guard(root), jsonl._locked_path(lock, exclusive=True, relative=coordinate):
        if expected_identity is not _UNSET and expected_identity != send_prefix._identity(path):
            return None
        prefix = _open(root, create=True)
        return None if prefix is None else {key: prefix.payload[key] for key in ("identity", "count", "head")}


class CertifiedEventSource:
    """Detached selected facts from three banks at one validated native cut."""
    def __init__(self, *, root, prefix, inbox, send, first, matching, cuts, count,
                 current_digest, recipient, worker_session_id, seal):
        if seal is not _CERTIFIED:
            raise send_prefix.CacheMiss()
        self.root, self.prefix, self.inbox, self.send = root, prefix, inbox, send
        self.first, self.rows, self.cuts = first, tuple(matching), dict(cuts)
        self.count, self.current_digest = count, current_digest
        self.recipient, self.worker_session_id = recipient, worker_session_id
        self.identity = prefix.payload["identity"]

    def __len__(self):
        return self.count

    def __getitem__(self, index):
        if index == 0 and self.count:
            return self.first
        raise IndexError(index)

    @property
    def first_kind(self):
        return None if self.first is None else self.first["kind"]

    def matching(self, node, plane):
        if node != self.recipient or plane != self.worker_session_id:
            raise send_prefix.CacheMiss()
        return self.rows

    def cut_position(self, digest):
        position = self.cuts.get(digest)
        return position if position is not None and position <= self.count else None

    def prepare_cuts(self, deliveries, *, presentations=()):
        from itertools import chain
        holds = (row.get("event_prefix_digest") for row in deliveries
            if row.get("kind") == "wake_hold_receipt")
        for digest in chain(holds, presentations):
            if type(digest) is not str or send_prefix._HEX.fullmatch(digest) is None:
                continue  # The native receipt validator owns its exact refusal.
            if digest not in self.cuts:
                position = self.prefix.get("cut", digest)
                if position is not None and (type(position) is not int or not 1 <= position <= self.prefix.payload["count"]):
                    raise send_prefix.CacheMiss()
                self.cuts[digest] = position
        self.assert_current()

    def assert_current(self):
        self.prefix._assert_current()
        self.inbox._assert_current()
        self.send.load()
        if any(bank.payload["identity"] != self.identity for bank in (self.prefix, self.inbox, self.send)):
            raise send_prefix.CacheMiss()

    def restricted(self, count, digest):
        if self.cut_position(digest) != count:
            raise send_prefix.CacheMiss()
        if any(retracted for _ordinal, _row, retracted in self.rows):
            # The selected bank proves current retraction, not its historical
            # ordinal. Such retries take native replay before projection.
            raise send_prefix.CacheMiss()
        return CertifiedEventSource(root=self.root, prefix=self.prefix, inbox=self.inbox,
            send=self.send, first=self.first if count else None,
            matching=[item for item in self.rows if item[0] <= count], cuts=self.cuts,
            count=count, current_digest=digest, recipient=self.recipient,
            worker_session_id=self.worker_session_id, seal=_CERTIFIED)


class CertifiedPrefixes:
    def __init__(self, source):
        self.source = source

    def __len__(self):
        return self.source.count + 1

    def __getitem__(self, index):
        if index == -1 or index == self.source.count:
            return self.source.current_digest
        if index == 0:
            return hashlib.sha256(self.source.prefix.domain_bytes).hexdigest()
        raise IndexError(index)

    def index(self, digest):
        value = self.source.cut_position(digest)
        if value is None:
            raise ValueError(digest)
        return value


def certified_source(root, recipient, worker_session_id=None, *, previous=None, _open_handle=None):
    """Read only current authenticated banks; uncertainty is native fallback."""
    from .events import EVENT_KINDS
    from .inbox_prefix import InboxPrefix, FIRST, recipient_key
    if type(previous) is not CertifiedEventSource or previous.root is not root or previous.recipient != recipient:
        previous = None
    try:
        if previous is not None and previous.worker_session_id == worker_session_id:
            previous.assert_current()
            return previous
    except send_prefix._CACHE_ERRORS:
        previous = None
    path = root.resolve_relative(RELATIVE)
    lock, coordinate = jsonl._lock_beside(path, RELATIVE)
    try:
        # A plane change can borrow the last acquisition's reader revision and
        # directory bytes. The new checkpoints, HMAC keys, selected rows and
        # final implementation fingerprint are still read for this plane.
        revision = send_prefix._revision() if previous is None else previous.send.binding["revision"]
        with epoch_guard(root), jsonl._locked_path(lock, exclusive=False, relative=coordinate,
                _open_handle=_open_handle):
            prefix = CutPrefix(root, read_only=True, source_revision=revision)
            prefix.load(previous=previous.prefix if previous is not None else None)
            inbox = InboxPrefix(root, read_only=True, source_revision=revision)
            inbox.load(previous=previous.inbox if previous is not None else None)
            send = send_prefix.Prefix(root, EVENT_KINDS, read_only=True, revision=revision)
            send.load()
        identity = prefix.payload["identity"]
        if (any(bank.payload["identity"] != identity for bank in (inbox, send))
                or any(bank.payload["count"] != prefix.payload["count"] for bank in (inbox, send))):
            raise send_prefix.CacheMiss()
        matching = []
        with inbox._scope():
            first = inbox.get("first", FIRST)
            if first is not None and first["kind"] == "bus_epoch_roll_receipt":
                return None
            if bool(first) != bool(prefix.payload["count"]):
                raise send_prefix.CacheMiss()
            for index in range(inbox.recipient_count(recipient)):
                row = inbox.get("recipient", recipient_key(recipient, index))
                if (type(row) is not dict or row.get("kind") != "message_envelope"
                        or row.get("recipient") != recipient):
                    raise send_prefix.CacheMiss()
                if row.get("worker_session_id") != worker_session_id:
                    continue
                ordinal = send.get("mail-ordinal-v1", row["id"])
                if type(ordinal) is not int or not 0 <= ordinal < prefix.payload["count"]:
                    raise send_prefix.CacheMiss()
                retraction = inbox.get("retracted", row["id"])
                if retraction is not None and (retraction.get("kind") != "message_retracted"
                        or retraction.get("retracted_message_id") != row["id"]):
                    raise send_prefix.CacheMiss()
                matching.append((ordinal + 1, row, retraction is not None))
        if len({ordinal for ordinal, _row, _retracted in matching}) != len(matching):
            raise send_prefix.CacheMiss()
        digest = prefix.payload["folds"]["cut"]["digest"]
        source = CertifiedEventSource(root=root, prefix=prefix, inbox=inbox, send=send,
            first=first, matching=sorted(matching, key=lambda item: item[0]),
            cuts={hashlib.sha256(prefix.domain_bytes).hexdigest(): 0, digest: prefix.payload["count"]},
            count=prefix.payload["count"], current_digest=digest,
            recipient=recipient, worker_session_id=worker_session_id, seal=_CERTIFIED)
        if send_prefix._revision() != revision:
            raise send_prefix.CacheMiss()
        source.assert_current()
        return source
    except send_prefix._CACHE_ERRORS:
        if previous is not None:
            # A different plane's reader revision may predate rebuilt banks.
            # Acquire once without any borrowed facts before native fallback.
            return certified_source(root, recipient, worker_session_id, _open_handle=_open_handle)
        return None

"""Disposable authenticated index of one fully validated event prefix.

Native send admission and selected ACK references consume this cache. Full projections and epoch
archives continue to read their authoritative ledgers. All storage I/O uses
observable Python reads; missing proof is a full replay, never an absent key.
"""
from __future__ import annotations

from contextlib import contextmanager
import copy
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
import uuid
from weakref import WeakKeyDictionary

from . import jsonl
from .errors import IntegrityFailure, ProtocolRefusal
from .framing import decode_frames, encode_frame


_HEX = re.compile(r"[0-9a-f]{64}")
_FIELDS = ("st_dev", "st_ino", "st_mode", "st_nlink", "st_size", "st_mtime_ns", "st_ctime_ns")
_FORMAT = "send-prefix-v5"
_GENERATION = re.compile(r"[0-9a-f]{32}\Z")
_EMPTY_HEAD = hashlib.sha256(b"floati-send-prefix-v1\0").hexdigest()
CACHE_NAME = ".send-prefix"


class CacheMiss(Exception):
    """Private optimization refusal; native validation remains authoritative."""


def _bytes(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")


def _identity(path):
    try:
        value = path.stat()
    except FileNotFoundError:
        return None
    return [getattr(value, field) for field in _FIELDS]


def _directory(path, *, create=True):
    if create:
        try:
            path.mkdir(mode=0o700)
        except FileExistsError:
            pass
    value = path.lstat()
    if not stat.S_ISDIR(value.st_mode) or value.st_uid != os.getuid() or value.st_mode & 0o077:
        raise CacheMiss()


def _read(path, maximum):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        value = os.fstat(descriptor)
        if (not stat.S_ISREG(value.st_mode) or value.st_uid != os.getuid()
                or value.st_mode & 0o077 or value.st_nlink != 1 or value.st_size > maximum):
            raise CacheMiss()
        data = b""
        while len(data) <= maximum:
            part = os.read(descriptor, min(65536, maximum + 1 - len(data)))
            if not part:
                break
            data += part
        if len(data) > maximum:
            raise CacheMiss()
        return data
    finally:
        os.close(descriptor)


def _atomic(path, data, *, durable=True):
    temporary = path.with_name(".pending-" + uuid.uuid4().hex)
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            if durable:
                os.fsync(stream.fileno())
        os.replace(temporary, path)
        if durable:
            parent = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(parent)
            finally:
                os.close(parent)
    finally:
        temporary.unlink(missing_ok=True)


def _revision():
    # Stat every shipped Python dependency and schema, not only the public
    # READER_VERSION. Updating a helper invalidates prior semantic approval.
    # This metadata work depends on the implementation, never ledger history.
    package = Path(__file__).resolve().parent
    paths = sorted(package.glob("*.py")) + sorted((package.parent / "schemas").rglob("*.json"))
    paths += [package.parent / ".floati-installed-reader.json",
        package.parent / ".floati-install" / "manifest.v0.json"]
    return hashlib.sha256(_bytes([sys.implementation.cache_tag,
        [(str(path), _identity(path)) for path in paths]])).hexdigest()


def _key(domain, value):
    digest = hashlib.sha256(domain.encode("ascii") + b"\0" + value.encode("utf-8")).digest()
    return "".join(format(byte, "08b") for byte in digest)


class Prefix:
    def __init__(self, root, kinds, *, revision=None, read_only=False):
        self.read_only = read_only
        self.root, self.kinds = root, frozenset(kinds)
        self.path = root.resolve_relative("events.jsonl")
        self.home = root.tenant_home / CACHE_NAME
        _directory(self.home, create=not read_only)
        self.nodes = self.home / "nodes"
        _directory(self.nodes, create=not read_only)
        self.bank = self.home / "bank.bin"
        self.staging = self.home / "staging"
        _directory(self.staging, create=not read_only)
        key_path = self.home / "seal.key"
        try:
            self.secret = _read(key_path, 32)
        except FileNotFoundError:
            if read_only:
                raise CacheMiss()
            self.secret = os.urandom(32)
            _atomic(key_path, self.secret)
        if len(self.secret) != 32:
            raise CacheMiss()
        self.binding = {"format": _FORMAT, "root": str(root.tenant_home),
            "tenant": root.tenant_id, "kinds": sorted(kinds),
            "maximum": jsonl.MAX_RECORD_BYTES, "ledger_bytes": jsonl.MAX_LEDGER_BYTES,
            "ledger_records": jsonl.MAX_LEDGER_RECORDS,
            "revision": _revision() if revision is None else revision}
        self.memo = {}
        self.payload = None
        self._rebuilding = False
        self._bank_data = None
        self._staged_data = None

    def _node(self, digest):
        if not isinstance(digest, str) or _HEX.fullmatch(digest) is None:
            raise CacheMiss()
        if digest not in self.memo:
            try:
                data = _read(self.nodes / digest, 5 * jsonl.MAX_RECORD_BYTES)
            except FileNotFoundError:
                from .send_bank import read
                data = read(self.bank, digest)
            if hashlib.sha256(data).hexdigest() != digest:
                raise CacheMiss()
            self.memo[digest] = json.loads(data)
        return self.memo[digest]

    def _put(self, node):
        if getattr(self, "read_only", False):
            raise CacheMiss()
        data = _bytes(node)
        digest = hashlib.sha256(data).hexdigest()
        if self._rebuilding:
            self._bank_data[digest] = data
            return digest
        if self._staged_data is not None:
            # Retain every changed-path output, even a memo hit: a concurrent
            # same-identity rebuild may prune an uncommitted historical node.
            self._staged_data[digest] = data
            self.memo[digest] = node
            return digest
        if digest in self.memo:
            return digest
        # Replace even an existing hostile file, without opening its target.
        _atomic(self.nodes / digest, data, durable=False)
        self.memo[digest] = node
        return digest

    def get(self, domain, value):
        if "pages" in self.payload or "filter" in self.payload:
            added = self._get_trie(domain, value)
            if added is not None:
                return added
            if "pages" in self.payload:
                from .send_prepare import lookup
            else:
                from .send_unprepared import lookup
            result = lookup(self, domain, value)
            identity = getattr(self, "_appending_to", None) or self.payload["identity"]
            if identity != _identity(self.path):
                raise CacheMiss()
            return result
        return self._get_trie(domain, value)

    def _get_trie(self, domain, value):
        key = _key(domain, value)
        digest = self.payload["trie"]
        previous = -1
        while digest is not None:
            node = self._node(digest)
            prefix = node["prefix"]
            if not isinstance(prefix, str) or len(prefix) <= previous or len(prefix) > 256:
                raise CacheMiss()
            previous = len(prefix)
            if not key.startswith(prefix):
                return None
            if "value" in node:
                if prefix != key:
                    raise CacheMiss()
                return self._node(node["value"])
            digest = node["children"].get(key[len(prefix)])
        return None

    def _insert(self, digest, key, value):
        if digest is None:
            return self._put({"prefix": key, "value": value})
        node = self._node(digest)
        prefix = node["prefix"]
        common = 0
        while common < min(len(prefix), len(key)) and prefix[common] == key[common]:
            common += 1
        if common < len(prefix):
            leaf = self._put({"prefix": key, "value": value})
            return self._put({"prefix": key[:common], "children": {prefix[common]: digest, key[common]: leaf}})
        if prefix == key:
            return self._put({"prefix": key, "value": value})
        children = dict(node["children"])
        digit = key[len(prefix)]
        children[digit] = self._insert(children.get(digit), key, value)
        return self._put({"prefix": prefix, "children": children})

    def set(self, domain, value, rows):
        self.payload["trie"] = self._insert(self.payload["trie"], _key(domain, value), self._put(rows))

    def load(self):
        value = json.loads(_read(self.home / "checkpoint.json", 16384))
        payload = value["payload"]
        seal = hmac.new(self.secret, _bytes(payload), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(seal, value["seal"]):
            raise CacheMiss()
        if (payload["binding"] != self.binding or payload["identity"] != _identity(self.path)
                or payload["offset"] != (0 if payload["identity"] is None else payload["identity"][4])
                or type(payload.get("mail_base_count")) is not int
                or not 0 <= payload["mail_base_count"] <= payload["count"]
                or not isinstance(payload.get("generation"), str)
                or _GENERATION.fullmatch(payload["generation"]) is None):
            raise CacheMiss()
        self.payload = payload

    def publish(self):
        if getattr(self, "read_only", False):
            raise CacheMiss()
        payload = self.payload
        value = {"payload": payload,
            "seal": hmac.new(self.secret, _bytes(payload), hashlib.sha256).hexdigest()}
        # Proof nodes are disposable and content authenticated. A lost node
        # requires native recovery; syncing the history-sized bank is not a
        # ledger durability barrier. Keep the checkpoint file/name durable.
        _atomic(self.home / "checkpoint.json", _bytes(value))

    def rebuild(self):
        from .events import validate_event_records
        before = _identity(self.path)
        # Use the same decoder and semantic validator as the native reader.
        records = jsonl._read_path_records(self.path, self.root.tenant_id, self.kinds, unrecognized={})
        validate_event_records(records)
        data = self.path.read_bytes() if before is not None else b""
        if before != _identity(self.path) or len(data) != (0 if before is None else before[4]):
            raise IntegrityFailure("ledger_identity_changed_during_read", "events.jsonl changed during prefix validation")
        # The second exact-byte capture must itself be validated: a writer
        # bypassing the advisory lock cannot swap decoder input unnoticed.
        checked = jsonl._decode_path_records(self.path, self.root.tenant_id, self.kinds, data, unrecognized={})
        validate_event_records(checked)
        physical = decode_frames(data)
        self.memo = {}
        self.payload = {"binding": self.binding, "identity": before, "offset": len(data),
            "count": len(physical), "head": _EMPTY_HEAD, "trie": None,
            "mail_base_count": len(physical),
            # Only certified native appends may retain this identity. Any
            # replay, including an equivalent replacement, revokes old joins.
            "generation": uuid.uuid4().hex}
        pending = pending_envelopes(physical)
        if (pending is not None and pending * 64 > jsonl.MAX_RECORD_BYTES
                and all(frame == encode_frame(row) for frame, row in zip(data.splitlines(keepends=True), checked))):
            self.payload["pending_only"] = pending
        entries = {_key("id", row["id"]): row for row in physical}
        groups = {}
        companions = {}
        for row in checked:
            kind = row["kind"]
            if kind == "message_envelope":
                groups.setdefault(row["idempotency_key"], row)
            elif kind == "message_retracted":
                entries[_key("retraction", row["retracted_message_id"])] = row
            elif kind in {"delivery_claim", "send_readiness_observation", "seat_execution"}:
                reference = row.get("note_ref") if kind == "delivery_claim" else row.get("envelope_id")
                companions.setdefault((reference, kind), row)
        for key, envelope in groups.items():
            rows = [envelope] + [companions[(envelope["id"], kind)] for kind in
                ("delivery_claim", "send_readiness_observation", "seat_execution")
                if (envelope["id"], kind) in companions]
            entries[_key("send", key)] = rows
        complete = len(physical) == len(checked)
        if complete:
            from .mail_summary import event_entries
            entries.update(event_entries(physical))
        # Future kinds revoke the optional mail map, including its physical
        # ordinals. Ordered references retain the native reader below instead
        # of publishing a history-sized map for an incomplete certificate.
        entries[_key("mail-valid-v1", "complete")] = complete
        entries[_key("mail-first-v1", "physical")] = physical[0] if physical else None
        # Build bottom-up once on recovery, rather than fsync every obsolete
        # intermediate root that an incremental insertion would produce.
        def build(items):
            if not items:
                return None
            if len(items) == 1:
                key, value = items[0]
                return self._put({"prefix": key, "value": self._put(value)})
            first, last = items[0][0], items[-1][0]
            length = 0
            while first[length] == last[length]:
                length += 1
            groups = {}
            for key, value in items:
                groups.setdefault(key[length], []).append((key, value))
            return self._put({"prefix": first[:length],
                "children": {digit: build(group) for digit, group in groups.items()}})
        self._rebuilding = True
        self._bank_data = {}
        try:
            self.payload["trie"] = build(sorted(entries.items()))
            from .send_bank import publish
            publish(self.bank, self._bank_data)
        finally:
            self._rebuilding = False
            self._bank_data = None
        for frame in data.splitlines(keepends=True):
            self.payload["head"] = hashlib.sha256(bytes.fromhex(self.payload["head"]) + frame).hexdigest()
        if before != _identity(self.path):
            raise IntegrityFailure("ledger_identity_changed_during_read", "events.jsonl changed during prefix validation")
        self.publish()
        # The complete cold prefix now lives in one compact bank. Retired
        # incremental paths can be reclaimed only after its seal is durable.
        # Ordinary sends never enumerate or rewrite the historical bank.
        for node in self.nodes.iterdir():
            if _HEX.fullmatch(node.name) is not None:
                node.unlink()

    def selected(self, key, reply_to):
        rows = list(self.get("send", key) or ())
        if isinstance(reply_to, str):
            original = self.get("id", reply_to)
            if original is not None and original["kind"] == "message_envelope" and original not in rows:
                rows.append(original)
        return rows

    def _check_append_identity(self, encoded, identity):
        expected_size = self.payload["offset"] + sum(map(len, encoded))
        if (identity is None or identity[4] != expected_size
                or list(identity) != _identity(self.path)
                or (self.payload["identity"] is not None
                    and list(identity[:4]) != self.payload["identity"][:4])):
            raise CacheMiss()

    def _advance(self, batch, encoded):
        pending = self.payload.get("pending_only")
        if pending is not None:
            added = pending_envelopes(batch)
            self.payload["pending_only"] = None if added is None else pending + added
        if not self.payload["count"] and batch:
            self.set("mail-first-v1", "physical", batch[0])
        for row in batch:
            self.set("id", row["id"], row)
        envelope = batch[0]
        self.set("send", envelope["idempotency_key"], list(batch))
        if self.get("mail-valid-v1", "complete") is True:
            from .mail_summary import advance_events
            advance_events(self, batch)
        for frame in encoded:
            self.payload["head"] = hashlib.sha256(bytes.fromhex(self.payload["head"]) + frame).hexdigest()
        self.payload["count"] += len(batch)

    def _seal_append(self, identity):
        if list(identity) != _identity(self.path):
            raise CacheMiss()
        if (self.home / "preparation.json").is_file():
            from .send_prepare import advance_source
            advance_source(self, identity)
        self.payload["identity"] = list(identity)
        self.payload["offset"] = self.payload["identity"][4]
        self.publish()

    def appended(self, batch, encoded, identity):
        self._check_append_identity(encoded, identity)
        self._appending_to = list(identity)
        try:
            self._advance(batch, encoded)
            self._seal_append(identity)
        finally:
            self._appending_to = None


_CACHE_ERRORS = (CacheMiss, OSError, ValueError, KeyError, TypeError, IndexError, RecursionError)


def pending_envelopes(rows):
    """A lower bound only on a plane containing exclusively send transactions."""
    count = 0
    for row in rows:
        if row["kind"] == "message_envelope":
            count += 1
        elif row["kind"] not in {"send_readiness_observation", "delivery_claim"}:
            return None
    return count


def large_pending_only_epoch(root):
    """Under epoch EX, prove a roll would copy every byte of a large send plane."""
    from .adhoc_records import JOURNAL_RELATIVE
    from .bus_epoch import _preflight_selected_members, _require_owned_epoch_guard
    from .events import EVENT_KINDS
    _require_owned_epoch_guard(root, exclusive=True)
    try:
        prefix = Prefix(root, EVENT_KINDS, read_only=True)
        prefix.load()
        count = prefix.payload.get("pending_only")
        # Each envelope alone needs a 64-byte SHA in the v0 inventory.
        if type(count) is not int or count * 64 <= jsonl.MAX_RECORD_BYTES:
            return False
        for relative in (JOURNAL_RELATIVE, "receipts/cursor-presentations", "receipts/seat-teardown"):
            try:
                (root.tenant_home / relative).lstat()
            except FileNotFoundError:
                continue
            return False
        members = _preflight_selected_members(root)
        return (len(members) == 1 and members[0][0] == "events.jsonl"
                and list(members[0][3]) == prefix.payload["identity"])
    except _CACHE_ERRORS:
        return False


def current_epoch_has_no_roll_head(root):
    """Prove that this exact current event source has no archive-chain head.

    This is a read-only source certificate, independent of mail completeness:
    valid future-kind tails do not revoke the already certified physical head.
    False means unavailable or a roll head, and lends no archive answer.
    """
    from .bus_epoch import epoch_guard
    from .events import EVENT_KINDS
    try:
        with epoch_guard(root, exclusive=False):
            prefix = Prefix(root, EVENT_KINDS, read_only=True)
            prefix.load()
            payload = prefix.payload
            count, head = payload["count"], payload["head"]
            if (type(count) is not int or not 0 <= count <= jsonl.MAX_LEDGER_RECORDS
                    or type(head) is not str or _HEX.fullmatch(head) is None):
                return False
            first = prefix.get("mail-first-v1", "physical")
            if count == 0:
                if first is not None or payload["offset"] != 0 or head != _EMPTY_HEAD:
                    return False
            else:
                if (type(first) is not dict or type(first.get("id")) is not str
                        or type(first.get("kind")) is not str or payload["offset"] <= 0
                        or head == _EMPTY_HEAD
                        or prefix.get("id", first["id"]) != first):
                    return False
                if first["kind"] == "bus_epoch_roll_receipt":
                    return False
            return payload["identity"] == _identity(prefix.path)
    except _CACHE_ERRORS:
        return False


def _recover_send(prefix):
    from .send_prepare import STEP_BYTES, _progress, finish_interrupted_publish
    identity = _identity(prefix.path)
    preparing = (prefix.home / "preparation.json").exists()
    if preparing:
        try:
            if finish_interrupted_publish(prefix, _progress(prefix)):
                return
        except _CACHE_ERRORS:
            pass
    if (identity is not None and identity[4]
            and (identity[4] > STEP_BYTES or preparing)):
        from .send_unprepared import certify
        certify(prefix)
    else:
        prefix.rebuild()


def open_prefix(root, kinds, *, revision=None, routine_send=False):
    """Called only while the native event ledger lock is held."""
    try:
        prefix = Prefix(root, kinds, revision=revision)
    except _CACHE_ERRORS:
        return None
    try:
        prefix.load()
    except _CACHE_ERRORS:
        try:
            if routine_send:
                _recover_send(prefix)
            else:
                prefix.rebuild()
        except _CACHE_ERRORS:
            return None
    return prefix


def _selected(prefix, root, kinds, key, reply_to):
    if prefix is not None:
        try:
            return prefix.selected(key, reply_to), prefix
        except _CACHE_ERRORS:
            try:
                _recover_send(prefix)
                return prefix.selected(key, reply_to), prefix
            except _CACHE_ERRORS:
                pass
    from .events import validate_event_records
    path = root.resolve_relative("events.jsonl")
    from .send_prepare import STEP_BYTES
    identity = _identity(path)
    if identity and identity[4] > STEP_BYTES:
        from .send_unprepared import report_validation
        report_validation("send_preparation_unavailable", "validating_native_history", source_bytes=identity[4])
    rows = jsonl._read_path_records(path, root.tenant_id,
        kinds, unrecognized={})
    validate_event_records(rows)
    return rows, None


class _SelectedRows(list):
    """One operation's authenticated paths, rechecked at its append boundary."""
    def __init__(self, rows, prefix, key, reply_to):
        super().__init__(rows)
        self._prefix, self._key, self._reply_to = prefix, key, reply_to


def _prior_prefix(root, kinds, key, reply_to, prior_selection):
    if type(prior_selection) is _SelectedRows:
        candidate = prior_selection._prefix
        if (candidate is not None and prior_selection._key == key and prior_selection._reply_to == reply_to
                and candidate.root == root and candidate.kinds == kinds
                and candidate.payload["identity"] == _identity(candidate.path)
                and candidate.binding["maximum"] == jsonl.MAX_RECORD_BYTES
                and candidate.binding["ledger_bytes"] == jsonl.MAX_LEDGER_BYTES
                and candidate.binding["ledger_records"] == jsonl.MAX_LEDGER_RECORDS):
            return candidate
    return None


def selected_records(root, key, reply_to, *, previous=None):
    from .events import EVENT_KINDS
    path = root.resolve_relative("events.jsonl")
    lock, coordinate = jsonl._lock_beside(path, "events.jsonl")
    # This operation's loaded implementation is fixed; the next fresh
    # operation measures its own complete dependency metadata again.
    try:
        revision = _revision() if previous is None else None
    except _CACHE_ERRORS:
        revision = None  # Guarded open_prefix retains native full validation.
    with jsonl._epoch_writer_guard(root, "events.jsonl", path=path), jsonl._locked_path(
            lock, exclusive=True, relative=coordinate):
        prefix = _prior_prefix(root, EVENT_KINDS, key, reply_to, previous)
        prefix = prefix or open_prefix(root, EVENT_KINDS, revision=revision, routine_send=True)
        rows, prefix = _selected(prefix, root, EVENT_KINDS, key, reply_to)
        return _SelectedRows(rows, prefix, key, reply_to)


def _reference_rows(prefix, item_ids, physical_order):
    rows, retracted = [], set()
    for item_id in item_ids:
        row = prefix.get("id", item_id)
        if row is not None and row["kind"] == "message_envelope":
            rows.append(row)
        if prefix.get("retraction", item_id) is not None:
            retracted.add(item_id)
    if physical_order:
        count = prefix.payload["count"]
        if type(count) is not int or not 0 <= count <= jsonl.MAX_LEDGER_RECORDS:
            raise CacheMiss()
        positions = [prefix.get("mail-ordinal-v1", row["id"]) for row in rows]
        if (any(type(value) is not int or not 0 <= value < count
                for value in positions) or len(set(positions)) != len(positions)):
            raise CacheMiss()
        rows = [row for _position, row in sorted(zip(positions, rows), key=lambda pair: pair[0])]
    return rows, retracted


def event_references(root, item_ids, *, previous=None, physical_order=False, optimistic=False):
    """Resolve exact message/retraction proofs without projecting other events."""
    from .events import EVENT_KINDS, validate_event_records
    path = root.resolve_relative("events.jsonl")
    lock, coordinate = jsonl._lock_beside(path, "events.jsonl")
    try:
        revision = _revision() if previous is None else None
    except _CACHE_ERRORS:
        revision = None
    if optimistic:
        # Capture a complete native certificate while its source is locked;
        # immutable hash-addressed paths can then be checked without blocking
        # appends. A concurrent append invalidates this cut and takes the
        # original guarded reader. No recovery writes occur outside its lock.
        with jsonl._epoch_writer_guard(root, "events.jsonl", path=path):
            try:
                with jsonl._locked_path(lock, exclusive=True, relative=coordinate):
                    prefix = Prefix(root, EVENT_KINDS, revision=revision, read_only=True)
                    prefix.load()
                if physical_order and prefix.get("mail-valid-v1", "complete") is False:
                    raise CacheMiss()
                rows, retracted = _reference_rows(prefix, item_ids, physical_order)
                if (root.resolve_relative("events.jsonl") != prefix.path
                        or prefix.payload["identity"] != _identity(prefix.path)):
                    raise CacheMiss()
                return rows, retracted, _SelectedRows([], prefix, "", None)
            except _CACHE_ERRORS:
                pass
        return event_references(root, item_ids, previous=previous, physical_order=physical_order)
    with jsonl._epoch_writer_guard(root, "events.jsonl", path=path), jsonl._locked_path(
            lock, exclusive=True, relative=coordinate):
        prefix = _prior_prefix(root, EVENT_KINDS, "", None, previous)
        prefix = prefix or open_prefix(root, EVENT_KINDS, revision=revision)
        for attempt in range(2):
            if prefix is None:
                break
            try:
                if physical_order and prefix.get("mail-valid-v1", "complete") is False:
                    # This map deliberately has no ordinal proof. Read the
                    # validated native order without rebuilding the same bank.
                    break
                rows, retracted = _reference_rows(prefix, item_ids, physical_order)
                return rows, retracted, _SelectedRows([], prefix, "", None)
            except _CACHE_ERRORS:
                if attempt == 0:
                    try:
                        prefix.rebuild()
                    except _CACHE_ERRORS:
                        prefix = None
        rows = jsonl._read_path_records(path, root.tenant_id, EVENT_KINDS, unrecognized={})
        validate_event_records(rows)
        wanted = set(item_ids)
        return ([row for row in rows if row["kind"] == "message_envelope" and row["id"] in wanted],
            {row["retracted_message_id"] for row in rows if row["kind"] == "message_retracted"
                and row["retracted_message_id"] in wanted}, None)


def _validate_batch(batch, prior):
    """Certify only new send rows; prior reply ancestry is already sealed."""
    from .events import validate_event_records
    from .native_dispatch_launcher import KIND as launcher_kind
    if (not 2 <= len(batch) <= 4 or batch[0]["kind"] != "message_envelope"
            or any(row["kind"] not in {"message_envelope", "delivery_claim",
                "send_readiness_observation", "seat_execution", launcher_kind} for row in batch)
            or len({row["kind"] for row in batch}) != len(batch)
            or sum(row["kind"] == "message_envelope" for row in batch) != 1
            or sum(row["kind"] == "send_readiness_observation" for row in batch) != 1):
        raise ProtocolRefusal("record_kind_invalid", "indexed send requires one native notice batch")
    envelope = batch[0]
    reply = envelope.get("reply_to")
    if reply is not None:
        original = next((row for row in prior if row["id"] == reply and row["kind"] == "message_envelope"), None)
        if original is None or original["sender"] != envelope["recipient"] or original["recipient"] != envelope["sender"]:
            raise IntegrityFailure("message_order_invalid", "reply evidence must reverse its referenced original parties")
    # This removes only the dependency on previously certified ancestry, not
    # any schema check or direct-party check on the exact encoded envelope.
    sanitized = dict(envelope)
    sanitized.pop("reply_to", None)
    validate_event_records([sanitized, *batch[1:]])


def _require_native_launcher_writer(batch, decide):
    from .events import EventLog
    from .native_dispatch_launcher import KIND as launcher_kind

    if not any(row.get("kind") == launcher_kind for row in batch):
        return
    callback_code = getattr(decide, "__code__", None)
    if callback_code is None or not any(callback_code is code
                                       for code in EventLog._send_impl.__code__.co_consts):
        jsonl._native_launcher_only()


class _SendStage:
    """Opaque handle: construction and caller attributes grant no authority."""
    __slots__ = ("__weakref__",)


_STAGES = WeakKeyDictionary()


class _StageState:
    """One native producer's immutable, uncommitted preparation."""
    def __init__(self, base, prefix, decide, result, batch, encoded, directory, data, *, failure=None):
        self.base, self.prefix, self.decide = base, prefix, decide
        self.before = copy.deepcopy(base.payload)
        self.result, self.batch, self.encoded = result, batch, encoded
        self.directory, self.data = Path(directory), data
        self.failure = failure

    def install(self, identity):
        prefix = self.prefix
        prefix._check_append_identity(self.encoded, identity)
        for digest, data in self.data.items():
            pending, target = self.directory / digest, prefix.nodes / digest
            if hashlib.sha256(data).hexdigest() != digest or _read(pending, 5 * jsonl.MAX_RECORD_BYTES) != data:
                raise CacheMiss()
            try:
                if _read(target, 5 * jsonl.MAX_RECORD_BYTES) == data:
                    continue  # Preserve already-certified immutable metadata.
            except _CACHE_ERRORS:
                pass
            os.replace(pending, target)
        prefix._seal_append(identity)


@contextmanager
def stage_send(root, kinds, key, reply_to, prior_selection, decide, *, max_bytes=jsonl.MAX_RECORD_BYTES):
    """Prepare only the native send callback; generic callbacks stay locked."""
    stage, temporary = None, None
    try:
        base = _prior_prefix(root, kinds, key, reply_to, prior_selection)
        if base is not None:
            prefix = copy.copy(base)
            prefix.payload, prefix.memo = copy.deepcopy(base.payload), dict(base.memo)
            prefix._staged_data = {}
            result, candidates = copy.deepcopy(decide(copy.deepcopy(list(prior_selection))))
            batch = tuple(candidates)
            if batch:
                try:
                    encoded = tuple(jsonl._encode_record(row, root.tenant_id, kinds,
                        max_bytes=max_bytes) for row in batch)
                except ProtocolRefusal as exc:
                    # Keep the failure tied to this exact prefix. Only the
                    # locked transaction may decide it is still authoritative.
                    stage = _SendStage()
                    _STAGES[stage] = _StageState(base, prefix, decide, result, batch,
                        (), prefix.staging, {}, failure=exc)
                else:
                    _validate_batch(batch, prior_selection)
                    if (batch[0]["idempotency_key"] != key or batch[0].get("reply_to") != reply_to
                            or any(row["kind"] == "message_envelope" and row["idempotency_key"] == key
                                for row in prior_selection)):
                        raise CacheMiss()
                    ids = [row["id"] for row in batch]
                    if (len(set(ids)) != len(ids) or any(prefix.get("id", value) is not None for value in ids)
                            or prefix.payload["count"] + len(batch) > jsonl.MAX_LEDGER_RECORDS):
                        raise CacheMiss()
                    prefix._advance(batch, encoded)
                    data = prefix._staged_data
                    prefix._staged_data = None
                    temporary = tempfile.TemporaryDirectory(prefix="pending-", dir=prefix.staging)
                    for digest, raw in data.items():
                        _atomic(Path(temporary.name) / digest, raw, durable=False)
                    stage = _SendStage()
                    _STAGES[stage] = _StageState(base, prefix, decide, result, batch, encoded, temporary.name, data)
    except Exception:
        # Speculation never decides a refusal against a possibly newer prefix.
        stage = None
    try:
        yield stage
    finally:
        if stage is not None:
            _STAGES.pop(stage, None)
        if temporary is not None:
            try:
                temporary.cleanup()
            except _CACHE_ERRORS:
                pass  # Unsealed remnants can never authorize ledger bytes.


def transact_send_locked(root, relative, decide, *, kinds, max_bytes, selection, execution_marker, _use_cache=True):
    """Closed send-only transaction, entered under jsonl's native guards."""
    from .events import EVENT_KINDS
    if Path(relative) != Path("events.jsonl") or kinds != EVENT_KINDS or max_bytes != jsonl.MAX_RECORD_BYTES:
        raise ProtocolRefusal("record_kind_invalid", "indexed send requires the native event ledger")
    key, reply_to = selection[:2]
    prior_selection = selection[2] if len(selection) >= 3 else None
    token = selection[3] if len(selection) == 4 else None
    stage = _STAGES.get(token) if type(token) is _SendStage else None
    prefix = None
    if _use_cache:
        prefix = _prior_prefix(root, kinds, key, reply_to, prior_selection)
        if prefix is None:
            prefix = open_prefix(root, kinds, routine_send=True)
    if (stage is not None and prefix is stage.base and stage.decide is decide
            and prefix.payload == stage.before and prefix.binding == stage.prefix.binding
            and stage.before["identity"] == _identity(prefix.path)):
        _require_native_launcher_writer(stage.batch, decide)
        if any(row.get("kind") == "seat_execution" for row in stage.batch) and execution_marker is not jsonl._SEAT_EXECUTION_APPEND_MARKER:
            raise ProtocolRefusal("seat_execution_controller_only", "execution testimony requires its sealed event transaction",
                remedy="use the authenticated native execution producer instead of generic ledger append")
        if stage.failure is not None:
            raise stage.failure
        from .inbox_prefix import prepare_append, advance
        inbox = prepare_append(root, relative, kinds, max_bytes, stage.batch)
        from .cut_prefix import prepare_append as prepare_cut, advance as advance_cut
        cut = prepare_cut(root, relative, kinds, max_bytes, stage.batch)
        identity = jsonl._append_frame(prefix.path, b"".join(stage.encoded))
        try:
            stage.install(identity)
        except _CACHE_ERRORS:
            pass  # Durable ledger is authoritative; the previous seal is stale.
        advance(inbox, stage.batch, stage.encoded, identity)
        advance_cut(cut, stage.batch, stage.encoded, identity)
        jsonl.publish_ledger_hints(root, relative, stage.batch)
        return stage.result
    prior, prefix = _selected(prefix, root, kinds, key, reply_to)
    result, candidates = decide(prior)
    batch = tuple(candidates)
    if not batch:
        return result
    _require_native_launcher_writer(batch, decide)
    if any(row.get("kind") == "seat_execution" for row in batch) and execution_marker is not jsonl._SEAT_EXECUTION_APPEND_MARKER:
        raise ProtocolRefusal("seat_execution_controller_only", "execution testimony requires its sealed event transaction",
            remedy="use the authenticated native execution producer instead of generic ledger append")
    encoded = tuple(jsonl._encode_record(row, root.tenant_id, kinds, max_bytes=max_bytes) for row in batch)
    _validate_batch(batch, prior)
    if batch[0]["idempotency_key"] != key or batch[0].get("reply_to") != reply_to:
        raise ProtocolRefusal("idempotency_conflict", "indexed send selection must equal its notice")
    if any(row["kind"] == "message_envelope" and row["idempotency_key"] == key for row in prior):
        raise ProtocolRefusal("idempotency_conflict", "idempotency key has different content")
    candidate_ids = [row["id"] for row in batch]
    if len(set(candidate_ids)) != len(batch):
        raise ProtocolRefusal("duplicate_record_id", "record batch repeats an id")
    if prefix is not None:
        try:
            duplicates = [record_id for record_id in candidate_ids if prefix.get("id", record_id) is not None]
        except _CACHE_ERRORS:
            # A broken selected proof may not be interpreted as absence.
            try:
                _recover_send(prefix)
                duplicates = [record_id for record_id in candidate_ids if prefix.get("id", record_id) is not None]
            except _CACHE_ERRORS:
                return transact_send_locked(root, relative, decide, kinds=kinds,
                    max_bytes=max_bytes, selection=selection,
                    execution_marker=execution_marker, _use_cache=False)
        count = prefix.payload["count"]
    else:
        duplicates = [record_id for record_id in candidate_ids if any(row["id"] == record_id for row in prior)]
        # Include future-kind physical records when the cache is unavailable.
        physical = decode_frames(root.resolve_relative(relative).read_bytes()) if root.resolve_relative(relative).exists() else []
        count = len(physical)
        duplicates += [record_id for record_id in candidate_ids if any(row["id"] == record_id for row in physical)]
    if duplicates:
        raise ProtocolRefusal("duplicate_record_id", f"record id {duplicates[0]} already exists")
    if count + len(batch) > jsonl.MAX_LEDGER_RECORDS:
        raise ProtocolRefusal("ledger_record_limit", f"ledger maximum is {jsonl.MAX_LEDGER_RECORDS} records")
    path = root.resolve_relative(relative)
    if prefix is not None and prefix.payload["identity"] != _identity(path):
        raise IntegrityFailure("ledger_identity_changed_during_read", "events.jsonl changed before send append")
    from .inbox_prefix import prepare_append, advance
    inbox = prepare_append(root, relative, kinds, max_bytes, batch)
    from .cut_prefix import prepare_append as prepare_cut, advance as advance_cut
    cut = prepare_cut(root, relative, kinds, max_bytes, batch)
    identity = jsonl._append_frame(path, b"".join(encoded))
    if prefix is not None:
        try:
            prefix.appended(batch, encoded, identity)
        except _CACHE_ERRORS:
            # Ledger fsync already succeeded. Leave the previous identity seal
            # stale so the next process recovers the exact result by full read.
            pass
    advance(inbox, batch, encoded, identity)
    advance_cut(cut, batch, encoded, identity)
    jsonl.publish_ledger_hints(root, relative, batch)
    return result


def warm(root):
    """Validate a copied fixture's exact inode before a fresh timed process."""
    from .events import EVENT_KINDS
    path = root.resolve_relative("events.jsonl")
    lock, coordinate = jsonl._lock_beside(path, "events.jsonl")
    with jsonl._epoch_writer_guard(root, "events.jsonl", path=path), jsonl._locked_path(
            lock, exclusive=True, relative=coordinate):
        prefix = open_prefix(root, EVENT_KINDS)
        if prefix is not None and ("pages" in prefix.payload or "filter" in prefix.payload):
            prefix.rebuild()
        result = None if prefix is None else {"identity": prefix.payload["identity"],
            "count": prefix.payload["count"], "head": prefix.payload["head"]}
    return result

"""Exact selected admission through authenticated fixed-capacity membership.

Sealed bucket hashes prove exact absence. A positive proof with a missing entry
always requires native recovery, including deletion during index publication.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import stat
import uuid
import zlib
from contextlib import contextmanager
from functools import wraps

from . import jsonl, send_prefix
from .errors import IntegrityFailure

INDEX_FORMAT = "bucket-key-prefix-v1"
BUCKET_COUNT = 1024
BUCKET_BYTES = 8192
BUCKET_CAPACITY = 255
DIRECTORY_BYTES = BUCKET_COUNT * 32
EMPTY_BUCKET = bytes(BUCKET_BYTES)
EMPTY_HASH = hashlib.sha256(EMPTY_BUCKET).digest()
MAX_BUCKET_STORAGE_BYTES = BUCKET_BYTES + 64
BUCKET_ENCODING = {"format": "zlib-page-v1", "level": 9, "wbits": 15, "mem_level": 8,
    "strategy": 0, "runtime_version": zlib.ZLIB_RUNTIME_VERSION}
_GENERATION = re.compile(r"[0-9a-f]{32}\Z")
_KEY = re.compile(r"[01]{256}\Z")
_FIELDS = frozenset({"binding", "identity", "offset", "count", "head", "folds", "generation", "directory_sha256"})


def _confined(operation):
    @wraps(operation)
    def invoke(self, *args, **kwargs):
        with self._scope():
            return operation(self, *args, **kwargs)
    return invoke


def bucket_index(secret, generation, key):
    digest = hmac.new(secret, b"floati-key-bucket-v1\0" + generation.encode("ascii") + b"\0"
        + bytes.fromhex(KeyPrefix.filename(key)), hashlib.sha256).digest()
    return int.from_bytes(digest[:2], "big") >> 6


def encode_bucket(keys):
    values = sorted(bytes.fromhex(KeyPrefix.filename(key)) for key in keys)
    if len(values) > BUCKET_CAPACITY or len(set(values)) != len(values):
        raise send_prefix.CacheMiss()
    return len(values).to_bytes(2, "big") + b"".join(values) + bytes(BUCKET_BYTES - 2 - 32 * len(values))


def decode_bucket(raw):
    if len(raw) != BUCKET_BYTES:
        raise send_prefix.CacheMiss()
    count = int.from_bytes(raw[:2], "big")
    if count > BUCKET_CAPACITY:
        raise send_prefix.CacheMiss()
    values = [raw[2 + 32 * index:2 + 32 * (index + 1)] for index in range(count)]
    if values != sorted(set(values)) or any(raw[2 + 32 * count:]):
        raise send_prefix.CacheMiss()
    return {"".join(format(byte, "08b") for byte in value) for value in values}


def encode_storage(page):
    if len(page) != BUCKET_BYTES:
        raise send_prefix.CacheMiss()
    compressor = zlib.compressobj(level=9, method=zlib.DEFLATED, wbits=15, memLevel=8, strategy=zlib.Z_DEFAULT_STRATEGY)
    return compressor.compress(page) + compressor.flush(zlib.Z_FINISH)


def decode_storage(raw):
    try:
        decoder = zlib.decompressobj(wbits=15)
        page = decoder.decompress(raw, BUCKET_BYTES + 1)
        if (len(page) != BUCKET_BYTES or not decoder.eof or decoder.unused_data or decoder.unconsumed_tail
                or encode_storage(page) != raw):
            raise send_prefix.CacheMiss()
        return page
    except zlib.error as error:
        raise send_prefix.CacheMiss() from error


class KeyPrefix:
    @staticmethod
    def storage_declaration(*, relative, cache_name):
        """Declare the writer's authoritative source and publication roles.

        The bank can be rebuilt from the source. During an ordinary append,
        existing keys and their secret stay immutable while the directory,
        affected buckets and checkpoint are published by atomic replacement.
        """
        return {"source": {"path": Path(relative).as_posix(), "role": "retained_append_only"},
            "index": {"path": cache_name, "role": "derived_rebuildable",
                "files": {"seal.key": "retained_immutable",
                    "directory.bin": "derived_atomic_replace", "checkpoint.json": "derived_atomic_replace"},
                "members": {"nodes": "retained_immutable", "buckets": "derived_replaceable"}}}

    def __init__(self, root, kinds, *, relative, cache_name, format, revision=None, folds=None,
                 read_only=False):
        self.read_only = read_only
        self.root, self.kinds = root, frozenset(kinds)
        storage = self.storage_declaration(relative=relative, cache_name=cache_name)
        self.path = root.resolve_relative(storage["source"]["path"])
        self.home = root.tenant_home / storage["index"]["path"]
        self.nodes, self.staging, self.buckets = self.home / "nodes", self.home / "staging", self.home / "buckets"
        if self.home.parent != root.tenant_home or cache_name in {"", ".", ".."}:
            raise send_prefix.CacheMiss()
        self._io = None
        self._anchors = None
        with self._scope(create=not read_only):
            try:
                self.secret = self._read_at("home", "seal.key", 32)
            except FileNotFoundError:
                if read_only:
                    raise send_prefix.CacheMiss()
                self.secret = os.urandom(32)
                self._atomic_at("home", "seal.key", self.secret)
        if len(self.secret) != 32:
            raise send_prefix.CacheMiss()
        self.binding = {"format": format, "index_format": INDEX_FORMAT,
            "bucket_count": BUCKET_COUNT, "bucket_bytes": BUCKET_BYTES, "bucket_capacity": BUCKET_CAPACITY,
            "bucket_encoding": dict(BUCKET_ENCODING),
            "relative": Path(relative).as_posix(), "root": str(root.tenant_home), "tenant": root.tenant_id,
            "kinds": sorted(kinds), "maximum": jsonl.MAX_RECORD_BYTES,
            "ledger_bytes": jsonl.MAX_LEDGER_BYTES, "ledger_records": jsonl.MAX_LEDGER_RECORDS,
            "revision": send_prefix._revision() if revision is None else revision,
            "folds": {} if folds is None else dict(folds)}
        self.payload, self.directory, self._directory_identity = None, None, None
        self.memo = {}
        self._publishing = False

    @staticmethod
    def _directory_status(info, *, root=False):
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                or (not root and stat.S_IMODE(info.st_mode) != 0o700)):
            raise send_prefix.CacheMiss()

    @contextmanager
    def _scope(self, *, create=False):
        if self._io is not None:
            self._check_directories()
            yield
            return
        held = []
        opened = {}
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
        try:
            descriptor = os.open(self.root.tenant_home, flags)
            held.append(descriptor)
            self._directory_status(os.fstat(descriptor), root=True)
            opened["root"] = descriptor
            for name, parent, leaf in (("home", "root", self.home.name),
                    ("nodes", "home", "nodes"), ("staging", "home", "staging"),
                    ("buckets", "home", "buckets")):
                if create:
                    try:
                        os.mkdir(leaf, mode=0o700, dir_fd=opened[parent])
                    except FileExistsError:
                        pass
                descriptor = os.open(leaf, flags, dir_fd=opened[parent])
                held.append(descriptor)
                self._directory_status(os.fstat(descriptor))
                if os.fstat(descriptor).st_dev != os.fstat(opened["root"]).st_dev:
                    raise send_prefix.CacheMiss()
                opened[name] = descriptor
            identities = {name: (os.fstat(descriptor).st_dev, os.fstat(descriptor).st_ino)
                for name, descriptor in opened.items()}
            if self._anchors is None:
                self._anchors = identities
            elif identities != self._anchors:
                raise send_prefix.CacheMiss()
            self._io = opened
            self._check_directories()
            yield
        finally:
            self._io = None
            for descriptor in reversed(held):
                os.close(descriptor)

    def _check_directories(self):
        root_status = os.fstat(self._io["root"])
        named = self.root.tenant_home.lstat()
        self._directory_status(root_status, root=True)
        self._directory_status(named, root=True)
        if (root_status.st_dev, root_status.st_ino) != (named.st_dev, named.st_ino):
            raise send_prefix.CacheMiss()
        for name, parent, leaf in (("home", "root", self.home.name),
                ("nodes", "home", "nodes"), ("staging", "home", "staging"),
                ("buckets", "home", "buckets")):
            actual = os.fstat(self._io[name])
            named = os.stat(leaf, dir_fd=self._io[parent], follow_symlinks=False)
            self._directory_status(actual)
            self._directory_status(named)
            if (actual.st_dev != root_status.st_dev
                    or (actual.st_dev, actual.st_ino) != (named.st_dev, named.st_ino)):
                raise send_prefix.CacheMiss()

    @staticmethod
    def _leaf(name):
        if type(name) is not str or name in {"", ".", ".."} or "/" in name:
            raise send_prefix.CacheMiss()
        return name

    def _identity_at(self, bank, name):
        try:
            info = os.stat(self._leaf(name), dir_fd=self._io[bank], follow_symlinks=False)
        except FileNotFoundError:
            return None
        return [getattr(info, field) for field in send_prefix._FIELDS]

    def _read_at(self, bank, name, maximum):
        self._check_directories()
        name = self._leaf(name)
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
            dir_fd=self._io[bank])
        try:
            info = os.fstat(descriptor)
            identity = [getattr(info, field) for field in send_prefix._FIELDS]
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1 or info.st_size > maximum):
                raise send_prefix.CacheMiss()
            data = bytearray()
            while len(data) <= maximum:
                part = os.read(descriptor, min(65536, maximum + 1 - len(data)))
                if not part:
                    break
                data.extend(part)
            after = os.fstat(descriptor)
            if (len(data) > maximum or self._identity_at(bank, name) != identity
                    or [getattr(after, field) for field in send_prefix._FIELDS] != identity):
                raise send_prefix.CacheMiss()
            self._check_directories()
            return bytes(data)
        finally:
            os.close(descriptor)

    def _atomic_at(self, bank, name, data, *, durable=True):
        if self.read_only:
            raise send_prefix.CacheMiss()
        self._check_directories()
        name = self._leaf(name)
        temporary = ".pending-" + uuid.uuid4().hex
        parent = self._io[bank]
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600, dir_fd=parent)
        try:
            try:
                stream = os.fdopen(descriptor, "wb")
            except BaseException:
                os.close(descriptor)
                raise
            with stream:
                stream.write(data)
                stream.flush()
                if durable:
                    os.fsync(stream.fileno())
            self._check_directories()
            os.replace(temporary, name, src_dir_fd=parent, dst_dir_fd=parent)
            if durable:
                os.fsync(parent)
            self._check_directories()
        finally:
            try:
                os.unlink(temporary, dir_fd=parent)
            except FileNotFoundError:
                pass

    @_confined
    def _assert_current(self):
        self._check_directories()
        self._check_folds(self.payload["folds"])
        if (self.payload["binding"] != self.binding or self.payload["identity"] != send_prefix._identity(self.path)
                or self._directory_identity != self._identity_at("home", "directory.bin")):
            raise send_prefix.CacheMiss()

    @_confined
    def load(self, *, previous=None):
        raw = self._read_at("home", "checkpoint.json", 16384)
        value = json.loads(raw)
        if send_prefix._bytes(value) != raw:
            raise send_prefix.CacheMiss()
        if (type(value) is not dict or set(value) != {"payload", "seal"}
                or type(value["payload"]) is not dict or set(value["payload"]) != _FIELDS):
            raise send_prefix.CacheMiss()
        payload = value["payload"]
        self._check_folds(payload["folds"])
        seal = hmac.new(self.secret, send_prefix._bytes(payload), hashlib.sha256).hexdigest()
        if (not isinstance(value["seal"], str) or not hmac.compare_digest(seal, value["seal"])
                or type(payload["count"]) is not int or not 0 <= payload["count"] <= jsonl.MAX_LEDGER_RECORDS
                or type(payload["offset"]) is not int or not 0 <= payload["offset"] <= jsonl.MAX_LEDGER_BYTES
                or not isinstance(payload["head"], str) or send_prefix._HEX.fullmatch(payload["head"]) is None
                or not isinstance(payload["generation"], str) or _GENERATION.fullmatch(payload["generation"]) is None
                or payload["offset"] != (0 if payload["identity"] is None else payload["identity"][4])):
            raise send_prefix.CacheMiss()
        before = self._identity_at("home", "directory.bin")
        if (self.read_only and type(previous) is type(self) and previous.read_only
                and previous._anchors == self._anchors and previous.binding == self.binding
                and before is not None and previous._directory_identity == before):
            # The same immutable directory is already in memory. The fresh
            # checkpoint above and its hash below still authenticate all bytes.
            directory = previous.directory
        else:
            directory = self._read_at("home", "directory.bin", DIRECTORY_BYTES)
        if (len(directory) != DIRECTORY_BYTES or hashlib.sha256(directory).hexdigest() != payload["directory_sha256"]
                or before != self._identity_at("home", "directory.bin")):
            raise send_prefix.CacheMiss()
        self.payload, self.directory, self._directory_identity = payload, bytearray(directory), before
        self._assert_current()

    @_confined
    def publish(self):
        value = {"payload": self.payload,
            "seal": hmac.new(self.secret, send_prefix._bytes(self.payload), hashlib.sha256).hexdigest()}
        self._atomic_at("home", "checkpoint.json", send_prefix._bytes(value))

    @staticmethod
    def filename(key):
        if not isinstance(key, str) or _KEY.fullmatch(key) is None:
            raise send_prefix.CacheMiss()
        return format(int(key, 2), "064x")

    def _check_folds(self, folds):
        if type(folds) is not dict or set(folds) != set(self.binding["folds"]):
            raise send_prefix.CacheMiss()

    def _fold(self, rows, prior):
        return {}

    def _entry(self, key, value):
        payload = {"index_format": INDEX_FORMAT, "binding_sha256": hashlib.sha256(send_prefix._bytes(self.binding)).hexdigest(),
            "generation": self.payload["generation"], "key": key, "value": value}
        return {"payload": payload, "seal": hmac.new(self.secret, send_prefix._bytes(payload), hashlib.sha256).hexdigest()}

    def _entry_bytes(self, key, value):
        raw = send_prefix._bytes(self._entry(key, value))
        if len(raw) > 5 * jsonl.MAX_RECORD_BYTES:
            raise send_prefix.CacheMiss()
        return raw

    @_confined
    def _read_entry(self, key):
        raw = self._read_at("nodes", self.filename(key), 5 * jsonl.MAX_RECORD_BYTES)
        value = json.loads(raw)
        if send_prefix._bytes(value) != raw:
            raise send_prefix.CacheMiss()
        if type(value) is not dict or set(value) != {"payload", "seal"} or type(value["payload"]) is not dict:
            raise send_prefix.CacheMiss()
        payload = value["payload"]
        if set(payload) != {"index_format", "binding_sha256", "generation", "key", "value"}:
            raise send_prefix.CacheMiss()
        seal = hmac.new(self.secret, send_prefix._bytes(payload), hashlib.sha256).hexdigest()
        if (not isinstance(value["seal"], str) or not hmac.compare_digest(seal, value["seal"])
                or payload != self._entry(key, payload["value"])["payload"]):
            raise send_prefix.CacheMiss()
        return payload["value"]

    @_confined
    def _read_bucket(self, index):
        expected = bytes(self.directory[32 * index:32 * (index + 1)])
        if expected == EMPTY_HASH:
            return set()
        raw = self._read_at("buckets", format(index, "04x"), MAX_BUCKET_STORAGE_BYTES)
        page = decode_storage(raw)
        if hashlib.sha256(page).digest() != expected:
            raise send_prefix.CacheMiss()
        return decode_bucket(page)

    @_confined
    def get(self, domain, value):
        key = send_prefix._key(domain, value)
        self._assert_current()
        members = self._read_bucket(bucket_index(self.secret, self.payload["generation"], key))
        row = self._read_entry(key) if key in members else None
        self._assert_current()
        return row

    @_confined
    def set(self, domain, value, row):
        if not self._publishing:
            raise send_prefix.CacheMiss()
        key = send_prefix._key(domain, value)
        index = bucket_index(self.secret, self.payload["generation"], key)
        members = self._read_bucket(index)
        if key in members:
            if send_prefix._bytes(self._read_entry(key)) == send_prefix._bytes(row):
                return
            raise send_prefix.CacheMiss()
        members.add(key)
        page = encode_bucket(members)
        self._atomic_at("nodes", self.filename(key), self._entry_bytes(key, row), durable=False)
        self._atomic_at("buckets", format(index, "04x"), encode_storage(page), durable=False)
        self.directory[32 * index:32 * (index + 1)] = hashlib.sha256(page).digest()

    @_confined
    def rebuild_entries(self, rows, entries, before, data):
        if before != send_prefix._identity(self.path) or len(data) != (0 if before is None else before[4]):
            raise IntegrityFailure("ledger_identity_changed_during_read", "key ledger changed during validation")
        if jsonl._decode_path_records(self.path, self.root.tenant_id, self.kinds, data) != rows:
            raise send_prefix.CacheMiss()
        folds = self._fold(rows, {})
        self._check_folds(folds)
        # 261120 slots cover at most 200000 ID/key entries. A bounded fresh
        # keyed generation handles bucket overflow; failure uses native replay.
        for _attempt in range(4):
            generation = uuid.uuid4().hex
            groups = {}
            for key in entries:
                groups.setdefault(bucket_index(self.secret, generation, key), set()).add(key)
            if all(len(group) <= BUCKET_CAPACITY for group in groups.values()):
                break
        else:
            raise send_prefix.CacheMiss()
        for bank, pattern in (("nodes", r"[0-9a-f]{64}"), ("buckets", r"[0-9a-f]{4}")):
            self._check_directories()
            old = os.listdir(self._io[bank])
            for name in old:
                info = os.stat(name, dir_fd=self._io[bank], follow_symlinks=False)
                if (re.fullmatch(pattern, name) is None or not stat.S_ISREG(info.st_mode)
                        or info.st_uid != os.getuid() or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600):
                    raise send_prefix.CacheMiss()
            for name in old:
                self._check_directories()
                os.unlink(name, dir_fd=self._io[bank])
        head = send_prefix._EMPTY_HEAD
        for frame in data.splitlines(keepends=True):
            head = hashlib.sha256(bytes.fromhex(head) + frame).hexdigest()
        self.payload = {"binding": self.binding, "identity": before, "offset": len(data), "count": len(rows),
            "head": head, "folds": folds, "generation": generation, "directory_sha256": None}
        self.directory = bytearray(EMPTY_HASH * BUCKET_COUNT)
        # Apply the reader's bound before publishing any entry. In particular,
        # an oversized live catalog must miss toward native history.
        for key, value in entries.items():
            self._entry_bytes(key, value)
        for key, value in entries.items():
            self._atomic_at("nodes", self.filename(key), self._entry_bytes(key, value), durable=False)
        for index, members in groups.items():
            page = encode_bucket(members)
            self._atomic_at("buckets", format(index, "04x"), encode_storage(page), durable=False)
            self.directory[32 * index:32 * (index + 1)] = hashlib.sha256(page).digest()
        if before != send_prefix._identity(self.path):
            raise IntegrityFailure("ledger_identity_changed_during_read", "key ledger changed during validation")
        check_sources = getattr(self, "check_sources", None)
        if check_sources is not None:
            check_sources()
        self._publish_directory()
        self._assert_current()
        self.publish()

    @_confined
    def _publish_directory(self):
        raw = bytes(self.directory)
        self._atomic_at("home", "directory.bin", raw)
        self._directory_identity = self._identity_at("home", "directory.bin")
        self.payload["directory_sha256"] = hashlib.sha256(raw).hexdigest()

    @_confined
    def _check_append_identity(self, encoded, identity):
        if (identity is None or identity[4] != self.payload["offset"] + sum(map(len, encoded))
                or list(identity) != send_prefix._identity(self.path)
                or (self.payload["identity"] is not None and list(identity[:4]) != self.payload["identity"][:4])
                or self._directory_identity != self._identity_at("home", "directory.bin")):
            raise send_prefix.CacheMiss()

    @_confined
    def _seal_append(self, identity):
        if list(identity) != send_prefix._identity(self.path):
            raise send_prefix.CacheMiss()
        self.payload["identity"], self.payload["offset"] = list(identity), identity[4]
        self._publish_directory()
        self._assert_current()
        self.publish()

    @_confined
    def appended(self, batch, encoded, identity):
        self._check_append_identity(encoded, identity)
        self._publishing = True
        try:
            self._advance(batch, encoded)
            self.payload["folds"] = self._fold(batch, self.payload["folds"])
            self._check_folds(self.payload["folds"])
        finally:
            self._publishing = False
        self._seal_append(identity)

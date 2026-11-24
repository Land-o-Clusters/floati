"""Authenticated source-frame locations for a prepared native send prefix.

Each directory entry commits to one complete hash bucket. The directory itself
is committed by the source-bound checkpoint. Only a verified bucket (including
a committed empty bucket) can prove a key absent. Values name exact ledger
frames. An unfinished step appends new pages without overwriting its last cut.
"""
import hashlib
import os
import stat
import struct
from contextlib import contextmanager

from . import jsonl
from .send_prefix import CacheMiss

BUCKETS = 1024
PAGE = struct.Struct(">QI32s")
ENTRY = struct.Struct(">32sIII32s")
EMPTY = (0, 0, bytes(32))
MAX_BUCKET_BYTES = 256 * 1024


@contextmanager
def open_bank(path, *, create=False, write=False):
    flags = os.O_RDWR if write else os.O_RDONLY
    if create:
        flags |= os.O_CREAT | os.O_EXCL
    fd = os.open(path, flags | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    with os.fdopen(fd, "r+b" if write else "rb") as stream:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077 or info.st_nlink != 1
                or info.st_size > 64 * jsonl.MAX_LEDGER_BYTES):
            raise CacheMiss()
        yield stream


def read_range(stream, pointer, limit):
    offset, size, digest = pointer
    if size <= 0 or size > limit:
        raise CacheMiss()
    stream.seek(offset)
    data = stream.read(size)
    if len(data) != size or hashlib.sha256(data).digest() != digest:
        raise CacheMiss()
    return data


class Pages:
    def __init__(self, bank, directory):
        self.bank = bank
        if directory is None:
            self.directory = [EMPTY] * BUCKETS
        else:
            pointer = directory[0], BUCKETS * PAGE.size, bytes.fromhex(directory[1])
            self.directory = list(PAGE.iter_unpack(read_range(bank, pointer, BUCKETS * PAGE.size)))
        self.buckets = {}
        self.changed = set()

    def _select(self, domain, value):
        key = hashlib.sha256(domain.encode("ascii") + b"\0" + value.encode("utf-8")).digest()
        bucket = int.from_bytes(key[:2], "big") % BUCKETS
        if bucket not in self.buckets:
            pointer = self.directory[bucket]
            data = b"" if pointer == EMPTY else read_range(self.bank, pointer, MAX_BUCKET_BYTES)
            if len(data) % ENTRY.size:
                raise CacheMiss()
            self.buckets[bucket] = {key: (offset, size, ordinal, digest)
                for key, offset, size, ordinal, digest in ENTRY.iter_unpack(data)}
        return bucket, key

    def get(self, domain, value):
        bucket, key = self._select(domain, value)
        return self.buckets[bucket].get(key)

    def put(self, domain, value, pointer):
        bucket, key = self._select(domain, value)
        rows = self.buckets[bucket]
        if key in rows:
            return False
        rows[key] = pointer
        self.changed.add(bucket)
        return True

    def commit(self, end):
        # Bytes beyond the last sealed cut can only be an interrupted step.
        self.bank.seek(0, os.SEEK_END)
        if self.bank.tell() < end:
            raise CacheMiss()
        self.bank.truncate(end)
        self.bank.seek(end)
        for bucket in sorted(self.changed):
            rows = self.buckets[bucket]
            data = b"".join(ENTRY.pack(key, *pointer) for key, pointer in rows.items())
            if len(data) > MAX_BUCKET_BYTES:
                raise CacheMiss()
            self.directory[bucket] = self.bank.tell(), len(data), hashlib.sha256(data).digest()
            self.bank.write(data)
        directory = b"".join(PAGE.pack(*pointer) for pointer in self.directory)
        location = [self.bank.tell(), hashlib.sha256(directory).hexdigest()]
        self.bank.write(directory)
        self.bank.flush()
        os.fsync(self.bank.fileno())
        return location, self.bank.tell()

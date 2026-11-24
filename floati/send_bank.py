"""Packed backing for disposable, individually authenticated send trie nodes.

The directory only locates bytes: neither an index miss nor a directory entry
proves membership or absence. Callers authenticate each selected node against
their sealed trie root, and recover from the native ledger on any miss.
"""
from __future__ import annotations

import hashlib
import os
import stat
import uuid

from . import jsonl
from .send_prefix import CacheMiss, _FIELDS


MAGIC = b"FLOATI-SEND-BANK\x00\x01"
HEADER_BYTES = len(MAGIC) + 8
ENTRY_BYTES = 44


def _open(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077 or info.st_nlink != 1
                or info.st_size > 64 * jsonl.MAX_LEDGER_BYTES):
            raise CacheMiss()
        return descriptor, info
    except BaseException:
        os.close(descriptor)
        raise


def _same(before, after):
    return all(getattr(before, field) == getattr(after, field) for field in _FIELDS)


def _range(descriptor, offset, size):
    parts = []
    os.lseek(descriptor, offset, os.SEEK_SET)
    while size:
        part = os.read(descriptor, min(size, 65536))
        if not part:
            raise CacheMiss()
        parts.append(part)
        size -= len(part)
    return b"".join(parts)


def read(path, digest):
    """Locate a node without loading the history-sized directory or bank."""
    descriptor, before = _open(path)
    try:
        header = _range(descriptor, 0, HEADER_BYTES)
        if not header.startswith(MAGIC):
            raise CacheMiss()
        count = int.from_bytes(header[len(MAGIC):], "big")
        directory_end = HEADER_BYTES + count * ENTRY_BYTES
        if directory_end > before.st_size:
            raise CacheMiss()
        wanted = bytes.fromhex(digest)
        low, high = 0, count
        while low < high:
            middle = (low + high) // 2
            entry = _range(descriptor, HEADER_BYTES + middle * ENTRY_BYTES, ENTRY_BYTES)
            if entry[:32] < wanted:
                low = middle + 1
            elif entry[:32] > wanted:
                high = middle
            else:
                offset = int.from_bytes(entry[32:40], "big")
                size = int.from_bytes(entry[40:], "big")
                if (not 0 < size <= 5 * jsonl.MAX_RECORD_BYTES or offset < directory_end
                        or size > before.st_size - offset):
                    raise CacheMiss()
                data = _range(descriptor, offset, size)
                if hashlib.sha256(data).digest() != wanted or not _same(before, os.fstat(descriptor)):
                    raise CacheMiss()
                return data
        # In particular, an incomplete or unsorted directory cannot turn a
        # missing historical proof into an authenticated negative answer.
        raise CacheMiss()
    finally:
        os.close(descriptor)


def publish(path, nodes):
    """Publish a complete cold bank with one replacement, retaining equal bytes.

    The checkpoint is published separately only after native validation and
    source identity checks. This file is a cache, not a durability barrier.
    """
    items = sorted(nodes.items())
    directory = bytearray(MAGIC + len(items).to_bytes(8, "big"))
    offset = HEADER_BYTES + len(items) * ENTRY_BYTES
    for digest, data in items:
        if not 0 < len(data) <= 5 * jsonl.MAX_RECORD_BYTES:
            raise CacheMiss()
        directory.extend(bytes.fromhex(digest) + offset.to_bytes(8, "big") + len(data).to_bytes(4, "big"))
        offset += len(data)

    def chunks():
        yield directory
        yield from (data for _digest, data in items)

    try:
        descriptor, before = _open(path)
        with os.fdopen(descriptor, "rb") as stream:
            if (before.st_size == offset and all(stream.read(len(chunk)) == chunk for chunk in chunks())
                    and _same(before, os.fstat(stream.fileno()))):
                return
    except (CacheMiss, OSError):
        pass
    temporary = path.with_name(".pending-" + uuid.uuid4().hex)
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.writelines(chunks())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)

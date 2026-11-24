"""Exact opaque epoch ZIP codec; no catalog or retirement authority.

Directory records are parsed incrementally: no unbounded central-directory blob
is allocated before checking the verified inventory. All reads verify the whole
container, including queries for absent members. Only requested bytes accumulate.
"""
from __future__ import annotations

from contextlib import contextmanager
from collections.abc import Sequence
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import struct
import zipfile
import zlib

from .errors import IntegrityFailure, ProtocolRefusal

_BUFFER = 64 * 1024
_HEX = re.compile(r"[0-9a-f]{64}\Z")
_CENTRAL = struct.Struct("<4s6H3I5H2I")
_LOCAL = struct.Struct("<4s5H3I2H")


def _invalid(detail):
    return IntegrityFailure("archive_container_invalid", detail)


def _plane(name):
    if (not isinstance(name, str) or not name or "\\" in name or "\x00" in name
            or any(part in {"", ".", ".."} for part in name.split("/"))):
        raise _invalid("member path is not a canonical native relative name")
    try:
        name.encode("utf-8")
    except UnicodeError as exc:
        raise _invalid("member path is not valid UTF-8") from exc
    if name == "events.jsonl":
        return "events"
    for plane in ("deliveries", "acks"):
        if name.startswith("receipts/" + plane + "/") and name.endswith(".jsonl"):
            return plane
    raise _invalid("member path is outside the native archive family")


def _inventory(inventory):
    if not isinstance(inventory, Sequence) or isinstance(inventory, (str, bytes)):
        raise _invalid("inventory must be a finite ordered sequence")
    rows, previous = [], None
    for row in inventory:
        if not isinstance(row, dict) or set(row) != {"path", "plane", "byte_length", "sha256"}:
            raise _invalid("inventory row has an invalid field set")
        plane = _plane(row["path"])
        key = row["path"].encode("utf-8")
        if previous is not None and key <= previous:
            raise _invalid("inventory names must be unique and sorted by UTF-8 bytes")
        if (row["plane"] != plane or type(row["byte_length"]) is not int
                or row["byte_length"] < 0 or not isinstance(row["sha256"], str)
                or _HEX.fullmatch(row["sha256"]) is None):
            raise _invalid("inventory plane, length or digest is invalid")
        rows.append(dict(row))
        previous = key
    if not rows or rows[0]["path"] != "events.jsonl":
        raise _invalid("inventory must contain exactly one native events member")
    return tuple(rows)


def _facts(rows):
    digest = hashlib.sha256()
    for row in rows:
        digest.update(json.dumps(row, ensure_ascii=False, allow_nan=False,
                                 sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n")
    return {"archive_sha256": digest.hexdigest(), "archive_file_count": len(rows),
            "plane_counts": {plane: sum(row["plane"] == plane for row in rows)
                             for plane in ("events", "deliveries", "acks")},
            "span": {"byte_start": 0, "byte_end": sum(row["byte_length"] for row in rows)}}


def _identity(status):
    return (status.st_dev, status.st_ino, status.st_mode, status.st_nlink,
            status.st_size, status.st_mtime_ns, status.st_ctime_ns)


def _same_named(parent, name, status):
    named = os.stat(name, dir_fd=parent, follow_symlinks=False)
    if _identity(named) != _identity(status):
        raise _invalid("container pathname or identity changed during access")


@contextmanager
def _parent(path):
    if not isinstance(path, Path) or not path.is_absolute() or ".." in path.parts:
        raise _invalid("container path must be an absolute canonical Path")
    descriptors, witnesses, yielded = [], [], False
    try:
        descriptor = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        descriptors.append(descriptor)
        current = Path(path.anchor)
        for part in path.parts[1:-1]:
            descriptor = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                 dir_fd=descriptor)
            descriptors.append(descriptor)
            current = current / part
            witnesses.append((current, os.fstat(descriptor)))
        def check():
            # Our own file creation changes the parent mtime. Bind directory
            # identity/mode instead, retaining descriptors for all ancestors.
            for name, original in witnesses:
                now = name.lstat()
                if ((now.st_dev, now.st_ino, now.st_mode) !=
                        (original.st_dev, original.st_ino, original.st_mode)):
                    raise _invalid("container parent identity changed during access")
        check()
        yielded = True
        yield descriptor, path.name, check
        check()
    except OSError as exc:
        if yielded:
            raise
        raise _invalid("container path is unavailable, aliased or changed") from exc
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _hash_file(handle, expected_length=None):
    handle.seek(0)
    digest, length = hashlib.sha256(), 0
    while True:
        block = handle.read(_BUFFER)
        if not block:
            break
        length += len(block)
        if expected_length is not None and length > expected_length:
            raise _invalid("container exceeds its verified compressed length")
        digest.update(block)
    return {"byte_length": length, "sha256": digest.hexdigest()}


def write_container(destination: Path, inventory, open_member):
    """Create only the absent destination, streaming exact verified sources.

    On failure the explicit destination is preserved, including uncertain
    partial output. This API holds no destination-directory exclusion and
    cannot make a pathname compare/unlink atomic. Disposition belongs to the
    caller under its separate verified ownership/exclusion contract.
    """
    rows = _inventory(inventory)
    if not callable(open_member):
        raise _invalid("open_member must supply binary readable context managers")
    try:
        with _parent(destination) as (parent, name, check):
            try:
                descriptor = os.open(name, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                     0o600, dir_fd=parent)
            except OSError as exc:
                raise ProtocolRefusal("archive_container_destination_invalid",
                                      "container destination must be absent and owned") from exc
            with os.fdopen(descriptor, "w+b") as handle:
                with zipfile.ZipFile(handle, "w", compression=zipfile.ZIP_DEFLATED,
                                     allowZip64=True) as container:
                    for row in rows:
                        info = zipfile.ZipInfo(row["path"], date_time=(1980, 1, 1, 0, 0, 0))
                        info.create_system = 3
                        info.external_attr = (stat.S_IFREG | 0o600) << 16
                        info.compress_type = zipfile.ZIP_DEFLATED
                        info.file_size = row["byte_length"]
                        digest, length = hashlib.sha256(), 0
                        with open_member(row["path"]) as source, container.open(info, "w") as target:
                            while True:
                                block = source.read(_BUFFER)
                                if not isinstance(block, bytes) or len(block) > _BUFFER:
                                    raise _invalid("source stream must return bounded binary bytes")
                                if not block:
                                    break
                                length += len(block)
                                if length > row["byte_length"]:
                                    raise _invalid("source exceeds its verified inventory length")
                                digest.update(block)
                                target.write(block)
                        if length != row["byte_length"] or digest.hexdigest() != row["sha256"]:
                            raise _invalid("source stream differs from its verified inventory")
                handle.flush()
                os.fsync(handle.fileno())
                before = os.fstat(handle.fileno())
                if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                    raise _invalid("completed container is not an ordinary single-link file")
                facts = _hash_file(handle, before.st_size)
                entries, start = _directory(handle, before.st_size, rows)
                _decode_members(handle, entries, start, rows, None)
                if _identity(os.fstat(handle.fileno())) != _identity(before):
                    raise _invalid("container changed while its completed output was verified")
                _same_named(parent, name, before)
                check()
                os.fsync(parent)
                return facts
    except IntegrityFailure as exc:
        if exc.detail.startswith("container path") or exc.detail.startswith("container parent"):
            raise ProtocolRefusal("archive_container_destination_invalid", exc.detail) from exc
        raise
    except (OSError, ValueError, struct.error, zlib.error, zipfile.BadZipFile, zipfile.LargeZipFile) as exc:
        raise _invalid("container creation or source read failed") from exc


def _read_exact(handle, size):
    blocks, remaining = [], size
    while remaining:
        block = handle.read(min(_BUFFER, remaining))
        if not block:
            raise _invalid("container metadata or payload is truncated")
        blocks.append(block)
        remaining -= len(block)
    return b"".join(blocks)


def _zip64(extra, fields):
    """Resolve sentinel fields in ZIP64's defined field order."""
    found, offset = None, 0
    while offset < len(extra):
        if len(extra) - offset < 4:
            raise _invalid("ZIP extra field is truncated")
        kind, length = struct.unpack_from("<HH", extra, offset)
        offset += 4
        data = extra[offset:offset + length]
        if len(data) != length:
            raise _invalid("ZIP extra field length is invalid")
        offset += length
        if kind == 1:
            if found is not None:
                raise _invalid("duplicate ZIP64 metadata")
            found = data
    result, offset = [], 0
    for value, sentinel, width in fields:
        if value == sentinel:
            if found is None or len(found) - offset < width:
                raise _invalid("required ZIP64 metadata is missing")
            value = int.from_bytes(found[offset:offset + width], "little")
            offset += width
        result.append(value)
    if found is not None and offset != len(found):
        raise _invalid("unsupported ZIP64 metadata")
    return result


def _directory(handle, length, rows):
    # Single-disk ZIP/ZIP64, no archive-level comment, executable prefix or
    # trailing data. Unsupported representations must preserve legacy bytes.
    if length < 22:
        raise _invalid("container has no complete ZIP end record")
    handle.seek(length - 22)
    end = struct.unpack("<4s4H2IH", _read_exact(handle, 22))
    if end[0] != b"PK\x05\x06" or end[1] or end[2] or end[7]:
        raise _invalid("unsupported ZIP end record")
    count, size, start, directory_end = end[4], end[5], end[6], length - 22
    if end[3] != count:
        raise _invalid("ZIP spans multiple disks")
    # ZIP64 end records are legal even when the classic fields still fit.
    # Conversely, exactly 65535 entries can use the ordinary end record.
    if start + size != directory_end:
        handle.seek(length - 42)
        locator = struct.unpack("<4sIQI", _read_exact(handle, 20))
        if (locator[0] != b"PK\x06\x07" or locator[1] or locator[3] != 1
                or locator[2] + 56 != length - 42):
            raise _invalid("unsupported ZIP64 locator")
        handle.seek(locator[2])
        large = struct.unpack("<4sQ2H2I4Q", _read_exact(handle, 56))
        if (large[0] != b"PK\x06\x06" or large[1] != 44 or large[4] or large[5]
                or large[6] != large[7] or locator[2] + 56 != length - 42):
            raise _invalid("unsupported ZIP64 end record")
        count, size, start, directory_end = large[7], large[8], large[9], locator[2]
        if (end[4] != min(count, 65535) or end[5] != min(size, 0xffffffff)
                or end[6] != min(start, 0xffffffff)):
            raise _invalid("classic and ZIP64 end records disagree")
    if count != len(rows) or start + size != directory_end:
        raise _invalid("ZIP directory count or byte range differs from inventory")
    by_name, entries, seen = {row["path"]: row for row in rows}, [], set()
    handle.seek(start)
    for index in range(count):
        if handle.tell() + _CENTRAL.size > directory_end:
            raise _invalid("ZIP directory exceeds its verified byte range")
        values = _CENTRAL.unpack(_read_exact(handle, _CENTRAL.size))
        (signature, creator, version, flags, method, time, date, crc, compressed,
         expanded, namesize, extrasize, commentsize, disk, internal, external, offset) = values
        if signature != b"PK\x01\x02":
            raise _invalid("ZIP directory member signature is invalid")
        if handle.tell() + namesize + extrasize + commentsize > directory_end:
            raise _invalid("ZIP member metadata exceeds its directory range")
        raw_name = _read_exact(handle, namesize)
        extra = _read_exact(handle, extrasize)
        _read_exact(handle, commentsize)
        try:
            name = raw_name.decode("utf-8" if flags & 0x800 else "cp437")
        except UnicodeError as exc:
            raise _invalid("ZIP member name encoding is invalid") from exc
        _plane(name)
        if name not in by_name or name in seen:
            raise _invalid("ZIP contains extra, duplicate or unknown members")
        expanded, compressed, offset, disk = _zip64(extra, (
            (expanded, 0xffffffff, 8), (compressed, 0xffffffff, 8),
            (offset, 0xffffffff, 8), (disk, 65535, 4)))
        mode = external >> 16
        if (flags & ~0x800 or method != zipfile.ZIP_DEFLATED or disk
                or version > 45 or stat.S_IFMT(mode) not in {0, stat.S_IFREG}
                or external & 0x18 or expanded != by_name[name]["byte_length"]
                or compressed > length or offset >= start):
            raise _invalid("ZIP member type, flags, format or lengths are invalid")
        seen.add(name)
        entries.append((name, raw_name, flags, method, crc, compressed, expanded, offset))
    if handle.tell() != directory_end:
        raise _invalid("ZIP directory has extra entries or trailing metadata")
    return entries, start


def _decode_members(handle, entries, directory_start, rows, selected):
    by_name, result, next_offset, total = {row["path"]: row for row in rows}, None, 0, 0
    multiple = isinstance(selected, frozenset)
    if multiple:
        result = {}
    for name, raw_name, flags, method, crc, compressed, expanded, offset in sorted(entries, key=lambda entry: entry[-1]):
        if offset != next_offset:
            raise _invalid("ZIP member ranges overlap or contain unowned bytes")
        handle.seek(offset)
        local = _LOCAL.unpack(_read_exact(handle, _LOCAL.size))
        signature, version, local_flags, local_method, time, date, local_crc, packed, unpacked, namesize, extrasize = local
        if handle.tell() + namesize + extrasize + compressed > directory_start:
            raise _invalid("ZIP payload exceeds its container byte range")
        local_name, extra = _read_exact(handle, namesize), _read_exact(handle, extrasize)
        unpacked, packed = _zip64(extra, ((unpacked, 0xffffffff, 8), (packed, 0xffffffff, 8)))
        if (signature != b"PK\x03\x04" or version > 45 or local_flags != flags
                or local_method != method or local_name != raw_name or local_crc != crc
                or packed != compressed or unpacked != expanded):
            raise _invalid("ZIP local header differs from directory testimony")
        inflater, digest, actual_crc, length = zlib.decompressobj(-15), hashlib.sha256(), 0, 0
        output = bytearray() if (name in selected if multiple else name == selected) else None
        remaining = compressed
        while remaining:
            pending = _read_exact(handle, min(_BUFFER, remaining))
            remaining -= len(pending)
            while True:
                block = inflater.decompress(pending, min(_BUFFER, expanded - length + 1))
                pending = inflater.unconsumed_tail
                length += len(block)
                if length > expanded:
                    raise _invalid("decoded member exceeds its verified inventory length")
                digest.update(block)
                actual_crc = zlib.crc32(block, actual_crc)
                if output is not None:
                    output.extend(block)
                if inflater.unused_data or (inflater.eof and (pending or remaining)):
                    raise _invalid("compressed member contains trailing or extra streams")
                # max_length may leave decoded output inside zlib even after
                # all input was consumed. Drain it with empty input, bounded
                # by the same exact inventory quota, before asking for more.
                if not pending and (not block or inflater.eof):
                    break
        if (not inflater.eof or length != expanded or actual_crc != crc
                or digest.hexdigest() != by_name[name]["sha256"]):
            raise _invalid("decoded member length, CRC or SHA-256 differs from inventory "
                           f"({length}/{expanded} bytes, complete={inflater.eof})")
        next_offset = handle.tell()
        total += length
        if output is not None:
            if multiple:
                result[name] = bytes(output)
            else:
                result = bytes(output)
    if next_offset != directory_start or total != sum(row["byte_length"] for row in rows):
        raise _invalid("decoded inventory or compressed byte coverage is incomplete")
    return result


def _verify(path, container_facts, rows, selected):
    if (not isinstance(container_facts, dict) or set(container_facts) != {"byte_length", "sha256"}
            or type(container_facts["byte_length"]) is not int or container_facts["byte_length"] < 0
            or not isinstance(container_facts["sha256"], str)
            or _HEX.fullmatch(container_facts["sha256"]) is None):
        raise _invalid("container facts must name exact length and SHA-256")
    try:
        with _parent(path) as (parent, name, check):
            descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            with os.fdopen(descriptor, "rb") as handle:
                before = os.fstat(handle.fileno())
                if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
                        or before.st_size != container_facts["byte_length"]):
                    raise _invalid("container is not the exact ordinary single-link file")
                _same_named(parent, name, before)
                if _hash_file(handle, container_facts["byte_length"]) != container_facts:
                    raise _invalid("container length or SHA-256 differs from verified facts")
                entries, start = _directory(handle, before.st_size, rows)
                result = _decode_members(handle, entries, start, rows, selected)
                if _identity(os.fstat(handle.fileno())) != _identity(before):
                    raise _invalid("container changed during its bound read")
                _same_named(parent, name, before)
                check()
                return result
    except (OSError, ValueError, struct.error, zlib.error) as exc:
        raise _invalid("container is unavailable, malformed or corrupt") from exc


def verify_container(path: Path, container_facts, inventory):
    """Verify both representations and derive four original archive facts."""
    rows = _inventory(inventory)
    _verify(path, dict(container_facts) if isinstance(container_facts, dict) else container_facts, rows, None)
    return _facts(rows)


def read_container_member(path: Path, container_facts, inventory, relative: str):
    """Return exact bytes or None after complete container verification."""
    _plane(relative)
    rows = _inventory(inventory)
    return _verify(path, dict(container_facts) if isinstance(container_facts, dict) else container_facts, rows, relative)


def container_facts(path: Path):
    """Hash one ordinary container file without accepting its members."""
    try:
        with _parent(path) as (parent, name, check):
            descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            with os.fdopen(descriptor, "rb") as handle:
                before = os.fstat(handle.fileno())
                if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                    raise _invalid("container is not an ordinary single-link file")
                facts = _hash_file(handle, before.st_size)
                _same_named(parent, name, before)
                check()
                return facts
    except OSError as exc:
        raise _invalid("container is unavailable, malformed or corrupt") from exc


def export_members(path: Path):
    """Return verified container facts and exact member bytes.

    Candidate bytes are inflated only to build the inventory the strict
    decoder then rechecks. Returned payloads are the decoder's bytes.
    """
    facts = container_facts(path)
    try:
        with zipfile.ZipFile(path) as container:
            names = [info.filename for info in container.infolist()]
            inflated = {}
            for name in names:
                with container.open(name) as stream:
                    payload = bytearray()
                    while True:
                        block = stream.read(_BUFFER)
                        if not block:
                            break
                        payload.extend(block)
                    inflated[name] = bytes(payload)
    except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile, ValueError) as exc:
        raise _invalid("container is unavailable, malformed or corrupt") from exc
    inventory = [{"path": name, "plane": _plane(name), "byte_length": len(inflated[name]),
                  "sha256": hashlib.sha256(inflated[name]).hexdigest()}
                 for name in sorted(inflated, key=lambda value: value.encode("utf-8"))]
    # Admit every requested member in the same complete strict verification.
    # Repeating a whole-container verification per member is quadratic work.
    payloads = _verify(path, facts, _inventory(inventory), frozenset(inflated))
    if any(payloads[row["path"]] != inflated[row["path"]] for row in inventory):
        raise _invalid("decoded member bytes differ from the container inventory")
    return facts, payloads

"""Offline integrity of the exact existing transport selected for a new seat.

These are the static manifest rules used by the governed Codex gateway. No
gateway invocation, interpreter execution, installation or root discovery occurs.
The transport's Floati source pin is independent of the product being cloned.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from pathlib import Path, PurePosixPath

from .errors import ProtocolRefusal
from .install_limits import (
    MAX_INSTALL_FILES, MAX_INSTALL_FILE_BYTES, MAX_INSTALL_TOTAL_BYTES, MAX_MANIFEST_BYTES,
)


TRANSPORT_FIELDS = {"kind", "install_root", "manifest_path", "manifest_sha256", "source_sha",
                    "interpreter", "max_files", "max_file_bytes", "max_total_bytes"}
MANIFEST_FIELDS = {"schema_version", "source_ref", "source_sha", "files"}
FIXED_CLIENT_FILES = frozenset({"floati/__init__.py", "floati/__main__.py", "floati/cli.py",
                               "floati/codex_hook_install.py", "floati/codex_wait.py"})


def _refuse(detail):
    raise ProtocolRefusal("seat_transport_preflight_invalid", detail,
        remedy="restore the selected installed transport's exact pinned files and ordinary executable interpreter, or review corrected transport pins before retrying seat add")


def _path(value):
    if not isinstance(value, str) or not value or any(ord(char) < 32 or ord(char) == 127 for char in value):
        _refuse("transport coordinate must be a printable absolute path")
    path = Path(value)
    if not path.is_absolute() or str(path) != value or ".." in path.parts:
        _refuse("transport coordinate must be absolute and normalized")
    for component in (*reversed(path.parents), path):
        if component.is_symlink():
            _refuse("transport coordinate traverses a symlink: " + str(path))
    if path.resolve(strict=True) != path:
        _refuse("transport coordinate is not canonical: " + str(path))
    return path


def _open_parent(path):
    """Open each ordinary parent without following a raced symlink."""
    descriptor = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for component in path.parts[1:-1]:
            child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                            dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _read(path, maximum):
    path = _path(str(path))
    parent = _open_parent(path)
    descriptor = None
    try:
        descriptor = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                             dir_fd=parent)
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or not 0 <= before.st_size <= maximum:
            _refuse("transport input is not a bounded ordinary file: " + str(path))
        pieces = []
        remaining = maximum + 1
        while remaining:
            piece = os.read(descriptor, min(1048576, remaining))
            if not piece:
                break
            pieces.append(piece)
            remaining -= len(piece)
        encoded = b"".join(pieces)
        after = os.fstat(descriptor)
        _path(str(path))
        current_parent = _open_parent(path)
        try:
            original_parent, observed_parent = os.fstat(parent), os.fstat(current_parent)
            if (original_parent.st_dev, original_parent.st_ino) != (observed_parent.st_dev, observed_parent.st_ino):
                _refuse("transport input parent changed during its read: " + str(path))
            current = os.stat(path.name, dir_fd=current_parent, follow_symlinks=False)
        finally:
            os.close(current_parent)
        def identity(metadata):
            return (metadata.st_dev, metadata.st_ino, metadata.st_size, metadata.st_mtime_ns,
                    metadata.st_ctime_ns, metadata.st_mode, metadata.st_uid)
        if len(encoded) != before.st_size or len(encoded) > maximum or identity(before) != identity(after) or identity(after) != identity(current):
            _refuse("transport input changed during its read: " + str(path))
        return encoded
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(parent)


def _unique(pairs):
    result = {}
    for name, value in pairs:
        if name in result:
            _refuse("installed transport manifest repeats a JSON field")
        result[name] = value
    return result


def _preflight(transport):
    if not isinstance(transport, dict) or set(transport) != TRANSPORT_FIELDS or transport["kind"] != "floati-install-manifest-v0":
        _refuse("selected transport requires the exact manifest-pinned metadata shape")
    for field, length in (("source_sha", 40), ("manifest_sha256", 64)):
        if not isinstance(transport[field], str) or re.fullmatch("[0-9a-f]{" + str(length) + "}", transport[field]) is None:
            _refuse("selected transport pin is invalid: " + field)
    for field, ceiling in (("max_files", MAX_INSTALL_FILES),
                           ("max_file_bytes", MAX_INSTALL_FILE_BYTES),
                           ("max_total_bytes", MAX_INSTALL_TOTAL_BYTES)):
        if type(transport[field]) is not int or not 1 <= transport[field] <= ceiling:
            _refuse("selected transport bound is invalid: " + field)
    if transport["max_file_bytes"] > transport["max_total_bytes"]:
        _refuse("selected transport file bound exceeds its total bound")
    install = _path(transport["install_root"])
    if not stat.S_ISDIR(install.lstat().st_mode):
        _refuse("selected transport install is not an ordinary directory")
    manifest = _path(transport["manifest_path"])
    if manifest != install / ".floati-install/manifest.v0.json":
        _refuse("selected transport manifest is outside its exact install metadata coordinate")
    interpreter = _path(transport["interpreter"])
    if not stat.S_ISREG(interpreter.lstat().st_mode) or not os.access(interpreter, os.X_OK):
        _refuse("selected transport interpreter is not an ordinary executable file")
    encoded = _read(manifest, MAX_MANIFEST_BYTES)
    if hashlib.sha256(encoded).hexdigest() != transport["manifest_sha256"]:
        _refuse("selected transport manifest differs from its pinned digest")
    document = json.loads(encoded.decode("utf-8"), object_pairs_hook=_unique)
    if not isinstance(document, dict) or not MANIFEST_FIELDS <= set(document) or set(document) - MANIFEST_FIELDS - {"ownership"}:
        _refuse("installed transport manifest has unknown or missing fields")
    if "ownership" in document and not isinstance(document["ownership"], dict):
        _refuse("installed transport manifest ownership is malformed")
    if type(document["schema_version"]) is not int or document["schema_version"] not in (0, 1) or document["source_sha"] != transport["source_sha"]:
        _refuse("installed transport manifest schema or source pin disagrees")
    reference = document["source_ref"]
    if not isinstance(reference, str) or not reference or len(reference.encode("utf-8")) > 1024 or any(char.isspace() for char in reference):
        _refuse("installed transport manifest source reference is invalid")
    entries = document["files"]
    if not isinstance(entries, list) or not entries or len(entries) > transport["max_files"]:
        _refuse("installed transport manifest file count is outside its bound")
    seen, total = set(), 0
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {"path", "sha256"}:
            _refuse("installed transport manifest entry has unknown or missing fields")
        name, digest = entry["path"], entry["sha256"]
        if not isinstance(name, str) or not name or not isinstance(digest, str) or re.fullmatch("[0-9a-f]{64}", digest) is None:
            _refuse("installed transport manifest entry is malformed")
        relative = PurePosixPath(name)
        if relative.is_absolute() or str(relative) != name or not relative.parts or ".." in relative.parts or name in seen:
            _refuse("installed transport manifest entry path is unsafe or duplicated")
        seen.add(name)
        content = _read(install.joinpath(*relative.parts), transport["max_file_bytes"])
        if hashlib.sha256(content).hexdigest() != digest:
            _refuse("installed transport client differs from its manifest digest: " + name)
        total += len(content)
        if total > transport["max_total_bytes"]:
            _refuse("installed transport exceeds its total byte bound")
    if not FIXED_CLIENT_FILES <= seen:
        _refuse("installed transport manifest is missing a fixed gateway client entry")
    return {"state": "verified", "install_root": str(install), "manifest_path": str(manifest),
            "interpreter": str(interpreter), "source_sha": transport["source_sha"],
            "manifest_sha256": transport["manifest_sha256"], "file_count": len(entries), "total_bytes": total}


def preflight_transport(transport: dict) -> dict:
    """Verify one explicit selected transport without writes or execution."""
    try:
        return _preflight(transport)
    except ProtocolRefusal:
        raise
    except (OSError, UnicodeError, ValueError, RuntimeError) as exc:
        _refuse("selected transport cannot be verified: " + type(exc).__name__)

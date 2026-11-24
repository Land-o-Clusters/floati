"""Bind Claude's terminal-only identity to its exact parent-frozen draft.

Preparation never writes. The writer derives identity and bytes from durable
native testimony and banking intent; it accepts no caller identity override.
"""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import stat
import unicodedata

from .adhoc_records import canonical_json, refuse

MAX_RESULT_BYTES = 1_048_576


def _strict_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _decode(raw):
    try:
        if type(raw) is not bytes or not 0 < len(raw) <= MAX_RESULT_BYTES:
            raise ValueError()
        value = json.loads(raw.decode("utf-8", "strict"), object_pairs_hook=_strict_object,
            parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
        if type(value) is not dict:
            raise ValueError()
        return value
    except (ValueError, UnicodeError, RecursionError, OverflowError):
        refuse("adhoc_result_disposition_invalid", "Claude identity binding requires bounded strict draft JSON")


def _workspace_owner(workspace):
    from .adhoc_workspace import BorrowedWorkspace
    from .root import FloatiRoot
    if (type(workspace) is not BorrowedWorkspace or type(workspace.root) is not FloatiRoot
        or workspace._pin is None or workspace._pin["profile"]["adapter"] != "claude"):
        refuse("adhoc_binding_changed", "Claude identity binding requires its exact descriptor-owned workspace")
    workspace._check_pinned_files()


def _relative(relative):
    try:
        size = len(relative.encode("utf-8", "strict")) if type(relative) is str else 0
    except UnicodeError:
        size = MAX_RESULT_BYTES
    if (type(relative) is not str or not relative or size > 4096
        or Path(relative).is_absolute() or str(Path(relative)) != relative
        or any(part.casefold() in {"", ".", "..", ".git"} for part in relative.split("/"))
        or any(unicodedata.category(c).startswith("C") for c in relative)):
        refuse("adhoc_result_disposition_invalid", "Claude identity binding requires an ordinary project result path")
    return relative.split("/")


def _fingerprint(metadata):
    return (metadata.st_dev, metadata.st_ino, metadata.st_size,
            metadata.st_mtime_ns, metadata.st_ctime_ns, metadata.st_mode, metadata.st_nlink)


def _same_entry(parent, name, descriptor):
    try:
        current = os.stat(name, dir_fd=parent, follow_symlinks=False)
        held = os.fstat(descriptor)
    except OSError:
        refuse("adhoc_binding_changed", "Claude result inode changed before identity binding")
    if (not stat.S_ISREG(current.st_mode) or not stat.S_ISREG(held.st_mode)
        or current.st_uid != os.geteuid() or held.st_uid != os.geteuid()
        or current.st_nlink != 1 or held.st_nlink != 1
        or (current.st_dev, current.st_ino) != (held.st_dev, held.st_ino)):
        refuse("adhoc_binding_changed", "Claude result inode changed before identity binding")
    return held


@contextmanager
def _document(workspace, relative, *, writable=False):
    _workspace_owner(workspace)
    parts = _relative(relative)
    parent = os.dup(workspace._fds["workspace"])
    descriptor = None
    try:
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            os.close(parent)
            parent = child
        descriptor = os.open(parts[-1], (os.O_RDWR if writable else os.O_RDONLY)
            | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        _same_entry(parent, parts[-1], descriptor)
        yield descriptor, parent, parts[-1]
    except OSError:
        refuse("adhoc_result_disposition_invalid", "Claude identity binding requires an owned unaliased ordinary result inode")
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(parent)


def _read(descriptor):
    before = os.fstat(descriptor)
    if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.geteuid()
        or before.st_nlink != 1 or not 0 < before.st_size <= MAX_RESULT_BYTES):
        refuse("adhoc_result_disposition_invalid", "Claude identity binding requires an owned unaliased ordinary result inode")
    data = bytearray()
    while len(data) <= MAX_RESULT_BYTES:
        chunk = os.pread(descriptor, min(65_536, MAX_RESULT_BYTES + 1 - len(data)), len(data))
        if not chunk:
            break
        data.extend(chunk)
    after = os.fstat(descriptor)
    if len(data) != before.st_size or _fingerprint(before) != _fingerprint(after):
        refuse("adhoc_binding_changed", "Claude result changed during its bounded identity read")
    return bytes(data), after


def _normalized(raw, coordinate):
    from .adhoc_records import _native_coordinate
    _native_coordinate(coordinate, refuse)
    if coordinate["provider"] != "claude_local":
        refuse("native_session_mismatch", "only authenticated Claude terminal identity can bind a null draft")
    value = _decode(raw)
    if value.get("provider_coordinate", object()) is not None:
        refuse("adhoc_result_disposition_invalid", "Claude identity input must explicitly declare a null coordinate")
    value["provider_coordinate"] = deepcopy(coordinate)
    try:
        output = canonical_json(value) + b"\n"
    except (ValueError, UnicodeError, RecursionError, OverflowError):
        refuse("adhoc_result_disposition_invalid", "Claude identity binding requires bounded strict draft JSON")
    if len(output) > MAX_RESULT_BYTES:
        refuse("native_protocol_limit", "bound Claude result exceeds its fixed document budget")
    return output


def prepare_claude_result_identity(workspace, relative, raw, observed_coordinate):
    """Return draft-derived bytes and inode metadata; perform no mutation.

    The controller must validate the complete normalized result before freezing
    this metadata in banking intent. The writer later ignores caller identity.
    """
    output = _normalized(raw, observed_coordinate)
    with _document(workspace, relative) as (descriptor, parent, name):
        actual, metadata = _read(descriptor)
        _same_entry(parent, name, descriptor)
        if actual != raw:
            refuse("adhoc_binding_changed", "Claude result differs from the draft selected for identity binding")
        binding = {"path": relative, "dev": metadata.st_dev, "ino": metadata.st_ino,
            "mode": "100755" if metadata.st_mode & 0o111 else "100644",
            "input_sha256": hashlib.sha256(raw).hexdigest(),
            "output_sha256": hashlib.sha256(output).hexdigest()}
    return output, binding


def bind_claude_result_identity(root, dispatch_id, workspace):
    """Bind only exact frozen input or accept exact previously bound output.

    Unknown partial writes, replacement paths, aliases and changed content are
    retained and refused. No recovery branch repairs unrecognized bytes.
    """
    from .adhoc_journal import AdhocJournal
    from .adhoc_workspace import BorrowedWorkspace
    from .root import FloatiRoot
    if type(root) is not FloatiRoot or type(workspace) is not BorrowedWorkspace or workspace.root is not root:
        refuse("adhoc_binding_changed", "Claude identity binding requires its exact descriptor-owned workspace")
    journal = AdhocJournal(root)
    admission = journal.show(dispatch_id)
    phases = {(row["phase"], row["evidence"].get("stage")): row["evidence"]
              for row in journal.lifecycle(dispatch_id)}
    intent = phases.get(("process_closed", "banking_intent"))
    terminal = phases.get(("provider_terminal", None), {}).get("event")
    cleanup = phases.get(("process_closed", "cleanup"), {}).get("cleanup")
    reservation = phases.get(("spawn_reserved", None))
    if (intent is None or "identity_binding" not in intent or reservation is None
        or terminal is None or terminal["outcome"] != "success" or cleanup is None
        or cleanup["alive_after_cleanup"] or cleanup["adapter_pid"] is None
        or cleanup["isolation_backend"] is None
        or any((phase, None) in phases for phase in ("failed", "unknown", "teardown_completed"))):
        refuse("adhoc_binding_changed", "Claude identity binding requires durable native terminal and cleanup intent")
    candidate, binding = intent["result_candidate"], intent["identity_binding"]
    coordinate = terminal["provider_coordinate"]
    if (coordinate["provider"] != "claude_local" or candidate["provider_coordinate"] != coordinate
        or candidate["doc"] != admission["dispatch"]["result_document"]
        or binding["path"] != candidate["doc"]
        or candidate["blob_sha256"] != binding["output_sha256"]
        or workspace.binding != reservation["native_context"]["workspace_binding"]
        or workspace.binding["profile_id"] != admission["profile_id"]):
        refuse("native_session_mismatch", "Claude identity binding differs from authenticated terminal testimony")
    output = canonical_json({"schema_version": 1, "dispatch_digest": admission["dispatch_digest"],
        **{field: candidate[field] for field in ("provider_coordinate", "summary", "inbox_dispositions")}}) + b"\n"
    if hashlib.sha256(output).hexdigest() != binding["output_sha256"]:
        refuse("adhoc_binding_changed", "Claude bound output differs from its immutable banking intent")
    if workspace._pin is None:
        workspace._pin_workspace(clean=False)
    _workspace_owner(workspace)
    if (workspace._pin["profile"]["id"] != admission["profile_id"]
        or hashlib.sha256(workspace._read_fd("index")).hexdigest() != reservation["workspace_pin"]["index_sha256"]):
        refuse("adhoc_binding_changed", "Claude identity binding differs from the reserved workspace index")
    with _document(workspace, binding["path"], writable=True) as (descriptor, parent, name):
        raw, metadata = _read(descriptor)
        mode = "100755" if metadata.st_mode & 0o111 else "100644"
        if (metadata.st_dev != binding["dev"] or metadata.st_ino != binding["ino"] or mode != binding["mode"]):
            refuse("adhoc_binding_changed", "Claude result inode changed before identity binding")
        digest = hashlib.sha256(raw).hexdigest()
        if digest == binding["output_sha256"] and raw == output:
            return deepcopy(binding)
        if digest != binding["input_sha256"] or _normalized(raw, coordinate) != output:
            refuse("adhoc_binding_changed", "Claude result differs from the exact frozen input or bound output")
        current = workspace._git("rev-parse", "refs/heads/" + workspace._pin["profile"]["branch"]).decode().strip()
        if current != reservation["workspace_pin"]["expected_head"]:
            refuse("adhoc_binding_changed", "Claude draft cannot be rebound after its banking branch advanced")
        workspace._check_pinned_files()
        if _fingerprint(_same_entry(parent, name, descriptor)) != _fingerprint(metadata):
            refuse("adhoc_binding_changed", "Claude result changed before its identity write")
        checked, before_write = _read(descriptor)
        if checked != raw or _fingerprint(before_write) != _fingerprint(metadata):
            refuse("adhoc_binding_changed", "Claude result changed before its identity write")
        from .adhoc_profiles import current_time
        from .work import WorkLog
        current = current_time(None)
        profile = workspace._pin["profile"]
        workspace.profiles._lease(profile["node_id"], current, expected=profile["node_lease_id"])
        WorkLog(root)._require_authority(admission["dispatch"]["authority_subject"], admission["actor"],
            admission["dispatch"]["authority_epoch"], current)
        offset = 0
        while offset < len(output):
            written = os.pwrite(descriptor, output[offset:], offset)
            if written <= 0:
                refuse("adhoc_binding_changed", "Claude identity write did not complete")
            offset += written
        os.ftruncate(descriptor, len(output))
        os.fsync(descriptor)
        actual, _after = _read(descriptor)
        _same_entry(parent, name, descriptor)
        workspace._check_pinned_files()
        if actual != output:
            refuse("adhoc_binding_changed", "Claude identity write differs from its frozen output")
    return deepcopy(binding)

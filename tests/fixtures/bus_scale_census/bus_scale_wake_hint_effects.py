"""Read-only proof of exact native wake-hint creation during census writes."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import stat

from floati.framing import decode_frames, encode_frame
from floati.records import validate_record
from floati.root import FloatiRoot, validate_identifier


def _require(value, detail):
    if not value:
        raise ValueError("native wake hint effect: " + detail)


def _family(path):
    return type(path) is str and (path == "state/wake-hints" or path.startswith("state/wake-hints/"))


def _recipient(node):
    return "state/wake-hints/" + validate_identifier(node, "node") + "/generation"


def _permitted(root, before, after, ledger_kinds):
    """Route only actual canonical appends in the caller's closed ledger set."""
    permitted = set()
    for relative, kinds in ledger_kinds.items():
        old, new = before.get(relative, b""), after.get(relative, b"")
        if old == new:
            continue
        _require(type(old) is bytes and type(new) is bytes and new.startswith(old),
                 "selected native ledger did not retain its prefix")
        rows = decode_frames(new[len(old):])
        _require(b"".join(encode_frame(row) for row in rows) == new[len(old):],
                 "selected native append is not canonical")
        for row in rows:
            validate_record(row, root.tenant_id, kinds, integrity=True)
        if relative == "events.jsonl":
            for row in rows:
                if isinstance(row.get("recipient"), str):
                    permitted.add(_recipient(row["recipient"]))
                else:
                    permitted.add("state/wake-hints/root-generation")
        elif relative == "registry/entries.jsonl" and rows:
            permitted.add("state/wake-hints/root-generation")
        else:
            parts = Path(relative).parts
            if len(parts) != 3 or parts[0] != "receipts" or not parts[2].endswith(".jsonl"):
                continue
            node = parts[2][:-len(".jsonl")]
            if parts[1] in {"deliveries", "acks", "codex-wait-consent", "codex-wait-session", "cursor-presentations"} and rows:
                permitted.add(_recipient(node))
            elif parts[1] == "wake-control":
                for row in rows:
                    _require(row["kind"] == "wake_control_receipt" and row["node_id"] == node,
                             "wake-control hint does not match its native actor")
                    if row["operation"] in {"pause", "resume"}:
                        permitted.add(_recipient(node))
    return permitted


def _metadata(home, relative):
    _require(home.is_absolute() and home.resolve(strict=True) == home,
             "root is not the exact canonical coordinate")
    owner = os.geteuid()
    root_info = home.lstat()
    _require(stat.S_ISDIR(root_info.st_mode) and root_info.st_uid == owner,
             "root is not an owned ordinary directory")
    descriptors, lineage = [], []
    def identity(info):
        return info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_nlink
    try:
        parent = os.open(home, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        descriptors.append(parent)
        _require(identity(os.fstat(parent)) == identity(root_info), "root identity changed")
        parts = Path(relative).parts
        for index, component in enumerate(parts):
            leaf = index == len(parts) - 1
            flags = os.O_RDONLY | os.O_NOFOLLOW | (os.O_NONBLOCK if leaf else os.O_DIRECTORY)
            descriptor = os.open(component, flags, dir_fd=parent)
            descriptors.append(descriptor)
            lineage.append((parent, component, descriptor))
            info = os.fstat(descriptor)
            _require(info.st_uid == owner, "hint has a foreign owner")
            if leaf:
                _require(stat.S_ISREG(info.st_mode) and stat.S_IMODE(info.st_mode) == 0o600
                         and info.st_nlink == 1 and info.st_size == 0,
                         "hint is not a private empty ordinary file")
                _require(os.read(descriptor, 1) == b"", "hint bytes changed during proof")
            else:
                _require(stat.S_ISDIR(info.st_mode), "hint parent is not an ordinary directory")
                _require(not info.st_mode & (0o022 if index == 0 else 0o077),
                         "hint parent permissions differ from the native publisher")
            parent = descriptor
        for ancestor, name, descriptor in lineage:
            _require(identity(os.stat(name, dir_fd=ancestor, follow_symlinks=False))
                     == identity(os.fstat(descriptor)), "hint namespace changed during proof")
        def retained_identity(info):
            # A native operation may add sibling directories, changing a
            # parent's link count. Its inode, owner, group and mode still
            # identify the exact retained lineage; the ordinary leaf must
            # keep its sole link as well.
            return [info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_gid,
                    info.st_nlink if stat.S_ISREG(info.st_mode) else None]
        return {"root": retained_identity(root_info),
                "lineage": [{"component": name, "identity": retained_identity(os.fstat(descriptor))}
                            for _ancestor, name, descriptor in lineage],
                "mtime_ns": os.fstat(descriptors[-1]).st_mtime_ns}
    except OSError as error:
        raise ValueError("native wake hint effect: ordinary hint metadata unavailable") from error
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def verify_wake_hint_effects(root, before, after, delta, *, ledger_kinds):
    """Retain each changed hint; never exempt a namespace or mutate evidence."""
    _require(isinstance(root, FloatiRoot), "root is not validated")
    _require(type(after) is dict and type(delta) is dict, "raw evidence is malformed")
    changed_in_delta = {path for path in delta if _family(path)}
    if before is None:
        _require(not changed_in_delta, "hint creation requires retained immediate before bytes")
        return {}
    _require(type(before) is dict, "retained raw evidence is malformed")
    changed = {path for path in set(before) | set(after)
               if _family(path) and before.get(path) != after.get(path)}
    _require(changed_in_delta == changed, "complete changed hint population differs")
    if not changed:
        return {}
    permitted = _permitted(root, before, after, ledger_kinds)
    empty_sha = hashlib.sha256(b"").hexdigest()
    exact = {"operation": "append", "before_sha256": empty_sha, "after_sha256": empty_sha,
             "before_bytes": 0, "after_bytes": 0, "payload_base64": ""}
    result = {}
    for path in sorted(changed):
        _require(path in permitted, "unreviewed native hint coordinate: " + path)
        _require(path not in before and after.get(path) == b"",
                 "only absent-to-empty native hint creation is reviewed")
        candidate = delta[path]
        _require(type(candidate) is dict and set(candidate) == set(exact)
                 and all(type(candidate[key]) is type(value) and candidate[key] == value
                         for key, value in exact.items()),
                 "hint delta does not bind exact native empty bytes")
        _metadata(root.tenant_home, path)
        result[path] = {"operation": "create_empty_hint", "kind": "native_wake_hint"}
    return result

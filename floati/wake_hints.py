"""Disposable writer hints: invalidation only, never ledger admission."""
from __future__ import annotations

import os
import select
import stat
from pathlib import Path

from .errors import ProtocolRefusal
from .root import FloatiRoot, validate_identifier


ROOT_HINT_RELATIVE = Path("state/wake-hints/root-generation")
SAFETY_RECHECK_SECONDS = 60.0
_RECIPIENT_PLANES = frozenset({
    "deliveries", "acks", "codex-wait-consent", "codex-wait-session",
    "cursor-presentations",
})


def wait_readable(source, timeout_seconds: float) -> bool:
    """Readiness is an invalidation hint; leave it pending for native drain."""
    return bool(select.select([source], [], [], max(0.0, timeout_seconds))[0])


def recipient_hint_relative(node_id: str) -> Path:
    return Path("state/wake-hints") / validate_identifier(node_id, "node") / "generation"


def _identity(info):
    return info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_nlink


def _touch(root, relative):
    """Never follow a hint alias or let an advisory failure mask native truth."""
    descriptors = []
    try:
        if not isinstance(root, FloatiRoot) or not root.tenant_home.is_absolute():
            return False
        parent = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
        descriptors.append(parent)
        # Retain every parent: namespace replacement cannot redirect an open
        # into a different root, and no symlinked component is followed.
        lineage = []
        for component in root.tenant_home.parts[1:]:
            child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                            dir_fd=parent)
            descriptors.append(child)
            lineage.append((parent, component, child))
            parent = child
        if os.fstat(parent).st_uid != os.geteuid():
            return False
        for index, component in enumerate(relative.parts[:-1]):
            try:
                os.mkdir(component, 0o700, dir_fd=parent)
            except FileExistsError:
                pass
            child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                            dir_fd=parent)
            descriptors.append(child)
            info = os.fstat(child)
            if info.st_uid != os.geteuid() or info.st_mode & (0o022 if index == 0 else 0o077):
                return False
            lineage.append((parent, component, child))
            parent = child
        for ancestor, name, descriptor in lineage:
            if _identity(os.stat(name, dir_fd=ancestor, follow_symlinks=False)) != _identity(os.fstat(descriptor)):
                return False
        leaf = relative.name
        descriptor = os.open(leaf, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
                             0o600, dir_fd=parent)
        descriptors.append(descriptor)
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_size != 0
                or _identity(info) != _identity(os.stat(leaf, dir_fd=parent, follow_symlinks=False))):
            return False
        os.utime(descriptor, None)
        os.fsync(descriptor)
        os.fsync(parent)
        return _identity(os.fstat(descriptor)) == _identity(
            os.stat(leaf, dir_fd=parent, follow_symlinks=False))
    except (OSError, ValueError, ProtocolRefusal):
        # Native bytes have already committed. The bounded native safety
        # reread owns discovery when this disposable notification is lost.
        return False
    finally:
        for descriptor in reversed(descriptors):
            try:
                os.close(descriptor)
            except OSError:
                pass


def publish_recipient_hint(root: FloatiRoot, node_id: str) -> bool:
    try:
        return _touch(root, recipient_hint_relative(node_id))
    except ProtocolRefusal:
        return False


def publish_root_hint(root: FloatiRoot) -> bool:
    return _touch(root, ROOT_HINT_RELATIVE)


def publish_ledger_hints(root, relative, records) -> bool:
    """Route only committed native axes; never look up authority under a lock."""
    if not isinstance(root, FloatiRoot):
        return False
    relative = Path(relative)
    nodes = set()
    broad = False
    if relative == Path("events.jsonl"):
        for record in records:
            node = record.get("recipient")
            if isinstance(node, str):
                nodes.add(node)
            else:
                broad = True
    elif relative == Path("registry/entries.jsonl"):
        broad = True
    elif (len(relative.parts) >= 3 and relative.parts[0] == "receipts"
          and relative.parts[1] in _RECIPIENT_PLANES):
        nodes.add(Path(relative.parts[2]).stem)
    else:
        return False
    complete = True
    for node in sorted(nodes):
        complete = publish_recipient_hint(root, node) and complete
    if broad:
        complete = publish_root_hint(root) and complete
    return complete

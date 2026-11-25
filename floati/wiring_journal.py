"""The U2 wiring journal — manifest-before-meaning, append-only.

Every wiring action appends its entry BEFORE performing the action it
records (E3.1 idiom, architect-approved). A crash between append and
action therefore fails toward an HONEST EXTRA REPORT on replay (absent
target = already-done + reported), never toward an untracked artifact.

Fail-closed reading: the first line that will not parse or validate stops
the read; the byte offset is carried in the error so a half-broken journal
is visible, never silently truncated.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, TextIO

JOURNAL_SCHEMA_VERSION = 1
JOURNAL_NAME = "wiring-journal.v1.jsonl"

CLOSED_KINDS = (
    "file",
    "hook_entry",
    "plugin",
    "flag",
    "marker",
    "state_dir",
    "bus_root",
    "dir",
)
CLOSED_ACTIONS = ("install", "update", "register", "uninstall", "purge")
CLOSED_OPS = ("create", "modify", "replace", "delete")


@dataclass(frozen=True)
class JournalEntry:
    ordinal: int                 # 1-based position in the journal
    byte_offset: int             # byte offset of the line's start
    payload: Dict[str, Any]

    @property
    def kind(self) -> str:
        return str(self.payload["kind"])

    @property
    def path(self) -> str:
        return str(self.payload["path"])

    @property
    def op(self) -> str:
        return str(self.payload["op"])

    @property
    def sha256(self) -> Optional[str]:
        value = self.payload.get("sha256")
        return str(value) if value else None

    @property
    def preserved(self) -> bool:
        return bool(self.payload.get("preserved", False))


class WiringJournalCorrupt(Exception):
    def __init__(self, offset: int, detail: str) -> None:
        super().__init__(f"wiring journal corrupt at byte {offset}: {detail}")
        self.offset = offset
        self.detail = detail


def journal_path(destination: Path) -> Path:
    from .storage_identity import INSTALL_METADATA_DIRECTORY

    return destination / INSTALL_METADATA_DIRECTORY / JOURNAL_NAME


def canonical_entry_bytes(payload: Dict[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def replay_durability(destination: Path) -> List[JournalEntry]:
    """Re-establish the journal file/link barriers and validate its bytes."""

    path = journal_path(destination)
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    _fsync_directory(path.parent)
    _fsync_directory(path.parent.parent)
    return read_entries(path)


def _revision(info: os.stat_result) -> tuple[int, int, int, int, int]:
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


class JournalAppender:
    """Carry the verified tip between durable appends by one deployment."""

    def __init__(self, destination: Path) -> None:
        self.path = journal_path(destination)
        self._revision: Optional[tuple[int, int, int, int, int]] = None
        self._previous_hash: Optional[str] = None
        self._ordinal = 0

    def append(self, payload: Dict[str, Any]) -> JournalEntry:
        """Durably append one intent before performing its wiring action."""
        if payload.get("v") != JOURNAL_SCHEMA_VERSION:
            raise ValueError("wiring journal entry requires v == 1")
        if payload.get("kind") not in CLOSED_KINDS:
            raise ValueError(f"unknown wiring kind: {payload.get('kind')!r}")
        if payload.get("action") not in CLOSED_ACTIONS:
            raise ValueError(f"unknown wiring action: {payload.get('action')!r}")
        if payload.get("op") not in CLOSED_OPS:
            raise ValueError(f"unknown wiring op: {payload.get('op')!r}")
        if not isinstance(payload.get("path"), str) or not payload["path"]:
            raise ValueError("wiring entry requires a non-empty path")

        path = self.path
        parent_existed = path.parent.exists()
        path.parent.mkdir(mode=0o700, exist_ok=True)

        with open(path, "a+", encoding="utf-8") as handle:
            descriptor = handle.fileno()
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            info = os.fstat(descriptor)
            if not os.path.samestat(info, path.stat()):
                raise WiringJournalCorrupt(0, "journal was replaced while opening for append")
            offset = info.st_size
            previous_hash, ordinal = self._previous_hash, self._ordinal
            if _revision(info) != self._revision:
                # Reopen and an intervening writer both require full validation.
                handle.seek(0)
                entries = _read_entries(handle, require_terminated=True)
                previous_hash = entries[-1].payload["entryHash"] if entries else None
                ordinal = len(entries)

            stamped = dict(payload)
            stamped["prevHash"] = previous_hash
            # The previous hash participates in the digest. The entry's own
            # hash is derived here even if the caller supplied that field.
            stamped.pop("entryHash", None)
            stamped["entryHash"] = hashlib.sha256(canonical_entry_bytes(stamped)).hexdigest()
            line = canonical_entry_bytes(stamped) + b"\n"
            handle.write(line.decode("utf-8"))
            handle.flush()
            os.fsync(descriptor)
            # Persist the journal link and a newly created metadata directory.
            _fsync_directory(path.parent)
            if not parent_existed:
                _fsync_directory(path.parent.parent)

            # Only carry a tip that belongs to this completed durable append.
            written = os.fstat(descriptor)
            if written.st_size == offset + len(line):
                self._revision = _revision(written)
                self._previous_hash = stamped["entryHash"]
                self._ordinal = ordinal + 1

        return JournalEntry(ordinal=ordinal + 1, byte_offset=offset, payload=stamped)


def append_entry(destination: Path, payload: Dict[str, Any]) -> JournalEntry:
    """Append one durable intent before performing the action it records."""
    return JournalAppender(destination).append(payload)


def read_entries(path: Path) -> List[JournalEntry]:
    """Fail-closed read. Stops at the first line that fails to parse,
    validate, or verify against the prevHash chain; the exception carries
    the byte offset so a half-broken journal is visible, never truncated.
    """
    if not path.exists():
        return []
    with open(path, "r", encoding="utf-8") as handle:
        return _read_entries(handle)


def _read_entries(
    handle: TextIO, *, require_terminated: bool = False
) -> List[JournalEntry]:
    entries: List[JournalEntry] = []
    previous_hash: Optional[str] = None
    offset = 0
    ordinal = 0
    for raw_line in handle:
        line_offset = offset
        offset += len(raw_line.encode("utf-8"))
        if require_terminated and not raw_line.endswith("\n"):
            raise WiringJournalCorrupt(line_offset, "unterminated journal line")
        stripped = raw_line.strip()
        if not stripped:
            continue
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise WiringJournalCorrupt(
                line_offset, f"unparseable line: {exc}") from exc
        if not isinstance(payload, dict):
            raise WiringJournalCorrupt(line_offset, "line is not an object")
        if payload.get("v") != JOURNAL_SCHEMA_VERSION:
            raise WiringJournalCorrupt(line_offset, "schema version mismatch")
        if payload.get("kind") not in CLOSED_KINDS:
            raise WiringJournalCorrupt(
                line_offset, f"unknown wiring kind: {payload.get('kind')!r}")
        claimed_prev = payload.get("prevHash")
        if claimed_prev != previous_hash:
            raise WiringJournalCorrupt(
                line_offset,
                f"prevHash chain break: expected {previous_hash}, found {claimed_prev}",
            )
        body = {
            k: v for k, v in payload.items() if k != "entryHash"
        }
        expected_entry_hash = hashlib.sha256(
            canonical_entry_bytes(body)).hexdigest()
        if payload.get("entryHash") != expected_entry_hash:
            raise WiringJournalCorrupt(
                line_offset, "entryHash mismatch (entry was altered)")
        ordinal += 1
        entries.append(JournalEntry(ordinal=ordinal,
                                    byte_offset=line_offset,
                                    payload=payload))
        previous_hash = payload.get("entryHash")
    return entries

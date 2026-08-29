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

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

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


def append_entry(destination: Path, payload: Dict[str, Any]) -> JournalEntry:
    """Append one entry. MANIFEST-BEFORE-MEANING: callers MUST call this
    BEFORE performing the wiring action the entry records.

    Validates the closed vocabularies and chains the entry to its
    predecessor; a missing journal creates it (first wiring action).
    """
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

    path = journal_path(destination)
    path.parent.mkdir(mode=0o700, exist_ok=True)

    previous_hash, ordinal = _tail(path)
    stamped = dict(payload)
    stamped["prevHash"] = previous_hash
    # The chain field participates in the entry hash: hash covers
    # everything EXCEPT entryHash itself (prevHash included) — identical
    # to the reader's recomputation.
    body = canonical_entry_bytes(stamped)
    stamped["entryHash"] = hashlib.sha256(body).hexdigest()

    line = json.dumps(stamped, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")) + "\n"
    offset = path.stat().st_size if path.exists() else 0
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(line)
        handle.flush()
        os.fsync(handle.fileno())
    return JournalEntry(ordinal=ordinal + 1, byte_offset=offset,
                        payload=stamped)


def _tail(path: Path) -> Tuple[Optional[str], int]:
    """Return (prevHash, ordinal) of the last valid entry; (None, 0) if absent."""
    if not path.exists():
        return None, 0
    last_hash: Optional[str] = None
    ordinal = 0
    with open(path, "r", encoding="utf-8") as handle:
        for line in handle:
            stripped = line.strip()
            if not stripped:
                continue
            try:
                payload = json.loads(stripped)
            except json.JSONDecodeError as exc:
                raise WiringJournalCorrupt(ordinal, f"unparseable line: {exc}") from exc
            last_hash = payload.get("entryHash")
            ordinal += 1
    return last_hash, ordinal


def read_entries(path: Path) -> List[JournalEntry]:
    """Fail-closed read. Stops at the first line that fails to parse,
    validate, or verify against the prevHash chain; the exception carries
    the byte offset so a half-broken journal is visible, never truncated.
    """
    entries: List[JournalEntry] = []
    if not path.exists():
        return entries
    previous_hash: Optional[str] = None
    offset = 0
    ordinal = 0
    with open(path, "r", encoding="utf-8") as handle:
        for raw_line in handle:
            line_offset = offset
            offset += len(raw_line.encode("utf-8"))
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

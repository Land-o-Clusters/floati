"""Status snapshots anchored to certified compact mail observations.

The mail witness is derived evidence, not another ledger record kind. Its
validator checks the observation's native source identities and authenticated
index roots. Other status inputs retain SnapshotStore's ordinary decoder.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

from .errors import SnapshotRefusal
from .jsonl import MAX_RECORD_BYTES
from .snapshot import SnapshotStore, SourceSpec


def _mail_source(relative):
    parts = Path(relative).parts
    return parts == ("events.jsonl",) or (
        len(parts) >= 2 and parts[0] == "receipts"
        and parts[1] in {"deliveries", "acks", "consumed"}
    )


class StatusSnapshotStore(SnapshotStore):
    """Keep the existing envelope while replacing mail-history anchors."""

    def __init__(self, root, *, observation, discover_sources):
        self.observation = observation
        event_payload = observation.events.payload
        self._event_archive_anchor = (
            event_payload["count"], event_payload["offset"], event_payload["head"],
        )
        self._mail_bytes = {
            Path(relative): raw if raw.endswith(b"\n") else raw + b"\n"
            for relative, raw in observation.sources()
        }
        if not self._mail_bytes or any(len(raw) > MAX_RECORD_BYTES or raw.count(b"\n") != 1
                                       for raw in self._mail_bytes.values()):
            raise SnapshotRefusal("snapshot_source_invalid",
                                  "status mail witnesses are not bounded canonical frames")
        self._native_sources = discover_sources
        super().__init__(root, reader="status", key="default",
                         discover_sources=self._status_sources)

    def _status_sources(self):
        sources = [source for source in self._native_sources()
                   if not _mail_source(source.relative)]
        sources.extend(SourceSpec(relative, frozenset({"status_mail_witness"}))
                       for relative in sorted(self._mail_bytes))
        return tuple(sources)

    def _require_current(self):
        from .send_prefix import _CACHE_ERRORS

        try:
            current = self.observation.current()
        except _CACHE_ERRORS:
            current = False
        if not current:
            raise SnapshotRefusal("snapshot_source_changed",
                                  "certified mail sources changed during status")

    def _read_source(self, source):
        if source.relative not in self._mail_bytes:
            return super()._read_source(source)
        self._require_current()
        return self._mail_bytes[source.relative]

    def _archives(self):
        from .send_prefix import _CACHE_ERRORS

        try:
            current = self.observation.current()
            no_roll = current and self.observation.current_epoch_has_no_roll_head()
        except _CACHE_ERRORS:
            current, no_roll = False, False
        if no_roll:
            # Native lineage starts exclusively at physical event record one.
            # Its authenticated absence proves the same empty lineage without
            # asking the full-history reader to discover the head again.
            self._require_current()
            return ()
        archives = super()._archives()
        if self._event_anchor_was_archived(archives):
            raise SnapshotRefusal("snapshot_source_archived",
                                  "derived snapshot source moved to an owned epoch archive")
        # Native anchors still receive the generic archived-prefix check.
        # A stale witness that has no verified archive match is subsequently
        # refused by _read_source, rather than being mistaken for a roll.
        return archives

    def _event_anchor_was_archived(self, archives):
        """Resolve a stale private witness through verified native lineage."""
        from .epoch_history import read_archived_bytes
        from .send_prefix import _EMPTY_HEAD

        count, offset, expected = self._event_archive_anchor
        for archive in archives:
            try:
                data = read_archived_bytes(archive, "events.jsonl")
            except OSError:
                continue
            if data is None or len(data) < offset:
                continue
            frames = data[:offset].splitlines(keepends=True)
            if len(frames) != count or (offset and not data[:offset].endswith(b"\n")):
                continue
            head = _EMPTY_HEAD
            for frame in frames:
                head = hashlib.sha256(bytes.fromhex(head) + frame).hexdigest()
            if head == expected:
                return True
        return False

    def _validated_records(self, source, data):
        if source.relative not in self._mail_bytes:
            return super()._validated_records(source, data)
        self._require_current()
        if not data:
            return ()
        if data != self._mail_bytes[source.relative]:
            raise SnapshotRefusal("snapshot_source_invalid",
                                  "status mail witness does not match its certified roots")
        # One derived observation has one exact-byte identity. These values
        # never pass through the durable record/schema authoring API.
        return ({"id": hashlib.sha256(data).hexdigest()},)

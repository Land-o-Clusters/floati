"""Versioned, tenant-bound derived snapshots with exact ledger anchors."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, FrozenSet, Mapping, Optional, Sequence, Tuple

from .errors import ProtocolRefusal, SnapshotRefusal
from .framing import FrameError, decode_frames
from .jsonl import MAX_LEDGER_BYTES, MAX_LEDGER_RECORDS, MAX_RECORD_BYTES
from .records import validate_record
from .root import FloatiRoot
from .storage_identity import SNAPSHOT_DIRECTORY as SNAPSHOT_DIRECTORY_NAME


SNAPSHOT_VERSION = 0
SNAPSHOT_DIRECTORY = Path(SNAPSHOT_DIRECTORY_NAME) / "v0"
_READER = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_ENVELOPE_FIELDS = frozenset(
    {
        "snapshot_version",
        "reader",
        "key",
        "root",
        "tenant_id",
        "sources",
        "payload",
        "checksum",
    }
)
_SOURCE_FIELDS = frozenset(
    {
        "path",
        "byte_offset",
        "record_ordinal",
        "prefix_sha256",
        "id_fingerprints",
    }
)


@dataclass(frozen=True)
class SourceSpec:
    relative: Path
    allowed_kinds: FrozenSet[str]
    max_bytes: int = MAX_RECORD_BYTES

    def __post_init__(self) -> None:
        relative = Path(self.relative)
        if (
            relative.is_absolute()
            or not relative.parts
            or any(part in ("", ".", "..") for part in relative.parts)
        ):
            raise SnapshotRefusal(
                "snapshot_source_invalid", "snapshot source must be tenant-relative"
            )
        if not self.allowed_kinds:
            raise SnapshotRefusal(
                "snapshot_source_invalid", "snapshot source kinds are required"
            )
        object.__setattr__(self, "relative", relative)


@dataclass(frozen=True)
class SnapshotLoad:
    payload: Dict[str, object]
    tails: Dict[str, Tuple[Dict[str, object], ...]]


@dataclass(frozen=True)
class SnapshotCapture:
    tokens: Tuple[Tuple[str, int, str], ...]


def _canonical(value: Mapping[str, object]) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise SnapshotRefusal(
            "snapshot_payload_invalid", "snapshot content is not compact I-JSON"
        ) from exc


def _checksum(value: Mapping[str, object]) -> str:
    unsigned = dict(value)
    unsigned.pop("checksum", None)
    return hashlib.sha256(_canonical(unsigned)).hexdigest()


def _fingerprint(record_id: str) -> str:
    return hashlib.sha256(record_id.encode("utf-8")).hexdigest()[:16]


class SnapshotStore:
    """Load and refresh one reader-owned derived projection."""

    def __init__(
        self,
        root: FloatiRoot,
        *,
        reader: str,
        key: str,
        discover_sources: Callable[[], Sequence[SourceSpec]],
    ) -> None:
        if not isinstance(reader, str) or _READER.fullmatch(reader) is None:
            raise SnapshotRefusal(
                "snapshot_reader_invalid", "snapshot reader identifier is invalid"
            )
        if not isinstance(key, str) or not key:
            raise SnapshotRefusal(
                "snapshot_key_invalid", "snapshot key must be a nonempty string"
            )
        self.root = root
        self.reader = reader
        self.key = key
        self._discover_sources = discover_sources
        suffix = hashlib.sha256(key.encode("utf-8")).hexdigest()
        try:
            self.path = root.resolve_relative(
                SNAPSHOT_DIRECTORY / f"{reader}-{suffix}.json"
            )
        except ProtocolRefusal as exc:
            raise SnapshotRefusal(
                "snapshot_path_invalid",
                "derived snapshot path is outside the selected tenant",
            ) from exc

    def _sources(self) -> Tuple[SourceSpec, ...]:
        try:
            sources = tuple(
                sorted(self._discover_sources(), key=lambda item: item.relative.as_posix())
            )
        except SnapshotRefusal:
            raise
        except Exception as exc:
            raise SnapshotRefusal(
                "snapshot_source_invalid", "snapshot source discovery failed"
            ) from exc
        paths = [source.relative.as_posix() for source in sources]
        if len(paths) != len(set(paths)):
            raise SnapshotRefusal(
                "snapshot_source_invalid", "snapshot source paths must be unique"
            )
        return sources

    def _read_source(self, source: SourceSpec) -> bytes:
        path = self.root.resolve_relative(source.relative)
        try:
            if not path.exists():
                return b""
            data = path.read_bytes()
        except OSError as exc:
            raise SnapshotRefusal(
                "snapshot_source_unavailable", f"{source.relative.as_posix()} is unavailable"
            ) from exc
        if len(data) > MAX_LEDGER_BYTES:
            raise SnapshotRefusal(
                "snapshot_source_invalid", f"{source.relative.as_posix()} is too large"
            )
        return data

    def _validated_records(
        self, source: SourceSpec, data: bytes
    ) -> Tuple[Dict[str, object], ...]:
        for raw in data.splitlines(keepends=True):
            if len(raw) > source.max_bytes:
                raise SnapshotRefusal(
                    "snapshot_source_invalid",
                    f"{source.relative.as_posix()} contains an oversized record",
                )
        try:
            framed = decode_frames(data)
        except FrameError as exc:
            raise SnapshotRefusal(
                "snapshot_source_invalid",
                f"{source.relative.as_posix()} cannot be framed: {exc.code}",
            ) from exc
        if len(framed) > MAX_LEDGER_RECORDS:
            raise SnapshotRefusal(
                "snapshot_source_invalid",
                f"{source.relative.as_posix()} exceeds the record limit",
            )
        records = []
        seen = set()
        for raw_record in framed:
            try:
                record = validate_record(
                    raw_record,
                    self.root.tenant_id,
                    source.allowed_kinds,
                    integrity=True,
                )
            except Exception as exc:
                raise SnapshotRefusal(
                    "snapshot_source_invalid",
                    f"{source.relative.as_posix()} contains invalid evidence",
                ) from exc
            if record["id"] in seen:
                raise SnapshotRefusal(
                    "snapshot_source_invalid",
                    f"{source.relative.as_posix()} repeats a record id",
                )
            seen.add(record["id"])
            records.append(record)
        return tuple(records)

    @staticmethod
    def _capture_for(
        sources: Sequence[Tuple[SourceSpec, bytes]],
    ) -> SnapshotCapture:
        return SnapshotCapture(
            tuple(
                (
                    source.relative.as_posix(),
                    len(data),
                    hashlib.sha256(data).hexdigest(),
                )
                for source, data in sources
            )
        )

    def _source_bytes(self) -> Tuple[Tuple[SourceSpec, bytes], ...]:
        return tuple(
            (source, self._read_source(source)) for source in self._sources()
        )

    def capture(self) -> SnapshotCapture:
        return self._capture_for(self._source_bytes())

    def refresh(
        self,
        payload: Mapping[str, object],
        *,
        expected: Optional[SnapshotCapture] = None,
    ) -> None:
        captured_sources = self._source_bytes()
        if expected is not None and self._capture_for(captured_sources) != expected:
            raise SnapshotRefusal(
                "snapshot_source_changed",
                "authoritative sources changed while the projection was built",
            )
        sources = []
        for source, data in captured_sources:
            records = self._validated_records(source, data)
            sources.append(
                {
                    "path": source.relative.as_posix(),
                    "byte_offset": len(data),
                    "record_ordinal": len(records),
                    "prefix_sha256": hashlib.sha256(data).hexdigest(),
                    "id_fingerprints": "".join(
                        sorted(_fingerprint(str(record["id"])) for record in records)
                    ),
                }
            )
        envelope: Dict[str, object] = {
            "snapshot_version": SNAPSHOT_VERSION,
            "reader": self.reader,
            "key": self.key,
            "root": str(self.root.path),
            "tenant_id": self.root.tenant_id,
            "sources": sources,
            "payload": dict(payload),
        }
        envelope["checksum"] = _checksum(envelope)
        encoded = _canonical(envelope) + b"\n"
        self._atomic_write(encoded)

    def _atomic_write(self, encoded: bytes) -> None:
        descriptor = -1
        temporary = ""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            descriptor, temporary = tempfile.mkstemp(
                prefix=f".{self.path.name}.", dir=self.path.parent
            )
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb", closefd=True) as stream:
                descriptor = -1
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            temporary = ""
            parent = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(parent)
            finally:
                os.close(parent)
        except OSError as exc:
            raise SnapshotRefusal(
                "snapshot_persistence_failed", "derived snapshot could not be persisted"
            ) from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            if temporary:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass

    def _read_envelope(self) -> Dict[str, object]:
        try:
            data = self.path.read_bytes()
        except FileNotFoundError as exc:
            raise SnapshotRefusal(
                "snapshot_missing", "derived snapshot does not exist"
            ) from exc
        except OSError as exc:
            raise SnapshotRefusal(
                "snapshot_parse_invalid", "derived snapshot is unreadable"
            ) from exc
        try:
            envelope = json.loads(data)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SnapshotRefusal(
                "snapshot_parse_invalid", "derived snapshot is not valid JSON"
            ) from exc
        if not isinstance(envelope, dict) or frozenset(envelope) != _ENVELOPE_FIELDS:
            raise SnapshotRefusal(
                "snapshot_parse_invalid", "derived snapshot envelope fields are invalid"
            )
        return envelope

    def load(self) -> SnapshotLoad:
        envelope = self._read_envelope()
        version = envelope["snapshot_version"]
        if version != SNAPSHOT_VERSION or isinstance(version, bool):
            raise SnapshotRefusal(
                "snapshot_version_mismatch", "derived snapshot version is unsupported"
            )
        if (
            envelope["reader"] != self.reader
            or envelope["key"] != self.key
            or envelope["root"] != str(self.root.path)
            or envelope["tenant_id"] != self.root.tenant_id
        ):
            raise SnapshotRefusal(
                "snapshot_identity_mismatch", "derived snapshot belongs to another scope"
            )
        checksum = envelope["checksum"]
        if not isinstance(checksum, str) or _HEX64.fullmatch(checksum) is None:
            raise SnapshotRefusal(
                "snapshot_checksum_mismatch", "derived snapshot checksum is invalid"
            )
        if _checksum(envelope) != checksum:
            raise SnapshotRefusal(
                "snapshot_checksum_mismatch", "derived snapshot checksum does not match"
            )
        payload = envelope["payload"]
        if not isinstance(payload, dict):
            raise SnapshotRefusal(
                "snapshot_payload_invalid", "derived snapshot payload must be an object"
            )
        anchors = envelope["sources"]
        if not isinstance(anchors, list):
            raise SnapshotRefusal(
                "snapshot_source_set_mismatch", "derived snapshot sources are invalid"
            )
        sources = self._sources()
        if [source.relative.as_posix() for source in sources] != [
            anchor.get("path") if isinstance(anchor, dict) else None
            for anchor in anchors
        ]:
            raise SnapshotRefusal(
                "snapshot_source_set_mismatch", "derived snapshot source set changed"
            )
        tails: Dict[str, Tuple[Dict[str, object], ...]] = {}
        for source, anchor in zip(sources, anchors):
            if not isinstance(anchor, dict) or frozenset(anchor) != _SOURCE_FIELDS:
                raise SnapshotRefusal(
                    "snapshot_parse_invalid", "derived snapshot anchor fields are invalid"
                )
            offset = anchor["byte_offset"]
            ordinal = anchor["record_ordinal"]
            digest = anchor["prefix_sha256"]
            fingerprints = anchor["id_fingerprints"]
            if (
                not isinstance(offset, int)
                or isinstance(offset, bool)
                or offset < 0
                or not isinstance(ordinal, int)
                or isinstance(ordinal, bool)
                or ordinal < 0
                or not isinstance(digest, str)
                or _HEX64.fullmatch(digest) is None
                or not isinstance(fingerprints, str)
                or len(fingerprints) % 16
            ):
                raise SnapshotRefusal(
                    "snapshot_parse_invalid", "derived snapshot anchor values are invalid"
                )
            data = self._read_source(source)
            if offset > len(data):
                raise SnapshotRefusal(
                    "snapshot_anchor_past_eof", "derived snapshot anchor points past EOF"
                )
            prefix = data[:offset]
            if prefix and not prefix.endswith(b"\n"):
                raise SnapshotRefusal(
                    "snapshot_anchor_offset_mismatch", "anchor is not a frame boundary"
                )
            if prefix.count(b"\n") != ordinal:
                raise SnapshotRefusal(
                    "snapshot_anchor_ordinal_mismatch", "anchor ordinal does not match bytes"
                )
            if hashlib.sha256(prefix).hexdigest() != digest:
                raise SnapshotRefusal(
                    "snapshot_anchor_digest_mismatch", "anchor prefix digest does not match"
                )
            tail_records = self._validated_records(source, data[offset:])
            if ordinal + len(tail_records) > MAX_LEDGER_RECORDS:
                raise SnapshotRefusal(
                    "snapshot_source_invalid", "tail exceeds the ledger record limit"
                )
            tail_seen = set()
            for record in tail_records:
                record_id = str(record["id"])
                fingerprint = _fingerprint(record_id)
                if fingerprint in fingerprints or record_id in tail_seen:
                    raise SnapshotRefusal(
                        "snapshot_tail_duplicate_id", "tail may repeat a prefix record id"
                    )
                tail_seen.add(record_id)
            tails[source.relative.as_posix()] = tail_records
        return SnapshotLoad(dict(payload), tails)

"""Canonical segmented storage for run records, independent of RunLedger."""

from __future__ import annotations

import hashlib
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, FrozenSet, Iterator, List, Optional, Tuple

from .errors import IntegrityFailure, ProtocolRefusal
from .ids import uuid7_hex
from .jsonl import MAX_LEDGER_RECORDS, _append_frame, _encode_record, _ensure_directory, _locked_path, _read_path_records
from .records import segment_seal_digest
from .root import FloatiRoot


_METADATA_KINDS = frozenset({"segment_opened", "segment_sealed"})
_EMPTY_PREFIX_DIGEST = hashlib.sha256(b"").hexdigest()


def _advance_prefix_digest(previous: str, encoded: bytes) -> str:
    return hashlib.sha256(bytes.fromhex(previous) + encoded).hexdigest()


@dataclass(frozen=True)
class SegmentConfig:
    max_records: int = 10000
    max_bytes: int = 8 * 1024 * 1024


@dataclass(frozen=True)
class PhysicalCoordinate:
    segment_number: int
    frame_ordinal: int
    global_ordinal: int


@dataclass(frozen=True)
class LocatedRecord:
    record: Dict[str, object]
    coordinate: PhysicalCoordinate


class RunStoreSnapshot:
    def __init__(
        self,
        located: List[LocatedRecord],
        known: Optional[Dict[str, LocatedRecord]] = None,
        prefix_digest: str = _EMPTY_PREFIX_DIGEST,
    ) -> None:
        # Validated state stays private and borrowed so an internal managed
        # cache hit does not copy the full durable prefix.  The historically
        # visible underscore attributes below are lazy, snapshot-owned views:
        # callback mutation can change only those copies, never store state.
        self.__validated_located = tuple(located)
        self.__validated_known = (
            dict(known)
            if known is not None
            else {str(item.record["id"]): item for item in located}
        )
        self.__owned_located: Optional[Tuple[LocatedRecord, ...]] = None
        self.__owned_known: Optional[Dict[str, LocatedRecord]] = None
        self.total_records = len(located)
        self.prefix_digest = prefix_digest

    def _ensure_owned_views(self) -> None:
        if self.__owned_located is None:
            self.__owned_located = tuple(
                LocatedRecord(deepcopy(item.record), item.coordinate)
                for item in self.__validated_located
            )
        if self.__owned_known is None:
            self.__owned_known = {
                str(item.record["id"]): item
                for item in self.__owned_located
            }

    @property
    def _located(self) -> Tuple[LocatedRecord, ...]:
        self._ensure_owned_views()
        assert self.__owned_located is not None
        return self.__owned_located

    @_located.setter
    def _located(self, located: Tuple[LocatedRecord, ...]) -> None:
        self.__owned_located = type(located)(
            LocatedRecord(deepcopy(item.record), item.coordinate)
            for item in located
        )

    @property
    def _known(self) -> Dict[str, LocatedRecord]:
        self._ensure_owned_views()
        assert self.__owned_known is not None
        return self.__owned_known

    @_known.setter
    def _known(self, known: Dict[str, LocatedRecord]) -> None:
        self.__owned_known = {
            record_id: LocatedRecord(deepcopy(item.record), item.coordinate)
            for record_id, item in known.items()
        }

    def iter_records(self) -> Iterator[Dict[str, object]]:
        for item in self.__validated_located:
            yield deepcopy(item.record)

    def lookup(self, record_id: str) -> Optional[LocatedRecord]:
        item = self.__validated_known.get(record_id)
        if item is None:
            return None
        return LocatedRecord(deepcopy(item.record), item.coordinate)


@dataclass
class _ActiveState:
    located: List[LocatedRecord]
    known: Dict[str, LocatedRecord]
    opening: Dict[str, object]
    segment_records: List[Dict[str, object]]
    segment_bytes: int
    prefix_digest: str
    sealed_tail: Optional[Dict[str, object]] = None


class SegmentedRunStore:
    def __init__(
        self,
        root: FloatiRoot,
        allowed_kinds: FrozenSet[str],
        config: SegmentConfig = SegmentConfig(),
    ) -> None:
        if not isinstance(root, FloatiRoot):
            raise ProtocolRefusal("root_required", "segmented run storage requires a validated FloatiRoot")
        if not isinstance(allowed_kinds, frozenset) or not allowed_kinds:
            raise ProtocolRefusal("ledger_kind_required", "run storage requires a nonempty frozen kind set")
        if not all(isinstance(kind, str) and kind and kind not in _METADATA_KINDS for kind in allowed_kinds):
            raise ProtocolRefusal("record_kind_invalid", "run kinds must be nonempty non-metadata strings")
        if not isinstance(config, SegmentConfig):
            raise ProtocolRefusal("segment_config_invalid", "segment config is required")
        self._integer(config.max_records, 1, 100000, "max_records")
        self._integer(config.max_bytes, 65536, 64 * 1024 * 1024, "max_bytes")
        # Prove each configured kind is governed without exposing an arbitrary path API.
        from .records import _SPECS
        if not allowed_kinds <= frozenset(_SPECS):
            raise ProtocolRefusal("record_kind_invalid", "run kind is not governed")
        self.root = root
        self.allowed_kinds = allowed_kinds
        self.config = config
        self._cached_state: Optional[_ActiveState] = None
        self._cached_signature: Optional[Tuple[Tuple[str, int, int, int], ...]] = None

    @property
    def _legacy(self) -> Path:
        return self.root.resolve_relative("runs/events.jsonl")

    @property
    def _segments(self) -> Path:
        return self.root.resolve_relative("runs/segments")

    @property
    def _metadata(self) -> Path:
        return self.root.resolve_relative("runs/segments/events.jsonl")

    # The two segmented-storage locks, each paired with the root-relative
    # coordinate a refusal must print. Kept beside the paths so the two cannot
    # drift: once only a basename survives, "runs/events.jsonl.lock" is
    # indistinguishable from the tenant "events.jsonl.lock".
    _WRITER_LOCK_RELATIVE = "runs/segments/writer.lock"
    _TRANSITION_LOCK_RELATIVE = "runs/events.jsonl.lock"

    @property
    def _writer_lock(self) -> Path:
        return self.root.resolve_relative(self._WRITER_LOCK_RELATIVE)

    @property
    def _transition_lock(self) -> Path:
        return self._legacy.with_name(self._legacy.name + ".lock")

    @staticmethod
    def _integer(value: object, minimum: int, maximum: int, field: str) -> None:
        if not isinstance(value, int) or isinstance(value, bool) or not minimum <= value <= maximum:
            raise ProtocolRefusal(field + "_invalid", field + " is outside segmented storage bounds")

    def is_active(self) -> bool:
        return self._segments.exists()

    def activate(self, *, now: datetime) -> Dict[str, object]:
        testimony = self._timestamp(now)
        with _locked_path(self._transition_lock, exclusive=True, relative=self._TRANSITION_LOCK_RELATIVE):
            legacy = _read_path_records(self._legacy, self.root.tenant_id, self.allowed_kinds)
            _ensure_directory(self._legacy.parent)
            _ensure_directory(self._segments)
            with _locked_path(self._writer_lock, exclusive=True, relative=self._WRITER_LOCK_RELATIVE):
                if self._metadata.exists():
                    raise ProtocolRefusal("segment_already_active", "segmented storage is already active")
                leftovers = [path for path in self._segments.iterdir() if path.name != "writer.lock"]
                zero = self._segment_path(0)
                if leftovers and (leftovers != [zero] or not zero.is_file() or zero.stat().st_size != 0):
                    raise IntegrityFailure("segment_metadata_missing", "segment namespace exists without activation metadata")
                opened = self._opened(0, len(legacy) + 1, None, testimony)
                encoded = _encode_record(opened, self.root.tenant_id, _METADATA_KINDS)
                _append_frame(self._segment_path(0), b"")
                _append_frame(self._metadata, encoded)
                return deepcopy(opened)

    def iter_records(self) -> Iterator[Dict[str, object]]:
        if not self.is_active():
            with _locked_path(self._transition_lock, exclusive=False, relative=self._TRANSITION_LOCK_RELATIVE):
                if self._metadata.exists() or self._segments.exists():
                    with _locked_path(self._writer_lock, exclusive=False, relative=self._WRITER_LOCK_RELATIVE):
                        for item in self._validate_active().located:
                            yield deepcopy(item.record)
                    return
                for record in _read_path_records(self._legacy, self.root.tenant_id, self.allowed_kinds):
                    yield deepcopy(record)
            return
        with _locked_path(self._writer_lock, exclusive=False, relative=self._WRITER_LOCK_RELATIVE):
            for item in self._validate_active().located:
                yield deepcopy(item.record)

    def records(self) -> List[Dict[str, object]]:
        return list(self.iter_records())

    def lookup(self, record_id: str) -> Optional[LocatedRecord]:
        if not self.is_active():
            with _locked_path(self._transition_lock, exclusive=False, relative=self._TRANSITION_LOCK_RELATIVE):
                if self._metadata.exists() or self._segments.exists():
                    with _locked_path(self._writer_lock, exclusive=False, relative=self._WRITER_LOCK_RELATIVE):
                        return self._lookup(self._validate_active().located, record_id)
                legacy = _read_path_records(self._legacy, self.root.tenant_id, self.allowed_kinds)
                return self._lookup(self._locate_legacy(legacy), record_id)
        with _locked_path(self._writer_lock, exclusive=False, relative=self._WRITER_LOCK_RELATIVE):
            state = self._validate_active()
            located = state.known.get(record_id)
            if located is None:
                return None
            return LocatedRecord(deepcopy(located.record), located.coordinate)

    def transact(
        self,
        decide: Callable[[RunStoreSnapshot], Tuple[Any, Optional[Dict[str, object]]]],
    ) -> Any:
        if not self.is_active():
            with _locked_path(self._transition_lock, exclusive=True, relative=self._TRANSITION_LOCK_RELATIVE):
                if self._metadata.exists() or self._segments.exists():
                    with _locked_path(self._writer_lock, exclusive=True, relative=self._WRITER_LOCK_RELATIVE):
                        state = self._validate_active()
                        result, candidate = decide(self._active_snapshot(state))
                        if candidate is not None:
                            self._append_cached(state, [candidate])
                        return result
                rows = _read_path_records(self._legacy, self.root.tenant_id, self.allowed_kinds)
                result, candidate = decide(self._legacy_snapshot(rows))
                if candidate is not None:
                    self._append_legacy_candidates(rows, [candidate])
                return result
        with _locked_path(self._writer_lock, exclusive=True, relative=self._WRITER_LOCK_RELATIVE):
            state = self._validate_active()
            result, candidate = decide(self._active_snapshot(state))
            if candidate is not None:
                self._append_cached(state, [candidate])
            return result

    def transact_batch(
        self,
        decide: Callable[[RunStoreSnapshot], Tuple[List[Any], List[Dict[str, object]]]],
    ) -> List[Any]:
        results, _identity = self._transact_batch_identity(decide)
        return results

    def _transact_batch_identity(
        self,
        decide: Callable[[RunStoreSnapshot], Tuple[List[Any], List[Dict[str, object]]]],
    ) -> Tuple[List[Any], Tuple[int, str]]:
        if not self.is_active():
            with _locked_path(self._transition_lock, exclusive=True, relative=self._TRANSITION_LOCK_RELATIVE):
                if self._metadata.exists() or self._segments.exists():
                    with _locked_path(self._writer_lock, exclusive=True, relative=self._WRITER_LOCK_RELATIVE):
                        state = self._validate_active()
                        results, candidates = decide(self._active_snapshot(state))
                        if not isinstance(results, list) or not isinstance(candidates, list) or len(results) != len(candidates):
                            raise ProtocolRefusal("batch_result_mismatch", "batch results and candidates must correspond")
                        self._append_cached(state, candidates)
                        return results, (len(state.located), state.prefix_digest)
                rows = _read_path_records(self._legacy, self.root.tenant_id, self.allowed_kinds)
                results, candidates = decide(
                    self._legacy_snapshot(rows, fingerprint=True)
                )
                if not isinstance(results, list) or not isinstance(candidates, list) or len(results) != len(candidates):
                    raise ProtocolRefusal("batch_result_mismatch", "batch results and candidates must correspond")
                self._append_legacy_candidates(rows, candidates)
                completed = _read_path_records(
                    self._legacy, self.root.tenant_id, self.allowed_kinds
                )
                identity = self._legacy_snapshot(completed, fingerprint=True)
                return results, (identity.total_records, identity.prefix_digest)
        with _locked_path(self._writer_lock, exclusive=True, relative=self._WRITER_LOCK_RELATIVE):
            state = self._validate_active()
            results, candidates = decide(self._active_snapshot(state))
            if not isinstance(results, list) or not isinstance(candidates, list) or len(results) != len(candidates):
                raise ProtocolRefusal("batch_result_mismatch", "batch results and candidates must correspond")
            self._append_cached(state, candidates)
            return results, (len(state.located), state.prefix_digest)

    def _validate_active(self) -> _ActiveState:
        signature = self._state_signature()
        if self._cached_state is not None and signature == self._cached_signature:
            return self._cached_state
        state = self._validate_active_uncached()
        self._cached_state = state
        self._cached_signature = self._state_signature()
        return state

    def _active_snapshot(self, state: _ActiveState) -> RunStoreSnapshot:
        return RunStoreSnapshot(state.located, state.known, state.prefix_digest)

    def _legacy_snapshot(
        self,
        records: List[Dict[str, object]],
        *,
        fingerprint: bool = False,
    ) -> RunStoreSnapshot:
        located = self._locate_legacy(records)
        return RunStoreSnapshot(
            located,
            {str(item.record["id"]): item for item in located},
            self._prefix_digest(located) if fingerprint else _EMPTY_PREFIX_DIGEST,
        )

    def _prefix_digest(self, located: List[LocatedRecord]) -> str:
        digest = _EMPTY_PREFIX_DIGEST
        for item in located:
            digest = _advance_prefix_digest(
                digest,
                _encode_record(item.record, self.root.tenant_id, self.allowed_kinds),
            )
        return digest

    def _state_signature(self) -> Tuple[Tuple[str, int, int, int], ...]:
        paths = [self._legacy]
        if self._segments.exists():
            paths.extend(
                path
                for path in self._segments.iterdir()
                if path.is_file() and path.name != "writer.lock"
            )
        signature = []
        for path in sorted(paths):
            if not path.exists():
                continue
            metadata = path.stat()
            signature.append(
                (
                    path.relative_to(self.root.tenant_home).as_posix(),
                    metadata.st_size,
                    metadata.st_mtime_ns,
                    metadata.st_ctime_ns,
                )
            )
        return tuple(signature)

    def _validate_active_uncached(self) -> _ActiveState:
        if not self._metadata.exists():
            raise IntegrityFailure("segment_metadata_missing", "activation metadata is missing")
        metadata = _read_path_records(self._metadata, self.root.tenant_id, _METADATA_KINDS)
        if not metadata:
            raise IntegrityFailure("segment_metadata_missing", "activation metadata is empty")
        legacy = _read_path_records(self._legacy, self.root.tenant_id, self.allowed_kinds)
        located = self._locate_legacy(legacy)
        seen = {str(item.record["id"]) for item in located}
        expected_number = 0
        expected_global = len(legacy) + 1
        previous_digest: Optional[str] = None
        opening: Optional[Dict[str, object]] = None
        sealed_tail: Optional[Dict[str, object]] = None
        active_records: List[Dict[str, object]] = []
        active_bytes = 0
        expected_files = set()
        index = 0
        while index < len(metadata):
            opened = metadata[index]
            if opened["kind"] != "segment_opened" or opened["segment_number"] != expected_number:
                raise IntegrityFailure("segment_metadata_order", "segment opening order is not contiguous")
            if opened["first_global_ordinal"] != expected_global or opened["previous_seal_digest"] != previous_digest:
                raise IntegrityFailure("segment_metadata_lineage", "segment opening lineage does not match canonical history")
            opening = opened
            path = self._segment_path(expected_number)
            expected_files.add(path.name)
            if not path.is_file():
                raise IntegrityFailure("segment_missing", f"segment {expected_number} is missing")
            records = _read_path_records(path, self.root.tenant_id, self.allowed_kinds)
            try:
                data = path.read_bytes()
            except OSError as exc:
                raise IntegrityFailure("segment_unavailable", str(exc)) from exc
            for ordinal, record in enumerate(records, 1):
                record_id = str(record["id"])
                if record_id in seen:
                    raise IntegrityFailure("duplicate_record_id", f"canonical run history repeats id {record_id}")
                seen.add(record_id)
                located.append(LocatedRecord(record, PhysicalCoordinate(expected_number, ordinal, expected_global)))
                expected_global += 1
            active_records, active_bytes = records, len(data)
            if len(records) > int(opened["max_records"]) or len(data) > int(opened["max_bytes"]):
                raise IntegrityFailure("segment_threshold_exceeded", "segment exceeds its opened thresholds")
            index += 1
            if index == len(metadata):
                break
            seal = metadata[index]
            if seal["kind"] != "segment_sealed" or seal["segment_number"] != expected_number:
                raise IntegrityFailure("segment_metadata_order", "segment seal order is not contiguous")
            if (
                seal["opening_record_id"] != opened["id"]
                or seal["record_count"] != len(records)
                or seal["byte_length"] != len(data)
                or seal["segment_sha256"] != hashlib.sha256(data).hexdigest()
                or seal["last_global_ordinal"] != expected_global - 1
                or seal["seal_digest"] != segment_seal_digest(seal)
            ):
                raise IntegrityFailure("segment_seal_mismatch", "sealed metadata does not match exact segment bytes")
            previous_digest = str(seal["seal_digest"])
            expected_number += 1
            index += 1
            if index == len(metadata):
                sealed_tail = seal
                successor = self._segment_path(expected_number)
                if successor.exists():
                    if not successor.is_file() or successor.stat().st_size != 0:
                        raise IntegrityFailure("unexpected_segment_file", "unopened successor is not exact zero bytes")
                    expected_files.add(successor.name)
        allowed_names = expected_files | {"events.jsonl", "writer.lock"}
        unexpected = [path.name for path in self._segments.iterdir() if path.name not in allowed_names]
        if unexpected:
            raise IntegrityFailure("unexpected_segment_file", f"unexpected segment file {sorted(unexpected)[0]}")
        assert opening is not None
        return _ActiveState(
            located,
            {str(item.record["id"]): item for item in located},
            opening,
            active_records,
            active_bytes,
            self._prefix_digest(located),
            sealed_tail,
        )

    def _append_cached(
        self, state: _ActiveState, candidates: List[Dict[str, object]]
    ) -> None:
        try:
            self._append_candidates(state, candidates)
        except BaseException:
            self._cached_state = None
            self._cached_signature = None
            raise
        self._cached_state = state
        self._cached_signature = self._state_signature()

    def _append_legacy_candidates(
        self, existing: List[Dict[str, object]], candidates: List[Dict[str, object]]
    ) -> None:
        known = {str(record["id"]): record for record in existing}
        prepared: List[bytes] = []
        for candidate in candidates:
            owned = deepcopy(candidate)
            encoded = _encode_record(owned, self.root.tenant_id, self.allowed_kinds)
            record_id = str(owned["id"])
            prior = known.get(record_id)
            if prior is not None:
                if prior != owned:
                    raise ProtocolRefusal("duplicate_record_id", f"record id {record_id} has divergent payload")
                continue
            known[record_id] = owned
            prepared.append(encoded)
        if len(existing) + len(prepared) > MAX_LEDGER_RECORDS:
            raise ProtocolRefusal("ledger_record_limit", f"ledger maximum is {MAX_LEDGER_RECORDS} records")
        if prepared:
            _append_frame(self._legacy, b"".join(prepared))

    def _append_candidates(self, state: _ActiveState, candidates: List[Dict[str, object]]) -> None:
        new_records: Dict[str, Dict[str, object]] = {}
        prepared: List[Tuple[Dict[str, object], bytes]] = []
        for candidate in candidates:
            owned = deepcopy(candidate)
            encoded = _encode_record(owned, self.root.tenant_id, self.allowed_kinds)
            record_id = str(owned["id"])
            located = state.known.get(record_id)
            prior = located.record if located is not None else new_records.get(record_id)
            if prior is not None:
                if prior != owned:
                    raise ProtocolRefusal("duplicate_record_id", f"record id {record_id} has divergent payload")
                continue
            new_records[record_id] = owned
            prepared.append((owned, encoded))
        pending: List[Tuple[Dict[str, object], bytes]] = []

        def flush() -> None:
            if not pending:
                return
            path = self._segment_path(int(state.opening["segment_number"]))
            _append_frame(path, b"".join(encoded for _candidate, encoded in pending))
            first = int(state.opening["first_global_ordinal"])
            for candidate, encoded in pending:
                state.segment_records.append(candidate)
                state.segment_bytes += len(encoded)
                located = LocatedRecord(
                    candidate,
                    PhysicalCoordinate(
                        int(state.opening["segment_number"]),
                        len(state.segment_records),
                        first + len(state.segment_records) - 1,
                    ),
                )
                state.located.append(located)
                state.known[str(candidate["id"])] = located
                state.prefix_digest = _advance_prefix_digest(
                    state.prefix_digest, encoded
                )
            pending.clear()

        for candidate, encoded in prepared:
            if state.sealed_tail is not None:
                flush()
                self._recover_sealed_tail(state)
            max_records = int(state.opening["max_records"])
            max_bytes = int(state.opening["max_bytes"])
            pending_bytes = sum(len(item[1]) for item in pending)
            if (state.segment_records or pending) and (
                len(state.segment_records) + len(pending) >= max_records
                or state.segment_bytes + pending_bytes >= max_bytes
                or state.segment_bytes + pending_bytes + len(encoded) > max_bytes
            ):
                flush()
                self._rotate(state)
            pending.append((candidate, encoded))
        flush()

    def _recover_sealed_tail(self, state: _ActiveState) -> None:
        seal = state.sealed_tail
        assert seal is not None
        next_number = int(seal["segment_number"]) + 1
        path = self._segment_path(next_number)
        if path.exists():
            if not path.is_file() or path.stat().st_size != 0:
                raise IntegrityFailure("unexpected_segment_file", "unopened successor is not exact zero bytes")
        else:
            _append_frame(path, b"")
        opened = self._opened(
            next_number,
            int(seal["last_global_ordinal"]) + 1,
            str(seal["seal_digest"]),
            self._timestamp(datetime.now(timezone.utc)),
        )
        _append_frame(self._metadata, _encode_record(opened, self.root.tenant_id, _METADATA_KINDS))
        state.opening = opened
        state.segment_records = []
        state.segment_bytes = 0
        state.sealed_tail = None

    def _rotate(self, state: _ActiveState) -> None:
        number = int(state.opening["segment_number"])
        path = self._segment_path(number)
        data = path.read_bytes()
        now = self._timestamp(datetime.now(timezone.utc))
        seal: Dict[str, object] = {
            "schema_version": 1,
            "id": "run-segment-sealed-" + uuid7_hex(),
            "tenant_id": self.root.tenant_id,
            "timestamp": now,
            "kind": "segment_sealed",
            "segment_number": number,
            "opening_record_id": state.opening["id"],
            "last_global_ordinal": int(state.opening["first_global_ordinal"]) + len(state.segment_records) - 1,
            "record_count": len(state.segment_records),
            "byte_length": len(data),
            "segment_sha256": hashlib.sha256(data).hexdigest(),
            "seal_digest": "",
        }
        seal["seal_digest"] = segment_seal_digest(seal)
        _append_frame(self._metadata, _encode_record(seal, self.root.tenant_id, _METADATA_KINDS))
        next_number = number + 1
        opened = self._opened(
            next_number,
            int(seal["last_global_ordinal"]) + 1,
            str(seal["seal_digest"]),
            now,
        )
        _append_frame(self._segment_path(next_number), b"")
        _append_frame(self._metadata, _encode_record(opened, self.root.tenant_id, _METADATA_KINDS))
        state.opening = opened
        state.segment_records = []
        state.segment_bytes = 0

    def _opened(self, number: int, first: int, previous: Optional[str], timestamp: str) -> Dict[str, object]:
        return {
            "schema_version": 1,
            "id": "run-segment-opened-" + uuid7_hex(),
            "tenant_id": self.root.tenant_id,
            "timestamp": timestamp,
            "kind": "segment_opened",
            "segment_number": number,
            "first_global_ordinal": first,
            "previous_seal_digest": previous,
            "max_records": self.config.max_records,
            "max_bytes": self.config.max_bytes,
        }

    def _segment_path(self, number: int) -> Path:
        return self.root.resolve_relative(f"runs/segments/{number:08d}.jsonl")

    @staticmethod
    def _timestamp(now: datetime) -> str:
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
            raise ProtocolRefusal("timestamp_invalid", "activation time must be timezone-aware")
        return now.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")

    @staticmethod
    def _locate_legacy(records: List[Dict[str, object]]) -> List[LocatedRecord]:
        return [LocatedRecord(record, PhysicalCoordinate(-1, ordinal, ordinal)) for ordinal, record in enumerate(records, 1)]

    @staticmethod
    def _lookup(located: List[LocatedRecord], record_id: str) -> Optional[LocatedRecord]:
        for item in reversed(located):
            if item.record["id"] == record_id:
                return LocatedRecord(deepcopy(item.record), item.coordinate)
        return None

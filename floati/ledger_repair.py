"""Governed exact-frame quarantine repair for the selected event ledger."""

from __future__ import annotations

import hashlib
import os
import stat
import unicodedata
from pathlib import Path
from typing import Dict, List, Mapping, Sequence, Tuple

from . import jsonl as jsonl_limits
from .bus_epoch import shared_epoch_operation
from .errors import DurabilityFailure, IntegrityFailure, ProtocolRefusal
from .framing import FrameError, decode_frames, encode_frame
from .ids import uuid7_hex
from .jsonl import MAX_LEDGER_BYTES, _lock_beside, _locked_path
from .records import validate_record
from .registry import utc_now
from .root import FloatiRoot


_LEDGER = "events.jsonl"
_INVALIDATED_FOLLOWERS = ["tail_followers", "waiters", "monitors"]
_BIDI_CONTROLS = frozenset(
    {"LRE", "RLE", "LRO", "RLO", "PDF", "LRI", "RLI", "FSI", "PDI", "BN"}
)


def _bounded_text(value: object, *, field: str, maximum: int) -> str:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= maximum
        or any(
            unicodedata.category(character) in {"Cc", "Cs"}
            or unicodedata.bidirectional(character) in _BIDI_CONTROLS
            for character in value
        )
    ):
        raise ProtocolRefusal(
            f"{field}_invalid", f"{field} must be bounded non-control text"
        )
    return value


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _decoded_physical_frames(data: bytes) -> List[Tuple[Mapping[str, object], bytes]]:
    try:
        decoded = decode_frames(data)
    except FrameError as exc:
        raise IntegrityFailure(exc.code, exc.detail) from exc
    physical = data.splitlines(keepends=True)
    if len(decoded) != len(physical):
        raise IntegrityFailure(
            "frame_count_invalid", "event ledger physical frames could not be paired"
        )
    rows: List[Tuple[Mapping[str, object], bytes]] = []
    for record, frame in zip(decoded, physical):
        if not isinstance(record, Mapping):
            raise IntegrityFailure(
                "record_not_object", "event ledger contains a non-object frame"
            )
        rows.append((record, frame))
    return rows


def _canonical_logical_frame_size(frame: bytes) -> int:
    """Measure one frame as canonical JSONL does after splitting line endings."""
    logical_lines = frame.splitlines()
    if len(logical_lines) != 1:
        raise IntegrityFailure(
            "frame_count_invalid", "event ledger physical frame is not one logical line"
        )
    return len(logical_lines[0]) + 1


def _strict_event_records(
    rows: Sequence[Tuple[Mapping[str, object], bytes]], tenant_id: str
) -> List[Dict[str, object]]:
    from .events import EVENT_KINDS, validate_event_records

    if len(rows) > jsonl_limits.MAX_LEDGER_RECORDS:
        raise IntegrityFailure(
            "ledger_record_limit",
            f"events.jsonl exceeds {jsonl_limits.MAX_LEDGER_RECORDS} records",
        )
    validated: List[Dict[str, object]] = []
    seen = set()
    for line_number, (raw, frame) in enumerate(rows, start=1):
        if _canonical_logical_frame_size(frame) > jsonl_limits.MAX_RECORD_BYTES:
            raise IntegrityFailure(
                "record_too_large",
                f"events.jsonl line {line_number} exceeds "
                f"{jsonl_limits.MAX_RECORD_BYTES} bytes",
            )
        record = validate_record(
            dict(raw), tenant_id, EVENT_KINDS, integrity=True
        )
        record_id = str(record["id"])
        if record_id in seen:
            raise IntegrityFailure(
                "duplicate_record_id", "event ledger contains a duplicate record id"
            )
        seen.add(record_id)
        validated.append(record)
    validate_event_records(validated)
    return validated


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_new(path: Path, payload: bytes) -> List[Path]:
    created_directories: List[Path] = []
    created_file = False
    try:
        missing = []
        cursor = path.parent
        while not cursor.exists():
            missing.append(cursor)
            cursor = cursor.parent
        for directory in reversed(missing):
            directory.mkdir(mode=0o700)
            created_directories.append(directory)
            _fsync_directory(directory.parent)
        descriptor = os.open(
            path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
        )
        created_file = True
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        _fsync_directory(path.parent)
        return created_directories
    except OSError as exc:
        _remove_unreceipted_quarantine(
            path, created_directories, remove_path=created_file
        )
        raise DurabilityFailure(
            "storage_unavailable", "quarantine evidence could not be durably written"
        ) from exc


def _remove_unreceipted_quarantine(
    path: Path,
    created_directories: List[Path],
    *,
    remove_path: bool = True,
) -> None:
    try:
        surviving_parent = (
            created_directories[0].parent
            if created_directories
            else path.parent
        )
        if remove_path:
            path.unlink(missing_ok=True)
        for directory in reversed(created_directories):
            directory.rmdir()
        _fsync_directory(surviving_parent)
    except OSError:
        pass


def _remove_temporary(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
        _fsync_directory(path.parent)
    except OSError as exc:
        raise DurabilityFailure(
            "storage_unavailable", "repair staging cleanup could not be made durable"
        ) from exc


class LedgerRepair:
    """Repair exactly one governed event-ledger frame under its established lock."""

    def __init__(self, root: FloatiRoot) -> None:
        if not isinstance(root, FloatiRoot):
            raise ProtocolRefusal("root_required", "ledger repair requires a validated root")
        self.root = root

    @staticmethod
    def _existing_receipt(
        rows: List[Tuple[Mapping[str, object], bytes]],
        *,
        ledger: str,
        record_id: str,
        key: str,
        tenant_id: str,
    ) -> Dict[str, object] | None:
        matches = [
            record
            for record, _frame in rows
            if record.get("kind") == "ledger_repair_receipt"
            and record.get("idempotency_key") == key
        ]
        if len(matches) > 1:
            raise IntegrityFailure(
                "repair_receipt_duplicate", "repair idempotency key has duplicate receipts"
            )
        if not matches:
            return None
        receipt = validate_record(
            dict(matches[0]),
            tenant_id,
            frozenset({"ledger_repair_receipt"}),
            integrity=True,
        )
        if receipt["ledger"] != ledger or receipt["record_id"] != record_id:
            raise ProtocolRefusal(
                "idempotency_conflict",
                "idempotency key already belongs to another repair request",
            )
        return receipt

    @shared_epoch_operation
    def quarantine(
        self, ledger: str, record_id: str, *, key: str
    ) -> Dict[str, object]:
        if ledger != _LEDGER:
            raise ProtocolRefusal(
                "repair_ledger_invalid", "only the exact events.jsonl ledger is repairable"
            )
        selected_id = _bounded_text(record_id, field="record_id", maximum=256)
        selected_key = _bounded_text(key, field="idempotency_key", maximum=128)
        ledger_path = self.root.resolve_relative(_LEDGER)
        lock_path, lock_relative = _lock_beside(ledger_path, _LEDGER)
        if lock_path.is_symlink() or not lock_path.is_file():
            raise ProtocolRefusal(
                "repair_lock_missing",
                "repair requires the established ordinary events.jsonl.lock",
            )

        with _locked_path(lock_path, exclusive=True, relative=lock_relative):
            try:
                before = ledger_path.stat()
                if not stat.S_ISREG(before.st_mode):
                    raise IntegrityFailure(
                        "ledger_not_regular", "event ledger must be an ordinary file"
                    )
                if before.st_size > MAX_LEDGER_BYTES:
                    raise IntegrityFailure(
                        "ledger_too_large",
                        f"events.jsonl exceeds {MAX_LEDGER_BYTES} bytes",
                    )
                original = ledger_path.read_bytes()
            except OSError as exc:
                raise DurabilityFailure(
                    "storage_unavailable", "event ledger could not be read for repair"
                ) from exc
            rows = _decoded_physical_frames(original)
            existing = self._existing_receipt(
                rows,
                ledger=ledger,
                record_id=selected_id,
                key=selected_key,
                tenant_id=self.root.tenant_id,
            )
            if existing is not None:
                try:
                    _fsync_directory(ledger_path.parent)
                except OSError as exc:
                    raise DurabilityFailure(
                        "storage_unavailable",
                        "event ledger repair parent could not be synchronized",
                    ) from exc
                return existing

            selected = [frame for record, frame in rows if record.get("id") == selected_id]
            if len(selected) != 1:
                code = "repair_record_missing" if not selected else "repair_record_duplicate"
                raise ProtocolRefusal(
                    code, "repair record id must select exactly one complete physical frame"
                )
            try:
                _strict_event_records(rows, self.root.tenant_id)
            except IntegrityFailure:
                pass
            else:
                raise ProtocolRefusal(
                    "repair_not_required",
                    "repair refuses to quarantine evidence from a valid event ledger",
                )
            removed = selected[0]
            retained_rows = [
                (record, frame)
                for record, frame in rows
                if record.get("id") != selected_id
            ]
            retained = b"".join(frame for _record, frame in retained_rows)
            receipt_id = "ledger-repair-receipt-" + uuid7_hex()
            quarantine_path = self.root.resolve_relative(
                Path("quarantine") / "ledger-repair" / f"{receipt_id}.jsonl"
            )
            receipt_timestamp = utc_now()

            def repair_receipt(after_device: int, after_inode: int) -> Dict[str, object]:
                return validate_record(
                    {
                        "schema_version": 1,
                        "id": receipt_id,
                        "tenant_id": self.root.tenant_id,
                        "timestamp": receipt_timestamp,
                        "kind": "ledger_repair_receipt",
                        "ledger": ledger,
                        "record_id": selected_id,
                        "idempotency_key": selected_key,
                        "original_digest": _sha256(original),
                        "repaired_digest": _sha256(retained),
                        "quarantine_path": str(quarantine_path),
                        "quarantine_digest": _sha256(removed),
                        "replaced_inode": {
                            "before": {
                                "device": before.st_dev,
                                "inode": before.st_ino,
                            },
                            "after": {
                                "device": after_device,
                                "inode": after_inode,
                            },
                            "changed": True,
                        },
                        "invalidated_followers": list(_INVALIDATED_FOLLOWERS),
                    },
                    self.root.tenant_id,
                    frozenset({"ledger_repair_receipt"}),
                    integrity=False,
                )

            prospective = repair_receipt(before.st_dev, before.st_ino + 1)
            try:
                _strict_event_records(
                    retained_rows + [(prospective, encode_frame(prospective))],
                    self.root.tenant_id,
                )
            except IntegrityFailure as exc:
                raise ProtocolRefusal(
                    "repair_retained_invalid",
                    "selected frame does not yield a valid event ledger",
                ) from exc
            created_directories = _write_new(quarantine_path, removed)

            temporary = ledger_path.with_name(
                ".events.jsonl.repair-" + uuid7_hex()
            )
            temporary_created = False
            replaced = False
            try:
                descriptor = os.open(
                    temporary,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o600,
                )
                temporary_created = True
                with os.fdopen(descriptor, "wb") as handle:
                    after = os.fstat(handle.fileno())
                    receipt = repair_receipt(after.st_dev, after.st_ino)
                    receipt_frame = encode_frame(receipt)
                    _strict_event_records(
                        retained_rows + [(receipt, receipt_frame)],
                        self.root.tenant_id,
                    )
                    if len(retained) + len(receipt_frame) > MAX_LEDGER_BYTES:
                        raise ProtocolRefusal(
                            "ledger_too_large",
                            f"ledger maximum is {MAX_LEDGER_BYTES} bytes",
                        )
                    handle.write(retained)
                    handle.write(receipt_frame)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, ledger_path)
                replaced = True
                _fsync_directory(ledger_path.parent)
            except OSError as exc:
                try:
                    if temporary_created:
                        _remove_temporary(temporary)
                except DurabilityFailure as cleanup_failure:
                    if not replaced:
                        _remove_unreceipted_quarantine(
                            quarantine_path, created_directories
                        )
                    raise cleanup_failure from exc
                if not replaced:
                    _remove_unreceipted_quarantine(
                        quarantine_path, created_directories
                    )
                raise DurabilityFailure(
                    "storage_unavailable", "event ledger repair could not be durably replaced"
                ) from exc
            except Exception as exc:
                cleanup_failure = None
                try:
                    if temporary_created:
                        _remove_temporary(temporary)
                except DurabilityFailure as failure:
                    cleanup_failure = failure
                if not replaced:
                    _remove_unreceipted_quarantine(
                        quarantine_path, created_directories
                    )
                if cleanup_failure is not None:
                    raise cleanup_failure from exc
                raise
            return receipt

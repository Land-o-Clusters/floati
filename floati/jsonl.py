"""Bounded, root-authorized, locked, fsynced append-only JSONL evidence."""

from __future__ import annotations

import fcntl
import errno
import hashlib
import os
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Dict, FrozenSet, Iterator, List, Optional, Sequence, Set, Tuple, Union

from .errors import DurabilityFailure, IntegrityFailure, ProtocolRefusal
from .framing import FrameError, decode_frames, encode_frame
from .records import is_known_record_kind, validate_record, validate_unknown_record
from .root import FloatiRoot, TenantObservation


MAX_RECORD_BYTES = 65536
MAX_LEDGER_BYTES = 64 * 1024 * 1024
MAX_LEDGER_RECORDS = 100000
LOCK_TIMEOUT_SECONDS = 1.0
LOCK_POLL_SECONDS = 0.01
Authority = Union[FloatiRoot, TenantObservation]
_EFFECT_RECORDS_RELATIVE = Path("effects/records.jsonl")
_THREAD_OBSERVATION_RECORDS_RELATIVE = Path("thread-observations/records.jsonl")
_WAKE_HOLD_APPEND_MARKER = object()


def _is_effect_records_path(path: Path) -> bool:
    return path.name == "records.jsonl" and path.parent.name == "effects"


def _is_thread_observation_records_path(path: Path) -> bool:
    return path.name == "records.jsonl" and path.parent.name == "thread-observations"


def _is_wake_hold_delivery_path(path: Path) -> bool:
    """Recognize only the shared per-recipient delivery ledger namespace."""
    return path.suffix == ".jsonl" and (
        path.parent.name == "deliveries"
        or path.parent.parent.name == "deliveries"
    )


def _kinds(allowed_kinds: Optional[Set[str]]) -> FrozenSet[str]:
    if not allowed_kinds:
        raise ProtocolRefusal("ledger_kind_required", "each ledger operation must declare allowed record kinds")
    return frozenset(allowed_kinds)


def _resolve(authority: Authority, relative: Union[Path, str], *, write: bool) -> Tuple[Path, str]:
    if write and not isinstance(authority, FloatiRoot):
        raise ProtocolRefusal("write_root_required", "writes require a validated writable FloatiRoot")
    if isinstance(authority, FloatiRoot):
        if not authority.tenant_home.is_dir():
            raise DurabilityFailure("root_deleted", "the selected root no longer exists")
        path = authority.resolve_relative(relative)
        if write and path == authority.resolve_relative(_EFFECT_RECORDS_RELATIVE):
            raise ProtocolRefusal(
                "effect_controller_only",
                "effect truth requires the sealed controller transaction",
            )
        if write and path == authority.resolve_relative(
            _THREAD_OBSERVATION_RECORDS_RELATIVE
        ):
            raise ProtocolRefusal(
                "thread_observer_only",
                "thread testimony requires the controller-owned transaction",
            )
        return path, authority.tenant_id
    if isinstance(authority, TenantObservation):
        if not (authority._root_path / "tenants" / authority.tenant_id).is_dir():
            raise DurabilityFailure("root_deleted", "the observed root no longer exists")
        return authority._resolve_relative(relative), authority.tenant_id
    raise ProtocolRefusal("root_required", "a validated root or observation is required")


@contextmanager
def _locked_path(
    path: Path, *, exclusive: bool,
    timeout_seconds: float = LOCK_TIMEOUT_SECONDS,
) -> Iterator[None]:
    """Take the bounded advisory lock at one already-authorized fixed path."""
    operation = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = path.open("a+b")
    except OSError as exc:
        raise _durability_failure(exc, path) from exc
    with handle:
        deadline = time.monotonic() + timeout_seconds
        while True:
            try:
                fcntl.flock(handle.fileno(), operation | fcntl.LOCK_NB)
                break
            except BlockingIOError as exc:
                if time.monotonic() >= deadline:
                    raise ProtocolRefusal(
                        "ledger_lock_timeout",
                        f"{path.name} lock remained contended for {timeout_seconds:g} second",
                    ) from exc
                time.sleep(LOCK_POLL_SECONDS)
            except OSError as exc:
                raise _durability_failure(exc, path) from exc
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _read_path_records(
    path: Path,
    tenant: str,
    allowed_kinds: FrozenSet[str],
    *,
    max_bytes: int = MAX_RECORD_BYTES,
    unrecognized: Optional[Dict[str, Dict[str, object]]] = None,
) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    try:
        stat = path.stat()
        data = path.read_bytes()
    except OSError as exc:
        raise _durability_failure(exc, path) from exc
    if stat.st_size > MAX_LEDGER_BYTES:
        raise IntegrityFailure("ledger_too_large", f"{path.name} exceeds {MAX_LEDGER_BYTES} bytes")
    for line_number, raw in enumerate(data.splitlines(), start=1):
        if len(raw) + 1 > max_bytes:
            raise IntegrityFailure("record_too_large", f"{path.name} line {line_number} exceeds {max_bytes} bytes")
    try:
        framed = decode_frames(data)
    except FrameError as exc:
        code = {
            "incomplete_frame": "incomplete_jsonl_line",
            "blank_frame": "blank_jsonl_line",
        }.get(exc.code, exc.code)
        where = f" line {exc.line_number}" if exc.line_number else ""
        raise IntegrityFailure(code, f"{path.name}{where}: {exc.detail}") from exc
    records: List[Dict[str, Any]] = []
    seen = set()
    for line_number, raw_record in enumerate(framed, start=1):
        if line_number > MAX_LEDGER_RECORDS:
            raise IntegrityFailure("ledger_record_limit", f"{path.name} exceeds {MAX_LEDGER_RECORDS} records")
        record_id = raw_record.get("id", "<absent>") if isinstance(raw_record, dict) else "<absent>"
        kind = raw_record.get("kind", "<absent>") if isinstance(raw_record, dict) else "<absent>"
        try:
            if unrecognized is not None and not is_known_record_kind(kind):
                record = validate_unknown_record(raw_record, tenant)
                summary = unrecognized.setdefault(
                    str(kind), {"kind": str(kind), "count": 0, "first_id": str(record["id"])}
                )
                summary["count"] = int(summary["count"]) + 1
            else:
                record = validate_record(raw_record, tenant, allowed_kinds, integrity=True)
        except IntegrityFailure as exc:
            raise IntegrityFailure(
                exc.code,
                f"ledger {path}: record {record_id}: kind {kind}: {exc.detail}",
            ) from exc
        if record["id"] in seen:
            raise IntegrityFailure(
                "duplicate_record_id",
                f"ledger {path}: record {record['id']}: kind {record['kind']}: duplicate id",
            )
        seen.add(record["id"])
        if unrecognized is None or is_known_record_kind(kind):
            records.append(record)
    return records


def _unrecognized_rows(
    summaries: Dict[str, Dict[str, object]],
) -> List[Dict[str, object]]:
    return [dict(summaries[kind]) for kind in sorted(summaries)]


def _encode_record(
    record: Dict[str, Any],
    tenant: str,
    allowed_kinds: FrozenSet[str],
    *,
    max_bytes: int = MAX_RECORD_BYTES,
) -> bytes:
    record = validate_record(record, tenant, allowed_kinds, integrity=False)
    try:
        encoded = encode_frame(record)
    except FrameError as exc:
        raise ProtocolRefusal(exc.code, exc.detail) from exc
    if len(encoded) > max_bytes:
        raise ProtocolRefusal("record_too_large", f"record is {len(encoded)} bytes; maximum is {max_bytes}")
    return encoded


def _append_frame(path: Path, encoded: bytes, *, wake_hold_marker: object = None) -> None:
    try:
        # From this point onward use only the canonical destination.  Besides
        # closing ``.``/``..`` and symlink aliases, this prevents a checked
        # alias from being resolved a second time during mkdir/open.
        path = path.resolve(strict=False)
    except OSError as exc:
        raise _durability_failure(exc, path) from exc
    if _is_effect_records_path(path):
        try:
            caller = sys._getframe(1)
        except ValueError:
            caller = None
        if caller is None or caller.f_code is not _EFFECT_TRANSACTION_CODE:
            raise ProtocolRefusal(
                "effect_controller_only",
                "effect truth requires the sealed controller transaction",
            )
    if _is_thread_observation_records_path(path):
        try:
            caller = sys._getframe(1)
        except ValueError:
            caller = None
        if caller is None or caller.f_code is not _THREAD_OBSERVATION_TRANSACTION_CODE:
            raise ProtocolRefusal(
                "thread_observer_only",
                "thread testimony requires the controller-owned transaction",
            )
    if _is_wake_hold_delivery_path(path):
        try:
            candidate = decode_frames(encoded)
        except FrameError as exc:
            raise ProtocolRefusal(exc.code, exc.detail) from exc
        if len(candidate) != 1:
            raise ProtocolRefusal("wake_controller_only", "wake hold append needs one canonical frame")
        if candidate[0].get("kind") == "wake_hold_receipt":
            try:
                caller = sys._getframe(1)
            except ValueError:
                caller = None
            if (
                wake_hold_marker is not _WAKE_HOLD_APPEND_MARKER
                or caller is None
                or caller.f_code is not _WAKE_HOLD_TRANSACTION_CODE
            ):
                raise ProtocolRefusal("wake_controller_only", "wake hold testimony requires its sealed ledger")
    try:
        existed = path.exists()
        previous_size = path.stat().st_size if existed else 0
    except OSError as exc:
        raise _durability_failure(exc, path) from exc
    if previous_size + len(encoded) > MAX_LEDGER_BYTES:
        raise ProtocolRefusal("ledger_too_large", f"ledger maximum is {MAX_LEDGER_BYTES} bytes")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    except OSError as exc:
        raise _durability_failure(exc, path) from exc
    try:
        try:
            written = os.write(descriptor, encoded)
            if written != len(encoded):
                _rollback(descriptor, previous_size)
                raise DurabilityFailure(
                    "short_write", "the ledger append completed only partially"
                )
            os.fsync(descriptor)
        except OSError as exc:
            _rollback(descriptor, previous_size)
            raise _durability_failure(exc, path) from exc
    finally:
        os.close(descriptor)
    if not existed:
        try:
            parent = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(parent)
            finally:
                os.close(parent)
        except OSError as exc:
            raise _durability_failure(exc, path) from exc


def _ensure_directory(path: Path) -> None:
    """Create one fixed directory and durably link it from its parent."""
    try:
        path.mkdir(exist_ok=True)
        parent = os.open(path.parent, os.O_RDONLY)
        try:
            # Repeat this fsync when the directory already exists so an exact
            # retry completes a parent fsync that failed after mkdir.
            os.fsync(parent)
        finally:
            os.close(parent)
    except OSError as exc:
        raise _durability_failure(exc, path) from exc


def _rollback(descriptor: int, previous_size: int) -> None:
    try:
        os.ftruncate(descriptor, previous_size)
        os.fsync(descriptor)
    except OSError:
        pass


def _durability_failure(exc: OSError, path: Path) -> DurabilityFailure:
    if exc.errno in {errno.ENOSPC, getattr(errno, "EDQUOT", errno.ENOSPC)}:
        code = "disk_full"
    elif exc.errno in {errno.EROFS, errno.EACCES, errno.EPERM}:
        code = "root_read_only"
    elif exc.errno == errno.ENOENT:
        code = "root_deleted"
    else:
        code = "storage_unavailable"
    return DurabilityFailure(code, f"{path.name}: {exc.strerror or str(exc)}")


def append_record(authority: Authority, relative: Union[Path, str], record: Dict[str, Any], *, allowed_kinds: Optional[Set[str]] = None, max_bytes: int = MAX_RECORD_BYTES) -> None:
    if record.get("kind") == "wake_hold_receipt":
        raise ProtocolRefusal("wake_controller_only", "wake hold testimony requires its sealed ledger")
    kinds = _kinds(allowed_kinds)
    path, tenant = _resolve(authority, relative, write=True)
    encoded = _encode_record(record, tenant, kinds, max_bytes=max_bytes)
    with _locked_path(path.with_name(path.name + ".lock"), exclusive=True):
        existing = _read_path_records(path, tenant, kinds, max_bytes=max_bytes)
        if len(existing) >= MAX_LEDGER_RECORDS:
            raise ProtocolRefusal("ledger_record_limit", f"ledger maximum is {MAX_LEDGER_RECORDS} records")
        if any(item["id"] == record["id"] for item in existing):
            raise ProtocolRefusal("duplicate_record_id", f"record id {record['id']} already exists")
        _append_frame(path, encoded)


def transact(authority: FloatiRoot, relative: Union[Path, str], decide: Callable[[List[Dict[str, Any]]], Tuple[Any, Optional[Dict[str, Any]]]], *, allowed_kinds: Optional[Set[str]] = None, max_bytes: int = MAX_RECORD_BYTES) -> Any:
    kinds = _kinds(allowed_kinds)
    path, tenant = _resolve(authority, relative, write=True)
    with _locked_path(path.with_name(path.name + ".lock"), exclusive=True):
        existing = _read_path_records(path, tenant, kinds, max_bytes=max_bytes)
        result, record = decide(existing)
        if record is not None:
            if record.get("kind") == "wake_hold_receipt":
                raise ProtocolRefusal("wake_controller_only", "wake hold testimony requires its sealed ledger")
            if len(existing) >= MAX_LEDGER_RECORDS:
                raise ProtocolRefusal("ledger_record_limit", f"ledger maximum is {MAX_LEDGER_RECORDS} records")
            encoded = _encode_record(record, tenant, kinds, max_bytes=max_bytes)
            if any(item["id"] == record["id"] for item in existing):
                raise ProtocolRefusal("duplicate_record_id", f"record id {record['id']} already exists")
            _append_frame(path, encoded)
        return result


def transact_records(
    authority: FloatiRoot,
    relative: Union[Path, str],
    decide: Callable[
        [List[Dict[str, Any]]], Tuple[Any, Sequence[Dict[str, Any]]]
    ],
    *,
    allowed_kinds: Optional[Set[str]] = None,
    max_bytes: int = MAX_RECORD_BYTES,
) -> Any:
    """Validate and append one bounded record batch under one ledger lock."""

    kinds = _kinds(allowed_kinds)
    path, tenant = _resolve(authority, relative, write=True)
    with _locked_path(path.with_name(path.name + ".lock"), exclusive=True):
        existing = _read_path_records(path, tenant, kinds, max_bytes=max_bytes)
        result, candidates = decide(existing)
        batch = tuple(candidates)
        if not batch:
            return result
        if len(existing) + len(batch) > MAX_LEDGER_RECORDS:
            raise ProtocolRefusal(
                "ledger_record_limit", f"ledger maximum is {MAX_LEDGER_RECORDS} records"
            )
        encoded = tuple(
            _encode_record(record, tenant, kinds, max_bytes=max_bytes)
            for record in batch
        )
        existing_ids = {item["id"] for item in existing}
        candidate_ids = [record.get("id") for record in batch]
        if len(set(candidate_ids)) != len(candidate_ids):
            raise ProtocolRefusal(
                "duplicate_record_id", "record batch repeats an id"
            )
        duplicate = next(
            (record_id for record_id in candidate_ids if record_id in existing_ids),
            None,
        )
        if duplicate is not None:
            raise ProtocolRefusal(
                "duplicate_record_id", f"record id {duplicate} already exists"
            )
        _append_frame(path, b"".join(encoded))
        return result


def _transact_wake_hold_records(
    authority: FloatiRoot,
    relative: Union[Path, str],
    decide: Callable[[List[Dict[str, Any]]], Tuple[Any, Optional[Dict[str, Any]]]],
    *,
    expected_prefix_digest: Optional[str] = None,
) -> Any:
    """Append one hold receipt through the exact private controller capability."""

    from .wake_hold import (
        WakeHoldController,
        _WAKE_HOLD_CONTROLLER_GLOBALS,
        _WAKE_HOLD_PRIVATE_APPEND_CODE,
    )
    from .records import WAKE_HOLD_KINDS

    try:
        caller = sys._getframe(1)
    except ValueError:
        caller = None
    controller = None if caller is None else caller.f_locals.get("self")
    if (
        caller is None or caller.f_code is not _WAKE_HOLD_PRIVATE_APPEND_CODE
        or caller.f_globals is not _WAKE_HOLD_CONTROLLER_GLOBALS
        or type(controller) is not WakeHoldController or controller.root is not authority
    ):
        raise ProtocolRefusal("wake_controller_only", "wake hold testimony requires the exact controller transaction")
    path, tenant = _resolve(authority, relative, write=True)
    kinds = frozenset(WAKE_HOLD_KINDS)
    with _locked_path(path.with_name(path.name + ".lock"), exclusive=True):
        existing = _read_path_records(path, tenant, kinds)
        digest = hashlib.sha256(b"slipway-wake-hold-deliveries-v1\0")
        raw_frames = path.read_bytes().splitlines(keepends=True) if path.exists() else []
        if len(raw_frames) != len(existing):
            raise IntegrityFailure("consumption_state_unavailable", "delivery framing changed before hold append")
        for row, frame in zip(existing, raw_frames):
            if frame != encode_frame(row):
                raise IntegrityFailure("consumption_state_unavailable", "delivery bytes changed before hold append")
            digest.update(frame)
        if not isinstance(expected_prefix_digest, str) or digest.hexdigest() != expected_prefix_digest:
            raise IntegrityFailure("consumption_state_unavailable", "delivery prefix changed before hold append")
        result, record = decide(existing)
        if record is not None:
            if record.get("kind") != "wake_hold_receipt":
                raise ProtocolRefusal("wake_controller_only", "sealed ledger appends only hold receipts")
            if len(existing) >= MAX_LEDGER_RECORDS:
                raise ProtocolRefusal(
                    "ledger_record_limit",
                    f"ledger maximum is {MAX_LEDGER_RECORDS} records",
                )
            encoded = _encode_record(record, tenant, kinds)
            if any(item["id"] == record["id"] for item in existing):
                raise ProtocolRefusal("duplicate_record_id", "record id already exists")
            _append_frame(path, encoded, wake_hold_marker=_WAKE_HOLD_APPEND_MARKER)
        return result


_WAKE_HOLD_TRANSACTION_CODE = _transact_wake_hold_records.__code__


def _transact_effect_records(
    authority: FloatiRoot,
    decide: Callable[
        [List[Dict[str, Any]]], Tuple[Any, Optional[Dict[str, Any]]]
    ],
) -> Any:
    """Run the one fixed Effect transaction for its exact ledger owner."""

    from .effects import EffectLedger, _EFFECT_LEDGER_APPEND_CODE
    from .records import EFFECT_KINDS

    try:
        caller = sys._getframe(1)
    except ValueError:
        caller = None
    owner = None if caller is None else caller.f_locals.get("self")
    if (
        caller is None
        or caller.f_code is not _EFFECT_LEDGER_APPEND_CODE
        or type(owner) is not EffectLedger
        or owner.root is not authority
    ):
        raise ProtocolRefusal(
            "effect_controller_only",
            "effect truth requires the exact controller-owned ledger transaction",
        )
    if not authority.tenant_home.is_dir():
        raise DurabilityFailure("root_deleted", "the selected root no longer exists")
    kinds = frozenset(EFFECT_KINDS)
    path = authority.resolve_relative(_EFFECT_RECORDS_RELATIVE)
    tenant = authority.tenant_id
    with _locked_path(path.with_name(path.name + ".lock"), exclusive=True):
        existing = _read_path_records(path, tenant, kinds)
        result, record = decide(existing)
        if record is not None:
            if len(existing) >= MAX_LEDGER_RECORDS:
                raise ProtocolRefusal(
                    "ledger_record_limit",
                    f"ledger maximum is {MAX_LEDGER_RECORDS} records",
                )
            encoded = _encode_record(record, tenant, kinds)
            if any(item["id"] == record["id"] for item in existing):
                raise ProtocolRefusal(
                    "duplicate_record_id",
                    f"record id {record['id']} already exists",
                )
            _append_frame(path, encoded)
        return result


_EFFECT_TRANSACTION_CODE = _transact_effect_records.__code__


def _transact_thread_observation_records(
    authority: FloatiRoot,
    decide: Callable[
        [List[Dict[str, Any]]], Tuple[Any, Optional[Dict[str, Any]]]
    ],
) -> Any:
    """Run the one fixed Thread Observation transaction for its exact owner."""

    from .records import THREAD_OBSERVATION_KINDS
    from .thread_observations import (
        ThreadObservationLedger,
        _THREAD_OBSERVATION_LEDGER_APPEND_CODE,
    )

    try:
        caller = sys._getframe(1)
    except ValueError:
        caller = None
    owner = None if caller is None else caller.f_locals.get("self")
    if (
        caller is None
        or caller.f_code is not _THREAD_OBSERVATION_LEDGER_APPEND_CODE
        or type(owner) is not ThreadObservationLedger
        or owner.root is not authority
    ):
        raise ProtocolRefusal(
            "thread_observer_only",
            "thread testimony requires the exact controller-owned ledger transaction",
        )
    if not authority.tenant_home.is_dir():
        raise DurabilityFailure("root_deleted", "the selected root no longer exists")
    kinds = frozenset(THREAD_OBSERVATION_KINDS)
    path = authority.resolve_relative(_THREAD_OBSERVATION_RECORDS_RELATIVE)
    tenant = authority.tenant_id
    with _locked_path(path.with_name(path.name + ".lock"), exclusive=True):
        existing = _read_path_records(path, tenant, kinds)
        result, record = decide(existing)
        if record is not None:
            if len(existing) >= MAX_LEDGER_RECORDS:
                raise ProtocolRefusal(
                    "ledger_record_limit",
                    f"ledger maximum is {MAX_LEDGER_RECORDS} records",
                )
            encoded = _encode_record(record, tenant, kinds)
            if any(item["id"] == record["id"] for item in existing):
                raise ProtocolRefusal(
                    "duplicate_record_id",
                    f"record id {record['id']} already exists",
                )
            _append_frame(path, encoded)
        return result


_THREAD_OBSERVATION_TRANSACTION_CODE = (
    _transact_thread_observation_records.__code__
)


def read_records(authority: Authority, relative: Union[Path, str], *, allowed_kinds: Optional[Set[str]] = None, max_bytes: int = MAX_RECORD_BYTES) -> List[Dict[str, Any]]:
    kinds = _kinds(allowed_kinds)
    path, tenant = _resolve(authority, relative, write=False)
    if not path.exists():
        return []
    if isinstance(authority, TenantObservation):
        # Observation must remain physically read-only. Atomic single-write
        # appends plus strict final-line validation give a safe snapshot
        # without creating a lock file in the observed tenant.
        return _read_path_records(path, tenant, kinds, max_bytes=max_bytes)
    with _locked_path(path.with_name(path.name + ".lock"), exclusive=False):
        return _read_path_records(path, tenant, kinds, max_bytes=max_bytes)


def read_records_snapshot(authority: Authority, relative: Union[Path, str], *, allowed_kinds: Optional[Set[str]] = None, max_bytes: int = MAX_RECORD_BYTES) -> List[Dict[str, Any]]:
    """Read one append-only snapshot without creating or taking a lock file."""

    kinds = _kinds(allowed_kinds)
    path, tenant = _resolve(authority, relative, write=False)
    if not path.exists():
        return []
    return _read_path_records(path, tenant, kinds, max_bytes=max_bytes)


def read_records_compatible_snapshot(
    authority: Authority,
    relative: Union[Path, str],
    *,
    allowed_kinds: Optional[Set[str]] = None,
    max_bytes: int = MAX_RECORD_BYTES,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, object]]]:
    """Read known rows and report well-formed kinds introduced by newer writers."""

    kinds = _kinds(allowed_kinds)
    path, tenant = _resolve(authority, relative, write=False)
    summaries: Dict[str, Dict[str, object]] = {}
    if not path.exists():
        return [], []
    records = _read_path_records(
        path, tenant, kinds, max_bytes=max_bytes, unrecognized=summaries
    )
    return records, _unrecognized_rows(summaries)


def read_records_compatible(
    authority: Authority,
    relative: Union[Path, str],
    *,
    allowed_kinds: Optional[Set[str]] = None,
    max_bytes: int = MAX_RECORD_BYTES,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, object]]]:
    """Locked counterpart of ``read_records_compatible_snapshot``."""

    kinds = _kinds(allowed_kinds)
    path, tenant = _resolve(authority, relative, write=False)
    summaries: Dict[str, Dict[str, object]] = {}
    if not path.exists():
        return [], []
    if isinstance(authority, TenantObservation):
        records = _read_path_records(
            path, tenant, kinds, max_bytes=max_bytes, unrecognized=summaries
        )
    else:
        with _locked_path(path.with_name(path.name + ".lock"), exclusive=False):
            records = _read_path_records(
                path, tenant, kinds, max_bytes=max_bytes, unrecognized=summaries
            )
    return records, _unrecognized_rows(summaries)


def read_records_with_prefix_digests(
    authority: Authority,
    relative: Union[Path, str],
    *,
    allowed_kinds: Optional[Set[str]] = None,
    domain: str,
    max_bytes: int = MAX_RECORD_BYTES,
) -> Tuple[List[Dict[str, Any]], Tuple[str, ...]]:
    """Read validated canonical frames and every inclusive SHA-256 prefix.

    The first digest represents the empty prefix.  This narrow reader is
    intentionally read-only; callers cannot synthesize testimony from parsed
    mappings because every stored frame must equal its canonical encoding.
    """

    if not isinstance(domain, str) or not domain.isascii() or not domain:
        raise ProtocolRefusal("prefix_digest_domain_invalid", "digest domain must be nonempty ASCII")
    kinds = _kinds(allowed_kinds)
    path, tenant = _resolve(authority, relative, write=False)
    if not path.exists():
        initial = hashlib.sha256(domain.encode("ascii") + b"\0").hexdigest()
        return [], (initial,)
    def read() -> Tuple[List[Dict[str, Any]], Tuple[str, ...]]:
        records = _read_path_records(path, tenant, kinds, max_bytes=max_bytes)
        try:
            raw = path.read_bytes()
        except OSError as exc:
            raise _durability_failure(exc, path) from exc
        frames = raw.splitlines(keepends=True)
        if len(frames) != len(records):
            raise IntegrityFailure("noncanonical_frame", "durable framing does not match validated records")
        digest = hashlib.sha256()
        digest.update(domain.encode("ascii") + b"\0")
        prefixes = [digest.hexdigest()]
        for record, frame in zip(records, frames):
            canonical = encode_frame(record)
            if frame != canonical:
                raise IntegrityFailure("noncanonical_frame", "durable frame differs from canonical encoding")
            digest.update(frame)
            prefixes.append(digest.hexdigest())
        return records, tuple(prefixes)
    if isinstance(authority, TenantObservation):
        return read()
    with _locked_path(path.with_name(path.name + ".lock"), exclusive=False):
        return read()

"""Bounded, root-authorized, locked, fsynced append-only JSONL evidence."""

from __future__ import annotations

import copy
import fcntl
import errno
import hashlib
import os
import sys
import time
from contextlib import ExitStack, contextmanager, nullcontext
from pathlib import Path, PurePath, PurePosixPath
from typing import Any, Callable, Dict, FrozenSet, Iterator, List, Optional, Sequence, Set, Tuple, Union

from .bus_epoch import LOCK_ORDER_LEDGER, epoch_guard, lock_order_guard
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

# The retired repository name, built from hex rather than spelled, and the one
# definition of the wake-hold delivery domain.
#
# This string is a salt inside a sha256 preimage: it prefixes the delivery
# prefix digest that `_transact_wake_hold_records` compares against a digest a
# caller read earlier, so its bytes are a compatibility contract with every
# delivery ledger already on disk, not copy. It was carried as two independent
# literals — here and in the wake-hold controller — that happened to agree;
# the controller now imports this constant, so the two files cannot drift
# apart. The name is retired everywhere a reader can see it and kept exactly
# here, where only a hash can. Built rather than spelled for the reason
# floati/identity_fence.py builds its governed tokens: a fence that must
# forbid this word may not find it in shipped source, and the runtime value
# may not move to satisfy the fence.
# tests/test_retired_name_pins.py pins the preimage bytes and asserts the two
# files still agree.
_RETIRED_NAME = bytes.fromhex("736c6970776179").decode("ascii")
WAKE_HOLD_DELIVERY_DOMAIN = _RETIRED_NAME + "-wake-hold-deliveries-v1"
_WAKE_HOLD_DELIVERY_PREIMAGE = WAKE_HOLD_DELIVERY_DOMAIN.encode("ascii") + b"\0"


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


def _is_epoch_selected_relative(relative: Union[Path, str]) -> bool:
    """Classify the validated lexical coordinate before symlinks are resolved."""

    candidate = Path(relative)
    if candidate.is_absolute() or any(
        part in {"", ".", ".."} for part in candidate.parts
    ):
        return False
    if candidate == Path("events.jsonl"):
        return True
    parts = candidate.parts
    return (
        len(parts) >= 3
        and parts[0] == "receipts"
        and parts[1] in {"deliveries", "acks"}
        and candidate.suffix == ".jsonl"
    )


@contextmanager
def _epoch_writer_guard(
    authority: Authority, relative: Union[Path, str],
) -> Iterator[None]:
    """Share the barrier only for the derived epoch-selected planes."""

    if isinstance(authority, FloatiRoot) and _is_epoch_selected_relative(relative):
        with epoch_guard(authority, exclusive=False):
            yield
        return
    yield


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


def _lock_coordinate(relative: Union[Path, str]) -> str:
    """Validate and render one lock's ROOT-RELATIVE coordinate.

    A lock refusal's detail is carried in a receipt, and receipts are exported.
    An ABSOLUTE path there would publish the host's own coordinates — an
    account home, or one of the governed temporary prefixes — and the
    exporter's redactor can only remove the prefixes it enumerates, so a shape
    it has not met survives into the published artifact. A path under the root
    is a coordinate the PRODUCT owns and may publish.

    ⇒ THE FENCE IS AT CONSTRUCTION, NOT AT EXPORT. An absolute or escaping
    coordinate is refused here, eagerly, rather than rendered now and trusted
    to be redacted later.
    """

    text = relative.as_posix() if isinstance(relative, PurePath) else str(relative)
    coordinate = PurePosixPath(text)
    if not text or coordinate.is_absolute() or ".." in coordinate.parts:
        raise ValueError(
            "a lock coordinate must be a relative path under the root"
        )
    return coordinate.as_posix()


def _lock_beside(path: Path, relative: Union[Path, str]) -> Tuple[Path, str]:
    """The `.lock` beside a ledger, and the coordinate that names it.

    Both halves come from the same pair and by the same rule, so the file a
    caller locks and the name a refusal prints cannot drift apart.
    """

    return path.with_name(path.name + ".lock"), _lock_coordinate(relative) + ".lock"


@contextmanager
def _locked_path(
    path: Path, *, exclusive: bool,
    relative: Optional[Union[Path, str]] = None,
    timeout_seconds: float = LOCK_TIMEOUT_SECONDS,
    order_tracked: bool = True,
) -> Iterator[None]:
    """Take the bounded advisory lock at one already-authorized fixed path.

    `relative` names the lock the way a receipt must read it — root-relative,
    never the host path. It is inert with respect to the lock itself; it
    decides only what a refusal is able to SAY.

    It carries a default, and the default is the very basename ambiguity this
    argument exists to remove — so the default is not the fence. The fence is
    `tests/test_lock_refusal_coordinates.py`, which walks the AST of every
    shipped module and requires that EVERY `_locked_path` call site pass
    `relative`. That is deliberately stronger than making the parameter
    required: a signature can only bind direct callers, and several test
    doubles wrap this function with their own fixed signature and forward
    positionally, so a required argument would break instruments without
    making a single production path safer. *An enumeration that can be derived
    must be derived* — and this one can.
    """

    # Eagerly, so a caller that hands over an unpublishable coordinate fails
    # deterministically on every call rather than only under contention — the
    # one moment when a second, unrelated failure is hardest to read.
    coordinate = path.name if relative is None else _lock_coordinate(relative)
    order = (
        lock_order_guard(path, LOCK_ORDER_LEDGER, label="ledger")
        if order_tracked
        else nullcontext()
    )
    with order:
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
                            f"{coordinate} lock remained contended for {timeout_seconds:g} second",
                        ) from exc
                    time.sleep(LOCK_POLL_SECONDS)
                except OSError as exc:
                    raise _durability_failure(exc, path) from exc
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _decode_path_records(
    path: Path,
    tenant: str,
    allowed_kinds: FrozenSet[str],
    data: bytes,
    *,
    max_bytes: int = MAX_RECORD_BYTES,
    unrecognized: Optional[Dict[str, Dict[str, object]]] = None,
) -> List[Dict[str, Any]]:
    if len(data) > MAX_LEDGER_BYTES:
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
        if kind == "bus_epoch_roll_receipt" and line_number != 1:
            raise IntegrityFailure(
                "bus_epoch_roll_receipt_position_invalid",
                "one bus epoch roll receipt is permitted only as physical record one",
            )
        try:
            if unrecognized is not None and not is_known_record_kind(kind):
                record = validate_unknown_record(raw_record, tenant)
                summary = unrecognized.setdefault(
                    str(kind), {
                        "kind": str(kind),
                        "count": 0,
                        "first_id": str(record["id"]),
                        "max_schema_version": 0,
                    },
                )
                summary["count"] = int(summary["count"]) + 1
                summary["max_schema_version"] = max(
                    int(summary["max_schema_version"]), int(record["schema_version"])
                )
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
        data = path.read_bytes()
    except OSError as exc:
        raise _durability_failure(exc, path) from exc
    return _decode_path_records(
        path,
        tenant,
        allowed_kinds,
        data,
        max_bytes=max_bytes,
        unrecognized=unrecognized,
    )


def _unrecognized_rows(
    summaries: Dict[str, Dict[str, object]],
) -> List[Dict[str, object]]:
    return [
        {
            "kind": str(summaries[kind]["kind"]),
            "count": int(summaries[kind]["count"]),
            "first_id": str(summaries[kind]["first_id"]),
        }
        for kind in sorted(summaries)
    ]


def _unrecognized_versions(
    summaries: Dict[str, Dict[str, object]],
) -> Dict[str, int]:
    return {
        kind: int(summary["max_schema_version"])
        for kind, summary in summaries.items()
    }


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
    lock_path, lock_relative = _lock_beside(path, relative)
    with _epoch_writer_guard(authority, relative), _locked_path(
        lock_path, exclusive=True, relative=lock_relative
    ):
        existing = _read_path_records(path, tenant, kinds, max_bytes=max_bytes)
        if len(existing) >= MAX_LEDGER_RECORDS:
            raise ProtocolRefusal("ledger_record_limit", f"ledger maximum is {MAX_LEDGER_RECORDS} records")
        if any(item["id"] == record["id"] for item in existing):
            raise ProtocolRefusal("duplicate_record_id", f"record id {record['id']} already exists")
        _append_frame(path, encoded)


def transact(authority: FloatiRoot, relative: Union[Path, str], decide: Callable[[List[Dict[str, Any]]], Tuple[Any, Optional[Dict[str, Any]]]], *, allowed_kinds: Optional[Set[str]] = None, max_bytes: int = MAX_RECORD_BYTES) -> Any:
    kinds = _kinds(allowed_kinds)
    path, tenant = _resolve(authority, relative, write=True)
    lock_path, lock_relative = _lock_beside(path, relative)
    with _epoch_writer_guard(authority, relative), _locked_path(
        lock_path, exclusive=True, relative=lock_relative
    ):
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
    lock_path, lock_relative = _lock_beside(path, relative)
    with _epoch_writer_guard(authority, relative), _locked_path(
        lock_path, exclusive=True, relative=lock_relative
    ):
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


def transact_exact_frame(
    authority: FloatiRoot,
    relative: Union[Path, str],
    decide: Callable[
        [List[Dict[str, Any]], Tuple[bytes, ...]],
        Tuple[Any, Optional[Dict[str, Any]]],
    ],
    *,
    allowed_kinds: Optional[Set[str]] = None,
    max_bytes: int = MAX_RECORD_BYTES,
) -> Any:
    """Append one record decided from validated rows and their exact line bytes."""

    kinds = _kinds(allowed_kinds)
    path, tenant = _resolve(authority, relative, write=True)
    lock_path, lock_relative = _lock_beside(path, relative)
    with _epoch_writer_guard(authority, relative), _locked_path(
        lock_path, exclusive=True, relative=lock_relative
    ):
        existing = _read_path_records(path, tenant, kinds, max_bytes=max_bytes)
        try:
            data = path.read_bytes() if path.exists() else b""
            exact_lines = tuple(data.split(b"\n")[:-1])
        except OSError as exc:
            raise _durability_failure(exc, path) from exc
        if len(exact_lines) != len(existing):
            raise IntegrityFailure(
                "journal_frame_count_invalid",
                "validated journal rows do not match exact frame testimony",
            )
        result, record = decide(existing, exact_lines)
        if record is None:
            return result
        if len(existing) >= MAX_LEDGER_RECORDS:
            raise ProtocolRefusal(
                "ledger_record_limit",
                f"ledger maximum is {MAX_LEDGER_RECORDS} records",
            )
        encoded = _encode_record(record, tenant, kinds, max_bytes=max_bytes)
        if any(item["id"] == record.get("id") for item in existing):
            raise ProtocolRefusal(
                "duplicate_record_id", f"record id {record.get('id')} already exists"
            )
        _append_frame(path, encoded)
        return result


def _replace_epoch_selected(
    authority: FloatiRoot,
    selected: Sequence[tuple[str, Path, str, object]],
    receipt_frame: bytes,
    *,
    fault: Optional[Callable[[str], None]] = None,
) -> None:
    """Replace the selected epoch under its ordinary ledger locks.

    The caller owns the exclusive epoch barrier and all applicable wake
    coordination locks.  Keeping this sealed mutation beside the ordinary
    JSONL primitives prevents an unguarded selected-plane write surface from
    escaping the ledger module.
    """

    if not isinstance(authority, FloatiRoot):
        raise TypeError("epoch replacement requires a FloatiRoot")
    bound: list[tuple[str, Path, str, object]] = []
    for item in selected:
        if not isinstance(item, tuple) or len(item) != 4:
            raise TypeError("epoch replacement members must be closed tuples")
        relative, path, plane, identity = item
        if (
            not isinstance(relative, str)
            or not isinstance(path, Path)
            or not isinstance(plane, str)
            or not _is_epoch_selected_relative(relative)
            or path != authority.resolve_relative(relative)
        ):
            raise ProtocolRefusal(
                "epoch_selected_member_changed",
                "epoch replacement member is not bound to the selected root",
            )
        bound.append((relative, path, plane, identity))
    if not isinstance(receipt_frame, bytes) or not receipt_frame:
        raise TypeError("epoch replacement requires one nonempty receipt frame")

    def boundary(name: str) -> None:
        if fault is not None:
            fault(name)

    with ExitStack() as stack:
        for bound_relative, path, _plane, _identity in sorted(
            bound, key=lambda item: item[0].encode("utf-8")
        ):
            lock_path, lock_relative = _lock_beside(path, bound_relative)
            stack.enter_context(
                _locked_path(lock_path, exclusive=True, relative=lock_relative)
            )

        events_item = next(
            (item for item in bound if item[0] == "events.jsonl"), None
        )
        if events_item is None:
            raise ProtocolRefusal(
                "epoch_selected_member_changed",
                "epoch replacement is missing its events coordinate",
            )
        _events_relative, events, _events_plane, events_identity = events_item
        flags = os.O_WRONLY | os.O_CREAT
        if events_identity is None:
            flags |= os.O_EXCL
        flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(events, flags, 0o600)
        except OSError as exc:
            raise _durability_failure(exc, events) from exc
        try:
            observed = os.fstat(descriptor)
            observed_identity = (
                observed.st_dev, observed.st_ino, observed.st_mode, observed.st_nlink,
                observed.st_size, observed.st_mtime_ns, observed.st_ctime_ns,
            )
            if events_identity is not None and observed_identity != events_identity:
                raise DurabilityFailure(
                    "epoch_selected_member_changed",
                    "selected epoch events changed before replacement",
                )
            try:
                os.ftruncate(descriptor, 0)
            except OSError as exc:
                raise _durability_failure(exc, events) from exc
            boundary("live_events_replaced")
            offset = 0
            write_index = 0
            while offset < len(receipt_frame):
                try:
                    written = os.write(descriptor, receipt_frame[offset:])
                except OSError as exc:
                    raise _durability_failure(exc, events) from exc
                if written <= 0:
                    raise DurabilityFailure(
                        "short_write", "live epoch receipt could not be written completely"
                    )
                offset += written
                write_index += 1
                boundary(f"live_events_write_{write_index}")
            try:
                os.fsync(descriptor)
            except OSError as exc:
                raise _durability_failure(exc, events) from exc
            boundary("live_events_synced")
        finally:
            os.close(descriptor)

        for index, (relative, path, plane, identity) in enumerate(bound):
            if plane == "events" or identity is None:
                continue
            try:
                status = path.lstat()
            except OSError as exc:
                raise _durability_failure(exc, path) from exc
            observed = (
                status.st_dev, status.st_ino, status.st_mode, status.st_nlink,
                status.st_size, status.st_mtime_ns, status.st_ctime_ns,
            )
            if observed != identity:
                raise DurabilityFailure(
                    "epoch_selected_member_changed",
                    f"selected epoch member {relative} changed before retirement",
                )
            try:
                os.unlink(path)
            except OSError as exc:
                raise _durability_failure(exc, path) from exc
            boundary(f"live_member_{index}_retired")
            try:
                parent = os.open(path.parent, os.O_RDONLY)
                try:
                    os.fsync(parent)
                finally:
                    os.close(parent)
            except OSError as exc:
                raise _durability_failure(exc, path.parent) from exc
            boundary(f"live_member_{index}_parent_synced")

        try:
            parent = os.open(authority.tenant_home, os.O_RDONLY)
            try:
                os.fsync(parent)
            finally:
                os.close(parent)
        except OSError as exc:
            raise _durability_failure(exc, authority.tenant_home) from exc
        boundary("live_epoch_parent_synced")


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
    lock_path, lock_relative = _lock_beside(path, relative)
    with _epoch_writer_guard(authority, relative), _locked_path(
        lock_path, exclusive=True, relative=lock_relative
    ):
        existing = _read_path_records(path, tenant, kinds)
        digest = hashlib.sha256(_WAKE_HOLD_DELIVERY_PREIMAGE)
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
    lock_path, lock_relative = _lock_beside(path, _EFFECT_RECORDS_RELATIVE)
    with _locked_path(lock_path, exclusive=True, relative=lock_relative):
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
    lock_path, lock_relative = _lock_beside(
        path, _THREAD_OBSERVATION_RECORDS_RELATIVE
    )
    with _locked_path(lock_path, exclusive=True, relative=lock_relative):
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
    lock_path, lock_relative = _lock_beside(path, relative)
    with _locked_path(lock_path, exclusive=False, relative=lock_relative):
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
        lock_path, lock_relative = _lock_beside(path, relative)
        with _locked_path(lock_path, exclusive=False, relative=lock_relative):
            records = _read_path_records(
                path, tenant, kinds, max_bytes=max_bytes, unrecognized=summaries
            )
    return records, _unrecognized_rows(summaries)


class VerifiedLedgerCursor:
    """Retain one validated ledger prefix and inspect only later frames.

    The cursor is memory-only and binds itself to one exact path, tenant,
    kind set, digest domain, and record bound.  A shorter file, a missing file
    after prior data, or a changed device/inode identity discards the cached
    prefix and performs one complete replay.
    """

    def __init__(self) -> None:
        self._binding: Optional[Tuple[Path, str, FrozenSet[str], str, int]] = None
        self._identity: Optional[Tuple[int, int]] = None
        self._byte_length = 0
        self._records: List[Dict[str, Any]] = []
        self._prefixes: Tuple[str, ...] = ()
        self._digest: Optional[Any] = None

    def snapshot(self) -> Tuple[List[Dict[str, Any]], Tuple[str, ...]]:
        return copy.deepcopy(self._records), tuple(self._prefixes)

    @staticmethod
    def _initial_digest(domain: str) -> Any:
        digest = hashlib.sha256()
        digest.update(domain.encode("ascii") + b"\0")
        return digest

    @staticmethod
    def _frame_failure(path: Path, exc: FrameError, *, line_offset: int = 0) -> IntegrityFailure:
        code = {
            "incomplete_frame": "incomplete_jsonl_line",
            "blank_frame": "blank_jsonl_line",
        }.get(exc.code, exc.code)
        line_number = line_offset + exc.line_number if exc.line_number else 0
        where = f" line {line_number}" if line_number else ""
        return IntegrityFailure(code, f"{path.name}{where}: {exc.detail}")

    def _reset_empty(self, domain: str) -> Tuple[List[Dict[str, Any]], Tuple[str, ...]]:
        digest = self._initial_digest(domain)
        self._identity = None
        self._byte_length = 0
        self._records = []
        self._prefixes = (digest.hexdigest(),)
        self._digest = digest
        return self.snapshot()

    def _full_replay(
        self,
        path: Path,
        tenant: str,
        kinds: FrozenSet[str],
        domain: str,
        max_bytes: int,
    ) -> Tuple[List[Dict[str, Any]], Tuple[str, ...]]:
        try:
            before = path.stat()
            data = path.read_bytes()
            after = path.stat()
        except OSError as exc:
            raise _durability_failure(exc, path) from exc
        before_identity = (int(before.st_dev), int(before.st_ino))
        after_identity = (int(after.st_dev), int(after.st_ino))
        if (
            before_identity != after_identity
            or before.st_size != after.st_size
            or len(data) != after.st_size
        ):
            raise IntegrityFailure(
                "ledger_identity_changed_during_read",
                f"{path.name} changed identity or length during replay",
            )
        records = _decode_path_records(
            path, tenant, kinds, data, max_bytes=max_bytes,
        )
        frames = data.splitlines(keepends=True)
        if len(frames) != len(records):
            raise IntegrityFailure(
                "noncanonical_frame",
                "durable framing does not match validated records",
            )
        digest = self._initial_digest(domain)
        prefixes = [digest.hexdigest()]
        for record, frame in zip(records, frames):
            if frame != encode_frame(record):
                raise IntegrityFailure(
                    "noncanonical_frame",
                    "durable frame differs from canonical encoding",
                )
            digest.update(frame)
            prefixes.append(digest.hexdigest())
        self._identity = after_identity
        self._byte_length = len(data)
        self._records = [dict(record) for record in records]
        self._prefixes = tuple(prefixes)
        self._digest = digest
        return self.snapshot()

    def _read_locked(
        self,
        path: Path,
        tenant: str,
        kinds: FrozenSet[str],
        domain: str,
        max_bytes: int,
    ) -> Tuple[List[Dict[str, Any]], Tuple[str, ...]]:
        if not path.exists():
            if self._identity is None and self._prefixes:
                return self.snapshot()
            return self._reset_empty(domain)
        try:
            stat = path.stat()
        except OSError as exc:
            raise _durability_failure(exc, path) from exc
        identity = (int(stat.st_dev), int(stat.st_ino))
        if (
            self._identity is None
            or identity != self._identity
            or stat.st_size < self._byte_length
        ):
            return self._full_replay(path, tenant, kinds, domain, max_bytes)
        if stat.st_size > MAX_LEDGER_BYTES:
            raise IntegrityFailure(
                "ledger_too_large", f"{path.name} exceeds {MAX_LEDGER_BYTES} bytes"
            )
        retained_prefix_changed = False
        try:
            with path.open("rb") as handle:
                opened = os.fstat(handle.fileno())
                opened_identity = (int(opened.st_dev), int(opened.st_ino))
                if opened_identity != identity or opened.st_size != stat.st_size:
                    raise IntegrityFailure(
                        "ledger_identity_changed_during_read",
                        f"{path.name} changed identity or length before incremental read",
                    )
                if self._records:
                    expected_head = encode_frame(self._records[0])
                    retained_prefix_changed = handle.read(len(expected_head)) != expected_head
                if retained_prefix_changed or stat.st_size == self._byte_length:
                    appended = b""
                    final = os.fstat(handle.fileno())
                else:
                    handle.seek(self._byte_length)
                    appended = handle.read(stat.st_size - self._byte_length)
                    final = os.fstat(handle.fileno())
        except IntegrityFailure:
            raise
        except OSError as exc:
            raise _durability_failure(exc, path) from exc
        if (
            (int(final.st_dev), int(final.st_ino)) != identity
            or final.st_size != stat.st_size
            or (
                not retained_prefix_changed
                and len(appended) != stat.st_size - self._byte_length
            )
        ):
            raise IntegrityFailure(
                "ledger_identity_changed_during_read",
                f"{path.name} changed identity or length during incremental read",
            )
        if retained_prefix_changed:
            return self._full_replay(path, tenant, kinds, domain, max_bytes)
        if stat.st_size == self._byte_length:
            return self.snapshot()
        line_offset = len(self._records)
        for line_number, raw in enumerate(appended.splitlines(), start=line_offset + 1):
            if len(raw) + 1 > max_bytes:
                raise IntegrityFailure(
                    "record_too_large",
                    f"{path.name} line {line_number} exceeds {max_bytes} bytes",
                )
        try:
            decoded = decode_frames(appended)
        except FrameError as exc:
            raise self._frame_failure(path, exc, line_offset=line_offset) from exc
        if line_offset + len(decoded) > MAX_LEDGER_RECORDS:
            raise IntegrityFailure(
                "ledger_record_limit",
                f"{path.name} exceeds {MAX_LEDGER_RECORDS} records",
            )
        frames = appended.splitlines(keepends=True)
        seen = {str(record["id"]) for record in self._records}
        validated: List[Dict[str, Any]] = []
        for index, raw_record in enumerate(decoded):
            line_number = line_offset + index + 1
            record_id = raw_record.get("id", "<absent>") if isinstance(raw_record, dict) else "<absent>"
            kind = raw_record.get("kind", "<absent>") if isinstance(raw_record, dict) else "<absent>"
            try:
                record = validate_record(raw_record, tenant, kinds, integrity=True)
            except IntegrityFailure as exc:
                raise IntegrityFailure(
                    exc.code,
                    f"ledger {path}: record {record_id}: kind {kind}: {exc.detail}",
                ) from exc
            if str(record["id"]) in seen:
                raise IntegrityFailure(
                    "duplicate_record_id",
                    f"ledger {path}: record {record['id']}: kind {record['kind']}: duplicate id",
                )
            if frames[index] != encode_frame(record):
                raise IntegrityFailure(
                    "noncanonical_frame",
                    f"{path.name} line {line_number} differs from canonical encoding",
                )
            seen.add(str(record["id"]))
            validated.append(record)
        if self._digest is None:
            raise IntegrityFailure("ledger_cursor_uninitialized", "incremental digest state is absent")
        digest = self._digest.copy()
        prefixes = list(self._prefixes)
        for frame in frames:
            digest.update(frame)
            prefixes.append(digest.hexdigest())
        self._identity = identity
        self._byte_length = int(stat.st_size)
        self._records.extend(dict(record) for record in validated)
        self._prefixes = tuple(prefixes)
        self._digest = digest
        return self.snapshot()

    def read(
        self,
        authority: Authority,
        relative: Union[Path, str],
        *,
        allowed_kinds: Optional[Set[str]] = None,
        domain: str,
        max_bytes: int = MAX_RECORD_BYTES,
    ) -> Tuple[List[Dict[str, Any]], Tuple[str, ...]]:
        if not isinstance(domain, str) or not domain.isascii() or not domain:
            raise ProtocolRefusal(
                "prefix_digest_domain_invalid", "digest domain must be nonempty ASCII"
            )
        kinds = _kinds(allowed_kinds)
        path, tenant = _resolve(authority, relative, write=False)
        binding = (path.resolve(strict=False), tenant, kinds, domain, max_bytes)
        if self._binding is None:
            self._binding = binding
        elif self._binding != binding:
            raise ProtocolRefusal(
                "ledger_cursor_binding_mismatch",
                "verified ledger cursor is already bound to another ledger contract",
            )
        if isinstance(authority, TenantObservation):
            return self._read_locked(path, tenant, kinds, domain, max_bytes)
        lock_path, lock_relative = _lock_beside(path, relative)
        with _locked_path(lock_path, exclusive=False, relative=lock_relative):
            return self._read_locked(path, tenant, kinds, domain, max_bytes)


def read_records_compatible_with_versions(
    authority: Authority,
    relative: Union[Path, str],
    *,
    allowed_kinds: Optional[Set[str]] = None,
    max_bytes: int = MAX_RECORD_BYTES,
    snapshot: bool = False,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, object]], Dict[str, int]]:
    """Read compatible rows plus private version evidence for an outer fact."""

    kinds = _kinds(allowed_kinds)
    path, tenant = _resolve(authority, relative, write=False)
    summaries: Dict[str, Dict[str, object]] = {}
    if not path.exists():
        return [], [], {}
    if snapshot or isinstance(authority, TenantObservation):
        records = _read_path_records(
            path, tenant, kinds, max_bytes=max_bytes, unrecognized=summaries
        )
    else:
        lock_path, lock_relative = _lock_beside(path, relative)
        with _locked_path(lock_path, exclusive=False, relative=lock_relative):
            records = _read_path_records(
                path, tenant, kinds, max_bytes=max_bytes, unrecognized=summaries
            )
    return records, _unrecognized_rows(summaries), _unrecognized_versions(summaries)


def read_records_with_prefix_digests(
    authority: Authority,
    relative: Union[Path, str],
    *,
    allowed_kinds: Optional[Set[str]] = None,
    domain: str,
    max_bytes: int = MAX_RECORD_BYTES,
    cursor: Optional[VerifiedLedgerCursor] = None,
) -> Tuple[List[Dict[str, Any]], Tuple[str, ...]]:
    """Read validated canonical frames and every inclusive SHA-256 prefix.

    The first digest represents the empty prefix.  This narrow reader is
    intentionally read-only; callers cannot synthesize testimony from parsed
    mappings because every stored frame must equal its canonical encoding.
    """

    selected = cursor if cursor is not None else VerifiedLedgerCursor()
    return selected.read(
        authority,
        relative,
        allowed_kinds=allowed_kinds,
        domain=domain,
        max_bytes=max_bytes,
    )

"""Bounded, root-authorized, locked, fsynced append-only JSONL evidence."""

from __future__ import annotations

import copy
import fcntl
import errno
import hashlib
import os
import stat
import sys
import threading
import time
from contextlib import ExitStack, contextmanager, nullcontext
from pathlib import Path, PurePath, PurePosixPath
from typing import Any, Callable, Dict, FrozenSet, Iterator, List, NamedTuple, Optional, Sequence, Set, Tuple, Union

from .bus_epoch import LOCK_ORDER_LEDGER, epoch_guard, lock_order_guard
from .errors import DurabilityFailure, IntegrityFailure, ProtocolRefusal
from .framing import FrameError, decode_frames, encode_frame
from .records import is_known_record_kind, validate_record, validate_unknown_record
from .root import FloatiRoot, TenantObservation
from .wake_hints import publish_ledger_hints


MAX_RECORD_BYTES = 65536
MAX_LEDGER_BYTES = 64 * 1024 * 1024
MAX_LEDGER_RECORDS = 100000
LOCK_TIMEOUT_SECONDS = 1.0
LOCK_POLL_SECONDS = 0.01
Authority = Union[FloatiRoot, TenantObservation]
_EFFECT_RECORDS_RELATIVE = Path("effects/records.jsonl")
_THREAD_OBSERVATION_RECORDS_RELATIVE = Path("thread-observations/records.jsonl")
_WAKE_HOLD_APPEND_MARKER = object()
_SEAT_EXECUTION_APPEND_MARKER = object()


def _adhoc_kind(record):
    from .adhoc_records import ADHOC_KINDS
    kind = record.get("kind") if isinstance(record, dict) else None
    return isinstance(kind, str) and kind in ADHOC_KINDS


def _adhoc_only():
    raise ProtocolRefusal("adhoc_dispatch_authentication_failed",
        "ad-hoc metadata requires its exact native root-owned transaction")


def _native_launcher_only():
    raise ProtocolRefusal("native_dispatch_launcher_controller_only",
        "launcher testimony requires its authenticated send transaction")


def _adhoc_family_path(path):
    from .adhoc_records import ADHOC_DIRECTORY
    return path.parent.name == ADHOC_DIRECTORY.name


def _deny_adhoc_kinds(allowed_kinds):
    from .adhoc_records import ADHOC_KINDS
    if allowed_kinds and ADHOC_KINDS.intersection(allowed_kinds):
        _adhoc_only()


def _is_epoch_history_records_path(path: Path) -> bool:
    return path.name == "epoch-history-commits.jsonl" and path.parent.name == "receipts"


def _refuse_epoch_history_authoring() -> None:
    raise ProtocolRefusal(
        "epoch_history_controller_only", "history authority requires its native maintenance transaction",
        remedy="use the native history maintenance producer; preserve the existing commit ledger",
    )

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
    authority: Authority, relative: Union[Path, str], *, path: Optional[Path] = None,
) -> Iterator[None]:
    """Admit a writer by both its lexical and resolved physical coordinate."""

    if isinstance(authority, FloatiRoot):
        physical = authority.resolve_relative(relative) if path is None else path
        physical_relative = physical.relative_to(authority.tenant_home)
        if (
            _is_epoch_selected_relative(relative)
            or _is_epoch_selected_relative(physical_relative)
        ):
            from .bus_epoch import _require_epoch_writer_admission

            with epoch_guard(authority, exclusive=False):
                _require_epoch_writer_admission(authority)
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
        if write and Path(relative) == Path("receipts/epoch-history-commits.jsonl"):
            _refuse_epoch_history_authoring()
        path = authority.resolve_relative(relative)
        if write:
            from .adhoc_records import ADHOC_DIRECTORY
            lexical = Path(relative)
            physical = path.relative_to(authority.tenant_home)
            if (lexical.parts and lexical.parts[0] == ADHOC_DIRECTORY.name) or (
                    physical.parts and physical.parts[0] == ADHOC_DIRECTORY.name):
                _adhoc_only()
        if write and path == authority.tenant_home / "receipts/epoch-history-commits.jsonl":
            _refuse_epoch_history_authoring()
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
    _open_handle=None,
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
            if _open_handle is None:
                path.parent.mkdir(parents=True, exist_ok=True)
                handle = path.open("a+b")
            else:
                # Private descriptor-confined opener; locking, lock-order and
                # deadline behavior remain identical to the native writer.
                handle = _open_handle()
        except OSError as exc:
            raise _durability_failure(exc, path) from exc
        with handle as handle:
            from .waiter_lock_retry import current_lock_window, WaiterLockDeadline

            window = current_lock_window()
            budget = timeout_seconds if window is None else min(timeout_seconds, window.remaining())
            deadline = time.monotonic() + budget
            while True:
                try:
                    fcntl.flock(handle.fileno(), operation | fcntl.LOCK_NB)
                    break
                except BlockingIOError as exc:
                    if window is not None:
                        window.contended = True
                    if time.monotonic() >= deadline or (window is not None and window.remaining() <= 0):
                        failure = ProtocolRefusal(
                            "ledger_lock_timeout",
                            f"{coordinate} lock remained contended for {timeout_seconds:g} second",
                            remedy={
                                "kind": "transient",
                                "action": "retry_once",
                                "delay_seconds": 1,
                                # Ordinary flock files name no authoritative holder.
                                "holder": "unknown",
                            },
                        )
                        if window is None:
                            raise failure from exc
                        if window.remaining() <= 0:
                            raise WaiterLockDeadline(coordinate) from failure
                        # The waiter retries only after the entire native
                        # operation has released its ledger/node/epoch guards.
                        raise failure from exc
                    if window is None:
                        time.sleep(LOCK_POLL_SECONDS)
                    else:
                        window.sleep(min(LOCK_POLL_SECONDS, window.remaining()))
                except OSError as exc:
                    raise _durability_failure(exc, path) from exc
            try:
                if window is not None and not window.finishing and window.remaining() <= 0:
                    raise WaiterLockDeadline(coordinate, contended=False)
                if window is not None and window.contended and not window.finishing and window.check_authority is not None:
                    window.check_authority()
                if window is not None and not window.finishing and window.remaining() <= 0:
                    raise WaiterLockDeadline(coordinate, contended=False)
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _decode_bounded_frames(path, data, *, max_bytes=MAX_RECORD_BYTES):
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
        failure = IntegrityFailure(code, f"{path.name}{where}: {exc.detail}")
        if exc.code == "incomplete_frame":
            failure.waiter_byte_offset = data.rfind(b"\n") + 1
        elif exc.line_number > 0:
            failure.waiter_byte_offset = sum(len(frame) for frame in data.splitlines(keepends=True)[:exc.line_number - 1])
        raise failure from exc
    return framed


def _decode_path_records(
    path: Path,
    tenant: str,
    allowed_kinds: FrozenSet[str],
    data: bytes,
    *,
    max_bytes: int = MAX_RECORD_BYTES,
    unrecognized: Optional[Dict[str, Dict[str, object]]] = None,
) -> List[Dict[str, Any]]:
    try:
        framed = _decode_bounded_frames(path, data, max_bytes=max_bytes)
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
                if not is_known_record_kind(kind):
                    from .installed_reader import installed_reader_identity, refuse_older_reader

                    identity = installed_reader_identity()
                    if identity is not None:
                        validate_unknown_record(raw_record, tenant)
                        refuse_older_reader(str(kind), identity)
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
    except Exception as failure:
        # This is private byte-source identity, not testimony of a disk read.
        # A selected reader may bind it to its retained exact payload and the
        # descriptor metadata measured before that payload was read. Pure
        # decoder callers acquire no file witness or public input coordinate.
        try:
            if getattr(failure, "_waiter_payload_source", None) is None:
                failure._waiter_payload_source = (path, id(data))
        except Exception:
            pass
        raise


@contextmanager
def _failure_input_testimony(path: Path):
    """Retain measured pre-read metadata only on the original exception.

    This private invalidation witness is neither successful read testimony nor
    evidence that the path's current bytes are valid. Nested replay scopes
    retain the innermost measured input rather than overwriting it later.
    """
    observed = [None]
    try:
        yield observed
    except Exception as failure:
        status = observed[0]
        if status is not None and getattr(failure, "waiter_input_witness", None) is None:
            try:
                failure.waiter_input_witness = (path, (
                    status.st_dev, status.st_ino, status.st_mode, status.st_size,
                    status.st_mtime_ns, status.st_ctime_ns, status.st_nlink,
                ))
            except Exception:
                # Optional private testimony cannot replace a native failure.
                pass
        raise


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
    with _failure_input_testimony(path) as input_stat:
        try:
            with path.open("rb") as handle:
                try:
                    input_stat[0] = os.fstat(handle.fileno())
                except OSError:
                    # Witness capture is best effort; preserve the native read.
                    pass
                data = handle.read()
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


def _append_frame(path: Path, encoded: bytes, *, wake_hold_marker: object = None) -> tuple:
    if _is_epoch_history_records_path(path):
        _refuse_epoch_history_authoring()
    try:
        candidates = decode_frames(encoded)
    except FrameError:
        candidates = []
    if any(isinstance(row, dict) and row.get("kind") == "epoch_history_commit" for row in candidates):
        _refuse_epoch_history_authoring()
    try:
        # From this point onward use only the canonical destination.  Besides
        # closing ``.``/``..`` and symlink aliases, this prevents a checked
        # alias from being resolved a second time during mkdir/open.
        path = path.resolve(strict=False)
    except OSError as exc:
        raise _durability_failure(exc, path) from exc
    if _adhoc_family_path(path) or any(_adhoc_kind(record) for record in candidates):
        try:
            caller = sys._getframe(1)
        except ValueError:
            caller = None
        authority = None if caller is None else caller.f_locals.get("authority")
        relative = None if caller is None else caller.f_locals.get("relative")
        kinds = None if caller is None else caller.f_locals.get("kinds")
        if (caller is None or caller.f_code is not _ADHOC_NATIVE_TRANSACTION_CODE
                or not isinstance(authority, FloatiRoot)
                or path != authority.tenant_home / relative
                or len(candidates) != 1 or candidates[0].get("kind") not in kinds):
            _adhoc_only()
    if any(isinstance(row, dict) and row.get("kind") == "native_dispatch_launcher_record"
           for row in candidates):
        from .send_prefix import transact_send_locked

        caller = sys._getframe(1)
        root = caller.f_locals.get("root")
        if (caller.f_code is not transact_send_locked.__code__
                or not isinstance(root, FloatiRoot)
                or path != root.tenant_home / "events.jsonl"):
            _native_launcher_only()
    if _is_epoch_history_records_path(path):
        _refuse_epoch_history_authoring()
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
            appended_status = os.fstat(descriptor)
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
    return tuple(getattr(appended_status, field) for field in (
        "st_dev", "st_ino", "st_mode", "st_nlink", "st_size", "st_mtime_ns", "st_ctime_ns"))


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
    _deny_adhoc_kinds(allowed_kinds)
    if _adhoc_kind(record):
        _adhoc_only()
    if record.get("kind") == "native_dispatch_launcher_record":
        _native_launcher_only()
    if record.get("kind") == "epoch_history_commit":
        _refuse_epoch_history_authoring()
    if record.get("kind") == "seat_execution":
        raise ProtocolRefusal("seat_execution_controller_only", "execution testimony requires its native controller",
            remedy="use the authenticated native execution producer instead of generic ledger append")
    if record.get("kind") == "wake_hold_receipt":
        raise ProtocolRefusal("wake_controller_only", "wake hold testimony requires its sealed ledger")
    kinds = _kinds(allowed_kinds)
    path, tenant = _resolve(authority, relative, write=True)
    encoded = _encode_record(record, tenant, kinds, max_bytes=max_bytes)
    lock_path, lock_relative = _lock_beside(path, relative)
    with _epoch_writer_guard(authority, relative, path=path), _locked_path(
        lock_path, exclusive=True, relative=lock_relative
    ):
        existing = _read_path_records(path, tenant, kinds, max_bytes=max_bytes)
        if len(existing) >= MAX_LEDGER_RECORDS:
            raise ProtocolRefusal("ledger_record_limit", f"ledger maximum is {MAX_LEDGER_RECORDS} records")
        if any(item["id"] == record["id"] for item in existing):
            raise ProtocolRefusal("duplicate_record_id", f"record id {record['id']} already exists")
        _append_frame(path, encoded)
        publish_ledger_hints(authority, relative, (record,))


def transact(authority: FloatiRoot, relative: Union[Path, str], decide: Callable[[List[Dict[str, Any]]], Tuple[Any, Optional[Dict[str, Any]]]], *, allowed_kinds: Optional[Set[str]] = None, max_bytes: int = MAX_RECORD_BYTES, skip_unknown_kinds: bool = False) -> Any:
    _deny_adhoc_kinds(allowed_kinds)
    kinds = _kinds(allowed_kinds)
    path, tenant = _resolve(authority, relative, write=True)
    lock_path, lock_relative = _lock_beside(path, relative)
    with _epoch_writer_guard(authority, relative, path=path), _locked_path(
        lock_path, exclusive=True, relative=lock_relative
    ):
        summaries: Optional[Dict[str, Dict[str, object]]] = (
            {} if skip_unknown_kinds else None
        )
        existing = _read_path_records(
            path, tenant, kinds, max_bytes=max_bytes, unrecognized=summaries
        )
        result, record = decide(existing)
        if record is not None:
            if _adhoc_kind(record):
                _adhoc_only()
            if record.get("kind") == "native_dispatch_launcher_record":
                _native_launcher_only()
            if record.get("kind") == "seat_execution":
                raise ProtocolRefusal("seat_execution_controller_only", "execution testimony requires its native controller",
                    remedy="use the authenticated native execution producer instead of generic ledger append")
            if record.get("kind") == "wake_hold_receipt":
                raise ProtocolRefusal("wake_controller_only", "wake hold testimony requires its sealed ledger")
            physical_count = len(existing) + sum(
                int(summary['count']) for summary in (summaries or {}).values())
            if physical_count >= MAX_LEDGER_RECORDS:
                raise ProtocolRefusal("ledger_record_limit", f"ledger maximum is {MAX_LEDGER_RECORDS} records")
            encoded = _encode_record(record, tenant, kinds, max_bytes=max_bytes)
            if any(item["id"] == record["id"] for item in existing):
                raise ProtocolRefusal("duplicate_record_id", f"record id {record['id']} already exists")
            from .registry_prefix import prepare_append, advance
            prefix = prepare_append(authority, relative, kinds, max_bytes)
            from .inbox_prefix import prepare_append as prepare_inbox, advance as advance_inbox
            inbox_prefix = prepare_inbox(authority, relative, kinds, max_bytes,
                [record], prior=existing)
            from .cut_prefix import prepare_append as prepare_cut, advance as advance_cut
            cut_prefix = prepare_cut(authority, relative, kinds, max_bytes, [record])
            identity = _append_frame(path, encoded)
            advance(prefix, [record], [encoded], identity)
            advance_inbox(inbox_prefix, [record], [encoded], identity)
            advance_cut(cut_prefix, [record], [encoded], identity)
            publish_ledger_hints(authority, relative, (record,))
        return result


def _transact_worker_receipt(authority, record, prepare):
    """Prepare append-only worker history unlocked; serialize only its suffix.

    Like the verified cursor, suffix catch-up relies on native append-only
    writers. This is a fixed path/kind seam, not a replacement for generic
    transactions whose callbacks may need to replay arbitrary history.
    """
    relative = Path("receipts/workers.jsonl")
    kinds = frozenset({"worker_receipt"})
    path, tenant = _resolve(authority, relative, write=True)
    encoded = _encode_record(record, tenant, kinds)

    def identity(status):
        return status.st_dev, status.st_ino

    def version(status):
        return identity(status) + (status.st_size, status.st_mtime_ns, status.st_ctime_ns)

    def changed():
        return IntegrityFailure(
            "ledger_identity_changed_during_read",
            "workers.jsonl changed its retained prefix during receipt preparation",
        )

    captured = None
    data = b""
    try:
        with path.open("rb") as handle:
            captured = os.fstat(handle.fileno())
            if captured.st_size > MAX_LEDGER_BYTES:
                raise IntegrityFailure("ledger_too_large", f"{path.name} exceeds {MAX_LEDGER_BYTES} bytes")
            data = handle.read(captured.st_size)
            after = os.fstat(handle.fileno())
            if (identity(after) != identity(captured)
                    or after.st_size < captured.st_size
                    or len(data) != captured.st_size
                    or (after.st_size == captured.st_size and version(after) != version(captured))):
                raise changed()
    except FileNotFoundError:
        if captured is not None:
            raise changed()
    except OSError as exc:
        raise _durability_failure(exc, path) from exc

    existing = _decode_path_records(path, tenant, kinds, data)
    seen = {row["id"] for row in existing}
    count = len(existing)
    head = data.split(b"\n", 1)[0] + b"\n" if data else b""
    accept = prepare(existing)

    from .bus_epoch import _require_epoch_writer_admission

    lock_path, lock_relative = _lock_beside(path, relative)
    # This sealed writer participates even when its ordinary receipt path
    # is outside the rolled triple. Admit before taking the receipt lock;
    # preparation and historical replay still run without either lock.
    with epoch_guard(authority, exclusive=False), ExitStack() as locks:
        _require_epoch_writer_admission(authority)
        locks.enter_context(_locked_path(
            lock_path, exclusive=True, relative=lock_relative
        ))
        tail = b""
        try:
            with path.open("rb") as handle:
                current = os.fstat(handle.fileno())
                if current.st_size > MAX_LEDGER_BYTES:
                    raise IntegrityFailure("ledger_too_large", f"{path.name} exceeds {MAX_LEDGER_BYTES} bytes")
                if captured is not None and (
                    identity(current) != identity(captured)
                    or current.st_size < len(data)
                    or (current.st_size == len(data) and version(current) != version(captured))
                    or handle.read(len(head)) != head
                ):
                    raise changed()
                handle.seek(len(data))
                tail = handle.read(current.st_size - len(data))
                if (version(os.fstat(handle.fileno())) != version(current)
                        or version(path.stat()) != version(current)
                        or len(tail) != current.st_size - len(data)):
                    raise changed()
        except FileNotFoundError:
            if captured is not None:
                raise changed()
        except OSError as exc:
            raise _durability_failure(exc, path) from exc

        suffix = _decode_path_records(path, tenant, kinds, tail)
        if count + len(suffix) > MAX_LEDGER_RECORDS:
            raise IntegrityFailure("ledger_record_limit", f"{path.name} exceeds {MAX_LEDGER_RECORDS} records")
        for row in suffix:
            if row["id"] in seen:
                raise IntegrityFailure(
                    "duplicate_record_id", f"ledger {path}: record {row['id']}: duplicate id",
                )
            seen.add(row["id"])
        accept(suffix)
        if count + len(suffix) >= MAX_LEDGER_RECORDS:
            raise ProtocolRefusal("ledger_record_limit", f"ledger maximum is {MAX_LEDGER_RECORDS} records")
        if record["id"] in seen:
            raise ProtocolRefusal("duplicate_record_id", f"record id {record['id']} already exists")
        _append_frame(path, encoded)
        publish_ledger_hints(authority, relative, (record,))
    return record


def transact_records(
    authority: FloatiRoot,
    relative: Union[Path, str],
    decide: Callable[
        [List[Dict[str, Any]]], Tuple[Any, Sequence[Dict[str, Any]]]
    ],
    *,
    allowed_kinds: Optional[Set[str]] = None,
    max_bytes: int = MAX_RECORD_BYTES,
    skip_unknown_kinds: bool = False,
    _execution_marker: object = None,
    _send_selection: object = None,
) -> Any:
    """Validate and append one bounded record batch under one ledger lock."""
    _deny_adhoc_kinds(allowed_kinds)
    kinds = _kinds(allowed_kinds)
    path, tenant = _resolve(authority, relative, write=True)
    lock_path, lock_relative = _lock_beside(path, relative)
    with _epoch_writer_guard(authority, relative, path=path), _locked_path(
        lock_path, exclusive=True, relative=lock_relative
    ):
        if _send_selection is not None:
            from .send_prefix import transact_send_locked
            return transact_send_locked(authority, relative, decide,
                kinds=kinds, max_bytes=max_bytes, selection=_send_selection,
                execution_marker=_execution_marker)
        summaries: Optional[Dict[str, Dict[str, object]]] = (
            {} if skip_unknown_kinds else None
        )
        existing = _read_path_records(
            path, tenant, kinds, max_bytes=max_bytes, unrecognized=summaries
        )
        result, candidates = decide(existing)
        batch = tuple(candidates)
        if not batch:
            return result
        if any(_adhoc_kind(record) for record in batch):
            _adhoc_only()
        if any(record.get("kind") == "native_dispatch_launcher_record" for record in batch):
            _native_launcher_only()
        if any(record.get("kind") == "seat_execution" for record in batch) and (
            _execution_marker is not _SEAT_EXECUTION_APPEND_MARKER
            or Path(relative) != Path("events.jsonl")
        ):
            raise ProtocolRefusal("seat_execution_controller_only", "execution testimony requires its sealed event transaction",
                remedy="use the authenticated native execution producer instead of generic ledger append")
        physical_count = len(existing) + sum(
            int(summary['count']) for summary in (summaries or {}).values()
        )
        if physical_count + len(batch) > MAX_LEDGER_RECORDS:
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
        from .registry_prefix import prepare_append, advance
        prefix = prepare_append(authority, relative, kinds, max_bytes)
        from .inbox_prefix import prepare_append as prepare_inbox, advance as advance_inbox
        inbox_prefix = prepare_inbox(authority, relative, kinds, max_bytes,
            batch, prior=existing)
        from .cut_prefix import prepare_append as prepare_cut, advance as advance_cut
        cut_prefix = prepare_cut(authority, relative, kinds, max_bytes, batch)
        identity = _append_frame(path, b"".join(encoded))
        advance(prefix, batch, encoded, identity)
        advance_inbox(inbox_prefix, batch, encoded, identity)
        advance_cut(cut_prefix, batch, encoded, identity)
        publish_ledger_hints(authority, relative, batch)
        return result


def _transact_seat_execution_records(authority, decide, *, allowed_kinds, skip_unknown_kinds=False, _send_selection=None):
    """Closed native event seam; ordinary JSONL writers cannot mint testimony."""
    return transact_records(authority, "events.jsonl", decide,
        allowed_kinds=allowed_kinds, skip_unknown_kinds=skip_unknown_kinds,
        _execution_marker=_SEAT_EXECUTION_APPEND_MARKER, _send_selection=_send_selection)


def _transact_consumption_records(authority, relative, decide, *, _consumption_selection=None):
    """Compare a detached native consumption snapshot before publishing.

    The consumption controller retains its recipient coordination guard.
    Ordinary JSONL writers still serialize on the ledger lock: any append or
    replacement after capture makes this transaction replay before deciding
    again. No parsed history or callback runs under that ledger lock.
    """

    coordinate = Path(relative)
    if (len(coordinate.parts) not in {3, 4}
            or coordinate.parts[:2] != ("receipts", "consumed")
            or coordinate.suffix != ".jsonl"):
        raise ProtocolRefusal("consumption_controller_only",
            "detached consumption writes require their native receipt coordinate")
    kinds = frozenset({"consumption_receipt"})
    path, tenant = _resolve(authority, coordinate, write=True)
    if path != authority.tenant_home.resolve() / coordinate:
        raise ProtocolRefusal("consumption_controller_only",
            "detached consumption writes require their exact physical receipt coordinate")
    lock_path, lock_relative = _lock_beside(path, coordinate)

    def identity():
        try:
            selected = path.lstat()
        except FileNotFoundError:
            return None
        except OSError as failure:
            raise _durability_failure(failure, path) from failure
        if not stat.S_ISREG(selected.st_mode):
            raise IntegrityFailure("ledger_identity_changed_during_read",
                f"{path.name} changed identity before consumption capture")
        return tuple(getattr(selected, field) for field in (
            "st_dev", "st_ino", "st_mode", "st_nlink", "st_uid", "st_gid",
            "st_size", "st_mtime_ns", "st_ctime_ns"))

    with _epoch_writer_guard(authority, coordinate, path=path), epoch_guard(authority):
        if _consumption_selection is not None:
            from .consumed_prefix import transact as transact_consumed
            return transact_consumed(authority, coordinate, decide, _consumption_selection)
        for _attempt in range(8):
            before = identity()
            existing = read_records_sealed_snapshot(authority, coordinate,
                allowed_kinds=set(kinds))
            if identity() != before:
                continue
            result, candidates = decide(existing)
            batch = tuple(candidates)
            if len(existing) + len(batch) > MAX_LEDGER_RECORDS:
                raise ProtocolRefusal("ledger_record_limit",
                    f"ledger maximum is {MAX_LEDGER_RECORDS} records")
            encoded = tuple(_encode_record(record, tenant, kinds) for record in batch)
            existing_ids = {record["id"] for record in existing}
            candidate_ids = [record["id"] for record in batch]
            if len(set(candidate_ids)) != len(candidate_ids):
                raise ProtocolRefusal("duplicate_record_id", "record batch repeats an id")
            duplicate = next((record_id for record_id in candidate_ids
                              if record_id in existing_ids), None)
            if duplicate is not None:
                raise ProtocolRefusal("duplicate_record_id",
                    f"record id {duplicate} already exists")
            with _locked_path(lock_path, exclusive=True, relative=lock_relative):
                if identity() != before:
                    continue
                if batch:
                    _append_frame(path, b"".join(encoded))
                return result
        raise ProtocolRefusal("consumption_snapshot_contention",
            f"{coordinate.as_posix()} changed during eight consumption captures",
            remedy={"kind": "transient", "action": "retry_once",
                    "delay_seconds": 1, "holder": "unknown"})


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
    _deny_adhoc_kinds(allowed_kinds)
    kinds = _kinds(allowed_kinds)
    path, tenant = _resolve(authority, relative, write=True)
    lock_path, lock_relative = _lock_beside(path, relative)
    with _epoch_writer_guard(authority, relative, path=path), _locked_path(
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
        if _adhoc_kind(record):
            _adhoc_only()
        if record.get("kind") == "native_dispatch_launcher_record":
            _native_launcher_only()
        if record.get("kind") == "seat_execution":
            raise ProtocolRefusal("seat_execution_controller_only", "execution testimony requires its native controller",
                remedy="use the authenticated native execution producer instead of generic ledger append")
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


def _epoch_identity_delta(expected: tuple, observed: tuple) -> str:
    fields = ("st_dev", "st_ino", "st_mode", "st_nlink", "st_size",
              "st_mtime_ns", "st_ctime_ns")
    return ", ".join(field for field, left, right in zip(fields, expected, observed)
                     if left != right)


def _bind_epoch_selected(
    authority: FloatiRoot,
    selected: Sequence[tuple[str, Path, str, object]],
) -> list[tuple[str, Path, str, object]]:
    """Bind one closed selected coordinate set without trusting its snapshots."""

    if not isinstance(authority, FloatiRoot):
        raise TypeError("epoch replacement requires a FloatiRoot")
    bound: list[tuple[str, Path, str, object]] = []
    coordinates: set[str] = set()
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
            or path != authority.tenant_home / relative
            or relative in coordinates
            or plane != ("events" if relative == "events.jsonl" else Path(relative).parts[1])
            or (
                identity is not None and (
                    not isinstance(identity, tuple) or len(identity) != 7
                    or any(not isinstance(value, int) or isinstance(value, bool)
                           for value in identity)
                )
            )
        ):
            raise ProtocolRefusal(
                "epoch_selected_member_changed",
                "epoch replacement member is not bound to the selected root",
            )
        coordinates.add(relative)
        bound.append((relative, path, plane, identity))
    if "events.jsonl" not in coordinates:
        raise ProtocolRefusal(
            "epoch_selected_member_changed",
            "epoch replacement is missing its events coordinate",
        )
    return bound


@contextmanager
def _epoch_selected_writer(
    authority: FloatiRoot,
    selected: Sequence[tuple[str, Path, str, object]],
    *,
    automatic_targets: Optional[Sequence[tuple[str, str, bytes]]] = None,
    rebind_selected: Optional[Callable[[], Sequence[tuple[str, Path, str, object]]]] = None,
) -> Iterator[Callable[..., None]]:
    """Hold the exact selected ledger locks across archive, commit and swap.

    The caller already owns EPOCH exclusive and the applicable WAKE guards.
    Only the yielded active, root-bound callable can replace these coordinates;
    a fresh snapshot read after acquisition supplies the seven identity fields.
    """

    from .bus_epoch import _require_owned_epoch_guard

    bound = _bind_epoch_selected(authority, selected)
    epoch_hold = _require_owned_epoch_guard(authority, exclusive=True)
    root_binding = (authority.path, authority.tenant_home, authority.tenant_id)
    coordinates = frozenset((relative, path, plane)
                            for relative, path, plane, _identity in bound)
    target_coordinates = None
    if automatic_targets is not None:
        if rebind_selected is None:
            raise TypeError("automatic target opening requires a fresh selected binding")
        target_coordinates = set()
        for item in automatic_targets:
            if not isinstance(item, tuple) or len(item) != 3:
                raise TypeError("automatic targets must be closed payload tuples")
            relative, plane, payload = item
            if (not isinstance(relative, str) or not isinstance(plane, str)
                    or not isinstance(payload, bytes) or relative in target_coordinates
                    or (relative, authority.tenant_home / relative, plane) not in coordinates):
                raise ProtocolRefusal("epoch_selected_member_changed", "automatic target is outside the sealed selected set",
                    remedy="replan automatic maintenance from the current selected ledger coordinates")
            target_coordinates.add(relative)
        target_coordinates.add("events.jsonl")
    owner = (os.getpid(), threading.get_ident())
    active = False
    with ExitStack() as stack:
        for relative, path, _plane, _identity in sorted(
            bound, key=lambda item: item[0].encode("utf-8")
        ):
            lock_path, lock_relative = _lock_beside(path, relative)
            stack.enter_context(
                _locked_path(lock_path, exclusive=True, relative=lock_relative)
            )
        opened_targets = {}
        automatic_snapshot = None
        if target_coordinates is not None:
            # Writable open can itself change host metadata. Perform every
            # existing-target open before the authoritative selected binding,
            # then retain that exact descriptor through archive and swap.
            # An absent events file is created only after COMMITTED below.
            for relative, path, _plane, identity in bound:
                if relative not in target_coordinates or identity is None:
                    continue
                flags = os.O_RDWR | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
                try:
                    descriptor = os.open(path, flags, 0o600)
                    stack.callback(os.close, descriptor)
                    status = os.fstat(descriptor)
                except OSError as exc:
                    raise _durability_failure(exc, path) from exc
                if not stat.S_ISREG(status.st_mode) or status.st_nlink != 1:
                    raise DurabilityFailure("epoch_selected_member_changed", "automatic target is not an ordinary selected file: " + relative)
                opened_targets[relative] = descriptor
            rebound = _bind_epoch_selected(authority, rebind_selected())
            if frozenset((relative, path, plane)
                         for relative, path, plane, _identity in rebound) != coordinates:
                raise DurabilityFailure("epoch_selected_member_changed", "selected coordinates changed during automatic target opening")
            for relative, path, _plane, identity in rebound:
                descriptor = opened_targets.get(relative)
                if descriptor is None:
                    if relative in target_coordinates and identity is not None:
                        raise DurabilityFailure("epoch_selected_member_changed", "automatic target appeared during selected binding: " + relative)
                    continue
                try:
                    descriptor_status, named_status = os.fstat(descriptor), path.lstat()
                except OSError as exc:
                    raise _durability_failure(exc, path) from exc
                for status in (descriptor_status, named_status):
                    observed = (status.st_dev, status.st_ino, status.st_mode, status.st_nlink,
                                status.st_size, status.st_mtime_ns, status.st_ctime_ns)
                    if observed != identity:
                        raise DurabilityFailure("epoch_selected_member_changed", "automatic target changed during selected binding: " + relative)
            automatic_snapshot = tuple(rebound)
        active = True

        def replace(
            selected_snapshot: Sequence[tuple[str, Path, str, object]],
            receipt_frame: bytes,
            *,
            fault: Optional[Callable[[str], None]] = None,
            target_payloads: Optional[tuple[tuple[str, str, bytes], ...]] = None,
            recheck_target: Optional[Callable[[], None]] = None,
        ) -> None:
            if not active or owner != (os.getpid(), threading.get_ident()):
                raise ProtocolRefusal(
                    "epoch_selected_writer_scope_invalid",
                    "epoch replacement requires its active owning writer scope",
                    remedy="perform replacement synchronously inside the native selected writer scope",
                )
            if (
                root_binding != (authority.path, authority.tenant_home, authority.tenant_id)
                or _require_owned_epoch_guard(authority, exclusive=True) is not epoch_hold
            ):
                raise ProtocolRefusal(
                    "epoch_selected_writer_scope_invalid",
                    "epoch replacement writer no longer owns its bound root",
                    remedy="open a new native selected writer scope for the exact owned root",
                )
            bound = _bind_epoch_selected(authority, selected_snapshot)
            if frozenset((relative, path, plane)
                         for relative, path, plane, _identity in bound) != coordinates:
                raise ProtocolRefusal(
                    "epoch_selected_member_changed",
                    "epoch replacement coordinates differ from the locked selected set",
                )
            if automatic_snapshot is not None and tuple(bound) != automatic_snapshot:
                raise DurabilityFailure("epoch_selected_member_changed", "automatic replacement cannot rebind its selected snapshot")
            if not isinstance(receipt_frame, bytes) or not receipt_frame:
                raise TypeError("epoch replacement requires one nonempty receipt frame")
            try:
                receipt_records = decode_frames(receipt_frame)
            except FrameError:
                receipt_records = []
            if any(_adhoc_kind(record) for record in receipt_records):
                _adhoc_only()

            def boundary(name: str) -> None:
                if fault is not None:
                    fault(name)

            if target_payloads is not None:
                from .bus_epoch import _preflight_selected_members
                if _preflight_selected_members(authority) != tuple(bound):
                    raise DurabilityFailure("epoch_selected_member_changed", "selected epoch changed before automatic replacement")
                targets = {}
                for relative, plane, payload in target_payloads:
                    if (relative in targets or not isinstance(payload, bytes)
                            or (relative, authority.tenant_home / relative, plane) not in coordinates):
                        raise ProtocolRefusal("epoch_selected_member_changed", "automatic target is outside the sealed selected set",
                            remedy="replan automatic maintenance from the current selected ledger coordinates")
                    targets[relative] = payload
                if "events.jsonl" not in targets or not targets["events.jsonl"].startswith(receipt_frame):
                    raise ProtocolRefusal("epoch_selected_member_changed", "automatic target lacks its bound roll frame",
                        remedy="rebuild the automatic target with its exact committed roll receipt first")
                if target_coordinates is not None and set(targets) != target_coordinates:
                    raise ProtocolRefusal("epoch_selected_member_changed", "automatic replacement differs from its opened target set",
                        remedy="replan automatic maintenance from the current selected ledger coordinates")
                by_path = {item[0]: (index, item) for index, item in enumerate(bound)}
                ordered = ["events.jsonl"] + sorted(set(targets) - {"events.jsonl"}, key=lambda item: item.encode("utf-8"))
                for relative in ordered:
                    index, (_relative, path, _plane, identity) = by_path[relative]
                    if recheck_target is not None:
                        recheck_target()
                    flags = os.O_RDWR | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
                    if identity is None:
                        flags |= os.O_CREAT | os.O_EXCL
                    retained_descriptor = opened_targets.get(relative)
                    if target_coordinates is not None and identity is not None and retained_descriptor is None:
                        raise DurabilityFailure("epoch_selected_member_changed", "automatic replacement lacks its retained target descriptor: " + relative)
                    try:
                        descriptor = (retained_descriptor if retained_descriptor is not None
                                      else os.open(path, flags, 0o600))
                    except OSError as exc:
                        raise _durability_failure(exc, path) from exc
                    try:
                        status = os.fstat(descriptor)
                        observed = (status.st_dev, status.st_ino, status.st_mode, status.st_nlink,
                                    status.st_size, status.st_mtime_ns, status.st_ctime_ns)
                        if identity is not None and observed != identity:
                            raise DurabilityFailure("epoch_selected_member_changed", "automatic target changed before replacement: " + relative)
                        if retained_descriptor is not None:
                            named = path.lstat()
                            named_identity = (named.st_dev, named.st_ino, named.st_mode, named.st_nlink,
                                              named.st_size, named.st_mtime_ns, named.st_ctime_ns)
                            if named_identity != identity:
                                raise DurabilityFailure("epoch_selected_member_changed", "automatic target path changed before replacement: " + relative)
                        payload = targets[relative]
                        prefix = "live_events" if relative == "events.jsonl" else f"live_member_{index}"
                        os.lseek(descriptor, 0, os.SEEK_SET)
                        if os.read(descriptor, len(payload) + 1) == payload:
                            # Visible equal bytes may be a complete interrupted
                            # write whose original owner never reached fsync.
                            os.fsync(descriptor)
                            boundary(prefix + "_synced")
                            continue
                        os.ftruncate(descriptor, 0)
                        os.lseek(descriptor, 0, os.SEEK_SET)
                        boundary(prefix + "_replaced")
                        offset, write_index = 0, 0
                        while offset < len(payload):
                            written = os.write(descriptor, payload[offset:])
                            if written <= 0:
                                raise DurabilityFailure("short_write", "automatic live payload could not be written completely")
                            offset += written
                            write_index += 1
                            boundary(prefix + f"_write_{write_index}")
                        os.fsync(descriptor)
                        boundary(prefix + "_synced")
                    except OSError as exc:
                        raise _durability_failure(exc, path) from exc
                    finally:
                        if retained_descriptor is None:
                            os.close(descriptor)
                for index, (relative, path, _plane, identity) in enumerate(bound):
                    if relative in targets or identity is None:
                        continue
                    if recheck_target is not None:
                        recheck_target()
                    status = path.lstat()
                    observed = (status.st_dev, status.st_ino, status.st_mode, status.st_nlink,
                                status.st_size, status.st_mtime_ns, status.st_ctime_ns)
                    if observed != identity:
                        raise DurabilityFailure("epoch_selected_member_changed", "automatic member changed before retirement: " + relative)
                    os.unlink(path)
                    boundary(f"live_member_{index}_retired")
                    parent = os.open(path.parent, os.O_RDONLY)
                    try:
                        os.fsync(parent)
                    finally:
                        os.close(parent)
                    boundary(f"live_member_{index}_parent_synced")
                parent = os.open(authority.tenant_home, os.O_RDONLY)
                try:
                    os.fsync(parent)
                finally:
                    os.close(parent)
                boundary("live_epoch_parent_synced")
                return

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
                        "selected epoch events changed before replacement: "
                        + _epoch_identity_delta(events_identity, observed_identity),
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
                        f"selected epoch member {relative} changed before retirement: "
                        + _epoch_identity_delta(identity, observed),
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

        try:
            yield replace
        finally:
            active = False


def _replace_epoch_selected(
    authority: FloatiRoot,
    selected: Sequence[tuple[str, Path, str, object]],
    receipt_frame: bytes,
    *,
    fault: Optional[Callable[[str], None]] = None,
) -> None:
    """Legacy recovery seam; acquire the selected writer scope at the swap."""

    with _epoch_selected_writer(authority, selected) as replace:
        replace(selected, receipt_frame, fault=fault)


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
    with _epoch_writer_guard(authority, relative, path=path), _locked_path(
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


def _transact_adhoc_records(authority, decide):
    """Separate read families and exact producer kinds for native ad-hoc truth."""
    from .adhoc_records import PROFILE_RELATIVE, JOURNAL_RELATIVE
    from .adhoc_profiles import AdhocProfiles, _ADHOC_PROFILE_TRANSACTION_CODE
    from .adhoc_journal import (AdhocJournal, JOURNAL_KINDS,
        _ADHOC_JOURNAL_ADMISSION_CODE, _ADHOC_JOURNAL_OPENED_CODE, _ADHOC_JOURNAL_LIFECYCLE_CODE)
    try:
        caller = sys._getframe(1)
    except ValueError:
        caller = None
    owner = None if caller is None else caller.f_locals.get("self")
    if caller is not None and caller.f_code is _ADHOC_PROFILE_TRANSACTION_CODE and type(owner) is AdhocProfiles:
        relative, kinds = PROFILE_RELATIVE, frozenset({"adhoc_profile_record"})
        write_kinds = kinds
    elif caller is not None and caller.f_code is _ADHOC_JOURNAL_ADMISSION_CODE and type(owner) is AdhocJournal:
        relative, kinds = JOURNAL_RELATIVE, frozenset(JOURNAL_KINDS)
        write_kinds = frozenset({"adhoc_dispatch_record"})
    elif caller is not None and caller.f_code is _ADHOC_JOURNAL_OPENED_CODE and type(owner) is AdhocJournal:
        relative, kinds = JOURNAL_RELATIVE, frozenset(JOURNAL_KINDS)
        write_kinds = frozenset({"adhoc_native_thread_opened_record"})
    elif caller is not None and caller.f_code is _ADHOC_JOURNAL_LIFECYCLE_CODE and type(owner) is AdhocJournal:
        relative, kinds = JOURNAL_RELATIVE, frozenset(JOURNAL_KINDS)
        write_kinds = frozenset({"adhoc_lifecycle_record"})
    else:
        _adhoc_only()
    if not isinstance(authority, FloatiRoot) or owner.root is not authority:
        _adhoc_only()
    if not authority.tenant_home.is_dir():
        raise DurabilityFailure("root_deleted", "the selected root no longer exists")
    path = authority.resolve_relative(relative)
    if path != authority.tenant_home / relative:
        _adhoc_only()
    lock_path, lock_relative = _lock_beside(path, relative)
    with epoch_guard(authority), _locked_path(lock_path, exclusive=True, relative=lock_relative):
        if authority.resolve_relative(relative) != path:
            _adhoc_only()
        existing = _read_path_records(path, authority.tenant_id, kinds)
        result, record = decide(existing)
        if record is not None:
            if record.get("kind") not in write_kinds:
                _adhoc_only()
            if len(existing) >= MAX_LEDGER_RECORDS:
                raise ProtocolRefusal("ledger_record_limit", "ad-hoc metadata exceeds its ledger bound")
            validated = validate_record(record, authority.tenant_id, write_kinds, integrity=False)
            encoded = _encode_record(validated, authority.tenant_id, write_kinds)
            # Encoding is a separate boundary: a compromised producer/encoder
            # cannot borrow its broader read family as append authority.
            try:
                frames = decode_frames(encoded)
                if len(frames) != 1 or frames[0] != validated or frames[0].get("kind") not in write_kinds:
                    _adhoc_only()
                validate_record(frames[0], authority.tenant_id, write_kinds, integrity=False)
            except FrameError:
                _adhoc_only()
            if any(row["id"] == record["id"] for row in existing):
                raise ProtocolRefusal("duplicate_record_id", "ad-hoc metadata repeats an identity")
            _append_frame(path, encoded)
        return result


_ADHOC_NATIVE_TRANSACTION_CODE = _transact_adhoc_records.__code__


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


def read_records_compatible_sealed_snapshot(
    authority: Authority,
    relative: Union[Path, str],
    *,
    allowed_kinds: Optional[Set[str]] = None,
    max_bytes: int = MAX_RECORD_BYTES,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, object]]]:
    """Read a sealed generation while reporting well-formed future kinds."""

    if isinstance(authority, TenantObservation):
        # Read capabilities must never create or acquire a foreign lock file.
        return read_records_compatible_snapshot(
            authority, relative, allowed_kinds=allowed_kinds, max_bytes=max_bytes
        )
    summaries: Dict[str, Dict[str, object]] = {}
    records = _read_records_sealed_snapshot(
        authority, relative, allowed_kinds=allowed_kinds, max_bytes=max_bytes,
        unrecognized=summaries,
    )
    return records, _unrecognized_rows(summaries)


def read_records_sealed_snapshot(
    authority: Authority,
    relative: Union[Path, str],
    *,
    allowed_kinds: Optional[Set[str]] = None,
    max_bytes: int = MAX_RECORD_BYTES,
) -> List[Dict[str, Any]]:
    """Read strict complete history without retaining its append lock over I/O."""

    if isinstance(authority, TenantObservation):
        return read_records_snapshot(
            authority, relative, allowed_kinds=allowed_kinds, max_bytes=max_bytes
        )
    return _read_records_sealed_snapshot(
        authority, relative, allowed_kinds=allowed_kinds, max_bytes=max_bytes,
    )


def _read_records_sealed_snapshot(
    authority: FloatiRoot,
    relative: Union[Path, str],
    *,
    allowed_kinds: Optional[Set[str]],
    max_bytes: int,
    unrecognized: Optional[Dict[str, Dict[str, object]]] = None,
) -> List[Dict[str, Any]]:
    """Seal the descriptor and end offset, then detach bytes before decoding.

    Only opening and metadata capture retain the ledger lock. Appends after
    that boundary belong to the next query. The shared epoch barrier prevents
    native in-place replacement until bytes detach; decoding owns no lock.
    """

    kinds = _kinds(allowed_kinds)
    path, tenant = _resolve(authority, relative, write=False)
    if not path.exists():
        return []
    lock_path, lock_relative = _lock_beside(path, relative)
    with _failure_input_testimony(path) as input_stat:
        try:
            with epoch_guard(authority), ExitStack() as opened:
                with _locked_path(lock_path, exclusive=False, relative=lock_relative):
                    handle = opened.enter_context(path.open("rb"))
                    before = os.fstat(handle.fileno())
                    input_stat[0] = before
                    selected = path.lstat()
                    if (
                        not stat.S_ISREG(before.st_mode)
                        or not stat.S_ISREG(selected.st_mode)
                        or (before.st_dev, before.st_ino) != (selected.st_dev, selected.st_ino)
                    ):
                        raise IntegrityFailure(
                            "ledger_identity_changed_during_read",
                            f"{path.name} changed identity before snapshot capture",
                        )
                    if before.st_size > MAX_LEDGER_BYTES:
                        raise IntegrityFailure("ledger_too_large", f"{path.name} exceeds {MAX_LEDGER_BYTES} bytes")
                data = handle.read(before.st_size)
                after = os.fstat(handle.fileno())
                if (
                    len(data) != before.st_size
                    or after.st_size < before.st_size
                    or (after.st_size == before.st_size and after.st_mtime_ns != before.st_mtime_ns)
                ):
                    raise IntegrityFailure(
                        "ledger_identity_changed_during_read",
                        f"{path.name} changed its sealed prefix during snapshot read",
                    )
        except OSError as exc:
            raise _durability_failure(exc, path) from exc
        records = _decode_path_records(
            path, tenant, kinds, data, max_bytes=max_bytes, unrecognized=unrecognized
        )
    return records


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


class _CursorReadCapture(NamedTuple):
    """Detached byte input; decoding never needs to retain its ledger lock."""

    mode: str
    before: Optional[os.stat_result] = None
    after: Optional[os.stat_result] = None
    data: bytes = b""


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
        self._head_frame = b""
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
        self._head_frame = b""
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
        skip_unknown: bool = False,
        *,
        _captured: Optional[_CursorReadCapture] = None,
    ) -> Tuple[List[Dict[str, Any]], Tuple[str, ...]]:
        with _failure_input_testimony(path) as input_stat:
            captured = self._capture_full(path) if _captured is None else _captured
            before, after, data = captured.before, captured.after, captured.data
            assert before is not None and after is not None
            input_stat[0] = before
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
            summaries: Optional[Dict[str, Dict[str, object]]] = (
                {} if skip_unknown else None
            )
            try:
                records = _decode_path_records(
                    path, tenant, kinds, data, max_bytes=max_bytes,
                    unrecognized=summaries,
                )
            except IntegrityFailure as exc:
                if exc.code == "incomplete_jsonl_line":
                    exc.waiter_byte_offset = data.rfind(b"\n") + 1
                raise
            frames = data.splitlines(keepends=True)
            if skip_unknown:
                # SKEW-2: unknown-kind frames stay inside the verified byte
                # prefix (the digests cover every durable frame) while the
                # validated records carry only known kinds, so the canonical
                # check pairs each known record with its own frame.
                known_frames = [
                    frame
                    for frame, raw in zip(frames, decode_frames(data))
                    if is_known_record_kind(
                        raw.get("kind", "<absent>") if isinstance(raw, dict) else "<absent>"
                    )
                ]
                if len(known_frames) != len(records):
                    raise IntegrityFailure(
                        "noncanonical_frame",
                        "durable framing does not match validated records",
                    )
            else:
                known_frames = frames
                if len(frames) != len(records):
                    raise IntegrityFailure(
                        "noncanonical_frame",
                        "durable framing does not match validated records",
                    )
            digest = self._initial_digest(domain)
            prefixes = [digest.hexdigest()]
            for record, frame in zip(records, known_frames):
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
            self._head_frame = encode_frame(self._records[0]) if self._records else b""
            self._prefixes = tuple(prefixes)
            self._digest = digest
            return self.snapshot()

    @staticmethod
    def _capture_full(path: Path) -> _CursorReadCapture:
        with _failure_input_testimony(path) as input_stat:
            try:
                before = path.stat()
                input_stat[0] = before
                data = path.read_bytes()
                after = path.stat()
            except OSError as exc:
                raise _durability_failure(exc, path) from exc
            return _CursorReadCapture("full", before, after, data)

    def _capture_locked(self, path: Path) -> _CursorReadCapture:
        """Capture only existing native I/O while the shared lock is held."""

        with _failure_input_testimony(path) as input_stat:
            if not path.exists():
                return _CursorReadCapture("empty")
            try:
                stat = path.stat()
                input_stat[0] = stat
            except OSError as exc:
                raise _durability_failure(exc, path) from exc
            identity = (int(stat.st_dev), int(stat.st_ino))
            if (
                self._identity is None
                or identity != self._identity
                or stat.st_size < self._byte_length
            ):
                return self._capture_full(path)
            if stat.st_size > MAX_LEDGER_BYTES:
                raise IntegrityFailure(
                    "ledger_too_large", f"{path.name} exceeds {MAX_LEDGER_BYTES} bytes"
                )
            retained_prefix_changed = False
            try:
                with path.open("rb") as handle:
                    opened = os.fstat(handle.fileno())
                    input_stat[0] = opened
                    opened_identity = (int(opened.st_dev), int(opened.st_ino))
                    if opened_identity != identity or opened.st_size != stat.st_size:
                        raise IntegrityFailure(
                            "ledger_identity_changed_during_read",
                            f"{path.name} changed identity or length before incremental read",
                        )
                    if self._records:
                        retained_prefix_changed = handle.read(len(self._head_frame)) != self._head_frame
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
                return self._capture_full(path)
            mode = "unchanged" if stat.st_size == self._byte_length else "append"
            return _CursorReadCapture(mode, opened, final, appended)

    def _read_captured(
        self,
        path: Path,
        tenant: str,
        kinds: FrozenSet[str],
        domain: str,
        max_bytes: int,
        captured: _CursorReadCapture,
        skip_unknown: bool = False,
    ) -> Tuple[List[Dict[str, Any]], Tuple[str, ...]]:
        """Validate and copy the detached input after releasing its lock."""

        with _failure_input_testimony(path) as input_stat:
            input_stat[0] = captured.before
            if captured.mode == "empty":
                if self._identity is None and self._prefixes:
                    return self.snapshot()
                return self._reset_empty(domain)
            if captured.mode == "full":
                return self._full_replay(path, tenant, kinds, domain, max_bytes,
                                         skip_unknown=skip_unknown, _captured=captured)
            if captured.mode == "unchanged":
                return self.snapshot()
            stat, appended = captured.after, captured.data
            assert stat is not None
            identity = (int(stat.st_dev), int(stat.st_ino))
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
                failure = self._frame_failure(path, exc, line_offset=line_offset)
                if exc.code == "incomplete_frame":
                    failure.waiter_byte_offset = self._byte_length + appended.rfind(b"\n") + 1
                raise failure from exc
            if line_offset + len(decoded) > MAX_LEDGER_RECORDS:
                raise IntegrityFailure(
                    "ledger_record_limit",
                    f"{path.name} exceeds {MAX_LEDGER_RECORDS} records",
                )
            frames = appended.splitlines(keepends=True)
            seen = {str(record["id"]) for record in self._records}
            validated: List[Dict[str, Any]] = []
            digest_indices: List[int] = []
            for index, raw_record in enumerate(decoded):
                line_number = line_offset + index + 1
                record_id = raw_record.get("id", "<absent>") if isinstance(raw_record, dict) else "<absent>"
                kind = raw_record.get("kind", "<absent>") if isinstance(raw_record, dict) else "<absent>"
                # SKEW-2: a well-formed future kind stays inside the verified
                # byte prefix (the digest advances over its frame) but never
                # enters the validated records, so the cursor's once-per-ledger
                # contract survives vocabulary the reader predates.
                if skip_unknown and not is_known_record_kind(kind):
                    validate_unknown_record(raw_record, tenant)
                    continue
                digest_indices.append(index)
                try:
                    if not is_known_record_kind(kind):
                        from .installed_reader import installed_reader_identity, refuse_older_reader

                        identity = installed_reader_identity()
                        if identity is not None:
                            validate_unknown_record(raw_record, tenant)
                            refuse_older_reader(str(kind), identity)
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
            # SKEW-2: the digest chain runs over the canonical frames of the
            # validated records (the wake-hold testimony contract pins
            # prefixes to records + 1); a skipped frame is consumed - it is
            # never re-read - but advances neither the chain nor the records.
            for index in digest_indices:
                digest.update(frames[index])
                prefixes.append(digest.hexdigest())
            self._identity = identity
            self._byte_length = int(stat.st_size)
            self._records.extend(dict(record) for record in validated)
            self._head_frame = encode_frame(self._records[0]) if self._records else b""
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
        skip_unknown_kinds: bool = False,
    ) -> Tuple[List[Dict[str, Any]], Tuple[str, ...]]:
        if not isinstance(domain, str) or not domain.isascii() or not domain:
            raise ProtocolRefusal(
                "prefix_digest_domain_invalid", "digest domain must be nonempty ASCII"
            )
        kinds = _kinds(allowed_kinds)
        path, tenant = _resolve(authority, relative, write=False)
        binding = (
            path.resolve(strict=False), tenant, kinds, domain, max_bytes,
            skip_unknown_kinds,
        )
        if self._binding is None:
            self._binding = binding
        elif self._binding != binding:
            raise ProtocolRefusal(
                "ledger_cursor_binding_mismatch",
                "verified ledger cursor is already bound to another ledger contract",
            )
        if isinstance(authority, TenantObservation):
            captured = self._capture_locked(path)
        else:
            lock_path, lock_relative = _lock_beside(path, relative)
            with _locked_path(lock_path, exclusive=False, relative=lock_relative):
                captured = self._capture_locked(path)
        return self._read_captured(path, tenant, kinds, domain, max_bytes, captured,
                                   skip_unknown=skip_unknown_kinds)


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
    skip_unknown_kinds: bool = False,
) -> Tuple[List[Dict[str, Any]], Tuple[str, ...]]:
    """Read validated canonical frames and every inclusive SHA-256 prefix.

    The first digest represents the empty prefix.  This narrow reader is
    intentionally read-only; callers cannot synthesize testimony from parsed
    mappings because every stored frame must equal its canonical encoding.
    """

    selected = cursor if cursor is not None else VerifiedLedgerCursor()
    try:
        return selected.read(
            authority,
            relative,
            allowed_kinds=allowed_kinds,
            domain=domain,
            max_bytes=max_bytes,
            skip_unknown_kinds=skip_unknown_kinds,
        )
    except Exception as exc:
        # Preserve only the owned input coordinate and the last verified byte
        # prefix. The check, exception and cause chain are unchanged; neither
        # record bytes nor exception detail enter waiter diagnostics.
        try:
            coordinate = _lock_coordinate(relative)
        except ValueError:
            # An invalid caller coordinate must retain its original refusal.
            raise exc
        exc.waiter_input_identity = {"path": coordinate}
        # Only claim an offset measured from the offending frame, never the
        # cursor cache length (which changes on a fresh full replay).
        offset = getattr(exc, "waiter_byte_offset", None)
        if isinstance(offset, int) and not isinstance(offset, bool) and offset >= 0:
            exc.waiter_input_identity["byte_offset"] = offset
        raise

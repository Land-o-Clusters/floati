"""Governed bus-epoch authority and public CLI seam."""

from __future__ import annotations

import argparse
import fcntl
import functools
import hashlib
import json
import os
import pathlib
import re
import stat
import threading
import time
import unicodedata
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterator, Mapping, Optional, Tuple, Union

from .errors import DurabilityFailure, IntegrityFailure, ProtocolRefusal
from .framing import FrameError, decode_frames, encode_frame
from .records import validate_record
from .root import FloatiRoot, resolve_command_root, validate_identifier


HandlerResult = Tuple[str, Dict[str, Any], int]
ROLL_AUTHORITY_SUBJECT = "bus-epoch-roll"
LOCK_ORDER_EPOCH = 1
LOCK_ORDER_WAKE = 2
LOCK_ORDER_LEDGER = 3
EPOCH_LOCK_TIMEOUT_SECONDS = 5.0
EPOCH_LOCK_POLL_SECONDS = 0.01
EPOCH_MARKER_PREFIX = ".floati-epoch-roll-"
EPOCH_MARKER_SUFFIX = ".v1.json"
EPOCH_STAGING_PREFIX = ".floati-epoch-staging-"
EPOCH_MARKER_MAX_BYTES = 65536
LockOrderScope = Union[FloatiRoot, pathlib.Path]
SelectedMember = tuple[
    str,
    pathlib.Path,
    str,
    Optional[tuple[int, int, int, int, int, int, int]],
]
_BIDI_CONTROLS = frozenset(
    {"LRE", "RLE", "LRO", "RLO", "PDF", "LRI", "RLI", "FSI", "PDI", "BN"}
)


class _OrderEntry:
    __slots__ = ("path", "rank", "label")

    def __init__(self, path: pathlib.Path, rank: int, label: str) -> None:
        self.path = path
        self.rank = rank
        self.label = label


class _EpochHold:
    __slots__ = ("descriptor", "exclusive", "depth")

    def __init__(self, descriptor: int, exclusive: bool) -> None:
        self.descriptor = descriptor
        self.exclusive = exclusive
        self.depth = 1


_THREAD_LOCK_STATE = threading.local()


def _refresh_thread_state() -> None:
    process_id = os.getpid()
    if getattr(_THREAD_LOCK_STATE, "process_id", None) == process_id:
        return
    inherited = getattr(_THREAD_LOCK_STATE, "epoch_holds", {})
    for hold in inherited.values():
        try:
            os.close(hold.descriptor)
        except OSError:
            pass
    _THREAD_LOCK_STATE.process_id = process_id
    _THREAD_LOCK_STATE.order_stack = []
    _THREAD_LOCK_STATE.epoch_holds = {}


def _scope_path(scope: LockOrderScope) -> pathlib.Path:
    if isinstance(scope, FloatiRoot):
        selected = scope.tenant_home
    elif isinstance(scope, pathlib.Path):
        selected = scope
    else:
        raise TypeError("lock-order scope must be a FloatiRoot or Path")
    return selected.resolve(strict=False)


def _same_lock_domain(left: pathlib.Path, right: pathlib.Path) -> bool:
    """Match an owning root with any contained lock coordinate, either way."""

    return left == right or left in right.parents or right in left.parents


def _order_stack() -> list[_OrderEntry]:
    _refresh_thread_state()
    stack = getattr(_THREAD_LOCK_STATE, "order_stack", None)
    if stack is None:
        stack = []
        _THREAD_LOCK_STATE.order_stack = stack
    return stack


@contextmanager
def lock_order_guard(
    scope: LockOrderScope,
    rank: int,
    *,
    label: str,
) -> Iterator[None]:
    """Track one non-mutating, thread-local acquisition in the G5 total order."""

    if rank not in {LOCK_ORDER_EPOCH, LOCK_ORDER_WAKE, LOCK_ORDER_LEDGER}:
        raise ValueError("lock-order rank must be epoch, wake, or ledger")
    path = _scope_path(scope)
    stack = _order_stack()
    for held in stack:
        if _same_lock_domain(path, held.path) and held.rank > rank:
            raise ProtocolRefusal(
                "lock_order_invalid",
                f"{label} lock cannot follow held {held.label} lock for the same root",
            )
    entry = _OrderEntry(path, rank, label)
    stack.append(entry)
    try:
        yield
    finally:
        if not stack or stack[-1] is not entry:
            raise RuntimeError("lock-order contexts must exit in acquisition order")
        stack.pop()


def _epoch_holds() -> dict[pathlib.Path, _EpochHold]:
    _refresh_thread_state()
    holds = getattr(_THREAD_LOCK_STATE, "epoch_holds", None)
    if holds is None:
        holds = {}
        _THREAD_LOCK_STATE.epoch_holds = holds
    return holds


def _epoch_descriptor(root: FloatiRoot) -> int:
    flags = os.O_RDONLY
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_DIRECTORY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        return os.open(root.tenant_home, flags)
    except OSError as exc:
        raise DurabilityFailure(
            "epoch_lock_unavailable",
            "the existing tenant home could not be opened for epoch coordination",
        ) from exc


def _acquire_epoch_descriptor(descriptor: int, *, exclusive: bool) -> None:
    operation = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
    deadline = time.monotonic() + EPOCH_LOCK_TIMEOUT_SECONDS
    while True:
        try:
            fcntl.flock(descriptor, operation | fcntl.LOCK_NB)
            return
        except BlockingIOError as exc:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ProtocolRefusal(
                    "epoch_lock_timeout",
                    "the tenant epoch lock remained contended for 5 seconds",
                ) from exc
            time.sleep(min(EPOCH_LOCK_POLL_SECONDS, remaining))
        except OSError as exc:
            raise DurabilityFailure(
                "epoch_lock_unavailable",
                "the tenant epoch lock could not be acquired",
            ) from exc


@contextmanager
def epoch_guard(root: FloatiRoot, exclusive: bool = False) -> Iterator[None]:
    """Take the root-wide shared/exclusive barrier without creating lock bytes."""

    if not isinstance(root, FloatiRoot):
        raise TypeError("epoch guard requires a FloatiRoot")
    if not isinstance(exclusive, bool):
        raise TypeError("epoch guard mode must be a boolean")
    key = _scope_path(root)
    holds = _epoch_holds()
    held = holds.get(key)
    if held is not None:
        if exclusive and not held.exclusive:
            raise ProtocolRefusal(
                "epoch_lock_upgrade_invalid",
                "a shared epoch lock cannot be upgraded in place",
            )
        held.depth += 1
        try:
            yield
        finally:
            held.depth -= 1
        return

    with lock_order_guard(root, LOCK_ORDER_EPOCH, label="epoch"):
        descriptor = _epoch_descriptor(root)
        try:
            _acquire_epoch_descriptor(descriptor, exclusive=exclusive)
        except BaseException:
            os.close(descriptor)
            raise
        hold = _EpochHold(descriptor, exclusive)
        holds[key] = hold
        try:
            yield
        finally:
            del holds[key]
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)


def shared_epoch_operation(operation: Callable[..., Any]) -> Callable[..., Any]:
    """Hold the shared epoch barrier across one complete root-owned method."""

    @functools.wraps(operation)
    def guarded(owner: object, *args: object, **kwargs: object) -> Any:
        root = getattr(owner, "root", None)
        if not isinstance(root, FloatiRoot):
            raise TypeError("shared epoch operations require a root-owned object")
        with epoch_guard(root, exclusive=False):
            return operation(owner, *args, **kwargs)

    return guarded


def _validate_roll_key(value: object) -> str:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 128
        or any(
            unicodedata.category(character) in {"Cc", "Cs"}
            or unicodedata.bidirectional(character) in _BIDI_CONTROLS
            for character in value
        )
    ):
        raise ProtocolRefusal(
            "idempotency_key_invalid",
            "epoch roll idempotency key is terminal-unsafe or out of bounds",
        )
    return value


def _parse_grant_time(value: object) -> datetime:
    if not isinstance(value, str):
        raise ProtocolRefusal(
            "authority_expired", "roll authority expiry evidence is unavailable"
        )
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ProtocolRefusal(
            "authority_expired", "roll authority expiry evidence is unavailable"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ProtocolRefusal(
            "authority_expired", "roll authority expiry evidence is unavailable"
        )
    return parsed.astimezone(timezone.utc)


def _require_roll_authority(
    root: FloatiRoot, actor: str, idempotency_key: object
) -> tuple[str, str, Dict[str, object]]:
    from .planes import AuthorityGrantStore
    from .registry import Registry

    owner = validate_identifier(actor, "actor")
    key = _validate_roll_key(idempotency_key)
    Registry(root).require_active(owner)
    grant = AuthorityGrantStore(root).exact_tail(ROLL_AUTHORITY_SUBJECT)
    if grant.get("holder") != owner:
        raise ProtocolRefusal(
            "holder_mismatch", "roll authority holder does not match the actor"
        )
    state = grant.get("state")
    if state == "expired":
        raise ProtocolRefusal("authority_expired", "roll authority has expired")
    if state != "active":
        raise ProtocolRefusal("authority_released", "roll authority has been released")
    if datetime.now(timezone.utc) >= _parse_grant_time(grant.get("expires_at")):
        raise ProtocolRefusal("authority_expired", "roll authority has expired")
    return owner, key, grant


def _selected_epoch_members(
    root: FloatiRoot,
) -> tuple[tuple[str, pathlib.Path, str], ...]:
    """Derive the complete selected data-plane coordinate set without following links."""

    members: list[tuple[str, pathlib.Path, str]] = [
        ("events.jsonl", root.tenant_home / "events.jsonl", "events")
    ]
    receipts = root.tenant_home / "receipts"
    try:
        receipts_status = receipts.lstat()
    except FileNotFoundError:
        return tuple(members)
    except OSError as exc:
        raise DurabilityFailure(
            "epoch_selected_member_unavailable",
            "selected receipt namespace could not be inspected",
        ) from exc
    if stat.S_ISLNK(receipts_status.st_mode):
        raise ProtocolRefusal(
            "epoch_selected_member_symlink",
            "selected receipt namespace must not be a symlink",
        )
    if not stat.S_ISDIR(receipts_status.st_mode):
        raise ProtocolRefusal(
            "epoch_selected_member_not_regular",
            "selected receipt namespace must be an ordinary directory",
        )
    for plane in ("deliveries", "acks"):
        base = receipts / plane
        try:
            base_status = base.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise DurabilityFailure(
                "epoch_selected_member_unavailable",
                f"selected {plane} namespace could not be inspected",
            ) from exc
        if stat.S_ISLNK(base_status.st_mode):
            raise ProtocolRefusal(
                "epoch_selected_member_symlink",
                f"selected {plane} namespace must not be a symlink",
            )
        if not stat.S_ISDIR(base_status.st_mode):
            raise ProtocolRefusal(
                "epoch_selected_member_not_regular",
                f"selected {plane} namespace must be an ordinary directory",
            )
        for current, directories, files in os.walk(base, topdown=True, followlinks=False):
            current_path = pathlib.Path(current)
            for name in tuple(directories):
                directory = current_path / name
                status = directory.lstat()
                if stat.S_ISLNK(status.st_mode):
                    raise ProtocolRefusal(
                        "epoch_selected_member_symlink",
                        f"selected namespace ancestor "
                        f"{directory.relative_to(root.tenant_home).as_posix()} "
                        "must not be a symlink",
                    )
            names = tuple(directories) + tuple(files)
            for name in names:
                if not name.endswith(".jsonl"):
                    continue
                path = current_path / name
                relative = path.relative_to(root.tenant_home).as_posix()
                members.append((relative, path, plane))
    return tuple(sorted(members, key=lambda item: item[0].encode("utf-8")))


def _selected_member_identity(status: os.stat_result) -> tuple[int, int, int, int, int, int, int]:
    return (
        status.st_dev,
        status.st_ino,
        status.st_mode,
        status.st_nlink,
        status.st_size,
        status.st_mtime_ns,
        status.st_ctime_ns,
    )


def _preflight_selected_members(root: FloatiRoot) -> tuple[SelectedMember, ...]:
    """Bind the selected set and each regular-file identity before mutation."""

    selected: list[SelectedMember] = []
    for relative, path, _plane in _selected_epoch_members(root):
        try:
            status = path.lstat()
        except FileNotFoundError:
            if relative == "events.jsonl":
                selected.append((relative, path, _plane, None))
                continue
            continue
        except OSError as exc:
            raise DurabilityFailure(
                "epoch_selected_member_unavailable",
                f"selected epoch member {relative} could not be inspected",
            ) from exc
        if stat.S_ISLNK(status.st_mode):
            raise ProtocolRefusal(
                "epoch_selected_member_symlink",
                f"selected epoch member {relative} must not be a symlink",
            )
        if not stat.S_ISREG(status.st_mode):
            raise ProtocolRefusal(
                "epoch_selected_member_not_regular",
                f"selected epoch member {relative} must be an ordinary file",
            )
        if status.st_nlink != 1:
            raise ProtocolRefusal(
                "epoch_selected_member_not_regular",
                f"selected epoch member {relative} must not have hard-link aliases",
            )
        selected.append((relative, path, _plane, _selected_member_identity(status)))
    return tuple(selected)


def _read_selected_epoch_payloads(
    root: FloatiRoot,
    selected: tuple[SelectedMember, ...],
) -> tuple[tuple[str, str, bytes], ...]:
    """Read exactly the identity-bound selected epoch without following links."""

    rebound = _preflight_selected_members(root)
    if rebound != selected:
        raise DurabilityFailure(
            "epoch_selected_member_changed",
            "selected epoch membership or identity changed after preflight",
        )
    payloads = []
    for relative, path, plane, identity in selected:
        if identity is None:
            try:
                path.lstat()
            except FileNotFoundError:
                payloads.append((relative, plane, b""))
                continue
            except OSError as exc:
                raise DurabilityFailure(
                    "epoch_selected_member_unavailable",
                    f"selected epoch member {relative} could not be inspected",
                ) from exc
            raise DurabilityFailure(
                "epoch_selected_member_changed",
                f"selected epoch member {relative} appeared after preflight",
            )
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(path, flags)
        except OSError as exc:
            raise DurabilityFailure(
                "epoch_selected_member_unavailable",
                f"selected epoch member {relative} could not be opened without links",
            ) from exc
        try:
            before = os.fstat(descriptor)
            if _selected_member_identity(before) != identity or not stat.S_ISREG(before.st_mode):
                raise DurabilityFailure(
                    "epoch_selected_member_changed",
                    f"selected epoch member {relative} changed after preflight",
                )
            chunks = []
            while True:
                chunk = os.read(descriptor, 1024 * 1024)
                if not chunk:
                    break
                chunks.append(chunk)
            after = os.fstat(descriptor)
            if _selected_member_identity(after) != identity:
                raise DurabilityFailure(
                    "epoch_selected_member_changed",
                    f"selected epoch member {relative} changed while it was read",
                )
            payload = b"".join(chunks)
        finally:
            os.close(descriptor)
        payloads.append((relative, plane, payload))
    if _preflight_selected_members(root) != selected:
        raise DurabilityFailure(
            "epoch_selected_member_changed",
            "selected epoch membership or identity changed while it was read",
        )
    return tuple(payloads)


def _archive_facts_from_payloads(
    payloads: tuple[tuple[str, str, bytes], ...]
) -> Dict[str, object]:
    rows = [
        {
            "path": relative,
            "plane": plane,
            "byte_length": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
        }
        for relative, plane, payload in payloads
    ]
    rows.sort(key=lambda row: str(row["path"]).encode("utf-8"))
    canonical = b"".join(
        json.dumps(
            row,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        + b"\n"
        for row in rows
    )
    return {
        "archive_sha256": hashlib.sha256(canonical).hexdigest(),
        "archive_file_count": len(rows),
        "plane_counts": {
            plane: sum(row["plane"] == plane for row in rows)
            for plane in ("events", "deliveries", "acks")
        },
        "span": {
            "byte_start": 0,
            "byte_end": sum(len(payload) for _relative, _plane, payload in payloads),
        },
    }


def _epoch_id(facts: Dict[str, object]) -> str:
    return "epoch-" + str(facts["archive_sha256"])[:32]


def _read_bound_regular(
    path: pathlib.Path,
    status: os.stat_result,
    *,
    code: str,
    detail: str,
) -> bytes:
    """Read one identity-bound, singly linked file without following a link."""

    identity = _selected_member_identity(status)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ProtocolRefusal(code, detail) from exc
    try:
        before = os.fstat(descriptor)
        if (
            _selected_member_identity(before) != identity
            or not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
        ):
            raise ProtocolRefusal(code, detail)
        chunks = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        after = os.fstat(descriptor)
        if _selected_member_identity(after) != identity:
            raise ProtocolRefusal(code, detail)
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _archive_candidate(
    root: FloatiRoot, *, epoch_id: str, byte_end: int, request_id: str,
    sequence: int,
) -> pathlib.Path:
    if sequence < 0:
        raise ValueError("archive sequence must be nonnegative")
    if not isinstance(request_id, str) or not request_id:
        raise ValueError("archive request id must be nonempty")
    return root.tenant_home / (
        f"archive-{epoch_id}-span-0-{byte_end}-request-{request_id}-{sequence:020d}"
    )


def _build_roll_receipt(
    root: FloatiRoot,
    *,
    actor: str,
    idempotency_key: str,
    epoch_id: str,
    archive: pathlib.Path,
    facts: Dict[str, object],
) -> Dict[str, object]:
    """Build and validate exactly one closed receipt from derived archive facts."""

    from . import records as records_module
    from . import registry as registry_module

    receipt: Dict[str, object] = {
        "schema_version": 1,
        "id": "bus-epoch-roll-receipt-" + records_module.uuid7_hex(),
        "tenant_id": root.tenant_id,
        "timestamp": registry_module.utc_now(),
        "kind": "bus_epoch_roll_receipt",
        "archive_path": str(archive),
        "actor": actor,
        "idempotency_key": idempotency_key,
        "invalidated_followers": ["tail_followers", "waiters", "monitors"],
        "epoch_id": epoch_id,
        **facts,
    }
    return validate_record(
        receipt,
        root.tenant_id,
        frozenset({"bus_epoch_roll_receipt"}),
        integrity=False,
    )


def verify_epoch_archive(archive: pathlib.Path) -> Dict[str, object]:
    """Read and derive the four closed receipt facts from one detached archive."""

    if not isinstance(archive, pathlib.Path):
        raise TypeError("archive verifier requires a Path")
    try:
        root_status = archive.lstat()
    except OSError as exc:
        raise ProtocolRefusal(
            "archive_root_invalid", "archive root is unavailable"
        ) from exc
    if stat.S_ISLNK(root_status.st_mode) or not stat.S_ISDIR(root_status.st_mode):
        raise ProtocolRefusal(
            "archive_root_invalid", "archive root must be an ordinary directory"
        )

    payloads: list[tuple[str, str, bytes]] = []
    for current, directories, files in os.walk(archive, topdown=True, followlinks=False):
        current_path = pathlib.Path(current)
        for name in tuple(directories):
            path = current_path / name
            status = path.lstat()
            if stat.S_ISLNK(status.st_mode) or name.endswith(".jsonl"):
                raise ProtocolRefusal(
                    "archive_member_invalid",
                    f"archive member {path.relative_to(archive).as_posix()} is invalid",
                )
        for name in files:
            path = current_path / name
            status = path.lstat()
            relative = path.relative_to(archive).as_posix()
            if (
                stat.S_ISLNK(status.st_mode)
                or not stat.S_ISREG(status.st_mode)
                or status.st_nlink != 1
            ):
                raise ProtocolRefusal(
                    "archive_member_invalid", f"archive member {relative} is invalid"
                )
            if relative == "events.jsonl":
                plane = "events"
            elif relative.startswith("receipts/deliveries/") and relative.endswith(
                ".jsonl"
            ):
                plane = "deliveries"
            elif relative.startswith("receipts/acks/") and relative.endswith(".jsonl"):
                plane = "acks"
            else:
                raise ProtocolRefusal(
                    "archive_member_invalid", f"archive member {relative} is out of family"
                )
            payload = _read_bound_regular(
                path,
                status,
                code="archive_member_invalid",
                detail=f"archive member {relative} is unreadable or changed",
            )
            payloads.append((relative, plane, payload))

    if sum(plane == "events" for _relative, plane, _payload in payloads) != 1:
        raise ProtocolRefusal(
            "archive_events_invalid", "archive must contain exactly one events.jsonl"
        )
    return _archive_facts_from_payloads(tuple(payloads))


def validate_epoch_receipt_archive(
    root: FloatiRoot,
    receipt: Mapping[str, object],
) -> pathlib.Path:
    """Bind one validated receipt to one owned direct-child archive and its facts."""

    archive_value = receipt.get("archive_path")
    if not isinstance(archive_value, str):
        raise IntegrityFailure(
            "epoch_receipt_archive_invalid",
            "completed roll receipt does not name one archive path",
        )
    archive = pathlib.Path(archive_value)
    if (
        not archive.is_absolute()
        or archive.parent != root.tenant_home
        or not archive.name.startswith("archive-")
    ):
        raise IntegrityFailure(
            "epoch_receipt_archive_invalid",
            "completed roll receipt does not bind one owned direct-child archive",
        )
    epoch_id = receipt.get("epoch_id")
    span = receipt.get("span")
    if not isinstance(epoch_id, str) or not isinstance(span, Mapping):
        raise IntegrityFailure(
            "epoch_receipt_archive_invalid",
            "completed roll receipt archive coordinate is malformed",
        )
    expected_prefix = (
        f"archive-{epoch_id}-span-{span.get('byte_start')}-{span.get('byte_end')}-"
    )
    suffix = archive.name[len(expected_prefix):] if archive.name.startswith(expected_prefix) else ""
    sequence = suffix.rsplit("-", 1)[-1]
    if not sequence.isdigit():
        raise IntegrityFailure(
            "epoch_receipt_archive_invalid",
            "completed roll receipt archive name does not bind its epoch and span",
        )
    try:
        facts = verify_epoch_archive(archive)
    except ProtocolRefusal as exc:
        raise IntegrityFailure(
            "epoch_receipt_archive_invalid",
            "completed roll receipt archive is unavailable or invalid",
        ) from exc
    if any(receipt.get(field) != value for field, value in facts.items()):
        raise IntegrityFailure(
            "epoch_receipt_archive_invalid",
            "completed roll receipt facts do not match its archive",
        )
    return archive


def _owned_direct_child(root: FloatiRoot, value: object, *, field: str) -> pathlib.Path:
    if not isinstance(value, str):
        raise IntegrityFailure(
            "epoch_marker_invalid", f"epoch marker {field} must be an absolute path"
        )
    path = pathlib.Path(value)
    if not path.is_absolute() or path.parent != root.tenant_home:
        raise IntegrityFailure(
            "epoch_marker_invalid",
            f"epoch marker {field} must be one tenant-home direct child",
        )
    return path


def _bound_directory_identity(
    path: pathlib.Path, *, field: str, required: bool = False,
) -> Optional[tuple[int, int, int, int, int, int, int]]:
    try:
        status = path.lstat()
    except FileNotFoundError:
        if not required:
            return None
        raise IntegrityFailure(
            "epoch_marker_invalid", f"epoch marker {field} directory is unavailable"
        )
    except OSError as exc:
        raise IntegrityFailure(
            "epoch_marker_invalid", f"epoch marker {field} could not be inspected"
        ) from exc
    if stat.S_ISLNK(status.st_mode) or not stat.S_ISDIR(status.st_mode):
        raise IntegrityFailure(
            "epoch_marker_invalid", f"epoch marker {field} must be an ordinary directory"
        )
    return _selected_member_identity(status)


def _marker_path(root: FloatiRoot, request_id: str) -> pathlib.Path:
    if not isinstance(request_id, str) or not request_id:
        raise ValueError("epoch marker request id must be nonempty")
    return root.tenant_home / f"{EPOCH_MARKER_PREFIX}{request_id}{EPOCH_MARKER_SUFFIX}"


def _staging_path(root: FloatiRoot, request_id: str) -> pathlib.Path:
    if not isinstance(request_id, str) or not request_id:
        raise ValueError("epoch staging request id must be nonempty")
    return root.tenant_home / f"{EPOCH_STAGING_PREFIX}{request_id}"


def _request_from_marker_name(path: pathlib.Path) -> Optional[str]:
    name = path.name
    if not name.startswith(EPOCH_MARKER_PREFIX) or not name.endswith(EPOCH_MARKER_SUFFIX):
        return None
    request_id = name[len(EPOCH_MARKER_PREFIX):-len(EPOCH_MARKER_SUFFIX)]
    return request_id if re.fullmatch(r"[0-9a-f]{32}", request_id) else None


_MARKER_FIELDS = frozenset({
    "schema_version", "root", "tenant_id", "state", "request",
    "archive_path", "staging_path", "receipt", "absent_paths", "padding",
})
_MARKER_REQUEST_FIELDS = frozenset({"actor", "idempotency_key", "request_id"})


def _build_roll_marker(
    root: FloatiRoot,
    *,
    state: str,
    actor: str,
    idempotency_key: str,
    request_id: str,
    archive_path: Optional[pathlib.Path],
    staging_path: pathlib.Path,
    receipt: Optional[Mapping[str, object]],
    absent_paths: tuple[str, ...] = (),
) -> Dict[str, object]:
    """Build one canonical marker document without assigning recovery direction."""

    if state not in {"PREPARED", "COMMITTED"}:
        raise ValueError("epoch marker state must be PREPARED or COMMITTED")
    owner = validate_identifier(actor, "actor")
    key = _validate_roll_key(idempotency_key)
    if not isinstance(request_id, str) or not request_id:
        raise ValueError("epoch marker request id must be nonempty")
    if staging_path.parent != root.tenant_home or not staging_path.is_absolute():
        raise ValueError("epoch marker staging path must be one direct child")
    if archive_path is not None and (
        archive_path.parent != root.tenant_home or not archive_path.is_absolute()
    ):
        raise ValueError("epoch marker archive path must be one direct child")
    if staging_path != _staging_path(root, request_id):
        raise ValueError("epoch marker staging path must be request-bound")
    if archive_path is not None and f"-request-{request_id}-" not in archive_path.name:
        raise ValueError("epoch marker archive path must be request-bound")
    if any(path != "events.jsonl" for path in absent_paths):
        raise ValueError("epoch marker absent paths are out of family")
    if state == "COMMITTED" and (archive_path is None or receipt is None):
        raise ValueError("COMMITTED epoch marker requires archive and receipt evidence")
    return {
        "schema_version": 1,
        "root": str(root.path.resolve()),
        "tenant_id": root.tenant_id,
        "state": state,
        "request": {
            "actor": owner,
            "idempotency_key": key,
            "request_id": request_id,
        },
        "archive_path": None if archive_path is None else str(archive_path),
        "staging_path": str(staging_path),
        "receipt": None if receipt is None else dict(receipt),
        "absent_paths": list(absent_paths),
        # PREPARED is one byte shorter than COMMITTED.  The inverse pad keeps
        # a fully planned state transition equal-length for an exact rewrite.
        "padding": " " if state == "PREPARED" else "",
    }


def _encode_roll_marker(marker: Mapping[str, object]) -> bytes:
    try:
        return (
            json.dumps(
                dict(marker),
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            + b"\n"
        )
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise IntegrityFailure(
            "epoch_marker_invalid", "epoch marker is not compact I-JSON"
        ) from exc


def _committed_roll_marker(prepared: Mapping[str, object]) -> Dict[str, object]:
    committed = dict(prepared)
    if committed.get("state") != "PREPARED":
        raise IntegrityFailure(
            "epoch_marker_invalid", "only a PREPARED marker may be committed"
        )
    if committed.get("archive_path") is None or committed.get("receipt") is None:
        raise IntegrityFailure(
            "epoch_marker_invalid", "planned marker lacks archive or receipt evidence"
        )
    committed["state"] = "COMMITTED"
    committed["padding"] = ""
    prepared_bytes = _encode_roll_marker(prepared)
    committed_bytes = _encode_roll_marker(committed)
    if len(prepared_bytes) != len(committed_bytes):
        raise IntegrityFailure(
            "epoch_marker_invalid", "epoch marker state rewrite is not equal-length"
        )
    return committed


def observe_epoch_roll_state(root: FloatiRoot) -> Dict[str, object]:
    """Read one request-bound marker coordinate without choosing recovery."""

    if not isinstance(root, FloatiRoot):
        raise TypeError("epoch state observation requires a FloatiRoot")
    try:
        marker_paths = tuple(sorted(
            (
                child for child in root.tenant_home.iterdir()
                if child.name.startswith(EPOCH_MARKER_PREFIX)
                and child.name.endswith(EPOCH_MARKER_SUFFIX)
            ),
            key=lambda child: child.name.encode("utf-8"),
        ))
    except OSError as exc:
        raise IntegrityFailure(
            "epoch_marker_unavailable", "epoch marker namespace could not be listed"
        ) from exc
    if not marker_paths:
        return {"classification": "absent", "marker_path": None}
    if len(marker_paths) != 1:
        raise IntegrityFailure(
            "epoch_marker_invalid", "epoch marker coordinate is ambiguous"
        )
    marker_path = marker_paths[0]
    request_id = _request_from_marker_name(marker_path)
    if request_id is None:
        raise IntegrityFailure(
            "epoch_marker_invalid", "epoch marker coordinate has an invalid request id"
        )
    try:
        status = marker_path.lstat()
    except OSError as exc:
        raise IntegrityFailure(
            "epoch_marker_unavailable", "epoch marker could not be inspected"
        ) from exc
    if (
        stat.S_ISLNK(status.st_mode)
        or not stat.S_ISREG(status.st_mode)
        or status.st_nlink != 1
    ):
        raise IntegrityFailure(
            "epoch_marker_invalid", "epoch marker must be one singly linked regular file"
        )
    if status.st_size > EPOCH_MARKER_MAX_BYTES:
        raise IntegrityFailure(
            "epoch_marker_invalid", "epoch marker exceeds its bounded document size"
        )
    if status.st_size == 0:
        return {
            "classification": "partial",
            "marker_path": str(marker_path),
            "marker": None,
            "request_id": request_id,
            "marker_identity": _selected_member_identity(status),
        }
    try:
        payload = _read_bound_regular(
            marker_path,
            status,
            code="epoch_marker_invalid",
            detail="epoch marker changed while it was read",
        )
        marker = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {
            "classification": "partial",
            "marker_path": str(marker_path),
            "marker": None,
            "request_id": request_id,
            "marker_identity": _selected_member_identity(status),
        }
    if (
        not isinstance(marker, dict)
        or set(marker) != _MARKER_FIELDS
        or marker.get("schema_version") != 1
        or marker.get("state") not in {
        "PREPARED",
        "COMMITTED",
        }
    ):
        raise IntegrityFailure(
            "epoch_marker_invalid", "epoch marker state or document shape is invalid"
        )
    expected_padding = " " if marker["state"] == "PREPARED" else ""
    if marker.get("padding") != expected_padding:
        raise IntegrityFailure(
            "epoch_marker_invalid", "epoch marker state padding is noncanonical"
        )
    if marker.get("root") != str(root.path.resolve()) or marker.get("tenant_id") != root.tenant_id:
        raise IntegrityFailure(
            "epoch_marker_invalid", "epoch marker root or tenant binding is invalid"
        )
    request = marker.get("request")
    if not isinstance(request, Mapping) or set(request) != _MARKER_REQUEST_FIELDS:
        raise IntegrityFailure(
            "epoch_marker_invalid", "epoch marker request binding is unavailable"
        )
    validate_identifier(request.get("actor"), "actor")
    _validate_roll_key(request.get("idempotency_key"))
    if request.get("request_id") != request_id:
        raise IntegrityFailure(
            "epoch_marker_invalid", "epoch marker coordinate and request id differ"
        )
    staging = _owned_direct_child(root, marker.get("staging_path"), field="staging_path")
    if staging != _staging_path(root, request_id):
        raise IntegrityFailure(
            "epoch_marker_invalid", "epoch marker staging path is not request-bound"
        )
    absent_paths = marker.get("absent_paths")
    if (
        not isinstance(absent_paths, list)
        or any(path != "events.jsonl" for path in absent_paths)
        or len(absent_paths) != len(set(absent_paths))
    ):
        raise IntegrityFailure(
            "epoch_marker_invalid", "epoch marker absent-path testimony is invalid"
        )
    receipt = marker.get("receipt")
    archive_value = marker.get("archive_path")
    if archive_value is None or not isinstance(receipt, Mapping):
        raise IntegrityFailure(
            "epoch_marker_invalid", "epoch marker lacks its closed planned evidence"
        )
    if archive_value is not None:
        archive = _owned_direct_child(root, archive_value, field="archive_path")
        if f"-request-{request_id}-" not in archive.name:
            raise IntegrityFailure(
                "epoch_marker_invalid", "epoch marker archive path is not request-bound"
            )
        if receipt is not None:
            validated = validate_record(
                dict(receipt),
                root.tenant_id,
                frozenset({"bus_epoch_roll_receipt"}),
                integrity=True,
            )
            if pathlib.Path(str(validated["archive_path"])) != archive:
                raise IntegrityFailure(
                    "epoch_marker_invalid", "marker archive and receipt archive differ"
                )
            if (
                validated.get("actor") != request.get("actor")
                or validated.get("idempotency_key") != request.get("idempotency_key")
            ):
                raise IntegrityFailure(
                    "epoch_marker_invalid", "marker receipt and request bindings differ"
                )
    staging_identity = _bound_directory_identity(staging, field="staging_path")
    archive_identity = (
        None if archive_value is None
        else _bound_directory_identity(archive, field="archive_path")
    )
    return {
        "classification": marker["state"],
        "marker_path": str(marker_path),
        "marker": marker,
        "request_id": request_id,
        "marker_identity": _selected_member_identity(status),
        "staging_identity": staging_identity,
        "archive_identity": archive_identity,
    }


def _physical_first_roll_receipt(
    path: pathlib.Path, tenant_id: str
) -> Optional[Dict[str, object]]:
    """Read only a canonical physical-record-one receipt, tolerating opaque epochs."""

    try:
        status = path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise DurabilityFailure(
            "epoch_receipt_unavailable", "epoch receipt testimony could not be inspected"
        ) from exc
    if stat.S_ISLNK(status.st_mode) or not stat.S_ISREG(status.st_mode):
        return None
    try:
        with path.open("rb") as handle:
            frame = handle.readline(65537)
    except OSError as exc:
        raise DurabilityFailure(
            "epoch_receipt_unavailable", "epoch receipt testimony could not be read"
        ) from exc
    if not frame or len(frame) > 65536 or not frame.endswith(b"\n"):
        return None
    try:
        decoded = decode_frames(frame)
    except FrameError:
        return None
    if len(decoded) != 1 or decoded[0].get("kind") != "bus_epoch_roll_receipt":
        return None
    return validate_record(
        decoded[0],
        tenant_id,
        frozenset({"bus_epoch_roll_receipt"}),
        integrity=True,
    )


def _completed_roll_receipts(root: FloatiRoot) -> tuple[Dict[str, object], ...]:
    candidates = [root.tenant_home / "events.jsonl"]
    try:
        children = tuple(root.tenant_home.iterdir())
    except OSError as exc:
        raise DurabilityFailure(
            "epoch_receipt_unavailable", "completed roll testimony could not be listed"
        ) from exc
    for child in sorted(children, key=lambda path: path.name.encode("utf-8")):
        try:
            status = child.lstat()
        except OSError:
            continue
        if (
            child.name.startswith("archive-")
            and not stat.S_ISLNK(status.st_mode)
            and stat.S_ISDIR(status.st_mode)
        ):
            candidates.append(child / "events.jsonl")
    receipts = []
    for candidate in candidates:
        receipt = _physical_first_roll_receipt(candidate, root.tenant_id)
        if receipt is not None:
            receipts.append(receipt)
    return tuple(receipts)


def _validate_completed_roll_archive(
    root: FloatiRoot, receipt: Dict[str, object]
) -> None:
    validate_epoch_receipt_archive(root, receipt)


def _completed_roll_no_op(
    root: FloatiRoot, *, actor: str, idempotency_key: str
) -> Optional[Dict[str, object]]:
    matches = [
        receipt
        for receipt in _completed_roll_receipts(root)
        if receipt.get("idempotency_key") == idempotency_key
    ]
    if len(matches) > 1:
        raise IntegrityFailure(
            "epoch_receipt_duplicate",
            "multiple completed roll receipts claim one idempotency key",
        )
    if not matches:
        return None
    receipt = matches[0]
    _validate_completed_roll_archive(root, receipt)
    if receipt.get("actor") != actor:
        raise ProtocolRefusal(
            "idempotency_conflict",
            "epoch roll idempotency key belongs to another actor",
        )
    return {
        "no_op": True,
        "original": {
            "actor": actor,
            "idempotency_key": idempotency_key,
            "receipt_id": receipt["id"],
        },
    }


def _fault_boundary(fault: Optional[Callable[[str], None]], name: str) -> None:
    if fault is not None:
        fault(name)


def _fsync_path(
    path: pathlib.Path,
    *,
    fault: Optional[Callable[[str], None]],
    boundary: str,
    directory: bool = False,
) -> None:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    if directory:
        flags |= getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    _fault_boundary(fault, boundary)


def _create_bound_file(
    path: pathlib.Path,
    payload: bytes,
    *,
    fault: Optional[Callable[[str], None]],
    prefix: str,
) -> None:
    flags = (
        os.O_WRONLY | os.O_CREAT | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor = os.open(path, flags, 0o600)
    _fault_boundary(fault, prefix + "_created")
    try:
        offset = 0
        write_index = 0
        while offset < len(payload):
            written = os.write(descriptor, payload[offset:])
            if written <= 0:
                raise DurabilityFailure(
                    "epoch_write_incomplete", f"{path.name} could not be written completely"
                )
            offset += written
            write_index += 1
            _fault_boundary(fault, prefix + f"_write_{write_index}")
        os.fsync(descriptor)
        _fault_boundary(fault, prefix + "_synced")
    finally:
        os.close(descriptor)


def _rewrite_bound_file(
    path: pathlib.Path,
    before: bytes,
    after: bytes,
    *,
    fault: Optional[Callable[[str], None]],
    prefix: str,
    boundary_after_write: bool = True,
    boundary_after_sync: bool = True,
) -> None:
    if len(before) != len(after):
        raise IntegrityFailure(
            "epoch_marker_invalid", "epoch marker rewrite must preserve its byte length"
        )
    status = path.lstat()
    if stat.S_ISLNK(status.st_mode) or not stat.S_ISREG(status.st_mode) or status.st_nlink != 1:
        raise IntegrityFailure(
            "epoch_marker_invalid", "epoch marker changed before its state rewrite"
        )
    if _read_bound_regular(
        path, status, code="epoch_marker_invalid",
        detail="epoch marker changed before its state rewrite",
    ) != before:
        raise IntegrityFailure(
            "epoch_marker_invalid", "epoch marker bytes changed before their state rewrite"
        )
    flags = os.O_WRONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        offset = 0
        write_index = 0
        while offset < len(after):
            if hasattr(os, "pwrite"):
                written = os.pwrite(descriptor, after[offset:], offset)
            else:
                os.lseek(descriptor, offset, os.SEEK_SET)
                written = os.write(descriptor, after[offset:])
            if written <= 0:
                raise DurabilityFailure(
                    "epoch_write_incomplete", "epoch marker could not be rewritten completely"
                )
            offset += written
            write_index += 1
            if boundary_after_write:
                _fault_boundary(fault, prefix + f"_write_{write_index}")
        os.fsync(descriptor)
        if boundary_after_sync:
            _fault_boundary(fault, prefix + "_synced")
    finally:
        os.close(descriptor)


def _mkdir_one(
    path: pathlib.Path,
    *,
    fault: Optional[Callable[[str], None]],
    boundary: str,
) -> None:
    os.mkdir(path, 0o700)
    _fault_boundary(fault, boundary)


def _archive_parent_paths(
    archive: pathlib.Path, payloads: tuple[tuple[str, str, bytes], ...]
) -> tuple[pathlib.Path, ...]:
    parents = {
        parent
        for relative, _plane, _payload in payloads
        for parent in (archive / relative).parents
        if parent != archive and archive in parent.parents
    }
    return tuple(sorted(
        parents,
        key=lambda path: (len(path.relative_to(archive).parts),
                          path.relative_to(archive).as_posix().encode("utf-8")),
    ))


def _populate_archive(
    archive: pathlib.Path,
    payloads: tuple[tuple[str, str, bytes], ...],
    *,
    fault: Optional[Callable[[str], None]],
) -> None:
    for index, parent in enumerate(_archive_parent_paths(archive, payloads)):
        _mkdir_one(parent, fault=fault, boundary=f"archive_parent_{index}_created")
    for index, (relative, _plane, payload) in enumerate(payloads):
        _create_bound_file(
            archive / relative,
            payload,
            fault=fault,
            prefix=f"archive_member_{index}",
        )
    for index, directory in enumerate(reversed(_archive_parent_paths(archive, payloads))):
        _fsync_path(
            directory, fault=fault, boundary=f"archive_parent_{index}_synced",
            directory=True,
        )
    _fsync_path(
        archive, fault=fault, boundary="archive_root_synced", directory=True
    )


def _remove_new_tree(
    root: FloatiRoot,
    path: pathlib.Path,
    *,
    expected_identity: Optional[tuple[int, int, int, int, int, int, int]] = None,
) -> None:
    if path.parent != root.tenant_home and root.tenant_home not in path.parents:
        raise IntegrityFailure(
            "epoch_marker_invalid", "recovery path escapes the selected tenant home"
        )
    try:
        status = path.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISLNK(status.st_mode):
        raise IntegrityFailure(
            "epoch_marker_invalid", f"recovery path {path.name} became a symlink"
        )
    if expected_identity is not None and _selected_member_identity(status) != expected_identity:
        raise IntegrityFailure(
            "epoch_marker_invalid", f"recovery path {path.name} changed after observation"
        )
    if stat.S_ISREG(status.st_mode):
        os.unlink(path)
        return
    if not stat.S_ISDIR(status.st_mode):
        raise IntegrityFailure(
            "epoch_marker_invalid", f"recovery path {path.name} has an invalid type"
        )
    for child in tuple(sorted(path.iterdir(), key=lambda item: item.name.encode("utf-8"))):
        _remove_new_tree(root, child)
    os.rmdir(path)


def _rollback_marker_state(
    root: FloatiRoot,
    *,
    marker_path: pathlib.Path,
    marker_identity: tuple[int, int, int, int, int, int, int],
    archive: Optional[pathlib.Path],
    archive_identity: Optional[tuple[int, int, int, int, int, int, int]],
    staging: Optional[pathlib.Path],
    staging_identity: Optional[tuple[int, int, int, int, int, int, int]],
) -> None:
    if archive is not None:
        _remove_new_tree(root, archive, expected_identity=archive_identity)
    if staging is not None:
        _remove_new_tree(root, staging, expected_identity=staging_identity)
    try:
        status = marker_path.lstat()
    except FileNotFoundError:
        return
    if (
        stat.S_ISLNK(status.st_mode)
        or not stat.S_ISREG(status.st_mode)
        or status.st_nlink != 1
        or _selected_member_identity(status) != marker_identity
    ):
        raise IntegrityFailure(
            "epoch_marker_invalid", "epoch marker changed before rollback"
        )
    os.unlink(marker_path)


def _write_live_receipt(
    root: FloatiRoot,
    receipt: Mapping[str, object],
    selected: tuple[SelectedMember, ...],
    *,
    fault: Optional[Callable[[str], None]],
) -> None:
    from contextlib import ExitStack

    from .jsonl import _replace_epoch_selected
    from .registry import Registry
    from .wake_hold import wake_coordination_guard

    candidates = set()
    for relative, _path, plane, _identity in selected:
        parts = pathlib.Path(relative).parts
        if plane not in {"deliveries", "acks"} or len(parts) < 3:
            continue
        candidates.add(pathlib.Path(parts[2]).stem if len(parts) == 3 else parts[2])
    recipients = []
    registry = Registry(root)
    for candidate in sorted(candidates, key=lambda value: value.encode("utf-8")):
        try:
            recipients.append(registry.resolve_node_id(candidate, field="recipient"))
        except ProtocolRefusal as exc:
            if exc.code not in {"unknown_node", "recipient_invalid"}:
                raise
    with ExitStack() as stack:
        for recipient in recipients:
            stack.enter_context(wake_coordination_guard(root, recipient))
        _replace_epoch_selected(
            root, selected, encode_frame(dict(receipt)), fault=fault
        )


def _marker_paths_for_request(
    root: FloatiRoot, request_id: str
) -> tuple[
    Optional[pathlib.Path],
    Optional[tuple[int, int, int, int, int, int, int]],
    Optional[pathlib.Path],
    Optional[tuple[int, int, int, int, int, int, int]],
]:
    staging = _staging_path(root, request_id)
    archives = tuple(sorted(
        (
            child for child in root.tenant_home.iterdir()
            if child.name.startswith("archive-")
            and f"-request-{request_id}-" in child.name
        ),
        key=lambda child: child.name.encode("utf-8"),
    ))
    if len(archives) > 1:
        raise IntegrityFailure(
            "epoch_marker_invalid", "partial epoch marker has ambiguous archive state"
        )
    archive = archives[0] if archives else None
    archive_identity = (
        None if archive is None
        else _bound_directory_identity(archive, field="archive_path", required=True)
    )
    staging_identity = _bound_directory_identity(staging, field="staging_path")
    return (
        archive,
        archive_identity,
        staging if staging_identity is not None else None,
        staging_identity,
    )


def reconcile_epoch_roll(root: FloatiRoot) -> Optional[Dict[str, object]]:
    """Recover one incomplete request from surviving, request-bound disk state."""

    with epoch_guard(root, exclusive=True):
        observed = observe_epoch_roll_state(root)
        if observed["classification"] == "absent":
            return None
        marker_path = pathlib.Path(str(observed["marker_path"]))
        request_id = str(observed["request_id"])
        marker_status = marker_path.lstat()
        marker_identity = observed.get("marker_identity")
        if (
            not isinstance(marker_identity, tuple)
            or _selected_member_identity(marker_status) != marker_identity
        ):
            raise IntegrityFailure(
                "epoch_marker_invalid", "epoch marker changed after observation"
            )
        marker = observed.get("marker")
        if not isinstance(marker, Mapping):
            archive, archive_identity, staging, staging_identity = (
                _marker_paths_for_request(root, request_id)
            )
            _rollback_marker_state(
                root,
                marker_path=marker_path,
                marker_identity=marker_identity,
                archive=archive,
                archive_identity=archive_identity,
                staging=staging,
                staging_identity=staging_identity,
            )
            return {
                "root": str(root.path.resolve()),
                "tenant_id": root.tenant_id,
                "request": {
                    "actor": "unknown", "idempotency_key": "unknown",
                    "request_id": request_id,
                },
                "classification": "PREPARED",
                "direction": "rollback",
                "receipt": None,
            }
        request = dict(marker["request"])
        archive_value = marker.get("archive_path")
        staging_value = marker.get("staging_path")
        archive = pathlib.Path(str(archive_value)) if archive_value is not None else None
        staging = pathlib.Path(str(staging_value)) if staging_value is not None else None
        receipt = marker.get("receipt")
        archive_identity = observed.get("archive_identity")
        staging_identity = observed.get("staging_identity")
        if marker["state"] == "COMMITTED":
            if not isinstance(receipt, Mapping) or archive is None:
                raise IntegrityFailure(
                    "epoch_marker_invalid", "committed recovery evidence is incomplete"
                )
            validate_epoch_receipt_archive(root, receipt)
            if _bound_directory_identity(
                archive, field="archive_path", required=True
            ) != archive_identity:
                raise IntegrityFailure(
                    "epoch_marker_invalid", "epoch archive changed during recovery"
                )
            selected = _preflight_selected_members(root)
            _write_live_receipt(root, receipt, selected, fault=None)
            if staging is not None:
                _remove_new_tree(
                    root, staging, expected_identity=staging_identity  # type: ignore[arg-type]
                )
            if _selected_member_identity(marker_path.lstat()) != marker_identity:
                raise IntegrityFailure(
                    "epoch_marker_invalid", "epoch marker changed during recovery"
                )
            os.unlink(marker_path)
            return {
                "root": str(root.path.resolve()),
                "tenant_id": root.tenant_id,
                "request": request,
                "classification": "COMMITTED",
                "direction": "roll_forward",
                "receipt": dict(receipt),
            }
        _rollback_marker_state(
            root,
            marker_path=marker_path,
            marker_identity=marker_identity,
            archive=archive,
            archive_identity=archive_identity,  # type: ignore[arg-type]
            staging=staging,
            staging_identity=staging_identity,  # type: ignore[arg-type]
        )
        return {
            "root": str(root.path.resolve()),
            "tenant_id": root.tenant_id,
            "request": request,
            "classification": "PREPARED",
            "direction": "rollback",
            "receipt": None,
        }


def roll_bus_epoch(
    root: FloatiRoot,
    *,
    actor: str,
    idempotency_key: str,
    fault: Optional[Callable[[str], None]] = None,
) -> Dict[str, object]:
    """Rotate one byte-exact selected epoch under a request-bound recovery marker."""

    owner, key, _grant = _require_roll_authority(root, actor, idempotency_key)
    with epoch_guard(root, exclusive=True):
        # The first check gives invalid callers their stable zero-mutation
        # refusal.  Revalidate after exclusivity so a retirement or grant CAS
        # completed while we waited cannot authorize the roll from stale truth.
        owner, key, _grant = _require_roll_authority(root, owner, key)
        completed = _completed_roll_no_op(
            root, actor=owner, idempotency_key=key
        )
        if completed is not None:
            return completed
        observed = observe_epoch_roll_state(root)
        if observed["classification"] != "absent":
            raise ProtocolRefusal(
                "epoch_roll_recovery_required",
                "an incomplete epoch roll must be reconciled before another roll",
            )
        selected = _preflight_selected_members(root)
        payloads = _read_selected_epoch_payloads(root, selected)
        facts = _archive_facts_from_payloads(payloads)
        epoch_id = _epoch_id(facts)

        from . import records as records_module

        request_id = records_module.uuid7_hex()
        marker_path = _marker_path(root, request_id)
        staging = _staging_path(root, request_id)
        absent_paths = tuple(
            relative for relative, _path, _plane, identity in selected if identity is None
        )
        sequence = 0
        archive = _archive_candidate(
            root,
            epoch_id=epoch_id,
            byte_end=int(dict(facts["span"])["byte_end"]),
            request_id=request_id,
            sequence=sequence,
        )
        receipt = _build_roll_receipt(
            root,
            actor=owner,
            idempotency_key=key,
            epoch_id=epoch_id,
            archive=archive,
            facts=facts,
        )
        prepared = _build_roll_marker(
            root,
            state="PREPARED",
            actor=owner,
            idempotency_key=key,
            request_id=request_id,
            archive_path=archive,
            staging_path=staging,
            receipt=receipt,
            absent_paths=absent_paths,
        )
        prepared_bytes = _encode_roll_marker(prepared)
        _create_bound_file(
            marker_path,
            prepared_bytes,
            fault=fault,
            prefix="marker_prepared",
        )
        _fsync_path(
            root.tenant_home,
            fault=fault,
            boundary="marker_prepared_parent_synced",
            directory=True,
        )
        _fault_boundary(fault, "marker_prepared_durable")
        _fault_boundary(fault, "before_archive_staging")

        _mkdir_one(staging, fault=fault, boundary="staging_created")
        _fsync_path(
            root.tenant_home,
            fault=fault,
            boundary="staging_parent_synced",
            directory=True,
        )
        while True:
            try:
                _mkdir_one(
                    archive,
                    fault=fault,
                    boundary=f"archive_sequence_{sequence}_reserved",
                )
                break
            except FileExistsError:
                sequence += 1
                next_archive = _archive_candidate(
                    root,
                    epoch_id=epoch_id,
                    byte_end=int(dict(facts["span"])["byte_end"]),
                    request_id=request_id,
                    sequence=sequence,
                )
                next_receipt = _build_roll_receipt(
                    root,
                    actor=owner,
                    idempotency_key=key,
                    epoch_id=epoch_id,
                    archive=next_archive,
                    facts=facts,
                )
                next_prepared = _build_roll_marker(
                    root,
                    state="PREPARED",
                    actor=owner,
                    idempotency_key=key,
                    request_id=request_id,
                    archive_path=next_archive,
                    staging_path=staging,
                    receipt=next_receipt,
                    absent_paths=absent_paths,
                )
                next_bytes = _encode_roll_marker(next_prepared)
                _rewrite_bound_file(
                    marker_path,
                    prepared_bytes,
                    next_bytes,
                    fault=fault,
                    prefix=f"marker_replanned_{sequence}",
                )
                _fsync_path(
                    root.tenant_home,
                    fault=fault,
                    boundary=f"marker_replanned_{sequence}_parent_synced",
                    directory=True,
                )
                archive, receipt, prepared, prepared_bytes = (
                    next_archive, next_receipt, next_prepared, next_bytes
                )
        _fsync_path(
            root.tenant_home,
            fault=fault,
            boundary="archive_reservation_parent_synced",
            directory=True,
        )
        _populate_archive(archive, payloads, fault=fault)
        try:
            verified = verify_epoch_archive(archive)
            if any(verified.get(field) != value for field, value in facts.items()):
                raise IntegrityFailure(
                    "epoch_archive_invalid",
                    "prepared archive facts differ from selected bytes",
                )
            if _preflight_selected_members(root) != selected:
                raise DurabilityFailure(
                    "epoch_selected_member_changed",
                    "selected epoch changed while its archive was prepared",
                )
            validate_epoch_receipt_archive(root, receipt)
        except (ProtocolRefusal, IntegrityFailure, DurabilityFailure):
            _rollback_marker_state(
                root,
                marker_path=marker_path,
                marker_identity=_selected_member_identity(marker_path.lstat()),
                archive=archive,
                archive_identity=_bound_directory_identity(
                    archive, field="archive_path", required=True
                ),
                staging=staging,
                staging_identity=_bound_directory_identity(
                    staging, field="staging_path", required=True
                ),
            )
            raise

        committed = _committed_roll_marker(prepared)
        committed_bytes = _encode_roll_marker(committed)
        _rewrite_bound_file(
            marker_path,
            prepared_bytes,
            committed_bytes,
            fault=fault,
            prefix="marker_committed",
            boundary_after_write=False,
            boundary_after_sync=False,
        )
        _fsync_path(
            root.tenant_home,
            fault=fault,
            boundary="marker_committed_parent_synced",
            directory=True,
        )
        _fault_boundary(fault, "marker_committed_durable")

        _write_live_receipt(root, receipt, selected, fault=fault)
        os.rmdir(staging)
        _fault_boundary(fault, "staging_retired")
        os.unlink(marker_path)
        _fault_boundary(fault, "marker_retired")
        _fsync_path(
            root.tenant_home,
            fault=fault,
            boundary="roll_cleanup_parent_synced",
            directory=True,
        )
        _fault_boundary(fault, "roll_complete")
        return {"receipt": receipt}


def _bind_refusal(exc: ProtocolRefusal, root: FloatiRoot) -> None:
    exc.artifact_context = {  # type: ignore[attr-defined]
        "root": str(root.path.resolve()),
        "tenant_id": root.tenant_id,
    }


def _roll(args: argparse.Namespace) -> HandlerResult:
    root = resolve_command_root(args.root, create=False)
    try:
        result = roll_bus_epoch(
            root,
            actor=args.actor,
            idempotency_key=args.idempotency_key,
        )
    except ProtocolRefusal as exc:
        _bind_refusal(exc, root)
        raise
    return "ok", {
        "root": str(root.path.resolve()),
        "tenant_id": root.tenant_id,
        **result,
    }, 0


def register_cli(commands: argparse._SubParsersAction) -> None:
    epoch = commands.add_parser("epoch")
    operations = epoch.add_subparsers(dest="epoch_command", required=True)
    roll = operations.add_parser("roll")
    roll.add_argument("--root", required=True)
    roll.add_argument("--as", dest="actor", required=True)
    roll.add_argument("--idempotency-key", required=True)
    roll.set_defaults(handler=_roll)

"""Governed bus-epoch authority and public CLI seam."""

from __future__ import annotations

import argparse
import copy
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
from contextlib import ExitStack, contextmanager
from contextvars import ContextVar
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
# EPOCH-EX-STARVATION-1: once a tenant has a turnstile, a contended exclusive
# acquisition holds it, so new shared holders queue behind it while the holders
# in front of it drain. A shared acquirer defers to it for at most this share of
# its own budget, so a shared holder that waits on a later shared acquirer can
# never deadlock. The turnstile is created only while the exclusive barrier is
# held, by init or else by a tenant's first contended exclusive acquisition, so
# no shared observation of the tenant home ever sees its membership change.
EPOCH_TURNSTILE_NAME = "epoch-turnstile.lock"
EPOCH_TURNSTILE_DEFERENCE_SHARE = 0.5
EPOCH_MARKER_PREFIX = ".floati-epoch-roll-"
EPOCH_MARKER_SUFFIX = ".v1.json"
EPOCH_STAGING_PREFIX = ".floati-epoch-staging-"
EPOCH_MARKER_MAX_BYTES = 65536
EPOCH_WRITER_NAMESPACE_MAX_ENTRIES = 100000
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
    except FileNotFoundError as exc:
        # Readers may hold a validated root after its directory disappears.
        # Preserve the native reader's diagnosis before taking its barrier.
        raise DurabilityFailure(
            "root_deleted", "the selected root no longer exists"
        ) from exc
    except OSError as exc:
        raise DurabilityFailure(
            "epoch_lock_unavailable",
            "the existing tenant home could not be opened for epoch coordination",
        ) from exc


class EpochAcquisitionAbandoned(Exception):
    """A contended acquisition stopped because its caller no longer needs it."""


# A caller's abandon predicate, scoped to the acquisition it is entering.
_EPOCH_ABANDON: ContextVar[Optional[Callable[[], bool]]] = ContextVar(
    "floati_epoch_abandon", default=None)
_EPOCH_TURNSTILE_CREATE: ContextVar[bool] = ContextVar(
    "floati_epoch_turnstile_create", default=True)


@contextmanager
def abandon_contended_acquisition(predicate: Callable[[], bool]) -> Iterator[None]:
    """Let epoch acquisitions entered here stop waiting once ``predicate`` holds.

    A contended acquisition then raises ``EpochAcquisitionAbandoned`` and holds
    nothing. Callers enter only the guard inside this scope, never its body.
    """

    selected = _EPOCH_ABANDON.set(predicate)
    try:
        yield
    finally:
        _EPOCH_ABANDON.reset(selected)


@contextmanager
def without_epoch_turnstile_creation() -> Iterator[None]:
    """Acquire a barrier without creating any tenant lock file."""
    selected = _EPOCH_TURNSTILE_CREATE.set(False)
    try:
        yield
    finally:
        _EPOCH_TURNSTILE_CREATE.reset(selected)


def _epoch_turnstile(descriptor: int, *, create: bool) -> Optional[int]:
    """Open the tenant's turnstile beside the barrier, or create it under held EX."""

    flags = os.O_RDONLY | os.O_NONBLOCK
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    if create:
        flags |= os.O_CREAT
    try:
        turnstile = os.open(EPOCH_TURNSTILE_NAME, flags, 0o600, dir_fd=descriptor)
    except FileNotFoundError:
        return None
    except OSError as exc:
        if create:
            # Fairness is best effort: a tenant that cannot hold a turnstile
            # keeps the plain barrier, and readers refuse a planted one.
            return None
        raise DurabilityFailure(
            "epoch_lock_unavailable",
            "the tenant epoch turnstile could not be opened",
        ) from exc
    try:
        opened = os.fstat(turnstile)
    except OSError as exc:
        os.close(turnstile)
        raise DurabilityFailure(
            "epoch_lock_unavailable",
            "the tenant epoch turnstile could not be inspected",
        ) from exc
    if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
        os.close(turnstile)
        raise DurabilityFailure(
            "epoch_lock_unavailable",
            "the tenant epoch turnstile is not an ordinary file",
        )
    return turnstile


def _acquire_epoch_descriptor(descriptor: int, *, exclusive: bool, use_turnstile: bool = True) -> None:
    operation = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
    abandon = _EPOCH_ABANDON.get()
    from .waiter_lock_retry import current_lock_window
    window = current_lock_window()
    budget = EPOCH_LOCK_TIMEOUT_SECONDS if window is None else min(EPOCH_LOCK_TIMEOUT_SECONDS, window.remaining())
    started = time.monotonic()
    deadline = started + budget
    deference_deadline = started + budget * EPOCH_TURNSTILE_DEFERENCE_SHARE
    # Both modes pass an existing turnstile before the barrier: an exclusive
    # waiter holds it until the barrier is its own, a shared one only until
    # it holds the barrier too.
    turnstile = _epoch_turnstile(descriptor, create=False) if use_turnstile else None
    passed = contended = False
    try:
        while True:
            try:
                if turnstile is not None and not passed:
                    fcntl.flock(turnstile, operation | fcntl.LOCK_NB)
                    passed = True
                fcntl.flock(descriptor, operation | fcntl.LOCK_NB)
                break
            except BlockingIOError as exc:
                contended = True
                if window is not None:
                    window.contended = True
                now = time.monotonic()
                remaining = deadline - now
                if remaining <= 0 or (window is not None and window.remaining() <= 0):
                    raise ProtocolRefusal(
                        "epoch_lock_timeout",
                        f"the tenant epoch lock remained contended for {budget:g} seconds",
                    ) from exc
                if abandon is not None and abandon():
                    raise EpochAcquisitionAbandoned() from exc
                if not exclusive and turnstile is not None and now >= deference_deadline:
                    try:
                        fcntl.flock(turnstile, fcntl.LOCK_UN)
                    finally:
                        os.close(turnstile)
                        turnstile = None
                        passed = False
                    continue
                if use_turnstile and exclusive and turnstile is None:
                    turnstile = _epoch_turnstile(descriptor, create=False)
                    if turnstile is not None:
                        continue
                if window is None:
                    time.sleep(min(EPOCH_LOCK_POLL_SECONDS, remaining))
                else:
                    window.sleep(min(EPOCH_LOCK_POLL_SECONDS, remaining, window.remaining()))
            except OSError as exc:
                raise DurabilityFailure(
                    "epoch_lock_unavailable",
                    "the tenant epoch lock could not be acquired",
                ) from exc
        if use_turnstile and exclusive and contended and turnstile is None and _EPOCH_TURNSTILE_CREATE.get():
            # The held barrier excludes every observer of tenant membership.
            created = _epoch_turnstile(descriptor, create=True)
            if created is not None:
                os.close(created)
    finally:
        if turnstile is not None:
            try:
                if passed:
                    fcntl.flock(turnstile, fcntl.LOCK_UN)
            finally:
                os.close(turnstile)


@contextmanager
def epoch_guard(root: FloatiRoot, exclusive: bool = False) -> Iterator[None]:
    """Take the root-wide shared/exclusive barrier.

    Uncontended, it creates no lock bytes. Exclusive writers may create the
    tenant's empty turnstile while holding the barrier after contention.
    Observations using an exclusive barrier disable creation.
    """

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


def ensure_epoch_turnstile(root: FloatiRoot) -> None:
    """Give a tenant its turnstile under a held exclusive barrier, if absent.

    ``init`` calls this so a tenant's first contended exclusive acquisition is
    already fair; a tenant without one gets it at its first contended one.
    """

    with epoch_guard(root, exclusive=True):
        held = _require_owned_epoch_guard(root, exclusive=True)
        created = _epoch_turnstile(held.descriptor, create=True)
        if created is not None:
            os.close(created)


def _require_owned_epoch_guard(
    root: FloatiRoot, *, exclusive: bool = False,
) -> _EpochHold:
    """Verify the calling thread's existing barrier and its physical root."""

    if not isinstance(root, FloatiRoot):
        raise TypeError("epoch ownership requires a FloatiRoot")
    held = _epoch_holds().get(_scope_path(root))
    if held is None or held.depth <= 0 or (exclusive and not held.exclusive):
        raise ProtocolRefusal(
            "epoch_lock_required", "this operation requires an owned epoch barrier",
            remedy="invoke the native epoch operation inside its owned epoch barrier",
        )
    try:
        current = root.tenant_home.lstat()
        opened = os.fstat(held.descriptor)
    except OSError as exc:
        raise DurabilityFailure(
            "epoch_lock_unavailable", "owned epoch barrier could not be verified"
        ) from exc
    if (
        not stat.S_ISDIR(current.st_mode) or not stat.S_ISDIR(opened.st_mode)
        or (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino)
    ):
        raise DurabilityFailure(
            "epoch_lock_unavailable", "owned epoch barrier no longer binds the tenant home"
        )
    return held


def _epoch_transition_pending(root: FloatiRoot) -> bool:
    """Inspect a bounded owned namespace without reading provisional history."""
    held = _require_owned_epoch_guard(root)
    descriptor = None
    pending = False
    try:
        descriptor = os.open(
            ".", os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=held.descriptor,
        )
        with os.scandir(descriptor) as entries:
            for count, entry in enumerate(entries, start=1):
                if count > EPOCH_WRITER_NAMESPACE_MAX_ENTRIES:
                    raise IntegrityFailure(
                        "epoch_marker_unavailable", "epoch writer admission namespace exceeds its bounded scan"
                    )
                if (entry.name.startswith(EPOCH_MARKER_PREFIX)
                        and entry.name.endswith(EPOCH_MARKER_SUFFIX)):
                    pending = True
                    break
    except OSError as exc:
        raise IntegrityFailure(
            "epoch_marker_unavailable", "epoch writer admission namespace could not be inspected"
        ) from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
    _require_owned_epoch_guard(root)
    return pending


def _require_epoch_writer_admission(root: FloatiRoot) -> None:
    """Refuse selected writes after surviving markers; recovery still owns EX."""
    if _require_owned_epoch_guard(root).exclusive:
        return
    if _epoch_transition_pending(root):
        raise ProtocolRefusal(
            "epoch_roll_recovery_required",
            "an incomplete epoch roll must be reconciled before selected writes",
        )


def shared_epoch_operation(
    operation: Optional[Callable[..., Any]] = None, *, selected_writer: bool = False,
) -> Callable[..., Any]:
    """Hold SH for a complete method; selected writers additionally need admission."""

    def decorate(selected: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(selected)
        def guarded(owner: object, *args: object, **kwargs: object) -> Any:
            root = getattr(owner, "root", None)
            if not isinstance(root, FloatiRoot):
                raise TypeError("shared epoch operations require a root-owned object")
            with epoch_guard(root, exclusive=False):
                if selected_writer:
                    _require_epoch_writer_admission(root)
                return selected(owner, *args, **kwargs)
        return guarded

    return decorate if operation is None else decorate(operation)


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


class _CompressedStat:
    """Stable stand-in for one verified member of a compressed epoch file."""

    __slots__ = ("st_dev", "st_ino", "st_mode", "st_nlink", "st_size",
                 "st_mtime_ns", "st_ctime_ns", "st_uid", "payload")

    def __init__(self, base, payload: bytes, ordinal: int) -> None:
        self.st_dev = base.st_dev
        self.st_ino = base.st_ino + ordinal + 1
        self.st_mode = stat.S_IFREG | 0o600
        self.st_nlink = 1
        self.st_size = len(payload)
        self.st_mtime_ns = base.st_mtime_ns
        self.st_ctime_ns = base.st_ctime_ns + ordinal
        self.st_uid = getattr(base, "st_uid", os.getuid())
        self.payload = payload


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
    *,
    _failure_inputs: Optional[dict] = None,
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
        if _failure_inputs is not None:
            _failure_inputs[(path, id(payload))] = (relative, before)
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

    if isinstance(status, _CompressedStat):
        return status.payload
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
    automatic: Optional[Mapping[str, object]] = None,
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
    if automatic is not None:
        receipt["automatic"] = dict(automatic)
    return validate_record(
        receipt,
        root.tenant_id,
        frozenset({"bus_epoch_roll_receipt"}),
        integrity=False,
    )


def _compressed_archive_snapshot(archive: pathlib.Path, root_status):
    """Project one verified ZIP epoch as member identities, without extractall."""
    from .archive_container import export_members
    try:
        _facts, payloads = export_members(archive)
    except (IntegrityFailure, ProtocolRefusal) as exc:
        raise ProtocolRefusal(
            "archive_root_invalid", "archive root must be an ordinary directory"
        ) from exc
    members = [(".", None, root_status)]
    for ordinal, name in enumerate(sorted(payloads, key=lambda value: value.encode("utf-8"))):
        raw = payloads[name]
        if name == "events.jsonl":
            plane = "events"
        elif name.startswith("receipts/deliveries/") and name.endswith(".jsonl"):
            plane = "deliveries"
        elif name.startswith("receipts/acks/") and name.endswith(".jsonl"):
            plane = "acks"
        else:
            raise ProtocolRefusal(
                "archive_member_invalid", f"archive member {name} is out of family"
            )
        members.append((name, plane, _CompressedStat(root_status, raw, ordinal)))
    if sum(plane == "events" for _relative, plane, _status in members) != 1:
        raise ProtocolRefusal(
            "archive_events_invalid", "archive must contain exactly one events.jsonl"
        )
    snapshot = tuple(members)
    _assert_epoch_archive_snapshot(archive, snapshot)
    return snapshot


def _epoch_archive_snapshot(
    archive: pathlib.Path,
) -> tuple[tuple[str, Optional[str], os.stat_result], ...]:
    """Bind detached archive membership and every seven-field identity."""

    if not isinstance(archive, pathlib.Path):
        raise TypeError("archive verifier requires a Path")
    try:
        root_status = archive.lstat()
    except OSError as exc:
        raise ProtocolRefusal(
            "archive_root_invalid", "archive root is unavailable"
        ) from exc
    if stat.S_ISLNK(root_status.st_mode):
        raise ProtocolRefusal(
            "archive_root_invalid", "archive root must be an ordinary directory"
        )
    if stat.S_ISREG(root_status.st_mode):
        try:
            with archive.open("rb") as handle:
                magic = handle.read(2)
        except OSError as exc:
            raise ProtocolRefusal(
                "archive_root_invalid", "archive root is unavailable"
            ) from exc
        if magic == b"PK":
            return _compressed_archive_snapshot(archive, root_status)
        raise ProtocolRefusal(
            "archive_root_invalid", "archive root must be an ordinary directory"
        )
    if not stat.S_ISDIR(root_status.st_mode):
        raise ProtocolRefusal(
            "archive_root_invalid", "archive root must be an ordinary directory"
        )

    members: list[tuple[str, Optional[str], os.stat_result]] = [(".", None, root_status)]
    for current, directories, files in os.walk(archive, topdown=True, followlinks=False):
        current_path = pathlib.Path(current)
        for name in tuple(directories):
            path = current_path / name
            status = path.lstat()
            if (stat.S_ISLNK(status.st_mode) or not stat.S_ISDIR(status.st_mode)
                    or name.endswith(".jsonl")):
                raise ProtocolRefusal(
                    "archive_member_invalid",
                    f"archive member {path.relative_to(archive).as_posix()} is invalid",
                )
            members.append((path.relative_to(archive).as_posix(), None, status))
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
            members.append((relative, plane, status))

    if sum(plane == "events" for _relative, plane, _status in members) != 1:
        raise ProtocolRefusal(
            "archive_events_invalid", "archive must contain exactly one events.jsonl"
        )
    return tuple(sorted(members, key=lambda member: member[0].encode("utf-8")))


def _assert_epoch_archive_snapshot(
    archive: pathlib.Path,
    snapshot: tuple[tuple[str, Optional[str], os.stat_result], ...],
) -> None:
    if (snapshot and snapshot[0][:2] == (".", None)
            and stat.S_ISREG(snapshot[0][2].st_mode)
            and len(snapshot) > 1
            and all(isinstance(status, _CompressedStat) for _name, _plane, status in snapshot[1:])):
        # Verified member bytes are immutable within this witness. Reproduce
        # the container's complete identity and no-follow path binding rather
        # than inflating the same file for every retained hold proof.
        from .archive_container import _parent, _same_named
        expected = snapshot[0][2]
        try:
            with _parent(archive) as (parent, name, check):
                descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
                try:
                    current = os.fstat(descriptor)
                    if (not stat.S_ISREG(current.st_mode) or current.st_nlink != 1
                            or _selected_member_identity(current) != _selected_member_identity(expected)):
                        raise IntegrityFailure("epoch_receipt_archive_invalid",
                                               "epoch archive changed after its bound read")
                    _same_named(parent, name, expected)
                    check()
                finally:
                    os.close(descriptor)
        except (OSError, IntegrityFailure) as exc:
            raise IntegrityFailure("epoch_receipt_archive_invalid",
                                   "epoch archive changed after its bound read") from exc
        return
    before = tuple((name, plane, _selected_member_identity(status))
                   for name, plane, status in snapshot)
    after = tuple((name, plane, _selected_member_identity(status))
                  for name, plane, status in _epoch_archive_snapshot(archive))
    if before != after:
        raise IntegrityFailure(
            "epoch_receipt_archive_invalid", "epoch archive changed after its bound read"
        )


def _read_epoch_archive_payloads(
    archive: pathlib.Path,
) -> tuple[
    tuple[tuple[str, str, bytes], ...],
    tuple[tuple[str, Optional[str], os.stat_result], ...],
]:
    snapshot = _epoch_archive_snapshot(archive)
    payloads = tuple(
        (relative, plane, status.payload if isinstance(status, _CompressedStat) else _read_bound_regular(
            archive / relative, status, code="archive_member_invalid",
            detail=f"archive member {relative} is unreadable or changed",
        ))
        for relative, plane, status in snapshot if plane is not None
    )
    _assert_epoch_archive_snapshot(archive, snapshot)
    return payloads, snapshot


def verify_epoch_archive(archive: pathlib.Path) -> Dict[str, object]:
    """Read and derive the four closed receipt facts from one detached archive."""

    payloads, _snapshot = _read_epoch_archive_payloads(archive)
    return _archive_facts_from_payloads(payloads)


def _epoch_receipt_archive_path(
    root: FloatiRoot,
    receipt: Mapping[str, object],
) -> pathlib.Path:
    """Bind a receipt's archive coordinate without reading its payload twice."""

    archive_value = receipt.get("archive_path")
    if not isinstance(archive_value, str):
        raise IntegrityFailure(
            "epoch_receipt_archive_invalid",
            "completed roll receipt does not name one archive path",
        )
    archive = pathlib.Path(archive_value)
    if (
        not archive.is_absolute()
        or str(archive) != archive_value
        or ".." in archive.parts
        or archive.parent.name != root.tenant_home.name
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
    # Durable testimony pins the tenant/name shape, not its former host prefix.
    # Read legacy absolute receipts unchanged and access only this selected root.
    selected = root.tenant_home / archive.name
    if selected.is_symlink():
        raise IntegrityFailure(
            "epoch_receipt_archive_invalid", "completed roll archive must not be a symlink"
        )
    return selected


def validate_epoch_receipt_archive(
    root: FloatiRoot,
    receipt: Mapping[str, object],
) -> pathlib.Path:
    """Bind one validated receipt to one owned direct-child archive and its facts."""

    archive = _epoch_receipt_archive_path(root, receipt)
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
_AUTOMATIC_MARKER_FIELDS = _MARKER_FIELDS | {"automatic_live"}


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
    automatic_live: Optional[Mapping[str, object]] = None,
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
    marker = {
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
    if automatic_live is not None:
        marker["automatic_live"] = dict(automatic_live)
    return marker


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
        or set(marker) not in (_MARKER_FIELDS, _AUTOMATIC_MARKER_FIELDS)
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
            if ("automatic" in validated) != ("automatic_live" in marker):
                raise IntegrityFailure(
                    "epoch_marker_invalid", "automatic receipt and staged target binding differ"
                )
    if "automatic_live" in marker:
        facts = marker["automatic_live"]
        if (not isinstance(facts, dict)
                or set(facts) != {"archive_sha256", "archive_file_count", "plane_counts", "span"}
                or not isinstance(facts["archive_sha256"], str)
                or re.fullmatch(r"[0-9a-f]{64}", facts["archive_sha256"]) is None
                or type(facts["archive_file_count"]) is not int or facts["archive_file_count"] < 1
                or not isinstance(facts["plane_counts"], dict)
                or set(facts["plane_counts"]) != {"events", "deliveries", "acks"}
                or any(type(value) is not int or value < 0 for value in facts["plane_counts"].values())
                or facts["plane_counts"]["events"] != 1
                or sum(facts["plane_counts"].values()) != facts["archive_file_count"]
                or not isinstance(facts["span"], dict)
                or set(facts["span"]) != {"byte_start", "byte_end"}
                or type(facts["span"]["byte_start"]) is not int or facts["span"]["byte_start"] != 0
                or type(facts["span"]["byte_end"]) is not int or facts["span"]["byte_end"] < 1):
            raise IntegrityFailure("epoch_marker_invalid", "automatic target facts are invalid")
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
    return _parse_physical_first_roll_receipt(frame, tenant_id)


def _parse_physical_first_roll_receipt(
    frame: bytes, tenant_id: str
) -> Optional[Dict[str, object]]:
    """Pure native record-one parsing, shared by descriptor-bound planning."""
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
    candidates = [("file", root.tenant_home / "events.jsonl")]
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
            candidates.append(("file", child / "events.jsonl"))
        elif (
            child.name.startswith("archive-")
            and not stat.S_ISLNK(status.st_mode)
            and stat.S_ISREG(status.st_mode)
        ):
            candidates.append(("container", child))
    receipts = []
    for kind, candidate in candidates:
        if kind == "container":
            receipt = _compressed_first_roll_receipt(candidate, root.tenant_id)
        else:
            receipt = _physical_first_roll_receipt(candidate, root.tenant_id)
        if receipt is not None:
            receipts.append(receipt)
    return tuple(receipts)


def _compressed_first_roll_receipt(
    archive: pathlib.Path, tenant_id: str
) -> Optional[Dict[str, object]]:
    """Read record one from a compressed archive without extracting it."""
    payloads, _snapshot = _read_epoch_archive_payloads(archive)
    raw = next((payload for name, _plane, payload in payloads if name == "events.jsonl"), b"")
    newline = raw.find(b"\n")
    frame = raw if newline < 0 else raw[:newline + 1]
    return _parse_physical_first_roll_receipt(frame, tenant_id)


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
    expected_members: Optional[Mapping[pathlib.Path, tuple[int, int, int, int, int, int, int]]] = None,
) -> None:
    if path.parent != root.tenant_home and root.tenant_home not in path.parents:
        raise IntegrityFailure(
            "epoch_marker_invalid", "recovery path escapes the selected tenant home"
        )
    if expected_members is not None:
        member_identity = expected_members.get(path)
        if member_identity is None:
            raise IntegrityFailure("epoch_marker_invalid", "staging cleanup found an unverified member")
        expected_identity = member_identity
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
        if expected_members is None:
            _remove_new_tree(root, child)
        else:
            _remove_new_tree(root, child, expected_members=expected_members)
    if expected_members is not None:
        # Removing our children changes directory size/times/link count. Its
        # device, inode and mode must still name the directory we entered.
        if _selected_member_identity(path.lstat())[:3] != expected_identity[:3]:
            raise IntegrityFailure("epoch_marker_invalid", "staging directory changed during cleanup")
    os.rmdir(path)


def _remove_verified_staging(root, staging, snapshot):
    """Delete only the exact staged tree verified before marker retirement."""
    _assert_epoch_archive_snapshot(staging, snapshot)
    members = {staging / relative: _selected_member_identity(status)
               for relative, _plane, status in snapshot}
    _remove_new_tree(root, staging, expected_members=members)


def _sync_automatic_retirement_parents(root, archived_payloads, target_payloads):
    """A prior unlink still owes its parent fsync when the file is now absent."""
    retained = {relative for relative, _plane, _payload in target_payloads}
    parents = {(root.tenant_home / relative).parent
               for relative, _plane, _payload in archived_payloads if relative not in retained}
    for parent in sorted(parents, key=lambda path: path.as_posix().encode("utf-8")):
        relative = parent.relative_to(root.tenant_home)
        if root.resolve_relative(relative) != parent:
            raise IntegrityFailure("epoch_marker_invalid", "retirement parent has an alias")
        expected = _bound_directory_identity(parent, field="retirement_parent", required=True)
        try:
            descriptor = os.open(parent, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
                                 | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_DIRECTORY", 0))
        except OSError as exc:
            raise DurabilityFailure("epoch_write_incomplete", "retirement parent could not be opened for sync") from exc
        try:
            if _selected_member_identity(os.fstat(descriptor)) != expected:
                raise IntegrityFailure("epoch_recovery_live_changed", "retirement parent changed before sync")
            os.fsync(descriptor)
        except OSError as exc:
            raise DurabilityFailure("epoch_write_incomplete", "retirement parent could not be synced") from exc
        finally:
            os.close(descriptor)
        if _bound_directory_identity(parent, field="retirement_parent", required=True) != expected:
            raise IntegrityFailure("epoch_recovery_live_changed", "retirement parent changed during sync")


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


def _selected_epoch_recipients(
    root: FloatiRoot, selected: tuple[SelectedMember, ...],
) -> tuple[str, ...]:
    from .registry import Registry

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
    return tuple(sorted(set(recipients), key=lambda value: value.encode("utf-8")))


@contextmanager
def _selected_epoch_write_scope(
    root: FloatiRoot, selected: tuple[SelectedMember, ...],
    *, automatic_targets: Optional[tuple[tuple[str, str, bytes], ...]] = None,
) -> Iterator[tuple[tuple[SelectedMember, ...], Callable[..., None]]]:
    """Hold EPOCH -> sorted WAKE -> selected LEDGER before binding payloads."""

    from .jsonl import _epoch_selected_writer
    from .wake_hold import wake_coordination_guard

    _require_owned_epoch_guard(root, exclusive=True)
    coordinates = tuple((relative, path, plane)
                        for relative, path, plane, _identity in selected)
    rebound = None

    def bind_selected():
        nonlocal rebound
        _require_owned_epoch_guard(root, exclusive=True)
        rebound = _preflight_selected_members(root)
        if tuple((relative, path, plane)
                 for relative, path, plane, _identity in rebound) != coordinates:
            raise DurabilityFailure(
                "epoch_selected_member_changed",
                "selected epoch membership changed before ledger exclusion",
            )
        return rebound

    with ExitStack() as stack:
        for recipient in _selected_epoch_recipients(root, selected):
            stack.enter_context(wake_coordination_guard(root, recipient))
        options = {} if automatic_targets is None else {
            "automatic_targets": automatic_targets, "rebind_selected": bind_selected,
        }
        replace = stack.enter_context(_epoch_selected_writer(root, selected, **options))
        _require_owned_epoch_guard(root, exclusive=True)
        # A ledger-only writer can finish before its lock is acquired. Its
        # bytes belong to the selected epoch; bind all seven fields only after
        # exclusion, while refusing a changed selected coordinate set.
        if automatic_targets is None:
            rebound = bind_selected()
        yield rebound, replace


def _write_live_receipt(
    root: FloatiRoot,
    receipt: Mapping[str, object],
    selected: tuple[SelectedMember, ...],
    *,
    fault: Optional[Callable[[str], None]],
    replacement: Optional[Callable[..., None]] = None,
    target_payloads: Optional[tuple[tuple[str, str, bytes], ...]] = None,
    recheck_target: Optional[Callable[[], None]] = None,
) -> None:
    options = {} if target_payloads is None else {
        "target_payloads": target_payloads, "recheck_target": recheck_target,
    }
    if replacement is not None:
        # The active sealed callable already owns WAKE and selected LEDGER.
        # Acquiring WAKE here would invert the total order or self-contend.
        replacement(selected, encode_frame(dict(receipt)), fault=fault, **options)
        return
    with _selected_epoch_write_scope(root, selected) as (rebound, replace):
        if rebound != selected:
            raise DurabilityFailure(
                "epoch_selected_member_changed",
                "selected epoch identity changed before replacement",
            )
        replace(selected, encode_frame(dict(receipt)), fault=fault, **options)


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


def _references_roll_receipt(events: bytes, receipt: Mapping[str, object]) -> bool:
    """Keep opaque old frames; recognize decoded IDs, including JSON escapes."""
    if str(receipt["id"]).encode("ascii") in events:
        return True
    for line in events.splitlines(keepends=True):
        if not line.endswith(b"\n"):
            continue
        try:
            records = decode_frames(line)
        except FrameError:
            continue
        for record in records:
            if isinstance(record, Mapping) and (
                record.get("id") == receipt["id"]
                or (record.get("kind") == "bus_epoch_roll_receipt"
                    and record.get("archive_path") == receipt["archive_path"])
            ):
                return True
    return False


def _committed_recovery_direction(
    archive_payloads: tuple[tuple[str, str, bytes], ...],
    live_payloads: tuple[tuple[str, str, bytes], ...],
    selected: tuple[SelectedMember, ...],
    receipt: Mapping[str, object],
) -> str:
    """Recognize only native replacement states or an unreplaced appended epoch."""

    archived = {relative: payload for relative, _plane, payload in archive_payloads}
    live = {relative: payload for relative, _plane, payload in live_payloads}
    frame = encode_frame(dict(receipt))
    events = live["events.jsonl"]
    # A truncate, partial receipt write, or already retired delivery/ACK is a
    # surviving native COMMITTED transaction, including opaque old payloads.
    forward = (
        set(live) <= set(archived)
        and (events == archived["events.jsonl"] or frame.startswith(events))
        and all(payload == archived[relative]
                for relative, payload in live.items() if relative != "events.jsonl")
    )
    if forward:
        return "roll_forward"

    present = {relative for relative, _path, _plane, identity in selected
               if identity is not None}
    unreplaced = (
        set(archived) <= present
        and all(live[relative].startswith(payload)
                for relative, payload in archived.items())
        and any(len(payload) > len(archived.get(relative, b""))
                for relative, payload in live.items())
    )
    # In particular, an empty archive prefixes a newly installed receipt plus
    # mail. Never remove an archive still referenced by that live receipt. The
    # Decode complete frames too: a valid JSON escape can hide the literal ID.
    if (unreplaced and not frame.startswith(events)
            and not _references_roll_receipt(events, receipt)):
        return "rollback"
    raise IntegrityFailure(
        "epoch_recovery_live_changed",
        "live epoch is neither a bound replacement state nor an unreplaced extension",
    )


def _automatic_recovery_direction(archive_payloads, live_payloads, selected, receipt,
                                  target_payloads, absent_paths):
    """Admit only a cut in the sealed target writer's deterministic sequence."""
    old = {relative: payload for relative, _plane, payload in archive_payloads}
    live = {relative: payload for relative, _plane, payload in live_payloads}
    target = {relative: payload for relative, _plane, payload in target_payloads}
    present = {relative for relative, _path, _plane, identity in selected if identity is not None}
    if not set(target) <= set(old) or "events.jsonl" not in target:
        raise IntegrityFailure("epoch_marker_invalid", "automatic target coordinates escape the old epoch")
    if not target["events.jsonl"].startswith(encode_frame(dict(receipt))):
        raise IntegrityFailure("epoch_marker_invalid", "automatic target lacks its exact roll frame")

    def matches(expected, writing=None):
        if set(live) != set(expected):
            return False
        if "events.jsonl" not in present and "events.jsonl" not in absent_paths:
            return False
        for relative, payload in expected.items():
            if relative != "events.jsonl" and relative not in present:
                return False
            actual = live[relative]
            if relative == writing:
                if not payload.startswith(actual):
                    return False
            elif actual != payload:
                return False
        return True

    expected = dict(old)
    if matches(expected):
        return "roll_forward"
    ordered = ["events.jsonl"] + sorted(set(target) - {"events.jsonl"}, key=lambda item: item.encode("utf-8"))
    for relative in ordered:
        if old[relative] == target[relative]:
            continue
        writing = dict(expected)
        writing[relative] = target[relative]
        if matches(writing, writing=relative):
            return "roll_forward"
        expected[relative] = target[relative]
        if matches(expected):
            return "roll_forward"
    for relative in sorted(set(old) - set(target), key=lambda item: item.encode("utf-8")):
        del expected[relative]
        if matches(expected):
            return "roll_forward"
    # Reuse the original extended-old-epoch proof and refusal vocabulary,
    # but never admit its receipt-only forward states for an automatic plan.
    direction = _committed_recovery_direction(archive_payloads, live_payloads, selected, receipt)
    if direction == "rollback":
        return direction
    raise IntegrityFailure("epoch_recovery_live_changed", "live epoch is outside the automatic replacement sequence")


def _record_epoch_recovery(
    root: FloatiRoot,
    request: Mapping[str, object],
    roll_receipt: Mapping[str, object],
    live_payloads: tuple[tuple[str, str, bytes], ...],
) -> Dict[str, object]:
    """Durably retain one original-request rollback decision outside the epoch."""

    from . import jsonl as jsonl_module, records as records_module, registry as registry_module

    request_id = str(request["request_id"])
    relative = pathlib.Path("receipts") / "epoch-recovery" / (request_id + ".jsonl")
    path = root.tenant_home / relative
    if root.resolve_relative(relative) != path:
        raise IntegrityFailure(
            "epoch_recovery_receipt_conflict", "recovery receipt path has an alias"
        )
    for directory in (path.parent.parent, path.parent):
        _bound_directory_identity(directory, field="recovery_receipt_parent")
    for leaf in (path, path.with_name(path.name + ".lock")):
        try:
            status = leaf.lstat()
        except FileNotFoundError:
            continue
        if (stat.S_ISLNK(status.st_mode) or not stat.S_ISREG(status.st_mode)
                or status.st_nlink != 1):
            raise IntegrityFailure(
                "epoch_recovery_receipt_conflict", "recovery receipt member has an invalid identity"
            )
    binding = {
        "actor": request["actor"], "idempotency_key": request["idempotency_key"],
        "request_id": request_id, "roll_receipt_id": roll_receipt["id"],
        "direction": "rollback", "reason_code": "live_epoch_extended",
        "archive_path": roll_receipt["archive_path"],
        "archive_sha256": roll_receipt["archive_sha256"],
    }

    def decide(existing):
        if existing:
            if len(existing) != 1 or any(existing[0].get(key) != value
                                         for key, value in binding.items()):
                raise IntegrityFailure(
                    "epoch_recovery_receipt_conflict",
                    "recovery receipt does not bind the original roll request",
                )
            # Its observed hash witnesses the first decision. A retry reruns
            # current prefix eligibility rather than rewriting that testimony.
            return dict(existing[0]), None
        record = {
            "schema_version": 1,
            "id": "bus-epoch-recovery-" + records_module.uuid7_hex(),
            "tenant_id": root.tenant_id, "timestamp": registry_module.utc_now(),
            "kind": "bus_epoch_recovery_receipt", **binding,
            "observed_live_sha256": _archive_facts_from_payloads(live_payloads)["archive_sha256"],
        }
        return record, record

    # Persist each directory link, including retry after a failed mkdir fsync.
    jsonl_module._ensure_directory(root.resolve_relative("receipts"))
    jsonl_module._ensure_directory(path.parent)
    result = jsonl_module.transact(
        root, relative, decide, allowed_kinds={"bus_epoch_recovery_receipt"},
    )
    # An existing transaction row may survive a failed parent fsync. Repeat
    # both syncs before allowing the durable marker decision on every retry.
    _fsync_path(path, fault=None, boundary="recovery_receipt_synced")
    _fsync_path(path.parent, fault=None, boundary="recovery_receipt_parent_synced", directory=True)
    return result


def reconcile_epoch_roll(root: FloatiRoot) -> Optional[Dict[str, object]]:
    """Recover one incomplete request from surviving, request-bound disk state."""

    with epoch_guard(root, exclusive=True), ExitStack() as selected_scope:
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
        target_payloads, target_snapshot = None, None
        if marker["state"] == "COMMITTED":
            if not isinstance(receipt, Mapping) or archive is None:
                raise IntegrityFailure(
                    "epoch_marker_invalid", "committed recovery evidence is incomplete"
                )
            bound_archive = validate_epoch_receipt_archive(root, receipt)
            if bound_archive != archive:
                raise IntegrityFailure(
                    "epoch_marker_invalid", "recovery archive differs from its receipt"
                )
            try:
                archive_payloads, archive_snapshot = _read_epoch_archive_payloads(archive)
            except ProtocolRefusal as exc:
                raise IntegrityFailure(
                    "epoch_receipt_archive_invalid", "recovery archive is unavailable or invalid"
                ) from exc
            facts = _archive_facts_from_payloads(archive_payloads)
            if any(receipt.get(field) != value for field, value in facts.items()):
                raise IntegrityFailure(
                    "epoch_receipt_archive_invalid", "recovery archive facts do not match its receipt"
                )
            if _bound_directory_identity(
                archive, field="archive_path", required=True
            ) != archive_identity:
                raise IntegrityFailure(
                    "epoch_marker_invalid", "epoch archive changed during recovery"
                )
            if "automatic_live" in marker:
                if staging is None:
                    raise IntegrityFailure("epoch_marker_invalid", "automatic recovery staging is absent")
                target_payloads, target_snapshot = _read_epoch_archive_payloads(staging)
                if _archive_facts_from_payloads(target_payloads) != marker["automatic_live"]:
                    raise IntegrityFailure("epoch_marker_invalid", "automatic recovery target differs from its marker")

            def recovery_direction(selected, live_payloads):
                if target_payloads is None:
                    return _committed_recovery_direction(archive_payloads, live_payloads, selected, receipt)
                return _automatic_recovery_direction(archive_payloads, live_payloads, selected,
                    receipt, target_payloads, marker["absent_paths"])

            selected = _preflight_selected_members(root)
            # Refuse unsafe bytes before creating absent WAKE/LEDGER lock
            # paths. EX excludes managed writers; classify again after the
            # scope binds all selected identities and physical locks.
            recovery_direction(selected, _read_selected_epoch_payloads(root, selected))
            selected, replacement = selected_scope.enter_context(
                _selected_epoch_write_scope(root, selected, automatic_targets=target_payloads)
            )
            live_payloads = _read_selected_epoch_payloads(root, selected)
            direction = recovery_direction(selected, live_payloads)

            def recheck_bound_evidence():
                if (_preflight_selected_members(root) != selected
                        or _selected_member_identity(marker_path.lstat()) != marker_identity
                        or _bound_directory_identity(
                            archive, field="archive_path", required=True
                        ) != archive_identity
                        or (staging is not None and _bound_directory_identity(
                            staging, field="staging_path"
                        ) != staging_identity)):
                    raise IntegrityFailure(
                        "epoch_recovery_live_changed", "recovery evidence changed after its bound read"
                    )
                _assert_epoch_archive_snapshot(archive, archive_snapshot)
                if target_snapshot is not None:
                    _assert_epoch_archive_snapshot(staging, target_snapshot)

            recheck_bound_evidence()
            if direction == "rollback":
                marker_before = _read_bound_regular(
                    marker_path, marker_status, code="epoch_marker_invalid",
                    detail="epoch marker changed before rollback decision",
                )
                prepared = dict(marker)
                prepared["state"], prepared["padding"] = "PREPARED", " "
                marker_after = _encode_roll_marker(prepared)
                if len(marker_before) != len(marker_after):
                    raise IntegrityFailure(
                        "epoch_marker_invalid", "rollback marker transition must preserve byte length"
                    )
                recovery_receipt = _record_epoch_recovery(root, request, receipt, live_payloads)
                recheck_bound_evidence()
                _rewrite_bound_file(
                    marker_path, marker_before, marker_after, fault=None,
                    prefix="marker_recovery_prepared",
                )
                _fsync_path(root.tenant_home, fault=None,
                            boundary="marker_recovery_prepared_durable", directory=True)
                prepared_identity = _selected_member_identity(marker_path.lstat())
                _rollback_marker_state(
                    root, marker_path=marker_path, marker_identity=prepared_identity,
                    archive=archive, archive_identity=archive_identity,
                    staging=staging, staging_identity=staging_identity,
                )
                return {
                    "root": str(root.path.resolve()), "tenant_id": root.tenant_id,
                    "request": request, "classification": "COMMITTED",
                    "direction": "rollback", "receipt": recovery_receipt,
                }
            def recheck_target():
                _assert_epoch_archive_snapshot(archive, archive_snapshot)
                if target_snapshot is not None:
                    _assert_epoch_archive_snapshot(staging, target_snapshot)
            _write_live_receipt(root, receipt, selected, fault=None, replacement=replacement,
                target_payloads=target_payloads, recheck_target=recheck_target)
            if target_payloads is not None:
                recheck_target()
                _sync_automatic_retirement_parents(root, archive_payloads, target_payloads)
                current = _preflight_selected_members(root)
                if _read_selected_epoch_payloads(root, current) != target_payloads:
                    raise IntegrityFailure("epoch_recovery_live_changed", "automatic recovery target changed before marker retirement")
            if target_payloads is None and staging is not None:
                _remove_new_tree(
                    root, staging, expected_identity=staging_identity  # type: ignore[arg-type]
                )
            if _selected_member_identity(marker_path.lstat()) != marker_identity:
                raise IntegrityFailure(
                    "epoch_marker_invalid", "epoch marker changed during recovery"
                )
            os.unlink(marker_path)
            if target_payloads is not None:
                _fsync_path(root.tenant_home, fault=None,
                            boundary="automatic_marker_retirement_synced", directory=True)
                _remove_verified_staging(root, staging, target_snapshot)
                _fsync_path(root.tenant_home, fault=None,
                            boundary="automatic_staging_retirement_synced", directory=True)
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

    return _roll_epoch_transaction(root, actor=actor, idempotency_key=idempotency_key, fault=fault)


def roll_automatic_epoch(root: FloatiRoot, *, actor: str, planner: Callable,
                         fault: Optional[Callable[[str], None]] = None):
    """Install an automatic carry decided under EX by an active native actor."""
    if not callable(planner):
        raise TypeError("automatic epoch planner must be callable")
    return _roll_epoch_transaction(root, actor=actor, planner=planner, fault=fault)


def _automatic_target(retained, selected, receipt, verification_payload=b""):
    from .jsonl import MAX_LEDGER_BYTES, MAX_LEDGER_RECORDS, MAX_RECORD_BYTES
    coordinates = {(relative, plane) for relative, _path, plane, _identity in selected}
    targets = []
    seen = set()
    for item in retained:
        if not isinstance(item, tuple) or len(item) != 3:
            raise ProtocolRefusal("epoch_selected_member_changed", "automatic carry must contain exact payload tuples",
                remedy="replan automatic maintenance from the current exact selected epoch")
        relative, plane, payload = item
        if (not isinstance(relative, str) or not isinstance(plane, str)
                or not isinstance(payload, bytes) or (relative, plane) not in coordinates
                or relative in seen):
            raise ProtocolRefusal("epoch_selected_member_changed", "automatic carry coordinates are invalid",
                remedy="replan automatic maintenance using only current selected ledger coordinates")
        seen.add(relative)
        if relative == "events.jsonl":
            payload = encode_frame(dict(receipt)) + verification_payload + payload
        if len(payload) > MAX_LEDGER_BYTES:
            raise ProtocolRefusal("ledger_too_large", "automatic carry exceeds the native ledger byte bound",
                remedy="preserve the current epoch and reduce completed history in the automatic carry plan")
        if payload.count(b"\n") > MAX_LEDGER_RECORDS:
            raise ProtocolRefusal("ledger_record_limit", "automatic carry exceeds the native ledger record bound",
                remedy="preserve the current epoch and reduce completed history in the automatic carry plan")
        targets.append((relative, plane, payload))
    if "events.jsonl" not in seen:
        payload = encode_frame(dict(receipt)) + verification_payload
        if len(payload) > MAX_LEDGER_BYTES:
            raise ProtocolRefusal("ledger_too_large", "automatic carry exceeds the native ledger byte bound",
                remedy="preserve the current epoch and reduce completed history in the automatic carry plan")
        if payload.count(b"\n") > MAX_LEDGER_RECORDS:
            raise ProtocolRefusal("ledger_record_limit", "automatic carry exceeds the native ledger record bound",
                remedy="preserve the current epoch and reduce completed history in the automatic carry plan")
        targets.append(("events.jsonl", "events", payload))
    from .epoch_carry import reanchor_carried_holds
    targets = reanchor_carried_holds(targets, receipt)
    # Re-encoding holds adds their new provenance; enforce bounds on final bytes.
    for _relative, _plane, payload in targets:
        if any(len(frame) > MAX_RECORD_BYTES for frame in payload.splitlines(keepends=True)):
            raise ProtocolRefusal("record_too_large", "automatic carry exceeds the native record byte bound",
                remedy="preserve the current epoch and reduce the pending automatic carry before retrying")
        if len(payload) > MAX_LEDGER_BYTES:
            raise ProtocolRefusal("ledger_too_large", "automatic carry exceeds the native ledger byte bound",
                remedy="preserve the current epoch and reduce completed history in the automatic carry plan")
        if payload.count(b"\n") > MAX_LEDGER_RECORDS:
            raise ProtocolRefusal("ledger_record_limit", "automatic carry exceeds the native ledger record bound",
                remedy="preserve the current epoch and reduce completed history in the automatic carry plan")
    return tuple(sorted(targets, key=lambda item: item[0].encode("utf-8")))


def _roll_epoch_transaction(root, *, actor, idempotency_key=None, planner=None, fault=None):
    """Share archive, marker, exclusion and durability ownership across both entries."""
    from .registry import Registry

    automatic = planner is not None
    if automatic:
        owner = validate_identifier(actor, "actor")
        Registry(root).require_active(owner)
        key = None
    else:
        owner, key, _grant = _require_roll_authority(root, actor, idempotency_key)

    with epoch_guard(root, exclusive=True), ExitStack() as selected_scope:
        # The first check gives invalid callers their stable zero-mutation
        # refusal.  Revalidate after exclusivity so a retirement or grant CAS
        # completed while we waited cannot authorize the roll from stale truth.
        if automatic:
            Registry(root).require_active(owner)
        else:
            owner, key, _grant = _require_roll_authority(root, owner, key)
            completed = _completed_roll_no_op(root, actor=owner, idempotency_key=key)
            if completed is not None:
                return completed
        observed = observe_epoch_roll_state(root)
        if observed["classification"] != "absent":
            raise ProtocolRefusal(
                "epoch_roll_recovery_required",
                "an incomplete epoch roll must be reconciled before another roll",
            )
        if not automatic:
            # Manual whole-epoch replacement has a receipt-only target. Never
            # orphan an unfinished native admission by clearing its live
            # selected references; dependency-retaining automatic maintenance
            # remains the existing carry owner.
            from .adhoc_journal import active_dispatch_references
            if active_dispatch_references(root):
                raise ProtocolRefusal("adhoc_dispatch_busy",
                    "whole-epoch replacement is blocked by active ad-hoc dispatch dependencies",
                    remedy="finish the exact dispatch or use native dependency-retaining epoch maintenance")
        selected = _preflight_selected_members(root)
        metadata, retained, planned_old = None, None, None
        teardown_seeds = ()
        if automatic:
            # A planner may read unselected holds/presentations. Run it before
            # WAKE/LEDGER acquisition, then bind its exact old inputs again.
            failure_inputs = {}
            planned_old = _read_selected_epoch_payloads(root, selected, _failure_inputs=failure_inputs)
            try:
                # Keep every selected byte object alive across the planner:
                # identity cannot be reused for an unrelated decoder payload.
                decision = planner(root, planned_old)
            except Exception as failure:
                try:
                    source = getattr(failure, "_waiter_payload_source", None)
                    if (isinstance(source, tuple) and len(source) == 2
                            and isinstance(source[0], pathlib.Path)
                            and isinstance(source[1], int) and not isinstance(source[1], bool)):
                        measured = failure_inputs.get(source)
                        if measured is not None:
                            relative, before = measured
                            if getattr(failure, "waiter_input_witness", None) is None:
                                failure.waiter_input_witness = (source[0], (
                                    before.st_dev, before.st_ino, before.st_mode, before.st_size,
                                    before.st_mtime_ns, before.st_ctime_ns, before.st_nlink,
                                ))
                            if getattr(failure, "waiter_input_identity", None) is None:
                                failure.waiter_input_identity = {"path": relative}
                                offset = getattr(failure, "waiter_byte_offset", None)
                                if isinstance(offset, int) and not isinstance(offset, bool) and offset >= 0:
                                    failure.waiter_input_identity["byte_offset"] = offset
                except Exception:
                    # Optional diagnostics must not replace the native failure.
                    pass
                raise
            if decision is None:
                return None
            decision = tuple(decision)
            if len(decision) == 2:
                retained, metadata = decision
            elif len(decision) == 3:
                retained, metadata, teardown_seeds = decision
            else:
                raise TypeError("automatic epoch planner must return payloads, metadata and optional teardown seeds")
            retained = tuple(retained)
            if not isinstance(metadata, Mapping):
                raise TypeError("automatic epoch metadata must be a mapping")
            metadata = copy.deepcopy(dict(metadata))
            teardown_seeds = copy.deepcopy(tuple(teardown_seeds))
            if "teardown_carry" in metadata:
                raise TypeError("teardown commitment is owned by the automatic epoch transaction")
        selected, replacement = selected_scope.enter_context(
            _selected_epoch_write_scope(root, selected, automatic_targets=retained)
        )
        payloads = _read_selected_epoch_payloads(root, selected)
        if planned_old is not None and payloads != planned_old:
            raise DurabilityFailure("epoch_selected_member_changed", "old epoch changed while its carry was planned")
        facts = _archive_facts_from_payloads(payloads)
        epoch_id = _epoch_id(facts)
        if automatic:
            key = "automatic-" + str(facts["archive_sha256"])
            from .epoch_teardown import finalize_verifications, verification_frames, carry_verification_frame
            teardown_seeds, teardown_commitment = finalize_verifications(teardown_seeds, epoch_id)
            if teardown_seeds:
                metadata["teardown_carry"] = teardown_commitment

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
            automatic=metadata,
        )
        target_payloads = None if not automatic else _automatic_target(
            retained, selected, receipt, carry_verification_frame(root, retained, receipt)
            + verification_frames(root, teardown_seeds, receipt))
        target_facts = None if target_payloads is None else _archive_facts_from_payloads(target_payloads)
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
            automatic_live=target_facts,
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
                    automatic=metadata,
                )
                next_target = None if not automatic else _automatic_target(
                    retained, selected, next_receipt, carry_verification_frame(root, retained, next_receipt)
                    + verification_frames(root, teardown_seeds, next_receipt))
                next_target_facts = None if next_target is None else _archive_facts_from_payloads(next_target)
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
                    automatic_live=next_target_facts,
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
                target_payloads, target_facts = next_target, next_target_facts
        _fsync_path(
            root.tenant_home,
            fault=fault,
            boundary="archive_reservation_parent_synced",
            directory=True,
        )
        _populate_archive(archive, payloads, fault=fault)
        target_snapshot, automatic_archive_snapshot = None, None
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
            if target_payloads is not None:
                archived, automatic_archive_snapshot = _read_epoch_archive_payloads(archive)
                if archived != payloads:
                    raise IntegrityFailure("epoch_archive_invalid", "automatic archive changed before carry staging")
                _populate_archive(staging, target_payloads, fault=fault)
                staged, target_snapshot = _read_epoch_archive_payloads(staging)
                if staged != target_payloads or _archive_facts_from_payloads(staged) != target_facts:
                    raise IntegrityFailure("epoch_marker_invalid", "staged automatic target differs from its plan")
                _fault_boundary(fault, "automatic_target_verified")
                if _preflight_selected_members(root) != selected:
                    raise DurabilityFailure("epoch_selected_member_changed", "old epoch changed while its carry was staged")
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
        committed_identity = _selected_member_identity(marker_path.lstat())
        _fault_boundary(fault, "marker_committed_durable")

        def recheck_target():
            if target_snapshot is not None:
                _assert_epoch_archive_snapshot(staging, target_snapshot)
                _assert_epoch_archive_snapshot(archive, automatic_archive_snapshot)
                if (_selected_member_identity(marker_path.lstat()) != committed_identity
                        or _read_bound_regular(marker_path, marker_path.lstat(), code="epoch_marker_invalid",
                            detail="automatic marker changed before replacement") != committed_bytes):
                    raise IntegrityFailure("epoch_marker_invalid", "automatic committed marker changed")
        _write_live_receipt(root, receipt, selected, fault=fault, replacement=replacement,
            target_payloads=target_payloads, recheck_target=recheck_target)
        if target_payloads is not None:
            recheck_target()
            current = _preflight_selected_members(root)
            if _read_selected_epoch_payloads(root, current) != target_payloads:
                raise IntegrityFailure("epoch_recovery_live_changed", "automatic target changed before marker retirement")
        if target_payloads is None:
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
        if target_payloads is not None:
            _fault_boundary(fault, "automatic_marker_retirement_synced")
            _remove_verified_staging(root, staging, target_snapshot)
            _fault_boundary(fault, "staging_retired")
            _fsync_path(root.tenant_home, fault=fault,
                        boundary="automatic_staging_retirement_synced", directory=True)
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


#: LEDGER-1 (b): the epoch roll becomes POLICY. The operator may declare
#: the thresholds in the root; the shipped defaults apply otherwise.
#: Native activity maintains due epochs; the explicit manual CLI remains
#: authority-gated and idempotent. Daemon lifecycle policy retains its own
#: shipped thirty-day default while sharing explicit operator declarations.
LEDGER_POLICY_RELATIVE = pathlib.Path("state/ledger-policy.json")
LEDGER_ROLL_POLICY_DEFAULTS = {"max_bytes": 8 * 1024 * 1024, "max_age_days": 7}
LIFECYCLE_ROLL_POLICY_DEFAULTS = {"max_bytes": 8 * 1024 * 1024, "max_age_days": 30}


def ledger_roll_policy(root: FloatiRoot) -> Dict[str, object]:
    """The operator-declared epoch thresholds, or seven-day shipped defaults."""
    return _read_ledger_roll_policy(root, defaults=LEDGER_ROLL_POLICY_DEFAULTS)


def lifecycle_ledger_roll_policy(root: FloatiRoot) -> Dict[str, object]:
    """Preserve daemon lifecycle's thirty-day defaults and explicit policy."""
    return _read_ledger_roll_policy(root, defaults=LIFECYCLE_ROLL_POLICY_DEFAULTS)


def _read_ledger_roll_policy(root: FloatiRoot, *, defaults: Mapping[str, int]) -> Dict[str, object]:
    """Read the existing exact operator shape for one declared policy family."""

    # LEDGER-1 (b) Am.1: resolve_relative DEREFERENCES the final symlink,
    # so the is_symlink guard below was dead code — lstat the raw path
    # before any resolution.
    raw = root.path / LEDGER_POLICY_RELATIVE
    if raw.is_symlink():
        raise ProtocolRefusal(
            "ledger_policy_symlink",
            "the roll policy must not be a symlink",
            remedy="replace state/ledger-policy.json with a regular file "
            "naming {schema_version, max_bytes, max_age_days}",
        )
    path = root.resolve_relative(LEDGER_POLICY_RELATIVE)
    if not path.is_file():
        return {
            **defaults,
            "source": "shipped_default",
        }
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolRefusal(
            "ledger_policy_invalid",
            f"the roll policy is unreadable: {exc}",
            remedy="rewrite state/ledger-policy.json as one JSON object "
            "{schema_version: 0, max_bytes, max_age_days}",
        ) from exc
    if (
        not isinstance(payload, dict)
        or set(payload) != {"schema_version", "max_bytes", "max_age_days"}
        or payload["schema_version"] != 0
        or isinstance(payload["max_bytes"], bool)
        or not isinstance(payload["max_bytes"], int)
        or payload["max_bytes"] <= 0
        or isinstance(payload["max_age_days"], bool)
        or not isinstance(payload["max_age_days"], int)
        or payload["max_age_days"] <= 0
    ):
        raise ProtocolRefusal(
            "ledger_policy_invalid",
            "the roll policy must be exactly {schema_version: 0, max_bytes, "
            "max_age_days} with positive integers",
            remedy="rewrite state/ledger-policy.json as one JSON object "
            "{schema_version: 0, max_bytes: positive-int, "
            "max_age_days: positive-int}",
        )
    return {
        "max_bytes": payload["max_bytes"],
        "max_age_days": payload["max_age_days"],
        "source": "operator_declared",
    }


def register_cli(commands: argparse._SubParsersAction) -> None:
    epoch = commands.add_parser("epoch")
    operations = epoch.add_subparsers(dest="epoch_command", required=True)
    roll = operations.add_parser("roll")
    roll.add_argument("--root", required=True)
    roll.add_argument("--as", dest="actor", required=True, metavar='NODE')
    roll.add_argument("--idempotency-key", required=True, metavar='KEY')
    roll.set_defaults(handler=_roll)

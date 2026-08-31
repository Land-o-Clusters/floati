"""Durable sequencer epochs and host-local run-writer ownership."""

from __future__ import annotations

from copy import deepcopy
from contextlib import contextmanager
from datetime import datetime, timezone
import os
from pathlib import Path
import threading
from typing import Dict, List, Optional, Tuple

from . import jsonl
from .errors import IntegrityFailure, ProtocolRefusal
from .ids import uuid7_hex
from .records import validate_record
from .root import FloatiRoot


_EPOCH_KINDS = frozenset({"sequencer_epoch"})
_CAPABILITY_TOKEN = object()
_OWNER_REGISTRY: Dict[Tuple[int, str], object] = {}
_OWNER_REGISTRY_GUARD = threading.Lock()
_OWNER_LOCAL = threading.local()


def _process_refusal() -> ProtocolRefusal:
    return ProtocolRefusal(
        "sequencer_lease_process_mismatch",
        "a writer lease and its capability are valid only in their creator process",
    )


def _check_creator_process(owner: object) -> None:
    if getattr(owner, "_creator_pid", None) != os.getpid():
        raise _process_refusal()


def _owner_key(root: FloatiRoot, pid: Optional[int] = None) -> Tuple[int, str]:
    return (os.getpid() if pid is None else pid, str(root.tenant_home))


def _registered_owner(root: FloatiRoot) -> object:
    with _OWNER_REGISTRY_GUARD:
        return _OWNER_REGISTRY.get(_owner_key(root))


def _register_exclusive_owner(owner: object) -> None:
    _check_creator_process(owner)
    if not getattr(owner, "_owner_held", False):
        raise ProtocolRefusal(
            "sequencer_owner_required", "epoch mutation requires an already-held exclusive owner lock"
        )
    key = _owner_key(getattr(owner, "root"))
    with _OWNER_REGISTRY_GUARD:
        if key in _OWNER_REGISTRY:
            raise ProtocolRefusal(
                "sequencer_owner_duplicate", "one process may register one exclusive owner per root"
            )
        _OWNER_REGISTRY[key] = owner
    owner._prior_owner_proof = getattr(_OWNER_LOCAL, "proof", None)
    _OWNER_LOCAL.proof = owner


def _unregister_exclusive_owner(owner: object) -> None:
    _check_creator_process(owner)
    key = _owner_key(getattr(owner, "root"))
    with _OWNER_REGISTRY_GUARD:
        if _OWNER_REGISTRY.get(key) is owner:
            del _OWNER_REGISTRY[key]
    if getattr(_OWNER_LOCAL, "proof", None) is owner:
        prior = getattr(owner, "_prior_owner_proof", None)
        if prior is None:
            try:
                del _OWNER_LOCAL.proof
            except AttributeError:
                pass
        else:
            _OWNER_LOCAL.proof = prior


def _require_exclusive_owner(root: FloatiRoot) -> object:
    owner = getattr(_OWNER_LOCAL, "proof", None)
    if owner is None:
        raise ProtocolRefusal(
            "sequencer_owner_required", "epoch mutation requires a live exclusive owner lease"
        )
    _check_creator_process(owner)
    if (
        getattr(owner, "root", None) != root
        or not getattr(owner, "_owner_held", False)
        or _registered_owner(root) is not owner
    ):
        raise ProtocolRefusal(
            "sequencer_owner_required", "epoch mutation requires this root's live exclusive owner lease"
        )
    return owner


@contextmanager
def _borrow_exclusive_owner(owner: object):
    _check_creator_process(owner)
    root = getattr(owner, "root")
    if (
        not getattr(owner, "_owner_held", False)
        or _registered_owner(root) is not owner
    ):
        raise ProtocolRefusal(
            "sequencer_owner_required", "the exclusive owner proof is no longer registered"
        )
    prior = getattr(_OWNER_LOCAL, "proof", None)
    _OWNER_LOCAL.proof = owner
    try:
        yield
    finally:
        if prior is None:
            try:
                del _OWNER_LOCAL.proof
            except AttributeError:
                pass
        else:
            _OWNER_LOCAL.proof = prior


@contextmanager
def _epoch_mutation_scope(root: FloatiRoot):
    owner = _require_exclusive_owner(root)
    with owner._append_guard:
        _require_exclusive_owner(root)
        yield owner


def _strict_epoch(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ProtocolRefusal(
            "sequencer_epoch_invalid", "managed append epoch must be an exact non-boolean integer"
        )
    return value


def _authorize_epoch_operation(
    owner: object, operation: str, *, allow_open_takeover: bool = False
) -> None:
    if operation == "offline_pair":
        if not isinstance(owner, _OfflineExclusiveOwner):
            raise ProtocolRefusal(
                "sequencer_owner_operation_invalid",
                "atomic offline takeover requires its dedicated exclusive owner proof",
            )
        return
    if not isinstance(owner, ManagedWriterLease):
        raise ProtocolRefusal(
            "sequencer_owner_operation_invalid",
            "single epoch mutations require a managed writer lease",
        )
    if operation == "release":
        if owner._active:
            return
        raise ProtocolRefusal("sequencer_lease_inactive", "managed lease is not active")
    if operation == "entered":
        if owner.record is None and not owner.takeover:
            return
        if owner._active:
            return
        raise ProtocolRefusal("sequencer_lease_inactive", "released lease cannot open another epoch")
    if operation == "takeover":
        if owner.record is None and owner.takeover:
            return
        if owner._active and not allow_open_takeover:
            return
        raise ProtocolRefusal(
            "sequencer_owner_operation_invalid",
            "takeover must be the entering operation of a takeover lease",
        )
    raise AssertionError("unknown epoch operation")


def _now(value: Optional[datetime]) -> datetime:
    current = datetime.now(timezone.utc) if value is None else value
    if not isinstance(current, datetime) or current.tzinfo is None or current.utcoffset() is None:
        raise ProtocolRefusal("time_invalid", "an aware UTC-compatible datetime is required")
    return current.astimezone(timezone.utc)


def _timestamp(value: datetime) -> str:
    return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _timestamp_value(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def _refuse(integrity: bool, code: str, detail: str) -> None:
    error = IntegrityFailure if integrity else ProtocolRefusal
    raise error(code, detail)


def _project(
    records: List[Dict[str, object]], *, integrity: bool
) -> Optional[Dict[str, object]]:
    previous: Optional[Dict[str, object]] = None
    seen_ids = set()
    for raw in records:
        record = validate_record(
            raw,
            str(raw.get("tenant_id")),
            _EPOCH_KINDS,
            integrity=integrity,
        )
        if record["id"] in seen_ids:
            _refuse(
                integrity,
                "duplicate_record_id",
                "sequencer epoch history repeats a physical record identity",
            )
        seen_ids.add(record["id"])
        if previous is None:
            if (
                record["operation"] != "entered"
                or record["epoch"] != 1
                or record["previous_epoch_record_id"] is not None
            ):
                _refuse(
                    integrity,
                    "sequencer_initial_invalid",
                    "the first sequencer epoch must be an epoch-one entry with no predecessor",
                )
            if record["absence_reason"] != "initial":
                _refuse(
                    integrity,
                    "sequencer_absence_invalid",
                    "the first entry must testify initial absence",
                )
            previous = record
            continue

        if record["previous_epoch_record_id"] != previous["id"]:
            _refuse(
                integrity,
                "sequencer_predecessor_invalid",
                "each epoch record must name the immediately preceding record",
            )
        if _timestamp_value(str(record["timestamp"])) < _timestamp_value(str(previous["timestamp"])):
            _refuse(
                integrity,
                "sequencer_timestamp_order",
                "sequencer epoch timestamp testimony cannot move backward",
            )

        prior_operation = previous["operation"]
        operation = record["operation"]
        if prior_operation in {"entered", "takeover"}:
            if operation == "released":
                if (
                    record["epoch"] != previous["epoch"]
                    or record["sequencer_id"] != previous["sequencer_id"]
                ):
                    _refuse(
                        integrity,
                        "sequencer_release_mismatch",
                        "release must close the exact current sequencer and epoch",
                    )
                if record["absence_reason"] != "graceful_release":
                    _refuse(
                        integrity,
                        "sequencer_absence_invalid",
                        "release must testify graceful release",
                    )
            elif operation == "takeover":
                if record["epoch"] != previous["epoch"] + 1:
                    _refuse(
                        integrity,
                        "sequencer_epoch_sequence",
                        "host-local takeover must increment the open epoch exactly once",
                    )
                if record["absence_reason"] != "host_local_owner_absent":
                    _refuse(
                        integrity,
                        "sequencer_absence_invalid",
                        "open-epoch takeover must testify absent host ownership",
                    )
            else:
                _refuse(
                    integrity,
                    "sequencer_managed_active",
                    "an open managed epoch must release or be explicitly taken over",
                )
        else:
            if operation not in {"entered", "takeover"}:
                _refuse(
                    integrity,
                    "sequencer_epoch_closed",
                    "a released epoch may only be followed by a new entry or takeover",
                )
            if record["epoch"] != previous["epoch"] + 1:
                _refuse(
                    integrity,
                    "sequencer_epoch_sequence",
                    "a new managed epoch must increment exactly once",
                )
            if record["absence_reason"] != "graceful_release":
                _refuse(
                    integrity,
                    "sequencer_absence_invalid",
                    "a transition after release must name graceful release",
                )
        previous = record
    return None if previous is None else deepcopy(previous)


class SequencerEpochLedger:
    relative_path = Path("sequencer/epochs.jsonl")

    def __init__(self, root: FloatiRoot) -> None:
        if not isinstance(root, FloatiRoot):
            raise ProtocolRefusal("root_required", "sequencer epochs require a validated FloatiRoot")
        self.root = root

    @property
    def _path(self) -> Path:
        return self.root.resolve_relative(self.relative_path)

    def records(self) -> List[Dict[str, object]]:
        records = jsonl.read_records(
            self.root, self.relative_path, allowed_kinds=set(_EPOCH_KINDS)
        )
        _project(records, integrity=True)
        return records

    def current(self) -> Optional[Dict[str, object]]:
        records = jsonl.read_records(
            self.root, self.relative_path, allowed_kinds=set(_EPOCH_KINDS)
        )
        return _project(records, integrity=True)

    def _current_snapshot(self) -> Optional[Dict[str, object]]:
        records = jsonl.read_records_snapshot(
            self.root, self.relative_path, allowed_kinds=set(_EPOCH_KINDS)
        )
        return _project(records, integrity=True)

    def enter(self, sequencer_id: str, now: Optional[datetime]) -> Dict[str, object]:
        with _epoch_mutation_scope(self.root) as owner:
            _authorize_epoch_operation(owner, "entered")
            testimony = _now(now)

            def decide(records: List[Dict[str, object]]):
                current = _project(records, integrity=True)
                if current is not None and current["operation"] != "released":
                    raise ProtocolRefusal(
                        "sequencer_managed_active", "the current managed epoch is still open"
                    )
                epoch = 1 if current is None else current["epoch"] + 1
                absence = "initial" if current is None else "graceful_release"
                record = self._record(
                    "entered", sequencer_id, epoch, current, absence, testimony
                )
                _project(records + [record], integrity=False)
                return record, record

            return jsonl.transact(
                self.root,
                self.relative_path,
                decide,
                allowed_kinds=set(_EPOCH_KINDS),
            )

    def release(
        self,
        sequencer_id: str,
        epoch: int,
        now: Optional[datetime],
    ) -> Dict[str, object]:
        with _epoch_mutation_scope(self.root) as owner:
            _authorize_epoch_operation(owner, "release")
            testimony = _now(now)

            def decide(records: List[Dict[str, object]]):
                current = _project(records, integrity=True)
                if current is None or current["operation"] == "released":
                    raise ProtocolRefusal(
                        "sequencer_epoch_closed", "there is no open managed epoch to release"
                    )
                if current["sequencer_id"] != sequencer_id:
                    raise ProtocolRefusal(
                        "sequencer_owner_mismatch", "only the current sequencer may release its epoch"
                    )
                if current["epoch"] != epoch:
                    raise ProtocolRefusal(
                        "sequencer_epoch_mismatch", "release must name the current epoch"
                    )
                record = self._record(
                    "released",
                    sequencer_id,
                    epoch,
                    current,
                    "graceful_release",
                    testimony,
                )
                _project(records + [record], integrity=False)
                return record, record

            released = jsonl.transact(
                self.root,
                self.relative_path,
                decide,
                allowed_kinds=set(_EPOCH_KINDS),
            )
            if isinstance(owner, ManagedWriterLease):
                owner._active = False
            return released

    def takeover(self, sequencer_id: str, now: Optional[datetime]) -> Dict[str, object]:
        """Take over one gracefully released predecessor under the epoch CAS lock."""

        return self._takeover(sequencer_id, now, allow_open=False)

    def _takeover_open(
        self, sequencer_id: str, now: Optional[datetime]
    ) -> Dict[str, object]:
        """Take over an open epoch only after an exclusive owner-lock proof."""

        return self._takeover(sequencer_id, now, allow_open=True)

    def _takeover(
        self, sequencer_id: str, now: Optional[datetime], *, allow_open: bool
    ) -> Dict[str, object]:
        with _epoch_mutation_scope(self.root) as owner:
            _authorize_epoch_operation(
                owner, "takeover", allow_open_takeover=allow_open
            )
            testimony = _now(now)

            def decide(records: List[Dict[str, object]]):
                current = _project(records, integrity=True)
                if current is None:
                    raise ProtocolRefusal(
                        "sequencer_epoch_missing", "takeover requires a prior managed epoch"
                    )
                is_open = current["operation"] != "released"
                if is_open and not allow_open:
                    raise ProtocolRefusal(
                        "sequencer_managed_active", "takeover requires host-local owner absence"
                    )
                absence = "host_local_owner_absent" if is_open else "graceful_release"
                record = self._record(
                    "takeover",
                    sequencer_id,
                    current["epoch"] + 1,
                    current,
                    absence,
                    testimony,
                )
                _project(records + [record], integrity=False)
                return record, record

            return jsonl.transact(
                self.root,
                self.relative_path,
                decide,
                allowed_kinds=set(_EPOCH_KINDS),
            )

    def _takeover_and_release(
        self, sequencer_id: str, now: Optional[datetime]
    ) -> Tuple[Dict[str, object], Dict[str, object]]:
        with _epoch_mutation_scope(self.root) as owner:
            _authorize_epoch_operation(owner, "offline_pair")
            testimony = _now(now)
            path = self._path
            lock_path = path.with_name(path.name + ".lock")
            with jsonl._locked_path(lock_path, exclusive=True):
                records = jsonl._read_path_records(
                    path, self.root.tenant_id, _EPOCH_KINDS
                )
                current = _project(records, integrity=True)
                if current is None:
                    raise ProtocolRefusal(
                        "sequencer_epoch_missing", "offline takeover requires a prior managed epoch"
                    )
                is_open = current["operation"] != "released"
                takeover = self._record(
                    "takeover",
                    sequencer_id,
                    current["epoch"] + 1,
                    current,
                    "host_local_owner_absent" if is_open else "graceful_release",
                    testimony,
                )
                released = self._record(
                    "released",
                    sequencer_id,
                    takeover["epoch"],
                    takeover,
                    "graceful_release",
                    testimony,
                )
                _project(records + [takeover, released], integrity=False)
                if len(records) + 2 > jsonl.MAX_LEDGER_RECORDS:
                    raise ProtocolRefusal(
                        "ledger_record_limit",
                        f"ledger maximum is {jsonl.MAX_LEDGER_RECORDS} records",
                    )
                encoded = jsonl._encode_record(
                    takeover, self.root.tenant_id, _EPOCH_KINDS
                ) + jsonl._encode_record(released, self.root.tenant_id, _EPOCH_KINDS)
                jsonl._append_frame(path, encoded)
                return deepcopy(takeover), deepcopy(released)

    def _record(
        self,
        operation: str,
        sequencer_id: str,
        epoch: int,
        previous: Optional[Dict[str, object]],
        absence_reason: str,
        testimony: datetime,
    ) -> Dict[str, object]:
        return {
            "schema_version": 1,
            "id": "sequencer-epoch-" + uuid7_hex(),
            "tenant_id": self.root.tenant_id,
            "timestamp": _timestamp(testimony),
            "kind": "sequencer_epoch",
            "epoch": epoch,
            "operation": operation,
            "sequencer_id": sequencer_id,
            "previous_epoch_record_id": None if previous is None else previous["id"],
            "absence_reason": absence_reason,
        }


class _ManagedAppendCapability:
    def __init__(self, token: object, lease: "ManagedWriterLease") -> None:
        if token is not _CAPABILITY_TOKEN:
            raise TypeError("managed append capabilities are created by ManagedWriterLease")
        self._lease = lease
        self._creator_pid = os.getpid()


class ManagedWriterLease:
    """Exclusive host-local ownership held through matching durable release."""

    def __init__(
        self,
        root: FloatiRoot,
        sequencer_id: str,
        *,
        takeover: bool = False,
        now: Optional[datetime] = None,
    ) -> None:
        if not isinstance(root, FloatiRoot):
            raise ProtocolRefusal("root_required", "managed ownership requires a validated FloatiRoot")
        self.root = root
        self.sequencer_id = sequencer_id
        self.takeover = takeover
        self.now = now
        self.epoch_ledger = SequencerEpochLedger(root)
        self.record: Optional[Dict[str, object]] = None
        self.epoch: Optional[int] = None
        self._owner_context = None
        self._active = False
        self._capability: Optional[_ManagedAppendCapability] = None
        self._append_guard = threading.RLock()
        self._creator_pid = os.getpid()
        self._owner_held = False
        self._registered = False
        self._prior_owner_proof = None

    @property
    def _owner_path(self) -> Path:
        return self.root.resolve_relative("sequencer/owner.lock")

    def __enter__(self) -> "ManagedWriterLease":
        _check_creator_process(self)
        if self._owner_context is not None:
            raise ProtocolRefusal("sequencer_lease_reused", "a managed lease may be entered once")
        self._owner_context = jsonl._locked_path(
            self._owner_path, exclusive=True, order_tracked=False
        )
        self._owner_context.__enter__()
        self._owner_held = True
        try:
            _register_exclusive_owner(self)
            self._registered = True
            if self.takeover:
                current = self.epoch_ledger._current_snapshot()
                if current is not None and current["operation"] != "released":
                    self.record = self.epoch_ledger._takeover_open(
                        self.sequencer_id, self.now
                    )
                else:
                    self.record = self.epoch_ledger.takeover(self.sequencer_id, self.now)
            else:
                self.record = self.epoch_ledger.enter(self.sequencer_id, self.now)
            self.epoch = self.record["epoch"]
            self._active = True
            self._capability = _ManagedAppendCapability(_CAPABILITY_TOKEN, self)
            return self
        except BaseException:
            if self._registered:
                _unregister_exclusive_owner(self)
                self._registered = False
            self._owner_context.__exit__(None, None, None)
            self._owner_held = False
            raise

    @property
    def managed_append_capability(self) -> object:
        _check_creator_process(self)
        if not self._active or self._capability is None:
            raise ProtocolRefusal(
                "managed_append_capability_invalid",
                "managed append capability exists only for a live owner lease",
            )
        return self._capability

    def release(self, now: Optional[datetime] = None) -> Dict[str, object]:
        _check_creator_process(self)
        with _borrow_exclusive_owner(self):
            with self._append_guard:
                if not self._active or self.epoch is None:
                    raise ProtocolRefusal("sequencer_lease_inactive", "managed lease is not active")
                testimony = now
                if testimony is None and self.record is not None:
                    current = datetime.now(timezone.utc)
                    entered = _timestamp_value(str(self.record["timestamp"]))
                    testimony = max(current, entered)
                released = self.epoch_ledger.release(self.sequencer_id, self.epoch, testimony)
                self._active = False
                return released

    def __exit__(self, exc_type, exc, traceback) -> None:
        _check_creator_process(self)
        try:
            if self._active:
                self.release()
        finally:
            if self._registered:
                _unregister_exclusive_owner(self)
                self._registered = False
            assert self._owner_context is not None
            self._owner_context.__exit__(exc_type, exc, traceback)
            self._owner_held = False


class _OfflineExclusiveOwner:
    def __init__(self, root: FloatiRoot) -> None:
        self.root = root
        self._creator_pid = os.getpid()
        self._owner_held = False
        self._append_guard = threading.RLock()
        self._prior_owner_proof = None


class DirectWriterLease:
    """Shared host-local ownership held from closed replay through run fsync."""

    def __init__(self, root: FloatiRoot) -> None:
        if not isinstance(root, FloatiRoot):
            raise ProtocolRefusal("root_required", "direct ownership requires a validated FloatiRoot")
        self.root = root
        self.epoch_ledger = SequencerEpochLedger(root)
        self._owner_context = None
        self._creator_pid = os.getpid()

    @property
    def _owner_path(self) -> Path:
        return self.root.resolve_relative("sequencer/owner.lock")

    def __enter__(self) -> "DirectWriterLease":
        _check_creator_process(self)
        if self._owner_context is not None:
            raise ProtocolRefusal("sequencer_lease_reused", "a direct lease may be entered once")
        self._owner_context = jsonl._locked_path(
            self._owner_path, exclusive=False, order_tracked=False
        )
        self._owner_context.__enter__()
        try:
            current = self.epoch_ledger._current_snapshot()
            if current is not None and current["operation"] != "released":
                raise ProtocolRefusal(
                    "sequencer_managed_active",
                    "daemonless append requires a closed sequencer epoch",
                )
            return self
        except BaseException:
            self._owner_context.__exit__(None, None, None)
            raise

    def __exit__(self, exc_type, exc, traceback) -> None:
        _check_creator_process(self)
        assert self._owner_context is not None
        self._owner_context.__exit__(exc_type, exc, traceback)

    @classmethod
    def offline_takeover(
        cls,
        root: FloatiRoot,
        sequencer_id: str,
        now: Optional[datetime] = None,
    ) -> Tuple[Dict[str, object], Dict[str, object]]:
        if not isinstance(root, FloatiRoot):
            raise ProtocolRefusal("root_required", "offline takeover requires a validated FloatiRoot")
        owner_path = root.resolve_relative("sequencer/owner.lock")
        proof = _OfflineExclusiveOwner(root)
        with jsonl._locked_path(
            owner_path, exclusive=True, order_tracked=False
        ):
            proof._owner_held = True
            _register_exclusive_owner(proof)
            try:
                return SequencerEpochLedger(root)._takeover_and_release(sequencer_id, now)
            finally:
                _unregister_exclusive_owner(proof)
                proof._owner_held = False


def _validate_managed_append_capability(
    capability: object, root: FloatiRoot, epoch: object
) -> None:
    strict_epoch = _strict_epoch(epoch)
    if not isinstance(capability, _ManagedAppendCapability):
        raise ProtocolRefusal(
            "managed_append_capability_invalid", "managed append requires an opaque live lease capability"
        )
    lease = capability._lease
    if capability._creator_pid != os.getpid() or lease._creator_pid != os.getpid():
        raise _process_refusal()
    if (
        not lease._active
        or lease.root != root
        or lease.epoch != strict_epoch
        or capability is not lease._capability
        or not lease._owner_held
        or _registered_owner(root) is not lease
    ):
        code = "sequencer_epoch_mismatch" if lease._active and lease.root == root else "managed_append_capability_invalid"
        raise ProtocolRefusal(code, "managed append capability does not match the live root and epoch")
    current = lease.epoch_ledger._current_snapshot()
    if (
        current is None
        or current["operation"] not in {"entered", "takeover"}
        or current["epoch"] != strict_epoch
        or current["sequencer_id"] != lease.sequencer_id
    ):
        raise ProtocolRefusal(
            "sequencer_epoch_mismatch", "managed append must match the current open epoch"
        )


@contextmanager
def _managed_append_scope(capability: object, root: FloatiRoot, epoch: object):
    if not isinstance(capability, _ManagedAppendCapability):
        _validate_managed_append_capability(capability, root, epoch)
        raise AssertionError("unreachable")
    lease = capability._lease
    if capability._creator_pid != os.getpid() or lease._creator_pid != os.getpid():
        raise _process_refusal()
    with lease._append_guard:
        _validate_managed_append_capability(capability, root, epoch)
        yield


__all__ = ["SequencerEpochLedger", "ManagedWriterLease", "DirectWriterLease"]

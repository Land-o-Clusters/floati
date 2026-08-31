"""Separated liveness, authority, and mutual-exclusion state machines."""

from __future__ import annotations

import fcntl
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Iterator, Optional

from .bus_epoch import shared_epoch_operation
from .errors import ProtocolRefusal
from .ids import uuid7_hex
from .jsonl import LOCK_POLL_SECONDS, LOCK_TIMEOUT_SECONDS, append_record, read_records, read_records_snapshot, transact
from .root import FloatiRoot, validate_identifier


MAX_TTL_SECONDS = 86400


def _as_utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ProtocolRefusal("time_invalid", "an aware UTC-compatible datetime is required")
    return value.astimezone(timezone.utc)


def _format_time(value: datetime) -> str:
    return _as_utc(value).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def _validate_ttl(ttl_seconds: int) -> int:
    if not isinstance(ttl_seconds, int) or isinstance(ttl_seconds, bool) or not 1 <= ttl_seconds <= MAX_TTL_SECONDS:
        raise ProtocolRefusal("ttl_invalid", f"TTL must be 1 through {MAX_TTL_SECONDS} seconds")
    return ttl_seconds


def _validate_interval(ttl_seconds: int, deadline_seconds: int) -> None:
    ttl = _validate_ttl(ttl_seconds)
    if (
        not isinstance(deadline_seconds, int)
        or isinstance(deadline_seconds, bool)
        or deadline_seconds < 1
        or deadline_seconds > MAX_TTL_SECONDS
    ):
        raise ProtocolRefusal("deadline_invalid", f"deadline must be 1 through {MAX_TTL_SECONDS} seconds")
    if deadline_seconds > ttl:
        raise ProtocolRefusal("deadline_exceeds_ttl", "deadline must be less than or equal to TTL")


@contextmanager
def _cas_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(path.name + ".cas.lock")
    with lock_path.open("a+b") as handle:
        deadline = time.monotonic() + LOCK_TIMEOUT_SECONDS
        while True:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError as exc:
                if time.monotonic() >= deadline:
                    raise ProtocolRefusal(
                        "cas_lock_timeout",
                        f"{path.name} CAS lock remained contended for {LOCK_TIMEOUT_SECONDS:g} second",
                    ) from exc
                time.sleep(LOCK_POLL_SECONDS)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def _existing_cas_read_lock(path: Path) -> Iterator[None]:
    """Share an existing CAS lock without creating observational state."""
    lock_path = path.with_name(path.name + ".cas.lock")
    if not lock_path.exists():
        raise ProtocolRefusal(
            "authority_lock_missing",
            "existing authority evidence requires its canonical CAS lock",
        )
    try:
        handle = lock_path.open("rb")
    except FileNotFoundError as exc:
        raise ProtocolRefusal(
            "authority_lock_missing",
            "existing authority evidence requires its canonical CAS lock",
        ) from exc
    with handle:
        deadline = time.monotonic() + LOCK_TIMEOUT_SECONDS
        while True:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_SH | fcntl.LOCK_NB)
                break
            except BlockingIOError as exc:
                if time.monotonic() >= deadline:
                    raise ProtocolRefusal(
                        "cas_lock_timeout",
                        f"{path.name} CAS lock remained contended for {LOCK_TIMEOUT_SECONDS:g} second",
                    ) from exc
                time.sleep(LOCK_POLL_SECONDS)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class LivenessPresenceStore:
    """Heartbeat evidence only; it grants neither authority nor exclusion."""

    def __init__(self, root: FloatiRoot) -> None:
        self.root = root

    def path_for(self, node_id: str) -> Path:
        node = validate_identifier(node_id, "node")
        return self.root.tenant_home / "liveness-presence" / f"{node}.jsonl"

    def observe(self, node_id: str, ttl_seconds: int, now: datetime) -> Dict[str, object]:
        node = validate_identifier(node_id, "node")
        ttl = _validate_ttl(ttl_seconds)
        observed = _as_utc(now)
        record: Dict[str, object] = {
            "schema_version": 0,
            "id": "presence-" + uuid7_hex(),
            "tenant_id": self.root.tenant_id,
            "timestamp": _format_time(observed),
            "kind": "liveness_presence",
            "node_id": node,
            "observed_at": _format_time(observed),
            "expires_at": _format_time(observed + timedelta(seconds=ttl)),
            "state": "present",
        }
        relative = Path("liveness-presence") / f"{node}.jsonl"
        def decide(records: list[Dict[str, object]]) -> tuple[Dict[str, object], Dict[str, object]]:
            if records and observed < _parse_time(records[-1]["observed_at"]):
                raise ProtocolRefusal("time_regression", "liveness observation cannot move backward")
            return record, record
        return transact(self.root, relative, decide, allowed_kinds={"liveness_presence"})

    def status(self, node_id: str, now: datetime) -> str:
        node = validate_identifier(node_id, "node")
        records = read_records(self.root, Path("liveness-presence") / f"{node}.jsonl", allowed_kinds={"liveness_presence"})
        if not records:
            raise ProtocolRefusal("liveness_unknown", f"no liveness evidence exists for {node_id}")
        latest = records[-1]
        observed = _parse_time(latest["observed_at"])
        expires = _parse_time(latest["expires_at"])
        current = _as_utc(now)
        if current >= expires:
            return "expired"
        if current >= observed + (expires - observed) / 2:
            return "silent"
        return "present"


class _CasIntervalStore:
    def __init__(
        self,
        root: FloatiRoot,
        directory: str,
        kind: str,
        subject_field: str,
        id_prefix: str,
        claim_time_field: str,
        held_code: str,
        missing_code: str,
        expired_code: str,
        released_code: str,
    ) -> None:
        self.root = root
        self.directory = directory
        self.kind = kind
        self.subject_field = subject_field
        self.id_prefix = id_prefix
        self.claim_time_field = claim_time_field
        self.held_code = held_code
        self.missing_code = missing_code
        self.expired_code = expired_code
        self.released_code = released_code

    def path_for(self, subject_id: str) -> Path:
        subject = validate_identifier(subject_id, self.subject_field.removesuffix("_id"))
        return self.root.tenant_home / self.directory / f"{subject}.jsonl"

    def _latest(self, path: Path) -> Optional[Dict[str, object]]:
        relative = path.relative_to(self.root.tenant_home)
        records = read_records(self.root, relative, allowed_kinds={self.kind})
        return records[-1] if records else None

    def claim(
        self,
        subject_id: str,
        holder: str,
        ttl_seconds: int,
        deadline_seconds: int,
        now: datetime,
    ) -> Dict[str, object]:
        subject = validate_identifier(subject_id, self.subject_field.removesuffix("_id"))
        owner = validate_identifier(holder, "holder")
        _validate_interval(ttl_seconds, deadline_seconds)
        current = _as_utc(now)
        path = self.path_for(subject)
        with _cas_lock(path):
            prior = self._latest(path)
            if prior is not None and current < _parse_time(prior["timestamp"]):
                raise ProtocolRefusal("time_regression", "claim time cannot precede current evidence")
            if (
                prior is not None
                and prior.get("state") == "active"
                and current < _parse_time(prior["expires_at"])
            ):
                raise ProtocolRefusal(self.held_code, f"{subject} already has an active holder")
            epoch = int(prior["epoch"]) + 1 if prior is not None else 1
            record = self._record(
                subject,
                owner,
                epoch,
                current,
                current,
                ttl_seconds,
                deadline_seconds,
                "active",
                None,
            )
            append_record(self.root, path.relative_to(self.root.tenant_home), record, allowed_kinds={self.kind})
            return record

    def renew(
        self,
        subject_id: str,
        holder: str,
        epoch: int,
        ttl_seconds: int,
        deadline_seconds: int,
        now: datetime,
    ) -> Dict[str, object]:
        subject = validate_identifier(subject_id, self.subject_field.removesuffix("_id"))
        owner = validate_identifier(holder, "holder")
        _validate_interval(ttl_seconds, deadline_seconds)
        current = _as_utc(now)
        path = self.path_for(subject)
        with _cas_lock(path):
            prior = self._require_current(path, owner, epoch, current)
            claimed = _parse_time(prior[self.claim_time_field])
            record = self._record(
                subject,
                owner,
                epoch,
                claimed,
                current,
                ttl_seconds,
                deadline_seconds,
                "active",
                None,
            )
            append_record(self.root, path.relative_to(self.root.tenant_home), record, allowed_kinds={self.kind})
            return record

    def release(self, subject_id: str, holder: str, epoch: int, now: datetime) -> Dict[str, object]:
        subject = validate_identifier(subject_id, self.subject_field.removesuffix("_id"))
        owner = validate_identifier(holder, "holder")
        current = _as_utc(now)
        path = self.path_for(subject)
        with _cas_lock(path):
            prior = self._require_current(path, owner, epoch, current)
            record = self._record(
                subject,
                owner,
                epoch,
                _parse_time(prior[self.claim_time_field]),
                _parse_time(prior["renewed_at"]),
                int(prior["ttl_seconds"]),
                int(prior["deadline_seconds"]),
                "released",
                current,
            )
            append_record(self.root, path.relative_to(self.root.tenant_home), record, allowed_kinds={self.kind})
            return record

    def expire(self, subject_id: str, holder: str, epoch: int, now: datetime) -> Dict[str, object]:
        subject = validate_identifier(subject_id, self.subject_field.removesuffix("_id"))
        owner = validate_identifier(holder, "holder")
        current = _as_utc(now)
        path = self.path_for(subject)
        with _cas_lock(path):
            prior = self._require_current(path, owner, epoch, current)
            record: Dict[str, object] = {
                "schema_version": 0,
                "id": self.id_prefix + uuid7_hex(),
                "tenant_id": self.root.tenant_id,
                "timestamp": _format_time(current),
                "kind": self.kind,
                self.subject_field: subject,
                "holder": owner,
                "epoch": epoch,
                self.claim_time_field: prior[self.claim_time_field],
                "renewed_at": prior["renewed_at"],
                "expires_at": _format_time(current),
                "released_at": None,
                "ttl_seconds": prior["ttl_seconds"],
                "deadline_seconds": prior["deadline_seconds"],
                "state": "expired",
            }
            append_record(
                self.root,
                path.relative_to(self.root.tenant_home),
                record,
                allowed_kinds={self.kind},
            )
            return record

    def _require_current(
        self,
        path: Path,
        holder: str,
        epoch: int,
        now: datetime,
    ) -> Dict[str, object]:
        prior = self._latest(path)
        if prior is None:
            raise ProtocolRefusal(self.missing_code, "no current record exists")
        if not isinstance(epoch, int) or isinstance(epoch, bool) or epoch < 1:
            raise ProtocolRefusal("epoch_invalid", "epoch must be a positive integer")
        if prior.get("epoch") != epoch:
            raise ProtocolRefusal("epoch_mismatch", "epoch does not match current evidence")
        if prior.get("holder") != holder:
            raise ProtocolRefusal("holder_mismatch", "holder does not match current evidence")
        if prior.get("state") != "active":
            raise ProtocolRefusal(self.released_code, "the current epoch has been released")
        if now < _parse_time(prior["renewed_at"]):
            raise ProtocolRefusal("time_regression", "operation time cannot precede current evidence")
        if now >= _parse_time(prior["expires_at"]):
            raise ProtocolRefusal(self.expired_code, "the current epoch has expired")
        return prior

    def _record(
        self,
        subject_id: str,
        holder: str,
        epoch: int,
        claimed_at: datetime,
        renewed_at: datetime,
        ttl_seconds: int,
        deadline_seconds: int,
        state: str,
        released_at: Optional[datetime],
    ) -> Dict[str, object]:
        expires_at = released_at or (renewed_at + timedelta(seconds=ttl_seconds))
        record: Dict[str, object] = {
            "schema_version": 0,
            "id": self.id_prefix + uuid7_hex(),
            "tenant_id": self.root.tenant_id,
            "timestamp": _format_time(released_at or renewed_at),
            "kind": self.kind,
            self.subject_field: subject_id,
            "holder": holder,
            "epoch": epoch,
            self.claim_time_field: _format_time(claimed_at),
            "renewed_at": _format_time(renewed_at),
            "expires_at": _format_time(expires_at),
            "released_at": _format_time(released_at) if released_at is not None else None,
            "ttl_seconds": ttl_seconds,
            "deadline_seconds": deadline_seconds,
            "state": state,
        }
        return record


class AuthorityGrantStore:
    """Expiring acting authority with exact-holder and exact-epoch CAS."""

    def __init__(self, root: FloatiRoot) -> None:
        self.root = root
        self._store = _CasIntervalStore(
            root,
            "authority-grants",
            "authority_grant",
            "subject_id",
            "authority-",
            "claimed_at",
            "authority_held",
            "authority_missing",
            "authority_expired",
            "authority_released",
        )

    def path_for(self, subject_id: str) -> Path:
        return self._store.path_for(subject_id)

    def exact_tail(self, subject_id: str) -> Dict[str, object]:
        """Return copied physical tail evidence without evaluating clocks or writing."""
        path = self._store.path_for(subject_id)
        relative = path.relative_to(self._store.root.tenant_home)
        cas_lock = path.with_name(path.name + ".cas.lock")
        if not path.exists() and not cas_lock.exists():
            raise ProtocolRefusal(
                "authority_missing", "no current authority record exists"
            )
        with _existing_cas_read_lock(path):
            records = read_records_snapshot(
                self._store.root, relative, allowed_kinds={self._store.kind}
            )
        if not records:
            raise ProtocolRefusal(
                "authority_missing", "no current authority record exists"
            )
        return dict(records[-1])

    @shared_epoch_operation
    def claim(self, subject_id: str, holder: str, ttl_seconds: int, deadline_seconds: int, now: datetime) -> Dict[str, object]:
        return self._store.claim(subject_id, holder, ttl_seconds, deadline_seconds, now)

    @shared_epoch_operation
    def grant_exact(
        self,
        subject_id: str,
        holder: str,
        epoch: int,
        now: datetime,
    ) -> Dict[str, object]:
        """Append or replay one exact public grant coordinate."""

        subject = validate_identifier(subject_id, "subject")
        owner = validate_identifier(holder, "holder")
        if not isinstance(epoch, int) or isinstance(epoch, bool) or epoch < 1:
            raise ProtocolRefusal(
                "authority_epoch_invalid",
                "authority epoch must be a positive integer",
            )
        current = _as_utc(now)
        path = self._store.path_for(subject)
        with _cas_lock(path):
            prior = self._store._latest(path)
            if prior is not None and current < _parse_time(str(prior["timestamp"])):
                raise ProtocolRefusal(
                    "time_regression",
                    "grant time cannot precede current authority evidence",
                )
            if (
                prior is not None
                and prior.get("state") == "active"
                and prior.get("holder") == owner
                and prior.get("epoch") == epoch
                and current < _parse_time(str(prior["expires_at"]))
            ):
                return dict(prior)
            expected_epoch = 1 if prior is None else int(prior["epoch"]) + 1
            if epoch != expected_epoch:
                raise ProtocolRefusal(
                    "authority_epoch_mismatch",
                    f"requested epoch={epoch}; next epoch={expected_epoch} "
                    f"for (holder={owner}, subject={subject})",
                )
            record = self._store._record(
                subject,
                owner,
                epoch,
                current,
                current,
                MAX_TTL_SECONDS,
                MAX_TTL_SECONDS,
                "active",
                None,
            )
            append_record(
                self._store.root,
                path.relative_to(self._store.root.tenant_home),
                record,
                allowed_kinds={self._store.kind},
            )
            return record

    @shared_epoch_operation
    def revoke_exact(
        self,
        subject_id: str,
        holder: str,
        epoch: int,
        now: datetime,
    ) -> Dict[str, object]:
        """Release one exact coordinate; exact repeated revocation is replay."""

        try:
            return self.release(subject_id, holder, epoch, now)
        except ProtocolRefusal as exc:
            if exc.code != "authority_released":
                raise
            prior = self.exact_tail(subject_id)
            if (
                prior.get("state") == "released"
                and prior.get("holder") == holder
                and prior.get("epoch") == epoch
            ):
                return prior
            raise

    @shared_epoch_operation
    def renew(self, subject_id: str, holder: str, epoch: int, ttl_seconds: int, deadline_seconds: int, now: datetime) -> Dict[str, object]:
        return self._store.renew(subject_id, holder, epoch, ttl_seconds, deadline_seconds, now)

    @shared_epoch_operation
    def release(self, subject_id: str, holder: str, epoch: int, now: datetime) -> Dict[str, object]:
        return self._store.release(subject_id, holder, epoch, now)

    @shared_epoch_operation
    def expire(self, subject_id: str, holder: str, epoch: int, now: datetime) -> Dict[str, object]:
        return self._store.expire(subject_id, holder, epoch, now)


class MutualExclusionHoldStore:
    """Single-writer exclusion evidence, independent of liveness and authority."""

    def __init__(self, root: FloatiRoot) -> None:
        self._store = _CasIntervalStore(
            root,
            "mutual-exclusion-holds",
            "mutual_exclusion_hold",
            "resource_id",
            "hold-",
            "acquired_at",
            "exclusion_held",
            "exclusion_missing",
            "exclusion_expired",
            "exclusion_released",
        )

    def path_for(self, resource_id: str) -> Path:
        return self._store.path_for(resource_id)

    def acquire(self, resource_id: str, holder: str, ttl_seconds: int, deadline_seconds: int, now: datetime) -> Dict[str, object]:
        return self._store.claim(resource_id, holder, ttl_seconds, deadline_seconds, now)

    def renew(self, resource_id: str, holder: str, epoch: int, ttl_seconds: int, deadline_seconds: int, now: datetime) -> Dict[str, object]:
        return self._store.renew(resource_id, holder, epoch, ttl_seconds, deadline_seconds, now)

    def release(self, resource_id: str, holder: str, epoch: int, now: datetime) -> Dict[str, object]:
        return self._store.release(resource_id, holder, epoch, now)

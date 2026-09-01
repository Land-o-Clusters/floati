"""Secret-free, append-only credential lease lifecycle."""

from __future__ import annotations

import re
import os
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from .errors import IntegrityFailure, ProtocolRefusal
from .ids import uuid7_hex
from .jsonl import read_records, transact, transact_records
from .planes import AuthorityGrantStore, MAX_TTL_SECONDS
from .policy import RepositoryPolicy, validate_repository_policy_integrity
from .root import FloatiRoot, IDENTIFIER_PATTERN
from .runtruth import RunLedger


CREDENTIAL_LEASE_KINDS = frozenset({
    "credential_lease_granted",
    "credential_lease_consumed",
    "credential_lease_revoked",
})
DELIVERY_MODES = frozenset({"inherited_fd", "helper_response"})
_UUID7_HEX = r"[0-9a-f]{12}7[0-9a-f]{3}[89ab][0-9a-f]{15}"
_ATTEMPT_ID = re.compile(r"attempt-" + _UUID7_HEX)
_AUTHORITY_RECORD_ID = re.compile(r"authority-" + _UUID7_HEX)
_CAPABILITY_SET_ID = re.compile(r"capability-set-bound-" + _UUID7_HEX)
_CAPABILITY = re.compile(r"[a-z0-9](?:[a-z0-9._-]{0,126}[a-z0-9])?")
MAX_HELPER_RESPONSE_BYTES = 8192


def _now(value: Optional[datetime]) -> datetime:
    current = datetime.now(timezone.utc) if value is None else value
    if (
        not isinstance(current, datetime)
        or current.tzinfo is None
        or current.utcoffset() is None
    ):
        raise ProtocolRefusal(
            "time_invalid", "credential leases require an aware datetime"
        )
    return current.astimezone(timezone.utc)


def _timestamp(value: datetime) -> str:
    return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _parse_time(value: object) -> datetime:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(
            timezone.utc
        )
    except (TypeError, ValueError) as exc:
        raise IntegrityFailure(
            "credential_lease_time_invalid",
            "persisted credential lease time is invalid",
        ) from exc


def _binding(
    attempt_id: object,
    principal: object,
    capability: object,
    secret_alias: object,
    ttl_seconds: object,
    authority_epoch: object,
    authority_record_id: object,
    capability_set_bound_id: object,
) -> Tuple[str, str, str, str, int, int, str, str]:
    valid = (
        isinstance(attempt_id, str)
        and _ATTEMPT_ID.fullmatch(attempt_id) is not None
        and isinstance(principal, str)
        and IDENTIFIER_PATTERN.fullmatch(principal) is not None
        and isinstance(capability, str)
        and _CAPABILITY.fullmatch(capability) is not None
        and isinstance(secret_alias, str)
        and IDENTIFIER_PATTERN.fullmatch(secret_alias) is not None
        and isinstance(ttl_seconds, int)
        and not isinstance(ttl_seconds, bool)
        and 1 <= ttl_seconds <= MAX_TTL_SECONDS
        and isinstance(authority_epoch, int)
        and not isinstance(authority_epoch, bool)
        and 1 <= authority_epoch <= 2**63 - 1
        and isinstance(authority_record_id, str)
        and _AUTHORITY_RECORD_ID.fullmatch(authority_record_id) is not None
        and isinstance(capability_set_bound_id, str)
        and _CAPABILITY_SET_ID.fullmatch(capability_set_bound_id) is not None
    )
    if not valid:
        raise ProtocolRefusal(
            "credential_lease_binding_invalid",
            "credential lease requires exact attempt, principal, capability, "
            "alias, TTL, authority, and capability snapshot coordinates",
        )
    return (
        attempt_id,
        principal,
        capability,
        secret_alias,
        ttl_seconds,
        authority_epoch,
        authority_record_id,
        capability_set_bound_id,
    )


class CredentialLeaseLedger:
    """The secret-free physical credential lease lifecycle."""

    relative_path = Path("credentials/leases.jsonl")

    def __init__(self, root: FloatiRoot) -> None:
        if not isinstance(root, FloatiRoot):
            raise ProtocolRefusal(
                "root_required", "credential leases require a writable FloatiRoot"
            )
        self.root = root

    def records(self) -> List[Dict[str, object]]:
        return read_records(
            self.root, self.relative_path, allowed_kinds=set(CREDENTIAL_LEASE_KINDS)
        )

    @staticmethod
    def _project(
        records: List[Dict[str, object]], *, integrity: bool
    ) -> tuple[
        Dict[str, Dict[str, object]],
        Dict[str, Dict[str, object]],
        Dict[str, Dict[str, object]],
    ]:
        error = IntegrityFailure if integrity else ProtocolRefusal
        grants: Dict[str, Dict[str, object]] = {}
        consumed: Dict[str, Dict[str, object]] = {}
        revoked: Dict[str, Dict[str, object]] = {}
        for record in records:
            kind = str(record["kind"])
            if kind == "credential_lease_granted":
                grants[str(record["id"])] = record
                continue
            lease_id = str(record["lease_id"])
            if lease_id not in grants:
                raise error(
                    "credential_lease_forward_reference",
                    "credential lease lifecycle records must follow their grant",
                )
            target = consumed if kind == "credential_lease_consumed" else revoked
            if lease_id in target:
                raise error(
                    "credential_lease_duplicate_lifecycle",
                    "credential lease lifecycle record is duplicated",
                )
            target[lease_id] = record
        return grants, consumed, revoked

    @staticmethod
    def _active_from(
        records: List[Dict[str, object]], lease_id: str, current: datetime
    ) -> Dict[str, object]:
        grants, consumed, revoked = CredentialLeaseLedger._project(
            records, integrity=True
        )
        grant = grants.get(lease_id)
        if grant is None:
            raise ProtocolRefusal(
                "credential_lease_unknown", "credential lease grant is absent"
            )
        if lease_id in revoked:
            raise ProtocolRefusal(
                "credential_lease_revoked", "credential lease was revoked"
            )
        if lease_id in consumed:
            raise ProtocolRefusal(
                "credential_lease_already_consumed",
                "credential lease can be consumed once",
            )
        if current >= _parse_time(grant["expires_at"]):
            raise ProtocolRefusal(
                "credential_lease_expired", "credential lease TTL has elapsed"
            )
        return grant

    def issue(
        self,
        attempt_id: str,
        principal: str,
        capability: str,
        secret_alias: str,
        ttl_seconds: int,
        authority_epoch: int,
        authority_record_id: str,
        capability_set_bound_id: str,
        *,
        now: Optional[datetime] = None,
    ) -> Dict[str, object]:
        (
            attempt,
            owner,
            capability_name,
            alias,
            ttl,
            epoch,
            authority_id,
            snapshot_id,
        ) = _binding(
            attempt_id,
            principal,
            capability,
            secret_alias,
            ttl_seconds,
            authority_epoch,
            authority_record_id,
            capability_set_bound_id,
        )
        current = _now(now)
        record: Dict[str, object] = {
            "schema_version": 1,
            "id": "credential-lease-" + uuid7_hex(),
            "tenant_id": self.root.tenant_id,
            "timestamp": _timestamp(current),
            "kind": "credential_lease_granted",
            "attempt_id": attempt,
            "principal": owner,
            "capability": capability_name,
            "secret_alias": alias,
            "ttl_seconds": ttl,
            "expires_at": _timestamp(current + timedelta(seconds=ttl)),
            "authority_epoch": epoch,
            "authority_record_id": authority_id,
            "capability_set_bound_id": snapshot_id,
        }

        def choose(records: List[Dict[str, object]]):
            self._project(records, integrity=True)
            return record, record

        return transact(
            self.root,
            self.relative_path,
            choose,
            allowed_kinds=set(CREDENTIAL_LEASE_KINDS),
        )

    def active(
        self, lease_id: str, *, now: Optional[datetime] = None
    ) -> Dict[str, object]:
        return dict(self._active_from(self.records(), lease_id, _now(now)))

    def consume(
        self,
        lease_id: str,
        delivery_mode: str,
        *,
        now: Optional[datetime] = None,
    ) -> Dict[str, object]:
        if delivery_mode not in DELIVERY_MODES:
            raise ProtocolRefusal(
                "credential_lease_delivery_unavailable",
                "credential delivery mode is not declared in v1",
            )
        current = _now(now)
        record: Dict[str, object] = {
            "schema_version": 1,
            "id": "credential-lease-consumed-" + uuid7_hex(),
            "tenant_id": self.root.tenant_id,
            "timestamp": _timestamp(current),
            "kind": "credential_lease_consumed",
            "lease_id": lease_id,
            "delivery_mode": delivery_mode,
            "consumed_at_testimony": _timestamp(current),
        }

        def choose(records: List[Dict[str, object]]):
            self._active_from(records, lease_id, current)
            return record, record

        return transact(
            self.root,
            self.relative_path,
            choose,
            allowed_kinds=set(CREDENTIAL_LEASE_KINDS),
        )

    def revoke_alias(
        self,
        secret_alias: str,
        principal: str,
        authority_epoch: int,
        *,
        now: Optional[datetime] = None,
    ) -> List[Dict[str, object]]:
        if (
            not isinstance(secret_alias, str)
            or IDENTIFIER_PATTERN.fullmatch(secret_alias) is None
            or not isinstance(principal, str)
            or IDENTIFIER_PATTERN.fullmatch(principal) is None
            or not isinstance(authority_epoch, int)
            or isinstance(authority_epoch, bool)
            or authority_epoch < 1
        ):
            raise ProtocolRefusal(
                "credential_lease_binding_invalid",
                "credential lease revocation requires exact alias authority",
            )
        current = _now(now)

        def choose(records: List[Dict[str, object]]):
            grants, _consumed, revoked = self._project(records, integrity=True)
            candidates = []
            for lease_id, grant in grants.items():
                if (
                    grant["secret_alias"] != secret_alias
                    or grant["principal"] != principal
                    or grant["authority_epoch"] != authority_epoch
                    or lease_id in revoked
                ):
                    continue
                candidates.append({
                    "schema_version": 1,
                    "id": "credential-lease-revoked-" + uuid7_hex(),
                    "tenant_id": self.root.tenant_id,
                    "timestamp": _timestamp(current),
                    "kind": "credential_lease_revoked",
                    "lease_id": lease_id,
                    "authority_epoch": authority_epoch,
                    "revoked_at_testimony": _timestamp(current),
                })
            return [dict(row) for row in candidates], candidates

        return transact_records(
            self.root,
            self.relative_path,
            choose,
            allowed_kinds=set(CREDENTIAL_LEASE_KINDS),
        )


@dataclass
class CredentialDelivery:
    """One secret-bearing delivery value whose representation stays secret-free."""

    mode: str
    receipt: Dict[str, object]
    inherited_fd: Optional[int] = field(default=None, repr=False)
    helper_response: Optional[bytes] = field(default=None, repr=False)

    def close(self) -> None:
        descriptor = self.inherited_fd
        self.inherited_fd = None
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass


class CredentialLeaseService:
    """Internal launch boundary for authority-bound credential delivery."""

    def __init__(self, root: FloatiRoot) -> None:
        if not isinstance(root, FloatiRoot):
            raise ProtocolRefusal(
                "root_required", "credential lease service requires a writable FloatiRoot"
            )
        self.root = root
        self.ledger = CredentialLeaseLedger(root)
        self.authorities = AuthorityGrantStore(root)
        self.runs = RunLedger(root)

    def _authority(
        self,
        secret_alias: str,
        principal: str,
        authority_epoch: int,
        current: datetime,
        *,
        missing_code: str,
    ) -> Dict[str, object]:
        try:
            authority = self.authorities.exact_tail(secret_alias)
        except ProtocolRefusal as exc:
            if exc.code != "authority_missing":
                raise
            raise ProtocolRefusal(
                missing_code, "credential alias authority is absent"
            ) from exc
        if (
            authority.get("holder") != principal
            or authority.get("epoch") != authority_epoch
        ):
            raise ProtocolRefusal(
                "credential_lease_authority_mismatch",
                "credential lease does not match the exact alias authority coordinate",
            )
        if (
            authority.get("state") != "active"
            or current < _parse_time(authority["renewed_at"])
            or current >= _parse_time(authority["expires_at"])
        ):
            raise ProtocolRefusal(
                "credential_lease_authority_inactive",
                "credential alias authority is not active",
            )
        return authority

    def issue(
        self,
        snapshot: Dict[str, object],
        attempt_id: str,
        principal: str,
        capability: str,
        secret_alias: str,
        authority_epoch: int,
        ttl_seconds: int,
        *,
        now: Optional[datetime] = None,
    ) -> Dict[str, object]:
        current = _now(now)
        if not isinstance(snapshot, dict) or not isinstance(snapshot.get("id"), str):
            raise ProtocolRefusal(
                "credential_lease_snapshot_missing",
                "credential lease issue requires one persisted capability snapshot",
            )
        persisted = next(
            (row for row in self.runs.records() if row.get("id") == snapshot["id"]),
            None,
        )
        if persisted is None:
            raise ProtocolRefusal(
                "credential_lease_snapshot_missing",
                "credential lease issue requires one persisted capability snapshot",
            )
        if persisted != snapshot:
            raise ProtocolRefusal(
                "credential_lease_snapshot_mismatch",
                "credential lease snapshot must equal its persisted record",
            )
        if (
            snapshot.get("kind") != "capability_set_bound"
            or snapshot.get("attempt_id") != attempt_id
            or snapshot.get("chosen_worker") != principal
        ):
            raise ProtocolRefusal(
                "credential_lease_snapshot_mismatch",
                "credential lease attempt and principal must match the capability snapshot",
            )
        effective = snapshot.get("effective_grants")
        if not isinstance(effective, list) or capability not in {
            row.get("capability_name") for row in effective if isinstance(row, dict)
        }:
            raise ProtocolRefusal(
                "credential_lease_capability_missing",
                "credential lease capability is absent from the bound snapshot",
            )
        authority = self._authority(
            secret_alias,
            principal,
            authority_epoch,
            current,
            missing_code="credential_lease_authority_missing",
        )
        return self.ledger.issue(
            attempt_id,
            principal,
            capability,
            secret_alias,
            ttl_seconds,
            authority_epoch,
            str(authority["id"]),
            str(snapshot["id"]),
            now=current,
        )

    @staticmethod
    def _helper_secret(helper_argv: Sequence[str], secret_alias: str) -> bytes:
        if (
            not isinstance(helper_argv, (tuple, list))
            or not helper_argv
            or any(
                not isinstance(argument, str) or not argument or "\x00" in argument
                for argument in helper_argv
            )
        ):
            raise ProtocolRefusal(
                "credential_lease_helper_refused",
                "credential helper invocation was refused",
            )
        try:
            completed = subprocess.run(
                tuple(helper_argv),
                input=(secret_alias + "\n").encode("utf-8"),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=5,
                check=False,
                close_fds=True,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise ProtocolRefusal(
                "credential_lease_helper_refused",
                "credential helper invocation was refused",
            ) from exc
        output = completed.stdout
        if (
            completed.returncode != 0
            or not isinstance(output, bytes)
            or not 2 <= len(output) <= MAX_HELPER_RESPONSE_BYTES + 1
            or not output.endswith(b"\n")
            or output.count(b"\n") != 1
        ):
            raise ProtocolRefusal(
                "credential_lease_helper_refused",
                "credential helper invocation was refused",
            )
        return output[:-1]

    def deliver(
        self,
        lease_id: str,
        policy: RepositoryPolicy,
        worker_profile: str,
        helper_argv: Sequence[str],
        *,
        now: Optional[datetime] = None,
    ) -> CredentialDelivery:
        policy = validate_repository_policy_integrity(policy)
        profile = policy.worker_profiles.get(worker_profile)
        if profile is None or profile.secret_isolation is None:
            raise ProtocolRefusal(
                "credential_lease_adapter_undeclared",
                "worker adapter does not declare credential isolation",
            )
        delivery_mode = {
            "process": "inherited_fd",
            "helper": "helper_response",
            "none": None,
        }[profile.secret_isolation]
        if delivery_mode is None:
            raise ProtocolRefusal(
                "credential_lease_delivery_unavailable",
                "worker adapter declares no credential delivery",
            )
        current = _now(now)
        lease = self.ledger.active(lease_id, now=current)
        self._authority(
            str(lease["secret_alias"]),
            str(lease["principal"]),
            int(lease["authority_epoch"]),
            current,
            missing_code="credential_lease_authority_inactive",
        )
        receipt = self.ledger.consume(lease_id, delivery_mode, now=current)
        secret = self._helper_secret(helper_argv, str(lease["secret_alias"]))
        if delivery_mode == "helper_response":
            return CredentialDelivery(
                delivery_mode, dict(receipt), helper_response=secret
            )
        read_fd: Optional[int] = None
        write_fd: Optional[int] = None
        try:
            read_fd, write_fd = os.pipe()
            os.set_inheritable(read_fd, True)
            view = memoryview(secret)
            while view:
                written = os.write(write_fd, view)
                view = view[written:]
            os.close(write_fd)
            write_fd = None
            return CredentialDelivery(
                delivery_mode, dict(receipt), inherited_fd=read_fd
            )
        except OSError as exc:
            if read_fd is not None:
                os.close(read_fd)
            if write_fd is not None:
                os.close(write_fd)
            raise ProtocolRefusal(
                "credential_lease_delivery_unavailable",
                "inherited descriptor delivery was unavailable",
            ) from exc


__all__ = [
    "CredentialDelivery", "CredentialLeaseLedger", "CredentialLeaseService",
]

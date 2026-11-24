"""Closed consent, lifecycle, and adapter-binding contracts for wake daemons."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

from .errors import IntegrityFailure, ProtocolRefusal
from .ids import uuid7_hex
from .jsonl import read_records_snapshot, transact
from .records import validate_record
from .registry import Registry, utc_now
from .root import FloatiRoot
from .wake_control import validate_session_id
from .wake_daemon_roll import (
    consent_relative,
    ensure_rolled,
    lifecycle_relative,
)


DAEMON_KINDS = frozenset({
    "wake_daemon_consent_receipt",
    "wake_daemon_lifecycle_receipt",
    "wake_daemon_directory_ownership_record",
    "codex_binding_repair_receipt",
})
SUPPORTED_HARNESSES = frozenset({"codex", "cursor", "grok-build", "zcode"})
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_VERSION = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]{0,62}[A-Za-z0-9])?$")
_BINDING_FIELDS = frozenset({
    "schema_version", "tenant_id", "node_id", "harness",
    "coordinate_digest", "session_id", "session_digest", "workspace",
    "executable", "executable_digest", "adapter_version", "adapter_digest",
    "binding_epoch",
})
# WD-R5c-F1: resume_state is additive-optional. Two exact shapes are valid:
# the closed pre-R5c record, and that record plus one of the three bindable
# resume states. Unknown extra keys remain invalid.
_BINDABLE_RESUME_STATES = frozenset({
    "resume_proven", "resume_unproven", "resume_suspect",
})


def _schema_version(harness: str) -> int:
    # grok-build and zcode carry the v1 daemon record shape; the v1
    # harness enum in records.py is widened to match (WD-R2).
    return 1 if harness in ("grok-build", "zcode") else 0


def _sha256(value: object, field: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ProtocolRefusal(f"{field}_invalid", f"{field} must be a SHA-256 digest")
    return value


def _positive_integer(value: object, field: str, *, maximum: int = 2**31 - 1) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= maximum:
        raise ProtocolRefusal(f"{field}_invalid", f"{field} is outside its integer bounds")
    return value


def _version(value: object, field: str = "adapter_version") -> str:
    if not isinstance(value, str) or _VERSION.fullmatch(value) is None:
        raise ProtocolRefusal(f"{field}_invalid", f"{field} is not a bounded version")
    return value


@dataclass(frozen=True)
class DaemonCoordinate:
    root: FloatiRoot
    node_id: str
    harness: str

    def __post_init__(self) -> None:
        if not isinstance(self.root, FloatiRoot):
            raise ProtocolRefusal(
                "wake_daemon_root_invalid", "wake daemon requires one validated direct-home root"
            )
        node = Registry(self.root).resolve_node_id(self.node_id, field="node")
        if self.harness not in SUPPORTED_HARNESSES:
            raise ProtocolRefusal(
                "wake_daemon_harness_unsupported",
                "wake daemon v1 supports only codex, cursor, grok-build, or zcode",
            )
        object.__setattr__(self, "node_id", node)

    @property
    def digest(self) -> str:
        payload = "\0".join(
            (str(self.root.path), self.root.tenant_id, self.node_id, self.harness)
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()


class DaemonConsentLedger:
    def __init__(self, root: FloatiRoot) -> None:
        if not isinstance(root, FloatiRoot):
            raise ProtocolRefusal("wake_daemon_root_invalid", "consent requires a validated root")
        self.root = root

    @staticmethod
    def _relative(node: str) -> Path:
        # FQ-9 / P5: consent lives in its own plane, so require_active
        # reads the consent record and never a lifecycle row. The
        # lifecycle file keeps its historical path and rolls.
        return consent_relative(node)

    @staticmethod
    def _legacy_relative(node: str) -> Path:
        # The pre-split mixed file, still the consent source on a root
        # whose layout has not migrated yet.
        return lifecycle_relative(node)

    def _rows(self, coordinate: DaemonCoordinate) -> list[Dict[str, Any]]:
        plane_exists = (self.root.path / self._relative(coordinate.node_id)).is_file()
        source = (
            self._relative(coordinate.node_id)
            if plane_exists
            else self._legacy_relative(coordinate.node_id)
        )
        return [
            row
            for row in read_records_snapshot(
                self.root, source, allowed_kinds=DAEMON_KINDS
            )
            if row.get("kind") == "wake_daemon_consent_receipt"
            and row.get("coordinate_digest") == coordinate.digest
        ]

    def consent(
        self,
        coordinate: DaemonCoordinate,
        *,
        adapter_version: str,
        adapter_digest: str,
        min_poll_seconds: int,
        max_poll_seconds: int,
        max_backoff_seconds: int,
        activation_epoch: int,
        idempotency_key: str,
        idle_retire_days: Optional[int] = None,
    ) -> Dict[str, Any]:
        self._require_coordinate(coordinate)
        adapter_version = _version(adapter_version)
        adapter_digest = _sha256(adapter_digest, "adapter_digest")
        minimum = _positive_integer(min_poll_seconds, "min_poll_seconds", maximum=86400)
        maximum = _positive_integer(max_poll_seconds, "max_poll_seconds", maximum=86400)
        backoff = _positive_integer(max_backoff_seconds, "max_backoff_seconds", maximum=86400)
        epoch = _positive_integer(activation_epoch, "activation_epoch", maximum=2**63 - 1)
        # WD-4: the idle window is end-user configuration, written under the
        # consent kind's NEXT schema version. None keeps the legacy v0/v1
        # shape byte-identical; a value emits schema_version 2.
        if idle_retire_days is not None and (
            isinstance(idle_retire_days, bool)
            or not isinstance(idle_retire_days, int)
            or not 0 <= idle_retire_days <= 3650
        ):
            raise ProtocolRefusal(
                "idle_retire_days_invalid",
                "idle_retire_days must be an integer 0 through 3650 (0 = never)",
            )
        if maximum < minimum:
            raise ProtocolRefusal("wake_daemon_poll_bounds_invalid", "maximum poll is below minimum")
        if backoff < maximum:
            raise ProtocolRefusal("wake_daemon_backoff_bounds_invalid", "maximum backoff is below maximum poll")
        key = self._key(idempotency_key)
        row = {
            "schema_version": _schema_version(coordinate.harness),
            "id": "wake-daemon-consent-" + uuid7_hex(),
            "tenant_id": self.root.tenant_id,
            "timestamp": utc_now(),
            "kind": "wake_daemon_consent_receipt",
            "node_id": coordinate.node_id,
            "harness": coordinate.harness,
            "coordinate_digest": coordinate.digest,
            "adapter_version": adapter_version,
            "adapter_digest": adapter_digest,
            "min_poll_seconds": minimum,
            "max_poll_seconds": maximum,
            "max_backoff_seconds": backoff,
            "activation_epoch": epoch,
            "operation": "consent",
            "state": "active",
            "predecessor_receipt_id": None,
            "idempotency_key": key,
        }
        if idle_retire_days is not None:
            row["schema_version"] = 2
            row["idle_retire_days"] = int(idle_retire_days)
        return self._append(coordinate, row)

    def revoke(
        self, coordinate: DaemonCoordinate, *, idempotency_key: str
    ) -> Dict[str, Any]:
        active = self.require_active(coordinate)
        row = dict(active)
        row.update({
            "id": "wake-daemon-consent-" + uuid7_hex(),
            "timestamp": utc_now(),
            "operation": "revoke",
            "state": "revoked",
            "predecessor_receipt_id": active["id"],
            "idempotency_key": self._key(idempotency_key),
        })
        return self._append(coordinate, row)

    def require_active(self, coordinate: DaemonCoordinate) -> Dict[str, Any]:
        self._require_coordinate(coordinate)
        rows = self._rows(coordinate)
        if not rows or rows[-1].get("state") != "active":
            raise ProtocolRefusal(
                "wake_daemon_consent_absent", "wake daemon coordinate has no active consent"
            )
        return rows[-1]

    def _append(self, coordinate: DaemonCoordinate, row: Dict[str, Any]) -> Dict[str, Any]:
        # The consent write is the write that must see every prior consent
        # row, so it migrates a legacy mixed file first; the idempotency
        # and activation-epoch scans below then read a complete prior.
        ensure_rolled(self.root, coordinate.node_id)
        semantic = tuple(
            key for key in row if key not in {"id", "timestamp"}
        )

        def decide(prior: list[Dict[str, Any]]):
            matching = [item for item in prior if item.get("idempotency_key") == row["idempotency_key"]]
            if matching:
                existing = matching[-1]
                if all(existing.get(field) == row.get(field) for field in semantic):
                    return existing, None
                raise ProtocolRefusal(
                    "wake_daemon_consent_idempotency_conflict",
                    "wake daemon consent key has different content",
                )
            same = [
                item for item in prior
                if item.get("kind") == "wake_daemon_consent_receipt"
                and item.get("coordinate_digest") == coordinate.digest
            ]
            if row["operation"] == "consent" and same:
                latest_epoch = max(int(item["activation_epoch"]) for item in same)
                if int(row["activation_epoch"]) <= latest_epoch:
                    raise ProtocolRefusal(
                        "wake_daemon_activation_epoch_stale",
                        "wake daemon activation epoch must increase",
                    )
                row["predecessor_receipt_id"] = same[-1]["id"]
            validate_record(row, self.root.tenant_id, DAEMON_KINDS, integrity=False)
            return row, row

        return transact(
            self.root, self._relative(coordinate.node_id), decide, allowed_kinds=DAEMON_KINDS
        )

    def _require_coordinate(self, coordinate: DaemonCoordinate) -> None:
        if not isinstance(coordinate, DaemonCoordinate) or coordinate.root is not self.root:
            raise ProtocolRefusal(
                "wake_daemon_coordinate_invalid", "daemon coordinate belongs to another root"
            )

    @staticmethod
    def _key(value: object) -> str:
        if not isinstance(value, str) or not 1 <= len(value) <= 128:
            raise ProtocolRefusal("idempotency_key_invalid", "idempotency key is out of bounds")
        return value


class DaemonLifecycleLedger:
    def __init__(self, root: FloatiRoot) -> None:
        self.root = root

    def record(
        self,
        coordinate: DaemonCoordinate,
        *,
        daemon_instance_id: str,
        activation_epoch: int,
        event: str,
        state: str,
        reason_code: Optional[str],
        adapter_digest: str,
        plist_digest: Optional[str],
        session_digest: Optional[str],
        predecessor_receipt_id: Optional[str],
        idempotency_key: str,
        exception_type: Optional[str] = None,
        exception_message: Optional[str] = None,
        conflict_remedy: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Append one lifecycle receipt, idempotent on ``idempotency_key``.

        F2-TAKEOVER-DAEMON-1: a key that already holds different content is
        still refused - the guard is unchanged - but the refusal now names
        the key, the receipt that spent it and the fields that differ, and
        carries ``conflict_remedy``: the caller that composed the key is the
        one that knows which act clears its collision.
        """
        if coordinate.root is not self.root:
            raise ProtocolRefusal("wake_daemon_coordinate_invalid", "lifecycle coordinate belongs to another root")
        # The lifecycle append is the one per-cycle write: it owns the
        # roll check, so a root that cycles pays two lstat calls and a
        # root whose receipts crossed the policy threshold rolls here.
        ensure_rolled(self.root, coordinate.node_id)
        row = {
            "schema_version": _schema_version(coordinate.harness),
            "id": "wake-daemon-lifecycle-" + uuid7_hex(),
            "tenant_id": self.root.tenant_id,
            "timestamp": utc_now(),
            "kind": "wake_daemon_lifecycle_receipt",
            "node_id": coordinate.node_id,
            "harness": coordinate.harness,
            "coordinate_digest": coordinate.digest,
            "daemon_instance_id": _version(daemon_instance_id, "daemon_instance_id"),
            "activation_epoch": _positive_integer(activation_epoch, "activation_epoch", maximum=2**63 - 1),
            "event": event,
            "state": state,
            "reason_code": reason_code,
            "adapter_digest": _sha256(adapter_digest, "adapter_digest"),
            "plist_digest": None if plist_digest is None else _sha256(plist_digest, "plist_digest"),
            "session_digest": None if session_digest is None else _sha256(session_digest, "session_digest"),
            "predecessor_receipt_id": predecessor_receipt_id,
            "idempotency_key": DaemonConsentLedger._key(idempotency_key),
        }
        if (exception_type is None) != (exception_message is None):
            raise ProtocolRefusal(
                "wake_daemon_exception_detail_invalid",
                "exception type and message are recorded together",
                "pass both exception_type and exception_message to the "
                "lifecycle record, or neither - one-sided exception detail "
                "is not a recordable shape",
            )
        if exception_type is not None:
            row["exception_type"] = exception_type
            row["exception_message"] = exception_message
        semantic = tuple(key for key in row if key not in {"id", "timestamp"})

        def decide(prior: list[Dict[str, Any]]):
            for existing in prior:
                if existing.get("idempotency_key") != row["idempotency_key"]:
                    continue
                differing = [
                    field for field in semantic if existing.get(field) != row.get(field)
                ]
                if not differing:
                    return existing, None
                # The key leads the detail: the cycle_exception receipt keeps
                # only the first CYCLE_EXCEPTION_MESSAGE_BOUND characters.
                raise ProtocolRefusal(
                    "wake_daemon_lifecycle_idempotency_conflict",
                    f"wake daemon lifecycle key {row['idempotency_key']} is already "
                    f"spent with different {', '.join(differing)} by receipt "
                    f"{existing.get('id')}",
                    remedy=conflict_remedy,
                )
            validate_record(row, self.root.tenant_id, DAEMON_KINDS, integrity=False)
            return row, row

        return transact(
            self.root,
            lifecycle_relative(coordinate.node_id),
            decide,
            allowed_kinds=DAEMON_KINDS,
        )

    def record_directory_ownership(
        self,
        coordinate: DaemonCoordinate,
        *,
        lifecycle_receipt_id: str,
        unit_path: str,
        unit_digest: str,
        created_directories: list[Dict[str, Any]],
        idempotency_key: str,
    ) -> Dict[str, Any]:
        """Append optional ownership after the unit's installed receipt.

        This shares the lifecycle ledger's governed transaction and retention.
        It deliberately does not trigger another roll between the two appends.
        A missing append grants no directory-removal authority.
        """
        if coordinate.root is not self.root:
            raise ProtocolRefusal(
                "wake_daemon_coordinate_invalid", "directory ownership belongs to another root"
            )
        row = {
            "schema_version": 1,
            "id": "wake-daemon-directory-ownership-" + uuid7_hex(),
            "tenant_id": self.root.tenant_id,
            "timestamp": utc_now(),
            "kind": "wake_daemon_directory_ownership_record",
            "node_id": coordinate.node_id,
            "harness": coordinate.harness,
            "coordinate_digest": coordinate.digest,
            "lifecycle_receipt_id": lifecycle_receipt_id,
            "unit_path": unit_path,
            "unit_digest": unit_digest,
            "created_directories": created_directories,
            "idempotency_key": DaemonConsentLedger._key(idempotency_key),
        }
        validate_record(row, self.root.tenant_id, DAEMON_KINDS, integrity=False)
        semantic = tuple(key for key in row if key not in {"id", "timestamp"})

        def decide(prior: list[Dict[str, Any]]):
            for existing in prior:
                if existing.get("idempotency_key") == row["idempotency_key"]:
                    if all(existing.get(field) == row[field] for field in semantic):
                        return existing, None
                    raise ProtocolRefusal(
                        "idempotency_conflict", "directory ownership key has different content",
                        remedy="use a fresh key for a different directory ownership record",
                    )
            return row, row

        return transact(
            self.root, lifecycle_relative(coordinate.node_id), decide, allowed_kinds=DAEMON_KINDS
        )


class AdapterBindingStore:
    def __init__(self, root: FloatiRoot) -> None:
        if not isinstance(root, FloatiRoot):
            raise ProtocolRefusal("wake_daemon_root_invalid", "binding store requires a validated root")
        self.root = root

    def path(self, coordinate: DaemonCoordinate) -> Path:
        self._require_coordinate(coordinate)
        return self.root.resolve_relative(
            Path("state/wake-daemon/adapters") / coordinate.node_id / f"{coordinate.harness}.json"
        )

    def write(
        self,
        coordinate: DaemonCoordinate,
        *,
        session_id: str,
        workspace: Path,
        executable: Path,
        adapter_version: str,
        adapter_digest: str,
        binding_epoch: int,
        resume_state: Optional[str] = None,
        expected_executable_digest: Optional[str] = None,
    ) -> Dict[str, Any]:
        self._require_coordinate(coordinate)
        session = validate_session_id(session_id)
        if resume_state is not None and resume_state not in _BINDABLE_RESUME_STATES:
            raise ProtocolRefusal(
                "wake_daemon_resume_state_invalid",
                "binding resume_state must be one of: "
                + ", ".join(sorted(_BINDABLE_RESUME_STATES)),
            )
        workspace_path = self._ordinary_directory(workspace, "workspace")
        executable_path = self._binding_executable(coordinate, executable)
        # The fixed adapter constant declares Codex. The resolved target is
        # testimony: its disappearance can be distinguished from byte drift
        # at a still-existing target without changing the binding shape.
        record = {
            "schema_version": _schema_version(coordinate.harness),
            "tenant_id": self.root.tenant_id,
            "node_id": coordinate.node_id,
            "harness": coordinate.harness,
            "coordinate_digest": coordinate.digest,
            "session_id": session,
            "session_digest": hashlib.sha256(session.encode("utf-8")).hexdigest(),
            "workspace": str(workspace_path),
            "executable": str(executable_path),
            "executable_digest": hashlib.sha256(executable_path.read_bytes()).hexdigest(),
            "adapter_version": _version(adapter_version),
            "adapter_digest": _sha256(adapter_digest, "adapter_digest"),
            "binding_epoch": _positive_integer(binding_epoch, "binding_epoch", maximum=2**63 - 1),
        }
        if (expected_executable_digest is not None
                and record['executable_digest'] != _sha256(expected_executable_digest, 'expected_executable_digest')):
            raise ProtocolRefusal('wake_daemon_codex_identity_changed',
                'executable bytes changed after provider identification')
        if resume_state is not None:
            record["resume_state"] = resume_state
        encoded = (json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
        path = self.path(coordinate)
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid7_hex()}.tmp")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            if os.write(descriptor, encoded) != len(encoded):
                raise OSError("short binding write")
            os.fsync(descriptor)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
        finally:
            os.close(descriptor)
        os.replace(temporary, path)
        return self.read(coordinate)

    def read(self, coordinate: DaemonCoordinate) -> Dict[str, Any]:
        record = self._read_metadata(coordinate)
        try:
            executable = self._binding_executable(coordinate, Path(record["executable"]))
            current_digest = hashlib.sha256(executable.read_bytes()).hexdigest()
        except (ProtocolRefusal, OSError, RuntimeError) as exc:
            _, remedy = executable_drift_remedy(
                coordinate, record, Path(record["executable"]), None)
            raise ProtocolRefusal(
                "wake_daemon_executable_invalid",
                f"adapter executable is missing, unreadable, or invalid: {record['executable']}",
                remedy=remedy,
            ) from exc
        if current_digest != record["executable_digest"]:
            detail, remedy = executable_drift_remedy(coordinate, record, executable, current_digest)
            raise ProtocolRefusal("wake_daemon_executable_digest_mismatch", detail, remedy=remedy)
        self._validate_metadata_contract(record)
        from .wake_daemon_adapters import CODEX_EXECUTABLE
        declared_codex = coordinate.harness == "codex" and record["executable"] == str(CODEX_EXECUTABLE)
        if not declared_codex and str(executable) != record["executable"]:
            raise IntegrityFailure("wake_daemon_binding_invalid", "adapter binding paths are not canonical")
        return record

    def _read_metadata(self, coordinate: DaemonCoordinate) -> Dict[str, Any]:
        """Validate the closed identity before executable use or explicit refresh.

        This never authorizes execution. Refresh callers must independently
        prove the existing seat and current waiter claim before writing.
        """
        self._require_coordinate(coordinate)
        path = self.path(coordinate)
        if path.is_symlink() or not path.is_file():
            raise ProtocolRefusal("wake_daemon_binding_absent", "exact daemon adapter binding is absent")
        try:
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            try:
                raw = os.read(descriptor, 65537)
            finally:
                os.close(descriptor)
            if len(raw) > 65536:
                raise ValueError("oversized")
            record = json.loads(raw.decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise IntegrityFailure("wake_daemon_binding_invalid", "adapter binding is unreadable") from exc
        # WD-R5b: resume_state is additive-optional - a record with it (new
        # binds) and a legacy record without it (pre-probe binds, codex
        # waiter binds) are both valid shapes; anything else is drift.
        if not isinstance(record, dict) or set(record) not in (
            _BINDING_FIELDS,
            _BINDING_FIELDS | {"resume_state"},
        ):
            raise IntegrityFailure("wake_daemon_binding_invalid", "adapter binding shape is invalid")
        session = validate_session_id(record.get("session_id"))
        if (
            record.get("schema_version") != _schema_version(coordinate.harness)
            or record.get("tenant_id") != self.root.tenant_id
            or record.get("node_id") != coordinate.node_id
            or record.get("harness") != coordinate.harness
            or record.get("coordinate_digest") != coordinate.digest
            or record.get("session_digest") != hashlib.sha256(session.encode("utf-8")).hexdigest()
        ):
            raise IntegrityFailure("wake_daemon_binding_invalid", "adapter binding identity is invalid")
        workspace = self._ordinary_directory(Path(str(record["workspace"])), "workspace")
        if not isinstance(record["executable"], str) or not Path(record["executable"]).is_absolute():
            raise IntegrityFailure("wake_daemon_binding_invalid", "adapter executable path must be absolute")
        if str(workspace) != record["workspace"]:
            raise IntegrityFailure("wake_daemon_binding_invalid", "adapter binding paths are not canonical")
        return dict(record)

    @staticmethod
    def _validate_metadata_contract(record: Dict[str, Any]) -> None:
        # Reads report executable drift before other contract errors, as they
        # did before metadata-only recovery existed. Refresh must validate
        # these fields explicitly without trusting or executing old bytes.
        _sha256(record.get("executable_digest"), "executable_digest")
        _sha256(record.get("adapter_digest"), "adapter_digest")
        _version(record.get("adapter_version"))
        _positive_integer(record.get("binding_epoch"), "binding_epoch", maximum=2**63 - 1)
        if "resume_state" in record and record["resume_state"] not in _BINDABLE_RESUME_STATES:
            raise IntegrityFailure(
                "wake_daemon_binding_invalid", "adapter binding resume_state is invalid"
            )

    @classmethod
    def _binding_executable(cls, coordinate: DaemonCoordinate, value: Path) -> Path:
        from .wake_daemon_adapters import CODEX_EXECUTABLE

        if coordinate.harness == "codex" and value == CODEX_EXECUTABLE:
            value = value.resolve(strict=True)
        return cls._ordinary_executable(value)

    def remove(self, coordinate: DaemonCoordinate) -> Dict[str, Any]:
        record = self.read(coordinate)
        try:
            self.path(coordinate).unlink()
        except OSError as exc:
            raise ProtocolRefusal("wake_daemon_binding_remove_failed", "adapter binding could not be removed") from exc
        return record

    def _require_coordinate(self, coordinate: DaemonCoordinate) -> None:
        if not isinstance(coordinate, DaemonCoordinate) or coordinate.root is not self.root:
            raise ProtocolRefusal("wake_daemon_coordinate_invalid", "binding coordinate belongs to another root")

    @staticmethod
    def _ordinary_directory(value: Path, field: str) -> Path:
        path = Path(value).expanduser()
        if not path.is_absolute() or path.is_symlink() or not path.is_dir():
            raise ProtocolRefusal(f"wake_daemon_{field}_invalid", f"{field} must be an existing absolute ordinary directory")
        return path.resolve(strict=True)

    @staticmethod
    def _ordinary_executable(value: Path) -> Path:
        path = Path(value).expanduser()
        if not path.is_absolute() or path.is_symlink() or not path.is_file():
            raise ProtocolRefusal("wake_daemon_executable_invalid", "adapter executable must be an absolute ordinary file")
        resolved = path.resolve(strict=True)
        mode = resolved.stat().st_mode
        if not stat.S_ISREG(mode) or mode & 0o111 == 0:
            raise ProtocolRefusal("wake_daemon_executable_invalid", "adapter executable must be executable")
        return resolved


def lifecycle_conflict_remedy(runtime_path: Path) -> str:
    """The one act that clears a daemon's spent lifecycle key.

    The daemon composes its key from the runtime it persists (instance id and
    cycle index). A conflict means that runtime points at a slot the ledger
    already holds. The runtime is the disposable side by design - an absent
    runtime is initialized on the next cycle, under an instance and cycle
    count this process has never spent - while the ledger is the durable
    record and is never edited. So the act removes the runtime file, and it
    is the same typeable line for every harness, Codex included.
    """
    import shlex

    return shlex.join(["rm", "--", str(runtime_path)])


def executable_drift_remedy(
    coordinate: DaemonCoordinate,
    record: Dict[str, Any],
    executable: Path,
    current_digest: Optional[str],
) -> tuple[str, str]:
    """Describe observed drift and the existing explicit binding writer."""
    import shlex

    recorded_digest = record['executable_digest']
    if not isinstance(recorded_digest, str) or _SHA256.fullmatch(recorded_digest) is None:
        recorded_digest = 'invalid recorded digest'
    detail = (
        f"adapter executable changed: recorded executable {record['executable']}; "
        f"current executable {executable}; recorded SHA-256 {recorded_digest}; "
        f"current SHA-256 {current_digest if current_digest is not None else 'unreadable'}"
    )
    epoch = record['binding_epoch']
    if type(epoch) is not int or not 1 <= epoch <= 2**63 - 1:
        # Adapter callers can pass a projection. Never turn unchecked
        # metadata into command syntax.
        return detail, (
            "rebind through floati wake daemon bind --help with a valid positive "
            "--binding-epoch; the recorded binding epoch is invalid"
        )
    argv = [
        "floati", "wake", "daemon", "bind", "--root", str(coordinate.root.path),
        "--as", coordinate.node_id, "--harness", coordinate.harness,
        "--session", str(record['session_id']), "--workspace", str(record['workspace']),
        "--executable", str(executable), "--binding-epoch", str(epoch),
    ]
    if coordinate.harness == "codex":
        from .codex_wait_contract import CodexWaitSessionLedger
        from .wake_daemon_adapters import CODEX_EXECUTABLE

        # Boarding claims the acting session before its resume step. An old
        # adapter can therefore name the predecessor; the remedy must name
        # the current claim, which the refresh independently revalidates.
        claims = read_records_snapshot(coordinate.root,
            CodexWaitSessionLedger._relative(coordinate.node_id),
            allowed_kinds=CodexWaitSessionLedger._KINDS)
        matching = [row for row in claims if row["workspace"] == str(record["workspace"])]
        if matching:
            argv[argv.index("--session") + 1] = str(matching[-1]["acting_session_id"])
        argv[argv.index("--executable") + 1] = str(CODEX_EXECUTABLE)
    return detail, shlex.join(argv)

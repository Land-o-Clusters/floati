"""Closed Floati-owned identity and consent contracts for the Codex waiter."""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .errors import ProtocolRefusal
from .ids import uuid7_hex
from .jsonl import read_records_snapshot, transact
from .registry import Registry, utc_now
from .root import FloatiRoot, validate_identifier


WORKSPACE_MAP_RELATIVE = Path("codex-wait/workspaces.v0.json")

CODEX_WAIT_REOPEN_KIND = "codex_wait_reopen_fact"
CODEX_WAIT_REOPEN_OUTCOMES = frozenset({"rearmed", "consent_withdrawn"})
CODEX_WAIT_REOPEN_FIELDS = frozenset(
    {
        "schema_version",
        "kind",
        "tenant_id",
        "timestamp",
        "node_id",
        "session_digest",
        "ledger",
        "before",
        "after",
        "waited_seconds",
        "outcome",
        "invocation_id",
    }
)
LedgerIdentity = Dict[str, int]


def consent_ledger_relative(node_id: str) -> Path:
    """One derived coordinate for the per-node Codex waiter consent ledger."""

    return Path("receipts/codex-wait-consent") / f"{node_id}.jsonl"


def observe_ledger_identity(path: Path) -> Optional[LedgerIdentity]:
    """Read the device/inode a PATH names right now, or None when it names nothing."""

    try:
        status = Path(path).stat()
    except OSError:
        return None
    return {"device": int(status.st_dev), "inode": int(status.st_ino)}


class WatchedLedger:
    """Follow one ledger by PATH so a repair, rotation or restore is observed.

    ``VerifiedLedgerCursor`` already discards its cached prefix when the wake
    planes change device/inode.  A ledger read once into memory has no such
    follower, so the path it came from must be watched explicitly: the file a
    waiter is deciding from can be replaced under it at any poll.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._identity = observe_ledger_identity(self.path)

    @property
    def identity(self) -> Optional[LedgerIdentity]:
        return None if self._identity is None else dict(self._identity)

    def poll(self) -> Optional[Tuple[LedgerIdentity, Optional[LedgerIdentity]]]:
        """Report each replacement exactly once as the pair it moved between.

        A first appearance is a creation and not a replacement, so it is
        adopted silently; a disappearance after prior data is reported, because
        the document the caller is deciding from is gone.
        """

        current = observe_ledger_identity(self.path)
        if self._identity is None:
            self._identity = current
            return None
        if current == self._identity:
            return None
        before = self._identity
        self._identity = current
        return before, current


def _coordinate(value: object, field: str) -> None:
    if not isinstance(value, dict) or set(value) != {"device", "inode"}:
        raise ProtocolRefusal(
            "codex_wait_reopen_identity_invalid",
            f"{field} must be one device/inode coordinate",
        )
    for name in ("device", "inode"):
        number = value[name]
        if not isinstance(number, int) or isinstance(number, bool) or number < 0:
            raise ProtocolRefusal(
                "codex_wait_reopen_identity_invalid",
                f"{field}.{name} must be a nonnegative integer",
            )


def validate_reopen_fact(row: object, tenant_id: str) -> Dict[str, object]:
    """Refuse any reopen testimony that is not the exact closed v1 shape."""

    if not isinstance(row, dict) or set(row) != set(CODEX_WAIT_REOPEN_FIELDS):
        raise ProtocolRefusal(
            "codex_wait_reopen_fields_invalid",
            "reopen testimony must carry exactly its closed field set",
        )
    if row["schema_version"] != 1 or isinstance(row["schema_version"], bool):
        raise ProtocolRefusal(
            "codex_wait_reopen_schema_version_invalid",
            "reopen testimony is version 1 only",
        )
    if row["kind"] != CODEX_WAIT_REOPEN_KIND or row["tenant_id"] != tenant_id:
        raise ProtocolRefusal(
            "codex_wait_reopen_identity_invalid",
            "reopen testimony must name its own kind and tenant",
        )
    if not isinstance(row["timestamp"], str) or not 1 <= len(row["timestamp"]) <= 64:
        raise ProtocolRefusal(
            "codex_wait_reopen_timestamp_invalid", "reopen timestamp is out of bounds"
        )
    validate_identifier(row["node_id"], "node")
    if (
        not isinstance(row["session_digest"], str)
        or re.fullmatch(r"[0-9a-f]{64}", row["session_digest"]) is None
    ):
        raise ProtocolRefusal(
            "codex_wait_reopen_session_digest_invalid",
            "reopen session digest must be SHA-256",
        )
    ledger = row["ledger"]
    if (
        not isinstance(ledger, str)
        or not 1 <= len(ledger) <= 4096
        or not ledger.endswith(".jsonl")
        or ledger.startswith("/")
        or any(part in {"", ".", ".."} for part in ledger.split("/"))
    ):
        raise ProtocolRefusal(
            "codex_wait_reopen_ledger_invalid",
            "reopen testimony must name one bounded relative ledger coordinate",
        )
    _coordinate(row["before"], "before")
    if row["after"] is not None:
        _coordinate(row["after"], "after")
    if row["before"] == row["after"]:
        raise ProtocolRefusal(
            "codex_wait_reopen_identity_invalid",
            "reopen testimony must prove the path changed identity",
        )
    waited = row["waited_seconds"]
    if (
        not isinstance(waited, int)
        or isinstance(waited, bool)
        or not 0 <= waited <= 86399
    ):
        raise ProtocolRefusal(
            "codex_wait_reopen_waited_seconds_invalid",
            "reopen position is out of bounds",
        )
    if row["outcome"] not in CODEX_WAIT_REOPEN_OUTCOMES:
        raise ProtocolRefusal(
            "codex_wait_reopen_outcome_invalid",
            "reopen outcome is outside the closed vocabulary",
        )
    if (
        not isinstance(row["invocation_id"], str)
        or not 1 <= len(row["invocation_id"]) <= 128
    ):
        raise ProtocolRefusal(
            "codex_wait_reopen_invocation_invalid",
            "reopen invocation id is out of bounds",
        )
    return row


class CodexWaitReopenLedger:
    """Typed, bounded, append-only testimony that a watched ledger was replaced.

    This is waiter state and not bus evidence: the bus record vocabulary is
    closed in ``floati/records.py``, and admitting ``codex_wait_reopen_fact``
    there is a separate row.  The shape is validated on write and on read, so
    the file is a contract rather than a log.
    """

    _MAX_BYTES = 65536

    def __init__(self, root: FloatiRoot) -> None:
        self.root = root

    @staticmethod
    def _relative(node_id: str) -> Path:
        return Path("state/codex-wait") / node_id / "reopen.jsonl"

    def path(self, node_id: str) -> Path:
        return self.root.resolve_relative(
            self._relative(validate_identifier(node_id, "node"))
        )

    def record(
        self,
        *,
        node_id: str,
        session_digest: str,
        ledger: str,
        before: LedgerIdentity,
        after: Optional[LedgerIdentity],
        waited_seconds: int,
        outcome: str,
        invocation_id: str,
    ) -> Dict[str, object]:
        node = validate_identifier(node_id, "node")
        row: Dict[str, object] = {
            "schema_version": 1,
            "kind": CODEX_WAIT_REOPEN_KIND,
            "tenant_id": self.root.tenant_id,
            "timestamp": utc_now(),
            "node_id": node,
            "session_digest": session_digest,
            "ledger": ledger,
            "before": None if before is None else dict(before),
            "after": None if after is None else dict(after),
            "waited_seconds": waited_seconds,
            "outcome": outcome,
            "invocation_id": invocation_id,
        }
        validate_reopen_fact(row, self.root.tenant_id)
        path = self.path(node)
        encoded = (
            json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode("utf-8")
        try:
            existing = path.stat().st_size
        except OSError:
            existing = 0
        if existing + len(encoded) > self._MAX_BYTES:
            raise ProtocolRefusal(
                "codex_wait_reopen_ledger_full",
                "reopen testimony for this node is at its bound",
            )
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(
            path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600
        )
        try:
            if os.write(descriptor, encoded) != len(encoded):
                raise ProtocolRefusal(
                    "codex_wait_reopen_write_short", "reopen testimony was truncated"
                )
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        return row

    def read(self, node_id: str) -> List[Dict[str, object]]:
        path = self.path(node_id)
        try:
            data = path.read_bytes()
        except OSError:
            return []
        if len(data) > self._MAX_BYTES:
            raise ProtocolRefusal(
                "codex_wait_reopen_ledger_full", "reopen testimony exceeds its bound"
            )
        rows: List[Dict[str, object]] = []
        for raw in data.decode("utf-8").splitlines():
            if not raw:
                raise ProtocolRefusal(
                    "codex_wait_reopen_frame_invalid", "reopen testimony has a blank frame"
                )
            try:
                decoded = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise ProtocolRefusal(
                    "codex_wait_reopen_frame_invalid", "reopen testimony is not JSON"
                ) from exc
            rows.append(validate_reopen_fact(decoded, self.root.tenant_id))
        return rows


@dataclass(frozen=True)
class WorkspaceBinding:
    workspace: Path
    node_id: str
    map_digest: str


@dataclass(frozen=True)
class CodexWaitParticipant:
    binding: WorkspaceBinding
    root: FloatiRoot


def _contained(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def resolve_workspace_binding(bus_home: Path, workspace: Path) -> Optional[WorkspaceBinding]:
    """Resolve one workspace without consulting ambient identity or transport state."""

    home = Path(bus_home).expanduser()
    current = Path(workspace).expanduser()
    if not home.is_absolute() or home.is_symlink() or not current.is_absolute():
        return None
    map_path = home / WORKSPACE_MAP_RELATIVE
    try:
        encoded = map_path.read_bytes()
        raw = json.loads(encoded)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict) or set(raw) != {"schema_version", "tenant_id", "mappings"}:
        return None
    if raw["schema_version"] != 0 or isinstance(raw["schema_version"], bool):
        return None
    try:
        tenant = validate_identifier(raw["tenant_id"], "tenant")
    except Exception:
        return None
    if tenant != home.name or not isinstance(raw["mappings"], list):
        return None
    try:
        resolved_current = current.resolve(strict=True)
    except OSError:
        return None
    candidates: list[tuple[int, Path, str]] = []
    prior: tuple[str, str] | None = None
    for entry in raw["mappings"]:
        if not isinstance(entry, dict) or set(entry) != {"workspace", "node_id"}:
            return None
        coordinate = entry["workspace"]
        if not isinstance(coordinate, str):
            return None
        lexical = Path(coordinate).expanduser()
        if not lexical.is_absolute() or lexical.is_symlink():
            return None
        try:
            canonical = lexical.resolve(strict=True)
            node = validate_identifier(entry["node_id"], "node")
        except (OSError, ProtocolRefusal):
            return None
        ordering = (canonical.as_posix(), node)
        if prior is not None and ordering <= prior:
            return None
        prior = ordering
        if _contained(resolved_current, canonical):
            candidates.append((len(canonical.parts), canonical, node))
    if not candidates:
        return None
    _depth, canonical, node = max(candidates, key=lambda item: item[0])
    return WorkspaceBinding(
        workspace=canonical,
        node_id=node,
        map_digest=hashlib.sha256(encoded).hexdigest(),
    )


def resolve_participant(bus_home: Path, workspace: Path) -> Optional[CodexWaitParticipant]:
    """Resolve a mapped workspace through the same active registry as send."""

    binding = resolve_workspace_binding(bus_home, workspace)
    if binding is None:
        return None
    try:
        root = FloatiRoot.open_direct_home(bus_home)
        node = Registry(root).resolve_node_id(binding.node_id, field="node")
    except ProtocolRefusal:
        return None
    return CodexWaitParticipant(
        binding=WorkspaceBinding(binding.workspace, node, binding.map_digest),
        root=root,
    )


class CodexWaitConsentLedger:
    """Append-only per-node consent for one mapped Codex workspace."""

    _KINDS = frozenset({"codex_wait_consent_receipt"})

    def __init__(self, root: FloatiRoot) -> None:
        self.root = root

    @staticmethod
    def _relative(node_id: str) -> Path:
        return consent_ledger_relative(node_id)

    def arm(
        self,
        binding: WorkspaceBinding,
        *,
        hook_timeout_seconds: int,
        wait_deadline_seconds: int,
        idempotency_key: str,
    ) -> Dict[str, object]:
        node = Registry(self.root).resolve_node_id(binding.node_id, field="node")
        if (
            not isinstance(hook_timeout_seconds, int)
            or isinstance(hook_timeout_seconds, bool)
            or not isinstance(wait_deadline_seconds, int)
            or isinstance(wait_deadline_seconds, bool)
            or not 0 < wait_deadline_seconds < hook_timeout_seconds <= 86400
        ):
            raise ProtocolRefusal(
                "wait_deadline_invalid",
                "wait deadline must be positive and strictly below hook timeout",
            )
        if not isinstance(idempotency_key, str) or not 1 <= len(idempotency_key) <= 128:
            raise ProtocolRefusal("idempotency_key_invalid", "idempotency key is out of bounds")
        row: Dict[str, object] = {
            "schema_version": 1,
            "id": "codex-wait-consent-" + uuid7_hex(),
            "tenant_id": self.root.tenant_id,
            "timestamp": utc_now(),
            "kind": "codex_wait_consent_receipt",
            "node_id": node,
            "workspace": binding.workspace.as_posix(),
            "workspace_map_digest": binding.map_digest,
            "hook_timeout_seconds": hook_timeout_seconds,
            "wait_deadline_seconds": wait_deadline_seconds,
            "state": "armed",
            "idempotency_key": idempotency_key,
        }

        def decide(prior: list[Dict[str, object]]) -> tuple[Dict[str, object], Optional[Dict[str, object]]]:
            for existing in prior:
                if existing.get("idempotency_key") != idempotency_key:
                    continue
                fields = (
                    "node_id", "workspace",
                    "hook_timeout_seconds", "wait_deadline_seconds", "state",
                )
                if all(existing.get(field) == row[field] for field in fields):
                    return existing, None
                raise ProtocolRefusal(
                    "codex_wait_consent_idempotency_conflict",
                    "consent key has different content",
                )
            return row, row

        return transact(
            self.root,
            self._relative(node),
            decide,
            allowed_kinds=set(self._KINDS),
        )

    def require_armed(self, binding: WorkspaceBinding) -> Dict[str, object]:
        node = Registry(self.root).resolve_node_id(binding.node_id, field="node")
        rows = read_records_snapshot(
            self.root,
            self._relative(node),
            allowed_kinds=set(self._KINDS),
        )
        matching = [
            row
            for row in rows
            if row.get("workspace") == binding.workspace.as_posix()
        ]
        if not matching or matching[-1].get("state") != "armed":
            raise ProtocolRefusal(
                "codex_wait_consent_missing",
                "workspace has no current consent receipt",
            )
        return matching[-1]


class CodexWaitSessionLedger:
    """Append-only authority for the one armed session of a Codex binding."""

    _KINDS = frozenset({"codex_wait_session_receipt"})

    def __init__(self, root: FloatiRoot) -> None:
        self.root = root

    @staticmethod
    def _relative(node_id: str) -> Path:
        return Path("receipts/codex-wait-session") / f"{node_id}.jsonl"

    def _identity(
        self,
        binding: WorkspaceBinding,
        consent: Dict[str, object],
        session_id: object,
    ) -> tuple[str, str]:
        from .wake_control import validate_session_id

        node = Registry(self.root).resolve_node_id(binding.node_id, field="node")
        session = validate_session_id(session_id)
        if (
            consent.get("kind") != "codex_wait_consent_receipt"
            or consent.get("tenant_id") != self.root.tenant_id
            or consent.get("node_id") != node
            or consent.get("workspace") != binding.workspace.as_posix()
            or consent.get("state") != "armed"
            or not isinstance(consent.get("id"), str)
        ):
            raise ProtocolRefusal(
                "codex_wait_consent_mismatch",
                "armed-session authority requires the current binding consent",
            )
        return node, session

    @staticmethod
    def _matching(
        prior: list[Dict[str, object]], binding: WorkspaceBinding
    ) -> list[Dict[str, object]]:
        return [
            row
            for row in prior
            if row.get("workspace") == binding.workspace.as_posix()
        ]

    @staticmethod
    def _key(value: object) -> str:
        if not isinstance(value, str) or not 1 <= len(value) <= 128:
            raise ProtocolRefusal(
                "idempotency_key_invalid", "idempotency key is out of bounds"
            )
        return value

    def arm(
        self,
        binding: WorkspaceBinding,
        consent: Dict[str, object],
        session_id: object,
        *,
        idempotency_key: str,
    ) -> Dict[str, object]:
        node, session = self._identity(binding, consent, session_id)
        key = self._key(idempotency_key)

        def decide(
            prior: list[Dict[str, object]],
        ) -> tuple[Dict[str, object], Optional[Dict[str, object]]]:
            for existing in prior:
                if existing.get("idempotency_key") != key:
                    continue
                if (
                    existing.get("node_id") == node
                    and existing.get("workspace") == binding.workspace.as_posix()
                    and existing.get("acting_session_id") == session
                    and existing.get("consent_receipt_id") == consent["id"]
                ):
                    return existing, None
                raise ProtocolRefusal(
                    "codex_wait_session_idempotency_conflict",
                    "armed-session key has different content",
                )
            matching = self._matching(prior, binding)
            predecessor = matching[-1] if matching else None
            row: Dict[str, object] = {
                "schema_version": 1,
                "id": "codex-wait-session-" + uuid7_hex(),
                "tenant_id": self.root.tenant_id,
                "timestamp": utc_now(),
                "kind": "codex_wait_session_receipt",
                "node_id": node,
                "workspace": binding.workspace.as_posix(),
                "workspace_map_digest": binding.map_digest,
                "acting_session_id": session,
                "operation": "arm" if predecessor is None else "takeover",
                "state": "armed",
                "predecessor_receipt_id": (
                    None if predecessor is None else predecessor["id"]
                ),
                "consent_receipt_id": consent["id"],
                "idempotency_key": key,
            }
            return row, row

        return transact(
            self.root,
            self._relative(node),
            decide,
            allowed_kinds=set(self._KINDS),
        )

    def participate(
        self,
        binding: WorkspaceBinding,
        consent: Dict[str, object],
        session_id: object,
    ) -> Optional[Dict[str, object]]:
        node, session = self._identity(binding, consent, session_id)
        key = "codex-wait-claim-" + hashlib.sha256(
            (node + "\0" + binding.workspace.as_posix() + "\0" + session).encode("utf-8")
        ).hexdigest()[:32]

        def decide(
            prior: list[Dict[str, object]],
        ) -> tuple[Optional[Dict[str, object]], Optional[Dict[str, object]]]:
            matching = self._matching(prior, binding)
            if matching:
                current = matching[-1]
                if current.get("acting_session_id") == session:
                    return current, None
                return None, None
            row: Dict[str, object] = {
                "schema_version": 1,
                "id": "codex-wait-session-" + uuid7_hex(),
                "tenant_id": self.root.tenant_id,
                "timestamp": utc_now(),
                "kind": "codex_wait_session_receipt",
                "node_id": node,
                "workspace": binding.workspace.as_posix(),
                "workspace_map_digest": binding.map_digest,
                "acting_session_id": session,
                "operation": "claim",
                "state": "armed",
                "predecessor_receipt_id": None,
                "consent_receipt_id": consent["id"],
                "idempotency_key": key,
            }
            return row, row

        return transact(
            self.root,
            self._relative(node),
            decide,
            allowed_kinds=set(self._KINDS),
        )


class CodexWaitReceiptLedger:
    """Append bounded waiter lifecycle evidence without implying delivery."""

    _KINDS = frozenset({"codex_wait_exhaustion_receipt"})

    def __init__(self, root: FloatiRoot) -> None:
        self.root = root

    def record_exhaustion(
        self,
        *,
        node_id: str,
        session_digest: str,
        waited_seconds: int,
        idempotency_key: str,
    ) -> Dict[str, object]:
        node = Registry(self.root).resolve_node_id(node_id, field="node")
        row: Dict[str, object] = {
            "schema_version": 1,
            "id": "codex-wait-exhaustion-" + uuid7_hex(),
            "tenant_id": self.root.tenant_id,
            "timestamp": utc_now(),
            "kind": "codex_wait_exhaustion_receipt",
            "node_id": node,
            "session_digest": session_digest,
            "waited_seconds": waited_seconds,
            "outcome": "rearmed",
            "idempotency_key": idempotency_key,
        }
        relative = Path("receipts/codex-wait-exhaustion") / f"{node}.jsonl"

        def decide(prior: list[Dict[str, object]]) -> tuple[Dict[str, object], Optional[Dict[str, object]]]:
            for existing in prior:
                if existing.get("idempotency_key") != idempotency_key:
                    continue
                fields = ("node_id", "session_digest", "waited_seconds", "outcome")
                if all(existing.get(field) == row[field] for field in fields):
                    return existing, None
                raise ProtocolRefusal(
                    "codex_wait_exhaustion_idempotency_conflict",
                    "exhaustion key has different content",
                )
            return row, row

        return transact(
            self.root,
            relative,
            decide,
            allowed_kinds=set(self._KINDS),
        )

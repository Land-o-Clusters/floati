"""Closed Floati-owned identity and consent contracts for the Codex waiter."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional

from .errors import ProtocolRefusal
from .ids import uuid7_hex
from .jsonl import read_records_snapshot, transact
from .registry import Registry, utc_now
from .root import FloatiRoot, validate_identifier


WORKSPACE_MAP_RELATIVE = Path("codex-wait/workspaces.v0.json")


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
        return Path("receipts/codex-wait-consent") / f"{node_id}.jsonl"

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

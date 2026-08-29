"""Version-zero local gateway contracts and dark append-only implementation."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional, Sequence

from .errors import ProtocolRefusal
from .ids import uuid7_hex
from .jsonl import append_record, read_records_snapshot
from .root import FloatiRoot


GATEWAY_KINDS = {
    "gateway_session_ingress",
    "gateway_capability_declaration",
    "gateway_approval_forward",
}


def _timestamp(now: Optional[datetime]) -> str:
    current = datetime.now(timezone.utc) if now is None else now
    if current.tzinfo is None or current.utcoffset() is None:
        raise ProtocolRefusal("time_invalid", "an aware UTC-compatible datetime is required")
    return current.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _absolute_lexical(value: object) -> Optional[Path]:
    if not isinstance(value, str) or not 2 <= len(value) <= 1024:
        return None
    path = Path(value)
    if not path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts[1:]):
        return None
    return path


def _ordered_strings(value: object, code: str) -> list[str]:
    if (
        not isinstance(value, (list, tuple))
        or not value
        or any(not isinstance(item, str) for item in value)
    ):
        raise ProtocolRefusal(code, "gateway list must contain bounded strings")
    return sorted(set(value))


@dataclass(frozen=True)
class GatewayConfig:
    path: Path
    workspace_root: Path
    transport: str = "stdio"
    network: str = "disabled"
    approval_mode: str = "forward_fail_closed"
    schema_version: int = 0

    @classmethod
    def load(cls, path: Path | str) -> "GatewayConfig":
        candidate = Path(path).expanduser()
        if not candidate.is_absolute() or candidate.is_symlink():
            raise ProtocolRefusal(
                "gateway_config_identity_invalid",
                "gateway config must be an explicit absolute non-symlink file",
            )
        if not candidate.is_file():
            raise ProtocolRefusal("gateway_config_missing", "gateway config file is absent")
        try:
            raw = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProtocolRefusal("gateway_config_malformed", "gateway config is not valid JSON") from exc
        expected = {
            "schema_version",
            "kind",
            "transport",
            "network",
            "workspace_root",
            "approval_mode",
        }
        workspace_root = _absolute_lexical(raw.get("workspace_root") if isinstance(raw, dict) else None)
        if (
            not isinstance(raw, dict)
            or set(raw) != expected
            or raw.get("schema_version") != 0
            or isinstance(raw.get("schema_version"), bool)
            or raw.get("kind") != "local_gateway_config"
            or raw.get("transport") != "stdio"
            or raw.get("network") != "disabled"
            or raw.get("approval_mode") != "forward_fail_closed"
            or workspace_root is None
        ):
            raise ProtocolRefusal(
                "gateway_config_malformed",
                "gateway config must be exact local stdio version zero",
            )
        return cls(candidate, workspace_root)


class LocalGatewayV0:
    """No listener or transport: only explicit local contract records."""

    relative_path = Path("gateway/events.jsonl")

    def __init__(self, root: FloatiRoot, config: GatewayConfig) -> None:
        if not isinstance(root, FloatiRoot) or not isinstance(config, GatewayConfig):
            raise ProtocolRefusal("gateway_configuration_invalid", "validated root and gateway config are required")
        self.root = root
        self.config = config

    def records(self) -> list[Dict[str, object]]:
        return read_records_snapshot(
            self.root, self.relative_path, allowed_kinds=GATEWAY_KINDS
        )

    def ingress(
        self,
        session_id: str,
        actor: str,
        workspace: Path | str,
        *,
        now: Optional[datetime] = None,
    ) -> Dict[str, object]:
        path = _absolute_lexical(str(workspace))
        if path is None or not self._confined(path):
            raise ProtocolRefusal(
                "gateway_workspace_outside_root",
                "gateway workspace must be lexically below the configured root",
            )
        if any(row["kind"] == "gateway_session_ingress" and row["session_id"] == session_id for row in self.records()):
            raise ProtocolRefusal("gateway_session_duplicate", "gateway session already entered")
        record: Dict[str, object] = {
            "schema_version": 0,
            "id": "gateway-ingress-" + uuid7_hex(),
            "tenant_id": self.root.tenant_id,
            "timestamp": _timestamp(now),
            "kind": "gateway_session_ingress",
            "gateway_version": 0,
            "session_id": session_id,
            "actor": actor,
            "workspace": str(path),
            "transport": "stdio",
        }
        self._append(record)
        return record

    def declare(
        self,
        session_id: str,
        capabilities: Sequence[str],
        *,
        now: Optional[datetime] = None,
    ) -> Dict[str, object]:
        records = self.records()
        if not self._has_session(records, session_id):
            raise ProtocolRefusal("gateway_session_missing", "capability declaration requires session ingress")
        ordered = _ordered_strings(capabilities, "gateway_capabilities_invalid")
        record: Dict[str, object] = {
            "schema_version": 0,
            "id": "gateway-capability-" + uuid7_hex(),
            "tenant_id": self.root.tenant_id,
            "timestamp": _timestamp(now),
            "kind": "gateway_capability_declaration",
            "gateway_version": 0,
            "session_id": session_id,
            "capabilities": ordered,
        }
        self._append(record)
        return record

    def forward_approval(
        self,
        session_id: str,
        request_id: str,
        capability: str,
        scope: Sequence[str],
        *,
        now: Optional[datetime] = None,
    ) -> Dict[str, object]:
        records = self.records()
        declared = {
            item
            for row in records
            if row["kind"] == "gateway_capability_declaration" and row["session_id"] == session_id
            for item in row["capabilities"]
        }
        if not self._has_session(records, session_id):
            raise ProtocolRefusal("gateway_session_missing", "approval forwarding requires session ingress")
        if capability not in declared or "approval.forward" not in declared:
            raise ProtocolRefusal("gateway_capability_missing", "approval forwarding requires declared capabilities")
        ordered_scope = _ordered_strings(scope, "gateway_scope_invalid")
        record: Dict[str, object] = {
            "schema_version": 0,
            "id": "gateway-approval-" + uuid7_hex(),
            "tenant_id": self.root.tenant_id,
            "timestamp": _timestamp(now),
            "kind": "gateway_approval_forward",
            "gateway_version": 0,
            "session_id": session_id,
            "request_id": request_id,
            "capability": capability,
            "scope": ordered_scope,
            "state": "forwarded_unresolved",
        }
        self._append(record)
        return record

    def _append(self, record: Dict[str, object]) -> None:
        append_record(
            self.root, self.relative_path, record, allowed_kinds=GATEWAY_KINDS
        )

    def _confined(self, path: Path) -> bool:
        root_parts = self.config.workspace_root.parts
        return path.parts[: len(root_parts)] == root_parts and path != self.config.workspace_root

    @staticmethod
    def _has_session(records: Sequence[Dict[str, object]], session_id: str) -> bool:
        return any(
            row["kind"] == "gateway_session_ingress" and row["session_id"] == session_id
            for row in records
        )

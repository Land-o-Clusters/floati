"""Receipted, marker-only control for exactly one harness session."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, Optional

from .errors import IntegrityFailure, ProtocolRefusal
from .ids import uuid7_hex
from .jsonl import transact
from .registry import Registry, utc_now
from .root import FloatiRoot


_SESSION = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._:-]{0,255})$")
_RESERVED = frozenset({"all", "global"})
_KINDS = {"wake_control_receipt"}
_MARKER_FIELDS = {
    "schema_version", "node_id", "session_digest", "state", "paused_at", "receipt_id",
}


def validate_session_id(value: object) -> str:
    if (
        not isinstance(value, str)
        or _SESSION.fullmatch(value) is None
        or value.casefold() in _RESERVED
    ):
        raise ProtocolRefusal(
            "wake_session_invalid",
            "wake control requires one exact terminal-safe session id; global and wildcard selectors are forbidden",
        )
    return value


def _digest(session_id: str) -> str:
    return hashlib.sha256(session_id.encode("utf-8")).hexdigest()


class WakeController:
    """Control one node/session marker without editing hook registration."""

    def __init__(self, root: FloatiRoot) -> None:
        if not isinstance(root, FloatiRoot):
            raise ProtocolRefusal("wake_control_root_invalid", "wake control requires a validated root")
        self.root = root

    def _identity(self, node_id: str, session_id: str) -> tuple[str, str, str]:
        node = Registry(self.root).resolve_node_id(node_id, field="node")
        session = validate_session_id(session_id)
        return node, session, _digest(session)

    def _marker(self, node: str, session_digest: str) -> Path:
        return self.root.resolve_relative(
            Path("state/wake-control") / node / f"{session_digest}.json"
        )

    def _ledger(self, node: str) -> Path:
        return Path("receipts/wake-control") / f"{node}.jsonl"

    @contextmanager
    def _lock(self, node: str) -> Iterator[None]:
        path = self.root.resolve_relative(Path("state/wake-control") / f"{node}.lock")
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with path.open("a+b") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    @staticmethod
    def _encode_marker(marker: Dict[str, Any]) -> bytes:
        return (json.dumps(marker, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")

    def _write_marker(self, path: Path, marker: Dict[str, Any]) -> None:
        if path.is_symlink() or path.exists():
            raise ProtocolRefusal("wake_session_already_paused", "session already has a pause marker")
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid7_hex()}.tmp")
        encoded = self._encode_marker(marker)
        descriptor = -1
        try:
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            if os.write(descriptor, encoded) != len(encoded):
                raise OSError("short marker write")
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = -1
            os.replace(temporary, path)
        except OSError as exc:
            if descriptor >= 0:
                os.close(descriptor)
            try:
                temporary.unlink()
            except OSError:
                pass
            raise ProtocolRefusal("wake_marker_unavailable", "pause marker could not be committed") from exc

    def _read_marker(self, path: Path, node: str, session_digest: str) -> Optional[Dict[str, Any]]:
        if path.is_symlink():
            raise IntegrityFailure("wake_marker_invalid", "pause marker must not be a symlink")
        if not path.exists():
            return None
        try:
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            try:
                raw = os.read(descriptor, 8193)
            finally:
                os.close(descriptor)
            if len(raw) > 8192:
                raise ValueError("oversized")
            marker = json.loads(raw.decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise IntegrityFailure("wake_marker_invalid", "pause marker is not valid bounded JSON") from exc
        if (
            not isinstance(marker, dict)
            or set(marker) != _MARKER_FIELDS
            or marker.get("schema_version") != 0
            or marker.get("node_id") != node
            or marker.get("session_digest") != session_digest
            or marker.get("state") != "paused"
            or not isinstance(marker.get("paused_at"), str)
            or not isinstance(marker.get("receipt_id"), str)
        ):
            raise IntegrityFailure("wake_marker_invalid", "pause marker identity or shape is invalid")
        return marker

    def _receipt(
        self,
        *,
        node: str,
        session_digest: str,
        operation: str,
        state: str,
        predecessor: Optional[str],
        idempotency_key: str,
    ) -> Dict[str, Any]:
        if not isinstance(idempotency_key, str) or not 1 <= len(idempotency_key) <= 128:
            raise ProtocolRefusal("idempotency_key_invalid", "idempotency key is out of bounds")
        return {
            "schema_version": 0,
            "id": "wake-control-" + uuid7_hex(),
            "tenant_id": self.root.tenant_id,
            "timestamp": utc_now(),
            "kind": "wake_control_receipt",
            "node_id": node,
            "session_digest": session_digest,
            "operation": operation,
            "state": state,
            "predecessor_receipt_id": predecessor,
            "idempotency_key": idempotency_key,
        }

    def _append(self, node: str, row: Dict[str, Any]) -> Dict[str, Any]:
        def decide(prior: list[Dict[str, Any]]):
            for existing in prior:
                if existing.get("idempotency_key") != row["idempotency_key"]:
                    continue
                fields = (
                    "node_id", "session_digest", "operation", "state",
                    "predecessor_receipt_id",
                )
                if all(existing.get(field) == row[field] for field in fields):
                    return existing, None
                raise ProtocolRefusal(
                    "wake_control_idempotency_conflict",
                    "wake control key has different content",
                )
            return row, row

        return transact(self.root, self._ledger(node), decide, allowed_kinds=_KINDS)

    @staticmethod
    def _artifact(
        *, node: str, session_digest: str, state: str, marker: Path,
        receipt: Optional[Dict[str, Any]], paused_at: Optional[str],
    ) -> Dict[str, Any]:
        paused = state == "paused"
        return {
            "schema_version": 0,
            "node_id": node,
            "session_digest": session_digest,
            "state": state,
            "paused_by": node if paused else None,
            "paused_at": paused_at if paused else None,
            "marker": str(marker),
            "receipt": receipt,
            "cached_session_state": "unknown",
            "harness_trust_gate": "unknown",
            "display": (
                f"paused by you at {paused_at}; the running session's cached state and harness trust gate are unknown."
                if paused
                else "wake monitoring is active; the running session's cached state and harness trust gate are unknown."
            ),
        }

    def pause(self, node_id: str, session_id: str, *, idempotency_key: str) -> Dict[str, Any]:
        node, _session, session_digest = self._identity(node_id, session_id)
        marker_path = self._marker(node, session_digest)
        with self._lock(node):
            if self._read_marker(marker_path, node, session_digest) is not None:
                raise ProtocolRefusal("wake_session_already_paused", "session is already paused")
            row = self._receipt(
                node=node,
                session_digest=session_digest,
                operation="pause",
                state="paused",
                predecessor=None,
                idempotency_key=idempotency_key,
            )
            marker = {
                "schema_version": 0,
                "node_id": node,
                "session_digest": session_digest,
                "state": "paused",
                "paused_at": row["timestamp"],
                "receipt_id": row["id"],
            }
            self._write_marker(marker_path, marker)
            try:
                committed = self._append(node, row)
            except Exception:
                try:
                    marker_path.unlink()
                except OSError:
                    pass
                raise
        return self._artifact(
            node=node, session_digest=session_digest, state="paused",
            marker=marker_path, receipt=committed, paused_at=str(row["timestamp"]),
        )

    def resume(self, node_id: str, session_id: str, *, idempotency_key: str) -> Dict[str, Any]:
        node, _session, session_digest = self._identity(node_id, session_id)
        marker_path = self._marker(node, session_digest)
        with self._lock(node):
            marker = self._read_marker(marker_path, node, session_digest)
            if marker is None:
                raise ProtocolRefusal("wake_session_not_paused", "session has no pause marker")
            row = self._receipt(
                node=node,
                session_digest=session_digest,
                operation="resume",
                state="resume_requested",
                predecessor=str(marker["receipt_id"]),
                idempotency_key=idempotency_key,
            )
            committed = self._append(node, row)
            try:
                marker_path.unlink()
            except OSError as exc:
                raise ProtocolRefusal(
                    "wake_marker_unavailable",
                    "resume was receipted but the exact pause marker remains",
                ) from exc
        return self._artifact(
            node=node, session_digest=session_digest, state="active",
            marker=marker_path, receipt=committed, paused_at=None,
        )

    def status(self, node_id: str, session_id: str) -> Dict[str, Any]:
        node, _session, session_digest = self._identity(node_id, session_id)
        marker_path = self._marker(node, session_digest)
        marker = self._read_marker(marker_path, node, session_digest)
        return self._artifact(
            node=node,
            session_digest=session_digest,
            state="paused" if marker is not None else "active",
            marker=marker_path,
            receipt=None,
            paused_at=None if marker is None else str(marker["paused_at"]),
        )


def is_session_paused(root: FloatiRoot, node_id: str, session_id: str) -> bool:
    """Fail closed if an exact new-style marker is present, even if malformed."""

    node, _session, session_digest = WakeController(root)._identity(node_id, session_id)
    path = WakeController(root)._marker(node, session_digest)
    return path.exists() or path.is_symlink()

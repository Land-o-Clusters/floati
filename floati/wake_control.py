"""Receipted, marker-only control for exactly one harness session."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, Mapping, Optional

from .errors import IntegrityFailure, ProtocolRefusal
from .ids import uuid7_hex
from .jsonl import transact
from .registry import Registry, utc_now
from .root import FloatiRoot
from .session_identity import _RESERVED, _SESSION, validate_session_id


_KINDS = {"wake_control_receipt"}
_MARKER_FIELDS = {
    "schema_version", "node_id", "session_digest", "state", "paused_at", "receipt_id",
}


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
            os.fchmod(descriptor, 0o600)
            if os.write(descriptor, encoded) != len(encoded):
                raise OSError("short marker write")
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = -1
            from .codex_hook_install import _rename_generation_noreplace
            _rename_generation_noreplace(temporary, path)
            if path.read_bytes() != encoded:
                raise OSError("wake marker readback differs")
            parent = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(parent)
            finally:
                os.close(parent)
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

    def pause(self, node_id: str, session_id: str, *, idempotency_key: str,
              reserve_receipt=None) -> Dict[str, Any]:
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
            if reserve_receipt is not None:
                if not callable(reserve_receipt):
                    raise ProtocolRefusal("wake_pause_intent_invalid", "pause reservation requires a callable durable recorder",
                        remedy="supply the exact native reserved pause receipt or ID from the seat ownership record")
                from .records import validate_record
                validate_record(row, self.root.tenant_id, frozenset(_KINDS), integrity=False)
                # The caller cannot alter the native candidate after reservation.
                reserve_receipt(json.loads(json.dumps(row)))
            self._write_marker(marker_path, marker)
            try:
                committed = self._append(node, row)
            except Exception:
                if reserve_receipt is None:
                    try:
                        marker_path.unlink()
                    except OSError:
                        pass
                # Reserved callers have durable exact intent and can reconcile;
                # do not unlink a possibly superseding marker on their behalf.
                raise
            from .wake_hints import publish_recipient_hint
            publish_recipient_hint(self.root, node)
        return self._artifact(
            node=node, session_digest=session_digest, state="paused",
            marker=marker_path, receipt=committed, paused_at=str(row["timestamp"]),
        )

    def reconcile_pause(self, node_id: str, session_id: str, *, expected_receipt: dict) -> Optional[dict]:
        """Receipt only the exact reserved pause marker left by an interrupted pause."""
        from .records import validate_record
        from .jsonl import read_records_snapshot
        import stat

        node, _session, session_digest = self._identity(node_id, session_id)
        try:
            row = validate_record(expected_receipt, self.root.tenant_id,
                frozenset(_KINDS), integrity=False)
            if row["node_id"] != node or row["session_digest"] != session_digest or row["operation"] != "pause" or row["state"] != "paused" or row["predecessor_receipt_id"] is not None:
                raise ValueError("reserved pause belongs to another coordinate or operation")
        except (ProtocolRefusal, ValueError, KeyError, TypeError) as exc:
            raise ProtocolRefusal("wake_pause_intent_invalid", "reserved native pause receipt is invalid",
                remedy="supply the exact native reserved pause receipt or ID from the seat ownership record") from exc
        marker_path = self._marker(node, session_digest)
        with self._lock(node):
            quarantine = self._resume_quarantine(marker_path, row["id"])
            if not marker_path.exists() and not marker_path.is_symlink() and (quarantine.exists() or quarantine.is_symlink()):
                marker_path = quarantine
                parent = os.open(marker_path.parent, os.O_RDONLY)
                try:
                    os.fsync(parent)
                finally:
                    os.close(parent)
            marker = self._read_marker(marker_path, node, session_digest)
            if marker is None:
                return None  # Do not recreate an absent capability from old intent.
            expected_marker = {"schema_version": 0, "node_id": node,
                "session_digest": session_digest, "state": "paused",
                "paused_at": row["timestamp"], "receipt_id": row["id"]}
            metadata = marker_path.lstat()
            if marker != expected_marker or marker_path.read_bytes() != self._encode_marker(expected_marker) or not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) != 0o600:
                raise ProtocolRefusal("wake_pause_intent_conflict", "pause marker differs from the exact reserved native receipt; preserve it",
                    remedy="preserve the current marker and native ledger; inspect superseding evidence before removing the seat")
            descriptor = os.open(marker_path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            try:
                opened = os.fstat(descriptor)
                if (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino) or os.read(descriptor, 8193) != self._encode_marker(expected_marker):
                    raise ProtocolRefusal("wake_pause_intent_conflict", "reserved marker changed before durable reconciliation",
                        remedy="preserve the current marker and native ledger; inspect superseding evidence before removing the seat")
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            parent = os.open(marker_path.parent, os.O_RDONLY)
            try:
                os.fsync(parent)
            finally:
                os.close(parent)
            prior = read_records_snapshot(self.root, self._ledger(node), allowed_kinds=_KINDS)
            matches = [existing for existing in prior if existing["id"] == row["id"] or existing["idempotency_key"] == row["idempotency_key"]]
            if matches and (len(matches) != 1 or matches[0] != row):
                raise ProtocolRefusal("wake_pause_intent_conflict", "native pause ID or key has conflicting evidence; preserve it",
                    remedy="preserve the current marker and native ledger; inspect superseding evidence before removing the seat")
            return matches[0] if matches else self._append(node, row)

    @staticmethod
    def _resume_quarantine(path: Path, receipt_id: str) -> Path:
        return path.with_name("." + path.name + ".resume-" + receipt_id)

    def resume(self, node_id: str, session_id: str, *, idempotency_key: str,
               expected_pause_receipt_id: Optional[str] = None) -> Dict[str, Any]:
        if expected_pause_receipt_id is not None and (not isinstance(expected_pause_receipt_id, str) or re.fullmatch(r"wake-control-[0-9a-f]{32}", expected_pause_receipt_id) is None):
            raise ProtocolRefusal("wake_pause_intent_invalid", "resume requires an exact native pause receipt ID",
                remedy="supply the exact native reserved pause receipt or ID from the seat ownership record")
        node, _session, session_digest = self._identity(node_id, session_id)
        marker_path = self._marker(node, session_digest)
        with self._lock(node):
            quarantine = None if expected_pause_receipt_id is None else self._resume_quarantine(marker_path, expected_pause_receipt_id)
            original_path = marker_path
            if quarantine is not None and (quarantine.exists() or quarantine.is_symlink()):
                if marker_path.exists() or marker_path.is_symlink():
                    raise ProtocolRefusal("wake_pause_intent_conflict", "pause and conditional removal staging both exist; preserve them",
                        remedy="preserve the current marker and native ledger; inspect superseding evidence before removing the seat")
                marker_path = quarantine
            marker = self._read_marker(marker_path, node, session_digest)
            if marker is None:
                raise ProtocolRefusal("wake_session_not_paused", "session has no pause marker")
            if expected_pause_receipt_id is not None and marker["receipt_id"] != expected_pause_receipt_id:
                raise ProtocolRefusal("wake_pause_intent_conflict", "resume predecessor was superseded; preserve the current pause marker",
                    remedy="preserve the current marker and native ledger; inspect superseding evidence before removing the seat")
            if expected_pause_receipt_id is not None:
                from .jsonl import read_records_snapshot
                import stat
                pauses = [entry for entry in read_records_snapshot(self.root, self._ledger(node), allowed_kinds=_KINDS)
                    if entry["id"] == expected_pause_receipt_id]
                metadata = marker_path.lstat()
                if len(pauses) != 1 or pauses[0]["operation"] != "pause" or pauses[0]["state"] != "paused" or pauses[0]["node_id"] != node or pauses[0]["session_digest"] != session_digest or pauses[0]["timestamp"] != marker["paused_at"] or marker_path.read_bytes() != self._encode_marker(marker) or not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) != 0o600:
                    raise ProtocolRefusal("wake_pause_intent_conflict", "conditional resume lacks the exact receipted pause marker",
                        remedy="preserve the current marker and native ledger; inspect superseding evidence before removing the seat")
                verified_identity = (metadata.st_dev, metadata.st_ino)
                verified_bytes = self._encode_marker(marker)
            if quarantine is not None and marker_path != quarantine:
                from .codex_hook_install import _rename_generation_noreplace
                metadata = marker_path.lstat()
                encoded = marker_path.read_bytes()
                _rename_generation_noreplace(marker_path, quarantine)
                moved = quarantine.lstat()
                if (moved.st_dev, moved.st_ino) != (metadata.st_dev, metadata.st_ino) or quarantine.read_bytes() != encoded:
                    try:
                        _rename_generation_noreplace(quarantine, original_path)
                    except OSError as exc:
                        raise ProtocolRefusal("wake_pause_intent_conflict", "replacement marker preserved at " + str(quarantine),
                            remedy="preserve the current marker and native ledger; inspect superseding evidence before removing the seat") from exc
                    raise ProtocolRefusal("wake_pause_intent_conflict", "replacement pause marker preserved without a resume receipt",
                        remedy="preserve the current marker and native ledger; inspect superseding evidence before removing the seat")
                marker_path = quarantine
            if quarantine is not None:
                parent = os.open(marker_path.parent, os.O_RDONLY)
                try:
                    os.fsync(parent)
                finally:
                    os.close(parent)
            row = self._receipt(
                node=node,
                session_digest=session_digest,
                operation="resume",
                state="resume_requested",
                predecessor=str(marker["receipt_id"]),
                idempotency_key=idempotency_key,
            )
            try:
                committed = self._append(node, row)
            except Exception:
                if quarantine is not None:
                    from .codex_hook_install import _rename_generation_noreplace
                    try:
                        _rename_generation_noreplace(quarantine, original_path)
                    except OSError:
                        pass
                raise
            try:
                if quarantine is not None:
                    current = marker_path.lstat()
                    if not stat.S_ISREG(current.st_mode) or current.st_uid != os.getuid() or stat.S_IMODE(current.st_mode) != 0o600 or (current.st_dev, current.st_ino) != verified_identity or marker_path.read_bytes() != verified_bytes:
                        raise ProtocolRefusal("wake_pause_intent_conflict", "conditional removal staging changed after the resume receipt; preserve it",
                            remedy="preserve the current marker and native ledger; inspect superseding evidence before removing the seat")
                marker_path.unlink()
                if quarantine is not None:
                    if marker_path.exists() or marker_path.is_symlink():
                        raise OSError("conditional pause removal readback differs")
                    parent = os.open(marker_path.parent, os.O_RDONLY)
                    try:
                        os.fsync(parent)
                    finally:
                        os.close(parent)
            except OSError as exc:
                raise ProtocolRefusal(
                    "wake_marker_unavailable",
                    "resume was receipted but the exact pause marker remains",
                ) from exc
            from .wake_hints import publish_recipient_hint
            publish_recipient_hint(self.root, node)
        return self._artifact(
            node=node, session_digest=session_digest, state="active",
            marker=original_path, receipt=committed, paused_at=None,
        )

    def status(self, node_id: str, session_id: str) -> Dict[str, Any]:
        from .wake_daemon import breaker_status_for_node

        node, _session, session_digest = self._identity(node_id, session_id)
        marker_path = self._marker(node, session_digest)
        marker = self._read_marker(marker_path, node, session_digest)
        artifact = self._artifact(
            node=node,
            session_digest=session_digest,
            state="paused" if marker is not None else "active",
            marker=marker_path,
            receipt=None,
            paused_at=None if marker is None else str(marker["paused_at"]),
        )
        breaker = breaker_status_for_node(self.root, node)
        artifact["breaker"] = breaker
        artifact["display"] = _display_with_breaker(artifact["display"], breaker)
        return artifact


def _display_with_breaker(display: str, breaker: Mapping[str, Any]) -> str:
    coordinates = breaker.get("coordinates")
    if not isinstance(coordinates, list) or not coordinates:
        clause = "breaker unbound"
    else:
        parts = []
        for row in coordinates:
            harness = row.get("harness")
            state = row.get("state")
            if state == "underivable":
                reason = row.get("reason") or "none"
                parts.append(f"{harness}=underivable reason={reason}")
                continue
            reason = row.get("last_trip_reason") or "none"
            parts.append(
                f"{harness}={state} threshold={breaker.get('threshold')} "
                f"last_trip_reason={reason}"
            )
        clause = "breaker " + "; ".join(parts)
    if "; " in display:
        head, tail = display.split("; ", 1)
        return f"{head}; {clause}; {tail}"
    return f"{display} {clause}"


def is_session_paused(root: FloatiRoot, node_id: str, session_id: str) -> bool:
    """Fail closed if an exact new-style marker is present, even if malformed."""

    node, _session, session_digest = WakeController(root)._identity(node_id, session_id)
    path = WakeController(root)._marker(node, session_digest)
    return path.exists() or path.is_symlink()

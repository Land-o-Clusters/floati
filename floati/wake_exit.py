"""Closed waiter-exit testimony for bounded Stop-hook invocations."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, Optional

from .errors import ProtocolRefusal
from .ids import uuid7_hex
from .jsonl import transact
from .records import validate_record
from .registry import Registry, utc_now
from .root import FloatiRoot


WAKE_EXIT_REASONS = frozenset(
    {"exhausted", "paused", "not_claimant", "breaker", "integrity_failure"}
)


class WakeExitLedger:
    """Append one idempotent, reason-complete waiter exit receipt."""

    _KINDS = frozenset({"wake_waiter_exit_receipt"})

    def __init__(self, root: FloatiRoot) -> None:
        self.root = root

    def record(
        self,
        *,
        node_id: str,
        session_digest: str,
        reason_code: str,
        waited_seconds: int,
        idempotency_key: str,
    ) -> Dict[str, object]:
        node = Registry(self.root).resolve_node_id(node_id, field="node")
        if reason_code not in WAKE_EXIT_REASONS:
            raise ProtocolRefusal(
                "wake_waiter_exit_reason_invalid",
                "waiter exit reason is outside the closed decline vocabulary",
            )
        if not isinstance(session_digest, str) or re.fullmatch(
            r"[0-9a-f]{64}", session_digest
        ) is None:
            raise ProtocolRefusal(
                "session_digest_invalid", "waiter exit session digest must be SHA-256"
            )
        if (
            not isinstance(waited_seconds, int)
            or isinstance(waited_seconds, bool)
            or not 0 <= waited_seconds <= 86399
        ):
            raise ProtocolRefusal(
                "waited_seconds_invalid", "waiter exit duration is out of bounds"
            )
        row: Dict[str, object] = {
            "schema_version": 1,
            "id": "wake-waiter-exit-" + uuid7_hex(),
            "tenant_id": self.root.tenant_id,
            "timestamp": utc_now(),
            "kind": "wake_waiter_exit_receipt",
            "node_id": node,
            "session_digest": session_digest,
            "reason_code": reason_code,
            "waited_seconds": waited_seconds,
            "idempotency_key": idempotency_key,
        }
        row = validate_record(row, self.root.tenant_id, self._KINDS, integrity=False)
        relative = Path("receipts/wake-waiter-exit") / f"{node}.jsonl"

        def decide(
            prior: list[Dict[str, object]],
        ) -> tuple[Dict[str, object], Optional[Dict[str, object]]]:
            for existing in prior:
                if existing.get("idempotency_key") != idempotency_key:
                    continue
                fields = (
                    "node_id", "session_digest", "reason_code", "waited_seconds"
                )
                if all(existing.get(field) == row[field] for field in fields):
                    return existing, None
                raise ProtocolRefusal(
                    "wake_waiter_exit_idempotency_conflict",
                    "waiter exit key has different content",
                )
            return row, row

        return transact(
            self.root, relative, decide, allowed_kinds=set(self._KINDS)
        )

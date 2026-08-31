"""Append-only optional per-node tide policy records."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Dict, Optional

from .errors import ProtocolRefusal
from .ids import uuid7_hex
from .jsonl import read_records_snapshot, transact
from .records import validate_record
from .registry import Registry, utc_now
from .root import FloatiRoot
from .tide_catalog import TideMetric, metric_for, policy_metric_for


TIDE_POLICY_KINDS = frozenset({"tide_policy_record"})
TIDE_TESTIMONY_KINDS = frozenset({"tide_testimony_record"})


def normalize_threshold(raw: object, metric: TideMetric) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise ProtocolRefusal("tide_threshold_invalid", "threshold must be numeric text")
    value = raw.strip()
    percent = value.endswith("%")
    try:
        number = Decimal(value[:-1] if percent else value)
        if percent:
            number /= Decimal(100)
    except (InvalidOperation, ValueError):
        raise ProtocolRefusal("tide_threshold_invalid", "threshold must be numeric text") from None
    if not number.is_finite() or number <= 0:
        raise ProtocolRefusal("tide_threshold_invalid", "threshold must be positive")
    if metric.value_kind == "fraction":
        if number > 1:
            raise ProtocolRefusal("tide_threshold_invalid", "fraction threshold must not exceed 100%")
        canonical = f"{number:.6f}"
        if canonical == "0.000000":
            raise ProtocolRefusal("tide_threshold_invalid", "fraction threshold is below canonical precision")
        return canonical
    if number != number.to_integral_value():
        raise ProtocolRefusal("tide_threshold_invalid", "proxy threshold must be an integer")
    return str(int(number))


class TidePolicyLedger:
    def __init__(self, root: FloatiRoot) -> None:
        if not isinstance(root, FloatiRoot):
            raise ProtocolRefusal("tide_root_invalid", "tide policy requires a validated root")
        self.root = root
        self.registry = Registry(root)

    @staticmethod
    def _relative(node: str) -> Path:
        return Path("receipts/tide-policy") / f"{node}.jsonl"

    def _rows(self, node: str) -> list[Dict[str, Any]]:
        return read_records_snapshot(
            self.root, self._relative(node), allowed_kinds=TIDE_POLICY_KINDS
        )

    def show(self, node_id: str) -> Optional[Dict[str, Any]]:
        node = self.registry.resolve_node_id(node_id, field="node")
        latest = self.latest(node)
        if latest is None or latest["state"] != "active":
            return None
        return latest

    def latest(self, node_id: str) -> Optional[Dict[str, Any]]:
        node = self.registry.resolve_node_id(node_id, field="node")
        rows = self._rows(node)
        return rows[-1] if rows else None

    def by_id(self, node_id: str, policy_id: object) -> Optional[Dict[str, Any]]:
        node = self.registry.resolve_node_id(node_id, field="node")
        if not isinstance(policy_id, str):
            return None
        return next(
            (row for row in reversed(self._rows(node)) if row["id"] == policy_id),
            None,
        )

    def _assert_mutable(self, node: str, idempotency_key: str) -> None:
        if any(row["idempotency_key"] == idempotency_key for row in self._rows(node)):
            return
        rows = read_records_snapshot(
            self.root,
            Path("receipts/tide") / f"{node}.jsonl",
            allowed_kinds=frozenset({"tide_receipt"}),
        )
        lifecycle = next(
            (
                row for row in reversed(rows)
                if row["evaluation_state"] in {"directed", "state_flushed"}
            ),
            None,
        )
        if lifecycle is not None and lifecycle["evaluation_state"] == "directed":
            raise ProtocolRefusal(
                "tide_directive_active",
                "complete the directed D5 state flush before changing its policy",
            )

    def set(
        self,
        node_id: str,
        metric: str,
        threshold: str,
        action: str,
        *,
        idempotency_key: str,
    ) -> Dict[str, Any]:
        active = self.registry.require_active(node_id)
        node = str(active["node_id"])
        selected = policy_metric_for(active["role"], metric)
        if action not in {"recommend", "direct"}:
            raise ProtocolRefusal(
                "tide_action_not_supported",
                "T1 authorizes recommend or direct; no native non-interactive compact verb was measured",
            )
        key = self._key(idempotency_key)
        self._assert_mutable(node, key)
        latest = self.latest(node)
        return self._append({
            "schema_version": 1 if selected.name == "quota_fraction" else 0,
            "id": "tide-policy-" + uuid7_hex(),
            "tenant_id": self.root.tenant_id,
            "timestamp": utc_now(),
            "kind": "tide_policy_record",
            "node_id": node,
            "harness": selected.harness,
            "metric": selected.name,
            "threshold": normalize_threshold(threshold, selected),
            "action": action,
            "access_class": selected.access_class,
            "stamp": selected.stamp,
            "formula": selected.formula,
            "receipt_path": selected.receipt_path,
            "state": "active",
            "predecessor_policy_id": latest["id"] if latest else None,
            "idempotency_key": key,
        })

    def clear(self, node_id: str, *, idempotency_key: str) -> Dict[str, Any]:
        node = self.registry.resolve_node_id(node_id, field="node")
        key = self._key(idempotency_key)
        retry = next(
            (row for row in reversed(self._rows(node)) if row["idempotency_key"] == key),
            None,
        )
        if retry is not None:
            if retry["state"] == "cleared":
                return retry
            raise ProtocolRefusal("tide_policy_idempotency_conflict", "tide policy key has different content")
        self._assert_mutable(node, key)
        active = self.show(node_id)
        if active is None:
            raise ProtocolRefusal("tide_policy_absent", "node has no active tide policy")
        row = dict(active)
        row.update({
            "id": "tide-policy-" + uuid7_hex(),
            "timestamp": utc_now(),
            "state": "cleared",
            "predecessor_policy_id": active["id"],
            "idempotency_key": key,
        })
        return self._append(row)

    def _append(self, row: Dict[str, Any]) -> Dict[str, Any]:
        semantic = tuple(
            key for key in row
            if key not in {"id", "timestamp", "predecessor_policy_id"}
        )

        def decide(prior: list[Dict[str, Any]]):
            matches = [item for item in prior if item["idempotency_key"] == row["idempotency_key"]]
            if matches:
                existing = matches[-1]
                if all(existing.get(key) == row.get(key) for key in semantic):
                    return existing, None
                raise ProtocolRefusal("tide_policy_idempotency_conflict", "tide policy key has different content")
            latest = prior[-1] if prior else None
            expected = None if latest is None else latest["id"]
            if row["predecessor_policy_id"] != expected:
                raise ProtocolRefusal("tide_policy_predecessor_stale", "tide policy predecessor is stale")
            validate_record(row, self.root.tenant_id, TIDE_POLICY_KINDS, integrity=False)
            return row, row

        return transact(
            self.root, self._relative(str(row["node_id"])), decide,
            allowed_kinds=TIDE_POLICY_KINDS,
        )

    @staticmethod
    def _key(value: object) -> str:
        if not isinstance(value, str) or not 1 <= len(value) <= 128:
            raise ProtocolRefusal("idempotency_key_invalid", "idempotency key is out of bounds")
        return value


class TideTestimonyLedger:
    def __init__(self, root: FloatiRoot) -> None:
        self.root = root
        self.registry = Registry(root)

    @staticmethod
    def _relative(node: str) -> Path:
        return Path("receipts/tide-testimony") / f"{node}.jsonl"

    def latest(self, node_id: str, metric: str) -> Optional[Dict[str, Any]]:
        node = self.registry.resolve_node_id(node_id, field="node")
        rows = read_records_snapshot(self.root, self._relative(node), allowed_kinds=TIDE_TESTIMONY_KINDS)
        return next((row for row in reversed(rows) if row["metric"] == metric), None)

    def record(
        self,
        node_id: str,
        metric: str,
        value: str,
        command: str,
        *,
        idempotency_key: str,
    ) -> Dict[str, Any]:
        active = self.registry.require_active(node_id)
        selected = metric_for(active["role"], metric)
        if selected.access_class != "B" or selected.stamp != "SELF_REPORTED":
            raise ProtocolRefusal("tide_testimony_not_supported", "metric is not a class-B testimony surface")
        if not isinstance(command, str) or command not in {"/context", "/status", "/usage", "/cost"}:
            raise ProtocolRefusal("tide_testimony_command_invalid", "testimony must name its measured /context-family command")
        normalized = normalize_threshold(value, selected)
        row: Dict[str, Any] = {
            "schema_version": 0,
            "id": "tide-testimony-" + uuid7_hex(),
            "tenant_id": self.root.tenant_id,
            "timestamp": utc_now(),
            "kind": "tide_testimony_record",
            "node_id": active["node_id"],
            "harness": selected.harness,
            "metric": selected.name,
            "value": normalized,
            "command": command,
            "access_class": "B",
            "stamp": "SELF_REPORTED",
            "formula": selected.formula,
            "receipt_path": selected.receipt_path,
            "idempotency_key": TidePolicyLedger._key(idempotency_key),
        }

        def decide(prior: list[Dict[str, Any]]):
            match = next((item for item in prior if item["idempotency_key"] == row["idempotency_key"]), None)
            if match is not None:
                semantic = tuple(key for key in row if key not in {"id", "timestamp"})
                if all(match.get(key) == row.get(key) for key in semantic):
                    return match, None
                raise ProtocolRefusal("tide_testimony_idempotency_conflict", "testimony key has different content")
            validate_record(row, self.root.tenant_id, TIDE_TESTIMONY_KINDS, integrity=False)
            return row, row

        return transact(self.root, self._relative(str(active["node_id"])), decide, allowed_kinds=TIDE_TESTIMONY_KINDS)

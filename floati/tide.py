"""Threshold evaluation and receipted tide actions."""

from __future__ import annotations

import json
import re
import shlex
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Callable, Dict, Mapping, Optional, Protocol, Sequence

from .errors import IntegrityFailure, ProtocolRefusal
from .events import EventLog
from .ids import uuid7_hex
from .jsonl import read_records_snapshot, transact
from .quota import QuotaLedger, require_schedulable_fraction
from .records import validate_record
from .registry import Registry, utc_now
from .root import FloatiRoot
from .tide_catalog import TideMetric, metric_for, provider_for_harness
from .tide_policy import TidePolicyLedger


TIDE_KINDS = frozenset({"tide_receipt"})


@dataclass(frozen=True)
class TideReading:
    metric: str
    value: str
    stamp: str
    access_class: str
    formula: str
    sources: tuple[str, ...]


class TideReader(Protocol):
    def read(self, binding: object, metric: TideMetric) -> TideReading: ...


class UnavailableTideReader:
    def read(self, binding: object, metric: TideMetric) -> TideReading:
        raise ProtocolRefusal(
            "tide_reading_unavailable",
            f"{metric.name} has no readable exact-session artifact for this daemon binding",
        )


class QuotaAwareTideReader:
    """Read V4 quota receipts and delegate every pre-V4 metric unchanged."""

    def __init__(self, root: FloatiRoot, delegate: TideReader) -> None:
        if not isinstance(root, FloatiRoot) or not hasattr(delegate, "read"):
            raise ProtocolRefusal(
                "tide_reader_invalid", "quota-aware Tide needs one root and delegate"
            )
        self.root = root
        self.delegate = delegate

    def read(self, binding: object, metric: TideMetric) -> TideReading:
        if metric.name != "quota_fraction":
            return self.delegate.read(binding, metric)
        provider = provider_for_harness(getattr(binding, "harness", None))
        latest = QuotaLedger(self.root).latest_record(provider)
        if latest is None:
            raise ProtocolRefusal(
                "quota_fact_unknown", f"provider {provider} has no quota receipt"
            )
        record_id, receipt = latest
        fractions = tuple(
            fact for fact in receipt.facts
            if fact.state.kind == "consumed_fraction"
        )
        if not fractions:
            unknowns = tuple(
                fact for fact in receipt.facts if fact.state.kind == "unknown"
            )
            if unknowns:
                require_schedulable_fraction(max(
                    unknowns, key=lambda fact: (fact.observed_at, fact.surface)
                ))
            raise ProtocolRefusal(
                "quota_fact_unknown",
                f"provider {provider} receipt has no consumed-fraction fact",
            )
        fact = max(
            fractions,
            key=lambda item: (
                item.observed_at,
                Decimal(item.state.value or "0"),
                item.surface,
            ),
        )
        value = require_schedulable_fraction(fact)
        return TideReading(
            metric=metric.name,
            value=value,
            stamp=fact.stamp,
            access_class=metric.access_class,
            formula=metric.formula,
            sources=(
                metric.receipt_path,
                "quota-receipt:" + record_id,
                "quota-source:" + fact.source,
                "quota-evidence-sha256:" + fact.evidence_digest,
                "quota-receipt-sha256:" + receipt.receipt_digest,
            ),
        )


class BoundedTideReader:
    """Read only exact-session T1 artifacts for daemon-supported harnesses."""

    _MAX_BYTES = 16 * 1024 * 1024
    _SESSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

    def __init__(
        self,
        *,
        root: Optional[FloatiRoot] = None,
        codex_root: Optional[Path] = None,
        cursor_root: Optional[Path] = None,
    ) -> None:
        home = Path.home()
        self.codex_root = home / ".codex/sessions" if codex_root is None else Path(codex_root)
        self.cursor_root = home / ".cursor/projects" if cursor_root is None else Path(cursor_root)
        self.root = root

    def _session(self, binding: object) -> str:
        value = getattr(binding, "session_id", None)
        if not isinstance(value, str) or self._SESSION.fullmatch(value) is None:
            raise ProtocolRefusal("tide_session_invalid", "tide reader requires a bounded exact session id")
        return value

    @staticmethod
    def _one(paths: Sequence[Path], root: Path) -> Path:
        try:
            lexical_root = root.absolute()
            canonical_root = root.resolve(strict=True)
        except OSError:
            raise ProtocolRefusal(
                "tide_reading_unavailable", "exact-session artifact root is unavailable"
            ) from None
        ordinary = []
        for path in paths:
            lexical = path.absolute()
            try:
                relative = lexical.relative_to(lexical_root)
                cursor = lexical_root
                for part in relative.parts:
                    cursor /= part
                    if cursor.is_symlink():
                        raise ProtocolRefusal(
                            "tide_reading_unavailable",
                            "exact session artifact crosses a symlink boundary",
                        )
                canonical = lexical.resolve(strict=True)
                canonical.relative_to(canonical_root)
            except ProtocolRefusal:
                raise
            except (OSError, ValueError):
                raise ProtocolRefusal(
                    "tide_reading_unavailable",
                    "exact session artifact escapes its configured root",
                ) from None
            if canonical.is_file():
                ordinary.append(canonical)
        if len(ordinary) != 1:
            raise ProtocolRefusal("tide_reading_unavailable", "exact session artifact is absent or ambiguous")
        if ordinary[0].stat().st_size > BoundedTideReader._MAX_BYTES:
            raise ProtocolRefusal("tide_reading_unavailable", "exact session artifact exceeds the read bound")
        return ordinary[0]

    def read(self, binding: object, metric: TideMetric) -> TideReading:
        if metric.access_class == "B":
            if self.root is None:
                raise ProtocolRefusal("tide_reading_unavailable", "class-B testimony needs the selected fleet root")
            from .tide_policy import TideTestimonyLedger

            node = getattr(binding, "node_id", None)
            testimony = TideTestimonyLedger(self.root).latest(node, metric.name)
            if testimony is None:
                raise ProtocolRefusal("tide_reading_unavailable", "node has not submitted class-B testimony")
            return TideReading(
                metric=metric.name,
                value=str(testimony["value"]),
                stamp="SELF_REPORTED",
                access_class="B",
                formula=metric.formula,
                sources=(metric.receipt_path, f"testimony:{testimony['id']}", f"command:{testimony['command']}"),
            )
        session = self._session(binding)
        harness = getattr(binding, "harness", None)
        if harness == "codex":
            path = self._one(
                tuple(self.codex_root.glob(f"*/*/*/rollout-*-{session}.jsonl")),
                self.codex_root,
            )
            value = self._codex_value(path, metric)
        elif harness == "cursor":
            path = self._one(
                tuple(self.cursor_root.glob(f"*/agent-transcripts/{session}/{session}.jsonl")),
                self.cursor_root,
            )
            if metric.name == "transcript_bytes":
                value = str(path.stat().st_size)
            elif metric.name == "turn_count":
                with path.open("rb") as handle:
                    value = str(sum(1 for line in handle if line.strip()))
            else:
                raise ProtocolRefusal("tide_metric_not_derivable", "cursor metric is outside the T1 recipe")
        else:
            raise ProtocolRefusal("tide_reading_unavailable", "daemon binding has no shipped class-A reader")
        return TideReading(
            metric=metric.name,
            value=value,
            stamp=metric.stamp,
            access_class=metric.access_class,
            formula=metric.formula,
            sources=(metric.receipt_path, str(path)),
        )

    def _codex_value(self, path: Path, metric: TideMetric) -> str:
        if metric.name == "transcript_bytes":
            return str(path.stat().st_size)
        turns = set()
        latest: Optional[tuple[Decimal, Decimal]] = None
        with path.open("rb") as handle:
            for raw in handle:
                if not raw.strip() or len(raw) > 1024 * 1024:
                    continue
                try:
                    row = json.loads(raw)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
                payload = row.get("payload") if isinstance(row, dict) else None
                if not isinstance(payload, dict):
                    continue
                turn = payload.get("turn_id")
                if isinstance(turn, str):
                    turns.add(turn)
                info = payload.get("info")
                if not isinstance(info, dict):
                    continue
                usage = info.get("last_token_usage")
                window = info.get("model_context_window")
                if isinstance(usage, dict) and isinstance(usage.get("total_tokens"), (int, float)) and isinstance(window, (int, float)) and window > 0:
                    latest = Decimal(str(usage["total_tokens"])), Decimal(str(window))
        if metric.name == "turn_count":
            return str(len(turns))
        if metric.name == "context_fraction" and latest is not None:
            return f"{latest[0] / latest[1]:.6f}"
        raise ProtocolRefusal("tide_reading_unavailable", "exact Codex session lacks the authorized nested metric")


class TideEvaluator:
    def __init__(
        self,
        root: FloatiRoot,
        *,
        reader: Optional[TideReader] = None,
        turnover_projector: Optional[Callable[[str], Mapping[str, object]]] = None,
        source_sha: str,
    ) -> None:
        self.root = root
        self.reader = (
            QuotaAwareTideReader(root, BoundedTideReader(root=root))
            if reader is None else reader
        )
        self.policies = TidePolicyLedger(root)
        self.events = EventLog(root)
        self.source_sha = source_sha
        self.turnover_projector = turnover_projector or self._project_turnover

    @staticmethod
    def _decimal(value: object) -> Decimal:
        try:
            number = Decimal(str(value))
        except (InvalidOperation, ValueError):
            raise ProtocolRefusal("tide_reading_invalid", "tide reading is not numeric") from None
        if not number.is_finite() or number < 0:
            raise ProtocolRefusal("tide_reading_invalid", "tide reading is outside its bounds")
        return number

    def _relative(self, node: str) -> Path:
        return Path("receipts/tide") / f"{node}.jsonl"

    def _rows(self, node: str) -> list[Dict[str, object]]:
        return read_records_snapshot(self.root, self._relative(node), allowed_kinds=TIDE_KINDS)

    def _latest_policy_rows(self, node: str, policy_id: str) -> list[Dict[str, object]]:
        return [row for row in self._rows(node) if row["policy_id"] == policy_id]

    def evaluate(self, node_id: str, binding: object) -> Dict[str, object]:
        policy = self.policies.show(node_id)
        if policy is None:
            return {"state": "off", "node_id": node_id, "receipt": None}
        if self.dispatch_held(node_id):
            return {"state": "held", "node_id": node_id, "receipt": self.status(node_id)["receipt"]}
        if getattr(binding, "harness", None) != policy["harness"]:
            raise ProtocolRefusal("tide_binding_mismatch", "tide policy and daemon binding harness disagree")
        metric = metric_for(policy["harness"], policy["metric"])
        try:
            reading = self.reader.read(binding, metric)
        except ProtocolRefusal as exc:
            if metric.name != "quota_fraction":
                raise
            state = (
                "quota_unknown" if exc.code == "quota_fact_unknown"
                else "quota_refused"
            )
            return {
                "state": state,
                "node_id": node_id,
                "receipt": None,
                "reason": {"code": exc.code, "detail": exc.detail},
            }
        self._validate_reading(reading, metric)
        value = self._decimal(reading.value)
        threshold = self._decimal(policy["threshold"])
        prior = self._latest_policy_rows(str(policy["node_id"]), str(policy["id"]))
        crossing_epoch = max((int(row["crossing_epoch"]) for row in prior), default=0)
        currently_above = bool(prior) and prior[-1]["evaluation_state"] in {
            "crossed", "above", "recommended", "directed", "state_flushed"
        }
        if prior and prior[-1]["evaluation_state"] == "crossed":
            crossing = prior[-1]
            envelope = self._act(policy, crossing, crossing_epoch)
            crossing_reading = TideReading(
                metric=str(crossing["metric"]),
                value=str(crossing["value"]),
                stamp=str(crossing["stamp"]),
                access_class=str(crossing["access_class"]),
                formula=str(crossing["formula"]),
                sources=tuple(str(item) for item in crossing["sources"]),
            )
            receipt = self._record(
                policy, crossing_reading,
                "directed" if policy["action"] == "direct" else "recommended",
                crossing_epoch, str(crossing["id"]), None, str(envelope["id"]),
            )
            return {"state": "crossed", "node_id": node_id, "receipt": receipt, "envelope": envelope}
        if value < threshold:
            if not currently_above:
                return {"state": "below", "node_id": node_id, "receipt": None}
            receipt = self._record(policy, reading, "rearmed", crossing_epoch, None, None, None)
            return {"state": "rearmed", "node_id": node_id, "receipt": receipt}
        if currently_above:
            return {"state": "above", "node_id": node_id, "receipt": prior[-1]}
        crossing_epoch += 1
        receipt = self._record(policy, reading, "crossed", crossing_epoch, None, None, None)
        envelope = self._act(policy, receipt, crossing_epoch)
        receipt = self._record(
            policy, reading,
            "directed" if policy["action"] == "direct" else "recommended",
            crossing_epoch, str(receipt["id"]), None, str(envelope["id"]),
        )
        return {"state": "crossed", "node_id": node_id, "receipt": receipt, "envelope": envelope}

    def _validate_reading(self, reading: TideReading, metric: TideMetric) -> None:
        quota_stamp_valid = (
            metric.name == "quota_fraction"
            and metric.stamp == "MEASURED_OR_DERIVED"
            and isinstance(reading, TideReading)
            and reading.stamp in {"MEASURED", "DERIVED"}
        )
        quota_sources_valid = (
            metric.name != "quota_fraction"
            or (
                any(source.startswith("quota-receipt:") for source in reading.sources)
                and any(source.startswith("quota-source:") for source in reading.sources)
                and any(source.startswith("quota-evidence-sha256:") for source in reading.sources)
            )
        ) if isinstance(reading, TideReading) else False
        if (
            not isinstance(reading, TideReading)
            or reading.metric != metric.name
            or (reading.stamp != metric.stamp and not quota_stamp_valid)
            or reading.access_class != metric.access_class
            or reading.formula != metric.formula
            or not reading.sources
            or metric.receipt_path not in reading.sources
            or not quota_sources_valid
        ):
            raise IntegrityFailure("tide_reading_invalid", "reading is not bound to the ruled Tide recipe")

    def _architect(self) -> str:
        registry = Registry(self.root)
        for node in registry.active_node_ids():
            if registry.require_active(node)["role"].casefold() == "architect":
                return node
        raise ProtocolRefusal("tide_architect_absent", "fleet has no active architect node")

    def _act(self, policy: Mapping[str, object], receipt: Mapping[str, object], epoch: int) -> Dict[str, object]:
        node = str(policy["node_id"])
        architect = self._architect()
        action = str(policy["action"])
        if action == "recommend":
            sender, recipient = node, architect
            note = (
                f"TIDE NOTICE metric={policy['metric']} value={receipt['value']} "
                f"threshold={policy['threshold']} receipt={receipt['id']}"
            )
        else:
            sender, recipient = architect, node
            projected = dict(self.turnover_projector(node))
            steps = projected.get("steps")
            provenance = projected.get("role_provenance")
            if not isinstance(steps, list) or len(steps) != 3 or not isinstance(provenance, Mapping):
                raise IntegrityFailure("tide_turnover_recipe_invalid", "E2 turnover projection is malformed")
            commands = []
            for step in steps:
                if not isinstance(step, Mapping) or not isinstance(step.get("argv"), list) or not all(isinstance(item, str) for item in step["argv"]):
                    raise IntegrityFailure("tide_turnover_recipe_invalid", "E2 turnover step is malformed")
                commands.append(f"{step.get('kind')}={shlex.join(step['argv'])}")
            note = (
                "TURNOVER DIRECTIVE "
                f"e2={projected.get('kind')} role={provenance.get('role_record_id')} "
                + " | ".join(commands)
            )
        if policy["metric"] == "quota_fraction":
            note = "DRAFT - " + note
        return self.events.send(
            sender, recipient, "floati", self.source_sha,
            "docs/design/tide-tables-spec-2026-08-28.md", note,
            idempotency_key=f"tide-{policy['id']}-{epoch}-{action}",
        )

    def _record(
        self,
        policy: Mapping[str, object],
        reading: TideReading,
        state: str,
        epoch: int,
        crossing_receipt_id: Optional[str],
        state_flush_receipt_id: Optional[str],
        envelope_id: Optional[str],
    ) -> Dict[str, object]:
        row: Dict[str, object] = {
            "schema_version": 1 if policy["metric"] == "quota_fraction" else 0,
            "id": "tide-receipt-" + uuid7_hex(),
            "tenant_id": self.root.tenant_id,
            "timestamp": utc_now(),
            "kind": "tide_receipt",
            "node_id": policy["node_id"],
            "harness": policy["harness"],
            "policy_id": policy["id"],
            "metric": policy["metric"],
            "value": str(reading.value),
            "threshold": policy["threshold"],
            "stamp": reading.stamp,
            "access_class": reading.access_class,
            "formula": reading.formula,
            "sources": list(reading.sources),
            "evaluation_state": state,
            "action": policy["action"],
            "crossing_epoch": epoch,
            "crossing_receipt_id": crossing_receipt_id,
            "state_flush_receipt_id": state_flush_receipt_id,
            "envelope_id": envelope_id,
            "idempotency_key": f"tide-{policy['id']}-{epoch}-{state}",
        }

        def decide(prior: list[Dict[str, object]]):
            match = next((item for item in prior if item["idempotency_key"] == row["idempotency_key"]), None)
            if match is not None:
                return match, None
            validate_record(row, self.root.tenant_id, TIDE_KINDS, integrity=False)
            return row, row

        return transact(self.root, self._relative(str(policy["node_id"])), decide, allowed_kinds=TIDE_KINDS)

    def dispatch_held(self, node_id: str) -> bool:
        return self.status(node_id).get("turnover_state") == "directed"

    def status(self, node_id: str) -> Dict[str, object]:
        node = Registry(self.root).resolve_node_id(node_id, field="node")
        rows = self._rows(node)
        directed = next((row for row in reversed(rows) if row["evaluation_state"] in {"directed", "state_flushed"}), None)
        if directed is None:
            return {"node_id": node, "turnover_state": "none", "receipt": None}
        state = "state_flushed" if directed["evaluation_state"] == "state_flushed" else "directed"
        return {"node_id": node, "turnover_state": state, "receipt": directed}

    def projection(self, node_id: str) -> Dict[str, object]:
        node = Registry(self.root).resolve_node_id(node_id, field="node")
        policy = self.policies.show(node)
        rows = self._rows(node)
        latest = rows[-1] if rows else None
        reading = None
        if latest is not None:
            reading = {
                key: latest[key]
                for key in (
                    "metric", "value", "threshold", "stamp", "access_class",
                    "formula", "sources", "evaluation_state", "timestamp",
                )
            }
        return {
            "node_id": node,
            "policy": policy,
            "last_reading": reading,
            "turnover_state": self.status(node)["turnover_state"],
        }

    def observe_state_flush(self, receipt: Mapping[str, object]) -> Dict[str, object]:
        node = str(receipt.get("node_id", ""))
        if receipt.get("tenant_id") != self.root.tenant_id or receipt.get("kind") != "node_state_flush_receipt":
            raise ProtocolRefusal("tide_state_flush_invalid", "state-flush receipt is foreign or malformed")
        status = self.status(node)
        prior = status.get("receipt")
        if status["turnover_state"] != "directed" or not isinstance(prior, Mapping):
            raise ProtocolRefusal("tide_directive_absent", "node has no directed turnover awaiting state flush")
        if str(receipt.get("timestamp")) <= str(prior["timestamp"]):
            raise ProtocolRefusal("tide_state_flush_stale", "state-flush receipt does not follow the directive")
        policy = self.policies.by_id(node, prior["policy_id"])
        if policy is None:
            raise ProtocolRefusal("tide_policy_absent", "directed turnover policy receipt is absent")
        reading = TideReading(
            metric=str(prior["metric"]), value=str(prior["value"]), stamp=str(prior["stamp"]),
            access_class=str(prior["access_class"]), formula=str(prior["formula"]),
            sources=tuple(str(item) for item in prior["sources"]),
        )
        return self._record(
            policy, reading, "state_flushed", int(prior["crossing_epoch"]),
            str(prior["crossing_receipt_id"]), str(receipt["id"]), str(prior["envelope_id"]),
        )

    def _project_turnover(self, node: str) -> Mapping[str, object]:
        from .context import ContextTurnoverProjection

        return ContextTurnoverProjection(self.root, node).project()

"""H2 — delivery-health scoreboard (hardening intake, launch cut @09283dc7).

Per registered node: undelivered-envelope count, oldest pending age, and
time since the last drain receipt. Aged pending mail is RED with the
intake's plain sentence ("alice: 9 undelivered, oldest 42m, no drain since
03:28"). Silence is not health: counters are stated even when green.

Pure analysis over injected records — no I/O here. The doctor supplies:
- events records (the message_envelope log snapshot),
- delivery receipt rows per node (from receipts/deliveries/),
- the active node list,
- the evaluation clock.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional

DELIVERY_STALL_RED_MINUTES = 15
# Threshold STAMPED RULED by Fable (2026-08-22, msg-01a0281107…): 15 is a
# judgment with margin — NOT measured, NOT derived from the incident (the
# incident's silence ran 40+ minutes). Do not re-derive it from sightings;
# amendments enter by ruling.


@dataclass(frozen=True)
class NodeDeliveryHealth:
    node: str
    undelivered_count: int
    oldest_pending_minutes: Optional[int]
    last_drain_minutes_ago: Optional[int]
    red: bool
    sentence: str


@dataclass(frozen=True)
class DeliveryHealthReport:
    by_node: Dict[str, NodeDeliveryHealth]
    findings_by_node: Dict[str, dict] = field(default_factory=dict)

    @property
    def findings(self) -> List[dict]:
        return [self.findings_by_node[node] for node in sorted(self.by_node)]

    @property
    def any_red(self) -> bool:
        return any(health.red for health in self.by_node.values())


class DeliveryHealthAnalyzer:
    @staticmethod
    def analyze(
        events: List[Dict[str, object]],
        root,
        nodes: List[str],
        now: datetime,
    ) -> DeliveryHealthReport:
        envelopes_by_node: Dict[str, List[Dict[str, object]]] = {
            node: [] for node in nodes
        }
        for record in events:
            if record.get("kind") != "message_envelope":
                continue
            recipient = record.get("recipient")
            if recipient in envelopes_by_node:
                envelopes_by_node[recipient].append(record)

        by_node: Dict[str, NodeDeliveryHealth] = {}
        findings_by_node: Dict[str, dict] = {}
        for node in nodes:
            delivered_ids = _delivered_ids(root, node)
            pending = sorted(
                (
                    envelope
                    for envelope in envelopes_by_node[node]
                    if envelope["id"] not in delivered_ids
                ),
                key=lambda envelope: str(envelope["timestamp"]),
            )
            last_drain_minutes_ago = _last_drain_minutes_ago(root, node, now)
            oldest_pending_minutes: Optional[int] = None
            if pending:
                oldest = _parse_timestamp(str(pending[0]["timestamp"]))
                oldest_pending_minutes = int(
                    (now - oldest).total_seconds() // 60
                )
            red = bool(pending) and (
                last_drain_minutes_ago is None
                or (
                    oldest_pending_minutes is not None
                    and oldest_pending_minutes > DELIVERY_STALL_RED_MINUTES
                )
            )
            sentence = _sentence(
                node,
                undelivered=len(pending),
                oldest_pending_minutes=oldest_pending_minutes,
                last_drain_minutes_ago=last_drain_minutes_ago,
            )
            by_node[node] = NodeDeliveryHealth(
                node=node,
                undelivered_count=len(pending),
                oldest_pending_minutes=oldest_pending_minutes,
                last_drain_minutes_ago=last_drain_minutes_ago,
                red=red,
                sentence=sentence,
            )
            findings_by_node[node] = {
                "code": "delivery_health",
                "severity": "error" if red else "ok",
                "subject": node,
                "detail": sentence,
                "remediation": (
                    "check the node's wake path, then drain its inbox "
                    "(floati inbox --root <root> --as %s)" % node
                )
                if red
                else None,
            }
        return DeliveryHealthReport(by_node=by_node, findings_by_node=findings_by_node)


def _sentence(
    node: str,
    *,
    undelivered: int,
    oldest_pending_minutes: Optional[int],
    last_drain_minutes_ago: Optional[int],
) -> str:
    parts = [f"{node}: {undelivered} undelivered"]
    if oldest_pending_minutes is not None:
        parts.append(f"oldest {oldest_pending_minutes}m")
    if last_drain_minutes_ago is None:
        parts.append("no drain on record")
    else:
        parts.append(f"last drain {last_drain_minutes_ago}m ago")
    parts.append(
        f"RED threshold {DELIVERY_STALL_RED_MINUTES}m RULED"
    )
    return ", ".join(parts)


def _delivery_rows(root, node: str) -> List[Dict[str, object]]:
    """Read every drain receipt row for one node (plain file + sessions)."""

    import json

    base = root.resolve_relative("receipts/deliveries")
    candidates = []
    plain = base / f"{node}.jsonl"
    if plain.is_file():
        candidates.append(plain)
    session_dir = base / node
    if session_dir.is_dir():
        candidates.extend(sorted(p for p in session_dir.glob("*.jsonl") if p.is_file()))
    rows: List[Dict[str, object]] = []
    for path in candidates:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                rows.append(json.loads(line))
    return rows


def _delivered_ids(root, node: str) -> set:
    delivered: set = set()
    for row in _delivery_rows(root, node):
        item_ids = row.get("item_ids")
        if isinstance(item_ids, list):
            delivered.update(item_ids)
    return delivered


def _parse_timestamp(stamp: str) -> datetime:
    """Parse the ledger's Z-suffixed RFC3339 stamps (Python 3.9-safe)."""

    return datetime.fromisoformat(stamp.replace("Z", "+00:00"))


def _last_drain_minutes_ago(root, node: str, now: datetime) -> Optional[int]:
    latest: Optional[datetime] = None
    for row in _delivery_rows(root, node):
        stamp = row.get("timestamp")
        if not isinstance(stamp, str):
            continue
        moment = _parse_timestamp(stamp)
        if latest is None or moment > latest:
            latest = moment
    if latest is None:
        return None
    return int((now - latest).total_seconds() // 60)

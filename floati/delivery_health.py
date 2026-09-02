"""H2 — delivery-health scoreboard (hardening intake, launch cut @09283dc7).

Per registered node: undelivered-envelope count, oldest pending age, and
time since the last drain receipt. Aged pending mail is RED with the
intake's plain sentence ("builder-a: 9 undelivered, oldest 42m, no drain since
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
from typing import Dict, List, Mapping, Optional, Tuple

from .mail_health import oldest_unread_fact

DELIVERY_STALL_RED_MINUTES = 15
# Threshold STAMPED RULED by the reviewer (2026-08-22, msg-01a0281107…): 15 is a
# judgment with margin — NOT measured, NOT derived from the incident (the
# incident's silence ran 40+ minutes). Do not re-derive it from sightings;
# amendments enter by ruling.


@dataclass(frozen=True)
class NodeDeliveryHealth:
    node: str
    undelivered_count: int
    oldest_pending_minutes: Optional[int]
    oldest_unread: Optional[dict]
    last_drain_minutes_ago: Optional[int]
    red: bool
    sentence: str


@dataclass(frozen=True)
class NodeAcknowledgmentHealth:
    node: str
    delivered_unacknowledged_count: int
    oldest_attention_minutes: Optional[int]
    oldest_message_id: Optional[str]
    acknowledged_count: int
    acknowledgment_latencies_seconds: Tuple[int, ...]
    cadence: Optional[str]
    red: bool
    sentence: str


@dataclass(frozen=True)
class DeliveryHealthReport:
    by_node: Dict[str, NodeDeliveryHealth]
    findings_by_node: Dict[str, dict] = field(default_factory=dict)
    acknowledgments_by_node: Dict[str, NodeAcknowledgmentHealth] = field(
        default_factory=dict
    )
    acknowledgment_findings_by_node: Dict[str, dict] = field(default_factory=dict)

    @property
    def findings(self) -> List[dict]:
        rows: List[dict] = []
        for node in sorted(self.by_node):
            rows.append(self.findings_by_node[node])
            rows.append(self.acknowledgment_findings_by_node[node])
        return rows

    @property
    def any_red(self) -> bool:
        return any(health.red for health in self.by_node.values()) or any(
            health.red for health in self.acknowledgments_by_node.values()
        )


class DeliveryHealthAnalyzer:
    @staticmethod
    def analyze(
        events: List[Dict[str, object]],
        root,
        nodes: List[str],
        now: datetime,
        cadences: Optional[Mapping[str, str]] = None,
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
        acknowledgments_by_node: Dict[str, NodeAcknowledgmentHealth] = {}
        acknowledgment_findings_by_node: Dict[str, dict] = {}
        for node in nodes:
            delivery_times = _receipt_item_times(root, "deliveries", node)
            acknowledgment_times = _receipt_item_times(root, "acks", node)
            delivered_ids = set(delivery_times)
            pending = sorted(
                (
                    envelope
                    for envelope in envelopes_by_node[node]
                    if envelope["id"] not in delivered_ids
                ),
                key=lambda envelope: str(envelope["timestamp"]),
            )
            last_drain_minutes_ago = _last_drain_minutes_ago(root, node, now)
            oldest_unread = oldest_unread_fact(node, pending, now=now)
            oldest_pending_minutes = (
                None if oldest_unread is None else int(oldest_unread["age_minutes"])
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
                oldest_unread=oldest_unread,
                last_drain_minutes_ago=last_drain_minutes_ago,
                red=red,
                sentence=sentence,
            )
            findings_by_node[node] = {
                "code": "delivery_health",
                "severity": "error" if red else "ok",
                "subject": node,
                "detail": sentence,
                "oldest_unread": oldest_unread,
                "remediation": (
                    "check the node's wake path, then drain its inbox "
                    "(floati inbox --root <root> --as %s --session <session-id>)" % node
                )
                if red
                else None,
            }

            delivered_unacknowledged = sorted(
                (
                    envelope
                    for envelope in envelopes_by_node[node]
                    if envelope["id"] in delivery_times
                    and envelope["id"] not in acknowledgment_times
                ),
                key=lambda envelope: delivery_times[str(envelope["id"])],
            )
            acknowledged = sorted(
                (
                    envelope
                    for envelope in envelopes_by_node[node]
                    if envelope["id"] in delivery_times
                    and envelope["id"] in acknowledgment_times
                ),
                key=lambda envelope: str(envelope["timestamp"]),
            )
            latencies = tuple(
                int(
                    (
                        acknowledgment_times[str(envelope["id"])]
                        - delivery_times[str(envelope["id"])]
                    ).total_seconds()
                )
                for envelope in acknowledged
            )
            if any(seconds < 0 for seconds in latencies):
                from .errors import IntegrityFailure

                raise IntegrityFailure(
                    "acknowledgment_receipt_order_invalid",
                    "acknowledgment precedes its delivery receipt",
                )
            latency_rows = tuple(
                {
                    "message_id": str(envelope["id"]),
                    "seconds": seconds,
                }
                for envelope, seconds in zip(acknowledged, latencies)
            )
            oldest_message_id = (
                None
                if not delivered_unacknowledged
                else str(delivered_unacknowledged[0]["id"])
            )
            oldest_attention_minutes = (
                None
                if oldest_message_id is None
                else int(
                    (now - delivery_times[oldest_message_id]).total_seconds() // 60
                )
            )
            cadence = None if cadences is None else cadences.get(node)
            acknowledgment_red = False
            acknowledgment_sentence = _acknowledgment_sentence(
                node,
                delivered_unacknowledged=len(delivered_unacknowledged),
                oldest_attention_minutes=oldest_attention_minutes,
                acknowledged=len(acknowledged),
                latencies=latencies,
                cadence=cadence,
            )
            acknowledgments_by_node[node] = NodeAcknowledgmentHealth(
                node=node,
                delivered_unacknowledged_count=len(delivered_unacknowledged),
                oldest_attention_minutes=oldest_attention_minutes,
                oldest_message_id=oldest_message_id,
                acknowledged_count=len(acknowledged),
                acknowledgment_latencies_seconds=latencies,
                cadence=cadence,
                red=acknowledgment_red,
                sentence=acknowledgment_sentence,
            )
            acknowledgment_findings_by_node[node] = {
                "code": "acknowledgment_health",
                "severity": (
                    "error"
                    if acknowledgment_red
                    else "info"
                    if delivered_unacknowledged
                    else "ok"
                ),
                "subject": node,
                "detail": acknowledgment_sentence,
                "remediation": (
                    "acknowledge the exact delivered batch with "
                    "floati ack --root <root> --as %s --id <message-id> "
                    "--session <session-id>" % node
                )
                if delivered_unacknowledged
                else None,
                "acknowledgment": {
                    "delivered_unacknowledged_count": len(
                        delivered_unacknowledged
                    ),
                    "oldest_attention_minutes": oldest_attention_minutes,
                    "oldest_message_id": oldest_message_id,
                    "acknowledged_count": len(acknowledged),
                    "latencies": list(latency_rows),
                    "cadence": cadence,
                    "sla": "undeclared",
                },
            }
        return DeliveryHealthReport(
            by_node=by_node,
            findings_by_node=findings_by_node,
            acknowledgments_by_node=acknowledgments_by_node,
            acknowledgment_findings_by_node=acknowledgment_findings_by_node,
        )


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


def _acknowledgment_sentence(
    node: str,
    *,
    delivered_unacknowledged: int,
    oldest_attention_minutes: Optional[int],
    acknowledged: int,
    latencies: Tuple[int, ...],
    cadence: Optional[str],
) -> str:
    parts = [
        f"{node}: {delivered_unacknowledged} delivered-unacknowledged",
        f"{acknowledged} acknowledged",
    ]
    if oldest_attention_minutes is not None:
        parts.append(f"oldest attention {oldest_attention_minutes}m")
    if latencies:
        parts.append("ack latency seconds=" + ",".join(str(value) for value in latencies))
    if cadence is None:
        parts.append("role cadence unavailable")
    else:
        parts.append(f"cadence {cadence}")
    parts.append("no acknowledgment SLA declared")
    return ", ".join(parts)


def _receipt_rows(root, directory: str, node: str) -> List[Dict[str, object]]:
    """Read every receipt row for one node (plain file + sessions)."""

    import json

    base = root.resolve_relative(f"receipts/{directory}")
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


def _delivery_rows(root, node: str) -> List[Dict[str, object]]:
    return _receipt_rows(root, "deliveries", node)


def _receipt_item_times(root, directory: str, node: str) -> Dict[str, datetime]:
    result: Dict[str, datetime] = {}
    for row in _receipt_rows(root, directory, node):
        stamp = row.get("timestamp")
        item_ids = row.get("item_ids")
        if not isinstance(stamp, str) or not isinstance(item_ids, list):
            continue
        moment = _parse_timestamp(stamp)
        for item_id in item_ids:
            if isinstance(item_id, str) and (
                item_id not in result or moment < result[item_id]
            ):
                result[item_id] = moment
    return result


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

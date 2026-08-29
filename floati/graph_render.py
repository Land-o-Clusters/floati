"""Human Harbor Chart rendering composed from topology and counts-only traffic."""

from __future__ import annotations

from collections import Counter
from typing import Mapping, Optional

from .brand import BUOY_ORANGE, RESET
from .copy import register


CHART_HEADER = register("graph.header", "FLOATI // HARBOR CHART", "Harbor Chart header")
TRAFFIC_HEADER = register("graph.traffic", "TRAFFIC", "Harbor Chart traffic panel")
TRAFFIC_UNAVAILABLE = register(
    "graph.traffic_unavailable",
    "traffic: unavailable",
    "Harbor Chart typed absence",
)


def _node_summary(
    node: Mapping[str, object], workers: list[Mapping[str, object]]
) -> str:
    states = Counter(
        str(worker.get("state", "unknown")).upper()
        for worker in workers
        if worker.get("node_id") == node.get("id")
    )
    worker_summary = (
        " · ".join(f"{count} {state}" for state, count in sorted(states.items()))
        if states
        else "0 WORKERS"
    )
    return f"{str(node.get('state', 'unknown')).upper()} · {worker_summary}"


def _box(node_id: str, summary: str) -> list[str]:
    inside = max(len(node_id), len(summary), 16)
    return [
        "┌" + "─" * (inside + 2) + "┐",
        f"│ {node_id:<{inside}} │",
        f"│ {summary:<{inside}} │",
        "└" + "─" * (inside + 2) + "┘",
    ]


def _count(value: int, singular: str) -> str:
    return f"{value} {singular if value == 1 else singular + 's'}"


def render_harbor_chart(
    topology: Mapping[str, object],
    traffic: Optional[Mapping[str, object]],
    *,
    color: bool,
) -> str:
    """Render existing structure plus the separately ruled traffic projection."""

    workers = [
        worker
        for worker in topology.get("workers", [])
        if isinstance(worker, Mapping)
    ]
    nodes = sorted(
        (
            node
            for node in topology.get("nodes", [])
            if isinstance(node, Mapping)
        ),
        key=lambda node: str(node.get("id", "")),
    )
    lines = [CHART_HEADER, f"tenant: {topology.get('tenant_id', '?')}", ""]
    for node in nodes:
        lines.extend(
            _box(
                str(node.get("id", "?")),
                _node_summary(node, workers),
            )
        )
        lines.append("")

    lines.append(TRAFFIC_HEADER)
    if traffic is None:
        lines.append(TRAFFIC_UNAVAILABLE)
    else:
        pairs = sorted(
            (
                pair
                for pair in traffic.get("pairs", [])
                if isinstance(pair, Mapping)
            ),
            key=lambda pair: (
                str(pair.get("sender", "")),
                str(pair.get("recipient", "")),
            ),
        )
        if not pairs:
            lines.append("traffic: 0 directed pairs")
        for pair in pairs:
            envelopes = int(pair.get("envelope_count", 0))
            denials = int(pair.get("denial_count", 0))
            denial = _count(denials, "denial")
            if denials:
                denial = "! " + denial
            edge = (
                f"{pair.get('sender', '?')} ── {_count(envelopes, 'envelope')} "
                f"· {denial} ──▶ {pair.get('recipient', '?')}"
            )
            lines.append(BUOY_ORANGE + edge + RESET if color else edge)
    return "\n".join(lines).rstrip() + "\n"

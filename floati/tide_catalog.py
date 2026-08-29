"""Closed T1-authorized tide metric catalog."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Tuple

from .errors import ProtocolRefusal


T1_SURVEY = "docs/evidence/gauntlet/T1-tide-survey.md"
T1_DEPTH2 = "docs/evidence/gauntlet/T1-depth2.md"


@dataclass(frozen=True)
class TideMetric:
    harness: str
    name: str
    access_class: str
    stamp: str
    value_kind: str
    formula: str
    receipt_path: str


_ROWS = (
    TideMetric("codex", "context_fraction", "A", "DERIVED", "fraction", "latest payload.info.last_token_usage.total_tokens / payload.info.model_context_window", T1_DEPTH2),
    TideMetric("codex", "transcript_bytes", "A", "DERIVED", "proxy_bytes", "bytes of the exact bound session jsonl", T1_SURVEY),
    TideMetric("codex", "turn_count", "A", "DERIVED", "proxy_count", "distinct turn_id values in the exact bound session jsonl", T1_SURVEY),
    TideMetric("claude", "context_fraction", "A", "DERIVED", "fraction", "latest message.usage input_tokens + cache_read_input_tokens / cited model context window", T1_DEPTH2),
    TideMetric("opencode", "session_tokens", "A", "DERIVED", "proxy_count", "session tokens_input + tokens_output + tokens_reasoning + tokens_cache_read + tokens_cache_write", T1_SURVEY),
    TideMetric("cursor", "transcript_bytes", "A", "DERIVED", "proxy_bytes", "bytes of the exact bound agent transcript jsonl", T1_DEPTH2),
    TideMetric("cursor", "turn_count", "A", "DERIVED", "proxy_count", "records in the exact bound agent transcript jsonl", T1_DEPTH2),
    TideMetric("grok-build", "message_count", "A", "DERIVED", "proxy_count", "summary.json num_messages", T1_SURVEY),
    TideMetric("grok-build", "session_bytes", "A", "DERIVED", "proxy_bytes", "bytes of the exact session directory", T1_SURVEY),
    TideMetric("codex", "self_reported_context_fraction", "B", "SELF_REPORTED", "fraction", "latest testimony from the node's /status or /context-family command", T1_SURVEY),
    TideMetric("claude", "self_reported_context_fraction", "B", "SELF_REPORTED", "fraction", "latest testimony from the node's /context command", T1_SURVEY),
    TideMetric("cursor", "self_reported_context_fraction", "B", "SELF_REPORTED", "fraction", "latest human-typed composer /context testimony from the node", T1_SURVEY),
    TideMetric("grok-build", "self_reported_context_fraction", "B", "SELF_REPORTED", "fraction", "latest testimony from the node's /context command", T1_SURVEY),
)

_BY_KEY: Dict[Tuple[str, str], TideMetric] = {
    (row.harness, row.name): row for row in _ROWS
}


def canonical_harness(value: object) -> str:
    if not isinstance(value, str):
        raise ProtocolRefusal("tide_harness_invalid", "tide harness must be text")
    key = value.strip().casefold()
    aliases = {"grok": "grok-build", "claude-code": "claude"}
    return aliases.get(key, key)


def metrics_for(harness: object) -> tuple[TideMetric, ...]:
    key = canonical_harness(harness)
    return tuple(row for row in _ROWS if row.harness == key)


def policy_metrics_for(harness: object) -> tuple[TideMetric, ...]:
    """Return only metrics backed by the shipped wake-daemon evaluator."""
    key = canonical_harness(harness)
    if key not in {"codex", "cursor"}:
        return ()
    return metrics_for(key)


def policy_metric_for(harness: object, metric: object) -> TideMetric:
    selected = metric_for(harness, metric)
    if selected.harness not in {"codex", "cursor"}:
        raise ProtocolRefusal(
            "tide_evaluator_unavailable",
            f"{selected.harness} has no shipped wake-daemon evaluator; authority: {selected.receipt_path}",
        )
    return selected


def metric_for(harness: object, metric: object) -> TideMetric:
    key = canonical_harness(harness)
    if not isinstance(metric, str):
        metric = ""
    row = _BY_KEY.get((key, metric.strip().casefold()))
    if row is None:
        raise ProtocolRefusal(
            "tide_metric_not_derivable",
            f"metric is not derivable for {key}; authority: {T1_SURVEY}",
        )
    return row

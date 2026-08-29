"""Deterministic event-sequence activity testimony for TUI surfaces."""

from __future__ import annotations

from typing import Dict, Mapping, Sequence, Tuple

from .errors import ProtocolRefusal


ACTIVITY_BUCKETS = 5
ACTIVITY_GLYPHS = "⣀⣠⣤⣶⣿"
NODE_REFERENCE_FIELDS = (
    "sender",
    "recipient",
    "node_id",
    "claimed_sender",
    "claimed_recipient",
    "owner",
    "holder",
)

ActivitySamples = Tuple[int, int, int, int, int]


def _samples(value: Sequence[int]) -> ActivitySamples:
    selected = tuple(value)
    if (
        len(selected) != ACTIVITY_BUCKETS
        or any(
            not isinstance(item, int) or isinstance(item, bool) or item < 0
            for item in selected
        )
    ):
        raise ProtocolRefusal(
            "tui_activity_samples_invalid",
            "activity testimony must contain five non-negative integer buckets",
        )
    return selected  # type: ignore[return-value]


def activity_braille(samples: Sequence[int]) -> str:
    """Render five counts with a visible low-water glyph for measured zero."""

    selected = _samples(samples)
    peak = max(selected)
    if peak == 0:
        return ACTIVITY_GLYPHS[0] * ACTIVITY_BUCKETS
    return "".join(
        ACTIVITY_GLYPHS[min(4, round(value * 4 / peak))]
        for value in selected
    )


def activity_series(
    node_ids: Sequence[str],
    records: Sequence[Mapping[str, object]],
) -> Dict[str, ActivitySamples]:
    """Count exact node references into five ordinal buckets.

    Records retain their supplied order. A record counts at most once for a
    node even if it names that node in more than one ruled actor field.
    """

    nodes = tuple(node_ids)
    if any(not isinstance(node, str) or not node for node in nodes) or len(
        set(nodes)
    ) != len(nodes):
        raise ProtocolRefusal(
            "tui_activity_nodes_invalid",
            "activity node identities must be non-empty and unique",
        )
    buckets = {node: [0] * ACTIVITY_BUCKETS for node in nodes}
    record_count = len(records)
    if record_count == 0:
        return {node: _samples(values) for node, values in buckets.items()}
    node_set = set(nodes)
    for ordinal, record in enumerate(records):
        if not isinstance(record, Mapping):
            raise ProtocolRefusal(
                "tui_activity_record_invalid",
                "activity records must be mappings",
            )
        bucket = min(ACTIVITY_BUCKETS - 1, ordinal * ACTIVITY_BUCKETS // record_count)
        named = {
            value
            for field in NODE_REFERENCE_FIELDS
            if isinstance((value := record.get(field)), str) and value in node_set
        }
        for node in named:
            buckets[node][bucket] += 1
    return {node: _samples(values) for node, values in buckets.items()}


def board_activity(model: object) -> Dict[str, ActivitySamples]:
    """Project only record sequences already held by a HarborBoardModel."""

    nodes = tuple(str(row.get("node_id")) for row in getattr(model, "nodes", ()))
    records = tuple(
        record
        for field in (
            "deliveries",
            "acknowledgments",
            "denials",
            "workers",
            "worker_refusals",
            "worker_receipts",
            "work_items",
        )
        for record in getattr(model, field, ())
    )
    return activity_series(nodes, records)


def _harbor_nodes(
    artifact: Mapping[str, object],
) -> tuple[tuple[str, str, str], ...]:
    found = []
    buses = artifact.get("buses", ())
    if not isinstance(buses, Sequence) or isinstance(buses, (str, bytes)):
        return ()
    for bus in buses:
        if not isinstance(bus, Mapping):
            continue
        bus_id = bus.get("bus_id")
        nodes = bus.get("nodes", ())
        if not isinstance(bus_id, str) or not bus_id:
            continue
        if not isinstance(nodes, Sequence) or isinstance(nodes, (str, bytes)):
            continue
        for node in nodes:
            if not isinstance(node, Mapping):
                continue
            node_id = node.get("id")
            if isinstance(node_id, str) and node_id:
                found.append((bus_id + "/" + node_id, bus_id, node_id))
    return tuple(found)


def harbor_activity(
    artifact: Mapping[str, object],
    records: Sequence[Mapping[str, object]],
) -> Dict[str, ActivitySamples]:
    """Project bus-qualified vessel series from exact route testimony."""

    targets = _harbor_nodes(artifact)
    buckets = {target: [0] * ACTIVITY_BUCKETS for target, _bus, _node in targets}
    vessel_counts: Dict[str, int] = {}
    for _target, _bus, vessel in targets:
        vessel_counts[vessel] = vessel_counts.get(vessel, 0) + 1
    record_count = len(records)
    if record_count == 0:
        return {target: _samples(values) for target, values in buckets.items()}
    for ordinal, record in enumerate(records):
        bucket = min(ACTIVITY_BUCKETS - 1, ordinal * ACTIVITY_BUCKETS // record_count)
        source_bus = record.get("source_bus")
        target_bus = record.get("target_bus")
        sender = record.get("sender")
        recipient = record.get("recipient")
        node_id = record.get("node_id")
        claimed_sender = record.get("claimed_sender")
        for target, bus_id, vessel in targets:
            matches = (
                source_bus == bus_id
                and (sender == vessel or claimed_sender == vessel)
            ) or (
                target_bus == bus_id and recipient == vessel
            ) or (
                node_id == vessel
                and (
                    (sender == vessel and source_bus == bus_id)
                    or (recipient == vessel and target_bus == bus_id)
                    or (source_bus == target_bus == bus_id)
                    or (
                        source_bus is None
                        and target_bus is None
                        and vessel_counts[vessel] == 1
                    )
                )
            )
            if matches:
                buckets[target][bucket] += 1
    return {target: _samples(values) for target, values in buckets.items()}


def live_map_activity(artifact: Mapping[str, object]) -> Dict[str, ActivitySamples]:
    records = artifact.get("envelopes", ())
    if not isinstance(records, Sequence) or isinstance(records, (str, bytes)):
        records = ()
    return harbor_activity(
        artifact,
        tuple(record for record in records if isinstance(record, Mapping)),
    )


def replay_activity(state: object) -> Dict[str, ActivitySamples]:
    artifact = {"buses": getattr(state, "buses", ())}
    records = tuple(
        record
        for record in getattr(state, "events", ())
        if isinstance(record, Mapping)
    )
    return harbor_activity(artifact, records)

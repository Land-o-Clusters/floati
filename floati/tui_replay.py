"""Pure event-indexed state for Flight-Recorder Cinema."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Optional, Sequence, Tuple


@dataclass(frozen=True)
class ReplayRoute:
    source_bus: str
    sender: str
    target_bus: str
    recipient: str


@dataclass(frozen=True)
class ReplayCinemaState:
    artifact: Mapping[str, object]
    events: Tuple[Mapping[str, object], ...]
    all_events: Tuple[Mapping[str, object], ...]
    buses: Tuple[Mapping[str, object], ...]
    relationships: Tuple[Mapping[str, object], ...]
    visible_count: int
    total_count: int
    duration_ms: int
    pulse: Optional[ReplayRoute]
    fault_node: Optional[str]
    fault_code: Optional[str]


def _mappings(value: object) -> Tuple[Mapping[str, object], ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return ()
    return tuple(item for item in value if isinstance(item, Mapping))


def _actor(event: Mapping[str, object]) -> Optional[str]:
    for key in ("node_id", "claimed_sender", "claimed_recipient"):
        value = event.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _derived_harbor(
    events: Sequence[Mapping[str, object]],
) -> tuple[Tuple[Mapping[str, object], ...], Tuple[Mapping[str, object], ...]]:
    del events
    return (), ()


def _harbor(
    artifact: Mapping[str, object],
    events: Sequence[Mapping[str, object]],
) -> tuple[Tuple[Mapping[str, object], ...], Tuple[Mapping[str, object], ...]]:
    harbor = artifact.get("harbor")
    if not isinstance(harbor, Mapping):
        return _derived_harbor(events)
    buses = []
    for bus in _mappings(harbor.get("buses")):
        bus_id = bus.get("bus_id")
        if not isinstance(bus_id, str) or not bus_id:
            continue
        architect = bus.get("architect_node")
        nodes = []
        for node in _mappings(bus.get("nodes")):
            node_id = node.get("id")
            role = node.get("role")
            if isinstance(node_id, str) and node_id:
                nodes.append(
                    {
                        "id": node_id,
                        "role": role if isinstance(role, str) and role else None,
                    }
                )
        buses.append(
            {
                "bus_id": bus_id,
                "architect_node": architect if isinstance(architect, str) else "",
                "nodes": tuple(nodes),
            }
        )
    relationships = []
    for edge in _mappings(harbor.get("relationships")):
        source = edge.get("source")
        target = edge.get("target")
        if isinstance(source, str) and source and isinstance(target, str) and target:
            relationships.append({"source": source, "target": target})
    if not buses:
        return _derived_harbor(events)
    return tuple(buses), tuple(relationships)


def _route(event: Mapping[str, object]) -> Optional[ReplayRoute]:
    values = tuple(
        event.get(key)
        for key in ("source_bus", "sender", "target_bus", "recipient")
    )
    if not all(isinstance(value, str) and value for value in values):
        return None
    return ReplayRoute(values[0], values[1], values[2], values[3])


def _fault(event: Mapping[str, object]) -> tuple[Optional[str], Optional[str]]:
    if event.get("event_class") not in {"denial", "degradation"} and event.get(
        "record_kind"
    ) not in {"worker_refusal", "denial_receipt"}:
        return None, None
    actor = _actor(event)
    code = event.get("reason_code") or event.get("outcome_code") or event.get(
        "transition"
    )
    return actor, code if isinstance(code, str) and code else None


class ReplayCinemaController:
    """Project immutable cinema state from canonical replay sequence order."""

    def __init__(self, artifact: Mapping[str, object]) -> None:
        self.artifact = artifact
        self.events = _mappings(artifact.get("events"))
        self.buses, self.relationships = _harbor(artifact, self.events)

    def state(self, visible_count: int) -> ReplayCinemaState:
        count = min(max(0, int(visible_count)), len(self.events))
        visible = self.events[:count]
        current = visible[-1] if visible else None
        pulse = None if current is None else _route(current)
        fault_node, fault_code = (None, None) if current is None else _fault(current)
        duration = self.artifact.get("duration_ms")
        return ReplayCinemaState(
            artifact=self.artifact,
            events=visible,
            all_events=self.events,
            buses=self.buses,
            relationships=self.relationships,
            visible_count=count,
            total_count=len(self.events),
            duration_ms=(
                duration
                if isinstance(duration, int) and not isinstance(duration, bool) and duration >= 0
                else 0
            ),
            pulse=pulse,
            fault_node=fault_node,
            fault_code=fault_code,
        )

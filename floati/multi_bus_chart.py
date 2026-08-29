"""Declared-root-only multi-bus topology and ASCII Harbor Chart."""

from __future__ import annotations

import json
import os
import stat
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from .errors import ProtocolRefusal
from .events import EVENT_KINDS, validate_event_records
from .jsonl import read_records_snapshot
from .root import FloatiRoot, validate_identifier
from .registry import REGISTRY_KINDS


_MAX_REGISTRY_BYTES = 1024 * 1024


def _read_declared_file(path: Path) -> Dict[str, Any]:
    if not path.is_absolute():
        raise ProtocolRefusal(
            "declared_roots_absolute_required", "declared-roots file must be absolute"
        )
    if path.is_symlink():
        raise ProtocolRefusal(
            "declared_roots_symlinked", "declared-roots file must not be a symlink"
        )
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as exc:
        raise ProtocolRefusal(
            "declared_roots_unavailable", "declared-roots file is unavailable"
        ) from exc
    try:
        identity = os.fstat(descriptor)
        if not stat.S_ISREG(identity.st_mode) or identity.st_size > _MAX_REGISTRY_BYTES:
            raise ProtocolRefusal(
                "declared_roots_invalid", "declared-roots file must be a bounded regular file"
            )
        raw = b""
        while True:
            chunk = os.read(descriptor, 65536)
            if not chunk:
                break
            raw += chunk
    finally:
        os.close(descriptor)
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolRefusal(
            "declared_roots_invalid", "declared-roots file is not valid UTF-8 JSON"
        ) from exc
    if not isinstance(payload, dict):
        raise ProtocolRefusal("declared_roots_invalid", "declared-roots value must be an object")
    return payload


def _parse_time(value: object) -> datetime:
    if not isinstance(value, str):
        raise ProtocolRefusal("chart_timestamp_invalid", "ledger timestamp must be text")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ProtocolRefusal("chart_timestamp_invalid", "ledger timestamp is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ProtocolRefusal("chart_timestamp_invalid", "ledger timestamp needs a UTC offset")
    return parsed.astimezone(timezone.utc)


def _display_time(value: datetime) -> str:
    return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")


class DeclaredRoots:
    """Validate one explicit registry without discovering any other root."""

    def __init__(self, path: os.PathLike[str] | str) -> None:
        self.path = Path(path).expanduser()

    def load(self) -> Tuple[Dict[str, Any], ...]:
        payload = _read_declared_file(self.path)
        if set(payload) != {"schema_version", "roots"}:
            raise ProtocolRefusal(
                "declared_roots_invalid", "declared-roots object has an unexpected shape"
            )
        if payload["schema_version"] != 0 or isinstance(payload["schema_version"], bool):
            raise ProtocolRefusal(
                "declared_roots_invalid", "declared-roots schema version is unsupported"
            )
        roots = payload["roots"]
        if not isinstance(roots, list) or not roots:
            raise ProtocolRefusal(
                "declared_roots_invalid", "declared-roots must contain at least one root"
            )
        entries: List[Dict[str, Any]] = []
        bus_ids = set()
        paths = set()
        for entry in roots:
            if not isinstance(entry, dict) or set(entry) != {
                "bus_id", "root", "architect_node", "downstream"
            }:
                raise ProtocolRefusal(
                    "declared_roots_invalid", "declared root has an unexpected shape"
                )
            bus_id = validate_identifier(entry["bus_id"], "bus")
            architect = validate_identifier(entry["architect_node"], "architect_node")
            raw_root = entry["root"]
            if not isinstance(raw_root, str):
                raise ProtocolRefusal("declared_root_invalid", "declared root must be text")
            root = Path(raw_root)
            if not root.is_absolute():
                raise ProtocolRefusal("declared_root_invalid", "declared root must be absolute")
            if root.is_symlink() or not root.is_dir():
                raise ProtocolRefusal(
                    "declared_root_invalid", "declared root must be an existing non-symlink directory"
                )
            canonical = root.resolve()
            downstream = entry["downstream"]
            if not isinstance(downstream, list):
                raise ProtocolRefusal("declared_roots_invalid", "downstream must be a list")
            targets = [validate_identifier(value, "downstream_bus") for value in downstream]
            if len(targets) != len(set(targets)) or bus_id in targets:
                raise ProtocolRefusal(
                    "declared_roots_invalid", "downstream edges must be unique and non-self"
                )
            if bus_id in bus_ids or canonical in paths:
                raise ProtocolRefusal(
                    "declared_roots_invalid", "declared bus ids and root paths must be unique"
                )
            bus_ids.add(bus_id)
            paths.add(canonical)
            entries.append(
                {
                    "bus_id": bus_id,
                    "root": canonical,
                    "architect_node": architect,
                    "downstream": tuple(sorted(targets)),
                }
            )
        for entry in entries:
            unknown = set(entry["downstream"]) - bus_ids
            if unknown:
                raise ProtocolRefusal(
                    "declared_roots_invalid",
                    f"downstream bus is not declared: {sorted(unknown)[0]}",
                )
        return tuple(sorted(entries, key=lambda item: item["bus_id"]))


class MultiBusHarborChart:
    """Project a multi-root view from declarations and validated ledger bytes."""

    def __init__(
        self,
        declared_roots: os.PathLike[str] | str,
        *,
        now: Optional[datetime] = None,
    ) -> None:
        self.declared_roots = DeclaredRoots(declared_roots)
        self.now = datetime.now(timezone.utc) if now is None else now
        if self.now.tzinfo is None or self.now.utcoffset() is None:
            raise ProtocolRefusal("chart_time_invalid", "chart time must be timezone-aware")
        self.now = self.now.astimezone(timezone.utc)

    def _bus(self, declaration: Mapping[str, Any]) -> Dict[str, Any]:
        root = FloatiRoot.open_direct_home(declaration["root"])
        registry = read_records_snapshot(
            root,
            Path("registry/entries.jsonl"),
            allowed_kinds=REGISTRY_KINDS,
        )
        events = read_records_snapshot(
            root,
            Path("events.jsonl"),
            allowed_kinds=set(EVENT_KINDS),
        )
        validate_event_records(events)
        latest: Dict[str, Dict[str, Any]] = {}
        for record in registry:
            if record["kind"] != "registry_entry":
                continue
            latest[str(record["node_id"])] = record
        nodes = [
            {
                "id": node_id,
                "role": str(record["role"]),
                "state": "active",
            }
            for node_id, record in sorted(latest.items())
            if record["state"] == "active"
        ]
        architect = str(declaration["architect_node"])
        if architect not in {node["id"] for node in nodes}:
            raise ProtocolRefusal(
                "declared_architect_inactive",
                f"declared architect is not active for bus {declaration['bus_id']}",
            )
        activity = [
            _parse_time(record["timestamp"])
            for record in [*registry, *events]
        ]
        last = max(activity) if activity else None
        age = None if last is None else max(0, int((self.now - last).total_seconds()))
        return {
            "bus_id": declaration["bus_id"],
            "root": str(root.path),
            "architect_node": architect,
            "downstream": list(declaration["downstream"]),
            "nodes": nodes,
            "last_activity_at": None if last is None else _display_time(last),
            "last_activity_age_seconds": age,
        }

    def artifact(self) -> Dict[str, Any]:
        declarations = self.declared_roots.load()
        buses = [self._bus(entry) for entry in declarations]
        relationships = [
            {"source": bus["bus_id"], "target": target}
            for bus in buses
            for target in bus["downstream"]
        ]
        return {
            "schema_version": 0,
            "source": "declared_roots_and_ledgers",
            "buses": buses,
            "relationships": relationships,
        }

    def to_json(self) -> str:
        return json.dumps(
            self.artifact(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ) + "\n"


def render_multi_bus_chart(artifact: Mapping[str, Any]) -> str:
    """Render the JSON topology as a deterministic ASCII-only twin."""

    lines = ["FLOATI // MULTI-BUS HARBOR CHART"]
    buses: Sequence[Mapping[str, Any]] = artifact.get("buses", [])
    for bus in buses:
        age = bus.get("last_activity_age_seconds")
        age_text = "unknown" if age is None else f"{int(age)}s"
        lines.append(
            f"{bus['bus_id']} [architect: {bus['architect_node']}] "
            f"[last activity: {age_text}]"
        )
        for node in bus.get("nodes", []):
            lines.append(f"  |-- {node['id']} ({node['role']})")
        if not bus.get("nodes"):
            lines.append("  |-- (no active nodes)")
    relationships: Sequence[Mapping[str, Any]] = artifact.get("relationships", [])
    lines.append("relationships:")
    if not relationships:
        lines.append("  (none)")
    else:
        for edge in relationships:
            lines.append(f"  {edge['source']} -> {edge['target']}")
    return "\n".join(lines) + "\n"

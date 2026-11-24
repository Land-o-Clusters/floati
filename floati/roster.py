"""Measured roster cells. A cell with no bus record is UNKNOWN and has no age."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from .errors import FloatiError
from .jsonl import read_records_snapshot
from .registry import Registry
from .root import FloatiRoot


Fact = Tuple[str, str]
_READ_ERRORS = (FloatiError, OSError, ValueError, TypeError, KeyError)


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("roster time must be aware")
    return value.astimezone(timezone.utc)


def _parse(value: object) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("roster timestamp must be aware")
    return parsed.astimezone(timezone.utc)


def unknown_cell() -> Dict[str, object]:
    return {
        "stamp": "UNKNOWN",
        "value": None,
        "observed_at": None,
        "age_seconds": None,
    }


def _measured(value: str, observed_at: object, now: datetime) -> Dict[str, object]:
    observed = _parse(observed_at)
    if observed > now:
        return unknown_cell()
    return {
        "stamp": "MEASURED",
        "value": value,
        "observed_at": observed.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "age_seconds": int((now - observed).total_seconds()),
    }


def _agree(facts: Sequence[Fact], now: datetime) -> Dict[str, object]:
    if not facts:
        return unknown_cell()
    try:
        chosen = {value for value, _observed in facts}
        if len(chosen) != 1:
            return unknown_cell()
        _value, observed_at = max(facts, key=lambda item: _parse(item[1]))
    except (ValueError, TypeError):
        return unknown_cell()
    return _measured(next(iter(chosen)), observed_at, now)


def _lanes(root: FloatiRoot, node: str) -> Optional[List[Fact]]:
    relative = Path("nodes") / node / "lanes.jsonl"
    if not root.resolve_relative(relative).is_file():
        return []
    records = read_records_snapshot(
        root, relative, allowed_kinds={"lane_workspace_record"}
    )
    opened: Dict[str, Dict[str, object]] = {}
    closed = set()
    for record in records:
        if record.get("node_id") != node or record.get("state") not in {"open", "closed"}:
            return None
        record_id = record.get("id")
        if not isinstance(record_id, str):
            return None
        if record["state"] == "open":
            if record_id in opened:
                return None
            opened[record_id] = record
            continue
        predecessor = record.get("opened_record_id")
        if not isinstance(predecessor, str) or predecessor not in opened or predecessor in closed:
            return None
        closed.add(predecessor)
    facts = []
    for record_id, record in opened.items():
        if record_id in closed:
            continue
        repo = record.get("repo")
        branch = record.get("branch")
        observed = record.get("opened_at")
        if not isinstance(repo, str) or not isinstance(branch, str) or not isinstance(observed, str):
            return None
        facts.append((f"{repo}@{branch}", observed))
    return facts


def _claimed_cars(root: FloatiRoot, node: str) -> Optional[List[Fact]]:
    from .consumption import ConsumptionLedger

    facts: List[Fact] = []
    for item in ConsumptionLedger(root).project().values():
        if item.get("state") != "claimed" or item.get("holder") != node:
            continue
        binding = item.get("car_binding")
        if not isinstance(binding, dict):
            continue
        repository = binding.get("repository")
        branch = binding.get("branch")
        observed = item.get("last_activity")
        if (
            not isinstance(repository, str)
            or not isinstance(branch, str)
            or not isinstance(observed, str)
        ):
            return None
        facts.append((f"{repository}@{branch}", observed))
    return facts


def _repository(root: FloatiRoot, node: str, now: datetime) -> Dict[str, object]:
    try:
        lanes = _lanes(root, node)
        cars = _claimed_cars(root, node)
    except _READ_ERRORS:
        return unknown_cell()
    if lanes is None or cars is None:
        return unknown_cell()
    return _agree([*lanes, *cars], now)


def _thread_activity(root: FloatiRoot, node: str) -> Optional[List[Fact]]:
    from .thread_observations import ThreadObservationLedger

    facts: List[Fact] = []
    for row in ThreadObservationLedger(root).project().attachments():
        attachment = row.get("attachment")
        if not isinstance(attachment, dict) or attachment.get("registered_by") != node:
            continue
        if row.get("detachment") is not None:
            continue
        latest = row.get("latest_observation")
        if not isinstance(latest, dict):
            continue
        status = latest.get("provider_status")
        observed = latest.get("observed_at_testimony")
        if not isinstance(status, dict) or not isinstance(observed, str):
            return None
        if status.get("evidence_class") != "measured":
            continue
        measured = status.get("value")
        if measured == "active":
            facts.append(("busy", observed))
        elif measured == "idle":
            facts.append(("idle", observed))
    return facts


def _worker_activity(root: FloatiRoot, node: str) -> Optional[List[Fact]]:
    from .workers import WorkerReceipts

    facts: List[Fact] = []
    for session in WorkerReceipts(root).sessions():
        if session.get("node_id") != node or session.get("state") != "driving":
            continue
        observed = session.get("last_activity")
        if not isinstance(observed, str):
            return None
        facts.append(("busy", observed))
    return facts


def _activity(root: FloatiRoot, node: str, now: datetime) -> Dict[str, object]:
    try:
        threads = _thread_activity(root, node)
        workers = _worker_activity(root, node)
    except _READ_ERRORS:
        return unknown_cell()
    if threads is None or workers is None:
        return unknown_cell()
    return _agree([*threads, *workers], now)


def _action(root: FloatiRoot, node: str, now: datetime) -> Dict[str, object]:
    from .host_observations import _read_state

    try:
        state = _read_state(root, node)
    except _READ_ERRORS:
        return unknown_cell()
    if not isinstance(state, dict):
        return unknown_cell()
    observed = state.get("observed_at")
    tool_name = state.get("tool_name")
    event = state.get("event")
    if isinstance(tool_name, str) and tool_name:
        value = tool_name
    elif isinstance(event, str) and event:
        value = event
    else:
        return unknown_cell()
    try:
        return _measured(value, observed, now)
    except _READ_ERRORS:
        return unknown_cell()


def _liveness(root: FloatiRoot, node: str, now: datetime) -> Dict[str, object]:
    from .presence import PresenceService

    report = next(
        (item for item in PresenceService(root).reports(now) if item.get("node_id") == node),
        None,
    )
    if not isinstance(report, dict) or report.get("state") == "never_reported":
        return unknown_cell()
    if report.get("state") == "recent_report":
        return _measured("recent_report", report.get("reported_at"), now)
    if report.get("state") != "no_report_since":
        return unknown_cell()
    cell = _measured("no_report_since", report.get("reported_at"), now)
    if cell["stamp"] != "MEASURED":
        return cell
    expires_at = report.get("expires_at")
    if not isinstance(expires_at, str):
        return unknown_cell()
    cell["named_time"] = expires_at
    return cell


def _harness(entry: Dict[str, object], now: datetime) -> Dict[str, object]:
    role = entry.get("role")
    if not isinstance(role, str) or not role:
        return unknown_cell()
    try:
        return _measured(role, entry.get("timestamp"), now)
    except _READ_ERRORS:
        return unknown_cell()


def project_roster(root: FloatiRoot, now: datetime) -> List[Dict[str, object]]:
    """One row per active node. Owner comes only from operator testimony."""

    from .persons import person_records

    current = _utc(now)
    persons = person_records(root)
    rows = []
    for node_id in Registry(root).active_node_ids():
        entry = Registry(root).require_active(node_id)
        person = persons.get(node_id)
        owner = unknown_cell()
        if person is not None and person["person"] is not None:
            owner = _measured(person["person"], person["timestamp"], current)
        try:
            liveness = _liveness(root, node_id, current)
        except _READ_ERRORS:
            liveness = unknown_cell()
        rows.append(
            {
                "node_id": node_id,
                "owner": owner,
                "harness": _harness(entry, current),
                "repository_branch": _repository(root, node_id, current),
                "activity": _activity(root, node_id, current),
                "current_action": _action(root, node_id, current),
                "liveness": liveness,
            }
        )
    return rows

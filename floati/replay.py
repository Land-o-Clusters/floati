"""Deterministic orchestration timeline projected from durable ledgers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, Iterator, Mapping, Optional, Sequence

from .consumption import ConsumptionLedger
from .errors import SnapshotRefusal
from .jsonl import read_records_snapshot
from .root import FloatiRoot
from .snapshot import SnapshotStore, SourceSpec
from .work import WORK_KINDS
from .workers import WORKER_KINDS, WORKER_REFUSAL_KINDS


EVENT_CLASSES = ("claim", "turn", "degradation", "denial", "completion")
REPLAY_SOURCES = (
    (Path("work/items.jsonl"), frozenset(WORK_KINDS)),
    (Path("receipts/workers.jsonl"), frozenset(WORKER_KINDS)),
    (Path("receipts/worker-refusals.jsonl"), frozenset(WORKER_REFUSAL_KINDS)),
    (Path("receipts/denials.jsonl"), frozenset({"denial_receipt"})),
)


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def _event_class(record: Mapping[str, object]) -> str | None:
    kind = record["kind"]
    transition = record.get("transition")
    if kind == "work_item":
        return None
    if kind == "work_transition":
        return "claim" if record["action"] == "claim" else "completion"
    if kind == "worker_receipt":
        if transition == "claim":
            return "claim"
        if transition in {"spawn", "drive", "bind_artifact"}:
            return "turn"
        if transition == "degrade":
            return "degradation"
        if transition == "complete":
            return "completion"
    if kind in {"worker_refusal", "denial_receipt"}:
        return "denial"
    return None


def _normalize(
    record: Mapping[str, object], source: Path, source_ordinal: int
) -> Dict[str, object] | None:
    classified = _event_class(record)
    if classified is None:
        return None
    transition = record.get("transition", record.get("action"))
    if record["kind"] in {"worker_refusal", "denial_receipt"}:
        transition = "refused" if record["kind"] == "worker_refusal" else "denied"
    return {
        "timestamp": record["timestamp"],
        "record_id": record["id"],
        "source": source.as_posix(),
        "source_ordinal": source_ordinal,
        "record_kind": record["kind"],
        "event_class": classified,
        "transition": transition,
        "node_id": record.get("node_id", record.get("actor")),
        "work_item_id": record.get("work_item_id"),
        "session_id": record.get("session_id"),
        "outcome_code": record.get("outcome_code"),
        "reason_code": record.get("reason_code"),
        "claimed_sender": record.get("claimed_sender"),
        "claimed_recipient": record.get("claimed_recipient"),
        "process_id": None,
    }


@dataclass(frozen=True)
class ReplayTimeline:
    events: Sequence[Mapping[str, object]]
    sources: Sequence[str]
    source_ordinals: Mapping[str, int]
    cached_plain: Optional[str] = None
    cached_counts: Optional[Mapping[str, int]] = None
    cached_duration_ms: Optional[int] = None

    @classmethod
    def from_root(cls, root: FloatiRoot) -> "ReplayTimeline":
        store = None
        try:
            store = _replay_snapshot_store(root)
            loaded = store.load()
            return _timeline_from_snapshot(root, loaded.payload, loaded.tails)
        except SnapshotRefusal:
            before_scan = None
            if store is not None:
                try:
                    before_scan = store.capture()
                except SnapshotRefusal:
                    pass
            timeline = _read_full_timeline(root)
            artifact = timeline.artifact()
            from .replay_render import render_replay_plain

            plain = render_replay_plain(artifact)
            payload = {
                "plain": plain,
                "counts": artifact["counts"],
                "duration_ms": artifact["duration_ms"],
                "sources": artifact["sources"],
                "event_count": len(timeline.events),
                "first_timestamp": (
                    str(timeline.events[0]["timestamp"])
                    if timeline.events
                    else None
                ),
                "prior_elapsed_ms": (
                    int(timeline.events[-1]["elapsed_ms"])
                    if timeline.events
                    else 0
                ),
                "source_ordinals": dict(timeline.source_ordinals),
            }
            if store is not None and before_scan is not None:
                try:
                    store.refresh(payload, expected=before_scan)
                except SnapshotRefusal:
                    pass
            return cls(
                timeline.events,
                timeline.sources,
                timeline.source_ordinals,
                plain,
                artifact["counts"],
                int(artifact["duration_ms"]),
            )

    def artifact(self) -> Dict[str, object]:
        if self.cached_counts is None:
            events: Sequence[Mapping[str, object]] = [
                dict(row) for row in self.events
            ]
            counts = {name: 0 for name in EVENT_CLASSES}
            for event in events:
                counts[str(event["event_class"])] += 1
            duration = int(events[-1]["elapsed_ms"]) if events else 0
        else:
            events = (
                self.events
                if isinstance(self.events, _LazyReplayEvents)
                else [dict(row) for row in self.events]
            )
            counts = dict(self.cached_counts)
            duration = int(self.cached_duration_ms or 0)
        return ReplayArtifact(
            {
                "replay_schema_version": 0,
                "events": events,
                "counts": counts,
                "duration_ms": duration,
                "sources": list(self.sources),
            },
            self.cached_plain,
        )


class ReplayArtifact(dict):
    def __init__(self, values: Mapping[str, object], plain: Optional[str]) -> None:
        super().__init__(values)
        self.plain_cache = plain

    def materialized(self) -> Dict[str, object]:
        result = dict(self)
        result["events"] = [dict(row) for row in self["events"]]
        return result


class _LazyReplayEvents(Sequence[Mapping[str, object]]):
    def __init__(self, root: FloatiRoot, count: int) -> None:
        self.root = root
        self.count = count
        self._loaded: Optional[tuple[Mapping[str, object], ...]] = None

    def _events(self) -> tuple[Mapping[str, object], ...]:
        if self._loaded is None:
            self._loaded = tuple(_read_full_timeline(self.root).events)
        return self._loaded

    def __len__(self) -> int:
        return self.count

    def __getitem__(self, index: object) -> object:
        return self._events()[index]

    def __iter__(self) -> Iterator[Mapping[str, object]]:
        return iter(self._events())


def _replay_snapshot_store(root: FloatiRoot) -> SnapshotStore:
    return SnapshotStore(
        root,
        reader="replay-render",
        key="plain-120",
        discover_sources=lambda: tuple(
            SourceSpec(source, kinds) for source, kinds in REPLAY_SOURCES
        ),
    )


def _read_full_timeline(root: FloatiRoot) -> ReplayTimeline:
    events: list[Dict[str, object]] = []
    populated_sources: list[str] = []
    source_ordinals: Dict[str, int] = {}
    for source, kinds in REPLAY_SOURCES:
        records = read_records_snapshot(root, source, allowed_kinds=set(kinds))
        source_ordinals[source.as_posix()] = len(records)
        if source == Path("work/items.jsonl"):
            ConsumptionLedger(root).project(records)
        normalized = [
            event
            for ordinal, record in enumerate(records, start=1)
            if (event := _normalize(record, source, ordinal)) is not None
        ]
        if records:
            populated_sources.append(source.as_posix())
        events.extend(normalized)
    source_rank = {
        source.as_posix(): rank
        for rank, (source, _kinds) in enumerate(REPLAY_SOURCES)
    }
    events.sort(
        key=lambda row: (
            source_rank[str(row["source"])],
            int(row["source_ordinal"]),
        )
    )
    if events:
        start = _parse_time(str(events[0]["timestamp"]))
        prior_elapsed = 0
        for sequence, event in enumerate(events, start=1):
            event["sequence"] = sequence
            observed_elapsed = round(
                (_parse_time(str(event["timestamp"])) - start).total_seconds()
                * 1000
            )
            event["elapsed_ms"] = max(0, prior_elapsed, observed_elapsed)
            prior_elapsed = int(event["elapsed_ms"])
    return ReplayTimeline(
        tuple(events), tuple(sorted(populated_sources)), source_ordinals
    )


def _timeline_from_snapshot(
    root: FloatiRoot,
    payload: Dict[str, object],
    tails: Dict[str, Sequence[Dict[str, object]]],
) -> ReplayTimeline:
    fields = {
        "plain",
        "counts",
        "duration_ms",
        "sources",
        "event_count",
        "first_timestamp",
        "prior_elapsed_ms",
        "source_ordinals",
    }
    if set(payload) != fields:
        raise SnapshotRefusal(
            "snapshot_payload_invalid", "replay snapshot fields are invalid"
        )
    plain = payload["plain"]
    counts = payload["counts"]
    sources = payload["sources"]
    event_count = payload["event_count"]
    first_timestamp = payload["first_timestamp"]
    prior_elapsed = payload["prior_elapsed_ms"]
    source_ordinals = payload["source_ordinals"]
    duration = payload["duration_ms"]
    if (
        not isinstance(plain, str)
        or not isinstance(counts, dict)
        or set(counts) != set(EVENT_CLASSES)
        or not all(isinstance(counts[name], int) for name in EVENT_CLASSES)
        or not isinstance(sources, list)
        or not all(isinstance(source, str) for source in sources)
        or not isinstance(event_count, int)
        or isinstance(event_count, bool)
        or event_count < 0
        or first_timestamp is not None
        and not isinstance(first_timestamp, str)
        or not isinstance(prior_elapsed, int)
        or isinstance(prior_elapsed, bool)
        or prior_elapsed < 0
        or not isinstance(duration, int)
        or isinstance(duration, bool)
        or duration < 0
        or not isinstance(source_ordinals, dict)
        or set(source_ordinals) != {
            source.as_posix() for source, _kinds in REPLAY_SOURCES
        }
        or not all(
            isinstance(value, int) and not isinstance(value, bool) and value >= 0
            for value in source_ordinals.values()
        )
    ):
        raise SnapshotRefusal(
            "snapshot_payload_invalid", "replay snapshot payload is malformed"
        )
    nonempty = {path: records for path, records in tails.items() if records}
    if nonempty:
        ranks = {
            source.as_posix(): rank
            for rank, (source, _kinds) in enumerate(REPLAY_SOURCES)
        }
        latest_rank = max((ranks[source] for source in sources), default=-1)
        if set(nonempty) != {Path("receipts/denials.jsonl").as_posix()} or any(
            ranks[path] < latest_rank for path in nonempty
        ):
            raise SnapshotRefusal(
                "snapshot_tail_history_required",
                "replay tail would precede retained source history",
            )
        denial_path = Path("receipts/denials.jsonl")
        tail_events = []
        for index, record in enumerate(nonempty[denial_path.as_posix()], start=1):
            event = _normalize(
                record,
                denial_path,
                int(source_ordinals[denial_path.as_posix()]) + index,
            )
            if event is not None:
                tail_events.append(event)
        if tail_events:
            if first_timestamp is None:
                first_timestamp = str(tail_events[0]["timestamp"])
            start = _parse_time(str(first_timestamp))
            for sequence, event in enumerate(tail_events, start=event_count + 1):
                event["sequence"] = sequence
                observed_elapsed = round(
                    (_parse_time(str(event["timestamp"])) - start).total_seconds()
                    * 1000
                )
                event["elapsed_ms"] = max(0, prior_elapsed, observed_elapsed)
                prior_elapsed = int(event["elapsed_ms"])
                counts[str(event["event_class"])] += 1
            from .replay_render import (
                REPLAY_SUMMARY,
                REPLAY_UNIT_EVENTS,
                REPLAY_UNIT_MILLISECONDS,
                _event_line,
            )

            lines = plain.splitlines()
            event_count += len(tail_events)
            lines = lines[:-1] + [_event_line(event, 120) for event in tail_events]
            lines.append(
                f"{REPLAY_SUMMARY} // {event_count} {REPLAY_UNIT_EVENTS} // "
                f"{prior_elapsed} {REPLAY_UNIT_MILLISECONDS}"
            )
            plain = "\n".join(lines) + "\n"
            duration = prior_elapsed
            if denial_path.as_posix() not in sources:
                sources.append(denial_path.as_posix())
                sources.sort()
    return ReplayTimeline(
        _LazyReplayEvents(root, event_count),
        tuple(sources),
        {str(key): int(value) for key, value in source_ordinals.items()},
        plain,
        {str(key): int(value) for key, value in counts.items()},
        duration,
    )

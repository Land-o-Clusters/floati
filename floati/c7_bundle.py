"""Read-only C7.1 candidate bundle materialization and verification.

The C7.1 package is deliberately a reader seam.  It never appends to a
tenant ledger, discovers a root, opens a network connection, or selects a
causal order from timestamps.  An explicit caller may copy one already
selected tenant snapshot to an explicit destination and inspect its
physical-frame projection there.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path, PurePosixPath
from typing import Any, Dict, Iterable, Mapping, Sequence

from .decisions import DECISION_KINDS
from .errors import ProtocolRefusal, FloatiError
from .framing import FrameError, decode_frames
from .records import validate_record, validate_repository_coordinate
from .root import FloatiRoot, validate_identifier
from .runtruth import RUN_KINDS, RunProjection
from .work import WORK_KINDS
from .workers import WORKER_KINDS


C7_SCHEMA_VERSION = "c7.1-candidate"
C7_INDEX_KIND = "c7_read_bundle_index"
C7_PROJECTION_KIND = "c7_canonical_projection"
RUN_LEDGER = "runs/events.jsonl"
REGISTRY_LEDGER = "registry/entries.jsonl"
WORKER_LEDGER = "receipts/workers.jsonl"
WORK_ITEM_LEDGER = "work/items.jsonl"
RAW_PREFIX = "raw/"
RAW_RUN_LEDGER = RAW_PREFIX + RUN_LEDGER
_PACKAGE_ROOT = Path(__file__).resolve().parent.parent / "bundle" / "c7.1"
_RUNTIME_ROOT = Path(__file__).resolve().parent.parent
_STATIC_FILES = ("bundle-index.json", "schema-catalog.json", "README.md")
_FAMILY_NAMES = (
    "runs",
    "work_items",
    "attempts",
    "claims",
    "leases",
    "retries",
    "cancellations",
    "result_phases",
    "logical_outcomes",
    "run_outcomes",
    "task_contracts",
    "session_bindings",
    "supervisor_orphans",
    "decisions",
)
SEGMENT_RELATION_VOCABULARY = ("resume", "fork", "handoff")
_INDEX_SCHEMA_IDENTITY = {
    "id": "https://landoclusters.com/floati/schemas/c7.1/c7-read-bundle.schema.json",
    "version": C7_SCHEMA_VERSION,
    "file": "schemas/c7-read-bundle.schema.json",
}
_INDEX_PREDECESSOR = {
    "path": "docs/CONFLUENCE-v0.md",
    "version": 0,
    "mutation": "forbidden",
}
_INDEX_FAMILIES = [
    {
        "name": "run_truth",
        "ledger": RAW_RUN_LEDGER,
        "projection": "families/run-projection.json",
        "causal_order": "physical_frame",
    },
    {
        "name": "worker_receipts",
        "ledger": RAW_PREFIX + WORKER_LEDGER,
        "projection": "raw_fallback_only",
        "causal_order": "evidence_only_no_merge",
    },
    {
        "name": "work_item_context",
        "ledger": RAW_PREFIX + WORK_ITEM_LEDGER,
        "projection": "raw_fallback_only",
        "causal_order": "physical_frame_independent",
    },
    {
        "name": "decision_register",
        "ledger_template": "raw/repositories/<repository-coordinate>/decisions.jsonl",
        "projection_pointer": "/families/decisions",
        "causal_order": "physical_frame_independent",
    },
    {
        "name": "registry_lineage",
        "ledger": RAW_PREFIX + REGISTRY_LEDGER,
        "projection_pointer": "/families/supervisor_orphans",
        "causal_order": "physical_frame_independent",
    },
]
_AUXILIARY_SOURCE_NAMES = (
    "worker_receipts",
    "registry",
    "decisions",
    "work_items",
)
_HEX = frozenset("0123456789abcdef")


def canonical_json_bytes(value: object) -> bytes:
    """Return C7's one compact I-JSON representation for digest domains."""

    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise ProtocolRefusal(
            "c7_not_ijson", "C7 bundle data cannot form canonical I-JSON"
        ) from exc


def semantic_digest(projection: Mapping[str, object]) -> str:
    """Hash the timestamp-free semantic domain, never either output digest.

    `raw_source_digest` deliberately stays outside this domain.  It proves
    the exact testimony bytes, while changing a timestamp alone must not
    change state selection or this semantic digest.
    """

    domain = _semantic_domain(projection)
    return hashlib.sha256(canonical_json_bytes(domain)).hexdigest()


def _semantic_domain(value: object) -> object:
    """Remove every raw-byte digest from the semantic, timestamp-free domain."""

    if isinstance(value, Mapping):
        return {
            str(key): _semantic_domain(item)
            for key, item in value.items()
            if key not in {"raw_source_digest", "semantic_digest", "self_digest"}
        }
    if isinstance(value, list):
        return [_semantic_domain(item) for item in value]
    return value


def self_digest(projection: Mapping[str, object]) -> str:
    """Hash the emitted projection except its self-referential digest field."""

    domain = {key: value for key, value in projection.items() if key != "self_digest"}
    return hashlib.sha256(canonical_json_bytes(domain)).hexdigest()


def _pointer(ledger: str, first: int, last: int) -> Dict[str, object]:
    return {"ledger": ledger, "first_frame": first, "last_frame": last}


def _absent(reason: str, ledger: str, *, first: int = 0, last: int = 0) -> Dict[str, object]:
    return {
        "state": "absent",
        "reason": reason,
        "raw_fallback": _pointer(ledger, first, last),
    }


def _error(code: str, ledger: str, first: int, last: int) -> Dict[str, object]:
    return {
        "state": {
            "kind": "error",
            "code": code,
            "offending_frame_range": _pointer(ledger, first, last),
        },
        "raw_fallback": _pointer(ledger, first, last),
    }


def _present(value: object) -> Dict[str, object]:
    return {"state": "present", "value": value}


def _require_root(root: object) -> FloatiRoot:
    if not isinstance(root, FloatiRoot):
        raise ProtocolRefusal("root_required", "C7 materialization requires an explicit FloatiRoot")
    return root


def _destination_path(value: object) -> Path:
    """Convert only an explicit ordinary path boundary into a Path."""

    try:
        return Path(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise ProtocolRefusal(
            "c7_destination_invalid", "C7 destination must be a path-like value"
        ) from exc


def _read_source(root: FloatiRoot, relative: str) -> bytes:
    path = root.resolve_relative(relative)
    if not path.exists():
        return b""
    if not path.is_file():
        raise ProtocolRefusal("c7_source_not_file", f"C7 source is not a file: {relative}")
    try:
        return path.read_bytes()
    except OSError as exc:
        raise ProtocolRefusal("c7_source_unreadable", f"C7 source cannot be read: {relative}") from exc


def _source_exists(root: FloatiRoot, relative: str) -> bool:
    try:
        return root.resolve_relative(relative).exists()
    except OSError as exc:
        raise ProtocolRefusal("c7_source_unreadable", f"C7 source cannot be read: {relative}") from exc


def _decode_records(
    raw: bytes,
    *,
    ledger: str,
    tenant_id: str,
    allowed_kinds: frozenset[str],
    allowed_schema_versions: frozenset[int] = frozenset({0}),
) -> tuple[list[Dict[str, object]], list[Dict[str, object]], Dict[str, object] | None]:
    """Decode one exact byte stream without creating a second causal order."""

    try:
        decoded = decode_frames(raw)
    except FrameError as exc:
        last = exc.line_number or (raw.count(b"\n") + 1 if raw else 0)
        first = exc.line_number or last
        return [], [], _error(exc.code, ledger, first, last)

    records: list[Dict[str, object]] = []
    frames: list[Dict[str, object]] = []
    seen_ids: set[str] = set()
    for ordinal, raw_record in enumerate(decoded, start=1):
        if not isinstance(raw_record, dict):
            return records, frames, _error("record_not_object", ledger, ordinal, ordinal)
        try:
            record = validate_record(raw_record, tenant_id, allowed_kinds, integrity=True)
        except FloatiError as exc:
            return records, frames, _error(exc.code, ledger, ordinal, ordinal)
        if record.get("schema_version") not in allowed_schema_versions:
            return records, frames, _error(
                "c7_source_schema_version_unsupported", ledger, ordinal, ordinal
            )
        record_id = str(record["id"])
        if record_id in seen_ids:
            return records, frames, _error("duplicate_record_id", ledger, ordinal, ordinal)
        seen_ids.add(record_id)
        records.append(record)
        frames.append(
            {"ordinal": ordinal, "record_id": record_id, "kind": str(record["kind"])}
        )
    return records, frames, None


def _capture_ledger(
    root: FloatiRoot,
    *,
    name: str,
    relative: str,
    allowed_kinds: frozenset[str],
    raw_override: bytes | None = None,
    allowed_schema_versions: frozenset[int] = frozenset({0}),
) -> Dict[str, object]:
    """Capture exact ledger bytes once for both projection and snapshot output."""

    if raw_override is not None and not isinstance(raw_override, bytes):
        raise ProtocolRefusal(
            "c7_raw_run_bytes_invalid", "C7 raw run override must be exact bytes"
        )
    exists = raw_override is not None or _source_exists(root, relative)
    raw = _read_source(root, relative) if raw_override is None else raw_override
    return _capture_bytes(
        name=name,
        relative=relative,
        raw=raw,
        exists=exists,
        tenant_id=root.tenant_id,
        allowed_kinds=allowed_kinds,
        allowed_schema_versions=allowed_schema_versions,
    )


def _capture_bytes(
    *,
    name: str,
    relative: str,
    raw: bytes,
    exists: bool,
    tenant_id: str,
    allowed_kinds: frozenset[str],
    allowed_schema_versions: frozenset[int] = frozenset({0}),
) -> Dict[str, object]:
    """Decode a captured snapshot byte stream without ambient root discovery."""

    if not isinstance(raw, bytes):
        raise ProtocolRefusal("c7_snapshot_bytes_invalid", "C7 snapshot source must be exact bytes")
    ledger = RAW_PREFIX + relative
    records, frames, error = _decode_records(
        raw,
        ledger=ledger,
        tenant_id=tenant_id,
        allowed_kinds=allowed_kinds,
        allowed_schema_versions=allowed_schema_versions,
    )
    return {
        "name": name,
        "relative": relative,
        "ledger": ledger,
        "exists": exists,
        "raw": raw,
        "raw_source_digest": hashlib.sha256(raw).hexdigest(),
        "records": records,
        "source_frames": frames,
        "error": error,
    }


def _capture_sources(
    root: FloatiRoot,
    repository: str,
    *,
    raw_run_bytes: bytes | None = None,
    allowed_schema_versions: frozenset[int] = frozenset({0}),
) -> Dict[str, Dict[str, object]]:
    """Capture every read ledger before a destination write or a projection decision."""

    decision_relative = f"repositories/{repository}/decisions.jsonl"
    captures = {
        "runs": _capture_ledger(
            root,
            name="runs",
            relative=RUN_LEDGER,
            allowed_kinds=frozenset(RUN_KINDS),
            raw_override=raw_run_bytes,
            allowed_schema_versions=allowed_schema_versions,
        ),
        "worker_receipts": _capture_ledger(
            root,
            name="worker_receipts",
            relative=WORKER_LEDGER,
            allowed_kinds=frozenset(WORKER_KINDS),
            allowed_schema_versions=allowed_schema_versions,
        ),
        "registry": _capture_ledger(
            root,
            name="registry",
            relative=REGISTRY_LEDGER,
            allowed_kinds=frozenset({"registry_entry"}),
            allowed_schema_versions=allowed_schema_versions,
        ),
        "decisions": _capture_ledger(
            root,
            name="decisions",
            relative=decision_relative,
            allowed_kinds=frozenset(DECISION_KINDS),
            allowed_schema_versions=allowed_schema_versions,
        ),
        "work_items": _capture_ledger(
            root,
            name="work_items",
            relative=WORK_ITEM_LEDGER,
            allowed_kinds=frozenset(WORK_KINDS),
            allowed_schema_versions=allowed_schema_versions,
        ),
    }
    _bound_decision_repository(captures["decisions"], repository)
    return captures


def _bound_decision_repository(capture: Mapping[str, object], repository: str) -> None:
    """Keep an evidence-only decision ledger bound to its named repository path."""

    if isinstance(capture.get("error"), Mapping):
        return
    records = capture.get("records")
    if not isinstance(records, list):
        raise ProtocolRefusal("c7_capture_invalid", "C7 decision capture is invalid")
    for ordinal, record in enumerate(records, start=1):
        if not isinstance(record, Mapping) or record.get("repository") != repository:
            mutable = capture
            if isinstance(mutable, dict):
                mutable["error"] = _error(
                    "c7_decision_repository_mismatch", str(capture["ledger"]), ordinal, ordinal
                )
            return


def _auxiliary_source(capture: Mapping[str, object]) -> Dict[str, object]:
    """Expose non-causal evidence with exact bytes and physical-frame provenance."""

    ledger = str(capture["ledger"])
    output: Dict[str, object] = {
        "ledger": ledger,
        "raw_source_digest": capture["raw_source_digest"],
        "source_frames": list(capture["source_frames"]),
    }
    error = capture.get("error")
    if isinstance(error, Mapping):
        output.update(dict(error))
    elif not bool(capture["exists"]):
        output.update(_absent("source_absent", ledger))
    else:
        output.update(_present({"frame_count": len(capture["records"])}))
    return output


def _auxiliary_sources(captures: Mapping[str, Mapping[str, object]]) -> Dict[str, object]:
    return {
        name: _auxiliary_source(captures[name])
        for name in _AUXILIARY_SOURCE_NAMES
    }


def _family_frame(records: Sequence[Mapping[str, object]], record_id: object) -> int:
    for ordinal, record in enumerate(records, start=1):
        if record.get("id") == record_id:
            return ordinal
    return 0


def _current_maps(records: Sequence[Dict[str, object]]) -> Dict[str, Dict[str, object]]:
    """Derive every C7 map by the supplied frame sequence, never timestamps."""

    runs: Dict[str, Dict[str, object]] = {}
    work_items: Dict[str, Dict[str, object]] = {}
    attempts: Dict[str, Dict[str, object]] = {}
    retries: Dict[str, Dict[str, object]] = {}
    cancellations: Dict[str, Dict[str, object]] = {}
    results: Dict[str, Dict[str, object]] = {}
    contracts: Dict[str, Dict[str, object]] = {}
    claims: Dict[str, Dict[str, object]] = {}
    leases: Dict[str, Dict[str, object]] = {}
    bindings: Dict[str, list[Dict[str, object]]] = {}

    for ordinal, record in enumerate(records, start=1):
        kind = str(record["kind"])
        run_id = str(record.get("run_id", ""))
        record_id = str(record["id"])
        if kind == "run_created":
            runs[run_id] = {
                "current_state": "open",
                "record_id": record_id,
                "frame": ordinal,
            }
            for item_id in record["item_ids"]:
                key = f"{run_id}:{item_id}"
                work_items[key] = {
                    "run_id": run_id,
                    "item_id": item_id,
                    "current_state": "unresolved",
                    "record_id": record_id,
                    "frame": ordinal,
                }
        elif kind == "run_terminal":
            runs[run_id] = {
                "current_state": "terminal",
                "record_id": record_id,
                "frame": ordinal,
                "outcome": record["outcome"],
            }
        elif kind in {"attempt_opened", "attempt_started", "attempt_terminal"}:
            attempt_id = str(record["attempt_id"])
            states = {
                "attempt_opened": "opened",
                "attempt_started": "started",
                "attempt_terminal": "terminal",
            }
            entry = {
                "run_id": run_id,
                "item_id": record["item_id"],
                "current_state": states[kind],
                "record_id": record_id,
                "frame": ordinal,
            }
            if kind == "attempt_opened":
                entry.update(
                    {
                        "ordinal": record["ordinal"],
                        "fence_token": record["fence_token"],
                    }
                )
            if kind == "attempt_terminal":
                entry["terminal_state"] = record["terminal_state"]
                if record.get("policy_class") is not None:
                    entry["policy_class"] = record["policy_class"]
            attempts[attempt_id] = entry
        elif kind in {"retry_scheduled", "retry_exhausted"}:
            attempt_id = str(
                record["previous_attempt_id"]
                if kind == "retry_scheduled"
                else record["attempt_id"]
            )
            retries[attempt_id] = {
                "current_state": "scheduled" if kind == "retry_scheduled" else "exhausted",
                "record_id": record_id,
                "frame": ordinal,
                "run_id": run_id,
                "item_id": record["item_id"],
            }
        elif kind in {
            "cancel_observed",
            "cancel_signal_sent",
            "cancel_terminal",
            "cancel_unconfirmed",
        }:
            states = {
                "cancel_observed": "observed",
                "cancel_signal_sent": "signal_sent",
                "cancel_terminal": "terminal",
                "cancel_unconfirmed": "unconfirmed",
            }
            cancellations[str(record["attempt_id"])] = {
                "current_state": states[kind],
                "record_id": record_id,
                "frame": ordinal,
                "run_id": run_id,
                "item_id": record["item_id"],
                "cancel_mode": record["cancel_mode"],
            }
        elif kind in {"result_produced", "result_verified", "result_accepted"}:
            states = {
                "result_produced": "produced",
                "result_verified": "verified",
                "result_accepted": "accepted",
            }
            results[str(record["attempt_id"])] = {
                "current_state": states[kind],
                "record_id": record_id,
                "frame": ordinal,
                "run_id": run_id,
                "item_id": record["item_id"],
            }
        elif kind == "task_contract":
            contracts[f"{run_id}:{record['item_id']}"] = {
                "task_contract_id": record_id,
                "contract_digest": record["contract_digest"],
                "frame": ordinal,
            }
        elif kind == "plan_amendment":
            contracts[f"{run_id}:{record['item_id']}"] = {
                "task_contract_id": record["task_contract_id"],
                "contract_digest": record["contract_digest"],
                "frame": ordinal,
                "amendment_record_id": record_id,
            }
        elif kind == "attempt_harness_session_bound":
            attempt_id = str(record["attempt_id"])
            entry = {
                "binding_record_id": record_id,
                "frame": ordinal,
                "run_id": run_id,
                "item_id": record["item_id"],
                "claim_id": record["claim_id"],
                "lease_id": record["lease_id"],
                "worker_session_id": record["worker_session_id"],
                "segments": [
                    {
                        "source_ref": {
                            "binding_record_id": record_id,
                            "ordinal": segment["ordinal"],
                        },
                        "harness_session_id": segment["harness_session_id"],
                        "segment_kind": _absent(
                            "not_durable_c7_1",
                            RAW_RUN_LEDGER,
                            first=ordinal,
                            last=ordinal,
                        ),
                        "predecessor_segment_id": _absent(
                            "not_durable_c7_1",
                            RAW_RUN_LEDGER,
                            first=ordinal,
                            last=ordinal,
                        ),
                    }
                    for segment in record["harness_segments"]
                ],
            }
            bindings.setdefault(attempt_id, []).append(entry)
            for family, identifier in ((claims, record["claim_id"]), (leases, record["lease_id"])):
                family.setdefault(
                    str(identifier),
                    {"opaque_identifier": identifier, "frames": []},
                )["frames"].append(ordinal)
        elif kind == "supervisor_orphaned":
            for family, identifier in ((claims, record["claim_id"]), (leases, record["lease_id"])):
                family.setdefault(
                    str(identifier),
                    {"opaque_identifier": identifier, "frames": []},
                )["frames"].append(ordinal)

    return {
        "runs": runs,
        "work_items": work_items,
        "attempts": attempts,
        "retries": retries,
        "cancellations": cancellations,
        "result_phases": results,
        "task_contracts": contracts,
        "claims": claims,
        "leases": leases,
        "session_bindings": _binding_map(bindings),
    }


def _apply_logical_outcomes(
    maps: Mapping[str, Dict[str, object]], projection: RunProjection, record_count: int
) -> tuple[Dict[str, Dict[str, object]], Dict[str, Dict[str, object]]]:
    logical: Dict[str, Dict[str, object]] = {}
    outcomes: Dict[str, Dict[str, object]] = {}
    for run_id in maps["runs"]:
        for item_id, outcome in projection.item_outcomes(run_id).items():
            key = f"{run_id}:{item_id}"
            work_item = maps["work_items"].get(key)
            if work_item is not None:
                work_item["current_state"] = outcome
            logical[f"{run_id}:{item_id}"] = {
                "outcome": outcome,
                "raw_fallback": _pointer(RAW_RUN_LEDGER, 1, record_count),
            }
        outcomes[run_id] = {
            "outcome": projection.run_outcome(run_id),
            "raw_fallback": _pointer(RAW_RUN_LEDGER, 1, record_count),
        }
    return logical, outcomes


def _binding_map(
    bindings: Mapping[str, Sequence[Dict[str, object]]]
) -> Dict[str, object]:
    """Expose source bindings without inventing segment relations or winners."""

    projected: Dict[str, object] = {}
    for attempt_id, candidates in bindings.items():
        candidates = list(candidates)
        keys = {
            (candidate["claim_id"], candidate["lease_id"], candidate["worker_session_id"])
            for candidate in candidates
        }
        seen_sessions: set[object] = set()
        overlap = False
        for candidate in candidates:
            session_values = [
                segment["harness_session_id"] for segment in candidate["segments"]
            ]
            sessions = set(session_values)
            if len(sessions) != len(session_values):
                overlap = True
            if seen_sessions & sessions:
                overlap = True
            seen_sessions.update(sessions)
        first = min(int(candidate["frame"]) for candidate in candidates)
        last = max(int(candidate["frame"]) for candidate in candidates)
        if len(keys) > 1 or overlap:
            projected[attempt_id] = {
                **_error("conflicting_binding", RAW_RUN_LEDGER, first, last),
                "candidates": candidates,
            }
            continue
        current = candidates[-1]
        current = dict(current)
        if len(candidates) > 1:
            current["supersession"] = {
                "rule": "physical_binding_frame_order",
                "superseded_binding_record_ids": [
                    candidate["binding_record_id"] for candidate in candidates[:-1]
                ],
            }
        projected[attempt_id] = current
    return projected


def _registry_lineage(capture: Mapping[str, object]) -> Dict[str, object]:
    error = capture.get("error")
    if isinstance(error, Mapping):
        return dict(error)
    ledger = str(capture["ledger"])
    records = capture["records"]
    if not isinstance(records, list):
        raise ProtocolRefusal("c7_capture_invalid", "C7 registry capture is invalid")
    matches = [
        (ordinal, record)
        for ordinal, record in enumerate(records, start=1)
        if isinstance(record, Mapping) and record.get("node_id") == "floati-supervisor"
    ]
    if not matches:
        return _absent("floati_supervisor_not_registered", ledger)
    ordinal, record = matches[-1]
    return {
        "ledger": ledger,
        "frame": ordinal,
        "record_id": record["id"],
        "node_id": record["node_id"],
        "role": record["role"],
        "state": record["state"],
    }


def _orphan_map(
    records: Sequence[Dict[str, object]], registry_capture: Mapping[str, object]
) -> Dict[str, Dict[str, object]]:
    lineage = _registry_lineage(registry_capture)
    output: Dict[str, Dict[str, object]] = {}
    for ordinal, record in enumerate(records, start=1):
        if record["kind"] != "supervisor_orphaned":
            continue
        output[str(record["id"])] = {
            "record_id": record["id"],
            "frame": ordinal,
            "run_id": record["run_id"],
            "item_id": record["item_id"],
            "attempt_id": record["attempt_id"],
            "claim_id": record["claim_id"],
            "lease_id": record["lease_id"],
            "worker_session_id": record["worker_session_id"],
            "orphan_class": record["orphan_class"],
            "supervisor_id": record["supervisor_id"],
            "registration_lineage": lineage,
        }
    return output


def _decision_family(capture: Mapping[str, object]) -> Dict[str, object]:
    error = capture.get("error")
    if isinstance(error, Mapping):
        return dict(error)
    ledger = str(capture["ledger"])
    records = capture["records"]
    frames = capture["source_frames"]
    if not isinstance(records, list) or not isinstance(frames, list):
        raise ProtocolRefusal("c7_capture_invalid", "C7 decision capture is invalid")
    if not records:
        return _absent("decision_register_absent", ledger)
    return _present(
        {
            "ledger": ledger,
            "frames": [
                {
                    "frame": frame["ordinal"],
                    "record_id": record["id"],
                    "decision_id": record["decision_id"],
                    "status": record["status"],
                }
                for frame, record in zip(frames, records)
            ],
            "consolidation": "excluded-c7.1",
        }
    )


def _records_reference_workers(records: Sequence[Mapping[str, object]]) -> bool:
    """Whether run-state conclusions depend on raw worker evidence."""

    for record in records:
        kind = record.get("kind")
        field = (
            "evidence_bindings"
            if kind == "acceptance_receipt"
            else "worker_receipt_ids"
            if kind
            in {
                "result_produced",
                "result_verified",
                "result_accepted",
                "stale_attempt_evidence",
            }
            else None
        )
        if field is not None and bool(record.get(field)):
            return True
    return False


def _worker_dependent_error(
    capture: Mapping[str, object], code: str = "worker_receipt_invalid"
) -> Dict[str, object]:
    error = capture.get("error")
    if isinstance(error, Mapping):
        return dict(error)
    frames = capture.get("source_frames")
    last = len(frames) if isinstance(frames, list) else 0
    return _error(code, str(capture["ledger"]), 0, last)


def _session_family(value: Mapping[str, object] | None = None) -> Dict[str, object]:
    family = (
        _present(dict(value))
        if value
        else _absent("no_session_binding_frames", RAW_RUN_LEDGER)
    )
    family["segment_relation_vocabulary"] = list(SEGMENT_RELATION_VOCABULARY)
    return family


def _run_error_families(
    error: Mapping[str, object], decisions: Mapping[str, object]
) -> Dict[str, object]:
    families: Dict[str, object] = {
        name: dict(error) for name in _FAMILY_NAMES if name not in {"decisions", "session_bindings"}
    }
    session_error = dict(error)
    session_error["segment_relation_vocabulary"] = list(SEGMENT_RELATION_VOCABULARY)
    families["session_bindings"] = session_error
    families["decisions"] = dict(decisions)
    return families


def project_c7_1(
    root: FloatiRoot,
    *,
    repository: str,
    raw_run_bytes: bytes | None = None,
    _captured_sources: Mapping[str, Mapping[str, object]] | None = None,
) -> Dict[str, object]:
    """Project one already-selected tenant root in exact run-frame order.

    A malformed run ledger produces typed per-family errors and leaves its raw
    fallback pointer intact.  A malformed decision register remains isolated
    to that family; it never rewrites run state.
    """

    root = _require_root(root)
    repository = validate_repository_coordinate(repository)
    captures = (
        dict(_captured_sources)
        if _captured_sources is not None
        else _capture_sources(root, repository, raw_run_bytes=raw_run_bytes)
    )
    return _project_from_captures(
        captures,
        tenant_id=root.tenant_id,
        repository=repository,
    )


def _project_from_captures(
    captures: Mapping[str, Mapping[str, object]], *, tenant_id: str, repository: str
) -> Dict[str, object]:
    """Project only exact captured bytes; used by writer and reader alike."""

    tenant_id = validate_identifier(tenant_id, "c7_tenant")
    repository = validate_repository_coordinate(repository)
    if set(captures) != {"runs", "worker_receipts", "registry", "decisions", "work_items"}:
        raise ProtocolRefusal("c7_capture_invalid", "C7 capture sources are incomplete")
    _bound_decision_repository(captures["decisions"], repository)
    run_capture = captures["runs"]
    records = run_capture["records"]
    source_frames = run_capture["source_frames"]
    error = run_capture.get("error")
    if not isinstance(records, list) or not isinstance(source_frames, list):
        raise ProtocolRefusal("c7_capture_invalid", "C7 run capture is invalid")
    decisions = _decision_family(captures["decisions"])
    if isinstance(error, Mapping):
        return _finish_projection(
            run_capture,
            _run_error_families(error, decisions),
            captures,
            tenant_id=tenant_id,
            repository=repository,
        )

    maps = _current_maps(records)
    worker_capture = captures["worker_receipts"]
    worker_error = worker_capture.get("error")
    dependent_worker_error: Dict[str, object] | None = None
    canonical: RunProjection | None = None
    if isinstance(worker_error, Mapping):
        if _records_reference_workers(records):
            dependent_worker_error = _worker_dependent_error(worker_capture)
        else:
            try:
                canonical = RunProjection.from_records(records, (), integrity=True)
            except FloatiError as exc:
                failure = _error(exc.code, RAW_RUN_LEDGER, 1 if records else 0, len(records))
                return _finish_projection(
                    run_capture,
                    _run_error_families(failure, decisions),
                    captures,
                    tenant_id=tenant_id,
                    repository=repository,
                )
    else:
        worker_records = worker_capture["records"]
        if not isinstance(worker_records, list):
            raise ProtocolRefusal("c7_capture_invalid", "C7 worker capture is invalid")
        try:
            canonical = RunProjection.from_records(records, worker_records, integrity=True)
        except FloatiError as exc:
            if _records_reference_workers(records) and exc.code in {
                "worker_receipt_invalid",
                "acceptance_receipt_invalid",
            }:
                dependent_worker_error = _worker_dependent_error(worker_capture, exc.code)
            else:
                failure = _error(exc.code, RAW_RUN_LEDGER, 1 if records else 0, len(records))
                return _finish_projection(
                    run_capture,
                    _run_error_families(failure, decisions),
                    captures,
                    tenant_id=tenant_id,
                    repository=repository,
                )

    if canonical is not None:
        logical, outcomes = _apply_logical_outcomes(maps, canonical, len(records))
        result_family: Dict[str, object] = (
            _present(maps["result_phases"])
            if maps["result_phases"]
            else _absent("no_result_frames", RAW_RUN_LEDGER)
        )
        logical_family: Dict[str, object] = (
            _present(logical) if logical else _absent("no_logical_outcomes", RAW_RUN_LEDGER)
        )
        outcome_family: Dict[str, object] = (
            _present(outcomes) if outcomes else _absent("no_run_outcomes", RAW_RUN_LEDGER)
        )
    elif dependent_worker_error is not None:
        result_family = dict(dependent_worker_error)
        logical_family = dict(dependent_worker_error)
        outcome_family = dict(dependent_worker_error)
    else:
        raise ProtocolRefusal("c7_projection_invalid", "C7 projection could not select a state")

    orphans = _orphan_map(records, captures["registry"])
    families = {
        "runs": _present(maps["runs"]) if maps["runs"] else _absent("no_run_frames", RAW_RUN_LEDGER),
        "work_items": _present(maps["work_items"]) if maps["work_items"] else _absent("no_run_items", RAW_RUN_LEDGER),
        "attempts": _present(maps["attempts"]) if maps["attempts"] else _absent("no_attempt_frames", RAW_RUN_LEDGER),
        "claims": _present(maps["claims"]) if maps["claims"] else _absent("no_opaque_claim_references", RAW_RUN_LEDGER),
        "leases": _present(maps["leases"]) if maps["leases"] else _absent("no_opaque_lease_references", RAW_RUN_LEDGER),
        "retries": _present(maps["retries"]) if maps["retries"] else _absent("no_retry_frames", RAW_RUN_LEDGER),
        "cancellations": _present(maps["cancellations"]) if maps["cancellations"] else _absent("no_cancellation_frames", RAW_RUN_LEDGER),
        "result_phases": result_family,
        "logical_outcomes": logical_family,
        "run_outcomes": outcome_family,
        "task_contracts": _present(maps["task_contracts"]) if maps["task_contracts"] else _absent("no_task_contract_frames", RAW_RUN_LEDGER),
        "session_bindings": _session_family(maps["session_bindings"]),
        "supervisor_orphans": _present(orphans) if orphans else _absent("no_supervisor_orphan_frames", RAW_RUN_LEDGER),
        "decisions": decisions,
    }
    return _finish_projection(
        run_capture,
        families,
        captures,
        tenant_id=tenant_id,
        repository=repository,
    )


def _finish_projection(
    run_capture: Mapping[str, object],
    families: Mapping[str, object],
    captures: Mapping[str, Mapping[str, object]],
    *,
    tenant_id: str,
    repository: str,
) -> Dict[str, object]:
    projection: Dict[str, object] = {
        "schema_version": C7_SCHEMA_VERSION,
        "kind": C7_PROJECTION_KIND,
        "tenant_id": tenant_id,
        "repository": repository,
        "raw_source": RAW_RUN_LEDGER,
        "raw_source_digest": run_capture["raw_source_digest"],
        "source_frames": list(run_capture["source_frames"]),
        "families": dict(families),
        "auxiliary_sources": _auxiliary_sources(captures),
        "cross_ledger_rule": "no_timestamp_merge",
    }
    projection["semantic_digest"] = semantic_digest(projection)
    projection["self_digest"] = self_digest(projection)
    return projection


def _load_json(path: Path, code: str) -> Dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolRefusal(code, f"C7 JSON cannot be read: {path}") from exc
    if not isinstance(value, dict):
        raise ProtocolRefusal(code, f"C7 JSON must be an object: {path}")
    return value


def _shape(condition: bool, detail: str) -> None:
    if not condition:
        raise ProtocolRefusal("c7_projection_shape_invalid", detail)


def _is_integer(value: object, *, minimum: int = 0) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= minimum


def _is_digest(value: object) -> bool:
    return isinstance(value, str) and len(value) == 64 and set(value) <= _HEX


def _is_nonempty_string(value: object) -> bool:
    return isinstance(value, str) and bool(value)


def _as_object(value: object, detail: str) -> Mapping[str, object]:
    _shape(isinstance(value, Mapping), detail)
    return value  # type: ignore[return-value]


def _exact_keys(value: Mapping[str, object], keys: set[str], detail: str) -> None:
    _shape(set(value) == keys, detail)


def _contains_none(value: object) -> bool:
    if value is None:
        return True
    if isinstance(value, Mapping):
        return any(_contains_none(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_none(item) for item in value)
    return False


def _validate_frame_range(value: object, detail: str) -> None:
    row = _as_object(value, detail)
    _exact_keys(row, {"ledger", "first_frame", "last_frame"}, detail)
    _shape(_is_nonempty_string(row.get("ledger")), detail)
    _shape(_is_integer(row.get("first_frame")), detail)
    _shape(_is_integer(row.get("last_frame")), detail)
    _shape(row["first_frame"] <= row["last_frame"], detail)


def _validate_source_frames(value: object, detail: str) -> Dict[str, tuple[int, str]]:
    _shape(isinstance(value, list) and len(value) <= 100000, detail)
    index: Dict[str, tuple[int, str]] = {}
    for expected_ordinal, frame in enumerate(value, start=1):
        row = _as_object(frame, detail)
        _exact_keys(row, {"ordinal", "record_id", "kind"}, detail)
        _shape(row.get("ordinal") == expected_ordinal, detail)
        _shape(_is_nonempty_string(row.get("record_id")), detail)
        _shape(_is_nonempty_string(row.get("kind")), detail)
        record_id = str(row["record_id"])
        _shape(record_id not in index, detail)
        index[record_id] = (expected_ordinal, str(row["kind"]))
    return index


def _validate_absent(value: object, detail: str) -> None:
    row = _as_object(value, detail)
    _exact_keys(row, {"state", "reason", "raw_fallback"}, detail)
    _shape(row.get("state") == "absent", detail)
    _shape(_is_nonempty_string(row.get("reason")), detail)
    _validate_frame_range(row.get("raw_fallback"), detail)


def _validate_error(value: object, detail: str) -> None:
    row = _as_object(value, detail)
    _exact_keys(row, {"state", "raw_fallback"}, detail)
    state = _as_object(row.get("state"), detail)
    _exact_keys(state, {"kind", "code", "offending_frame_range"}, detail)
    _shape(state.get("kind") == "error", detail)
    _shape(_is_nonempty_string(state.get("code")), detail)
    _validate_frame_range(state.get("offending_frame_range"), detail)
    _validate_frame_range(row.get("raw_fallback"), detail)


def _validate_family(
    value: object, detail: str, *, extras: set[str] | None = None
) -> str:
    row = _as_object(value, detail)
    extra_keys = set() if extras is None else extras
    state = row.get("state")
    if state == "present":
        _exact_keys(row, {"state", "value"} | extra_keys, detail)
        _shape(isinstance(row.get("value"), Mapping), detail)
        return "present"
    if state == "absent":
        _exact_keys(row, {"state", "reason", "raw_fallback"} | extra_keys, detail)
        _validate_absent(
            {
                "state": row.get("state"),
                "reason": row.get("reason"),
                "raw_fallback": row.get("raw_fallback"),
            },
            detail,
        )
        return "absent"
    _shape(isinstance(state, Mapping), detail)
    _exact_keys(row, {"state", "raw_fallback"} | extra_keys, detail)
    _validate_error(
        {"state": row.get("state"), "raw_fallback": row.get("raw_fallback")},
        detail,
    )
    return "error"


def _source_frame(
    source_index: Mapping[str, tuple[int, str]], record_id: object, detail: str
) -> tuple[int, str]:
    _shape(_is_nonempty_string(record_id), detail)
    _shape(str(record_id) in source_index, detail)
    return source_index[str(record_id)]


def _validate_map_entry(
    value: object,
    detail: str,
    source_index: Mapping[str, tuple[int, str]],
    *,
    required: set[str],
    allowed: set[str],
    kinds: set[str],
) -> tuple[int, str]:
    row = _as_object(value, detail)
    _shape(required <= set(row) <= allowed, detail)
    ordinal, kind = _source_frame(source_index, row.get("record_id"), detail)
    _shape(kind in kinds, detail)
    _shape(_is_integer(row.get("frame"), minimum=1), detail)
    _shape(row["frame"] == ordinal, detail)
    for name in required - {"record_id", "frame"}:
        if name == "ordinal":
            _shape(_is_integer(row.get(name), minimum=1), detail)
        else:
            _shape(_is_nonempty_string(row.get(name)), detail)
    return ordinal, kind


def _validate_run_fallback(
    value: object, source_index: Mapping[str, tuple[int, str]], detail: str
) -> None:
    _validate_frame_range(value, detail)
    pointer = _as_object(value, detail)
    _shape(pointer.get("ledger") == RAW_RUN_LEDGER, detail)
    if source_index:
        _shape(pointer.get("first_frame") >= 1, detail)
        _shape(pointer.get("last_frame") <= len(source_index), detail)


def _validate_current_map(
    family: str, value: object, source_index: Mapping[str, tuple[int, str]]
) -> None:
    row = _as_object(value, f"C7 {family} family is not an object")
    mapping = _as_object(row["value"], f"C7 {family} current-state map is not an object")
    _shape(bool(mapping), f"C7 {family} present map cannot be empty")
    standard: Dict[str, tuple[set[str], set[str], set[str]]] = {
        "runs": (
            {"current_state", "record_id", "frame"},
            {"current_state", "record_id", "frame", "outcome"},
            {"run_created", "run_terminal"},
        ),
        "work_items": (
            {"run_id", "item_id", "current_state", "record_id", "frame"},
            {"run_id", "item_id", "current_state", "record_id", "frame"},
            {"run_created"},
        ),
        "attempts": (
            {"run_id", "item_id", "current_state", "record_id", "frame"},
            {
                "run_id", "item_id", "current_state", "record_id", "frame",
                "ordinal", "fence_token", "terminal_state", "policy_class",
            },
            {"attempt_opened", "attempt_started", "attempt_terminal"},
        ),
        "retries": (
            {"run_id", "item_id", "current_state", "record_id", "frame"},
            {"run_id", "item_id", "current_state", "record_id", "frame"},
            {"retry_scheduled", "retry_exhausted"},
        ),
        "cancellations": (
            {"run_id", "item_id", "current_state", "record_id", "frame"},
            {"run_id", "item_id", "current_state", "record_id", "frame", "cancel_mode"},
            {"cancel_observed", "cancel_signal_sent", "cancel_terminal", "cancel_unconfirmed"},
        ),
        "result_phases": (
            {"run_id", "item_id", "current_state", "record_id", "frame"},
            {"run_id", "item_id", "current_state", "record_id", "frame"},
            {"result_produced", "result_verified", "result_accepted"},
        ),
    }
    for key, entry in mapping.items():
        _shape(_is_nonempty_string(key), f"C7 {family} map key is invalid")
        if family in standard:
            required, allowed, kinds = standard[family]
            _ordinal, kind = _validate_map_entry(
                entry,
                f"C7 {family} map entry is invalid",
                source_index,
                required=required,
                allowed=allowed,
                kinds=kinds,
            )
            item = _as_object(entry, f"C7 {family} map entry is invalid")
            if family == "runs":
                _shape((kind == "run_terminal") == ("outcome" in item), f"C7 {family} map entry is invalid")
            if family == "attempts":
                if kind == "attempt_opened":
                    _shape({"ordinal", "fence_token"} <= set(item), f"C7 {family} map entry is invalid")
                elif kind == "attempt_terminal":
                    _shape("terminal_state" in item, f"C7 {family} map entry is invalid")
                else:
                    _shape(not ({"ordinal", "fence_token", "terminal_state", "policy_class"} & set(item)), f"C7 {family} map entry is invalid")
        elif family == "task_contracts":
            item = _as_object(entry, "C7 task-contract entry is invalid")
            allowed = {"task_contract_id", "contract_digest", "frame", "amendment_record_id"}
            _shape({"task_contract_id", "contract_digest", "frame"} <= set(item) and set(item) <= allowed, "C7 task-contract entry is invalid")
            contract_ordinal, contract_kind = _source_frame(source_index, item.get("task_contract_id"), "C7 task-contract entry is invalid")
            _shape(contract_kind == "task_contract", "C7 task-contract entry is invalid")
            _shape(_is_nonempty_string(item.get("contract_digest")), "C7 task-contract entry is invalid")
            _shape(_is_integer(item.get("frame"), minimum=1), "C7 task-contract entry is invalid")
            if "amendment_record_id" in item:
                amendment_ordinal, amendment_kind = _source_frame(source_index, item.get("amendment_record_id"), "C7 task-contract entry is invalid")
                _shape(amendment_kind == "plan_amendment", "C7 task-contract entry is invalid")
                _shape(item.get("frame") == amendment_ordinal, "C7 task-contract entry is invalid")
            else:
                _shape(item.get("frame") == contract_ordinal, "C7 task-contract entry is invalid")
        elif family in {"claims", "leases"}:
            item = _as_object(entry, f"C7 {family} entry is invalid")
            _exact_keys(item, {"opaque_identifier", "frames"}, f"C7 {family} entry is invalid")
            _shape(_is_nonempty_string(item.get("opaque_identifier")), f"C7 {family} entry is invalid")
            frames = item.get("frames")
            _shape(isinstance(frames, list) and bool(frames), f"C7 {family} frames are invalid")
            _shape(all(_is_integer(frame, minimum=1) for frame in frames), f"C7 {family} frames are invalid")
            _shape(frames == sorted(set(frames)), f"C7 {family} frames are invalid")
            allowed_kinds = {"attempt_harness_session_bound", "supervisor_orphaned"}
            ordinals = {ordinal: kind for ordinal, kind in source_index.values()}
            _shape(all(ordinals.get(frame) in allowed_kinds for frame in frames), f"C7 {family} frames are invalid")
        elif family in {"logical_outcomes", "run_outcomes"}:
            item = _as_object(entry, f"C7 {family} entry is invalid")
            _exact_keys(item, {"outcome", "raw_fallback"}, f"C7 {family} entry is invalid")
            _shape(_is_nonempty_string(item.get("outcome")), f"C7 {family} outcome is invalid")
            _validate_run_fallback(item.get("raw_fallback"), source_index, f"C7 {family} fallback is invalid")


def _validate_binding(
    value: object, source_index: Mapping[str, tuple[int, str]], detail: str
) -> None:
    row = _as_object(value, detail)
    allowed = {
        "binding_record_id", "frame", "run_id", "item_id", "claim_id", "lease_id",
        "worker_session_id", "segments", "supersession",
    }
    _shape(set(row) <= allowed and {"binding_record_id", "frame", "run_id", "item_id", "claim_id", "lease_id", "worker_session_id", "segments"} <= set(row), detail)
    binding_ordinal, binding_kind = _source_frame(source_index, row.get("binding_record_id"), detail)
    _shape(binding_kind == "attempt_harness_session_bound", detail)
    _shape(_is_integer(row.get("frame"), minimum=1), detail)
    _shape(row["frame"] == binding_ordinal, detail)
    for field in ("run_id", "item_id", "claim_id", "lease_id", "worker_session_id"):
        _shape(_is_nonempty_string(row.get(field)), detail)
    segments = row.get("segments")
    _shape(isinstance(segments, list) and bool(segments), detail)
    segment_ordinals: list[int] = []
    for segment in segments:
        item = _as_object(segment, detail)
        _exact_keys(item, {"source_ref", "harness_session_id", "segment_kind", "predecessor_segment_id"}, detail)
        source_ref = _as_object(item.get("source_ref"), detail)
        _exact_keys(source_ref, {"binding_record_id", "ordinal"}, detail)
        _shape(source_ref.get("binding_record_id") == row["binding_record_id"], detail)
        _shape(_is_integer(source_ref.get("ordinal"), minimum=1), detail)
        segment_ordinals.append(int(source_ref["ordinal"]))
        _shape(_is_nonempty_string(item.get("harness_session_id")), detail)
        _validate_absent(item.get("segment_kind"), detail)
        _validate_absent(item.get("predecessor_segment_id"), detail)
        for field in ("segment_kind", "predecessor_segment_id"):
            absent = _as_object(item[field], detail)
            fallback = absent.get("raw_fallback")
            _validate_run_fallback(fallback, source_index, detail)
            pointer = _as_object(fallback, detail)
            _shape(
                pointer.get("first_frame") == binding_ordinal
                and pointer.get("last_frame") == binding_ordinal,
                detail,
            )
    _shape(len(segment_ordinals) == len(set(segment_ordinals)), detail)
    if "supersession" in row:
        supersession = _as_object(row["supersession"], detail)
        _exact_keys(supersession, {"rule", "superseded_binding_record_ids"}, detail)
        _shape(supersession.get("rule") == "physical_binding_frame_order", detail)
        prior = supersession.get("superseded_binding_record_ids")
        _shape(isinstance(prior, list) and bool(prior), detail)
        _shape(all(_is_nonempty_string(item) for item in prior), detail)
        _shape(len(prior) == len(set(prior)), detail)
        for record_id in prior:
            prior_ordinal, prior_kind = _source_frame(source_index, record_id, detail)
            _shape(prior_kind == "attempt_harness_session_bound", detail)
            _shape(prior_ordinal < binding_ordinal and record_id != row["binding_record_id"], detail)


def _validate_session_family(
    value: object, source_index: Mapping[str, tuple[int, str]]
) -> None:
    row = _as_object(value, "C7 session binding family is invalid")
    state = _validate_family(row, "C7 session binding family is invalid", extras={"segment_relation_vocabulary"})
    _shape(row.get("segment_relation_vocabulary") == list(SEGMENT_RELATION_VOCABULARY), "C7 segment vocabulary is invalid")
    if state != "present":
        return
    mapping = _as_object(row["value"], "C7 session binding map is invalid")
    _shape(bool(mapping), "C7 session binding map cannot be empty")
    for attempt_id, entry in mapping.items():
        _shape(_is_nonempty_string(attempt_id), "C7 session binding key is invalid")
        candidate = _as_object(entry, "C7 session binding entry is invalid")
        if "state" not in candidate:
            _validate_binding(candidate, source_index, "C7 session binding entry is invalid")
            continue
        _exact_keys(candidate, {"state", "raw_fallback", "candidates"}, "C7 conflicting binding is invalid")
        _validate_error(
            {"state": candidate.get("state"), "raw_fallback": candidate.get("raw_fallback")},
            "C7 conflicting binding is invalid",
        )
        state_value = _as_object(candidate["state"], "C7 conflicting binding is invalid")
        _shape(state_value.get("code") == "conflicting_binding", "C7 conflicting binding code is invalid")
        candidates = candidate.get("candidates")
        _shape(isinstance(candidates, list) and bool(candidates), "C7 conflicting binding candidates are invalid")
        for binding in candidates:
            _validate_binding(binding, source_index, "C7 conflicting binding candidate is invalid")


def _validate_supervisor_orphans(
    value: object,
    source_index: Mapping[str, tuple[int, str]],
    registry_index: Mapping[str, tuple[int, str]],
) -> None:
    state = _validate_family(value, "C7 supervisor orphan family is invalid")
    if state != "present":
        return
    mapping = _as_object(_as_object(value, "C7 supervisor orphan family is invalid")["value"], "C7 supervisor orphan map is invalid")
    _shape(bool(mapping), "C7 supervisor orphan map cannot be empty")
    required = {
        "record_id", "frame", "run_id", "item_id", "attempt_id", "claim_id", "lease_id",
        "worker_session_id", "orphan_class", "supervisor_id", "registration_lineage",
    }
    for entry in mapping.values():
        row = _as_object(entry, "C7 supervisor orphan entry is invalid")
        _exact_keys(row, required, "C7 supervisor orphan entry is invalid")
        ordinal, kind = _source_frame(source_index, row.get("record_id"), "C7 supervisor orphan entry is invalid")
        _shape(kind == "supervisor_orphaned", "C7 supervisor orphan entry is invalid")
        _shape(row.get("frame") == ordinal, "C7 supervisor orphan entry is invalid")
        for field in ("run_id", "item_id", "attempt_id", "claim_id", "lease_id", "worker_session_id"):
            _shape(_is_nonempty_string(row.get(field)), "C7 supervisor orphan entry is invalid")
        _shape(row.get("orphan_class") in {"owner_loss", "unregister", "lease_abandonment"}, "C7 orphan class is invalid")
        _shape(row.get("supervisor_id") == "floati-supervisor", "C7 supervisor identity is invalid")
        lineage = _as_object(row.get("registration_lineage"), "C7 registration lineage is invalid")
        if lineage.get("state") in {"absent", "error"}:
            _validate_family(lineage, "C7 registration lineage is invalid")
            fallback = lineage.get("raw_fallback")
            _validate_frame_range(fallback, "C7 registration lineage is invalid")
            _shape(
                _as_object(fallback, "C7 registration lineage is invalid").get("ledger")
                == RAW_PREFIX + REGISTRY_LEDGER,
                "C7 registration lineage is invalid",
            )
        else:
            _exact_keys(lineage, {"ledger", "frame", "record_id", "node_id", "role", "state"}, "C7 registration lineage is invalid")
            _shape(lineage.get("ledger") == RAW_PREFIX + REGISTRY_LEDGER, "C7 registration lineage is invalid")
            _shape(_is_integer(lineage.get("frame"), minimum=1), "C7 registration lineage is invalid")
            lineage_ordinal, lineage_kind = _source_frame(
                registry_index, lineage.get("record_id"), "C7 registration lineage is invalid"
            )
            _shape(lineage_kind == "registry_entry", "C7 registration lineage is invalid")
            _shape(lineage.get("frame") == lineage_ordinal, "C7 registration lineage is invalid")
            _shape(lineage.get("node_id") == "floati-supervisor", "C7 registration lineage is invalid")
            _shape(_is_nonempty_string(lineage.get("role")), "C7 registration lineage is invalid")
            _shape(_is_nonempty_string(lineage.get("state")), "C7 registration lineage is invalid")


def _validate_decisions(
    value: object, source_index: Mapping[str, tuple[int, str]]
) -> None:
    state = _validate_family(value, "C7 decision family is invalid")
    if state != "present":
        return
    row = _as_object(_as_object(value, "C7 decision family is invalid")["value"], "C7 decision map is invalid")
    _exact_keys(row, {"ledger", "frames", "consolidation"}, "C7 decision map is invalid")
    _shape(_is_nonempty_string(row.get("ledger")), "C7 decision ledger is invalid")
    _shape(row.get("consolidation") == "excluded-c7.1", "C7 decision consolidation is invalid")
    frames = row.get("frames")
    _shape(isinstance(frames, list), "C7 decision frames are invalid")
    for ordinal, frame in enumerate(frames, start=1):
        item = _as_object(frame, "C7 decision frame is invalid")
        _exact_keys(item, {"frame", "record_id", "decision_id", "status"}, "C7 decision frame is invalid")
        source_ordinal, source_kind = _source_frame(
            source_index, item.get("record_id"), "C7 decision frame is invalid"
        )
        _shape(source_kind == "decision_record", "C7 decision frame is invalid")
        _shape(item.get("frame") == ordinal == source_ordinal, "C7 decision frame is invalid")
        _shape(_is_nonempty_string(item.get("decision_id")), "C7 decision frame is invalid")
        _shape(item.get("status") in {"proposed", "accepted", "rejected"}, "C7 decision status is invalid")


def _validate_auxiliary_sources(
    value: object, repository: str
) -> Dict[str, Dict[str, tuple[int, str]]]:
    sources = _as_object(value, "C7 auxiliary sources are invalid")
    _exact_keys(sources, set(_AUXILIARY_SOURCE_NAMES), "C7 auxiliary sources are invalid")
    expected = {
        "worker_receipts": RAW_PREFIX + WORKER_LEDGER,
        "registry": RAW_PREFIX + REGISTRY_LEDGER,
        "work_items": RAW_PREFIX + WORK_ITEM_LEDGER,
    }
    indexes: Dict[str, Dict[str, tuple[int, str]]] = {}
    for name in _AUXILIARY_SOURCE_NAMES:
        row = _as_object(sources[name], "C7 auxiliary source is invalid")
        _shape({"ledger", "raw_source_digest", "source_frames"} <= set(row), "C7 auxiliary source is invalid")
        state = _validate_family(
            {key: row[key] for key in row if key not in {"ledger", "raw_source_digest", "source_frames"}},
            "C7 auxiliary source state is invalid",
        )
        _shape(_is_digest(row.get("raw_source_digest")), "C7 auxiliary source digest is invalid")
        indexes[name] = _validate_source_frames(row.get("source_frames"), "C7 auxiliary source frames are invalid")
        ledger = row.get("ledger")
        if name in expected:
            _shape(ledger == expected[name], "C7 auxiliary source ledger is invalid")
        else:
            _shape(
                ledger == RAW_PREFIX + f"repositories/{repository}/decisions.jsonl",
                "C7 decision source ledger is invalid",
            )
        if state == "present":
            body = _as_object(row["value"], "C7 auxiliary source value is invalid")
            _exact_keys(body, {"frame_count"}, "C7 auxiliary source value is invalid")
            _shape(body.get("frame_count") == len(row["source_frames"]), "C7 auxiliary source frame count is invalid")
    return indexes


def validate_c7_1_index(index: Mapping[str, object]) -> Dict[str, object]:
    """Fail closed before selecting an unsupported C7 index version."""

    if index.get("schema_version") != C7_SCHEMA_VERSION:
        raise ProtocolRefusal("c7_version_unsupported", "C7 index version is not understood")
    expected_keys = {
        "schema_version", "kind", "title", "approvals", "reader_upgrade", "index_schema",
        "predecessor", "schema_catalog", "families",
    }
    if "index_schema" not in index:
        raise ProtocolRefusal("c7_index_schema_invalid", "C7 index schema identity is absent")
    if set(index) != expected_keys:
        raise ProtocolRefusal("c7_index_shape_invalid", "C7 index fields are not understood")
    if index.get("kind") != C7_INDEX_KIND:
        raise ProtocolRefusal("c7_index_kind_invalid", "C7 index kind is not understood")
    if index.get("approvals") != "excluded-c7.1":
        raise ProtocolRefusal("c7_approvals_not_excluded", "C7.1 cannot infer approval joins")
    title = index.get("title")
    if not isinstance(title, str) or not title:
        raise ProtocolRefusal("c7_index_title_invalid", "C7 index needs a non-empty title")
    upgrade = index.get("reader_upgrade")
    if (
        not isinstance(upgrade, dict)
        or upgrade.get("highest_understood") is not True
        or upgrade.get("unknown") != "fail_closed"
    ):
        raise ProtocolRefusal("c7_upgrade_rule_invalid", "C7 index must fail closed for unknown versions")
    if index.get("index_schema") != _INDEX_SCHEMA_IDENTITY:
        raise ProtocolRefusal(
            "c7_index_schema_invalid", "C7 index schema identity is not understood"
        )
    if index.get("predecessor") != _INDEX_PREDECESSOR:
        raise ProtocolRefusal(
            "c7_predecessor_invalid", "C7 index must preserve its frozen C7 v0 predecessor"
        )
    if index.get("schema_catalog") != "schema-catalog.json":
        raise ProtocolRefusal("c7_schema_catalog_invalid", "C7 schema catalog path is not understood")
    families = index.get("families")
    if families != _INDEX_FAMILIES:
        raise ProtocolRefusal("c7_families_invalid", "C7 index needs its declared read families")
    return dict(index)


def _relative_path(value: object, code: str, detail: str) -> PurePosixPath:
    if not isinstance(value, str) or not value:
        raise ProtocolRefusal(code, detail)
    pure = PurePosixPath(value)
    if pure.is_absolute() or not pure.parts or any(part in {"", ".", ".."} for part in pure.parts):
        raise ProtocolRefusal(code, detail)
    return pure


def validate_c7_1_catalog(catalog: Mapping[str, object]) -> Dict[str, object]:
    """Validate the static source inventory before it controls any copied path."""

    expected = {"schema_version", "kind", "projection_schema", "entries"}
    if set(catalog) != expected:
        raise ProtocolRefusal("c7_catalog_shape_invalid", "C7 catalog fields are not understood")
    if catalog.get("schema_version") != C7_SCHEMA_VERSION or catalog.get("kind") != "c7_schema_catalog":
        raise ProtocolRefusal("c7_catalog_shape_invalid", "C7 catalog identity is not understood")
    projection_schema = catalog.get("projection_schema")
    if not isinstance(projection_schema, Mapping) or set(projection_schema) != {"id", "version", "file", "sha256"}:
        raise ProtocolRefusal("c7_catalog_shape_invalid", "C7 projection schema entry is invalid")
    if (
        projection_schema.get("id") != "https://landoclusters.com/floati/schemas/c7.1/canonical-projection.schema.json"
        or projection_schema.get("version") != C7_SCHEMA_VERSION
        or projection_schema.get("file") != "schemas/canonical-projection.schema.json"
        or not _is_digest(projection_schema.get("sha256"))
    ):
        raise ProtocolRefusal("c7_catalog_shape_invalid", "C7 projection schema entry is invalid")
    entries = catalog.get("entries")
    if not isinstance(entries, list) or not entries:
        raise ProtocolRefusal("c7_catalog_shape_invalid", "C7 catalog entries are invalid")
    source_fields = {"id", "version", "file", "sha256", "pointers", "ledger", "state_role"}
    entry_fields = {"family", "ledger", "ledger_template", "representation", "reason", "sources", "exposure"}
    family_names: set[str] = set()
    for entry in entries:
        if not isinstance(entry, Mapping) or not set(entry) <= entry_fields:
            raise ProtocolRefusal("c7_catalog_shape_invalid", "C7 catalog family entry is invalid")
        if not _is_nonempty_string(entry.get("family")) or not isinstance(entry.get("sources"), list):
            raise ProtocolRefusal("c7_catalog_shape_invalid", "C7 catalog family entry is invalid")
        family = str(entry["family"])
        if family in family_names:
            raise ProtocolRefusal("c7_catalog_shape_invalid", "C7 catalog family is duplicated")
        family_names.add(family)
        for source in entry["sources"]:
            if not isinstance(source, Mapping) or not set(source) <= source_fields:
                raise ProtocolRefusal("c7_catalog_shape_invalid", "C7 catalog source is invalid")
            if not {"id", "version", "file", "sha256", "pointers"} <= set(source):
                raise ProtocolRefusal("c7_catalog_shape_invalid", "C7 catalog source is invalid")
            version = source.get("version")
            if not _is_nonempty_string(source.get("id")) or not (
                (type(version) is int and version == 0) or version == C7_SCHEMA_VERSION
            ):
                raise ProtocolRefusal("c7_catalog_shape_invalid", "C7 catalog source is invalid")
            _relative_path(source.get("file"), "c7_catalog_shape_invalid", "C7 catalog source path is invalid")
            if not _is_digest(source.get("sha256")):
                raise ProtocolRefusal("c7_catalog_shape_invalid", "C7 catalog source digest is invalid")
            pointers = source.get("pointers")
            if not isinstance(pointers, list) or not all(isinstance(pointer, str) and pointer.startswith("/") for pointer in pointers):
                raise ProtocolRefusal("c7_catalog_shape_invalid", "C7 catalog pointers are invalid")
    return dict(catalog)


def _safe_regular_file(root: Path, relative: object, code: str, detail: str) -> Path:
    pure = _relative_path(relative, code, detail)
    if _has_unsafe_symlink_component(root) or root.is_symlink() or not root.is_dir():
        raise ProtocolRefusal(code, detail)
    path = root
    for part in pure.parts:
        path = path / part
        if path.is_symlink():
            raise ProtocolRefusal(code, detail)
    if not path.is_file():
        raise ProtocolRefusal(code, detail)
    return path


def _static_inventory(package: Path) -> list[PurePosixPath]:
    if package.is_symlink() or not package.is_dir():
        raise ProtocolRefusal("c7_contract_package_missing", "checked-in C7.1 contract package is absent")
    files: list[PurePosixPath] = []
    for path in sorted(package.rglob("*")):
        if path.is_symlink():
            raise ProtocolRefusal("c7_contract_package_invalid", "C7 contract package contains a symlink")
        if path.is_file():
            files.append(PurePosixPath(path.relative_to(package).as_posix()))
    if not files:
        raise ProtocolRefusal("c7_contract_package_missing", "checked-in C7.1 contract package is absent")
    return files


def _catalog_schema_source_path(source: Mapping[str, object]) -> Path:
    version = source["version"]
    base = _PACKAGE_ROOT if version == C7_SCHEMA_VERSION else _RUNTIME_ROOT
    path = _safe_regular_file(
        base,
        source["file"],
        "c7_catalog_schema_missing",
        "C7 catalog schema source is unavailable",
    )
    if hashlib.sha256(path.read_bytes()).hexdigest() != source["sha256"]:
        raise ProtocolRefusal("c7_catalog_schema_digest_invalid", "C7 catalog schema digest is invalid")
    return path


def _verify_static_inventory(root: Path) -> None:
    package = _PACKAGE_ROOT.resolve()
    for relative in _static_inventory(package):
        expected = _safe_regular_file(
            package, relative.as_posix(), "c7_contract_package_invalid", "C7 contract package is invalid"
        )
        actual = _safe_regular_file(
            root, relative.as_posix(), "c7_static_inventory_invalid", "C7 static contract file is invalid"
        )
        if hashlib.sha256(expected.read_bytes()).hexdigest() != hashlib.sha256(actual.read_bytes()).hexdigest():
            raise ProtocolRefusal("c7_static_inventory_invalid", "C7 static contract file digest is invalid")


def validate_c7_1_projection(projection: Mapping[str, object]) -> Dict[str, object]:
    """Verify the two non-recursive C7 digest domains before returning data."""

    expected_keys = {
        "schema_version", "kind", "tenant_id", "repository", "raw_source", "raw_source_digest", "source_frames",
        "families", "auxiliary_sources", "cross_ledger_rule", "semantic_digest", "self_digest",
    }
    _shape(set(projection) == expected_keys, "C7 projection fields are not understood")
    _shape(not _contains_none(projection), "C7 projection cannot use null as unknown")
    if projection.get("schema_version") != C7_SCHEMA_VERSION:
        raise ProtocolRefusal("c7_projection_version_invalid", "C7 projection version is not understood")
    if projection.get("kind") != C7_PROJECTION_KIND:
        raise ProtocolRefusal("c7_projection_kind_invalid", "C7 projection kind is not understood")
    try:
        validate_identifier(projection.get("tenant_id"), "c7_tenant")
    except ProtocolRefusal as exc:
        raise ProtocolRefusal("c7_projection_tenant_invalid", "C7 projection tenant is invalid") from exc
    try:
        validate_repository_coordinate(projection.get("repository"))
    except (ProtocolRefusal, FloatiError) as exc:
        raise ProtocolRefusal("c7_projection_repository_invalid", "C7 projection repository is invalid") from exc
    _shape(projection.get("raw_source") == RAW_RUN_LEDGER, "C7 raw source path is invalid")
    _shape(_is_digest(projection.get("raw_source_digest")), "C7 raw source digest is invalid")
    source_index = _validate_source_frames(projection.get("source_frames"), "C7 source frames are invalid")
    families = _as_object(projection.get("families"), "C7 families are invalid")
    _exact_keys(families, set(_FAMILY_NAMES), "C7 family names are invalid")
    current_map_families = {
        "runs", "work_items", "attempts", "claims", "leases", "retries", "cancellations",
        "result_phases", "logical_outcomes", "run_outcomes", "task_contracts",
    }
    for name in current_map_families:
        state = _validate_family(families[name], f"C7 {name} family is invalid")
        if state == "present":
            _validate_current_map(name, families[name], source_index)
    auxiliary_indexes = _validate_auxiliary_sources(
        projection.get("auxiliary_sources"), str(projection["repository"])
    )
    _validate_session_family(families["session_bindings"], source_index)
    _validate_supervisor_orphans(
        families["supervisor_orphans"], source_index, auxiliary_indexes["registry"]
    )
    _validate_decisions(families["decisions"], auxiliary_indexes["decisions"])
    _shape(projection.get("cross_ledger_rule") == "no_timestamp_merge", "C7 cross-ledger rule is invalid")
    _shape(_is_digest(projection.get("semantic_digest")), "C7 semantic digest is invalid")
    _shape(_is_digest(projection.get("self_digest")), "C7 self digest is invalid")
    if projection.get("semantic_digest") != semantic_digest(projection):
        raise ProtocolRefusal("c7_semantic_digest_invalid", "C7 semantic projection digest does not match")
    if projection.get("self_digest") != self_digest(projection):
        raise ProtocolRefusal("c7_self_digest_invalid", "C7 self digest does not match")
    return dict(projection)


def _catalog_schema_entries(catalog: Mapping[str, object]) -> Iterable[Mapping[str, object]]:
    projection_schema = catalog.get("projection_schema")
    if isinstance(projection_schema, Mapping):
        yield projection_schema
    entries = catalog.get("entries")
    if not isinstance(entries, list):
        return
    for entry in entries:
        if not isinstance(entry, Mapping):
            continue
        sources = entry.get("sources")
        if not isinstance(sources, list):
            continue
        for source in sources:
            if isinstance(source, Mapping):
                yield source


def _copy_catalog_schemas(destination: Path, catalog: Mapping[str, object]) -> None:
    copied: set[tuple[object, object]] = set()
    for source in _catalog_schema_entries(catalog):
        key = (source["version"], source["file"])
        if key in copied:
            continue
        copied.add(key)
        if source["version"] == C7_SCHEMA_VERSION:
            # The checked-in package copy is authoritative and was copied as
            # part of the static inventory above.
            continue
        source_path = _catalog_schema_source_path(source)
        target = _output_path(
            destination,
            str(source["file"]),
            "c7_destination_unwritable",
            "C7 catalog schema cannot be copied",
        )
        try:
            shutil.copyfile(source_path, target)
        except OSError as exc:
            raise ProtocolRefusal(
                "c7_destination_unwritable", "C7 catalog schema cannot be copied"
            ) from exc


def _verify_catalog_schemas(root: Path, catalog: Mapping[str, object]) -> None:
    seen: set[tuple[object, object]] = set()
    for source in _catalog_schema_entries(catalog):
        key = (source["version"], source["file"])
        if key in seen:
            continue
        seen.add(key)
        path = _safe_regular_file(
            root, source["file"], "c7_catalog_schema_missing", "C7 copied catalog schema is unavailable"
        )
        if hashlib.sha256(path.read_bytes()).hexdigest() != source["sha256"]:
            raise ProtocolRefusal("c7_catalog_schema_digest_invalid", "C7 copied catalog schema digest is invalid")
        schema = _load_json(path, "c7_catalog_schema_invalid")
        if schema.get("$id") != source["id"]:
            raise ProtocolRefusal("c7_catalog_schema_invalid", "C7 copied catalog schema identity is invalid")
        properties = schema.get("properties")
        if not isinstance(properties, Mapping):
            raise ProtocolRefusal("c7_catalog_schema_invalid", "C7 copied catalog schema is invalid")
        version = properties.get("schema_version")
        if not isinstance(version, Mapping) or version.get("const") != source["version"]:
            raise ProtocolRefusal("c7_catalog_schema_invalid", "C7 copied catalog schema version is invalid")


def _platform_alias(path: Path) -> bool:
    """Permit macOS's canonical /tmp and /var spelling while rejecting caller links."""

    expected = {"/tmp": "/private/tmp", "/var": "/private/var"}
    literal = str(path)
    if literal not in expected:
        return False
    try:
        return str(path.resolve()) == expected[literal]
    except OSError:
        return False


def _has_unsafe_symlink_component(path: Path) -> bool:
    """Inspect lexical path components before resolving a caller-controlled output path."""

    try:
        absolute = Path(os.path.abspath(path))
    except OSError as exc:
        raise ProtocolRefusal("c7_destination_invalid", "C7 path cannot be resolved") from exc
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current = current / part
        try:
            if current.is_symlink() and not _platform_alias(current):
                return True
        except OSError as exc:
            raise ProtocolRefusal("c7_destination_invalid", "C7 path cannot be inspected") from exc
    return False


def _output_path(destination: Path, relative: str, code: str, detail: str) -> Path:
    """Create a new output path only through ordinary, non-symlinked parents."""

    pure = _relative_path(relative, code, detail)
    if destination.is_symlink() or _has_unsafe_symlink_component(destination):
        raise ProtocolRefusal("c7_destination_symlink", "C7 destination traverses a symlink")
    if not destination.is_dir():
        raise ProtocolRefusal(code, detail)
    current = destination
    for part in pure.parts[:-1]:
        current = current / part
        if current.is_symlink():
            raise ProtocolRefusal("c7_destination_symlink", "C7 destination traverses a symlink")
        if current.exists():
            if not current.is_dir():
                raise ProtocolRefusal(code, detail)
        else:
            try:
                current.mkdir()
            except OSError as exc:
                raise ProtocolRefusal(code, detail) from exc
        if current.is_symlink() or not current.is_dir():
            raise ProtocolRefusal("c7_destination_symlink", "C7 destination traverses a symlink")
    target = current / pure.parts[-1]
    if target.exists() or target.is_symlink():
        if target.is_symlink():
            raise ProtocolRefusal("c7_destination_symlink", "C7 destination traverses a symlink")
        raise ProtocolRefusal("c7_destination_not_fresh", "C7 snapshot destination must be fresh")
    return target


def _preflight_destination(root: FloatiRoot, destination: Path) -> Path:
    """Reject every pre-existing output link before even one destination write."""

    if not destination.is_absolute():
        raise ProtocolRefusal("c7_destination_not_absolute", "C7 destination must be absolute")
    if _has_unsafe_symlink_component(destination):
        raise ProtocolRefusal("c7_destination_symlink", "C7 destination traverses a symlink")
    try:
        source_home = root.tenant_home.resolve()
        package = _PACKAGE_ROOT.resolve()
        resolved_destination = destination.resolve(strict=False)
    except OSError as exc:
        raise ProtocolRefusal("c7_destination_invalid", "C7 destination cannot be resolved") from exc
    for protected, code, detail in (
        (source_home, "c7_destination_inside_source", "C7 snapshot destination cannot be the source tenant or a descendant"),
        (package, "c7_destination_contract_package", "C7 snapshots require a destination outside the checked-in contract package"),
    ):
        try:
            resolved_destination.relative_to(protected)
        except ValueError:
            continue
        raise ProtocolRefusal(code, detail)
    if destination.exists() or destination.is_symlink():
        if destination.is_symlink():
            raise ProtocolRefusal("c7_destination_symlink", "C7 destination traverses a symlink")
        if not destination.is_dir():
            raise ProtocolRefusal("c7_destination_invalid", "C7 destination is not a directory")
        nonempty = False
        try:
            for current, directories, filenames in os.walk(destination, followlinks=False):
                for name in [*directories, *filenames]:
                    child = Path(current) / name
                    if child.is_symlink():
                        raise ProtocolRefusal("c7_destination_symlink", "C7 destination traverses a symlink")
                    nonempty = True
        except OSError as exc:
            raise ProtocolRefusal("c7_destination_invalid", "C7 destination cannot be inspected") from exc
        if nonempty:
            raise ProtocolRefusal("c7_destination_not_fresh", "C7 snapshot destination must be fresh")
    return destination


def _copy_contract_package(destination: Path) -> Dict[str, object]:
    package = _PACKAGE_ROOT
    inventory = _static_inventory(package)
    index = validate_c7_1_index(
        _load_json(
            _safe_regular_file(package, "bundle-index.json", "c7_contract_package_missing", "C7 index is absent"),
            "c7_index_unreadable",
        )
    )
    catalog = validate_c7_1_catalog(
        _load_json(
            _safe_regular_file(package, "schema-catalog.json", "c7_contract_package_missing", "C7 catalog is absent"),
            "c7_catalog_unreadable",
        )
    )
    for source in _catalog_schema_entries(catalog):
        _catalog_schema_source_path(source)
    for relative in inventory:
        source = _safe_regular_file(
            package,
            relative.as_posix(),
            "c7_contract_package_invalid",
            "C7 contract package file is invalid",
        )
        target = _output_path(
            destination,
            relative.as_posix(),
            "c7_destination_unwritable",
            "C7 static contract file cannot be copied",
        )
        try:
            shutil.copyfile(source, target)
        except OSError as exc:
            raise ProtocolRefusal(
                "c7_destination_unwritable", "C7 static contract file cannot be copied"
            ) from exc
    _copy_catalog_schemas(destination, catalog)
    return index


def _write_snapshot_source(destination: Path, relative: str, raw: bytes) -> None:
    if not isinstance(raw, bytes):
        raise ProtocolRefusal("c7_snapshot_bytes_invalid", "C7 snapshot source must be exact bytes")
    target = _output_path(
        destination,
        RAW_PREFIX + relative,
        "c7_destination_unwritable",
        f"C7 snapshot cannot write: {relative}",
    )
    try:
        target.write_bytes(raw)
    except OSError as exc:
        raise ProtocolRefusal("c7_destination_unwritable", f"C7 snapshot cannot write: {relative}") from exc


def build_c7_1_bundle(
    root: FloatiRoot,
    destination: Path | str,
    *,
    repository: str,
) -> Dict[str, object]:
    """Materialize one self-contained, explicit read-only snapshot."""

    root = _require_root(root)
    repository = validate_repository_coordinate(repository)
    destination_path = _preflight_destination(root, _destination_path(destination))
    captures = _capture_sources(root, repository)
    projection = _project_from_captures(
        captures, tenant_id=root.tenant_id, repository=repository
    )
    try:
        destination_path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ProtocolRefusal("c7_destination_unwritable", "C7 destination cannot be created") from exc
    index = _copy_contract_package(destination_path)
    for name in ("runs", "worker_receipts", "registry", "decisions", "work_items"):
        capture = captures[name]
        _write_snapshot_source(destination_path, str(capture["relative"]), capture["raw"])
    target = _output_path(
        destination_path,
        "families/run-projection.json",
        "c7_destination_unwritable",
        "C7 projection cannot be written",
    )
    try:
        target.write_bytes(canonical_json_bytes(projection) + b"\n")
    except OSError as exc:
        raise ProtocolRefusal("c7_destination_unwritable", "C7 projection cannot be written") from exc
    return index


def _load_bundle_json(root: Path, relative: str, code: str) -> Dict[str, object]:
    return _load_json(_safe_regular_file(root, relative, code, "C7 bundle file is unavailable"), code)


def _read_bundle_bytes(root: Path, relative: str, code: str) -> bytes:
    path = _safe_regular_file(root, relative, code, "C7 bundle file is unavailable")
    try:
        return path.read_bytes()
    except OSError as exc:
        raise ProtocolRefusal(code, "C7 bundle file cannot be read") from exc


def _pointer_ledgers(value: object) -> Iterable[str]:
    if isinstance(value, Mapping):
        if set(value) == {"ledger", "first_frame", "last_frame"}:
            ledger = value.get("ledger")
            if isinstance(ledger, str):
                yield ledger
            return
        for item in value.values():
            yield from _pointer_ledgers(item)
    elif isinstance(value, list):
        for item in value:
            yield from _pointer_ledgers(item)


def _snapshot_captures(
    root: Path, projection: Mapping[str, object]
) -> Dict[str, Dict[str, object]]:
    """Recreate the writer's capture input only from checked snapshot bytes."""

    tenant_id = validate_identifier(projection.get("tenant_id"), "c7_tenant")
    repository = validate_repository_coordinate(projection.get("repository"))
    auxiliary = projection.get("auxiliary_sources")
    if not isinstance(auxiliary, Mapping):
        raise ProtocolRefusal("c7_projection_shape_invalid", "C7 auxiliary sources are invalid")
    layouts = {
        "runs": (RUN_LEDGER, frozenset(RUN_KINDS), None),
        "worker_receipts": (WORKER_LEDGER, frozenset(WORKER_KINDS), "worker_receipts"),
        "registry": (REGISTRY_LEDGER, frozenset({"registry_entry"}), "registry"),
        "decisions": (f"repositories/{repository}/decisions.jsonl", frozenset(DECISION_KINDS), "decisions"),
        "work_items": (WORK_ITEM_LEDGER, frozenset(WORK_KINDS), "work_items"),
    }
    captures: Dict[str, Dict[str, object]] = {}
    for name, (relative, kinds, auxiliary_name) in layouts.items():
        raw_relative = RAW_PREFIX + relative
        raw = _read_bundle_bytes(
            root,
            raw_relative,
            "c7_raw_source_unreadable" if name == "runs" else "c7_auxiliary_source_unreadable",
        )
        if name == "runs":
            expected_digest = projection.get("raw_source_digest")
            exists = True
        else:
            source = auxiliary.get(auxiliary_name)
            if not isinstance(source, Mapping) or source.get("ledger") != raw_relative:
                raise ProtocolRefusal("c7_auxiliary_source_invalid", "C7 auxiliary source ledger is invalid")
            expected_digest = source.get("raw_source_digest")
            exists = source.get("state") != "absent"
        if hashlib.sha256(raw).hexdigest() != expected_digest:
            code = "c7_raw_source_digest_invalid" if name == "runs" else "c7_auxiliary_source_digest_invalid"
            raise ProtocolRefusal(code, "C7 captured source digest does not match")
        captures[name] = _capture_bytes(
            name=name,
            relative=relative,
            raw=raw,
            exists=exists,
            tenant_id=tenant_id,
            allowed_kinds=kinds,
        )
    _bound_decision_repository(captures["decisions"], repository)
    return captures


def read_c7_1_bundle(destination: Path | str) -> Dict[str, Dict[str, object]]:
    """Read, verify, and deterministically reproject one C7.1 snapshot."""

    root = _destination_path(destination)
    if not root.is_absolute():
        raise ProtocolRefusal("c7_destination_not_absolute", "C7 bundle path must be absolute")
    if _has_unsafe_symlink_component(root) or root.is_symlink() or not root.is_dir():
        raise ProtocolRefusal("c7_destination_symlink", "C7 bundle path traverses a symlink")
    index = validate_c7_1_index(_load_bundle_json(root, "bundle-index.json", "c7_index_unreadable"))
    _verify_static_inventory(root)
    catalog = validate_c7_1_catalog(
        _load_bundle_json(root, str(index["schema_catalog"]), "c7_catalog_unreadable")
    )
    _verify_catalog_schemas(root, catalog)
    projection = validate_c7_1_projection(
        _load_bundle_json(root, "families/run-projection.json", "c7_projection_unreadable")
    )
    for ledger in _pointer_ledgers(projection):
        _read_bundle_bytes(root, ledger, "c7_raw_fallback_unreadable")
    captures = _snapshot_captures(root, projection)
    expected = _project_from_captures(
        captures,
        tenant_id=str(projection["tenant_id"]),
        repository=str(projection["repository"]),
    )
    if canonical_json_bytes(expected) != canonical_json_bytes(projection):
        raise ProtocolRefusal(
            "c7_projection_reprojection_invalid",
            "C7 projection does not match its captured physical sources",
        )
    return {"index": index, "projection": projection}

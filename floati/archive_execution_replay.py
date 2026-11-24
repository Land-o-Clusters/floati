"""Pure native execution/worker replay DATA, never checkpoint admission.

Inputs are chronological decoded native event batches and worker transcripts.
Public validators require the selected tenant explicitly, including for empty
collections. No call opens a root, reads a path, probes a process, authenticates
a launch, issues a capability or authorizes publication/retirement.
"""
from copy import deepcopy
import json

from . import seat_teardown_activity as _activity
from .epoch_carry import unique_epoch_rows
from .errors import IntegrityFailure
from .events import EVENT_KINDS, EventLog, validate_event_records
from .records import WORKER_OUTCOME_CODES, validate_record
from .root import validate_identifier
from .worker_bootstrap_protocol import validate_isolation_backend
from .worker_errors import WorkerAdapterFailure
from .workers import WorkerReceipts, WORKER_KINDS


def _checked(tenant_id, rows, kinds):
    validate_identifier(tenant_id, "tenant")
    return [validate_record(deepcopy(row), tenant_id, kinds, integrity=True) for row in rows]


def execution_history(tenant_id, event_batches):
    """Validate physical batches before chronological exact-ID coalescing."""
    validate_identifier(tenant_id, "tenant")
    batches = [_checked(tenant_id, rows, EVENT_KINDS) for rows in event_batches]
    return _execution_history(batches)


def _execution_history(batches):
    # Native readers already performed record/schema/tenant decoding. Keep
    # their exact original semantic ordering and refusal codes here.
    for rows in batches:
        validate_event_records(rows)
    history = unique_epoch_rows(batches)
    _validate_execution_continuity(history)
    return history


def validate_execution_history(tenant_id, history):
    """Validate schemas and continuity in already-coalesced logical history.

    Physical first-frame/event validation belongs to execution_history's
    ordered batches; a concatenated lineage contains several roll frames.
    """
    _validate_execution_continuity(_checked(tenant_id, history, EVENT_KINDS))


def _validate_execution_continuity(history):
    # Exact native _validate_history body: historical source bytes are not
    # reauthenticated as new invocation/launch authority.
    from .seat_execution import _HOST_SOURCES
    starts, closed, keys = {}, set(), set()
    identity_fields = ('node_id', 'acting_session_id', 'work_item_id',
        'work_claim_id', 'invocation_id', 'source_kind', 'source_receipt_id',
        'source_pin', 'source_binding', 'workspace', 'command')
    for row in history:
        if row.get('kind') != 'seat_execution':
            continue
        invocation = row['invocation_id']
        if row['idempotency_key'] in keys:
            raise IntegrityFailure('seat_execution_history_invalid', 'execution acceptance key is duplicated')
        keys.add(row['idempotency_key'])
        if row['event'] == 'running':
            if invocation in starts or invocation in closed:
                raise IntegrityFailure('seat_execution_history_invalid', 'native invocation has more than one start')
            starts[invocation] = row
        elif row['source_kind'] in _HOST_SOURCES:
            # Authenticated host observations stand alone. A prior synthetic
            # running command would claim execution the callback did not see.
            if invocation in starts or invocation in closed:
                raise IntegrityFailure('seat_execution_history_invalid', 'host observation must have one standalone acceptance')
            closed.add(invocation)
        else:
            start = starts.get(invocation)
            if start is None or invocation in closed or any(
                start[field] != row[field] for field in identity_fields
            ):
                raise IntegrityFailure('seat_execution_history_invalid', 'native close needs one unchanged, unclosed start')
            closed.add(invocation)


def execution_result(tenant_id, row, history):
    """Reproduce native notice and legacy readiness fallback, without I/O.

    This narrow replay selector preserves _result's permissive historical
    fallback; use execution_history separately for strict batch validation.
    """
    checked = _checked(tenant_id, [row], {"seat_execution"})[0]
    return _execution_result(checked, _checked(tenant_id, history, EVENT_KINDS))


def _execution_result(row, history):
    if row["envelope_id"] is None:
        return {"execution": row, "notice": None}
    envelope = next((r for r in history if r.get("kind") == "message_envelope"
                     and r["id"] == row["envelope_id"]), None)
    if envelope is None:
        raise IntegrityFailure("seat_execution_notice_missing", "accepted execution notice is absent")
    return {"execution": row, "notice": EventLog._send_receipt(
        envelope, EventLog._saved_readiness(history, envelope))}


def project_worker_records(tenant_id, records):
    """Replay exact native worker chronology, with caller-owned result data."""
    return _project_worker_records(_checked(tenant_id, records, WORKER_KINDS),
                                  WorkerReceipts._NEXT, WorkerReceipts._STATES)


def _project_worker_records(records, transitions, states):
    # Exact native WorkerReceipts._project body; callers retain their original
    # class/instance transition constants instead of introducing new defaults.
    projected, work_sessions = {}, {}
    for record in records:
        _advance_worker_record(record, projected, work_sessions, transitions, states)
    return list(projected.values())


def _advance_worker_record(record, projected, work_sessions, transitions, states):
    """Apply the same chronology rules to a full replay or one new tail row."""
    session_id = str(record["session_id"])
    row = projected.get(session_id)
    if row is None:
        if record["transition"] != "claim":
            raise IntegrityFailure(
                "worker_transition_invalid", "durable worker session does not begin with claim")
        work_item_id = str(record["work_item_id"])
        if work_item_id in work_sessions:
            raise IntegrityFailure(
                "worker_claim_already_bound", "durable work claim has multiple worker sessions")
        work_sessions[work_item_id] = session_id
        row = {
            "session_id": session_id,
            "work_item_id": record["work_item_id"],
            "node_id": record["node_id"],
            "adapter": record["adapter"],
            "authority_subject": record["authority_subject"],
            "authority_epoch": record["authority_epoch"],
            "artifact_bindings": [],
        }
        projected[session_id] = row
    else:
        if (
            row["work_item_id"] != record["work_item_id"]
            or row["node_id"] != record["node_id"]
            or row["adapter"] != record["adapter"]
            or row["authority_subject"] != record["authority_subject"]
            or row["authority_epoch"] != record["authority_epoch"]
        ):
            raise IntegrityFailure(
                "worker_session_mismatch", "durable worker session identity changed")
        if record["transition"] not in transitions.get(str(row["transition"]), set()):
            raise IntegrityFailure(
                "worker_transition_invalid", "durable worker transition is out of order")
    row["transition"] = record["transition"]
    row["state"] = states[str(record["transition"])]
    row["outcome_code"] = record["outcome_code"]
    row["last_activity"] = record["timestamp"]
    for binding in record["artifact_bindings"]:
        if binding not in row["artifact_bindings"]:
            row["artifact_bindings"].append(binding)


def worker_pid_receipts(tenant_id, history, workers, *, node):
    """Retain native exact spawn joins; output is DATA, never PID authority."""
    checked_history = _checked(tenant_id, history, EVENT_KINDS)
    _validate_execution_continuity(checked_history)
    checked_workers = _checked(tenant_id, workers, WORKER_KINDS)
    _project_worker_records(checked_workers, WorkerReceipts._NEXT, WorkerReceipts._STATES)
    return _worker_pid_join(checked_history, checked_workers, node)


def _worker_pid_join(history, workers, node):
    sources = {}
    for source in workers:
        if source["id"] in sources:
            raise _activity._unknown("Duplicate native worker source receipt identity")
        sources[source["id"]] = source
    result = {}
    for row in history:
        if (row.get("kind") != "seat_execution" or row["node_id"] != node or
                row["source_kind"] != "worker" or row["event"] not in {"ended", "unknown"}):
            continue
        source = sources.get(row["source_receipt_id"])
        # An absent source cannot supply ownership; retained contradiction
        # refuses. Historical sources are not reauthenticated as new launches.
        if source is None:
            continue
        if (source["transition"] != "spawn" or row["invocation_id"] != source["id"] or
                source["node_id"] != node or source["session_id"] != row["acting_session_id"] or
                source["work_item_id"] != row["work_item_id"] or
                row["command"] != "floati worker run " + source["adapter"]):
            raise _activity._unknown("Native worker audit differs from retained spawn receipt " + source["id"])
        pid = worker_audit_pid(row)
        if pid is not None:
            result.setdefault(pid, []).append(row["id"])
    return {pid: sorted(set(ids)) for pid, ids in result.items()}


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate audit field")
        result[key] = value
    return result


def worker_audit_pid(row):
    """Parse only native cleanup audit data; unsupported/null means no PID."""
    try:
        audit = json.loads(row["detail"], object_pairs_hook=_unique_object,
                           parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
        if type(audit) is not dict or set(audit) != {
                "adapter_pid", "registered_process_groups", "alive_after_cleanup", "isolation_backend"}:
            return None
        pid = audit["adapter_pid"]
        if pid is not None and (type(pid) is not int or not 1 < pid <= 2147483647):
            return None
        for key in ("registered_process_groups", "alive_after_cleanup"):
            values = audit[key]
            if (type(values) is not list or len(values) > _activity._MAX_PROCESSES or
                    any(type(value) is not int or not 1 < value <= 2147483647 for value in values) or
                    len(set(values)) != len(values)):
                return None
        groups = audit["registered_process_groups"]
        alive = audit["alive_after_cleanup"]
        if groups != sorted(groups) or not set(alive) <= set(groups) | ({pid} if pid is not None else set()):
            return None
        backend = audit["isolation_backend"]
        if backend is not None:
            validate_isolation_backend(backend)
        if row["event"] == "unknown":
            if row["code"] != "worker_cleanup_unconfirmed" or not alive:
                return None
        elif (row["event"] != "ended" or alive or
              row["code"] not in WORKER_OUTCOME_CODES | {"worker_completed_cleanup"}):
            return None
        return pid
    except (ValueError, TypeError, KeyError, WorkerAdapterFailure):
        return None

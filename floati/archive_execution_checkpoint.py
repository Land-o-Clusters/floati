"""Read-only execution/PID survivor planning over verified legacy history.

This module neither issues proof nor publishes state. Returned data is mutable
caller-owned planning material, not a callback capability or PID authorization.
Only the caller's already-owned exclusive EPOCH can enter the planner.
"""
from __future__ import annotations

from copy import deepcopy

from .bus_epoch import _require_owned_epoch_guard
from .epoch_carry import carry_dependency_witness
from .errors import ProtocolRefusal
from .jsonl import read_records_snapshot
from .seat_execution import SeatExecutionController, _history, _validate_history
from .seat_teardown_activity import _worker_audit_pid, _worker_pid_receipts
from .snapshot import epoch_history
from .workers import WorkerReceipts, WORKER_KINDS


def _blocked(detail):
    return ProtocolRefusal("archive_execution_checkpoint_blocked", detail,
                           remedy="preserve archive payloads until the complete native execution closure can be verified")


def plan_execution_checkpoint(root):
    """Plan the complete supported legacy execution domain without mutation.

    Native validation errors retain their codes. Unavailable/changed dependency
    witnesses and unsupported chain depth block; no partial plan is returned.
    The whole-tree identity witness is an advisory recheck, not issuance proof.
    """
    _require_owned_epoch_guard(root, exclusive=True)
    before = carry_dependency_witness(root)
    if before is None:
        raise _blocked("execution dependency membership is unavailable or exceeds the supported witness bound")
    # The old Path wrapper can return a bounded prefix. Retirement planning may
    # never mistake that for complete lineage, so admit the strict API first.
    archives = epoch_history(root)
    history = _history(root, snapshot=True)
    _validate_history(history)
    executions = [row for row in history if row["kind"] == "seat_execution"]
    controller = SeatExecutionController(root)
    replays = [{"execution_id": row["id"], "result": controller._result(row, history)}
               for row in executions]
    envelope_ids = {row["envelope_id"] for row in executions if row["envelope_id"] is not None}
    notices = [row for row in history if
               (row["kind"] == "message_envelope" and row["id"] in envelope_ids) or
               (row["kind"] == "send_readiness_observation" and row["envelope_id"] in envelope_ids)]

    # Keep the unselected worker transcript, including failed/absent source
    # evidence. Historical bytes are not reauthenticated as new launch authority.
    workers = []
    for archive in reversed(archives):
        legacy = archive.legacy_path
        relative = legacy.relative_to(root.tenant_home) / "receipts/workers.jsonl"
        if legacy.is_file() and not legacy.is_symlink():
            from .epoch_history import read_archived_bytes
            from .jsonl import _decode_path_records
            raw = read_archived_bytes(legacy, "receipts/workers.jsonl")
            if raw:
                workers.extend(_decode_path_records(
                    legacy / "receipts/workers.jsonl", root.tenant_id,
                    frozenset(WORKER_KINDS), raw))
        else:
            workers.extend(read_records_snapshot(root, relative, allowed_kinds=WORKER_KINDS))
    workers.extend(WorkerReceipts(root).records())
    WorkerReceipts(root)._project(workers)
    nodes = sorted({row["node_id"] for row in executions})
    pid_receipts = []
    for node in nodes:
        # Reuse the native exact spawn/execution join, audit parser and absent
        # source semantics. This helper performs no process inspection.
        for pid, ids in sorted(_worker_pid_receipts(root, node, snapshot=True).items()):
            pid_receipts.append({"node_id": node, "pid": pid, "execution_ids": ids})
    by_id = {row["id"]: row for row in workers}
    joins = [{"execution_id": row["id"], "source_receipt_id": row["source_receipt_id"],
              "source": by_id.get(row["source_receipt_id"]),
              "audit_pid": (_worker_audit_pid(row)
                            if row["source_receipt_id"] in by_id else None)}
             for row in executions
             if row["source_kind"] == "worker" and row["event"] in {"ended", "unknown"}]

    # Access every captured archive again, even if no execution came from it.
    # This also rechecks complete directory membership, including empty dirs.
    for archive in archives:
        archive.facts
    _require_owned_epoch_guard(root, exclusive=True)
    after = carry_dependency_witness(root)
    if after is None or after != before:
        raise _blocked("execution source or dependency membership/identity changed while planning")
    result = {"state": "planned", "domain": "execution", "tenant_id": root.tenant_id,
              "executions": executions,
              "acceptance_keys": [{"key": row["idempotency_key"], "execution_id": row["id"]}
                                  for row in executions],
              "replays": replays, "notice_rows": notices,
              "worker_records": workers, "worker_joins": joins,
              "pid_receipts": pid_receipts,
              "source_witness": [{"path": name, "identity": list(identity)}
                                 for name, identity in before]}
    return deepcopy(result)

"""Validated event history and its receipt-owned epoch members."""

from pathlib import Path

from .epoch_history import iter_roll_lineage
from .events import EVENT_KINDS, validate_event_records
from .jsonl import (
    MAX_LEDGER_BYTES,
    MAX_LEDGER_RECORDS,
    _decode_path_records,
    read_records_sealed_snapshot,
)


def read_epoch_member(root, archive, relative, kinds):
    if archive is None:
        return read_records_sealed_snapshot(root, relative, allowed_kinds=kinds)
    raw = archive.read_member(Path(relative).as_posix())
    if raw is None:
        return []
    return _decode_path_records(root.tenant_home / relative, root.tenant_id, kinds, raw)


def event_epochs(root):
    """Return oldest-to-newest events and any receipt-backed retention gap."""
    from .archive_retention import REMOVAL_KIND, REMOVAL_LEDGER

    relative = "events.jsonl"
    live = read_epoch_member(root, None, relative, EVENT_KINDS)
    archives = []
    gap = None
    if live and live[0]["kind"] == "bus_epoch_roll_receipt":
        def removed(receipt):
            nonlocal gap
            removals = read_epoch_member(root, None, REMOVAL_LEDGER, {REMOVAL_KIND})
            for row in removals:
                if all(row[field] == receipt[field]
                       for field in ("epoch_id", "archive_path", "archive_sha256")):
                    gap = {"reason": "epoch_archive_removed", "epoch_id": receipt["epoch_id"],
                           "removal_receipt_id": row["id"]}
                    return True
            return False

        for _receipt, archive in iter_roll_lineage(
                root, max_records=MAX_LEDGER_RECORDS, max_bytes=MAX_LEDGER_BYTES,
                report_absent=removed):
            if archive is not None:
                archives.append(archive)

    def snapshots():
        for archive in reversed(archives):
            events = read_epoch_member(root, archive, relative, EVENT_KINDS)
            validate_event_records(events)
            yield archive, events
        validate_event_records(live)
        yield None, live

    return snapshots(), gap


def project_asked(request, *, work_intent=None, dispatch_revision=None):
    """Project the request's author evidence from the admitted receipt source."""
    if request["kind"] == "dispatch_revision":
        return {
            "status": "recorded", "receipt_id": request["id"], "timestamp": request["timestamp"],
            "node": request["actor"], "dispatch_id": request["dispatch_id"], "revision": request["revision"],
        }
    evidence = {"status": "recorded", "receipt_id": request["id"], "timestamp": request["timestamp"]}
    if request["kind"] == "ask_question":
        evidence["node"] = request["sender"]
    elif work_intent is None:
        evidence["actor"] = {"status": "missing", "reason": "work_dispatcher_receipt_missing"}
    else:
        evidence["actor"] = {
            "status": "recorded",
            "receipt_id": work_intent["id"],
            "timestamp": work_intent["timestamp"],
            "node": work_intent["dispatcher"],
        }
    if dispatch_revision is not None:
        evidence["dispatch"] = project_asked(dispatch_revision)
    return evidence

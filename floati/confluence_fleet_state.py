"""Read-only, root-local archive and waiter testimony for Confluence v2."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import stat

from .archive_retention import REMOVAL_KIND, REMOVAL_LEDGER
from .archive_retention_policy import read_archive_policy, retention_decision
from .bus_epoch import _epoch_receipt_archive_path, epoch_guard
from .epoch_history import iter_roll_lineage
from .errors import IntegrityFailure
from .jsonl import MAX_LEDGER_BYTES, MAX_LEDGER_RECORDS, read_records_snapshot
from .registry import Registry
from .root import FloatiRoot


def _unknown(reason):
    return {"stamp": "UNKNOWN", "value": None, "observed_at": None,
            "age_seconds": None, "reason": reason}


def _stamp(value):
    return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _archive_bytes(path):
    """Physical retained bytes; never follow an archive or member alias."""
    info = path.lstat()
    if stat.S_ISREG(info.st_mode):
        return info.st_size
    if not stat.S_ISDIR(info.st_mode):
        raise IntegrityFailure("epoch_archive_retention_invalid",
                               "archive payload is not an ordinary file or directory")
    total = 0
    for member in path.iterdir():
        total += _archive_bytes(member)
    return total


def _archives(root, now):
    # Native compression/removal holds exclusive EPOCH. The directory lock
    # creates no root file and keeps policy, receipts, and sizes coherent.
    with epoch_guard(root):
        return _archives_guarded(root, now)


def _roll_receipts(root):
    # A missing owned payload is reported as unknown unless its removal is
    # receipted. Present payloads must pass native lineage admission.
    for receipt, _archive in iter_roll_lineage(
            root, max_records=MAX_LEDGER_RECORDS, max_bytes=MAX_LEDGER_BYTES,
            report_absent=lambda _receipt: True):
        yield receipt


def _archives_guarded(root, now):
    policy = read_archive_policy(root)
    removals = read_records_snapshot(root, REMOVAL_LEDGER, allowed_kinds={REMOVAL_KIND})
    removed = {row["epoch_id"]: row for row in removals}
    epochs = {}
    for row in _roll_receipts(root):
        decision = retention_decision(row, policy, now)
        epoch = row["epoch_id"]
        path = _epoch_receipt_archive_path(root, row)
        removal = removed.get(epoch)
        if removal is not None and any(removal[key] != row[key] for key in ("archive_path", "archive_sha256")):
            raise IntegrityFailure("epoch_archive_retention_invalid",
                                   "archive removal receipt differs from its roll receipt")
        try:
            size = _archive_bytes(path)
        except FileNotFoundError:
            # Only a durable removal receipt proves zero retained payload.
            size = 0 if removal is not None else None
        rolled_at = datetime.fromisoformat(row["timestamp"].replace("Z", "+00:00"))
        epochs[epoch] = {
            "epoch_id": epoch, "archive_path": row["archive_path"],
            "rolled_at": row["timestamp"], "size_bytes": size,
            "size_reason": "archive_payload_absent" if size is None else None,
            "expires_at": None if policy["keep_forever"] else _stamp(rolled_at + timedelta(days=30)),
            "expired": decision["expired"], "removed": removal is not None,
        }
    # A removal receipt can outlive the archive containing its roll receipt.
    for epoch, row in removed.items():
        if epoch not in epochs:
            path = root.resolve_relative(Path(row["archive_path"]).name)
            try:
                size = _archive_bytes(path)
            except FileNotFoundError:
                size = 0
            epochs[epoch] = {
                "epoch_id": epoch, "archive_path": row["archive_path"],
                "rolled_at": None, "size_bytes": size, "size_reason": None,
                "expires_at": None, "expired": True, "removed": True,
            }
    return {"keep_forever": policy["keep_forever"],
            "epochs": [epochs[key] for key in sorted(epochs)],
            "last_removal_receipt": max(removals, key=lambda row: (row["timestamp"], row["id"]), default=None)}


def _last_wake(root, node, now):
    rows = read_records_snapshot(root, Path("receipts/wakes") / (node + ".jsonl"),
                                 allowed_kinds={"wake_attempt_receipt"})
    successful = [row for row in rows if row["node_id"] == node and row["outcome"] == "woke"]
    row = max(successful, key=lambda row: (row["timestamp"], row["id"]), default=None)
    if row is None:
        return _unknown("wake_receipt_absent")
    observed = datetime.fromisoformat(row["timestamp"].replace("Z", "+00:00"))
    if observed > now:
        return _unknown("wake_receipt_in_future")
    return {"stamp": "MEASURED", "value": row["timestamp"],
            "observed_at": row["timestamp"], "age_seconds": int((now - observed).total_seconds()),
            "receipt_id": row["id"]}


def project_fleet_state(root: FloatiRoot, now: datetime):
    """No discovery, probes, writes, or CPU estimates; unknowns name causes."""
    from .waiter_state import project_waiter_mode

    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("fleet observation time must be aware")
    now = now.astimezone(timezone.utc)
    return {"observed_at": _stamp(now), "archive_retention": _archives(root, now),
            "waiters": [{"node_id": node, "mode": project_waiter_mode(root, node, now),
                         "last_wake": _last_wake(root, node, now),
                         "idle_cpu": _unknown("idle_cpu_not_measured")}
                        for node in Registry(root).active_node_ids()]}

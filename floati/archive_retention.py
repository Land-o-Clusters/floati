"""Compress rolled epochs and remove expired payloads under native maintenance.

Directory archives become ZIP_DEFLATED files at the same owned path. An
expired payload is unlinked only after its removal receipt is appended.
Keep-forever suppresses removal. A malformed ledger is skipped.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import timezone
import hashlib
import io
import os
from pathlib import Path
import shutil

from .archive_container import container_facts, write_container, _plane
from .archive_maintenance_authority import require_admission
from .archive_retention_policy import read_archive_policy, retention_decision
from .bus_epoch import _epoch_receipt_archive_path, _read_epoch_archive_payloads
from .errors import IntegrityFailure, ProtocolRefusal
from .framing import FrameError, decode_frames
from .ids import uuid7_hex
from .jsonl import append_record

REMOVAL_LEDGER = "receipts/epoch-archive-removals.jsonl"
REMOVAL_KIND = "epoch_archive_removal"


def _invalid(detail):
    return IntegrityFailure("epoch_archive_retention_invalid", detail)


def removed_epoch_ids(root):
    path = root.tenant_home / REMOVAL_LEDGER
    if not path.is_file():
        return set()
    found = set()
    try:
        records = decode_frames(path.read_bytes())
    except FrameError:
        return found
    for record in records:
        if isinstance(record, dict) and record.get("kind") == REMOVAL_KIND:
            found.add(record.get("epoch_id"))
    return found


def iter_roll_receipts(root):
    """Roll receipts from the live ledger and from retained epoch payloads."""
    found, seen = [], set()

    def add(raw):
        if not isinstance(raw, (bytes, bytearray)) or not raw:
            return
        try:
            records = decode_frames(raw)
        except FrameError:
            return
        for record in records:
            if not isinstance(record, dict) or record.get("kind") != "bus_epoch_roll_receipt":
                continue
            identity = record.get("id")
            if identity in seen:
                continue
            seen.add(identity)
            found.append(record)

    events = root.tenant_home / "events.jsonl"
    if events.is_file():
        from .send_prefix import current_epoch_has_no_roll_head
        if not current_epoch_has_no_roll_head(root):
            add(events.read_bytes())
    for path in sorted(root.tenant_home.glob("archive-*"), key=lambda item: item.name.encode("utf-8")):
        if path.is_symlink():
            continue
        if path.is_dir():
            member = path / "events.jsonl"
            if member.is_file():
                add(member.read_bytes())
        elif path.is_file():
            try:
                payloads, _snapshot = _read_epoch_archive_payloads(path)
            except (OSError, IntegrityFailure, ProtocolRefusal):
                continue
            for name, _plane, raw in payloads:
                if name == "events.jsonl":
                    add(raw)
    return found


def _owned_archive(root, receipt):
    try:
        return _epoch_receipt_archive_path(root, receipt)
    except IntegrityFailure as exc:
        raise _invalid(exc.detail) from exc


def _compress(archive: Path):
    payloads = {}
    for path in archive.rglob("*"):
        if path.is_symlink() or not path.is_file():
            continue
        payloads[path.relative_to(archive).as_posix()] = path.read_bytes()
    inventory = [{"path": name, "plane": _plane(name), "byte_length": len(payloads[name]),
                  "sha256": hashlib.sha256(payloads[name]).hexdigest()}
                 for name in sorted(payloads, key=lambda value: value.encode("utf-8"))]

    @contextmanager
    def open_member(name):
        yield io.BytesIO(payloads[name])

    staging = archive.parent / (".retain-" + archive.name)
    holding = archive.parent / (".retain-dir-" + archive.name)
    if staging.exists() or holding.exists():
        raise _invalid("archive compression staging path is already present")
    write_container(staging, inventory, open_member)
    archive.rename(holding)
    try:
        staging.rename(archive)
    except OSError:
        if not archive.exists():
            holding.rename(archive)
        raise
    shutil.rmtree(holding)


def _stamp(observed):
    return observed.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _removal_receipt(root, receipt, actor, trigger, observed, archive):
    if receipt["epoch_id"] in removed_epoch_ids(root):
        return
    facts = container_facts(archive)
    record = {"schema_version": 0, "kind": REMOVAL_KIND,
              "id": "epoch-archive-removal-" + uuid7_hex(), "tenant_id": root.tenant_id,
              "timestamp": _stamp(observed), "actor": actor, "trigger": trigger,
              "epoch_id": receipt["epoch_id"], "archive_path": receipt["archive_path"],
              "archive_sha256": receipt["archive_sha256"], "object_sha256": facts["sha256"]}
    append_record(root, REMOVAL_LEDGER, record, allowed_kinds={REMOVAL_KIND})


def _admitted(root, admission):
    try:
        require_admission(root, admission)
    except ProtocolRefusal:
        return False
    return True


def retain_rolled_epochs(root, admission, observed, *, actor, trigger):
    """Compress every directory archive, then drop payloads the decision expired."""
    if not _admitted(root, admission):
        return {"compressed": 0, "expired": 0}
    policy = read_archive_policy(root)
    receipts = iter_roll_receipts(root)
    compressed = []
    for receipt in receipts:
        archive = _owned_archive(root, receipt)
        if archive.is_dir():
            _compress(archive)
            compressed.append(receipt["epoch_id"])
    expiring = []
    for receipt in receipts:
        decision = retention_decision(receipt, policy, observed)
        archive = _owned_archive(root, receipt)
        if decision["expired"] and archive.is_file() and receipt["epoch_id"] not in removed_epoch_ids(root):
            expiring.append((receipt, archive))
    # Check every candidate while all original sources are still present. An
    # earlier unlink must not erase an origin needed by a later admission.
    from .archive_checkpoint import require_wd3_retirement_admission
    for receipt, archive in expiring:
        if archive.is_file():
            require_wd3_retirement_admission(root, receipt)
    for receipt, archive in expiring:
        if archive.is_file():
            _removal_receipt(root, receipt, actor, trigger, observed, archive)
            os.unlink(archive)
    return {"compressed": len(compressed), "expired": len(expiring)}

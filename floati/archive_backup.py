"""Export one named epoch's exact member bytes. Removal testimony is absence."""
from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path

from .archive_retention import iter_roll_receipts, removed_epoch_ids
from .bus_epoch import _epoch_receipt_archive_path, _read_epoch_archive_payloads
from .errors import IntegrityFailure, ProtocolRefusal

NO_RESULT = 32


def _refuse(detail):
    return ProtocolRefusal("archive_backup_refused", detail,
                           remedy="choose an absent directory outside the fleet root")


def _no_result(epoch_id, detail):
    return "no_result", {"code": "epoch_not_retained", "detail": detail, "epoch_id": epoch_id}, NO_RESULT


def export_epoch(root, epoch_id, destination):
    """Copy selected member bytes of one retained epoch into an absent directory."""
    if not isinstance(destination, Path):
        destination = Path(destination)
    if not destination.is_absolute():
        raise _refuse("backup directory must be absolute")
    if destination.exists() or destination.is_symlink():
        raise _refuse("backup directory must be absent")
    try:
        destination.relative_to(root.tenant_home)
        inside = True
    except ValueError:
        inside = False
    if inside or root.tenant_home == destination:
        raise _refuse("backup directory must not overlap the fleet root")
    if epoch_id in removed_epoch_ids(root):
        return _no_result(epoch_id, "removal testimony records this epoch as expired")
    receipt = next((row for row in iter_roll_receipts(root) if row.get("epoch_id") == epoch_id), None)
    if receipt is None:
        return _no_result(epoch_id, "no retained epoch matches this identifier")
    archive = _epoch_receipt_archive_path(root, receipt)
    if not archive.exists():
        raise IntegrityFailure("epoch_archive_missing",
                               "retained epoch bytes are absent without removal testimony")
    payloads, _snapshot = _read_epoch_archive_payloads(archive)
    representation = "compressed" if archive.is_file() else "legacy"
    staging = destination.with_name(destination.name + ".partial")
    if staging.exists():
        raise _refuse("backup staging directory must be absent")
    staging.mkdir()
    try:
        digest = hashlib.sha256()
        for relative, _plane, raw in payloads:
            target = staging / relative
            if target.is_symlink() or ".." in Path(relative).parts:
                raise _refuse("archive member path is not a contained relative name")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(raw)
            digest.update(relative.encode("utf-8") + b"\0" + raw)
        os.rename(staging, destination)
    except Exception:
        if staging.exists() and not destination.exists():
            import shutil
            shutil.rmtree(staging, ignore_errors=True)
        raise
    return "ok", {"epoch_id": epoch_id, "representation": representation,
                  "member_count": len(payloads), "output_sha256": digest.hexdigest(),
                  "archive_sha256": receipt.get("archive_sha256")}, 0


def register_cli(commands):
    backup = commands.add_parser("backup", floati_mcp_exposure="never")
    backup.add_argument("directory", metavar="DIR")
    backup.add_argument("--root", required=True, metavar="ROOT")
    backup.add_argument("--epoch", required=True, dest="epoch_id", metavar="EPOCH_ID")
    backup.set_defaults(handler=_handle)


def _handle(args):
    from .cli import _root
    return export_epoch(_root(args.root), args.epoch_id, Path(args.directory).expanduser())

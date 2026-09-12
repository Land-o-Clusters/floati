"""FQ-9 / P5: the wake daemon's own receipts roll like LEDGER-1 (b).

Two ``wake daemon serve`` processes stood down on 2026-09-10 were each
carrying a 23.8 MB ``receipts/wake-daemon/<node>.jsonl`` that doubled in
four days, and every cycle re-read the whole file to answer whether
consent was active. The layout this module owns:

  receipts/wake-daemon/<node>.jsonl            lifecycle receipts (rolled)
  receipts/wake-daemon/<node>.consent.jsonl    consent receipts (the O(1) read)
  receipts/wake-daemon/<node>.roll.json        one marker: prepared → committed
  receipts/wake-daemon/archive-<date>/         archive-whole destination

The roll is LEDGER-1 (b)'s shape at ledger scale: the operator's
``state/ledger-policy.json`` (or the shipped defaults) states when a roll
is due; the current file moves whole, byte-identical, into the dated
archive; a fresh file begins with the next append; and the consent rows
are carried into their own plane first, so ``require_active`` reads the
consent record and never a lifecycle row. The last full read of a node's
receipts is the migration that splits it.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from .bus_epoch import ledger_roll_policy
from .errors import DurabilityFailure, IntegrityFailure, ProtocolRefusal
from .jsonl import (
    _epoch_writer_guard,
    _lock_beside,
    _locked_path,
    read_records_snapshot,
    transact_records,
)
from .root import FloatiRoot

RECEIPTS_DIR_RELATIVE = Path("receipts/wake-daemon")
ROLL_REASONS = ("threshold_bytes", "threshold_age", "layout_migration")


def lifecycle_relative(node_id: str) -> Path:
    return RECEIPTS_DIR_RELATIVE / f"{node_id}.jsonl"


def consent_relative(node_id: str) -> Path:
    return RECEIPTS_DIR_RELATIVE / f"{node_id}.consent.jsonl"


def marker_relative(node_id: str) -> Path:
    return RECEIPTS_DIR_RELATIVE / f"{node_id}.roll.json"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _raw(root: FloatiRoot, relative: Path) -> Path:
    """The unresolved host path. resolve_relative dereferences the final
    symlink, so every guard must lstat this raw path first (LEDGER-1 (b)
    Am.1)."""

    return root.path / relative


def _regular_or_absent(raw: Path, relative: Path) -> Optional[os.stat_result]:
    try:
        status = raw.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise DurabilityFailure(
            "wake_daemon_receipts_unavailable",
            f"the wake daemon receipt file {relative.as_posix()} could not be inspected",
        ) from exc
    if stat.S_ISLNK(status.st_mode):
        raise ProtocolRefusal(
            "wake_daemon_receipts_symlink",
            f"the wake daemon receipt file {relative.as_posix()} must not be a symlink",
            remedy="remove the symlink; the next append recreates a regular receipt file",
        )
    if not stat.S_ISREG(status.st_mode):
        raise ProtocolRefusal(
            "wake_daemon_receipts_not_regular",
            f"the wake daemon receipt file {relative.as_posix()} must be an ordinary file",
            remedy="move the foreign file aside; the next append creates a regular receipt file",
        )
    if status.st_nlink != 1:
        raise ProtocolRefusal(
            "wake_daemon_receipts_not_regular",
            f"the wake daemon receipt file {relative.as_posix()} must not have hard-link aliases",
            remedy="remove the extra hard links so the receipt path is the file's only name",
        )
    return status


def _read_marker(raw: Path) -> Optional[Dict[str, Any]]:
    try:
        payload = raw.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise DurabilityFailure(
            "wake_daemon_roll_marker_unavailable", "the roll marker could not be read"
        ) from exc
    try:
        marker = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise IntegrityFailure(
            "wake_daemon_roll_marker_invalid", "the roll marker is unreadable"
        ) from exc
    if not isinstance(marker, dict) or marker.get("schema_version") != 0:
        raise IntegrityFailure(
            "wake_daemon_roll_marker_invalid", "the roll marker shape is invalid"
        )
    return marker


def _write_marker(root: FloatiRoot, relative: Path, marker: Dict[str, Any]) -> None:
    raw = _raw(root, relative)
    raw.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    encoded = (json.dumps(marker, sort_keys=True, indent=2) + "\n").encode("utf-8")
    temporary = raw.with_name(f".{raw.name}.{os.getpid()}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(descriptor, encoded)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.replace(temporary, raw)
    _fsync_directory(raw.parent)


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def _daemon_kinds() -> "frozenset[str]":
    # Imported lazily: the contract module imports this module for the
    # consent-plane paths, so the kind set crosses in the other direction
    # only inside calls.
    from .wake_daemon_contract import DAEMON_KINDS

    return DAEMON_KINDS


def _ensure_plane(root: FloatiRoot, relative: Path) -> None:
    """Leave the consent plane present, empty if it has nothing to hold.

    FQ-9 Am.2 (F2): the plane's EXISTENCE is what every reader in this
    codebase already uses to answer "has this root's layout split?" —
    ``ensure_rolled`` here, ``DaemonConsentLedger._rows``, and the
    terminal read in ``wake_daemon.py`` all test exactly this file. So a
    migration that carries nothing still has to leave the file, or the
    "not yet migrated" condition never clears and the root rolls again on
    every single append. An empty plane reads identically to the absent
    one it replaces: ``read_records_snapshot`` returns no rows either
    way, so ``require_active`` still refuses a node that never consented.
    """

    raw = _raw(root, relative)
    if raw.exists():
        return
    raw.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        descriptor = os.open(raw, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return
    except OSError as exc:
        raise DurabilityFailure(
            "wake_daemon_receipts_unavailable",
            f"the wake daemon receipt file {relative.as_posix()} could not be created",
        ) from exc
    os.close(descriptor)
    _fsync_directory(raw.parent)


def _carry_consent(root: FloatiRoot, node_id: str, consent_rows: List[Dict[str, Any]]) -> int:
    """Copy consent rows into the plane exactly once, by record id.

    The plane is present when this returns whether or not anything was
    carried (Am.2 F2, ``_ensure_plane``), and the copy is idempotent by
    record id — the property Am.2 F1 leans on, because a crash can land
    inside the carry and the heal has to be able to finish one.
    """

    relative = consent_relative(node_id)
    existing = {
        row["id"]
        for row in read_records_snapshot(
            root, relative, allowed_kinds=_daemon_kinds()
        )
    }
    missing = [row for row in consent_rows if row["id"] not in existing]
    if not missing:
        _ensure_plane(root, relative)
        return 0

    def decide(prior: List[Dict[str, Any]]):
        prior_ids = {row["id"] for row in prior}
        batch = [row for row in missing if row["id"] not in prior_ids]
        return None, batch

    transact_records(root, relative, decide, allowed_kinds=_daemon_kinds())
    return len(missing)


def _archive_destination(
    root: FloatiRoot, node_id: str, *, now: datetime
) -> "tuple[Path, Path, str, str]":
    """A dated archive directory and a collision-proof file name."""

    archive_dir_relative = RECEIPTS_DIR_RELATIVE / f"archive-{now.strftime('%Y-%m-%d')}"
    archive_dir = _raw(root, archive_dir_relative)
    archive_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    sequence = 0
    while True:
        suffix = "" if sequence == 0 else f"-{sequence}"
        name = f"{node_id}.{stamp}{suffix}.jsonl"
        destination = archive_dir / name
        if not destination.exists():
            return archive_dir, destination, archive_dir_relative, name
        sequence += 1


def _roll(
    root: FloatiRoot,
    node_id: str,
    *,
    policy: Dict[str, object],
    now: datetime,
) -> Dict[str, object]:
    """Archive the whole lifecycle file and carry consent to its plane.

    The reason is re-derived under the lock: another process may have
    rolled between the caller's unlocked check and this acquisition. Lock
    discipline matches ``transact`` on the lifecycle ledger exactly; the
    consent plane is only ever taken while the lifecycle lock is held, so
    the two-ledger order is total.
    """

    from .wake_daemon_contract import DAEMON_KINDS

    relative = lifecycle_relative(node_id)
    raw = _raw(root, relative)
    lock_path, lock_coordinate = _lock_beside(raw, relative)
    with _epoch_writer_guard(root, relative), _locked_path(
        lock_path, exclusive=True, relative=lock_coordinate
    ):
        healed = _heal_prepared_marker(root, node_id, now=now)
        if healed is not None:
            return healed
        status = _regular_or_absent(raw, relative)
        if status is None:
            return _committed_marker_or_none(root, node_id)
        if not _raw(root, consent_relative(node_id)).exists():
            reason = "layout_migration"
        elif status.st_size > int(policy["max_bytes"]):
            reason = "threshold_bytes"
        elif (
            now - datetime.fromtimestamp(status.st_mtime, tz=timezone.utc)
        ).total_seconds() / 86400.0 > int(policy["max_age_days"]):
            reason = "threshold_age"
        else:
            return _committed_marker_or_none(root, node_id)

        rows = read_records_snapshot(root, relative, allowed_kinds=DAEMON_KINDS)
        consent_rows = [
            row
            for row in rows
            if row.get("kind") == "wake_daemon_consent_receipt"
        ]
        lifecycle_rows = [
            row
            for row in rows
            if row.get("kind") == "wake_daemon_lifecycle_receipt"
        ]
        payload = raw.read_bytes()
        marker_path = marker_relative(node_id)
        archive_dir, destination, archive_dir_relative, archived_name = (
            _archive_destination(root, node_id, now=now)
        )
        prepared = {
            "schema_version": 0,
            "node_id": node_id,
            "state": "prepared",
            "reason": reason,
            "archive_dir": archive_dir_relative.as_posix(),
            "archived_name": archived_name,
            "policy": {
                "max_bytes": policy["max_bytes"],
                "max_age_days": policy["max_age_days"],
                "source": policy["source"],
            },
        }
        _write_marker(root, marker_path, prepared)

        carried = _carry_consent(root, node_id, consent_rows)
        os.replace(raw, destination)
        _fsync_directory(archive_dir)
        _fsync_directory(_raw(root, RECEIPTS_DIR_RELATIVE))

        receipt = dict(prepared)
        receipt.update({
            "state": "committed",
            "archived_to": (archive_dir_relative / archived_name).as_posix(),
            "archived_bytes": len(payload),
            "archived_sha256": hashlib.sha256(payload).hexdigest(),
            "archived_records": len(rows),
            "consent_rows_carried": carried,
            "lifecycle_rows_archived": len(lifecycle_rows),
            "rolled_at": now.isoformat(timespec="milliseconds").replace(
                "+00:00", "Z"
            ),
        })
        _write_marker(root, marker_path, receipt)
        return receipt


def _committed_marker_or_none(root: FloatiRoot, node_id: str) -> Optional[Dict[str, object]]:
    marker = _read_marker(_raw(root, marker_relative(node_id)))
    return marker if marker is not None and marker.get("state") == "committed" else None


def _heal_prepared_marker(
    root: FloatiRoot, node_id: str, *, now: datetime
) -> Optional[Dict[str, object]]:
    """Complete a roll a crash interrupted — the move AND the carry.

    prepared + destination present  → the move happened; carry and testify.
    prepared + legacy present       → the move did not happen; finish it now.

    FQ-9 Am.2 (F1). This function used to perform only the move, on the
    reasoning that ``_roll`` carries consent before it writes the marker.
    It does not: ``_roll``'s order is marker → carry → move, so a process
    killed in the first window came back with every row safe in the
    archive, the marker ``committed``, and no consent plane at all —
    ``require_active`` refusing ``wake_daemon_consent_absent`` for ever,
    for a node whose consent was never revoked, with nothing anywhere
    that rebuilds the plane from the archive. The heal is the third
    creator of the post-roll layout and it owes the same plane the other
    two owe.

    The carry runs AFTER the move and reads its rows from the
    destination, which is byte-identical to the pre-roll file at every
    crash point, so one code path covers all of them; and it runs BEFORE
    the committed marker, so a crash inside the heal leaves ``prepared``
    and the next heal finishes it. ``_carry_consent`` is idempotent by
    record id, which is what makes that safe to repeat — and is why the
    fix is here rather than a reordering of ``_roll``: carrying before
    the marker would leave a crash window with a carried plane and NO
    marker, which ``ensure_rolled`` reads as an already-split root and
    never migrates, stranding a mixed lifecycle file permanently.
    """

    marker = _read_marker(_raw(root, marker_relative(node_id)))
    if marker is None or marker.get("state") != "prepared":
        return None
    for field in ("archive_dir", "archived_name", "reason"):
        if not isinstance(marker.get(field), str) or not marker[field]:
            raise IntegrityFailure(
                "wake_daemon_roll_marker_invalid",
                f"the prepared roll marker is missing {field}",
            )
    destination = root.path / marker["archive_dir"] / marker["archived_name"]
    raw = _raw(root, lifecycle_relative(node_id))
    moved = destination.is_file()
    if not moved:
        status = _regular_or_absent(raw, lifecycle_relative(node_id))
        if status is None:
            raise IntegrityFailure(
                "wake_daemon_roll_marker_unresolvable",
                "the prepared roll marker names neither an archive nor a ledger",
            )
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.replace(raw, destination)
        _fsync_directory(destination.parent)
    payload = destination.read_bytes()
    archived_relative = Path(marker["archive_dir"]) / marker["archived_name"]
    archived_rows = read_records_snapshot(
        root, archived_relative, allowed_kinds=_daemon_kinds()
    )
    carried = _carry_consent(
        root,
        node_id,
        [
            row
            for row in archived_rows
            if row.get("kind") == "wake_daemon_consent_receipt"
        ],
    )
    receipt = dict(marker)
    receipt.update({
        "state": "committed",
        "archived_to": archived_relative.as_posix(),
        "archived_bytes": len(payload),
        "archived_sha256": hashlib.sha256(payload).hexdigest(),
        "archived_records": payload.count(b"\n"),
        "consent_rows_carried": carried,
        "rolled_at": now.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
    })
    _write_marker(root, marker_relative(node_id), receipt)
    return receipt


def ensure_rolled(
    root: FloatiRoot,
    node_id: str,
    *,
    policy: Optional[Dict[str, object]] = None,
    now: Optional[datetime] = None,
) -> Optional[Dict[str, object]]:
    """Roll the node's daemon receipts when the layout or the policy says so.

    Steady state is two lstat calls: a node whose consent plane exists and
    whose lifecycle file is inside both thresholds is returned untouched.
    """

    if not isinstance(root, FloatiRoot):
        raise ProtocolRefusal(
            "wake_daemon_root_invalid", "rolling receipts requires a validated root"
        )
    moment = now or _utc_now()
    healed = _heal_prepared_marker(root, node_id, now=moment)
    raw = _raw(root, lifecycle_relative(node_id))
    status = _regular_or_absent(raw, lifecycle_relative(node_id))
    if status is None:
        return healed
    policy = policy or ledger_roll_policy(root)
    if not _raw(root, consent_relative(node_id)).exists():
        # The layout migration subsumes any threshold on a mixed root:
        # it is the roll that empties the consent read of lifecycle rows.
        return _roll(root, node_id, policy=policy, now=moment)
    if status.st_size > int(policy["max_bytes"]):
        return _roll(root, node_id, policy=policy, now=moment)
    if (
        moment - datetime.fromtimestamp(status.st_mtime, tz=timezone.utc)
    ).total_seconds() / 86400.0 > int(policy["max_age_days"]):
        return _roll(root, node_id, policy=policy, now=moment)
    return healed

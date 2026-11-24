"""Exact waiter capability preparation and retirement for seat onboarding.

These helpers never install hooks, supervise processes, or erase mail. Native
consent testimony stays append-only; the removable workspace map and stale
Codex adapter are checked separately from the executable's current availability.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
from pathlib import Path

from .codex_hook_install import _rename_generation_noreplace
from .codex_wait_contract import (
    CodexWaitConsentLedger,
    DEFAULT_HOOK_TIMEOUT_SECONDS,
    DEFAULT_WAIT_DEADLINE_SECONDS,
    WORKSPACE_MAP_RELATIVE,
    _write_workspace_map_atomically,
    consent_ledger_relative,
    resolve_participant,
)
from .cursor import SparseCursor
from .errors import IntegrityFailure, ProtocolRefusal
from .events import EventLog
from .ids import uuid7_hex
from .jsonl import read_records_snapshot, transact
from .records import WAKE_HOLD_KINDS
from .registry import utc_now
from .root import FloatiRoot, validate_identifier
from .waiter_consent import arm_waiter_consent
from .wake_daemon_contract import _BINDING_FIELDS, _BINDABLE_RESUME_STATES
from .wake_control import validate_session_id


def _refuse(detail):
    raise ProtocolRefusal(
        "seat_waiter_lifecycle_invalid", detail,
        remedy="inspect the exact seat mapping or adapter; restore matching owned evidence before retrying",
    )


def _identity(root, node):
    if not isinstance(root, FloatiRoot) or root.path != root.tenant_home:
        _refuse("waiter lifecycle requires one validated direct-home root")
    try:
        return validate_identifier(node, "node")
    except ProtocolRefusal:
        _refuse("node identifier is invalid")


def _key(key):
    if not isinstance(key, str) or not 1 <= len(key) <= 128 or any(
        ord(c) < 32 or ord(c) == 127 for c in key
    ):
        _refuse("retirement key must be bounded terminal-safe text")
    return key


def _safe_path(value, *, directory=False):
    if not isinstance(value, (str, Path)):
        _refuse("capability path is not a path")
    path = Path(value)
    if not path.is_absolute() or str(path) != str(value) or any(
        ord(c) < 32 or ord(c) == 127 for c in str(value)
    ) or ".." in path.parts:
        _refuse("capability path must be an absolute normalized terminal-safe path")
    for part in (*reversed(path.parents), path):
        if part.is_symlink():
            _refuse("capability path contains a symlink")
    if path.resolve(strict=False) != path:
        _refuse("capability path is not canonical")
    if directory and not path.is_dir():
        _refuse("workspace must be an existing ordinary directory")
    return path


def _json(encoded):
    def unique(pairs):
        result = {}
        for name, value in pairs:
            if name in result:
                _refuse("capability JSON contains duplicate fields")
            result[name] = value
        return result
    try:
        if not isinstance(encoded, bytes) or len(encoded) > 1048576:
            _refuse("capability JSON is oversized")
        return json.loads(encoded.decode("utf-8"), object_pairs_hook=unique)
    except (UnicodeError, ValueError, TypeError):
        _refuse("capability JSON is unreadable")


def _map(root, encoded):
    raw = _json(encoded)
    if not isinstance(raw, dict) or set(raw) != {"schema_version", "tenant_id", "mappings"}:
        _refuse("workspace map has foreign fields")
    if type(raw["schema_version"]) is not int or raw["schema_version"] != 0 or (
        raw["tenant_id"] != root.tenant_id or not isinstance(raw["mappings"], list)
    ):
        _refuse("workspace map identity is invalid")
    prior = None
    for entry in raw["mappings"]:
        if not isinstance(entry, dict) or set(entry) != {"node_id", "workspace"}:
            _refuse("workspace mapping has foreign fields")
        node = _identity(root, entry["node_id"])
        workspace = _safe_path(entry["workspace"])
        pair = (str(workspace), node)
        if prior is not None and pair <= prior:
            _refuse("workspace mappings are duplicated or out of order")
        prior = pair
    return raw


def _current_map(root):
    path = _safe_path(root.path / WORKSPACE_MAP_RELATIVE)
    if not path.exists():
        return path, None, None
    if not path.is_file():
        _refuse("workspace map is not an ordinary file")
    encoded = path.read_bytes()
    return path, encoded, _map(root, encoded)


def prepare_waiter(root, node, workspace,
                   hook_timeout_seconds=DEFAULT_HOOK_TIMEOUT_SECONDS,
                   wait_deadline_seconds=DEFAULT_WAIT_DEADLINE_SECONDS):
    """Arm the public native waiter capability for one exact registered seat."""
    node = _identity(root, node)
    workspace = _safe_path(workspace, directory=True)
    _current_map(root)
    _consent_rows(root, node)
    result = arm_waiter_consent(root.path, workspace, node, "codex",
                               hook_timeout_seconds, wait_deadline_seconds)
    matching = [row for row in _consent_rows(root, node) if row["workspace"] == str(workspace)]
    if matching and matching[-1]["state"] == "disarmed":
        # Native arm keys describe the workspace/options, so a same-home
        # re-add can replay testimony older than the retirement. Bind a fresh
        # native arm to that exact disarm, keeping repeated preparation stable.
        participant = resolve_participant(root.path, workspace)
        if participant is None or participant.binding.node_id != node:
            _refuse("prepared waiter map no longer resolves to the exact node")
        receipt = CodexWaitConsentLedger(root).arm(
            participant.binding, hook_timeout_seconds=hook_timeout_seconds,
            wait_deadline_seconds=wait_deadline_seconds,
            idempotency_key="seat-rearm-" + hashlib.sha256(str(matching[-1]["id"]).encode()).hexdigest(),
        )
    else:
        receipt = matching[-1]
    result.update(consent_receipt_id=receipt["id"], state=receipt["state"],
                  idempotency_key=receipt["idempotency_key"])
    return result


def preflight_retire_waiter(root, node, workspace):
    """Read-only validation; an absent exact capability is a safe no-op."""
    _preflight_retire_waiter(root, node, workspace, require_directory=True)


def _preflight_retire_waiter(root, node, workspace, *, require_directory):
    node = _identity(root, node)
    workspace = _safe_path(workspace, directory=require_directory)
    _path, _encoded, raw = _current_map(root)
    if raw is not None:
        for entry in raw["mappings"]:
            if entry["workspace"] == str(workspace) and entry["node_id"] != node:
                _refuse("workspace map names a foreign node at the exact seat home")
    rows = _consent_rows(root, node)
    for row in rows:
        if row["node_id"] != node:
            _refuse("consent ledger contains a foreign node")


def _consent_rows(root, node):
    try:
        return read_records_snapshot(root, consent_ledger_relative(node),
                                     allowed_kinds={"codex_wait_consent_receipt"})
    except (ProtocolRefusal, IntegrityFailure):
        _refuse("native waiter consent cannot be validated; preserve its ledger for investigation")


def retire_waiter(root, node, workspace, key, previous_map=None):
    """Disarm native consent, then remove only this seat's exact map pair.

    ``previous_map=None`` means onboarding created the map. Original bytes
    are restored only when their pair inventory still matches the retained
    inventory, so another seat's later mapping is never overwritten.
    """
    return _retire_waiter(root, node, workspace, key, previous_map, require_directory=True)


def _retire_waiter(root, node, workspace, key, previous_map, *, require_directory):
    from .bus_epoch import epoch_guard
    node = _identity(root, node)
    with epoch_guard(root):
        return _retire_waiter_already_guarded(root, node, workspace, key, previous_map,
            require_directory=require_directory)


def _retire_waiter_already_guarded(root, node, workspace, key, previous_map, *, require_directory):
    node = _identity(root, node)
    key = _key(key)
    workspace = _safe_path(workspace, directory=require_directory)
    _preflight_retire_waiter(root, node, workspace, require_directory=require_directory)
    previous = None if previous_map is None else _map(root, previous_map)
    path, encoded, raw = _current_map(root)
    matching_pair = {"node_id": node, "workspace": str(workspace)}
    retained = [] if raw is None else [entry for entry in raw["mappings"] if entry != matching_pair]
    relative = consent_ledger_relative(node)
    rows = _consent_rows(root, node)
    matching = [row for row in rows if row["workspace"] == str(workspace)]
    receipt = None
    for row in rows:
        if row["idempotency_key"] == key:
            if row["workspace"] != str(workspace) or row["node_id"] != node or row["state"] != "disarmed":
                _refuse("retirement key names different consent coordinates")
            receipt = row
    if receipt is None and matching and matching[-1]["state"] == "armed":
        candidate = dict(matching[-1], id="codex-wait-consent-" + uuid7_hex(),
                         timestamp=utc_now(), state="disarmed", idempotency_key=key)
        def decide(prior):
            latest = [row for row in prior if row["workspace"] == str(workspace)]
            if not latest or latest[-1] != matching[-1]:
                _refuse("waiter consent changed during retirement")
            return candidate, candidate
        receipt = transact(root, relative, decide, allowed_kinds={"codex_wait_consent_receipt"})
    elif receipt is None and matching:
        receipt = matching[-1]
    if raw is not None and matching_pair in raw["mappings"]:
        if path.read_bytes() != encoded:
            _refuse("workspace map changed during retirement; disarm receipt remains recorded")
        if not retained and previous_map is None:
            path.unlink()
            map_state = "removed"
        else:
            replacement = previous_map if previous is not None and previous["mappings"] == retained else (
                json.dumps(dict(raw, mappings=retained), sort_keys=True, separators=(",", ":")).encode() + b"\n"
            )
            _write_workspace_map_atomically(path, replacement)
            map_state = "retained"
    else:
        map_state = "absent" if raw is None else "unchanged"
    return {"node_id": node, "workspace": str(workspace),
            "state": "disarmed" if receipt is not None else "absent",
            "consent_receipt_id": None if receipt is None else receipt["id"],
            "map_state": map_state, "remaining_mappings": retained}


def disarm_and_unmap(root, node, workspace, key):
    """Compatibility name for exact native capability retirement."""
    return retire_waiter(root, node, workspace, key)


def _adapter(root, node, encoded):
    raw = _json(encoded)
    if not isinstance(raw, dict) or set(raw) not in (
        _BINDING_FIELDS, _BINDING_FIELDS | {"resume_state"}
    ):
        _refuse("adapter binding has foreign fields")
    digest = hashlib.sha256((str(root.path) + "\0" + root.tenant_id + "\0" + node + "\0codex").encode()).hexdigest()
    if type(raw["schema_version"]) is not int or raw["schema_version"] != 0 or any(
        raw[field] != value for field, value in (("tenant_id", root.tenant_id),
        ("node_id", node), ("harness", "codex"), ("coordinate_digest", digest))
    ):
        _refuse("adapter binding belongs to a foreign seat or harness")
    try:
        session = validate_session_id(raw["session_id"])
    except ProtocolRefusal:
        _refuse("adapter binding session is invalid")
    if raw["session_digest"] != hashlib.sha256(session.encode()).hexdigest():
        _refuse("adapter session digest disagrees with its session")
    for field in ("coordinate_digest", "session_digest", "executable_digest", "adapter_digest"):
        if not isinstance(raw[field], str) or re.fullmatch(r"[0-9a-f]{64}", raw[field]) is None:
            _refuse("adapter binding digest is invalid")
    if not isinstance(raw["adapter_version"], str) or re.fullmatch(
        r"[A-Za-z0-9](?:[A-Za-z0-9._-]{0,62}[A-Za-z0-9])?", raw["adapter_version"]
    ) is None or type(raw["binding_epoch"]) is not int or not 1 <= raw["binding_epoch"] <= 2**63 - 1:
        _refuse("adapter version or epoch is invalid")
    if "resume_state" in raw and (not isinstance(raw["resume_state"], str)
            or raw["resume_state"] not in _BINDABLE_RESUME_STATES):
        _refuse("adapter resume state is invalid")
    _safe_path(raw["workspace"])
    from .wake_daemon_adapters import CODEX_EXECUTABLE

    # This is metadata-only retirement admission, never executable trust.
    # The fixed Codex declaration may be a package-manager symlink; an
    # existing target still prevents stale retirement below. All other
    # recorded paths retain the ordinary canonical-path fence.
    if raw["executable"] != str(CODEX_EXECUTABLE):
        _safe_path(raw["executable"])
    return raw


def _archive_bytes(path, *, sync=False):
    """Read exact private archive bytes through one ordinary owned inode."""
    path = _safe_path(path)
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        identity = os.fstat(descriptor)
        if (not stat.S_ISREG(identity.st_mode) or identity.st_uid != os.getuid()
                or stat.S_IMODE(identity.st_mode) != 0o600 or identity.st_size > 1048576):
            _refuse("stale adapter archive must be a bounded owned private ordinary file")
        if sync:
            os.fsync(descriptor)
        pieces = []
        remaining = 1048577
        while remaining:
            piece = os.read(descriptor, min(65536, remaining))
            if not piece:
                break
            pieces.append(piece)
            remaining -= len(piece)
        encoded = b"".join(pieces)
        current = os.lstat(path)
        if len(encoded) > 1048576 or (
            current.st_dev, current.st_ino, current.st_mode, current.st_uid
        ) != (identity.st_dev, identity.st_ino, identity.st_mode, identity.st_uid):
            _refuse("stale adapter archive changed during verification")
        return encoded
    finally:
        os.close(descriptor)


def _publish_archive(path, encoded):
    """Fsync and verify private staging, then publish without replacing a leaf."""
    descriptor, temporary_name = tempfile.mkstemp(prefix=".seat-archive-", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        try:
            os.fchmod(descriptor, 0o600)
            offset = 0
            while offset < len(encoded):
                written = os.write(descriptor, encoded[offset:])
                if written <= 0:
                    _refuse("stale adapter archive staging write made no progress")
                offset += written
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        if _archive_bytes(temporary) != encoded:
            _refuse("stale adapter archive staging disagrees with the exact binding bytes")
        try:
            _rename_generation_noreplace(temporary, path)
        except FileExistsError:
            _refuse("stale adapter archive appeared during publication; existing leaf preserved")
        if _archive_bytes(path) != encoded:
            _refuse("published stale adapter archive disagrees with the exact binding bytes")
    finally:
        temporary.unlink(missing_ok=True)


def retire_stale_adapter(root, node, key):
    """Archive only a missing-executable Codex binding with no runtime evidence."""
    node = _identity(root, node)
    key = _key(key)
    path = _safe_path(root.path / "state/wake-daemon/adapters" / node / "codex.json")
    key_digest = hashlib.sha256(key.encode()).hexdigest()
    archive = _safe_path(root.path / "state/seat-onboarding-history" / node / ("codex-" + key_digest + ".json"))
    if not path.exists():
        if archive.exists():
            _adapter(root, node, _archive_bytes(archive))
            return {"node_id": node, "state": "retired", "reason": "executable_missing", "archive_path": str(archive)}
        return {"node_id": node, "state": "absent", "reason": "binding_absent", "archive_path": None}
    if not path.is_file():
        _refuse("adapter binding is not an ordinary file")
    identity = os.lstat(path)
    encoded = path.read_bytes()
    raw = _adapter(root, node, encoded)
    executable = Path(raw["executable"])
    if executable.exists():
        if not executable.is_file() or not stat.S_ISREG(executable.stat().st_mode):
            _refuse("adapter executable is not an ordinary file")
        return {"node_id": node, "state": "unchanged", "reason": "executable_present", "archive_path": None}
    runtime = _safe_path(root.path / "state/wake-daemon/runtime" / (raw["coordinate_digest"] + ".json"))
    if runtime.exists():
        _refuse("daemon runtime evidence prevents proving the adapter stale")
    if archive.exists() and _archive_bytes(archive) != encoded:
        _refuse("stale adapter archive conflicts with this retirement key")
    history = archive.parent.parent
    history.mkdir(mode=0o700, exist_ok=True)
    archive.parent.mkdir(mode=0o700, exist_ok=True)
    if not archive.exists():
        _publish_archive(archive, encoded)
    # Recheck durability on retries too: a previous publication may have
    # succeeded before its directory fsync failed, leaving the binding intact.
    archived = _archive_bytes(archive, sync=True)
    directory = os.open(archive.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
    if _archive_bytes(archive) != archived:
        _refuse("stale adapter archive changed during its durability check")
    if archived != encoded:
        _refuse("stale adapter archive changed before binding retirement")
    _adapter(root, node, archived)
    if path.read_bytes() != encoded:
        _refuse("adapter binding changed after archival; retained archive names the prior binding")
    # Quarantine the name atomically, then verify the actual moved file. A
    # writer replacing the source between the last read and rename cannot
    # make us unlink its new binding; restore without overwriting another name.
    quarantine = archive.parent / (".retiring-" + uuid7_hex())
    os.replace(path, quarantine)
    moved = os.lstat(quarantine)
    if (moved.st_dev, moved.st_ino) != (identity.st_dev, identity.st_ino) or (
        quarantine.is_symlink() or quarantine.read_bytes() != encoded
    ):
        try:
            os.link(quarantine, path, follow_symlinks=False)
            quarantine.unlink()
        except OSError:
            _refuse("adapter changed during removal; replacement retained at " + str(quarantine))
        _refuse("adapter changed during removal; replacement binding restored")
    quarantine.unlink()
    return {"node_id": node, "state": "retired", "reason": "executable_missing", "archive_path": str(archive)}


def preflight_rehome(root, node):
    """Validate every old map/consent and require absent Codex runtime.

    Old workspace contents are never read. Their lexical capability paths
    may be missing; symlinked coordinates remain refusals.
    """
    node = _identity(root, node)
    _path, _encoded, raw = _current_map(root)
    rows = _consent_rows(root, node)
    if any(row["node_id"] != node for row in rows):
        _refuse("native waiter consent contains a foreign node")
    if raw is not None:
        for entry in raw["mappings"]:
            if entry["node_id"] == node:
                _preflight_retire_waiter(root, node, Path(entry["workspace"]), require_directory=False)
    digest = hashlib.sha256((str(root.path) + "\0" + root.tenant_id + "\0" + node + "\0codex").encode()).hexdigest()
    runtime = _safe_path(root.path / "state/wake-daemon/runtime" / (digest + ".json"))
    if runtime.exists():
        _refuse("existing Codex runtime must be explicitly stopped before rehome")
    binding = _safe_path(root.path / "state/wake-daemon/adapters" / node / "codex.json")
    if binding.exists():
        if not binding.is_file():
            _refuse("rehome adapter binding is not an ordinary file")
        adapter = _adapter(root, node, binding.read_bytes())
        if Path(adapter["executable"]).exists():
            _refuse("intact Codex adapter requires explicit session retirement before rehome")


def rehome_waiter(root, node, key):
    """Retire all prior workspace capabilities for this existing node only."""
    node = _identity(root, node)
    key = _key(key)
    preflight_rehome(root, node)
    _path, _encoded, raw = _current_map(root)
    old_workspaces = [] if raw is None else [entry["workspace"] for entry in raw["mappings"] if entry["node_id"] == node]
    # Validate all derived replay coordinates before the first archival or
    # disarm so a conflict at a later home cannot silently remove earlier ones.
    rows = _consent_rows(root, node)
    keys = {workspace: "rehome-" + hashlib.sha256((key + "\0" + workspace).encode()).hexdigest()
            for workspace in old_workspaces}
    for row in rows:
        for workspace, retirement_key in keys.items():
            if row["idempotency_key"] == retirement_key and (
                row["workspace"] != workspace or row["state"] != "disarmed"
            ):
                _refuse("rehome key names different native consent coordinates")
    adapter = retire_stale_adapter(root, node, "rehome-adapter-" + hashlib.sha256(key.encode()).hexdigest())
    if adapter["state"] == "unchanged":
        _refuse("Codex adapter became intact after rehome preflight; mappings retained")
    retired = [_retire_waiter(root, node, Path(workspace), keys[workspace], None,
                              require_directory=False) for workspace in old_workspaces]
    return {"node_id": node, "state": "rehomed", "old_workspaces": old_workspaces,
            "retired_waiters": retired, "adapter": adapter}


def prior_life_mail(root, node):
    """Return unacknowledged historical IDs without presenting or acknowledging."""
    node = _identity(root, node)
    frames, _unknown = EventLog(root).compatible_event_records(snapshot=True)
    events = [row for row in frames if row["kind"] == "message_envelope"]
    retracted = {str(row["retracted_message_id"]) for row in frames if row["kind"] == "message_retracted"}
    sessions = {row.get("worker_session_id") for row in events if row["recipient"] == node}
    acknowledged = set()
    by_id = {row["id"]: row for row in events}
    cursor = SparseCursor(root)
    for session in sessions:
        # SparseCursor's ordinary reads create coordination lock files. Use
        # its exact paths with snapshot reads for this observational inventory.
        delivered = set()
        for receipt in read_records_snapshot(root, cursor._delivery_relative_path_for(
            node, worker_session_id=session), allowed_kinds=set(WAKE_HOLD_KINDS)
        ):
            if receipt.get("recipient") != node:
                _refuse("historical delivery receipt belongs to another seat")
            for item_id in receipt["item_ids"]:
                event = by_id.get(item_id)
                if event is None or event["recipient"] != node or event.get("worker_session_id") != session:
                    _refuse("historical delivery names a missing or foreign message")
                delivered.add(item_id)
        for receipt in read_records_snapshot(root, cursor._relative_path_for(
            node, worker_session_id=session), allowed_kinds={"ack_receipt"}
        ):
            if receipt.get("recipient") != node:
                _refuse("historical acknowledgment belongs to another seat")
            for item_id in receipt["item_ids"]:
                event = by_id.get(item_id)
                if item_id not in delivered or event is None or event["recipient"] != node or event.get("worker_session_id") != session:
                    _refuse("historical acknowledgment has no matching delivery")
                acknowledged.add(item_id)
    return [str(row["id"]) for row in events if row["recipient"] == node
            and row["id"] not in acknowledged and row["id"] not in retracted]

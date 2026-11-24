"""Read-only recipient-bound readiness; configuration is not live testimony.

Callers hold the shared epoch and physical recipient guard. No ambient HOME,
configuration discovery, process probes, or capability writes are performed.
"""
from __future__ import annotations

import hashlib
import json
import os
import stat
from datetime import datetime, timezone
from pathlib import Path

from .errors import FloatiError, ProtocolRefusal


def _time(value):
    current = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("naive timestamp")
    return current.astimezone(timezone.utc)


def _path(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 4096 or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError("invalid coordinate")
    path = Path(value)
    if not path.is_absolute() or str(path) != value or ".." in path.parts:
        raise ValueError("noncanonical coordinate")
    return path


def _parent(path):
    descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:-1]:
            following = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = following
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _signature(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_uid,
            info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _read(path, limit=32 * 1024 * 1024, *, with_identity=False):
    """Bounded ordinary-file snapshot, including descriptor-bound parents."""
    path = _path(str(path))
    parent = _parent(path)
    descriptor = None
    reopened = None
    try:
        descriptor = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_size > limit:
            raise ValueError("not bounded ordinary file")
        chunks = []
        remaining = limit + 1
        while remaining:
            chunk = os.read(descriptor, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        if len(data) > limit or _signature(before) != _signature(os.fstat(descriptor)):
            raise ValueError("changed file")
        reopened = _parent(path)
        if (os.fstat(parent).st_dev, os.fstat(parent).st_ino) != (os.fstat(reopened).st_dev, os.fstat(reopened).st_ino):
            raise ValueError("changed parent")
        if _signature(before) != _signature(os.stat(path.name, dir_fd=reopened, follow_symlinks=False)):
            raise ValueError("changed path")
        if with_identity:
            parent_info = os.fstat(parent)
            return data, _signature(before), (parent_info.st_dev, parent_info.st_ino)
        return data
    finally:
        if reopened is not None:
            os.close(reopened)
        if descriptor is not None:
            os.close(descriptor)
        os.close(parent)


def _json(data):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate field")
            result[key] = value
        return result
    def constant(value):
        raise ValueError("invalid numeric constant")
    return json.loads(data.decode("utf-8"), object_pairs_hook=unique, parse_constant=constant)


def _rows(root, relative, kinds, *, snapshots=None):
    from .jsonl import _decode_path_records
    path = root.tenant_home / relative
    try:
        snapshot = _read(path, with_identity=True)
        data = snapshot[0]
    except FileNotFoundError:
        return []
    rows = _decode_path_records(path, root.tenant_id, frozenset(kinds), data)
    if snapshots is not None:
        snapshots.append((path, snapshot))
    return rows


def _source(path, identity=None):
    return {"path": str(path), "record_id": identity}


def _fact(mechanism, state, reason, sources=()):
    return {"mechanism": mechanism, "state": state, "reason": reason, "sources": list(sources)[:16]}


def _maps(root, node, *, snapshots=None):
    from .codex_wait_contract import WORKSPACE_MAP_RELATIVE
    from .root import validate_identifier
    try:
        path = root.tenant_home / WORKSPACE_MAP_RELATIVE
        snapshot = _read(path, 1024 * 1024, with_identity=True)
        data = snapshot[0]
    except FileNotFoundError:
        return []
    if snapshots is not None:
        snapshots.append((path, snapshot))
    value = _json(data)
    if not isinstance(value, dict) or set(value) != {"schema_version", "tenant_id", "mappings"} or type(value["schema_version"]) is not int or value["schema_version"] != 0 or value["tenant_id"] != root.tenant_id or not isinstance(value["mappings"], list):
        raise ValueError("invalid workspace map")
    prior = None
    result = []
    for row in value["mappings"]:
        if not isinstance(row, dict) or set(row) != {"workspace", "node_id"}:
            raise ValueError("invalid mapping")
        workspace = _path(row["workspace"])
        # Descriptor walk rejects a symlink at every directory component.
        directory = _parent(workspace / "coordinate")
        os.close(directory)
        owner = validate_identifier(row["node_id"], "node")
        coordinate = (str(workspace), owner)
        if prior is not None and coordinate <= prior:
            raise ValueError("unordered mapping")
        prior = coordinate
        if owner == node:
            result.append(workspace)
    return result


def _native(root, node, workspace, *, snapshots=None):
    from .codex_wait_contract import CodexWaitConsentLedger, CodexWaitSessionLedger
    claims = _rows(root, CodexWaitSessionLedger._relative(node), {"codex_wait_session_receipt"}, snapshots=snapshots)
    consents = _rows(root, CodexWaitConsentLedger._relative(node), {"codex_wait_consent_receipt"}, snapshots=snapshots)
    if any(row["node_id"] != node for row in claims + consents):
        raise ValueError("foreign native receipt")
    matching = [row for row in claims if row["workspace"] == str(workspace)]
    for index, row in enumerate(matching):
        predecessor = None if index == 0 else matching[index - 1]["id"]
        if row["predecessor_receipt_id"] != predecessor:
            raise ValueError("broken claim lineage")
    current = matching[-1] if matching else None
    consent = [row for row in consents if row["workspace"] == str(workspace)]
    return current, consent[-1] if consent else None


def _paused(root, node, session):
    from .wake_control import WakeController
    controller = WakeController(root)
    digest = hashlib.sha256(session.encode()).hexdigest()
    # Keep the literal coordinate: resolving it first would erase a forged
    # symlink, including one whose in-tenant target does not exist.
    path = root.tenant_home / "state/wake-control" / node / (digest + ".json")
    try:
        data = _read(path, 8192)
    except FileNotFoundError:
        return False
    # Native reader verifies the closed marker identity; compare the snapshot
    # again so an ungoverned replacement cannot silently qualify a witness.
    marker = controller._read_marker(path, node, digest)
    if marker is None or _read(path, 8192) != data:
        raise ValueError("changed pause marker")
    return True


def _lease(root, node, now, registry_rows):
    from .registry import Registry
    registry = Registry(root)
    value = registry._node_lease_state_from_records(node, registry_rows, now=now)
    return _fact("lease", "arranged" if value["state"] == "active" else "absent",
        "active_lease" if value["state"] == "active" else "no_active_lease",
        [_source(root.tenant_home / registry.relative_path, value["node_lease_id"])])


def _hold(root, node, now, maps, harness, *, observed_snapshots=None):
    from .stop_hold import StopHoldLedger
    from .codex_wait_contract import CodexWaitConsentLedger, CodexWaitSessionLedger
    path = root.tenant_home / "receipts/stop-holds" / (node + ".jsonl")
    snapshots = []
    # Consume the shared native reader; safe snapshots also protect its path.
    before = _rows(root, Path("receipts/stop-holds") / (node + ".jsonl"), {"stop_hold_receipt"}, snapshots=snapshots)
    rows = StopHoldLedger(root).read(node)
    after = _rows(root, Path("receipts/stop-holds") / (node + ".jsonl"), {"stop_hold_receipt"}, snapshots=snapshots)
    if before != rows or rows != after:
        raise ValueError("changed hold ledger")
    invocations = {}
    fields = ("node_id", "acting_session_id", "consent_receipt_id", "workspace", "harness", "started_at", "deadline_seconds")
    for row in rows:
        if row["node_id"] != node:
            raise ValueError("foreign hold")
        invocation = row["invocation_id"]
        if row["phase"] == "start":
            if invocation in invocations:
                raise ValueError("duplicate hold start")
            invocations[invocation] = [row, None]
        else:
            if invocation not in invocations or invocations[invocation][1] or any(row[field] != invocations[invocation][0][field] for field in fields):
                raise ValueError("unmatched hold exit")
            invocations[invocation][1] = row
    for row, closed in reversed(list(invocations.values())):
        age = (now - _time(row["started_at"])).total_seconds()
        if closed or not 0 <= age < row["deadline_seconds"]:
            continue
        workspace = _path(row["workspace"])
        if workspace not in maps or row["harness"] != harness:
            continue
        claim, consent = _native(root, node, workspace)
        if consent is None or consent["state"] != "armed" or consent["id"] != row["consent_receipt_id"] or row["deadline_seconds"] > consent["wait_deadline_seconds"]:
            continue
        if harness != "cursor" and claim is None:
            continue
        if claim is not None and (claim["acting_session_id"] != row["acting_session_id"] or claim["consent_receipt_id"] != consent["id"] or claim["state"] != "armed"):
            continue
        if _paused(root, node, row["acting_session_id"]):
            continue
        sources = [_source(path, row["id"]), _source(root.tenant_home / CodexWaitConsentLedger._relative(node), consent["id"])]
        if claim is not None:
            sources.append(_source(root.tenant_home / CodexWaitSessionLedger._relative(node), claim["id"]))
        return _fact("stop_hook", "arranged", "active_stop_hold", sources)
    # An expired unmatched start is ambiguous, not proof that its invocation
    # ended. Only a completed latest invocation of the current claim can
    # expose affirmative non-recovery. This never arms or displaces a session.
    if harness != "codex" or len(maps) != 1:
        return None
    if _maps(root, node, snapshots=snapshots) != maps:
        raise ValueError("changed workspace map")
    workspace = maps[0]
    claim, consent = _native(root, node, workspace, snapshots=snapshots)
    if claim is None or consent is None or claim["state"] != "armed" or consent["state"] != "armed" or claim["consent_receipt_id"] != consent["id"]:
        return None
    if _time(claim["timestamp"]) > now or _time(consent["timestamp"]) > now:
        return None
    matching = [(start, exit_row) for start, exit_row in invocations.values()
        if start["workspace"] == str(workspace) and start["harness"] == harness
        and start["acting_session_id"] == claim["acting_session_id"]
        and start["consent_receipt_id"] == consent["id"]]
    if not matching or any(exit_row is None or start["deadline_seconds"] > consent["wait_deadline_seconds"]
            for start, exit_row in matching):
        return None
    # An older invocation's late exit cannot hide a newer start.
    start, exit_row = matching[-1]
    if not _time(start["started_at"]) <= _time(exit_row["timestamp"]) <= now or any(
            _time(row["timestamp"]) > _time(exit_row["timestamp"]) for row in (claim, consent)):
        return None
    if any(_time(row["started_at"]) > now or _time(end["timestamp"]) > now
           for row, end in matching):
        return None
    if any(_read(coordinate, with_identity=True) != snapshot for coordinate, snapshot in snapshots):
        raise ValueError("changed closed claimant testimony")
    if observed_snapshots is not None:
        observed_snapshots.extend(snapshots)
    return _fact("stop_hook", "unknown", "closed_claimant_not_holding", [
        _source(root.tenant_home / CodexWaitSessionLedger._relative(node), claim["id"]),
        _source(path, start["id"]), _source(path, exit_row["id"]),
        _source(root.tenant_home / CodexWaitConsentLedger._relative(node), consent["id"])])


def _hook(root, node, now, registry_rows, *, snapshots=None):
    maps = _maps(root, node)
    registry = next((row for row in reversed(registry_rows)
        if row["kind"] == "registry_entry" and row["node_id"] == node), None)
    if registry is None or registry["state"] != "active":
        raise ValueError("inactive registration")
    harness = registry["role"].lower()
    active = _hold(root, node, now, maps, harness, observed_snapshots=snapshots)
    if active is not None:
        return active
    if len(maps) > 1:
        raise ValueError("ambiguous recipient mappings")
    if harness == "cursor":
        provenance = root.tenant_home / "state/cursor-wait" / node / "installed-hook.json"
        saved = _json(_read(provenance, 1024 * 1024))
        if not isinstance(saved, dict) or set(saved) != {"root", "node", "workspace", "hooks_path"} or saved["root"] != str(root.tenant_home) or saved["node"] != node:
            raise ValueError("foreign install coordinate")
        workspace = _path(saved["workspace"])
        hooks = _path(saved["hooks_path"])
        if hooks != workspace / ".cursor/hooks.json" or (maps and maps[0] != workspace):
            raise ValueError("foreign hook coordinate")
        directory = _parent(workspace / "coordinate")
        os.close(directory)
        value = _json(_read(hooks, 1024 * 1024))
        if not isinstance(value, dict) or type(value.get("version")) is not int or value["version"] != 1 or not isinstance(value.get("hooks"), dict):
            raise ValueError("invalid Cursor hook configuration")
        stops = value["hooks"].get("stop")
    elif harness == "codex":
        from .seat_onboarding import _load, _record_path
        if len(maps) != 1 or maps[0].name != "repo":
            raise ValueError("no exact Codex seat journal coordinate")
        home = maps[0].parent
        provenance = _record_path(root, home)
        before = _read(provenance)
        saved = _load(provenance, root, home)
        if _read(provenance) != before or saved["state"] != "ready" or saved["node_id"] != node or saved["registry_row"] != registry:
            raise ValueError("foreign or incomplete Codex journal")
        hooks = _path(saved["request"]["codex_hooks"])
        _read(_path(saved["request"]["codex_config"]), 1024 * 1024)
        value = _json(_read(hooks, 1024 * 1024))
        if not isinstance(value, dict) or not isinstance(value.get("hooks"), dict):
            raise ValueError("invalid Codex hook configuration")
        stops = value["hooks"].get("Stop")
    elif harness in {"worker", "smoke"}:
        # These fixture/worker harnesses cannot carry a Stop hook. Other
        # unsupported harnesses can, so unreadable provenance stays unknown.
        return _fact("stop_hook", "absent", "harness_has_no_stop_hook",
            [_source(root.tenant_home / "registry/entries.jsonl", registry["id"])])
    else:
        return _fact("stop_hook", "unknown", "hook_provenance_unavailable")
    if not isinstance(stops, list):
        raise ValueError("invalid Stop list")
    return _fact("stop_hook", "absent" if not stops else "unknown",
        "empty_hook_configuration" if not stops else "configured_without_live_testimony",
        [_source(provenance), _source(hooks)])


def _board(root, node, now, registry_rows, *, snapshots=None):
    from .codex_wait_contract import CodexWaitConsentLedger, CodexWaitSessionLedger
    from .seat_board import SeatBoard, KIND, _validate_progress
    from .seat_declaration import require_declared_coordinate
    relative = Path("receipts/seat-board") / (node + ".jsonl")
    rows = _rows(root, relative, {KIND})
    claims = _rows(root, CodexWaitSessionLedger._relative(node), {"codex_wait_session_receipt"})
    sources = [_source(root.tenant_home / relative)]
    if not rows and not claims:
        return _fact("seat_board", "absent", "no_board_or_claim", sources)
    groups = {}
    for row in rows:
        if row["node_id"] != node:
            raise ValueError("foreign boarding receipt")
        groups.setdefault(row["idempotency_key"], []).append(row)
    for group in groups.values():
        _validate_progress(group)
    if not rows or rows[-1]["step"] != "completed":
        return _fact("seat_board", "unknown", "boarding_incomplete", sources)
    row = rows[-1]
    maps = _maps(root, node)
    referenced = [claim for claim in claims if claim["id"] == row["arm_receipt_id"]]
    if len(referenced) != 1 or referenced[0]["node_id"] != node:
        raise ValueError("boarding arm unavailable")
    workspace = _path(referenced[0]["workspace"])
    if workspace not in maps:
        raise ValueError("boarding workspace unavailable")
    claim, consent = _native(root, node, workspace)
    if claim is None or consent is None or claim["id"] != row["arm_receipt_id"] or claim["consent_receipt_id"] != consent["id"] or consent["state"] != "armed" or claim["state"] != "armed":
        return _fact("seat_board", "unknown", "boarding_authority_not_current", sources)
    coordinate = node + "\0" + str(workspace) + "\0" + row["acting_session_id"] + "\0"
    if row["coordinate_digest"] not in {hashlib.sha256((coordinate + str(flag)).encode()).hexdigest() for flag in (False, True)}:
        raise ValueError("foreign board coordinate")
    declaration_path = workspace / "SEAT.json"
    declaration_snapshot = _read(declaration_path, 1024 * 1024, with_identity=True)
    declaration = require_declared_coordinate(workspace, node, root)
    if declaration["workspace_identity"] != "declared" or _read(declaration_path, 1024 * 1024, with_identity=True) != declaration_snapshot:
        raise ValueError("boarding declaration no longer current")
    # Read native referenced planes safely before using the existing replay
    # validator. Its result only projects receipts; it never drains or ACKs.
    board_planes = {}
    for path in (Path("receipts/wake-control") / (node + ".jsonl"),
                 Path("receipts/deliveries") / (node + ".jsonl"),
                 Path("receipts/acks") / (node + ".jsonl")):
        try:
            board_planes[path] = _read(root.tenant_home / path)
        except FileNotFoundError:
            board_planes[path] = None
    SeatBoard(root)._result(row, workspace=str(workspace))
    for path, data in board_planes.items():
        try:
            current = _read(root.tenant_home / path)
        except FileNotFoundError:
            current = None
        if current != data:
            raise ValueError("changed native board evidence")
    if row["resume_outcome"] not in {"resumed", "already_active"} or (row["resume_outcome"] == "already_active" and row["resume_receipt_id"] is not None):
        raise ValueError("invalid board resume evidence")
    if not row["item_ids"] and (row["delivery_receipt_id"] is not None or row["ack_receipt_id"] is not None):
        raise ValueError("forged empty drain evidence")
    age = (now - _time(row["timestamp"])).total_seconds()
    if not 0 <= age < consent["wait_deadline_seconds"] or _paused(root, node, row["acting_session_id"]):
        return _fact("seat_board", "unknown", "boarding_not_live", sources)
    # A current board is a distinct witness, but its older receipt cannot
    # advertise a claimant whose later native Stop invocation has closed.
    registry = next((entry for entry in reversed(registry_rows)
        if entry["kind"] == "registry_entry" and entry["node_id"] == node), None)
    if registry is not None:
        hold = _hold(root, node, now, maps, registry["role"].lower(), observed_snapshots=snapshots)
        if hold is not None and hold["reason"] == "closed_claimant_not_holding":
            return _fact("seat_board", "unknown", hold["reason"], [
                _source(root.tenant_home / relative, row["id"]), *hold["sources"]])
    return _fact("seat_board", "arranged", "recent_current_board", [
        _source(root.tenant_home / relative, row["id"]),
        _source(root.tenant_home / CodexWaitSessionLedger._relative(node), claim["id"]),
        _source(root.tenant_home / CodexWaitConsentLedger._relative(node), consent["id"]),
        _source(declaration_path)])


def observe_mechanisms(root, node_id, *, observed_at):
    from .root import validate_identifier
    from .registry import REGISTRY_KINDS, Registry
    try:
        node = validate_identifier(node_id, "node")
        now = _time(observed_at)
    except (FloatiError, TypeError, ValueError, AttributeError) as exc:
        raise ProtocolRefusal("recipient_readiness_invalid", "readiness requires an exact node and aware observation timestamp",
            remedy="supply the registered recipient node and an aware ISO observation timestamp") from exc
    names = ("lease", "stop_hook", "seat_board")
    snapshots = []
    try:
        registry_rows = _rows(root, Path("registry/entries.jsonl"), REGISTRY_KINDS, snapshots=snapshots)
        # This also proves active registration from the very same snapshot used
        # for all mechanism facts, without another native registry read.
        Registry._node_lease_state_from_records(node, registry_rows, now=now)
    except (FloatiError, OSError, UnicodeError, ValueError, TypeError, KeyError, RecursionError):
        return [_fact(name, "unknown", "registry_evidence_unavailable") for name in names]
    facts = []
    for name, reader in (("lease", _lease), ("stop_hook", _hook), ("seat_board", _board)):
        try:
            options = {} if name == "lease" else {"snapshots": snapshots}
            fact = reader(root, node, now, registry_rows, **options)
        except (FloatiError, OSError, UnicodeError, ValueError, TypeError, KeyError, RecursionError):
            fact = _fact(name, "unknown", "evidence_unavailable")
        facts.append(fact)
    for path, snapshot in snapshots:
        try:
            if _read(path, with_identity=True) == snapshot:
                continue
        except (FloatiError, OSError, UnicodeError, ValueError, TypeError, KeyError, RecursionError):
            pass
        reason = "registry_evidence_changed" if path == root.tenant_home / "registry/entries.jsonl" else "mechanism_evidence_changed"
        return [_fact(name, "unknown", reason) for name in names]
    return facts

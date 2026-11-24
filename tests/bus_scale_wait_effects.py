"""Read-only decoding of a complete idle native census waiter invocation.

This proves durable testimony, not phase admission or performance. The caller
retains the original transport bytes and collected-child identity. Holder birth
is bounded native testimony; this module performs no process inspection.
"""
from __future__ import annotations

import base64
import binascii
import copy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import uuid

from floati.codex_wait_contract import (
    CodexWaitConsentLedger, CodexWaitSessionLedger, WORKSPACE_MAP_RELATIVE,
    consent_ledger_relative, resolve_participant,
)
from floati.errors import FloatiError
from floati.framing import decode_frames, encode_frame
from floati.jsonl import MAX_LEDGER_BYTES, _decode_path_records
from floati.recipient_mechanisms import _read, _signature
from floati.registry import Registry
from floati.stop_hold import StopHoldLedger, _IMMUTABLE
from tests.bus_scale_durable import (
    _RAW_FIELDS, _EMPTY, _context, _coordinate, _equal, _json, _stamp, _unchanged,
)


NODE = "scale-worker"
SESSION = "bus-scale-native-census"
DIGEST = hashlib.sha256(SESSION.encode()).hexdigest()
HOLDER = "state/codex-wait/scale-worker/holder.json"
BREAKER = "state/codex-wait/scale-worker/breaker.json"
HOLDS = "receipts/stop-holds/scale-worker.jsonl"
EXHAUSTION = "receipts/codex-wait-exhaustion/scale-worker.jsonl"
EXITS = "receipts/wake-waiter-exit/scale-worker.jsonl"
PRESENTATIONS = "receipts/cursor-presentations/scale-worker.jsonl"
WITNESS = "state/cursor-wait/scale-worker/presentation-history.v1.json"
ADAPTER = "state/wake-daemon/adapters/scale-worker/codex.json"
OBSERVATION = "state/waiter-observations/scale-worker.json"
WAKE_CONTROL_LOCK = "state/wake-control/scale-worker.lock"
INVOCATION = "state/waiter-invocations/scale-worker/" + DIGEST + ".json"
INVOCATION_LOCK = "state/waiter-invocations/scale-worker/" + DIGEST + ".lock"
APPENDS = {HOLDS: "stop_hold_receipt", EXHAUSTION: "codex_wait_exhaustion_receipt",
           EXITS: "wake_waiter_exit_receipt", PRESENTATIONS: "cursor_presentation_receipt"}
REPLACES = {HOLDER, BREAKER, WITNESS, ADAPTER, INVOCATION, OBSERVATION}
LOCKS = {path + ".lock" for path in APPENDS} | {
    "events.jsonl.lock", "receipts/deliveries/scale-worker.jsonl.lock",
    "receipts/acks/scale-worker.jsonl.lock", "receipts/wake-coordination/scale-worker/lane.lock",
    WAKE_CONTROL_LOCK, INVOCATION_LOCK}


def _closed(value, fields, label):
    if type(value) is not dict or set(value) != set(fields):
        raise ValueError(label + " has an unreviewed shape")
    _json(value)


def _number(value, label, *, minimum=0):
    if type(value) not in (int, float) or not math.isfinite(value) or value < minimum:
        raise ValueError(label + " must be finite and bounded")
    return value


def _generated(value, prefix, begin, end):
    if type(value) is not str or re.fullmatch(re.escape(prefix) + r"[0-9a-f]{32}", value) is None:
        raise ValueError("generated identity has a nonnative prefix or encoding")
    ident = uuid.UUID(hex=value[len(prefix):])
    if ident.version != 7 or ident.variant != uuid.RFC_4122:
        raise ValueError("generated identity is not native UUIDv7")
    moment = datetime.fromtimestamp((ident.int >> 80) / 1000, timezone.utc)
    if not begin - timedelta(milliseconds=1) <= moment <= end:
        raise ValueError("generated identity lies outside retained invocation")


def _snapshot(root, relative, snapshots):
    path = root.resolve_relative(relative)
    if path != root.tenant_home / relative:
        raise ValueError("wait source coordinate is aliased")
    limit = 2048 if relative == OBSERVATION else MAX_LEDGER_BYTES
    observed = _read(path, limit, with_identity=True)
    if observed[1][3] != os.getuid():
        raise ValueError("wait source is not account-owned")
    if relative in {WAKE_CONTROL_LOCK, OBSERVATION}:
        metadata = path.lstat()
        if metadata.st_nlink != 1 or _signature(metadata) != observed[1]:
            raise ValueError("wait source is shared or changed during its snapshot")
        if relative == OBSERVATION and stat.S_IMODE(metadata.st_mode) != 0o600:
            raise ValueError("waiter observation is not private")
    if path in snapshots and snapshots[path] != observed:
        raise ValueError("wait source changed between reads")
    snapshots[path] = observed
    return observed[0]


def _object(raw):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON field")
            result[key] = value
        return result
    value = json.loads(raw, object_pairs_hook=unique)
    _json(value)  # Reject nonfinite JSON numbers without deleting any fields.
    return value


def _projection(root, delta):
    if type(delta) is not dict or not delta or not set(delta) <= set(APPENDS) | REPLACES | LOCKS:
        raise ValueError("wait delta contains missing or unreviewed changed paths")
    result, snapshots = {}, {}
    for relative, row in delta.items():
        _closed(row, _RAW_FIELDS, "raw wait delta")
        if type(relative) is not str or Path(relative).as_posix() != relative:
            raise ValueError("wait delta path is not canonical")
        for field in ("before_bytes", "after_bytes"):
            if type(row[field]) is not int or not 0 <= row[field] <= MAX_LEDGER_BYTES:
                raise ValueError("wait delta length is not a bounded integer")
        for field in ("before_sha256", "after_sha256"):
            if type(row[field]) is not str or re.fullmatch(r"[0-9a-f]{64}", row[field]) is None:
                raise ValueError("wait delta hash is not SHA-256")
        if type(row["payload_base64"]) is not str:
            raise ValueError("wait delta payload is not base64 text")
        payload = base64.b64decode(row["payload_base64"], validate=True)
        if base64.b64encode(payload).decode() != row["payload_base64"]:
            raise ValueError("wait delta payload is not canonical base64")
        data = _snapshot(root, relative, snapshots)
        if len(data) != row["after_bytes"] or hashlib.sha256(data).hexdigest() != row["after_sha256"]:
            raise ValueError("wait delta does not bind current native bytes")
        if relative in LOCKS:
            if (row["operation"] != "create_lock" or payload or data
                    or row["before_bytes"] != 0 or row["before_sha256"] != _EMPTY):
                raise ValueError("wait lock creation is not the exact empty native lock")
            result[relative] = {"operation": "create_lock"}
        elif relative in REPLACES:
            if row["operation"] != "replace" or data != payload:
                raise ValueError("wait replacement differs from current native JSON")
            # These fixed census fixtures begin without per-invocation state.
            if row["before_bytes"] != 0 or row["before_sha256"] != _EMPTY:
                raise ValueError("wait state replacement has unexpected prior state")
            result[relative] = {"operation": "replace", "value": _object(data)}
        else:
            before = row["before_bytes"]
            if (row["operation"] != "append" or before + len(payload) != len(data)
                    or data[before:] != payload
                    or hashlib.sha256(data[:before]).hexdigest() != row["before_sha256"]):
                raise ValueError("wait append boundary or prefix is inconsistent")
            prior, frames = decode_frames(data[:before]), decode_frames(payload)
            rows = _decode_path_records(root.resolve_relative(relative), root.tenant_id,
                frozenset({APPENDS[relative]}), data)
            _equal(rows, prior + frames, "complete native wait framing")
            if b"".join(encode_frame(frame) for frame in rows) != data:
                raise ValueError("wait ledger does not use native canonical framing")
            if relative == HOLDS:
                StopHoldLedger._progress(rows, NODE)
            if not frames and relative != PRESENTATIONS:
                raise ValueError("required wait append is empty")
            result[relative] = {"operation": "append", "frames": copy.deepcopy(frames)}
    required = {HOLDER, BREAKER, HOLDS, EXHAUSTION, EXITS, PRESENTATIONS, WITNESS,
                INVOCATION, INVOCATION_LOCK}
    if not required <= set(result):
        raise ValueError("complete native idle wait effects are missing")
    return result, snapshots


def _authority(root, workspace, snapshots):
    # Snapshot these before native read-only resolution, then recheck all at
    # completion. Never call participate/arm/select: those can append evidence.
    for path in ("registry/entries.jsonl", str(WORKSPACE_MAP_RELATIVE),
                 str(consent_ledger_relative(NODE)), str(CodexWaitSessionLedger._relative(NODE))):
        _snapshot(root, path, snapshots)
    participant = resolve_participant(root.tenant_home, workspace)
    if participant is None or participant.binding.node_id != NODE or participant.binding.workspace != Path(workspace):
        raise ValueError("wait workspace has no exact native participant binding")
    consent = CodexWaitConsentLedger(root).require_armed(participant.binding)
    for key, value in dict(node_id=NODE, workspace=str(workspace), state="armed",
                           workspace_map_digest=participant.binding.map_digest).items():
        _equal(consent[key], value, "current consent " + key)
    relative = str(CodexWaitSessionLedger._relative(NODE))
    rows = _decode_path_records(root.resolve_relative(relative), root.tenant_id,
        frozenset({"codex_wait_session_receipt"}), snapshots[root.resolve_relative(relative)][0])
    matching = CodexWaitSessionLedger._matching(rows, participant.binding)
    if not matching:
        raise ValueError("wait has no current native session claim")
    claim = matching[-1]
    for key, value in dict(node_id=NODE, acting_session_id=SESSION, state="armed",
                           consent_receipt_id=consent["id"],
                           workspace_map_digest=participant.binding.map_digest).items():
        _equal(claim[key], value, "current session " + key)
    role = Registry(root).require_active(NODE)["role"].casefold()
    if role not in {"codex", "zcode"}:
        raise ValueError("wait census harness is outside the reviewed fixture")
    from floati.wake_control import is_session_paused
    if is_session_paused(root, NODE, SESSION):
        raise ValueError("native waiter session is currently paused")
    return consent, role


def _normalize(result, path, key, value, reason, *, index=None):
    row = result["delta"][path]
    pointer = _coordinate("/delta", path)
    if index is None:
        target, pointer = row["value"], pointer + "/value"
    else:
        target, pointer = row["frames"][index], pointer + "/frames/" + str(index)
    # Durations may already equal the consent bound; they still require an
    # audit because they are explicitly measured variables in raw evidence.
    target[key] = value
    result["normalizations"].append(dict(pointer=_coordinate(pointer, key), reason=reason))


def _validate(result, root, workspace, begin, end, child_pid, snapshots, before):
    _equal(result["artifact"], {"decision": "block", "reason":
        "(floati: wait deadline exhausted; end this turn to re-arm)"}, "idle expiry stdout")
    consent, role = _authority(root, workspace, snapshots)
    limit = consent["wait_deadline_seconds"]
    holder = result["delta"][HOLDER]["value"]
    _closed(holder, {"schema_version", "kind", "tenant_id", "node_id", "session_id", "pid",
                     "process_start_epoch", "timestamp"}, "holder")
    for key, value in dict(schema_version=1, kind="codex_wait_holder_testimony",
            tenant_id=root.tenant_id, node_id=NODE, session_id=SESSION, pid=child_pid).items():
        _equal(holder[key], value, "holder " + key)
    if type(holder["pid"]) is not int or not 1 <= holder["pid"] <= 2**31 - 1:
        raise ValueError("holder PID is not native")
    stamp = _stamp(holder["timestamp"], begin, end)
    birth = _number(holder["process_start_epoch"], "holder birth", minimum=.000001)
    if birth > stamp.timestamp() + .001 or (
            child_pid != os.getpid() and birth < begin.timestamp() - 1):
        raise ValueError("native holder birth is outside retained child bounds")
    # In-process controls have a pre-existing test-process birth. The actual
    # census supplies a distinct retained Popen PID and gets both wall bounds.
    breaker = result["delta"][BREAKER]["value"]
    _closed(breaker, {"hits"}, "breaker")
    if type(breaker["hits"]) is not list or len(breaker["hits"]) != 1:
        raise ValueError("wait must append exactly one native breaker hit")
    hit = _number(breaker["hits"][0], "breaker time")
    if not begin.timestamp() <= hit <= end.timestamp():
        raise ValueError("breaker hit lies outside retained invocation")

    rows = {path: result["delta"][path]["frames"] for path in APPENDS}
    if len(rows[HOLDS]) != 2 or any(len(rows[path]) != 1 for path in (EXHAUSTION, EXITS)):
        raise ValueError("wait must have one complete hold pair, exhaustion and exit")
    if rows[PRESENTATIONS] or snapshots[root.resolve_relative(PRESENTATIONS)][0]:
        raise ValueError("idle wait unexpectedly prepared or published presentation frames")
    start, finish = rows[HOLDS]
    if [start["phase"], finish["phase"]] != ["start", "exit"]:
        raise ValueError("hold pair is not ordered start then exit")
    StopHoldLedger._progress(rows[HOLDS], NODE)
    for key in _IMMUTABLE:
        _equal(start[key], finish[key], "hold immutable " + key)
    if start["id"] != start["invocation_id"] or finish["id"] == start["id"]:
        raise ValueError("hold pair does not bind one native invocation")
    from tests.fixtures.bus_scale_census.bus_scale_waiter_invocation_effects import verify_waiter_invocation
    if type(before) is not dict:
        raise ValueError("native invocation proof requires retained immediate before bytes")
    after = dict(before)
    after.update({path.relative_to(root.tenant_home).as_posix(): observed[0]
                  for path, observed in snapshots.items()})
    invocation = verify_waiter_invocation(root, NODE, SESSION, workspace, before, after,
        child_pid=child_pid, wall_begin=begin, wall_end=end, phase="exited")
    _equal(invocation["path"], INVOCATION, "exact waiter invocation coordinate")
    _equal(invocation["value"], result["delta"][INVOCATION]["value"], "retained invocation state")
    for row in rows[HOLDS]:
        _equal(row.get("holder_pid"), child_pid, "hold native invocation PID")
        _equal(row.get("process_start_epoch"), birth, "hold native invocation birth")
    for row in rows[HOLDS]:
        for key, value in dict(node_id=NODE, acting_session_id=SESSION, workspace=str(workspace),
                               consent_receipt_id=consent["id"], harness=role).items():
            _equal(row[key], value, "hold " + key)
        _generated(row["id"], "stop-hold-", begin, end)
        _stamp(row["timestamp"], begin, end)
    started = _stamp(start["started_at"], begin, end)
    if start["timestamp"] != start["started_at"] or _stamp(finish["timestamp"], begin, end) < started:
        raise ValueError("hold timestamps do not enclose the native invocation")
    budget = _number(start["deadline_seconds"], "native hold budget", minimum=.000001)
    if budget > limit:
        raise ValueError("native hold budget exceeds its exact consent")
    exhaustion, exit_row = rows[EXHAUSTION][0], rows[EXITS][0]
    for row, prefix in ((exhaustion, "codex-wait-exhaustion-"), (exit_row, "wake-waiter-exit-")):
        _generated(row["id"], prefix, begin, end)
        _stamp(row["timestamp"], begin, end)
        _equal(row["node_id"], NODE, "expiry node")
        _equal(row["session_digest"], DIGEST, "expiry session")
    _equal(exhaustion["outcome"], "consent_reopened", "natural exhaustion outcome")
    _equal(exit_row["reason_code"], "exhausted", "natural waiter exit")
    waited = exhaustion["waited_seconds"]
    _equal(waited, exit_row["waited_seconds"], "correlated natural expiry duration")
    if type(waited) is not int or not limit <= waited <= int((end - begin).total_seconds()) + 1:
        raise ValueError("natural expiry duration lies outside consent/wall bounds")
    keys = []
    for row, suffix in ((exhaustion, "-exhaustion"), (exit_row, "-exit-exhausted")):
        key = row["idempotency_key"]
        if type(key) is not str or not key.endswith(suffix):
            raise ValueError("expiry idempotency key has an unreviewed path class")
        invocation = key[:-len(suffix)]
        _generated(invocation, "codex-stop-", begin, end)
        keys.append(invocation)
    witness = result["delta"][WITNESS]["value"]
    _closed(witness, {"schema_version", "tenant_id", "node_id", "genesis_id", "before_bytes",
                      "before_digest", "reserved_record"}, "presentation genesis")
    for key, value in dict(schema_version=1, tenant_id=root.tenant_id, node_id=NODE,
                           before_bytes=0, before_digest=_EMPTY, reserved_record=None).items():
        _equal(witness[key], value, "empty presentation witness " + key)
    _generated(witness["genesis_id"], "genesis-", begin, end)

    audits = result["normalizations"]
    for key, value in (("pid", "<retained-child-pid>"), ("process_start_epoch", "<native-holder-birth>"),
                       ("timestamp", "<invocation-time>")):
        _normalize(result, HOLDER, key, value, "exact retained PID, bounded native birth and holder session validated")
    breaker["hits"][0] = "<invocation-time>"
    audits.append(dict(pointer=_coordinate("/delta", BREAKER) + "/value/hits/0",
                       reason="one native breaker hit within retained invocation validated"))
    _normalize(result, WITNESS, "genesis_id", "<generated-presentation-genesis>",
               "native empty presentation ledger and coupled genesis witness validated")
    for index in (0, 1):
        for key, value in (("id", "<hold-start-id>" if index == 0 else "<hold-exit-id>"),
                ("invocation_id", "<hold-start-id>"), ("timestamp", "<hold-start-time>" if index == 0 else "<hold-exit-time>"),
                ("started_at", "<hold-start-time>"), ("workspace", "<census-workspace>")):
            _normalize(result, HOLDS, key, value, "native correlated hold pair and exact fixture authority validated", index=index)
        _normalize(result, HOLDS, "deadline_seconds", limit,
                   "native paired measured remaining budget bounded by exact consent; raw duration retained", index=index)
        for key, value in (("holder_pid", "<retained-child-pid>"),
                           ("process_start_epoch", "<native-holder-birth>")):
            _normalize(result, HOLDS, key, value,
                       "exact native invocation and hold process binding validated", index=index)
    for key, value in (("pid", "<retained-child-pid>"),
            ("process_start_epoch", "<native-holder-birth>"),
            ("parent_pid", "<native-parent-pid>"),
            ("parent_process_start_epoch", "<native-parent-birth>"),
            ("token", "<native-invocation-token>"),
            ("invocation_id", "<hold-start-id>"), ("workspace", "<census-workspace>")):
        _normalize(result, INVOCATION, key, value,
                   "exact exited native invocation, ancestry and hold pair validated")
    ancestry = result["delta"][INVOCATION]["value"]["host_ancestry"]
    for index, ancestor in enumerate(ancestry):
        for key in ("pid", "process_start_epoch", "parent_pid"):
            if key == "parent_pid" and index == len(ancestry) - 1:
                continue  # The proved terminal edge remains PID 1.
            ancestor[key] = "<native-host-" + key + "-" + str(index) + ">"
            audits.append(dict(pointer=_coordinate("/delta", INVOCATION)
                + "/value/host_ancestry/" + str(index) + "/" + key,
                reason="complete native ancestry and exact parent edges validated"))
    for path, label, invocation, suffix in ((EXHAUSTION, "exhaustion", keys[0], "-exhaustion"),
            (EXITS, "exit", keys[1], "-exit-exhausted")):
        # _integrity_exit can share the startup key. Ordinary evaluation uses
        # a fresh exhaustion key; retain this legal path-class relationship.
        name = "<startup-invocation>" if invocation == keys[1] else "<evaluation-invocation>"
        for key, value in (("id", "<generated-" + label + "-id>"), ("timestamp", "<invocation-time>"),
                           ("idempotency_key", name + suffix)):
            _normalize(result, path, key, value, "native expiry identity, bounded clock and legal path-class relationship validated", index=0)
        _normalize(result, path, "waited_seconds", limit,
                   "matched natural expiry duration bounded by consent and retained wall; raw duration retained", index=0)
    if ADAPTER in result["delta"]:
        _adapter(result, root, workspace, begin, end, role)
    path = root.tenant_home / OBSERVATION
    if OBSERVATION in result["delta"]:
        from floati.waiter_state import _decode

        raw = snapshots[path][0]
        row, observed, expires = _decode(root, NODE, raw)
        if (not begin <= observed <= end or expires - observed != timedelta(seconds=120)
                or raw != (json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n").encode()
                or any(row[key] != moment.isoformat() for key, moment in
                       (("observed_at", observed), ("valid_until", expires)))):
            raise ValueError("waiter observation differs from bounded native backend testimony")
        for field in ("observed_at", "valid_until"):
            _normalize(result, OBSERVATION, field, "<native-waiter-" + field + ">",
                       "exact backend testimony, invocation clock and 120-second validity validated")
    elif (before is None or OBSERVATION not in before) and os.path.lexists(path):
        raise ValueError("wait delta omitted a new native waiter observation")


def _adapter(result, root, workspace, begin, end, role):
    from floati.wake_daemon_adapters import adapter_contract_digest, _ADAPTER_VERSIONS, CODEX_EXECUTABLE
    from floati.wake_daemon_contract import AdapterBindingStore, DaemonCoordinate, _BINDING_FIELDS
    if role != "codex":
        raise ValueError("non-Codex waiter must not publish a Codex adapter")
    row = result["delta"][ADAPTER]["value"]
    _closed(row, _BINDING_FIELDS, "waiter Codex adapter")
    coordinate = DaemonCoordinate(root, NODE, "codex")
    _equal(AdapterBindingStore(root).read(coordinate), row, "current native adapter binding")
    for key, value in dict(session_id=SESSION, session_digest=DIGEST, workspace=str(workspace),
                           executable=str(CODEX_EXECUTABLE),
                           adapter_version=_ADAPTER_VERSIONS["codex"],
                           adapter_digest=adapter_contract_digest("codex")).items():
        _equal(row[key], value, "Codex adapter " + key)
    epoch = row["binding_epoch"]
    if type(epoch) is not int or not int(begin.timestamp() * 1e9) <= epoch <= int(end.timestamp() * 1e9):
        raise ValueError("adapter binding clock is outside retained invocation")
    for key, value in (("workspace", "<census-workspace>"), ("binding_epoch", "<native-binding-epoch>"),
                       ("coordinate_digest", "<exact-fixture-coordinate-digest>")):
        _normalize(result, ADAPTER, key, value,
                   "source-backed native Codex binding and exact fixture coordinate validated")


def normalize_wait_delta(case, delta, artifact, *, wall_begin, wall_end, child_pid,
                         home, workspace, child_cpu_seconds=None, before_raw_files=None):
    """Decode idle expiry only; preserve every unaudited semantic field.

    Both wait case names describe the same complete invocation delta here.
    Phase coverage/CPU qualification remain the producer's separate obligation.
    The optional collected CPU bound is checked for shape but is not a duration
    field in native wait receipts and creates no additional normalization.
    """
    try:
        if case not in {"wait-start", "wait-idle"}:
            raise ValueError("unsupported wait census case")
        if child_cpu_seconds is not None:
            _number(child_cpu_seconds, "collected child CPU")
        root, begin, end = _context(wall_begin, wall_end, child_pid, home, workspace)
        semantic, snapshots = _projection(root, delta)
        result = dict(artifact=copy.deepcopy(artifact), delta=semantic, normalizations=[])
        _json(result)
        _validate(result, root, Path(workspace), begin, end, child_pid, snapshots, before_raw_files)
        _unchanged(snapshots)
        # The native reader's signature omits nlink. Recheck unshared identity.
        for relative in (WAKE_CONTROL_LOCK, OBSERVATION):
            if root.tenant_home / relative in snapshots:
                _snapshot(root, relative, snapshots)
        return result
    except (FloatiError, OSError, TypeError, KeyError, AttributeError, OverflowError,
            binascii.Error, UnicodeError) as exc:
        raise ValueError("native wait effect proof could not be established") from exc

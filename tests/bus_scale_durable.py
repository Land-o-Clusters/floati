"""Read-only, explicit semantic projections of native census durable effects.

Raw inventories remain the producer's evidence. This helper binds every append
to its current ordinary ledger, validates native framing/schema and own-time
facts, and preserves complete records. It neither measures nor writes anything.
Only the audited generated fields below receive comparison placeholders.
"""
from __future__ import annotations

import base64
import binascii
import copy
from datetime import datetime, timedelta, timezone
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import re
import stat

from floati.errors import FloatiError
from floati.events import EVENT_KINDS, validate_event_records
from floati.framing import decode_frames
from floati.jsonl import MAX_LEDGER_BYTES, _decode_path_records
from floati.mail_health import RecipientReadiness
from floati.recipient_mechanisms import _read
from floati.root import FloatiRoot
from floati.timings import argv_digest, outcome_for_status, refusal_code_for


_SOURCE = Path(__file__).resolve().parents[1]
_EVENTS = "events.jsonl"
_EXHAUSTION = "receipts/codex-wait-exhaustion/scale-worker.jsonl"
_RAW_FIELDS = {"operation", "before_sha256", "after_sha256", "before_bytes",
               "after_bytes", "payload_base64"}
_EMPTY = hashlib.sha256(b"").hexdigest()
_FIXTURE = "<bus-scale-fixture>"
_SESSION = hashlib.sha256(b"bus-scale-native-census").hexdigest()


def _json(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False,
                      sort_keys=True, separators=(",", ":"))


def _equal(actual, expected, label):
    if _json(actual) != _json(expected):
        raise ValueError(label + " differs from native testimony")


def _directory(value, label):
    path = Path(value)
    if (not path.is_absolute() or path.resolve(strict=True) != path
            or not stat.S_ISDIR(path.lstat().st_mode)
            or path.lstat().st_uid != os.getuid()):
        raise ValueError(label + " must be its canonical owned directory")
    return path


def _context(wall_begin, wall_end, child_pid, home, workspace):
    for value in (wall_begin, wall_end):
        if (not isinstance(value, datetime) or value.tzinfo is None
                or value.utcoffset() is None):
            raise ValueError("retained walls must be aware datetimes")
    begin, end = wall_begin.astimezone(timezone.utc), wall_end.astimezone(timezone.utc)
    if begin > end or type(child_pid) is not int or child_pid <= 0:
        raise ValueError("invalid retained invocation bounds or child identity")
    home = _directory(home, "home")
    _directory(workspace, "workspace")
    return FloatiRoot.open_direct_home(home, create=False), begin, end


def _stamp(value, begin, end):
    if type(value) is not str or re.fullmatch(
            r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z", value) is None:
        raise ValueError("generated clock must be the native millisecond UTC stamp")
    moment = datetime.fromisoformat(value[:-1] + "+00:00")
    # utc_now truncates milliseconds; the retained parent clock does not.
    if not begin - timedelta(milliseconds=1) <= moment <= end:
        raise ValueError("generated clock lies outside retained invocation")
    return moment


def _coordinate(parent, key):
    return parent + "/" + str(key).replace("~", "~0").replace("/", "~1")


def _replace(container, key, value, pointer, audits, reason):
    if _json(container[key]) == _json(value):
        raise ValueError("normalization must change its validated leaf")
    container[key] = value
    audits.append({"pointer": _coordinate(pointer, key), "reason": reason})


def _raw_projection(root, delta, append_paths, lock_paths):
    if type(delta) is not dict or not delta or not set(delta) <= append_paths | lock_paths:
        raise ValueError("durable delta contains unreviewed changed paths")
    semantic, snapshots = {}, {}
    for relative, row in delta.items():
        if (type(relative) is not str or Path(relative).as_posix() != relative
                or type(row) is not dict or set(row) != _RAW_FIELDS):
            raise ValueError("raw delta must retain the exact native entry fields")
        path = root.resolve_relative(relative)
        if path != root.tenant_home / relative:
            raise ValueError("raw delta path is aliased")
        for field in ("before_bytes", "after_bytes"):
            if type(row[field]) is not int or not 0 <= row[field] <= MAX_LEDGER_BYTES:
                raise ValueError("raw delta length is not a bounded integer")
        for field in ("before_sha256", "after_sha256"):
            if type(row[field]) is not str or re.fullmatch(r"[0-9a-f]{64}", row[field]) is None:
                raise ValueError("raw delta hash is not SHA-256")
        if type(row["payload_base64"]) is not str:
            raise ValueError("raw append payload must be base64 text")
        payload = base64.b64decode(row["payload_base64"], validate=True)
        if base64.b64encode(payload).decode("ascii") != row["payload_base64"]:
            raise ValueError("raw append payload uses a noncanonical encoding")
        snapshot = _read(path, MAX_LEDGER_BYTES, with_identity=True)
        data, identity, _ = snapshot
        if identity[3] != os.getuid():
            raise ValueError("raw delta file is not owned by the fixture operator")
        if len(data) != row["after_bytes"] or hashlib.sha256(data).hexdigest() != row["after_sha256"]:
            raise ValueError("raw delta does not bind the current ledger bytes")
        snapshots[path] = snapshot
        if relative in lock_paths:
            if (row["operation"] != "create_lock" or data or payload
                    or row["before_bytes"] != 0 or row["after_bytes"] != 0
                    or row["before_sha256"] != _EMPTY or row["after_sha256"] != _EMPTY):
                raise ValueError("lock creation must be the exact empty native lock")
            semantic[relative] = {"operation": "create_lock"}
            continue
        if row["operation"] != "append" or not payload:
            raise ValueError("native durable effect must append nonempty framed bytes")
        before = row["before_bytes"]
        if (before > len(data) or len(data) != before + len(payload)
                or data[before:] != payload
                or hashlib.sha256(data[:before]).hexdigest() != row["before_sha256"]):
            raise ValueError("append prefix, payload and current raw ledger disagree")
        # Decode the prior boundary as well: a byte offset inside a frame is
        # never accepted as an append even if the claimed suffix parses.
        decode_frames(data[:before])
        frames = decode_frames(payload)
        kinds = EVENT_KINDS if relative == _EVENTS else frozenset({
            "codex_wait_exhaustion_receipt" if relative == _EXHAUSTION else "timing_receipt"})
        rows = _decode_path_records(path, root.tenant_id, kinds, data)
        if relative == _EVENTS:
            validate_event_records(rows)
        _equal(rows[-len(frames):], frames, "native append frames")
        semantic[relative] = {"operation": "append", "frames": copy.deepcopy(frames)}
    return semantic, snapshots


def _unchanged(snapshots):
    for path, before in snapshots.items():
        if _read(path, MAX_LEDGER_BYTES, with_identity=True) != before:
            raise ValueError("durable source changed during its read-only proof")


def _artifact(artifact, command, statuses):
    if (type(artifact) is not dict or type(artifact.get("artifact_version")) is not int
            or artifact["artifact_version"] != 0 or artifact.get("command") != command
            or artifact.get("status") not in statuses or type(artifact.get("evidence")) is not dict):
        raise ValueError("stdout is not the expected native command artifact")
    _json(artifact)


def _fixed_arguments(case, home, workspace):
    # Exact vectors of bus-scale-native-entry-2026-10-09.py:arguments. These
    # source coordinates remain part of the producer's instrument witness.
    if case == "send":
        specification = importlib.util.spec_from_file_location("census_fixed_send_contract",
            _SOURCE / "tests/fixtures/bus_scale_census/bus-scale-native-entry-2026-10-09.py")
        entry = importlib.util.module_from_spec(specification)
        specification.loader.exec_module(entry)
        return entry.arguments(case, home, workspace)
    if case == "doctor":
        return ["doctor", "--root", str(home), "--source", str(_SOURCE), "--ref", "HEAD",
                "--no-sandbox", "--json"]
    raise ValueError("unsupported timing command")


def _timing(case, result, root, begin, end, child_cpu_seconds, workspace):
    path = "receipts/timings/" + case + ".jsonl"
    rows = result["delta"][path]["frames"]
    if len(rows) != 1 or rows[0]["kind"] != "timing_receipt":
        raise ValueError("command must append exactly one native timing row")
    row = rows[0]
    artifact = result["artifact"]
    _artifact(artifact, case, {"ok"} if case == "send" else {"ok", "degraded"})
    expected = dict(command=case, node_id="scale-worker" if case == "send" else None,
                    outcome=outcome_for_status(artifact["status"]),
                    argv_digest=argv_digest(_fixed_arguments(case, root.path, workspace), root.path))
    expected["refusal_code"] = refusal_code_for(expected["outcome"], artifact["evidence"])
    for key, value in expected.items():
        _equal(row[key], value, "timing " + key)
    finished = _stamp(row["timestamp"], begin, end)
    started = _stamp(row["started_at"], begin, end)
    if started > finished:
        raise ValueError("timing receipt precedes its native start")
    for key in ("cpu_seconds", "wall_seconds"):
        value = row[key]
        if (type(value) not in (int, float) or not math.isfinite(value) or value < 0
                or value != round(value, 3)):
            raise ValueError("timing duration must be finite, nonnegative native millisecond precision")
    if row["wall_seconds"] > (end - begin).total_seconds() + .001:
        raise ValueError("timing wall duration exceeds the retained invocation")
    if case == "send":
        sent = _stamp(artifact["evidence"]["message"]["timestamp"], begin, end)
        if not started <= sent <= finished:
            raise ValueError("timing interval does not enclose its atomic send")
    if child_cpu_seconds is not None:
        if (type(child_cpu_seconds) not in (int, float) or not math.isfinite(child_cpu_seconds)
                or child_cpu_seconds < 0 or row["cpu_seconds"] > child_cpu_seconds + .001):
            raise ValueError("timing CPU duration exceeds retained collected-child CPU")
    pointer = _coordinate("/delta", path) + "/frames/0"
    for key, value in (("id", "<generated-timing-id>"), ("timestamp", "<invocation-time>"),
                       ("started_at", "<invocation-start>"), ("wall_seconds", "<measured-wall-seconds>"),
                       ("cpu_seconds", "<measured-cpu-seconds>"), ("argv_digest", "<fixed-census-argv-digest>")):
        _replace(row, key, value, pointer, result["normalizations"],
                 "native timing schema, fixed argv and retained invocation bounds validated; raw metric retained")


def _send(result, root, begin, end, workspace):
    artifact = result["artifact"]
    _artifact(artifact, "send", {"ok"})
    rows = result["delta"][_EVENTS]["frames"]
    if len(rows) != 2 or [row["kind"] for row in rows] != ["message_envelope", "send_readiness_observation"]:
        raise ValueError("send must append its exact ordered envelope/readiness pair")
    envelope, companion = rows
    evidence = artifact["evidence"]
    _equal(evidence.get("message"), envelope, "send stdout envelope")
    _equal(evidence.get("recipient_readiness"), companion["readiness"], "send stdout readiness")
    arguments = _fixed_arguments("send", root.path, workspace)
    expected_sha = arguments[arguments.index("--sha") + 1]
    for key, value in dict(sender="scale-worker", recipient="architect", repo="floati", sha=expected_sha,
                           doc="note.md", idempotency_key="census-send",
                           attempt_binding="absent_legacy").items():
        _equal(envelope.get(key), value, "census send " + key)
    if "reply_to" in envelope or "worker_session_id" in envelope:
        raise ValueError("census send has an unrequested reply or worker-session binding")
    if "receipts/timings/send.jsonl" in result["delta"]:
        _equal(envelope["note"], "fixed selected census send", "fixed CLI send note")
    for row in rows:
        _stamp(row["timestamp"], begin, end)
    _equal(companion["envelope_id"], envelope["id"], "readiness envelope binding")
    _equal(companion["timestamp"], envelope["timestamp"], "atomic send timestamp")
    _equal(companion["observed_at"], envelope["timestamp"], "send observation clock")
    # The parent's fixed scratch is quiescent after collection. This native
    # reader performs only evidence reads; unlike observe(), it creates no
    # coordination locks and performs no parent mutation.
    own = RecipientReadiness._observe_already_guarded(
        root, "architect", observed_at=envelope["timestamp"]).artifact()
    _equal(companion["readiness"], own, "native recipient own-time readiness")
    # Preserve the complete native result above: Codex seed nodes can have
    # unknown hook provenance, while generic workers have absent mechanisms.
    # Neither state/reason is a volatile coordinate to normalize.
    audit = result["normalizations"]
    for obj, pointer, key, value in (
            (evidence["message"], "/artifact/evidence/message", "id", "<generated-message-id>"),
            (evidence["message"], "/artifact/evidence/message", "timestamp", "<invocation-time>"),
            (evidence["recipient_readiness"], "/artifact/evidence/recipient_readiness", "observed_at", "<invocation-time>"),
            (envelope, "/delta/events.jsonl/frames/0", "id", "<generated-message-id>"),
            (envelope, "/delta/events.jsonl/frames/0", "timestamp", "<invocation-time>"),
            (companion, "/delta/events.jsonl/frames/1", "id", "<generated-send-readiness-id>"),
            (companion, "/delta/events.jsonl/frames/1", "envelope_id", "<generated-message-id>"),
            (companion, "/delta/events.jsonl/frames/1", "timestamp", "<invocation-time>"),
            (companion, "/delta/events.jsonl/frames/1", "observed_at", "<invocation-time>"),
            (companion["readiness"], "/delta/events.jsonl/frames/1/readiness", "observed_at", "<invocation-time>")):
        _replace(obj, key, value, pointer, audit,
                 "native send pair, stdout binding and invocation clock validated")
    for index, fact in enumerate(own["mechanism_evidence"]):
        for source_index, source in enumerate(fact["sources"]):
            path = source["path"]
            if not path.startswith(str(root.path) + "/"):
                raise ValueError("native readiness source is outside the exact fixture")
            relative = path[len(str(root.path)) + 1:]
            if str(root.resolve_relative(relative)) != path:
                raise ValueError("readiness source coordinate is aliased")
            tail = "/mechanism_evidence/%d/sources/%d" % (index, source_index)
            for readiness, pointer in (
                    (evidence["recipient_readiness"], "/artifact/evidence/recipient_readiness"),
                    (companion["readiness"], "/delta/events.jsonl/frames/1/readiness")):
                target = readiness["mechanism_evidence"][index]["sources"][source_index]
                _replace(target, "path", _FIXTURE + "/" + relative, pointer + tail, audit,
                         "exact native fixture-bound readiness source coordinate validated")


def _exhaustion(result, begin, end):
    rows = result["delta"][_EXHAUSTION]["frames"]
    if len(rows) != 1 or rows[0]["kind"] != "codex_wait_exhaustion_receipt":
        raise ValueError("receipt append must contain exactly one exhaustion row")
    row = rows[0]
    _equal(result["artifact"], row, "exhaustion stdout row")
    for key, value in dict(node_id="scale-worker", session_digest=_SESSION, waited_seconds=2,
                           outcome="consent_reopened", idempotency_key="census-receipt").items():
        _equal(row[key], value, "census exhaustion " + key)
    _stamp(row["timestamp"], begin, end)
    for target, pointer in ((result["artifact"], "/artifact"),
                            (row, _coordinate("/delta", _EXHAUSTION) + "/frames/0")):
        for key, value in (("id", "<generated-exhaustion-id>"), ("timestamp", "<invocation-time>")):
            _replace(target, key, value, pointer, result["normalizations"],
                     "native exhaustion schema, exact census fields and invocation clock validated")


def normalize_durable_delta(case, delta, artifact, *, wall_begin, wall_end,
                            child_pid, home, workspace, child_cpu_seconds=None):
    """Project only send/receipt-append; preserve all unselected semantic fields."""
    try:
        if case not in {"send", "receipt-append"}:
            raise ValueError("unsupported durable census case")
        root, begin, end = _context(wall_begin, wall_end, child_pid, home, workspace)
        timing = "receipts/timings/send.jsonl"
        append = {_EVENTS, timing} if case == "send" else {_EXHAUSTION}
        locks = {path + ".lock" for path in append}
        if case == "send":
            locks.add("receipts/wake-coordination/architect/lane.lock")
        semantic, snapshots = _raw_projection(root, delta, append, locks)
        required = _EVENTS if case == "send" else _EXHAUSTION
        if required not in semantic or timing + ".lock" in semantic and timing not in semantic:
            raise ValueError("required native append is missing")
        result = dict(artifact=copy.deepcopy(artifact), delta=semantic, normalizations=[])
        _json(result)
        if case == "send":
            if timing in semantic:
                _timing("send", result, root, begin, end, child_cpu_seconds, workspace)
            _send(result, root, begin, end, workspace)
        else:
            _exhaustion(result, begin, end)
        _unchanged(snapshots)
        return result
    except (FloatiError, OSError, TypeError, KeyError, AttributeError, OverflowError,
            binascii.Error, UnicodeError) as exc:
        raise ValueError("native durable proof could not be established") from exc


def normalize_timing_delta(case, delta, artifact, *, wall_begin, wall_end,
                           child_pid, home, workspace, child_cpu_seconds=None):
    """Validate a timing-only doctor/send delta, leaving stdout unchanged.

    This is separate from a full command-effect proof: the parent must validate
    doctor stdout independently. Collected-child CPU is an optional upper bound;
    PID is retained context, since native timing rows themselves contain no PID.
    """
    try:
        if case not in {"send", "doctor"}:
            raise ValueError("unsupported timing census case")
        root, begin, end = _context(wall_begin, wall_end, child_pid, home, workspace)
        path = "receipts/timings/" + case + ".jsonl"
        semantic, snapshots = _raw_projection(root, delta, {path}, {path + ".lock"})
        if path not in semantic:
            raise ValueError("required native timing append is missing")
        result = dict(artifact=copy.deepcopy(artifact), delta=semantic, normalizations=[])
        _json(result)
        _timing(case, result, root, begin, end, child_cpu_seconds, workspace)
        _unchanged(snapshots)
        return result
    except (FloatiError, OSError, TypeError, KeyError, AttributeError, OverflowError,
            binascii.Error, UnicodeError) as exc:
        raise ValueError("native timing proof could not be established") from exc

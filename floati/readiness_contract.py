"""Closed, content-free contracts for send and native Stop testimony."""
from __future__ import annotations

import math
import re
from datetime import datetime
from pathlib import Path

MECHANISMS = ("lease", "stop_hook", "seat_board")
READINESS_REASONS = frozenset({
    "active_lease", "stop_hook", "seat_board", "all_mechanisms_absent",
    "mechanism_evidence_incomplete",
})


def validate_readiness(value, observed_at, refuse):
    from .records import _bounded_string, _enum, _terminal_unsafe
    if not isinstance(value, dict) or set(value) != {
        "state", "reason", "observed_at", "checked_mechanisms", "mechanism_evidence",
    }:
        refuse("readiness_invalid", "readiness must have the exact observation fields")
    if value["observed_at"] != observed_at:
        refuse("readiness_invalid", "readiness time differs from its observation")
    _enum(value["state"], {"recipient_wake_arranged", "recipient_not_listening", "unknown"}, "readiness.state", refuse)
    _enum(value["reason"], READINESS_REASONS, "readiness.reason", refuse)
    if value["checked_mechanisms"] != list(MECHANISMS):
        refuse("readiness_invalid", "new testimony must check the three mechanisms in order")
    evidence = value["mechanism_evidence"]
    if not isinstance(evidence, list) or len(evidence) != len(MECHANISMS):
        refuse("readiness_invalid", "readiness needs one fact per checked mechanism")
    for mechanism, fact in zip(MECHANISMS, evidence):
        if not isinstance(fact, dict) or set(fact) != {"mechanism", "state", "reason", "sources"}:
            refuse("readiness_invalid", "mechanism evidence has unexpected fields")
        if fact["mechanism"] != mechanism:
            refuse("readiness_invalid", "mechanism evidence is duplicated or reordered")
        _enum(fact["state"], {"arranged", "absent", "unknown"}, "mechanism.state", refuse)
        _bounded_string(fact["reason"], 1, 128, "mechanism.reason", refuse)
        if re.fullmatch(r"[a-z][a-z0-9_]{0,127}", fact["reason"]) is None:
            refuse("readiness_invalid", "mechanism reason must be a bounded code")
        sources = fact["sources"]
        if not isinstance(sources, list) or len(sources) > 16:
            refuse("readiness_invalid", "mechanism sources exceed their bound")
        for source in sources:
            if not isinstance(source, dict) or set(source) != {"path", "record_id"}:
                refuse("readiness_invalid", "source must name only path and receipt")
            _bounded_string(source["path"], 1, 4096, "source.path", refuse)
            if _terminal_unsafe(source["path"]):
                refuse("readiness_invalid", "source path is terminal-unsafe")
            if source["record_id"] is not None:
                _bounded_string(source["record_id"], 1, 512, "source.record_id", refuse)
                if _terminal_unsafe(source["record_id"]):
                    refuse("readiness_invalid", "source receipt is terminal-unsafe")
    states = [fact["state"] for fact in evidence]
    if value["state"] == "recipient_wake_arranged":
        witness = {"active_lease": 0, "stop_hook": 1, "seat_board": 2}.get(value["reason"])
        if witness is None or states[witness] != "arranged":
            refuse("readiness_invalid", "positive readiness lacks its named arrangement")
    elif value["state"] == "recipient_not_listening":
        if states != ["absent"] * 3 or value["reason"] != "all_mechanisms_absent":
            refuse("readiness_invalid", "negative readiness requires all affirmative absences")
    elif "arranged" in states or "unknown" not in states or value["reason"] != "mechanism_evidence_incomplete":
        refuse("readiness_invalid", "unknown readiness must retain unresolved evidence")


def validate_readiness_record(record, refuse):
    from .records import (
        _bounded_string, _enum, _identifier, _record_ref,
        _terminal_unsafe, _timestamp_value,
    )
    if record["kind"] == "send_readiness_observation":
        for field in ("sender", "recipient"):
            if not _identifier(record[field]):
                refuse("node_id_invalid", field + " must be an identifier")
        _record_ref(record["envelope_id"], "msg-", "envelope_id", refuse)
        _bounded_string(record["idempotency_key"], 1, 128, "idempotency_key", refuse)
        if _terminal_unsafe(record["idempotency_key"]):
            refuse("readiness_invalid", "observation key is terminal-unsafe")
        _timestamp_value(record["observed_at"], "observed_at", refuse)
        if record["timestamp"] != record["observed_at"]:
            refuse("readiness_invalid", "observation timestamp is not its frozen time")
        validate_readiness(record["readiness"], record["observed_at"], refuse)
        return
    if not _identifier(record["node_id"]):
        refuse("node_id_invalid", "node_id must be an identifier")
    if "holder_pid" in record:
        if type(record["holder_pid"]) is not int or not 0 < record["holder_pid"] < 2**31:
            refuse("stop_hold_invalid", "holder PID is outside its native bound")
        birth = record.get("process_start_epoch")
        if (type(birth) not in {int, float} or not 0 <= birth <= 253402300799
                or not math.isfinite(birth)):
            refuse("stop_hold_invalid", "holder process birth is not finite native testimony")
    for field, maximum in (("invocation_id", 128), ("acting_session_id", 512), ("workspace", 4096)):
        _bounded_string(record[field], 1, maximum, field, refuse)
        if _terminal_unsafe(record[field]):
            refuse("stop_hold_invalid", field + " is terminal-unsafe")
    if not Path(record["workspace"]).is_absolute():
        refuse("stop_hold_invalid", "hold workspace must be absolute")
    _enum(record["harness"], {"codex", "cursor", "zcode", "claude"}, "harness", refuse)
    _enum(record["phase"], {"start", "exit"}, "phase", refuse)
    _record_ref(record["consent_receipt_id"], "codex-wait-consent-", "consent_receipt_id", refuse)
    for field in ("timestamp", "started_at"):
        if not isinstance(record[field], str) or re.fullmatch(
            r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z", record[field]
        ) is None:
            refuse("stop_hold_invalid", field + " must use exact UTC milliseconds")
    _timestamp_value(record["started_at"], "started_at", refuse)
    seconds = record["deadline_seconds"]
    if not isinstance(seconds, (int, float)) or isinstance(seconds, bool) or not 0 < seconds <= 86400 or not math.isfinite(seconds):
        refuse("stop_hold_invalid", "hold deadline must be finite and within one day")
    if record["phase"] == "start" and record["timestamp"] != record["started_at"]:
        refuse("stop_hold_invalid", "start timestamp differs from the invocation start")
    start = datetime.fromisoformat(record["started_at"].replace("Z", "+00:00"))
    end = datetime.fromisoformat(record["timestamp"].replace("Z", "+00:00"))
    if end < start:
        refuse("stop_hold_invalid", "hold testimony predates its start")

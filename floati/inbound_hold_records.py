"""Closed version-one inbound-hold receipts: hold, release and per-item decision.

One ledger per held node, ``receipts/inbound-holds/<node>.jsonl``. Every row
carries the same closed field set (HOLD-1's shape plus the decision fields);
fields a phase does not use are null. Only the operator module
``floati.inbound_hold`` authors rows. A decision row and a release row are each
the receipt of a human act: a decision binds the exact envelope digest that
was rendered at the controlling terminal, and both bind the digest of the
rendered panel itself.
"""
from __future__ import annotations

from datetime import datetime
import re
import unicodedata

KIND = "inbound_hold_receipt"
PREFIX = "inbound-hold-"
COMMON = frozenset({"schema_version", "id", "tenant_id", "timestamp", "kind"})
FIELDS = COMMON | {
    "node_id", "phase", "hold_id", "reason", "started_at", "expected_release",
    "released_at", "message_id", "envelope_digest", "decided_at", "human_act",
    "idempotency_key",
}
PHASES = frozenset({"held", "released", "approved", "rejected"})
DECISIONS = frozenset({"approved", "rejected"})
HUMAN_ACT_FIELDS = frozenset({"channel", "rendered_digest"})
HUMAN_ACT_CHANNEL = "controlling_terminal"
INBOUND_HOLD_SPECS = {KIND: (PREFIX, frozenset(FIELDS))}
INBOUND_HOLD_KINDS = frozenset(INBOUND_HOLD_SPECS)

_UUID7 = r"[0-9a-f]{12}7[0-9a-f]{3}[89ab][0-9a-f]{15}"
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_MESSAGE = re.compile(r"msg-" + _UUID7 + r"\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


def _timestamp(value, field, error):
    if not isinstance(value, str) or not value.endswith("Z"):
        error("inbound_hold_time_invalid", field + " must be a UTC timestamp ending in Z")
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, OverflowError):
        error("inbound_hold_time_invalid", field + " must be a UTC timestamp ending in Z")
    if moment.tzinfo is None:
        error("inbound_hold_time_invalid", field + " must carry a UTC offset")


def _reference(value, prefix, field, error):
    if not isinstance(value, str) or re.fullmatch(re.escape(prefix) + _UUID7, value) is None:
        error("inbound_hold_reference_invalid", field + " must name one " + prefix + " record")


def _null(record, fields, error):
    for field in fields:
        if record[field] is not None:
            error("inbound_hold_phase_invalid", field + " does not belong to this phase")


def _human_act(act, error):
    if (not isinstance(act, dict) or frozenset(act) != HUMAN_ACT_FIELDS
            or act["channel"] != HUMAN_ACT_CHANNEL
            or not isinstance(act["rendered_digest"], str)
            or _SHA256.match(act["rendered_digest"]) is None):
        error("inbound_hold_human_act_invalid",
              "this act records the controlling-terminal confirmation and the rendered panel digest")


def validate_inbound_hold_record(record, tenant, error):
    """Strict authoring validation; readers stay tolerant in inbound_hold.py."""
    if not isinstance(record, dict) or frozenset(record) != FIELDS:
        error("record_fields_invalid", "inbound hold receipt has missing or extra fields")
    if type(record["schema_version"]) is not int or record["schema_version"] != 1:
        error("schema_version_invalid", "inbound hold receipts require integer version one")
    if record["kind"] != KIND:
        error("kind_invalid", "inbound hold receipt kind is fixed")
    if record["tenant_id"] != tenant:
        error("tenant_mismatch", "record tenant does not match the selected root")
    _reference(record["id"], PREFIX, "id", error)
    _timestamp(record["timestamp"], "timestamp", error)
    for field in ("node_id", "idempotency_key"):
        if not isinstance(record[field], str) or _IDENTIFIER.match(record[field]) is None:
            error("inbound_hold_identifier_invalid", field + " must be one bounded identifier")
    phase = record["phase"]
    if phase not in PHASES:
        error("inbound_hold_phase_invalid", "phase must be held, released, approved or rejected")
    if phase == "held":
        reason = record["reason"]
        if (not isinstance(reason, str) or not reason.strip()
                or any(unicodedata.category(c).startswith("C") for c in reason)
                or len(reason.encode("utf-8")) > 1024):
            error("inbound_hold_reason_invalid", "reason must be visible text within 1024 UTF-8 bytes")
        _timestamp(record["started_at"], "started_at", error)
        if record["expected_release"] is not None:
            _timestamp(record["expected_release"], "expected_release", error)
        _null(record, ("hold_id", "released_at", "message_id", "envelope_digest",
                       "decided_at", "human_act"), error)
        return dict(record)
    _reference(record["hold_id"], PREFIX, "hold_id", error)
    _null(record, ("reason", "started_at", "expected_release"), error)
    if phase == "released":
        _timestamp(record["released_at"], "released_at", error)
        _null(record, ("message_id", "envelope_digest", "decided_at"), error)
        _human_act(record["human_act"], error)
        return dict(record)
    _null(record, ("released_at",), error)
    if not isinstance(record["message_id"], str) or _MESSAGE.match(record["message_id"]) is None:
        error("inbound_hold_reference_invalid", "message_id must name one message envelope")
    if not isinstance(record["envelope_digest"], str) or _SHA256.match(record["envelope_digest"]) is None:
        error("inbound_hold_digest_invalid", "envelope_digest must be one lowercase SHA-256")
    _timestamp(record["decided_at"], "decided_at", error)
    _human_act(record["human_act"], error)
    return dict(record)

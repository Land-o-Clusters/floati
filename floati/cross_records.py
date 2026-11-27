"""Closed R7 semantic bodies that can draw on a reserved slot."""

from __future__ import annotations

import hashlib
import re
from datetime import datetime

from .cross_attended import verified_start
from .cross_coordinates import MAX_INTEGER, envelope_id, receipt_id, trust_step_head
from .cross_reserve import ACTIVATION_PRICE, COMPLETION_KINDS, COMPLETION_PRICE, PRICES, REPAIR_PRICE, completion_id, delivery_key
from .errors import ProtocolRefusal
from .framing import encode_frame
from .session_identity import IDENTIFIER_PATTERN, validate_session_id


_HEX16 = re.compile(r"[0-9a-f]{32}\Z")
_HEX32 = re.compile(r"[0-9a-f]{64}\Z")
_IDENT128 = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_UUID7 = r"[0-9a-f]{12}7[0-9a-f]{3}[89ab][0-9a-f]{15}\Z"
_LOCAL = re.compile("rmsg-" + _UUID7)
_HOLD = re.compile("inbound-hold-" + _UUID7)
_TIMESTAMP = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{3}Z\Z")
_BASE = {"schema_version", "id", "kind"}
_ENABLE = {"enable_id", "enable_generation"}
_PAIR_LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
_OPENING = {"ceremony_id", "opening_id", "ceremony_token", "pair_id"}
_PAIR_ACTIVATION = _BASE | _ENABLE | _OPENING | {
    "role", "label", "local_der_sha256", "peer_der", "peer_sha256", "peer_descriptor", "policy", "allowlist", "wake_list",
    "transcript_receipt_digest", "human_act",
}
_SCOPE = {"pair_id", "direction", "delivery_seq", "inbound_id", "envelope_digest"}
_COMPLETION_FIELDS = {
    "remote_presented": {"local_message_id", "recipient", "worker_session_id", "presented_at", "token_sha256"},
    "remote_local_ack": {"local_message_id", "recipient", "worker_session_id", "release_id"},
    "remote_receipt_confirmed": {"receipt_id", "receipt_digest"},
    "remote_hold_rejected": {"local_message_id", "recipient", "hold_id", "hold_digest", "decision_id", "decision_sha256"},
}


def _invalid():
    return ProtocolRefusal("cross_priced_record_invalid", "the record does not match its reserved R7 shape")


def _match(pattern, value):
    return isinstance(value, str) and pattern.fullmatch(value) is not None


def _within(value, start, end=MAX_INTEGER):
    return type(value) is int and start <= value <= end


def validate_pair_activation(body: dict) -> None:
    """Validate the closed attended activation body, before assigning storage coordinates."""
    if (set(body) != _PAIR_ACTIVATION or body["kind"] != "cross_pair_active"
        or not _within(body["schema_version"], 1, 1) or not _within(body["enable_generation"], 1)
        or not all(_match(_HEX16, body[key]) for key in _OPENING | {"enable_id"})
        or len({body["opening_id"], body["ceremony_id"], body["ceremony_token"]}) != 3
        or body["id"] != f"cross-pair-active-{body['pair_id']}" or body["role"] not in ("S", "T")
        or not _match(_PAIR_LABEL, body["label"]) or not _within(body["policy"], 19, 19)
        or not all(_match(_HEX32, body[key]) for key in ("local_der_sha256", "peer_sha256", "transcript_receipt_digest"))
        or body["local_der_sha256"] == body["peer_sha256"]):
        raise _invalid()
    try:
        der = bytes.fromhex(body["peer_der"])
    except (ValueError, TypeError) as exc:
        raise _invalid() from exc
    if not der or der.hex() != body["peer_der"] or hashlib.sha256(der).hexdigest() != body["peer_sha256"]:
        raise _invalid()
    peer = body["peer_descriptor"]
    if (not isinstance(peer, dict) or set(peer) != {"v", "enable_id", "salt", "label_hint", "lan", "ssh_launcher"}
        or not _within(peer["v"], 5, 5) or not _match(_HEX16, peer["enable_id"])
        or peer["enable_id"] == body["enable_id"] or not _match(_HEX16, peer["salt"])
        or not isinstance(peer["label_hint"], str) or len(peer["label_hint"]) > 256
        or not isinstance(peer["ssh_launcher"], str) or len(peer["ssh_launcher"]) > 512
        or not isinstance(peer["lan"], list) or len(peer["lan"]) > 16
        or any(not isinstance(address, str) or len(address) > 256 for address in peer["lan"])):
        raise _invalid()
    for nodes in (body["allowlist"], body["wake_list"]):
        if (not isinstance(nodes, list) or any(not _match(IDENTIFIER_PATTERN, node) for node in nodes)
            or len(set(nodes)) != len(nodes)):
            raise _invalid()
    if not set(body["wake_list"]).issubset(body["allowlist"]):
        raise _invalid()
    act = body["human_act"]
    if (not isinstance(act, dict) or set(act) != {"channel", "rendered_digest"}
        or act["channel"] != "controlling_terminal" or not _match(_HEX32, act["rendered_digest"])):
        raise _invalid()


def pair_seen_record(activation: dict) -> dict:
    return dict({key: activation[key] for key in _ENABLE | _OPENING | {"transcript_receipt_digest"}},
                schema_version=1, kind="cross_pair_seen", id=f"cross-pair-seen-{activation['pair_id']}",
                activation_id=activation["id"])


def validate_pair_revocation(activation: dict, revocation: dict) -> None:
    if (not _within(revocation.get("schema_version"), 1, 1)
        or any(revocation.get(key) != activation[key] for key in _ENABLE | {"pair_id"})):
        raise ProtocolRefusal("cross_pair_scope_refused", "the revocation must name the activation enable scope")


def _time(value):
    if not _match(_TIMESTAMP, value):
        return False
    try:
        datetime.fromisoformat(value[:-1])
    except ValueError:
        return False
    return True


def _complete_receipt(body):
    items, notices, checks, timings = (body[key] for key in ("items", "notices", "checks", "step_ms"))
    if not isinstance(items, list) or len(items) > 160 or not isinstance(notices, list) or len(notices) > 64:
        raise _invalid()
    if any(not isinstance(item, dict) or set(item) != {"item", "class", "cause"}
           or not _match(_IDENT128, item["item"]) or item["class"] != "confirmed_absent"
           or item["cause"] not in ("absent_on_recheck", "removed_and_verified") for item in items):
        raise _invalid()
    if any(not isinstance(notice, dict) or set(notice) != {"pair_id", "outcome"}
           or not _match(_HEX16, notice["pair_id"]) or notice["outcome"] not in ("notified", "UNKNOWN")
           for notice in notices):
        raise _invalid()
    for ordered in ([item["item"] for item in items], [notice["pair_id"] for notice in notices]):
        if ordered != sorted(set(ordered)):
            raise _invalid()
    if (not isinstance(checks, dict) or set(checks) != {"unit_absent", "unit_not_loadable", "no_listener", "no_live_endpoint"}
        or any(value is not True for value in checks.values())
        or not isinstance(timings, list) or len(timings) != 7
        or any(value is not None and not _within(value, 0) for value in [*timings, body["total_ms"]])):
        raise _invalid()


def _completion_id(body):
    kind = body["kind"]
    if set(body) != _BASE | _ENABLE | _SCOPE | _COMPLETION_FIELDS[kind] or body["schema_version"] != 2:
        raise _invalid()
    pair, direction, number, envelope = (body[key] for key in ("pair_id", "direction", "delivery_seq", "envelope_digest"))
    if not _match(_HEX32, envelope):
        raise _invalid()
    expected_inbound = f"remote-inbound-{body['enable_id']}-{envelope_id(pair, direction, number)}"
    if body["inbound_id"] != expected_inbound:
        raise _invalid()
    if kind == "remote_receipt_confirmed":
        if body["receipt_id"] != receipt_id(pair, direction, number, envelope) or not _match(_HEX32, body["receipt_digest"]):
            raise _invalid()
    else:
        if not _match(_LOCAL, body["local_message_id"]) or not _match(IDENTIFIER_PATTERN, body["recipient"]):
            raise _invalid()
        if kind in {"remote_presented", "remote_local_ack"}:
            validate_session_id(body["worker_session_id"])
        if kind == "remote_presented":
            if not _time(body["presented_at"]) or not _match(_HEX32, body["token_sha256"]):
                raise _invalid()
            return completion_id(body["token_sha256"], kind)
        if kind == "remote_local_ack" and not re.fullmatch(r"cross-completion-remote-presented-[0-9a-f]{64}",
                                                           str(body["release_id"])):
            raise _invalid()
        if kind == "remote_hold_rejected" and (
            not all(_match(_HOLD, body[key]) for key in ("hold_id", "decision_id"))
            or not all(_match(_HEX32, body[key]) for key in ("hold_digest", "decision_sha256"))
        ):
            raise _invalid()
    return completion_id(delivery_key(body["inbound_id"]), kind)


def priced_frame(body: dict, seq: int, prev: str, *, trust=None) -> bytes:
    """Validate a semantic body and assign its bounded, priced frame fields."""
    kind = body.get("kind")
    if not _within(seq, 1, 100000) or not _match(_HEX32, prev) or type(body.get("schema_version")) is not int:
        raise _invalid()
    if kind != "cross_journal_tail_repaired" and (
        not _match(_HEX16, body.get("enable_id")) or not _within(body.get("enable_generation"), 1)
    ):
        raise _invalid()
    version = body["schema_version"]
    maximum = COMPLETION_PRICE
    entry = dict(body, seq=seq, prev=prev)
    changes_trust = kind in {"cross_optin", "cross_pair_revoked", "cross_pair_active"}
    if changes_trust:
        if trust is None:
            raise _invalid()
        old_head, count = trust
        if not _match(_HEX32, old_head) or not _within(count, 1):
            raise _invalid()
        head = trust_step_head(body, old_head, count)
        entry.update(prev_trust_digest=old_head, trust_count=count, trust_head=head)
    if kind in COMPLETION_KINDS:
        identifier = _completion_id(body)
    elif kind == "cross_optin":
        phase, cause = body.get("phase"), body.get("cause")
        if version == 2:
            if phase == "enable_started":
                expected = verified_start(body)
                if body != expected:
                    raise _invalid()
            elif (phase != "enable_complete" or cause is not None
                or set(body) != _BASE | _ENABLE | {"phase", "cause", "token", "approval_sha256"}
                or not _match(_HEX16, body["token"]) or not _match(_HEX32, body["approval_sha256"])):
                raise _invalid()
            maximum = ACTIVATION_PRICE
        elif (version != 1 or set(body) != _BASE | _ENABLE | {"phase", "cause"} or (phase, cause) not in (
            ("disable_started", "local"), ("disable_started", "anchor_absent_reconstructed"), ("disable_complete", None)
        )):
            raise _invalid()
        identifier = f"cross-optin-{body['enable_id']}-{phase.replace('_', '-')}"
    elif kind == "cross_ceremony_closed":
        if (version != 2 or set(body) != _BASE | _ENABLE | {"ceremony_id", "ceremony_token", "cause"}
            or not all(_match(_HEX16, body[key]) for key in ("ceremony_id", "ceremony_token"))
            or body["cause"] not in ("abort", "crash", "disabled")):
            raise _invalid()
        identifier = f"cross-ceremony-closed-{body['ceremony_id']}"
    elif kind == "cross_pair_active":
        validate_pair_activation(body)
        identifier = f"cross-pair-active-{body['pair_id']}"
        maximum = ACTIVATION_PRICE
    elif kind == "cross_pair_revoked":
        if (version != 1 or set(body) != _BASE | _ENABLE | {"pair_id", "idempotency_key", "cause"}
            or not _match(_HEX16, body["pair_id"])):
            raise _invalid()
        cause, key = body["cause"], body["idempotency_key"]
        if (cause == "local" and not _match(_IDENT128, key)
            or cause != "local" and (cause not in ("disable", "key_absent_reconstructed") or key is not None)):
            raise _invalid()
        identifier = f"cross-pair-revoked-{body['pair_id']}"
    elif kind == "cross_teardown_receipt":
        if (version != 1 or set(body) != _BASE | _ENABLE | {"outcome", "items", "notices", "checks", "step_ms", "total_ms"}
            or body["outcome"] != "complete"):
            raise _invalid()
        _complete_receipt(body)
        identifier = f"cross-teardown-{body['enable_id']}-complete"
    elif kind == "cross_journal_tail_repaired":
        required = _BASE | {"offset", "tail_len", "tail_sha256", "complete_frames"}
        if (version != 0 or set(body) - {"completion_delivery"} != required
            or not str(body["id"]).startswith("cross-repair-")
            or not _within(body["offset"], 0, 64 * 1024 * 1024)
            or not _within(body["tail_len"], 1, 65536) or not _match(_HEX32, body["tail_sha256"])
            or not _within(body["complete_frames"], 0, 1)
            or ("completion_delivery" in body and not _match(_HEX32, body["completion_delivery"]))):
            raise _invalid()
        identifier = body["id"]
        maximum = REPAIR_PRICE
    else:
        raise _invalid()
    if body["id"] != identifier:
        raise _invalid()
    frame = encode_frame(entry)
    width = 6 - len(str(seq)) if kind == "cross_pair_active" else 0
    price = maximum if kind == "cross_optin" and version == 2 else PRICES.get(kind, maximum)
    if len(frame) + width > price:
        raise _invalid()
    return frame

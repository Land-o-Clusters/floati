"""Directional delivery projections and retained immutable commitments."""

from __future__ import annotations

import re

from .cross_coordinates import MAX_INTEGER, digest, enc, envelope_id, receipt_id
from .cross_reserve import inbound_result
from .errors import ProtocolRefusal
from .framing import encode_frame
from .session_identity import IDENTIFIER_PATTERN


_FIELDS = {
    "v", "pair_id", "direction", "sender_node", "recipient_node", "message_class", "kind",
    "reply_to", "repo", "sha", "doc", "note",
}


def request_commitment(request: dict) -> str:
    fields = request.get("envelope_fields")
    if (set(request) != {"verb", "envelope_fields"} or request["verb"] not in ("ask", "send")
        or not isinstance(fields, dict) or set(fields) != _FIELDS or fields["v"] != 5):
        raise ProtocolRefusal("cross_delivery_request_invalid", "the immutable outbound request is malformed")
    return digest(encode_frame(request)[:-1])


def next_sequence(floor: int, suffix) -> int:
    if type(floor) is not int or not 0 <= floor <= MAX_INTEGER:
        raise ProtocolRefusal("cross_delivery_history_lost", "the local delivery floor is malformed")
    sequence = floor
    values = list(suffix)
    if any(type(item) is not int for item in values):
        raise ProtocolRefusal("cross_delivery_history_lost", "the retained delivery suffix is malformed")
    for value in sorted(values):
        sequence += 1
        if value != sequence or value > MAX_INTEGER:
            raise ProtocolRefusal("cross_delivery_history_lost", "the retained delivery suffix is not contiguous")
    if sequence == MAX_INTEGER:
        raise ProtocolRefusal("cross_delivery_sequence_exhausted", "the local delivery sequence is exhausted")
    return sequence + 1


def retirable_through(floor: int, inbound_rows, completions, *, qualified_rejections=frozenset()) -> int:
    """Advance only across exact peer confirmations and checked local terminal outcomes."""
    confirmations = {}
    terminal = set()
    for row in completions:
        if row["kind"] == "remote_receipt_confirmed":
            confirmations[row.get("inbound_id")] = row
        elif row["kind"] == "remote_local_ack" or (
            row["kind"] == "remote_hold_rejected" and row["id"] in qualified_rejections
        ):
            terminal.add(row.get("inbound_id"))
    inbound_rows = [row for row in inbound_rows if type(row.get("delivery_seq")) is int]
    sequences = [row["delivery_seq"] for row in inbound_rows]
    if len(set(sequences)) != len(sequences):
        raise ProtocolRefusal("cross_delivery_history_lost", "receiver history repeats a delivery coordinate")
    for item in sorted(inbound_rows, key=lambda row: row["delivery_seq"]):
        if item["delivery_seq"] <= floor:
            continue
        if item["delivery_seq"] != floor + 1:
            break
        ack = confirmations.get(item["id"])
        keys = ("pair_id", "direction", "receipt_id", "receipt_digest")
        if (
            ack is None or any(not item.get(key) or item[key] != ack.get(key) for key in keys)
            or ack.get("delivery_seq", ack.get("wire_seq")) != item["delivery_seq"]
        ):
            break
        outcome = inbound_result(item)
        if outcome != "refused" and (outcome != "accepted" or item["id"] not in terminal):
            break
        floor += 1
    return floor


def sender_replay_id(scope: dict) -> str:
    return "cross-replay-" + digest(enc(b"floati-sender-key-v1"), enc(encode_frame(scope)[:-1]))


_SENDER_KEY = ("enable_id", "enable_generation", "pair_id", "direction", "sender_node", "idempotency_key")
_REPLAY_FIELDS = {"kind", "schema_version", "id", "verb", "request_sha256", "delivery_seq",
                  "envelope_id", "envelope_digest", "result", "receipt_digest"}
_HEX32 = re.compile(r"[0-9a-f]{32}\Z")
_HEX64 = re.compile(r"[0-9a-f]{64}\Z")
_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_MESSAGE = re.compile(r"rmsg-[0-9a-f]{12}7[0-9a-f]{3}[89ab][0-9a-f]{15}\Z")
_DURABLE_CODES = frozenset({"cross_node_not_allowed", "cross_answer_uncorrelated", "cross_envelope_invalid"})
_PRICE_RESULT = {"accepted": {"record_id": "rmsg-0190f0e1a2b37c4d8e9f0a1b2c3d4e5f"}}


def _replay_invalid():
    return ProtocolRefusal("cross_sender_replay_invalid", "the sender replay has no exact scoped commitment")


def _match(pattern, value):
    return isinstance(value, str) and pattern.fullmatch(value) is not None


def sender_scope(outbound: dict) -> dict:
    return {key: outbound["envelope"]["sender_node"] if key == "sender_node" else outbound[key]
            for key in _SENDER_KEY}


def sender_receipt(record: dict, result: dict) -> tuple:
    """Reconstruct the v5 receipt and its digest from the exact retired tuple."""
    pair, direction, number, envelope = (record[k] for k in ("pair_id", "direction", "delivery_seq", "envelope_digest"))
    if not isinstance(result, dict) or len(result) != 1:
        raise _replay_invalid()
    if "accepted" in result:
        accepted = result["accepted"]
        if not isinstance(accepted, dict) or set(accepted) != {"record_id"} or not _match(_MESSAGE, accepted["record_id"]):
            raise _replay_invalid()
    elif "refused" in result:
        refused = result["refused"]
        if (not isinstance(refused, dict) or set(refused) != {"code"}
            or not isinstance(refused["code"], str) or refused["code"] not in _DURABLE_CODES):
            raise _replay_invalid()
    else:
        raise _replay_invalid()
    body = {
        "v": 5, "pair_id": pair, "direction": direction, "seq": number,
        "envelope_id": envelope_id(pair, direction, number), "envelope_digest": envelope,
        "receipt_id": receipt_id(pair, direction, number, envelope), "result": result,
    }
    return body, digest(enc(b"floati-remote-receipt-v5"), enc(encode_frame(body)[:-1]))


def validate_sender_replay(row: dict) -> None:
    if (
        set(row) != set(_SENDER_KEY) | _REPLAY_FIELDS
        or row["kind"] != "remote_sender_replay" or type(row["schema_version"]) is not int or row["schema_version"] != 1
        or not _match(_HEX32, row["enable_id"]) or not _match(_HEX32, row["pair_id"])
        or type(row["enable_generation"]) is not int or not 1 <= row["enable_generation"] <= MAX_INTEGER
        or row["direction"] not in ("S-to-T", "T-to-S") or not _match(IDENTIFIER_PATTERN, row["sender_node"])
        or not _match(_KEY, row["idempotency_key"]) or row["verb"] not in ("ask", "send")
        or not _match(_HEX64, row["request_sha256"]) or not _match(_HEX64, row["envelope_digest"])
        or row["envelope_id"] != envelope_id(row["pair_id"], row["direction"], row["delivery_seq"])
        or row["id"] != sender_replay_id({key: row[key] for key in _SENDER_KEY})
    ):
        raise _replay_invalid()
    if row["result"] is None:
        if row["receipt_digest"] is not None:
            raise _replay_invalid()
    elif sender_receipt(row, row["result"])[1] != row["receipt_digest"]:
        raise _replay_invalid()


def sender_replay(outbound: dict, result) -> dict:
    scope = sender_scope(outbound)
    body = dict(scope, kind="remote_sender_replay", schema_version=1, id=sender_replay_id(scope),
                verb=outbound["verb"], request_sha256=outbound["request_sha256"],
                delivery_seq=outbound["delivery_seq"], envelope_id=outbound["envelope_id"],
                envelope_digest=outbound["envelope_digest"], result=result, receipt_digest=None)
    if result is not None:
        body["receipt_digest"] = sender_receipt(body, result)[1]
    validate_sender_replay(body)
    return body


def sender_replay_price(outbound: dict) -> int:
    frame = encode_frame(dict(sender_replay(outbound, _PRICE_RESULT), seq=100000, prev="f" * 64))
    return len(frame)


_OUTBOUND_FIELDS = {
    "kind", "schema_version", "id", "enable_id", "enable_generation", "pair_id", "direction",
    "delivery_seq", "envelope_id", "envelope", "envelope_digest", "verb", "idempotency_key",
    "request_sha256", "replay_retained", "replay_price",
}


def validate_outbound(row: dict) -> None:
    if (set(row) != _OUTBOUND_FIELDS or row["kind"] != "remote_outbound"
        or type(row["schema_version"]) is not int or row["schema_version"] != 2
        or type(row["replay_retained"]) is not bool or type(row["replay_price"]) is not int
        or not isinstance(row["envelope"], dict)):
        raise _replay_invalid()
    envelope = row["envelope"]
    fields = {key: value for key, value in envelope.items() if key not in {"seq", "envelope_id", "sender_timestamp"}}
    encoded = encode_frame(envelope)[:-1]
    if (row["request_sha256"] != request_commitment({"verb": row["verb"], "envelope_fields": fields})
        or row["envelope_digest"] != digest(enc(b"floati-remote-envelope-v5"), enc(encoded))
        or any(envelope.get(key) != row[key] for key in ("pair_id", "direction", "envelope_id"))
        or type(envelope.get("seq")) is not int or envelope["seq"] != row["delivery_seq"]
        or not isinstance(envelope.get("sender_timestamp"), str)
        or row["id"] != f"remote-outbound-{row['enable_id']}-{row['envelope_id']}"
        or row["replay_price"] != sender_replay_price(row)):
        raise _replay_invalid()

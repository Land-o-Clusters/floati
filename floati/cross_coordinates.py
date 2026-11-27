"""Byte-exact coordinates for the dark cross journal."""

from __future__ import annotations

import hashlib
import re
from datetime import date
from typing import Iterable, Mapping, Optional, Tuple

from .errors import IntegrityFailure, ProtocolRefusal
from .framing import encode_frame


MAX_INTEGER = 2**53 - 1
_HEX16 = re.compile(r"[0-9a-f]{32}\Z")
_HEX32 = re.compile(r"[0-9a-f]{64}\Z")
POSITION_FIELDS = frozenset({"seq", "prev", "prev_trust_digest", "trust_count", "trust_head"})
_TRUST_PHASES = frozenset({"enable_started", "enable_complete", "disable_started", "disable_complete"})
Trust = Tuple[str, int, int, str]


def enc(value: bytes) -> bytes:
    return len(value).to_bytes(4, "big") + value


def digest(*parts: bytes) -> str:
    return hashlib.sha256(b"".join(parts)).hexdigest()


def semantic(record: Mapping[str, object]) -> dict:
    return {key: value for key, value in record.items() if key not in POSITION_FIELDS}


def trust_coordinate(records: Iterable[Mapping[str, object]]) -> Optional[Trust]:
    """Fold only the live suffix of an ordered trust checkpoint."""
    coordinate = None
    rows = list(records)
    start = 0
    for index, row in enumerate(rows):
        if row["kind"] == "cross_trust_checkpoint" and row.get("schema_version") == 1:
            coordinate = row.get("enable_id"), row.get("enable_generation"), row.get("trust_count"), row.get("trust_head")
            enable_id, epoch, count, head = coordinate
            if (
                not isinstance(enable_id, str) or _HEX16.fullmatch(enable_id) is None
                or type(epoch) is not int or not 1 <= epoch <= MAX_INTEGER
                or type(count) is not int or not 1 <= count <= MAX_INTEGER
                or not isinstance(head, str) or _HEX32.fullmatch(head) is None
            ):
                raise IntegrityFailure("cross_journal_corrupt", "the ordered trust checkpoint is malformed")
            start = index + 1
    for row in rows[start:]:
        phase = row.get("phase")
        kind = row["kind"]
        if kind == "cross_optin" and phase == "enable_started":
            coordinate = trust_genesis(row)
        if coordinate is None or not (
            kind in {"cross_pair_active", "cross_pair_revoked"}
            or (kind == "cross_optin" and phase in _TRUST_PHASES)
        ):
            continue
        enable_id, epoch, count, previous = coordinate
        if count >= MAX_INTEGER:
            raise ProtocolRefusal("cross_trust_counter_exhausted", "the ordered trust counter is exhausted")
        count += 1
        head = trust_step_head(row, previous, count)
        if (POSITION_FIELDS - {"seq", "prev"}) & row.keys() and (
            row.get("prev_trust_digest") != previous or type(row.get("trust_count")) is not int
            or row["trust_count"] != count or row.get("trust_head") != head
        ):
            raise IntegrityFailure("cross_journal_corrupt", "the ordered trust fields do not match the sealed predecessor")
        coordinate = enable_id, epoch, count, head
    return coordinate


def trust_genesis(row: Mapping[str, object]) -> Optional[Trust]:
    enable_id, epoch = row.get("enable_id"), row.get("enable_generation")
    if (
        not isinstance(enable_id, str) or _HEX16.fullmatch(enable_id) is None
        or type(epoch) is not int or not 1 <= epoch <= MAX_INTEGER
    ):
        return None
    head = digest(enc(b"floati-trust-genesis-v6"), enc(bytes.fromhex(enable_id)), epoch.to_bytes(8, "big"))
    return enable_id, epoch, 0, head


def trust_step_head(row: Mapping[str, object], previous: str, count: int) -> str:
    return digest(enc(b"floati-trust-step-v6"), enc(bytes.fromhex(previous)), count.to_bytes(8, "big"),
                  enc(encode_frame(semantic(row))[:-1]))


def _directional_scope(pair_id: str, direction: str, seq: int, low: int = 1) -> None:
    if (
        not isinstance(pair_id, str) or _HEX16.fullmatch(pair_id) is None
        or direction not in ("S-to-T", "T-to-S")
        or type(seq) is not int or not low <= seq <= MAX_INTEGER
    ):
        raise ProtocolRefusal("cross_delivery_coordinate_invalid", "the directional delivery coordinate is invalid")


def envelope_id(pair_id: str, direction: str, seq: int) -> str:
    _directional_scope(pair_id, direction, seq)
    return "renv-" + digest(enc(b"floati-envelope-id-v5"), enc(bytes.fromhex(pair_id)),
                            enc(direction.encode("ascii")), seq.to_bytes(8, "big"))[:32]


def receipt_id(pair_id: str, direction: str, seq: int, envelope_digest: str) -> str:
    _directional_scope(pair_id, direction, seq)
    if not isinstance(envelope_digest, str) or _HEX32.fullmatch(envelope_digest) is None:
        raise ProtocolRefusal("cross_delivery_coordinate_invalid", "the envelope digest is invalid")
    return "rcpt-" + digest(enc(b"floati-receipt-id-v5"), enc(bytes.fromhex(pair_id)),
                            enc(direction.encode("ascii")), seq.to_bytes(8, "big"),
                            enc(bytes.fromhex(envelope_digest)))[:32]


def prune_digest(pair_id: str, direction: str, frontier: int, archive: bytes, exceptions: list) -> str:
    _directional_scope(pair_id, direction, frontier, low=0)
    body = dict(archive_sha256=digest(archive), direction=direction, exceptions=exceptions,
                frontier=frontier, pair_id=pair_id)
    return digest(enc(b"floati-prune-v6"), enc(encode_frame(body)[:-1]))


def legacy_quota_cutoff(observed_day: str, charged_days: Iterable[str], *, exclusive: bool) -> str:
    if not exclusive:
        raise ProtocolRefusal("cross_quota_cutoff_unproved", "legacy openings are not exclusively quiescent")
    try:
        days = [observed_day, *charged_days]
        if any(date.fromisoformat(day).isoformat() != day for day in days):
            raise ValueError("noncanonical civil date")
    except (TypeError, ValueError) as exc:
        raise ProtocolRefusal("cross_quota_cutoff_unproved", "the last possible UTC opening date is unknown") from exc
    return max(days)


def fold_quota_history(openings: Iterable[Tuple[str, str, str]]) -> Tuple[Optional[str], int]:
    """Fold authenticated charged openings, counting identical retained copies once."""
    identities = {}
    charges = {}
    for item in openings:
        if not isinstance(item, tuple) or len(item) != 3:
            raise ProtocolRefusal("cross_quota_history_unproved", "an authenticated quota charge is malformed")
        record_id, token, utc_day = item
        try:
            valid_day = date.fromisoformat(utc_day).isoformat() == utc_day
        except (TypeError, ValueError):
            valid_day = False
        if (not isinstance(record_id, str) or not record_id or not isinstance(token, str)
            or _HEX16.fullmatch(token) is None or not valid_day):
            raise ProtocolRefusal("cross_quota_history_unproved", "an authenticated quota charge is malformed")
        coordinate = token, utc_day
        if record_id in identities:
            if identities[record_id] != coordinate:
                raise ProtocolRefusal("cross_quota_history_unproved", "a retained quota charge changes its identity")
            continue
        identities[record_id] = coordinate
        charges[utc_day] = charges.get(utc_day, 0) + 1
        if charges[utc_day] > 32:
            raise ProtocolRefusal("cross_quota_history_unproved", "authenticated quota history exceeds its daily limit")
    day = max(charges, default=None)
    return day, charges[day] if day is not None else 0


def successor_quota_sha256(projection: Mapping[str, object]) -> str:
    """Commit to the exact outer quota projection and its source cut."""
    return digest(enc(b"floati-successor-quota-v1"), enc(encode_frame(projection)[:-1]))

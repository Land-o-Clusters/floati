"""Byte derivations for the version-three public comparison ceremony."""

from __future__ import annotations

from dataclasses import dataclass, field
from hashlib import sha256
import hmac
import json

from .cross_coordinates import MAX_INTEGER, enc


ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
POLICY = b"\x13"
_ALIASES = str.maketrans("OIL", "011")


def canonical(value: dict) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":"), allow_nan=False).encode("ascii")


def _hash(domain: bytes, *values: bytes) -> bytes:
    return sha256(enc(domain) + b"".join(map(enc, values))).digest()


@dataclass(frozen=True, repr=False)
class Contribution:
    leaf: bytes
    descriptor_digest: bytes
    nonce: bytes

    def opened(self) -> tuple:
        return self.leaf, self.descriptor_digest, self.nonce


def descriptor_digest(descriptor: dict) -> bytes:
    return sha256(enc(b"floati-desc-v3") + canonical(descriptor)).digest()


def commitment(ceremony_id: bytes, show: Contribution) -> bytes:
    return _hash(b"floati-pair-v3-commit", ceremony_id, POLICY, *show.opened())


def transcript(ceremony_id: bytes, show: Contribution, join: Contribution) -> bytes:
    return b"".join(map(enc, (b"floati-pair-v3", b"\x03", b"S", b"T", ceremony_id, POLICY, POLICY,
                              *show.opened(), *join.opened())))


def _crockford(value: bytes, width: int) -> str:
    integer = int.from_bytes(value, "big") >> (len(value) * 8 - width * 5)
    return "".join(ALPHABET[(integer >> (index * 5)) & 31] for index in reversed(range(width)))


@dataclass(frozen=True)
class Comparisons:
    t: bytes = field(repr=False)
    code_a: str = field(repr=False)
    code_b: str = field(repr=False)
    pair_id: str
    tag: str

    @property
    def display_a(self) -> str:
        return self.code_a[:4] + "-" + self.code_a[4:]


def comparisons(ceremony_id: bytes, show: Contribution, join: Contribution) -> Comparisons:
    t = sha256(enc(b"floati-pair-v3-transcript") + transcript(ceremony_id, show, join)).digest()
    return Comparisons(t, _crockford(_hash(b"floati-pair-v3-sas-A", t), 8),
                       _crockford(_hash(b"floati-pair-v3-sas-B", t), 4),
                       _hash(b"floati-pair-v3-id", t)[:16].hex(), _crockford(ceremony_id, 4))


def comparison_bytes(value: str, role: str):
    if role not in ("S", "T") or not isinstance(value, str) or not value.isascii():
        return None
    symbols = value.replace(" ", "").replace("-", "").upper().translate(_ALIASES)
    width = 8 if role == "T" else 4
    if len(symbols) != width or any(symbol not in ALPHABET for symbol in symbols):
        return None
    integer = 0
    for symbol in symbols:
        integer = (integer << 5) | ALPHABET.index(symbol)
    return integer.to_bytes((width * 5 + 7) // 8, "big")


def compare(value: str, expected: str, role: str) -> bool:
    entered = comparison_bytes(value, role)
    return entered is not None and hmac.compare_digest(entered, comparison_bytes(expected, role))


def gate_root(t: bytes, show_share: bytes, join_share: bytes) -> bytes:
    return _hash(b"floati-gate-root-v3", t, show_share, join_share)


def confirmation(root: bytes, role: str, t: bytes) -> bytes:
    return hmac.digest(root, enc(b"floati-pair-v3-confirm") + enc(role.encode("ascii")) + enc(t), "sha256")


def receipt_digest(t: bytes) -> bytes:
    return _hash(b"floati-pair-v3-receipt", t)


def panel_digest(template: str, t: bytes) -> str:
    return _hash(b"floati-pair-v3-panel", template.encode("ascii"), receipt_digest(t)).hex()


def direction_key(root: bytes, pair_id: bytes, direction: str) -> bytes:
    if direction not in ("S-to-T", "T-to-S") or len(pair_id) != 16:
        raise ValueError("invalid gate scope")
    return hmac.digest(root, enc(b"floati-gate-key-v3") + enc(pair_id) + enc(direction.encode("ascii")), "sha256")


def gate_prefix(key: bytes, counter: int) -> bytes:
    if type(counter) is not int or not 1 <= counter <= MAX_INTEGER:
        raise ValueError("invalid gate counter")
    prefix = b"\x03" + bytes(23) + counter.to_bytes(8, "big")
    return prefix + hmac.digest(key, enc(b"floati-gate-v3") + prefix, "sha256")

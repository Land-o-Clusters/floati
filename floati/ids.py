"""RFC 9562 UUIDv7 identifiers using only the Python standard library."""

from __future__ import annotations

import secrets
import time
import uuid
from typing import Optional


MAX_TIMESTAMP_MS = (1 << 48) - 1
MAX_RANDOM_BITS = (1 << 74) - 1


def uuid7_hex(
    *,
    timestamp_ms: Optional[int] = None,
    random_bits: Optional[int] = None,
) -> str:
    """Return a lowercase 32-hex UUIDv7 with RFC variant bits."""

    milliseconds = time.time_ns() // 1_000_000 if timestamp_ms is None else timestamp_ms
    randomness = secrets.randbits(74) if random_bits is None else random_bits
    if not isinstance(milliseconds, int) or isinstance(milliseconds, bool) or not 0 <= milliseconds <= MAX_TIMESTAMP_MS:
        raise ValueError("timestamp_ms must be a 48-bit non-negative integer")
    if not isinstance(randomness, int) or isinstance(randomness, bool) or not 0 <= randomness <= MAX_RANDOM_BITS:
        raise ValueError("random_bits must be a 74-bit non-negative integer")
    random_a = randomness >> 62
    random_b = randomness & ((1 << 62) - 1)
    value = (
        (milliseconds << 80)
        | (0x7 << 76)
        | (random_a << 64)
        | (0b10 << 62)
        | random_b
    )
    return uuid.UUID(int=value).hex

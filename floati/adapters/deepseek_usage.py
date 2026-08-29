"""DeepSeek usage decoder — B5F3 brief field-for-field.

Every field traces to docs/research/batch5-format-deepseek-2026-08-22.md
and the ratified contract drafts/adapters-deepseek/docs/CONTRACT.md.

DIALECT PIN (dual-dialect law): this decoder speaks the OpenAI-shape base
(https://api.deepseek.com) ONLY. The Anthropic-shape base (/anthropic) is
a separate future contract — never silently mixed.

WHAT MAKES THIS WIRE ITSELF, NOT A SIBLING CLONE:
- usage is REQUIRED; a response without one refuses;
- cache accounting is a REQUIRED hit/miss pair whose sum must equal
  prompt_tokens — a receipt-level checksum; violation refuses the row
  naming all three values;
- finish_reason insufficient_system_resource classes as infra_death,
  distinct from length/tool/content stops;
- cost is COMPUTED from tokens × price × TIME BAND (off-peak = half of
  peak); no cost field exists on the wire and none is modeled here;
- rate-limit headers are documented ABSENT on this backend and no header
  model exists here.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

DEEPSEEK_DIALECT = "openai-shape"
DEEPSEEK_BASE_URL = "https://api.deepseek.com"

_PEAK_WINDOWS_UTC = ((1, 4), (6, 10))  # [start hour, end hour) pairs


class DeepSeekUsageError(Exception):
    """Cause-named refusal vocabulary (law 12)."""
    pass


class DeepSeekMalformedResponse(DeepSeekUsageError):
    pass


class DeepSeekCacheIdentityViolation(DeepSeekUsageError):
    def __init__(self, hit: int, miss: int, prompt: int):
        self.hit = hit
        self.miss = miss
        self.prompt = prompt
        super().__init__(
            f"prompt_cache_hit_tokens({hit}) + prompt_cache_miss_tokens"
            f"({miss}) != prompt_tokens({prompt})"
        )


@dataclass(frozen=True)
class DeepSeekUsage:
    prompt_tokens: int
    prompt_cache_hit_tokens: int
    prompt_cache_miss_tokens: int
    completion_tokens: int
    total_tokens: int
    reasoning_tokens: Optional[int]

    @property
    def sum_identity_holds(self) -> bool:
        return (
            self.prompt_cache_hit_tokens + self.prompt_cache_miss_tokens
            == self.prompt_tokens
        )


def _require_int(obj: dict, key: str, where: str) -> int:
    val = obj.get(key)
    if not isinstance(val, int) or isinstance(val, bool):
        raise DeepSeekMalformedResponse(
            f"{where}.{key}: expected integer, got {type(val).__name__}"
        )
    return val


def _optional_int(obj: dict, key: str, where: str) -> Optional[int]:
    val = obj.get(key)
    if val is None:
        return None
    if not isinstance(val, int) or isinstance(val, bool):
        raise DeepSeekMalformedResponse(
            f"{where}.{key}: expected integer or null, "
            f"got {type(val).__name__}"
        )
    return val


def decode_chat_usage(data: bytes) -> DeepSeekUsage:
    """Decode a chat completions response object from raw JSON bytes."""
    try:
        root = json.loads(data)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise DeepSeekMalformedResponse(f"unparseable JSON: {exc}") from exc
    if not isinstance(root, dict):
        raise DeepSeekMalformedResponse("root is not an object")
    usage = root.get("usage")
    if not isinstance(usage, dict):
        raise DeepSeekMalformedResponse(
            "usage object missing or not an object "
            "(usage is REQUIRED on this wire)"
        )

    details_raw = usage.get("completion_tokens_details")
    reasoning = None
    if details_raw is not None:
        if not isinstance(details_raw, dict):
            raise DeepSeekMalformedResponse(
                "usage.completion_tokens_details: expected object"
            )
        reasoning = _optional_int(
            details_raw, "reasoning_tokens", "completion_tokens_details"
        )

    decoded = DeepSeekUsage(
        prompt_tokens=_require_int(usage, "prompt_tokens", "usage"),
        prompt_cache_hit_tokens=_require_int(
            usage, "prompt_cache_hit_tokens", "usage"
        ),
        prompt_cache_miss_tokens=_require_int(
            usage, "prompt_cache_miss_tokens", "usage"
        ),
        completion_tokens=_require_int(usage, "completion_tokens", "usage"),
        total_tokens=_require_int(usage, "total_tokens", "usage"),
        reasoning_tokens=reasoning,
    )
    if not decoded.sum_identity_holds:
        raise DeepSeekCacheIdentityViolation(
            hit=decoded.prompt_cache_hit_tokens,
            miss=decoded.prompt_cache_miss_tokens,
            prompt=decoded.prompt_tokens,
        )
    return decoded


def classify_finish_reason(reason: Optional[str]) -> str:
    """Ledger class for a terminal reason. Infrastructure death gets its
    own class — it is neither a content stop nor a refusal."""
    if reason == "insufficient_system_resource":
        return "infra_death"
    if reason in ("stop", "length", "tool_calls", "content_filter"):
        return reason
    if reason is None:
        return "absent"
    return "unknown"


def time_band(ts: datetime) -> str:
    """'peak' | 'off_peak' for an aware UTC timestamp.

    Peak windows are [01:00,04:00) and [06:00,10:00) UTC (half-open:
    window start inclusive). A naive timestamp REFUSES — the band is
    derived only from a real instant, never from local-clock guessing.
    """
    if ts.tzinfo is None:
        raise DeepSeekMalformedResponse(
            "time_band requires an aware timestamp (UTC offset present)"
        )
    utc = ts.astimezone(timezone.utc)
    for start, end in _PEAK_WINDOWS_UTC:
        if start <= utc.hour < end:
            return "peak"
    return "off_peak"

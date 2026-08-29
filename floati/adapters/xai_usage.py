"""xAI usage/billing decoder — B5F1 brief field-for-field.

Every field traces to docs/research/batch5-format-xai-2026-08-21.md.
Typed ABSENT stays ABSENT — the decoder never fills a slot because
the shape has one. Refusal vocabulary is cause-named.

GATE REPAIRS (f0636f3 refusal, findings 1-4):
- inner detail fields are Optional: a field xAI never reported is None,
  never a measured zero;
- present-but-wrong-typed values REFUSE, and a non-object details block
  refuses instead of downgrading silently to absent;
- the /v1/responses usage surface is decoded (brief sec2.2), so
  XaiResponsesUsage is constructed and covered, not dead;
- XaiCostMissing removed: cost is optional on both documented surfaces,
  so no lawful raising site exists — unused vocabulary is decoration.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Dict, Optional

TICKS_PER_USD = 10_000_000_000


class XaiUsageError(Exception):
    """Cause-named refusal vocabulary (law 12)."""
    pass


class XaiMalformedResponse(XaiUsageError):
    pass


def _ticks_to_usd_string(ticks: int) -> str:
    negative = ticks < 0
    magnitude = abs(ticks)
    whole = magnitude // TICKS_PER_USD
    remainder = magnitude % TICKS_PER_USD
    frac = str(remainder).zfill(10).rstrip("0") or "0"
    return ("-" if negative else "") + f"{whole}.{frac}"


@dataclass(frozen=True)
class XaiPromptTokensDetails:
    text_tokens: Optional[int]
    audio_tokens: Optional[int]
    image_tokens: Optional[int]
    cached_tokens: Optional[int]


@dataclass(frozen=True)
class XaiCompletionTokensDetails:
    reasoning_tokens: Optional[int]
    audio_tokens: Optional[int]
    accepted_prediction_tokens: Optional[int]
    rejected_prediction_tokens: Optional[int]


@dataclass(frozen=True)
class XaiChatUsage:
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    prompt_tokens_details: Optional[XaiPromptTokensDetails]
    completion_tokens_details: Optional[XaiCompletionTokensDetails]
    num_sources_used: Optional[int]
    cost_usd: Optional[str]  # MEASURED from ticks, integer-exact conversion

    @property
    def cost_is_measured(self) -> bool:
        return self.cost_usd is not None


@dataclass(frozen=True)
class XaiResponsesUsage:
    input_tokens: int
    output_tokens: int
    total_tokens: int
    input_tokens_details_cached: Optional[int]
    output_tokens_details_reasoning: Optional[int]
    context_details_input: Optional[int]
    context_details_output: Optional[int]
    cost_in_nano_usd: Optional[int]
    cost_in_usd_ticks: Optional[int]
    num_sources_used: Optional[int]
    num_server_side_tools_used: Optional[int]

    @property
    def cost_usd_ticks_string(self) -> Optional[str]:
        if self.cost_in_usd_ticks is None:
            return None
        return _ticks_to_usd_string(self.cost_in_usd_ticks)


def _require_int(obj: dict, key: str, where: str) -> int:
    val = obj.get(key)
    if not isinstance(val, int) or isinstance(val, bool):
        raise XaiMalformedResponse(
            f"{where}.{key}: expected integer, got {type(val).__name__}"
        )
    return val


def _optional_int(obj: dict, key: str, where: str) -> Optional[int]:
    """A field xAI never reported stays ABSENT (None). A field it
    reported wrong-typed (including boolean) REFUSES."""
    val = obj.get(key)
    if val is None:
        return None
    if not isinstance(val, int) or isinstance(val, bool):
        raise XaiMalformedResponse(f"{where}.{key}: expected integer or null")
    return val


def _details_object(raw: Any, where: str) -> Optional[dict]:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise XaiMalformedResponse(
            f"{where}: expected object or null, got {type(raw).__name__}"
        )
    return raw


def _prompt_details(raw: Any) -> Optional[XaiPromptTokensDetails]:
    details = _details_object(raw, "usage.prompt_tokens_details")
    if details is None:
        return None
    return XaiPromptTokensDetails(
        text_tokens=_optional_int(details, "text_tokens", "prompt_tokens_details"),
        audio_tokens=_optional_int(details, "audio_tokens", "prompt_tokens_details"),
        image_tokens=_optional_int(details, "image_tokens", "prompt_tokens_details"),
        cached_tokens=_optional_int(details, "cached_tokens", "prompt_tokens_details"),
    )


def _completion_details(raw: Any) -> Optional[XaiCompletionTokensDetails]:
    details = _details_object(raw, "usage.completion_tokens_details")
    if details is None:
        return None
    return XaiCompletionTokensDetails(
        reasoning_tokens=_optional_int(
            details, "reasoning_tokens", "completion_tokens_details"
        ),
        audio_tokens=_optional_int(
            details, "audio_tokens", "completion_tokens_details"
        ),
        accepted_prediction_tokens=_optional_int(
            details, "accepted_prediction_tokens", "completion_tokens_details"
        ),
        rejected_prediction_tokens=_optional_int(
            details, "rejected_prediction_tokens", "completion_tokens_details"
        ),
    )


def decode_chat_usage(data: bytes) -> XaiChatUsage:
    """Decode chat completions usage from raw JSON bytes."""
    try:
        root = json.loads(data)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise XaiMalformedResponse(f"unparseable JSON: {exc}") from exc
    if not isinstance(root, dict):
        raise XaiMalformedResponse("root is not an object")
    usage = root.get("usage")
    if not isinstance(usage, dict):
        raise XaiMalformedResponse("usage object missing")

    ticks = _optional_int(usage, "cost_in_usd_ticks", "usage")
    cost_usd = _ticks_to_usd_string(ticks) if ticks is not None else None

    return XaiChatUsage(
        prompt_tokens=_require_int(usage, "prompt_tokens", "usage"),
        completion_tokens=_require_int(usage, "completion_tokens", "usage"),
        total_tokens=_require_int(usage, "total_tokens", "usage"),
        prompt_tokens_details=_prompt_details(
            usage.get("prompt_tokens_details")
        ),
        completion_tokens_details=_completion_details(
            usage.get("completion_tokens_details")
        ),
        num_sources_used=_optional_int(usage, "num_sources_used", "usage"),
        cost_usd=cost_usd,
    )


def decode_responses_usage(data: bytes) -> XaiResponsesUsage:
    """Decode a /v1/responses usage object from raw JSON bytes (B5F1
    sec2.2): token totals, cached/reasoning details, latest-context
    accounting, both cost encodings, live-search and server-side-tool
    metering."""
    try:
        root = json.loads(data)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise XaiMalformedResponse(f"unparseable JSON: {exc}") from exc
    if not isinstance(root, dict):
        raise XaiMalformedResponse("root is not an object")
    usage = root.get("usage")
    if not isinstance(usage, dict):
        raise XaiMalformedResponse("usage object missing")

    input_details = _details_object(
        usage.get("input_tokens_details"), "usage.input_tokens_details"
    )
    output_details = _details_object(
        usage.get("output_tokens_details"), "usage.output_tokens_details"
    )
    context = _details_object(usage.get("context_details"), "usage.context_details")

    return XaiResponsesUsage(
        input_tokens=_require_int(usage, "input_tokens", "usage"),
        output_tokens=_require_int(usage, "output_tokens", "usage"),
        total_tokens=_require_int(usage, "total_tokens", "usage"),
        input_tokens_details_cached=(
            _optional_int(input_details, "cached_tokens", "input_tokens_details")
            if input_details
            else None
        ),
        output_tokens_details_reasoning=(
            _optional_int(
                output_details, "reasoning_tokens", "output_tokens_details"
            )
            if output_details
            else None
        ),
        context_details_input=(
            _optional_int(context, "input_tokens", "context_details")
            if context
            else None
        ),
        context_details_output=(
            _optional_int(context, "output_tokens", "context_details")
            if context
            else None
        ),
        cost_in_nano_usd=_optional_int(usage, "cost_in_nano_usd", "usage"),
        cost_in_usd_ticks=_optional_int(usage, "cost_in_usd_ticks", "usage"),
        num_sources_used=_optional_int(usage, "num_sources_used", "usage"),
        num_server_side_tools_used=_optional_int(
            usage, "num_server_side_tools_used", "usage"
        ),
    )


def encode_chat_usage(usage: XaiChatUsage) -> Dict[str, Any]:
    """Reverse: typed model → wire-format dict (for test round-trips)."""
    result: Dict[str, Any] = {
        "prompt_tokens": usage.prompt_tokens,
        "completion_tokens": usage.completion_tokens,
        "total_tokens": usage.total_tokens,
    }
    if usage.prompt_tokens_details:
        d = usage.prompt_tokens_details
        result["prompt_tokens_details"] = {
            k: v for k, v in (
                ("text_tokens", d.text_tokens),
                ("audio_tokens", d.audio_tokens),
                ("image_tokens", d.image_tokens),
                ("cached_tokens", d.cached_tokens),
            ) if v is not None
        }
    if usage.completion_tokens_details:
        d = usage.completion_tokens_details
        result["completion_tokens_details"] = {
            k: v for k, v in (
                ("reasoning_tokens", d.reasoning_tokens),
                ("audio_tokens", d.audio_tokens),
                ("accepted_prediction_tokens", d.accepted_prediction_tokens),
                ("rejected_prediction_tokens", d.rejected_prediction_tokens),
            ) if v is not None
        }
    if usage.num_sources_used is not None:
        result["num_sources_used"] = usage.num_sources_used
    return result

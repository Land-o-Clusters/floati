"""OpenAI usage decoder — B5F2 brief field-for-field.

Every field traces to docs/research/batch5-format-openai-2026-08-22.md
and the ratified contract drafts/adapters-openai/docs/CONTRACT.md.

THE TYPED ABSENCE THAT DEFINES THIS BACKEND: OpenAI carries NO wire cost
field. Cost is COMPUTED from pricing × echoed service_tier, never read.
The decoded types therefore have no cost slot at all — absence enforced
by shape, not by convention. `usage` itself is OPTIONAL on the completion
object; absent usage decodes to None, never zeros (interrupted streams
are documented to possibly never deliver it).

Rate-limit headers ARE documented for this backend (contrast xAI, where
header documentation is typed ABSENT) and decode as usage-context facts.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Dict, Mapping, Optional


class OpenAiUsageError(Exception):
    """Cause-named refusal vocabulary (law 12)."""
    pass


class OpenAiMalformedResponse(OpenAiUsageError):
    pass


@dataclass(frozen=True)
class OpenAiPromptTokensDetails:
    cached_tokens: Optional[int]
    cache_write_tokens: Optional[int]
    image_tokens: Optional[int]
    audio_tokens: Optional[int]
    text_tokens: Optional[int]


@dataclass(frozen=True)
class OpenAiCompletionTokensDetails:
    reasoning_tokens: Optional[int]
    accepted_prediction_tokens: Optional[int]
    rejected_prediction_tokens: Optional[int]
    audio_tokens: Optional[int]
    text_tokens: Optional[int]


@dataclass(frozen=True)
class OpenAiUsage:
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    prompt_tokens_details: Optional[OpenAiPromptTokensDetails]
    completion_tokens_details: Optional[OpenAiCompletionTokensDetails]


@dataclass(frozen=True)
class OpenAiCompletionEnvelope:
    usage: Optional[OpenAiUsage]
    service_tier: Optional[str]  # the ECHOED tier prices the request


@dataclass(frozen=True)
class OpenAiRateLimitHeaders:
    limit_requests: Optional[int]
    remaining_requests: Optional[int]
    limit_tokens: Optional[int]
    remaining_tokens: Optional[int]
    reset_requests: Optional[str]
    reset_tokens: Optional[str]
    request_id: Optional[str]
    project_limit_requests: Optional[str]
    project_remaining_requests: Optional[str]
    project_reset_requests: Optional[str]
    project_limit_tokens: Optional[str]
    project_remaining_tokens: Optional[str]
    project_reset_tokens: Optional[str]


def _require_int(obj: dict, key: str, where: str) -> int:
    val = obj.get(key)
    if not isinstance(val, int) or isinstance(val, bool):
        raise OpenAiMalformedResponse(
            f"{where}.{key}: expected integer, got {type(val).__name__}"
        )
    return val


def _optional_int(obj: dict, key: str) -> Optional[int]:
    val = obj.get(key)
    if val is None:
        return None
    if not isinstance(val, int) or isinstance(val, bool):
        raise OpenAiMalformedResponse(
            f"{key}: expected integer or null, got {type(val).__name__}"
        )
    return val


def _optional_str(value: Any, where: str) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str):
        raise OpenAiMalformedResponse(
            f"{where}: expected string or null, got {type(value).__name__}"
        )
    return value


def _prompt_details(raw: Any) -> Optional[OpenAiPromptTokensDetails]:
    if raw is None or not isinstance(raw, dict):
        return None
    return OpenAiPromptTokensDetails(
        cached_tokens=_optional_int(raw, "cached_tokens"),
        cache_write_tokens=_optional_int(raw, "cache_write_tokens"),
        image_tokens=_optional_int(raw, "image_tokens"),
        audio_tokens=_optional_int(raw, "audio_tokens"),
        text_tokens=_optional_int(raw, "text_tokens"),
    )


def _completion_details(raw: Any) -> Optional[OpenAiCompletionTokensDetails]:
    if raw is None or not isinstance(raw, dict):
        return None
    return OpenAiCompletionTokensDetails(
        reasoning_tokens=_optional_int(raw, "reasoning_tokens"),
        accepted_prediction_tokens=_optional_int(
            raw, "accepted_prediction_tokens"
        ),
        rejected_prediction_tokens=_optional_int(
            raw, "rejected_prediction_tokens"
        ),
        audio_tokens=_optional_int(raw, "audio_tokens"),
        text_tokens=_optional_int(raw, "text_tokens"),
    )


def decode_completion_usage(data: bytes) -> OpenAiCompletionEnvelope:
    """Decode a Chat Completions response object from raw JSON bytes."""
    try:
        root = json.loads(data)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise OpenAiMalformedResponse(f"unparseable JSON: {exc}") from exc
    if not isinstance(root, dict):
        raise OpenAiMalformedResponse("root is not an object")

    usage = None
    raw_usage = root.get("usage")
    if raw_usage is not None:
        if not isinstance(raw_usage, dict):
            raise OpenAiMalformedResponse(
                f"usage: expected object or null, got {type(raw_usage).__name__}"
            )
        usage = OpenAiUsage(
            prompt_tokens=_require_int(raw_usage, "prompt_tokens", "usage"),
            completion_tokens=_require_int(
                raw_usage, "completion_tokens", "usage"
            ),
            total_tokens=_require_int(raw_usage, "total_tokens", "usage"),
            prompt_tokens_details=_prompt_details(
                raw_usage.get("prompt_tokens_details")
            ),
            completion_tokens_details=_completion_details(
                raw_usage.get("completion_tokens_details")
            ),
        )

    return OpenAiCompletionEnvelope(
        usage=usage,
        service_tier=_optional_str(root.get("service_tier"), "service_tier"),
    )


_HEADER_MAP = {
    "limit_requests": "x-ratelimit-limit-requests",
    "remaining_requests": "x-ratelimit-remaining-requests",
    "limit_tokens": "x-ratelimit-limit-tokens",
    "remaining_tokens": "x-ratelimit-remaining-tokens",
}

_VERBATIM_HEADERS = {
    "reset_requests": "x-ratelimit-reset-requests",
    "reset_tokens": "x-ratelimit-reset-tokens",
    "request_id": "x-request-id",
    "project_limit_requests": "x-ratelimit-project-limit-requests",
    "project_remaining_requests": "x-ratelimit-project-remaining-requests",
    "project_reset_requests": "x-ratelimit-project-reset-requests",
    "project_limit_tokens": "x-ratelimit-project-limit-tokens",
    "project_remaining_tokens": "x-ratelimit-project-remaining-tokens",
    "project_reset_tokens": "x-ratelimit-project-reset-tokens",
}


def parse_rate_limit_headers(
    headers: Mapping[str, str],
) -> OpenAiRateLimitHeaders:
    """Documented rate-limit headers → typed facts.

    Count headers decode as integers (ledger-usable); reset windows and
    request ids keep their verbatim wire spelling.
    """
    lower = {k.lower(): v for k, v in headers.items()}
    counts: Dict[str, Optional[int]] = {}
    for field, header in _HEADER_MAP.items():
        raw = lower.get(header)
        if raw is None:
            counts[field] = None
            continue
        try:
            counts[field] = int(raw)
        except ValueError as exc:
            raise OpenAiMalformedResponse(
                f"{header}: expected integer, got {raw!r}"
            ) from exc
    verbatim: Dict[str, Optional[str]] = {
        field: lower.get(header)
        for field, header in _VERBATIM_HEADERS.items()
    }
    return OpenAiRateLimitHeaders(**counts, **verbatim)

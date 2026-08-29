"""Mistral usage decoder — B5F4 brief field-for-field.

Every field traces to docs/research/batch5-format-mistral-2026-08-22.md,
the published OpenAPI excerpt ($defs/UsageInfo family), and the ratified
contract drafts/adapters-mistral/docs/CONTRACT.md.

THE COMMON PATH IS EMPTINESS: UsageInfo fields are all default-0/nullable
and the published chat example shows "usage": {} — an all-default usage
object decodes to a typed ABSENT row (envelope.usage is None), never to
zeros wearing the clothes of a measurement.

DUAL CACHE COUNTERS, ONE AUTHORITY: num_cached_tokens (top level) and
prompt_tokens_details.cached_tokens report the same fact. The contract
pins the DETAILS object as authoritative; disagreement refuses the row at
decode time, naming both values.

completion_tokens is NULLABLE — unique across the Batch-5 backends.
No cost slot exists on any decoded type: Mistral marks wire cost ABSENT.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Dict, List, Optional


class MistralUsageError(Exception):
    """Cause-named refusal vocabulary (law 12)."""
    pass


class MistralMalformedResponse(MistralUsageError):
    pass


class MistralDualCounterDisagreement(MistralUsageError):
    def __init__(self, top_level: int, details: int):
        self.top_level = top_level
        self.details = details
        super().__init__(
            f"num_cached_tokens({top_level}) != "
            f"prompt_tokens_details.cached_tokens({details})"
        )


@dataclass(frozen=True)
class MistralMessageTokens:
    role: str
    total_tokens: Optional[int]
    truncated: bool
    usage_count: int


@dataclass(frozen=True)
class MistralPromptTokensDetails:
    cached_tokens: int
    audio_tokens: int
    messages: List[MistralMessageTokens]


@dataclass(frozen=True)
class MistralCompletionTokensDetails:
    reasoning_tokens: int


@dataclass(frozen=True)
class MistralUsageInfo:
    prompt_tokens: int = 0
    total_tokens: int = 0
    completion_tokens: Optional[int] = None
    num_cached_tokens: Optional[int] = None
    prompt_tokens_details: Optional[MistralPromptTokensDetails] = None
    completion_tokens_details: Optional[MistralCompletionTokensDetails] = None
    prompt_audio_seconds: Optional[int] = None
    request_count: Optional[int] = None

    def validated_cache_read(self) -> Optional[int]:
        """Details-object value is the pinned authority; the top-level
        counter corroborates. Only one side present still yields that side."""
        top = self.num_cached_tokens
        det = (
            self.prompt_tokens_details.cached_tokens
            if self.prompt_tokens_details
            else None
        )
        if det is not None:
            return det
        return top

    def is_all_default(self) -> bool:
        return (
            self.prompt_tokens == 0
            and self.total_tokens == 0
            and self.completion_tokens is None
            and self.num_cached_tokens is None
            and self.prompt_tokens_details is None
            and self.completion_tokens_details is None
            and self.prompt_audio_seconds is None
            and self.request_count is None
        )


@dataclass(frozen=True)
class MistralChatEnvelope:
    usage: Optional[MistralUsageInfo]


def _require_int(obj: dict, key: str, where: str) -> int:
    val = obj.get(key)
    if not isinstance(val, int) or isinstance(val, bool):
        raise MistralMalformedResponse(
            f"{where}.{key}: expected integer, got {type(val).__name__}"
        )
    return val


def _optional_int(obj: dict, key: str, where: str) -> Optional[int]:
    val = obj.get(key)
    if val is None:
        return None
    if not isinstance(val, int) or isinstance(val, bool):
        raise MistralMalformedResponse(
            f"{where}.{key}: expected integer or null, "
            f"got {type(val).__name__}"
        )
    return val


def _spec_int(raw: dict, key: str, where: str, default: int) -> int:
    """Spec-declared default (OpenAPI excerpt) applied ONLY when the key
    is absent; a present value must type-check — never silently coerced
    (inherited gate binding f0636f3 finding 2)."""
    if key not in raw:
        return default
    val = raw[key]
    if not isinstance(val, int) or isinstance(val, bool):
        raise MistralMalformedResponse(
            f"{where}.{key}: expected integer, got {type(val).__name__}"
        )
    return val


def _spec_bool(raw: dict, key: str, where: str, default: bool) -> bool:
    if key not in raw:
        return default
    val = raw[key]
    if not isinstance(val, bool):
        raise MistralMalformedResponse(
            f"{where}.{key}: expected boolean, got {type(val).__name__}"
        )
    return val


def _message_tokens(raw: Any) -> MistralMessageTokens:
    if not isinstance(raw, dict):
        raise MistralMalformedResponse(
            f"prompt_tokens_details.messages[]: expected object, "
            f"got {type(raw).__name__}"
        )
    role = raw.get("role")
    if not isinstance(role, str):
        raise MistralMalformedResponse(
            "messages[].role: required string, got "
            f"{type(role).__name__}"
        )
    return MistralMessageTokens(
        role=role,
        total_tokens=_optional_int(raw, "total_tokens", "messages[]"),
        truncated=_spec_bool(raw, "truncated", "messages[]", False),
        usage_count=_spec_int(raw, "usage_count", "messages[]", 1),
    )


def _prompt_details(raw: Any) -> Optional[MistralPromptTokensDetails]:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise MistralMalformedResponse(
            f"prompt_tokens_details: expected object, "
            f"got {type(raw).__name__}"
        )
    messages_raw = raw.get("messages") or []
    if not isinstance(messages_raw, list):
        raise MistralMalformedResponse(
            "prompt_tokens_details.messages: expected array"
        )
    return MistralPromptTokensDetails(
        cached_tokens=_spec_int(raw, "cached_tokens", "prompt_tokens_details", 0),
        audio_tokens=_spec_int(raw, "audio_tokens", "prompt_tokens_details", 0),
        messages=[_message_tokens(m) for m in messages_raw],
    )


def _completion_details(
    raw: Any,
) -> Optional[MistralCompletionTokensDetails]:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise MistralMalformedResponse(
            f"completion_tokens_details: expected object, "
            f"got {type(raw).__name__}"
        )
    return MistralCompletionTokensDetails(
        reasoning_tokens=_spec_int(
            raw, "reasoning_tokens", "completion_tokens_details", 0
        )
    )


def decode_chat_usage(data: bytes) -> MistralChatEnvelope:
    """Decode a chat completions response object from raw JSON bytes."""
    try:
        root = json.loads(data)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise MistralMalformedResponse(f"unparseable JSON: {exc}") from exc
    if not isinstance(root, dict):
        raise MistralMalformedResponse("root is not an object")

    raw_usage = root.get("usage")
    if raw_usage is None:
        return MistralChatEnvelope(usage=None)
    if not isinstance(raw_usage, dict):
        raise MistralMalformedResponse(
            f"usage: expected object or null, got {type(raw_usage).__name__}"
        )

    fields: Dict[str, Any] = {
        "prompt_tokens": raw_usage.get("prompt_tokens", 0),
        "total_tokens": raw_usage.get("total_tokens", 0),
        "completion_tokens": raw_usage.get("completion_tokens"),
        "num_cached_tokens": raw_usage.get("num_cached_tokens"),
        "prompt_audio_seconds": raw_usage.get("prompt_audio_seconds"),
        "request_count": raw_usage.get("request_count"),
    }
    for key in ("prompt_tokens", "total_tokens"):
        val = fields[key]
        if not isinstance(val, int) or isinstance(val, bool):
            raise MistralMalformedResponse(
                f"usage.{key}: expected integer, got {type(val).__name__}"
            )
    for key in ("completion_tokens", "num_cached_tokens",
                "prompt_audio_seconds", "request_count"):
        val = fields[key]
        if val is not None and (not isinstance(val, int) or isinstance(val, bool)):
            raise MistralMalformedResponse(
                f"usage.{key}: expected integer or null, "
                f"got {type(val).__name__}"
            )
    fields["prompt_tokens_details"] = _prompt_details(
        raw_usage.get("prompt_tokens_details")
    )
    fields["completion_tokens_details"] = _completion_details(
        raw_usage.get("completion_tokens_details")
    )

    usage = MistralUsageInfo(**fields)
    if usage.is_all_default():
        # The published example's "usage": {} shape: nothing was measured.
        return MistralChatEnvelope(usage=None)

    top = usage.num_cached_tokens
    det = (
        usage.prompt_tokens_details.cached_tokens
        if usage.prompt_tokens_details is not None
        else None
    )
    if top is not None and det is not None and top != det:
        raise MistralDualCounterDisagreement(top_level=top, details=det)

    return MistralChatEnvelope(usage=usage)

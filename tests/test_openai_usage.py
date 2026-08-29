"""OpenAI usage decoder conformance — B5F2 brief field-for-field.

Every wire field traces to docs/research/batch5-format-openai-2026-08-22.md
(puddle draft/b5-format-openai) and the ratified adapter contract
(drafts/adapters-openai/docs/CONTRACT.md @95aa34ee).

The load-bearing negative: NO cost field exists on this wire. The absence
is TYPED — the decoded types carry no cost slot at all, so the decoder
cannot grow one by accident of shape (B5 row binding: decoders must not
converge; xAI measures cost from ticks, OpenAI has nothing to measure).
"""

from __future__ import annotations

import dataclasses
import json
import unittest

from floati.adapters.openai_usage import (
    OpenAiCompletionEnvelope,
    OpenAiMalformedResponse,
    OpenAiRateLimitHeaders,
    OpenAiUsage,
    decode_completion_usage,
    parse_rate_limit_headers,
)


def completion_bytes(**overrides) -> bytes:
    body = {
        "id": "chatcmpl-1",
        "service_tier": "fast",
        "usage": {
            "prompt_tokens": 120,
            "completion_tokens": 48,
            "total_tokens": 168,
            "prompt_tokens_details": {
                "cached_tokens": 64,
                "cache_write_tokens": 16,
                "image_tokens": 0,
                "audio_tokens": 0,
                "text_tokens": 120,
            },
            "completion_tokens_details": {
                "reasoning_tokens": 12,
                "accepted_prediction_tokens": 3,
                "rejected_prediction_tokens": 7,
                "audio_tokens": 0,
                "text_tokens": 48,
            },
        },
    }
    body.update(overrides)
    return json.dumps(body).encode()


class TestOpenAiUsage(unittest.TestCase):
    def test_full_surface_decodes_field_for_field(self):
        envelope = decode_completion_usage(completion_bytes())
        self.assertIsNotNone(envelope.usage)
        usage = envelope.usage
        self.assertEqual(usage.prompt_tokens, 120)
        self.assertEqual(usage.completion_tokens, 48)
        self.assertEqual(usage.total_tokens, 168)
        self.assertIsNotNone(usage.prompt_tokens_details)
        self.assertIsNotNone(usage.completion_tokens_details)
        # cache WRITE side: billable class unique to OpenAI's wire — its own
        # line, never merged into the cached-read number.
        self.assertEqual(usage.prompt_tokens_details.cache_write_tokens, 16)
        self.assertEqual(usage.prompt_tokens_details.cached_tokens, 64)
        # rejected prediction tokens are billed-but-unusable: preserved as
        # its own line per the brief ("still counted ... for billing").
        self.assertEqual(
            usage.completion_tokens_details.rejected_prediction_tokens, 7
        )
        self.assertEqual(usage.completion_tokens_details.reasoning_tokens, 12)

    def test_absent_usage_is_typed_absent_not_zero(self):
        raw = json.dumps({"id": "chatcmpl-2"}).encode()
        envelope = decode_completion_usage(raw)
        self.assertIsNone(envelope.usage)
        self.assertIsNone(envelope.service_tier)

    def test_no_cost_slot_exists_on_any_decoded_type(self):
        """OpenAI carries NO wire cost field (contrast xAI ticks).

        The typed absence is structural: no cost-named field exists on any
        decoded type, so a cost cell cannot appear here by convergence.
        """
        for cls in (
            OpenAiCompletionEnvelope,
            OpenAiUsage,
            OpenAiRateLimitHeaders,
        ):
            names = {f.name for f in dataclasses.fields(cls)}
            self.assertFalse(
                any("cost" in name for name in names),
                f"{cls.__name__} grew a cost slot; the B5F2 brief marks "
                "wire cost ABSENT",
            )
        details = [
            f.type
            for f in dataclasses.fields(OpenAiUsage)
            if "details" in f.name
        ]
        self.assertEqual(len(details), 2)

    def test_service_tier_echo_preserved_verbatim(self):
        envelope = decode_completion_usage(completion_bytes())
        self.assertEqual(envelope.service_tier, "fast")

    def test_interrupted_stream_shape_still_decodes(self):
        raw = json.dumps({"id": "chatcmpl-3", "usage": None}).encode()
        envelope = decode_completion_usage(raw)
        self.assertIsNone(envelope.usage)

    def test_required_trio_missing_refuses(self):
        raw = json.dumps({"usage": {"total_tokens": 3}}).encode()
        with self.assertRaises(OpenAiMalformedResponse):
            decode_completion_usage(raw)

    def test_string_prompt_tokens_refuses(self):
        raw = json.dumps({"usage": {"prompt_tokens": "many"}}).encode()
        with self.assertRaises(OpenAiMalformedResponse):
            decode_completion_usage(raw)

    def test_boolean_as_integer_refuses(self):
        raw = json.dumps({"usage": {"prompt_tokens": True}}).encode()
        with self.assertRaises(OpenAiMalformedResponse):
            decode_completion_usage(raw)

    def test_non_object_usage_refuses(self):
        raw = json.dumps({"usage": 41}).encode()
        with self.assertRaises(OpenAiMalformedResponse):
            decode_completion_usage(raw)

    def test_malformed_json_refuses(self):
        with self.assertRaises(OpenAiMalformedResponse):
            decode_completion_usage(b"{not json")

    def test_rate_limit_headers_decode_as_facts(self):
        headers = parse_rate_limit_headers(
            {
                "x-ratelimit-limit-requests": "60",
                "x-ratelimit-remaining-requests": "59",
                "x-ratelimit-reset-requests": "1s",
                "x-ratelimit-limit-tokens": "100000",
                "x-ratelimit-remaining-tokens": "99832",
                "x-ratelimit-reset-tokens": "2m",
                "x-request-id": "req-1",
            }
        )
        self.assertEqual(headers.remaining_requests, 59)
        self.assertEqual(headers.limit_tokens, 100000)
        self.assertEqual(headers.reset_tokens, "2m")
        self.assertEqual(headers.request_id, "req-1")
        self.assertIsNone(headers.project_remaining_requests)

    def test_non_numeric_rate_limit_count_refuses(self):
        with self.assertRaises(OpenAiMalformedResponse):
            parse_rate_limit_headers({"x-ratelimit-limit-requests": "many"})


if __name__ == "__main__":
    unittest.main()

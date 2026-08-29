"""DeepSeek usage decoder conformance — B5F3 brief field-for-field.

Every wire field traces to docs/research/batch5-format-deepseek-2026-08-22.md
(puddle draft/b5-format-deepseek) and the ratified adapter contract
(drafts/adapters-deepseek/docs/CONTRACT.md @95aa34ee).

Load-bearing differences from the sibling backends (the formats differ
and the decoders must not converge):
- usage is REQUIRED here (OpenAI: optional; xAI: required) — a response
  without one REFUSES rather than rendering typed-absent;
- cache accounting is a REQUIRED hit/miss PAIR whose sum must equal
  prompt_tokens (receipt-level checksum; violation refuses the row);
- NO rate-limit headers exist on this wire (documented ABSENT, unlike
  OpenAI) and NO cost field (like OpenAI, unlike xAI);
- finish_reason insufficient_system_resource classes as infra-death,
  distinct from length/tool/content stops.
"""

from __future__ import annotations

import json
import unittest
from datetime import datetime, timezone

from floati.adapters.deepseek_usage import (
    DeepSeekCacheIdentityViolation,
    DeepSeekMalformedResponse,
    DeepSeekUsage,
    classify_finish_reason,
    decode_chat_usage,
    time_band,
)


def usage_bytes(**overrides) -> bytes:
    body = {
        "id": "dsk-1",
        "usage": {
            "prompt_tokens": 100,
            "prompt_cache_hit_tokens": 40,
            "prompt_cache_miss_tokens": 60,
            "completion_tokens": 25,
            "total_tokens": 125,
            "completion_tokens_details": {"reasoning_tokens": 7},
        },
        "finish_reason": "stop",
    }
    body["usage"].update(overrides.pop("usage", {}))
    body.update(overrides)
    return json.dumps(body).encode()


class TestDeepSeekUsage(unittest.TestCase):
    def test_full_surface_decodes_field_for_field(self):
        usage = decode_chat_usage(usage_bytes())
        self.assertEqual(usage.prompt_tokens, 100)
        self.assertEqual(usage.prompt_cache_hit_tokens, 40)
        self.assertEqual(usage.prompt_cache_miss_tokens, 60)
        self.assertEqual(usage.completion_tokens, 25)
        self.assertEqual(usage.total_tokens, 125)
        self.assertEqual(usage.reasoning_tokens, 7)

    def test_identity_holds_on_documented_shape(self):
        usage = decode_chat_usage(usage_bytes())
        self.assertTrue(usage.sum_identity_holds)

    def test_identity_violation_refuses_at_decode_naming_values(self):
        with self.assertRaises(DeepSeekCacheIdentityViolation) as caught:
            decode_chat_usage(usage_bytes(usage={
                "prompt_tokens": 100,
                "prompt_cache_hit_tokens": 30,
                "prompt_cache_miss_tokens": 50,
                "completion_tokens": 25,
                "total_tokens": 125,
            }))
        self.assertEqual(caught.exception.hit, 30)
        self.assertEqual(caught.exception.miss, 50)
        self.assertEqual(caught.exception.prompt, 100)

    def test_missing_cache_hit_refuses_required_pair(self):
        raw = json.dumps({"usage": {
            "prompt_tokens": 10,
            "prompt_cache_miss_tokens": 10,
            "completion_tokens": 2,
            "total_tokens": 12,
        }}).encode()
        with self.assertRaises(DeepSeekMalformedResponse):
            decode_chat_usage(raw)

    def test_missing_cache_miss_refuses_required_pair(self):
        raw = json.dumps({"usage": {
            "prompt_tokens": 10,
            "prompt_cache_hit_tokens": 0,
            "completion_tokens": 2,
            "total_tokens": 12,
        }}).encode()
        with self.assertRaises(DeepSeekMalformedResponse):
            decode_chat_usage(raw)

    def test_absent_usage_refuses(self):
        """Dialect difference pinned: usage is REQUIRED on this wire."""
        raw = json.dumps({"id": "dsk-2"}).encode()
        with self.assertRaises(DeepSeekMalformedResponse):
            decode_chat_usage(raw)

    def test_reasoning_details_optional(self):
        raw = json.dumps({"usage": {
            "prompt_tokens": 10,
            "prompt_cache_hit_tokens": 4,
            "prompt_cache_miss_tokens": 6,
            "completion_tokens": 2,
            "total_tokens": 12,
        }}).encode()
        usage = decode_chat_usage(raw)
        self.assertIsNone(usage.reasoning_tokens)

    def test_string_token_refuses(self):
        raw = json.dumps({"usage": {"prompt_tokens": "many"}}).encode()
        with self.assertRaises(DeepSeekMalformedResponse):
            decode_chat_usage(raw)

    def test_boolean_as_integer_refuses(self):
        with self.assertRaises(DeepSeekMalformedResponse):
            decode_chat_usage(usage_bytes(usage={"prompt_tokens": True}))

    def test_malformed_json_refuses(self):
        with self.assertRaises(DeepSeekMalformedResponse):
            decode_chat_usage(b"{not json")

    def test_no_cost_slot_and_no_rate_limit_model_exist(self):
        """Wire cost is ABSENT (computed, time-banded) and rate-limit
        headers are documented ABSENT — neither may converge into this
        decoder from its siblings."""
        import dataclasses

        from floati.adapters import deepseek_usage as d

        for name in dir(d):
            obj = getattr(d, name)
            if dataclasses.is_dataclass(obj) and isinstance(obj, type):
                names = {f.name for f in dataclasses.fields(obj)}
                self.assertFalse(
                    any("cost" in n for n in names),
                    f"{obj.__name__} grew a cost slot",
                )
                self.assertFalse(
                    any("ratelimit" in n.replace("_", "")
                        for n in names),
                    f"{obj.__name__} grew a rate-limit model",
                )


class TestInfraDeathClassification(unittest.TestCase):
    def test_insufficient_system_resource_is_infra_death(self):
        self.assertEqual(
            classify_finish_reason("insufficient_system_resource"),
            "infra_death",
        )

    def test_ordinary_stops_keep_their_own_classes(self):
        self.assertEqual(classify_finish_reason("stop"), "stop")
        self.assertEqual(classify_finish_reason("length"), "length")
        self.assertEqual(classify_finish_reason("tool_calls"), "tool_calls")

    def test_unknown_finish_reason_stays_typed_unknown(self):
        self.assertEqual(classify_finish_reason("wat"), "unknown")
        self.assertEqual(classify_finish_reason(None), "absent")


class TestTimeBand(unittest.TestCase):
    """Peak = [01:00,04:00) and [06:00,10:00) UTC; off-peak is HALF price,
    so the band recorded beside tokens decides whether a receipt's cost
    can be reproduced at all."""

    def test_peak_windows(self):
        for hour in (1, 2, 3, 6, 7, 9):
            ts = datetime(2026, 8, 22, hour, 30, tzinfo=timezone.utc)
            self.assertEqual(time_band(ts), "peak", f"{ts} should be peak")

    def test_off_peak_windows(self):
        for hour in (0, 4, 5, 10, 12, 23):
            ts = datetime(2026, 8, 22, hour, 30, tzinfo=timezone.utc)
            self.assertEqual(time_band(ts), "off_peak", f"{ts} off_peak")

    def test_window_start_boundaries_are_inclusive(self):
        for hour, minute, expected in (
            (1, 0, "peak"), (4, 0, "off_peak"),
            (6, 0, "peak"), (10, 0, "off_peak"),
        ):
            ts = datetime(2026, 8, 22, hour, minute, tzinfo=timezone.utc)
            self.assertEqual(time_band(ts), expected)

    def test_naive_timestamp_refuses_no_local_clock_guessing(self):
        with self.assertRaises(DeepSeekMalformedResponse):
            time_band(datetime(2026, 8, 22, 2, 0))


if __name__ == "__main__":
    unittest.main()

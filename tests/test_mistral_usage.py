"""Mistral usage decoder conformance — B5F4 brief field-for-field.

Every wire field traces to docs/research/batch5-format-mistral-2026-08-22.md
(puddle draft/b5-format-mistral), the published OpenAPI excerpt
(mistral-openapi-excerpt-UsageInfo.yaml @f36f46d7), and the ratified
adapter contract (drafts/adapters-mistral/docs/CONTRACT.md @95aa34ee).

Load-bearing negatives pinned here:
- empty/missing usage is the COMMON path and renders as typed ABSENT,
  never zeros;
- the dual cache counters are checked AT DECODE: disagreement refuses the
  row (details-object is the pinned authority);
- completion_tokens is NULLABLE — unique in Batch-5.
"""

from __future__ import annotations

import json
import unittest

from floati.adapters.mistral_usage import (
    MistralChatEnvelope,
    MistralDualCounterDisagreement,
    MistralMalformedResponse,
    decode_chat_usage,
)


def usage_bytes(**overrides) -> bytes:
    body = {
        "id": "cmpl-1",
        "model": "mistral-large-latest",
        "usage": {
            "prompt_tokens": 210,
            "total_tokens": 262,
            "completion_tokens": 52,
            "num_cached_tokens": 64,
            "prompt_tokens_details": {
                "cached_tokens": 64,
                "audio_tokens": 0,
                "messages": [
                    {
                        "role": "user",
                        "total_tokens": 210,
                        "truncated": False,
                        "usage_count": 1,
                    },
                    {
                        "role": "assistant",
                        "total_tokens": 180,
                        "truncated": True,
                        "usage_count": 2,
                    },
                ],
            },
            "completion_tokens_details": {"reasoning_tokens": 9},
            "prompt_audio_seconds": 0,
            "request_count": 1,
        },
    }
    body["usage"].update(overrides.pop("usage", {}))
    body.update(overrides)
    return json.dumps(body).encode()


class TestMistralUsage(unittest.TestCase):
    def test_full_surface_decodes_field_for_field(self):
        envelope = decode_chat_usage(usage_bytes())
        self.assertIsNotNone(envelope.usage)
        usage = envelope.usage
        self.assertEqual(usage.prompt_tokens, 210)
        self.assertEqual(usage.total_tokens, 262)
        self.assertEqual(usage.completion_tokens, 52)
        self.assertEqual(usage.num_cached_tokens, 64)
        self.assertEqual(usage.prompt_audio_seconds, 0)
        self.assertEqual(usage.request_count, 1)
        details = usage.prompt_tokens_details
        self.assertIsNotNone(details)
        self.assertEqual(details.cached_tokens, 64)
        self.assertEqual(details.audio_tokens, 0)
        self.assertEqual(len(details.messages), 2)

    def test_per_message_breakdown_surfaced_not_summarized(self):
        usage = decode_chat_usage(usage_bytes()).usage
        second = usage.prompt_tokens_details.messages[1]
        self.assertEqual(second.role, "assistant")
        self.assertEqual(second.total_tokens, 180)
        self.assertTrue(second.truncated)
        self.assertEqual(second.usage_count, 2)

    def test_empty_usage_object_is_typed_absent_not_zeros(self):
        """Published chat example shows "usage": {}; the contract rules the
        empty/all-default shape renders as an ABSENT row, never zeros."""
        envelope = decode_chat_usage(json.dumps({"usage": {}}).encode())
        self.assertIsNone(envelope.usage)

    def test_absent_usage_key_is_typed_absent(self):
        raw = json.dumps({"id": "cmpl-2"}).encode()
        envelope = decode_chat_usage(raw)
        self.assertIsNone(envelope.usage)

    def test_nullable_completion_tokens_preserved(self):
        raw = json.dumps({"usage": {
            "prompt_tokens": 10,
            "total_tokens": 10,
            "completion_tokens": None,
        }}).encode()
        usage = decode_chat_usage(raw).usage
        self.assertIsNone(usage.completion_tokens)

    def test_dual_counter_disagreement_refuses_at_decode(self):
        """Contract pins the details-object as authoritative; the top-level
        counter corroborates. Disagreement refuses the row AT DECODE."""
        with self.assertRaises(MistralDualCounterDisagreement) as caught:
            decode_chat_usage(usage_bytes(usage={
                "prompt_tokens": 100,
                "num_cached_tokens": 80,
                "prompt_tokens_details": {
                    "cached_tokens": 64, "audio_tokens": 0, "messages": [],
                },
            }))
        self.assertEqual(caught.exception.top_level, 80)
        self.assertEqual(caught.exception.details, 64)

    def test_agreement_yields_the_agreed_counter(self):
        """When counters agree (the only case that decodes with both
        present) every authority ordering returns the same value — the
        pin's observable content is the decode-time disagreement refusal,
        which test_dual_counter_disagreement_refuses_at_decode pins."""
        usage = decode_chat_usage(usage_bytes()).usage
        self.assertEqual(usage.validated_cache_read(), 64)

    def test_top_level_only_still_yields_cache_read(self):
        raw = json.dumps({"usage": {
            "prompt_tokens": 30,
            "total_tokens": 30,
            "num_cached_tokens": 12,
        }}).encode()
        usage = decode_chat_usage(raw).usage
        self.assertEqual(usage.validated_cache_read(), 12)

    def test_details_only_still_yields_cache_read(self):
        raw = json.dumps({"usage": {
            "prompt_tokens": 30,
            "total_tokens": 30,
            "prompt_tokens_details": {
                "cached_tokens": 12, "audio_tokens": 0, "messages": [],
            },
        }}).encode()
        usage = decode_chat_usage(raw).usage
        self.assertEqual(usage.validated_cache_read(), 12)

    def test_missing_required_message_role_refuses(self):
        """MessageTokens marks role REQUIRED; fabricating an empty role
        would hide a malformed wire."""
        raw = json.dumps({"usage": {
            "prompt_tokens": 5,
            "total_tokens": 5,
            "prompt_tokens_details": {
                "cached_tokens": 0,
                "audio_tokens": 0,
                "messages": [{"total_tokens": 5}],
            },
        }}).encode()
        with self.assertRaises(MistralMalformedResponse):
            decode_chat_usage(raw)

    def test_string_token_refuses(self):
        raw = json.dumps({"usage": {"prompt_tokens": "many"}}).encode()
        with self.assertRaises(MistralMalformedResponse):
            decode_chat_usage(raw)

    def test_boolean_as_integer_refuses(self):
        raw = json.dumps({"usage": {"prompt_tokens": True}}).encode()
        with self.assertRaises(MistralMalformedResponse):
            decode_chat_usage(raw)

    def test_non_integer_truncated_flag_refuses(self):
        raw = json.dumps({"usage": {
            "prompt_tokens": 5,
            "total_tokens": 5,
            "prompt_tokens_details": {
                "cached_tokens": 0,
                "audio_tokens": 0,
                "messages": [
                    {"role": "user", "truncated": "yes"},
                ],
            },
        }}).encode()
        with self.assertRaises(MistralMalformedResponse):
            decode_chat_usage(raw)

    def test_wrong_typed_inner_cached_tokens_refuses(self):
        """Inherited gate binding (f0636f3 finding 2): spec defaults apply
        when a key is ABSENT; a PRESENT wrong-typed value refuses."""
        raw = json.dumps({"usage": {
            "prompt_tokens": 10,
            "total_tokens": 10,
            "prompt_tokens_details": {
                "cached_tokens": "many", "audio_tokens": 0, "messages": [],
            },
        }}).encode()
        with self.assertRaises(MistralMalformedResponse):
            decode_chat_usage(raw)

    def test_wrong_typed_inner_reasoning_tokens_refuses(self):
        raw = json.dumps({"usage": {
            "prompt_tokens": 10,
            "total_tokens": 10,
            "completion_tokens": 2,
            "completion_tokens_details": {"reasoning_tokens": None},
        }}).encode()
        with self.assertRaises(MistralMalformedResponse):
            decode_chat_usage(raw)

    def test_wrong_typed_usage_count_refuses(self):
        raw = json.dumps({"usage": {
            "prompt_tokens": 5,
            "total_tokens": 5,
            "prompt_tokens_details": {
                "cached_tokens": 0,
                "audio_tokens": 0,
                "messages": [
                    {"role": "user", "usage_count": "twice"},
                ],
            },
        }}).encode()
        with self.assertRaises(MistralMalformedResponse):
            decode_chat_usage(raw)

    def test_spec_defaults_apply_only_when_key_absent(self):
        """Spec-declared defaults (OpenAPI excerpt: cached_tokens 0,
        truncated false, usage_count 1) are wire contract, not invention
        — applied on absence, never substituted for a wrong value."""
        raw = json.dumps({"usage": {
            "prompt_tokens": 7,
            "total_tokens": 9,
            "completion_tokens": 2,
            "prompt_tokens_details": {
                "messages": [{"role": "user", "total_tokens": 7}],
            },
        }}).encode()
        usage = decode_chat_usage(raw).usage
        details = usage.prompt_tokens_details
        self.assertEqual(details.cached_tokens, 0)
        msg = details.messages[0]
        self.assertFalse(msg.truncated)
        self.assertEqual(msg.usage_count, 1)

    def test_malformed_json_refuses(self):
        with self.assertRaises(MistralMalformedResponse):
            decode_chat_usage(b"{not json")

    def test_no_cost_slot_exists_on_any_decoded_type(self):
        """B5F4 brief marks wire cost ABSENT (dashboard-only spend)."""
        import dataclasses

        from floati.adapters import mistral_usage as m

        for name in dir(m):
            obj = getattr(m, name)
            if dataclasses.is_dataclass(obj) and isinstance(obj, type):
                names = {f.name for f in dataclasses.fields(obj)}
                self.assertFalse(
                    any("cost" in n for n in names),
                    f"{obj.__name__} grew a cost slot; the B5F4 brief "
                    "marks wire cost ABSENT",
                )


if __name__ == "__main__":
    unittest.main()

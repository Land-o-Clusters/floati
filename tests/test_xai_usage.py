from __future__ import annotations

import json
import unittest

from floati.adapters.xai_usage import (
    XaiChatUsage,
    XaiMalformedResponse,
    decode_chat_usage,
    decode_responses_usage,
)


def chat_usage_bytes(**overrides) -> bytes:
    base = {
        "prompt_tokens": 32,
        "completion_tokens": 9,
        "total_tokens": 41,
        "prompt_tokens_details": {
            "text_tokens": 32, "audio_tokens": 0,
            "image_tokens": 0, "cached_tokens": 6,
        },
        "completion_tokens_details": {
            "reasoning_tokens": 5, "audio_tokens": 0,
            "accepted_prediction_tokens": 0, "rejected_prediction_tokens": 0,
        },
        "num_sources_used": 0,
        "cost_in_usd_ticks": 15000000,
    }
    base.update(overrides)
    return json.dumps(base).encode()


class TestXaiChatUsage(unittest.TestCase):
    def test_round_trip(self):
        raw = chat_usage_bytes()
        usage = decode_chat_usage(json.dumps({"usage": json.loads(raw)}).encode())
        self.assertEqual(usage.prompt_tokens, 32)
        self.assertEqual(usage.completion_tokens, 9)
        self.assertEqual(usage.total_tokens, 41)
        self.assertIsNotNone(usage.prompt_tokens_details)
        self.assertEqual(usage.prompt_tokens_details.cached_tokens, 6)

    def test_absent_optional_fields_are_none(self):
        raw = json.dumps({"usage": {
            "prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3,
        }}).encode()
        usage = decode_chat_usage(raw)
        self.assertIsNone(usage.prompt_tokens_details)
        self.assertIsNone(usage.completion_tokens_details)
        self.assertIsNone(usage.cost_usd)  # typed absence — NOT zero

    def test_malformed_json_raises(self):
        with self.assertRaises(XaiMalformedResponse):
            decode_chat_usage(b"{not json")

    def test_missing_usage_raises(self):
        with self.assertRaises(XaiMalformedResponse):
            decode_chat_usage(b'{"choices": []}')

    def test_non_integer_prompt_tokens_raises(self):
        raw = json.dumps({"prompt_tokens": "many", "total_tokens": 3}).encode()
        with self.assertRaises(XaiMalformedResponse):
            decode_chat_usage(raw)

    def test_boolean_reaches_the_integer_validator(self):
        """REPAIR of gate finding 3: the old test posted prompt_tokens
        without a usage key and died at the missing-usage guard first.
        All sibling required fields are supplied so the ONLY possible
        refusal source is the boolean check itself."""
        raw = json.dumps({
            "usage": {
                "prompt_tokens": True,
                "completion_tokens": 2,
                "total_tokens": 3,
            }
        }).encode()
        with self.assertRaises(XaiMalformedResponse):
            decode_chat_usage(raw)

    def test_absent_inner_field_is_none_not_measured_zero(self):
        """REPAIR of gate finding 1: a field xAI never reported stays
        ABSENT — it must not become a measured zero in a burn column."""
        raw = json.dumps({"usage": {
            "prompt_tokens": 10,
            "completion_tokens": 3,
            "total_tokens": 13,
            "prompt_tokens_details": {"text_tokens": 10},
        }}).encode()
        usage = decode_chat_usage(raw)
        details = usage.prompt_tokens_details
        self.assertIsNotNone(details)
        self.assertEqual(details.text_tokens, 10)
        self.assertIsNone(details.cached_tokens)
        self.assertIsNone(details.audio_tokens)
        self.assertIsNone(details.image_tokens)

    def test_wrong_typed_inner_field_refuses(self):
        raw = json.dumps({"usage": {
            "prompt_tokens": 10,
            "completion_tokens": 3,
            "total_tokens": 13,
            "prompt_tokens_details": {"cached_tokens": "many"},
        }}).encode()
        with self.assertRaises(XaiMalformedResponse):
            decode_chat_usage(raw)

    def test_non_dict_details_object_refuses_not_silently_absent(self):
        """REPAIR of gate finding 2: a wrong-typed details object must not
        downgrade to absent."""
        raw = json.dumps({"usage": {
            "prompt_tokens": 10,
            "completion_tokens": 3,
            "total_tokens": 13,
            "prompt_tokens_details": "cached",
        }}).encode()
        with self.assertRaises(XaiMalformedResponse):
            decode_chat_usage(raw)


class TestXaiResponsesUsage(unittest.TestCase):
    def test_full_surface_constructs_every_brief_field(self):
        raw = json.dumps({"usage": {
            "input_tokens": 500,
            "output_tokens": 120,
            "total_tokens": 620,
            "input_tokens_details": {"cached_tokens": 200},
            "output_tokens_details": {"reasoning_tokens": 40},
            "context_details": {
                "input_tokens": 480, "output_tokens": 140,
            },
            "cost_in_nano_usd": 1500,
            "cost_in_usd_ticks": 15_000_000_000,
            "num_sources_used": 4,
            "num_server_side_tools_used": 2,
        }}).encode()
        usage = decode_responses_usage(raw)
        self.assertEqual(usage.input_tokens, 500)
        self.assertEqual(usage.output_tokens, 120)
        self.assertEqual(usage.total_tokens, 620)
        self.assertEqual(usage.input_tokens_details_cached, 200)
        self.assertEqual(usage.output_tokens_details_reasoning, 40)
        self.assertEqual(usage.context_details_input, 480)
        self.assertEqual(usage.context_details_output, 140)
        self.assertEqual(usage.cost_in_nano_usd, 1500)
        self.assertEqual(usage.cost_in_usd_ticks, 15_000_000_000)
        self.assertEqual(usage.num_sources_used, 4)
        self.assertEqual(usage.num_server_side_tools_used, 2)
        # integer-exact tick law, unchanged by the repair
        self.assertEqual(usage.cost_usd_ticks_string, "1.5")

    def test_absent_optionals_are_none(self):
        raw = json.dumps({"usage": {
            "input_tokens": 5, "output_tokens": 2, "total_tokens": 7,
        }}).encode()
        usage = decode_responses_usage(raw)
        self.assertIsNone(usage.cost_in_usd_ticks)
        self.assertIsNone(usage.num_server_side_tools_used)
        self.assertIsNone(usage.context_details_input)

    def test_nullable_costs_stay_null_typed(self):
        raw = json.dumps({"usage": {
            "input_tokens": 5, "output_tokens": 2, "total_tokens": 7,
            "cost_in_nano_usd": None, "cost_in_usd_ticks": None,
        }}).encode()
        usage = decode_responses_usage(raw)
        self.assertIsNone(usage.cost_in_nano_usd)
        self.assertIsNone(usage.cost_in_usd_ticks)

    def test_boolean_input_tokens_refuses(self):
        raw = json.dumps({"usage": {
            "input_tokens": True, "output_tokens": 2, "total_tokens": 3,
        }}).encode()
        with self.assertRaises(XaiMalformedResponse):
            decode_responses_usage(raw)


if __name__ == "__main__":
    unittest.main()

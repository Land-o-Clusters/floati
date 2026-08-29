from __future__ import annotations

import json
import unittest
from pathlib import Path

try:
    from floati.adapters.acp import ACPAdapter, ACPRefusal, MAXIMUM_ACP_LINE_BYTES, probe_reference_harness
except (ImportError, ModuleNotFoundError):
    ACPAdapter = None
    ACPRefusal = ValueError
    MAXIMUM_ACP_LINE_BYTES = None
    probe_reference_harness = None


FIXTURES = Path("tests/fixtures/acp")


class ACPAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.assertIsNotNone(ACPAdapter, "ACP v0 fixture codec must exist")
        self.adapter = ACPAdapter()

    def test_initialize_request_and_response_round_trip_semantically(self) -> None:
        for name, category in (
            ("initialize-request.json", "request"),
            ("initialize-response.json", "response"),
        ):
            with self.subTest(name=name):
                raw = (FIXTURES / name).read_bytes()
                message = self.adapter.decode_line(raw)
                self.assertEqual(category, message.category)
                self.assertEqual(json.loads(raw), json.loads(self.adapter.encode_line(message)))

    def test_duplicate_keys_and_unruled_methods_refuse(self) -> None:
        with self.assertRaises(ACPRefusal) as duplicate:
            self.adapter.decode_line(b'{"jsonrpc":"2.0","id":1,"id":2,"method":"initialize"}')
        self.assertEqual("acp_duplicate_key", duplicate.exception.code)

        with self.assertRaises(ACPRefusal) as method:
            self.adapter.decode_line(b'{"jsonrpc":"2.0","id":1,"method":"fs/write","params":{}}')
        self.assertEqual("acp_method_unruled", method.exception.code)

    def test_lines_are_bounded_and_extensions_are_quarantined(self) -> None:
        self.assertEqual(1_048_576, MAXIMUM_ACP_LINE_BYTES)
        message = self.adapter.decode_line(
            b'{"jsonrpc":"2.0","method":"session/update","params":{},"future":{"x":1}}'
        )
        self.assertEqual({"future": {"x": 1}}, message.quarantined)
        with self.assertRaises(ACPRefusal) as oversized:
            self.adapter.decode_line(b" " * MAXIMUM_ACP_LINE_BYTES + b"{}")
        self.assertEqual("acp_line_too_large", oversized.exception.code)

    def test_reference_probe_reports_honest_absence_without_launching(self) -> None:
        self.assertIsNotNone(probe_reference_harness, "ACP harness probe must exist")
        result = probe_reference_harness(which=lambda name: None)
        self.assertEqual(
            {"status": "reference_harness_absent", "executable": None, "command": None},
            result,
        )


if __name__ == "__main__":
    unittest.main()

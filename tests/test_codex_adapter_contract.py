from __future__ import annotations

import ast
import json
import unittest
from pathlib import Path


FIXTURE_DIR = Path("tests/fixtures/codex-app-server")
SCHEMA_DIR = Path("schemas/v0")

try:
    from floati.adapters.codex import (
        MAXIMUM_FRAME_BYTES,
        CodexContractAdapter,
        ContractRefusal,
    )
except (ImportError, ModuleNotFoundError):
    MAXIMUM_FRAME_BYTES = None
    CodexContractAdapter = None
    ContractRefusal = Exception


class CodexAdapterContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.assertIsNotNone(CodexContractAdapter, "dark Codex contract codec must exist")
        self.adapter = CodexContractAdapter()

    def fixture(self, name: str) -> dict:
        return json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))

    def test_recorded_request_response_notification_round_trip_losslessly(self) -> None:
        expected = {
            "initialize-request.json": "request",
            "initialize-response.json": "response",
            "initialized-notification.json": "notification",
        }
        for name, category in expected.items():
            with self.subTest(name=name):
                raw = self.fixture(name)
                message = self.adapter.decode(raw)
                self.assertEqual(category, message.category)
                self.assertEqual(raw, self.adapter.encode(message))

    def test_unknown_root_fields_are_quarantined_but_round_trip(self) -> None:
        raw = self.fixture("initialize-response.json")
        raw["futureServerField"] = {"opaque": [1, 2, 3]}
        message = self.adapter.decode(raw)
        self.assertNotIn("futureServerField", message.fields)
        self.assertEqual({"futureServerField": {"opaque": [1, 2, 3]}}, message.quarantined)
        self.assertEqual(raw, self.adapter.encode(message))

    def test_categories_fail_closed_when_ambiguous_or_malformed(self) -> None:
        invalid = [
            [],
            {},
            {"id": True, "method": "initialize"},
            {"id": 1, "method": "initialize", "result": {}},
            {"id": 1},
            {"id": 1, "result": {}, "error": {"code": -1, "message": "no"}},
            {"method": "initialized", "params": []},
        ]
        for raw in invalid:
            with self.subTest(raw=raw):
                with self.assertRaises(ContractRefusal):
                    self.adapter.decode(raw)

    def test_payload_and_nesting_are_bounded(self) -> None:
        self.assertEqual(1_048_576, MAXIMUM_FRAME_BYTES)
        with self.assertRaises(ContractRefusal):
            self.adapter.decode({"method": "initialized", "future": "x" * MAXIMUM_FRAME_BYTES})
        nested: object = "leaf"
        for _ in range(70):
            nested = [nested]
        with self.assertRaises(ContractRefusal):
            self.adapter.decode({"method": "initialized", "future": nested})

    def test_contract_module_has_no_process_socket_or_network_surface(self) -> None:
        source_path = Path("floati/adapters/codex.py")
        tree = ast.parse(source_path.read_text(encoding="utf-8"))
        forbidden = {"asyncio", "http", "socket", "subprocess", "urllib"}
        imports = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imports.add(node.module.split(".")[0])
        self.assertTrue(forbidden.isdisjoint(imports), imports & forbidden)
        source = source_path.read_text(encoding="utf-8")
        for call in ("Popen(", "run(", "create_connection(", "urlopen("):
            self.assertNotIn(call, source)

    def test_three_adapter_schemas_are_strict_category_envelopes(self) -> None:
        expected = {
            "codex-app-server-request.schema.json": {"id", "method"},
            "codex-app-server-response.schema.json": {"id"},
            "codex-app-server-notification.schema.json": {"method"},
        }
        for name, required in expected.items():
            schema = json.loads((SCHEMA_DIR / name).read_text(encoding="utf-8"))
            self.assertEqual("https://json-schema.org/draft/2020-12/schema", schema["$schema"])
            self.assertEqual(
                f"https://landoclusters.com/floati/schemas/v0/{name}",
                schema["$id"],
            )
            self.assertEqual("object", schema["type"])
            self.assertIs(False, schema["additionalProperties"])
            self.assertTrue(required.issubset(set(schema["required"])))

    def test_thread_observer_source_is_additive_pull_only_protocol(self) -> None:
        source = Path("floati/thread_source.py").read_text(encoding="utf-8")
        self.assertEqual(1, source.count('"thread/read"'))
        self.assertIn('"initialize"', source)
        self.assertIn('"initialized"', source)
        for forbidden in (
            '"thread/start"',
            '"thread/list"',
            '"thread/archive"',
            '"thread/resume"',
            '"thread/fork"',
            '"turn/start"',
            '"turn/steer"',
            '"turn/interrupt"',
        ):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()

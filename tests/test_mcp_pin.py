from __future__ import annotations

import builtins
import importlib
import json
import os
import socket
import time
import unittest
from pathlib import Path
from unittest import mock

from floati.errors import ProtocolRefusal
from floati.ids import uuid7_hex
from floati.records import _SPECS
from tests.schema_validation import SchemaValidationError, validate_json_schema


SCHEMA = Path("schemas/v1/mcp-integration-pin.schema.json")

try:
    mcp_pin = importlib.import_module("floati.mcp_pin")
except ModuleNotFoundError:
    mcp_pin = None


ALPHA_SCHEMA_DIGEST = "df0cd751860cebb7dbf04cb311a379d2ffcef96035486aafc13f2b6d5c610077"
ALPHA_DESCRIPTION_DIGEST = "fb140002f5890c7a8d5729ec4752f770d81b1e17f3ae425d31bc78a16d711e56"
ZETA_SCHEMA_DIGEST = "00404e686415370f1711c4d7acfa2905444d3cf23cef2e10c47d445ebe690f96"
ZETA_DESCRIPTION_DIGEST = "8fac852e054b97064efe6f58031a9769995155930c5fa76ea18842cc1dd4f5f8"


def _id(prefix: str) -> str:
    return prefix + uuid7_hex()


def pin_record() -> dict[str, object]:
    return {
        "schema_version": 1,
        "id": _id("mcp-integration-pin-"),
        "tenant_id": "alpha",
        "timestamp": "2026-09-01T02:20:00.000Z",
        "kind": "mcp_integration_pin",
        "integration_id": "local.tools-v1",
        "server_command": [
            "/opt/floati/bin/mcp-server",
            "--stdio",
            "--config",
            "/opt/floati/mcp.json",
        ],
        "server_executable_digest": "a" * 64,
        "server_config_digest": None,
        "transport": "stdio",
        "declared_capabilities": ["files.read", "tools.list"],
        "tools": [
            {
                "name": "alpha",
                "schema_digest": ALPHA_SCHEMA_DIGEST,
                "description_digest": ALPHA_DESCRIPTION_DIGEST,
            },
            {
                "name": "zeta",
                "schema_digest": ZETA_SCHEMA_DIGEST,
                "description_digest": ZETA_DESCRIPTION_DIGEST,
            },
        ],
        "network_posture": "none",
        "first_seen": "2026-09-01T02:00:00.000Z",
        "last_verified": "2026-09-01T02:20:00.000Z",
        "pin_state": "pinned",
        "unknown_fields": ["server_config_digest"],
    }


def observation() -> dict[str, object]:
    return {
        "integration_id": "local.tools-v1",
        "server_command": [
            "/opt/floati/bin/mcp-server",
            "--stdio",
            "--config",
            "/opt/floati/mcp.json",
        ],
        "server_executable_digest": "a" * 64,
        "server_config_digest": None,
        "transport": "stdio",
        "declared_capabilities": ["files.read", "tools.list"],
        "network_posture": "none",
        "tools": [
            {
                "name": "alpha",
                "schema": {
                    "type": "object",
                    "properties": {"x": {"type": "string"}},
                },
                "description": "Read alpha",
            },
            {
                "name": "zeta",
                "schema": {"type": "string"},
                "description": "Run zeta",
            },
        ],
    }


@unittest.skipIf(mcp_pin is None, "floati.mcp_pin is not implemented")
class McpPinTests(unittest.TestCase):
    def test_validator_accepts_the_closed_schema_valid_pin(self) -> None:
        validated = mcp_pin.validate_mcp_integration_pin(pin_record(), "alpha")

        self.assertEqual(pin_record().keys(), validated.keys())
        self.assertEqual("mcp_integration_pin", validated["kind"])
        self.assertTrue(validated["id"].startswith("mcp-integration-pin-"))
        validate_json_schema(validated, SCHEMA)

    def test_missing_or_extra_record_field_refuses_at_the_closed_boundary(self) -> None:
        for field in pin_record():
            with self.subTest(field=field):
                candidate = pin_record()
                candidate.pop(field)
                with self.assertRaises(ProtocolRefusal) as refusal:
                    mcp_pin.validate_mcp_integration_pin(candidate, "alpha")
                self.assertEqual("mcp_pin_fields_invalid", refusal.exception.code)

        extra = pin_record()
        extra["future"] = "not ruled"
        with self.assertRaises(ProtocolRefusal) as refusal:
            mcp_pin.validate_mcp_integration_pin(extra, "alpha")
        self.assertEqual("mcp_pin_fields_invalid", refusal.exception.code)

    def test_unknown_requires_null_and_listing_in_both_directions(self) -> None:
        for field in ("server_executable_digest", "server_config_digest"):
            with self.subTest(field=field, direction="unnamed"):
                unnamed = pin_record()
                unnamed["server_executable_digest"] = "a" * 64
                unnamed["server_config_digest"] = "b" * 64
                unnamed[field] = None
                unnamed["unknown_fields"] = []
                with self.assertRaises(ProtocolRefusal) as refusal:
                    mcp_pin.validate_mcp_integration_pin(unnamed, "alpha")
                self.assertEqual("mcp_pin_unknown_unnamed", refusal.exception.code)
                with self.assertRaises(SchemaValidationError):
                    validate_json_schema(unnamed, SCHEMA)

            with self.subTest(field=field, direction="contradicted"):
                contradicted = pin_record()
                contradicted["server_executable_digest"] = "a" * 64
                contradicted["server_config_digest"] = "b" * 64
                contradicted["unknown_fields"] = [field]
                with self.assertRaises(ProtocolRefusal) as refusal:
                    mcp_pin.validate_mcp_integration_pin(contradicted, "alpha")
                self.assertEqual("mcp_pin_unknown_contradicted", refusal.exception.code)
                with self.assertRaises(SchemaValidationError):
                    validate_json_schema(contradicted, SCHEMA)

    def test_unknown_fields_rejects_non_string_members_without_crashing(self) -> None:
        malformed = pin_record()
        malformed["unknown_fields"] = [[]]

        with self.assertRaises(ProtocolRefusal) as refusal:
            mcp_pin.validate_mcp_integration_pin(malformed, "alpha")
        self.assertEqual("unknown_fields_invalid", refusal.exception.code)
        with self.assertRaises(SchemaValidationError):
            validate_json_schema(malformed, SCHEMA)

    def test_field_specific_refusals_name_the_invalid_coordinate(self) -> None:
        cases = (
            ("integration_id", "not allowed", "integration_id_invalid"),
            ("transport", "https", "transport_invalid"),
            ("network_posture", "ambient", "network_posture_invalid"),
            ("pin_state", "needs_verification", "pin_state_invalid"),
            ("server_executable_digest", "not-a-digest", "server_executable_digest_invalid"),
            ("declared_capabilities", ["tools.list", "files.read"], "declared_capabilities_invalid"),
        )
        for field, value, code in cases:
            with self.subTest(field=field):
                candidate = pin_record()
                candidate[field] = value
                if field == "server_executable_digest":
                    candidate["unknown_fields"] = ["server_config_digest"]
                with self.assertRaises(ProtocolRefusal) as refusal:
                    mcp_pin.validate_mcp_integration_pin(candidate, "alpha")
                self.assertEqual(code, refusal.exception.code)

    def test_server_command_preserves_order_and_duplicates_but_requires_absolute_argv0(self) -> None:
        candidate = pin_record()
        candidate["server_command"] = ["/bin/server", "same", "same", "--first"]
        validated = mcp_pin.validate_mcp_integration_pin(candidate, "alpha")
        self.assertEqual(candidate["server_command"], validated["server_command"])
        validate_json_schema(validated, SCHEMA)

        relative = pin_record()
        relative["server_command"] = ["server", "--stdio"]
        with self.assertRaises(ProtocolRefusal) as refusal:
            mcp_pin.validate_mcp_integration_pin(relative, "alpha")
        self.assertEqual("server_command_invalid", refusal.exception.code)

    def test_tools_are_closed_sorted_unique_digest_rows(self) -> None:
        for tools in (
            list(reversed(pin_record()["tools"])),
            [pin_record()["tools"][0], pin_record()["tools"][0]],
            [dict(pin_record()["tools"][0], description="raw text")],
        ):
            with self.subTest(tools=tools):
                candidate = pin_record()
                candidate["tools"] = tools
                with self.assertRaises(ProtocolRefusal) as refusal:
                    mcp_pin.validate_mcp_integration_pin(candidate, "alpha")
                self.assertEqual("tools_invalid", refusal.exception.code)
                with self.assertRaises(SchemaValidationError):
                    validate_json_schema(candidate, SCHEMA)

    def test_schema_required_set_matches_runtime_spec(self) -> None:
        schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
        self.assertEqual(1, schema["properties"]["schema_version"]["const"])
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(set(schema["required"]), _SPECS["mcp_integration_pin"][1])
        self.assertEqual(set(schema["properties"]), set(schema["required"]))

    def test_tool_digest_canonicalizes_schema_but_never_description_text(self) -> None:
        first = mcp_pin.mcp_tool_digest(
            {"type": "object", "properties": {"x": {"type": "string"}}},
            "Read alpha",
        )
        reordered = mcp_pin.mcp_tool_digest(
            {"properties": {"x": {"type": "string"}}, "type": "object"},
            "Read alpha",
        )
        zero_width = mcp_pin.mcp_tool_digest(
            {"type": "object", "properties": {"x": {"type": "string"}}},
            "Read\u200d alpha",
        )
        trailing_newline = mcp_pin.mcp_tool_digest(
            {"type": "object", "properties": {"x": {"type": "string"}}},
            "Read alpha\n",
        )

        self.assertEqual(
            {
                "schema_digest": ALPHA_SCHEMA_DIGEST,
                "description_digest": ALPHA_DESCRIPTION_DIGEST,
            },
            first,
        )
        self.assertEqual(first, reordered)
        self.assertEqual(
            "fc971df27326085b5d9df49ea65e502bcdb9df7b90f3b2d50d6876824d472bf7",
            zero_width["description_digest"],
        )
        self.assertEqual(
            "9ea58f1d6d5e77db6adea0e3344ac4306f5f740e651348f647bd91da7d8f6306",
            trailing_newline["description_digest"],
        )
        self.assertNotEqual(first["description_digest"], zero_width["description_digest"])
        self.assertNotEqual(first["description_digest"], trailing_newline["description_digest"])

    def test_observation_validator_is_closed_and_names_its_fields(self) -> None:
        self.assertEqual(observation(), mcp_pin.validate_mcp_observation(observation()))

        extra = observation()
        extra["last_verified"] = "2026-09-01T02:20:00.000Z"
        with self.assertRaises(ProtocolRefusal) as refusal:
            mcp_pin.validate_mcp_observation(extra)
        self.assertEqual("mcp_observation_fields_invalid", refusal.exception.code)

        malformed = observation()
        malformed["tools"][0]["description"] = 7
        with self.assertRaises(ProtocolRefusal) as refusal:
            mcp_pin.validate_mcp_observation(malformed)
        self.assertEqual("tools_invalid", refusal.exception.code)

    def test_byte_identical_observation_is_unchanged_with_empty_detail(self) -> None:
        self.assertEqual(
            {
                "verdict": "unchanged",
                "changed": [],
                "added_tools": [],
                "removed_tools": [],
            },
            mcp_pin.compare_mcp_integration(pin_record(), observation()),
        )

    def test_description_perturbations_drift_by_digest_without_leaking_text(self) -> None:
        for description in ("Read\u200d alpha", "Read alpha\n"):
            with self.subTest(description=repr(description)):
                observed = observation()
                observed["tools"][0]["description"] = description
                result = mcp_pin.compare_mcp_integration(pin_record(), observed)

                self.assertEqual("drifted", result["verdict"])
                self.assertEqual(1, len(result["changed"]))
                self.assertEqual(
                    "tools.alpha.description_digest",
                    result["changed"][0]["field"],
                )
                self.assertEqual(ALPHA_DESCRIPTION_DIGEST, result["changed"][0]["pinned"])
                self.assertNotEqual(ALPHA_DESCRIPTION_DIGEST, result["changed"][0]["observed"])
                self.assertNotIn(description, json.dumps(result, ensure_ascii=False))

    def test_added_removed_and_changed_details_are_sorted_and_derive_verdict(self) -> None:
        observed = observation()
        observed["transport"] = "local_socket"
        observed["server_command"] = ["/opt/floati/bin/mcp-server", "--socket"]
        observed["tools"] = [
            observed["tools"][0],
            {"name": "beta", "schema": {"type": "null"}, "description": "Beta"},
        ]
        result = mcp_pin.compare_mcp_integration(pin_record(), observed)

        self.assertEqual("drifted", result["verdict"])
        self.assertEqual(["beta"], result["added_tools"])
        self.assertEqual(["zeta"], result["removed_tools"])
        self.assertEqual(
            ["server_command", "transport"],
            [row["field"] for row in result["changed"]],
        )
        self.assertTrue(result["changed"] or result["added_tools"] or result["removed_tools"])

    def test_schema_key_reordering_is_unchanged(self) -> None:
        observed = observation()
        observed["tools"][0]["schema"] = {
            "properties": {"x": {"type": "string"}},
            "type": "object",
        }
        self.assertEqual(
            "unchanged",
            mcp_pin.compare_mcp_integration(pin_record(), observed)["verdict"],
        )

    def test_wrong_integration_refuses_instead_of_returning_drift(self) -> None:
        observed = observation()
        observed["integration_id"] = "different.integration"
        with self.assertRaises(ProtocolRefusal) as refusal:
            mcp_pin.compare_mcp_integration(pin_record(), observed)
        self.assertEqual("mcp_observation_mismatch", refusal.exception.code)

    def test_digest_validation_and_comparison_have_zero_io(self) -> None:
        with mock.patch.object(
            builtins, "open", side_effect=AssertionError("MCP pin opened a file")
        ), mock.patch.object(
            socket, "socket", side_effect=AssertionError("MCP pin opened a socket")
        ), mock.patch.object(
            socket,
            "create_connection",
            side_effect=AssertionError("MCP pin opened a connection"),
        ), mock.patch.object(
            os, "getenv", side_effect=AssertionError("MCP pin read the environment")
        ), mock.patch.object(
            time, "time", side_effect=AssertionError("MCP pin read the clock")
        ):
            mcp_pin.validate_mcp_integration_pin(pin_record(), "alpha")
            mcp_pin.mcp_tool_digest({"type": "null"}, "description")
            mcp_pin.compare_mcp_integration(pin_record(), observation())

    def test_r1_exports_only_pure_contract_functions_and_adds_no_surface(self) -> None:
        from floati.cli import _parser
        from floati.command_contract import describe_parser, project_mcp_surface

        self.assertEqual(
            (
                "compare_mcp_integration",
                "mcp_tool_digest",
                "validate_mcp_integration_pin",
                "validate_mcp_observation",
            ),
            mcp_pin.__all__,
        )
        self.assertFalse(hasattr(mcp_pin, "McpPinRegistry"))
        parser = _parser()
        paths = {tuple(row["path"]) for row in describe_parser(parser)["commands"]}
        self.assertFalse(any("pin" in component for path in paths for component in path))
        tools = {tool["name"] for tool in project_mcp_surface(parser)["tools"]}
        self.assertFalse(any("pin" in tool for tool in tools))


class McpPinRedTests(unittest.TestCase):
    def test_mcp_pin_module_exists(self) -> None:
        self.assertIsNotNone(mcp_pin, "floati.mcp_pin must implement the R3 R1 contract")

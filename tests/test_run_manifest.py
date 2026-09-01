from __future__ import annotations

import json
import socket
import unittest
from pathlib import Path
from unittest import mock

from floati.errors import ProtocolRefusal
from floati.ids import uuid7_hex
from floati.records import _SPECS
from floati.run_manifest import validate_run_manifest_fact
from tests.schema_validation import validate_json_schema
from tests.schema_validation import SchemaValidationError


SCHEMA = Path("schemas/v1/run-manifest-fact.schema.json")


def _id(prefix: str) -> str:
    return prefix + uuid7_hex()


def manifest_fact() -> dict[str, object]:
    return {
        "schema_version": 1,
        "id": _id("run-manifest-"),
        "tenant_id": "alpha",
        "timestamp": "2026-09-01T01:00:00.000Z",
        "kind": "run_manifest_fact",
        "attempt_id": _id("attempt-"),
        "run_id": _id("run-"),
        "item_id": _id("work-"),
        "adapter": "codex",
        "harness_version": "codex-app-server/1",
        "model_observed": "gpt-observed",
        "provider_observed": "openai-observed",
        "capability_set_bound_id": _id("capability-set-bound-"),
        "task_contract_id": _id("task-contract-"),
        "task_contract_digest": "1" * 64,
        "policy_digest": "2" * 64,
        "tool_set": ["git", "python"],
        "workspace_base_commit": "3" * 40,
        "toolchain_fingerprint": "4" * 64,
        "budget_allocation": [{"budget_id": "tokens", "amount": 1000}],
        "approvals_consumed": [],
        "verification_commands": [
            {
                "argv": ["python3", "-m", "unittest"],
                "exit_code": 0,
                "self_reported": False,
            }
        ],
        "operator_interventions": [],
        "terminal_outcome": "succeeded",
        "unknown_fields": [],
        "self_reported_fields": ["model_observed"],
    }


class RunManifestFactTests(unittest.TestCase):
    def test_validator_accepts_the_closed_schema_valid_fact(self) -> None:
        fact = manifest_fact()
        validated = validate_run_manifest_fact(fact, "alpha")

        self.assertEqual(fact, validated)
        self.assertEqual("run_manifest_fact", validated["kind"])
        self.assertTrue(str(validated["id"]).startswith("run-manifest-"))
        validate_json_schema(validated, SCHEMA)

    def test_missing_required_field_refuses_at_validation_boundary(self) -> None:
        for field in manifest_fact():
            with self.subTest(field=field):
                candidate = manifest_fact()
                candidate.pop(field)
                with self.assertRaises(ProtocolRefusal) as refusal:
                    validate_run_manifest_fact(candidate, "alpha")
                self.assertEqual(
                    "run_manifest_fields_invalid", refusal.exception.code
                )

    def test_unknown_requires_null_and_listing_in_both_directions(self) -> None:
        nullable = (
            "harness_version",
            "model_observed",
            "provider_observed",
            "toolchain_fingerprint",
            "workspace_base_commit",
        )
        for field in nullable:
            with self.subTest(field=field, direction="unnamed"):
                unnamed = manifest_fact()
                unnamed[field] = None
                with self.assertRaises(ProtocolRefusal) as refusal:
                    validate_run_manifest_fact(unnamed, "alpha")
                self.assertEqual(
                    "run_manifest_unknown_unnamed", refusal.exception.code
                )
                with self.assertRaises(SchemaValidationError):
                    validate_json_schema(unnamed, SCHEMA)

            with self.subTest(field=field, direction="contradicted"):
                contradicted = manifest_fact()
                contradicted["unknown_fields"] = [field]
                with self.assertRaises(ProtocolRefusal) as refusal:
                    validate_run_manifest_fact(contradicted, "alpha")
                self.assertEqual(
                    "run_manifest_unknown_contradicted", refusal.exception.code
                )
                with self.assertRaises(SchemaValidationError):
                    validate_json_schema(contradicted, SCHEMA)

            with self.subTest(field=field, direction="lawful"):
                lawful = manifest_fact()
                lawful[field] = None
                lawful["unknown_fields"] = [field]
                validated = validate_run_manifest_fact(lawful, "alpha")
                self.assertIsNone(validated[field])
                validate_json_schema(validated, SCHEMA)

    def test_empty_approvals_means_observed_none_not_unknown(self) -> None:
        fact = manifest_fact()
        fact["approvals_consumed"] = []
        validated = validate_run_manifest_fact(fact, "alpha")
        self.assertEqual([], validated["approvals_consumed"])
        self.assertNotIn("approvals_consumed", validated["unknown_fields"])

    def test_validation_opens_zero_network_sockets(self) -> None:
        with mock.patch.object(
            socket,
            "socket",
            side_effect=AssertionError("run manifest opened a socket"),
        ), mock.patch.object(
            socket,
            "create_connection",
            side_effect=AssertionError("run manifest opened a connection"),
        ):
            validate_run_manifest_fact(manifest_fact(), "alpha")

    def test_schema_required_set_matches_runtime_spec(self) -> None:
        schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
        self.assertEqual(1, schema["properties"]["schema_version"]["const"])
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(
            set(schema["required"]), _SPECS["run_manifest_fact"][1]
        )

    def test_schema_and_runtime_refuse_noncanonical_collections(self) -> None:
        uuid = "0" * 12 + "7" + "0" * 3 + "8" + "0" * 15
        mutations = {
            "tool_set": ["python", "git"],
            "approvals_consumed": [
                "approval-request-" + uuid,
                "approval-decision-" + uuid,
            ],
            "operator_interventions": ["zeta-" + uuid, "alpha-" + uuid],
            "self_reported_fields": ["tool_set", "adapter"],
        }
        for field, value in mutations.items():
            with self.subTest(field=field):
                fact = manifest_fact()
                fact[field] = value
                with self.assertRaises(ProtocolRefusal):
                    validate_run_manifest_fact(fact, "alpha")
                with self.assertRaises(SchemaValidationError):
                    validate_json_schema(fact, SCHEMA)

        unknown = manifest_fact()
        unknown["model_observed"] = None
        unknown["provider_observed"] = None
        unknown["unknown_fields"] = ["provider_observed", "model_observed"]
        with self.assertRaises(ProtocolRefusal):
            validate_run_manifest_fact(unknown, "alpha")
        with self.assertRaises(SchemaValidationError):
            validate_json_schema(unknown, SCHEMA)

        duplicate = manifest_fact()
        duplicate["tool_set"] = ["git", "git"]
        with self.assertRaises(ProtocolRefusal):
            validate_run_manifest_fact(duplicate, "alpha")
        with self.assertRaises(SchemaValidationError):
            validate_json_schema(duplicate, SCHEMA)

    def test_schema_and_runtime_share_terminal_safe_character_bounds(self) -> None:
        unsafe = manifest_fact()
        unsafe["model_observed"] = "fabricated\nsecond-line"
        with self.assertRaises(ProtocolRefusal):
            validate_run_manifest_fact(unsafe, "alpha")
        with self.assertRaises(SchemaValidationError):
            validate_json_schema(unsafe, SCHEMA)

        multibyte = manifest_fact()
        multibyte["verification_commands"][0]["argv"] = ["é" * 3000]
        validated = validate_run_manifest_fact(multibyte, "alpha")
        validate_json_schema(validated, SCHEMA)

    def test_budget_allocation_normalizes_integral_json_numbers(self) -> None:
        fact = manifest_fact()
        fact["budget_allocation"] = [{"budget_id": "tokens", "amount": 1000.0}]
        validated = validate_run_manifest_fact(fact, "alpha")
        self.assertEqual(1000, validated["budget_allocation"][0]["amount"])
        self.assertIs(type(validated["budget_allocation"][0]["amount"]), int)
        self.assertIs(type(fact["budget_allocation"][0]["amount"]), float)
        validate_json_schema(validated, SCHEMA)

    def test_r2_adds_no_writer_cli_verb_or_mcp_tool(self) -> None:
        import floati.run_manifest as run_manifest
        from floati.cli import _parser
        from floati.command_contract import describe_parser, project_mcp_surface

        self.assertEqual(("validate_run_manifest_fact",), run_manifest.__all__)
        self.assertFalse(hasattr(run_manifest, "RunManifestWriter"))
        parser = _parser()
        paths = {
            tuple(row["path"]) for row in describe_parser(parser)["commands"]
        }
        self.assertFalse(
            any("manifest" in component for path in paths for component in path)
        )
        tools = {
            tool["name"] for tool in project_mcp_surface(parser)["tools"]
        }
        self.assertFalse(any("manifest" in tool for tool in tools))
        for relative in (
            "floati/cli.py",
            "floati/mcp.py",
            "floati/runtruth.py",
            "floati/scheduler.py",
            "floati/workers.py",
        ):
            self.assertNotIn(
                "run_manifest",
                Path(relative).read_text(encoding="utf-8"),
                relative,
            )

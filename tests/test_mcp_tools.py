from __future__ import annotations

from floati import fixture_ids as public_ids

import argparse
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from floati.cli import _parser
from floati.command_contract import describe_parser
try:
    from floati.command_contract import project_mcp_surface
except ImportError:
    project_mcp_surface = None
from floati.events import EventLog
from floati.ids import uuid7_hex
from floati.jsonl import append_record
try:
    from floati.mcp import McpServer, run_cli_artifact
except ImportError:
    McpServer = None
    run_cli_artifact = None
from floati.registry import REGISTRY_KINDS, Registry
from floati.root import FloatiRoot


SHA = "a" * 40
READ_TOOLS = {
    "describe",
    "doctor",
    "effects",
    "graph",
    "inbox",
    "log",
    "receipts",
    "status",
}
GOVERNED_TOOLS = {
    "ack",
    "send",
    "wake_pause",
    "wake_resume",
    "work_claim",
    "work_complete",
}


def leaf_parser(
    parser: argparse.ArgumentParser,
    path: tuple[str, ...],
) -> argparse.ArgumentParser:
    current = parser
    for command in path:
        action = next(
            item
            for item in current._actions
            if isinstance(item, argparse._SubParsersAction)
        )
        current = action.choices[command]
    return current


class McpToolSurfaceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(dir="\x2fprivate/tmp")
        self.addCleanup(self.temporary.cleanup)
        self.root = FloatiRoot.open_direct_home(
            Path(self.temporary.name) / "fleet",
            create=True,
        )
        self.registry = Registry(self.root)
        self.registry.register(public_ids.builder('a'), "Codex")
        self.registry.register(public_ids.builder('b'), "Codex")

    def server(self, node: str = public_ids.builder('a')):
        self.assertIsNotNone(McpServer, "floati.mcp.McpServer must exist")
        return McpServer(str(self.root.path), node, "session-a")

    def surface(self, parser: argparse.ArgumentParser | None = None) -> dict[str, object]:
        self.assertIsNotNone(
            project_mcp_surface,
            "command_contract.project_mcp_surface must exist",
        )
        return project_mcp_surface(_parser() if parser is None else parser)

    def tool(self, name: str, *, server=None) -> dict[str, object]:
        selected = self.server() if server is None else server
        return next(tool for tool in selected.list_tools() if tool["name"] == name)

    def send_arguments(self, *, note: str, key: str) -> dict[str, object]:
        return {
            "recipient": public_ids.builder('b'),
            "repo": "floati",
            "sha": SHA,
            "doc": "docs/evidence/mcp-tools.md",
            "note": note,
            "idempotency_key": key,
        }

    def assert_tool_error(self, result: dict[str, object], code: str) -> dict[str, object]:
        self.assertIs(True, result["isError"])
        artifact = result["structuredContent"]
        self.assertEqual("refused", artifact["status"])
        self.assertEqual(code, artifact["evidence"]["code"])
        self.assertEqual(
            json.dumps(artifact, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            result["content"][0]["text"],
        )
        return artifact

    def test_tool_set_is_derived_from_parser_exposure_tags(self) -> None:
        """Catches a hand-maintained allowlist drifting from live parser tags."""

        tools = self.server().list_tools()

        self.assertEqual(READ_TOOLS | GOVERNED_TOOLS, {tool["name"] for tool in tools})
        for tool in tools:
            self.assertNotIn("DRAFT", tool["description"])
            self.assertNotEqual(tool["description"], "floati " + tool["name"].replace("_", " "))
        self.assertEqual("approved", self.surface()["copy_state"])

    def test_exposure_mutation_changes_both_generated_tool_and_deny_sets(self) -> None:
        """Catches the server ignoring a parser exposure mutation or shadowing deny truth."""

        parser = _parser()
        before = self.surface(parser)
        described = describe_parser(parser)
        expected_denied = {
            tuple(row["path"])
            for row in described["commands"]
            if row["executable"] and row["mcp_exposure"] == "never"
        }
        self.assertEqual(
            expected_denied,
            {tuple(path) for path in before["denied_paths"]},
        )

        leaf_parser(parser, ("worker", "run")).floati_mcp_exposure = "governed"
        after = self.surface(parser)

        self.assertNotIn(("worker", "run"), {tuple(path) for path in after["denied_paths"]})
        self.assertIn("worker_run", {tool["name"] for tool in after["tools"]})

    def test_input_schemas_are_closed_and_derived_from_argparse_actions(self) -> None:
        """Catches bound fields, wrong scalar types, choices, or repeats in MCP schemas."""

        send = self.tool("send")["inputSchema"]
        self.assertFalse({"root", "sender", "actor", "session"} & set(send["properties"]))
        self.assertIn("recipient", send["properties"])
        self.assertEqual(
            {"recipient", "repo", "sha", "doc", "note", "idempotency_key"},
            set(send["required"]),
        )
        self.assertIs(False, send["additionalProperties"])

        status = self.tool("status")["inputSchema"]
        self.assertNotIn("root", status["properties"])
        self.assertNotIn("json", status["properties"])
        doctor = self.tool("doctor")["inputSchema"]
        self.assertEqual("number", doctor["properties"]["probe_budget"]["type"])
        self.assertEqual("boolean", doctor["properties"]["probe"]["type"])
        receipts = self.tool("receipts")["inputSchema"]
        self.assertEqual(["node"], receipts["required"])

        parser = _parser()
        leaf_parser(parser, ("worker", "run")).floati_mcp_exposure = "governed"
        leaf_parser(parser, ("work", "add")).floati_mcp_exposure = "governed"
        generated = {tool["name"]: tool for tool in self.surface(parser)["tools"]}
        self.assertEqual(
            ["claude", "codex", "pi"],
            generated["worker_run"]["inputSchema"]["properties"]["adapter"]["enum"],
        )
        self.assertEqual(
            {"type": "array", "items": {"type": "string"}, "default": []},
            generated["work_add"]["inputSchema"]["properties"]["needs"],
        )

    def test_log_exposes_only_its_single_artifact_path(self) -> None:
        """Catches replay presentation frames escaping the MCP artifact boundary."""

        server = self.server()
        schema = self.tool("log", server=server)["inputSchema"]

        self.assertEqual({}, schema["properties"])
        result = server.call_tool("log", {})
        self.assertEqual("log", result["structuredContent"]["command"])
        self.assertEqual("no_result", result["structuredContent"]["status"])
        self.assertNotIn("isError", result)

    def test_bound_identity_fields_are_absent_and_rejected(self) -> None:
        """Catches a tool call switching root, actor, sender, recipient identity, or session."""

        cases = (
            ("send", "root", str(self.root.path / "other")),
            ("send", "sender", public_ids.builder('b')),
            ("work_claim", "actor", public_ids.builder('b')),
            ("ack", "recipient", public_ids.builder('b')),
            ("ack", "session", "session-b"),
            ("wake_pause", "actor", public_ids.builder('b')),
        )
        server = self.server()
        for tool_name, field, value in cases:
            with self.subTest(tool=tool_name, field=field):
                schema = self.tool(tool_name, server=server)["inputSchema"]
                self.assertNotIn(field, schema["properties"])
                self.assert_tool_error(
                    server.call_tool(tool_name, {field: value}),
                    "arguments_invalid",
                )

    def test_mcp_keyed_governed_tools_refuse_without_a_caller_key(self) -> None:
        """Catches MCP inheriting the CLI's human-only idempotency-key mint."""

        server = self.server()
        send = self.send_arguments(note="missing key", key="discarded")
        del send["idempotency_key"]
        cases = (
            ("send", send),
            ("wake_pause", {}),
            ("wake_resume", {}),
        )
        for tool_name, arguments in cases:
            with self.subTest(tool=tool_name):
                schema = self.tool(tool_name, server=server)["inputSchema"]
                self.assertIn("idempotency_key", schema["required"])
                self.assert_tool_error(
                    server.call_tool(tool_name, arguments),
                    "arguments_invalid",
                )
        self.assertEqual([], EventLog(self.root).records())
        self.assertFalse(
            (self.root.path / "receipts" / "wake-control").exists()
        )

    def test_supplied_wake_key_survives_cli_and_mcp_artifacts(self) -> None:
        """Catches either transport replacing the caller's wake-control key."""

        direct_exit, direct = run_cli_artifact([
            "wake", "pause",
            "--root", str(self.root.path),
            "--as", public_ids.builder('a'),
            "--session", "session-direct",
            "--idempotency-key", "direct-pause-key",
        ])
        server = self.server()
        paused = server.call_tool(
            "wake_pause",
            {"idempotency_key": "mcp-pause-key"},
        )
        resumed = server.call_tool(
            "wake_resume",
            {"idempotency_key": "mcp-resume-key"},
        )

        self.assertEqual(0, direct_exit)
        self.assertEqual(
            "direct-pause-key",
            direct["evidence"]["receipt"]["idempotency_key"],
        )
        self.assertEqual(
            "mcp-pause-key",
            paused["structuredContent"]["evidence"]["receipt"]["idempotency_key"],
        )
        self.assertEqual(
            "mcp-resume-key",
            resumed["structuredContent"]["evidence"]["receipt"]["idempotency_key"],
        )

    def test_human_cli_minted_wake_key_is_echoed_in_the_artifact(self) -> None:
        """Catches human convenience silently minting an unobservable replay key."""

        exit_code, artifact = run_cli_artifact([
            "wake", "pause",
            "--root", str(self.root.path),
            "--as", public_ids.builder('a'),
            "--session", "session-human",
        ])

        self.assertEqual(0, exit_code)
        self.assertTrue(
            artifact["evidence"]["receipt"]["idempotency_key"].startswith(
                "wake-cli-pause-"
            )
        )

    def test_call_routes_through_exact_cli_artifact_and_deduplicates_across_servers(self) -> None:
        """Catches MCP inventing output, identity, or process-local idempotency truth."""

        self.assertIsNotNone(run_cli_artifact, "floati.mcp.run_cli_artifact must exist")
        arguments = self.send_arguments(note="n" * 1024, key="mcp-deduplicate")
        direct_exit, direct_artifact = run_cli_artifact([
            "send",
            "--root", str(self.root.path),
            "--from", public_ids.builder('a'),
            "--to", public_ids.builder('b'),
            "--repo", "floati",
            "--sha", SHA,
            "--doc", "docs/evidence/mcp-tools.md",
            "--note", "n" * 1024,
            "--idempotency-key", "mcp-deduplicate",
        ])
        first = self.server().call_tool("send", arguments)
        second = self.server().call_tool("send", arguments)

        self.assertEqual(0, direct_exit)
        self.assertEqual(direct_artifact, first["structuredContent"])
        self.assertEqual(first, second)
        self.assertNotIn("isError", first)
        self.assertEqual(public_ids.builder('a'), direct_artifact["evidence"]["sender"])
        self.assertEqual(1, len(EventLog(self.root).records()))

    def test_note_cap_refuses_with_exact_cli_artifact_parity(self) -> None:
        """Catches MCP weakening the CLI note cap or translating its refusal."""

        self.assertIsNotNone(run_cli_artifact, "floati.mcp.run_cli_artifact must exist")
        note = "n" * 1025
        direct_exit, direct_artifact = run_cli_artifact([
            "send",
            "--root", str(self.root.path),
            "--from", public_ids.builder('a'),
            "--to", public_ids.builder('b'),
            "--repo", "floati",
            "--sha", SHA,
            "--doc", "docs/evidence/mcp-tools.md",
            "--note", note,
            "--idempotency-key", "mcp-too-long",
        ])
        result = self.server().call_tool(
            "send",
            self.send_arguments(note=note, key="mcp-too-long"),
        )

        self.assertEqual(20, direct_exit)
        self.assertEqual(direct_artifact, result["structuredContent"])
        self.assert_tool_error(result, direct_artifact["evidence"]["code"])
        self.assertEqual([], EventLog(self.root).records())

    def test_retirement_is_rechecked_for_listing_and_every_governed_call(self) -> None:
        """Catches process-cached liveness preserving authority after retirement."""

        server = self.server()
        self.assertEqual(
            GOVERNED_TOOLS,
            {tool["name"] for tool in server.list_tools()} - READ_TOOLS,
        )
        self.registry.retire(public_ids.builder('a'))

        self.assertEqual(READ_TOOLS, {tool["name"] for tool in server.list_tools()})
        wake_exit, wake_artifact = run_cli_artifact([
            "wake", "pause",
            "--root", str(self.root.path),
            "--as", public_ids.builder('a'),
            "--session", "session-a",
            "--idempotency-key", "retired-wake",
        ])
        wake_result = server.call_tool(
            "wake_pause",
            {"idempotency_key": "retired-wake"},
        )
        self.assertEqual(20, wake_exit)
        self.assertEqual(wake_artifact, wake_result["structuredContent"])
        self.assert_tool_error(wake_result, "unknown_node")
        arguments = self.send_arguments(
            note="after retirement",
            key="retired-send",
        )
        direct_exit, direct_artifact = run_cli_artifact([
            "send",
            "--root", str(self.root.path),
            "--from", public_ids.builder('a'),
            "--to", public_ids.builder('b'),
            "--repo", "floati",
            "--sha", SHA,
            "--doc", "docs/evidence/mcp-tools.md",
            "--note", "after retirement",
            "--idempotency-key", "retired-send",
        ])
        result = server.call_tool(
            "send",
            arguments,
        )
        self.assertEqual(20, direct_exit)
        self.assertEqual(direct_artifact, result["structuredContent"])
        self.assert_tool_error(result, direct_artifact["evidence"]["code"])
        self.assertEqual([], EventLog(self.root).records())

    def test_expired_lease_refuses_at_the_cli_boundary_without_hiding_tool(self) -> None:
        """Catches MCP caching lease state or replacing the CLI's typed refusal."""

        expired_at = datetime.now(timezone.utc) - timedelta(minutes=1)
        append_record(
            self.root,
            self.registry.relative_path,
            {
                "schema_version": 0,
                "id": "lease-" + uuid7_hex(),
                "tenant_id": self.root.tenant_id,
                "timestamp": (expired_at - timedelta(minutes=1)).isoformat(
                    timespec="milliseconds"
                ).replace("+00:00", "Z"),
                "kind": "node_lease",
                "node_id": public_ids.builder('a'),
                "workspace": str(self.root.path / "nodes" / public_ids.builder('a')),
                "expires_at": expired_at.isoformat(timespec="milliseconds").replace(
                    "+00:00", "Z"
                ),
                "state": "active",
            },
            allowed_kinds=REGISTRY_KINDS,
        )
        server = self.server()

        self.assertIn("send", {tool["name"] for tool in server.list_tools()})
        result = server.call_tool(
            "send",
            self.send_arguments(note="after expiry", key="expired-send"),
        )

        artifact = self.assert_tool_error(result, "node_lease_expired")
        self.assertIsNone(artifact["evidence"]["remedy"])
        self.assertEqual([], EventLog(self.root).records())


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import sys
import argparse
import io
import json
import os
import sys
import tempfile
import unittest
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from floati import cli
from floati import fixture_ids as public_ids
from floati.mcp import McpServer, _StdioSession
from floati.mcp_pin import (
    compare_mcp_integration,
    mcp_tool_digest,
    validate_mcp_integration_pin,
    validate_mcp_observation,
)
from floati.registry import Registry
from floati.root import FloatiRoot
from tests.temp_roots import REAL_TEMP_ROOT


INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "pin-tests", "version": "0"},
    },
}


class McpServePinTests(unittest.TestCase):
    """WIRE-3: a served MCP surface carries its integration pin material,
    validated on the serve path by the R1 pure module."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
        self.addCleanup(self.temporary.cleanup)
        self.root = FloatiRoot.open_direct_home(
            Path(self.temporary.name) / "fleet", create=True
        )
        self.registry = Registry(self.root)
        self.node = public_ids.builder("a")
        self.registry.register(self.node, "Codex")

    def server(self) -> McpServer:
        return McpServer(
            str(self.root.path),
            self.node,
            "session-a",
            server_command=[
                sys.executable,
                "-m",
                "floati",
                "mcp",
                "serve",
                "--root",
                str(self.root.path),
                "--as",
                self.node,
                "--session",
                "session-a",
            ],
        )

    def initialize_result(self, server: McpServer) -> dict:
        session = _StdioSession(server)
        response = session.dispatch(deepcopy(INITIALIZE))
        self.assertNotIn("error", response)
        return response["result"]

    def test_served_initialize_carries_a_valid_pin(self) -> None:
        result = self.initialize_result(self.server())

        self.assertIn("floatiIntegrationPin", result)
        carried = result["floatiIntegrationPin"]
        self.assertEqual(carried, validate_mcp_observation(carried))

    def test_the_pin_describes_the_served_surface(self) -> None:
        server = self.server()
        served = {tool["name"]: tool for tool in server.list_tools()}
        result = self.initialize_result(server)
        pin = result["floatiIntegrationPin"]

        self.assertEqual(self.node, pin["integration_id"])
        self.assertEqual("stdio", pin["transport"])
        self.assertEqual("none", pin["network_posture"])
        self.assertEqual(["tools"], pin["declared_capabilities"])
        self.assertEqual(sorted(served), [tool["name"] for tool in pin["tools"]])
        for tool in pin["tools"]:
            self.assertEqual(served[tool["name"]]["inputSchema"], tool["schema"])
            self.assertEqual(served[tool["name"]]["description"], tool["description"])

    def test_the_carried_pin_is_pinnable_and_detects_drift(self) -> None:
        server = self.server()
        observation = self.initialize_result(server)["floatiIntegrationPin"]
        moment = (
            datetime.now(timezone.utc)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z")
        )
        pin = {
            "schema_version": 1,
            "kind": "mcp_integration_pin",
            "id": "mcp-integration-pin-018f0f23abcd71238000000000000000",
            "timestamp": moment,
            "tenant_id": self.root.tenant_id,
            "integration_id": observation["integration_id"],
            "server_command": observation["server_command"],
            "server_executable_digest": None,
            "server_config_digest": None,
            "transport": observation["transport"],
            "declared_capabilities": observation["declared_capabilities"],
            "network_posture": observation["network_posture"],
            "tools": [
                {
                    "name": tool["name"],
                    **mcp_tool_digest(tool["schema"], tool["description"]),
                }
                for tool in observation["tools"]
            ],
            "first_seen": moment,
            "last_verified": moment,
            "pin_state": "pinned",
            "unknown_fields": [
                "server_config_digest",
                "server_executable_digest",
            ],
        }
        validate_mcp_integration_pin(pin, self.root.tenant_id)
        self.assertEqual(
            "unchanged", compare_mcp_integration(pin, observation)["verdict"]
        )

        drifted = deepcopy(observation)
        drifted["tools"][0]["description"] += " "
        verdict = compare_mcp_integration(pin, drifted)
        self.assertEqual("drifted", verdict["verdict"])
        self.assertIn(
            f"tools.{drifted['tools'][0]['name']}.description_digest",
            [row["field"] for row in verdict["changed"]],
        )


class McpServeHandlerTests(unittest.TestCase):
    """WIRE-3 rework: the serve handler's pin command must not depend on the
    shell's working directory - a relative argv token is never CWD-joined."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
        self.addCleanup(self.temporary.cleanup)
        self.root = FloatiRoot.open_direct_home(
            Path(self.temporary.name) / "fleet", create=True
        )
        self.registry = Registry(self.root)
        self.node = public_ids.builder("a")
        self.registry.register(self.node, "Codex")
        self.original_cwd = os.getcwd()
        self.addCleanup(os.chdir, self.original_cwd)

    def handler_server_command(self, cwd: str) -> list:
        """Run the real serve handler from one cwd and read the carried pin."""

        request = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "handler-tests", "version": "0"},
            },
        }
        stdin = io.StringIO(json.dumps(request, ensure_ascii=False) + "\n")
        stdout = io.StringIO()
        arguments = argparse.Namespace(
            root=str(self.root.path),
            actor=self.node,
            session="session-a",
        )
        os.chdir(cwd)
        with mock.patch.object(sys, "stdin", stdin), mock.patch.object(
            sys, "stdout", stdout
        ):
            self.assertEqual(0, cli._mcp_serve(arguments))
        responses = [
            json.loads(line) for line in stdout.getvalue().splitlines() if line
        ]
        self.assertEqual([1], [response["id"] for response in responses])
        return responses[0]["result"]["floatiIntegrationPin"]["server_command"]

    def test_serve_handler_pin_is_identical_from_two_working_directories(self) -> None:
        first = self.handler_server_command(self.temporary.name)
        second = self.handler_server_command(
            str(Path(self.temporary.name).resolve() / "fleet")
        )

        self.assertEqual(first, second)
        self.assertTrue(os.path.isabs(first[0]))
        self.assertEqual(sys.argv[1:], first[1:])

    def test_serve_handler_pin_never_contains_cwd_joined_tokens(self) -> None:
        cwd = self.temporary.name
        command = self.handler_server_command(cwd)

        for token in command:
            self.assertFalse(
                token.startswith(cwd + "/"),
                f"CWD-joined fake path in the pinned command: {token!r}",
            )


if __name__ == "__main__":
    unittest.main()

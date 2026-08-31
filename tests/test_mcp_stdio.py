from __future__ import annotations

from floati import __version__
from floati import fixture_ids as public_ids

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from floati.registry import Registry
from floati.root import FloatiRoot
from tests.temp_roots import REAL_TEMP_ROOT


REPOSITORY_ROOT = Path(__file__).parents[1]
LATEST_PROTOCOL = "2025-11-25"
LEGACY_PROTOCOL = "2025-06-18"


def compact(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class McpStdioTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
        self.addCleanup(self.temporary.cleanup)
        self.root = FloatiRoot.open_direct_home(
            Path(self.temporary.name) / "fleet",
            create=True,
        )
        Registry(self.root).register(public_ids.builder('a'), "Codex")

    def run_server(self, messages: list[object | str]) -> subprocess.CompletedProcess[str]:
        lines = [message if isinstance(message, str) else compact(message) for message in messages]
        return subprocess.run(
            [
                sys.executable,
                "-m",
                "floati",
                "mcp",
                "serve",
                "--root",
                str(self.root.path),
                "--as",
                public_ids.builder('a'),
                "--session",
                "session-a",
            ],
            cwd=REPOSITORY_ROOT,
            input="\n".join(lines) + "\n",
            text=True,
            capture_output=True,
            check=False,
        )

    @staticmethod
    def initialize(request_id: object, version: str = LATEST_PROTOCOL) -> dict[str, object]:
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "initialize",
            "params": {
                "protocolVersion": version,
                "capabilities": {},
                "clientInfo": {"name": "floati-test", "version": "1"},
            },
        }

    def responses(self, completed: subprocess.CompletedProcess[str]) -> list[dict[str, object]]:
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertEqual("", completed.stderr)
        lines = completed.stdout.splitlines()
        decoded = [json.loads(line) for line in lines]
        self.assertEqual([compact(item) for item in decoded], lines)
        return decoded

    def test_real_cli_subprocess_runs_one_silent_newline_framed_tool_session(self) -> None:
        """Catches stdout noise, notification replies, bypassed CLI tools, or lost binding."""

        completed = self.run_server([
            self.initialize("initialize"),
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": "ping", "method": "ping"},
            {"jsonrpc": "2.0", "id": "list", "method": "tools/list", "params": {}},
            {
                "jsonrpc": "2.0",
                "id": "status",
                "method": "tools/call",
                "params": {"name": "status", "arguments": {}},
            },
            {
                "jsonrpc": "2.0",
                "id": "invalid-tool-input",
                "method": "tools/call",
                "params": {
                    "name": "status",
                    "arguments": {"root": "\x2fprivate/tmp/actor-switch"},
                },
            },
        ])

        responses = self.responses(completed)
        self.assertEqual(
            ["initialize", "ping", "list", "status", "invalid-tool-input"],
            [response["id"] for response in responses],
        )
        self.assertEqual(
            {
                "protocolVersion": LATEST_PROTOCOL,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "floati", "version": __version__},
            },
            responses[0]["result"],
        )
        self.assertEqual({}, responses[1]["result"])
        self.assertIn(
            "status",
            {tool["name"] for tool in responses[2]["result"]["tools"]},
        )
        status = responses[3]["result"]
        self.assertEqual("status", status["structuredContent"]["command"])
        self.assertEqual("ok", status["structuredContent"]["status"])
        self.assertNotIn("isError", status)
        invalid = responses[4]["result"]
        self.assertIs(True, invalid["isError"])
        self.assertEqual(
            "arguments_invalid",
            invalid["structuredContent"]["evidence"]["code"],
        )

    def test_initialize_negotiates_the_explicit_legacy_version(self) -> None:
        """Catches a server that advertises legacy support but always answers latest."""

        response = self.responses(self.run_server([
            self.initialize(1, LEGACY_PROTOCOL),
        ]))[0]

        self.assertEqual(LEGACY_PROTOCOL, response["result"]["protocolVersion"])

    def test_protocol_errors_are_jsonrpc_errors_while_tool_refusals_are_results(self) -> None:
        """Catches malformed framing, lifecycle bypass, or tool errors using the wrong channel."""

        completed = self.run_server([
            "{",
            '{"jsonrpc":"2.0","id":1,"method":"ping"}{"jsonrpc":"2.0","id":2,"method":"ping"}',
            '{"jsonrpc":"2.0","id":1,"id":2,"method":"ping"}',
            '{"jsonrpc":"2.0","id":NaN,"method":"ping"}',
            [],
            {},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/list"},
            self.initialize(4),
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "method": "notifications/unknown"},
            {"jsonrpc": "2.0", "id": 5, "method": "unknown/method"},
            {
                "jsonrpc": "2.0",
                "id": 6,
                "method": "tools/call",
                "params": {"name": "unknown_tool", "arguments": {}},
            },
            {
                "jsonrpc": "2.0",
                "id": 7,
                "method": "tools/call",
                "params": {"arguments": {}},
            },
        ])

        responses = self.responses(completed)
        self.assertEqual(
            [None, None, None, None, None, None, 3, 4, 5, 6, 7],
            [row.get("id") for row in responses],
        )
        self.assertEqual(
            [
                -32700, -32700, -32700, -32700, -32600, -32600,
                -32002, None, -32601, -32602, -32602,
            ],
            [row.get("error", {}).get("code") for row in responses],
        )
        self.assertNotIn("error", responses[7])


if __name__ == "__main__":
    unittest.main()

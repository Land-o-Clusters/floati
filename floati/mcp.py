"""Dependency-free MCP tool projection over Floati's existing CLI artifacts."""

from __future__ import annotations

import argparse
import io
import json
import sys
from contextlib import redirect_stderr, redirect_stdout
from typing import Dict, List, Optional, Sequence, TextIO

from . import __version__
from . import cli
from .command_contract import project_mcp_surface
from .errors import ProtocolRefusal
from .registry import Registry
from .root import FloatiRoot, validate_identifier
from .wake_control import validate_session_id


_ERROR_STATUSES = frozenset({
    "refused",
    "cannot_speak",
    "malformed_evidence",
    "degraded",
})
LATEST_PROTOCOL_VERSION = "2025-11-25"
LEGACY_PROTOCOL_VERSION = "2025-06-18"
SUPPORTED_PROTOCOL_VERSIONS = frozenset({
    LATEST_PROTOCOL_VERSION,
    LEGACY_PROTOCOL_VERSION,
})

_PARSE_ERROR = -32700
_INVALID_REQUEST = -32600
_METHOD_NOT_FOUND = -32601
_INVALID_PARAMS = -32602
_INTERNAL_ERROR = -32603
_NOT_INITIALIZED = -32002


def run_cli_artifact(argv: Sequence[str]) -> tuple[int, Dict[str, object]]:
    """Run the ordinary CLI boundary and decode its one compact artifact."""

    stdout = io.StringIO()
    stderr = io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        exit_code = cli.main(argv)
    stdout_value = stdout.getvalue()
    stderr_value = stderr.getvalue()
    raw = stdout_value or stderr_value
    if not raw or (stdout_value and stderr_value) or len(raw.splitlines()) != 1:
        raise RuntimeError("CLI artifact boundary emitted an ambiguous result")
    artifact = json.loads(raw)
    if not isinstance(artifact, dict):
        raise RuntimeError("CLI artifact boundary emitted a non-object result")
    return exit_code, artifact


def _tool_result(artifact: Dict[str, object]) -> Dict[str, object]:
    result: Dict[str, object] = {
        "content": [{
            "type": "text",
            "text": json.dumps(
                artifact,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
        }],
        "structuredContent": artifact,
    }
    if artifact.get("status") in _ERROR_STATUSES:
        result["isError"] = True
    return result


def _compact(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _error(
    request_id: object,
    code: int,
    message: str,
) -> Dict[str, object]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": code, "message": message},
    }


def _response(request_id: object, result: object) -> Dict[str, object]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _object_without_duplicate_keys(
    pairs: Sequence[tuple[str, object]],
) -> Dict[str, object]:
    value: Dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON object key")
        value[key] = item
    return value


def _reject_non_json_constant(value: str) -> object:
    raise ValueError(f"non-JSON constant {value}")


def _decode_message(line: str) -> object:
    return json.loads(
        line,
        object_pairs_hook=_object_without_duplicate_keys,
        parse_constant=_reject_non_json_constant,
    )


def _leaf_parser(
    parser: argparse.ArgumentParser,
    path: Sequence[str],
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


def _matches_type(action: argparse.Action, value: object) -> bool:
    if isinstance(action, (argparse._StoreTrueAction, argparse._StoreFalseAction)):
        return isinstance(value, bool)
    if isinstance(action, argparse._AppendAction):
        if not isinstance(value, list):
            return False
        return all(_matches_scalar(action, item) for item in value)
    return _matches_scalar(action, value)


def _matches_scalar(action: argparse.Action, value: object) -> bool:
    if action.type is int:
        return isinstance(value, int) and not isinstance(value, bool)
    if action.type is float:
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    return isinstance(value, str)


def _append_action(argv: List[str], action: argparse.Action, value: object) -> None:
    option = action.option_strings[0] if action.option_strings else None
    if isinstance(action, argparse._StoreTrueAction):
        if value and option is not None:
            argv.append(option)
        return
    if isinstance(action, argparse._StoreFalseAction):
        if not value and option is not None:
            argv.append(option)
        return
    values = value if isinstance(action, argparse._AppendAction) else [value]
    for item in values:
        if option is not None:
            argv.append(option)
        argv.append(str(item))


class McpServer:
    """One launch-bound node/session projected as generated MCP tools."""

    def __init__(self, root: str, node: str, session: str) -> None:
        self.root = FloatiRoot.open_direct_home(root, create=False)
        self.node = validate_identifier(node, "node")
        self.session = validate_session_id(session)
        self.parser = cli._parser()
        surface = project_mcp_surface(self.parser)
        self._catalog = {
            str(tool["name"]): tool
            for tool in surface["tools"]
        }

    @staticmethod
    def _metadata(tool: Dict[str, object]) -> Dict[str, object]:
        return tool["_meta"]["floati"]

    def list_tools(self) -> List[Dict[str, object]]:
        try:
            Registry(self.root).require_active(self.node)
            active = True
        except ProtocolRefusal:
            active = False
        selected = [
            tool
            for tool in self._catalog.values()
            if self._metadata(tool)["exposure"] == "read" or active
        ]
        return json.loads(json.dumps(selected, ensure_ascii=False))

    def has_tool(self, name: str) -> bool:
        return isinstance(name, str) and name in self._catalog

    def _invalid_arguments(self, path: Sequence[str]) -> Dict[str, object]:
        _, artifact = run_cli_artifact([*path, "--mcp-invalid-argument"])
        return _tool_result(artifact)

    def _bound_value(
        self,
        path: tuple[str, ...],
        action: argparse.Action,
    ) -> object | None:
        if action.dest == "root":
            return str(self.root.path)
        if action.dest in {"actor", "sender"}:
            return self.node
        if action.dest == "session":
            return self.session
        if action.dest == "recipient" and path in {("inbox",), ("ack",)}:
            return self.node
        if action.dest == "json" and path in {
            ("describe",),
            ("graph",),
            ("status",),
        }:
            return True
        return None

    def _argv(
        self,
        tool: Dict[str, object],
        arguments: Dict[str, object],
    ) -> List[str] | None:
        metadata = self._metadata(tool)
        path = tuple(str(part) for part in metadata["commandPath"])
        schema = tool["inputSchema"]
        properties = schema["properties"]
        if (
            not isinstance(arguments, dict)
            or not set(arguments).issubset(properties)
            or not set(schema["required"]).issubset(arguments)
        ):
            return None

        argv = list(path)
        parser = _leaf_parser(self.parser, path)
        consumed: set[str] = set()
        for action in parser._actions:
            if isinstance(action, argparse._SubParsersAction):
                continue
            bound = self._bound_value(path, action)
            if bound is not None:
                _append_action(argv, action, bound)
                continue
            if action.dest not in arguments:
                continue
            value = arguments[action.dest]
            if not _matches_type(action, value):
                return None
            _append_action(argv, action, value)
            consumed.add(action.dest)
        if consumed != set(arguments):
            return None
        return argv

    def call_tool(
        self,
        name: str,
        arguments: Dict[str, object],
    ) -> Dict[str, object]:
        tool = self._catalog.get(name)
        if tool is None:
            _, artifact = run_cli_artifact(["mcp-unknown-tool"])
            return _tool_result(artifact)
        metadata = self._metadata(tool)
        path = tuple(str(part) for part in metadata["commandPath"])
        argv = self._argv(tool, arguments)
        if argv is None:
            return self._invalid_arguments(path)
        _, artifact = run_cli_artifact(argv)
        return _tool_result(artifact)


class _StdioSession:
    def __init__(self, server: McpServer) -> None:
        self.server = server
        self.state = "new"

    @staticmethod
    def _valid_id(value: object) -> bool:
        return (
            isinstance(value, str)
            or isinstance(value, int) and not isinstance(value, bool)
        )

    def _initialize(
        self,
        request_id: object,
        params: object,
    ) -> Dict[str, object]:
        if self.state != "new" or not isinstance(params, dict):
            return _error(request_id, _INVALID_PARAMS, "Invalid initialize parameters")
        version = params.get("protocolVersion")
        capabilities = params.get("capabilities")
        client_info = params.get("clientInfo")
        if (
            not isinstance(version, str)
            or not isinstance(capabilities, dict)
            or not isinstance(client_info, dict)
            or not isinstance(client_info.get("name"), str)
            or not isinstance(client_info.get("version"), str)
        ):
            return _error(request_id, _INVALID_PARAMS, "Invalid initialize parameters")
        selected = (
            version
            if version in SUPPORTED_PROTOCOL_VERSIONS
            else LATEST_PROTOCOL_VERSION
        )
        self.state = "initializing"
        return _response(
            request_id,
            {
                "protocolVersion": selected,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "floati", "version": __version__},
            },
        )

    def dispatch(self, message: object) -> Optional[Dict[str, object]]:
        if not isinstance(message, dict):
            return _error(None, _INVALID_REQUEST, "Invalid Request")
        has_id = "id" in message
        request_id = message.get("id")
        method = message.get("method")
        if (
            message.get("jsonrpc") != "2.0"
            or not isinstance(method, str)
            or (has_id and not self._valid_id(request_id))
            or not set(message).issubset({"jsonrpc", "id", "method", "params"})
        ):
            return _error(
                request_id if self._valid_id(request_id) else None,
                _INVALID_REQUEST,
                "Invalid Request",
            )

        if not has_id:
            if method == "notifications/initialized" and self.state == "initializing":
                self.state = "ready"
            return None

        params = message.get("params")
        if method == "initialize":
            return self._initialize(request_id, params)
        if method == "ping":
            if params not in (None, {}):
                return _error(request_id, _INVALID_PARAMS, "Invalid ping parameters")
            return _response(request_id, {})
        if self.state != "ready":
            return _error(request_id, _NOT_INITIALIZED, "Server not initialized")
        if method == "tools/list":
            if params not in (None, {}):
                return _error(request_id, _INVALID_PARAMS, "Invalid tools/list parameters")
            return _response(request_id, {"tools": self.server.list_tools()})
        if method == "tools/call":
            if not isinstance(params, dict):
                return _error(request_id, _INVALID_PARAMS, "Invalid tools/call parameters")
            name = params.get("name")
            arguments = params.get("arguments", {})
            if (
                not set(params).issubset({"name", "arguments", "_meta"})
                or not isinstance(name, str)
                or not isinstance(arguments, dict)
            ):
                return _error(request_id, _INVALID_PARAMS, "Invalid tools/call parameters")
            if not self.server.has_tool(name):
                return _error(request_id, _INVALID_PARAMS, f"Unknown tool: {name}")
            try:
                result = self.server.call_tool(name, arguments)
            except Exception:
                return _error(request_id, _INTERNAL_ERROR, "Internal error")
            return _response(request_id, result)
        return _error(request_id, _METHOD_NOT_FOUND, "Method not found")


def serve_stdio(server: McpServer, stdin: TextIO, stdout: TextIO) -> int:
    """Serve one launch-bound MCP session over newline-delimited stdio."""

    session = _StdioSession(server)
    for raw_line in stdin:
        line = raw_line[:-1] if raw_line.endswith("\n") else raw_line
        if line.endswith("\r"):
            line = line[:-1]
        try:
            message = _decode_message(line)
        except (json.JSONDecodeError, UnicodeError, ValueError):
            result: Optional[Dict[str, object]] = _error(
                None,
                _PARSE_ERROR,
                "Parse error",
            )
        else:
            result = session.dispatch(message)
        if result is not None:
            stdout.write(_compact(result) + "\n")
            stdout.flush()
    return 0


def serve_bound_stdio(root: str, node: str, session: str) -> int:
    """Launch the production stdin/stdout pair for one exact identity."""

    return serve_stdio(McpServer(root, node, session), sys.stdin, sys.stdout)

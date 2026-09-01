"""Pure schema-v1 pin validation and MCP integration comparison."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from copy import deepcopy
from pathlib import Path
from typing import Dict, Mapping

from .errors import ProtocolRefusal
from .records import _SPECS, validate_record


__all__ = (
    "compare_mcp_integration",
    "mcp_tool_digest",
    "validate_mcp_integration_pin",
    "validate_mcp_observation",
)

MCP_PIN_KINDS = frozenset({"mcp_integration_pin"})
MCP_PIN_FIELDS = _SPECS["mcp_integration_pin"][1]
MCP_OBSERVATION_FIELDS = frozenset({
    "integration_id",
    "server_command",
    "server_executable_digest",
    "server_config_digest",
    "transport",
    "declared_capabilities",
    "network_posture",
    "tools",
})
_INTEGRATION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_CAPABILITY = re.compile(r"[a-z0-9](?:[a-z0-9._-]{0,126}[a-z0-9])?")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_BIDI_CONTROLS = frozenset({
    "LRE", "RLE", "LRO", "RLO", "PDF", "LRI", "RLI", "FSI", "PDI", "BN",
})
_COMPARED_FIELDS = (
    "server_command",
    "server_executable_digest",
    "server_config_digest",
    "transport",
    "declared_capabilities",
    "network_posture",
)


def _refuse(code: str, detail: str) -> None:
    raise ProtocolRefusal(code, detail)


def _terminal_unsafe(value: str) -> bool:
    return any(
        unicodedata.category(character) in {"Cc", "Cs"}
        or unicodedata.bidirectional(character) in _BIDI_CONTROLS
        for character in value
    )


def _integration_id(value: object) -> bool:
    return (
        isinstance(value, str)
        and 1 <= len(value) <= 256
        and _INTEGRATION_ID.fullmatch(value) is not None
    )


def _server_command(value: object) -> bool:
    return (
        isinstance(value, list)
        and 1 <= len(value) <= 128
        and all(
            isinstance(argument, str)
            and 1 <= len(argument) <= 4096
            and not _terminal_unsafe(argument)
            for argument in value
        )
        and Path(value[0]).is_absolute()
    )


def _nullable_digest(value: object) -> bool:
    return value is None or (
        isinstance(value, str) and _SHA256.fullmatch(value) is not None
    )


def _capabilities(value: object) -> bool:
    return (
        isinstance(value, list)
        and len(value) <= 64
        and all(
            isinstance(item, str) and _CAPABILITY.fullmatch(item) is not None
            for item in value
        )
        and value == sorted(set(value))
    )


def _tool_name(value: object) -> bool:
    return (
        isinstance(value, str)
        and 1 <= len(value) <= 256
        and not _terminal_unsafe(value)
    )


def validate_mcp_integration_pin(
    pin: Mapping[str, object], expected_tenant: str
) -> Dict[str, object]:
    """Validate one complete durable pin without observing or persisting it."""

    if type(pin) is not dict or set(pin) != MCP_PIN_FIELDS:
        _refuse(
            "mcp_pin_fields_invalid",
            "MCP integration pin requires the exact closed schema-v1 field set",
        )
    return validate_record(
        dict(pin), expected_tenant, MCP_PIN_KINDS, integrity=False
    )


def mcp_tool_digest(schema: object, description: str) -> Dict[str, str]:
    """Digest semantic JSON canonically and untrusted description text byte-exactly."""

    try:
        canonical_schema = json.dumps(
            schema,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise ProtocolRefusal(
            "tool_schema_invalid",
            "tool schema must be finite UTF-8 JSON",
        ) from exc
    if not isinstance(description, str):
        _refuse("description_invalid", "tool description must be text")
    try:
        description_bytes = description.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ProtocolRefusal(
            "description_invalid",
            "tool description must encode as UTF-8 without normalization",
        ) from exc
    return {
        "schema_digest": hashlib.sha256(canonical_schema).hexdigest(),
        "description_digest": hashlib.sha256(description_bytes).hexdigest(),
    }


def validate_mcp_observation(observation: Mapping[str, object]) -> Dict[str, object]:
    """Validate the closed caller-supplied raw observation without any I/O."""

    if type(observation) is not dict or set(observation) != MCP_OBSERVATION_FIELDS:
        _refuse(
            "mcp_observation_fields_invalid",
            "MCP observation requires the exact closed R1 field set",
        )
    if not _integration_id(observation["integration_id"]):
        _refuse("integration_id_invalid", "integration_id violates its closed bounds")
    if not _server_command(observation["server_command"]):
        _refuse(
            "server_command_invalid",
            "server_command must be bounded ordered argv with an absolute executable",
        )
    for field in ("server_executable_digest", "server_config_digest"):
        if not _nullable_digest(observation[field]):
            _refuse(f"{field}_invalid", f"{field} must be null or lowercase SHA-256")
    if observation["transport"] not in {"stdio", "local_socket"}:
        _refuse("transport_invalid", "transport is not a closed v1 value")
    if not _capabilities(observation["declared_capabilities"]):
        _refuse(
            "declared_capabilities_invalid",
            "declared_capabilities must be a sorted unique bounded set",
        )
    if observation["network_posture"] not in {"none", "pinned_server_only"}:
        _refuse(
            "network_posture_invalid",
            "network_posture is not a closed v1 value",
        )

    tools = observation["tools"]
    if not isinstance(tools, list) or len(tools) > 256:
        _refuse("tools_invalid", "observed tools must be a bounded sorted table")
    names = []
    for tool in tools:
        if not isinstance(tool, dict) or set(tool) != {
            "name", "schema", "description"
        }:
            _refuse("tools_invalid", "observed tools require the exact raw fields")
        if not _tool_name(tool["name"]):
            _refuse("tools_invalid", "observed tool name violates its bounds")
        if not isinstance(tool["description"], str):
            _refuse("tools_invalid", "observed tool description must be text")
        mcp_tool_digest(tool["schema"], tool["description"])
        names.append(tool["name"])
    if names != sorted(set(names)):
        _refuse("tools_invalid", "observed tools must be sorted and unique by name")
    return deepcopy(dict(observation))


def compare_mcp_integration(
    pin: Mapping[str, object], observation: Mapping[str, object]
) -> Dict[str, object]:
    """Compare one validated pin with one closed raw observation, without I/O."""

    if type(pin) is not dict or set(pin) != MCP_PIN_FIELDS:
        _refuse("mcp_pin_fields_invalid", "comparison requires one closed MCP pin")
    observed = validate_mcp_observation(observation)
    if pin["integration_id"] != observed["integration_id"]:
        _refuse(
            "mcp_observation_mismatch",
            "pin and observation name different MCP integrations",
        )

    changed = []
    for field in _COMPARED_FIELDS:
        if pin[field] != observed[field]:
            changed.append({
                "field": field,
                "pinned": deepcopy(pin[field]),
                "observed": deepcopy(observed[field]),
            })

    pinned_tools = {tool["name"]: tool for tool in pin["tools"]}
    observed_tools = {
        tool["name"]: dict(name=tool["name"], **mcp_tool_digest(
            tool["schema"], tool["description"]
        ))
        for tool in observed["tools"]
    }
    added_tools = sorted(set(observed_tools) - set(pinned_tools))
    removed_tools = sorted(set(pinned_tools) - set(observed_tools))
    for name in sorted(set(pinned_tools) & set(observed_tools)):
        for digest_field in ("schema_digest", "description_digest"):
            pinned_value = pinned_tools[name][digest_field]
            observed_value = observed_tools[name][digest_field]
            if pinned_value != observed_value:
                changed.append({
                    "field": f"tools.{name}.{digest_field}",
                    "pinned": pinned_value,
                    "observed": observed_value,
                })
    changed.sort(key=lambda row: row["field"])
    verdict = "drifted" if changed or added_tools or removed_tools else "unchanged"
    return {
        "verdict": verdict,
        "changed": changed,
        "added_tools": added_tools,
        "removed_tools": removed_tools,
    }

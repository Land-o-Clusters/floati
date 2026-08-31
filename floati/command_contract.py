"""Machine projections derived from Floati's live argparse registry."""

from __future__ import annotations

import argparse

from .helptext import name_line_description
from typing import Any, Dict, Iterable, List, Sequence, Tuple


CONTRACT_SCHEMA_VERSION = 0


def _subparsers(parser: argparse.ArgumentParser) -> Iterable[argparse._SubParsersAction]:
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            yield action


def _children(parser: argparse.ArgumentParser) -> Tuple[Tuple[str, argparse.ArgumentParser], ...]:
    return tuple(
        (name, child)
        for action in _subparsers(parser)
        for name, child in action.choices.items()
    )


def _executable(parser: argparse.ArgumentParser) -> bool:
    return "handler" in parser._defaults or "direct_handler" in parser._defaults


def _safe_default(value: object) -> object:
    if value is argparse.SUPPRESS:
        return None
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (list, tuple)):
        return [_safe_default(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _safe_default(item) for key, item in value.items()}
    return None


def _value_type(action: argparse.Action) -> str:
    if isinstance(action, (argparse._StoreTrueAction, argparse._StoreFalseAction)):
        return "boolean"
    if isinstance(action, argparse._AppendAction):
        return "array"
    if action.type is int:
        return "integer"
    if action.type is float:
        return "number"
    return "string"


def _argument_row(
    action: argparse.Action,
    group_index: Dict[int, int],
) -> Dict[str, object]:
    choices = [] if action.choices is None else list(action.choices)
    return {
        "name": action.dest,
        "option_strings": list(action.option_strings),
        "positional": not action.option_strings,
        "required": bool(action.required),
        "type": _value_type(action),
        "item_type": (
            "integer"
            if action.type is int
            else "number"
            if action.type is float
            else "string"
        ),
        "repeat": isinstance(action, argparse._AppendAction),
        "nargs": action.nargs,
        "choices": choices,
        "default": _safe_default(action.default),
        "mutually_exclusive_group": group_index.get(id(action)),
    }


def _parser_arguments(parser: argparse.ArgumentParser) -> List[Dict[str, object]]:
    group_index = {
        id(action): index
        for index, group in enumerate(parser._mutually_exclusive_groups)
        for action in group._group_actions
    }
    return [
        _argument_row(action, group_index)
        for action in parser._actions
        if not isinstance(action, argparse._SubParsersAction)
    ]


def _group_rows(parser: argparse.ArgumentParser) -> List[Dict[str, object]]:
    return [
        {
            "required": bool(group.required),
            "arguments": [action.dest for action in group._group_actions],
        }
        for group in parser._mutually_exclusive_groups
    ]


def _example_value(action: argparse.Action) -> str:
    if action.choices:
        return str(next(iter(action.choices)))
    if action.type is int:
        return "1"
    if action.type is float:
        return "1.0"
    if action.dest in {"root", "source", "destination", "workspace"}:
        return "\x2fprivate\x2ftmp/floati-contract"
    if action.dest in {"roots", "declared_roots", "plan", "policy"}:
        return "\x2fprivate\x2ftmp/floati-contract.json"
    return "value"


def _argument_example(action: argparse.Action) -> List[str]:
    if isinstance(action, argparse._StoreTrueAction):
        return [action.option_strings[0]] if action.option_strings else []
    if isinstance(action, argparse._StoreFalseAction):
        return [action.option_strings[0]] if action.option_strings else []

    count = 1
    if isinstance(action.nargs, int):
        count = action.nargs
    values = [_example_value(action)] * count
    if action.option_strings:
        return [action.option_strings[0], *values]
    return values


def _required_arguments(parser: argparse.ArgumentParser) -> List[str]:
    selected_group_actions = {
        id(group._group_actions[0])
        for group in parser._mutually_exclusive_groups
        if group.required and group._group_actions
    }
    argv: List[str] = []
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            continue
        positional_required = not action.option_strings and action.nargs not in ("?", "*")
        if not (action.required or positional_required or id(action) in selected_group_actions):
            continue
        argv.extend(_argument_example(action))
    return argv


def _completion(
    parser: argparse.ArgumentParser,
) -> Tuple[Tuple[str, argparse.ArgumentParser], ...]:
    if _executable(parser):
        return ()
    children = _children(parser)
    if not children:
        return ()
    name, child = children[0]
    return ((name, child),) + _completion(child)


def _example_argv(
    path: Tuple[str, ...],
    parser_chain: Tuple[argparse.ArgumentParser, ...],
) -> List[str]:
    completed_path = list(path)
    completed_chain = list(parser_chain)
    for name, parser in _completion(parser_chain[-1]):
        completed_path.append(name)
        completed_chain.append(parser)

    argv: List[str] = []
    for index, command in enumerate(completed_path):
        argv.extend(_required_arguments(completed_chain[index]))
        argv.append(command)
    argv.extend(_required_arguments(completed_chain[-1]))
    return argv


def _command_rows(parser: argparse.ArgumentParser) -> List[Dict[str, object]]:
    rows: List[Dict[str, object]] = []

    def visit(
        current: argparse.ArgumentParser,
        path: Tuple[str, ...],
        chain: Tuple[argparse.ArgumentParser, ...],
    ) -> None:
        for name, child in _children(current):
            child_path = path + (name,)
            child_chain = chain + (child,)
            rows.append({
                "path": list(child_path),
                "program": child.prog,
                "executable": _executable(child),
                "public": bool(getattr(child, "floati_public", True)),
                "mcp_exposure": getattr(child, "floati_mcp_exposure", "never"),
                "mcp_required": list(getattr(child, "floati_mcp_required", ())),
                "mcp_omit": list(getattr(child, "floati_mcp_omit", ())),
                "artifact_schema_version": child._defaults.get("artifact_schema_version"),
                "arguments": _parser_arguments(child),
                "mutually_exclusive_groups": _group_rows(child),
                "example_argv": _example_argv(child_path, child_chain),
            })
            visit(child, child_path, child_chain)

    visit(parser, (), (parser,))
    return rows


def _mcp_bound_argument(path: Tuple[str, ...], argument: Dict[str, object]) -> bool:
    name = argument["name"]
    if name in {"root", "actor", "sender", "session"}:
        return True
    return name == "recipient" and path in {("inbox",), ("ack",)}


def _mcp_forced_argument(path: Tuple[str, ...], argument: Dict[str, object]) -> bool:
    return argument["name"] == "json" and path in {
        ("describe",),
        ("graph",),
        ("status",),
    }


def _json_schema_property(argument: Dict[str, object]) -> Dict[str, object]:
    value_type = argument["type"]
    if value_type == "array":
        schema: Dict[str, object] = {
            "type": "array",
            "items": {"type": argument["item_type"]},
        }
    else:
        schema = {"type": value_type}
    choices = argument["choices"]
    if choices:
        schema["enum"] = list(choices)
    default = argument["default"]
    if default is not None:
        schema["default"] = default
    return schema


def _mcp_input_schema(
    path: Tuple[str, ...],
    arguments: Sequence[Dict[str, object]],
    mcp_required: Sequence[str],
    mcp_omit: Sequence[str],
) -> Dict[str, object]:
    exposed = [
        argument
        for argument in arguments
        if not _mcp_bound_argument(path, argument)
        and not _mcp_forced_argument(path, argument)
        and argument["name"] not in mcp_omit
    ]
    properties = {
        str(argument["name"]): _json_schema_property(argument)
        for argument in exposed
    }
    required = [
        str(argument["name"])
        for argument in exposed
        if argument["required"]
        or argument["name"] in mcp_required
        or (
            argument["positional"]
            and argument["nargs"] not in ("?", "*")
        )
    ]
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


def project_mcp_surface(parser: argparse.ArgumentParser) -> Dict[str, object]:
    """Derive MCP tools and the complementary deny set from parser metadata."""

    tools: List[Dict[str, object]] = []
    denied_paths: List[List[str]] = []
    for row in _command_rows(parser):
        if not row["executable"]:
            continue
        exposure = row["mcp_exposure"]
        path = tuple(str(part) for part in row["path"])
        if exposure == "never":
            denied_paths.append(list(path))
            continue
        if exposure not in {"read", "governed"}:
            raise ValueError(f"invalid MCP exposure {exposure!r}")
        tools.append({
            "name": "_".join(path),
            "description": name_line_description(" ".join(path)),
            "inputSchema": _mcp_input_schema(
                path,
                row["arguments"],
                row["mcp_required"],
                row["mcp_omit"],
            ),
            "_meta": {
                "floati": {
                    "commandPath": list(path),
                    "copyState": "draft",
                    "exposure": exposure,
                }
            },
        })
    return {
        "schema_version": CONTRACT_SCHEMA_VERSION,
        "copy_state": "approved",
        "tools": tools,
        "denied_paths": denied_paths,
    }


def describe_parser(parser: argparse.ArgumentParser) -> Dict[str, object]:
    """Project one schema-versioned contract from the parser argparse executes."""

    commands = _command_rows(parser)
    return {
        "schema_version": CONTRACT_SCHEMA_VERSION,
        "kind": "command_contract",
        "program": parser.prog,
        "command_count": len(commands),
        "commands": commands,
        "exit_codes": list(getattr(parser, "floati_exit_codes", ())),
    }


def schema_version_for_arguments(
    parser: argparse.ArgumentParser,
    arguments: Sequence[str],
) -> int | None:
    """Derive a top-level command family's unambiguous artifact schema."""

    if not arguments:
        return None

    selected = next(
        (
            child
            for action in _subparsers(parser)
            if (child := action.choices.get(arguments[0])) is not None
        ),
        None,
    )
    if selected is None:
        return None

    versions: set[int] = set()

    def collect(current: argparse.ArgumentParser) -> None:
        value = current._defaults.get("artifact_schema_version")
        if isinstance(value, int) and not isinstance(value, bool):
            versions.add(value)
        for _, child in _children(current):
            collect(child)

    collect(selected)
    if len(versions) != 1:
        return None
    return next(iter(versions))

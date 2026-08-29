"""Live prose and JSON twins for one node boot projection."""

from __future__ import annotations

import unicodedata
from typing import Dict, Mapping, Sequence

from .errors import ProtocolRefusal
from .node_projections import NodeBootProjection, NodeProjectionSource
from .role_templates import RoleTemplate
from .root import FloatiRoot


_BOOT_FIELDS = frozenset(
    {
        "schema_version",
        "kind",
        "node_id",
        "harness",
        "workspace",
        "state_file",
        "fleet_map",
        "role",
        "wake",
        "managed_bus",
        "command",
        "prompt",
    }
)
_FLEET_FIELDS = frozenset({"architect_node", "nodes", "declared_roots"})
_ROLE_FIELDS = frozenset(
    {
        "template_role",
        "template_version",
        "template_sha256",
        "duties",
        "decision_rights",
        "stops",
        "fences",
        "cadence",
        "answers",
    }
)
_WAKE_FIELDS = frozenset({"status", "poll_at_row_boundaries"})
_MANAGED_FIELDS = frozenset(
    {"harness", "executable", "profile", "inbox", "ack", "send", "optional_send"}
)


def _refuse(detail: str) -> None:
    raise ProtocolRefusal("node_explain_output_invalid", detail)


def _mapping(value: object, detail: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        _refuse(detail)
    return value


def _text(value: object, detail: str, *, multiline: bool = False) -> str:
    if not isinstance(value, str) or not value:
        _refuse(detail)
    for character in value:
        if ord(character) == 127 or unicodedata.bidirectional(character) in {
            "LRE",
            "RLE",
            "LRO",
            "RLO",
            "PDF",
            "LRI",
            "RLI",
            "FSI",
            "PDI",
            "BN",
        }:
            _refuse(detail)
        if ord(character) < 32 and (not multiline or character != "\n"):
            _refuse(detail)
    try:
        value.encode("ascii")
    except UnicodeEncodeError as exc:
        raise ProtocolRefusal("node_explain_output_invalid", detail) from exc
    return value


def _lines(value: object, detail: str) -> list[str]:
    if not isinstance(value, list) or not value:
        _refuse(detail)
    rendered = []
    for item in value:
        line = _text(item, detail)
        if "\n" in line or line.startswith("DRAFT - "):
            _refuse(detail)
        rendered.append(line)
    return rendered


def _command_parts(value: object, detail: str) -> str:
    if not isinstance(value, list) or not value:
        _refuse(detail)
    parts = []
    for item in value:
        part = _text(item, detail)
        if "\n" in part:
            _refuse(detail)
        parts.append(part)
    return " ".join(parts)


def _validate_boot_artifact(artifact: object) -> Mapping[str, object]:
    value = _mapping(artifact, "projection artifact must be an object")
    if set(value) != _BOOT_FIELDS:
        _refuse("projection artifact fields do not match the D3 boot record")
    if (
        value.get("schema_version") != 0
        or isinstance(value.get("schema_version"), bool)
        or value.get("kind") != "node_boot_projection"
    ):
        _refuse("explanation requires a node boot projection record")
    for field in ("node_id", "harness", "workspace", "state_file", "command"):
        _text(value.get(field), f"projection {field} is invalid")
    prompt = _text(value.get("prompt"), "projection prompt is invalid", multiline=True)
    if prompt.startswith("DRAFT - "):
        _refuse("projection prompt still carries a DRAFT stamp")

    fleet = _mapping(value.get("fleet_map"), "projection fleet map is invalid")
    if set(fleet) != _FLEET_FIELDS:
        _refuse("projection fleet map fields are invalid")
    _text(fleet.get("architect_node"), "projection architect is invalid")
    raw_nodes = fleet.get("nodes")
    if not isinstance(raw_nodes, list) or not raw_nodes:
        _refuse("projection fleet nodes are invalid")
    for node in raw_nodes:
        node_record = _mapping(node, "projection fleet node is invalid")
        if set(node_record) != {"node_id", "role", "harness", "state"}:
            _refuse("projection fleet node fields are invalid")
        for field in ("node_id", "role", "harness", "state"):
            _text(node_record.get(field), "projection fleet node text is invalid")
    raw_roots = fleet.get("declared_roots")
    if not isinstance(raw_roots, list) or not raw_roots:
        _refuse("projection declared roots are invalid")
    for declaration in raw_roots:
        root = _mapping(declaration, "projection declared root is invalid")
        if set(root) != {"bus_id", "root", "architect_node", "downstream"}:
            _refuse("projection declared root fields are invalid")
        for field in ("bus_id", "root", "architect_node"):
            _text(root.get(field), "projection declared root text is invalid")
        downstream = root.get("downstream")
        if not isinstance(downstream, list):
            _refuse("projection declared root downstream is invalid")
        for target in downstream:
            _text(target, "projection declared root downstream text is invalid")

    role = _mapping(value.get("role"), "projection role is invalid")
    if set(role) != _ROLE_FIELDS:
        _refuse("projection role fields are invalid")
    for field in ("template_role", "template_sha256", "cadence"):
        _text(role.get(field), "projection role text is invalid")
    version = role.get("template_version")
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        _refuse("projection role version is invalid")
    for field in ("duties", "decision_rights", "stops", "fences"):
        _lines(role.get(field), "projection role copy is invalid")
    answers = role.get("answers")
    if not isinstance(answers, Mapping) or not answers:
        _refuse("projection role answers are invalid")
    for key, answer in answers.items():
        _text(key, "projection answer key is invalid")
        _text(answer, "projection answer is invalid")

    wake = _mapping(value.get("wake"), "projection wake is invalid")
    if set(wake) != _WAKE_FIELDS:
        _refuse("projection wake fields are invalid")
    if wake.get("status") not in {"armed", "eligible", "none"}:
        _refuse("projection wake status is invalid")
    if not isinstance(wake.get("poll_at_row_boundaries"), bool):
        _refuse("projection wake boundary flag is invalid")

    managed = _mapping(value.get("managed_bus"), "projection managed bus is invalid")
    if set(managed) != _MANAGED_FIELDS:
        _refuse("projection managed bus fields are invalid")
    for field in ("harness", "executable", "profile"):
        _text(managed.get(field), "projection managed bus text is invalid")
    for field in ("inbox", "ack", "send", "optional_send"):
        if not isinstance(managed.get(field), list):
            _refuse("projection managed bus verbs are invalid")
        _command_parts(managed[field], "projection managed bus verb is invalid")
    return value


class NodeExplainProjection:
    """Explain a node by reprojecting one live D3 boot source."""

    def __init__(
        self,
        root: FloatiRoot,
        node_id: str,
        source: NodeProjectionSource,
        templates: Mapping[str, RoleTemplate],
    ) -> None:
        self._boot = NodeBootProjection(root, node_id, source, templates)

    @classmethod
    def from_boot(cls, boot: NodeBootProjection) -> "NodeExplainProjection":
        if not isinstance(boot, NodeBootProjection):
            raise TypeError("from_boot requires a NodeBootProjection")
        instance = cls.__new__(cls)
        instance._boot = boot
        return instance

    def project(self) -> Dict[str, object]:
        """Return a fresh D3 boot record for this explanation request."""

        return self._boot.project()

    def to_json(self) -> str:
        """Return the exact JSON twin of the current D3 boot record."""

        return self._boot.to_json()

    def render(self) -> str:
        """Render the current D3 boot record as an explanation board."""

        return render_node_explanation(self.project())


def render_node_explanation(artifact: Mapping[str, object]) -> str:
    """Render every D3 boot field as deterministic ASCII prose."""

    value = _validate_boot_artifact(artifact)
    fleet = value["fleet_map"]
    role = value["role"]
    wake = value["wake"]
    managed = value["managed_bus"]
    assert isinstance(fleet, Mapping)
    assert isinstance(role, Mapping)
    assert isinstance(wake, Mapping)
    assert isinstance(managed, Mapping)

    node_id = str(value["node_id"])
    harness = str(value["harness"])
    role_name = str(role["template_role"])
    architect = str(fleet["architect_node"])
    lines = [
        "NODE EXPLANATION",
        f"WHAT THIS NODE IS: {node_id} using harness {harness}.",
        f"WHY THIS ROLE: {role_name} is assigned from the live role record.",
        f"CURRENT ARCHITECT: {architect}",
        f"WORKSPACE: {value['workspace']}",
        f"STATE FILE: {value['state_file']}",
        "CURRENT FLEET NODES:",
    ]
    for node in fleet["nodes"]:
        assert isinstance(node, Mapping)
        lines.append(
            f"{node['node_id']} | role={node['role']} | "
            f"harness={node['harness']} | state={node['state']}"
        )
    lines.append("DECLARED ROOTS:")
    for declaration in fleet["declared_roots"]:
        assert isinstance(declaration, Mapping)
        downstream = declaration["downstream"]
        assert isinstance(downstream, list)
        lines.append(
            f"{declaration['bus_id']} | root={declaration['root']} | "
            f"architect={declaration['architect_node']} | "
            f"downstream={','.join(str(target) for target in downstream) or 'none'}"
        )
    lines.extend(
        [
            f"ROLE TEMPLATE: {role_name} v{role['template_version']} "
            f"sha256={role['template_sha256']}.",
            "ROLE DUTIES:",
        ]
    )
    lines.extend(str(item) for item in role["duties"])
    lines.append("DECISION RIGHTS:")
    lines.extend(str(item) for item in role["decision_rights"])
    lines.append("STOPS:")
    lines.extend(str(item) for item in role["stops"])
    lines.append("FENCES:")
    lines.extend(str(item) for item in role["fences"])
    lines.append(f"CADENCE: {role['cadence']}.")
    lines.append("INTERVIEW ANSWERS:")
    for key, answer in sorted(role["answers"].items()):
        lines.append(f"{key}: {answer}")
    lines.extend(
        [
            f"WAKE: {wake['status']}; poll at row boundaries: "
            f"{'yes' if wake['poll_at_row_boundaries'] else 'no'}",
            f"MANAGED BUS: {managed['executable']} {managed['profile']}",
            f"INBOX VERB: {_command_parts(managed['inbox'], 'managed inbox is invalid')}",
            f"ACK VERB: {_command_parts(managed['ack'], 'managed ack is invalid')}",
            f"SEND VERB: {_command_parts(managed['send'], 'managed send is invalid')}",
            f"OPTIONAL SEND FLAG: {_command_parts(managed['optional_send'], 'managed optional send is invalid')}",
            f"BOOT COMMAND: {value['command']}",
            "BOOT PROMPT:",
            str(value["prompt"]),
        ]
    )
    rendered = "\n".join(lines) + "\n"
    _text(rendered, "explanation board is invalid", multiline=True)
    return rendered

"""Read-only boot and teardown projections for one live node."""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, Mapping, Protocol, Sequence, Tuple

from .errors import ProtocolRefusal
from .records import validate_role
from .role_templates import RoleTemplate
from .root import FloatiRoot, validate_identifier


_UUID7_HEX = r"[0-9a-f]{12}7[0-9a-f]{3}[89ab][0-9a-f]{15}"
_ROLE_RECORD_ID = re.compile(r"^registry-role-" + _UUID7_HEX + r"$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_TIMESTAMP = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{3})?Z$"
)
_EXECUTABLE = re.compile(r"^[A-Za-z0-9_./~-]+$")
_WAKE_STATES = frozenset({"armed", "eligible", "none"})
_ROLE_RECORD_FIELDS = frozenset(
    {
        "schema_version",
        "id",
        "tenant_id",
        "timestamp",
        "kind",
        "node_id",
        "template_role",
        "template_version",
        "template_sha256",
        "answers",
        "state",
        "predecessor_role_record_id",
    }
)
_DECLARED_ROOT_FIELDS = frozenset(
    {"bus_id", "root", "architect_node", "downstream"}
)
_REQUIRED_SEND_FLAGS = (
    "--to",
    "--sha",
    "--doc",
    "--idempotency-key",
    "--note",
)


def _refuse(code: str, detail: str) -> None:
    raise ProtocolRefusal(code, detail)


def _safe_text(value: object, code: str, detail: str, *, maximum: int = 4096) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= maximum:
        _refuse(code, detail)
    if any(
        unicodedata.category(character) in {"Cc", "Cs"}
        or unicodedata.bidirectional(character)
        in {"LRE", "RLE", "LRO", "RLO", "PDF", "LRI", "RLI", "FSI", "PDI", "BN"}
        for character in value
    ):
        _refuse(code, detail)
    return value


def _sequence(value: object, code: str, detail: str) -> Sequence[object]:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        _refuse(code, detail)
    return value


def _mapping(value: object, code: str, detail: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        _refuse(code, detail)
    return value


def _safe_path_text(value: object, code: str, detail: str) -> str:
    text = _safe_text(value, code, detail)
    if not Path(text).is_absolute():
        _refuse(code, detail)
    return text


def _valid_timestamp(value: str) -> bool:
    if _TIMESTAMP.fullmatch(value) is None:
        return False
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return True


@dataclass(frozen=True)
class ManagedVerbShape:
    """The exact managed-bus verbs a harness seat may print."""

    harness: str
    executable: str
    profile: str
    inbox: Tuple[str, ...] = ("inbox",)
    ack: Tuple[str, ...] = ("ack", "--id", "--session")
    send: Tuple[str, ...] = ("send",) + _REQUIRED_SEND_FLAGS
    optional_send: Tuple[str, ...] = ("--reply-to",)

    def __post_init__(self) -> None:
        try:
            harness = validate_role(self.harness)
        except ProtocolRefusal as exc:
            raise ProtocolRefusal(
                "node_projection_managed_bus_invalid",
                "managed bus harness is invalid",
            ) from exc
        executable = _safe_text(
            self.executable,
            "node_projection_managed_bus_invalid",
            "managed bus executable is invalid",
            maximum=512,
        )
        profile = _safe_text(
            self.profile,
            "node_projection_managed_bus_invalid",
            "managed bus profile is invalid",
            maximum=128,
        )
        if _EXECUTABLE.fullmatch(executable) is None:
            raise ProtocolRefusal(
                "node_projection_managed_bus_invalid",
                "managed bus executable must be a safe command component",
            )
        try:
            validate_identifier(profile, "profile")
        except ProtocolRefusal as exc:
            raise ProtocolRefusal(
                "node_projection_managed_bus_invalid",
                "managed bus profile is not a bounded identifier",
            ) from exc
        for field_name, value in (
            ("inbox", self.inbox),
            ("ack", self.ack),
            ("send", self.send),
            ("optional_send", self.optional_send),
        ):
            if not isinstance(value, tuple) or any(
                not isinstance(part, str) or not part or "\n" in part or "\r" in part
                for part in value
            ):
                raise ProtocolRefusal(
                    "node_projection_managed_bus_invalid",
                    f"managed bus {field_name} shape is invalid",
                )
        if self.inbox != ("inbox",):
            raise ProtocolRefusal(
                "node_projection_managed_bus_invalid",
                "managed bus inbox shape is not the exact inbox verb",
            )
        if self.ack != ("ack", "--id", "--session"):
            raise ProtocolRefusal(
                "node_projection_managed_bus_invalid",
                "managed bus ack shape is not the exact ack verb",
            )
        if self.send != ("send",) + _REQUIRED_SEND_FLAGS:
            raise ProtocolRefusal(
                "node_projection_managed_bus_invalid",
                "managed bus send shape does not use the exact required flag order",
            )
        if self.optional_send != ("--reply-to",):
            raise ProtocolRefusal(
                "node_projection_managed_bus_invalid",
                "managed bus optional send shape is invalid",
            )
        object.__setattr__(self, "harness", harness)
        object.__setattr__(self, "executable", executable)
        object.__setattr__(self, "profile", profile)

    @property
    def record(self) -> Dict[str, object]:
        return {
            "harness": self.harness,
            "executable": self.executable,
            "profile": self.profile,
            "inbox": list(self.inbox),
            "ack": list(self.ack),
            "send": list(self.send),
            "optional_send": list(self.optional_send),
        }

    def _prefix(self) -> str:
        return f"{self.executable} {self.profile}"

    def inbox_line(self) -> str:
        return f"{self._prefix()} inbox"

    def ack_line(self) -> str:
        return (
            f"{self._prefix()} ack --id <message-id> "
            "[--id <message-id> ...] --session <session-id>"
        )

    def send_line(self) -> str:
        return (
            f"{self._prefix()} send --to <recipient> --sha <40-hex-sha> "
            "--doc <document> --idempotency-key <idempotency-key> --note <note> "
            "[--reply-to <message-id>]"
        )


class NodeProjectionSource(Protocol):
    """A train-owned adapter that reads each mutable ledger at call time."""

    def active_node(self, node_id: str) -> Mapping[str, object]: ...

    def active_nodes(self) -> Sequence[Mapping[str, object]]: ...

    def role_record(self, node_id: str) -> Mapping[str, object]: ...

    def declared_roots(self) -> Sequence[Mapping[str, object]]: ...

    def wake_status(self, node_id: str) -> str: ...

    def managed_verbs(self, node_id: str, harness: str) -> ManagedVerbShape: ...


def _copy_role_lines(template: RoleTemplate, field: str) -> list[str]:
    values = getattr(template, field)
    if not isinstance(values, tuple) or not values:
        _refuse("node_projection_role_invalid", f"role template {field} is empty")
    copied = []
    for value in values:
        if not isinstance(value, str) or not value or value.startswith("DRAFT - "):
            _refuse(
                "node_projection_copy_invalid",
                f"role template {field} contains DRAFT-stamped or empty copy",
            )
        copied.append(value)
    return copied


def _validate_templates(templates: Mapping[str, RoleTemplate]) -> Dict[str, RoleTemplate]:
    if not isinstance(templates, Mapping) or not templates:
        _refuse("node_projection_templates_invalid", "role template catalog is empty")
    checked: Dict[str, RoleTemplate] = {}
    for name, template in templates.items():
        if not isinstance(name, str) or not isinstance(template, RoleTemplate):
            _refuse("node_projection_templates_invalid", "role template catalog is malformed")
        if name != template.role:
            _refuse("node_projection_templates_invalid", "role template catalog key is mismatched")
        checked[name] = template
    return checked


class _NodeProjection:
    _kind = ""

    def __init__(
        self,
        root: FloatiRoot,
        node_id: str,
        source: NodeProjectionSource,
        templates: Mapping[str, RoleTemplate],
    ) -> None:
        if not isinstance(root, FloatiRoot):
            _refuse("node_projection_root_invalid", "projection requires a validated root")
        self.root = root
        self.node_id = validate_identifier(node_id, "node")
        self.source = source
        self.templates = _validate_templates(templates)

    def _node(self, value: object, *, code: str) -> Dict[str, object]:
        record = dict(_mapping(value, code, "node evidence must be an object"))
        if record.get("tenant_id") != self.root.tenant_id:
            _refuse(code, "node evidence belongs to another tenant")
        if record.get("state") != "active":
            _refuse(code, "node evidence is not active")
        try:
            node_id = validate_identifier(record.get("node_id"), "node")
            role = validate_identifier(record.get("role"), "role")
            harness = validate_role(record.get("harness"))
        except ProtocolRefusal as exc:
            raise ProtocolRefusal(code, "node evidence has invalid identity") from exc
        return {
            "node_id": node_id,
            "role": role,
            "harness": harness,
            "state": "active",
        }

    def _live_node_and_fleet(self) -> Tuple[Dict[str, object], Dict[str, object]]:
        selected = self._node(
            self.source.active_node(self.node_id), code="node_projection_node_invalid"
        )
        if selected["node_id"] != self.node_id:
            _refuse(
                "node_projection_node_invalid",
                "active node evidence names another node",
            )
        raw_nodes = _sequence(
            self.source.active_nodes(),
            "node_projection_fleet_invalid",
            "active fleet evidence must be a sequence",
        )
        nodes = []
        seen = set()
        for raw_node in raw_nodes:
            node = self._node(raw_node, code="node_projection_fleet_invalid")
            node_id = str(node["node_id"])
            if node_id in seen:
                _refuse("node_projection_fleet_invalid", "active fleet repeats a node")
            seen.add(node_id)
            nodes.append(node)
        if selected["node_id"] not in seen:
            _refuse("node_projection_fleet_invalid", "active fleet omits the selected node")
        matching = [node for node in nodes if node["node_id"] == selected["node_id"]]
        if matching != [selected]:
            _refuse("node_projection_fleet_invalid", "selected node disagrees with the active fleet")
        architects = [node for node in nodes if str(node["role"]).casefold() == "architect"]
        if len(architects) != 1:
            _refuse(
                "node_projection_fleet_invalid",
                "active fleet must contain exactly one architect",
            )
        nodes.sort(key=lambda node: str(node["node_id"]))
        return selected, {
            "architect_node": architects[0]["node_id"],
            "nodes": nodes,
        }

    def _declared_roots(self) -> list[Dict[str, object]]:
        raw_roots = _sequence(
            self.source.declared_roots(),
            "node_projection_roots_invalid",
            "declared roots evidence must be a sequence",
        )
        roots = []
        seen = set()
        for raw_root in raw_roots:
            declaration = _mapping(
                raw_root,
                "node_projection_roots_invalid",
                "declared root evidence must be an object",
            )
            if set(declaration) != _DECLARED_ROOT_FIELDS:
                _refuse(
                    "node_projection_roots_invalid",
                    "declared root evidence has an unexpected shape",
                )
            try:
                bus_id = validate_identifier(declaration["bus_id"], "bus")
                architect = validate_identifier(
                    declaration["architect_node"], "architect_node"
                )
            except ProtocolRefusal as exc:
                raise ProtocolRefusal(
                    "node_projection_roots_invalid",
                    "declared root identity is invalid",
                ) from exc
            root_path = _safe_path_text(
                declaration["root"],
                "node_projection_roots_invalid",
                "declared root path is invalid",
            )
            downstream = _sequence(
                declaration["downstream"],
                "node_projection_roots_invalid",
                "declared root downstream value must be a sequence",
            )
            targets = []
            target_seen = set()
            for target in downstream:
                try:
                    normalized_target = validate_identifier(target, "downstream_bus")
                except ProtocolRefusal as exc:
                    raise ProtocolRefusal(
                        "node_projection_roots_invalid",
                        "declared root downstream identity is invalid",
                    ) from exc
                if normalized_target in target_seen:
                    _refuse(
                        "node_projection_roots_invalid",
                        "declared root downstream repeats a bus",
                    )
                target_seen.add(normalized_target)
                targets.append(normalized_target)
            if bus_id in seen:
                _refuse("node_projection_roots_invalid", "declared roots repeat a bus")
            seen.add(bus_id)
            roots.append(
                {
                    "bus_id": bus_id,
                    "root": root_path,
                    "architect_node": architect,
                    "downstream": sorted(targets),
                }
            )
        if not roots:
            _refuse("node_projection_roots_invalid", "declared roots evidence is empty")
        roots.sort(key=lambda declaration: str(declaration["bus_id"]))
        return roots

    def _role(self) -> Dict[str, object]:
        raw = _mapping(
            self.source.role_record(self.node_id),
            "node_projection_role_invalid",
            "role record evidence must be an object",
        )
        if set(raw) != _ROLE_RECORD_FIELDS:
            _refuse("node_projection_role_invalid", "role record has an unexpected shape")
        if (
            raw.get("schema_version") != 0
            or isinstance(raw.get("schema_version"), bool)
            or raw.get("tenant_id") != self.root.tenant_id
            or raw.get("kind") != "registry_role_record"
            or raw.get("node_id") != self.node_id
            or raw.get("state") != "active"
            or not isinstance(raw.get("id"), str)
            or _ROLE_RECORD_ID.fullmatch(raw["id"]) is None
            or not isinstance(raw.get("timestamp"), str)
            or not _valid_timestamp(raw["timestamp"])
        ):
            _refuse("node_projection_role_invalid", "role record identity or state is invalid")
        predecessor = raw.get("predecessor_role_record_id")
        if predecessor is not None and (
            not isinstance(predecessor, str) or _ROLE_RECORD_ID.fullmatch(predecessor) is None
        ):
            _refuse("node_projection_role_invalid", "role record predecessor is invalid")
        try:
            template_role = validate_identifier(raw.get("template_role"), "template_role")
        except ProtocolRefusal as exc:
            raise ProtocolRefusal(
                "node_projection_role_invalid", "role record template role is invalid"
            ) from exc
        version = raw.get("template_version")
        if not isinstance(version, int) or isinstance(version, bool) or version < 1:
            _refuse("node_projection_role_invalid", "role record template version is invalid")
        digest = raw.get("template_sha256")
        if not isinstance(digest, str) or _SHA256.fullmatch(digest) is None:
            _refuse("node_projection_role_invalid", "role record template digest is invalid")
        answers = _mapping(
            raw.get("answers"),
            "node_projection_role_invalid",
            "role record answers must be an object",
        )
        template = self.templates.get(template_role)
        if template is None:
            _refuse("node_projection_role_mismatch", "role record template is not in the catalog")
        if (
            template.template_version != version
            or template.digest != digest
            or template.role != template_role
        ):
            _refuse("node_projection_role_mismatch", "role record provenance does not match the catalog")
        question_keys = {question.key for question in template.questions}
        if set(answers) != question_keys:
            _refuse(
                "node_projection_role_invalid",
                "role record answers do not match the declared interview",
            )
        resolved_answers: Dict[str, str] = {}
        for key in sorted(question_keys):
            try:
                validate_identifier(key, "answer_key")
            except ProtocolRefusal as exc:
                raise ProtocolRefusal(
                    "node_projection_role_invalid", "role record answer key is invalid"
                ) from exc
            resolved_answers[key] = _safe_text(
                answers[key],
                "node_projection_role_invalid",
                "role record answer is invalid",
                maximum=500,
            )
        return {
            "template_role": template.role,
            "template_version": template.template_version,
            "template_sha256": template.digest,
            "duties": _copy_role_lines(template, "duties"),
            "decision_rights": _copy_role_lines(template, "decision_rights"),
            "stops": _copy_role_lines(template, "stops"),
            "fences": _copy_role_lines(template, "fences"),
            "cadence": _safe_text(
                template.cadence,
                "node_projection_role_invalid",
                "role cadence is invalid",
            ),
            "answers": resolved_answers,
        }

    def _wake(self) -> Dict[str, object]:
        value = self.source.wake_status(self.node_id)
        if not isinstance(value, str) or value not in _WAKE_STATES:
            _refuse("node_projection_wake_invalid", "wake status is not a declared posture")
        return {
            "status": value,
            "poll_at_row_boundaries": value in {"armed", "eligible"},
        }

    def _managed_bus(self, harness: str) -> Tuple[ManagedVerbShape, Dict[str, object]]:
        try:
            shape = self.source.managed_verbs(self.node_id, harness)
        except ProtocolRefusal:
            raise
        except Exception as exc:
            raise ProtocolRefusal(
                "node_projection_managed_bus_missing",
                "managed bus shape could not be resolved",
            ) from exc
        if not isinstance(shape, ManagedVerbShape):
            _refuse(
                "node_projection_managed_bus_invalid",
                "managed bus source did not return a typed shape",
            )
        if shape.harness != harness:
            _refuse(
                "node_projection_managed_bus_invalid",
                "managed bus shape names another harness",
            )
        return shape, shape.record

    def _context(self) -> Dict[str, object]:
        selected, fleet = self._live_node_and_fleet()
        roots = self._declared_roots()
        for declaration in roots:
            if declaration["architect_node"] != fleet["architect_node"]:
                _refuse(
                    "node_projection_roots_invalid",
                    "declared root names a stale architect",
                )
        role = self._role()
        wake = self._wake()
        shape, managed_bus = self._managed_bus(str(selected["harness"]))
        workspace = self.root.path / "nodes" / str(selected["node_id"])
        state_file = workspace / "STATE.md"
        fleet["declared_roots"] = roots
        return {
            "root": str(self.root.path),
            "node_id": selected["node_id"],
            "harness": selected["harness"],
            "workspace": str(workspace),
            "state_file": str(state_file),
            "fleet_map": fleet,
            "role": role,
            "wake": wake,
            "managed_bus": managed_bus,
            "managed_shape": shape,
        }

    @staticmethod
    def _boot_prompt(context: Mapping[str, object]) -> str:
        fleet = context["fleet_map"]
        role = context["role"]
        wake = context["wake"]
        shape = context["managed_shape"]
        assert isinstance(fleet, Mapping)
        assert isinstance(role, Mapping)
        assert isinstance(wake, Mapping)
        assert isinstance(shape, ManagedVerbShape)
        lines = [
            f"Read your state file first: {context['state_file']}.",
            f"You are node {context['node_id']} using harness {context['harness']}.",
            f"Current architect: {fleet['architect_node']}.",
            "Current fleet nodes:",
        ]
        for node in fleet["nodes"]:
            lines.append(
                f"{node['node_id']} | role={node['role']} | "
                f"harness={node['harness']} | state={node['state']}"
            )
        lines.append("Declared roots:")
        for declaration in fleet["declared_roots"]:
            lines.append(
                f"{declaration['bus_id']} | root={declaration['root']} | "
                f"architect={declaration['architect_node']} | "
                f"downstream={','.join(declaration['downstream']) or 'none'}"
            )
        lines.extend(
            (
                f"Wake posture: {wake['status']}; poll at row boundaries: "
                f"{'yes' if wake['poll_at_row_boundaries'] else 'no'}.",
                f"Managed bus executable: {shape.executable}.",
                f"Managed bus profile: {shape.profile}.",
                f"Inbox verb: {shape.inbox_line()}.",
                f"Ack verb: {shape.ack_line()}.",
                f"Send verb: {shape.send_line()}.",
                f"Pause this exact session: floati wake pause --root {context['root']} "
                f"--as {context['node_id']} --session <session-id>.",
                f"Resume this exact session: floati wake resume --root {context['root']} "
                f"--as {context['node_id']} --session <session-id>.",
                f"Inspect this exact session: floati wake status --root {context['root']} "
                f"--as {context['node_id']} --session <session-id>.",
                "Role duties:",
            )
        )
        lines.extend(role["duties"])
        lines.append("Decision rights:")
        lines.extend(role["decision_rights"])
        lines.append("Stops (verbatim):")
        lines.extend(role["stops"])
        lines.append("Fences (verbatim):")
        lines.extend(role["fences"])
        lines.append("Cadence: " + str(role["cadence"]) + ".")
        lines.append("Interview answers:")
        for key, value in sorted(role["answers"].items()):
            lines.append(f"{key}: {value}")
        prompt = "\n".join(lines)
        try:
            prompt.encode("ascii")
        except UnicodeEncodeError as exc:
            raise ProtocolRefusal(
                "node_projection_output_invalid",
                "projection prompt contains non-ASCII text",
            ) from exc
        return prompt

    @staticmethod
    def _common_artifact(context: Mapping[str, object], kind: str) -> Dict[str, object]:
        return {
            "schema_version": 0,
            "kind": kind,
            "node_id": context["node_id"],
            "harness": context["harness"],
            "workspace": context["workspace"],
            "state_file": context["state_file"],
            "fleet_map": context["fleet_map"],
            "role": context["role"],
            "wake": context["wake"],
            "managed_bus": context["managed_bus"],
        }

    def project(self) -> Dict[str, object]:
        return self._artifact()

    def _artifact(self) -> Dict[str, object]:
        raise NotImplementedError

    def to_json(self) -> str:
        return json.dumps(
            self.project(),
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ) + "\n"


class NodeBootProjection(_NodeProjection):
    """Compose one boot prompt from the current ledger projection."""

    _kind = "node_boot_projection"

    def _artifact(self) -> Dict[str, object]:
        context = self._context()
        artifact = self._common_artifact(context, self._kind)
        prompt = self._boot_prompt(context)
        artifact["command"] = (
            f"Read your state file first: {artifact['state_file']}; "
            f"start the {artifact['harness']} seat for {artifact['node_id']} "
            "with the prompt below."
        )
        artifact["prompt"] = prompt
        return artifact


class NodeTeardownProjection(_NodeProjection):
    """Compose the state-preserving teardown ritual from live evidence."""

    _kind = "node_teardown_projection"

    @staticmethod
    def _ritual(context: Mapping[str, object]) -> list[Dict[str, str]]:
        shape = context["managed_shape"]
        assert isinstance(shape, ManagedVerbShape)
        state_file = str(context["state_file"])
        workspace = str(context["workspace"])
        return [
            {
                "kind": "read_state",
                "instruction": f"Read your state file first: {state_file}.",
            },
            {
                "kind": "flush_state",
                "instruction": f"flush seat state to {state_file} before teardown.",
            },
            {
                "kind": "check_committed_and_banked",
                "instruction": "check that every change is both committed and banked on the named ref.",
            },
            {
                "kind": "push_and_envelope_unbanked",
                "instruction": (
                    "push and envelope anything unbanked with the exact send verb: "
                    + shape.send_line()
                    + "."
                ),
            },
            {
                "kind": "report_drained",
                "instruction": "report DRAINED after the inbox is intentionally silent.",
            },
            {
                "kind": "close_lease",
                "instruction": "close the active lease through the train-owned lifecycle adapter, if one exists.",
            },
            {
                "kind": "retire_without_deleting_workspace",
                "instruction": f"retire node {context['node_id']} mechanically and never delete workspace {workspace}.",
            },
        ]

    def _artifact(self) -> Dict[str, object]:
        context = self._context()
        artifact: Dict[str, object] = self._common_artifact(context, self._kind)
        artifact["ritual"] = self._ritual(context)
        ritual = artifact["ritual"]
        assert isinstance(ritual, list)
        artifact["command"] = "; ".join(step["instruction"] for step in ritual)
        prompt = self._boot_prompt(context)
        artifact["prompt"] = prompt + "\nTeardown ritual order:\n" + "\n".join(
            step["instruction"] for step in ritual
        )
        return artifact


def render_node_projection(artifact: Mapping[str, object]) -> str:
    """Render the JSON artifact as a deterministic ASCII board."""

    if not isinstance(artifact, Mapping):
        _refuse("node_projection_output_invalid", "projection artifact must be an object")
    kind = artifact.get("kind")
    if kind not in {"node_boot_projection", "node_teardown_projection"}:
        _refuse("node_projection_output_invalid", "projection kind is invalid")
    title = "NODE BOOT PROJECTION" if kind == "node_boot_projection" else "NODE TEARDOWN PROJECTION"
    fleet = _mapping(
        artifact.get("fleet_map"),
        "node_projection_output_invalid",
        "projection fleet map is invalid",
    )
    wake = _mapping(
        artifact.get("wake"),
        "node_projection_output_invalid",
        "projection wake state is invalid",
    )
    managed = _mapping(
        artifact.get("managed_bus"),
        "node_projection_output_invalid",
        "projection managed bus is invalid",
    )
    def render_text(value: object, detail: str, *, multiline: bool = False) -> str:
        if not isinstance(value, str):
            _refuse("node_projection_output_invalid", detail)
        if any(
            (ord(character) < 32 and (not multiline or character != "\n"))
            or ord(character) == 127
            or unicodedata.bidirectional(character)
            in {"LRE", "RLE", "LRO", "RLO", "PDF", "LRI", "RLI", "FSI", "PDI", "BN"}
            for character in value
        ):
            _refuse("node_projection_output_invalid", detail)
        return value

    node_id = render_text(artifact.get("node_id"), "projection node id is invalid")
    harness = render_text(artifact.get("harness"), "projection harness is invalid")
    workspace = render_text(artifact.get("workspace"), "projection workspace is invalid")
    state_file = render_text(artifact.get("state_file"), "projection state file is invalid")
    architect = render_text(fleet.get("architect_node"), "projection architect is invalid")
    wake_status = render_text(wake.get("status"), "projection wake status is invalid")
    executable = render_text(managed.get("executable"), "projection executable is invalid")
    profile = render_text(managed.get("profile"), "projection profile is invalid")
    command = render_text(artifact.get("command"), "projection command is invalid")
    prompt = render_text(
        artifact.get("prompt"), "projection prompt is invalid", multiline=True
    )
    if command.startswith("DRAFT - ") or prompt.startswith("DRAFT - "):
        _refuse("node_projection_output_invalid", "projection copy still carries a DRAFT stamp")
    lines = [
        f"{title}",
        f"NODE: {node_id}",
        f"HARNESS: {harness}",
        f"WORKSPACE: {workspace}",
        f"STATE FILE: {state_file}",
        f"ARCHITECT: {architect}",
        f"WAKE: {wake_status}; poll at row boundaries: "
        f"{'yes' if wake.get('poll_at_row_boundaries') else 'no'}",
        f"MANAGED BUS: {executable} {profile}",
        f"COMMAND: {command}",
        "PROMPT:",
        prompt,
    ]
    rendered = "\n".join(lines) + "\n"
    try:
        rendered.encode("ascii")
    except UnicodeEncodeError as exc:
        raise ProtocolRefusal(
            "node_projection_output_invalid",
            "projection board contains non-ASCII text",
        ) from exc
    return rendered

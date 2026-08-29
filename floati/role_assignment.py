"""Preview-first registry role assignment step for the node wizard."""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterable, Mapping, Optional, Protocol, TextIO, Tuple

from .errors import ProtocolRefusal
from .records import validate_role
from .role_templates import RoleTemplate
from .root import FloatiRoot, validate_identifier


_UUID7_HEX = r"[0-9a-f]{12}7[0-9a-f]{3}[89ab][0-9a-f]{15}"
_ID_SUFFIX = re.compile(r"^" + _UUID7_HEX + r"$")
_REGISTRY_ID = re.compile(r"^registry-" + _UUID7_HEX + r"$")


def _timestamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ProtocolRefusal("wizard_clock_invalid", "wizard clock must be timezone-aware")
    return (
        value.astimezone(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _safe_answer(value: object) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= 500:
        raise ProtocolRefusal(
            "role_answer_invalid", "role answers must be text between 1 and 500 characters"
        )
    if any(
        unicodedata.category(character) in {"Cc", "Cs"}
        or unicodedata.bidirectional(character)
        in {"LRE", "RLE", "LRO", "RLO", "PDF", "LRI", "RLI", "FSI", "PDI", "BN"}
        for character in value
    ):
        raise ProtocolRefusal("role_answer_invalid", "role answer is terminal-unsafe")
    return value


@dataclass(frozen=True)
class RoleAssignmentPlan:
    node_id: str
    template_role: str
    record: Dict[str, Any]


class RoleAssignmentBackend(Protocol):
    """Train-owned adapter for registry reads and one role-record commit."""

    def active_node(self, node_id: str) -> Dict[str, Any]: ...

    def current_architect(self) -> Dict[str, Any]: ...

    def commit_role(self, plan: RoleAssignmentPlan) -> Dict[str, Any]: ...


class RoleStepWizard:
    """Collect only declared questions and preview the exact role record."""

    def __init__(
        self,
        root: FloatiRoot,
        backend: RoleAssignmentBackend,
        templates: Mapping[str, RoleTemplate],
        *,
        id_factory: Callable[[], str],
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        checked: Dict[str, RoleTemplate] = {}
        for name, template in templates.items():
            if not isinstance(name, str) or not isinstance(template, RoleTemplate):
                raise ProtocolRefusal(
                    "role_template_catalog_invalid", "role template catalog is malformed"
                )
            if name != template.role:
                raise ProtocolRefusal(
                    "role_template_catalog_invalid", "role template catalog key does not match role"
                )
            checked[name] = template
        if not checked:
            raise ProtocolRefusal(
                "role_template_catalog_invalid", "role template catalog is empty"
            )
        self.root = root
        self.backend = backend
        self.templates = checked
        self.id_factory = id_factory
        self.now = now

    def _id(self) -> str:
        suffix = self.id_factory()
        if not isinstance(suffix, str) or not _ID_SUFFIX.fullmatch(suffix):
            raise ProtocolRefusal(
                "wizard_id_invalid",
                "wizard id factory must return a lowercase UUIDv7 without hyphens",
            )
        return "registry-role-" + suffix

    def _template(self, value: object) -> RoleTemplate:
        try:
            name = validate_identifier(value, "role")
        except ProtocolRefusal as exc:
            raise ProtocolRefusal(
                "role_template_unknown", "selected role template is not shipped"
            ) from exc
        template = self.templates.get(name)
        if template is None:
            raise ProtocolRefusal(
                "role_template_unknown", "selected role template is not shipped"
            )
        return template

    def _registry_entry(
        self,
        record: object,
        *,
        code: str,
        expected_node: Optional[str] = None,
        require_architect: bool = False,
    ) -> Dict[str, Any]:
        if (
            not isinstance(record, dict)
            or record.get("schema_version") != 0
            or record.get("kind") != "registry_entry"
            or not isinstance(record.get("id"), str)
            or not _REGISTRY_ID.fullmatch(record["id"])
            or record.get("tenant_id") != self.root.tenant_id
            or record.get("state") != "active"
        ):
            raise ProtocolRefusal(code, "active registry evidence is invalid")
        try:
            node = validate_identifier(record.get("node_id"), "node")
            role = validate_role(record.get("role"))
        except ProtocolRefusal as exc:
            raise ProtocolRefusal(code, "active registry evidence is invalid") from exc
        if expected_node is not None and node != expected_node:
            raise ProtocolRefusal(code, "active registry evidence names another node")
        if require_architect and role.casefold() != "architect":
            raise ProtocolRefusal(code, "current architect evidence has another role")
        return dict(record)

    @staticmethod
    def _input(values: Iterable[str]) -> Tuple[str, ...]:
        try:
            answers = tuple(values)
        except TypeError as exc:
            raise ProtocolRefusal("wizard_input_invalid", "wizard answers are required") from exc
        if any(not isinstance(value, str) for value in answers):
            raise ProtocolRefusal("wizard_input_invalid", "wizard answers must be text")
        return answers

    def _plan(self, values: Iterable[str]) -> RoleAssignmentPlan:
        provided = self._input(values)
        if len(provided) < 2:
            raise ProtocolRefusal(
                "wizard_input_invalid", "role step requires a node and template"
            )
        node = validate_identifier(provided[0].strip(), "node")
        template = self._template(provided[1].strip())
        if len(provided) != 2 + len(template.questions):
            raise ProtocolRefusal(
                "wizard_input_invalid", "role step answer count must match declared questions"
            )

        resolved: Dict[str, str] = {}
        for question, raw_value in zip(template.questions, provided[2:]):
            candidate = raw_value.strip()
            if candidate:
                resolved[question.key] = _safe_answer(candidate)
                continue
            if question.default is None:
                raise ProtocolRefusal(
                    "role_answer_required", f"role answer {question.key} is required"
                )
            if question.default == "<architect>":
                architect = self._registry_entry(
                    self.backend.current_architect(),
                    code="role_architect_invalid",
                    require_architect=True,
                )
                resolved[question.key] = str(architect["node_id"])
            else:
                resolved[question.key] = _safe_answer(question.default)

        self._registry_entry(
            self.backend.active_node(node),
            code="role_assignment_invalid",
            expected_node=node,
        )
        record: Dict[str, Any] = {
            "schema_version": 0,
            "id": self._id(),
            "tenant_id": self.root.tenant_id,
            "timestamp": _timestamp(self.now()),
            "kind": "registry_role_record",
            "node_id": node,
            "template_role": template.role,
            "template_version": template.template_version,
            "template_sha256": template.digest,
            "answers": resolved,
            "state": "active",
            "predecessor_role_record_id": None,
        }
        return RoleAssignmentPlan(
            node_id=node,
            template_role=template.role,
            record=record,
        )

    @staticmethod
    def _preview(record: Dict[str, Any], output: TextIO) -> None:
        output.write(
            "ledger preview: "
            + json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            + "\n"
        )
        output.flush()

    def assign_from_keys(self, values: Iterable[str], output: TextIO) -> Dict[str, Any]:
        plan = self._plan(values)
        self._preview(plan.record, output)
        return dict(self.backend.commit_role(plan))

    def assign_plain(self, input_stream: TextIO, output: TextIO) -> Dict[str, Any]:
        answers = []
        for prompt in ("node id: ", "role template: "):
            output.write(prompt)
            output.flush()
            value = input_stream.readline()
            if value == "":
                raise ProtocolRefusal("wizard_input_closed", "plain input ended before preview")
            answers.append(value.rstrip("\r\n"))
        template = self._template(answers[1].strip())
        for question in template.questions:
            output.write(question.ask + " ")
            output.flush()
            value = input_stream.readline()
            if value == "":
                raise ProtocolRefusal("wizard_input_closed", "plain input ended before preview")
            answers.append(value.rstrip("\r\n"))
        output.write("\n")
        return self.assign_from_keys(answers, output)

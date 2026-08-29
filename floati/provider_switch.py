"""Preview-first node harness/model reassignment wizard."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterable, Optional, Protocol, TextIO, Tuple

from .errors import ProtocolRefusal
from .records import validate_role
from .root import FloatiRoot, validate_identifier


_UUID7_HEX = r"[0-9a-f]{12}7[0-9a-f]{3}[89ab][0-9a-f]{15}"
_ID_SUFFIX = re.compile(r"^" + _UUID7_HEX + r"$")
_REGISTRY_ID = re.compile(r"^registry-" + _UUID7_HEX + r"$")
_MODEL = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._:/-]{0,126}[A-Za-z0-9])?$")


def _timestamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ProtocolRefusal("wizard_clock_invalid", "wizard clock must be timezone-aware")
    return (
        value.astimezone(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def validate_model(value: object) -> str:
    if not isinstance(value, str) or not _MODEL.fullmatch(value):
        raise ProtocolRefusal(
            "model_invalid",
            "model must be 1-128 ASCII letters, digits, dots, underscores, colons, slashes, or hyphens",
        )
    return value


@dataclass(frozen=True)
class ProviderSwitchPlan:
    node_id: str
    harness: str
    model: str
    records: Tuple[Dict[str, Any], ...]


class ProviderSwitchBackend(Protocol):
    """Train-owned adapter for one registry reassignment transaction."""

    def active_assignment(self, node_id: str) -> Dict[str, Any]: ...

    def commit_switch(self, plan: ProviderSwitchPlan) -> Dict[str, Any]: ...


class ProviderSwitchWizard:
    """Preview one registry row and its receipt before delegating one commit."""

    def __init__(
        self,
        root: FloatiRoot,
        backend: ProviderSwitchBackend,
        *,
        id_factory: Callable[[], str],
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self.root = root
        self.backend = backend
        self.id_factory = id_factory
        self.now = now

    def _id(self, prefix: str) -> str:
        suffix = self.id_factory()
        if not isinstance(suffix, str) or not _ID_SUFFIX.fullmatch(suffix):
            raise ProtocolRefusal(
                "wizard_id_invalid",
                "wizard id factory must return a lowercase UUIDv7 without hyphens",
            )
        return prefix + suffix

    @staticmethod
    def _answers(values: Iterable[str]) -> Tuple[str, ...]:
        try:
            answers = tuple(values)
        except TypeError as exc:
            raise ProtocolRefusal("wizard_input_invalid", "wizard answers are required") from exc
        if len(answers) != 3 or any(not isinstance(value, str) for value in answers):
            raise ProtocolRefusal(
                "wizard_input_invalid",
                "provider switch requires exactly node, harness, and model text",
            )
        return answers

    def _plan(self, values: Iterable[str]) -> ProviderSwitchPlan:
        answers = self._answers(values)
        node = validate_identifier(answers[0].strip(), "node")
        harness = validate_role(answers[1].strip())
        model = validate_model(answers[2].strip())
        active = self.backend.active_assignment(node)
        if (
            not isinstance(active, dict)
            or active.get("schema_version") != 0
            or active.get("kind") != "registry_entry"
            or not isinstance(active.get("id"), str)
            or not _REGISTRY_ID.fullmatch(active["id"])
            or active.get("node_id") != node
            or active.get("tenant_id") != self.root.tenant_id
            or active.get("state") != "active"
        ):
            raise ProtocolRefusal(
                "provider_assignment_invalid",
                "active assignment evidence does not belong to this fleet root",
            )
        try:
            previous_harness = validate_role(active.get("role"))
            previous_model_value: Optional[object] = active.get("model")
            previous_model = (
                None if previous_model_value is None else validate_model(previous_model_value)
            )
        except ProtocolRefusal as exc:
            raise ProtocolRefusal(
                "provider_assignment_invalid", "active assignment evidence is malformed"
            ) from exc
        if previous_harness == harness and previous_model == model:
            raise ProtocolRefusal(
                "provider_switch_unchanged", "requested harness and model are already active"
            )

        timestamp = _timestamp(self.now())
        registry_record: Dict[str, Any] = {
            "schema_version": 0,
            "id": self._id("registry-"),
            "tenant_id": self.root.tenant_id,
            "timestamp": timestamp,
            "kind": "registry_entry",
            "node_id": node,
            "role": harness,
            "state": "active",
        }
        receipt: Dict[str, Any] = {
            "schema_version": 0,
            "id": self._id("provider-switch-receipt-"),
            "tenant_id": self.root.tenant_id,
            "timestamp": timestamp,
            "kind": "provider_switch_receipt",
            "node_id": node,
            "previous_registry_entry_id": active["id"],
            "previous_harness": previous_harness,
            "previous_model": previous_model,
            "harness": harness,
            "model": model,
            "registry_entry_id": registry_record["id"],
        }
        return ProviderSwitchPlan(
            node_id=node,
            harness=harness,
            model=model,
            records=(registry_record, receipt),
        )

    @staticmethod
    def _preview(records: Tuple[Dict[str, Any], ...], output: TextIO) -> None:
        for record in records:
            output.write(
                "ledger preview: "
                + json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                + "\n"
            )
        output.flush()

    def switch_from_keys(self, values: Iterable[str], output: TextIO) -> Dict[str, Any]:
        plan = self._plan(values)
        self._preview(plan.records, output)
        return dict(self.backend.commit_switch(plan))

    def switch_plain(self, input_stream: TextIO, output: TextIO) -> Dict[str, Any]:
        answers = []
        for prompt in (
            "node id to reassign: ",
            "new harness: ",
            "new model: ",
        ):
            output.write(prompt)
            output.flush()
            value = input_stream.readline()
            if value == "":
                raise ProtocolRefusal("wizard_input_closed", "plain input ended before preview")
            answers.append(value.rstrip("\r\n"))
        output.write("\n")
        return self.switch_from_keys(answers, output)

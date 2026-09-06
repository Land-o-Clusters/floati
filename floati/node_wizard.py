"""Keyboard/plain node lifecycle wizard with preview-before-commit plans."""

from __future__ import annotations

import json
import re
import shlex
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Optional, Protocol, TextIO, Tuple

from .errors import ProtocolRefusal
from .foreign_bus_survey import (
    ForeignBusSurvey,
    nested_buses_out_of_scope,
    undeclared_buses_in_scope,
)
from .records import validate_role
from .registry import Registry
from .root import FloatiRoot, validate_identifier
from .seat_declaration import FleetGovernance
from .tide_catalog import policy_metric_for, policy_metrics_for
from .tide_policy import normalize_threshold


_ID_SUFFIX = re.compile(r"^[0-9a-f]{32}$")
_RETIRE_NOTICE = "Teardown retires the node and retains its workspace."
_SURVEY_OFFER_PROMPT = "undeclared bus in scope; run read-only survey? "
_ADOPT_PROMPT = "adopt and continue node add? "


def _yes_no(value: str) -> bool:
    answer = value.strip().casefold()
    if answer in {"yes", "y"}:
        return True
    if answer in {"no", "n"}:
        return False
    raise ProtocolRefusal(
        "wizard_input_invalid", "survey and adopt answers must be yes or no"
    )


class _ImmutableRecord(Mapping[str, Any]):
    """Recursively immutable planned record with stable mapping order."""

    __slots__ = ("_items",)

    def __init__(self, values: Mapping[str, Any]) -> None:
        object.__setattr__(
            self,
            "_items",
            tuple((key, _freeze_record_value(value)) for key, value in values.items()),
        )

    def __setattr__(self, name: str, value: Any) -> None:
        del name, value
        raise AttributeError("planned records are immutable")

    def __delattr__(self, name: str) -> None:
        del name
        raise AttributeError("planned records are immutable")

    def __getitem__(self, key: str) -> Any:
        for candidate, value in self._items:
            if candidate == key:
                return value
        raise KeyError(key)

    def __iter__(self):
        return (key for key, _ in self._items)

    def __len__(self) -> int:
        return len(self._items)


def _freeze_record_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return _ImmutableRecord(value)
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_record_value(item) for item in value)
    return value


def _timestamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ProtocolRefusal("wizard_clock_invalid", "wizard clock must be timezone-aware")
    return (
        value.astimezone(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _one_line(parts: Iterable[str]) -> str:
    return " ".join(shlex.quote(value) for value in parts)


@dataclass(frozen=True)
class NodeAddPlan:
    node_id: str
    harness: str
    lifetime: str
    lease_minutes: Optional[int]
    workspace: str
    records: Tuple[Mapping[str, Any], ...]
    boot_command: Optional[str]
    teardown_command: Optional[str]
    governance: Optional[FleetGovernance] = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "records",
            tuple(_ImmutableRecord(record) for record in self.records),
        )


@dataclass(frozen=True)
class NodeRetirePlan:
    node_id: str
    workspace: str
    records: Tuple[Dict[str, Any], ...]


class NodeMutationBackend(Protocol):
    """Train-owned adapter that commits previewed records through existing verbs."""

    def active_node(self, node_id: str) -> Dict[str, Any]: ...

    def active_lease(self, node_id: str) -> Optional[Dict[str, Any]]: ...

    def commit_add(self, plan: NodeAddPlan) -> Dict[str, Any]: ...

    def commit_retire(self, plan: NodeRetirePlan) -> Dict[str, Any]: ...


class NodeWizard:
    """Collect lifecycle choices, preview exact rows, then delegate one commit."""

    def __init__(
        self,
        root: FloatiRoot,
        backend: NodeMutationBackend,
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
                "wizard_id_invalid", "wizard id factory must return 32 lowercase hex characters"
            )
        return prefix + suffix

    @staticmethod
    def _answers(values: Iterable[str]) -> Tuple[str, ...]:
        try:
            answers = tuple(values)
        except TypeError as exc:
            raise ProtocolRefusal("wizard_input_invalid", "wizard answers are required") from exc
        if any(not isinstance(value, str) for value in answers):
            raise ProtocolRefusal("wizard_input_invalid", "wizard answers must be text")
        return answers

    def _add_plan(self, values: Iterable[str]) -> NodeAddPlan:
        answers = self._answers(values)
        if len(answers) not in (3, 4):
            raise ProtocolRefusal(
                "wizard_input_invalid",
                "node add requires node, harness, lifetime, and a temporary lease length",
            )
        node = validate_identifier(answers[0].strip(), "node")
        harness = validate_role(answers[1].strip())
        lifetime = answers[2].strip().lower()
        if lifetime not in {"permanent", "temporary"}:
            raise ProtocolRefusal(
                "node_lifetime_invalid", "lifetime must be permanent or temporary"
            )
        if lifetime == "permanent" and len(answers) != 3:
            raise ProtocolRefusal(
                "node_lease_invalid", "permanent nodes do not accept a lease length"
            )
        lease_minutes: Optional[int] = None
        if lifetime == "temporary":
            if len(answers) != 4:
                raise ProtocolRefusal(
                    "node_lease_invalid", "temporary nodes require a lease length"
                )
            try:
                lease_minutes = int(answers[3], 10)
            except ValueError as exc:
                raise ProtocolRefusal(
                    "node_lease_invalid", "lease minutes must be an integer"
                ) from exc
            if lease_minutes < 1 or lease_minutes > 10080:
                raise ProtocolRefusal(
                    "node_lease_invalid", "lease minutes must be between 1 and 10080"
                )

        observed = self.now()
        timestamp = _timestamp(observed)
        workspace = str(self.root.path / "nodes" / node)
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
        records = [registry_record]
        boot_command: Optional[str] = None
        teardown_command: Optional[str] = None
        if lease_minutes is not None:
            lease_id = self._id("lease-")
            records.append(
                {
                    "schema_version": 0,
                    "id": lease_id,
                    "tenant_id": self.root.tenant_id,
                    "timestamp": timestamp,
                    "kind": "node_lease",
                    "node_id": node,
                    "workspace": workspace,
                    "expires_at": _timestamp(observed + timedelta(minutes=lease_minutes)),
                    "state": "active",
                }
            )
            boot_command = _one_line(
                (
                    "floati", "node", "boot", "--root", str(self.root.path),
                    "--node", node, "--declared-roots", "<declared-roots-file>",
                    "--managed-executable", "<managed-bus-executable>",
                    "--profile", "<managed-profile>",
                )
            )
            teardown_command = _one_line(
                (
                    "floati", "node", "retire", "--root", str(self.root.path),
                    "--node", node,
                )
            )
        return NodeAddPlan(
            node_id=node,
            harness=harness,
            lifetime=lifetime,
            lease_minutes=lease_minutes,
            workspace=workspace,
            records=tuple(records),
            boot_command=boot_command,
            teardown_command=teardown_command,
            governance=Registry(self.root).governance(),
        )

    def plan_add(self, values: Iterable[str]) -> NodeAddPlan:
        """Build one immutable node-add transaction before it is shown or committed."""
        return self._add_plan(values)

    def _retire_plan(self, values: Iterable[str]) -> NodeRetirePlan:
        answers = self._answers(values)
        if len(answers) != 1:
            raise ProtocolRefusal("wizard_input_invalid", "node retire requires one node id")
        node = validate_identifier(answers[0].strip(), "node")
        active = self.backend.active_node(node)
        if (
            active.get("node_id") != node
            or active.get("tenant_id") != self.root.tenant_id
            or active.get("state") != "active"
        ):
            raise ProtocolRefusal("unknown_node", "node is not active in this fleet root")
        role = validate_role(active.get("role"))
        timestamp = _timestamp(self.now())
        records = [
            {
                "schema_version": 0,
                "id": self._id("registry-"),
                "tenant_id": self.root.tenant_id,
                "timestamp": timestamp,
                "kind": "registry_entry",
                "node_id": node,
                "role": role,
                "state": "retired",
            }
        ]
        lease = self.backend.active_lease(node)
        if lease is not None:
            lease_id = lease.get("id")
            if (
                not isinstance(lease_id, str)
                or lease.get("node_id") != node
                or lease.get("tenant_id") != self.root.tenant_id
                or lease.get("state") != "active"
            ):
                raise ProtocolRefusal("node_lease_invalid", "active lease evidence is invalid")
            records.append(
                {
                    "schema_version": 0,
                    "id": self._id("lease-"),
                    "tenant_id": self.root.tenant_id,
                    "timestamp": timestamp,
                    "kind": "node_lease",
                    "node_id": node,
                    "predecessor_lease_id": lease_id,
                    "workspace": str(self.root.path / "nodes" / node),
                    "state": "retired",
                }
            )
        return NodeRetirePlan(
            node_id=node,
            workspace=str(self.root.path / "nodes" / node),
            records=tuple(records),
        )

    @staticmethod
    def _preview(records: Tuple[Mapping[str, Any], ...], output: TextIO) -> None:
        for record in records:
            output.write(
                "ledger preview: "
                + json.dumps(dict(record), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                + "\n"
            )
        output.flush()

    def render_add_preview(self, plan: NodeAddPlan) -> str:
        """Return the exact compact ledger rows represented by one add plan."""
        if not isinstance(plan, NodeAddPlan):
            raise ProtocolRefusal("wizard_plan_invalid", "node add preview requires one add plan")
        return "".join(
            "ledger preview: "
            + json.dumps(
                dict(record), ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
            + "\n"
            for record in plan.records
        )

    def commit_add(self, plan: NodeAddPlan, output: TextIO) -> Dict[str, Any]:
        """Flush one rendered plan, then commit that same immutable object once."""
        if not isinstance(plan, NodeAddPlan):
            raise ProtocolRefusal("wizard_plan_invalid", "node add commit requires one add plan")
        output.write(self.render_add_preview(plan))
        output.flush()
        result = dict(self.backend.commit_add(plan))
        result.update(
            {
                "workspace": plan.workspace,
                "lifetime": plan.lifetime,
                "boot_command": plan.boot_command,
                "teardown_command": plan.teardown_command,
                "tide_metrics": [metric.name for metric in policy_metrics_for(plan.harness)],
            }
        )
        return result

    def add_from_keys(self, values: Iterable[str], output: TextIO) -> Dict[str, Any]:
        return self.commit_add(self.plan_add(values), output)

    _PLAN_REQUIRED = ("node", "harness", "lifetime")
    _PLAN_OPTIONAL = frozenset({"lease_minutes", "survey", "adopt"})
    _PLAN_REMEDY = (
        "write one JSON object with node, harness, lifetime, "
        "lease_minutes when temporary, and optional survey and adopt booleans"
    )

    def values_from_plan(self, payload: Mapping[str, Any]) -> Tuple[str, ...]:
        """Translate one plan object into the same answers `add_from_keys` takes."""

        if not isinstance(payload, Mapping):
            raise ProtocolRefusal(
                "node_add_plan_invalid",
                "plan must be one JSON object",
                self._PLAN_REMEDY,
            )
        unknown = sorted(set(payload) - set(self._PLAN_REQUIRED) - self._PLAN_OPTIONAL)
        if unknown:
            raise ProtocolRefusal(
                "node_add_plan_invalid",
                "plan has unknown fields: " + ", ".join(unknown),
                self._PLAN_REMEDY,
            )
        missing = [key for key in self._PLAN_REQUIRED if key not in payload]
        if missing:
            raise ProtocolRefusal(
                "node_add_plan_invalid",
                "plan requires node, harness, and lifetime",
                self._PLAN_REMEDY,
            )
        node = payload["node"]
        harness = payload["harness"]
        lifetime = payload["lifetime"]
        if not all(isinstance(value, str) for value in (node, harness, lifetime)):
            raise ProtocolRefusal(
                "node_add_plan_invalid",
                "plan node, harness, and lifetime must be text",
                self._PLAN_REMEDY,
            )
        values = [node, harness, lifetime]
        if "lease_minutes" in payload:
            minutes = payload["lease_minutes"]
            if isinstance(minutes, bool) or not isinstance(minutes, int):
                raise ProtocolRefusal(
                    "node_add_plan_invalid",
                    "plan lease_minutes must be an integer",
                    self._PLAN_REMEDY,
                )
            values.append(str(minutes))
        return tuple(values)

    def _plan_flag(self, payload: Mapping[str, Any], key: str) -> Optional[bool]:
        if key not in payload:
            return None
        value = payload[key]
        if value is not True and value is not False:
            raise ProtocolRefusal(
                "wizard_input_invalid",
                f"plan {key} flag must be boolean",
                f"write plan {key} as a JSON boolean true or false",
            )
        return value

    def add_from_plan(self, payload: Mapping[str, Any], output: TextIO) -> Dict[str, Any]:
        """Commit the same add as the interactive wizard, from one plan object."""

        values = self.values_from_plan(payload)
        extra = self._offer_survey_and_adopt(
            output,
            survey=self._plan_flag(payload, "survey"),
            adopt=self._plan_flag(payload, "adopt"),
        )
        result = self.add_from_keys(values, output)
        result.update(extra)
        return result

    def _offer_survey_and_adopt(
        self,
        output: TextIO,
        *,
        survey: Optional[bool],
        adopt: Optional[bool],
        input_stream: Optional[TextIO] = None,
    ) -> Dict[str, Any]:
        found = undeclared_buses_in_scope(self.root.path)
        extra: Dict[str, Any] = {}
        if not found:
            nested = nested_buses_out_of_scope(self.root.path)
            return {
                "undeclared_in_scope": [],
                "survey": "not_offered",
                "reason": (
                    "nested_out_of_scope" if nested else "none_in_scope"
                ),
            }
        extra["undeclared_in_scope"] = [
            {"root": path, "apparent_schema": schema} for path, schema in found
        ]
        run_survey = survey
        if run_survey is None:
            run_survey = _yes_no(
                self._read_plain(
                    input_stream,
                    output,
                    _SURVEY_OFFER_PROMPT,
                    "plain input ended before survey offer",
                )
            )
        if run_survey:
            artifact = ForeignBusSurvey.around_live_root(self.root.path).run()
            extra["survey"] = artifact
            output.write(artifact["notice"] + "\n")
            for entry in artifact["foreign_buses"]:
                output.write("survey: " + entry["root"] + "\n")
            output.flush()
        consent = adopt
        if consent is None:
            consent = _yes_no(
                self._read_plain(
                    input_stream,
                    output,
                    _ADOPT_PROMPT,
                    "plain input ended before adopt",
                )
            )
        if not consent:
            raise ProtocolRefusal(
                "wizard_undeclared_bus_not_adopted",
                "node add waits for adopt consent when an undeclared bus is in scope",
            )
        extra["adopted"] = True
        return extra

    @staticmethod
    def _read_plain(
        input_stream: Optional[TextIO],
        output: TextIO,
        prompt: str,
        closed_detail: str,
    ) -> str:
        if input_stream is None:
            raise ProtocolRefusal(
                "wizard_input_invalid",
                "survey and adopt choices are required when an undeclared bus is in scope",
            )
        output.write(prompt)
        output.flush()
        value = input_stream.readline()
        if value == "":
            raise ProtocolRefusal("wizard_input_closed", closed_detail)
        return value.rstrip("\r\n")

    def add_plain(self, input_stream: TextIO, output: TextIO) -> Dict[str, Any]:
        answers = []
        for prompt in (
            "node id: ",
            "harness: ",
            "lifetime (permanent/temporary): ",
        ):
            output.write(prompt)
            output.flush()
            value = input_stream.readline()
            if value == "":
                raise ProtocolRefusal("wizard_input_closed", "plain input ended before preview")
            answers.append(value.rstrip("\r\n"))
        if answers[2].strip().lower() == "temporary":
            output.write("lease minutes: ")
            output.flush()
            value = input_stream.readline()
            if value == "":
                raise ProtocolRefusal("wizard_input_closed", "plain input ended before preview")
            answers.append(value.rstrip("\r\n"))
        output.write("tide metric (blank for off): ")
        output.flush()
        value = input_stream.readline()
        tide_metric = "" if value == "" else value.rstrip("\r\n").strip()
        tide: Optional[tuple[str, str, str]] = None
        if tide_metric:
            selected = policy_metric_for(answers[1], tide_metric)
            output.write("tide threshold: ")
            output.flush()
            threshold_value = input_stream.readline()
            if threshold_value == "":
                raise ProtocolRefusal("wizard_input_closed", "plain input ended before tide preview")
            threshold = threshold_value.rstrip("\r\n").strip()
            normalize_threshold(threshold, selected)
            output.write("tide action (recommend/direct): ")
            output.flush()
            action_value = input_stream.readline()
            if action_value == "":
                raise ProtocolRefusal("wizard_input_closed", "plain input ended before tide preview")
            action = action_value.rstrip("\r\n").strip().casefold()
            if action not in {"recommend", "direct"}:
                raise ProtocolRefusal(
                    "tide_action_not_supported",
                    "T1 authorizes recommend or direct; no native non-interactive compact verb was measured",
                )
            tide = (selected.name, threshold, action)
        extra = self._offer_survey_and_adopt(
            output,
            input_stream=input_stream,
            survey=None,
            adopt=None,
        )
        output.write("\n")
        result = self.add_from_keys(answers, output)
        result.update(extra)
        if tide is not None:
            from .tide_policy import TidePolicyLedger

            result["tide_policy"] = TidePolicyLedger(self.root).set(
                answers[0], tide[0], tide[1], tide[2],
                idempotency_key=self._id("wizard-tide-"),
            )
        return result

    def retire_from_keys(self, values: Iterable[str], output: TextIO) -> Dict[str, Any]:
        plan = self._retire_plan(values)
        self._preview(plan.records, output)
        result = dict(self.backend.commit_retire(plan))
        result.update({"workspace": plan.workspace, "notice": _RETIRE_NOTICE})
        return result

    def retire_plain(self, input_stream: TextIO, output: TextIO) -> Dict[str, Any]:
        output.write("node id to retire: ")
        output.flush()
        value = input_stream.readline()
        if value == "":
            raise ProtocolRefusal("wizard_input_closed", "plain input ended before preview")
        output.write("\n")
        return self.retire_from_keys([value.rstrip("\r\n")], output)

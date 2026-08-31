"""Keyboard/plain node lifecycle wizard with preview-before-commit plans."""

from __future__ import annotations

import json
import re
import shlex
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Optional, Protocol, TextIO, Tuple

from .errors import ProtocolRefusal
from .records import validate_role
from .registry import Registry
from .root import FloatiRoot, validate_identifier
from .seat_declaration import FleetGovernance
from .tide_catalog import policy_metric_for, policy_metrics_for
from .tide_policy import normalize_threshold


_ID_SUFFIX = re.compile(r"^[0-9a-f]{32}$")
_RETIRE_NOTICE = "Teardown retires the node and retains its workspace."


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
    records: Tuple[Dict[str, Any], ...]
    boot_command: Optional[str]
    teardown_command: Optional[str]
    governance: Optional[FleetGovernance] = None


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
    def _preview(records: Tuple[Dict[str, Any], ...], output: TextIO) -> None:
        for record in records:
            output.write(
                "ledger preview: "
                + json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                + "\n"
            )
        output.flush()

    def add_from_keys(self, values: Iterable[str], output: TextIO) -> Dict[str, Any]:
        plan = self._add_plan(values)
        self._preview(plan.records, output)
        result = dict(self.backend.commit_add(plan))
        result.update(
            {
                "workspace": plan.workspace,
                "lifetime": plan.lifetime,
                "boot_command": plan.boot_command,
                "teardown_command": plan.teardown_command,
                "tide_metrics": [
                    metric.name for metric in policy_metrics_for(plan.harness)
                ],
            }
        )
        return result

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
        output.write("\n")
        result = self.add_from_keys(answers, output)
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

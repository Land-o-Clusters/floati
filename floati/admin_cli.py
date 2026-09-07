"""Reconciled registrations for the WS-B and WS-D admin surfaces."""

from __future__ import annotations

import argparse
import io
import json
import signal
import sys
import threading
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Tuple

from .admin_registry import RegistryAdminBackend
from .copy import TUI_DOOR_COPY
from .foreign_bus_survey import ForeignBusSurvey
from .ids import uuid7_hex
from .multi_bus_chart import MultiBusHarborChart, render_multi_bus_chart
from .multi_bus_chart import DeclaredRoots
from .node_explain import NodeExplainProjection
from .node_projections import (
    ManagedVerbShape,
    NodeBootProjection,
    NodeTeardownProjection,
)
from .node_wizard import NodeWizard
from .provider_switch import ProviderSwitchWizard
from .role_assignment import RoleStepWizard
from .role_templates import RoleTemplate
from .root import FloatiRoot, resolve_command_root
from .workspace_layout import register_node, retire_node
from .state_receipts import record_state_flush


HandlerResult = Tuple[str, Dict[str, Any], int]
OK = 0
_NODE_ADD_PLAN_REMEDY = (
    "pass --root ROOT --plan FILE with an absolute JSON object "
    "{node, harness, lifetime, lease_minutes when temporary, "
    "and optional survey and adopt booleans}"
)
_NODE_ADD_FULLY_FLAGGED_REMEDY = TUI_DOOR_COPY[
    "tui.door.node_add_fully_flagged_remedy"
]


def _root(path: str | None) -> FloatiRoot:
    return resolve_command_root(path, create=False)


def _templates(root: FloatiRoot) -> Dict[str, RoleTemplate]:
    from .role_library import RoleTemplateLibrary

    return RoleTemplateLibrary(root).templates()


def _previewed(result: Mapping[str, Any], preview: io.StringIO) -> Dict[str, Any]:
    evidence = dict(result)
    rows = []
    prefix = "ledger preview: "
    for line in preview.getvalue().splitlines():
        if line.startswith(prefix):
            rows.append(json.loads(line[len(prefix) :]))
    evidence["preview_rows"] = rows
    return evidence


def _reject_duplicate_plan_keys(pairs: Iterable[Tuple[str, Any]]) -> Dict[str, Any]:
    from .errors import ProtocolRefusal

    payload: Dict[str, Any] = {}
    for key, value in pairs:
        if key in payload:
            raise ProtocolRefusal(
                "node_add_plan_invalid",
                "plan must not repeat keys",
                _NODE_ADD_PLAN_REMEDY,
            )
        payload[key] = value
    return payload


def _load_node_add_plan(path: str) -> Dict[str, Any]:
    from .errors import ProtocolRefusal

    candidate = Path(path)
    if not candidate.is_absolute():
        raise ProtocolRefusal(
            "node_add_plan_path_not_absolute",
            "plan path must be absolute",
            _NODE_ADD_PLAN_REMEDY,
        )
    try:
        if candidate.is_symlink() or not candidate.is_file():
            raise ProtocolRefusal(
                "node_add_plan_path_invalid",
                "plan path must be a regular file",
                _NODE_ADD_PLAN_REMEDY,
            )
        raw = json.loads(
            candidate.read_text(encoding="utf-8"),
            object_pairs_hook=_reject_duplicate_plan_keys,
        )
    except ProtocolRefusal:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ProtocolRefusal(
            "node_add_plan_invalid",
            "plan must be one readable JSON object",
            _NODE_ADD_PLAN_REMEDY,
        ) from exc
    if not isinstance(raw, dict):
        raise ProtocolRefusal(
            "node_add_plan_invalid",
            "plan must be one JSON object",
            _NODE_ADD_PLAN_REMEDY,
        )
    return raw


def _node_add_from_plan(args: argparse.Namespace) -> HandlerResult:
    from .errors import ProtocolRefusal

    identity_flags = (args.node, args.harness, args.lifetime, args.lease_minutes)
    tide_flags = (
        args.tide_metric,
        args.tide_threshold,
        args.tide_action,
        args.tide_idempotency_key,
    )
    if any(value is not None for value in identity_flags + tide_flags):
        raise ProtocolRefusal(
            "arguments_invalid",
            "node add --plan cannot be combined with identity or tide flags",
            _NODE_ADD_PLAN_REMEDY,
        )
    if args.root is None:
        raise ProtocolRefusal(
            "arguments_invalid",
            "node add --plan requires --root",
            _NODE_ADD_PLAN_REMEDY,
        )
    root = _root(args.root)
    preview = io.StringIO()
    wizard = NodeWizard(
        root,
        RegistryAdminBackend(root, repository=Path.cwd()),
        id_factory=uuid7_hex,
    )
    result = wizard.add_from_plan(_load_node_add_plan(args.plan), preview)
    return "ok", _previewed(result, preview), OK


def _register_node(args: argparse.Namespace) -> HandlerResult:
    evidence = register_node(
        _root(args.root),
        args.node,
        args.harness,
        create_workspace=args.create_workspace,
    )
    return "ok", dict(evidence["registry"], workspace=evidence["workspace"]), OK


def _retire_node(args: argparse.Namespace) -> HandlerResult:
    evidence = retire_node(_root(args.root), args.node)
    return "ok", dict(evidence["registry"], workspace=evidence["workspace"]), OK


def _node_add(args: argparse.Namespace) -> HandlerResult:
    plan = getattr(args, "plan", None)
    required_values = (args.root, args.node, args.harness, args.lifetime)
    option_values = (
        args.lease_minutes,
        args.tide_metric,
        args.tide_threshold,
        args.tide_action,
        args.tide_idempotency_key,
    )
    if plan is not None:
        return _node_add_from_plan(args)
    interactive = not any(value is not None for value in required_values + option_values)
    if not interactive and not all(value is not None for value in required_values):
        from .errors import ProtocolRefusal

        raise ProtocolRefusal(
            "arguments_invalid",
            TUI_DOOR_COPY["tui.door.node_add_shape_invalid"],
            _NODE_ADD_FULLY_FLAGGED_REMEDY,
        )
    if interactive:
        from .tui_doors import DoorTerminalIOError, require_door_terminal

        try:
            require_door_terminal(
                sys.stdin,
                sys.stderr,
                remedy=_NODE_ADD_FULLY_FLAGGED_REMEDY,
            )
        except DoorTerminalIOError as exc:
            from .errors import ProtocolRefusal

            raise ProtocolRefusal(
                "door_terminal_io_failed",
                TUI_DOOR_COPY["tui.door.terminal_io_failed"],
                _NODE_ADD_FULLY_FLAGGED_REMEDY,
            ) from exc
    root = _root(args.root)
    tide_values = (args.tide_metric, args.tide_threshold, args.tide_action)
    if any(value is not None for value in tide_values) and not all(
        value is not None for value in tide_values
    ):
        from .errors import ProtocolRefusal

        raise ProtocolRefusal(
            "arguments_invalid",
            TUI_DOOR_COPY["tui.door.tide_fields_incomplete"],
        )
    if all(value is not None for value in tide_values):
        from .tide_catalog import policy_metric_for
        from .tide_policy import normalize_threshold

        normalize_threshold(
            args.tide_threshold,
            policy_metric_for(args.harness, args.tide_metric),
        )
    preview = io.StringIO()
    wizard = NodeWizard(
        root,
        RegistryAdminBackend(root, repository=Path.cwd()),
        id_factory=uuid7_hex,
    )
    if interactive:
        from .tui_doors import DoorTerminalIOError, run_node_add_door

        try:
            result = run_node_add_door(
                wizard,
                output=preview,
                input_stream=sys.stdin,
                terminal_output=sys.stderr,
            )
        except DoorTerminalIOError as exc:
            from .errors import ProtocolRefusal

            raise ProtocolRefusal(
                "door_terminal_io_failed",
                TUI_DOOR_COPY["tui.door.terminal_io_failed"],
                _NODE_ADD_FULLY_FLAGGED_REMEDY,
            ) from exc
        return "ok", _previewed(result, preview), OK
    values = [args.node, args.harness, args.lifetime]
    if args.lease_minutes is not None:
        values.append(str(args.lease_minutes))
    result = wizard.add_from_keys(values, preview)
    if all(value is not None for value in tide_values):
        from .tide_policy import TidePolicyLedger

        result["tide_policy"] = TidePolicyLedger(root).set(
            args.node,
            args.tide_metric,
            args.tide_threshold,
            args.tide_action,
            idempotency_key=args.tide_idempotency_key
            or "node-add-tide-" + uuid7_hex(),
        )
    return "ok", _previewed(result, preview), OK


def _node_retire(args: argparse.Namespace) -> HandlerResult:
    if args.instance is not None:
        from .lane_scaling import LaneScalingService, load_role_profiles

        profiles = load_role_profiles(Path(__file__).parents[1] / "roles" / "profiles")
        result = LaneScalingService(_root(args.root), profiles).retire(
            actor=args.actor,
            instance=args.instance,
            drain=args.drain,
        )
        return "ok", result, OK
    if args.drain:
        from .errors import ProtocolRefusal

        raise ProtocolRefusal(
            "arguments_invalid", "--drain composes only with --instance"
        )
    root = _root(args.root)
    preview = io.StringIO()
    result = NodeWizard(
        root, RegistryAdminBackend(root), id_factory=uuid7_hex
    ).retire_from_keys([args.node], preview)
    return "ok", _previewed(result, preview), OK


def _node_drain(args: argparse.Namespace) -> HandlerResult:
    from .events import EventLog

    result = EventLog(_root(args.root)).empty_inbox(
        args.node, acting_session_id=args.session
    )
    return "ok", result, OK


def _node_spawn(args: argparse.Namespace) -> HandlerResult:
    from .lane_scaling import LaneScalingService, load_role_profiles

    profiles = load_role_profiles(Path(__file__).parents[1] / "roles" / "profiles")
    result = LaneScalingService(_root(args.root), profiles).spawn(
        actor=args.actor,
        profile_name=args.lane_profile,
        ordinal=args.ordinal,
    )
    return "ok", result, OK


def _node_switch(args: argparse.Namespace) -> HandlerResult:
    root = _root(args.root)
    preview = io.StringIO()
    result = ProviderSwitchWizard(
        root, RegistryAdminBackend(root), id_factory=uuid7_hex
    ).switch_from_keys([args.node, args.harness, args.model], preview)
    return "ok", _previewed(result, preview), OK


def _answer_values(template: RoleTemplate, raw_answers: Iterable[str]) -> list[str]:
    supplied: Dict[str, str] = {}
    for raw in raw_answers:
        key, separator, value = raw.partition("=")
        if not separator or not key or key in supplied:
            from .errors import ProtocolRefusal

            raise ProtocolRefusal(
                "role_answer_invalid", "role answers must be unique key=value pairs"
            )
        supplied[key] = value
    expected = {question.key for question in template.questions}
    if set(supplied) != expected:
        from .errors import ProtocolRefusal

        raise ProtocolRefusal(
            "role_answer_invalid", "role answers must exactly match the template questions"
        )
    return [supplied[question.key] for question in template.questions]


def _node_role(args: argparse.Namespace) -> HandlerResult:
    root = _root(args.root)
    templates = _templates(root)
    template = templates.get(args.template)
    if template is None:
        from .errors import ProtocolRefusal

        raise ProtocolRefusal("role_template_unknown", "selected role template is not in this root library",
            remedy="pass --template a role that role list shows for this --root")
    preview = io.StringIO()
    values = [args.node, args.template, *_answer_values(template, args.answers)]
    result = RoleStepWizard(
        root, RegistryAdminBackend(root), templates, id_factory=uuid7_hex
    ).assign_from_keys(values, preview)
    return "ok", _previewed(result, preview), OK


def _role_list(args: argparse.Namespace) -> HandlerResult:
    root = _root(args.root)
    return "ok", {"roles": sorted(_templates(root))}, OK


def _role_show(args: argparse.Namespace) -> HandlerResult:
    root = _root(args.root)
    templates = _templates(root)
    template = templates.get(args.role)
    if template is None:
        from .errors import ProtocolRefusal

        raise ProtocolRefusal("role_template_unknown", "selected role template is not in this root library",
            remedy="name a role that role list shows for this --root")
    return "ok", {"template": template.record, "sha256": template.digest}, OK


def _node_prompts(args: argparse.Namespace) -> HandlerResult:
    from .lifecycle_prompts import project_prompts

    artifact = project_prompts(_root(args.root), args.as_node, args.harness, Path(args.out))
    return "ok", artifact, OK



def _role_write(args: argparse.Namespace) -> HandlerResult:
    from .role_library import RoleTemplateLibrary
    from .role_templates import _reject_duplicate_keys
    from .errors import ProtocolRefusal
    import re

    library = RoleTemplateLibrary(_root(args.root))
    key = args.idempotency_key
    if args.role_command == 'new':
        result = library.new(args.name, from_role=args.from_role, idempotency_key=key)
    elif args.role_command == 'import':
        result = library.import_file(Path(args.from_file), idempotency_key=key)
    else:
        changes = None
        if args.set_fields is not None:
            changes = {}
            for supplied in args.set_fields:
                field, separator, value = supplied.partition('=')
                if not separator or not re.fullmatch(r'[a-z][a-z0-9_]{0,63}', field):
                    raise ProtocolRefusal('role_template_invalid', 'edit fields require field=value',
                        remedy='pass --set as field=value with a lowercase field name')
                if field in changes:
                    raise ProtocolRefusal('role_template_invalid', 'edit field is repeated: ' + field,
                        remedy='pass each --set field at most once')
                try:
                    parsed = json.loads(value, object_pairs_hook=_reject_duplicate_keys,
                        parse_constant=lambda value: (_ for _ in ()).throw(ValueError('non-finite number')))
                except json.JSONDecodeError:
                    if value[:1] in ('[', '{', '"'):
                        raise ProtocolRefusal('role_template_invalid', field + ' must contain valid JSON',
                            remedy='pass the --set value as valid JSON, or as bare text that does not start with a quote or bracket')
                    parsed = value
                except ValueError:
                    raise ProtocolRefusal('role_template_invalid', field + ' must contain strict JSON',
                        remedy='pass a finite JSON number and no duplicate object keys in the --set value')
                changes[field] = parsed
        result = library.edit(args.name, changes=changes,
            from_file=Path(args.from_file) if args.from_file is not None else None,
            idempotency_key=key)
    return 'ok', result, OK


def _role_validate(args: argparse.Namespace) -> HandlerResult:
    from .role_library import RoleTemplateLibrary

    template = RoleTemplateLibrary(_root(args.root)).validate_file(Path(args.from_file))
    return 'ok', {'template': template.record, 'sha256': template.digest}, OK



def _role_transfer_architect(args: argparse.Namespace) -> HandlerResult:
    root = _root(args.root)
    templates = _templates(root)
    result = RegistryAdminBackend(root).transfer_architect(
        to=args.to,
        idempotency_key=args.idempotency_key,
        declared_templates=templates,
    )
    return "ok", result, OK


def _quota_collect(args: argparse.Namespace) -> HandlerResult:
    from .errors import ProtocolRefusal
    from .quota import QuotaLedger
    from .quota_adapters import (
        MAX_PROVIDER_PAYLOAD_BYTES,
        adapter_for,
        collect_codex_app_server,
    )

    root = _root(args.root)
    adapter = adapter_for(args.provider)
    if args.provider == "openai_codex":
        if args.executable is None:
            raise ProtocolRefusal(
                "quota_executable_required",
                "Codex quota collection requires one explicit local executable",
            )
        receipt = collect_codex_app_server(
            Path(args.executable),
            observed_at=args.observed_at,
            idempotency_key=args.idempotency_key,
        )
    else:
        if args.executable is not None:
            raise ProtocolRefusal(
                "quota_executable_not_supported",
                "only Codex quota collection accepts an executable",
            )
        if args.provider in {"anthropic_claude_code", "google_gemini"}:
            payload = sys.stdin.buffer.read(MAX_PROVIDER_PAYLOAD_BYTES + 1)
            if len(payload) > MAX_PROVIDER_PAYLOAD_BYTES:
                raise ProtocolRefusal(
                    "quota_payload_oversized", "provider testimony exceeds one MiB"
                )
        else:
            payload = b""
        receipt = adapter.observe(
            payload,
            observed_at=args.observed_at,
            idempotency_key=args.idempotency_key,
        )
    row = QuotaLedger(root).append(receipt)
    return "ok", {
        "provider": receipt.provider,
        "receipt": receipt.to_dict(),
        "ledger_record_id": row["id"],
    }, OK


def _quota_show(args: argparse.Namespace) -> HandlerResult:
    from .quota import QuotaLedger
    from .quota_adapters import adapter_for

    root = _root(args.root)
    adapter_for(args.provider)
    latest = QuotaLedger(root).latest_record(args.provider)
    if latest is None:
        return "no_result", {"provider": args.provider, "receipt": None}, 32
    record_id, receipt = latest
    return "ok", {
        "provider": args.provider,
        "receipt": receipt.to_dict(),
        "ledger_record_id": record_id,
    }, OK


def _quota_provider_choices() -> Tuple[str, ...]:
    from .quota_adapters import adapter_roster

    return tuple(adapter.provider for adapter in adapter_roster())


def _chart(args: argparse.Namespace) -> HandlerResult:
    if not args.declared_roots:
        from .errors import ProtocolRefusal

        raise ProtocolRefusal("arguments_invalid", "chart requires --declared-roots")
    chart = MultiBusHarborChart(args.declared_roots)
    artifact = chart.artifact()
    if not args.json:
        if args.live:
            from .tui_chart import run_live_harbor_map

            run_live_harbor_map(
                snapshot_loader=chart.artifact,
                input_stream=sys.stdin,
                output_stream=sys.stderr,
            )
        else:
            sys.stderr.write(render_multi_bus_chart(artifact))
            sys.stderr.flush()
    return "ok", artifact, OK


def _chart_add_root(args: argparse.Namespace) -> HandlerResult:
    result = DeclaredRoots(args.declared_roots).add_root(
        bus_id=args.bus_id,
        root=args.root_path,
        architect_node=args.architect_node,
        downstream=args.downstream,
    )
    return "ok", result, OK


def _chart_remove_root(args: argparse.Namespace) -> HandlerResult:
    result = DeclaredRoots(args.declared_roots).remove_root(bus_id=args.bus_id)
    return "ok", result, OK


def _survey(args: argparse.Namespace) -> HandlerResult:
    artifact = ForeignBusSurvey(
        args.declared_roots,
        search_paths=args.search_paths,
        hooks_path=args.hooks_path,
        targets_paths=args.targets_paths,
    ).run()
    return "ok", artifact, OK


class _LiveNodeProjectionSource:
    """Read all mutable projection inputs at each call boundary."""

    def __init__(
        self,
        root: FloatiRoot,
        declared_roots: str,
        managed_executable: str,
        profile: str,
    ) -> None:
        self.root = root
        self.backend = RegistryAdminBackend(root)
        self.declarations = DeclaredRoots(declared_roots)
        self.managed_executable = managed_executable
        self.profile = profile

    def _enrich(self, record: Mapping[str, Any]) -> Dict[str, Any]:
        node_id = str(record["node_id"])
        role = self.backend.role_record(node_id)
        return {
            "tenant_id": record["tenant_id"],
            "node_id": node_id,
            "role": role["template_role"],
            "harness": record["role"],
            "state": record["state"],
        }

    def active_node(self, node_id: str) -> Mapping[str, object]:
        return self._enrich(self.backend.active_node(node_id))

    def active_nodes(self) -> list[Mapping[str, object]]:
        return [self._enrich(record) for record in self.backend.active_nodes()]

    def role_record(self, node_id: str) -> Mapping[str, object]:
        return self.backend.role_record(node_id)

    def declared_roots(self) -> tuple[Dict[str, Any], ...]:
        return tuple(
            {
                "bus_id": declaration["bus_id"],
                "root": str(declaration["root"]),
                "architect_node": declaration["architect_node"],
                "downstream": list(declaration["downstream"]),
            }
            for declaration in self.declarations.load()
        )

    def wake_status(self, node_id: str) -> str:
        self.backend.active_node(node_id)
        return "none"

    def managed_verbs(self, node_id: str, harness: str) -> ManagedVerbShape:
        active = self.backend.active_node(node_id)
        if active["role"] != harness:
            from .errors import ProtocolRefusal

            raise ProtocolRefusal(
                "node_projection_managed_bus_invalid",
                "managed bus harness does not match the live registry",
            )
        return ManagedVerbShape(
            harness=harness,
            executable=self.managed_executable,
            profile=self.profile,
        )


def _projection_source(args: argparse.Namespace, root: FloatiRoot) -> _LiveNodeProjectionSource:
    return _LiveNodeProjectionSource(
        root,
        args.declared_roots,
        args.managed_executable,
        args.profile,
    )


def _node_boot(args: argparse.Namespace) -> HandlerResult:
    root = _root(args.root)
    projection = NodeBootProjection(
        root, args.node, _projection_source(args, root), _templates(root)
    )
    return "ok", projection.project(), OK


def _node_teardown(args: argparse.Namespace) -> HandlerResult:
    root = _root(args.root)
    projection = NodeTeardownProjection(
        root, args.node, _projection_source(args, root), _templates(root)
    )
    return "ok", projection.project(), OK


def _node_explain(args: argparse.Namespace) -> HandlerResult:
    root = _root(args.root)
    projection = NodeExplainProjection(
        root, args.node, _projection_source(args, root), _templates(root)
    )
    return "ok", projection.project(), OK


def _node_prep_clear(args: argparse.Namespace) -> HandlerResult:
    from .prep_clear import PrepClear

    receipt = PrepClear(_root(args.root)).clear(
        args.actor,
        Path(args.workspace),
        args.session,
        repo=args.repo,
        doc=args.doc,
        note=args.note,
        idempotency_key=args.idempotency_key
        or "prep-clear-cli-" + uuid7_hex(),
        complement=args.complement,
        to=args.to,
        git_executable=args.git_executable,
    )
    return "ok", receipt, OK


def _state_flush(args: argparse.Namespace) -> HandlerResult:
    root = _root(args.root)
    RegistryAdminBackend(root).active_node(args.node)
    receipt = dict(
        record_state_flush(root, args.node, prior_mtime_ns=args.prior_mtime_ns)
    )
    from .errors import ProtocolRefusal
    from .tide import TideEvaluator

    try:
        TideEvaluator(
            root,
            source_sha="f2b587634cfc6d6a52cc24bd02bfd978919c359b",
        ).observe_state_flush(receipt)
    except ProtocolRefusal as exc:
        if exc.code != "tide_directive_absent":
            raise
    return "ok", receipt, OK


def _wake_pause(args: argparse.Namespace) -> HandlerResult:
    from .wake_control import WakeController

    artifact = WakeController(_root(args.root)).pause(
        args.actor,
        args.session,
        idempotency_key=args.idempotency_key
        or "wake-cli-pause-" + uuid7_hex(),
    )
    return "ok", artifact, OK


def _wake_resume(args: argparse.Namespace) -> HandlerResult:
    from .wake_control import WakeController

    artifact = WakeController(_root(args.root)).resume(
        args.actor,
        args.session,
        idempotency_key=args.idempotency_key
        or "wake-cli-resume-" + uuid7_hex(),
    )
    return "ok", artifact, OK


def _wake_status(args: argparse.Namespace) -> HandlerResult:
    from .wake_control import WakeController

    return "ok", WakeController(_root(args.root)).status(args.actor, args.session), OK


def _seat_board(args: argparse.Namespace) -> HandlerResult:
    from .seat_board import SeatBoard
    result = SeatBoard(_root(args.root)).board(args.actor, Path(args.workspace),
        args.session,
        idempotency_key=args.idempotency_key, take_over=args.take_over)
    return "ok", result, OK


def _wake_arm(args: argparse.Namespace) -> HandlerResult:
    from .codex_wait_contract import CodexWaitConsentLedger, CodexWaitSessionLedger, resolve_participant
    from .errors import ProtocolRefusal

    root = _root(args.root)
    participant = resolve_participant(root.tenant_home, Path(args.workspace))
    if participant is None or participant.root.tenant_home != root.tenant_home:
        raise ProtocolRefusal(
            "codex_wait_participant_unresolved",
            "no waiter workspace binding for this workspace; install the waiter through the governed path, then arm",
        )
    if participant.binding.node_id != args.actor:
        raise ProtocolRefusal(
            "codex_wait_actor_mismatch",
            "acting node does not own this workspace binding",
        )
    consent = CodexWaitConsentLedger(root).require_armed(participant.binding)
    artifact = CodexWaitSessionLedger(root).arm(
        participant.binding,
        consent,
        args.session,
        idempotency_key=args.idempotency_key,
        take_over=args.take_over,
    )
    return "ok", artifact, OK


def _wake_daemon_coordinate(args: argparse.Namespace):
    from .wake_daemon_contract import DaemonCoordinate

    return DaemonCoordinate(_root(args.root), args.actor, args.harness)


def _wake_daemon_manager(coordinate):
    launcher = Path(__file__).resolve().parents[1] / "scripts" / "floati"
    if sys.platform.startswith("linux"):
        from .wake_daemon_systemd import SystemdUserUnitManager

        return SystemdUserUnitManager(coordinate, installed_launcher=launcher)
    from .wake_daemon_launchd import LaunchAgentManager

    return LaunchAgentManager(coordinate, installed_launcher=launcher)


def _wake_daemon_display(artifact: Mapping[str, Any]) -> str:
    from .copy import (
        WAKE_DAEMON_INSTALLED_DISPLAY,
        WAKE_DAEMON_REMOVED_DISPLAY,
        WAKE_DAEMON_REVOKED_DISPLAY,
        WAKE_DAEMON_RUNNING_DISPLAY,
        WAKE_DAEMON_STOPPED_DISPLAY,
        WAKE_DAEMON_UNKNOWN_DISPLAY,
    )

    state = artifact.get("state")
    return {
        "installed": WAKE_DAEMON_INSTALLED_DISPLAY,
        "running": WAKE_DAEMON_RUNNING_DISPLAY,
        "stopped": WAKE_DAEMON_STOPPED_DISPLAY,
        "removed": WAKE_DAEMON_REMOVED_DISPLAY,
        "revoked": WAKE_DAEMON_REVOKED_DISPLAY,
    }.get(state, WAKE_DAEMON_UNKNOWN_DISPLAY)


def _bind_resume_probe(
    coordinate,
    args: argparse.Namespace,
    executable: Path,
    workspace: Path,
    session: str,
) -> Optional[str]:
    """WD-R5b (Am.1): a turn-costing resume probe is a consent surface, not a
    silent cost. Bind offers it - showing what will run and what it costs -
    and asks; --yes is the exception, not the interface. Declining produces a
    recorded absence (resume_unproven); probe failure refuses the bind."""
    from .errors import ProtocolRefusal
    from .wake_daemon import WAKE_BREAKER_REMEDY
    from .wake_daemon_adapters import (
        PROBE_DEADLINE_SECONDS,
        PROBE_REASON,
        resume_probe_class,
        wake_adapter_for,
    )
    import time

    if resume_probe_class(coordinate.harness) != "costs_one_turn":
        return None
    adapter = wake_adapter_for(
        coordinate.root,
        coordinate.node_id,
        coordinate.harness,
        zcode_node_executable=args.zcode_node_executable,
        zcode_entry_executable=args.zcode_entry_executable,
    )
    if not getattr(args, "yes", False):
        sys.stderr.write(
            "floati wake daemon bind: this adapter's resume probe costs one turn.\n"
            f"It will run ONE resume against session {session!r} (bounded at "
            f"{PROBE_DEADLINE_SECONDS}s)\nto prove the session can wake before "
            "binding. Run the probe now? [y/N]: "
        )
        sys.stderr.flush()
        answer = sys.stdin.readline().strip().casefold()
        if answer not in ("y", "yes"):
            return "resume_unproven"
    started = time.monotonic()
    result = adapter.probe_resume(
        executable, workspace, session, PROBE_REASON, PROBE_DEADLINE_SECONDS
    )
    observed = round(time.monotonic() - started, 3)
    if result.outcome != "woke":
        raise ProtocolRefusal(
            "wake_bind_target_unresumable",
            f"resume probe failed after {observed}s "
            f"(outcome={result.outcome}, reason={result.reason_code}); "
            f"{WAKE_BREAKER_REMEDY}",
        )
    return "resume_proven"


def _wake_daemon_bind(args: argparse.Namespace) -> HandlerResult:
    from .copy import WAKE_DAEMON_BOUND_DISPLAY, WAKE_DAEMON_GROK_BOUND_DISPLAY
    from .errors import ProtocolRefusal
    from .wake_daemon_adapters import adapter_contract_digest, resume_probe_class
    from .wake_daemon_contract import AdapterBindingStore

    coordinate = _wake_daemon_coordinate(args)
    if coordinate.harness == "codex":
        raise ProtocolRefusal(
            "wake_daemon_codex_binding_source_invalid",
            "Codex daemon binding is accepted only from trusted waiter participation",
        )
    # WD-R5a: an adapter that declares no resume_probe class cannot be bound.
    probe_class = resume_probe_class(coordinate.harness)
    workspace_path = Path(args.workspace)
    executable_path = Path(args.executable)
    resume_state = (
        None
        if probe_class != "costs_one_turn"
        else _bind_resume_probe(
            coordinate, args, executable_path, workspace_path, args.session
        )
    )
    artifact = AdapterBindingStore(coordinate.root).write(
        coordinate,
        session_id=args.session,
        workspace=workspace_path,
        executable=executable_path,
        adapter_version="1",
        adapter_digest=adapter_contract_digest(coordinate.harness),
        binding_epoch=args.binding_epoch,
        resume_state=resume_state,
    )
    artifact["display"] = (
        WAKE_DAEMON_GROK_BOUND_DISPLAY
        if coordinate.harness == "grok-build"
        else WAKE_DAEMON_BOUND_DISPLAY
    )
    return "ok", artifact, OK


def _wake_daemon_consent(args: argparse.Namespace) -> HandlerResult:
    from .copy import WAKE_DAEMON_CONSENTED_DISPLAY
    from .wake_daemon_adapters import adapter_contract_digest
    from .wake_daemon_contract import DaemonConsentLedger

    coordinate = _wake_daemon_coordinate(args)
    artifact = DaemonConsentLedger(coordinate.root).consent(
        coordinate,
        adapter_version="1",
        adapter_digest=adapter_contract_digest(coordinate.harness),
        min_poll_seconds=args.min_poll_seconds,
        max_poll_seconds=args.max_poll_seconds,
        max_backoff_seconds=args.max_backoff_seconds,
        activation_epoch=args.activation_epoch,
        idempotency_key="wake-daemon-cli-consent-" + uuid7_hex(),
    )
    artifact["display"] = WAKE_DAEMON_CONSENTED_DISPLAY
    return "ok", artifact, OK


def _wake_daemon_install(args: argparse.Namespace) -> HandlerResult:
    artifact = _wake_daemon_manager(_wake_daemon_coordinate(args)).install()
    artifact["display"] = _wake_daemon_display(artifact)
    return "ok", artifact, OK


def _wake_daemon_start(args: argparse.Namespace) -> HandlerResult:
    artifact = _wake_daemon_manager(_wake_daemon_coordinate(args)).start()
    artifact["display"] = _wake_daemon_display(artifact)
    return "ok", artifact, OK


def _wake_daemon_status(args: argparse.Namespace) -> HandlerResult:
    from .copy import WAKE_DAEMON_INACTIVE_DISPLAY
    from .errors import ProtocolRefusal
    from .wake_daemon_contract import DaemonConsentLedger

    coordinate = _wake_daemon_coordinate(args)
    try:
        DaemonConsentLedger(coordinate.root).require_active(coordinate)
    except ProtocolRefusal as exc:
        if exc.code != "wake_daemon_consent_absent":
            raise
        return "ok", {
            "schema_version": 0,
            "state": "inactive",
            "node_id": coordinate.node_id,
            "harness": coordinate.harness,
            "coordinate_digest": coordinate.digest,
            "display": WAKE_DAEMON_INACTIVE_DISPLAY,
        }, OK
    artifact = _wake_daemon_manager(coordinate).status()
    artifact["display"] = _wake_daemon_display(artifact)
    return "ok", artifact, OK


def _wake_daemon_stop(args: argparse.Namespace) -> HandlerResult:
    artifact = _wake_daemon_manager(_wake_daemon_coordinate(args)).stop()
    artifact["display"] = _wake_daemon_display(artifact)
    return "ok", artifact, OK


def _wake_daemon_remove(args: argparse.Namespace) -> HandlerResult:
    artifact = _wake_daemon_manager(_wake_daemon_coordinate(args)).remove()
    artifact["display"] = _wake_daemon_display(artifact)
    return "ok", artifact, OK


def _wake_daemon_revoke(args: argparse.Namespace) -> HandlerResult:
    artifact = _wake_daemon_manager(_wake_daemon_coordinate(args)).revoke(
        idempotency_key="wake-daemon-cli-revoke-" + uuid7_hex()
    )
    artifact["display"] = _wake_daemon_display(artifact)
    return "ok", artifact, OK


def _wake_daemon_serve(args: argparse.Namespace) -> HandlerResult:
    from .errors import ProtocolRefusal
    from .wake_daemon import WakeDaemon
    from .wake_daemon_adapters import wake_adapter_for
    from .wake_daemon_contract import DaemonConsentLedger

    coordinate = _wake_daemon_coordinate(args)
    consent = DaemonConsentLedger(coordinate.root).require_active(coordinate)
    if consent["activation_epoch"] != args.activation_epoch:
        raise ProtocolRefusal(
            "wake_daemon_activation_epoch_mismatch",
            "LaunchAgent activation epoch does not match active consent",
        )
    stop = threading.Event()
    previous = signal.getsignal(signal.SIGTERM)
    signal.signal(signal.SIGTERM, lambda _signum, _frame: stop.set())
    try:
        WakeDaemon(
            coordinate,
            wake_adapter_for(
                coordinate.root,
                coordinate.node_id,
                coordinate.harness,
                zcode_node_executable=args.zcode_node_executable,
                zcode_entry_executable=args.zcode_entry_executable,
            ),
        ).serve(stop.is_set)
    finally:
        signal.signal(signal.SIGTERM, previous)
    return "ok", {"schema_version": 0, "state": "stopped"}, OK


def _add_wake_identity(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--root", required=True)
    parser.add_argument("--as", dest="actor", required=True, metavar='NODE')
    parser.add_argument("--session", required=True)


def _add_wake_daemon_identity(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--root", required=True)
    parser.add_argument("--as", dest="actor", required=True, metavar='NODE')
    parser.add_argument(
        "--harness", choices=("codex", "cursor", "grok-build", "zcode"), required=True
    )


def _add_zcode_executable_declarations(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--zcode-node-executable", metavar='EXE')
    parser.add_argument("--zcode-entry-executable", metavar='EXE')


def _add_projection_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--root", required=True)
    parser.add_argument("--node", required=True, metavar='NODE')
    parser.add_argument("--declared-roots", required=True, metavar='FILE')
    parser.add_argument("--managed-executable", required=True, metavar='EXE')
    parser.add_argument("--profile", required=True, metavar='PROFILE')
    parser.add_argument("--json", action="store_true")


def register_admin_commands(commands: argparse._SubParsersAction) -> None:
    """Register every reconciled WS-B/WS-D command exactly once."""

    node = commands.add_parser("node")
    node_commands = node.add_subparsers(dest="node_command", required=True)

    add = node_commands.add_parser("add")
    add.add_argument("--root")
    add.add_argument("--node", metavar='NODE')
    add.add_argument("--harness")
    add.add_argument("--lifetime", choices=("permanent", "temporary"))
    add.add_argument("--lease-minutes", type=int, metavar='N')
    add.add_argument("--tide-metric", metavar='METRIC')
    add.add_argument("--tide-threshold", metavar='VALUE')
    add.add_argument("--tide-action", choices=("recommend", "direct"))
    add.add_argument("--tide-idempotency-key", metavar='KEY')
    add.add_argument("--plan", metavar='FILE')
    add.set_defaults(handler=_node_add)

    spawn = node_commands.add_parser("spawn")
    spawn.add_argument("--root", required=True)
    spawn.add_argument("--as", dest="actor", required=True, metavar='NODE')
    spawn.add_argument("--profile", dest="lane_profile", required=True, metavar='PROFILE')
    spawn.add_argument("--ordinal", type=int, metavar='N')
    spawn.set_defaults(handler=_node_spawn)

    retire = node_commands.add_parser("retire")
    retire.add_argument("--root", required=True)
    retire_target = retire.add_mutually_exclusive_group(required=True)
    retire_target.add_argument("--node", metavar='NODE')
    retire_target.add_argument("--instance")
    retire.add_argument("--as", dest="actor", metavar='NODE')
    retire.add_argument("--drain", action="store_true")
    retire.set_defaults(handler=_node_retire)

    drain = node_commands.add_parser("drain")
    drain.add_argument("--root", required=True)
    drain.add_argument("--node", required=True)
    drain.add_argument("--session", required=True)
    drain.set_defaults(handler=_node_drain)

    switch = node_commands.add_parser("switch")
    switch.add_argument("--root", required=True)
    switch.add_argument("--node", required=True, metavar='NODE')
    switch.add_argument("--harness", required=True)
    switch.add_argument("--model", required=True)
    switch.set_defaults(handler=_node_switch)

    role_step = node_commands.add_parser("role")
    role_step.add_argument("--root", required=True)
    role_step.add_argument("--node", required=True, metavar='NODE')
    role_step.add_argument("--template", required=True)
    role_step.add_argument("--answer", dest="answers", action="append", default=[])
    role_step.set_defaults(handler=_node_role)

    boot = node_commands.add_parser("boot")
    _add_projection_arguments(boot)
    boot.set_defaults(handler=_node_boot)

    teardown = node_commands.add_parser("teardown")
    _add_projection_arguments(teardown)
    teardown.set_defaults(handler=_node_teardown)

    explain = node_commands.add_parser("explain")
    _add_projection_arguments(explain)
    explain.set_defaults(handler=_node_explain)

    prep_clear = node_commands.add_parser("prep-clear")
    prep_clear.add_argument("--root", required=True)
    prep_clear.add_argument("--as", dest="actor", required=True)
    prep_clear.add_argument("--session", required=True)
    prep_clear.add_argument("--workspace", required=True)
    prep_clear.add_argument("--repo", required=True)
    prep_clear.add_argument("--doc", required=True)
    prep_clear.add_argument("--note", required=True)
    prep_clear.add_argument("--complement")
    prep_clear.add_argument("--to")
    prep_clear.add_argument("--idempotency-key")
    prep_clear.add_argument("--git-executable")
    prep_clear.set_defaults(handler=_node_prep_clear)

    state_flush = node_commands.add_parser("state-flush")
    state_flush.add_argument("--root", required=True)
    state_flush.add_argument("--node", required=True, metavar='NODE')
    state_flush.add_argument("--prior-mtime-ns", type=int, metavar='N')
    state_flush.set_defaults(handler=_state_flush)

    prompts = node_commands.add_parser("prompts")
    prompts.add_argument("--root", required=True)
    prompts.add_argument("--as", required=True, dest="as_node", metavar="NODE")
    prompts.add_argument("--harness", required=True, metavar="HARNESS")
    prompts.add_argument("--out", required=True, metavar="DIR")
    prompts.set_defaults(handler=_node_prompts)

    role = commands.add_parser("role")
    role_commands = role.add_subparsers(dest="role_command", required=True)
    role_list = role_commands.add_parser("list")
    role_list.add_argument("--root", required=True)
    role_list.set_defaults(handler=_role_list)
    role_show = role_commands.add_parser("show")
    role_show.add_argument("--root", required=True)
    role_show.add_argument("role", metavar="ROLE")
    role_show.set_defaults(handler=_role_show)
    role_transfer = role_commands.add_parser("transfer-architect")
    role_transfer.add_argument("--root", required=True)
    role_transfer.add_argument("--to", required=True, metavar="NODE")
    role_transfer.add_argument("--idempotency-key", required=True, dest="idempotency_key")
    role_transfer.set_defaults(handler=_role_transfer_architect)

    role_new = role_commands.add_parser('new')
    role_new.add_argument('--root', required=True, metavar='ROOT')
    role_new.add_argument('--name', required=True, metavar='ROLE')
    role_new.add_argument('--from', dest='from_role', required=True, metavar='ROLE')
    role_new.add_argument('--idempotency-key', required=True, metavar='KEY')
    role_new.set_defaults(handler=_role_write)

    role_import = role_commands.add_parser('import')
    role_import.add_argument('--root', required=True, metavar='ROOT')
    role_import.add_argument('--from', dest='from_file', required=True, metavar='PATH')
    role_import.add_argument('--idempotency-key', required=True, metavar='KEY')
    role_import.set_defaults(handler=_role_write)

    role_edit = role_commands.add_parser('edit')
    role_edit.add_argument('--root', required=True, metavar='ROOT')
    role_edit.add_argument('--name', required=True, metavar='ROLE')
    edit_input = role_edit.add_mutually_exclusive_group(required=True)
    edit_input.add_argument('--set', dest='set_fields', action='append', metavar='FIELD=VALUE')
    edit_input.add_argument('--from', dest='from_file', metavar='PATH')
    role_edit.add_argument('--idempotency-key', required=True, metavar='KEY')
    role_edit.set_defaults(handler=_role_write)

    role_validate = role_commands.add_parser('validate')
    role_validate.add_argument('--root', required=True, metavar='ROOT')
    role_validate.add_argument('--from', dest='from_file', required=True, metavar='PATH')
    role_validate.set_defaults(handler=_role_validate)

    quota = commands.add_parser("quota")
    quota_commands = quota.add_subparsers(dest="quota_command", required=True)
    quota_collect = quota_commands.add_parser("collect")
    quota_collect.add_argument("--root", required=True)
    quota_collect.add_argument(
        "--provider",
        choices=_quota_provider_choices(),
        required=True,
    )
    quota_collect.add_argument("--observed-at", required=True, metavar='TIMESTAMP')
    quota_collect.add_argument("--idempotency-key", required=True, metavar='KEY')
    quota_collect.add_argument("--executable", metavar='EXE')
    quota_collect.set_defaults(handler=_quota_collect)
    quota_show = quota_commands.add_parser("show")
    quota_show.add_argument("--root", required=True)
    quota_show.add_argument(
        "--provider",
        choices=_quota_provider_choices(),
        required=True,
    )
    quota_show.set_defaults(handler=_quota_show)

    chart = commands.add_parser("chart")
    chart.add_argument("--declared-roots", metavar='FILE')
    chart.add_argument("--live", action="store_true")
    chart.add_argument("--json", action="store_true")
    chart.set_defaults(handler=_chart, json=False, live=False)
    chart_commands = chart.add_subparsers(dest="chart_command")
    add_root = chart_commands.add_parser("add-root")
    add_root.add_argument("--declared-roots", required=True, metavar='FILE')
    add_root.add_argument("--bus-id", required=True, metavar='ID')
    add_root.add_argument("--root", dest="root_path", required=True, metavar='PATH')
    add_root.add_argument("--architect-node", required=True, metavar='NODE')
    add_root.add_argument("--downstream", action="append", default=[], metavar='ID')
    add_root.set_defaults(handler=_chart_add_root)
    remove_root = chart_commands.add_parser("remove-root")
    remove_root.add_argument("--declared-roots", required=True, metavar='FILE')
    remove_root.add_argument("--bus-id", required=True, metavar='ID')
    remove_root.set_defaults(handler=_chart_remove_root)

    survey = commands.add_parser("survey")
    survey.add_argument("--declared-roots", required=True, metavar='FILE')
    survey.add_argument("--search-path", dest="search_paths", action="append", default=[], metavar='PATH')
    survey.add_argument("--hooks", dest="hooks_path", metavar='PATH')
    survey.add_argument("--targets", dest="targets_paths", action="append", default=[], metavar='PATH')
    survey.add_argument("--json", action="store_true")
    survey.set_defaults(handler=_survey)

    seat = commands.add_parser("seat")
    seat_commands = seat.add_subparsers(dest="seat_command", required=True)
    board = seat_commands.add_parser("board")
    board.add_argument("--root", metavar="ROOT", required=True)
    board.add_argument("--as", metavar="NODE", dest="actor", required=True)
    board.add_argument("--workspace", metavar="PATH", required=True)
    board.add_argument("--session", metavar="SESSION", required=True)
    board.add_argument("--idempotency-key", metavar="KEY", required=True)
    board.add_argument("--take-over", action="store_true")
    board.set_defaults(handler=_seat_board, artifact_schema_version=1)

    wake = commands.add_parser("wake")
    wake_commands = wake.add_subparsers(dest="wake_command", required=True)
    wake_pause = wake_commands.add_parser(
        "pause",
        floati_mcp_exposure="governed",
        floati_mcp_required=("idempotency_key",),
    )
    _add_wake_identity(wake_pause)
    wake_pause.add_argument("--idempotency-key", metavar='KEY')
    wake_pause.set_defaults(handler=_wake_pause)
    wake_resume = wake_commands.add_parser(
        "resume",
        floati_mcp_exposure="governed",
        floati_mcp_required=("idempotency_key",),
    )
    _add_wake_identity(wake_resume)
    wake_resume.add_argument("--idempotency-key", metavar='KEY')
    wake_resume.set_defaults(handler=_wake_resume)
    wake_status = wake_commands.add_parser("status")
    _add_wake_identity(wake_status)
    wake_status.set_defaults(handler=_wake_status)
    wake_arm = wake_commands.add_parser("arm")
    _add_wake_identity(wake_arm)
    wake_arm.add_argument("--workspace", required=True)
    wake_arm.add_argument("--idempotency-key", required=True, metavar='KEY')
    wake_arm.add_argument("--take-over", action="store_true")
    wake_arm.set_defaults(handler=_wake_arm)

    wake_daemon = wake_commands.add_parser("daemon")
    daemon_commands = wake_daemon.add_subparsers(
        dest="wake_daemon_command", required=True
    )
    daemon_consent = daemon_commands.add_parser("consent")
    _add_wake_daemon_identity(daemon_consent)
    daemon_consent.add_argument("--min-poll-seconds", type=int, required=True, metavar='N')
    daemon_consent.add_argument("--max-poll-seconds", type=int, required=True, metavar='N')
    daemon_consent.add_argument("--max-backoff-seconds", type=int, required=True, metavar='N')
    daemon_consent.add_argument("--activation-epoch", type=int, required=True, metavar='N')
    daemon_consent.set_defaults(handler=_wake_daemon_consent)

    daemon_bind = daemon_commands.add_parser("bind")
    _add_wake_daemon_identity(daemon_bind)
    daemon_bind.add_argument("--session", required=True)
    daemon_bind.add_argument("--workspace", required=True)
    daemon_bind.add_argument("--executable", required=True, metavar='EXE')
    daemon_bind.add_argument("--binding-epoch", type=int, required=True, metavar='N')
    daemon_bind.add_argument(
        "--yes", action="store_true",
        help="consent to the turn-costing resume probe without the interactive ask",
    )
    _add_zcode_executable_declarations(daemon_bind)
    daemon_bind.set_defaults(handler=_wake_daemon_bind)

    for operation, handler in (
        ("install", _wake_daemon_install),
        ("start", _wake_daemon_start),
        ("status", _wake_daemon_status),
        ("stop", _wake_daemon_stop),
        ("remove", _wake_daemon_remove),
        ("revoke", _wake_daemon_revoke),
    ):
        daemon_operation = daemon_commands.add_parser(operation)
        _add_wake_daemon_identity(daemon_operation)
        daemon_operation.set_defaults(handler=handler)

    daemon_serve = daemon_commands.add_parser(
        "serve", help=argparse.SUPPRESS, floati_public=False
    )
    _add_wake_daemon_identity(daemon_serve)
    daemon_serve.add_argument("--activation-epoch", type=int, required=True, metavar='N')
    _add_zcode_executable_declarations(daemon_serve)
    daemon_serve.set_defaults(handler=_wake_daemon_serve)

    from .uninstall import register_cli as register_uninstall

    register_uninstall(commands)


def register_legacy_workspace_options(
    register: argparse.ArgumentParser, retire: argparse.ArgumentParser
) -> None:
    """Bind the existing public verbs to the B2 composition contract."""

    register.add_argument("--create-workspace", action="store_true")
    register.set_defaults(handler=_register_node)
    retire.set_defaults(handler=_retire_node)

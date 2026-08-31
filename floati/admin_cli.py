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
from .foreign_bus_survey import ForeignBusSurvey
from .ids import uuid7_hex
from .multi_bus_chart import MultiBusHarborChart
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
from .role_templates import RoleTemplate, load_shipped_role_templates
from .root import FloatiRoot, resolve_command_root
from .workspace_layout import register_node, retire_node
from .state_receipts import record_state_flush


HandlerResult = Tuple[str, Dict[str, Any], int]
OK = 0


def _root(path: str | None) -> FloatiRoot:
    return resolve_command_root(path, create=False)


def _templates() -> Dict[str, RoleTemplate]:
    return load_shipped_role_templates(Path(__file__).parents[1] / "roles" / "shipped")


def _previewed(result: Mapping[str, Any], preview: io.StringIO) -> Dict[str, Any]:
    evidence = dict(result)
    rows = []
    prefix = "ledger preview: "
    for line in preview.getvalue().splitlines():
        if line.startswith(prefix):
            rows.append(json.loads(line[len(prefix) :]))
    evidence["preview_rows"] = rows
    return evidence


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
    root = _root(args.root)
    tide_values = (args.tide_metric, args.tide_threshold, args.tide_action)
    if any(value is not None for value in tide_values) and not all(
        value is not None for value in tide_values
    ):
        from .errors import ProtocolRefusal

        raise ProtocolRefusal(
            "arguments_invalid",
            "optional tide step requires metric, threshold, and action together",
        )
    if all(value is not None for value in tide_values):
        from .tide_catalog import policy_metric_for
        from .tide_policy import normalize_threshold

        normalize_threshold(
            args.tide_threshold,
            policy_metric_for(args.harness, args.tide_metric),
        )
    preview = io.StringIO()
    values = [args.node, args.harness, args.lifetime]
    if args.lease_minutes is not None:
        values.append(str(args.lease_minutes))
    result = NodeWizard(
        root, RegistryAdminBackend(root), id_factory=uuid7_hex
    ).add_from_keys(values, preview)
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
    templates = _templates()
    template = templates.get(args.template)
    if template is None:
        from .errors import ProtocolRefusal

        raise ProtocolRefusal("role_template_unknown", "selected role template is not shipped")
    preview = io.StringIO()
    values = [args.node, args.template, *_answer_values(template, args.answers)]
    result = RoleStepWizard(
        root, RegistryAdminBackend(root), templates, id_factory=uuid7_hex
    ).assign_from_keys(values, preview)
    return "ok", _previewed(result, preview), OK


def _role_list(args: argparse.Namespace) -> HandlerResult:
    _root(args.root)
    return "ok", {"roles": sorted(_templates())}, OK


def _role_show(args: argparse.Namespace) -> HandlerResult:
    _root(args.root)
    templates = _templates()
    template = templates.get(args.role)
    if template is None:
        from .errors import ProtocolRefusal

        raise ProtocolRefusal("role_template_unknown", "selected role template is not shipped")
    return "ok", {"template": template.record, "sha256": template.digest}, OK


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
    return "ok", MultiBusHarborChart(args.declared_roots).artifact(), OK


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
        root, args.node, _projection_source(args, root), _templates()
    )
    return "ok", projection.project(), OK


def _node_teardown(args: argparse.Namespace) -> HandlerResult:
    root = _root(args.root)
    projection = NodeTeardownProjection(
        root, args.node, _projection_source(args, root), _templates()
    )
    return "ok", projection.project(), OK


def _node_explain(args: argparse.Namespace) -> HandlerResult:
    root = _root(args.root)
    projection = NodeExplainProjection(
        root, args.node, _projection_source(args, root), _templates()
    )
    return "ok", projection.project(), OK


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
    )
    return "ok", artifact, OK


def _wake_daemon_coordinate(args: argparse.Namespace):
    from .wake_daemon_contract import DaemonCoordinate

    return DaemonCoordinate(_root(args.root), args.actor, args.harness)


def _wake_daemon_manager(coordinate):
    from .wake_daemon_launchd import LaunchAgentManager

    launcher = Path(__file__).resolve().parents[1] / "scripts" / "floati"
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


def _wake_daemon_bind(args: argparse.Namespace) -> HandlerResult:
    from .copy import WAKE_DAEMON_BOUND_DISPLAY, WAKE_DAEMON_GROK_BOUND_DISPLAY
    from .errors import ProtocolRefusal
    from .wake_daemon_adapters import adapter_contract_digest
    from .wake_daemon_contract import AdapterBindingStore

    coordinate = _wake_daemon_coordinate(args)
    if coordinate.harness == "codex":
        raise ProtocolRefusal(
            "wake_daemon_codex_binding_source_invalid",
            "Codex daemon binding is accepted only from trusted waiter participation",
        )
    artifact = AdapterBindingStore(coordinate.root).write(
        coordinate,
        session_id=args.session,
        workspace=Path(args.workspace),
        executable=Path(args.executable),
        adapter_version="1",
        adapter_digest=adapter_contract_digest(coordinate.harness),
        binding_epoch=args.binding_epoch,
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
            wake_adapter_for(coordinate.root, coordinate.node_id, coordinate.harness),
        ).serve(stop.is_set)
    finally:
        signal.signal(signal.SIGTERM, previous)
    return "ok", {"schema_version": 0, "state": "stopped"}, OK


def _add_wake_identity(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--root", required=True)
    parser.add_argument("--as", dest="actor", required=True)
    parser.add_argument("--session", required=True)


def _add_wake_daemon_identity(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--root", required=True)
    parser.add_argument("--as", dest="actor", required=True)
    parser.add_argument(
        "--harness", choices=("codex", "cursor", "grok-build"), required=True
    )


def _add_projection_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--root", required=True)
    parser.add_argument("--node", required=True)
    parser.add_argument("--declared-roots", required=True)
    parser.add_argument("--managed-executable", required=True)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--json", action="store_true")


def register_admin_commands(commands: argparse._SubParsersAction) -> None:
    """Register every reconciled WS-B/WS-D command exactly once."""

    node = commands.add_parser("node")
    node_commands = node.add_subparsers(dest="node_command", required=True)

    add = node_commands.add_parser("add")
    add.add_argument("--root", required=True)
    add.add_argument("--node", required=True)
    add.add_argument("--harness", required=True)
    add.add_argument("--lifetime", choices=("permanent", "temporary"), required=True)
    add.add_argument("--lease-minutes", type=int)
    add.add_argument("--tide-metric")
    add.add_argument("--tide-threshold")
    add.add_argument("--tide-action", choices=("recommend", "direct"))
    add.add_argument("--tide-idempotency-key")
    add.set_defaults(handler=_node_add)

    spawn = node_commands.add_parser("spawn")
    spawn.add_argument("--root", required=True)
    spawn.add_argument("--as", dest="actor", required=True)
    spawn.add_argument("--profile", dest="lane_profile", required=True)
    spawn.add_argument("--ordinal", type=int)
    spawn.set_defaults(handler=_node_spawn)

    retire = node_commands.add_parser("retire")
    retire.add_argument("--root", required=True)
    retire_target = retire.add_mutually_exclusive_group(required=True)
    retire_target.add_argument("--node")
    retire_target.add_argument("--instance")
    retire.add_argument("--as", dest="actor")
    retire.add_argument("--drain", action="store_true")
    retire.set_defaults(handler=_node_retire)

    switch = node_commands.add_parser("switch")
    switch.add_argument("--root", required=True)
    switch.add_argument("--node", required=True)
    switch.add_argument("--harness", required=True)
    switch.add_argument("--model", required=True)
    switch.set_defaults(handler=_node_switch)

    role_step = node_commands.add_parser("role")
    role_step.add_argument("--root", required=True)
    role_step.add_argument("--node", required=True)
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

    state_flush = node_commands.add_parser("state-flush")
    state_flush.add_argument("--root", required=True)
    state_flush.add_argument("--node", required=True)
    state_flush.add_argument("--prior-mtime-ns", type=int)
    state_flush.set_defaults(handler=_state_flush)

    role = commands.add_parser("role")
    role_commands = role.add_subparsers(dest="role_command", required=True)
    role_list = role_commands.add_parser("list")
    role_list.add_argument("--root", required=True)
    role_list.set_defaults(handler=_role_list)
    role_show = role_commands.add_parser("show")
    role_show.add_argument("--root", required=True)
    role_show.add_argument("role")
    role_show.set_defaults(handler=_role_show)

    quota = commands.add_parser("quota")
    quota_commands = quota.add_subparsers(dest="quota_command", required=True)
    quota_collect = quota_commands.add_parser("collect")
    quota_collect.add_argument("--root", required=True)
    quota_collect.add_argument(
        "--provider",
        choices=_quota_provider_choices(),
        required=True,
    )
    quota_collect.add_argument("--observed-at", required=True)
    quota_collect.add_argument("--idempotency-key", required=True)
    quota_collect.add_argument("--executable")
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
    chart.add_argument("--declared-roots", required=True)
    chart.add_argument("--json", action="store_true")
    chart.set_defaults(handler=_chart)

    survey = commands.add_parser("survey")
    survey.add_argument("--declared-roots", required=True)
    survey.add_argument("--search-path", dest="search_paths", action="append", default=[])
    survey.add_argument("--hooks", dest="hooks_path")
    survey.add_argument("--targets", dest="targets_paths", action="append", default=[])
    survey.add_argument("--json", action="store_true")
    survey.set_defaults(handler=_survey)

    wake = commands.add_parser("wake")
    wake_commands = wake.add_subparsers(dest="wake_command", required=True)
    wake_pause = wake_commands.add_parser(
        "pause",
        floati_mcp_exposure="governed",
        floati_mcp_required=("idempotency_key",),
    )
    _add_wake_identity(wake_pause)
    wake_pause.add_argument("--idempotency-key")
    wake_pause.set_defaults(handler=_wake_pause)
    wake_resume = wake_commands.add_parser(
        "resume",
        floati_mcp_exposure="governed",
        floati_mcp_required=("idempotency_key",),
    )
    _add_wake_identity(wake_resume)
    wake_resume.add_argument("--idempotency-key")
    wake_resume.set_defaults(handler=_wake_resume)
    wake_status = wake_commands.add_parser("status")
    _add_wake_identity(wake_status)
    wake_status.set_defaults(handler=_wake_status)
    wake_arm = wake_commands.add_parser("arm")
    _add_wake_identity(wake_arm)
    wake_arm.add_argument("--workspace", required=True)
    wake_arm.add_argument("--idempotency-key", required=True)
    wake_arm.set_defaults(handler=_wake_arm)

    wake_daemon = wake_commands.add_parser("daemon")
    daemon_commands = wake_daemon.add_subparsers(
        dest="wake_daemon_command", required=True
    )
    daemon_consent = daemon_commands.add_parser("consent")
    _add_wake_daemon_identity(daemon_consent)
    daemon_consent.add_argument("--min-poll-seconds", type=int, required=True)
    daemon_consent.add_argument("--max-poll-seconds", type=int, required=True)
    daemon_consent.add_argument("--max-backoff-seconds", type=int, required=True)
    daemon_consent.add_argument("--activation-epoch", type=int, required=True)
    daemon_consent.set_defaults(handler=_wake_daemon_consent)

    daemon_bind = daemon_commands.add_parser("bind")
    _add_wake_daemon_identity(daemon_bind)
    daemon_bind.add_argument("--session", required=True)
    daemon_bind.add_argument("--workspace", required=True)
    daemon_bind.add_argument("--executable", required=True)
    daemon_bind.add_argument("--binding-epoch", type=int, required=True)
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
    daemon_serve.add_argument("--activation-epoch", type=int, required=True)
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

"""Closed BUS-SCALE-CENSUS-1 workloads and normalized evidence validation.

The frozen parser closure is checked before any workload/exclusion partition.
These APIs execute no command and compare no performance budget. A valid RED
measurement remains valid evidence. Normalized native outcomes and digest
witnesses must be derived from retained raw by the owned runner; this validator
does not independently replay or authenticate that raw.

``family`` initially names each case's primary ledger. The matrix coordinator
may supply an independently validated scope copy selecting one of the four
closed families; an observation must still equal that exact declared family.
Primary-family coverage alone does not prove the complete case/family matrix.
"""

from __future__ import annotations

import argparse
import copy
from functools import lru_cache
import hashlib
import inspect
import json
from pathlib import Path
import math
import re

from tests import bus_scale_commands
from tests.bus_scale_attention import ATTENTION_CASES, require_attention_pair, validate_attention_proof
from tests.bus_scale_native_refusal import is_functional_refusal, validate_functional_refusal
from floati.command_contract import project_mcp_surface


FAMILIES = frozenset({"events", "registry", "wait-exhaustion", "deliveries"})
_MAX_RECORDS = 100000
_MAX_BYTES = 64 * 1024 * 1024
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")

# Each entry binds a complete native command or one explicitly named seam.
# Seams cannot substitute for the entire native wait-start command.
_CASES = {
    "describe": {"command":"describe","history_output":False,"family":"registry"},
    "overlap-report": {"command":"overlap report","history_output":False,"family":"registry"},
    "lane-open": {"command":"lane open","history_output":False,"family":"registry"},
    "lane-close": {"command":"lane close","history_output":False,"family":"registry"},
    "sweep": {"command":"sweep","history_output":False,"family":"registry"},
    "reclaim": {"command":"reclaim","history_output":False,"family":"events"},
    "send": {"command":"send","history_output":False,"family":"events"},
    "verify": {"command":"verify","history_output":False,"family":"events"},
    "inbox": {"command":"inbox","history_output":False,"family":"events"},
    "wake-evaluate": {"command":"wake-evaluate","history_output":False,"family":"events"},
    "wake-record": {"command":"wake-record","history_output":False,"family":"events"},
    "ack": {"command":"ack","history_output":False,"family":"deliveries"},
    "sent": {"command":"sent","history_output":True,"family":"events"},
    "log-history": {"command":"log","history_output":True,"family":"events"},
    "status": {"command":"status","history_output":False,"family":"events"},
    "effects": {"command":"effects","history_output":True,"family":"events"},
    "effect-show": {"command":"effect show","history_output":False,"family":"events"},
    "graph": {"command":"graph","history_output":False,"family":"events"},
    "doctor": {"command":"doctor","history_output":False,"family":"events"},
    "watch": {"command":"watch","history_output":False,"family":"events"},
    "wait-start": {"command":"wait","history_output":False,"family":"events"},
    "receipts-history": {"command":"receipts","history_output":True,"family":"deliveries"},
    "consume": {"command":"consume","history_output":False,"family":"deliveries"},
    "supervise": {"command":"supervise","history_output":False,"family":"events"},
    "presence-report": {"command":"presence report","history_output":False,"family":"events"},
    "presence-show": {"command":"presence show","history_output":False,"family":"events"},
    "sequencer-status": {"command":"sequencer status","history_output":False,"family":"events"},
    "work-add": {"command":"work add","history_output":False,"family":"events"},
    "work-claim": {"command":"work claim","history_output":False,"family":"events"},
    "work-complete": {"command":"work complete","history_output":False,"family":"events"},
    "work-show": {"command":"work show","history_output":True,"family":"events"},
    "node-drain": {"command":"node drain","history_output":False,"family":"events"},
    "node-boot": {"command":"node boot","history_output":False,"family":"registry"},
    "node-teardown": {"command":"node teardown","history_output":False,"family":"registry"},
    "node-explain": {"command":"node explain","history_output":False,"family":"registry"},
    "node-prep-clear": {"command":"node prep-clear","history_output":False,"family":"events"},
    "node-state-flush": {"command":"node state-flush","history_output":False,"family":"events"},
    "node-prompts": {"command":"node prompts","history_output":False,"family":"registry"},
    "net-route-list": {"command":"net route list","history_output":False,"family":"events"},
    "role-list": {"command":"role list","history_output":False,"family":"registry"},
    "role-show": {"command":"role show","history_output":False,"family":"registry"},
    "role-validate": {"command":"role validate","history_output":False,"family":"registry"},
    "quota-show": {"command":"quota show","history_output":False,"family":"events"},
    "chart": {"command":"chart","history_output":False,"family":"registry"},
    "chart-timings": {"command":"chart timings","history_output":False,"family":"events"},
    "seat-board": {"command":"seat board","history_output":False,"family":"events"},
    "seat-teardown": {"command":"seat teardown","history_output":False,"family":"events"},
    "wake-pause": {"command":"wake pause","history_output":False,"family":"events"},
    "wake-resume": {"command":"wake resume","history_output":False,"family":"events"},
    "wake-status": {"command":"wake status","history_output":False,"family":"events"},
    "wake-arm": {"command":"wake arm","history_output":False,"family":"events"},
    "wake-wait": {"command":"wake wait","history_output":False,"family":"events"},
    "wake-daemon-status": {"command":"wake daemon status","history_output":False,"family":"events"},
    "context-status": {"command":"context status","history_output":False,"family":"events"},
    "context-turnover": {"command":"context turnover","history_output":False,"family":"events"},
    "context-policy-show": {"command":"context policy show","history_output":False,"family":"events"},
    "context-reading-record": {"command":"context reading record","history_output":False,"family":"events"},
    "hold-start": {"command":"hold start","history_output":False,"family":"events"},
    "hold-release": {"command":"hold release","history_output":False,"family":"events"},
    "wait-idle": {"command":"seam:floati.codex_wait.run_stop_waiter:unchanged_idle_tick","history_output":False,"family":"events"},
    "readiness-read": {"command":"seam:floati.mail_health.RecipientReadiness.observe","history_output":False,"family":"events"},
    "receipt-append": {"command":"seam:floati.codex_wait_contract.CodexWaitReceiptLedger.record_exhaustion","history_output":False,"family":"wait-exhaustion"},
    "wake-callback": {"command":"wake-callback","history_output":False,"family":"events"},
    "confluence-grant": {"command":"confluence grant","history_output":False,"family":"events"},
    "confluence-revoke": {"command":"confluence revoke","history_output":False,"family":"events"},
    "confluence-status": {"command":"confluence status","history_output":False,"family":"events"},
    "confluence-bundle": {"command":"confluence bundle","history_output":True,"family":"events"},
    "intake-scan": {"command":"intake scan","history_output":False,"family":"events"},
    "intake-show": {"command":"intake show","history_output":True,"family":"events"},
    "intake-preview": {"command":"intake preview","history_output":False,"family":"events"},
    "wake-daemon-serve": {"command":"seam:floati.wake_daemon.WakeDaemon.serve:one_iteration","history_output":False,"family":"events"},
}
_NATIVE = {
    "describe": (0, "ok", "command_contract_returned"),
    "overlap-report": (0, "ok", "overlap_report_returned"),
    "lane-open": (0, "ok", "lane_workspace_opened"),
    "lane-close": (0, "ok", "lane_workspace_closed"),
    "sweep": (0, "ok", "lane_sweep_returned"),
    "reclaim": (0, "ok", "reclaim_plan_returned"),
    "send": (0, "ok", "message_envelope_appended"),
    "verify": (0, "ok", "delivery_claim_verified"),
    "inbox": (0, "ok", "messages_presented"),
    "wake-evaluate": (0, "ok", "wake_decision_returned"),
    "wake-record": (0, "ok", "wake_attempt_recorded"),
    "ack": (0, "ok", "acknowledgment_appended"),
    "sent": (0, "ok", "sender_receipts_returned"),
    "log-history": (0, "ok", "history_returned"),
    "status": (0, "ok", "status_returned"),
    "effects": (0, "ok", "effects_returned"),
    "effect-show": (0, "ok", "effect_returned"),
    "graph": (0, "ok", "graph_returned"),
    "doctor": (35, "degraded", "doctor_returned"),
    "watch": (0, "ok", "bounded_watch_returned"),
    "wait-start": (0, "ok", "natural_deadline_exhausted"),
    "receipts-history": (0, "ok", "history_returned"),
    "consume": (0, "ok", "consumption_receipt_appended"),
    "supervise": (0, "ok", "supervision_returned"),
    "presence-report": (0, "ok", "presence_report_appended"),
    "presence-show": (0, "ok", "presence_reports_returned"),
    "sequencer-status": (0, "ok", "sequencer_status_returned"),
    "work-add": (0, "ok", "work_item_added"),
    "work-claim": (0, "ok", "work_item_claimed"),
    "work-complete": (0, "ok", "consumed_work_completed"),
    "work-show": (0, "ok", "work_items_returned"),
    "node-drain": (0, "ok", "node_mail_drained"),
    "node-boot": (0, "ok", "node_boot_returned"),
    "node-teardown": (0, "ok", "node_teardown_returned"),
    "node-explain": (0, "ok", "node_explanation_returned"),
    "node-prep-clear": (0, "ok", "prep_clear_receipted"),
    "node-state-flush": (0, "ok", "state_flush_receipted"),
    "node-prompts": (0, "ok", "lifecycle_prompts_written"),
    "net-route-list": (0, "ok", "declared_routes_returned"),
    "role-list": (0, "ok", "roles_returned"),
    "role-show": (0, "ok", "role_returned"),
    "role-validate": (0, "ok", "role_validated"),
    "quota-show": (0, "ok", "quota_testimony_returned"),
    "chart": (0, "ok", "declared_chart_returned"),
    "chart-timings": (0, "ok", "chart_timings_returned"),
    "seat-board": (0, "ok", "seat_board_receipted"),
    "seat-teardown": (0, "ok", "seat_teardown_completed"),
    "wake-pause": (0, "ok", "wake_paused"),
    "wake-resume": (0, "ok", "wake_resumed"),
    "wake-status": (0, "ok", "wake_status_returned"),
    "wake-arm": (0, "ok", "wake_armed"),
    "wake-wait": (0, "ok", "cursor_followup_returned"),
    "wake-daemon-status": (0, "ok", "wake_daemon_status_returned"),
    "context-status": (0, "ok", "context_status_returned"),
    "context-turnover": (0, "ok", "context_turnover_returned"),
    "context-policy-show": (0, "ok", "context_policy_returned"),
    "context-reading-record": (0, "ok", "context_reading_appended"),
    "hold-start": (0, "ok", "hold_started"),
    "hold-release": (0, "ok", "hold_released"),
    "wait-idle": (0, "ok", "unchanged_idle_tick"),
    "readiness-read": (0, "unknown", "readiness_observed"),
    "receipt-append": (0, "consent_reopened", "exhaustion_receipt_appended"),
    "wake-callback": (0, "ok", "wake_callback_receipted"),
    "confluence-grant": (0, "ok", "confluence_grant_appended"),
    "confluence-revoke": (0, "ok", "confluence_revocation_appended"),
    "confluence-status": (0, "ok", "confluence_grants_returned"),
    "confluence-bundle": (0, "ok", "confluence_bundle_written"),
    "intake-scan": (0, "ok", "intake_scan_returned"),
    "intake-show": (0, "ok", "intake_snapshots_returned"),
    "intake-preview": (0, "ok", "intake_preview_returned"),
    "wake-daemon-serve": (0, "ok", "daemon_poll_completed"),
}

# Exact per-path dispositions accepted under Am7; no prefix exemption.
_NONROUTINE = {
    "work notify": "Sender-authorized notification replay is a write: it may append mail envelopes and work-notification sent receipts. It requires a preauthored notification intent/artifact and the matching sender lease; the existing unnotified work-add/complete workloads do not supply or qualify that transaction.",
    "hook relay": "Claude context-hook relay requires a native hook payload and a proved context flush before presentation; ordinary CLI inbox workloads do not qualify that harness transaction.",
    "confluence adopt": "Observer-session managed-mode adoption requires manager authority; this is consumer onboarding, not routine seat operation.",
    "confluence release": "Observer-session managed-mode release ends external consumer management; ordinary seats do not administer consumer sessions.",
    "effect reconcile": "Recovery of an orchestrator-owned external effect requires its operation/fence authority; ordinary seats do not run recovery transactions.",
    "effect compensate": "Compensation approval of an external effect is an orchestrator/owner recovery decision, not ordinary seat work.",
    "threads": "Provider-thread attachment inventory belongs to orchestrator supervision, rather than the seat's own bus/mail lifecycle.",
    "thread attach": "Provider-thread association is orchestrator integration setup, not routine bus-seat registration or boarding.",
    "thread observe": "Provider-thread observation launches the configured provider CLI under an orchestrator attachment; ordinary seat mail reads do not do so.",
    "thread detach": "Ending an orchestrator provider-thread attachment is integration administration, not seat wake/session control.",
    "thread show": "Inspection of an orchestrator provider-thread attachment is outside the bus seat's own mail/work/session lifecycle.",
    "plan": "Scheduling-plan policy admission is a coordinator planning operation; ordinary seats claim already-dispatched work.",
    "waiter arm": "Workspace mapping and durable harness-wait consent are seat onboarding/configuration; routine turnover uses seat board and wake arm.",
    "board": "Interactive terminal harbor display is an operator UI session; bus boarding is the separately measured seat board command.",
    "orchestrate": "Starting a provider worker fleet is coordinator orchestration, not ordinary seat mail/work operation.",
    "intake adopt": "Adopting local/GitHub work sources is coordinator intake administration, not ordinary seat work execution.",
    "intake dispatch": "Binding an approved external GitHub mutation into an effect requires intake-controller authority, not ordinary seat mail authority.",
    "worker run": "Provider subprocess execution is an orchestrator-launched worker runtime, not a routine bus verb run by its seat.",
    "worker adhoc profile": "Leased native worker profiling binds a lane, adapter, authority and optional human launch grant; routine bus-seat workloads do not create worker launch profiles.",
    "worker adhoc run": "Authenticated persistent native dispatch executes a leased provider worker under profile and envelope authority; routine send/inbox/ack workloads do not launch that runtime.",
    "worker adhoc show": "Inspection of a durable native worker dispatch belongs to its governed worker lifecycle; routine bus-seat workloads supply no admitted dispatch to inspect.",
    "worker adhoc teardown": "Closing a proved-complete leased native worker row requires its dispatch and session authority; ordinary seat teardown does not qualify that worker transaction.",
    "mcp serve": "MCP transports CLI handlers. A complete source-bound receipt requires a measured native case for every served tool.",
    "quota collect": "Provider credential/quota collection performs an explicit external provider observation; ordinary bus seats consume quota show testimony.",
}

# These reviewed parser paths still need native workloads. Each disposition
# names the fixture or operation that a new workload would have to exercise.
_PENDING_WORKLOADS = {
    "ask": "Needs a correlated peer question with a presentation receipt and an answer, decline or timeout, plus a role ask preview and confirmation using a local quota snapshot.",
    "checkout-liveness": "Needs a checkout with a known host process or worktree liveness witness.",
    "dispatch brief": "Needs an authenticated dispatch with a specific recorded brief revision.",
    "dispatch close": "Needs an open dispatch and authority to close it.",
    "dispatch record": "Needs a brief, sender authority and a bound dispatch carrier.",
    "dispatch return": "Needs a dispatch and a Git artifact for the selected revision.",
    "dispatch show": "Needs dispatch history with revisions, returns and outstanding work.",
    "hold inbound show": "Needs a node inbox hold with recorded delivery decisions.",
    "hold inbound start": "Needs a node-scoped inbound hold fixture separate from the project hold.",
    "needs admission": "Needs an attention budget and inbox state to evaluate dispatch admission.",
    "needs budget": "Needs an attention policy that can be set or cleared with a receipt.",
    "needs close": "Needs an open attention item owned by the acting node.",
    "needs file": "Needs a governed attention item filing with actor and idempotency bindings.",
    "needs observe": "Needs an explicit fixture orchestrator that supplies thread observations.",
    "needs read": "Needs an attention item and a human read-receipt fixture.",
    "needs release": "Needs a dispatch hold created by the attention budget.",
    "needs show": "Needs an attention inbox that exercises item freshness and read state.",
    "overlap observe": "Needs a repository with worktree pairs that changed the same path.",
    "quiet release": "Needs a quiet window whose driver is gone, or one stopped intent an operator resolves with the target pid and start token.",
    "quiet run": "Needs a controlled host load or pressure fixture and enrolled seats.",
    "quiet show": "Needs quiet-window history with seat exposure receipts.",
    "quiet status": "Needs an active quiet window and an enrolled command to evaluate.",
    "replay": "Needs question and work histories across retained epochs, including a question replay that compares the cited Git objects in an explicit repository.",
    "territory declare": "Needs a scoped workspace and actor that can lease paths.",
    "territory preview": "Needs active territory leases and a prospective dispatch edit set.",
    "territory release": "Needs an active territory lease with a selected owner and ID.",
    "territory show": "Needs a mix of active and expired territory claims.",
    "who": "Needs a roster with measured and unknown evidence cells.",
    "work handoff record": "Needs exact Git source evidence and a work item to bind it to.",
    "work handoff show": "Needs a recorded work handoff whose source evidence can be revalidated.",
}

_SEAM_NATIVE_COMMANDS = {"wake-daemon-serve": "wake daemon serve"}


def _fields(value: object, expected: set[str], label: str) -> dict:
    if type(value) is not dict or set(value) != expected:
        raise ValueError(f"{label} has missing or extra fields")
    return value


def _native_closure(rows: object) -> dict:
    """Reuse the full frozen grammar validator, never an exclusion prefix."""
    classified = bus_scale_commands._require_closed_contract(rows)
    if len(rows) != 186:
        raise ValueError("workload scope requires the reviewed 186 executable rows")
    return classified


def _exclusions(rows: dict, classified: dict) -> dict[str, str]:
    measured = {
        case["command"] for case in _CASES.values()
        if not case["command"].startswith("seam:")
    }
    measured.update(_SEAM_NATIVE_COMMANDS.values())
    if len(measured) != 68 or not measured <= set(rows):
        raise ValueError("workload native bindings do not match the frozen inventory")
    administration = classified["administration"]
    remaining = set(rows) - measured
    nonroutine, pending, admin = set(_NONROUTINE), set(_PENDING_WORKLOADS), set(administration)
    if (nonroutine & pending or (nonroutine | pending) & (measured | admin)
            or remaining != nonroutine | pending | admin
            or set(classified["routine"]) != measured | nonroutine | pending):
        raise ValueError("every native path needs an exact workload disposition")
    reasons = {
        **administration,
        **_NONROUTINE,
        **{path: "Native workload pending. " + reason for path, reason in _PENDING_WORKLOADS.items()},
    }
    return {path: reasons[path] for path in sorted(remaining)}


def scope_contract(parser: argparse.ArgumentParser) -> dict:
    """Return a fresh exhaustive scope only for the exact reviewed parser."""
    rows = bus_scale_commands.executable_contract(parser)
    classified = _native_closure(rows)
    return {
        "inventory": copy.deepcopy(rows),
        "cases": copy.deepcopy(_CASES),
        "excluded": _exclusions(rows, classified),
    }


def mcp_tool_bindings(parser: argparse.ArgumentParser) -> dict:
    """Bind every served MCP tool to its measured native handler and source.

    This is a finite read-only receipt, not proof that any workload ran.
    The caller must retain it in the native run's receipt to justify the
    mcp-serve transport exclusion.
    """
    scope = scope_contract(parser)
    by_command = {case["command"]: name for name, case in scope["cases"].items()
                  if not case["command"].startswith("seam:")}
    source_root = Path(__file__).resolve().parents[1]
    bindings, source_hashes = [], {}
    for tool in project_mcp_surface(parser)["tools"]:
        path = tool["_meta"]["floati"]["commandPath"]
        command = " ".join(path)
        if command not in by_command:
            raise ValueError("served MCP tool has no measured native implementation: " + command)
        leaf = parser
        for component in path:
            actions = [action for action in leaf._actions
                       if isinstance(action, argparse._SubParsersAction)]
            if len(actions) != 1 or component not in actions[0].choices:
                raise ValueError("served MCP tool does not bind the current parser")
            leaf = actions[0].choices[component]
        handler = leaf._defaults.get("handler", leaf._defaults.get("direct_handler"))
        if not callable(handler) or not handler.__module__.startswith("floati."):
            raise ValueError("served MCP tool does not bind a native Floati handler")
        source = Path(inspect.getsourcefile(handler)).resolve(strict=True)
        try:
            relative = source.relative_to(source_root).as_posix()
        except ValueError as exc:
            raise ValueError("served MCP implementation source escaped the repository") from exc
        if source not in source_hashes:
            source_hashes[source] = hashlib.sha256(source.read_bytes()).hexdigest()
        signature = bus_scale_commands._signature(scope["inventory"][command])
        schema = json.dumps(tool["inputSchema"], sort_keys=True, separators=(",", ":"), allow_nan=False)
        bindings.append({"tool": tool["name"], "command": command, "case": by_command[command],
                         "contract_sha256": hashlib.sha256(signature.encode()).hexdigest(),
                         "tool_schema_sha256": hashlib.sha256(schema.encode()).hexdigest(),
                         "implementation": handler.__module__ + "." + handler.__qualname__,
                         "implementation_signature": str(inspect.signature(handler)),
                         "source_path": relative, "source_sha256": source_hashes[source]})
    return {"source_sha": bus_scale_commands.BASELINE_SOURCE_SHA,
            "tools": sorted(bindings, key=lambda row: row["tool"])}


def _require_scope(scope: object) -> dict:
    scope = _fields(scope, {"inventory", "cases", "excluded"}, "workload scope")
    # Recheck the live pinned fixture even on a successful cache hit. The
    # reviewed signatures include the current platform's exact example tokens.
    # Supplied mutable inputs are keyed by strict JSON content, never identity.
    _baseline, reviewed = bus_scale_commands._baseline_state()
    disposition = bus_scale_commands._signature({
        "administration": bus_scale_commands._ADMINISTRATION,
        "cases": _CASES, "nonroutine": _NONROUTINE, "pending": _PENDING_WORKLOADS,
        "seams": _SEAM_NATIVE_COMMANDS, "families": sorted(FAMILIES),
    })
    _validate_scope_content(bus_scale_commands._signature(scope),
                            tuple(sorted(reviewed.items())), disposition)
    return scope


@lru_cache(maxsize=8)
def _validate_scope_content(content: str, reviewed: tuple, disposition: str) -> None:
    """Cache only successful closed validation, with no shared mutable result."""
    scope = json.loads(content)
    classified = _native_closure(scope["inventory"])
    cases = _fields(scope["cases"], set(_CASES), "workload cases")
    for case, expected in _CASES.items():
        row = _fields(cases[case], {"command", "history_output", "family"}, case)
        if type(row["command"]) is not str or row["command"] != expected["command"]:
            raise ValueError(f"{case} changed its native command or seam binding")
        if row["history_output"] is not expected["history_output"]:
            raise ValueError(f"{case} changed its history-output classification")
        if type(row["family"]) is not str or row["family"] not in FAMILIES:
            raise ValueError(f"{case} declares an unreviewed ledger family")
    excluded = scope["excluded"]
    expected_excluded = _exclusions(scope["inventory"], classified)
    if type(excluded) is not dict or excluded != expected_excluded:
        raise ValueError("workload exclusions changed or fail the exact partition")


def _digest(value: object, label: str) -> None:
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        raise ValueError(f"{label} requires an exact lowercase SHA-256 witness")


def _integer(value: object, *, minimum: int, maximum: int | None, label: str) -> None:
    if type(value) is not int or value < minimum or (maximum is not None and value > maximum):
        raise ValueError(f"{label} is outside its exact integer bounds")


def _number(value: object, *, positive: bool, label: str) -> None:
    if type(value) not in (int, float):
        raise ValueError(f"{label} must be a finite numeric observation")
    try:
        finite = math.isfinite(value)
    except (OverflowError, ValueError):
        finite = False
    if not finite or (value <= 0 if positive else value < 0):
        raise ValueError(f"{label} is unresolved, negative or nonfinite")


def _require_arm(case: str, arm: object) -> dict:
    fields = {"records", "bytes", "seed_sha256", "clone_sha256", "child_collected",
              "raw_sha256", "native", "coverage", "metrics"}
    if type(arm) is dict and "attention_proof" in arm:
        if case not in ATTENTION_CASES:
            raise ValueError(case + " attention category malformed: unreviewed proof case")
        fields.add("attention_proof")
        validate_attention_proof(case, arm["attention_proof"])
    arm = _fields(
        arm, fields,
        f"{case} scale arm",
    )
    _integer(arm["records"], minimum=1, maximum=_MAX_RECORDS, label="fixture records")
    _integer(arm["bytes"], minimum=1, maximum=_MAX_BYTES - 1, label="fixture bytes")
    for field in ("seed_sha256", "clone_sha256", "raw_sha256"):
        _digest(arm[field], f"{case} {field}")
    if arm["seed_sha256"] != arm["clone_sha256"]:
        raise ValueError(f"{case} working clone does not equal its restored seed")
    if arm["child_collected"] is not True:
        raise ValueError(f"{case} child collection is incomplete or unknown")

    native_fields = {"exit_code", "status", "effect"}
    if case == "doctor":
        native_fields.add("findings_sha256")
    native = _fields(arm["native"], native_fields, f"{case} native outcome")
    exit_code, status, effect_kind = _NATIVE[case]
    if type(native["exit_code"]) is not int or native["exit_code"] != exit_code:
        raise ValueError(f"{case} did not return its expected native exit")
    if type(native["status"]) is not str or native["status"] != status:
        raise ValueError(f"{case} did not return its expected native status")
    effect = _fields(native["effect"], {"kind", "count"}, f"{case} native effect")
    if type(effect["kind"]) is not str or effect["kind"] != effect_kind:
        raise ValueError(f"{case} did not observe the required native work")
    _integer(effect["count"], minimum=1, maximum=None, label=f"{case} effect count")
    if case == "doctor":
        _digest(native["findings_sha256"], "doctor native findings")

    coverage = _fields(arm["coverage"], {"cpu", "bytes", "locks"}, f"{case} coverage")
    if any(value is not True for value in coverage.values()):
        raise ValueError(f"{case} CPU, byte or lock coverage is incomplete")
    metrics = _fields(
        arm["metrics"], {"cpu_seconds", "bytes_read", "max_ledger_lock_seconds"},
        f"{case} metrics",
    )
    _number(metrics["cpu_seconds"], positive=True, label=f"{case} CPU")
    _integer(metrics["bytes_read"], minimum=0, maximum=None, label=f"{case} read bytes")
    _number(metrics["max_ledger_lock_seconds"], positive=False, label=f"{case} lock hold")
    return arm


def require_observations(scope: dict, observations: dict, *, allow_ruled_refusals=False) -> None:
    """Require all declared workload pairs and their closed observable schema.

    Return normally for valid measurements, including RED ratios. Missing
    observations are errors; history-output cases retain all measurements.
    CPU must resolve positively. Verified zero reads/holds are permitted when
    the corresponding complete-coverage witnesses are present.
    """
    if type(allow_ruled_refusals) is not bool:
        raise ValueError("ruled refusal admission must be boolean")
    scope = _require_scope(scope)
    observations = _fields(observations, set(_CASES), "workload observations")
    for case in _CASES:
        row = _fields(
            observations[case], {"family", "prefix_exact", "selected_work_equal", "arms"},
            f"{case} observation",
        )
        if type(row["family"]) is not str or row["family"] != scope["cases"][case]["family"]:
            raise ValueError(f"{case} observation does not match its declared primary family")
        arms = _fields(row["arms"], {"1x", "10x"}, f"{case} scale arms")
        one = _require_arm(case, arms["1x"])
        refused = allow_ruled_refusals and is_functional_refusal(arms["10x"])
        if row["prefix_exact"] is not True or row["selected_work_equal"] is not (False if refused else True):
            raise ValueError(f"{case} prefix or selected-work disposition is unavailable")
        if refused:
            ten = validate_functional_refusal(case, row["family"], "10x", arms["10x"])
        else:
            ten = _require_arm(case, arms["10x"])
            require_attention_pair(case, one, ten)
        if ten["records"] != 10 * one["records"]:
            raise ValueError(f"{case} fixture record growth is not exactly tenfold")
        if ten["bytes"] <= one["bytes"]:
            raise ValueError(f"{case} actual fixture bytes did not increase")

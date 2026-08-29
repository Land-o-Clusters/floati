"""Dependency-free artifact CLI over Floati's direct-home core."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import signal
import shutil
import sys
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Sequence, Tuple

from .cursor import SparseCursor
from .copy import (
    EFFECT_ATTEMPT_INVALID_DETAIL,
    EFFECT_COMPENSATION_PLAN_UNAVAILABLE_DETAIL,
    EFFECT_OPERATION_INVALID_DETAIL,
    EFFECT_PLAN_DIGEST_INVALID_DETAIL,
    EFFECT_RUN_INVALID_DETAIL,
)
from .deploy import DeploymentWriter
from .errors import DurabilityFailure, IntegrityFailure, ProtocolRefusal
from .events import EventLog
from .helptext import help_for
from .installer_shadow import observe_installer_shadow, observation_exit_code
from .projection import (
    EffectStatusProjection,
    FleetProjection,
    ThreadObservationStatusProjection,
    iter_deltas,
)
from .registry import Registry
from .root import FloatiRoot, resolve_command_root
from .supervisor import Supervisor
from .work import WorkLog
from .workers import WorkerRunner
from .adapters.codex_live import CodexAppServerAdapter
from .adapters.claude import ClaudeHeadlessAdapter
from .adapters.pi import PiRpcAdapter
from .admin_cli import register_admin_commands, register_legacy_workspace_options


OK = 0
CONFIGURATION_REFUSED = 20
INTENTIONAL_SILENCE = 31
NO_RESULT = 32
MALFORMED_EVIDENCE = 33
DEGRADED = 35
CANNOT_SPEAK = 22

HandlerResult = Tuple[str, Dict[str, Any], int]

_UUID7 = r"[0-9a-f]{12}7[0-9a-f]{3}[89ab][0-9a-f]{15}"
_HIDDEN_COMMANDS = frozenset({"wake-evaluate", "wake-record", "wake-callback"})


class _ArtifactParser(argparse.ArgumentParser):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        kwargs.setdefault("add_help", False)
        kwargs.setdefault("allow_abbrev", False)
        super().__init__(*args, **kwargs)

    def error(self, message: str) -> None:
        raise ProtocolRefusal("arguments_invalid", message)

    def _check_value(self, action: argparse.Action, value: object) -> None:
        choices = action.choices
        if (
            action.dest == "command"
            and choices is not None
            and value not in choices
        ):
            public_choices = [choice for choice in choices if choice not in _HIDDEN_COMMANDS]
            detail = "invalid choice: {!r} (choose from {})".format(
                value, ", ".join(map(repr, public_choices)),
            )
            raise argparse.ArgumentError(action, detail)
        super()._check_value(action, value)


def _root(path: Optional[str], *, create: bool = False) -> FloatiRoot:
    return resolve_command_root(path, create=create)


def _init(args: argparse.Namespace) -> HandlerResult:
    if args.solo is None and args.harness is not None:
        raise ProtocolRefusal("arguments_invalid", "--harness requires --solo")
    solo_inputs: Optional[tuple[str, str]] = None
    if args.solo is not None:
        from .solo import validate_solo_bootstrap_inputs

        solo_inputs = validate_solo_bootstrap_inputs(
            args.solo, "solo" if args.harness is None else args.harness
        )
    root = _root(args.root, create=True)
    evidence: Dict[str, Any] = {"root": str(root.path), "tenant_id": root.tenant_id}
    if solo_inputs is not None:
        from .solo import initialize_solo

        evidence["solo"] = initialize_solo(
            root, *solo_inputs
        )
    return "ok", evidence, OK


def _register(args: argparse.Namespace) -> HandlerResult:
    entry = Registry(_root(args.root)).register(args.node, args.harness)
    return "ok", entry, OK


def _retire(args: argparse.Namespace) -> HandlerResult:
    entry = Registry(_root(args.root)).retire(args.node)
    return "ok", entry, OK


def _send(args: argparse.Namespace) -> HandlerResult:
    root = _root(args.root)
    message = EventLog(root).send(
        args.sender,
        args.recipient,
        args.repo,
        args.sha,
        args.doc,
        args.note,
        reply_to=args.reply_to,
        idempotency_key=args.idempotency_key,
    )
    return "ok", message, OK


def _inbox(args: argparse.Namespace) -> HandlerResult:
    root = _root(args.root)
    messages, receipt = EventLog(root).present(args.recipient)
    evidence = {"messages": messages, "receipt": receipt}
    if not messages:
        return "intentional_silence", evidence, INTENTIONAL_SILENCE
    return "ok", evidence, OK


def _wake_evaluate(args: argparse.Namespace) -> HandlerResult:
    """Evaluate the hidden, explicit-root wake gate without public copy."""
    from .wake_hold import WakeHoldController

    artifact = WakeHoldController(_root(args.root)).evaluate(
        args.recipient,
        idempotency_key=args.idempotency_key,
        worker_session_id=args.worker_session,
        limit=args.limit,
    )
    if artifact["state"] == "fresh_work":
        return "ok", artifact, OK
    return "intentional_silence", artifact, INTENTIONAL_SILENCE


def _wake_record(args: argparse.Namespace) -> HandlerResult:
    """Record the host prompt outcome without exposing a public command."""
    from .wake_hold import WakeAttemptLedger

    receipt = WakeAttemptLedger(_root(args.root)).record(
        recipient=args.recipient,
        acting_session_id=args.session,
        item_ids=args.message_ids,
        decision_receipt_id=args.decision,
        message_worker_session_id=args.message_worker_session,
        idempotency_key=args.idempotency_key,
        outcome=args.outcome,
        reason_code=args.reason_code,
    )
    return "ok", receipt, OK


def _ack(args: argparse.Namespace) -> HandlerResult:
    receipt = SparseCursor(_root(args.root)).ack(
        args.recipient,
        [args.message_id],
        acting_session_id=args.session,
    )
    return "ok", receipt, OK


def _log(args: argparse.Namespace) -> HandlerResult:
    messages = EventLog(_root(args.root)).records()
    evidence = {"messages": messages}
    if not messages:
        return "no_result", evidence, NO_RESULT
    return "ok", evidence, OK


def _log_command(args: argparse.Namespace) -> int:
    if not args.replay:
        if args.speed is not None or args.plain:
            raise ProtocolRefusal(
                "arguments_invalid", "--speed and --plain require --replay"
            )
        status, evidence, exit_code = _log(args)
        _emit("log", status, evidence, exit_code)
        return exit_code
    from .replay import ReplayTimeline
    from .replay_render import play_replay

    artifact = ReplayTimeline.from_root(_root(args.root)).artifact()
    if not artifact["events"]:
        _emit("log", "no_result", artifact, NO_RESULT)
        return NO_RESULT
    play_replay(
        artifact,
        speed=1.0 if args.speed is None else args.speed,
        stream=sys.stderr,
        plain=args.plain,
    )
    materialize = getattr(artifact, "materialized", None)
    if callable(materialize):
        artifact = materialize()
    _emit("log", "ok", artifact, OK)
    return OK


def _current_time() -> datetime:
    return datetime.now(timezone.utc)


def _parse_time(value: Optional[str]) -> Optional[datetime]:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ProtocolRefusal("time_invalid", "time must be a UTC RFC3339 value") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ProtocolRefusal("time_invalid", "time must include a UTC offset")
    return parsed.astimezone(timezone.utc)


def _status(args: argparse.Namespace) -> HandlerResult:
    projection = FleetProjection(_root(args.root))
    snapshot = (
        projection.status_artifact(_current_time())
        if args.json
        else projection.snapshot(_current_time())
    )
    shadow = observe_installer_shadow(getattr(args, "destination", None))
    snapshot["installer_shadow"] = shadow
    if args.json:
        snapshot["status_schema_version"] = 1
    return "ok", snapshot, observation_exit_code(shadow)


def _typed_effect_filters(args: argparse.Namespace) -> None:
    operation_id = getattr(args, "operation_id", None)
    run_id = getattr(args, "run_id", None)
    attempt_id = getattr(args, "attempt_id", None)
    if operation_id is not None and re.fullmatch("effect-op-" + _UUID7, operation_id) is None:
        raise ProtocolRefusal("effect_operation_id_invalid", EFFECT_OPERATION_INVALID_DETAIL)
    if run_id is not None and re.fullmatch("run-" + _UUID7, run_id) is None:
        raise ProtocolRefusal("run_id_invalid", EFFECT_RUN_INVALID_DETAIL)
    if attempt_id is not None and re.fullmatch("attempt-" + _UUID7, attempt_id) is None:
        raise ProtocolRefusal("attempt_id_invalid", EFFECT_ATTEMPT_INVALID_DETAIL)


def _effect_status(args: argparse.Namespace) -> HandlerResult:
    _typed_effect_filters(args)
    artifact = EffectStatusProjection(_root(args.root)).artifact(
        _current_time(),
        run_id=getattr(args, "run_id", None),
        attempt_id=getattr(args, "attempt_id", None),
        operation_id=getattr(args, "operation_id", None),
    )
    if not artifact["operations"]:
        return "no_result", artifact, NO_RESULT
    return "ok", artifact, OK


def _thread_attach(args: argparse.Namespace) -> HandlerResult:
    work_shape = (
        args.work_item_id is not None
        and args.run_id is None
        and args.attempt_id is None
    )
    attempt_shape = all(
        value is not None
        for value in (args.work_item_id, args.run_id, args.attempt_id)
    )
    if not (work_shape ^ attempt_shape):
        raise ProtocolRefusal(
            "arguments_invalid", "attach requires one exact subject"
        )
    from .thread_observations import ThreadObserver

    observer = ThreadObserver(_root(args.root))
    if work_shape:
        row = observer.register_work_item(
            args.work_item_id, args.provider_thread_id, args.actor
        )
    else:
        row = observer.register_attempt(
            args.run_id,
            args.work_item_id,
            args.attempt_id,
            args.provider_thread_id,
            args.actor,
        )
    artifact = ThreadObservationStatusProjection(observer.root).artifact(
        _current_time(), attachment_id=str(row["id"])
    )
    return "ok", artifact, OK


def _thread_observe(args: argparse.Namespace) -> HandlerResult:
    from .thread_observations import ThreadObserver

    observer = ThreadObserver(_root(args.root))
    row = observer.observe(args.attachment_id)
    artifact = ThreadObservationStatusProjection(observer.root).artifact(
        _current_time(), attachment_id=str(row["attachment_id"])
    )
    return "ok", artifact, OK


def _thread_detach(args: argparse.Namespace) -> HandlerResult:
    from .thread_observations import ThreadObserver

    observer = ThreadObserver(_root(args.root))
    row = observer.detach(args.attachment_id, args.actor)
    artifact = ThreadObservationStatusProjection(observer.root).artifact(
        _current_time(), attachment_id=str(row["attachment_id"])
    )
    return "ok", artifact, OK


def _thread_show(args: argparse.Namespace) -> HandlerResult:
    artifact = ThreadObservationStatusProjection(_root(args.root)).artifact(
        _current_time(), attachment_id=args.attachment_id
    )
    if not artifact["attachments"]:
        return "no_result", artifact, NO_RESULT
    return "ok", artifact, OK


def _threads(args: argparse.Namespace) -> HandlerResult:
    artifact = ThreadObservationStatusProjection(_root(args.root)).artifact(
        _current_time()
    )
    if not artifact["attachments"]:
        return "no_result", artifact, NO_RESULT
    return "ok", artifact, OK


def _effect_reconcile(args: argparse.Namespace) -> HandlerResult:
    _typed_effect_filters(args)
    root = _root(args.root)
    from .approvals import ApprovalLedger
    from .effects import EffectController, EffectLedger
    from .policy import RepositoryPolicy
    from .runtruth import RunLedger

    controller = EffectController(
        EffectLedger(root),
        RunLedger(root),
        RepositoryPolicy.load(root.path / "FLOATI.toml"),
        ApprovalLedger(root),
    )
    controller.reconcile(args.operation_id)
    return _effect_status(args)


def _effect_compensate(args: argparse.Namespace) -> HandlerResult:
    _typed_effect_filters(args)
    if args.confirm is not None and re.fullmatch(r"[0-9a-f]{64}", args.confirm) is None:
        raise ProtocolRefusal("plan_digest_invalid", EFFECT_PLAN_DIGEST_INVALID_DETAIL)
    _root(args.root)
    raise ProtocolRefusal(
        "effect_compensation_plan_unavailable",
        EFFECT_COMPENSATION_PLAN_UNAVAILABLE_DETAIL,
    )



def _graph_command(args: argparse.Namespace) -> int:
    from .graph import HarborGraph, HarborTraffic

    root = _root(args.root)
    topology = HarborGraph(root).artifact()
    if args.json:
        _emit("graph", "ok", topology, OK)
        return OK

    from .graph_render import render_harbor_chart

    traffic = HarborTraffic(root).artifact()
    print(
        render_harbor_chart(topology, traffic, color=sys.stdout.isatty()),
        end="",
    )
    return OK


def _plan(args: argparse.Namespace) -> HandlerResult:
    """Explain one explicit immutable plan after resolving its command root."""

    from .admission import AdmissionEvaluator, AdmissionPlan
    from .policy import RepositoryPolicy

    _root(args.root)
    plan = AdmissionPlan.load(args.plan)
    policy = RepositoryPolicy.load(args.policy)
    artifact = AdmissionEvaluator.evaluate(plan, policy)
    return artifact.outcome, artifact.machine(), OK


def _doctor(args: argparse.Namespace) -> HandlerResult:
    from .doctor import Doctor

    doctor = Doctor(
        args.source,
        _root(args.root).path,
        ref=args.ref,
        gateway_config=args.gateway_config,
        destination=args.destination,
        profile=args.profile,
        codex_hooks=args.codex_hooks,
        codex_config=args.codex_config,
    )
    artifact, return_code = doctor.artifact()
    if getattr(args, "probe", False):
        probe_artifact, probe_rc = doctor.probe(
            getattr(args, "probe_budget", 60.0) or 60.0
        )
        artifact["probe"] = probe_artifact
        if probe_rc != 0 and return_code == 0:
            return_code = 35
        artifact["state"] = {0: "healthy", 20: "refused", 33: "malformed_evidence",
                             35: "degraded"}.get(return_code, "degraded")
    return str(artifact["state"]), artifact, return_code


def _supervise(args: argparse.Namespace) -> HandlerResult:
    snapshot = Supervisor(_root(args.root)).snapshot(_current_time())
    return "ok", snapshot, OK


def _receipts(args: argparse.Namespace) -> HandlerResult:
    history = FleetProjection(_root(args.root)).receipts(args.node)
    if not any(history[key] for key in ("deliveries", "acks", "denials")):
        return "no_result", history, NO_RESULT
    return "ok", history, OK


def _watch(args: argparse.Namespace) -> int:
    root = _root(args.root)
    installer_shadow = observe_installer_shadow(getattr(args, "destination", None))
    exit_code = observation_exit_code(installer_shadow)
    try:
        for delta in iter_deltas(
            FleetProjection(root),
            interval=args.interval,
            iterations=args.iterations,
        ):
            snapshot = delta["snapshot"]
            if not isinstance(snapshot, dict):
                raise ProtocolRefusal("watch_snapshot_invalid", "watch snapshot is not an object")
            snapshot["installer_shadow"] = installer_shadow
            _emit(
                "watch",
                "ok",
                {"delta": delta},
                exit_code,
                schema_version=1,
            )
            sys.stdout.flush()
    except KeyboardInterrupt:
        return exit_code
    return exit_code


def _binding(args: argparse.Namespace) -> list[Dict[str, str]]:
    values = (getattr(args, "repo", None), getattr(args, "sha", None), getattr(args, "doc", None))
    if all(value is None for value in values):
        return []
    if any(value is None for value in values):
        raise ProtocolRefusal("artifact_binding_incomplete", "repo, sha, and doc must be supplied together")
    return [{"repo": values[0], "sha": values[1], "doc": values[2]}]


def _work_add(args: argparse.Namespace) -> HandlerResult:
    root = _root(args.root)
    if args.owner is None:
        from .solo import resolve_solo_node

        args.owner = resolve_solo_node(root)
    item = WorkLog(root).add(
        args.title,
        args.owner,
        _binding(args),
        needs=args.needs,
        provision_workspace=args.workspace,
    )
    return "ok", item, OK


def _work_claim(args: argparse.Namespace) -> HandlerResult:
    root = _root(args.root)
    current = _parse_time(args.now)
    if any(
        value is None
        for value in (args.actor, args.authority_subject, args.authority_epoch)
    ):
        from .solo import resolve_solo_authority, resolve_solo_node

        solo_node = resolve_solo_node(root)
        actor = solo_node if args.actor is None else args.actor
        authority = resolve_solo_authority(root, actor, current)
        authority_subject = (
            str(authority["authority_subject"])
            if args.authority_subject is None
            else args.authority_subject
        )
        authority_epoch = (
            int(authority["authority_epoch"])
            if args.authority_epoch is None
            else args.authority_epoch
        )
    else:
        actor = args.actor
        authority_subject = args.authority_subject
        authority_epoch = args.authority_epoch
    transition = WorkLog(root).claim(
        args.item_id,
        actor,
        authority_subject,
        authority_epoch,
        now=current,
    )
    return "ok", transition, OK


def _work_complete(args: argparse.Namespace) -> HandlerResult:
    root = _root(args.root)
    if args.actor is None:
        from .solo import resolve_solo_node

        args.actor = resolve_solo_node(root)
    transition = WorkLog(root).complete(
        args.item_id,
        args.actor,
        _binding(args),
        now=_parse_time(args.now),
    )
    return "ok", transition, OK


def _work_show(args: argparse.Namespace) -> HandlerResult:
    items = WorkLog(_root(args.root)).show(args.item_id)
    evidence = {"items": items}
    if not items:
        return "no_result", evidence, NO_RESULT
    return "ok", evidence, OK


def _worker_run(args: argparse.Namespace) -> HandlerResult:
    result = WorkerRunner(
        _root(args.root), {
            "claude": ClaudeHeadlessAdapter(),
            "codex": CodexAppServerAdapter(),
            "pi": PiRpcAdapter(),
        }
    ).run(args.actor, args.adapter)
    if result["transition"] == "degrade":
        return "degraded", result, NO_RESULT
    return "ok", result, OK


def _deploy(args: argparse.Namespace) -> HandlerResult:
    evidence = DeploymentWriter(
        args.source,
        args.destination,
        args.command,
        ref=args.ref,
        committed_tree=args.committed_tree,
    ).run()
    return "ok", evidence, OK


def _board(args: argparse.Namespace) -> int:
    from pathlib import Path
    from .demo import demo_model_loader, seed_demo
    from .tui import acknowledge_visible, model_from_root, run_board

    if args.demo:
        with tempfile.TemporaryDirectory(prefix="floati-demo-") as temporary:
            root = seed_demo(Path(temporary) / "synthetic-fleet")
            return run_board(
                model_loader=demo_model_loader(root),
                ack_callback=lambda action: acknowledge_visible(
                    root, action, acting_session_id="demo-session"
                ),
                no_animation=args.no_animation,
            )
    root = _root(args.root)

    def acknowledge_live(action):
        if args.session is None:
            raise ProtocolRefusal(
                "board_session_required",
                "live board acknowledgment requires one exact acting session",
            )
        acknowledge_visible(root, action, acting_session_id=args.session)

    return run_board(
        model_loader=lambda: model_from_root(root),
        ack_callback=acknowledge_live,
        no_animation=args.no_animation,
    )


def _orchestrate(args: argparse.Namespace) -> int:
    from .orchestrate import FleetOrchestrator, OrchestrationPlan
    from .tui import model_from_orchestration_frame, state_signature
    from .tui_render import render_frame, render_plain_dump

    root = _root(args.root)
    plan = OrchestrationPlan.load(Path(args.plan))
    prior: Optional[str] = None

    def stream(frame: Dict[str, object]) -> None:
        nonlocal prior
        model = model_from_orchestration_frame(frame)
        signature = state_signature(model)
        if signature == prior:
            return
        interactive = bool(getattr(sys.stderr, "isatty", lambda: False)())
        if args.no_animation or not interactive or os.environ.get("TERM") == "dumb":
            rendered = render_plain_dump(model)
        else:
            size = shutil.get_terminal_size((120, 40))
            rendered = (
                "\x1b[?2026h\x1b[H"
                + render_frame(
                    model,
                    size.columns,
                    size.lines,
                    selected=0,
                    color=True,
                )
                + "\x1b[J\x1b[?2026l"
            )
        sys.stderr.write(rendered)
        sys.stderr.flush()
        prior = signature

    result = FleetOrchestrator(
        root,
        {"codex": CodexAppServerAdapter()},
        adapter_name=args.adapter,
    ).run(
        plan,
        deadline_seconds=args.deadline,
        on_frame=stream,
    )
    artifact = {
        "artifact_version": 0,
        "command": "orchestrate",
        "status": result["state"],
        "evidence": result,
    }
    print(
        json.dumps(artifact, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        file=sys.stdout,
    )
    return int(result["return_code"])


def _add_artifact_options(parser: _ArtifactParser) -> None:
    parser.add_argument("--repo")
    parser.add_argument("--sha")
    parser.add_argument("--doc")


def _sequencer_status(args: argparse.Namespace) -> HandlerResult:
    from .sequencer import sequencer_socket_path
    from .sequencer_epoch import SequencerEpochLedger

    root = _root(args.root)
    current = SequencerEpochLedger(root)._current_snapshot()
    open_epoch = current is not None and current["operation"] != "released"
    owner_path = root.resolve_relative("sequencer/owner.lock")
    socket_path = sequencer_socket_path(root)
    live_owner = _existing_lock_is_held(owner_path) if open_epoch else False
    live_service = live_owner and socket_path.exists()
    evidence: Dict[str, Any] = {
        "mode": "managed" if live_service else "direct",
        "managed_epoch_open": open_epoch,
        "local_service_live": live_service,
        "epoch": None if current is None else current["epoch"],
        "sequencer_id": None if current is None else current["sequencer_id"],
        "socket_path": str(socket_path),
    }
    if open_epoch and not live_service:
        evidence["managed_evidence"] = "owner_absent"
    return "ok", evidence, OK


def _existing_lock_is_held(path: Path) -> bool:
    if not path.is_file() or path.is_symlink():
        return False
    try:
        with path.open("rb") as handle:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            finally:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                except OSError:
                    pass
    except OSError:
        return False
    return False


def _sequencer_direct(args: argparse.Namespace) -> HandlerResult:
    from .sequencer_epoch import DirectWriterLease, SequencerEpochLedger

    root = _root(args.root)
    current = SequencerEpochLedger(root)._current_snapshot()
    transitioned = False
    if current is not None and current["operation"] != "released":
        DirectWriterLease.offline_takeover(root, args.actor, None)
        transitioned = True
    else:
        with DirectWriterLease(root):
            pass
    final = SequencerEpochLedger(root)._current_snapshot()
    return "ok", {
        "mode": "direct",
        "managed_epoch_open": False,
        "takeover_recorded": transitioned,
        "epoch": None if final is None else final["epoch"],
    }, OK


def _sequencer_serve(args: argparse.Namespace) -> HandlerResult:
    from .sequencer import SequencerConfig, SequencerService

    root = _root(args.root)
    service = SequencerService(
        root,
        args.actor,
        config=SequencerConfig(takeover=args.takeover),
    )
    stop = threading.Event()
    previous_sigterm = signal.getsignal(signal.SIGTERM)

    def request_stop(_signum: int, _frame: object) -> None:
        stop.set()

    signal.signal(signal.SIGTERM, request_stop)
    try:
        service.serve_forever(stop)
    except KeyboardInterrupt:
        stop.set()
    finally:
        signal.signal(signal.SIGTERM, previous_sigterm)
        service.close()
    return "ok", {
        "mode": "direct",
        "managed_epoch_open": False,
        "released_epoch": service.epoch,
        "sequencer_id": args.actor,
    }, OK


def _wake_callback(args: argparse.Namespace) -> HandlerResult:
    """Internal launchd callback; omitted from static public help and copy."""
    from .wake import OneShotWakeRequest, run_one_shot_wake_callback

    root_path = Path(args.root)
    if not root_path.is_dir() or not (root_path / "tenants" / args.tenant).is_dir():
        raise ProtocolRefusal("wake_root_missing", "wake callback requires its existing exact root and tenant")
    root = FloatiRoot.open(root_path, args.tenant)
    request = OneShotWakeRequest(
        root=root, run_id=args.run_id, item_id=args.item_id, attempt_id=args.attempt_id,
        wake_at=args.wake_at, scheduler_epoch=args.scheduler_epoch, fence_token=args.fence_token,
    )
    return "ok", run_one_shot_wake_callback(request), OK


def _parser() -> _ArtifactParser:
    parser = _ArtifactParser(prog="floati")
    commands = parser.add_subparsers(dest="command", required=True)

    init = commands.add_parser("init")
    init.add_argument("--root")
    init.add_argument("--solo")
    init.add_argument("--harness")
    init.set_defaults(handler=_init)

    register = commands.add_parser("register")
    register.add_argument("--root")
    register.add_argument("node")
    register.add_argument("--harness", required=True)
    register.set_defaults(handler=_register)

    retire = commands.add_parser("retire")
    retire.add_argument("--root")
    retire.add_argument("node")
    retire.set_defaults(handler=_retire)

    register_legacy_workspace_options(register, retire)

    send = commands.add_parser("send")
    send.add_argument("--root")
    send.add_argument("--from", dest="sender", required=True)
    send.add_argument("--to", dest="recipient", required=True)
    send.add_argument("--repo", required=True)
    send.add_argument("--sha", required=True)
    send.add_argument("--doc", required=True)
    send.add_argument("--note", required=True)
    send.add_argument("--reply-to")
    send.add_argument("--idempotency-key")
    send.set_defaults(handler=_send)

    inbox = commands.add_parser("inbox")
    inbox.add_argument("--root")
    inbox.add_argument("--as", dest="recipient", required=True)
    inbox.set_defaults(handler=_inbox)

    wake_evaluate = commands.add_parser("wake-evaluate", help=argparse.SUPPRESS)
    wake_evaluate.add_argument("--root", required=True)
    wake_evaluate.add_argument("--as", dest="recipient", required=True)
    wake_evaluate.add_argument("--idempotency-key", required=True)
    wake_evaluate.add_argument("--worker-session")
    wake_evaluate.add_argument("--limit", type=int, default=1000)
    wake_evaluate.set_defaults(handler=_wake_evaluate, artifact_schema_version=1)

    wake_record = commands.add_parser("wake-record", help=argparse.SUPPRESS)
    wake_record.add_argument("--root", required=True)
    wake_record.add_argument("--as", dest="recipient", required=True)
    wake_record.add_argument("--session", required=True)
    wake_record.add_argument("--id", dest="message_ids", action="append", required=True)
    wake_record.add_argument("--decision")
    wake_record.add_argument("--message-worker-session")
    wake_record.add_argument("--idempotency-key", required=True)
    wake_record.add_argument("--outcome", choices=("woke", "refused"), required=True)
    wake_record.add_argument("--reason-code")
    wake_record.set_defaults(handler=_wake_record, artifact_schema_version=1)

    ack = commands.add_parser("ack")
    ack.add_argument("--root")
    ack.add_argument("--as", dest="recipient", required=True)
    ack.add_argument("--id", dest="message_id", required=True)
    ack.add_argument("--session", required=True)
    ack.set_defaults(handler=_ack)

    log = commands.add_parser("log")
    log.add_argument("--root")
    log.add_argument("--replay", action="store_true")
    log.add_argument("--speed", type=float)
    log.add_argument("--plain", action="store_true")
    log.set_defaults(direct_handler=_log_command)

    status = commands.add_parser("status")
    status.add_argument("--root")
    status.add_argument("--destination")
    status.add_argument("--json", action="store_true")
    status.set_defaults(handler=_status)

    effects = commands.add_parser("effects")
    effects.add_argument("--root")
    effects.add_argument("--run", dest="run_id")
    effects.add_argument("--attempt", dest="attempt_id")
    effects.set_defaults(handler=_effect_status, artifact_schema_version=1)

    effect = commands.add_parser("effect")
    effect_commands = effect.add_subparsers(dest="effect_command", required=True)
    effect_show = effect_commands.add_parser("show")
    effect_show.add_argument("--root")
    effect_show.add_argument("--operation", dest="operation_id", required=True)
    effect_show.set_defaults(handler=_effect_status, artifact_schema_version=1)
    effect_reconcile = effect_commands.add_parser("reconcile")
    effect_reconcile.add_argument("--root")
    effect_reconcile.add_argument("--operation", dest="operation_id", required=True)
    effect_reconcile.set_defaults(handler=_effect_reconcile, artifact_schema_version=1)
    effect_compensate = effect_commands.add_parser("compensate")
    effect_compensate.add_argument("--root")
    effect_compensate.add_argument("--operation", dest="operation_id", required=True)
    compensation_mode = effect_compensate.add_mutually_exclusive_group(required=True)
    compensation_mode.add_argument("--preview", action="store_true")
    compensation_mode.add_argument("--confirm")
    effect_compensate.set_defaults(handler=_effect_compensate, artifact_schema_version=1)

    threads = commands.add_parser("threads")
    threads.add_argument("--root")
    threads.set_defaults(handler=_threads, artifact_schema_version=1)

    thread = commands.add_parser("thread")
    thread_commands = thread.add_subparsers(dest="thread_command", required=True)
    thread_attach = thread_commands.add_parser("attach")
    thread_attach.add_argument("--root")
    thread_attach.add_argument("--as", dest="actor", required=True)
    thread_attach.add_argument("--thread", dest="provider_thread_id", required=True)
    thread_attach.add_argument("--work-item", dest="work_item_id")
    thread_attach.add_argument("--run", dest="run_id")
    thread_attach.add_argument("--attempt", dest="attempt_id")
    thread_attach.set_defaults(handler=_thread_attach, artifact_schema_version=1)
    thread_observe = thread_commands.add_parser("observe")
    thread_observe.add_argument("--root")
    thread_observe.add_argument("--attachment", dest="attachment_id", required=True)
    thread_observe.set_defaults(handler=_thread_observe, artifact_schema_version=1)
    thread_detach = thread_commands.add_parser("detach")
    thread_detach.add_argument("--root")
    thread_detach.add_argument("--as", dest="actor", required=True)
    thread_detach.add_argument("--attachment", dest="attachment_id", required=True)
    thread_detach.set_defaults(handler=_thread_detach, artifact_schema_version=1)
    thread_show = thread_commands.add_parser("show")
    thread_show.add_argument("--root")
    thread_show.add_argument("--attachment", dest="attachment_id", required=True)
    thread_show.set_defaults(handler=_thread_show, artifact_schema_version=1)

    graph = commands.add_parser("graph")
    graph.add_argument("--root")
    graph.add_argument("--json", action="store_true")
    graph.set_defaults(direct_handler=_graph_command)

    plan = commands.add_parser("plan")
    plan.add_argument("--root")
    plan.add_argument("--plan", required=True)
    plan.add_argument("--policy", required=True)
    plan.add_argument("--explain", action="store_true", required=True)
    plan.add_argument("--json", action="store_true")
    plan.set_defaults(handler=_plan)

    doctor = commands.add_parser("doctor")
    doctor.add_argument("--root")
    doctor.add_argument("--source", required=True)
    doctor.add_argument("--ref", default="origin/main")
    doctor.add_argument("--gateway-config")
    doctor.add_argument("--profile")
    doctor.add_argument(
        "--probe", action="store_true",
        help="H3: send a loopback envelope to every registered node and "
             "verify each drains it within the budget (appends probe mail; "
             "per-node PASS/DEAF)",
    )
    doctor.add_argument(
        "--probe-budget", type=float, default=60.0,
        help="probe drain budget in seconds (default 60)",
    )
    doctor.add_argument("--destination")
    doctor.add_argument(
        "--codex-hooks",
        help="exact Codex hooks.json path for per-hook trust measurement",
    )
    doctor.add_argument(
        "--codex-config",
        help="exact Codex config.toml path containing hook trust state",
    )
    doctor.set_defaults(handler=_doctor)

    watch = commands.add_parser("watch")
    watch.add_argument("--root")
    watch.add_argument("--destination")
    watch.add_argument("--interval", type=float, default=0.25)
    watch.add_argument("--iterations", type=int)
    watch.set_defaults(direct_handler=_watch)

    receipts = commands.add_parser("receipts")
    receipts.add_argument("node")
    receipts.add_argument("--root")
    receipts.set_defaults(handler=_receipts)

    supervise = commands.add_parser("supervise")
    supervise.add_argument("--root")
    supervise.set_defaults(handler=_supervise)

    board = commands.add_parser("board")
    board_root = board.add_mutually_exclusive_group()
    board_root.add_argument("--root")
    board_root.add_argument("--demo", action="store_true")
    board.add_argument("--no-animation", action="store_true")
    board.add_argument("--session")
    board.set_defaults(direct_handler=_board)

    orchestrate = commands.add_parser("orchestrate")
    orchestrate.add_argument("--root")
    orchestrate.add_argument("--plan", required=True)
    orchestrate.add_argument("--adapter", choices=("codex",), required=True)
    orchestrate.add_argument("--deadline", type=float, required=True)
    orchestrate.add_argument("--no-animation", action="store_true")
    orchestrate.set_defaults(direct_handler=_orchestrate)

    sequencer = commands.add_parser("sequencer")
    sequencer_commands = sequencer.add_subparsers(
        dest="sequencer_command", required=True
    )
    sequencer_status = sequencer_commands.add_parser("status")
    sequencer_status.add_argument("--root")
    sequencer_status.set_defaults(handler=_sequencer_status)
    sequencer_serve = sequencer_commands.add_parser("serve")
    sequencer_serve.add_argument("--root")
    sequencer_serve.add_argument("--as", dest="actor", required=True)
    sequencer_serve.add_argument("--takeover", action="store_true")
    sequencer_serve.set_defaults(handler=_sequencer_serve)
    sequencer_direct = sequencer_commands.add_parser("direct")
    sequencer_direct.add_argument("--root")
    sequencer_direct.add_argument("--as", dest="actor", required=True)
    sequencer_direct.set_defaults(handler=_sequencer_direct)

    wake_callback = commands.add_parser("wake-callback", help=argparse.SUPPRESS)
    wake_callback.add_argument("--root", required=True)
    wake_callback.add_argument("--tenant", required=True)
    wake_callback.add_argument("--run-id", required=True)
    wake_callback.add_argument("--item-id", required=True)
    wake_callback.add_argument("--attempt-id", required=True)
    wake_callback.add_argument("--wake-at", required=True)
    wake_callback.add_argument("--scheduler-epoch", type=int, required=True)
    wake_callback.add_argument("--fence-token", required=True)
    wake_callback.set_defaults(handler=_wake_callback)

    work = commands.add_parser("work")
    work_commands = work.add_subparsers(dest="work_command", required=True)

    work_add = work_commands.add_parser("add")
    work_add.add_argument("--root")
    work_add.add_argument("--title", required=True)
    work_add.add_argument("--owner")
    work_add.add_argument("--workspace", action="store_true")
    work_add.add_argument("--needs", action="append", default=[])
    _add_artifact_options(work_add)
    work_add.set_defaults(handler=_work_add)

    work_claim = work_commands.add_parser("claim")
    work_claim.add_argument("--root")
    work_claim.add_argument("--id", dest="item_id", required=True)
    work_claim.add_argument("--as", dest="actor")
    work_claim.add_argument("--authority-subject")
    work_claim.add_argument("--authority-epoch", type=int)
    work_claim.add_argument("--now")
    work_claim.set_defaults(handler=_work_claim)

    work_complete = work_commands.add_parser("complete")
    work_complete.add_argument("--root")
    work_complete.add_argument("--id", dest="item_id", required=True)
    work_complete.add_argument("--as", dest="actor")
    work_complete.add_argument("--now")
    _add_artifact_options(work_complete)
    work_complete.set_defaults(handler=_work_complete)

    work_show = work_commands.add_parser("show")
    work_show.add_argument("--root")
    work_show.add_argument("--id", dest="item_id")
    work_show.set_defaults(handler=_work_show)

    worker = commands.add_parser("worker")
    worker_commands = worker.add_subparsers(dest="worker_command", required=True)
    worker_run = worker_commands.add_parser("run")
    worker_run.add_argument("--root")
    worker_run.add_argument("--as", dest="actor", required=True)
    worker_run.add_argument("--adapter", choices=("claude", "codex", "pi"), required=True)
    worker_run.set_defaults(handler=_worker_run)

    for operation in ("install", "update"):
        deployment = commands.add_parser(operation)
        deployment.add_argument("--source", required=True)
        deployment.add_argument("--destination", required=True)
        deployment.add_argument("--ref", default="origin/main")
        deployment.add_argument("--committed-tree", action="store_true")
        deployment.add_argument("--json", action="store_true")
        deployment.set_defaults(handler=_deploy)

    from .grants import register_cli as register_grant

    register_grant(commands)
    register_admin_commands(commands)
    from .context import register_cli as register_context
    from .purge import register_cli as register_purge

    register_context(commands)
    register_purge(commands)
    return parser


def _emit(
    command: Optional[str],
    status: str,
    evidence: Dict[str, Any],
    exit_code: int,
    *,
    schema_version: Optional[int] = None,
) -> None:
    artifact = {
        "artifact_version": 0,
        "command": command,
        "status": status,
        "evidence": evidence,
    }
    if schema_version is not None:
        artifact["schema_version"] = schema_version
    stream = sys.stdout if exit_code == OK else sys.stderr
    if command == "install" and exit_code == OK and stream.isatty():
        from .brand import render_buoy_mark

        print(render_buoy_mark(color=True), file=stream)
    print(
        json.dumps(artifact, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        file=stream,
    )


def main(argv: Optional[Sequence[str]] = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    command = arguments[0] if arguments else None
    artifact_schema_version = (
        1 if command in {"effects", "effect", "threads", "thread", "wake-evaluate", "wake-record"} else None
    )
    static_help = help_for(arguments)
    if static_help is not None:
        print(static_help, end="")
        return OK
    try:
        parsed = _parser().parse_args(arguments)
        if hasattr(parsed, "direct_handler"):
            direct_handler: Callable[[argparse.Namespace], int] = parsed.direct_handler
            return direct_handler(parsed)
        handler: Callable[[argparse.Namespace], HandlerResult] = parsed.handler
        status, evidence, exit_code = handler(parsed)
    except ProtocolRefusal as exc:
        if exc.code == "cannot_speak":
            status, evidence, exit_code = (
                "cannot_speak",
                {"code": exc.code, "detail": exc.detail},
                CANNOT_SPEAK,
            )
        else:
            status, evidence, exit_code = (
                "refused",
                {"code": exc.code, "detail": exc.detail},
                CONFIGURATION_REFUSED,
            )
    except IntegrityFailure as exc:
        status, evidence, exit_code = (
            "malformed_evidence",
            {"code": exc.code, "detail": exc.detail},
            MALFORMED_EVIDENCE,
        )
    except DurabilityFailure as exc:
        status, evidence, exit_code = (
            "degraded",
            {"code": exc.code, "detail": exc.detail},
            DEGRADED,
        )
    _emit(
        command,
        status,
        evidence,
        exit_code,
        schema_version=artifact_schema_version,
    )
    return exit_code

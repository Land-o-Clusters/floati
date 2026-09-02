"""Dependency-free artifact CLI over Floati's direct-home core."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import signal
import shutil
import subprocess
import sys
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from contextlib import contextmanager
from typing import Any, Callable, Dict, Iterator, Optional, Sequence, Tuple, Union

from .cursor import SparseCursor
from .copy import (
    EFFECT_ATTEMPT_INVALID_DETAIL,
    EFFECT_COMPENSATION_PLAN_UNAVAILABLE_DETAIL,
    EFFECT_OPERATION_INVALID_DETAIL,
    EFFECT_PLAN_DIGEST_INVALID_DETAIL,
    EFFECT_RUN_INVALID_DETAIL,
    TUI_DOOR_COPY,
)
from .command_scope import CommandScope, resolve_command_scope
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
from .root import FloatiRoot, resolve_command_root, validate_identifier
from .seat_declaration import (
    COORDINATOR_AUTHORITIES,
    OWNER_TIERS,
    TOPOLOGIES,
    require_declared_coordinate,
    validate_governance_options,
)
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
_SOLO_DOOR_SENTINEL = object()
_SOLO_FULLY_FLAGGED_REMEDY = TUI_DOOR_COPY[
    "tui.door.solo_fully_flagged_remedy"
]

_EXIT_CODE_CONTRACT = (
    {"code": OK, "status": "ok"},
    {"code": CONFIGURATION_REFUSED, "status": "refused"},
    {"code": CANNOT_SPEAK, "status": "cannot_speak"},
    {"code": INTENTIONAL_SILENCE, "status": "intentional_silence"},
    {"code": NO_RESULT, "status": "no_result"},
    {"code": MALFORMED_EVIDENCE, "status": "malformed_evidence"},
    {"code": 34, "status": "orchestration_deadline"},
    {"code": DEGRADED, "status": "degraded"},
)


class _ArtifactParser(argparse.ArgumentParser):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.floati_mcp_exposure = kwargs.pop("floati_mcp_exposure", "never")
        self.floati_mcp_required = tuple(kwargs.pop("floati_mcp_required", ()))
        self.floati_mcp_omit = tuple(kwargs.pop("floati_mcp_omit", ()))
        self.floati_public = kwargs.pop("floati_public", True)
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


def _with_scope(scope: CommandScope, evidence: Dict[str, Any]) -> Dict[str, Any]:
    return {**evidence, "scope": scope.evidence()}


def _scoped_failure(
    scope: CommandScope,
    exc: Union[ProtocolRefusal, IntegrityFailure, DurabilityFailure],
) -> HandlerResult:
    if isinstance(exc, ProtocolRefusal):
        return (
            "refused",
            _with_scope(scope, _protocol_refusal_evidence(exc)),
            CONFIGURATION_REFUSED,
        )
    evidence = _with_scope(scope, {"code": exc.code, "detail": exc.detail})
    if isinstance(exc, IntegrityFailure):
        return "malformed_evidence", evidence, MALFORMED_EVIDENCE
    return "degraded", evidence, DEGRADED


def _confluence_grant(args: argparse.Namespace) -> HandlerResult:
    from .confluence import ConfluenceGrantLedger

    record = ConfluenceGrantLedger(_root(args.root)).grant(
        args.consumer, args.idempotency_key)
    return "ok", {
        "grant_id": record["id"],
        "consumer": record["consumer"],
        "state": record["state"],
    }, OK


def _confluence_revoke(args: argparse.Namespace) -> HandlerResult:
    from .confluence import ConfluenceGrantLedger

    record = ConfluenceGrantLedger(_root(args.root)).revoke(
        args.consumer, args.idempotency_key)
    return "ok", {
        "grant_id": record["id"],
        "consumer": record["consumer"],
        "state": record["state"],
        "predecessor_receipt_id": record["predecessor_receipt_id"],
    }, OK


def _confluence_status(args: argparse.Namespace) -> HandlerResult:
    from .confluence import ConfluenceGrantLedger

    grants = ConfluenceGrantLedger(_root(args.root)).grants()
    return "ok", {
        "grants": [
            {
                "grant_id": record["id"],
                "consumer": record["consumer"],
                "state": record["state"],
                "timestamp": record["timestamp"],
            }
            for record in grants
        ]
    }, OK


def _confluence_adopt(args: argparse.Namespace) -> HandlerResult:
    from .confluence import confluence_adopt

    record = confluence_adopt(
        _root(args.root),
        consumer=args.consumer,
        session=args.session,
        manager=args.manager,
        authority_subject=args.authority_subject,
        authority_epoch=args.authority_epoch,
        authority_expires_at=args.authority_expires_at,
    )
    return "ok", {
        "adoption_id": record["id"],
        "session_id": record["session_id"],
        "mode": record["mode"],
        "manager_node_id": record["manager_node_id"],
        "lease_subject": record["lease_subject"],
        "lease_epoch": record["lease_epoch"],
    }, OK


def _confluence_release(args: argparse.Namespace) -> HandlerResult:
    from .confluence import confluence_release

    record = confluence_release(
        _root(args.root),
        consumer=args.consumer,
        session=args.session,
        manager=args.manager,
        authority_epoch=args.authority_epoch,
    )
    return "ok", {
        "release_id": record["id"],
        "session_id": record["session_id"],
        "adoption_id": record["adoption_id"],
        "manager_node_id": record["manager_node_id"],
    }, OK


def _confluence_bundle(args: argparse.Namespace) -> HandlerResult:
    from .confluence import materialize_bundle

    out = materialize_bundle(
        _root(args.root), consumer=args.consumer, out=Path(args.out))
    document = json.loads(out.read_text(encoding="utf-8"))
    return "ok", {
        "grant_id": document["grant_id"],
        "consumer": args.consumer,
        "out": str(out),
        "entries": len(document["entries"]),
        "snapshot_at": document["snapshot_at"],
    }, OK


def _init(args: argparse.Namespace) -> HandlerResult:
    interactive_solo = args.solo is _SOLO_DOOR_SENTINEL
    if interactive_solo and any(
        value is not None
        for value in (
            args.harness,
            args.topology,
            args.coordinator,
            args.coordinator_authority,
            args.owner_tier,
        )
    ):
        raise ProtocolRefusal(
            "arguments_invalid",
            TUI_DOOR_COPY["tui.door.solo_flags_conflict"],
            _SOLO_FULLY_FLAGGED_REMEDY,
        )
    if args.solo is None and args.harness is not None:
        raise ProtocolRefusal(
            "arguments_invalid", TUI_DOOR_COPY["tui.door.harness_requires_solo"]
        )
    solo_inputs: Optional[tuple[str, str]] = None
    solo_plan: Optional[object] = None
    if interactive_solo:
        from .tui_doors import DoorTerminalIOError, run_solo_door
        from .solo import SoloInitPlan, validate_solo_bootstrap_inputs

        try:
            door_inputs = run_solo_door(
                input_stream=sys.stdin,
                output_stream=sys.stderr,
            )
        except DoorTerminalIOError as exc:
            raise ProtocolRefusal(
                "door_terminal_io_failed",
                TUI_DOOR_COPY["tui.door.terminal_io_failed"],
                _SOLO_FULLY_FLAGGED_REMEDY,
            ) from exc
        if isinstance(door_inputs, SoloInitPlan):
            solo_plan = door_inputs
        else:
            solo_inputs = validate_solo_bootstrap_inputs(*door_inputs)
    elif args.solo is not None:
        from .solo import validate_solo_bootstrap_inputs

        solo_inputs = validate_solo_bootstrap_inputs(
            args.solo, "solo" if args.harness is None else args.harness
        )
    governance = validate_governance_options(
        args.topology,
        args.coordinator,
        args.coordinator_authority,
        args.owner_tier,
    )
    root = _root(args.root, create=True)
    # REL-1: a newly initialized root already owns the two durable bus
    # coordinates Doctor probes.  Their absence before first use is not a
    # sandbox fact; it is an incomplete bootstrap shape.
    for relative in ("cursors", "receipts/deliveries"):
        root.resolve_relative(relative).mkdir(parents=True, exist_ok=True)
    evidence: Dict[str, Any] = {"root": str(root.path), "tenant_id": root.tenant_id}
    if governance is not None:
        recorded = Registry(root).record_governance(
            topology=governance[0],
            coordinator=governance[1],
            coordinator_authority=governance[2],
            owner_tier=governance[3],
        )
        evidence["governance"] = recorded.artifact()
    if solo_plan is not None:
        from .solo import initialize_solo_plan

        evidence["solo"] = initialize_solo_plan(root, solo_plan)
    elif solo_inputs is not None:
        from .solo import initialize_solo

        evidence["solo"] = initialize_solo(root, *solo_inputs)
    return "ok", evidence, OK


def _register(args: argparse.Namespace) -> HandlerResult:
    entry = Registry(_root(args.root)).register(args.node, args.harness)
    return "ok", entry, OK


def _retire(args: argparse.Namespace) -> HandlerResult:
    entry = Registry(_root(args.root)).retire(args.node)
    return "ok", entry, OK


def _journal(args: argparse.Namespace):
    from .journal_chain import JournalChain

    return JournalChain(
        _root(args.root),
        Path(args.journal),
        journal_id=args.journal_id,
        allowed_kinds=set(args.kinds),
    )


def _journal_checkpoint(args: argparse.Namespace) -> HandlerResult:
    checkpoint = _journal(args).write_checkpoint(Path(args.output))
    return "ok", {"state": "checkpointed", **checkpoint}, OK


def _journal_verify(args: argparse.Namespace) -> HandlerResult:
    journal = _journal(args)
    checkpoint = journal.read_checkpoint(Path(args.checkpoint))
    return "ok", journal.verify(checkpoint, historical=args.historical), OK


def _repair_quarantine(args: argparse.Namespace) -> HandlerResult:
    from .ledger_repair import LedgerRepair

    root = _root(args.root)
    receipt = LedgerRepair(root).quarantine(
        args.ledger,
        args.record_id,
        key=args.idempotency_key,
    )
    return "ok", {
        "root": str(root.path),
        "tenant_id": root.tenant_id,
        "receipt": receipt,
    }, OK


def _signature_sign(args: argparse.Namespace) -> HandlerResult:
    from .signing import sign_minisign

    evidence = sign_minisign(
        _root(args.root),
        Path(args.artifact),
        Path(args.signature),
        secret_key=Path(args.secret_key),
        version=args.version,
        journal_id=args.journal_id,
        through_seq=args.through_seq,
        minisign_executable=(
            Path(args.minisign_executable)
            if args.minisign_executable is not None
            else None
        ),
    )
    return "ok", evidence, OK


def _signature_verify(args: argparse.Namespace) -> HandlerResult:
    from .signing import verify_minisign

    evidence = verify_minisign(
        _root(args.root),
        Path(args.artifact),
        Path(args.signature),
        Path(args.public_key),
        version=args.version,
        journal_id=args.journal_id,
        through_seq=args.through_seq,
        minisign_executable=(
            Path(args.minisign_executable)
            if args.minisign_executable is not None
            else None
        ),
    )
    if evidence["state"] == "signature_unverified":
        return "no_result", evidence, NO_RESULT
    return "ok", evidence, OK


def _require_banked_sha(sha: str) -> None:
    """RB-1: refuse a sha committed here but reachable from no remote ref.

    Same instrument and trust posture as the verify path (`verification.py:
    _require_banked`), on the send side: the incident is the sender reporting
    work that was never pushed. A sha absent from this checkout is not this
    fence's case — the verify path's `sha_absent` fence still owns it — so the
    send passes it through; the checked ref set is named in the refusal.
    """
    hardened = ("/usr/bin/git", "--no-optional-locks", "--no-replace-objects",
                "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false")
    try:
        toplevel = subprocess.run(
            [*hardened, "-C", os.getcwd(), "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=10,
        )
    except subprocess.TimeoutExpired:
        raise ProtocolRefusal(
            "repository_invalid",
            f"git did not answer within 10s from {os.getcwd()}",
            "retry once git is responsive, from the checkout that holds the commit",
        )
    if toplevel.returncode != 0:
        return
    repository = toplevel.stdout.strip()
    try:
        exists = subprocess.run(
            [*hardened, "-C", repository, "cat-file", "-e", sha + "^{commit}"],
            capture_output=True, text=True, timeout=10,
        )
        if exists.returncode != 0:
            return
        checked = subprocess.run(
            [*hardened, "-C", repository, "for-each-ref",
             "--format=%(refname)", "refs/remotes"],
            capture_output=True, text=True, timeout=10,
        )
        banked = subprocess.run(
            [*hardened, "-C", repository, "for-each-ref",
             "--format=%(refname)", "--contains", sha, "refs/remotes"],
            capture_output=True, text=True, timeout=10,
        )
    except subprocess.TimeoutExpired:
        raise ProtocolRefusal(
            "repository_invalid",
            f"git did not answer within 10s in {repository}",
            "retry once git is responsive, from the checkout that holds the commit",
        )
    refs = [line for line in banked.stdout.splitlines() if line]
    if refs:
        return
    checked_refs = [line for line in checked.stdout.splitlines() if line]
    shown = (
        ", ".join(checked_refs[:12])
        + (f", +{len(checked_refs) - 12} more" if len(checked_refs) > 12 else "")
        if checked_refs
        else "(none fetched)"
    )
    raise ProtocolRefusal(
        "sha_unbanked",
        f"{sha} is reachable from no ref in refs/remotes of {repository} "
        f"(checked: {shown})",
        f"push the commit, run git fetch --all in {repository}, then send again",
    )


def _send(args: argparse.Namespace) -> HandlerResult:
    root = _root(args.root)
    _require_banked_sha(args.sha)
    claim = None
    if args.claim is not None:
        from .verification import load_claim_document

        claim = load_claim_document(args.claim)
    message = EventLog(root).send(
        args.sender,
        args.recipient,
        args.repo,
        args.sha,
        args.doc,
        args.note,
        reply_to=args.reply_to,
        idempotency_key=args.idempotency_key,
        claim=claim,
    )
    return "ok", message, OK


def _verify(args: argparse.Namespace) -> HandlerResult:
    from .verification import DeliveryVerifier

    receipt = DeliveryVerifier(_root(args.root)).verify(args.actor, args.claim)
    if receipt["outcome"] == "verification_unrunnable":
        evidence = dict(
            receipt,
            code=receipt["reason_code"],
            detail=receipt["remedy"],
        )
        return "refused", evidence, CONFIGURATION_REFUSED
    return "ok", receipt, OK


def _inbox(args: argparse.Namespace) -> HandlerResult:
    root, scope = resolve_command_scope(args.root)
    try:
        if args.peek and args.session is not None:
            raise ProtocolRefusal(
                "inbox_mode_invalid", "inbox accepts --peek or --session, never both"
            )
        if not args.peek and args.session is None:
            raise ProtocolRefusal(
                "inbox_session_required",
                "default inbox drain requires --session; use --peek for an explicit non-acknowledging read",
            )
        identity = require_declared_coordinate(Path.cwd(), args.recipient, root)
        if args.peek:
            messages, receipt = EventLog(root).present(args.recipient)
            acknowledgment = None
        else:
            messages, receipt, acknowledgment = EventLog(root).drain(
                args.recipient, acting_session_id=args.session
            )
    except (ProtocolRefusal, IntegrityFailure, DurabilityFailure) as exc:
        if isinstance(exc, ProtocolRefusal) and exc.code == "unknown_node":
            exc = ProtocolRefusal(
                "recipient_unregistered",
                exc.detail,
                exc.remedy,
            )
        return _scoped_failure(scope, exc)
    evidence = _with_scope(
        scope,
        {
            "messages": messages,
            "receipt": receipt,
            "acknowledgment": acknowledgment,
            **identity,
        },
    )
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
        args.message_ids,
        acting_session_id=args.session,
    )
    return "ok", receipt, OK


def _sent(args: argparse.Namespace) -> HandlerResult:
    from .sent import SentProjection

    evidence = SentProjection(_root(args.root)).artifact(args.sender)
    if not evidence["items"]:
        return "no_result", evidence, NO_RESULT
    return "ok", evidence, OK


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
    root, scope = resolve_command_scope(args.root)
    consumer = getattr(args, "consumer", None)
    if consumer is not None:
        # Confluence dispatch: a consumer that declares itself on the
        # read contract must hold an active grant; the operator path
        # (no --consumer) stays unchanged.
        from .confluence import ConfluenceGrantLedger

        ConfluenceGrantLedger(root).require_active(consumer)
    projection = FleetProjection(root)
    try:
        snapshot = (
            projection.status_artifact(_current_time(), scope=scope)
            if args.json
            else projection.snapshot(_current_time(), scope=scope)
        )
    except (ProtocolRefusal, IntegrityFailure, DurabilityFailure) as exc:
        return _scoped_failure(scope, exc)
    shadow = observe_installer_shadow(getattr(args, "destination", None))
    snapshot["installer_shadow"] = shadow
    if args.json:
        snapshot["status_schema_version"] = 1
    return "ok", snapshot, observation_exit_code(shadow)


def _snapshot_bundle(args: argparse.Namespace) -> HandlerResult:
    from .support_bundle import create_support_bundle

    evidence = create_support_bundle(
        root=_root(args.root),
        source=Path(__file__).resolve().parents[1],
        out=Path(args.out),
        lines=args.lines,
        yes=args.yes,
        stream=sys.stdout,
        input_stream=sys.stdin,
    )
    if not evidence["written"]:
        return "intentional_silence", evidence, INTENTIONAL_SILENCE
    return "ok", evidence, OK


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

    observer = ThreadObserver(
        _root(args.root), codex_executable=args.codex_executable
    )
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
        no_sandbox=getattr(args, "no_sandbox", False),
    )
    artifact, return_code = doctor.artifact()
    if getattr(args, "probe", False):
        probe_artifact, probe_rc = doctor.probe(
            getattr(args, "probe_budget", 60.0) or 60.0
        )
        artifact["probe"] = probe_artifact
        # PROBE-1: a reader of the obvious key sees the DEAF verdict too,
        # with the remediation it already carries.
        artifact["findings"].extend(
            row
            for row in probe_artifact.get("findings", [])
            if row.get("severity") == "error"
        )
        if probe_rc != 0 and return_code == 0:
            return_code = 35
        artifact["state"] = {0: "healthy", 20: "refused", 33: "malformed_evidence",
                             35: "degraded", 36: "sandbox_refused"}.get(return_code, "degraded")
    return str(artifact["state"]), artifact, return_code


def _doctor_command(args: argparse.Namespace) -> int:
    """Keep machine bytes for pipes/--json; dress only an interactive TTY."""

    status, artifact, return_code = _doctor(args)
    if args.json or not sys.stdout.isatty():
        _emit("doctor", status, artifact, return_code)
        return return_code
    from .tui_doctor import render_doctor

    print(render_doctor(artifact), end="")
    return return_code


def _supervise(args: argparse.Namespace) -> HandlerResult:
    snapshot = Supervisor(_root(args.root)).snapshot(_current_time())
    return "ok", snapshot, OK


def _presence_report(args: argparse.Namespace) -> HandlerResult:
    from .presence import PresenceService

    report = PresenceService(_root(args.root)).report_self(
        args.actor,
        ttl_seconds=args.ttl_seconds,
        now=_current_time(),
    )
    return "ok", report, OK


def _presence_show(args: argparse.Namespace) -> HandlerResult:
    from .presence import PresenceService

    current = _current_time()
    reports = PresenceService(_root(args.root)).reports(current)
    return "ok", {
        "observed_at": current.isoformat(timespec="milliseconds").replace(
            "+00:00", "Z"
        ),
        "reports": reports,
    }, OK


def _receipts(args: argparse.Namespace) -> HandlerResult:
    history = FleetProjection(_root(args.root)).receipts(args.node)
    if not any(history[key] for key in ("deliveries", "acks", "denials")):
        return "no_result", history, NO_RESULT
    return "ok", history, OK


WATCH_TRACE_VARIABLE = "FLOATI_WATCH_TRACE"


@contextmanager
def _watch_signal_trace() -> Iterator[None]:
    """WATCH-1: on request, say WHERE this process was when SIGINT arrived.

    The watch child was observed once, under a loaded host, not honouring
    SIGINT within a thirty-second bound while its startup was normal - so the
    question is not "how slow" but "where", and a hang that leaves no trace is
    a row nobody can finish. Two things are recorded, and the pair is what
    classifies the hang:

      * a SIGINT handler that writes ITS OWN frame before re-raising. If this
        line is present, the signal was delivered AND Python ran the handler,
        so whatever blocked came after - a shutdown path, a flush, a lock.
      * `faulthandler` on SIGUSR1, dumping EVERY thread. This is the half that
        survives the case the first half cannot report: a main thread stuck in
        a call that never returns to the interpreter, or a signal that never
        arrives at all. faulthandler writes from inside the signal handler, so
        it works precisely when ordinary Python cannot run.

    Absent the environment variable this is a no-op and the child's signal
    behaviour is byte-for-byte what it was; the variable is set by the test.
    """

    target = os.environ.get(WATCH_TRACE_VARIABLE)
    if not target:
        yield
        return
    import faulthandler
    import traceback

    handle = open(target, "a", encoding="utf-8", buffering=1)
    previous = signal.getsignal(signal.SIGINT)

    def record_interrupt(signum: int, frame: object) -> None:
        handle.write(f"SIGINT_HANDLER_ENTERED pid={os.getpid()} signum={signum}\n")
        handle.write("".join(traceback.format_stack(frame)))
        handle.write("SIGINT_HANDLER_RERAISING\n")
        handle.flush()
        raise KeyboardInterrupt

    try:
        faulthandler.enable(file=handle, all_threads=True)
        if hasattr(signal, "SIGUSR1"):
            faulthandler.register(
                signal.SIGUSR1, file=handle, all_threads=True, chain=False
            )
        signal.signal(signal.SIGINT, record_interrupt)
        handle.write(f"WATCH_TRACE_ARMED pid={os.getpid()}\n")
        handle.flush()
        yield
    finally:
        handle.write("WATCH_TRACE_DISARMED\n")
        handle.flush()
        signal.signal(signal.SIGINT, previous)
        if hasattr(signal, "SIGUSR1"):
            faulthandler.unregister(signal.SIGUSR1)
        faulthandler.disable()
        handle.close()


def _watch(args: argparse.Namespace) -> int:
    root = _root(args.root)
    installer_shadow = observe_installer_shadow(getattr(args, "destination", None))
    exit_code = observation_exit_code(installer_shadow)
    with _watch_signal_trace():
        return _watch_loop(args, root, installer_shadow, exit_code)


def _watch_loop(
    args: argparse.Namespace,
    root: object,
    installer_shadow: object,
    exit_code: int,
) -> int:
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


def _intake_scan(args: argparse.Namespace) -> HandlerResult:
    from .intake import scan_directory

    root = _root(args.root)
    directory = Path(args.directory)
    return "ok", {
        "root": str(root.path),
        "directory": str(directory),
        "verdicts": scan_directory(directory),
    }, OK


def _intake_show(args: argparse.Namespace) -> HandlerResult:
    from .intake import show_snapshots

    root = _root(args.root)
    snapshots = show_snapshots(root, args.snapshot_id)
    if not snapshots:
        return "no_result", {"snapshots": []}, NO_RESULT
    return "ok", {"snapshots": snapshots}, OK


def _intake_adopt(args: argparse.Namespace) -> HandlerResult:
    from .intake import adopt_github, adopt_local

    current = _parse_time(args.now) if args.now is not None else None
    root = _root(args.root)
    if args.source == "local":
        if args.directory is None or args.relative_path is None:
            raise ProtocolRefusal(
                "arguments_invalid", "local intake adopt requires --from and --path"
            )
        if any(value is not None for value in (args.repository, args.issue, args.gh_executable)):
            raise ProtocolRefusal(
                "arguments_invalid", "local intake adopt does not accept GitHub coordinates"
            )
        result = adopt_local(
            root, Path(args.directory), args.relative_path,
            owner=args.owner, now=current,
        )
    else:
        if args.repository is None or args.issue is None or args.gh_executable is None:
            raise ProtocolRefusal(
                "arguments_invalid", "GitHub intake adopt requires --repo, --issue, and --gh"
            )
        if args.directory is not None or args.relative_path is not None:
            raise ProtocolRefusal(
                "arguments_invalid", "GitHub intake adopt does not accept local path coordinates"
            )
        coordinates = args.repository.split("/")
        if len(coordinates) != 2 or not all(coordinates):
            raise ProtocolRefusal(
                "github_repository_invalid", "GitHub repository must use owner/repository coordinates"
            )
        result = adopt_github(
            root, coordinates[0], coordinates[1], args.issue, args.gh_executable,
            owner=args.owner, now=current,
        )
    return "ok", result, OK


def _intake_outbound_request(args: argparse.Namespace) -> Dict[str, object]:
    operation = args.operation
    body = getattr(args, "body", None)
    body_file = getattr(args, "body_file", None)
    labels = getattr(args, "labels", None)
    reason = getattr(args, "reason", None)
    pull_request = getattr(args, "pull_request", None)
    supplied_groups = {
        "comment": body is not None or body_file is not None,
        "label": labels is not None,
        "close": reason is not None,
        "pr_link": pull_request is not None,
    }
    allowed_group = {
        "comment": "comment",
        "label_add": "label",
        "label_remove": "label",
        "close": "close",
        "pr_link": "pr_link",
    }[operation]
    if any(present for group, present in supplied_groups.items() if group != allowed_group):
        raise ProtocolRefusal(
            "intake_request_invalid",
            "intake request flags must match exactly the selected operation",
        )
    if operation == "comment":
        if body is not None and body_file is not None:
            raise ProtocolRefusal(
                "intake_request_body_ambiguous",
                "comment accepts exactly one of --body or --body-file",
            )
        if body_file is not None:
            try:
                with Path(body_file).open("r", encoding="utf-8") as stream:
                    body = stream.read(65_537)
            except (OSError, UnicodeError) as exc:
                raise ProtocolRefusal(
                    "intake_request_invalid",
                    "comment body file must be one readable UTF-8 path",
                ) from exc
        return {"body": "" if body is None else body}
    if operation == "label_add":
        return {"labels": [] if labels is None else labels}
    if operation == "label_remove":
        if labels is None or len(labels) != 1:
            raise ProtocolRefusal(
                "intake_label_invalid", "label_remove requires exactly one --label"
            )
        return {"label": labels[0]}
    if operation == "close":
        return {"state": "closed", "state_reason": reason}
    if pull_request is None:
        return {"body": ""}
    value = str(pull_request)
    if value.isdecimal() and 1 <= len(value) <= 10 and int(value) > 0:
        return {"body": f"Linked pull request: #{value}"}
    return {"body": f"Linked pull request: {value}"}


def _intake_preview(args: argparse.Namespace) -> HandlerResult:
    from .intake import preview_github_mutation

    preview = preview_github_mutation(
        _root(args.root),
        args.snapshot_id,
        args.operation,
        _intake_outbound_request(args),
    )
    return "ok", preview, OK


def _intake_dispatch(args: argparse.Namespace) -> HandlerResult:
    from .intake import dispatch_github_mutation

    intent = dispatch_github_mutation(
        _root(args.root),
        args.snapshot_id,
        args.operation,
        _intake_outbound_request(args),
        confirm_digest=args.confirm_digest,
        run_id=args.run_id,
        item_id=args.item_id,
        attempt_id=args.attempt_id,
        fence_token=args.fence_token,
        approval_request_id=args.approval_request_id,
        approval_decision_id=args.approval_decision_id,
        approval_consumption_id=args.approval_consumption_id,
    )
    return "ok", intent, OK


def _worker_run(args: argparse.Namespace) -> HandlerResult:
    from .fcd20_conformance import (
        ROWS,
        _host_condition,
        resolve_declared_executable,
        validate_declarations,
    )

    validate_identifier(args.actor, "node")
    declarations = validate_declarations(
        {
            "claude": args.claude_executable,
            "codex": args.codex_executable,
            "pi": args.pi_executable,
        }
    )
    spec = next(row for row in ROWS if row.harness == args.adapter)
    resolution = resolve_declared_executable(spec, declarations)
    if resolution.executable is None:
        return "degraded", _host_condition(spec, resolution), DEGRADED

    executable = str(resolution.executable)
    adapters = {
        "claude": lambda: ClaudeHeadlessAdapter((executable,)),
        "codex": lambda: CodexAppServerAdapter(
            (executable, "app-server", "--stdio")
        ),
        "pi": lambda: PiRpcAdapter(
            (executable, "--mode", "rpc", "--no-session")
        ),
    }
    result = WorkerRunner(
        _root(args.root), {args.adapter: adapters[args.adapter]()}
    ).run(args.actor, args.adapter)
    if result["transition"] == "degrade":
        return "degraded", result, NO_RESULT
    return "ok", result, OK


def _deploy(args: argparse.Namespace) -> HandlerResult:
    if args.source is None or args.destination is None:
        raise ProtocolRefusal(
            "arguments_invalid",
            "legacy update requires --source and --destination; fleet updates require an explicit preview or apply subcommand",
        )
    evidence = DeploymentWriter(
        args.source,
        args.destination,
        args.command,
        ref=args.ref,
        committed_tree=args.committed_tree,
    ).run()
    return "ok", evidence, OK


def _update(args: argparse.Namespace) -> HandlerResult:
    action = args.update_action
    if action is None:
        missing = [
            option
            for option, value in (
                ("--source", args.source),
                ("--destination", args.destination),
            )
            if value is None
        ]
        if missing:
            raise ProtocolRefusal(
                "arguments_invalid",
                "the following arguments are required: " + ", ".join(missing),
            )
        return _deploy(args)

    if args.source is not None or args.committed_tree:
        raise ProtocolRefusal(
            "arguments_invalid",
            "update actions do not compose with --source or --committed-tree",
        )
    if args.channel is None:
        raise ProtocolRefusal(
            "arguments_invalid",
            "update action requires --channel naming the exact HTTPS coordinate",
        )
    from .update_consent import UpdateConsentLedger

    destination = Path(args.destination)
    ledger = UpdateConsentLedger(destination)
    if action == "consent":
        if args.epoch is None or args.idempotency_key is None:
            raise ProtocolRefusal(
                "arguments_invalid",
                "update consent requires --epoch and --idempotency-key",
            )
        return "ok", ledger.consent(
            channel=args.channel,
            epoch=args.epoch,
            idempotency_key=args.idempotency_key,
        ), OK
    if action == "revoke":
        if args.idempotency_key is None:
            raise ProtocolRefusal(
                "arguments_invalid",
                "update revoke requires --idempotency-key",
            )
        return "ok", ledger.revoke(
            channel=args.channel,
            idempotency_key=args.idempotency_key,
        ), OK
    if action == "status":
        return "ok", ledger.status(args.channel), OK
    if action == "check":
        if args.idempotency_key is None:
            raise ProtocolRefusal(
                "arguments_invalid",
                "update check requires --idempotency-key",
            )
        from .update_check import check_for_updates

        evidence = check_for_updates(
            destination=destination,
            channel=args.channel,
            entrypoint=destination / "scripts" / "floati",
            idempotency_key=args.idempotency_key,
            minisign_executable=(
                Path(args.minisign_executable)
                if args.minisign_executable is not None
                else None
            ),
        )
        return "ok", evidence, OK
    if action == "apply":
        if args.version is None or args.idempotency_key is None:
            raise ProtocolRefusal(
                "arguments_invalid",
                "update apply requires --version and --idempotency-key",
            )
        from .update_apply import apply_update

        evidence = apply_update(
            destination=destination,
            channel=args.channel,
            entrypoint=destination / "scripts" / "floati",
            version=args.version,
            idempotency_key=args.idempotency_key,
        )
        return "ok", evidence, OK
    raise ProtocolRefusal("arguments_invalid", "unsupported update action")


def _fleet_update_preview(_args: argparse.Namespace) -> HandlerResult:
    raise ProtocolRefusal(
        "fleet_update_target_unavailable",
        "the signed AU-1 target staging boundary is not yet installed",
    )


def _fleet_update_apply(_args: argparse.Namespace) -> HandlerResult:
    raise ProtocolRefusal(
        "fleet_update_apply_not_available",
        "the fleet update receipt saga is not yet installed",
    )


def _add_fleet_update_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--root", required=True)
    parser.add_argument("--as", dest="actor", required=True)
    parser.add_argument("--destination", required=True)
    parser.add_argument("--channel", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--waiter-binding", required=True)
    parser.add_argument("--transport-registry", required=True)
    parser.add_argument("--transport", required=True)
    parser.add_argument("--json", action="store_true")


def _add_update_action_arguments(
    parser: argparse.ArgumentParser,
    action: str,
) -> None:
    parser.add_argument("--source")
    parser.add_argument("--destination", required=True)
    parser.add_argument("--ref", default="origin/main")
    parser.add_argument("--committed-tree", action="store_true")
    parser.add_argument("--channel")
    parser.add_argument("--epoch", type=int)
    parser.add_argument("--idempotency-key")
    parser.add_argument("--version")
    if action == "check":
        parser.add_argument("--minisign-executable")
    parser.add_argument("--json", action="store_true")
    parser.set_defaults(handler=_update, update_action=action)


def _board(args: argparse.Namespace) -> int:
    from pathlib import Path
    from .demo import demo_model_loader, seed_demo
    from .tui import acknowledge_visible, model_from_root, run_board

    if args.demo:
        with tempfile.TemporaryDirectory(prefix="floati-demo-") as temporary:
            root = seed_demo(Path(temporary) / "synthetic-fleet")
            return run_board(
                model_loader=demo_model_loader(root),
                model_root=root.tenant_home,
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
        model_root=root.tenant_home,
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


def _describe(args: argparse.Namespace) -> HandlerResult:
    from .command_contract import describe_parser

    return "ok", describe_parser(_parser()), OK


def _overlap_report(args: argparse.Namespace) -> HandlerResult:
    """Emit the existing local overlap fact through one read-only product verb."""

    from .overlap_radar import derive_overlap_report

    report = derive_overlap_report(
        Path(args.repository),
        args.base_ref,
        args.left_ref,
        args.right_ref,
    )
    return "ok", report, OK


def _mcp_serve(args: argparse.Namespace) -> int:
    from .mcp import serve_bound_stdio

    return serve_bound_stdio(args.root, args.actor, args.session)


def _parser() -> _ArtifactParser:
    parser = _ArtifactParser(prog="floati")
    parser.floati_exit_codes = _EXIT_CODE_CONTRACT
    commands = parser.add_subparsers(dest="command", required=True)

    describe = commands.add_parser("describe", floati_mcp_exposure="read")
    describe.add_argument("--json", action="store_true", required=True)
    describe.set_defaults(handler=_describe)

    overlap = commands.add_parser("overlap")
    overlap_commands = overlap.add_subparsers(
        dest="overlap_command", required=True
    )
    overlap_report = overlap_commands.add_parser("report")
    overlap_report.add_argument("--repository", required=True)
    overlap_report.add_argument("--base-ref", required=True)
    overlap_report.add_argument("--left-ref", required=True)
    overlap_report.add_argument("--right-ref", required=True)
    overlap_report.set_defaults(handler=_overlap_report)

    init = commands.add_parser("init")
    init.add_argument("--root")
    init.add_argument("--solo", nargs="?", const=_SOLO_DOOR_SENTINEL)
    init.add_argument("--harness")
    init.add_argument("--topology", choices=TOPOLOGIES)
    init.add_argument("--coordinator")
    init.add_argument(
        "--coordinator-authority",
        action="append",
        choices=COORDINATOR_AUTHORITIES,
    )
    init.add_argument("--owner-tier", action="append", choices=OWNER_TIERS)
    init.set_defaults(handler=_init)

    confluence = commands.add_parser("confluence")
    confluence_commands = confluence.add_subparsers(
        dest="confluence_command", required=True
    )
    confluence_grant = confluence_commands.add_parser(
        "grant", floati_mcp_exposure="governed"
    )
    confluence_grant.add_argument("--root", required=True)
    confluence_grant.add_argument("--consumer", required=True)
    confluence_grant.add_argument("--idempotency-key", required=True)
    confluence_grant.set_defaults(handler=_confluence_grant)

    confluence_revoke = confluence_commands.add_parser(
        "revoke", floati_mcp_exposure="governed"
    )
    confluence_revoke.add_argument("--root", required=True)
    confluence_revoke.add_argument("--consumer", required=True)
    confluence_revoke.add_argument("--idempotency-key", required=True)
    confluence_revoke.set_defaults(handler=_confluence_revoke)

    confluence_status = confluence_commands.add_parser(
        "status", floati_mcp_exposure="read"
    )
    confluence_status.add_argument("--root", required=True)
    confluence_status.set_defaults(handler=_confluence_status)

    confluence_bundle = confluence_commands.add_parser(
        "bundle", floati_mcp_exposure="read"
    )
    confluence_bundle.add_argument("--root", required=True)
    confluence_bundle.add_argument("--consumer", required=True)
    confluence_bundle.add_argument("--out", required=True)
    confluence_bundle.set_defaults(handler=_confluence_bundle)

    confluence_adopt = confluence_commands.add_parser("adopt")
    confluence_adopt.add_argument("--root", required=True)
    confluence_adopt.add_argument("--consumer", required=True)
    confluence_adopt.add_argument("--session", required=True)
    confluence_adopt.add_argument("--manager", required=True)
    confluence_adopt.add_argument("--authority-subject", required=True)
    confluence_adopt.add_argument("--authority-epoch", type=int, required=True)
    confluence_adopt.add_argument("--authority-expires-at", required=True)
    confluence_adopt.set_defaults(handler=_confluence_adopt)

    confluence_release = confluence_commands.add_parser("release")
    confluence_release.add_argument("--root", required=True)
    confluence_release.add_argument("--consumer", required=True)
    confluence_release.add_argument("--session", required=True)
    confluence_release.add_argument("--manager", required=True)
    confluence_release.add_argument("--authority-epoch", type=int, required=True)
    confluence_release.set_defaults(handler=_confluence_release)

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

    journal = commands.add_parser("journal")
    journal_commands = journal.add_subparsers(dest="journal_command", required=True)

    journal_checkpoint = journal_commands.add_parser("checkpoint")
    journal_checkpoint.add_argument("--root", required=True)
    journal_checkpoint.add_argument("--journal", required=True)
    journal_checkpoint.add_argument("--journal-id", required=True)
    journal_checkpoint.add_argument("--kind", dest="kinds", action="append", required=True)
    journal_checkpoint.add_argument("--output", required=True)
    journal_checkpoint.add_argument("--json", action="store_true")
    journal_checkpoint.set_defaults(handler=_journal_checkpoint)

    journal_verify = journal_commands.add_parser("verify")
    journal_verify.add_argument("--root", required=True)
    journal_verify.add_argument("--journal", required=True)
    journal_verify.add_argument("--journal-id", required=True)
    journal_verify.add_argument("--kind", dest="kinds", action="append", required=True)
    journal_verify.add_argument("--checkpoint", required=True)
    journal_verify.add_argument("--historical", action="store_true")
    journal_verify.add_argument("--json", action="store_true")
    journal_verify.set_defaults(handler=_journal_verify)

    repair = commands.add_parser("repair")
    repair_commands = repair.add_subparsers(dest="repair_command", required=True)
    repair_quarantine = repair_commands.add_parser("quarantine")
    repair_quarantine.add_argument("--root", required=True)
    repair_quarantine.add_argument("--ledger", required=True)
    repair_quarantine.add_argument("--record-id", required=True)
    repair_quarantine.add_argument("--idempotency-key", required=True)
    repair_quarantine.set_defaults(handler=_repair_quarantine)

    signature = commands.add_parser("signature")
    signature_commands = signature.add_subparsers(
        dest="signature_command", required=True
    )

    signature_sign = signature_commands.add_parser("sign")
    signature_sign.add_argument("--root", required=True)
    signature_sign.add_argument("--artifact", required=True)
    signature_sign.add_argument("--signature", required=True)
    signature_sign.add_argument("--secret-key", required=True)
    signature_sign.add_argument("--version", required=True)
    signature_sign.add_argument("--journal-id")
    signature_sign.add_argument("--through-seq", type=int)
    signature_sign.add_argument("--minisign-executable")
    signature_sign.add_argument("--json", action="store_true")
    signature_sign.set_defaults(handler=_signature_sign)

    signature_verify = signature_commands.add_parser("verify")
    signature_verify.add_argument("--root", required=True)
    signature_verify.add_argument("--artifact", required=True)
    signature_verify.add_argument("--signature", required=True)
    signature_verify.add_argument("--public-key", required=True)
    signature_verify.add_argument("--version", required=True)
    signature_verify.add_argument("--journal-id")
    signature_verify.add_argument("--through-seq", type=int)
    signature_verify.add_argument("--minisign-executable")
    signature_verify.add_argument("--json", action="store_true")
    signature_verify.set_defaults(handler=_signature_verify)

    send = commands.add_parser(
        "send",
        floati_mcp_exposure="governed",
        floati_mcp_required=("idempotency_key",),
    )
    send.add_argument("--root")
    send.add_argument("--from", dest="sender", required=True)
    send.add_argument("--to", dest="recipient", required=True)
    send.add_argument("--repo", required=True)
    send.add_argument("--sha", required=True)
    send.add_argument("--doc", required=True)
    send.add_argument("--note", required=True)
    send.add_argument("--reply-to")
    send.add_argument("--idempotency-key")
    send.add_argument("--claim")
    send.set_defaults(handler=_send)

    verify = commands.add_parser("verify")
    verify.add_argument("--root", required=True)
    verify.add_argument("--as", dest="actor", required=True)
    verify.add_argument("--claim", required=True)
    verify.add_argument("--json", action="store_true")
    verify.set_defaults(handler=_verify)

    inbox = commands.add_parser(
        "inbox", floati_mcp_exposure="governed", floati_mcp_omit=("peek",)
    )
    inbox.add_argument("--root")
    inbox.add_argument("--as", dest="recipient", required=True)
    inbox.add_argument("--session")
    inbox.add_argument("--peek", action="store_true")
    inbox.set_defaults(handler=_inbox)

    wake_evaluate = commands.add_parser(
        "wake-evaluate", help=argparse.SUPPRESS, floati_public=False
    )
    wake_evaluate.add_argument("--root", required=True)
    wake_evaluate.add_argument("--as", dest="recipient", required=True)
    wake_evaluate.add_argument("--idempotency-key", required=True)
    wake_evaluate.add_argument("--worker-session")
    wake_evaluate.add_argument("--limit", type=int, default=1000)
    wake_evaluate.set_defaults(handler=_wake_evaluate, artifact_schema_version=1)

    wake_record = commands.add_parser(
        "wake-record", help=argparse.SUPPRESS, floati_public=False
    )
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

    ack = commands.add_parser("ack", floati_mcp_exposure="governed")
    ack.add_argument("--root")
    ack.add_argument("--as", dest="recipient", required=True)
    ack.add_argument("--id", dest="message_ids", action="append", required=True)
    ack.add_argument("--session", required=True)
    ack.set_defaults(handler=_ack)

    sent = commands.add_parser("sent")
    sent.add_argument("--root")
    sent.add_argument("--as", dest="sender", required=True)
    sent.set_defaults(handler=_sent)

    log = commands.add_parser(
        "log",
        floati_mcp_exposure="read",
        floati_mcp_omit=("replay", "speed", "plain"),
    )
    log.add_argument("--root")
    log.add_argument("--replay", action="store_true")
    log.add_argument("--speed", type=float)
    log.add_argument("--plain", action="store_true")
    log.set_defaults(direct_handler=_log_command)

    status = commands.add_parser("status", floati_mcp_exposure="read")
    status.add_argument("--root")
    status.add_argument("--destination")
    status.add_argument("--json", action="store_true")
    status.set_defaults(handler=_status)

    snapshot_bundle = commands.add_parser("snapshot")
    snapshot_bundle.add_argument("--root", required=True)
    snapshot_bundle.add_argument("--out", required=True)
    snapshot_bundle.add_argument("--lines", type=int, default=240)
    snapshot_bundle.add_argument("--yes", action="store_true")
    snapshot_bundle.set_defaults(handler=_snapshot_bundle)

    effects = commands.add_parser("effects", floati_mcp_exposure="read")
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
    thread_observe.add_argument("--codex-executable")
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

    graph = commands.add_parser("graph", floati_mcp_exposure="read")
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

    doctor = commands.add_parser("doctor", floati_mcp_exposure="read")
    doctor.add_argument("--root")
    doctor.add_argument("--source", required=True)
    doctor.add_argument("--ref", default="origin/main")
    doctor.add_argument("--gateway-config")
    doctor.add_argument("--profile")
    doctor.add_argument(
        "--no-sandbox", action="store_true",
        help="skip the default sandbox write-set checks",
    )
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
    doctor.add_argument("--json", action="store_true")
    doctor.set_defaults(direct_handler=_doctor_command)

    watch = commands.add_parser("watch")
    watch.add_argument("--root")
    watch.add_argument("--destination")
    watch.add_argument("--interval", type=float, default=0.25)
    watch.add_argument("--iterations", type=int)
    watch.set_defaults(direct_handler=_watch)

    receipts = commands.add_parser("receipts", floati_mcp_exposure="read")
    receipts.add_argument("node")
    receipts.add_argument("--root")
    receipts.set_defaults(handler=_receipts)

    supervise = commands.add_parser("supervise")
    supervise.add_argument("--root")
    supervise.set_defaults(handler=_supervise)

    presence = commands.add_parser("presence")
    presence_commands = presence.add_subparsers(
        dest="presence_command", required=True
    )
    presence_report = presence_commands.add_parser("report")
    presence_report.add_argument("--root")
    presence_report.add_argument("--as", dest="actor", required=True)
    presence_report.add_argument("--ttl-seconds", type=int, required=True)
    presence_report.set_defaults(handler=_presence_report)
    presence_show = presence_commands.add_parser("show")
    presence_show.add_argument("--root")
    presence_show.set_defaults(handler=_presence_show)

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

    wake_callback = commands.add_parser(
        "wake-callback", help=argparse.SUPPRESS, floati_public=False
    )
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

    work_claim = work_commands.add_parser("claim", floati_mcp_exposure="governed")
    work_claim.add_argument("--root")
    work_claim.add_argument("--id", dest="item_id", required=True)
    work_claim.add_argument("--as", dest="actor")
    work_claim.add_argument("--authority-subject")
    work_claim.add_argument("--authority-epoch", type=int)
    work_claim.add_argument("--now")
    work_claim.set_defaults(handler=_work_claim)

    work_complete = work_commands.add_parser("complete", floati_mcp_exposure="governed")
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

    intake = commands.add_parser("intake")
    intake_commands = intake.add_subparsers(dest="intake_command", required=True)

    intake_scan = intake_commands.add_parser("scan", floati_mcp_exposure="read")
    intake_scan.add_argument("--root", required=True)
    intake_scan.add_argument("--from", dest="directory", required=True)
    intake_scan.set_defaults(handler=_intake_scan)

    intake_show = intake_commands.add_parser("show", floati_mcp_exposure="read")
    intake_show.add_argument("--root", required=True)
    intake_show.add_argument("--id", dest="snapshot_id")
    intake_show.set_defaults(handler=_intake_show)

    intake_adopt = intake_commands.add_parser("adopt")
    intake_adopt.add_argument("--root", required=True)
    intake_adopt.add_argument("--source", choices=("local", "github"), required=True)
    intake_adopt.add_argument("--from", dest="directory")
    intake_adopt.add_argument("--path", dest="relative_path")
    intake_adopt.add_argument("--repo", dest="repository")
    intake_adopt.add_argument("--issue", type=int)
    intake_adopt.add_argument("--gh", dest="gh_executable")
    intake_adopt.add_argument("--owner")
    intake_adopt.add_argument("--now")
    intake_adopt.set_defaults(handler=_intake_adopt)

    def add_intake_request_arguments(command: argparse.ArgumentParser) -> None:
        command.add_argument(
            "--operation",
            choices=("comment", "label_add", "label_remove", "close", "pr_link"),
            required=True,
        )
        command.add_argument("--body")
        command.add_argument("--body-file")
        command.add_argument("--label", dest="labels", action="append")
        command.add_argument("--reason", choices=("completed", "not_planned"))
        command.add_argument("--pr", dest="pull_request")

    intake_preview = intake_commands.add_parser(
        "preview", floati_mcp_exposure="read", floati_mcp_omit=("body_file",)
    )
    intake_preview.add_argument("--root", required=True)
    intake_preview.add_argument("--snapshot", dest="snapshot_id", required=True)
    add_intake_request_arguments(intake_preview)
    intake_preview.set_defaults(handler=_intake_preview)

    intake_dispatch = intake_commands.add_parser("dispatch")
    intake_dispatch.add_argument("--root", required=True)
    intake_dispatch.add_argument("--snapshot", dest="snapshot_id", required=True)
    add_intake_request_arguments(intake_dispatch)
    intake_dispatch.add_argument("--confirm-digest", required=True)
    intake_dispatch.add_argument("--run-id", required=True)
    intake_dispatch.add_argument("--item-id", required=True)
    intake_dispatch.add_argument("--attempt-id", required=True)
    intake_dispatch.add_argument("--fence-token", required=True)
    intake_dispatch.add_argument("--approval-request", dest="approval_request_id")
    intake_dispatch.add_argument("--approval-decision", dest="approval_decision_id")
    intake_dispatch.add_argument("--approval-consumption", dest="approval_consumption_id")
    intake_dispatch.set_defaults(handler=_intake_dispatch)

    worker = commands.add_parser("worker")
    worker_commands = worker.add_subparsers(dest="worker_command", required=True)
    worker_run = worker_commands.add_parser("run")
    worker_run.add_argument("--root")
    worker_run.add_argument("--as", dest="actor", required=True)
    worker_run.add_argument("--adapter", choices=("claude", "codex", "pi"), required=True)
    worker_run.add_argument("--claude-executable")
    worker_run.add_argument("--codex-executable")
    worker_run.add_argument("--pi-executable")
    worker_run.set_defaults(handler=_worker_run)

    mcp = commands.add_parser("mcp")
    mcp_commands = mcp.add_subparsers(dest="mcp_command", required=True)
    mcp_serve = mcp_commands.add_parser("serve")
    mcp_serve.add_argument("--root", required=True)
    mcp_serve.add_argument("--as", dest="actor", required=True)
    mcp_serve.add_argument("--session", required=True)
    mcp_serve.set_defaults(direct_handler=_mcp_serve)

    install = commands.add_parser("install")
    install.add_argument("--source", required=True)
    install.add_argument("--destination", required=True)
    install.add_argument("--ref", default="origin/main")
    install.add_argument("--committed-tree", action="store_true")
    install.add_argument("--json", action="store_true")
    install.set_defaults(handler=_deploy)

    update = commands.add_parser("update")
    update.add_argument("--source")
    update.add_argument("--destination")
    update.add_argument("--ref", default="origin/main")
    update.add_argument("--committed-tree", action="store_true")
    update.add_argument("--json", action="store_true")
    update.set_defaults(handler=_update, update_action=None)
    update_commands = update.add_subparsers(dest="update_command")
    for update_action in ("consent", "revoke", "status", "check", "apply"):
        update_action_parser = update_commands.add_parser(
            update_action,
            help=argparse.SUPPRESS,
            floati_public=False,
        )
        _add_update_action_arguments(update_action_parser, update_action)
    update_fleet = update_commands.add_parser("fleet")
    update_fleet_commands = update_fleet.add_subparsers(
        dest="fleet_update_command", required=True
    )
    update_fleet_preview = update_fleet_commands.add_parser("preview")
    _add_fleet_update_arguments(update_fleet_preview)
    update_fleet_preview.set_defaults(handler=_fleet_update_preview)
    update_fleet_apply = update_fleet_commands.add_parser("apply")
    _add_fleet_update_arguments(update_fleet_apply)
    update_fleet_apply.add_argument("--plan-digest", required=True)
    update_fleet_apply.add_argument("--idempotency-key", required=True)
    update_fleet_apply.set_defaults(handler=_fleet_update_apply)

    from .bus_epoch import register_cli as register_epoch
    from .grants import register_cli as register_grant

    register_epoch(commands)
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
    # PROBE-1: doctor's completed measurement prints its artifact on stdout
    # whatever it measured (34 deadline, 35 degraded) — a script redirecting
    # stdout captures the probe verdict. Every other verb keeps the stock
    # channel: ok on stdout, silence/no-result/refusal-class on stderr.
    if exit_code == OK:
        stream = sys.stdout
    elif command == "doctor" and exit_code in (34, DEGRADED):
        stream = sys.stdout
    else:
        stream = sys.stderr
    if command == "install" and exit_code == OK and stream.isatty():
        from .brand import render_buoy_mark

        print(render_buoy_mark(color=True), file=stream)
    print(
        json.dumps(artifact, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        file=stream,
    )


def _protocol_refusal_evidence(exc: ProtocolRefusal) -> Dict[str, object]:
    evidence: Dict[str, object] = {
        "code": exc.code,
        "detail": exc.detail,
        "remedy": exc.remedy,
    }
    context = getattr(exc, "artifact_context", None)
    if isinstance(context, dict) and set(context) == {"root", "tenant_id"}:
        evidence = {**context, **evidence}
    return evidence


def main(argv: Optional[Sequence[str]] = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    command = arguments[0] if arguments else None
    parser = _parser()
    from .command_contract import schema_version_for_arguments

    artifact_schema_version = schema_version_for_arguments(parser, arguments)
    static_help = help_for(arguments)
    if static_help is not None:
        print(static_help, end="")
        return OK
    try:
        parsed = parser.parse_args(arguments)
        if hasattr(parsed, "direct_handler"):
            direct_handler: Callable[[argparse.Namespace], int] = parsed.direct_handler
            return direct_handler(parsed)
        handler: Callable[[argparse.Namespace], HandlerResult] = parsed.handler
        status, evidence, exit_code = handler(parsed)
    except ProtocolRefusal as exc:
        evidence = _protocol_refusal_evidence(exc)
        if exc.code == "cannot_speak":
            status, evidence, exit_code = (
                "cannot_speak",
                evidence,
                CANNOT_SPEAK,
            )
        else:
            status, evidence, exit_code = (
                "refused",
                evidence,
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

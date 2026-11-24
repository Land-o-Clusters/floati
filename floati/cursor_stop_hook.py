"""Scoped Cursor host observations; fixture contract, native adoption pending.

Public host IDs identify an occurrence, not authority. Only the current native
workspace, armed consent, session and claimed car can admit a callback. No tool
text, transcript, decision or fabricated running event crosses this boundary.
"""
from hashlib import sha256
import json
import math
from pathlib import Path
import sys

from .codex_tool_hook import _source_acceptance_guard
from .codex_wait_contract import (CodexWaitConsentLedger,
    CodexWaitSessionLedger, resolve_participant)
from .errors import DurabilityFailure, IntegrityFailure, ProtocolRefusal
from .jsonl import read_records_snapshot
from .records import _terminal_unsafe_hit
from .registry import Registry, utc_now
from .seat_execution import SeatExecutionController, _history
from .wake_control import is_session_paused, validate_session_id


CONTRACT_PIN = "044ce0931100af5fe95403e9a692b02932bc3e13a6763f576b851f503fefa518"
CURSOR_VERSION = "3.24.12"


def _text(value, cap=128):
    return isinstance(value, str) and 1 <= len(value) <= cap and _terminal_unsafe_hit(value) is None


def _common(payload, event):
    if not isinstance(payload, dict) or payload.get("hook_event_name") != event or (
        payload.get("cursor_version") != CURSOR_VERSION
    ):
        return False
    if any(not _text(payload.get(field)) for field in ("conversation_id", "generation_id")):
        return False
    # A delegated tool call is not the current main-thread seat invocation.
    if payload.get("parent_tool_call_id") not in (None, ""):
        return False
    return True


def observe_cursor_stop(bus_home, payload):
    """Retain the native loop outcome before the existing waiter starts."""
    if not _common(payload, "stop") or payload.get("status") not in (
        "completed", "aborted", "error"
    ) or type(payload.get("loop_count")) is not int or not 0 <= payload["loop_count"] <= 2**31 - 1:
        return 0
    binding = {"contract_pin": CONTRACT_PIN, "cursor_version": CURSOR_VERSION,
        "conversation_id": payload["conversation_id"], "generation_id": payload["generation_id"],
        "loop_count": payload["loop_count"], "status": payload["status"]}
    return _observe(bus_home, payload, "cursor_host", "ended", binding,
        [payload["conversation_id"], payload["generation_id"], payload["loop_count"]])


def observe_cursor_tool_failure(bus_home, payload):
    """A native denial is an after-fact observation, never pending approval."""
    if not _common(payload, "postToolUseFailure") or payload.get("failure_type") != "permission_denied":
        return 0
    if any(not _text(payload.get(field)) for field in ("tool_use_id", "tool_name")):
        return 0
    if "duration" in payload and (type(payload["duration"]) not in (int, float)
        or not 0 <= payload["duration"] <= 2**53 - 1 or not math.isfinite(payload["duration"])):
        return 0
    if "is_interrupt" in payload and type(payload["is_interrupt"]) is not bool:
        return 0
    binding = {"contract_pin": CONTRACT_PIN, "cursor_version": CURSOR_VERSION,
        "hook_event_name": "postToolUseFailure", "conversation_id": payload["conversation_id"],
        "generation_id": payload["generation_id"], "tool_use_id": payload["tool_use_id"],
        "tool_name": payload["tool_name"], "failure_type": "permission_denied"}
    return _observe(bus_home, payload, "cursor_tool", "tool_request_refused", binding,
        [payload["conversation_id"], payload["generation_id"], payload["tool_use_id"]])


def _observe(bus_home, payload, source, event, binding, occurrence):
    try:
        workspace = Path.cwd()
        if not workspace.is_absolute() or workspace.resolve(strict=True) != workspace or not workspace.is_dir():
            return 0
        participant = resolve_participant(Path(bus_home), workspace, _strict=True)
        if participant is None:
            return 0
        root, node = participant.root, participant.binding.node_id
        with _source_acceptance_guard(root, node):
            current = resolve_participant(Path(bus_home), workspace, _strict=True)
            if current is None or current.binding != participant.binding:
                return 0
            if Registry(root).require_active(node)["role"].casefold() != "cursor":
                return 0
            consent = CodexWaitConsentLedger(root).require_armed(current.binding)
            session = validate_session_id(payload["conversation_id"])
            if is_session_paused(root, node, session):
                return 0
            claims = read_records_snapshot(root, CodexWaitSessionLedger._relative(node),
                allowed_kinds={"codex_wait_session_receipt"})
            # A canonical descendant runs in its actual cwd, but native seat
            # receipts belong to the mapped root workspace, not that cwd.
            matching = [row for row in claims if row["workspace"] == str(current.binding.workspace)]
            claim = matching[-1] if matching else None
            if claim is None or claim["acting_session_id"] != session or claim["consent_receipt_id"] != consent["id"]:
                return 0
            controller = SeatExecutionController(root)
            # Check the unique current car before replay lookup or issuance;
            # historical callbacks cannot select a missing or ambiguous car.
            controller._car(node)
            invocation = source.replace("_", "-") + "-" + sha256(json.dumps(
                [node, *occurrence], separators=(",", ":")).encode()).hexdigest()
            binding = {**binding, "consent_receipt_id": consent["id"], "session_claim_id": claim["id"]}
            if source == "cursor_tool":
                saved = [row for row in _history(root) if row.get("kind") == "seat_execution"
                    and row["source_kind"] == source and row["invocation_id"] == invocation]
                # The first native observation owns T; replay never refreshes
                # it from a host-supplied timestamp or the replay's clock.
                binding["observed_at"] = saved[0]["source_binding"]["observed_at"] if saved else utc_now()
            capability = controller._issue(node=node, session=session, workspace=workspace,
                invocation=invocation, source=source, receipt_id=claim["id"],
                source_pin=sha256(Path(__file__).read_bytes()).hexdigest(), command=None,
                source_binding=binding, running=False, already_guarded=True)
            controller._callback_already_guarded(capability,
                {"event": event, "code": None, "detail": None})
    except (DurabilityFailure, IntegrityFailure) as exc:
        print(json.dumps({"status": "degraded", "source": source,
            "code": exc.code}, ensure_ascii=True, sort_keys=True), file=sys.stderr)
    except (ProtocolRefusal, OSError, ValueError):
        # Unsupported or stale authority supplies no invented host testimony.
        pass
    return 0

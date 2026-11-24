"""Main-thread PostToolUse refusal testimony; fixture-qualified, adoption pending.

The pinned installed outer contract allows arbitrary tool_response JSON. This
adapter admits only the measured fixture's exit_code/output object; it does not
guess other serializers, infer completion, or execute command text. Installing
the hook and measuring a live response remain an explicit owner-seat operation.
"""
from hashlib import sha256
from contextlib import contextmanager, ExitStack
import json
from pathlib import Path
import re
import sys
import time

from .car_fence import car_acceptance_guard
from .bus_epoch import epoch_guard
from .codex_wait_contract import (CodexWaitConsentLedger,
    CodexWaitSessionLedger, resolve_participant)
from .errors import DurabilityFailure, IntegrityFailure, ProtocolRefusal
from .jsonl import _locked_path, read_records_snapshot
from .records import _terminal_unsafe_hit
from .registry import Registry
from .seat_execution import SeatExecutionController
from .wake_control import is_session_paused

CONTRACT_PIN = "8ea1e4bccb262fad05b85c300d562d2653c5a64118d6a2c5704468fc4ea836a9"
_FIELDS = {"cwd", "hook_event_name", "model", "permission_mode", "session_id",
    "transcript_path", "turn_id", "tool_name", "tool_use_id", "tool_input", "tool_response"}


def _text(value, cap):
    return isinstance(value, str) and 1 <= len(value) <= cap and _terminal_unsafe_hit(value) is None


def _refusal(payload):
    # A closed main-thread shape admits no caller-car, node, authentication or
    # delegated-agent fields. Never find a refusal by scanning assistant prose.
    if not isinstance(payload, dict) or set(payload) != _FIELDS or (
        payload["hook_event_name"] != "PostToolUse" or payload["tool_name"] != "Bash"
        or not isinstance(payload["permission_mode"], str)
        or payload["permission_mode"] not in {"default", "acceptEdits", "plan", "dontAsk", "bypassPermissions"}
    ):
        return None
    if any(not _text(payload[field], cap) for field, cap in (
        ("cwd", 4096), ("model", 128), ("session_id", 128), ("turn_id", 128), ("tool_use_id", 128)
    )) or (payload["transcript_path"] is not None and not _text(payload["transcript_path"], 4096)):
        return None
    request, response = payload["tool_input"], payload["tool_response"]
    if not isinstance(request, dict) or set(request) != {"command"} or not _text(request["command"], 1024):
        return None
    if re.match(r"(?:~|/[^\s]+?)/\.codex/bin/codex-[a-z0-9-]+(?: |$)", request["command"]) is None:
        return None
    if not isinstance(response, dict) or set(response) != {"exit_code", "output"} or (
        type(response["exit_code"]) is not int or response["exit_code"] != 64
        or not isinstance(response["output"], str) or not 1 <= len(response["output"]) <= 8192
        or any(0xD800 <= ord(char) <= 0xDFFF for char in response["output"])
    ):
        return None
    code = re.match(r"(CODEX_[A-Z0-9_]+_APPROVAL_REQUIRED)(?=: |$)", response["output"])
    if code is None or len(code[1]) > 128:
        return None
    return request["command"], code[1], response["output"]


@contextmanager
def _source_acceptance_guard(root, node):
    # Existing board operations hold shared epoch before pause control; bare
    # pause holds its marker lock before appending under shared epoch. Take
    # exclusive epoch first, try pause without waiting, and release/retry both
    # on contention so neither ordering can form a cycle.
    relative = Path("state/wake-control") / (node + ".lock")
    deadline = time.monotonic() + 5.0
    while True:
        with ExitStack() as stack:
            stack.enter_context(epoch_guard(root, exclusive=True))
            try:
                stack.enter_context(_locked_path(root.resolve_relative(relative),
                    exclusive=True, relative=relative, timeout_seconds=0.0, order_tracked=False))
            except ProtocolRefusal as exc:
                if exc.code != "ledger_lock_timeout" or time.monotonic() >= deadline:
                    raise
            else:
                stack.enter_context(car_acceptance_guard(root))
                yield
                return
        time.sleep(min(0.01, max(0.0, deadline - time.monotonic())))


def run_tool_hook(*, bus_home, hook_payload):
    """Handle PostToolUse silently; no waiter, presentation or acknowledgment.

    Callers must route every PostToolUse here, even unsupported/successful
    calls. Native guards span current authority checks, issuance and the
    atomic execution/envelope/readiness publication.
    """
    from .host_observations import observe_codex_host
    observe_codex_host(bus_home, hook_payload)
    refusal = _refusal(hook_payload)
    if refusal is None:
        return 0
    command, code, detail = refusal
    try:
        workspace = Path(hook_payload["cwd"])
        if not workspace.is_absolute() or workspace.resolve(strict=True) != workspace or not workspace.is_dir():
            return 0
        participant = resolve_participant(Path(bus_home), workspace, _strict=True)
        if participant is None:
            return 0
        root, node = participant.root, participant.binding.node_id
        with _source_acceptance_guard(root, node):
            # Resolve again inside the retained native guards, never reuse an
            # outside mapping as authority for accepting a callback.
            current = resolve_participant(Path(bus_home), workspace, _strict=True)
            if current is None or current.binding != participant.binding:
                return 0
            if Registry(root).require_active(node)["role"].casefold() != "codex":
                return 0
            consent = CodexWaitConsentLedger(root).require_armed(current.binding)
            session = hook_payload["session_id"]
            if is_session_paused(root, node, session):
                return 0
            claims = read_records_snapshot(root, CodexWaitSessionLedger._relative(node),
                allowed_kinds={"codex_wait_session_receipt"})
            matching = [row for row in claims if row["workspace"] == str(current.binding.workspace)]
            claim = matching[-1] if matching else None
            if claim is None or claim["acting_session_id"] != session or claim["consent_receipt_id"] != consent["id"]:
                return 0
            controller = SeatExecutionController(root)
            binding = {"contract_pin": CONTRACT_PIN, "turn_id": hook_payload["turn_id"],
                "tool_use_id": hook_payload["tool_use_id"], "tool_name": "Bash",
                "consent_receipt_id": consent["id"], "session_claim_id": claim["id"]}
            invocation = "codex-tool-" + sha256(json.dumps(
                [session, binding["turn_id"], binding["tool_use_id"]],
                separators=(",", ":")).encode()).hexdigest()
            capability = controller._issue(node=node, session=session, workspace=workspace,
                invocation=invocation, source="codex_tool", receipt_id=claim["id"],
                source_pin=sha256(Path(__file__).read_bytes()).hexdigest(), command=command,
                source_binding=binding, already_guarded=True)
            controller._callback_already_guarded(capability,
                {"event": "refused_action", "code": code, "detail": detail})
    except (DurabilityFailure, IntegrityFailure) as exc:
        print(json.dumps({"status": "degraded", "source": "codex_tool",
            "code": exc.code}, ensure_ascii=True, sort_keys=True), file=sys.stderr)
        return 0
    except (ProtocolRefusal, OSError):
        # No supported source authority means no invented execution event.
        # Durable native failures retain their existing evidence for diagnosis.
        return 0
    return 0

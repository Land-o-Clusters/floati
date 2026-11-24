"""Observational desktop notifications, never permission/Stop decisions.

Contracts are pinned in the fixture inventory. Claude occurrences coalesce by
native prompt and type, not a fabricated notification UUID. Grok idle is a
session observation with a native dispatch clock, not an identified turn end.
Native fields identify observations; current local receipts supply authority.
No transcript, message, title, command or tool output is retained or executed.
"""
import argparse
from hashlib import sha256
import json
from pathlib import Path
import re
import sys

from .codex_tool_hook import _source_acceptance_guard, _text
from .codex_wait_contract import CodexWaitConsentLedger, CodexWaitSessionLedger, resolve_participant
from .errors import FloatiError
from .jsonl import read_records_snapshot
from .recipient_mechanisms import _time
from .registry import Registry, utc_now
from .seat_execution import SeatExecutionController, _history
from .wake_control import is_session_paused, validate_session_id


CONTRACT_PIN = "167b2548837b5ed8163c850355fe125f8c0b6a9c81961188c807740d9e2eb952"
_CLAUDE_REQUIRED = {"session_id", "cwd", "transcript_path", "hook_event_name", "notification_type", "message"}
_CLAUDE_OPTIONAL = {"prompt_id", "title", "permission_mode", "scratchpad_dir", "effort", "agent_id", "agent_type"}
_GROK_REQUIRED = {"hookEventName", "sessionId", "cwd", "workspaceRoot", "timestamp", "permissionMode", "notificationType", "message"}
_GROK_OPTIONAL = {"promptId", "subagentType", "hook_event_name"}
_UUID = re.compile(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}\Z")


def _absence(harness, code):
    print(json.dumps({"status": "no_result", "source": "desktop_host",
        "harness": harness, "code": code}, sort_keys=True), file=sys.stderr)


def _notification(harness, payload):
    if not isinstance(payload, dict):
        return None
    event_id = None
    if harness == "opencode":
        if set(payload) != {"cwd", "worktree", "event"} or not _text(payload["worktree"], 4096):
            return None
        event = payload["event"]
        if not isinstance(event, dict) or set(event) != {"id", "type", "properties"}:
            return None
        event_id = event["id"]
        properties = event["properties"]
        if not _text(event_id, 128) or not isinstance(properties, dict):
            return None
        session = properties.get("sessionID")
        if event["type"] == "permission.asked":
            required = {"id", "sessionID", "permission", "patterns", "metadata", "always"}
            if not required <= set(properties) <= required | {"tool"} or not _text(properties["id"], 128):
                return None
            if not _text(properties["permission"], 128) or not isinstance(properties["metadata"], dict):
                return None
            for field in ("patterns", "always"):
                if not isinstance(properties[field], list) or not all(isinstance(v, str) for v in properties[field]):
                    return None
            if "tool" in properties and (not isinstance(properties["tool"], dict)
                or set(properties["tool"]) != {"messageID", "callID"}
                or not all(_text(v, 128) for v in properties["tool"].values())):
                return None
            kind = "permission_prompt"
        elif event["type"] == "session.idle" and set(properties) == {"sessionID"}:
            kind = "idle_prompt"
        else:
            return None
        if not _text(session, 128) or not _text(payload["cwd"], 4096):
            return None
        return session, kind, None, None, event_id
    if harness == "claude":
        if not _CLAUDE_REQUIRED <= set(payload) <= _CLAUDE_REQUIRED | _CLAUDE_OPTIONAL:
            return None
        if payload["hook_event_name"] != "Notification" or payload.get("agent_id") or payload.get("agent_type"):
            return None
        kind, session = payload["notification_type"], payload["session_id"]
        prompt, native_stamp = payload.get("prompt_id"), None
        if prompt is None:
            _absence(harness, "desktop_prompt_identity_absent")
            return None
        if not isinstance(prompt, str) or _UUID.fullmatch(prompt) is None:
            return None
        if payload["transcript_path"] is not None and not _text(payload["transcript_path"], 4096):
            return None
    elif harness == "grok-build":
        if not _GROK_REQUIRED <= set(payload) <= _GROK_REQUIRED | _GROK_OPTIONAL:
            return None
        if payload["hookEventName"] != "notification" or payload.get("subagentType"):
            return None
        if "hook_event_name" in payload and payload["hook_event_name"] != "Notification":
            return None
        if payload["permissionMode"] not in {"default", "auto", "plan", "bypassPermissions"}:
            return None
        kind, session = payload["notificationType"], payload["sessionId"]
        prompt, native_stamp = payload.get("promptId"), payload["timestamp"]
        if not _text(native_stamp, 128):
            return None
        _time(native_stamp)
        if kind == "permission_prompt" and not _text(prompt, 128):
            _absence(harness, "desktop_prompt_identity_absent")
            return None
        if kind == "idle_prompt" and prompt is not None:
            return None  # This contract is explicitly session scoped.
        if not _text(payload["workspaceRoot"], 4096):
            return None
    else:
        return None
    if kind not in {"permission_prompt", "idle_prompt"} or not _text(session, 128):
        return None
    if not _text(payload["cwd"], 4096) or not isinstance(payload["message"], str) or len(payload["message"]) > 65536:
        return None
    return session, kind, prompt, native_stamp, event_id


def observe_desktop_notification(bus_home, harness, payload):
    """Authenticate locally and publish one atomic notification outcome.

    No caller node, car, recipient, authority flag or session override is
    accepted. Exact native prompt/type replays reuse the original observation.
    A Grok idle callback has a native timestamp but no turn ID; it reports only
    the session-idle observation, never a turn-end timestamp.
    """
    try:
        occurrence = _notification(harness, payload)
        if occurrence is None:
            return 0
        session, kind, prompt, native_stamp, event_id = occurrence
        validate_session_id(session)
        workspace = Path(payload["cwd"])
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
            role = Registry(root).require_active(node)["role"].casefold()
            roles = {"claude": {"claude", "claude code", "claude-code"}, "grok-build": {"grok-build"}, "opencode": {"opencode"}}
            if role not in roles[harness]:
                return 0
            if harness == "grok-build" and payload["workspaceRoot"] != str(current.binding.workspace):
                return 0
            if harness == "opencode" and payload["worktree"] != str(current.binding.workspace):
                return 0
            consent = CodexWaitConsentLedger(root).require_armed(current.binding)
            if is_session_paused(root, node, session):
                return 0
            claims = read_records_snapshot(root, CodexWaitSessionLedger._relative(node),
                allowed_kinds={"codex_wait_session_receipt"})
            matching = [r for r in claims if r["workspace"] == str(current.binding.workspace)]
            claim = matching[-1] if matching else None
            if claim is None or claim["acting_session_id"] != session or claim["consent_receipt_id"] != consent["id"]:
                return 0
            controller = SeatExecutionController(root)
            controller._car(node)
            invocation = "desktop-host-" + sha256(json.dumps([harness, node, session,
                claim["id"], kind, event_id if event_id is not None else (prompt if prompt is not None else native_stamp)],
                separators=(",", ":")).encode()).hexdigest()
            saved = [r for r in _history(root) if r.get("kind") == "seat_execution"
                and r["source_kind"] == "desktop_host" and r["invocation_id"] == invocation]
            binding = {"contract_pin": CONTRACT_PIN, "harness": harness,
                "hook_event_name": (payload["event"]["type"] if harness == "opencode" else
                    ("Notification" if harness == "claude" else "notification")),
                "notification_type": kind, "native_prompt_id": prompt,
                "native_timestamp": native_stamp, "native_event_id": event_id, "observed_at": utc_now(),
                "consent_receipt_id": consent["id"], "session_claim_id": claim["id"]}
            if saved:
                # Message/title and a later native notification clock cannot
                # refresh the first accepted occurrence for this prompt/type.
                binding = saved[0]["source_binding"]
            capability = controller._issue(node=node, session=session, workspace=workspace,
                invocation=invocation, source="desktop_host", receipt_id=claim["id"],
                source_pin=sha256(Path(__file__).read_bytes()).hexdigest(), command=None,
                source_binding=binding, running=False, already_guarded=True)
            controller._callback_already_guarded(capability,
                {"event": "approval_pending" if kind == "permission_prompt" else "ended",
                 "code": None, "detail": None})
    except (FloatiError, OSError, ValueError, TypeError, OverflowError) as exc:
        _absence(harness, getattr(exc, "code", "desktop_observation_unavailable"))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="Observe a native desktop notification; emits no host decision")
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--harness", required=True, choices=("claude", "grok-build", "opencode"))
    args = parser.parse_args(argv)
    # Bound stdin and refuse ambiguous duplicate keys. Never read a transcript.
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate input field")
            result[key] = value
        return result
    try:
        raw = sys.stdin.buffer.read(131073)
        if len(raw) > 131072:
            return 0
        payload = json.loads(raw, object_pairs_hook=pairs)
    except (ValueError, UnicodeError, OSError):
        return 0
    return observe_desktop_notification(args.root, args.harness, payload)

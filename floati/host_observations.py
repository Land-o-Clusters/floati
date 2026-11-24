"""Bounded native host activity, separate from execution and approval state.

Only authenticated native occurrences issue in-memory capabilities. The latest
state is invalidation metadata, never serialized publication authority. Host
content, commands, transcript paths and tool responses are not retained.
"""
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import stat
import sys
import time

from .codex_tool_hook import _source_acceptance_guard, _text
from .codex_wait_contract import (CodexWaitConsentLedger, CodexWaitSessionLedger,
    resolve_participant)
from .errors import FloatiError, ProtocolRefusal
from .ids import uuid7_hex
from .jsonl import read_records_snapshot
from .recipient_mechanisms import _json, _parent, _read, _signature, _time
from .registry import Registry
from .root import validate_identifier
from .seat_execution import SeatExecutionController, _history
from .wake_control import is_session_paused


PERMISSION_PIN = "75c73d7a38cfc0e73ef06bd1fc506a44d25874522069ec4fb85e0bf1e7d6b8fb"
INTERRUPT_PIN = "be37402db3524543efe1e4058e6b9a3a53f7dd8f7791a9b75bb2accf7842196e"
QUIET_SECONDS = 900
_BASE = {"cwd", "hook_event_name", "session_id", "transcript_path"}
_TURN = _BASE | {"model", "turn_id"}
_PERMISSION = _TURN | {"permission_mode"}
_SHAPES = {
    "PermissionRequest": _PERMISSION | {"tool_name", "tool_input"},
    "PreToolUse": _PERMISSION | {"tool_name", "tool_input", "tool_use_id"},
    "PostToolUse": _PERMISSION | {"tool_name", "tool_input", "tool_use_id", "tool_response"},
    "UserPromptSubmit": _PERMISSION | {"prompt"},
    "SessionStart": _BASE | {"model", "permission_mode", "source"},
    "SessionEnd": _BASE | {"reason"},
    "PreCompact": _TURN | {"trigger"},
    "PostCompact": _TURN | {"trigger"},
    "SubagentStart": _PERMISSION | {"agent_id", "agent_type"},
    "SubagentStop": _PERMISSION | {"agent_id", "agent_type", "agent_transcript_path",
        "last_assistant_message", "stop_hook_active"},
    "Stop": _PERMISSION | {"last_assistant_message", "stop_hook_active"},
    "Interrupt": _PERMISSION,
}
HOST_OBSERVER_EVENTS = tuple(event for event in _SHAPES if event != "Stop")
_STATE_FIELDS = {"schema_version", "node_id", "workspace", "session_id",
    "session_claim_id", "consent_receipt_id", "sequence", "storage_generation",
    "event", "turn_id", "observed_at", "tool_name", "work_item_id",
    "work_claim_id", "invocation_id"}
_STATE_LIMIT = 8192


@dataclass
class NativeOccurrence:
    controller: object
    capability: object
    observation: dict
    started_monotonic: float
    owner: object = None
    startup_attempted: bool = False
    startup_cleanup_unproved: bool = False


def _valid_payload(payload):
    if not isinstance(payload, dict):
        return False
    event = payload.get("hook_event_name")
    if not isinstance(event, str):
        return False
    required = _SHAPES.get(event)
    # Optional agent metadata on tool hooks identifies a delegated callback,
    # not this claimed main session. Parent SubagentStart/Stop are distinct
    # supported events whose required child metadata is deliberately ignored.
    if required is None or set(payload) != required:
        return False
    for field, cap in (("cwd", 4096), ("session_id", 128), ("model", 128),
                       ("turn_id", 128), ("tool_name", 128), ("tool_use_id", 128),
                       ("agent_id", 128), ("agent_type", 128)):
        if field in payload and not _text(payload[field], cap):
            return False
    if "permission_mode" in payload and (not isinstance(payload["permission_mode"], str)
        or payload["permission_mode"] not in {
        "default", "acceptEdits", "plan", "dontAsk", "bypassPermissions"}):
        return False
    for field in ("transcript_path", "agent_transcript_path", "last_assistant_message"):
        if field in payload and payload[field] is not None and not isinstance(payload[field], str):
            return False
    if "stop_hook_active" in payload and type(payload["stop_hook_active"]) is not bool:
        return False
    if "prompt" in payload and not isinstance(payload["prompt"], str):
        return False
    if event == "SessionEnd" and payload["reason"] != "other":
        return False
    if event == "SessionStart" and (not isinstance(payload["source"], str)
        or payload["source"] not in {"startup", "resume", "clear", "compact", "fork"}):
        return False
    if event in {"PreCompact", "PostCompact"} and (not isinstance(payload["trigger"], str)
        or payload["trigger"] not in {"manual", "auto"}):
        return False
    return True


def _native_context(bus_home, workspace, session):
    workspace = Path(workspace)
    if not workspace.is_absolute() or workspace.resolve(strict=True) != workspace or not workspace.is_dir():
        raise ValueError("noncanonical native workspace")
    participant = resolve_participant(Path(bus_home), workspace, _strict=True)
    if participant is None:
        raise ValueError("native workspace is not mapped")
    root, node = participant.root, participant.binding.node_id
    if Registry(root).require_active(node)["role"].casefold() != "codex":
        raise ValueError("native session is not Codex")
    consent = CodexWaitConsentLedger(root).require_armed(participant.binding)
    if is_session_paused(root, node, session):
        raise ValueError("native session is paused")
    rows = read_records_snapshot(root, CodexWaitSessionLedger._relative(node),
        allowed_kinds={"codex_wait_session_receipt"})
    matching = [row for row in rows if row["workspace"] == str(participant.binding.workspace)]
    claim = matching[-1] if matching else None
    if claim is None or claim["acting_session_id"] != session or claim["consent_receipt_id"] != consent["id"]:
        raise ValueError("native session claim is not current")
    return participant, consent, claim


def _state_path(root, node):
    return root.tenant_home / "state/host-observations" / validate_identifier(node, "node") / "latest.json"


def _storage_parent(root, node, *, create=False):
    """Retain each directory descriptor; aliases never become coordinates."""
    validate_identifier(node, "node")
    descriptor = _parent(root.tenant_home / "coordinate")
    created = False
    try:
        for part in ("state", "host-observations", node):
            try:
                following = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            except FileNotFoundError:
                if not create:
                    raise
                os.mkdir(part, mode=0o700, dir_fd=descriptor)
                following = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
                if part == node:
                    created = True
            info = os.fstat(following)
            if info.st_uid != os.getuid() or info.st_mode & 0o022:
                os.close(following)
                raise ValueError("observation directory is not privately owned")
            os.close(descriptor)
            descriptor = following
        return descriptor, created
    except BaseException:
        os.close(descriptor)
        raise


def _validate_state(value, node):
    if not isinstance(value, dict) or set(value) != _STATE_FIELDS or (
        type(value["schema_version"]) is not int or value["schema_version"] != 1
        or value["node_id"] != node or not isinstance(value["event"], str) or value["event"] not in _SHAPES
        or type(value["sequence"]) is not int or not 1 <= value["sequence"] < 2 ** 63
        or not isinstance(value["storage_generation"], str)
        or re.fullmatch(r"[0-9a-f]{32}", value["storage_generation"]) is None
    ):
        raise ValueError("invalid observation state")
    for field, cap in (("workspace", 4096), ("session_id", 128),
                       ("session_claim_id", 128), ("consent_receipt_id", 128), ("observed_at", 64)):
        if not _text(value[field], cap):
            raise ValueError("invalid observation field")
    _time(value["observed_at"])
    for field in ("turn_id", "tool_name", "work_item_id", "work_claim_id", "invocation_id"):
        if value[field] is not None and not _text(value[field], 128):
            raise ValueError("invalid optional observation field")
    if value["event"] in {"PermissionRequest", "Interrupt"} and value["turn_id"] is None:
        raise ValueError("native occurrence needs its actual turn")
    # Tool hooks keep the tool name they already carried. Older rows recorded
    # that name only for a permission request; a missing name there is still
    # a row, and it is not a reason to invent one.
    if value["event"] == "PermissionRequest" and value["tool_name"] is None:
        raise ValueError("permission observation requires its tool name")
    if value["tool_name"] is not None and value["event"] not in {
        "PermissionRequest", "PreToolUse", "PostToolUse",
    }:
        raise ValueError("tool name belongs only to a tool observation")
    if any(value[field] is None for field in ("work_item_id", "work_claim_id", "invocation_id")) and not all(
        value[field] is None for field in ("work_item_id", "work_claim_id", "invocation_id")):
        raise ValueError("partial car occurrence")
    return value


def _read_state(root, node):
    try:
        descriptor, _created = _storage_parent(root, node)
    except FileNotFoundError:
        return None
    path = _state_path(root, node)
    try:
        try:
            info = os.stat(path.name, dir_fd=descriptor, follow_symlinks=False)
        except FileNotFoundError as exc:
            # An existing per-node directory witnesses prior initialization.
            # Deleting state must not restart an old session's local sequence.
            raise ValueError("initialized observation state is missing") from exc
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError("observation state is not a private ordinary file")
        data, signature, parent_identity = _read(path, _STATE_LIMIT, with_identity=True)
        opened = os.fstat(descriptor)
        if parent_identity != (opened.st_dev, opened.st_ino) or signature != _signature(info):
            raise ValueError("observation coordinate changed")
        return _validate_state(_json(data), node)
    finally:
        os.close(descriptor)


def _write_state(root, node, value, previous):
    _validate_state(value, node)
    data = json.dumps(value, sort_keys=True, separators=(",", ":")).encode() + b"\n"
    if len(data) > _STATE_LIMIT:
        raise ValueError("observation exceeds bound")
    parent, created = _storage_parent(root, node, create=True)
    path = _state_path(root, node)
    temporary = ".latest-" + uuid7_hex()
    descriptor = None
    try:
        if created != (previous is None):
            raise ValueError("observation initialization changed")
        if not created and _read_state(root, node) != previous:
            raise ValueError("observation state changed before replacement")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600, dir_fd=parent)
        remaining = memoryview(data)
        while remaining:
            written = os.write(descriptor, remaining)
            if written <= 0:
                raise OSError("short observation write")
            remaining = remaining[written:]
        os.fsync(descriptor)
        os.close(descriptor); descriptor = None
        reopened = _parent(path)
        try:
            before, after = os.fstat(parent), os.fstat(reopened)
            if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
                raise ValueError("observation parent changed before publication")
            os.replace(temporary, path.name, src_dir_fd=parent, dst_dir_fd=parent)
            os.fsync(parent)
        finally:
            os.close(reopened)
        if _read_state(root, node) != value:
            raise ValueError("observation readback differs")
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            os.unlink(temporary, dir_fd=parent)
        except FileNotFoundError:
            pass
        os.close(parent)


def _same_parent(descriptor, path):
    reopened = _parent(path)
    try:
        before, after = os.fstat(descriptor), os.fstat(reopened)
        if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
            raise ValueError("observation parent changed")
    finally:
        os.close(reopened)


def _poison_previous(root, node, previous):
    """Remove only the exact old state while the source guard is retained."""
    if previous is None:
        return
    descriptor, _created = _storage_parent(root, node)
    path = _state_path(root, node)
    try:
        if _read_state(root, node) != previous:
            return  # A committed replacement must survive a later failure.
        before = os.stat(path.name, dir_fd=descriptor, follow_symlinks=False)
        _same_parent(descriptor, path)
        if _read_state(root, node) != previous or _signature(before) != _signature(
            os.stat(path.name, dir_fd=descriptor, follow_symlinks=False)):
            return
        os.unlink(path.name, dir_fd=descriptor)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _probe_storage(root, observation):
    """Measure local writability; lost events cannot be reconstructed later."""
    node = observation["node_id"]
    parent, _created = _storage_parent(root, node)
    path = _state_path(root, node)
    name = ".floati-host-probe-" + uuid7_hex()
    descriptor = None
    created = False
    try:
        _same_parent(parent, path)
        if _read_state(root, node) != observation:
            raise ValueError("observation changed before storage probe")
        descriptor = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600, dir_fd=parent)
        created = True
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError("storage probe is not a private ordinary file")
        if os.write(descriptor, b"1") != 1:
            raise OSError("short storage probe write")
        os.fsync(descriptor)
        os.close(descriptor); descriptor = None
        _same_parent(parent, path)
        os.unlink(name, dir_fd=parent); created = False
        os.fsync(parent)
        _same_parent(parent, path)
        if _read_state(root, node) != observation:
            raise ValueError("observation changed during storage probe")
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if created:
            try:
                os.unlink(name, dir_fd=parent)
            except OSError:
                pass
        os.close(parent)


def _matches_authority(root, observation, controller):
    participant, consent, claim = _native_context(root.path,
        observation["workspace"], observation["session_id"])
    if participant.binding.node_id != observation["node_id"] or (
        consent["id"] != observation["consent_receipt_id"]
        or claim["id"] != observation["session_claim_id"]):
        return False
    item = controller._car(observation["node_id"])
    return item["id"] == observation["work_item_id"] and item["claim_id"] == observation["work_claim_id"]


def _still_current(controller, observation):
    return _read_state(controller.root, observation["node_id"]) == observation and (
        _matches_authority(controller.root, observation, controller))


def _host_binding(observation):
    return {"contract_pin": PERMISSION_PIN if observation["event"] == "PermissionRequest" else INTERRUPT_PIN,
        "hook_event_name": observation["event"], "turn_id": observation["turn_id"],
        "observed_sequence": observation["sequence"], "observed_at": observation["observed_at"],
        "tool_name": observation["tool_name"], "consent_receipt_id": observation["consent_receipt_id"],
        "session_claim_id": observation["session_claim_id"]}


def observe_codex_host(bus_home, payload, *, now=None, launch=True):
    """Record supported native activity; produce no permission decision."""
    if not _valid_payload(payload):
        return None
    started = time.monotonic()
    at = datetime.now(timezone.utc) if now is None else now
    if not isinstance(at, datetime) or at.tzinfo is None or at.utcoffset() is None:
        return None
    stamp = at.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    try:
        participant, _consent, _claim = _native_context(bus_home, payload["cwd"], payload["session_id"])
        root, node = participant.root, participant.binding.node_id
        with _source_acceptance_guard(root, node):
            current, consent, claim = _native_context(bus_home, payload["cwd"], payload["session_id"])
            if current.binding != participant.binding:
                return None
            previous = _read_state(root, node)
            sequence = previous["sequence"] + 1 if previous is not None else 1
            generation = previous["storage_generation"] if previous is not None else uuid7_hex()
            event = payload["hook_event_name"]
            controller = SeatExecutionController(root)
            item = None
            if event in {"PermissionRequest", "Interrupt"}:
                try:
                    item = controller._car(node)
                except ProtocolRefusal as exc:
                    if exc.code not in {"seat_execution_car_absent", "seat_execution_car_ambiguous"}:
                        raise
            invocation = None if item is None else "codex-host-" + sha256(json.dumps(
                [payload["session_id"], payload["turn_id"], sequence, generation],
                separators=(",", ":")).encode()).hexdigest()
            observation = {"schema_version": 1, "node_id": node,
                "workspace": str(current.binding.workspace), "session_id": payload["session_id"],
                "session_claim_id": claim["id"], "consent_receipt_id": consent["id"],
                "sequence": sequence, "storage_generation": generation, "event": event,
                "turn_id": payload.get("turn_id"), "observed_at": stamp,
                "tool_name": payload["tool_name"] if event in {
                    "PermissionRequest", "PreToolUse", "PostToolUse",
                } else None,
                "work_item_id": None if item is None else item["id"],
                "work_claim_id": None if item is None else item["claim_id"], "invocation_id": invocation}
            try:
                _write_state(root, node, observation, previous)
            except (OSError, ValueError, TypeError):
                try:
                    _poison_previous(root, node, previous)
                except (OSError, ValueError, TypeError):
                    pass  # The separate guarded probe bounds testimony on failure.
                raise
            if item is None:
                return None
            binding = _host_binding(observation)
            capability = controller._issue(node=node, session=payload["session_id"],
                workspace=current.binding.workspace, invocation=invocation, source="codex_host",
                receipt_id=claim["id"], source_pin=sha256(Path(__file__).read_bytes()).hexdigest(),
                command=None, source_binding=binding, item_id=item["id"], running=False, already_guarded=True)
            occurrence = NativeOccurrence(controller, capability, observation, started)
            if event == "Interrupt":
                controller._callback_already_guarded(capability,
                    {"event": "interrupted", "code": None, "detail": None})
                return occurrence
        if launch:
            from .host_deadline import start_deadline
            owner = start_deadline(occurrence)
            if owner is not None:
                owner.detach()
            elif occurrence.startup_cleanup_unproved:
                print(json.dumps({"status": "degraded", "source": "codex_host",
                    "code": "host_deadline_startup_cleanup_unproved"}, sort_keys=True), file=sys.stderr)
        return occurrence
    except (FloatiError, OSError, ValueError, TypeError, OverflowError) as exc:
        # Native failures do not become execution, approval or delivery facts.
        if isinstance(exc, OSError):
            print(json.dumps({"status": "degraded", "source": "codex_host",
                "code": "host_observation_unavailable"}, sort_keys=True), file=sys.stderr)
        return None


def _publish_due(controller, capability, observation, *, now):
    try:
        with _source_acceptance_guard(controller.root, observation["node_id"]):
            if observation["event"] != "PermissionRequest" or not _still_current(controller, observation):
                return None
            if (now.astimezone(timezone.utc) - _time(observation["observed_at"])).total_seconds() < QUIET_SECONDS:
                return None
            _probe_storage(controller.root, observation)
            if not _still_current(controller, observation):
                return None
            return controller._callback_already_guarded(capability,
                {"event": "permission_quiet", "code": None, "detail": None})
    except (FloatiError, OSError, ValueError, TypeError):
        return None


def host_approval_projection(root, node):
    unknown = {"state": "UNKNOWN", "reason": "host_observation_absent"}
    try:
        role = Registry(root).require_active(node)["role"].casefold()
        if role == "cursor":
            return {"state": "UNKNOWN", "reason": "cursor_approval_callback_absent",
                "observed_cursor_version": "3.24.12",
                "reopen_trigger": "a Cursor version shipping the dedicated approval-wait hook",
                "forum_url": "https://forum.cursor.com/t/fire-a-hook-when-agent-waits-for-command-tool-approval/166947/5",
                "staff_reply_date": "2026-07-31"}
        if role != "codex":
            return unknown
        # Ordinary projection without host testimony remains physically read-only.
        # Native observation already creates its guards; absence cannot mint them.
        if _read_state(root, node) is None:
            return unknown
        with _source_acceptance_guard(root, node):
            return _codex_approval_projection(root, node, unknown)
    except (FloatiError, OSError, ValueError, TypeError, KeyError):
        return {"state": "UNKNOWN", "reason": "host_observation_unavailable"}


def _codex_approval_projection(root, node, unknown):
    try:
        observation = _read_state(root, node)
        if observation is None:
            return unknown
        controller = SeatExecutionController(root)
        if observation["event"] not in {"PermissionRequest", "Interrupt"}:
            return {"state": "UNKNOWN", "reason": "later_host_event_observed"}
        if observation["work_item_id"] is None or not _matches_authority(root, observation, controller):
            return {"state": "UNKNOWN", "reason": "host_observation_authority_changed"}
        rows = [row for row in _history(root) if row.get("kind") == "seat_execution"
            and row["node_id"] == node and row["invocation_id"] == observation["invocation_id"]
            and row["work_item_id"] == observation["work_item_id"]
            and row["work_claim_id"] == observation["work_claim_id"]
            and row["acting_session_id"] == observation["session_id"]
            and row["workspace"] == observation["workspace"]
            and row["source_receipt_id"] == observation["session_claim_id"]
            and row["source_kind"] == "codex_host"]
        expected_event = "permission_quiet" if observation["event"] == "PermissionRequest" else "interrupted"
        fact = next((row for row in reversed(rows) if row["event"] == expected_event
            and row["source_binding"] == _host_binding(observation)), None)
        if _read_state(root, node) != observation or not _matches_authority(root, observation, controller):
            return {"state": "UNKNOWN", "reason": "host_observation_changed_during_read"}
        if fact is None:
            return {"state": "UNKNOWN", "reason": "native_outcome_not_published"}
        try:
            _probe_storage(root, observation)
        except OSError as exc:
            return {"state": "UNKNOWN", "reason": "host_observation_storage_not_writable",
                "errno": exc.errno}
        if not _still_current(controller, observation):
            return {"state": "UNKNOWN", "reason": "host_observation_changed_during_read"}
        return {"state": "FACT", "reason": fact["event"],
            "observed_at": fact["source_binding"]["observed_at"], "tool_name": fact["source_binding"]["tool_name"],
            "sequence": fact["source_binding"]["observed_sequence"], "turn_id": fact["source_binding"]["turn_id"],
            "session_id": fact["acting_session_id"], "execution_id": fact["id"],
            "envelope_id": fact["envelope_id"]}
    except (FloatiError, OSError, ValueError, TypeError, KeyError):
        return {"state": "UNKNOWN", "reason": "host_observation_unavailable"}

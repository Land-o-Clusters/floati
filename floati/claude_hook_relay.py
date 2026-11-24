"""Synchronous Claude context relay, with a cheap gate and native receipts.

This never contacts Claude's socket. The watermark is only a polling hint;
native inbox, consent, claims and presentation history remain authoritative.
Idle doorbells enter through UserPromptSubmit. Deferred mail remains on Stop.
"""
from contextlib import contextmanager
import fcntl
import hashlib
import json
import math
import os
import stat
import sys
import time

def _refuse(code, detail):
    from .errors import ProtocolRefusal
    raise ProtocolRefusal("claude_relay_" + code, detail,
        remedy="preserve the explicit seat coordinates and use the native Stop/inbox path")


def _interval():
    value = os.environ.get("FLOATI_CLAUDE_RELAY_INTERVAL_SECONDS", "30")
    if not value.isascii() or not value.isdecimal() or not 5 <= int(value) <= 3600:
        _refuse("interval_invalid", "relay interval must be an integer from 5 through 3600 seconds")
    return int(value)


def _ordinary(info):
    return stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_uid == os.getuid()


@contextmanager
def _gate_directory(bus_home, node, session):
    directory = os.path.join(bus_home, "state", "claude-wait", node)
    if os.path.realpath(directory) != directory:
        _refuse("state_invalid", "relay state must not traverse symlinks")
    os.makedirs(directory, mode=0o700, exist_ok=True)
    stem = "relay-" + hashlib.sha256(session.encode("utf-8")).hexdigest()
    lock = os.path.join(directory, stem + ".lock")
    try:
        descriptor = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    except OSError:
        _refuse("state_invalid", "relay lock could not be opened safely")
    try:
        if not _ordinary(os.fstat(descriptor)):
            _refuse("state_invalid", "relay lock is not an owned ordinary file")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            # A concurrent hook already owns this session's scan and flush.
            yield None
            return
        yield os.path.join(directory, stem + ".json")
    finally:
        os.close(descriptor)


def _load_watermark(path):
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    except OSError:
        _refuse("state_invalid", "relay watermark could not be opened safely")
    try:
        info = os.fstat(descriptor)
        if not _ordinary(info) or info.st_mode & 0o077 or info.st_size > 4096:
            _refuse("state_invalid", "relay watermark must be a private owned ordinary file")
        with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
            descriptor = None
            value = json.load(stream)
        if not isinstance(value, dict) or set(value) != {"fingerprint", "last_full_read", "workspace"} or (
            not isinstance(value["fingerprint"], list) or len(value["fingerprint"]) != 4
            or not all(type(item) is int and item >= 0 for item in value["fingerprint"])
            or type(value["last_full_read"]) not in (int, float)
            or not math.isfinite(value["last_full_read"])
            or not isinstance(value["workspace"], str)
        ):
            raise ValueError("invalid watermark")
        return value
    except (ValueError, UnicodeError):
        _refuse("state_invalid", "relay watermark is malformed")
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _save_watermark(path, value):
    import tempfile
    descriptor, temporary = tempfile.mkstemp(prefix=".relay-", dir=os.path.dirname(path))
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, sort_keys=True, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _body(event, messages, session, *, presence=""):
    from .colleague_frame import frame_clause
    context = ((presence + "\n" if presence else "") + frame_clause(messages) + "\n[floati] Exact native envelopes:\n"
        + json.dumps(messages, sort_keys=True)
        + "\nAct on these exact envelopes, then explicitly acknowledge their IDs "
        + "with acting session " + session + ". Context delivery is not acknowledgment.")
    return {"hookSpecificOutput": {"hookEventName": event, "additionalContext": context}}


def _read_and_present(bus_home, node, runtime, payload, stdout):
    # Heavy imports and all native ledger reads occur only beyond the gate.
    from pathlib import Path
    from .bridge_publication import BridgePublication
    from .bus_epoch import epoch_guard, _require_epoch_writer_admission
    from .car_fence import car_acceptance_guard
    from .codex_wait_contract import CodexWaitConsentLedger, CodexWaitSessionLedger, resolve_participant
    from .cursor_presentations import CursorPresentationLedger
    from .events import EventLog
    from .errors import DurabilityFailure, IntegrityFailure, ProtocolRefusal
    from .jsonl import _locked_path
    from .registry import Registry
    from .wake_control import is_session_paused
    from .wake_hold import wake_coordination_guard

    bus_home = Path(bus_home)
    workspace, session = Path(payload["cwd"]), payload["session_id"]
    participant = resolve_participant(bus_home, workspace, _strict=True)
    if participant is None or participant.binding.node_id != node:
        _refuse("workspace_mismatch", "hook cwd does not resolve to the explicit node")
    root = participant.root
    with epoch_guard(root), car_acceptance_guard(root), wake_coordination_guard(root, node):
        _require_epoch_writer_admission(root)
        pause_relative = Path("state/wake-control") / (node + ".lock")
        with _locked_path(root.resolve_relative(pause_relative), exclusive=True,
                relative=pause_relative, timeout_seconds=5, order_tracked=False):
            current = resolve_participant(bus_home, workspace, _strict=True)
            if current is None or current.binding != participant.binding:
                _refuse("workspace_mismatch", "hook mapping changed before presentation")
            if Registry(root).require_active(node)["role"].casefold() != "claude":
                _refuse("harness_mismatch", "explicit relay node must be registered under Claude")
            consent = CodexWaitConsentLedger(root).require_armed(current.binding)
            if is_session_paused(root, node, session):
                return
            classification = CodexWaitSessionLedger(root)._participate_already_guarded(
                current.binding, consent, session, classified=True)
            if classification.state != "authorized":
                return
            messages = EventLog(root)._read_inbox_already_guarded(node)
            if not messages:
                return
            plane = CursorPresentationLedger(root)
            # Claude's existing Stop path uses this same historical harness
            # value. Sharing it keeps both paths in one conversation history.
            selection = plane.select(current.binding, consent, classification, harness="codex")
            candidates = {message["id"]: message for message in messages}
            from .fleet_presence import presence_line
            line = presence_line(root, node)
            chosen = []
            for message in selection["fresh_messages"]:
                if candidates.get(message["id"]) != message:
                    break
                body = _body(payload["hook_event_name"], chosen + [message], session, presence=line)
                if len(body["hookSpecificOutput"]["additionalContext"]) > 10000:
                    break
                chosen.append(message)
            if not chosen:
                return
            selection = dict(selection, fresh_messages=chosen)
            body = _body(payload["hook_event_name"], chosen, session, presence=line)
            prepared = plane.prepare(selection, body)
            publication = BridgePublication(bus_home, node, workspace, session)
            with publication.capture():
                publication.defer(EventLog(root), node, chosen, None)
            try:
                delivery = publication._publish_context_already_guarded(chosen, body, stdout)
                plane.flushed(prepared, delivery)
            except (DurabilityFailure, IntegrityFailure, ProtocolRefusal) as exc:
                if not publication.output_started:
                    raise
                # The context is already on stdout: a second JSON object
                # would invalidate the entire hook response. Keep the native
                # prepared/uncertain reservation and report only to stderr.
                print(json.dumps({"status": "degraded", "source": "claude_relay",
                                  "code": exc.code}), file=sys.stderr)


def run_relay(*, bus_home, node, runtime, hook_payload, stdout, clock=time.monotonic):
    """One bounded hook invocation; silence means the Stop fallback still owns it."""
    if not isinstance(hook_payload, dict):
        _refuse("payload_invalid", "hook input must be a JSON object")
    if hook_payload.get("hook_event_name") not in {"SessionStart", "PostToolUse", "UserPromptSubmit"} or (
        hook_payload.get("agent_id") or hook_payload.get("agent_transcript_path")
    ):
        return 0
    from .session_identity import validate_identifier, validate_session_id
    node = validate_identifier(node, "node")
    session = validate_session_id(hook_payload.get("session_id"))
    workspace = hook_payload.get("cwd")
    if not isinstance(workspace, str) or not os.path.isabs(workspace):
        _refuse("payload_invalid", "hook input must name an absolute cwd")
    bus_home = os.fspath(bus_home)
    if not os.path.isabs(bus_home) or os.path.realpath(bus_home) != bus_home or not os.path.isdir(bus_home):
        _refuse("root_invalid", "relay requires an explicit canonical existing root")
    if hook_payload["hook_event_name"] == "SessionStart":
        from .claude_inbox import capture_session
        capture_session(bus_home, node, workspace, session, hook_payload)
    interval = _interval()
    with _gate_directory(bus_home, node, session) as path:
        if path is None:
            return 0
        # Exactly one stat of the event axis; never open it on a gated call.
        try:
            info = os.stat(os.path.join(bus_home, "events.jsonl"), follow_symlinks=False)
            if not _ordinary(info):
                _refuse("root_invalid", "event axis must be an owned ordinary file")
            fingerprint = [info.st_size, info.st_mtime_ns, info.st_dev, info.st_ino]
        except FileNotFoundError:
            fingerprint = [0, 0, 0, 0]
        mark = _load_watermark(path)
        now = clock()
        if type(now) not in (int, float) or not math.isfinite(now):
            _refuse("clock_invalid", "relay clock must be finite")
        if mark is not None and (
            mark["fingerprint"] == fingerprint or (
                hook_payload["hook_event_name"] != "UserPromptSubmit"
                and 0 <= now - mark["last_full_read"] < interval)
        ):
            return 0
        # Save the read start, not the end or a later skipped invocation. A
        # reader/output failure cannot turn every subsequent tool into a scan.
        _save_watermark(path, {"fingerprint": fingerprint, "last_full_read": now,
                               "workspace": workspace})
        _read_and_present(bus_home, node, runtime, hook_payload, stdout)
    return 0


def run_claude_relay(args):
    try:
        payload = json.loads(sys.stdin.read())
    except (ValueError, OSError):
        _refuse("payload_invalid", "hook input must be JSON")
    class HookOutput:
        started = False
        def write(self, value):
            self.started = True
            return sys.stdout.write(value)
        def flush(self):
            return sys.stdout.flush()
    output = HookOutput()
    try:
        return run_relay(bus_home=args.root, node=args.actor, runtime=args.runtime,
                         hook_payload=payload, stdout=output)
    except Exception as exc:
        # Native context-manager exit checks also run after flush. Cover the
        # entire guarded invocation so those failures cannot add a second
        # JSON artifact to an already-started hook response.
        from .errors import DurabilityFailure, IntegrityFailure, ProtocolRefusal
        if output.started and isinstance(exc, (DurabilityFailure, IntegrityFailure, ProtocolRefusal)):
            print(json.dumps({"status": "degraded", "source": "claude_relay",
                              "code": exc.code}), file=sys.stderr)
            return 0
        if output.started and isinstance(exc, OSError):
            print(json.dumps({"status": "degraded", "source": "claude_relay",
                              "code": "claude_relay_io_failed"}), file=sys.stderr)
            return 35
        raise


_ARGUMENTS = (("--harness", "harness", ("claude",)), ("--root", "root", None),
              ("--as", "actor", None), ("--runtime", "runtime", None))


def add_relay_arguments(parser):
    """One argument contract for the lightweight route and the CLI registry."""
    for option, destination, choices in _ARGUMENTS:
        keywords = {"dest": destination, "required": True}
        if choices is not None:
            keywords["choices"] = choices
        if destination == "actor":
            keywords["metavar"] = "NODE"
        parser.add_argument(option, **keywords)


def _parse_arguments(argv):
    from types import SimpleNamespace
    arguments = list(sys.argv[1:] if argv is None else argv)
    # Installed commands always carry four separate option/value pairs. Use
    # the same descriptors as the registry and defer argparse's imports for
    # alternate spellings, usage errors and diagnostic formatting.
    if len(arguments) == 2 * len(_ARGUMENTS):
        values = dict(zip(arguments[::2], arguments[1::2]))
        if set(values) == {row[0] for row in _ARGUMENTS} and all(
            values[option] and not values[option].startswith("-")
            and (choices is None or values[option] in choices)
            for option, _, choices in _ARGUMENTS
        ):
            return SimpleNamespace(**{destination: values[option]
                                      for option, destination, _ in _ARGUMENTS})
    import argparse
    class RelayParser(argparse.ArgumentParser):
        def error(self, message):
            from .errors import ProtocolRefusal
            raise ProtocolRefusal("arguments_invalid", message)
    parser = RelayParser(prog="floati hook relay", add_help=False, allow_abbrev=False)
    add_relay_arguments(parser)
    return parser.parse_args(arguments)


def main(argv=None):
    try:
        return run_claude_relay(_parse_arguments(argv))
    except OSError:
        # A stream failure may occur after a partial context write. Never
        # append a second JSON artifact to that stdout stream.
        print(json.dumps({"status": "degraded", "source": "claude_relay",
                          "code": "claude_relay_io_failed"}), file=sys.stderr)
        return 35
    except Exception as exc:
        from .errors import DurabilityFailure, IntegrityFailure, ProtocolRefusal
        if isinstance(exc, ProtocolRefusal):
            status, code = "refused", 20
        elif isinstance(exc, IntegrityFailure):
            status, code = "malformed_evidence", 33
        elif isinstance(exc, DurabilityFailure):
            status, code = "degraded", 35
        else:
            raise
        evidence = {"code": exc.code, "detail": exc.detail}
        if isinstance(exc, ProtocolRefusal):
            evidence["remedy"] = exc.remedy
        print(json.dumps({"artifact_version": 0, "command": "hook", "status": status,
                          "evidence": evidence}))
        return code

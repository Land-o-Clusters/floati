"""A mail-free Claude doorbell; native flushed presentation is the only proof.

The undocumented 2.1.309 Unix protocol accepts newline JSON without auth or
``from``. EOF has no delivery meaning. No listener or credential is created.
"""
from contextlib import contextmanager
import hashlib
import json
import math
import os
from pathlib import Path
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time


VERIFIED_VERSIONS = frozenset({"2.1.309"})
# Existing relay report: full-read wall maximum 453.589 ms, N=5.
# Two seconds allows over four times that observed maximum per attempt.
CONFIRM_SECONDS = 2.0
POLL_SECONDS = 0.05


def _refuse(detail):
    from .errors import ProtocolRefusal
    raise ProtocolRefusal("claude_inbox_state_invalid", detail,
        remedy="preserve the seat state and use the native Stop/inbox path")


def _directory(bus_home, node, *, create=False):
    from .session_identity import validate_identifier
    node = validate_identifier(node, "node")
    home = os.fspath(bus_home)
    if not os.path.isabs(home) or os.path.realpath(home) != home or not os.path.isdir(home):
        _refuse("doorbell state requires a canonical existing root")
    legacy = Path(home) / "state/claude-wait" / node
    directory = legacy / "inbox"
    if os.path.realpath(directory) != str(directory):
        _refuse("doorbell state must not traverse symlinks")
    # The hook installer owns the legacy parent and may create it as 0755.
    # Keep its permissions intact; only this dedicated inbox bank is private.
    for ancestor in (Path(home), Path(home) / "state", legacy.parent, legacy, directory):
        try:
            info = ancestor.lstat()
        except FileNotFoundError:
            if not create:
                return None
            ancestor.mkdir(mode=0o700)
            info = ancestor.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
            _refuse("doorbell ancestry must contain only owned ordinary directories")
        if ancestor == directory and info.st_mode & 0o077:
            _refuse("doorbell state must be a private owned directory")
    return directory


def _ordinary(info):
    return (stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()
        and info.st_nlink == 1 and stat.S_IMODE(info.st_mode) == 0o600)


def _read(path):
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    except OSError:
        _refuse("doorbell state could not be opened safely")
    try:
        info = os.fstat(descriptor)
        if not _ordinary(info) or info.st_size > 16384:
            _refuse("doorbell state must be a bounded private ordinary file")
        with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
            descriptor = None
            value = json.load(stream)
        if not isinstance(value, dict):
            _refuse("doorbell state must be an object")
        return value
    except (ValueError, UnicodeError):
        _refuse("doorbell state is malformed")
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _save(path, value):
    _read(path)  # Refuse unsafe existing state, rather than overwrite it.
    descriptor, temporary = tempfile.mkstemp(prefix=".inbox-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, sort_keys=True, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        parent = os.open(path.parent, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            os.fsync(parent)
        finally:
            os.close(parent)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _session_path(directory, session):
    from .session_identity import validate_session_id
    session = validate_session_id(session)
    return directory / ("inbox-" + hashlib.sha256(session.encode()).hexdigest() + ".json")


def _load(directory, session=None):
    if directory is None:
        return None
    if session is None:
        latest = _read(directory / "inbox-latest.json")
        if latest is None:
            return None
        if set(latest) != {"session"}:
            _refuse("doorbell latest-session pointer is malformed")
        session = latest["session"]
    value = _read(_session_path(directory, session))
    if value is None:
        return None
    fields = {"session", "workspace", "socket", "version", "version_source", "settings_files", "fallback"}
    if set(value) != fields or value["session"] != session or (
            not isinstance(value["workspace"], str) or not os.path.isabs(value["workspace"])
            or not isinstance(value["socket"], str) or not isinstance(value["version"], str)
            or value["version_source"] != "installed_client"
            or not isinstance(value["settings_files"], list) or len(value["settings_files"]) != 4
            or not all(isinstance(path, str) and os.path.isabs(path) for path in value["settings_files"])
            or value["fallback"] not in (None, "claude_inbox_unavailable", "claude_inbox_unconfirmed",
                "claude_inbox_unverified_version", "claude_inbox_held", "claude_inbox_refused")):
        _refuse("doorbell session state has invalid coordinates")
    return value


def _installed_version():
    """Read only an explicitly declared canonical client, with no user state."""
    executable = os.environ.get("FLOATI_CLAUDE_EXECUTABLE", "")
    if (not os.path.isabs(executable) or os.path.realpath(executable) != executable
            or not os.path.isfile(executable) or not os.access(executable, os.X_OK)):
        return ""
    try:
        with tempfile.TemporaryDirectory(prefix="floati-claude-version-") as home:
            result = subprocess.run([executable, "--version"], stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=2,
                env={"HOME": home, "PATH": "/usr/bin:/bin", "NO_COLOR": "1"},
                text=True, encoding="utf-8", errors="replace")
        if result.returncode == 0:
            text = result.stdout.strip()
            suffix = " (Claude Code)"
            if text.endswith(suffix) and len(text) < 80:
                return text[:-len(suffix)]
    except (OSError, subprocess.SubprocessError):
        pass
    return ""


def capture_session(bus_home, node, workspace, session, payload):
    """Capture this explicit SessionStart coordinate, never a messaging token."""
    if not isinstance(payload, dict) or payload.get("hook_event_name") != "SessionStart":
        return
    if payload.get("agent_id") or payload.get("agent_transcript_path"):
        return
    sock = os.environ.get("CLAUDE_CODE_MESSAGING_SOCKET", "")
    workspace = os.fspath(workspace)
    if not os.path.isabs(workspace) or os.path.realpath(workspace) != workspace:
        _refuse("doorbell workspace must be canonical and absolute")
    # Coordinates are hints, never an alternative to native mapping/consent.
    # SessionStart's relay may claim the session later; capture never does.
    from .codex_wait_contract import CodexWaitConsentLedger, resolve_participant
    from .registry import Registry
    participant = resolve_participant(Path(bus_home), Path(workspace), _strict=True)
    if participant is None or participant.binding.node_id != node:
        _refuse("doorbell workspace does not resolve to the explicit node")
    if Registry(participant.root).require_active(node)["role"].casefold() != "claude":
        _refuse("doorbell node must be registered under Claude")
    CodexWaitConsentLedger(participant.root).require_armed(participant.binding)
    workspace = str(participant.binding.workspace)
    directory = _directory(bus_home, node, create=True)
    path = _session_path(directory, session)
    if not os.path.isabs(sock):
        sock = ""
    user = Path(os.path.expanduser("~")) / ".claude/settings.json"
    managed = (Path("/Library/Application Support/ClaudeCode/managed-settings.json")
        if sys.platform == "darwin" else Path("/etc/claude-code/managed-settings.json"))
    value = {"session": session, "workspace": workspace, "socket": sock,
        "version": _installed_version() if sock else "", "version_source": "installed_client",
        "settings_files": [str(managed), str(user), str(Path(workspace) / ".claude/settings.json"),
            str(Path(workspace) / ".claude/settings.local.json")], "fallback": None}
    _save(path, value)
    _save(directory / "inbox-latest.json", {"session": session})


def _policy(state):
    values = []
    for path in state["settings_files"]:
        try:
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
                info = os.fstat(stream.fileno())
                value = json.load(stream) if stat.S_ISREG(info.st_mode) and info.st_size <= 1048576 else None
            policy = value.get("crossSessionInbound") if isinstance(value, dict) else None
        except (OSError, ValueError, UnicodeError):
            policy = None
        values.append(policy if policy in ("accept", "hold", "refuse") else None)
    # Managed takes precedence over user; project and local may only tighten.
    selected = values[0] or values[1]
    rank = {None: 0, "accept": 0, "hold": 1, "refuse": 2}
    for policy in values[2:]:
        if rank[policy] > rank[selected]:
            selected = policy
    return selected


def _availability(state):
    if sys.platform not in {"darwin", "linux"}:
        return "claude_inbox_unavailable"
    if not state["socket"]:
        return "claude_inbox_unavailable"
    if state["version"] not in VERIFIED_VERSIONS:
        return "claude_inbox_unverified_version"
    policy = _policy(state)
    if policy in {"hold", "refuse"}:
        return "claude_inbox_held" if policy == "hold" else "claude_inbox_refused"
    try:
        info = os.lstat(state["socket"])
        if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
            return "claude_inbox_unavailable"
    except OSError:
        return "claude_inbox_unavailable"
    return None


@contextmanager
def _native_authority(root, node, state):
    from .bus_epoch import epoch_guard
    from .codex_wait_contract import CodexWaitConsentLedger, CodexWaitSessionLedger, resolve_participant
    from .jsonl import _locked_path, read_records_snapshot
    from .registry import Registry
    from .wake_control import is_session_paused
    from .wake_hold import wake_coordination_guard
    with epoch_guard(root, exclusive=False), wake_coordination_guard(root, node):
        relative = Path("state/wake-control") / (node + ".lock")
        with _locked_path(root.resolve_relative(relative), exclusive=True,
                relative=relative, timeout_seconds=5, order_tracked=False):
            participant = resolve_participant(root.tenant_home, Path(state["workspace"]), _strict=True)
            valid = participant is not None and participant.root.path == root.path and participant.binding.node_id == node
            if valid:
                binding = participant.binding
                consent = CodexWaitConsentLedger(root).require_armed(binding)
                ledger = CodexWaitSessionLedger(root)
                claims = ledger._matching(read_records_snapshot(root, ledger._relative(node), allowed_kinds=ledger._KINDS), binding)
                valid = bool(claims) and claims[-1]["acting_session_id"] == state["session"] and (
                    claims[-1]["consent_receipt_id"] == consent["id"]
                    and claims[-1]["workspace_map_digest"] == binding.map_digest
                    and Registry(root).require_active(node)["role"].casefold() == "claude"
                    and not is_session_paused(root, node, state["session"]))
            yield valid


def stop_path_required(bus_home, node, session):
    from .errors import ProtocolRefusal
    from .root import FloatiRoot
    try:
        state = _load(_directory(bus_home, node), session)
        if state is None or state["fallback"] or _availability(state):
            return True
        root = FloatiRoot.open_direct_home(Path(bus_home), create=False)
        with _native_authority(root, node, state) as valid:
            return not valid
    except (OSError, ProtocolRefusal):
        return True


def _confirmed(root, node, state, message):
    from .cursor_presentations import CursorPresentationLedger
    return next((row for row in CursorPresentationLedger(root).read(node)
        if row["phase"] == "flushed" and row["acting_session_id"] == state["session"]
        and row["workspace"] == state["workspace"] and message["id"] in row["item_ids"]), None)


def _observation_path(directory, session, item):
    name = hashlib.sha256((session + "\0" + item).encode()).hexdigest()
    return directory / ("inbox-observation-" + name + ".json")


def delivery_fallbacks(root, node, session, messages):
    """Decorate real native delivery with exact prior fallback testimony only."""
    from .errors import ProtocolRefusal
    if session is None:
        return None
    try:
        directory = _directory(root.tenant_home, node)
        state = _load(directory, session)
        if state is None:
            return None
        # Stop can independently detect missing/policy-blocked coordinates
        # before a sender publishes its per-item observation. That same-session
        # evidence is enough to name the fallback, never to prove delivery.
        current_code = state["fallback"] or _availability(state)
        fallbacks = []
        for message in messages:
            row = _read(_observation_path(directory, session, message["id"]))
            code = current_code if row is None else None
            if row is not None and row.get("status") == "fallback" and (
                    row.get("tenant_id"), row.get("node"), row.get("session"), row.get("item_id")
                    ) == (root.tenant_id, node, session, message["id"]) and (
                    row.get("policy_source") == "readable_settings_files" and row.get("code") in (
                        "claude_inbox_unavailable", "claude_inbox_unconfirmed",
                        "claude_inbox_held", "claude_inbox_refused", "claude_inbox_unverified_version")):
                code = row["code"]
            if code:
                fallbacks.append({"item_id": message["id"], "code": code})
        if fallbacks:
            return {"acting_session_id": session, "policy_source": "readable_settings_files",
                "fallbacks": fallbacks}
    except (OSError, ProtocolRefusal):
        # Optional corrupt state must never prevent the native presentation.
        return None
    return None


def _ring(state):
    from .copy import CLAUDE_INBOX_DOORBELL
    body = {"type": "user", "message": {"role": "user", "content": CLAUDE_INBOX_DOORBELL}}
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(0.5)
        connection.connect(state["socket"])
        connection.sendall((json.dumps(body, separators=(",", ":")) + "\n").encode())
        connection.shutdown(socket.SHUT_WR)


def notify_send(root, message, *, clock=time.monotonic, wait=None):
    """Observe one durable native envelope after the send's locks are released."""
    from .errors import ProtocolRefusal
    from .jsonl import _locked_path
    node = message["recipient"]
    directory = _directory(root.tenant_home, node)
    state = _load(directory)
    if state is None or message.get("worker_session_id") is not None:
        return None
    descriptor = os.open(directory / "inbox-send.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        if not _ordinary(os.fstat(descriptor)):
            _refuse("doorbell send lock must be a private ordinary file")
        rings = 0
        def observation(code=None, proof=None):
            value = {"status": "fallback" if code else "confirmed", "code": code,
                "rings": rings, "policy_source": "readable_settings_files",
                "session": state["session"], "item_id": message["id"],
                "node": node, "tenant_id": root.tenant_id,
                "presentation_receipt_id": proof["id"] if proof else None,
                "delivery_receipt_id": proof["delivery_receipt_id"] if proof else None}
            _save(_observation_path(directory, state["session"], message["id"]), value)
            return value
        def fallback(code):
            result = observation(code)
            current = _load(directory, state["session"])
            if current == state:
                _save(_session_path(directory, state["session"]), dict(state, fallback=code))
            return result
        wait = threading.Event().wait if wait is None else wait
        last_empty_cut = None
        def confirmed():
            nonlocal last_empty_cut
            from .cursor_presentations import CursorPresentationLedger
            relative = CursorPresentationLedger.relative(node)
            try:
                info = os.stat(root.resolve_relative(relative), follow_symlinks=False)
                fingerprint = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
            except FileNotFoundError:
                return False
            if last_empty_cut == fingerprint:
                return False
            result = _confirmed(root, node, state, message)
            if not result:
                last_empty_cut = fingerprint
            return result
        def attempts():
            nonlocal rings
            for attempt in range(2):
                try:
                    code = state["fallback"] or _availability(state)
                    if code:
                        return fallback(code)
                    with _native_authority(root, node, state) as valid:
                        if not valid:
                            return fallback("claude_inbox_unavailable")
                    proof = confirmed()
                    if proof:
                        return observation(proof=proof)
                    # Release native locks before waiting for the receiving hook.
                    with _native_authority(root, node, state) as valid:
                        if not valid:
                            return fallback("claude_inbox_unavailable")
                        _ring(state)
                        rings += 1
                except (OSError, ProtocolRefusal):
                    return fallback("claude_inbox_unavailable")
                deadline = clock() + CONFIRM_SECONDS
                if not math.isfinite(deadline):
                    return fallback("claude_inbox_unconfirmed")
                while True:
                    try:
                        proof = confirmed()
                        if proof:
                            with _native_authority(root, node, state) as valid:
                                return observation(proof=proof) if valid else fallback("claude_inbox_unavailable")
                    except (OSError, ProtocolRefusal):
                        return fallback("claude_inbox_unavailable")
                    remaining = deadline - clock()
                    if remaining <= 0:
                        break
                    wait(min(POLL_SECONDS, remaining))
            return fallback("claude_inbox_unconfirmed")
        relative = (directory / "inbox-send.lock").relative_to(root.tenant_home)
        # Six seconds exceeds two (0.5s transport + 2s confirmation) arms.
        # Retain the private opener and the native bounded lock vocabulary.
        try:
            with _locked_path(directory / "inbox-send.lock", exclusive=True,
                    relative=relative, timeout_seconds=6, order_tracked=False,
                    _open_handle=lambda: os.fdopen(os.dup(descriptor), "r+b")):
                state = _load(directory)
                return attempts()
        except ProtocolRefusal:
            return fallback("claude_inbox_unconfirmed")
    finally:
        os.close(descriptor)

"""Explicit Claude settings installation, preserving foreign JSON bytes."""

from __future__ import annotations

import hashlib
import json
import shlex
from pathlib import Path
from typing import Optional

from .codex_hook_install import _rewrite_hook_group, _write_atomic
from .errors import DurabilityFailure, IntegrityFailure, ProtocolRefusal
from .registry import Registry
from .root import FloatiRoot
from .waiter_consent import arm_waiter_consent
from .zcode_hook_install import ZCODE_HOLD_SECONDS


_RELAY_EVENTS = ("SessionStart", "PostToolUse", "UserPromptSubmit")


def _refuse(code: str, detail: str) -> None:
    raise ProtocolRefusal("claude_hook_" + code, detail,
        remedy="use the same explicit settings, root, node, workspace and runtime; preserve changed settings before retrying")


def _settings_path(hooks: Optional[Path]) -> Path:
    # No default, expanduser, home lookup, or settings discovery is permitted.
    if hooks is None:
        _refuse("settings_required", "Claude requires an explicit --hooks settings path")
    if not isinstance(hooks, (str, Path)):
        _refuse("settings_invalid", "settings path must be a string or Path")
    path = Path(hooks)
    if not path.is_absolute() or path.resolve() != path or path.is_symlink():
        _refuse("settings_invalid", "settings must be an absolute canonical nonsymlink path")
    if path.exists() and not path.is_file():
        _refuse("settings_invalid", "settings path is not an ordinary file")
    return path


def _coordinates(workspace, root, node, runtime, hooks):
    settings = _settings_path(hooks)
    workspace, runtime, root = Path(workspace), Path(runtime), Path(root)
    for name, path in (("workspace", workspace), ("runtime", runtime), ("root", root)):
        if not path.is_absolute() or path.resolve() != path or not path.is_dir():
            _refuse("coordinates_invalid", f"{name} must be an existing absolute canonical directory")
    launcher = runtime / "scripts/floati"
    if launcher.is_symlink() or not launcher.is_file() or launcher.resolve() != launcher:
        _refuse("coordinates_invalid", "runtime must contain an ordinary scripts/floati launcher")
    fleet = FloatiRoot.open_direct_home(root)
    registry = Registry(fleet)
    node = registry.resolve_node_id(node, field="node")
    if registry.require_active(node)["role"].casefold() != "claude":
        _refuse("harness_mismatch", "the explicit node must be registered under Claude")
    directory = fleet.path / "state/claude-wait" / node
    if directory.resolve() != directory:
        _refuse("snapshot_invalid", "install snapshot directory must not traverse symlinks")
    return settings, workspace, runtime, fleet, node, directory


def _decode(before: bytes):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    def invalid_constant(value):
        raise ValueError("non-JSON number")

    try:
        text = before.decode("utf-8")
        document = json.loads(text, object_pairs_hook=unique, parse_constant=invalid_constant)
    except (UnicodeError, ValueError):
        _refuse("settings_invalid", "settings are not unambiguous UTF-8 JSON")
    if not isinstance(document, dict):
        _refuse("settings_invalid", "settings must be a JSON object")
    hooks = document.get("hooks", {})
    if not isinstance(hooks, dict) or any(
            not isinstance(hooks.get(event, []), list)
            for event in ("Stop",) + _RELAY_EVENTS):
        _refuse("settings_invalid", "hooks must be an object and Claude hook events must be arrays")
    return text, document


def format_claude_stop_command(*, runtime, root, node, hook_timeout_seconds, deadline_seconds):
    return " ".join(shlex.quote(str(part)) for part in (
        Path(runtime) / "scripts/floati", "wake", "wait", "--harness", "claude",
        "--root", root, "--as", node, "--runtime", runtime,
        "--deadline-seconds", deadline_seconds, "--hook-timeout-seconds", hook_timeout_seconds))


def format_claude_relay_command(*, runtime, root, node):
    return " ".join(shlex.quote(str(part)) for part in (
        Path(runtime) / "scripts/floati", "hook", "relay", "--harness", "claude",
        "--root", root, "--as", node, "--runtime", runtime))


def _insert(text, document, hook, event="Stop"):
    block = {"hooks": [hook]}
    if "hooks" in document:
        return _rewrite_hook_group(text, event, block)
    # Insert only the new member. Every original byte, including whitespace,
    # escaped strings and numeric spellings, stays in place.
    start = next(i for i, char in enumerate(text) if not char.isspace())
    insertion = '"hooks":' + json.dumps({event: [block]}, separators=(",", ":"))
    if document:
        insertion += ","
    return (text[:start + 1] + insertion + text[start + 1:]).encode("utf-8")


def _with_relays(text, document, command, events=_RELAY_EVENTS):
    for event in events:
        encoded = _insert(text, document,
            {"type": "command", "command": command, "timeout": 10}, event)
        text, document = _decode(encoded)
    return encoded


def _with_stop_and_relays(text, document, command, timeout, relay_command,
                          events=_RELAY_EVENTS):
    encoded = _insert(text, document,
        {"type": "command", "command": command, "timeout": timeout})
    text, document = _decode(encoded)
    return _with_relays(text, document, relay_command, events)


def _digest(data):
    return hashlib.sha256(data).hexdigest()


def _write(path, data):
    try:
        return _write_atomic(path, data)
    except OSError as exc:
        raise DurabilityFailure("claude_hook_write_failed",
            f"could not publish {path}; preserve the install snapshot and retry the exact coordinates") from exc


def _record(directory):
    path = directory / "installed-hook.json"
    if path.is_symlink():
        _refuse("snapshot_invalid", "install receipt is a symlink")
    if not path.exists():
        return None
    try:
        record = json.loads(path.read_bytes())
    except (OSError, ValueError):
        _refuse("snapshot_invalid", "install receipt is unreadable")
    if not isinstance(record, dict):
        _refuse("snapshot_invalid", "install receipt is not an object")
    return record


def _require_match(record, coordinates):
    if any(record.get(key) != value for key, value in coordinates.items()):
        _refuse("snapshot_mismatch", "the install receipt names different coordinates")


def _verify_owned_entries(document, command, timeout, relay_command):
    # Compare complete owned groups after JSON decoding: formatting and foreign
    # groups may change, but command strings and matcher/timeout content may not.
    def canonical(value):
        return json.dumps(value, sort_keys=True, separators=(",", ":"))

    for event in ("Stop",) + _RELAY_EVENTS:
        hook = {"type": "command", "command": command if event == "Stop" else relay_command,
                "timeout": timeout if event == "Stop" else 10}
        expected = canonical({"hooks": [hook]})
        blocks = document.get("hooks", {}).get(event, [])
        if sum(canonical(block) == expected for block in blocks) != 1:
            _refuse("settings_changed",
                f"Floati entry at hooks.{event} changed, is missing, or is duplicated")


def install_claude_hook(*, workspace: Path, root: Path, node: str, runtime: Path,
                        hooks: Optional[Path] = None, verify_existing: bool = False,
                        plan=None) -> dict:
    from .lifecycle_prompts import install_seat_commands

    if plan is None:
        settings, workspace, runtime, fleet, node, directory = _coordinates(
            workspace, root, node, runtime, hooks)
        root = fleet.path
    else:
        settings = _settings_path(hooks)
        directory = root / "state/claude-wait" / node
    existed = settings.exists()
    before = settings.read_bytes() if existed else b'{}\n'
    text, document = _decode(before)
    timeout, deadline = ZCODE_HOLD_SECONDS
    command = format_claude_stop_command(runtime=runtime, root=root, node=node,
        hook_timeout_seconds=timeout, deadline_seconds=deadline)
    relay_command = format_claude_relay_command(runtime=runtime, root=root, node=node)
    coordinates = {"hooks_path": str(settings), "workspace": str(workspace),
                   "runtime": str(runtime), "root": str(root), "node": node}
    record = _record(directory) if plan is None else None
    if record is not None:
        _require_match(record, coordinates)
        if record.get("command") != command:
            _refuse("settings_changed", "settings or runtime command changed since install")
        if "relay_command" not in record:
            # Upgrade only the exact old publication, retaining its original
            # uninstall preimage and waiter consent. The old installed hash
            # makes a failed settings publication recoverable on retry.
            if _digest(before) == record.get("installed_sha256"):
                encoded = _with_relays(text, document, relay_command)
                record["relay_upgrade_from_sha256"] = _digest(before)
            elif (_digest(before) == record.get("preinstall_sha256")
                    and existed == record.get("existed")):
                encoded = _with_stop_and_relays(text, document, command, timeout, relay_command)
            else:
                _refuse("settings_changed", "settings changed since install")
            install_seat_commands(workspace, 'claude')
            record.update(relay_command=relay_command, relay_events=list(_RELAY_EVENTS),
                          relay_upgrade_events=list(_RELAY_EVENTS),
                          installed_sha256=_digest(encoded))
            _write(directory / "installed-hook.json", (json.dumps(record, sort_keys=True) + "\n").encode())
            _write(settings, encoded)
            return record
        if record.get("relay_command") != relay_command:
            _refuse("settings_changed", "relay command changed since install")
        recorded_events = record.get("relay_events", ["SessionStart", "PostToolUse"])
        if recorded_events not in (["SessionStart", "PostToolUse"], list(_RELAY_EVENTS)):
            _refuse("snapshot_invalid", "install receipt names unsupported relay events")
        if verify_existing and recorded_events == list(_RELAY_EVENTS):
            # Boot checks only this completed installation's owned entries. It
            # never repairs or rewrites a settings file Claude or the user owns.
            # Explicit reinstall/uninstall and legacy upgrades retain their
            # exact-file preimage checks below.
            _verify_owned_entries(document, command, timeout, relay_command)
            install_seat_commands(workspace, 'claude')
            return record
        if _digest(before) != record.get("installed_sha256"):
            # A failed atomic publication can leave the exact original bytes
            # and durable snapshot. Complete only that proven pending write.
            if (_digest(before) == record.get("preinstall_sha256")
                    and existed == record.get("existed")):
                encoded = _with_stop_and_relays(text, document, command, timeout,
                                               relay_command, recorded_events)
            elif _digest(before) == record.get("relay_upgrade_from_sha256"):
                encoded = _with_relays(text, document, relay_command,
                    record.get("relay_upgrade_events", ["SessionStart", "PostToolUse"]))
            else:
                _refuse("settings_changed", "settings changed since install")
            if _digest(encoded) != record.get("installed_sha256"):
                _refuse("settings_changed", "settings changed since install")
            _write(settings, encoded)
            before = encoded
            text, document = _decode(before)
        if recorded_events != list(_RELAY_EVENTS):
            # Earlier relay installs already own the startup and tool hooks.
            # Add only the new event, preserving those handlers and every
            # foreign byte. Keep the original preimage for exact uninstall.
            encoded = _with_relays(text, document, relay_command, ("UserPromptSubmit",))
            install_seat_commands(workspace, 'claude')
            record.update(relay_events=list(_RELAY_EVENTS),
                          relay_upgrade_from_sha256=_digest(before),
                          relay_upgrade_events=["UserPromptSubmit"],
                          installed_sha256=_digest(encoded))
            _write(directory / "installed-hook.json", (json.dumps(record, sort_keys=True) + "\n").encode())
            _write(settings, encoded)
            return record
        install_seat_commands(workspace, 'claude')
        return record
    encoded = _with_stop_and_relays(text, document, command, timeout, relay_command)
    if plan is None:
        install_seat_commands(workspace, 'claude')
        consent = arm_waiter_consent(root, workspace, node, "claude", timeout, deadline)
    else:
        plan.commands(workspace, 'claude')
        consent = plan.consent(workspace, node, "claude", timeout, deadline)
    record = {"schema_version": 0, "harness": "claude", **coordinates,
              "command": command, "relay_command": relay_command,
              "relay_events": list(_RELAY_EVENTS),
              "existed": existed, "preinstall_sha256": _digest(before),
              "installed_sha256": _digest(encoded), "consent": consent}
    # Snapshot and receipt precede publication so exact restoration survives
    # failure after replacing settings. A changed preimage refuses on retry.
    write = _write if plan is None else plan.file
    write(directory / "pre-install-settings.bin", before)
    write(directory / "installed-hook.json", (json.dumps(record, sort_keys=True) + "\n").encode())
    write(settings, encoded)
    return record


def uninstall_claude_hook(*, workspace: Path, root: Path, node: str, runtime: Path,
                          hooks: Optional[Path] = None) -> dict:
    settings, workspace, runtime, fleet, node, directory = _coordinates(
        workspace, root, node, runtime, hooks)
    record = _record(directory)
    if record is None:
        _refuse("uninstall_absent", "no Claude installation is recorded for this node")
    _require_match(record, {"hooks_path": str(settings), "workspace": str(workspace),
        "runtime": str(runtime), "root": str(fleet.path), "node": node})
    if not settings.is_file() or _digest(settings.read_bytes()) != record.get("installed_sha256"):
        _refuse("settings_changed", "settings changed since install; exact restoration would overwrite them")
    snapshot = directory / "pre-install-settings.bin"
    if snapshot.is_symlink() or not snapshot.is_file():
        _refuse("snapshot_invalid", "pre-install settings bytes are missing")
    before = snapshot.read_bytes()
    if _digest(before) != record.get("preinstall_sha256") or type(record.get("existed")) is not bool:
        _refuse("snapshot_invalid", "pre-install settings snapshot is inconsistent")
    if record["existed"]:
        _write(settings, before)
    else:
        settings.unlink()
    (directory / "installed-hook.json").unlink()
    snapshot.unlink()
    return {"schema_version": 0, "harness": "claude", "hooks_path": str(settings),
            "workspace": str(workspace), "root": str(fleet.path), "node": node, "restored": True}


def project_claude_installed_hook_findings(root):
    """Read only explicit recorded settings; never discover account settings."""
    from .doctor import _exception_finding, _finding

    findings = []
    wait_root = root.path / "state/claude-wait"
    registry = Registry(root)
    try:
        nodes = [node for node in registry.active_node_ids()
                 if registry.require_active(node)["role"].casefold() == "claude"]
    except (ProtocolRefusal, IntegrityFailure, DurabilityFailure) as exc:
        return [_exception_finding(exc, "error", str(root.path))]
    for node in nodes:
        directory = wait_root / node
        subject = node + "/claude"
        try:
            if directory.resolve() != directory or wait_root.is_symlink() or directory.is_symlink():
                _refuse("snapshot_invalid", "install receipt directory is a symlink")
            record = _record(directory)
            if record is None:
                findings.append(_finding("claude_hook_absent", "warning", subject,
                    "no explicit Claude Stop hook installation is recorded",
                    "install Claude with an explicit --hooks settings path"))
                continue
            path = _settings_path(record.get("hooks_path"))
            data = path.read_bytes() if path.is_file() else None
            installed = data is not None and _digest(data) == record.get("installed_sha256")
            code = "claude_hook_installed" if installed else "claude_hook_absent"
            severity = "ok" if installed else "warning"
            consent = record.get("consent", {})
            timeout, deadline = consent.get("hook_timeout_seconds"), consent.get("wait_deadline_seconds")
            findings.append(_finding(code, severity, subject,
                f"Claude Stop hook {path}: {code}; timeout {timeout} s, waiter deadline {deadline} s",
                None if installed else "review the explicitly recorded settings path and reinstall"))
        except (OSError, ValueError, ProtocolRefusal, AttributeError):
            findings.append(_finding("claude_hook_invalid", "warning", subject,
                "the explicit Claude hook installation record or settings cannot be read safely",
                "review the Claude install receipt and explicit settings path"))
    return findings


def run_claude_stop_wait(args):
    """Validate explicit seat coordinates, then use the native Stop waiter."""
    import sys
    from .codex_wait import run_stop_waiter
    from .codex_wait_contract import CodexWaitConsentLedger, resolve_participant
    from .wake_control import validate_session_id

    if sys.stdin.isatty():
        _refuse("payload_invalid", "a Claude Stop payload is required on stdin")
    try:
        payload = json.loads(sys.stdin.read())
    except (ValueError, OSError):
        _refuse("payload_invalid", "Claude Stop payload is not JSON")
    if not isinstance(payload, dict) or payload.get("hook_event_name", "Stop") != "Stop":
        _refuse("payload_invalid", "expected a Claude Stop object")
    workspace = payload.get("cwd")
    if not isinstance(workspace, str) or not workspace:
        _refuse("payload_invalid", "Claude Stop payload must name cwd")
    validate_session_id(payload.get("session_id"))
    participant = resolve_participant(Path(args.root), Path(workspace))
    if participant is None or participant.binding.node_id != args.actor:
        _refuse("workspace_mismatch", "Stop payload does not resolve to the explicit node")
    if Registry(participant.root).require_active(args.actor)["role"].casefold() != "claude":
        _refuse("harness_mismatch", "the explicit node is not registered under Claude")
    consent = CodexWaitConsentLedger(participant.root).require_armed(participant.binding)
    if (args.hook_timeout_seconds != consent["hook_timeout_seconds"]
            or args.deadline_seconds != consent["wait_deadline_seconds"]):
        _refuse("hold_mismatch", "command hold differs from the armed waiter consent")
    from .claude_inbox import stop_path_required
    if not stop_path_required(Path(args.root), args.actor, payload["session_id"]):
        return 0
    return run_stop_waiter(bus_home=Path(args.root), hook_payload=payload,
                           stdout=sys.stdout, stderr=sys.stderr)

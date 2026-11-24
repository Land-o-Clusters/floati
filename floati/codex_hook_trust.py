"""Read-only measurement of Codex hook trust for Floati-owned waiter hooks."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shlex
import sys
from pathlib import Path
from typing import Dict, List, Optional

from .errors import ProtocolRefusal


_TRUSTED_HASH = re.compile(r"[0-9a-f]{64}\Z")
_CODEX_WAITER_NAMES = frozenset({"floati-codex-wait", "floati-codex-wait.py"})
_INCOMPLETE_CODEX_LAUNCHER = re.compile(
    r"(?:/[^\x00\r\n]*?/)?floati-codex-wait(?:\.py)?(?=[ \t]|$)")


def _codex_waiter_group_candidate(group: object) -> bool:
    """Classify launcher positions only; this grants no command/trust authority."""
    def candidate(command):
        if not isinstance(command, str):
            return False
        lexer = shlex.shlex(command, posix=True)
        lexer.whitespace_split = True
        lexer.commenters = ""

        def word():
            try:
                return lexer.get_token(), False
            except ValueError:
                # shlex retains the unfinished lexical word. It is used only
                # to identify uncertainty at an executable/script position,
                # never as parsed argv, a launcher path or authority.
                return lexer.token, True

        executable, unfinished = word()
        if unfinished:
            return _INCOMPLETE_CODEX_LAUNCHER.match(executable) is not None
        if executable is None:
            return False
        if Path(executable).name in _CODEX_WAITER_NAMES:
            return True
        if re.fullmatch(r"python(?:3(?:\.\d+)?)?", Path(executable).name) is not None:
            while True:
                token, unfinished = word()
                if unfinished:
                    return _INCOMPLETE_CODEX_LAUNCHER.match(token) is not None
                if token is None:
                    return False
                if token == "--":
                    token, unfinished = word()
                    return (_INCOMPLETE_CODEX_LAUNCHER.match(token) is not None if unfinished
                        else token is not None and Path(token).name in _CODEX_WAITER_NAMES)
                if token.startswith(("-c", "-m")):
                    return False  # Inline code/module operands are not script paths.
                if token in {"-W", "-X", "--check-hash-based-pycs"}:
                    _value, unfinished = word()
                    if unfinished or _value is None:
                        return False
                    continue
                if token.startswith("-"):
                    continue
                # Only the first actual script operand is inspected. Audit
                # arguments after a foreign Python script cannot claim ownership.
                return Path(token).name in _CODEX_WAITER_NAMES
        # Preserve the bounded bare-token malformed-owner refusal, including
        # `echo floati-codex-wait`, without searching arbitrary later arguments.
        second, unfinished = word()
        return not unfinished and second in _CODEX_WAITER_NAMES

    if not isinstance(group, dict):
        return False
    if candidate(group.get("command")):
        return True  # A misplaced actual command is a malformed candidate.
    handlers = group.get("hooks")
    if isinstance(handlers, dict):
        return candidate(handlers.get("command"))
    return isinstance(handlers, list) and any(isinstance(handler, dict)
        and candidate(handler.get("command")) for handler in handlers)


HOOK_TRUST_REMEDIATION = (
    "Review and trust this exact Stop hook in Codex settings, then "
    "relaunch the session; hook bytes are not an armed hook."
)

# BL-11: the ruled operator banner — one remedy everywhere (the gateway's
# board guard and doctor). The position is the measured Stop-hook group
# index, so the sentence names the exact hook.
STOP_HOOK_BANNER_TEMPLATE = (
    "**Codex has not trusted Puddle's Stop hook.** Codex → Settings → Hooks → "
    "trust `floati-codex-wait` (Stop hook {position}), relaunch this thread, "
    "run `/boardbus` again."
)


def stop_hook_banner(position: int) -> str:
    return STOP_HOOK_BANNER_TEMPLATE.format(position=position)


def board_guard_observation(
    hooks_path: Path, config_path: Optional[Path] = None
) -> Dict[str, object]:
    """Measure whether boarding may proceed under BL-11.

    Refuses when any measured Floati waiter is not armed; a board that leaves
    ``hook_armed:false`` silently is the surface the ruling outlaws. An absent
    waiter refuses nothing here — the install path owns that condition.
    """

    rows = observe_codex_waiter_hooks(hooks_path, config_path)
    for row in rows:
        if row.get("hook_armed") is not True:
            position = int(str(row.get("hook_trust_key", "")).rsplit(":", 2)[-2])
            return {
                "refused": True,
                "code": "stop_hook_untrusted",
                "hook_trust_key": row["hook_trust_key"],
                "hook_group_index": position,
                "hook_trust_status": row.get("hook_trust_status"),
                "banner": row.get("hook_trust_remediation") or stop_hook_banner(position),
            }
    return {"refused": False, "measured_waiters": len(rows)}


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m floati.codex_hook_trust",
        description="Measure Codex Stop-hook trust for Floati waiter hooks.",
    )
    parser.add_argument("--hooks", required=True)
    parser.add_argument("--config", default=None)
    arguments = parser.parse_args(argv)
    guard = board_guard_observation(
        Path(arguments.hooks),
        None if arguments.config is None else Path(arguments.config),
    )
    print(
        json.dumps(
            {"board_guard": guard}, sort_keys=True, separators=(",", ":")
        )
    )
    return 0


def codex_hook_current_hash(block: object) -> str:
    """Reproduce Codex's normalized trust hash for one Stop command group."""

    if not isinstance(block, dict) or set(block) != {"hooks"}:
        raise ProtocolRefusal("codex_wait_hook_trust_unavailable", "waiter hook group is invalid")
    hooks = block.get("hooks")
    if not isinstance(hooks, list) or len(hooks) != 1 or not isinstance(hooks[0], dict):
        raise ProtocolRefusal("codex_wait_hook_trust_unavailable", "waiter hook handler is invalid")
    hook = hooks[0]
    command = hook.get("command")
    timeout = hook.get("timeout", 600)
    status_message = hook.get("statusMessage")
    if (
        hook.get("type") != "command"
        or not isinstance(command, str)
        or not isinstance(timeout, int)
        or isinstance(timeout, bool)
        or status_message is not None
        and not isinstance(status_message, str)
    ):
        raise ProtocolRefusal("codex_wait_hook_trust_unavailable", "waiter hook identity is invalid")
    handler: Dict[str, object] = {
        "async": bool(hook.get("async", False)),
        "command": command,
        "timeout": max(1, timeout),
        "type": "command",
    }
    if status_message is not None:
        handler["statusMessage"] = status_message
    identity = {"event_name": "stop", "hooks": [handler]}
    encoded = json.dumps(
        identity, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    # Fleet-update testimony and the receipt schema use one bare SHA-256
    # representation.  Codex's settings state is compared against this same
    # normalized value; an absent setting remains ``None`` rather than a
    # fabricated digest.
    return hashlib.sha256(encoded).hexdigest()


def _read_state(config_path: Path, key: str) -> tuple[Optional[bool], Optional[str]]:
    """Read only the exact user-state table; absence means enabled and untrusted."""

    if config_path.is_symlink():
        raise ProtocolRefusal(
            "codex_wait_hook_trust_unavailable", "Codex trust config is symlinked"
        )
    if not config_path.exists():
        return True, None
    try:
        lines = config_path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        raise ProtocolRefusal(
            "codex_wait_hook_trust_unavailable", "Codex trust config is unreadable"
        ) from exc
    header = "[hooks.state." + json.dumps(key, ensure_ascii=False) + "]"
    start = next((index for index, line in enumerate(lines) if line.strip() == header), None)
    if start is None:
        return True, None
    enabled: Optional[bool] = True
    trusted_hash: Optional[str] = None
    for line in lines[start + 1 :]:
        stripped = line.strip()
        if stripped.startswith("["):
            break
        enabled_match = re.fullmatch(r"enabled\s*=\s*(true|false)", stripped)
        if enabled_match is not None:
            enabled = enabled_match.group(1) == "true"
            continue
        if stripped.startswith("trusted_hash"):
            hash_match = re.fullmatch(r"trusted_hash\s*=\s*(\"(?:[^\"\\]|\\.)*\")", stripped)
            if hash_match is None:
                raise ProtocolRefusal(
                    "codex_wait_hook_trust_unavailable",
                    "Codex trusted hash is malformed",
                )
            try:
                value = json.loads(hash_match.group(1))
            except json.JSONDecodeError as exc:
                raise ProtocolRefusal(
                    "codex_wait_hook_trust_unavailable",
                    "Codex trusted hash is malformed",
                ) from exc
            if not isinstance(value, str):
                raise ProtocolRefusal(
                    "codex_wait_hook_trust_unavailable",
                    "Codex trusted hash is malformed",
                )
            if _TRUSTED_HASH.fullmatch(value) is not None:
                trusted_hash = value
            elif value.startswith("sha256:") and _TRUSTED_HASH.fullmatch(value[7:]) is not None:
                trusted_hash = value[7:]
            else:
                raise ProtocolRefusal(
                    "codex_wait_hook_trust_unavailable",
                    "Codex trusted hash is malformed",
                )
    return enabled, trusted_hash


def observe_codex_waiter_hooks(
    hooks_path: Path, config_path: Optional[Path] = None, *,
    definition: Optional[bytes] = None,
) -> list[Dict[str, object]]:
    """Measure every Floati waiter in one explicitly named Codex hooks file."""

    hooks_path = Path(hooks_path).expanduser()
    config_path = (
        hooks_path.with_name("config.toml")
        if config_path is None
        else Path(config_path).expanduser()
    )
    if (
        not hooks_path.is_absolute()
        or hooks_path.is_symlink()
        or not config_path.is_absolute()
    ):
        raise ProtocolRefusal(
            "codex_wait_hook_trust_unavailable",
            "Codex hook and trust paths must be absolute non-symlink identities",
        )
    try:
        document = json.loads(hooks_path.read_text(encoding="utf-8") if definition is None else definition)
        groups = document["hooks"]
        if not isinstance(groups, dict):
            raise TypeError("hooks must be an object")
        stop = groups.get("Stop", [])
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise ProtocolRefusal(
            "codex_wait_hook_trust_unavailable", "Codex Stop hooks are unreadable"
        ) from exc
    if not isinstance(stop, list):
        raise ProtocolRefusal(
            "codex_wait_hook_trust_unavailable", "Codex Stop hooks are invalid"
        )
    observations: list[Dict[str, object]] = []
    from .codex_hook_install import parse_waiter_stop_argv

    for group_index, block in enumerate(stop):
        if not _codex_waiter_group_candidate(block):
            continue
        current_hash = codex_hook_current_hash(block)
        try:
            parsed = parse_waiter_stop_argv(shlex.split(block["hooks"][0]["command"]))
        except ValueError:
            parsed = None
        supported = parsed is not None and parsed[0].is_absolute() and (
            parsed[0].name in _CODEX_WAITER_NAMES and parsed[1].is_absolute())
        key = f"{hooks_path}:stop:{group_index}:0"
        enabled, trusted_hash = _read_state(config_path, key)
        trust_status = (
            "trusted"
            if trusted_hash == current_hash
            else "modified"
            if trusted_hash is not None
            else "untrusted"
        )
        armed = enabled is True and trust_status == "trusted" and supported
        observations.append(
            {
                "hook_trust_key": key,
                "hook_group_index": group_index,
                "hook_trust": "trusted" if trust_status == "trusted" else "untrusted_pending_user",
                "hook_trust_status": trust_status,
                "hook_trust_current_hash": current_hash,
                "hook_trust_observed_hash": trusted_hash,
                "hook_enabled": enabled,
                "hook_armed": armed,
                "hook_trust_remediation": (
                    "repair the unsupported Stop command through explicit native hook installation, "
                    "then review owner trust and relaunch the session" if not supported
                    else None if armed else stop_hook_banner(group_index)
                ),
            }
        )
    return observations


def observe_rebound_waiter(
    hooks_path: Path, *, expected_key: str, expected_hash: str
) -> Dict[str, object]:
    """Re-observe one planned hook coordinate after a surgical rebind.

    This is deliberately a measurement only: it does not alter Codex's trust
    state and never promotes the observed hook state to a running-session fact.
    """

    if not isinstance(expected_key, str) or not isinstance(expected_hash, str):
        raise ProtocolRefusal("fleet_update_waiter_binding_drift", "planned trust coordinate is invalid")
    rows = observe_codex_waiter_hooks(hooks_path)
    if len(rows) != 1:
        raise ProtocolRefusal("fleet_update_waiter_binding_drift", "waiter trust coordinate is ambiguous after rebind")
    row = rows[0]
    if row.get("hook_trust_key") != expected_key or row.get("hook_trust_current_hash") != expected_hash:
        raise ProtocolRefusal("fleet_update_waiter_binding_drift", "waiter trust coordinate changed after rebind")
    return row


if __name__ == "__main__":
    main()

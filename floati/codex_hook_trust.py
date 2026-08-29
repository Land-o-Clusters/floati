"""Read-only measurement of Codex hook trust for Floati-owned waiter hooks."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Dict, Optional

from .errors import ProtocolRefusal


HOOK_TRUST_REMEDIATION = (
    "Review and trust this exact Stop hook in Codex settings, then "
    "relaunch the session; hook bytes are not an armed hook."
)


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
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


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
        hash_match = re.fullmatch(r"trusted_hash\s*=\s*(\"(?:[^\"\\]|\\.)*\")", stripped)
        if hash_match is not None:
            try:
                value = json.loads(hash_match.group(1))
            except json.JSONDecodeError as exc:
                raise ProtocolRefusal(
                    "codex_wait_hook_trust_unavailable",
                    "Codex trusted hash is malformed",
                ) from exc
            if isinstance(value, str):
                trusted_hash = value
    return enabled, trusted_hash


def observe_codex_waiter_hooks(
    hooks_path: Path, config_path: Optional[Path] = None
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
        document = json.loads(hooks_path.read_text(encoding="utf-8"))
        stop = document["hooks"]["Stop"]
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise ProtocolRefusal(
            "codex_wait_hook_trust_unavailable", "Codex Stop hooks are unreadable"
        ) from exc
    if not isinstance(stop, list):
        raise ProtocolRefusal(
            "codex_wait_hook_trust_unavailable", "Codex Stop hooks are invalid"
        )
    observations: list[Dict[str, object]] = []
    for group_index, block in enumerate(stop):
        if "floati-codex-wait" not in json.dumps(block, sort_keys=True):
            continue
        current_hash = codex_hook_current_hash(block)
        key = f"{hooks_path}:stop:{group_index}:0"
        enabled, trusted_hash = _read_state(config_path, key)
        trust_status = (
            "trusted"
            if trusted_hash == current_hash
            else "modified"
            if trusted_hash is not None
            else "untrusted"
        )
        armed = enabled is True and trust_status == "trusted"
        observations.append(
            {
                "hook_trust_key": key,
                "hook_trust": "trusted" if trust_status == "trusted" else "untrusted_pending_user",
                "hook_trust_status": trust_status,
                "hook_trust_current_hash": current_hash,
                "hook_trust_observed_hash": trusted_hash,
                "hook_enabled": enabled,
                "hook_armed": armed,
                "hook_trust_remediation": None if armed else HOOK_TRUST_REMEDIATION,
            }
        )
    return observations

"""`floati waiter arm` — the public, harness-typed waiter consent act.

The consent ledger is append-only and shared by every harness; the only
code path that armed it was the Codex hook installer, so every other
harness armed it through a Python heredoc. This module is the runbook's
step 3 as one verb: it checks the seat's registered harness, maps the
workspace, and arms the same ledger under the same derived-key
idempotency the installer uses. It writes nothing until every check has
passed — a refused arm leaves no mutation.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Dict

from .codex_wait_contract import (
    CodexWaitConsentLedger,
    resolve_participant,
    write_workspace_map,
)
from .errors import ProtocolRefusal
from .registry import Registry
from .root import FloatiRoot


def arm_waiter_consent(
    bus_home: Path,
    workspace: Path,
    node_id: str,
    harness: str,
    hook_timeout_seconds: int,
    wait_deadline_seconds: int,
) -> Dict[str, object]:
    """Map one workspace and arm its waiter consent, harness-checked first."""

    if not isinstance(node_id, str) or not isinstance(harness, str) or not node_id or not harness:
        raise ProtocolRefusal(
            "waiter_arm_identity_invalid",
            "node and harness must be non-empty identifiers",
            remedy="pass --node and --harness as non-empty values; the node must be registered",
        )
    if (
        not isinstance(hook_timeout_seconds, int)
        or isinstance(hook_timeout_seconds, bool)
        or not isinstance(wait_deadline_seconds, int)
        or isinstance(wait_deadline_seconds, bool)
        or not 0 < wait_deadline_seconds < hook_timeout_seconds <= 86400
    ):
        raise ProtocolRefusal(
            "wait_deadline_invalid",
            "wait deadline must be positive and strictly below hook timeout",
        )
    workspace = Path(workspace).expanduser()
    if (
        not workspace.is_absolute()
        or workspace.is_symlink()
        or not workspace.is_dir()
    ):
        raise ProtocolRefusal(
            "codex_wait_workspace_invalid",
            "workspace must be an existing absolute directory",
        )
    root = FloatiRoot.open_direct_home(Path(bus_home))
    node = Registry(root).resolve_node_id(node_id, field="node")
    role = Registry(root).require_active(node).get("role")
    if not isinstance(role, str) or role.casefold() != harness.casefold():
        raise ProtocolRefusal(
            "waiter_harness_mismatch",
            f"node {node!r} is registered as harness {role!r}, not {harness!r}",
            remedy=(
                "arm with the registered harness spelling, or register the "
                "seat under this harness first"
            ),
        )
    map_digest = write_workspace_map(Path(bus_home), workspace, node)
    participant = resolve_participant(Path(bus_home), workspace)
    if participant is None or participant.binding.node_id != node:
        raise ProtocolRefusal(
            "codex_wait_participant_unresolved", "armed workspace did not resolve"
        )
    key_material = json.dumps(
        {
            "harness": harness.casefold(),
            "node_id": node,
            "workspace": participant.binding.workspace.as_posix(),
            "hook_timeout_seconds": hook_timeout_seconds,
            "wait_deadline_seconds": wait_deadline_seconds,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    idempotency_key = "waiter-consent-arm-" + hashlib.sha256(key_material).hexdigest()[:32]
    receipt = CodexWaitConsentLedger(participant.root).arm(
        participant.binding,
        hook_timeout_seconds=hook_timeout_seconds,
        wait_deadline_seconds=wait_deadline_seconds,
        idempotency_key=idempotency_key,
    )
    return {
        "node_id": receipt["node_id"],
        "workspace": receipt["workspace"],
        "harness": harness.casefold(),
        "hook_timeout_seconds": receipt["hook_timeout_seconds"],
        "wait_deadline_seconds": receipt["wait_deadline_seconds"],
        "state": receipt["state"],
        "consent_receipt_id": receipt["id"],
        "workspace_map_digest": map_digest,
        "idempotency_key": idempotency_key,
    }

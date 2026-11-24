"""Sender testimony captured by the native send transaction."""

from datetime import datetime
import os
from pathlib import Path
import re

from .errors import FloatiError
from .ids import uuid7_hex

KIND = "native_dispatch_launcher_record"
_NATIVE_CODEX_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}")


def native_launcher_runtime_session():
    session = os.environ.get("CODEX_THREAD_ID", "")
    return session if _NATIVE_CODEX_ID.fullmatch(session) else None


def capture_native_launcher(root, envelope, session):
    """Called under the sender's wake guard, through the event append."""
    from .codex_wait_contract import (
        CodexWaitConsentLedger, CodexWaitSessionLedger, resolve_participant,
    )
    from .jsonl import read_records_snapshot
    from .registry import Registry
    from .seat_declaration import require_declared_coordinate
    from .wake_control import is_session_paused

    sender = envelope["sender"]
    try:
        workspace = Path(os.environ["FLOATI_GATEWAY_SEND_WORKSPACE"]) if (
            "FLOATI_GATEWAY_SEND_WORKSPACE" in os.environ
        ) else Path.cwd()
        # A managed gateway can run from an external, mapped product
        # workspace. A present seat marker must still agree with the sender.
        require_declared_coordinate(workspace, sender, root)
        if Registry(root).require_active(sender)["role"].casefold() != "codex":
            return None
        participant = resolve_participant(root.tenant_home, workspace)
        if participant is None or participant.binding.node_id != sender:
            return None
        consent = CodexWaitConsentLedger(root).require_armed(participant.binding)
        if is_session_paused(root, sender, session):
            return None
        claims = read_records_snapshot(root, CodexWaitSessionLedger._relative(sender),
                                       allowed_kinds=CodexWaitSessionLedger._KINDS)
    except (FloatiError, OSError):
        return None
    claim = next((row for row in reversed(claims)
                  if row["workspace"] == str(participant.binding.workspace)), None)
    if (claim is None or claim["acting_session_id"] != session
            or claim["consent_receipt_id"] != consent["id"]
            or datetime.fromisoformat(claim["timestamp"].replace("Z", "+00:00"))
            > datetime.fromisoformat(envelope["timestamp"].replace("Z", "+00:00"))):
        return None
    return {
        "schema_version": 1, "kind": KIND, "id": "native-dispatch-launcher-" + uuid7_hex(),
        "tenant_id": root.tenant_id, "timestamp": envelope["timestamp"],
        "message_id": envelope["id"], "sender_node_id": sender,
        "sender_provider": "codex", "sender_native_session_id": session,
        "session_claim_id": claim["id"],
    }


def validate_native_launcher_sender(record, refuse):
    provider = record["sender_provider"]
    session = record["sender_native_session_id"]
    if (provider != "codex" or type(session) is not str or not _NATIVE_CODEX_ID.fullmatch(session)):
        refuse("native_dispatch_launcher_invalid", "launcher requires a native Codex session")

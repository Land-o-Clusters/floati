"""Read-only inventory of documented desktop callback contracts.

SUPPORTED describes a native event contract accepted by a producer; it never
proves hook installation, host availability, receipt delivery, or a live seat's
execution state. UNKNOWN preserves the named gap without probing ambient state.

Vendor references, surveyed 2026-10-25:
https://learn.chatgpt.com/docs/hooks
https://code.claude.com/docs/en/hooks
https://cursor.com/docs/hooks
https://github.com/xai-org/grok-build/blob/main/crates/codegen/xai-grok-pager/docs/user-guide/10-hooks.md
https://opencode.ai/docs/plugins/
OpenCode event delivery is pinned to its v1.18.23 plugin runtime below.
"""

CODEX_HOOKS = "https://learn.chatgpt.com/docs/hooks"
CLAUDE_HOOKS = "https://code.claude.com/docs/en/hooks"
CURSOR_HOOKS = "https://cursor.com/docs/hooks"
GROK_HOOKS = ("https://github.com/xai-org/grok-build/blob/main/"
    "crates/codegen/xai-grok-pager/docs/user-guide/10-hooks.md")
OPENCODE_PLUGIN = ("https://raw.githubusercontent.com/anomalyco/opencode/"
    "v1.18.23/packages/opencode/src/plugin/index.ts")


def _unknown(reason, detail, source=None):
    row = {"state": "UNKNOWN", "reason": reason, "detail": detail}
    if source is not None:
        row["source"] = source
    return row


def _notification(source, notification_type, identity, detail):
    return {"state": "SUPPORTED", "reason": "documented_native_callback",
        "event": "Notification", "notification_type": notification_type,
        "required_identity": identity, "detail": detail, "source": source}


def desktop_callback_support(harness):
    """Return fresh contract metadata, with no filesystem or host inspection.

    Callback producers must still authenticate the current native workspace,
    session and claimed car. This inventory supplies none of that authority.
    """
    name = harness.strip().casefold() if isinstance(harness, str) else "unknown"
    name = {"claude code": "claude", "claude-code": "claude"}.get(name, name)
    name = name or "unknown"
    if name == "codex":
        pending = _unknown("codex_pending_approval_callback_absent",
            "PermissionRequest precedes the approval prompt and another hook may "
            "allow or deny automatically; it cannot testify that approval is pending.", CODEX_HOOKS)
        ended = _unknown("codex_ended_turn_callback_absent",
            "Stop is a continuation gate that can block the stop; its invocation "
            "does not testify that a desktop turn ended.", CODEX_HOOKS)
    elif name == "cursor":
        pending = _unknown("cursor_approval_callback_absent",
            "The documented Cursor hook inventory has no notification of an "
            "approval prompt actually waiting for the user.", CURSOR_HOOKS)
        ended = {"state": "SUPPORTED", "reason": "documented_native_callback",
            "event": "stop", "source": CURSOR_HOOKS,
            "detail": "The native stop callback reports the completed agent loop; "
            "the existing Cursor producer authenticates its session and generation."}
    elif name == "claude":
        pending = _notification(CLAUDE_HOOKS, "permission_prompt", "prompt_id",
            "Observe the actual permission notification with native session_id and "
            "prompt_id; PermissionRequest is a decision gate and is not used.")
        ended = _notification(CLAUDE_HOOKS, "idle_prompt", "prompt_id",
            "Observe the native idle notification after responding, requiring "
            "session_id and prompt_id; Stop can block and is not used.")
    elif name == "grok-build":
        pending = _notification(GROK_HOOKS, "permission_prompt", "promptId",
            "Observe the actual permission notification with native sessionId and "
            "promptId; no caller-created turn identity is accepted.")
        ended = _notification(GROK_HOOKS, "idle_prompt", "timestamp",
            "Observe the session-scoped idle notification using native sessionId "
            "and dispatch timestamp. It has no native promptId or turn identity; "
            "timestamp identifies the observation, not when a turn ended. Stop "
            "can block and is not used.")
    elif name == "opencode":
        pending = {"state": "SUPPORTED", "reason": "documented_native_callback",
            "event": "permission.asked", "required_identity": "event.id",
            "source": OPENCODE_PLUGIN, "host_version": "1.18.23",
            "detail": "Observe the native plugin permission event after host "
            "directory filtering, requiring event.id and properties.sessionID; "
            "the plugin directory supplies native workspace scope."}
        ended = {"state": "SUPPORTED", "reason": "documented_native_callback",
            "event": "session.idle", "required_identity": "event.id",
            "source": OPENCODE_PLUGIN, "host_version": "1.18.23",
            "detail": "Observe the native plugin session-idle event after host "
            "directory filtering, requiring event.id and properties.sessionID; "
            "the event identifies an idle observation, not an invented turn."}
    else:
        prefix = name if name == "zcode" else name + "_host"
        suffix = "_callback_absent" if name == "zcode" else "_callback_not_documented"
        pending = _unknown(prefix + "_pending_approval" + suffix,
            "No supported pending-approval callback is documented in this "
            "contract inventory for " + name + ".")
        ended = _unknown(prefix + "_ended_turn" + suffix,
            "No supported ended-turn callback is documented in this contract "
            "inventory for " + name + ".")
    return {"harness": name, "pending_approval": pending, "ended_turn": ended}

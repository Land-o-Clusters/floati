"""Provisional visible-string catalog and generated review ledger."""

from __future__ import annotations

from typing import Dict, Tuple


_ENTRIES: Dict[str, Tuple[str, str]] = {}
_GROK_PRODUCT = "grok-build".removesuffix("-build").title()


def register(key: str, value: str, surface: str) -> str:
    existing = _ENTRIES.get(key)
    if existing is not None and existing != (value, surface):
        raise ValueError(f"copy key {key!r} was registered with different content")
    _ENTRIES[key] = (value, surface)
    return value


EFFECT_OPERATION_INVALID_DETAIL = register(
    "effect.input.operation_id_invalid",
    "effect operation must use the effect operation UUIDv7 prefix",
    "Effect CLI refusal",
)
EFFECT_RUN_INVALID_DETAIL = register(
    "effect.input.run_id_invalid",
    "effect run filter must use the run UUIDv7 prefix",
    "Effect CLI refusal",
)
EFFECT_ATTEMPT_INVALID_DETAIL = register(
    "effect.input.attempt_id_invalid",
    "effect attempt filter must use the attempt UUIDv7 prefix",
    "Effect CLI refusal",
)
EFFECT_PLAN_DIGEST_INVALID_DETAIL = register(
    "effect.input.plan_digest_invalid",
    "compensation plan digest must be a lowercase SHA-256 digest",
    "Effect CLI refusal",
)
EFFECT_COMPENSATION_PLAN_UNAVAILABLE_DETAIL = register(
    "effect.compensation.plan_unavailable",
    "Compensation action specification is not available through this CLI.",
    "Effect CLI refusal",
)
REGISTRY_RETIRE_UNKNOWN_NODE_DETAIL = register(
    "registry.retire.unknown_node",
    "only a registered node can retire; this name has no registry row",
    "Registry retirement refusal",
)
REGISTRY_RETIRE_ALREADY_RETIRED_DETAIL = register(
    "registry.retire.already_retired",
    "this node's registry row is already retired",
    "Registry retirement refusal",
)
DOCTOR_PROFILE_INVALID_DETAIL = register(
    "doctor.profile.invalid",
    "not a ruled doctor profile; ruled profiles are bus-only and orchestration",
    "Doctor CLI refusal",
)
DOCTOR_LIVE_DIRS_EXPECTED_ABSENT_DETAIL = register(
    "doctor.live_dirs.expected_absent",
    "live directories absent, as the bus-only profile expects",
    "Doctor finding row",
)
GH_AUTHENTICATION_REMEDY = register(
    "intake.github.authentication_absent",
    "Floati does not read gh auth login credentials; export GH_TOKEN or GITHUB_TOKEN.",
    "GitHub intake refusal and help",
)
BOARD_EVENT_ROOT_UNAVAILABLE_DETAIL = register(
    "tui.event.root_unavailable",
    "Board event root must be one existing absolute directory",
    "Live Board refusal",
)
BOARD_EVENT_SOURCE_UNSUPPORTED_DETAIL = register(
    "tui.event.source_unsupported",
    "live Board requires kqueue or inotify filesystem events",
    "Live Board refusal",
)
BOARD_EVENT_WATCH_UNAVAILABLE_DETAIL = register(
    "tui.event.watch_unavailable",
    "live Board could not establish complete durable-root event coverage",
    "Live Board refusal",
)

WAKE_DAEMON_INACTIVE_DISPLAY = register(
    "wake.daemon.inactive",
    "wake daemon is inactive; no active consent exists.",
    "Wake daemon lifecycle",
)
WAKE_DAEMON_BOUND_DISPLAY = register(
    "wake.daemon.bound",
    "exact Cursor session binding recorded.",
    "Wake daemon lifecycle",
)
WAKE_DAEMON_GROK_BOUND_DISPLAY = register(
    "wake.daemon.grok_bound",
    f"exact {_GROK_PRODUCT} session binding recorded.",
    "Wake daemon lifecycle",
)
WAKE_DAEMON_CONSENTED_DISPLAY = register(
    "wake.daemon.consented",
    "wake daemon consent is active for this exact coordinate.",
    "Wake daemon lifecycle",
)
WAKE_DAEMON_INSTALLED_DISPLAY = register(
    "wake.daemon.installed",
    "exact wake daemon LaunchAgent is installed but not started.",
    "Wake daemon lifecycle",
)
WAKE_DAEMON_RUNNING_DISPLAY = register(
    "wake.daemon.running",
    "exact wake daemon is running.",
    "Wake daemon lifecycle",
)
WAKE_DAEMON_STOPPED_DISPLAY = register(
    "wake.daemon.stopped",
    "exact wake daemon is stopped.",
    "Wake daemon lifecycle",
)
WAKE_DAEMON_REMOVED_DISPLAY = register(
    "wake.daemon.removed",
    "exact wake daemon LaunchAgent was removed.",
    "Wake daemon lifecycle",
)
WAKE_DAEMON_REVOKED_DISPLAY = register(
    "wake.daemon.revoked",
    "wake daemon consent was revoked.",
    "Wake daemon lifecycle",
)
WAKE_DAEMON_UNKNOWN_DISPLAY = register(
    "wake.daemon.unknown",
    "wake daemon process state is unknown.",
    "Wake daemon lifecycle",
)


def copy_ledger_markdown() -> str:
    from . import brand  # noqa: F401 - import registers brand labels
    from . import graph_render  # noqa: F401 - import registers graph labels
    from . import helptext  # noqa: F401 - import registers the static help bank
    from . import registry  # noqa: F401 - import registers retirement refusal copy
    from . import doctor  # noqa: F401 - import registers doctor profile copy
    from . import replay_render  # noqa: F401 - import registers replay labels
    from . import tui_render  # noqa: F401 - import registers TUI labels

    lines = [
        "# Copy ledger",
        "",
        "Status: `ARCHITECT VOICE PASS 2026-08-28 (wave 3, full DRAFT restamp)` — the catalog below is voice-passed.",
        "Shipped strings carry no provenance marker (voice pass 2026-08-29); a `DRAFT - `",
        "value in this table means copy awaiting restamp, never approved copy.",
        "",
        "Generated from the product-visible string catalog. Function labels are",
        "permitted by `HM1_BRIEF.md`.",
        "",
        "| Key | String | Surface |",
        "| --- | --- | --- |",
    ]
    for key, (value, surface) in sorted(_ENTRIES.items()):
        escaped = value.replace("|", "\\|").replace("\n", "<br>")
        lines.append(f"| `{key}` | {escaped} | {surface} |")
    return "\n".join(lines) + "\n"


def main() -> int:
    print(copy_ledger_markdown(), end="")
    return 0


if __name__ == "__main__":
    from .copy import main as canonical_main

    raise SystemExit(canonical_main())

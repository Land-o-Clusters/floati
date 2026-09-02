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

TUI_DOOR_COPY = {
    "tui.door.hints": register(
        "tui.door.hints",
        "DRAFT - arrows/digits choose · Enter continue · Esc back",
        "TUI onboarding door",
    ),
    "tui.door.node_prompt": register(
        "tui.door.node_prompt", "DRAFT - Node id", "TUI onboarding door"
    ),
    "tui.door.harness_prompt": register(
        "tui.door.harness_prompt", "DRAFT - Harness", "TUI onboarding door"
    ),
    "tui.door.lease_prompt": register(
        "tui.door.lease_prompt", "DRAFT - Lease minutes", "TUI onboarding door"
    ),
    "tui.door.text_input_prefix": register(
        "tui.door.text_input_prefix", "DRAFT - > ", "TUI onboarding door"
    ),
    "tui.door.lifetime_title": register(
        "tui.door.lifetime_title",
        "DRAFT - Choose node lifetime",
        "TUI onboarding door",
    ),
    "tui.door.preview_title": register(
        "tui.door.preview_title",
        "DRAFT - Review exact ledger records",
        "TUI onboarding door",
    ),
    "tui.door.permanent_label": register(
        "tui.door.permanent_label", "DRAFT - Permanent", "TUI onboarding door"
    ),
    "tui.door.permanent_detail": register(
        "tui.door.permanent_detail",
        "DRAFT - No automatic expiry.",
        "TUI onboarding door",
    ),
    "tui.door.temporary_label": register(
        "tui.door.temporary_label", "DRAFT - Temporary", "TUI onboarding door"
    ),
    "tui.door.temporary_detail": register(
        "tui.door.temporary_detail",
        "DRAFT - Requires a bounded lease.",
        "TUI onboarding door",
    ),
    "tui.door.commit_label": register(
        "tui.door.commit_label",
        "DRAFT - Commit these records",
        "TUI onboarding door",
    ),
    "tui.door.commit_detail": register(
        "tui.door.commit_detail",
        "DRAFT - Append the previewed records once.",
        "TUI onboarding door",
    ),
    "tui.door.back_label": register(
        "tui.door.back_label", "DRAFT - Back", "TUI onboarding door"
    ),
    "tui.door.back_detail": register(
        "tui.door.back_detail",
        "DRAFT - Revise the preceding answer.",
        "TUI onboarding door",
    ),
    "tui.door.interactive_terminal_required": register(
        "tui.door.interactive_terminal_required",
        "DRAFT - onboarding doors require interactive stdin and stderr",
        "TUI onboarding door refusal",
    ),
    "tui.door.viewport_too_small": register(
        "tui.door.viewport_too_small",
        "DRAFT - the onboarding frame does not fit the measured terminal viewport",
        "TUI onboarding door refusal",
    ),
    "tui.door.input_closed": register(
        "tui.door.input_closed",
        "DRAFT - onboarding input ended before confirmation",
        "TUI onboarding door refusal",
    ),
    "tui.door.controller_invalid": register(
        "tui.door.controller_invalid",
        "DRAFT - onboarding requires one door controller",
        "TUI onboarding door refusal",
    ),
    "tui.door.solo_fully_flagged_remedy": register(
        "tui.door.solo_fully_flagged_remedy",
        "DRAFT - floati init --root ROOT --solo NODE --harness HARNESS",
        "TUI onboarding door remedy",
    ),
    "tui.door.node_add_fully_flagged_remedy": register(
        "tui.door.node_add_fully_flagged_remedy",
        "DRAFT - floati node add --root ROOT --node NODE --harness HARNESS --lifetime permanent|temporary [--lease-minutes N]",
        "TUI onboarding door remedy",
    ),
    "tui.door.option_invalid": register(
        "tui.door.option_invalid",
        "DRAFT - door options require ids, labels, and details",
        "TUI onboarding door refusal",
    ),
    "tui.door.frame_option_invalid": register(
        "tui.door.frame_option_invalid",
        "DRAFT - door frame requires unique options and one focused option",
        "TUI onboarding door refusal",
    ),
    "tui.door.width_invalid": register(
        "tui.door.width_invalid",
        "DRAFT - onboarding doors require at least 40 terminal columns",
        "TUI onboarding door refusal",
    ),
    "tui.door.color_tier_invalid": register(
        "tui.door.color_tier_invalid",
        "DRAFT - door color tier must be 256, 16, or mono",
        "TUI onboarding door refusal",
    ),
    "tui.door.title_invalid": register(
        "tui.door.title_invalid",
        "DRAFT - door title must be non-empty text",
        "TUI onboarding door refusal",
    ),
    "tui.door.body_invalid": register(
        "tui.door.body_invalid",
        "DRAFT - door body rows must be text",
        "TUI onboarding door refusal",
    ),
    "tui.door.text_invalid": register(
        "tui.door.text_invalid",
        "DRAFT - this onboarding text step requires non-empty text",
        "TUI onboarding door refusal",
    ),
    "tui.door.preview_invalid": register(
        "tui.door.preview_invalid",
        "DRAFT - exact preview can be attached once at the preview step",
        "TUI onboarding door refusal",
    ),
    "tui.door.key_invalid": register(
        "tui.door.key_invalid",
        "DRAFT - door keys must be text",
        "TUI onboarding door refusal",
    ),
    "tui.door.solo_text_invalid": register(
        "tui.door.solo_text_invalid",
        "DRAFT - solo onboarding requires explicit node and harness text",
        "TUI onboarding door refusal",
    ),
    "tui.door.output_invalid": register(
        "tui.door.output_invalid",
        "DRAFT - node add onboarding requires a preview output",
        "TUI onboarding door refusal",
    ),
    "tui.door.preview_required": register(
        "tui.door.preview_required",
        "DRAFT - node add requires the preview decision step",
        "TUI onboarding door refusal",
    ),
    "tui.door.terminal_io_failed": register(
        "tui.door.terminal_io_failed",
        "DRAFT - the onboarding terminal could not complete its I/O lifecycle",
        "TUI onboarding door refusal",
    ),
    "tui.door.cancelled": register(
        "tui.door.cancelled",
        "DRAFT - onboarding was cancelled before commit",
        "TUI onboarding door refusal",
    ),
    "tui.door.solo_flags_conflict": register(
        "tui.door.solo_flags_conflict",
        "DRAFT - no-value --solo does not compose with harness or governance flags",
        "TUI onboarding door refusal",
    ),
    "tui.door.harness_requires_solo": register(
        "tui.door.harness_requires_solo",
        "DRAFT - --harness requires --solo",
        "TUI onboarding door refusal",
    ),
    "tui.door.node_add_shape_invalid": register(
        "tui.door.node_add_shape_invalid",
        "DRAFT - node add requires either no flags or the complete flagged shape",
        "TUI onboarding door refusal",
    ),
    "tui.door.tide_fields_incomplete": register(
        "tui.door.tide_fields_incomplete",
        "DRAFT - optional tide step requires metric, threshold, and action together",
        "TUI onboarding door refusal",
    ),
    "tui.door.node_add_commit_failed_prefix": register(
        "tui.door.node_add_commit_failed_prefix",
        "DRAFT - node add commit: ",
        "TUI onboarding door degraded detail",
    ),
    "tui.door.solo_config_preview": register(
        "tui.door.solo_config_preview",
        "DRAFT - solo configuration exact bytes:",
        "TUI onboarding door preview",
    ),
    "tui.door.solo_registry_preview": register(
        "tui.door.solo_registry_preview",
        "DRAFT - registry entry exact values:",
        "TUI onboarding door preview",
    ),
    "tui.door.solo_authority_preview": register(
        "tui.door.solo_authority_preview",
        "DRAFT - authority grant exact values:",
        "TUI onboarding door preview",
    ),
}


def copy_ledger_markdown() -> str:
    from . import brand  # noqa: F401 - import registers brand labels
    from . import graph_render  # noqa: F401 - import registers graph labels
    from . import helptext  # noqa: F401 - import registers the static help bank
    from . import registry  # noqa: F401 - import registers retirement refusal copy
    from . import doctor  # noqa: F401 - import registers doctor profile copy
    from . import replay_render  # noqa: F401 - import registers replay labels
    from . import tui_doors  # noqa: F401 - import registers TUI door copy consumers
    from . import tui_doctor  # noqa: F401 - import registers doctor TTY copy
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

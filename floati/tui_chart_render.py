"""Pure full-screen rendering for the interactive multi-bus Harbor Map."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping, Optional, Sequence, Tuple

from .tui_chart import HarborHitRegion, HarborPulseFrame, HarborTarget
from .tui_render import (
    DETAIL_LABEL,
    LIVE_MAP_CHANNELS,
    LIVE_MAP_ENVELOPE,
    LIVE_MAP_ESTATE,
    LIVE_MAP_HEADER,
    LIVE_MAP_HINTS,
    LIVE_MAP_LAST,
    LIVE_MAP_LEDGER,
    LIVE_MAP_PIER,
    LIVE_MAP_VESSEL,
    RECEIPTS_HEADER,
    ROLE_LABEL,
    UNKNOWN_LABEL,
    VISIBLE_MAIL_LABEL,
)
from .tui_activity import activity_braille


_RESET = "\x1b[0m"
_COLORS_256 = {
    "structure": "\x1b[38;5;240m",
    "body": "\x1b[38;5;252m",
    "dim": "\x1b[38;5;245m",
    "green": "\x1b[38;5;42m",
    "amber": "\x1b[38;5;214m",
    "cyan": "\x1b[38;5;45m",
    "orange": "\x1b[38;5;208m",
}
_COLORS_16 = {
    "structure": "\x1b[90m",
    "body": "\x1b[97m",
    "dim": "\x1b[37m",
    "green": "\x1b[92m",
    "amber": "\x1b[93m",
    "cyan": "\x1b[96m",
    "orange": "\x1b[93m",
}


@dataclass(frozen=True)
class RenderedHarborMap:
    text: str
    hit_regions: Tuple[HarborHitRegion, ...]
    activity_positions: Mapping[str, tuple[int, int]] = field(default_factory=dict)


def _clip(text: str, width: int) -> str:
    if width <= 0:
        return ""
    if len(text) <= width:
        return text
    if width == 1:
        return "…"
    return text[: width - 1] + "…"


def _sequence(value: object) -> Tuple[Mapping[str, object], ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return ()
    return tuple(item for item in value if isinstance(item, Mapping))


def _age(value: object) -> Optional[int]:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None


def _lamp(value: object) -> tuple[str, str]:
    age = _age(value)
    if age is None:
        return "?", "dim"
    if age <= 60:
        return "●", "green"
    if age <= 300:
        return "◐", "amber"
    return "○", "dim"


def _fact(value: object) -> str:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return str(value)
    if isinstance(value, str) and value:
        return value
    return UNKNOWN_LABEL


def _node(bus: Mapping[str, object], node_id: Optional[str]) -> Optional[Mapping[str, object]]:
    for node in _sequence(bus.get("nodes")):
        if str(node.get("id", "")) == node_id:
            return node
    return None


def _bus(
    artifact: Mapping[str, object], bus_id: str
) -> Optional[Mapping[str, object]]:
    for bus in _sequence(artifact.get("buses")):
        if str(bus.get("bus_id", "")) == bus_id:
            return bus
    return None


def _paint(text: str, color_tier: str) -> str:
    palette = _COLORS_16 if color_tier == "16" else _COLORS_256
    if color_tier == "mono":
        return text

    def ink(fragment: str, name: str) -> str:
        return palette[name] + fragment + _RESET

    painted = text
    painted = painted.replace("⊙", ink("⊙", "orange"))
    painted = painted.replace("●", ink("●", "cyan" if LIVE_MAP_ENVELOPE in text else "green"))
    painted = painted.replace("◐", ink("◐", "amber"))
    painted = painted.replace("○", ink("○", "dim"))
    painted = painted.replace("▶", ink("▶", "cyan"))
    painted = painted.replace("⚑", ink("⚑", "orange"))
    return painted


def _pulse_line(pulse: HarborPulseFrame, width: int) -> str:
    rail = list("─────────")
    position = (0, len(rail) // 2, len(rail) - 1)[pulse.frame_index]
    rail[position] = "●"
    return _clip(
        f"  {pulse.sender} [{pulse.source_bus}] {''.join(rail)}▶ "
        f"{pulse.recipient} [{pulse.target_bus}]  {LIVE_MAP_ENVELOPE} {pulse.event_id}",
        width,
    )


def render_live_harbor_map(
    artifact: Mapping[str, object],
    *,
    selected: HarborTarget,
    detail_open: bool,
    pulses: Sequence[HarborPulseFrame],
    width: int,
    height: int,
    color_tier: str,
    tail_visible: bool = False,
    activity_by_node: Mapping[str, Sequence[int]] | None = None,
) -> RenderedHarborMap:
    """Render one immutable map frame and its exact visible hit regions."""

    width = max(1, int(width))
    height = max(1, int(height))
    buses = _sequence(artifact.get("buses"))
    vessel_count = sum(len(_sequence(bus.get("nodes"))) for bus in buses)
    plain_lines = [
        _clip("⊙ " + LIVE_MAP_HEADER, width),
        _clip("~ ≈ ~ " + "~" * max(0, min(width - 6, 42)), width),
        _clip(
            f"{LIVE_MAP_ESTATE} // {len(buses)} {LIVE_MAP_PIER}S // "
            f"{vessel_count} {LIVE_MAP_VESSEL}S",
            width,
        ),
        "",
    ]
    styles = ["body", "dim", "dim", "body"]
    pending_hits: list[HarborHitRegion] = []
    pending_activity: dict[str, tuple[int, int]] = {}

    def append_target(
        line: str,
        target: HarborTarget,
        style: str,
        activity_target_id: str | None = None,
    ) -> None:
        row = len(plain_lines) + 1
        clipped = _clip(line, width)
        plain_lines.append(clipped)
        styles.append(style)
        pending_hits.append(HarborHitRegion(target, 1, max(1, len(clipped)), row))
        if activity_target_id is not None and activity_by_node is not None:
            glyphs = activity_braille(activity_by_node[activity_target_id])
            column = clipped.find(glyphs) + 1
            if column > 0:
                pending_activity[activity_target_id] = (row, column)

    if detail_open:
        detail_bus = _bus(artifact, selected.bus_id)
        if detail_bus is not None:
            kind_label = LIVE_MAP_PIER if selected.kind == "pier" else LIVE_MAP_VESSEL
            plain_lines.extend(("", _clip(f"{DETAIL_LABEL} // {kind_label} " + (
                selected.node_id or selected.bus_id
            ), width)))
            styles.extend(("body", "body"))
            if selected.kind == "pier":
                plain_lines.append(
                    _clip(
                        f"  {LIVE_MAP_LEDGER}: {_fact(detail_bus.get('ledger_event_count'))}",
                        width,
                    )
                )
                styles.append("dim")
            else:
                detail_node = _node(detail_bus, selected.node_id)
                if detail_node is not None:
                    plain_lines.extend(
                        (
                            _clip(f"  {ROLE_LABEL}: {_fact(detail_node.get('role'))}", width),
                            _clip(
                                f"  {VISIBLE_MAIL_LABEL}: {_fact(detail_node.get('inbox_count'))}",
                                width,
                            ),
                            _clip(
                                f"  {RECEIPTS_HEADER}: {_fact(detail_node.get('receipt_count'))}",
                                width,
                            ),
                        )
                    )
                    styles.extend(("dim", "dim", "dim"))
            plain_lines.append("")
            styles.append("body")

    map_start = len(plain_lines)
    for bus in buses:
        bus_id = str(bus.get("bus_id", UNKNOWN_LABEL))
        target = HarborTarget("pier", bus_id)
        marker = "▶" if target == selected else " "
        lamp, lamp_style = _lamp(bus.get("last_activity_age_seconds"))
        age = _age(bus.get("last_activity_age_seconds"))
        age_text = UNKNOWN_LABEL if age is None else f"{age}s"
        append_target(
            f"╭─ {marker} {lamp} {LIVE_MAP_PIER} {bus_id}  "
            f"{LIVE_MAP_LAST} {age_text} " + "─" * 10,
            target,
            lamp_style,
        )
        architect = str(bus.get("architect_node", ""))
        for node in _sequence(bus.get("nodes")):
            node_id = str(node.get("id", UNKNOWN_LABEL))
            node_target = HarborTarget("vessel", bus_id, node_id)
            node_marker = "▶" if node_target == selected else " "
            node_lamp, node_style = _lamp(node.get("last_activity_age_seconds"))
            flag = " ⚑" if node_id == architect else ""
            role = _fact(node.get("role"))
            activity_target_id = bus_id + "/" + node_id
            activity = ""
            if (
                activity_by_node is not None
                and activity_target_id in activity_by_node
            ):
                activity = "  " + activity_braille(
                    activity_by_node[activity_target_id]
                )
            append_target(
                f"│  {node_marker} {node_lamp} ▤ {LIVE_MAP_VESSEL} "
                f"{node_id}{flag}  {role}{activity}",
                node_target,
                node_style,
                activity_target_id if activity else None,
            )
        plain_lines.append(_clip("╰" + "─" * min(max(1, width - 1), 42), width))
        styles.append("structure")
        plain_lines.append("")
        styles.append("body")

    plain_lines.append(_clip(LIVE_MAP_CHANNELS + " " + "─" * 20, width))
    styles.append("structure")
    if pulses:
        for pulse in pulses:
            plain_lines.append(_pulse_line(pulse, width))
            styles.append("cyan")
    else:
        relationships = _sequence(artifact.get("relationships"))
        if relationships:
            for edge in relationships:
                plain_lines.append(
                    _clip(f"  {edge.get('source', '?')} ─────────▶ {edge.get('target', '?')}", width)
                )
                styles.append("dim")
        else:
            plain_lines.append("  " + UNKNOWN_LABEL)
            styles.append("dim")

    plain_lines.extend(("", _clip(LIVE_MAP_HINTS, width)))
    styles.extend(("body", "dim"))
    selected_region = next(
        (region for region in pending_hits if region.target == selected),
        None,
    )
    scroll_offset = 0
    body_capacity = max(0, height - map_start)
    max_scroll = max(0, len(plain_lines) - map_start - body_capacity)
    if tail_visible:
        scroll_offset = max_scroll
    elif selected_region is not None and selected_region.row > height:
        scroll_offset = min(max_scroll, selected_region.row - height)
    scrolled_plain = plain_lines[:map_start] + plain_lines[map_start + scroll_offset :]
    scrolled_styles = styles[:map_start] + styles[map_start + scroll_offset :]
    visible_plain = scrolled_plain[:height]
    visible_styles = scrolled_styles[:height]
    painted = [
        _paint(line, color_tier) if style != "body" or color_tier != "mono" else line
        for line, style in zip(visible_plain, visible_styles)
    ]
    return RenderedHarborMap(
        "\n".join(painted),
        tuple(
            HarborHitRegion(
                region.target,
                region.left,
                region.right,
                region.row - scroll_offset,
            )
            for region in pending_hits
            if map_start < region.row - scroll_offset <= height
        ),
        {
            target_id: (row - scroll_offset, column)
            for target_id, (row, column) in pending_activity.items()
            if map_start < row - scroll_offset <= height
        },
    )

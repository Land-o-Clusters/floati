"""Pure text and stdlib GIF rendering for Flight-Recorder Cinema."""

from __future__ import annotations

import struct
from typing import Callable, Mapping, Sequence

from .brand import BUOY_MARK, BUOY_ORANGE, RESET, render_buoy_mark
from .tui_activity import activity_braille
from .replay_render import (
    REPLAY_CINEMA_EVENTS,
    REPLAY_CINEMA_FAULT,
    REPLAY_CINEMA_HEADER,
    REPLAY_CINEMA_MAP,
    REPLAY_CINEMA_ROUTE,
    REPLAY_CINEMA_SPEED,
    REPLAY_CINEMA_TIMELINE,
    REPLAY_SUMMARY,
)
from .tui_render import LIVE_MAP_PIER, LIVE_MAP_VESSEL, UNKNOWN_LABEL
from .tui_replay import ReplayCinemaController, ReplayCinemaState


_RESET = "\x1b[0m"
_COLORS_256 = {
    "structure": "\x1b[38;5;240m",
    "body": "\x1b[38;5;252m",
    "dim": "\x1b[38;5;245m",
    "cyan": "\x1b[38;5;45m",
    "orange": BUOY_ORANGE,
    "green": "\x1b[38;5;42m",
    "red": "\x1b[38;5;196m",
}
_COLORS_16 = {
    "structure": "\x1b[90m",
    "body": "\x1b[97m",
    "dim": "\x1b[37m",
    "cyan": "\x1b[96m",
    "orange": "\x1b[93m",
    "green": "\x1b[92m",
    "red": "\x1b[91m",
}


def _clip(text: str, width: int) -> str:
    if width <= 0:
        return ""
    if len(text) <= width:
        return text
    if width == 1:
        return "…"
    return text[: width - 1] + "…"


def _mappings(value: object) -> tuple[Mapping[str, object], ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return ()
    return tuple(item for item in value if isinstance(item, Mapping))


def _paint(line: str, style: str, tier: str) -> str:
    if tier == "mono":
        return line
    palette = _COLORS_16 if tier == "16" else _COLORS_256
    base_style = "structure" if style == "timeline" else style
    base = palette.get(base_style, "")
    if base:
        line = base + line + _RESET
    line = line.replace("◆", palette["orange"] + "◆" + _RESET + base)
    if style == "timeline":
        line = line.replace("x", palette["red"] + "x" + base)
    return line


def _is_fault(event: Mapping[str, object]) -> bool:
    return event.get("event_class") in {"denial", "degradation"} or event.get(
        "record_kind"
    ) in {"worker_refusal", "denial_receipt"}


def _event_actor(event: Mapping[str, object]) -> str | None:
    for key in ("node_id", "claimed_sender", "claimed_recipient"):
        value = event.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _timeline(state: ReplayCinemaState, available: int) -> str:
    markers = []
    for index, event in enumerate(state.all_events, start=1):
        if _is_fault(event):
            markers.append("x")
        elif index == state.visible_count:
            markers.append("●")
        elif index < state.visible_count:
            markers.append("◆")
        else:
            markers.append("○")
    expanded = "├" + "──".join(markers) + "┤"
    if len(expanded) <= available:
        return expanded
    cells = max(1, available - 2)
    rail = ["─"] * cells
    for index, event in enumerate(state.all_events, start=1):
        position = 0 if state.total_count <= 1 else round(
            (cells - 1) * (index - 1) / (state.total_count - 1)
        )
        marker = "x" if _is_fault(event) else (
            "●" if index == state.visible_count else "◆" if index < state.visible_count else "○"
        )
        priority = {"─": 0, "○": 1, "◆": 2, "●": 3, "x": 4}
        if priority[marker] >= priority[rail[position]]:
            rail[position] = marker
    return "├" + "".join(rail) + "┤"


def render_replay_cinema(
    state: ReplayCinemaState,
    *,
    width: int,
    height: int,
    color_tier: str,
    speed: float = 1.0,
    event_line: Callable[[Mapping[str, object], int], str] | None = None,
    activity_by_node: Mapping[str, Sequence[int]] | None = None,
) -> str:
    """Render one event-indexed cinema frame; color never carries unique truth."""

    width = max(1, int(width))
    height = max(1, int(height))
    Row = tuple[str, str]

    def row(value: str = "", style: str = "body") -> Row:
        return _clip(value, width), style

    header = [
        row("⊙ " + REPLAY_CINEMA_HEADER),
        row("~ ≈ ~ " + "~" * max(0, min(width - 6, 42)), "dim"),
        row(f"{REPLAY_CINEMA_MAP} // {REPLAY_CINEMA_SPEED} {speed:g}x", "structure"),
    ]
    fault_actors = {
        actor
        for event in state.all_events
        if _is_fault(event) and (actor := _event_actor(event)) is not None
    }
    if state.fault_node is not None:
        fault_actors.add(state.fault_node)
    current_actor = _event_actor(state.events[-1]) if state.events else None
    map_blocks: list[tuple[int, list[Row]]] = []
    for ordinal, bus in enumerate(state.buses):
        bus_id = str(bus.get("bus_id", UNKNOWN_LABEL))
        architect = bus.get("architect_node")
        nodes = list(_mappings(bus.get("nodes")))
        node_ids = {str(node.get("id", "")) for node in nodes}
        priority = 0 if node_ids & fault_actors else 1 if current_actor in node_ids else 2
        nodes.sort(
            key=lambda node: (
                0 if str(node.get("id", "")) == state.fault_node else
                1 if str(node.get("id", "")) in fault_actors else
                2 if str(node.get("id", "")) == current_actor else 3
            )
        )
        block = [row(f"╭─ ● {LIVE_MAP_PIER} {bus_id} " + "─" * 10, "structure")]
        for node in nodes:
            node_id = str(node.get("id", UNKNOWN_LABEL))
            flag = " ⚑" if node_id == architect else ""
            activity_target_id = bus_id + "/" + node_id
            activity = ""
            if (
                activity_by_node is not None
                and activity_target_id in activity_by_node
            ):
                activity = "  " + activity_braille(
                    activity_by_node[activity_target_id]
                )
            if node_id == state.fault_node:
                code = state.fault_code or UNKNOWN_LABEL
                block.append(
                    row(
                        f"│  x ▤ {LIVE_MAP_VESSEL} {node_id}{flag}  "
                        f"{REPLAY_CINEMA_FAULT} {code}{activity}",
                        "red",
                    )
                )
            else:
                block.append(
                    row(f"│    ▤ {LIVE_MAP_VESSEL} {node_id}{flag}{activity}")
                )
        block.append(row("╰" + "─" * min(max(1, width - 1), 42), "structure"))
        map_blocks.append((priority * 1000 + ordinal, block))
    map_blocks.sort(key=lambda item: item[0])

    route_rows = []
    if state.pulse is not None:
        route_rows.append(
            row(
                f"● {REPLAY_CINEMA_ROUTE} // {state.pulse.sender} "
                f"[{state.pulse.source_bus}] ─────▶ {state.pulse.recipient} "
                f"[{state.pulse.target_bus}]",
                "cyan",
            )
        )
    timeline_rows = [
        row(
            f"{REPLAY_CINEMA_TIMELINE} "
            f"{_timeline(state, max(3, width - len(REPLAY_CINEMA_TIMELINE) - 1))}",
            "timeline",
        ),
        row(f"  {state.visible_count}/{state.total_count} // {state.duration_ms}ms", "dim"),
        row(REPLAY_CINEMA_EVENTS + " " + "─" * 16, "structure"),
    ]
    footer = []
    if state.total_count > 0 and state.visible_count == state.total_count:
        footer.extend(row(line, "orange") for line in render_buoy_mark(color=False).splitlines())
        footer.append(row(REPLAY_SUMMARY, "green"))
    event_capacity = 0 if event_line is None else min(
        len(state.events), max(1, height // 5)
    )
    event_rows = [
        row(event_line(event, width), "red" if _is_fault(event) else "body")
        for event in state.events[-event_capacity:]
    ] if event_line is not None else []
    fixed_count = len(header) + len(route_rows) + len(timeline_rows) + len(event_rows) + len(footer)
    map_capacity = max(0, height - fixed_count)
    map_rows: list[Row] = []
    if not map_blocks and map_capacity:
        map_rows.append(row("  " + UNKNOWN_LABEL, "dim"))
    for _priority, block in map_blocks:
        remaining = map_capacity - len(map_rows)
        if remaining <= 0:
            break
        if len(block) <= remaining:
            map_rows.extend(block)
            continue
        if remaining == 1:
            map_rows.append(block[0])
        elif remaining == 2:
            map_rows.extend((block[0], block[-1]))
        else:
            map_rows.extend(block[: remaining - 2])
            map_rows.append(row("│  …", "dim"))
            map_rows.append(block[-1])
        break
    composed = header + map_rows + route_rows + timeline_rows + event_rows + footer
    painted = [_paint(value, style, color_tier) for value, style in composed]
    return "\n".join(painted[:height])


_GIF_PALETTE = (
    (5, 12, 20),
    (68, 78, 88),
    (218, 225, 231),
    (0, 190, 220),
    (255, 120, 40),
    (40, 205, 110),
    (235, 45, 65),
    (245, 190, 40),
    (24, 36, 48),
    (102, 118, 132),
    (255, 255, 255),
    (16, 92, 120),
    (76, 44, 92),
    (20, 72, 44),
    (90, 28, 32),
    (0, 0, 0),
)


def _rect(
    pixels: list[int], width: int, height: int, x: int, y: int, w: int, h: int, ink: int
) -> None:
    for row in range(max(0, y), min(height, y + h)):
        start = row * width + max(0, x)
        stop = row * width + min(width, x + w)
        pixels[start:stop] = [ink] * max(0, stop - start)


def _line(
    pixels: list[int], width: int, height: int, start: tuple[int, int], end: tuple[int, int], ink: int
) -> None:
    x0, y0 = start
    x1, y1 = end
    dx = abs(x1 - x0)
    sx = 1 if x0 < x1 else -1
    dy = -abs(y1 - y0)
    sy = 1 if y0 < y1 else -1
    error = dx + dy
    while True:
        if 0 <= x0 < width and 0 <= y0 < height:
            pixels[y0 * width + x0] = ink
        if x0 == x1 and y0 == y1:
            break
        doubled = 2 * error
        if doubled >= dy:
            error += dy
            x0 += sx
        if doubled <= dx:
            error += dx
            y0 += sy


def _cinema_pixels(
    state: ReplayCinemaState, width: int, height: int, surface: str
) -> list[int]:
    pixels = [0] * (width * height)
    _rect(pixels, width, height, 0, 0, width, 5, 4)
    node_points: dict[str, tuple[int, int]] = {}
    bus_points: dict[str, tuple[int, int]] = {}
    bus_count = max(1, len(state.buses))
    spacing = max(36, (width - 20) // bus_count)
    for bus_index, bus in enumerate(state.buses):
        x = 10 + bus_index * spacing
        y = 18
        bus_id = str(bus.get("bus_id", ""))
        bus_points[bus_id] = (x + 12, y)
        _rect(pixels, width, height, x, y, 26, 3, 1)
        for node_index, node in enumerate(_mappings(bus.get("nodes"))):
            node_id = str(node.get("id", ""))
            point = (x + 6 + (node_index % 3) * 8, y + 12 + (node_index // 3) * 8)
            node_points[node_id] = point
            ink = 6 if node_id == state.fault_node else 5
            _rect(pixels, width, height, point[0] - 2, point[1] - 2, 5, 5, ink)
    for edge in state.relationships:
        source = bus_points.get(str(edge.get("source", "")))
        target = bus_points.get(str(edge.get("target", "")))
        if source is not None and target is not None:
            _line(pixels, width, height, source, target, 1)
    if state.pulse is not None:
        source = node_points.get(state.pulse.sender)
        target = node_points.get(state.pulse.recipient)
        if source is not None and target is not None:
            _line(pixels, width, height, source, target, 3)
            progress = 0.5 if state.total_count <= 1 else (state.visible_count - 1) / (
                state.total_count - 1
            )
            dot = (
                round(source[0] + (target[0] - source[0]) * progress),
                round(source[1] + (target[1] - source[1]) * progress),
            )
            _rect(pixels, width, height, dot[0] - 2, dot[1] - 2, 5, 5, 4)
    timeline_y = max(6, height - 12)
    _line(pixels, width, height, (8, timeline_y), (width - 9, timeline_y), 1)
    for index, event in enumerate(state.all_events, start=1):
        position = 8 if state.total_count <= 1 else round(
            8 + (width - 17) * (index - 1) / (state.total_count - 1)
        )
        ink = 6 if _is_fault(event) else (4 if index == state.visible_count else 9)
        _rect(pixels, width, height, position - 2, timeline_y - 2, 5, 5, ink)
    _rect(pixels, width, height, 8, height - 6, max(1, width - 16), 2, 8)
    filled = 0 if state.total_count == 0 else round(
        (width - 16) * state.visible_count / state.total_count
    )
    _rect(pixels, width, height, 8, height - 6, filled, 2, 3)
    for row_index, text_row in enumerate(surface.splitlines()[:11]):
        top = 7 + row_index * 6
        if top + 5 >= height - 14:
            break
        for column_index, character in enumerate(text_row[:48]):
            if character == " ":
                continue
            left = 4 + column_index * 3
            if left + 2 >= width:
                break
            code = ord(character) * 2654435761
            ink = 6 if character in {"x", "!"} else 4 if character in {"◆", "●"} else 2
            for glyph_row in range(5):
                for glyph_column in range(2):
                    shift = (glyph_row * 2 + glyph_column) % 24
                    if (code >> shift) & 1:
                        _rect(
                            pixels,
                            width,
                            height,
                            left + glyph_column,
                            top + glyph_row,
                            1,
                            1,
                            ink,
                        )
    return pixels


def _gif_image_data(indices: Sequence[int]) -> bytes:
    clear = 16
    end = 17
    codes = []
    for index in indices:
        codes.extend((clear, int(index)))
    codes.append(end)
    packed = bytearray()
    accumulator = 0
    bit_count = 0
    for code in codes:
        accumulator |= code << bit_count
        bit_count += 5
        while bit_count >= 8:
            packed.append(accumulator & 0xFF)
            accumulator >>= 8
            bit_count -= 8
    if bit_count:
        packed.append(accumulator & 0xFF)
    blocks = bytearray((4,))
    for start in range(0, len(packed), 255):
        block = packed[start : start + 255]
        blocks.append(len(block))
        blocks.extend(block)
    blocks.append(0)
    return bytes(blocks)


def record_replay_cinema_gif(
    artifact: Mapping[str, object],
    *,
    width: int = 160,
    height: int = 90,
    speed: float = 4.0,
) -> bytes:
    """Record the replay's own canonical states as a deterministic GIF89a."""

    controller = ReplayCinemaController(artifact)
    if not controller.events:
        states = (controller.state(0),)
    else:
        states = tuple(controller.state(index) for index in range(1, len(controller.events) + 1))
    result = bytearray(b"GIF89a")
    result.extend(struct.pack("<HHBBB", width, height, 0xF3, 0, 0))
    for red, green, blue in _GIF_PALETTE:
        result.extend((red, green, blue))
    result.extend(b"\x21\xff\x0bNETSCAPE2.0\x03\x01\x00\x00\x00")
    previous_elapsed = 0
    for state in states:
        current = state.events[-1] if state.events else {}
        elapsed = current.get("elapsed_ms")
        elapsed_ms = elapsed if isinstance(elapsed, int) and elapsed >= 0 else 0
        delta = max(50, elapsed_ms - previous_elapsed)
        delay = max(1, min(65535, round(delta / max(0.1, speed) / 10)))
        result.extend(b"\x21\xf9\x04\x04")
        result.extend(struct.pack("<H", delay))
        result.extend(b"\x00\x00")
        result.extend(b"\x2c\x00\x00\x00\x00")
        result.extend(struct.pack("<HH", width, height))
        result.append(0)
        surface = render_replay_cinema(
            state,
            width=100,
            height=30,
            color_tier="mono",
            speed=speed,
            event_line=lambda event, line_width: _clip(
                f"{event.get('sequence', '?')} {event.get('event_class', '?')} "
                f"{event.get('node_id') or '?'}",
                line_width,
            ),
        )
        result.extend(
            _gif_image_data(_cinema_pixels(state, width, height, surface))
        )
        previous_elapsed = elapsed_ms
    result.append(0x3B)
    return bytes(result)

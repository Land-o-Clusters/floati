"""Pure text rendering and paced playback for receipt-ledger replay."""

from __future__ import annotations

import os
import time
from typing import Callable, Mapping, Sequence, TextIO

from .copy import register
from .brand import BUOY_ORANGE, RESET, render_buoy_mark
from .errors import ProtocolRefusal


REPLAY_HEADER = register("replay.header", "FLOATI // FLIGHT RECORDER", "Replay TUI header")
REPLAY_PLAIN_PREFIX = register("replay.plain_prefix", "PLAIN REPLAY // ", "Replay plain-dump mode")
REPLAY_SUMMARY = register("replay.summary", "REPLAY COMPLETE", "Replay completion label")
REPLAY_EVENT_CLAIM = register("replay.event.claim", "CLAIM", "Replay claim event label")
REPLAY_EVENT_TURN = register("replay.event.turn", "TURN", "Replay turn event label")
REPLAY_EVENT_DEGRADED = register("replay.event.degraded", "DEGRADED", "Replay degradation event label")
REPLAY_EVENT_DENIED = register("replay.event.denied", "DENIED", "Replay denial event label")
REPLAY_EVENT_COMPLETE = register("replay.event.complete", "COMPLETE", "Replay completion event label")
REPLAY_SOURCE_WORK = register("replay.source.work", "WORK", "Replay work-ledger source label")
REPLAY_SOURCE_WORKER = register("replay.source.worker", "WORKER", "Replay worker-receipt source label")
REPLAY_SOURCE_REFUSAL = register("replay.source.refusal", "REFUSAL", "Replay refusal-receipt source label")
REPLAY_SOURCE_DENIAL = register("replay.source.denial", "DENIAL", "Replay denial-receipt source label")
REPLAY_SOURCE_EVENT = register("replay.source.event", "EVENT", "Replay fallback source label")
REPLAY_UNIT_EVENTS = register("replay.unit.events", "EVENTS", "Replay event-count unit")
REPLAY_UNIT_MILLISECONDS = register("replay.unit.milliseconds", "MS", "Replay duration unit")
REPLAY_CINEMA_HEADER = register(
    "replay.cinema.header",
    "FLIGHT RECORDER CINEMA",
    "Replay cinema header",
)
REPLAY_CINEMA_MAP = register(
    "replay.cinema.map", "HARBOR MAP", "Replay cinema map panel"
)
REPLAY_CINEMA_TIMELINE = register(
    "replay.cinema.timeline", "TIMELINE", "Replay cinema timeline"
)
REPLAY_CINEMA_SPEED = register(
    "replay.cinema.speed", "SPEED", "Replay cinema speed label"
)
REPLAY_CINEMA_ROUTE = register(
    "replay.cinema.route", "ROUTE", "Replay cinema route pulse"
)
REPLAY_CINEMA_FAULT = register(
    "replay.cinema.fault", "FAULT", "Replay cinema fault label"
)
REPLAY_CINEMA_EVENTS = register(
    "replay.cinema.events", "EVENT STREAM", "Replay cinema event panel"
)


def validate_speed(value: object) -> float:
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not 0.1 <= float(value) <= 100.0
    ):
        raise ProtocolRefusal("replay_speed_invalid", "replay speed must be 0.1 through 100")
    return float(value)


def _clip(value: str, width: int) -> str:
    if len(value) <= width:
        return value
    if width <= 1:
        return "…"[:width]
    return value[: width - 1] + "…"


def _label(event: Mapping[str, object]) -> str:
    event_class = str(event["event_class"])
    return {
        "claim": REPLAY_EVENT_CLAIM,
        "turn": REPLAY_EVENT_TURN,
        "degradation": REPLAY_EVENT_DEGRADED,
        "denial": REPLAY_EVENT_DENIED,
        "completion": REPLAY_EVENT_COMPLETE,
    }[event_class]


def _event_line(event: Mapping[str, object], width: int = 120, *, color: bool = False) -> str:
    elapsed = int(event["elapsed_ms"])
    actor = event.get("node_id") or event.get("claimed_sender") or "?"
    work = event.get("work_item_id") or "-"
    detail = event.get("outcome_code") or event.get("reason_code") or event.get("transition") or "-"
    source = {
        "work_transition": REPLAY_SOURCE_WORK,
        "worker_receipt": REPLAY_SOURCE_WORKER,
        "worker_refusal": REPLAY_SOURCE_REFUSAL,
        "denial_receipt": REPLAY_SOURCE_DENIAL,
    }.get(str(event.get("record_kind")), REPLAY_SOURCE_EVENT)
    rail = {
        "work_transition": "◆",
        "worker_receipt": "│",
        "worker_refusal": "!",
        "denial_receipt": "!",
    }.get(str(event.get("record_kind")), "·")
    if color and rail == "◆":
        rail = BUOY_ORANGE + rail + RESET
    line = f"{rail} +{elapsed / 1000:08.3f}s  {_label(event):<8} {source:<7} {actor} {work} {detail}"
    return _clip(line, width)


def render_replay_plain(artifact: Mapping[str, object], width: int = 120) -> str:
    cached = getattr(artifact, "plain_cache", None)
    if width == 120 and isinstance(cached, str):
        return cached
    events = list(artifact.get("events", []))
    lines = [
        f"{REPLAY_PLAIN_PREFIX}v{artifact.get('replay_schema_version', '?')}",
        *(_event_line(event, width) for event in events),
        f"{REPLAY_SUMMARY} // {len(events)} {REPLAY_UNIT_EVENTS} // "
        f"{int(artifact.get('duration_ms', 0))} {REPLAY_UNIT_MILLISECONDS}",
    ]
    return "\n".join(lines) + "\n"


def render_replay_frame(
    artifact: Mapping[str, object],
    visible_count: int,
    width: int = 120,
    height: int = 40,
) -> str:
    events: Sequence[Mapping[str, object]] = list(artifact.get("events", []))
    visible = list(events[:visible_count])
    total = len(events)
    filled = 20 if total == 0 else round(20 * len(visible) / total)
    lines = [
        _clip(REPLAY_HEADER, width),
        _clip(f"[{'#' * filled}{'.' * (20 - filled)}] {len(visible)}/{total}", width),
        "",
    ]
    footer = []
    if len(visible) == total:
        footer = ["", *render_buoy_mark(color=True).splitlines(), _clip(REPLAY_SUMMARY, width)]
    rail_height = max(1, height - len(lines) - len(footer))
    lines.extend(
        _event_line(event, width, color=True) for event in visible[-rail_height:]
    )
    lines.extend(footer)
    return "\n".join(lines[:height])


def render_replay_cinema_frame(
    artifact: Mapping[str, object],
    visible_count: int,
    width: int = 120,
    height: int = 40,
    *,
    speed: float = 1.0,
    color_tier: str = "256",
    activity: bool = True,
) -> str:
    from .tui_activity import replay_activity
    from .tui_replay import ReplayCinemaController
    from .tui_replay_render import render_replay_cinema

    state = ReplayCinemaController(artifact).state(visible_count)
    return render_replay_cinema(
        state,
        width=width,
        height=height,
        color_tier=color_tier,
        speed=speed,
        event_line=lambda event, line_width: _event_line(
            event, line_width, color=False
        ),
        activity_by_node=replay_activity(state) if activity else None,
    )


def play_replay(
    artifact: Mapping[str, object],
    *,
    speed: object,
    stream: TextIO,
    plain: bool = False,
    sleeper: Callable[[float], None] = time.sleep,
    term: str | None = None,
    terminal_size: os.terminal_size | None = None,
) -> None:
    rate = validate_speed(speed)
    interactive = bool(getattr(stream, "isatty", lambda: False)())
    terminal = os.environ.get("TERM", "") if term is None else term
    if plain or not interactive or terminal == "dumb":
        stream.write(render_replay_plain(artifact))
        stream.flush()
        return
    if terminal_size is None:
        try:
            terminal_size = os.get_terminal_size(stream.fileno())
        except (AttributeError, OSError, ValueError):
            terminal_size = os.terminal_size((120, 40))
    width = max(1, int(terminal_size.columns))
    height = max(1, int(terminal_size.lines))
    events: Sequence[Mapping[str, object]] = list(artifact.get("events", []))
    if os.environ.get("NO_COLOR"):
        color_tier = "mono"
    elif "256color" in terminal:
        color_tier = "256"
    else:
        color_tier = "16"
    if "CI" in os.environ:
        frame = render_replay_cinema_frame(
            artifact,
            len(events),
            width=width,
            height=height,
            speed=rate,
            color_tier=color_tier,
        )
        stream.write("\x1b[?2026h\x1b[H" + frame + "\x1b[J\x1b[?2026l")
        stream.flush()
        return
    previous_elapsed = 0
    for visible_count, event in enumerate(events, start=1):
        elapsed = int(event["elapsed_ms"])
        if visible_count > 1:
            sleeper(max(0.0, elapsed - previous_elapsed) / 1000.0 / rate)
        frame = render_replay_cinema_frame(
            artifact,
            visible_count,
            width=width,
            height=height,
            speed=rate,
            color_tier=color_tier,
        )
        stream.write("\x1b[?2026h\x1b[H" + frame + "\x1b[J\x1b[?2026l")
        stream.flush()
        previous_elapsed = elapsed

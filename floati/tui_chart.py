"""Pure controller state for the interactive multi-bus Harbor Map."""

from __future__ import annotations

import os
import select
import signal
import shutil
import sys
import termios
import time
import tty
from dataclasses import dataclass
from typing import Callable, Mapping, Optional, Sequence, TextIO, Tuple

from .tui_capabilities import probe_terminal_capabilities
from .tui_activity import live_map_activity
from .tui_graphics import kitty_delete_images, plan_activity_overlays
from .tui_protocol import (
    MouseEvent,
    TerminalInput,
    TerminalInputDecoder,
    kitty_keyboard_mode,
    mouse_tracking,
    synchronized_output_frame,
)


PULSE_FRAME_SECONDS = 0.05
PULSE_FRAME_COUNT = 3
PULSE_DURATION_SECONDS = PULSE_FRAME_SECONDS * PULSE_FRAME_COUNT
MAX_ACTIVE_PULSES = 16
MAX_TRACKED_ENVELOPES = 4096


@dataclass(frozen=True)
class HarborTarget:
    kind: str
    bus_id: str
    node_id: Optional[str] = None


@dataclass(frozen=True)
class HarborHitRegion:
    target: HarborTarget
    left: int
    right: int
    row: int

    def contains(self, column: int, row: int) -> bool:
        return self.row == row and self.left <= column <= self.right


@dataclass(frozen=True)
class HarborMapAction:
    kind: str
    target: Optional[HarborTarget] = None


@dataclass(frozen=True)
class HarborPulse:
    event_id: str
    source_bus: str
    sender: str
    target_bus: str
    recipient: str
    started_at: float


@dataclass(frozen=True)
class HarborPulseFrame:
    event_id: str
    source_bus: str
    sender: str
    target_bus: str
    recipient: str
    frame_index: int


@dataclass(frozen=True)
class HarborSnapshotEvent:
    """One upstream-declared state change for the otherwise blocking loop."""

    artifact: Mapping[str, object]


@dataclass(frozen=True)
class HarborResizeEvent:
    """A terminal viewport change delivered through the resize self-pipe."""


class _ResizeWakeup:
    """Turn SIGWINCH into a selectable byte without installing a timer."""

    def __init__(self) -> None:
        self.read_descriptor, self._write_descriptor = os.pipe()
        os.set_blocking(self.read_descriptor, False)
        os.set_blocking(self._write_descriptor, False)
        self._previous_handler: object = None
        self._installed = False
        self._closed = False

    def install(self) -> None:
        self._previous_handler = signal.getsignal(signal.SIGWINCH)
        signal.signal(signal.SIGWINCH, self._handle_signal)
        self._installed = True

    def _handle_signal(self, signum: int, frame: object) -> None:
        del signum, frame
        try:
            os.write(self._write_descriptor, b"r")
        except (BlockingIOError, OSError):
            pass

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._installed:
            signal.signal(signal.SIGWINCH, self._previous_handler)
        os.close(self.read_descriptor)
        os.close(self._write_descriptor)


def _buses(artifact: Mapping[str, object]) -> Tuple[Mapping[str, object], ...]:
    value = artifact.get("buses", ())
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return ()
    return tuple(bus for bus in value if isinstance(bus, Mapping))


def harbor_targets(artifact: Mapping[str, object]) -> Tuple[HarborTarget, ...]:
    targets = []
    for bus in _buses(artifact):
        bus_id = str(bus.get("bus_id", ""))
        if not bus_id:
            continue
        targets.append(HarborTarget("pier", bus_id))
        nodes = bus.get("nodes", ())
        if not isinstance(nodes, Sequence) or isinstance(nodes, (str, bytes)):
            continue
        for node in nodes:
            if not isinstance(node, Mapping):
                continue
            node_id = str(node.get("id", ""))
            if node_id:
                targets.append(HarborTarget("vessel", bus_id, node_id))
    return tuple(targets)


def _envelopes(artifact: Mapping[str, object]) -> Tuple[Mapping[str, object], ...]:
    value = artifact.get("envelopes", ())
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return ()
    return tuple(item for item in value if isinstance(item, Mapping))


def _optional_count(value: object) -> Optional[int]:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None


def normalize_harbor_map_snapshot(
    artifact: Mapping[str, object],
) -> dict[str, object]:
    """Normalize the v0 live seam without reading roots or inventing facts.

    The existing declared-chart producer supplies topology, roles, and bus
    activity. Live producers may additionally supply per-node activity/counts,
    pier ledger counts, and validated envelope facts. Missing enrichment stays
    ``None`` or empty so the renderer degrades to explicit unknowns.
    """

    buses = []
    for bus in _buses(artifact):
        bus_id = bus.get("bus_id")
        architect = bus.get("architect_node")
        if not isinstance(bus_id, str) or not bus_id:
            continue
        if not isinstance(architect, str):
            architect = ""
        nodes = []
        raw_nodes = bus.get("nodes", ())
        if isinstance(raw_nodes, Sequence) and not isinstance(raw_nodes, (str, bytes)):
            for node in raw_nodes:
                if not isinstance(node, Mapping):
                    continue
                node_id = node.get("id")
                role = node.get("role")
                if not isinstance(node_id, str) or not node_id:
                    continue
                nodes.append(
                    {
                        "id": node_id,
                        "role": role if isinstance(role, str) and role else None,
                        "last_activity_age_seconds": _optional_count(
                            node.get("last_activity_age_seconds")
                        ),
                        "inbox_count": _optional_count(node.get("inbox_count")),
                        "receipt_count": _optional_count(node.get("receipt_count")),
                    }
                )
        downstream = bus.get("downstream", ())
        buses.append(
            {
                "bus_id": bus_id,
                "architect_node": architect,
                "last_activity_age_seconds": _optional_count(
                    bus.get("last_activity_age_seconds")
                ),
                "ledger_event_count": _optional_count(bus.get("ledger_event_count")),
                "nodes": nodes,
                "downstream": [
                    value
                    for value in downstream
                    if isinstance(value, str) and value
                ]
                if isinstance(downstream, Sequence)
                and not isinstance(downstream, (str, bytes))
                else [],
            }
        )

    relationships = []
    raw_relationships = artifact.get("relationships", ())
    if isinstance(raw_relationships, Sequence) and not isinstance(
        raw_relationships, (str, bytes)
    ):
        for edge in raw_relationships:
            if not isinstance(edge, Mapping):
                continue
            source = edge.get("source")
            target = edge.get("target")
            if isinstance(source, str) and source and isinstance(target, str) and target:
                relationships.append({"source": source, "target": target})

    envelopes = []
    for item in _envelopes(artifact)[-MAX_TRACKED_ENVELOPES:]:
        fields = {
            key: item.get(key)
            for key in ("id", "source_bus", "sender", "target_bus", "recipient")
        }
        if all(isinstance(value, str) and value for value in fields.values()):
            envelopes.append(fields)

    source = artifact.get("source")
    return {
        "schema_version": 0,
        "source": source if isinstance(source, str) and source else "declared_snapshot",
        "buses": buses,
        "relationships": relationships,
        "envelopes": envelopes,
    }


class LiveHarborMapController:
    """Track focus and bounded, event-sourced motion without touching a bus."""

    def __init__(self, artifact: Mapping[str, object]) -> None:
        self.artifact = normalize_harbor_map_snapshot(artifact)
        self._targets = harbor_targets(self.artifact)
        self._selected = 0
        self.detail_open = False
        self.tail_visible = False
        self.quit_requested = False
        self._seen_envelopes = {
            str(item.get("id"))
            for item in _envelopes(self.artifact)[-MAX_TRACKED_ENVELOPES:]
            if isinstance(item.get("id"), str) and item.get("id")
        }
        self._pulses: list[HarborPulse] = []

    @property
    def selected_target(self) -> HarborTarget:
        if self._targets:
            return self._targets[self._selected]
        return HarborTarget("pier", "")

    def update(self, artifact: Mapping[str, object], *, observed_at: float) -> bool:
        artifact = normalize_harbor_map_snapshot(artifact)
        previous_target = self.selected_target
        changed = artifact != self.artifact
        self.active_pulses(observed_at)
        envelope_window = _envelopes(artifact)[-MAX_TRACKED_ENVELOPES:]
        previously_seen = self._seen_envelopes
        for item in envelope_window:
            event_id = item.get("id")
            if not isinstance(event_id, str) or not event_id or event_id in previously_seen:
                continue
            fields = tuple(item.get(key) for key in (
                "source_bus", "sender", "target_bus", "recipient"
            ))
            if not all(isinstance(value, str) and value for value in fields):
                continue
            self._pulses.append(
                HarborPulse(event_id, fields[0], fields[1], fields[2], fields[3], observed_at)
            )
            self._pulses = self._pulses[-MAX_ACTIVE_PULSES:]
            changed = True
        self._seen_envelopes = {
            str(item.get("id"))
            for item in envelope_window
            if isinstance(item.get("id"), str) and item.get("id")
        }
        self.artifact = artifact
        self._targets = harbor_targets(artifact)
        if previous_target in self._targets:
            self._selected = self._targets.index(previous_target)
        else:
            self._selected = min(self._selected, max(0, len(self._targets) - 1))
            self.detail_open = False
            self.tail_visible = False
        return changed

    def settle_pulses(self) -> None:
        self._pulses = []

    def active_pulses(self, now: float) -> Tuple[HarborPulseFrame, ...]:
        self._pulses = [
            pulse
            for pulse in self._pulses
            if now - pulse.started_at < PULSE_DURATION_SECONDS
        ]
        frames = []
        for pulse in self._pulses:
            elapsed = max(0.0, now - pulse.started_at)
            frame_index = min(
                PULSE_FRAME_COUNT - 1,
                int((elapsed + 1e-9) / PULSE_FRAME_SECONDS),
            )
            frames.append(
                HarborPulseFrame(
                    pulse.event_id,
                    pulse.source_bus,
                    pulse.sender,
                    pulse.target_bus,
                    pulse.recipient,
                    frame_index,
                )
            )
        return tuple(frames)

    def next_wakeup(self, now: float) -> Optional[float]:
        frames = self.active_pulses(now)
        if not frames:
            return None
        deadlines = []
        by_id = {pulse.event_id: pulse for pulse in self._pulses}
        for frame in frames:
            pulse = by_id[frame.event_id]
            boundary = pulse.started_at + (frame.frame_index + 1) * PULSE_FRAME_SECONDS
            deadlines.append(min(boundary, pulse.started_at + PULSE_DURATION_SECONDS))
        return min(deadlines)

    def handle_key(self, key: str) -> HarborMapAction:
        if key in ("j", "KEY_DOWN", "\x1b[B"):
            if self._targets:
                if self._selected == len(self._targets) - 1:
                    self.tail_visible = True
                else:
                    self._selected += 1
                    self.tail_visible = False
            self.detail_open = False
            return HarborMapAction("select", self.selected_target)
        if key in ("k", "KEY_UP", "\x1b[A"):
            if self.tail_visible:
                self.tail_visible = False
            elif self._targets:
                self._selected = max(0, self._selected - 1)
            self.detail_open = False
            return HarborMapAction("select", self.selected_target)
        if key in ("ENTER", "\r", "\n"):
            self.tail_visible = False
            self.detail_open = not self.detail_open
            return HarborMapAction("detail", self.selected_target)
        if key == "\x1b" and self.detail_open:
            self.detail_open = False
            return HarborMapAction("detail", self.selected_target)
        if key == "q":
            self.quit_requested = True
            return HarborMapAction("quit", self.selected_target)
        return HarborMapAction("none", self.selected_target)

    def handle_mouse(
        self,
        event: MouseEvent,
        *,
        hit_regions: Sequence[HarborHitRegion],
    ) -> HarborMapAction:
        if not event.pressed or event.button != 0:
            return HarborMapAction("none", self.selected_target)
        for region in hit_regions:
            if region.contains(event.column, event.row) and region.target in self._targets:
                self._selected = self._targets.index(region.target)
                self.tail_visible = False
                self.detail_open = True
                return HarborMapAction("detail", region.target)
        return HarborMapAction("none", self.selected_target)


def _read_terminal_event(
    stream: TextIO,
    timeout: Optional[float],
    decoder: TerminalInputDecoder,
    pending: list[TerminalInput],
    *,
    resize_descriptor: Optional[int] = None,
) -> object:
    if pending:
        return pending.pop(0)
    descriptor = stream.fileno()
    descriptors = [descriptor]
    if resize_descriptor is not None:
        descriptors.append(resize_descriptor)
    readable, _, _ = select.select(descriptors, [], [], timeout)
    if not readable:
        return ""
    if resize_descriptor is not None and resize_descriptor in readable:
        try:
            os.read(resize_descriptor, 4096)
        except BlockingIOError:
            pass
        return HarborResizeEvent()
    pending.extend(decoder.feed(os.read(descriptor, 64)))
    return "" if not pending else pending.pop(0)


def _color_tier() -> str:
    if os.environ.get("NO_COLOR"):
        return "mono"
    term = os.environ.get("TERM", "")
    colorterm = os.environ.get("COLORTERM", "")
    if "256color" in term or colorterm.casefold() in {"truecolor", "24bit"}:
        return "256"
    return "16"


def run_live_harbor_map(
    *,
    snapshot_loader: Callable[[], Mapping[str, object]],
    input_stream: TextIO = sys.stdin,
    output_stream: TextIO = sys.stdout,
    read_event: Optional[Callable[[Optional[float]], object]] = None,
    capability_receipt_sink: Optional[
        Callable[[Mapping[str, object]], None]
    ] = None,
    terminal_response: Optional[bytes] = None,
    color_tier: Optional[str] = None,
    settled_frames: bool = False,
) -> int:
    """Run the live map without owning discovery, roots, or CLI dispatch."""

    from .multi_bus_chart import render_multi_bus_chart
    from .tui_chart_render import render_live_harbor_map

    initial = snapshot_loader()
    interactive = bool(getattr(input_stream, "isatty", lambda: False)()) and bool(
        getattr(output_stream, "isatty", lambda: False)()
    )
    if not interactive or os.environ.get("TERM") == "dumb":
        output_stream.write(render_multi_bus_chart(initial))
        output_stream.flush()
        return 0

    descriptor = input_stream.fileno()
    prior_termios = termios.tcgetattr(descriptor)
    controller = LiveHarborMapController(initial)
    tier = _color_tier() if color_tier is None else color_tier
    settled = settled_frames or "CI" in os.environ
    decoder = TerminalInputDecoder()
    pending: list[TerminalInput] = []
    previous_text: Optional[str] = None
    previous_pulses: Tuple[HarborPulseFrame, ...] = ()
    previous_size: Optional[os.terminal_size] = None
    current_hits: Tuple[HarborHitRegion, ...] = ()
    needs_render = True
    screen_entered = False
    cbreak_attempted = False
    mouse_enabled = False
    kitty_keyboard_enabled = False
    resize_wakeup: Optional[_ResizeWakeup] = None
    activity_overlays = ()
    activity_image_ids: set[int] = set()
    try:
        screen_entered = True
        output_stream.write("\x1b[?1049h\x1b[?25l")
        output_stream.flush()
        cbreak_attempted = True
        tty.setcbreak(descriptor)
        capability_receipt, remainder = probe_terminal_capabilities(
            input_stream,
            output_stream,
            environment=os.environ,
            preloaded_response=(
                b""
                if read_event is not None and terminal_response is None
                else terminal_response
            ),
        )
        pending.extend(decoder.feed(remainder))
        if capability_receipt_sink is not None:
            capability_receipt_sink(capability_receipt.to_dict())
        mouse_enabled = True
        output_stream.write(mouse_tracking(True).decode("ascii"))
        if capability_receipt.enabled("kitty_keyboard"):
            kitty_keyboard_enabled = True
            output_stream.write(kitty_keyboard_mode(True).decode("ascii"))
        output_stream.flush()
        if read_event is None:
            resize_wakeup = _ResizeWakeup()
            resize_wakeup.install()
        while not controller.quit_requested:
            now = time.monotonic()
            if settled:
                controller.settle_pulses()
                pulses: Tuple[HarborPulseFrame, ...] = ()
            else:
                pulses = controller.active_pulses(now)
            if pulses != previous_pulses:
                needs_render = True
            size = shutil.get_terminal_size((120, 40))
            if size != previous_size:
                needs_render = True
            if needs_render:
                activity = live_map_activity(controller.artifact)
                rendered = render_live_harbor_map(
                    controller.artifact,
                    selected=controller.selected_target,
                    detail_open=controller.detail_open,
                    pulses=pulses,
                    width=size.columns,
                    height=size.lines,
                    color_tier=tier,
                    tail_visible=controller.tail_visible,
                    activity_by_node=activity,
                )
                current_hits = rendered.hit_regions
                overlay_plan = plan_activity_overlays(
                    activity_by_target=activity,
                    visible_positions=rendered.activity_positions,
                    capability_receipt=capability_receipt,
                    color_tier=tier,
                    previous=activity_overlays,
                )
                if rendered.text != previous_text or overlay_plan.payload:
                    output_stream.write(
                        synchronized_output_frame(
                            rendered.text,
                            image=overlay_plan.payload,
                        ).decode("utf-8")
                    )
                    output_stream.flush()
                    previous_text = rendered.text
                    activity_image_ids.update(
                        overlay.image_id for overlay in overlay_plan.overlays
                    )
                    activity_overlays = overlay_plan.overlays
                previous_pulses = pulses
                previous_size = size
                needs_render = False
            deadline = None if settled else controller.next_wakeup(now)
            timeout = None if deadline is None else max(0.0, deadline - now)
            event = (
                read_event(timeout)
                if read_event is not None
                else _read_terminal_event(
                    input_stream,
                    timeout,
                    decoder,
                    pending,
                    resize_descriptor=(
                        None if resize_wakeup is None else resize_wakeup.read_descriptor
                    ),
                )
            )
            if isinstance(event, HarborSnapshotEvent):
                if controller.update(event.artifact, observed_at=time.monotonic()):
                    needs_render = True
                continue
            if isinstance(event, HarborResizeEvent):
                needs_render = True
                continue
            if isinstance(event, MouseEvent):
                action = controller.handle_mouse(event, hit_regions=current_hits)
            else:
                action = controller.handle_key(event)
            if action.kind not in {"none", "quit"}:
                needs_render = True
        return 0
    finally:
        primary_error = sys.exc_info()[1]
        restore_error: Optional[BaseException] = None
        cleanup_error: Optional[BaseException] = None
        resize_error: Optional[BaseException] = None
        if resize_wakeup is not None:
            try:
                resize_wakeup.close()
            except BaseException as exc:
                resize_error = exc
        if cbreak_attempted:
            try:
                termios.tcsetattr(descriptor, termios.TCSADRAIN, prior_termios)
            except BaseException as exc:
                restore_error = exc
        try:
            if activity_image_ids:
                output_stream.write(
                    kitty_delete_images(sorted(activity_image_ids)).decode("ascii")
                )
            if kitty_keyboard_enabled:
                output_stream.write(kitty_keyboard_mode(False).decode("ascii"))
            if mouse_enabled:
                output_stream.write(mouse_tracking(False).decode("ascii"))
            if screen_entered:
                output_stream.write("\x1b[?25h\x1b[?1049l")
            output_stream.flush()
        except BaseException as exc:
            cleanup_error = exc
        if primary_error is None:
            if restore_error is not None:
                raise restore_error
            if resize_error is not None:
                raise resize_error
            if cleanup_error is not None:
                raise cleanup_error

"""Pure node-onboarding door state and full-card rendering.

This module deliberately owns no terminal and no CLI routing.  A caller supplies
text answers and feeds decoded key or pointer events into the controller.
"""

from __future__ import annotations

import os
import re
import select
import sys
import termios
import tty
import unicodedata
from dataclasses import dataclass
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    Dict,
    Iterable,
    Optional,
    Sequence,
    TextIO,
    Tuple,
    Union,
)

from .errors import DurabilityFailure, ProtocolRefusal
from .copy import TUI_DOOR_COPY
from .tui_choice import ChoiceAction, ChoiceFocusController
from .tui_capabilities import probe_terminal_capabilities
from .tui_protocol import (
    MouseEvent,
    TerminalInput,
    TerminalInputDecoder,
    kitty_keyboard_mode,
    mouse_tracking,
    synchronized_output_frame,
)

if TYPE_CHECKING:
    from .node_wizard import NodeAddPlan, NodeWizard
    from .solo import SoloInitPlan


_DOOR_HINTS = TUI_DOOR_COPY["tui.door.hints"]
_NODE_PROMPT = TUI_DOOR_COPY["tui.door.node_prompt"]
_HARNESS_PROMPT = TUI_DOOR_COPY["tui.door.harness_prompt"]
_LEASE_PROMPT = TUI_DOOR_COPY["tui.door.lease_prompt"]
_TEXT_INPUT_PREFIX = TUI_DOOR_COPY["tui.door.text_input_prefix"]
_LIFETIME_TITLE = TUI_DOOR_COPY["tui.door.lifetime_title"]
_PREVIEW_TITLE = TUI_DOOR_COPY["tui.door.preview_title"]
_PERMANENT_LABEL = TUI_DOOR_COPY["tui.door.permanent_label"]
_PERMANENT_DETAIL = TUI_DOOR_COPY["tui.door.permanent_detail"]
_TEMPORARY_LABEL = TUI_DOOR_COPY["tui.door.temporary_label"]
_TEMPORARY_DETAIL = TUI_DOOR_COPY["tui.door.temporary_detail"]
_COMMIT_LABEL = TUI_DOOR_COPY["tui.door.commit_label"]
_COMMIT_DETAIL = TUI_DOOR_COPY["tui.door.commit_detail"]
_BACK_LABEL = TUI_DOOR_COPY["tui.door.back_label"]
_BACK_DETAIL = TUI_DOOR_COPY["tui.door.back_detail"]

_FOCUS_256 = "\x1b[38;5;208m"
_FOCUS_16 = "\x1b[93m"
_RESET = "\x1b[0m"
_SGR_CONTROL = re.compile(r"\x1b\[[0-9;]*m")

_SOLO_FULLY_FLAGGED_REMEDY = TUI_DOOR_COPY[
    "tui.door.solo_fully_flagged_remedy"
]
_NODE_ADD_FULLY_FLAGGED_REMEDY = TUI_DOOR_COPY[
    "tui.door.node_add_fully_flagged_remedy"
]
_INTERACTIVE_TERMINAL_DETAIL = (
    TUI_DOOR_COPY["tui.door.interactive_terminal_required"]
)


class DoorTerminalIOError(RuntimeError):
    """A failure confined to the guarded terminal transport lifecycle."""


def _terminal_io(operation: Callable[[], Any]) -> Any:
    try:
        return operation()
    except OSError as exc:
        raise DoorTerminalIOError(str(exc)) from exc


@dataclass(frozen=True)
class DoorOption:
    option_id: str
    label: str
    detail: str

    def __post_init__(self) -> None:
        if not all(
            isinstance(value, str) and value
            for value in (self.option_id, self.label, self.detail)
        ):
            raise ProtocolRefusal(
                "door_option_invalid", TUI_DOOR_COPY["tui.door.option_invalid"]
            )


@dataclass(frozen=True)
class DoorHitRegion:
    option_id: str
    x: int
    y: int
    width: int
    height: int

    def contains(self, x: int, y: int) -> bool:
        return self.x <= x < self.x + self.width and self.y <= y < self.y + self.height


@dataclass(frozen=True)
class DoorFrame:
    text: str
    hit_regions: Tuple[DoorHitRegion, ...]
    screen_id: str = "choice"
    generation: Optional[object] = None
    viewport: Optional[Tuple[int, int]] = None

    def hit_test(self, x: int, y: int) -> Optional[str]:
        for region in self.hit_regions:
            if region.contains(x, y):
                return region.option_id
        return None


def _validate_frame(
    options: Sequence[DoorOption], focused_option_id: str, width: int, color_tier: str
) -> None:
    ids = tuple(option.option_id for option in options)
    if width < 40:
        raise ProtocolRefusal(
            "door_width_invalid", TUI_DOOR_COPY["tui.door.width_invalid"]
        )
    if color_tier not in {"256", "16", "mono"}:
        raise ProtocolRefusal(
            "door_color_tier_invalid",
            TUI_DOOR_COPY["tui.door.color_tier_invalid"],
        )
    if not ids or len(ids) != len(set(ids)) or focused_option_id not in ids:
        raise ProtocolRefusal(
            "door_option_invalid",
            TUI_DOOR_COPY["tui.door.frame_option_invalid"],
        )


def render_door_frame(
    title: str,
    options: Sequence[DoorOption],
    *,
    focused_option_id: str,
    width: int,
    color_tier: str,
    body_lines: Iterable[str] = (),
    screen_id: str = "choice",
    generation: Optional[object] = None,
    height: Optional[int] = None,
) -> DoorFrame:
    """Render a choice surface whose hit map covers every card cell exactly."""
    _validate_frame(options, focused_option_id, width, color_tier)
    if not isinstance(title, str) or not title:
        raise ProtocolRefusal(
            "door_title_invalid", TUI_DOOR_COPY["tui.door.title_invalid"]
        )
    inner_width = width - 2
    rows = [title[:width].ljust(width)]
    body = tuple(body_lines)
    for line in body:
        if not isinstance(line, str):
            raise ProtocolRefusal(
                "door_body_invalid", TUI_DOOR_COPY["tui.door.body_invalid"]
            )
        for start in range(0, max(1, len(line)), inner_width):
            rows.append((line[start : start + inner_width]).ljust(width))
    if body:
        rows.append("".ljust(width))
    regions = []
    focus_color = _FOCUS_256 if color_tier == "256" else _FOCUS_16
    for index, option in enumerate(options, start=1):
        focused = option.option_id == focused_option_id
        y = len(rows)
        if focused:
            top, side, bottom, marker, label = "╔", "║", "╚", "▶", option.label.upper()
            horizontal = "═"
        else:
            top, side, bottom, marker, label = "┌", "│", "└", " ", option.label
            horizontal = "─"
        lines = [
            top + horizontal * (width - 2) + ("╗" if focused else "┐"),
            side + f" {marker} {index}  {label}"[:inner_width].ljust(inner_width) + side,
            side + f"     {option.detail}"[:inner_width].ljust(inner_width) + side,
            bottom + horizontal * (width - 2) + ("╝" if focused else "┘"),
        ]
        if focused and color_tier != "mono":
            lines = [focus_color + line + _RESET for line in lines]
        rows.extend(lines)
        regions.append(DoorHitRegion(option.option_id, 0, y, width, len(lines)))
    rows.append(_DOOR_HINTS[:width].ljust(width))
    if height is not None and (height < 1 or len(rows) > height):
        raise ProtocolRefusal(
            "door_viewport_too_small",
            TUI_DOOR_COPY["tui.door.viewport_too_small"],
        )
    return DoorFrame(
        "\n".join(rows),
        tuple(regions),
        screen_id,
        generation,
        None if height is None else (width, height),
    )


class DoorController:
    """One-decision-per-screen node-add state with no mutation capability."""

    _TEXT_STEPS = ("node", "harness", "lease")

    def __init__(self, *, flow: str = "node_add") -> None:
        self._flow = flow
        self._step = "node"
        self._values: dict[str, str] = {}
        self._choice: Optional[ChoiceFocusController] = None
        self._preview_plan: Optional[Any] = None
        self._preview_text = ""
        self._committed = False
        self._commit_result: Optional[Dict[str, Any]] = None
        self._rendered_frame: Optional[DoorFrame] = None
        self._rendered_generation: Optional[object] = None

    @classmethod
    def node_add(cls) -> "DoorController":
        return cls()

    @classmethod
    def solo(cls) -> "DoorController":
        return cls(flow="solo")

    @property
    def step(self) -> str:
        return self._step

    @property
    def committed(self) -> bool:
        return self._committed

    @property
    def focused_option_id(self) -> Optional[str]:
        return None if self._choice is None else self._choice.focused_option_id

    @property
    def values(self) -> Tuple[str, ...]:
        if self._flow == "solo":
            return (
                self._values.get("node", ""),
                self._values.get("harness", ""),
            )
        values = (
            self._values.get("node", ""),
            self._values.get("harness", ""),
            self._values.get("lifetime", ""),
        )
        return values + ((self._values["lease"],) if values[2] == "temporary" else ())

    def submit_text(self, value: str) -> ChoiceAction:
        if (
            self._step not in self._TEXT_STEPS
            or not isinstance(value, str)
            or not value.strip()
        ):
            raise ProtocolRefusal(
                "door_text_invalid", TUI_DOOR_COPY["tui.door.text_invalid"]
            )
        self._values[self._step] = value
        if self._step == "node":
            self._step = "harness"
        elif self._step == "harness":
            if self._flow == "solo":
                self._step = "preview"
                self._set_choices(("commit", "back"), "commit")
            else:
                self._step = "lifetime"
                self._set_choices(("permanent", "temporary"), "permanent")
        else:
            self._step = "preview"
            self._set_choices(("commit", "back"), "commit")
        self._invalidate_rendered_frame()
        return ChoiceAction("advanced", self._step)

    def attach_preview(self, plan: Any, preview_text: str) -> None:
        if (
            self._step != "preview"
            or self._preview_plan is not None
            or not isinstance(preview_text, str)
        ):
            raise ProtocolRefusal(
                "door_preview_invalid",
                TUI_DOOR_COPY["tui.door.preview_invalid"],
            )
        self._preview_plan = plan
        self._preview_text = preview_text
        self._invalidate_rendered_frame()

    def preview_plan(self) -> Optional[Any]:
        return self._preview_plan

    def handle_key(self, key: str) -> ChoiceAction:
        if not isinstance(key, str):
            raise ProtocolRefusal(
                "door_key_invalid", TUI_DOOR_COPY["tui.door.key_invalid"]
            )
        if key in {"ESC", "\x1b"}:
            return self._back()
        if self._choice is None:
            return ChoiceAction("none")
        if key in {"ENTER", "\r", "\n"}:
            return self._activate(self._choice.focused_option_id)
        action = self._choice.handle_key(key)
        if action.kind == "focused":
            self._invalidate_rendered_frame()
        return action

    def handle_pointer(
        self, frame: DoorFrame, x: int, y: int, *, activate: bool
    ) -> ChoiceAction:
        if (
            frame.screen_id != self._step
            or self._choice is None
            or frame is not self._rendered_frame
            or frame.generation is None
            or frame.generation is not self._rendered_generation
        ):
            return ChoiceAction("none")
        option_id = frame.hit_test(x, y)
        if option_id is None:
            return ChoiceAction("none")
        action = self._choice.handle_pointer(option_id, activate=activate)
        if action.kind == "activated":
            return self._activate(option_id)
        self._invalidate_rendered_frame()
        return action

    def render(
        self,
        *,
        width: int = 72,
        height: Optional[int] = None,
        color_tier: str = "mono",
    ) -> DoorFrame:
        generation = object()
        if self._step == "node":
            frame = DoorFrame(
                _NODE_PROMPT[:width].ljust(width),
                (),
                self._step,
                generation,
                None if height is None else (width, height),
            )
        elif self._step == "harness":
            frame = DoorFrame(
                _HARNESS_PROMPT[:width].ljust(width),
                (),
                self._step,
                generation,
                None if height is None else (width, height),
            )
        elif self._step == "lease":
            frame = DoorFrame(
                _LEASE_PROMPT[:width].ljust(width),
                (),
                self._step,
                generation,
                None if height is None else (width, height),
            )
        elif self._step == "lifetime":
            frame = render_door_frame(
                _LIFETIME_TITLE,
                (
                    DoorOption("permanent", _PERMANENT_LABEL, _PERMANENT_DETAIL),
                    DoorOption("temporary", _TEMPORARY_LABEL, _TEMPORARY_DETAIL),
                ),
                focused_option_id=self._choice.focused_option_id,
                width=width,
                color_tier=color_tier,
                screen_id=self._step,
                generation=generation,
                height=height,
            )
        else:
            frame = render_door_frame(
                _PREVIEW_TITLE,
                (
                    DoorOption("commit", _COMMIT_LABEL, _COMMIT_DETAIL),
                    DoorOption("back", _BACK_LABEL, _BACK_DETAIL),
                ),
                focused_option_id=self._choice.focused_option_id,
                width=width,
                color_tier=color_tier,
                body_lines=self._preview_text.splitlines(),
                screen_id=self._step,
                generation=generation,
                height=height,
            )
        self._rendered_frame = frame
        self._rendered_generation = generation
        return frame

    def _set_choices(self, options: Sequence[str], initial: str) -> None:
        self._choice = ChoiceFocusController(options, initial_option_id=initial)

    def _invalidate_rendered_frame(self) -> None:
        self._rendered_frame = None
        self._rendered_generation = None

    def _activate(self, option_id: str) -> ChoiceAction:
        if self._step == "lifetime":
            self._values["lifetime"] = option_id
            if option_id == "temporary":
                self._step = "lease"
                self._choice = None
            else:
                self._step = "preview"
                self._set_choices(("commit", "back"), "commit")
            self._invalidate_rendered_frame()
            return ChoiceAction("advanced", option_id)
        if self._step == "preview" and option_id == "commit":
            if self._preview_plan is None:
                return ChoiceAction("preview_required", option_id)
            self._committed = True
            self._invalidate_rendered_frame()
            return ChoiceAction("committed", option_id)
        if self._step == "preview" and option_id == "back":
            return self._back()
        return ChoiceAction("none")

    def _back(self) -> ChoiceAction:
        if self._step == "preview":
            self._preview_plan = None
            self._preview_text = ""
            if self._flow == "solo":
                self._step = "harness"
                self._choice = None
            elif self._values.get("lifetime") == "temporary":
                self._step = "lease"
                self._choice = None
            else:
                self._step = "lifetime"
                self._set_choices(("permanent", "temporary"), "permanent")
        elif self._step == "lease":
            self._step = "lifetime"
            self._set_choices(("permanent", "temporary"), "temporary")
        elif self._step == "lifetime":
            self._step = "harness"
            self._choice = None
        elif self._step == "harness":
            self._step = "node"
        else:
            return ChoiceAction("none")
        self._committed = False
        self._commit_result = None
        self._invalidate_rendered_frame()
        return ChoiceAction("back", self._step)


def _interactive(stream: TextIO) -> bool:
    return bool(getattr(stream, "isatty", lambda: False)())


def require_door_terminal(
    input_stream: TextIO,
    output_stream: TextIO,
    *,
    remedy: str = _NODE_ADD_FULLY_FLAGGED_REMEDY,
) -> None:
    """Refuse an interactive-only shape before any caller resolves or mutates a root."""
    interactive = _terminal_io(
        lambda: _interactive(input_stream) and _interactive(output_stream)
    )
    if not interactive or os.environ.get("TERM") == "dumb":
        raise ProtocolRefusal(
            "interactive_terminal_required",
            _INTERACTIVE_TERMINAL_DETAIL,
            remedy,
        )


def _read_terminal_event(
    stream: TextIO,
    decoder: TerminalInputDecoder,
    pending: list[TerminalInput],
) -> TerminalInput:
    if pending:
        return pending.pop(0)
    descriptor = _terminal_io(stream.fileno)
    while True:
        if decoder.has_standalone_escape():
            readable, _, _ = _terminal_io(
                lambda: select.select([descriptor], [], [], 0.03)
            )
            if not readable:
                pending.extend(decoder.resolve_standalone_escape())
                if pending:
                    return pending.pop(0)
                continue
        else:
            _terminal_io(lambda: select.select([descriptor], [], []))
        raw = _terminal_io(lambda: os.read(descriptor, 64))
        if not raw:
            raise ProtocolRefusal(
                "door_input_closed",
                TUI_DOOR_COPY["tui.door.input_closed"],
            )
        pending.extend(decoder.feed(raw))
        if pending:
            return pending.pop(0)


def _write_cleanup(
    operation: Callable[[], object], errors: list[BaseException]
) -> None:
    try:
        operation()
    except BaseException as exc:
        errors.append(exc)


def _write_and_flush(stream: TextIO, value: str) -> None:
    def write() -> None:
        written = stream.write(value)
        if written is not None and written != len(value):
            raise OSError("terminal write ended before the control frame was complete")
        stream.flush()

    _terminal_io(write)


def _is_activation_event(event: TerminalInput) -> bool:
    if isinstance(event, MouseEvent):
        return event.button == 0 and event.pressed
    return isinstance(event, str) and event in {"ENTER", "\r", "\n"}


def _discard_queued_activations(pending: list[TerminalInput]) -> None:
    pending[:] = [event for event in pending if not _is_activation_event(event)]


def _discard_viewport_bound_events(pending: list[TerminalInput]) -> None:
    pending[:] = [
        event
        for event in pending
        if not isinstance(event, MouseEvent) and not _is_activation_event(event)
    ]


def _discard_stale_queued_activations(
    pending: list[TerminalInput], prior_step: str, current_step: str
) -> None:
    if prior_step != current_step:
        _discard_queued_activations(pending)


def _terminal_cell_width(value: str) -> int:
    value = _SGR_CONTROL.sub("", value)
    return sum(
        0
        if unicodedata.combining(character)
        else 2
        if unicodedata.east_asian_width(character) in {"F", "W"}
        else 1
        for character in value
    )


def _terminal_suffix(value: str, width: int) -> str:
    selected = []
    used = 0
    for character in reversed(value):
        character_width = _terminal_cell_width(character)
        if used + character_width > width:
            break
        selected.append(character)
        used += character_width
    return "".join(reversed(selected))


def _physical_row_count(value: str, width: int) -> int:
    return sum(
        max(1, (_terminal_cell_width(row) + width - 1) // width)
        for row in value.splitlines()
    )


def _validated_viewport(size: os.terminal_size) -> Tuple[int, int]:
    viewport = (size.columns, size.lines)
    if size.columns <= 0 or size.lines <= 0:
        raise ProtocolRefusal(
            "door_viewport_too_small",
            TUI_DOOR_COPY["tui.door.viewport_too_small"],
        )
    return viewport


def _measured_color_tier(capability_receipt: object) -> str:
    if os.environ.get("NO_COLOR"):
        return "mono"
    enabled = getattr(capability_receipt, "enabled", lambda _name: False)
    return "256" if enabled("rgb") else "mono"


def _terminal_frame(text: str, *, synchronized: bool) -> str:
    stable_rows = text.replace("\n", "\x1b[1E")
    if synchronized:
        return synchronized_output_frame(stable_rows).decode("utf-8")
    return "\x1b[H" + stable_rows + "\x1b[J"


def run_door_terminal(
    controller: DoorController,
    *,
    input_stream: TextIO = sys.stdin,
    output_stream: TextIO = sys.stderr,
    read_event: Optional[Callable[[Optional[float]], object]] = None,
    terminal_response: Optional[bytes] = None,
    prepare: Optional[Callable[[DoorController], None]] = None,
    complete: Callable[[DoorController], bool],
    remedy: str = _NODE_ADD_FULLY_FLAGGED_REMEDY,
) -> DoorController:
    """Own one guarded full-screen door and restore terminal state in reverse order."""
    if not isinstance(controller, DoorController):
        raise ProtocolRefusal(
            "door_controller_invalid", TUI_DOOR_COPY["tui.door.controller_invalid"]
        )
    require_door_terminal(input_stream, output_stream, remedy=remedy)

    descriptor = _terminal_io(input_stream.fileno)
    prior_termios = _terminal_io(lambda: termios.tcgetattr(descriptor))
    decoder = TerminalInputDecoder()
    pending: list[TerminalInput] = []
    text_buffer = ""
    text_step: Optional[str] = None
    active_frame: Optional[DoorFrame] = None
    active_viewport: Optional[Tuple[int, int]] = None
    cleanup_actions: list[Callable[[], object]] = []
    mouse_enabled = False
    synchronized_output_enabled = False
    tier = "mono"
    try:
        cleanup_actions.append(
            lambda: _write_and_flush(output_stream, "\x1b[?25h\x1b[?1049l")
        )
        _write_and_flush(output_stream, "\x1b[?1049h\x1b[?25l")
        cleanup_actions.append(
            lambda: _terminal_io(
                lambda: termios.tcsetattr(
                    descriptor, termios.TCSADRAIN, prior_termios
                )
            )
        )
        _terminal_io(lambda: tty.setcbreak(descriptor))
        capability_receipt, remainder = _terminal_io(
            lambda: probe_terminal_capabilities(
                input_stream,
                output_stream,
                environment=os.environ,
                preloaded_response=(
                    b""
                    if read_event is not None and terminal_response is None
                    else terminal_response
                ),
            )
        )
        pending.extend(decoder.feed(remainder))
        synchronized_output_enabled = capability_receipt.enabled(
            "synchronized_output"
        )
        tier = _measured_color_tier(capability_receipt)
        if capability_receipt.enabled("sgr_mouse"):
            cleanup_actions.append(
                lambda: _write_and_flush(
                    output_stream, mouse_tracking(False).decode("ascii")
                )
            )
            _write_and_flush(
                output_stream, mouse_tracking(True).decode("ascii")
            )
            mouse_enabled = True
        if capability_receipt.enabled("kitty_keyboard"):
            cleanup_actions.append(
                lambda: _write_and_flush(
                    output_stream, kitty_keyboard_mode(False).decode("ascii")
                )
            )
            _write_and_flush(
                output_stream, kitty_keyboard_mode(True).decode("ascii")
            )

        needs_render = True
        while not complete(controller):
            if prepare is not None:
                prepare(controller)
            if controller.step in DoorController._TEXT_STEPS:
                if text_step != controller.step:
                    text_step = controller.step
                    text_buffer = ""
            else:
                text_step = None
                text_buffer = ""
            if needs_render:
                size = _terminal_io(
                    lambda: os.get_terminal_size(output_stream.fileno())
                )
                viewport = _validated_viewport(size)
                active_frame = controller.render(
                    width=size.columns,
                    height=size.lines,
                    color_tier=tier,
                )
                rendered = active_frame.text
                if controller.step in DoorController._TEXT_STEPS:
                    input_width = max(
                        0,
                        size.columns - _terminal_cell_width(_TEXT_INPUT_PREFIX),
                    )
                    visible_input = _terminal_suffix(text_buffer, input_width)
                    rendered += "\n" + _TEXT_INPUT_PREFIX + visible_input
                if _physical_row_count(rendered, size.columns) > size.lines:
                    raise ProtocolRefusal(
                        "door_viewport_too_small",
                        TUI_DOOR_COPY["tui.door.viewport_too_small"],
                    )
                terminal_frame = _terminal_frame(
                    rendered,
                    synchronized=synchronized_output_enabled,
                )
                synchronized_cleanup: Optional[Callable[[], object]] = None
                if synchronized_output_enabled:
                    synchronized_cleanup = lambda: _write_and_flush(
                        output_stream, "\x1b[?2026l"
                    )
                    cleanup_actions.append(synchronized_cleanup)
                _write_and_flush(output_stream, terminal_frame)
                if synchronized_cleanup is not None:
                    assert cleanup_actions[-1] is synchronized_cleanup
                    cleanup_actions.pop()
                active_viewport = viewport
                needs_render = False

            event = (
                _terminal_io(lambda: read_event(None))
                if read_event is not None
                else _read_terminal_event(input_stream, decoder, pending)
            )
            prior_step = controller.step
            current_size = _terminal_io(
                lambda: os.get_terminal_size(output_stream.fileno())
            )
            current_viewport = _validated_viewport(current_size)
            if current_viewport != active_viewport:
                _discard_viewport_bound_events(pending)
                decoder = TerminalInputDecoder()
                controller._invalidate_rendered_frame()
                active_frame = None
                active_viewport = None
                needs_render = True
                continue
            if isinstance(event, MouseEvent):
                if (
                    not mouse_enabled
                    or active_frame is None
                    or active_frame.viewport != current_viewport
                ):
                    continue
                action = controller.handle_pointer(
                    active_frame,
                    event.column - 1,
                    event.row - 1,
                    activate=event.button == 0 and event.pressed,
                )
                if action.kind != "none":
                    needs_render = True
                _discard_stale_queued_activations(
                    pending, prior_step, controller.step
                )
                continue
            if not isinstance(event, str):
                continue
            if event in {"ESC", "\x1b"}:
                action = controller.handle_key(event)
                if action.kind != "none":
                    text_buffer = ""
                    text_step = None
                    needs_render = True
                _discard_stale_queued_activations(
                    pending, prior_step, controller.step
                )
                continue
            if controller.step in DoorController._TEXT_STEPS:
                if event in {"ENTER", "\r", "\n"}:
                    controller.submit_text(text_buffer)
                    text_buffer = ""
                    text_step = None
                    needs_render = True
                elif event in {"\x7f", "\b"}:
                    if text_buffer:
                        text_buffer = text_buffer[:-1]
                        needs_render = True
                elif len(event) == 1 and event.isprintable():
                    text_buffer += event
                    needs_render = True
                _discard_stale_queued_activations(
                    pending, prior_step, controller.step
                )
                continue
            action = controller.handle_key(event)
            if action.kind != "none":
                needs_render = True
            _discard_stale_queued_activations(
                pending, prior_step, controller.step
            )
        return controller
    finally:
        primary_error = sys.exc_info()[1]
        cleanup_errors: list[BaseException] = []
        for cleanup in reversed(cleanup_actions):
            _write_cleanup(cleanup, cleanup_errors)
        if primary_error is None and cleanup_errors:
            raise cleanup_errors[0]


def run_solo_door(
    node_id: Optional[str] = None,
    harness: Optional[str] = None,
    *,
    input_stream: TextIO = sys.stdin,
    output_stream: TextIO = sys.stderr,
    read_event: Optional[Callable[[Optional[float]], object]] = None,
    terminal_response: Optional[bytes] = None,
) -> Union[Tuple[str, str], "SoloInitPlan"]:
    """Return explicit answers or one committed immutable solo plan."""
    if node_id is None and harness is None:
        from .solo import plan_solo_bootstrap, render_solo_bootstrap_preview

        controller = DoorController.solo()

        def prepare_preview(current: DoorController) -> None:
            if current.step == "preview" and current.preview_plan() is None:
                plan = plan_solo_bootstrap(current.values[0], current.values[1])
                current.attach_preview(plan, render_solo_bootstrap_preview(plan))

        try:
            run_door_terminal(
                controller,
                input_stream=input_stream,
                output_stream=output_stream,
                read_event=read_event,
                terminal_response=terminal_response,
                prepare=prepare_preview,
                complete=lambda current: current.committed,
                remedy=_SOLO_FULLY_FLAGGED_REMEDY,
            )
        except KeyboardInterrupt as exc:
            raise ProtocolRefusal(
                "door_cancelled",
                TUI_DOOR_COPY["tui.door.cancelled"],
                _SOLO_FULLY_FLAGGED_REMEDY,
            ) from exc
        plan = controller.preview_plan()
        assert plan is not None
        return plan
    if not all(isinstance(value, str) and value.strip() for value in (node_id, harness)):
        raise ProtocolRefusal(
            "door_text_invalid", TUI_DOOR_COPY["tui.door.solo_text_invalid"]
        )
    return str(node_id), str(harness)


def run_node_add_door(
    wizard: "NodeWizard",
    controller: Optional[DoorController] = None,
    output: Optional[TextIO] = None,
    *,
    input_stream: TextIO = sys.stdin,
    terminal_output: TextIO = sys.stderr,
    read_event: Optional[Callable[[Optional[float]], object]] = None,
    terminal_response: Optional[bytes] = None,
) -> Dict[str, Any]:
    """Prepare one preview, then commit its identical plan only after final consent."""
    if controller is None:
        if output is None:
            raise ProtocolRefusal(
                "door_output_invalid", TUI_DOOR_COPY["tui.door.output_invalid"]
            )
        controller = DoorController.node_add()

        def prepare_preview(current: DoorController) -> None:
            if current.step == "preview" and current.preview_plan() is None:
                plan = wizard.plan_add(current.values)
                current.attach_preview(plan, wizard.render_add_preview(plan))

        try:
            run_door_terminal(
                controller,
                input_stream=input_stream,
                output_stream=terminal_output,
                read_event=read_event,
                terminal_response=terminal_response,
                prepare=prepare_preview,
                complete=lambda current: current.committed,
                remedy=_NODE_ADD_FULLY_FLAGGED_REMEDY,
            )
        except KeyboardInterrupt as exc:
            raise ProtocolRefusal(
                "door_cancelled",
                TUI_DOOR_COPY["tui.door.cancelled"],
                _NODE_ADD_FULLY_FLAGGED_REMEDY,
            ) from exc
        committed = run_node_add_door(wizard, controller, output)
        committed.pop("plan", None)
        return committed
    if output is None:
        raise ProtocolRefusal(
            "door_output_invalid", TUI_DOOR_COPY["tui.door.output_invalid"]
        )
    if controller.step != "preview":
        raise ProtocolRefusal(
            "door_preview_required", TUI_DOOR_COPY["tui.door.preview_required"]
        )
    plan = controller.preview_plan()
    if plan is None:
        plan = wizard.plan_add(controller.values)
        controller.attach_preview(plan, wizard.render_add_preview(plan))
    if not controller.committed:
        return {"status": "preview", "plan": plan, "preview": wizard.render_add_preview(plan)}
    if controller._commit_result is not None:
        return dict(controller._commit_result)
    try:
        result = wizard.commit_add(plan, output)
    except OSError as exc:
        raise DurabilityFailure(
            "node_add_commit_failed",
            TUI_DOOR_COPY["tui.door.node_add_commit_failed_prefix"]
            + (exc.strerror or str(exc)),
        ) from exc
    controller._commit_result = dict(result, plan=plan)
    return dict(controller._commit_result)

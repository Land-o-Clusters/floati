from __future__ import annotations

import io
import os
import re
import unittest
from unittest.mock import patch


FULL_CAPABILITY_RESPONSE = (
    b"\x1b[?1;2c"
    b"\x1b[?2026;1$y\x1b[?1006;1$y\x1b[?1016;1$y"
    b"\x1b_Gi=7108;OK\x1b\\"
    b"\x1b[?1u"
    b"\x1bP1+r524742=31\x1b\\"
)
DENIED_CAPABILITY_RESPONSE = (
    b"\x1b[?1;2c"
    b"\x1b[?2026;0$y\x1b[?1006;0$y\x1b[?1016;0$y"
    b"\x1b_Gi=7108;EINVAL\x1b\\"
    b"\x1bP0+r524742\x1b\\"
)


class TTY(io.StringIO):
    def isatty(self) -> bool:
        return True

    def fileno(self) -> int:
        return 81


class ChoiceFocusControllerTests(unittest.TestCase):
    def test_keyboard_and_pointer_share_stable_option_focus_semantics(self) -> None:
        """Catches approvals and doors diverging into separate focus engines."""
        from floati.tui_choice import ChoiceFocusController

        controller = ChoiceFocusController(
            ("first", "second", "third"), initial_option_id="first"
        )
        self.assertEqual("first", controller.focused_option_id)
        self.assertEqual("second", controller.handle_key("KEY_DOWN").option_id)
        self.assertEqual("second", controller.focused_option_id)
        self.assertEqual("third", controller.handle_key("3").option_id)
        self.assertEqual("third", controller.focused_option_id)
        self.assertEqual("second", controller.handle_key("k").option_id)
        self.assertEqual("second", controller.focused_option_id)
        self.assertEqual("third", controller.handle_pointer("third", activate=False).option_id)
        self.assertEqual("third", controller.focused_option_id)
        action = controller.handle_pointer("third", activate=True)
        self.assertEqual("activated", action.kind)
        self.assertEqual("third", action.option_id)
        self.assertEqual("third", controller.focused_option_id)


class DoorStateAndRenderTests(unittest.TestCase):
    def test_large_card_regions_own_every_visible_card_cell(self) -> None:
        """Catches pointer selection working only on a card label instead of its full rectangle."""
        from floati.tui_doors import DoorOption, render_door_frame

        frame = render_door_frame(
            "Choose node lifetime",
            (
                DoorOption("permanent", "Permanent", "No automatic expiry."),
                DoorOption("temporary", "Temporary", "Requires a bounded lease."),
            ),
            focused_option_id="permanent",
            width=72,
            color_tier="mono",
        )

        self.assertEqual(2, len(frame.hit_regions))
        self.assertTrue(all(region.width == 72 for region in frame.hit_regions))
        self.assertTrue(all(region.height >= 4 for region in frame.hit_regions))
        self.assertTrue(all(region.contains(region.x, region.y) for region in frame.hit_regions))
        self.assertTrue(
            all(
                region.contains(region.x + region.width - 1, region.y + region.height - 1)
                for region in frame.hit_regions
            )
        )

    def test_every_visible_card_row_matches_its_full_width_hit_rectangle(self) -> None:
        """Catches card side rows being narrower than the rectangle the pointer can select."""
        from floati.tui_doors import DoorOption, render_door_frame

        frame = render_door_frame(
            "Choose node lifetime",
            (
                DoorOption("permanent", "Permanent", "No automatic expiry."),
                DoorOption("temporary", "Temporary", "Requires a bounded lease."),
            ),
            focused_option_id="permanent",
            width=72,
            color_tier="256",
        )
        rows = re.sub(r"\x1b\[[0-9;]*m", "", frame.text).splitlines()

        for region in frame.hit_regions:
            self.assertEqual(
                [region.width] * region.height,
                [len(row) for row in rows[region.y : region.y + region.height]],
            )

    def test_focus_changes_visible_card_treatment_without_changing_color_semantics(self) -> None:
        """Catches a focused card losing its non-color marker or monochrome equivalent."""
        from floati.tui_doors import DoorOption, render_door_frame

        options = (
            DoorOption("permanent", "Permanent", "No automatic expiry."),
            DoorOption("temporary", "Temporary", "Requires a bounded lease."),
        )
        focused = render_door_frame(
            "Choose node lifetime", options, focused_option_id="permanent", width=72, color_tier="256"
        )
        unfocused = render_door_frame(
            "Choose node lifetime", options, focused_option_id="temporary", width=72, color_tier="256"
        )
        mono = render_door_frame(
            "Choose node lifetime", options, focused_option_id="permanent", width=72, color_tier="mono"
        )

        strip_ansi = lambda value: re.sub(r"\x1b\[[0-9;]*m", "", value)
        self.assertNotEqual(strip_ansi(focused.text), strip_ansi(unfocused.text))
        self.assertIn("▶", strip_ansi(focused.text))
        self.assertIn("═", strip_ansi(focused.text))
        self.assertEqual(strip_ansi(focused.text), mono.text)

    def test_node_add_door_requires_distinct_answers_and_escape_cannot_commit(self) -> None:
        """Catches the node door collapsing a decision or mutating before final confirmation."""
        from floati.tui_doors import DoorController

        controller = DoorController.node_add()
        self.assertEqual("node", controller.step)
        controller.submit_text("alpha")
        self.assertEqual("harness", controller.step)
        controller.submit_text("Codex")
        self.assertEqual("lifetime", controller.step)
        controller.handle_key("KEY_DOWN")
        controller.handle_key("ENTER")
        self.assertEqual("lease", controller.step)
        controller.submit_text("90")
        self.assertEqual("preview", controller.step)
        self.assertEqual("back", controller.handle_key("ESC").kind)
        self.assertEqual("lease", controller.step)
        self.assertFalse(controller.committed)

    def test_pointer_rejects_a_pre_preview_frame_after_exact_preview_attaches(self) -> None:
        """Catches an old same-screen card activating after preview content replaced the frame."""
        from floati.tui_doors import DoorController

        controller = DoorController.node_add()
        controller.submit_text("alpha")
        controller.submit_text("Codex")
        controller.handle_key("ENTER")
        self.assertEqual("preview", controller.step)
        stale_frame = controller.render()
        controller.attach_preview(object(), "ledger preview: exact")
        current_frame = controller.render()

        self.assertEqual(
            "none",
            controller.handle_pointer(
                stale_frame,
                stale_frame.hit_regions[0].x,
                stale_frame.hit_regions[0].y,
                activate=True,
            ).kind,
        )
        self.assertEqual(
            "committed",
            controller.handle_pointer(
                current_frame,
                current_frame.hit_regions[0].x,
                current_frame.hit_regions[0].y,
                activate=True,
            ).kind,
        )


class DoorTerminalLifecycleTests(unittest.TestCase):
    def _assert_coalesced_activation_does_not_reach_preview(
        self, raw_events: bytes
    ) -> None:
        from floati.tui_doors import DoorController, run_door_terminal

        controller = DoorController.node_add()
        controller.submit_text("builder-a")
        controller.submit_text("Codex")
        output = TTY()
        reads = iter((raw_events, RuntimeError("stop after queued activation")))

        def read_raw(_descriptor: int, _limit: int) -> bytes:
            value = next(reads)
            if isinstance(value, BaseException):
                raise value
            return value

        def prepare_preview(current: DoorController) -> None:
            if current.step == "preview" and current.preview_plan() is None:
                current.attach_preview(object(), "ledger preview: exact")

        with (
            patch.dict(os.environ, {"TERM": "xterm-256color"}, clear=True),
            patch("floati.tui_doors.termios.tcgetattr", return_value=[]),
            patch("floati.tui_doors.termios.tcsetattr"),
            patch("floati.tui_doors.tty.setcbreak"),
            patch(
                "floati.tui_doors.os.get_terminal_size",
                return_value=os.terminal_size((72, 24)),
            ),
            patch(
                "floati.tui_doors.select.select",
                return_value=([TTY().fileno()], [], []),
            ),
            patch("floati.tui_doors.os.read", side_effect=read_raw),
        ):
            with self.assertRaisesRegex(RuntimeError, "stop after queued activation"):
                run_door_terminal(
                    controller,
                    input_stream=TTY(),
                    output_stream=output,
                    terminal_response=FULL_CAPABILITY_RESPONSE,
                    prepare=prepare_preview,
                    complete=lambda current: current.committed,
                )

        self.assertEqual("preview", controller.step)
        self.assertFalse(controller.committed)

    def test_two_enters_decoded_from_one_read_cannot_commit_the_next_frame(self) -> None:
        """Catches a pre-preview queued Enter committing the newly rendered preview."""
        self._assert_coalesced_activation_does_not_reach_preview(b"\r\r")

    def test_two_mouse_activations_decoded_from_one_read_cannot_commit_the_next_frame(self) -> None:
        """Catches a pre-preview queued click activating Commit on the next frame."""
        click_first_card = b"\x1b[<0;1;2M"
        self._assert_coalesced_activation_does_not_reach_preview(
            click_first_card + click_first_card
        )

    def test_resize_discards_enter_until_the_new_frame_is_flushed(self) -> None:
        """Catches resize-coalesced Enter activating a choice from the stale frame."""
        from floati.tui_doors import DoorController, run_door_terminal

        controller = DoorController.node_add()
        controller.submit_text("builder-a")
        controller.submit_text("Codex")
        output = TTY()
        events = iter(("ENTER", RuntimeError("stop after resized frame")))
        observed_frames = []
        measured_sizes = iter(
            (
                os.terminal_size((72, 24)),
                os.terminal_size((60, 20)),
                os.terminal_size((60, 20)),
            )
        )

        def read_event(_timeout):
            observed_frames.append(output.getvalue().count("\x1b[?2026h"))
            event = next(events)
            if isinstance(event, BaseException):
                raise event
            return event

        with (
            patch.dict(os.environ, {"TERM": "xterm-256color"}, clear=True),
            patch("floati.tui_doors.termios.tcgetattr", return_value=[]),
            patch("floati.tui_doors.termios.tcsetattr"),
            patch("floati.tui_doors.tty.setcbreak"),
            patch("floati.tui_doors.os.get_terminal_size", side_effect=measured_sizes),
        ):
            with self.assertRaisesRegex(RuntimeError, "stop after resized frame"):
                run_door_terminal(
                    controller,
                    input_stream=TTY(),
                    output_stream=output,
                    read_event=read_event,
                    terminal_response=FULL_CAPABILITY_RESPONSE,
                    complete=lambda _current: False,
                )

        self.assertEqual("lifetime", controller.step)
        self.assertEqual([1, 2], observed_frames)

    def test_resize_discards_enter_before_refusing_a_too_small_new_viewport(self) -> None:
        """Catches stale Enter mutating state before a resized frame can be flushed."""
        from floati.errors import ProtocolRefusal
        from floati.tui_doors import DoorController, run_door_terminal

        controller = DoorController.node_add()
        controller.submit_text("builder-a")
        controller.submit_text("Codex")
        measured_sizes = iter(
            (
                os.terminal_size((72, 24)),
                os.terminal_size((40, 5)),
                os.terminal_size((40, 5)),
            )
        )
        with (
            patch.dict(os.environ, {"TERM": "xterm-256color"}, clear=True),
            patch("floati.tui_doors.termios.tcgetattr", return_value=[]),
            patch("floati.tui_doors.termios.tcsetattr"),
            patch("floati.tui_doors.tty.setcbreak"),
            patch("floati.tui_doors.os.get_terminal_size", side_effect=measured_sizes),
        ):
            with self.assertRaises(ProtocolRefusal) as caught:
                run_door_terminal(
                    controller,
                    input_stream=TTY(),
                    output_stream=TTY(),
                    read_event=lambda _timeout: "ENTER",
                    terminal_response=FULL_CAPABILITY_RESPONSE,
                    complete=lambda _current: False,
                )

        self.assertEqual("door_viewport_too_small", caught.exception.code)
        self.assertEqual("lifetime", controller.step)

    def test_decoder_resize_purges_coalesced_old_viewport_activations(self) -> None:
        """Catches a decoder-buffered Enter committing after replacement-frame reflow."""
        from floati.tui_doors import DoorController, run_door_terminal

        rendered_frames = []

        class RecordingController(DoorController):
            def render(self, **kwargs):
                frame = super().render(**kwargs)
                rendered_frames.append(frame)
                return frame

        controller = RecordingController()
        controller.submit_text("builder-a")
        controller.submit_text("Codex")
        controller.handle_key("ENTER")
        controller.attach_preview(object(), "ledger preview: " + "x" * 39)
        output = TTY()
        reads = iter((b"\r\r", RuntimeError("stop after replacement frame")))
        measured_sizes = iter(
            (
                os.terminal_size((40, 20)),
                os.terminal_size((72, 20)),
                os.terminal_size((72, 20)),
                os.terminal_size((72, 20)),
            )
        )

        def read_raw(_descriptor: int, _limit: int) -> bytes:
            value = next(reads)
            if isinstance(value, BaseException):
                raise value
            return value

        with (
            patch.dict(os.environ, {"TERM": "xterm-256color"}, clear=True),
            patch("floati.tui_doors.termios.tcgetattr", return_value=[]),
            patch("floati.tui_doors.termios.tcsetattr"),
            patch("floati.tui_doors.tty.setcbreak"),
            patch("floati.tui_doors.os.get_terminal_size", side_effect=measured_sizes),
            patch("floati.tui_doors.select.select", return_value=([TTY().fileno()], [], [])),
            patch("floati.tui_doors.os.read", side_effect=read_raw),
        ):
            with self.assertRaisesRegex(RuntimeError, "stop after replacement frame"):
                run_door_terminal(
                    controller,
                    input_stream=TTY(),
                    output_stream=output,
                    terminal_response=FULL_CAPABILITY_RESPONSE,
                    complete=lambda current: current.committed,
                )

        self.assertFalse(controller.committed)
        self.assertEqual([(40, 20), (72, 20)], [frame.viewport for frame in rendered_frames])
        self.assertNotEqual(
            rendered_frames[0].hit_regions[0].y,
            rendered_frames[1].hit_regions[0].y,
        )

    def test_decoder_resize_purges_every_old_geometry_mouse_event(self) -> None:
        """Catches an old secondary press or release refocusing the replacement card."""
        from floati.tui_doors import DoorController, run_door_terminal

        controller = DoorController.node_add()
        controller.submit_text("builder-a")
        controller.submit_text("Codex")
        controller.handle_key("ENTER")
        controller.attach_preview(object(), "ledger preview: " + "x" * 39)
        old_secondary_press = b"\x1b[<2;1;8M"
        old_primary_release = b"\x1b[<0;1;8m"
        reads = iter(
            (
                b"x" + old_secondary_press + old_primary_release,
                b"\r",
                RuntimeError("stop after later Enter"),
            )
        )
        size_calls = 0

        def terminal_size(_descriptor: int) -> os.terminal_size:
            nonlocal size_calls
            size_calls += 1
            return os.terminal_size((40, 20) if size_calls == 1 else (72, 20))

        def read_raw(_descriptor: int, _limit: int) -> bytes:
            value = next(reads)
            if isinstance(value, BaseException):
                raise value
            return value

        try:
            with (
                patch.dict(os.environ, {"TERM": "xterm-256color"}, clear=True),
                patch("floati.tui_doors.termios.tcgetattr", return_value=[]),
                patch("floati.tui_doors.termios.tcsetattr"),
                patch("floati.tui_doors.tty.setcbreak"),
                patch("floati.tui_doors.os.get_terminal_size", side_effect=terminal_size),
                patch(
                    "floati.tui_doors.select.select",
                    return_value=([TTY().fileno()], [], []),
                ),
                patch("floati.tui_doors.os.read", side_effect=read_raw),
            ):
                run_door_terminal(
                    controller,
                    input_stream=TTY(),
                    output_stream=TTY(),
                    terminal_response=FULL_CAPABILITY_RESPONSE,
                    complete=lambda current: current.committed,
                )
        except RuntimeError as exc:
            self.assertEqual("stop after later Enter", str(exc))

        self.assertTrue(controller.committed)
        self.assertEqual("preview", controller.step)

    def test_colored_lifetime_frame_fits_its_exact_minimum_height(self) -> None:
        """Catches SGR control bytes being counted as visible terminal cells."""
        from floati.tui_doors import DoorController, run_door_terminal

        controller = DoorController.node_add()
        controller.submit_text("builder-a")
        controller.submit_text("Codex")
        with (
            patch.dict(os.environ, {"TERM": "xterm-256color"}, clear=True),
            patch("floati.tui_doors.termios.tcgetattr", return_value=[]),
            patch("floati.tui_doors.termios.tcsetattr"),
            patch("floati.tui_doors.tty.setcbreak"),
            patch(
                "floati.tui_doors.os.get_terminal_size",
                return_value=os.terminal_size((40, 10)),
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "minimum frame flushed"):
                run_door_terminal(
                    controller,
                    input_stream=TTY(),
                    output_stream=TTY(),
                    read_event=lambda _timeout: (_ for _ in ()).throw(
                        RuntimeError("minimum frame flushed")
                    ),
                    terminal_response=FULL_CAPABILITY_RESPONSE,
                    complete=lambda _current: False,
                )

    def test_zero_column_viewport_is_a_typed_refusal(self) -> None:
        """Catches invalid geometry reaching physical-row division."""
        from floati.errors import ProtocolRefusal
        from floati.tui_doors import DoorController, run_door_terminal

        with (
            patch.dict(os.environ, {"TERM": "xterm-256color"}, clear=True),
            patch("floati.tui_doors.termios.tcgetattr", return_value=[]),
            patch("floati.tui_doors.termios.tcsetattr"),
            patch("floati.tui_doors.tty.setcbreak"),
            patch(
                "floati.tui_doors.os.get_terminal_size",
                return_value=os.terminal_size((0, 2)),
            ),
        ):
            with self.assertRaises(ProtocolRefusal) as caught:
                run_door_terminal(
                    DoorController.node_add(),
                    input_stream=TTY(),
                    output_stream=TTY(),
                    read_event=lambda _timeout: (_ for _ in ()).throw(
                        AssertionError("invalid viewport reached input")
                    ),
                    terminal_response=DENIED_CAPABILITY_RESPONSE,
                    complete=lambda _current: False,
                )

        self.assertEqual("door_viewport_too_small", caught.exception.code)

    def test_maximum_visible_text_input_reserves_the_registered_prefix_width(self) -> None:
        """Catches the DRAFT input prefix wrapping an uncounted physical row."""
        from floati.tui_doors import DoorController, run_door_terminal

        class FrameRecordingTTY(TTY):
            def __init__(self) -> None:
                super().__init__()
                self.frames = []

            def write(self, value: str) -> int:
                if value.startswith("\x1b[H") and value.endswith("\x1b[J"):
                    self.frames.append(value)
                return super().write(value)

        output = FrameRecordingTTY()
        events = iter((*"x" * 31, RuntimeError("stop after maximum input")))

        def read_event(_timeout):
            event = next(events)
            if isinstance(event, BaseException):
                raise event
            return event

        with (
            patch.dict(os.environ, {"TERM": "xterm-256color"}, clear=True),
            patch("floati.tui_doors.termios.tcgetattr", return_value=[]),
            patch("floati.tui_doors.termios.tcsetattr"),
            patch("floati.tui_doors.tty.setcbreak"),
            patch(
                "floati.tui_doors.os.get_terminal_size",
                return_value=os.terminal_size((40, 2)),
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "stop after maximum input"):
                run_door_terminal(
                    DoorController.node_add(),
                    input_stream=TTY(),
                    output_stream=output,
                    read_event=read_event,
                    terminal_response=DENIED_CAPABILITY_RESPONSE,
                    complete=lambda _current: False,
                )

        final_frame = output.frames[-1][len("\x1b[H") : -len("\x1b[J")]
        rows = final_frame.split("\x1b[1E")
        self.assertEqual(2, len(rows))
        self.assertEqual("DRAFT - > " + "x" * 30, rows[-1])
        self.assertEqual([40, 40], [len(row) for row in rows])

    def test_coalesced_text_then_escape_resolves_before_a_blocking_read(self) -> None:
        """Catches a buffered lone Escape blocking forever after coalesced text returns."""
        from floati.tui_doors import _read_terminal_event
        from floati.tui_protocol import TerminalInputDecoder

        decoder = TerminalInputDecoder()
        pending = list(decoder.feed(b"a\x1b"))
        self.assertEqual("a", _read_terminal_event(TTY(), decoder, pending))
        select_timeouts = []

        def select_once(_read, _write, _error, timeout=None):
            select_timeouts.append(timeout)
            if timeout is None:
                raise AssertionError("buffered Escape reached a blocking select")
            return [], [], []

        with (
            patch("floati.tui_doors.select.select", side_effect=select_once),
            patch(
                "floati.tui_doors.os.read",
                side_effect=AssertionError("buffered Escape reached os.read"),
            ),
        ):
            self.assertEqual("\x1b", _read_terminal_event(TTY(), decoder, pending))

        self.assertEqual([0.03], select_timeouts)

    def test_backend_commit_oserror_keeps_durability_semantics(self) -> None:
        """Catches backend commit I/O being mislabeled as pre-mutation terminal failure."""
        from floati.errors import DurabilityFailure
        from floati.tui_doors import DoorController, run_node_add_door

        plan = object()
        test_case = self

        class FailingWizard:
            def render_add_preview(self, exact_plan):
                test_case.assertIs(plan, exact_plan)
                return "ledger preview: exact"

            def commit_add(self, exact_plan, output):
                test_case.assertIs(plan, exact_plan)
                output.write("ledger preview: exact\n")
                raise OSError("ledger fsync")

        controller = DoorController.node_add()
        controller.submit_text("builder-a")
        controller.submit_text("Codex")
        controller.handle_key("ENTER")
        controller.attach_preview(plan, "ledger preview: exact")
        controller.handle_key("ENTER")

        with self.assertRaises(DurabilityFailure) as caught:
            run_node_add_door(FailingWizard(), controller, io.StringIO())

        self.assertEqual("node_add_commit_failed", caught.exception.code)
        self.assertIn("ledger fsync", caught.exception.detail)

    def test_keyboard_and_whole_card_mouse_changes_redraw_before_the_next_event(self) -> None:
        """Catches changed door state waiting for another key before its frame becomes active."""
        from floati.tui_doors import DoorController, run_door_terminal
        from floati.tui_protocol import MouseEvent

        controller = DoorController.node_add()
        controller.submit_text("builder-a")
        controller.submit_text("Codex")
        output = TTY()
        events = iter(
            (
                "KEY_DOWN",
                "ENTER",
                "9",
                "0",
                "ENTER",
                MouseEvent(button=0, column=1, row=4, pressed=True),
            )
        )
        observed_frame_counts = []

        def read_event(_timeout):
            observed_frame_counts.append(output.getvalue().count("\x1b[?2026h"))
            return next(events)

        def prepare_preview(current):
            if current.step == "preview" and current.preview_plan() is None:
                current.attach_preview(object(), "ledger preview: exact")

        with (
            patch.dict(os.environ, {"TERM": "xterm-256color"}, clear=True),
            patch("floati.tui_doors.termios.tcgetattr", return_value=[]),
            patch("floati.tui_doors.termios.tcsetattr"),
            patch("floati.tui_doors.tty.setcbreak"),
            patch(
                "floati.tui_doors.os.get_terminal_size",
                return_value=os.terminal_size((72, 24)),
            ),
        ):
            run_door_terminal(
                controller,
                input_stream=TTY(),
                output_stream=output,
                read_event=read_event,
                terminal_response=FULL_CAPABILITY_RESPONSE,
                prepare=prepare_preview,
                complete=lambda current: current.committed,
            )

        self.assertEqual(sorted(observed_frame_counts), observed_frame_counts)
        self.assertEqual(len(set(observed_frame_counts)), len(observed_frame_counts))
        self.assertTrue(controller.committed)
        rendered = output.getvalue()
        self.assertIn("TEMPORARY", rendered)
        self.assertIn("ledger preview: exact", rendered)

    def test_terminal_cleanup_is_reverse_order_and_primary_exception_wins(self) -> None:
        """Catches cleanup masking the door failure or leaving pushed terminal modes active."""
        from floati.tui_doors import DoorController, run_door_terminal

        events: list[str] = []

        class RecordingTTY(TTY):
            def write(self, value: str) -> int:
                if value == "\x1b[<u":
                    events.append("kitty-pop")
                elif value == "\x1b[?1000l\x1b[?1006l":
                    events.append("mouse-off")
                elif value == "\x1b[?25h\x1b[?1049l":
                    events.append("screen-leave")
                return super().write(value)

        def restore(*_args):
            events.append("termios-restore")
            raise OSError("cleanup")

        controller = DoorController.node_add()
        controller.submit_text("builder-a")
        controller.submit_text("Codex")
        output = RecordingTTY()
        with (
            patch.dict(os.environ, {"TERM": "xterm-256color"}, clear=True),
            patch("floati.tui_doors.termios.tcgetattr", return_value=[]),
            patch("floati.tui_doors.termios.tcsetattr", side_effect=restore),
            patch("floati.tui_doors.tty.setcbreak"),
            patch(
                "floati.tui_doors.os.get_terminal_size",
                return_value=os.terminal_size((72, 24)),
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "primary"):
                run_door_terminal(
                    controller,
                    input_stream=TTY(),
                    output_stream=output,
                    read_event=lambda _timeout: (_ for _ in ()).throw(
                        RuntimeError("primary")
                    ),
                    terminal_response=FULL_CAPABILITY_RESPONSE,
                    complete=lambda _current: False,
                )

        self.assertEqual(
            ["kitty-pop", "mouse-off", "termios-restore", "screen-leave"],
            events,
        )

    def test_each_terminal_reverse_is_armed_before_partial_write_or_flush_failure(self) -> None:
        """Catches an ambiguous forward write leaving a terminal mode or sync frame active."""
        from floati.tui_doors import (
            DoorController,
            DoorTerminalIOError,
            run_door_terminal,
        )

        class Receipt:
            def __init__(self, enabled: set[str]) -> None:
                self._enabled = enabled

            def enabled(self, name: str) -> bool:
                return name in self._enabled

        scenarios = (
            (
                "alternate-screen-cursor",
                set(),
                lambda value: value == "\x1b[?1049h\x1b[?25l",
                "\x1b[?25h\x1b[?1049l",
            ),
            (
                "mouse",
                {"sgr_mouse"},
                lambda value: value == "\x1b[?1000h\x1b[?1006h",
                "\x1b[?1000l\x1b[?1006l",
            ),
            (
                "kitty-keyboard",
                {"sgr_mouse", "kitty_keyboard"},
                lambda value: value == "\x1b[>1u",
                "\x1b[<u",
            ),
            (
                "synchronized-output",
                {"synchronized_output"},
                lambda value: value.startswith("\x1b[?2026h"),
                "\x1b[?2026l",
            ),
        )

        for label, enabled, matches_forward, expected_reverse in scenarios:
            for failure_kind in ("partial-write", "flush"):
                with self.subTest(mode=label, failure=failure_kind):
                    class AmbiguousFailureTTY(TTY):
                        def __init__(self) -> None:
                            super().__init__()
                            self.writes: list[str] = []
                            self.failed = False
                            self.fail_next_flush = False

                        def write(self, value: str) -> int:
                            self.writes.append(value)
                            written = super().write(value)
                            if not self.failed and matches_forward(value):
                                self.failed = True
                                if failure_kind == "partial-write":
                                    return written - 1
                                self.fail_next_flush = True
                            return written

                        def flush(self) -> None:
                            if self.fail_next_flush:
                                self.fail_next_flush = False
                                raise OSError(label + " flush")
                            super().flush()

                    controller = DoorController.node_add()
                    controller.submit_text("builder-a")
                    controller.submit_text("Codex")
                    output = AmbiguousFailureTTY()
                    receipt = Receipt(enabled)
                    with (
                        patch.dict(
                            os.environ, {"TERM": "xterm-256color"}, clear=True
                        ),
                        patch("floati.tui_doors.termios.tcgetattr", return_value=[]),
                        patch("floati.tui_doors.termios.tcsetattr"),
                        patch("floati.tui_doors.tty.setcbreak"),
                        patch(
                            "floati.tui_doors.probe_terminal_capabilities",
                            return_value=(receipt, b""),
                        ),
                        patch(
                            "floati.tui_doors.os.get_terminal_size",
                            return_value=os.terminal_size((72, 24)),
                        ),
                    ):
                        with self.assertRaises(DoorTerminalIOError):
                            run_door_terminal(
                                controller,
                                input_stream=TTY(),
                                output_stream=output,
                                read_event=lambda _timeout: (_ for _ in ()).throw(
                                    AssertionError("failed forward reached input")
                                ),
                                complete=lambda _current: False,
                            )

                    forward_index = next(
                        index
                        for index, value in enumerate(output.writes)
                        if matches_forward(value)
                    )
                    self.assertGreater(
                        len(output.writes),
                        forward_index + 1,
                        output.writes,
                    )
                    self.assertEqual(
                        expected_reverse,
                        output.writes[forward_index + 1],
                        output.writes,
                    )

    def test_output_viewport_resize_invalidates_old_commit_geometry_before_click(self) -> None:
        """Catches a resized visible Back card activating Commit through stale coordinates."""
        from floati.tui_doors import DoorController, run_door_terminal
        from floati.tui_protocol import MouseEvent

        class OutputTTY(TTY):
            def fileno(self) -> int:
                return 91

        controller = DoorController.node_add()
        controller.submit_text("builder-a")
        controller.submit_text("Codex")
        controller.handle_key("ENTER")
        controller.attach_preview(
            object(), "ledger preview: " + "x" * 39
        )
        output = OutputTTY()
        events = iter(
            (
                MouseEvent(button=0, column=1, row=8, pressed=True),
                RuntimeError("stop after resize redraw"),
            )
        )
        measured_descriptors = []
        measured_sizes = iter(
            (
                os.terminal_size((40, 20)),
                os.terminal_size((72, 20)),
                os.terminal_size((72, 20)),
                os.terminal_size((72, 20)),
            )
        )

        def terminal_size(descriptor):
            measured_descriptors.append(descriptor)
            return next(measured_sizes)

        def read_event(_timeout):
            event = next(events)
            if isinstance(event, BaseException):
                raise event
            return event

        with (
            patch.dict(os.environ, {"TERM": "xterm-256color"}, clear=True),
            patch("floati.tui_doors.termios.tcgetattr", return_value=[]),
            patch("floati.tui_doors.termios.tcsetattr"),
            patch("floati.tui_doors.tty.setcbreak"),
            patch("floati.tui_doors.os.get_terminal_size", side_effect=terminal_size),
        ):
            with self.assertRaisesRegex(RuntimeError, "stop after resize redraw"):
                run_door_terminal(
                    controller,
                    input_stream=TTY(),
                    output_stream=output,
                    read_event=read_event,
                    terminal_response=FULL_CAPABILITY_RESPONSE,
                    complete=lambda _current: False,
                )

        self.assertFalse(controller.committed)
        self.assertTrue(measured_descriptors)
        self.assertEqual({91}, set(measured_descriptors))
        self.assertIn("\x1b[1E", output.getvalue())

    def test_output_viewport_height_refuses_a_frame_that_would_scroll(self) -> None:
        """Catches a preview rendering beyond stderr height and scrolling its hit map."""
        from floati.errors import ProtocolRefusal
        from floati.tui_doors import DoorController, run_door_terminal

        controller = DoorController.node_add()
        controller.submit_text("builder-a")
        controller.submit_text("Codex")
        controller.handle_key("ENTER")
        controller.attach_preview(object(), "\n".join(("one", "two", "three")))
        with (
            patch.dict(os.environ, {"TERM": "xterm-256color"}, clear=True),
            patch("floati.tui_doors.termios.tcgetattr", return_value=[]),
            patch("floati.tui_doors.termios.tcsetattr"),
            patch("floati.tui_doors.tty.setcbreak"),
            patch(
                "floati.tui_doors.os.get_terminal_size",
                return_value=os.terminal_size((40, 5)),
            ),
        ):
            with self.assertRaises(ProtocolRefusal) as caught:
                run_door_terminal(
                    controller,
                    input_stream=TTY(),
                    output_stream=TTY(),
                    read_event=lambda _timeout: (_ for _ in ()).throw(
                        AssertionError("oversize frame reached input")
                    ),
                    terminal_response=FULL_CAPABILITY_RESPONSE,
                    complete=lambda _current: False,
                )

        self.assertEqual("door_viewport_too_small", caught.exception.code)

    def test_denied_capabilities_enable_no_unmeasured_protocol_or_color(self) -> None:
        """Catches TERM testimony enabling sync, mouse, or palette without measurement."""
        from floati.tui_doors import DoorController, run_door_terminal

        controller = DoorController.node_add()
        controller.submit_text("builder-a")
        controller.submit_text("Codex")
        output = TTY()
        with (
            patch.dict(os.environ, {"TERM": "xterm-256color"}, clear=True),
            patch("floati.tui_doors.termios.tcgetattr", return_value=[]),
            patch("floati.tui_doors.termios.tcsetattr"),
            patch("floati.tui_doors.tty.setcbreak"),
            patch("floati.tui_doors.os.get_terminal_size", return_value=os.terminal_size((72, 24))),
        ):
            with self.assertRaisesRegex(RuntimeError, "stop"):
                run_door_terminal(
                    controller,
                    input_stream=TTY(),
                    output_stream=output,
                    read_event=lambda _timeout: (_ for _ in ()).throw(RuntimeError("stop")),
                    terminal_response=DENIED_CAPABILITY_RESPONSE,
                    complete=lambda _current: False,
                )

        rendered = output.getvalue()
        self.assertNotIn("\x1b[?2026h", rendered)
        self.assertNotIn("\x1b[?1000h\x1b[?1006h", rendered)
        self.assertNotIn("\x1b[>1u", rendered)
        self.assertNotIn("\x1b[38;5;208m", rendered)
        self.assertNotIn("\n", rendered)

    def test_solo_requires_exact_preview_and_current_commit_card_before_returning(self) -> None:
        """Catches solo exiting after text input without an immutable preview/commit plan."""
        from floati.tui_doors import run_solo_door

        output = TTY()
        events = iter((*"me", "ENTER", *"Codex", "ENTER", "ENTER"))
        with (
            patch.dict(os.environ, {"TERM": "xterm-256color"}, clear=True),
            patch("floati.tui_doors.termios.tcgetattr", return_value=[]),
            patch("floati.tui_doors.termios.tcsetattr"),
            patch("floati.tui_doors.tty.setcbreak"),
            patch(
                "floati.tui_doors.os.get_terminal_size",
                return_value=os.terminal_size((200, 30)),
            ),
        ):
            reviewed_plan = run_solo_door(
                input_stream=TTY(),
                output_stream=output,
                read_event=lambda _timeout: next(events),
                terminal_response=FULL_CAPABILITY_RESPONSE,
            )

        configuration_bytes = getattr(reviewed_plan, "configuration_bytes", None)
        self.assertIsInstance(configuration_bytes, bytes)
        self.assertIn(configuration_bytes.decode("utf-8").strip(), output.getvalue())
        self.assertIn("DRAFT - COMMIT THESE RECORDS", output.getvalue())
        self.assertNotIn("authority_epoch", output.getvalue())
        self.assertNotIn("expires_at", output.getvalue())
        with self.assertRaises(AttributeError):
            reviewed_plan.node_id = "other"

    def test_ctrl_c_becomes_typed_refusal_only_after_lifo_terminal_cleanup(self) -> None:
        """Catches Ctrl-C escaping before Kitty, mouse, termios, and screen restoration."""
        from floati.errors import ProtocolRefusal
        from floati.tui_doors import run_solo_door

        cleanup: list[str] = []

        class RecordingTTY(TTY):
            def write(self, value: str) -> int:
                if value == "\x1b[<u":
                    cleanup.append("kitty-pop")
                elif value == "\x1b[?1000l\x1b[?1006l":
                    cleanup.append("mouse-off")
                elif value == "\x1b[?25h\x1b[?1049l":
                    cleanup.append("screen-leave")
                return super().write(value)

        def restore(*_args: object) -> None:
            cleanup.append("termios-restore")

        try:
            with (
                patch.dict(
                    os.environ, {"TERM": "xterm-256color"}, clear=True
                ),
                patch("floati.tui_doors.termios.tcgetattr", return_value=[]),
                patch("floati.tui_doors.termios.tcsetattr", side_effect=restore),
                patch("floati.tui_doors.tty.setcbreak"),
                patch(
                    "floati.tui_doors.os.get_terminal_size",
                    return_value=os.terminal_size((72, 24)),
                ),
            ):
                with self.assertRaises(ProtocolRefusal) as caught:
                    run_solo_door(
                        input_stream=TTY(),
                        output_stream=RecordingTTY(),
                        read_event=lambda _timeout: (_ for _ in ()).throw(
                            KeyboardInterrupt()
                        ),
                        terminal_response=FULL_CAPABILITY_RESPONSE,
                    )
        except KeyboardInterrupt:
            self.fail("Ctrl-C escaped the interactive door boundary")

        self.assertEqual("door_cancelled", caught.exception.code)
        self.assertEqual(
            "DRAFT - floati init --root ROOT --solo NODE --harness HARNESS",
            caught.exception.remedy,
        )
        self.assertEqual(
            ["kitty-pop", "mouse-off", "termios-restore", "screen-leave"],
            cleanup,
        )

    def test_escape_is_navigation_before_text_editing(self) -> None:
        """Catches the text-entry branch swallowing an Escape that should go back."""
        from floati.tui_doors import DoorController, run_door_terminal

        controller = DoorController.node_add()
        controller.submit_text("builder-a")
        events = iter(("\x1b", RuntimeError("stop after back")))

        def read_event(_timeout):
            event = next(events)
            if isinstance(event, BaseException):
                raise event
            return event

        with (
            patch.dict(os.environ, {"TERM": "xterm-256color"}, clear=True),
            patch("floati.tui_doors.termios.tcgetattr", return_value=[]),
            patch("floati.tui_doors.termios.tcsetattr"),
            patch("floati.tui_doors.tty.setcbreak"),
            patch("floati.tui_doors.os.get_terminal_size", return_value=os.terminal_size((72, 24))),
        ):
            with self.assertRaisesRegex(RuntimeError, "stop after back"):
                run_door_terminal(
                    controller,
                    input_stream=TTY(),
                    output_stream=TTY(),
                    read_event=read_event,
                    terminal_response=FULL_CAPABILITY_RESPONSE,
                    complete=lambda _current: False,
                )

        self.assertEqual("node", controller.step)


if __name__ == "__main__":
    unittest.main()

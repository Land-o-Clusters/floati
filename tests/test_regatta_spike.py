from __future__ import annotations

import hashlib
import io
import json
import os
import re
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.test_tui_render import RECEIPTS, SNAPSHOT, WORK


FULL_CAPABILITY_RESPONSE = (
    b"\x1b[?1;2c"
    b"\x1b[?2026;1$y\x1b[?1006;1$y\x1b[?1016;1$y"
    b"\x1b_Gi=7108;OK\x1b\\"
    b"\x1b[?1u"
    b"\x1bP1+r524742=31\x1b\\"
)


class RegattaMachineTwinTests(unittest.TestCase):
    def test_board_plain_and_json_bytes_stay_pinned_during_regatta(self) -> None:
        """Catches visual work leaking into either stable machine-facing twin."""
        from floati.tui_render import HarborBoardModel, render_plain_dump

        model = HarborBoardModel.from_projection(SNAPSHOT, WORK, RECEIPTS)
        plain = render_plain_dump(model, width=100).encode("utf-8")
        artifact = (
            json.dumps(
                model.to_snapshot(),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")

        self.assertEqual(
            "0fa896c0fb3d8a3ed97bdab22847214558c500e28ea2ea601366e16911da3504",
            hashlib.sha256(plain).hexdigest(),
        )
        self.assertEqual(
            "711c98bd6475631ef1d0bf3bb9a3b11b9eed9326eaf5b9c45297ebdfacd5b847",
            hashlib.sha256(artifact).hexdigest(),
        )


class RegattaTerminalProtocolTests(unittest.TestCase):
    def test_sync_frame_closes_the_transaction_after_image_overlay(self) -> None:
        """Catches a torn frame caused by ending sync mode before the image placement."""
        from floati.tui_protocol import synchronized_output_frame

        payload = synchronized_output_frame("harbor", image=b"KITTY")

        self.assertEqual(b"\x1b[?2026h\x1b[Hharbor\x1b[J\x1b[HKITTY\x1b[?2026l", payload)

    def test_sgr_mouse_click_opens_the_clicked_node_with_keyboard_parity(self) -> None:
        """Catches mouse coordinates being parsed but routed to the wrong vessel."""
        from floati.tui import BoardController
        from floati.tui_protocol import MouseEvent, decode_terminal_input
        from floati.tui_render import HarborBoardModel, node_row_positions

        model = HarborBoardModel.from_projection(SNAPSHOT, WORK, RECEIPTS)
        controller = BoardController(model)
        rows = node_row_positions(model, width=100)
        event = decode_terminal_input(f"\x1b[<0;12;{rows[1]}M".encode("ascii"))

        self.assertEqual(MouseEvent(button=0, column=12, row=rows[1], pressed=True), event)
        action = controller.handle_mouse(event, node_rows=rows, viewport_width=100)
        self.assertEqual("detail", action.kind)
        self.assertEqual(1, controller.selected)
        self.assertTrue(controller.detail_open)

    def test_mouse_hit_rows_exclude_nodes_clipped_by_the_hint_footer(self) -> None:
        """Catches a click on the footer selecting a vessel that is not visible."""
        from floati.tui import BoardController
        from floati.tui_protocol import MouseEvent
        from floati.tui_render import HarborBoardModel, node_row_positions

        model = HarborBoardModel.from_projection(SNAPSHOT, WORK, RECEIPTS)
        rows = node_row_positions(model, width=100, height=8)
        controller = BoardController(model)

        self.assertEqual((7,), rows)
        action = controller.handle_mouse(
            MouseEvent(button=0, column=12, row=8, pressed=True),
            node_rows=rows,
            viewport_width=100,
        )
        self.assertEqual("none", action.kind)
        self.assertEqual(0, controller.selected)
        self.assertFalse(controller.detail_open)

    def test_terminal_input_decoder_retains_split_and_coalesced_events(self) -> None:
        """Catches one read boundary being mistaken for one terminal event boundary."""
        from floati.tui_protocol import MouseEvent, TerminalInputDecoder

        decoder = TerminalInputDecoder()
        self.assertEqual((), decoder.feed(b"\x1b"))
        self.assertEqual(("KEY_UP",), decoder.feed(b"[A"))
        self.assertEqual((), decoder.feed(b"\x1b[<0;12;"))
        self.assertEqual(
            (MouseEvent(button=0, column=12, row=7, pressed=True), "q"),
            decoder.feed(b"7Mq"),
        )
        self.assertEqual(("KEY_DOWN", "KEY_DOWN"), decoder.feed(b"\x1b[B\x1b[B"))

    def test_terminal_input_decoder_recovers_after_a_malformed_sgr_prefix(self) -> None:
        """Catches a malformed control prefix swallowing later keyboard input."""
        from floati.tui_protocol import TerminalInputDecoder

        decoder = TerminalInputDecoder()
        self.assertEqual((), decoder.feed(b"\x1b[<badq"))
        self.assertEqual(("j",), decoder.feed(b"j"))

    def test_mouse_hit_target_rejects_columns_outside_the_viewport(self) -> None:
        """Catches row-only routing opening a vessel outside the rendered viewport."""
        from floati.tui import BoardController
        from floati.tui_protocol import MouseEvent
        from floati.tui_render import HarborBoardModel, node_row_positions

        model = HarborBoardModel.from_projection(SNAPSHOT, WORK, RECEIPTS)
        controller = BoardController(model)
        rows = node_row_positions(model, width=20, height=12)
        action = controller.handle_mouse(
            MouseEvent(button=0, column=32767, row=rows[0], pressed=True),
            node_rows=rows,
            viewport_width=20,
        )
        self.assertEqual("none", action.kind)
        self.assertFalse(controller.detail_open)

    def test_terminal_input_decoder_drops_overlong_or_unbounded_sgr_frames(self) -> None:
        """Catches malformed mouse input retaining unbounded bytes or huge coordinates."""
        from floati.tui_protocol import TerminalInputDecoder

        incomplete = TerminalInputDecoder()
        self.assertEqual((), incomplete.feed(b"\x1b[<0;12;" + b"9" * 100))
        self.assertEqual(("q",), incomplete.feed(b"q"))

        complete = TerminalInputDecoder()
        self.assertEqual(
            ("q",),
            complete.feed(b"\x1b[<0;999999;7Mq"),
        )

    def test_kitty_graphics_requires_an_exact_terminal_response(self) -> None:
        """Catches user-agent guessing or malformed responses enabling graphics."""
        from floati.tui_protocol import kitty_graphics_supported

        self.assertTrue(kitty_graphics_supported(b"\x1b_Gi=7108;OK\x1b\\"))
        self.assertFalse(kitty_graphics_supported(b"TERM=xterm-kitty"))
        self.assertFalse(kitty_graphics_supported(b"\x1b_Gi=7108;EINVAL\x1b\\"))
        self.assertFalse(kitty_graphics_supported(b""))

    def test_kitty_probe_split_preserves_interleaved_keyboard_input(self) -> None:
        """Catches capability probing consuming keys typed during startup."""
        from floati.tui_protocol import TerminalInputDecoder, split_kitty_response

        response, remainder = split_kitty_response(
            b"j\x1b_Gi=7108;OK\x1b\\q"
        )

        self.assertEqual(b"\x1b_Gi=7108;OK\x1b\\", response)
        self.assertEqual(("j", "q"), TerminalInputDecoder().feed(remainder))

    def test_kitty_buoy_transmits_one_real_png_with_quiet_placement(self) -> None:
        """Catches a placeholder escape or non-image payload shipping as the brand mark."""
        import base64

        from floati.tui_protocol import KITTY_IMAGE_ID, kitty_buoy_image

        transmission = kitty_buoy_image()
        match = re.fullmatch(rb"\x1b_G([^;]+);([^\x1b]+)\x1b\\", transmission)
        self.assertIsNotNone(match)
        assert match is not None
        controls = match.group(1).split(b",")
        self.assertIn(b"a=T", controls)
        self.assertIn(b"f=100", controls)
        self.assertIn(b"q=2", controls)
        self.assertIn(f"i={KITTY_IMAGE_ID}".encode("ascii"), controls)
        self.assertTrue(base64.b64decode(match.group(2)).startswith(b"\x89PNG\r\n\x1a\n"))

    def test_interactive_board_enables_mouse_and_places_one_probed_buoy(self) -> None:
        """Catches protocol primitives existing without reaching the real Board loop."""
        from floati.tui import run_board
        from floati.tui_render import HarborBoardModel

        class TTY(io.StringIO):
            def isatty(self) -> bool:
                return True

            def fileno(self) -> int:
                return 71

        model = HarborBoardModel.from_projection(SNAPSHOT, WORK, RECEIPTS)
        output = TTY()
        with (
            patch.dict(os.environ, {"TERM": "xterm-256color"}, clear=True),
            patch("floati.tui.termios.tcgetattr", return_value=[]),
            patch("floati.tui.termios.tcsetattr"),
            patch("floati.tui.tty.setcbreak"),
            patch("floati.tui._read_terminal_input", return_value="q"),
            patch(
                "floati.tui.shutil.get_terminal_size",
                return_value=os.terminal_size((100, 30)),
            ),
        ):
            code = run_board(
                model_loader=lambda: model,
                input_stream=TTY(),
                output_stream=output,
                terminal_response=FULL_CAPABILITY_RESPONSE,
            )

        rendered = output.getvalue()
        self.assertEqual(0, code)
        self.assertIn("\x1b[?1000h\x1b[?1006h", rendered)
        self.assertIn("\x1b[?2026h", rendered)
        self.assertGreaterEqual(rendered.count("\x1b_Ga=T,"), 3)
        self.assertEqual(1, len(re.findall(r"\x1b_Ga=T,[^;]*i=7109(?:,|;)", rendered)))
        self.assertLess(rendered.index("\x1b_Ga=T,"), rendered.index("\x1b[?2026l"))
        self.assertIn("\x1b_Ga=d,d=I,q=2,i=7109\x1b\\", rendered)
        self.assertIn("\x1b[?1000l\x1b[?1006l", rendered)

    def test_terminal_restore_failure_cannot_skip_cleanup_or_mask_primary_error(self) -> None:
        """Catches a disconnected TTY leaving mouse/image/alt-screen state armed."""
        from floati.tui import run_board
        from floati.tui_render import HarborBoardModel

        class TTY(io.StringIO):
            def isatty(self) -> bool:
                return True

            def fileno(self) -> int:
                return 73

        model = HarborBoardModel.from_projection(SNAPSHOT, WORK, RECEIPTS)
        output = TTY()
        with (
            patch.dict(os.environ, {"TERM": "xterm-256color"}, clear=True),
            patch("floati.tui.termios.tcgetattr", return_value=[]),
            patch("floati.tui.termios.tcsetattr", side_effect=OSError("restore")),
            patch("floati.tui.tty.setcbreak"),
            patch("floati.tui._read_terminal_input", side_effect=RuntimeError("primary")),
            patch(
                "floati.tui.shutil.get_terminal_size",
                return_value=os.terminal_size((100, 30)),
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "primary"):
                run_board(
                    model_loader=lambda: model,
                    input_stream=TTY(),
                    output_stream=output,
                    terminal_response=FULL_CAPABILITY_RESPONSE,
                )

        rendered = output.getvalue()
        self.assertIn("\x1b_Ga=d,d=I,q=2,i=7109\x1b\\", rendered)
        self.assertIn("\x1b[?1000l\x1b[?1006l", rendered)
        self.assertTrue(rendered.endswith("\x1b[?25h\x1b[?1049l"))

    def test_terminal_setup_failure_restores_cursor_and_alt_screen(self) -> None:
        """Catches setcbreak failure stranding the user in Floati terminal state."""
        from floati.tui import run_board
        from floati.tui_render import HarborBoardModel

        class TTY(io.StringIO):
            def isatty(self) -> bool:
                return True

            def fileno(self) -> int:
                return 74

        model = HarborBoardModel.from_projection(SNAPSHOT, WORK, RECEIPTS)
        output = TTY()
        with (
            patch.dict(os.environ, {"TERM": "xterm-256color"}, clear=True),
            patch("floati.tui.termios.tcgetattr", return_value=[]),
            patch("floati.tui.termios.tcsetattr"),
            patch("floati.tui.tty.setcbreak", side_effect=OSError("cbreak")),
        ):
            with self.assertRaisesRegex(OSError, "cbreak"):
                run_board(
                    model_loader=lambda: model,
                    input_stream=TTY(),
                    output_stream=output,
                    terminal_response=b"",
                )

        rendered = output.getvalue()
        self.assertTrue(rendered.startswith("\x1b[?1049h\x1b[?25l"))
        self.assertTrue(rendered.endswith("\x1b[?25h\x1b[?1049l"))


class RegattaDegradationTests(unittest.TestCase):
    def test_board_keeps_text_identical_across_256_16_and_monochrome(self) -> None:
        """Catches a signal whose meaning disappears outside the 256-color tier."""
        from floati.tui_render import HarborBoardModel, render_frame

        model = HarborBoardModel.from_projection(SNAPSHOT, WORK, RECEIPTS)
        color256 = render_frame(model, 100, 30, selected=0, color=True, color_tier="256")
        color16 = render_frame(model, 100, 30, selected=0, color=True, color_tier="16")
        mono = render_frame(model, 100, 30, selected=0, color=False, color_tier="mono")
        strip = lambda value: re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", value)

        self.assertIn("\x1b[38;5;208m", color256)
        self.assertIn("\x1b[93m", color16)
        self.assertNotIn("\x1b", mono)
        self.assertEqual(mono, strip(color256))
        self.assertEqual(mono, strip(color16))
        self.assertIn("⊙ FLOATI // HARBOR BOARD", mono)

    def test_interactive_no_color_keeps_protocol_but_removes_palette_bytes(self) -> None:
        """Catches NO_COLOR disabling the TUI instead of only its palette."""
        from floati.tui import run_board
        from floati.tui_render import HarborBoardModel

        class TTY(io.StringIO):
            def isatty(self) -> bool:
                return True

            def fileno(self) -> int:
                return 72

        model = HarborBoardModel.from_projection(SNAPSHOT, WORK, RECEIPTS)
        output = TTY()
        with (
            patch.dict(
                os.environ,
                {"TERM": "xterm-256color", "NO_COLOR": "1"},
                clear=True,
            ),
            patch("floati.tui.termios.tcgetattr", return_value=[]),
            patch("floati.tui.termios.tcsetattr"),
            patch("floati.tui.tty.setcbreak"),
            patch("floati.tui._read_terminal_input", return_value="q"),
            patch(
                "floati.tui.shutil.get_terminal_size",
                return_value=os.terminal_size((100, 30)),
            ),
        ):
            code = run_board(
                model_loader=lambda: model,
                input_stream=TTY(),
                output_stream=output,
                terminal_response=FULL_CAPABILITY_RESPONSE,
            )

        rendered = output.getvalue()
        self.assertEqual(0, code)
        self.assertIn("\x1b[?2026h", rendered)
        self.assertIn("⊙ FLOATI // HARBOR BOARD", rendered)
        self.assertIsNone(re.search(r"\x1b\[[0-9;]*m", rendered))
        self.assertNotIn("\x1b[38;5;", rendered)
        self.assertNotIn("\x1b_Ga=T,", rendered)

    def test_empty_no_color_keeps_the_unset_board_tier(self) -> None:
        """Catches an empty NO_COLOR value suppressing the current color default."""
        from floati.tui import _color_tier

        with patch.dict(os.environ, {"TERM": "xterm-256color"}, clear=True):
            unset = _color_tier()
        with patch.dict(
            os.environ,
            {"TERM": "xterm-256color", "NO_COLOR": ""},
            clear=True,
        ):
            empty = _color_tier()

        self.assertEqual("256", unset)
        self.assertEqual(unset, empty)

    def test_banked_capture_pair_is_current_and_monochrome_is_legible(self) -> None:
        """Catches stale spike captures or a monochrome bank that lost its signal twins."""
        from floati.demo import capture_demo

        root = Path(__file__).parents[1] / "docs" / "evidence" / "captures"
        color = (root / "regatta-spike-color.txt").read_text(encoding="utf-8")
        mono = (root / "regatta-spike-monochrome.txt").read_text(encoding="utf-8")

        self.assertEqual(capture_demo(color=True), color)
        self.assertEqual(capture_demo(color=False), mono)
        self.assertIn("\x1b[38;5;208m", color)
        self.assertNotIn("\x1b", mono)
        for signal in ("! DENIAL", "! STALE", "DEGRADED", "PROCESS DIED"):
            self.assertIn(signal, mono)


if __name__ == "__main__":
    unittest.main()

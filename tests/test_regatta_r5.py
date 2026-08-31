from __future__ import annotations

import hashlib
import io
import json
import os
import re
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.public_projection import projected_role_text
from tests.test_regatta_r1 import _artifact
from tests.test_regatta_spike import FULL_CAPABILITY_RESPONSE
from tests.test_tui_render import RECEIPTS, SNAPSHOT, WORK


SYNC_BEGIN = "\x1b[?2026h"
SYNC_END = "\x1b[?2026l"
KITTY_KEYBOARD_PUSH = "\x1b[>1u"
KITTY_KEYBOARD_POP = "\x1b[<u"
MOUSE_ENABLE = "\x1b[?1000h\x1b[?1006h"
MOUSE_DISABLE = "\x1b[?1000l\x1b[?1006l"


class _TTY(io.StringIO):
    def isatty(self) -> bool:
        return True

    def fileno(self) -> int:
        return 91


class R5AMachineTwinPins(unittest.TestCase):
    def test_plain_and_json_twins_are_byte_identical_before_r5_render_work(self) -> None:
        """Catches the modernization floor leaking protocol dressing into machine output."""
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
            "31fdd93cdc00ff456f440ca3b022c0a16d9b0e88a6c2e0e4be83a86aeb8ce545",
            hashlib.sha256(plain).hexdigest(),
        )
        self.assertEqual(
            "5c86d938487eacd6ca498aa31b0882c6640349d1458e0ff485a9d93f81543034",
            hashlib.sha256(artifact).hexdigest(),
        )


class R5BKeyboardProtocolTests(unittest.TestCase):
    def test_kitty_keyboard_uses_stack_scoped_disambiguation_and_exact_reverse(self) -> None:
        """Catches mode replacement or a cleanup reset that destroys the caller's flags."""
        import floati.tui_protocol as protocol

        mode = getattr(protocol, "kitty_keyboard_mode", None)
        self.assertIsNotNone(mode, "R5 must expose the Kitty keyboard lifecycle")
        self.assertEqual(b"\x1b[>1u", mode(True))
        self.assertEqual(b"\x1b[<u", mode(False))

    def test_decoder_preserves_split_kitty_modified_arrow_and_escape_events(self) -> None:
        """Catches fixed three-byte CSI reads corrupting Kitty keyboard events."""
        from floati.tui_protocol import TerminalInputDecoder

        decoder = TerminalInputDecoder()

        self.assertEqual((), decoder.feed(b"\x1b[1;2"))
        self.assertEqual(
            ("KEY_DOWN", "\x1b", "q"),
            decoder.feed(b"B\x1b[27uq"),
        )


class R5CFullScreenFloorTests(unittest.TestCase):
    def test_board_uses_all_protocols_atomically_and_emits_no_idle_frame(self) -> None:
        """Catches a Board event boundary tearing or a no-op input manufacturing a frame."""
        from floati.tui import run_board
        from floati.tui_render import HarborBoardModel

        model = HarborBoardModel.from_projection(SNAPSHOT, WORK, RECEIPTS)
        events = ["", "j", "", "q"]
        output = _TTY()
        with (
            patch.dict(os.environ, {"TERM": "xterm-256color"}, clear=True),
            patch("floati.tui.termios.tcgetattr", return_value=[]),
            patch("floati.tui.termios.tcsetattr"),
            patch("floati.tui.tty.setcbreak"),
            patch("floati.tui._read_terminal_input", side_effect=lambda *args: events.pop(0)),
            patch(
                "floati.tui.shutil.get_terminal_size",
                return_value=os.terminal_size((100, 30)),
            ),
        ):
            code = run_board(
                model_loader=lambda: model,
                input_stream=_TTY(),
                output_stream=output,
                terminal_response=FULL_CAPABILITY_RESPONSE,
            )

        rendered = output.getvalue()
        self.assertEqual(0, code)
        self.assertEqual(1, rendered.count(KITTY_KEYBOARD_PUSH))
        self.assertEqual(1, rendered.count(KITTY_KEYBOARD_POP))
        self.assertIn(MOUSE_ENABLE, rendered)
        self.assertIn(MOUSE_DISABLE, rendered)
        self.assertEqual(2, rendered.count(SYNC_BEGIN))
        self.assertEqual(rendered.count(SYNC_BEGIN), rendered.count(SYNC_END))
        self.assertLess(rendered.index(KITTY_KEYBOARD_PUSH), rendered.index(SYNC_BEGIN))
        self.assertLess(rendered.rindex(SYNC_END), rendered.index(KITTY_KEYBOARD_POP))
        self.assertLess(rendered.index(KITTY_KEYBOARD_POP), rendered.index("\x1b[?1049l"))

    def test_live_map_uses_all_protocols_atomically_and_emits_no_idle_frame(self) -> None:
        """Catches the Live Map missing the shared floor or redrawing on a no-op event."""
        from floati.tui_chart import run_live_harbor_map

        events = ["", "KEY_DOWN", "", "q"]
        output = _TTY()
        with (
            patch.dict(os.environ, {"TERM": "xterm-256color"}, clear=True),
            patch("floati.tui_chart.termios.tcgetattr", return_value=[]),
            patch("floati.tui_chart.termios.tcsetattr"),
            patch("floati.tui_chart.tty.setcbreak"),
            patch(
                "floati.tui_chart.shutil.get_terminal_size",
                return_value=os.terminal_size((100, 30)),
            ),
        ):
            code = run_live_harbor_map(
                snapshot_loader=lambda: _artifact(include_envelope=False),
                input_stream=_TTY(),
                output_stream=output,
                read_event=lambda timeout: events.pop(0),
                terminal_response=FULL_CAPABILITY_RESPONSE,
                color_tier="mono",
                settled_frames=True,
            )

        rendered = output.getvalue()
        self.assertEqual(0, code)
        self.assertEqual(1, rendered.count(KITTY_KEYBOARD_PUSH))
        self.assertEqual(1, rendered.count(KITTY_KEYBOARD_POP))
        self.assertIn(MOUSE_ENABLE, rendered)
        self.assertIn(MOUSE_DISABLE, rendered)
        self.assertEqual(2, rendered.count(SYNC_BEGIN))
        self.assertEqual(rendered.count(SYNC_BEGIN), rendered.count(SYNC_END))
        self.assertLess(rendered.index(KITTY_KEYBOARD_PUSH), rendered.index(SYNC_BEGIN))
        self.assertLess(rendered.rindex(SYNC_END), rendered.index(KITTY_KEYBOARD_POP))
        self.assertLess(rendered.index(KITTY_KEYBOARD_POP), rendered.index("\x1b[?1049l"))

    def test_board_primary_failure_still_pops_kitty_keyboard_before_alt_screen(self) -> None:
        """Catches an exception leaking the alternate screen's keyboard mode stack."""
        from floati.tui import run_board
        from floati.tui_render import HarborBoardModel

        model = HarborBoardModel.from_projection(SNAPSHOT, WORK, RECEIPTS)
        output = _TTY()
        with (
            patch.dict(os.environ, {"TERM": "xterm-256color"}, clear=True),
            patch("floati.tui.termios.tcgetattr", return_value=[]),
            patch("floati.tui.termios.tcsetattr"),
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
                    input_stream=_TTY(),
                    output_stream=output,
                    terminal_response=FULL_CAPABILITY_RESPONSE,
                )

        rendered = output.getvalue()
        self.assertEqual(1, rendered.count(KITTY_KEYBOARD_PUSH))
        self.assertEqual(1, rendered.count(KITTY_KEYBOARD_POP))
        self.assertLess(rendered.index(KITTY_KEYBOARD_POP), rendered.index("\x1b[?1049l"))

    def test_unmeasured_kitty_keyboard_never_changes_terminal_mode(self) -> None:
        """Catches TERM-brand inference enabling a protocol without a measured receipt."""
        from floati.tui import run_board
        from floati.tui_render import HarborBoardModel

        model = HarborBoardModel.from_projection(SNAPSHOT, WORK, RECEIPTS)
        output = _TTY()
        with (
            patch.dict(os.environ, {"TERM": "xterm-kitty"}, clear=True),
            patch("floati.tui.termios.tcgetattr", return_value=[]),
            patch("floati.tui.termios.tcsetattr"),
            patch("floati.tui.tty.setcbreak"),
            patch("floati.tui._read_terminal_input", return_value="q"),
            patch(
                "floati.tui.shutil.get_terminal_size",
                return_value=os.terminal_size((100, 30)),
            ),
        ):
            run_board(
                model_loader=lambda: model,
                input_stream=_TTY(),
                output_stream=output,
                terminal_response=b"\x1b[?1;2c",
            )

        rendered = output.getvalue()
        self.assertNotIn(KITTY_KEYBOARD_PUSH, rendered)
        self.assertNotIn(KITTY_KEYBOARD_POP, rendered)


class R5DCaptureBankTests(unittest.TestCase):
    def test_board_and_live_map_capture_pairs_are_banked_from_product_renderers(self) -> None:
        """Catches a protocol-only row shipping without current color and mono surfaces."""
        from floati.demo import capture_demo, capture_harbor_map

        captures = Path("docs/evidence/captures")
        expected = (
            ("regatta-r5-board-color.txt", projected_role_text(capture_demo(color=True))),
            ("regatta-r5-board-monochrome.txt", projected_role_text(capture_demo(color=False))),
            ("regatta-r5-live-map-color.txt", projected_role_text(capture_harbor_map(color=True))),
            ("regatta-r5-live-map-monochrome.txt", projected_role_text(capture_harbor_map(color=False))),
        )
        for name, rendered in expected:
            with self.subTest(name=name):
                path = captures / name
                self.assertTrue(path.is_file(), f"missing governed R5 capture: {name}")
                self.assertEqual(rendered.encode("utf-8"), path.read_bytes())
        self.assertIsNone(re.search(r"\x1b", expected[1][1]))
        self.assertIsNone(re.search(r"\x1b", expected[3][1]))


if __name__ == "__main__":
    unittest.main()

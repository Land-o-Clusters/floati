from __future__ import annotations

from floati import fixture_ids as public_ids

import io
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from tests.test_tui_render import RECEIPTS, SNAPSHOT, WORK, strip_ansi


class TuiControlTests(unittest.TestCase):
    def controller(self):
        from floati.tui import BoardController
        from floati.tui_render import HarborBoardModel

        return BoardController(HarborBoardModel.from_projection(SNAPSHOT, WORK, RECEIPTS))

    def test_arrows_vim_enter_ack_and_quit_are_complete_keyboard_path(self) -> None:
        controller = self.controller()

        self.assertEqual(0, controller.selected)
        self.assertEqual("select", controller.handle_key("j").kind)
        self.assertEqual(1, controller.selected)
        controller.handle_key("KEY_DOWN")
        self.assertEqual(1, controller.selected)
        controller.handle_key("k")
        self.assertEqual(0, controller.selected)
        controller.handle_key("KEY_UP")
        self.assertEqual(0, controller.selected)
        self.assertEqual("detail", controller.handle_key("ENTER").kind)
        self.assertTrue(controller.detail_open)
        acknowledged = controller.handle_key("a")
        self.assertEqual("ack", acknowledged.kind)
        self.assertEqual(public_ids.builder('a'), acknowledged.node_id)
        self.assertEqual("msg-018f0f23abcd71238000000000000000", acknowledged.message_id)
        self.assertEqual("quit", controller.handle_key("q").kind)
        self.assertTrue(controller.quit_requested)

    def test_idle_input_does_not_mutate_selection_or_detail(self) -> None:
        controller = self.controller()
        before = (controller.selected, controller.detail_open, controller.quit_requested)

        action = controller.handle_key("")

        self.assertEqual("none", action.kind)
        self.assertEqual(before, (controller.selected, controller.detail_open, controller.quit_requested))

    def test_idle_board_blocks_without_reloading_or_redrawing(self) -> None:
        """Catches a timer waking the Board when neither state nor viewport changed."""
        from floati.tui import run_board
        from floati.tui_render import render_frame

        class TTY(io.StringIO):
            def isatty(self) -> bool:
                return True

            def fileno(self) -> int:
                return 91

        model = self.controller().model
        loads = 0
        events = ["", "", "q"]
        timeouts: list[float | None] = []

        def load_model():
            nonlocal loads
            loads += 1
            return model

        def read_event(timeout):
            timeouts.append(timeout)
            return events.pop(0)

        output = TTY()
        with (
            patch.dict(os.environ, {"TERM": "xterm-256color"}, clear=True),
            patch("floati.tui.termios.tcgetattr", return_value=[]),
            patch("floati.tui.termios.tcsetattr"),
            patch("floati.tui.tty.setcbreak"),
            patch(
                "floati.tui.shutil.get_terminal_size",
                return_value=os.terminal_size((100, 40)),
            ),
            patch("floati.tui.render_frame", wraps=render_frame) as render_spy,
        ):
            code = run_board(
                model_loader=load_model,
                input_stream=TTY(),
                output_stream=output,
                terminal_response=b"\x1b[?1;2c",
                read_event=read_event,
            )

        self.assertEqual(0, code)
        self.assertEqual(1, loads)
        self.assertEqual(1, render_spy.call_count)
        self.assertEqual([None, None, None], timeouts)

    def test_state_and_resize_events_are_the_only_board_redraw_wakeups(self) -> None:
        """Catches an event-driven Board dropping a durable change or a resize."""
        from dataclasses import replace
        from floati.tui import BoardModelEvent, BoardResizeEvent, run_board
        from floati.tui_render import render_frame

        class TTY(io.StringIO):
            def isatty(self) -> bool:
                return True

            def fileno(self) -> int:
                return 92

        initial = self.controller().model
        nodes = [dict(node) for node in initial.nodes]
        nodes[0]["inbox_depth"] = 7
        changed = replace(initial, nodes=tuple(nodes))
        events = [BoardModelEvent(changed), BoardResizeEvent(), "", "q"]
        loads = 0
        timeouts: list[float | None] = []

        def load_model():
            nonlocal loads
            loads += 1
            return initial

        def read_event(timeout):
            timeouts.append(timeout)
            return events.pop(0)

        output = TTY()
        sizes = (
            os.terminal_size((100, 40)),
            os.terminal_size((100, 40)),
            os.terminal_size((120, 40)),
        )
        with (
            patch.dict(os.environ, {"TERM": "xterm-256color"}, clear=True),
            patch("floati.tui.termios.tcgetattr", return_value=[]),
            patch("floati.tui.termios.tcsetattr"),
            patch("floati.tui.tty.setcbreak"),
            patch("floati.tui.shutil.get_terminal_size", side_effect=sizes),
            patch("floati.tui.render_frame", wraps=render_frame) as render_spy,
        ):
            code = run_board(
                model_loader=load_model,
                input_stream=TTY(),
                output_stream=output,
                terminal_response=b"\x1b[?1;2c",
                read_event=read_event,
            )

        self.assertEqual(0, code)
        self.assertEqual(1, loads)
        self.assertEqual(3, render_spy.call_count)
        self.assertEqual([None, None, None, None], timeouts)
        self.assertIn("7  12:00:09.000", strip_ansi(output.getvalue()))

    def test_filesystem_wakeup_reloads_live_board_without_an_injected_event(self) -> None:
        """Catches the production Board blocking forever after a durable append."""
        from dataclasses import replace
        from floati.tui import BoardFilesystemWakeup, run_board

        class TTY(io.StringIO):
            def __init__(self, descriptor: int, value: str = "") -> None:
                super().__init__(value)
                self._descriptor = descriptor

            def isatty(self) -> bool:
                return True

            def fileno(self) -> int:
                return self._descriptor

        initial = self.controller().model
        nodes = [dict(node) for node in initial.nodes]
        nodes[0]["inbox_depth"] = 7
        changed = replace(initial, nodes=tuple(nodes))
        loads = 0
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            ledger = root / "events.jsonl"
            ledger.write_text("", encoding="utf-8")
            wakeup = BoardFilesystemWakeup(root)
            read_descriptor, write_descriptor = os.pipe()
            os.write(write_descriptor, b"q")

            def load_model():
                nonlocal loads
                loads += 1
                if loads == 1:
                    ledger.write_text("durable append\n", encoding="utf-8")
                    return initial
                return changed

            output = TTY(read_descriptor)
            try:
                with (
                    patch.dict(os.environ, {"TERM": "xterm-256color"}, clear=True),
                    patch("floati.tui.termios.tcgetattr", return_value=[]),
                    patch("floati.tui.termios.tcsetattr"),
                    patch("floati.tui.tty.setcbreak"),
                    patch(
                        "floati.tui.shutil.get_terminal_size",
                        return_value=os.terminal_size((100, 40)),
                    ),
                ):
                    code = run_board(
                        model_loader=load_model,
                        model_wakeup=wakeup,
                        input_stream=output,
                        output_stream=output,
                        terminal_response=b"\x1b[?1;2c",
                    )
            finally:
                wakeup.close()
                os.close(read_descriptor)
                os.close(write_descriptor)

        self.assertEqual(0, code)
        self.assertEqual(2, loads)
        self.assertIn("7  12:00:09.000", strip_ansi(output.getvalue()))

    def test_filesystem_wakeup_refuses_incomplete_watch_coverage(self) -> None:
        """Catches an unreadable ledger path silently leaving the Board stale."""
        from floati.errors import ProtocolRefusal
        from floati import tui

        class Call:
            def __init__(self, result: int) -> None:
                self.result = result
                self.argtypes = None
                self.restype = None

            def __call__(self, *_args):
                return self.result

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "events.jsonl").write_text("", encoding="utf-8")
            if hasattr(tui.select, "kqueue"):
                with patch("floati.tui.os.open", side_effect=PermissionError("denied")):
                    with self.assertRaises(ProtocolRefusal) as refusal:
                        tui.BoardFilesystemWakeup(root)
            else:
                read_descriptor, write_descriptor = os.pipe()
                libc = type(
                    "FakeLibc",
                    (),
                    {
                        "inotify_init1": Call(read_descriptor),
                        "inotify_add_watch": Call(-1),
                    },
                )()
                try:
                    with (
                        patch("floati.tui.ctypes.CDLL", return_value=libc),
                        patch("floati.tui.ctypes.get_errno", return_value=13),
                    ):
                        with self.assertRaises(ProtocolRefusal) as refusal:
                            tui.BoardFilesystemWakeup(root)
                finally:
                    os.close(write_descriptor)

        self.assertEqual("board_event_watch_unavailable", refusal.exception.code)

    def test_inotify_registration_failure_is_typed_and_closes_descriptor(self) -> None:
        """Catches Linux event registration failing open or leaking its source fd."""
        from floati.errors import ProtocolRefusal
        from floati.tui import _InotifyBoardFilesystemWakeup

        class Call:
            def __init__(self, result: int) -> None:
                self.result = result
                self.argtypes = None
                self.restype = None

            def __call__(self, *_args):
                return self.result

        read_descriptor, write_descriptor = os.pipe()
        libc = type(
            "FakeLibc",
            (),
            {
                "inotify_init1": Call(read_descriptor),
                "inotify_add_watch": Call(-1),
            },
        )()
        try:
            with tempfile.TemporaryDirectory() as temporary:
                with (
                    patch("floati.tui.ctypes.CDLL", return_value=libc),
                    patch("floati.tui.ctypes.get_errno", return_value=13),
                ):
                    with self.assertRaises(ProtocolRefusal) as refusal:
                        _InotifyBoardFilesystemWakeup(Path(temporary))
            self.assertEqual("board_event_watch_unavailable", refusal.exception.code)
            with self.assertRaises(OSError):
                os.fstat(read_descriptor)
        finally:
            os.close(write_descriptor)

    def test_inotify_refresh_rebinds_a_recreated_same_path_directory(self) -> None:
        """Catches an inode-bound watch surviving only as a stale path mapping."""
        from floati.tui import _InotifyBoardFilesystemWakeup

        class Call:
            def __init__(self, results: list[int]) -> None:
                self.results = results
                self.argtypes = None
                self.restype = None
                self.calls = 0

            def __call__(self, *_args):
                result = self.results[min(self.calls, len(self.results) - 1)]
                self.calls += 1
                return result

        read_descriptor, write_descriptor = os.pipe()
        add_watch = Call([7, 8])
        libc = type(
            "FakeLibc",
            (),
            {
                "inotify_init1": Call([read_descriptor]),
                "inotify_add_watch": add_watch,
            },
        )()
        try:
            with tempfile.TemporaryDirectory() as temporary:
                with patch("floati.tui.ctypes.CDLL", return_value=libc):
                    wakeup = _InotifyBoardFilesystemWakeup(Path(temporary))
                    self.assertEqual(7, wakeup._watches[Path(temporary)])
                    wakeup._refresh()
                    self.assertEqual(8, wakeup._watches[Path(temporary)])
                    self.assertEqual(2, add_watch.calls)
                    wakeup.close()
        finally:
            os.close(write_descriptor)

    def test_observation_clock_alone_does_not_trigger_state_animation(self) -> None:
        from dataclasses import replace
        from floati.tui import state_signature

        model = self.controller().model
        later = replace(model, observed_at="2026-07-31T12:00:11.000Z")
        self.assertEqual(state_signature(model), state_signature(later))

    def test_worker_receipt_change_triggers_board_redraw(self) -> None:
        from dataclasses import replace
        from floati.tui import state_signature

        model = self.controller().model
        changed = replace(
            model,
            workers=(*model.workers, {"session_id": "new", "state": "claim"}),
        )
        self.assertNotEqual(state_signature(model), state_signature(changed))

    def test_non_terminal_run_uses_one_plain_final_frame(self) -> None:
        from floati.tui import run_board
        from floati.tui_render import HarborBoardModel

        output = io.StringIO()
        code = run_board(
            model_loader=lambda: HarborBoardModel.from_projection(SNAPSHOT, WORK, RECEIPTS),
            input_stream=io.StringIO(),
            output_stream=output,
        )

        self.assertEqual(0, code)
        self.assertTrue(output.getvalue().startswith("PLAIN DUMP\n"))
        self.assertEqual(1, output.getvalue().count("PLAIN DUMP\n"))

    def test_visible_ack_action_uses_the_durable_acknowledgment_core(self) -> None:
        from floati.cursor import SparseCursor
        from floati.demo import build_demo_model, seed_demo
        from floati.tui import BoardController, acknowledge_visible

        with tempfile.TemporaryDirectory() as temporary:
            root = seed_demo(Path(temporary) / "fleet")
            controller = BoardController(build_demo_model(root))
            controller.selected = next(
                index for index, node in enumerate(controller.model.nodes)
                if node.get("visible_message_id")
            )
            action = controller.handle_key("a")
            acknowledge_visible(root, action, acting_session_id="tui-test-session")

            self.assertIn(action.message_id, SparseCursor(root).acked_ids(action.node_id))

    def test_tui_and_supervisor_are_projection_only_and_append_no_effect_rows(self) -> None:
        from floati.effects import EffectLedger
        from floati.root import FloatiRoot
        from floati.supervisor import Supervisor
        from floati.tui import model_from_root
        from tests.test_effect_cli import lifecycle_rows, write_effect_rows

        with tempfile.TemporaryDirectory() as temporary:
            root = FloatiRoot.open_direct_home(Path(temporary) / "alpha", create=True)
            _, rows = lifecycle_rows("unknown")
            write_effect_rows(root, rows)
            ledger_path = root.resolve_relative(EffectLedger.relative_path)
            before = ledger_path.read_bytes()

            model = model_from_root(
                root, datetime(2026, 8, 13, 12, 0, tzinfo=timezone.utc)
            )
            supervised = Supervisor(root).snapshot(
                datetime(2026, 8, 13, 12, 0, tzinfo=timezone.utc)
            )

            self.assertEqual(before, ledger_path.read_bytes())
            self.assertEqual(1, model.effects["counts"]["unknown"])
            self.assertEqual(1, supervised["effects"]["counts"]["unknown"])


if __name__ == "__main__":
    unittest.main()

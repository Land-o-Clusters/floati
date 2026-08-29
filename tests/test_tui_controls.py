from __future__ import annotations

import io
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from tests.test_tui_render import RECEIPTS, SNAPSHOT, WORK


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
        self.assertEqual("lane-a", acknowledged.node_id)
        self.assertEqual("msg-018f0f23abcd71238000000000000000", acknowledged.message_id)
        self.assertEqual("quit", controller.handle_key("q").kind)
        self.assertTrue(controller.quit_requested)

    def test_idle_input_does_not_mutate_selection_or_detail(self) -> None:
        controller = self.controller()
        before = (controller.selected, controller.detail_open, controller.quit_requested)

        action = controller.handle_key("")

        self.assertEqual("none", action.kind)
        self.assertEqual(before, (controller.selected, controller.detail_open, controller.quit_requested))

    def test_redraw_interval_never_exceeds_250_milliseconds(self) -> None:
        from floati.tui import REDRAW_INTERVAL_SECONDS

        self.assertGreater(REDRAW_INTERVAL_SECONDS, 0)
        self.assertLessEqual(REDRAW_INTERVAL_SECONDS, 0.25)

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

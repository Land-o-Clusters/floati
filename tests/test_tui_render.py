from __future__ import annotations

import re
import unittest
from dataclasses import replace


SNAPSHOT = {
    "observed_at": "2026-07-31T12:00:10.000Z",
    "nodes": [
        {"node_id": "lane-a", "role": "Codex", "liveness": "present", "authority": "active", "mutex": "none", "inbox_depth": 2, "last_activity": "2026-07-31T12:00:09.000Z", "visible_message_id": "msg-018f0f23abcd71238000000000000000"},
        {"node_id": "lane-b-with-a-name-that-must-clip", "role": "Claude", "liveness": "expired", "authority": "none", "mutex": "expired", "inbox_depth": 0, "last_activity": "2026-07-31T11:59:00.000Z", "visible_message_id": None},
    ],
    "stale_leases": [{"plane": "mutex", "subject_id": "workspace", "holder": "lane-b-with-a-name-that-must-clip", "epoch": 1, "expires_at": "2026-07-31T12:00:05.000Z"}],
    "work_counts": {"open": 1, "claimed": 1, "completed": 1},
    "receipt_counts": {"delivery": 2, "ack": 1, "denial": 1},
    "workers": [
        {"session_id": "worker-a", "node_id": "lane-a", "adapter": "codex", "state": "claim", "outcome_code": None},
        {"session_id": "worker-b", "node_id": "lane-a", "adapter": "codex", "state": "driving", "outcome_code": None},
        {"session_id": "worker-c", "node_id": "lane-b", "adapter": "acp", "state": "degraded", "outcome_code": "process_died"},
        {"session_id": "worker-d", "node_id": "lane-b", "adapter": "acp", "state": "complete", "outcome_code": None},
        {"session_id": "worker-e", "node_id": "lane-c", "adapter": "codex", "state": "degraded", "outcome_code": "process_cancelled"},
        {"session_id": "worker-f", "work_item_id": "work-f", "node_id": "lane-d", "adapter": "codex", "state": "degraded", "outcome_code": "authority_expired_mid_claim"},
    ],
}
WORK = [
    {"id": "work-a", "title": "frame mail", "owner": "lane-a", "state": "open", "readiness": "ready", "needs": [], "holder": None, "last_activity": "2026-07-31T12:00:01.000Z"},
    {"id": "work-b", "title": "build harbor board", "owner": "lane-a", "state": "open", "readiness": "blocked", "needs": ["work-a"], "holder": None, "last_activity": "2026-07-31T12:00:02.000Z"},
    {"id": "work-c", "title": "study acp", "owner": "lane-b", "state": "claimed", "readiness": "claimed", "needs": [], "holder": "lane-b", "last_activity": "2026-07-31T12:00:03.000Z"},
    {"id": "work-d", "title": "publish proof", "owner": "lane-b", "state": "completed", "readiness": "done", "needs": ["work-a", "work-c"], "holder": "lane-b", "last_activity": "2026-07-31T12:00:04.000Z"},
]
RECEIPTS = {
    "deliveries": [{"kind": "delivery_receipt", "id": "delivery-1", "timestamp": "2026-07-31T12:00:04.000Z", "item_ids": ["msg-a"]}],
    "acks": [{"kind": "ack_receipt", "id": "ack-1", "timestamp": "2026-07-31T12:00:05.000Z", "item_ids": ["msg-a"]}],
    "denials": [{"kind": "denial_receipt", "id": "denial-1", "timestamp": "2026-07-31T12:00:06.000Z", "claimed_sender": "ghost", "claimed_recipient": "lane-a", "reason_code": "unknown_sender"}],
    "workers": [{"kind": "worker_receipt", "id": "worker-receipt-1", "timestamp": "2026-07-31T12:00:07.000Z", "node_id": "lane-a", "transition": "drive", "outcome_code": None}],
}


def strip_ansi(value: str) -> str:
    return re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", value)


class TuiRenderTests(unittest.TestCase):
    def model(self):
        from floati.tui_render import HarborBoardModel

        return HarborBoardModel.from_projection(SNAPSHOT, WORK, RECEIPTS)

    def test_frame_keeps_three_plane_lamps_and_surfaces_denial_loudly(self) -> None:
        from floati.tui_render import render_frame

        frame = render_frame(self.model(), 100, 30, selected=0, color=False)

        self.assertIn("FLOATI // HARBOR BOARD", frame)
        self.assertIn("LIVE", frame)
        self.assertIn("AUTH", frame)
        self.assertIn("MUTEX", frame)
        self.assertIn("lane-a", frame)
        self.assertIn("PRESENT", frame)
        self.assertIn("ACTIVE", frame)
        self.assertIn("UNKNOWN_SENDER", frame)
        self.assertLess(frame.index("! DENIAL"), frame.index("WORK DAG"))
        self.assertIn("↑/k ↓/j select", frame)
        self.assertIn("a ack", frame)
        self.assertIn("q quit", frame)

    def test_color_uses_buoy_orange_and_harbor_slate_without_carrying_meaning(self) -> None:
        from floati.tui_render import render_frame

        color = render_frame(self.model(), 100, 30, selected=0, color=True)
        mono = render_frame(self.model(), 100, 30, selected=0, color=False)

        self.assertIn("\x1b[38;5;208m", color)
        self.assertNotIn("\x1b[48;5;236m", color)
        self.assertNotIn("\x1b", mono)
        self.assertEqual(mono, strip_ansi(color))

    def test_semantic_accent_marks_attention_and_activity_not_the_brand(self) -> None:
        from floati.tui_render import BUOY_ORANGE, RESET, render_frame

        frame = render_frame(self.model(), 120, 40, selected=0, color=True)
        lines = frame.splitlines()

        self.assertEqual("⊙ FLOATI // HARBOR BOARD", lines[0])
        self.assertTrue(next(line for line in lines if "! DENIAL" in line).startswith(BUOY_ORANGE))
        degraded = next(line for line in lines if "DEGRADED" in line)
        driving = next(line for line in lines if "DRIVING" in line)
        self.assertTrue(degraded.startswith(BUOY_ORANGE))
        self.assertTrue(degraded.endswith(RESET))
        self.assertTrue(driving.startswith(BUOY_ORANGE))
        self.assertTrue(driving.endswith(RESET))
        self.assertIn(BUOY_ORANGE + "▓", next(line for line in lines if "WORK DAG" in line))
        self.assertTrue(next(line for line in lines if strip_ansi(line).startswith("> ")).startswith(BUOY_ORANGE))
        self.assertTrue(next(line for line in lines if "! DENIAL" in line).endswith(RESET))

    def test_frame_is_viewport_bounded_and_plain_dump_is_distinguishable(self) -> None:
        from floati.tui_render import render_frame, render_plain_dump

        frame = render_frame(self.model(), 72, 16, selected=1, color=False)
        dump = render_plain_dump(self.model(), width=72)

        self.assertLessEqual(len(frame.splitlines()), 16)
        self.assertTrue(all(len(line) <= 72 for line in frame.splitlines()))
        self.assertTrue(dump.startswith("PLAIN DUMP\n"))
        self.assertNotEqual(frame, dump)
        self.assertNotIn("\x1b", dump)

    def test_honest_overshoot_never_inflates_printed_digits(self) -> None:
        from floati.tui_render import honest_percent, settle_geometry

        geometry = settle_geometry(0.60, 0.70)

        self.assertGreater(geometry, 0.60)
        self.assertEqual(60, honest_percent(0.60))
        self.assertEqual(100, honest_percent(1.20))

    def test_enter_detail_panel_exposes_selected_node_facts(self) -> None:
        from floati.tui_render import render_frame

        frame = render_frame(
            self.model(), 100, 30, selected=0, color=False, detail_open=True
        )

        self.assertIn("DETAIL lane-a", frame)
        self.assertIn("ROLE Codex", frame)
        self.assertIn("VISIBLE MAIL msg-018f0f23abcd71238000000000000000", frame)

    def test_board_renders_directed_and_state_flushed_tide_flags(self) -> None:
        from floati.tui_render import render_frame

        for state in ("directed", "state_flushed"):
            with self.subTest(state=state):
                snapshot = dict(SNAPSHOT)
                nodes = [dict(node) for node in SNAPSHOT["nodes"]]
                nodes[0]["tide"] = {"policy": "active", "turnover_state": state}
                snapshot["nodes"] = nodes
                from floati.tui_render import HarborBoardModel

                model = HarborBoardModel.from_projection(snapshot, WORK, RECEIPTS)
                frame = render_frame(
                    model, 140, 34, selected=0, color=False, detail_open=True
                )

                self.assertIn(f"TIDE {state.upper()}", frame)

    def test_worker_rows_render_only_receipt_projected_states(self) -> None:
        from floati.tui_render import render_plain_dump

        frame = render_plain_dump(self.model(), width=100)

        self.assertIn("WORKERS", frame)
        self.assertIn("CLAIM", frame)
        self.assertIn("DRIVING", frame)
        self.assertIn("DEGRADED", frame)
        self.assertIn("COMPLETE", frame)
        self.assertIn("PROCESS DIED", frame)
        self.assertIn("PROCESS CANCELLED", frame)
        self.assertIn("AUTHORITY EXPIRED", frame)
        self.assertIn("work-f", frame)

    def test_dag_panel_and_receipt_ticker_show_real_orchestration_states(self) -> None:
        from floati.tui_render import render_plain_dump

        frame = render_plain_dump(self.model(), width=120)

        for state in ("READY", "BLOCKED", "CLAIMED", "DONE"):
            self.assertIn(state, frame)
        self.assertIn("needs:work-a", frame)
        self.assertIn("WORKER", frame)
        self.assertIn("lane-a DRIVE", frame)

    def test_idle_board_collapses_empty_instrument_panels_into_one_calm_row(self) -> None:
        from floati.tui_render import HarborBoardModel, render_frame

        idle = HarborBoardModel(
            observed_at="2026-08-01T12:00:10.000Z",
            nodes=(), work_items=(), deliveries=(), acknowledgments=(),
            denials=(), stale_leases=(),
            consumption={"coordinate": "work/items.jsonl", "state": "caught_up"},
        )
        frame = render_frame(idle, 120, 34, selected=0, color=False)
        summary = next(line for line in frame.splitlines() if line.startswith("WORK DAG"))

        self.assertIn("CONSUMPTION", summary)
        self.assertIn("WORKERS NONE", summary)
        self.assertIn("RECEIPTS NONE", summary)
        self.assertLessEqual(len(frame.splitlines()), 9)

    def test_worker_rows_pair_title_before_a_deterministically_shortened_work_id(self) -> None:
        from floati.tui_render import render_plain_dump

        long_id = "work-018f0f23abcd71238000000000000000"
        model = self.model()
        work = [dict(item) for item in model.work_items]
        workers = [dict(item) for item in model.workers]
        work[0]["id"] = long_id
        work[0]["title"] = "frame mail"
        workers[1]["work_item_id"] = long_id
        frame = render_plain_dump(replace(model, work_items=work, workers=workers), width=120)
        worker_line = next(line for line in frame.splitlines() if "DRIVING" in line)

        self.assertLess(worker_line.index("frame mail"), worker_line.index("work-…000000"))
        self.assertNotIn(long_id, worker_line)

    def test_degraded_urgency_is_ordered_and_receipt_does_not_echo_outcome(self) -> None:
        from floati.tui_render import render_plain_dump

        model = self.model()
        receipt = {
            "kind": "worker_receipt",
            "timestamp": "2026-07-31T12:00:08.000Z",
            "node_id": "lane-b",
            "transition": "degrade",
            "outcome_code": "process_died",
        }
        degraded = replace(
            model,
            consumption={"coordinate": "work/items.jsonl", "state": "caught_up", "wake_state": "unsatisfied_wake"},
            worker_receipts=(receipt,),
        )
        frame = render_plain_dump(degraded, width=120)

        self.assertLess(frame.index("! DENIAL"), frame.index("UNSATISFIED WAKE"))
        self.assertLess(frame.index("UNSATISFIED WAKE"), frame.index("DEGRADED"))
        self.assertEqual(1, frame.count("PROCESS DIED"))

    def test_plain_dump_uses_prefix_without_repeating_standard_board_header(self) -> None:
        from floati.tui_render import render_plain_dump

        frame = render_plain_dump(self.model(), width=100)

        self.assertTrue(frame.startswith("PLAIN DUMP\nOBSERVED "))
        self.assertNotIn("FLOATI // HARBOR BOARD", frame)
        self.assertNotIn("↑/k ↓/j select", frame)
        self.assertEqual(1, frame.count(self.model().observed_at))

    def test_denials_are_newest_two_unique_groups_with_honest_counts(self) -> None:
        from floati.tui_render import render_plain_dump

        denials = (
            {"reason_code": "oldest", "claimed_sender": "a", "claimed_recipient": "b"},
            {"reason_code": "duplicate", "claimed_sender": "ghost", "claimed_recipient": "lane-a"},
            {"reason_code": "middle", "claimed_sender": "c", "claimed_recipient": "d"},
            {"reason_code": "duplicate", "claimed_sender": "ghost", "claimed_recipient": "lane-a"},
            {"reason_code": "newest", "claimed_sender": "e", "claimed_recipient": "f"},
        )

        frame = render_plain_dump(replace(self.model(), denials=denials), width=120)

        self.assertIn("! DENIAL NEWEST e → f", frame)
        self.assertIn("! DENIAL DUPLICATE ghost → lane-a ×2", frame)
        self.assertNotIn("! DENIAL MIDDLE", frame)
        self.assertNotIn("! DENIAL OLDEST", frame)
        self.assertIn("+2 older denials · floati log to list", frame)

    def test_node_column_fits_longest_registered_identity_without_truncation(self) -> None:
        from floati.tui_render import render_plain_dump

        frame = render_plain_dump(self.model(), width=120)

        node_line = next(line for line in frame.splitlines() if "EXPIRED    NONE" in line)
        self.assertIn("lane-b-with-a-name-that-must-clip", node_line)
        self.assertNotIn("lane-b-with-a-name…", node_line)

    def test_unknown_effect_is_visually_distinct_and_precedes_ordinary_failure(self) -> None:
        from floati.tui_render import render_plain_dump

        model = replace(
            self.model(),
            effects={
                "attention": [
                    {"state": "unknown", "count": 1},
                    {"state": "incomplete", "count": 1},
                    {"state": "failed", "count": 1},
                    {"state": "confirmed", "count": 2},
                ],
                "compensation_counts": {"none": 3, "proposed": 1, "executed": 0},
            },
        )

        frame = render_plain_dump(model, width=120)

        self.assertIn("!! EFFECT UNKNOWN 1", frame)
        self.assertIn("!! EFFECT INCOMPLETE 1", frame)
        self.assertIn("! EFFECT FAILED 1", frame)
        self.assertLess(frame.index("!! EFFECT UNKNOWN"), frame.index("! EFFECT FAILED"))


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

from floati import fixture_ids as public_ids

import hashlib
import io
import json
import os
import re
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.public_projection import projected_role_text
from tests.test_tui_render import RECEIPTS, SNAPSHOT, WORK


ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _artifact(*, include_envelope: bool) -> dict[str, object]:
    artifact: dict[str, object] = {
        "schema_version": 0,
        "source": "declared_roots_and_ledgers",
        "buses": [
            {
                "bus_id": "alpha",
                "architect_node": "architect-a",
                "last_activity_age_seconds": 12,
                "ledger_event_count": 18,
                "nodes": [
                    {
                        "id": "architect-a",
                        "role": "Architect",
                        "last_activity_age_seconds": 12,
                        "inbox_count": 1,
                        "receipt_count": 9,
                    },
                    {
                        "id": public_ids.builder('a'),
                        "role": "Codex",
                        "last_activity_age_seconds": 75,
                        "inbox_count": 0,
                        "receipt_count": 4,
                    },
                ],
                "downstream": ["beta"],
            },
            {
                "bus_id": "beta",
                "architect_node": "architect-b",
                "last_activity_age_seconds": 305,
                "ledger_event_count": 7,
                "nodes": [
                    {
                        "id": "architect-b",
                        "role": "Architect",
                        "last_activity_age_seconds": 305,
                        "inbox_count": 0,
                        "receipt_count": 3,
                    },
                    {
                        "id": public_ids.builder('b'),
                        "role": "Claude",
                        "last_activity_age_seconds": 31,
                        "inbox_count": 2,
                        "receipt_count": 6,
                    },
                ],
                "downstream": [],
            },
        ],
        "relationships": [{"source": "alpha", "target": "beta"}],
        "envelopes": [],
    }
    if include_envelope:
        artifact["envelopes"] = [
            {
                "id": "envelope-002",
                "source_bus": "alpha",
                "sender": public_ids.builder('a'),
                "target_bus": "beta",
                "recipient": public_ids.builder('b'),
            }
        ]
    return artifact


class RegattaR1MachineTwinTests(unittest.TestCase):
    def test_existing_plain_and_json_twins_remain_byte_identical(self) -> None:
        """Catches the live-map dressing leaking into stable machine surfaces."""
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


class RegattaR1LiveMapTests(unittest.TestCase):
    def test_empty_no_color_keeps_the_unset_live_map_tier(self) -> None:
        """Catches an empty NO_COLOR value suppressing the live map palette."""
        from floati.tui_chart import _color_tier

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

    def test_nonempty_no_color_suppresses_live_map_sgr_unless_tier_is_explicit(self) -> None:
        """Catches live-map policy leaking SGR or overriding an explicit tier."""
        from floati.tui_chart import run_live_harbor_map

        class TTY(io.StringIO):
            def isatty(self) -> bool:
                return True

            def fileno(self) -> int:
                return 80

        def render(color_tier: str | None) -> str:
            output = TTY()
            events = ["q"]
            with (
                patch.dict(
                    os.environ,
                    {"TERM": "xterm-256color", "NO_COLOR": "1"},
                    clear=True,
                ),
                patch("floati.tui_chart.termios.tcgetattr", return_value=[]),
                patch("floati.tui_chart.termios.tcsetattr"),
                patch("floati.tui_chart.tty.setcbreak"),
                patch(
                    "floati.tui_chart.shutil.get_terminal_size",
                    return_value=os.terminal_size((80, 24)),
                ),
            ):
                run_live_harbor_map(
                    snapshot_loader=lambda: _artifact(include_envelope=False),
                    input_stream=TTY(),
                    output_stream=output,
                    read_event=lambda timeout: events.pop(0),
                    color_tier=color_tier,
                )
            return output.getvalue()

        suppressed = render(None)
        overridden = render("256")

        self.assertIsNone(ANSI.search(suppressed))
        self.assertIsNotNone(ANSI.search(overridden))
        self.assertIn("FLOATI // LIVE HARBOR MAP", suppressed)

    def test_envelope_pulse_is_new_event_only_three_frames_and_then_idle(self) -> None:
        """Catches ambient animation or one ledger event replaying more than once."""
        from floati.tui_chart import LiveHarborMapController

        controller = LiveHarborMapController(_artifact(include_envelope=False))
        self.assertEqual((), controller.active_pulses(10.0))
        self.assertIsNone(controller.next_wakeup(10.0))

        self.assertTrue(controller.update(_artifact(include_envelope=True), observed_at=10.0))
        self.assertEqual([0, 1, 2], [
            controller.active_pulses(moment)[0].frame_index
            for moment in (10.0, 10.05, 10.10)
        ])
        self.assertEqual((), controller.active_pulses(10.151))
        self.assertIsNone(controller.next_wakeup(10.151))
        self.assertFalse(controller.update(_artifact(include_envelope=True), observed_at=11.0))

    def test_live_envelope_window_and_active_pulses_are_bounded(self) -> None:
        """Catches an overnight map retaining an unbounded envelope-id history."""
        from floati.tui_chart import MAX_ACTIVE_PULSES, MAX_TRACKED_ENVELOPES
        from floati.tui_chart import LiveHarborMapController

        artifact = _artifact(include_envelope=False)
        artifact["envelopes"] = [
            {
                "id": f"envelope-{index:05d}",
                "source_bus": "alpha",
                "sender": public_ids.builder('a'),
                "target_bus": "beta",
                "recipient": public_ids.builder('b'),
            }
            for index in range(MAX_TRACKED_ENVELOPES + 20)
        ]
        controller = LiveHarborMapController(_artifact(include_envelope=False))
        controller.update(artifact, observed_at=10.0)

        self.assertEqual(MAX_TRACKED_ENVELOPES, len(controller._seen_envelopes))
        self.assertLessEqual(len(controller.active_pulses(10.0)), MAX_ACTIVE_PULSES)

    def test_map_is_cartography_with_lamps_channels_and_information_equal_tiers(self) -> None:
        """Catches R1 collapsing into a table or color carrying unique meaning."""
        from floati.tui_chart import LiveHarborMapController
        from floati.tui_chart_render import render_live_harbor_map

        controller = LiveHarborMapController(_artifact(include_envelope=False))
        controller.update(_artifact(include_envelope=True), observed_at=10.0)
        color = render_live_harbor_map(
            controller.artifact,
            selected=controller.selected_target,
            detail_open=True,
            pulses=controller.active_pulses(10.05),
            width=100,
            height=30,
            color_tier="256",
        )
        mono = render_live_harbor_map(
            controller.artifact,
            selected=controller.selected_target,
            detail_open=True,
            pulses=controller.active_pulses(10.05),
            width=100,
            height=30,
            color_tier="mono",
        )
        color16 = render_live_harbor_map(
            controller.artifact,
            selected=controller.selected_target,
            detail_open=True,
            pulses=controller.active_pulses(10.05),
            width=100,
            height=30,
            color_tier="16",
        )

        self.assertEqual(ANSI.sub("", color.text), mono.text)
        self.assertEqual(ANSI.sub("", color16.text), mono.text)
        self.assertIn("\x1b[92m", color16.text)
        for signal in ("FLOATI // LIVE HARBOR MAP", "⚑", "▤", "●", "◐", "○"):
            self.assertIn(signal, mono.text)
        self.assertIn(public_ids.builder('a'), mono.text)
        self.assertIn(public_ids.builder('b'), mono.text)
        self.assertRegex(mono.text, public_ids.compose(public_ids.builder('a'), ' .*●.* ', public_ids.builder('b')))
        self.assertTrue(all(len(line) <= 100 for line in mono.text.splitlines()))
        self.assertLessEqual(len(mono.text.splitlines()), 30)

    def test_keyboard_and_mouse_open_the_same_vessel_detail_from_rendered_hits(self) -> None:
        """Catches mouse geometry and keyboard focus resolving different targets."""
        from floati.tui_chart import LiveHarborMapController
        from floati.tui_chart_render import render_live_harbor_map
        from floati.tui_protocol import MouseEvent

        controller = LiveHarborMapController(_artifact(include_envelope=False))
        controller.handle_key("KEY_DOWN")
        keyboard_target = controller.selected_target
        self.assertEqual("architect-a", keyboard_target.node_id)
        self.assertEqual("detail", controller.handle_key("ENTER").kind)

        controller = LiveHarborMapController(_artifact(include_envelope=False))
        rendered = render_live_harbor_map(
            controller.artifact,
            selected=controller.selected_target,
            detail_open=False,
            pulses=(),
            width=80,
            height=24,
            color_tier="mono",
        )
        hit = next(region for region in rendered.hit_regions if region.target == keyboard_target)
        action = controller.handle_mouse(
            MouseEvent(
                button=0,
                column=hit.left,
                row=hit.row,
                pressed=True,
            ),
            hit_regions=rendered.hit_regions,
        )
        self.assertEqual("detail", action.kind)
        self.assertEqual(keyboard_target, controller.selected_target)
        self.assertTrue(controller.detail_open)

    def test_pier_detail_and_clipped_targets_are_honest(self) -> None:
        """Catches invisible vessels remaining clickable or pier detail inventing counts."""
        from floati.tui_chart import LiveHarborMapController
        from floati.tui_chart_render import render_live_harbor_map

        controller = LiveHarborMapController(_artifact(include_envelope=False))
        self.assertEqual("pier", controller.selected_target.kind)
        controller.handle_key("ENTER")
        rendered = render_live_harbor_map(
            controller.artifact,
            selected=controller.selected_target,
            detail_open=controller.detail_open,
            pulses=(),
            width=80,
            height=9,
            color_tier="mono",
        )
        self.assertIn("18", rendered.text)
        self.assertTrue(all(region.row <= 9 for region in rendered.hit_regions))
        self.assertFalse(any(region.target.bus_id == "beta" for region in rendered.hit_regions))

    def test_live_loop_routes_rendered_mouse_hits_and_cleans_up_protocols(self) -> None:
        """Catches a pure map mockup shipping without a real interactive terminal loop."""
        from floati.tui_chart import run_live_harbor_map
        from floati.tui_protocol import MouseEvent

        class TTY(io.StringIO):
            def isatty(self) -> bool:
                return True

            def fileno(self) -> int:
                return 81

        events = [
            MouseEvent(button=0, column=2, row=6, pressed=True),
            "q",
        ]
        output = TTY()
        with (
            patch.dict(os.environ, {"TERM": "xterm-256color"}, clear=True),
            patch("floati.tui_chart.termios.tcgetattr", return_value=[]),
            patch("floati.tui_chart.termios.tcsetattr"),
            patch("floati.tui_chart.tty.setcbreak"),
            patch(
                "floati.tui_chart.shutil.get_terminal_size",
                return_value=os.terminal_size((80, 24)),
            ),
        ):
            code = run_live_harbor_map(
                snapshot_loader=lambda: _artifact(include_envelope=False),
                input_stream=TTY(),
                output_stream=output,
                read_event=lambda timeout: events.pop(0),
                color_tier="mono",
            )

        rendered = output.getvalue()
        self.assertEqual(0, code)
        self.assertIn("\x1b[?1000h\x1b[?1006h", rendered)
        self.assertIn("\x1b[?2026h", rendered)
        self.assertIn("DETAIL // VESSEL architect-a", rendered)
        self.assertIn("\x1b[?1000l\x1b[?1006l", rendered)
        self.assertTrue(rendered.endswith("\x1b[?25h\x1b[?1049l"))

    def test_live_loop_has_zero_idle_redraws(self) -> None:
        """Catches the live label introducing an ambient render or frame timer."""
        from floati.tui_chart import run_live_harbor_map
        from floati.tui_chart_render import render_live_harbor_map

        class TTY(io.StringIO):
            def isatty(self) -> bool:
                return True

            def fileno(self) -> int:
                return 82

        events = ["", "", "q"]
        timeouts: list[float | None] = []
        loads = 0

        def load_snapshot() -> dict[str, object]:
            nonlocal loads
            loads += 1
            return _artifact(include_envelope=False)

        def read_event(timeout: float | None) -> str:
            timeouts.append(timeout)
            return events.pop(0)

        output = TTY()
        with (
            patch.dict(os.environ, {"TERM": "xterm-256color"}, clear=True),
            patch("floati.tui_chart.termios.tcgetattr", return_value=[]),
            patch("floati.tui_chart.termios.tcsetattr"),
            patch("floati.tui_chart.tty.setcbreak"),
            patch(
                "floati.tui_chart.shutil.get_terminal_size",
                return_value=os.terminal_size((80, 24)),
            ),
            patch(
                "floati.tui_chart_render.render_live_harbor_map",
                wraps=render_live_harbor_map,
            ) as render_spy,
        ):
            run_live_harbor_map(
                snapshot_loader=load_snapshot,
                input_stream=TTY(),
                output_stream=output,
                read_event=read_event,
                color_tier="mono",
            )

        self.assertEqual(1, output.getvalue().count("\x1b[?2026h"))
        self.assertEqual(1, render_spy.call_count)
        self.assertEqual(1, loads)
        self.assertEqual([None, None, None], timeouts)

    def test_live_loop_treats_terminal_resize_as_a_state_change(self) -> None:
        """Catches zero-idle optimization leaving a resized map stale."""
        from floati.tui_chart import run_live_harbor_map

        class TTY(io.StringIO):
            def isatty(self) -> bool:
                return True

            def fileno(self) -> int:
                return 83

        events = ["", "q"]
        output = TTY()
        with (
            patch.dict(os.environ, {"TERM": "xterm-256color"}, clear=True),
            patch("floati.tui_chart.termios.tcgetattr", return_value=[]),
            patch("floati.tui_chart.termios.tcsetattr"),
            patch("floati.tui_chart.tty.setcbreak"),
            patch(
                "floati.tui_chart.shutil.get_terminal_size",
                side_effect=(
                    os.terminal_size((40, 24)),
                    os.terminal_size((80, 24)),
                ),
            ),
        ):
            run_live_harbor_map(
                snapshot_loader=lambda: _artifact(include_envelope=False),
                input_stream=TTY(),
                output_stream=output,
                read_event=lambda timeout: events.pop(0),
                color_tier="mono",
            )

        self.assertEqual(2, output.getvalue().count("\x1b[?2026h"))

    def test_selected_target_scrolls_into_the_mouse_viewport(self) -> None:
        """Catches keyboard focus escaping the renderer's clickable viewport."""
        from floati.tui_chart import LiveHarborMapController
        from floati.tui_chart_render import render_live_harbor_map

        artifact = _artifact(include_envelope=False)
        bus = artifact["buses"][0]
        bus["nodes"] = [
            {
                "id": public_ids.builder(f"{index:02d}"),
                "role": "Codex",
                "last_activity_age_seconds": index,
                "inbox_count": 0,
                "receipt_count": index,
            }
            for index in range(30)
        ]
        controller = LiveHarborMapController(artifact)
        for _ in range(30):
            controller.handle_key("KEY_DOWN")
        rendered = render_live_harbor_map(
            controller.artifact,
            selected=controller.selected_target,
            detail_open=False,
            pulses=(),
            width=80,
            height=12,
            color_tier="mono",
        )

        self.assertIn(
            controller.selected_target,
            {region.target for region in rendered.hit_regions},
        )
        self.assertIn(public_ids.builder('29'), rendered.text)

        for _ in range(4):
            controller.handle_key("KEY_DOWN")
        footer = render_live_harbor_map(
            controller.artifact,
            selected=controller.selected_target,
            detail_open=False,
            pulses=(),
            width=80,
            height=12,
            color_tier="mono",
            tail_visible=controller.tail_visible,
        )
        self.assertIn("CHANNELS", footer.text)
        self.assertIn("q quit", footer.text)

    def test_resize_self_pipe_wakes_the_blocking_terminal_reader(self) -> None:
        """Catches SIGWINCH being unable to wake the timer-free event loop."""
        import signal

        from floati.tui_chart import (
            HarborResizeEvent,
            _ResizeWakeup,
            _read_terminal_event,
        )
        from floati.tui_protocol import TerminalInputDecoder

        input_reader, input_writer = os.pipe()
        resize = _ResizeWakeup()
        try:
            resize._handle_signal(signal.SIGWINCH, None)
            with os.fdopen(input_reader, "r", encoding="utf-8") as stream:
                event = _read_terminal_event(
                    stream,
                    None,
                    TerminalInputDecoder(),
                    [],
                    resize_descriptor=resize.read_descriptor,
                )
            self.assertIsInstance(event, HarborResizeEvent)
        finally:
            os.close(input_writer)
            resize.close()

    def test_ci_pty_forces_settled_frames(self) -> None:
        """Catches CI animation escaping through an allocated pseudo-terminal."""
        from floati.tui_chart import HarborSnapshotEvent, run_live_harbor_map

        class TTY(io.StringIO):
            def isatty(self) -> bool:
                return True

            def fileno(self) -> int:
                return 84

        events = [HarborSnapshotEvent(_artifact(include_envelope=True)), "q"]
        output = TTY()
        with (
            patch.dict(os.environ, {"TERM": "xterm-256color", "CI": "1"}, clear=True),
            patch("floati.tui_chart.termios.tcgetattr", return_value=[]),
            patch("floati.tui_chart.termios.tcsetattr"),
            patch("floati.tui_chart.tty.setcbreak"),
            patch(
                "floati.tui_chart.shutil.get_terminal_size",
                return_value=os.terminal_size((80, 24)),
            ),
        ):
            run_live_harbor_map(
                snapshot_loader=lambda: _artifact(include_envelope=False),
                input_stream=TTY(),
                output_stream=output,
                read_event=lambda timeout: events.pop(0),
                color_tier="mono",
            )

        self.assertNotIn("ENVELOPE", output.getvalue())

    def test_current_declared_chart_artifact_degrades_without_invented_detail(self) -> None:
        """Catches the optional live enrichment becoming a fabricated requirement."""
        from floati.tui_chart import normalize_harbor_map_snapshot

        current = _artifact(include_envelope=False)
        for bus in current["buses"]:
            bus.pop("ledger_event_count")
            for node in bus["nodes"]:
                node.pop("last_activity_age_seconds")
                node.pop("inbox_count")
                node.pop("receipt_count")

        normalized = normalize_harbor_map_snapshot(current)

        self.assertEqual([], normalized["envelopes"])
        self.assertIsNone(normalized["buses"][0]["ledger_event_count"])
        self.assertIsNone(normalized["buses"][0]["nodes"][0]["inbox_count"])

    def test_r1_capture_pair_matches_the_deterministic_demo(self) -> None:
        """Catches a landed render whose governed color/mono evidence is stale."""
        from floati.demo import capture_harbor_map

        capture_root = Path("docs/evidence/captures")
        expected = {
            True: capture_root / "regatta-r1-color.txt",
            False: capture_root / "regatta-r1-monochrome.txt",
        }
        for color, path in expected.items():
            with self.subTest(color=color):
                self.assertTrue(path.is_file())
                self.assertEqual(
                    projected_role_text(capture_harbor_map(color=color)),
                    path.read_text(encoding="utf-8"),
                )
        self.assertNotIn("ENVELOPE", capture_harbor_map(color=False))


if __name__ == "__main__":
    unittest.main()

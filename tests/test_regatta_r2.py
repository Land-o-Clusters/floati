from __future__ import annotations

import hashlib
import io
import json
import re
import unittest
from pathlib import Path
from unittest.mock import patch


ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _artifact() -> dict[str, object]:
    return {
        "replay_schema_version": 0,
        "duration_ms": 1500,
        "sources": [
            "work/items.jsonl",
            "receipts/workers.jsonl",
            "receipts/denials.jsonl",
        ],
        "counts": {
            "claim": 1,
            "turn": 1,
            "degradation": 0,
            "denial": 1,
            "completion": 1,
        },
        "harbor": {
            "buses": [
                {
                    "bus_id": "alpha",
                    "architect_node": "lane-a",
                    "nodes": [{"id": "lane-a", "role": "Codex"}],
                },
                {
                    "bus_id": "beta",
                    "architect_node": "lane-b",
                    "nodes": [{"id": "lane-b", "role": "Codex"}],
                },
            ],
            "relationships": [{"source": "alpha", "target": "beta"}],
        },
        "events": [
            {
                "sequence": 1,
                "elapsed_ms": 0,
                "event_class": "claim",
                "record_kind": "work_transition",
                "node_id": "lane-a",
                "work_item_id": "work-1",
                "transition": "claim",
                "source_bus": "alpha",
                "sender": "lane-a",
                "target_bus": "alpha",
                "recipient": "lane-a",
            },
            {
                "sequence": 2,
                "elapsed_ms": 500,
                "event_class": "turn",
                "record_kind": "worker_receipt",
                "node_id": "lane-b",
                "work_item_id": "work-1",
                "transition": "drive",
                "source_bus": "alpha",
                "sender": "lane-a",
                "target_bus": "beta",
                "recipient": "lane-b",
            },
            {
                "sequence": 3,
                "elapsed_ms": 1000,
                "event_class": "denial",
                "record_kind": "denial_receipt",
                "node_id": "lane-b",
                "work_item_id": "work-1",
                "reason_code": "E_DENIED",
                "source_bus": "beta",
                "sender": "lane-b",
                "target_bus": "alpha",
                "recipient": "lane-a",
            },
            {
                "sequence": 4,
                "elapsed_ms": 1500,
                "event_class": "completion",
                "record_kind": "work_transition",
                "node_id": "lane-a",
                "work_item_id": "work-1",
                "transition": "complete",
                "source_bus": "alpha",
                "sender": "lane-a",
                "target_bus": "alpha",
                "recipient": "lane-a",
            },
        ],
    }


class RegattaR2MachineTwinTests(unittest.TestCase):
    def test_replay_plain_and_json_twins_are_byte_identical(self) -> None:
        """Catches cinema dressing leaking into the replay machine surfaces."""
        from floati.replay_render import render_replay_plain

        artifact = _artifact()
        plain = render_replay_plain(artifact).encode("utf-8")
        machine_json = (
            json.dumps(
                artifact,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")

        self.assertEqual(
            "d2ad41e2bd649988466583a585bf8d8b922e3fded124864dbaebb8c5cab6a917",
            hashlib.sha256(plain).hexdigest(),
        )
        self.assertEqual(
            "d2874ff77b5f79306c7abb5d2453d133cdb3f55dfe3c799208ace0f81db097b5",
            hashlib.sha256(machine_json).hexdigest(),
        )

    def test_static_frame_keeps_the_approved_flight_recorder_surface(self) -> None:
        """Catches cinema dressing replacing the approved README hero surface."""
        from floati.replay_render import render_replay_frame

        frame = ANSI.sub("", render_replay_frame(_artifact(), 3))

        self.assertIn("FLOATI // FLIGHT RECORDER", frame)
        self.assertNotIn("FLIGHT RECORDER CINEMA", frame)


class RegattaR2CinemaTests(unittest.TestCase):
    def test_replay_state_keeps_order_routes_and_fault_identity(self) -> None:
        """Catches replay motion or faults being inferred from wall-clock state."""
        from floati.tui_replay import ReplayCinemaController

        controller = ReplayCinemaController(_artifact())
        second = controller.state(2)
        third = controller.state(3)

        self.assertEqual([1, 2], [event["sequence"] for event in second.events])
        self.assertEqual(("lane-a", "lane-b"), (second.pulse.sender, second.pulse.recipient))
        self.assertIsNone(second.fault_node)
        self.assertEqual("lane-b", third.fault_node)
        self.assertEqual("E_DENIED", third.fault_code)

    def test_cinema_map_fault_scrubber_and_degradation_tiers_match(self) -> None:
        """Catches color carrying unique replay meaning or the map becoming a table."""
        from floati.tui_replay import ReplayCinemaController
        from floati.tui_replay_render import render_replay_cinema

        state = ReplayCinemaController(_artifact()).state(3)
        tiers = {
            tier: render_replay_cinema(state, width=100, height=30, color_tier=tier)
            for tier in ("256", "16", "mono")
        }

        self.assertEqual(ANSI.sub("", tiers["256"]), tiers["mono"])
        self.assertEqual(ANSI.sub("", tiers["16"]), tiers["mono"])
        self.assertIn("\x1b[38;5;196m", tiers["256"])
        self.assertIn("\x1b[38;5;196mx", tiers["256"])
        self.assertNotIn("\x1b[38;5;", tiers["16"])
        for signal in ("FLIGHT RECORDER CINEMA", "▤", "x", "lane-b", "E_DENIED"):
            self.assertIn(signal, tiers["mono"])
        self.assertRegex(tiers["mono"], r"lane-b.*x|x.*lane-b")
        self.assertIn("├", tiers["mono"])
        self.assertIn("┤", tiers["mono"])
        self.assertIn("●", tiers["mono"])
        self.assertTrue(all(len(line) <= 100 for line in tiers["mono"].splitlines()))

    def test_required_timeline_fault_and_completion_survive_a_tall_harbor(self) -> None:
        """Catches map rows consuming the scrubber, event stream, or final moment."""
        from floati.tui_replay import ReplayCinemaController
        from floati.tui_replay_render import render_replay_cinema

        artifact = _artifact()
        artifact["harbor"]["buses"][0]["nodes"].extend(
            {"id": f"filler-{index:02d}", "role": "Codex"}
            for index in range(40)
        )
        state = ReplayCinemaController(artifact).state(4)
        rendered = render_replay_cinema(
            state,
            width=80,
            height=24,
            color_tier="mono",
            event_line=lambda event, width: str(event["sequence"]),
        )

        self.assertIn("TIMELINE", rendered)
        self.assertIn("EVENT STREAM", rendered)
        self.assertIn("x", rendered)
        self.assertIn("REPLAY COMPLETE", rendered)
        self.assertIn("lane-b", rendered)

    def test_claimed_sender_fault_is_prioritized_in_a_tall_harbor(self) -> None:
        """Catches denial identity fallback being clipped from the map."""
        from floati.tui_replay import ReplayCinemaController
        from floati.tui_replay_render import render_replay_cinema

        artifact = _artifact()
        artifact["events"][2].pop("node_id")
        artifact["events"][2]["claimed_sender"] = "lane-b"
        artifact["harbor"]["buses"][0]["nodes"].extend(
            {"id": f"filler-{index:02d}", "role": "Codex"}
            for index in range(40)
        )
        state = ReplayCinemaController(artifact).state(3)
        rendered = render_replay_cinema(
            state,
            width=80,
            height=16,
            color_tier="mono",
        )

        self.assertIn("x ▤ VESSEL lane-b", rendered)

    def test_compressed_timeline_never_overwrites_the_current_tick(self) -> None:
        """Catches future events colliding with and erasing the scrubber position."""
        from floati.tui_replay import ReplayCinemaController
        from floati.tui_replay_render import render_replay_cinema

        artifact = _artifact()
        artifact["events"] = [
            {
                "sequence": index,
                "elapsed_ms": index * 10,
                "event_class": "turn",
                "record_kind": "worker_receipt",
                "node_id": "lane-a",
                "work_item_id": "work-1",
                "transition": "drive",
            }
            for index in range(1, 31)
        ]
        artifact["duration_ms"] = 300
        state = ReplayCinemaController(artifact).state(15)
        rendered = render_replay_cinema(
            state,
            width=30,
            height=16,
            color_tier="mono",
        )

        timeline = next(line for line in rendered.splitlines() if "TIMELINE" in line)
        self.assertIn("●", timeline)

    def test_current_production_artifact_does_not_invent_topology_or_routes(self) -> None:
        """Catches route-capable dressing fabricating a bus absent from replay evidence."""
        from floati.tui_replay import ReplayCinemaController

        artifact = _artifact()
        artifact.pop("harbor")
        for event in artifact["events"]:
            for key in ("source_bus", "sender", "target_bus", "recipient"):
                event.pop(key)

        state = ReplayCinemaController(artifact).state(3)

        self.assertEqual((), state.buses)
        self.assertEqual((), state.relationships)
        self.assertIsNone(state.pulse)

    def test_playback_speed_changes_waiting_never_event_order(self) -> None:
        """Catches speed changing replay order or event inclusion."""
        from floati.replay_render import play_replay, render_replay_cinema_frame

        class TTY(io.StringIO):
            def isatty(self) -> bool:
                return True

        observed: dict[float, list[int]] = {}
        delays: dict[float, list[float]] = {}
        for speed in (1.0, 4.0):
            observed[speed] = []
            delays[speed] = []

            def record_frame(*args, **kwargs):
                observed[speed].append(args[1])
                return render_replay_cinema_frame(*args, **kwargs)

            with patch(
                "floati.replay_render.render_replay_cinema_frame",
                side_effect=record_frame,
            ):
                play_replay(
                    _artifact(),
                    speed=speed,
                    stream=TTY(),
                    sleeper=delays[speed].append,
                    term="xterm-256color",
                )

        self.assertEqual([1, 2, 3, 4], observed[1.0])
        self.assertEqual(observed[1.0], observed[4.0])
        self.assertEqual([0.5, 0.5, 0.5], delays[1.0])
        self.assertEqual([0.125, 0.125, 0.125], delays[4.0])

    def test_ci_pty_renders_one_settled_frame_without_sleeping(self) -> None:
        """Catches replay motion escaping through a CI pseudo-terminal."""
        from floati.replay_render import play_replay

        class TTY(io.StringIO):
            def isatty(self) -> bool:
                return True

        stream = TTY()
        delays: list[float] = []
        with patch.dict("os.environ", {"TERM": "xterm-256color", "CI": "1"}, clear=True):
            play_replay(
                _artifact(),
                speed=4.0,
                stream=stream,
                sleeper=delays.append,
                term="xterm-256color",
            )

        self.assertEqual([], delays)
        self.assertEqual(1, stream.getvalue().count("\x1b[?2026h"))
        self.assertIn("FLIGHT RECORDER CINEMA", stream.getvalue())
        self.assertIn("REPLAY COMPLETE", stream.getvalue())

    def test_no_color_requires_a_nonempty_value_and_only_suppresses_sgr(self) -> None:
        """Catches empty NO_COLOR changing bytes or non-empty policy leaking SGR."""
        from floati.replay_render import play_replay

        class TTY(io.StringIO):
            def isatty(self) -> bool:
                return True

        def render(extra_environment: dict[str, str]) -> str:
            stream = TTY()
            environment = {"TERM": "xterm-256color", "CI": "1"}
            environment.update(extra_environment)
            with patch.dict("os.environ", environment, clear=True):
                play_replay(
                    _artifact(),
                    speed=4.0,
                    stream=stream,
                    sleeper=lambda _: None,
                    term="xterm-256color",
                )
            return stream.getvalue()

        unset = render({})
        empty = render({"NO_COLOR": ""})
        suppressed = render({"NO_COLOR": "1"})

        self.assertEqual(unset, empty)
        self.assertIsNotNone(ANSI.search(unset))
        self.assertIsNone(ANSI.search(suppressed))
        self.assertIn("FLIGHT RECORDER CINEMA", suppressed)

    def test_r2_capture_pair_matches_deterministic_demo(self) -> None:
        """Catches a landed cinema frame whose governed capture pair is stale."""
        from floati.demo import capture_replay_cinema

        capture_root = Path("docs/evidence/captures")
        paths = {
            True: capture_root / "regatta-r2-color.txt",
            False: capture_root / "regatta-r2-monochrome.txt",
        }
        for color, path in paths.items():
            with self.subTest(color=color):
                self.assertTrue(path.is_file())
                self.assertEqual(
                    capture_replay_cinema(color=color),
                    path.read_text(encoding="utf-8"),
                )

    def test_hero_gif_is_self_recorded_from_the_same_replay_states(self) -> None:
        """Catches a hand-authored hero asset drifting from product replay state."""
        from floati.demo import record_replay_cinema_gif, replay_cinema_demo_artifact
        from floati.tui_replay_render import render_replay_cinema

        expected = Path("docs/evidence/captures/regatta-r2-hero.gif").read_bytes()
        with patch(
            "floati.tui_replay_render.render_replay_cinema",
            wraps=render_replay_cinema,
        ) as render_spy:
            recorded = record_replay_cinema_gif(replay_cinema_demo_artifact())

        self.assertEqual(expected, recorded)
        self.assertTrue(recorded.startswith(b"GIF89a"))
        self.assertIn(b"NETSCAPE2.0", recorded)
        self.assertEqual(4, recorded.count(b"\x21\xf9\x04"))
        self.assertEqual(4, render_spy.call_count)
        self.assertGreater(len(recorded), 1000)


if __name__ == "__main__":
    unittest.main()

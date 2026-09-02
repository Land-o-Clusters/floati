from __future__ import annotations

import io
import json
import os
import re
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from floati.errors import IntegrityFailure
from floati.jsonl import append_record
from floati.planes import AuthorityGrantStore
from floati.registry import Registry
from floati.root import FloatiRoot
from floati.work import WorkLog
from floati.workers import WorkerReceipts, WorkerRefusals


NOW = datetime(2026, 8, 1, 12, 0, 0, tzinfo=timezone.utc)
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


class _Tty(io.StringIO):
    def isatty(self) -> bool:
        return True


class ReplayProjectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = FloatiRoot.open_direct_home(
            Path(self.temp.name) / "replay-fleet", create=True
        )
        Registry(self.root).register("solo", "Codex")
        self.grant = AuthorityGrantStore(self.root).claim(
            "solo-work", "solo", 3600, 3600, NOW
        )

    def _seed_completed_turn(self) -> str:
        work = WorkLog(self.root)
        item = work.add("prove replay", "solo", [], now=NOW)
        work.claim(
            str(item["id"]),
            "solo",
            "solo-work",
            int(self.grant["epoch"]),
            now=NOW + timedelta(seconds=1),
        )
        receipts = WorkerReceipts(self.root)
        claim = receipts.append(
            "worker-00000000000070008000000000000001",
            str(item["id"]),
            "solo",
            "codex",
            "claim",
            None,
            [],
            now=NOW + timedelta(seconds=1, milliseconds=100),
        )
        for offset, transition in ((200, "spawn"), (300, "drive")):
            receipts.append(
                str(claim["session_id"]),
                str(item["id"]),
                "solo",
                "codex",
                transition,
                None,
                [],
                now=NOW + timedelta(seconds=1, milliseconds=offset),
            )
        receipts.append(
            str(claim["session_id"]),
            str(item["id"]),
            "solo",
            "codex",
            "bind_artifact",
            None,
            [],
            now=NOW + timedelta(seconds=1, milliseconds=400),
        )
        work.complete(
            str(item["id"]), "solo", [], now=NOW + timedelta(seconds=2)
        )
        receipts.append(
            str(claim["session_id"]),
            str(item["id"]),
            "solo",
            "codex",
            "complete",
            None,
            [],
            now=NOW + timedelta(seconds=2, milliseconds=200),
        )
        return str(item["id"])

    def test_projection_orders_and_classifies_only_durable_ledger_events(self) -> None:
        from floati.replay import ReplayTimeline

        work_id = self._seed_completed_turn()
        work = WorkLog(self.root)
        degraded_item = work.add(
            "prove degradation", "solo", [], now=NOW + timedelta(milliseconds=50)
        )
        work.claim(
            str(degraded_item["id"]),
            "solo",
            "solo-work",
            int(self.grant["epoch"]),
            now=NOW + timedelta(seconds=2, milliseconds=300),
        )
        degraded_receipts = WorkerReceipts(self.root)
        degraded_claim = degraded_receipts.append(
            "worker-00000000000070008000000000000004",
            str(degraded_item["id"]),
            "solo",
            "codex",
            "claim",
            None,
            [],
            now=NOW + timedelta(seconds=2, milliseconds=400),
        )
        degraded_receipts.append(
            str(degraded_claim["session_id"]),
            str(degraded_item["id"]),
            "solo",
            "codex",
            "degrade",
            "process_timeout",
            [],
            now=NOW + timedelta(seconds=2, milliseconds=500),
        )
        WorkerRefusals(self.root).append(
            "solo",
            "codex",
            work_id,
            "worker_work_absent",
            now=NOW + timedelta(seconds=3),
        )
        append_record(
            self.root,
            "receipts/denials.jsonl",
            {
                "schema_version": 0,
                "id": "denial-00000000000070008000000000000002",
                "tenant_id": self.root.tenant_id,
                "timestamp": "2026-08-01T12:00:03.000Z",
                "kind": "denial_receipt",
                "attempt_id": "attempt-00000000000070008000000000000003",
                "claimed_sender": "solo",
                "claimed_recipient": "missing",
                "reason_code": "unknown_recipient",
            },
            allowed_kinds={"denial_receipt"},
        )

        artifact = ReplayTimeline.from_root(self.root).artifact()
        events = artifact["events"]

        self.assertEqual(0, artifact["replay_schema_version"])
        self.assertEqual(list(range(1, len(events) + 1)), [row["sequence"] for row in events])
        source_rank = {
            "work/items.jsonl": 0,
            "receipts/workers.jsonl": 1,
            "receipts/worker-refusals.jsonl": 2,
            "receipts/denials.jsonl": 3,
        }
        self.assertEqual(
            sorted(
                events,
                key=lambda row: (
                    source_rank[row["source"]], row["source_ordinal"]
                ),
            ),
            events,
        )
        self.assertEqual(
            sorted(row["elapsed_ms"] for row in events),
            [row["elapsed_ms"] for row in events],
        )
        self.assertEqual(
            {"claim", "turn", "degradation", "denial", "completion"},
            {row["event_class"] for row in events},
        )
        self.assertEqual(0, events[0]["elapsed_ms"])
        self.assertEqual(2000, artifact["duration_ms"])
        self.assertEqual(
            [
                "receipts/denials.jsonl",
                "receipts/worker-refusals.jsonl",
                "receipts/workers.jsonl",
                "work/items.jsonl",
            ],
            artifact["sources"],
        )
        self.assertTrue(all(row.get("process_id") is None for row in events))
        self.assertTrue(
            all(
                all(key in row for key in ("source_bus", "sender", "target_bus", "recipient"))
                for row in events
            )
        )
        self.assertEqual(
            (self.root.tenant_id, "solo", self.root.tenant_id, "solo"),
            tuple(
                events[0][key]
                for key in ("source_bus", "sender", "target_bus", "recipient")
            ),
        )
        denial = next(row for row in events if row["record_kind"] == "denial_receipt")
        self.assertEqual(
            (self.root.tenant_id, "solo", self.root.tenant_id, "missing"),
            tuple(
                denial[key]
                for key in ("source_bus", "sender", "target_bus", "recipient")
            ),
        )

    def test_empty_root_yields_a_stable_empty_artifact(self) -> None:
        from floati.replay import ReplayTimeline

        artifact = ReplayTimeline.from_root(self.root).artifact()

        self.assertEqual([], artifact["events"])
        self.assertEqual(0, artifact["duration_ms"])
        self.assertEqual(
            {"claim": 0, "turn": 0, "degradation": 0, "denial": 0, "completion": 0},
            artifact["counts"],
        )

    def test_production_route_facts_derive_a_real_replay_harbor(self) -> None:
        """Catches production replay routes remaining dark behind an UNKNOWN harbor."""
        from floati.replay import ReplayTimeline
        from floati.tui_replay import ReplayCinemaController

        self._seed_completed_turn()
        artifact = ReplayTimeline.from_root(self.root).artifact()
        state = ReplayCinemaController(artifact).state(2)

        self.assertEqual([self.root.tenant_id], [bus["bus_id"] for bus in state.buses])
        self.assertEqual(
            ["solo"],
            [node["id"] for node in state.buses[0]["nodes"]],
        )
        self.assertEqual((), state.relationships)
        self.assertIsNotNone(state.pulse)
        self.assertEqual(
            (self.root.tenant_id, "solo", self.root.tenant_id, "solo"),
            (
                state.pulse.source_bus,
                state.pulse.sender,
                state.pulse.target_bus,
                state.pulse.recipient,
            ),
        )

    def test_malformed_allowlisted_ledger_is_not_skipped(self) -> None:
        path = self.root.resolve_relative("receipts/workers.jsonl")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"not":"framed"}\n', encoding="utf-8")

        from floati.replay import ReplayTimeline

        with self.assertRaises(IntegrityFailure):
            ReplayTimeline.from_root(self.root)

    def test_plain_and_interactive_rendering_share_order_but_only_tty_sleeps(self) -> None:
        from floati.replay import ReplayTimeline
        from floati.replay_render import play_replay, render_replay_plain

        self._seed_completed_turn()
        artifact = ReplayTimeline.from_root(self.root).artifact()
        plain = render_replay_plain(artifact)
        plain_stream = io.StringIO()
        plain_delays: list[float] = []
        # This test measures INTERACTIVE motion: one sleep per event on a tty.
        # `play_replay` reads `CI` from the ambient environment and renders one
        # settled frame with no sleeps when it is set, so on any CI host this
        # test used to measure the settled path while claiming to measure the
        # moving one. The environment it needs is declared here, exactly as its
        # sibling test_ci_pty_renders_one_settled_frame_without_sleeping
        # declares the opposite one — no predicate moves.
        with patch.dict("os.environ", {"TERM": "xterm-256color"}, clear=True):
            play_replay(
                artifact,
                speed=2.0,
                stream=plain_stream,
                plain=True,
                sleeper=plain_delays.append,
            )
            tty_stream = _Tty()
            tty_delays: list[float] = []
            play_replay(
                artifact,
                speed=2.0,
                stream=tty_stream,
                plain=False,
                sleeper=tty_delays.append,
                term="xterm-256color",
            )

        self.assertEqual(plain, plain_stream.getvalue())
        self.assertEqual([], plain_delays)
        self.assertIn("PLAIN REPLAY //", plain)
        self.assertIn("CLAIM", plain)
        self.assertIn("TURN", plain)
        self.assertIn("COMPLETE", plain)
        self.assertIn("CLAIM    WORK   ", plain)
        self.assertIn("CLAIM    WORKER ", plain)
        self.assertIn("COMPLETE WORK   ", plain)
        self.assertIn("COMPLETE WORKER ", plain)
        self.assertIn("\x1b[?2026h", tty_stream.getvalue())
        self.assertEqual(len(artifact["events"]) - 1, len(tty_delays))
        self.assertAlmostEqual(0.5, tty_delays[0])
        self.assertTrue(all(delay >= 0 for delay in tty_delays))

    def test_speed_validation_is_bounded(self) -> None:
        from floati.errors import ProtocolRefusal
        from floati.replay_render import validate_speed

        self.assertEqual(0.1, validate_speed(0.1))
        self.assertEqual(100.0, validate_speed(100))
        for value in (0, 0.09, 100.1, True, "2"):
            with self.subTest(value=value), self.assertRaises(ProtocolRefusal):
                validate_speed(value)  # type: ignore[arg-type]

    def test_event_rail_visually_distinguishes_work_causality_from_worker_receipts(self) -> None:
        from floati.replay import ReplayTimeline
        from floati.replay_render import render_replay_plain

        self._seed_completed_turn()
        rendered = render_replay_plain(ReplayTimeline.from_root(self.root).artifact())

        self.assertTrue(any(line.startswith("◆ ") and " WORK " in line for line in rendered.splitlines()))
        self.assertTrue(any(line.startswith("│ ") and " WORKER " in line for line in rendered.splitlines()))

    def test_interactive_completion_has_the_buoy_moment_and_orange_work_rail(self) -> None:
        from floati.brand import BUOY_MARK, BUOY_ORANGE, RESET
        from floati.replay import ReplayTimeline
        from floati.replay_render import render_replay_frame, render_replay_plain

        self._seed_completed_turn()
        artifact = ReplayTimeline.from_root(self.root).artifact()

        frame = render_replay_frame(
            artifact, len(artifact["events"]), width=120, height=40
        )
        plain = render_replay_plain(artifact)

        stripped = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", frame)
        self.assertIn(BUOY_MARK, stripped)
        self.assertIn(BUOY_ORANGE + "◆" + RESET, frame)
        self.assertNotIn("⊙", plain)
        self.assertNotIn("\x1b", plain)

    def test_interactive_playback_uses_the_terminal_viewport(self) -> None:
        from unittest.mock import patch

        from floati.replay import ReplayTimeline
        from floati.replay_render import play_replay, render_replay_cinema_frame

        self._seed_completed_turn()
        artifact = ReplayTimeline.from_root(self.root).artifact()
        stream = _Tty()
        with patch(
            "floati.replay_render.render_replay_cinema_frame",
            wraps=render_replay_cinema_frame,
        ) as render:
            play_replay(
                artifact,
                speed=100,
                stream=stream,
                sleeper=lambda _: None,
                term="xterm-256color",
                terminal_size=os.terminal_size((80, 24)),
            )

        self.assertTrue(render.call_args_list)
        self.assertTrue(
            all(call.kwargs["width"] == 80 for call in render.call_args_list)
        )
        self.assertTrue(
            all(call.kwargs["height"] == 24 for call in render.call_args_list)
        )

    def test_interactive_playback_scopes_mouse_and_kitty_modes_in_lifo_order(self) -> None:
        """Catches replay leaking mouse or kitty keyboard modes past playback."""
        from tests.test_regatta_spike import FULL_CAPABILITY_RESPONSE

        from floati.replay import ReplayTimeline
        from floati.replay_render import play_replay

        self._seed_completed_turn()
        artifact = ReplayTimeline.from_root(self.root).artifact()
        output = _Tty()
        try:
            play_replay(
                artifact,
                speed=100,
                stream=output,
                input_stream=_Tty(),
                sleeper=lambda _: None,
                term="xterm-256color",
                terminal_size=os.terminal_size((80, 24)),
                terminal_response=FULL_CAPABILITY_RESPONSE,
            )
        except TypeError as exc:
            self.fail(f"interactive replay lacks its terminal lifecycle seam: {exc}")

        rendered = output.getvalue()
        mouse_enable = "\x1b[?1000h\x1b[?1006h"
        mouse_disable = "\x1b[?1000l\x1b[?1006l"
        kitty_push = "\x1b[>1u"
        kitty_pop = "\x1b[<u"
        self.assertEqual(1, rendered.count(mouse_enable))
        self.assertEqual(1, rendered.count(mouse_disable))
        self.assertEqual(1, rendered.count(kitty_push))
        self.assertEqual(1, rendered.count(kitty_pop))
        self.assertLess(rendered.index(mouse_enable), rendered.index(kitty_push))
        self.assertLess(rendered.rindex("\x1b[?2026l"), rendered.index(kitty_pop))
        self.assertLess(rendered.index(kitty_pop), rendered.index(mouse_disable))

    def test_interactive_playback_unwinds_terminal_modes_after_render_failure(self) -> None:
        """Catches a renderer failure leaking replay's terminal protocol modes."""
        from tests.test_regatta_spike import FULL_CAPABILITY_RESPONSE

        from floati.replay import ReplayTimeline
        from floati.replay_render import play_replay

        self._seed_completed_turn()
        artifact = ReplayTimeline.from_root(self.root).artifact()
        output = _Tty()
        with patch(
            "floati.replay_render.render_replay_cinema_frame",
            side_effect=RuntimeError("render failed"),
        ):
            with self.assertRaisesRegex(RuntimeError, "render failed"):
                play_replay(
                    artifact,
                    speed=100,
                    stream=output,
                    input_stream=_Tty(),
                    sleeper=lambda _: None,
                    term="xterm-256color",
                    terminal_size=os.terminal_size((80, 24)),
                    terminal_response=FULL_CAPABILITY_RESPONSE,
                )

        rendered = output.getvalue()
        kitty_pop = "\x1b[<u"
        mouse_disable = "\x1b[?1000l\x1b[?1006l"
        self.assertEqual(1, rendered.count(kitty_pop))
        self.assertEqual(1, rendered.count(mouse_disable))
        self.assertLess(rendered.index(kitty_pop), rendered.index(mouse_disable))

    def test_cli_replay_emits_plain_frames_and_one_final_artifact(self) -> None:
        self._seed_completed_turn()

        result = subprocess.run(
            [
                "python3", "-m", "floati", "log", "--root", str(self.root.path),
                "--replay", "--speed", "100", "--plain",
            ],
            cwd=REPOSITORY_ROOT,
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertTrue(result.stderr.startswith("PLAIN REPLAY //"))
        artifact = json.loads(result.stdout)
        self.assertEqual("log", artifact["command"])
        self.assertEqual("ok", artifact["status"])
        self.assertEqual(0, artifact["evidence"]["replay_schema_version"])

    def test_cli_replay_empty_and_nonreplay_options_refuse_honestly(self) -> None:
        empty = subprocess.run(
            ["python3", "-m", "floati", "log", "--root", str(self.root.path), "--replay"],
            cwd=REPOSITORY_ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        invalid = subprocess.run(
            ["python3", "-m", "floati", "log", "--root", str(self.root.path), "--speed", "2"],
            cwd=REPOSITORY_ROOT,
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(32, empty.returncode)
        self.assertEqual("no_result", json.loads(empty.stderr)["status"])
        self.assertEqual("", empty.stdout)
        self.assertEqual(20, invalid.returncode)
        self.assertEqual("arguments_invalid", json.loads(invalid.stderr)["evidence"]["code"])


if __name__ == "__main__":
    unittest.main()

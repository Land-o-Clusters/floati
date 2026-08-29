from __future__ import annotations

import hashlib
import io
import json
import os
import re
import struct
import unittest
import zlib
from pathlib import Path
from unittest.mock import patch

from tests.test_regatta_r1 import _artifact as live_artifact
from tests.test_regatta_r2 import _artifact as replay_artifact
from tests.test_regatta_spike import FULL_CAPABILITY_RESPONSE
from tests.test_tui_render import RECEIPTS, SNAPSHOT, WORK


ANSI = re.compile(r"\x1b\[[0-9;]*m")
SAMPLES = (0, 1, 2, 3, 4)
BRAILLE = "⣀⣠⣤⣶⣿"


def _require_r3_gfx(testcase: unittest.TestCase) -> None:
    try:
        __import__("floati.tui_activity")
        __import__("floati.tui_graphics")
    except ModuleNotFoundError:
        testcase.fail("R3-GFX modules must exist")


def _receipt(*, kitty_state: str = "supported", kitty_stamp: str = "MEASURED"):
    from floati.tui_capabilities import (
        CAPABILITY_NAMES,
        CapabilityFact,
        TerminalCapabilityReceipt,
    )

    facts = []
    for name in CAPABILITY_NAMES:
        state = kitty_state if name == "kitty_graphics" else "unsupported"
        stamp = kitty_stamp if name == "kitty_graphics" else "MEASURED"
        facts.append(
            CapabilityFact(
                name=name,
                state=state,
                stamp=stamp,
                source="test:" + name,
                evidence_digest=hashlib.sha256(
                    (name + ":" + state + ":" + stamp).encode("ascii")
                ).hexdigest(),
            )
        )
    return TerminalCapabilityReceipt.create(
        endpoint_id="test-endpoint",
        endpoint_kind="terminal",
        endpoint_stamp="MEASURED",
        facts=facts,
    )


def _png_pixels(payload: bytes) -> tuple[int, int, tuple[tuple[int, int, int, int], ...]]:
    if not payload.startswith(b"\x89PNG\r\n\x1a\n"):
        raise AssertionError("not a PNG")
    cursor = 8
    width = height = 0
    compressed = bytearray()
    while cursor < len(payload):
        length = struct.unpack(">I", payload[cursor : cursor + 4])[0]
        kind = payload[cursor + 4 : cursor + 8]
        body = payload[cursor + 8 : cursor + 8 + length]
        cursor += 12 + length
        if kind == b"IHDR":
            width, height = struct.unpack(">II", body[:8])
        elif kind == b"IDAT":
            compressed.extend(body)
        elif kind == b"IEND":
            break
    raw = zlib.decompress(bytes(compressed))
    stride = 1 + width * 4
    rows = [raw[offset : offset + stride] for offset in range(0, len(raw), stride)]
    if len(rows) != height or any(row[:1] != b"\x00" for row in rows):
        raise AssertionError("unexpected PNG filtering")
    pixels = tuple(
        tuple(row[index : index + 4])
        for row in rows
        for index in range(1, len(row), 4)
    )
    return width, height, pixels


class RegattaR3GraphicsTests(unittest.TestCase):
    def test_honest_five_bucket_series_counts_only_exact_loaded_records(self) -> None:
        """Catches wall-clock sampling, substring matches, duplicate fields, or invented history."""
        _require_r3_gfx(self)
        from floati.tui_activity import activity_series, harbor_activity

        records = [
            {"sender": "lane-a", "recipient": "lane-a"},
            {"node_id": "lane-b"},
            {"claimed_sender": "lane-a"},
            {"detail": "lane-a"},
            {"recipient": "lane-b"},
        ]

        self.assertEqual(
            {
                "lane-a": (1, 0, 1, 0, 0),
                "lane-b": (0, 1, 0, 0, 1),
                "lane-c": (0, 0, 0, 0, 0),
            },
            activity_series(("lane-a", "lane-b", "lane-c"), records),
        )
        duplicate_topology = {
            "buses": [
                {"bus_id": "alpha", "nodes": [{"id": "lane-a"}]},
                {"bus_id": "beta", "nodes": [{"id": "lane-a"}]},
            ]
        }
        self.assertEqual(
            {
                "alpha/lane-a": (1, 0, 0, 0, 0),
                "beta/lane-a": (0, 0, 0, 0, 0),
            },
            harbor_activity(
                duplicate_topology,
                ({"source_bus": "alpha", "sender": "lane-a"},),
            ),
        )

    def test_braille_twin_is_visible_and_identical_in_every_text_tier(self) -> None:
        """Catches color or graphics carrying a bucket value unavailable to monochrome."""
        _require_r3_gfx(self)
        from floati.tui_render import HarborBoardModel, render_frame

        model = HarborBoardModel.from_projection(SNAPSHOT, WORK, RECEIPTS)
        activity = {"lane-a": SAMPLES, "lane-b-with-a-name-that-must-clip": (0,) * 5}
        tiers = {
            tier: render_frame(
                model,
                120,
                40,
                selected=0,
                color=tier != "mono",
                color_tier=tier,
                activity_by_node=activity,
            )
            for tier in ("256", "16", "mono")
        }

        self.assertEqual(tiers["mono"], ANSI.sub("", tiers["256"]))
        self.assertEqual(tiers["mono"], ANSI.sub("", tiers["16"]))
        self.assertIn(BRAILLE, tiers["mono"])
        self.assertIn(">", tiers["mono"])
        self.assertIn("EXPIRED", tiers["mono"])

    def test_pixel_gate_accepts_only_measured_supported_artifact_and_color_policy(self) -> None:
        """Catches TERM, brand, heuristic, unknown, malformed, or NO_COLOR enabling pixels."""
        _require_r3_gfx(self)
        from floati.tui_graphics import graphics_allowed

        self.assertTrue(graphics_allowed(_receipt(), color_tier="256"))
        for receipt in (
            _receipt(kitty_state="unsupported"),
            _receipt(kitty_state="unknown"),
            _receipt(kitty_stamp="DERIVED"),
            _receipt(kitty_stamp="ESTIMATE"),
        ):
            with self.subTest(receipt=receipt):
                self.assertFalse(graphics_allowed(receipt, color_tier="256"))
        self.assertFalse(graphics_allowed(_receipt(), color_tier="mono"))

    def test_activity_png_is_deterministic_bounded_and_brand_only(self) -> None:
        """Catches nondeterministic encoders, unbounded strips, foreign color, or lost samples."""
        _require_r3_gfx(self)
        from floati.tui_graphics import (
            FLOATI_CLEAR,
            FLOATI_DARK,
            FLOATI_ORANGE,
            MAX_ACTIVITY_PNG_BYTES,
            activity_strip_png,
        )

        first = activity_strip_png(SAMPLES)
        second = activity_strip_png(SAMPLES)
        width, height, pixels = _png_pixels(first)

        self.assertEqual(first, second)
        self.assertLessEqual(len(first), MAX_ACTIVITY_PNG_BYTES)
        self.assertEqual((20, 8), (width, height))
        self.assertLessEqual(set(pixels), {FLOATI_ORANGE, FLOATI_DARK, FLOATI_CLEAR})
        orange_by_bucket = tuple(
            sum(
                pixels[row * width + column] == FLOATI_ORANGE
                for row in range(height)
                for column in range(bucket * 4, bucket * 4 + 4)
            )
            for bucket in range(5)
        )
        self.assertEqual(tuple(sorted(orange_by_bucket)), orange_by_bucket)
        self.assertEqual(5, len(set(orange_by_bucket)))

    def test_overlay_geometry_uses_visible_rows_and_deletes_stale_placements(self) -> None:
        """Catches clipped vessels retaining images or resize/removal leaking placements."""
        _require_r3_gfx(self)
        from floati.tui_graphics import plan_activity_overlays

        first = plan_activity_overlays(
            activity_by_target={"alpha/lane-a": SAMPLES, "alpha/lane-b": (4, 3, 2, 1, 0)},
            visible_positions={"alpha/lane-a": (7, 70), "alpha/lane-b": (8, 70)},
            capability_receipt=_receipt(),
            color_tier="256",
        )
        second = plan_activity_overlays(
            activity_by_target={"alpha/lane-a": SAMPLES, "alpha/lane-b": (4, 3, 2, 1, 0)},
            visible_positions={"alpha/lane-a": (6, 60)},
            capability_receipt=_receipt(),
            color_tier="256",
            previous=first.overlays,
        )

        self.assertEqual(2, len(first.overlays))
        self.assertEqual(2, len({overlay.image_id for overlay in first.overlays}))
        self.assertEqual((6, 60), (second.overlays[0].row, second.overlays[0].column))
        removed_id = next(
            overlay.image_id for overlay in first.overlays if overlay.target_id == "alpha/lane-b"
        )
        self.assertIn(removed_id, second.delete_ids)
        self.assertNotIn("alpha/lane-b", {overlay.target_id for overlay in second.overlays})

    def test_text_images_and_cursor_share_one_sync_frame_and_cleanup_all_ids(self) -> None:
        """Catches torn overlay frames or termios failure skipping image deletion."""
        _require_r3_gfx(self)
        from floati.tui import run_board
        from floati.tui_render import HarborBoardModel

        class TTY(io.StringIO):
            def isatty(self) -> bool:
                return True

            def fileno(self) -> int:
                return 93

        model = HarborBoardModel.from_projection(SNAPSHOT, WORK, RECEIPTS)
        output = TTY()
        with (
            patch.dict(os.environ, {"TERM": "xterm-256color"}, clear=True),
            patch("floati.tui.termios.tcgetattr", return_value=[]),
            patch("floati.tui.termios.tcsetattr", side_effect=OSError("restore")),
            patch("floati.tui.tty.setcbreak"),
            patch("floati.tui._read_terminal_input", return_value="q"),
            patch(
                "floati.tui.shutil.get_terminal_size",
                return_value=os.terminal_size((100, 30)),
            ),
        ):
            with self.assertRaisesRegex(OSError, "restore"):
                run_board(
                    model_loader=lambda: model,
                    input_stream=TTY(),
                    output_stream=output,
                    terminal_response=FULL_CAPABILITY_RESPONSE,
                )

        rendered = output.getvalue()
        frame_start = rendered.index("\x1b[?2026h")
        frame_end = rendered.index("\x1b[?2026l", frame_start)
        transmissions = re.findall(r"\x1b_Ga=T,[^;]*i=(\d+)", rendered[frame_start:frame_end])
        self.assertGreaterEqual(len(set(transmissions)), 3)  # buoy plus two vessels
        for image_id in set(transmissions):
            self.assertIn(f"\x1b_Ga=d,d=I,q=2,i={image_id}\x1b\\", rendered)
        self.assertTrue(rendered.endswith("\x1b[?25h\x1b[?1049l"))

    def test_board_live_map_and_replay_share_the_same_braille_expression(self) -> None:
        """Catches one surface using a different activity law or replay guessing pixels."""
        _require_r3_gfx(self)
        from floati.tui_chart import HarborTarget
        from floati.tui_chart_render import render_live_harbor_map
        from floati.tui_render import HarborBoardModel, render_frame
        from floati.tui_replay import ReplayCinemaController
        from floati.tui_replay_render import render_replay_cinema

        board_model = HarborBoardModel.from_projection(SNAPSHOT, WORK, RECEIPTS)
        board = render_frame(
            board_model,
            120,
            40,
            selected=0,
            color=False,
            color_tier="mono",
            activity_by_node={"lane-a": SAMPLES},
        )
        live = render_live_harbor_map(
            live_artifact(include_envelope=False),
            selected=HarborTarget("pier", "alpha"),
            detail_open=False,
            pulses=(),
            width=120,
            height=40,
            color_tier="mono",
            activity_by_node={"alpha/lane-a": SAMPLES},
        ).text
        replay = render_replay_cinema(
            ReplayCinemaController(replay_artifact()).state(4),
            width=120,
            height=40,
            color_tier="mono",
            activity_by_node={"alpha/lane-a": SAMPLES},
        )

        self.assertIn(BRAILLE, board)
        self.assertIn(BRAILLE, live)
        self.assertIn(BRAILLE, replay)
        self.assertNotIn("\x1b_G", replay)

    def test_live_map_pixels_follow_the_same_receipt_gate_and_cleanup_law(self) -> None:
        """Catches the flagship map showing braille while never upgrading measured vessels."""
        from floati.tui_chart import run_live_harbor_map

        class TTY(io.StringIO):
            def isatty(self) -> bool:
                return True

            def fileno(self) -> int:
                return 94

        output = TTY()
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
                snapshot_loader=lambda: live_artifact(include_envelope=True),
                input_stream=TTY(),
                output_stream=output,
                read_event=lambda _timeout: "q",
                terminal_response=FULL_CAPABILITY_RESPONSE,
                color_tier="256",
                settled_frames=True,
            )

        rendered = output.getvalue()
        self.assertEqual(0, code)
        frame_start = rendered.index("\x1b[?2026h")
        frame_end = rendered.index("\x1b[?2026l", frame_start)
        transmissions = set(
            re.findall(r"\x1b_Ga=T,[^;]*i=(\d+)", rendered[frame_start:frame_end])
        )
        self.assertEqual(4, len(transmissions))
        for image_id in transmissions:
            self.assertIn(f"\x1b_Ga=d,d=I,q=2,i={image_id}\x1b\\", rendered)

    def test_r3_capture_pair_is_current_while_machine_twins_remain_pinned(self) -> None:
        """Catches stale gate assets or graphics leaking into stable plain/JSON surfaces."""
        _require_r3_gfx(self)
        from floati.demo import capture_regatta_r3
        from floati.tui_render import HarborBoardModel, render_plain_dump

        capture_root = Path("docs/evidence/captures")
        for color, name in (
            (True, "regatta-r3-color.txt"),
            (False, "regatta-r3-monochrome.txt"),
        ):
            with self.subTest(color=color):
                expected = capture_root / name
                self.assertTrue(expected.is_file())
                self.assertEqual(capture_regatta_r3(color=color), expected.read_text())

        model = HarborBoardModel.from_projection(SNAPSHOT, WORK, RECEIPTS)
        plain = render_plain_dump(model, width=100).encode("utf-8")
        machine = (
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
            hashlib.sha256(machine).hexdigest(),
        )


if __name__ == "__main__":
    unittest.main()

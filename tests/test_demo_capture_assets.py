from __future__ import annotations

from floati import fixture_ids as public_ids

import hashlib
import importlib.util
import os
import re
import tempfile
import unittest
from pathlib import Path

if importlib.util.find_spec("PIL") is not None:
    from PIL import Image


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "capture-demo-assets.py"
REPLAY = ROOT / "docs" / "evidence" / "captures" / "floati-replay-drill.txt"


def operator_account_name() -> str:
    return os.environ.get("USER") or os.environ.get("LOGNAME") or Path.home().name


def load_capture_module():
    if not SCRIPT.is_file():
        raise AssertionError("capture script must exist")
    spec = importlib.util.spec_from_file_location("capture_demo_assets", SCRIPT)
    if spec is None or spec.loader is None:
        raise AssertionError("capture script must be importable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@unittest.skipUnless(importlib.util.find_spec("PIL"), "Pillow is not installed")
class DemoCaptureAssetTests(unittest.TestCase):
    def test_specs_name_exact_ruled_candidate_set(self) -> None:
        capture = load_capture_module()

        self.assertEqual(
            [
                "hero-three-fault-replay.gif",
                "board-glow.gif",
                "harbor-chart-map.gif",
                "install-moment.gif",
            ],
            [spec.name for spec in capture.capture_specs()],
        )

    def test_candidate_crops_match_fable_round_one_verdict(self) -> None:
        capture = load_capture_module()

        self.assertEqual(
            {
                "hero-three-fault-replay.gif": (1400, 640),
                "board-glow.gif": (1400, 760),
                "harbor-chart-map.gif": (1400, 520),
                "install-moment.gif": (1400, 600),
            },
            capture.candidate_sizes(),
        )

    def test_lit_buoy_lamp_survives_as_ruled_yellow_pixels(self) -> None:
        capture = load_capture_module()

        frame = capture.render_capture_frame(
            capture.build_text_frames()["install-moment.gif"][-1],
            candidate_size=(1400, 600),
            phase=6,
        )
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "lit-lamp.gif"
            capture.write_gif(
                path,
                [Image.new("RGB", frame.size, capture.BACKGROUND), frame],
                duration_ms=100,
            )
            with Image.open(path) as encoded:
                encoded.seek(1)
                colors = encoded.convert("RGB").getcolors(
                    maxcolors=encoded.width * encoded.height
                )
        yellow_pixels = sum(
            count for count, color in colors if color == (245, 197, 24)
        )

        self.assertEqual("#F5C518", capture.LIT_LAMP)
        self.assertGreater(yellow_pixels, 0)

    def test_palette_reservation_does_not_rewrite_frames_without_a_lamp(self) -> None:
        capture = load_capture_module()
        frame = Image.new("RGB", (32, 32))
        frame.putdata(
            [
                (index % 256, (index * 7) % 256, (index * 17) % 256)
                for index in range(32 * 32)
            ]
        )

        expected = frame.convert("P", palette=Image.Palette.ADAPTIVE)
        actual = capture._palettize_frame(frame)

        self.assertEqual(expected.tobytes(), actual.tobytes())
        self.assertEqual(expected.getpalette(), actual.getpalette())

    def test_replay_source_is_the_banked_three_fault_artifact(self) -> None:
        capture = load_capture_module()

        artifact = capture.load_replay_artifact(REPLAY)

        self.assertEqual(16, len(artifact["events"]))
        self.assertEqual(
            {"claim", "turn", "denial", "degradation"},
            {event["event_class"] for event in artifact["events"]},
        )
        self.assertEqual(3, artifact["counts"]["degradation"])

    def test_source_sha_and_capture_text_fail_closed(self) -> None:
        capture = load_capture_module()

        for invalid in ("a" * 39, "A" * 40, "z" * 40, ""):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    capture.validate_source_sha(invalid)
        for unsafe in (
            "\x2fUsers/example/project",
            operator_account_name(),
            "slipway-spawn-groups",
        ):
            with self.subTest(unsafe=unsafe):
                with self.assertRaises(ValueError):
                    capture.ensure_capture_text_safe(unsafe)

    def test_capture_guard_source_does_not_embed_the_operator_account(self) -> None:
        source = SCRIPT.read_text(encoding="utf-8")

        self.assertNotIn(operator_account_name().casefold(), source.casefold())

    def test_output_boundaries_refuse_wrong_storage_classes(self) -> None:
        capture = load_capture_module()

        capture.validate_output_paths(
            ROOT / "docs" / "demo" / "candidates",
            Path("\x2fprivate/tmp/floati-demo-masters/a"),
        )
        invalid = [
            (ROOT / "docs" / "evidence", Path("\x2fprivate/tmp/master")),
            (ROOT / "docs" / "demo", Path("relative/master")),
            (ROOT / "docs" / "demo", ROOT / "docs" / "demo" / "masters"),
            (ROOT / "docs" / "demo" / "candidates", Path("../..\x2fprivate/tmp/master")),
        ]
        for output, master in invalid:
            with self.subTest(output=output, master=master):
                with self.assertRaises(ValueError):
                    capture.validate_output_paths(output, master)

    def test_text_frames_use_real_product_renderers_and_safe_provenance(self) -> None:
        capture = load_capture_module()
        self.assertTrue(
            hasattr(capture, "build_text_frames"),
            "capture pipeline must expose real renderer frames",
        )

        frames = capture.build_text_frames()

        self.assertEqual(
            {spec.name for spec in capture.capture_specs()},
            set(frames),
        )
        for name, sequence in frames.items():
            with self.subTest(name=name):
                self.assertGreater(len(sequence), 1)
                for frame in sequence:
                    self.assertEqual(frame, capture.ensure_capture_text_safe(frame))
        hero = frames["hero-three-fault-replay.gif"]
        self.assertIn("FLOATI // FLIGHT RECORDER", hero[0])
        self.assertIn("DEGRADED", hero[-1])
        self.assertIn("REPLAY COMPLETE", hero[-1])
        board = frames["board-glow.gif"]
        self.assertTrue(any("DRIVING" in frame for frame in board))
        self.assertTrue(any("! DENIAL" in frame for frame in board))
        chart = frames["harbor-chart-map.gif"]
        self.assertIn("FLOATI // HARBOR CHART", chart[0])
        self.assertIn("TRAFFIC", chart[0])
        for name in ("board-glow.gif", "harbor-chart-map.gif"):
            rendered = "\n".join(frames[name])
            with self.subTest(name=name):
                for retired in (
                    public_ids.compose("fa", "ble"),
                    public_ids.compose("lane", "-app"),
                    public_ids.compose("lane", "-floati"),
                ):
                    self.assertNotIn(retired, rendered)
                for identity in ("builder-review", "builder-app", "builder-core"):
                    self.assertIn(identity, rendered)
        install = frames["install-moment.gif"]
        self.assertIn("⊙", install[-1])
        self.assertIn('"command":"install"', install[-1])
        self.assertIn('"source_sha"', install[-1])
        self.assertIn('"status":"ok"', install[-1])
        visible = re.sub(r"\x1b\[[0-9;]*m", "", install[-1])
        self.assertLessEqual(max(map(len, visible.splitlines())), 112)

    def test_build_candidates_writes_four_hashed_animated_gifs(self) -> None:
        capture = load_capture_module()
        self.assertTrue(
            hasattr(capture, "build_candidates"),
            "capture pipeline must build the ruled candidate set",
        )

        demo_root = ROOT / "docs" / "demo"
        with tempfile.TemporaryDirectory(dir=demo_root) as output_text:
            with tempfile.TemporaryDirectory(
                prefix="floati-demo-master-test-",
                dir="\x2fprivate/tmp",
            ) as master_text:
                artifacts = capture.build_candidates(
                    Path(output_text),
                    Path(master_text),
                    "a" * 40,
                    ffmpeg=None,
                    candidate_size=(280, 168),
                )

                self.assertEqual(
                    [spec.name for spec in capture.capture_specs()],
                    [artifact.name for artifact in artifacts],
                )
                for artifact in artifacts:
                    with self.subTest(artifact=artifact.name):
                        data = artifact.path.read_bytes()
                        self.assertEqual(
                            hashlib.sha256(data).hexdigest(),
                            artifact.sha256,
                        )
                        self.assertTrue(data.startswith(b"GIF89a"))
                        self.assertEqual("a" * 40, artifact.source_sha)
                        self.assertEqual(2, artifact.source_scale)
                        with Image.open(artifact.path) as image:
                            self.assertEqual((280, 168), image.size)
                            self.assertGreater(image.n_frames, 1)
                            self.assertEqual(0, image.info["loop"])

    def test_tiny_gif_write_is_deterministic_and_animated(self) -> None:
        capture = load_capture_module()
        frames = [
            Image.new("RGB", (80, 48), color)
            for color in ((18, 22, 28), (255, 159, 67), (18, 22, 28))
        ]

        with tempfile.TemporaryDirectory() as temporary:
            first = Path(temporary) / "first.gif"
            second = Path(temporary) / "second.gif"
            capture.write_gif(first, frames, duration_ms=100)
            capture.write_gif(second, frames, duration_ms=100)

            self.assertEqual(first.read_bytes(), second.read_bytes())
            self.assertTrue(first.read_bytes().startswith(b"GIF89a"))
            with Image.open(first) as image:
                self.assertEqual(3, image.n_frames)
                self.assertEqual(0, image.info["loop"])


if __name__ == "__main__":
    unittest.main()

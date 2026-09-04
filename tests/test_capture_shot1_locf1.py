from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from tests.temp_roots import REAL_TEMP_ROOT
from floati.scrub import FORBIDDEN_NAMES


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "capture-shot1-locf1.py"
FIXTURE_DIR = ROOT / "tests" / "fixtures" / "capture-shot1"
SOURCE_SHA = "a" * 40
CAPTURED_AT = "2026-09-02T17:00:00.000Z"
CAPTURE_FONT_CANDIDATES = (
    Path("/System/Library/Fonts/Menlo.ttc"),
    Path("/System/Library/Fonts/SFNSMono.ttf"),
    Path("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"),
)


def load_capture_module():
    if not SCRIPT.is_file():
        raise AssertionError("SHOT-1/LOC-F1 capture generator must exist")
    spec = importlib.util.spec_from_file_location("capture_shot1_locf1", SCRIPT)
    if spec is None or spec.loader is None:
        raise AssertionError("capture generator must be importable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def declared_loc_inputs() -> dict[str, Path]:
    return {
        "dead_receiver_transcript": FIXTURE_DIR / "dead-receiver.txt",
        "adapter_transcript": FIXTURE_DIR / "adapter-boundary.txt",
    }


def declared_shot_inputs() -> dict[str, Path]:
    return {"wake_evidence": FIXTURE_DIR / "wake-ping.md"}


def fixture_capture_args() -> list[str]:
    loc = declared_loc_inputs()
    shot = declared_shot_inputs()
    return [
        "--dead-receiver-transcript",
        str(loc["dead_receiver_transcript"]),
        "--adapter-transcript",
        str(loc["adapter_transcript"]),
        "--wake-evidence",
        str(shot["wake_evidence"]),
    ]


def resolve_test_capture_font(test: unittest.TestCase, capture):
    """Use one fixed system face or prove the generator's typed absence."""

    try:
        return capture.resolve_capture_font(
            None,
            candidates=CAPTURE_FONT_CANDIDATES,
        )
    except capture.CaptureRefusal as refusal:
        test.assertEqual("capture_font_absent", refusal.code)
        test.assertIn("--font", refusal.remedy)
        return None


EXCLUDED_RUNTIME_EVIDENCE = (
    "rp1-public-safe-svg-20260831",
    "gate-p0-organic-wake-complete-2026-08-28.md",
)


class CaptureBoundaryTests(unittest.TestCase):
    def test_exported_capture_code_does_not_name_excluded_evidence(self) -> None:
        """SHOT-1-F2: a projection is a different artifact; excluded paths must not be runtime inputs."""

        source = SCRIPT.read_text(encoding="utf-8")
        for needle in EXCLUDED_RUNTIME_EVIDENCE:
            self.assertNotIn(needle, source)

    def test_terminal_grid_is_exactly_120_columns_by_40_rows(self) -> None:
        capture = load_capture_module()

        grid = capture.terminal_grid("one\ntwo")

        lines = grid.splitlines()
        self.assertEqual(40, len(lines))
        self.assertTrue(all(len(line) == 120 for line in lines))
        self.assertEqual("one", lines[0].rstrip())
        self.assertEqual("two", lines[1].rstrip())

    def test_terminal_grid_refuses_text_that_would_be_cropped(self) -> None:
        capture = load_capture_module()

        with self.assertRaises(capture.CaptureRefusal) as caught:
            capture.terminal_grid("x" * 121)

        self.assertEqual("capture_terminal_overflow", caught.exception.code)

    def test_terminal_transcript_preserves_testimony_without_cell_padding(self) -> None:
        capture = load_capture_module()

        transcript = capture.terminal_transcript("one\ntwo\n")

        self.assertEqual("one\ntwo\n", transcript)
        self.assertTrue(all(line == line.rstrip() for line in transcript.splitlines()))

    def test_invalid_font_declaration_refuses_before_output_creation(self) -> None:
        capture = load_capture_module()
        with tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT) as temporary:
            base = Path(temporary)
            site_output = base / "site"
            shot_output = base / "shot"
            scratch = base / "scratch"
            stderr = io.StringIO()

            with redirect_stderr(stderr):
                result = capture.main(
                    [
                        "--font",
                        str(base / "missing.ttf"),
                        "--source-sha",
                        "a" * 40,
                        "--captured-at",
                        "2026-09-02T17:00:00.000Z",
                        "--scratch",
                        str(scratch),
                        "--site-output",
                        str(site_output),
                        "--shot-output",
                        str(shot_output),
                        *fixture_capture_args(),
                    ]
                )

            self.assertEqual(capture.CAPTURE_REFUSAL_EXIT, result)
            self.assertFalse(site_output.exists())
            self.assertFalse(shot_output.exists())
            self.assertFalse(scratch.exists())
            artifact = json.loads(stderr.getvalue())
            self.assertEqual("capture_font_declaration_invalid", artifact["evidence"]["code"])
            self.assertIn("absolute readable regular file", artifact["evidence"]["remedy"])

    @unittest.skipUnless(importlib.util.find_spec("PIL"), "Pillow is not installed")
    def test_raster_is_two_times_the_declared_120_by_40_cell_grid(self) -> None:
        capture = load_capture_module()
        font = resolve_test_capture_font(self, capture)
        if font is None:
            return
        image = capture.render_terminal_png(
            capture.terminal_grid("Floati"), "dark", font, scale=2
        )

        self.assertEqual(
            (120 * font.cell_width * 2, 40 * font.cell_height * 2),
            image.size,
        )


class CaptureMomentTests(unittest.TestCase):
    def test_fixed_capture_faces_include_the_debian_system_candidate(self) -> None:
        capture = load_capture_module()

        self.assertEqual(CAPTURE_FONT_CANDIDATES, capture.FONT_CANDIDATES)

    def test_missing_declared_and_fixed_capture_face_is_typed(self) -> None:
        capture = load_capture_module()

        with self.assertRaises(capture.CaptureRefusal) as caught:
            capture.resolve_capture_font(None, candidates=())

        self.assertEqual("capture_font_absent", caught.exception.code)
        self.assertIn("--font", caught.exception.remedy)

    def test_failure_injection_selects_human_replay_not_machine_artifact(self) -> None:
        capture = load_capture_module()

        moment = capture._failure_moment()

        self.assertIn("REPLAY COMPLETE", moment.text)
        self.assertNotIn('"artifact_version"', moment.text)
        self.assertLessEqual(len(moment.text.splitlines()), capture.TERMINAL_ROWS)

    def test_missing_declared_transcript_is_typed(self) -> None:
        capture = load_capture_module()
        missing = Path("\x2ftmp/floati-shot-1-f2-missing-transcript.txt")
        if missing.exists():
            self.fail("precondition: declared-missing path must not exist")

        with self.assertRaises(capture.CaptureRefusal) as caught:
            capture._dead_receiver_moment(missing)

        self.assertEqual("capture_input_declaration_invalid", caught.exception.code)
        self.assertIn("--dead-receiver-transcript", caught.exception.remedy)

    def test_loc_moments_are_the_exact_eight_requested_surfaces(self) -> None:
        capture = load_capture_module()
        with tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT) as temporary:
            moments = capture.build_loc_moments(
                Path(temporary) / "loc",
                SOURCE_SHA,
                CAPTURED_AT,
                **declared_loc_inputs(),
            )

        self.assertEqual(
            {
                "board",
                "handoff",
                "lease",
                "dead-receiver",
                "identity",
                "adapter-boundary",
                "failure-injection",
                "help",
            },
            {moment.name for moment in moments},
        )
        by_name = {moment.name: moment for moment in moments}
        self.assertEqual("fixture", by_name["board"].data_class)
        self.assertIn("delivered", by_name["handoff"].text.casefold())
        self.assertIn("acknowledged", by_name["handoff"].text.casefold())
        self.assertIn("denied", by_name["handoff"].text.casefold())
        self.assertIn("never means down", by_name["lease"].text.casefold())
        self.assertIn("PASS", by_name["dead-receiver"].text)
        self.assertIn("DEAF", by_name["dead-receiver"].text)
        self.assertIn("no certificate view exists", by_name["identity"].text.casefold())
        self.assertIn("one bus, many harnesses", by_name["adapter-boundary"].text.casefold())
        self.assertIn("caught", by_name["failure-injection"].text.casefold())
        self.assertIn("EXIT STATUS", by_name["help"].text)

    def test_shot_moments_are_the_exact_three_missed_frames(self) -> None:
        capture = load_capture_module()
        with tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT) as temporary:
            moments = capture.build_shot_moments(
                Path(temporary) / "shot",
                SOURCE_SHA,
                CAPTURED_AT,
                **declared_shot_inputs(),
            )

        self.assertEqual(
            {"survey", "wake-ping", "uninstall-dry-run"},
            {moment.name for moment in moments},
        )
        by_name = {moment.name: moment for moment in moments}
        self.assertIn("read-only", by_name["survey"].text.casefold())
        self.assertIn("outcome: woke", by_name["wake-ping"].text.casefold())
        self.assertEqual("real", by_name["wake-ping"].data_class)
        self.assertIn("dry run", by_name["uninstall-dry-run"].text.casefold())
        self.assertIn("foreign files retained", by_name["uninstall-dry-run"].text.casefold())

    def test_wake_summary_projects_real_evidence_without_seat_names(self) -> None:
        capture = load_capture_module()
        public_fence = importlib.import_module("scripts.public_name_fence")
        source = (
            "`wake_attempt_receipt`s with REAL sessions, `outcome: woke`. "
            "The full chain held with no human in the mail path: trusted hook, "
            "armed waiter, envelope lands, session wakes, work begins. The "
            "organic wake receipt from a real harness Stop satisfied acceptance."
        )

        summary = capture.render_wake_ping_summary(source)

        self.assertIn("outcome: woke", summary)
        with tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT) as temporary:
            transcript = Path(temporary) / "wake-ping.txt"
            transcript.write_text(summary, encoding="utf-8")
            self.assertEqual([], public_fence.scan_tree(transcript.parent))

    def test_uninstall_summary_projects_counts_without_expanding_receipt_rows(self) -> None:
        capture = load_capture_module()
        artifact = {
            "artifact_version": 0,
            "command": "uninstall",
            "status": "ok",
            "evidence": {
                "data_retention_notice": (
                    "Bus roots and ledgers are retained; uninstall removes tool files only."
                ),
                "dry_run": True,
                "foreign_preserved": ["foreign.keep"],
                "removal_receipts": [
                    {"path": f"owned-{index}", "sha256": "a" * 64}
                    for index in range(500)
                ],
                "removed_count": 0,
            },
        }

        text = capture.render_uninstall_summary(artifact)

        self.assertIn("OWNED TOOL FILES PLANNED: 500", text)
        self.assertIn("FILES REMOVED: 0", text)
        self.assertIn("FOREIGN FILES RETAINED: foreign.keep", text)
        self.assertIn("Bus roots and ledgers are retained", text)
        self.assertLessEqual(len(text.splitlines()), capture.TERMINAL_ROWS)

    def test_uninstall_summary_bounds_a_large_foreign_file_population(self) -> None:
        capture = load_capture_module()
        artifact = {
            "artifact_version": 0,
            "command": "uninstall",
            "status": "ok",
            "evidence": {
                "data_retention_notice": (
                    "Bus roots and ledgers are retained; uninstall removes tool files only."
                ),
                "dry_run": True,
                "foreign_preserved": [
                    f"floati/__pycache__/module_{index:03d}.cpython-39.pyc"
                    for index in range(375)
                ],
                "removal_receipts": [],
                "removed_count": 0,
            },
        }

        text = capture.render_uninstall_summary(artifact)

        self.assertIn("FOREIGN FILES RETAINED: 375", text)
        self.assertNotIn("module_000", text)
        self.assertLessEqual(len(text.splitlines()), capture.TERMINAL_ROWS)

    def test_exposure_refuses_both_request_banned_names(self) -> None:
        capture = load_capture_module()

        for code, raw in FORBIDDEN_NAMES:
            with self.subTest(code=code):
                with self.assertRaises(capture.CaptureRefusal) as caught:
                    capture.expose_capture_text(
                        "terminal " + raw.decode("ascii") + " testimony",
                        scratch_roots=(),
                    )
                self.assertEqual("capture_text_forbidden", caught.exception.code)

    def test_all_exposed_moments_are_public_safe_and_fit_one_grid(self) -> None:
        capture = load_capture_module()
        public_fence = importlib.import_module("scripts.public_name_fence")
        with tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT) as temporary:
            scratch = Path(temporary)
            moments = (
                *capture.build_loc_moments(
                    scratch / "loc", SOURCE_SHA, CAPTURED_AT, **declared_loc_inputs()
                ),
                *capture.build_shot_moments(
                    scratch / "shot", SOURCE_SHA, CAPTURED_AT, **declared_shot_inputs()
                ),
            )
            transcripts = scratch / "transcripts"
            transcripts.mkdir()

            for moment in moments:
                with self.subTest(moment=moment.name):
                    self.assertNotIn(str(scratch), moment.text)
                    for _code, raw in FORBIDDEN_NAMES:
                        self.assertNotIn(raw.decode("ascii").casefold(), moment.text.casefold())
                    grid = capture.terminal_grid(moment.text)
                    self.assertEqual(40, len(grid.splitlines()))
                    (transcripts / f"{moment.name}.txt").write_text(
                        moment.text,
                        encoding="utf-8",
                    )

            self.assertEqual([], public_fence.scan_tree(transcripts))

    @unittest.skipUnless(importlib.util.find_spec("PIL"), "Pillow is not installed")
    def test_site_directory_writes_exact_png_and_transcript_contract(self) -> None:
        capture = load_capture_module()
        font = resolve_test_capture_font(self, capture)
        if font is None:
            return
        with tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT) as temporary:
            base = Path(temporary)
            moments = capture.build_loc_moments(
                base / "scratch", SOURCE_SHA, CAPTURED_AT, **declared_loc_inputs()
            )
            manifest = capture.write_capture_directory(
                base / "site-v3",
                moments,
                font,
                SOURCE_SHA,
                CAPTURED_AT,
                site_names=True,
            )

            paths = {artifact["path"] for artifact in manifest["artifacts"]}
            self.assertEqual(32, len(paths))
            for moment in moments:
                for theme in ("dark", "light"):
                    stem = f"floati-{moment.name}-{theme}-source"
                    self.assertIn(stem + ".png", paths)
                    self.assertIn(stem + ".txt", paths)
            self.assertEqual(8, len(manifest["moments"]))
            persisted = json.loads(
                (base / "site-v3" / "manifest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(manifest, persisted)


class CaptureInventoryAndReproducibilityTests(unittest.TestCase):
    def test_demo_inventory_derives_direct_bytes_and_nested_manifests_once(self) -> None:
        capture = load_capture_module()
        with tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT) as temporary:
            demo = Path(temporary) / "demo"
            nested = demo / "site-v3"
            nested.mkdir(parents=True)
            (demo / "hero.gif").write_bytes(b"GIF89a-capture")
            (demo / "manifest.json").write_text("stale self\n", encoding="utf-8")
            (demo / "CAPTURE-INVENTORY.md").write_text(
                "stale derived document\n", encoding="utf-8"
            )
            (nested / "manifest.json").write_text(
                '{"schema_version":0}\n', encoding="utf-8"
            )
            (nested / "frame.png").write_bytes(b"nested bytes owned by its manifest")

            model = capture.derive_demo_inventory(demo)
            rendered = capture.render_demo_inventory(model)

        self.assertEqual(["hero.gif"], [row["path"] for row in model["direct_files"]])
        self.assertEqual(
            ["site-v3/manifest.json"],
            [row["path"] for row in model["nested_manifests"]],
        )
        serialized = json.dumps(model, sort_keys=True)
        self.assertNotIn("frame.png", serialized)
        self.assertNotIn("stale self", serialized)
        self.assertIn("hero.gif", rendered)
        self.assertIn("site-v3/manifest.json", rendered)
        self.assertIn("1 direct capture file", rendered)
        self.assertIn("1 nested capture manifest", rendered)

    @unittest.skipUnless(importlib.util.find_spec("PIL"), "Pillow is not installed")
    def test_two_clean_full_runs_are_byte_identical(self) -> None:
        capture = load_capture_module()
        declared_font = resolve_test_capture_font(self, capture)
        if declared_font is None:
            return
        font = declared_font.path

        def digest_tree(root: Path) -> dict[str, str]:
            return {
                path.relative_to(root).as_posix(): hashlib.sha256(
                    path.read_bytes()
                ).hexdigest()
                for path in sorted(root.rglob("*"))
                if path.is_file()
            }

        with tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT) as temporary:
            base = Path(temporary)
            maps = []
            for label in ("run-a", "run-b"):
                run = base / label
                stdout = io.StringIO()
                with redirect_stdout(stdout):
                    result = capture.main(
                        [
                            "--font",
                            str(font),
                            "--source-sha",
                            SOURCE_SHA,
                            "--captured-at",
                            CAPTURED_AT,
                            "--scratch",
                            str(run / "scratch"),
                            "--site-output",
                            str(run / "site"),
                            "--shot-output",
                            str(run / "shot"),
                            *fixture_capture_args(),
                        ]
                    )
                self.assertEqual(0, result, stdout.getvalue())
                maps.append(
                    {
                        **{
                            f"site/{name}": digest
                            for name, digest in digest_tree(run / "site").items()
                        },
                        **{
                            f"shot/{name}": digest
                            for name, digest in digest_tree(run / "shot").items()
                        },
                    }
                )

        self.assertEqual(maps[0], maps[1])
        self.assertEqual(46, len(maps[0]))


if __name__ == "__main__":
    unittest.main()

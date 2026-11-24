from __future__ import annotations

import json
import importlib.util
import io
import os
import shutil
import sys
import tempfile
import types
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock
from tests.temp_roots import REAL_TEMP_ROOT


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def capture_module(*, image=None, image_draw=None, image_font=None):
    script = REPOSITORY_ROOT / "scripts" / "capture-readme-real-ledgers.py"
    spec = importlib.util.spec_from_file_location("capture_readme_real_ledgers", script)
    if spec is None or spec.loader is None:
        raise AssertionError("capture generator cannot be imported")
    module = importlib.util.module_from_spec(spec)
    needs_fake_pillow = (
        image is not None
        or image_draw is not None
        or image_font is not None
        or importlib.util.find_spec("PIL") is None
    )
    if needs_fake_pillow:
        pillow = types.ModuleType("PIL")
        pillow.Image = image if image is not None else types.SimpleNamespace()
        pillow.ImageDraw = (
            image_draw if image_draw is not None else types.SimpleNamespace()
        )
        pillow.ImageFont = (
            image_font if image_font is not None else types.SimpleNamespace()
        )
        with mock.patch.dict(sys.modules, {"PIL": pillow}):
            spec.loader.exec_module(module)
    else:
        spec.loader.exec_module(module)
    return module


def resolve_test_capture_font(test: unittest.TestCase, module):
    """Use one fixed system face or prove the generator's typed absence."""

    try:
        return module.resolve_capture_font(None, environ={})
    except module.ProtocolRefusal as refusal:
        test.assertEqual(module.FONT_ABSENT_CODE, refusal.code)
        test.assertIn(module.CAPTURE_FONT_FLAG, refusal.remedy)
        return None


class CaptureReadmeRealLedgersTests(unittest.TestCase):
    def test_fixed_candidates_end_with_the_debian_system_face(self) -> None:
        module = capture_module()

        self.assertEqual(
            Path("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"),
            module.FONT_CANDIDATES[-1],
        )

    def test_declared_capture_moment_makes_real_board_bank_repeatable(self) -> None:
        """Catches wall time or random capture IDs changing a repeated bank."""
        module = capture_module()
        font = resolve_test_capture_font(self, module)
        if font is None:
            return
        captured_at = "2026-09-02T22:00:00.000Z"
        with tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT) as temporary:
            root = Path(temporary)
            scratch = root / "scratch"
            output = root / "output"
            arguments = [
                "capture-readme-real-ledgers.py",
                "--font",
                str(font),
                "--captured-at",
                captured_at,
                "--scratch",
                str(scratch),
                "--output",
                str(output),
                "--source-sha",
                "a" * 40,
                "--doctor-root",
                str(root / "unused-doctor-root"),
                "--doctor-source",
                str(REPOSITORY_ROOT),
                "--capture",
                "harbor-board",
            ]

            snapshots = []
            for _ in range(2):
                with (
                    mock.patch.object(sys, "argv", arguments),
                    mock.patch.object(module, "_assert_rendered_identity_safe"),
                    redirect_stderr(io.StringIO()),
                    redirect_stdout(io.StringIO()),
                ):
                    try:
                        code = module.main()
                    except SystemExit as exc:
                        self.fail(
                            "README capture does not accept the declared reproducibility "
                            f"moment: {exc}"
                        )
                self.assertEqual(0, code)
                snapshots.append(
                    {
                        path.relative_to(output): path.read_bytes()
                        for path in output.rglob("*")
                        if path.is_file()
                    }
                )
                shutil.rmtree(scratch)
                shutil.rmtree(output)

        self.assertEqual(snapshots[0], snapshots[1])
        manifest = json.loads(snapshots[0][Path("manifest.json")])
        self.assertEqual(captured_at, manifest["captured_at"])

    def test_seeded_doctor_fixture_has_one_fixed_aged_envelope(self) -> None:
        """Catches a recapture depending on mutable pre-existing doctor state."""
        from floati.events import EventLog
        from floati.registry import Registry
        from floati.root import FloatiRoot

        module = capture_module()
        prepare = getattr(module, "_prepare_doctor_fixture", None)
        self.assertIsNotNone(prepare, "README capture doctor fixture seeder is missing")
        moment = datetime(2026, 9, 2, 22, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT) as temporary:
            root_path = Path(temporary) / "doctor"
            prepare(root_path, moment)
            root = FloatiRoot.open_direct_home(root_path, create=False)

            self.assertEqual(
                ("architect-codex", "builder-floati", "reviewer-opencode"),
                Registry(root).active_node_ids(),
            )
            messages = EventLog(root).records()

        self.assertEqual(1, len(messages))
        self.assertEqual("architect-codex", messages[0]["sender"])
        self.assertEqual("builder-floati", messages[0]["recipient"])
        self.assertEqual("2026-09-02T21:43:00.000Z", messages[0]["timestamp"])

    def test_font_selection_precedence_is_flag_then_variable_then_candidates(
        self,
    ) -> None:
        module = capture_module()
        with tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT) as temporary:
            root = Path(temporary)
            declared = root / "declared.ttf"
            variable = root / "variable.ttf"
            candidate = root / "candidate.ttf"
            for path in (declared, variable, candidate):
                path.write_bytes(path.name.encode("ascii"))

            resolver = getattr(module, "resolve_capture_font", None)
            self.assertIsNotNone(resolver, "README capture font resolver is missing")
            self.assertEqual(
                declared,
                resolver(
                    declared,
                    environ={module.CAPTURE_FONT_VARIABLE: str(variable)},
                    candidates=(candidate,),
                ),
            )
            self.assertEqual(
                variable,
                resolver(
                    None,
                    environ={module.CAPTURE_FONT_VARIABLE: str(variable)},
                    candidates=(candidate,),
                ),
            )
            self.assertEqual(
                candidate,
                resolver(None, environ={}, candidates=(candidate,)),
            )

    def test_invalid_font_declaration_never_falls_back(self) -> None:
        module = capture_module()
        with tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT) as temporary:
            root = Path(temporary)
            candidate = root / "candidate.ttf"
            candidate.write_bytes(b"candidate")
            missing = root / "missing.ttf"

            resolver = getattr(module, "resolve_capture_font", None)
            self.assertIsNotNone(resolver, "README capture font resolver is missing")
            with self.assertRaises(module.ProtocolRefusal) as caught:
                resolver(missing, environ={}, candidates=(candidate,))

        self.assertEqual(
            "readme_capture_font_declaration_invalid", caught.exception.code
        )

    def test_absent_font_is_typed_and_writes_no_partial_output(self) -> None:
        module = capture_module()
        with tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT) as temporary:
            root = Path(temporary)
            scratch = root / "scratch"
            output = root / "output"
            arguments = [
                "capture-readme-real-ledgers.py",
                "--scratch",
                str(scratch),
                "--output",
                str(output),
                "--source-sha",
                "a" * 40,
                "--doctor-root",
                str(root / "unused-doctor-root"),
                "--doctor-source",
                str(REPOSITORY_ROOT),
            ]
            environment = dict(os.environ)
            environment.pop("FLOATI_CAPTURE_FONT", None)
            stderr = io.StringIO()
            with (
                mock.patch.object(sys, "argv", arguments),
                mock.patch.object(module, "FONT_CANDIDATES", (), create=True),
                mock.patch.dict(os.environ, environment, clear=True),
                mock.patch.object(module, "_board_capture", return_value={}),
                mock.patch.object(module, "_replay_capture", return_value={}),
                mock.patch.object(module, "_onboard_capture", return_value={}),
                mock.patch.object(module, "_chart_capture", return_value={}),
                mock.patch.object(module, "_doctor_capture", return_value={}),
                redirect_stderr(stderr),
                redirect_stdout(io.StringIO()),
            ):
                code = module.main()

            self.assertEqual(3, code)
            report = json.loads(stderr.getvalue())
            self.assertEqual("readme_capture_font_absent", report["condition"])
            self.assertEqual(
                {"flag": "--font", "variable": "FLOATI_CAPTURE_FONT"},
                report["declaration"],
            )
            self.assertFalse(scratch.exists())
            self.assertFalse(output.exists())

    def test_terminal_frame_loads_declared_font_bytes_without_a_path_probe(
        self,
    ) -> None:
        calls: list[tuple[object, int]] = []

        class FakeImage:
            def __init__(self, size) -> None:
                self.width, self.height = size

        class FakeImageModule:
            @staticmethod
            def new(_mode, size, _color):
                return FakeImage(size)

        class FakeDraw:
            def text(self, *_args, **_kwargs) -> None:
                return None

            def textlength(self, text, *, font) -> int:
                return len(text)

        class FakeImageDraw:
            @staticmethod
            def Draw(_image):
                return FakeDraw()

        class FakeImageFont:
            @staticmethod
            def truetype(source, size):
                calls.append((source, size))
                return object()

        module = capture_module(
            image=FakeImageModule,
            image_draw=FakeImageDraw,
            image_font=FakeImageFont,
        )
        with tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT) as temporary:
            declared = Path(temporary) / "declared.ttf"
            declared.write_bytes(b"declared-font-bytes")
            try:
                module._terminal_frame("testimony", "dark", font_path=declared)
            except TypeError as exc:
                self.fail(f"terminal frame does not accept a declared font: {exc}")

        self.assertEqual(1, len(calls))
        source, size = calls[0]
        self.assertIsInstance(source, io.BytesIO)
        self.assertEqual(b"declared-font-bytes", source.getvalue())
        self.assertEqual(20, size)

    def test_unredacted_homebrew_scratch_is_written_without_instrument_roots(
        self,
    ) -> None:
        """Control: Homebrew prefixes are not a governed-identity token."""
        module = capture_module()
        scratch = Path("/opt/homebrew/var/floati-cap2-test/scratch")
        with tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT) as temporary:
            output = Path(temporary)
            module._write_capture(
                output,
                "harbor-board",
                f"ROOT={scratch}/harbor-board\n$ floati board --root $ROOT\n",
            )
            written = (output / "harbor-board-standard.txt").read_text(
                encoding="utf-8"
            )
        self.assertIn(f"ROOT={scratch}/harbor-board", written)

    def test_exposure_redacts_scratch_roots_before_write(self) -> None:
        module = capture_module()
        scratch = Path("/opt/homebrew/var/floati-cap2-test/scratch")
        with tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT) as temporary:
            output = Path(temporary)
            module._write_capture(
                output,
                "harbor-board",
                f"ROOT={scratch}/harbor-board\n$ floati board --root $ROOT\n",
                instrument_roots=(scratch,),
            )
            written = (output / "harbor-board-standard.txt").read_text(
                encoding="utf-8"
            )
            svg = (output / "harbor-board-dark.svg").read_text(encoding="utf-8")
        self.assertNotIn("/opt/homebrew", written)
        self.assertNotIn(str(scratch), written)
        self.assertIn("ROOT=<scratch>/harbor-board", written)
        self.assertNotIn("/opt/homebrew", svg)
        self.assertIn("ROOT=&lt;scratch&gt;/harbor-board", svg)

    def test_static_capture_refuses_governed_identity_before_writing(self) -> None:
        module = capture_module()
        governed = bytes.fromhex("2f707269766174652f746d70").decode("ascii")
        with tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT) as temporary:
            output = Path(temporary)

            with self.assertRaises(module.RenderedIdentityRefusal) as caught:
                module._write_capture(
                    output,
                    "harbor-chart-multibus",
                    f"DECLARED_ROOTS={governed}/fleet/declared-roots.json",
                )

            self.assertEqual("capture_rendered_identity_forbidden", caught.exception.code)
            self.assertEqual([], list(output.iterdir()))

    def test_rendered_frames_refuse_governed_identity_before_rasterization(self) -> None:
        module = capture_module()
        governed = (
            bytes.fromhex("2f55736572732f").decode("ascii") + "operator/fleet",
            bytes.fromhex("2f707269766174652f746d70").decode("ascii") + "/fleet",
            bytes.fromhex("2f707269766174652f7661722f746d70").decode("ascii") + "/fleet",
            bytes.fromhex("2f7661722f666f6c64657273").decode("ascii") + "/fleet",
            bytes.fromhex("2f746d70").decode("ascii") + "/fleet",
            bytes.fromhex("63687269736d656e656e64657a").decode("ascii"),
        )

        for testimony in governed:
            with self.subTest(testimony=testimony):
                with self.assertRaises(module.RenderedIdentityRefusal) as caught:
                    module._assert_rendered_identity_safe(
                        "flight-recorder-replay", [f"ROOT={testimony}"]
                    )
                self.assertEqual("capture_rendered_identity_forbidden", caught.exception.code)

    def test_capture_selection_does_not_retake_unselected_frames(self) -> None:
        with tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT) as temporary:
            base = Path(temporary)
            scratch = base / "scratch"
            output = base / "output"
            module = capture_module()
            font = resolve_test_capture_font(self, module)
            if font is None:
                return
            arguments = [
                    "capture-readme-real-ledgers.py",
                    "--font",
                    str(font),
                    "--scratch",
                    str(scratch),
                    "--output",
                    str(output),
                    "--source-sha",
                    "a" * 40,
                    "--doctor-root",
                    str(base / "unused-doctor-root"),
                    "--doctor-source",
                    str(REPOSITORY_ROOT),
                    "--capture",
                    "flight-recorder-replay",
                    "--capture",
                    "harbor-chart-multibus",
                ]
            with (
                mock.patch.object(sys, "argv", arguments),
                mock.patch.object(
                    module,
                    "_replay_capture",
                    return_value={"name": "flight-recorder-replay"},
                ) as replay,
                mock.patch.object(
                    module,
                    "_chart_capture",
                    return_value={"name": "harbor-chart-multibus"},
                ) as chart,
                mock.patch.object(module, "_board_capture") as board,
                mock.patch.object(module, "_onboard_capture") as onboard,
                mock.patch.object(module, "_doctor_capture") as doctor,
                redirect_stdout(io.StringIO()),
            ):
                result = module.main()

            self.assertEqual(0, result)
            replay.assert_called_once()
            chart.assert_called_once()
            board.assert_not_called()
            onboard.assert_not_called()
            doctor.assert_not_called()
            manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(
                ["flight-recorder-replay", "harbor-chart-multibus"],
                [capture["name"] for capture in manifest["captures"]],
            )
            self.assertFalse(any(output.glob("harbor-board-*")))
            self.assertFalse(any(output.glob("onboard-wizard-*")))
            self.assertFalse(any(output.glob("doctor-delivery-health-*")))


if __name__ == "__main__":
    unittest.main()

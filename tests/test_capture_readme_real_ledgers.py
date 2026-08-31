from __future__ import annotations

import json
import importlib.util
import io
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def capture_module():
    script = REPOSITORY_ROOT / "scripts" / "capture-readme-real-ledgers.py"
    spec = importlib.util.spec_from_file_location("capture_readme_real_ledgers", script)
    if spec is None or spec.loader is None:
        raise AssertionError("capture generator cannot be imported")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class CaptureReadmeRealLedgersTests(unittest.TestCase):
    def test_static_capture_refuses_governed_identity_before_writing(self) -> None:
        module = capture_module()
        governed = bytes.fromhex("2f707269766174652f746d70").decode("ascii")
        with tempfile.TemporaryDirectory(dir="\x2ftmp") as temporary:
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
        with tempfile.TemporaryDirectory(dir="\x2ftmp") as temporary:
            base = Path(temporary)
            scratch = base / "scratch"
            output = base / "output"
            module = capture_module()
            arguments = [
                    "capture-readme-real-ledgers.py",
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

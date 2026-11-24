"""The fixed paired job must refuse caller-selected authority coordinates."""
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from tests.temp_roots import REAL_TEMP_ROOT


class ParallelQualificationCliTests(unittest.TestCase):
    def test_cli_refuses_authority_overrides_without_touching_owned_artifacts(self):
        root = Path(__file__).resolve().parents[1]
        script = root / "scripts" / "qualify-parallel.py"
        self.assertTrue(script.is_file(), "fixed-input qualification entry point is absent")
        with tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT) as directory:
            for ordinal, flag in enumerate(("--profile", "--registry", "--root",
                                            "--workers", "--reference", "--sha")):
                with self.subTest(flag=flag):
                    output = Path(directory) / str(ordinal)
                    output.mkdir()
                    (output / "sentinel").write_bytes(b"must remain unchanged\n")
                    # Existing output also prevents a broken parser from starting
                    # a nested full suite while this negative control runs.
                    result = subprocess.run(
                        [sys.executable, "-B", str(script), "--output", str(output),
                         flag, "/caller-override"], cwd=root, capture_output=True,
                        text=True, timeout=10,
                    )
                    self.assertEqual(2, result.returncode, result.stderr)
                    self.assertIn("unrecognized arguments", result.stderr)
                    self.assertIn(flag, result.stderr)
                    self.assertEqual({"sentinel": b"must remain unchanged\n"},
                                     {path.name: path.read_bytes() for path in output.iterdir()})


if __name__ == "__main__":
    unittest.main()

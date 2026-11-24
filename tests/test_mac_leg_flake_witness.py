import json
import signal
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests.readiness import communicate, wait_readable
from tests.temp_roots import REAL_TEMP_ROOT


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check_mac_leg_flake.py"


class MacLegFlakeWitnessTests(unittest.TestCase):
    def test_missing_witness_inputs_are_refused(self):
        inputs = {
            "scratch_required": ["--scratch", "unused"],
            "scratch_prefix_required": ["--scratch-prefix", "witness."],
            "hold_markers_required": ["--hold-markers", "/unused-ready", "/unused-go"],
        }
        for missing in inputs:
            with self.subTest(missing=missing):
                arguments = [arg for code, option in inputs.items()
                             if code != missing for arg in option]
                result = subprocess.run(
                    [sys.executable, "-B", str(SCRIPT), *arguments, __name__],
                    capture_output=True, text=True, timeout=5,
                )

                self.assertEqual(2, result.returncode, result.stderr)
                self.assertIn(f"error: {missing}:", result.stderr)

    def test_termination_reaps_the_test_group_and_load_worker(self):
        # Substitute idle children for unittest and yes. Each exits on EOF if
        # the witness dies, so a failing witness cannot leave live workers.
        driver = """
import os
from pathlib import Path
import runpy
import subprocess
import sys
from unittest import mock

script, root, *arguments = sys.argv[1:]
launch = subprocess.Popen
read_fd, write_fd = os.pipe()

def idle(_command, **options):
    return launch([sys.executable, "-B", "-c",
        "import os, sys; os.read(int(sys.argv[1]), 1)", str(read_fd)],
        pass_fds=(read_fd,), **options)

namespace = runpy.run_path(script)
main = namespace["main"]
sys.argv = [script, *arguments]
with mock.patch.dict(main.__globals__, {"Path": mock.Mock(
         side_effect=lambda value: Path(root) if value == "\x2fprivate/tmp" else Path(value),
         home=Path.home)}), mock.patch.object(os, "cpu_count", return_value=1), \\
         mock.patch.object(subprocess, "Popen", side_effect=idle):
    raise SystemExit(main())
"""
        with tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT) as temporary:
            root = Path(temporary)
            scratch = root / "witness"
            scratch.mkdir(mode=0o700)
            markers = [root / name for name in ("ready", "release")]
            for marker in markers:
                marker.touch()
            child = subprocess.Popen(
                [sys.executable, "-B", "-c", driver, str(SCRIPT), str(root),
                 "--scratch", str(scratch), "--scratch-prefix", scratch.name,
                 "--hold-markers", *(str(marker) for marker in markers), "--load", __name__],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            try:
                wait_readable(child.stdout, "witness child readiness")
                ready = json.loads(child.stdout.readline())
                self.assertEqual(1, len(ready["load_pids"]))
                child.send_signal(signal.SIGTERM)
                _, error = communicate(child, "witness termination")
                self.assertNotEqual(0, child.returncode, error)
                summary = scratch / "summary.json"
                self.assertTrue(summary.is_file(), "termination bypassed the cleanup receipt")
                record = json.loads(summary.read_text())
                self.assertEqual(ready["test_pid"], record["test_group_cleanup"]["group"])
                self.assertTrue(record["test_group_cleanup"]["gone"])
                self.assertEqual(1, len(record["load_exit_codes"]))
            finally:
                if child.poll() is None:
                    child.terminate()
                    communicate(child, "witness test cleanup")

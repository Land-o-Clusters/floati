"""Explicit latency-instrument self-test; run before banking a latency receipt.

Run with ``python -m unittest tests.harbor_net_latency_selftest``. The fixed-delay
provider is outside default discovery because it exercises a measurement path.
"""

import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from tests.readiness import wait_until
from tests.temp_roots import REAL_TEMP_ROOT
from tests.test_net_latency_1 import SOURCE, SCRIPT, _LatencyFleet, _latency_argv


PROVIDER = SOURCE / "tests/fixtures/latency_provider.py"


class NetLatencySelftestTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.fixture = _LatencyFleet(self.base)
        self.out = self.base / "receipts"
        self.out.mkdir()

    def test_real_ask_path_with_fixed_delay_provider_and_ambient_root_ignored(self):
        ready = self.base / "ready"
        provider = subprocess.Popen([
            "/usr/bin/python3", "-B", str(PROVIDER), "--root", str(self.fixture.fleet),
            "--repository", str(self.fixture.repository), "--sha", self.fixture.sha,
            "--count", "4", "--delay", "0.08", "--ready", str(ready)],
            env=self.fixture.environment, cwd=self.fixture.repository, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True)
        try:
            wait_until(lambda: ready.exists() or provider.poll() is not None,
                       "fake provider readiness before warmup")
            self.assertTrue(ready.exists(), "fake provider exited before warmup")
            foreign = self.base / "foreign-root"
            result = subprocess.run([
                "/usr/bin/python3", "-B", str(SCRIPT),
                *_latency_argv(self.fixture, self.out)], cwd=self.fixture.repository,
                env=dict(self.fixture.environment, FLOATI_BUS_ROOT=str(foreign)),
                capture_output=True, text=True, timeout=15)
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            self.assertEqual(1, len(result.stdout.splitlines()))
            receipt = json.loads(next(self.out.glob("*.json")).read_text())
            self.assertEqual(3, receipt["n"])
            self.assertEqual(3, len(receipt["samples"]))
            self.assertGreaterEqual(receipt["min_seconds"], 0.08)
            self.assertLess(receipt["max_seconds"], 3)
            self.assertFalse(foreign.exists())
            output, errors = provider.communicate(timeout=5)
            self.assertEqual(0, provider.returncode, output + errors)
        finally:
            if provider.poll() is None:
                provider.terminate()
            provider.communicate(timeout=5)


if __name__ == "__main__":
    unittest.main()

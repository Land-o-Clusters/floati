from __future__ import annotations

import json
import os
import select
import signal
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


class WatchTests(unittest.TestCase):
    def test_help_names_initial_and_changed_streaming_observations(self) -> None:
        result = subprocess.run(
            [sys.executable, "-m", "floati", "watch", "--help"],
            cwd=REPOSITORY_ROOT,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("initial observation", result.stdout)
        self.assertIn("each changed observation", result.stdout)

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name) / "fleet"
        subprocess.run(
            [sys.executable, "-m", "floati", "init", "--root", str(self.home)],
            cwd=REPOSITORY_ROOT, check=True, capture_output=True, text=True,
        )
        self.destination = Path(self.temp.name) / "installed"
        destination_scripts = self.destination / "scripts"
        destination_scripts.mkdir(parents=True)
        (destination_scripts / "floati").write_bytes(b"installed\n")
        self.shadow = Path(self.temp.name) / "shadow"
        self.shadow.mkdir()
        (self.shadow / "floati").write_bytes(b"shadow\n")
        self.environment = {
            **os.environ,
            "PATH": os.pathsep.join((
                str(self.shadow),
                str(destination_scripts),
            )),
        }

    def run_cli(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "floati", *args], cwd=REPOSITORY_ROOT,
            check=False, capture_output=True, text=True, env=self.environment,
        )

    def test_unchanged_poll_iterations_emit_only_the_initial_delta(self) -> None:
        result = self.run_cli(
            "watch", "--root", str(self.home), "--destination", str(self.destination),
            "--interval", "0.05", "--iterations", "3",
        )

        self.assertEqual(0, result.returncode, result.stderr)
        lines = result.stdout.splitlines()
        self.assertEqual(1, len(lines))
        artifact = json.loads(lines[0])
        self.assertEqual("watch", artifact["command"])
        self.assertEqual("initial", artifact["evidence"]["delta"]["kind"])
        self.assertEqual(1, artifact["schema_version"])
        self.assertEqual(
            "found",
            artifact["evidence"]["delta"]["snapshot"]["installer_shadow"]["outcome"],
        )

    def test_unbounded_watch_streams_initial_and_changed_artifacts_before_exit(self) -> None:
        child = subprocess.Popen(
            [
                sys.executable, "-m", "floati", "watch", "--root", str(self.home),
                "--destination", str(self.destination), "--interval", "0.05",
            ],
            cwd=REPOSITORY_ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=self.environment,
        )
        def close_child() -> None:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=2)
            if child.stdout is not None:
                child.stdout.close()
            if child.stderr is not None:
                child.stderr.close()

        self.addCleanup(close_child)
        self.assertIsNotNone(child.stdout)

        ready, _, _ = select.select([child.stdout], [], [], 1.0)
        self.assertTrue(ready, "initial watch artifact must stream before the unbounded loop exits")
        initial = json.loads(child.stdout.readline())
        self.assertEqual("initial", initial["evidence"]["delta"]["kind"])

        registered = self.run_cli(
            "register", "--root", str(self.home), "lane-a", "--harness", "Codex"
        )
        self.assertEqual(0, registered.returncode, registered.stderr)
        ready, _, _ = select.select([child.stdout], [], [], 1.0)
        self.assertTrue(ready, "changed watch artifact must stream while the loop remains active")
        changed = json.loads(child.stdout.readline())
        self.assertEqual("change", changed["evidence"]["delta"]["kind"])
        self.assertIsNone(child.poll())

        child.terminate()
        child.wait(timeout=2)

    def test_interval_below_floor_refuses_with_artifact_exit(self) -> None:
        result = self.run_cli(
            "watch", "--root", str(self.home), "--destination", str(self.destination),
            "--interval", "0.01", "--iterations", "1",
        )

        self.assertEqual(20, result.returncode)
        artifact = json.loads(result.stderr)
        self.assertEqual("refused", artifact["status"])
        self.assertEqual("watch_interval_invalid", artifact["evidence"]["code"])

    def test_interrupt_after_streamed_delta_exits_cleanly_without_traceback(self) -> None:
        child = subprocess.Popen(
            [
                sys.executable, "-m", "floati", "watch", "--root", str(self.home),
                "--destination", str(self.destination), "--interval", "0.05",
            ],
            cwd=REPOSITORY_ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=self.environment,
        )
        def close_child() -> None:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=2)
            if child.stdout is not None:
                child.stdout.close()
            if child.stderr is not None:
                child.stderr.close()

        self.addCleanup(close_child)
        self.assertIsNotNone(child.stdout)
        self.assertIsNotNone(child.stderr)
        ready, _, _ = select.select([child.stdout], [], [], 1.0)
        self.assertTrue(ready)
        self.assertEqual("initial", json.loads(child.stdout.readline())["evidence"]["delta"]["kind"])

        child.send_signal(signal.SIGINT)
        child.wait(timeout=2)
        self.assertEqual(0, child.returncode)
        self.assertEqual("", child.stderr.read())


if __name__ == "__main__":
    unittest.main()

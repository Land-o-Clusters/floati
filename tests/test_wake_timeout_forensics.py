from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from floati import fixture_ids as public_ids
from floati.registry import Registry
from floati.root import FloatiRoot
from floati.wake_daemon_contract import DaemonCoordinate
from floati.wake_timeout_forensics import run_with_timeout_forensics
from tests.temp_roots import REAL_TEMP_ROOT


_HANGING_CHILD = """\
import os
r, w = os.pipe()
os.read(r, 1)
"""

_GATED_CHILD = """\
import sys
import time
from pathlib import Path
go, done = Path(sys.argv[1]), Path(sys.argv[2])
while not go.is_file():
    time.sleep(0.05)
done.write_text("done\\n", encoding="utf-8")
"""

_OUTPUT_AFTER_CAP_CHILD = """\
import os
import sys
import time
from pathlib import Path
capacity = int(sys.argv[1])
pid_path, done = Path(sys.argv[2]), Path(sys.argv[3])
pid_path.write_text(str(os.getpid()), encoding="utf-8")
time.sleep(1.4)
remaining = capacity + 4096
chunk = b"x" * 4096
while remaining:
    written = os.write(1, chunk[:remaining])
    remaining -= written
done.write_text("done\\n", encoding="utf-8")
"""


class WakeTimeoutForensicsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.cwd = self.base / "cwd"
        self.cwd.mkdir()

    def _hanging_script(self) -> Path:
        script = self.base / "hang-on-pipe"
        script.write_text("#!" + sys.executable + "\n" + _HANGING_CHILD, encoding="utf-8")
        script.chmod(0o700)
        return script

    def _gated_script(self) -> Path:
        script = self.base / "finish-after-gate"
        script.write_text("#!" + sys.executable + "\n" + _GATED_CHILD, encoding="utf-8")
        script.chmod(0o700)
        return script

    def _output_after_cap_script(self) -> Path:
        script = self.base / "output-after-cap"
        script.write_text(
            "#!" + sys.executable + "\n" + _OUTPUT_AFTER_CAP_CHILD,
            encoding="utf-8",
        )
        script.chmod(0o700)
        return script

    def _measure_pipe_capacity(self) -> int:
        read_fd, write_fd = os.pipe()
        try:
            os.set_blocking(write_fd, False)
            capacity = 0
            chunk = b"x" * 4096
            while True:
                try:
                    capacity += os.write(write_fd, chunk)
                except BlockingIOError:
                    return capacity
        finally:
            os.close(read_fd)
            os.close(write_fd)

    def _terminate_for_cleanup(self, pid: int) -> None:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            return
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return
            time.sleep(0.02)

    def _assert_reaped(self, pid: int) -> None:
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return
            time.sleep(0.02)
        self.fail(f"child pid {pid} remained present instead of being reaped")

    def test_pipe_blocked_child_sidecar_names_the_blocked_call(self) -> None:
        script = self._hanging_script()
        sidecar = self.base / "sidecar.json"
        argv = (str(script),)
        with self.assertRaises(subprocess.TimeoutExpired):
            run_with_timeout_forensics(
                argv,
                self.cwd,
                1,
                sidecar_path=sidecar,
                attempt_key="scratch-pipe-attempt-1",
            )
        self.assertTrue(sidecar.is_file())
        row = json.loads(sidecar.read_text(encoding="utf-8"))
        self.assertEqual("scratch-pipe-attempt-1", row["attempt_key"])
        self.assertEqual([str(script)], row["argv"])
        self.assertIsInstance(row["pid"], int)
        blob = f"{row.get('lsof_stdout', '')}\n{row.get('sample_stdout', '')}".upper()
        self.assertTrue(
            "PIPE" in blob or "FIFO" in blob or row.get("blocked_call") in {"read", "pipe"},
            msg=f"sidecar did not name the blocked call: {row}",
        )
        self.addCleanup(self._terminate_for_cleanup, int(row["pid"]))

    def test_timeout_caps_the_wait_and_keeps_the_child(self) -> None:
        script = self._hanging_script()
        sidecar = self.base / "keep-child.json"
        with self.assertRaises(subprocess.TimeoutExpired):
            run_with_timeout_forensics(
                (str(script),),
                self.cwd,
                1,
                sidecar_path=sidecar,
                attempt_key="scratch-keep-child-1",
            )
        pid = int(json.loads(sidecar.read_text(encoding="utf-8"))["pid"])
        self.addCleanup(self._terminate_for_cleanup, pid)
        os.kill(pid, 0)

    def test_kept_child_can_finish_work_after_the_cap(self) -> None:
        script = self._gated_script()
        go = self.base / "go"
        marker = self.base / "finished"
        sidecar = self.base / "finish-after-cap.json"
        with self.assertRaises(subprocess.TimeoutExpired):
            run_with_timeout_forensics(
                (str(script), str(go), str(marker)),
                self.cwd,
                1,
                sidecar_path=sidecar,
                attempt_key="scratch-finish-after-cap-1",
            )
        pid = int(json.loads(sidecar.read_text(encoding="utf-8"))["pid"])
        self.addCleanup(self._terminate_for_cleanup, pid)
        os.kill(pid, 0)
        go.write_text("go\n", encoding="utf-8")
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and not marker.is_file():
            time.sleep(0.02)
        self.assertEqual("done\n", marker.read_text(encoding="utf-8"))

    def test_child_writes_beyond_measured_pipe_capacity_after_cap(self) -> None:
        capacity = self._measure_pipe_capacity()
        script = self._output_after_cap_script()
        pid_path = self.base / "output-child.pid"
        marker = self.base / "output-finished"
        with self.assertRaises(subprocess.TimeoutExpired):
            run_with_timeout_forensics(
                (str(script), str(capacity), str(pid_path), str(marker)),
                self.cwd,
                1,
            )
        pid = int(pid_path.read_text(encoding="utf-8"))
        self.addCleanup(self._terminate_for_cleanup, pid)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and not marker.is_file():
            time.sleep(0.02)
        self.assertEqual("done\n", marker.read_text(encoding="utf-8"))
        self._assert_reaped(pid)

    def test_capped_attempts_do_not_retain_parent_descriptors(self) -> None:
        before = len(os.listdir("/dev/fd"))
        attempts = 3
        for index in range(attempts):
            with self.assertRaises(subprocess.TimeoutExpired):
                run_with_timeout_forensics(
                    (
                        sys.executable,
                        "-c",
                        "import time; time.sleep(0.4)",
                    ),
                    self.cwd,
                    0.05,
                    attempt_key=f"descriptor-attempt-{index}",
                )
        after = len(os.listdir("/dev/fd"))
        self.assertLess(after - before, attempts, msg=(before, after, attempts))

    def test_success_reads_file_output_into_completed_process(self) -> None:
        argv = (
            sys.executable,
            "-c",
            "import sys; print('wake-ok'); print('wake-note', file=sys.stderr)",
        )
        result = run_with_timeout_forensics(argv, self.cwd, 3)
        self.assertEqual(list(argv), result.args)
        self.assertEqual(0, result.returncode)
        self.assertEqual("wake-ok\n", result.stdout)
        self.assertEqual("wake-note\n", result.stderr)

    def test_cursor_default_runner_photographs_before_timeout_result(self) -> None:
        from floati.wake_daemon_adapters import CursorResumeWakeAdapter
        from floati.wake_daemon_contract import AdapterBindingStore
        from floati.wake_daemon_adapters import adapter_contract_digest

        script = self._hanging_script()
        root = FloatiRoot.open_direct_home(self.base / "fleet-alpha", create=True)
        Registry(root).register(public_ids.builder('a'), "worker")
        workspace = self.base / "workspace"
        workspace.mkdir()
        coordinate = DaemonCoordinate(root, public_ids.builder('a'), "cursor")
        binding = AdapterBindingStore(root).write(
            coordinate,
            session_id="session-1",
            workspace=workspace,
            executable=script,
            adapter_version="1",
            adapter_digest=adapter_contract_digest("cursor"),
            binding_epoch=1,
        )
        adapter = CursorResumeWakeAdapter(coordinate)
        adapter.arm_timeout_forensics("daemon-deadbeef0000-1-1-attempt-1")
        result = adapter.request_wake(binding, "wake exact cursor session", 1)
        self.assertEqual("unknown", result.outcome)
        self.assertEqual("wake_daemon_adapter_timeout", result.reason_code)
        sidecar = root.resolve_relative(
            Path("receipts/wake-timeout-forensics/daemon-deadbeef0000-1-1-attempt-1.json")
        )
        self.assertTrue(sidecar.is_file())
        row = json.loads(sidecar.read_text(encoding="utf-8"))
        self.assertEqual(str(script), row["argv"][0])
        blob = f"{row.get('lsof_stdout', '')}\n{row.get('sample_stdout', '')}".upper()
        self.assertTrue(
            "PIPE" in blob or "FIFO" in blob or row.get("blocked_call") in {"read", "pipe"},
            msg=f"adapter sidecar did not name the blocked call: {row}",
        )
        self.addCleanup(self._terminate_for_cleanup, int(row["pid"]))


if __name__ == "__main__":
    unittest.main()

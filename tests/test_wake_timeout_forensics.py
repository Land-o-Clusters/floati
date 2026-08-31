from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from floati import fixture_ids as public_ids
from floati.registry import Registry
from floati.root import FloatiRoot
from floati.wake_daemon_contract import DaemonCoordinate
from floati.wake_timeout_forensics import run_with_timeout_forensics


_HANGING_CHILD = """\
import os
r, w = os.pipe()
os.read(r, 1)
"""


class WakeTimeoutForensicsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(dir="\x2fprivate\x2ftmp")
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.cwd = self.base / "cwd"
        self.cwd.mkdir()

    def _hanging_script(self) -> Path:
        script = self.base / "hang-on-pipe"
        script.write_text("#!" + sys.executable + "\n" + _HANGING_CHILD, encoding="utf-8")
        script.chmod(0o700)
        return script

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


if __name__ == "__main__":
    unittest.main()

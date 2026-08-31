from __future__ import annotations

import tempfile
import sys
import time
import unittest
from pathlib import Path

from floati.adapters.pi import PiRpcSession
from floati.workers import WorkerAdapterFailure


HARNESS = Path(__file__).parent / "fixtures" / "pi-rpc" / "deadline_race_harness.py"


class PiDeadlineClassificationTests(unittest.TestCase):
    def test_queued_malformed_frame_wins_over_an_expired_read_deadline(self) -> None:
        with tempfile.TemporaryDirectory(dir="\x2fprivate\x2ftmp") as temporary:
            root = Path(temporary)
            workspace = root / "workspace"
            workspace.mkdir()
            marker = root / "malformed-queued.marker"
            command = (str(Path(sys.executable).resolve()), str(HARNESS), str(marker))
            session = PiRpcSession(command, workspace)
            try:
                session.start(deadline_seconds=5)
                session._write({"id": "req-1", "type": "prompt", "message": "fixture"})
                deadline = time.monotonic() + 2
                while not marker.exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(marker.is_file(), "fixture must queue malformed output before the read")
                session._deadline = time.monotonic() - 1
                with self.assertRaises(WorkerAdapterFailure) as caught:
                    session._read()
                self.assertEqual("protocol_error", caught.exception.code)
            finally:
                session.close()


if __name__ == "__main__":
    unittest.main()

"""Headless bounds use real disposable providers, never native account state."""

from __future__ import annotations

from tests.readiness import wait_until

import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from floati.adapters.headless_template import HarnessProfile, HeadlessProfileAdapter
from floati.worker_errors import WorkerAdapterFailure
from tests.temp_roots import REAL_TEMP_ROOT
from tests.test_headless_worker_protocol import headless_adapter_types


class AdhocStage2TemplateTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.parent = self.directory / "work"
        for module in ("floati.adapters.headless_template", "floati.adapters.codex_live"):
            patcher = mock.patch(module + "._WORKSPACE_PARENT", self.parent)
            patcher.start()
            self.addCleanup(patcher.stop)

    def population(self):
        # Shared transport runs once; any future override earns its own run.
        transport = ("spawn", "drive", "_read_bounded", "_wait", "_terminate")
        return [HeadlessProfileAdapter, *(adapter for adapter in headless_adapter_types()
            if any(getattr(adapter, method) is not getattr(HeadlessProfileAdapter, method)
                   for method in transport))]

    def spawn(self, adapter_type, body, *, duration=5, isolate=True):
        label = adapter_type.__name__
        script = self.directory / (label + ".py")
        script.write_text(body, encoding="utf-8")
        profile = HarnessProfile(name=adapter_type.name,
            command=("/usr/bin/python3", "-B", str(script)), stderr_name=label + ".stderr")
        adapter = adapter_type(profile, isolate_process_group=isolate)
        self.addCleanup(adapter.cancel)
        item = {"id": "work-" + label, "workspace": str(self.parent / ("work-" + label)),
                "title": "disposable bound probe"}
        handle = adapter.spawn(item, deadline_seconds=duration)
        return adapter, handle

    def test_silent_native_provider_cannot_wait_past_its_deadline(self):
        # A blocking stdout read would wait for the provider's sleep instead.
        for adapter_type in self.population():
            with self.subTest(adapter=adapter_type.__name__):
                adapter, handle = self.spawn(adapter_type,
                    "import time\ntime.sleep(30)\n", duration=0.25)
                started = time.monotonic()
                with self.assertRaises(WorkerAdapterFailure) as caught:
                    adapter.drive(handle, deadline_seconds=0.25)
                self.assertEqual("deadline_exceeded", caught.exception.code)
                self.assertLess(time.monotonic() - started, 10)
                self.assertIsNotNone(handle.process.returncode)

    def test_stderr_overflow_cannot_be_a_success_or_unbounded_evidence(self):
        # Success-shaped stdout cannot hide a separate stderr byte overflow.
        for adapter_type in self.population():
            with self.subTest(adapter=adapter_type.__name__):
                adapter, handle = self.spawn(adapter_type,
                    "import os\nos.write(1, b'{}\\n')\n"
                    "for _ in range(129):\n    os.write(2, b'x' * 8192)\n")
                with self.assertRaises(WorkerAdapterFailure) as caught:
                    adapter.drive(handle, deadline_seconds=5)
                self.assertEqual("output_limit_exceeded", caught.exception.code)
                self.assertLessEqual(handle.stderr_path.stat().st_size, 1_048_576)
                self.assertIsNotNone(handle.process.returncode)

    def test_forced_native_cleanup_reaps_before_returning(self):
        # Ignoring TERM takes the actual KILL path. Shorten only its grace wait;
        # the process, signal delivery and final wait/reap remain real.
        adapter, handle = self.spawn(HeadlessProfileAdapter,
            "import signal, time\nfrom pathlib import Path\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            "Path('.floati/provider-ready').touch()\ntime.sleep(180)\n", isolate=False)
        ready = handle.filesystem_workspace / ".floati/provider-ready"
        wait_until(ready.exists, "fake provider TERM handler")
        original_wait = handle.process.wait
        try:
            with mock.patch.object(handle.process, "wait",
                    side_effect=lambda timeout=None: original_wait(timeout=min(timeout or 10, 0.05))):
                adapter.cancel()
            self.assertIsNotNone(handle.process.returncode,
                "cancellation must reap the KILLed child before relinquishing ownership")
        finally:
            # Own only this fixture child even when the pre-fix assertion fails.
            if handle.process.poll() is None:
                handle.process.kill()
            original_wait(timeout=5)

    def test_concurrent_native_streams_keep_bounded_stderr_and_raw_stdout(self):
        adapter, handle = self.spawn(HeadlessProfileAdapter,
            "import os, threading\n"
            "def stderr():\n    for _ in range(32):\n        os.write(2, b'x' * 8192)\n"
            "writer = threading.Thread(target=stderr)\nwriter.start()\n"
            "os.write(1, b'provider diagnostic, not JSON\\n')\nwriter.join()\n")
        result = adapter.drive(handle, deadline_seconds=5)
        self.assertEqual({"raw": "provider diagnostic, not JSON\n"}, result["payload"])
        self.assertEqual(b"x" * 262_144, handle.stderr_path.read_bytes())
        self.assertEqual(0, handle.process.returncode)

    def test_provider_cannot_bypass_output_limit_by_writing_evidence_file(self):
        # The provider can write the fresh workspace. Its diagnostic path must
        # still be read with a bound even when bytes did not arrive on stderr.
        adapter, handle = self.spawn(HeadlessProfileAdapter,
            "from pathlib import Path\nimport os\n"
            "Path('.floati/HeadlessProfileAdapter.stderr').write_bytes(b'x' * 1_048_577)\n"
            "os.write(1, b'{}\\n')\n")
        with self.assertRaises(WorkerAdapterFailure) as caught:
            adapter.drive(handle, deadline_seconds=5)
        self.assertEqual("output_limit_exceeded", caught.exception.code)
        self.assertIsNotNone(handle.process.returncode)

    def test_unproved_owned_group_cannot_be_reported_as_cleaned(self):
        # A reaped leader is insufficient when signal-zero still observes its
        # exact owned group. Only that observation is simulated; this child and
        # all actual termination/wait operations remain real.
        adapter, handle = self.spawn(HeadlessProfileAdapter,
            "import time\ntime.sleep(30)\n")
        real_killpg = os.killpg

        def group_observation(group, signum):
            if group == handle.process.pid and signum == 0:
                return None
            return real_killpg(group, signum)

        with mock.patch("floati.adapters.headless_template.os.killpg",
                side_effect=group_observation):
            with self.assertRaises(WorkerAdapterFailure) as caught:
                adapter.cancel()
        self.assertEqual("process_timeout", caught.exception.code)
        self.assertIsNotNone(adapter._active_process,
            "an unresolved group must remain owned for cleanup recovery")

    def test_real_background_provider_group_is_absent_after_deadline(self):
        # The supervisor owns waitpid for its TERM-ignoring descendant. Keeping
        # that parent alive avoids an orphan whose reaping we cannot prove.
        child = ("import signal,time\nfrom pathlib import Path\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            "Path('.floati/descendant-ready').touch()\ntime.sleep(180)\n")
        adapter, handle = self.spawn(HeadlessProfileAdapter,
            "import subprocess,time,os,signal\nfrom pathlib import Path\n"
            "child = subprocess.Popen(['/usr/bin/python3', '-B', '-c', " + repr(child) + "], "
            "stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
            "def stop(signum, frame):\n"
            "    signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            "    child.kill()\n    child.wait(timeout=3)\n"
            "    Path('.floati/descendant-reaped').touch()\n    raise SystemExit(0)\n"
            "signal.signal(signal.SIGTERM, stop)\n"
            "while not Path('.floati/descendant-ready').exists():\n    time.sleep(0.01)\n"
            "Path('.floati/supervisor-ready').touch()\nos.write(1, b'{}\\n')\n"
            "while True:\n    time.sleep(1)\n")
        ready = handle.filesystem_workspace / ".floati/supervisor-ready"
        wait_until(ready.exists, "supervisor child ownership")
        with self.assertRaises(WorkerAdapterFailure) as caught:
            adapter.drive(handle, deadline_seconds=0.25)
        self.assertEqual("deadline_exceeded", caught.exception.code)
        self.assertIsNotNone(handle.process.returncode)
        self.assertIsNone(adapter._active_process)
        self.assertTrue((handle.filesystem_workspace / ".floati/descendant-reaped").is_file())
        with self.assertRaises(ProcessLookupError):
            os.killpg(handle.process.pid, 0)


if __name__ == "__main__":
    unittest.main()

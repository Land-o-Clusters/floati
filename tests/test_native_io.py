"""Real-pipe component controls; no borrowed-provider success is implied."""
from contextlib import contextmanager
import hashlib
import importlib
import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from floati.worker_bootstrap_protocol import NATIVE_THREAD_BOUNDS
from floati.worker_errors import WorkerAdapterFailure
from tests.temp_roots import REAL_TEMP_ROOT
from tests.readiness import Readiness, wait_process, wait_until

HARNESS = Path(__file__).parent / "fixtures/adhoc-native/stream_harness.py"
# These reviewed fixture branches never create another process. Unknown modes
# retain group cleanup; inherited-pipe deliberately owns a surviving descendant.
NO_DESCENDANT_MODES = frozenset({
    "silent", "stdout-over", "stderr-over", "simultaneous", "frame-over",
    "depth-over", "depth-exact", "frame-exact", "invalid-utf8", "duplicate-key",
    "non-finite", "partial-eof", "records-over", "records-exact", "ignored-over",
    "terminal-stderr-over", "terminal-malformed", "two-terminal", "digest",
    "terminal-large",
})


def required_api(case, module, name):
    case.assertIsNotNone(importlib.util.find_spec(module),
                         "API ABSENT, not behavioral RED: " + module)
    loaded = importlib.import_module(module)
    case.assertTrue(hasattr(loaded, name), "API ABSENT: " + module + "." + name)
    return getattr(loaded, name)


def _pid_absent(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    return False


@contextmanager
def owned_child(case, mode, *args, duration=None):
    # Explicit durations exercise the product's original deadline. Ordinary
    # byte/framing controls only need a generous fixture liveness watchdog.
    readiness = Readiness("native I/O fixture " + mode)
    deadline = time.monotonic() + (readiness.bound if duration is None else duration)
    process = subprocess.Popen([sys.executable, "-I", "-B", str(HARNESS), mode,
                                *map(str, args), "--fixture-deadline", str(deadline)],
                               stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               start_new_session=True)
    try:
        process.fixture_deadline = deadline
        yield process
    except WorkerAdapterFailure as error:
        if duration is None and error.code == "process_timeout":
            raise readiness.failure() from error
        raise
    finally:
        try:
            if mode in NO_DESCENDANT_MODES:
                # Popen retains ownership of its unreaped PID across an exit
                # race; a collected single-process fixture needs no signal.
                if process.poll() is None:
                    process.terminate()
                    try:
                        wait_process(process, "terminated native fixture exits")
                    except AssertionError:
                        process.kill()
            else:
                # Unknown modes may share the deliberate inherited-pipe
                # fixture's surviving descendants and retain group cleanup.
                for sig in (signal.SIGTERM, signal.SIGKILL):
                    try:
                        os.killpg(process.pid, sig)
                    except ProcessLookupError:
                        break
                    except PermissionError:
                        # A post-TERM group transition can refuse escalation.
                        # Only this known fixture has an independent descendant
                        # witness below; unknown modes must still fail closed.
                        if mode != "inherited-pipe" or sig != signal.SIGKILL:
                            raise
                    try:
                        wait_process(process, "native group fixture leader exits")
                    except AssertionError:
                        continue
            wait_process(process, "collect the native I/O fixture leader")
            if mode == "inherited-pipe":
                # Prove cleanup here even if the test body raised, rather than
                # relying on an assertion after the context manager returns.
                descendant = int(Path(args[0]).read_text())
                wait_until(lambda: _pid_absent(descendant), "owned inherited-pipe descendant exits")
        finally:
            process.stdout.close()
            process.stderr.close()
        case.assertIsNotNone(process.returncode)


class _NativeIOFixture:
    """Shared helpers only; concrete classes own disjoint test methods."""

    def reader(self, process):
        api = required_api(self, "floati.adapters.native_io", "BoundedNativeIO")
        return api(process, deadline=process.fixture_deadline,
                   bounds=dict(NATIVE_THREAD_BOUNDS))

    def refuses(self, mode, expected, *, terminal=False):
        # Resolve first: a missing API never leaves a fixture child running.
        required_api(self, "floati.adapters.native_io", "BoundedNativeIO")
        with owned_child(self, mode) as process:
            reader = self.reader(process)
            with self.assertRaises(WorkerAdapterFailure) as caught:
                if terminal:
                    reader.read_terminal_json()
                else:
                    while reader.read_record() is not None:
                        pass
                reader.drain_to_exit()
            self.assertEqual(expected, caught.exception.code)



class NativeIOTests(_NativeIOFixture, unittest.TestCase):
    """Short deadlines and descendant-drain controls remain exclusive."""

    def test_bounds_and_deadline_are_validated_and_detached(self):
        api = required_api(self, "floati.adapters.native_io", "BoundedNativeIO")
        changed = [
            {**NATIVE_THREAD_BOUNDS, "stdout_bytes": 2_097_152},
            {**NATIVE_THREAD_BOUNDS, "json_depth": True},
            {**NATIVE_THREAD_BOUNDS, "unreviewed": 1},
            {key: value for key, value in NATIVE_THREAD_BOUNDS.items() if key != "stderr_bytes"},
        ]
        with owned_child(self, "silent") as process:
            for bounds in changed:
                with self.subTest(bounds=bounds), self.assertRaises(WorkerAdapterFailure) as caught:
                    api(process, deadline=process.fixture_deadline, bounds=bounds)
                self.assertEqual("protocol_error", caught.exception.code)
            for deadline, code in ((float("nan"), "protocol_error"),
                                   (float("inf"), "protocol_error"),
                                   (True, "protocol_error"),
                                   (time.monotonic() - 1, "process_timeout")):
                with self.subTest(deadline=deadline), self.assertRaises(WorkerAdapterFailure) as caught:
                    api(process, deadline=deadline, bounds=dict(NATIVE_THREAD_BOUNDS))
                self.assertEqual(code, caught.exception.code)
        with owned_child(self, "digest") as process:
            bounds = dict(NATIVE_THREAD_BOUNDS)
            reader = api(process, deadline=process.fixture_deadline, bounds=bounds)
            bounds["stdout_bytes"] = 1
            self.assertEqual({"ok": True}, reader.read_record())
            self.assertEqual(0, reader.drain_to_exit()["exit_code"])
        actual_popen = subprocess.Popen
        def delayed_spawn(*args, **kwargs):
            time.sleep(0.15)  # Before the actual child exists; no lost cleanup handle.
            return actual_popen(*args, **kwargs)
        with mock.patch.object(subprocess, "Popen", delayed_spawn):
            with owned_child(self, "silent", duration=0.05) as process:
                with self.assertRaises(WorkerAdapterFailure) as caught:
                    api(process, deadline=process.fixture_deadline, bounds=dict(NATIVE_THREAD_BOUNDS))
                self.assertEqual("process_timeout", caught.exception.code)

    def test_silent_child_exits_by_deadline(self):
        required_api(self, "floati.adapters.native_io", "BoundedNativeIO")
        with owned_child(self, "silent", duration=0.15) as process:
            started = time.monotonic()
            reader = self.reader(process)
            with self.assertRaises(WorkerAdapterFailure) as caught:
                reader.read_record()
            self.assertEqual("process_timeout", caught.exception.code)
            self.assertLess(time.monotonic() - started, 1.5)
        # Reader reports the deadline; the real process owner terminates/reaps.
        self.assertIsNotNone(process.returncode)
        with self.assertRaises(ProcessLookupError):
            os.kill(process.pid, 0)

    def test_inherited_pipe_drain_uses_original_deadline(self):
        required_api(self, "floati.adapters.native_io", "BoundedNativeIO")
        with tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT) as directory:
            pid_file = Path(directory) / "descendant.pid"
            with owned_child(self, "inherited-pipe", pid_file, duration=3) as process:
                reader = self.reader(process)
                self.assertEqual({"terminal": True}, reader.read_record())
                wait_process(process, "inherited-pipe leader exits after its terminal frame")
                # Give both interpreters time to start, then consume most of
                # the same original budget before checking the inherited pipe.
                # A fresh budget in drain_to_exit would miss this deadline.
                remaining = process.fixture_deadline - time.monotonic()
                self.assertGreater(remaining, 0.3, "fixture setup exhausted its original budget")
                time.sleep(max(0, remaining - 0.2))
                with self.assertRaises(WorkerAdapterFailure) as caught:
                    reader.drain_to_exit()
                self.assertEqual("process_timeout", caught.exception.code)
                self.assertLess(time.monotonic(), process.fixture_deadline + 0.3)
            descendant = int(pid_file.read_text())
            wait_until(lambda: _pid_absent(descendant), "inherited-pipe descendant is absent after cleanup")



class OrdinaryNativeIOTests(_NativeIOFixture, unittest.TestCase):
    """Ordinary framing cases use the generous owned-child watchdog."""

    def test_collected_single_process_fixture_is_not_signalled_again(self):
        # The digest fixture cannot spawn descendants. A real wait supplies the
        # ownership proof; signalling its now-collected group is unnecessary.
        with mock.patch.object(os, "killpg", side_effect=AssertionError(
                "collected single-process fixture was signalled")):
            with owned_child(self, "digest") as process:
                wait_process(process, "native digest fixture exits")

    def test_live_single_process_fixture_uses_owned_process_signal(self):
        process = None
        try:
            with mock.patch.object(os, "killpg", side_effect=AssertionError(
                    "live single-process fixture used group signalling")):
                with owned_child(self, "silent") as process:
                    self.assertIsNone(process.poll())
        finally:
            # The RED helper raises before its cleanup. Retain the actual owned
            # process handle and clean it here even when that assertion fails.
            if process is not None:
                try:
                    if process.poll() is None:
                        process.terminate()
                    try:
                        wait_process(process, "collect terminated native signal control")
                    except AssertionError:
                        process.kill()
                        wait_process(process, "collect killed native signal control")
                finally:
                    process.stdout.close()
                    process.stderr.close()

    def test_aggregate_stream_byte_bounds(self):
        for mode in ("stderr-over", "ignored-over"):
            with self.subTest(mode=mode):
                self.refuses(mode, "native_output_limit")

    def test_frame_depth_and_record_count_bounds(self):
        for mode in ("frame-over", "stdout-over", "depth-over", "records-over"):
            with self.subTest(mode=mode):
                self.refuses(mode, "native_protocol_limit")

    def test_malformed_records_are_not_ignored(self):
        for mode in ("invalid-utf8", "duplicate-key", "non-finite", "partial-eof"):
            with self.subTest(mode=mode):
                self.refuses(mode, "protocol_error")

    def test_exact_frame_depth_and_record_limits_are_accepted(self):
        required_api(self, "floati.adapters.native_io", "BoundedNativeIO")
        for mode, count in (("frame-exact", 1), ("depth-exact", 1), ("records-exact", 4096)):
            with self.subTest(mode=mode), owned_child(self, mode) as process:
                reader = self.reader(process)
                observed = 0
                while reader.read_record() is not None:
                    observed += 1
                self.assertEqual(count, observed)
                self.assertEqual(count, reader.drain_to_exit()["protocol_records"])

    def test_ignored_notifications_count_toward_total_limit(self):
        self.refuses("ignored-over", "native_output_limit")

    def test_success_then_stderr_flood_is_not_success(self):
        required_api(self, "floati.adapters.native_io", "BoundedNativeIO")
        with owned_child(self, "terminal-stderr-over") as process:
            reader = self.reader(process)
            with self.assertRaises(WorkerAdapterFailure) as caught:
                # A bounded pump may detect stderr overflow before returning stdout.
                self.assertEqual({"terminal": True}, reader.read_record())
                reader.drain_to_exit()
            self.assertEqual("native_output_limit", caught.exception.code)

    def test_concurrent_streams_at_exact_limits_do_not_deadlock(self):
        required_api(self, "floati.adapters.native_io", "BoundedNativeIO")
        with owned_child(self, "simultaneous") as process:
            reader = self.reader(process)
            self.assertEqual({}, json.loads(reader.read_terminal_json()))
            evidence = reader.drain_to_exit()
        self.assertEqual(1_048_576, evidence["stdout_bytes"])
        self.assertEqual(1_048_576, evidence["stderr_bytes"])
        self.assertEqual(2_097_152, evidence["combined_bytes"])

    def test_post_terminal_malformed_output_is_not_discarded(self):
        required_api(self, "floati.adapters.native_io", "BoundedNativeIO")
        with owned_child(self, "terminal-malformed") as process:
            reader = self.reader(process)
            with self.assertRaises(WorkerAdapterFailure) as caught:
                self.assertEqual({"terminal": True}, reader.read_record())
                reader.drain_to_exit()
            self.assertEqual("protocol_error", caught.exception.code)

    def test_drain_delivers_every_remaining_record_to_owner_validator(self):
        required_api(self, "floati.adapters.native_io", "BoundedNativeIO")
        with owned_child(self, "two-terminal") as process:
            reader = self.reader(process)
            self.assertEqual({"terminal": True}, reader.read_record())
            remaining = []
            evidence = reader.drain_to_exit(on_record=remaining.append)
        self.assertEqual([{"terminal": True}], remaining)
        self.assertEqual(2, evidence["protocol_records"])

    def test_failure_snapshot_retains_observed_counts_without_transcript(self):
        required_api(self, "floati.adapters.native_io", "BoundedNativeIO")
        with owned_child(self, "stderr-over") as process:
            reader = self.reader(process)
            with self.assertRaises(WorkerAdapterFailure) as caught:
                reader.drain_to_exit()
            self.assertEqual("native_output_limit", caught.exception.code)
            evidence = reader.snapshot()
            self.assertGreater(evidence["stderr_bytes"], NATIVE_THREAD_BOUNDS["stderr_bytes"])
            self.assertEqual({"stdout_bytes", "stderr_bytes", "combined_bytes",
                              "protocol_records", "stdout_sha256", "stderr_sha256",
                              "exit_code"}, set(evidence))

    def test_claude_terminal_json_uses_stdout_bound_not_jsonl_bound(self):
        required_api(self, "floati.adapters.native_io", "BoundedNativeIO")
        with owned_child(self, "terminal-large") as process:
            reader = self.reader(process)
            self.assertEqual(70_000, len(json.loads(reader.read_terminal_json())["value"]))
            reader.drain_to_exit()

    def test_single_document_validation_and_mode_mixing_remain_strict(self):
        for mode in ("duplicate-key", "non-finite", "invalid-utf8", "two-terminal"):
            with self.subTest(document=mode):
                self.refuses(mode, "protocol_error", terminal=True)
        required_api(self, "floati.adapters.native_io", "BoundedNativeIO")
        for first in ("jsonl", "terminal"):
            with self.subTest(first=first), owned_child(self, "digest") as process:
                reader = self.reader(process)
                if first == "jsonl":
                    reader.read_record()
                    other = reader.read_terminal_json
                else:
                    reader.read_terminal_json()
                    other = reader.read_record
                with self.assertRaises(WorkerAdapterFailure) as caught:
                    other()
                self.assertEqual("protocol_error", caught.exception.code)

    def test_drain_propagates_owner_validator_refusal(self):
        required_api(self, "floati.adapters.native_io", "BoundedNativeIO")
        with owned_child(self, "two-terminal") as process:
            reader = self.reader(process)
            self.assertEqual({"terminal": True}, reader.read_record())
            observed = []
            def reject(record):
                observed.append(record)
                raise WorkerAdapterFailure("protocol_error")
            with self.assertRaises(WorkerAdapterFailure) as caught:
                reader.drain_to_exit(on_record=reject)
            self.assertEqual("protocol_error", caught.exception.code)
            self.assertEqual([{"terminal": True}], observed)

    def test_counters_and_digests_cover_exact_bytes_without_transcripts(self):
        required_api(self, "floati.adapters.native_io", "BoundedNativeIO")
        with owned_child(self, "digest") as process:
            reader = self.reader(process)
            self.assertEqual({"ok": True}, reader.read_record())
            evidence = reader.drain_to_exit()
        stdout, stderr = b'{"ok":true}\n', b"diagnostic bytes only\n"
        self.assertEqual({"stdout_bytes", "stderr_bytes", "combined_bytes",
                          "protocol_records", "stdout_sha256", "stderr_sha256",
                          "exit_code"}, set(evidence))
        self.assertEqual(len(stdout), evidence["stdout_bytes"])
        self.assertEqual(len(stderr), evidence["stderr_bytes"])
        self.assertEqual(len(stdout) + len(stderr), evidence["combined_bytes"])
        self.assertEqual(1, evidence["protocol_records"])
        self.assertEqual(hashlib.sha256(stdout).hexdigest(), evidence["stdout_sha256"])
        self.assertEqual(hashlib.sha256(stderr).hexdigest(), evidence["stderr_sha256"])
        self.assertEqual(0, evidence["exit_code"])

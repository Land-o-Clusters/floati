"""Native controls for test-only bus-scale I/O coverage, before measurement."""
from __future__ import annotations

import builtins
import fcntl
import io
import math
import mmap
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest

from floati.errors import IntegrityFailure
from floati.jsonl import read_records
from floati.registry import REGISTRY_KINDS, Registry
from floati.root import FloatiRoot
from tests.temp_roots import REAL_TEMP_ROOT

try:
    from tests.bus_scale_io import IOProbe
except ModuleNotFoundError as error:
    if error.name != "tests.bus_scale_io":
        raise
    IOProbe = None


class BusScaleIOTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(
            IOProbe,
            "tests.bus_scale_io.IOProbe must measure native reads and locks before scale claims",
        )
        temporary = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.path = self.root / "events.jsonl"
        self.path.write_bytes(b"ab\ncdef\nz")
        self.lock = self.root / "events.jsonl.lock"
        self.lock.touch()

    def assert_bytes(self, probe, expected):
        self.assertEqual(expected, dict(probe.read_bytes_by_path))
        probe.assert_complete()

    def assert_hold(self, row, path, mode, lower, upper):
        self.assertEqual(path, row["path"])
        self.assertEqual(mode, row["mode"])
        self.assertTrue(math.isfinite(row["seconds"]))
        self.assertGreaterEqual(row["seconds"], lower)
        self.assertLessEqual(row["seconds"], upper)

    def test_path_read_bytes_counts_returned_bytes_once(self):
        with IOProbe(self.root) as probe:
            self.assertEqual(b"ab\ncdef\nz", self.path.read_bytes())
        self.assert_bytes(probe, {"events.jsonl": 9})

    def test_unbuffered_readall_counts_returned_bytes_once(self):
        with IOProbe(self.root) as probe:
            with builtins.open(self.path, "rb", buffering=0) as stream:
                self.assertEqual(b"ab\ncdef\nz", stream.readall())
                self.assertEqual(b"", stream.readall())
        self.assert_bytes(probe, {"events.jsonl": 9})

    def test_direct_fileio_cannot_bypass_returned_byte_coverage(self):
        with IOProbe(self.root) as probe:
            with self.assertRaisesRegex(RuntimeError, "unsupported"):
                with io.FileIO(self.path, "r") as stream:
                    stream.read()
        with self.assertRaisesRegex(RuntimeError, "unsupported"):
            probe.assert_complete()

    def test_positional_and_vector_reads_cannot_bypass_byte_coverage(self):
        for name in ("pread", "readv"):
            if not hasattr(os, name):
                continue
            with self.subTest(api=name):
                with IOProbe(self.root) as probe:
                    fd = os.open(self.path, os.O_RDONLY)
                    try:
                        with self.assertRaisesRegex(RuntimeError, "unsupported"):
                            if name == "pread":
                                os.pread(fd, 2, 0)
                            else:
                                os.readv(fd, [bytearray(2)])
                    finally:
                        os.close(fd)
                with self.assertRaisesRegex(RuntimeError, "unsupported"):
                    probe.assert_complete()

    def test_descriptor_aliases_cannot_shorten_native_flock_lifetime(self):
        operations = ["dup", "dup2", "F_DUPFD"]
        if hasattr(fcntl, "F_DUPFD_CLOEXEC"):
            operations.append("F_DUPFD_CLOEXEC")
        for operation in operations:
            with self.subTest(api=operation):
                native_close = os.close
                with IOProbe(self.root) as probe:
                    fd = os.open(self.lock, os.O_RDWR)
                    target = os.open(self.path, os.O_RDONLY)
                    alias = None
                    try:
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        with self.assertRaisesRegex(RuntimeError, "unsupported"):
                            if operation == "dup":
                                alias = os.dup(fd)
                            elif operation == "dup2":
                                os.dup2(fd, target)
                            else:
                                alias = fcntl.fcntl(fd, getattr(fcntl, operation), 0)
                    finally:
                        # Collect every native descriptor even against a broken
                        # probe; alias refusal must precede the kernel call.
                        if alias is not None:
                            native_close(alias)
                        # dup2 may invalidate the probe's original descriptor
                        # identity. Native cleanup must still collect both.
                        try:
                            os.close(target)
                        except RuntimeError:
                            native_close(target)
                        finally:
                            os.close(fd)
                with self.assertRaisesRegex(RuntimeError, "unsupported"):
                    probe.assert_complete()

    def test_builtin_open_partial_read_and_eof_count_once(self):
        with IOProbe(self.root) as probe:
            with builtins.open(self.path, "rb") as stream:
                self.assertEqual(b"ab", stream.read(2))
                self.assertEqual(b"\ncdef\nz", stream.read())
                self.assertEqual(b"", stream.read())
        self.assert_bytes(probe, {"events.jsonl": 9})

    def test_fdopen_counts_reads_without_stealing_closefd_false_descriptor(self):
        with IOProbe(self.root) as probe:
            descriptor = os.open(self.path, os.O_RDONLY)
            try:
                with os.fdopen(descriptor, "rb", closefd=False) as stream:
                    self.assertEqual(b"ab\ncdef\nz", stream.read())
                self.assertEqual(b"", os.read(descriptor, 1))
                os.fstat(descriptor)
            finally:
                os.close(descriptor)
        self.assert_bytes(probe, {"events.jsonl": 9})

    def test_os_read_counts_partial_eof_and_reread_bytes(self):
        with IOProbe(self.root) as probe:
            descriptor = os.open(self.path, os.O_RDONLY)
            try:
                self.assertEqual(b"ab\nc", os.read(descriptor, 4))
                self.assertEqual(b"def", os.read(descriptor, 3))
                self.assertEqual(b"\nz", os.read(descriptor, 8))
                self.assertEqual(b"", os.read(descriptor, 8))
                os.lseek(descriptor, 0, os.SEEK_SET)
                self.assertEqual(b"ab", os.read(descriptor, 2))
            finally:
                os.close(descriptor)
        self.assert_bytes(probe, {"events.jsonl": 11})

    def test_iteration_readline_and_readlines_count_consumed_bytes_once(self):
        with IOProbe(self.root) as probe:
            with self.path.open("rb") as stream:
                self.assertEqual([b"ab\n", b"cdef\n", b"z"], list(stream))
                stream.seek(0)
                self.assertEqual(b"ab\n", stream.readline())
                self.assertEqual([b"cdef\n", b"z"], stream.readlines())
                self.assertEqual([], stream.readlines())
        self.assert_bytes(probe, {"events.jsonl": 18})

    def test_readinto_counts_returned_length_not_buffer_capacity(self):
        buffer = bytearray(4)
        with IOProbe(self.root) as probe:
            with self.path.open("rb") as stream:
                self.assertEqual(4, stream.readinto(buffer))
                self.assertEqual(b"ab\nc", bytes(buffer))
                self.assertEqual(4, stream.readinto(buffer))
                self.assertEqual(b"def\n", bytes(buffer))
                self.assertEqual(1, stream.readinto(buffer))
                self.assertEqual(ord("z"), buffer[0])
                self.assertEqual(0, stream.readinto(buffer))
        self.assert_bytes(probe, {"events.jsonl": 9})

    def test_text_reads_count_utf8_bytes_not_character_count(self):
        self.path.write_bytes(b"\xc3\xa9\n")
        with IOProbe(self.root) as probe:
            with self.path.open("r", encoding="utf-8", newline="") as stream:
                self.assertEqual("\u00e9", stream.read(1))
                self.assertEqual("\n", stream.read())
        self.assert_bytes(probe, {"events.jsonl": 3})

    def test_failed_open_does_not_invent_read_bytes(self):
        with IOProbe(self.root) as probe:
            with self.assertRaises(FileNotFoundError):
                (self.root / "missing.jsonl").read_bytes()
        self.assert_bytes(probe, {})

    def test_failed_nonblocking_flock_does_not_invent_a_hold(self):
        blocker = os.open(self.lock, os.O_RDWR)
        try:
            fcntl.flock(blocker, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with IOProbe(self.root) as probe:
                contender = os.open(self.lock, os.O_RDWR)
                try:
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
                finally:
                    os.close(contender)
            self.assertEqual([], probe.holds)
            probe.assert_complete()
        finally:
            fcntl.flock(blocker, fcntl.LOCK_UN)
            os.close(blocker)

    def test_shared_flock_interval_is_bounded_by_native_unlock(self):
        with IOProbe(self.root) as probe:
            with self.lock.open("a+b") as stream:
                before = time.monotonic()
                fcntl.flock(stream.fileno(), fcntl.LOCK_SH | fcntl.LOCK_NB)
                acquired = time.monotonic()
                self.lock.stat()
                releasing = time.monotonic()
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
                after = time.monotonic()
        self.assertEqual(1, len(probe.holds))
        self.assert_hold(probe.holds[0], "events.jsonl.lock", "shared",
                         releasing - acquired, after - before)
        probe.assert_complete()

    def test_exclusive_flock_interval_is_bounded_by_native_unlock(self):
        with IOProbe(self.root) as probe:
            descriptor = os.open(self.lock, os.O_RDWR)
            try:
                before = time.monotonic()
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = time.monotonic()
                self.lock.stat()
                releasing = time.monotonic()
                fcntl.flock(descriptor, fcntl.LOCK_UN)
                after = time.monotonic()
            finally:
                os.close(descriptor)
        self.assertEqual(1, len(probe.holds))
        self.assert_hold(probe.holds[0], "events.jsonl.lock", "exclusive",
                         releasing - acquired, after - before)
        probe.assert_complete()

    def test_os_close_finishes_successful_hold_without_explicit_unlock(self):
        with IOProbe(self.root) as probe:
            descriptor = os.open(self.lock, os.O_RDWR)
            try:
                before = time.monotonic()
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = time.monotonic()
                self.lock.stat()
                releasing = time.monotonic()
            finally:
                os.close(descriptor)
            after = time.monotonic()
        self.assertEqual(1, len(probe.holds))
        self.assert_hold(probe.holds[0], "events.jsonl.lock", "exclusive",
                         releasing - acquired, after - before)
        probe.assert_complete()

    def test_file_close_finishes_successful_hold_without_explicit_unlock(self):
        with IOProbe(self.root) as probe:
            with builtins.open(self.lock, "a+b") as stream:
                before = time.monotonic()
                fcntl.flock(stream.fileno(), fcntl.LOCK_SH | fcntl.LOCK_NB)
                acquired = time.monotonic()
                self.lock.stat()
                releasing = time.monotonic()
            after = time.monotonic()
        self.assertEqual(1, len(probe.holds))
        self.assert_hold(probe.holds[0], "events.jsonl.lock", "shared",
                         releasing - acquired, after - before)
        probe.assert_complete()

    def test_reused_descriptor_does_not_retain_previous_read_path(self):
        second = self.root / "other.jsonl"
        second.write_bytes(b"12345")
        with IOProbe(self.root) as probe:
            first_descriptor = os.open(self.path, os.O_RDONLY)
            try:
                self.assertEqual(b"ab", os.read(first_descriptor, 2))
            finally:
                os.close(first_descriptor)
            second_descriptor = os.open(second, os.O_RDONLY)
            try:
                self.assertEqual(first_descriptor, second_descriptor,
                                 "the native control must actually reuse the closed descriptor")
                self.assertEqual(b"123", os.read(second_descriptor, 3))
            finally:
                os.close(second_descriptor)
        self.assert_bytes(probe, {"events.jsonl": 2, "other.jsonl": 3})

    def test_mmap_cannot_silently_certify_complete_read_coverage(self):
        with self.assertRaisesRegex(RuntimeError, "(?i)(mmap|unsupported|coverage)"):
            with IOProbe(self.root) as probe:
                with self.path.open("rb") as stream:
                    with mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_READ) as mapping:
                        self.assertEqual(b"ab", mapping[:2])
            probe.assert_complete()

    def test_spawn_cannot_silently_certify_complete_process_coverage(self):
        child = None
        with self.assertRaisesRegex(RuntimeError, "(?i)(spawn|subprocess|unsupported|coverage)"):
            try:
                with IOProbe(self.root) as probe:
                    child = subprocess.Popen(
                        [sys.executable, "-I", "-S", "-c", "pass"],
                        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL, start_new_session=True,
                    )
                    self.assertEqual(0, child.wait(timeout=15))
                probe.assert_complete()
            finally:
                if child is not None and child.poll() is None:
                    child.send_signal(signal.SIGINT)
                    child.wait(timeout=15)

    def test_native_middle_frame_corruption_still_refuses_with_probe_active(self):
        authority = FloatiRoot.open(self.root / "native", "alpha")
        registry = Registry(authority)
        for node in ("node-one", "node-two", "node-three"):
            registry.register(node, "worker")
        path = registry.path
        intact = path.read_bytes()
        frames = intact.splitlines(keepends=True)
        self.assertEqual(3, len(frames))
        self.assertEqual(3, len(read_records(authority, registry.relative_path,
                                          allowed_kinds=REGISTRY_KINDS)))
        original_stat = path.stat()
        frames[1] = b"!" + frames[1][1:]
        damaged = b"".join(frames)
        self.assertEqual(len(intact), len(damaged))
        path.write_bytes(damaged)
        os.utime(path, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
        self.assertEqual(original_stat.st_ino, path.stat().st_ino)
        with IOProbe(authority.tenant_home) as probe:
            with self.assertRaises(IntegrityFailure) as refusal:
                read_records(authority, registry.relative_path, allowed_kinds=REGISTRY_KINDS)
        self.assertEqual("malformed_json", refusal.exception.code)
        self.assertEqual({"registry/entries.jsonl": len(damaged)},
                         dict(probe.read_bytes_by_path))
        self.assertEqual(1, len(probe.holds))
        self.assertEqual("registry/entries.jsonl.lock", probe.holds[0]["path"])
        self.assertEqual("shared", probe.holds[0]["mode"])
        probe.assert_complete()


if __name__ == "__main__":
    unittest.main()

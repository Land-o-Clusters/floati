"""Transient observer probes cannot drive a Board projection/reload loop.

The parent owns execution. Native watch controls use only private temporary
files; the event-loop controls replace terminal setup and pipe input only.
"""
import io
import os
from pathlib import Path
import select
import tempfile
import unittest
from unittest import mock
import uuid

import tests
from floati.tui import BoardFilesystemWakeup, run_board
from floati.tui_render import HarborBoardModel
from tests.temp_roots import REAL_TEMP_ROOT
from tests.test_tui_render import RECEIPTS, SNAPSHOT, WORK
from tests.readiness import wait_readable


class HostProbeBoardNativeWatchTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.observer = self.root / "state" / "host-observations" / "probe-seat"
        self.observer.mkdir(parents=True)
        self.latest = self.observer / "latest.json"

    def watch(self):
        source = BoardFilesystemWakeup(self.root)
        self.addCleanup(source.close)
        return source

    def create_probe(self, directory=None):
        probe = (self.observer if directory is None else directory) / (
            ".floati-host-probe-" + uuid.uuid4().hex)
        with probe.open("xb") as stream:
            stream.write(b"private writable-store probe\n")
            stream.flush()
            os.fsync(stream.fileno())
        return probe

    def drain_native(self, source):
        wait_readable(source.fileno(), "native backend did not report controlled filesystem activity")
        return source.drain()

    def test_native_watch_ignores_completed_probe_only_directory_activity(self):
        self.latest.write_bytes(b"original latest\n")
        source = self.watch()
        probe = self.create_probe()
        probe.unlink()
        self.assertFalse(self.drain_native(source),
            "writable-store probe alone would cause projection -> probe -> reload forever")
        self.assertEqual(b"original latest\n", self.latest.read_bytes())

    def test_native_watch_preserves_real_latest_add_replace_and_change_with_probe(self):
        for operation in ("add", "replace", "change"):
            with self.subTest(operation=operation):
                self.latest.unlink(missing_ok=True)
                if operation != "add":
                    self.latest.write_bytes(b"original latest\n")
                source = self.watch()
                try:
                    probe = self.create_probe()
                    if operation == "replace":
                        replacement = self.observer / "next-latest.json"
                        replacement.write_bytes(b"replacement latest\n")
                        replacement.replace(self.latest)
                    else:
                        self.latest.write_bytes(b"changed latest\n")
                    probe.unlink()
                    self.assertTrue(self.drain_native(source),
                        "probe filtering concealed a real latest.json " + operation)
                finally:
                    source.close()

    def test_same_probe_name_outside_host_observation_node_is_not_filtered(self):
        outside = self.root / "state" / "other-state" / "probe-seat"
        outside.mkdir(parents=True)
        source = self.watch()
        probe = self.create_probe(outside)
        probe.unlink()
        self.assertTrue(self.drain_native(source),
            "probe exception escaped its exact host-observations/<node> coordinate")

    def test_non_probe_entry_inside_observer_directory_remains_meaningful(self):
        source = self.watch()
        ordinary = self.observer / ".floati-host-probe-not-a-uuid"
        ordinary.write_bytes(b"ordinary state, not a private probe\n")
        self.assertTrue(self.drain_native(source),
            "prefix-only filtering concealed an ordinary observer directory entry")


class HostProbeBoardEventLoopTests(unittest.TestCase):
    def check_drain(self, drain_result, expected_loads):
        class TTY(io.StringIO):
            def __init__(self, descriptor):
                super().__init__()
                self.descriptor = descriptor

            def isatty(self):
                return True

            def fileno(self):
                return self.descriptor

        model = HarborBoardModel.from_projection(SNAPSHOT, WORK, RECEIPTS)
        key_reader, key_writer = os.pipe()
        source_reader, source_writer = os.pipe()
        for descriptor in (key_reader, key_writer, source_reader, source_writer):
            self.addCleanup(os.close, descriptor)
        os.write(key_writer, b"q")
        os.write(source_writer, b"probe-only-event")
        drains, loads = [], []

        class Wakeup:
            def fileno(self):
                return source_reader

            def drain(self):
                drains.append(os.read(source_reader, 4096))
                return drain_result

        def load_model():
            loads.append(True)
            return model

        stream = TTY(key_reader)
        with mock.patch.dict(os.environ, {"TERM": "xterm-256color"}, clear=True), mock.patch(
                "floati.tui.termios.tcgetattr", return_value=[]), mock.patch(
                "floati.tui.termios.tcsetattr"), mock.patch("floati.tui.tty.setcbreak"), mock.patch(
                "floati.tui.shutil.get_terminal_size", return_value=os.terminal_size((100, 40))):
            self.assertEqual(0, run_board(model_loader=load_model,
                model_wakeup=Wakeup(), input_stream=stream, output_stream=stream,
                terminal_response=b"\x1b[?1;2c"))
        self.assertEqual([b"probe-only-event"], drains)
        self.assertEqual(expected_loads, len(loads),
            "event loop ignored the native drain's meaningful-change verdict")

    def test_false_probe_only_drain_does_not_reload_board(self):
        for result, expected_loads in ((False, 1), (True, 2), (None, 2)):
            with self.subTest(result=result):
                self.check_drain(result, expected_loads)

"""Bounded ordinary-inode controls for controller result ingestion."""
import importlib
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from floati.errors import ProtocolRefusal
from tests.temp_roots import REAL_TEMP_ROOT


class AdhocResultReadTests(unittest.TestCase):
    def test_result_parent_preparation_stays_inside_the_borrowed_row(self):
        module = importlib.import_module("floati.adhoc_threads")
        with tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT) as scratch:
            base = Path(scratch)
            workspace = base / "row"
            workspace.mkdir()
            foreign = base / "foreign"
            foreign.mkdir()
            (workspace / "linked").symlink_to(foreign, target_is_directory=True)
            (workspace / "ordinary-file").write_bytes(b"retained")
            descriptor = os.open(workspace, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            self.addCleanup(os.close, descriptor)
            for relative in ("result.json", "docs/evidence/result.json", "docs/evidence/result.json"):
                with self.subTest(relative=relative):
                    module._prepare_result_parent(descriptor, relative)
                    self.assertTrue((workspace / relative).parent.is_dir())
                    self.assertFalse((workspace / relative).exists())
            for relative in ("linked/new/result.json", "ordinary-file/result.json", "../escaped/result.json"):
                with self.subTest(relative=relative):
                    with self.assertRaises(ProtocolRefusal) as caught:
                        module._prepare_result_parent(descriptor, relative)
                    self.assertEqual("adhoc_result_disposition_invalid", caught.exception.code)
            self.assertEqual([], list(foreign.iterdir()))
            self.assertEqual(b"retained", (workspace / "ordinary-file").read_bytes())

    def test_fifo_result_refuses_without_blocking(self):
        with tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT) as scratch:
            path = Path(scratch)
            os.mkfifo(path / "result.json", 0o600)
            module = importlib.import_module("floati.adhoc_threads")
            real_open = os.open
            opened = []

            def observe_open(name, flags, *args, **kwargs):
                if name == "result.json":
                    # Catch a blocking-open regression before entering the
                    # kernel: a FIFO with no writer would otherwise hang.
                    self.assertTrue(flags & os.O_NONBLOCK,
                                    "the FIFO must be opened without waiting for a writer")
                descriptor = real_open(name, flags, *args, **kwargs)
                if name == "result.json":
                    opened.append(descriptor)
                    self.assertFalse(os.get_blocking(descriptor))
                return descriptor

            with mock.patch.object(module.os, "open", observe_open):
                with self.assertRaises(ProtocolRefusal) as caught:
                    module._read_result(scratch, "result.json")
            self.assertEqual("adhoc_result_disposition_invalid", caught.exception.code)
            self.assertEqual(1, len(opened), "the actual native FIFO open must be observed")

    def test_hardlinked_result_cannot_borrow_foreign_bytes(self):
        with tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT) as scratch:
            path = Path(scratch)
            foreign = path / "foreign"
            workspace = path / "row"
            workspace.mkdir()
            foreign.write_bytes(b"foreign retained bytes")
            os.link(foreign, workspace / "result.json")
            module = importlib.import_module("floati.adhoc_threads")
            with self.assertRaises(ProtocolRefusal) as caught:
                module._read_result(workspace, "result.json")
            self.assertEqual("adhoc_result_disposition_invalid", caught.exception.code)
            self.assertEqual(b"foreign retained bytes", foreign.read_bytes())

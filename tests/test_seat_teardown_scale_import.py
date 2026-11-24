"""Scale discovery and child cleanup share the runner's portable enumeration."""

import importlib.util
import io
import os
from pathlib import Path
import signal
import stat
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from scripts.parallel_processes import ProcessQuery
from tests.temp_roots import REAL_TEMP_ROOT


def load_scale_module():
    source = Path(__file__).with_name('test_seat_teardown_scale.py')
    spec = importlib.util.spec_from_file_location('_scale_import_control', source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ScaleImportTests(unittest.TestCase):
    def test_measurement_refuses_unsafe_scratch_before_loading_native_fixture(self):
        """Missing mode or symlink validation must not admit a native run."""
        source = Path(__file__).resolve().parents[1] / 'scripts/measure-send-index-bank.py'
        spec = importlib.util.spec_from_file_location('_measurement_scratch_control', source)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        temporary = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
        self.addCleanup(temporary.cleanup)
        parent = Path(temporary.name)
        scratch = parent / 'scratch'
        scratch.mkdir(mode=0o700)
        identity = scratch.stat()
        native_stat, native_lstat, native_import = Path.stat, Path.lstat, __import__

        class NativeFixtureBoundary(Exception):
            pass

        def guarded_import(name, *args, **kwargs):
            if name == 'tests.test_seat_teardown_scale':
                raise NativeFixtureBoundary
            return native_import(name, *args, **kwargs)

        for fault in ('safe', 'group-writable', 'symlink', 'foreign-owner'):
            with self.subTest(fault=fault):
                followed = SimpleNamespace(st_mode=stat.S_IFDIR | (
                    0o770 if fault == 'group-writable' else 0o700),
                    st_uid=os.getuid() + (fault == 'foreign-owner'),
                    st_dev=identity.st_dev, st_ino=identity.st_ino)
                direct = (SimpleNamespace(st_mode=stat.S_IFLNK | 0o777, st_uid=os.getuid())
                    if fault == 'symlink' else followed)

                def metadata(path, *args, **kwargs):
                    return followed if path == scratch else native_stat(path, *args, **kwargs)

                def link_metadata(path, *args, **kwargs):
                    return direct if path == scratch else native_lstat(path, *args, **kwargs)

                with mock.patch.object(module, 'Path', side_effect=lambda value:
                        parent if value == '\x2fprivate/tmp' else Path(value)),\
                        mock.patch.object(Path, 'stat', autospec=True, side_effect=metadata),\
                        mock.patch.object(Path, 'lstat', autospec=True, side_effect=link_metadata),\
                        mock.patch.dict(os.environ, {name: str(scratch) for name in
                            ('HOME', 'TMPDIR', 'FLOATI_TEST_TEMP_ROOT')}),\
                        mock.patch('sys.argv', [str(source), '--records', '10000',
                            '--output', str(scratch / 'summary.json')]),\
                        mock.patch('sys.stderr', new_callable=io.StringIO),\
                        mock.patch('builtins.__import__', side_effect=guarded_import):
                    if fault == 'safe':
                        with self.assertRaises(NativeFixtureBoundary):
                            module.main()
                    else:
                        with self.assertRaises(SystemExit) as refused:
                            module.main()
                        self.assertEqual(2, refused.exception.code)

    def test_measurement_does_not_overwrite_a_link_target(self):
        source = Path(__file__).resolve().parents[1] / "scripts/measure-send-index-bank.py"
        spec = importlib.util.spec_from_file_location("_measurement_output_control", source)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        class EmptyScaleFixture(unittest.TestCase):
            def test_native_observer_final_hundred_thousand_includes_report_pair(self):
                pass

            def seed_history(self, records):
                pass

            def perform(self, *, native):
                pass

        with tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT) as temporary:
            root = Path(temporary)
            scratch = root / "measurement"
            scratch.mkdir(mode=0o700)
            sentinel = root / "existing"
            sentinel.write_text("original contents")
            output = scratch / "summary.json"
            output.symlink_to(sentinel)
            fixture = SimpleNamespace(SeatTeardownNativeScaleTests=EmptyScaleFixture)
            with mock.patch.object(module, "Path", side_effect=lambda value:
                    root if value == "\x2fprivate/tmp" else Path(value)),\
                    mock.patch.dict(os.environ, {name: str(scratch) for name in
                        ("HOME", "TMPDIR", "FLOATI_TEST_TEMP_ROOT")}),\
                    mock.patch.dict(sys.modules, {"tests.test_seat_teardown_scale": fixture}),\
                    mock.patch.object(sys.modules["tests"], "test_seat_teardown_scale",
                                      fixture, create=True),\
                    mock.patch("sys.argv", [str(source), "--records", "100000",
                        "--output", str(output)]),\
                    mock.patch("sys.stdout", new_callable=io.StringIO),\
                    mock.patch("sys.stderr", new_callable=io.StringIO):
                try:
                    module.main()
                except (FileExistsError, SystemExit):
                    pass

            self.assertEqual("original contents", sentinel.read_text())

    def test_reaper_enumerates_both_generations_then_kills_and_waits(self):
        tree = {410: [411, 412], 411: [421], 412: [422]}
        with mock.patch('sys.platform', 'linux'), mock.patch('ctypes.CDLL',
                side_effect=OSError('libproc is unavailable')) as load_library:
            module = load_scale_module()
            with mock.patch.object(ProcessQuery, 'children', autospec=True,
                    side_effect=lambda query, pid: tree[pid]) as children,\
                    mock.patch.object(module.os, 'getpid', return_value=410),\
                    mock.patch.object(module.os, 'kill') as kill,\
                    mock.patch.object(module.os, 'waitpid',
                        side_effect=lambda pid, options: (pid, 0)) as wait:
                module._reap_direct_children()
            load_library.assert_not_called()
            self.assertEqual([410, 411, 412],
                [call.args[1] for call in children.call_args_list])
            self.assertEqual([mock.call(pid, signal.SIGKILL)
                for pid in (411, 412, 421, 422)], kill.call_args_list)
            self.assertEqual([mock.call(pid, module.os.WNOHANG)
                for pid in (411, 412)], wait.call_args_list)


if __name__ == '__main__':
    unittest.main()

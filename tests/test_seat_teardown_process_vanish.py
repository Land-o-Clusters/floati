"""Process exit during native observation is absence, never unreadability."""
import ctypes
import errno
import io
import os
import subprocess
import struct
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from floati import codex_wait_liveness as liveness
from floati import seat_host_worker as host
from floati import seat_teardown_activity as activity
from floati import waiter_invocation as waiter
from floati.errors import ProtocolRefusal


def _stat(pid, parent=1, state='S'):
    return str(pid) + ' (fixture) ' + ' '.join([state, str(parent)] + ['0'] * 17 + ['100'] + ['0'] * 3)


class ProcessVanishTests(unittest.TestCase):
    def test_darwin_public_metadata_preserves_exit_and_unreadable_verdicts(self):
        # Both interfaces can genuinely lose a process. Permission errors
        # from both must retain the refusal and identify each attempted API.
        class Info(ctypes.Structure):
            _fields_ = [('pbi_pid', ctypes.c_uint32), ('pbi_ppid', ctypes.c_uint32),
                        ('pbi_status', ctypes.c_uint32), ('pbi_start_tvsec', ctypes.c_uint64),
                        ('pbi_start_tvusec', ctypes.c_uint64)]
        for error in (errno.ENOENT, errno.ESRCH, errno.EACCES, errno.EPERM,
                      'empty', 'fallback'):
            def failed(*args):
                if error == 'empty':
                    ctypes.cast(args[3], ctypes.POINTER(ctypes.c_size_t))[0] = 0
                    return 0
                ctypes.set_errno(errno.EPERM if error == 'fallback' else error)
                return -1
            def proc(*args):
                if error != 'fallback':
                    return failed(*args)
                info = ctypes.cast(args[3], ctypes.POINTER(Info)).contents
                info.pbi_pid, info.pbi_ppid, info.pbi_status = 42, 1, 2
                info.pbi_start_tvsec, info.pbi_start_tvusec = 100, 125_000
                return ctypes.sizeof(info)
            with self.subTest(error=error), patch.object(
                    liveness, '_LIBSYSTEM', SimpleNamespace(sysctl=failed), create=True), patch.object(
                    liveness, '_LIBPROC', SimpleNamespace(proc_pidinfo=proc), create=True), patch.object(
                    liveness, '_ProcBsdInfo', Info, create=True), patch.object(
                    liveness, '_PROC_PIDTBSDINFO', 3, create=True):
                if error in (errno.ENOENT, errno.ESRCH, 'empty'):
                    self.assertIsNone(liveness._darwin_process_info(42))
                elif error == 'fallback':
                    info = liveness._darwin_process_info(42)
                    self.assertEqual(100.125, info['process_start_epoch'])
                    self.assertEqual(1, info['parent_pid'])
                else:
                    with self.assertRaises(ProtocolRefusal) as caught:
                        liveness._darwin_process_info(42)
                    self.assertEqual('codex_wait_start_time_unmeasurable', caught.exception.code)
                    self.assertIn('sysctl(KERN_PROC_PID)', caught.exception.detail)
                    self.assertIn('proc_pidinfo(PROC_PIDTBSDINFO)', caught.exception.detail)

    def test_darwin_public_metadata_rejects_wrong_identity_and_invalid_time(self):
        for field, value in (('pid', 43), ('microseconds', 1_000_000),
                             ('seconds', 0), ('size', 647)):
            def native(mib, count, buffer, size, new, newsize):
                raw = bytearray(648)
                struct.pack_into('=qi', raw, 0, value if field == 'seconds' else 100,
                                 value if field == 'microseconds' else 123)
                raw[36] = 2
                struct.pack_into('=i', raw, 40, value if field == 'pid' else 42)
                struct.pack_into('=i', raw, 560, 1)
                ctypes.memmove(buffer, bytes(raw), len(raw))
                ctypes.cast(size, ctypes.POINTER(ctypes.c_size_t))[0] = value if field == 'size' else 648
                return 0
            with self.subTest(field=field), patch.object(
                    liveness, '_LIBSYSTEM', SimpleNamespace(sysctl=native), create=True):
                with self.assertRaises(ProtocolRefusal) as caught:
                    liveness._darwin_process_info(42)
                self.assertEqual('codex_wait_start_time_unmeasurable', caught.exception.code)

    def test_linux_start_time_vanishes_during_stat_read(self):
        for error in (errno.ENOENT, errno.ESRCH):
            with self.subTest(error=error), patch.object(
                    Path, 'read_text', side_effect=OSError(error, 'fixture exited')):
                self.assertIsNone(liveness._linux_start_epoch(42))

    def test_linux_start_time_permission_denied_is_typed_refusal(self):
        with patch.object(Path, 'read_text', side_effect=PermissionError(errno.EACCES, 'fixture denied')):
            with self.assertRaises(ProtocolRefusal) as caught:
                liveness._linux_start_epoch(42)
        self.assertEqual('codex_wait_start_time_unmeasurable', caught.exception.code)

    def _darwin_failure(self, error):
        class Info(ctypes.Structure):
            _fields_ = [('pbi_pid', ctypes.c_uint32)]
        def failed(*args):
            ctypes.set_errno(error)
            return 0
        return (patch.object(host.sys, 'platform', 'darwin'),
                patch.object(liveness, '_ProcBsdInfo', Info, create=True),
                patch.object(liveness, '_LIBPROC', SimpleNamespace(proc_pidinfo=failed, proc_pidpath=failed), create=True))

    def test_darwin_identity_and_executable_vanish_or_refuse_by_native_errno(self):
        for reader in (host._identity, host._executable):
            for error in (errno.ENOENT, errno.ESRCH, errno.EACCES):
                with self.subTest(reader=reader.__name__, error=error):
                    platform, info, library = self._darwin_failure(error)
                    with platform, info, library:
                        if error == errno.EACCES:
                            with self.assertRaises(ProtocolRefusal) as caught:
                                reader(42)
                            self.assertEqual('seat_teardown_activity_unknown', caught.exception.code)
                        else:
                            self.assertIsNone(reader(42))

    def test_linux_identity_disappears_at_either_proc_read(self):
        for target in ('stat', 'status'):
            for error in (errno.ENOENT, errno.ESRCH):
                with self.subTest(target=target, error=error):
                    def read(path, *args, **kwargs):
                        if path.name == target:
                            raise OSError(error, 'fixture process exited')
                        return _stat(42) if path.name == 'stat' else 'Uid:\t501\t501\t501\t501\n'
                    with patch.object(host.sys, 'platform', 'linux'), patch.object(Path, 'read_text', read):
                        self.assertIsNone(host._identity(42))

    def test_linux_identity_permission_denied_is_typed_unknown(self):
        with patch.object(host.sys, 'platform', 'linux'), patch.object(
                Path, 'read_text', side_effect=PermissionError(errno.EACCES, 'fixture denied')):
            with self.assertRaises(ProtocolRefusal) as caught:
                host._identity(42)
        self.assertEqual('seat_teardown_activity_unknown', caught.exception.code)

    def test_executable_vanishes_during_readlink(self):
        for error in (errno.ENOENT, errno.ESRCH):
            with self.subTest(error=error), patch.object(host.sys, 'platform', 'linux'), patch.object(
                    os, 'readlink', side_effect=OSError(error, 'fixture process exited')):
                self.assertIsNone(host._executable(42))

    def test_linux_executable_permission_denied_remains_unknown(self):
        with patch.object(host.sys, 'platform', 'linux'), patch.object(
                os, 'readlink', side_effect=PermissionError(errno.EACCES, 'fixture protected ancestor')):
            with self.assertRaises(ProtocolRefusal) as caught:
                host._executable(42)
        self.assertEqual('seat_teardown_activity_unknown', caught.exception.code)
        self.assertIn('PID 42', caught.exception.detail)

    def test_linux_terminal_process_is_gone_without_reading_its_executable(self):
        for state in ('Z', 'X'):
            with self.subTest(state=state), patch.object(host.sys, 'platform', 'linux'), patch.object(
                    Path, 'read_text', return_value=_stat(42, state=state)), patch.object(
                    host, '_executable') as executable:
                self.assertIsNone(host._process(42))
                executable.assert_not_called()

    def test_process_group_permission_denied_is_typed_unknown(self):
        with patch.object(host, '_identity', return_value={'pid': 42}), patch.object(
                os, 'getpgid', side_effect=PermissionError(errno.EACCES, 'fixture denied')):
            with self.assertRaises(ProtocolRefusal) as caught:
                host._process(42)
        self.assertEqual('seat_teardown_activity_unknown', caught.exception.code)

    def test_process_group_vanishes_after_identity(self):
        with patch.object(host, '_identity', return_value={'pid': 42}), patch.object(
                os, 'getpgid', side_effect=ProcessLookupError(errno.ESRCH, 'fixture exited')):
            self.assertIsNone(host._process(42))

    def test_waiter_parent_vanishes_without_reprobing_a_reused_pid(self):
        for error in (errno.ENOENT, errno.ESRCH):
            with self.subTest(error=error), patch.object(waiter.sys, 'platform', 'linux'), patch.object(
                    Path, 'read_text', side_effect=OSError(error, 'fixture process exited')), patch.object(
                    waiter, 'process_alive', return_value=True) as alive:
                self.assertIsNone(waiter._native_parent(42))
                alive.assert_not_called()

    def test_waiter_unreadable_parent_stays_typed_refusal(self):
        with patch.object(waiter.sys, 'platform', 'linux'), patch.object(
                Path, 'read_text', side_effect=PermissionError(errno.EACCES, 'fixture denied')):
            with self.assertRaises(ProtocolRefusal) as caught:
                waiter._native_parent(42)
        self.assertEqual('waiter_invocation_unproven', caught.exception.code)

    def test_waiter_exit_during_ancestry_read_is_not_alive(self):
        with patch.object(waiter.sys, 'platform', 'linux'), patch.object(
                waiter, 'process_start_epoch', return_value=100), patch.object(
                Path, 'read_text', side_effect=ProcessLookupError(errno.ESRCH, 'fixture exited')):
            self.assertFalse(waiter._same_process(42, 100))

    def test_waiter_missing_start_time_never_confuses_a_reused_pid_with_its_holder(self):
        with patch.object(waiter, 'process_start_epoch', return_value=None), patch.object(
                waiter, 'process_alive', return_value=True), patch.object(
                waiter, '_native_parent', return_value=1), patch.object(
                host, '_identity', return_value={'pid': 42, 'process_start_epoch': 101}):
            self.assertFalse(waiter._same_process(42, 100))

    def test_waiter_unmeasurable_birth_with_live_parent_remains_unproven(self):
        with patch.object(waiter, 'process_start_epoch', return_value=None), patch.object(
                host, '_identity', return_value=None), patch.object(waiter, '_native_parent', return_value=1):
            with self.assertRaises(ProtocolRefusal) as caught:
                waiter._same_process(42, 100)
        self.assertEqual('waiter_invocation_unproven', caught.exception.code)

    def _linux_snapshot(self, target, error, terminal=None):
        cwd_read = False
        class Entries:
            def __enter__(self):
                return iter(SimpleNamespace(name=str(pid)) for pid in (1, 41, 42))
            def __exit__(self, *args):
                pass
        def opened(path, *args, **kwargs):
            pid = int(path.parent.name)
            if pid == 42 and path.name == target:
                raise OSError(error, 'fixture process exited')
            state = terminal if pid == 42 and cwd_read and terminal else 'S'
            raw = 'Uid:\t501\t501\t501\t501\n' if path.name == 'status' else _stat(pid, 0 if pid == 1 else 1, state)
            return io.BytesIO(raw.encode())
        def cwd(path):
            nonlocal cwd_read
            if int(path.parent.name) == 42 and target == 'cwd':
                cwd_read = True
                raise OSError(error, 'fixture process exited')
            return '/'
        def read_text(path, *args, **kwargs):
            with opened(path) as handle:
                return handle.read().decode()
        with patch.object(activity.sys, 'platform', 'linux'), patch.object(
                os, 'scandir', return_value=Entries()), patch.object(Path, 'open', opened), patch.object(
                os, 'getpid', return_value=41), patch.object(os, 'getuid', return_value=501), patch.object(
                os, 'readlink', side_effect=cwd), patch.object(Path, 'read_text', read_text), patch.object(
                Path, 'exists', return_value=terminal is not None):
            return activity._linux_snapshot()

    def test_linux_exit_between_stat_and_cwd_is_gone_even_with_a_zombie_proc_directory(self):
        for state in ('Z', 'X'):
            with self.subTest(state=state):
                rows, ancestry, unknown = self._linux_snapshot('cwd', errno.ENOENT, terminal=state)
                self.assertNotIn(42, [row['pid'] for row in rows + unknown])
                self.assertEqual([41, 1], ancestry)

    def test_linux_missing_cwd_of_still_live_process_remains_unknown(self):
        self.assertEqual([{'pid': 42, 'reason': 'cwd_missing'}],
                         self._linux_snapshot('cwd', errno.ENOENT, terminal='S')[2])

    def test_linux_process_vanishes_between_listing_and_each_native_read(self):
        for target in ('stat', 'status', 'cwd'):
            for error in (errno.ENOENT, errno.ESRCH):
                with self.subTest(target=target, error=error):
                    rows, ancestry, unknown = self._linux_snapshot(target, error)
                    self.assertNotIn(42, [row['pid'] for row in rows + unknown])
                    self.assertEqual([41, 1], ancestry)

    def test_linux_permission_denied_remains_unknown(self):
        for target in ('stat', 'status'):
            with self.subTest(target=target), self.assertRaises(ProtocolRefusal) as caught:
                self._linux_snapshot(target, errno.EACCES)
            self.assertEqual('seat_teardown_activity_unknown', caught.exception.code)
        self.assertEqual([{'pid': 42, 'reason': 'cwd_unreadable'}],
                         self._linux_snapshot('cwd', errno.EACCES)[2])

    def test_darwin_cwd_row_is_gone_when_final_snapshot_has_no_live_pid(self):
        before = ({41: 1, 1: 0, 42: 1}, {41: 'S', 1: 'S', 42: 'S'}, {41: 501, 1: 0, 42: 501})
        for final_state in (None, 'Z', 'X'):
            after = ({41: 1, 1: 0}, {41: 'S', 1: 'S'}, {41: 501, 1: 0})
            if final_state is not None:
                after[0][42], after[1][42], after[2][42] = 1, final_state, 501
            with self.subTest(final_state=final_state), patch.object(
                    activity, '_darwin_pid_snapshot', side_effect=[before, after]), patch.object(
                    activity, '_command', return_value=subprocess.CompletedProcess([], 0, b'p42\0fcwd\0n/seat/repo\0', b'')), patch.object(
                    Path, 'is_file', return_value=True), patch.object(os, 'getpid', return_value=41), patch.object(
                    os, 'getuid', return_value=501):
                rows, ancestry, unknown = activity._darwin_snapshot()
                self.assertEqual([], rows)
                self.assertEqual([41, 1], ancestry)
                self.assertNotIn(42, [row['pid'] for row in unknown])

    def test_signal_zero_exit_is_not_alive(self):
        with patch.object(os, 'kill', side_effect=ProcessLookupError(errno.ESRCH, 'fixture exited')):
            self.assertFalse(liveness.process_alive(42))


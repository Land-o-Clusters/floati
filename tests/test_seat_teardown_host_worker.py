"""A managed host is named at teardown; the jobs it launches still block.

The copied native executable is deliberately a stand-in. Attribution is a
local work classification, not a claim that Cursor binaries are unforgeable.
"""
import errno
import os
import json
import shutil
import subprocess
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from floati.errors import ProtocolRefusal
from floati.registry import Registry
from tests import test_seat_teardown as teardown_fixture

NODE, SESSION = teardown_fixture.NODE, teardown_fixture.SESSION


class SeatTeardownHostWorkerTests(unittest.TestCase):
    def setUp(self):
        register = Registry.register
        def cursor_seat(registry, node, role):
            return register(registry, node, 'Cursor' if node == NODE else role)
        with patch.object(Registry, 'register', cursor_seat):
            teardown_fixture.SeatTeardownTests.setUp(self)
        self.user_home = self.base / 'invoking-user'
        self.user_home.mkdir()
        self.environment = patch.dict(os.environ, HOME=str(self.user_home))
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.managed = (self.user_home / 'Library/Application Support/Cursor/User/'
            'globalStorage/anysphere.cursor-agent-worker/agent-cli/.local/share/'
            'cursor-agent/versions/fixture/cursor-agent')
        self.managed.parent.mkdir(parents=True)
        compiled = subprocess.run(['/usr/bin/cc', '-o', str(self.managed),
            str(Path(__file__).parent / 'fixtures/managed_host_worker.c')],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.assertEqual(0, compiled.returncode, compiled.stderr)
        self.managed.chmod(0o700)

    def _start(self, executable, *, cwd=None, child=False):
        if child:
            command = [str(executable), '--child', str(executable)]
        else:
            command = [str(executable), '--hold']
        process = subprocess.Popen(command, cwd=cwd or self.workspace,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(process.communicate, timeout=10)
        ready = process.stdout.readline().split()
        if not ready:
            self.fail('native fixture exited before ready: ' + process.stderr.read())
        self.assertEqual('ready', ready[0])
        self.assertIsNone(process.poll())
        return process, int(ready[1])

    def _teardown(self):
        from floati.seat_teardown import SeatTeardown
        return SeatTeardown(self.root).teardown(NODE, self.home, SESSION,
            repo='floati', doc='report.md', idempotency_key='host-worker')

    def _refuses_job(self, process, pid):
        with self.assertRaises(ProtocolRefusal) as caught:
            self._teardown()
        self.assertEqual('seat_teardown_activity_in_flight', caught.exception.code)
        self.assertIn('PID ' + str(pid), caught.exception.detail)
        self.assertIsNone(process.poll(), 'activity observation must never signal a host or job')

    def test_managed_native_worker_allows_teardown_and_is_named_in_receipt(self):
        custom = (self.base / 'short-data/User/globalStorage/anysphere.cursor-agent-worker/'
            'agent-cli/.local/share/cursor-agent/versions/fixture/cursor-agent')
        custom.parent.mkdir(parents=True)
        shutil.copyfile(self.managed, custom)
        custom.chmod(0o700)
        hosts = [(executable, *self._start(executable))
            for executable in (self.managed, custom)]
        result = self._teardown()
        self.assertEqual('completed', result['step'])
        workers = result['activity']['native_host_workers']
        self.assertEqual(2, len(workers))
        self.assertEqual({pid for _, _, pid in hosts}, {worker['pid'] for worker in workers})
        for executable, process, pid in hosts:
            with self.subTest(install_root=executable):
                worker = next(worker for worker in workers if worker['pid'] == pid)
                self.assertEqual(str(executable), worker['executable'])
                self.assertEqual('cursor_managed_agent_exact_workspace', worker['reason'])
                self.assertGreater(worker['process_start_epoch'], 0)
                self.assertIsNone(process.poll(), 'the hosting process must remain alive')

    def test_same_native_binary_outside_managed_path_still_blocks(self):
        outside = self.base / 'cursor-agent'
        shutil.copyfile(self.managed, outside)
        outside.chmod(0o700)
        child_workspace = self.workspace / 'ignored-build'
        child_workspace.mkdir()
        # Isolate process attribution from the independent Git data guard.
        (self.workspace / '.git/info/exclude').write_text('/ignored-build/\n')
        for row in ('outside', 'descendant', 'workspace-subdirectory', 'symlink'):
            with self.subTest(process=row):
                if row == 'symlink':
                    # Last: this permanently replaces the managed fixture path.
                    self.managed.unlink()
                    self.managed.symlink_to(outside)
                executable = outside if row == 'outside' else self.managed
                process, pid = self._start(executable,
                    cwd=child_workspace if row == 'workspace-subdirectory' else None,
                    child=row == 'descendant')
                try:
                    if row == 'descendant':
                        self.assertNotEqual(process.pid, pid)
                    self._refuses_job(process, pid)
                finally:
                    process.communicate(timeout=10)

    def test_live_descendant_blocks_when_linux_ancestor_executable_is_denied(self):
        from floati import seat_host_worker as host
        process, child_pid = self._start(self.managed, child=True)
        native_executable = host._native_executable
        denied = []

        def executable(pid):
            if pid == os.getpid():
                denied.append(pid)
                # Linux checks ptrace access before reading /proc/PID/exe.
                # Inject that kernel boundary even on a Darwin test host.
                with patch.object(host.sys, 'platform', 'linux'), patch.object(
                        os, 'readlink', side_effect=PermissionError(errno.EACCES, 'fixture protected ancestor')):
                    return native_executable(pid)
            return native_executable(pid)

        with patch.object(host, '_native_executable', side_effect=executable):
            self._refuses_job(process, child_pid)
        self.assertIn(os.getpid(), denied)

    def test_floati_receipt_attributed_managed_process_still_blocks(self):
        from floati.planes import AuthorityGrantStore
        from floati.ids import uuid7_hex
        from floati.seat_execution import SeatExecutionController
        from floati.work import WorkLog
        from floati.workers import WorkerReceipts
        process, pid = self._start(self.managed)
        now = datetime(2026, 10, 25, tzinfo=timezone.utc)
        grant = AuthorityGrantStore(self.root).claim('work-claims', NODE, 60, 60, now)
        work = WorkLog(self.root)
        item = work.add('owned process fixture', NODE,
            [{'repo': 'floati', 'sha': self.sha, 'doc': 'report.md'}],
            car_binding={'row': 'F2-NODE-TURNOVER-1', 'repository': 'floati',
                'branch': 'main'}, now=now)
        work.claim(item['id'], NODE, 'work-claims', grant['epoch'], now=now)
        workers = WorkerReceipts(self.root)
        worker_session = 'worker-' + uuid7_hex()
        workers.append(worker_session, item['id'], NODE, 'codex', 'claim', None, [], now=now)
        source = workers.append(worker_session, item['id'], NODE, 'codex', 'spawn', None, [], now=now)
        controller = SeatExecutionController(self.root, recipient='architect')
        invocation = controller.bind_worker(source, self.workspace)
        controller.callback(invocation, {'event': 'unknown', 'code': 'worker_cleanup_unconfirmed',
            'detail': json.dumps({'adapter_pid': pid, 'registered_process_groups': [],
                'alive_after_cleanup': [pid], 'isolation_backend': 'macos-sandbox'})})
        self._refuses_job(process, pid)

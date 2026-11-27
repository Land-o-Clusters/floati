"""A different node's live Floati dispatch never becomes this seat's host."""
from tests.readiness import READY_SECONDS, Readiness, communicate, wait_join, wait_process, wait_until
import json
import multiprocessing
import os
import shutil
import subprocess
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from floati.errors import ProtocolRefusal
from floati.ids import uuid7_hex
from floati.planes import AuthorityGrantStore
from floati.registry import Registry
from floati.work import WorkLog
from tests import test_seat_teardown_host_worker as fixture


class _ManagedJobAdapter:
    name = 'fixture'

    def __init__(self, executable, workspace, ready, release, input_path=None, crash=None):
        self.executable, self.workspace = executable, workspace
        self.ready, self.release = ready, release
        self.input_path = input_path
        self.crash = crash

    def spawn(self, item, *, deadline_seconds):
        incoming = self.input_path.open('r') if self.input_path is not None else subprocess.PIPE
        try:
            process = subprocess.Popen([str(self.executable), '--hold'], cwd=self.workspace,
                stdin=incoming, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        finally:
            if self.input_path is not None:
                incoming.close()
        ready = process.stdout.readline().split()
        if len(ready) != 2 or ready[0] != 'ready':
            communicate(process, "unready managed job fixture exits")
            raise RuntimeError('native managed job did not become ready')
        publication = self.ready.with_name(self.ready.name + '.publishing')
        publication.write_text(ready[1])
        publication.replace(self.ready)
        return process

    def drive(self, process, item, *, deadline_seconds):
        readiness = Readiness("managed job fixture release", READY_SECONDS * 2)
        try:
            while not self.release.exists():
                readiness.remaining()
                if self.crash is not None and self.crash.exists():
                    os._exit(0)
                time.sleep(.01)
            return []
        finally:
            communicate(process, "managed job fixture exits after release")


def _run_worker(root, node, adapter, cwd, crash=None):
    from floati.workers import WorkerRunner
    os.chdir(cwd)
    def on_drive():
        while not crash.exists():
            time.sleep(.01)
        os._exit(0)
    # Use the product's maximum admitted call budget. The release gate's
    # safety fallback is independent of that product deadline.
    WorkerRunner(root, {'fixture': adapter}, call_timeout=READY_SECONDS).run(node, 'fixture',
        on_drive=on_drive if crash is not None else None)


class SeatTeardownWorkerProvenanceTests(unittest.TestCase):
    setUp = fixture.SeatTeardownHostWorkerTests.setUp
    _start = fixture.SeatTeardownHostWorkerTests._start
    _teardown = fixture.SeatTeardownHostWorkerTests._teardown
    _refuses_job = fixture.SeatTeardownHostWorkerTests._refuses_job

    def _other_work(self, *, car=False):
        node = 'other-worker'
        Registry(self.root).register(node, 'Codex')
        now = datetime.now(timezone.utc)
        AuthorityGrantStore(self.root).claim('work-claims', node, 60, 30, now)
        item = WorkLog(self.root).add('other node dispatch', node,
            [{'repo': 'floati', 'sha': self.sha, 'doc': 'report.md'}],
            car_binding={'row': 'F2-NODE-TURNOVER-1', 'repository': 'floati', 'branch': 'main'} if car else None,
            now=now)
        return node, item, now

    def test_other_node_live_runner_descendant_is_not_exempt_without_cleanup_audit(self):
        node, item, _ = self._other_work()
        ready, release = self.base / 'job-ready', self.base / 'job-release'
        adapter = _ManagedJobAdapter(self.managed, self.workspace, ready, release)
        worker = multiprocessing.get_context('fork').Process(target=_run_worker,
            args=(self.root, node, adapter, self.base))
        worker.start()
        def finish():
            release.touch()
            try:
                wait_join(worker, "managed job worker fixture exits")
            except AssertionError:
                worker.terminate()
                wait_join(worker, "collect terminated managed job worker fixture")
            self.assertEqual(0, worker.exitcode)
            if ready.exists():
                from floati.seat_host_worker import _identity
                pid = int(ready.read_text())
                def absent():
                    return _identity(pid) is None

                wait_until(absent, "native job exits after fixture EOF")
        self.addCleanup(finish)
        wait_until(lambda: ready.exists() or not worker.is_alive(),
            "other-node worker managed child publication")
        self.assertTrue(ready.exists(), 'the real other-node worker exited before launching its managed child')
        pid = int(ready.read_text())
        self.assertEqual('claimed', WorkLog(self.root).show(item['id'])[0]['state'])
        with self.assertRaises(ProtocolRefusal) as caught:
            self._teardown()
        self.assertEqual('seat_teardown_activity_in_flight', caught.exception.code)
        self.assertIn('PID ' + str(pid), caught.exception.detail)

    def test_runner_anchor_is_visible_before_the_first_child_can_start(self):
        from floati.workers import WorkerRunner
        from tests.test_workers import _RuntimeIdentityAdapter
        node, _, _ = self._other_work()
        process, pid = self._start(self.managed)
        original = multiprocessing.process.BaseProcess.start
        observed = []
        def start(child):
            with self.assertRaises(ProtocolRefusal) as caught:
                self._teardown()
            self.assertEqual('seat_teardown_activity_in_flight', caught.exception.code)
            self.assertIn('PID ' + str(pid), caught.exception.detail)
            observed.append(True)
            return original(child)
        with patch.object(multiprocessing.process.BaseProcess, 'start', start):
            WorkerRunner(self.root, {'fixture': _RuntimeIdentityAdapter(self.base / 'identity')}).run(node, 'fixture')
        self.assertEqual([True], observed)
        self.assertIsNone(process.poll())

    def test_symlinked_anchor_storage_refuses_launch_with_existing_degrade_receipt(self):
        from floati.workers import WorkerRunner
        from tests.test_workers import _RuntimeIdentityAdapter
        node, _, _ = self._other_work()
        outside = self.base / 'foreign-anchor-storage'
        outside.mkdir()
        anchor = self.root.tenant_home / 'state/worker-runner-anchors'
        anchor.parent.mkdir(exist_ok=True)
        anchor.symlink_to(outside, target_is_directory=True)
        identity = self.base / 'must-not-launch'
        result = WorkerRunner(self.root, {'fixture': _RuntimeIdentityAdapter(identity)}).run(node, 'fixture')
        self.assertEqual('degrade', result['transition'])
        self.assertEqual('process_start_failed', result['outcome_code'])
        self.assertFalse(identity.exists())
        self.assertEqual([], list(outside.iterdir()))

    def test_unreadable_runner_identity_retains_degradation_without_launch(self):
        from floati.workers import WorkerRunner
        from tests.test_workers import _RuntimeIdentityAdapter
        node, _, _ = self._other_work()
        identity = self.base / 'must-not-launch'
        with patch('floati.seat_host_worker._identity', side_effect=ProtocolRefusal(
                'seat_teardown_activity_unknown', 'native identity unreadable')):
            result = WorkerRunner(self.root, {'fixture': _RuntimeIdentityAdapter(identity)}).run(node, 'fixture')
        self.assertEqual('degrade', result['transition'])
        self.assertEqual('process_start_failed', result['outcome_code'])
        self.assertFalse(identity.exists())

    def test_crashed_runner_anchor_keeps_orphan_managed_job_out_of_host_class(self):
        node, _, _ = self._other_work()
        ready, release, crash = (self.base / name for name in ('ready', 'release', 'crash'))
        incoming = self.base / 'job-input'
        os.mkfifo(incoming)
        writer = os.open(incoming, os.O_RDWR | os.O_NONBLOCK)
        adapter = _ManagedJobAdapter(self.managed, self.workspace, ready, release, incoming, crash)
        worker = multiprocessing.get_context('fork').Process(target=_run_worker,
            args=(self.root, node, adapter, self.base, crash))
        worker.start()
        def finish():
            crash.touch()
            release.touch()
            os.close(writer)
            try:
                wait_join(worker, "managed job worker fixture exits")
            except AssertionError:
                worker.terminate()
                wait_join(worker, "collect terminated managed job worker fixture")
            self.assertEqual(0, worker.exitcode)
            if ready.exists():
                from floati.seat_host_worker import _identity
                pid = int(ready.read_text())
                def absent():
                    return _identity(pid) is None

                wait_until(absent, "orphan native job exits after fixture EOF")
        self.addCleanup(finish)
        wait_until(lambda: ready.exists() or not worker.is_alive(),
            "orphan managed child publication")
        self.assertTrue(ready.exists(), 'worker exited before managed child publication')
        pid = int(ready.read_text())
        from floati.seat_host_worker import _identity
        original_parent = _identity(pid)['ppid']
        crash.touch()
        wait_join(worker, "crashed runner fixture exits")
        self.assertEqual(0, worker.exitcode)
        readiness = Readiness("orphan native job reparenting")
        identity = _identity(pid)
        while identity is not None and identity['ppid'] == original_parent:
            readiness.remaining()
            time.sleep(.01)
            identity = _identity(pid)
        self.assertIsNotNone(identity, 'the externally held native job must survive its runner')
        self.assertNotEqual(original_parent, identity['ppid'])
        with self.assertRaises(ProtocolRefusal) as caught:
            self._teardown()
        self.assertEqual('seat_teardown_activity_in_flight', caught.exception.code)
        self.assertIn('PID ' + str(pid), caught.exception.detail)

    def test_failed_terminal_callback_retains_uncertain_launch_provenance(self):
        from floati.seat_execution import SeatExecutionController
        from floati.workers import WorkerRunner
        from tests.test_workers import _RuntimeIdentityAdapter
        node, _, _ = self._other_work(car=True)
        runner = WorkerRunner(self.root, {'fixture': _RuntimeIdentityAdapter(self.base / 'identity')})
        with patch.object(SeatExecutionController, 'callback', side_effect=OSError('unavailable durable report')) as callback:
            runner.run(node, 'fixture')
        self.assertEqual(1, callback.call_count)
        self.assertEqual('unknown', runner.last_execution_report['status'])
        process, pid = self._start(self.managed)
        self._refuses_job(process, pid)

    def test_cleanup_group_leader_remains_work_when_adapter_pid_is_different(self):
        from floati.seat_execution import SeatExecutionController
        from floati.workers import WorkerReceipts
        node, item, now = self._other_work(car=True)
        WorkLog(self.root).claim(item['id'], node, 'work-claims', 1, now=now)
        process = subprocess.Popen([str(self.managed), '--hold'], cwd=self.workspace,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, start_new_session=True)
        self.addCleanup(process.communicate, timeout=10)
        ready = process.stdout.readline().split()
        self.assertEqual(['ready', str(process.pid)], ready)
        pid = process.pid
        outside = self.base / 'adapter'
        shutil.copyfile(self.managed, outside)
        outside.chmod(0o700)
        adapter, _ = self._start(outside, cwd=self.base)
        session = 'worker-' + uuid7_hex()
        workers = WorkerReceipts(self.root)
        workers.append(session, item['id'], node, 'codex', 'claim', None, [], now=now)
        source = workers.append(session, item['id'], node, 'codex', 'spawn', None, [], now=now)
        controller = SeatExecutionController(self.root, recipient='architect')
        invocation = controller.bind_worker(source, self.workspace)
        controller.callback(invocation, {'event': 'unknown', 'code': 'worker_cleanup_unconfirmed',
            'detail': json.dumps({'adapter_pid': adapter.pid, 'registered_process_groups': [pid],
                'alive_after_cleanup': [pid], 'isolation_backend': 'macos-sandbox'})})
        self._refuses_job(process, pid)

    def test_cleanup_group_still_identifies_job_after_its_leader_exits(self):
        from floati.seat_execution import SeatExecutionController
        from floati.workers import WorkerReceipts
        node, item, now = self._other_work(car=True)
        WorkLog(self.root).claim(item['id'], node, 'work-claims', 1, now=now)
        leader = subprocess.Popen([str(self.managed), '--orphan-child', str(self.managed)],
            cwd=self.workspace, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, start_new_session=True)
        self.addCleanup(leader.communicate, timeout=10)
        ready = leader.stdout.readline().split()
        self.assertEqual('ready', ready[0])
        pid = int(ready[1])
        self.assertEqual(0, wait_process(leader, "managed group leader exits after its readiness frame"))
        self.assertNotEqual(pid, leader.pid)
        self.assertEqual(leader.pid, os.getpgid(pid))
        session = 'worker-' + uuid7_hex()
        workers = WorkerReceipts(self.root)
        workers.append(session, item['id'], node, 'codex', 'claim', None, [], now=now)
        source = workers.append(session, item['id'], node, 'codex', 'spawn', None, [], now=now)
        controller = SeatExecutionController(self.root, recipient='architect')
        invocation = controller.bind_worker(source, self.workspace)
        controller.callback(invocation, {'event': 'unknown', 'code': 'worker_cleanup_unconfirmed',
            'detail': json.dumps({'adapter_pid': leader.pid, 'registered_process_groups': [leader.pid],
                'alive_after_cleanup': [leader.pid], 'isolation_backend': 'macos-sandbox'})})
        with self.assertRaises(ProtocolRefusal) as caught:
            self._teardown()
        self.assertEqual('seat_teardown_activity_in_flight', caught.exception.code)
        self.assertIn('PID ' + str(pid), caught.exception.detail)

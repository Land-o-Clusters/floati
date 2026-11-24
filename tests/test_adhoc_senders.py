"""Sender conversations use real scratch receipts and independent Python providers."""
from copy import copy
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import subprocess
import unittest
from unittest import mock

from floati.adhoc_threads import AdhocThreadController
from floati.errors import ProtocolRefusal
from floati.ids import uuid7_hex
from floati.workers import WorkerReceipts
from tests.test_adhoc_threads import ControllerFixture
from tests.readiness import Readiness


PROVIDER = r'''
import json, os, pathlib, sys, time
v = json.load(sys.stdin)
slot = pathlib.Path(v['slot'])
with (slot / 'launches').open('a') as stream:
    stream.write('launch\n')
history = slot / 'conversation.json'
if v['operation'] == 'resume' and not history.exists():
    print(json.dumps({'code': 'native_session_unmaterialized'}))
    raise SystemExit(20)
saved = json.loads(history.read_text()) if history.exists() else {
    'coordinate': v['coordinate'], 'senders': [], 'requests': []}
if v['operation'] == 'resume' and saved['coordinate'] != v['coordinate']:
    raise SystemExit(21)
saved['senders'].append(v['sender'])
saved['requests'].append(v['request'])
history.write_text(json.dumps(saved))
if v['barrier']:
    barrier = pathlib.Path(v['barrier'])
    (barrier / v['sender']).touch()
    started = time.monotonic()
    bound = 60.0
    deadline = started + bound
    while len(list(barrier.iterdir())) != 2:
        if time.monotonic() >= deadline:
            print('provider barrier readiness: bound=%.3fs elapsed=%.3fs host_load=%r'
                  % (bound, time.monotonic() - started, os.getloadavg()), file=sys.stderr)
            raise SystemExit(22)
        time.sleep(0.01)
result = {'schema_version': 1, 'dispatch_digest': v['digest'],
    'provider_coordinate': saved['coordinate'], 'summary': json.dumps(saved),
    'inbox_dispositions': [{'message_id': row['id'], 'disposition': 'acted',
        'reason': 'handled this request', 'result_paths': []} for row in v['messages']]}
path = pathlib.Path(v['result'])
path.parent.mkdir(parents=True, exist_ok=True)
path.write_text(json.dumps(result) + '\n')
print(json.dumps(saved))
'''


class SenderConversationsTests(ControllerFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        c = self.case
        self.provider = c.base / 'provider.py'
        self.provider.write_text(PROVIDER)
        from floati.adhoc_launch import DEFAULT_POLICY, grant_adhoc_launch
        # This scratch operator explicitly grants capacity for concurrent senders.
        (c.root.path / 'FLOATI.toml').write_text(DEFAULT_POLICY.replace('max_concurrency = 1', 'max_concurrency = 8'))
        self.peers = {}
        self.barrier = None
        for sender in ('foreign', 'permanent'):
            opened = c.lanes.open(actor='node-a', row='serve-' + sender, repo='fixture')
            profile = c.profiles().create('node-a', 'codex', opened['record']['id'],
                'fixture', sender, idempotency_key='profile-' + sender, now=c.now)
            peer = copy(c)
            peer.profile_record = profile
            peer.coordinate = {'provider': 'codex_local', 'session_id': self.coordinate_id(),
                               'session_file': None}
            self.peers[sender] = peer
            grant_adhoc_launch(c.root, profile, authority_subject='borrowed-work', authority_epoch=1)
        self.requests = {}

    @staticmethod
    def coordinate_id():
        value = uuid7_hex()
        return '-'.join((value[:8], value[8:12], value[12:16], value[16:20], value[20:]))

    def request(self, sender, number, *, profile=None, operation=None, prior_request=None, version=2):
        peer = self.peers[sender]
        c = self.case
        profile = peer.profile_record if profile is None else profile
        request = prior_request or c.events.send(sender, 'node-a', 'fixture', c.base_sha, 'tracked.txt',
            sender + '-' + str(number), idempotency_key=sender + '-request-' + str(number), now=c.now)
        # A duplicate request needs a second item to reach the ad hoc refusal; the product never authors one.
        item = c.work.add('answer ' + request['id'], 'node-a', [],
            adhoc_profile_id=profile['id'], now=c.now, refuse_duplicates=prior_request is None)
        dispatch = c.dispatch(profile, schema_version=2, request_id=request['id'], node_id='node-a',
            node_lease_id=profile['node_lease_id'], lane_opening_id=profile['lane_opening_id'],
            work_item_id=item['id'], authority_subject='borrowed-work',
            operation=operation or ('start' if number == 1 else 'resume'), result_recipient=sender,
            result_document='docs/evidence/' + sender + '-' + str(number) + '.json')
        if version == 1:
            dispatch['schema_version'] = 1
            del dispatch['request_id']
        envelope = c.envelope(dispatch, name=sender + '-dispatch-' + str(number), recipient='node-a')
        invocation = {'request': request, 'dispatch': dispatch, 'envelope': envelope,
                      'profile': profile, 'key': sender + '-run-' + str(number)}
        self.requests[sender, number] = invocation
        return invocation

    def controller_for(self, invocation):
        c = self.case
        peer = self.peers[invocation['request']['sender']]
        controller = AdhocThreadController(c.root, clock=lambda: c.now)
        prepare = controller._prepare_execution
        owner = None

        def execute(node, adapter, work, *, native_thread_context, on_native_event,
                    startup_prompt='', now=None):
            from floati.native_thread_protocol import NativeThreadProtocol
            self.assertIs(type(getattr(on_native_event, '__self__', None)), NativeThreadProtocol,
                'the real worker requires the original bound native callback')
            receipts = WorkerReceipts(c.root)
            session = 'worker-' + uuid7_hex()
            c.work.claim(work, node, 'borrowed-work', 1, now=c.now)
            receipts.append(session, work, node, adapter, 'claim', None, [], now=c.now)
            receipts.append(session, work, node, adapter, 'spawn', None, [], now=c.now)
            prompt = json.loads(startup_prompt)
            payload = {'slot': native_thread_context['provider_state_binding']['path'],
                'operation': native_thread_context['operation'], 'coordinate': peer.coordinate,
                'sender': invocation['request']['sender'], 'request': invocation['request']['id'],
                'barrier': None if self.barrier is None else str(self.barrier),
                'digest': native_thread_context['dispatch_digest'],
                'messages': prompt['messages'], 'result': invocation['dispatch']['result_document']}
            # Exactly one independently executed fake provider per native launch.
            child = subprocess.Popen(['/usr/bin/python3', '-B', str(self.provider)],
                cwd=invocation['profile']['workspace'], text=True, stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
            try:
                readiness = Readiness("fake provider completes conversation", timeout=120)
                try:
                    stdout, stderr = child.communicate(json.dumps(payload), timeout=readiness.remaining())
                except subprocess.TimeoutExpired:
                    raise readiness.failure() from None
            finally:
                if child.poll() is None:
                    child.terminate()
                    child.communicate(timeout=3)
            owner.last_process_audit.update(adapter_pid=child.pid, registered_process_groups=[child.pid])
            owner.last_native_transport = {'stdout_bytes': len(stdout.encode()),
                'stderr_bytes': len(stderr.encode()), 'combined_bytes': len(stdout.encode()) + len(stderr.encode()),
                'protocol_records': 0, 'stdout_sha256': hashlib.sha256(stdout.encode()).hexdigest(),
                'stderr_sha256': hashlib.sha256(stderr.encode()).hexdigest(), 'exit_code': child.returncode}
            if child.returncode:
                code = json.loads(stdout)['code'] if child.returncode == 20 else 'process_died'
                raise ProtocolRefusal(code, 'fake provider refused: ' + stderr)
            for kind in ('opened', 'terminal'):
                peer.context = native_thread_context
                event = peer.event(kind)
                on_native_event(event)
                owner.last_native_events.append(event)
            receipts.append(session, work, node, adapter, 'drive', None, [], now=c.now)
            return receipts.append(session, work, node, adapter, 'bind_artifact', None, [], now=c.now)

        def prepare_fake(*args):
            nonlocal owner
            owner, effect = prepare(*args)
            owner.last_native_events = []
            owner.last_process_audit = {'adapter_pid': None, 'registered_process_groups': [],
                'alive_after_cleanup': [], 'isolation_backend': 'fixture-process'}
            owner.run_exact = execute
            return owner, effect
        controller._prepare_execution = prepare_fake
        return controller

    def run_request(self, invocation, controller=None):
        controller = self.controller_for(invocation) if controller is None else controller
        return controller.run(invocation['profile']['id'], invocation['envelope']['id'],
            actor='node-a', acting_session_id='fixture-parent', idempotency_key=invocation['key'])

    def bank(self, invocation):
        c = self.case
        profile = invocation['profile']
        sha = c.git(Path(profile['workspace']), 'rev-parse', 'HEAD')
        c.git(c.repository, 'update-ref', 'refs/remotes/origin/result-' + profile['id'], sha)
        c.git(c.repository, 'push', '--quiet', 'origin', sha + ':refs/heads/' + profile['branch'])

    def finish(self, invocation):
        controller = self.controller_for(invocation)
        finish = controller._finish
        def bank_and_finish(admission, result):
            self.bank(invocation)
            return finish(admission, result)
        controller._finish = bank_and_finish
        return self.run_request(invocation, controller)

    def result(self, projection):
        return next(row['evidence']['result'] for row in projection['phases']
                    if row['phase'] == 'process_closed' and row['evidence'].get('stage') == 'result')

    def launches(self, sender):
        slot = self.case.root.path / self.peers[sender].profile_record['provider_state']
        return len((slot / 'launches').read_text().splitlines())

    def test_interleaved_senders_keep_only_their_own_conversation_and_receipts(self):
        requests = [self.request(sender, 1) for sender in self.peers]
        coordinates = []
        for invocation in requests:
            projection = self.finish(invocation)
            sender = invocation['request']['sender']
            result = self.result(projection)
            history = json.loads(result['summary'])
            self.assertEqual([sender], history['senders'])
            self.assertEqual({invocation['request']['id'], invocation['envelope']['id']},
                {row['message_id'] for row in result['inbox_dispositions']})
            coordinates.append(result['provider_coordinate'])
            published = next(row for row in projection['phases'] if row['phase'] == 'result_published')
            envelope = next(row for row in self.case.events.event_records()
                if row['id'] == published['evidence']['envelope_id'])
            self.assertEqual(sender, envelope['recipient'])
            self.assertEqual(invocation['request']['id'], envelope['reply_to'])
            self.assertEqual(1, self.launches(sender))
        self.assertNotEqual(*coordinates)
        projection = self.finish(self.request('foreign', 2))
        self.assertEqual(['foreign', 'foreign'], json.loads(self.result(projection)['summary'])['senders'])
        self.assertEqual(2, self.launches('foreign'))

        peer = self.peers['foreign']
        (self.case.root.path / peer.profile_record['provider_state'] / 'conversation.json').unlink()
        request = self.request('foreign', 3)
        for case, controller in (
                ('missing-conversation', self.controller_for(request)),
                ('saved-refusal', AdhocThreadController(self.case.root, clock=lambda: self.case.now))):
            with self.subTest(case=case):
                with self.assertRaises(ProtocolRefusal) as caught:
                    self.run_request(request, controller)
                self.assertEqual('native_session_unmaterialized', caught.exception.code)
                self.assertEqual(3, self.launches('foreign'))

    def test_different_senders_reach_provider_barrier_in_parallel(self):
        requests = [self.request(sender, 1) for sender in self.peers]
        controllers = [self.controller_for(request) for request in requests]
        admissions = []
        # Admission/approval setup is serialized; only the actual served turns
        # overlap. This avoids making the short ledger-lock budget a timing
        # dependency of the provider overlap property on a shared test host.
        for request, controller in zip(requests, controllers):
            admission = self.admit(request)
            admissions.append(admission)
            prepared = controller._prepare_execution(request['profile'], admission)
            controller._prepare_execution = lambda *_, prepared=prepared: prepared
        self.barrier = self.case.base / 'parallel-barrier'
        self.barrier.mkdir()
        def run(pair):
            try:
                self.run_request(*pair)
            except ProtocolRefusal as exc:
                return exc.code
        with ThreadPoolExecutor(max_workers=2) as pool:
            codes = list(pool.map(run, zip(requests, controllers)))
        self.assertEqual(['adhoc_result_unbanked'] * 2, codes)
        self.assertEqual(set(self.peers), {path.name for path in self.barrier.iterdir()})
        for request, controller, admission in zip(requests, controllers, admissions):
            with self.subTest(case=request['request']['sender']):
                projection = controller.show(admission['dispatch_id'])
                self.assertTrue(any(row['phase'] == 'process_closed'
                    and row['evidence'].get('stage') == 'result' for row in projection['phases']))
                sender = request['request']['sender']
                self.assertEqual([sender], json.loads(self.result(projection)['summary'])['senders'])
                self.assertEqual(1, self.launches(sender))

    def test_restart_after_acceptance_closes_one_attempt_without_relaunch(self):
        request = self.request('foreign', 1)
        with self.assertRaises(ProtocolRefusal) as caught:
            self.run_request(request)
        self.assertEqual('adhoc_result_unbanked', caught.exception.code)
        self.bank(request)
        with mock.patch('floati.scheduler.RunScheduler.terminal_attempt', side_effect=RuntimeError('lost after acceptance')):
            with self.assertRaisesRegex(RuntimeError, 'lost after acceptance'):
                self.run_request(request, AdhocThreadController(self.case.root, clock=lambda: self.case.now))
        fresh = AdhocThreadController(self.case.root, clock=lambda: self.case.now)
        with mock.patch.object(fresh, '_prepare_execution', side_effect=AssertionError('must not relaunch')):
            result = self.run_request(request, fresh)
            self.assertEqual(result, self.run_request(request, fresh))
        from floati.runtruth import RunLedger
        run_id = 'run-' + result['dispatch_id'].removeprefix('adhoc-dispatch-')
        rows = [row for row in RunLedger(self.case.root).records() if row['run_id'] == run_id]
        self.assertEqual(1, sum(row['kind'] == 'result_accepted' for row in rows))
        self.assertEqual(1, sum(row['kind'] == 'attempt_terminal' for row in rows))
        self.assertEqual(1, self.launches('foreign'))
        self.assertEqual('result_published', result['phase'])

    def admit(self, invocation):
        return AdhocThreadController(self.case.root, clock=lambda: self.case.now).journal.admit(
            invocation['profile']['id'], invocation['envelope']['id'], invocation['dispatch'],
            actor='node-a', acting_session_id='fixture-parent', idempotency_key=invocation['key'], now=self.case.now)

    def test_sender_and_profile_are_bound_atomically_before_launch(self):
        # Refuse foreign coordinates before any successful sender binding.
        c = self.case
        self.peers['coordinator'] = copy(c)
        with self.subTest(case='legacy-conversation'):
            request = self.request('coordinator', 1, profile=c.profile_record)
            with self.assertRaises(ProtocolRefusal) as caught:
                self.admit(request)
            self.assertEqual('adhoc_sender_binding_changed', caught.exception.code)
        for number, (field, value) in enumerate(
                [('recipient', 'foreign'), ('repo', 'another-repository')], start=101):
            with self.subTest(case=field):
                fields = {'sender': 'foreign', 'recipient': 'node-a', 'repo': 'fixture'}
                fields[field] = value
                request = c.events.send(**fields, sha=c.base_sha, doc='tracked.txt',
                    note='foreign coordinate', now=c.now)
                invocation = self.request('foreign', number, prior_request=request, operation='start')
                with self.assertRaises(ProtocolRefusal) as caught:
                    self.admit(invocation)
                self.assertEqual('adhoc_sender_binding_changed', caught.exception.code)
        first = self.request('foreign', 1)
        self.admit(first)
        # No provider has run; even two concurrent first requests cannot acquire
        # different conversations for the same sender.
        c = self.case
        opened = c.lanes.open(actor='node-a', row='foreign-alternate', repo='fixture')
        alternate = c.profiles().create('node-a', 'codex', opened['record']['id'],
            'fixture', 'foreign', idempotency_key='foreign-alternate', now=c.now)
        second = self.request('foreign', 2, profile=alternate, operation='start')
        with self.assertRaises(ProtocolRefusal) as caught:
            self.admit(second)
        self.assertEqual('adhoc_sender_binding_changed', caught.exception.code)
        other = self.request('permanent', 1, profile=first['profile'])
        with self.assertRaises(ProtocolRefusal):
            self.admit(other)
        duplicate = self.request('foreign', 3, prior_request=first['request'])
        with self.assertRaises(ProtocolRefusal) as caught:
            self.admit(duplicate)
        self.assertEqual('adhoc_request_conflict', caught.exception.code)
        legacy = self.request('foreign', 4, version=1)
        with self.assertRaises(ProtocolRefusal) as caught:
            self.admit(legacy)
        self.assertEqual('adhoc_sender_binding_changed', caught.exception.code)

"""Performing teardown with real native ledgers and scratch Git.

The activity observer is an explicitly isolated service seam here. The union
integration tests run the separately owned native observer without this patch.
"""
import contextlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from floati.codex_wait_contract import WORKSPACE_MAP_RELATIVE, CodexWaitConsentLedger, CodexWaitSessionLedger, resolve_participant
from floati.cursor import SparseCursor
from floati.errors import IntegrityFailure, ProtocolRefusal
from floati.events import EventLog
from floati.jsonl import read_records_snapshot
from floati.registry import Registry
from floati.root import FloatiRoot
from floati.seat_declaration import FleetGovernance, SeatDeclaration
from floati.wake_control import WakeController
from tests.test_prep_clear import _git
from tests.temp_roots import REAL_TEMP_ROOT

NODE = 'builder-a'
SESSION = 'teardown-session'


class SeatTeardownCodexHostTests(unittest.TestCase):
    """The Stop hook and tool command can have disjoint native ancestry."""

    def setUp(self):
        register = Registry.register

        def codex_seat(registry, node, role):
            return register(registry, node, 'codex' if node == NODE else role)

        with mock.patch.object(Registry, 'register', codex_seat):
            SeatTeardownTests.setUp(self)
        self.chat, self.shell, self.waiter, self.job = range(987701, 987705)
        self.birth = 1700000000.25
        self.identities = {
            self.chat: {'pid': self.chat, 'ppid': self.shell, 'uid': os.getuid(),
                        'birth': (self.chat,), 'process_start_epoch': self.birth},
            self.shell: {'pid': self.shell, 'ppid': 1, 'uid': os.getuid(),
                         'birth': (self.shell,), 'process_start_epoch': self.birth - 1},
        }
        from floati.stop_hold import StopHoldLedger
        from floati.registry import utc_now
        from floati.waiter_invocation import _paths, _write
        holds = StopHoldLedger(self.root)
        with mock.patch('floati.stop_hold.os.getpid', return_value=self.waiter), mock.patch(
                'floati.codex_wait_liveness.process_start_epoch', return_value=self.birth + 1):
            start = holds.start(self.binding, self.consent, SESSION,
                started_at=utc_now(), deadline_seconds=1)
        holds.close(start, timestamp=utc_now())
        self.state_path = _paths(self.root, NODE, SESSION)[1]
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state = {'node_id': NODE, 'session_id': SESSION, 'workspace': str(self.workspace),
            'pid': self.waiter, 'process_start_epoch': self.birth + 1,
            'parent_pid': self.chat, 'parent_process_start_epoch': self.birth,
            'host_ancestry': [{'pid': pid, 'process_start_epoch': info['process_start_epoch'],
                               'parent_pid': info['ppid']} for pid, info in self.identities.items()],
            'token': 'native-invocation-token', 'invocation_id': start['invocation_id'], 'phase': 'exited'}
        _write(self.state_path, self.state)

    def _teardown(self, *, job=False):
        from floati.waiter_invocation import _write
        _write(self.state_path, self.state)
        pids = [self.chat, self.shell] + ([self.job] if job else [])
        rows = [{'pid': pid, 'cwd': str(self.workspace)} for pid in pids]
        with mock.patch('floati.seat_teardown_activity._native_snapshot',
                        return_value=(rows, [os.getpid(), 1], [])), mock.patch(
                'floati.seat_host_worker._identity', side_effect=self.identities.get):
            from floati.seat_teardown import SeatTeardown
            return SeatTeardown(self.root).teardown(NODE, self.home, SESSION,
                repo='floati', doc='report.md', idempotency_key='codex-host')

    def test_acting_chat_and_launch_shell_allow_teardown_across_daemon_ancestry(self):
        result = self._teardown()
        self.assertEqual('completed', result['step'])
        self.assertTrue({self.chat, self.shell} <= set(result['activity']['exempt_ancestry']))
        self.assertEqual([], result['activity']['native_jobs'])

    def test_other_workspace_job_still_refuses(self):
        # A child of the acting chat is still work, not another host ancestor.
        self.identities[self.job] = dict(self.identities[self.chat], pid=self.job, ppid=self.chat)
        with self.assertRaises(ProtocolRefusal) as caught:
            self._teardown(job=True)
        self.assertEqual('seat_teardown_activity_in_flight', caught.exception.code)
        self.assertIn('PID ' + str(self.job), caught.exception.detail)
        self.assertFalse(WakeController(self.root).status(NODE, SESSION)['state'] == 'paused')

    def test_exited_hook_shell_preserves_live_chat_ancestry(self):
        hook_shell = self.waiter + 10
        self.state['parent_pid'] = hook_shell
        self.state['parent_process_start_epoch'] = self.birth + .5
        self.state['host_ancestry'].insert(0, {'pid': hook_shell,
            'process_start_epoch': self.birth + .5, 'parent_pid': self.chat})
        self.assertEqual('completed', self._teardown()['step'])

    def test_running_waiter_preflight_and_exited_final_cutoff(self):
        from floati.registry import utc_now
        from floati.stop_hold import StopHoldLedger
        from floati.seat_teardown_activity import inspect_seat_activity
        from floati.waiter_invocation import _write
        holds = StopHoldLedger(self.root)
        with mock.patch('floati.stop_hold.os.getpid', return_value=self.waiter), mock.patch(
                'floati.codex_wait_liveness.process_start_epoch', return_value=self.birth + 1):
            start = holds.start(self.binding, self.consent, SESSION,
                started_at=utc_now(), deadline_seconds=1)
        self.state.update(phase='running', invocation_id=start['invocation_id'])
        _write(self.state_path, self.state)
        self.identities[self.waiter] = dict(self.identities[self.chat],
            pid=self.waiter, ppid=self.chat, process_start_epoch=self.birth + 1)
        rows = [{'pid': pid, 'cwd': str(self.workspace)}
                for pid in (self.chat, self.shell, self.waiter)]
        with mock.patch('floati.seat_teardown_activity._native_snapshot',
                        return_value=(rows, [os.getpid(), 1], [])), mock.patch(
                'floati.seat_host_worker._identity', side_effect=self.identities.get), mock.patch(
                'floati.waiter_invocation._same_process', return_value=True):
            activity = inspect_seat_activity(self.home, self.workspace,
                root=self.root, node=NODE, session=SESSION, _retiring_waiter=self.state)
        self.assertEqual([], activity['native_jobs'])
        self.assertNotIn(self.waiter, activity['exempt_ancestry'])
        holds.close(start, timestamp=utc_now())
        self.state['phase'] = 'exited'
        self.identities.pop(self.waiter)
        self.assertEqual('completed', self._teardown()['step'])

    def test_unjoined_or_changed_native_host_evidence_cannot_exempt_chat(self):
        from copy import deepcopy
        original = deepcopy(self.state)
        identities = deepcopy(self.identities)
        for kind in ('session', 'workspace', 'node', 'invocation', 'waiter-birth',
                     'chat-birth', 'parent', 'uid', 'cycle', 'missing'):
            with self.subTest(evidence=kind):
                self.state = deepcopy(original)
                self.identities = deepcopy(identities)
                if kind in {'session', 'workspace', 'node', 'invocation'}:
                    field = {'session': 'session_id', 'workspace': 'workspace',
                             'node': 'node_id', 'invocation': 'invocation_id'}[kind]
                    self.state[field] = 'other-coordinate'
                elif kind == 'waiter-birth':
                    self.state['process_start_epoch'] += 1
                elif kind == 'chat-birth':
                    self.identities[self.chat]['process_start_epoch'] += 1
                elif kind == 'parent':
                    self.identities[self.chat]['ppid'] = 1
                elif kind == 'uid':
                    self.identities[self.chat]['uid'] += 1
                elif kind == 'cycle':
                    self.state['host_ancestry'].append(self.state['host_ancestry'][0])
                else:
                    self.state.pop('host_ancestry')
                with self.assertRaises(ProtocolRefusal) as caught:
                    self._teardown()
                self.assertEqual('seat_teardown_activity_in_flight', caught.exception.code)
                self.assertIn('PID ' + str(self.chat), caught.exception.detail)


class SeatTeardownTests(unittest.TestCase):
    def setUp(self):
        self.base = Path(tempfile.mkdtemp(dir=REAL_TEMP_ROOT))
        self.addCleanup(shutil.rmtree, self.base, True)
        self.home = self.base / 'seat-home'
        self.workspace = self.home / 'repo'
        self.workspace.mkdir(parents=True)
        self.root = FloatiRoot.open_direct_home(self.base / 'fleet', create=True)
        registry = Registry(self.root)
        registry.register(NODE, 'builder')
        registry.register('architect', 'architect')
        self.governance = FleetGovernance.create(self.root, topology='star', coordinator='architect',
            coordinator_authority=['dispatch_bounded_work'], owner_tier=['publishing'])
        SeatDeclaration.publish_declared_workspace(self.workspace, NODE, self.root, self.governance)
        _git(['init','-q','-b','main',str(self.workspace)], self.base)
        remote = self.base / 'remote.git'
        _git(['init','-q','--bare',str(remote)], self.base)
        _git(['remote','add','origin',str(remote)], self.workspace)
        (self.workspace / 'report.md').write_text('completed scratch work\n')
        _git(['add','.'], self.workspace)
        _git(['commit','-q','-m','declared scratch seat'], self.workspace)
        _git(['push','-q','-u','origin','main'], self.workspace)
        self.sha = _git(['rev-parse','HEAD'], self.workspace).stdout.strip()
        path = self.root.tenant_home / WORKSPACE_MAP_RELATIVE
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({'schema_version':0,'tenant_id':self.root.tenant_id,
            'mappings':[{'workspace':str(self.workspace),'node_id':NODE}]})+'\n')
        self.binding = resolve_participant(self.root.tenant_home, self.workspace).binding
        self.consent = CodexWaitConsentLedger(self.root).arm(self.binding, hook_timeout_seconds=10,
            wait_deadline_seconds=2,idempotency_key='consent')
        self.claim = CodexWaitSessionLedger(self.root).arm(self.binding,self.consent,SESSION,idempotency_key='claim')

    def service(self):
        self.assertIsNotNone(importlib.util.find_spec('floati.seat_teardown'), 'performing teardown service is missing')
        from floati.seat_teardown import SeatTeardown
        return SeatTeardown(self.root)

    def teardown(self, **kwargs):
        service = self.service()
        args = dict(node=NODE,home=self.home,session=SESSION,repo='floati',doc='report.md',idempotency_key='teardown-one')
        args.update(kwargs)
        activity = self.activity()
        with mock.patch('floati.seat_teardown._inspect_activity', return_value=activity):
            return service.teardown(**args)

    def activity(self):
        return dict(workspace_set=[str(self.workspace)],worktrees=[{'workspace':str(self.workspace),
            'head':self.sha,'tracked_changes':[],'untracked_files':[],'unpushed_commits':[]}],
            native_jobs=[],native_unknown=[],native_roots=[str(self.home)],exempt_ancestry=[os.getpid()],
            native_scope={'population':'invoking_uid','uid':os.getuid()})

    def rows(self):
        return read_records_snapshot(self.root,'receipts/seat-teardown/'+NODE+'.jsonl',allowed_kinds={'seat_teardown_receipt'})

    def send(self, key):
        return EventLog(self.root).send('architect',NODE,'floati',self.sha,'report.md','scratch action',idempotency_key=key)['message']

    def test_valid_pauses_before_report_and_preserves_shared_bytes(self):
        map_bytes = (self.root.tenant_home / WORKSPACE_MAP_RELATIVE).read_bytes()
        result = self.teardown()
        self.assertEqual('completed',result['step'])
        self.assertEqual(['started','paused','reported','completed'],[r['step'] for r in self.rows()])
        self.assertEqual(self.claim['id'],result['claim_receipt_id'])
        self.assertEqual(self.sha,result['pushed_tip'])
        self.assertEqual('paused',WakeController(self.root).status(NODE,SESSION)['state'])
        report = next(r for r in EventLog(self.root).event_records() if r['id']==result['envelope_id'])
        self.assertEqual('architect',report['recipient'])
        self.assertIn('TORN DOWN',report['note'])
        self.assertIn(result['started_receipt_id'],report['note'])
        self.assertEqual(map_bytes,(self.root.tenant_home / WORKSPACE_MAP_RELATIVE).read_bytes())
        CodexWaitConsentLedger(self.root).require_armed(self.binding)

    def test_completed_replay_never_consumes_later_mail(self):
        result = self.teardown()
        later = self.send('later')
        self.assertEqual(result,self.teardown())
        self.assertNotIn(later['id'],SparseCursor(self.root).acked_ids(NODE))
        self.assertEqual(2,len([r for r in EventLog(self.root).event_records() if r['kind']=='message_envelope']))

    def test_unnamed_unknown_is_reported_preserved_and_not_resampled_on_replay(self):
        activity=self.activity()
        activity['native_roots']=[str(self.home)]
        activity['native_unknown']=[{'pid':999997,'reason':'cwd_unreadable','receipt_ids':[]}]
        with mock.patch('floati.seat_teardown._inspect_activity',return_value=activity):
            result=self.service().teardown(NODE,self.home,SESSION,repo='floati',doc='report.md',idempotency_key='unknown-cutoff')
        self.assertEqual(activity,result['activity'])
        self.assertTrue(all(row['activity']==activity for row in self.rows()))
        report=next(row for row in EventLog(self.root).event_records() if row['id']==result['envelope_id'])
        self.assertIn('unnamed native UNKNOWN 1 (reasons in receipt)',report['note'])
        with mock.patch('floati.seat_teardown._inspect_activity',side_effect=AssertionError('replay sampled later processes')):
            self.assertEqual(result,self.service().teardown(NODE,self.home,SESSION,repo='floati',doc='report.md',idempotency_key='unknown-cutoff'))

    def test_receipt_named_unknown_refuses_before_pause_or_report(self):
        activity=self.activity()
        activity['native_roots']=[str(self.home)]
        activity['native_unknown']=[{'pid':999997,'reason':'cwd_unreadable',
            'receipt_ids':['seat-execution-00000000000070008000000000000001']}]
        with mock.patch('floati.seat_teardown._inspect_activity',return_value=activity),\
             mock.patch.object(WakeController,'pause',side_effect=AssertionError('named UNKNOWN reached pause')) as pause,\
             mock.patch.object(EventLog,'send',side_effect=AssertionError('named UNKNOWN reached report')) as send:
            with self.assertRaises(ProtocolRefusal) as caught:
                self.service().teardown(NODE,self.home,SESSION,repo='floati',doc='report.md',idempotency_key='named-unknown')
        self.assertEqual('seat_teardown_activity_unknown',caught.exception.code)
        self.assertIn('999997',caught.exception.detail)
        pause.assert_not_called(); send.assert_not_called()
        self.assertFalse(self.rows())

    def test_unknown_shape_cannot_enter_durable_success(self):
        from floati.records import validate_record
        from tests.schema_validation import validate_json_schema
        result=self.teardown()
        for unknown in ([{'pid':True,'reason':'cwd_unreadable','receipt_ids':[]}],
                        [{'pid':999997,'reason':'arbitrary private detail','receipt_ids':[]}],
                        [{'pid':999997,'reason':'cwd_unreadable','receipt_ids':['seat-execution-00000000000070008000000000000001']}],
                        [{'pid':999997,'reason':'cwd_unreadable','receipt_ids':[],'unowned':True}]):
            with self.subTest(unknown=unknown):
                row=json.loads(json.dumps(result)); row['activity']['native_unknown']=unknown
                with self.assertRaises(ProtocolRefusal):
                    validate_record(row,self.root.tenant_id,{'seat_teardown_receipt'},integrity=False)
                with self.assertRaises(AssertionError):
                    validate_json_schema(row,Path('schemas/v2/seat-teardown-record.schema.json'))

    def test_native_scope_must_match_invoking_uid_before_effects(self):
        """Missing, foreign, or noncanonical population testimony cannot pause/send."""
        service=self.service()
        valid={'population':'invoking_uid','uid':os.getuid()}
        bad_scopes=(None, {}, {'uid':os.getuid()}, {'population':'all_uids','uid':os.getuid()},
            dict(valid,extra=True), dict(valid,uid=os.getuid()+1), dict(valid,uid=True),
            dict(valid,uid=float(os.getuid())), dict(valid,uid=str(os.getuid())),
            dict(valid,uid=-1), dict(valid,uid=4294967296))
        for scope in ('missing', *bad_scopes):
            with self.subTest(scope=scope):
                activity=self.activity()
                if scope=='missing': activity.pop('native_scope')
                else: activity['native_scope']=scope
                with mock.patch('floati.seat_teardown._inspect_activity',return_value=activity),\
                     mock.patch.object(WakeController,'pause',side_effect=AssertionError('invalid scope reached native pause')) as pause,\
                     mock.patch.object(EventLog,'send',side_effect=AssertionError('invalid scope reached report send')) as send:
                    with self.assertRaises(ProtocolRefusal) as caught:
                        service.teardown(NODE,self.home,SESSION,repo='floati',doc='report.md',idempotency_key='bad-scope')
                self.assertEqual('seat_teardown_activity_unknown',caught.exception.code)
                self.assertTrue(caught.exception.remedy)
                pause.assert_not_called(); send.assert_not_called()
                self.assertFalse(self.rows())
                self.assertEqual('active',WakeController(self.root).status(NODE,SESSION)['state'])

    def test_report_and_every_phase_bind_observed_native_uid(self):
        result=self.teardown()
        scope={'population':'invoking_uid','uid':os.getuid()}
        self.assertTrue(all(row['activity']['native_scope']==scope for row in self.rows()))
        report=next(row for row in EventLog(self.root).event_records() if row['id']==result['envelope_id'])
        expected=('TORN DOWN '+NODE+' session '+SESSION+'; receipt '+result['started_receipt_id']+
            '; event cutoff '+str(result['event_cutoff']['count'])+
            '; native scope invoking uid '+str(scope['uid'])+'; unnamed native UNKNOWN 0 (reasons in receipt)'+
            '; other UIDs out of scope; later mail belongs to next boot')
        self.assertEqual(expected,result['report_note'])
        self.assertEqual(expected,report['note'])
        self.assertNotIn('\n',report['note'])

    def test_historical_scope_reader_and_completed_replay_do_not_resample_uid(self):
        from floati.seat_teardown import _validate_native
        result=self.teardown()
        recorded_uid=result['activity']['native_scope']['uid']
        with mock.patch('floati.seat_teardown.os.getuid',return_value=recorded_uid+1):
            _validate_native(self.root,result,complete=True)
        with mock.patch('floati.seat_teardown._inspect_activity',side_effect=AssertionError('historical replay resampled activity')):
            self.assertEqual(result,self.service().teardown(NODE,self.home,SESSION,repo='floati',doc='report.md',idempotency_key='teardown-one'))

    def test_native_scope_tampering_breaks_progress_and_report_binding(self):
        from floati.seat_teardown import completed_teardown_for_session
        result=self.teardown()
        path=self.root.resolve_relative('receipts/seat-teardown/'+NODE+'.jsonl')
        original=[json.loads(line) for line in path.read_text().splitlines()]
        for changed in ('one_phase','all_phases','all_phases_and_note'):
            with self.subTest(changed=changed):
                rows=json.loads(json.dumps(original))
                selected=rows[-1:] if changed=='one_phase' else rows
                for row in selected:
                    row['activity']['native_scope']['uid']+=1
                    if changed=='all_phases_and_note':
                        row['report_note']=row['report_note'].replace('invoking uid '+str(os.getuid()),'invoking uid '+str(os.getuid()+1))
                path.write_text(''.join(json.dumps(row)+'\n' for row in rows))
                with self.assertRaises(IntegrityFailure) as caught:
                    completed_teardown_for_session(self.root,NODE,SESSION)
                self.assertEqual('seat_teardown_progress_invalid' if changed=='one_phase' else 'seat_teardown_native_invalid',caught.exception.code)
                expected_detail={'one_phase':'coordinates or cutoff changed',
                    'all_phases':'report note differs','all_phases_and_note':'report has different native parties or payload'}
                self.assertIn(expected_detail[changed],caught.exception.detail)
        path.write_text(''.join(json.dumps(row)+'\n' for row in original))
        self.assertEqual(result,completed_teardown_for_session(self.root,NODE,SESSION))

    def test_native_scope_schema_requires_closed_population_and_bounded_uid(self):
        from floati.records import validate_record
        from tests.schema_validation import validate_json_schema
        result=self.teardown()
        schema=Path('schemas/v2/seat-teardown-record.schema.json')
        validate_json_schema(result,schema)
        valid={'population':'invoking_uid','uid':os.getuid()}
        for scope in ('missing',None,{},dict(valid,population='all_uids'),dict(valid,extra=1),
                      dict(valid,uid=True),dict(valid,uid='501'),dict(valid,uid=-1),dict(valid,uid=4294967296)):
            with self.subTest(scope=scope):
                row=json.loads(json.dumps(result))
                if scope=='missing': row['activity'].pop('native_scope')
                else: row['activity']['native_scope']=scope
                with self.assertRaises(ProtocolRefusal):
                    validate_record(row,self.root.tenant_id,{'seat_teardown_receipt'},integrity=False)
                with self.assertRaises(AssertionError): validate_json_schema(row,schema)
        # JSON Schema integers include integral JSON numbers; the durable
        # runtime contract additionally refuses noncanonical float testimony.
        row=json.loads(json.dumps(result)); row['activity']['native_scope']['uid']=float(os.getuid())
        with self.assertRaises(ProtocolRefusal):
            validate_record(row,self.root.tenant_id,{'seat_teardown_receipt'},integrity=False)
        for uid in (0,4294967295):
            row=json.loads(json.dumps(result)); row['activity']['native_scope']['uid']=uid
            validate_json_schema(row,schema)
            validate_record(row,self.root.tenant_id,{'seat_teardown_receipt'},integrity=False)

    def test_unacted_mail_refuses_then_explicit_ack_allows_teardown(self):
        message = self.send('unacted')
        with self.assertRaises(ProtocolRefusal) as caught: self.teardown()
        self.assertEqual('seat_teardown_mail_pending',caught.exception.code)
        self.assertIn(message['id'],caught.exception.detail)
        self.assertFalse(self.rows())
        EventLog(self.root).present(NODE)
        ack = SparseCursor(self.root).ack(NODE,[message['id']],acting_session_id=SESSION)
        result = self.teardown()
        ordinary=next(ref['prefix'] for ref in result['ack_cutoff_refs'] if ref['worker_session_id'] is None)
        self.assertEqual(1,ordinary['count'])
        self.assertEqual(ack['id'],ordinary['last_id'])
        saved=EventLog(self.root).event_records()[:result['event_cutoff']['count']]
        self.assertIn(message['id'],[row['id'] for row in saved])

    def test_terminal_inbound_rejection_closes_mail_without_acknowledgment(self):
        from types import SimpleNamespace
        from floati.framing import encode_frame
        from floati.inbound_hold import start_hold
        from tests.test_net_hold_inbound_1 import HOLD_LEDGER, _decision_row
        from tests.schema_validation import validate_json_schema

        hold = start_hold(self.root, NODE, "operator review", "hold")
        held = self.send("held")
        with self.assertRaises(ProtocolRefusal) as unresolved:
            self.teardown()
        self.assertEqual("seat_teardown_mail_pending", unresolved.exception.code)
        rejection = _decision_row(SimpleNamespace(root=self.root, node=NODE,
            hold_id=hold["id"], held=held), "rejected")
        ledger = self.root.tenant_home / HOLD_LEDGER / (NODE + ".jsonl")
        with ledger.open("ab") as stream:
            stream.write(encode_frame(rejection))
        result = self.teardown()
        self.assertEqual("completed", result["step"])
        validate_json_schema(result, Path("schemas/v3/seat-teardown-record.schema.json"))
        self.assertNotIn(held["id"], SparseCursor(self.root).acked_ids(NODE))
        self.assertEqual(result, self.teardown())
        original = ledger.read_bytes()
        ledger.write_bytes(original[:-len(encode_frame(rejection))])
        with self.assertRaises(IntegrityFailure):
            self.teardown()

    def test_codex_teardown_sends_report_without_relocking_its_lane(self):
        from floati import wake_hold
        from floati.ids import uuid7_hex
        from uuid import UUID

        lock = wake_hold._locked_path

        def no_wait_on_lane(*args, **kwargs):
            kwargs["timeout_seconds"] = 0
            return lock(*args, **kwargs)

        with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": str(UUID(hex=uuid7_hex()))}),\
                mock.patch.object(wake_hold, "_locked_path", side_effect=no_wait_on_lane):
            result = self.teardown()
        self.assertEqual("completed", result["step"])
        envelopes = EventLog(self.root).event_records()
        report = next(row for row in envelopes if row["id"] == result["envelope_id"])
        self.assertEqual("architect", report["recipient"])

    def test_absent_declaration_refuses_before_effects(self):
        (self.workspace/'SEAT.json').unlink()
        with self.assertRaises(ProtocolRefusal): self.teardown()
        self.assertEqual('active',WakeController(self.root).status(NODE,SESSION)['state'])

    def test_wrong_session_refuses_before_effects(self):
        with self.assertRaises(ProtocolRefusal): self.teardown(session='foreign')
        self.assertFalse(self.rows())

    def test_home_symlink_and_relative_boundary_refuse(self):
        alias = self.base/'alias'
        alias.symlink_to(self.home, target_is_directory=True)
        for case_index, home in enumerate((alias,Path('relative'))):
            with self.subTest(case=(
                "symlink-home",
                "relative-home",
            )[case_index]), self.assertRaises(ProtocolRefusal): self.teardown(home=home)
        self.assertFalse(self.rows())

    def test_malformed_declaration_refuses(self):
        (self.workspace/'SEAT.json').write_text('{broken')
        with self.assertRaises(IntegrityFailure): self.teardown()
        self.assertFalse(self.rows())

    def test_unreadable_activity_is_not_absence(self):
        service = self.service()
        with mock.patch('floati.seat_teardown._inspect_activity',side_effect=ProtocolRefusal('seat_teardown_activity_unknown','unknown')):
            with self.assertRaises(ProtocolRefusal): service.teardown(NODE,self.home,SESSION,repo='floati',doc='report.md',idempotency_key='key')
        self.assertFalse(self.rows())

    def test_wrong_declared_coordinator_refuses_before_effects(self):
        path = self.workspace/'SEAT.json'
        value=json.loads(path.read_text()); value['coordinator']='different'; path.write_text(json.dumps(value))
        with self.assertRaises(ProtocolRefusal): self.teardown()
        self.assertFalse(self.rows())

    def test_key_conflict_binds_whole_coordinate(self):
        self.teardown()
        for changes in ({'session':'other'},{'doc':'other.md'},{'repo':'other'}):
            with self.subTest(changes=changes), self.assertRaises(ProtocolRefusal) as caught: self.teardown(**changes)
            self.assertEqual('seat_teardown_idempotency_conflict',caught.exception.code)

    def test_pause_failure_does_not_publish_report(self):
        self.service()
        with mock.patch.object(WakeController,'_write_marker',side_effect=OSError('crash before marker')):
            with self.assertRaises(OSError): self.teardown()
        self.assertFalse(EventLog(self.root).event_records())
        self.assertEqual('completed',self.teardown()['step'])

    def test_crash_after_pause_marker_reconciles_exact_reserved_receipt(self):
        self.service()
        with mock.patch.object(WakeController,'_append',side_effect=OSError('crash after marker')):
            with self.assertRaises(OSError): self.teardown()
        reserved = self.rows()[0]['pause_intent']
        result=self.teardown()
        self.assertEqual(reserved['id'],result['pause_receipt_id'])

    def test_crash_after_each_phase_recovers_without_duplicate_report(self):
        from_phase=('paused','reported','completed')
        for phase in from_phase:
            with self.subTest(phase=phase):
                key='fault-'+phase
                service=self.service(); real=type(service)._append
                def append(owner,relative,row):
                    if row['step']==phase: raise OSError('scratch interruption')
                    return real(owner,relative,row)
                # Each control uses a new exact session and held native claim.
                session=SESSION+'-'+phase
                CodexWaitSessionLedger(self.root).arm(self.binding,self.consent,session,idempotency_key='claim-'+phase,take_over=True)
                with mock.patch.object(type(service),'_append',append):
                    with self.assertRaises(OSError): self.teardown(session=session,idempotency_key=key)
                result=self.teardown(session=session,idempotency_key=key)
                self.assertEqual('completed',result['step'])
                reports=[r for r in EventLog(self.root).event_records() if r['kind']=='message_envelope' and r['id']==result['envelope_id']]
                self.assertEqual(1,len(reports))

    def test_superseding_pause_is_preserved(self):
        service=self.service(); real=type(service)._append
        def append(owner,relative,row):
            if row['step']=='paused': raise OSError('interrupted')
            return real(owner,relative,row)
        with mock.patch.object(type(service),'_append',append):
            with self.assertRaises(OSError): self.teardown()
        controller=WakeController(self.root)
        controller.resume(NODE,SESSION,idempotency_key='resume')
        foreign=controller.pause(NODE,SESSION,idempotency_key='foreign-pause')['receipt']
        marker=controller._marker(NODE,controller._identity(NODE,SESSION)[2])
        before=marker.read_bytes()
        with self.assertRaises(ProtocolRefusal): self.teardown()
        self.assertEqual(before,marker.read_bytes())
        self.assertEqual(foreign['id'],json.loads(before)['receipt_id'])

    def test_completed_reader_matches_current_pause_and_new_session_is_independent(self):
        self.service()
        from floati.seat_teardown import completed_teardown_for_session
        result=self.teardown()
        self.assertEqual(result,completed_teardown_for_session(self.root,NODE,SESSION))
        self.assertIsNone(completed_teardown_for_session(self.root,NODE,'successor'))
        CodexWaitSessionLedger(self.root).arm(self.binding,self.consent,'successor',idempotency_key='successor',take_over=True)
        self.assertEqual('active',WakeController(self.root).status(NODE,'successor')['state'])

    def test_record_schema_rejects_missing_native_reference(self):
        self.service()
        from floati.records import validate_record
        from tests.schema_validation import validate_json_schema
        result=self.teardown()
        for row in self.rows(): validate_json_schema(row,Path('schemas/v2/seat-teardown-record.schema.json'))
        row=dict(result,pause_receipt_id=None)
        with self.assertRaises(ProtocolRefusal): validate_record(row,self.root.tenant_id,{'seat_teardown_receipt'},integrity=False)
        with self.assertRaises(AssertionError): validate_json_schema(row,Path('schemas/v2/seat-teardown-record.schema.json'))

    def test_cli_registers_closed_explicit_coordinate(self):
        from floati.cli import main
        self.service()
        output=io.StringIO()
        with contextlib.redirect_stdout(output):
            code=main(['seat','teardown','--root',str(self.root.path),'--as',NODE,'--home',str(self.home),
                '--session',SESSION,'--repo','floati','--doc','report.md','--idempotency-key','cli','--to','foreign'])
        self.assertEqual(20,code)
        self.assertEqual('arguments_invalid',json.loads(output.getvalue())['evidence']['code'])

    def test_invalid_report_selector_refuses_without_pause(self):
        for changes in ({'repo':'bad repository'},{'doc':'../report.md'},{'doc':'missing.md'}):
            with self.subTest(changes=changes), self.assertRaises(ProtocolRefusal): self.teardown(**changes)
            self.assertEqual('active',WakeController(self.root).status(NODE,SESSION)['state'])
            self.assertFalse(self.rows())

    def test_worker_session_mail_is_pending_without_automatic_ack(self):
        message=EventLog(self.root).send('architect',NODE,'floati',self.sha,'report.md','worker action',
            idempotency_key='worker-mail',worker_session_id='native-worker')['message']
        with self.assertRaises(ProtocolRefusal) as caught: self.teardown()
        self.assertEqual('seat_teardown_mail_pending',caught.exception.code)
        self.assertIn(message['id'],caught.exception.detail)
        EventLog(self.root).present(NODE,worker_session_id='native-worker')
        ack=SparseCursor(self.root).ack(NODE,[message['id']],acting_session_id=SESSION,worker_session_id='native-worker')
        result=self.teardown()
        worker=next(ref['prefix'] for ref in result['ack_cutoff_refs'] if ref['worker_session_id']=='native-worker')
        self.assertEqual(1,worker['count'])
        self.assertEqual(ack['id'],worker['last_id'])

    def test_report_send_failure_resumes_while_paused(self):
        self.service()
        with mock.patch.object(EventLog,'send',side_effect=OSError('report unavailable')):
            with self.assertRaises(OSError): self.teardown()
        self.assertEqual('paused',WakeController(self.root).status(NODE,SESSION)['state'])
        self.assertEqual('paused',self.rows()[-1]['step'])
        scope=self.rows()[-1]['activity']['native_scope']
        with mock.patch('floati.seat_teardown._inspect_activity',side_effect=AssertionError('recovery resampled native population')):
            result=self.service().teardown(NODE,self.home,SESSION,repo='floati',doc='report.md',idempotency_key='teardown-one')
        self.assertEqual('completed',result['step'])
        self.assertEqual(scope,result['activity']['native_scope'])

    def test_missing_marker_after_native_pause_refuses_retry(self):
        service=self.service(); real=type(service)._append
        def append(owner,relative,row):
            if row['step']=='paused': raise OSError('interruption')
            return real(owner,relative,row)
        with mock.patch.object(type(service),'_append',append):
            with self.assertRaises(OSError): self.teardown()
        WakeController(self.root).resume(NODE,SESSION,idempotency_key='deliberate-resume')
        with self.assertRaises(ProtocolRefusal): self.teardown()
        self.assertEqual('active',WakeController(self.root).status(NODE,SESSION)['state'])
        self.assertFalse(EventLog(self.root).event_records())

    def test_completed_replay_is_historical_after_successor_claim(self):
        result=self.teardown()
        CodexWaitSessionLedger(self.root).arm(self.binding,self.consent,'successor',idempotency_key='next',take_over=True)
        WakeController(self.root).resume(NODE,SESSION,idempotency_key='old-session-resume')
        self.assertEqual(result,self.teardown())
        self.assertEqual('active',WakeController(self.root).status(NODE,'successor')['state'])

    def test_linked_checkout_cannot_impersonate_primary_home(self):
        primary=self.home/'old-primary'
        self.workspace.rename(primary)
        _git(['worktree','add','-q','--detach',str(self.workspace),'HEAD'],primary)
        with self.assertRaises(ProtocolRefusal) as caught: self.teardown()
        self.assertEqual('seat_teardown_primary_invalid',caught.exception.code)

    def test_outstanding_governed_work_refuses(self):
        from floati.work import WorkLog
        pending=WorkLog(self.root).add('unfinished scratch work',NODE,[])
        with self.assertRaises(ProtocolRefusal) as caught: self.teardown()
        self.assertEqual('seat_teardown_work_pending',caught.exception.code)
        self.assertIn(pending['id'],caught.exception.detail)

    def test_missing_native_consent_cannot_support_completed_reader(self):
        self.service()
        from floati.seat_teardown import completed_teardown_for_session
        self.teardown()
        path=self.root.resolve_relative(CodexWaitConsentLedger._relative(NODE))
        path.unlink()
        with self.assertRaises(IntegrityFailure): completed_teardown_for_session(self.root,NODE,SESSION)

    def test_tampered_report_recipient_refuses_completed_replay(self):
        self.teardown()
        path=self.root.resolve_relative('receipts/seat-teardown/'+NODE+'.jsonl')
        rows=[json.loads(line) for line in path.read_text().splitlines()]
        for row in rows: row['envelope_recipient']='foreign'
        path.write_text(''.join(json.dumps(row)+'\n' for row in rows))
        with self.assertRaises(IntegrityFailure): self.teardown()

    def test_malformed_teardown_ledger_is_typed_failure(self):
        self.teardown()
        path=self.root.resolve_relative('receipts/seat-teardown/'+NODE+'.jsonl')
        path.write_text('{broken\n')
        with self.assertRaises(IntegrityFailure): self.teardown()

    def test_cli_success_reports_explicit_session(self):
        from floati.cli import main
        self.service()
        output=io.StringIO()
        activity=self.activity()
        with mock.patch('floati.seat_teardown._inspect_activity',return_value=activity),contextlib.redirect_stdout(output):
            code=main(['seat','teardown','--root',str(self.root.path),'--as',NODE,'--home',str(self.home),
                '--session',SESSION,'--repo','floati','--doc','report.md','--idempotency-key','cli-success'])
        self.assertEqual(0,code,output.getvalue())
        self.assertEqual(SESSION,json.loads(output.getvalue())['evidence']['acting_session_id'])

    def test_report_append_then_failure_reuses_native_envelope(self):
        self.service(); original=EventLog.send
        def send(log,*args,**kwargs):
            original(log,*args,**kwargs)
            raise OSError('after envelope append')
        with mock.patch.object(EventLog,'send',send):
            with self.assertRaises(OSError): self.teardown()
        before=EventLog(self.root).event_records()
        reports=[r for r in before if r['kind']=='message_envelope']
        readiness=[r for r in before if r['kind']=='send_readiness_observation']
        self.assertEqual(1,len(reports))
        self.assertEqual([reports[0]['id']],[r['envelope_id'] for r in readiness])
        result=self.teardown()
        self.assertEqual(reports[0]['id'],result['envelope_id'])
        self.assertEqual(before,EventLog(self.root).event_records())

    def test_unrelated_native_session_effect_refuses_no_effect_retry(self):
        self.service()
        with mock.patch.object(WakeController,'_write_marker',side_effect=OSError('before effect')):
            with self.assertRaises(OSError): self.teardown()
        foreign=WakeController(self.root).pause(NODE,'different-session',idempotency_key='other-native')['receipt']
        controller=WakeController(self.root)
        foreign_marker=controller._marker(NODE,controller._identity(NODE,'different-session')[2])
        before=foreign_marker.read_bytes()
        with self.assertRaises(ProtocolRefusal): self.teardown()
        self.assertEqual(before,foreign_marker.read_bytes())
        self.assertEqual('active',controller.status(NODE,SESSION)['state'])
        self.assertEqual(foreign['id'],json.loads(before)['receipt_id'])

    def test_unreadable_teardown_ledger_cannot_enable_completion(self):
        self.service()
        original=read_records_snapshot
        def read(root,relative,**kwargs):
            if str(relative).startswith('receipts/seat-teardown/'):
                raise IntegrityFailure('jsonl_unreadable','scratch unreadable ledger')
            return original(root,relative,**kwargs)
        with mock.patch('floati.seat_teardown.read_records_snapshot',side_effect=read):
            with self.assertRaises(IntegrityFailure): self.teardown()
        self.assertEqual('active',WakeController(self.root).status(NODE,SESSION)['state'])

    def test_malformed_activity_cannot_claim_completed_teardown(self):
        import os
        service=self.service()
        activity=self.activity()
        activity['worktrees'][0]['tracked_changes']=[' M report.md']
        with mock.patch('floati.seat_teardown._inspect_activity',return_value=activity):
            with self.assertRaises(ProtocolRefusal): service.teardown(NODE,self.home,SESSION,repo='floati',doc='report.md',idempotency_key='false-activity')
        self.assertEqual('active',WakeController(self.root).status(NODE,SESSION)['state'])

    def test_dangling_exact_pause_symlink_refuses_before_effects(self):
        controller=WakeController(self.root)
        digest=controller._identity(NODE,SESSION)[2]
        lexical=self.root.tenant_home/'state/wake-control'/NODE/(digest+'.json')
        lexical.parent.mkdir(parents=True,exist_ok=True)
        target=lexical.parent/'missing-native-marker.json'
        lexical.symlink_to(target)
        with self.assertRaises(IntegrityFailure): self.teardown()
        self.assertTrue(lexical.is_symlink())
        self.assertFalse(target.exists())
        self.assertFalse(EventLog(self.root).event_records())

    def test_no_effect_retry_preserves_foreign_marker_symlink(self):
        self.service()
        with mock.patch.object(WakeController,'_write_marker',side_effect=OSError('before marker')):
            with self.assertRaises(OSError): self.teardown()
        controller=WakeController(self.root)
        controller.pause(NODE,'foreign-session',idempotency_key='foreign-native')
        foreign=controller._marker(NODE,controller._identity(NODE,'foreign-session')[2])
        before=foreign.read_bytes()
        lexical=self.root.tenant_home/'state/wake-control'/NODE/(controller._identity(NODE,SESSION)[2]+'.json')
        lexical.symlink_to(foreign)
        with self.assertRaises(IntegrityFailure): self.teardown()
        self.assertTrue(lexical.is_symlink())
        self.assertEqual(before,foreign.read_bytes())

    def test_completed_reader_refuses_symlink_replacement(self):
        self.service()
        from floati.seat_teardown import completed_teardown_for_session
        self.teardown()
        controller=WakeController(self.root)
        lexical=controller._marker(NODE,controller._identity(NODE,SESSION)[2])
        saved=lexical.with_suffix('.saved')
        lexical.rename(saved)
        lexical.symlink_to(saved)
        with self.assertRaises(IntegrityFailure): completed_teardown_for_session(self.root,NODE,SESSION)
        self.assertTrue(lexical.is_symlink())

    def test_real_union_git_observer_completes_native_transaction(self):
        """Real workspace inventory/Git/ledgers; only native process table is isolated."""
        service=self.service()
        linked=self.home/'linked'
        _git(['worktree','add','-q','--detach',str(linked),'HEAD'],self.workspace)
        with mock.patch('floati.seat_teardown_activity._native_snapshot',return_value=([],[os.getpid()],[])):
            result=service.teardown(NODE,self.home,SESSION,repo='floati',doc='report.md',idempotency_key='real-union')
        self.assertEqual(sorted([str(self.workspace),str(linked)]),result['activity']['workspace_set'])
        self.assertEqual(result['activity']['workspace_set'],[row['workspace'] for row in result['activity']['worktrees']])
        from floati.seat_teardown import completed_teardown_for_session
        self.assertEqual(result,completed_teardown_for_session(self.root,NODE,SESSION))

    def test_real_union_git_observer_refuses_dirty_extra_clone(self):
        service=self.service()
        other=self.home/'other-clone'
        _git(['clone','-q',str(self.workspace),str(other)],self.base)
        (other/'report.md').write_text('uncommitted additional checkout work\n')
        with mock.patch('floati.seat_teardown_activity._native_snapshot',return_value=([],[os.getpid()],[])):
            with self.assertRaises(ProtocolRefusal) as caught:
                service.teardown(NODE,self.home,SESSION,repo='floati',doc='report.md',idempotency_key='real-dirty')
        self.assertEqual('seat_teardown_activity_in_flight',caught.exception.code)
        self.assertIn(str(other),caught.exception.detail)
        self.assertFalse(self.rows())
        self.assertEqual('active',WakeController(self.root).status(NODE,SESSION)['state'])

    def test_native_child_teardown_cursor_and_successor_journey(self):
        """Supported-host control: no service or native-process observer seam."""
        service=self.service()
        child=subprocess.Popen([sys.executable,'-u','-c','import sys; print("ready", flush=True); sys.stdin.read()'],
            cwd=str(self.home),stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        try:
            self.assertEqual(b'ready\n',child.stdout.readline())
            with self.assertRaises(ProtocolRefusal) as caught:
                service.teardown(NODE,self.home,SESSION,repo='floati',doc='report.md',idempotency_key='native-journey')
        finally:
            # EOF ends this owned scratch child; no signal or sibling process control.
            child.communicate(timeout=5)
        self.assertEqual('seat_teardown_activity_in_flight',caught.exception.code,caught.exception.detail)
        self.assertIn('PID '+str(child.pid),caught.exception.detail)
        self.assertFalse(self.rows())
        self.assertFalse(EventLog(self.root).event_records())
        self.assertEqual('active',WakeController(self.root).status(NODE,SESSION)['state'])
        result=service.teardown(NODE,self.home,SESSION,repo='floati',doc='report.md',idempotency_key='native-journey')
        from floati.cursor_wait import run_cursor_stop_wait
        output=io.StringIO()
        # The hook's current caller is the scratch workspace. Keep native
        # process observation real while supplying that harness boundary.
        with mock.patch('floati.cursor_wait.Path.cwd',return_value=self.workspace):
            code=run_cursor_stop_wait(root=self.root.path,node=NODE,runtime=self.workspace,deadline_seconds=1,
                poll_seconds=.1,hook_timeout_seconds=10,payload={'conversation_id':SESSION,'status':'error','loop_count':0},
                stdout=output,stderr=io.StringIO())
        self.assertEqual(0,code)
        self.assertEqual({},json.loads(output.getvalue()))
        journal=self.root.path/'state/cursor-wait'/NODE/'journal.jsonl'
        testimony=json.loads(journal.read_text().splitlines()[-1])
        self.assertEqual('exit_empty_teardown',testimony['event'])
        self.assertEqual(result['id'],testimony['teardown_receipt_id'])
        later=self.send('successor-arrival')
        self.assertNotIn(later['id'],SparseCursor(self.root).acked_ids(NODE))
        from floati.seat_board import SeatBoard
        successor=SeatBoard(self.root).board(NODE,self.workspace,'successor-native',idempotency_key='successor-native',take_over=True)
        self.assertEqual([later['id']],successor['item_ids'])
        self.assertEqual('active',WakeController(self.root).status(NODE,'successor-native')['state'])

    def test_completed_reader_rejects_malformed_governed_work_testimony(self):
        self.service()
        from floati.seat_teardown import completed_teardown_for_session
        self.teardown()
        path=self.root.resolve_relative('receipts/seat-teardown/'+NODE+'.jsonl')
        original=[json.loads(line) for line in path.read_text().splitlines()]
        for bad in ([{}],['duplicated','duplicated'],['unsafe\x00id'],['x'*129],['missing-native-session']):
            with self.subTest(bad=bad):
                rows=json.loads(json.dumps(original))
                for row in rows: row['work_evidence']['worker_session_ids']=bad
                path.write_text(''.join(json.dumps(row)+'\n' for row in rows))
                with self.assertRaises(IntegrityFailure): completed_teardown_for_session(self.root,NODE,SESSION)

    def test_looped_home_ancestor_is_typed_before_effects(self):
        loop=self.base/'loop'
        loop.symlink_to('loop',target_is_directory=True)
        with self.assertRaises(ProtocolRefusal) as caught: self.teardown(home=loop/'seat')
        self.assertEqual('seat_teardown_home_invalid',caught.exception.code)
        self.assertFalse(self.rows())
        self.assertEqual('active',WakeController(self.root).status(NODE,SESSION)['state'])

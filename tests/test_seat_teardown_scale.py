"""Scale teardown against canonical native history and scratch primary Git.

The ordinary service cases isolate only the activity observer through the
existing named fixture seam. Native companion cases leave that observer intact.
Bulk history is constructed as valid canonical frames in one linear write;
the real ledger readers and performing service still validate that history.
"""
import copy
import json
import os
import signal
import time
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from floati import jsonl
from floati.cursor import SparseCursor
from floati.errors import IntegrityFailure, ProtocolRefusal
from floati.events import EVENT_KINDS, EventLog
from floati.framing import decode_frames, encode_frame
from floati.ids import uuid7_hex
from floati.records import validate_record, validate_unknown_record
from floati.seat_teardown import completed_teardown_for_session
from floati.wake_control import WakeController
from scripts.parallel_processes import ProcessQuery
from tests import test_seat_teardown as fixture
from tests.ledger_limit_patch import patched_ledger_limit


# Automatic epoch maintenance ages the first event against epoch_rotation's
# clock. Keep that clock pinned, and keep this history one hour behind it,
# inside the seven-day roll window.
PINNED_EPOCH_NOW = datetime(2026, 10, 16, 12, 0, tzinfo=timezone.utc)
HISTORY_TIMESTAMP = (PINNED_EPOCH_NOW - timedelta(hours=1)).isoformat(
    timespec="milliseconds").replace("+00:00", "Z")

BOUNDARY_CASES = (
    'test_initial_hundred_thousand_refuses_capacity_before_any_teardown_effect',
    'test_future_kind_frame_counts_toward_physical_capacity',
    'test_native_report_batch_counts_future_frames_atomically',
    'test_single_native_retraction_counts_future_frames_atomically',
    'test_existing_overfull_history_names_preservation_remedy_without_effects',
    'test_capacity_filled_after_preflight_preserves_exact_paused_recovery',
)


class _PinnedEpochClock(datetime):
    """The epoch observer's clock. Its instances must satisfy that module's isinstance check."""

    @classmethod
    def now(cls, tz=None):
        moment = PINNED_EPOCH_NOW if tz is None else PINNED_EPOCH_NOW.astimezone(tz)
        return cls(moment.year, moment.month, moment.day, moment.hour, moment.minute,
            moment.second, moment.microsecond, tzinfo=moment.tzinfo)

    @classmethod
    def fromisoformat(cls, value):
        parsed = datetime.fromisoformat(value)
        return cls(parsed.year, parsed.month, parsed.day, parsed.hour, parsed.minute,
            parsed.second, parsed.microsecond, tzinfo=parsed.tzinfo)


def _child_pids(pid):
    """Direct children, using the parallel runner's cross-platform query."""

    return ProcessQuery().children(pid)


def _reap_direct_children():
    """SIGKILL every process this case still owns.

    The native observer test has left a direct child of the worker alive until
    the worker exited. A snapshot taken before setUp must not protect that
    child: later cases in the same worker were skipping it. Grandchildren are
    collected before the parents are signaled, then signaled by pid.
    """

    me = os.getpid()
    direct = _child_pids(me)
    owned = list(direct)
    for pid in direct:
        owned.extend(_child_pids(pid))
    for pid in owned:
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
    for _ in range(10):
        pending = False
        for pid in direct:
            try:
                waited, _ = os.waitpid(pid, os.WNOHANG)
            except OSError:
                continue
            if waited == 0:
                pending = True
        if not pending:
            break
        time.sleep(0.01)


class SeatTeardownScaleTests(unittest.TestCase):
    boundary_limit = 32

    def setUp(self):
        # Added first so it runs after tearDown and the fixture cleanups.
        self.addCleanup(_reap_direct_children)
        if self._testMethodName in BOUNDARY_CASES and self.boundary_limit is not None:
            limits = patched_ledger_limit(self.boundary_limit)
            limits.__enter__()
            self.addCleanup(limits.__exit__, None, None, None)
        patcher = mock.patch("floati.epoch_rotation.datetime", _PinnedEpochClock)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.f = fixture.SeatTeardownTests()
        self.addCleanup(self.f.doCleanups)
        self.f.setUp()
        self.root = self.f.root
        self.node, self.session = fixture.NODE, fixture.SESSION

    def tearDown(self):
        _reap_direct_children()

    @staticmethod
    def identity(prefix, ordinal):
        return prefix + uuid7_hex(timestamp_ms=1, random_bits=ordinal)

    def envelope(self, ordinal, *, recipient='architect', scope=None):
        row = dict(schema_version=0, kind='message_envelope',
            id=self.identity('msg-', ordinal), tenant_id=self.root.tenant_id,
            timestamp=HISTORY_TIMESTAMP, sender='architect',
            recipient=recipient, repo='floati', sha=self.f.sha, doc='report.md',
            note='scratch historical evidence', idempotency_key='history-' + str(ordinal))
        if scope is not None:
            row['worker_session_id'] = scope
        return row

    def write_records(self, path, records, kinds):
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('wb') as output:
            for row in records:
                output.write(encode_frame(validate_record(row, self.root.tenant_id,
                    kinds, integrity=False)))

    def seed_history(self, count):
        # The native service validates every frame. The fixture varies just two
        # ASCII cells of a schema-validated, canonically encoded envelope.
        template = self.envelope(0)
        encoded = encode_frame(validate_record(template, self.root.tenant_id, EVENT_KINDS, integrity=False))
        before_id, middle = encoded.split(template['id'].encode('ascii'))
        between, after_key = middle.split(template['idempotency_key'].encode('ascii'))
        with (self.root.path / 'events.jsonl').open('wb') as output:
            for index in range(count):
                output.write(b''.join((before_id, self.identity('msg-', index).encode('ascii'), between,
                                       b'history-', str(index).encode('ascii'), after_key)))

    def perform(self, *, native=False):
        if not native:
            return self.f.teardown()
        return self.f.service().teardown(self.node, self.f.home, self.session,
            repo='floati', doc='report.md', idempotency_key='teardown-one')

    def assert_prefix_shape(self, value):
        self.assertEqual({'count', 'last_id', 'digest'}, set(value))
        self.assertIs(type(value['count']), int)
        self.assertGreaterEqual(value['count'], 0)
        self.assertRegex(value['digest'], r'^[0-9a-f]{64}$')
        if value['count'] == 0:
            self.assertIsNone(value['last_id'])
        else:
            self.assertIsInstance(value['last_id'], str)

    def assert_compact_completion(self, result, count):
        self.assertEqual('completed', result['step'])
        rows = self.f.rows()
        self.assertEqual(['started', 'paused', 'reported', 'completed'],
            [row['step'] for row in rows])
        for row in rows:
            self.assertEqual(2, row['schema_version'])
            self.assertLessEqual(len(encode_frame(row)), 65536)
            self.assertFalse({'event_cutoff_ids', 'wake_cutoff_ids',
                'ack_receipt_ids'} & set(row))
            self.assert_prefix_shape(row['event_cutoff'])
            self.assert_prefix_shape(row['wake_cutoff'])
            self.assertEqual(count, row['event_cutoff']['count'])
            self.assertEqual(len(row['ack_cutoff_refs']), row['ack_scope_count'])
            for scope in row['ack_cutoff_refs']:
                self.assertEqual({'worker_session_id', 'prefix'}, set(scope))
                self.assert_prefix_shape(scope['prefix'])
        for field in ('event_cutoff', 'wake_cutoff', 'ack_cutoff_refs', 'ack_scope_count'):
            self.assertTrue(all(row[field] == result[field] for row in rows))
        self.assertEqual(result, self.perform())
        self.assertEqual(result, completed_teardown_for_session(
            self.root, self.node, self.session))
        print(json.dumps({'control': 'F2-SEAT-TEARDOWN-SCALE-1',
            'observer': 'native' if isinstance(self, SeatTeardownNativeScaleTests)
                else 'isolated_activity_service',
            'event_prefix_count': count,
            'phase_bytes': [len(encode_frame(row)) for row in rows],
            'receipt_limit_bytes': 65536,
            'evidence_kind': 'scratch_correctness'}, sort_keys=True), flush=True)

    def test_empty_prefixes_are_closed_and_separate_native_ledger_domains(self):
        result = self.perform()
        self.assert_compact_completion(result, 0)
        self.assertEqual(0, result['wake_cutoff']['count'])
        self.assertEqual(1, result['ack_scope_count'])
        ordinary = result['ack_cutoff_refs'][0]
        self.assertIsNone(ordinary['worker_session_id'])
        self.assertEqual(0, ordinary['prefix']['count'])
        self.assertEqual(3, len({result['event_cutoff']['digest'],
            result['wake_cutoff']['digest'], ordinary['prefix']['digest']}))

    def test_certified_teardown_and_replay_read_less_than_one_unrelated_history(self):
        from floati import cut_prefix, inbox_prefix, send_prefix
        from floati.seat_teardown_activity import _worker_pid_receipts
        from floati.seat_teardown_prefix import prefix_proof
        from tests.bus_scale_io import IOProbe

        self.seed_history(31)
        log = EventLog(self.root)
        self.assertEqual(b''.join(encode_frame(validate_record(self.envelope(index),
            self.root.tenant_id, EVENT_KINDS, integrity=False)) for index in range(31)),
            log.path.read_bytes())
        expected = prefix_proof(log.event_records(), log.relative_path)
        history_bytes = log.path.stat().st_size
        # Exercise the indexed writer with exactly two slots left, across an
        # ID-block boundary; the native companion uses the real 100,000 limit.
        limit = expected['count'] + 2
        with patched_ledger_limit(limit):
            for bank in (send_prefix, inbox_prefix, cut_prefix):
                self.assertIsNotNone(bank.warm(self.root))
            result = self.perform()
            with IOProbe(self.root.tenant_home) as probe:
                self.assertEqual({}, _worker_pid_receipts(self.root, self.node))
                self.assertEqual(result, completed_teardown_for_session(self.root, self.node, self.session))
        probe.assert_complete()
        self.assertEqual(expected, result['event_cutoff'])
        self.assertEqual('completed', result['step'])
        self.assertLess(probe.read_bytes_by_path.get('events.jsonl', 0), history_bytes)

    def assert_no_effect_refusal(self, code):
        before_events = (self.root.path / 'events.jsonl').read_bytes()
        controller = WakeController(self.root)
        marker = controller._marker(self.node, controller._identity(self.node, self.session)[2])
        wake_path = self.root.resolve_relative(controller._ledger(self.node))
        before_wake = wake_path.read_bytes() if wake_path.exists() else None
        with self.assertRaises(ProtocolRefusal) as caught:
            self.perform()
        self.assertEqual(code, caught.exception.code)
        remedy = json.dumps(caught.exception.remedy).lower()
        self.assertRegex(remedy, r'epoch.*roll|roll.*epoch')
        self.assertIn('preserve', remedy)
        self.assertEqual([], self.f.rows())
        self.assertFalse(marker.exists() or marker.is_symlink())
        self.assertEqual(before_events, (self.root.path / 'events.jsonl').read_bytes())
        self.assertEqual(before_wake, wake_path.read_bytes() if wake_path.exists() else None)

    def test_initial_hundred_thousand_refuses_capacity_before_any_teardown_effect(self):
        for remaining in (0, 1):
            with self.subTest(remaining=remaining):
                self.seed_history(jsonl.MAX_LEDGER_RECORDS - remaining)
                self.assert_no_effect_refusal('seat_teardown_capacity')

    def test_future_kind_frame_counts_toward_physical_capacity(self):
        self.seed_history(jsonl.MAX_LEDGER_RECORDS - 1)
        future = dict(schema_version=3, kind='future_scale_padding',
            id=self.identity('future-', 1), tenant_id=self.root.tenant_id,
            timestamp=HISTORY_TIMESTAMP)
        with (self.root.path / 'events.jsonl').open('ab') as output:
            output.write(encode_frame(validate_unknown_record(future, self.root.tenant_id)))
        self.assert_no_effect_refusal('seat_teardown_capacity')

    def test_native_report_batch_counts_future_frames_atomically(self):
        self.seed_history(jsonl.MAX_LEDGER_RECORDS - 2)
        future = dict(schema_version=3, kind='future_scale_padding',
            id=self.identity('future-', 1), tenant_id=self.root.tenant_id,
            timestamp=HISTORY_TIMESTAMP)
        path = self.root.path / 'events.jsonl'
        with path.open('ab') as output:
            output.write(encode_frame(validate_unknown_record(future, self.root.tenant_id)))
        before = path.read_bytes()
        with self.assertRaises(ProtocolRefusal) as caught:
            EventLog(self.root).send(self.node, 'architect', 'floati', self.f.sha,
                'report.md', 'scratch capacity race', idempotency_key='atomic-capacity')
        self.assertEqual('ledger_record_limit', caught.exception.code)
        self.assertEqual(before, path.read_bytes())

    def test_single_native_retraction_counts_future_frames_atomically(self):
        self.write_records(self.root.path / 'events.jsonl',
            (self.envelope(index, scope='scratch-worker' if index == 0 else None)
                for index in range(jsonl.MAX_LEDGER_RECORDS - 1)), EVENT_KINDS)
        future = dict(schema_version=3, kind='future_scale_padding',
            id=self.identity('future-', 1), tenant_id=self.root.tenant_id,
            timestamp=HISTORY_TIMESTAMP)
        path = self.root.path / 'events.jsonl'
        with path.open('ab') as output:
            output.write(encode_frame(validate_unknown_record(future, self.root.tenant_id)))
        before = path.read_bytes()
        with self.assertRaises(ProtocolRefusal) as caught:
            EventLog(self.root).retract(self.envelope(0)['id'],
                worker_session_id='scratch-worker', reason='stale_recipient', author='architect')
        self.assertEqual('ledger_record_limit', caught.exception.code)
        self.assertEqual(before, path.read_bytes())

    def test_existing_overfull_history_names_preservation_remedy_without_effects(self):
        self.seed_history(jsonl.MAX_LEDGER_RECORDS + 1)
        self.assert_no_effect_refusal('seat_teardown_capacity')

    def test_capacity_filled_after_preflight_preserves_exact_paused_recovery(self):
        self.seed_history(jsonl.MAX_LEDGER_RECORDS - 2)
        service = self.f.service()
        original = type(service)._append
        path = self.root.path / 'events.jsonl'

        def append(instance, relative, row):
            original(instance, relative, row)
            if row['step'] == 'paused':
                with path.open('ab') as output:
                    for index in range(2):
                        future = dict(schema_version=3, kind='future_scale_padding',
                            id=self.identity('future-', index), tenant_id=self.root.tenant_id,
                            timestamp=HISTORY_TIMESTAMP)
                        output.write(encode_frame(validate_unknown_record(future, self.root.tenant_id)))

        with mock.patch.object(type(service), '_append', append):
            with self.assertRaises(ProtocolRefusal) as caught:
                self.perform()
        self.assertEqual('seat_teardown_capacity', caught.exception.code)
        self.assertIn('epoch roll', json.dumps(caught.exception.remedy).lower())
        self.assertEqual(['started', 'paused'], [row['step'] for row in self.f.rows()])
        paused = self.f.rows()[-1]
        controller = WakeController(self.root)
        marker = controller._marker(self.node, controller._identity(self.node, self.session)[2])
        before_marker = marker.read_bytes()
        self.assertEqual(paused['pause_intent']['id'], json.loads(before_marker)['receipt_id'])
        before_events = path.read_bytes()
        before_rows = list(self.f.rows())
        with self.assertRaises(ProtocolRefusal) as replay:
            self.perform()
        self.assertEqual('seat_teardown_capacity', replay.exception.code)
        self.assertEqual(before_events, path.read_bytes())
        self.assertEqual(before_marker, marker.read_bytes())
        self.assertEqual(before_rows, self.f.rows())

    def test_scope_inventory_overflow_names_epoch_roll_before_any_effect(self):
        def history():
            for index in range(200):
                scope = ('worker-' + str(index) + '-').ljust(512, 'x')
                envelope = self.envelope(index, recipient=self.node, scope=scope)
                yield envelope
                yield dict(schema_version=0, kind='message_retracted',
                    id=self.identity('ret-', index), tenant_id=self.root.tenant_id,
                    timestamp=HISTORY_TIMESTAMP,
                    retracted_message_id=envelope['id'], worker_session_id=scope,
                    reason='stale_recipient', author='architect')
        self.write_records(self.root.path / 'events.jsonl', history(), EVENT_KINDS)
        self.assert_no_effect_refusal('seat_teardown_record_too_large')

    def seed_acted_mail(self, key, *, scope=None):
        args = {} if scope is None else {'worker_session_id': scope}
        message = EventLog(self.root).send('architect', self.node, 'floati',
            self.f.sha, 'report.md', 'scratch acted mail', idempotency_key=key,
            **args)['message']
        EventLog(self.root).present(self.node, **args)
        SparseCursor(self.root).ack(self.node, [message['id']],
            acting_session_id=self.session, **args)
        return message

    def test_non_native_ten_thousand_history_completes_and_replays_saved_prefix(self):
        """Non-native completion compacts a history whose IDs exceed one row.

        The historical test ID stays stable. Two thousand canonical IDs alone
        exceed 64 KiB; the native ceiling case owns the 100 000-frame boundary,
        and the next test owns saved-prefix replay after later mail and ACKs.
        """
        history = 2000
        self.assertGreater(sum(len(self.envelope(index)['id'].encode('utf-8'))
            for index in range(history)), 65536)
        self.seed_history(history)
        result = self.perform()
        self.assert_compact_completion(result, history)
        # The removed ceiling case required 99 998 history frames to become
        # 100 000 physical frames: completion appends that report pair.
        report_pair = 100000 - 99998
        self.assertEqual(
            history + report_pair,
            len(decode_frames((self.root.path / 'events.jsonl').read_bytes())),
        )

    def test_replay_preserves_saved_prefix_after_later_mail_and_ack_appends(self):
        # Saved-cutoff behavior needs a nonempty prefix, not a capacity fixture.
        # The native maximum-success case separately exercises 99,998 frames.
        self.seed_history(3)
        self.seed_acted_mail('original-ordinary')
        self.seed_acted_mail('original-worker', scope='scratch-worker')
        result = self.perform()
        saved_rows = (self.root.path / 'receipts/seat-teardown' /
            (self.node + '.jsonl')).read_bytes()
        later = self.seed_acted_mail('later-ordinary')
        self.seed_acted_mail('later-worker', scope='scratch-worker')
        pending = self.f.send('later-unacted')
        self.assertEqual(result, self.perform())
        self.assertEqual(result, completed_teardown_for_session(self.root, self.node, self.session))
        self.assertEqual(saved_rows, (self.root.path / 'receipts/seat-teardown' /
            (self.node + '.jsonl')).read_bytes())
        self.assertIn(later['id'], SparseCursor(self.root).acked_ids(self.node))
        self.assertIn(pending['id'], EventLog(self.root).unacked_ids(self.node))

    def test_event_ack_and_wake_prefix_id_mutations_refuse_native_completion(self):
        self.seed_history(3)
        for index in range(3):
            self.seed_acted_mail('prefix-owned-mail-' + str(index))
        controller = WakeController(self.root)
        controller.pause(self.node, 'historical-chat', idempotency_key='history-pause')
        controller.resume(self.node, 'historical-chat', idempotency_key='history-resume')
        result = self.perform()
        # The refusal below is the native prefix proof. An automatic age roll
        # rewrites this ledger first and the decoder then refuses a different code.
        self.assertNotEqual(
            'bus_epoch_roll_receipt',
            decode_frames((self.root.path / 'events.jsonl').read_bytes())[0]['kind'],
            'seeded history must stay inside the automatic roll window')
        paths = {
            'events': (self.root.path / 'events.jsonl', EVENT_KINDS),
            'acks': (SparseCursor(self.root).path_for(self.node), {'ack_receipt'}),
            'wakes': (self.root.resolve_relative(controller._ledger(self.node)), {'wake_control_receipt'}),
        }
        for label, (path, kinds) in paths.items():
            original = path.read_bytes()
            records = decode_frames(original)
            for mutation in ('changed', 'inserted', 'dropped', 'reordered'):
                with self.subTest(ledger=label, mutation=mutation):
                    modified = copy.deepcopy(records)
                    if mutation == 'dropped':
                        del modified[0]
                    elif mutation == 'reordered':
                        modified[0], modified[1] = modified[1], modified[0]
                    else:
                        replacement = dict(modified[0], id=self.identity(
                            modified[0]['id'].rsplit('-', 1)[0] + '-', 900000))
                        if mutation == 'changed':
                            modified[0] = replacement
                            if result['schema_version'] == 1:
                                identities = (result['event_cutoff_ids'] if label == 'events' else
                                    result['wake_cutoff_ids'] if label == 'wakes' else
                                    next(row['receipt_ids'] for row in result['ack_cutoff_refs']
                                        if row['worker_session_id'] is None))
                                cutoff = {'count': len(identities), 'last_id': identities[-1]}
                            else:
                                cutoff = (result['event_cutoff'] if label == 'events' else
                                    result['wake_cutoff'] if label == 'wakes' else
                                    next(row['prefix'] for row in result['ack_cutoff_refs']
                                        if row['worker_session_id'] is None))
                            self.assertGreater(cutoff['count'], 1)
                            self.assertEqual(cutoff['last_id'],
                                modified[cutoff['count'] - 1]['id'],
                                'interior-ID corruption must keep the saved last ID unchanged')
                        else:
                            modified.insert(0, replacement)
                    self.write_records(path, modified, kinds)
                    try:
                        with self.assertRaises(IntegrityFailure) as caught:
                            completed_teardown_for_session(self.root, self.node, self.session)
                        self.assertEqual('seat_teardown_native_invalid', caught.exception.code)
                    finally:
                        path.write_bytes(original)
        self.assertIsNotNone(completed_teardown_for_session(self.root, self.node, self.session))


class SeatTeardownNativeScaleTests(unittest.TestCase):
    """Companion cases use the real native observer without the activity seam."""
    cold_send_index = False
    setUp = SeatTeardownScaleTests.setUp
    identity = staticmethod(SeatTeardownScaleTests.identity)
    envelope = SeatTeardownScaleTests.envelope
    write_records = SeatTeardownScaleTests.write_records
    seed_history = SeatTeardownScaleTests.seed_history
    assert_prefix_shape = SeatTeardownScaleTests.assert_prefix_shape
    assert_compact_completion = SeatTeardownScaleTests.assert_compact_completion

    def perform(self, *, native=True):
        return SeatTeardownScaleTests.perform(self, native=native)

    def tearDown(self):
        _reap_direct_children()

    def test_native_observer_final_hundred_thousand_includes_report_pair(self):
        from floati.send_prefix import CACHE_NAME, Prefix

        self.seed_history(99998)
        # This fixture bulk-loads a complete history for a single final send.
        # Exercise the native writer with its optional index unavailable so the
        # capacity control need not construct a file per historical trie node.
        if self.cold_send_index:
            self.assertFalse((self.root.tenant_home / CACHE_NAME).exists())
        else:
            (self.root.tenant_home / CACHE_NAME).touch(exist_ok=False)
        events = self.root.tenant_home / 'events.jsonl'
        cutoff_bytes = events.stat().st_size
        result = self.perform(native=True)
        self.assert_compact_completion(result, 99998)
        with events.open('rb') as stream:
            self.assertEqual(100000, sum(1 for _ in stream))
            stream.seek(cutoff_bytes)
            envelope, readiness = decode_frames(stream.read())
        self.assertEqual(('message_envelope', 'send_readiness_observation'),
            (envelope['kind'], readiness['kind']))
        self.assertEqual(result['envelope_id'], envelope['id'])
        self.assertEqual(envelope['id'], readiness['envelope_id'])
        if self.cold_send_index:
            prefix = Prefix(self.root, EVENT_KINDS, read_only=True)
            prefix.load()
            self.assertEqual(100000, prefix.payload['count'])
            self.assertEqual(envelope, prefix.get("id", envelope['id']))
            self.assertEqual(readiness, prefix.get("id", readiness['id']))

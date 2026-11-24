"""Native teardown continuity across automatic epoch carries.

Authored before implementation during W3 HOLD. No results are claimed until
the coordinated managed battery executes these sources after RESUME.
"""
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock
import unittest

from floati import bus_epoch
from floati.cursor import SparseCursor
from floati.errors import IntegrityFailure, ProtocolRefusal
from floati.events import EventLog
from floati.framing import decode_frames, encode_frame
from floati.jsonl import read_records_snapshot
from floati.wake_control import WakeController
from tests import test_seat_teardown as native_fixture
from tests import test_seat_teardown_scale_contract as historical_fixture


class EpochTeardownReaderTests(unittest.TestCase):
    def setUp(self):
        self.f = native_fixture.SeatTeardownTests('runTest')
        self.addCleanup(self.f.doCleanups)
        self.f.setUp()
        message = self.f.send('acted-before-teardown')
        EventLog(self.f.root).present(native_fixture.NODE)
        SparseCursor(self.f.root).ack(native_fixture.NODE, [message['id']],
            acting_session_id=native_fixture.SESSION)

    def invoke(self, *, legacy=False, phase=None):
        from floati import seat_teardown
        if legacy:
            historical_fixture.ScaleContractTests.setUpClass()
            target = historical_fixture.ScaleContractTests.legacy
        else:
            target = seat_teardown
        service = target.SeatTeardown(self.f.root)
        append = service._append
        def interrupted(relative, row):
            saved = append(relative, row)
            if row['step'] == phase:
                raise OSError('native durable phase interruption')
            return saved
        with mock.patch.object(target, '_inspect_activity', return_value=self.f.activity()):
            if phase is None:
                return service.teardown(native_fixture.NODE, self.f.home, native_fixture.SESSION,
                    repo='floati', doc='report.md', idempotency_key='epoch-teardown')
            with mock.patch.object(service, '_append', side_effect=interrupted):
                with self.assertRaisesRegex(OSError, 'native durable phase interruption'):
                    service.teardown(native_fixture.NODE, self.f.home, native_fixture.SESSION,
                        repo='floati', doc='report.md', idempotency_key='epoch-teardown')
        return self.f.rows()[-1]

    def roll(self):
        from floati.epoch_carry import build_carry_plan
        from floati.epoch_teardown import collect_verifications
        def planner(root, old):
            plan = build_carry_plan(root, old)
            metadata = {'trigger': 'send', 'policy': {'max_bytes': 1,
                'max_age_days': 7, 'source': 'operator_declared'},
                'observed_bytes': next(len(raw) for path, _plane, raw in old if path == 'events.jsonl'),
                'observed_at': datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z'),
                'epoch_started_at': None, 'due_reasons': ['bytes']}
            return plan.live_payloads, metadata, collect_verifications(root, old)
        return bus_epoch.roll_automatic_epoch(self.f.root, actor='architect', planner=planner)

    def acknowledge_report(self, row):
        if row['envelope_id'] is not None:
            EventLog(self.f.root).present('architect')
            SparseCursor(self.f.root).ack('architect', [row['envelope_id']],
                acting_session_id='architect-acted')

    def assert_completed(self, original):
        from floati.seat_teardown import SeatTeardown, completed_teardown_for_session
        with mock.patch('floati.seat_teardown._inspect_activity',
                side_effect=AssertionError('carried recovery resampled activity')):
            result = SeatTeardown(self.f.root).teardown(native_fixture.NODE, self.f.home,
                native_fixture.SESSION, repo='floati', doc='report.md', idempotency_key='epoch-teardown')
        self.assertEqual('completed', result['step'])
        self.assertEqual(original['schema_version'], result['schema_version'])
        self.assertEqual(original['started_receipt_id'], result['started_receipt_id'])
        self.assertEqual(result, completed_teardown_for_session(
            self.f.root, native_fixture.NODE, native_fixture.SESSION))
        pauses = read_records_snapshot(self.f.root, WakeController(self.f.root)._ledger(native_fixture.NODE),
            allowed_kinds={'wake_control_receipt'})
        self.assertEqual(1, sum(row['operation'] == 'pause' for row in pauses))
        return result

    def test_completed_v2_with_acked_report_survives_fresh_reader_without_archive_sources(self):
        original = self.invoke()
        self.acknowledge_report(original)
        rolled = self.roll()
        Path(rolled['receipt']['archive_path']).rename(self.f.base / 'unavailable-original-archive')
        with mock.patch('floati.snapshot._owned_epoch_archives',
                side_effect=AssertionError('reader boot replayed archives')), mock.patch(
                'floati.bus_epoch.validate_epoch_receipt_archive',
                side_effect=AssertionError('reader boot opened old archive')):
            self.assertEqual(original, self.assert_completed(original))

    def test_started_v2_continues_with_stable_context_and_new_native_report(self):
        original = self.invoke(phase='started')
        self.roll()
        result = self.assert_completed(original)
        self.assertIsNotNone(result['envelope_id'])
        self.assertTrue(any(row['id'] == result['envelope_id'] for row in EventLog(self.f.root).records()))

    def test_paused_v2_continues_after_roll_without_duplicate_pause(self):
        original = self.invoke(phase='paused')
        self.roll()
        self.assert_completed(original)

    def test_reported_v2_continues_with_verified_old_report(self):
        original = self.invoke(phase='reported')
        self.acknowledge_report(original)
        self.roll()
        self.assertEqual(original['envelope_id'], self.assert_completed(original)['envelope_id'])

    def test_real_v1_completed_and_exact_id_cutoff_algorithm_survive_carry(self):
        original = self.invoke(legacy=True)
        self.acknowledge_report(original)
        self.roll()
        self.assertEqual(original, self.assert_completed(original))
        self.assertTrue(all(row['schema_version'] == 1 for row in self.f.rows()))

    def test_real_v1_started_can_continue_as_v1_after_roll(self):
        original = self.invoke(legacy=True, phase='started')
        self.roll()
        self.assert_completed(original)
        self.assertTrue(all(row['schema_version'] == 1 for row in self.f.rows()))

    def test_real_v1_paused_can_continue_without_version_conversion(self):
        original = self.invoke(legacy=True, phase='paused')
        self.roll()
        self.assert_completed(original)

    def test_real_v1_reported_can_complete_with_attested_original_report(self):
        original = self.invoke(legacy=True, phase='reported')
        self.acknowledge_report(original)
        self.roll()
        self.assertEqual(original['envelope_id'], self.assert_completed(original)['envelope_id'])

    def test_second_roll_rebinds_verification_and_still_replays_completion(self):
        original = self.invoke()
        self.acknowledge_report(original)
        self.roll()
        self.f.send('later-unacked')
        self.roll()
        self.assertEqual(original, self.assert_completed(original))

    def proof_rows(self):
        return [row for row in EventLog(self.f.root).event_records()
                if row['kind'] == 'seat_teardown_epoch_verification']

    def test_structured_bad_prefix_becomes_failed_carry_with_named_refusal(self):
        from floati.seat_teardown import completed_teardown_for_session
        original = self.invoke()
        path = self.f.root.tenant_home / 'receipts/seat-teardown' / (native_fixture.NODE + '.jsonl')
        rows = decode_frames(path.read_bytes())
        # All phases retain one structurally valid but false cutoff context.
        for row in rows:
            row['event_cutoff']['digest'] = 'f' * 64
        path.write_bytes(b''.join(encode_frame(row) for row in rows))
        self.roll()
        self.assertEqual('failed', self.proof_rows()[0]['verification']['state'])
        with self.assertRaises((IntegrityFailure, ProtocolRefusal)) as caught:
            completed_teardown_for_session(self.f.root, native_fixture.NODE, native_fixture.SESSION)
        self.assertEqual('seat_teardown_native_invalid', caught.exception.code)
        self.assertTrue(getattr(caught.exception, 'remedy', None))
        from floati.epoch_teardown import carried_verification
        events = EventLog(self.f.root).event_records()
        proof = next(row for row in events if row['kind'] == 'seat_teardown_epoch_verification')
        proof['verification'] = {'state': 'verified', 'failure_code': None, 'remedy': None}
        with self.assertRaises((IntegrityFailure, ProtocolRefusal)):
            carried_verification(self.f.root, self.f.rows()[-1], events)

    def test_modified_missing_duplicate_proofs_fail_complete_commitment(self):
        from floati.epoch_teardown import carried_verification
        original = self.invoke()
        self.roll()
        events = EventLog(self.f.root).event_records()
        proof = self.proof_rows()[0]
        for name in ('missing', 'duplicate', 'modified', 'wrong-roll', 'wrong-context'):
            with self.subTest(mutation=name):
                changed = deepcopy(events)
                if name == 'missing':
                    changed = [row for row in changed if row['kind'] != proof['kind']]
                elif name == 'duplicate':
                    changed.append(deepcopy(proof))
                else:
                    target = next(row for row in changed if row['id'] == proof['id'])
                    target[{'modified': 'outgoing_epoch_id', 'wrong-roll': 'roll_receipt_sha256',
                            'wrong-context': 'teardown_context_sha256'}[name]] = '0' * 64
                with self.assertRaises((IntegrityFailure, ProtocolRefusal)):
                    carried_verification(self.f.root, original, changed)

    def test_retained_old_report_cannot_change_bytes_under_attested_id(self):
        from floati.seat_teardown import completed_teardown_for_session
        original = self.invoke()
        self.roll()  # Unacked original report remains live.
        path = self.f.root.tenant_home / 'events.jsonl'
        rows = decode_frames(path.read_bytes())
        path.write_bytes(b''.join(encode_frame(dict(row, note='changed report testimony')
            if row['id'] == original['envelope_id'] else row) for row in rows))
        with self.assertRaises((IntegrityFailure, ProtocolRefusal)):
            completed_teardown_for_session(self.f.root, native_fixture.NODE, native_fixture.SESSION)

    def test_changed_immutable_context_cannot_borrow_stable_started_id(self):
        from floati.epoch_teardown import carried_verification
        original = self.invoke()
        self.roll()
        changed = deepcopy(original)
        changed['report_note'] += ' changed'
        with self.assertRaises((IntegrityFailure, ProtocolRefusal)):
            carried_verification(self.f.root, changed, EventLog(self.f.root).event_records())

    def test_new_postroll_teardown_uses_native_prefix_validation(self):
        self.roll()
        original = self.invoke()
        self.assertEqual(original, self.assert_completed(original))
        path = self.f.root.tenant_home / 'events.jsonl'
        rows = decode_frames(path.read_bytes())
        path.write_bytes(b''.join(encode_frame(row) for row in rows[1:]))
        with self.assertRaises((IntegrityFailure, ProtocolRefusal)):
            self.assert_completed(original)

    def test_carried_cutoff_does_not_mask_invalid_postroll_ack(self):
        original = self.invoke()
        self.acknowledge_report(original)
        self.roll()
        later = self.f.send('postroll-acted-mail')
        EventLog(self.f.root).present(native_fixture.NODE)
        ack = SparseCursor(self.f.root).ack(native_fixture.NODE, [later['id']],
            acting_session_id=native_fixture.SESSION)
        path = self.f.root.tenant_home / 'receipts/acks' / (native_fixture.NODE + '.jsonl')
        rows = decode_frames(path.read_bytes())
        path.write_bytes(b''.join(encode_frame(dict(row, item_ids=[original['envelope_id']])
            if row['id'] == ack['id'] else row) for row in rows))
        with self.assertRaises((IntegrityFailure, ProtocolRefusal)):
            self.assert_completed(original)

    def test_carried_prefix_does_not_replace_current_exact_pause_proof(self):
        original = self.invoke()
        self.roll()
        relative = WakeController(self.f.root)._ledger(native_fixture.NODE)
        (self.f.root.tenant_home / relative).write_bytes(b'')
        with self.assertRaises((IntegrityFailure, ProtocolRefusal)):
            self.assert_completed(original)

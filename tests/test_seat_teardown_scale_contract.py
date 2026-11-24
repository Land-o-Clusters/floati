"""Schema and real d66-writer compatibility for bounded teardown cutoffs.

Prepared during HOLD-1016a; execution requires the parent's RESUME and slot.
The historical writer is pinned source bytes captured from d66 Git
objects, not version-flipped current receipts. Their text fixtures also work
in shallow CI clones. Only the native activity observer is an isolated seam.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
from pathlib import Path
import sys
import types
import unittest
from unittest import mock

from floati.cursor import SparseCursor
from floati.errors import ProtocolRefusal
from floati.events import EventLog
from floati.jsonl import read_records_snapshot
from floati.records import validate_record
from floati.wake_control import WakeController
from tests import test_seat_teardown as fixture
from tests.schema_validation import validate_json_schema


LEGACY_COMMIT = 'd66a94daa357bd392b3040c55cbe21e20ac884a3'
LEGACY_PINS = {
    'seat_teardown': 'bf32421bfee0f4fff18c45e0d5c040ae41283e54ed738f53d5ee2c72f2f5a13f',
}
SCHEMA_V1 = Path('schemas/v1/seat-teardown-record.schema.json')
SCHEMA_V2 = Path('schemas/v2/seat-teardown-record.schema.json')
KINDS = {'seat_teardown_receipt'}


class ScaleContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        repository = Path(__file__).resolve().parents[1]
        sources = {}
        for name, expected in LEGACY_PINS.items():
            path = repository / 'tests/fixtures/seat_teardown_d66' / (name + '.py.txt')
            try:
                source = path.read_bytes()
            except OSError as exc:
                raise AssertionError('Legacy fixture admission failed: required pinned d66 source '
                    + name + ' is unavailable; do not replace it with current source or skip') from exc
            if hashlib.sha256(source).hexdigest() != expected:
                raise AssertionError('Legacy fixture source pin differs: ' + name)
            sources[name] = source

        def load(name):
            alias = 'floati._scale_d66_' + name
            module = types.ModuleType(alias)
            module.__package__ = 'floati'
            module.__file__ = str(repository / 'floati' / (name + '.py'))
            # The original writer uses the current native ledger implementation;
            # its v1 compatibility is exactly what these controls exercise.
            # No installed source, configuration or hook is changed.
            with mock.patch.dict(sys.modules, {alias: module}):
                exec(compile(sources[name], module.__file__, 'exec'), module.__dict__)
            return module

        cls.legacy = load('seat_teardown')

    def setUp(self):
        self.f = fixture.SeatTeardownTests()
        self.addCleanup(self.f.doCleanups)
        self.f.setUp()

    def current(self):
        from floati.seat_teardown import SeatTeardown
        return SeatTeardown(self.f.root)

    def invoke(self, service, *, legacy=False):
        target = self.legacy if legacy else sys.modules['floati.seat_teardown']
        with mock.patch.object(target, '_inspect_activity', return_value=self.f.activity()):
            return service.teardown(fixture.NODE, self.f.home, fixture.SESSION,
                repo='floati', doc='report.md', idempotency_key='scale-contract')

    def historical_recovery(self, phase):
        old = self.legacy.SeatTeardown(self.f.root)
        append = old._append

        def interrupt(relative, row):
            result = append(relative, row)
            if row['step'] == phase:
                raise OSError('owned scratch interruption after historical ' + phase)
            return result

        with mock.patch.object(old, '_append', side_effect=interrupt):
            with self.assertRaisesRegex(OSError, 'owned scratch interruption'):
                self.invoke(old, legacy=True)
        before = self.f.rows()
        self.assertTrue(before)
        self.assertTrue(all(row['schema_version'] == 1 for row in before))
        self.assertTrue(all('event_cutoff_ids' in row and 'event_cutoff' not in row for row in before))
        later = self.f.send('later-after-legacy-' + phase)

        from floati import seat_teardown
        with mock.patch.object(seat_teardown, '_inspect_activity',
                side_effect=AssertionError('legacy recovery resampled native activity')):
            result = self.current().teardown(fixture.NODE, self.f.home, fixture.SESSION,
                repo='floati', doc='report.md', idempotency_key='scale-contract')
        self.assertEqual('completed', result['step'])
        self.assertEqual(1, result['schema_version'])
        self.assertTrue(all(row['schema_version'] == 1 for row in self.f.rows()))
        self.assertEqual(before, self.f.rows()[:len(before)], 'historical frames were rewritten')
        for row in self.f.rows():
            validate_record(row, self.f.root.tenant_id, KINDS, integrity=False)
            validate_json_schema(row, SCHEMA_V1)
        self.assertEqual(result, seat_teardown.completed_teardown_for_session(
            self.f.root, fixture.NODE, fixture.SESSION))
        self.assertEqual(result, self.legacy.completed_teardown_for_session(
            self.f.root, fixture.NODE, fixture.SESSION))
        self.assertEqual(result, self.invoke(old, legacy=True))
        self.assertNotIn(later['id'], SparseCursor(self.f.root).acked_ids(fixture.NODE))
        reports = [row for row in EventLog(self.f.root).event_records()
            if row['kind'] == 'message_envelope' and row['sender'] == fixture.NODE]
        self.assertEqual([result['envelope_id']], [row['id'] for row in reports])
        pauses = read_records_snapshot(self.f.root,
            WakeController(self.f.root)._ledger(fixture.NODE), allowed_kinds={'wake_control_receipt'})
        self.assertEqual([result['pause_receipt_id']], [row['id'] for row in pauses])

    def test_real_v1_started_recovers_without_version_mixing(self):
        cases = ('started', 'paused', 'reported', 'completed')
        for index, phase in enumerate(cases):
            with self.subTest(phase=phase):
                if index:
                    self.doCleanups()
                    self.setUp()
                self.historical_recovery(phase)

    def v2(self, *, nonempty=True):
        if nonempty:
            message = self.f.send('proof-shape-mail')
            EventLog(self.f.root).present(fixture.NODE)
            SparseCursor(self.f.root).ack(fixture.NODE, [message['id']],
                acting_session_id=fixture.SESSION)
            controller = WakeController(self.f.root)
            controller.pause(fixture.NODE, fixture.SESSION, idempotency_key='shape-pause')
            controller.resume(fixture.NODE, fixture.SESSION, idempotency_key='shape-resume')
        result = self.invoke(self.current())
        self.assertEqual(2, result['schema_version'], 'new invocations must use the bounded v2 writer')
        return result

    def valid_both(self, row):
        self.assertEqual(row, validate_record(row, self.f.root.tenant_id, KINDS, integrity=False))
        validate_json_schema(row, SCHEMA_V2)

    def invalid_both(self, row):
        with self.assertRaises(ProtocolRefusal):
            validate_record(row, self.f.root.tenant_id, KINDS, integrity=False)
        with self.assertRaises(AssertionError):
            validate_json_schema(row, SCHEMA_V2)

    def test_v2_native_phase_records_match_published_schema(self):
        result = self.v2()
        self.assertEqual(['started', 'paused', 'reported', 'completed'],
            [row['step'] for row in self.f.rows()])
        for row in self.f.rows():
            self.valid_both(row)
            self.assertEqual(1, row['ack_scope_count'])
            self.assertEqual({'worker_session_id', 'prefix'}, set(row['ack_cutoff_refs'][0]))
            for proof in (row['event_cutoff'], row['wake_cutoff'], row['ack_cutoff_refs'][0]['prefix']):
                self.assertEqual({'count', 'last_id', 'digest'}, set(proof))
                self.assertGreater(proof['count'], 0)
        from floati.seat_teardown import completed_teardown_for_session
        self.assertEqual(result, completed_teardown_for_session(
            self.f.root, fixture.NODE, fixture.SESSION))

    def test_v2_empty_prefixes_require_zero_count_and_null_last_id(self):
        result = self.v2(nonempty=False)
        self.valid_both(result)
        for field in ('event_cutoff', 'wake_cutoff'):
            self.assertEqual(0, result[field]['count'])
            self.assertIsNone(result[field]['last_id'])
            row = deepcopy(result)
            row[field]['last_id'] = 'msg-00000000000070008000000000000001'
            self.invalid_both(row)
            row = deepcopy(result)
            row[field]['count'] = 1
            self.invalid_both(row)

    def test_v2_proof_counts_last_ids_and_digests_have_closed_bounds(self):
        result = self.v2()
        mutations = [
            ('count', True), ('count', False), ('count', -1), ('count', 0), ('count', 100001),
            ('count', 1.5), ('count', '1'), ('last_id', None), ('last_id', ''),
            ('last_id', 'x' * 129), ('last_id', 'unsafe\n'),
            ('digest', ''), ('digest', 'a' * 63), ('digest', 'a' * 65),
            ('digest', 'G' * 64), ('digest', None),
        ]
        for target in ('event_cutoff', 'wake_cutoff', 'ack'):
            for field, value in mutations:
                with self.subTest(target=target, field=field, value=value):
                    row = deepcopy(result)
                    proof = row['ack_cutoff_refs'][0]['prefix'] if target == 'ack' else row[target]
                    proof[field] = value
                    self.invalid_both(row)
            row = deepcopy(result)
            proof = row['ack_cutoff_refs'][0]['prefix'] if target == 'ack' else row[target]
            proof['extra'] = 'not part of a prefix'
            self.invalid_both(row)

    def test_v2_schema_accepts_the_native_ledger_count_boundary(self):
        row = self.v2()
        # This is shape admission only. Native prefix verification must still
        # reject a digest/count that does not match the actual retained ledger.
        for proof in (row['event_cutoff'], row['wake_cutoff'], row['ack_cutoff_refs'][0]['prefix']):
            proof['count'] = 100000
        self.valid_both(row)

    def test_v2_ack_scope_count_bounds_and_required_field(self):
        result = self.v2()
        for value in (True, False, -1, 0, 1001, '1', 1.5, None):
            with self.subTest(value=value):
                row = deepcopy(result)
                row['ack_scope_count'] = value
                self.invalid_both(row)
        row = deepcopy(result)
        del row['ack_scope_count']
        self.invalid_both(row)

    def test_v2_ack_scope_count_equals_actual_scope_count_in_native_validator(self):
        # Standard JSON Schema has no cross-property array-length comparison.
        # The native contract owns this equality; primitive bounds are shared.
        row = self.v2()
        row['ack_scope_count'] = 2
        with self.assertRaises(ProtocolRefusal):
            validate_record(row, self.f.root.tenant_id, KINDS, integrity=False)

    def test_v2_and_v1_shapes_cannot_be_mixed_or_version_flipped(self):
        result = self.v2()
        for old_field in ('event_cutoff_ids', 'ack_receipt_ids', 'wake_cutoff_ids'):
            row = deepcopy(result)
            row[old_field] = []
            self.invalid_both(row)
        row = deepcopy(result)
        row['ack_cutoff_refs'][0]['receipt_ids'] = []
        self.invalid_both(row)
        row = deepcopy(result)
        row['schema_version'] = 1
        with self.assertRaises(ProtocolRefusal):
            validate_record(row, self.f.root.tenant_id, KINDS, integrity=False)
        with self.assertRaises(AssertionError):
            validate_json_schema(row, SCHEMA_V1)


if __name__ == '__main__':
    unittest.main()

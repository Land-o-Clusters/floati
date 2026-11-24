"""Sealed presentation testimony is checked at arm and audited by doctor."""
import hashlib
import io
import json
import unittest
from contextlib import ExitStack
from unittest import mock

from floati.cursor_presentations import CursorPresentationLedger, _HistoricalCuts
from floati.doctor import Doctor
from floati.epoch_carry import ArchiveProofResolver
from floati.errors import IntegrityFailure
from floati.framing import decode_frames, encode_frame
from tests import test_epoch_carry
from pathlib import Path


class WaiterSealedHistoryTests(unittest.TestCase):
    def test_roll_anchors_legacy_holds_natively_and_keeps_old_imports_readable(self):
        from floati.wake_hold import WakeHoldController
        from floati.records import wake_hold_decision_digest
        for version in (1, 2):
            with self.subTest(version=version):
                fixture = test_epoch_carry.EpochCarryTests('runTest')
                fixture.setUp()
                try:
                    fixture.send('held')
                    original = WakeHoldController(fixture.root).evaluate(
                        'seat-a', idempotency_key='held')['receipt']
                    path = fixture.root.path / 'receipts/deliveries/seat-a.jsonl'
                    if version == 2:
                        # A native release re-presents the already held item.
                        release = dict(original, schema_version=2,
                            id='wake-hold-' + __import__('floati.ids', fromlist=['uuid7_hex']).uuid7_hex(),
                            idempotency_key='released', selection_reason='holder_released')
                        source = WakeHoldController(fixture.root)._read('seat-a', None)
                        release['delivery_prefix_digest'] = source[3][-1]
                        release['decision_digest'] = wake_hold_decision_digest(release)
                        path.write_bytes(path.read_bytes() + encode_frame(release))
                        original = release
                    # Pre-change carry remains byte-exact and uses sealed origins.
                    plan = fixture.plan()
                    fixture.install_fixture_carry(plan.live_payloads)
                    native = ArchiveProofResolver._hold_origin
                    with mock.patch.object(ArchiveProofResolver, '_hold_origin', autospec=True,
                                           side_effect=native) as origins:
                        self.assertEqual('held_only', fixture.hold_result()['state'])
                    self.assertGreater(origins.call_count, 0)
                    fixture.automatic_roll()
                    with mock.patch.object(ArchiveProofResolver, '_hold_origin', autospec=True,
                                           side_effect=native) as origins:
                        self.assertEqual('held_only', fixture.hold_result()['state'])
                    self.assertEqual(0, origins.call_count)
                    holds = [row for row in decode_frames(path.read_bytes())
                             if row['kind'] == 'wake_hold_receipt']
                    carried = next(row for row in holds
                                   if row['idempotency_key'] == original['idempotency_key'])
                    self.assertEqual(3, carried['schema_version'])
                    for field in ('timestamp', 'item_ids', 'limit', 'recipient', 'worker_session_id',
                                  'idempotency_key', 'decision_digest'):
                        self.assertEqual(original[field], carried[field], field)
                    self.assertEqual(original.get('selection_reason'), carried.get('selection_reason'))
                    receipt = decode_frames((fixture.root.path / 'events.jsonl').read_bytes())[0]
                    self.assertEqual({'hold_id': original['id'], 'roll_receipt_id': receipt['id']},
                                     carried['carried_from'])
                    fixture.send('later-fresh-mail')
                    fixture.automatic_roll()
                    retried = WakeHoldController(fixture.root).evaluate('seat-a',
                        idempotency_key=original['idempotency_key'])
                    self.assertEqual(original['item_ids'],
                                     [row['id'] for row in retried['fresh_messages']])
                    holds = [row for row in decode_frames(path.read_bytes())
                             if row['kind'] == 'wake_hold_receipt']
                    previous = carried
                    carried = next(row for row in holds
                                   if row['idempotency_key'] == original['idempotency_key'])
                    self.assertEqual(previous['id'], carried['carried_from']['hold_id'])
                    # A v3 mismatch is native corruption, never an archive import.
                    for field in ('event_prefix_digest', 'delivery_prefix_digest',
                                  'acknowledgment_prefix_digest'):
                        with self.subTest(field=field):
                            path.write_bytes(b''.join(encode_frame(dict(row, **{field: 'f' * 64})
                                if row['id'] == carried['id'] else row) for row in holds))
                            with mock.patch.object(ArchiveProofResolver, '_hold_origin', autospec=True,
                                                   side_effect=native) as origins:
                                result = fixture.hold_result('corrupt-' + field)
                            self.assertEqual('consumption_state_unavailable', result['code'])
                            self.assertEqual(0, origins.call_count)
                finally:
                    fixture.doCleanups()

    def test_roll_binds_borrowed_frames_without_reading_sealed_sources(self):
        from floati import bus_epoch
        from floati.wake_hold import wake_coordination_guard
        for flushed in (False, True):
            with self.subTest(flushed=flushed):
                fixture = test_epoch_carry.EpochCarryTests('runTest')
                fixture.setUp()
                try:
                    message = fixture.send('borrowed')
                    binding, consent, classification, _ = fixture.presentation(flushed=flushed)
                    for roll in (1, 2):
                        fixture.automatic_roll()
                        events = fixture.root.path / 'events.jsonl'
                        raw = events.read_bytes()
                        native = ArchiveProofResolver._source
                        with mock.patch.object(ArchiveProofResolver, '_source', autospec=True,
                                               side_effect=native) as sources:
                            plane = CursorPresentationLedger(fixture.root, sealed='bytes')
                            with bus_epoch.epoch_guard(fixture.root), wake_coordination_guard(fixture.root, 'seat-a'):
                                selection = plane._select_waiter(binding, consent, classification, harness='codex')
                        self.assertEqual(0, sources.call_count)
                        self.assertEqual([], selection['sealed_read'])
                        self.assertEqual('held_unacked' if flushed else 'presentation_uncertain',
                                         plane.held_evidence(selection)['state'])
                        for corruption in ('changed_frame', 'absent_entry'):
                            with self.subTest(roll=roll, corruption=corruption):
                                from floati.records import epoch_carry_verification_digest
                                rows = decode_frames(raw)
                                for row in rows:
                                    if corruption == 'changed_frame' and row['id'] == message['id']:
                                        row['note'] = 'changed carried payload'
                                    elif corruption == 'absent_entry' and row['kind'] == 'epoch_carry_verification':
                                        row['carried_envelopes'] = [entry for entry in row['carried_envelopes']
                                                                  if entry['id'] != message['id']]
                                        row['digest'] = epoch_carry_verification_digest(row['carried_envelopes'])
                                events.write_bytes(b''.join(encode_frame(row) for row in rows))
                                with mock.patch.object(ArchiveProofResolver, '_source', autospec=True,
                                                       side_effect=native) as sources:
                                    plane = CursorPresentationLedger(fixture.root, sealed='bytes')
                                    with self.assertRaisesRegex(IntegrityFailure,
                                            'carried presentation envelope differs from its exact original native frame'):
                                        with bus_epoch.epoch_guard(fixture.root), wake_coordination_guard(fixture.root, 'seat-a'):
                                            plane._select_waiter(binding, consent, classification, harness='codex')
                                self.assertEqual(0, sources.call_count)
                                events.write_bytes(raw)
                finally:
                    fixture.doCleanups()

    def setUp(self):
        self.fixture = test_epoch_carry.EpochCarryTests('runTest')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.root
        self.fixture.send('sealed')
        self.binding, self.consent, _, _ = self.fixture.presentation(flushed=True)
        self.fixture.install_fixture_carry(self.fixture.plan().live_payloads)

    def test_lineage_hashes_two_epochs_without_decoding_archive_rows(self):
        from floati import bus_epoch, epoch_carry, epoch_history, framing, jsonl
        from floati.archive_container import write_container
        from floati.codex_wait_contract import CodexWaitSessionLedger
        from floati.wake_hold import wake_coordination_guard
        import shutil

        self.fixture.automatic_roll()
        archives = ArchiveProofResolver(self.root).archives()
        payloads = {archive: {path.relative_to(archive).as_posix(): path.read_bytes()
                    for path in archive.rglob('*') if path.is_file()} for archive in archives}
        archived_events = {members['events.jsonl'] for members in payloads.values()}
        self.assertEqual(2, len(archived_events))
        self.assertTrue(all(raw.count(b'\n') > 1 for raw in archived_events))
        sessions = CodexWaitSessionLedger(self.root)
        sessions.arm(self.binding, self.consent, 'conversation-b',
                     idempotency_key='lineage-session', take_over=True)
        classification = sessions.participate_classified(
            self.binding, self.consent, 'conversation-b')
        native = framing.decode_frames
        decoded = []
        def observe(raw, *args, **kwargs):
            if raw in archived_events:
                decoded.append(raw)
            return native(raw, *args, **kwargs)

        for representation in ('directory', 'container'):
            if representation == 'container':
                for archive, members in payloads.items():
                    inventory = [{'path': name, 'plane': 'events' if name == 'events.jsonl'
                                  else name.split('/')[1], 'byte_length': len(raw),
                                  'sha256': hashlib.sha256(raw).hexdigest()}
                                 for name, raw in sorted(members.items())]
                    target = archive.with_name(archive.name + '.zip')
                    write_container(target, inventory, lambda name: io.BytesIO(members[name]))
                    shutil.rmtree(archive)
                    target.rename(archive)
            for reader in ('arm', 'history'):
                with self.subTest(representation=representation, reader=reader):
                    epoch_carry._FACT_CACHE.clear()
                    decoded.clear()
                    with ExitStack() as stack:
                        for module in (framing, epoch_carry, epoch_history, jsonl, bus_epoch):
                            stack.enter_context(mock.patch.object(module, 'decode_frames', observe))
                        if reader == 'arm':
                            plane = CursorPresentationLedger(self.root, sealed='bytes')
                            with bus_epoch.epoch_guard(self.root), wake_coordination_guard(self.root, 'seat-a'):
                                selection = plane._select_waiter(self.binding, self.consent,
                                    classification, harness='codex')
                            self.assertEqual([], selection['sealed_read'])
                        else:
                            history = epoch_history.read_history(self.root, max_records=10,
                                                                 max_bytes=1024 * 1024)
                            self.assertEqual(2, len(history))
                    self.assertEqual(0, len(decoded), 'lineage decoded whole archived events')

    def test_audit_carry_and_collector_still_decode_sealed_rows(self):
        from floati import bus_epoch, epoch_carry, jsonl
        from floati.archive_checkpoint import collect_presentation_sources
        native = jsonl.decode_frames
        archive = ArchiveProofResolver(self.root).archives()[0]
        events = (archive / 'events.jsonl').read_bytes()
        decoded = []
        def observe(raw, *args, **kwargs):
            if raw == events:
                decoded.append(raw)
            return native(raw, *args, **kwargs)
        readers = (
            lambda: CursorPresentationLedger(self.root)._load('seat-a', materialize=False),
            self.fixture.plan,
            lambda: collect_presentation_sources(self.root),
        )
        for reader in readers:
            with self.subTest(reader=reader):
                epoch_carry._FACT_CACHE.clear()
                epoch_carry._ORIGIN_CACHE.clear()
                decoded.clear()
                with mock.patch.object(jsonl, 'decode_frames', observe), mock.patch.object(epoch_carry, 'decode_frames', observe):
                    with bus_epoch.epoch_guard(self.root, exclusive=True):
                        reader()
                self.assertTrue(decoded, 'row reader did not decode sealed events')

    def test_sealed_source_is_not_loaded_but_live_cut_is_proved(self):
        self.fixture.send('live')
        # A new session can present in the live epoch.
        from floati.codex_wait_contract import CodexWaitSessionLedger
        from floati import bus_epoch
        from floati.wake_hold import wake_coordination_guard
        binding, consent = self.binding, self.consent
        sessions = CodexWaitSessionLedger(self.root)
        sessions.arm(binding, consent, 'conversation-b', idempotency_key='live-session', take_over=True)
        classification = sessions.participate_classified(binding, consent, 'conversation-b')
        plane = CursorPresentationLedger(self.root)
        with bus_epoch.epoch_guard(self.root), wake_coordination_guard(self.root, 'seat-a'):
            selection = plane.select(binding, consent, classification, harness='codex')
            plane.prepare(selection, {'followup_message': 'live'})
        source = ArchiveProofResolver._source
        sources = CursorPresentationLedger._sources
        eligible = _HistoricalCuts.eligible
        calls, cuts = [], []
        def observed(resolver, home, *args, **kwargs):
            calls.append(home)
            return source(resolver, home, *args, **kwargs)
        def observed_sources(ledger, home, *args, **kwargs):
            if home != self.root.tenant_home:
                calls.append(home)
            return sources(ledger, home, *args, **kwargs)
        def observed_eligible(index, *args, **kwargs):
            cuts.append(True)
            return eligible(index, *args, **kwargs)
        with mock.patch.object(ArchiveProofResolver, '_source', observed), mock.patch.object(CursorPresentationLedger, '_sources', observed_sources), mock.patch.object(_HistoricalCuts, 'eligible', observed_eligible):
            plane = CursorPresentationLedger(self.root, sealed='bytes')
            with bus_epoch.epoch_guard(self.root), wake_coordination_guard(self.root, 'seat-a'):
                selection = plane._select_waiter(binding, consent, classification, harness='codex')
        self.assertEqual([], [home for home in calls if home != self.root.tenant_home])
        self.assertEqual([], selection['sealed_read'])
        self.assertTrue(cuts)

    def test_semantic_rewrite_passes_arm_and_doctor_names_it(self):
        plane = CursorPresentationLedger(self.root)
        path = self.root.path / plane.relative('seat-a')
        witness_path = self.root.path / plane.witness_relative('seat-a')
        rows = decode_frames(path.read_bytes())
        for row in rows:
            row['event_prefix_digest'] = 'f' * 64
        raw = b''.join(encode_frame(row) for row in rows)
        witness = json.loads(witness_path.read_bytes())
        reserved = witness['reserved_record']
        if reserved is not None:
            reserved['event_prefix_digest'] = 'f' * 64
            prefix = raw[:-len(encode_frame(rows[-1]))]
        else:
            prefix = raw
        witness.update(before_bytes=len(prefix), before_digest=hashlib.sha256(prefix).hexdigest())
        path.write_bytes(raw)
        witness_path.write_text(json.dumps(witness))
        CursorPresentationLedger(self.root, sealed='bytes')._load_native('seat-a', materialize=False)
        report, _ = Doctor(Path.cwd(), self.root.path, ref='HEAD').artifact()
        self.assertTrue(any(row['code'] == 'presentation_history_unavailable' and 'seat-a' in row['subject'] for row in report['findings']))

    def test_byte_rewrite_is_refused(self):
        from floati import codex_wait
        plane = CursorPresentationLedger(self.root, sealed='bytes')
        path = self.root.path / plane.relative('seat-a')
        path.write_bytes(path.read_bytes().replace(b'"event_prefix_digest":"', b'"event_prefix_digest":"f', 1))
        with self.assertRaises(IntegrityFailure):
            plane._load_native('seat-a', materialize=False)
        now, result = [0.0], {}
        def sleep(seconds):
            now[0] += seconds
        status = codex_wait.run_stop_waiter(bus_home=self.root.path,
            hook_payload={'cwd': str(self.binding.workspace), 'session_id': 'conversation-a'},
            stdout=io.StringIO(), stderr=io.StringIO(), monotonic=lambda: now[0], sleep=sleep,
            _waiter_result=result)
        self.assertEqual(0, status)
        self.assertEqual('integrity_failure', result['kind'])
        self.assertEqual('presentation_history_unavailable', result['code'])

    def test_borrowed_presentation_reads_its_sealed_epoch_once_after_two_rolls(self):
        from floati import bus_epoch
        from floati.codex_wait_contract import CodexWaitSessionLedger
        from floati.wake_hold import wake_coordination_guard
        plan = self.fixture.plan()
        self.fixture.install_fixture_carry(plan.live_payloads)
        classification = CodexWaitSessionLedger(self.root).participate_classified(
            self.binding, self.consent, 'conversation-a')
        plane = CursorPresentationLedger(self.root, sealed='bytes')
        native = ArchiveProofResolver._source
        calls = []
        def observe(resolver, home, *args, **kwargs):
            calls.append(home)
            return native(resolver, home, *args, **kwargs)
        with mock.patch.object(ArchiveProofResolver, '_source', observe):
            with bus_epoch.epoch_guard(self.root), wake_coordination_guard(self.root, 'seat-a'):
                selection = plane._select_waiter(self.binding, self.consent, classification, harness='codex')
        self.assertEqual(1, len(calls))
        self.assertEqual(selection['sealed_read'], [decode_frames(
            (self.root.path / plane.relative('seat-a')).read_bytes())[0]['epoch_anchor']])
        self.assertEqual(1, selection['held_total'])

    def test_two_borrowed_anchors_are_each_read_once(self):
        from floati import bus_epoch
        from floati.codex_wait_contract import CodexWaitSessionLedger
        from floati.wake_hold import wake_coordination_guard
        self.fixture.send('second')
        classification = CodexWaitSessionLedger(self.root).participate_classified(
            self.binding, self.consent, 'conversation-a')
        plane = CursorPresentationLedger(self.root)
        with bus_epoch.epoch_guard(self.root), wake_coordination_guard(self.root, 'seat-a'):
            selection = plane.select(self.binding, self.consent, classification, harness='codex')
            prepared = plane.prepare(selection, {'followup_message': 'second'})
            plane.flushed(prepared, plane.deliver(prepared, selection))
        self.fixture.install_fixture_carry(self.fixture.plan().live_payloads)
        rows = decode_frames((self.root.path / plane.relative('seat-a')).read_bytes())
        anchors = sorted({row['epoch_anchor'] for row in rows})
        calls = []
        native = ArchiveProofResolver._source
        def observe(resolver, home, *args, **kwargs):
            calls.append(home)
            return native(resolver, home, *args, **kwargs)
        plane = CursorPresentationLedger(self.root, sealed='bytes')
        with mock.patch.object(ArchiveProofResolver, '_source', observe):
            with bus_epoch.epoch_guard(self.root), wake_coordination_guard(self.root, 'seat-a'):
                selection = plane._select_waiter(self.binding, self.consent, classification, harness='codex')
        self.assertEqual(2, len(set(calls)))
        self.assertEqual(2, len(calls))
        self.assertEqual(anchors, selection['sealed_read'])
        self.assertEqual(2, selection['held_total'])

    def test_archive_byte_rewrite_refuses_through_stop_waiter(self):
        from floati import codex_wait
        resolver = ArchiveProofResolver(self.root)
        archive = resolver.archives()[0]
        events = archive / 'events.jsonl'
        raw = events.read_bytes()
        self.assertIn(b'native carry fixture', raw)
        events.write_bytes(raw.replace(b'native carry fixture', b'native carry fixturf', 1))
        output, error, result = io.StringIO(), io.StringIO(), {}
        now = [0.0]
        def sleep(seconds):
            now[0] += seconds
        status = codex_wait.run_stop_waiter(bus_home=self.root.path,
            hook_payload={'cwd': str(self.binding.workspace), 'session_id': 'conversation-a'},
            stdout=output, stderr=error, monotonic=lambda: now[0], sleep=sleep,
            _waiter_result=result)
        self.assertEqual(0, status)
        self.assertEqual('integrity_failure', result['kind'])
        self.assertEqual('epoch_carry_evidence_invalid', result['code'])

    def test_changed_carried_frames_refuse_in_byte_mode(self):
        from floati import bus_epoch
        from floati.wake_hold import wake_coordination_guard
        for flushed in (False, True):
            with self.subTest(flushed=flushed):
                fixture = test_epoch_carry.EpochCarryTests('runTest')
                fixture.setUp()
                try:
                    message = fixture.send('borrowed')
                    binding, consent, classification, _ = fixture.presentation(flushed=flushed)
                    fixture.automatic_roll()
                    plane = CursorPresentationLedger(fixture.root, sealed='bytes')
                    with bus_epoch.epoch_guard(fixture.root), wake_coordination_guard(fixture.root, 'seat-a'):
                        selection = plane._select_waiter(binding, consent, classification, harness='codex')
                    self.assertEqual(1, selection['held_total'])
                    path = fixture.root.path / 'events.jsonl'
                    rows = decode_frames(path.read_bytes())
                    path.write_bytes(b''.join(encode_frame(dict(row, note='changed carried payload')
                        if row['id'] == message['id'] else row) for row in rows))
                    plane = CursorPresentationLedger(fixture.root, sealed='bytes')
                    with self.assertRaises(IntegrityFailure):
                        with bus_epoch.epoch_guard(fixture.root), wake_coordination_guard(fixture.root, 'seat-a'):
                            plane._select_waiter(binding, consent, classification, harness='codex')
                finally:
                    fixture.doCleanups()

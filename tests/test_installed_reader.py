"""An installed reader names valid future vocabulary without alleging corruption."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from floati.framing import encode_frame


class InstalledReaderTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name).resolve()
        self.runtime = self.base / 'runtime'
        self.runtime.mkdir()
        shutil.copytree(Path(__file__).resolve().parents[1] / 'floati', self.runtime / 'floati',
                        ignore=shutil.ignore_patterns('__pycache__'))
        self.identity = {
            'schema_version': 0, 'source_sha': 'a' * 40,
            'source_checkout': str(self.base / 'checkout'),
            'install_root': str(self.base / 'installation'),
        }
        self.provenance = self.runtime / '.floati-installed-reader.json'
        self.provenance.write_text(json.dumps(self.identity))
        self.ledger = self.base / 'events.jsonl'
        self.record = {'schema_version': 2, 'id': 'future-01a04900000070008000000000000000',
                       'tenant_id': 'fixture', 'timestamp': '2026-08-29T12:00:00.000Z',
                       'kind': 'future_receipt', 'payload': {'future': True}}

    def read(self, *, compatible=False):
        self.ledger.write_bytes(encode_frame(self.record))
        program = '''import json, sys
from pathlib import Path
from floati.jsonl import _decode_path_records
from floati.errors import FloatiError
try:
    rows = _decode_path_records(Path(sys.argv[1]), 'fixture', frozenset({'message_envelope'}), Path(sys.argv[1]).read_bytes(), unrecognized={} if sys.argv[2] == 'yes' else None)
    print(json.dumps({'status':'ok','rows':rows}))
except FloatiError as exc:
    print(json.dumps({'status':type(exc).__name__,'code':exc.code,'detail':exc.detail,'remedy':getattr(exc,'remedy',None)}))
'''
        environment = dict(os.environ, PYTHONDONTWRITEBYTECODE='1')
        environment.pop('PYTHONPATH', None)
        result = subprocess.run([sys.executable, '-c', program, str(self.ledger),
                                 'yes' if compatible else 'no'], cwd=self.runtime,
                                env=environment, capture_output=True, text=True, timeout=15)
        self.assertEqual(0, result.returncode, result.stderr)
        return json.loads(result.stdout)

    def test_installed_reader_refuses_valid_future_kind_with_exact_update_remedy(self):
        for compatible in (False, True):
            with self.subTest(compatible=compatible):
                result = self.read(compatible=compatible)
                self.assertEqual('ProtocolRefusal', result['status'])
                self.assertEqual('reader_older_than_ledger', result['code'])
                self.assertIn('future_receipt', result['detail'])
                self.assertIn('a' * 40, result['detail'])
                self.assertEqual('floati update --source ' + self.identity['source_checkout'] +
                                 ' --destination ' + self.identity['install_root'], result['remedy'])

    def test_invalid_future_envelope_remains_corruption(self):
        self.record['timestamp'] = 'invalid'
        result = self.read(compatible=True)
        self.assertEqual('IntegrityFailure', result['status'])
        self.assertEqual('timestamp_invalid', result['code'])

    def test_known_kind_in_wrong_ledger_remains_corruption(self):
        self.record['kind'] = 'registry_entry'
        result = self.read(compatible=True)
        self.assertEqual('IntegrityFailure', result['status'])
        self.assertEqual('record_kind_invalid', result['code'])

    def test_source_compatible_reader_still_skips_valid_future_kind(self):
        self.provenance.unlink()
        self.assertEqual({'status': 'ok', 'rows': []}, self.read(compatible=True))

    def test_malformed_provenance_is_not_silent_source_mode(self):
        self.provenance.write_text('{broken')
        result = self.read(compatible=True)
        self.assertEqual('ProtocolRefusal', result['status'])
        self.assertEqual('installed_reader_identity_invalid', result['code'])
        self.assertIsInstance(result['remedy'], str)

    def test_unknown_kind_remedy_preserves_explicit_profile_repin_binding(self):
        registry = str(self.base / 'profiles.json')
        self.identity.update(profile_registry=registry, fleet_profile='fixture-reader')
        self.provenance.write_text(json.dumps(self.identity))
        result = self.read(compatible=True)
        self.assertEqual('reader_older_than_ledger', result['code'])
        self.assertEqual('floati update --source ' + self.identity['source_checkout'] +
                         ' --destination ' + self.identity['install_root'] +
                         ' --profile-registry ' + registry + ' --fleet-profile fixture-reader',
                         result['remedy'])

    def test_partial_profile_binding_refuses_instead_of_omitting_repin(self):
        self.identity['fleet_profile'] = 'fixture-reader'
        self.provenance.write_text(json.dumps(self.identity))
        result = self.read(compatible=True)
        self.assertEqual('installed_reader_identity_invalid', result['code'])

    def test_waiter_bundle_preserves_installed_source_sha_without_host_paths(self):
        from floati.codex_hook_install import CodexHookInstaller
        self.provenance.unlink()
        (self.runtime / 'LICENSE').write_text('fixture license')
        (self.runtime / 'scripts').mkdir()
        (self.runtime / 'scripts' / 'floati-codex-wait').write_text('# fixture launcher')
        metadata = self.runtime / '.floati-install'
        metadata.mkdir()
        (metadata / 'manifest.v0.json').write_text(json.dumps({
            'schema_version': 0, 'source_ref': 'origin/main', 'source_sha': 'b' * 40,
            'files': [],
        }))
        installer = CodexHookInstaller(source_root=self.runtime, bus_home=self.base / 'fleet',
                                      hooks_path=self.base / 'hooks.json',
                                      destination=self.base / 'waiters')
        _, target, _ = installer._install_bundle()
        provenance = target / '.floati-installed-reader.json'
        self.assertTrue(provenance.is_file(), 'waiter install loses source commit provenance')
        self.assertEqual({'schema_version': 0, 'source_sha': 'b' * 40,
                          'source_state': 'measured'}, json.loads(provenance.read_text()))

    def test_waiter_bundle_source_without_provenance_is_typed_absence(self):
        from floati.codex_hook_install import CodexHookInstaller
        self.provenance.unlink()
        (self.runtime / 'LICENSE').write_text('fixture license')
        (self.runtime / 'scripts').mkdir()
        (self.runtime / 'scripts' / 'floati-codex-wait').write_text('# fixture launcher')
        installer = CodexHookInstaller(source_root=self.runtime, bus_home=self.base / 'fleet',
                                      hooks_path=self.base / 'hooks.json',
                                      destination=self.base / 'waiters')
        _, target, _ = installer._install_bundle()
        provenance = target / '.floati-installed-reader.json'
        self.assertTrue(provenance.is_file(), 'waiter install lacks typed source absence')
        self.assertEqual({'schema_version': 0, 'source_sha': None,
                          'source_state': 'absent'}, json.loads(provenance.read_text()))

    def test_actual_installed_stop_waiter_forwards_reader_refusal(self):
        from floati.codex_wait_contract import CodexWaitConsentLedger, CodexWaitSessionLedger, resolve_participant, consent_ledger_relative
        from floati.registry import Registry
        from floati.root import FloatiRoot

        self.provenance.write_text(json.dumps({'schema_version': 0, 'source_sha': 'a' * 40,
                                              'source_state': 'measured'}))
        for ledger_name in ('registry', 'consent', 'events', 'wake_attempt', 'wake_attempt_race'):
            with self.subTest(ledger=ledger_name):
                bus = self.base / ('fleet-' + ledger_name)
                root = FloatiRoot.open_direct_home(bus, create=True)
                Registry(root).register('fixture-reader', 'worker')
                workspace = self.base / ('workspace-' + ledger_name)
                workspace.mkdir()
                map_path = bus / 'codex-wait/workspaces.v0.json'
                map_path.parent.mkdir(exist_ok=True)
                map_path.write_text(json.dumps({'schema_version': 0, 'tenant_id': root.tenant_id,
                                               'mappings': [{'workspace': str(workspace), 'node_id': 'fixture-reader'}]}))
                participant = resolve_participant(bus, workspace)
                self.assertIsNotNone(participant)
                consent = CodexWaitConsentLedger(root).arm(participant.binding, hook_timeout_seconds=10,
                                                           wait_deadline_seconds=2, idempotency_key='fixture-consent')
                CodexWaitSessionLedger(root).arm(participant.binding, consent, 'fixture-session',
                                                idempotency_key='fixture-session-arm')
                if ledger_name.startswith('wake_attempt'):
                    from floati.events import EventLog
                    Registry(root).register('fixture-sender', 'worker')
                    EventLog(root).send('fixture-sender', 'fixture-reader', 'floati', 'b' * 40,
                                        'AGENTS.md', 'fixture fresh message', idempotency_key='fixture-fresh')
                relative = {'registry': Path('registry/entries.jsonl'),
                            'consent': consent_ledger_relative('fixture-reader'),
                            'events': Path('events.jsonl'),
                            'wake_attempt': Path('receipts/wakes/fixture-reader.jsonl'),
                            'wake_attempt_race': Path('receipts/wakes/fixture-reader.jsonl')}[ledger_name]
                unknown = dict(self.record, tenant_id=root.tenant_id)
                (bus / relative).parent.mkdir(parents=True, exist_ok=True)
                if ledger_name != 'wake_attempt_race':
                    with (bus / relative).open('ab') as stream:
                        stream.write(encode_frame(unknown))
                env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1')
                env.pop('PYTHONPATH', None)
                command = [sys.executable, '-m', 'floati.codex_wait', '--root', str(bus)]
                if ledger_name == 'wake_attempt_race':
                    # The output boundary introduces newer testimony only after
                    # preflight; no production ledger/reader behavior is mocked.
                    program = '''import sys
from pathlib import Path
from floati.codex_wait import main
from floati.framing import encode_frame
original = sys.stdout
class RaceOutput:
    def write(self, value):
        with Path(sys.argv[1]).open('ab') as stream:
            stream.write(encode_frame(RECORD))
        return original.write(value)
    def flush(self):
        original.flush()
sys.stdout = RaceOutput()
raise SystemExit(main(['--root', sys.argv[2]]))
'''.replace('RECORD', repr(unknown))
                    command = [sys.executable, '-c', program, str(bus / relative), str(bus)]
                result = subprocess.run(command,
                                        input=json.dumps({'cwd': str(workspace), 'session_id': 'fixture-session'}),
                                        cwd=self.runtime, env=env, capture_output=True, text=True, timeout=10)
                self.assertEqual(20, result.returncode, 'waiter swallowed refusal: ' + result.stderr)
                self.assertEqual(1, len(result.stdout.splitlines()), 'waiter emitted more than one JSON artifact')
                if ledger_name == 'wake_attempt_race':
                    self.assertEqual('block', json.loads(result.stdout)['decision'])
                    artifact = json.loads(result.stderr)
                else:
                    artifact = json.loads(result.stdout)
                self.assertEqual('refused', artifact['status'])
                self.assertEqual('reader_older_than_ledger', artifact['evidence']['code'])
                self.assertIn('future_receipt', artifact['evidence']['detail'])
                remedy = artifact['evidence']['remedy']
                self.assertNotIn('--destination ' + str(self.runtime), remedy)
                self.assertIn('codex-fleet-bus <profile> attach', remedy)
                self.assertIn('trust', remedy)

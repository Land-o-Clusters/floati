"""Local role writes preserve validated files and recover their exact receipts."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from floati.errors import ProtocolRefusal
from floati.jsonl import read_records_snapshot
from floati.role_templates import load_role_template
from floati.root import FloatiRoot
from tests.temp_roots import REAL_TEMP_ROOT
from tests.test_role_templates import template_payload


class RoleLibraryTests(unittest.TestCase):
    def setUp(self):
        from floati.role_library import RoleTemplateLibrary
        temporary = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.root = FloatiRoot.open_direct_home(self.base / 'fleet', create=True)
        self.library = RoleTemplateLibrary(self.root)
        self.custom = self.root.path / 'roles/custom'
        self.source = self.base / 'role.json'

    def write_source(self, record=None):
        self.source.write_text(json.dumps(record or template_payload('custom-role')), encoding='utf-8')
        return self.source

    def receipts(self):
        return read_records_snapshot(self.root, 'receipts/role-templates.jsonl',
            allowed_kinds={'role_template_write_receipt'})

    def test_new_custom_role_is_discoverable_and_preserves_shipped_bytes(self):
        shipped = Path(__file__).resolve().parents[1] / 'roles/shipped/builder.json'
        original = shipped.read_bytes()
        receipt = self.library.new('custom-role', from_role='builder', idempotency_key='create-role')
        template = self.library.templates()['custom-role']
        self.assertEqual('custom-role', template.role)
        self.assertEqual(load_role_template(shipped).duties, template.duties)
        self.assertEqual(original, shipped.read_bytes())
        self.assertEqual('applied', receipt['state'])
        self.assertEqual('roles/custom/custom-role.json', receipt['path'])
        self.assertEqual(hashlib.sha256((self.custom / 'custom-role.json').read_bytes()).hexdigest(), receipt['after_sha256'])
        self.assertEqual(template.digest, receipt['template_sha256'])
        self.assertEqual(['prepared', 'applied'], [r['state'] for r in self.receipts()])
        self.assertEqual(self.receipts()[0]['id'], receipt['predecessor_receipt_id'])

    def test_new_and_import_never_replace_shipped_or_existing_names(self):
        with self.assertRaises(ProtocolRefusal):
            self.library.new('builder', from_role='architect', idempotency_key='reserved')
        self.write_source(template_payload('builder'))
        with self.assertRaises(ProtocolRefusal):
            self.library.import_file(self.source, idempotency_key='reserved-import')
        self.assertEqual([], self.receipts())
        self.library.new('custom-role', from_role='builder', idempotency_key='first')
        before = (self.custom / 'custom-role.json').read_bytes()
        with self.assertRaises(ProtocolRefusal):
            self.library.new('custom-role', from_role='architect', idempotency_key='second')
        self.assertEqual(before, (self.custom / 'custom-role.json').read_bytes())
        self.assertEqual(2, len(self.receipts()))

    def test_validate_and_import_share_validator_without_partial_invalid_write(self):
        self.write_source(dict(template_payload('custom-role'), duties=[]))
        for operation in (lambda: self.library.validate_file(self.source),
                          lambda: self.library.import_file(self.source, idempotency_key='invalid')):
            with self.assertRaisesRegex(ProtocolRefusal, 'duties'):
                operation()
        self.assertFalse(self.custom.exists())
        self.assertEqual([], self.receipts())
        self.write_source()
        template = self.library.validate_file(self.source)
        self.assertEqual('custom-role', template.role)
        self.assertEqual([], self.receipts())
        receipt = self.library.import_file(self.source, idempotency_key='valid')
        self.assertEqual(template.digest, receipt['template_sha256'])

    def test_edit_validates_before_replacing_exact_old_bytes(self):
        self.library.new('custom-role', from_role='builder', idempotency_key='new')
        target = self.custom / 'custom-role.json'
        old = target.read_bytes()
        with self.assertRaisesRegex(ProtocolRefusal, 'duties'):
            self.library.edit('custom-role', changes={'duties': []}, idempotency_key='bad-edit')
        self.assertEqual(old, target.read_bytes())
        self.assertEqual(2, len(self.receipts()))
        receipt = self.library.edit('custom-role', changes={'cadence': 'on-demand'}, idempotency_key='edit')
        self.assertEqual(hashlib.sha256(old).hexdigest(), receipt['before_sha256'])
        self.assertEqual('on-demand', load_role_template(target).cadence)
        self.assertEqual(4, len(self.receipts()))

    def test_edit_from_file_cannot_rename_or_overwrite_shipped_role(self):
        self.library.new('custom-role', from_role='builder', idempotency_key='new')
        before = (self.custom / 'custom-role.json').read_bytes()
        self.write_source(template_payload('different-role'))
        with self.assertRaises(ProtocolRefusal):
            self.library.edit('custom-role', from_file=self.source, idempotency_key='rename')
        with self.assertRaises(ProtocolRefusal):
            self.library.edit('builder', changes={'cadence': 'on-demand'}, idempotency_key='shipped')
        self.assertEqual(before, (self.custom / 'custom-role.json').read_bytes())

    def test_idempotent_write_reuses_receipt_and_rejects_changed_request(self):
        first = self.library.new('custom-role', from_role='builder', idempotency_key='same-key')
        self.assertEqual(first, self.library.new('custom-role', from_role='builder', idempotency_key='same-key'))
        self.assertEqual(2, len(self.receipts()))
        with self.assertRaises(ProtocolRefusal):
            self.library.new('custom-role', from_role='architect', idempotency_key='same-key')
        self.assertEqual(2, len(self.receipts()))

    def test_replay_after_file_publication_recovers_receipt_without_rewriting(self):
        original = self.library._append
        def fail_applied(row):
            if row['state'] == 'applied':
                raise OSError('injected receipt failure')
            return original(row)
        with mock.patch.object(self.library, '_append', side_effect=fail_applied):
            with self.assertRaises(OSError):
                self.library.new('custom-role', from_role='builder', idempotency_key='recover')
        target = self.custom / 'custom-role.json'
        before = target.stat().st_mtime_ns
        self.assertEqual(['prepared'], [r['state'] for r in self.receipts()])
        receipt = self.library.new('custom-role', from_role='builder', idempotency_key='recover')
        self.assertEqual('applied', receipt['state'])
        self.assertEqual(before, target.stat().st_mtime_ns)
        self.assertEqual(2, len(self.receipts()))

    def test_recovery_refuses_foreign_bytes_instead_of_overwriting(self):
        original = self.library._append
        def fail_applied(row):
            if row['state'] == 'applied':
                raise OSError('injected receipt failure')
            return original(row)
        with mock.patch.object(self.library, '_append', side_effect=fail_applied):
            with self.assertRaises(OSError):
                self.library.new('custom-role', from_role='builder', idempotency_key='recover')
        target = self.custom / 'custom-role.json'
        target.write_bytes(b'foreign bytes\n')
        with self.assertRaises(ProtocolRefusal):
            self.library.new('custom-role', from_role='builder', idempotency_key='recover')
        self.assertEqual(b'foreign bytes\n', target.read_bytes())
        self.assertEqual(1, len(self.receipts()))

    def test_local_source_symlink_and_url_refuse(self):
        self.write_source()
        link = self.base / 'link.json'
        link.symlink_to(self.source)
        for source in (link, 'https://example.invalid/role.json'):
            with self.subTest(source=str(source)), self.assertRaises(ProtocolRefusal):
                self.library.import_file(source, idempotency_key='bad-source')
        self.assertEqual([], self.receipts())

    def test_custom_parent_symlink_never_writes_outside_root(self):
        outside = self.base / 'outside'
        outside.mkdir()
        (self.root.path / 'roles').mkdir()
        self.custom.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(ProtocolRefusal):
            self.library.new('custom-role', from_role='builder', idempotency_key='bad-target')
        self.assertEqual([], list(outside.iterdir()))
        self.assertEqual([], self.receipts())

    def test_edit_replay_uses_explicit_request_not_new_current_state(self):
        self.library.new('custom-role', from_role='builder', idempotency_key='create')
        changes = {'cadence': 'on-demand'}
        first = self.library.edit('custom-role', changes=changes, idempotency_key='edit-once')
        self.library.edit('custom-role', changes={'cadence': 'on-message'}, idempotency_key='edit-later')
        self.assertEqual(first, self.library.edit('custom-role', changes=changes, idempotency_key='edit-once'))
        self.assertEqual('on-message', self.library.templates()['custom-role'].cadence)
        self.assertEqual(6, len(self.receipts()))

    def test_changed_import_bytes_with_same_key_refuse(self):
        self.write_source()
        self.library.import_file(self.source, idempotency_key='source-key')
        self.source.write_text(self.source.read_text() + '\n')
        with self.assertRaisesRegex(ProtocolRefusal, 'idempotency_conflict'):
            self.library.import_file(self.source, idempotency_key='source-key')
        self.assertEqual(2, len(self.receipts()))

    def test_pending_write_blocks_different_request_until_exact_recovery(self):
        original = self.library._append
        def fail_applied(row):
            if row['state'] == 'applied':
                raise OSError('injected receipt failure')
            return original(row)
        with mock.patch.object(self.library, '_append', side_effect=fail_applied):
            with self.assertRaises(OSError):
                self.library.new('custom-role', from_role='builder', idempotency_key='recover')
        with self.assertRaisesRegex(ProtocolRefusal, 'write_pending'):
            self.library.edit('custom-role', changes={'cadence': 'on-demand'}, idempotency_key='different')
        self.assertEqual(1, len(self.receipts()))
        self.library.new('custom-role', from_role='builder', idempotency_key='recover')
        self.assertEqual(2, len(self.receipts()))

    @unittest.skipUnless(hasattr(os, 'mkfifo'), 'FIFO fixture requires local FIFO support')
    def test_fifo_source_refuses_without_waiting_for_a_writer(self):
        fifo = self.base / 'fifo.json'
        os.mkfifo(fifo)
        with self.assertRaises(ProtocolRefusal):
            self.library.validate_file(fifo)
        self.assertEqual([], self.receipts())

    def test_reserved_custom_filename_refuses_merged_read(self):
        self.custom.mkdir(parents=True)
        (self.custom / 'builder.json').write_text(json.dumps(template_payload('builder')))
        with self.assertRaisesRegex(ProtocolRefusal, 'reserved'):
            self.library.templates()

    def test_missing_and_unknown_field_refusals_name_the_field(self):
        record = template_payload('custom-role')
        del record['cadence']
        self.write_source(record)
        with self.assertRaisesRegex(ProtocolRefusal, 'cadence'):
            self.library.validate_file(self.source)
        self.write_source(dict(template_payload('custom-role'), unknown_field='value'))
        with self.assertRaisesRegex(ProtocolRefusal, 'unknown_field'):
            self.library.validate_file(self.source)

    def test_completed_replay_remains_available_during_later_pending_write(self):
        first = self.library.new('custom-role', from_role='builder', idempotency_key='completed-key')
        original = self.library._append
        def interrupt_after_intent(row):
            result = original(row)
            if row['idempotency_key'] == 'later-key' and row['state'] == 'prepared':
                raise OSError('injected interruption after intent')
            return result
        with mock.patch.object(self.library, '_append', side_effect=interrupt_after_intent):
            with self.assertRaises(OSError):
                self.library.edit('custom-role', changes={'cadence': 'on-demand'}, idempotency_key='later-key')
        target = self.custom / 'custom-role.json'
        before = target.read_bytes()
        self.assertEqual(first, self.library.new('custom-role', from_role='builder', idempotency_key='completed-key'))
        self.assertEqual(before, target.read_bytes())
        self.assertEqual(3, len(self.receipts()))

    def test_publication_fsync_failure_is_uncertain_and_retry_syncs_before_applied(self):
        import stat
        from floati.errors import DurabilityFailure
        self.library.new('custom-role', from_role='builder', idempotency_key='create')
        real_replace, real_fsync = os.replace, os.fsync
        published = False
        def replace(source, destination, **kwargs):
            nonlocal published
            result = real_replace(source, destination, **kwargs)
            if destination == 'custom-role.json':
                published = True
            return result
        def fail_directory_sync(descriptor):
            if published and stat.S_ISDIR(os.fstat(descriptor).st_mode):
                raise OSError('injected directory fsync failure')
            return real_fsync(descriptor)
        with mock.patch('floati.role_library.os.replace', side_effect=replace), mock.patch('floati.role_library.os.fsync', side_effect=fail_directory_sync):
            with self.assertRaises(DurabilityFailure) as caught:
                self.library.edit('custom-role', changes={'cadence': 'on-demand'}, idempotency_key='uncertain')
        self.assertEqual('role_template_publication_unknown', caught.exception.code)
        self.assertEqual('on-demand', load_role_template(self.custom / 'custom-role.json').cadence)
        self.assertEqual(3, len(self.receipts()))
        directory_synced = False
        def track_sync(descriptor):
            nonlocal directory_synced
            if stat.S_ISDIR(os.fstat(descriptor).st_mode):
                directory_synced = True
            return real_fsync(descriptor)
        append = self.library._append
        def require_sync(row):
            if row['state'] == 'applied':
                self.assertTrue(directory_synced, 'recovery must establish directory durability before applied')
            return append(row)
        with mock.patch('floati.role_library.os.fsync', side_effect=track_sync), mock.patch.object(self.library, '_append', side_effect=require_sync):
            result = self.library.edit('custom-role', changes={'cadence': 'on-demand'}, idempotency_key='uncertain')
        self.assertEqual('applied', result['state'])
        self.assertEqual(4, len(self.receipts()))

    def test_directory_creation_retry_syncs_existing_parent_before_applied(self):
        from floati.errors import DurabilityFailure
        root_identity = self.root.path.stat()
        real_fsync = os.fsync
        def fail_root_after_mkdir(descriptor):
            identity = os.fstat(descriptor)
            if (identity.st_dev, identity.st_ino) == (root_identity.st_dev, root_identity.st_ino) and (self.root.path / 'roles').exists():
                raise OSError('injected parent fsync failure after mkdir')
            return real_fsync(descriptor)
        with mock.patch('floati.role_library.os.fsync', side_effect=fail_root_after_mkdir):
            with self.assertRaises(DurabilityFailure):
                self.library.new('custom-role', from_role='builder', idempotency_key='mkdir-recovery')
        self.assertTrue((self.root.path / 'roles').is_dir())
        self.assertEqual(['prepared'], [r['state'] for r in self.receipts()])
        synced = set()
        def track_sync(descriptor):
            identity = os.fstat(descriptor)
            synced.add((identity.st_dev, identity.st_ino))
            return real_fsync(descriptor)
        append = self.library._append
        def require_ancestry(row):
            if row['state'] == 'applied':
                for path in (self.root.path, self.root.path / 'roles', self.custom):
                    identity = path.stat()
                    self.assertIn((identity.st_dev, identity.st_ino), synced)
            return append(row)
        with mock.patch('floati.role_library.os.fsync', side_effect=track_sync), mock.patch.object(self.library, '_append', side_effect=require_ancestry):
            self.library.new('custom-role', from_role='builder', idempotency_key='mkdir-recovery')

    def test_published_recovery_syncs_entire_directory_ancestry(self):
        append = self.library._append
        def fail_applied(row):
            if row['state'] == 'applied':
                raise OSError('injected receipt failure')
            return append(row)
        with mock.patch.object(self.library, '_append', side_effect=fail_applied):
            with self.assertRaises(OSError):
                self.library.new('custom-role', from_role='builder', idempotency_key='chain-recovery')
        synced = set()
        real_fsync = os.fsync
        def track_sync(descriptor):
            identity = os.fstat(descriptor)
            synced.add((identity.st_dev, identity.st_ino))
            return real_fsync(descriptor)
        def require_ancestry(row):
            if row['state'] == 'applied':
                for path in (self.root.path, self.root.path / 'roles', self.custom):
                    identity = path.stat()
                    self.assertIn((identity.st_dev, identity.st_ino), synced)
            return append(row)
        with mock.patch('floati.role_library.os.fsync', side_effect=track_sync), mock.patch.object(self.library, '_append', side_effect=require_ancestry):
            self.library.new('custom-role', from_role='builder', idempotency_key='chain-recovery')

"""Ruled role authoring must produce usable root-local roles without an editor."""
from __future__ import annotations
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from floati.cli import main
from tests.test_role_templates import template_payload


class RoleLibraryCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / 'fleet'
        self.call('init', '--root', str(self.root))

    def call(self, *args, expected=0):
        output = io.StringIO()
        with redirect_stdout(output):
            status = main(list(args))
        artifact = json.loads(output.getvalue())
        self.assertEqual(expected, status, artifact)
        return artifact['evidence']

    def test_new_edit_import_validate_and_existing_consumers_share_one_library(self):
        root = ('--root', str(self.root))
        self.call('role', 'new', *root, '--name', 'specialist', '--from', 'builder', '--idempotency-key', 'new-1')
        shown = self.call('role', 'show', 'specialist', *root)
        self.assertEqual('specialist', shown['template']['role'])
        self.assertIn('specialist', self.call('role', 'list', *root)['roles'])
        self.call('role', 'edit', *root, '--name', 'specialist', '--set', 'cadence=on-demand', '--idempotency-key', 'edit-1')
        self.assertEqual('on-demand', self.call('role', 'show', 'specialist', *root)['template']['cadence'])
        source = self.base / 'custom.json'
        source.write_text(json.dumps(template_payload('local-review')))
        validated = self.call('role', 'validate', *root, '--from', str(source))
        self.assertEqual('local-review', validated['template']['role'])
        self.assertFalse((self.root / 'roles/custom/local-review.json').exists())
        self.call('role', 'import', *root, '--from', str(source), '--idempotency-key', 'import-1')
        self.assertIn('local-review', self.call('role', 'list', *root)['roles'])

    def test_invalid_declarative_edit_preserves_file_and_names_field(self):
        root = ('--root', str(self.root))
        self.call('role', 'new', *root, '--name', 'specialist', '--from', 'builder', '--idempotency-key', 'new-1')
        path = self.root / 'roles/custom/specialist.json'
        before = path.read_bytes()
        result = self.call('role', 'edit', *root, '--name', 'specialist', '--set', 'duties=invalid', '--idempotency-key', 'edit-bad', expected=20)
        self.assertEqual('role_template_invalid', result['code'])
        self.assertIn('duties', result['detail'])
        self.assertEqual(before, path.read_bytes())

    def test_custom_template_is_assignable_through_existing_node_role_verb(self):
        from floati.admin_registry import RegistryAdminBackend
        from floati.root import FloatiRoot
        root = ('--root', str(self.root))
        self.call('role', 'new', *root, '--name', 'specialist', '--from', 'builder', '--idempotency-key', 'new-1')
        template = self.call('role', 'show', 'specialist', *root)['template']
        self.call('register', 'builder-a', *root, '--harness', 'Codex')
        answers = [part for question in template['questions']
                   for part in ('--answer', question['key'] + '=declared-scope')]
        self.call('node', 'role', *root, '--node', 'builder-a', '--template', 'specialist', *answers)
        row = RegistryAdminBackend(FloatiRoot.open_direct_home(self.root)).role_record('builder-a')
        self.assertEqual('specialist', row['template_role'])
        self.assertEqual(self.call('role', 'show', 'specialist', *root)['sha256'], row['template_sha256'])

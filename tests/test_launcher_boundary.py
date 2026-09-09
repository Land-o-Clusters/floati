"""Installed launchers retain caller authority without trusting import or PATH input."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

from floati.registry import Registry
from floati.root import FloatiRoot

SOURCE = Path(__file__).resolve().parents[1]


class LauncherBoundaryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='floati-launcher-')
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.install = self.base / 'installed with spaces'
        shutil.copytree(SOURCE / 'floati', self.install / 'floati',
                        ignore=shutil.ignore_patterns('__pycache__'))
        (self.install / 'scripts').mkdir()
        for name in ('floati', 'floati-quota-statusline'):
            shutil.copy2(SOURCE / 'scripts' / name, self.install / 'scripts' / name)
        self.caller = self.base / 'caller'
        self.caller.mkdir()
        self.env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1')
        self.env.pop('FLOATI_PYTHON', None)
        self.env.pop('PYTHONPATH', None)

    def launch(self, *args, name='floati'):
        return subprocess.run([str(self.install / 'scripts' / name), *args],
                              cwd=self.caller, env=self.env, capture_output=True,
                              text=True, timeout=30)

    def test_installed_send_refuses_the_callers_unpushed_commit(self):
        def git(*args):
            return subprocess.check_output(['/usr/bin/git', '-C', str(self.caller), *args],
                                           stderr=subprocess.DEVNULL, text=True).strip()
        git('init')
        (self.caller / 'README.md').write_text('fixture\n')
        git('add', 'README.md')
        git('-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
            'commit', '-m', 'fixture')
        sha = git('rev-parse', 'HEAD')
        root = FloatiRoot.open_direct_home(self.base / 'fleet', create=True)
        registry = Registry(root)
        registry.register('fixture-sender', 'worker')
        registry.register('fixture-recipient', 'worker')
        args = ['send', '--root', str(root.path), '--from', 'fixture-sender', '--to', 'fixture-recipient',
                '--repo', 'fixture', '--sha', sha, '--doc', 'README.md',
                '--note', 'spaces and "quotes"; literal $value',
                '--idempotency-key', 'launcher-boundary']
        direct = subprocess.run([sys.executable, '-B', '-m', 'floati', *args],
                                cwd=self.caller, env=dict(self.env, PYTHONPATH=str(SOURCE)),
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(20, direct.returncode, direct.stdout + direct.stderr)
        self.assertEqual('sha_unbanked', json.loads(direct.stdout)['evidence']['code'])
        launched = self.launch(*args)
        self.assertEqual(20, launched.returncode, launched.stdout + launched.stderr)
        self.assertEqual('sha_unbanked', json.loads(launched.stdout)['evidence']['code'])
        git('update-ref', 'refs/remotes/origin/main', sha)
        allowed = self.launch(*args)
        self.assertEqual(0, allowed.returncode, allowed.stdout + allowed.stderr)
        self.assertEqual(args[args.index('--note') + 1], json.loads(allowed.stdout)['evidence']['message']['note'])

    def test_installed_inbox_checks_the_callers_seat_declaration(self):
        root = FloatiRoot.open_direct_home(self.base / 'fleet', create=True)
        Registry(root).register('fixture-sender', 'worker')
        (self.caller / 'SEAT.json').write_text(json.dumps({
            'schema_version': 1, 'tenant_id': root.tenant_id, 'root': str(root.path),
            'node_id': 'fixture-recipient', 'topology': 'star', 'coordinator': 'architect',
            'coordinator_authority': ['dispatch_bounded_work'],
            'owner_tier': ['publishing'],
        }))
        result = self.launch('inbox', '--root', str(root.path), '--as', 'fixture-sender',
                             '--session', 'test-session')
        self.assertEqual(20, result.returncode, result.stdout + result.stderr)
        self.assertEqual('workspace_identity_mismatch',
                         json.loads(result.stdout)['evidence']['code'])

    def test_path_dirname_and_python_decoys_never_execute(self):
        fake = self.base / 'bin'
        fake.mkdir()
        marker = self.base / 'path-marker'
        self.env['LAUNCHER_TEST_MARKER'] = str(marker)
        self.env['PATH'] = str(fake) + ':/usr/bin:/bin'
        for name in ('dirname', 'python3'):
            command = fake / name
            command.write_text('#!/bin/sh\nprintf executed > "$LAUNCHER_TEST_MARKER"\nexit 91\n')
            command.chmod(0o700)
        for name in ('floati', 'floati-quota-statusline'):
            with self.subTest(name=name):
                if marker.exists():
                    marker.unlink()
                result = self.launch('--help', name=name)
                self.assertFalse(marker.exists(), name + ' executed a PATH decoy')
                if name == 'floati':
                    self.assertEqual(0, result.returncode, result.stdout + result.stderr)
                else:
                    self.assertEqual(20, result.returncode, result.stdout + result.stderr)
                    self.assertEqual('arguments_invalid', json.loads(result.stderr)['evidence']['code'])

    def test_import_shadowing_cannot_run_before_the_installed_package(self):
        marker = self.base / 'import-marker'
        self.env['LAUNCHER_TEST_MARKER'] = str(marker)
        for location in (self.caller, self.base / 'pythonpath'):
            location.mkdir(exist_ok=True)
            package = location / 'floati'
            package.mkdir()
            payload = 'import os\nopen(os.environ["LAUNCHER_TEST_MARKER"], "w").write("executed")\nraise SystemExit(91)\n'
            (package / '__init__.py').write_text(payload)
            (location / 'sitecustomize.py').write_text(payload)
        self.env['PYTHONPATH'] = str(self.base / 'pythonpath')
        result = self.launch('--help')
        self.assertFalse(marker.exists(), 'untrusted startup/import code executed')
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn('floati', result.stdout)

    def test_quota_executable_ignores_startup_import_injection(self):
        marker = self.base / 'quota-import-marker'
        self.env['LAUNCHER_TEST_MARKER'] = str(marker)
        payload = 'import os\nopen(os.environ["LAUNCHER_TEST_MARKER"], "w").write("executed")\n'
        injected = self.base / 'pythonpath'
        injected.mkdir()
        for location in (self.caller, injected):
            (location / 'sitecustomize.py').write_text(payload)
            package = location / 'floati'
            package.mkdir()
            (package / '__init__.py').write_text(payload + 'raise SystemExit(91)\n')
        self.env['PYTHONPATH'] = str(injected)
        result = self.launch(name='floati-quota-statusline')
        self.assertFalse(marker.exists(), 'quota startup/import code executed')
        self.assertEqual(20, result.returncode, result.stdout + result.stderr)
        self.assertEqual('arguments_invalid', json.loads(result.stderr)['evidence']['code'])

    def test_quota_shebang_explicitly_disables_bytecode_in_isolated_mode(self):
        # macOS system Python defaults can mask the Linux bytecode write.
        # One combined argument is portable across kernel shebang parsers.
        self.assertEqual('#!/usr/bin/python3 -IB',
                         (self.install / 'scripts' / 'floati-quota-statusline')
                         .read_text().splitlines()[0])

    def test_help_does_not_write_bytecode_in_the_install(self):
        self.env.pop('PYTHONDONTWRITEBYTECODE', None)
        for name in ('floati', 'floati-quota-statusline'):
            with self.subTest(name=name):
                result = self.launch('--help', name=name)
                self.assertEqual(0 if name == 'floati' else 20, result.returncode,
                                 result.stdout + result.stderr)
                self.assertEqual([], list(self.install.rglob('*.pyc')))

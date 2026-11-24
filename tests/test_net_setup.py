"""Measure a one-seat setup against the scratch account's actual inventory."""
import contextlib
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

from floati.cli import main
from floati.codex_wait_contract import CodexWaitConsentLedger, resolve_participant
from floati.lifecycle_prompts import REPOSITORY_ROOT
from floati.registry import Registry
from floati.root import FloatiRoot
from tests import test_mcp_stdio
from tests.temp_roots import REAL_TEMP_ROOT


def inventory(home):
    found = {}
    for path in home.rglob('*'):
        if path.is_symlink():
            found[str(path)] = ('symlink', hashlib.sha256(os.fsencode(os.readlink(path))).hexdigest(), 0)
        else:
            mode = stat.S_IMODE(path.stat().st_mode)
            found[str(path)] = ('directory', None, mode) if path.is_dir() else (
                'file', hashlib.sha256(path.read_bytes()).hexdigest(), mode)
    return found


class NetworkSetupTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name).resolve()
        bin_dir = self.home / 'bin'
        bin_dir.mkdir()
        self.codex_home = self.home / '.codex'
        self.codex_home.mkdir()
        (self.codex_home / 'hooks.json').write_bytes(b'{"hooks": {}}\n')
        cursor_home = self.home / '.cursor'
        cursor_home.mkdir()
        cursor_hooks = cursor_home / 'hooks.json'
        cursor_hooks.write_bytes(b'{ "version": 1, "hooks": {"stop": [{"command": "foreign"}]} }\n')
        cursor_hooks.chmod(0o640)
        environment = patch.dict('os.environ', {
            'HOME': str(self.home), 'PATH': str(bin_dir), 'CODEX_HOME': str(self.codex_home)
        })
        environment.start()
        self.addCleanup(environment.stop)

    def cli(self, root, *args, expected=0):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            rc = main(['setup', '--root', str(root), *args])
        self.assertEqual(rc, expected, output.getvalue())
        return json.loads(output.getvalue())['evidence']

    def install(self, root, flags):
        before = inventory(self.home)
        preview = self.cli(root, *flags)
        self.assertEqual(before, inventory(self.home))
        self.assertNotIn('harnesses', preview)
        self.assertIsNone(preview['executable'])
        self.assertEqual('unchecked', preview['executable_status'])
        self.cli(root, *flags, '--confirm', preview['confirmation'])
        after = inventory(self.home)
        self.assertEqual({path: value for path, value in after.items()
                          if value[0] != 'directory' and before.get(path) != value},
                         {row['path']: (row['type'], row['sha256'], row['mode'])
                          for row in preview['files']})
        self.assertEqual({path for path, value in after.items()
                          if value[0] == 'directory' and path not in before}, set(preview['directories']))
        return preview, before

    def test_contended_setup_and_teardown_preserve_the_preview_write_set(self):
        root = self.home / 'fleet'
        flags = ('--seat', 'claude', '--session', 'setup-contention')
        before = inventory(self.home)
        preview = self.cli(root, *flags)
        flock = fcntl.flock
        home_fd = os.open(self.home, os.O_RDONLY | os.O_DIRECTORY)
        try:
            for args in ((*flags, '--confirm', preview['confirmation']), ('--teardown',)):
                with self.subTest(action=args[-1]):
                    flock(home_fd, fcntl.LOCK_SH)
                    blocked = []

                    def release_on_contention(fd, operation):
                        try:
                            return flock(fd, operation)
                        except BlockingIOError:
                            blocked.append(True)
                            flock(home_fd, fcntl.LOCK_UN)
                            raise

                    with patch.object(fcntl, 'flock', side_effect=release_on_contention):
                        self.cli(root, *args)
                    self.assertTrue(blocked)
                    allowed = {row['path'] for row in preview['files']} | set(preview['directories'])
                    self.assertEqual(set(), set(inventory(self.home)) - set(before) - allowed)
        finally:
            os.close(home_fd)
        self.assertEqual(before, inventory(self.home))

    def test_confirm_and_teardown_preserve_a_hook_edit_during_staging(self):
        fsync = os.fsync
        flags = ('--seat', 'codex', '--session', 'setup-edit', '--hook', 'codex')
        for teardown in (False, True):
            home = self.home / ('teardown-home' if teardown else 'confirm-home')
            codex_home = home / '.codex'
            codex_home.mkdir(parents=True)
            hook_path = codex_home / 'hooks.json'
            hook_path.write_bytes(b'{"hooks": {}}\n')
            with self.subTest(teardown=teardown), patch.dict(os.environ, {
                'HOME': str(home), 'CODEX_HOME': str(codex_home),
            }):
                root = home / 'fleet'
                preview = self.cli(root, *flags)
                args = (*flags, '--confirm', preview['confirmation'])
                if teardown:
                    self.cli(root, *args)
                    args = ('--teardown',)
                stage = next(Path(row['path']) for row in preview['temporary_files']
                             if Path(row['path']).parent == hook_path.parent)
                edit = b'{"hooks": {"Stop": []}}\n'

                def fsync_and_save_edit(fd):
                    fsync(fd)
                    if stage.exists() and os.path.samestat(stage.stat(), os.fstat(fd)):
                        hook_path.write_bytes(edit)

                output = io.StringIO()
                with patch.object(os, 'fsync', side_effect=fsync_and_save_edit):
                    with contextlib.redirect_stdout(output):
                        rc = main(['setup', '--root', str(root), *args])
                self.assertEqual(edit, hook_path.read_bytes())
                self.assertEqual(20, rc, output.getvalue())

    def test_default_setup_installs_only_the_chosen_seats_mcp_and_skill(self):
        start = time.monotonic()
        layouts = {
            'claude': ('.mcp.json', '.claude/skills'),
            'codex': ('.codex/config.toml', '.agents/skills'),
            'cursor': ('.cursor/mcp.json', '.cursor/skills'),
            'zcode': ('.zcode/config.json', '.zcode/skills'),
        }
        counts = {}
        for seat, (config_path, skills) in layouts.items():
            with self.subTest(seat=seat):
                root = self.home / 'fleet'
                flags = ('--seat', seat, '--session', 'fixture-session')
                preview, before = self.install(root, flags)
                workspace = root / 'nodes' / seat
                self.assertEqual(preview['workspace'], str(workspace))
                self.assertEqual(preview['hooks'], {})
                self.assertTrue(all(Path(row['path']).is_relative_to(root) for row in preview['files']))
                self.assertTrue((workspace / skills / 'floati/SKILL.md').is_file())
                self.assertEqual(Registry(FloatiRoot.open_direct_home(root)).active_node_ids(), (seat,))
                self.assertIsNone(resolve_participant(root, workspace))
                config = (workspace / config_path).read_text()
                if seat == 'codex':
                    header, command, args = config.splitlines()
                    self.assertEqual(header, '[mcp_servers.floati]')
                    entry = {'command': json.loads(command.split('=', 1)[1]),
                             'args': json.loads(args.split('=', 1)[1])}
                else:
                    config = json.loads(config)
                    servers = config['mcp']['servers'] if seat == 'zcode' else config['mcpServers']
                    entry = servers['floati']
                self.assertEqual(entry['args'], ['mcp', 'serve', '--root', str(root),
                                                '--as', seat, '--session', 'fixture-session'])
                if seat == 'claude':
                    messages = [test_mcp_stdio.McpStdioTests.initialize(1),
                                {'jsonrpc': '2.0', 'method': 'notifications/initialized'},
                                {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'}]
                    completed = subprocess.run([entry['command'], *entry['args']], cwd=workspace,
                        input=''.join(test_mcp_stdio.compact(message) + '\n' for message in messages),
                        text=True, capture_output=True)
                    self.assertEqual(completed.returncode, 0, completed.stderr)
                    ready, catalog = map(json.loads, completed.stdout.splitlines())
                    self.assertEqual(ready['result']['floatiIntegrationPin']['integration_id'], seat)
                    self.assertTrue(catalog['result']['tools'])
                    user_file = workspace / 'notes.txt'
                    user_file.write_text('work from the session')
                    changed = inventory(self.home)
                    refusal = self.cli(root, '--teardown', expected=20)
                    self.assertEqual(refusal['code'], 'setup_teardown_changed')
                    self.assertEqual(changed, inventory(self.home))
                    user_file.unlink()
                self.cli(root, '--teardown')
                self.assertEqual(before, inventory(self.home))
                counts[seat] = len(preview['files'])
        print(f'Default setup inventory files={counts} elapsed={time.monotonic() - start:.3f}s')

    def test_named_hook_opt_in_only_installs_selected_harness_and_restores_home(self):
        start = time.monotonic()
        runtime = self.home / 'runtime'
        for name in ('LICENSE', 'scripts/floati', 'scripts/floati-codex-wait',
                     'floati/codex_wait.py', 'floati/codex_wait_contract.py'):
            destination = runtime / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(REPOSITORY_ROOT / name, destination)
        counts = {}
        for seat in ('claude', 'codex', 'cursor', 'zcode'):
            with self.subTest(seat=seat), patch('floati.network_setup.REPOSITORY_ROOT', runtime):
                root = self.home / 'fleet'
                preview, before = self.install(root, ('--seat', seat, '--session', 'fixture-session',
                                                      '--hook', seat))
                self.assertEqual(set(preview['hooks']), {seat})
                participant = resolve_participant(root, Path(preview['workspace']), _strict=True)
                self.assertEqual(participant.binding.node_id, seat)
                armed = CodexWaitConsentLedger(participant.root).require_armed(participant.binding)
                self.assertEqual(armed['state'], 'armed')
                self.cli(root, '--teardown')
                self.assertEqual(before, inventory(self.home))
                counts[seat] = len(preview['files'])
        print(f'Hook setup inventory files={counts} elapsed={time.monotonic() - start:.3f}s')

    def test_confirmation_cannot_change_selection_session_or_hook_opt_in(self):
        start = time.monotonic()
        root = self.home / 'fleet'
        before = inventory(self.home)
        preview = self.cli(root, '--seat', 'claude', '--session', 'fixture-session')
        for seat, session, hook in (('cursor', 'fixture-session', ()), ('claude', 'other-session', ()),
                                    ('claude', 'fixture-session', ('--hook', 'claude'))):
            with self.subTest(seat=seat, session=session, hook=hook):
                refusal = self.cli(root, '--seat', seat, '--session', session, *hook,
                                   '--confirm', preview['confirmation'], expected=20)
                self.assertEqual(refusal['code'], 'setup_confirmation_changed')
                self.assertEqual(before, inventory(self.home))
        refusal = self.cli(root, '--seat', 'claude', '--session', 'fixture-session',
                           '--hook', 'codex', expected=20)
        self.assertEqual(refusal['code'], 'setup_hook_seat_mismatch')
        outside_calendar = 'f' * 12 + preview['confirmation'][12:]
        refusal = self.cli(root, '--seat', 'claude', '--session', 'fixture-session',
                           '--confirm', outside_calendar, expected=20)
        self.assertEqual(refusal['code'], 'setup_confirmation_invalid')
        self.assertEqual(before, inventory(self.home))
        print(f'Setup confirmation elapsed={time.monotonic() - start:.3f}s')

    def test_explicit_executable_is_validated_and_bound_to_confirmation(self):
        start = time.monotonic()
        root = self.home / 'fleet'
        executable = self.home / 'bin/claude'
        executable.write_text('#!/bin/sh\nexit 1\n')
        executable.chmod(0o700)
        alternate = executable.with_name('alternate')
        shutil.copy2(executable, alternate)
        symlink = executable.with_name('alias')
        symlink.symlink_to(executable)
        flags = ('--seat', 'claude', '--session', 'fixture-session')
        before = inventory(self.home)

        preview = self.cli(root, *flags, '--executable', str(executable))
        self.assertEqual(str(executable), preview['executable'])
        self.assertEqual('path_validated', preview['executable_status'])
        self.assertEqual(before, inventory(self.home))

        confirm = ('--confirm', preview['confirmation'])
        for replacement in ((), ('--executable', str(alternate))):
            with self.subTest(replacement=replacement):
                refusal = self.cli(root, *flags, *replacement, *confirm, expected=20)
                self.assertEqual('setup_confirmation_changed', refusal['code'])
                self.assertEqual(before, inventory(self.home))

        for invalid in ('claude', executable.parent, executable.with_name('missing'),
                        self.codex_home / 'hooks.json', symlink):
            with self.subTest(invalid=invalid):
                refusal = self.cli(root, *flags, '--executable', str(invalid), *confirm, expected=20)
                self.assertEqual('setup_executable_invalid', refusal['code'])
                self.assertEqual(before, inventory(self.home))

        installed = self.cli(root, *flags, '--executable', str(executable), *confirm)
        self.assertEqual('installed', installed['state'])
        self.assertEqual(str(executable), installed['executable'])
        self.cli(root, '--teardown')
        self.assertEqual(before, inventory(self.home))
        print(f'Explicit setup executable elapsed={time.monotonic() - start:.3f}s')

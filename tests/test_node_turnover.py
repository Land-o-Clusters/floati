"""Fresh-session turnover composed from real scratch native services."""
import contextlib
import io
import json
import os
import runpy
import shlex
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from floati.codex_wait_contract import (
    CodexWaitConsentLedger, WORKSPACE_MAP_RELATIVE, resolve_participant,
)
from floati.errors import ProtocolRefusal
from floati.registry import Registry
from floati.root import FloatiRoot
from floati.seat_declaration import FleetGovernance, SeatDeclaration
from floati.stop_hold import StopHoldLedger
from tests.temp_roots import REAL_TEMP_ROOT
from tests.test_prep_clear import _git


@contextlib.contextmanager
def _cursor_demo_fixture():
    demo = runpy.run_path(str(Path(__file__).resolve().parents[1] /
        'scripts/prepare-node-turnover-demo.py'))
    # Keep the owned parent alive: prepare exclusively creates its absent
    # child, and every credential/launch validation uses this fixture root.
    with tempfile.TemporaryDirectory(prefix='floati-turnover-owner-',
            dir=REAL_TEMP_ROOT) as temporary:
        parent = Path(temporary).resolve()
        with mock.patch.dict(demo['prepare'].__globals__,
                {'platform_temp_root': lambda: parent}):
            yield demo, parent / 'floati-turnover-owner-demo'


class CursorDemoCredentialTests(unittest.TestCase):
    """A scratch account authenticates only from its owner's private key file."""

    @classmethod
    def setUpClass(cls):
        stack = contextlib.ExitStack()
        cls.addClassCleanup(stack.close)
        cls.demo, cls.base = stack.enter_context(_cursor_demo_fixture())
        cls.prepared = cls.demo['prepare'](cls.base, 'cursor', None)

    def setUp(self):
        self.account = self.base / 'account'
        self.credential = self.account / 'cursor-api-key'
        self.addCleanup(lambda: self.credential.unlink(missing_ok=True))

    def launch(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = self.demo['main'](['--harness', 'cursor', '--base', str(self.base),
                '--launch-cursor', '/opt/homebrew/bin/cursor-agent'])
        return code, output.getvalue()

    def test_missing_private_key_refuses_before_native_credential_store_access(self):
        with mock.patch('os.execve') as native:
            code, output = self.launch()
        self.assertEqual(20, code)
        result = json.loads(output)
        self.assertEqual('turnover_demo_cursor_credential_required', result['code'])
        self.assertIn('CURSOR_API_KEY', result['remedy'])
        self.assertIn('cursor-api-key', result['remedy'])
        native.assert_not_called()
        self.assertFalse(self.credential.exists())

    def test_owner_key_reaches_only_cursor_child_environment(self):
        self.assertEqual(self.base, self.demo['validate_base'](str(self.base), existing=True))
        secret = 'synthetic-cursor-demo-key'
        self.credential.touch(mode=0o600)
        self.credential.write_text(secret + '\n')
        ambient = {'CURSOR_API_KEY': 'ambient-key', 'CURSOR_AUTH_TOKEN': 'ambient-token',
            'CURSOR_API_URL': 'https://example.invalid', 'XDG_DATA_HOME': '/outside'}
        launched = {}
        def capture(*args):
            launched['cwd'] = Path.cwd()
        with mock.patch.dict(os.environ, ambient), mock.patch('os.execve', side_effect=capture) as native:
            code, output = self.launch()
            self.assertEqual(ambient['CURSOR_API_KEY'], os.environ['CURSOR_API_KEY'])
        self.assertEqual(0, code, output)
        executable, argv, environment = native.call_args.args
        self.assertEqual('/opt/homebrew/bin/cursor-agent', executable)
        self.assertEqual([executable], argv)
        self.assertEqual(secret, environment['CURSOR_API_KEY'])
        self.assertNotIn('CURSOR_AUTH_TOKEN', environment)
        self.assertNotIn('CURSOR_API_URL', environment)
        self.assertNotIn('XDG_DATA_HOME', environment)
        self.assertEqual(str(self.account), environment['HOME'])
        self.assertEqual(self.base / 'seat/repo', launched['cwd'])
        self.assertEqual(str(Path(__file__).resolve().parents[1] / 'scripts'),
            environment['PATH'].split(os.pathsep)[0])
        self.assertNotIn(secret, output + repr(argv))
        self.assertEqual(0o600, self.credential.stat().st_mode & 0o777)

    def test_prepare_prints_owner_save_and_launch_route_without_reading_credentials(self):
        route = self.prepared.get('credential', {})
        self.assertEqual('CURSOR_API_KEY', route.get('environment_variable'))
        self.assertEqual(str(self.credential), route.get('file'))
        self.assertIn('--launch-cursor', route.get('launch_command', ''))
        self.assertIn('cursor.com', route.get('documentation', ''))

    def save_key(self, prompt):
        command = shlex.split(self.prepared['credential']['save_command'])
        output = io.StringIO()
        with contextlib.redirect_stdout(output), mock.patch('getpass.getpass', side_effect=prompt):
            code = self.demo['main'](command[3:])
        return code, output.getvalue()

    def test_owner_save_is_private_exclusive_and_requires_hidden_input(self):
        import getpass
        import warnings
        secret = 'synthetic-owner-saved-key'
        code, output = self.save_key(lambda *_: secret)
        self.assertEqual(0, code, output)
        self.assertEqual(secret + '\n', self.credential.read_text())
        self.assertEqual(0o600, self.credential.stat().st_mode & 0o777)
        code, output = self.save_key(lambda *_: 'replacement')
        self.assertEqual(20, code)
        self.assertEqual(secret + '\n', self.credential.read_text())
        self.assertNotIn(secret, output)
        self.credential.unlink()
        def no_hidden_input(*_):
            warnings.warn('hidden input unavailable', getpass.GetPassWarning)
            return secret
        code, output = self.save_key(no_hidden_input)
        self.assertEqual(20, code)
        self.assertFalse(self.credential.exists())

    def test_owner_save_refuses_changed_account_before_requesting_a_key(self):
        for kind in ('mode', 'account-symlink', 'base-symlink', 'changed-during-prompt'):
            with self.subTest(kind=kind):
                moved = None
                try:
                    if kind == 'mode':
                        self.account.chmod(0o755)
                    elif kind in ('account-symlink', 'base-symlink'):
                        target = self.account if kind == 'account-symlink' else self.base
                        moved = target.with_name(target.name + '-moved')
                        target.rename(moved)
                        target.symlink_to(moved)
                    def value(*_):
                        if kind == 'changed-during-prompt':
                            self.account.chmod(0o755)
                        return 'synthetic-key'
                    prompt = mock.Mock(side_effect=value)
                    code, output = self.save_key(prompt)
                    self.assertEqual(20, code, output)
                    if kind != 'changed-during-prompt':
                        prompt.assert_not_called()
                    self.assertFalse(self.credential.exists())
                finally:
                    if moved is not None:
                        target.unlink()
                        moved.rename(target)
                    self.account.chmod(0o700)

    def test_changed_seat_refuses_without_launch_or_credential_mutation(self):
        registry = Registry(FloatiRoot.open_direct_home(self.base / 'fleet'))
        path = self.base / 'seat/repo/SEAT.json'
        before = path.read_bytes()
        # An otherwise valid prepared base cannot silently become a foreign seat.
        declaration = json.loads(before)
        declaration['node_id'] = 'architect'
        path.write_text(json.dumps(declaration))
        self.addCleanup(path.write_bytes, before)
        with mock.patch('os.execve') as native:
            code, output = self.launch()
        self.assertEqual(20, code)
        self.assertEqual('turnover_demo_cursor_seat_invalid', json.loads(output)['code'])
        native.assert_not_called()
        self.assertEqual('cursor', registry.require_active('seat')['role'])

    def test_unsafe_or_empty_key_files_never_launch(self):
        outside = self.base / 'outside'
        outside.touch(mode=0o600)
        outside.write_text('synthetic-outside-key\n')
        for kind in ('symlink', 'hardlink', 'public', 'empty', 'multiline', 'fifo'):
            with self.subTest(kind=kind):
                self.credential.unlink(missing_ok=True)
                if kind == 'symlink':
                    self.credential.symlink_to(outside)
                elif kind == 'hardlink':
                    os.link(outside, self.credential)
                elif kind == 'fifo':
                    os.mkfifo(self.credential, 0o600)
                else:
                    self.credential.touch(mode=0o600)
                    self.credential.write_text('' if kind == 'empty' else
                        'first\nsecond\n' if kind == 'multiline' else 'synthetic-key\n')
                    if kind == 'public':
                        self.credential.chmod(0o644)
                with mock.patch('os.execve') as native:
                    code, output = self.launch()
                self.assertEqual(20, code)
                self.assertEqual('turnover_demo_cursor_credential_invalid', json.loads(output)['code'])
                native.assert_not_called()
                self.credential.unlink()


class CursorDemoUmaskTests(unittest.TestCase):
    """Prepare each mask once, without the credential class's extra scratch seat."""

    def test_prepared_seat_saves_key_and_launches_under_stock_account_umasks(self):
        for mask in (0o002, 0o022):
            with self.subTest(umask=oct(mask)), _cursor_demo_fixture() as (demo, base):
                prior = os.umask(mask)
                try:
                    demo['prepare'](base, 'cursor', None)
                    output = io.StringIO()
                    with contextlib.redirect_stdout(output), mock.patch(
                            'getpass.getpass', return_value='synthetic-umask-key'):
                        saved = demo['main'](['--harness', 'cursor', '--base',
                            str(base), '--save-cursor-key'])
                    self.assertEqual(0, saved, output.getvalue())
                    output = io.StringIO()
                    with contextlib.redirect_stdout(output), mock.patch('os.execve') as native:
                        launched = demo['main'](['--harness', 'cursor', '--base',
                            str(base), '--launch-cursor', '/opt/homebrew/bin/cursor-agent'])
                    self.assertEqual(0, launched, output.getvalue())
                    self.assertEqual('synthetic-umask-key',
                        native.call_args.args[2]['CURSOR_API_KEY'])
                finally:
                    os.umask(prior)


class ZcodeDemoEntrypointTests(unittest.TestCase):
    def test_owner_can_prepare_turnover_with_an_explicit_terminal_executable(self):
        demo = runpy.run_path(str(Path(__file__).resolve().parents[1] /
            'scripts/prepare-node-turnover-demo.py'))
        with tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT) as temporary:
            directory = Path(temporary).resolve()
            standalone = directory / 'ZCode Terminal'
            launched = directory / 'launched'
            standalone.write_text('#!/bin/sh\ntouch ' + shlex.quote(str(launched)) + '\n')
            standalone.chmod(0o700)
            base = directory / 'floati-turnover-owner-zcode'
            args = ['--harness', 'zcode', '--base', str(base), '--zcode-executable']
            with mock.patch.dict(demo['validate_base'].__globals__,
                    {'platform_temp_root': lambda: directory}),\
                    mock.patch('floati.adapters.zcode._DEFAULT_COMMAND',
                    ('/usr/bin/node', str(directory / 'ZCode.app/Contents/Resources/glm/zcode.cjs'))):
                for invalid in ('zcode', str(directory), str(directory / 'missing')):
                    with self.subTest(executable=invalid), contextlib.redirect_stdout(io.StringIO()):
                        self.assertEqual(20, demo['main'](args + [invalid]))
                        self.assertFalse(base.exists())
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    code = demo['main'](args + [str(standalone)])
            result = json.loads(output.getvalue())
            self.assertEqual(0, code, result)
            self.assertEqual([str(standalone)], result['launch_argv'])
            self.assertEqual(str(base / 'account'), result['launch_environment']['HOME'])
            prepared = {'workspace': Path(result['workspace'])}
            # The preparer's workspace is synthetic: it must sit under this test's
            # temporary directory, and the joins below read it as such.
            self.assertTrue(prepared['workspace'].is_relative_to(directory), prepared)
            self.assertTrue((prepared['workspace'] / '.zcode/hooks.json').is_file())
            self.assertTrue((prepared['workspace'] / 'boot-seat.md').is_file())
            self.assertTrue((prepared['workspace'] / 'teardown-seat.md').is_file())
            self.assertFalse(launched.exists())

    def test_missing_interactive_runtime_refuses_before_preparing_a_seat(self):
        import plistlib
        import subprocess

        demo = runpy.run_path(str(Path(__file__).resolve().parents[1] /
            'scripts/prepare-node-turnover-demo.py'))
        with tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT) as temporary:
            directory = Path(temporary)
            entry = directory / 'ZCode.app/Contents/Resources/glm/zcode.cjs'
            entry.parent.mkdir(parents=True)
            entry.write_text('async function tui() { return import("@zcode/tui"); }\n')
            (entry.parents[2] / 'Info.plist').write_bytes(plistlib.dumps({
                'CFBundleShortVersionString': '3.17.1'}))
            base = directory / 'floati-turnover-owner-zcode'
            output = io.StringIO()
            with mock.patch.dict(demo['validate_base'].__globals__,
                    {'platform_temp_root': lambda: directory}),\
                    mock.patch('floati.adapters.zcode._DEFAULT_COMMAND',
                    ('/usr/bin/node', str(entry))), mock.patch('subprocess.run', return_value=
                    subprocess.CompletedProcess([], 0, '{"code":"ERR_MODULE_NOT_FOUND"}', '')),\
                    contextlib.redirect_stdout(output):
                code = demo['main'](['--harness', 'zcode', '--base', str(base)])
            result = json.loads(output.getvalue())
            self.assertEqual(20, code, result)
            self.assertEqual('zcode_entrypoint_unavailable', result['code'])
            self.assertIn('3.17.1', result['detail'])
            self.assertIn('@zcode/tui', result['detail'])
            self.assertFalse(base.exists(), 'unusable terminal must refuse before setup writes')


class NodeTurnoverTests(unittest.TestCase):
    def setUp(self):
        self.base = Path(tempfile.mkdtemp(dir=REAL_TEMP_ROOT))
        self.addCleanup(shutil.rmtree, self.base, True)
        self.home = self.base / 'seat'
        self.workspace = self.home / 'repo'
        self.workspace.mkdir(parents=True)
        self.account = self.base / 'account'
        self.account.mkdir()
        self.env = mock.patch.dict(os.environ, {'HOME': str(self.account),
            'CODEX_HOME': str(self.account / '.codex')})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.root = FloatiRoot.open_direct_home(self.base / 'fleet', create=True)
        Registry(self.root).register('architect', 'architect')
        self.governance = FleetGovernance.create(self.root, topology='star',
            coordinator='architect', coordinator_authority=['dispatch_bounded_work'],
            owner_tier=['publishing'])
        self.source = Path(__file__).resolve().parents[1]

    @contextlib.contextmanager
    def cwd(self, path):
        prior = Path.cwd()
        os.chdir(path)
        try:
            yield
        finally:
            os.chdir(prior)

    def service(self, harness='cursor'):
        Registry(self.root).register('seat', harness)
        SeatDeclaration.publish_declared_workspace(self.workspace, 'seat', self.root, self.governance)
        from floati.node_turnover import NodeTurnover
        return NodeTurnover.from_workspace(self.workspace)

    def boot(self, service, session='fresh-session', **options):
        # The clock seam bounds a REAL native first hold without waiting 25 min.
        clock = [0.0]
        def advance(*_):
            clock[0] = 100000.0
            return False
        with self.cwd(self.workspace):
            return service.boot(session, runtime=self.source,
                monotonic=lambda: clock[0], sleep=advance, wait_readable=advance, **options)

    def test_fresh_boot_boards_arms_verifies_and_holds_for_each_supported_harness(self):
        from floati.node_turnover import NodeTurnover
        for harness in ('cursor', 'codex', 'zcode'):
            with self.subTest(harness=harness):
                if harness != 'cursor':
                    self.setUp()
                service = self.service(harness)
                if harness == 'codex':
                    # Owner setup acknowledges the exact scratch Stop definition.
                    from floati.codex_hook_install import CodexHookInstaller
                    from tests.test_codex_hook_install import operator_pair, expected_stop_block
                    hooks = self.account / '.codex/hooks.json'
                    hooks.parent.mkdir(exist_ok=True)
                    hooks.write_text('{"hooks":{"Stop":[]}}\n')
                    store = self.account / '.local/share/floati/waiter'
                    installer = CodexHookInstaller(source_root=self.source,
                        bus_home=self.root.path, hooks_path=hooks, destination=store)
                    installed = installer.install(self.workspace, 'seat', hook_timeout_seconds=1800,
                        wait_deadline_seconds=1500, acknowledge_stop_hook_change=operator_pair(
                            None, expected_stop_block(store, self.root.path, 1800)))
                    # Construct only scratch harness testimony; never grant live trust.
                    (hooks.parent / 'config.toml').write_text('[hooks.state.' +
                        json.dumps(installed['hook_trust_key']) + ']\ntrusted_hash = ' +
                        json.dumps(installed['hook_trust_current_hash']) + '\n')
                first = self.boot(service, 'fresh-' + harness)
                self.assertEqual('completed', first['board']['step'])
                self.assertEqual('verified', first['hook']['state'])
                self.assertEqual('start', first['first_hold']['phase'])
                self.assertEqual(harness, first['first_hold']['harness'])
                holds = StopHoldLedger(self.root).read('seat')
                self.assertTrue(any(r['phase'] == 'exit' and r['invocation_id'] ==
                    first['first_hold']['invocation_id'] for r in holds))
                self.assertEqual(first, self.boot(service, 'fresh-' + harness))
                self.assertEqual(len(holds), len(StopHoldLedger(self.root).read('seat')))
                # Exact activation retirement for the next independent harness case.
                from floati.wake_control import WakeController
                from floati.seat_waiter_lifecycle import retire_waiter
                WakeController(self.root).pause('seat', 'fresh-' + harness,
                    idempotency_key='pause-' + harness)
                retire_waiter(self.root, 'seat', self.workspace, 'retire-' + harness)

    def test_cursor_user_stop_after_boot_holds_from_account_hook_directory(self):
        from floati import cursor_user_hook
        from floati.cursor_wait import run_cursor_stop_wait

        service = self.service('cursor')
        boot_clock = [0.0]
        def finish_first_hold(*_):
            boot_clock[0] = 1500.0
            return False
        with self.cwd(self.workspace):
            booted = service.boot('fresh-session', runtime=self.source,
                monotonic=lambda: boot_clock[0], sleep=finish_first_hold,
                wait_readable=finish_first_hold)
        payload = {'hook_event_name': 'stop', 'status': 'completed', 'loop_count': 0,
            'conversation_id': 'fresh-session', 'workspace_roots': [str(self.workspace)]}
        raw = json.dumps(payload).encode()
        output, errors = io.StringIO(), io.StringIO()

        # Direct invocation from an undeclared directory still fails closed.
        with self.cwd(self.account):
            run_cursor_stop_wait(root=self.root.path, node='seat', runtime=self.source,
                deadline_seconds=1500, poll_seconds=1, hook_timeout_seconds=1800,
                payload=payload, stdout=output, stderr=errors)
        self.assertIn('cursor_wait_workspace_unbound', output.getvalue() + errors.getvalue())
        self.assertEqual(1, len([row for row in StopHoldLedger(self.root).read('seat')
            if row['phase'] == 'start']))
        output, errors = io.StringIO(), io.StringIO()

        def wait_from_child_directory(argv, *, input, cwd=None):
            # Model subprocess cwd semantics while bounding the real waiter by
            # its clock seam. No Cursor process or wall-clock wait is needed.
            clock = [0.0]
            def advance(*_):
                clock[0] = 100000.0
                return False
            with self.cwd(Path.cwd() if cwd is None else Path(cwd)):
                code = run_cursor_stop_wait(root=self.root.path, node='seat',
                    runtime=self.source, deadline_seconds=1500, poll_seconds=1,
                    hook_timeout_seconds=1800, payload=json.loads(input),
                    stdout=output, stderr=errors, clock=lambda: clock[0],
                    sleep=advance, wait_readable=advance)
            return type('Completed', (), {'returncode': code})()

        # Cursor runs user hooks from ~/.cursor, independently of the project
        # hook. The marker must route the child back to the booted workspace.
        with self.cwd(self.account / '.cursor'), mock.patch.object(
                cursor_user_hook.sys, 'stdin', io.TextIOWrapper(io.BytesIO(raw))),\
                mock.patch.object(cursor_user_hook.subprocess, 'run',
                    side_effect=wait_from_child_directory):
            self.assertEqual(0, cursor_user_hook.main())
        self.assertNotIn('cursor_wait_workspace_unbound', output.getvalue() + errors.getvalue())
        starts = [row for row in StopHoldLedger(self.root).read('seat') if row['phase'] == 'start']
        self.assertEqual(2, len(starts), output.getvalue() + errors.getvalue())
        self.assertNotEqual(booted['first_hold']['invocation_id'], starts[-1]['invocation_id'])
        self.assertEqual(str(self.workspace), starts[-1]['workspace'])

    def test_wrong_workspace_refuses_before_creating_activation(self):
        from floati.node_turnover import NodeTurnover
        with self.assertRaises(ProtocolRefusal) as caught:
            NodeTurnover.from_workspace(self.account, node='seat', root=self.root.path)
        self.assertEqual('node_boot_workspace_unrooted', caught.exception.code)
        self.assertIn('open', caught.exception.remedy.lower())
        self.assertIsNone(resolve_participant(self.root.path, self.account))

    def test_claude_boot_requires_explicit_settings_without_home_discovery(self):
        service = self.service('claude')
        with mock.patch.object(Path, 'home', side_effect=AssertionError('home discovery')):
            with self.assertRaises(ProtocolRefusal) as caught:
                self.boot(service)
        self.assertEqual('claude_hook_settings_required', caught.exception.code)
        self.assertIsNone(resolve_participant(self.root.path, self.workspace))

    def test_claude_fresh_boot_journals_board_consent_verified_hook_and_first_hold_once(self):
        service = self.service('claude')
        settings = self.account / 'explicit-claude-settings.json'
        foreign = b'"foreign" : {"unicode":"\\u00e9", "number":1e2}'
        settings.write_bytes(b'{ ' + foreign + b' }\n')
        with mock.patch.object(Path, 'home', side_effect=AssertionError('home discovery')):
            first = self.boot(service, hooks=settings)
            holds = StopHoldLedger(self.root).read('seat')
            shared = settings.read_bytes()
            self.assertEqual(first, self.boot(service, hooks=settings))
        self.assertEqual('completed', first['board']['step'])
        self.assertEqual('verified', first['hook']['state'])
        self.assertEqual(str(settings), first['hook']['path'])
        self.assertEqual('claude', first['first_hold']['harness'])
        self.assertEqual('start', first['first_hold']['phase'])
        self.assertTrue(any(r['phase'] == 'exit' and r['invocation_id'] ==
            first['first_hold']['invocation_id'] for r in holds))
        self.assertEqual(holds, StopHoldLedger(self.root).read('seat'))
        self.assertEqual(shared, settings.read_bytes())
        self.assertIn(foreign, shared)
        participant = resolve_participant(self.root.path, self.workspace)
        self.assertEqual(first['consent_receipt_id'],
            CodexWaitConsentLedger(self.root).require_armed(participant.binding)['id'])
        for verb in ('boot', 'teardown'):
            command = (self.workspace / '.claude/commands' / (verb + '-seat.md')).read_text()
            self.assertIn('floati node ' + verb, command)
            self.assertNotIn(str(self.root.path), command)
            self.assertNotIn(str(settings), command)

    def test_claude_rooted_elsewhere_refuses_before_hook_or_activation_write(self):
        service = self.service('claude')
        settings = self.account / 'explicit-settings.json'
        settings.write_bytes(b'{"foreign":true}\n')
        with self.cwd(self.account), self.assertRaises(ProtocolRefusal) as caught:
            service.boot('fresh-session', hooks=settings)
        self.assertEqual('node_boot_workspace_unrooted', caught.exception.code)
        self.assertIn(str(self.workspace), caught.exception.remedy)
        self.assertEqual(b'{"foreign":true}\n', settings.read_bytes())
        self.assertIsNone(resolve_participant(self.root.path, self.workspace))

    def test_claude_first_launch_foreign_writes_boot_without_rewriting_settings(self):
        from floati.claude_hook_install import install_claude_hook

        service = self.service('claude')
        settings = self.account / 'explicit-settings.json'
        foreign = {'hooks': [{'type': 'command', 'command': '/usr/bin/true'}]}
        settings.write_text(json.dumps({'hooks': {'Stop': [foreign]}}))
        install_claude_hook(workspace=self.workspace, root=self.root.path,
            node=service.node, runtime=self.source, hooks=settings)
        document = json.loads(settings.read_bytes())
        document.update(theme='dark', preferences={'unicode': 'é', 'number': 100})
        document['hooks']['Stop'][0]['hooks'][0]['command'] = '/usr/bin/false'
        document['hooks']['SessionStart'].insert(0, foreign)
        changed = (json.dumps(document, indent=4, sort_keys=True) + '\n').encode()
        settings.write_bytes(changed)
        before = settings.stat()
        result = self.boot(service, hooks=settings)
        self.assertEqual('completed', result['board']['step'])
        self.assertEqual('verified', result['hook']['state'])
        self.assertEqual(changed, settings.read_bytes())
        self.assertEqual((before.st_ino, before.st_mtime_ns),
            (settings.stat().st_ino, settings.stat().st_mtime_ns))

    def test_claude_boot_refuses_changed_missing_or_duplicate_owned_entries(self):
        from floati.claude_hook_install import install_claude_hook

        service = self.service('claude')
        settings = self.account / 'explicit-settings.json'
        install_claude_hook(workspace=self.workspace, root=self.root.path,
            node=service.node, runtime=self.source, hooks=settings)
        original = settings.read_bytes()
        for event in ('Stop', 'SessionStart', 'PostToolUse', 'UserPromptSubmit'):
            for change in ('command', 'timeout', 'matcher', 'missing', 'duplicate'):
                with self.subTest(event=event, change=change):
                    document = json.loads(original)
                    blocks = document['hooks'][event]
                    if change == 'missing':
                        blocks.clear()
                    elif change == 'duplicate':
                        blocks.append(blocks[0])
                    elif change == 'matcher':
                        blocks[0]['matcher'] = 'never'
                    else:
                        blocks[0]['hooks'][0][change] = 'changed'
                    changed = json.dumps(document, indent=2).encode()
                    settings.write_bytes(changed)
                    with self.assertRaises(ProtocolRefusal) as caught:
                        self.boot(service, hooks=settings)
                    self.assertEqual('claude_hook_settings_changed', caught.exception.code)
                    self.assertIn('hooks.' + event, caught.exception.detail)
                    self.assertEqual(changed, settings.read_bytes())
                    self.assertEqual([], StopHoldLedger(self.root).read(service.node))

    def test_claude_identity_free_cli_boot_uses_explicit_settings(self):
        from floati.cli import main
        from floati.codex_wait import run_stop_waiter
        self.service('claude')
        settings = self.account / 'explicit-settings.json'
        clock = [0.0]
        def advance(*_):
            clock[0] = 100000.0
            return False
        def bounded(**options):
            options.update(monotonic=lambda: clock[0], sleep=advance, wait_readable=advance)
            return run_stop_waiter(**options)
        output = io.StringIO()
        with self.cwd(self.workspace), contextlib.redirect_stdout(output), mock.patch(
                'floati.codex_wait.run_stop_waiter', side_effect=bounded):
            code = main(['node', 'boot', '--session', 'native-claude',
                '--settings', str(settings)])
        self.assertEqual(0, code, output.getvalue())
        artifact = json.loads(output.getvalue())
        self.assertEqual('completed', artifact['evidence']['board']['step'])
        self.assertEqual('claude', artifact['evidence']['first_hold']['harness'])

    def test_identity_free_cli_refuses_an_unrooted_session(self):
        from floati.cli import main
        output = io.StringIO()
        with self.cwd(self.account), contextlib.redirect_stdout(output):
            code = main(['node', 'boot', '--session', 'fresh'])
        self.assertEqual(20, code)
        self.assertEqual('node_boot_workspace_unrooted', json.loads(output.getvalue())['evidence']['code'])

    def prepare_trusted_codex(self):
        from floati.codex_hook_install import CodexHookInstaller
        from tests.test_codex_hook_install import operator_pair, expected_stop_block
        hooks = self.account / '.codex/hooks.json'
        hooks.parent.mkdir(exist_ok=True)
        hooks.write_text('{"hooks":{"Stop":[]}}\n')
        store = self.account / '.local/share/floati/waiter'
        installed = CodexHookInstaller(source_root=self.source, bus_home=self.root.path,
            hooks_path=hooks, destination=store).install(self.workspace, 'seat',
                hook_timeout_seconds=1800, wait_deadline_seconds=1500,
                acknowledge_stop_hook_change=operator_pair(None,
                    expected_stop_block(store, self.root.path, 1800)))
        (hooks.parent / 'config.toml').write_text('[hooks.state.' +
            json.dumps(installed['hook_trust_key']) + ']\ntrusted_hash = ' +
            json.dumps(installed['hook_trust_current_hash']) + '\n')

    def test_roleless_codex_boot_names_global_preparation_before_host_trust(self):
        from floati.cli import main
        service = self.service('codex')
        with self.assertRaises(ProtocolRefusal) as refused:
            self.boot(service)
        self.assertEqual('node_boot_hook_setup_required', refused.exception.code)
        command = shlex.split(refused.exception.remedy)
        self.assertEqual(['floati', 'hook', 'install'], command[:3])
        self.assertIn('--prepare-only', command)
        self.assertIn('--acknowledge-stop-hook-change', command)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = main(command[1:])
        self.assertEqual(0, code, output.getvalue())
        prepared = json.loads(output.getvalue())['evidence']
        self.assertIsNone(resolve_participant(self.root.path, self.workspace))
        with self.assertRaises(ProtocolRefusal) as untrusted:
            self.boot(service)
        self.assertEqual('stop_hook_untrusted', untrusted.exception.code)
        hooks = self.account / '.codex/hooks.json'
        (hooks.parent / 'config.toml').write_text('[hooks.state.' +
            json.dumps(prepared['hook_trust_key']) + ']\ntrusted_hash = ' +
            json.dumps(prepared['hook_trust_current_hash']) + '\n')
        first = self.boot(service)
        self.assertEqual('completed', first['board']['step'])
        self.assertEqual('start', first['first_hold']['phase'])
        from floati.admin_registry import RegistryAdminBackend
        self.assertIsNone(RegistryAdminBackend(self.root).current_role('seat'))

    def test_teardown_preserves_node_and_shared_hook_then_next_session_boards(self, harness='cursor'):
        service = self.service(harness)
        if harness == 'codex':
            self.prepare_trusted_codex()
        options = {}
        if harness == 'claude':
            settings = self.account / 'explicit-claude-settings.json'
            settings.write_bytes(b'{ "foreign" : [1e2, "\\u00e9"], "hooks":{"Stop":[]} }\n')
            options['hooks'] = settings
        if harness == 'claude':
            from floati.waiter_consent import arm_waiter_consent
            from floati.zcode_hook_install import ZCODE_HOLD_SECONDS
            other = self.base / 'other-seat'
            other.mkdir()
            Registry(self.root).register('other', 'claude')
            arm_waiter_consent(self.root.path, other, 'other', 'claude', *ZCODE_HOLD_SECONDS)
            other_participant = resolve_participant(self.root.path, other)
            other_consent = CodexWaitConsentLedger(self.root).require_armed(other_participant.binding)
        self.boot(service, **options)
        hook = (self.account / '.cursor/hooks.json' if harness == 'cursor' else
            self.account / '.codex/hooks.json' if harness == 'codex' else
            settings if harness == 'claude' else
            self.workspace / '.zcode/hooks.json')
        shared = hook.read_bytes()
        if harness == 'claude':
            other_participant = resolve_participant(self.root.path, other)
        # Teardown's existing clean/banked work precondition is intentionally real.
        _git(['init', '-q', '-b', 'main', str(self.workspace)], self.base)
        remote = self.base / 'origin.git'
        _git(['init', '-q', '--bare', str(remote)], self.base)
        _git(['remote', 'add', 'origin', str(remote)], self.workspace)
        (self.workspace / 'report.md').write_text('scratch completed work\n')
        _git(['add', '.'], self.workspace)
        _git(['commit', '-q', '-m', 'Bank scratch work'], self.workspace)
        _git(['push', '-q', '-u', 'origin', 'main'], self.workspace)
        sha = _git(['rev-parse', 'HEAD'], self.workspace).stdout.strip()
        activity = dict(workspace_set=[str(self.workspace)], worktrees=[{
            'workspace':str(self.workspace), 'head':sha, 'tracked_changes':[],
            'untracked_files':[], 'unpushed_commits':[]}], native_jobs=[], native_unknown=[],
            native_roots=[str(self.home)], exempt_ancestry=[os.getpid()],
            native_scope={'population':'invoking_uid','uid':os.getuid()})
        with mock.patch('floati.seat_teardown._inspect_activity', return_value=activity):
            result = service.teardown('fresh-session', repo='floati', doc='report.md')
            self.assertEqual(result, service.teardown('fresh-session', repo='floati', doc='report.md'))
        self.assertEqual('completed', result['teardown']['step'])
        self.assertEqual('disarmed', result['activation']['state'])
        self.assertIsNone(resolve_participant(self.root.path, self.workspace))
        self.assertEqual('active', Registry(self.root).require_active('seat')['state'])
        self.assertEqual(shared, hook.read_bytes())
        if harness == 'claude':
            retained = resolve_participant(self.root.path, other)
            self.assertEqual(other_participant.binding.node_id, retained.binding.node_id)
            self.assertEqual(other_participant.binding.workspace, retained.binding.workspace)
            self.assertEqual(other_consent,
                CodexWaitConsentLedger(self.root).require_armed(other_participant.binding))
        self.assertFalse((self.workspace / '.cursor/floati-seat.json').exists())
        self.assertEqual('completed', self.boot(service, 'next-session', **options)['board']['step'])
        participant = resolve_participant(self.root.path, self.workspace)
        consent = CodexWaitConsentLedger(self.root).require_armed(participant.binding)
        activation_paths = [self.root.path / WORKSPACE_MAP_RELATIVE]
        if harness == 'cursor':
            activation_paths.append(self.workspace / '.cursor/floati-seat.json')
        before = {path: path.read_bytes() for path in activation_paths}
        with self.assertRaises(ProtocolRefusal) as caught:
            self.boot(service, 'fresh-session', **options)
        self.assertEqual('node_boot_session_ended', caught.exception.code)
        self.assertEqual(before, {path: path.read_bytes() for path in activation_paths})
        self.assertEqual(consent, CodexWaitConsentLedger(self.root).require_armed(participant.binding))
        with self.assertRaises(ProtocolRefusal) as caught:
            service.teardown('fresh-session', repo='floati', doc='report.md')
        self.assertEqual('node_teardown_session_superseded', caught.exception.code)
        self.assertEqual(participant, resolve_participant(self.root.path, self.workspace))
        self.assertEqual(before, {path: path.read_bytes() for path in activation_paths})

    def test_codex_teardown_preserves_hook_and_next_session_boards(self):
        cases = ('codex', 'zcode', 'claude')
        for index, harness in enumerate(cases):
            with self.subTest(harness=harness):
                if index:
                    self.doCleanups()
                    self.setUp()
                self.test_teardown_preserves_node_and_shared_hook_then_next_session_boards(harness)

    def test_untrusted_codex_hook_refuses_before_boarding(self):
        service = self.service('codex')
        from floati.codex_hook_install import CodexHookInstaller
        from tests.test_codex_hook_install import operator_pair, expected_stop_block
        hooks = self.account / '.codex/hooks.json'
        hooks.parent.mkdir()
        hooks.write_text('{"hooks":{"Stop":[]}}\n')
        store = self.account / '.local/share/floati/waiter'
        CodexHookInstaller(source_root=self.source, bus_home=self.root.path,
            hooks_path=hooks, destination=store).install(self.workspace, 'seat',
                hook_timeout_seconds=1800, wait_deadline_seconds=1500,
                acknowledge_stop_hook_change=operator_pair(None,
                    expected_stop_block(store, self.root.path, 1800)))
        with self.assertRaises(ProtocolRefusal) as caught:
            self.boot(service)
        self.assertEqual('stop_hook_untrusted', caught.exception.code)
        self.assertFalse((self.root.path / 'receipts/seat-board/seat.jsonl').exists())

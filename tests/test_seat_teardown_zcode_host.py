"""Teardown inside a zcode host must separate hosting from workspace work.

Only native observations and the adapter's default Node path are faked. Seat
declarations, adapter binding, config, Git, gates and receipts are real fixtures.
"""
import json
import os
import time
import unittest
from contextlib import contextmanager, nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from floati.errors import ProtocolRefusal
from floati.registry import Registry
from floati.seat_teardown import SeatTeardown
from tests import test_seat_teardown as fixture
from tests.test_prep_clear import _git


class SeatTeardownZcodeHostTests(unittest.TestCase):
    def setUp(self):
        register = Registry.register

        def zcode_seat(registry, node, role):
            return register(registry, node, 'zcode' if node == fixture.NODE else role)

        with patch.object(Registry, 'register', zcode_seat):
            fixture.SeatTeardownTests.setUp(self)
        self.runtime, self.tool_host, self.mcp, self.python_mcp = range(987601, 987605)
        self.job = 987605
        self.grandchild = 987606
        self.node = self.base / 'node'
        self.node.write_text('fixture node\n')
        self.node.chmod(0o755)
        self.entry = self.base / 'ZCode.app/Contents/Resources/glm/zcode.cjs'
        self.entry.parent.mkdir(parents=True)
        self.entry.write_text('fixture declared entry\n')
        self.entry.chmod(0o755)
        self.plugin = self.entry.parent / 'packages/node-repl-host/dist/mcp/server.js'
        self.plugin.parent.mkdir(parents=True)
        self.plugin.write_text('fixture bundled tool host\n')
        self.plugin_manifest = self.plugin.parents[2] / '.zcode-plugin/plugin.json'
        self.plugin_manifest.parent.mkdir(parents=True)
        self.plugin_manifest.write_text(json.dumps({'name': 'node-repl-host', 'version': '0.6.1'}))
        browser = self.entry.parent / 'packages/browser-use-plugin/.zcode-plugin/plugin.json'
        browser.parent.mkdir(parents=True)
        browser.write_text(json.dumps({'name': 'browser-use', 'version': '0.5.1'}))
        self.config = self.base / '.zcode/cli/config.json'
        self.config.parent.mkdir(parents=True)
        self.config.write_text(json.dumps({'provider': {'apiKey': 'PRIVATE-FIXTURE-CREDENTIAL'},
            'mcp': {'servers': {
            'node-tools': {'type': 'stdio', 'command': str(self.node), 'args': ['/fixture/tools.js']},
            'python-tools': {'type': 'stdio', 'command': '/fixture/python', 'args': ['/fixture/tools.py']}
        }}}))
        from floati.wake_daemon_contract import AdapterBindingStore, DaemonCoordinate
        from floati.wake_daemon_adapters import adapter_contract_digest
        self.adapter_binding = AdapterBindingStore(self.root).write(
            DaemonCoordinate(self.root, fixture.NODE, 'zcode'),
            session_id=fixture.SESSION, workspace=self.workspace, executable=self.entry,
            adapter_version='1', adapter_digest=adapter_contract_digest('zcode'), binding_epoch=1)
        self.arguments = {
            self.runtime: ([str(self.node), str(self.entry), '--resume', fixture.SESSION,
                            '--prompt', 'private prompt'], str(self.base)),
            self.tool_host: ([str(self.node), str(self.entry), '__zcode-plugin-host', str(self.plugin)], str(self.base)),
            self.mcp: ([str(self.node), '/fixture/tools.js'], str(self.base)),
            self.python_mcp: (['/fixture/python', '/fixture/tools.py'], str(self.base)),
            self.job: ([str(self.node), '/fixture/build.js'], str(self.base)),
            self.grandchild: ([str(self.node), '/fixture/tools.js'], str(self.base)),
        }
        self.ancestry = [os.getpid(), self.tool_host, self.runtime, 1]
        # The acting runtime hosts the command through a tool server and
        # starts two additional MCP servers in the same workspace.
        self.processes = {
            1: dict(self._process(1, 0, '/fixture/init'), uid=0, process_group=1),
            self.runtime: self._process(self.runtime, 1, str(self.node)),
            self.tool_host: self._process(self.tool_host, self.runtime, str(self.node)),
            self.mcp: self._process(self.mcp, self.runtime, str(self.node)),
            self.python_mcp: self._process(self.python_mcp, self.runtime, '/fixture/python'),
            self.job: self._process(self.job, self.runtime, str(self.node)),
            self.grandchild: self._process(self.grandchild, self.mcp, str(self.node)),
            os.getpid(): self._process(os.getpid(), self.tool_host, '/fixture/python'),
        }

    def _process(self, pid, parent, executable):
        # A fixture runtime starts after its fixture sources have settled.
        start = time.time() + 3
        return {'pid': pid, 'ppid': parent, 'uid': os.getuid(),
            'birth': (pid,), 'process_start_epoch': start,
            'source_start_before_ns': int(start * 1_000_000_000),
            'executable': Path(executable), 'process_group': self.runtime}

    @contextmanager
    def _source_clock(self, path, modified_ns, changed_ns=None):
        """Inject only the filesystem clock, retaining the real file identity."""
        native_stat = Path.stat

        def observed(candidate, *args, **kwargs):
            info = native_stat(candidate, *args, **kwargs)
            if candidate != path:
                return info
            fields = {name: getattr(info, name) for name in dir(info) if name.startswith('st_')}
            ctime = modified_ns if changed_ns is None else changed_ns
            fields.update(st_mtime_ns=modified_ns, st_ctime_ns=ctime,
                          st_mtime=modified_ns / 1e9, st_ctime=ctime / 1e9)
            return SimpleNamespace(**fields)

        with patch.object(Path, 'stat', observed):
            yield

    def _linux_runtime(self, ticks=10080):
        """Use the real Linux identity reader with a 100 Hz proc clock."""
        from floati import seat_host_worker as host
        from floati import codex_wait_liveness as liveness
        base = int(time.time()) + 10
        proc = str(self.runtime) + ' (node) ' + ' '.join(
            ['S', '1'] + ['0'] * 17 + [str(ticks)] + ['0'] * 3)
        files = {f'/proc/{self.runtime}/stat': proc,
                 f'/proc/{self.runtime}/status': f'Uid:\t{os.getuid()}\t{os.getuid()}\t0\t0\n',
                 '/proc/stat': f'btime {base - 100}\n'}
        with patch.object(host.sys, 'platform', 'linux'), patch.object(
                Path, 'read_text', lambda path, *a, **kw: files[str(path)]), patch.object(
                os, 'sysconf', return_value=100), patch.object(
                liveness, '_start_epoch', liveness._linux_start_epoch):
            identity = host._native_identity(self.runtime)
        self.processes[self.runtime].update(identity)
        return base

    def _teardown(self, *, job=False, grandchild=False, children=None):
        pids = [self.runtime, self.tool_host] + (
            [self.mcp, self.python_mcp] if children is None else children)
        if job:
            pids.append(self.job)
        if grandchild:
            pids.append(self.grandchild)
        rows = [{'pid': pid, 'cwd': str(self.workspace)} for pid in pids]
        with patch('floati.seat_teardown_activity._native_snapshot',
                   return_value=(rows, self.ancestry, [])), patch(
                'floati.seat_host_worker._process', side_effect=self.processes.get), patch(
                'floati.seat_host_worker._arguments', side_effect=self.arguments.get, create=True), patch(
                'floati.wake_daemon_adapters.ZCODE_NODE', self.node):
            return SeatTeardown(self.root).teardown(fixture.NODE, self.home,
                fixture.SESSION, repo='floati', doc='report.md',
                idempotency_key='zcode-host')

    def test_acting_zcode_mcp_hosts_allow_teardown_and_are_named_in_receipt(self):
        try:
            result = self._teardown()
        except ProtocolRefusal as exc:
            self.fail('acting zcode hosting processes blocked teardown: ' +
                      exc.code + ': ' + exc.detail)
        self.assertEqual('completed', result['step'])
        hosts = result['activity']['native_host_workers']
        self.assertEqual([self.runtime, self.tool_host, self.mcp, self.python_mcp],
                         [row['pid'] for row in hosts])
        self.assertEqual([str(self.node), str(self.node), str(self.node), '/fixture/python'],
                         [row['executable'] for row in hosts])
        self.assertTrue(all(row['process_start_epoch'] > 0 for row in hosts))
        self.assertEqual({'kind': 'config', 'path': str(self.config), 'server': 'node-tools'},
                         hosts[2]['declaring_source'])
        self.assertEqual({'kind': 'bundle', 'path': str(self.entry.parents[3])},
                         hosts[1]['declaring_source'])
        self.assertNotIn('PRIVATE-FIXTURE-CREDENTIAL', json.dumps(result))
        self.assertNotIn('private prompt', json.dumps(result))
        from tests.schema_validation import validate_json_schema
        validate_json_schema(result, Path('schemas/v2/seat-teardown-record.schema.json'))

    def test_undeclared_grandchild_of_declared_mcp_still_blocks(self):
        for kind in ('grandchild', 'ordinary-job', 'undeclared-bundle'):
            with self.subTest(kind=kind):
                if kind == 'undeclared-bundle':
                    self.plugin_manifest.unlink()
                    self.arguments[self.job] = self.arguments[self.tool_host]
                pid = self.grandchild if kind == 'grandchild' else self.job
                with self.assertRaises(ProtocolRefusal) as caught:
                    self._teardown(grandchild=kind == 'grandchild', job=kind != 'grandchild')
                self.assertEqual('seat_teardown_activity_in_flight', caught.exception.code)
                self.assertIn('PID ' + str(pid), caught.exception.detail)

    def test_unlocatable_loaded_config_has_named_refusal(self):
        self.arguments[self.runtime] = (self.arguments[self.runtime][0], None)
        with self.assertRaises(ProtocolRefusal) as caught:
            self._teardown()
        self.assertEqual('seat_teardown_zcode_config_unlocated', caught.exception.code)

    def _replace_config(self, config):
        self.config.write_text(json.dumps(config))
        self.processes[self.runtime].update(self._process(self.runtime, 1, str(self.node)))

    def test_harness_rejected_server_definition_cannot_exempt_a_job(self):
        server = {'type': 'stdio', 'command': str(self.node), 'args': ['/fixture/tools.js']}
        variants = (
            ('server-field', {'mcp': {'servers': {'bad': dict(server, unexpected=True)}}},
             'seat_teardown_zcode_config_unlocated'),
            ('config-section', {'hooks': 'invalid', 'mcp': {'servers': {'bad': server}}},
             'seat_teardown_zcode_config_unlocated'),
            ('disabled', {'mcp': {'servers': {'disabled': dict(server, enabled=False)}}},
             'seat_teardown_activity_in_flight'),
        )
        for name, config, code in variants:
            with self.subTest(config=name):
                self._replace_config(config)
                with self.assertRaises(ProtocolRefusal) as caught:
                    self._teardown(children=[self.mcp])
                self.assertEqual(code, caught.exception.code)

    def test_wrong_session_runtime_cannot_exempt_children(self):
        from floati.wake_daemon_contract import AdapterBindingStore, DaemonCoordinate
        from floati.wake_daemon_adapters import adapter_contract_digest
        original_runtime = dict(self.processes[self.runtime])
        original_args = list(self.arguments[self.runtime][0])
        original_parent = self.processes[self.tool_host]['ppid']
        # Rebinding changes durable evidence, so those rows run last.
        for index, kind in enumerate(('wrong-session', 'stale-ancestry', 'rebound', 'linux-rebound')):
            with self.subTest(evidence=kind):
                self.processes[self.runtime] = dict(original_runtime)
                self.arguments[self.runtime] = (list(original_args), str(self.base))
                self.processes[self.tool_host]['ppid'] = original_parent
                if kind == 'wrong-session':
                    self.arguments[self.runtime][0][3] = 'other-session'
                elif kind == 'stale-ancestry':
                    self.processes[self.tool_host]['ppid'] = 1
                else:
                    if kind == 'linux-rebound':
                        base = self._linux_runtime()
                        changed = base * 10**9 + 400_000_000
                    else:
                        changed = int(original_runtime['process_start_epoch'] * 1e9) + 1
                    self.entry.write_text('replacement entry after runtime launch\n')
                    AdapterBindingStore(self.root).write(DaemonCoordinate(self.root, fixture.NODE, 'zcode'),
                        session_id=fixture.SESSION, workspace=self.workspace, executable=self.entry,
                        adapter_version='1', adapter_digest=adapter_contract_digest('zcode'),
                        binding_epoch=index + 2)
                with self._source_clock(self.entry, changed) if kind in {'rebound', 'linux-rebound'} else nullcontext():
                    with self.assertRaises(ProtocolRefusal) as caught:
                        self._teardown()
                self.assertEqual('seat_teardown_activity_in_flight', caught.exception.code)

    def test_config_changed_after_runtime_started_does_not_supply_launch_proof(self):
        original_runtime = dict(self.processes[self.runtime])
        birth = int(original_runtime['process_start_epoch'] * 1e9)
        self.config.write_text(self.config.read_text() + ' ')
        # One native fixture covers exact, coarse filesystem and Linux proc clocks.
        # The successful row is last because it completes teardown durably.
        for kind in ('after-start', 'linux-overlap', 'linux-same-tick', 'at-start',
                     'coarse-second', 'linux-lower-boundary', 'backdated-mtime', 'linux-before'):
            with self.subTest(clock=kind):
                self.processes[self.runtime] = dict(original_runtime)
                modified, changed = birth + 1, None
                if kind.startswith('linux-') or kind == 'backdated-mtime':
                    base = self._linux_runtime(ticks=10101 if kind == 'linux-lower-boundary' else 10080)
                    modified = base * 10**9 + 400_000_000
                    if kind == 'linux-same-tick':
                        modified = base * 10**9 + 790_000_000
                    elif kind == 'linux-lower-boundary':
                        modified = (base - 1) * 10**9
                    elif kind == 'backdated-mtime':
                        modified, changed = (base - 10) * 10**9, modified
                elif kind == 'at-start':
                    modified = birth
                elif kind == 'coarse-second':
                    base = int(time.time()) + 10
                    self.processes[self.runtime].update(process_start_epoch=base + .0001,
                        source_start_before_ns=base * 10**9 + 100_000)
                    modified = base * 10**9 - 100_000
                if kind == 'linux-before':
                    self.assertEqual('completed', self._teardown()['step'])
                else:
                    with self._source_clock(self.config, modified, changed), self.assertRaises(ProtocolRefusal) as caught:
                        self._teardown()
                    self.assertEqual('seat_teardown_zcode_config_unlocated', caught.exception.code)

    def test_dirty_tree_with_hosting_processes_still_blocks(self):
        (self.workspace / 'report.md').write_text('unfinished work\n')
        with self.assertRaises(ProtocolRefusal) as caught:
            self._teardown()
        self.assertEqual('seat_teardown_activity_in_flight', caught.exception.code)
        self.assertIn('tracked_changes', caught.exception.detail)
        _git(['add', 'report.md'], self.workspace)
        _git(['commit', '-q', '-m', 'Keep unpublished scratch work'], self.workspace)
        with self.assertRaises(ProtocolRefusal) as caught:
            self._teardown()
        self.assertEqual('seat_teardown_activity_in_flight', caught.exception.code)
        self.assertIn('unpushed_commits', caught.exception.detail)


class NativeLaunchArgumentsTests(unittest.TestCase):
    def test_darwin_launch_decodes_exact_argv_and_only_home(self):
        import sys
        from floati.seat_host_worker import _darwin_arguments
        raw = ((3).to_bytes(4, sys.byteorder, signed=True) + b'/usr/bin/node\0\0'
               b'/usr/bin/node\0/declared/entry.cjs\0\0HOME=/declared/home\0'
               b'TOKEN=PRIVATE-FIXTURE-CREDENTIAL\0\0')
        self.assertEqual((('/usr/bin/node', '/declared/entry.cjs', ''), '/declared/home'),
                         _darwin_arguments(raw))

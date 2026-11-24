"""CLI and Doctor consume real lane records using explicit fixture observers."""
from __future__ import annotations

import io
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from floati import cli
from floati.command_contract import project_mcp_surface
from tests import test_lane_workspaces as lane_fixtures
from tests import test_doctor as doctor_fixtures
from tests.schema_validation import validate_json_schema


class LaneWorkspaceCliTests(unittest.TestCase):
    def setUp(self):
        # Compose the existing fixture, without inheriting its six test methods.
        self.fixture = lane_fixtures.LaneWorkspaceTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def invoke(self, *arguments):
        from floati.lane_workspaces import LaneWorkspaces

        def fixture_service(root):
            return LaneWorkspaces(root, launch_agents=self.fixture.launch_agents,
                                  lsof_executable=self.fixture.lsof)

        stdout, stderr = io.StringIO(), io.StringIO()
        # Replace only the observer coordinates, retaining the actual service.
        with patch('floati.lane_workspaces.LaneWorkspaces', side_effect=fixture_service),\
                redirect_stdout(stdout), redirect_stderr(stderr):
            result = cli.main(list(arguments))
        self.assertEqual('', stderr.getvalue())
        self.assertEqual(1, len(stdout.getvalue().splitlines()))
        return result, json.loads(stdout.getvalue())

    def open_row(self, row='cli-row'):
        return self.invoke('lane', 'open', '--root', str(self.fixture.root.path),
                           '--as', 'builder', '--row', row, '--repo', 'fixture')

    def test_checkout_liveness_reports_all_git_blockers_for_linked_worktree(self):
        _, opened = self.open_row()
        workspace = Path(opened['evidence']['workspace'])
        tracked = workspace / 'tracked.txt'
        tracked.write_text('local commit\n')
        self.fixture._git(workspace, 'commit', '-am', 'local commit')
        tracked.write_text('uncommitted edit\n')
        (workspace / 'untracked').write_text('untracked data\n')
        self.fixture._git(self.fixture.repository, 'worktree', 'lock', str(workspace))
        arguments = ('checkout-liveness', '--root', str(self.fixture.root.path),
                     '--path', str(workspace), '--json')
        try:
            _, artifact = self.invoke(*arguments)
        except SystemExit as exc:
            self.fail(f'checkout liveness produced no verdict (exit {exc.code})')
        evidence = artifact['evidence']
        self.assertIn(evidence['verdict'], ('in_use', 'unknown'))
        self.assertTrue({'tracked_changes', 'untracked_files', 'unpushed_commits', 'worktree_locked'}
                        <= {reason['code'] for reason in evidence['reasons']})
        self.assertEqual('linked', evidence['git']['registration'])

    def checkout_liveness(self, path):
        code, artifact = self.invoke('checkout-liveness', '--root', str(self.fixture.root.path),
                                     '--path', str(path), '--json')
        self.assertEqual(0, code, artifact)
        return artifact

    def test_checkout_liveness_primary_checkout_and_mcp_are_read_only(self):
        from floati.mcp import McpServer

        repo = self.fixture.repository
        root = self.fixture.root
        server = McpServer(str(root.path), 'builder', 'fixture-session')
        before = {path: path.read_bytes() for tree in (repo, root.path)
                  for path in tree.rglob('*') if path.is_file()}
        with patch('floati.checkout_liveness.host_process_cwds', return_value=()):
            artifact = self.checkout_liveness(repo)
            result = server.call_tool('checkout-liveness', {'path': str(repo)})
        self.assertEqual(artifact, result['structuredContent'])
        evidence = artifact['evidence']
        self.assertEqual('safe', evidence['verdict'])
        self.assertEqual('primary', evidence['git']['registration'])
        self.assertEqual([], evidence['reasons'])
        self.assertEqual(before, {path: path.read_bytes() for tree in (repo, root.path)
                                  for path in tree.rglob('*') if path.is_file()})

    def test_checkout_liveness_known_reasons_survive_unreadable_inputs(self):
        repo = self.fixture.repository
        (repo / 'tracked.txt').write_text('edit\n')
        variants = (
            ('process_cwds', OSError('process table unavailable')),
            ('git_registration', None),
            ('work_leases', None),
            ('territory_leases', None),
            ('git_status', None),
        )
        ledgers = {'work_leases': 'work/items.jsonl', 'territory_leases': 'territories/records.jsonl'}
        for source, failure in variants:
            with self.subTest(source=source):
                if source in ledgers:
                    ledger = self.fixture.root.path / ledgers[source]
                    ledger.parent.mkdir(exist_ok=True)
                    ledger.write_bytes(b'malformed ledger data\n')
                    self.addCleanup(ledger.unlink, missing_ok=True)
                if source == 'git_status':
                    self.fixture._git(repo, 'update-index', '--assume-unchanged', 'tracked.txt')
                target = repo / '.git' if source == 'git_registration' else repo
                with patch('floati.checkout_liveness.host_process_cwds', return_value=(),
                           side_effect=failure):
                    evidence = self.checkout_liveness(target)['evidence']
                self.assertEqual('unknown', evidence['verdict'])
                self.assertIn(source, {row.get('source') for row in evidence['reasons']})
                if source in ('process_cwds', 'work_leases', 'territory_leases'):
                    self.assertIn('tracked_changes', {row['code'] for row in evidence['reasons']})

    def test_checkout_liveness_counts_live_territory_leases_on_the_checkout(self):
        from floati.ids import uuid7_hex
        from floati.jsonl import append_record
        from floati.territory import TerritoryLedger
        from floati.territory_records import TERRITORY_KINDS, TERRITORY_RELATIVE

        root = self.fixture.root
        _, opened = self.open_row()
        workspace = Path(opened['evidence']['workspace'])
        territories = TerritoryLedger(root)
        variants = (('inside', workspace / 'src'), ('covering', workspace.parent))
        with patch('floati.checkout_liveness.host_process_cwds', return_value=()):
            self.assertEqual('safe', self.checkout_liveness(workspace)['evidence']['verdict'])
            for label, leased in variants:
                with self.subTest(lease=label):
                    lease = territories.declare('builder', [str(leased)], 600, 'territory-' + label)
                    live = self.checkout_liveness(workspace)['evidence']
                    territories.release('builder', lease['id'], 'release-' + label)
                    released = self.checkout_liveness(workspace)['evidence']
                    self.assertEqual('in_use', live['verdict'], live)
                    self.assertEqual([{'code': 'live_territory_lease', 'workspace': str(leased.resolve()),
                                       'node': 'builder', 'territory_id': lease['id']}],
                                     live['reasons'])
                    self.assertEqual('safe', released['verdict'], released)
            orphan = dict(lease, id=lease['id'][:-32] + uuid7_hex(), node_id='missing-holder',
                          idempotency_key='missing-holder')
            orphan.pop('advisory', None)
            append_record(root, TERRITORY_RELATIVE, orphan, allowed_kinds=TERRITORY_KINDS)
            unidentified = self.checkout_liveness(workspace)['evidence']
        self.assertEqual('unknown', unidentified['verdict'], unidentified)
        self.assertTrue(any(row['code'] == 'unidentified_holder' and row['holder'] == 'missing-holder'
                            and row['source'] == 'territory_leases'
                            for row in unidentified['reasons']), unidentified)

    def test_checkout_liveness_tracks_live_sessions_and_work_and_node_leases(self):
        from floati.codex_wait_contract import CodexWaitConsentLedger, CodexWaitSessionLedger, resolve_participant
        from floati.ids import uuid7_hex
        from floati.jsonl import append_record
        from floati.planes import AuthorityGrantStore
        from floati.registry import REGISTRY_KINDS, Registry
        from floati.wake_control import WakeController
        from floati.work import WorkLog

        root = self.fixture.root
        _, opened = self.open_row()
        workspace = Path(opened['evidence']['workspace'])
        now = datetime.now(timezone.utc).replace(microsecond=0)
        appendix = root.path / 'codex-wait/workspaces.v0.json'
        appendix.parent.mkdir()
        appendix.write_text(json.dumps({
            'schema_version': 0, 'tenant_id': root.tenant_id,
            'mappings': [{'workspace': str(workspace), 'node_id': 'builder'}],
        }))
        binding = resolve_participant(root.path, workspace).binding
        consent = CodexWaitConsentLedger(root).arm(binding, hook_timeout_seconds=10,
                                                   wait_deadline_seconds=2, idempotency_key='consent')
        session = CodexWaitSessionLedger(root).arm(binding, consent, 'fixture-session',
                                                  idempotency_key='session')
        AuthorityGrantStore(root).claim('work-claims', 'builder', 60, 60, now)
        work = WorkLog(root)
        with patch('floati.work.worker_workspace_root', return_value=workspace / 'floati-work'):
            item = work.add('Fixture work', 'builder', [], provision_workspace=True, now=now)
        work.claim(item['id'], 'builder', 'work-claims', 1, now=now)
        lease = {
            'schema_version': 0, 'id': 'lease-' + uuid7_hex(), 'kind': 'node_lease',
            'tenant_id': root.tenant_id, 'node_id': 'builder', 'workspace': str(workspace),
            'timestamp': now.isoformat().replace('+00:00', 'Z'),
            'expires_at': (now + timedelta(days=1)).isoformat().replace('+00:00', 'Z'),
            'state': 'active',
        }
        registry = Registry(root)
        append_record(root, registry.relative_path, lease, allowed_kinds=REGISTRY_KINDS)
        with patch('floati.checkout_liveness.host_process_cwds', return_value=()):
            live = self.checkout_liveness(workspace)['evidence']
            WakeController(root).pause('builder', 'fixture-session', idempotency_key='paused')
            work.complete(item['id'], 'builder', [], now=now)
            expired = dict(lease, id='lease-' + uuid7_hex(),
                           timestamp=(now - timedelta(days=2)).isoformat().replace('+00:00', 'Z'),
                           expires_at=(now - timedelta(days=1)).isoformat().replace('+00:00', 'Z'))
            append_record(root, registry.relative_path, expired, allowed_kinds=REGISTRY_KINDS)
            released = self.checkout_liveness(workspace)['evidence']
            tracked = workspace / 'tracked.txt'
            tracked.write_text('local commit\n')
            self.fixture._git(workspace, 'commit', '-am', 'local commit')
            tracked.write_text('local edit\n')
            (workspace / 'untracked').write_text('local data\n')
            residue = self.checkout_liveness(workspace)['evidence']
            orphan = dict(session, id='codex-wait-session-' + uuid7_hex(),
                          node_id='missing-holder', idempotency_key='missing-holder')
            append_record(root, 'receipts/codex-wait-session/missing-holder.jsonl', orphan,
                          allowed_kinds={'codex_wait_session_receipt'})
            unidentified = self.checkout_liveness(workspace)['evidence']
        self.assertEqual('in_use', live['verdict'], live)
        self.assertEqual({'live_session', 'live_work_lease', 'live_node_lease'},
                         {row['code'] for row in live['reasons']})
        self.assertEqual('safe', released['verdict'], released)
        self.assertEqual('in_use', residue['verdict'], residue)
        head = self.fixture._git(workspace, 'rev-parse', 'HEAD')
        branch = self.fixture._git(workspace, 'branch', '--show-current')
        self.assertEqual(f'left behind: {branch} @ {head}, 1 unpushed, 2 dirty',
                         residue['left_behind']['summary'])
        self.assertEqual('unknown', unidentified['verdict'], unidentified)
        self.assertTrue(any(row['code'] == 'unidentified_holder' and row['holder'] == 'missing-holder'
                            for row in unidentified['reasons']), unidentified)

    def test_checkout_liveness_resolves_tmp_alias_for_a_live_process(self):
        from tests.readiness import wait_process

        _, opened = self.open_row()
        workspace = Path(opened['evidence']['workspace'])
        child_dir = workspace / 'child'
        child_dir.mkdir()
        with tempfile.TemporaryDirectory(dir='\x2ftmp') as directory:
            alias = Path(directory) / 'alias'
            alias.symlink_to(workspace, target_is_directory=True)
            child = subprocess.Popen([sys.executable, '-B', '-c', 'import sys; sys.stdin.read()'],
                                     cwd=alias / 'child', stdin=subprocess.PIPE)
            try:
                evidence = self.checkout_liveness(alias)['evidence']
            finally:
                child.stdin.close()
                wait_process(child, 'checkout liveness process fixture')
        self.assertEqual(str(workspace), evidence['path'])
        self.assertIn({'code': 'process_cwd', 'cwd': str(child_dir)}, evidence['reasons'])

    def test_open_emits_one_artifact_naming_real_worktree_and_durable_record(self):
        status, artifact = self.open_row()
        self.assertEqual(0, status)
        self.assertEqual('ok', artifact['status'])
        self.assertEqual('lane', artifact['command'])
        workspace = Path(artifact['evidence']['workspace'])
        self.assertTrue(workspace.is_dir())
        self.assertEqual(self.fixture.base_sha, self.fixture._git(workspace, 'rev-parse', 'HEAD'))
        self.assertIn(artifact['evidence']['record'], self.fixture._records())

    def test_close_forwards_dirty_refusal_and_explicit_force_reason(self):
        _, opened = self.open_row()
        workspace = Path(opened['evidence']['workspace'])
        (workspace / 'tracked.txt').write_text('dirty fixture\n')
        arguments = ('lane', 'close', '--root', str(self.fixture.root.path),
                     '--as', 'builder', '--row', 'cli-row')
        status, artifact = self.invoke(*arguments)
        self.assertEqual(20, status)
        self.assertEqual('lane_workspace_dirty', artifact['evidence']['code'])
        self.assertTrue(artifact['evidence']['remedy'])
        self.assertTrue(workspace.is_dir())
        status, artifact = self.invoke(*arguments, '--force', '--why', 'Discard this fixture edit')
        self.assertEqual(0, status)
        self.assertEqual('Discard this fixture edit', artifact['evidence']['record']['why'])
        self.assertFalse(workspace.exists())

    def test_sweep_apply_reports_degraded_and_preserves_unmanaged_bytes(self):
        _, opened = self.open_row()
        workspace = Path(opened['evidence']['workspace'])
        foreign = self.fixture.lanes_root / 'unmanaged'
        foreign.mkdir()
        payload = foreign / 'keep.bin'
        before = b'operator-owned\x00\xff'
        payload.write_bytes(before)
        self.fixture._state('lane-board.json', {'schema_version': 0, 'rows': {'cli-row': 'landed'}})
        status, artifact = self.invoke('sweep', '--root', str(self.fixture.root.path), '--apply')
        self.assertEqual(35, status)
        self.assertEqual('degraded', artifact['status'])
        self.assertEqual(1, len(artifact['evidence']['closed']))
        self.assertTrue(artifact['evidence']['unmanaged'])
        self.assertFalse(workspace.exists())
        self.assertEqual(before, payload.read_bytes())

    def test_lane_and_sweep_verbs_are_not_exposed_as_mcp_tools(self):
        surface = project_mcp_surface(cli._parser())
        exposed = {tuple(row['_meta']['floati']['commandPath']) for row in surface['tools']}
        denied = {tuple(path) for path in surface['denied_paths']}
        for path in (('lane', 'open'), ('lane', 'close'), ('sweep',)):
            self.assertNotIn(path, exposed)
            self.assertIn(path, denied)

    def doctor(self):
        fixture = doctor_fixtures.DoctorContractTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        hooks = fixture.base / 'hooks.json'
        hooks.write_text(json.dumps({'hooks': {'Stop': []}}))
        config = fixture.base / 'config.toml'
        config.write_text('')
        return fixture.doctor(root=self.fixture.root.path, codex_hooks=hooks,
                              codex_config=config, codex_gateway_host=fixture.base / 'gateway',
                              no_sandbox=True)

    def test_doctor_reports_real_lane_inventory_in_valid_schema(self):
        self.fixture._service().open(actor='builder', row='doctor-row', repo='fixture')
        foreign = self.fixture.lanes_root / 'unmanaged'
        foreign.mkdir()
        (foreign / 'keep.bin').write_bytes(b'ninebytes')
        artifact, _ = self.doctor().artifact()
        validate_json_schema(artifact, Path(__file__).resolve().parents[1] / 'schemas/v1/doctor-artifact.schema.json')
        finding = next(row for row in artifact['findings'] if row['code'] == 'lane_workspaces')
        self.assertEqual('warning', finding['severity'])
        self.assertEqual(9, finding['lane_workspaces']['unmanaged_bytes'])
        node = next(row for row in finding['lane_workspaces']['nodes'] if row['node_id'] == 'builder')
        self.assertEqual(1, node['open_lanes'])
        self.assertGreaterEqual(node['oldest_open_age_seconds'], 0)
        node_lines = [row for row in artifact['findings'] if row['code'] == 'lane_workspace_node']
        self.assertEqual(['builder'], [row['subject'] for row in node_lines])

    def test_doctor_without_lane_declaration_preserves_old_fleet_behavior(self):
        (self.fixture.root.path / 'state/lanes-root.json').unlink()
        artifact, _ = self.doctor().artifact()
        self.assertFalse(any(row['code'].startswith('lane_workspace') for row in artifact['findings']))

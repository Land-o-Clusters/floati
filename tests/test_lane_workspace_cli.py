"""CLI and Doctor consume real lane records using explicit fixture observers."""
from __future__ import annotations

import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
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

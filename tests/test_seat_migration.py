"""BL-10: a registered seat turns over in one step.

A seat registered before the governed path existed — an active registry entry,
a workspace-map binding, an armed waiter consent, no SEAT.json, no fleet
governance — is migrated once, by a verb, with a receipt, and then boards in
one step forever after. Every refusal the row leaves in place names the one
command that resolves it, and the two subjects behind the old
seat_board_workspace_undeclared refusal are split so a reader can act.
"""
import json
import shlex
import unittest

from floati import fixture_ids as ids
from floati.errors import ProtocolRefusal
from floati.events import EventLog
from floati.jsonl import read_records_snapshot
from floati.seat_board import SeatBoard
from tests import test_codex_wait as fixture


class PreGovernanceSeatTurnoverTests(fixture.CodexWaitRuntimeTests):
    """CodexWaitRuntimeTests.setUp is exactly the pre-governance seat: the
    registration predates the wizard's governance plan, so nothing published
    nodes/<node>/ or SEAT.json, and no registry/governance.json exists."""

    def setUp(self) -> None:
        super().setUp()
        self.assertFalse((self.workspace / 'SEAT.json').exists())
        self.assertFalse((self.root.path / 'registry' / 'governance.json').exists())

    def migrate(self, *extra):
        import contextlib
        import io
        from floati.cli import main
        argv = ['seat', 'migrate', '--root', str(self.root.path), '--as', ids.builder('floati'),
            '--workspace', str(self.workspace), '--idempotency-key', 'bl10-migrate', *extra]
        output = io.StringIO()
        try:
            with contextlib.redirect_stdout(output):
                code = main(argv)
        except SystemExit as exc:
            return (exc.code if isinstance(exc.code, int) else 0), output.getvalue()
        return code, output.getvalue()

    def _assert_ruled_governance(self, declaration):
        self.assertEqual(ids.builder('floati'), declaration['node_id'])
        self.assertEqual(str(self.root.path), declaration['root'])
        self.assertEqual('star', declaration['topology'])
        self.assertEqual(
            ['dispatch_bounded_work', 'gate_results_before_merge', 'decide_non_owner_tier_questions'],
            list(declaration['coordinator_authority']))
        self.assertEqual(['publishing', 'credentials', 'key_custody'], list(declaration['owner_tier']))

    def _seat_board_rows(self, node):
        return read_records_snapshot(
            self.root, 'receipts/seat-board/' + node + '.jsonl',
            allowed_kinds={'seat_board_receipt'})

    def test_registered_pre_governance_seat_migrates_once_and_boards_completed(self):
        code, output = self.migrate('--coordinator', 'architect')
        self.assertEqual(0, code, output)
        artifact = json.loads(output)
        self.assertEqual('ok', artifact['status'])

        declaration = json.loads((self.workspace / 'SEAT.json').read_text())
        self.assertEqual('architect', declaration['coordinator'])
        self._assert_ruled_governance(declaration)
        governance = json.loads(
            (self.root.path / 'registry' / 'governance.json').read_text())
        self.assertEqual('architect', governance['coordinator'])

        # Idempotent, and the coordinator derives from the registry's architect
        # role where one is recorded: a second run WITHOUT the argument writes
        # nothing new and agrees with what stands.
        code_again, output_again = self.migrate()
        self.assertEqual(0, code_again, output_again)
        self.assertEqual(
            declaration,
            json.loads((self.workspace / 'SEAT.json').read_text()))

        result = self.board('turnover-one', 'bl10-board-1')
        self.assertEqual('completed', result['step'])
        rows = self._seat_board_rows(ids.builder('floati'))
        self.assertEqual('completed', rows[-1]['step'])
        self.assertEqual(ids.builder('floati'), rows[-1]['node_id'])
        migration_rows = read_records_snapshot(
            self.root, 'receipts/seat-migration/' + ids.builder('floati') + '.jsonl',
            allowed_kinds={'seat_migration_receipt'})
        self.assertTrue(migration_rows)

    def board(self, session, key):
        from floati.seat_board import SeatBoard
        return SeatBoard(self.root).board(
            ids.builder('floati'), self.workspace, session, idempotency_key=key)

    def test_second_session_boards_one_step_and_replay_consumes_nothing_new(self):
        code, output = self.migrate('--coordinator', 'architect')
        self.assertEqual(0, code, output)

        first_message = self.send('first')
        first = self.board('session-one', 'bl10-board-1')
        self.assertEqual([first_message['id']], first['item_ids'])
        self.assertEqual(ids.builder('floati'), first['node_id'])

        second_message = self.send('second')
        # A turnover boarding after a live predecessor is the --take-over shape
        # (the boarding order's own rule); it is still one step, no migration.
        second = SeatBoard(self.root).board(ids.builder('floati'), self.workspace, 'session-two',
            idempotency_key='bl10-board-2', take_over=True)
        self.assertEqual([second_message['id']], second['item_ids'])
        self.assertEqual(ids.builder('floati'), second['node_id'])
        replay = self.board('session-one', 'bl10-board-1')
        self.assertEqual(first, replay)
        self.assertEqual(
            0, len(EventLog(self.root).present(ids.builder('floati'))[0]),
            'a completed-key replay must not consume the mail the second boarding drained')

    def test_board_refusal_on_unmigrated_seat_names_the_migration_command(self):
        with self.assertRaises(ProtocolRefusal) as caught:
            self.board('session-three', 'bl10-board-3')
        refusal = caught.exception
        self.assertIsNotNone(
            refusal.remedy, 'the refusal must name the one command that resolves it')
        self.assertIn('seat migrate', refusal.remedy)
        argv = shlex.split(refusal.remedy)
        self.assertEqual('floati', argv[0])
        # The remedy is runnable for THIS root/node/workspace: it parses against
        # the CLI's own parser and completes idempotently.
        import contextlib
        import io
        from floati.cli import main
        output = io.StringIO()
        try:
            with contextlib.redirect_stdout(output):
                code = main(argv[1:])
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else 0
        self.assertEqual(0, code, output.getvalue())
        self.assertEqual('completed', self.board('session-three', 'bl10-board-3')['step'])

    def test_unmapped_workspace_refuses_unbound_not_undeclared(self):
        stranger = self.base / 'stranger-workspace'
        stranger.mkdir()
        from floati.seat_board import SeatBoard
        with self.assertRaises(ProtocolRefusal) as caught:
            SeatBoard(self.root).board(
                ids.builder('floati'), stranger, 'session-four', idempotency_key='bl10-board-4')
        self.assertEqual('seat_board_workspace_unbound', caught.exception.code)

    def test_unregistered_workspace_still_refuses(self):
        stranger = self.base / 'stranger-workspace'
        stranger.mkdir()
        from floati.seat_board import SeatBoard
        with self.assertRaises(ProtocolRefusal):
            SeatBoard(self.root).board(
                ids.builder('floati'), stranger, 'session-four', idempotency_key='bl10-board-4')

    def test_doctor_names_the_migration_command_for_an_unmigrated_seat(self):
        from floati.doctor import project_seat_declaration_findings
        from floati.seat_migration import remedy_for

        findings = project_seat_declaration_findings(self.root)
        mine = [f for f in findings if f['code'] == 'seat_declaration_absent'
                and f['subject'] == str(self.workspace)]
        self.assertEqual(1, len(mine), findings)
        expected = remedy_for(self.root, ids.builder('floati'), self.workspace)
        self.assertEqual(expected, mine[0]['remediation'])
        # The printed remedy is the same runnable command the refusal carries.
        argv = shlex.split(mine[0]['remediation'])
        self.assertEqual(['floati', 'seat', 'migrate'], argv[:3])

    def test_migrated_declaration_mismatch_still_refuses(self):
        code, output = self.migrate('--coordinator', 'architect')
        self.assertEqual(0, code, output)
        marker = self.workspace / 'SEAT.json'
        row = json.loads(marker.read_text())
        row['node_id'] = 'different-builder'
        marker.write_text(json.dumps(row))
        code, output = self.migrate()
        self.assertEqual(20, code, output)
        artifact = json.loads(output)
        self.assertEqual('refused', artifact['status'])
        self.assertEqual('workspace_identity_mismatch', artifact['evidence']['code'])


if __name__ == '__main__':
    unittest.main()

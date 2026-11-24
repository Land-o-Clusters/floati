"""Scratch recovery of executable drift without bypassing the digest fence."""
import hashlib
import io
import json
import shlex
import unittest
from contextlib import redirect_stdout
from unittest import mock

from floati import fixture_ids as ids
from floati.cli import main
from floati.errors import ProtocolRefusal
from floati.jsonl import read_records_snapshot
from floati.records import WAKE_HOLD_KINDS
from floati.registry import Registry
from floati.seat_board import SeatBoard
from floati.wake_daemon_contract import AdapterBindingStore, DaemonCoordinate
from tests import test_seat_board as fixture


class DigestRefusalRemedyTests(unittest.TestCase):
    setUp = fixture.SeatBoardTests.setUp
    write_map = fixture.SeatBoardTests.write_map
    send = fixture.SeatBoardTests.send
    arm_session = fixture.SeatBoardTests.arm_session

    def cli(self, arguments):
        output = io.StringIO()
        with redirect_stdout(output), mock.patch("sys.stdin", io.StringIO("")):
            status = main(arguments)
        return status, json.loads(output.getvalue())

    def bind(self, harness, node=None):
        from floati.wake_daemon_adapters import adapter_contract_digest
        node = node or ids.builder('floati')
        executable = self.base / "agent's executable"
        executable.write_bytes(b"#!/bin/sh\nexit 0\n")
        executable.chmod(0o700)
        coordinate = DaemonCoordinate(self.root, node, harness)
        store = AdapterBindingStore(self.root)
        record = store.write(coordinate, session_id="new-session",
            workspace=self.workspace, executable=executable, adapter_version="1",
            adapter_digest=adapter_contract_digest(harness), binding_epoch=7)
        return executable, coordinate, store, record

    def recover_board(self, harness):
        """Dropping the remedy or accepting drift would fail before mail is drained."""
        executable, coordinate, store, record = self.bind(harness)
        message = self.send("digest-mail-" + harness)
        old_digest = record['executable_digest']
        changed_bytes = b"#!/bin/sh\n# updated executable\nexit 0\n"
        executable.write_bytes(changed_bytes)
        board_arguments = ['seat', 'board', '--root', str(self.root.path),
            '--as', ids.builder('floati'), '--workspace', str(self.workspace),
            '--session', 'new-session', '--take-over', '--idempotency-key', 'digest-board']
        before = store.path(coordinate).read_bytes()
        status, artifact = self.cli(board_arguments)
        self.assertEqual(20, status, artifact)
        evidence = artifact['evidence']
        self.assertEqual('wake_daemon_executable_digest_mismatch', evidence['code'])
        self.assertIn('boarding resume step', evidence['detail'])
        self.assertEqual(before, store.path(coordinate).read_bytes())
        self.assertFalse(self.root.resolve_relative('receipts/deliveries/' + ids.builder('floati') + '.jsonl').exists())
        self.assertFalse(self.root.resolve_relative('receipts/acks/' + ids.builder('floati') + '.jsonl').exists())
        self.assertIsInstance(evidence['remedy'], str, evidence)
        self.assertIn(old_digest, evidence['detail'])
        self.assertIn(hashlib.sha256(changed_bytes).hexdigest(), evidence['detail'])
        self.assertIn(str(executable), evidence['detail'])
        remedy = shlex.split(evidence['remedy'])
        self.assertEqual(['floati', 'wake', 'daemon', 'bind'], remedy[:4])
        self.assertNotIn('--yes', remedy)
        with mock.patch('floati.wake_daemon_adapters.ZCODE_ENTRY_SCRIPT', executable):
            status, rebound = self.cli(remedy[1:])
        self.assertEqual(0, status, rebound)
        self.assertEqual('resume_unproven', rebound['evidence']['resume_state'])
        status, completed = self.cli(board_arguments)
        self.assertEqual(0, status, completed)
        self.assertEqual([message['id']], completed['evidence']['item_ids'])
        status, replay = self.cli(board_arguments)
        self.assertEqual(0, status, replay)
        self.assertEqual(completed, replay)
        for ledger, kinds in (('deliveries', WAKE_HOLD_KINDS), ('acks', {'ack_receipt'})):
            rows = read_records_snapshot(self.root, 'receipts/' + ledger + '/' + ids.builder('floati') + '.jsonl', allowed_kinds=set(kinds))
            self.assertEqual(1, len(rows))
            self.assertEqual([message['id']], rows[0]['item_ids'])

    def test_cursor_rebind_remedy_completes_same_board_without_early_delivery(self):
        cases = ('cursor', 'zcode', 'grok-build')
        for index, harness in enumerate(cases):
            with self.subTest(harness=harness):
                if index:
                    self.doCleanups()
                    self.setUp()
                self.recover_board(harness)

    def test_codex_byte_drift_rebind_remedy_boards_the_current_claim(self):
        """The printed exact-claim rebind runs before the same board can drain."""
        from tests.test_codex_wait import CodexWaitRuntimeTests
        from tests.test_workspace_seat_declaration import WorkspaceSeatDeclarationTests
        node = ids.worker('codex')
        CodexWaitRuntimeTests.arm_codex_role_node(self, node)
        WorkspaceSeatDeclarationTests.write_seat_fixture(self, self.workspace, self.root.path, node)
        executable, coordinate, store, record = self.bind('codex', node)
        changed_bytes = b"#!/bin/sh\n# Codex updated\nexit 0\n"
        executable.write_bytes(changed_bytes)
        with mock.patch('floati.wake_daemon_adapters.CODEX_EXECUTABLE', executable):
            with self.assertRaises(ProtocolRefusal) as caught:
                SeatBoard(self.root).board(node, self.workspace, 'new-session', idempotency_key='codex-board', take_over=True)
            refusal = caught.exception
            remedy = shlex.split(refusal.remedy)
            self.assertEqual(['floati', 'wake', 'daemon', 'bind'], remedy[:4])
            self.assertIn(record['executable_digest'], refusal.detail)
            self.assertIn(hashlib.sha256(changed_bytes).hexdigest(), refusal.detail)
            foreign = list(remedy[1:])
            foreign[foreign.index('--executable') + 1] = str(self.base / 'foreign-codex')
            status, refused = self.cli(foreign)
            self.assertEqual(20, status)
            self.assertEqual('wake_daemon_codex_binding_source_invalid', refused['evidence']['code'])
            status, rebound = self.cli(remedy[1:])
            self.assertEqual(0, status, rebound)
            self.assertEqual(hashlib.sha256(changed_bytes).hexdigest(), store.read(coordinate)['executable_digest'])
            completed = SeatBoard(self.root).board(node, self.workspace, 'new-session', idempotency_key='codex-board', take_over=True)
            self.assertEqual('completed', completed['step'])

    def test_codex_path_retarget_names_both_paths_even_when_digests_are_equal(self):
        """A path change must stay refused even when the two file hashes coincide."""
        from floati.wake_daemon_adapters import CodexQueueWakeAdapter
        executable, coordinate, store, record = self.bind('codex')
        replacement = self.base / 'replacement-codex'
        replacement.write_bytes(executable.read_bytes())
        replacement.chmod(0o700)
        with mock.patch('floati.wake_daemon_adapters.CODEX_EXECUTABLE', replacement):
            with self.assertRaises(ProtocolRefusal) as caught:
                CodexQueueWakeAdapter(coordinate).request_wake(record, 'scratch reason', 1)
        refusal = caught.exception
        self.assertEqual('wake_daemon_executable_digest_mismatch', refusal.code)
        self.assertIsInstance(refusal.remedy, str)
        self.assertIn(str(executable), refusal.detail)
        self.assertIn(str(replacement), refusal.detail)
        self.assertEqual(2, refusal.detail.count(record['executable_digest']))
        self.assertEqual(['floati', 'wake', 'daemon', 'bind'], shlex.split(refusal.remedy)[:4])

    def test_malformed_recorded_digest_is_not_quoted_as_a_hash_or_private_text(self):
        """A corrupt digest field must not leak its contents into new testimony."""
        executable, coordinate, store, record = self.bind('cursor')
        record['executable_digest'] = 'PRIVATE-BODY-not-a-digest'
        record['binding_epoch'] = '7; injected-command'
        store.path(coordinate).write_text(json.dumps(record) + '\n')
        with self.assertRaises(ProtocolRefusal) as caught:
            store.read(coordinate)
        refusal = caught.exception
        self.assertEqual('wake_daemon_executable_digest_mismatch', refusal.code)
        self.assertNotIn('PRIVATE-BODY', refusal.detail)
        self.assertNotIn('injected-command', refusal.remedy)
        self.assertIsInstance(refusal.remedy, str)
        self.assertIn('--binding-epoch', refusal.remedy)

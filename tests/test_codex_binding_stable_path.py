"""Package-manager paths may move; executable trust must not move with them."""
from __future__ import annotations

import contextlib
import io
import hashlib
import json
import shlex
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from floati import fixture_ids as ids
from floati.cli import main
from floati.codex_wait_contract import CodexWaitConsentLedger, resolve_participant
from floati.registry import Registry
from floati.root import FloatiRoot
from floati.wake_daemon_contract import AdapterBindingStore, DaemonCoordinate
from tests import test_codex_wait as fixture
from tests import test_seat_board as boarding


class CodexBindingStablePathTests(unittest.TestCase):
    write_map = fixture.CodexWaitContractTests.write_map
    arm_session = fixture.CodexWaitRuntimeTests.arm_session
    board = boarding.SeatBoardTests.board

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path.home())
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.bus_home = self.base / "demo-fleet"
        self.root = FloatiRoot.open_direct_home(self.bus_home, create=True)
        self.node = ids.builder("floati")
        Registry(self.root).register(self.node, "Codex")
        self.workspace = self.base / "workspace"
        self.workspace.mkdir()
        from tests.test_workspace_seat_declaration import WorkspaceSeatDeclarationTests
        WorkspaceSeatDeclarationTests.write_seat_fixture(
            self, self.workspace, self.root.path, self.node)
        self.write_map([{"workspace": str(self.workspace), "node_id": self.node}])
        self.participant = resolve_participant(self.bus_home, self.workspace)
        CodexWaitConsentLedger(self.root).arm(self.participant.binding,
            hook_timeout_seconds=10, wait_deadline_seconds=2, idempotency_key="consent")
        self.arm_session("new-session")
        self.link = self.base / "bin" / "codex"
        self.link.parent.mkdir()
        self.old = self.binary("1", b"#!/bin/sh\nexit 0\n")
        self.link.symlink_to(self.old)
        self.patch = mock.patch("floati.wake_daemon_adapters.CODEX_EXECUTABLE", self.link)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.coordinate = DaemonCoordinate(self.root, self.node, "codex")
        self.store = AdapterBindingStore(self.root)

    def binary(self, version, content):
        path = self.base / "Caskroom" / "codex" / version / "bin" / "codex"
        path.parent.mkdir(parents=True)
        path.write_bytes(content)
        path.chmod(0o700)
        return path

    def bind(self, *, legacy=False):
        from floati.wake_daemon_adapters import adapter_contract_digest, record_codex_daemon_binding
        if not legacy:
            return record_codex_daemon_binding(self.participant, "new-session", binding_epoch=1)
        return self.store.write(self.coordinate, session_id="new-session",
            workspace=self.workspace, executable=self.old, adapter_version="1",
            adapter_digest=adapter_contract_digest("codex"), binding_epoch=1)

    def upgrade(self, content):
        new = self.binary("2", content)
        self.link.unlink()
        self.link.symlink_to(new)
        shutil.rmtree(self.old.parents[1])
        return new

    def cli(self, argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), mock.patch("sys.stdin", io.StringIO("")):
            status = main(argv)
        return status, json.loads(out.getvalue())

    def test_vanished_version_boards_and_receipts_the_repair(self):
        old = self.bind(legacy=True)
        new = self.upgrade(b"#!/bin/sh\nprintf 'codex-cli 0.164.0\\n'\n")
        argv = ["seat", "board", "--root", str(self.root.path), "--as", self.node,
            "--session", "new-session", "--workspace", str(self.workspace),
            "--idempotency-key", "upgrade-board"]
        status, artifact = self.cli(argv)
        self.assertEqual(0, status, artifact)
        from floati.jsonl import read_records_snapshot
        from floati.wake_daemon_contract import DAEMON_KINDS
        from floati.wake_daemon_roll import lifecycle_relative
        repairs = [row for row in read_records_snapshot(self.root,
            lifecycle_relative(self.node), allowed_kinds=DAEMON_KINDS)
            if row['kind'] == 'codex_binding_repair_receipt']
        self.assertEqual(1, len(repairs))
        repair = repairs[0]
        self.assertEqual(str(self.link), repair['declared_executable'])
        self.assertEqual(old['executable'], repair['old_executable'])
        self.assertEqual(old['executable_digest'], repair['old_executable_digest'])
        self.assertEqual(str(new), repair['new_executable'])
        self.assertEqual(hashlib.sha256(new.read_bytes()).hexdigest(), repair['new_executable_digest'])
        self.assertEqual('codex-cli 0.164.0', repair['provider_banner'])
        self.assertEqual('recorded_path_vanished', repair['reason'])
        self.assertEqual(artifact['evidence']['arm_receipt_id'], repair['arm_receipt_id'])
        self.assertEqual('new-session', repair['acting_session_id'])
        status, replay = self.cli(argv)
        self.assertEqual(0, status, replay)
        self.assertEqual(str(new), self.store.read(self.coordinate)['executable'])

    def test_existing_recorded_path_digest_drift_refuses_until_printed_remedy_runs(self):
        self.bind(legacy=True)
        self.old.write_bytes(b"#!/bin/sh\nprintf 'codex-cli 0.164.0\\n'\n")
        argv = ["seat", "board", "--root", str(self.root.path), "--as", self.node,
            "--session", "new-session", "--workspace", str(self.workspace),
            "--idempotency-key", "drift-board"]
        before = self.store.path(self.coordinate).read_bytes()
        status, artifact = self.cli(argv)
        self.assertEqual(20, status, artifact)
        self.assertEqual('wake_daemon_executable_digest_mismatch', artifact['evidence']['code'])
        self.assertEqual(before, self.store.path(self.coordinate).read_bytes())
        remedy = shlex.split(artifact['evidence']['remedy'])
        self.assertEqual(['floati', 'wake', 'daemon', 'bind'], remedy[:4])
        status, rebound = self.cli(remedy[1:])
        self.assertEqual(0, status, rebound)
        status, boarded = self.cli(argv)
        self.assertEqual(0, status, boarded)

    def test_vanished_version_never_trusts_an_executable_impostor(self):
        self.bind(legacy=True)
        self.upgrade(b"#!/bin/sh\nprintf 'some-other-tool 0.164.0\\n'\n")
        before = self.store.path(self.coordinate).read_bytes()
        status, artifact = self.cli(["seat", "board", "--root", str(self.root.path),
            "--as", self.node, "--session", "new-session", "--workspace", str(self.workspace),
            "--idempotency-key", "impostor-board"])
        self.assertEqual(20, status, artifact)
        self.assertEqual('wake_daemon_codex_identity_invalid', artifact['evidence']['code'])
        self.assertEqual(before, self.store.path(self.coordinate).read_bytes())

    def test_new_binding_survives_a_version_upgrade_without_reboarding(self):
        from floati.wake_daemon_adapters import CodexQueueWakeAdapter
        from tests.test_wake_daemon_adapters import _Runner
        self.bind()
        new = self.upgrade(b"#!/bin/sh\nprintf 'codex-cli 0.164.0\\n'\n")
        runner = _Runner(stdout='')
        adapter = CodexQueueWakeAdapter(self.coordinate, runner=runner)
        current = adapter.exact_binding()
        result = adapter.request_wake(current, 'mail arrived', 30)
        self.assertEqual('queued', result.outcome)
        self.assertEqual(str(new), runner.calls[0][0][0])

    def test_doctor_names_stable_declaration_and_does_not_measure_a_retained_old_target(self):
        from floati.harness_versions import harness_version_artifact
        from floati.doctor import project_harness_binary_inventory
        self.old.write_bytes(b"#!/bin/sh\nprintf 'codex-cli 0.163.0\\n'\n")
        self.bind(legacy=True)
        replacement = self.binary('2', b"#!/bin/sh\nprintf 'codex-cli 0.164.0\\n'\n")
        self.link.unlink()
        self.link.symlink_to(replacement)
        before = self.store.path(self.coordinate).read_bytes()
        row = harness_version_artifact(self.root, environ={'PATH': ''})['declared'][0]
        self.assertEqual(str(self.link), row['executable'])
        self.assertIsNone(row['version'])
        self.assertEqual('wake_daemon_codex_declaration_changed', row['version_reason'])
        with mock.patch.dict('os.environ', {'PATH': ''}):
            findings = project_harness_binary_inventory(self.root, currency_current=True)
        self.assertTrue(all(finding['severity'] == 'ok' for finding in findings), findings)
        self.assertEqual(before, self.store.path(self.coordinate).read_bytes())

    def test_repair_cannot_trust_bytes_replaced_after_provider_identification(self):
        from floati.wake_daemon_adapters import CodexQueueWakeAdapter
        self.bind(legacy=True)
        new = self.upgrade(b"#!/bin/sh\nprintf 'codex-cli 0.164.0\\n'\n")
        original_write = AdapterBindingStore.write
        def replace_before_write(store, *args, **kwargs):
            new.write_bytes(b'#!/bin/sh\nexit 0\n')
            return original_write(store, *args, **kwargs)
        with mock.patch.object(AdapterBindingStore, 'write', replace_before_write):
            from floati.errors import ProtocolRefusal
            with self.assertRaises(ProtocolRefusal):
                CodexQueueWakeAdapter(self.coordinate).exact_binding()

    def test_package_removal_during_provider_identification_is_a_typed_refusal(self):
        from floati import codex_binding_repair
        from floati.wake_daemon_adapters import CodexQueueWakeAdapter
        from floati.errors import ProtocolRefusal
        self.bind(legacy=True)
        new = self.upgrade(b"#!/bin/sh\nprintf 'codex-cli 0.164.0\\n'\n")
        identify = codex_binding_repair._provider_banner
        def remove_after_identification(*args):
            banner = identify(*args)
            new.unlink()
            return banner
        with mock.patch.object(codex_binding_repair, '_provider_banner', remove_after_identification):
            with self.assertRaises(ProtocolRefusal) as caught:
                CodexQueueWakeAdapter(self.coordinate).exact_binding()
        self.assertEqual('wake_daemon_codex_identity_changed', caught.exception.code)

    def test_intact_recorded_target_boards_without_authorizing_a_changed_declaration(self):
        from floati.errors import ProtocolRefusal
        from floati.wake_daemon_adapters import CodexQueueWakeAdapter
        from tests.test_wake_daemon_adapters import _Runner
        self.bind(legacy=True)
        new = self.binary('2', b'#!/bin/sh\nexit 0\n')
        self.link.unlink()
        before = self.store.path(self.coordinate).read_bytes()
        for state, refusal in (
                ('absent', 'wake_daemon_codex_executable_absent'),
                ('retargeted', 'wake_daemon_executable_digest_mismatch')):
            with self.subTest(declaration=state):
                if state == 'retargeted':
                    self.link.symlink_to(new)
                status, artifact = self.cli(['seat', 'board', '--root', str(self.root.path),
                    '--as', self.node, '--session', 'new-session', '--workspace', str(self.workspace),
                    '--idempotency-key', 'retained-target-' + state])
                self.assertEqual(0, status, artifact)
                self.assertEqual('completed', artifact['evidence']['step'])
                self.assertEqual(before, self.store.path(self.coordinate).read_bytes())
                runner = _Runner()
                adapter = CodexQueueWakeAdapter(self.coordinate, runner=runner)
                with self.assertRaises(ProtocolRefusal) as caught:
                    adapter.request_wake(adapter.exact_binding(), 'mail arrived', 30)
                self.assertEqual(refusal, caught.exception.code)
                self.assertEqual([], runner.calls)

    def test_interrupted_repair_reuses_testimony_after_a_lifecycle_roll(self):
        from floati.codex_binding_repair import repair_codex_binding
        from floati.errors import ProtocolRefusal
        from floati.jsonl import read_records_snapshot
        from floati.wake_daemon_contract import DAEMON_KINDS
        from floati.wake_daemon_roll import ensure_rolled, lifecycle_relative
        self.bind(legacy=True)
        self.upgrade(b"#!/bin/sh\nprintf 'codex-cli 0.164.0\\n'\n")
        with mock.patch.object(AdapterBindingStore, 'write', side_effect=OSError('interrupted replacement')):
            with self.assertRaises(ProtocolRefusal):
                repair_codex_binding(self.coordinate)
        ensure_rolled(self.root, self.node)
        repair_codex_binding(self.coordinate)
        paths = [lifecycle_relative(self.node)] + [path.relative_to(self.root.path)
            for path in (self.root.path / 'receipts/wake-daemon').glob('archive-*/*.jsonl')]
        repairs = [row for path in paths for row in read_records_snapshot(
            self.root, path, allowed_kinds=DAEMON_KINDS) if row['kind'] == 'codex_binding_repair_receipt']
        self.assertEqual(1, len(repairs))

    def test_upgrade_recovers_a_partially_resumed_board(self):
        from floati.errors import ProtocolRefusal
        from floati.wake_control import WakeController
        self.bind(legacy=True)
        status_method = WakeController.status
        calls = []
        def interrupt_after_resume(controller, *args):
            calls.append(None)
            if len(calls) == 2:
                raise ProtocolRefusal('fixture_interruption', 'interrupted after resume')
            return status_method(controller, *args)
        with mock.patch.object(WakeController, 'status', interrupt_after_resume):
            with self.assertRaises(ProtocolRefusal):
                self.board()
        self.upgrade(b"#!/bin/sh\nprintf 'codex-cli 0.164.0\\n'\n")
        self.assertEqual('completed', self.board()['step'])

    def rebind_argv(self):
        return ["wake", "daemon", "bind", "--root", str(self.root.path),
            "--as", self.node, "--harness", "codex", "--session", "new-session",
            "--workspace", str(self.workspace), "--executable", str(self.link),
            "--binding-epoch", "1"]

    def test_rebind_cannot_create_initial_binding(self):
        status, artifact = self.cli(self.rebind_argv())
        self.assertEqual(20, status, artifact)
        self.assertFalse(self.store.path(self.coordinate).exists())

    def test_rebind_cannot_change_executable_session_workspace_or_epoch(self):
        self.bind(legacy=True)
        original = self.store.path(self.coordinate).read_bytes()
        for flag, value in (("--executable", str(self.old)), ("--session", "foreign-session"),
                ("--workspace", str(self.base)), ("--binding-epoch", "2")):
            with self.subTest(flag=flag):
                argv = self.rebind_argv()
                argv[argv.index(flag) + 1] = value
                status, artifact = self.cli(argv)
                self.assertEqual(20, status, artifact)
                self.assertEqual(original, self.store.path(self.coordinate).read_bytes())

    def test_rebind_requires_current_claim_and_declaration(self):
        self.bind(legacy=True)
        original = self.store.path(self.coordinate).read_bytes()
        seat = self.workspace / "SEAT.json"
        declaration = seat.read_bytes()
        seat.unlink()
        status, artifact = self.cli(self.rebind_argv())
        self.assertEqual(20, status, artifact)
        self.assertEqual(original, self.store.path(self.coordinate).read_bytes())
        seat.write_bytes(declaration)
        self.arm_session("successor")
        status, artifact = self.cli(self.rebind_argv())
        self.assertEqual(20, status, artifact)
        self.assertEqual(original, self.store.path(self.coordinate).read_bytes())

    def test_rebind_requires_current_consent(self):
        self.bind(legacy=True)
        original = self.store.path(self.coordinate).read_bytes()
        CodexWaitConsentLedger(self.root).arm(self.participant.binding,
            hook_timeout_seconds=11, wait_deadline_seconds=2, idempotency_key="new-consent")
        status, artifact = self.cli(self.rebind_argv())
        self.assertEqual(20, status, artifact)
        self.assertEqual(original, self.store.path(self.coordinate).read_bytes())

    def test_upgrade_repairs_for_the_newly_boarded_claimant(self):
        self.bind(legacy=True)
        self.upgrade(b"#!/bin/sh\nprintf 'codex-cli 0.164.0\\n'\n")
        argv = ["seat", "board", "--root", str(self.root.path), "--as", self.node,
            "--session", "successor", "--workspace", str(self.workspace),
            "--idempotency-key", "successor-board", "--take-over"]
        status, artifact = self.cli(argv)
        self.assertEqual(0, status, artifact)
        self.assertEqual("successor", self.store.read(self.coordinate)["session_id"])

    def test_package_removal_between_binding_read_and_launch_is_typed(self):
        from floati.errors import ProtocolRefusal
        from floati.wake_daemon_adapters import CodexQueueWakeAdapter
        from tests.test_wake_daemon_adapters import _Runner
        binding = self.bind()
        original_read = Path.read_bytes
        executable_reads = 0

        def remove_before_second_read(path):
            nonlocal executable_reads
            if path == self.old:
                executable_reads += 1
                if executable_reads == 2:
                    path.unlink()
            return original_read(path)

        runner = _Runner()
        with mock.patch.object(Path, "read_bytes", remove_before_second_read):
            with self.assertRaises(ProtocolRefusal) as caught:
                CodexQueueWakeAdapter(self.coordinate, runner=runner).request_wake(binding, "wake", 30)
        self.assertIn(str(self.old), caught.exception.detail)
        self.assertIn("floati wake daemon bind", caught.exception.remedy)
        self.assertEqual([], runner.calls)

    def test_declared_path_removal_before_launch_keeps_specific_rebind_recovery(self):
        from floati.errors import ProtocolRefusal
        from floati.wake_daemon_adapters import CodexQueueWakeAdapter
        from tests.test_wake_daemon_adapters import _Runner
        binding = self.bind()
        before = self.store.path(self.coordinate).read_bytes()
        runner = _Runner()
        adapter = CodexQueueWakeAdapter(self.coordinate, runner=runner)
        require_current = adapter._require_current

        def remove_after_validation(value):
            current = require_current(value)
            self.link.unlink()
            return current

        with mock.patch.object(adapter, "_require_current", remove_after_validation):
            with self.assertRaises(ProtocolRefusal) as caught:
                adapter.request_wake(binding, "wake", 30)
        refusal = caught.exception
        self.assertEqual("wake_daemon_codex_executable_absent", refusal.code)
        self.assertIn(str(self.link), refusal.detail)
        self.assertEqual([], runner.calls)
        self.assertEqual(before, self.store.path(self.coordinate).read_bytes())
        remedy = shlex.split(refusal.remedy)
        self.assertEqual(["floati", *self.rebind_argv()], remedy)

        replacement = self.binary("2", b"#!/bin/sh\n# reviewed replacement\nexit 0\n")
        self.link.symlink_to(replacement)
        status, rebound = self.cli(remedy[1:])
        self.assertEqual(0, status, rebound)
        self.assertEqual("completed", self.board()["step"])

    def test_stable_declaration_remains_intact_for_seat_retirement(self):
        from floati.seat_waiter_lifecycle import retire_stale_adapter
        self.bind()
        before = self.store.path(self.coordinate).read_bytes()
        result = retire_stale_adapter(self.root, self.node, "keep-stable")
        self.assertEqual("unchanged", result["state"])
        self.assertEqual(before, self.store.path(self.coordinate).read_bytes())
        self.assertTrue(self.old.is_file())

    def test_version_inventory_does_not_execute_a_swapped_bound_binary(self):
        from floati.harness_versions import harness_version_artifact
        self.old.write_bytes(b"#!/bin/sh\nprintf 'codex original\\n'\n")
        self.bind()
        original = harness_version_artifact(self.root, environ={"PATH": ""})
        self.assertEqual("codex original", original["declared"][0]["version"])
        marker = self.base / "swapped-ran"
        self.upgrade(("#!/bin/sh\ntouch " + shlex.quote(str(marker))
            + "\nprintf 'codex swapped\\n'\n").encode())
        artifact = harness_version_artifact(self.root, environ={"PATH": ""})
        self.assertFalse(marker.exists(), artifact)
        self.assertIsNone(artifact["declared"][0]["version"])
        self.assertEqual('wake_daemon_executable_invalid', artifact['declared'][0]['version_reason'])

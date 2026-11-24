from __future__ import annotations

from tests.test_cli import LAUNCHER

from floati import fixture_ids as public_ids

import hashlib
import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from floati.errors import ProtocolRefusal
from floati.events import EventLog
from floati.jsonl import read_records_snapshot
from floati.registry import Registry
from floati.root import FloatiRoot
from tests.schema_validation import SchemaValidationError, validate_json_schema
from tests.temp_roots import REAL_TEMP_ROOT


REPOSITORY_ROOT = Path(__file__).parents[1]


class WakeControlTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.root = FloatiRoot.open_direct_home(self.base / "demo-fleet", create=True)
        Registry(self.root).register(public_ids.builder('a'), "Codex")
        Registry(self.root).register(public_ids.builder('b'), "Cursor")

    def rows(self, node: str) -> list[dict]:
        return read_records_snapshot(
            self.root,
            f"receipts/wake-control/{node}.jsonl",
            allowed_kinds={"wake_control_receipt"},
        )

    def test_pause_is_marker_only_receipted_and_status_names_unknowns(self) -> None:
        """Catches pause editing hook registration or representing pause as absence."""
        from floati.wake_control import WakeController

        hook = REPOSITORY_ROOT / ".githooks" / "pre-commit"
        before_hook = hook.read_bytes()
        controller = WakeController(self.root)
        paused = controller.pause(
            public_ids.builder('a'), "session-one", idempotency_key="pause-session-one"
        )
        status = controller.status(public_ids.builder('a'), "session-one")

        self.assertEqual("paused", paused["state"])
        self.assertEqual("paused", status["state"])
        self.assertEqual(public_ids.builder('a'), status["paused_by"])
        self.assertIn("paused by you at", status["display"])
        self.assertEqual("unknown", status["cached_session_state"])
        self.assertEqual("unknown", status["harness_trust_gate"])
        self.assertTrue(Path(paused["marker"]).is_file())
        self.assertEqual(before_hook, hook.read_bytes())
        self.assertEqual(["pause"], [row["operation"] for row in self.rows(public_ids.builder('a'))])

    def test_pause_and_resume_affect_exactly_one_node_session(self) -> None:
        """Catches a global marker or digest collision controlling another session."""
        from floati.wake_control import WakeController

        controller = WakeController(self.root)
        controller.pause(public_ids.builder('a'), "session-one", idempotency_key="pause-a-one")
        controller.pause(public_ids.builder('a'), "session-two", idempotency_key="pause-a-two")
        controller.pause(public_ids.builder('b'), "session-one", idempotency_key="pause-b-one")

        resumed = controller.resume(
            public_ids.builder('a'), "session-one", idempotency_key="resume-a-one"
        )

        self.assertEqual("active", resumed["state"])
        self.assertEqual("active", controller.status(public_ids.builder('a'), "session-one")["state"])
        self.assertEqual("paused", controller.status(public_ids.builder('a'), "session-two")["state"])
        self.assertEqual("paused", controller.status(public_ids.builder('b'), "session-one")["state"])
        self.assertEqual(["pause", "pause", "resume"], [row["operation"] for row in self.rows(public_ids.builder('a'))])

    def test_wildcards_global_and_unknown_sessions_refuse_without_state(self) -> None:
        """Catches a convenience selector widening one invocation beyond one session."""
        from floati.wake_control import WakeController

        controller = WakeController(self.root)
        for session in ("*", "all", "global", "session/../other", ""):
            with self.subTest(session=session):
                with self.assertRaises(ProtocolRefusal):
                    controller.pause(public_ids.builder('a'), session, idempotency_key="unsafe")
        with self.assertRaises(ProtocolRefusal):
            controller.resume(public_ids.builder('a'), "never-paused", idempotency_key="absent")
        self.assertFalse((self.root.path / "receipts" / "wake-control").exists())

    def test_waiter_treats_receipted_pause_as_intentional_silence(self) -> None:
        """Catches a paused session consuming mail or recording a false wake outcome."""
        from floati.codex_wait import run_stop_waiter
        from floati.codex_wait_contract import CodexWaitConsentLedger, resolve_participant
        from floati.wake_control import WakeController

        workspace = self.base / "workspace"
        workspace.mkdir()
        map_path = self.root.path / "codex-wait" / "workspaces.v0.json"
        map_path.parent.mkdir()
        map_path.write_text(
            json.dumps({
                "schema_version": 0,
                "tenant_id": self.root.tenant_id,
                "mappings": [{"workspace": str(workspace), "node_id": public_ids.builder('a')}],
            }, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        participant = resolve_participant(self.root.path, workspace)
        assert participant is not None
        CodexWaitConsentLedger(self.root).arm(
            participant.binding,
            hook_timeout_seconds=10,
            wait_deadline_seconds=2,
            idempotency_key="pause-test-consent",
        )
        EventLog(self.root).send(
            public_ids.builder('b'), public_ids.builder('a'), "floati", "a" * 40,
            "docs/evidence/ping.md", "ping", idempotency_key="pause-ping",
        )
        WakeController(self.root).pause(
            public_ids.builder('a'), "session-paused", idempotency_key="pause-waiter"
        )
        stdout = io.StringIO()

        result = run_stop_waiter(
            bus_home=self.root.path,
            hook_payload={"cwd": str(workspace), "session_id": "session-paused"},
            stdout=stdout,
            stderr=io.StringIO(),
        )

        self.assertEqual(0, result)
        self.assertEqual("", stdout.getvalue())
        self.assertFalse((self.root.path / "receipts" / "wakes" / public_ids.ledger(public_ids.builder('a'))).exists())

    def test_cli_requires_one_exact_session_and_emits_status_artifacts(self) -> None:
        """Catches parser aliases or omitted identity reaching the controller."""
        def run(*arguments: str) -> subprocess.CompletedProcess[str]:
            return subprocess.run(
                [str(LAUNCHER), *arguments],
                cwd=REPOSITORY_ROOT,
                env=dict(os.environ),
                text=True,
                capture_output=True,
                check=False,
            )

        paused = run(
            "wake", "pause", "--root", str(self.root.path),
            "--as", public_ids.builder('a'), "--session", "cli-session",
        )
        status = run(
            "wake", "status", "--root", str(self.root.path),
            "--as", public_ids.builder('a'), "--session", "cli-session",
        )
        refused = run(
            "wake", "pause", "--root", str(self.root.path),
            "--as", public_ids.builder('a'), "--session", "*",
        )

        self.assertEqual(0, paused.returncode, paused.stderr)
        self.assertEqual("paused", json.loads(paused.stdout)["evidence"]["state"])
        self.assertEqual(0, status.returncode, status.stderr)
        self.assertIn("paused by you", json.loads(status.stdout)["evidence"]["display"])
        self.assertEqual(20, refused.returncode)

    def test_reserved_pause_reconciles_exact_marker_after_abrupt_receipt_cut(self):
        from floati.wake_control import WakeController
        controller = WakeController(self.root)
        node = public_ids.builder('a')
        reserved = []
        with mock.patch.object(controller, "_append", side_effect=KeyboardInterrupt("after marker")):
            with self.assertRaises(KeyboardInterrupt):
                controller.pause(node, "reserved-session", idempotency_key="reserved-pause", reserve_receipt=reserved.append)
        self.assertEqual([], self.rows(node))
        expected = reserved[0]
        self.assertEqual(expected, controller.reconcile_pause(node, "reserved-session", expected_receipt=expected))
        resumed = controller.resume(node, "reserved-session", idempotency_key="reserved-resume", expected_pause_receipt_id=expected["id"])
        self.assertFalse(Path(resumed["marker"]).exists())
        self.assertEqual(expected["id"], self.rows(node)[1]["predecessor_receipt_id"])

    def test_reconcile_absent_reserved_marker_does_not_recreate_pause(self):
        from floati.wake_control import WakeController
        controller = WakeController(self.root)
        node = public_ids.builder('a')
        reserved = []
        paused = controller.pause(node, "absent-session", idempotency_key="absent-pause", reserve_receipt=reserved.append)
        controller.resume(node, "absent-session", idempotency_key="caller-resume")
        prior = self.rows(node)
        self.assertIsNone(controller.reconcile_pause(node, "absent-session", expected_receipt=reserved[0]))
        self.assertEqual(prior, self.rows(node))
        self.assertFalse(Path(paused["marker"]).exists())

    def test_conditional_resume_rejects_superseding_marker_under_its_lock(self):
        from floati.wake_control import WakeController
        controller = WakeController(self.root)
        node = public_ids.builder('a')
        owned = controller.pause(node, "superseded-session", idempotency_key="owned-pause")
        controller.resume(node, "superseded-session", idempotency_key="caller-resume")
        foreign = controller.pause(node, "superseded-session", idempotency_key="foreign-pause")
        marker = Path(foreign["marker"])
        before, rows = marker.read_bytes(), self.rows(node)
        with self.assertRaises(ProtocolRefusal) as caught:
            controller.resume(node, "superseded-session", idempotency_key="conditional-resume", expected_pause_receipt_id=owned["receipt"]["id"])
        self.assertEqual("wake_pause_intent_conflict", caught.exception.code)
        self.assertEqual(before, marker.read_bytes())
        self.assertEqual(rows, self.rows(node))

    def test_conditional_resume_preserves_replacement_raced_before_quarantine(self):
        from floati import codex_hook_install
        from floati.wake_control import WakeController
        controller = WakeController(self.root)
        node = public_ids.builder('a')
        owned = controller.pause(node, "race-session", idempotency_key="race-pause")
        marker = Path(owned["marker"])
        replacement = marker.read_bytes().replace(owned["receipt"]["id"].encode(), ("wake-control-" + "f" * 32).encode())
        original = codex_hook_install._rename_generation_noreplace
        raced = []
        def replace_then_move(source, target):
            if source == marker and not raced:
                raced.append(True)
                fresh = marker.with_name("foreign-marker-fixture")
                fresh.write_bytes(replacement)
                fresh.chmod(0o600)
                os.replace(fresh, marker)
            return original(source, target)
        with mock.patch.object(codex_hook_install, "_rename_generation_noreplace", side_effect=replace_then_move):
            with self.assertRaises(ProtocolRefusal) as caught:
                controller.resume(node, "race-session", idempotency_key="race-resume", expected_pause_receipt_id=owned["receipt"]["id"])
        self.assertEqual("wake_pause_intent_conflict", caught.exception.code)
        self.assertEqual(replacement, marker.read_bytes())
        self.assertEqual(["pause"], [row["operation"] for row in self.rows(node)])

    def test_conditional_resume_retries_owned_quarantine_and_receipt_cuts(self):
        from floati import codex_hook_install
        from floati.wake_control import WakeController
        controller = WakeController(self.root)
        node = public_ids.builder('a')
        for cut in ("quarantine", "receipt"):
            with self.subTest(cut=cut):
                session = "cut-" + cut
                owned = controller.pause(node, session, idempotency_key="pause-" + cut)
                marker = Path(owned["marker"])
                receipt_id = owned["receipt"]["id"]
                quarantine = controller._resume_quarantine(marker, receipt_id)
                original_rename = codex_hook_install._rename_generation_noreplace
                original_append = controller._append
                def interrupted_rename(source, target):
                    original_rename(source, target)
                    if source == marker:
                        raise KeyboardInterrupt("after quarantine rename")
                def interrupted_append(target_node, row):
                    result = original_append(target_node, row)
                    if row["operation"] == "resume":
                        raise KeyboardInterrupt("after resume receipt append")
                    return result
                patcher = mock.patch.object(codex_hook_install, "_rename_generation_noreplace", side_effect=interrupted_rename) if cut == "quarantine" else mock.patch.object(controller, "_append", side_effect=interrupted_append)
                with patcher:
                    with self.assertRaises(KeyboardInterrupt):
                        controller.resume(node, session, idempotency_key="resume-" + cut, expected_pause_receipt_id=receipt_id)
                self.assertFalse(marker.exists())
                self.assertTrue(quarantine.exists())
                controller.reconcile_pause(node, session, expected_receipt=owned["receipt"])
                resumed = controller.resume(node, session, idempotency_key="resume-" + cut, expected_pause_receipt_id=receipt_id)
                self.assertEqual(str(marker), resumed["marker"])
                self.assertFalse(marker.exists())
                self.assertFalse(quarantine.exists())
                matching = [row for row in self.rows(node) if row["idempotency_key"] == "resume-" + cut]
                self.assertEqual(1, len(matching))

    def test_conditional_resume_preserves_quarantine_replaced_during_receipt_append(self):
        from floati.wake_control import WakeController
        controller = WakeController(self.root)
        node = public_ids.builder('a')
        owned = controller.pause(node, "late-race-session", idempotency_key="late-race-pause")
        marker = Path(owned["marker"])
        quarantine = controller._resume_quarantine(marker, owned["receipt"]["id"])
        replacement = b"caller replacement must survive\n"
        original_append = controller._append
        def replace_after_append(target_node, row):
            result = original_append(target_node, row)
            if row["operation"] == "resume":
                fresh = quarantine.with_name("foreign-quarantine-fixture")
                fresh.write_bytes(replacement)
                fresh.chmod(0o600)
                os.replace(fresh, quarantine)
            return result
        with mock.patch.object(controller, "_append", side_effect=replace_after_append):
            with self.assertRaises(ProtocolRefusal) as caught:
                controller.resume(node, "late-race-session", idempotency_key="late-race-resume", expected_pause_receipt_id=owned["receipt"]["id"])
        self.assertEqual("wake_pause_intent_conflict", caught.exception.code)
        self.assertEqual(replacement, quarantine.read_bytes())
        self.assertFalse(marker.exists())
        self.assertEqual(["pause", "resume"], [row["operation"] for row in self.rows(node)])

    def test_reserved_receipt_cannot_adopt_a_different_native_key(self):
        from floati.wake_control import WakeController
        controller = WakeController(self.root)
        node = public_ids.builder('a')
        paused = controller.pause(node, "key-session", idempotency_key="owned-key")
        marker = Path(paused["marker"])
        candidate = {**paused["receipt"], "idempotency_key": "tampered-key"}
        before = marker.read_bytes()
        with self.assertRaises(ProtocolRefusal) as caught:
            controller.reconcile_pause(node, "key-session", expected_receipt=candidate)
        self.assertEqual("wake_pause_intent_conflict", caught.exception.code)
        self.assertEqual(before, marker.read_bytes())
        self.assertEqual(1, len(self.rows(node)))

    def test_receipt_schema_is_closed_and_matches_runtime_validation(self) -> None:
        """Catches schema/runtime drift or an unreceipted pause state field."""
        from floati.wake_control import WakeController

        WakeController(self.root).pause(
            public_ids.builder('a'), "schema-session", idempotency_key="schema-pause"
        )
        row = self.rows(public_ids.builder('a'))[0]
        schema = REPOSITORY_ROOT / "schemas" / "v0" / "wake-control-receipt.schema.json"
        validate_json_schema(row, schema)
        with self.assertRaises(SchemaValidationError):
            validate_json_schema(dict(row, global_pause=True), schema)


if __name__ == "__main__":
    unittest.main()

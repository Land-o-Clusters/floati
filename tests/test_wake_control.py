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

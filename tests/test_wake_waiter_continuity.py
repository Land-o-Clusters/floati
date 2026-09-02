from __future__ import annotations

import hashlib
import io
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from floati import fixture_ids as public_ids
from floati.codex_wait_contract import (
    CodexWaitConsentLedger,
    CodexWaitSessionLedger,
    resolve_participant,
)
from floati.events import EventLog
from floati.jsonl import read_records_snapshot
from floati.planes import AuthorityGrantStore
from floati.registry import Registry
from floati.root import FloatiRoot
from tests.schema_validation import validate_json_schema
from tests.temp_roots import REAL_TEMP_ROOT


class WakeWaiterContinuityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)

    def fixture(self, label: str) -> tuple[FloatiRoot, Path, object]:
        home = self.base / label / "wake-waiter-continuity"
        root = FloatiRoot.open_direct_home(home, create=True)
        Registry(root).register("architect", "architect")
        Registry(root).register(public_ids.builder("floati"), "Codex")
        workspace = self.base / label / "workspace"
        workspace.mkdir()
        mapping = home / "codex-wait" / "workspaces.v0.json"
        mapping.parent.mkdir()
        mapping.write_text(
            json.dumps(
                {
                    "schema_version": 0,
                    "tenant_id": root.tenant_id,
                    "mappings": [
                        {"workspace": str(workspace), "node_id": public_ids.builder("floati")}
                    ],
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n",
            encoding="utf-8",
        )
        participant = resolve_participant(home, workspace)
        self.assertIsNotNone(participant)
        assert participant is not None
        CodexWaitConsentLedger(root).arm(
            participant.binding,
            hook_timeout_seconds=10,
            wait_deadline_seconds=2,
            idempotency_key="consent-" + label,
        )
        return root, workspace, participant

    @staticmethod
    def rows(root: FloatiRoot) -> list[dict[str, object]]:
        return read_records_snapshot(
            root,
            public_ids.compose("receipts/wake-waiter-exit/", public_ids.ledger(public_ids.builder("floati"))),
            allowed_kinds={"wake_waiter_exit_receipt"},
        )

    def test_closed_exit_ledger_records_every_decline_reason_idempotently(self) -> None:
        from floati.wake_exit import WakeExitLedger

        root, _workspace, _participant = self.fixture("exit-ledger")
        digest = hashlib.sha256(b"session-one").hexdigest()
        receipts = []
        for reason in (
            "exhausted", "paused", "not_claimant", "consent_withdrawn",
            "breaker", "integrity_failure",
        ):
            with self.subTest(reason=reason):
                receipt = WakeExitLedger(root).record(
                    node_id=public_ids.builder("floati"),
                    session_digest=digest,
                    reason_code=reason,
                    waited_seconds=2 if reason == "exhausted" else 0,
                    idempotency_key="exit-" + reason,
                )
                replay = WakeExitLedger(root).record(
                    node_id=public_ids.builder("floati"),
                    session_digest=digest,
                    reason_code=reason,
                    waited_seconds=2 if reason == "exhausted" else 0,
                    idempotency_key="exit-" + reason,
                )
                self.assertEqual(receipt, replay)
                self.assertEqual(reason, receipt["reason_code"])
                validate_json_schema(
                    receipt, Path("schemas/v1/wake-waiter-exit-receipt.schema.json")
                )
                receipts.append(receipt)
        self.assertEqual(6, len(self.rows(root)))
        self.assertEqual(6, len({str(row["id"]) for row in receipts}))

    def test_waiter_records_all_five_terminal_declines(self) -> None:
        from floati.codex_wait import run_stop_waiter
        from floati.errors import IntegrityFailure
        from floati.wake_control import WakeController

        # exhausted
        root, workspace, _participant = self.fixture("exhausted")
        clock = [0.0]
        run_stop_waiter(
            bus_home=root.path,
            hook_payload={"cwd": str(workspace), "session_id": "seat-exhausted"},
            stdout=io.StringIO(),
            stderr=io.StringIO(),
            monotonic=lambda: clock[0],
            sleep=lambda seconds: clock.__setitem__(0, clock[0] + seconds),
        )
        self.assertEqual(["exhausted"], [row["reason_code"] for row in self.rows(root)])

        # paused
        root, workspace, _participant = self.fixture("paused")
        WakeController(root).pause(
            public_ids.builder("floati"), "seat-paused", idempotency_key="pause-seat"
        )
        run_stop_waiter(
            bus_home=root.path,
            hook_payload={"cwd": str(workspace), "session_id": "seat-paused"},
            stdout=io.StringIO(), stderr=io.StringIO(),
        )
        self.assertEqual(["paused"], [row["reason_code"] for row in self.rows(root)])

        # not claimant after an in-flight takeover
        root, workspace, participant = self.fixture("not-claimant")
        consent = CodexWaitConsentLedger(root).require_armed(participant.binding)
        CodexWaitSessionLedger(root).arm(
            participant.binding, consent, "seat-primary", idempotency_key="primary"
        )
        clock = [0.0]

        def takeover(seconds: float) -> None:
            clock[0] += seconds
            CodexWaitSessionLedger(root).arm(
                participant.binding,
                consent,
                "seat-takeover",
                idempotency_key="takeover",
            )

        run_stop_waiter(
            bus_home=root.path,
            hook_payload={"cwd": str(workspace), "session_id": "seat-primary"},
            stdout=io.StringIO(), stderr=io.StringIO(),
            monotonic=lambda: clock[0], sleep=takeover,
        )
        self.assertEqual(["not_claimant"], [row["reason_code"] for row in self.rows(root)])

        # breaker
        root, workspace, participant = self.fixture("breaker")
        consent = CodexWaitConsentLedger(root).require_armed(participant.binding)
        CodexWaitSessionLedger(root).arm(
            participant.binding, consent, "seat-breaker", idempotency_key="breaker-arm"
        )
        with mock.patch("floati.codex_wait._breaker_tripped", return_value=True):
            run_stop_waiter(
                bus_home=root.path,
                hook_payload={"cwd": str(workspace), "session_id": "seat-breaker"},
                stdout=io.StringIO(), stderr=io.StringIO(),
            )
        self.assertEqual(["breaker"], [row["reason_code"] for row in self.rows(root)])

        # integrity failure
        root, workspace, participant = self.fixture("integrity")
        consent = CodexWaitConsentLedger(root).require_armed(participant.binding)
        CodexWaitSessionLedger(root).arm(
            participant.binding, consent, "seat-integrity", idempotency_key="integrity-arm"
        )
        with mock.patch(
            "floati.codex_wait.WakeHoldController.evaluate",
            side_effect=IntegrityFailure("fixture_integrity", "fixture"),
        ):
            run_stop_waiter(
                bus_home=root.path,
                hook_payload={"cwd": str(workspace), "session_id": "seat-integrity"},
                stdout=io.StringIO(), stderr=io.StringIO(),
            )
        self.assertEqual(
            ["integrity_failure"], [row["reason_code"] for row in self.rows(root)]
        )

    def test_waiter_reopens_after_epoch_replace_without_replaying_delivered_work(self) -> None:
        from floati.bus_epoch import roll_bus_epoch
        from floati.codex_wait import run_stop_waiter

        root, workspace, participant = self.fixture("epoch-reopen")
        first = EventLog(root).send(
            "architect", public_ids.builder("floati"), "floati", "a" * 40,
            "docs/evidence/first.md", "first", idempotency_key="first",
        )
        first_stdout = io.StringIO()
        run_stop_waiter(
            bus_home=root.path,
            hook_payload={"cwd": str(workspace), "session_id": "seat-reopen"},
            stdout=first_stdout, stderr=io.StringIO(),
        )
        self.assertIn(str(first["id"]), first_stdout.getvalue())

        now = datetime.now(timezone.utc)
        AuthorityGrantStore(root).grant_exact(
            "bus-epoch-roll", "architect", 1, now
        )
        clock = [0.0]
        later: list[dict[str, object]] = []

        def sleep(seconds: float) -> None:
            clock[0] += seconds
            if later:
                return
            roll_bus_epoch(
                root, actor="architect", idempotency_key="waiter-reopen-roll"
            )
            later.append(EventLog(root).send(
                "architect", public_ids.builder("floati"), "floati", "b" * 40,
                "docs/evidence/later.md", "later", idempotency_key="later",
            )["message"])

        stdout = io.StringIO()
        run_stop_waiter(
            bus_home=root.path,
            hook_payload={"cwd": str(workspace), "session_id": "seat-reopen"},
            stdout=stdout,
            stderr=io.StringIO(),
            monotonic=lambda: clock[0],
            sleep=sleep,
        )

        self.assertEqual(1.0, clock[0])
        self.assertIn(str(later[0]["id"]), stdout.getvalue())
        self.assertNotIn(str(first["id"]), stdout.getvalue())


if __name__ == "__main__":
    unittest.main()

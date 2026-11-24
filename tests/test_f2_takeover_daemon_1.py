"""F2-TAKEOVER-DAEMON-1: a take-over never trips the wake daemon on its own key.

Board Am.9 (2026-09-27). A Codex seat's daemon was bound to session A; a
take-over rebound it to session B (the trusted Codex Stop waiter writes the
new binding); the SAME running daemon then tripped
``wake_daemon_lifecycle_idempotency_conflict`` three times, its logs were
empty, and the breaker notice told the seat to "rebind the wake daemon" -
which, for Codex, is ``wake daemon bind``, refused by design.

Mechanism (measured here, not assumed): the lifecycle receipt key is
``<daemon instance>-<cycle_index>-<event>``. A session change re-initializes
the runtime with ``cycle_index`` back at 0 while the running process keeps
its instance id, so the new session's first cycle recomposes the
predecessor's first key with different content.

Every root, home and binding here is a scratch fixture under the real temp
root; nothing touches a live bus.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import shlex
import subprocess
import tempfile
import unittest
from pathlib import Path

from floati import fixture_ids as public_ids
from floati.errors import ProtocolRefusal
from floati.events import EventLog
from floati.jsonl import read_records
from floati.registry import Registry
from floati.root import FloatiRoot
from floati.wake_daemon_adapters import adapter_contract_digest
from floati.wake_daemon_contract import (
    AdapterBindingStore,
    DaemonConsentLedger,
    DaemonCoordinate,
    DaemonLifecycleLedger,
)
from floati.wake_daemon_roll import lifecycle_relative
from tests.temp_roots import REAL_TEMP_ROOT
from tests.test_wake_daemon import _Adapter, _TideEvaluator

CONFLICT = "wake_daemon_lifecycle_idempotency_conflict"
SESSION_A = "019a0f00-0000-7000-8000-00000000000a"
SESSION_B = "019a0f00-0000-7000-8000-00000000000b"


class _Fixture(unittest.TestCase):
    harness = "codex"

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.root = FloatiRoot.open_direct_home(self.base / "fleet", create=True)
        registry = Registry(self.root)
        registry.register("sender", "architect")
        registry.register(public_ids.builder("a"), "worker")
        self.workspace = self.base / "workspace"
        self.workspace.mkdir()
        self.executable = self.base / "harness-agent"
        self.executable.write_bytes(b"#!/bin/sh\nexit 0\n")
        self.executable.chmod(0o700)
        self.coordinate = DaemonCoordinate(
            self.root, public_ids.builder("a"), self.harness
        )
        self.adapter = _Adapter(self.root, self.coordinate)
        self.bind(SESSION_A, binding_epoch=1)
        DaemonConsentLedger(self.root).consent(
            self.coordinate,
            adapter_version="1",
            adapter_digest=adapter_contract_digest(self.harness),
            min_poll_seconds=1,
            max_poll_seconds=4,
            max_backoff_seconds=8,
            activation_epoch=1,
            idempotency_key="takeover-daemon-consent",
        )

    def bind(self, session_id: str, *, binding_epoch: int) -> None:
        # The record the trusted Codex Stop waiter writes for the session
        # that took over (record_codex_daemon_binding -> this store).
        AdapterBindingStore(self.root).write(
            self.coordinate,
            session_id=session_id,
            workspace=self.workspace,
            executable=self.executable,
            adapter_version="1",
            adapter_digest=adapter_contract_digest(self.harness),
            binding_epoch=binding_epoch,
        )

    def daemon(self):
        from floati.wake_daemon import WakeDaemon

        return WakeDaemon(self.coordinate, self.adapter, tide_evaluator=_TideEvaluator())

    def send(self, key: str) -> None:
        EventLog(self.root).send(
            "sender",
            public_ids.builder("a"),
            "floati",
            "a" * 40,
            "docs/evidence/f2-takeover-daemon-1.md",
            "takeover daemon evidence",
            idempotency_key=key,
        )

    def lifecycle(self) -> list[dict]:
        return read_records(
            self.root,
            lifecycle_relative(self.coordinate.node_id),
            allowed_kinds={"wake_daemon_lifecycle_receipt", "wake_daemon_consent_receipt"},
        )

    def lifecycle_receipts(self) -> list[dict]:
        return [
            row for row in self.lifecycle()
            if row.get("kind") == "wake_daemon_lifecycle_receipt"
        ]

    def serve_once(self, daemon, now: float) -> None:
        calls = iter((False, True))
        daemon.serve(lambda: next(calls, True), clock=lambda: now, sleep=lambda _s: None)

    def run_cycle_or_fail(self, daemon, now: float) -> dict:
        try:
            return daemon.run_cycle(now)
        except ProtocolRefusal as exc:
            self.fail(f"the take-over cycle refused: {exc.code}: {exc.detail}")


class TakeoverNextCycleTests(_Fixture):
    """RED at base: the new session's first cycle recomposes A's first key."""

    def test_the_cycle_after_a_takeover_wakes_the_new_session_without_tripping(self) -> None:
        daemon = self.daemon()
        self.send("takeover-before")
        self.assertEqual("woke", daemon.run_cycle(100.0)["state"])
        self.assertEqual(SESSION_A, self.adapter.calls[-1][0])

        self.bind(SESSION_B, binding_epoch=2)
        self.send("takeover-after")
        result = self.run_cycle_or_fail(daemon, 200.0)

        self.assertEqual("woke", result["state"])
        self.assertEqual(SESSION_B, self.adapter.calls[-1][0])
        self.assertEqual(2, len(self.adapter.calls))
        receipts = self.lifecycle_receipts()
        self.assertEqual([], [row for row in receipts if row.get("reason_code") == CONFLICT])
        keys = [row["idempotency_key"] for row in receipts]
        self.assertEqual(len(keys), len(set(keys)), "a take-over respent a lifecycle key")

    def test_a_serving_daemon_records_no_conflict_after_a_takeover(self) -> None:
        """The live shape: one serve process, the binding moves under it."""
        daemon = self.daemon()
        self.send("serve-before")
        self.serve_once(daemon, 100.0)
        self.bind(SESSION_B, binding_epoch=2)
        self.send("serve-after")
        self.serve_once(daemon, 200.0)

        reasons = [row.get("reason_code") for row in self.lifecycle_receipts()]
        self.assertNotIn(CONFLICT, reasons)
        last = self.lifecycle_receipts()[-1]
        self.assertEqual("wake_attempt", last["event"])
        self.assertEqual(SESSION_B, self.adapter.calls[-1][0])

    def test_a_takeover_back_to_the_first_session_respends_no_key(self) -> None:
        """A -> B -> A in one process: scoping the key by session is not enough."""
        daemon = self.daemon()
        for index, (session, epoch) in enumerate(
            ((SESSION_A, 1), (SESSION_B, 2), (SESSION_A, 3))
        ):
            self.bind(session, binding_epoch=epoch)
            self.send(f"round-trip-{index}")
            result = self.run_cycle_or_fail(daemon, 100.0 * (index + 1))
            self.assertEqual("woke", result["state"])
            self.assertEqual(session, self.adapter.calls[-1][0])
        keys = [row["idempotency_key"] for row in self.lifecycle_receipts()]
        self.assertEqual(3, len(keys))
        self.assertEqual(len(keys), len(set(keys)))

    def test_a_runtime_lost_mid_process_respends_no_key(self) -> None:
        daemon = self.daemon()
        self.send("lost-before")
        self.assertEqual("woke", daemon.run_cycle(100.0)["state"])
        daemon.runtime_path.unlink()
        self.send("lost-after")
        result = self.run_cycle_or_fail(daemon, 200.0)
        self.assertIn(result["state"], {"woke", "idle"})
        # Two cycles ran, so two receipts. A respent key whose content
        # happens to match is not refused - it silently returns the first
        # cycle's receipt and the second cycle leaves no testimony at all.
        receipts = self.lifecycle_receipts()
        self.assertEqual(2, len(receipts), "a cycle's lifecycle receipt was swallowed as a replay")
        self.assertEqual(receipts[-1]["id"], daemon.read_runtime()["last_lifecycle_receipt_id"])


class ForcedConflictTests(_Fixture):
    """RED at base: a conflict names no key and no act; the logs stay empty."""

    def seed_next_key(self, daemon, event: str) -> str:
        runtime = daemon.read_runtime()
        key = f"{runtime['daemon_instance_id']}-{int(runtime['cycle_index']) + 1}-{event}"
        DaemonLifecycleLedger(self.root).record(
            self.coordinate,
            daemon_instance_id=str(runtime["daemon_instance_id"]),
            activation_epoch=1,
            event="idle",
            state="idle",
            reason_code="forced-by-f2-takeover-daemon-1",
            adapter_digest=adapter_contract_digest(self.harness),
            plist_digest=None,
            session_digest=None,
            predecessor_receipt_id=None,
            idempotency_key=key,
        )
        return key

    def test_a_forced_conflict_names_its_key_and_an_act_that_clears_it(self) -> None:
        daemon = self.daemon()
        self.send("forced-first")
        self.assertEqual("woke", daemon.run_cycle(100.0)["state"])
        key = self.seed_next_key(daemon, "wake_attempt")
        self.send("forced-second")

        with self.assertRaises(ProtocolRefusal) as caught:
            daemon.run_cycle(200.0)
        refusal = caught.exception
        self.assertEqual(CONFLICT, refusal.code, "the guard must still refuse")
        self.assertIn(key, refusal.detail)
        remedy = refusal.remedy
        self.assertIsInstance(remedy, str, f"remedy is not an act: {remedy!r}")
        self.assertEqual(
            shlex.join(["rm", "--", str(daemon.runtime_path)]), remedy
        )

        # Perform the printed remedy exactly as typed; the next cycle clears.
        completed = subprocess.run(shlex.split(remedy), check=False)
        self.assertEqual(0, completed.returncode)
        before = len(self.lifecycle_receipts())
        result = self.run_cycle_or_fail(daemon, 300.0)
        # The refused cycle had already woken the seat (the conflict is at the
        # transition, after the adapter), so the mail now reads as held work.
        self.assertEqual("held", result["state"])
        receipts = self.lifecycle_receipts()
        self.assertEqual(before + 1, len(receipts))
        self.assertNotEqual(key, receipts[-1]["idempotency_key"])
        self.assertEqual(receipts[-1]["id"], daemon.read_runtime()["last_lifecycle_receipt_id"])

    def test_a_serving_daemon_logs_the_conflicting_key(self) -> None:
        daemon = self.daemon()
        self.send("logged-first")
        self.serve_once(daemon, 100.0)
        key = self.seed_next_key(daemon, "wake_attempt")
        self.send("logged-second")

        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            self.serve_once(daemon, 200.0)

        logged = stderr.getvalue()
        self.assertIn(CONFLICT, logged)
        self.assertIn(key, logged)
        self.assertIn(shlex.join(["rm", "--", str(daemon.runtime_path)]), logged)
        exception_rows = [
            row for row in self.lifecycle_receipts()
            if row.get("event") == "cycle_exception" and row.get("reason_code") == CONFLICT
        ]
        self.assertEqual(1, len(exception_rows))
        self.assertIn(key, exception_rows[0]["exception_message"])


class CodexBreakerRemedyTests(_Fixture):
    """RED at base: the Codex breaker notice prints the refused bind remedy."""

    def open_breaker(self):
        self.send("breaker-open")
        self.adapter.outcome = "unknown"
        daemon = self.daemon()
        for now in (100.0, 102.0, 106.0):
            daemon.run_cycle(now)
        return daemon

    def notice(self) -> dict:
        import json

        path = self.root.resolve_relative(
            Path("state/wake-daemon/notices") / f"{self.coordinate.digest}.json"
        )
        return json.loads(path.read_text(encoding="utf-8"))

    def test_the_codex_bind_path_the_old_remedy_names_is_refused(self) -> None:
        """Premise control: `wake daemon bind` refuses a Codex coordinate."""
        from floati.admin_cli import _wake_daemon_bind

        args = argparse.Namespace(
            root=str(self.root.path), actor=self.coordinate.node_id, harness="codex",
            session=SESSION_B, workspace=str(self.workspace),
            executable=str(self.executable), binding_epoch=9, yes=True,
            zcode_node_executable=None, zcode_entry_executable=None,
        )
        with self.assertRaises(ProtocolRefusal) as caught:
            _wake_daemon_bind(args)
        self.assertEqual("wake_daemon_codex_binding_source_invalid", caught.exception.code)

    def test_a_codex_breaker_notice_names_a_remedy_codex_can_perform(self) -> None:
        self.open_breaker()
        remedy = self.notice()["remedy"]
        self.assertNotIn("rebind the wake daemon", remedy)
        self.assertIn("codex-fleet-bus PROFILE board --take-over", remedy)
        self.assertIn("Stop waiter", remedy)

    def test_doctor_names_the_same_codex_remedy_for_an_open_breaker(self) -> None:
        from floati.doctor import project_wake_daemon_health

        self.open_breaker()
        findings = [
            finding
            for finding in project_wake_daemon_health(self.root, currency_current=True)
            if finding["code"] == "wake_daemon_health"
            and finding["subject"] == f"{self.coordinate.node_id}/codex"
        ]
        self.assertEqual(1, len(findings))
        self.assertEqual("warning", findings[0]["severity"])
        self.assertEqual(self.notice()["remedy"], findings[0]["remediation"])
        self.assertNotIn("rebind the wake daemon", findings[0]["remediation"])

    def test_the_codex_takeover_the_remedy_names_closes_the_breaker(self) -> None:
        """Control: the remedy's effect (a Stop-waiter rebind) recovers.

        Green at base too: the open breaker's cycles were not wake_attempt,
        so the new session's first key happens not to collide. The RED above
        is the case where the events line up."""
        daemon = self.open_breaker()
        self.assertEqual("open", daemon.read_runtime()["circuit_state"])
        self.bind(SESSION_B, binding_epoch=2)
        self.adapter.outcome = "woke"
        self.send("breaker-after-takeover")
        result = self.run_cycle_or_fail(daemon, 400.0)
        self.assertEqual("woke", result["state"])
        self.assertEqual("closed", result["circuit_state"])


class GreenControls(_Fixture):
    """Green at base and at the fix."""

    def test_a_daemon_with_no_takeover_composes_its_keys_unchanged(self) -> None:
        daemon = self.daemon()
        self.send("control-1")
        daemon.run_cycle(100.0)
        daemon.run_cycle(110.0)
        daemon.run_cycle(130.0)
        receipts = self.lifecycle_receipts()
        instance = daemon.read_runtime()["daemon_instance_id"]
        self.assertEqual(
            [f"{instance}-{index}-{row['event']}" for index, row in enumerate(receipts, 1)],
            [row["idempotency_key"] for row in receipts],
        )
        self.assertEqual(3, daemon.read_runtime()["cycle_index"])

    def test_a_new_daemon_instance_after_a_rebind_still_starts_its_count(self) -> None:
        self.send("control-new-instance-1")
        self.assertEqual("woke", self.daemon().run_cycle(100.0)["state"])
        self.bind(SESSION_B, binding_epoch=2)
        self.send("control-new-instance-2")
        fresh = self.daemon()
        self.assertEqual("woke", fresh.run_cycle(200.0)["state"])
        self.assertEqual(1, fresh.read_runtime()["cycle_index"])

    def test_a_same_content_replay_returns_the_existing_receipt(self) -> None:
        ledger = DaemonLifecycleLedger(self.root)
        arguments = dict(
            daemon_instance_id="control-instance",
            activation_epoch=1,
            event="idle",
            state="idle",
            reason_code=None,
            adapter_digest=adapter_contract_digest(self.harness),
            plist_digest=None,
            session_digest=None,
            predecessor_receipt_id=None,
            idempotency_key="control-replay-key",
        )
        first = ledger.record(self.coordinate, **arguments)
        self.assertEqual(first["id"], ledger.record(self.coordinate, **arguments)["id"])


class CursorBreakerControl(_Fixture):
    harness = "cursor"

    def test_a_non_codex_breaker_notice_keeps_its_remedy_byte_identical(self) -> None:
        import json

        from floati.wake_daemon import WAKE_BREAKER_REMEDY

        self.send("cursor-breaker")
        self.adapter.outcome = "unknown"
        daemon = self.daemon()
        for now in (100.0, 102.0, 106.0):
            daemon.run_cycle(now)
        path = self.root.resolve_relative(
            Path("state/wake-daemon/notices") / f"{self.coordinate.digest}.json"
        )
        self.assertEqual(
            WAKE_BREAKER_REMEDY, json.loads(path.read_text(encoding="utf-8"))["remedy"]
        )


if __name__ == "__main__":
    unittest.main()

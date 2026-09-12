"""FQ-8 / BL-4: a rebound seat records a fresh wake attempt, never a replay throw.

The attempt slot is scoped by the acting session digest. A seat rebind
reinitializes the runtime (cycle counters restart, the consent's
activation epoch persists), so without session scoping the recomputed
attempt key collides with the previous session's durable row — the
decision replays identically, only ``acting_session_id`` differs — and
the replay check throws ``wake_evidence_unknown`` at legitimate work.

The replay comparison itself is NOT loosened: same-session replay
returns the existing row, and a genuinely different replay still throws.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from floati import fixture_ids as public_ids
from floati.events import EventLog
from floati.jsonl import read_records_snapshot
from floati.registry import Registry
from floati.root import FloatiRoot
from floati.wake_daemon_adapters import (
    AdapterBinding,
    WakeAdapterResult,
    adapter_contract_digest,
)
from floati.wake_daemon_contract import (
    AdapterBindingStore,
    DaemonConsentLedger,
    DaemonCoordinate,
)
from tests.temp_roots import REAL_TEMP_ROOT

SESSION_A = "cursor-session-1"
SESSION_B = "cursor-session-2"


class _Adapter:
    def __init__(self, root: FloatiRoot, coordinate: DaemonCoordinate) -> None:
        self.root = root
        self.coordinate = coordinate

    def exact_binding(self) -> AdapterBinding:
        record = AdapterBindingStore(self.root).read(self.coordinate)
        return AdapterBinding.from_record(record)

    def observe_session(self, binding: AdapterBinding) -> str:
        return "unknown"

    def request_wake(
        self,
        binding: AdapterBinding,
        reason: str,
        deadline_seconds: int,
        envelopes: object = None,
    ) -> WakeAdapterResult:
        return WakeAdapterResult("woke", None, 0, "e" * 64)


class _TideEvaluator:
    def dispatch_held(self, node_id: str) -> bool:
        return False

    def evaluate(self, node_id: str, binding: object) -> dict:
        return {"state": "off", "node_id": node_id, "receipt": None}


class Fq8RebindAttemptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.root = FloatiRoot.open_direct_home(self.base / "fleet", create=True)
        registry = Registry(self.root)
        registry.register("sender", "architect")
        registry.register(public_ids.builder("a"), "worker")
        self.workspace = self.base / "workspace"
        self.workspace.mkdir()
        self.executable = self.base / "cursor-agent"
        self.executable.write_bytes(b"#!/bin/sh\nexit 0\n")
        self.executable.chmod(0o700)
        self.coordinate = DaemonCoordinate(self.root, public_ids.builder("a"), "cursor")
        self.bind_session(SESSION_A, binding_epoch=1)
        DaemonConsentLedger(self.root).consent(
            self.coordinate,
            adapter_version="1",
            adapter_digest=adapter_contract_digest("cursor"),
            min_poll_seconds=1,
            max_poll_seconds=4,
            max_backoff_seconds=8,
            activation_epoch=1,
            idempotency_key="fq8-consent",
        )

    def bind_session(self, session_id: str, *, binding_epoch: int) -> None:
        AdapterBindingStore(self.root).write(
            self.coordinate,
            session_id=session_id,
            workspace=self.workspace,
            executable=self.executable,
            adapter_version="1",
            adapter_digest=adapter_contract_digest("cursor"),
            binding_epoch=binding_epoch,
        )

    def daemon(self) -> object:
        from floati.wake_daemon import WakeDaemon

        return WakeDaemon(
            self.coordinate, _Adapter(self.root, self.coordinate), tide_evaluator=_TideEvaluator()
        )

    def send_untagged_mail(self, key: str) -> None:
        EventLog(self.root).send(
            "sender",
            public_ids.builder("a"),
            "floati",
            "a" * 40,
            "docs/evidence/fq-8.md",
            "fq-8 evidence",
            worker_session_id=None,
            idempotency_key=key,
        )

    def durable_rows(self) -> list[dict]:
        return read_records_snapshot(
            self.root,
            Path("receipts/wakes") / f"{self.coordinate.node_id}.jsonl",
            allowed_kinds={"wake_attempt_receipt"},
        )

    def drop_runtime(self) -> None:
        runtime_dir = self.root.resolve_relative(Path("state/wake-daemon/runtime"))
        if runtime_dir.is_dir():
            for leftover in runtime_dir.glob("*.json"):
                leftover.unlink()

    def test_rebound_seat_records_a_fresh_attempt_instead_of_throwing(self) -> None:
        self.send_untagged_mail("fq8-first")
        first = self.daemon().run_cycle(100.0)
        self.assertEqual("woke", first["state"])
        rows_before = self.durable_rows()
        self.assertEqual(1, len(rows_before))
        self.assertEqual(SESSION_A, rows_before[0]["acting_session_id"])

        # The rebind: a new acting session for the same seat, same consent.
        # _read_or_initialize reinitializes the runtime, so the cycle counter
        # restarts while the consent's activation epoch and the durable wake
        # rows persist — the recorded incident shape.
        self.bind_session(SESSION_B, binding_epoch=2)
        rebound = self.daemon()
        result = rebound.run_cycle(200.0)

        self.assertEqual("woke", result["state"])
        rows_after = self.durable_rows()
        self.assertEqual(2, len(rows_after))
        self.assertEqual(SESSION_A, rows_after[0]["acting_session_id"])
        self.assertEqual(SESSION_B, rows_after[1]["acting_session_id"])
        # The decision replayed identically; only the session moved, so the
        # fresh attempt carries the same item_ids under a DIFFERENT key.
        self.assertEqual(rows_after[0]["item_ids"], rows_after[1]["item_ids"])
        self.assertEqual(
            rows_after[0]["decision_receipt_id"], rows_after[1]["decision_receipt_id"]
        )
        self.assertNotEqual(
            rows_after[0]["idempotency_key"], rows_after[1]["idempotency_key"]
        )
        # The previous session's durable row is untouched evidence.
        self.assertEqual(rows_before[0], rows_after[0])

    def test_same_session_restart_replays_the_existing_row(self) -> None:
        self.send_untagged_mail("fq8-replay")
        first = self.daemon().run_cycle(100.0)
        self.assertEqual("woke", first["state"])
        rows_before = self.durable_rows()
        self.assertEqual(1, len(rows_before))

        # Daemon restart with a lost runtime, same seat, SAME acting session.
        self.drop_runtime()
        replayed = self.daemon().run_cycle(200.0)
        self.assertEqual("woke", replayed["state"])
        rows_after = self.durable_rows()
        self.assertEqual(1, len(rows_after))
        self.assertEqual(rows_before[0], rows_after[0])

    def test_genuinely_different_replay_still_throws(self) -> None:
        self.send_untagged_mail("fq8-different")
        daemon = self.daemon()
        self.assertEqual("woke", daemon.run_cycle(100.0)["state"])
        rows = self.durable_rows()
        self.assertEqual(1, len(rows))

        # Reconstruct the runtime view whose attempt key is the durable row's
        # key (wake key, attempt number, session digest), then replay with
        # genuinely different content under that exact slot.
        runtime = daemon.read_runtime()
        durable_key = rows[0]["idempotency_key"]
        wake_key, _, tail = durable_key.rpartition("-attempt-")
        attempt_no, _, session_digest = tail.partition("-")
        runtime["current_wake_key"] = wake_key
        runtime["cycle_index"] = int(attempt_no) - 1
        runtime["session_digest"] = session_digest or None
        binding = daemon._exact_binding()
        with self.assertRaises(Exception) as caught:
            daemon._existing_attempt(
                runtime,
                binding,
                ["msg-different-than-durable"],
                {"id": "wake-hold-different"},
                None,
            )
        self.assertIn("wake attempt replay differs", str(caught.exception))


if __name__ == "__main__":
    unittest.main()

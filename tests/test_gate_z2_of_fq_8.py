"""GATE of the FQ-8 row, at gated tip 2aa58f7c - clean room.

The deciding questions, constructed by the gate from the BL-4 repro contract
(fq-8-repro-2026-09-09.md): a seat rebind (new acting session, consent epoch
persisting) reinitializes the runtime, so an UNSCOPED attempt key recomputes
the previous session's slot and the daemon throws `wake attempt replay
differs from its durable row` at legitimate work.

G1  the incident: session A wakes a cycle, the seat rebinds to session B,
    a fresh daemon runs the same cycle - the gate requires `woke`, TWO
    durable rows (A then B) with identical item_ids and decision id but
    DIFFERENT idempotency keys, and row A byte-untouched;
G2  control: a same-session restart replays the existing row - one row,
    byte-identical;
G3  control: a genuinely differing replay under the exact reconstructed
    slot still throws - the comparison is not loosened.
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

SESSION_A = "gate-session-alpha"
SESSION_B = "gate-session-beta"


class _Adapter:
    def __init__(self, root: FloatiRoot, coordinate: DaemonCoordinate) -> None:
        self.root = root
        self.coordinate = coordinate

    def exact_binding(self) -> AdapterBinding:
        return AdapterBinding.from_record(
            AdapterBindingStore(self.root).read(self.coordinate)
        )

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


class _Tide:
    def dispatch_held(self, node_id: str) -> bool:
        return False

    def evaluate(self, node_id: str, binding: object) -> dict:
        return {"state": "off", "node_id": node_id, "receipt": None}


class GateWorld:
    def __init__(self, testcase: unittest.TestCase) -> None:
        self.temporary = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
        testcase.addCleanup(self.temporary.cleanup)
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
        self.bind(SESSION_A)
        DaemonConsentLedger(self.root).consent(
            self.coordinate,
            adapter_version="1",
            adapter_digest=adapter_contract_digest("cursor"),
            min_poll_seconds=1,
            max_poll_seconds=4,
            max_backoff_seconds=8,
            activation_epoch=1,
            idempotency_key="gate-fq8-consent",
        )

    def bind(self, session_id: str, *, epoch: int = 1) -> None:
        AdapterBindingStore(self.root).write(
            self.coordinate,
            session_id=session_id,
            workspace=self.workspace,
            executable=self.executable,
            adapter_version="1",
            adapter_digest=adapter_contract_digest("cursor"),
            binding_epoch=epoch,
        )

    def daemon(self):
        from floati.wake_daemon import WakeDaemon

        return WakeDaemon(self.coordinate, _Adapter(self.root, self.coordinate), tide_evaluator=_Tide())

    def send_mail(self, key: str) -> None:
        EventLog(self.root).send(
            "sender", public_ids.builder("a"), "floati", "a" * 40,
            "docs/evidence/gate-z2-of-fq-8.md", "gate mail",
            worker_session_id=None, idempotency_key=key,
        )

    def rows(self) -> list[dict]:
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


class GateRebindRecordsAFreshAttempt(unittest.TestCase):
    def test_g1_rebind_wakes_without_throwing_and_row_a_is_untouched(
        self,
    ) -> None:
        world = GateWorld(self)
        world.send_mail("gate-fq8-mail")
        first = world.daemon().run_cycle(100.0)
        self.assertEqual("woke", first["state"], first)
        before = world.rows()
        self.assertEqual(1, len(before))
        self.assertEqual(SESSION_A, before[0]["acting_session_id"])

        world.bind(SESSION_B, epoch=2)
        result = world.daemon().run_cycle(200.0)

        self.assertEqual(
            "woke", result["state"],
            f"the rebound cycle must wake, not throw or refuse: {result}",
        )
        self.assertNotEqual(
            "wake attempt replay differs",
            result.get("reason_code"),
            result,
        )
        after = world.rows()
        self.assertEqual(2, len(after), after)
        self.assertEqual(SESSION_A, after[0]["acting_session_id"], after)
        self.assertEqual(SESSION_B, after[1]["acting_session_id"], after)
        self.assertEqual(after[0]["item_ids"], after[1]["item_ids"])
        self.assertEqual(
            after[0]["decision_receipt_id"], after[1]["decision_receipt_id"]
        )
        self.assertNotEqual(
            after[0]["idempotency_key"], after[1]["idempotency_key"],
            "the fresh attempt must live under its own session-scoped slot",
        )
        self.assertEqual(before[0], after[0], "row A must be byte-untouched")


class GateSameSessionReplayControl(unittest.TestCase):
    def test_g2_same_session_restart_replays_the_existing_row(self) -> None:
        world = GateWorld(self)
        world.send_mail("gate-fq8-replay")
        first = world.daemon().run_cycle(100.0)
        self.assertEqual("woke", first["state"])
        before = world.rows()
        self.assertEqual(1, len(before))

        world.drop_runtime()
        replayed = world.daemon().run_cycle(200.0)
        self.assertEqual("woke", replayed["state"])
        after = world.rows()
        self.assertEqual(1, len(after))
        self.assertEqual(before[0], after[0])


class GateGenuinelyDifferingReplayStillThrows(unittest.TestCase):
    def test_g3_differing_replay_under_the_exact_slot_still_refuses(
        self,
    ) -> None:
        world = GateWorld(self)
        world.send_mail("gate-fq8-different")
        daemon = world.daemon()
        self.assertEqual("woke", daemon.run_cycle(100.0)["state"])
        rows = world.rows()
        self.assertEqual(1, len(rows))

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
                ["msg-genuinely-different"],
                {"id": "wake-hold-genuinely-different"},
                None,
            )
        self.assertIn("wake attempt replay differs", str(caught.exception))


if __name__ == "__main__":
    unittest.main()

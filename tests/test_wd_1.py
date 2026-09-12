"""WD-1: P1 cursor cost, P2 paused backoff, P3 measured CPU.

RED-first on the pre-fix tree. Properties are named; line numbers are not.
"""

from __future__ import annotations

from floati import fixture_ids as public_ids

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from floati.events import EventLog
from floati.registry import Registry
from floati.root import FloatiRoot
from floati.wake_control import WakeController
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
from floati.wake_hold import WakeHoldController
from tests.temp_roots import REAL_TEMP_ROOT


class _Adapter:
    def __init__(self, root: FloatiRoot, coordinate: DaemonCoordinate) -> None:
        self.store = AdapterBindingStore(root)
        self.coordinate = coordinate
        self.calls: list[tuple[str, str, int, object]] = []
        self.outcome = "woke"
        self.reason_code: str | None = None

    def exact_binding(self) -> AdapterBinding:
        return AdapterBinding.from_record(self.store.read(self.coordinate))

    def observe_session(self, binding: AdapterBinding) -> str:
        return "unknown"

    def request_wake(
        self,
        binding: AdapterBinding,
        reason: str,
        deadline_seconds: int,
        envelopes: object = None,
    ) -> WakeAdapterResult:
        self.calls.append((binding.session_id, reason, deadline_seconds, envelopes))
        reason_code = None if self.outcome in {"woke", "queued"} else self.reason_code
        return WakeAdapterResult(self.outcome, reason_code, 0, "e" * 64)


class _TideEvaluator:
    def dispatch_held(self, node_id: str) -> bool:
        return False

    def evaluate(self, node_id: str, binding: object) -> dict:
        return {"state": "off", "node_id": node_id, "receipt": None}


class Wd1Fixture(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.root = FloatiRoot.open_direct_home(self.base / "fleet-alpha", create=True)
        registry = Registry(self.root)
        registry.register("sender", "architect")
        registry.register(public_ids.builder("a"), "worker")
        registry.register("other", "worker")
        self.workspace = self.base / "workspace"
        self.workspace.mkdir()
        self.executable = self.base / "cursor-agent"
        self.executable.write_bytes(b"#!/bin/sh\nexit 0\n")
        self.executable.chmod(0o700)
        self.coordinate = DaemonCoordinate(self.root, public_ids.builder("a"), "cursor")
        self.adapter = _Adapter(self.root, self.coordinate)
        AdapterBindingStore(self.root).write(
            self.coordinate,
            session_id="cursor-session-1",
            workspace=self.workspace,
            executable=self.executable,
            adapter_version="1",
            adapter_digest=adapter_contract_digest("cursor"),
            binding_epoch=1,
        )
        DaemonConsentLedger(self.root).consent(
            self.coordinate,
            adapter_version="1",
            adapter_digest=adapter_contract_digest("cursor"),
            min_poll_seconds=1,
            max_poll_seconds=4,
            max_backoff_seconds=8,
            activation_epoch=1,
            idempotency_key="daemon-consent",
        )

    def daemon(self):
        from floati.wake_daemon import WakeDaemon

        return WakeDaemon(
            self.coordinate, self.adapter, tide_evaluator=_TideEvaluator()
        )

    def send_to(self, recipient: str, key: str) -> dict:
        return EventLog(self.root).send(
            "sender",
            recipient,
            "floati",
            "a" * 40,
            "docs/evidence/wd-1.md",
            "wd-1 evidence",
            worker_session_id="cursor-session-1" if recipient == public_ids.builder("a") else None,
            idempotency_key=key,
        )


class PausedBackoffTests(Wd1Fixture):
    def test_five_paused_cycles_back_off_away_from_the_floor(self) -> None:
        """P2: after five consecutive paused cycles, backoff is not min_poll."""

        WakeController(self.root).pause(
            public_ids.builder("a"),
            "cursor-session-1",
            idempotency_key="pause-wd1",
        )
        daemon = self.daemon()
        now = 100.0
        for _ in range(5):
            result = daemon.run_cycle(now)
            self.assertEqual("paused", result["state"])
            now = float(daemon.read_runtime()["next_poll_at"])
        runtime = daemon.read_runtime()
        self.assertGreater(int(runtime["current_backoff"]), 1)

    def test_woke_and_queued_still_reset_backoff_to_the_floor(self) -> None:
        daemon = self.daemon()
        self.assertEqual("idle", daemon.run_cycle(100.0)["state"])
        self.send_to(public_ids.builder("a"), "woke-floor")
        woke = daemon.run_cycle(104.0)
        self.assertEqual("woke", woke["state"])
        self.assertEqual(1, int(daemon.read_runtime()["current_backoff"]))
        self.adapter.outcome = "queued"
        self.send_to(public_ids.builder("a"), "queued-floor")
        queued = daemon.run_cycle(106.0)
        self.assertEqual("queued", queued["state"])
        self.assertEqual(1, int(daemon.read_runtime()["current_backoff"]))


class CursorCostTests(Wd1Fixture):
    def _second_cycle_full_replay_bytes(self, n_records: int) -> int:
        from floati.jsonl import VerifiedLedgerCursor

        runtime_dir = self.root.resolve_relative(Path("state/wake-daemon/runtime"))
        if runtime_dir.is_dir():
            for leftover in runtime_dir.glob("*.json"):
                leftover.unlink()
        for index in range(n_records):
            self.send_to("other", f"pad-{n_records}-{index}")
        daemon = self.daemon()
        sizes: list[int] = []
        original = VerifiedLedgerCursor._full_replay

        def wrapped(cursor, path, *args, **kwargs):
            result = original(cursor, path, *args, **kwargs)
            if path.name == "events.jsonl":
                sizes.append(path.stat().st_size)
            return result

        with mock.patch.object(VerifiedLedgerCursor, "_full_replay", wrapped):
            self.assertEqual("idle", daemon.run_cycle(100.0)["state"])
            first_reads = len(sizes)
            self.assertGreater(first_reads, 0)
            daemon.run_cycle(float(daemon.read_runtime()["next_poll_at"]))
            second_reads = sizes[first_reads:]
        return sum(second_reads)

    def test_second_cycle_ledger_read_does_not_grow_with_record_count(self) -> None:
        """P1: the second cycle's full-replay of events.jsonl is independent of N."""

        small = self._second_cycle_full_replay_bytes(8)
        large = self._second_cycle_full_replay_bytes(64)
        self.assertEqual(small, large)

    def test_hold_controller_survives_across_cycles_on_one_daemon(self) -> None:
        daemon = self.daemon()
        self.assertEqual("idle", daemon.run_cycle(100.0)["state"])
        first = getattr(daemon, "_hold_controller", None)
        self.assertIsInstance(first, WakeHoldController)
        daemon.run_cycle(float(daemon.read_runtime()["next_poll_at"]))
        self.assertIs(first, daemon._hold_controller)


class CpuBudgetTests(Wd1Fixture):
    def test_runtime_records_cpu_seconds_from_getrusage_deltas(self) -> None:
        daemon = self.daemon()
        self.assertEqual("idle", daemon.run_cycle(100.0)["state"])
        runtime = daemon.read_runtime()
        self.assertIn("cpu_seconds_last_cycle", runtime)
        self.assertIn("cpu_seconds_total", runtime)
        self.assertGreaterEqual(float(runtime["cpu_seconds_last_cycle"]), 0.0)
        self.assertGreaterEqual(float(runtime["cpu_seconds_total"]), 0.0)

    def test_over_budget_cycle_backs_off_to_the_poll_ceiling(self) -> None:
        daemon = self.daemon()
        original = daemon.consent.require_active

        def with_budget(coordinate):
            row = dict(original(coordinate))
            row["max_cpu_seconds_per_cycle"] = 0.0001
            return row

        clock = iter([0.0, 1.0, 1.0, 2.0])

        def fake_cpu() -> float:
            return next(clock)

        with mock.patch.object(daemon.consent, "require_active", with_budget):
            with mock.patch("floati.wake_daemon._cpu_seconds", fake_cpu, create=True):
                result = daemon.run_cycle(100.0)
        self.assertEqual("wake_daemon_cycle_over_budget", result["reason_code"])
        runtime = daemon.read_runtime()
        self.assertEqual(4, int(runtime["current_backoff"]))


class DoctorCopyTests(Wd1Fixture):
    def _write_runtime(self, **fields: object) -> None:
        payload = {
            "schema_version": 0,
            "tenant_id": self.root.tenant_id,
            "node_id": self.coordinate.node_id,
            "harness": "cursor",
            "coordinate_digest": self.coordinate.digest,
            "daemon_instance_id": "daemon-test",
            "activation_epoch": 1,
            "cycle_index": 3,
            "current_wake_key": None,
            "consecutive_refusals": 0,
            "circuit_state": "closed",
            "next_poll_at": 0,
            "current_backoff": 5,
            "wake_timestamps": [],
            "session_digest": hashlib.sha256(b"cursor-session-1").hexdigest(),
            "last_state": "idle",
            "last_reason_code": None,
            "last_lifecycle_receipt_id": "receipt-test",
        }
        payload.update(fields)
        path = self.root.resolve_relative(
            Path("state/wake-daemon/runtime") / f"{self.coordinate.digest}.json"
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload) + "\n", encoding="utf-8")

    def _health_details(self) -> list[str]:
        from floati.doctor import project_wake_daemon_health

        return [
            str(row["detail"])
            for row in project_wake_daemon_health(self.root, currency_current=True)
            if row["code"] == "wake_daemon_health"
        ]

    def test_unqualified_runtime_detail_is_byte_identical_to_today(self) -> None:
        self._write_runtime()
        details = "\n".join(self._health_details())
        self.assertIn("circuit_state=closed consecutive_refusals=0 current_backoff=5", details)
        node = public_ids.builder("a")
        self.assertNotIn(f"wake daemon {node}:", details)
        self.assertNotIn("not enough cycles to measure", details)
        self.assertNotIn("is over its CPU budget", details)
        self.assertNotIn("is paused and polls", details)

    def test_fewer_than_ten_cycles_render_not_enough_to_measure(self) -> None:
        self._write_runtime(
            cycle_index=3,
            cpu_seconds_last_cycle=0.01,
            cpu_seconds_total=0.03,
            cpu_seconds_window=[0.01, 0.01, 0.01],
            cpu_wall_window=[1.0, 1.0, 1.0],
        )
        details = "\n".join(self._health_details())
        self.assertIn("not enough cycles to measure", details)

    def test_ten_cycles_render_the_authored_fact_line(self) -> None:
        window = [0.10] * 10
        walls = [2.0] * 10
        self._write_runtime(
            cycle_index=10,
            cpu_seconds_last_cycle=0.10,
            cpu_seconds_total=1.0,
            cpu_seconds_window=window,
            cpu_wall_window=walls,
        )
        details = "\n".join(self._health_details())
        node = public_ids.builder("a")
        self.assertIn(
            f"wake daemon {node}: 0.10 s CPU per cycle, 180 s per hour, measured over the last 10 cycles.",
            details,
        )
        self.assertNotIn("is over its CPU budget", details)

    def test_over_budget_runtime_renders_the_authored_degraded_line(self) -> None:
        self._write_runtime(
            cycle_index=10,
            current_backoff=4,
            last_reason_code="wake_daemon_cycle_over_budget",
            cpu_seconds_last_cycle=0.40,
            cpu_seconds_total=4.0,
            cpu_seconds_window=[0.40] * 10,
            cpu_wall_window=[4.0] * 10,
        )
        details = "\n".join(self._health_details())
        node = public_ids.builder("a")
        self.assertIn(
            f"wake daemon {node} is over its CPU budget and has backed off to 4 s between polls.",
            details,
        )

    def test_paused_runtime_renders_the_authored_paused_line(self) -> None:
        self._write_runtime(last_state="paused", current_backoff=4)
        details = "\n".join(self._health_details())
        node = public_ids.builder("a")
        self.assertIn(f"wake daemon {node} is paused and polls every 4 s.", details)


if __name__ == "__main__":
    unittest.main()

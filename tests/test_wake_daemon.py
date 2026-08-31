from __future__ import annotations

from floati import fixture_ids as public_ids

import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from floati.errors import ProtocolRefusal
from floati.events import EventLog
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


class _Adapter:
    def __init__(self, root: FloatiRoot, coordinate: DaemonCoordinate) -> None:
        self.store = AdapterBindingStore(root)
        self.coordinate = coordinate
        self.calls: list[tuple[str, str, int]] = []
        self.outcome = "woke"

    def exact_binding(self) -> AdapterBinding:
        return AdapterBinding.from_record(self.store.read(self.coordinate))

    def observe_session(self, binding: AdapterBinding) -> str:
        return "unknown"

    def request_wake(
        self, binding: AdapterBinding, reason: str, deadline_seconds: int
    ) -> WakeAdapterResult:
        self.calls.append((binding.session_id, reason, deadline_seconds))
        reason_code = None if self.outcome == "woke" else "fake_adapter_" + self.outcome
        return WakeAdapterResult(self.outcome, reason_code, 0, "e" * 64)


class _TideEvaluator:
    def __init__(self) -> None:
        self.calls = 0
        self.held = False
        self.failure = None
        self.hold_during_evaluation = False

    def dispatch_held(self, node_id: str) -> bool:
        return self.held

    def evaluate(self, node_id: str, binding: object) -> dict:
        self.calls += 1
        if self.failure is not None:
            raise self.failure
        if self.hold_during_evaluation:
            self.held = True
        return {"state": "off", "node_id": node_id, "receipt": None}


class _WakeDaemonFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(dir="\x2fprivate/tmp")
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.root = FloatiRoot.open_direct_home(self.base / "fleet-alpha", create=True)
        registry = Registry(self.root)
        registry.register("sender", "architect")
        registry.register(public_ids.builder('a'), "worker")
        self.workspace = self.base / "workspace"
        self.workspace.mkdir()
        self.executable = self.base / "cursor-agent"
        self.executable.write_bytes(b"#!/bin/sh\nexit 0\n")
        self.executable.chmod(0o700)
        self.coordinate = DaemonCoordinate(self.root, public_ids.builder('a'), "cursor")
        self.adapter = _Adapter(self.root, self.coordinate)

    def bind(self, session_id: str = "cursor-session-1") -> None:
        AdapterBindingStore(self.root).write(
            self.coordinate,
            session_id=session_id,
            workspace=self.workspace,
            executable=self.executable,
            adapter_version="1",
            adapter_digest=adapter_contract_digest("cursor"),
            binding_epoch=1,
        )

    def consent(self, *, key: str = "daemon-consent") -> None:
        DaemonConsentLedger(self.root).consent(
            self.coordinate,
            adapter_version="1",
            adapter_digest=adapter_contract_digest("cursor"),
            min_poll_seconds=1,
            max_poll_seconds=4,
            max_backoff_seconds=8,
            activation_epoch=1,
            idempotency_key=key,
        )

    def daemon(self):
        from floati.wake_daemon import WakeDaemon

        return WakeDaemon(self.coordinate, self.adapter)

    def send(self, key: str = "daemon-message") -> dict:
        return EventLog(self.root).send(
            "sender",
            public_ids.builder('a'),
            "floati",
            "a" * 40,
            "docs/evidence/wake-daemon.md",
            "wake daemon evidence",
            worker_session_id="cursor-session-1",
            idempotency_key=key,
        )

    def send_unbound(self, key: str = "daemon-unbound-message") -> dict:
        return EventLog(self.root).send(
            "sender",
            public_ids.builder('a'),
            "floati",
            "a" * 40,
            "docs/evidence/wake-daemon.md",
            "wake daemon evidence",
            idempotency_key=key,
        )


class WakeDaemonRedTests(_WakeDaemonFixture):

    def test_no_consent_means_no_runtime_or_owner_files(self) -> None:
        daemon = self.daemon()
        with self.assertRaisesRegex(ProtocolRefusal, "consent_absent"):
            daemon.serve(lambda: True)
        self.assertFalse(daemon.runtime_path.exists())
        self.assertFalse(daemon.owner.path.exists())
        self.assertEqual([], self.adapter.calls)

    def test_tide_evaluation_runs_once_only_on_a_due_consented_cycle(self) -> None:
        from floati.wake_daemon import WakeDaemon

        self.bind()
        self.consent()
        tide = _TideEvaluator()
        daemon = WakeDaemon(self.coordinate, self.adapter, tide_evaluator=tide)
        self.assertEqual("idle", daemon.run_cycle(100.0)["state"])
        self.assertEqual(1, tide.calls)
        self.assertEqual("backpressure", daemon.run_cycle(101.0)["state"])
        self.assertEqual(1, tide.calls)

    def test_existing_direct_tide_hold_blocks_new_dispatch_evaluation(self) -> None:
        from floati.wake_daemon import WakeDaemon

        self.bind()
        self.consent()
        self.send()
        tide = _TideEvaluator()
        tide.held = True
        daemon = WakeDaemon(self.coordinate, self.adapter, tide_evaluator=tide)
        result = daemon.run_cycle(100.0)
        self.assertEqual("held", result["state"])
        self.assertEqual("tide_directive_hold", result["reason_code"])
        self.assertEqual(1, tide.calls)
        self.assertEqual([], self.adapter.calls)

    def test_direct_tide_crossing_holds_dispatch_in_the_same_cycle(self) -> None:
        from floati.wake_daemon import WakeDaemon

        self.bind()
        self.consent()
        self.send()
        tide = _TideEvaluator()
        tide.hold_during_evaluation = True
        daemon = WakeDaemon(self.coordinate, self.adapter, tide_evaluator=tide)

        result = daemon.run_cycle(100.0)

        self.assertEqual("held", result["state"])
        self.assertEqual("tide_directive_hold", result["reason_code"])
        self.assertEqual(1, tide.calls)
        self.assertEqual([], self.adapter.calls)

    def test_tide_refusal_backs_off_without_killing_the_daemon_loop(self) -> None:
        from floati.wake_daemon import WakeDaemon

        self.bind()
        self.consent()
        tide = _TideEvaluator()
        tide.failure = ProtocolRefusal("tide_reading_unavailable", "no exact reading")
        result = WakeDaemon(
            self.coordinate, self.adapter, tide_evaluator=tide
        ).run_cycle(100.0)
        self.assertEqual("refused", result["state"])
        self.assertEqual("tide_reading_unavailable", result["reason_code"])
        self.assertEqual([], self.adapter.calls)

    def test_daemon_never_discovers_or_opens_a_foreign_root(self) -> None:
        self.bind()
        self.consent()
        foreign = self.base / "foreign-root"
        foreign.mkdir()
        sentinel = foreign / "sentinel"
        sentinel.write_text("untouched\n", encoding="utf-8")
        with mock.patch.object(
            FloatiRoot, "open_direct_home", side_effect=AssertionError("root discovery")
        ):
            result = self.daemon().run_cycle(100.0)
        self.assertEqual("idle", result["state"])
        self.assertEqual("untouched\n", sentinel.read_text(encoding="utf-8"))

    def test_two_instances_cannot_own_one_coordinate(self) -> None:
        from floati.wake_daemon import DaemonOwner

        first = DaemonOwner(self.coordinate)
        second = DaemonOwner(self.coordinate)
        first.acquire()
        self.addCleanup(first.release)
        with self.assertRaisesRegex(ProtocolRefusal, "owner_unknown"):
            second.acquire()

    def test_global_or_wildcard_pause_cannot_be_expressed(self) -> None:
        from floati.wake_control import WakeController

        for session in ("all", "global", "*"):
            with self.subTest(session=session):
                with self.assertRaisesRegex(ProtocolRefusal, "exact"):
                    WakeController(self.root).pause(
                        public_ids.builder('a'), session, idempotency_key="forbidden-" + session
                    )

    def test_malformed_pause_marker_blocks_the_adapter(self) -> None:
        self.bind()
        self.consent()
        digest = hashlib.sha256(b"cursor-session-1").hexdigest()
        marker = self.root.resolve_relative(
            Path(public_ids.compose('state/wake-control/', public_ids.builder('a'))) / f"{digest}.json"
        )
        marker.parent.mkdir(parents=True)
        marker.write_bytes(b"{not-json\n")

        result = self.daemon().run_cycle(100.0)

        self.assertEqual("pause_unknown", result["state"])
        self.assertEqual([], self.adapter.calls)

    def test_adapter_success_without_durable_attempt_is_unknown(self) -> None:
        self.bind()
        self.consent()
        self.send()
        with mock.patch(
            "floati.wake_daemon.WakeAttemptLedger.record",
            side_effect=OSError("injected append failure"),
        ):
            result = self.daemon().run_cycle(100.0)

        self.assertEqual("wake_evidence_unknown", result["state"])
        self.assertEqual(1, len(self.adapter.calls))
        self.assertFalse(
            self.root.resolve_relative(public_ids.compose('receipts/wakes/', public_ids.ledger(public_ids.builder('a')))).exists()
        )


class WakeDaemonGreenTests(_WakeDaemonFixture):
    def setUp(self) -> None:
        super().setUp()
        self.bind()
        self.consent()

    def test_idle_fresh_woke_and_held_transitions_are_deterministic(self) -> None:
        daemon = self.daemon()
        self.assertEqual("idle", daemon.run_cycle(100.0)["state"])
        message = self.send("fresh-message")
        woke = daemon.run_cycle(102.0)
        self.assertEqual("woke", woke["state"])
        self.assertIn(message["id"], self.adapter.calls[0][1])
        self.assertEqual("held", daemon.run_cycle(104.0)["state"])

    def test_lane_level_envelope_wakes_the_exact_bound_session(self) -> None:
        message = self.send_unbound()

        result = self.daemon().run_cycle(100.0)

        self.assertEqual("woke", result["state"])
        self.assertIn(message["id"], self.adapter.calls[0][1])
        wake_rows = self.root.resolve_relative(public_ids.compose('receipts/wakes/', public_ids.ledger(public_ids.builder('a')))).read_text(
            encoding="utf-8"
        )
        self.assertIn('"message_worker_session_id":null', wake_rows)

    def test_pause_and_resume_are_exact_session_scoped(self) -> None:
        from floati.wake_control import WakeController

        self.send("pause-message")
        control = WakeController(self.root)
        control.pause(public_ids.builder('a'), "cursor-session-1", idempotency_key="pause-exact")
        daemon = self.daemon()
        self.assertEqual("paused", daemon.run_cycle(100.0)["state"])
        self.assertEqual([], self.adapter.calls)
        control.resume(public_ids.builder('a'), "cursor-session-1", idempotency_key="resume-exact")
        self.assertEqual("woke", daemon.run_cycle(102.0)["state"])

    def test_unknowns_back_off_and_trip_the_circuit_without_acknowledging(self) -> None:
        from floati.cursor import SparseCursor

        self.send("breaker-message")
        self.adapter.outcome = "unknown"
        daemon = self.daemon()
        states = [daemon.run_cycle(now)["state"] for now in (100.0, 102.0, 106.0)]
        self.assertEqual(["adapter_unknown"] * 3, states)
        runtime = daemon.read_runtime()
        self.assertEqual("open", runtime["circuit_state"])
        self.assertEqual(frozenset(), SparseCursor(self.root).acked_ids(public_ids.builder('a'), worker_session_id="cursor-session-1"))

    def test_three_wakes_exhaust_the_rolling_budget_without_a_fourth_call(self) -> None:
        daemon = self.daemon()
        for index, now in enumerate((100.0, 102.0, 104.0), start=1):
            self.send(f"budget-{index}")
            self.assertEqual("woke", daemon.run_cycle(now)["state"])
        self.send("budget-4")
        self.assertEqual("exhausted", daemon.run_cycle(106.0)["state"])
        self.assertEqual(3, len(self.adapter.calls))

    def test_revocation_refuses_the_next_cycle_without_adapter_work(self) -> None:
        ledger = DaemonConsentLedger(self.root)
        ledger.revoke(self.coordinate, idempotency_key="daemon-revoke")
        with self.assertRaisesRegex(ProtocolRefusal, "consent_absent"):
            self.daemon().run_cycle(100.0)
        self.assertEqual([], self.adapter.calls)

    def test_restart_after_attempt_commit_does_not_invoke_the_adapter_twice(self) -> None:
        self.send("restart-message")
        daemon = self.daemon()
        real_write = daemon._write_runtime
        writes = [0]

        def fail_final_write(runtime: dict) -> None:
            writes[0] += 1
            if writes[0] == 3:
                raise ProtocolRefusal(
                    "wake_daemon_runtime_unavailable", "injected final write failure"
                )
            real_write(runtime)

        with mock.patch.object(daemon, "_write_runtime", side_effect=fail_final_write):
            with self.assertRaisesRegex(ProtocolRefusal, "runtime_unavailable"):
                daemon.run_cycle(100.0)
        self.assertEqual(1, len(self.adapter.calls))

        restarted = self.daemon()
        self.assertEqual("woke", restarted.run_cycle(100.0)["state"])
        self.assertEqual(1, len(self.adapter.calls))

    def test_restart_after_request_before_evidence_is_unknown_without_duplicate_wake(self) -> None:
        self.send("pending-evidence-message")
        daemon = self.daemon()
        with mock.patch(
            "floati.wake_daemon.WakeAttemptLedger.record",
            side_effect=KeyboardInterrupt("injected process death"),
        ):
            with self.assertRaises(KeyboardInterrupt):
                daemon.run_cycle(100.0)
        self.assertEqual(1, len(self.adapter.calls))

        restarted = self.daemon()
        self.assertEqual(
            "wake_evidence_unknown", restarted.run_cycle(100.0)["state"]
        )
        self.assertEqual(1, len(self.adapter.calls))

    def test_graceful_serve_releases_the_kernel_owner(self) -> None:
        from floati.wake_daemon import DaemonOwner

        stops = iter((False, True))
        self.daemon().serve(lambda: next(stops), clock=lambda: 100.0)
        replacement = DaemonOwner(self.coordinate)
        replacement.acquire()
        replacement.release()


if __name__ == "__main__":
    unittest.main()

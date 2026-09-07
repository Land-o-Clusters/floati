"""SKEW-2 Am.1: wake hold and wake daemon survive unknown event kinds."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from floati import fixture_ids as public_ids
from floati.events import EventLog
from floati.framing import encode_frame
from floati.registry import Registry
from floati.root import FloatiRoot
from floati.wake_daemon_adapters import adapter_contract_digest
from floati.wake_daemon_contract import (
    AdapterBindingStore,
    DaemonConsentLedger,
    DaemonCoordinate,
)
from floati.wake_hold import WakeHoldController
from tests.temp_roots import REAL_TEMP_ROOT


FUTURE_EVENT_KIND = "future_event_kind_v99"
FUTURE_EVENT_ID = "msg-future-kind-000000000000000000000001"


class _WakeDaemonAdapter:
    def __init__(self, root: FloatiRoot, coordinate: DaemonCoordinate) -> None:
        self.coordinate = coordinate
        self.store = AdapterBindingStore(root)

    def exact_binding(self):
        from floati.wake_daemon_adapters import AdapterBinding

        return AdapterBinding.from_record(self.store.read(self.coordinate))

    def observe_session(self, binding: object) -> str:
        return "unknown"

    def request_wake(
        self,
        binding: object,
        reason: str,
        deadline_seconds: int,
        envelopes: object = None,
    ):
        from floati.wake_daemon_adapters import WakeAdapterResult

        return WakeAdapterResult("woke", None, 0, "e" * 64)


class Skew2Am1WakeSurvivalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.root = FloatiRoot.open_direct_home(self.base / "fleet", create=True)
        Registry(self.root).register("sender", "architect")
        Registry(self.root).register(public_ids.builder("a"), "worker")
        self.workspace = self.base / "workspace"
        self.workspace.mkdir()
        self.executable = self.base / "cursor-agent"
        self.executable.write_bytes(b"#!/bin/sh\nexit 0\n")
        self.executable.chmod(0o700)
        self.coordinate = DaemonCoordinate(self.root, public_ids.builder("a"), "cursor")
        self.adapter = _WakeDaemonAdapter(self.root, self.coordinate)
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
            idempotency_key="skew2-am1-consent",
        )
        self.log = EventLog(self.root)
        self.known = self.log.send(
            "sender",
            public_ids.builder("a"),
            "floati",
            "a" * 40,
            "docs/evidence/skew-2-am1.md",
            "skew2 am1 known",
            worker_session_id="cursor-session-1",
            idempotency_key="skew2-am1-known",
        )

    def append_unknown_event(self) -> None:
        path = self.root.resolve_relative("events.jsonl")
        with path.open("ab") as handle:
            handle.write(
                encode_frame(
                    {
                        "schema_version": 0,
                        "id": FUTURE_EVENT_ID,
                        "tenant_id": self.root.tenant_id,
                        "timestamp": "2026-09-06T01:00:00.000Z",
                        "kind": FUTURE_EVENT_KIND,
                        "sender": "sender",
                        "recipient": public_ids.builder("a"),
                        "repo": "floati",
                        "sha": "a" * 40,
                        "doc": "docs/evidence/skew-2-am1.md",
                        "note": "unknown kind probe",
                        "idempotency_key": "skew2-am1-unknown-event",
                    }
                )
            )

    def daemon(self):
        from floati.wake_daemon import WakeDaemon

        return WakeDaemon(self.coordinate, self.adapter)

    def test_wake_hold_read_skips_unknown_event_kind(self) -> None:
        """WakeHoldController._read must not raise on future event vocabulary."""

        self.append_unknown_event()
        controller = WakeHoldController(self.root)
        events, event_prefixes, deliveries, delivery_prefixes, acknowledgments, acknowledgment_prefixes = (
            controller._read(public_ids.builder("a"), "cursor-session-1")
        )
        ids = [row["id"] for row in events]
        self.assertIn(self.known["id"], ids)
        self.assertNotIn(FUTURE_EVENT_ID, ids)
        self.assertEqual(len(events) + 1, len(event_prefixes))
        self.assertEqual([], deliveries)
        self.assertEqual([], acknowledgments)
        self.assertEqual(1, len(delivery_prefixes))
        self.assertEqual(1, len(acknowledgment_prefixes))

    def test_run_cycle_returns_state_with_unknown_event_kind(self) -> None:
        """RED: one run_cycle with unknown event kind returns a state, never raises."""

        self.append_unknown_event()
        result = self.daemon().run_cycle(100.0)
        self.assertIsInstance(result, dict)
        self.assertIn("state", result)
        self.assertEqual("woke", result["state"])

    def test_serve_records_cycle_exception_lifecycle_receipt_and_continues(self) -> None:
        """Defense in depth: serve() records cycle_exception and keeps polling."""

        from floati.errors import IntegrityFailure
        from floati.jsonl import read_records

        daemon = self.daemon()
        calls = {"count": 0}

        def fake_run_cycle(now: float) -> dict:
            calls["count"] += 1
            if calls["count"] == 1:
                raise IntegrityFailure(
                    "consumption_state_unavailable", "synthetic cycle fault"
                )
            return {"next_poll_at": now + 1.0, "state": "idle"}

        sleeps: list[float] = []
        with mock.patch.object(daemon, "run_cycle", side_effect=fake_run_cycle):
            daemon.serve(
                lambda: calls["count"] >= 2,
                clock=lambda: 100.0,
                sleep=lambda delay: sleeps.append(delay),
            )
        self.assertEqual(2, calls["count"])
        self.assertEqual(1, len(sleeps))
        self.assertEqual(2.0, sleeps[0])
        lifecycle = read_records(
            self.root,
            DaemonConsentLedger._relative(self.coordinate.node_id),
            allowed_kinds={
                "wake_daemon_lifecycle_receipt",
                "wake_daemon_consent_receipt",
            },
        )
        lifecycle_rows = [
            row for row in lifecycle if row.get("kind") == "wake_daemon_lifecycle_receipt"
        ]
        self.assertEqual("cycle_exception", lifecycle_rows[-1]["event"])
        self.assertEqual("unknown", lifecycle_rows[-1]["state"])
        self.assertEqual("consumption_state_unavailable", lifecycle_rows[-1]["reason_code"])


if __name__ == "__main__":
    unittest.main()

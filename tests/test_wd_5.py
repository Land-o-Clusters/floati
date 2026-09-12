"""WD-5: doctor names a dormant seat holding undelivered mail.

RED-first on a throwaway node under a scratch root. Copy is from the ruling;
numbers are measured from the receipts. A live holder, or no undelivered mail,
renders nothing new — the green control is byte-identical to today.
"""

from __future__ import annotations

from floati import fixture_ids as public_ids

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path

from floati.codex_wait_liveness import classify_holder, process_start_epoch, read_holder_testimony
from floati.events import EventLog
from floati.registry import Registry
from floati.root import FloatiRoot
from floati.wake_daemon_adapters import adapter_contract_digest
from floati.wake_daemon_contract import AdapterBindingStore, DaemonCoordinate
from tests.temp_roots import REAL_TEMP_ROOT

_NODE = public_ids.builder("a")
_SESSION = "cursor-session-wd-5"
_HOLDER_TIME = "2026-09-10T21:00:00.000Z"


class Wd5Fixture(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.root = FloatiRoot.open_direct_home(self.base / "fleet-wd5", create=True)
        registry = Registry(self.root)
        registry.register("sender", "architect")
        registry.register(_NODE, "worker")
        self.workspace = self.base / "workspace"
        self.workspace.mkdir()
        self.executable = self.base / "cursor-agent"
        self.executable.write_bytes(b"#!/bin/sh\nexit 0\n")
        self.executable.chmod(0o700)
        self.coordinate = DaemonCoordinate(self.root, _NODE, "cursor")
        AdapterBindingStore(self.root).write(
            self.coordinate,
            session_id=_SESSION,
            workspace=self.workspace,
            executable=self.executable,
            adapter_version="1",
            adapter_digest=adapter_contract_digest("cursor"),
            binding_epoch=1,
        )
        self._write_runtime()

    def _write_runtime(self) -> None:
        payload = {
            "schema_version": 0,
            "tenant_id": self.root.tenant_id,
            "node_id": _NODE,
            "harness": "cursor",
            "coordinate_digest": self.coordinate.digest,
            "daemon_instance_id": "daemon-wd5",
            "activation_epoch": 1,
            "cycle_index": 3,
            "current_wake_key": None,
            "consecutive_refusals": 0,
            "circuit_state": "closed",
            "next_poll_at": 0,
            "current_backoff": 5,
            "wake_timestamps": [],
            "session_digest": hashlib.sha256(_SESSION.encode("utf-8")).hexdigest(),
            "last_state": "idle",
            "last_reason_code": None,
            "last_lifecycle_receipt_id": "receipt-wd5",
        }
        path = self.root.resolve_relative(
            Path("state/wake-daemon/runtime") / f"{self.coordinate.digest}.json"
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload) + "\n", encoding="utf-8")

    def _write_holder(
        self, *, pid: int, start_epoch: float, timestamp: str = _HOLDER_TIME
    ) -> None:
        relative = Path("state/codex-wait") / _NODE / "holder.json"
        path = self.root.resolve_relative(relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        row = {
            "schema_version": 1,
            "kind": "codex_wait_holder_testimony",
            "tenant_id": self.root.tenant_id,
            "node_id": _NODE,
            "session_id": _SESSION,
            "pid": int(pid),
            "process_start_epoch": float(start_epoch),
            "timestamp": timestamp,
        }
        path.write_text(json.dumps(row, sort_keys=True) + "\n", encoding="utf-8")

    def _send_one_undelivered(self) -> None:
        EventLog(self.root).send(
            "sender",
            _NODE,
            "floati",
            "a" * 40,
            "docs/evidence/wd-5.md",
            "wd-5 evidence",
            worker_session_id=_SESSION,
            idempotency_key="wd-5-undelivered-1",
        )

    def _health_detail(self) -> str:
        from floati.doctor import project_wake_daemon_health

        rows = [
            str(row["detail"])
            for row in project_wake_daemon_health(self.root, currency_current=True)
            if row["code"] == "wake_daemon_health" and row["subject"] == f"{_NODE}/cursor"
        ]
        self.assertEqual(1, len(rows), rows)
        return rows[0]

    def test_released_holder_with_one_undelivered_renders_the_authored_line(self) -> None:
        """Ruling §2: released + undelivered → first authored line; n measured."""

        self._send_one_undelivered()
        # A pid that cannot be alive: classify_holder → released.
        self._write_holder(pid=2_147_483_647, start_epoch=1.0)
        testimony = read_holder_testimony(self.root, _NODE)
        self.assertIsNotNone(testimony)
        self.assertEqual("released", classify_holder(testimony))

        detail = self._health_detail()
        expected = (
            f"wake daemon {_NODE}: 1 undelivered; no live seat since {_HOLDER_TIME}. "
            "Restart the seat to drain them."
        )
        self.assertIn(expected, detail)

    def test_absent_testimony_with_undelivered_renders_the_unproven_line(self) -> None:
        """Ruling §2: testimony absent → unproven typed-absence line."""

        self._send_one_undelivered()
        self.assertIsNone(read_holder_testimony(self.root, _NODE))
        self.assertEqual("unproven", classify_holder(None))

        detail = self._health_detail()
        expected = (
            f"Floati cannot tell whether {_NODE} has a live seat; "
            "1 messages are undelivered."
        )
        self.assertIn(expected, detail)
        self.assertNotIn("no live seat since", detail)

    def test_live_holder_with_undelivered_renders_nothing_new(self) -> None:
        """Ruling §2 green control: live holder → byte-identical to today."""

        self._send_one_undelivered()
        pid = os.getpid()
        start = process_start_epoch(pid)
        self.assertIsNotNone(start)
        self._write_holder(pid=pid, start_epoch=float(start))
        self.assertEqual("live", classify_holder(read_holder_testimony(self.root, _NODE)))

        detail = self._health_detail()
        self.assertNotIn("undelivered", detail)
        self.assertNotIn("no live seat since", detail)
        self.assertNotIn("Floati cannot tell whether", detail)
        self.assertIn("circuit_state=closed consecutive_refusals=0 current_backoff=5", detail)

    def test_released_holder_with_no_undelivered_renders_nothing_new(self) -> None:
        """Ruling §2 green control: no undelivered mail → nothing new."""

        self._write_holder(pid=2_147_483_647, start_epoch=1.0)
        self.assertEqual("released", classify_holder(read_holder_testimony(self.root, _NODE)))

        detail = self._health_detail()
        self.assertNotIn("undelivered", detail)
        self.assertNotIn("no live seat since", detail)
        self.assertNotIn("Floati cannot tell whether", detail)


if __name__ == "__main__":
    unittest.main()

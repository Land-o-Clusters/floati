"""GATE of WD-5, at gated tip 5ea7394c - clean room.

The deciding questions, constructed by the gate from the ruling:

G1  holder testimony RELEASED + one undelivered envelope renders EXACTLY
    "wake daemon <node>: 1 undelivered; no live seat since <time>. Restart
    the seat to drain them." - n and time measured from the receipts.
G2  testimony ABSENT + one undelivered envelope renders EXACTLY
    "Floati cannot tell whether <node> has a live seat; 1 messages are
    undelivered."
G3  green controls (live holder; released with zero undelivered) render
    wake-health output BYTE-IDENTICAL to the pre-fix tree - the gate runs
    the same fixture on both trees and diffs the bytes (this class only;
    write the rendered output where $GATE_WD5_OUT names it).
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from floati import fixture_ids as public_ids
from floati.events import EventLog
from floati.registry import Registry
from floati.root import FloatiRoot
from floati.wake_daemon_adapters import adapter_contract_digest
from floati.wake_daemon_contract import AdapterBindingStore, DaemonCoordinate
from tests.temp_roots import REAL_TEMP_ROOT

_NODE = public_ids.builder("a")
_SESSION = "gate-session-wd-5"
_HOLDER_TIME = "2026-09-10T21:00:00.000Z"


class _World:
    def __init__(self, testcase: unittest.TestCase, root_name: str) -> None:
        self.temporary = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
        testcase.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.root = FloatiRoot.open_direct_home(self.base / root_name, create=True)
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
            "daemon_instance_id": "daemon-gate-wd5",
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
            "last_lifecycle_receipt_id": "receipt-gate-wd5",
        }
        path = self.root.resolve_relative(
            Path("state/wake-daemon/runtime") / f"{self.coordinate.digest}.json"
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload) + "\n", encoding="utf-8")

    def write_holder(self, *, pid: int, start_epoch: float) -> None:
        path = self.root.resolve_relative(
            Path("state/codex-wait") / _NODE / "holder.json"
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "kind": "codex_wait_holder_testimony",
                    "tenant_id": self.root.tenant_id,
                    "node_id": _NODE,
                    "session_id": _SESSION,
                    "pid": int(pid),
                    "process_start_epoch": float(start_epoch),
                    "timestamp": _HOLDER_TIME,
                },
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )

    def send_one_undelivered(self) -> None:
        EventLog(self.root).send(
            "sender", _NODE, "floati", "a" * 40,
            "docs/evidence/gate-z2-of-wd-5.md", "gate evidence",
            worker_session_id=_SESSION,
            idempotency_key="gate-wd5-undelivered-1",
        )

    def wake_health_details(self) -> list[str]:
        from floati.doctor import project_wake_daemon_health

        return [
            str(row["detail"])
            for row in project_wake_daemon_health(self.root, currency_current=True)
            if row["code"] == "wake_daemon_health"
            and row["subject"] == f"{_NODE}/cursor"
        ]


class GateDormantSeatLines(unittest.TestCase):
    def test_g1_released_plus_undelivered_renders_the_exact_line(self) -> None:
        world = _World(self, "gate-wd5-g1")
        world.send_one_undelivered()
        world.write_holder(pid=2_147_483_647, start_epoch=1.0)

        details = world.wake_health_details()
        self.assertEqual(1, len(details), details)
        expected = (
            f"wake daemon {_NODE}: 1 undelivered; "
            f"no live seat since {_HOLDER_TIME}. "
            "Restart the seat to drain them."
        )
        lines = details[0].splitlines()
        self.assertIn(
            expected, lines,
            "the authored line must render EXACTLY, whole: "
            + repr(lines),
        )

    def test_g2_absent_testimony_renders_the_unproven_line(self) -> None:
        world = _World(self, "gate-wd5-g2")
        world.send_one_undelivered()

        details = world.wake_health_details()
        self.assertEqual(1, len(details), details)
        expected = (
            f"Floati cannot tell whether {_NODE} has a live seat; "
            "1 messages are undelivered."
        )
        lines = details[0].splitlines()
        self.assertIn(expected, lines, repr(lines))


class GateControlByteIdenticalToMain(unittest.TestCase):
    def _render(self, world: _World) -> str:
        return "\n".join(
            world.wake_health_details()
        ) + "\n"

    def test_g3_live_holder_control(self) -> None:
        """The holder is THIS test process, alive now, with its start
        measured by /bin/ps - the same ground truth a seat's testimony
        carries. On the gated tree the holder classifies live; on the
        pre-fix tree the whole WD-5 block does not exist."""

        import datetime

        world = _World(self, "gate-wd5-g3-live")
        world.send_one_undelivered()
        pid = os.getpid()
        listed = subprocess.run(
            ["/bin/ps", "-p", str(pid), "-o", "lstart="],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        # `ps -o lstart=` prints LOCAL wall time; a naive parse interpreted
        # in this host's timezone is the true epoch.
        started = datetime.datetime.strptime(
            listed, "%a %b %d %H:%M:%S %Y"
        ).timestamp()
        world.write_holder(pid=pid, start_epoch=started)
        out = os.environ.get("GATE_WD5_OUT")
        rendered = self._render(world)
        if out:
            Path(out).write_text(rendered, encoding="utf-8")
        self.assertNotIn("undelivered", rendered)
        self.assertNotIn("no live seat since", rendered)

    def test_g3_released_zero_undelivered_control(self) -> None:
        world = _World(self, "gate-wd5-g3-zero")
        world.write_holder(pid=2_147_483_647, start_epoch=1.0)
        out = os.environ.get("GATE_WD5_OUT")
        rendered = self._render(world)
        if out:
            Path(out + ".zero", ).write_text(rendered, encoding="utf-8")
        self.assertNotIn("undelivered", rendered)

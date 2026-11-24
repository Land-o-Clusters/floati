"""WD-4: the idle time-to-live - a daemon retires itself after
``idle_retire_days`` (consent field, default 14, 0 = never) with no wake
attempt and no mail for its node; retires the WD-2 way (one lifecycle
receipt, reused enum vocabulary, reason ``wake_daemon_idle_retired``, exit
0 and final), and doctor announces the retirement date from seven days out.
"""

from __future__ import annotations

import datetime
import json
import tempfile
import time
import unittest
from pathlib import Path

from floati import fixture_ids as public_ids
from floati.events import EventLog
from floati.jsonl import read_records_snapshot
from floati.registry import Registry
from floati.root import FloatiRoot
from floati.wake_daemon_adapters import adapter_contract_digest
from floati.wake_daemon_contract import (
    DAEMON_KINDS,
    AdapterBindingStore,
    DaemonConsentLedger,
    DaemonCoordinate,
)
from tests.temp_roots import REAL_TEMP_ROOT

_DAY = 86400.0


class IdleRetireFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.root = FloatiRoot.open_direct_home(self.base / "fleet", create=True)
        self.node = public_ids.builder("a")
        Registry(self.root).register("sender", "architect")
        Registry(self.root).register(self.node, "worker")
        self.coordinate = DaemonCoordinate(self.root, self.node, "cursor")
        self.workspace = self.base / "workspace"
        self.workspace.mkdir()
        self.executable = self.base / "cursor-agent"
        self.executable.write_bytes(b"#!/bin/sh\nexit 0\n")
        self.executable.chmod(0o700)
        AdapterBindingStore(self.root).write(
            self.coordinate,
            session_id="cursor-session-1",
            workspace=self.workspace,
            executable=self.executable,
            adapter_version="1",
            adapter_digest=adapter_contract_digest("cursor"),
            binding_epoch=1,
        )
        self.consent = DaemonConsentLedger(self.root)
        self.consent.consent(
            self.coordinate,
            adapter_version="1",
            adapter_digest=adapter_contract_digest("cursor"),
            min_poll_seconds=1,
            max_poll_seconds=30,
            max_backoff_seconds=120,
            activation_epoch=1,
            idempotency_key="wd4-consent-1",
        )

    def daemon(self):
        from floati.wake_daemon import WakeDaemon

        class _NoMailAdapter:
            def __init__(self, root, coordinate):
                self.coordinate = coordinate
                self.store = AdapterBindingStore(root)

            def exact_binding(self):
                from floati.wake_daemon_adapters import AdapterBinding

                return AdapterBinding.from_record(self.store.read(self.coordinate))

            def observe_session(self, binding):
                return "unknown"

            def request_wake(self, binding, reason, deadline_seconds, envelopes=None):
                from floati.wake_daemon_adapters import WakeAdapterResult

                return WakeAdapterResult("woke", None, 0, "e" * 64)

        return WakeDaemon(self.coordinate, _NoMailAdapter(self.root, self.coordinate))

    def runtime_path(self) -> Path:
        return self.root.resolve_relative(
            Path("state/wake-daemon/runtime") / f"{self.coordinate.digest}.json"
        )

    def runtime(self) -> dict:
        return json.loads(self.runtime_path().read_text(encoding="utf-8"))

    def write_runtime_idle(self, *, idle_seconds: float, days: int = 14) -> None:
        """A runtime that has been idle for `idle_seconds`, serving under
        `days`, as an older build's runtime plus the two WD-4 fields."""

        path = self.runtime_path()
        payload = json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
        now = time.time()
        runtime = {
            "schema_version": 0,
            "tenant_id": self.root.tenant_id,
            "node_id": self.node,
            "harness": "cursor",
            "coordinate_digest": self.coordinate.digest,
            "daemon_instance_id": "daemon-wd4",
            "activation_epoch": 1,
            "cycle_index": 5,
            "current_wake_key": None,
            "consecutive_refusals": 0,
            "circuit_state": "closed",
            "next_poll_at": now - 10.0,
            "current_backoff": 30,
            "wake_timestamps": [],
            "session_digest": None,
            "last_state": "idle",
            "last_reason_code": None,
            "last_lifecycle_receipt_id": None,
            "bus_epoch_archive": None,
            "breaker_transitions": 0,
            "awaiting_work": False,
            "cpu_seconds_last_cycle": 0.0,
            "cpu_seconds_total": 0.0,
            "cpu_seconds_window": [],
            "cpu_wall_window": [],
            "last_wake_attempt_at": now - idle_seconds,
            "last_mail_seen_at": now - idle_seconds,
            "idle_retire_days": days,
        }
        if payload is not None:
            payload.update(
                {
                    "last_wake_attempt_at": now - idle_seconds,
                    "last_mail_seen_at": now - idle_seconds,
                    "idle_retire_days": days,
                }
            )
            runtime = payload
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(runtime) + "\n", encoding="utf-8")

    def retired_rows(self) -> list[dict]:
        return read_records_snapshot(
            self.root,
            Path("receipts/wake-daemon") / f"{self.node}.jsonl",
            allowed_kinds=DAEMON_KINDS,
        )

    def retired(self, reason: str) -> list[dict]:
        rows = read_records_snapshot(
            self.root,
            Path("receipts/wake-daemon") / f"{self.node}.jsonl",
            allowed_kinds=DAEMON_KINDS,
        )
        return [
            row
            for row in rows
            if row.get("event") in ("revoked", "stopped", "retired")
            and row.get("reason_code") == reason
        ]

    def serve_bounded(self, daemon, hook=None, cycles: int = 4) -> list[float]:
        """Real clock: the idle window is measured in days against the
        stamps the fixture writes in real epoch time."""

        sleeps: list[float] = []
        state = {"n": 0}

        def stop_requested() -> bool:
            return state["n"] >= cycles

        def sleep(delay: float) -> None:
            state["n"] += 1
            if hook is not None:
                hook(state)
            sleeps.append(delay)

        daemon.serve(stop_requested, clock=time.time, sleep=sleep)
        return sleeps


class IdleRetireTests(IdleRetireFixture):
    def test_a_runtime_idle_past_the_window_retires_and_exits_clean(self) -> None:
        self.write_runtime_idle(idle_seconds=15 * _DAY, days=14)

        sleeps = self.serve_bounded(self.daemon(), None, cycles=4)

        self.assertLessEqual(len(sleeps), 2, "daemon kept polling while idle past its window")
        retired = self.retired("wake_daemon_idle_retired")
        self.assertEqual(1, len(retired), retired)
        self.assertEqual("stopped", retired[0]["event"])

    def test_b_zero_days_never_retires(self) -> None:
        self.write_runtime_idle(idle_seconds=400 * _DAY, days=0)
        self.consent.consent(
            self.coordinate,
            adapter_version="1",
            adapter_digest=adapter_contract_digest("cursor"),
            min_poll_seconds=1,
            max_poll_seconds=30,
            max_backoff_seconds=120,
            activation_epoch=2,
            idle_retire_days=0,
            idempotency_key="wd4-consent-never",
        )
        _cp = self.root.path / "receipts" / "wake-daemon" / f"{self.node}.consent.jsonl"
        print("POST-V2 PLANE:", [
            (json.loads(x).get("activation_epoch"), json.loads(x).get("idle_retire_days"))
            for x in _cp.read_text().splitlines() if x.strip()
        ] if _cp.is_file() else "ABSENT")

        sleeps = self.serve_bounded(self.daemon(), None, cycles=3)

        self.assertEqual(3, len(sleeps), "0 = never: the daemon keeps cycling; rows=" + repr([
            (r.get("event"), r.get("state"), r.get("reason_code")) for r in self.retired_rows()
        ]))
        self.assertEqual([], self.retired("wake_daemon_idle_retired"))

    def test_c_wake_attempt_inside_the_window_resets_the_clock(self) -> None:
        self.write_runtime_idle(idle_seconds=2 * _DAY, days=14)
        world = self

        def stamp_recent_wake(_state: dict) -> None:
            runtime = world.runtime()
            runtime["last_wake_attempt_at"] = time.time()
            world.runtime_path().write_text(json.dumps(runtime) + "\n", encoding="utf-8")

        sleeps = self.serve_bounded(self.daemon(), stamp_recent_wake, cycles=4)

        self.assertEqual(_assert_cycles(len(sleeps)), 4)
        self.assertEqual([], self.retired("wake_daemon_idle_retired"))

    def test_control_mail_in_the_window_stays_up(self) -> None:
        self.write_runtime_idle(idle_seconds=2 * _DAY, days=14)
        EventLog(self.root).send(
            "sender", self.node, "floati", "a" * 40,
            "docs/evidence/wd-4.md", "wd-4 mail",
            worker_session_id="cursor-session-1",
            idempotency_key="wd4-control-mail",
        )

        sleeps = self.serve_bounded(self.daemon(), None, cycles=3)

        self.assertEqual([], self.retired("wake_daemon_idle_retired"))


def _assert_cycles(count: int) -> int:
    return count


class ConsentIdleRetireDaysTests(IdleRetireFixture):
    def test_consent_writer_emits_schema_version_2_with_the_field(self) -> None:
        self.consent.consent(
            self.coordinate,
            adapter_version="1",
            adapter_digest=adapter_contract_digest("cursor"),
            min_poll_seconds=1,
            max_poll_seconds=30,
            max_backoff_seconds=120,
            activation_epoch=2,
            idle_retire_days=30,
            idempotency_key="wd4-consent-2",
        )
        rows = read_records_snapshot(
            self.root,
            Path("receipts/wake-daemon") / f"{self.node}.consent.jsonl",
            allowed_kinds=DAEMON_KINDS,
        )
        mine = [
            row
            for row in rows
            if row.get("idempotency_key") == "wd4-consent-2"
        ]
        self.assertEqual(1, len(mine), mine)
        self.assertEqual(2, mine[0]["schema_version"], mine)
        self.assertEqual(30, mine[0]["idle_retire_days"], mine)

    def test_consent_writer_without_the_field_keeps_the_legacy_shape(self) -> None:
        # The fixture's first consent predates the FQ-9 plane split: it
        # lives in the legacy mixed file, still a valid consent source.
        # FQ-9 split the planes: consent rows live in <node>.consent.jsonl.
        rows = read_records_snapshot(
            self.root,
            Path("receipts/wake-daemon") / f"{self.node}.consent.jsonl",
            allowed_kinds=DAEMON_KINDS,
        )
        mine = [
            row
            for row in rows
            if row.get("idempotency_key") == "wd4-consent-1"
        ]
        self.assertEqual(1, len(mine), mine)
        self.assertEqual(0, mine[0]["schema_version"], mine)
        self.assertNotIn("idle_retire_days", mine[0], mine)




class DoctorRetirementAnnouncementTests(IdleRetireFixture):
    def _detail(self) -> str:
        from floati.doctor import project_wake_daemon_health

        rows = [
            str(row["detail"])
            for row in project_wake_daemon_health(self.root, currency_current=True)
            if row["code"] == "wake_daemon_health"
            and row["subject"] == f"{self.node}/cursor"
        ]
        self.assertEqual(1, len(rows), rows)
        return rows[0]

    def test_d_doctor_announces_from_seven_days_out_and_not_at_eight(self) -> None:
        """The authored line, verbatim, with the MEASURED retirement date."""
        from floati.doctor import project_wake_daemon_health

        # 6 days idle of 14: eight days out - no announcement yet.
        self.write_runtime_idle(idle_seconds=6 * _DAY, days=14)
        early = self._detail()
        self.assertNotIn("retires on", early, early)

        # 7 days idle of 14: seven days out - the line appears, dated.
        # The fixture stamped last-activity at now-7d; retirement lands at
        # stamp + 14 days = today + 7 days.
        self.write_runtime_idle(idle_seconds=7 * _DAY, days=14)
        detail = self._detail()
        expected_date = (
            datetime.datetime.now(datetime.timezone.utc).date()
            + datetime.timedelta(days=7)
        ).isoformat()
        expected = (
            f"wake daemon {self.node} retires on {expected_date} unless it "
            "wakes something first; change idle_retire_days at consent to "
            "keep it."
        )
        self.assertIn(expected, detail, detail)


if __name__ == "__main__":
    unittest.main()

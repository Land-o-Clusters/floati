"""WAKE-BREAKER-1: an open circuit is never terminal.

After the current backoff elapses it goes half-open and makes exactly one
probe wake. Success closes it. Failure re-opens with backoff capped at its
maximum — one probe per interval, never faster.

Am.1 (architect msg-01a073ea):
1. pause on an open breaker must never claim a probe succeeded; if it clears
   the circuit the closed words are cleared_by_operator_pause.
2. half-open with an empty inbox is half_open_awaiting_work — held until mail
   exists; the first mail is the probe; never a probe that wakes an empty seat.
"""

from __future__ import annotations

import unittest
from pathlib import Path

from floati import fixture_ids as public_ids
from floati.cursor import SparseCursor
from floati.ids import uuid7_hex
from floati.jsonl import append_record, read_records
from floati.events import EVENT_KINDS
from floati.registry import REGISTRY_KINDS, Registry
from floati.role_library import load_shipped_role_templates
from floati.wake_control import WakeController
from floati.wake_hold import WakeHoldController
from tests.test_wake_daemon import _WakeDaemonFixture


ARCHITECT = "sender"


class WakeBreakerHalfOpenTests(_WakeDaemonFixture):
    def setUp(self) -> None:
        super().setUp()
        self.bind()
        self.consent()

    def declare_architect(self, node_id: str = ARCHITECT) -> None:
        template = load_shipped_role_templates(Path("roles/shipped"))["architect"]
        append_record(
            self.root,
            Registry(self.root).relative_path,
            {
                "schema_version": 0,
                "id": "registry-role-" + uuid7_hex(),
                "tenant_id": self.root.tenant_id,
                "timestamp": "2026-09-05T18:00:00.000Z",
                "kind": "registry_role_record",
                "node_id": node_id,
                "template_role": "architect",
                "template_version": template.template_version,
                "template_sha256": template.digest,
                "answers": {question.key: "fixture" for question in template.questions},
                "state": "active",
                "predecessor_role_record_id": None,
            },
            allowed_kinds=REGISTRY_KINDS,
        )

    def notice_path(self) -> Path:
        return self.root.resolve_relative(
            Path("state/wake-daemon/notices") / f"{self.coordinate.digest}.json"
        )

    def architect_inbound(self) -> list:
        return [
            row
            for row in read_records(
                self.root, "events.jsonl", allowed_kinds=EVENT_KINDS
            )
            if row.get("kind") == "message_envelope" and row.get("recipient") == ARCHITECT
        ]

    def open_breaker(self, reason_code: str = "wake_daemon_adapter_timeout"):
        self.send("breaker-probe-message")
        self.adapter.outcome = "unknown"
        self.adapter.reason_code = reason_code
        daemon = self.daemon()
        for now in (100.0, 102.0, 106.0):
            daemon.run_cycle(now)
        return daemon

    def due_probe_time(self, daemon) -> float:
        return float(daemon.read_runtime()["next_poll_at"])

    def test_an_open_circuit_with_elapsed_backoff_makes_exactly_one_probe(self) -> None:
        """RED today: elapsed backoff still returns circuit-open backpressure."""

        daemon = self.open_breaker()
        calls_at_open = len(self.adapter.calls)
        self.assertEqual("open", daemon.read_runtime()["circuit_state"])
        self.adapter.outcome = "woke"
        due = self.due_probe_time(daemon)

        result = daemon.run_cycle(due)

        self.assertEqual(calls_at_open + 1, len(self.adapter.calls))
        self.assertEqual("woke", result["state"])
        self.assertEqual("closed", daemon.read_runtime()["circuit_state"])
        self.assertEqual(0, daemon.read_runtime()["consecutive_refusals"])
        self.assertFalse(self.notice_path().exists())

    def test_a_failed_probe_reopens_at_capped_backoff_and_does_not_probe_faster(
        self,
    ) -> None:
        """RED today: open cycles schedule backoff without calling the adapter."""

        daemon = self.open_breaker()
        calls_at_open = len(self.adapter.calls)
        due = self.due_probe_time(daemon)

        first = daemon.run_cycle(due)

        self.assertEqual(calls_at_open + 1, len(self.adapter.calls))
        runtime = daemon.read_runtime()
        self.assertEqual("open", runtime["circuit_state"])
        self.assertEqual(8, runtime["current_backoff"])
        self.assertGreater(float(runtime["next_poll_at"]), due)
        self.assertNotEqual("woke", first["state"])

        too_soon = due + 0.5
        self.assertEqual("backpressure", daemon.run_cycle(too_soon)["state"])
        self.assertEqual(calls_at_open + 1, len(self.adapter.calls))

        again = float(daemon.read_runtime()["next_poll_at"])
        daemon.run_cycle(again)
        self.assertEqual(calls_at_open + 2, len(self.adapter.calls))
        self.assertEqual("open", daemon.read_runtime()["circuit_state"])

    def test_probe_success_posts_a_closed_envelope_keyed_on_breaker_transitions(
        self,
    ) -> None:
        """RED today: recovery never reaches the architect."""

        self.declare_architect()
        daemon = self.open_breaker()
        self.assertEqual(1, len(self.architect_inbound()))
        transitions = int(daemon.read_runtime()["breaker_transitions"])
        self.adapter.outcome = "woke"

        daemon.run_cycle(self.due_probe_time(daemon))

        inbound = self.architect_inbound()
        self.assertEqual(2, len(inbound))
        closed = inbound[1]
        self.assertIn("WAKE BREAKER CLOSED", closed["note"])
        self.assertEqual(
            f"wake-breaker-{self.coordinate.digest}-{transitions}-closed",
            closed["idempotency_key"],
        )
        self.assertNotEqual(inbound[0]["id"], closed["id"])

    def test_pause_on_open_breaker_clears_without_claiming_a_probe(self) -> None:
        """Am.1 RED: pause posts WAKE BREAKER CLOSED probe succeeded with zero
        adapter calls after the open — a false envelope."""

        self.declare_architect()
        daemon = self.open_breaker()
        calls_at_open = len(self.adapter.calls)
        self.assertEqual(1, len(self.architect_inbound()))
        WakeController(self.root).pause(
            public_ids.builder("a"),
            "cursor-session-1",
            idempotency_key="pause-open-breaker",
        )

        result = daemon.run_cycle(self.due_probe_time(daemon))

        self.assertEqual("paused", result["state"])
        self.assertEqual(calls_at_open, len(self.adapter.calls))
        runtime = daemon.read_runtime()
        self.assertEqual("closed", runtime["circuit_state"])
        self.assertEqual(0, runtime["consecutive_refusals"])
        self.assertFalse(self.notice_path().exists())
        inbound = self.architect_inbound()
        self.assertEqual(2, len(inbound))
        closed = inbound[1]
        self.assertIn("WAKE BREAKER CLOSED", closed["note"])
        self.assertIn("cleared_by_operator_pause", closed["note"])
        self.assertNotIn("probe succeeded", closed["note"])

    def test_half_open_empty_inbox_awaits_work_and_first_mail_is_the_probe(
        self,
    ) -> None:
        """Am.1 RED: empty inbox at half-open goes idle while the circuit stays
        open forever — never naming half_open_awaiting_work, never probing."""

        daemon = self.open_breaker()
        rows = [
            row
            for row in read_records(
                self.root, "events.jsonl", allowed_kinds=EVENT_KINDS
            )
            if row.get("kind") == "message_envelope"
            and row.get("recipient") == public_ids.builder("a")
        ]
        WakeHoldController(self.root).evaluate(
            public_ids.builder("a"),
            worker_session_id="cursor-session-1",
            idempotency_key="am1-drain",
        )
        SparseCursor(self.root).ack(
            public_ids.builder("a"),
            [row["id"] for row in rows],
            acting_session_id="cursor-session-1",
            worker_session_id="cursor-session-1",
        )
        calls_at_empty = len(self.adapter.calls)

        empty = daemon.run_cycle(self.due_probe_time(daemon))

        self.assertEqual("half_open_awaiting_work", empty["state"])
        self.assertEqual(calls_at_empty, len(self.adapter.calls))
        self.assertEqual("open", daemon.read_runtime()["circuit_state"])

        self.send("half-open-first-mail")
        self.adapter.outcome = "woke"
        due = float(daemon.read_runtime()["next_poll_at"])
        probed = daemon.run_cycle(due)

        self.assertEqual(calls_at_empty + 1, len(self.adapter.calls))
        self.assertEqual("woke", probed["state"])
        self.assertEqual("closed", daemon.read_runtime()["circuit_state"])


if __name__ == "__main__":
    unittest.main()

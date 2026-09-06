"""WAKE-NOTICE-1: a breaker that opens must say so ON THE BUS, once.

Measured on the fleet's live bus root, 2026-09-05: one cursor seat had
``circuit_state=open consecutive_refusals=381`` and had woken nothing since
2026-08-29T23:57:56Z — six days — and a second zcode seat's daemon had been open for 69.
The breaker had said so exactly where nobody was looking: one local file under
``state/wake-daemon/notices/``.  Nothing reached the bus, so the fleet's
architect had no way to learn that a seat had stopped being wakeable except by
running ``doctor`` and reading it.

WD-R7 chose "local only, no network, no telemetry" and that choice is right and
is NOT being reversed here: the bus is the fleet's own append-only ledger on
this same disk, not a network. A daemon that can wake a seat can tell the
architect it has stopped trying.

Idempotency is per TRANSITION, not per cycle: grok's open circuit has run 378
backpressure cycles, and 378 envelopes would be worse than silence. The
transition instrument is the notice file itself — WD-R7 already writes it
exactly once per crossing into open and clears it on recovery — so the
announcement rides that crossing and inherits its idempotency.

``wake_daemon_budget_exhausted`` reaches the bus through the same crossing: it
is one of the reasons that trips the breaker, and it is the reason recorded on
that second seat's live notice today.

The doctor half of this row was ALREADY SHIPPED (WD-2 GAP 2,
``doctor.project_wake_daemon_health``) and the case here is a control that
passes before and after: measured on the live root, doctor named grok at 381
and the second zcode seat at 69. It is kept because a row is not done until its
assertion exists.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from floati import fixture_ids as public_ids
from floati.ids import uuid7_hex
from floati.jsonl import append_record
from floati.registry import REGISTRY_KINDS, Registry
from floati.role_library import load_shipped_role_templates
from tests.test_wake_daemon import _WakeDaemonFixture


ARCHITECT = "sender"


class WakeBreakerAnnouncementTests(_WakeDaemonFixture):
    """RED before the fix: the breaker opens and the bus stays empty."""

    def setUp(self) -> None:
        super().setUp()
        self.bind()
        self.consent()

    def declare_architect(self, node_id: str = ARCHITECT) -> None:
        """Name the one active architect the way the root names it (ARCH-0)."""
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

    def notice_relative(self) -> str:
        return f"state/wake-daemon/notices/{self.coordinate.digest}.json"

    def open_breaker(self, reason_code: str = "wake_daemon_adapter_timeout"):
        self.send("breaker-announcement-message")
        self.adapter.outcome = "unknown"
        self.adapter.reason_code = reason_code
        daemon = self.daemon()
        for now in (100.0, 102.0, 106.0):
            daemon.run_cycle(now)
        return daemon

    def reconsent(self, activation_epoch: int, key: str) -> None:
        """Close the circuit the only way the engine closes it.

        ``breaker_status_for_node`` says it in as many words: "only a consent
        re-grant (a new activation epoch) re-initializes it".  That re-grant
        rebuilds the runtime from ``_initial_runtime``, and the next cycle
        clears the notice because the circuit reads closed again.
        """
        from floati.wake_daemon_adapters import adapter_contract_digest
        from floati.wake_daemon_contract import DaemonConsentLedger

        DaemonConsentLedger(self.root).consent(
            self.coordinate,
            adapter_version="1",
            adapter_digest=adapter_contract_digest("cursor"),
            min_poll_seconds=1,
            max_poll_seconds=4,
            max_backoff_seconds=8,
            activation_epoch=activation_epoch,
            idempotency_key=key,
        )

    def architect_inbound(self) -> list:
        from floati.events import EVENT_KINDS
        from floati.jsonl import read_records

        return [
            row
            for row in read_records(self.root, "events.jsonl", allowed_kinds=EVENT_KINDS)
            if row.get("kind") == "message_envelope" and row.get("recipient") == ARCHITECT
        ]

    def test_the_crossing_into_an_open_circuit_posts_one_inbound_to_the_architect(self) -> None:
        """Catches a seat going deaf for six days with nothing on the bus."""
        self.declare_architect()

        self.open_breaker()

        inbound = self.architect_inbound()
        self.assertEqual(1, len(inbound))
        envelope = inbound[0]
        self.assertEqual(public_ids.builder("a"), envelope["sender"])
        self.assertEqual(ARCHITECT, envelope["recipient"])
        self.assertIn("wake_daemon_adapter_timeout", envelope["note"])
        self.assertEqual(self.notice_relative(), envelope["doc"])
        self.assertEqual(self.coordinate.digest, envelope["sha"])

    def test_a_budget_exhausted_crossing_names_that_reason_on_the_bus(self) -> None:
        """Catches only one of the two named reason codes reaching the architect."""
        self.declare_architect()

        self.open_breaker(reason_code="wake_daemon_budget_exhausted")

        self.assertIn("wake_daemon_budget_exhausted", self.architect_inbound()[0]["note"])

    def test_the_inbound_is_one_per_transition_not_one_per_open_cycle(self) -> None:
        """Catches 378 envelopes: grok's live open circuit has run that many cycles."""
        self.declare_architect()
        daemon = self.open_breaker()

        self.assertEqual("adapter_unknown", daemon.run_cycle(114.0)["state"])
        self.assertEqual("adapter_unknown", daemon.run_cycle(122.0)["state"])
        self.assertEqual("adapter_unknown", daemon.run_cycle(138.0)["state"])

        self.assertEqual(1, len(self.architect_inbound()))

    def test_a_reopened_breaker_announces_again_instead_of_deduping_as_a_replay(
        self,
    ) -> None:
        """Catches the second outage arriving as a replay of the first.

        The announcement's idempotency key was
        ``wake-breaker-<coordinate digest>-<cycle_index>``, and a consent
        re-grant re-initializes the runtime with ``cycle_index`` back at 0
        while the breaker still trips at the same fixed threshold.  So a real
        close-then-reopen relapse composes a BYTE-IDENTICAL key, the bus
        dedupes the send as a replay, and the second announcement returns the
        FIRST envelope's id.  The architect learns of the first outage and
        never of the relapse - and the local notice says the announcement
        succeeded, naming a message id that belongs to a week-old outage.
        """
        self.declare_architect()

        self.open_breaker()
        first = self.architect_inbound()
        self.assertEqual(1, len(first))

        self.reconsent(2, "daemon-consent-reopened")
        self.send("breaker-relapse-message")
        self.adapter.outcome = "unknown"
        self.adapter.reason_code = "wake_daemon_adapter_timeout"
        relapsed = self.daemon()
        recovered = relapsed.run_cycle(300.0)
        self.assertEqual("closed", recovered["circuit_state"])
        self.assertFalse(self.notice_path().exists())
        for now in (302.0, 306.0):
            relapsed.run_cycle(now)
        self.assertTrue(self.notice_path().exists())

        inbound = self.architect_inbound()
        self.assertEqual(2, len(inbound))
        self.assertNotEqual(inbound[0]["id"], inbound[1]["id"])
        notice = json.loads(self.notice_path().read_text(encoding="utf-8"))
        self.assertEqual(inbound[1]["id"], notice["announcement"]["message_id"])

    def test_a_notice_lost_mid_outage_is_rederived_without_a_second_inbound(
        self,
    ) -> None:
        """Catches a second envelope for ONE outage when the notice is lost.

        WD-R7 chose presence over a count deliberately: the notice re-derives
        from the durable runtime, so a restart or a stray delete cannot leave
        it gone.  That makes the crossing guard reachable twice inside a single
        open circuit, and it is the case that decides whether the announcement
        key is a transition count or just a counter that happens to move.

        The re-derived notice records ``idempotency_conflict`` rather than a
        message id: the refusal count has moved on, so the same key now carries
        different text and the bus refuses to spend it twice.  That is the
        mechanism working - the FIRST envelope is still on the bus, and the
        notice says in a word why this send was not a second one.
        """
        self.declare_architect()
        daemon = self.open_breaker()
        first = self.architect_inbound()
        self.assertEqual(1, len(first))

        self.notice_path().unlink()
        self.assertEqual("adapter_unknown", daemon.run_cycle(114.0)["state"])
        self.assertTrue(self.notice_path().exists())

        self.assertEqual(first, self.architect_inbound())
        notice = json.loads(self.notice_path().read_text(encoding="utf-8"))
        self.assertEqual("idempotency_conflict", notice["announcement"]["refused"])

    def test_the_notice_records_where_the_announcement_went(self) -> None:
        """Catches a post whose success or refusal is invisible to the operator."""
        self.declare_architect()

        self.open_breaker()

        notice = json.loads(self.notice_path().read_text(encoding="utf-8"))
        self.assertEqual(ARCHITECT, notice["announcement"]["recipient"])
        self.assertEqual(
            self.architect_inbound()[0]["id"], notice["announcement"]["message_id"]
        )

    def test_a_bus_that_cannot_take_the_inbound_never_kills_the_cycle(self) -> None:
        """Catches a fleet-wide daemon crash the day nobody declares an architect."""
        daemon = self.open_breaker()

        self.assertEqual("adapter_unknown", daemon.run_cycle(114.0)["state"])
        notice = json.loads(self.notice_path().read_text(encoding="utf-8"))
        self.assertEqual("role_architect_invalid", notice["announcement"]["refused"])
        self.assertEqual([], self.architect_inbound())


class DoctorListsEveryOpenCircuitTests(_WakeDaemonFixture):
    """Control: shipped by WD-2 GAP 2; green before and after this row."""

    def test_doctor_names_every_open_circuit_with_its_refusal_count(self) -> None:
        from floati.doctor import project_wake_daemon_health

        self.bind()
        self.consent()
        self.send("doctor-breaker-message")
        self.adapter.outcome = "unknown"
        self.adapter.reason_code = "fake_adapter_unknown"
        daemon = self.daemon()
        for now in (100.0, 102.0, 106.0):
            daemon.run_cycle(now)

        findings = [
            finding
            for finding in project_wake_daemon_health(self.root, currency_current=True)
            if finding["code"] == "wake_daemon_health"
            and finding["subject"] == f"{public_ids.builder('a')}/cursor"
        ]

        self.assertEqual(1, len(findings))
        self.assertEqual("warning", findings[0]["severity"])
        self.assertIn("circuit_state=open", findings[0]["detail"])
        self.assertIn("consecutive_refusals=3", findings[0]["detail"])


if __name__ == "__main__":  # pragma: no cover - module is run by the discoverer
    unittest.main()

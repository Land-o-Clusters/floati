"""BUS-DELIVERY-UNPRESENTED-1: a delivery receipt is not evidence of presentation.

A delivery whose recipient presenter (its Stop waiter, hook or wake daemon)
cannot be shown alive from the bus's own receipts at delivery time is receipted
``delivered_unpresented``, a distinct kind, never ``delivered``
(``docs/status/BACKLOG-FLOATI-2026-09.md`` 2026-09-20 15:45Z; measured there:
grok's orphaned waiter receipted ``delivered`` to a stdout no model would ever
read).  Presenter liveness is derived only from journals that already exist -
the hook journal ``receipts/stop-holds/<node>.jsonl``,
``receipts/wakes/<node>.jsonl`` and ``receipts/wake-daemon/<node>.jsonl``;
``recipient_readiness`` alone is not evidence (SEND-READINESS-UNJOURNALED-1
measured it wrong for Codex lanes).
"""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import tests  # Pin HOME before any product import resolves a default path.

from floati.events import EventLog
from floati.jsonl import read_records_snapshot
from floati.registry import Registry
from floati.root import FloatiRoot

import tests.test_f2_readiness_hold as readiness


DELIVERY_PLANE_KINDS = {
    "delivery_receipt",
    "delivered_unpresented",
    "wake_hold_receipt",
}


class BusDeliveryUnpresentedTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(
            prefix="floati-bus-delivery-unpresented-"
        )
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name) / "fleet"
        self.root = FloatiRoot.open_direct_home(self.home, create=True)
        self.registry = Registry(self.root)
        self.registry.register("sender", "codex")
        self.registry.register("recipient", "codex")
        self.log = EventLog(self.root)

    def _send(self):
        return self.log.send(
            "sender",
            "recipient",
            "floati",
            "a" * 40,
            "docs/x.md",
            "envelope",
            now=datetime.now(timezone.utc),
        )

    def _delivery_rows(self, node: str = "recipient", root: FloatiRoot | None = None):
        root = root or self.root
        rows = []
        base = root.tenant_home / "receipts/deliveries"
        candidates = [base / f"{node}.jsonl"]
        session_directory = base / node
        if session_directory.is_dir():
            candidates.extend(sorted(session_directory.glob("*.jsonl")))
        for path in candidates:
            if not path.is_file():
                continue
            rows.extend(
                read_records_snapshot(
                    root,
                    path.relative_to(root.tenant_home),
                    allowed_kinds=set(DELIVERY_PLANE_KINDS),
                )
            )
        return rows

    def test_delivery_without_live_presenter_is_receipted_delivered_unpresented(
        self,
    ) -> None:
        """A fixture node with no live presenter may not receipt ``delivered``.

        No stop-hold testimony, no wake receipts and no daemon receipts exist
        for the recipient, so nothing on the bus shows a presenter alive at the
        delivery instant.  RED measured at harbor ``5445ad54``: the receipt said
        ``delivery_receipt``.
        """
        envelope = self._send()
        _messages, receipt = self.log.present("recipient")
        self.assertEqual("delivered_unpresented", receipt["kind"])
        self.assertEqual([envelope["id"]], receipt["item_ids"])
        evidence = receipt["presenter_evidence"]
        self.assertFalse(evidence["shown"])
        consulted = [source["path"] for source in evidence["sources"]]
        self.assertTrue(
            any(path.endswith("receipts/stop-holds/recipient.jsonl") for path in consulted),
            consulted,
        )
        durable = self._delivery_rows()
        self.assertEqual(["delivered_unpresented"], [row["kind"] for row in durable])

    def test_delivery_inside_live_hold_stays_delivered(self) -> None:
        """A real Stop waiter's publication inside its live hold stays delivered.

        The waiter writes the delivery receipt while its own hold-start
        testimony covers the delivery instant, so the hook journal shows the
        presenter alive and the plain kind stands.
        """
        f = readiness.NativeStopHoldReadinessTests("runTest")
        self.addCleanup(f.doCleanups)
        f.setUp()
        f.fixture("Codex")
        # This fixture advances a virtual waiter clock. Delivery must use the
        # same UTC clock, rather than letting real startup cost consume its
        # synthetic two-second testimony window.
        def fixture_now():
            return datetime.fromtimestamp(f.clock.wall_time(), timezone.utc)
        with mock.patch("floati.events.utc_now", side_effect=lambda:
                fixture_now().isoformat(timespec="milliseconds").replace("+00:00", "Z")):
            f.run_hold("Codex", lambda: f.send("during-hold", now=fixture_now()))
        rows = self._delivery_rows(f.node, root=f.root)
        self.assertEqual(1, len(rows))
        self.assertEqual("delivery_receipt", rows[0]["kind"],
            {"delivery": rows[0], "holds": f.hold_rows()})
        start = f.hold_rows()[0]
        began = datetime.fromisoformat(start["started_at"].replace("Z", "+00:00"))
        delivered = datetime.fromisoformat(rows[0]["timestamp"].replace("Z", "+00:00"))
        self.assertLessEqual(began, delivered)
        self.assertLess(delivered, began + timedelta(seconds=start["deadline_seconds"]))

    def test_malformed_presenter_journal_does_not_refuse_delivery(self) -> None:
        """Unreadable native hold evidence is not permission to lose the mail."""
        relative = Path("receipts/stop-holds/recipient.jsonl")
        journal = self.root.tenant_home / relative
        journal.parent.mkdir(parents=True, exist_ok=True)
        broken = b"not-a-native-record\n"
        journal.write_bytes(broken)
        envelope = self._send()

        messages, receipt = self.log.present("recipient")

        self.assertEqual([envelope["id"]], [row["id"] for row in messages])
        self.assertEqual("delivered_unpresented", receipt["kind"])
        self.assertFalse(receipt["presenter_evidence"]["shown"])
        self.assertIn(
            {"path": str(relative), "record_id": None},
            receipt["presenter_evidence"]["sources"],
        )
        self.assertEqual(broken, journal.read_bytes())

    def test_unreadable_daemon_lifecycle_cannot_credit_a_prior_attempt(self) -> None:
        """An unreadable native stop journal cannot be replaced by older activity."""
        from floati.errors import ProtocolRefusal
        from floati.wake_daemon_contract import (
            DaemonConsentLedger, DaemonCoordinate, DaemonLifecycleLedger,
        )
        from floati.wake_hold import WakeAttemptLedger

        coordinate = DaemonCoordinate(self.root, "recipient", "codex")
        DaemonConsentLedger(self.root).consent(
            coordinate, adapter_version="1.0.0", adapter_digest="a" * 64,
            min_poll_seconds=1, max_poll_seconds=5, max_backoff_seconds=10,
            activation_epoch=1, idempotency_key="unreadable-lifecycle-consent",
        )
        envelope = self._send()
        with self.assertRaises(ProtocolRefusal) as caught:
            WakeAttemptLedger(self.root).record(
                recipient="recipient", acting_session_id="daemon-session",
                item_ids=[envelope["id"]], decision_receipt_id=None,
                message_worker_session_id=None,
                idempotency_key="before-unreadable-lifecycle",
                outcome="refused", reason_code="wake_prompt_failed",
            )
        self.assertEqual("wake_prompt_failed", caught.exception.code)
        DaemonLifecycleLedger(self.root).record(
            coordinate, daemon_instance_id="daemon-unreadable-lifecycle",
            activation_epoch=1, event="stopped", state="stopped",
            reason_code="operator_stop", adapter_digest="a" * 64,
            plist_digest=None, session_digest=None, predecessor_receipt_id=None,
            idempotency_key="stop-before-unreadable-lifecycle",
        )
        relative = Path("receipts/wake-daemon/recipient.jsonl")
        journal = self.root.tenant_home / relative
        with journal.open("ab") as stream:
            stream.write(b"not-a-native-record\n")
        before = journal.read_bytes()

        messages, receipt = self.log.present("recipient")

        self.assertEqual([envelope["id"]], [row["id"] for row in messages])
        self.assertEqual("delivered_unpresented", receipt["kind"])
        self.assertFalse(receipt["presenter_evidence"]["shown"])
        self.assertIn({"path": str(relative), "record_id": None},
                      receipt["presenter_evidence"]["sources"])
        self.assertEqual(before, journal.read_bytes())

    def test_unpresented_delivery_never_suppresses_representation(self) -> None:
        """Mail receipted unpresented is presented again to the next presenter.

        An unpresented receipt is delivery evidence for acknowledgment alone;
        treating it as "the seat has it" would starve the seat silently, the
        exact deafness the 2026-09-20 finding names.
        """
        envelope = self._send()
        _first_messages, _first = self.log.present("recipient")
        second_messages, second = self.log.present("recipient")
        self.assertEqual([envelope["id"]], [message["id"] for message in second_messages])
        self.assertEqual("delivered_unpresented", second["kind"])
        self.assertEqual(2, second["presentation_count"])

    def test_wake_attempt_reads_mixed_delivery_plane_and_keeps_exact_decision(self) -> None:
        """An older unpresented row must not block a later lawful wake decision."""
        from floati.errors import ProtocolRefusal
        from floati.wake_hold import WakeAttemptLedger, WakeHoldController

        envelope = self._send()
        decision = WakeHoldController(self.root).evaluate(
            "recipient", idempotency_key="mixed-plane-decision",
        )["receipt"]
        _messages, first = self.log.present("recipient")
        self.assertEqual("delivered_unpresented", first["kind"])
        ledger = WakeAttemptLedger(self.root)
        arguments = dict(
            recipient="recipient", acting_session_id="mixed-plane-session",
            item_ids=[envelope["id"]], decision_receipt_id=decision["id"],
            message_worker_session_id=None, outcome="woke",
        )
        receipt = ledger.record(**arguments, idempotency_key="mixed-plane-wake")
        self.assertEqual("woke", receipt["outcome"])
        self.assertEqual(decision["id"], receipt["decision_receipt_id"])
        arguments["decision_receipt_id"] = first["id"]
        with self.assertRaises(ProtocolRefusal) as caught:
            ledger.record(**arguments, idempotency_key="wrong-plane-decision")
        self.assertEqual("decision_receipt_id_invalid", caught.exception.code)

    def test_unpresented_delivery_does_not_suppress_native_wake_selection(self) -> None:
        """The native wake plane must still offer mail that no presenter held."""
        from floati.wake_hold import WakeHoldController

        envelope = self._send()
        _messages, receipt = self.log.present("recipient")
        self.assertEqual("delivered_unpresented", receipt["kind"])
        artifact = WakeHoldController(self.root).evaluate(
            "recipient", idempotency_key="wake-after-unpresented",
        )
        self.assertIsNotNone(artifact["receipt"], artifact)
        self.assertEqual([envelope["id"]], artifact["receipt"]["item_ids"])

    def test_ack_after_unpresented_delivery_is_valid_evidence(self) -> None:
        """An ack against an unpresented delivery stands: SEEN is SEEN."""
        from floati.cursor import SparseCursor

        envelope = self._send()
        self.log.present("recipient")
        SparseCursor(self.root).ack(
            "recipient", [envelope["id"]], acting_session_id="session-unpresented"
        )
        from floati.sent import SentProjection

        items = SentProjection(self.root).items("sender")
        self.assertEqual("acknowledged", items[0]["state"])
        self.assertFalse(items[0]["delivered"])

    def test_sent_renders_the_distinct_state_and_the_board_counts_per_node(self) -> None:
        """``floati sent`` says delivered_unpresented; the seat board counts it.

        The fleet-status v0 machine contract is a closed schema (its node
        objects and receipt_counts admit no new keys), so the per-node count
        lives on the board model, not in the status artifact.
        """
        from floati.sent import SentProjection
        from floati.tui_render import HarborBoardModel, _node_line

        self._send()
        self.log.present("recipient")
        items = SentProjection(self.root).items("sender")
        self.assertEqual("delivered_unpresented", items[0]["state"])
        self.assertFalse(items[0]["delivered"])
        self.assertIsNotNone(items[0]["delivered_unpresented_at"])
        model = HarborBoardModel.from_projection(
            {"observed_at": "2026-10-11T00:00:00.000Z",
             "nodes": [{"node_id": "recipient"}, {"node_id": "sender"}]},
            [],
            {"deliveries": self._delivery_rows(), "acks": [], "denials": []},
        )
        counts = {
            str(row["node_id"]): row.get("delivered_unpresented", 0)
            for row in model.nodes
        }
        self.assertEqual(1, counts["recipient"])
        self.assertEqual(0, counts["sender"])
        recipient_row = next(row for row in model.nodes if row["node_id"] == "recipient")
        self.assertIn("UNPRESENTED 1", _node_line(recipient_row, False, 200, 12))
        sender_row = next(row for row in model.nodes if row["node_id"] == "sender")
        self.assertNotIn("UNPRESENTED", _node_line(sender_row, False, 200, 12))

    def test_recent_wake_daemon_cycle_shows_the_presenter(self) -> None:
        """A fresh daemon cycle inside its consented backoff window is a live
        presenter; an ownership append cannot hide it or a later stop."""
        from floati.ids import uuid7_hex
        from floati.jsonl import append_record
        from floati.registry import utc_now
        from floati.wake_daemon_contract import (
            DAEMON_KINDS,
            DaemonConsentLedger,
            DaemonCoordinate,
            DaemonLifecycleLedger,
        )

        coordinate = DaemonCoordinate(self.root, "recipient", "codex")
        consent_ledger = DaemonConsentLedger(self.root)
        consent_ledger.consent(
            coordinate,
            adapter_version="1.0.0",
            adapter_digest="a" * 64,
            min_poll_seconds=1,
            max_poll_seconds=5,
            max_backoff_seconds=10,
            activation_epoch=1,
            idempotency_key="unpresented-consent",
        )
        lifecycle = DaemonLifecycleLedger(self.root)
        installed = lifecycle.record(
            coordinate,
            daemon_instance_id="daemon-unpresented-1",
            activation_epoch=1,
            event="installed",
            state="installed",
            reason_code=None,
            adapter_digest="a" * 64,
            plist_digest="b" * 64,
            session_digest=None,
            predecessor_receipt_id=None,
            idempotency_key="daemon-unpresented-1-installed",
        )
        lifecycle.record(
            coordinate,
            daemon_instance_id="daemon-unpresented-1",
            activation_epoch=1,
            event="idle",
            state="idle",
            reason_code=None,
            adapter_digest="a" * 64,
            plist_digest=None,
            session_digest=None,
            predecessor_receipt_id=None,
            idempotency_key="daemon-unpresented-1-1-idle",
        )
        ownership = {
            "schema_version": 1,
            "id": "wake-daemon-directory-ownership-" + uuid7_hex(),
            "tenant_id": self.root.tenant_id,
            "timestamp": utc_now(),
            "kind": "wake_daemon_directory_ownership_record",
            "node_id": "recipient",
            "harness": "codex",
            "coordinate_digest": coordinate.digest,
            "lifecycle_receipt_id": installed["id"],
            "unit_path": str(self.home / ".config/systemd/user" / ("floati-wake-" + coordinate.digest + ".service")),
            "unit_digest": "b" * 64,
            "created_directories": [],
            "idempotency_key": "presenter-install-ownership",
        }
        kinds = DAEMON_KINDS | {"wake_daemon_directory_ownership_record"}
        relative = Path("receipts/wake-daemon/recipient.jsonl")
        append_record(self.root, relative, ownership, allowed_kinds=kinds)
        self._send()
        _messages, receipt = self.log.present("recipient")
        self.assertEqual("delivery_receipt", receipt["kind"])
        lifecycle.record(
            coordinate,
            daemon_instance_id="daemon-unpresented-1",
            activation_epoch=1,
            event="stopped",
            state="stopped",
            reason_code="operator_stop",
            adapter_digest="a" * 64,
            plist_digest=None,
            session_digest=None,
            predecessor_receipt_id=None,
            idempotency_key="daemon-unpresented-1-2-stopped",
        )
        append_record(
            self.root, relative,
            dict(ownership, id="wake-daemon-directory-ownership-" + uuid7_hex(),
                 idempotency_key="presenter-install-ownership-after-stop"),
            allowed_kinds=kinds,
        )
        _messages, stopped = self.log.present("recipient")
        self.assertEqual("delivered_unpresented", stopped["kind"])

    def test_stale_daemon_cycle_is_not_a_live_presenter(self) -> None:
        """A cycle older than the consented backoff bound shows nothing."""
        from datetime import timedelta
        from floati.wake_daemon_contract import (
            DaemonConsentLedger,
            DaemonCoordinate,
            DaemonLifecycleLedger,
        )

        coordinate = DaemonCoordinate(self.root, "recipient", "codex")
        DaemonConsentLedger(self.root).consent(
            coordinate,
            adapter_version="1.0.0",
            adapter_digest="a" * 64,
            min_poll_seconds=1,
            max_poll_seconds=5,
            max_backoff_seconds=10,
            activation_epoch=1,
            idempotency_key="unpresented-consent-stale",
        )
        DaemonLifecycleLedger(self.root).record(
            coordinate,
            daemon_instance_id="daemon-unpresented-2",
            activation_epoch=1,
            event="idle",
            state="idle",
            reason_code=None,
            adapter_digest="a" * 64,
            plist_digest=None,
            session_digest=None,
            predecessor_receipt_id=None,
            idempotency_key="daemon-unpresented-2-1-idle",
        )
        self._send()
        late = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(
            timespec="milliseconds"
        ).replace("+00:00", "Z")
        with mock.patch("floati.events.utc_now", return_value=late):
            _messages, receipt = self.log.present("recipient")
        self.assertEqual("delivered_unpresented", receipt["kind"])

    def test_stopped_or_paused_daemon_overrides_a_prior_recent_attempt(self) -> None:
        """The newest native stop state dominates its earlier wake activity."""
        from floati.wake_daemon_contract import (
            DaemonConsentLedger, DaemonCoordinate, DaemonLifecycleLedger,
        )
        from floati.wake_hold import WakeAttemptLedger
        from floati.errors import ProtocolRefusal

        coordinate = DaemonCoordinate(self.root, "recipient", "codex")
        DaemonConsentLedger(self.root).consent(
            coordinate, adapter_version="1.0.0", adapter_digest="a" * 64,
            min_poll_seconds=1, max_poll_seconds=5, max_backoff_seconds=10,
            activation_epoch=1, idempotency_key="stop-dominates-consent",
        )
        envelope = self._send()
        lifecycle = DaemonLifecycleLedger(self.root)
        for state in ("stopped", "paused", "pause_unknown"):
            with self.subTest(state=state):
                with self.assertRaises(ProtocolRefusal) as caught:
                    WakeAttemptLedger(self.root).record(
                        recipient="recipient", acting_session_id="daemon-session",
                        item_ids=[envelope["id"]], decision_receipt_id=None,
                        message_worker_session_id=None,
                        idempotency_key="prior-attempt-" + state,
                        outcome="refused", reason_code="wake_prompt_failed",
                    )
                self.assertEqual("wake_prompt_failed", caught.exception.code)
                lifecycle.record(
                    coordinate, daemon_instance_id="daemon-stop-dominates",
                    activation_epoch=1, event=state, state=state,
                    reason_code="operator_stop" if state == "stopped" else None,
                    adapter_digest="a" * 64, plist_digest=None,
                    session_digest=None, predecessor_receipt_id=None,
                    idempotency_key="newest-state-" + state,
                )
                messages, receipt = self.log.present("recipient")
                self.assertEqual([envelope["id"]], [row["id"] for row in messages])
                self.assertEqual("delivered_unpresented", receipt["kind"])
                self.assertFalse(receipt["presenter_evidence"]["shown"])

        lifecycle.record(
            coordinate, daemon_instance_id="daemon-stop-dominates",
            activation_epoch=1, event="idle", state="idle", reason_code=None,
            adapter_digest="a" * 64, plist_digest=None, session_digest=None,
            predecessor_receipt_id=None, idempotency_key="resumed-state-idle",
        )
        messages, resumed = self.log.present("recipient")
        self.assertEqual([envelope["id"]], [row["id"] for row in messages])
        self.assertEqual("delivery_receipt", resumed["kind"])

    def test_status_contract_ratifies_the_optional_per_node_count(self) -> None:
        """OPUS-RULED 2026-10-11: the added OPTIONAL per-node unpresented count
        is a loosening ratified into the v0 fleet-status contract.

        (1) every record valid under the pinned schema still validates (the
        published old-shape fixture, without the field); (2) the field is
        optional and admits only a non-negative integer; (3) the supervisor
        emits the key only when positive, so a zero count keeps the old bytes.
        """
        from tests.schema_validation import SchemaValidationError, validate_json_schema

        schema = Path("schemas/v0/fleet-status-artifact.schema.json")
        fixture = (
            Path("tests/fixtures/confluence/v0/fleet-status.json").resolve()
        )
        old_artifact = json.loads(fixture.read_text(encoding="utf-8"))
        validate_json_schema(old_artifact, schema)

        new_artifact = json.loads(fixture.read_text(encoding="utf-8"))
        new_artifact["evidence"]["nodes"][0]["delivered_unpresented"] = 2
        validate_json_schema(new_artifact, schema)

        for bad in (-1, "two", 1.5, None):
            broken = json.loads(fixture.read_text(encoding="utf-8"))
            broken["evidence"]["nodes"][0]["delivered_unpresented"] = bad
            with self.subTest(value=bad):
                with self.assertRaises(SchemaValidationError):
                    validate_json_schema(broken, schema)

    def test_supervisor_emits_the_count_only_when_positive(self) -> None:
        """A zero count keeps the old shape: no key, byte-identical rendering."""
        from floati.supervisor import Supervisor

        self.registry.register("quiet", "codex")
        nodes = Supervisor(self.root).snapshot(datetime.now(timezone.utc))["nodes"]
        for row in nodes:
            self.assertNotIn("delivered_unpresented", row)
        self._send()
        self.log.present("recipient")
        nodes = Supervisor(self.root).snapshot(datetime.now(timezone.utc))["nodes"]
        counts = {
            str(row["node_id"]): row.get("delivered_unpresented")
            for row in nodes
        }
        self.assertEqual(1, counts["recipient"])
        for node_id, value in counts.items():
            if node_id != "recipient":
                self.assertIsNone(value)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

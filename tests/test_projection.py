from __future__ import annotations

from floati import fixture_ids as public_ids

import tempfile
import fcntl
import threading
import unittest
from unittest import mock
from datetime import datetime, timedelta, timezone
from pathlib import Path

from floati.cursor import SparseCursor
from floati.errors import ProtocolRefusal
from floati.events import EventLog
from floati.ids import uuid7_hex
from floati.jsonl import append_record
from floati.planes import AuthorityGrantStore, LivenessPresenceStore
from floati.registry import REGISTRY_KINDS, Registry
from floati.root import FloatiRoot
from floati.work import WorkLog
from floati.workers import WorkerReceipts
from tests.readiness import wait_join


NOW = datetime(2026, 7, 31, 12, 0, 0, tzinfo=timezone.utc)


class FleetProjectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name) / "fleet"
        self.root = FloatiRoot.open_direct_home(self.home, create=True)
        registry = Registry(self.root)
        registry.register(public_ids.worker('alpha'), "Codex")
        registry.register("bravo", "Codex")
        LivenessPresenceStore(self.root).observe(public_ids.worker('alpha'), 60, NOW)
        AuthorityGrantStore(self.root).claim("work-claims", public_ids.worker('alpha'), 60, 60, NOW)
        WorkLog(self.root).add("open item", public_ids.worker('alpha'), [], now=NOW)

        events = EventLog(self.root)
        self.message = events.send(
            public_ids.worker('alpha'), "bravo", "floati", "a" * 40,
            "docs/evidence/checkpoint.md", "notice", idempotency_key="projection-mail",
        )
        events.present("bravo")
        SparseCursor(self.root).ack(
            "bravo", [self.message["id"]], acting_session_id="projection-session"
        )
        with self.assertRaises(ProtocolRefusal) as caught:
            events.send(
                public_ids.worker('alpha'), "bravo", "floati", "a" * 40,
                "docs/evidence/checkpoint.md", "denied",
                idempotency_key="projection-mail",
            )
        self.assertEqual("idempotency_conflict", caught.exception.code)

    def test_status_projection_summarizes_fleet_work_and_receipts(self) -> None:
        from floati.projection import FleetProjection

        snapshot = FleetProjection(self.root).snapshot(NOW + timedelta(seconds=10))

        self.assertNotIn("status_schema_version", snapshot)
        self.assertNotIn("kind", snapshot)
        self.assertNotIn("root", snapshot)
        self.assertNotIn("tenant_id", snapshot)
        self.assertNotIn("mode", snapshot)
        self.assertEqual([public_ids.worker('alpha'), "bravo"], [node["node_id"] for node in snapshot["nodes"]])
        self.assertEqual({"open": 1, "claimed": 0, "completed": 0}, snapshot["work_counts"])
        # BUS-DELIVERY-UNPRESENTED-1: the fixture's delivery carries no live
        # presenter journal, so it receipts delivered_unpresented; the frozen
        # contract's delivery counter stays presented-deliveries only, and the
        # count itself lives on the seat board, not in this closed artifact.
        self.assertEqual({"delivery": 0, "ack": 1, "denial": 1}, snapshot["receipt_counts"])
        self.assertEqual(0, snapshot["stale_lease_count"])

    def test_versioned_status_artifact_adds_contract_fields_and_report_only_mode(self) -> None:
        from floati.projection import FleetProjection

        snapshot = FleetProjection(self.root).status_artifact(
            NOW + timedelta(seconds=10)
        )

        self.assertEqual(0, snapshot["status_schema_version"])
        self.assertEqual("fleet_status", snapshot["kind"])
        self.assertEqual(str(self.root.path), snapshot["root"])
        self.assertEqual(self.root.tenant_id, snapshot["tenant_id"])
        self.assertEqual("report_only", snapshot["mode"])

    def test_status_accepts_a_node_lease_written_to_the_registry_ledger(self) -> None:
        """Catches status declaring a narrower vocabulary than the registry writer."""
        from floati.projection import FleetProjection

        append_record(
            self.root,
            "registry/entries.jsonl",
            {
                "schema_version": 0,
                "id": "lease-" + uuid7_hex(),
                "tenant_id": self.root.tenant_id,
                "timestamp": "2026-07-31T12:00:00.000Z",
                "kind": "node_lease",
                "node_id": public_ids.worker('alpha'),
                "workspace": str(self.root.path / "nodes" / public_ids.worker('alpha')),
                "expires_at": "2026-07-31T12:01:00.000Z",
                "state": "active",
            },
            allowed_kinds=set(REGISTRY_KINDS),
        )

        snapshot = FleetProjection(self.root).status_artifact(
            NOW + timedelta(minutes=2)
        )

        self.assertEqual([public_ids.worker('alpha'), "bravo"], [row["node_id"] for row in snapshot["nodes"]])

    def test_receipts_keeps_delivery_ack_and_denial_histories_distinct(self) -> None:
        from floati.projection import FleetProjection

        history = FleetProjection(self.root).receipts("bravo")

        self.assertEqual(1, len(history["deliveries"]))
        self.assertEqual(1, len(history["acks"]))
        self.assertEqual(1, len(history["denials"]))
        self.assertEqual(self.message["id"], history["deliveries"][0]["item_ids"][0])
        self.assertEqual(self.message["id"], history["acks"][0]["item_ids"][0])
        self.assertEqual("idempotency_conflict", history["denials"][0]["reason_code"])

    def test_status_projects_worker_state_from_receipts_only(self) -> None:
        work = WorkLog(self.root)
        item = work.show()[0]
        work.claim(item["id"], public_ids.worker('alpha'), "work-claims", 1, now=NOW)
        WorkerReceipts(self.root).append(
            "worker-018f0f23abcd71238000000000000000",
            item["id"], public_ids.worker('alpha'), "codex", "claim", None, [], now=NOW,
        )

        from floati.projection import FleetProjection

        snapshot = FleetProjection(self.root).snapshot(NOW + timedelta(seconds=10))

        self.assertEqual("claim", snapshot["workers"][0]["state"])
        self.assertNotIn("pid", snapshot["workers"][0])

    def test_status_counts_confirmed_failed_unknown_and_compensation_states(self) -> None:
        from floati.projection import EffectStatusProjection
        from tests.test_effect_cli import mixed_effect_rows, write_effect_rows

        write_effect_rows(self.root, mixed_effect_rows())

        summary = EffectStatusProjection(self.root).summary()

        self.assertEqual(
            {"confirmed": 4, "failed": 1, "unknown": 1, "incomplete": 0},
            summary["counts"],
        )
        self.assertEqual(
            {"none": 4, "proposed": 1, "executed": 1},
            summary["compensation_counts"],
        )
        self.assertEqual(
            [
                {"state": "unknown", "count": 1},
                {"state": "incomplete", "count": 1},
                {"state": "failed", "count": 1},
                {"state": "confirmed", "count": 3},
            ],
            summary["attention"],
        )

    def test_status_artifact_matches_v1_effect_status_schema(self) -> None:
        from floati.projection import EffectStatusProjection
        from tests.schema_validation import validate_json_schema
        from tests.test_effect_cli import lifecycle_rows, write_effect_rows

        _, rows = lifecycle_rows("unknown")
        write_effect_rows(self.root, rows)
        artifact = {
            "schema_version": 1,
            "artifact_version": 0,
            "command": "effects",
            "status": "ok",
            "evidence": EffectStatusProjection(self.root).artifact(NOW),
        }

        validate_json_schema(
            artifact,
            Path("schemas/v1/effect-status-artifact.schema.json"),
        )


class ReceiptHistorySnapshotTests(unittest.TestCase):
    """Complete receipt output must not hold append locks over history I/O."""

    def setUp(self) -> None:
        from floati.message_consumption import MessageConsumption

        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = FloatiRoot.open_direct_home(Path(self.temp.name) / "fleet", create=True)
        registry = Registry(self.root)
        registry.register("sender", "Codex")
        registry.register("recipient", "Codex")
        events = EventLog(self.root)
        message = events.send("sender", "recipient", "floati", "a" * 40,
            "docs/evidence/receipt-history.md", "receipt snapshot fixture",
            idempotency_key="receipt-history")["message"]
        _messages, delivery = events.present("recipient")
        ack = SparseCursor(self.root).ack("recipient", [message["id"]], acting_session_id="receipt-session")
        consumed = MessageConsumption(self.root).consume("recipient", message["id"], "receipt-session")
        self.expected = dict(node_id="recipient", deliveries=[delivery], acks=[ack],
                             consumptions=[consumed], denials=[])
        self.paths = {family: self.root.resolve_relative("receipts/" + family + "/recipient.jsonl")
                      for family in ("deliveries", "acks")}

    def query(self):
        from floati.projection import FleetProjection
        return FleetProjection(self.root).receipts("recipient")

    def observe_reads(self, callback):
        from tests.test_incremental_jsonl import _ObservedHistoryReader
        native_open = Path.open

        def opening(path, mode="r", *args, **kwargs):
            stream = native_open(path, mode, *args, **kwargs)
            if mode == "rb" and path in self.paths.values():
                return _ObservedHistoryReader(stream, lambda: callback(path))
            return stream

        return mock.patch.object(Path, "open", opening)

    @staticmethod
    def with_ledger_writer(path, callback=lambda: None):
        with path.with_name(path.name + ".lock").open("a+b") as lock:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return False
            try:
                callback()
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        return True

    def test_receipt_history_releases_delivery_and_ack_locks_during_reads_and_decode(self) -> None:
        from floati import jsonl
        reads, decodes = [], []
        native_decode = jsonl._decode_path_records

        def reading(path):
            reads.append((path, self.with_ledger_writer(path)))

        def decoding(path, *args, **kwargs):
            if path in self.paths.values():
                decodes.append((path, self.with_ledger_writer(path)))
            return native_decode(path, *args, **kwargs)

        with self.observe_reads(reading), mock.patch.object(jsonl, "_decode_path_records", decoding):
            result = self.query()
        self.assertEqual(self.expected, result)
        self.assertEqual(set(self.paths.values()), {path for path, _available in reads})
        self.assertEqual(set(self.paths.values()), {path for path, _available in decodes})
        self.assertTrue(all(available for _path, available in reads),
                        "receipt history retains a delivery or ACK ledger lock during file I/O")
        self.assertTrue(all(available for _path, available in decodes),
                        "receipt history retains a delivery or ACK ledger lock during decoding")

    def assert_append_is_sealed(self, family):
        from floati.framing import encode_frame

        path = self.paths[family]
        key = "deliveries" if family == "deliveries" else "acks"
        original = self.expected[key][0]
        prefix = "delivery-" if family == "deliveries" else "ack-"
        appended = dict(original, id=prefix + uuid7_hex())
        if "presentation_count" in appended:
            appended["presentation_count"] += 1
        did_append = []

        def reading(selected):
            if selected != path or did_append:
                return
            def append():
                with path.open("ab") as stream:
                    stream.write(encode_frame(appended))
            self.assertTrue(self.with_ledger_writer(path, append))
            did_append.append(True)

        with self.observe_reads(reading):
            result = self.query()
        self.assertEqual([True], did_append)
        self.assertEqual(self.expected, result, "receipt export escaped its sealed append boundary")
        expected_next = dict(self.expected, **{key: [original, appended]})
        self.assertEqual(expected_next, self.query())

    def test_delivery_append_after_capture_is_visible_only_to_the_next_receipt_query(self) -> None:
        self.assert_append_is_sealed("deliveries")

    def test_ack_append_after_capture_is_visible_only_to_the_next_receipt_query(self) -> None:
        self.assert_append_is_sealed("acks")

    def test_receipt_history_retains_native_epoch_barrier_while_reading_both_planes(self) -> None:
        from floati import bus_epoch
        outcomes = []

        def reading(path):
            result = []
            def contender():
                try:
                    with bus_epoch.epoch_guard(self.root, exclusive=True):
                        result.append("epoch_writer_admitted")
                except ProtocolRefusal as failure:
                    result.append(failure.code)
                except BaseException as failure:
                    result.append(repr(failure))
            writer = threading.Thread(target=contender)
            writer.start()
            wait_join(writer, "native epoch contender finishes during receipt query")
            self.assertFalse(writer.is_alive(), "native epoch contender exceeded its bounded attempt")
            outcomes.append((path, result))

        with mock.patch.object(bus_epoch, "EPOCH_LOCK_TIMEOUT_SECONDS", 0), self.observe_reads(reading):
            result = self.query()
        self.assertEqual(self.expected, result)
        self.assertEqual(set(self.paths.values()), {path for path, _result in outcomes})
        self.assertTrue(all(result == ["epoch_lock_timeout"] for _path, result in outcomes),
                        "native epoch writer can replace a receipt plane while history bytes are read")


if __name__ == "__main__":
    unittest.main()

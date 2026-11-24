"""The ruled recipient index replaces inbox snapshots and global history reads."""
from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from floati.cursor import SparseCursor
from floati.errors import IntegrityFailure
from floati.events import EventLog
from floati.registry import Registry
from floati.root import FloatiRoot
from floati.snapshot import SnapshotStore
from tests.bus_scale_io import IOProbe
from tests.temp_roots import REAL_TEMP_ROOT
from tests.bus_scale_test_profile import StableSourceFixture
from tests.bus_scale_test_profile import stable_fixture_sources


class InboxPrefixTests(StableSourceFixture, unittest.TestCase):
    @stable_fixture_sources()
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
        self.addCleanup(temporary.cleanup)
        self.root = FloatiRoot.open_direct_home(Path(temporary.name) / "fleet", create=True)
        for node in ("sender", "recipient", "other"):
            Registry(self.root).register(node, "codex")
        self.log = EventLog(self.root)
        self.sequence = 0

    def send(self, *, recipient="recipient", worker=None):
        self.sequence += 1
        return self.log.send("sender", recipient, "floati", "a" * 40,
            "note.md", "recipient index property", idempotency_key="inbox-prefix-" + str(self.sequence),
            worker_session_id=worker)

    def observed_present(self, *, worker=None, limit=1000):
        from floati import epoch_rotation
        from floati.jsonl import MAX_RECORD_BYTES
        with self.log.path.open("rb") as stream:
            first = stream.readline(MAX_RECORD_BYTES + 1)
        anchors = []
        native = epoch_rotation._first_frame
        def observed_anchor(root, input_stat):
            frame = native(root, input_stat)
            anchors.append(frame)
            return frame
        with patch.object(epoch_rotation, "_first_frame", side_effect=observed_anchor), IOProbe(self.root.tenant_home) as probe:
            result = EventLog(self.root).present("recipient", limit=limit,
                worker_session_id=worker)
        probe.assert_complete()
        # The existing automatic epoch guard may reread one bounded physical
        # first frame when root/event/policy stat identities change. That age
        # anchor is independent of inbox and is not a history replay.
        self.assertLessEqual(len(anchors), 1)
        self.assertTrue(all(frame == first for frame in anchors))
        self.assertEqual(sum(map(len, anchors)), probe.read_bytes_by_path.get("events.jsonl", 0),
            "a warm inbox read event history beyond its exact native epoch age anchor")
        return result

    def test_inbox_uses_one_exact_prefix_and_never_creates_a_snapshot(self):
        message = self.send()
        with patch.object(SnapshotStore, "load", side_effect=AssertionError(
                "inbox still owns a second SnapshotStore")):
            shown, _receipt = self.log.present("recipient")
        self.assertEqual([message["id"]], [row["id"] for row in shown])
        self.assertTrue((self.root.tenant_home / ".inbox-prefix/checkpoint.json").is_file(),
            "the per-recipient index must use its one exact artifact literal")

    def test_fresh_inbox_and_send_tail_read_only_the_bounded_epoch_age_anchor(self):
        first = self.send()
        self.log.present("recipient")
        shown, _receipt = self.observed_present()
        self.assertEqual([first["id"]], [row["id"] for row in shown])
        for _ in range(8):
            self.send(recipient="other")
        second = self.send()
        shown, _receipt = self.observed_present()
        self.assertEqual([first["id"], second["id"]], [row["id"] for row in shown])

    def test_unacked_ids_uses_the_verified_recipient_prefix_across_mail_planes(self):
        acknowledged = self.send()
        self.log.present("recipient")
        SparseCursor(self.root).ack("recipient", [acknowledged["id"]],
            acting_session_id="already-seen")
        pending = self.send()
        worker = self.send(worker="pending-worker")
        removed = self.send(worker="removed-worker")
        self.log.retract(removed["id"], worker_session_id="removed-worker",
            reason="sent_in_error", author="sender")
        self.send(recipient="other")
        # Certify the current native prefix before the fresh reader. This
        # small-fixture bound covers the final check made by node drain;
        # timing and 1x/10x qualification remain in the explicit census.
        self.log.present("recipient")
        with IOProbe(self.root.tenant_home) as probe:
            remaining = EventLog(self.root).unacked_ids("recipient")
        probe.assert_complete()
        self.assertEqual([pending["id"], worker["id"]], remaining,
            "unacked IDs must retain worker mail while excluding acked, retracted and foreign mail")
        self.assertEqual(0, probe.read_bytes_by_path.get("events.jsonl", 0),
            "a fresh unacked-ID query reread the already verified event history")

    def test_native_retraction_remains_indexed_and_does_not_force_a_global_scan(self):
        worker = "inbox-worker"
        removed = self.send(worker=worker)
        self.log.present("recipient", worker_session_id=worker)
        self.log.retract(removed["id"], worker_session_id=worker,
            reason="sent_in_error", author="sender")
        shown, receipt = self.observed_present(worker=worker)
        self.assertEqual([], shown)
        self.assertIsNone(receipt)
        ordinary = self.send()
        shown, _receipt = self.observed_present()
        self.assertEqual([ordinary["id"]], [row["id"] for row in shown])

    def test_prefix_replaces_truncated_snapshot_fallback_and_keeps_worker_planes_separate(self):
        first, second = self.send(), self.send()
        private = self.send(worker="private-worker")
        self.log.present("recipient", limit=1)
        SparseCursor(self.root).ack("recipient", [first["id"]], acting_session_id="seen")
        shown, _receipt = self.observed_present(limit=1)
        self.assertEqual([second["id"]], [row["id"] for row in shown])
        private_shown, _receipt = self.observed_present(worker="private-worker")
        self.assertEqual([private["id"]], [row["id"] for row in private_shown])

    def test_malformed_teardown_certificate_replays_native_worker_evidence(self):
        import hashlib
        import hmac
        from floati import inbox_prefix, send_prefix
        from floati.seat_teardown_activity import _worker_pid_receipts

        self.send()
        inbox_prefix.warm(self.root)
        bank = self.root.tenant_home / inbox_prefix.CACHE_NAME
        checkpoint = bank / "checkpoint.json"
        original = checkpoint.read_bytes()
        secret = (bank / "seal.key").read_bytes()
        for field, value in (("executions", -1), ("executions", 100001),
                             ("partial", []), ("length", 0)):
            with self.subTest(field=field, value=value):
                record = json.loads(original)
                record["payload"]["folds"]["teardown"][field] = value
                record["seal"] = hmac.new(secret, send_prefix._bytes(record["payload"]),
                    hashlib.sha256).hexdigest()
                checkpoint.write_bytes(send_prefix._bytes(record))
                with IOProbe(self.root.tenant_home) as probe:
                    self.assertEqual({}, _worker_pid_receipts(self.root, "recipient"))
                probe.assert_complete()
                self.assertGreaterEqual(probe.read_bytes_by_path.get("events.jsonl", 0),
                    self.log.path.stat().st_size)

    def test_inconsistent_verified_teardown_facts_preserve_native_results(self):
        from floati import inbox_prefix
        from floati.framing import encode_frame
        from floati.key_prefix import KeyPrefix
        from floati.seat_teardown import completed_teardown_for_session
        from floati.seat_teardown_activity import _worker_pid_receipts
        from floati.seat_teardown_prefix import prefix_proof
        from tests import test_seat_teardown as fixture

        native_load, native_get = KeyPrefix.load, KeyPrefix.get
        with self.subTest(fact="cutoff endpoint"):
            seat = fixture.SeatTeardownTests()
            self.addCleanup(seat.doCleanups)
            seat.setUp()
            log = EventLog(seat.root)
            log.send(fixture.NODE, "architect", "floati", seat.sha, "report.md",
                     "historical report", idempotency_key="before-teardown")
            self.assertIsNotNone(inbox_prefix.warm(seat.root))
            rows = log.event_records()
            expected = prefix_proof(rows, log.relative_path)

            def wrong_endpoint(bank, **kwargs):
                native_load(bank, **kwargs)
                if isinstance(bank, inbox_prefix.InboxPrefix) and bank.payload["count"] == len(rows):
                    # Model a verified fold with one inconsistent field. Its
                    # digest, saved SHA state and physical source are intact.
                    bank.payload["folds"]["teardown"]["partial"][-1][0] = rows[0]["id"]

            with patch.object(KeyPrefix, "load", wrong_endpoint):
                completed = seat.teardown()
            self.assertEqual(expected, completed["event_cutoff"])
            self.assertEqual([expected] * 4, [row["event_cutoff"] for row in seat.rows()])
            with patch("floati.seat_teardown.certified_mail", return_value=None):
                self.assertEqual(completed, completed_teardown_for_session(seat.root, fixture.NODE, fixture.SESSION))

        with self.subTest(fact="execution shape"):
            self.send()
            self.assertIsNotNone(inbox_prefix.warm(self.root))
            def counted_execution(bank, **kwargs):
                native_load(bank, **kwargs)
                if isinstance(bank, inbox_prefix.InboxPrefix):
                    bank.payload["folds"]["teardown"]["executions"] = 1
            def incomplete_execution(bank, domain, value):
                if isinstance(bank, inbox_prefix.InboxPrefix) and (domain, value) == ("execution", "0"):
                    return {"kind": "seat_execution"}
                return native_get(bank, domain, value)
            with patch.object(KeyPrefix, "load", counted_execution), patch.object(
                    KeyPrefix, "get", incomplete_execution), IOProbe(self.root.tenant_home) as probe:
                self.assertEqual({}, _worker_pid_receipts(self.root, "recipient"))
            probe.assert_complete()
            self.assertGreaterEqual(probe.read_bytes_by_path.get("events.jsonl", 0), self.log.path.stat().st_size)

        with self.subTest(fact="report note"):
            seat = fixture.SeatTeardownTests()
            self.addCleanup(seat.doCleanups)
            seat.setUp()
            completed = seat.teardown()
            log = EventLog(seat.root)
            self.assertIsNotNone(inbox_prefix.warm(seat.root))
            def wrong_report(bank, domain, value):
                found = native_get(bank, domain, value)
                if isinstance(bank, inbox_prefix.InboxPrefix) and (domain, value) == ("message", completed["envelope_id"]):
                    position, envelope = found
                    return [position, dict(envelope, note="inconsistent report")]
                return found
            with patch.object(KeyPrefix, "get", wrong_report):
                self.assertEqual(completed, completed_teardown_for_session(seat.root, fixture.NODE, fixture.SESSION))
            rows = log.event_records()
            report = next(row for row in rows if row["id"] == completed["envelope_id"])
            report["note"] = "inconsistent report"
            log.path.write_bytes(b"".join(encode_frame(row) for row in rows))
            with self.assertRaises(IntegrityFailure) as refused:
                completed_teardown_for_session(seat.root, fixture.NODE, fixture.SESSION)
            self.assertEqual("seat_teardown_native_invalid", refused.exception.code)

    def test_missing_checkpoint_or_entry_recovers_native_order_without_source_mutation(self):
        messages = [self.send() for _ in range(2)]
        self.log.present("recipient")
        bank = self.root.tenant_home / ".inbox-prefix"
        self.assertTrue(bank.is_dir(), "the native inbox did not certify its recipient prefix")
        for fault in ("checkpoint", "entry"):
            with self.subTest(fault=fault):
                source = self.log.path.read_bytes()
                if fault == "checkpoint":
                    (bank / "checkpoint.json").unlink()
                else:
                    for path in (bank / "nodes").iterdir():
                        path.unlink()
                shown, _receipt = self.log.present("recipient")
                self.assertEqual([row["id"] for row in messages], [row["id"] for row in shown])
                self.assertEqual(source, self.log.path.read_bytes())
                self.observed_present()

    def test_one_byte_old_event_perturbation_cannot_hide_behind_a_current_prefix(self):
        self.send()
        self.log.present("recipient")
        self.assertTrue((self.root.tenant_home / ".inbox-prefix/checkpoint.json").is_file(),
            "the native inbox did not certify its recipient prefix")
        prior = self.log.path.stat()
        raw = self.log.path.read_bytes().replace(b'"schema_version":1', b'"schema_version":9', 1)
        self.assertNotEqual(self.log.path.read_bytes(), raw)
        self.log.path.write_bytes(raw)
        os.utime(self.log.path, ns=(prior.st_atime_ns, prior.st_mtime_ns))
        with self.assertRaises(IntegrityFailure):
            self.log.present("recipient")
        self.assertEqual(raw, self.log.path.read_bytes())

    def test_send_and_retract_advance_existing_prefix_under_the_event_lock(self):
        from floati import jsonl
        message = self.send(worker="advance-worker")
        self.log.present("recipient", worker_session_id="advance-worker")
        checkpoint = self.root.tenant_home / ".inbox-prefix/checkpoint.json"
        self.assertTrue(checkpoint.is_file(), "the native inbox did not create its prefix")
        original = jsonl._append_frame
        checkpoints = []
        def append(path, encoded, **kwargs):
            identity = original(path, encoded, **kwargs)
            if path == self.log.path:
                checkpoints.append(json.loads(checkpoint.read_bytes())["payload"]["identity"])
            return identity
        with patch.object(jsonl, "_append_frame", side_effect=append):
            self.send()
            self.log.retract(message["id"], worker_session_id="advance-worker",
                reason="sent_in_error", author="sender")
        value = json.loads(checkpoint.read_bytes())["payload"]
        self.assertEqual(self.log.path.stat().st_size, value["offset"])
        self.assertEqual(self.log.path.stat().st_ino, value["identity"][1])
        self.assertEqual(2, len(checkpoints))
        self.assertNotEqual(checkpoints[0], checkpoints[1],
            "send left the index stale before the following retraction")

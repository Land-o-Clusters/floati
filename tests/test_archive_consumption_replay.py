"""Pure consumption kernels use genuine native multi-roll inputs only."""
import tests

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import unittest
from unittest import mock

from floati import archive_consumption_replay as replay, bus_epoch
from floati.consumption import ConsumptionLedger
from floati.cursor import SparseCursor
from floati.epoch_carry import ArchiveProofResolver, verified_hold_proofs
from floati.errors import IntegrityFailure, ProtocolRefusal
from floati.events import EventLog
from floati.framing import encode_frame
from floati.ids import uuid7_hex
from floati.message_consumption import MessageConsumption
from floati.planes import AuthorityGrantStore
from floati.root import FloatiRoot
from floati.wake_hold import WakeHoldController, project_wake_items
from floati.work import WorkLog
from tests import test_epoch_carry as native_fixture


class ArchiveConsumptionReplayTests(unittest.TestCase):
    def setUp(self):
        self.fixture = native_fixture.EpochCarryTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.root
        self.tenant = self.root.tenant_id
        self.reader = MessageConsumption(self.root)

    def prepare(self, *, worker=None, consume=True):
        message = self.fixture.send("kernel-" + uuid7_hex(), worker_session_id=worker)
        EventLog(self.root).present("seat-a", worker_session_id=worker)
        ack = SparseCursor(self.root).ack("seat-a", [message["id"]],
            acting_session_id=worker or "seen", worker_session_id=worker)
        receipt = self.reader.consume("seat-a", message["id"], worker or "acting") if consume else None
        return dict(message["message"]), ack, receipt

    def epochs(self, worker=None):
        with bus_epoch.epoch_guard(self.root, exclusive=True):
            resolver = ArchiveProofResolver(self.root)
            homes = (self.root.tenant_home, *resolver.archives())
            return tuple(resolver.source(home, "seat-a", worker) for home in homes)

    def project(self, index, worker=None):
        # Complete actual native replay, including imported hold proof, before
        # supplying DATA to the first-ACT helper. The DATA itself is not proof.
        with bus_epoch.epoch_guard(self.root, exclusive=True):
            resolver = ArchiveProofResolver(self.root)
            home = (self.root.tenant_home, *resolver.archives())[index]
            source = resolver.source(home, "seat-a", worker)
            proofs = verified_hold_proofs(self.root, source, "seat-a", worker, home=home, resolver=resolver)
            project_wake_items(events=source[0], event_prefix_digests=source[1],
                deliveries=source[2], delivery_prefix_digests=source[3], acknowledgments=source[4],
                acknowledgment_prefix_digests=source[5], recipient="seat-a", worker_session_id=worker,
                tenant_id=self.tenant, _hold_proofs=proofs)
            return source

    def roll(self, key):
        return bus_epoch.roll_bus_epoch(self.root, actor="sender", idempotency_key=key)["receipt"]

    def test_current_native_history_and_exact_ack_join_match_native_result(self):
        message, ack, receipt = self.prepare()
        epochs = self.epochs()
        matching = replay.ack_references(self.tenant, "seat-a", message,
                                        epochs[0][0], epochs[0][2], epochs[0][4])
        self.assertEqual(matching, [ack])
        result = replay.replay_history(self.tenant, "seat-a", None, epochs, [receipt], [])
        self.assertEqual(result["current_receipts"], self.reader.records("seat-a"))
        self.assertEqual(len(result["joins"]), 1)
        self.assertEqual(result["joins"][0]["ack_receipt"], ack)

    def test_full_history_keeps_old_receipt_across_two_manual_boundaries(self):
        message, ack, receipt = self.prepare()
        self.roll("kernel-manual-one")
        self.roll("kernel-empty-manual-two")
        result = replay.replay_history(self.tenant, "seat-a", None, self.epochs(), [receipt], [])
        self.assertEqual(result["current_receipts"], [])
        self.assertEqual(self.reader.records("seat-a"), [])
        self.assertEqual(len(result["joins"]), 1)
        self.assertEqual(result["joins"][0]["envelope"], message)
        self.assertEqual(result["joins"][0]["ack_receipt"], ack)
        self.assertEqual(result["joins"][0]["source_index"], 2)

    def test_future_first_act_without_prior_receipt_crosses_only_automatic_rolls(self):
        message, ack, unused = self.prepare(consume=False)
        self.fixture.automatic_roll()
        self.fixture.send("kernel-intermediate", "seat-b")
        self.fixture.automatic_roll()
        self.assertEqual([], self.reader._raw("seat-a"))
        epochs = self.epochs()
        source = replay.automatic_source(self.tenant, message["id"], epochs)
        self.assertIsNotNone(source)
        self.assertGreater(source["source_index"], 0)
        projected = self.project(source["source_index"])
        candidate = replay.first_act_candidate(self.tenant, "seat-a", None, message["id"], "acting",
                                               epochs, projection_source=projected)
        self.assertIn("message", candidate)
        self.assertEqual(candidate["message"], message)
        self.assertEqual(candidate["matching_ack_receipts"], [ack])
        native = self.reader.consume("seat-a", message["id"], "acting")
        self.assertEqual(native["ack_receipt_id"], ack["id"])

    def test_manual_boundary_blocks_future_first_act_but_not_old_history(self):
        message, ack, receipt = self.prepare()
        self.roll("kernel-manual-boundary")
        self.fixture.automatic_roll()
        epochs = self.epochs()
        self.assertIsNone(replay.automatic_source(self.tenant, message["id"], epochs))
        with self.assertRaises(ProtocolRefusal) as caught:
            replay.first_act_candidate(self.tenant, "seat-a", None, message["id"], "acting", epochs,
                                       projection_source=epochs[-1])
        self.assertEqual(caught.exception.code, "consume_item_archived")
        result = replay.replay_history(self.tenant, "seat-a", None, epochs, [receipt], [])
        self.assertEqual(len(result["joins"]), 1)

    def test_direct_ack_checks_cannot_replace_native_projection_prerequisite(self):
        message, ack, unused = self.prepare(consume=False)
        with self.assertRaises(ProtocolRefusal) as caught:
            replay.first_act_candidate(self.tenant, "seat-a", None, message["id"], "acting", self.epochs())
        self.assertEqual(caught.exception.code, "archive_consumption_projection_required")

    def test_candidate_requires_exact_projection_source_binding(self):
        message, ack, unused = self.prepare(consume=False)
        epochs = self.epochs()
        changed = deepcopy(epochs[0])
        changed[4].clear()
        with self.assertRaises(IntegrityFailure):
            replay.first_act_candidate(self.tenant, "seat-a", None, message["id"], "acting", epochs,
                                       projection_source=changed)

    def test_worker_and_recipient_joins_are_exact(self):
        worker = "worker-" + uuid7_hex()
        message, ack, receipt = self.prepare(worker=worker)
        epochs = self.epochs(worker)
        with self.assertRaises(IntegrityFailure):
            replay.replay_history(self.tenant, "seat-a", "wrong-worker", epochs, [receipt], [])
        with self.assertRaises(ProtocolRefusal) as caught:
            replay.first_act_candidate(self.tenant, "seat-a", worker, message["id"], "wrong-session", epochs,
                                       projection_source=self.project(0, worker))
        self.assertEqual(caught.exception.code, "consume_session_mismatch")
        with self.assertRaises(ProtocolRefusal) as caught:
            replay.first_act_candidate(self.tenant, "seat-b", worker, message["id"], worker, epochs,
                                       projection_source=epochs[0])
        self.assertEqual(caught.exception.code, "consume_recipient_mismatch")

    def test_exact_ack_id_missing_delivery_and_foreign_ack_fail_closed(self):
        message, ack, receipt = self.prepare()
        epochs = self.epochs()
        bad = dict(receipt, ack_receipt_id="ack-" + uuid7_hex())
        with self.assertRaises(IntegrityFailure):
            replay.replay_history(self.tenant, "seat-a", None, epochs, [bad], [])
        for deliveries, acks in (([], [ack]), (epochs[0][2], [dict(ack, recipient="seat-b")])):
            with self.assertRaises(IntegrityFailure):
                replay.ack_references(self.tenant, "seat-a", message, epochs[0][0], deliveries, acks)

    def test_duplicate_consumption_and_wrong_tenant_refuse(self):
        message, ack, receipt = self.prepare()
        with self.assertRaises(IntegrityFailure):
            replay.replay_history(self.tenant, "seat-a", None, self.epochs(),
                                  [receipt, dict(receipt, id="consume-" + uuid7_hex())], [])
        with self.assertRaises(IntegrityFailure):
            replay.replay_history("foreign", "seat-a", None, self.epochs(), [receipt], [])

    def test_first_act_retraction_refuses_without_erasing_authored_history(self):
        worker = "worker-" + uuid7_hex()
        message, ack, receipt = self.prepare(worker=worker)
        EventLog(self.root).retract(message["id"], worker_session_id=worker,
                                   reason="superseded_by_correction", author="sender")
        epochs = self.epochs(worker)
        with self.assertRaises(ProtocolRefusal) as caught:
            replay.first_act_candidate(self.tenant, "seat-a", worker, message["id"], worker, epochs,
                                       projection_source=epochs[0])
        self.assertEqual(caught.exception.code, "consume_item_retracted")
        result = replay.replay_history(self.tenant, "seat-a", worker, epochs, [receipt], [])
        self.assertEqual(len(result["joins"]), 1)
        self.assertEqual(self.reader.records("seat-a"), [receipt])

    def test_work_complete_joins_raw_work_rows_but_does_not_grant_current_holder(self):
        work = WorkLog(self.root)
        now = datetime.now(timezone.utc)
        grant = AuthorityGrantStore(self.root).claim("work-claims", "seat-a", 60, 60, now)
        item = work.add("native kernel work", "seat-a", [])
        work.claim(item["id"], "seat-a", "work-claims", grant["epoch"], now=now)
        message, ack, unused = self.prepare(consume=False)
        work.complete(item["id"], "seat-a", [], consume_ids=[message["id"]], session_id="acting")
        receipt = self.reader._raw("seat-a")[0]
        rows = ConsumptionLedger(self.root).records()
        self.roll("kernel-work-manual")
        result = replay.replay_history(self.tenant, "seat-a", None, self.epochs(), [receipt], rows)
        self.assertEqual(len(result["joins"]), 1)
        self.assertEqual(result["joins"][0]["work_item"]["holder"], "seat-a")
        with self.assertRaises(IntegrityFailure):
            replay.replay_history(self.tenant, "seat-a", None, self.epochs(), [receipt], [])
        # The native caller must still consult the current live work ledger.
        (self.root.path / "work/items.jsonl").unlink()
        with self.assertRaises(IntegrityFailure):
            self.reader.records("seat-a")

    def test_prefix_tamper_refuses(self):
        message, ack, receipt = self.prepare()
        changed = deepcopy(self.epochs())
        changed = list(changed)
        source = list(changed[0])
        source[1] = ("0" * 64,) + tuple(source[1][1:])
        changed[0] = source
        with self.assertRaises(IntegrityFailure):
            replay.replay_history(self.tenant, "seat-a", None, changed, [receipt], [])

    def test_conflicting_native_identity_across_epoch_inputs_refuses(self):
        message, ack, receipt = self.prepare()
        original = self.epochs()[0]
        conflicting = deepcopy(original)
        conflicting[0][0]["note"] = "changed immutable original envelope"
        digest = hashlib.sha256(WakeHoldController._EVENT_DOMAIN.encode("ascii") + b"\0")
        prefixes = [digest.hexdigest()]
        for row in conflicting[0]:
            digest.update(encode_frame(row))
            prefixes.append(digest.hexdigest())
        conflicting = list(conflicting)
        conflicting[1] = tuple(prefixes)
        with self.assertRaises(IntegrityFailure):
            replay.replay_history(self.tenant, "seat-a", None, [original, conflicting], [receipt], [])

    def test_kernels_return_detached_data_and_perform_no_filesystem_reads(self):
        message, ack, receipt = self.prepare()
        epochs = self.epochs()
        with mock.patch("builtins.open", side_effect=AssertionError("pure kernel opened file")),\
                mock.patch.object(os, "open", side_effect=AssertionError("pure kernel opened descriptor")),\
                mock.patch.object(Path, "read_bytes", side_effect=AssertionError("pure kernel read path")):
            result = replay.replay_history(self.tenant, "seat-a", None, epochs, [receipt], [])
        self.assertEqual(len(result["joins"]), 1)
        result["joins"][0]["envelope"]["note"] = "mutated output"
        self.assertEqual(epochs[0][0][0]["note"], message["note"])

    def test_shared_join_helpers_preserve_native_refusal_order(self):
        message, ack, receipt = self.prepare()
        other, unused_ack, unused_receipt = self.prepare()
        work_receipt = dict(receipt, source="work_complete", work_item_id="work-" + uuid7_hex())
        cases = (
            ("recipient before ACK", "seat-b", receipt, message, [], None),
            ("missing ACK", "seat-a", receipt, message, [], None),
            ("different envelope", "seat-a", receipt, other, [ack], None),
            ("receipt recipient", "seat-a", dict(receipt, recipient="seat-b"), message, [ack], None),
            ("malformed work item", "seat-a", work_receipt, message, [ack], "not a work item"),
            ("different work id", "seat-a", work_receipt, message, [ack],
             {"id": "work-" + uuid7_hex(), "holder": "seat-a"}),
            ("ACK membership", "seat-a", receipt, message, [dict(ack, item_ids=[other["id"]])], None),
            ("ACK recipient", "seat-a", receipt, message, [dict(ack, recipient="seat-b")], None),
        )
        for label, node, consumed, envelope, acknowledgments, work_item in cases:
            with self.subTest(case=label):
                with self.assertRaises(IntegrityFailure) as caught:
                    replay.validate_consumed_join(self.tenant, node, consumed, envelope, acknowledgments, work_item=work_item)
                self.assertEqual("consumption_evidence_invalid", caught.exception.code)
        # The same malformed receipt must fail the envelope join before ACK validation.
        with mock.patch.object(replay, "validate_consumed_ack", side_effect=AssertionError("ACK checked before envelope")):
            with self.assertRaises(IntegrityFailure):
                replay.validate_consumed_join(self.tenant, "seat-b", receipt, message, [])


    def test_pure_source_collection_has_early_canonical_byte_bound(self):
        message, ack, receipt = self.prepare()
        with mock.patch.object(replay, "MAX_CANONICAL_BYTES", 1):
            with self.assertRaises(ProtocolRefusal) as caught:
                replay.replay_history(self.tenant, "seat-a", None, self.epochs(), [receipt], [])
        self.assertEqual(caught.exception.code, "archive_consumption_replay_unsupported")

    def cold_empty_epochs(self):
        # Genuine native source decoding after reopening an empty fleet; no
        # fabricated digest table and no existing envelope to trigger validation.
        cold = FloatiRoot.open_direct_home(self.root.path)
        with bus_epoch.epoch_guard(cold, exclusive=True):
            source = ArchiveProofResolver(cold).source(cold.tenant_home, "seat-a")
        self.assertEqual([len(source[axis]) for axis in (0, 2, 4)], [0, 0, 0])
        return (source,)

    def test_cold_empty_automatic_source_refuses_absent_or_nonstring_tenant(self):
        epochs = self.cold_empty_epochs()
        for tenant in ("", None, 0, False, [], {}):
            for operation in ("automatic", "history"):
                with self.subTest(tenant=tenant, operation=operation), self.assertRaises(IntegrityFailure):
                    if operation == "automatic":
                        replay.automatic_source(tenant, "msg-" + uuid7_hex(), epochs)
                    else:
                        replay.replay_history(tenant, "seat-a", None, epochs, [], [])
        self.assertIsNone(replay.automatic_source(self.tenant, "msg-" + uuid7_hex(), epochs))
        self.assertEqual({"current_receipts": [], "joins": [], "work_items": {}},
                         replay.replay_history(self.tenant, "seat-a", None, epochs, [], []))


    def test_batch_history_copies_shared_native_ack_once_instead_of_per_join(self):
        messages = [self.fixture.send("shared-ack-" + uuid7_hex()) for _ in range(4)]
        EventLog(self.root).present("seat-a")
        ack = SparseCursor(self.root).ack("seat-a", [row["id"] for row in messages], acting_session_id="seen")
        consumed = self.reader.consume_many("seat-a", [row["id"] for row in messages], "acting")
        epochs = self.epochs()
        self.assertEqual(1, len(epochs[0][4]))
        result = replay.replay_history(self.tenant, "seat-a", None, epochs, consumed, [])
        self.assertEqual(len(messages), len(result["joins"]))
        shared_ack = result["joins"][0]["ack_receipt"]
        for joined in result["joins"]:
            self.assertEqual([ack], joined["matching_ack_receipts"])
            self.assertIs(shared_ack, joined["ack_receipt"])
            self.assertIs(shared_ack, joined["matching_ack_receipts"][0])
        self.assertIsNot(shared_ack, epochs[0][4][0])
        shared_ack["acting_session_id"] = "changed detached output"
        self.assertEqual(ack["acting_session_id"], epochs[0][4][0]["acting_session_id"])

    def test_projection_source_requires_finite_axes_before_encoding(self):
        message, ack, unused = self.prepare(consume=False)
        epochs = self.epochs()
        projection = list(epochs[0])
        visits = []
        def unbounded():
            visits.append("consumed")
            raise AssertionError("projection generator must never be consumed")
            yield {}
        projection[0] = unbounded()
        try:
            replay.first_act_candidate(self.tenant, "seat-a", None, message["id"], "acting", epochs,
                                       projection_source=projection)
        except Exception as exc:
            self.assertIsInstance(exc, IntegrityFailure)
        else:
            self.fail("nonfinite projection axis accepted")
        self.assertEqual(visits, [])

    def test_projection_source_checks_exact_axis_lengths_before_encoding(self):
        message, ack, unused = self.prepare(consume=False)
        epochs = self.epochs()
        projection = deepcopy(list(epochs[0]))
        projection[0].append(deepcopy(projection[0][0]))
        watched = {id(row) for row in projection[0]}
        encoded = []
        native = replay.encode_frame
        def observe(row):
            if id(row) in watched:
                encoded.append(row)
            return native(row)
        with mock.patch.object(replay, "encode_frame", observe):
            with self.assertRaises(IntegrityFailure):
                replay.first_act_candidate(self.tenant, "seat-a", None, message["id"], "acting", epochs,
                                           projection_source=projection)
        self.assertEqual(encoded, [], "oversized projection axis encoded before length refusal")

"""Survivor projection collection over genuine native multi-roll history."""
from copy import deepcopy
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from floati import archive_checkpoint, bus_epoch, epoch_carry
from floati.cursor import SparseCursor
from floati.cursor_presentations import CursorPresentationLedger
from floati.errors import IntegrityFailure, ProtocolRefusal
from floati.events import EventLog
from floati.framing import decode_frames, encode_frame
from floati.ids import uuid7_hex
from floati.records import wake_hold_decision_digest
from floati.wake_hold import WakeHoldController
from tests import test_epoch_carry as carry_fixture


class ArchiveCheckpointTests(unittest.TestCase):
    def setUp(self):
        self.fixture = carry_fixture.EpochCarryTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.root

    def collect(self):
        with bus_epoch.epoch_guard(self.root, exclusive=True):
            return archive_checkpoint.collect_projection_sources(self.root)

    def presentations(self):
        with bus_epoch.epoch_guard(self.root, exclusive=True):
            return archive_checkpoint.collect_presentation_sources(self.root)

    def retries(self):
        with bus_epoch.epoch_guard(self.root, exclusive=True):
            return archive_checkpoint.collect_roll_retry_sources(self.root)

    def snapshot(self):
        return {path.relative_to(self.root.path).as_posix(): path.read_bytes()
                for path in self.root.path.rglob("*") if path.is_file()}

    def test_byte_valid_manual_archive_cannot_authorize_invalid_hold_prefix(self):
        self.fixture.send("bad-semantic-cut")
        original = WakeHoldController(self.root).evaluate("seat-a", idempotency_key="bad-cut")
        path = self.root.path / "receipts/deliveries/seat-a.jsonl"
        hostile = deepcopy(original["receipt"])
        hostile["event_prefix_digest"] = "f" * 64
        hostile["decision_digest"] = wake_hold_decision_digest(hostile)
        path.write_bytes(encode_frame(hostile))
        rolled = bus_epoch.roll_bus_epoch(self.root, actor="sender", idempotency_key="opaque-bad-cut")
        # Manual archival intentionally verifies opaque bytes, not hold semantics.
        self.assertEqual(rolled["receipt"]["archive_sha256"],
                         bus_epoch.verify_epoch_archive(Path(rolled["receipt"]["archive_path"]))["archive_sha256"])
        with self.assertRaises(IntegrityFailure):
            self.collect()
        with bus_epoch.epoch_guard(self.root, exclusive=True):
            with self.assertRaises(IntegrityFailure):
                archive_checkpoint.require_wd3_retirement_admission(self.root, rolled["receipt"])

    def test_manual_boundary_is_kept_separate_from_later_automatic_roll(self):
        message = self.fixture.send("manual-boundary")
        rolled = bus_epoch.roll_bus_epoch(self.root, actor="sender", idempotency_key="manual-boundary")
        self.fixture.send("after-manual")
        self.fixture.automatic_roll()
        cuts = [cut for cut in self.collect()["cuts"]
                if any(row["id"] == message["id"] for row in cut["source"][0])]
        self.assertEqual(1, len(cuts))
        cut = cuts[0]
        self.assertFalse(cut["automatic"])
        self.assertEqual(rolled["receipt"], cut["roll_receipt"])

    def test_same_ack_id_cannot_change_across_native_carried_history(self):
        message = self.fixture.send("acked-original")
        EventLog(self.root).present("seat-a")
        ack = SparseCursor(self.root).ack("seat-a", [message["id"]], acting_session_id="original")
        EventLog(self.root).send("seat-a", "sender", "floati", "a" * 40,
            "docs/evidence/carry.md", "pending reply keeps terminal closure",
            reply_to=message["id"], idempotency_key="pending-reply")
        self.fixture.automatic_roll()
        ack_path = self.root.path / "receipts/acks/seat-a.jsonl"
        self.assertEqual([ack], decode_frames(ack_path.read_bytes()))
        ack_path.write_bytes(encode_frame(dict(ack, acting_session_id="substituted")))
        # The native projector admits each complete source individually; global
        # collection must additionally reject conflicting carried identities.
        with self.assertRaises(IntegrityFailure):
            self.collect()

    def test_event_axes_are_complete_even_without_any_envelope_pair(self):
        bus_epoch.roll_bus_epoch(self.root, actor="sender", idempotency_key="empty-event-axis")
        actual = list(decode_frames((self.root.path / "events.jsonl").read_bytes()))
        self.assertEqual(["bus_epoch_roll_receipt"], [row["kind"] for row in actual])
        collected = self.collect()
        self.assertEqual([], collected["cuts"])
        self.assertIn("event_source", collected["epochs"][0])
        self.assertEqual(actual, collected["epochs"][0]["event_source"][0])
        self.assertEqual(len(actual) + 1, len(collected["epochs"][0]["event_source"][1]))

    def test_native_held_redelivery_without_historical_release_proof_blocks_planning(self):
        self.fixture.send("native-held-redelivery")
        controller = WakeHoldController(self.root)
        controller.evaluate("seat-a", idempotency_key="initial-holder")
        with patch("floati.wake_hold._claim_holder_released", return_value=True):
            redelivery = controller.evaluate("seat-a", idempotency_key="released-holder")
        self.assertEqual("fresh_work", redelivery["state"])
        # Preserve an actual legacy v1 release frame without the new decision.
        path = self.root.path / "receipts/deliveries/seat-a.jsonl"
        rows = decode_frames(path.read_bytes())
        rows[-1].pop("selection_reason", None)
        rows[-1]["schema_version"] = 1
        rows[-1]["decision_digest"] = wake_hold_decision_digest(rows[-1])
        path.write_bytes(b"".join(encode_frame(row) for row in rows))
        with self.assertRaises((ProtocolRefusal, IntegrityFailure)) as refused:
            self.collect()
        self.assertEqual("archive_checkpoint_unsupported", refused.exception.code)

    def test_recorded_native_release_replays_after_restart_and_roll_without_host_check(self):
        from floati.archive_projection_replay import replay_projection_pool
        from floati.archive_survivor_pool import encode_survivor_source_pool
        from floati.archive_survivor_sources import collect_survivor_sources

        self.fixture.send("recorded-holder-release")
        WakeHoldController(self.root).evaluate("seat-a", idempotency_key="first-holder")
        with patch("floati.wake_hold._claim_holder_released", return_value=True):
            released = WakeHoldController(self.root).evaluate("seat-a", idempotency_key="recorded-release")
        self.assertEqual("fresh_work", released["state"])
        with patch("floati.wake_hold._claim_holder_released",
                   side_effect=AssertionError("historical release reran the host check")):
            try:
                retried = WakeHoldController(self.root).evaluate("seat-a", idempotency_key="recorded-release")
            except (IntegrityFailure, ProtocolRefusal) as error:
                self.fail("durable release must reproduce its original decision: " + error.code)
            self.assertEqual(released, retried)
            self.fixture.automatic_roll()
            self.assertEqual(released, WakeHoldController(self.root).evaluate(
                "seat-a", idempotency_key="recorded-release"))
            with bus_epoch.epoch_guard(self.root, exclusive=True):
                plan = collect_survivor_sources(self.root)
            replay = replay_projection_pool(encode_survivor_source_pool(plan), self.root.tenant_id)
        original = next(row for row in replay["holds"] if row["receipt"]["id"] == released["receipt"]["id"])
        self.assertEqual(released, original["artifact"])

    def test_release_testimony_cannot_change_selection_or_its_digest(self):
        self.fixture.send("release-binding")
        controller = WakeHoldController(self.root)
        first = controller.evaluate("seat-a", idempotency_key="binding-first")
        with patch("floati.wake_hold._claim_holder_released", return_value=True):
            released = controller.evaluate("seat-a", idempotency_key="binding-release")
        from floati.records import validate_record
        from tests.schema_validation import validate_json_schema, SchemaValidationError
        schema = Path(__file__).resolve().parents[1] / "schemas/v2/wake-hold-receipt-record.schema.json"
        validate_json_schema(released["receipt"], schema)
        validate_json_schema(released, Path(__file__).resolve().parents[1] /
                             "schemas/v1/wake-decision-artifact.schema.json")
        for edit in ({"selection_reason": "live"}, {"schema_version": 1}, {"decision_digest": "f" * 64}):
            with self.subTest(edit=edit), self.assertRaises((ProtocolRefusal, IntegrityFailure)):
                validate_record(dict(released["receipt"], **edit), self.root.tenant_id,
                                {"wake_hold_receipt"}, integrity=True)
        with self.assertRaises(SchemaValidationError):
            validate_json_schema(dict(released["receipt"], selection_reason="live"), schema)
        # A structurally valid release flag cannot authorize an originally fresh
        # selection, even when its digest has been recomputed by the caller.
        path = self.root.path / "receipts/deliveries/seat-a.jsonl"
        changed = dict(first["receipt"], schema_version=2, selection_reason="holder_released")
        changed["decision_digest"] = wake_hold_decision_digest(changed)
        path.write_bytes(encode_frame(changed))
        with self.assertRaises((ProtocolRefusal, IntegrityFailure)):
            self.collect()

    def test_retention_requires_recorded_release_before_removing_its_source(self):
        from datetime import datetime, timedelta
        from floati.archive_retention import REMOVAL_LEDGER
        from floati.epoch_rotation import maintenance
        from floati.message_consumption import MessageConsumption

        for scenario in ("legacy-release", "recorded-release", "ordinary-hold", "keep-forever"):
            with self.subTest(scenario=scenario):
                fixture = carry_fixture.EpochCarryTests()
                fixture.setUp()
                self.addCleanup(fixture.doCleanups)
                root = fixture.root
                message = fixture.send("retiring-release")
                controller = WakeHoldController(root)
                controller.evaluate("seat-a", idempotency_key="retiring-first")
                if scenario != "ordinary-hold":
                    with patch("floati.wake_hold._claim_holder_released", return_value=True):
                        controller.evaluate("seat-a", idempotency_key="retiring-release")
                if scenario == "legacy-release":
                    path = root.path / "receipts/deliveries/seat-a.jsonl"
                    rows = decode_frames(path.read_bytes())
                    rows[-1].pop("selection_reason", None)
                    rows[-1]["schema_version"] = 1
                    rows[-1]["decision_digest"] = wake_hold_decision_digest(rows[-1])
                    path.write_bytes(b"".join(encode_frame(row) for row in rows))
                EventLog(root).present("seat-a")
                SparseCursor(root).ack("seat-a", [message["id"]], acting_session_id="seen")
                MessageConsumption(root).consume("seat-a", message["id"], "finished")
                fixture.automatic_roll()
                receipt = decode_frames((root.path / "events.jsonl").read_bytes())[0]
                archive = Path(receipt["archive_path"])
                stamp = datetime.fromisoformat(receipt["timestamp"].replace("Z", "+00:00"))
                future = stamp + timedelta(days=30, seconds=1)
                class FutureClock(datetime):
                    @classmethod
                    def now(cls, tz=None):
                        return future.astimezone(tz)
                if scenario == "keep-forever":
                    path = root.path / "state/archive-policy.json"
                    path.write_text(json.dumps({"schema_version": 1, "keep_forever": True}) + "\n")
                with patch("floati.epoch_rotation.datetime", FutureClock), patch(
                        "floati.wake_hold._claim_holder_released",
                        side_effect=AssertionError("retention reran the host release check")):
                    if scenario == "legacy-release":
                        with self.assertRaises(ProtocolRefusal) as refused:
                            maintenance(root, actor="sender", trigger="send")
                        self.assertEqual("archive_checkpoint_unsupported", refused.exception.code)
                    else:
                        maintenance(root, actor="sender", trigger="send")
                retained = scenario in {"legacy-release", "keep-forever"}
                self.assertEqual(retained, archive.exists())
                removals = root.path / REMOVAL_LEDGER
                removed = decode_frames(removals.read_bytes()) if removals.exists() else []
                self.assertEqual(not retained, any(row["epoch_id"] == receipt["epoch_id"] for row in removed))

    def test_collection_is_read_only_and_does_not_retain_public_mutable_sources(self):
        self.fixture.send("read-only")
        # Acquire the normal guard once before measuring its lock file.
        first = self.collect()
        before = self.snapshot()
        second = self.collect()
        self.assertEqual(first, second)
        self.assertEqual(before, self.snapshot())
        self.assertTrue(second["cuts"])
        second["cuts"][0]["source"][0][0]["note"] = "caller-owned mutation"
        self.assertEqual(first, self.collect())

    def test_projection_expansion_bounds_precede_replay_and_source_copy(self):
        from floati.registry import Registry
        for index in range(4):
            node = "budget-seat-" + str(index)
            Registry(self.root).register(node, "Codex")
            self.fixture.send("projection-budget-" + str(index), node=node)
        for setting in ("MAX_DERIVED_RECORD_VISITS", "MAX_DERIVED_CANONICAL_BYTES"):
            with self.subTest(setting=setting), patch.object(archive_checkpoint, setting, 0),\
                    patch.object(archive_checkpoint, "_project",
                        side_effect=AssertionError("projection replay started before derived budget")),\
                    patch.object(archive_checkpoint, "_copy_source",
                        side_effect=AssertionError("source copy started before derived budget")):
                with self.assertRaises(ProtocolRefusal) as refused:
                    self.collect()
                self.assertEqual("archive_checkpoint_unsupported", refused.exception.code)

    def test_projection_capture_bound_precedes_bulk_selected_payload_read(self):
        self.fixture.send("projection-capture-budget")
        with patch.object(archive_checkpoint, "MAX_CAPTURED_BYTES", 0),\
                patch.object(bus_epoch, "_read_selected_epoch_payloads",
                    side_effect=AssertionError("bulk selected payload read started before capture budget")):
            with self.assertRaises(ProtocolRefusal) as refused:
                self.collect()
        self.assertEqual("archive_checkpoint_unsupported", refused.exception.code)

    def test_presentation_expansion_bound_precedes_full_epoch_source_copy(self):
        self.fixture.send("presentation-copy-budget")
        self.fixture.presentation(flushed=True)
        for setting in ("MAX_DERIVED_RECORD_VISITS", "MAX_DERIVED_CANONICAL_BYTES"):
            with self.subTest(setting=setting), patch.object(archive_checkpoint, setting, 0),\
                    patch.object(archive_checkpoint, "_copy_source",
                        side_effect=AssertionError("presentation source copied before derived budget")):
                with self.assertRaises(ProtocolRefusal) as refused:
                    self.presentations()
                self.assertEqual("archive_checkpoint_unsupported", refused.exception.code)

    def test_source_changes_during_native_collection_refuse(self):
        self.fixture.send("changed-during-collection")
        native = epoch_carry.ArchiveProofResolver.source
        changed = []
        def mutate(resolver, *args, **kwargs):
            source = native(resolver, *args, **kwargs)
            if not changed:
                path = self.root.path / "events.jsonl"
                path.write_bytes(path.read_bytes() + b"\n")
                changed.append(True)
            return source
        with patch.object(epoch_carry.ArchiveProofResolver, "source", mutate):
            with self.assertRaises(IntegrityFailure):
                self.collect()

    def test_exclusive_guard_is_required_but_grants_no_issuance_or_write(self):
        self.fixture.send("guard-control")
        with self.assertRaises(ProtocolRefusal):
            archive_checkpoint.collect_projection_sources(self.root)
        with bus_epoch.epoch_guard(self.root):
            with self.assertRaises(ProtocolRefusal):
                archive_checkpoint.collect_projection_sources(self.root)

    def test_reserved_prepare_is_collected_without_materializing_a_frame(self):
        self.fixture.send("reserved-before-append")
        with patch.object(CursorPresentationLedger, "_append_reserved", side_effect=OSError("fixture append fault")):
            with self.assertRaises(OSError):
                self.fixture.presentation()
        path = self.root.path / CursorPresentationLedger.relative("seat-a")
        self.assertEqual(b"", path.read_bytes())
        before = self.snapshot()
        entries = self.presentations()["entries"]
        self.assertEqual(1, len(entries))
        self.assertEqual(entries[0]["witness"]["reserved_record"], entries[0]["records"][0])
        self.assertEqual(0, entries[0]["physical_byte_length"])
        self.assertEqual(before, self.snapshot())

    def test_changed_presentation_cut_cannot_borrow_a_valid_archive(self):
        self.fixture.send("invalid-prepared-cut")
        _binding, _consent, _classification, prepared = self.fixture.presentation()
        path = self.root.path / CursorPresentationLedger.relative("seat-a")
        witness_path = self.root.path / CursorPresentationLedger.witness_relative("seat-a")
        changed = dict(prepared, event_prefix_digest="f" * 64)
        path.write_bytes(encode_frame(changed))
        witness = json.loads(witness_path.read_bytes())
        witness["reserved_record"] = changed
        witness_path.write_text(json.dumps(witness, sort_keys=True) + "\n")
        bus_epoch.roll_bus_epoch(self.root, actor="sender", idempotency_key="byte-valid-presentation-input")
        with self.assertRaises(IntegrityFailure):
            self.presentations()

    def test_disappearing_presentation_sources_are_never_reinitialized_by_collection(self):
        self.fixture.send("disappearing-presentation")
        self.fixture.presentation()
        path = self.root.path / CursorPresentationLedger.relative("seat-a")
        witness = self.root.path / CursorPresentationLedger.witness_relative("seat-a")
        native = CursorPresentationLedger._load
        removed = []
        def disappear(plane, *args, **kwargs):
            if not removed:
                path.unlink()
                witness.unlink()
                removed.append(True)
            return native(plane, *args, **kwargs)
        with patch.object(CursorPresentationLedger, "_load", disappear):
            with self.assertRaises(IntegrityFailure):
                self.presentations()
        self.assertFalse(path.exists(), "read-only collection recreated the missing ledger")
        self.assertFalse(witness.exists(), "read-only collection recreated the missing witness")

    def test_symlinked_witness_parent_refuses_before_listing_foreign_metadata(self):
        self.fixture.send("witness-parent-alias")
        self.fixture.presentation()
        state = self.root.path / "state"
        displaced = self.fixture.base / "foreign-state"
        state.rename(displaced)
        state.symlink_to(displaced, target_is_directory=True)
        listed = []
        native = Path.iterdir
        def observe(path):
            if path == state / "cursor-wait":
                listed.append(path)
            return native(path)
        with patch.object(Path, "iterdir", observe):
            with self.assertRaises((IntegrityFailure, ProtocolRefusal)):
                self.presentations()
        self.assertEqual([], listed, "collector listed metadata through a foreign state ancestor")

    def test_reserved_terminal_keeps_prepared_uncertainty_without_materialization(self):
        self.fixture.send("reserved-terminal")
        native = CursorPresentationLedger._append_reserved
        def fail_terminal(plane, node, data, row):
            if row["phase"] == "flushed":
                raise OSError("fixture terminal append fault")
            return native(plane, node, data, row)
        with patch.object(CursorPresentationLedger, "_append_reserved", fail_terminal):
            with self.assertRaises(OSError):
                self.fixture.presentation(flushed=True)
        before = self.snapshot()
        entries = self.presentations()["entries"]
        self.assertEqual(1, len(entries))
        self.assertEqual(["prepared"], [row["phase"] for row in entries[0]["records"]])
        self.assertEqual("flushed", entries[0]["witness"]["reserved_record"]["phase"])
        self.assertEqual(1, len(entries[0]["groups"][0]["records"]))
        self.assertEqual(before, self.snapshot())

    def test_off_lineage_completed_roll_stays_retry_testimony_only(self):
        self.fixture.send("original-roll-payload")
        first = bus_epoch.roll_bus_epoch(self.root, actor="sender", idempotency_key="off-lineage-key")
        message = self.fixture.send("new-genesis-fixture")
        # Native completed-roll scanning intentionally includes physical first
        # frames outside today's lineage. This fixture relocates that exact
        # native frame while leaving its real receipt-owned archive untouched.
        orphan = self.root.path / "archive-off-lineage-fixture"
        orphan.mkdir()
        (orphan / "events.jsonl").write_bytes(encode_frame(first["receipt"]))
        (self.root.path / "events.jsonl").write_bytes(encode_frame(dict(message["message"])))
        self.assertTrue(bus_epoch.roll_bus_epoch(self.root, actor="sender",
            idempotency_key="off-lineage-key")["no_op"])
        self.assertEqual([], self.collect()["epochs"][1:])
        before = self.snapshot()
        entries = self.retries()["entries"]
        self.assertEqual(1, len(entries))
        self.assertEqual(first["receipt"], entries[0]["receipt"])
        self.assertEqual(first["receipt"]["archive_sha256"], entries[0]["facts"]["archive_sha256"])
        self.assertEqual(before, self.snapshot())

    def test_duplicate_completed_roll_key_refuses_instead_of_deduplicating(self):
        self.fixture.send("duplicate-roll-key")
        rolled = bus_epoch.roll_bus_epoch(self.root, actor="sender", idempotency_key="duplicated-key")
        orphan = self.root.path / "archive-duplicate-key-fixture"
        orphan.mkdir()
        (orphan / "events.jsonl").write_bytes(encode_frame(rolled["receipt"]))
        with self.assertRaises(IntegrityFailure) as refused:
            self.retries()
        self.assertEqual("epoch_receipt_duplicate", refused.exception.code)

    def test_roll_retry_collection_cannot_admit_damaged_payload_facts(self):
        self.fixture.send("damaged-retry-payload")
        rolled = bus_epoch.roll_bus_epoch(self.root, actor="sender", idempotency_key="damaged-key")
        events = Path(rolled["receipt"]["archive_path"]) / "events.jsonl"
        events.write_bytes(events.read_bytes().replace(b"native carry fixture", b"native carry fixturf"))
        with self.assertRaises(IntegrityFailure):
            self.retries()

    def test_future_record_kind_is_preserved_by_blocking_retirement_planning(self):
        message = self.fixture.send("future-native-contract")
        future = {"schema_version": 7, "kind": "future_checkpoint_sensor",
                  "id": "future-" + uuid7_hex(), "tenant_id": self.root.tenant_id,
                  "timestamp": message["timestamp"], "opaque_future_value": {"sensor": 3}}
        path = self.root.path / "events.jsonl"
        path.write_bytes(path.read_bytes() + encode_frame(future))
        _known, unrecognized = EventLog(self.root).compatible_event_records(snapshot=True)
        self.assertEqual(1, len(unrecognized))
        with self.assertRaises((ProtocolRefusal, IntegrityFailure)) as refused:
            self.collect()
        self.assertEqual("archive_checkpoint_unsupported", refused.exception.code)


if __name__ == "__main__":
    unittest.main()

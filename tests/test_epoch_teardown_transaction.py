"""Teardown testimony shares the automatic roll's native durability boundary.

Authored before the transaction changes. Execution is held until the lane's
authenticated RESUME and integrated native record/domain implementation.
"""

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from floati import bus_epoch, epoch_carry, epoch_rotation, epoch_teardown
from floati.errors import ProtocolRefusal
from floati.events import EventLog
from floati.framing import decode_frames, encode_frame
from floati.ids import uuid7_hex
from floati.registry import Registry
from floati.root import FloatiRoot
from tests.temp_roots import REAL_TEMP_ROOT


class EpochTeardownTransactionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
        self.addCleanup(temporary.cleanup)
        self.root = FloatiRoot.open_direct_home(Path(temporary.name) / "fleet", create=True)
        for node in ("actor", "seat"):
            Registry(self.root).register(node, "Codex")
        self.message = EventLog(self.root).send("actor", "seat", "floati", "a" * 40,
            "AGENTS.md", "pending native transaction", idempotency_key="pending")
        self.events = self.root.path / "events.jsonl"
        self.before = self.events.read_bytes()
        self.seed = {"teardown_receipt_id": "seat-teardown-" + uuid7_hex(),
            "teardown_context_sha256": "2" * 64,
            "report_envelope_id": None, "report_envelope_sha256": None,
            "verification": {"state": "verified", "failure_code": None, "remedy": None}}

    def planner(self, root, old):
        self.assertEqual(self.root, root)
        return old, {"trigger": "send", "policy": {"max_bytes": 1,
            "max_age_days": 7, "source": "operator_declared"},
            "observed_bytes": len(self.before),
            "observed_at": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "epoch_started_at": None, "due_reasons": ["bytes"]}, (self.seed,)

    def roll(self, **kwargs):
        return bus_epoch.roll_automatic_epoch(self.root, actor="actor", planner=self.planner, **kwargs)

    def assert_binding(self, receipt):
        rows = decode_frames(self.events.read_bytes())
        self.assertEqual(receipt, rows[0])
        proofs = [row for row in rows if row["kind"] == "seat_teardown_epoch_verification"]
        self.assertEqual(1, len(proofs))
        proof = proofs[0]
        self.assertEqual(receipt["id"], proof["roll_receipt_id"])
        self.assertEqual(hashlib.sha256(encode_frame(receipt)).hexdigest(), proof["roll_receipt_sha256"])
        self.assertEqual(receipt["epoch_id"], proof["outgoing_epoch_id"])
        self.assertEqual(self.before, b"".join(encode_frame(row) for row in rows[1:]
            if row["kind"] not in {"seat_teardown_epoch_verification", "epoch_carry_verification"}))
        seed = {key: proof[key] for key in (*self.seed, "outgoing_epoch_id")}
        canonical = json.dumps([seed], ensure_ascii=False, allow_nan=False,
            sort_keys=True, separators=(",", ":")).encode("utf-8")
        digest = hashlib.sha256(b"floati-seat-teardown-epoch-seeds-v1\0" + canonical).hexdigest()
        self.assertEqual({"count": 1, "digest": digest}, receipt["automatic"]["teardown_carry"])
        self.assertEqual([self.message["id"]], EventLog(self.root).unacked_ids("seat"))
        self.assertEqual(self.before, (Path(receipt["archive_path"]) / "events.jsonl").read_bytes())
        return proof

    def test_optional_seeds_bind_native_receipt_without_changing_retained_frames(self):
        self.assert_binding(self.roll()["receipt"])

    def test_legacy_two_element_planner_omits_teardown_commitment(self):
        result = bus_epoch.roll_automatic_epoch(self.root, actor="actor",
            planner=lambda root, old: self.planner(root, old)[:2])
        self.assertNotIn("teardown_carry", result["receipt"]["automatic"])
        rows = decode_frames(self.events.read_bytes())
        self.assertEqual(result["receipt"], rows[0])
        self.assertFalse(any(row["kind"] == "seat_teardown_epoch_verification" for row in rows))
        self.assertEqual(self.before, b"".join(encode_frame(row) for row in rows[1:]
            if row["kind"] != "epoch_carry_verification"))

    def test_collision_rebinds_frozen_seed_to_new_full_receipt(self):
        saved = {}
        def fault(point):
            if point == "before_archive_staging":
                marker = bus_epoch.observe_epoch_roll_state(self.root)["marker"]
                collision = Path(marker["archive_path"])
                collision.mkdir()
                (collision / "foreign").write_bytes(b"preserve foreign collision")
                saved["collision"] = collision
                saved["receipt"] = marker["receipt"]
                self.seed["verification"]["state"] = "failed"
                self.seed["teardown_context_sha256"] = "3" * 64
        result = self.roll(fault=fault)
        proof = self.assert_binding(result["receipt"])
        self.assertEqual("verified", proof["verification"]["state"])
        self.assertEqual("2" * 64, proof["teardown_context_sha256"])
        self.assertNotEqual(saved["receipt"]["archive_path"], result["receipt"]["archive_path"])
        self.assertNotEqual(hashlib.sha256(encode_frame(saved["receipt"])).hexdigest(),
                            proof["roll_receipt_sha256"])
        self.assertEqual(b"preserve foreign collision", (saved["collision"] / "foreign").read_bytes())

    def test_committed_recovery_installs_exact_frozen_proof_without_reverification(self):
        saved = {}
        def fault(point):
            if point == "marker_committed_durable":
                marker = bus_epoch.observe_epoch_roll_state(self.root)["marker"]
                saved["target"] = (Path(marker["staging_path"]) / "events.jsonl").read_bytes()
                raise RuntimeError("interrupt before live replacement")
        with self.assertRaisesRegex(RuntimeError, "interrupt before live replacement"):
            self.roll(fault=fault)
        with mock.patch.object(epoch_teardown, "collect_verifications", side_effect=AssertionError("recollected")),\
             mock.patch.object(epoch_teardown, "finalize_verifications", side_effect=AssertionError("refinalized")),\
             mock.patch.object(epoch_teardown, "verification_frames", side_effect=AssertionError("regenerated")):
            result = bus_epoch.reconcile_epoch_roll(self.root)
        self.assertEqual(saved["target"], self.events.read_bytes())
        self.assert_binding(result["receipt"])

    def assert_capacity_refusal(self, name, bound, code):
        with mock.patch("floati.jsonl." + name, bound):
            with self.assertRaises(ProtocolRefusal) as failure:
                self.roll()
        self.assertEqual(code, failure.exception.code)
        self.assertEqual(self.before, self.events.read_bytes())
        self.assertEqual([], list(self.root.path.glob("archive-*")))
        self.assertEqual([], list(self.root.path.glob(".floati-epoch-*")))

    def test_new_proof_counts_against_physical_record_capacity_before_marker(self):
        self.assert_capacity_refusal("MAX_LEDGER_RECORDS", self.before.count(b"\n") + 1,
                                     "ledger_record_limit")

    def test_new_proof_counts_against_byte_capacity_before_marker(self):
        native_frames = epoch_teardown.verification_frames
        bounds = []
        def proof_and_bound(root, seeds, receipt):
            frames = native_frames(root, seeds, receipt)
            self.assertTrue(frames)
            # Anchor and retained frames fit exactly; only the additional
            # native testimony crosses the bound.
            bound = len(encode_frame(receipt)) + len(self.before)
            capacity = mock.patch("floati.jsonl.MAX_LEDGER_BYTES", bound)
            capacity.start()
            self.addCleanup(capacity.stop)
            bounds.append(bound)
            return frames
        with mock.patch.object(epoch_teardown, "verification_frames", side_effect=proof_and_bound):
            with self.assertRaises(ProtocolRefusal) as failure:
                self.roll()
        self.assertEqual("ledger_too_large", failure.exception.code)
        self.assertEqual(1, len(bounds))
        self.assertEqual(self.before, self.events.read_bytes())
        self.assertEqual([], list(self.root.path.glob("archive-*")))
        self.assertEqual([], list(self.root.path.glob(".floati-epoch-*")))

    def test_prior_proof_is_excluded_from_carry_and_nonreclaimable_comparison(self):
        self.roll()
        current = (("events.jsonl", "events", self.events.read_bytes()),)
        with bus_epoch.epoch_guard(self.root, exclusive=True):
            carry = epoch_carry.build_carry_plan(self.root, current)
        self.assertEqual(self.before, dict((relative, raw) for relative, _plane, raw in carry.live_payloads)["events.jsonl"])
        self.assertEqual(epoch_rotation._without_anchors(current),
                         epoch_rotation._without_anchors(carry.live_payloads))

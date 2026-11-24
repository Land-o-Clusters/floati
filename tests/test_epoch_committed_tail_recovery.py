"""COMMITTED recovery must preserve extended live mail and durably receipt rollback."""

import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import unittest
from unittest import mock

from floati import bus_epoch
from floati.bus_epoch import observe_epoch_roll_state, reconcile_epoch_roll, roll_bus_epoch
from floati.cursor import SparseCursor
from floati.errors import IntegrityFailure
from floati.events import EventLog
from floati.framing import decode_frames, encode_frame
from tests import test_epoch_roll_multi_recipient as composition


class EpochCommittedTailRecoveryTests(unittest.TestCase):
    """Catch destructive roll-forward and unreceipted/unsafe rollback decisions."""

    def setUp(self):
        # Compose the isolated fixture without importing its TestCase as a class
        # or subclassing it: its four existing tests must not be rediscovered.
        self.fixture = composition.MultiRecipientEpochRollTests()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()
        self.root = self.fixture.root
        self.maxDiff = 4000

    @staticmethod
    def identity(status):
        return (status.st_dev, status.st_ino, status.st_mode, status.st_nlink,
                status.st_size, status.st_mtime_ns, status.st_ctime_ns)

    def inventory(self):
        """Exact owned-tree bytes/membership/identities; ignore read atime."""
        result = {}
        for path in (self.root.path, *sorted(self.root.path.rglob("*"))):
            relative = path.relative_to(self.root.path).as_posix()
            status = path.lstat()
            if stat.S_ISREG(status.st_mode):
                result[relative] = ("file", path.read_bytes(), self.identity(status))
            elif stat.S_ISDIR(status.st_mode):
                result[relative] = ("directory", self.identity(status))
            else:
                self.fail("test-owned fixture has a nonordinary entry: " + relative)
        return result

    def selected(self):
        return {
            path.relative_to(self.root.path).as_posix():
                (path.read_bytes(), self.identity(path.lstat()))
            for path in self.root.path.rglob("*.jsonl")
            if path == self.root.path / "events.jsonl"
            or path.relative_to(self.root.path).parts[:2] in (
                ("receipts", "deliveries"), ("receipts", "acks"),
            )
        }

    def live_hash(self, selected):
        """Independent canonical selected-facts digest, including physical paths."""
        rows = []
        for relative, (payload, _identity) in sorted(selected.items()):
            plane = "events" if relative == "events.jsonl" else Path(relative).parts[1]
            rows.append({"path": relative, "plane": plane, "byte_length": len(payload),
                         "sha256": hashlib.sha256(payload).hexdigest()})
        canonical = b"".join(
            json.dumps(row, ensure_ascii=False, allow_nan=False, sort_keys=True,
                       separators=(",", ":")).encode("utf-8") + b"\n"
            for row in rows
        )
        return hashlib.sha256(canonical).hexdigest()

    def interrupt_committed(self, boundary="marker_committed_durable"):
        seen = []

        def interrupt(point):
            seen.append(point)
            if point == boundary:
                raise RuntimeError("test-owned interruption at " + point)

        with self.assertRaisesRegex(RuntimeError, "test-owned interruption at " + boundary):
            roll_bus_epoch(self.root, actor="architect", idempotency_key="committed-tail",
                           fault=interrupt)
        self.assertEqual(boundary, seen[-1])
        observed = observe_epoch_roll_state(self.root)
        self.assertEqual("COMMITTED", observed["classification"])
        return observed

    def extend_live(self):
        # Reviewed installed writers predate pending-marker admission. Keep
        # their native schemas/send/present/ACK behavior to reproduce surviving
        # mail. Immediate __wrapped__ calls omit automatic maintenance while
        # retaining the native SH wrappers; bypass only marker admission here.
        # Recovery itself never runs under this fixture-only patch.
        with mock.patch("floati.bus_epoch._require_epoch_writer_admission", return_value=None):
            return self._extend_live_from_reviewed_installed_writer()

    def _extend_live_from_reviewed_installed_writer(self):
        proofs = {}
        for node in ("seat-a", "seat-b"):
            sent = EventLog.send.__wrapped__(EventLog(self.root),
                "architect", node, "floati", "b" * 40, "docs/evidence/ping.md",
                "mail after committed interruption", idempotency_key="tail-" + node,
            )
            self.assertIn(sent["id"], EventLog(self.root).unacked_ids(node))
            rows, delivery = EventLog.present.__wrapped__(EventLog(self.root), node)
            self.assertEqual([sent["id"]], [row["id"] for row in rows])
            acknowledgment = SparseCursor.ack.__wrapped__(SparseCursor(self.root),
                node, [sent["id"]], acting_session_id="postcommit-tail",
            )
            self.assertEqual([], EventLog(self.root).unacked_ids(node))
            proofs.setdefault("events.jsonl", []).append(encode_frame(sent["message"]))
            proofs["receipts/deliveries/" + node + ".jsonl"] = [encode_frame(delivery)]
            proofs["receipts/acks/" + node + ".jsonl"] = [encode_frame(acknowledgment)]
        for relative, frames in proofs.items():
            for frame in frames:
                self.assertIn(frame, (self.root.path / relative).read_bytes(), relative)
        return proofs

    def receipt_path(self, observed):
        return (self.root.path / "receipts" / "epoch-recovery" /
                (observed["request_id"] + ".jsonl"))

    def read_receipt(self, observed):
        path = self.receipt_path(observed)
        self.assertTrue(path.is_file(), "rollback needs a durable recovery receipt")
        records = decode_frames(path.read_bytes())
        self.assertEqual(1, len(records), "one original-request recovery receipt")
        return records[0]

    def assert_receipt(self, observed, selected_at_decision, receipt):
        original = observed["marker"]["receipt"]
        request = observed["marker"]["request"]
        expected = {
            "schema_version": 1, "kind": "bus_epoch_recovery_receipt",
            "tenant_id": self.root.tenant_id, "actor": request["actor"],
            "idempotency_key": request["idempotency_key"],
            "request_id": observed["request_id"], "roll_receipt_id": original["id"],
            "direction": "rollback", "reason_code": "live_epoch_extended",
            "archive_path": original["archive_path"],
            "archive_sha256": original["archive_sha256"],
            "observed_live_sha256": self.live_hash(selected_at_decision),
        }
        for field, value in expected.items():
            self.assertEqual(value, receipt[field], field)
        self.assertRegex(receipt["id"], r"^bus-epoch-recovery-[0-9a-f]{12}7[0-9a-f]{3}[89ab][0-9a-f]{15}$")

    def assert_cleanup_preserves_controls(self, before, observed):
        after = self.inventory()
        marker = observed["marker"]
        removed_roots = {
            Path(observed["marker_path"]).relative_to(self.root.path).as_posix(),
            Path(marker["archive_path"]).relative_to(self.root.path).as_posix(),
            Path(marker["staging_path"]).relative_to(self.root.path).as_posix(),
        }
        receipt_relative = self.receipt_path(observed).relative_to(self.root.path)
        allowed_new = {receipt_relative.as_posix(), receipt_relative.as_posix() + ".lock"}
        allowed_new.update(parent.as_posix() for parent in receipt_relative.parents)
        for removed in removed_roots:
            self.assertFalse((self.root.path / removed).exists(), removed)
        affected_parents = {
            parent.as_posix()
            for path in (*removed_roots, receipt_relative.as_posix())
            for parent in Path(path).parents
        } | {"."}
        for relative, entry in before.items():
            if any(relative == removed or relative.startswith(removed + "/")
                   for removed in removed_roots):
                continue
            self.assertIn(relative, after, relative)
            if entry[0] == "file":
                self.assertEqual(entry, after[relative], relative)
            else:
                # Cleanup/new receipt changes necessary ancestor directory times,
                # size and link count; no directory may be replaced or chmodded.
                self.assertEqual(entry[1][:3], after[relative][1][:3], relative)
                affected = relative in affected_parents
                if not affected:
                    self.assertEqual(entry, after[relative], relative)
        self.assertLessEqual(set(after) - set(before), allowed_new)
        return after

    def assert_refused_unchanged(self):
        before = self.inventory()
        try:
            with self.assertRaises(IntegrityFailure) as caught:
                reconcile_epoch_roll(self.root)
            self.assertEqual("epoch_recovery_live_changed", caught.exception.code)
        finally:
            self.assertEqual(before, self.inventory(), "unsafe recovery changed owned evidence")

    def test_committed_extended_native_mail_rolls_back_receipted_without_selected_mutation(self):
        observed = self.interrupt_committed()
        archive = Path(observed["marker"]["archive_path"])
        self.assertEqual(
            set(self.fixture.selected),
            {path.relative_to(archive).as_posix()
             for path in archive.rglob("*") if path.is_file()},
        )
        for relative, payload in self.fixture.selected.items():
            self.assertEqual(payload, (archive / relative).read_bytes(), relative)
        proofs = self.extend_live()
        for relative, frames in proofs.items():
            for frame in frames:
                self.assertNotIn(frame, (archive / relative).read_bytes(), relative)
        selected_before, before = self.selected(), self.inventory()
        result = reconcile_epoch_roll(self.root)
        # Check native data preservation before result shape, so baseline RED
        # demonstrates actual tail loss rather than merely a missing new kind.
        self.assertEqual(selected_before, self.selected(), "recovery replaced live postcommit mail")
        self.assertEqual(("COMMITTED", "rollback"),
                         (result["classification"], result["direction"]))
        receipt = self.read_receipt(observed)
        self.assert_receipt(observed, selected_before, receipt)
        self.assertEqual(receipt, result["receipt"])
        stable = self.assert_cleanup_preserves_controls(before, observed)
        self.assertIsNone(reconcile_epoch_roll(self.root))
        self.assertEqual(stable, self.inventory(), "second reconciliation must be an exact no-op")

    def test_receipt_is_durable_before_prepared_decision_and_reused_on_retry(self):
        observed = self.interrupt_committed()
        self.extend_live()
        selected_before = self.selected()
        marker_path = Path(observed["marker_path"])
        marker_before = marker_path.read_bytes()
        real_rewrite, real_fsync = bus_epoch._rewrite_bound_file, os.fsync
        synced = set()

        def record_sync(descriptor):
            result = real_fsync(descriptor)
            status = os.fstat(descriptor)
            synced.add((status.st_dev, status.st_ino))
            return result

        def interrupt_decision(path, before, after, **kwargs):
            if Path(path) == marker_path and json.loads(after)["state"] == "PREPARED":
                receipt = self.read_receipt(observed)
                self.assert_receipt(observed, selected_before, receipt)
                for durable_path in (self.receipt_path(observed), self.receipt_path(observed).parent):
                    status = durable_path.stat()
                    self.assertIn((status.st_dev, status.st_ino), synced)
                self.assertEqual(marker_before, marker_path.read_bytes())
                raise RuntimeError("test-owned before PREPARED decision")
            return real_rewrite(path, before, after, **kwargs)

        with mock.patch("floati.bus_epoch.os.fsync", side_effect=record_sync):
            with mock.patch("floati.bus_epoch._rewrite_bound_file", side_effect=interrupt_decision):
                with self.assertRaisesRegex(RuntimeError, "before PREPARED decision"):
                    reconcile_epoch_roll(self.root)
        self.assertEqual(selected_before, self.selected())
        self.assertEqual("COMMITTED", observe_epoch_roll_state(self.root)["classification"])
        receipt_bytes = self.receipt_path(observed).read_bytes()
        before = self.inventory()
        result = reconcile_epoch_roll(self.root)
        self.assertEqual(("COMMITTED", "rollback"),
                         (result["classification"], result["direction"]))
        self.assertEqual(selected_before, self.selected())
        self.assertEqual(receipt_bytes, self.receipt_path(observed).read_bytes())
        self.assertEqual(self.read_receipt(observed), result["receipt"])
        self.assert_cleanup_preserves_controls(before, observed)

    def test_prepared_cleanup_interruption_resumes_without_duplicate_recovery_receipt(self):
        observed = self.interrupt_committed()
        self.extend_live()
        selected_before = self.selected()
        archive = Path(observed["marker"]["archive_path"])
        real_remove, real_fsync = bus_epoch._remove_new_tree, os.fsync
        synced = []

        def record_sync(descriptor):
            result = real_fsync(descriptor)
            status = os.fstat(descriptor)
            synced.append((status.st_dev, status.st_ino))
            return result

        def interrupt_after_archive(root, path, **kwargs):
            if Path(path) == archive:
                marker_status = Path(observed["marker_path"]).stat()
                root_status = self.root.path.stat()
                marker_key = (marker_status.st_dev, marker_status.st_ino)
                root_key = (root_status.st_dev, root_status.st_ino)
                self.assertIn(marker_key, synced)
                self.assertIn(root_key, synced)
                self.assertGreater(
                    max(index for index, key in enumerate(synced) if key == root_key),
                    max(index for index, key in enumerate(synced) if key == marker_key),
                    "PREPARED marker and root must be synced before archive cleanup",
                )
            result = real_remove(root, path, **kwargs)
            if Path(path) == archive:
                self.assertEqual("PREPARED", observe_epoch_roll_state(self.root)["classification"])
                raise RuntimeError("test-owned after rollback archive removal")
            return result

        with mock.patch("floati.bus_epoch.os.fsync", side_effect=record_sync):
            with mock.patch("floati.bus_epoch._remove_new_tree", side_effect=interrupt_after_archive):
                with self.assertRaisesRegex(RuntimeError, "after rollback archive removal"):
                    reconcile_epoch_roll(self.root)
        self.assertEqual(selected_before, self.selected())
        self.assertFalse(archive.exists())
        self.assertEqual("PREPARED", observe_epoch_roll_state(self.root)["classification"])
        receipt = self.read_receipt(observed)
        self.assert_receipt(observed, selected_before, receipt)
        receipt_bytes = self.receipt_path(observed).read_bytes()
        before = self.inventory()
        result = reconcile_epoch_roll(self.root)
        self.assertEqual(("PREPARED", "rollback"),
                         (result["classification"], result["direction"]))
        self.assertEqual(selected_before, self.selected())
        self.assertEqual(receipt_bytes, self.receipt_path(observed).read_bytes())
        stable = self.assert_cleanup_preserves_controls(before, observed)
        self.assertIsNone(reconcile_epoch_roll(self.root))
        self.assertEqual(stable, self.inventory())

    def test_same_size_rewrite_with_restored_mtime_is_refused_without_mutation(self):
        self.interrupt_committed()
        path = self.root.path / "events.jsonl"
        original, status = path.read_bytes(), path.stat()
        changed = original.replace(b"historical message", b"historical messagf", 1)
        self.assertNotEqual(original, changed)
        self.assertEqual(len(original), len(changed))
        path.write_bytes(changed)
        os.utime(path, ns=(status.st_atime_ns, status.st_mtime_ns))
        self.assert_refused_unchanged()

    def test_unsafe_recovery_does_not_create_missing_wake_lock_paths(self):
        self.interrupt_committed()
        path = self.root.path / "events.jsonl"
        original = path.read_bytes()
        changed = original.replace(b"historical message", b"historical messagf", 1)
        self.assertNotEqual(original, changed)
        path.write_bytes(changed)
        # Older surviving transactions can predate these coordination paths.
        # No child or lock owner survives this fixture's fault interruption.
        shutil.rmtree(self.root.path / "receipts" / "wake-coordination")
        self.assert_refused_unchanged()

    def test_empty_archive_receipt_plus_native_tail_is_ambiguous_and_refused(self):
        # An empty archive prefixes every live ledger. Native replacement plus
        # new mail must not be mistaken for a wholly unreplaced extended epoch.
        for relative in self.fixture.selected:
            (self.root.path / relative).write_bytes(b"")
        observed = self.interrupt_committed(boundary="live_events_synced")
        marker_frame = encode_frame(dict(observed["marker"]["receipt"]))
        self.assertEqual(marker_frame, (self.root.path / "events.jsonl").read_bytes())
        self.extend_live()
        live = (self.root.path / "events.jsonl").read_bytes()
        self.assertTrue(live.startswith(marker_frame))
        self.assertGreater(len(live), len(marker_frame))
        self.assertFalse(self.receipt_path(observed).exists())
        self.assert_refused_unchanged()
        self.assertFalse(self.receipt_path(observed).exists())

    def test_escaped_receipt_id_plus_tail_is_refused_without_deleting_its_archive(self):
        for relative in self.fixture.selected:
            (self.root.path / relative).write_bytes(b"")
        observed = self.interrupt_committed(boundary="live_events_synced")
        receipt = dict(observed["marker"]["receipt"])
        path = self.root.path / "events.jsonl"
        encoded_id = "".join("\\u%04x" % ord(char) for char in receipt["id"])
        escaped = encode_frame(receipt).replace(
            json.dumps(receipt["id"]).encode("ascii"),
            ('"' + encoded_id + '"').encode("ascii"),
        ).replace(b'"id":', b'"\\u0069d":')
        self.assertNotIn(receipt["id"].encode("ascii"), escaped)
        self.assertEqual([receipt], decode_frames(escaped))
        path.write_bytes(escaped)
        self.extend_live()
        self.assert_refused_unchanged()


if __name__ == "__main__":
    unittest.main()

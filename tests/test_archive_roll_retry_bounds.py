"""Native completed retry capture is bounded before opaque payload retention."""
import hashlib
import os
from pathlib import Path
import shutil
import unittest
from unittest import mock

from floati import archive_checkpoint as checkpoint, bus_epoch, jsonl
from floati.errors import FloatiError, IntegrityFailure, ProtocolRefusal
from floati.framing import encode_frame
from tests import test_archive_checkpoint as native_fixture


class ArchiveRollRetryBoundsTests(unittest.TestCase):
    setUp = native_fixture.ArchiveCheckpointTests.setUp
    retries = native_fixture.ArchiveCheckpointTests.retries
    snapshot = native_fixture.ArchiveCheckpointTests.snapshot

    def prepared(self, *, off_lineage=False):
        self.fixture.send("retry-manual-source")
        first = bus_epoch.roll_bus_epoch(self.root, actor="sender", idempotency_key="retry-bounds-manual")["receipt"]
        self.fixture.send("retry-automatic-source")
        second = self.fixture.automatic_roll()["receipt"]
        if off_lineage:
            message = self.fixture.send("retry-new-genesis")
            orphan = self.root.path / "archive-off-lineage-bounds"
            orphan.mkdir()
            (orphan / "events.jsonl").write_bytes(encode_frame(second))
            (self.root.path / "events.jsonl").write_bytes(encode_frame(dict(message["message"])))
        return first, second

    def members(self, receipts):
        return [Path(row["archive_path"]) / name for row in receipts
                for name, plane, status in bus_epoch._epoch_archive_snapshot(Path(row["archive_path"])) if plane is not None]

    def prepared_variants(self, cases):
        receipts = self.prepared(off_lineage=True)
        bank = self.fixture.base / "retry-variant-template"
        shutil.copytree(self.root.path, bank)
        for index, case in enumerate(cases):
            if index:
                shutil.rmtree(self.root.path)
                shutil.copytree(bank, self.root.path)
            yield case, receipts

    def test_exact_manual_automatic_off_lineage_retry_census_and_no_initialization(self):
        first, second = self.prepared(off_lineage=True)
        expected = bus_epoch._completed_roll_receipts(self.root)
        before = self.snapshot()
        result = self.retries()
        self.assertEqual([row["id"] for row in expected], [row["receipt"]["id"] for row in result["entries"]])
        self.assertEqual({first["id"], second["id"]}, {row["receipt"]["id"] for row in result["entries"]})
        for entry in result["entries"]:
            self.assertEqual(entry["receipt"]["archive_sha256"], entry["facts"]["archive_sha256"])
            for member in entry["inventory"]:
                raw = (Path(entry["receipt"]["archive_path"]) / member["path"]).read_bytes()
                self.assertEqual((len(raw), hashlib.sha256(raw).hexdigest()), (member["byte_length"], member["sha256"]))
        self.assertEqual(before, self.snapshot())

    def test_aggregate_all_archives_bytes_refuse_before_first_payload_read(self):
        receipts = self.prepared(off_lineage=True)
        limit = len(self.header_candidates()) * 65537 + sum(
            path.stat().st_size for path in self.members(receipts)) - 1
        error = None
        with mock.patch.object(checkpoint, "MAX_CAPTURED_BYTES", limit), mock.patch.object(
                bus_epoch, "_read_bound_regular", side_effect=AssertionError("opaque read before aggregate preflight")):
            try:
                self.retries()
            except Exception as caught:
                error = caught
        self.assertIsInstance(error, ProtocolRefusal)
        self.assertEqual("archive_checkpoint_unsupported", error.code)

    def test_physical_record_quota_precedes_facts_hash_and_result_retention(self):
        self.prepared(off_lineage=True)
        error = None
        # Permit the independently charged header pass, so this remains an
        # opaque-payload physical-record control rather than a header refusal.
        header_records = sum(max(1, path.read_bytes()[:65536].count(b"\n"))
                             if path.is_file() else 1 for path in self.header_candidates())
        with mock.patch.object(checkpoint, "MAX_CAPTURED_RECORDS", header_records), mock.patch.object(
                bus_epoch, "_archive_facts_from_payloads", side_effect=AssertionError("facts before physical-record quota")):
            try:
                self.retries()
            except Exception as caught:
                error = caught
        self.assertIsInstance(error, ProtocolRefusal)
        self.assertEqual("archive_checkpoint_unsupported", error.code)

    def test_each_opaque_payload_is_read_once_for_facts_and_inventory(self):
        receipts = self.prepared(off_lineage=True)
        target = self.members(receipts)[0]
        status = target.stat()
        native = os.read
        read_bytes, header_phase = [], []
        native_headers = checkpoint._completed_roll_retry_receipts
        def headers(*args, **kwargs):
            header_phase.append(True)
            try:
                return native_headers(*args, **kwargs)
            finally:
                header_phase.pop()
        def observed(fd, count):
            raw = native(fd, count)
            if not header_phase and (os.fstat(fd).st_dev, os.fstat(fd).st_ino) == (status.st_dev, status.st_ino):
                read_bytes.append(len(raw))
            return raw
        with mock.patch.object(checkpoint, "_completed_roll_retry_receipts", headers), mock.patch.object(os, "read", observed):
            self.retries()
        self.assertEqual(status.st_size, sum(read_bytes), "admission and inventory reread the same opaque bytes")

    def test_growing_member_never_reads_beyond_captured_size_plus_one(self):
        receipts = self.prepared(off_lineage=True)
        target = self.members(receipts)[0]
        status = target.stat()
        native = os.read
        requests, observed_bytes, changed = [], [], []
        def observed(fd, count):
            if (os.fstat(fd).st_dev, os.fstat(fd).st_ino) == (status.st_dev, status.st_ino):
                requests.append(count)
                if not changed:
                    with target.open("ab") as output:
                        output.write(b"x" * 131072)
                    changed.append(True)
                raw = native(fd, count)
                observed_bytes.append(len(raw))
                return raw
            return native(fd, count)
        with mock.patch.object(os, "read", observed):
            with self.assertRaises(FloatiError):
                self.retries()
        self.assertTrue(changed)
        self.assertLessEqual(max(requests), status.st_size + 1)
        self.assertLessEqual(sum(observed_bytes), status.st_size + 1)

    def test_opaque_native_retry_does_not_inherit_live_ledger_byte_limit(self):
        self.prepared(off_lineage=True)
        with mock.patch.object(jsonl, "MAX_LEDGER_BYTES", 8):
            self.assertEqual(2, len(self.retries()["entries"]))

    def test_derived_hash_work_quota_precedes_native_facts(self):
        self.prepared(off_lineage=True)
        error = None
        with mock.patch.object(checkpoint, "MAX_DERIVED_CANONICAL_BYTES", self.header_decode_cost()), mock.patch.object(
                bus_epoch, "_archive_facts_from_payloads", side_effect=AssertionError("facts hash before derived quota")):
            try:
                self.retries()
            except Exception as caught:
                error = caught
        self.assertIsInstance(error, ProtocolRefusal)
        self.assertEqual("archive_checkpoint_unsupported", error.code)

    def test_damaged_retry_payload_still_refuses_native_facts(self):
        receipts = self.prepared(off_lineage=True)
        target = self.members(receipts)[0]
        raw = target.read_bytes()
        target.write_bytes(raw + b" ")
        with self.assertRaises(IntegrityFailure):
            self.retries()

    def test_empty_members_inventory_metadata_is_bounded_before_result_clone(self):
        self.fixture.send("retry-empty-metadata")
        parent = self.root.path / "receipts/acks/seat-a"
        parent.mkdir(parents=True, exist_ok=True)
        for ordinal in range(12):
            (parent / (format(ordinal, "064x") + ".jsonl")).write_bytes(b"")
        receipt = bus_epoch.roll_bus_epoch(self.root, actor="sender", idempotency_key="retry-empty-metadata")["receipt"]
        total_raw = sum(path.stat().st_size for path in self.members([receipt]))
        error = None
        with mock.patch.object(checkpoint, "MAX_DERIVED_CANONICAL_BYTES", self.header_decode_cost() + 2 * total_raw + 1), mock.patch.object(
                checkpoint, "_detached", side_effect=AssertionError("inventory metadata cloned before byte quota")):
            try:
                self.retries()
            except Exception as caught:
                error = caught
        self.assertIsInstance(error, ProtocolRefusal)
        self.assertEqual("archive_checkpoint_unsupported", error.code)

    def test_missing_receipt_owned_archive_keeps_native_typed_refusal(self):
        receipts = self.prepared(off_lineage=True)
        archive = Path(receipts[0]["archive_path"])
        archive.rename(archive.with_name("saved-native-retry-payload"))
        error = None
        try:
            self.retries()
        except Exception as caught:
            error = caught
        self.assertIsInstance(error, IntegrityFailure)
        self.assertEqual("epoch_receipt_archive_invalid", error.code)

    def assert_native_malformed_member_parity(self, receipt):
        from floati.epoch_history import _admit
        # Compare the actual original native admission contract, rather than
        # manufacturing a schema exception or merely asserting any refusal.
        with bus_epoch.epoch_guard(self.root, exclusive=True):
            with self.assertRaises(IntegrityFailure) as original:
                _admit(self.root, receipt)
        self.assertEqual("epoch_receipt_archive_invalid", original.exception.code)
        error = None
        try:
            self.retries()
        except Exception as caught:
            error = caught
        self.assertIsInstance(error, IntegrityFailure)
        self.assertEqual(original.exception.code, error.code)

    def test_out_of_family_member_keeps_original_native_refusal(self):
        for member_kind, (receipt, unused) in self.prepared_variants(
                ("out-of-family", "symlink", "hardlink", "special", "directory")):
            with self.subTest(case=member_kind):
                # Each refusal starts from untouched native manual/automatic archives.
                archive = Path(receipt["archive_path"])
                parent = archive / "receipts/acks"
                parent.mkdir(parents=True, exist_ok=True)
                if member_kind == "out-of-family":
                    (archive / "foreign.txt").write_bytes(b"out of selected family")
                elif member_kind == "symlink":
                    foreign = self.root.path / "foreign-retry-target"
                    foreign.write_bytes(b"foreign bytes")
                    (parent / "symlink.jsonl").symlink_to(foreign)
                elif member_kind == "hardlink":
                    os.link(archive / "events.jsonl", parent / "hardlink.jsonl")
                elif member_kind == "special":
                    os.mkfifo(parent / "special.jsonl", 0o600)
                else:
                    (archive / "misplaced.jsonl").mkdir()
                self.assert_native_malformed_member_parity(receipt)
                if member_kind == "symlink":
                    self.assertEqual(b"foreign bytes", foreign.read_bytes())

    def header_candidates(self):
        # These are the native census coordinates, including candidates whose
        # first frame is absent or is not a completed roll receipt.
        return [self.root.path / "events.jsonl"] + [
            child / "events.jsonl" for child in self.root.path.iterdir()
            if child.name.startswith("archive-") and child.is_dir()
            and not child.is_symlink()]

    def header_decode_cost(self):
        return len(self.header_candidates()) * 65537 + sum(
            len(encode_frame(row)) for row in bus_epoch._completed_roll_receipts(self.root))

    def assert_header_preflight_refusal(self, **limits):
        error = None
        with mock.patch.multiple(checkpoint, **limits), mock.patch.object(
                bus_epoch, "_completed_roll_receipts",
                side_effect=AssertionError("native header census before aggregate preflight")):
            try:
                self.retries()
            except Exception as caught:
                error = caught
        self.assertIsInstance(error, ProtocolRefusal)
        self.assertEqual("archive_checkpoint_unsupported", error.code)

    def test_header_aggregate_bytes_refuse_before_native_receipt_census(self):
        self.prepared(off_lineage=True)
        candidates = self.header_candidates()
        actual_header_bytes = sum(min(path.stat().st_size, 65537)
                                  for path in candidates if path.exists())
        self.assertGreater(actual_header_bytes, 0)
        # Native manual + automatic + physically off-lineage testimony is
        # present. Its header reads alone already exceed this scaled quota.
        self.assert_header_preflight_refusal(MAX_CAPTURED_BYTES=actual_header_bytes - 1)

    def test_empty_and_absent_header_candidates_reserve_full_readline_bound(self):
        self.prepared(off_lineage=True)
        for ordinal in range(6):
            archive = self.root.path / ("archive-empty-header-%02d" % ordinal)
            archive.mkdir()
            if ordinal % 2 == 0:
                (archive / "events.jsonl").write_bytes(b"")
        candidates = self.header_candidates()
        limit = (len(candidates) - 1) * 65537
        self.assertLess(sum(min(path.stat().st_size, 65537)
                            for path in candidates if path.exists()), limit)
        # A captured empty/missing header may grow/appear before native open;
        # min(captured_size, 65537) does not bound that actual read request.
        self.assert_header_preflight_refusal(MAX_CAPTURED_BYTES=limit)

    def test_header_potential_physical_records_refuse_before_native_census(self):
        self.prepared(off_lineage=True)
        candidates = self.header_candidates()
        self.assertGreater(len(candidates), 1)
        self.assert_header_preflight_refusal(MAX_CAPTURED_RECORDS=len(candidates) - 1)

    def test_new_candidate_after_witness_refuses_before_opening_its_header(self):
        from floati import epoch_carry
        native_witness, native_open = epoch_carry.carry_dependency_witness, Path.open
        for mutation, (first, second) in self.prepared_variants(
                ("new-candidate", "replace-captured", "stable")):
            with self.subTest(case=mutation):
                candidates = set(self.header_candidates())
                target = (self.root.path / "archive-new-after-witness/events.jsonl"
                          if mutation == "new-candidate" else self.root.path / "events.jsonl")
                changed, opened = [], []
                def witness(root):
                    result = native_witness(root)
                    if not changed and mutation != "stable":
                        if mutation == "new-candidate":
                            target.parent.mkdir()
                            target.write_bytes(encode_frame(second))
                        else:
                            temporary = self.root.path / "replacement-header"
                            temporary.write_bytes(encode_frame(second))
                            temporary.replace(target)
                        changed.append(True)
                    return result
                def observed(path, *args, **kwargs):
                    mode = args[0] if args else kwargs.get("mode", "r")
                    if "r" in mode and (path == target or path in candidates):
                        opened.append(path)
                        if mutation == "stable":
                            raise AssertionError("retry header opened through an unbound pathname")
                    return native_open(path, *args, **kwargs)
                with mock.patch.object(epoch_carry, "carry_dependency_witness", witness), mock.patch.object(
                        Path, "open", observed):
                    if mutation == "stable":
                        self.assertEqual(2, len(self.retries()["entries"]))
                    else:
                        with self.assertRaises(IntegrityFailure) as refused:
                            self.retries()
                        self.assertEqual("archive_checkpoint_invalid", refused.exception.code)
                self.assertEqual(mutation != "stable", bool(changed))
                self.assertFalse(opened, "uncaptured or replaced header bytes were read before refusal")

    def test_native_header_skip_and_regular_hardlink_selection_are_preserved(self):
        first, second = self.prepared(off_lineage=True)
        # The native first-frame census admits a regular hardlinked header.
        # Its receipt-owned payload is a different, ordinary native archive.
        existing = self.root.path / "archive-off-lineage-bounds/events.jsonl"
        unselected = self.root.path / "unselected-header-link"
        existing.rename(unselected)
        os.link(unselected, existing)
        for name in ("archive-missing-header", "archive-symlink-header", "archive-special-header"):
            (self.root.path / name).mkdir()
        (self.root.path / "archive-symlink-header/events.jsonl").symlink_to(unselected)
        os.mkfifo(self.root.path / "archive-special-header/events.jsonl", 0o600)
        expected = bus_epoch._completed_roll_receipts(self.root)
        self.assertEqual({first["id"], second["id"]}, {row["id"] for row in expected})
        result = self.retries()
        self.assertEqual([row["id"] for row in expected],
                         [entry["receipt"]["id"] for entry in result["entries"]])


if __name__ == "__main__":
    unittest.main()

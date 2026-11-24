"""Producer bounds over native roots, before read/copy/origin work."""
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from floati import archive_checkpoint, bus_epoch, epoch_carry, jsonl
from floati.errors import IntegrityFailure, ProtocolRefusal
from floati.cursor_presentations import CursorPresentationLedger
from floati.wake_hold import WakeHoldController
from tests import test_archive_checkpoint as native_fixture


class ArchiveProducerBoundsTests(unittest.TestCase):
    setUp = native_fixture.ArchiveCheckpointTests.setUp
    collect = native_fixture.ArchiveCheckpointTests.collect
    def test_imported_origin_depth_refuses_before_cold_lookup(self):
        self.fixture.send("bounded-origin")
        WakeHoldController(self.root).evaluate("seat-a", idempotency_key="origin", limit=1)
        # Preserve the pre-v3 byte-exact import whose archive depth is bounded.
        self.fixture.install_fixture_carry(self.fixture.plan().live_payloads)
        epoch_carry._FACT_CACHE.clear()
        epoch_carry._ORIGIN_CACHE.clear()
        with patch.object(archive_checkpoint, "MAX_ORIGIN_DEPTH", 0, create=True):
            with self.assertRaises(ProtocolRefusal) as refused:
                self.collect()
        self.assertEqual("archive_checkpoint_unsupported", refused.exception.code)

    def test_census_refuses_substituted_descriptor_before_listing(self):
        self.fixture.send("descriptor-census")
        self.fixture.presentation()
        foreign = self.fixture.base / "unrelated-census"
        foreign.mkdir()
        (foreign / "private.jsonl").write_text("foreign\n")
        descriptor = os.open(foreign, os.O_RDONLY | os.O_DIRECTORY)
        self.addCleanup(os.close, descriptor)
        native_open, native_listdir = os.open, os.listdir
        listed = []
        identity = os.fstat(descriptor)
        def substituted(path, flags, *args, **kwargs):
            if str(path) == "cursor-presentations":
                return os.dup(descriptor)
            return native_open(path, flags, *args, **kwargs)
        def observed(path):
            if isinstance(path, int) and os.fstat(path).st_ino == identity.st_ino:
                listed.append(True)
            return native_listdir(path)
        caught = None
        with bus_epoch.epoch_guard(self.root, exclusive=True):
            with patch.object(os, "open", substituted), patch.object(os, "listdir", observed):
                try:
                    archive_checkpoint._directory_entries(self.root, Path("receipts/cursor-presentations"))
                except Exception as error:
                    caught = error
        self.assertIsInstance(caught, ProtocolRefusal)
        self.assertEqual([], listed, "unbound foreign descriptor was listed")

    def test_capture_reads_at_most_identity_size_plus_one(self):
        self.fixture.send("bounded-read-size")
        path = self.root.tenant_home / "events.jsonl"
        expected = path.stat()
        native = os.read
        requests = []
        def observed(descriptor, count):
            if os.fstat(descriptor).st_ino == expected.st_ino:
                requests.append(count)
            return native(descriptor, count)
        with patch.object(os, "read", observed):
            self.collect()
        self.assertTrue(requests)
        self.assertLessEqual(max(requests), expected.st_size + 1)

    def test_archive_aggregate_capture_refuses_before_native_fact_replay(self):
        self.fixture.send("archive-capture-budget")
        self.fixture.automatic_roll()
        epoch_carry._FACT_CACHE.clear()
        epoch_carry._ORIGIN_CACHE.clear()
        caught = None
        with bus_epoch.epoch_guard(self.root, exclusive=True):
            selected = bus_epoch._preflight_selected_members(self.root)
            live_bytes = sum(identity[4] for _relative, _path, _plane, identity in selected
                             if identity is not None)
            with patch.object(archive_checkpoint, "MAX_CAPTURED_BYTES", live_bytes),\
                    patch.object(bus_epoch, "_archive_facts_from_payloads",
                        side_effect=AssertionError("archive fact replay preceded aggregate capture refusal")):
                try:
                    archive_checkpoint.collect_projection_sources(self.root)
                except Exception as error:
                    caught = error
        self.assertIsInstance(caught, ProtocolRefusal)
        self.assertEqual("archive_checkpoint_unsupported", caught.code)

    def test_captured_physical_record_bound_precedes_native_decode(self):
        self.fixture.send("capture-record-budget")
        caught = None
        with patch.object(archive_checkpoint, "MAX_CAPTURED_RECORDS", 0),\
                patch.object(epoch_carry, "_decode",
                    side_effect=AssertionError("native decode preceded captured record refusal")):
            try:
                self.collect()
            except Exception as error:
                caught = error
        self.assertIsInstance(caught, ProtocolRefusal)
        self.assertEqual("archive_checkpoint_unsupported", caught.code)

    def test_grown_presentation_is_refused_before_native_bulk_reread(self):
        self.fixture.send("presentation-capture-growth")
        self.fixture.presentation(flushed=True)
        relative = CursorPresentationLedger.relative("seat-a")
        path = self.root.tenant_home / relative
        read = archive_checkpoint._CollectionBudget.read_member
        native_bytes = CursorPresentationLedger._bytes
        changed, reread = [], []
        def grow(budget, candidate, status):
            data = read(budget, candidate, status)
            if candidate == path and not changed:
                with path.open("ab") as stream:
                    stream.write(b" " * 4096)
                changed.append(True)
            return data
        def observed(plane, coordinate, *args, **kwargs):
            if coordinate == relative and changed:
                reread.append(True)
                raise AssertionError("grown physical source entered native bulk reread")
            return native_bytes(plane, coordinate, *args, **kwargs)
        caught = None
        with patch.object(archive_checkpoint._CollectionBudget, "read_member", grow),\
                patch.object(CursorPresentationLedger, "_bytes", observed):
            try:
                with bus_epoch.epoch_guard(self.root, exclusive=True):
                    archive_checkpoint.collect_presentation_sources(self.root)
            except Exception as error:
                caught = error
        self.assertIsInstance(caught, (IntegrityFailure, ProtocolRefusal))
        self.assertEqual([], reread)

    def test_archive_child_substitution_refuses_before_foreign_listing(self):
        self.fixture.send("archive-census-substitution")
        WakeHoldController(self.root).evaluate("seat-a", idempotency_key="census-hold", limit=1)
        self.fixture.automatic_roll()
        epoch_carry._FACT_CACHE.clear()
        epoch_carry._ORIGIN_CACHE.clear()
        archive = next(self.root.tenant_home.glob("archive-*"))
        parent, child = archive / "receipts", archive / "receipts/deliveries"
        old = self.fixture.base / "displaced-archive-deliveries"
        foreign = self.fixture.base / "foreign-archive-deliveries"
        foreign.mkdir()
        (foreign / "private.jsonl").write_text("private foreign bytes\n")
        foreign_identity, parent_identity = foreign.stat(), parent.stat()
        native = os.scandir
        changed, listed = [], []
        def observed(coordinate):
            info = os.fstat(coordinate) if isinstance(coordinate, int) else os.stat(coordinate)
            if info.st_ino == parent_identity.st_ino and not changed:
                child.rename(old)
                foreign.rename(child)
                changed.append(True)
            if info.st_ino == foreign_identity.st_ino:
                listed.append(True)
            return native(coordinate)
        caught = None
        # Start substitution only after the native initial witness has finished.
        witness = epoch_carry.carry_dependency_witness
        def witnessed(root):
            result = witness(root)
            if not changed:
                patcher = patch.object(os, "scandir", observed)
                patcher.start()
                self.addCleanup(patcher.stop)
            return result
        with patch.object(epoch_carry, "carry_dependency_witness", witnessed):
            try:
                self.collect()
            except Exception as error:
                caught = error
        self.assertIsInstance(caught, (IntegrityFailure, ProtocolRefusal))
        self.assertEqual([], listed, "replacement archive directory was enumerated")

    def test_changed_bounded_decoder_invalidates_planner_exact_source_hit(self):
        self.fixture.send("planner-helper-contract")
        with bus_epoch.epoch_guard(self.root, exclusive=True):
            witness = epoch_carry.carry_dependency_witness(self.root)
            budget = archive_checkpoint._CollectionBudget(self.root, witness)
            live = budget.selected(bus_epoch._preflight_selected_members(self.root))
            resolver = epoch_carry.ArchiveProofResolver(self.root, frozen_payloads=live, _planning=budget)
            resolver.source(self.root.tenant_home, "seat-a")
            with patch.object(jsonl, "_decode_bounded_frames",
                    side_effect=IntegrityFailure("changed_bounded_decoder", "controlled changed decoder")):
                with self.assertRaises(IntegrityFailure):
                    resolver.source(self.root.tenant_home, "seat-a")

    def test_changed_bounded_decoder_invalidates_native_origin_cache_hit(self):
        self.fixture.send("origin-helper-contract")
        WakeHoldController(self.root).evaluate("seat-a", idempotency_key="helper-origin", limit=1)
        # A legacy imported hold must invalidate its sealed-origin cache hit;
        # the current automatic writer instead mints a native v3 carry.
        self.fixture.install_fixture_carry(self.fixture.plan().live_payloads)
        epoch_carry._FACT_CACHE.clear()
        epoch_carry._ORIGIN_CACHE.clear()
        with bus_epoch.epoch_guard(self.root, exclusive=True):
            live = bus_epoch._read_selected_epoch_payloads(self.root,
                bus_epoch._preflight_selected_members(self.root))
            first = epoch_carry.ArchiveProofResolver(self.root, frozen_payloads=live)
            source = first.source(self.root.tenant_home, "seat-a")
            epoch_carry.verified_hold_proofs(self.root, source, "seat-a", resolver=first)
            second = epoch_carry.ArchiveProofResolver(self.root, frozen_payloads=live)
            source = second.source(self.root.tenant_home, "seat-a")
            with patch.object(jsonl, "_decode_bounded_frames",
                    side_effect=IntegrityFailure("changed_bounded_decoder", "controlled changed decoder")):
                with self.assertRaises(IntegrityFailure):
                    epoch_carry.verified_hold_proofs(self.root, source, "seat-a", resolver=second)

    def test_presentation_account_owner_is_checked_before_capture(self):
        self.fixture.send("presentation-owner-parity")
        self.fixture.presentation(flushed=True)
        native_lstat, native_fstat, native_read = Path.lstat, os.fstat, os.read
        class ForeignOwner:
            def __init__(self, status):
                self.status = status
            def __getattr__(self, name):
                return os.getuid() + 1 if name == "st_uid" else getattr(self.status, name)
        for relative, fstat_only in ((relative, fstat_only)
                for relative in (CursorPresentationLedger.relative("seat-a"),
                                 CursorPresentationLedger.witness_relative("seat-a"))
                for fstat_only in (False, True)):
            with self.subTest(relative=relative, fstat_only=fstat_only):
                target = self.root.tenant_home / relative
                inode, reads = target.stat().st_ino, []
                def lstat(path):
                    status = native_lstat(path)
                    return ForeignOwner(status) if path == target and not fstat_only else status
                def fstat(descriptor):
                    status = native_fstat(descriptor)
                    return ForeignOwner(status) if status.st_ino == inode else status
                def read(descriptor, count):
                    if native_fstat(descriptor).st_ino == inode:
                        reads.append(count)
                    return native_read(descriptor, count)
                caught = None
                with patch.object(Path, "lstat", lstat), patch.object(os, "fstat", fstat),\
                        patch.object(os, "read", read):
                    try:
                        with bus_epoch.epoch_guard(self.root, exclusive=True):
                            archive_checkpoint.collect_presentation_sources(self.root)
                    except Exception as error:
                        caught = error
                self.assertIsInstance(caught, (IntegrityFailure, ProtocolRefusal))
                self.assertEqual([], reads, "foreign-owned presentation bytes were captured")

    def test_raw_source_byte_work_refuses_before_native_decode(self):
        self.fixture.send("raw-source-byte-work")
        with bus_epoch.epoch_guard(self.root, exclusive=True):
            witness = epoch_carry.carry_dependency_witness(self.root)
            budget = archive_checkpoint._CollectionBudget(self.root, witness)
            live = budget.selected(bus_epoch._preflight_selected_members(self.root))
            resolver = epoch_carry.ArchiveProofResolver(self.root, frozen_payloads=live, _planning=budget)
            caught = None
            with patch.object(archive_checkpoint, "MAX_DERIVED_CANONICAL_BYTES", 0),\
                    patch.object(epoch_carry, "_decode",
                        side_effect=AssertionError("native decoder entered before raw byte work bound")):
                try:
                    resolver.source(self.root.tenant_home, "seat-a")
                except Exception as error:
                    caught = error
            self.assertIsInstance(caught, ProtocolRefusal)
            self.assertEqual("archive_checkpoint_unsupported", caught.code)


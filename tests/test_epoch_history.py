"""Verified history handles over genuine native legacy epochs."""
from __future__ import annotations

import tests  # Pin HOME/XDG before product imports.

from datetime import datetime, timezone
from pathlib import Path
import os
import tempfile
import unittest
from unittest import mock

from floati import snapshot, epoch_history
from floati.bus_epoch import roll_bus_epoch
from floati.errors import IntegrityFailure
from floati.framing import encode_frame
from floati.events import EventLog
from floati.planes import AuthorityGrantStore
from floati.registry import Registry
from floati.root import FloatiRoot
from tests.temp_roots import REAL_TEMP_ROOT


class EpochHistoryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.root = FloatiRoot.open_direct_home(self.base / "fleet", create=True)
        for node in ("actor", "recipient"):
            Registry(self.root).register(node, "Codex")
        AuthorityGrantStore(self.root).grant_exact("bus-epoch-roll", "actor", 1,
                                                  datetime.now(timezone.utc))
        self.receipts, self.paths, self.payloads = [], [], []
        for ordinal in range(2):
            EventLog(self.root).send("actor", "recipient", "floati", "a" * 40,
                "AGENTS.md", "native history fixture", idempotency_key=str(ordinal))
            receipt = roll_bus_epoch(self.root, actor="actor",
                                    idempotency_key="history-" + str(ordinal))["receipt"]
            archive = Path(receipt["archive_path"])
            self.receipts.insert(0, receipt)
            self.paths.insert(0, archive)
            self.payloads.insert(0, {path.relative_to(archive).as_posix(): path.read_bytes()
                                    for path in archive.rglob("*") if path.is_file()})

    def history(self):
        factory = getattr(snapshot, "epoch_history", None)
        self.assertTrue(callable(factory), "receipt-owned verified history handle contract is absent")
        return factory(self.root)

    def test_existing_path_reader_control_follows_two_native_epochs(self):
        self.assertEqual(snapshot._owned_epoch_archives(self.root), tuple(self.paths))
        self.assertTrue(all(path.is_dir() for path in self.paths))

    def test_a_chain_over_the_reader_quota_refuses_instead_of_truncating(self):
        with mock.patch.object(snapshot, "MAX_LEDGER_RECORDS", 1):
            with self.assertRaises(IntegrityFailure) as caught:
                self.history()
        self.assertEqual(caught.exception.code, "epoch_receipt_chain_invalid")

    def test_legacy_path_wrapper_keeps_its_existing_bounded_prefix_behavior(self):
        with mock.patch.object(snapshot, "MAX_LEDGER_RECORDS", 1):
            self.assertEqual(snapshot._owned_epoch_archives(self.root), (self.paths[0],))

    def test_verified_handles_preserve_exact_members_and_legacy_path_order(self):
        history = self.history()
        self.assertIsInstance(history, tuple)
        self.assertEqual(tuple(epoch.legacy_path for epoch in history), tuple(self.paths))
        for epoch, receipt, payloads in zip(history, self.receipts, self.payloads):
            self.assertEqual(epoch.epoch_id, receipt["epoch_id"])
            self.assertEqual(epoch.receipt, receipt)
            self.assertEqual(epoch.facts["archive_sha256"], receipt["archive_sha256"])
            self.assertEqual(epoch.members, tuple(sorted(payloads, key=lambda name: name.encode())))
            for relative, data in payloads.items():
                self.assertEqual(epoch.read_member(relative), data)
            self.assertIsNone(epoch.read_member("receipts/acks/absent.jsonl"))
            with self.assertRaises(ValueError):
                epoch.read_member("../events.jsonl")

    def test_same_bytes_replacement_invalidates_old_handle_but_new_admission_succeeds(self):
        epoch = self.history()[0]
        source = self.paths[0] / "events.jsonl"
        replacement = self.base / "replacement"
        replacement.write_bytes(source.read_bytes())
        replacement.replace(source)
        with self.assertRaises(IntegrityFailure):
            epoch.read_member("events.jsonl")
        self.assertEqual(self.history()[0].read_member("events.jsonl"),
                         self.payloads[0]["events.jsonl"])

    def test_complete_membership_change_invalidates_a_handle(self):
        epoch = self.history()[0]
        (self.paths[0] / "new-empty-directory").mkdir()
        with self.assertRaises(IntegrityFailure):
            epoch.read_member("events.jsonl")

    def test_returned_metadata_cannot_mutate_native_proof(self):
        epoch = self.history()[0]
        epoch.receipt["span"]["byte_end"] = 0
        epoch.facts["plane_counts"]["events"] = 999
        self.assertEqual(epoch.receipt, self.receipts[0])
        self.assertEqual(epoch.facts["plane_counts"], self.receipts[0]["plane_counts"])
        self.assertEqual(epoch.read_member("events.jsonl"), self.payloads[0]["events.jsonl"])

    def test_unreferenced_archive_directory_is_never_admitted(self):
        foreign = self.root.path / "archive-unreferenced"
        foreign.mkdir()
        (foreign / "events.jsonl").write_bytes(b"not native history\n")
        self.assertEqual(tuple(epoch.legacy_path for epoch in self.history()), tuple(self.paths))

    def test_member_symlink_invalidates_a_handle_without_reading_the_target(self):
        epoch = self.history()[0]
        source = self.paths[0] / "events.jsonl"
        source.unlink()
        source.symlink_to(self.base / "absent-foreign-target")
        with self.assertRaises(IntegrityFailure):
            epoch.read_member("events.jsonl")

    def test_exact_reader_quota_accepts_the_complete_chain(self):
        with mock.patch.object(snapshot, "MAX_LEDGER_RECORDS", 2):
            self.assertEqual(tuple(epoch.legacy_path for epoch in self.history()), tuple(self.paths))

    def test_empty_and_genesis_ledgers_have_no_archived_history(self):
        root = FloatiRoot.open_direct_home(self.base / "empty", create=True)
        self.assertEqual(snapshot.epoch_history(root), ())
        for node in ("actor", "recipient"):
            Registry(root).register(node, "Codex")
        EventLog(root).send("actor", "recipient", "floati", "a" * 40,
                            "AGENTS.md", "genesis", idempotency_key="genesis")
        self.assertEqual(snapshot.epoch_history(root), ())

    def test_new_hard_link_invalidates_every_proof_bearing_access(self):
        epoch = self.history()[0]
        os.link(self.paths[0] / "events.jsonl", self.base / "foreign-hardlink")
        for access in (lambda: epoch.receipt, lambda: epoch.facts,
                       lambda: epoch.members, lambda: epoch.epoch_id,
                       lambda: epoch.legacy_path, lambda: epoch.read_member("events.jsonl")):
            with self.subTest(access=access), self.assertRaises(IntegrityFailure):
                access()

    def test_foreign_root_receipt_is_refused_before_payload_admission(self):
        receipt = dict(self.receipts[0])
        receipt["archive_path"] = str(self.base / self.paths[0].name)
        (self.root.path / "events.jsonl").write_bytes(encode_frame(receipt))
        with self.assertRaises(IntegrityFailure) as caught:
            self.history()
        self.assertEqual(caught.exception.code, "epoch_receipt_archive_invalid")

    def test_member_bytes_changed_since_receipt_refuse_fresh_admission(self):
        source = self.paths[0] / "events.jsonl"
        source.write_bytes(source.read_bytes() + b"opaque trailing bytes")
        with self.assertRaises(IntegrityFailure) as caught:
            self.history()
        self.assertEqual(caught.exception.code, "epoch_receipt_archive_invalid")

    def test_reading_history_creates_no_persistence_or_changes_to_legacy_bytes(self):
        def files():
            return {path.relative_to(self.root.path).as_posix(): path.read_bytes()
                    for path in self.root.path.rglob("*") if path.is_file()}
        before = files()
        for epoch in self.history():
            for member in epoch.members:
                epoch.read_member(member)
        self.assertEqual(files(), before)

    def test_member_payload_is_opaque_even_when_it_is_not_decodable_jsonl(self):
        source = self.root.path / "receipts" / "acks" / "opaque.jsonl"
        source.parent.mkdir(parents=True, exist_ok=True)
        payload = b"\xff\x00opaque member, no JSON frame or final newline"
        source.write_bytes(payload)
        roll_bus_epoch(self.root, actor="actor", idempotency_key="opaque-member")
        self.assertEqual(self.history()[0].read_member("receipts/acks/opaque.jsonl"), payload)


class HistoryQueryContextTests(unittest.TestCase):
    """First context cut: native legacy lineage, no representation transition."""

    def setUp(self):
        self.fixture = EpochHistoryTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.root

    def context(self, supplied=None):
        factory = getattr(epoch_history, "open_history", None)
        self.assertTrue(callable(factory), "root-only native history context is absent")
        return factory(self.root if supplied is None else supplied)

    def test_context_preserves_native_lineage_and_exact_opaque_member_bytes(self):
        context = self.context()
        self.assertEqual(tuple(self.fixture.receipts),
                         tuple(epoch.receipt for epoch in context.epochs))
        for epoch, payloads in zip(context.epochs, self.fixture.payloads):
            for name, raw in payloads.items():
                self.assertEqual(raw, epoch.read_member(name))
            self.assertIsNone(epoch.read_member("receipts/acks/absent.jsonl"))

    def test_changed_live_bytes_invalidate_context_and_already_returned_epoch(self):
        context = self.context()
        epoch = context.epochs[0]
        EventLog(self.root).send("actor", "recipient", "floati", "a" * 40,
            "AGENTS.md", "post-context append", idempotency_key="post-context")
        for operation in (lambda: context.epochs, lambda: context.dependency_token,
                          lambda: epoch.receipt, lambda: epoch.read_member("events.jsonl")):
            with self.subTest(operation=operation), self.assertRaises(IntegrityFailure):
                operation()

    def test_new_roll_invalidates_context_and_fresh_context_preserves_full_lineage(self):
        context = self.context()
        previous = context.dependency_token
        roll_bus_epoch(self.root, actor="actor", idempotency_key="context-extra-roll")
        with self.assertRaises(IntegrityFailure):
            _ = context.dependency_token
        fresh = self.context()
        self.assertEqual(3, len(fresh.epochs))
        self.assertNotEqual(previous, fresh.dependency_token)

    def test_same_bytes_live_replacement_invalidates_a_context(self):
        context = self.context()
        live = self.root.path / "events.jsonl"
        replacement = self.fixture.base / "context-replacement"
        replacement.write_bytes(live.read_bytes())
        replacement.replace(live)
        with self.assertRaises(IntegrityFailure):
            _ = context.epochs

    def test_live_change_during_member_read_cannot_return_stale_bytes(self):
        from floati import bus_epoch
        original = bus_epoch._read_bound_regular
        armed, mutations = [], []
        def changed(*args, **kwargs):
            result = original(*args, **kwargs)
            if armed:
                live = self.root.path / "events.jsonl"
                live.write_bytes(live.read_bytes() + b"\n")
                mutations.append(True)
            return result
        with mock.patch.object(bus_epoch, "_read_bound_regular", changed):
            context = self.context()
            epoch = context.epochs[0]
            armed.append(True)
            with self.assertRaises(IntegrityFailure):
                epoch.read_member("events.jsonl")
        self.assertEqual([True], mutations)

    def test_new_selected_plane_member_invalidates_previous_absence(self):
        context = self.context()
        target = self.root.path / "receipts" / "acks" / "new-plane.jsonl"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"")
        with self.assertRaises(IntegrityFailure):
            _ = context.dependency_token

    def test_nonordinary_current_selected_ledger_cannot_be_admitted_as_genesis(self):
        live = self.root.path / "events.jsonl"
        raw = live.read_bytes()
        for kind in ("symlink", "fifo"):
            with self.subTest(kind=kind):
                live.unlink()
                if kind == "symlink":
                    live.symlink_to(self.fixture.paths[0] / "events.jsonl")
                else:
                    os.mkfifo(live)
                try:
                    with self.assertRaises(IntegrityFailure):
                        self.context()
                finally:
                    live.unlink()
                    live.write_bytes(raw)

    def test_context_metadata_and_token_are_detached_and_immutable(self):
        context = self.context()
        token = context.dependency_token
        self.assertIsInstance(token, tuple)
        epoch = context.epochs[0]
        epoch.receipt["plane_counts"]["events"] = 999
        self.assertEqual(self.fixture.receipts[0], epoch.receipt)
        self.assertEqual(token, context.dependency_token)

    def test_factory_accepts_only_actual_native_root(self):
        self.context()
        for supplied in (self.root.path, {"root": self.root}, self.fixture.receipts[0]):
            with self.subTest(supplied=type(supplied)), self.assertRaises(IntegrityFailure):
                self.context(supplied)

    def test_aggregate_legacy_input_bound_refuses_before_eager_archive_read(self):
        self.context()
        from floati import bus_epoch
        def eager_read(*args, **kwargs):
            self.fail("eager archive read exceeded aggregate budget")
        with mock.patch.object(epoch_history, "MAX_QUERY_INPUT_BYTES", 1),\
                mock.patch.object(bus_epoch, "_read_epoch_archive_payloads", eager_read):
            from floati.errors import ProtocolRefusal
            with self.assertRaises(ProtocolRefusal) as caught:
                self.context()
        self.assertEqual("epoch_history_query_unsupported", caught.exception.code)

    def test_empty_commit_is_absence_and_reading_has_no_persistent_effect(self):
        from floati import archive_catalog
        target = self.root.path / archive_catalog.LEDGER
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"")
        before = {path.relative_to(self.root.path).as_posix(): path.read_bytes()
                  for path in self.root.path.rglob("*") if path.is_file()}
        context = self.context()
        self.assertEqual(2, len(context.epochs))
        self.assertIsNone(context.generation)
        self.assertEqual(before, {path.relative_to(self.root.path).as_posix(): path.read_bytes()
                  for path in self.root.path.rglob("*") if path.is_file()})


class HistoryQueryReaderIdentityTests(unittest.TestCase):
    """Post-admission native reader changes cannot hide changed dependencies."""

    setUp = HistoryQueryContextTests.setUp
    context = HistoryQueryContextTests.context

    def test_replaced_namespace_evaluator_cannot_mask_live_append(self):
        from floati import epoch_carry
        context = self.context()
        child = context.epochs[0]
        observed = epoch_carry.carry_dependency_witness(self.root)
        EventLog(self.root).send("actor", "recipient", "floati", "a" * 40,
            "AGENTS.md", "reader replacement append", idempotency_key="reader-append")
        calls = []
        def replay_old(root):
            calls.append(root)
            return observed
        with mock.patch.object(epoch_carry, "carry_dependency_witness", replay_old):
            for operation in (lambda: context.epochs, lambda: context.dependency_token,
                              lambda: child.receipt):
                with self.subTest(operation=operation), self.assertRaises(IntegrityFailure):
                    operation()
        self.assertEqual([], calls, "replacement namespace evaluator executed")

    def test_replaced_legacy_metadata_getters_refuse_before_candidate(self):
        context = self.context()
        child = context.epochs[0]
        for name in ("epoch_id", "receipt", "facts", "members"):
            calls = []
            def replacement(archive):
                calls.append(archive)
                return "substituted metadata"
            with mock.patch.object(epoch_history.EpochArchive, name, property(replacement)):
                with self.subTest(getter=name), self.assertRaises(IntegrityFailure):
                    getattr(child, name)
            with self.subTest(uninvoked=name):
                self.assertEqual([], calls)

    def test_changed_namespace_support_limits_invalidate_context(self):
        from floati import epoch_carry
        context = self.context()
        for name in ("MAX_DEPENDENCY_WITNESS_ENTRIES", "MAX_DEPENDENCY_WITNESS_DEPTH"):
            with mock.patch.object(epoch_carry, name, getattr(epoch_carry, name) + 1):
                with self.subTest(limit=name), self.assertRaises(IntegrityFailure):
                    _ = context.generation

    def test_replaced_epoch_guard_refuses_before_candidate(self):
        from floati import bus_epoch
        context = self.context()
        calls = []
        def replacement(root):
            calls.append(root)
            raise AssertionError("replacement epoch guard executed")
        with mock.patch.object(bus_epoch, "epoch_guard", replacement):
            with self.assertRaises(IntegrityFailure):
                _ = context.epochs
        self.assertEqual([], calls)

    def test_replaced_query_entrypoints_refuse_before_candidate(self):
        from floati import epoch_carry

        context = self.context()
        child = context.epochs[0]
        for owner, name in ((epoch_history, "_query_contract"),
                            (epoch_history, "_check_query_readers"),
                            (epoch_history, "_context_state"),
                            (epoch_history, "_context_read"),
                            (type(child), "_read")):
            calls = []
            def replacement(*args, **kwargs):
                calls.append(True)
                raise AssertionError("replacement query entrypoint executed")
            with mock.patch.object(owner, name, replacement):
                with self.subTest(entrypoint=name), self.assertRaises(IntegrityFailure):
                    _ = child.receipt
            with self.subTest(uninvoked=name):
                self.assertEqual([], calls)

        def changed(handle):
            raise AssertionError("changed query reader code executed")

        for name, reader, read in (
            ("reader checker", epoch_history._check_query_readers, lambda: child.receipt),
            ("namespace evaluator", epoch_carry.carry_dependency_witness, lambda: context.epochs),
            ("legacy metadata getter", epoch_history.EpochArchive.receipt.fget, lambda: child.receipt),
        ):
            with self.subTest(changed_code=name):
                native_code = reader.__code__
                try:
                    reader.__code__ = changed.__code__
                    with self.assertRaises(IntegrityFailure):
                        read()
                finally:
                    reader.__code__ = native_code

    def test_changed_context_accessors_invalidate_sibling_reads(self):
        context = self.context()
        child = context.epochs[0]
        for owner, name in ((type(context), "epochs"), (type(child), "receipt")):
            calls = []
            def replacement(self):
                calls.append(True)
                return ()
            with mock.patch.object(owner, name, property(replacement)):
                with self.subTest(accessor=name), self.assertRaises(IntegrityFailure):
                    _ = context.generation
            self.assertEqual([], calls)


if __name__ == "__main__":
    unittest.main()

"""Exhaustion receipt indexing preserves native truth across fresh readers."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from floati import jsonl, receipt_prefix
from floati.codex_wait_contract import CodexWaitReceiptLedger
from floati.errors import IntegrityFailure, ProtocolRefusal
from floati.ids import uuid7_hex
from floati.registry import Registry
from floati.root import FloatiRoot
from tests.temp_roots import REAL_TEMP_ROOT
from tests.bus_scale_test_profile import StableSourceFixture
from tests.bus_scale_test_profile import stable_fixture_sources


class ReceiptPrefixTests(StableSourceFixture, unittest.TestCase):
    @stable_fixture_sources()
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
        self.addCleanup(self.temp.cleanup)
        self.root = FloatiRoot.open_direct_home(Path(self.temp.name) / "fleet", create=True)
        Registry(self.root).register("worker", "codex")
        self.path = self.root.resolve_relative(receipt_prefix.relative("worker"))

    def record(self, key, *, waited=2):
        return CodexWaitReceiptLedger(self.root).record_exhaustion(node_id="worker",
            session_digest=hashlib.sha256(b"session").hexdigest(),
            waited_seconds=waited, idempotency_key=key)

    def prefix(self):
        return receipt_prefix.ReceiptPrefix(self.root, "worker")

    def forbid_history_read(self):
        native = jsonl._read_path_records
        def read(path, *args, **kwargs):
            if path == self.path:
                self.fail("fresh receipt transaction replayed its already certified history")
            return native(path, *args, **kwargs)
        return patch.object(jsonl, "_read_path_records", side_effect=read)

    def assert_native_refusal(self):
        before = self.path.read_bytes()
        with self.assertRaises(IntegrityFailure) as native:
            jsonl.read_records_snapshot(self.root, receipt_prefix.relative("worker"),
                allowed_kinds=receipt_prefix.KINDS)
        with self.assertRaises(IntegrityFailure) as indexed:
            self.record("after-corruption")
        self.assertEqual(native.exception.code, indexed.exception.code)
        self.assertEqual(before, self.path.read_bytes())

    def test_fresh_instance_append_and_historical_retry_do_not_replay_verified_history(self):
        first = self.record("first")
        for number in range(16):
            self.record("history-" + str(number))
        with self.forbid_history_read():
            self.record("new")
            self.assertEqual(first, self.record("first"))
            with self.assertRaises(ProtocolRefusal) as conflict:
                self.record("first", waited=3)
        self.assertEqual("codex_wait_exhaustion_idempotency_conflict", conflict.exception.code)

    def test_missing_checkpoint_or_nodes_replays_and_preserves_idempotency(self):
        first = self.record("first")
        for fault in ("checkpoint", "nodes"):
            prefix = self.prefix()
            if fault == "checkpoint":
                (prefix.home / "checkpoint.json").unlink()
            else:
                for path in prefix.nodes.iterdir():
                    path.unlink()
            before = self.path.read_bytes()
            self.assertEqual(first, self.record("first"))
            self.assertEqual(before, self.path.read_bytes())

    def test_same_size_rewrite_with_restored_mtime_cannot_hide_malformed_history(self):
        self.record("first")
        before = self.path.stat()
        data = self.path.read_bytes().replace(b'"schema_version":1', b'"schema_version":9', 1)
        self.path.write_bytes(data)
        os.utime(self.path, ns=(before.st_atime_ns, before.st_mtime_ns))
        self.assertEqual(before.st_size, self.path.stat().st_size)
        self.assert_native_refusal()

    def test_unterminated_tail_refuses_before_append(self):
        self.record("first")
        self.path.write_bytes(self.path.read_bytes()[:-1])
        self.assert_native_refusal()

    def test_forged_checkpoint_identity_cannot_certify_changed_ledger(self):
        self.record("first")
        self.path.write_bytes(self.path.read_bytes()[:-1])
        checkpoint = self.prefix().home / "checkpoint.json"
        value = json.loads(checkpoint.read_text())
        value["payload"]["identity"] = receipt_prefix.send_prefix._identity(self.path)
        value["payload"]["offset"] = self.path.stat().st_size
        checkpoint.write_text(json.dumps(value))
        self.assert_native_refusal()

    def test_replaced_ledger_inode_rebuilds_instead_of_trusting_old_key(self):
        first = self.record("first")
        replacement = self.path.with_suffix(".replacement")
        replacement.write_bytes(self.path.read_bytes())
        replacement.replace(self.path)
        self.assertEqual(first, self.record("first"))
        prefix = self.prefix()
        prefix.load()
        self.assertEqual(self.path.stat().st_ino, prefix.payload["identity"][1])

    def test_checkpoint_crash_recovers_appended_receipt_once(self):
        self.record("first")
        checkpoint = self.prefix().home / "checkpoint.json"
        stale = checkpoint.read_bytes()
        second = self.record("second")
        checkpoint.write_bytes(stale)
        before = self.path.read_bytes()
        self.assertEqual(second, self.record("second"))
        self.assertEqual(before, self.path.read_bytes())

    def test_cached_transaction_keeps_duplicate_record_and_ledger_limits(self):
        first = self.record("first")
        with patch("floati.codex_wait_contract.uuid7_hex", return_value=first["id"].removeprefix("codex-wait-exhaustion-")):
            with self.assertRaises(ProtocolRefusal) as duplicate:
                self.record("duplicate-id")
        self.assertEqual("duplicate_record_id", duplicate.exception.code)
        with patch.object(jsonl, "MAX_LEDGER_RECORDS", 1):
            with self.assertRaises(ProtocolRefusal) as limit:
                self.record("past-limit")
        self.assertEqual("ledger_record_limit", limit.exception.code)

    def test_extra_bytes_after_native_append_are_not_sealed(self):
        self.record("first")
        native = jsonl._append_frame
        def append(path, encoded, **kwargs):
            identity = native(path, encoded, **kwargs)
            with path.open("ab") as stream:
                stream.write(b"unvalidated\n")
            return identity
        with patch.object(jsonl, "_append_frame", side_effect=append):
            self.record("append-race")
        self.assert_native_refusal()

    def test_unavailable_cache_falls_back_to_native_idempotency(self):
        first = self.record("first")
        before = self.path.read_bytes()
        with patch.object(receipt_prefix, "_open", return_value=None):
            self.assertEqual(first, self.record("first"))
            self.record("second")
        self.assertEqual(before, self.path.read_bytes()[:len(before)])

    def test_missing_selected_proof_is_healed_for_the_next_no_append_retry(self):
        first = self.record("first")
        prefix = self.prefix()
        for path in prefix.nodes.iterdir():
            path.unlink()
        before = self.path.read_bytes()
        self.assertEqual(first, self.record("first"))
        with self.forbid_history_read():
            self.assertEqual(first, self.record("first"))
        self.assertEqual(before, self.path.read_bytes())

    def test_internal_callback_cannot_advance_a_different_node_or_key(self):
        first = self.record("first")
        for available in (True, False):
            for field, value in (("node_id", "another-worker"),
                                 ("idempotency_key", "unselected")):
                with self.subTest(cache=available, field=field):
                    candidate = dict(first, id="codex-wait-exhaustion-" + uuid7_hex(),
                        idempotency_key="selected")
                    candidate[field] = value
                    before = self.path.read_bytes()
                    # Keep both the optimized and native-fallback seams bound
                    # to exactly the same closed candidate-selection contract.
                    native = receipt_prefix._open
                    with patch.object(receipt_prefix, "_open",
                            side_effect=native if available else lambda *args: None):
                        with self.assertRaises(ProtocolRefusal) as refused:
                            receipt_prefix.transact_exhaustion(self.root, "worker", "selected",
                                lambda prior: (candidate, candidate))
                    self.assertEqual("codex_wait_exhaustion_idempotency_conflict", refused.exception.code)
                    self.assertEqual(before, self.path.read_bytes())

    def test_internal_callback_cannot_replace_the_first_matching_key(self):
        first = self.record("first")
        candidate = dict(first, id="codex-wait-exhaustion-" + uuid7_hex(), waited_seconds=3)
        before = self.path.read_bytes()
        for available in (True, False):
            native = receipt_prefix._open
            with self.subTest(cache=available), patch.object(receipt_prefix, "_open",
                    side_effect=native if available else lambda *args: None):
                with self.assertRaises(ProtocolRefusal) as refused:
                    receipt_prefix.transact_exhaustion(self.root, "worker", "first",
                        lambda prior: (candidate, candidate))
            self.assertEqual("codex_wait_exhaustion_idempotency_conflict", refused.exception.code)
            self.assertEqual(before, self.path.read_bytes())
        self.assertEqual(first, self.record("first"))

    def test_selected_io_does_not_grow_or_enumerate_the_bank_with_history(self):
        from floati import key_prefix
        self.record("first")
        observed = []
        native_read, native_list = key_prefix.KeyPrefix._read_at, os.listdir
        bank, buckets = self.prefix().nodes, self.prefix().buckets
        bank_ids = {(path.stat().st_dev, path.stat().st_ino) for path in (bank, buckets)}
        def read(prefix, selected_bank, name, maximum):
            if prefix.home == bank.parent and (selected_bank in {"nodes", "buckets"} or name == "directory.bin"):
                observed.append((selected_bank, name, maximum))
            return native_read(prefix, selected_bank, name, maximum)
        def enumerate_bank(path="."):
            if isinstance(path, int):
                info = os.fstat(path)
                selected = (info.st_dev, info.st_ino) in bank_ids
            else:
                selected = Path(path) in (bank, buckets)
            if selected:
                self.fail("warm selected transaction enumerated its historical key bank")
            return native_list(path)
        for count in (16, 160):
            for number in range(count):
                self.record("history-" + str(number))
            observed.clear()
            with patch.object(key_prefix.KeyPrefix, "_read_at", new=read), patch.object(os, "listdir", side_effect=enumerate_bank), self.forbid_history_read():
                self.record("selected-" + str(count))
            self.assertEqual(1, sum(name == "directory.bin" for _, name, _ in observed))
            self.assertEqual(0, sum(selected_bank == "nodes" for selected_bank, _, _ in observed))
            self.assertLessEqual(sum(selected_bank == "buckets" for selected_bank, _, _ in observed), 4)

    def test_namespace_deletion_rebuilds_instead_of_proving_absence(self):
        first = self.record("first")
        prefix = self.prefix()
        prefix.load()
        name = prefix.filename(receipt_prefix.send_prefix._key("receipt", "first"))
        (prefix.nodes / name).unlink()
        before = self.path.read_bytes()
        self.assertEqual(first, self.record("first"))
        self.assertEqual(before, self.path.read_bytes())

    def test_entry_content_corruption_with_unchanged_bank_identity_rebuilds(self):
        first = self.record("first")
        prefix = self.prefix()
        prefix.load()
        name = prefix.filename(receipt_prefix.send_prefix._key("receipt", "first"))
        before_bank = receipt_prefix.send_prefix._identity(prefix.nodes)
        target = prefix.nodes / name
        target.write_bytes(target.read_bytes().replace(b'"seal":"', b'"seal":"0', 1))
        self.assertEqual(before_bank, receipt_prefix.send_prefix._identity(prefix.nodes))
        self.assertEqual(first, self.record("first"))

    def test_stale_generation_entry_cannot_override_rebuilt_first_key(self):
        first = self.record("first")
        prefix = self.prefix()
        prefix.load()
        key = receipt_prefix.send_prefix._key("receipt", "first")
        target = prefix.nodes / prefix.filename(key)
        stale = target.read_bytes()
        old_generation = prefix.payload["generation"]
        prefix.rebuild()
        self.assertNotEqual(old_generation, prefix.payload["generation"])
        bank_identity = receipt_prefix.send_prefix._identity(prefix.nodes)
        target.write_bytes(stale)
        self.assertEqual(bank_identity, receipt_prefix.send_prefix._identity(prefix.nodes))
        self.assertEqual(first, self.record("first"))

    def test_symlinked_bank_is_not_trusted_or_followed(self):
        first = self.record("first")
        prefix = self.prefix()
        retained = prefix.nodes.with_name("retained-bank")
        prefix.nodes.rename(retained)
        foreign = Path(self.temp.name) / "foreign"
        foreign.mkdir(mode=0o700)
        marker = foreign / "keep"
        marker.write_bytes(b"foreign cache bytes")
        prefix.nodes.symlink_to(foreign, target_is_directory=True)
        self.assertEqual(first, self.record("first"))
        self.assertEqual(b"foreign cache bytes", marker.read_bytes())
        self.assertEqual([marker], list(foreign.iterdir()))

    def test_unrelated_entry_deleted_during_publication_never_becomes_absent(self):
        first = self.record("first")
        key = receipt_prefix.send_prefix._key("receipt", "first")
        native = receipt_prefix.ReceiptPrefix._advance
        def advance(prefix, batch, encoded):
            (prefix.nodes / prefix.filename(key)).unlink()
            return native(prefix, batch, encoded)
        with patch.object(receipt_prefix.ReceiptPrefix, "_advance", new=advance):
            self.record("second")
        before = self.path.read_bytes()
        self.assertEqual(first, self.record("first"))
        self.assertEqual(before, self.path.read_bytes(), "positive membership with missing entry minted a duplicate")

    def test_missing_or_corrupt_membership_bucket_never_proves_absence(self):
        from floati import key_prefix
        first = self.record("first")
        for fault in ("missing", "corrupt"):
            prefix = self.prefix()
            prefix.load()
            key = receipt_prefix.send_prefix._key("receipt", "first")
            index = key_prefix.bucket_index(prefix.secret, prefix.payload["generation"], key)
            page = prefix.buckets / format(index, "04x")
            if fault == "missing":
                page.unlink()
            else:
                page.write_bytes(bytes(key_prefix.BUCKET_BYTES))
            before = self.path.read_bytes()
            self.assertEqual(first, self.record("first"))
            self.assertEqual(before, self.path.read_bytes())

    def test_rewritten_membership_directory_requires_native_rebuild(self):
        first = self.record("first")
        prefix = self.prefix()
        directory = prefix.home / "directory.bin"
        directory.write_bytes(bytes(directory.stat().st_size))
        before = self.path.read_bytes()
        self.assertEqual(first, self.record("first"))
        self.assertEqual(before, self.path.read_bytes())

    def test_three_recipient_native_maps_preserve_bounded_archived_query_refusals(self):
        from floati.cursor import SparseCursor
        from floati.events import EventLog
        from floati.ids import uuid7_hex
        for node in ("sender", "other-one", "other-two"):
            Registry(self.root).register(node, "codex")
        log, cursor = EventLog(self.root), SparseCursor(self.root)
        for number, node in enumerate(("worker", "other-one", "other-two")):
            message = log.send("sender", node, "floati", "a" * 40, "note.md", "native namespace budget",
                idempotency_key="query-history-" + str(number))
            log.present(node)
            cursor.ack(node, [message["id"]], acting_session_id="query-history")
        with self.assertRaises(ProtocolRefusal) as unknown:
            cursor.ack("worker", ["msg-" + uuid7_hex()], acting_session_id="unknown")
        self.assertEqual("ack_item_unknown", unknown.exception.code)
        pending = log.send("sender", "worker", "floati", "a" * 40, "note.md", "native unpresented item",
            idempotency_key="query-not-delivered")
        with self.assertRaises(ProtocolRefusal) as undelivered:
            cursor.ack("worker", [pending["id"]], acting_session_id="not-delivered")
        self.assertEqual("ack_item_not_delivered", undelivered.exception.code,
                         str(undelivered.exception))

    def test_sparse_buckets_use_canonical_lossless_compressed_storage(self):
        from floati import key_prefix
        self.record("first")
        prefix = self.prefix()
        prefix.load()
        for target in prefix.buckets.iterdir():
            raw = target.read_bytes()
            self.assertLess(len(raw), key_prefix.BUCKET_BYTES)
            page = key_prefix.decode_storage(raw)
            self.assertEqual(key_prefix.BUCKET_BYTES, len(page))
            self.assertEqual(raw, key_prefix.encode_storage(page))

    def test_compressed_bucket_alias_trailing_oversized_and_invalid_data_recover_natively(self):
        import zlib
        from floati import key_prefix
        first = self.record("first")
        for fault in ("alias", "trailing", "oversized", "invalid"):
            with self.subTest(fault=fault):
                prefix = self.prefix()
                prefix.load()
                key = receipt_prefix.send_prefix._key("receipt", "first")
                index = key_prefix.bucket_index(prefix.secret, prefix.payload["generation"], key)
                target = prefix.buckets / format(index, "04x")
                raw = target.read_bytes()
                if fault == "alias":
                    forged = zlib.compress(key_prefix.decode_storage(raw), 1)
                    self.assertNotEqual(raw, forged)
                elif fault == "trailing":
                    forged = raw + b"unreviewed trailing bytes"
                elif fault == "oversized":
                    forged = zlib.compress(bytes(key_prefix.BUCKET_BYTES + 1), 9)
                else:
                    forged = b"not a compressed membership page"
                target.write_bytes(forged)
                before = self.path.read_bytes()
                self.assertEqual(first, self.record("first"))
                self.assertEqual(before, self.path.read_bytes())

    def assert_rebuild_does_not_follow_bank_swapped_during_decode(self, bank_name, foreign_leaf):
        self.record("first")
        prefix = self.prefix()
        bank = prefix.home / bank_name
        retained = prefix.home / (bank_name + "-retained")
        foreign = Path(self.temp.name) / ("foreign-" + bank_name)
        foreign.mkdir(mode=0o700)
        marker = foreign / foreign_leaf
        marker.write_bytes(b"Owned foreign bytes must survive cache recovery exactly.\n")
        marker.chmod(0o600)
        foreign_before = {path.name: path.read_bytes() for path in foreign.iterdir()}
        ledger_before = self.path.read_bytes()
        native_decode = jsonl._decode_path_records
        swapped = []
        def decode(path, *args, **kwargs):
            rows = native_decode(path, *args, **kwargs)
            if path == self.path and not swapped:
                # Constructor checks have already succeeded. Native source
                # decoding is the real boundary before rebuild cleanup/write.
                bank.rename(retained)
                bank.symlink_to(foreign, target_is_directory=True)
                swapped.append(True)
            return rows
        with patch.object(jsonl, "_decode_path_records", side_effect=decode):
            try:
                prefix.rebuild()
            except receipt_prefix.send_prefix._CACHE_ERRORS:
                pass  # Cache refusal/recovery is allowed; foreign mutation is not.
        self.assertEqual([True], swapped, "control did not replace the bank during native decoding")
        self.assertTrue(bank.is_symlink(), "the control's foreign namespace was unexpectedly repaired")
        self.assertEqual(foreign_before, {path.name: path.read_bytes() for path in foreign.iterdir()},
            "cache rebuild followed a swapped directory and deleted or published foreign files")
        self.assertEqual(ledger_before, self.path.read_bytes(), "cache recovery changed authoritative receipt evidence")

    def test_rebuild_does_not_follow_nodes_swapped_during_native_decode(self):
        self.assert_rebuild_does_not_follow_bank_swapped_during_decode("nodes", "a" * 64)

    def test_rebuild_does_not_follow_buckets_swapped_during_native_decode(self):
        self.assert_rebuild_does_not_follow_bank_swapped_during_decode("buckets", "0000")

    def test_directory_descriptors_close_after_success_and_partial_open_failure(self):
        from floati import key_prefix
        first = self.record("first")
        native_open, native_close = os.open, os.close
        live = set()
        opened = []
        def open_descriptor(path, flags, *args, **kwargs):
            descriptor = native_open(path, flags, *args, **kwargs)
            if flags & os.O_DIRECTORY:
                live.add(descriptor)
                opened.append(descriptor)
            return descriptor
        def close_descriptor(descriptor):
            live.discard(descriptor)
            return native_close(descriptor)
        with patch.object(key_prefix.os, "open", side_effect=open_descriptor), patch.object(key_prefix.os, "close", side_effect=close_descriptor):
            prefix = self.prefix()
            self.assertEqual(set(), live, "constructor retained directory descriptors")
            prefix.load()
            self.assertEqual(first, prefix.get("receipt", "first"))
            self.assertEqual(set(), live, "selected read retained directory descriptors")
            retained = prefix.buckets.with_name("retained-buckets")
            prefix.buckets.rename(retained)
            foreign = Path(self.temp.name) / "foreign-fd-control"
            foreign.mkdir(mode=0o700)
            prefix.buckets.symlink_to(foreign, target_is_directory=True)
            with self.assertRaises(receipt_prefix.send_prefix._CACHE_ERRORS):
                self.prefix()
            self.assertEqual(set(), live, "partial constructor failure leaked previously opened directories")
        self.assertTrue(opened, "descriptor control did not observe actual confined directory opens")

    def test_native_ioprobe_observes_descriptor_relative_directory_bucket_and_entry_reads(self):
        from floati import key_prefix
        from tests.bus_scale_io import IOProbe
        first = self.record("first")
        prefix = self.prefix()
        prefix.load()
        key = receipt_prefix.send_prefix._key("receipt", "first")
        index = key_prefix.bucket_index(prefix.secret, prefix.payload["generation"], key)
        selected = (prefix.home / "seal.key", prefix.home / "checkpoint.json", prefix.home / "directory.bin",
            prefix.buckets / format(index, "04x"), prefix.nodes / prefix.filename(key))
        expected = {path.relative_to(self.root.tenant_home).as_posix(): path.stat().st_size for path in selected}
        with IOProbe(self.root.tenant_home) as probe:
            self.assertEqual(first, self.record("first"))
        for path, count in expected.items():
            self.assertEqual(count, probe.read_bytes_by_path.get(path), "native observer missed or double-counted " + path)
        probe.assert_complete()  # Includes no outstanding opened directory/file descriptors.


if __name__ == "__main__":
    unittest.main()

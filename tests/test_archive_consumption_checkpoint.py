"""Historical ACTED closure uses full lineage, never future-ACT eligibility."""
from __future__ import annotations

import tests

from contextlib import nullcontext
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import unittest
from unittest import mock

from floati import archive_consumption_checkpoint as checkpoint
from floati.bus_epoch import epoch_guard, roll_bus_epoch
from floati.errors import FloatiError, ProtocolRefusal
from floati.framing import encode_frame
from floati.ids import uuid7_hex
from floati.message_consumption import MessageConsumption
from floati.planes import AuthorityGrantStore
from floati.root import FloatiRoot
from tests import test_message_consumption as fixture


class ArchiveConsumptionCheckpointTests(unittest.TestCase):
    setUp = fixture.MessageConsumptionTests.setUp
    _send = fixture.MessageConsumptionTests._send
    _ack = fixture.MessageConsumptionTests._ack
    _claimed = fixture.MessageConsumptionTests._claimed

    def prepare(self, *, worker=None, work=False):
        item = self._claimed() if work else None
        message = self._send(worker_session_id=worker)
        ack = self._ack(message, worker_session_id=worker)
        if work:
            self.work.complete(item["id"], "recipient", [], consume_ids=[message["id"]],
                               session_id=worker or "acting-session")
            receipt = MessageConsumption(self.root)._raw("recipient")[0]
        else:
            receipt = MessageConsumption(self.root).consume("recipient", message["id"], worker or "acting-session")
        return message, ack, receipt, item

    def plan(self):
        with epoch_guard(self.root, exclusive=True):
            return checkpoint.plan_consumption_checkpoint(self.root)

    def roll(self, key):
        AuthorityGrantStore(self.root).grant_exact("bus-epoch-roll", "sender", 1,
                                                  datetime.now(timezone.utc))
        return roll_bus_epoch(self.root, actor="sender", idempotency_key=key)["receipt"]

    def tree(self):
        return {path.relative_to(self.root.tenant_home).as_posix():
                ("directory" if path.is_dir() else path.read_bytes())
                for path in self.root.tenant_home.rglob("*")}

    def write_receipts(self, rows, worker=None):
        path = self.root.tenant_home / MessageConsumption(self.root)._relative("recipient", worker)
        path.write_bytes(b"".join(encode_frame(row) for row in rows))


    def test_plan_retains_existing_receipt_source_ack_and_manual_boundary(self):
        message, ack, receipt, unused = self.prepare()
        first = self.roll("first")
        second = self.roll("second")
        result = self.plan()
        json.dumps(result, allow_nan=False)
        self.assertEqual([receipt], result["receipts"])
        self.assertTrue(result["joins"], "historical consumption source join is absent")
        joined = result["joins"][0]
        self.assertEqual(message["message"], joined["envelope"])
        self.assertEqual(ack, joined["ack_receipt"])
        self.assertEqual(receipt["id"], joined["receipt_id"])
        self.assertTrue(all("automatic" not in row for row in result["lineage"]))
        self.assertEqual([second, first], result["lineage"])
        self.assertEqual(Path(first["archive_path"]).relative_to(self.root.tenant_home).as_posix(),
                         joined["source_prefix"])

    def test_plain_and_worker_planes_are_both_retained(self):
        unused, unused_ack, plain, unused_item = self.prepare()
        unused, unused_ack, worker, unused_item = self.prepare(worker="worker-a")
        result = self.plan()
        self.assertEqual({plain["id"], worker["id"]}, {row["id"] for row in result["receipts"]})
        self.assertEqual({None, "worker-a"}, {row["worker_session_id"] for row in result["source_planes"]})

    def test_completed_work_consumption_preserves_current_holder_and_work_records(self):
        message, ack, receipt, item = self.prepare(work=True)
        self.roll("work-history")
        result = self.plan()
        self.assertEqual([receipt], result["receipts"])
        self.assertTrue(result["joins"], "missing native work-holder join")
        projected = result["joins"][0]["work_item"]
        self.assertEqual(item["id"], projected["id"])
        self.assertEqual("recipient", projected["holder"])
        self.assertEqual("completed", projected["state"])
        self.assertTrue(result["work_records"], "original work transition proof was omitted")

    def test_missing_current_work_holder_blocks_historical_receipt(self):
        cases = ("work holder", "exact ACK", "duplicate", "wrong recipient", "orphan node")
        for case in cases:
            with self.subTest(case=case):
                fixture = type(self)()
                fixture.setUp()
                try:
                    unused, unused_ack, receipt, unused_item = fixture.prepare(work=case == "work holder")
                    if case == "work holder":
                        (fixture.root.tenant_home / "work/items.jsonl").unlink()
                    elif case == "exact ACK":
                        fixture.write_receipts([dict(receipt, ack_receipt_id="ack-" + uuid7_hex())])
                    elif case == "duplicate":
                        fixture.write_receipts([receipt, dict(receipt, id="consume-" + uuid7_hex())])
                    elif case == "wrong recipient":
                        source = fixture.root.tenant_home / "receipts/consumed/recipient.jsonl"
                        source.replace(source.with_name("other.jsonl"))
                    else:
                        orphan = dict(receipt, recipient="not-registered", id="consume-" + uuid7_hex())
                        (fixture.root.tenant_home / "receipts/consumed/not-registered.jsonl").write_bytes(encode_frame(orphan))
                    with self.assertRaises(FloatiError) as caught:
                        fixture.plan()
                    self.assertEqual("consumption_evidence_invalid", caught.exception.code)
                finally:
                    fixture.doCleanups()


    def test_cold_root_plan_preserves_complete_bytes_and_membership(self):
        unused, unused_ack, receipt, unused_item = self.prepare(work=True)
        for path in self.root.tenant_home.rglob("*.lock"):
            path.unlink()
        with epoch_guard(self.root, exclusive=True):
            before = self.tree()
            error = None
            try:
                result = checkpoint.plan_consumption_checkpoint(self.root)
            except FloatiError as caught:
                error = caught
            self.assertEqual(before, self.tree(), "planning initialized a missing lock")
            self.assertIsNone(error, "valid cold consumption planning must succeed")
            self.assertEqual([receipt], result["receipts"])

    def test_owned_exclusive_epoch_is_required(self):
        self.prepare()
        with self.assertRaises(ProtocolRefusal) as caught:
            checkpoint.plan_consumption_checkpoint(self.root)
        self.assertEqual("epoch_lock_required", caught.exception.code)
        with epoch_guard(self.root):
            with self.assertRaises(ProtocolRefusal):
                checkpoint.plan_consumption_checkpoint(self.root)

    def test_source_substitution_after_native_validation_blocks(self):
        self.prepare()
        native = MessageConsumption._validated_history
        def changed(reader, *args, **kwargs):
            result = native(reader, *args, **kwargs)
            path = self.root.tenant_home / "receipts/consumed/recipient.jsonl"
            replacement = self.root.tenant_home / "replacement"
            replacement.write_bytes(path.read_bytes())
            replacement.replace(path)
            return result
        with mock.patch.object(MessageConsumption, "_validated_history", changed):
            with self.assertRaises(ProtocolRefusal) as caught:
                self.plan()
        self.assertEqual("archive_consumption_checkpoint_blocked", caught.exception.code)

    def test_symlinked_consumed_namespace_refuses_without_touching_foreign_bytes(self):
        self.prepare()
        source = self.root.tenant_home / "receipts/consumed"
        saved = source.with_name("saved-consumed")
        source.rename(saved)
        foreign = self.root.tenant_home.parent / "foreign"
        foreign.mkdir()
        sentinel = foreign / "sentinel"
        sentinel.write_bytes(b"foreign sentinel")
        source.symlink_to(foreign, target_is_directory=True)
        with self.assertRaises(FloatiError):
            self.plan()
        self.assertEqual(b"foreign sentinel", sentinel.read_bytes())

    def test_caller_owned_plan_cannot_mutate_native_history(self):
        unused, unused_ack, receipt, unused_item = self.prepare()
        result = self.plan()
        self.assertEqual([receipt], result["receipts"])
        result["receipts"][0]["acting_session_id"] = "caller mutation"
        self.assertEqual(receipt, MessageConsumption(self.root)._raw("recipient")[0])

    def test_unavailable_dependency_witness_blocks(self):
        self.prepare()
        with mock.patch.object(checkpoint, "carry_dependency_witness", return_value=None, create=True):
            with self.assertRaises(ProtocolRefusal):
                self.plan()

    def test_empty_consumption_domain_creates_no_namespace(self):
        before = self.tree()
        self.assertEqual([], self.plan()["receipts"])
        self.assertEqual(before, self.tree())

    def test_ordered_ack_history_and_exact_receipt_reference_are_both_retained(self):
        message, first, receipt, unused_item = self.prepare()
        second = self.cursor.ack("recipient", [message["id"]], acting_session_id="seen-again")
        joined = self.plan()["joins"][0]
        self.assertEqual(first, joined["ack_receipt"])
        self.assertEqual([first, second], joined.get("matching_ack_receipts", []))

    def test_unsupported_noncanonical_source_cut_blocks_without_rewriting_legacy_bytes(self):
        unused, unused_ack, receipt, unused_item = self.prepare()
        path = self.root.tenant_home / "events.jsonl"
        path.write_bytes(b" " + path.read_bytes())
        reader = MessageConsumption(self.root)
        self.assertEqual([receipt], reader._validated_history("recipient", reader._frames()))
        before = self.tree()
        with self.assertRaises(FloatiError):
            self.plan()
        self.assertEqual(before, self.tree())

    def test_node_directory_replaced_between_stat_and_open_is_not_listed(self):
        self.prepare(worker="worker-a")
        node = self.root.tenant_home / "receipts/consumed/recipient"
        # acks, deliveries, and wake also have a directory named recipient.
        # Only an open whose parent is the consumed directory is the
        # stat-then-open window for this node.
        consumed_identity = (node.parent.stat().st_dev, node.parent.stat().st_ino)
        foreign = self.root.tenant_home.parent / "foreign-node"
        foreign.mkdir()
        (foreign / "sentinel").write_bytes(b"not admitted")
        foreign_identity = (foreign.stat().st_dev, foreign.stat().st_ino)
        real_open, real_scandir = os.open, os.scandir
        swapped, listed = [], []
        def changed_open(path, flags, *args, **kwargs):
            directory = kwargs.get("dir_fd")
            if path == "recipient" and flags & os.O_DIRECTORY and isinstance(directory, int) and not swapped:
                parent = os.fstat(directory)
                if (parent.st_dev, parent.st_ino) == consumed_identity:
                    node.rename(node.with_name("saved-recipient"))
                    foreign.rename(node)
                    swapped.append(True)
            return real_open(path, flags, *args, **kwargs)
        def observed_scandir(path):
            if isinstance(path, int):
                info = os.fstat(path)
                if (info.st_dev, info.st_ino) == foreign_identity:
                    listed.append(True)
                    self.fail("replacement directory was listed before its opened identity was bound")
            return real_scandir(path)
        with mock.patch.object(os, "open", changed_open), mock.patch.object(os, "scandir", observed_scandir):
            with self.assertRaises(ProtocolRefusal) as caught:
                self.plan()
        self.assertEqual("archive_consumption_checkpoint_blocked", caught.exception.code)
        self.assertTrue(swapped)
        self.assertEqual([], listed)
        self.assertEqual(b"not admitted", (node / "sentinel").read_bytes())

    def test_post_census_swap_after_resolve_cannot_make_native_raw_list_foreign_directory(self):
        self.prepare(worker="worker-a")
        node = self.root.tenant_home / "receipts/consumed/recipient"
        relative = Path("receipts/consumed/recipient")
        foreign = self.root.tenant_home.parent / "foreign-after-resolve"
        foreign.mkdir()
        (foreign / "sentinel").write_bytes(b"not admitted")
        resolved = self.root.resolve_relative(relative)
        native_raw, native_resolve, real_scandir = MessageConsumption._raw, FloatiRoot.resolve_relative, os.scandir
        swapped, listed = [], []
        def stale_resolve(root, name):
            return resolved if Path(name) == relative else native_resolve(root, name)
        def changed_raw(reader, name, **kwargs):
            if not swapped:
                node.rename(node.with_name("saved-recipient"))
                node.symlink_to(foreign, target_is_directory=True)
                swapped.append(True)
            return native_raw(reader, name, **kwargs)
        def observed_scandir(path):
            if not isinstance(path, int) and Path(path) == node and swapped:
                listed.append(True)
                self.fail("native _raw followed a path resolved before namespace substitution")
            return real_scandir(path)
        accessor = getattr(node, "_accessor", None)
        accessor_patch = (mock.patch.object(accessor, "scandir", observed_scandir)
                          if accessor is not None else nullcontext())
        with mock.patch.object(MessageConsumption, "_raw", changed_raw),\
                mock.patch.object(FloatiRoot, "resolve_relative", stale_resolve),\
                mock.patch.object(os, "scandir", observed_scandir), accessor_patch:
            with self.assertRaises(ProtocolRefusal) as caught:
                self.plan()
        self.assertEqual("archive_consumption_checkpoint_blocked", caught.exception.code)
        self.assertTrue(swapped)
        self.assertEqual([], listed)
        self.assertEqual(b"not admitted", (foreign / "sentinel").read_bytes())

    def test_captured_member_replaced_before_open_is_not_read(self):
        self.prepare(worker="worker-a")
        source = self.root.tenant_home / MessageConsumption(self.root)._relative("recipient", "worker-a")
        foreign = self.root.tenant_home.parent / "foreign-member"
        foreign.write_bytes(b"foreign bytes must not be read")
        foreign_identity = (foreign.stat().st_dev, foreign.stat().st_ino)
        native_open, native_read = os.open, os.read
        swapped, reads = [], []
        def changed_open(path, flags, *args, **kwargs):
            if path == source.name and kwargs.get("dir_fd") is not None and not swapped:
                source.rename(source.with_suffix(".saved"))
                foreign.rename(source)
                swapped.append(True)
            return native_open(path, flags, *args, **kwargs)
        def observed_read(descriptor, size):
            info = os.fstat(descriptor)
            if (info.st_dev, info.st_ino) == foreign_identity:
                reads.append(True)
                self.fail("replacement member bytes were read before identity binding")
            return native_read(descriptor, size)
        with mock.patch.object(os, "open", changed_open), mock.patch.object(os, "read", observed_read):
            with self.assertRaises(ProtocolRefusal) as caught:
                self.plan()
        self.assertEqual("archive_consumption_checkpoint_blocked", caught.exception.code)
        self.assertTrue(swapped)
        self.assertEqual([], reads)
        self.assertEqual(b"foreign bytes must not be read", source.read_bytes())

    def test_root_replacement_cannot_rebind_census_away_from_owned_epoch_descriptor(self):
        self.prepare()
        home = self.root.tenant_home
        foreign = home.parent / "foreign-root"
        consumed = foreign / "receipts/consumed"
        consumed.mkdir(parents=True)
        (consumed / "sentinel").write_bytes(b"unowned root")
        identity = (consumed.stat().st_dev, consumed.stat().st_ino)
        native = checkpoint._consumed_sources
        native_scandir = os.scandir
        swapped = []
        def changed(root, **kwargs):
            if not swapped:
                home.rename(home.with_name("saved-root"))
                foreign.rename(home)
                swapped.append(True)
            return native(root, **kwargs)
        def observed(path):
            if isinstance(path, int):
                info = os.fstat(path)
                if (info.st_dev, info.st_ino) == identity:
                    self.fail("census adopted a root not owned by the EPOCH descriptor")
            return native_scandir(path)
        with mock.patch.object(checkpoint, "_consumed_sources", changed), mock.patch.object(os, "scandir", observed):
            with self.assertRaises(FloatiError):
                self.plan()
        self.assertTrue(swapped)
        self.assertEqual(b"unowned root", (home / "receipts/consumed/sentinel").read_bytes())

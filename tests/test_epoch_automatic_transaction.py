"""Automatic carries must survive the native epoch transaction and its recovery.

These regressions are authored before the transaction extension. The prior
private native probe witnesses the current pending-mail loss; execution of
this module awaits the coordinated managed repository battery.
"""

from datetime import datetime, timezone
import os
import stat
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from floati import bus_epoch
from floati.cursor import SparseCursor
from floati.errors import DurabilityFailure, IntegrityFailure, ProtocolRefusal
from floati.events import EventLog
from floati.framing import decode_frames, encode_frame
from floati.planes import AuthorityGrantStore
from floati.registry import Registry
from floati.root import FloatiRoot
from tests.temp_roots import REAL_TEMP_ROOT


class AutomaticEpochTransactionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
        self.addCleanup(temporary.cleanup)
        self.root = FloatiRoot.open_direct_home(Path(temporary.name) / "fleet", create=True)
        for node in ("actor", "seat-a", "seat-b"):
            Registry(self.root).register(node, "Codex")
        self.messages = {}
        for node in ("seat-a", "seat-b"):
            done = EventLog(self.root).send("actor", node, "floati", "a" * 40,
                "AGENTS.md", "completed history", idempotency_key="done-" + node)
            EventLog(self.root).present(node)
            SparseCursor(self.root).ack(node, [done["id"]], acting_session_id="before-carry")
            sent = EventLog(self.root).send("actor", node, "floati", "a" * 40,
                "AGENTS.md", "pending transaction fixture", idempotency_key="pending-" + node)
            EventLog(self.root).present(node)
            self.messages[node] = sent["id"]
        self.before = self.selected()

    def selected(self):
        return {path.relative_to(self.root.path).as_posix(): path.read_bytes()
                for path in self.root.path.rglob("*.jsonl")
                if path == self.root.path / "events.jsonl"
                or path.relative_to(self.root.path).parts[:2] in (
                    ("receipts", "deliveries"), ("receipts", "acks"))}

    def planner(self, root, old):
        self.assertEqual(self.root, root)
        self.assertEqual(self.before, {relative: payload for relative, _plane, payload in old})
        # Keep actual native bytes. The separate carry planner owns dependency
        # selection; this fixture isolates installation from that selection.
        return old, {"trigger": "send", "policy": {"max_bytes": 1,
            "max_age_days": 7, "source": "operator_declared"},
            "observed_bytes": len(self.before["events.jsonl"]),
            "observed_at": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "epoch_started_at": None, "due_reasons": ["bytes"]}

    def pruning_planner(self, root, old):
        _retained, metadata = self.planner(root, old)
        pending = set(self.messages.values())
        retained = []
        for relative, plane, payload in old:
            if plane == "acks":
                continue
            lines = []
            for line in payload.splitlines(keepends=True):
                row = decode_frames(line)[0]
                if (row["id"] in pending or row.get("envelope_id") in pending
                        or set(row.get("item_ids", ())).intersection(pending)):
                    lines.append(line)
            if lines or plane == "events":
                retained.append((relative, plane, b"".join(lines)))
        return tuple(retained), metadata

    def roll(self, planner=None, **kwargs):
        operation = getattr(bus_epoch, "roll_automatic_epoch", None)
        self.assertIsNotNone(operation, "automatic live-payload transaction is absent")
        return operation(self.root, actor="actor", planner=planner or self.planner, **kwargs)

    def assert_complete(self, receipt, retained=None):
        target = self.before if retained is None else {relative: payload for relative, _plane, payload in retained}
        frames = decode_frames((self.root.path / "events.jsonl").read_bytes())
        self.assertEqual(receipt, frames[0])
        from floati.events import validate_event_records
        validate_event_records(frames)
        carry = [row for row in frames if row["kind"] == "epoch_carry_verification"]
        self.assertEqual(1, len(carry))
        self.assertEqual(receipt["id"], carry[0]["roll_receipt_id"])
        self.assertEqual(target["events.jsonl"], b"".join(encode_frame(row) for row in frames[1:]
            if row["kind"] != "epoch_carry_verification"))
        for relative, payload in self.before.items():
            self.assertEqual(payload, (Path(receipt["archive_path"]) / relative).read_bytes())
            if relative != "events.jsonl":
                if relative in target:
                    self.assertEqual(target[relative], (self.root.path / relative).read_bytes())
                else:
                    self.assertFalse((self.root.path / relative).exists())
        self.assertEqual([], list(self.root.path.glob(".floati-epoch-*")))
        for node, item_id in self.messages.items():
            self.assertEqual([item_id], EventLog(self.root).unacked_ids(node))
            SparseCursor(self.root).ack(node, [item_id], acting_session_id="after-carry")
            self.assertEqual([], EventLog(self.root).unacked_ids(node))

    def interrupt(self, point, planner=None):
        def fault(boundary):
            if boundary == point:
                raise RuntimeError("native interruption " + point)
        with self.assertRaisesRegex(RuntimeError, "native interruption " + point):
            self.roll(planner=planner, fault=fault)
        return bus_epoch.observe_epoch_roll_state(self.root)

    def test_active_actor_without_manual_grant_carries_pending_native_ack_eligibility(self):
        self.assert_complete(self.roll()["receipt"])

    def test_current_no_op_does_not_create_a_marker_or_archive(self):
        operation = getattr(bus_epoch, "roll_automatic_epoch", None)
        self.assertIsNotNone(operation)
        self.assertIsNone(operation(self.root, actor="actor", planner=lambda root, old: None))
        self.assertEqual(self.before, self.selected())
        self.assertEqual([], list(self.root.path.glob("archive-*")))

    def test_large_carry_verification_fits_one_row_and_preserves_exact_native_frames(self):
        import hashlib
        from floati.epoch_teardown import carried_envelope_digests
        from floati.events import EVENT_KINDS
        from floati.ids import uuid7_hex
        from floati.jsonl import MAX_LEDGER_BYTES, MAX_RECORD_BYTES
        from floati.records import validate_record

        template = next(row for row in decode_frames(self.before["events.jsonl"])
                        if row["id"] == self.messages["seat-a"])
        frames = []
        # Individual native messages fit, as does their entire ledger. Their
        # combined roll inventory exceeds the one-record byte bound.
        for ordinal in range(1000):
            row = validate_record(dict(template, id="msg-" + uuid7_hex(),
                idempotency_key="carry-capacity-" + str(ordinal)),
                self.root.tenant_id, EVENT_KINDS, integrity=False)
            frame = encode_frame(row)
            self.assertLessEqual(len(frame), MAX_RECORD_BYTES)
            frames.append(frame)
        events = self.before["events.jsonl"] + b"".join(frames)
        self.assertLess(len(events), MAX_LEDGER_BYTES)
        with bus_epoch.epoch_guard(self.root, exclusive=True):
            (self.root.path / "events.jsonl").write_bytes(events)
        self.before = self.selected()

        receipt = self.roll()["receipt"]
        live = self.selected()
        current = decode_frames(live["events.jsonl"])
        self.assertEqual(receipt, current[0])
        verification, = [row for row in current if row["kind"] == "epoch_carry_verification"]
        self.assertLessEqual(len(encode_frame(verification)), MAX_RECORD_BYTES)
        self.assertEqual({row["id"]: hashlib.sha256(frame).hexdigest()
                          for frame, row in zip(events.splitlines(keepends=True), decode_frames(events))
                          if row["kind"] == "message_envelope"}, carried_envelope_digests(current))
        self.assertEqual(self.before["events.jsonl"], b"".join(frame for frame, row in zip(
            live["events.jsonl"].splitlines(keepends=True)[1:], current[1:])
            if row["kind"] != "epoch_carry_verification"))
        for relative, payload in self.before.items():
            self.assertEqual(payload, (Path(receipt["archive_path"]) / relative).read_bytes())
            if relative != "events.jsonl":
                self.assertEqual(payload, live[relative])
        self.assertEqual([], list(self.root.path.glob(".floati-epoch-*")))

    def test_manual_api_still_requires_its_grant(self):
        with self.assertRaises(ProtocolRefusal):
            bus_epoch.roll_bus_epoch(self.root, actor="actor", idempotency_key="manual")
        self.assertEqual(self.before, self.selected())

    def test_committed_before_first_write_recovers_carry(self):
        self.interrupt("marker_committed_durable")
        recovered = bus_epoch.reconcile_epoch_roll(self.root)
        self.assertEqual("roll_forward", recovered["direction"])
        self.assert_complete(recovered["receipt"])

    def test_partial_events_write_recovers_carry(self):
        self.interrupt("live_events_replaced")
        recovered = bus_epoch.reconcile_epoch_roll(self.root)
        self.assert_complete(recovered["receipt"])

    def test_complete_target_before_marker_cleanup_recovers_carry(self):
        self.interrupt("live_epoch_parent_synced")
        recovered = bus_epoch.reconcile_epoch_roll(self.root)
        self.assert_complete(recovered["receipt"])

    def assert_recovery_sync_before_marker_retirement(self, path):
        identity = (path.stat().st_dev, path.stat().st_ino)
        trace = []
        original_sync, original_unlink = os.fsync, os.unlink
        def sync(descriptor):
            info = os.fstat(descriptor)
            original_sync(descriptor)
            trace.append(("sync", info.st_dev, info.st_ino, stat.S_ISDIR(info.st_mode)))
        def unlink(path, *args, **kwargs):
            original_unlink(path, *args, **kwargs)
            if Path(path).name.startswith(".floati-epoch-roll-"):
                trace.append(("marker_retired",))
        with mock.patch("os.fsync", side_effect=sync), mock.patch("os.unlink", side_effect=unlink):
            recovered = bus_epoch.reconcile_epoch_roll(self.root)
        retirement = next(index for index, entry in enumerate(trace) if entry[0] == "marker_retired")
        self.assertTrue(any(entry[:3] == ("sync", *identity) for entry in trace[:retirement]),
                        "target file/retirement parent was not synced before marker retirement")
        return recovered

    def test_visible_complete_events_write_is_synced_again_before_recovery_retires_marker(self):
        self.interrupt("live_events_write_1")
        recovered = self.assert_recovery_sync_before_marker_retirement(self.root.path / "events.jsonl")
        self.assert_complete(recovered["receipt"])

    def nested_retirement_sync(self, plane):
        sent = EventLog(self.root).send("actor", "seat-a", "floati", "a" * 40,
            "AGENTS.md", "closed nested session", idempotency_key="nested-completed",
            worker_session_id="nested-worker")
        EventLog(self.root).present("seat-a", worker_session_id="nested-worker")
        cursor = SparseCursor(self.root)
        cursor.ack("seat-a", [sent["id"]], acting_session_id="nested-ack", worker_session_id="nested-worker")
        path = (cursor.path_for("seat-a", worker_session_id="nested-worker") if plane == "acks"
                else cursor.delivery_path_for("seat-a", worker_session_id="nested-worker"))
        self.before = self.selected()
        index = sorted(self.before).index(path.relative_to(self.root.path).as_posix())
        old = tuple((relative, "events" if relative == "events.jsonl" else Path(relative).parts[1], payload)
                    for relative, payload in sorted(self.before.items()))
        retained, _metadata = self.pruning_planner(self.root, old)
        self.interrupt(f"live_member_{index}_retired", planner=self.pruning_planner)
        self.assertFalse(path.exists())
        recovered = self.assert_recovery_sync_before_marker_retirement(path.parent)
        self.assert_complete(recovered["receipt"], retained)

    def test_already_retired_only_nested_ack_syncs_its_parent_during_recovery(self):
        self.nested_retirement_sync("acks")

    def test_already_retired_last_nested_delivery_syncs_its_parent_during_recovery(self):
        self.nested_retirement_sync("deliveries")

    def cleanup_change(self, *, replace_directory):
        saved = {}
        def fault(point):
            if point == "marker_committed_durable":
                observed = bus_epoch.observe_epoch_roll_state(self.root)
                saved["staging"] = Path(observed["marker"]["staging_path"])
            elif point == "automatic_marker_retirement_synced":
                staging = saved["staging"]
                if replace_directory:
                    staging.rename(staging.with_name(staging.name + "-original"))
                    staging.mkdir()
                    saved["changed"] = staging / "foreign"
                else:
                    saved["changed"] = staging / "events.jsonl"
                saved["changed"].write_bytes(b"preserve changed staging bytes\n")
        with self.assertRaises((IntegrityFailure, ProtocolRefusal)):
            self.roll(fault=fault)
        self.assertEqual(b"preserve changed staging bytes\n", saved["changed"].read_bytes())
        self.assertEqual("absent", bus_epoch.observe_epoch_roll_state(self.root)["classification"])
        for node, item_id in self.messages.items():
            self.assertEqual([item_id], EventLog(self.root).unacked_ids(node))

    def test_replaced_staging_tree_survives_post_retirement_cleanup_refusal(self):
        self.cleanup_change(replace_directory=True)

    def test_changed_staging_descendant_survives_post_retirement_cleanup_refusal(self):
        self.cleanup_change(replace_directory=False)

    def pruning_recovery(self, point):
        old = tuple((relative, "events" if relative == "events.jsonl" else Path(relative).parts[1], payload)
                    for relative, payload in sorted(self.before.items()))
        retained, _metadata = self.pruning_planner(self.root, old)
        self.interrupt(point, planner=self.pruning_planner)
        recovered = bus_epoch.reconcile_epoch_roll(self.root)
        self.assert_complete(recovered["receipt"], retained)

    def test_truncated_retained_delivery_recovers_exact_pending_ack_proof(self):
        self.pruning_recovery("live_member_3_replaced")

    def test_actual_short_delivery_write_recovers_exact_pending_ack_proof(self):
        inode = (self.root.path / "receipts/deliveries/seat-a.jsonl").stat().st_ino
        original_write = os.write
        def short_write(descriptor, payload):
            if os.fstat(descriptor).st_ino == inode:
                payload = payload[:max(1, len(payload) // 2)]
            return original_write(descriptor, payload)
        with mock.patch("os.write", side_effect=short_write):
            self.interrupt("live_member_3_write_1", planner=self.pruning_planner)
        current = (self.root.path / "receipts/deliveries/seat-a.jsonl").stat().st_size
        self.assertGreater(current, 0)
        self.assertLess(current, len(self.before["receipts/deliveries/seat-a.jsonl"]))
        old = tuple((relative, "events" if relative == "events.jsonl" else Path(relative).parts[1], payload)
                    for relative, payload in sorted(self.before.items()))
        retained, _metadata = self.pruning_planner(self.root, old)
        self.assert_complete(bus_epoch.reconcile_epoch_roll(self.root)["receipt"], retained)

    def test_first_omitted_ack_retirement_recovers_pending_work(self):
        self.pruning_recovery("live_member_1_retired")

    def test_second_omitted_ack_retirement_recovers_pending_work(self):
        self.pruning_recovery("live_member_2_retired")

    def test_changed_other_selected_file_is_detected_before_events_replacement(self):
        before = {}
        def fault(point):
            if point == "marker_committed_durable":
                path = self.root.path / "receipts/deliveries/seat-b.jsonl"
                path.write_bytes(path.read_bytes() + b"changed native fixture\n")
                before.update(self.selected())
        with self.assertRaises((DurabilityFailure, IntegrityFailure, ProtocolRefusal)):
            self.roll(fault=fault)
        self.assertEqual(before, self.selected())

    def test_archive_collision_rebinds_receipt_and_staged_target(self):
        foreign = []
        def fault(point):
            if point == "before_archive_staging":
                observed = bus_epoch.observe_epoch_roll_state(self.root)
                path = Path(observed["marker"]["archive_path"])
                path.mkdir()
                (path / "foreign").write_bytes(b"preserve collision")
                foreign.append(path)
        result = self.roll(fault=fault)
        self.assertNotEqual(foreign[0], Path(result["receipt"]["archive_path"]))
        self.assertEqual(b"preserve collision", (foreign[0] / "foreign").read_bytes())
        self.assert_complete(result["receipt"])

    def test_staging_cleanup_interruption_cannot_leave_a_committed_marker(self):
        observed = self.interrupt("automatic_marker_retirement_synced")
        self.assertEqual("absent", observed["classification"])
        self.assertIsNone(bus_epoch.reconcile_epoch_roll(self.root))
        for node, item_id in self.messages.items():
            self.assertEqual([item_id], EventLog(self.root).unacked_ids(node))
            SparseCursor(self.root).ack(node, [item_id], acting_session_id="after-cleanup-interruption")
        self.assertEqual(1, len(list(self.root.path.glob(".floati-epoch-staging-*"))))

    def test_changed_staging_descendant_refuses_without_live_replacement(self):
        observed = self.interrupt("marker_committed_durable")
        staged = Path(observed["marker"]["staging_path"]) / "events.jsonl"
        payload = staged.read_bytes()
        staged.write_bytes(payload[:-2] + b"x\n")
        before = self.selected()
        with self.assertRaises((IntegrityFailure, ProtocolRefusal)):
            bus_epoch.reconcile_epoch_roll(self.root)
        self.assertEqual(before, self.selected())

    def test_unreplaced_native_append_rolls_back_without_losing_mail(self):
        observed = self.interrupt("marker_committed_durable")
        # Simulate the admitted old writer without the new automatic repair
        # decorator; the explicit reconcile below owns this fixture boundary.
        with mock.patch("floati.bus_epoch._require_epoch_writer_admission", return_value=None):
            extra = EventLog.send.__wrapped__(EventLog(self.root), "actor", "seat-a", "floati", "b" * 40,
                "AGENTS.md", "native old-writer tail", idempotency_key="old-tail")
        before = self.selected()
        recovered = bus_epoch.reconcile_epoch_roll(self.root)
        self.assertEqual("rollback", recovered["direction"])
        self.assertEqual(before, self.selected())
        self.assertIn(extra["id"], EventLog(self.root).unacked_ids("seat-a"))
        self.assertFalse(Path(observed["marker"]["archive_path"]).exists())

    def test_same_size_live_rewrite_refuses_without_overwriting_it(self):
        self.interrupt("marker_committed_durable")
        path = self.root.path / "events.jsonl"
        payload = path.read_bytes()
        path.write_bytes(payload.replace(b"pending transaction", b"changed transaction", 1))
        self.assertEqual(len(payload), path.stat().st_size)
        before = self.selected()
        with self.assertRaises(IntegrityFailure):
            bus_epoch.reconcile_epoch_roll(self.root)
        self.assertEqual(before, self.selected())

    def test_complete_target_plus_new_tail_refuses_without_overwriting_it(self):
        self.interrupt("live_epoch_parent_synced")
        # Preserve the deliberately appended target tail until the explicit
        # recovery call, rather than invoking automatic repair while seeding it.
        with mock.patch("floati.bus_epoch._require_epoch_writer_admission", return_value=None):
            EventLog.send.__wrapped__(EventLog(self.root), "actor", "seat-a", "floati", "b" * 40,
                "AGENTS.md", "after-target tail", idempotency_key="after-target")
        before = self.selected()
        with self.assertRaises(IntegrityFailure):
            bus_epoch.reconcile_epoch_roll(self.root)
        self.assertEqual(before, self.selected())

    def test_legacy_committed_manual_marker_still_recovers_receipt_only(self):
        AuthorityGrantStore(self.root).grant_exact("bus-epoch-roll", "actor", 1,
                                                   datetime.now(timezone.utc))
        def fault(point):
            if point == "marker_committed_durable":
                raise RuntimeError("manual interruption")
        with self.assertRaisesRegex(RuntimeError, "manual interruption"):
            bus_epoch.roll_bus_epoch(self.root, actor="actor", idempotency_key="manual", fault=fault)
        result = bus_epoch.reconcile_epoch_roll(self.root)
        self.assertEqual(encode_frame(result["receipt"]), (self.root.path / "events.jsonl").read_bytes())
        self.assertEqual([], EventLog(self.root).unacked_ids("seat-a"))

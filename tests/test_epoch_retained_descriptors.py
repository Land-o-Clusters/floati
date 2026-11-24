"""Automatic swaps must bind after opening and refuse every post-bind change.

Authored after a native injected RED and before the retained-descriptor repair.
Execution belongs to the managed full-discovery battery.
"""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import errno
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest import mock

from floati import bus_epoch, epoch_rotation, registry
from floati.cursor import SparseCursor
from floati.errors import DurabilityFailure, IntegrityFailure, ProtocolRefusal
from floati.events import EventLog
from floati.framing import decode_frames, encode_frame
from floati.registry import Registry
from floati.root import FloatiRoot
from tests.temp_roots import REAL_TEMP_ROOT


class RetainedEpochDescriptorTests(unittest.TestCase):
    def fixture(self, count):
        temporary = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
        self.addCleanup(temporary.cleanup)
        root = FloatiRoot.open_direct_home(Path(temporary.name) / "fleet", create=True)
        Registry(root).register("actor", "Codex")
        pending = {}
        for index in range((count - 1) // 2):
            node = "seat-%02d" % index
            Registry(root).register(node, "Codex")
            done = EventLog(root).send("actor", node, "floati", "a" * 40, "AGENTS.md",
                "closed native history", idempotency_key="done-" + node)
            EventLog(root).present(node)
            SparseCursor(root).ack(node, [done["id"]], acting_session_id="before")
            pending[node] = EventLog(root).send("actor", node, "floati", "a" * 40,
                "AGENTS.md", "pending native history", idempotency_key="pending-" + node)["id"]
            EventLog(root).present(node)
        selected = bus_epoch._preflight_selected_members(root)
        self.assertEqual(count, len(selected))
        old = {relative: path.read_bytes() for relative, path, _plane, _identity in selected}
        return root, pending, old

    @staticmethod
    def identity(info):
        return (info.st_dev, info.st_ino, info.st_mode, info.st_nlink,
                info.st_size, info.st_mtime_ns, info.st_ctime_ns)

    def planner(self, old):
        def plan(root, payloads):
            self.assertEqual(old, {relative: payload for relative, _plane, payload in payloads})
            return payloads, {"trigger": "send", "policy": {"max_bytes": 1,
                "max_age_days": 7, "source": "operator_declared"},
                "observed_bytes": len(old["events.jsonl"]),
                "observed_at": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
                "epoch_started_at": None, "due_reasons": ["bytes"]}
        return plan

    def committed(self, root, old):
        def fault(point):
            if point == "marker_committed_durable":
                raise RuntimeError("intentional committed interruption")
        with self.assertRaisesRegex(RuntimeError, "intentional committed interruption"):
            bus_epoch.roll_automatic_epoch(root, actor="actor", planner=self.planner(old), fault=fault)

    @contextmanager
    def first_open_touch(self, root, old):
        native_open = os.open
        remaining = {root.path / relative for relative in old}
        touched, descriptors = [], []

        def open_file(path, flags, *args, **kwargs):
            coordinate = Path(path) if isinstance(path, (str, os.PathLike)) else None
            measured = coordinate in remaining and flags & os.O_ACCMODE == os.O_RDWR
            descriptor = native_open(path, flags, *args, **kwargs)
            if measured:
                try:
                    before = os.fstat(descriptor)
                    payload = os.pread(descriptor, before.st_size + 1, 0)
                    os.fchmod(descriptor, stat.S_IMODE(before.st_mode))
                    after = os.fstat(descriptor)
                    self.assertEqual(self.identity(before)[:6], self.identity(after)[:6])
                    self.assertNotEqual(before.st_ctime_ns, after.st_ctime_ns)
                    self.assertEqual(payload, os.pread(descriptor, after.st_size + 1, 0))
                    remaining.remove(coordinate)
                    touched.append(coordinate)
                    descriptors.append(descriptor)
                except BaseException:
                    os.close(descriptor)
                    raise
            return descriptor

        with mock.patch("os.open", side_effect=open_file):
            yield touched, descriptors

    def assert_closed(self, descriptors):
        for descriptor in descriptors:
            with self.assertRaises(OSError) as error:
                os.fstat(descriptor)
            self.assertEqual(errno.EBADF, error.exception.errno)

    def assert_continuity(self, root, pending, old):
        self.assertEqual("absent", bus_epoch.observe_epoch_roll_state(root)["classification"])
        archives = list(root.path.glob("archive-*"))
        self.assertEqual(1, len(archives))
        bus_epoch.verify_epoch_archive(archives[0])
        payloads, _snapshot = bus_epoch._read_epoch_archive_payloads(archives[0])
        stored = {name: raw for name, _plane, raw in payloads}
        for relative, payload in old.items():
            self.assertEqual(payload, stored[relative])
        for node, message_id in pending.items():
            messages, _receipt = EventLog(root).present(node)
            ids = [message["id"] for message in messages]
            self.assertIn(message_id, ids)
            SparseCursor(root).ack(node, ids, acting_session_id="after")
            self.assertEqual([], EventLog(root).unacked_ids(node))

    def test_normal_k5_and_k21_open_touch_complete_one_native_transaction(self):
        for count in (21,):
            with self.subTest(members=count):
                root, pending, old = self.fixture(count)
                with self.first_open_touch(root, old) as (touched, descriptors):
                    result = bus_epoch.roll_automatic_epoch(root, actor="actor", planner=self.planner(old))
                self.assertEqual(count, len(touched))
                self.assert_closed(descriptors)
                self.assertEqual(result["receipt"], decode_frames((root.path / "events.jsonl").read_bytes())[0])
                self.assert_continuity(root, pending, old)

    def test_committed_k5_and_k21_open_touch_complete_on_first_public_send(self):
        for count in (21,):
            with self.subTest(members=count):
                root, pending, old = self.fixture(count)
                self.committed(root, old)
                with self.first_open_touch(root, old) as (touched, descriptors):
                    result = EventLog(root).send("actor", sorted(pending)[0], "floati", "b" * 40,
                        "AGENTS.md", "one public recovery invocation", idempotency_key="after")
                self.assertEqual("message_envelope", result["kind"])
                self.assertEqual(count, len(touched))
                self.assert_closed(descriptors)
                self.assert_continuity(root, pending, old)

    def test_due_normal_public_send_binds_after_first_open_touch(self):
        for count in (21,):
            with self.subTest(members=count):
                root, pending, old = self.fixture(count)
                future = datetime.now(timezone.utc) + timedelta(days=8)
                before_maintenance = {}
                native_maintenance = epoch_rotation.maintenance

                def observe_maintenance(current, **kwargs):
                    before_maintenance.update({relative: path.read_bytes()
                        for relative, path, _plane, _identity in bus_epoch._preflight_selected_members(current)})
                    return native_maintenance(current, **kwargs)

                class FutureClock(datetime):
                    @classmethod
                    def now(cls, tz=None):
                        return cls.fromtimestamp(future.timestamp(), tz=tz)

                with self.first_open_touch(root, old) as (touched, descriptors),\
                        mock.patch.object(epoch_rotation, "datetime", FutureClock),\
                        mock.patch.object(epoch_rotation, "maintenance", side_effect=observe_maintenance),\
                        mock.patch.object(registry, "utc_now", return_value=future.isoformat(timespec="milliseconds").replace("+00:00", "Z")):
                    sent = EventLog(root).send("actor", sorted(pending)[0], "floati", "b" * 40,
                        "AGENTS.md", "native age-due transaction", idempotency_key="after")
                self.assertEqual("message_envelope", sent["kind"])
                self.assertTrue(before_maintenance["events.jsonl"].startswith(old["events.jsonl"]))
                appended = decode_frames(before_maintenance["events.jsonl"][len(old["events.jsonl"]):])
                self.assertIn(sent, appended)
                self.assertGreater(len(touched), 0)
                self.assert_closed(descriptors)
                self.assert_continuity(root, pending, before_maintenance)

    def test_post_bind_member_metadata_content_and_path_changes_are_not_overwritten(self):
        for mutation in ("metadata", "content", "path"):
            with self.subTest(mutation=mutation):
                root, _pending, old = self.fixture(5)
                relative = sorted(set(old) - {"events.jsonl"})[0]
                path = root.path / relative
                saved = {}

                def fault(point):
                    if point != "live_events_synced":
                        return
                    if mutation == "metadata":
                        os.chmod(path, stat.S_IMODE(path.stat().st_mode))
                    elif mutation == "content":
                        path.write_bytes(b"foreign replacement bytes\n")
                    else:
                        saved["detached"] = path.with_name(path.name + ".foreign-original")
                        path.rename(saved["detached"])
                        path.write_bytes(old[relative])
                    saved["identity"] = self.identity(path.lstat())
                    saved["bytes"] = path.read_bytes()

                with self.first_open_touch(root, old) as (_touched, descriptors):
                    with self.assertRaises(DurabilityFailure) as error:
                        bus_epoch.roll_automatic_epoch(root, actor="actor", planner=self.planner(old), fault=fault)
                self.assertEqual("epoch_selected_member_changed", error.exception.code)
                self.assertEqual(saved["bytes"], path.read_bytes())
                self.assertEqual(saved["identity"], self.identity(path.lstat()))
                if "detached" in saved:
                    self.assertEqual(old[relative], saved["detached"].read_bytes())
                self.assert_closed(descriptors)
                self.assertEqual("COMMITTED", bus_epoch.observe_epoch_roll_state(root)["classification"])

    def test_changed_archive_or_stage_still_refuses_before_next_member(self):
        for evidence in ("archive_path", "staging_path"):
            with self.subTest(evidence=evidence):
                root, _pending, old = self.fixture(5)
                relative = sorted(set(old) - {"events.jsonl"})[0]
                changed = {}

                def fault(point):
                    if point == "live_events_synced":
                        marker = bus_epoch.observe_epoch_roll_state(root)["marker"]
                        changed["path"] = Path(marker[evidence]) / relative
                        changed["path"].write_bytes(b"foreign frozen evidence\n")

                with self.assertRaises((IntegrityFailure, ProtocolRefusal)):
                    bus_epoch.roll_automatic_epoch(root, actor="actor", planner=self.planner(old), fault=fault)
                self.assertEqual(b"foreign frozen evidence\n", changed["path"].read_bytes())
                self.assertEqual(old[relative], (root.path / relative).read_bytes())

    def test_same_bytes_rename_during_preopen_refuses_before_marker_or_archive(self):
        root, _pending, old = self.fixture(5)
        events = root.path / "events.jsonl"
        native_open = os.open
        replaced, descriptors = [], []

        def open_file(path, flags, *args, **kwargs):
            descriptor = native_open(path, flags, *args, **kwargs)
            if Path(path) == events and flags & os.O_ACCMODE == os.O_RDWR and not replaced:
                descriptors.append(descriptor)
                detached = events.with_name("events.detached")
                events.rename(detached)
                events.write_bytes(old["events.jsonl"])
                replaced.append(detached)
            return descriptor

        with mock.patch("os.open", side_effect=open_file):
            with self.assertRaises(DurabilityFailure) as error:
                bus_epoch.roll_automatic_epoch(root, actor="actor", planner=self.planner(old))
        self.assertEqual("epoch_selected_member_changed", error.exception.code)
        self.assertTrue(replaced)
        self.assertEqual(old["events.jsonl"], events.read_bytes())
        self.assertEqual(old["events.jsonl"], replaced[0].read_bytes())
        self.assertEqual("absent", bus_epoch.observe_epoch_roll_state(root)["classification"])
        self.assertEqual([], list(root.path.glob("archive-*")))
        self.assert_closed(descriptors)

    def test_preopen_failure_closes_earlier_descriptors_before_any_marker(self):
        root, _pending, old = self.fixture(5)
        targets = {root.path / relative for relative in old}
        native_open, descriptors = os.open, []

        def open_file(path, flags, *args, **kwargs):
            measured = Path(path) in targets and flags & os.O_ACCMODE == os.O_RDWR
            if measured and descriptors:
                raise OSError(errno.EMFILE, "injected descriptor exhaustion")
            descriptor = native_open(path, flags, *args, **kwargs)
            if measured:
                descriptors.append(descriptor)
            return descriptor

        with mock.patch("os.open", side_effect=open_file):
            with self.assertRaises(DurabilityFailure):
                bus_epoch.roll_automatic_epoch(root, actor="actor", planner=self.planner(old))
        self.assertEqual(1, len(descriptors))
        self.assert_closed(descriptors)
        self.assertEqual("absent", bus_epoch.observe_epoch_roll_state(root)["classification"])
        self.assertEqual([], list(root.path.glob("archive-*")))
        for relative, payload in old.items():
            self.assertEqual(payload, (root.path / relative).read_bytes())

    def test_absent_events_is_created_exclusively_only_after_commit(self):
        temporary = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
        self.addCleanup(temporary.cleanup)
        root = FloatiRoot.open_direct_home(Path(temporary.name) / "fleet", create=True)
        Registry(root).register("actor", "Codex")
        events = root.path / "events.jsonl"
        self.assertFalse(events.exists())
        native_open, opens, committed = os.open, [], []

        def plan_absent_events(current, payloads):
            retained, metadata = self.planner({"events.jsonl": b""})(current, payloads)
            # Synthetic age evidence reaches the absent-target transaction branch;
            # it does not claim an absent ledger is eligible under public policy.
            observed = datetime.fromisoformat(metadata["observed_at"].replace("Z", "+00:00"))
            metadata["epoch_started_at"] = (observed - timedelta(days=8)).isoformat(
                timespec="milliseconds").replace("+00:00", "Z")
            metadata["due_reasons"] = ["age"]
            return retained, metadata

        def open_file(path, flags, *args, **kwargs):
            if Path(path) == events and flags & os.O_ACCMODE == os.O_RDWR:
                self.assertTrue(committed)
                self.assertTrue(flags & os.O_CREAT)
                self.assertTrue(flags & os.O_EXCL)
                opens.append(flags)
            return native_open(path, flags, *args, **kwargs)

        def fault(point):
            if point == "marker_committed_durable":
                self.assertFalse(events.exists())
                self.assertEqual([], opens)
                committed.append(True)

        with mock.patch("os.open", side_effect=open_file):
            result = bus_epoch.roll_automatic_epoch(root, actor="actor",
                planner=plan_absent_events, fault=fault)
        self.assertEqual(1, len(opens))
        rows = decode_frames(events.read_bytes())
        self.assertEqual(result["receipt"], rows[0])
        self.assertEqual(["bus_epoch_roll_receipt", "epoch_carry_verification"],
                         [row["kind"] for row in rows])
        self.assertEqual([], rows[1]["carried_envelopes"])
        from floati.events import validate_event_records
        validate_event_records(rows)

    def test_invalid_or_duplicate_retained_coordinates_never_open_live_targets(self):
        for bad in ("foreign", "duplicate"):
            with self.subTest(selection=bad):
                root, _pending, old = self.fixture(5)
                native_open, opens = os.open, []

                def plan(current, payloads):
                    retained, metadata = self.planner(old)(current, payloads)
                    extra = retained[0] if bad == "duplicate" else ("../outside.jsonl", "events", b"")
                    return (*retained, extra), metadata

                def open_file(path, flags, *args, **kwargs):
                    if flags & os.O_ACCMODE == os.O_RDWR:
                        opens.append(Path(path))
                    return native_open(path, flags, *args, **kwargs)

                with mock.patch("os.open", side_effect=open_file):
                    with self.assertRaises(ProtocolRefusal):
                        bus_epoch.roll_automatic_epoch(root, actor="actor", planner=plan)
                self.assertEqual([], opens)
                self.assertEqual("absent", bus_epoch.observe_epoch_roll_state(root)["classification"])
                self.assertEqual([], list(root.path.glob("archive-*")))

    def test_active_automatic_scope_rejects_caller_rebinding_after_mutation(self):
        for mutation in ("metadata", "content"):
            with self.subTest(mutation=mutation):
                root, _pending, old = self.fixture(5)
                receipt = bus_epoch.roll_automatic_epoch(root, actor="actor", planner=self.planner(old))["receipt"]
                with bus_epoch.epoch_guard(root, exclusive=True):
                    selected = bus_epoch._preflight_selected_members(root)
                    payloads = bus_epoch._read_selected_epoch_payloads(root, selected)
                    with bus_epoch._selected_epoch_write_scope(root, selected,
                            automatic_targets=payloads) as (_bound, replace):
                        events = root.path / "events.jsonl"
                        if mutation == "metadata":
                            os.chmod(events, stat.S_IMODE(events.stat().st_mode))
                        else:
                            events.write_bytes(b"foreign bytes must survive caller rebasing\n")
                        foreign_bytes = events.read_bytes()
                        foreign_identity = self.identity(events.lstat())
                        fresh = bus_epoch._preflight_selected_members(root)
                        with self.assertRaises(DurabilityFailure) as error:
                            replace(fresh, encode_frame(receipt), target_payloads=payloads)
                        self.assertEqual("epoch_selected_member_changed", error.exception.code)
                        self.assertEqual(foreign_bytes, events.read_bytes())
                        self.assertEqual(foreign_identity, self.identity(events.lstat()))

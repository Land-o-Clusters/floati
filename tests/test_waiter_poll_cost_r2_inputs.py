"""Native epoch, ancestry and lock inputs to the composed idle cache."""
from __future__ import annotations

from tests.readiness import wait_event, wait_join
from contextlib import contextmanager, nullcontext
from datetime import datetime, timezone
import fcntl
import hashlib
import io
import os
from pathlib import Path
import stat
import threading
import unittest
from unittest import mock

from floati import bus_epoch, codex_wait, snapshot, tui
from floati.errors import IntegrityFailure
from floati.events import EventLog
from floati.planes import AuthorityGrantStore
from floati.registry import Registry
from tests import test_waiter_poll_cost_r2 as fixture


class WaiterPollCostR2InputTests(unittest.TestCase):
    def setUp(self):
        self.c = fixture.WaiterPollCostR2Tests("runTest")
        self.addCleanup(self.c.doCleanups)
        self.c.setUp()
        self.f = self.c.f

    def roll_authority(self):
        AuthorityGrantStore(self.f.root).grant_exact(bus_epoch.ROLL_AUTHORITY_SUBJECT,
            "architect", 1, datetime.now(timezone.utc))

    def assert_invalidated(self, changed):
        self.assertEqual((True, True), changed,
            "an authoritative dependency changed without invalidating the idle decision")

    @contextmanager
    def watch(self, fallback, *, workspace=None):
        with mock.patch.object(tui, "BoardFilesystemWakeup", side_effect=OSError("scratch unavailable")) if fallback else nullcontext():
            errors = io.StringIO()
            watch = codex_wait._WaiterInputsWatch(self.f.participant,
                hashlib.sha256(self.c.session.encode()).hexdigest(), errors,
                workspace=workspace)
            try:
                self.assertTrue(watch.complete)
                self.assertEqual(fallback, watch.source is None, errors.getvalue())
                watch.changed()
                yield watch
            finally:
                watch.close()

    def native_other_epoch(self, *, roll=True):
        self.f.send("r2-own-native-prepared")
        with self.c.selection() as selection:
            self.c.prepare(selection)
        other = "other-reader"
        Registry(self.f.root).register(other, "worker")
        message = EventLog(self.f.root).send("architect", other, "floati", "a" * 40,
            "docs/evidence/ping.md", "other node native archive input",
            idempotency_key="r2-other-reader")
        messages, delivery, acknowledgment = EventLog(self.f.root).drain(other,
            acting_session_id="other-reader-conversation")
        self.assertEqual([message], messages)
        self.assertEqual([message["id"]], delivery["item_ids"])
        self.assertEqual([message["id"]], acknowledgment["item_ids"])
        self.roll_authority()
        if not roll:
            return other
        receipt = bus_epoch.roll_bus_epoch(self.f.root, actor="architect",
            idempotency_key="r2-archive-dependencies")["receipt"]
        archive = bus_epoch.validate_epoch_receipt_archive(self.f.root, receipt)
        self.assertEqual(Path(receipt["archive_path"]), archive)
        return other, archive

    def partial_marker(self, fallback):
        self.c.held()
        self.roll_authority()
        events = self.f.root.resolve_relative("events.jsonl")
        before = events.read_bytes()
        writing_marker = [False]
        write = os.write

        def bounded_write(descriptor, data):
            if writing_marker[0]:
                return write(descriptor, data[:max(1, len(data) // 2)])
            return write(descriptor, data)

        def stop(boundary):
            if boundary == "marker_prepared_created":
                writing_marker[0] = True
            if boundary == "marker_prepared_write_1":
                raise OSError("scratch stopped after actual partial marker write")

        with self.c.watch(fallback) as watch:
            with mock.patch.object(os, "write", side_effect=bounded_write):
                with self.assertRaises(OSError):
                    bus_epoch.roll_bus_epoch(self.f.root, actor="architect",
                        idempotency_key="r2-partial-marker", fault=stop)
            changed = watch.changed()
        observed = bus_epoch.observe_epoch_roll_state(self.f.root)
        self.assertEqual("partial", observed["classification"])
        self.assertGreater(Path(observed["marker_path"]).stat().st_size, 0)
        self.assertEqual(before, events.read_bytes())
        self.assert_invalidated(changed)

    def archived_other_node(self, fallback, plane):
        other, archive = self.native_other_epoch()
        # Validate the composed reader against the real receipt-owned archive
        # before changing a different node's delivery or ACK namespace.
        self.assertEqual(["prepared"], [row["phase"] for row in self.c.plane.read(self.c.node)])
        target = archive / "receipts" / plane / (other + ".jsonl")
        original = target.read_bytes()
        current_events = self.f.root.resolve_relative("events.jsonl").read_bytes()
        with self.c.watch(fallback) as watch:
            target.write_bytes(original[:-1] + b"!")
            changed = watch.changed()
        self.assertEqual(len(original), target.stat().st_size)
        self.assertEqual(current_events, self.f.root.resolve_relative("events.jsonl").read_bytes())
        with self.assertRaises(IntegrityFailure):
            self.c.plane.read(self.c.node)
        self.assert_invalidated(changed)

    def dynamic_archive(self, fallback):
        other = self.native_other_epoch(roll=False)
        with self.watch(fallback) as watch:
            receipt = bus_epoch.roll_bus_epoch(self.f.root, actor="architect",
                idempotency_key="r2-roll-after-watch")["receipt"]
            archive = bus_epoch.validate_epoch_receipt_archive(self.f.root, receipt)
            self.assert_invalidated(watch.changed())
            target = archive / "receipts/deliveries" / (other + ".jsonl")
            self.assertIn(target, watch.paths, "rolled lineage was not rebound before evaluation")
            self.assertEqual((fallback, False), watch.changed(),
                "a clean rebound archive caused perpetual full evaluation")
            self.assertEqual(fallback, watch.source is None)
            original = target.read_bytes()
            target.write_bytes(original[:-1] + b"!")
            self.assert_invalidated(watch.changed())
        with self.assertRaises(IntegrityFailure):
            self.c.plane.read(self.c.node)

    def ordinary_flock(self, fallback):
        self.c.held()
        lock = self.f.root.resolve_relative("events.jsonl.lock")
        before = lock.stat()
        with self.watch(fallback) as watch:
            for _ in range(3):
                with lock.open("a+b") as handle:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                self.assertEqual((fallback, False), watch.changed(),
                    "ordinary read-transaction flock caused full idle replay")
        after = lock.stat()
        for field in ("st_dev", "st_ino", "st_mode", "st_nlink", "st_size", "st_mtime_ns", "st_ctime_ns"):
            self.assertEqual(getattr(before, field), getattr(after, field), field)

    def caller_ancestry(self, fallback, alias):
        self.c.held()
        nested = self.f.workspace / "nested"
        caller = nested / "caller"
        caller.mkdir()
        foreign = self.f.base / "foreign-caller"
        foreign.mkdir()
        (foreign / "caller").mkdir()
        sentinel = foreign / "sentinel"
        sentinel.write_bytes(b"foreign scratch sentinel")
        entries = tuple(sorted(path.name for path in foreign.iterdir()))
        with self.watch(fallback, workspace=caller) as watch:
            self.assertIn(caller, watch.observed_paths)
            if alias:
                nested.rename(self.f.workspace / "displaced-nested")
                nested.symlink_to(foreign, target_is_directory=True)
            else:
                caller.rmdir()
            self.assert_invalidated(watch.changed())
        self.assertEqual(b"foreign scratch sentinel", sentinel.read_bytes())
        self.assertEqual(entries, tuple(sorted(path.name for path in foreign.iterdir())))

    def failed_inventory(self, fallback, budget):
        self.native_other_epoch()
        with self.watch(fallback) as watch:
            error = OSError("selected archive watch budget exceeded" if budget else "scratch archive census unavailable")
            with mock.patch.object(watch, "_archive_paths", side_effect=error) as census:
                # Match the native bind path: pending-state inspection now
                # requires the retained epoch guard before any archive read.
                with bus_epoch.epoch_guard(self.f.root):
                    watch._refresh_inputs()
                self.assertFalse(watch.complete)
                self.assert_invalidated(watch.changed())
                self.assert_invalidated(watch.changed())
                self.assertGreaterEqual(census.call_count, 3)
            self.assertIsNone(watch.source, "incomplete census retained a native clean hint")

    def archive_alias_confinement(self, fallback):
        _other, archive = self.native_other_epoch()
        foreign = self.f.base / "foreign-archive"
        foreign.mkdir()
        sentinel = foreign / "sentinel"
        sentinel.write_bytes(b"foreign scratch archive sentinel")
        entries = tuple(sorted(path.name for path in foreign.iterdir()))
        foreign_identity = (foreign.stat().st_dev, foreign.stat().st_ino)
        owned = snapshot._owned_epoch_archives
        listdir = os.listdir
        foreign_lists = []
        substituted = [False]

        def substitute_after_verification(root):
            archives = owned(root)
            if not substituted[0]:
                self.assertEqual((archive,), archives)
                parent = archive / "receipts/deliveries"
                parent.rename(archive / "receipts/displaced-deliveries")
                parent.symlink_to(foreign, target_is_directory=True)
                substituted[0] = True
            return archives

        def observe_listdir(path):
            info = os.fstat(path) if isinstance(path, int) else os.stat(path)
            if (info.st_dev, info.st_ino) == foreign_identity:
                foreign_lists.append(path)
            return listdir(path)

        errors = io.StringIO()
        with mock.patch.object(tui, "BoardFilesystemWakeup", side_effect=OSError("scratch unavailable")) if fallback else nullcontext():
            with mock.patch.object(snapshot, "_owned_epoch_archives", side_effect=substitute_after_verification),\
                 mock.patch.object(os, "listdir", side_effect=observe_listdir):
                watch = codex_wait._WaiterInputsWatch(self.f.participant,
                    hashlib.sha256(self.c.session.encode()).hexdigest(), errors)
                try:
                    self.assertFalse(watch.complete)
                    self.assertIsNone(watch.source)
                    self.assert_invalidated(watch.changed())
                finally:
                    watch.close()
        self.assertEqual([], foreign_lists, "archive census followed a substituted ancestor alias")
        self.assertEqual(b"foreign scratch archive sentinel", sentinel.read_bytes())
        self.assertEqual(entries, tuple(sorted(path.name for path in foreign.iterdir())))

    def flat_inventory_budget(self, fallback):
        _other, archive = self.native_other_epoch()
        target = archive / "receipts/deliveries"
        target_identity = (target.stat().st_dev, target.stat().st_ino)
        owned, scandir = snapshot._owned_epoch_archives, os.scandir
        populated, scanned = [False], [0]

        def grow_after_native_verification(root):
            archives = owned(root)
            if not populated[0]:
                self.assertEqual((archive,), archives)
                # Real membership growth after receipt verification exercises
                # the hint census budget; no native receipt is fabricated.
                for index in range(4100):
                    (target / ("racing-member-" + str(index) + ".jsonl")).write_bytes(b"x")
                populated[0] = True
            return archives

        class CountedScan:
            def __init__(self, handle):
                self.handle = handle
            def __iter__(self):
                return self
            def __next__(self):
                entry = next(self.handle)
                scanned[0] += 1
                return entry
            def __enter__(self):
                return self
            def __exit__(self, *_):
                self.close()
            def close(self):
                self.handle.close()

        def observe_scan(path):
            handle = scandir(path)
            if populated[0] and isinstance(path, int):
                info = os.fstat(path)
                if (info.st_dev, info.st_ino) == target_identity:
                    return CountedScan(handle)
            return handle

        with mock.patch.object(tui, "BoardFilesystemWakeup", side_effect=OSError("scratch unavailable")) if fallback else nullcontext():
            with mock.patch.object(snapshot, "_owned_epoch_archives", side_effect=grow_after_native_verification),\
                 mock.patch.object(os, "scandir", side_effect=observe_scan):
                watch = codex_wait._WaiterInputsWatch(self.f.participant,
                    hashlib.sha256(self.c.session.encode()).hexdigest(), io.StringIO())
                try:
                    self.assertFalse(watch.complete)
                    self.assertIsNone(watch.source)
                    self.assertGreater(scanned[0], 0, "flat inventory was materialized instead of incrementally scanned")
                    self.assertLessEqual(scanned[0], 4097, "flat census consumed every over-budget entry")
                    self.assert_invalidated(watch.changed())
                finally:
                    watch.close()

    def concurrent_roll_registration(self, fallback):
        other = self.native_other_epoch(roll=False)
        attempted = threading.Event()
        result, failures = [], []
        acquire = bus_epoch._acquire_epoch_descriptor
        register = codex_wait._WaiterInputsWatch._register

        def observe_acquire(descriptor, *, exclusive):
            if threading.current_thread().name == "r2-native-roller" and exclusive:
                attempted.set()
            return acquire(descriptor, exclusive=exclusive)

        def roll():
            try:
                result.append(bus_epoch.roll_bus_epoch(self.f.root, actor="architect",
                    idempotency_key="r2-concurrent-register-roll")["receipt"])
            except BaseException as error:
                failures.append(error)

        roller = threading.Thread(target=roll, name="r2-native-roller")
        registrations = [0]

        def register_with_contender(watch):
            registrations[0] += 1
            if registrations[0] == 1:
                descriptor = os.open(self.f.root.tenant_home,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                try:
                    with self.assertRaises(BlockingIOError,
                            msg="registration and baseline capture were outside the native epoch guard"):
                        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                finally:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
                    os.close(descriptor)
                roller.start()
                self.assertTrue(wait_event(attempted, "test_waiter_poll_cost_r2_inputs attempted"), "native roll never reached epoch acquisition")
            return register(watch)

        watch = None
        try:
            with mock.patch.object(tui, "BoardFilesystemWakeup", side_effect=OSError("scratch unavailable")) if fallback else nullcontext():
                with mock.patch.object(bus_epoch, "_acquire_epoch_descriptor", side_effect=observe_acquire),\
                     mock.patch.object(codex_wait._WaiterInputsWatch, "_register", register_with_contender):
                    watch = codex_wait._WaiterInputsWatch(self.f.participant,
                        hashlib.sha256(self.c.session.encode()).hexdigest(), io.StringIO())
                    wait_join(roller, "test_waiter_poll_cost_r2_inputs roller")
                self.assertFalse(roller.is_alive(), "owned native roll did not finish its bounded acquisition")
                self.assertEqual([], failures)
                self.assertEqual(1, len(result))
                archive = bus_epoch.validate_epoch_receipt_archive(self.f.root, result[0])
                self.assert_invalidated(watch.changed())
                self.assertIn(archive / "receipts/deliveries" / (other + ".jsonl"), watch.paths)
        finally:
            if roller.ident is not None:
                wait_join(roller, "test_waiter_poll_cost_r2_inputs roller")
            if watch is not None:
                watch.close()

    def archive_membership_after_census(self, fallback):
        _other, archive = self.native_other_epoch()
        new_directory = archive / "receipts/deliveries/after-census-empty"
        census = codex_wait._WaiterInputsWatch._archive_paths
        injected = [False]
        before_events = self.f.root.resolve_relative("events.jsonl").read_bytes()
        before_history = self.c.history.read_bytes()

        def grow_after_census(home, selected, *args, **kwargs):
            result = census(home, selected, *args, **kwargs)
            if home == archive and not injected[0]:
                new_directory.mkdir()
                injected[0] = True
            return result

        with mock.patch.object(tui, "BoardFilesystemWakeup", side_effect=OSError("scratch unavailable")) if fallback else nullcontext():
            with mock.patch.object(codex_wait._WaiterInputsWatch, "_archive_paths", side_effect=grow_after_census):
                watch = codex_wait._WaiterInputsWatch(self.f.participant,
                    hashlib.sha256(self.c.session.encode()).hexdigest(), io.StringIO())
            try:
                self.assertTrue(injected[0])
                self.assertTrue(watch.complete)
                self.assertEqual(fallback, watch.source is None)
                # Empty archive directories change no receipt facts, and the
                # authoritative evaluator remains caught up in the new epoch.
                self.assertEqual((archive,), snapshot._owned_epoch_archives(self.f.root))
                with self.c.selection() as selection:
                    self.assertEqual([], selection["fresh_messages"])
                    self.assertEqual(0, selection["held_total"])
                self.assertEqual(before_events, self.f.root.resolve_relative("events.jsonl").read_bytes())
                self.assertEqual(before_history, self.c.history.read_bytes())
                self.assertTrue(watch.generation_unstable,
                    "post-census archive membership was absorbed into a clean baseline")
                self.assert_invalidated(watch.changed())
                self.assertIn(new_directory, watch.paths,
                    "first dirty poll did not bind the newly observed archive directory")
                self.assertEqual((fallback, False), watch.changed())
                (new_directory / "second-empty-child").mkdir()
                self.assertEqual((archive,), snapshot._owned_epoch_archives(self.f.root))
                self.assert_invalidated(watch.changed())
            finally:
                watch.close()

    def parent_replacement(self, fallback):
        self.c.held()
        parent = self.f.root.tenant_home / "receipts"
        displaced = self.f.root.tenant_home / "displaced-receipts"
        leaf = self.c.history
        before_leaf = leaf.stat()
        before_parent = parent.stat()
        with self.c.watch(fallback) as watch:
            parent.rename(displaced)
            parent.mkdir(mode=stat.S_IMODE(before_parent.st_mode))
            # Move child directories as a unit: the ledger leaves themselves
            # retain inode, bytes, mode and all fingerprint timestamps.
            for child in tuple(displaced.iterdir()):
                child.rename(parent / child.name)
            changed = watch.changed()
        after_leaf = leaf.stat()
        self.assertNotEqual(before_parent.st_ino, parent.stat().st_ino)
        for field in ("st_dev", "st_ino", "st_mode", "st_size", "st_mtime_ns", "st_ctime_ns"):
            self.assertEqual(getattr(before_leaf, field), getattr(after_leaf, field), field)
        self.assertEqual(2, len(self.c.plane.read(self.c.node)))
        self.assert_invalidated(changed)

    def lock_safety(self, fallback, hardlink):
        self.c.held()
        lock = self.f.root.resolve_relative("events.jsonl.lock")
        self.assertTrue(lock.is_file(), "native presentation selection did not create its source lock")
        before = lock.stat()
        before_events = self.f.root.resolve_relative("events.jsonl").read_bytes()
        with self.c.watch(fallback) as watch:
            if hardlink:
                os.link(lock, self.f.base / "source-lock-alias")
            else:
                lock.chmod(stat.S_IMODE(before.st_mode) ^ stat.S_IWGRP)
            changed = watch.changed()
        self.assertEqual(before_events, self.f.root.resolve_relative("events.jsonl").read_bytes())
        self.assertEqual(before.st_ino, lock.stat().st_ino)
        if hardlink:
            self.assertEqual(2, lock.stat().st_nlink)
            with self.assertRaises(IntegrityFailure):
                self.c.plane.read(self.c.node)
        else:
            self.assertNotEqual(before.st_mode, lock.stat().st_mode)
        self.assert_invalidated(changed)

    def test_native_partial_epoch_marker_invalidates(self):
        fixture.exercise_watch_paths(self, self.partial_marker)

    def test_native_other_node_archived_delivery_corruption_invalidates(self):
        fixture.exercise_watch_paths(self, self.archived_other_node, plane=("deliveries", "acks"))

    def test_native_parent_replacement_with_unchanged_leaf_invalidates(self):
        fixture.exercise_watch_paths(self, self.parent_replacement)

    def test_native_source_lock_chmod_invalidates(self):
        fixture.exercise_watch_paths(self, self.lock_safety, hardlink=(False, True))

    def test_native_actual_roll_rebinds_archive_before_next_mutation(self):
        fixture.exercise_watch_paths(self, self.dynamic_archive)

    def test_native_ordinary_flock_does_not_invalidate(self):
        fixture.exercise_watch_paths(self, self.ordinary_flock)

    def test_native_nested_caller_ancestry_alias_invalidates(self):
        fixture.exercise_watch_paths(self, self.caller_ancestry, alias=(True, False))

    def test_native_failed_archive_inventory_never_certifies_clean(self):
        fixture.exercise_watch_paths(self, self.failed_inventory, budget=(False, True))

    def test_native_archive_census_ancestor_alias_stays_confined(self):
        fixture.exercise_watch_paths(self, self.archive_alias_confinement)

    def test_native_flat_archive_census_stops_incrementally_at_budget(self):
        fixture.exercise_watch_paths(self, self.flat_inventory_budget)

    def test_native_roll_cannot_cross_constructor_registration_generation(self):
        fixture.exercise_watch_paths(self, self.concurrent_roll_registration)

    def test_native_archive_child_created_after_census_stays_dirty_and_rebinds(self):
        fixture.exercise_watch_paths(self, self.archive_membership_after_census)

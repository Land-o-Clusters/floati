from __future__ import annotations

import json
import multiprocessing
import os
import tempfile
import threading
import time
import unittest
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from floati import jsonl, run_segments
from floati.errors import DurabilityFailure, IntegrityFailure, ProtocolRefusal
from floati.framing import encode_frame
from floati.ids import uuid7_hex
from floati.root import FloatiRoot
from floati.runtruth import RunLedger
from floati.sequencer_epoch import (
    DirectWriterLease,
    ManagedWriterLease,
    SequencerEpochLedger,
)


NOW = datetime(2026, 8, 9, 12, 0, 0, tzinfo=timezone.utc)


def _run_created(tenant: str, run_id: str = None):
    return {
        "schema_version": 0,
        "id": "run-created-" + uuid7_hex(),
        "tenant_id": tenant,
        "timestamp": "2026-08-09T12:00:00.000Z",
        "kind": "run_created",
        "run_id": run_id or "run-" + uuid7_hex(),
        "plan_digest": "a" * 64,
        "item_ids": ["work-" + uuid7_hex()],
        "dependency_edges": [],
    }


def _hold_managed_owner(root_path, ready, release):
    root = FloatiRoot.open_direct_home(Path(root_path), create=False)
    try:
        with ManagedWriterLease(root, "sequencer-live", now=NOW):
            ready.set()
            release.wait(5)
    except Exception:
        ready.set()
        raise


def _abandon_managed_owner(root_path, ready):
    root = FloatiRoot.open_direct_home(Path(root_path), create=False)
    lease = ManagedWriterLease(root, "sequencer-abandoned", now=NOW)
    lease.__enter__()
    ready.set()
    os._exit(0)


def _race_closed_takeover(root_path, sequencer_id, start, release, queue):
    try:
        root = FloatiRoot.open_direct_home(Path(root_path), create=False)
        start.wait(5)
        with ManagedWriterLease(
            root, sequencer_id, takeover=True, now=NOW + timedelta(seconds=2)
        ) as lease:
            queue.put(("ok", lease.epoch))
            release.wait(5)
    except ProtocolRefusal as exc:
        queue.put(("refused", exc.code))
    except Exception as exc:
        queue.put(("error", type(exc).__name__))


def _append_while_holding_owner(root_path, entered_fsync, continue_fsync, queue):
    root = FloatiRoot.open_direct_home(Path(root_path), create=False)
    record = _run_created(root.tenant_id)
    original = run_segments._append_frame

    def blocked_append(path, encoded):
        entered_fsync.set()
        if not continue_fsync.wait(5):
            raise RuntimeError("test barrier timed out")
        return original(path, encoded)

    try:
        with mock.patch.object(run_segments, "_append_frame", side_effect=blocked_append):
            RunLedger(root).append(record)
        queue.put(("ok", record["id"]))
    except Exception as exc:
        queue.put(("error", type(exc).__name__))


def _fork_managed_append(run_ledger, lease, capability, candidate, queue):
    try:
        run_ledger._append_managed(candidate, lease.epoch, capability)
        queue.put(("ok", candidate["id"]))
    except ProtocolRefusal as exc:
        queue.put(("refused", exc.code))
    except Exception as exc:
        queue.put(("error", type(exc).__name__))


def _fork_managed_release_and_exit(lease, queue):
    results = []
    for action in ("release", "exit"):
        try:
            if action == "release":
                lease.release(NOW + timedelta(seconds=1))
            else:
                lease.__exit__(None, None, None)
            results.append(("ok", action))
        except ProtocolRefusal as exc:
            results.append(("refused", exc.code))
        except Exception as exc:
            results.append(("error", type(exc).__name__))
    queue.put(results)


def _fork_direct_lease_use(lease, queue):
    results = []
    for action in ("exit", "enter"):
        try:
            if action == "exit":
                lease.__exit__(None, None, None)
            else:
                lease.__enter__()
            results.append(("ok", action))
        except ProtocolRefusal as exc:
            results.append(("refused", exc.code))
        except Exception as exc:
            results.append(("error", type(exc).__name__))
    queue.put(results)


class SequencerEpochTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = FloatiRoot.open_direct_home(
            Path(self.temp.name).resolve() / "alpha", create=True
        )
        self.ledger = SequencerEpochLedger(self.root)

    def _abandon_open_epoch(self) -> None:
        context = multiprocessing.get_context("fork")
        ready = context.Event()
        process = context.Process(
            target=_abandon_managed_owner, args=(str(self.root.path), ready)
        )
        process.start()
        self.assertTrue(ready.wait(5))
        process.join(5)
        self.assertEqual(0, process.exitcode)

    def _closed_epoch(self) -> None:
        lease = ManagedWriterLease(self.root, "sequencer-a", now=NOW).__enter__()
        lease.release(NOW + timedelta(seconds=1))
        lease.__exit__(None, None, None)

    def test_initial_enter_release_and_next_enter_have_exact_lifecycle(self) -> None:
        """Catches epoch-zero starts, release increments, loose fields, or broken predecessor testimony."""
        self.assertIsNone(self.ledger.current())
        lease = ManagedWriterLease(self.root, "sequencer-a", now=NOW).__enter__()
        entered = lease.record
        self.assertEqual(
            {
                "schema_version",
                "id",
                "tenant_id",
                "timestamp",
                "kind",
                "epoch",
                "operation",
                "sequencer_id",
                "previous_epoch_record_id",
                "absence_reason",
            },
            set(entered),
        )
        self.assertEqual(1, entered["schema_version"])
        self.assertEqual("sequencer_epoch", entered["kind"])
        self.assertEqual(1, entered["epoch"])
        self.assertEqual("entered", entered["operation"])
        self.assertEqual("sequencer-a", entered["sequencer_id"])
        self.assertIsNone(entered["previous_epoch_record_id"])
        self.assertEqual("initial", entered["absence_reason"])
        self.assertEqual("2026-08-09T12:00:00.000Z", entered["timestamp"])

        try:
            with self.assertRaises(ProtocolRefusal) as duplicate:
                self.ledger.enter("sequencer-a", NOW + timedelta(seconds=1))
            self.assertEqual("sequencer_managed_active", duplicate.exception.code)
            with self.assertRaises(ProtocolRefusal) as wrong_owner:
                self.ledger.release("sequencer-b", 1, NOW + timedelta(seconds=1))
            self.assertEqual("sequencer_owner_mismatch", wrong_owner.exception.code)
            with self.assertRaises(ProtocolRefusal) as wrong_epoch:
                self.ledger.release("sequencer-a", 2, NOW + timedelta(seconds=1))
            self.assertEqual("sequencer_epoch_mismatch", wrong_epoch.exception.code)
            released = self.ledger.release(
                "sequencer-a", 1, NOW + timedelta(seconds=1)
            )
        finally:
            lease.__exit__(None, None, None)
        self.assertEqual(1, released["epoch"])
        self.assertEqual("released", released["operation"])
        self.assertEqual(entered["id"], released["previous_epoch_record_id"])
        self.assertEqual("graceful_release", released["absence_reason"])
        self.assertEqual(released, self.ledger.current())

        with ManagedWriterLease(
            self.root, "sequencer-b", now=NOW + timedelta(seconds=2)
        ) as next_lease:
            next_entered = next_lease.record
        self.assertEqual(2, next_entered["epoch"])
        self.assertEqual(released["id"], next_entered["previous_epoch_record_id"])
        self.assertEqual("graceful_release", next_entered["absence_reason"])

    def test_takeover_after_matching_release_increments_once(self) -> None:
        """Catches takeover reusing or double-incrementing a gracefully closed epoch."""
        first = ManagedWriterLease(self.root, "sequencer-a", now=NOW).__enter__()
        entered = first.record
        released = first.release(NOW + timedelta(seconds=1))
        first.__exit__(None, None, None)
        with ManagedWriterLease(
            self.root,
            "sequencer-b",
            takeover=True,
            now=NOW + timedelta(seconds=2),
        ) as second:
            takeover = second.record
        self.assertEqual(2, takeover["epoch"])
        self.assertEqual("takeover", takeover["operation"])
        self.assertEqual("graceful_release", takeover["absence_reason"])
        self.assertEqual(released["id"], takeover["previous_epoch_record_id"])
        self.assertNotEqual(entered["id"], takeover["previous_epoch_record_id"])

    def test_timestamp_testimony_is_aware_utc_and_never_moves_backward(self) -> None:
        """Catches ambient time replacement, naive time acceptance, or backward lifecycle testimony."""
        lease = ManagedWriterLease(self.root, "sequencer-a", now=NOW).__enter__()
        entered = lease.record
        self.assertEqual("2026-08-09T12:00:00.000Z", entered["timestamp"])
        try:
            with self.assertRaises(ProtocolRefusal) as naive:
                self.ledger.release(
                    "sequencer-a", 1, datetime(2026, 8, 9, 12, 0, 1)
                )
            self.assertEqual("time_invalid", naive.exception.code)
            with self.assertRaises(ProtocolRefusal) as backward:
                self.ledger.release(
                    "sequencer-a", 1, NOW - timedelta(milliseconds=1)
                )
            self.assertEqual("sequencer_timestamp_order", backward.exception.code)
        finally:
            lease.__exit__(None, None, None)

    def test_strict_schema_matches_record_validator_contract(self) -> None:
        """Catches schema and exact runtime validator fields drifting apart."""
        with ManagedWriterLease(self.root, "sequencer-a", now=NOW) as lease:
            record = lease.record
        schema = json.loads(
            (Path(__file__).parents[1] / "schemas/v1/sequencer-epoch-record.schema.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(set(record), set(schema["required"]))
        self.assertEqual(set(record), set(schema["properties"]))

    def test_malformed_reordered_and_forward_records_fail_closed(self) -> None:
        """Catches replay accepting altered lifecycle semantics, physical reorder, or skipped epochs."""
        first = ManagedWriterLease(self.root, "sequencer-a", now=NOW).__enter__()
        entered = first.record
        released = first.release(NOW + timedelta(seconds=1))
        first.__exit__(None, None, None)
        second = ManagedWriterLease(
            self.root,
            "sequencer-b",
            takeover=True,
            now=NOW + timedelta(seconds=2),
        ).__enter__()
        takeover = second.record
        second.release(NOW + timedelta(seconds=3))
        second.__exit__(None, None, None)
        records = [entered, released, takeover]
        path = self.root.resolve_relative(self.ledger.relative_path)

        cases = (
            ([dict(entered, absence_reason="graceful_release")], "sequencer_absence_invalid"),
            ([released, entered, takeover], "sequencer_initial_invalid"),
            ([entered, released, dict(takeover, epoch=3)], "sequencer_epoch_sequence"),
            ([dict(entered, schema_version=2)], "schema_version_invalid"),
        )
        for rows, code in cases:
            with self.subTest(code=code):
                path.write_bytes(b"".join(encode_frame(row) for row in rows))
                with self.assertRaises(IntegrityFailure) as caught:
                    self.ledger.current()
                self.assertEqual(code, caught.exception.code)
        path.write_bytes(b"".join(encode_frame(row) for row in records))

    def test_abandoned_host_owner_lock_allows_explicit_takeover(self) -> None:
        """Catches durable open evidence being mistaken for live ownership after process death."""
        self._abandon_open_epoch()
        self.assertEqual("entered", self.ledger.current()["operation"])

        with ManagedWriterLease(
            self.root,
            "sequencer-replacement",
            takeover=True,
            now=NOW + timedelta(seconds=1),
        ) as lease:
            self.assertEqual(2, lease.epoch)
            self.assertEqual("host_local_owner_absent", lease.record["absence_reason"])
        self.assertEqual("released", self.ledger.current()["operation"])
        self.assertEqual(2, self.ledger.current()["epoch"])

    def test_positive_owner_control_precedes_live_owner_refusal(self) -> None:
        """Catches a vacuous all-denied host-lock test or takeover of a genuinely live owner."""
        with DirectWriterLease(self.root):
            pass

        context = multiprocessing.get_context("fork")
        ready, release = context.Event(), context.Event()
        process = context.Process(
            target=_hold_managed_owner,
            args=(str(self.root.path), ready, release),
        )
        process.start()
        self.assertTrue(ready.wait(5))
        before = self.ledger.current()
        try:
            with self.assertRaises(ProtocolRefusal) as caught:
                DirectWriterLease.offline_takeover(
                    self.root, "operator-direct", NOW + timedelta(seconds=1)
                )
            self.assertEqual("ledger_lock_timeout", caught.exception.code)
            self.assertEqual(before, self.ledger.current())
        finally:
            release.set()
            process.join(5)
        self.assertEqual(0, process.exitcode)

    def test_two_process_closed_epoch_takeover_cas_yields_one_next_epoch(self) -> None:
        """Catches both contenders appending from the same released predecessor."""
        self._closed_epoch()
        context = multiprocessing.get_context("fork")
        start, release, queue = context.Event(), context.Event(), context.Queue()
        processes = [
            context.Process(
                target=_race_closed_takeover,
                args=(str(self.root.path), name, start, release, queue),
            )
            for name in ("sequencer-b", "sequencer-c")
        ]
        for process in processes:
            process.start()
        start.set()
        results = [queue.get(timeout=3) for _ in processes]
        release.set()
        for process in processes:
            process.join(5)
            self.assertEqual(0, process.exitcode)
        self.assertEqual(1, sum(status == "ok" and value == 2 for status, value in results))
        self.assertEqual(
            1,
            sum(status == "refused" and value == "ledger_lock_timeout" for status, value in results),
        )
        self.assertEqual([1, 1, 2, 2], [row["epoch"] for row in self.ledger.records()])

    def test_direct_append_refuses_open_epoch_until_atomic_offline_takeover_pair(self) -> None:
        """Catches daemonless append bypassing open managed evidence or exposing a half-closed takeover."""
        self._abandon_open_epoch()
        run_ledger = RunLedger(self.root)
        candidate = _run_created(self.root.tenant_id)
        with self.assertRaises(ProtocolRefusal) as active:
            run_ledger.append(candidate)
        self.assertEqual("sequencer_managed_active", active.exception.code)
        self.assertEqual([], run_ledger.records())

        before = self.root.resolve_relative(self.ledger.relative_path).read_bytes()
        with mock.patch.object(jsonl.os, "write", return_value=1):
            with self.assertRaises(DurabilityFailure) as failed:
                DirectWriterLease.offline_takeover(
                    self.root, "operator-direct", NOW + timedelta(seconds=1)
                )
        self.assertEqual("short_write", failed.exception.code)
        self.assertEqual(before, self.root.resolve_relative(self.ledger.relative_path).read_bytes())

        takeover, released = DirectWriterLease.offline_takeover(
            self.root, "operator-direct", NOW + timedelta(seconds=2)
        )
        self.assertEqual("takeover", takeover["operation"])
        self.assertEqual("released", released["operation"])
        self.assertEqual(takeover["epoch"], released["epoch"])
        self.assertEqual(takeover["id"], released["previous_epoch_record_id"])
        self.assertEqual(candidate, run_ledger.append(candidate))

    def test_managed_capability_is_live_root_bound_and_epoch_bound(self) -> None:
        """Catches forged, cross-root, released, or stale-epoch managed append authority."""
        run_ledger = RunLedger(self.root)
        candidate = _run_created(self.root.tenant_id)
        with ManagedWriterLease(self.root, "sequencer-a", now=NOW) as lease:
            capability = lease.managed_append_capability
            with self.assertRaises(ProtocolRefusal) as forged:
                run_ledger._append_managed(candidate, lease.epoch, object())
            self.assertEqual("managed_append_capability_invalid", forged.exception.code)
            with self.assertRaises(ProtocolRefusal) as stale:
                run_ledger._append_managed(candidate, lease.epoch + 1, capability)
            self.assertEqual("sequencer_epoch_mismatch", stale.exception.code)
            before = run_ledger.records()
            for invalid_epoch in (True, 1.0):
                with self.subTest(invalid_epoch=invalid_epoch):
                    with self.assertRaises(ProtocolRefusal) as invalid:
                        run_ledger._append_managed(
                            _run_created(self.root.tenant_id),
                            invalid_epoch,
                            capability,
                        )
                    self.assertEqual("sequencer_epoch_invalid", invalid.exception.code)
                    self.assertEqual(before, run_ledger.records())

            other_root = FloatiRoot.open_direct_home(
                Path(self.temp.name).resolve() / "beta", create=True
            )
            with self.assertRaises(ProtocolRefusal) as cross_root:
                RunLedger(other_root)._append_managed(
                    _run_created("beta"), lease.epoch, capability
                )
            self.assertEqual("managed_append_capability_invalid", cross_root.exception.code)
            self.assertEqual(candidate, run_ledger._append_managed(candidate, lease.epoch, capability))

            divergent = _run_created(self.root.tenant_id, run_id=candidate["run_id"])
            with self.assertRaises(ProtocolRefusal) as projection:
                run_ledger._append_managed(divergent, lease.epoch, capability)
            self.assertEqual("run_duplicate", projection.exception.code)

        with self.assertRaises(ProtocolRefusal) as released:
            run_ledger._append_managed(_run_created("alpha"), 1, capability)
        self.assertEqual("managed_append_capability_invalid", released.exception.code)

    def test_raw_epoch_mutations_require_a_live_exclusive_owner_proof(self) -> None:
        """Catches public epoch methods mutating under CAS without lifetime owner testimony."""
        with ManagedWriterLease(self.root, "sequencer-a", now=NOW) as lease:
            self.assertEqual("entered", lease.record["operation"])
        before = self.root.resolve_relative(self.ledger.relative_path).read_bytes()
        calls = (
            lambda: self.ledger.enter("sequencer-b", NOW + timedelta(seconds=1)),
            lambda: self.ledger.release("sequencer-a", 1, NOW + timedelta(seconds=1)),
            lambda: self.ledger.takeover("sequencer-b", NOW + timedelta(seconds=1)),
        )
        for call in calls:
            with self.subTest(call=call):
                with self.assertRaises(ProtocolRefusal) as caught:
                    call()
                self.assertEqual("sequencer_owner_required", caught.exception.code)
                self.assertEqual(
                    before,
                    self.root.resolve_relative(self.ledger.relative_path).read_bytes(),
                )

    def test_released_lease_cannot_open_an_untracked_epoch_before_owner_exit(self) -> None:
        """Catches reusing one owner proof after release to leave a new open epoch at unlock."""
        lease = ManagedWriterLease(self.root, "sequencer-a", now=NOW).__enter__()
        lease.release(NOW + timedelta(seconds=1))
        before = self.root.resolve_relative(self.ledger.relative_path).read_bytes()
        try:
            with self.assertRaises(ProtocolRefusal) as caught:
                self.ledger.enter("sequencer-b", NOW + timedelta(seconds=2))
            self.assertEqual("sequencer_lease_inactive", caught.exception.code)
            self.assertEqual(
                before, self.root.resolve_relative(self.ledger.relative_path).read_bytes()
            )
        finally:
            lease.__exit__(None, None, None)
        self.assertEqual("released", self.ledger.current()["operation"])

    def test_forked_child_cannot_use_parent_managed_append_capability(self) -> None:
        """Catches fork inheritance turning a parent lease token into child append authority."""
        context = multiprocessing.get_context("fork")
        queue = context.Queue()
        run_ledger = RunLedger(self.root)
        parent_record = _run_created(self.root.tenant_id)
        child_record = _run_created(self.root.tenant_id)
        with ManagedWriterLease(self.root, "sequencer-a", now=NOW) as lease:
            capability = lease.managed_append_capability
            self.assertEqual(
                parent_record,
                run_ledger._append_managed(parent_record, lease.epoch, capability),
            )
            process = context.Process(
                target=_fork_managed_append,
                args=(run_ledger, lease, capability, child_record, queue),
            )
            process.start()
            process.join(5)
            self.assertEqual(0, process.exitcode)
            self.assertEqual(
                ("refused", "sequencer_lease_process_mismatch"),
                queue.get(timeout=1),
            )
            self.assertEqual([parent_record], run_ledger.records())

    def test_forked_child_release_and_exit_cannot_close_or_unlock_parent_lease(self) -> None:
        """Catches child release or context exit mutating and unlocking inherited parent ownership."""
        context = multiprocessing.get_context("fork")
        queue = context.Queue()
        lease = ManagedWriterLease(self.root, "sequencer-a", now=NOW).__enter__()
        self.assertEqual("entered", lease.record["operation"])
        try:
            process = context.Process(
                target=_fork_managed_release_and_exit, args=(lease, queue)
            )
            process.start()
            process.join(5)
            self.assertEqual(0, process.exitcode)
            self.assertEqual(
                [
                    ("refused", "sequencer_lease_process_mismatch"),
                    ("refused", "sequencer_lease_process_mismatch"),
                ],
                queue.get(timeout=1),
            )
            self.assertEqual("entered", self.ledger.current()["operation"])
            self.assertIsNotNone(lease.managed_append_capability)
        finally:
            lease.__exit__(None, None, None)
        self.assertEqual("released", self.ledger.current()["operation"])

    def test_forked_child_cannot_use_or_exit_parent_direct_lease(self) -> None:
        """Catches child context operations unlocking or reusing an inherited direct lease."""
        context = multiprocessing.get_context("fork")
        queue = context.Queue()
        lease = DirectWriterLease(self.root).__enter__()
        candidate = _run_created(self.root.tenant_id)
        self.assertEqual(candidate, RunLedger(self.root).append(candidate))
        try:
            process = context.Process(
                target=_fork_direct_lease_use, args=(lease, queue)
            )
            process.start()
            process.join(5)
            self.assertEqual(0, process.exitcode)
            self.assertEqual(
                [
                    ("refused", "sequencer_lease_process_mismatch"),
                    ("refused", "sequencer_lease_process_mismatch"),
                ],
                queue.get(timeout=1),
            )
        finally:
            lease.__exit__(None, None, None)

    def test_offline_takeover_pair_rejects_duplicate_generated_identities(self) -> None:
        """Catches the combined pair bypassing duplicate IDs within the candidate batch."""
        self._abandon_open_epoch()
        path = self.root.resolve_relative(self.ledger.relative_path)
        before = path.read_bytes()
        fixed = "0" * 12 + "7" + "0" * 3 + "8" + "0" * 15
        with mock.patch("floati.sequencer_epoch.uuid7_hex", return_value=fixed):
            with self.assertRaises(ProtocolRefusal) as caught:
                DirectWriterLease.offline_takeover(
                    self.root, "operator-direct", NOW + timedelta(seconds=1)
                )
        self.assertEqual("duplicate_record_id", caught.exception.code)
        self.assertEqual(before, path.read_bytes())

    def test_replay_rejects_duplicate_epoch_record_identity(self) -> None:
        """Catches projection accepting the same physical epoch identity twice."""
        self._closed_epoch()
        path = self.root.resolve_relative(self.ledger.relative_path)
        records = self.ledger.records()
        duplicate = dict(records[1], id=records[0]["id"])
        path.write_bytes(encode_frame(records[0]) + encode_frame(duplicate))
        with self.assertRaises(IntegrityFailure) as caught:
            self.ledger.current()
        self.assertEqual("duplicate_record_id", caught.exception.code)

    def test_matching_release_waits_through_managed_run_fsync(self) -> None:
        """Catches release closing the capability epoch between managed validation and run fsync."""
        run_ledger = RunLedger(self.root)
        candidate = _run_created(self.root.tenant_id)
        lease = ManagedWriterLease(self.root, "sequencer-a", now=NOW).__enter__()
        entered_fsync = threading.Event()
        continue_fsync = threading.Event()
        release_done = threading.Event()
        failures = []
        original = run_segments._append_frame

        def blocked_append(path, encoded):
            entered_fsync.set()
            if not continue_fsync.wait(5):
                raise RuntimeError("test barrier timed out")
            return original(path, encoded)

        def append_record():
            try:
                run_ledger._append_managed(
                    candidate, lease.epoch, lease.managed_append_capability
                )
            except Exception as exc:
                failures.append(exc)

        def release_lease():
            try:
                lease.release(NOW + timedelta(seconds=1))
            except Exception as exc:
                failures.append(exc)
            finally:
                release_done.set()

        try:
            with mock.patch.object(run_segments, "_append_frame", side_effect=blocked_append):
                append_thread = threading.Thread(target=append_record)
                append_thread.start()
                self.assertTrue(entered_fsync.wait(5))
                release_thread = threading.Thread(target=release_lease)
                release_thread.start()
                time.sleep(0.05)
                self.assertFalse(release_done.is_set())
                continue_fsync.set()
                append_thread.join(5)
                release_thread.join(5)
                self.assertFalse(append_thread.is_alive())
                self.assertFalse(release_thread.is_alive())
        finally:
            continue_fsync.set()
            lease.__exit__(None, None, None)
        self.assertEqual([], failures)
        self.assertEqual(candidate, run_ledger.records()[0])
        self.assertEqual("released", self.ledger.current()["operation"])

    def test_direct_owner_lock_is_held_through_run_fsync(self) -> None:
        """Catches releasing the shared owner lock between closed-epoch replay and durable run append."""
        context = multiprocessing.get_context("fork")
        entered_fsync, continue_fsync = context.Event(), context.Event()
        queue = context.Queue()
        process = context.Process(
            target=_append_while_holding_owner,
            args=(str(self.root.path), entered_fsync, continue_fsync, queue),
        )
        process.start()
        self.assertTrue(entered_fsync.wait(5))
        try:
            with self.assertRaises(ProtocolRefusal) as caught:
                ManagedWriterLease(
                    self.root, "sequencer-a", now=NOW
                ).__enter__()
            self.assertEqual("ledger_lock_timeout", caught.exception.code)
        finally:
            continue_fsync.set()
            process.join(5)
        self.assertEqual(0, process.exitcode)
        self.assertEqual("ok", queue.get(timeout=1)[0])

    def test_lock_instrumentation_proves_grant_owner_run_and_epoch_separation(self) -> None:
        """Catches owner acquisition under run, epoch CAS under append, or epoch operations taking grant/run locks."""
        events = []
        original_jsonl_lock = jsonl._locked_path
        original_run_lock = run_segments._locked_path

        @contextmanager
        def track_jsonl(
            path, *, exclusive,
            relative=None,
            timeout_seconds=jsonl.LOCK_TIMEOUT_SECONDS,
            order_tracked=True,
        ):
            events.append(path.name)
            with original_jsonl_lock(
                path,
                exclusive=exclusive,
                relative=relative,
                timeout_seconds=timeout_seconds,
                order_tracked=order_tracked,
            ):
                yield

        @contextmanager
        def track_run(path, *, exclusive, relative=None):
            events.append(path.name)
            with original_run_lock(
                path, exclusive=exclusive, relative=relative,
            ):
                yield

        candidate = _run_created(self.root.tenant_id)

        def append_under_grant(records):
            return RunLedger(self.root).append(candidate), None

        with mock.patch.object(jsonl, "_locked_path", track_jsonl), mock.patch.object(
            run_segments, "_locked_path", track_run
        ):
            jsonl.transact(
                self.root,
                "capabilities/grants.jsonl",
                append_under_grant,
                allowed_kinds={"capability_grant", "capability_revoked"},
            )
        self.assertEqual(
            [
                "grants.jsonl.lock",
                "owner.lock",
                "acceptance.lock",
                "events.jsonl.lock",
            ],
            events,
        )
        self.assertNotIn("epochs.jsonl.lock", events)

        events.clear()
        with mock.patch.object(jsonl, "_locked_path", track_jsonl), mock.patch.object(
            run_segments, "_locked_path", track_run
        ):
            with ManagedWriterLease(self.root, "sequencer-a", now=NOW):
                pass
        self.assertEqual(
            ["owner.lock", "epochs.jsonl.lock", "epochs.jsonl.lock"], events
        )
        self.assertNotIn("grants.jsonl.lock", events)
        self.assertNotIn("events.jsonl.lock", events)
        self.assertNotIn("writer.lock", events)

        events.clear()
        with mock.patch.object(jsonl, "_locked_path", track_jsonl), mock.patch.object(
            run_segments, "_locked_path", track_run
        ):
            with self.assertRaises(ProtocolRefusal) as raw:
                self.ledger.enter("sequencer-b", NOW + timedelta(seconds=1))
        self.assertEqual("sequencer_owner_required", raw.exception.code)
        self.assertEqual([], events)


if __name__ == "__main__":
    unittest.main()

"""Worker receipt appends keep historical CPU work out of their ledger lock."""

from __future__ import annotations

import json
import os
import statistics
import tempfile
import time
import unittest
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from floati import jsonl
from floati.errors import IntegrityFailure, ProtocolRefusal
from floati.ids import uuid7_hex
from floati.planes import AuthorityGrantStore
from floati.registry import Registry
from floati.root import FloatiRoot
from floati.work import WorkLog
from floati.workers import WorkerReceipts


class WorkerReceiptLockHoldTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = FloatiRoot.open_direct_home(Path(temporary.name) / "fleet", create=True)
        self.now = datetime.now(timezone.utc)
        Registry(self.root).register("builder", "Codex")
        work = WorkLog(self.root)
        self.item = work.add("receipt lock hold", "builder", [], now=self.now)
        AuthorityGrantStore(self.root).claim("work-claims", "builder", 60, 60, self.now)
        work.claim(self.item["id"], "builder", "work-claims", 1, now=self.now)
        self.receipts = WorkerReceipts(self.root)
        self.path = self.root.resolve_relative("receipts/workers.jsonl")

    def append(self, session=None, transition="claim"):
        return self.receipts.append(
            session or "worker-" + uuid7_hex(), self.item["id"], "builder",
            "fixture", transition, None, [], now=self.now,
        )

    def test_receipt_lock_hold_does_not_replay_growing_history(self):
        """A full historical validation/projection under the native lock fails this."""
        seed = self.append()
        histories = {}
        frames = []
        for index in range(2560):
            record = dict(seed, id="worker-receipt-" + uuid7_hex(),
                          session_id="worker-" + uuid7_hex(),
                          work_item_id="work-" + uuid7_hex())
            frames.append(jsonl.encode_frame(record))
            if index + 1 in (256, 2560):
                histories[index + 1] = b"".join(frames)

        native_lock = jsonl._locked_path
        native_validate = jsonl.validate_record
        native_project = self.receipts._project
        held = [False]
        historical_validations = []
        historical_projections = []
        measurements = {}
        durations = []

        @contextmanager
        def measured_lock(path, **kwargs):
            with native_lock(path, **kwargs):
                selected = path == self.path.with_name("workers.jsonl.lock")
                if selected:
                    held[0] = True
                    started = time.perf_counter()
                try:
                    yield
                finally:
                    if selected:
                        durations.append(time.perf_counter() - started)
                        held[0] = False

        def measured_validate(record, *args, **kwargs):
            if held[0] and record.get("work_item_id") != self.item["id"]:
                historical_validations.append(record["id"])
            return native_validate(record, *args, **kwargs)

        def measured_project(records):
            if held[0]:
                historical_projections.append(len(records))
            return native_project(records)

        with mock.patch.object(jsonl, "_locked_path", measured_lock),\
                mock.patch.object(jsonl, "validate_record", measured_validate),\
                mock.patch.object(self.receipts, "_project", measured_project):
            for count, history in histories.items():
                samples = []
                for _ in range(5):
                    self.path.write_bytes(history)
                    durations.clear()
                    appended = self.append()
                    self.assertEqual("claim", appended["transition"])
                    samples.append(sum(durations))
                measurements[count] = statistics.median(samples)

        print("[WORKER-RECEIPT-LOCK-HOLD-1] " + json.dumps({
            "history_records": [256, 2560],
            "median_hold_seconds": measurements,
            "historical_validations_while_locked": len(historical_validations),
            "historical_projection_rows_while_locked": sum(historical_projections),
        }, sort_keys=True))
        self.assertEqual([], historical_validations,
                         "receipt lock must not validate historical frames")
        self.assertEqual([], historical_projections,
                         "receipt lock must not project historical sessions")
        # The structural assertions above pin the scaling property; this
        # measured bound allows fixed fsync/scheduler noise, not 10x replay.
        self.assertLessEqual(measurements[2560], 3 * measurements[256] + 0.01)

    def during_preparation(self, action):
        native_project = self.receipts._project

        def project_then_act(records):
            projected = native_project(records)
            action()
            return projected

        return mock.patch.object(self.receipts, "_project", project_then_act)

    def test_competing_transition_after_preflight_is_not_lost(self):
        """A stale preflight must not authorize two native spawn transitions."""
        seed = self.append()

        def competing_spawn():
            WorkerReceipts(self.root).append(
                seed["session_id"], self.item["id"], "builder", "fixture",
                "spawn", None, [], now=self.now,
            )

        with self.during_preparation(competing_spawn):
            with self.assertRaises(ProtocolRefusal) as caught:
                self.append(seed["session_id"], "spawn")
        self.assertEqual("worker_transition_invalid", caught.exception.code)
        self.assertEqual(["claim", "spawn"],
                         [row["transition"] for row in self.receipts.records()])

    def test_competing_claim_after_empty_preflight_keeps_one_work_binding(self):
        """Absent-to-created receipt ledgers still bind work exactly once."""
        def competing_claim():
            WorkerReceipts(self.root).append(
                "worker-" + uuid7_hex(), self.item["id"], "builder", "fixture",
                "claim", None, [], now=self.now,
            )

        with self.during_preparation(competing_claim):
            with self.assertRaises(ProtocolRefusal) as caught:
                self.append()
        self.assertEqual("worker_claim_already_bound", caught.exception.code)
        self.assertEqual(1, len(self.receipts.records()))

    def test_invalid_concurrent_suffix_refuses_before_candidate_append(self):
        """Tail catch-up must validate both IDs and worker chronology."""
        seed = self.append()
        original = self.path.read_bytes()
        for suffix, code in (
            (dict(seed), "duplicate_record_id"),
            (dict(seed, id="worker-receipt-" + uuid7_hex(), transition="drive"),
             "worker_transition_invalid"),
            (dict(seed, id="worker-receipt-" + uuid7_hex(), node_id="other"),
             "worker_session_mismatch"),
        ):
            with self.subTest(code=code):
                self.path.write_bytes(original)
                tail = jsonl.encode_frame(suffix)

                def append_tail():
                    with self.path.open("ab") as handle:
                        handle.write(tail)

                with self.during_preparation(append_tail):
                    with self.assertRaises(IntegrityFailure) as caught:
                        self.append(seed["session_id"], "spawn")
                self.assertEqual(code, caught.exception.code)
                self.assertEqual(original + tail, self.path.read_bytes())

    def test_changed_preflight_identity_refuses_without_retry_or_append(self):
        """Replacement, truncation and same-size rewrite invalidate preflight."""
        seed = self.append()
        original = self.path.read_bytes()

        def replace():
            replacement = self.path.with_suffix(".replacement")
            replacement.write_bytes(original)
            os.replace(replacement, self.path)

        def rewrite():
            before = self.path.stat()
            self.path.write_bytes(original.replace(b'"fixture"', b'"changed"'))
            os.utime(self.path, ns=(before.st_atime_ns, before.st_mtime_ns))

        for action in (replace, lambda: self.path.write_bytes(b""), rewrite, self.path.unlink):
            with self.subTest(action=action.__name__):
                self.path.write_bytes(original)
                with self.during_preparation(action):
                    with self.assertRaises(IntegrityFailure) as caught:
                        self.append(seed["session_id"], "spawn")
                self.assertEqual("ledger_identity_changed_during_read", caught.exception.code)
                self.assertFalse(self.path.exists() and b'"spawn"' in self.path.read_bytes())

    def test_pending_epoch_refuses_receipt_append_without_mutation(self):
        """Native worker receipts must respect pending epoch recovery."""
        seed = self.append()
        before = self.path.read_bytes()
        marker = self.root.tenant_home / ".floati-epoch-roll-pending.v1.json"
        marker.write_text("{}\n", encoding="utf-8")
        with self.assertRaises(ProtocolRefusal) as caught:
            self.append(seed["session_id"], "spawn")
        self.assertEqual("epoch_roll_recovery_required", caught.exception.code)
        self.assertEqual(before, self.path.read_bytes())

    def test_contained_receipt_alias_keeps_physical_epoch_admission(self):
        """A worker-path alias must not bypass a pending selected-ledger roll."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.symlink_to("../events.jsonl")
        marker = self.root.tenant_home / ".floati-epoch-roll-pending.v1.json"
        marker.write_text("{}\n", encoding="utf-8")
        with self.assertRaises(ProtocolRefusal) as caught:
            self.append()
        self.assertEqual("epoch_roll_recovery_required", caught.exception.code)
        self.assertFalse((self.root.tenant_home / "events.jsonl").exists())

    def test_record_limit_counts_preflight_and_concurrent_suffix_together(self):
        """A short suffix must not conceal an already-full combined ledger."""
        seed = self.append()

        def competing_spawn():
            WorkerReceipts(self.root).append(
                seed["session_id"], self.item["id"], "builder", "fixture",
                "spawn", None, [], now=self.now,
            )

        with mock.patch.object(jsonl, "MAX_LEDGER_RECORDS", 2):
            with self.during_preparation(competing_spawn):
                with self.assertRaises(ProtocolRefusal) as caught:
                    self.append(seed["session_id"], "drive")
        self.assertEqual("ledger_record_limit", caught.exception.code)
        self.assertEqual(["claim", "spawn"],
                         [row["transition"] for row in self.receipts.records()])

    def test_candidate_id_collision_with_concurrent_suffix_is_refused(self):
        """A prepared candidate must be checked against newly appended IDs."""
        seed = self.append()

        def competing_spawn():
            WorkerReceipts(self.root).append(
                seed["session_id"], self.item["id"], "builder", "fixture",
                "spawn", None, [], now=self.now,
            )

        with mock.patch("floati.workers.uuid7_hex", return_value=uuid7_hex()):
            with self.during_preparation(competing_spawn):
                with self.assertRaises(ProtocolRefusal) as caught:
                    self.append(seed["session_id"], "drive")
        self.assertEqual("duplicate_record_id", caught.exception.code)
        self.assertEqual(["claim", "spawn"],
                         [row["transition"] for row in self.receipts.records()])


if __name__ == "__main__":
    unittest.main()

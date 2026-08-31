from __future__ import annotations

from floati import fixture_ids as public_ids

import multiprocessing
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from floati.cursor import SparseCursor
from floati.errors import ProtocolRefusal
from floati.events import EventLog
from floati.jsonl import read_records
from floati.planes import AuthorityGrantStore
from floati.registry import Registry
from floati.root import FloatiRoot
from floati.work import WorkLog
from floati.ids import uuid7_hex


def _run_created_worker(base: str, start: object, results: object, record: dict) -> None:
    from floati.runtruth import RunLedger
    root = FloatiRoot.open(Path(base), "alpha")
    start.wait()
    try:
        results.put(("ok", RunLedger(root).append(record)))
    except ProtocolRefusal as exc:
        results.put(("refused", exc.code))


def _register_worker(base: str, start: object, results: object, node: str) -> None:
    registry = Registry(FloatiRoot.open(Path(base), "alpha"))
    start.wait()
    try:
        results.put(("ok", registry.register(node, "worker")["id"]))
    except ProtocolRefusal as exc:
        results.put(("refused", exc.code))


def _send_worker(base: str, start: object, results: object) -> None:
    root = FloatiRoot.open(Path(base), "alpha")
    events = EventLog(root)
    start.wait()
    try:
        results.put(("ok", events.send(
            public_ids.worker('alpha'), "bob", "slipway", "a" * 40,
            "docs/evidence/checkpoint.md", "HM-0.5 delivered",
            idempotency_key="same-key",
        )["id"]))
    except ProtocolRefusal as exc:
        results.put(("refused", exc.code))


def _ack_worker(base: str, start: object, results: object, item_id: str) -> None:
    cursor = SparseCursor(FloatiRoot.open(Path(base), "alpha"))
    start.wait()
    try:
        results.put((
            "ok",
            cursor.ack("bob", [item_id], acting_session_id="atomicity-session")["id"],
        ))
    except ProtocolRefusal as exc:
        results.put(("refused", exc.code))


def _claim_work_worker(
    base: str, start: object, results: object, subject: str, epoch: int
) -> None:
    root = FloatiRoot.open(Path(base), "alpha")
    start.wait()
    try:
        item = WorkLog(root).claim_owned_oldest(
            public_ids.worker('alpha'),
            subject,
            epoch,
            now=datetime(2026, 7, 31, 22, 0, tzinfo=timezone.utc),
        )
        results.put(("ok", str(item["id"])))
    except ProtocolRefusal as exc:
        results.put(("refused", exc.code))


class ProcessAtomicityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = FloatiRoot.open(self.base, "alpha")
        self.context = multiprocessing.get_context("fork")

    def race(self, target: object, args: tuple[object, ...]) -> list[tuple[str, str]]:
        start = self.context.Event()
        results = self.context.Queue()
        processes = [self.context.Process(target=target, args=(str(self.base), start, results, *args)) for _ in range(4)]
        for process in processes:
            process.start()
        start.set()
        for process in processes:
            process.join(5)
            self.assertEqual(0, process.exitcode)
        return [results.get(timeout=1) for _ in processes]

    def test_registry_check_and_append_is_one_process_transaction(self) -> None:
        outcomes = self.race(_register_worker, ("shared",))
        self.assertEqual(1, sum(status == "ok" for status, _ in outcomes))
        self.assertEqual(3, sum(value == "registry_duplicate" for _, value in outcomes))
        rows = read_records(self.root, "registry/entries.jsonl", allowed_kinds={"registry_entry"})
        self.assertEqual(1, len(rows))

    def test_send_idempotency_is_process_atomic(self) -> None:
        registry = Registry(self.root)
        registry.register(public_ids.worker('alpha'), "worker")
        registry.register("bob", "worker")
        outcomes = self.race(_send_worker, ())
        self.assertEqual({"ok"}, {status for status, _ in outcomes})
        self.assertEqual(1, len({value for _, value in outcomes}))
        rows = read_records(self.root, "events.jsonl", allowed_kinds={"message_envelope"})
        self.assertEqual(1, len(rows))

    def test_sparse_ack_idempotency_is_process_atomic(self) -> None:
        registry = Registry(self.root)
        registry.register(public_ids.worker('alpha'), "worker")
        registry.register("bob", "worker")
        events = EventLog(self.root, registry)
        message = events.send(
            public_ids.worker('alpha'), "bob", "slipway", "a" * 40,
            "docs/evidence/checkpoint.md", "HM-0.5 delivered",
        )
        events.present("bob")
        outcomes = self.race(_ack_worker, (message["id"],))
        self.assertEqual({"ok"}, {status for status, _ in outcomes})
        self.assertEqual(1, len({value for _, value in outcomes}))
        rows = read_records(self.root, "receipts/acks/bob.jsonl", allowed_kinds={"ack_receipt"})
        self.assertEqual(1, len(rows))

    def test_ready_work_claims_have_zero_double_consumption_under_process_contention(self) -> None:
        Registry(self.root).register(public_ids.worker('alpha'), "worker")
        work = WorkLog(self.root)
        first = work.add("first", public_ids.worker('alpha'), [])
        second = work.add("second", public_ids.worker('alpha'), [])
        grant = AuthorityGrantStore(self.root).claim(
            "work-claims",
            public_ids.worker('alpha'),
            60,
            60,
            datetime(2026, 7, 31, 22, 0, tzinfo=timezone.utc),
        )

        outcomes = self.race(
            _claim_work_worker, ("work-claims", int(grant["epoch"]))
        )

        successes = [value for status, value in outcomes if status == "ok"]
        refusals = [value for status, value in outcomes if status == "refused"]
        self.assertEqual({first["id"], second["id"]}, set(successes))
        self.assertEqual(2, len(successes))
        self.assertEqual(["work_owned_open_absent"] * 2, sorted(refusals))
        rows = read_records(
            self.root,
            "work/items.jsonl",
            allowed_kinds={"work_item", "work_transition"},
        )
        claims = [row for row in rows if row["kind"] == "work_transition"]
        self.assertEqual(2, len(claims))
        self.assertEqual(2, len({row["work_item_id"] for row in claims}))

    def test_run_created_idempotency_is_process_atomic(self) -> None:
        record = {"schema_version": 0, "id": "run-created-" + uuid7_hex(), "tenant_id": "alpha",
            "timestamp": "2026-08-02T12:00:00.000Z", "kind": "run_created", "run_id": "run-" + uuid7_hex(),
            "plan_digest": "a" * 64, "item_ids": ["work-" + uuid7_hex()], "dependency_edges": []}
        start = self.context.Event(); results = self.context.Queue()
        processes = [self.context.Process(target=_run_created_worker, args=(str(self.base), start, results, record)) for _ in range(4)]
        for process in processes: process.start()
        start.set()
        for process in processes:
            process.join(5); self.assertEqual(0, process.exitcode)
        outcomes = [results.get(timeout=1) for _ in processes]
        self.assertEqual({"ok"}, {status for status, _ in outcomes})
        self.assertTrue(all(value == record for _, value in outcomes))
        self.assertEqual(1, len(__import__("floati.runtruth", fromlist=["RunLedger"]).RunLedger(self.root).records()))

    def test_active_run_created_idempotency_is_process_atomic(self) -> None:
        """Catches active-root contenders escaping the segment writer transaction into legacy."""
        from floati.run_segments import SegmentedRunStore
        from floati.runtruth import RUN_KINDS, RunLedger

        store = SegmentedRunStore(self.root, RUN_KINDS)
        store.activate(now=datetime(2026, 8, 9, 12, 0, tzinfo=timezone.utc))
        record = {"schema_version": 0, "id": "run-created-" + uuid7_hex(), "tenant_id": "alpha",
            "timestamp": "2026-08-09T12:00:00.000Z", "kind": "run_created", "run_id": "run-" + uuid7_hex(),
            "plan_digest": "a" * 64, "item_ids": ["work-" + uuid7_hex()], "dependency_edges": []}

        outcomes = self.race(_run_created_worker, (record,))

        self.assertEqual({"ok"}, {status for status, _ in outcomes})
        self.assertTrue(all(value == record for _, value in outcomes))
        self.assertEqual([record], store.records())
        self.assertFalse(self.root.resolve_relative(RunLedger.relative_path).exists())


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

from floati import fixture_ids as public_ids

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from floati.errors import IntegrityFailure, ProtocolRefusal
from floati.planes import AuthorityGrantStore
from floati.projection import FleetProjection, iter_deltas
from floati.registry import Registry
from floati.root import FloatiRoot
from floati.work import WorkLog
from floati.workers import WorkerRunner


NOW = datetime(2026, 7, 31, 21, 0, tzinfo=timezone.utc)


class _MustNotSpawn:
    name = "fixture"

    def spawn(self, item: dict, *, deadline_seconds: float) -> object:
        raise AssertionError("a caught-up worker must not spawn an adapter")

    def drive(self, handle: object, item: dict, *, deadline_seconds: float) -> list[dict[str, str]]:
        raise AssertionError("a caught-up worker must not drive an adapter")


class ConsumptionLedgerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = FloatiRoot.open_direct_home(Path(self.temp.name) / "fleet", create=True)
        Registry(self.root).register(public_ids.builder('a'), "Codex")

    def test_corrupt_consumption_ledger_is_not_projected_as_empty(self) -> None:
        from floati.consumption import ConsumptionLedger

        self.root.resolve_relative("work/items.jsonl").parent.mkdir(parents=True, exist_ok=True)
        self.root.resolve_relative("work/items.jsonl").write_text('{"incomplete":', encoding="utf-8")

        with self.assertRaises(IntegrityFailure) as caught:
            ConsumptionLedger(self.root).summary()

        self.assertEqual("consumption_state_unavailable", caught.exception.code)

    def test_worker_board_and_watch_share_the_work_coordinate(self) -> None:
        from floati.consumption import ConsumptionLedger

        coordinate = ConsumptionLedger(self.root).summary()["coordinate"]
        snapshot = FleetProjection(self.root).snapshot(NOW)
        delta = next(iter_deltas(FleetProjection(self.root), iterations=1, now=lambda: NOW))

        self.assertEqual("work/items.jsonl", coordinate)
        self.assertEqual(coordinate, snapshot["consumption"]["coordinate"])
        self.assertEqual(coordinate, delta["snapshot"]["consumption"]["coordinate"])
        self.assertEqual("caught_up", snapshot["consumption"]["state"])

        from floati.tui import model_from_root
        from floati.tui_render import render_plain_dump

        board = render_plain_dump(model_from_root(self.root, NOW), width=120)
        self.assertIn("CONSUMPTION", board)
        self.assertIn("CAUGHT UP", board)

    def test_no_work_refusal_is_visible_as_unsatisfied_wake(self) -> None:
        AuthorityGrantStore(self.root).claim("work-claims", public_ids.builder('a'), 60, 60, NOW)

        with self.assertRaises(ProtocolRefusal) as caught:
            WorkerRunner(self.root, {"fixture": _MustNotSpawn()}).run(
                public_ids.builder('a'), "fixture", now=NOW
            )

        self.assertEqual("worker_work_absent", caught.exception.code)
        snapshot = FleetProjection(self.root).snapshot(NOW)
        self.assertEqual("unsatisfied_wake", snapshot["consumption"]["wake_state"])
        self.assertEqual("worker_work_absent", snapshot["worker_refusals"][-1]["reason_code"])

    def test_consumption_never_creates_delivery_or_ack_receipts(self) -> None:
        AuthorityGrantStore(self.root).claim("work-claims", public_ids.builder('a'), 60, 60, NOW)
        with self.assertRaises(ProtocolRefusal):
            WorkerRunner(self.root, {"fixture": _MustNotSpawn()}).run(
                public_ids.builder('a'), "fixture", now=NOW
            )

        self.assertFalse(self.root.resolve_relative(public_ids.compose('receipts/deliveries/', public_ids.ledger(public_ids.builder('a')))).exists())
        self.assertFalse(self.root.resolve_relative(public_ids.compose('receipts/acks/', public_ids.ledger(public_ids.builder('a')))).exists())

    def test_worker_refusal_distinguishes_blocked_dependencies_from_no_work(self) -> None:
        Registry(self.root).register(public_ids.builder('b'), "Codex")
        dependency = WorkLog(self.root).add("upstream", public_ids.builder('b'), [], now=NOW)
        WorkLog(self.root).add(
            "downstream", public_ids.builder('a'), [], needs=[dependency["id"]], now=NOW
        )
        AuthorityGrantStore(self.root).claim("work-claims", public_ids.builder('a'), 60, 60, NOW)

        with self.assertRaises(ProtocolRefusal) as caught:
            WorkerRunner(self.root, {"fixture": _MustNotSpawn()}).run(
                public_ids.builder('a'), "fixture", now=NOW
            )

        self.assertEqual("worker_work_blocked", caught.exception.code)
        snapshot = FleetProjection(self.root).snapshot(NOW)
        self.assertEqual("worker_work_blocked", snapshot["worker_refusals"][-1]["reason_code"])


if __name__ == "__main__":
    unittest.main()

"""DRILL-1 — the drill's own round trip may not be charged to the worker.

`WorkerRunner.run` starts the worker's process deadline before it reaches the
`on_drive` gate, and `on_drive` is the DRILL's gate and nothing else: floati/
orchestrate.py builds it only from the drill's reached/release pair. So every
second a drill spent between "the worker reached drive" and "the drill let it
go" was spent out of the worker's deadline, and a slow drill made the WORKER
report `process_timeout`.

⇒ AN INSTRUMENT THAT BILLS ITS SUBJECT FOR THE INSTRUMENT'S OWN ROUND TRIP
DOES NOT MEASURE THE SUBJECT — and it fails in the subject's name, so the
receipt accuses the worker.

The remedy REFUNDS the pause; it does not re-grant the budget. The pre-gate
phase (process start, isolation ready, the spawn frame) is real worker time
and stays bounded, and the refund is clamped by the grant's own wall-clock
expiry so a drill cannot buy a worker time outside the authority it holds.
"""

from __future__ import annotations

import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

from floati import fixture_ids as public_ids
from floati.planes import AuthorityGrantStore
from floati.registry import Registry
from floati.root import FloatiRoot
from floati.work import WorkLog
from floati.workers import WorkerRunner


NOW = datetime(2026, 7, 31, 20, 0, tzinfo=timezone.utc)

# The gate must outlast the whole per-call budget, so that "the worker still
# completed" can only be explained by the refund. `call_timeout` is the
# binding term of `effective_deadline` here: the grant below is 60s and its
# TTL is far longer, so min(60, ttl, CALL_TIMEOUT) is CALL_TIMEOUT.
CALL_TIMEOUT = 1.0
GATE_HELD_SECONDS = 2.0


class _CompletingAdapter:
    """Spawns and drives immediately; every delay in the test is the gate's."""

    name = "fixture"

    def spawn(self, item: dict, *, deadline_seconds: float) -> object:
        return object()

    def drive(
        self, handle: object, item: dict, *, deadline_seconds: float,
    ) -> list:
        return [{"repo": "floati-proof", "sha": "a" * 40, "doc": "README.md"}]


class WorkerDrillGateDeadlineTests(unittest.TestCase):
    maxDiff = None

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = FloatiRoot.open_direct_home(
            Path(self.temp.name) / "fleet", create=True,
        )
        self.node = public_ids.builder("a")
        Registry(self.root).register(self.node, "Codex")
        self.work = WorkLog(self.root)
        self.item = self.work.add("write README line", self.node, [], now=NOW)
        AuthorityGrantStore(self.root).claim(
            "work-claims", self.node, 60, 60, NOW,
        )

    def _run(self, on_drive) -> dict:
        runner = WorkerRunner(
            self.root, {"fixture": _CompletingAdapter()},
            call_timeout=CALL_TIMEOUT,
        )
        return runner.run(self.node, "fixture", now=NOW, on_drive=on_drive)

    def test_a_gate_longer_than_the_deadline_does_not_time_out_the_worker(
        self,
    ) -> None:
        """Catches the drill's round trip being charged to the worker."""
        held = []

        def slow_gate() -> None:
            entered = time.monotonic()
            time.sleep(GATE_HELD_SECONDS)
            held.append(time.monotonic() - entered)

        result = self._run(slow_gate)

        # The gate really did outlast the whole budget — otherwise this test
        # would pass without ever staging the condition it exists for.
        self.assertEqual(1, len(held))
        self.assertGreater(held[0], CALL_TIMEOUT, held)
        self.assertEqual("complete", result["transition"], result)
        self.assertIsNone(result.get("reason_code"), result)
        item = self.work.show(self.item["id"])[0]
        self.assertEqual("completed", item["state"])

    def test_the_same_run_without_a_gate_completes_identically(self) -> None:
        """Controls the refund: the no-drill path is untouched by DRILL-1."""
        result = self._run(None)

        self.assertEqual("complete", result["transition"], result)
        item = self.work.show(self.item["id"])[0]
        self.assertEqual("completed", item["state"])

    def test_a_gate_shorter_than_the_deadline_still_completes(self) -> None:
        """Controls the arithmetic at the ordinary drill duration."""
        result = self._run(lambda: time.sleep(0.05))

        self.assertEqual("complete", result["transition"], result)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

from floati import fixture_ids as public_ids

import json
import multiprocessing
import os
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

from floati.errors import ProtocolRefusal
from floati.planes import AuthorityGrantStore
from floati.registry import Registry
from floati.root import FloatiRoot
from floati.work import WorkLog
from floati.workers import WorkerReceipts
from unittest.mock import patch


class _TimedCompletingAdapter:
    name = "fixture"

    def __init__(self, trace: Path, delay: float = 0.15) -> None:
        self.trace = trace
        self.delay = delay

    def spawn(self, item: dict, *, deadline_seconds: float) -> object:
        with self.trace.open("a", encoding="utf-8") as handle:
            handle.write(f"start {item['owner']} {item['id']} {time.monotonic():.9f}\n")
        return item["id"]

    def drive(
        self, handle: object, item: dict, *, deadline_seconds: float
    ) -> list[dict[str, str]]:
        time.sleep(min(self.delay, max(0.001, deadline_seconds / 2)))
        with self.trace.open("a", encoding="utf-8") as stream:
            stream.write(f"end {item['owner']} {item['id']} {time.monotonic():.9f}\n")
        return []


class _ZeroDelayAdapter:
    name = "fixture"

    def spawn(self, item: dict, *, deadline_seconds: float) -> object:
        return item["id"]

    def drive(
        self, handle: object, item: dict, *, deadline_seconds: float
    ) -> list[dict[str, str]]:
        return []


class _FaultAdapter:
    name = "fixture"

    def __init__(self, pid_trace: Path) -> None:
        self.pid_trace = pid_trace
        self.hang_node = None
        self.hang_event = None
        self.process_group_registrar = None
        self.child = None

    def set_hang_event(self, node: str, event: object) -> None:
        self.hang_node = node
        self.hang_event = event

    def set_process_group_registrar(self, registrar: object) -> None:
        self.process_group_registrar = registrar

    def spawn(self, item: dict, *, deadline_seconds: float) -> object:
        with self.pid_trace.open("a", encoding="utf-8") as handle:
            handle.write(f"{item['owner']} {os.getpid()}\n")
        return item["owner"]

    def drive(
        self, handle: object, item: dict, *, deadline_seconds: float
    ) -> list[dict[str, str]]:
        if item["owner"] == self.hang_node and self.hang_event is not None:
            if self.hang_event.wait(timeout=max(0.01, deadline_seconds / 2)):
                child = subprocess.Popen(
                    [sys.executable, "-c", "import time; time.sleep(10)"],
                    start_new_session=True,
                )
                self.child = child
                if self.process_group_registrar is None:
                    raise RuntimeError("missing process-group registrar")
                self.process_group_registrar(child.pid)
                with self.pid_trace.open("a", encoding="utf-8") as stream:
                    stream.write(f"grandchild {child.pid}\n")
                time.sleep(10)
        time.sleep(0.15)
        return []

    def cancel(self) -> None:
        if self.child is None:
            return
        try:
            os.killpg(self.child.pid, 15)
        except ProcessLookupError:
            pass
        try:
            self.child.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(self.child.pid, 9)
            except ProcessLookupError:
                pass
            self.child.wait(timeout=0.5)


def _fail_final_complete_receipt(
    receipt_store: object,
    session_id: str,
    work_item_id: str,
    node_id: str,
    adapter: str,
    transition: str,
    outcome_code: object,
    artifact_bindings: object,
    **kwargs: object,
) -> dict:
    from floati.workers import WorkerReceipts

    if transition == "complete" and all(
        item["readiness"] == "done" for item in WorkLog(receipt_store.root).show()
    ):
        raise SystemExit(72)
    return _ORIGINAL_APPEND(
        receipt_store,
        session_id,
        work_item_id,
        node_id,
        adapter,
        transition,
        outcome_code,
        artifact_bindings,
        **kwargs,
    )


_ORIGINAL_APPEND = WorkerReceipts.append


class OrchestrationPlanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)

    def write(self, value: object) -> Path:
        path = self.directory / "plan.json"
        path.write_text(json.dumps(value), encoding="utf-8")
        return path

    def test_plan_requires_three_unique_workers_and_more_topological_items(self) -> None:
        from floati.orchestrate import OrchestrationPlan

        valid = {
            "schema_version": 0,
            "workers": [public_ids.builder('a'), public_ids.builder('b'), public_ids.builder('c')],
            "items": [
                {"key": "a", "title": "Create A.txt", "owner": public_ids.builder('a'), "needs": []},
                {"key": "b", "title": "Create B.txt", "owner": public_ids.builder('b'), "needs": []},
                {"key": "c", "title": "Create C.txt", "owner": public_ids.builder('c'), "needs": ["a"]},
                {"key": "d", "title": "Create D.txt", "owner": public_ids.builder('a'), "needs": ["b", "c"]},
            ],
        }

        plan = OrchestrationPlan.load(self.write(valid))

        self.assertEqual((public_ids.builder('a'), public_ids.builder('b'), public_ids.builder('c')), plan.workers)
        self.assertEqual(("b", "c"), plan.items[-1].needs)

        invalid_cases = (
            ({**valid, "workers": [public_ids.builder('a'), public_ids.builder('b')]}, "orchestrate_worker_count_invalid"),
            ({**valid, "workers": [public_ids.builder('a'), public_ids.builder('a'), public_ids.builder('c')]}, "orchestrate_workers_invalid"),
            ({**valid, "items": valid["items"][:3]}, "orchestrate_item_count_invalid"),
            ({**valid, "items": [*valid["items"][:2], {"key": "c", "title": "C", "owner": public_ids.builder('c'), "needs": ["future"]}, valid["items"][3]]}, "orchestrate_dependency_invalid"),
        )
        for value, code in invalid_cases:
            with self.subTest(code=code):
                with self.assertRaises(ProtocolRefusal) as caught:
                    OrchestrationPlan.load(self.write(value))
                self.assertEqual(code, caught.exception.code)

    def test_retry_policy_rejects_bounds_before_any_orchestrator_can_schedule_work(self) -> None:
        """Catches an orchestration retry policy with an invalid attempt or backoff bound."""
        from floati.scheduler import RetryPolicy

        for kwargs in (
            {"max_attempts": 0, "base_delay_ms": 1, "cap_delay_ms": 1},
            {"max_attempts": 33, "base_delay_ms": 1, "cap_delay_ms": 1},
            {"max_attempts": 1, "base_delay_ms": 2, "cap_delay_ms": 1},
            {"max_attempts": 1, "base_delay_ms": 1, "cap_delay_ms": 86_400_001},
        ):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ProtocolRefusal) as caught:
                    RetryPolicy(**kwargs)
                self.assertEqual("retry_policy_invalid", caught.exception.code)


class FleetOrchestratorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.root = FloatiRoot.open_direct_home(self.directory / "fleet", create=True)
        current = datetime.now(timezone.utc)
        for node in (public_ids.builder('a'), public_ids.builder('b'), public_ids.builder('c')):
            Registry(self.root).register(node, "Codex")
            AuthorityGrantStore(self.root).claim(
                f"work-{node}", node, 30, 20, current
            )

    def plan(self):
        from floati.orchestrate import OrchestrationPlan

        path = self.directory / "plan.json"
        path.write_text(
            json.dumps(
                {
                    "schema_version": 0,
                    "workers": [public_ids.builder('a'), public_ids.builder('b'), public_ids.builder('c')],
                    "items": [
                        {"key": "a", "title": "Create A.txt", "owner": public_ids.builder('a'), "needs": []},
                        {"key": "b", "title": "Create B.txt", "owner": public_ids.builder('b'), "needs": []},
                        {"key": "c", "title": "Create C.txt", "owner": public_ids.builder('c'), "needs": []},
                        {"key": "d", "title": "Create D.txt", "owner": public_ids.builder('a'), "needs": ["a", "b", "c"]},
                    ],
                }
            ),
            encoding="utf-8",
        )
        return OrchestrationPlan.load(path)

    def test_three_controllers_drain_four_item_dag_with_full_receipt_chains(self) -> None:
        from floati.orchestrate import FleetOrchestrator

        trace = self.directory / "timing.txt"
        frames: list[dict] = []
        controller_names: set[str] = set()
        expected_controller_names = {
            public_ids.compose('floati-orchestrator-', public_ids.builder('a')),
            public_ids.compose('floati-orchestrator-', public_ids.builder('b')),
            public_ids.compose('floati-orchestrator-', public_ids.builder('c')),
        }

        def capture_frame(frame: dict) -> None:
            frames.append(frame)
            controller_names.update(
                process.name
                for process in multiprocessing.active_children()
                if process.name.endswith((public_ids.compose('-', public_ids.builder('a')), public_ids.compose('-', public_ids.builder('b')), public_ids.compose('-', public_ids.builder('c'))))
            )

        result = FleetOrchestrator(
            self.root,
            {"fixture": _TimedCompletingAdapter(trace)},
            adapter_name="fixture",
            redraw_interval=0.02,
        ).run(self.plan(), deadline_seconds=5, on_frame=capture_frame)

        self.assertEqual("drained", result["state"])
        self.assertEqual(0, result["return_code"])
        self.assertEqual(3, result["worker_count"])
        self.assertEqual(4, result["item_count"])
        self.assertEqual([], result["alive_after_cleanup"])
        self.assertEqual([0, 0, 0], result["controller_exit_codes"])
        self.assertTrue(all(row["state"] == "complete" for row in result["terminal_sessions"]))
        self.assertTrue(all(not row["alive_after_cleanup"] for row in result["process_audits"]))
        self.assertEqual({"done"}, {item["readiness"] for item in WorkLog(self.root).show()})
        self.assertEqual(expected_controller_names, controller_names)

        sessions = WorkerReceipts(self.root).sessions()
        self.assertEqual(4, len(sessions))
        records = WorkerReceipts(self.root).records()
        for session in sessions:
            chain = [
                row["transition"]
                for row in records
                if row["session_id"] == session["session_id"]
            ]
            self.assertEqual(
                ["claim", "spawn", "drive", "bind_artifact", "complete"], chain
            )

        intervals: dict[str, dict[str, float]] = {}
        for line in trace.read_text(encoding="utf-8").splitlines():
            phase, _owner, item_id, stamp = line.split()
            intervals.setdefault(item_id, {})[phase] = float(stamp)
        seeded = result["seeded_items"]
        initial = [intervals[seeded[key]] for key in ("a", "b", "c")]
        self.assertLess(max(row["start"] for row in initial), min(row["end"] for row in initial))
        self.assertGreaterEqual(
            intervals[seeded["d"]]["start"], max(row["end"] for row in initial)
        )
        self.assertGreaterEqual(len(frames), 2)
        self.assertTrue(all({"work", "workers", "receipts"} <= set(frame) for frame in frames))
        for pid in result["controller_pids"]:
            with self.assertRaises(ProcessLookupError):
                os.kill(pid, 0)

    def test_deadline_has_a_distinct_artifact_return_code_and_reaps_controllers(self) -> None:
        from floati.orchestrate import FleetOrchestrator

        result = FleetOrchestrator(
            self.root,
            {"fixture": _TimedCompletingAdapter(self.directory / "slow.txt", delay=1)},
            adapter_name="fixture",
            redraw_interval=0.01,
            worker_timeout=1,
        ).run(self.plan(), deadline_seconds=0.1)

        self.assertEqual("deadline", result["state"])
        self.assertEqual(34, result["return_code"])
        self.assertEqual([], result["alive_after_cleanup"])

    def test_completed_work_without_terminal_receipt_is_degraded_not_drained(self) -> None:
        from floati.orchestrate import FleetOrchestrator

        with patch("floati.workers.WorkerReceipts.append", _fail_final_complete_receipt):
            result = FleetOrchestrator(
                self.root,
                {"fixture": _TimedCompletingAdapter(self.directory / "broken-chain.txt")},
                adapter_name="fixture",
                redraw_interval=0.01,
            ).run(self.plan(), deadline_seconds=3)

        self.assertEqual({"done"}, {item["readiness"] for item in result["final_work"]})
        self.assertEqual("degraded", result["state"])
        self.assertEqual(35, result["return_code"])
        self.assertTrue(any(code not in (0, None) for code in result["controller_exit_codes"]))
        self.assertTrue(any(row["state"] == "driving" for row in result["terminal_sessions"]))

    def test_drain_requires_complete_zero_survivor_process_audit(self) -> None:
        from floati.orchestrate import FleetOrchestrator

        seeded = {"a": "work-a", "b": "work-b"}
        final_work = [
            {"id": "work-a", "readiness": "done"},
            {"id": "work-b", "readiness": "done"},
        ]
        sessions = [
            {
                "work_item_id": work_id,
                "state": "complete",
                "transition": "complete",
                "outcome_code": None,
            }
            for work_id in seeded.values()
        ]
        clean_audits = [
            {"work_item_id": work_id, "alive_after_cleanup": []}
            for work_id in seeded.values()
        ]

        self.assertFalse(
            FleetOrchestrator._drained(
                seeded,
                final_work,
                sessions,
                [0, 0, 0],
                clean_audits[:-1],
                [],
            )
        )
        self.assertFalse(
            FleetOrchestrator._drained(
                seeded,
                final_work,
                sessions,
                [0, 0, 0],
                clean_audits,
                [99999],
            )
        )


class FleetFaultDrillTests(FleetOrchestratorTests):
    def test_fault_adapter_without_a_hang_action_completes(self) -> None:
        from floati.orchestrate import FleetOrchestrator

        result = FleetOrchestrator(
            self.root,
            {"fixture": _FaultAdapter(self.directory / "no-hang-pids.txt")},
            adapter_name="fixture",
            redraw_interval=0.01,
            worker_timeout=1,
        ).run(self.plan(), deadline_seconds=3)

        self.assertEqual("drained", result["state"])

    def test_three_fault_modes_are_distinct_and_leave_no_processes(self) -> None:
        from floati.orchestrate import DrillAction, FleetOrchestrator

        pid_trace = self.directory / "fault-pids.txt"
        # CI-GREEN-20. This test flipped on the public runners at commits whose
        # code had not changed: builder-b came back `process_timeout` instead of
        # `authority_expired_mid_claim`. It is not the reaper racing the
        # leaked-process assertion - those clauses held. The worker's process
        # deadline starts in `WorkerRunner.run` BEFORE the drive gate, and the
        # `expire_authority` drill's round trip runs inside that window: the
        # orchestrator has to notice `drive_reached` on its redraw poll, perform
        # a REAL authority-grant expiry (a locked ledger write, not a flag), and
        # only then release the worker. All of that is charged against
        # `worker_timeout`, so a 0.3 s budget against a 0.15 s fixture drive was
        # a bet on the host's filesystem, and a slow runner collected. The
        # budget is raised to 1.5 s - ten times the fixture's own drive and far
        # under the 10 s hang that must still time out - so the outcome map
        # below stays EXACT. It is deliberately not relaxed to "either outcome":
        # accepting `process_timeout` here would let a real regression, the
        # expiry never taking effect, pass as a busy host.
        began = time.monotonic()
        result = FleetOrchestrator(
            self.root,
            {"fixture": _FaultAdapter(pid_trace)},
            adapter_name="fixture",
            redraw_interval=0.01,
            worker_timeout=1.5,
        ).run(
            self.plan(),
            deadline_seconds=6,
            drills=(
                DrillAction("kill_worker", public_ids.builder('a')),
                DrillAction("expire_authority", public_ids.builder('b')),
                DrillAction("hang_child", public_ids.builder('c')),
            ),
        )
        print(
            f"[CI-GREEN-20] {self.id()}: elapsed {time.monotonic() - began:.2f}s "
            f"against worker_timeout 1.5s / deadline 6s; "
            f"host loadavg {[round(value, 2) for value in os.getloadavg()]}; "
            f"drills {[(row['node'], row['outcome'], row['triggered']) for row in result['drills']]}"
        )

        self.assertEqual("degraded", result["state"])
        self.assertEqual(35, result["return_code"])
        self.assertEqual([], result["alive_after_cleanup"])
        outcomes = {row["node"]: row["outcome"] for row in result["drills"]}
        self.assertEqual(
            {
                public_ids.builder('a'): "process_cancelled",
                public_ids.builder('b'): "authority_expired_mid_claim",
                public_ids.builder('c'): "process_timeout",
            },
            outcomes,
        )
        self.assertTrue(all(row["triggered"] for row in result["drills"]))
        records = WorkerReceipts(self.root).records()
        for node in outcomes:
            chain = [row["transition"] for row in records if row["node_id"] == node]
            self.assertIn("drive", chain)
        sessions = {row["node_id"]: row for row in WorkerReceipts(self.root).sessions()}
        self.assertEqual(
            outcomes,
            {node: session["outcome_code"] for node, session in sessions.items()},
        )
        child_pids = [int(line.split()[1]) for line in pid_trace.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(4, len(child_pids))
        for pid in [*result["controller_pids"], *child_pids]:
            with self.subTest(
                pid=pid,
                controllers=result["controller_pids"],
                children=child_pids,
                audits=result["process_audits"],
            ):
                with self.assertRaises(ProcessLookupError):
                    os.kill(pid, 0)
        self.assertTrue(all(not row["alive_after_cleanup"] for row in result["process_audits"]))

    def test_degraded_dependency_stops_blocked_peers_before_overall_deadline(self) -> None:
        from floati.orchestrate import DrillAction, FleetOrchestrator

        started = time.monotonic()
        result = FleetOrchestrator(
            self.root,
            {"fixture": _FaultAdapter(self.directory / "expiry-only.txt")},
            adapter_name="fixture",
            redraw_interval=0.01,
            worker_timeout=1,
        ).run(
            self.plan(),
            deadline_seconds=3,
            drills=(DrillAction("expire_authority", public_ids.builder('b')),),
        )

        self.assertEqual("degraded", result["state"])
        self.assertLess(time.monotonic() - started, 2)

    def test_fast_worker_cannot_bypass_a_requested_drive_drill(self) -> None:
        from floati.orchestrate import DrillAction, FleetOrchestrator

        result = FleetOrchestrator(
            self.root,
            {"fixture": _ZeroDelayAdapter()},
            adapter_name="fixture",
            redraw_interval=0.25,
            worker_timeout=1,
        ).run(
            self.plan(),
            deadline_seconds=3,
            drills=(DrillAction("kill_worker", public_ids.builder('a')),),
        )

        self.assertEqual("degraded", result["state"])
        self.assertEqual(35, result["return_code"])
        self.assertTrue(result["drills"][0]["triggered"])
        self.assertEqual("process_cancelled", result["drills"][0]["outcome"])


if __name__ == "__main__":
    unittest.main()

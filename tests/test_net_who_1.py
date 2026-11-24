"""Roster cells are measured bus records, or UNKNOWN with no age."""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from floati.ids import uuid7_hex
from floati.jsonl import append_record
from floati.planes import AuthorityGrantStore, LivenessPresenceStore
from floati.presence import PresenceService
from floati.registry import Registry
from floati.root import FloatiRoot
from floati.tui import model_from_root
from floati.tui_render import render_plain_dump
from floati.work import WorkLog
from floati.workers import WorkerReceipts


WHEN = datetime(2026, 10, 24, 16, 0, tzinfo=timezone.utc)
THREAD_HARNESS = (
    Path(__file__).parent / "fixtures" / "codex-thread-observer" / "reference_harness.py"
).resolve()
PROVIDER_THREAD = "018f3a2b-4c5d-7e8f-9a0b-1c2d3e4f5678"
BINDING = {"repo": "floati", "sha": "a" * 40, "doc": "docs/car.md"}


def _stamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _age(start: object, end: datetime) -> int:
    if isinstance(start, str):
        start = datetime.fromisoformat(start.replace("Z", "+00:00"))
    return int((end - start).total_seconds())


def _fleet() -> tuple[tempfile.TemporaryDirectory, FloatiRoot]:
    temporary = tempfile.TemporaryDirectory()
    root = FloatiRoot.open_direct_home(Path(temporary.name) / "fleet", create=True)
    return temporary, root


def _board(root: FloatiRoot, now: datetime):
    model = model_from_root(root, now)
    return model, render_plain_dump(model, width=400)


def _cell(model, node_id: str, field: str):
    node = next((item for item in model.nodes if item.get("node_id") == node_id), None)
    roster = node.get("roster") if isinstance(node, dict) else None
    cell = roster.get(field) if isinstance(roster, dict) else None
    return cell if isinstance(cell, dict) else None


def _lane(root: FloatiRoot, node: str, row: str, repo: str, opened: datetime, *, closed: bool = False) -> None:
    opened_at = _stamp(opened)
    record = {
        "schema_version": 0,
        "kind": "lane_workspace_record",
        "id": "lane-workspace-" + uuid7_hex(),
        "tenant_id": root.tenant_id,
        "timestamp": opened_at,
        "node_id": node,
        "row": row,
        "path": f"{node}/work/{row}",
        "repo": repo,
        "base_sha": "b" * 40,
        "branch": f"codex/lane/{node}/{row}",
        "state": "open",
        "opened_at": opened_at,
        "closed_at": None,
        "closed_by": None,
        "opened_record_id": None,
        "why": None,
    }
    append_record(root, f"nodes/{node}/lanes.jsonl", record, allowed_kinds={"lane_workspace_record"})
    if not closed:
        return
    closing = dict(
        record,
        id="lane-workspace-" + uuid7_hex(),
        timestamp=_stamp(opened + timedelta(seconds=1)),
        state="closed",
        closed_at=_stamp(opened + timedelta(seconds=1)),
        closed_by=node,
        opened_record_id=record["id"],
        why="closed before the roster was read",
    )
    append_record(root, f"nodes/{node}/lanes.jsonl", closing, allowed_kinds={"lane_workspace_record"})


def _claim_car(root: FloatiRoot, node: str, branch: str, when: datetime) -> None:
    grant = AuthorityGrantStore(root).claim("work-claims", node, 60, 60, when)
    item = WorkLog(root).add(
        "bounded roster work",
        node,
        [BINDING],
        car_binding={"row": "ROW", "repository": "floati", "branch": branch},
        now=when,
    )
    WorkLog(root).claim(item["id"], node, "work-claims", grant["epoch"], now=when)


def _drive(root: FloatiRoot, node: str, when: datetime) -> datetime:
    grant = AuthorityGrantStore(root).claim("work-claims", node, 60, 60, when)
    item = WorkLog(root).add("drive roster work", node, [], now=when)
    WorkLog(root).claim(item["id"], node, "work-claims", grant["epoch"], now=when)
    receipts = WorkerReceipts(root)
    session = "worker-" + uuid7_hex()
    receipts.append(session, item["id"], node, "fixture", "claim", None, [], now=when)
    receipts.append(session, item["id"], node, "fixture", "spawn", None, [], now=when + timedelta(seconds=1))
    driven = when + timedelta(seconds=2)
    receipts.append(session, item["id"], node, "fixture", "drive", None, [], now=driven)
    return driven


def _observe_thread(root: FloatiRoot, node: str, mode: str, when: datetime) -> None:
    from floati.thread_observations import ThreadObserver
    from floati.thread_source import CodexLocalThreadSource

    item = WorkLog(root).add("observe roster thread", node, [], now=when)
    prefix = Path(root.path) / f"thread-source-{mode}"
    source = CodexLocalThreadSource._for_test(
        [
            sys.executable,
            str(THREAD_HARNESS),
            mode,
            str(prefix) + "-methods",
            str(prefix) + "-params.json",
            str(prefix) + "-diagnostic.json",
        ]
    )
    observer = ThreadObserver._for_test(root, source)
    attachment = observer.register_work_item(str(item["id"]), PROVIDER_THREAD, node, now=when)
    observer.observe(str(attachment["id"]), now=when)


class RosterMeasurementTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary, self.root = _fleet()
        self.addCleanup(self.temporary.cleanup)
        Registry(self.root).register("alpha", "Codex")

    def test_harness_is_measured_from_the_registry_and_owner_has_no_age(self) -> None:
        registered = Registry(self.root).require_active("alpha")
        now = datetime.fromisoformat(str(registered["timestamp"]).replace("Z", "+00:00")) + timedelta(seconds=90)
        WorkLog(self.root).add("owned but not an owner record", "alpha", [], now=now)
        model, text = _board(self.root, now)

        harness = _cell(model, "alpha", "harness")
        owner = _cell(model, "alpha", "owner")
        self.assertEqual("Codex", None if harness is None else harness.get("value"))
        self.assertEqual("MEASURED", None if harness is None else harness.get("stamp"))
        self.assertEqual(_age(registered["timestamp"], now), None if harness is None else harness.get("age_seconds"))
        self.assertIn("Codex", text)
        self.assertIsNone(None if owner is None else owner.get("value"))
        self.assertEqual("UNKNOWN", None if owner is None else owner.get("stamp"))
        self.assertIsNone(None if owner is None else owner.get("age_seconds"))

    def test_open_lane_shows_repository_at_branch_and_a_closed_lane_does_not(self) -> None:
        Registry(self.root).register("bravo", "Codex")
        opened = WHEN
        _lane(self.root, "alpha", "ROW", "fixture", opened)
        _lane(self.root, "bravo", "ROW", "fixture", opened, closed=True)
        now = opened + timedelta(seconds=30)
        model, text = _board(self.root, now)
        coordinate = "fixture@codex/lane/alpha/ROW"

        opened_cell = _cell(model, "alpha", "repository_branch")
        closed_cell = _cell(model, "bravo", "repository_branch")
        self.assertEqual(coordinate, None if opened_cell is None else opened_cell.get("value"))
        self.assertEqual("MEASURED", None if opened_cell is None else opened_cell.get("stamp"))
        self.assertEqual(_age(opened, now), None if opened_cell is None else opened_cell.get("age_seconds"))
        self.assertIn(coordinate, text)
        self.assertIsNone(None if closed_cell is None else closed_cell.get("value"))
        self.assertEqual("UNKNOWN", None if closed_cell is None else closed_cell.get("stamp"))
        self.assertIsNone(None if closed_cell is None else closed_cell.get("age_seconds"))
        self.assertNotIn("fixture@codex/lane/bravo/ROW", text)

    def test_unclaimed_work_repository_is_not_treated_as_the_current_checkout(self) -> None:
        WorkLog(self.root).add(
            "not claimed",
            "alpha",
            [BINDING],
            car_binding={"row": "ROW", "repository": "floati", "branch": "codex/car"},
            now=WHEN,
        )
        model, text = _board(self.root, WHEN + timedelta(seconds=5))
        cell = _cell(model, "alpha", "repository_branch")
        self.assertIsNone(None if cell is None else cell.get("value"))
        self.assertEqual("UNKNOWN", None if cell is None else cell.get("stamp"))
        self.assertIsNone(None if cell is None else cell.get("age_seconds"))
        self.assertNotIn("floati@codex/car", text)

    def test_claimed_car_repository_at_branch_is_measured_with_its_age(self) -> None:
        _claim_car(self.root, "alpha", "codex/car", WHEN)
        now = WHEN + timedelta(seconds=15)
        model, text = _board(self.root, now)
        cell = _cell(model, "alpha", "repository_branch")
        self.assertEqual("floati@codex/car", None if cell is None else cell.get("value"))
        self.assertEqual("MEASURED", None if cell is None else cell.get("stamp"))
        self.assertEqual(_age(WHEN, now), None if cell is None else cell.get("age_seconds"))
        self.assertIn("floati@codex/car", text)

    def test_disagreeing_repository_coordinates_stay_unknown(self) -> None:
        _lane(self.root, "alpha", "ROW", "fixture", WHEN)
        _claim_car(self.root, "alpha", "codex/car", WHEN)
        model, _text = _board(self.root, WHEN + timedelta(seconds=8))
        cell = _cell(model, "alpha", "repository_branch")
        self.assertIsNone(None if cell is None else cell.get("value"))
        self.assertEqual("UNKNOWN", None if cell is None else cell.get("stamp"))
        self.assertIsNone(None if cell is None else cell.get("age_seconds"))

    def test_expired_presence_is_measured_and_never_renders_as_idle(self) -> None:
        report = PresenceService(self.root).report_self("alpha", ttl_seconds=30, now=WHEN)
        now = WHEN + timedelta(seconds=90)
        model, text = _board(self.root, now)
        liveness = _cell(model, "alpha", "liveness")
        activity = _cell(model, "alpha", "activity")
        folded = text.casefold()
        self.assertIn(str(report["expires_at"]), text)
        self.assertIn("no report since", folded)
        self.assertNotIn("idle", folded)
        self.assertEqual("MEASURED", None if liveness is None else liveness.get("stamp"))
        self.assertEqual(_age(report["observed_at"], now), None if liveness is None else liveness.get("age_seconds"))
        self.assertNotEqual("idle", str(None if liveness is None else liveness.get("value")).casefold())
        self.assertEqual("UNKNOWN", None if activity is None else activity.get("stamp"))
        self.assertIsNone(None if activity is None else activity.get("age_seconds"))
        self.assertNotEqual("idle", str(None if activity is None else activity.get("value")).casefold())

    def test_a_fresh_report_is_measured_and_is_not_described_as_a_missing_report(self) -> None:
        report = LivenessPresenceStore(self.root).observe("alpha", 120, WHEN)
        now = WHEN + timedelta(seconds=10)
        model, text = _board(self.root, now)
        liveness = _cell(model, "alpha", "liveness")
        self.assertEqual("MEASURED", None if liveness is None else liveness.get("stamp"))
        self.assertEqual(_age(report["observed_at"], now), None if liveness is None else liveness.get("age_seconds"))
        self.assertNotIn("no report since", text.casefold())
        self.assertNotIn("idle", text.casefold())

    def test_never_reported_liveness_is_unknown_without_an_age(self) -> None:
        model, _text = _board(self.root, WHEN)
        liveness = _cell(model, "alpha", "liveness")
        self.assertEqual("UNKNOWN", None if liveness is None else liveness.get("stamp"))
        self.assertIsNone(None if liveness is None else liveness.get("value"))
        self.assertIsNone(None if liveness is None else liveness.get("age_seconds"))
        self.assertIsNone(None if liveness is None else liveness.get("observed_at"))

    def test_driving_worker_is_busy_with_the_drive_age(self) -> None:
        driven = _drive(self.root, "alpha", WHEN)
        now = driven + timedelta(seconds=7)
        model, text = _board(self.root, now)
        activity = _cell(model, "alpha", "activity")
        self.assertEqual("busy", None if activity is None else activity.get("value"))
        self.assertEqual("MEASURED", None if activity is None else activity.get("stamp"))
        self.assertEqual(_age(driven, now), None if activity is None else activity.get("age_seconds"))
        self.assertIn("busy", text.casefold())

    def test_measured_provider_idle_is_idle_and_not_loaded_is_not(self) -> None:
        _observe_thread(self.root, "alpha", "idle", WHEN)
        now = WHEN + timedelta(seconds=45)
        model, text = _board(self.root, now)
        idle = _cell(model, "alpha", "activity")
        self.assertEqual("idle", None if idle is None else idle.get("value"))
        self.assertEqual("MEASURED", None if idle is None else idle.get("stamp"))
        self.assertEqual(_age(WHEN, now), None if idle is None else idle.get("age_seconds"))
        self.assertIn("idle", text.casefold())

        other = _fleet()
        self.addCleanup(other[0].cleanup)
        Registry(other[1]).register("alpha", "Codex")
        _observe_thread(other[1], "alpha", "not-loaded", WHEN)
        other_model, other_text = _board(other[1], now)
        absent = _cell(other_model, "alpha", "activity")
        self.assertEqual("UNKNOWN", None if absent is None else absent.get("stamp"))
        self.assertIsNone(None if absent is None else absent.get("age_seconds"))
        self.assertNotEqual("idle", str(None if absent is None else absent.get("value")).casefold())
        self.assertNotIn("idle", other_text.casefold())

    def test_active_provider_status_is_busy_even_when_it_is_waiting(self) -> None:
        _observe_thread(self.root, "alpha", "active-input", WHEN)
        now = WHEN + timedelta(seconds=4)
        model, text = _board(self.root, now)
        activity = _cell(model, "alpha", "activity")
        self.assertEqual("busy", None if activity is None else activity.get("value"))
        self.assertEqual("MEASURED", None if activity is None else activity.get("stamp"))
        self.assertNotIn("idle", text.casefold())

    def test_disagreeing_busy_and_idle_measurements_stay_unknown(self) -> None:
        _drive(self.root, "alpha", WHEN)
        _observe_thread(self.root, "alpha", "idle", WHEN + timedelta(seconds=3))
        model, text = _board(self.root, WHEN + timedelta(seconds=20))
        activity = _cell(model, "alpha", "activity")
        self.assertIsNone(None if activity is None else activity.get("value"))
        self.assertEqual("UNKNOWN", None if activity is None else activity.get("stamp"))
        self.assertIsNone(None if activity is None else activity.get("age_seconds"))
        self.assertNotIn("idle", text.casefold())
        self.assertNotIn("busy", text.casefold())


class RosterHookActionTests(unittest.TestCase):
    def setUp(self) -> None:
        from tests import test_seat_execution_host_tool_native as native

        self.fixture = native.HostToolCallbackNativeTests("runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def test_last_hook_tool_is_the_current_action(self) -> None:
        from floati.host_observations import _read_state, observe_codex_host

        payload = self.fixture.payload()
        payload["tool_name"] = "Edit"
        observe_codex_host(
            self.fixture.root.path,
            payload,
            now=self.fixture.now,
            launch=False,
        )
        state = _read_state(self.fixture.root, "builder")
        self.assertIsNotNone(state)
        self.assertEqual("PostToolUse", state["event"])
        observed_at = state["observed_at"]
        observed = datetime.fromisoformat(str(observed_at).replace("Z", "+00:00"))
        now = observed + timedelta(seconds=12)
        model, text = _board(self.fixture.root, now)
        action = _cell(model, "builder", "current_action")
        self.assertEqual(payload["tool_name"], None if action is None else action.get("value"))
        self.assertEqual("MEASURED", None if action is None else action.get("stamp"))
        self.assertEqual(_age(observed_at, now), None if action is None else action.get("age_seconds"))
        self.assertIn(payload["tool_name"], text)

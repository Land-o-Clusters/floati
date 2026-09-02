from __future__ import annotations

from floati import fixture_ids as public_ids

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from floati.framing import encode_frame
from floati.decisions import DecisionRegister, decision_digest
from floati.registry import Registry
from floati.replay import ReplayTimeline
from floati.root import FloatiRoot
from floati.runtruth import RunLedger
from floati.supervisor import Supervisor
from floati.tui_render import HarborBoardModel, render_plain_dump
from tests.hm3i_gauntlet_fixtures import (
    axis_coverage_from_traces,
    assert_physical_projection,
    build_full_run_trace_set,
    canonical_observation_from_records,
)


UUIDS = (
    "018f7e9b3c117abc8def0123456789ab",
    "018f7e9b3c127abc8def0123456789ab",
    "018f7e9b3c137abc8def0123456789ab",
    "018f7e9b3c147abc8def0123456789ab",
)
NOW = datetime(2026, 11, 1, 7, 0, tzinfo=timezone.utc)


def _denial(index: int, timestamp: str) -> dict[str, object]:
    return {
        "schema_version": 0,
        "id": f"denial-{UUIDS[index]}",
        "tenant_id": "time-hostility",
        "timestamp": timestamp,
        "kind": "denial_receipt",
        "attempt_id": f"attempt-{UUIDS[index]}",
        "claimed_sender": public_ids.worker('alpha'),
        "claimed_recipient": "bob",
        "reason_code": "unknown_sender",
    }


def _message(index: int, timestamp: str) -> dict[str, object]:
    return {
        "schema_version": 0,
        "id": f"msg-{UUIDS[index]}",
        "tenant_id": "time-hostility",
        "timestamp": timestamp,
        "kind": "message_envelope",
        "sender": public_ids.worker('alpha'),
        "recipient": "bob",
        "repo": "floati",
        "sha": "a" * 40,
        "doc": "docs/evidence/HM3H-GAUNTLET.md",
        "note": f"time event {index}",
        "idempotency_key": f"time-{index}",
    }


def _decision(
    record_index: int,
    decision_index: int,
    *,
    status: str,
    timestamp: str,
    statement: str,
    supersedes: str | None = None,
) -> dict[str, object]:
    record: dict[str, object] = {
        "schema_version": 0,
        "id": f"decision-record-{UUIDS[record_index]}",
        "tenant_id": "time-hostility",
        "timestamp": timestamp,
        "kind": "decision_record",
        "repository": "owner/repo",
        "decision_id": f"decision-{UUIDS[decision_index]}",
        "scope": {"kind": "repository"},
        "statement": statement,
        "status": status,
        "author_authority": "worker" if status == "proposed" else "architect",
        "source_artifact_ids": ["run:run-018f7e9b3c137abc8def0123456789ab"],
        "task_contract_id": None,
        "decided_by": public_ids.reviewer(),
        "supersedes": supersedes,
    }
    record["decision_digest"] = decision_digest(record)
    return record


def _seed_decision_source(root: FloatiRoot) -> None:
    RunLedger(root).append(
        {
            "schema_version": 0,
            "id": "run-created-018f7e9b3c147abc8def0123456789ab",
            "tenant_id": root.tenant_id,
            "timestamp": "2026-08-08T12:00:00.000Z",
            "kind": "run_created",
            "run_id": "run-018f7e9b3c137abc8def0123456789ab",
            "plan_digest": "a" * 64,
            "item_ids": ["work-018f7e9b3c157abc8def0123456789ab"],
            "dependency_edges": [],
        }
    )


class TimeHostilityGauntletTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = FloatiRoot.open_direct_home(
            Path(self.temp.name) / "time-hostility", create=True
        )

    def test_replay_source_ordinal_beats_skew_future_and_dst_timestamps(self) -> None:
        records = (
            _denial(0, "2036-01-01T00:00:00.000Z"),
            _denial(1, "2026-11-01T05:59:59.999Z"),
            _denial(2, "2026-11-01T06:00:00.000Z"),
            _denial(3, "2020-01-01T00:00:00.000Z"),
        )
        path = self.root.resolve_relative("receipts/denials.jsonl")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"".join(encode_frame(record) for record in records))

        artifact = ReplayTimeline.from_root(self.root).artifact()

        self.assertEqual(
            [record["id"] for record in records],
            [event["record_id"] for event in artifact["events"]],
        )
        self.assertEqual([1, 2, 3, 4], [event["source_ordinal"] for event in artifact["events"]])
        elapsed = [event["elapsed_ms"] for event in artifact["events"]]
        self.assertEqual(sorted(elapsed), elapsed)
        self.assertGreaterEqual(min(elapsed), 0)

    def test_status_last_activity_uses_latest_append_not_largest_timestamp(self) -> None:
        registry = Registry(self.root)
        registry.register(public_ids.worker('alpha'), "worker")
        registry.register("bob", "worker")
        records = (
            _message(0, "2036-01-01T00:00:00.000Z"),
            _message(1, "2026-11-01T06:00:00.000Z"),
        )
        self.root.resolve_relative("events.jsonl").write_bytes(
            b"".join(encode_frame(record) for record in records)
        )

        snapshot = Supervisor(self.root).snapshot(NOW)

        nodes = {node["node_id"]: node for node in snapshot["nodes"]}
        self.assertEqual(records[-1]["timestamp"], nodes[public_ids.worker('alpha')]["last_activity"])
        self.assertEqual(records[-1]["timestamp"], nodes["bob"]["last_activity"])

    def test_board_receipt_ticker_uses_reverse_append_ordinal_not_timestamp(self) -> None:
        first = {
            "kind": "delivery_receipt",
            "timestamp": "2036-01-01T00:00:00.000Z",
            "item_ids": ["one"],
        }
        second = {
            "kind": "delivery_receipt",
            "timestamp": "2020-01-01T00:00:00.000Z",
            "item_ids": ["two", "three"],
        }
        model = HarborBoardModel(
            observed_at="2026-11-01T07:00:00.000Z",
            nodes=(),
            work_items=(),
            deliveries=(first, second),
            acknowledgments=(),
            denials=(),
            stale_leases=(),
        )

        rendered = render_plain_dump(model)

        self.assertLess(rendered.index("2 ITEMS"), rendered.index("1 ITEM"))

    def test_decision_capsule_uses_accepted_frame_ordinal_not_clock_testimony(self) -> None:
        """Catches timestamp testimony changing accepted state or physical selection."""
        timestamp_sets = (
            (
                "2036-01-01T00:00:00.000Z",
                "2020-01-01T00:00:00.000Z",
                "2026-11-01T05:59:59.999Z",
                "2026-11-01T06:00:00.000Z",
            ),
            (
                "2020-01-01T00:00:00.000Z",
                "2036-01-01T00:00:00.000Z",
                "2026-11-01T06:00:00.000Z",
                "2026-11-01T05:59:59.999Z",
            ),
        )
        accepted_selections = []
        for index, timestamps in enumerate(timestamp_sets):
            root = (
                self.root
                if index == 0
                else FloatiRoot.open_direct_home(
                    Path(self.temp.name) / "second" / "time-hostility", create=True
                )
            )
            _seed_decision_source(root)
            records = (
                _decision(0, 1, status="proposed", timestamp=timestamps[0], statement="Old decision."),
                _decision(2, 1, status="accepted", timestamp=timestamps[1], statement="Old decision."),
                _decision(1, 0, status="proposed", timestamp=timestamps[2], statement="Current decision."),
                _decision(
                    3, 0, status="accepted", timestamp=timestamps[3], statement="Current decision.",
                    supersedes=f"decision-{UUIDS[1]}",
                ),
            )
            register = DecisionRegister(root, "owner/repo")
            path = root.resolve_relative(register.relative_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"".join(encode_frame(record) for record in records))
            entries = register.capsule()["entries"]
            self.assertEqual([4], [entry["ledger_ordinal"] for entry in entries])
            accepted_selections.append(
                [
                    (
                        entry["decision_id"],
                        entry["decision"]["status"],
                        entry["ledger_ordinal"],
                    )
                    for entry in entries
                ]
            )

        # reviewer ruling A: the durable decision digest includes timestamp
        # testimony, so capsule bytes may differ.  The invariant is the
        # accepted state selected in physical ledger order.
        self.assertEqual(accepted_selections[0], accepted_selections[1])

    def test_hm3i_run_projection_is_timestamp_invariant_for_all_literal_run_traces(self) -> None:
        """Future, backward, equal, and DST testimony cannot reorder any physical run truth."""
        timestamp_cycle = (
            "2036-01-01T00:00:00.000Z",
            "2020-01-01T00:00:00.000Z",
            "2026-11-01T05:59:59.999Z",
            "2026-11-01T06:00:00.000Z",
            "2026-11-01T06:00:00.000Z",
        )
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            traces = build_full_run_trace_set(base)
            self.assertTrue(all(axis_coverage_from_traces("time", traces).values()))
            for trace in traces:
                with self.subTest(run_id=trace.run_id):
                    expected = assert_physical_projection(trace)
                    rewritten = [dict(record) for record in trace.records]
                    for index, record in enumerate(rewritten):
                        record["timestamp"] = timestamp_cycle[index % len(timestamp_cycle)]
                    self.assertEqual(
                        expected,
                        canonical_observation_from_records(trace, rewritten),
                    )


if __name__ == "__main__":
    unittest.main()

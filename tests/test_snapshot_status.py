from __future__ import annotations

from floati import fixture_ids as public_ids

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from floati.planes import LivenessPresenceStore
from floati.projection import FleetProjection
from floati.registry import Registry
from floati.root import FloatiRoot
from floati.work import WorkLog
from tests.bus_scale_io import IOProbe


NOW = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)


class StatusSnapshotTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = FloatiRoot.open_direct_home(
            Path(self.temp.name) / "status-snapshot", create=True
        )
        Registry(self.root).register(public_ids.worker('alpha'), "worker")
        WorkLog(self.root).add("first item", public_ids.worker('alpha'), [], now=NOW)

    def test_stable_status_and_work_tail_do_not_full_scan(self) -> None:
        projection = FleetProjection(self.root)
        first = projection.status_artifact(NOW)

        with patch.object(
            FleetProjection,
            "_snapshot",
            side_effect=AssertionError("stable status performed a full scan"),
        ):
            stable = projection.status_artifact(NOW + timedelta(seconds=1))
        self.assertNotEqual(first["observed_at"], stable["observed_at"])
        self.assertEqual(first["work_counts"], stable["work_counts"])

        WorkLog(self.root).add(
            "second item", public_ids.worker('alpha'), [], now=NOW + timedelta(seconds=2)
        )
        with patch.object(
            FleetProjection,
            "_snapshot",
            side_effect=AssertionError("work tail performed a full scan"),
        ):
            tailed = projection.status_artifact(NOW + timedelta(seconds=2))
        self.assertEqual(
            first["work_counts"]["open"] + 1,
            tailed["work_counts"]["open"],
        )

    def test_liveness_clock_boundary_falls_back_and_expires(self) -> None:
        LivenessPresenceStore(self.root).observe(public_ids.worker('alpha'), 10, NOW)
        projection = FleetProjection(self.root)
        before = projection.status_artifact(NOW + timedelta(seconds=4))

        with patch.object(
            FleetProjection,
            "_snapshot",
            wraps=projection._snapshot,
        ) as full_scan:
            after = projection.status_artifact(NOW + timedelta(seconds=10))

        self.assertEqual(1, full_scan.call_count)
        self.assertEqual("present", before["nodes"][0]["liveness"])
        self.assertEqual("expired", after["nodes"][0]["liveness"])

    def test_warm_mail_status_reprojects_the_clock_without_replaying_verified_history(self) -> None:
        """Current fleet truth must not replay acknowledged mail at every clock tick."""
        from floati.events import EventLog
        from floati.cursor import SparseCursor

        node = public_ids.worker('alpha')
        log = EventLog(self.root)
        # Keep this live-epoch property inside the native maintenance age
        # policy; historical clocks have separate archived-lineage controls.
        now = datetime.now(timezone.utc).replace(microsecond=0)
        stamp = now.isoformat(timespec="milliseconds").replace("+00:00", "Z")
        with patch("floati.events.utc_now", return_value=stamp):
            message = log.send(node, node, "floati", "a" * 40,
                "note.md", "status read bound", idempotency_key="status-read-bound")
            log.present(node, now=now)
        SparseCursor(self.root).ack(node, [message["id"]], acting_session_id="status-seen", now=now)
        LivenessPresenceStore(self.root).observe(node, 10, now)
        projection = FleetProjection(self.root)
        projection.status_artifact(now + timedelta(seconds=4))
        later = now + timedelta(seconds=10)
        expected, mode = projection._snapshot(later)
        with IOProbe(self.root.tenant_home) as probe:
            actual = FleetProjection(self.root).status_artifact(later)
        probe.assert_complete()
        self.assertEqual(mode, actual["mode"])
        self.assertEqual(expected, {key: actual[key] for key in expected})
        self.assertEqual("expired", actual["nodes"][0]["liveness"])
        for relative in ("events.jsonl", f"receipts/acks/{node}.jsonl",
                         f"receipts/deliveries/{node}.jsonl"):
            self.assertEqual(0, probe.read_bytes_by_path.get(relative, 0),
                "warm current status replayed already verified mail history: " + relative)

    def test_summary_status_preserves_the_native_backdated_ack_refusal(self) -> None:
        from floati.cursor import SparseCursor
        from floati.errors import IntegrityFailure
        from floati.events import EventLog
        from floati.supervisor import Supervisor
        from floati.wake_health import WakeHealthProjection

        node = public_ids.worker('alpha')
        now = datetime.now(timezone.utc).replace(microsecond=0)
        log = EventLog(self.root)
        message = log.send(node, node, "floati", "a" * 40,
            "note.md", "backdated acknowledgment", idempotency_key="backdated-status", now=now)
        delivered = now + timedelta(seconds=5)
        with patch("floati.events.utc_now", return_value=delivered.isoformat(
                timespec="milliseconds").replace("+00:00", "Z")):
            log.present(node, now=delivered)
        SparseCursor(self.root).ack(node, [message["id"]], acting_session_id="backdated",
            now=now + timedelta(seconds=3))
        later = now + timedelta(seconds=10)
        with self.assertRaises(IntegrityFailure) as native:
            FleetProjection(self.root)._snapshot(later)
        self.assertEqual("acknowledgment_receipt_order_invalid", native.exception.code)
        for query in (lambda: FleetProjection(self.root).status_artifact(later),
                      lambda: Supervisor(self.root).snapshot(later),
                      lambda: WakeHealthProjection(self.root).fact(node, later)):
            with self.subTest(query=query):
                with self.assertRaises(IntegrityFailure) as observed:
                    query()
                self.assertEqual(native.exception.code, observed.exception.code)

    def test_optional_mail_summary_preserves_native_foreign_empty_receipts(self) -> None:
        """An optional certificate must not add a native filename refusal."""
        from floati.events import EventLog
        from floati.supervisor import Supervisor
        from floati.wake_health import WakeHealthProjection

        node = public_ids.worker('alpha')
        now = datetime.now(timezone.utc).replace(microsecond=0)
        EventLog(self.root).send(node, node, "floati", "a" * 40, "note.md",
            "optional summary", idempotency_key="optional-summary", now=now)
        queries = (
            lambda: FleetProjection(self.root).status_artifact(now),
            lambda: Supervisor(self.root).snapshot(now),
            lambda: WakeHealthProjection(self.root).fact(node, now),
        )
        for family in ("acks", "deliveries", "consumed"):
            path = self.root.resolve_relative(Path("receipts") / family / "bad!.jsonl")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"")
            try:
                for query in queries:
                    with self.subTest(family=family, query=query):
                        with patch("floati.mail_summary.observe", return_value=None):
                            expected = query()
                        self.assertEqual(expected, query())
            finally:
                path.unlink()


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

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


NOW = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)


class StatusSnapshotTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = FloatiRoot.open_direct_home(
            Path(self.temp.name) / "status-snapshot", create=True
        )
        Registry(self.root).register("alice", "worker")
        WorkLog(self.root).add("first item", "alice", [], now=NOW)

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
            "second item", "alice", [], now=NOW + timedelta(seconds=2)
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
        LivenessPresenceStore(self.root).observe("alice", 10, NOW)
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


if __name__ == "__main__":
    unittest.main()

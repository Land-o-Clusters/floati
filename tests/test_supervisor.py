from __future__ import annotations

from floati import fixture_ids as public_ids

import hashlib
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from floati.events import EventLog
from floati.planes import AuthorityGrantStore, LivenessPresenceStore, MutualExclusionHoldStore
from floati.registry import Registry
from floati.root import FloatiRoot
from floati.work import WorkLog
from floati.workers import WorkerReceipts


NOW = datetime(2026, 7, 31, 12, 0, 0, tzinfo=timezone.utc)


def tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(b"d" if path.is_dir() else b"f")
        if path.is_file():
            digest.update(path.read_bytes())
    return digest.hexdigest()


class SupervisorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name) / "fleet"
        self.root = FloatiRoot.open_direct_home(self.home, create=True)
        registry = Registry(self.root)
        registry.register(public_ids.worker('alpha'), "Codex")
        registry.register("bravo", "Codex")
        LivenessPresenceStore(self.root).observe(public_ids.worker('alpha'), 60, NOW)
        AuthorityGrantStore(self.root).claim("build", public_ids.worker('alpha'), 60, 60, NOW)
        MutualExclusionHoldStore(self.root).acquire("workspace", "bravo", 5, 5, NOW)
        EventLog(self.root).send(
            public_ids.worker('alpha'), "bravo", "floati", "a" * 40,
            "docs/evidence/checkpoint.md", "notice", idempotency_key="supervisor-mail",
        )

    def test_snapshot_keeps_three_planes_distinct_and_names_stale_leases(self) -> None:
        from floati.supervisor import Supervisor

        snapshot = Supervisor(self.root).snapshot(NOW + timedelta(seconds=10))
        nodes = {node["node_id"]: node for node in snapshot["nodes"]}

        self.assertEqual("present", nodes[public_ids.worker('alpha')]["liveness"])
        self.assertEqual("active", nodes[public_ids.worker('alpha')]["authority"])
        self.assertEqual("none", nodes[public_ids.worker('alpha')]["mutex"])
        self.assertEqual("unknown", nodes["bravo"]["liveness"])
        self.assertEqual("none", nodes["bravo"]["authority"])
        self.assertEqual("expired", nodes["bravo"]["mutex"])
        self.assertEqual(1, nodes["bravo"]["inbox_depth"])
        self.assertEqual(0, nodes[public_ids.worker('alpha')]["inbox_depth"])
        self.assertEqual(
            [{
                "plane": "mutex",
                "subject_id": "workspace",
                "holder": "bravo",
                "epoch": 1,
                "expires_at": "2026-07-31T12:00:05.000Z",
            }],
            snapshot["stale_leases"],
        )
        self.assertEqual(
            {"confirmed": 0, "failed": 0, "unknown": 0, "incomplete": 0},
            snapshot["effects"]["counts"],
        )

    def test_snapshot_is_physically_read_only(self) -> None:
        from floati.supervisor import Supervisor

        before = tree_digest(self.home)
        snapshot = Supervisor(self.root).snapshot(NOW + timedelta(seconds=10))
        after = tree_digest(self.home)

        self.assertEqual(before, after)
        self.assertEqual("report_only", snapshot["mode"])
        self.assertFalse(any(path.name.endswith(".lock") and path.stat().st_size for path in self.home.rglob("*.lock")))

    def test_effects_expose_shared_attention_rank(self) -> None:
        from floati.supervisor import Supervisor
        from tests.test_effect_cli import attention_effect_rows, write_effect_rows

        write_effect_rows(self.root, attention_effect_rows())

        snapshot = Supervisor(self.root).snapshot(NOW + timedelta(seconds=10))

        self.assertEqual(
            ["unknown", "incomplete", "failed", "confirmed"],
            [row["state"] for row in snapshot["effects"]["attention"]],
        )
        self.assertEqual([1, 1, 1, 1], [
            row["count"] for row in snapshot["effects"]["attention"]
        ])

    def test_snapshot_reports_workers_from_receipts_without_taking_action(self) -> None:
        work = WorkLog(self.root)
        item = work.add("worker report", public_ids.worker('alpha'), [], now=NOW)
        work.claim(item["id"], public_ids.worker('alpha'), "build", 1, now=NOW)
        WorkerReceipts(self.root).append(
            "worker-018f0f23abcd71238000000000000000",
            item["id"], public_ids.worker('alpha'), "codex", "claim", None, [], now=NOW,
        )
        before = tree_digest(self.home)

        from floati.supervisor import Supervisor
        snapshot = Supervisor(self.root).snapshot(NOW + timedelta(seconds=10))

        self.assertEqual(before, tree_digest(self.home))
        self.assertEqual("claim", snapshot["workers"][0]["state"])
        self.assertEqual("report_only", snapshot["mode"])


if __name__ == "__main__":
    unittest.main()

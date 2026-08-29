from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from floati.errors import ProtocolRefusal
from floati.locks.git_observer import GitObserver
from floati.locks.ledger import LockLedger
from floati.locks.queue import PatchQueue
from floati.root import FloatiRoot


NOW = "2026-08-26T20:00:00.000Z"
WITNESS = {
    "kind": "file_contains_utf8",
    "path": "feature.txt",
    "needle": "locks-feature",
}


class LocksQueueTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name).resolve()
        self.repository = self.base / "repository"
        self.git(self.base, "init", "--quiet", "--initial-branch=main", str(self.repository))
        self.git(self.repository, "config", "user.name", "Locks Fixture")
        self.git(self.repository, "config", "user.email", "locks@example.invalid")
        (self.repository / "base.txt").write_text("base\n", encoding="utf-8")
        self.git(self.repository, "add", "base.txt")
        self.git(self.repository, "commit", "--quiet", "-m", "base")
        self.git(self.repository, "branch", "product")
        self.git(self.repository, "switch", "--quiet", "-c", "car-one")
        (self.repository / "feature.txt").write_text("locks-feature\n", encoding="utf-8")
        self.git(self.repository, "add", "feature.txt")
        self.git(self.repository, "commit", "--quiet", "-m", "car feature")
        self.source_sha = self.git(self.repository, "rev-parse", "refs/heads/car-one")
        state = self.base / "state"
        state.mkdir()
        self.ledger = LockLedger(FloatiRoot.open(state, "alpha"))
        self.observer = GitObserver(self.repository)
        self.queue = PatchQueue(self.ledger, self.observer)

    @staticmethod
    def git(cwd: Path, *arguments: str) -> str:
        completed = subprocess.run(
            ["/usr/bin/git", *arguments],
            cwd=cwd,
            check=True,
            capture_output=True,
            text=True,
            env={"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"},
        )
        return completed.stdout.strip()

    def submit_car_one(self) -> None:
        self.queue.submit(
            car_id="car-one",
            ref="refs/heads/car-one",
            witness=WITNESS,
            now=NOW,
        )

    def create_and_submit_car_two(self) -> None:
        self.git(self.repository, "switch", "--quiet", "main")
        self.git(self.repository, "switch", "--quiet", "-c", "car-two")
        (self.repository / "second.txt").write_text("second-locks-feature\n", encoding="utf-8")
        self.git(self.repository, "add", "second.txt")
        self.git(self.repository, "commit", "--quiet", "-m", "second car feature")
        self.queue.submit(
            car_id="car-two",
            ref="refs/heads/car-two",
            witness={
                "kind": "file_contains_utf8",
                "path": "second.txt",
                "needle": "second-locks-feature",
            },
            now=NOW,
        )

    def test_detached_head_cannot_be_submitted(self) -> None:
        """Catches a resolvable detached HEAD being mistaken for a queue ref."""

        with self.assertRaises(ProtocolRefusal) as caught:
            self.queue.submit(car_id="car-one", ref="HEAD", witness=WITNESS, now=NOW)
        self.assertEqual("car_ref_required", caught.exception.code)
        self.assertEqual({}, self.ledger.snapshot().cars)

    def test_cherry_pick_with_different_sha_is_landed_by_content(self) -> None:
        """Catches ancestry or SHA equality replacing the declared content witness."""

        self.submit_car_one()
        self.assertEqual(
            "not_landed",
            self.queue.landing_status("car-one", "refs/heads/product"),
        )

        self.git(self.repository, "switch", "--quiet", "product")
        (self.repository / "product-base.txt").write_text("different parent\n", encoding="utf-8")
        self.git(self.repository, "add", "product-base.txt")
        self.git(self.repository, "commit", "--quiet", "-m", "move product base")
        self.git(self.repository, "cherry-pick", "--quiet", self.source_sha)
        target_sha = self.git(self.repository, "rev-parse", "refs/heads/product")

        self.assertNotEqual(self.source_sha, target_sha)
        self.assertEqual(
            "landed",
            self.queue.landing_status("car-one", "refs/heads/product"),
        )

    def test_blocked_car_does_not_block_lower_arrival_approved_car(self) -> None:
        """Catches physical arrival order allowing one blocker to stop the train."""

        self.submit_car_one()
        self.create_and_submit_car_two()
        self.queue.record_review(
            "car-one",
            verdict="blocked",
            rank=100,
            base_ref="refs/heads/product",
            now=NOW,
        )
        self.queue.record_review(
            "car-two",
            verdict="approved",
            rank=70,
            base_ref="refs/heads/product",
            now=NOW,
        )
        self.assertEqual(
            "car-two",
            self.queue.select_next("refs/heads/product").car_id,
        )

    def test_moved_base_rederives_verdict_from_the_same_witness(self) -> None:
        """Catches verdict labels surviving a changed witness measurement."""

        self.submit_car_one()
        self.queue.record_review(
            "car-one",
            verdict="approved",
            rank=80,
            base_ref="refs/heads/product",
            now=NOW,
        )
        self.git(self.repository, "switch", "--quiet", "product")
        (self.repository / "unrelated.txt").write_text("base moved\n", encoding="utf-8")
        self.git(self.repository, "add", "unrelated.txt")
        self.git(self.repository, "commit", "--quiet", "-m", "move base without witness")
        self.assertEqual(
            "approved_rederived",
            self.queue.review_status("car-one", "refs/heads/product"),
        )

        (self.repository / "feature.txt").write_text("locks-feature\n", encoding="utf-8")
        self.git(self.repository, "add", "feature.txt")
        self.git(self.repository, "commit", "--quiet", "-m", "change witness result")
        self.assertEqual(
            "review_required",
            self.queue.review_status("car-one", "refs/heads/product"),
        )

    def test_measurement_transfers_only_when_exact_tree_diff_is_empty(self) -> None:
        """Catches an argument or ancestry claim replacing an exact tree diff."""

        self.git(self.repository, "branch", "identical", "refs/heads/car-one")
        self.assertTrue(
            self.observer.trees_agree("refs/heads/car-one", "refs/heads/identical"),
        )
        self.assertFalse(
            self.observer.trees_agree("refs/heads/car-one", "refs/heads/product"),
        )

    def test_land_next_records_only_after_executor_and_content_verification(self) -> None:
        """Catches a planned or attempted landing being recorded as landed."""

        self.submit_car_one()
        self.queue.record_review(
            "car-one",
            verdict="approved",
            rank=80,
            base_ref="refs/heads/product",
            now=NOW,
        )

        def executor(plan) -> None:
            self.assertEqual("car-one", plan.car_id)
            self.assertEqual("cherry_pick", plan.method)
            self.git(self.repository, "switch", "--quiet", "product")
            self.git(self.repository, "cherry-pick", "--quiet", self.source_sha)

        row = self.queue.land_next(
            "refs/heads/product",
            executor,
            method="cherry_pick",
            now=NOW,
        )
        self.assertEqual("car_landed", row["kind"])
        self.assertEqual("landed", self.queue.landing_status("car-one", "refs/heads/product"))
        self.assertEqual("landed", self.ledger.snapshot().cars["car-one"].state)

    def test_dissolution_requires_content_on_the_explicit_product_ref(self) -> None:
        """Catches a done claim proved only on the car's unmerged branch."""

        self.submit_car_one()
        with self.assertRaises(ProtocolRefusal) as caught:
            self.queue.dissolve("car-one", product_ref="refs/heads/product", now=NOW)
        self.assertEqual("car_not_landed_on_product", caught.exception.code)
        self.git(self.repository, "switch", "--quiet", "product")
        self.git(self.repository, "cherry-pick", "--quiet", self.source_sha)
        row = self.queue.dissolve("car-one", product_ref="refs/heads/product", now=NOW)
        self.assertEqual("car_dissolved", row["kind"])
        self.assertEqual("dissolved", self.ledger.snapshot().cars["car-one"].state)


if __name__ == "__main__":
    unittest.main()

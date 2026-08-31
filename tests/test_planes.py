from __future__ import annotations

from floati import fixture_ids as public_ids

import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from floati.errors import ProtocolRefusal
from floati.jsonl import read_records
from floati.root import FloatiRoot

try:
    from floati.planes import LivenessPresenceStore
except (ImportError, ModuleNotFoundError):
    LivenessPresenceStore = None

try:
    from floati.planes import AuthorityGrantStore
except (ImportError, ModuleNotFoundError):
    AuthorityGrantStore = None

try:
    from floati.planes import MutualExclusionHoldStore
except (ImportError, ModuleNotFoundError):
    MutualExclusionHoldStore = None


NOW = datetime(2026, 7, 31, 12, 0, 0, tzinfo=timezone.utc)


class PlaneTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = FloatiRoot.open(Path(self.temp.name), "alpha")


class LivenessTests(PlaneTestCase):
    def setUp(self) -> None:
        self.assertIsNotNone(LivenessPresenceStore, "floati.planes must implement liveness evidence")
        super().setUp()
        self.store = LivenessPresenceStore(self.root)

    def test_present_silent_and_expired_are_distinct(self) -> None:
        record = self.store.observe(public_ids.worker('alpha'), ttl_seconds=10, now=NOW)
        self.assertEqual("liveness_presence", record["kind"])
        self.assertEqual("present", self.store.status(public_ids.worker('alpha'), NOW + timedelta(seconds=4)))
        self.assertEqual("silent", self.store.status(public_ids.worker('alpha'), NOW + timedelta(seconds=5)))
        self.assertEqual("expired", self.store.status(public_ids.worker('alpha'), NOW + timedelta(seconds=10)))

    def test_observation_time_cannot_move_backward(self) -> None:
        self.store.observe(public_ids.worker('alpha'), 10, NOW)
        before = self.store.path_for(public_ids.worker('alpha')).read_bytes()
        with self.assertRaises(ProtocolRefusal) as caught:
            self.store.observe(public_ids.worker('alpha'), 10, NOW - timedelta(seconds=1))
        self.assertEqual("time_regression", caught.exception.code)
        self.assertEqual(before, self.store.path_for(public_ids.worker('alpha')).read_bytes())

    def test_liveness_has_its_own_path_and_rejects_invalid_ttl(self) -> None:
        path = self.store.path_for(public_ids.worker('alpha'))
        with self.assertRaises(ProtocolRefusal) as caught:
            self.store.observe(public_ids.worker('alpha'), ttl_seconds=0, now=NOW)
        self.assertEqual("ttl_invalid", caught.exception.code)
        self.assertFalse(path.exists())
        self.store.observe(public_ids.worker('alpha'), ttl_seconds=10, now=NOW)
        self.assertIn("liveness-presence", path.parts)
        self.assertNotIn("authority-grants", path.parts)
        self.assertNotIn("mutual-exclusion-holds", path.parts)


class AuthorityTests(PlaneTestCase):
    def setUp(self) -> None:
        self.assertIsNotNone(AuthorityGrantStore, "floati.planes must implement authority CAS")
        super().setUp()
        self.store = AuthorityGrantStore(self.root)

    def test_claim_renew_release_and_new_epoch(self) -> None:
        claimed = self.store.claim("build", public_ids.worker('alpha'), 10, 8, NOW)
        self.assertEqual(1, claimed["epoch"])
        self.assertEqual("authority_grant", claimed["kind"])
        renewed = self.store.renew("build", public_ids.worker('alpha'), 1, 10, 10, NOW + timedelta(seconds=1))
        self.assertEqual(1, renewed["epoch"])
        self.assertGreater(renewed["expires_at"], claimed["expires_at"])
        released = self.store.release("build", public_ids.worker('alpha'), 1, NOW + timedelta(seconds=2))
        self.assertEqual("released", released["state"])
        next_claim = self.store.claim("build", "bravo", 10, 5, NOW + timedelta(seconds=3))
        self.assertEqual(2, next_claim["epoch"])
        self.assertEqual("bravo", next_claim["holder"])

    def test_exact_tail_read_creates_no_lock_or_other_files(self) -> None:
        """Catches controller evaluation turning an authority read into mutation."""
        claimed = self.store.claim("build", public_ids.worker('alpha'), 10, 8, NOW)
        path = self.store.path_for("build")
        read_lock = path.with_name(path.name + ".lock")
        if read_lock.exists():
            read_lock.unlink()
        before = sorted(item.relative_to(self.root.tenant_home) for item in self.root.tenant_home.rglob("*"))
        self.assertEqual(claimed, self.store.exact_tail("build"))
        after = sorted(item.relative_to(self.root.tenant_home) for item in self.root.tenant_home.rglob("*"))
        self.assertEqual(before, after)
        self.assertFalse(read_lock.exists())

    def test_exact_tail_missing_authority_creates_no_files(self) -> None:
        """Catches a fresh read manufacturing authority synchronization state."""
        before = sorted(
            item.relative_to(self.root.tenant_home)
            for item in self.root.tenant_home.rglob("*")
        )
        with self.assertRaises(ProtocolRefusal) as caught:
            self.store.exact_tail("missing")
        self.assertEqual("authority_missing", caught.exception.code)
        after = sorted(
            item.relative_to(self.root.tenant_home)
            for item in self.root.tenant_home.rglob("*")
        )
        self.assertEqual(before, after)
        self.assertFalse(self.store.path_for("missing").exists())

    def test_expire_records_an_expired_terminal_state_at_the_observation(self) -> None:
        self.store.claim("build", public_ids.worker('alpha'), 10, 8, NOW)

        expired = self.store.expire(
            "build", public_ids.worker('alpha'), 1, NOW + timedelta(seconds=2)
        )

        self.assertEqual("expired", expired["state"])
        self.assertEqual("2026-07-31T12:00:02.000Z", expired["expires_at"])
        self.assertIsNone(expired["released_at"])
        with self.assertRaises(ProtocolRefusal) as caught:
            self.store.renew(
                "build", public_ids.worker('alpha'), 1, 10, 8, NOW + timedelta(seconds=3)
            )
        self.assertEqual("authority_released", caught.exception.code)

    def test_deadline_invariant_accepts_both_valid_directions_and_refuses_inverse(self) -> None:
        shorter = self.store.claim("shorter", public_ids.worker('alpha'), 10, 9, NOW)
        equal = self.store.claim("equal", public_ids.worker('alpha'), 10, 10, NOW)
        self.assertEqual(9, shorter["deadline_seconds"])
        self.assertEqual(10, equal["deadline_seconds"])
        refused_path = self.store.path_for("inverse")
        with self.assertRaises(ProtocolRefusal) as caught:
            self.store.claim("inverse", public_ids.worker('alpha'), 9, 10, NOW)
        self.assertEqual("deadline_exceeds_ttl", caught.exception.code)
        self.assertFalse(refused_path.exists())

    def test_expired_or_stale_holder_cannot_renew_or_release(self) -> None:
        self.store.claim("build", public_ids.worker('alpha'), 10, 10, NOW)
        path = self.store.path_for("build")
        before = path.read_bytes()
        cases = (
            (lambda: self.store.renew("build", "bravo", 1, 10, 10, NOW + timedelta(seconds=1)), "holder_mismatch"),
            (lambda: self.store.release("build", public_ids.worker('alpha'), 2, NOW + timedelta(seconds=1)), "epoch_mismatch"),
            (lambda: self.store.renew("build", public_ids.worker('alpha'), 1, 10, 10, NOW + timedelta(seconds=10)), "authority_expired"),
        )
        for operation, reason in cases:
            with self.subTest(reason=reason):
                with self.assertRaises(ProtocolRefusal) as caught:
                    operation()
                self.assertEqual(reason, caught.exception.code)
                self.assertEqual(before, path.read_bytes())

    def test_boolean_epoch_and_backward_time_refuse_without_mutation(self) -> None:
        self.store.claim("build", public_ids.worker('alpha'), 10, 10, NOW)
        path = self.store.path_for("build")
        before = path.read_bytes()
        for operation, code in (
            (lambda: self.store.renew("build", public_ids.worker('alpha'), True, 10, 10, NOW + timedelta(seconds=1)), "epoch_invalid"),
            (lambda: self.store.renew("build", public_ids.worker('alpha'), 1, 10, 10, NOW - timedelta(seconds=1)), "time_regression"),
            (lambda: self.store.release("build", public_ids.worker('alpha'), 1, NOW - timedelta(seconds=1)), "time_regression"),
        ):
            with self.subTest(code=code):
                with self.assertRaises(ProtocolRefusal) as caught:
                    operation()
                self.assertEqual(code, caught.exception.code)
                self.assertEqual(before, path.read_bytes())

    def test_concurrent_claim_has_exactly_one_winner(self) -> None:
        barrier = threading.Barrier(2)
        winners = []
        refusals = []

        def compete(holder: str) -> None:
            barrier.wait()
            try:
                winners.append(self.store.claim("shared", holder, 10, 10, NOW))
            except ProtocolRefusal as exc:
                refusals.append(exc.code)

        threads = [threading.Thread(target=compete, args=(holder,)) for holder in (public_ids.worker('alpha'), "bravo")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=2)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(1, len(winners))
        self.assertEqual(["authority_held"], refusals)
        self.assertEqual(1, len(read_records(self.root, "authority-grants/shared.jsonl", allowed_kinds={"authority_grant"})))


class MutualExclusionTests(PlaneTestCase):
    def setUp(self) -> None:
        self.assertIsNotNone(MutualExclusionHoldStore, "floati.planes must implement exclusion CAS")
        super().setUp()
        self.store = MutualExclusionHoldStore(self.root)

    def test_acquire_renew_release_and_expiry_takeover(self) -> None:
        acquired = self.store.acquire("workspace", public_ids.worker('alpha'), 10, 10, NOW)
        self.assertEqual("mutual_exclusion_hold", acquired["kind"])
        self.assertEqual(1, acquired["epoch"])
        renewed = self.store.renew("workspace", public_ids.worker('alpha'), 1, 10, 8, NOW + timedelta(seconds=1))
        self.assertEqual(1, renewed["epoch"])
        released = self.store.release("workspace", public_ids.worker('alpha'), 1, NOW + timedelta(seconds=2))
        self.assertEqual("released", released["state"])
        acquired_again = self.store.acquire("workspace", "bravo", 10, 10, NOW + timedelta(seconds=3))
        self.assertEqual(2, acquired_again["epoch"])
        expired_takeover = self.store.acquire("other", public_ids.worker('alpha'), 2, 2, NOW)
        self.assertEqual(1, expired_takeover["epoch"])
        after_expiry = self.store.acquire("other", "bravo", 2, 2, NOW + timedelta(seconds=2))
        self.assertEqual(2, after_expiry["epoch"])

    def test_exclusion_is_separate_and_enforces_deadline(self) -> None:
        path = self.store.path_for("workspace")
        with self.assertRaises(ProtocolRefusal) as caught:
            self.store.acquire("workspace", public_ids.worker('alpha'), 5, 6, NOW)
        self.assertEqual("deadline_exceeds_ttl", caught.exception.code)
        self.assertFalse(path.exists())
        self.store.acquire("workspace", public_ids.worker('alpha'), 5, 5, NOW)
        self.assertIn("mutual-exclusion-holds", path.parts)
        self.assertNotIn("authority-grants", path.parts)
        self.assertNotIn("liveness-presence", path.parts)


if __name__ == "__main__":
    unittest.main()

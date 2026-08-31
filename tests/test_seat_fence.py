from __future__ import annotations

from floati import fixture_ids as public_ids

import os
import tempfile
import unittest
from pathlib import Path

from floati.errors import ProtocolRefusal
from floati.registry import Registry
from floati.root import FloatiRoot


REPOSITORY_ROOT = Path(__file__).parents[1]


class SeatFenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = FloatiRoot.open_direct_home(
            Path(self.temporary.name) / "fleet", create=True
        )
        Registry(self.root).register(public_ids.builder('current'), "Codex")
        self.committer_email = f"builder-current@{self.root.tenant_id}"

    def test_active_registry_node_and_root_tenant_define_committer_identity(self) -> None:
        """Catches a fence pinning either the seat or its tenant identity."""
        from floati.seat_fence import validate_seat_fence

        evidence = validate_seat_fence(
            self.root,
            public_ids.builder('current'),
            committer_name=public_ids.builder('current'),
            committer_email=self.committer_email,
        )

        self.assertEqual(public_ids.builder('current'), evidence["node_id"])
        self.assertEqual("active_registry_entry", evidence["identity_source"])

        for name, email in (
            (public_ids.worker('necro'), f"worker-necro@{self.root.tenant_id}"),
            (public_ids.builder('current'), "owner@example.invalid"),
        ):
            with self.subTest(name=name, email=email):
                with self.assertRaises(ProtocolRefusal):
                    validate_seat_fence(
                        self.root,
                        public_ids.builder('current'),
                        committer_name=name,
                        committer_email=email,
                    )

    def test_validation_is_physically_read_only_and_creates_no_lock(self) -> None:
        """Catches a pre-commit identity read requiring write access to the fleet root."""
        from floati.seat_fence import validate_seat_fence

        registry_directory = self.root.path / "registry"
        lock = registry_directory / "entries.jsonl.lock"
        lock.unlink()
        os.chmod(registry_directory, 0o500)
        self.addCleanup(os.chmod, registry_directory, 0o700)

        validate_seat_fence(
            self.root,
            public_ids.builder('current'),
            committer_name=public_ids.builder('current'),
            committer_email=self.committer_email,
        )

        self.assertFalse(lock.exists())

    def test_retired_configured_seat_refuses(self) -> None:
        """Catches durable worktree configuration outliving the active registry seat."""
        from floati.seat_fence import validate_seat_fence

        Registry(self.root).retire(public_ids.builder('current'))
        with self.assertRaises(ProtocolRefusal) as raised:
            validate_seat_fence(
                self.root,
                public_ids.builder('current'),
                committer_name=public_ids.builder('current'),
                committer_email=self.committer_email,
            )
        self.assertEqual("seat_fence_node_inactive", raised.exception.code)

    def test_hook_is_opt_in_and_contains_no_hardcoded_historical_seat(self) -> None:
        """Catches a common hooksPath imposing one seat on every linked worktree."""
        hook = (REPOSITORY_ROOT / ".githooks" / "pre-commit").read_text(encoding="utf-8")

        self.assertNotIn(public_ids.worker('necro'), hook)
        self.assertIn("floati.seatFenceRoot", hook)
        self.assertIn("floati.seatFenceNode", hook)
        self.assertIn("if [ -z \"$seat_root\" ] && [ -z \"$seat_node\" ]", hook)


if __name__ == "__main__":
    unittest.main()

"""Task1 profile lease/row boundaries; all effects are scratch-local."""
from __future__ import annotations

import json
import stat
import unittest
from datetime import timedelta

from tests.fixtures.adhoc_stage1 import AdhocFixture, declared_api


class AdhocProfileTests(AdhocFixture, unittest.TestCase):
    def setUp(self):
        declared_api(self, "floati.adhoc_profiles", "AdhocProfiles")
        super().setUp()

    def test_profile_binds_exact_temporary_lease_seat_and_recorded_row(self):
        """Catches admitting arbitrary coordinates or failing to isolate provider history."""
        profile = self.profile()
        self.assertEqual("temporary", profile["node_id"])
        self.assertEqual(self.lease["id"], profile["node_lease_id"])
        self.assertEqual(self.opened["record"]["id"], profile["lane_opening_id"])
        self.assertEqual(str(self.workspace), profile["workspace"])
        self.assertEqual("codex/lane/temporary/adhoc-one", profile["branch"])
        self.assertEqual(str(self.repository / ".git"), profile["git_common_dir"])
        slot = self.root.path / "provider-state" / profile["id"]
        self.assertTrue(slot.is_dir())
        self.assertEqual(0o700, stat.S_IMODE(slot.stat().st_mode))
        expected_paths = {
            "root": self.root.path,
            "node_home": self.root.path / "nodes/temporary",
            "seat_file": self.root.path / "nodes/temporary/SEAT.json",
            "workspace": self.workspace,
            "git_common_dir": self.repository / ".git",
            "git_admin_dir": self.repository / ".git/worktrees/adhoc-one",
            "provider_state": slot,
        }
        self.assertEqual(set(expected_paths), set(profile["filesystem_bindings"]))
        for coordinate, path in expected_paths.items():
            identity = path.stat()
            self.assertEqual({"dev": identity.st_dev, "ino": identity.st_ino},
                             profile["filesystem_bindings"][coordinate])
        self.assertEqual(profile, self.profiles().get(profile["id"]))
        self.assertEqual(profile, self.profile(), "same key must replay its exact profile")
        self.assert_no_provider_effects()

    def test_foreign_lease_refuses_without_profile_side_effects(self):
        """Catches trusting the active lease label without its exact owned HOME."""
        self.add_lease("temporary", workspace=self.root.path / "nodes" / "foreign")
        service = self.profiles()
        before = self.tree_bytes()
        self.refuses("adhoc_lease_invalid", lambda: service.create(
            "temporary", "codex", self.opened["record"]["id"], "fixture", "coordinator",
            idempotency_key="foreign-lease", now=self.now))
        self.assertEqual(before, self.tree_bytes())
        self.assert_no_provider_effects()

    def test_permanent_node_refuses_without_provider_slot(self):
        """Catches silently treating permanent nodes as leased ad-hoc tenants."""
        service = self.profiles()
        opened = self.lanes.open(actor="permanent", row="permanent-one", repo="fixture")
        before = self.tree_bytes()
        self.refuses("adhoc_lease_invalid", lambda: service.create(
            "permanent", "codex", opened["record"]["id"], "fixture", "coordinator",
            idempotency_key="permanent-profile", now=self.now))
        self.assertEqual(before, self.tree_bytes())

    def test_expired_lease_refuses_without_profile_side_effects(self):
        """Neither an expired lease nor another node's genuine row may author a profile."""
        service = self.profiles()
        cases = (
            ("expired-profile", self.opened["record"]["id"],
             self.now + timedelta(hours=1), "adhoc_lease_invalid"),
            ("foreign-row", self.foreign_opened["record"]["id"],
             self.now, "adhoc_binding_changed"),
        )
        for key, opening, now, code in cases:
            with self.subTest(case=key):
                before = self.tree_bytes()
                git_before = self.tree_bytes(self.repository / ".git")
                self.refuses(code, lambda: service.create(
                    "temporary", "codex", opening, "fixture", "coordinator",
                    idempotency_key=key, now=now))
                self.assertEqual(before, self.tree_bytes())
                self.assertEqual(git_before, self.tree_bytes(self.repository / ".git"))

    def test_foreign_seat_refuses_without_profile_side_effects(self):
        """Catches binding a HOME whose SEAT names a different registry identity."""
        service = self.profiles()
        seat = self.root.path / "nodes/temporary/SEAT.json"
        data = json.loads(seat.read_text())
        data["node_id"] = "foreign"
        seat.write_text(json.dumps(data) + "\n")
        before = self.tree_bytes()
        self.refuses("adhoc_binding_changed", lambda: service.create(
            "temporary", "codex", self.opened["record"]["id"], "fixture", "coordinator",
            idempotency_key="foreign-seat", now=self.now))
        self.assertEqual(before, self.tree_bytes())

    def test_changed_owned_branch_refuses_without_profile_side_effects(self):
        """Catches trusting a path after its current branch stops matching the opening."""
        service = self.profiles()
        self.git(self.workspace, "switch", "--quiet", "-c", "codex/substituted")
        before = self.tree_bytes()
        self.refuses("adhoc_binding_changed", lambda: service.create(
            "temporary", "codex", self.opened["record"]["id"], "fixture", "coordinator",
            idempotency_key="changed-branch", now=self.now))
        self.assertEqual(before, self.tree_bytes())

    def test_same_path_provider_slot_replacement_refuses_get_create_and_profile_replay(self):
        """Catches accepting identical path/content after its descriptor-bound inode changes."""
        profile = self.profile()
        self.replace_provider_slot_at_same_path(profile)
        service = self.profiles()
        before = self.tree_bytes()
        operations = (
            lambda: service.get(profile["id"]),
            lambda: self.profile(),
            lambda: service.create("temporary", "codex", self.opened["record"]["id"],
                "fixture", "coordinator", idempotency_key="replacement-profile", now=self.now),
        )
        for operation in operations:
            self.refuses("adhoc_binding_changed", operation)
            self.assertEqual(before, self.tree_bytes(), "replacement must never be adopted")


if __name__ == "__main__":
    unittest.main()

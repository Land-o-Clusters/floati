"""LANES-1 review B: declared source checkouts are not unmanaged lanes."""
import unittest

from tests import test_lane_workspaces as fixtures


class LaneInventoryPrimaryTests(unittest.TestCase):
    setUp = fixtures.LaneWorkspaceTests.setUp
    _git = fixtures.LaneWorkspaceTests._git
    _state = fixtures.LaneWorkspaceTests._state
    _service = fixtures.LaneWorkspaceTests._service

    def test_declared_primary_is_excluded_but_unrecorded_linked_checkout_is_reported(self):
        """Catches a clean fleet degrading solely because its source is listed by Git."""
        self.lanes_root.mkdir()
        service = self._service()
        clean = service.sweep()
        self.assertEqual([], clean["unmanaged"])
        self.assertEqual("ok", clean["status"])

        foreign = self.base / "manual-worktree"
        self._git(self.repository, "worktree", "add", "--detach", str(foreign), "HEAD")
        payload = foreign / "keep.txt"
        payload.write_bytes(b"operator-owned work\n")
        result = service.sweep(apply=True)
        self.assertEqual([str(foreign)], [row["path"] for row in result["unmanaged"]])
        self.assertEqual("degraded", result["status"])
        self.assertEqual([], result["closed"])
        self.assertEqual(b"operator-owned work\n", payload.read_bytes())

    def test_linked_declaration_still_excludes_only_the_primary_checkout(self):
        """Catches mistaking the declared linked checkout for Git's primary."""
        self.lanes_root.mkdir()
        linked = self.base / "declared-linked"
        self._git(self.repository, "worktree", "add", "--detach", str(linked), "HEAD")
        self._state("lane-repositories.json", {
            "schema_version": 0,
            "repositories": {"fixture": {"path": str(linked),
                                           "default_base": "refs/remotes/origin/main"}},
        })
        result = self._service().sweep()
        self.assertEqual([str(linked)], [row["path"] for row in result["unmanaged"]])

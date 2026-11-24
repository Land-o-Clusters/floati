"""Historical native-close recovery, independent of index-settlement policy.

The fixture operator explicitly synchronizes its own scratch row index just
before the real native close. This is an external test precondition, not an
automatic product repair or qualification of the still-open index policy.
"""
from datetime import timedelta
from pathlib import Path
import unittest
from unittest import mock

from floati.adhoc_threads import AdhocThreadController
from tests.test_adhoc_threads import ControllerFixture


class AdhocTeardownRecoveryTests(ControllerFixture, unittest.TestCase):
    def close_then_crash(self):
        c = self.case
        self.assertEqual("adhoc_result_unbanked", self.capture_run()[0])
        self.bank_result()
        self.assertIsNone(self.capture_run()[0])
        workspace = Path(c.profile_record["workspace"])
        lanes = self.controller.profiles.lanes
        real_close = lanes.close
        observations = []
        def operator_precondition_then_close(actor, row, *args, **kwargs):
            # This exact scratch index belongs to the fixture operator.
            # Product performs no read-tree/reset/index settlement.
            c.git(workspace, "read-tree", "HEAD")
            self.assertEqual("", c.git(workspace, "status", "--porcelain=v1", "--untracked-files=all"))
            observations.append("external_fixture_index_synchronized")
            return real_close(actor, row, *args, **kwargs)
        with mock.patch.object(lanes, "close", side_effect=operator_precondition_then_close) as close:
            with mock.patch.object(self.controller, "_record", side_effect=RuntimeError("after native close")):
                with self.assertRaisesRegex(RuntimeError, "after native close"):
                    self.controller.teardown(c.admitted["dispatch_id"], actor="node-a",
                        acting_session_id="fixture-parent", idempotency_key="teardown-one")
            self.assertEqual(1, close.call_count)
        self.assertEqual(["external_fixture_index_synchronized"], observations)
        self.assertFalse(workspace.exists())
        self.assertEqual([], self.saved("teardown_completed"))
        closing = [row for row in c.lanes._records("node-a") if row["state"] == "closed"
                   and row["opened_record_id"] == c.profile_record["lane_opening_id"]]
        self.assertEqual(1, len(closing))
        return closing[0]

    def fresh_replay(self):
        c = self.case
        fresh = AdhocThreadController(c.root, clock=lambda: c.now)
        with mock.patch.object(fresh.profiles.lanes, "close",
                side_effect=AssertionError("historical recovery must not delete a row again")) as close:
            result = fresh.teardown(c.admitted["dispatch_id"], actor="node-a",
                acting_session_id="fixture-parent", idempotency_key="teardown-one")
            self.assertEqual(0, close.call_count)
        return fresh, result

    def test_old_close_recovery_preserves_successor_at_the_same_path(self):
        c = self.case
        closing = self.close_then_crash()
        replacement = c.add_lease("node-a", expires=c.now + timedelta(hours=3))
        # The fixture operator archives the retained old branch before opening
        # a separately receipted successor at the same former row path.
        c.git(c.repository, "branch", "-m", c.profile_record["branch"], "fixture/retained-old-row")
        successor = c.lanes.open(actor="node-a", row="borrowed", repo="fixture")
        path = Path(successor["workspace"])
        (path / "successor-only.txt").write_text("retain successor bytes\n")
        successor_bytes = c.tree_bytes(path)
        common_bytes = c.tree_bytes(c.repository / ".git")
        registry = c.registry.known_records()
        fresh, result = self.fresh_replay()
        self.assertEqual(closing["id"], result["resource_dispositions"]["lane_closing_id"])
        self.assertEqual(c.profile_record["lane_opening_id"], result["resource_dispositions"]["lane_opening_id"])
        self.assertEqual("inactive", result["history_state"])
        self.assertTrue((c.root.tenant_home / c.profile_record["provider_state"]).is_dir())
        self.assertEqual(result, fresh.teardown(c.admitted["dispatch_id"], actor="node-a",
            acting_session_id="fixture-parent", idempotency_key="teardown-one"))
        self.assertEqual(1, len(self.saved("teardown_completed")))
        self.assertEqual(c.profile_record["node_lease_id"], result["resource_dispositions"]["node_lease_id"])
        self.assertEqual(replacement["id"], c.registry.node_lease_state("node-a", now=c.now)["node_lease_id"])
        self.assertEqual(successor_bytes, c.tree_bytes(path))
        self.assertEqual(common_bytes, c.tree_bytes(c.repository / ".git"))
        self.assertEqual(registry, c.registry.known_records())

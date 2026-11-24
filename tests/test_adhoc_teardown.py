"""Ad-hoc teardown retains owned history and refuses destructive ambiguity."""
from datetime import timedelta
from pathlib import Path
import unittest

from floati.errors import ProtocolRefusal
from tests.test_adhoc_threads import ControllerFixture


class AdhocTeardownTests(ControllerFixture, unittest.TestCase):
    def teardown_dispatch(self):
        return self.controller.teardown(self.case.admitted["dispatch_id"], actor="node-a",
            acting_session_id="fixture-parent", idempotency_key="teardown-one")

    def test_dirty_teardown_retains_row_and_successor(self):
        c = self.case
        workspace = Path(c.profile_record["workspace"])
        dirty = workspace / "unbanked.txt"
        dirty.write_text("retain owner bytes\n")
        replacement = c.add_lease("node-a", expires=c.now + timedelta(hours=3))
        with self.assertRaises(ProtocolRefusal) as caught:
            self.teardown_dispatch()
        self.assertEqual("adhoc_teardown_blocked", caught.exception.code)
        self.assertEqual("retain owner bytes\n", dirty.read_text())
        self.assertEqual(replacement["id"], c.registry.node_lease_state("node-a", now=c.now)["node_lease_id"])
        self.assertTrue(workspace.is_dir())

    def test_outstanding_work_blocks_teardown_without_deletion(self):
        workspace = Path(self.case.profile_record["workspace"])
        with self.assertRaises(ProtocolRefusal) as caught:
            self.teardown_dispatch()
        self.assertEqual("adhoc_teardown_blocked", caught.exception.code)
        self.assertTrue(workspace.is_dir())
        self.assertEqual("open", self.case.work.show(self.case.requested["id"])[0]["state"])

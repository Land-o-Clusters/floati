"""Native lifecycle producer seals and parent-result authority exclusion."""
from copy import deepcopy
from pathlib import Path
import select
import subprocess
import sys
import unittest
from unittest import mock

from tests.test_adhoc_threads import ControllerFixture
from tests.readiness import communicate, wait_readable


class AdhocControllerAuthorityTests(ControllerFixture, unittest.TestCase):
    def test_fixture_like_object_cannot_supply_process_owner_proof(self):
        with mock.patch.object(self.controller, "_prepare_execution",
                return_value=(self.execution, deepcopy(self.case.context["effect_binding"]))):
            code, _result = self.capture_run()
        self.assertEqual("adhoc_dispatch_authentication_failed", code)
        self.assertEqual([], self.saved("provider_terminal"))
        self.assertEqual([], self.saved("process_closed", stage="cleanup"))
        self.assertEqual(set(), set(self.acked()))
        self.assertEqual(0, self.execution.launches)

    def test_parent_result_cas_excludes_concurrent_authority_revocation(self):
        from floati.adhoc_workspace import BorrowedWorkspace
        from floati.planes import AuthorityGrantStore
        c = self.case
        children = []
        original = BorrowedWorkspace.finalize
        script = ("from datetime import datetime\n"
                  "from floati.root import FloatiRoot\n"
                  "from floati.planes import AuthorityGrantStore\n"
                  "from floati.errors import ProtocolRefusal\n"
                  "import sys\n"
                  "root=FloatiRoot.open_direct_home(sys.argv[1])\n"
                  "print('READY',flush=True)\n"
                  "try: AuthorityGrantStore(root).revoke_exact('borrowed-work','node-a',1,datetime.fromisoformat(sys.argv[2]))\n"
                  "except ProtocolRefusal as e:\n"
                  " if e.code!='epoch_lock_timeout': raise\n"
                  " print(e.code,flush=True)\n")
        def at_finalization(workspace, paths, expected, **kwargs):
            child = subprocess.Popen([str(Path(sys.executable).resolve()), "-B", "-c", script,
                str(c.root.path), c.now.isoformat()], stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True)
            children.append(child)
            wait_readable(child.stdout, "independent revoker did not reach its native operation")
            self.assertEqual("READY", child.stdout.readline().strip())
            try:
                _out, errors = child.communicate(timeout=0.5)
            except subprocess.TimeoutExpired:
                pass
            else:
                self.fail("authority revocation crossed parent result finalization: " + errors)
            return original(workspace, paths, expected, **kwargs)
        try:
            with mock.patch.object(BorrowedWorkspace, "finalize", at_finalization):
                code, _result = self.capture_run()
            self.assertIn(code, {"adhoc_result_unbanked", "adhoc_lease_ended"})
            for child in children:
                output, errors = communicate(child, "revoker exits after guard release")
                self.assertEqual(0, child.returncode, errors)
                if output.strip() == "epoch_lock_timeout":
                    # The bounded native writer may time out while the real
                    # Git prepare still owns the epoch. It did not revoke.
                    self.assertEqual("active", AuthorityGrantStore(c.root).exact_tail("borrowed-work")["state"])
                    AuthorityGrantStore(c.root).revoke_exact("borrowed-work", "node-a", 1, c.now)
            self.assertEqual("released", AuthorityGrantStore(c.root).exact_tail("borrowed-work")["state"])
            self.assertEqual(set(), set(self.acked()))
            self.assertNotEqual("completed", c.work.show(c.requested["id"])[0]["state"])
        finally:
            for child in children:
                if child.poll() is None:
                    child.terminate()
                    communicate(child, "collect terminated revoker fixture")

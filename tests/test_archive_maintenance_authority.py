"""Native fixture controls for contextual maintenance admission, never publication."""
import copy
import inspect
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

from floati import archive_maintenance_authority as authority
from floati import epoch_rotation as rotation
from floati.bus_epoch import (epoch_guard, lock_order_guard, LOCK_ORDER_WAKE,
                              LOCK_ORDER_LEDGER)
from floati.errors import ProtocolRefusal
from floati.events import EventLog
from floati.registry import Registry
from floati.root import FloatiRoot
from tests.readiness import wait_join
from tests.temp_roots import REAL_TEMP_ROOT


class ArchiveMaintenanceAuthorityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
        self.addCleanup(temporary.cleanup)
        self.root = FloatiRoot.open_direct_home(Path(temporary.name) / "home", create=True)
        Registry(self.root).register("actor", "Codex")
        Registry(self.root).register("recipient", "Codex")
        self.message = EventLog(self.root).send("actor", "recipient", "floati", "a" * 40,
            "AGENTS.md", "authority fixture", idempotency_key="fixture")

    def refused(self, root, admission):
        with self.assertRaises(ProtocolRefusal) as caught:
            authority.require_admission(root, admission)
        self.assertEqual("archive_maintenance_required", caught.exception.code)

    def during(self, observe, *, checker=None, native_send=False):
        visited = []
        def native_roll(root, *, actor, planner):
            frame = inspect.currentframe()
            try:
                while frame is not None and frame.f_code is not rotation._maintenance.__code__:
                    frame = frame.f_back
                self.assertIsNotNone(frame, "fixture must enter original native maintenance")
                admission = frame.f_locals["admission"]
            finally:
                del frame
            visited.append(admission)
            observe(admission)
            return {"receipt": None}
        with mock.patch("floati.bus_epoch.roll_automatic_epoch", side_effect=native_roll):
            if native_send:
                policy = self.root.path / "state/ledger-policy.json"
                policy.parent.mkdir(exist_ok=True)
                policy.write_text(json.dumps({"schema_version": 0, "max_bytes": 1, "max_age_days": 7}))
                result = EventLog(self.root).send("actor", "recipient", "floati", "b" * 40,
                    "AGENTS.md", "second fixture", idempotency_key="second")
                self.assertEqual("second", result["idempotency_key"])
            else:
                result = rotation._maintenance(self.root, actor="actor",
                    trigger="stop_poll" if checker else "send", authority_check=checker)
                self.assertEqual({"changed": False, "action": "not_due"}, result)
        self.assertEqual(1, len(visited))
        return visited[0]

    def test_real_registered_send_can_admit_inside_native_maintenance(self):
        self.during(lambda token: authority.require_admission(self.root, token), native_send=True)

    def test_ex_ownership_and_caller_values_cannot_admit(self):
        class PretendAdmission(authority._Admission):
            pass
        with epoch_guard(self.root, exclusive=True):
            for value in ({"actor": "actor", "trigger": "send"}, authority._Admission(),
                          PretendAdmission(), None):
                with self.subTest(kind=type(value).__name__):
                    self.refused(self.root, value)

    def test_direct_private_context_cannot_issue(self):
        with self.assertRaises(ProtocolRefusal) as caught:
            with authority._native_context(self.root, actor="actor", trigger="send"):
                self.fail("caller-created maintenance context entered")
        self.assertEqual("archive_maintenance_required", caught.exception.code)

    def test_copies_of_live_admission_cannot_admit(self):
        def observe(token):
            for duplicate in (copy.copy(token), copy.deepcopy(token)):
                self.assertIsNot(token, duplicate)
                self.refused(self.root, duplicate)
        self.during(observe)

    def test_cross_root_and_equivalent_reopened_root_cannot_admit(self):
        other = FloatiRoot.open_direct_home(self.root.path.parent / "other", create=True)
        reopened = FloatiRoot.open_direct_home(self.root.path, create=False)
        def observe(token):
            with epoch_guard(other, exclusive=True):
                self.refused(other, token)
            self.refused(reopened, token)
        self.during(observe)

    def test_cross_thread_cannot_admit(self):
        def observe(token):
            errors = []
            def inspect_token():
                try:
                    authority.require_admission(self.root, token)
                except Exception as exc:
                    errors.append(exc)
            worker = threading.Thread(target=inspect_token)
            worker.start()
            wait_join(worker, "foreign admission check finishes")
            self.assertFalse(worker.is_alive())
            self.assertEqual(1, len(errors))
            self.assertIsInstance(errors[0], ProtocolRefusal)
            self.assertEqual("archive_maintenance_required", errors[0].code)
        self.during(observe)

    def test_expired_context_cannot_be_revived_with_ex(self):
        token = self.during(lambda token: authority.require_admission(self.root, token))
        with epoch_guard(self.root, exclusive=True):
            self.refused(self.root, token)

    def test_prior_token_cannot_enter_a_later_native_context(self):
        prior = self.during(lambda token: authority.require_admission(self.root, token))
        self.during(lambda token: self.refused(self.root, prior))

    def test_retired_actor_invalidates_live_admission(self):
        def observe(token):
            Registry(self.root).retire("actor")
            with self.assertRaises(ProtocolRefusal) as caught:
                authority.require_admission(self.root, token)
            self.assertEqual("unknown_node", caught.exception.code)
        self.during(observe)

    def test_stop_checker_is_rechecked_at_each_mutation_boundary(self):
        checks = []
        revoked = []
        def checker():
            checks.append(True)
            if revoked:
                raise ProtocolRefusal("fixture_consent_revoked", "fixture authority revoked")
        def observe(token):
            before = len(checks)
            authority.require_admission(self.root, token)
            self.assertEqual(before + 1, len(checks))
            revoked.append(True)
            with self.assertRaises(ProtocolRefusal) as caught:
                authority.require_admission(self.root, token)
            self.assertEqual("fixture_consent_revoked", caught.exception.code)
        self.during(observe, checker=checker)

    def test_recovery_precedes_admission_and_existing_recovered_return(self):
        with mock.patch.object(rotation, "_recover", return_value={"recovered": True}) as recovery,\
                mock.patch("floati.bus_epoch.roll_automatic_epoch") as roll:
            result = rotation.maintenance(self.root, actor="actor", trigger="send")
        recovery.assert_called_once_with(self.root, "actor", None)
        roll.assert_not_called()
        self.assertEqual({"changed": True, "action": "recovered"}, result)

    def test_native_nested_scopes_preserve_noop_without_admission(self):
        for scope in (lambda: epoch_guard(self.root),
                      lambda: epoch_guard(self.root, exclusive=True),
                      lambda: lock_order_guard(self.root, LOCK_ORDER_WAKE, label="fixture wake"),
                      lambda: lock_order_guard(self.root, LOCK_ORDER_LEDGER, label="fixture ledger")):
            with scope(), mock.patch("floati.bus_epoch.roll_automatic_epoch") as roll:
                self.assertEqual({"changed": False, "action": "nested"},
                    rotation.maintenance(self.root, actor="actor", trigger="send"))
                roll.assert_not_called()

    def test_unknown_and_inactive_actors_preserve_existing_outcomes(self):
        with mock.patch("floati.bus_epoch.roll_automatic_epoch") as roll:
            self.assertEqual({"changed": False, "action": "inactive"},
                rotation.maintenance(self.root, actor="missing", trigger="send"))
            with self.assertRaises(ValueError):
                rotation.maintenance(self.root, actor="actor", trigger="invented")
            roll.assert_not_called()

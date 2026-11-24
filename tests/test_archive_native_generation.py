"""Maintenance metadata remains bound to its genuine live frame."""
import inspect
from copy import copy
import unittest
from unittest import mock

from floati import archive_maintenance_authority as authority
from floati import bus_epoch, epoch_rotation
from floati.errors import ProtocolRefusal
from tests import test_epoch_carry as fixture

class ArchiveNativeGenerationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixture.EpochCarryTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.root

    def during(self, operation, checker=None):
        results = []
        def callback(root, *, actor, planner):
            frame = inspect.currentframe()
            try:
                while frame is not None and frame.f_code is not epoch_rotation._maintenance.__code__:
                    frame = frame.f_back
                self.assertIsNotNone(frame)
                results.append(operation(frame.f_locals["admission"]))
            finally:
                del frame
            return {"receipt": None}
        with mock.patch.object(bus_epoch, "roll_automatic_epoch", side_effect=callback):
            epoch_rotation._maintenance(self.root, actor="sender",
                trigger="stop_poll" if checker else "send", authority_check=checker)
        self.assertEqual(1, len(results))
        return results[0]

    def test_metadata_requires_exact_live_context_and_confers_no_authority(self):
        retained = []
        def operation(token):
            self.assertEqual(("sender", "send"), authority._admission_identity(self.root, token))
            retained.append(token)
            with self.assertRaises(ProtocolRefusal):
                authority._admission_identity(self.root, copy(token))
        self.during(operation)
        for token in (retained[0], {}, authority._Admission()):
            with self.subTest(value=type(token).__name__), self.assertRaises(ProtocolRefusal):
                authority._admission_identity(self.root, token)

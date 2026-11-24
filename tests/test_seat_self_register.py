"""Help must disclose the observed caller convention of the low-level verbs."""
from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest

from floati.cli import main
from floati.registry import Registry
from floati.root import FloatiRoot
from floati.seat_declaration import (
    COORDINATOR_AUTHORITIES, OWNER_TIERS, FleetGovernance, SeatDeclaration,
)
from tests.temp_roots import REAL_TEMP_ROOT


class SeatSelfRegistrationConventionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.root = FloatiRoot.open_direct_home(self.base / "fleet", create=True)
        self.registry = Registry(self.root)
        self.registry.register("seat-a", "Codex")
        governance = FleetGovernance.create(self.root, topology="star", coordinator="seat-a",
            coordinator_authority=COORDINATOR_AUTHORITIES, owner_tier=OWNER_TIERS)
        self.workspace = self.base / "seat-a-workspace"
        self.workspace.mkdir()
        SeatDeclaration.publish_declared_workspace(self.workspace, "seat-a", self.root, governance)

    def cli_from_declared_seat(self, *arguments):
        output, errors = io.StringIO(), io.StringIO()
        previous = Path.cwd()
        try:
            os.chdir(self.workspace)
            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
                try:
                    code = main(list(arguments))
                except SystemExit as exc:
                    code = exc.code
        finally:
            os.chdir(previous)
        return code, output.getvalue(), errors.getvalue()

    def assert_help_discloses_convention(self, verb):
        code, output, errors = self.cli_from_declared_seat(verb, "--help")
        self.assertEqual(0, code, errors or output)
        text = " ".join(output.lower().split())
        self.assertIn("convention", text,
            "successful foreign NODE operation must not be described as self-only enforcement")
        self.assertIn("not bound to the calling seat", text)

    def test_foreign_registration_succeeds_and_help_discloses_caller_convention(self):
        code, output, errors = self.cli_from_declared_seat("register", "--root", str(self.root.path),
            "seat-b", "--harness", "Codex")
        self.assertEqual(0, code, errors or output)
        self.assertEqual("seat-b", json.loads(output)["evidence"]["node_id"])
        self.assertEqual("active", self.registry._latest("seat-b")["state"])
        self.assertEqual("seat-a", SeatDeclaration.load(self.workspace).node_id)
        self.assert_help_discloses_convention("register")

    def test_foreign_retirement_succeeds_and_help_discloses_caller_convention(self):
        self.registry.register("seat-b", "Codex")
        code, output, errors = self.cli_from_declared_seat("retire", "--root", str(self.root.path), "seat-b")
        self.assertEqual(0, code, errors or output)
        self.assertEqual("seat-b", json.loads(output)["evidence"]["node_id"])
        self.assertEqual("retired", self.registry._latest("seat-b")["state"])
        self.assertEqual("active", self.registry._latest("seat-a")["state"])
        self.assertEqual("seat-a", SeatDeclaration.load(self.workspace).node_id)
        self.assert_help_discloses_convention("retire")

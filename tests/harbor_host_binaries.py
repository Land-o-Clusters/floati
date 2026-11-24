"""Explicit probes for the executables named in the host declarations."""

import subprocess
import unittest

from tests import harness_declaration, managed_test_tools
from tests.test_ad1_antigravity_adapter import AGY_CASK


class LiveExecutableTests(unittest.TestCase):
    def test_named_t3_binary_reports_version(self) -> None:
        executable = harness_declaration.live_executable_or_typed_absence(
            self, "t3"
        )
        if executable is None:
            return
        node = managed_test_tools.executable(
            "FLOATI_TEST_NODE_EXECUTABLE", "node"
        )
        self.assertIsNotNone(node, "AD-1 t3 requires a real Node executable")
        completed = subprocess.run(
            [str(node), str(executable), "--version"],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("t3", completed.stdout.lower())
        self.assertIn("0.0.35", completed.stdout)

    def test_named_herdr_binary_reports_version(self) -> None:
        executable = harness_declaration.live_executable_or_typed_absence(
            self, "herdr"
        )
        if executable is None:
            return
        completed = subprocess.run(
            [str(executable), "--version"],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("herdr", completed.stdout)


class DevinWorkAdapterTests(unittest.TestCase):
    def test_named_devin_binary_reports_version(self) -> None:
        executable = harness_declaration.live_executable_or_typed_absence(
            self, "devin"
        )
        if executable is None:
            return
        completed = subprocess.run(
            [str(executable), "--version"],
            check=False,
            capture_output=True,
            text=True,
            timeout=8,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("devin", completed.stdout.lower())


class AntigravityWorkAdapterTests(unittest.TestCase):
    def test_named_agy_binary_is_present(self) -> None:
        """Filesystem fact only. Do not launch the live binary here.

        The path is operator-declared; where it is not declared or not present
        the typed absence is asserted instead. THE DISCRIMINATION SURVIVES
        EITHER WAY: whatever is declared must not be the Homebrew cask, which
        is a different and older build (1.1.5, against the bound 1.1.22).
        """
        executable = harness_declaration.live_executable_or_typed_absence(
            self, "agy"
        )
        if executable is None:
            return
        self.assertNotEqual(executable, AGY_CASK)
        self.assertTrue(executable.is_file(), f"missing {executable}")
        self.assertGreater(executable.stat().st_size, 0)


class ZcodeAdapterDeclarationTests(unittest.TestCase):
    def test_pinned_binaries_are_present_files(self) -> None:
        """Filesystem facts only — no argument spelling is invented here
        (even --version is unexercised on this harness, so it is not run).

        THIS ROW PINS TWO ARTIFACTS, not one: the node binary and the entry
        script. Both are operator-declared, and either one being absent or
        undeclared is a typed absence — a declaration covering only the binary
        would leave the .app path hard-coded and this suite still host-bound.
        """
        node = harness_declaration.live_executable_or_typed_absence(
            self, "zcode-node"
        )
        entry = harness_declaration.live_executable_or_typed_absence(
            self, "zcode-entry"
        )
        if node is None or entry is None:
            return
        self.assertTrue(node.is_file(), f"missing {node}")
        self.assertTrue(entry.is_file(), f"missing {entry}")

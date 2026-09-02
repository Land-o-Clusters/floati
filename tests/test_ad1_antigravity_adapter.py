"""AD-1 antigravity C-row — HeadlessProfileAdapter work column.

Bound executable: user-local ~/.local/bin/agy (1.1.22 via Path.home()).
Does not bind Homebrew cask /opt/homebrew/bin/agy (1.1.5).
Live --version belongs in a sha-pinned receipt, not this suite.
"""

from __future__ import annotations

import importlib
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from floati.adapters.headless_template import HarnessProfile, HeadlessProfileAdapter
from floati.workers import WorkerAdapterFailure
from tests import harness_declaration
from tests.test_roster_adapters import ROSTER
from tests.operator_identity import assert_source_names_no_operator
from tests.temp_roots import REAL_TEMP_ROOT


AGY_CASK = Path("/opt/homebrew/bin/agy")


class AntigravityWorkAdapterTests(unittest.TestCase):
    def test_module_imports_and_is_headless_profile(self) -> None:
        try:
            module = importlib.import_module("floati.adapters.antigravity")
        except ImportError as exc:
            self.fail(
                "AD-1 antigravity RED: floati.adapters.antigravity is absent. "
                f"Work-column dash is still honest. {exc}"
            )
        self.assertTrue(hasattr(module, "AntigravityAdapter"))
        self.assertTrue(issubclass(module.AntigravityAdapter, HeadlessProfileAdapter))
        self.assertEqual(module.AntigravityAdapter.name, "antigravity")
        self.assertTrue(module.AntigravityAdapter.surface_verified)
        profile = module.AntigravityAdapter._default_profile()
        self.assertEqual((), profile.headless_arguments)
        bound = Path(profile.command[0])
        self.assertEqual(bound, Path.home() / ".local" / "bin" / "agy")
        self.assertNotEqual(bound, AGY_CASK)

    def test_shipped_source_has_no_owner_home_literal(self) -> None:
        assert_source_names_no_operator(self, "floati/adapters/antigravity.py")

    def test_antigravity_is_on_f3_roster(self) -> None:
        names = [name for name, _cls in ROSTER]
        self.assertIn("antigravity", names)

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

    def test_declaration_file_names_no_operator(self) -> None:
        """The declaration is the one file most likely to leak the account."""
        assert_source_names_no_operator(self, "tests/harness_declarations.json")
        assert_source_names_no_operator(self, "tests/harness_declaration.py")

    def test_adapter_deadline_exceeded_uses_fixture_slow_binary(self) -> None:
        """Suite pins the adapter's bounded timeout, not a live agy quirk."""
        with tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT) as temporary:
            root = Path(temporary)
            script = root / "slow-agy"
            script.write_text(
                "#!/usr/bin/env python3\n"
                "import sys, time\n"
                "while True:\n"
                "    sys.stdout.buffer.write(b'x')\n"
                "    sys.stdout.buffer.flush()\n"
                "    time.sleep(0.05)\n",
                encoding="utf-8",
            )
            script.chmod(0o755)
            parent = root / "floati-work"
            profile = HarnessProfile(
                name="antigravity",
                command=(str(script),),
                headless_arguments=(),
                stderr_name="antigravity.stderr",
            )
            from floati.adapters.antigravity import AntigravityAdapter

            with mock.patch(
                "floati.adapters.headless_template._WORKSPACE_PARENT", parent
            ), mock.patch(
                "floati.adapters.codex_live._WORKSPACE_PARENT", parent
            ):
                adapter = AntigravityAdapter(profile)
                item = {
                    "id": "w-slow-agy",
                    "workspace": str(parent / "w-slow-agy"),
                    "title": "slow",
                }
                handle = adapter.spawn(item, deadline_seconds=5)
                try:
                    with self.assertRaises(WorkerAdapterFailure) as caught:
                        adapter.drive(handle, deadline_seconds=0.2)
                    self.assertEqual("deadline_exceeded", caught.exception.code)
                finally:
                    adapter.cancel()


if __name__ == "__main__":
    unittest.main()

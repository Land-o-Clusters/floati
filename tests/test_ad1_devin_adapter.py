"""AD-1 devin C-row — HeadlessProfileAdapter work column.

The live executable for this row is OPERATOR-DECLARED in
``tests/harness_declarations.json`` and is never searched for. Where it is not
declared, or the declared path is not one canonical executable on this host,
the live test still runs and asserts the typed absence instead. It never skips.
"""

from __future__ import annotations

import importlib
import subprocess
import unittest

from floati.adapters.headless_template import HeadlessProfileAdapter
from tests import harness_declaration
from tests.test_roster_adapters import ROSTER


class DevinWorkAdapterTests(unittest.TestCase):
    def test_module_imports_and_is_headless_profile(self) -> None:
        try:
            module = importlib.import_module("floati.adapters.devin")
        except ImportError as exc:
            self.fail(
                "AD-1 devin RED: floati.adapters.devin is absent. "
                f"Work-column dash is still honest. {exc}"
            )
        self.assertTrue(hasattr(module, "DevinAdapter"))
        self.assertTrue(issubclass(module.DevinAdapter, HeadlessProfileAdapter))
        self.assertEqual(module.DevinAdapter.name, "devin")
        self.assertFalse(module.DevinAdapter.surface_verified)
        profile = module.DevinAdapter._default_profile()
        self.assertEqual((), profile.headless_arguments)
        self.assertEqual(("/opt/homebrew/bin/devin",), tuple(profile.command))

    def test_devin_is_on_f3_roster(self) -> None:
        names = [name for name, _cls in ROSTER]
        self.assertIn("devin", names)

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


if __name__ == "__main__":
    unittest.main()

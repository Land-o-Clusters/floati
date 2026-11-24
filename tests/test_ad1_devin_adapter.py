"""AD-1 devin C-row — HeadlessProfileAdapter work column.
"""

from __future__ import annotations

import importlib
import unittest

from floati.adapters.headless_template import HeadlessProfileAdapter
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


if __name__ == "__main__":
    unittest.main()

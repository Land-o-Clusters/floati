"""AD-1 t3 C-row — loopback CLIENT, not HeadlessProfileAdapter.
"""

from __future__ import annotations

import importlib
import unittest

from tests.test_roster_adapters import ROSTER


class ObservationClientFenceTests(unittest.TestCase):
    def test_client_has_no_worker_spawn_or_drive(self) -> None:
        try:
            adapter = importlib.import_module("floati.adapters.t3").T3ClientAdapter
        except ImportError as exc:
            self.fail(
                "AD-1 t3 RED: floati.adapters.t3 is absent. "
                f"Work-column dash is still honest. {exc}"
            )
        self.assertFalse(hasattr(adapter, "spawn"))
        self.assertFalse(hasattr(adapter, "drive"))
        self.assertEqual(adapter.name, "t3-client")
        self.assertNotEqual(adapter.name, "t3")

    def test_client_is_not_on_f3_headless_roster(self) -> None:
        names = [name for name, _cls in ROSTER]
        self.assertNotIn("t3", names)
        self.assertNotIn("t3-client", names)

    def test_client_is_not_headless_profile_adapter(self) -> None:
        try:
            module = importlib.import_module("floati.adapters.t3")
        except ImportError as exc:
            self.fail(f"AD-1 t3 RED: T3ClientAdapter missing ({exc})")
        from floati.adapters.headless_template import HeadlessProfileAdapter

        self.assertFalse(issubclass(module.T3ClientAdapter, HeadlessProfileAdapter))


if __name__ == "__main__":
    unittest.main()

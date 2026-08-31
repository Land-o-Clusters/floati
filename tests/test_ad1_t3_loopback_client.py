"""AD-1 t3 C-row — loopback CLIENT, not HeadlessProfileAdapter.

Live executable named for this row: /opt/homebrew/bin/t3
"""

from __future__ import annotations

import importlib
import subprocess
import unittest
from pathlib import Path

from tests.test_roster_adapters import ROSTER


T3_EXECUTABLE = Path("/opt/homebrew/bin/t3")


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


class LiveExecutableTests(unittest.TestCase):
    """C-row: live executable named and launched."""

    def test_named_t3_binary_reports_version(self) -> None:
        self.assertTrue(T3_EXECUTABLE.is_file(), f"missing {T3_EXECUTABLE}")
        completed = subprocess.run(
            [str(T3_EXECUTABLE), "--version"],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("t3", completed.stdout.lower())
        self.assertIn("0.0.35", completed.stdout)


if __name__ == "__main__":
    unittest.main()

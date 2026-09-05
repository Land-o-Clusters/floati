"""GROK-BIN-1: the grok-build adapter defaults to the live vendor binary."""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from floati.adapters import grok_build
from floati.adapters.grok_build import GrokBuildAdapter
from tests.temp_roots import REAL_TEMP_ROOT


class GrokBin1DefaultBinaryTests(unittest.TestCase):
    def test_probe_without_override_names_the_real_binary(self) -> None:
        probe = GrokBuildAdapter.availability()
        self.assertEqual("grok", Path(probe["binary"]).name)
        self.assertNotEqual("/opt/homebrew/bin/grok-build", probe["binary"])

    def test_default_is_a_fixed_candidate_list_naming_grok(self) -> None:
        candidates = grok_build._GROK_CANDIDATES
        self.assertIsInstance(candidates, tuple)
        self.assertGreaterEqual(len(candidates), 2)
        for candidate in candidates:
            self.assertTrue(Path(candidate).is_absolute(), candidate)
            self.assertEqual("grok", Path(candidate).name)
        self.assertNotIn("/opt/homebrew/bin/grok-build", candidates)
        command = GrokBuildAdapter._default_profile().command
        self.assertEqual(1, len(command))
        self.assertIn(command[0], candidates)

    def test_unavailable_candidates_ignore_a_path_decoy(self) -> None:
        decoy_dir = Path(
            tempfile.mkdtemp(prefix="grok-bin-decoy-", dir=REAL_TEMP_ROOT)
        )
        self.addCleanup(lambda: shutil.rmtree(decoy_dir, ignore_errors=True))
        decoy = decoy_dir / "grok"
        decoy.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        decoy.chmod(0o755)
        missing = (
            "/missing/opt/homebrew/bin/grok",
            "/missing/usr/local/bin/grok",
        )
        with mock.patch.object(grok_build, "_GROK_CANDIDATES", missing):
            with mock.patch.dict(os.environ, {"PATH": str(decoy_dir)}):
                probe = GrokBuildAdapter.availability()
        self.assertEqual(missing[0], probe["binary"])
        self.assertFalse(probe["present"])
        self.assertNotEqual(str(decoy), probe["binary"])

    def test_operator_command_override_is_kept(self) -> None:
        override = ("/explicit/declared/grok",)
        probe = GrokBuildAdapter.availability(command=override)
        self.assertEqual(override[0], probe["binary"])
        self.assertFalse(probe["present"])
        adapter = GrokBuildAdapter(override)
        self.assertEqual(override, tuple(adapter.profile.command))


if __name__ == "__main__":
    unittest.main()

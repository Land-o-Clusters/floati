from __future__ import annotations

import unittest
from pathlib import Path

from floati.adapters.cline import ClineAdapter
from floati.adapters.cursor import CursorAgentAdapter
from floati.adapters.grok_build import GrokBuildAdapter
from floati.adapters.opencode import OpenCodeAdapter
from floati.adapters.pi_observation import PiObservationAdapter


class DocumentedHeadlessInvocationTests(unittest.TestCase):
    def test_documented_roster_profiles_are_exactly_declared(self) -> None:
        expected = {
            "opencode": (
                ("run",),
                "https://opencode.ai/docs/cli/",
            ),
            "cline": (
                ("--json",),
                "https://docs.cline.bot/usage/cli-overview",
            ),
            "grok-build": (
                ("-p",),
                "https://docs.x.ai/build/cli/headless-scripting",
            ),
        }
        roster = {
            "opencode": OpenCodeAdapter,
            "cline": ClineAdapter,
            "grok-build": GrokBuildAdapter,
        }

        for name, adapter_cls in roster.items():
            with self.subTest(adapter=name):
                profile = adapter_cls._default_profile()
                self.assertEqual(expected[name][0], profile.headless_arguments)
                self.assertEqual(expected[name][1], profile.cited_source)

    def test_unresearched_roster_profiles_remain_explicitly_empty(self) -> None:
        for name, adapter_cls in (
            ("cursor", CursorAgentAdapter),
            ("pi-observation", PiObservationAdapter),
        ):
            with self.subTest(adapter=name):
                profile = adapter_cls._default_profile()
                self.assertEqual((), profile.headless_arguments)
                self.assertIsNone(profile.cited_source)

    def test_grok_build_default_binary_is_the_vendor_grok(self) -> None:
        command = GrokBuildAdapter._default_profile().command
        self.assertEqual("grok", Path(command[0]).name)
        self.assertNotEqual("/opt/homebrew/bin/grok-build", command[0])


if __name__ == "__main__":
    unittest.main()

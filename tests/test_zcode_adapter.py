"""K1 zcode C-row — the thin roster adapter over the MEASURED surface.

Every pin here cites the gauntlet receipt, never --help: finding ZC1-F1
measured 5 of 19 advertised options refusing at the parser, so on this
harness the help page is CLAIMED at best. The argv below was exercised
live (WAKE-PROBE-OK, rc=0, glm-4.7-flash) — the strongest citation class
in the roster, and the reason surface_verified ships True.
"""

from __future__ import annotations

import importlib
import unittest
from pathlib import Path

from floati.adapters.headless_template import HeadlessProfileAdapter
from tests import harness_declaration
from tests.test_roster_adapters import ROSTER


NODE_BINARY = Path("/opt/homebrew/bin/node")
ENTRY_SCRIPT = Path(
    "/Applications/ZCode.app/Contents/Resources/glm/zcode.cjs")
MEASURED_COMMAND = (str(NODE_BINARY), str(ENTRY_SCRIPT))
MEASURED_ARGV = ("--json", "--no-color")
MEASURED_CITATION = (
    "docs/evidence/gauntlet/ZC1-zcode-scoping-photograph-am2.md")

# ZC1-F1 (Am.1): ADVERTISED by --help, REFUSED by the parser — never
# declare any of these. The count is pinned beside the enumeration
# because this set cannot be derived from code; it is a measurement.
REFUSED_HELP_FLAGS = (
    "--allowed-tools",
    "--max-turns",
    "--settings",
    "--permission-mode",
    "--allow-main-worktree-yolo",
)


class ZcodeAdapterDeclarationTests(unittest.TestCase):
    def test_module_imports_and_is_headless_profile(self) -> None:
        module = importlib.import_module("floati.adapters.zcode")
        self.assertTrue(hasattr(module, "ZcodeAdapter"))
        self.assertTrue(
            issubclass(module.ZcodeAdapter, HeadlessProfileAdapter))
        self.assertEqual(module.ZcodeAdapter.name, "zcode")

    def test_surface_verified_is_true_from_the_live_exercise(self) -> None:
        """K1's headline: ours was exercised (WAKE-PROBE-OK, rc=0), so the
        CHECK-TWO flag flips — unlike the doc-cited roster members."""
        module = importlib.import_module("floati.adapters.zcode")
        self.assertIs(module.ZcodeAdapter.surface_verified, True)

    def test_profile_carries_the_measured_command(self) -> None:
        module = importlib.import_module("floati.adapters.zcode")
        profile = module.ZcodeAdapter._default_profile()
        self.assertEqual(tuple(profile.command), MEASURED_COMMAND)
        self.assertEqual(
            len(profile.command), 2,
            "command is node + entry script, nothing else")

    def test_profile_carries_the_measured_argv(self) -> None:
        module = importlib.import_module("floati.adapters.zcode")
        profile = module.ZcodeAdapter._default_profile()
        self.assertEqual(tuple(profile.headless_arguments), MEASURED_ARGV)
        self.assertEqual(len(profile.headless_arguments), 2)

    def test_profile_cites_the_measured_receipt(self) -> None:
        module = importlib.import_module("floati.adapters.zcode")
        profile = module.ZcodeAdapter._default_profile()
        self.assertEqual(profile.cited_source, MEASURED_CITATION)

    def test_no_declared_argument_is_a_refused_spelling(self) -> None:
        """ZC1-F1 guard: the help ADVERTISES five flags the parser refuses.
        If a future edit declares one, this goes red before a seat does."""
        self.assertEqual(
            len(REFUSED_HELP_FLAGS), 5,
            "the refused set is a measurement (5 of 19); if it changed, "
            "re-run the parse sweep and re-cite the receipt")
        module = importlib.import_module("floati.adapters.zcode")
        profile = module.ZcodeAdapter._default_profile()
        declared = set(profile.headless_arguments)
        self.assertEqual(declared & set(REFUSED_HELP_FLAGS), set())

    def test_profile_declares_the_measured_prompt_form(self) -> None:
        """K4 live finding: the template's `-- <title>` idiom is REFUSED by
        zcode (`Unknown command: …` + help dump, capture
        attempt1-stderr-helpdump.txt). zcode's measured prompt spelling is
        `--prompt <text>` (am1 parse sweep + am2 WAKE-PROBE-OK turn). The
        profile therefore carries prompt_form=("--prompt",) — the only
        zcode-specific wiring in the module."""
        module = importlib.import_module("floati.adapters.zcode")
        profile = module.ZcodeAdapter._default_profile()
        self.assertEqual(tuple(profile.prompt_form), ("--prompt",))

    def test_zcode_is_on_f3_roster(self) -> None:
        names = [name for name, _cls in ROSTER]
        self.assertIn("zcode", names)

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


if __name__ == "__main__":
    unittest.main()

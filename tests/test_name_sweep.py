from __future__ import annotations

from floati import fixture_ids as public_ids

import os
import re
import subprocess
import unittest
from pathlib import Path

from floati.identity_fence import HOME_PREFIX, OWNER_USERNAME
from tests.private_artifacts import require_private_artifact


PRIVATE_FLEET = bytes.fromhex("707564646c652d666c656574").decode("ascii")


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]

APPROVED_README_TOP = """<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/assets/floati-icon.svg#gh-dark-mode-only">
    <source media="(prefers-color-scheme: light)" srcset="docs/assets/floati-icon.svg#gh-light-mode-only">
    <img src="docs/assets/floati-icon.svg" alt="THE BUOY" width="180">
  </picture>
</p>

<h1 align="center">Floati</h1>
<p align="center"><strong>The fleet operating system for local coding agents.</strong></p>
<p align="center">Any harness, any mix — one bus, one board, one set of receipts.</p>

"""

APPROVED_HERO_LOOP = """<p align="center">
  <img src="docs/demo/hero-three-fault-replay.gif" alt="A three-fault replay" width="1400">
</p>"""

PUBLIC_LIVING_DOCS = (
    "README.md",
    "CONTRIBUTING.md",
    "docs/CONFLUENCE-v0.md",
    "docs/CONFORMANCE.md",
    "docs/COPY-LEDGER.md",
    "docs/DESIGN.md",
    "docs/FINDINGS.md",
    "docs/FLEET.md",
    "docs/SPEC-DRAFT.md",
    "bundle/c7.1/README.md",
    "bundle/c7.2/README.md",
)

PRIVATE_LIVING_DOCS = ("docs/PUBLICATION-CHECKLIST.md",)

PUBLIC_ACCOUNT_NEUTRAL_FILES = (
    "docs/evidence/DEMO-UAT-CAPTURE-GIF-CANDIDATES.md",
)

PRIVATE_ACCOUNT_NEUTRAL_FILES = ("docs/demo/corpus.v0.jsonl",)


# The forbidden operator coordinates are DERIVED and ANCHORED, never taken as
# a bare account name from the ambient environment.
#
# This sweep used to build one pattern out of `$USER` and search for it as a
# plain substring. On any CI host that account is called `runner`, which is
# also an ordinary English word, and the sweep duly reported docs/DESIGN.md as
# leaking an operator identity — on BOTH runners, in prose that names a test
# runner. The instrument was reporting the English language.
#
# ⇒ A BARE ACCOUNT NAME IS A SUBSTRING TEST, NOT AN IDENTITY TEST.
#
# So the same coordinates tests/operator_identity.py uses are used here, for
# the same reasons stated in that module's docstring:
#
# * `floati.identity_fence` — the module that owns this vocabulary and builds
#   every governed token from hex, so neither this file nor the exporter's
#   literal scanner ever spells one. HOME_PREFIX anchors the home-directory
#   form; OWNER_USERNAME names the account this repository is written on and
#   keeps naming it in the public checkout, where a literal would have been
#   rewritten into the project's public handle.
# * `Path.home()` — the account RUNNING the sweep, so a contributor who pastes
#   their own home path is caught too. Its FULL path is used and never its bare
#   basename: that is precisely the form that reported `runner` as a defect.
PRIVATE_ACCOUNT_PATTERNS = (
    re.compile(re.escape(HOME_PREFIX) + r"[^/\s`<>]+", re.IGNORECASE),
    re.compile(re.escape(str(Path.home())), re.IGNORECASE),
    re.compile(re.escape(OWNER_USERNAME), re.IGNORECASE),
)

TENANT_PATTERNS = (
    *PRIVATE_ACCOUNT_PATTERNS,
    re.compile(r"CMs-M5", re.IGNORECASE),
    re.compile(rf"\.slipway-bus/{re.escape(PRIVATE_FLEET)}", re.IGNORECASE),
    re.compile(re.escape(PRIVATE_FLEET), re.IGNORECASE),
    re.compile(r"~/Projects/puddle", re.IGNORECASE),
    re.compile(r"/absolute/slipway", re.IGNORECASE),
)

RETIRED_PRODUCT_NAME = re.compile(
    r"(?<![\w./-])(?:slipway|slip)(?!\w)",
    re.IGNORECASE,
)


class NameSweepLauncherTests(unittest.TestCase):
    def test_floati_is_canonical_regular_executable(self) -> None:
        """Catches a missing, symlinked, or non-executable public launcher."""
        launcher = REPOSITORY_ROOT / "scripts" / "floati"

        self.assertTrue(launcher.is_file())
        self.assertFalse(launcher.is_symlink())
        self.assertTrue(launcher.stat().st_mode & 0o111)

    def test_transition_alias_is_absent(self) -> None:
        """Catches a retained compatibility launcher or symlink."""
        launcher = REPOSITORY_ROOT / "scripts" / "slip"
        self.assertFalse(os.path.lexists(launcher))

    def test_public_help_uses_only_floati_command_token(self) -> None:
        """Catches any public command example that still teaches the retired verb."""
        launcher = REPOSITORY_ROOT / "scripts" / "floati"
        self.assertTrue(launcher.is_file())

        completed = subprocess.run(
            [str(launcher), "--help"],
            cwd=REPOSITORY_ROOT,
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(0, completed.returncode)
        self.assertIn("floati COMMAND [OPTIONS]", completed.stdout)
        self.assertNotRegex(completed.stdout, r"(?<![./])\bslip\b")
        self.assertNotIn("Slipway", completed.stdout)


class NameSweepLivingDocumentationTests(unittest.TestCase):
    def test_retired_product_name_detector_distinguishes_prose_from_ruled_coordinates(self) -> None:
        """Catches punctuation-bound retired prose without rejecting ruled coordinates."""

        for text in ("Slipway", "slip,", "SLIPWAY!"):
            with self.subTest(text=text):
                self.assertIsNotNone(RETIRED_PRODUCT_NAME.search(text))

        for text in ("https://slipway.dev", ".slipway", "x-slipway-*"):
            with self.subTest(text=text):
                self.assertIsNone(RETIRED_PRODUCT_NAME.search(text))

    def test_readme_begins_with_exact_architect_copy(self) -> None:
        'Catches invented README voice on the ruled reviewer-owned surface.'
        readme = (REPOSITORY_ROOT / "README.md").read_text(encoding="utf-8")

        self.assertTrue(readme.startswith(APPROVED_README_TOP))
        self.assertEqual(1, readme.count(APPROVED_HERO_LOOP))
        self.assertNotIn("[[readme.hero_loop]]", readme)

    def test_readme_capability_matrix_is_generated_not_hand_written(self) -> None:
        """Catches the README matrix drifting from the dataset it claims to render."""
        import subprocess
        readme = (REPOSITORY_ROOT / "README.md").read_text(encoding="utf-8")
        begin = readme.index("capability-matrix:begin")
        begin = readme.index("-->", begin) + len("-->\n")
        end = readme.index("<!-- capability-matrix:end -->")
        embedded = readme[begin:end].strip()
        rendered = subprocess.run(
            ["python3", "scripts/capability-matrix-render.py"],
            cwd=REPOSITORY_ROOT, capture_output=True, text=True, check=True,
        ).stdout.strip()
        self.assertEqual(rendered, embedded)

    def test_full_capability_grid_is_generated_not_hand_written(self) -> None:
        """The full-grid page must equal the renderer's full mode, same law as the README block."""
        import subprocess
        page = (REPOSITORY_ROOT / "docs" / "capability-matrix.md").read_text(encoding="utf-8")
        rendered = subprocess.run(
            ["python3", "scripts/capability-matrix-render.py", "--mode", "full"],
            cwd=REPOSITORY_ROOT, capture_output=True, text=True, check=True,
        ).stdout.strip()
        self.assertIn(rendered, page)

    def test_readme_local_references_resolve(self) -> None:
        """Catches a README link or image that points at an absent repository file."""
        readme = (REPOSITORY_ROOT / "README.md").read_text(encoding="utf-8")
        references = re.findall(r"\]\(([^)]+)\)", readme)
        references.extend(re.findall(r'\b(?:src|srcset)="([^"]+)"', readme))

        local_paths = {
            reference.split("#", 1)[0]
            for reference in references
            if not re.match(r"^[a-z][a-z0-9+.-]*:", reference, re.IGNORECASE)
            and not reference.startswith("#")
        }
        missing = sorted(
            relative
            for relative in local_paths
            if not (REPOSITORY_ROOT / relative).is_file()
        )

        self.assertEqual([], missing)

    def assert_docs_teach_floati_not_the_retired_name(
        self, relatives: tuple[str, ...]
    ) -> None:
        for relative in relatives:
            with self.subTest(relative=relative):
                text = (REPOSITORY_ROOT / relative).read_text(encoding="utf-8")
                self.assertNotIn("scripts/slip", text)
                self.assertIsNone(RETIRED_PRODUCT_NAME.search(text))

    def test_public_living_docs_teach_floati_not_the_retired_name(self) -> None:
        """Catches living public prose or command examples left behind."""
        self.assert_docs_teach_floati_not_the_retired_name(PUBLIC_LIVING_DOCS)

    def test_private_publication_checklist_teaches_floati_not_the_retired_name(self) -> None:
        """Catches retired prose in the harbor-only publication checklist."""
        require_private_artifact(self, "docs/PUBLICATION-CHECKLIST.md")
        self.assert_docs_teach_floati_not_the_retired_name(PRIVATE_LIVING_DOCS)

    def assert_docs_are_tenant_neutral(self, relatives: tuple[str, ...]) -> None:
        for relative in relatives:
            text = (REPOSITORY_ROOT / relative).read_text(encoding="utf-8")
            if relative == "README.md":
                text = text.replace(
                    f'"tenant_id":"{PRIVATE_FLEET}"',
                    '"tenant_id":"real-receipt"',
                )
            if relative == "docs/PUBLICATION-CHECKLIST.md":
                # The publication ruling preserves one live durable coordinate;
                # it does not make that coordinate acceptable elsewhere.
                text = text.replace(PRIVATE_FLEET, "preserved-live-coordinate", 1)
            for pattern in TENANT_PATTERNS:
                with self.subTest(relative=relative, pattern=pattern.pattern):
                    self.assertIsNone(pattern.search(text))

    def test_public_living_docs_are_tenant_neutral(self) -> None:
        """Catches an owner path, host identity, or private fleet in public copy."""
        self.assert_docs_are_tenant_neutral(PUBLIC_LIVING_DOCS)

    def test_private_publication_checklist_is_tenant_neutral(self) -> None:
        """Catches tenant coordinates outside the checklist's ruled exception."""
        require_private_artifact(self, "docs/PUBLICATION-CHECKLIST.md")
        self.assert_docs_are_tenant_neutral(PRIVATE_LIVING_DOCS)

    def assert_files_do_not_expose_an_operator_account(
        self, relatives: tuple[str, ...]
    ) -> None:
        for relative in relatives:
            text = (REPOSITORY_ROOT / relative).read_text(encoding="utf-8")
            for pattern in PRIVATE_ACCOUNT_PATTERNS:
                with self.subTest(relative=relative, pattern=pattern.pattern):
                    self.assertIsNone(pattern.search(text))

    def test_public_capture_evidence_does_not_expose_an_operator_account(self) -> None:
        """Catches real account coordinates in exported capture evidence."""
        self.assert_files_do_not_expose_an_operator_account(
            PUBLIC_ACCOUNT_NEUTRAL_FILES
        )

    def test_private_capture_corpus_does_not_expose_an_operator_account(self) -> None:
        """Catches real account coordinates in the harbor-only capture corpus."""
        require_private_artifact(self, "docs/demo/corpus.v0.jsonl")
        self.assert_files_do_not_expose_an_operator_account(
            PRIVATE_ACCOUNT_NEUTRAL_FILES
        )

    def test_demo_uses_neutral_floati_lane(self) -> None:
        """Catches synthetic public frames that expose the retired lane name."""
        from floati.demo import capture_demo

        frame = capture_demo(color=False)
        self.assertIn("builder-core", frame)
        self.assertNotIn(public_ids.builder('slipway'), frame)


class InstallerShadowDocumentationTests(unittest.TestCase):
    """Issue #3: the installer-shadow scan-input requirement must be stated."""

    def test_doctor_help_states_the_installer_shadow_path_requirement(self) -> None:
        """Catches the PATH requirement being left only in the source."""
        from floati.helptext import HELP

        self.assertIn("install scripts directory", HELP["doctor"])
        self.assertNotIn("[[", HELP["doctor"])

    def test_design_doc_carries_the_installer_shadow_operator_note(self) -> None:
        """Catches the operator note never landing in the living design doc."""
        text = (REPOSITORY_ROOT / "docs/DESIGN.md").read_text(encoding="utf-8")

        self.assertEqual(0, text.count("[[design.doctor.installer_shadow_path]]"))
        self.assertIn("never promoted to `affirmative_none`", text)

    def test_the_installer_shadow_documentation_is_written(self) -> None:
        """The architect's prose landed on both surfaces; placeholders are a regression."""
        from floati.helptext import HELP

        text = (REPOSITORY_ROOT / "docs/DESIGN.md").read_text(encoding="utf-8")
        self.assertIn("installer-shadow", text)
        self.assertNotIn("[[design.doctor.", text)
        self.assertNotIn("[[help.doctor.", HELP["doctor"])

if __name__ == "__main__":
    unittest.main()
